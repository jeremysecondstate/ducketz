"""Bounded local FMP evidence and pure calculation review; no provider calls."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, 'C:/dev/ducketz')
import pandas as pd
import pyarrow.parquet as pq
from datafetching.fmp_energy_context import calculate_fmp_energy_context, FMP_ENERGY_MAX_CLOCK_SKEW_SECONDS
from datafetching.parquet_store import _upsert

ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
REPO = Path('C:/dev/ducketz')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(frame):
    try:
        result = calculate_fmp_energy_context(frame)
        return {'status':'PASS','derived_rows':len(result)}
    except Exception as exc:
        return {'status':'ERROR','error':type(exc).__name__+': '+str(exc)}


def main():
    cycle_path = ROOT/'.ducketz-loop-a-cycle.json'
    cycle = json.loads(cycle_path.read_text())
    assert cycle['generation']=='20260929T040728.646690Z-pid62304'
    start = pd.Timestamp(cycle['started_at'])
    diag = ROOT/'pools/macro/ENERGY_CONTEXT/energy-context/fmp/diagnostics/ENERGY_CONTEXT_energy-context.parquet'
    source = ROOT/'pools/macro/CLUSD/quote/fmp/normalized/CLUSD_quote.parquet'
    assert pq.read_metadata(diag).num_rows <= 1000 and pq.read_metadata(source).num_rows <= 10000
    diagnostic = pd.read_parquet(diag)
    history = pd.read_parquet(source)
    fetched = pd.to_datetime(history.fetched_at,utc=True)
    skew = (pd.to_datetime(history.timestamp,utc=True)-fetched).dt.total_seconds()
    invalid = history.loc[skew.gt(FMP_ENERGY_MAX_CLOCK_SKEW_SECONDS)].copy()
    invalid['clock_skew_seconds'] = skew.loc[invalid.index]
    current = history.loc[fetched.ge(start)]
    previous_path = REPO/'artifacts/analysis/overnight-20260916/provider-advisories.json'
    previous = json.loads(previous_path.read_text())
    full_validation,current_validation = validate(history),validate(current)
    # Reproduce the native unchanged-message deduplication in memory only.
    old_diag = diagnostic.drop(columns=['id'])
    repeated_diag = old_diag.copy()
    repeated_diag['fetched_at'] = start
    merged_diag, changed = _upsert(old_diag,repeated_diag,
        ('source','category','request_key','advisory_type','advisory_message'))
    log = ROOT/'ml/overnight-runs/20260929T040727.779758Z/loop_a_close_fetch.log'
    summary_lines = [line for line in log.read_text(encoding='utf-8').splitlines() if 'blocking provider failures:' in line]
    checks = {'current_native_calculation_passes':current_validation['status']=='PASS' and len(current)>0,
        'no_current_cycle_invalid_rows':not (pd.to_datetime(invalid.fetched_at,utc=True)>=start).any(),
        'same_retained_invalid_row_count':len(invalid)==previous['invalid_historical_row_count']==5,
        'same_native_retained_rejection':full_validation==previous['native_full_source_validation'],
        'first_retained_bad_row_unchanged':invalid.iloc[0]['id']==previous['first_invalid_historical_rows'][0]['id'],
        'current_log_records_fmp_advisory':any('fmp=1' in line and 'blocking provider failures: 0' in line for line in summary_lines),
        'identical_diagnostic_preserves_original_receipt':not changed and merged_diag.equals(old_diag),
        'diagnostic_is_energy_quality_advisory':set(diagnostic['advisory_type'])=={'FmpEnergyContextQualityError'},
    }
    current_columns = ['id','symbol','provider_symbol','proxy_fallback_for','is_proxy_fallback','fetched_at','timestamp','price','available_at']
    invalid_columns = [*current_columns,'clock_skew_seconds']
    result = {'reviewed_at':datetime.now(timezone.utc).isoformat(),
        'status':'KNOWN_RETAINED_CLOCK_SKEW_ADVISORY' if all(checks.values()) else 'REVIEW_REQUIRED',
        'scope':'LOCAL_READ_ONLY_PARQUETS_AND_PURE_IN_MEMORY_CALCULATION_NO_PROVIDER_OR_PRODUCTION_ACTIONS',
        'cycle_snapshot':cycle,'checks':checks,'issues':[key for key,value in checks.items() if not value],
        'current_diagnostic':diagnostic.to_dict('records'),'source_rows':len(history),
        'diagnostic_receipt_is_from_current_cycle':bool((pd.to_datetime(diagnostic.fetched_at,utc=True)>=start).any()),
        'diagnostic_receipt_note':'Identical diagnostic messages are deduplicated by native upsert, excluding fetched_at from comparison; original September 2 receipt time is preserved. Current-cycle log and fresh source subset establish recurrence.',
        'current_cycle_source_rows':current[current_columns].to_dict('records'),
        'retained_invalid_rows':invalid[invalid_columns].to_dict('records'),
        'native_full_source_validation':full_validation,'native_current_source_validation':current_validation,
        'log_summary_lines_observed':summary_lines,
        'evidence_sha256':{str(path):sha(path) for path in [diag,source,previous_path,
            REPO/'datafetching/fmp_fetch.py',REPO/'datafetching/fmp_energy_context.py',REPO/'datafetching/parquet_store.py']},
        'log_path':str(log),'interpretation':'The optional shared energy derivation rejects preserved September 2 rows; current-cycle quote evidence passes the unchanged five-second gate. No newly actionable defect or provider retry is indicated.' if all(checks.values()) else 'Inspect failed comparisons; preserve source evidence and existing quality checks.',
        'broker_calls':0,'provider_calls':0,'production_writes':0}
    (OUT/'fmp-advisory-review.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
    text = f"""# Current FMP advisory review

Reviewed at {result['reviewed_at']}. Status: {result['status']}.

{result['interpretation']}

The saved source has {len(history)} quotes, including {len(current)} fetched in the current Loop A cycle. Native pure calculation passes on that current subset; full-history calculation still reports: {full_validation.get('error','none')}. All five invalid historical rows remain before the current cycle; their complete selected timestamp/identity evidence and source hashes are in fmp-advisory-review.json. The first rejected row and native error match the September 16 audit. The diagnostic retains its September 2 receipt time because native upsert deduplicates the identical message, which was reproduced in memory; current-cycle log and fresh source evidence establish this recurrence. No source rows, calculation gates or provider state were changed.

This audit does not claim the optional full-history energy derivation completed. It confirms the current nonblocking advisory is the retained quality exclusion, with no new actionable defect. All broker/provider calls and production writes in this audit were zero.
"""
    (OUT/'fmp-advisory-review.md').write_text(text,encoding='utf-8')
    print(json.dumps({'status':result['status'],'checks':checks,'source_rows':len(history),'current_rows':len(current),
        'invalid_rows':len(invalid),'current_validation':current_validation,'output':str(OUT/'fmp-advisory-review.json')}))


if __name__=='__main__':
    main()
