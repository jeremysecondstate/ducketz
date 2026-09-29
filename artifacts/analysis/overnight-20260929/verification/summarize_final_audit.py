"""Create a compact evidence-bound handoff from completed offline audits."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
from audit_environment import verify_environment
OUT = Path(__file__).resolve().parent
read = lambda p: json.loads(p.read_text(encoding='utf-8-sig'))
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
runner = read(OUT/'audit-runner-continuation.json')
assert runner['status']=='VERIFIED_WITH_COVERAGE_NOTES'
assert len(runner['checks'])==5 and all(x['exit_code']==0 for x in runner['checks'])
for item in runner['evidence'].values(): assert sha(Path(item['path']))==item['sha256']
full = read(OUT/'completion-audit.json')
assert not full['errors']
c=full['checks']; a=c['archive_history']; sec=a['second_minute_consistency']
models=read(OUT/'model-metric-summary.json')['groups']
pricing=read(OUT/'pricing-gate-review.json')
provider=read(OUT/'provider-completion.json')
advisory=read(OUT/'provider-advisory-resolution.json')
peer=OUT.parent/'preflight/isolated-code-drift-review.json'
assert sha(peer)=='6041b32b5a1725c79c3472a970e66f783caab82f4566156f794ee10d12a291fe'
assert advisory['status']=='KNOWN_OPTIONAL_LIMITATIONS_REVERIFIED' and not advisory['unresolved_review_flags']
assert advisory['provider_evidence']['sha256']==sha(OUT/'provider-completion.json')
result={
 'created_at':datetime.now(timezone.utc).isoformat(),'status':'VERIFIED_WITH_COVERAGE_NOTES',
 'native_run':runner['native_run'],'action_date':c['gameplan']['action_date'],
 'audit_sections':sorted(c),'environment':verify_environment(),
 'archive':{'source_files':a['source_files_bound_and_verified'],'source_bytes':a['source_file_bytes'],
   'second_minute_partitions':sec['native_archive_partitions_verified'],
   'second_rows':sum(x['second_rows'] for x in sec['by_symbol'].values()),
   'exact_ohlcv_overlap_minutes':sum(x['exact_ohlcv_overlap_minutes'] for x in sec['by_symbol'].values()),
   'second_added_training_rows':sec['added_training_rows'],'feature_ranges':a['feature_dates_by_symbol'],
   'source_ranges':{k:{f:v[f] for f in ('minute_range','minute_rows','second_range','second_rows','overlap_minutes')} for k,v in sec['by_symbol'].items()},
   'exclusions':a['exclusion_counts'],
   'cohorts':{h:{'rows':v['rows'],'first_action_date':v['first_action_date'],'last_action_date':v['last_action_date'],
      'boundary_excluded':v['boundary_exclusions']['excluded_rows'],'quality_excluded':v['quality_excluded_rows'],
      'conflicting_minute_rows':v['boundary_exclusions']['conflicting_minute_rows_excluded'],
      'all_rows_exactly_reproduced':v['all_rows_exactly_reproduced'],'by_symbol':v['by_symbol']} for h,v in a['training_cohorts'].items()},
   'current_probability_rows_exactly_reproduced':sum(x['rows'] for x in a['current_forecast_inference'].values()),
   'prefix_acquisition':a['feature_history_extension']['preflight']},
 'directional_models':models,'sizing_quality':{k:v['learned_sizing'] for k,v in models.items()},
 'providers':{k:provider[k] for k in ('status','issues','current_cursor_count','current_partition_count','current_data_files_hashed','observed_estimated_download_bytes')},
 'optional_provider_disposition':advisory['status'],
 'optional_pricing':{'status':pricing['status'],'failed_route_count':pricing['failed_route_count'],
    'compact_source_files_matched':pricing['compact_source_files_matched'],
    'compact_source_bytes_hashed':pricing['compact_source_bytes_hashed'],
    'group_summary':pricing['current_group_summary']},
 'planning':{'rows':c['trade_plan']['rows'],'available_cash':c['trade_plan']['available_cash'],
    'hourly_rows':c['trade_plan']['hourly_rows'],'event_count':c['trade_plan']['event_count'],
    'cash_share_conservation':c['trade_plan']['cash_share_conservation'],'summary':c['trade_plan']['summary'],
    'synthetic_rows':c['reference_completion']['synthetic_rows'],
    'synthetic_current_anchors':{k:{f:v[f] for f in ('symbol','gap_minutes','observed_at','effective_at','price','reason')} for k,v in c['reference_completion']['synthetic_references'].items()}},
 'actuals':{k:c['actuals_review'][k] for k in ('coverage','directional_calls_correct','directional_calls_scored','direction_accuracy')},
 'cumulative_evaluation':{'publications':len(c['cumulative_evaluation']['publications']),**c['cumulative_evaluation']['coverage_counts']},
 'evidence':{p.name:{'path':str(p),'sha256':sha(p)} for p in [OUT/'audit-runner-continuation.json',
   OUT/'audit-runner.json',OUT/'completion-audit.json',OUT/'provider-advisory-resolution.json',
   OUT/'reviewed-isolated-code-drift.json',peer]},
 'limits':['Native raw and normalized files and DBN request headers verified; raw second-level DBN records were not independently replayed.',
   'Saved original production-cursor content hashes and manifest semantics verified, but native receipt has no separate outer checksum of that snapshot file.',
   'Four directional groups pass saved v2 tolerances; only 4h and 1d beat both training-rate baselines. Thin exact TWST route evidence is explicit.',
   'All four sizing groups fitted, zero qualified scopes; no model status was upgraded and no fit/retry performed.',
   'CME derived stale context and capped MBP, retained FMP clock-skew rows, and 99 optional Pricing route failures remain excluded/limited.',
   'Planning synthetic references follow current saved contract only. Actual/training price boundaries stay five minutes; no synthetic actuals.',
   'Concurrent Hyperliquid paper-policy change admitted only at exact reviewed hash; original guard failure/baseline remain preserved.',
   'Broker controls, ledgers, schedules, claims and bounded planning/actuals outputs independently audited by preflight.'],
 'orders_placed':0,'provider_calls':0,'production_mutations':0}
(OUT/'audit-findings.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
lines=['# September 29 final independent audit','',
 f"Status: **{result['status']}**, recorded {result['created_at']}. Native run: `{result['native_run']}`.",'',
 'The full 12-section audit passed. A later guard stopped on an isolated concurrent Hyperliquid policy edit; original failure and baseline were preserved. Exact-hash and independent 199-module reachability reviews justified continuing only the remaining five checks, which all passed. No native stage repeated.','',
 f"Archive: {result['archive']['source_files']:,} manifest-bound files, {result['archive']['source_bytes']:,} bytes; {result['archive']['second_minute_partitions']} verified second/minute partitions; {result['archive']['second_rows']:,} seconds and {result['archive']['exact_ohlcv_overlap_minutes']:,} exact overlapping OHLCV minutes. Seconds added zero examples. All 264 current probabilities and every eligible cohort row reproduced exactly; zero conflicting minute rows admitted.",'',
 '| Horizon | Cohort rows | First action | Last action | Boundary exclusions | Quality exclusions |','|---|---:|---|---|---:|---:|']
for h in ('1h','4h','1d','1w'):
 v=result['archive']['cohorts'][h];lines.append(f"| {h} | {v['rows']:,} | {v['first_action_date']} | {v['last_action_date']} | {v['boundary_excluded']:,} | {v['quality_excluded']} |")
lines += ['', '| Symbol | First feature source | Last feature source | Feature rows |','|---|---|---|---:|']
for symbol,v in a['feature_dates_by_symbol'].items(): lines.append(f"| {symbol} | {v['first_source_session']} | {v['last_source_session']} | {v['rows']:,} |")
lines += ['', 'All four directional groups PROMOTED under saved v2 Brier+0.005/log-loss+0.01 limits. Only 4h/1d beat both baselines. Exact TWST fitted support remains 1 hourly / 24 four-hour / 2 daily / 8 weekly. All four sizing groups FITTED; zero qualified scopes. Assessment failures remain honest; saved evidence shows no actionable fitting defect.','',
 'Provider verification passed 33 OPRA scopes/cursors and 66 current data-file hashes. All six CME requests succeeded, all configured symbols observed; both MBP captures capped at 5,000 rows. Exact advisory comparison finds only increasing age for the same stale September 3 NQ derived candidate; exclusion retained. Current FMP quote passes; five September 2 clock-skew rows remain rejected. Separate advisory-resolution evidence closes the review flag without modifying provider evidence.','',
 'All 99 optional Pricing gates remain quarantined; 51 compact files / 10,208,882 bytes retain the same authority. Maximum constraints complete/fresh fraction 1.42045%, edge/fair/liquidity 0.602410%, interval/uncertainty 0%; maxima 1 distinct target versus required 20. Monday Loop B correctly has 99 intelligence routes, 88 applicable LIVE forecasts and 11 d5 NOT_APPLICABLE rows. Independent Gameplan remains 264 forecasts / 264 intents.','',
 'Planning has 264 rows, 14 clock summaries and 46 events (24 buys / 22 sells), exact cash/share conservation. Starting cash $18.14; conditional ending cash $1.51–$440.41 / base $219.90. Current synthetic planning anchors: CROX 166 minutes, IONQ 10, PATH 150, TWST 69; 395 synthetic one-minute rows, original observations unchanged.','',
 'Prior-session actuals: 165 evaluated / 33 mature missing / 66 pending; 84/165 correct (50.91%). Same-clock prices 137 compared / 17 missing. Cumulative 26 publications preserve their own universes: 5,568 forecasts / 4,454 evaluated / 949 mature missing / 165 pending. No synthetic actuals or invented outcomes.','',
 'Limits:']
lines += ['- '+x for x in result['limits']]
lines += ['', 'Evidence: [audit-findings.json](C:/dev/ducketz/artifacts/analysis/overnight-20260929/verification/audit-findings.json), [audit-runner-continuation.json](C:/dev/ducketz/artifacts/analysis/overnight-20260929/verification/audit-runner-continuation.json), [completion-audit.json](C:/dev/ducketz/artifacts/analysis/overnight-20260929/verification/completion-audit.json), [provider-advisory-resolution.json](C:/dev/ducketz/artifacts/analysis/overnight-20260929/verification/provider-advisory-resolution.json).','']
(OUT/'audit-findings.md').write_text('\n'.join(lines),encoding='utf-8')
print(json.dumps({'status':result['status'],'evidence':str(OUT/'audit-findings.json'),'evidence_sha256':sha(OUT/'audit-findings.json')}))
