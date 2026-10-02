"""Offline source-bound Gameplan CME trace; no production writes/model loads."""
from pathlib import Path
from datetime import date, datetime, timezone
import hashlib
import json
import subprocess
import sys
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path('C:/dev/ducketz')
DATA = Path('C:/DATASTORE')
OUT = ROOT / 'artifacts/analysis/overnight-20261001/provider-warning'
RUN = DATA / 'ml/nightly-gameplan-runs/20261002T063014.996205Z'
LOOP_B = DATA / 'ml/runs/20261002T055557.616329Z'
NATIVE = '20261002T040848.225355Z'
START = datetime.now(timezone.utc)
ACTION = date(2026, 10, 2)
SOURCE = date(2026, 10, 1)

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def norm(value):
    return str(value).replace('\\', '/').casefold()

def checked(path, spec):
    actual = sha(path)
    return {'path': str(path), 'expected_sha256': spec['checksum_sha256'], 'actual_sha256': actual,
            'size': path.stat().st_size, 'matches': actual == spec['checksum_sha256'] and path.stat().st_size == spec['size']}

baseline_path = ROOT / 'artifacts/analysis/overnight-20261001/verification/audit-environment-baseline.json'
baseline = read(baseline_path)
baseline_mismatch = [p for p, expected in baseline['files'].items() if not (ROOT/p).is_file() or sha(ROOT/p) != expected]
if baseline_mismatch:
    raise RuntimeError(f'Baseline source drift: {baseline_mismatch}')
head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
sys.path.insert(0, str(ROOT))
from ml.gameplan_source_selection import select_prior_session_sources

manifest = read(RUN / 'manifest.json')
receipt = read(RUN / 'receipt.json')
reports = read(RUN / 'model-reports.json')
gameplan = read(RUN / 'gameplan.json')
archive = read(RUN / 'archive-history.json')
pointer_path = DATA / 'ml/nightly-gameplan-latest/run.json'
pointer = read(pointer_path)
mh, rh = sha(RUN/'manifest.json'), sha(RUN/'receipt.json')
outputs = [checked(RUN/name, spec) for name, spec in manifest['output_files'].items()]
output_map = {norm(x['path']): x for x in outputs}
bindings = [checked(DATA/x['path'], x) for x in manifest['input_files'] if norm(x['path']).startswith(norm(LOOP_B.relative_to(DATA)) + '/')]
upstream_path = OUT / 'loop-b-model-trace-20261002T062803Z.json'
upstream = read(upstream_path)
forecasts = pd.read_parquet(RUN/'forecasts.parquet')
groups = []
for group, report in sorted(reports.items()):
    cohort_path = RUN/f'training-cohort-{group}.parquet'
    columns = pq.ParquetFile(cohort_path).schema.names
    cme_columns = [x for x in columns if x.startswith('cme__')]
    cohort = pd.read_parquet(cohort_path, columns=['decision_timestamp']+cme_columns)
    admitted = report['features']['admitted']
    admitted_cme = [x for x in admitted if x.startswith('cme__')]
    model = report['model_file']
    model_check = checked(RUN/model['path'], model)
    rows = forecasts.loc[forecasts['model_group'] == group]
    nonnull = {x: int(cohort[x].notna().sum()) for x in cme_columns}
    group_checks = {
        'cohort_bound_to_manifest': output_map[norm(cohort_path)]['matches'],
        'model_bound_to_report': model_check['matches'],
        'model_report_binding_agrees_with_manifest': model['checksum_sha256'] == output_map[norm(RUN/model['path'])]['expected_sha256'],
        'no_cme_feature_admitted': not admitted_cme,
        'all_cohort_cme_values_missing': bool(cme_columns) and not any(nonnull.values()),
        'all_forecasts_use_reported_model': bool(len(rows)) and set(rows['model_artifact']) == {model['path']},
    }
    groups.append({'group': group, 'checks': group_checks, 'promotion_gate': report['promotion_gate'],
                   'selected_family': report['selected_family'], 'admitted_feature_count': len(admitted),
                   'admitted_cme_features': admitted_cme, 'model_file': model_check,
                   'cohort': {'path': str(cohort_path), 'sha256': sha(cohort_path), 'rows': len(cohort),
                              'candidate_cme_features': cme_columns, 'non_null_by_column': nonnull,
                              'first_decision': str(cohort['decision_timestamp'].min()),
                              'last_decision': str(cohort['decision_timestamp'].max())},
                   'forecast_rows': len(rows), 'forecast_status_counts': rows['model_status'].value_counts().to_dict(),
                   'current_cme_use': 'NO_CME_FEATURE_ADMITTED; no CME values in saved cohort'})

