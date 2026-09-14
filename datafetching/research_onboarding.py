"""Onboard explicitly selected research companies as one verified Loops batch.

Planning is read-only at providers. Running a saved plan is the operator's
authorization to acquire its zero-cost history and prepare the candidate stack.
Research publication alone never registers or activates an onboarding.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid

from dateutil.relativedelta import relativedelta

from datafetching.cme_runtime import load_repository_environment
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from datafetching.symbol_onboarding import (
    VERSION as SYMBOL_VERSION, OPRA_SCHEMAS, US_EQUITIES_SCHEMAS,
    PRODUCTION_OPRA_SCHEMAS, _checksum, _now, _request, _write,
    complete_operational_history, enforce_budget, fetch_plan, load_plan,
    queue_history, register_onboarding, screen_secondary_history,
)
from datafetching.symbol_universe import REPOSITORY_WATCHLIST, WATCHLIST_ENV, normalize_symbol, read_symbols

VERSION = 'research-symbol-onboarding-v1'
POLICY = 'standard-included-history-2018-v1'


def effective_listing_date(profile_date: str, dataset_start: str, first_symbol_date: str) -> str:
    # The dataset's own start cannot establish a much older ticker's inception.
    return max(profile_date, first_symbol_date) if first_symbol_date > dataset_start[:10] else profile_date


def history_start(schema: str, *, listing: str, available: str, as_of: date,
                  floor: str = '2018-01-01') -> str:
    """Respect listing, schema availability, and the account's rolling levels."""
    if date.fromisoformat(floor) < date(2018, 1, 1):
        raise ValueError('Research onboarding cannot request dates before 2018')
    start = max(date.fromisoformat(floor), date.fromisoformat(listing), date.fromisoformat(available[:10]))
    if schema in ('tbbo', 'tcbbo', 'trades', 'bbo-1s', 'bbo-1m', 'cbbo-1s', 'cbbo-1m'):
        start = max(start, as_of - relativedelta(months=12))
    elif schema in ('mbp-10', 'mbo', 'imbalance'):
        start = max(start, as_of - relativedelta(months=1))
    return start.isoformat()


def research_selection(root: Path, week: str, symbols: list[str]) -> dict:
    from ml.opportunity_research.storage import ResearchStore
    edition = next((e for e in ResearchStore(root).history() if e['week'] == week), None)
    if edition is None:
        raise ValueError('Select a verified published research edition')
    candidates = {c['symbol']: c for c in edition['dossier']['candidates']}
    if any(s not in candidates for s in symbols):
        raise ValueError('Every selected symbol must have a company memo in this edition')
    return {'week': week, 'publication_sha256': edition['publication_sha256'],
            'report_path': edition['report_path'],
            'selected_companies': [{k: candidates[s].get(k) for k in
                ('symbol', 'security_id', 'company_name', 'decision')} for s in symbols]}


