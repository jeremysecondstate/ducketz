"""Read new overnight logs and report compact progress; never control workers."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

run = Path('C:/DATASTORE/ml/overnight-runs/20260924T044352.904210Z')
state_file = Path(__file__).with_name('monitor-offsets.json')
state = json.loads(state_file.read_text()) if state_file.exists() else {}
report = json.loads((run / 'stage-report.json').read_text())
result = {k: report.get(k) for k in ('status', 'current_stage', 'heartbeat_at', 'stage_health', 'reported_issue_count', 'recent_issues', 'cpu_seconds', 'io_bytes', 'log_bytes', 'deadline_at')}
result['observed_at'] = datetime.now(timezone.utc).isoformat()
previous = state.get('_metrics', {})
same_stage = previous.get('current_stage') == result.get('current_stage')
result['deltas'] = {key: result.get(key, 0) - previous.get(key, 0) for key in ('cpu_seconds', 'io_bytes', 'log_bytes') if same_stage and isinstance(result.get(key), (int, float)) and isinstance(previous.get(key), (int, float))}
state['_metrics'] = {key: result.get(key) for key in ('current_stage', 'cpu_seconds', 'io_bytes', 'log_bytes')}
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
with Path(__file__).with_name('monitor-observations.jsonl').open('a', encoding='utf-8') as history:
    history.write(json.dumps(result) + '\n')
compact = {k: result[k] for k in ('status', 'current_stage', 'observed_at', 'heartbeat_at', 'cpu_seconds', 'io_bytes', 'new_health_records')}
compact['new_logs'] = {name: {'new_lines': log['new_lines'], 'issues': log['issues'], 'last_line': log['tail'][-1][:350]} for name, log in result['new_logs'].items()}
compact['issues'] = result.get('recent_issues', []) + result.get('health_issues', [])
compact['deltas'] = result['deltas']
def summarize_issue(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return value[:600]
    if isinstance(value, dict):
        return {k: value[k] for k in ('training_event', 'fit', 'message', 'failed_checks', 'stage', 'stage_health', 'reported_issue_count') if k in value}
    return value
compact['issues'] = [summarize_issue(v) for v in compact['issues']]
for log in compact['new_logs'].values():
    log['issues'] = [summarize_issue(v) for v in log['issues'] if '"warnings": 0' not in v]
opra_text = (run / "loop_a_close_fetch.log").read_text(encoding="utf-8", errors="replace")
compact["opra_completed_scopes"] = len(re.findall(r"OPRA symbol/schema history:.*status=COMPLETE", opra_text))
print(json.dumps(compact))











