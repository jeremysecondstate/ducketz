"""Read new overnight logs and report compact progress; never control workers."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

run = Path('C:/DATASTORE/ml/overnight-runs/20260916T061451.489725Z')
state_file = Path(__file__).with_name('resume-monitor-offsets.json')
state = json.loads(state_file.read_text()) if state_file.exists() else {}
report = json.loads((run / 'stage-report.json').read_text())
result = {k: report.get(k) for k in ('status', 'current_stage', 'heartbeat_at', 'stage_health', 'reported_issue_count', 'recent_issues', 'cpu_seconds', 'io_bytes', 'log_bytes', 'deadline_at')}
result['observed_at'] = datetime.now(timezone.utc).isoformat()
result['new_logs'] = {}
for path in sorted(run.glob('*.log')):
    offset = state.get(path.name, 0)
    with path.open('rb') as handle:
        handle.seek(offset)
        raw = handle.read()
        state[path.name] = handle.tell()
    lines = raw.decode('utf-8', errors='replace').splitlines()
    if lines:
        issues = [line for line in lines if re.search(r'error|exception|traceback|warning|fail|unavailable|denied', line, re.I)]
        result['new_logs'][path.name] = {'new_lines': len(lines), 'issues': issues[-12:], 'tail': lines[-3:]}
health = run / 'health.jsonl'
if health.exists():
    with health.open('rb') as handle:
        handle.seek(state.get('health.jsonl', 0))
        lines = handle.read().decode('utf-8', errors='replace').splitlines()
        state['health.jsonl'] = handle.tell()
    records = [json.loads(line) for line in lines if line.strip()]
    result['new_health_records'] = len(records)
    result['health_issues'] = [r for r in records if r.get('recent_issues') or r.get('stage_health') not in ('RUNNING', 'COMPLETE', None)]
state_file.write_text(json.dumps(state, indent=2))
print(json.dumps(result, indent=2))