def build_batch(root: Path, output: Path, *, symbols: list[str], week: str,
                client: object, reference: str = 'COST', max_billable_bytes: int = 300_000_000_000) -> dict:
    from app.services.fmp_corporate_data import FmpCorporateDataProvider
    root, output = root.resolve(), output.resolve()
    if output.exists():
        raise ValueError('Plan already exists; resume it instead of replacing it')
    symbols = [normalize_symbol(s) for s in symbols]
    previous = read_symbols(REPOSITORY_WATCHLIST)
    if not symbols or len(set(symbols)) != len(symbols) or set(symbols).intersection(previous):
        raise ValueError('Select distinct companies that are not already active')
    if reference not in previous:
        raise ValueError('The operational reference must already be active')
    lineage = research_selection(root, week, symbols)
    as_of = datetime.now(timezone.utc).date()
    ranges = {d: client.metadata.get_dataset_range(dataset=d) for d in ('OPRA.PILLAR', 'XNAS.ITCH')}
    fmp = FmpCorporateDataProvider()
    candidate = [*previous, *symbols]
    plans = []
    all_estimates = []
    for symbol in symbols:
        profile = fmp._get_json('profile', {'symbol': symbol})
        if not isinstance(profile, list) or len(profile) != 1 or profile[0].get('symbol') != symbol:
            raise ValueError(f'Unambiguous company profile unavailable for {symbol}')
        listing = str(profile[0].get('ipoDate') or '')
        date.fromisoformat(listing)
        profile_listing = listing
        symbology = client.symbology.resolve(dataset='XNAS.ITCH', symbols=[symbol], stype_in='raw_symbol',
            stype_out='instrument_id', start_date=str(ranges['XNAS.ITCH']['start'])[:10],
            end_date=str(ranges['XNAS.ITCH']['end'])[:10])
        intervals = symbology.get('result',{}).get(symbol,[])
        if not intervals: raise ValueError(f'No verified XNAS ticker history for {symbol}')
        first_symbol_date = min(i['d0'] for i in intervals)
        listing = effective_listing_date(listing, str(ranges['XNAS.ITCH']['start']), first_symbol_date)
        company = next(c for c in lineage['selected_companies'] if c['symbol'] == symbol)
        cik = str(profile[0].get('cik') or '').lstrip('0')
        if not cik.isdigit() or not str(company['security_id']).startswith('CIK'+cik.zfill(10)+':'):
            raise ValueError(f'FMP profile and research security identity disagree for {symbol}')
        requests = []
        opra = (*PRODUCTION_OPRA_SCHEMAS, *(s for s in OPRA_SCHEMAS if s not in PRODUCTION_OPRA_SCHEMAS))
        for dataset, schemas in (('OPRA.PILLAR', opra), ('XNAS.ITCH', US_EQUITIES_SCHEMAS)):
            for schema in schemas:
                available = ranges[dataset]['schema'][schema]
                start = history_start(schema, listing=listing, available=available['start'], as_of=as_of)
                end = str(available['end'])[:10]
                if start >= end:
                    raise ValueError(f'No completed provider history for {symbol}/{schema}')
                requests.append(_request(root, symbol, dataset, schema, start, end))
        def estimate(r):
            from datafetching.databento_cold_start import _request_kwargs
            kw = _request_kwargs(r)
            return {'request_id': r['request_id'],
                    'estimated_download_size_bytes': client.metadata.get_billable_size(**kw),
                    'record_count': client.metadata.get_record_count(**kw),
                    'cost_usd': client.metadata.get_cost(**kw)}
        with ThreadPoolExecutor(max_workers=4) as pool:
            estimates = list(pool.map(estimate, requests))
        budget = enforce_budget(estimates, max_billable_bytes=max_billable_bytes,
                                max_cost_usd=0.0, free_bytes=shutil.disk_usage(root).free)
        payload = {'schema_version': SYMBOL_VERSION, 'created_at': _now(), 'datastore_root': str(root),
            'symbol': symbol, 'reference': reference, 'previous_symbols': list(previous),
            'candidate_symbols': candidate, 'requests': requests, 'estimates': estimates, 'budget': budget,
            'history_policy': POLICY, 'history_floor': '2018-01-01', 'listing_date': listing, 'cik': cik,
            'profile_ipo_date': profile_listing, 'first_observed_xnas_ticker_date': first_symbol_date,
            'research_lineage': lineage, 'shared_history': 'Reuse existing CME, FRED/ALFRED and FMP macro data'}
        plans.append({**payload, 'plan_id': _checksum(payload)})
        all_estimates.extend(estimates)
        print(f'PLANNED {symbol}: {len(requests)} schemas, cost={budget["estimated_cost_usd"]}', flush=True)
    budget = enforce_budget(all_estimates, max_billable_bytes=max_billable_bytes,
                            max_cost_usd=0.0, free_bytes=shutil.disk_usage(root).free)
    payload = {'schema_version': VERSION, 'created_at': _now(), 'datastore_root': str(root),
        'selected_symbols': symbols, 'previous_symbols': list(previous), 'candidate_symbols': candidate,
        'research_lineage': lineage, 'history_policy': POLICY, 'history_floor': '2018-01-01',
        'budget': budget, 'symbol_plan_ids': {p['symbol']: p['plan_id'] for p in plans},
        'preparation_scope': 'STOCK_ONLY', 'independent_stock_horizons': True,
        'stock_price_source': 'xnas-itch-archive-v1', 'orders_authorized': False}
    batch = {**payload, 'plan_id': _checksum(payload)}
    for plan in plans:
        _write(output.parent / plan['symbol'] / 'plan.json', plan)
    (output.parent / 'candidate-watchlist.txt').write_text('\n'.join(candidate)+'\n', encoding='utf-8')
    _write(output, batch)
    return batch


