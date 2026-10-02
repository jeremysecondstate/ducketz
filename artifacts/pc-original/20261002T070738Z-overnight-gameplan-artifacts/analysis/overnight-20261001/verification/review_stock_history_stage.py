"""Bounded offline current-stage review; no acquisition, fitting or state writes."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import math
import sys

REPO = Path('C:/dev/ducketz')
ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
RUN = ROOT/'ml/stock-target-history-runs/20261002T062041.538710Z'
NATIVE = ROOT/'ml/overnight-runs/20261002T040848.225355Z'
sys.path.insert(0, str(REPO))
from audit_environment import verify_environment
from datafetching.databento_cold_start import (
    _validate_manifest_checksum, _validate_manifest_included_scope,
    _validate_execution_request_identity, _verify_generic_partition, _checksum,
)
from datafetching.history_scope import price_floor
from datafetching.symbol_universe import read_symbols
from ml.stock_target_history import _verified_target_cursor
import pandas as pd

read = lambda p: json.loads(Path(p).read_text(encoding='utf-8-sig'))
sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
output = OUT/'stock-history-stage-review.json'
assert not output.exists(), 'Preserve prior observation'
baseline = verify_environment()
symbols = tuple(read_symbols(REPO/'datafetching/watchlist.txt'))
receipt, manifest = read(RUN/'receipt.json'), read(RUN/'manifest.json')
cost, capacity = read(RUN/'cost-preflight.json'), read(RUN/'preflight.json')
progress = read(RUN/'progress.json')
assert receipt['status'] == 'COMPLETE' and receipt['orders_placed'] == 0
assert receipt['completed_through'] == '2026-10-02'
for field, name in [('manifest_sha256','manifest.json'), ('cost_preflight_sha256','cost-preflight.json'),
                    ('preflight_sha256','preflight.json')]:
    assert receipt[field] == sha(RUN/name)
_validate_manifest_checksum(manifest)
_validate_manifest_included_scope(manifest)
requests = manifest['requests']
ids = {r['request_id'] for r in requests}
assert len(ids) == len(requests) == len(symbols) == 11
assert {r['symbol_scope'][0] for r in requests} == set(symbols)
assert {r['request_id'] for r in cost['requests']} == ids
estimates = {r['request_id']: r for r in capacity['estimates']}
assert set(estimates) == set(progress['entries']) == ids
assert cost['maximum_cost_usd'] == cost['total_cost_usd'] == receipt['estimated_cost_usd'] == 0
assert all(r['estimated_cost_usd'] == 0 for r in cost['requests'])
assert capacity['capacity_pass'] is True and capacity['shortfall_bytes'] == 0
assert capacity['available_free_bytes'] >= capacity['required_free_bytes']
assert capacity['manifest_id'] == manifest['manifest_id']
bounds = cost['dataset_range']['ohlcv-1m']
fresh = []
for request in requests:
    symbol = request['symbol_scope'][0]
    assert request['symbol_scope'] == [symbol] and request['schema'] == 'ohlcv-1m'
    assert request['dataset'] == 'XNAS.ITCH' and request['stype_in'] == 'raw_symbol'
    assert request['start'] == '2026-09-29' and request['end'] == '2026-10-02'
    assert bounds['start'] <= request['start'] < request['end'] <= bounds['end']
    estimate = estimates[request['request_id']]
    assert all(estimate[k] == request[k] for k in ('dataset','schema','symbol_scope','start','end'))
    for k in ('estimated_download_size_bytes','record_count'):
        value = estimate[k]
        assert not isinstance(value,bool) and math.isfinite(value) and int(value) == value > 0
    entry = progress['entries'][request['request_id']]
    assert entry['status'] == 'PUBLISHED' and entry['details']['preflight'] == estimate
    _validate_execution_request_identity(ROOT, request)
    partition = Path(request['storage_path']).resolve()
    _verify_generic_partition(partition, request)
    pm, pr = read(partition/'manifest.json'), read(partition/'receipt.json')
    assert pr['raw_checksum_sha256'] == pm['raw']['checksum_sha256']
    assert pr['normalized_checksum_sha256'] == pm['normalized']['checksum_sha256']
    assert pd.Timestamp(cost['generated_at']) <= pd.Timestamp(capacity['generated_at']) <= pd.Timestamp(pm['published_at']) <= pd.Timestamp(receipt['completed_at'])
    cursor = _verified_target_cursor(ROOT, symbol)
    assert cursor['completed_through'] == '2026-10-02' and cursor['request_id'] == request['request_id']
    fresh.append({'symbol':symbol,'request_id':request['request_id'],'start':request['start'],
        'end_exclusive':request['end'],'partition':str(partition),'manifest_sha256':sha(partition/'manifest.json'),
        'receipt_sha256':sha(partition/'receipt.json'),'raw_sha256':pm['raw']['checksum_sha256'],
        'normalized_sha256':pm['normalized']['checksum_sha256'],'raw_bytes':pm['raw']['size_bytes'],
        'normalized_bytes':pm['normalized']['size_bytes'],'observed_rows':pm['normalized']['row_count'],
        'first_observed':pm['normalized']['earliest_timestamp'],'last_observed':pm['normalized']['latest_timestamp'],
        'provider_warnings':pm.get('provider_warnings',[]),'current_cursor_verified':True})
assert receipt['counts'] == {'downloaded':11,'failed':0,'no_data':0,'verified':0}
assert capacity['total_estimated_download_size_bytes'] == sum(r['estimated_download_size_bytes'] for r in estimates.values())
assert capacity['total_record_count'] == sum(r['record_count'] for r in estimates.values())

binding = receipt['feature_history_extension']
assert binding['required'] is True and binding['status'] == 'VERIFIED'
assert Path(binding['path']).resolve() == RUN/'feature-history-extension.json'
assert binding['sha256'] == sha(RUN/'feature-history-extension.json')
extension, feature = read(RUN/'feature-history-extension.json'), read(RUN/'feature-history-manifest.json')
assert extension['manifest_sha256'] == sha(RUN/'feature-history-manifest.json')
assert extension['status'] == 'VERIFIED' and extension['current_cursors_preserved'] is True
assert extension['requests'] == 0 and extension['completed_requests'] == [] and feature['requests'] == []
assert extension['estimated_cost_usd'] == extension['orders_placed'] == 0
body = {k:v for k,v in feature.items() if k not in ('manifest_id','semantic_checksum_sha256')}
digest = _checksum(body)
assert feature['semantic_checksum_sha256'] == digest and feature['manifest_id'] == digest[:24]
assert feature['source_contract'] == 'xnas-itch-archive-v1' and feature['maximum_cost_usd'] == 0
assert feature['maximum_billable_bytes'] == 20_000_000_000
assert feature['cursor_policy'] == 'preserve_exact_existing_cursor_no_prefix_cursor_write'
snapshots = read(RUN/'feature-history-original-cursors.json')
assert set(feature['symbols']) == set(snapshots) == set(symbols)
prefixes, metadata_count = {}, 0
for symbol, evidence in feature['symbols'].items():
    snapshot = snapshots[symbol]
    assert hashlib.sha256(snapshot['content'].encode()).hexdigest() == snapshot['sha256']
    assert json.loads(snapshot['content']) == evidence['cursor_before']
    assert evidence['cursor_before']['completed_through'] == '2026-10-01'
    for partition in evidence['feature_partitions']+evidence['minute_partitions']:
        path = Path(partition['manifest_path'])
        assert sha(path) == partition['manifest_sha256']
        pm, pr = read(path), read(path.parent/'receipt.json')
        assert pm['request'] == partition['request'] and pm['request']['dataset'] == 'XNAS.ITCH'
        assert pm['request']['symbol_scope'] == [symbol]
        assert pr['manifest_checksum_sha256'] == partition['manifest_sha256']
        assert pr['request_id'] == pm['request']['request_id']
        metadata_count += 1
    start = min(pd.Timestamp(p['earliest_observed_timestamp']).date() for p in evidence['feature_partitions'])
    if evidence['history_policy'] is not None:
        start = max(start, price_floor(evidence['history_policy']).date())
    minute = min(pd.Timestamp(p['request']['start']).date() for p in evidence['minute_partitions'])
    assert start.isoformat() == evidence['feature_start'] and minute.isoformat() == evidence['minute_start']
    assert minute <= start and evidence['status'] == 'ALREADY_COVERED'
    prefixes[symbol] = {'feature_start':str(start),'minute_start':str(minute),'status':'ALREADY_COVERED',
        'feature_partitions':len(evidence['feature_partitions']),'minute_partitions':len(evidence['minute_partitions']),
        'saved_cursor_before':'2026-10-01','current_verified_cursor':'2026-10-02'}
native = read(NATIVE/'stage-report.json')
stage = next(s for s in native['stages'] if s['stage'] == 'stock_target_history')
assert stage['status'] == 'COMPLETE' and stage['exit_code'] == 0
assert str(RUN/'receipt.json') in (NATIVE/'stock_target_history.log').read_text()
result = {'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'VERIFIED_STAGE_EVIDENCE',
    'native_run':str(NATIVE),'native_status_at_review':native['status'],'history_run':str(RUN),
    'source_session':'2026-10-01','completed_through_exclusive':'2026-10-02','symbols':list(symbols),
    'receipt_counts':receipt['counts'],'estimated_cost_usd':0,'provider_available_range':bounds,
    'capacity':{k:capacity[k] for k in ('capacity_pass','total_estimated_download_size_bytes','total_record_count','required_free_bytes','available_free_bytes')},
    'fresh_partitions':fresh,'prefixes':prefixes,'new_prefix_requests':0,
    'historical_source_manifest_and_receipt_bindings_verified':metadata_count,
    'prefix_necessity_recomputed_from_recorded_observation_metadata':True,
    'source_baseline':baseline,'source_baseline_after':verify_environment(),
    'evidence':{n:{'path':str(RUN/n),'sha256':sha(RUN/n)} for n in
        ('receipt.json','manifest.json','cost-preflight.json','preflight.json','progress.json',
         'feature-history-extension.json','feature-history-manifest.json','feature-history-original-cursors.json')},
    'limitations':['Fresh eleven raw/normalized payloads and normalized timestamp ranges verified by native offline reader.',
        'Older prefix necessity recomputed from hash-bound recorded source observation metadata; older payload rehash, full archive feature/cohort and seconds/minutes verification remain final audit.',
        'Native extension reports exact cursor preservation; ordinary daily acquisition correctly advanced cursors afterward.',
        'No native full completion, independent directional promotion, sizing qualification or actual fills inferred.'],
    'provider_calls':0,'orders_placed':0,'production_mutations':0,'heavy_final_audit_started':False,
    'verifier':{'path':str(Path(__file__).resolve()),'sha256':sha(Path(__file__))}}
output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':result['status'],'fresh_partitions':len(fresh),'native_rows':sum(r['observed_rows'] for r in fresh),
    'new_prefix_requests':0,'historical_metadata_bindings':metadata_count,'output':str(output)}))