schema_columns = pq.ParquetFile(LOOP_B/'samples.parquet').schema.names
cme_columns = [x for x in schema_columns if x.startswith('cme__')]
required = ['symbol','horizon','timeframe','exchange_calendar','exchange_session','bar_timestamp',
            'bar_end_timestamp','information_available_at','decision_timestamp']
optional_identity = [x for x in ['venue','currency','provider','assumed_round_trip_cost'] if x in schema_columns]
sample = pd.read_parquet(LOOP_B/'samples.parquet', columns=required+optional_identity+cme_columns)
sample = sample.loc[pd.to_datetime(sample['exchange_session'], utc=True).dt.date == SOURCE]
selected = select_prior_session_sources(sample, symbols=gameplan['symbols'], available_at=manifest['run_timestamp'], feature_columns=cme_columns)
selected = selected.loc[selected['action_date'] == ACTION]
current = []
for _, row in selected.iterrows():
    f = forecasts.loc[forecasts['symbol'] == row['symbol']]
    causal = bool((pd.Timestamp(row['information_available_at']) <= pd.to_datetime(f['decision_timestamp'],utc=True)).all())
    current.append({'symbol': row['symbol'], 'action_date': str(row['action_date']), 'source_session': str(row['source_session']),
                    'operational_decision_timestamp': str(row['decision_timestamp']),
                    'operational_information_available_at': str(row['information_available_at']),
                    'source_bar_timestamp': str(row['source_bar_timestamp']), 'source_bar_end_timestamp': str(row['source_bar_end_timestamp']),
                    'cme_values': {c: None if pd.isna(row[c]) else float(row[c]) for c in cme_columns},
                    'forecast_count': len(f), 'forecast_source_sessions': sorted(set(f['source_session'].astype(str))),
                    'forecast_decision_timestamps': sorted(set(f['decision_timestamp'].astype(str))),
                    'optional_input_available_by_all_forecast_decisions': causal})

