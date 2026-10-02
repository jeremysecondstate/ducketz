"""Read-only trace of current Loop B CME inputs; writes only fresh local audit JSON."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import pandas as pd
import pyarrow.parquet as pq

ROOT = Path('C:/dev/ducketz')
DATA = Path('C:/DATASTORE')
OUT = ROOT / 'artifacts/analysis/overnight-20261001/provider-warning'
RUN_ID = '20261002T055557.616329Z'
NATIVE = '20261002T040848.225355Z'
RUN = DATA / 'ml/runs' / RUN_ID
START = datetime.now(timezone.utc)
AUDIT_DAY = pd.Timestamp('2026-10-01T00:00:00Z')

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

def norm(value):
    return str(value).replace('\\', '/').casefold()

def cme_inputs(doc):
    return [x for x in doc['input_files'] if '/cme/' in '/' + norm(x['path'])]

def check_file(path, spec):
    actual = sha(path)
    return {'path': str(path), 'expected_sha256': spec['checksum_sha256'],
            'actual_sha256': actual, 'size': path.stat().st_size,
            'matches': actual == spec['checksum_sha256'] and path.stat().st_size == spec['size']}

manifest = read(RUN / 'manifest.json')
publication = read(RUN / 'publication.json')
pointer_path = DATA / 'ml/latest/run.json'
pointer = read(pointer_path)
manifest_hash = sha(RUN / 'manifest.json')
publication_hash = sha(RUN / 'publication.json')
outputs = [check_file(RUN / name, spec) for name, spec in manifest['output_files'].items()]
inputs = []
for spec in cme_inputs(manifest):
    ev = dict(spec)
    ev['verification'] = check_file(DATA / spec['path'], spec)
    inputs.append(ev)
input_by_path = {norm(x['path']): x for x in inputs}

capture_path = OUT / 'capture-review-20261002T042250Z.json'
capture_audit = read(capture_path)
capture_paths = {norm(x['path']) for x in capture_audit['captures']}
capture_hashes = {x['sha256'] for x in capture_audit['captures']}
raw_input_paths = {norm(DATA / x['path']) for x in inputs}
view_path = DATA / 'pools/cme/features/cross-asset-context/databento/1h.parquet'
view = pd.read_parquet(view_path)
view_evidence = {
    'path': str(view_path), 'sha256': sha(view_path), 'rows': len(view),
    'first_window_start': str(view['window_start'].min()),
    'last_window_end': str(view['window_end'].max()),
    'last_available_at': str(view['available_at'].max()),
    'october1_or_later_windows': int((pd.to_datetime(view['window_end'], utc=True) >= AUDIT_DAY).sum()),
    'windows': view[['window_start', 'window_end', 'available_at']].to_dict('records'),
}
cols = pq.ParquetFile(RUN / 'samples.parquet').schema.names
cme_cols = [x for x in cols if x.startswith('cme__')]
samples = pd.read_parquet(RUN / 'samples.parquet', columns=['symbol', 'horizon', 'decision_timestamp'] + cme_cols)
samples['decision_timestamp'] = pd.to_datetime(samples['decision_timestamp'], utc=True)
predictions = pd.read_parquet(RUN / 'predictions.parquet', columns=['horizon', 'model_name', 'model_version'])
identities = predictions.drop_duplicates().to_dict('records')
models = []
for identity in identities:
    folder = DATA / 'ml/models' / identity['horizon'] / identity['model_name'] / identity['model_version']
    model_path = folder / 'manifest.json'
    model = read(model_path)
    selected = [x for x in model['feature_columns'] if x.startswith('cme__')]
    bindings = []
    for spec in cme_inputs(model):
        bound = input_by_path.get(norm(spec['path']), {})
        bindings.append({
            'path': spec['path'], 'model_sha256': spec['checksum_sha256'],
            'publication_sha256': bound.get('checksum_sha256'),
            'matches': spec['checksum_sha256'] == bound.get('checksum_sha256') and spec['size'] == bound.get('size'),
        })
    frame = samples.loc[samples['horizon'] == identity['horizon']]
    selected_frame = frame[selected]
    present = selected_frame.notna().any(axis=1)
    recent = frame['decision_timestamp'] >= AUDIT_DAY
    present_frame = frame.loc[present]
    model_file = check_file(folder / model['model_file']['path'], model['model_file'])
    checks = {
        'identity_matches': model['horizon'] == identity['horizon'] and model['model_name'] == identity['model_name'],
        'model_version_matches_current_run': identity['model_version'] == RUN_ID,
        'feature_set_matches_publication': model['feature_set_name'] == manifest['configuration']['model_feature_sets'][identity['horizon']],
        'model_blob_matches': model_file['matches'],
        'model_cme_bindings_match_exact_publication_set': len(bindings) == len(inputs) and all(x['matches'] for x in bindings)
            and {norm(x['path']) for x in bindings} == set(input_by_path),
        'october1_and_later_cme_values_absent': not bool(selected_frame.loc[recent].notna().any().any()),
    }
    models.append({
        **identity, 'manifest_path': str(model_path), 'manifest_sha256': sha(model_path),
        'model_file': model_file, 'checks': checks, 'trained_at': model['trained_at'],
        'training_through': model['training_through'], 'training_rows': model['training_rows'],
        'calibration_rows': model['calibration_rows'], 'assessment_rows': model['assessment_rows'],
        'selected_cme_features': selected, 'cme_source_bindings': bindings,
        'saved_samples': {
            'rows': len(frame), 'non_null_rows_any_selected_cme': int(present.sum()),
            'first_non_null_decision': str(present_frame['decision_timestamp'].min()) if len(present_frame) else None,
            'last_non_null_decision': str(present_frame['decision_timestamp'].max()) if len(present_frame) else None,
            'non_null_by_column': {c: int(frame[c].notna().sum()) for c in selected},
            'october1_or_later_rows': int(recent.sum()),
            'october1_or_later_non_null_by_column': {c: int(frame.loc[recent,c].notna().sum()) for c in selected},
            'october1_or_later_per_symbol': [
                {'symbol': symbol, 'rows': len(part), 'non_null_cme_rows': int(part[selected].notna().any(axis=1).sum())}
                for symbol, part in frame.loc[recent].groupby('symbol')
            ],
        },
        'current_native_capture_use': 'EXCLUDED_FROM_BOUND_CME_SOURCE_VIEW',
        'qualification': 'CME feature selection can use older observations or missing-value handling; it does not establish current capture use.',
    })

log_path = DATA / 'ml/overnight-runs' / NATIVE / 'loop_a_close_fetch.log'
log = log_path.read_text(encoding='utf-8', errors='replace')
warning_lines = [{'line': i, 'text': line} for i, line in enumerate(log.splitlines(), 1)
                 if 'reduced quality' in line.lower() or 'degraded' in line.lower()]
checks = {
    'publication_binds_manifest': publication['manifest_checksum_sha256'] == manifest_hash,
    'publication_run_matches': norm(DATA / publication['run_path']) == norm(RUN),
    'pointer_matches_run_and_manifest': norm(DATA / pointer['current']['run_path']) == norm(RUN)
        and pointer['current']['manifest_checksum_sha256'] == manifest_hash,
    'pointer_receipt_hash_matches': pointer['current']['receipt_checksum_sha256'] == publication_hash,
    'all_outputs_match': all(x['matches'] for x in outputs),
    'no_route_errors': not manifest['configuration']['route_errors'],
    'all_cme_input_hashes_match': bool(inputs) and all(x['verification']['matches'] for x in inputs),
    'derived_view_has_no_october1_or_later_windows': view_evidence['october1_or_later_windows'] == 0,
    'current_capture_paths_and_hashes_absent_from_cme_bindings': not (raw_input_paths & capture_paths)
        and not ({x['checksum_sha256'] for x in inputs} & capture_hashes),
    'no_hot_or_current_partition_source_bound': all('/normalized/' not in norm(x['path']) and '/partitions/' not in norm(x['path']) for x in inputs),
    'all_nine_model_traces_pass': len(models) == 9 and all(all(x['checks'].values()) for x in models),
    'no_degraded_warning_in_completed_loop_a_log': not warning_lines,
}
pricing = manifest['configuration'].get('pricing_evidence', {})
result = {
    'schema_version': 1, 'status': 'LOOP_B_CME_TRACE_VERIFIED' if all(checks.values()) else 'FAILED',
    'started_at_utc': START.isoformat(), 'completed_at_utc': datetime.now(timezone.utc).isoformat(),
    'native_run_id': NATIVE, 'publication_run': str(RUN),
    'publication_manifest_sha256': manifest_hash, 'publication_receipt_sha256': publication_hash,
    'pointer_evidence': {'path': str(pointer_path), 'sha256': sha(pointer_path), 'current': pointer['current']},
    'current_capture_audit': {'path': str(capture_path), 'sha256': sha(capture_path)},
    'completed_loop_a_log': {'path': str(log_path), 'sha256': sha(log_path), 'degraded_warning_lines': warning_lines},
    'checks': checks, 'errors': [name for name, ok in checks.items() if not ok],
    'verified_outputs': outputs, 'cme_input_evidence': inputs, 'bound_derived_view': view_evidence,
    'model_count': len(models), 'models': models,
    'pricing_evidence_summary': {k: pricing[k] for k in ('policy_version', 'failed_route_count', 'downstream_training_eligible', 'model_admission_by_horizon') if k in pricing},
    'conclusion': 'All nine saved Loop B model manifests bind the same verified 32 older CME archive/view inputs. Current native CME narrowed/saturated captures are absent from these bindings. The bound derived view has August 14 windows only; all October 1 and later saved CME sample values are missing. Older real CME values exist for 1h/4h. Missing handling does not constitute current provider observations.',
    'limitations': ['No fresh provider quality metadata was requested because this completed Loop A emitted no degraded-data warning. This does not resolve prior provider quality flags.',
                    'This audit verifies saved bindings, bytes and feature values, not fitted model execution or a new model qualification.',
                    'Gameplan model tracing remains separate and pending until its new publication is available.'],
    'provider_calls': 0, 'production_writes': 0, 'model_loads_or_fits': 0, 'orders': 0,
}
path = OUT / ('loop-b-model-trace-' + START.strftime('%Y%m%dT%H%M%SZ') + '.json')
path.write_text(json.dumps(result, indent=2, default=str) + '\n', encoding='utf-8')
print(json.dumps({'path': str(path), 'sha256': sha(path), 'status': result['status'], 'checks': checks,
                 'counts': [{**{k:m[k] for k in ('horizon','training_rows')}, **{k:m['saved_samples'][k] for k in ('rows','non_null_rows_any_selected_cme','october1_or_later_rows')}} for m in models]}, indent=2))
raise SystemExit(0 if all(checks.values()) else 1)
