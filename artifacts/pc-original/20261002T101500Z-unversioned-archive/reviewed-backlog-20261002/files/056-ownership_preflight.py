"""Local-only coherent read-only post-close ownership inspection.

No broker/provider calls, ledger initialization or writes, lock operations,
supervision operations, or trader/order actions. Output is sanitized evidence.
"""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json
import sqlite3
import subprocess
import sys

sys.dont_write_bytecode = True
REPO = Path('<LOCAL_CHECKOUT>')
ROOT = Path('<LOCAL_DATASTORE>')
OUT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    from ml.gameplan_trade_snapshot import _ownership
    db_path = ROOT/'state/independent-stock-trader/holdings.sqlite3'
    status_path = ROOT/'state/independent-stock-trader/session-status.json'
    symbols = [s.strip() for s in (REPO/'datafetching/watchlist.txt').read_text().splitlines()
               if s.strip() and not s.lstrip().startswith('#')]
    now = datetime.now(timezone.utc).isoformat()
    before_hashes = {str(p): sha(p) for p in [db_path, status_path] if p.exists()}
    sidecars = [Path(str(db_path)+suffix) for suffix in ['-wal', '-shm']]
    if sidecars[0].exists() and not sidecars[1].exists():
        raise RuntimeError('Read-only WAL state unavailable')
    sidecars_before = {str(p): sha(p) for p in sidecars if p.exists()}
    status = json.loads(status_path.read_text(encoding='utf-8-sig'))
    with sqlite3.connect(db_path.as_uri()+'?mode=ro', uri=True, timeout=2) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        table_state = {name: [dict(row) for row in connection.execute('SELECT * FROM "'+name+'" ORDER BY rowid')]
                       for name in tables}
        metadata = dict(connection.execute('SELECT key,value FROM metadata'))
        saved = dict(connection.execute('SELECT observed_at,ready,reasons,owned,payload FROM snapshots ORDER BY observed_at DESC LIMIT 1').fetchone())
        payload = json.loads(saved.pop('payload'))
        saved['reasons'] = json.loads(saved['reasons'])
        saved['owned'] = json.loads(saved['owned'])
        counts = [dict(row) for row in connection.execute('SELECT status,count(*) AS count FROM reservations GROUP BY status')]
        pending = [dict(row) for row in connection.execute("SELECT a.symbol,a.horizon,r.side,r.quantity,r.filled,(r.quantity-r.filled) AS remaining,r.status,r.last_evidence_at,a.start,a.end FROM reservations r JOIN allocations a ON a.id=r.allocation WHERE r.status IN ('RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL') ORDER BY a.symbol,a.horizon")]
        blocks = [dict(row) for row in connection.execute('SELECT symbol,reason FROM blocks ORDER BY symbol')]
        fallback_baselines = [] if 'fallback_days' not in tables else [dict(row) for row in connection.execute('SELECT action_date FROM fallback_days ORDER BY action_date')]
        logical_hash = hashlib.sha256(json.dumps(table_state, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    ownership = _ownership(ROOT, symbols, metadata['account'], payload['held_shares'], now)
    command = "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe','cmd.exe') -and $_.CommandLine -match 'ml\\.(gameplan_stock_trader|independent_stock_trader)|Start-Gameplan-Trader' } | Measure-Object | Select-Object -ExpandProperty Count"
    process_count = int(subprocess.run(['powershell.exe','-NoProfile','-Command',command], check=True, text=True, capture_output=True).stdout.strip())
    after_hashes = {str(p):sha(p) for p in [db_path,status_path] if p.exists()}
    sidecars_after = {str(p): sha(p) for p in sidecars if p.exists()}
    result = {
        'reviewed_at':now,
        'scope':'LOCAL_READ_ONLY_PRECHECK_NO_PROVIDER_OR_BROKER_CALLS_NO_PRODUCTION_WRITES',
        'native_overnight_run':'20261001T040644.667536Z',
        'status':'LOCAL_PLANNING_RECONCILIATION_BLOCKER' if not ownership['safe_for_planning'] else 'NO_LOCAL_RECONCILIATION_BLOCKER',
        'symbols':symbols,
        'trader':{key:status.get(key) for key in ['status','action_date','updated_at','finished_at']},
        'matching_trader_process_count':process_count,
        'native_session_lock_exists':(ROOT/'locks/independent-stock-session.lock').exists(),
        'native_cycle_lock_exists':(ROOT/'locks/stock-trader-hourly.lock').exists(),
        'latest_saved_ownership_reconciliation':saved,
        'saved_held_shares':payload['held_shares'],
        'reservation_status_counts':counts,
        'pending_reservations':pending,
        'persistent_blocks':blocks,
        'planning_check_using_saved_portfolio':{key:ownership.get(key) for key in ['status','safe_for_planning','reason_codes','blocked_symbols','account_matches','current_broker_reconciliation_performed']},
        'fallback_baseline_dates':fallback_baselines,
        'ledger_table_row_counts':{key:len(value) for key,value in table_state.items()},
        'ledger_logical_sha256':logical_hash,
        'source_sha256':before_hashes,
        'same_source_hashes_after':before_hashes==after_hashes,
        'same_sidecar_set_and_hashes_after':sidecars_before==sidecars_after,
        'broker_status_now':'UNKNOWN_NO_BROKER_CALLS',
        'limitation':'The saved portfolio is local historical evidence. Its ownership check does not establish current broker order status or fresh planning readiness.',
        'next_step':'Root should use the documented supervised locked native reconciliation path with exact matching-account terminal order history followed by a newer coherent portfolio snapshot. Do not infer cancellation from a locally working reservation or disappearance from open orders.' if pending else 'Native planning must capture its normal fresh account and ownership snapshot.'
    }
    if before_hashes!=after_hashes or sidecars_before!=sidecars_after:
        result['status']='SOURCE_CHANGED_DURING_READ_RECHECK_REQUIRED'
    path=OUT/'ownership-preflight.json'
    path.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:result[key] for key in ['reviewed_at','status','trader','matching_trader_process_count','pending_reservations','persistent_blocks','planning_check_using_saved_portfolio','same_source_hashes_after','same_sidecar_set_and_hashes_after']},indent=2))


if __name__=='__main__':
    main()
