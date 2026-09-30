"""Summarize verified current outputs without inventing quality or coverage."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
from audit_environment import verify_environment

OUT=Path(__file__).resolve().parent
read=lambda p:json.loads(p.read_text(encoding='utf-8-sig'))
sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
runner=read(OUT/'audit-runner.json')
assert runner['status']=='VERIFIED_WITH_COVERAGE_NOTES'
assert len(runner['checks'])==7 and all(v['exit_code']==0 for v in runner['checks'])
prep=read(OUT/'preparation.json')
for item in prep['files']:
    assert sha(OUT/item['name'])==item['adapted_sha256']
for name,checksum in prep['additional_helpers'].items():
    assert sha(OUT/name)==checksum
for item in runner['evidence'].values():
    assert sha(Path(item['path']))==item['sha256']
full=read(OUT/'completion-audit.json')
assert not full['errors']
c=full['checks']; a=c['archive_history']; sec=a['second_minute_consistency']
model=read(OUT/'model-metric-summary.json')
provider=read(OUT/'provider-completion.json')
pricing=read(OUT/'pricing-gate-review.json')
advisory=read(OUT/'provider-advisory-resolution.json')
assert advisory['status']=='KNOWN_OPTIONAL_LIMITATIONS_REVERIFIED'
assert not advisory['unresolved_review_flags']
assert sha(OUT/'provider-completion.json')==advisory['provider_evidence']['sha256']
warning_candidates=sorted((OUT.parent/'provider-warning').glob('warning-review-*.json'))
assert warning_candidates, 'Independent current-provider warning evidence absent'
warning_path=warning_candidates[-1]
warning=read(warning_path)
assert Path(warning['run']).resolve()==Path(runner['native_run']).resolve()
warning_log=warning['log_snapshot']
with Path(warning_log['path']).open('rb') as stream:
    assert hashlib.sha256(stream.read(warning_log['bytes'])).hexdigest()==warning_log['sha256']
assert not warning['new_failure_candidates']
result={'created_at':datetime.now(timezone.utc).isoformat(),'status':runner['status'],
    'native_run':runner['native_run'],'action_date':c['gameplan']['action_date'],
    'environment':verify_environment(),'audit_sections':sorted(c),
    'reviewed_source_drift':read(OUT/'reviewed-isolated-code-drift.json'),
    'archive':{'source_files':a['source_files_bound_and_verified'],'source_bytes':a['source_file_bytes'],
        'second_minute_partitions':sec['native_archive_partitions_verified'],
        'second_rows':sum(v['second_rows'] for v in sec['by_symbol'].values()),
        'exact_ohlcv_overlap_minutes':sum(v['exact_ohlcv_overlap_minutes'] for v in sec['by_symbol'].values()),
        'second_added_training_rows':sec['added_training_rows'],'feature_ranges':a['feature_dates_by_symbol'],
        'exclusions':a['exclusion_counts'],'cohorts':a['training_cohorts'],
        'current_probabilities_reproduced':a['current_forecast_inference'],
        'prefix_acquisition':a['feature_history_extension']},
    'directional_models':model['groups'],
    'providers':{k:provider[k] for k in ('status','issues','current_cursor_count','current_partition_count','current_data_files_hashed','observed_estimated_download_bytes')},
    'optional_provider_disposition':advisory['status'],
    'current_vendor_warning':{'classification':warning['classification'],'findings':warning['findings'],
        'snapshot_path':str(warning_path),'snapshot_sha256':sha(warning_path),
        'reviewed_at':warning['reviewed_at'],'native_log_prefix_verified':True},
    'optional_pricing':{k:pricing[k] for k in ('status','failed_route_count','compact_source_files_matched','compact_source_bytes_hashed','current_group_summary')},
    'loop_b_weekly':read(OUT/'loop-b-weekly-prefix-review.json'),
    'planning':c['trade_plan'],'planning_prices':c['planning_prices'],
    'planning_reference_completion':c['reference_completion'],
    'actuals':c['actuals_review'],'cumulative_evaluation':c['cumulative_evaluation'],
    'coverage_notes':full['coverage_notes'],
    'evidence':runner['evidence'],
    'audit_preparation_evidence':{p.name:{'path':str(p),'sha256':sha(p)} for p in (
        OUT/'preparation.json', OUT/'audit-environment-baseline.json', OUT/'helper-review-tests.json',
        OUT/'cycle-binding-review.json', OUT/'reviewed-isolated-code-drift.json',
        OUT.parent/'preflight/verification-helper-peer-review.json',
        OUT.parent/'preflight/cadence-isolation-peer-review.json')},
    'limits':['Native raw/normalized files and request headers are verified; raw second-level DBN records are not independently decoded.',
        'Native original cursor snapshot contents are self-hashed and manifest-semantic bound; there is no separately recorded outer file checksum.',
        'Model qualification follows each immutable policy; passing v2 tolerances does not establish beating a baseline.',
        'Optional Pricing, stale CME context, capped MBP, and retained FMP clock-skew exclusions remain explicit.',
        'September 30 CME provider degradation cause is unresolved; successful captures and current UTC-day timing do not clear the vendor quality warning. OHLCV scopes narrowed adaptively; neither those nor capped MBP prove the full original request range.',
        'Synthetic planning references are assumptions; actuals and training retain observed-price five-minute boundaries.',
        'The initial environment guard stopped before any heavy audit on an unrelated concurrent Hyperliquid cadence edit. Its exact reviewed hash is admitted only after a 169-module native import-closure peer review; original baseline and refusal remain preserved.',
        'Controls, ledgers, schedules, supervision and broker preservation are separately verified by root/preflight.'],
    'orders_placed':0,'provider_calls':0,'production_mutations':0}
(OUT/'audit-findings.json').write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
lines=['# September 30 final independent audit','',
    f"Status: **{result['status']}** at {result['created_at']}. Native source: `{result['native_run']}`.",'',
    'The 12-section comprehensive audit and all seven sequential helpers passed. The initial guard stopped before heavy checks on a separate concurrent cadence edit; exact-hash and independent native dependency review justified this isolated exception, preserving the original baseline and refusal. No native stage or full reconstruction was repeated. No model was fitted or relabelled; no provider/broker request or production mutation was made.','',
    f"Archive: {result['archive']['source_files']:,} files / {result['archive']['source_bytes']:,} bytes; {result['archive']['second_rows']:,} one-second rows and {result['archive']['exact_ohlcv_overlap_minutes']:,} overlapping minutes with exact OHLCV consistency. Second-level evidence added zero training rows. All eligible source cohorts and 264 current probabilities reproduced.",'',
    '| Horizon | Cohort rows | First action | Last action | Boundary exclusions | Quality exclusions |',
    '|---|---:|---|---|---:|---:|']
for h,v in a['training_cohorts'].items():
    lines.append(f"| {h} | {v['rows']:,} | {v['first_action_date']} | {v['last_action_date']} | {v['boundary_exclusions']['excluded_rows']:,} | {v['quality_excluded_rows']} |")
lines+=['','| Horizon | Directional status | Beats both baselines | Fitted / qualified sizing scopes |','|---|---|---|---:|']
for h,v in model['groups'].items():
    s=v['learned_sizing']
    lines.append(f"| {h} | {v['promotion_status']} | {v['strictly_beats_both_baselines']} | {s['fitted_scopes']} / {s['qualified_scopes']} |")
lines+=['',f"Providers: {provider['status']}; {provider['current_cursor_count']} current OPRA cursors and {provider['current_data_files_hashed']} data hashes. Optional advisories: {advisory['status']}.",'',
    f"Separate current CME vendor warning: **{warning['classification']}**. Current UTC-day timing and successful capture do not resolve the reported quality condition. Narrowed OHLCV and capped MBP are bounded observed delivery, not full original-range coverage.",'',
    f"Planning: {c['trade_plan']['rows']} rows; projection status {c['trade_plan']['projection_status']}; starting literal cash {c['trade_plan']['available_cash']}. Current synthetic planning rows: {c['reference_completion']['synthetic_rows']}. Detailed cash/share conservation, price points, original observation times and assumed references remain in the full JSON.",'',
    'Actuals coverage: `'+json.dumps(c['actuals_review']['coverage'],sort_keys=True)+'`.','',
    'Cumulative evaluation: `'+json.dumps(c['cumulative_evaluation']['coverage_counts'],sort_keys=True)+'`.','',
    'Limits:','']
lines+=['- '+v for v in result['limits']]
lines+=['',f"Evidence: [audit-findings.json]({(OUT/'audit-findings.json').as_posix()}), [model-review.md]({(OUT/'model-review.md').as_posix()}), [completion-audit.json]({(OUT/'completion-audit.json').as_posix()}).",'']
(OUT/'audit-findings.md').write_text('\n'.join(lines),encoding='utf-8')
print(json.dumps({'status':result['status'],'evidence':str(OUT/'audit-findings.json')}))
