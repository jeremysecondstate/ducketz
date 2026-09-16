"""Bounded local diagnostic comparison; no provider/runtime operations."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, 'C:/dev/ducketz')
import pandas as pd
from datafetching.fmp_energy_context import calculate_fmp_energy_context

ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
at = pd.Timestamp.now(tz='UTC')
cycle = json.loads((ROOT / '.ducketz-loop-a-cycle.json').read_text())
assert cycle['generation'] == '20260916T040758.032086Z-pid29216'
start = pd.Timestamp(cycle['started_at'])
previous = json.loads(Path('C:/dev/ducketz/artifacts/analysis/overnight-20260915/loop-a-provider-advisory-audit.json').read_text())
diag = ROOT / 'pools/macro/ENERGY_CONTEXT/energy-context/fmp/diagnostics/ENERGY_CONTEXT_energy-context.parquet'
source = ROOT / 'pools/macro/CLUSD/quote/fmp/normalized/CLUSD_quote.parquet'
d = pd.read_parquet(diag)
s = pd.read_parquet(source)
current = s.loc[pd.to_datetime(s.fetched_at, utc=True).ge(start)]

def validate(frame):
    try:
        result = calculate_fmp_energy_context(frame)
        return {'status': 'PASS', 'derived_rows': len(result)}
    except Exception as exc:
        return {'status': 'ERROR', 'error': f'{type(exc).__name__}: {exc}'}

skew = (pd.to_datetime(s.timestamp, utc=True) - pd.to_datetime(s.fetched_at, utc=True)).dt.total_seconds()
invalid = s.loc[skew.gt(5)].copy()
invalid['clock_skew_seconds'] = skew.loc[skew.gt(5)]
log_path = ROOT / 'ml/overnight-runs/20260916T040757.217937Z/loop_a_close_fetch.log'
lines = log_path.read_text(encoding='utf-8').splitlines()
result = {
    'audited_at': at.isoformat(), 'cycle_snapshot': cycle,
    'scope': 'Read-only local shared FMP energy diagnostic, retained quote source and pure in-memory native calculation; no provider calls or source mutations.',
    'diagnostic_path': str(diag), 'diagnostic_sha256': hashlib.sha256(diag.read_bytes()).hexdigest(),
    'diagnostic_rows': d.to_dict('records'),
    'current_cycle_diagnostic_rows': d.loc[pd.to_datetime(d.fetched_at, utc=True).ge(start)].to_dict('records'),
    'source_path': str(source), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
    'source_rows': len(s), 'current_cycle_source_rows': current.to_dict('records'),
    'native_full_source_validation': validate(s), 'native_current_source_validation': validate(current),
    'invalid_historical_row_count': len(invalid), 'first_invalid_historical_rows': invalid.head(3).to_dict('records'),
    'prior_day': previous['fmp_advisory_diagnosis'],
    'log_path': str(log_path),
    'provider_summary_lines_observed': [line for line in lines if 'blocking provider failures:' in line],
    'classification': 'REPEATED_OPTIONAL_DERIVATION_REJECTION_FROM_PRESERVED_HISTORICAL_CLOCK_SKEW',
    'recommendation': 'Retain advisory/source checks. No new repair or provider retry justified by this repeated historical rejection.',
    'cme_evidence': str(OUT / 'cme-advisory-audit.json'),
}
(OUT / 'provider-advisories.json').write_text(json.dumps(result, indent=2, default=str) + '\n', encoding='utf-8')
print(json.dumps({k: v for k, v in result.items() if k != 'prior_day'}, indent=2, default=str))
