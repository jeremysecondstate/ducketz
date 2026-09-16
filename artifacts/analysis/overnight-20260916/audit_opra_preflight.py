"""Inspect bounded current OPRA preflight JSON metadata only."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import json

root = Path('C:/DATASTORE/market-data/databento/opra/OPRA.PILLAR')
start = datetime.fromisoformat('2026-09-16T04:29:29+00:00')
records = []
for schema in ['ohlcv-1h', 'cbbo-1m', 'definition']:
    for path in (root / 'metadata/preflights' / schema).glob('*.OPT/*_to_2026-09-16/preflight.json'):
        payload = json.loads(path.read_text())
        generated = datetime.fromisoformat(payload['generated_at'])
        if generated >= start:
            records.append({'path': str(path), 'generated_at': payload['generated_at'],
                'scope': payload['scope'], 'estimated_cost_usd': payload['estimated_cost_usd'],
                'cost_estimates_complete': payload['cost_estimates_complete'],
                'estimated_download_size_bytes': payload['estimated_download_size_bytes'],
                'capacity_pass': payload['capacity_pass'], 'required_free_bytes': payload['required_free_bytes'],
                'available_free_bytes': payload['available_free_bytes'],
                'semantic_checksum_sha256': payload['semantic_checksum_sha256']})
records.sort(key=lambda row: row['generated_at'])
log_path = Path('C:/DATASTORE/ml/overnight-runs/20260916T040757.217937Z/loop_a_close_fetch.log')
log = log_path.read_text()
summary_lines = [line for line in log.splitlines() if 'OPRA history guarded preflight:' in line]
result = {'observed_at': datetime.now(timezone.utc).isoformat(),
    'worker_identity': '48476 -> 48416; datafetching.options_history incremental source session 2026-09-15',
    'inferred_phase': ('Guarded preflight selection complete; subsequent download verification belongs to native worker' if summary_lines else 'Sequential guarded storage preflights before download selection summary'),
    'observed_preflights': len(records), 'expected_scopes': 33,
    'observed_by_schema': dict(Counter(row['scope']['schemas'][0] for row in records)),
    'all_observed_costs_zero': all(row['estimated_cost_usd'] == 0 for row in records),
    'all_observed_costs_complete': all(row['cost_estimates_complete'] for row in records),
    'all_observed_capacity_pass': all(row['capacity_pass'] for row in records),
    'observed_aggregate_estimated_bytes': sum(row['estimated_download_size_bytes'] for row in records),
    'observed_aggregate_estimated_cost_usd': sum(row['estimated_cost_usd'] for row in records),
    'log_path': str(log_path), 'selection_summary_lines': summary_lines,
    'current_preflights': records,
    'source_explanation': 'options_runtime.py collects and publishes each scope preflight sequentially, then prints a single guarded-preflight summary after all scope plans. Each preflight obtains billable size, record count and estimated cost in sequence (databento_opra_history.py storage_preflight). Continuing new generated_at receipts show local forward progress during quiet stdout.',
    'limits': 'No provider calls, archive scans, process changes, claims or publication verification. Native summary confirms scope selection only, not completion of downloads or cursor coverage.'}
out = Path(__file__).resolve().parent / 'opra-preflight-progress.json'
out.write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({k:v for k,v in result.items() if k!='current_preflights'}, indent=2))
print(json.dumps({'first': records[0] if records else None, 'latest': records[-1] if records else None}, indent=2))
