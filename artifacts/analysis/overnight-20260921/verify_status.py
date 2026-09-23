"""Read-only saved-state audit for the September 20 Pacific scheduled wake."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import psutil

ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
RUN = ROOT / 'ml/overnight-runs/20260919T040748.657031Z'


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


now = datetime.now(timezone.utc)
report, receipt = read(RUN / 'stage-report.json'), read(RUN / 'receipt.json')
assert sha(RUN / 'stage-report.json') == receipt['stage_report_checksum_sha256']
assert (RUN / 'stage-report.json').stat().st_size == receipt['stage_report_size']
for name, bound in receipt['logs'].items():
    assert sha(RUN / name) == bound['checksum_sha256']
    assert (RUN / name).stat().st_size == bound['size']
assert report['status'] == receipt['status'] == 'FAILED'
assert report['failed_stage'] == 'gameplan_trade_planning'
assert report['orders_placed'] == receipt['orders_placed'] == 0
assert report['deadline_at'] == '2026-09-21T11:00:00+00:00'

names = ('overnight-latest', 'nightly-gameplan-latest', 'stock-trader-model-latest',
         'gameplan-trade-plan-latest', 'gameplan-actuals-review-latest', 'gameplan-evaluation-latest')
pointers = {name: {'sha256': sha(ROOT / 'ml' / name / 'run.json'),
                   'value': read(ROOT / 'ml' / name / 'run.json')} for name in names}
previous = read(Path('C:/dev/ducketz/artifacts/analysis/overnight-20260920/verification.json'))
unchanged = {name: value['sha256'] == previous['production_pointers'][name]['sha256']
             for name, value in pointers.items()}
assert all(unchanged.values())

db_path = ROOT / 'state/independent-stock-trader/holdings.sqlite3'
with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as db:
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    reservations = [dict(row) for row in db.execute('''
        SELECT a.symbol, a.horizon, r.side, r.quantity, r.filled, r.status,
               r.last_evidence_at
        FROM reservations r JOIN allocations a ON a.id=r.allocation
        WHERE r.status IN ('RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL')
    ''')]
    latest_snapshot = db.execute('SELECT observed_at, ready, reasons FROM snapshots ORDER BY observed_at DESC LIMIT 1').fetchone()
    snapshot = dict(latest_snapshot) if latest_snapshot else None
    block_count = db.execute('SELECT COUNT(*) FROM blocks').fetchone()[0]

status = read(ROOT / 'state/independent-stock-trader/session-status.json')
lock = dict(line.split('=', 1) for line in
            (ROOT / 'locks/independent-stock-session.lock').read_text().splitlines() if '=' in line)
lock.pop('token', None)
lock['pid'] = int(lock['pid'])
process = psutil.Process(status['pid'])
parent = process.parent()
identities = [{'pid': p.pid, 'parent_pid': p.ppid(), 'created_at': p.create_time(),
               'executable': p.exe(), 'command': p.cmdline()} for p in (parent, process)]
for identity in identities:
    command = identity['command']
    assert '-m' in command and 'ml.gameplan_stock_trader' in command
    assert '--wait-for-open' in command and '--run-session' in command
    assert command[command.index('--sizing-policy') + 1] == 'gameplan-direction-current-market-v1'
    assert '--late-opening-date' not in command
assert status['status'] == 'SLEEPING_UNTIL_OPEN'
assert status['action_date'] == '2026-09-21'
assert status['wakes_at'] == '2026-09-21T04:00:00-07:00'
assert status['calls'] == status['orders_submitted'] == 0
age = (datetime.now(timezone.utc) - datetime.fromisoformat(status['heartbeat_at'])).total_seconds()
assert 0 <= age <= 60, age
assert lock['pid'] == status['pid']

result = {
    'observed_at': now.isoformat(),
    'status': 'UNCHANGED_PLANNING_BLOCKER_REQUIRES_TRADING_CODE_AUTHORIZATION',
    'native_run': str(RUN), 'native_status': report['status'],
    'completed_stages': [row['stage'] for row in report['stages'] if row['status'] == 'COMPLETE'],
    'failed_stage': report['failed_stage'], 'pending_stage': 'gameplan_actuals_review',
    'deadline_at': report['deadline_at'], 'pinned_gameplan': report['enrichment_gameplan'],
    'native_report_and_log_hashes_verified': True,
    'production_pointers': pointers, 'pointers_unchanged_since_previous_audit': unchanged,
    'open_ledger_reservations': reservations, 'last_persisted_ownership_snapshot': snapshot,
    'ownership_block_count': block_count,
    'broker_evidence_reused_from': 'C:/dev/ducketz/artifacts/analysis/overnight-20260920/broker-order-status-readonly.json',
    'fresh_broker_read_performed': False,
    'manual_worker_status': status, 'manual_worker_heartbeat_age_seconds': age,
    'manual_worker_processes': identities, 'manual_worker_lock': lock,
    'orders_submitted_by_audit': 0, 'ledger_mutations': 0,
    'pipeline_started_or_resumed': False,
    'repair_scope': 'C:/dev/ducketz/artifacts/analysis/overnight-20260920/recovery-review.md',
}
(OUT / 'verification.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps({key: result[key] for key in ('status', 'open_ledger_reservations',
    'pointers_unchanged_since_previous_audit', 'manual_worker_heartbeat_age_seconds')}))