checks = {
    'receipt_manifest_binding': receipt['manifest_checksum_sha256'] == mh,
    'action_date_october2': receipt['action_date'] == manifest['configuration']['action_date'] == gameplan['action_date'] == str(ACTION),
    'source_loop_b_binding': norm(DATA/receipt['source_loop_b_run']) == norm(LOOP_B) == norm(DATA/manifest['configuration']['source_loop_b_run']),
    'all_saved_outputs_match': all(x['matches'] for x in outputs),
    'four_loop_b_inputs_match': len(bindings) == 4 and all(x['matches'] for x in bindings),
    'prior_loop_b_trace_pass_and_exact_sources': upstream['status'] == 'LOOP_B_CME_TRACE_VERIFIED'
        and upstream['publication_manifest_sha256'] == sha(LOOP_B/'manifest.json')
        and upstream['publication_receipt_sha256'] == sha(LOOP_B/'publication.json'),
    'all_four_groups_present_and_verified': set(reports) == {'1h','4h','1d','1w'} and all(all(x['checks'].values()) for x in groups),
    'all_eleven_current_operational_sources_present': len(current) == len(gameplan['symbols']) == 11 and {x['symbol'] for x in current} == set(gameplan['symbols']),
    'all_current_source_cme_values_missing': all(all(v is None for v in x['cme_values'].values()) for x in current),
    'all_current_optional_inputs_causal': all(x['optional_input_available_by_all_forecast_decisions'] for x in current),
    'pointer_selects_receipt': pointer['current']['receipt_checksum_sha256'] == rh and pointer['current']['manifest_checksum_sha256'] == mh and norm(DATA/pointer['current']['run_path']) == norm(RUN),
    'forecast_count_264': len(forecasts) == 264 and all(x['forecast_count'] == 24 for x in current),
    'source_baseline_unchanged': not baseline_mismatch,
}
result = {
    'schema_version': 1, 'status': 'GAMEPLAN_CME_TRACE_VERIFIED' if all(checks.values()) else 'FAILED',
    'started_at_utc': START.isoformat(), 'completed_at_utc': datetime.now(timezone.utc).isoformat(),
    'native_run_id': NATIVE, 'run_path': str(RUN), 'manifest_sha256': mh, 'receipt_sha256': rh,
    'model_reports_sha256': sha(RUN/'model-reports.json'), 'checks': checks,
    'errors': [k for k,v in checks.items() if not v], 'verified_output_files': outputs,
    'source_loop_b_input_bindings': bindings, 'groups': groups,
    'upstream_loop_b_trace': {'path': str(upstream_path), 'sha256': sha(upstream_path), 'status': upstream['status']},
    'current_operational_source_evidence': {
        'source_file': str(LOOP_B/'samples.parquet'), 'source_sha256': sha(LOOP_B/'samples.parquet'),
        'method': 'Pure select_prior_session_sources on immutable October1 samples, pinned publication run_timestamp, required clocks/identity and cme__ columns. No archive load, fitting, publishing or provider calls.',
        'selector_sha256': sha(ROOT/'ml/gameplan_source_selection.py'),
        'optional_attachment_sha256': sha(ROOT/'ml/gameplan_archive_integration.py'), 'rows': current},
    'archive_feature_contract': {'path': str(RUN/'archive-history.json'), 'sha256': sha(RUN/'archive-history.json'),
        'source_selection_contract': archive['source_selection_contract'],
        'optional_operational_cme_features': [c for c in archive['optional_operational_features'] if c.startswith('cme__')],
        'optional_feature_policy': archive['optional_feature_policy']},
    'source_checkout_evidence': {'observed_head': head, 'baseline_path': str(baseline_path),
        'baseline_sha256': sha(baseline_path), 'verified_file_count': len(baseline['files']), 'mismatches': baseline_mismatch,
        'qualification': 'Concurrent HEAD advancement is separate from exact verified stock source bytes and runtime ownership.'},
    'forecast_storage_limitation': 'forecasts.parquet saves a separate fixed enrichment subset. Absence of cme__ from that subset is not used as proof; exclusion is established by admitted features, cohorts and pinned source-row reconstruction.',
    'conclusion': 'All four Gameplan model groups admit zero CME features. Their exact saved cohorts contain no CME values; the eleven pinned current source rows also contain no CME values. October1/2 narrowed or saturated CME captures therefore did not enter these saved model feature inputs. This is distinct from prior Loop B historical CME feature use.',
    'quality_metadata_limitation': 'No degraded-data warning occurred in the completed Loop A. No new metadata calls were made; no prior quality flag is declared resolved.',
    'provider_calls': 0, 'production_writes': 0, 'model_fits': 0, 'model_deserialization': 0, 'orders': 0,
}
out = OUT/('gameplan-model-trace-'+START.strftime('%Y%m%dT%H%M%SZ')+'.json')
out.write_text(json.dumps(result,indent=2,default=str)+'\n', encoding='utf-8')
print(json.dumps({'path': str(out), 'sha256': sha(out), 'status': result['status'], 'checks': checks,
                  'groups': [{'group':x['group'],'cohort_rows':x['cohort']['rows'],'admitted_cme':x['admitted_cme_features'],'forecast_status_counts':x['forecast_status_counts']} for x in groups]},indent=2))
raise SystemExit(0 if all(checks.values()) else 1)
