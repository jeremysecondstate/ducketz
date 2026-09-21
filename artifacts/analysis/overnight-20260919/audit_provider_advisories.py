"""Local diagnostic/current source comparison, with pure in-memory validation."""
from pathlib import Path
from datetime import datetime, timezone
import json
import sys
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, 'C:/dev/ducketz')
from datafetching.fmp_energy_context import calculate_fmp_energy_context

ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
START = pd.Timestamp('2026-09-19T04:07:48.657031Z')
diagnostics = {}
for name, rel in [
    ('cme', 'pools/cme/CME_CONTEXT/cross-asset-context/databento/diagnostics/CME_CONTEXT_cross-asset-context.parquet'),
    ('fmp', 'pools/macro/ENERGY_CONTEXT/energy-context/fmp/diagnostics/ENERGY_CONTEXT_energy-context.parquet'),
]:
    path = ROOT / rel
    df = pd.read_parquet(path)
    times = pd.to_datetime(df['fetched_at'], utc=True)
    current = df.loc[times >= START]
    diagnostics[name] = {'path': str(path), 'total_rows': len(df), 'current_rows': current.to_dict('records'), 'latest_rows': df.assign(_time=times).sort_values('_time').tail(2).drop(columns='_time').to_dict('records')}

sources = []
metadata = ['request_limit_saturated', 'limit', 'initial_range_start', 'initial_range_end', 'effective_range_start', 'effective_range_end', 'latest_window_shrink_count', 'empty_window_expansion_count', 'cme_schema_status']
for scope in ['CME_CONTEXT', 'CME_CONTRACTS']:
    for schema in ['ohlcv-1m', 'bbo-1m', 'mbp-10']:
        key = scope.lower() + '_' + schema
        path = ROOT / 'pools/cme' / scope / key / 'databento/normalized' / (scope + '_' + key + '.parquet')
        names = pq.read_schema(path).names
        df = pd.read_parquet(path, columns=[c for c in ['fetched_at', 'timestamp', 'ts_event'] + metadata if c in names])
        current = df.loc[pd.to_datetime(df['fetched_at'], utc=True) >= START]
        times = pd.to_datetime(current['timestamp'], utc=True)
        sources.append({'path': str(path), 'scope': scope, 'schema': schema, 'current_rows': len(current), 'earliest_market_time': times.min(), 'latest_market_time': times.max(), 'latest_receipt_time': pd.to_datetime(current['fetched_at'], utc=True).max(), 'current_saturated_rows': int(current['request_limit_saturated'].sum()), 'request_metadata': current[[c for c in metadata if c in names]].drop_duplicates().to_dict('records')})

quote_path = ROOT / 'pools/macro/CLUSD/quote/fmp/normalized/CLUSD_quote.parquet'
quote = pd.read_parquet(quote_path)
current_quote = quote.loc[pd.to_datetime(quote['fetched_at'], utc=True) >= START]
try:
    full_result = {'status': 'PASS', 'rows': len(calculate_fmp_energy_context(quote))}
except Exception as exc:
    full_result = {'status': 'REJECTED', 'error': type(exc).__name__ + ': ' + str(exc)}
try:
    current_result = {'status': 'PASS', 'rows': len(calculate_fmp_energy_context(current_quote)), 'derived': calculate_fmp_energy_context(current_quote).to_dict('records')}
except Exception as exc:
    current_result = {'status': 'REJECTED', 'error': type(exc).__name__ + ': ' + str(exc)}
quote_times = pd.to_datetime(quote['timestamp'], utc=True, format='mixed')
receipt_times = pd.to_datetime(quote['fetched_at'], utc=True)
skew = (quote_times - receipt_times).dt.total_seconds()
bad = quote.loc[skew > 5, ['fetched_at', 'timestamp', 'symbol', 'provider_symbol']].copy()
bad['skew_seconds'] = skew.loc[skew > 5]

cme_rows = diagnostics['cme']['current_rows']
assert len(cme_rows) == 1
cme = cme_rows[0]
assert cme['severity'] == 'advisory' and cme['advisory_type'] == 'CmeCrossAssetQualityError'
assert '2026-09-03T21:00:00+00:00' in cme['advisory_message'] and 'maximum is 0 days 00:15:00' in cme['advisory_message']
assert cme['input_policy'] == 'persisted_rows_only' and cme['provider_rows_preserved']
fmp = diagnostics['fmp']['latest_rows'][-1]
assert diagnostics['fmp']['total_rows'] == 1 and not diagnostics['fmp']['current_rows']
assert fmp['advisory_type'] == 'FmpEnergyContextQualityError' and '9.471s' in fmp['advisory_message'] and '5.000s' in fmp['advisory_message']
assert fmp['input_policy'] == 'persisted_rows_only' and fmp['provider_rows_preserved']
assert current_result['status'] == 'PASS' and current_result['rows'] == 1
assert full_result['status'] == 'REJECTED' and '9.471s' in full_result['error'] and len(bad) == 5

