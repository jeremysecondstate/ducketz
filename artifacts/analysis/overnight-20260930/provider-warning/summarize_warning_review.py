"""Finalize the read-only warning triage after native Loop A has completed."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re

OUT = Path(__file__).resolve().parent
RUN = Path('C:/DATASTORE/ml/overnight-runs/20260930T040723.733025Z')
data = (OUT/'latest.json').read_bytes()
review = json.loads(data)
report = json.loads((RUN/'stage-report.json').read_text(encoding='utf-8-sig'))
stage = next(row for row in report['stages'] if row['stage'] == 'loop_a_close_fetch')
assert stage['status'] == 'COMPLETE'
native_log = (RUN/'loop_a_close_fetch.log').read_bytes()
assert hashlib.sha256(native_log).hexdigest() == review['log_snapshot']['sha256']
vendor = [row for row in review['warnings'] if 'reduced quality' in row['text']]
derived = [row for row in review['warnings'] if 'advisory recorded' in row['text']]
zero_calculations = [row for row in review['warnings'] if re.fullmatch(r'Failed calculations:\s*0',row['text'])]
maintenance = [row for row in review['warnings'] if row['text'].startswith('Options history maintenance finished: ')]
maintenance_fields = [dict(re.findall(r'(\w+)=([^;\s]+)',row['text'])) for row in maintenance]
healthy_maintenance = len(maintenance_fields)==1 and all(maintenance_fields[0].get(key)==value for key,value in {
    'requested_scopes':'33','completed_scopes':'33','capacity_blocked_scopes':'0','failed_scopes':'0',
    'bootstrap_required_scopes':'0','preflighted_scopes':'33','deferred_scopes':'0',
    'live_replay_completed_scopes':'0','live_replay_bytes':'0','selected_estimated_cost_usd':'0.0',
}.items())
other = [row for row in review['warnings'] if row not in vendor+derived+zero_calculations+(maintenance if healthy_maintenance else [])]
completed_requests = [row for row in review['cme_request_log'] if '] END ' in row['text'] and 'status=ok' in row['text']]
expected = {'CME_CONTEXT':{'CL.v.0','ES.v.0','GC.v.0','NQ.v.0','RTY.v.0'},
            'CME_CONTRACTS':{'CLX6','ESZ6','GCZ6','NQZ6'}}
checks = {
    'native_loop_a_complete':stage['status']=='COMPLETE',
    'all_six_cme_requests_completed':len(completed_requests)==6,
    'all_six_current_captures_observed':len(review['current_cme_captures'])==6 and all(row['current_native_rows'] for row in review['current_cme_captures']),
    'no_failed_request_or_traceback_candidate':not review['new_failure_candidates'],
    'all_eleven_technical_summaries_zero_failures':len(zero_calculations)==11,
    'no_unclassified_log_warning':not other,
    'native_opra_summary_zero_failed_scopes_and_cost':healthy_maintenance,
    'degraded_dates_only_captured_current_utc_day':review['provider_degraded_dates']==[['2026-09-30','degraded']],
    'configured_symbol_scopes_observed':all({group['symbol'] for group in row['symbols']} == expected[Path(row['path']).name.split('_cme_')[0]] for row in review['current_cme_captures']),
}
result = {
    'reviewed_at':datetime.now(timezone.utc).isoformat(),
    'status':'TRIAGE_COMPLETE_EXPLICIT_PROVIDER_QUALITY_LIMITATION' if all(checks.values()) else 'REVIEW_REQUIRED',
    'checks':checks,'native_stage':stage,
    'warning_classification':review['classification'],
    'vendor_reduced_quality_warning_occurrences':len(vendor),'vendor_reduced_quality_dates':review['provider_degraded_dates'],
    'derived_context_advisories':derived,'other_warning_lines':other,
    'native_opra_summary':maintenance_fields,
    'final_snapshot':{'path':str(OUT/'latest.json'),'sha256':hashlib.sha256(data).hexdigest()},
    'cme_capture_summary':[{'path':row['path'],'sha256':row['sha256'],'current_rows':row['current_native_rows'],
                            'symbols':[group['symbol'] for group in row['symbols']],
                            'request_metadata':row['current_request_metadata']} for row in review['current_cme_captures']],
    'findings':review['findings'],
    'required_final_audit':'Native completed-artifact, cost, provider coverage, input eligibility and source checksum verification remain required independently.',
    'provider_calls':0,'production_writes':0,'orders_placed':0,
}
(OUT/'final-triage.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
if not all(checks.values()):
    raise RuntimeError('CURRENT_WARNING_TRIAGE_REQUIRES_REVIEW')
lines = ['# Current provider-warning triage','',f"Reviewed: {result['reviewed_at']}. Native Loop A completed.",'',
    f"{len(vendor)} Databento warning occurrences identify September 30 as degraded. That UTC day was still in progress at acquisition, and the warning concerns current-day CME context extending after the completed September 29 stock-session boundary. The saved evidence does not establish that incomplete-day publication is the sole cause; the vendor quality warning remains explicit.",'',
    'All six CME requests succeeded and persisted their configured symbol scopes. OHLCV context/contracts contain 2,389/4,079 current rows; BBO contains 300/240. Both MBP captures contain 5,000 rows and remain explicitly limit-saturated. Adaptive effective request bounds are recorded; successful delivery is not full-range completeness or model eligibility.','',
    'The separate retained CME derived-context rejection matches the previous candidate, policy and 15-minute tolerance; only the stale NQ BBO age advances with receipt time. Its native guard remains enforced. Eleven technical summaries reported zero failures.','',
    'No provider calls, retries, production edits, configuration changes, gate changes or order actions were made by this triage. Final source/coverage audits remain separate.','',
    'Evidence: final-triage.json, latest.json, immutable warning-review-*.json snapshots and cme-derived-advisory.json.','']
(OUT/'review.md').write_text('\n'.join(lines),encoding='utf-8')
print(json.dumps({'status':result['status'],'checks':checks,'unclassified_warning_lines':len(other)}))
