from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json

ROOT = Path(__file__).resolve().parent
RUN = Path('C:/DATASTORE/ml/overnight-runs/20260930T040723.733025Z')
def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))
def binding(path):
    data = path.read_bytes()
    return {'path': path.as_posix(), 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}

audit = read(ROOT/'verification/audit-findings.json')
runner = read(ROOT/'verification/audit-runner.json')
native = read(RUN/'stage-report.json')
assert audit['status'] == runner['status'] == 'VERIFIED_WITH_COVERAGE_NOTES'
assert len(runner['checks']) == 7 and all(c['exit_code'] == 0 for c in runner['checks'])
assert native['status'] == 'COMPLETE' and len(native['stages']) == 8
assert all(s['status'] == 'COMPLETE' and s['exit_code'] == 0 for s in native['stages'])
assert native['orders_placed'] == 0 and native['broker_orders_enabled'] is False
assert native['deadline_at'] == '2026-09-30T11:00:00+00:00'
assert native['enrichment_gameplan']['action_date'] == '2026-09-30'
assert read(ROOT/'preflight/final-preservation.json')['status'] == 'PASS'
assert all(read(ROOT/'preflight/final-preservation.json')['checks'].values())
assert read(ROOT/'bounded-outputs/bounded-output-summary.json')['status'] == 'PASS_WITH_EXPLICIT_COVERAGE_LIMITATIONS'
for ev in runner['evidence'].values():
    assert binding(Path(ev['path']))['sha256'] == ev['sha256']
for ev in read(ROOT/'bounded-outputs/bounded-output-summary.json')['evidence']:
    actual = binding(Path(ev['path']))
    assert actual['sha256'] == ev['sha256'] and actual['bytes'] == ev['size']

paths = [
    ROOT/'completion-summary.md', ROOT/'verification/audit-findings.json',
    ROOT/'verification/audit-findings.md', ROOT/'verification/audit-runner.json',
    ROOT/'verification/model-metric-summary.json', ROOT/'verification/completion-audit.json',
    ROOT/'verification/model-review.md', ROOT/'preflight/final-preservation.json',
    ROOT/'preflight/final-authorities-peer-review.json', ROOT/'preflight/cadence-isolation-peer-review.json',
    ROOT/'preflight/reconciliation-peer-review.json',
    ROOT/'preflight/reconciliation/native-reconciliation-verification.json',
    ROOT/'bounded-outputs/bounded-output-summary.json', ROOT/'bounded-outputs/review.md',
    ROOT/'provider-warning/final-triage.json', ROOT/'provider-warning/review.md',
    RUN/'stage-report.json', RUN/'receipt.json', RUN/'operator-notes.md',
]
for optional in ('supervision-release.json', 'monitor/monitor-closure.json', 'preflight/completion-summary-peer-review.json'):
    if (ROOT/optional).exists():
        paths.append(ROOT/optional)
result = {
    'reviewed_at': datetime.now(timezone.utc).isoformat(),
    'status': 'VERIFIED_WITH_EXPLICIT_QUALITY_AND_COVERAGE_LIMITATIONS',
    'source_session': '2026-09-29', 'action_date': '2026-09-30',
    'native_run': RUN.as_posix(), 'native_completed_at': native['completed_at'],
    'original_deadline': native['deadline_at'], 'native_stages_complete': 8,
    'full_audit_sections': 12, 'audit_commands_passed': 7,
    'orders_placed': 0, 'native_restarts': 0, 'supervision_token': '3e1f4906-750c-46d5-8e0b-5dfdad3b651a',
    'supervision': read(ROOT/'supervision-release.json') if (ROOT/'supervision-release.json').exists() else 'HELD_FOR_FINAL_REVIEW',
    'evidence': [binding(p) for p in paths],
    'limits': audit['limits'],
}
(ROOT/'final-verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':result['status'], 'evidence_files':len(paths), 'supervision':result['supervision']}))