def load_batch(path: Path) -> dict:
    batch = json.loads(path.read_text(encoding='utf-8'))
    if batch.get('schema_version') != VERSION or batch.get('plan_id') != _checksum({k:v for k,v in batch.items() if k!='plan_id'}):
        raise ValueError('Batch plan checksum or version is invalid')
    expected = [*batch['previous_symbols'], *batch['selected_symbols']]
    if batch['candidate_symbols'] != expected or len(set(expected)) != len(expected):
        raise ValueError('Invalid candidate universe')
    if batch['history_policy'] != POLICY or batch['budget']['max_cost_usd'] != 0:
        raise ValueError('Research onboarding requires the zero-cost included-history policy')
    if (not batch['selected_symbols']
        or set(batch['symbol_plan_ids']) != set(batch['selected_symbols'])
        or batch.get('history_floor') != '2018-01-01'
        or batch.get('preparation_scope') != 'STOCK_ONLY'
        or batch.get('independent_stock_horizons') is not True
        or batch.get('stock_price_source') != 'xnas-itch-archive-v1'
        or batch.get('orders_authorized') is not False):
        raise ValueError('Batch scope or child-plan membership is invalid')
    for symbol, identity in batch['symbol_plan_ids'].items():
        if normalize_symbol(symbol) != symbol:
            raise ValueError('Invalid child symbol')
        plan = load_plan(path.parent / symbol / 'plan.json')
        if (plan['plan_id'] != identity or plan['candidate_symbols'] != expected
            or plan['symbol'] != symbol or plan['datastore_root'] != batch['datastore_root']
            or plan['history_floor'] != batch['history_floor']):
            raise ValueError('Symbol plan is not bound to this candidate batch')
    return batch


@contextmanager
def supervision(root: Path, token: str):
    from ml.overnight_runtime import claim_supervision
    if claim_supervision(root, token)['status'] != 'ACQUIRED':
        raise RuntimeError('Another operator owns supervision; leave its work intact')
    stop, lost = threading.Event(), threading.Event()
    def renew():
        while not stop.wait(30):
            try:
                if claim_supervision(root, token)['status'] != 'ACQUIRED':
                    lost.set(); return
            except Exception:
                lost.set(); return
    worker = threading.Thread(target=renew, daemon=True)
    worker.start()
    try:
        yield lost
    finally:
        stop.set(); worker.join(timeout=5)
        claim_supervision(root, token, release=True)


def register_batch(path: Path, batch: dict) -> None:
    from datafetching.history_scope import policy_digest, read_history_policy
    root = Path(batch['datastore_root'])
    _write(root / 'state/research-onboarding' / (batch['plan_id']+'.json'),
        {'plan_id': batch['plan_id'], 'plan_path': str(path.resolve()), 'selected_symbols': batch['selected_symbols'],
         'candidate_watchlist': str((path.parent/'candidate-watchlist.txt').resolve()), 'registered_at': _now()})
    for symbol in batch['selected_symbols']:
        p = path.parent/symbol/'plan.json'
        plan = load_plan(p)
        register_onboarding(p, plan)
        policy = {'symbol': symbol, 'history_floor': plan['history_floor'], 'listing_date': plan['listing_date'],
                  'batch_plan_id': batch['plan_id'], 'policy': POLICY}
        existing = read_history_policy(root,symbol)
        if existing:
            if existing['batch_plan_id'] != batch['plan_id']:
                raise ValueError('Existing historical scope belongs to another onboarding')
            policy = {**existing, 'listing_date':max(policy['listing_date'],existing['listing_date'])}
        _write(root/'state/symbol-history-policy'/f'{symbol}.json', {**policy, 'sha256': policy_digest(policy)})


def fresh_cost_check(path: Path, batch: dict, client: object) -> dict:
    from datafetching.databento_cold_start import _request_kwargs
    estimates = [estimate for symbol in batch['selected_symbols']
                 for estimate in load_plan(path.parent/symbol/'plan.json')['estimates']]
    enforce_budget(estimates, max_billable_bytes=batch['budget']['max_billable_bytes'],
                   max_cost_usd=0.0, free_bytes=shutil.disk_usage(Path(batch['datastore_root'])).free)
    costs = []
    for symbol in batch['selected_symbols']:
        plan = load_plan(path.parent/symbol/'plan.json')
        progress_path = path.parent/symbol/'progress.json'
        progress = json.loads(progress_path.read_text()) if progress_path.exists() else {}
        completed = set(progress.get('completed_requests', [])) if progress.get('plan_id') == plan['plan_id'] else set()
        for request in plan['requests']:
            if request['request_id'] in completed:
                continue
            cost = client.metadata.get_cost(**_request_kwargs(request))
            costs.append({'request_id': request['request_id'], 'cost_usd': cost})
            if cost != 0:
                raise ValueError('A remaining saved request now quotes a charge; replan its remaining included history before acquisition')
    receipt = {'plan_id': batch['plan_id'], 'checked_at': _now(), 'costs': costs, 'total_cost_usd': 0.0}
    _write(path.parent/'latest-cost-check.json', receipt)
    return receipt


