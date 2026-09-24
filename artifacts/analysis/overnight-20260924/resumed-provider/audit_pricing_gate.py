"""Bounded offline Pricing authority/gate audit; evidence-only writes."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re
import sys

ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
RUN = ROOT / 'ml/overnight-runs/20260924T044352.904210Z'
stage_precheck = json.loads((RUN / 'stage-report.json').read_text())
loop_b_stage = next((s for s in stage_precheck.get('stages', []) if s.get('stage') == 'loop_b_directional_generation'), None)
if not loop_b_stage or loop_b_stage.get('status') != 'COMPLETE' or loop_b_stage.get('exit_code') != 0:
    raise RuntimeError('Wait for the exact native Loop B completion before this once-only source audit')
run_lines = [line.removeprefix('Run: ').strip() for line in (RUN / 'loop_b_directional_generation.log').read_text(errors='replace').splitlines() if line.startswith('Run: ')]
if len(run_lines) != 1:
    raise RuntimeError('Expected one exact native Loop B publication path')
LOOP_B = Path(run_lines[0]).resolve()
if not LOOP_B.is_relative_to((ROOT / 'ml/runs').resolve()):
    raise RuntimeError('Native Loop B publication path escapes authority root')
PRIOR = Path('C:/dev/ducketz/artifacts/analysis/overnight-20260923/pricing-gate-review.json')
issues = []
compact_bytes = {}
def load(path):
    return json.loads(compact_bytes[path] if path in compact_bytes else path.read_bytes())
def check(value, message):
    if not value:
        issues.append(message)
    return bool(value)
def digest(path):
    return hashlib.sha256(compact_bytes[path] if path in compact_bytes else path.read_bytes()).hexdigest()
def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

prior = load(PRIOR)
pointer_path = ROOT / 'ml/option-pricing-latest/run.json'
pointer = load(pointer_path)
check(pointer == prior['pricing_pointer'], 'Pricing pointer changed; source normalization review needed')
files = []
for saved in prior['compact_source_comparison']:
    path = Path(saved['path'])
    if not path.resolve().is_relative_to((ROOT / 'ml/option-pricing-runs').resolve()):
        raise ValueError('Prior compact source path is outside exact Pricing root')
    data = path.read_bytes()
    compact_bytes[path] = data
    actual = hashlib.sha256(data).hexdigest()
    same = actual == saved['sha256'] and len(data) == saved['bytes']
    check(same, 'Changed compact source: ' + str(path))
    files.append({'path': str(path), 'sha256': actual, 'bytes': len(data), 'matches_prior': same})
check(len(files) == 51 and sum(f['bytes'] for f in files) <= 12_000_000,
      'Compact source audit exceeded established 51-file/12MB bound')
chain = []
record = pointer['current']
for index in range(18):
    if record is None:
        break
    run = (ROOT / record['run_path']).resolve()
    check(run.is_relative_to((ROOT / 'ml/option-pricing-runs').resolve()), 'Authority path leaves Pricing root')
    receipt = load(run / 'publication.json')
    check(digest(run / 'manifest.json') == record['manifest_checksum_sha256'], 'Authority manifest binding differs')
    check(digest(run / 'publication.json') == record['receipt_checksum_sha256'], 'Authority receipt binding differs')
    check(receipt['run_path'] == record['run_path'] and receipt['manifest_checksum_sha256'] == record['manifest_checksum_sha256'], 'Authority receipt identity differs')
    chain.append({'run_path': record['run_path'], 'published_at': record['published_at']})
    record = receipt.get('previous_publication')
check(record is None and chain == prior['authority_chain'], 'Reachable authority chain differs')
log_path = RUN / 'loop_b_directional_generation.log'
raw = log_path.read_bytes()
lines = raw.decode('utf-8', errors='replace').splitlines()
quarantines = []
for number, line in enumerate(lines, 1):
    match = re.fullmatch(r'\[Loop B/([^]]+)\] Option Pricing family quarantined; fitting (\S+) until the coverage/freshness gate passes \((.*)\)', line)
    if match:
        quarantines.append({'horizon': match[1], 'effective_feature_set': match[2],
                            'failed_routes': match[3].split(', '), 'log_line_number': number})
def identities(records):
    return [{k: row[k] for k in ('horizon', 'effective_feature_set', 'failed_routes')} for row in records]
check(identities(quarantines) == identities(prior['native_quarantine']), 'Native quarantine routes/contracts differ')
failed_routes = sorted(route for row in quarantines for route in row['failed_routes'])
check(len(failed_routes) == len(set(failed_routes)) == 99, 'Expected 99 unique exact quarantine routes')
sys.path.insert(0, 'C:/dev/ducketz')
from ml.horizons import horizon_specifications_for_profile
from ml.runtime_pipeline import (_specification_for_pricing_gate,
    OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION, OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE,
    OPTION_PRICING_LOOP_B_MINIMUM_DISTINCT_TARGETS)
specs = horizon_specifications_for_profile('loop-a-all-bsgp-active-v3', horizons=[q['horizon'] for q in quarantines])
contracts = {}
for row in quarantines:
    requested = specs[row['horizon']]
    effective = _specification_for_pricing_gate(requested, gate={'enabled': True, 'downstream_training_eligible': False})
    check(effective.feature_set == row['effective_feature_set'], 'Pure native fallback differs from log')
    contracts[row['horizon']] = {'requested_feature_set': requested.feature_set,
        'effective_feature_set': effective.feature_set, 'exact_non_pricing_feature_preservation': 'PASS'}
policy = {'version': OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION,
    'minimum_complete_row_fraction': OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE,
    'minimum_fresh_joined_row_fraction': OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE,
    'minimum_distinct_surface_targets': OPTION_PRICING_LOOP_B_MINIMUM_DISTINCT_TARGETS}
check(policy == prior['gate_policy'], 'Current native thresholds differ')
base = load(ROOT / '.ducketz-loop-a-complete.json')
check(base['generation'] == '20260924T044353.640845Z-pid64532' and base['status'] == 'COMPLETE', 'Base input cutoff is not current native cycle')
manifest_path = LOOP_B / 'manifest.json'
publication_path = LOOP_B / 'publication.json'
gate, group_summary, publication_binding = None, {}, None
current_manifest = manifest_path.is_file()
current_publication = publication_path.is_file()
if current_manifest and current_publication:
    manifest = load(manifest_path)
    publication = load(publication_path)
    configuration = manifest['configuration']
    check(publication['manifest_checksum_sha256'] == digest(manifest_path), 'Current publication manifest checksum differs')
    check(publication['run_path'] == LOOP_B.relative_to(ROOT).as_posix(), 'Current publication identity differs')
    check(timestamp(configuration['causal_input_cutoff']) == timestamp(base['finished_at']), 'Current causal cutoff differs from base cycle')
    check(configuration['route_errors'] == {}, 'Current route errors are present')
    gate = configuration['pricing_evidence']
    check(sorted(gate['failed_routes']) == failed_routes and set(gate['route_gates']) == set(failed_routes), 'Manifest gate route identities differ')
    check(gate['enabled'] is True and gate['downstream_training_eligible'] is False, 'Manifest Pricing gate admission differs')
    check(gate['policy_version'] == policy['version'] and gate['thresholds'] == {k:v for k,v in policy.items() if k != 'version'}, 'Manifest gate thresholds differ')
    check(gate['model_admission_by_horizon'] == prior['current_native_gate']['model_admission_by_horizon'], 'Model admission differs from prior verified policy')
    for route, report in gate['route_gates'].items():
        check(report['pass'] is False, 'Unexpected passing Pricing route: ' + route)
        for group, values in report['groups'].items():
            summary = group_summary.setdefault(group, {'routes':0, 'passing_routes':0,
                'maximum_complete_fraction':0, 'maximum_fresh_joined_fraction':0, 'maximum_distinct_targets':0})
            summary['routes'] += 1
            summary['passing_routes'] += int(values['pass'])
            for target, field in [('maximum_complete_fraction','complete_row_fraction'),
                                  ('maximum_fresh_joined_fraction','fresh_joined_row_fraction'),
                                  ('maximum_distinct_targets','distinct_surface_targets')]:
                summary[target] = max(summary[target], values[field])
    publication_binding = {'manifest_path': str(manifest_path), 'manifest_sha256': digest(manifest_path),
        'publication_path': str(publication_path), 'publication_sha256': digest(publication_path),
        'route_errors': configuration['route_errors']}
import pyarrow.parquet as pq
import pandas as pd
native_tables = {}
for name, binding in manifest['output_files'].items():
    path = LOOP_B / name
    actual_hash = digest(path)
    check(path.stat().st_size == binding['size'] and actual_hash == binding['checksum_sha256'], 'Current Loop B output checksum differs: ' + name)
    native_tables[name] = {'path': str(path), 'size_bytes': path.stat().st_size, 'sha256': actual_hash}
    if name.endswith('.parquet'):
        native_tables[name]['rows'] = pq.read_metadata(path).num_rows
sample_metadata = pq.read_metadata(LOOP_B / 'samples.parquet')
check(sample_metadata.num_rows <= 2_000_000, 'Sample metadata exceeds bounded projected review limit')
samples = pd.read_parquet(LOOP_B / 'samples.parquet', columns=['symbol', 'horizon'])
sample_routes = [{'symbol': str(symbol), 'horizon': str(horizon), 'rows': int(count)} for (symbol, horizon), count in samples.groupby(['symbol', 'horizon']).size().items()]
expected_sample_routes = {(symbol, horizon) for symbol in configuration['symbols'] for horizon in configuration['horizons']}
check({(x['symbol'], x['horizon']) for x in sample_routes} == expected_sample_routes, 'Saved sample symbol/horizon coverage differs')
completed_lines = [line for line in lines if line.startswith('COMPLETED: ')]
check(len(completed_lines) == 1, 'Expected one native model-training completion line')
completion = dict(re.findall(r'(\w+)=(\d+)', completed_lines[0])) if completed_lines else {}
check(int(completion.get('models_trained', 0)) + int(completion.get('models_reused', 0)) == len(configuration['models']) == len(configuration['horizons']), 'Native model-count coverage differs')
check(int(completion.get('samples', 0)) == sample_metadata.num_rows, 'Native sample count differs from saved parquet')
check(int(completion.get('evaluations', 0)) == native_tables['evaluations.parquet']['rows'], 'Native evaluation count differs')
check(configuration['publication_counts']['total_prediction_rows'] == native_tables['predictions.parquet']['rows'], 'Published prediction count differs')
stage = load(RUN / 'stage-report.json')
result = {'audited_at':datetime.now(timezone.utc).isoformat(),
    'status':'ISSUES_FOUND' if issues else 'KNOWN_PRICING_QUARANTINE_UNCHANGED_COMPACT_AUTHORITY',
    'issues':issues, 'overnight_run_path':str(RUN), 'loop_b_run_path':str(LOOP_B),
    'native_status_at_audit':{k:stage[k] for k in ('status','current_stage','stage_health','heartbeat_at')},
    'model_training_completion_verified':not issues, 'native_loop_b_stage':loop_b_stage, 'native_training_completion':completion, 'native_output_tables':native_tables, 'sample_routes':sample_routes, 'expected_sample_route_count':len(expected_sample_routes), 'current_models':configuration['models'], 'current_publication_counts':configuration['publication_counts'], 'log_path':str(log_path),
    'log_snapshot_sha256':hashlib.sha256(raw).hexdigest(), 'prior_evidence_path':str(PRIOR),
    'prior_evidence_sha256':digest(PRIOR), 'pricing_pointer_path':str(pointer_path),
    'pricing_pointer':pointer, 'pricing_pointer_matches_prior':pointer==prior['pricing_pointer'],
    'authority_chain':chain, 'compact_source_files_checked':len(files),
    'compact_source_files_matched':sum(f['matches_prior'] for f in files),
    'compact_source_bytes_hashed':sum(f['bytes'] for f in files), 'compact_source_comparison':files,
    'source_characteristics_from_unchanged_verified_prior_evidence':prior['source_characteristics_from_unchanged_verified_prior_evidence'],
    'native_quarantine':quarantines, 'native_quarantine_matches_prior':identities(quarantines)==identities(prior['native_quarantine']),
    'failed_route_count':len(failed_routes), 'effective_feature_contracts':contracts, 'gate_policy':policy,
    'causal_input_cutoff':base['finished_at'], 'materialization_log':[line for line in lines if 'END   loop-b.materialize-samples ' in line],
    'current_manifest_available':current_manifest, 'current_publication_available':current_publication,
    'current_native_gate':gate, 'current_group_summary':group_summary, 'current_publication_binding':publication_binding,
    'current_route_fraction_review':'VERIFIED' if publication_binding else 'PENDING_NATIVE_PUBLICATION',
    'scope':'Exactly 51 previously verified compact surfaces/manifests/receipts, current pointer chain, native logs and small receipts, pure native feature fallback, and current manifest/publication if present. No normalization, archive scan, training/retry, provider/broker calls or production writes.',
    'conclusion':'Same sparse stale August 20 Pricing authority and native optional exclusion. Exact current model, sample and source gates checked; preserve the non-Pricing feature fallback. No new Pricing defect or justified retry identified.'}
(OUT / 'pricing-gate-review.json').write_text(json.dumps(result,indent=2)+'\n', encoding='utf-8')
md = ['# September 24 Pricing gate review','',f"Reviewed {result['audited_at']}. **{result['status']}**.",'',
    f"All **{result['compact_source_files_matched']}/51** compact source bindings match yesterday ({result['compact_source_bytes_hashed']:,} bytes). The exact 17-generation authority chain still ends at August 20. Verified prior characteristics remain applicable: 72 natural rows, 17 targets, missing COST/CROX/IONQ/PATH/TWST and no predictive standard deviation or interval-calibration columns.",'',
    'The native log quarantines the same **99 exact symbol/horizon routes across nine groups**. Pure native fallback verifies exact preservation of all requested non-Pricing features. Coverage gates remain **80% complete, 80% fresh joined, and 20 distinct usable targets**. Historical rows can have been fresh at their original decision clocks; the authority remains stale at the current cutoff.', '',
    f"Current causal cutoff: `{base['finished_at']}`. Current manifest/publication available: **{current_manifest}/{current_publication}**. Current saved route fractions: **{result['current_route_fraction_review']}**. Native Loop B completion and bounded model/sample/publication counts are verified; no full overnight or archive Gameplan completion claim is made.",'']
if group_summary:
    md += ['| Subfamily | Maximum complete | Maximum fresh joined | Maximum targets | Passing routes |','|---|---:|---:|---:|---:|']
    for name, values in sorted(group_summary.items()):
        md.append(f"| {name} | {values['maximum_complete_fraction']:.4%} | {values['maximum_fresh_joined_fraction']:.4%} | {values['maximum_distinct_targets']} | {values['passing_routes']}/{values['routes']} |")
md += ['',f"Discrepancies: **{len(issues)}**. Preserve native gates and reduced feature contracts; no fresh source defect or justified focused repair was found. OPRA completion does not create a Pricing publication. CME raw-contract limitations remain separate.",'',
       '[Exact evidence](C:/dev/ducketz/artifacts/analysis/overnight-20260924/resumed-provider/pricing-gate-review.json)','']
(OUT / 'pricing-gate-review.md').write_text('\n'.join(md),encoding='utf-8')
print(json.dumps({k:result[k] for k in ('audited_at','status','issues','compact_source_files_matched','failed_route_count','current_manifest_available','current_publication_available','current_route_fraction_review','current_group_summary')},indent=2))
raise SystemExit(1 if issues else 0)
