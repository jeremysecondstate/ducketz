"""Read-only native-run monitor and same Scheduled-task lease renewer.

Stops on STOP or when the supervising agent stops refreshing active.txt.
Never releases the claim, changes the native run, or invokes trading/provider code.
"""
from pathlib import Path
import datetime as dt
import json
import re
import subprocess
import sys
import time

BASE = Path(__file__).resolve().parent
ROOT = Path('C:/dev/ducketz')
PYTHON = ROOT / '.venv/Scripts/python.exe'
TOKEN = '5de5b56b-8488-49a3-9606-9d615a47a2de'
offsets = {}
last_metrics = {}
pattern = re.compile(r'traceback|\b(?:error|failed|failure|warning)\b|non.finite|access.denied|permission.denied|license.denied|out.of.memory', re.I)

def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        return {'monitor_read_error': str(exc)}

def emit(value):
    print(json.dumps(value, ensure_ascii=True), flush=True)

def append(path, value):
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=True) + '\n')

def incremental(path):
    try:
        start = offsets.get(str(path), 0)
        with path.open('rb') as stream:
            if path.stat().st_size < start:
                start = 0
            stream.seek(start)
            new = stream.read()
            offsets[str(path)] = stream.tell()
        return new.decode('utf-8', errors='replace').splitlines()
    except OSError:
        return []

while True:
    if (BASE / 'STOP').exists():
        emit({'monitor_status': 'STOP_REQUESTED', 'claim_released': False})
        break
    active = BASE / 'active.txt'
    if not active.exists() or time.time() - active.stat().st_mtime > 105:
        emit({'monitor_status': 'AGENT_KEEPALIVE_EXPIRED', 'claim_released': False})
        break
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    renewal = subprocess.run([str(PYTHON), '-m', 'ml.overnight_runtime', '--datastore-target', 'pc', '--claim-supervision', TOKEN], cwd=ROOT, capture_output=True, text=True, timeout=25)
    try:
        lease = json.loads(renewal.stdout)
    except ValueError:
        lease = {'status': 'RENEWAL_PARSE_ERROR', 'returncode': renewal.returncode, 'stderr': renewal.stderr[-2000:]}
    append(BASE / 'renewals.jsonl', {'observed_at': now, **lease})
    if lease.get('status') != 'ACQUIRED':
        emit({'monitor_status': 'RENEWAL_REFUSED_STOPPING', 'lease': lease})
        break
    run = Path((BASE / 'target-run.txt').read_text(encoding='utf-8-sig').strip())
    report = read_json(run / 'stage-report.json')
    health = incremental(run / 'health.jsonl')
    log_path = Path(report.get('current_log_path', str(run / 'missing.log')))
    lines = incremental(log_path)
    issue_lines = [line for line in lines if pattern.search(line)]
    previous = last_metrics.get(str(run), {})
    record = {
        'observed_at': now, 'run': str(run), 'lease_status': lease.get('status'),
        'status': report.get('status'), 'stage': report.get('current_stage'),
        'heartbeat_at': report.get('heartbeat_at'), 'stage_health': report.get('stage_health'),
        'cpu_seconds': report.get('cpu_seconds'), 'io_bytes': report.get('io_bytes'),
        'log_bytes': report.get('log_bytes'), 'memory_bytes': report.get('memory_bytes'),
        'orders_placed': report.get('orders_placed'), 'recent_issues': report.get('recent_issues'),
        'new_log_lines': len(lines), 'issue_lines': issue_lines,
        'log_tail': lines[-8:], 'new_health_rows': len(health),
        'completed_stages': report.get('stages'),
    }
    for key in ('cpu_seconds', 'io_bytes', 'log_bytes'):
        if isinstance(report.get(key), (int, float)) and isinstance(previous.get(key), (int, float)):
            record[key + '_delta'] = report[key] - previous[key]
    last_metrics[str(run)] = report
    append(BASE / 'observations.jsonl', record)
    if lines:
        append(BASE / 'incremental-logs.jsonl', {'observed_at': now, 'log': str(log_path), 'lines': lines})
    if health:
        append(BASE / 'incremental-health.jsonl', {'observed_at': now, 'run': str(run), 'rows': health})
    (BASE / 'latest.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    emit(record)
    for _ in range(35):
        if (BASE / 'STOP').exists():
            break
        time.sleep(1)
