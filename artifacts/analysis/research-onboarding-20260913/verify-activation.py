"""Read-only production/UI verification after the batch's native activation."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pyarrow.parquet as pq

from app.ui.rolling_forecast_data import load_gameplan_dashboard
from datafetching.options_runtime import _read_opra_symbol_history_cursor
from datafetching.research_onboarding import load_batch
from datafetching.symbol_onboarding import PRODUCTION_OPRA_SCHEMAS, load_plan, _write
from datafetching.symbol_universe import REPOSITORY_WATCHLIST, read_symbols
from ml.artifacts import file_checksum

folder = Path(__file__).resolve().parent
batch = load_batch(folder/'plan.json')
root = Path(batch['datastore_root'])
active = json.loads((folder/'activation.json').read_text(encoding='utf-8'))
expected = tuple(batch['candidate_symbols'])
assert active['status'] == 'ACTIVE' and active['plan_id'] == batch['plan_id']
assert read_symbols(REPOSITORY_WATCHLIST) == expected
for name, digest in active['current_pointer_hashes'].items():
    assert file_checksum(root/name) == digest, f'Publication advanced: {name}'

histories = []
for symbol in batch['selected_symbols']:
    child = folder/symbol
    plan = load_plan(child/'plan.json')
    progress = json.loads((child/'progress.json').read_text(encoding='utf-8'))
    assert progress['status'] == 'HISTORY_FETCHED' and progress['plan_id'] == plan['plan_id']
    assert set(progress['completed_requests']) == {r['request_id'] for r in plan['requests']}
    assert set(progress['providers']) == {'databento','fmp','schwab','sec'}
    assert all(result['error_files'] == 0 for result in progress['providers'].values())
    histories.append({'symbol':symbol, 'schemas':len(progress['completed_requests']), 'providers':progress['providers']})

cursors = []
for symbol in expected:
    for schema in PRODUCTION_OPRA_SCHEMAS:
        marker = _read_opra_symbol_history_cursor(root, symbol=symbol, schema=schema)
        assert marker is not None, f'Invalid production cursor: {symbol}/{schema}'
        cursors.append({'symbol':symbol, 'schema':schema, 'completed_through':marker['completed_through']})

view = load_gameplan_dashboard(root/'ml/nightly-gameplan-latest/run.json')
assert {s.symbol for s in view.symbols} == set(expected) and len(view.symbols) == len(expected)
assert view.source_row_count == 24*len(expected)
assert not view.pending_symbols

gameplan = Path(active['gameplan_run'])
model_pointer = json.loads((root/'ml/stock-trader-model-latest/run.json').read_text(encoding='utf-8'))
training = json.loads((root/model_pointer['run_path']/'training-report.json').read_text(encoding='utf-8'))
training_evidence = {}
for horizon in active['fitted_horizons']:
    symbols = pq.read_table(gameplan/f'training-cohort-{horizon}.parquet', columns=['symbol']).column('symbol').to_pylist()
    counts = {symbol:symbols.count(symbol) for symbol in expected}
    assert all(counts[symbol] > 0 for symbol in batch['selected_symbols']), f'Missing training participation: {horizon}'
    report = training['horizons'][horizon]
    assert not set(report.get('symbols_without_targets', [])) & set(batch['selected_symbols'])
    training_evidence[horizon] = {'cohort_rows_by_symbol':counts,
        **{key:report.get(key) for key in ('status','admitted_rows','qualified_scope_count','symbols_without_targets')}}

sizes = {s:0 for s in batch['selected_symbols']}
total = 0
for directory, _, filenames in os.walk(root):
    parts = set(Path(directory).relative_to(root).parts)
    owned = [s for s in sizes if s in parts or s+'.OPT' in parts]
    for filename in filenames:
        try:
            size = (Path(directory)/filename).stat().st_size
        except FileNotFoundError:
            continue
        total += size
        for symbol in owned:
            sizes[symbol] += size

result = {'plan_id':batch['plan_id'], 'status':'VERIFIED',
    'verified_at':datetime.now(timezone.utc).isoformat(), 'symbols':list(expected),
    'history':histories, 'production_cursor_count':len(cursors), 'cursors':cursors,
    'dashboard':{'source_rows':view.source_row_count, 'symbols':[s.symbol for s in view.symbols],
        'pending_symbols':[], 'operational_label':view.operational_label, 'warnings':list(view.warnings)},
    'fitted_horizons':active['fitted_horizons'],
    'qualified_enrichment_horizons':active['qualified_enrichment_horizons'],
    'training_evidence':training_evidence,
    'orders_submitted':active['orders_submitted'], 'gameplan_run':active['gameplan_run'],
    'symbol_path_bytes':sizes, 'datastore_logical_bytes':total,
    'size_basis':'Logical file sizes in symbol-named paths, including native, normalized, and retained staging; shared data counted once in datastore total.'}
_write(folder/'completion-verification.json', result)
print(json.dumps(result, indent=2))