def prepare_stock_batches(path: Path, batch: dict, client: object, lost: threading.Event) -> dict:
    """Prepare exact native stock jobs while OPRA jobs are processing.

    This writes only the native checksummed job state. The existing fetch worker
    remains the sole downloader and publisher of canonical stock partitions.
    """
    from datafetching.databento_cold_start import (
        _generic_entry_paths, _load_or_submit_batch_fallback, _request_kwargs,
        _verify_generic_partition,
    )
    root = Path(batch['datastore_root'])
    if shutil.disk_usage(root).free < batch['budget']['required_free_bytes']:
        raise ValueError('Insufficient free space for the remaining approved batch')
    jobs = []
    with exclusive_runtime_lock(path.parent/'stock-preparation.lock', process_name='Research stock batch preparation'):
        for symbol in batch['selected_symbols']:
            plan = load_plan(path.parent/symbol/'plan.json')
            for request in plan['requests']:
                if request['dataset'] != 'XNAS.ITCH':
                    continue
                if lost.is_set():
                    raise RuntimeError('Supervision lease lost')
                destination, staging = _generic_entry_paths(root, request)
                if destination.exists():
                    _verify_generic_partition(destination, request)
                    continue
                if client.metadata.get_cost(**_request_kwargs(request)) != 0:
                    raise ValueError('Stock batch now quotes a charge; do not submit it')
                job = _load_or_submit_batch_fallback(client.batch, staging_base=staging, request=request, reporter=print)
                jobs.append({'symbol':symbol, 'schema':request['schema'], 'request_id':request['request_id'], 'job_id':job})
                _write(path.parent/'stock-batch-preparation.json', {'plan_id':batch['plan_id'], 'jobs':jobs})
    receipt = {'plan_id':batch['plan_id'], 'prepared_at':_now(), 'jobs':jobs}
    _write(path.parent/'stock-batch-preparation.json', receipt)
    return receipt


def fetch_batch(path: Path, batch: dict, client: object, lost: threading.Event) -> dict:
    register_batch(path, batch)
    fresh_cost_check(path, batch, client)
    for symbol in batch['selected_symbols']:
        if lost.is_set(): raise RuntimeError('Supervision lease lost')
        queue_history(path.parent/symbol/'plan.json', client)
    prepare_stock_batches(path, batch, client, lost)
    results = []
    for symbol in batch['selected_symbols']:
        if lost.is_set(): raise RuntimeError('Supervision lease lost')
        plan_path = path.parent/symbol/'plan.json'
        results.append(fetch_plan(plan_path, client))
    return {'plan_id': batch['plan_id'], 'status': 'HISTORY_FETCHED', 'symbols': batch['selected_symbols'], 'completed_at': _now()}


def candidate_environment(path: Path) -> dict:
    batch = load_batch(path)
    candidate = path.parent/'candidate-watchlist.txt'
    if read_symbols(candidate) != tuple(batch['candidate_symbols']):
        raise ValueError('Candidate watchlist differs from the approved batch')
    return {**os.environ, WATCHLIST_ENV: str(candidate.resolve())}


