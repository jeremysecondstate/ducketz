"""Close this task's local evidence after native release; never run a pipeline."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import psutil

BASE = Path(__file__).resolve().parent
NATIVE = Path('C:/DATASTORE/ml/overnight-runs/20260929T040727.779758Z')
TOKEN = '29918557-9e94-49dd-b438-9cd2c74337fd'

def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def bind(path):
    return {'path': str(path), 'sha256': sha(path)}

continuation = read(BASE/'verification/audit-runner-continuation.json')
assert continuation['status'] == 'VERIFIED_WITH_COVERAGE_NOTES'
assert len(continuation['checks']) == 5
assert all(x['exit_code'] == 0 for x in continuation['checks'])
assert continuation['inherited_successful_check']['exit_code'] == 0
assert continuation['inherited_successful_check']['completion_sha256'] == sha(BASE/'verification/completion-audit.json')
full = read(BASE/'verification/completion-audit.json')
assert full['status'] == 'VERIFIED_WITH_COVERAGE_NOTES' and not full['errors']
assert len(full['checks']) == 12
assert read(BASE/'verification/yg-completion.json')['status'] == 'VERIFIED'
assert not read(BASE/'verification/provider-advisory-resolution.json')['unresolved_review_flags']
for name in ('final-output-review.json', 'final-preservation.json'):
    evidence = read(BASE/'preflight'/name)
    assert evidence['status'] == 'PASS', (name, evidence.get('status'))
assert read(BASE/'preflight/actuals-coverage-review.json')['status'] == 'SAVED_ACTUALS_COVERAGE_VERIFIED'
peer = read(BASE/'preflight/completion-summary-peer-review.json')
assert peer['status'] in ('PASS', 'NO_CORRECTIONS_REMAIN', 'PASS_NO_CORRECTIONS'), peer.get('status')
summary_path = BASE/'completion-summary.md'
assert peer['summary_sha256'] == sha(summary_path), 'Summary changed after peer review'
assert read(NATIVE/'receipt.json')['status'] == 'COMPLETE'
assert read(NATIVE/'stage-report.json')['orders_placed'] == 0

release = read(BASE/'supervision-release.json')
assert release['status'] == 'RELEASED' and release['owner_token'] == TOKEN
assert (BASE/'monitor/STOP').exists()
running_helpers = []
for proc in psutil.process_iter(['pid', 'cmdline']):
    try:
        command = ' '.join(proc.info['cmdline'] or []).replace('\\', '/').lower()
        if 'overnight-20260929/monitor/monitor.py' in command:
            running_helpers.append(proc.info['pid'])
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        continue
assert not running_helpers, running_helpers
renewals = [json.loads(line) for line in (BASE/'monitor/renewals.jsonl').read_text(encoding='utf-8').splitlines() if line]
assert renewals and all(row['status'] == 'ACQUIRED' for row in renewals)
times = [datetime.fromisoformat(row['observed_at']) for row in renewals]
max_gap = max((b-a).total_seconds() for a, b in zip(times, times[1:]))
assert max_gap <= 60, max_gap

now = datetime.now(timezone.utc).isoformat()
closing_text = (f'Supervision was released at `{release["updated_at"]}` after the monitor stopped and final peer review passed. '
                'No owner or monitor remains from this task. Do not rerun the completed September 28 source session. '
                '[Release receipt](C:/dev/ducketz/artifacts/analysis/overnight-20260929/supervision-release.json).')
pending_text = 'Supervision remains held for final peer review and administrative closure. Do not rerun the completed September 28 source session.'
body = summary_path.read_text(encoding='utf-8')
assert pending_text in body
summary_path.write_text(body.replace(pending_text, closing_text), encoding='utf-8')

notes = NATIVE/'operator-notes.md'
with notes.open('a', encoding='utf-8') as stream:
    stream.write(f'\n\n## {now} — verification and supervision closure\n\n'
        'Full 12-section completion audit and all five remaining commands passed. '
        'Bounded output/preservation/actuals checks and final report peer review passed. '
        'Known optional source exclusions, zero sizing qualifications and unavailable actual observations remain explicit. '
        f'Same-task monitor stopped; own supervision UUID {TOKEN} released natively at {release["updated_at"]}. '
        'No native rerun, trader start, control change or overnight order. '
        'Final evidence: C:/dev/ducketz/artifacts/analysis/overnight-20260929/final-verification.json.\n')

paths = [summary_path,
    *[BASE/'verification'/name for name in ('audit-runner.json', 'audit-runner-continuation.json',
        'completion-audit.json', 'yg-completion.json', 'provider-completion.json',
        'provider-advisory-resolution.json', 'model-review.json', 'model-metric-summary.json',
        'pricing-gate-review.json', 'loop-b-weekly-prefix-review.json', 'reviewed-isolated-code-drift.json', 'audit-findings.json')],
    *[BASE/'preflight'/name for name in ('final-output-review.json', 'final-preservation.json',
        'actuals-coverage-review.json', 'completion-summary-peer-review.json',
        'isolated-code-drift-review.json', 'native-reconciliation-verification.json')],
    BASE/'supervision-release.json', BASE/'monitor/monitor-closure.json', BASE/'monitor/renewals.jsonl',
    NATIVE/'receipt.json', NATIVE/'stage-report.json', notes]
result = {
    'closed_at': now, 'status': 'VERIFIED_WITH_COVERAGE_NOTES', 'native_run': str(NATIVE),
    'source_session': '2026-09-28', 'action_date': '2026-09-29', 'orders_placed': 0,
    'all_eight_native_stages_complete': True, 'full_audit_sections_passed': 12,
    'remaining_audit_commands_exit_zero': 5, 'native_restarts': 0, 'native_stage_repeats': 0,
    'errors': [], 'supervision': {'owner_token': TOKEN, 'monitor_renewals': len(renewals),
        'all_monitor_renewals_acquired': True, 'maximum_monitor_renewal_gap_seconds': max_gap,
        'first_monitor_renewal_at': times[0].isoformat(), 'last_monitor_renewal_at': times[-1].isoformat(),
        'monitor_stopped': True, 'matching_monitor_processes': running_helpers, 'release': release},
    'peer_review': {'status': peer['status'], 'reviewed_summary_sha256': peer['summary_sha256'],
        'closure_only_summary_edit_after_review': True},
    'evidence': {str(path.relative_to(BASE)) if path.is_relative_to(BASE) else str(path): bind(path) for path in paths},
    'next_action': 'Do not rerun completed September 28 source. Preserve 21:05 Pacific schedule and native calendar no-ops.'}
(BASE/'final-verification.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')

memory = Path('C:/Users/7980X/.codex/automations/loops-hourly-operations/memory.md')
with memory.open('a', encoding='utf-8') as stream:
    stream.write(f'''\n\n## September 29 Gameplan VERIFIED; supervision RELEASED\n\nCurrent run time: {now}. Native C:/DATASTORE/ml/overnight-runs/20260929T040727.779758Z completed all eight stages at September28 23:29:23 Pacific, preparing sourceSeptember28/actionSeptember29 before03:30 target and original04:00 deadline. No native repair/restart/stage repeat/trader start/control change/overnight order. Do not rerun this completed source; preserve21:05 schedule and native calendar no-ops.\n\nAll12 full-audit sections and five remaining audit commands passed by06:51:54Z. Full archive reconstruction verified988bound source files/950452425bytes,223second-minute partitions,16443370second rows and2842334exactOHLCVoverlaps, no synthetic or additionaltraining rows. Cohorts1h171897/4h42965/1d29712/1w5853 reproduced exactly;264current probabilities and assessment metrics reproduced with zero error. Eleven minute updates cost0; runtime recomputed0missingprefixes. All33OPRAscopes/cursors and66payload hashes verified. Optional Pricing99gates retain51files/17generations and non-Pricing fallbacks. CME/FMP advisory review resolved with existing exclusions preserved; no new provider failure.\n\nGameplan061339.593647Z has264forecasts/264stock-onlyintents and4PROMOTED directional groups under saved v2 tolerances; only4h/1dbeatbothbaselines. Sizing062327.185960Z all4FITTED/0qualified; strict returnMSEfailures, daily/weeklyBrier/logloss and weeklyadverseMSE retained. ThinTWST exactroute support disclosed, no concrete defect justified retraining. LoopB9models/341464fit samples/88applicableLIVE plus11weeklyd5NOT_APPLICABLE under Monday remaining-week contract.\n\nTradeplan062512.268894Z:264rows,154prices,46conditionalevents(24buy22sell),cash/shareconservationPASS. Literal cash18.14; conditionalendinglow/base/high1.51/219.90/440.41. Four syntheticplanninganchors395minutes:CROX166,IONQ10,PATH150,TWST69; originaltimestamps retained under currentdocumented240minutecontract. Actuals062842.614868Z:165evaluated/33maturemissing/66pending;137compared/17missing prices;84/165directioncorrect,6/137inrange. Missing endpoints have complete requestcoverage but no qualifyingobservedprice; no syntheticactuals. Cumulative062119.284555Z:5568total/4454evaluated/949maturemissing/165pending with each saveduniverse/target preserved.\n\nOnly production maintenance: nativeGET-onlypostcloseledgerreconciliation04:16:30Z confirmedCROX1h34/PATH1h7/TWST4h21brokerCANCELED0fills;131tests/685checksPASS, holdings/allocations unchanged. Final34output/37preservation/21actuals checksPASS. FinalreportpeerreviewPASS. One concurrent Hyperliquidpaperpolicy edit triggered auditguard before assessment inference; exactbaseline/diff/199moduleclosure reviewed, exacthash exception admitted, original268filebaseline and failedattempt preserved. Allremainingaudits passed without repeating fullreconstruction or nativepipeline.\n\nMonitor renewals{len(renewals)} allACQUIRED, maxgap{max_gap:.3f}s. Monitorstopped; ownUUID{TOKEN} RELEASED at{release['updated_at']}. No owner/helper remains fromthis task. Full evidence C:/dev/ducketz/artifacts/analysis/overnight-20260929/completion-summary.md and final-verification.json. Readableplan C:/DATASTORE/ml/gameplan-trade-plan-runs/20260929T062512.268894Z/Gameplan.md; results C:/DATASTORE/ml/gameplan-actuals-review-runs/20260929T062842.614868Z/Gameplan-results.md.\n''')
print(json.dumps({'status': result['status'], 'closed_at': now, 'report': str(BASE/'final-verification.json'),
                  'renewals': len(renewals), 'max_gap_seconds': max_gap, 'release': release['status']}))
