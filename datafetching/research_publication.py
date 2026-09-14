"""Publish a completed research batch with rollback of its current references."""
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path

from datafetching.runtime_lock import exclusive_runtime_lock
from datafetching.symbol_onboarding import _checksum, _write

POINTERS = ('ml/nightly-gameplan-latest/run.json', 'ml/stock-trader-model-latest/run.json',
            'ml/gameplan-trade-plan-latest/run.json')


@contextmanager
def publication_locks(root):
    with ExitStack() as locks:
        for name in ('locks/independent-stock-session.lock', 'locks/stock-trader-hourly.lock',
                     '.ducketz-overnight-runtime.lock', '.ducketz-nightly-gameplan.lock',
                     'locks/stock-trader-training.lock', 'state/gameplan-trade-planning.lock'):
            locks.enter_context(exclusive_runtime_lock(root/name, process_name='Research publication handoff'))
        yield


def snapshot_references(root, folder, plan_id):
    path = folder/'production-baseline.json'
    if path.exists():
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('plan_id') != plan_id or saved.get('sha256') != _checksum(saved['pointers']):
            raise ValueError('Saved production baseline differs from the batch or checksum')
        return saved['pointers']
    pointers = {name:json.loads((root/name).read_text(encoding='utf-8')) for name in POINTERS}
    _write(path, {'plan_id':plan_id, 'pointers':pointers, 'sha256':_checksum(pointers)})
    return pointers


def _source_for_pointer(root, name, pointer):
    if name == POINTERS[0]:
        return pointer['current']['run_path']
    if name == POINTERS[1]:
        run = (root/pointer['run_path']).resolve()
        if run.parent != root/'ml/stock-trader-model-runs':
            raise ValueError('Model pointer escapes its archive')
        return json.loads((run/'model.json').read_text(encoding='utf-8'))['source_publication']['source_gameplan_run']
    run = (root/pointer['current']['run_path']).resolve()
    if run.parent != root/'ml/gameplan-trade-plan-runs':
        raise ValueError('Trade pointer escapes its archive')
    return json.loads((run/'receipt.json').read_text(encoding='utf-8'))['source_gameplan_run']


def restore_references(root, folder, plan_id, candidate_source):
    baseline = snapshot_references(root, folder, plan_id)
    current = {name:json.loads((root/name).read_text(encoding='utf-8')) for name in POINTERS}
    for name, pointer in current.items():
        if pointer != baseline[name] and _source_for_pointer(root, name, pointer) != candidate_source:
            raise ValueError('An independent publication advanced; automatic restoration cannot replace it')
    _write(folder/'candidate-references.json', {'plan_id':plan_id, 'pointers':current, 'candidate_source':candidate_source})
    for name in reversed(POINTERS):
        if current[name] != baseline[name]:
            _write(root/name, baseline[name])
    _write(folder/'automatic-restoration.json', {'plan_id':plan_id, 'status':'BASELINE_RESTORED',
                                               'candidate_source':candidate_source, 'orders_placed':0})


def finalize_batch(path, batch):
    from datafetching.research_onboarding import (verify_overnight_completion, verify_trade_plan,
                                                 validate_in_subprocess, activate_batch)
    from datafetching.symbol_universe import REPOSITORY_WATCHLIST, read_symbols
    from ml.artifacts import file_checksum, verify_manifest
    from ml.nightly_gameplan import read_gameplan_run
    root, folder = Path(batch['datastore_root']).resolve(), path.parent
    bound = json.loads((folder/'overnight-run.json').read_text(encoding='utf-8'))
    report = json.loads((Path(bound['run_path'])/'stage-report.json').read_text(encoding='utf-8'))
    source = report['enrichment_gameplan']['run_path']
    from ml.preparation_deadline import preparation_deadline
    from ml.artifacts import utc_timestamp
    exception = report.get('deadline_exception')
    exception_path = Path(exception['path']) if exception else None
    if exception and file_checksum(exception_path) != exception['sha256']:
        raise ValueError('The saved operator deadline exception changed')
    deadline, _ = preparation_deadline(root, root/source, report['deadline_at'], utc_timestamp(), exception_path)
    if utc_timestamp() >= deadline:
        raise ValueError('Batch activation deadline passed; retain the completed candidate for review')
    with publication_locks(root):
        if read_symbols(REPOSITORY_WATCHLIST) not in (tuple(batch['previous_symbols']), tuple(batch['candidate_symbols'])):
            raise ValueError('Production membership changed independently')
        baseline = snapshot_references(root, folder, batch['plan_id'])
        publication = read_gameplan_run(root, root/source)
        source_hash = file_checksum(publication.run_directory/'receipt.json')
        verify_overnight_completion(root, bound, batch['plan_id'], publication.run_directory, source_hash)
        verify_trade_plan(root, publication.run_directory, source_hash, tuple(batch['candidate_symbols']))
        models = []
        for receipt_path in (root/'ml/stock-trader-model-runs').glob('*/receipt.json'):
            model_path = receipt_path.parent/'model.json'
            if not model_path.exists():
                continue
            payload = json.loads(model_path.read_text(encoding='utf-8'))
            if payload.get('source_publication', {}).get('source_gameplan_run') == source:
                receipt = json.loads(receipt_path.read_text(encoding='utf-8'))
                models.append((receipt['trained_at'], receipt_path, receipt))
        if not models:
            raise ValueError('No fitted enrichment publication belongs to the pinned candidate')
        _, receipt_path, receipt = max(models, key=lambda item:item[0])
        verify_manifest(receipt_path.parent)
        if (file_checksum(receipt_path.parent/'model.json') != receipt['model_sha256']
                or file_checksum(receipt_path.parent/'manifest.json') != receipt['manifest_sha256']):
            raise ValueError('Candidate enrichment evidence changed')
        model_pointer = {key:receipt[key] for key in ('run_path','trained_at','model_fingerprint','manifest_sha256','model_sha256')}
        model_pointer.update(schema_version='stock-trader-enrichment-model-pointer-v1', receipt_sha256=file_checksum(receipt_path))
        for name in POINTERS:
            current = json.loads((root/name).read_text(encoding='utf-8'))
            if current != baseline[name] and _source_for_pointer(root, name, current) != source:
                raise ValueError('Current production references advanced independently')
        try:
            _write(root/POINTERS[1], model_pointer)
            _write(root/POINTERS[0], publication.pointer)
            validate_in_subprocess(path)
            if utc_timestamp() >= deadline:
                raise ValueError('Batch activation deadline passed during validation')
            return activate_batch(path, batch)
        except Exception:
            if read_symbols(REPOSITORY_WATCHLIST) == tuple(batch['previous_symbols']):
                restore_references(root, folder, batch['plan_id'], source)
            raise