def train_batch(path: Path, batch: dict, lost: threading.Event, *, resume_run: Path | None = None,
                deadline_exception: Path | None = None) -> None:
    import databento
    from datafetching.research_corporate_history import SecReader, fetch_corporate_history
    from ml.overnight_runtime import overnight_status
    root = Path(batch['datastore_root'])
    current = overnight_status(root)
    if current.get('status') == 'RUNNING':
        raise RuntimeError('A native overnight owner is already running; supervise it without duplication')
    from datafetching.research_publication import publication_locks, snapshot_references
    with publication_locks(root):
        snapshot_references(root, path.parent, batch['plan_id'])
    if deadline_exception is not None:
        from ml.overnight_runtime import _resume_configuration
        bound = json.loads((path.parent/'overnight-run.json').read_text(encoding='utf-8'))
        if resume_run is None or bound.get('plan_id') != batch['plan_id'] or Path(bound['run_path']).resolve() != resume_run.resolve():
            raise ValueError('Deadline exception requires this batch\'s exact saved attempt')
        _resume_configuration(root, resume_run, deadline_exception=deadline_exception)
    sec_reader = SecReader()
    for symbol in batch['selected_symbols']:
        if lost.is_set(): raise RuntimeError('Supervision lease lost')
        p = path.parent/symbol/'plan.json'
        plan = load_plan(p)
        progress = json.loads((p.parent/'progress.json').read_text())
        if progress.get('plan_id') != plan['plan_id'] or progress.get('status') != 'HISTORY_FETCHED':
            raise ValueError(f'History is not complete for {symbol}')
        fetch_corporate_history(plan, p.parent/'corporate-history.json', sec_reader=sec_reader)
        screen_secondary_history(plan, p.parent/'secondary-history-quality.json')
        complete_operational_history(plan, databento.Historical(os.environ['DATABENTO_API_KEY']), p.parent/'operational-history.json')
    subprocess.run([sys.executable, '-m', 'datafetching.research_onboarding', 'ownership-internal', '--plan', str(path)],
        env=candidate_environment(path), cwd=REPOSITORY_WATCHLIST.parent.parent, check=True)
    bound_path = path.parent/'overnight-run.json'
    prior = json.loads(bound_path.read_text()) if bound_path.exists() else None
    if prior:
        if prior.get('plan_id') != batch['plan_id']:
            raise ValueError('Overnight binding belongs to another batch')
        saved = Path(prior['run_path'])
        receipt_path = saved/'receipt.json'
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        if receipt.get('status') == 'COMPLETE':
            return
        if resume_run is None or resume_run.resolve() != saved.resolve():
            raise ValueError('Diagnose the saved attempt, then resume that exact run using --resume-run')
    elif resume_run is not None:
        raise ValueError('Cannot adopt an overnight run without a batch-bound source')
    command = [sys.executable, '-u', '-m', 'ml.overnight_runtime', '--datastore', str(root),
        '--once', '--stock-only', '--independent-stock-horizons', '--stock-price-source', 'xnas-itch-archive-v1']
    if resume_run is not None:
        command.extend(['--resume-run', str(resume_run.resolve())])
    if deadline_exception is not None:
        command.extend(['--deadline-exception', str(deadline_exception.resolve())])
    started = _now()
    with (path.parent/'overnight.log').open('a', encoding='utf-8') as log:
        process = subprocess.Popen(command, env=candidate_environment(path), cwd=REPOSITORY_WATCHLIST.parent.parent,
                                   stdout=log, stderr=subprocess.STDOUT)
        while True:
            try:
                code = process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                code = None
            status = overnight_status(root)
            if str(status.get('run_timestamp', '')) >= started or (resume_run and status.get('run_path') == str(resume_run)):
                _write(bound_path, {'plan_id': batch['plan_id'], 'run_path': status['run_path'], 'started_at': started,
                                    'owner_pid': process.pid, 'status': status.get('status')})
                print(f'OVERNIGHT {status.get("status")} {status.get("current_stage")} {status.get("run_path")}', flush=True)
            if code is not None: break
            if lost.is_set():
                raise RuntimeError('Supervision lease lost; existing native worker retains its own lock and receipts')
    if code:
        raise RuntimeError(f'Native overnight stage failed; inspect {path.parent / "overnight.log"} and resume the bound attempt')
    if not bound_path.exists():
        raise RuntimeError('Completed worker did not produce a bound overnight receipt')


def verify_overnight_completion(root: Path, bound: dict, batch_id: str,
                                gameplan_run: Path, gameplan_hash: str) -> dict:
    from ml.artifacts import file_checksum
    from ml.overnight_runtime import OVERNIGHT_RUNTIME_VERSION
    run = Path(bound['run_path']).resolve()
    if bound.get('plan_id') != batch_id or run.parent != (root/'ml/overnight-runs').resolve():
        raise ValueError('Overnight attempt is not bound to this batch')
    receipt = json.loads((run/'receipt.json').read_text())
    report = json.loads((run/'stage-report.json').read_text())
    pinned = report.get('enrichment_gameplan', {})
    if (receipt.get('schema_version') != OVERNIGHT_RUNTIME_VERSION
        or receipt.get('status') != 'COMPLETE' or report.get('status') != 'COMPLETE'
        or receipt.get('run_path') != run.relative_to(root).as_posix()
        or receipt.get('orders_placed') != 0 or receipt.get('broker_orders_enabled') is not False
        or receipt.get('stage_report_checksum_sha256') != file_checksum(run/'stage-report.json')
        or report.get('independent_stock_horizons') is not True
        or report.get('preparation_scope') != 'STOCK_ONLY'
        or report.get('stock_price_source') != 'xnas-itch-archive-v1'
        or pinned.get('run_path') != gameplan_run.relative_to(root).as_posix()
        or pinned.get('receipt_sha256') != gameplan_hash):
        raise ValueError('Overnight completion does not verify this stock-only Gameplan')
    completed = set(report.get('completed_stages_from_previous_attempt', []))
    completed.update(s['stage'] for s in report['stages'] if s['status'] == 'COMPLETE')
    required = {'loop_a_close_fetch', 'loop_b_directional_generation', 'stock_target_history',
                'gameplan_evaluation', 'gameplan_publication', 'stock_enrichment_training',
                'gameplan_trade_planning', 'gameplan_actuals_review'}
    if not required.issubset(completed):
        raise ValueError('Required onboarding pipeline stages are incomplete')
    for name, evidence in receipt.get('logs', {}).items():
        log = (run/name).resolve()
        if log.parent != run or file_checksum(log) != evidence['checksum_sha256']:
            raise ValueError('Overnight log evidence changed')
    return receipt


