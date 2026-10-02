import json
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace

import pytest

from datafetching.research_publication import POINTERS, snapshot_references, restore_references


def test_failed_candidate_restores_baseline_and_keeps_candidate_evidence(tmp_path):
    folder = tmp_path/'batch'; folder.mkdir()
    for name in POINTERS:
        path = tmp_path/name; path.parent.mkdir(parents=True)
        path.write_text('{"current":{"run_path":"old"}}')
    baseline = snapshot_references(tmp_path, folder, 'batch')
    candidate = {'current':{'run_path':'candidate'}}
    (tmp_path/POINTERS[0]).write_text(json.dumps(candidate))
    restore_references(tmp_path, folder, 'batch', 'candidate')
    assert all(json.loads((tmp_path/name).read_text()) == baseline[name] for name in POINTERS)
    evidence = json.loads((folder/'candidate-references.json').read_text())
    assert evidence['pointers'][POINTERS[0]] == candidate


def test_independent_publication_and_changed_baseline_cannot_be_overwritten(tmp_path):
    folder = tmp_path/'batch'; folder.mkdir()
    for name in POINTERS:
        path = tmp_path/name; path.parent.mkdir(parents=True)
        path.write_text('{"current":{"run_path":"old"}}')
    snapshot_references(tmp_path, folder, 'batch')
    (tmp_path/POINTERS[0]).write_text('{"current":{"run_path":"independent"}}')
    before = {name:(tmp_path/name).read_bytes() for name in POINTERS}
    with pytest.raises(ValueError, match='independent publication'):
        restore_references(tmp_path, folder, 'batch', 'candidate')
    assert before == {name:(tmp_path/name).read_bytes() for name in POINTERS}
    saved = json.loads((folder/'production-baseline.json').read_text())
    saved['pointers'][POINTERS[0]] = {'tampered':True}
    (folder/'production-baseline.json').write_text(json.dumps(saved))
    with pytest.raises(ValueError, match='checksum'):
        snapshot_references(tmp_path, folder, 'batch')


@pytest.mark.parametrize('local',[False,True])
def test_failed_finalization_restores_this_machines_production_pointers(tmp_path,monkeypatch,local):
    import datafetching.research_onboarding as onboarding
    import datafetching.research_publication as publication
    import datafetching.symbol_universe as universe
    import ml.artifacts as artifacts
    import ml.nightly_gameplan as gameplan
    import ml.preparation_deadline as deadlines

    shared = tmp_path/'watchlist.txt'
    shared.write_text('AAPL\n')
    monkeypatch.setattr(universe,'REPOSITORY_WATCHLIST',shared)
    watchlist = shared.with_name('watchlist.local.txt') if local else shared
    watchlist.write_text('MSFT\n')
    candidate = tmp_path/'candidate-watchlist.txt'
    candidate.write_text('MSFT\nIONQ\n')
    monkeypatch.setenv(universe.WATCHLIST_ENV,str(candidate))
    folder = tmp_path/'batch'; folder.mkdir()
    run = tmp_path/'ml/overnight-runs/candidate'; run.mkdir(parents=True)
    source = 'ml/nightly-gameplan-runs/candidate'
    source_run = tmp_path/source; source_run.mkdir(parents=True)
    (source_run/'receipt.json').write_text('{}')
    (folder/'overnight-run.json').write_text(json.dumps({'run_path':str(run)}))
    (run/'stage-report.json').write_text(json.dumps({
        'enrichment_gameplan':{'run_path':source},'deadline_at':'2099-01-01T00:00:00Z'}))
    for name in POINTERS:
        pointer = tmp_path/name; pointer.parent.mkdir(parents=True)
        pointer.write_text('{"current":{"run_path":"old"}}')
    baseline = {name:json.loads((tmp_path/name).read_text()) for name in POINTERS}
    model = tmp_path/'ml/stock-trader-model-runs/candidate'; model.mkdir(parents=True)
    (model/'model.json').write_text(json.dumps({'source_publication':{'source_gameplan_run':source}}))
    (model/'manifest.json').write_text('{}')
    (model/'receipt.json').write_text(json.dumps({
        'trained_at':'2026-09-29T00:00:00Z','run_path':model.relative_to(tmp_path).as_posix(),
        'model_fingerprint':'candidate','model_sha256':artifacts.file_checksum(model/'model.json'),
        'manifest_sha256':artifacts.file_checksum(model/'manifest.json')}))
    monkeypatch.setattr(publication,'publication_locks',lambda _:nullcontext())
    monkeypatch.setattr(deadlines,'preparation_deadline',lambda *args:(artifacts.utc_timestamp()+timedelta(days=1),None))
    monkeypatch.setattr(gameplan,'read_gameplan_run',lambda *args:SimpleNamespace(
        run_directory=source_run,pointer={'current':{'run_path':source}}))
    monkeypatch.setattr(onboarding,'verify_overnight_completion',lambda *args:None)
    monkeypatch.setattr(onboarding,'verify_trade_plan',lambda *args:None)
    monkeypatch.setattr(artifacts,'verify_manifest',lambda *args:None)
    def failed_validation(path):
        raise ValueError('candidate validation failed')
    monkeypatch.setattr(onboarding,'validate_in_subprocess',failed_validation)
    batch = {'plan_id':'batch','datastore_root':str(tmp_path),
             'previous_symbols':['MSFT'],'candidate_symbols':['MSFT','IONQ']}

    with pytest.raises(ValueError,match='candidate validation failed'):
        publication.finalize_batch(folder/'plan.json',batch)

    assert {name:json.loads((tmp_path/name).read_text()) for name in POINTERS} == baseline
    assert json.loads((folder/'automatic-restoration.json').read_text())['status']=='BASELINE_RESTORED'
    assert watchlist.read_text()=='MSFT\n'
    assert candidate.read_text()=='MSFT\nIONQ\n'
    if local:
        assert shared.read_text()=='AAPL\n'
