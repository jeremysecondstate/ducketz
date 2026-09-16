"""Read local CME metadata only; no provider or runtime operations."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
cycle = json.loads((ROOT / '.ducketz-loop-a-cycle.json').read_text())
assert cycle['generation'] == '20260916T040758.032086Z-pid29216'
start = pd.Timestamp(cycle['started_at'])
at = pd.Timestamp.now(tz='UTC')
diag = ROOT / 'pools/cme/CME_CONTEXT/cross-asset-context/databento/diagnostics/CME_CONTEXT_cross-asset-context.parquet'
data = pd.read_parquet(diag)
current = data.loc[pd.to_datetime(data.fetched_at, utc=True).ge(start)]
previous = json.loads(Path('C:/dev/ducketz/artifacts/analysis/overnight-20260915/loop-a-provider-advisory-audit.json').read_text())
log_path = ROOT / 'ml/overnight-runs/20260916T040757.217937Z/loop_a_close_fetch.log'
log = log_path.read_bytes()
lines = log.decode('utf-8').splitlines()
result = {
    'audited_at': at.isoformat(), 'cycle_snapshot': cycle,
    'diagnostic_path': str(diag), 'diagnostic_sha256': hashlib.sha256(diag.read_bytes()).hexdigest(),
    'current_diagnostic_rows': current.to_dict('records'),
    'prior_diagnostic_rows': [r for d in previous['diagnostics'] if d['path'] == str(diag) for r in d['current_cycle_rows']],
    'log_path': str(log_path), 'log_bytes_observed': len(log),
    'current_cme_log_lines': [line for line in lines if 'CME' in line or 'cross-asset-context' in line],
    'current_flat_sources': [], 'partition_inventory': {}, 'current_error_metadata': [],
    'classification': 'REPEATED_OPTIONAL_DERIVATION_REJECTION_FROM_OLD_PARTITION_SOURCE_SELECTION',
    'recommendation': 'Preserve the advisory and source gates. No new fetch or repair is justified solely by this repeated evidence.',
    'limits': 'Local bounded diagnostic and metadata audit, not exhaustive book/source qualification. Loop A is still in progress at observation.',
}
fields = ['timestamp', 'fetched_at', 'provider_symbol', 'databento_symbol', 'symbol', 'request_limit_saturated']
for path in sorted((ROOT / 'pools/cme').glob('CME_*/*/databento/normalized/*.parquet')):
    if path.stem.endswith('_status'):
        continue
    columns = pq.read_schema(path).names
    d = pd.read_parquet(path, columns=[c for c in fields if c in columns])
    latest = d.loc[pd.to_datetime(d.fetched_at, utc=True).ge(start)]
    result['current_flat_sources'].append({
        'path': str(path), 'current_rows': len(latest),
        'latest_market_time': d.timestamp.max(), 'latest_receipt_time': d.fetched_at.max(),
        'current_limit_saturated_rows': int(latest.request_limit_saturated.fillna(False).sum()) if 'request_limit_saturated' in latest else None,
    })
for schema in ['ohlcv-1m', 'bbo-1m', 'mbp-10']:
    paths = sorted((ROOT / 'pools/cme/events/databento/context' / schema / 'normalized').glob('**/*.parquet'))
    old = previous['cme_advisory_diagnosis']['partition_path_inventory_only'][schema]
    result['partition_inventory'][schema] = {'files': len(paths), 'last_path': str(paths[-1]) if paths else None, 'previous': old,
        'unchanged_inventory': len(paths) == old['files'] and (str(paths[-1]) if paths else None) == old['last_path']}
for path in sorted((ROOT / 'pools/cme').glob('CME_*/errors/*/*/*.parquet')):
    d = pd.read_parquet(path)
    if 'fetched_at' in d:
        recent = d.loc[pd.to_datetime(d.fetched_at, utc=True).ge(start)]
        if len(recent):
            result['current_error_metadata'].append({'path': str(path), 'rows': recent.to_dict('records')})
target = OUT / 'cme-advisory-audit.json'
target.write_text(json.dumps(result, indent=2, default=str) + '\n', encoding='utf-8')
print(json.dumps(result, indent=2, default=str))