def verify_trade_plan(root: Path, gameplan_run: Path, gameplan_hash: str, symbols: tuple[str, ...]) -> dict:
    import pandas as pd
    from ml.artifacts import file_checksum, verify_manifest
    pointer = json.loads((root/'ml/gameplan-trade-plan-latest/run.json').read_text())['current']
    run = (root/pointer['run_path']).resolve()
    if run.parent != (root/'ml/gameplan-trade-plan-runs').resolve():
        raise ValueError('Trade-plan pointer escapes its expected directory')
    receipt = json.loads((run/'receipt.json').read_text())
    if (pointer['receipt_sha256'] != file_checksum(run/'receipt.json')
        or receipt.get('status') != 'COMPLETE' or receipt.get('orders_placed') != 0
        or receipt.get('broker_orders_enabled') is not False
        or receipt.get('manifest_sha256') != file_checksum(run/'manifest.json')
        or receipt.get('source_gameplan_run') != gameplan_run.relative_to(root).as_posix()
        or receipt.get('source_receipt_sha256') != gameplan_hash):
        raise ValueError('Trade planning is not complete for this candidate Gameplan')
    verify_manifest(run)
    rows = pd.read_parquet(run/'trade-plan.parquet')
    if rows.groupby('symbol').size().to_dict() != {s:24 for s in symbols} or rows.duplicated(['symbol','route']).any():
        raise ValueError('Trade planning omits or duplicates candidate routes')
    return {'run_path': str(run), 'receipt_sha256': file_checksum(run/'receipt.json'), 'rows': len(rows)}


def validate_batch(path: Path) -> dict:
    import pandas as pd
    from ml.artifacts import file_checksum
    from ml.nightly_gameplan import read_current_gameplan
    from ml.stock_trader.model import IndependentEnrichmentModel, load_current_enrichment_model
    from ml.independent_stock_targets import GROUPS, STOCK_TARGET_CONTRACT_VERSION
    batch = load_batch(path)
    root = Path(batch['datastore_root'])
    expected = tuple(batch['candidate_symbols'])
    if read_symbols() != expected:
        raise ValueError('Validation must run in a fresh process with the batch candidate watchlist')
    publication = read_current_gameplan(root)
    config = publication.manifest['configuration']
    if (tuple(config['symbols']) != expected or config.get('target_contract_version') != STOCK_TARGET_CONTRACT_VERSION
        or config.get('target_price_source_contract') != batch['stock_price_source']
        or config.get('preparation_scope') != 'STOCK_ONLY'):
        raise ValueError('Current Gameplan has the wrong universe or horizon contract')
    counts = {}
    for filename in ('forecasts.parquet','option-strategy-intents.parquet'):
        frame = pd.read_parquet(publication.run_directory/filename)
        actual = frame.groupby('symbol').size().to_dict()
        if actual != {s:24 for s in expected} or frame.duplicated(['symbol','route']).any():
            raise ValueError(f'Incomplete or duplicated routes in {filename}')
        if filename == 'option-strategy-intents.parquet' and not frame.plan_status.eq('NO_TRADE_STOCK_ONLY').all():
            raise ValueError('Candidate options intents must retain stock-only placeholders')
        counts[filename] = len(frame)
    model = load_current_enrichment_model(root)
    if not isinstance(model, IndependentEnrichmentModel) or set(model.horizon_models) != set(GROUPS):
        raise ValueError('Current stock model must contain all four independent horizon fits')
    for fitted in model.horizon_models.values():
        if any('symbol_'+s not in fitted.feature_names for s in expected):
            raise ValueError('A fitted horizon omits a candidate symbol feature')
    pointer = json.loads((root/'ml/stock-trader-model-latest/run.json').read_text())
    payload = json.loads((root/pointer['run_path']/'model.json').read_text())
    if payload['source_publication']['source_gameplan_run'] != publication.run_directory.relative_to(root).as_posix():
        raise ValueError('Enrichment model is not trained from this Gameplan')
    bound = json.loads((path.parent/'overnight-run.json').read_text())
    receipt_path = Path(bound['run_path'])/'receipt.json'
    gameplan_hash = file_checksum(publication.run_directory/'receipt.json')
    verify_overnight_completion(root, bound, batch['plan_id'], publication.run_directory, gameplan_hash)
    trade_plan = verify_trade_plan(root, publication.run_directory, gameplan_hash, expected)
    pointer_hashes = {name: file_checksum(root/name) for name in
        ('ml/nightly-gameplan-latest/run.json', 'ml/stock-trader-model-latest/run.json',
         'ml/gameplan-trade-plan-latest/run.json')}
    result = {'plan_id': batch['plan_id'], 'validated_at': _now(), 'symbols': list(expected),
        'gameplan_run': str(publication.run_directory), 'gameplan_receipt_sha256': gameplan_hash,
        'action_date': publication.receipt['action_date'], 'counts': counts,
        'trade_plan': trade_plan, 'current_pointer_hashes': pointer_hashes,
        'stock_model_fingerprint': model.model_fingerprint, 'fitted_horizons': list(model.horizon_models),
        'qualified_enrichment_horizons': list(model.supported_horizons), 'orders_submitted': 0,
        'overnight_run': bound['run_path'], 'overnight_receipt_sha256': file_checksum(receipt_path)}
    _write(path.parent/'validation.json', result)
    return result