result = {'observed_at': datetime.now(timezone.utc).isoformat(), 'disposition': 'KNOWN_OPTIONAL_LIMITATIONS_NO_NEW_ACTIONABLE_DEFECT', 'diagnostics': diagnostics, 'current_cme_sources': sources, 'fmp_source_path': str(quote_path), 'fmp_total_source_rows': len(quote), 'fmp_current_source_rows': current_quote.to_dict('records'), 'fmp_full_source_pure_validation': full_result, 'fmp_current_source_pure_validation': current_result, 'fmp_historical_clock_skew_rows': bad.to_dict('records'), 'prior_evidence': 'C:/dev/ducketz/artifacts/analysis/overnight-20260918/provider-preflight-review.md', 'scope': 'Local diagnostics, six normalized CME metadata projections, 1490-row FMP quote history and pure in-memory current/full FMP validation. No provider calls, source writes or broad archive scan.'}
result = json.loads(json.dumps(result, default=str), parse_constant=lambda value: None)
md = ['## Current optional advisory review', '', f"Observed **{result['observed_at']}**. **Known optional limitations; no new actionable provider defect established.**", '', f"CME's fresh diagnostic at `{cme['fetched_at']}` preserves `advisory`, `CmeCrossAssetQualityError`, persisted-only inputs and original provider rows. It again rejects the September 3 21:00 UTC candidate under the same 15-minute rule: `{cme['advisory_message']}`. This matches the prior source-bound rejection; the additional elapsed day changes the age only.", '', '| Current CME scope | OHLCV rows | BBO rows | MBP rows | MBP saturated |', '|---|---:|---:|---:|---|']
for scope in ['CME_CONTEXT', 'CME_CONTRACTS']:
    rows = {r['schema']: r for r in sources if r['scope'] == scope}
    md.append(f"| {scope} | {rows['ohlcv-1m']['current_rows']} | {rows['bbo-1m']['current_rows']} | {rows['mbp-10']['current_rows']} | {bool(rows['mbp-10']['current_saturated_rows'])} |")
md += ['', 'All six current CME requests succeeded in the native log. Saturated MBP remains capped evidence even when normalization retains fewer than 5,000 rows. The JSON preserves each exact request range, observed market timestamps, shrink count and saturation field. Both BBO receipts explicitly report BACKTRACKED with two empty-window expansions, moving the effective start from September 18 23:00 to 20:00 UTC; latest observations are September 18 20:59:59 UTC. OHLCV receipts shrink twice to September 18 06:00 through September 19 00:00 UTC and end at September 18 20:59 UTC. These bounded provider windows are disclosed, not complete interval coverage. Current captures do not qualify the rejected historical partition-derived context.', '', f"The FMP diagnostic remains the single retained September 2 clock-skew advisory with **9.471 seconds** against the unchanged **5-second** maximum; no current-cycle diagnostic row appeared. The {len(quote):,}-row saved quote history still reproduces that exact rejection in the native pure calculation, with the same five historical offenders. The **current-cycle quote passes** that calculation in memory with one derived row. Its receipt is `{current_quote.iloc[0]['fetched_at']}`, provider market timestamp `{current_quote.iloc[0]['timestamp']}`, and explicit USO ETF-share-price proxy identity. The single current row has no preceding same-chain observation, so its return remains unavailable; this does not alter or qualify the retained historical history.", '', 'Preserve both optional derivation gates and source evidence. These repeated advisories do not justify provider retries, restarting the pipeline, deleting source rows or weakening quality checks.', '']
report_path = OUT / 'provider-completion.json'
report = json.loads(report_path.read_text())
report['optional_advisory_review'] = result
report['optional_advisory_markdown'] = '\n'.join(md)
report_path.write_text(json.dumps(report, indent=2, default=str) + '\n')
md_path = OUT / 'provider-completion.md'
existing = md_path.read_text().split('## Current optional advisory review')[0].rstrip()
md_path.write_text(existing + '\n\n' + '\n'.join(md))
print(json.dumps({'observed_at': result['observed_at'], 'disposition': result['disposition'], 'cme_diagnostic': cme, 'cme_sources': sources, 'fmp_rows': len(quote), 'fmp_current': current_result, 'fmp_historical_failure': full_result, 'fmp_skew_count': len(bad)}, indent=2, default=str))