def validate_in_subprocess(path: Path) -> None:
    subprocess.run([sys.executable, '-m', 'datafetching.research_onboarding', 'validate-internal', '--plan', str(path)],
        env=candidate_environment(path), cwd=REPOSITORY_WATCHLIST.parent.parent, check=True)


def activate_batch(path: Path, batch: dict) -> dict:
    from ml.artifacts import file_checksum
    validation = json.loads((path.parent/'validation.json').read_text())
    if validation.get('plan_id') != batch['plan_id'] or validation.get('symbols') != batch['candidate_symbols']:
        raise ValueError('Validation does not authorize this exact candidate batch')
    if file_checksum(Path(validation['gameplan_run'])/'receipt.json') != validation['gameplan_receipt_sha256']:
        raise ValueError('Validated Gameplan receipt changed')
    for symbol in batch['selected_symbols']:
        p = path.parent/symbol/'plan.json'
        plan = load_plan(p)
        for filename, status in (('progress.json','HISTORY_FETCHED'),('corporate-history.json','COMPLETE'),('operational-history.json','COMPLETE'),('secondary-history-quality.json','COMPLETE')):
            r = json.loads((p.parent/filename).read_text())
            if r.get('plan_id') != plan['plan_id'] or r.get('status') != status:
                raise ValueError(f'{symbol} source receipt is incomplete: {filename}')
    with exclusive_runtime_lock(REPOSITORY_WATCHLIST.with_suffix('.activation.lock'), process_name='Research batch activation'):
        root = Path(batch['datastore_root'])
        pointers = validation.get('current_pointer_hashes', {})
        if set(pointers) != {'ml/nightly-gameplan-latest/run.json', 'ml/stock-trader-model-latest/run.json',
                             'ml/gameplan-trade-plan-latest/run.json'}:
            raise ValueError('Activation requires fresh current-pointer validation')
        if any(file_checksum(root/name) != digest for name,digest in pointers.items()):
            raise ValueError('Current publication advanced; validate the candidate again before activation')
        current = read_symbols(REPOSITORY_WATCHLIST)
        if current not in (tuple(batch['previous_symbols']), tuple(batch['candidate_symbols'])):
            raise ValueError('Production membership changed independently; reconcile it before activation')
        if current != tuple(batch['candidate_symbols']):
            content = REPOSITORY_WATCHLIST.read_text(encoding='utf-8').rstrip()+'\n'+'\n'.join(batch['selected_symbols'])+'\n'
            temporary = REPOSITORY_WATCHLIST.with_suffix('.txt.tmp')
            temporary.write_text(content, encoding='utf-8'); temporary.replace(REPOSITORY_WATCHLIST)
        receipt = {**validation, 'status':'ACTIVE', 'activated_at': _now(), 'research_lineage': batch['research_lineage']}
        _write(path.parent/'activation.json', receipt)
        for symbol in batch['selected_symbols']:
            _write(path.parent/symbol/'activation.json', {**receipt, 'batch_plan_id': batch['plan_id'], 'plan_id': batch['symbol_plan_ids'][symbol]})
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    plan = sub.add_parser('plan')
    plan.add_argument('--symbols', nargs='+', required=True)
    plan.add_argument('--edition', required=True)
    plan.add_argument('--datastore-target', choices=tuple(DATASTORE_TARGETS), default='pc')
    plan.add_argument('--output', type=Path, required=True)
    plan.add_argument('--reference', default='COST')
    plan.add_argument('--max-billable-bytes', type=int, default=300_000_000_000)
    for command in ('fetch', 'train', 'validate', 'validate-internal', 'ownership-internal', 'activate', 'finalize', 'run', 'status'):
        phase = sub.add_parser(command)
        phase.add_argument('--plan', type=Path, required=True)
        phase.add_argument('--owner-token', default=None)
        phase.add_argument('--resume-run', type=Path, default=None)
        phase.add_argument('--deadline-exception', type=Path, default=None)
    args = parser.parse_args(argv)
    load_repository_environment()
    if args.command == 'plan':
        import databento
        result = build_batch(resolve_datastore_dir(target=args.datastore_target), args.output,
            symbols=args.symbols, week=args.edition, reference=args.reference,
            max_billable_bytes=args.max_billable_bytes, client=databento.Historical(os.environ['DATABENTO_API_KEY']))
        print(json.dumps({'plan': str(args.output), 'plan_id': result['plan_id'], 'budget': result['budget']}))
        return 0
    path = args.plan.resolve()
    batch = load_batch(path)
    if args.command == 'validate-internal':
        print(json.dumps(validate_batch(path))); return 0
    if args.command == 'ownership-internal':
        from datafetching.research_ownership import prepare_ownership
        if read_symbols() != tuple(batch['candidate_symbols']):
            raise ValueError('Ownership preparation requires the exact candidate universe')
        print(json.dumps(prepare_ownership(Path(batch['datastore_root']), symbols=tuple(batch['candidate_symbols']),
            plan_id=batch['plan_id'], output=path.parent/'ownership.json')))
        return 0
    if args.command == 'status':
        result = {name:json.loads((path.parent/name).read_text()) for name in
            ('progress.json','validation.json','activation.json') if (path.parent/name).exists()}
        result['symbol_progress'] = []
        for symbol in batch['selected_symbols']:
            child = path.parent/symbol
            progress = json.loads((child/'progress.json').read_text()) if (child/'progress.json').exists() else {}
            corporate = json.loads((child/'corporate-history.json').read_text()) if (child/'corporate-history.json').exists() else {}
            result['symbol_progress'].append({'symbol':symbol, 'history_status':progress.get('status','PENDING'),
                'completed_schema_requests':len(progress.get('completed_requests',[])),
                'total_schema_requests':len(load_plan(child/'plan.json')['requests']),
                'corporate_status':corporate.get('status','PENDING'),
                'sec_filing_count':corporate.get('sec_filing_count')})
        result['production_symbols'] = list(read_symbols(REPOSITORY_WATCHLIST))
        print(json.dumps(result, indent=2)); return 0
    root = Path(batch['datastore_root'])
    with supervision(root, args.owner_token or str(uuid.uuid4())) as lost:
        with exclusive_runtime_lock(root/'.ducketz-research-onboarding.lock', process_name='Research onboarding batch'):
            progress = {'plan_id': batch['plan_id'], 'stage': args.command, 'status': 'RUNNING', 'updated_at': _now(), 'owner_pid': os.getpid()}
            _write(path.parent/'progress.json', progress)
            try:
                if args.command in ('fetch','run'):
                    import databento
                    receipt = fetch_batch(path, batch, databento.Historical(os.environ['DATABENTO_API_KEY']), lost)
                    _write(path.parent/'history-receipt.json', receipt)
                if args.command in ('train','run'):
                    train_batch(path, batch, lost, resume_run=args.resume_run, deadline_exception=args.deadline_exception)
                if args.command in ('validate','activate'):
                    validate_in_subprocess(path)
                if args.command == 'activate':
                    activate_batch(path, batch)
                if args.command in ('finalize','run'):
                    from datafetching.research_publication import finalize_batch
                    finalize_batch(path, batch)
                progress.update(status='COMPLETE', completed_at=_now())
            except Exception as exc:
                progress.update(status='FAILED', error_type=type(exc).__name__, error=str(exc), updated_at=_now())
                if (args.command in ('train','run') and (path.parent/'production-baseline.json').exists()
                        and (path.parent/'overnight-run.json').exists() and not lost.is_set()):
                    try:
                        from datafetching.research_publication import publication_locks, restore_references
                        bound = json.loads((path.parent/'overnight-run.json').read_text(encoding='utf-8'))
                        report = json.loads((Path(bound['run_path'])/'stage-report.json').read_text(encoding='utf-8'))
                        if report.get('status') != 'RUNNING' and report.get('enrichment_gameplan'):
                            with publication_locks(root):
                                restore_references(root, path.parent, batch['plan_id'], report['enrichment_gameplan']['run_path'])
                    except Exception as restore_error:
                        progress['restoration_error'] = str(restore_error)
                raise
            finally:
                _write(path.parent/'progress.json', progress)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
