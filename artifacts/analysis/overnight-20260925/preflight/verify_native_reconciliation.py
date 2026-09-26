"""Independent local-only verification; never fetches or reconciles."""
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import sqlite3
import sys

sys.dont_write_bytecode = True
sys.path.insert(0,'C:/dev/ducketz')
from ml.gameplan_trade_snapshot import _ownership
from ml.stock_trader.horizon_ledger import HorizonLedger
from ml.stock_trader.contracts import canonical_sha256

ROOT=Path('C:/DATASTORE')
OUT=Path(__file__).resolve().parent
DB=ROOT/'state/independent-stock-trader/holdings.sqlite3'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def tables(path):
    connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    try:
        names=[row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        state={name:[dict(row) for row in connection.execute('SELECT * FROM "'+name+'" ORDER BY rowid')] for name in names}
        snapshot=HorizonLedger._snapshot(connection)
        return state,snapshot
    finally:
        connection.close()


def main():
    receipt=read(OUT/'native-reconciliation.json')
    plan=read(OUT/'reconciliation-plan.json')
    before,before_native=tables(Path(receipt['backup_path']))
    after,after_native=tables(DB)
    rehearsed,rehearsed_native=tables(OUT/'holdings-native-rehearsal.sqlite3')
    expected={row['reservation_id']:row for row in receipt['order_evidence']}
    pending_before={row.reservation_id:row for row in before_native.reservations if row.status in {'RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL'}}
    old_res={row['id']:row for row in before['reservations']}
    new_res={row['id']:row for row in after['reservations']}
    checks={
        'receipt_success':receipt['status']=='RECONCILED' and receipt['result']['ready'] is True,
        'backup_hash':sha(Path(receipt['backup_path']))==receipt['backup_sha256'],
        'before_logical_hash':canonical_sha256(before)==receipt['before_logical_sha256'],
        'after_logical_hash':canonical_sha256(after)==receipt['after_logical_sha256'],
        'rehearsal_logical_hash':canonical_sha256(rehearsed)==receipt['rehearsal_logical_sha256'],
        'production_equals_native_rehearsal':after==rehearsed and after_native==rehearsed_native,
        'exact_pending_set':set(expected)==set(pending_before)=={row['reservation_id'] for row in plan['pending']},
        'no_reservations_added_or_removed':set(old_res)==set(new_res),
        'all_old_fills_preserved_no_new_fills':before['fills']==after['fills'],
        'assignments_preserved':before.get('inventory_assignments')==after.get('inventory_assignments'),
        'assignment_releases_preserved':before.get('inventory_assignment_releases')==after.get('inventory_assignment_releases'),
        'no_pending_reservations':not any(row.reserved_quantity for row in after_native.reservations),
        'no_persistent_blocks':not after_native.persistent_blocks,
        'owned_allocations_unchanged':{row.allocation_id:row.filled_shares for row in before_native.allocations}=={row.allocation_id:row.filled_shares for row in after_native.allocations},
        'no_order_actions':all(receipt[key]==0 for key in ['orders_submitted','orders_cancelled','orders_replaced']),
        'zero_execution_budgets':receipt['execution_budgets']=='ZERO' and all(Decimal(str(value))==0 for value in json.loads(after['snapshots'][-1]['payload'])['symbol_budgets'].values()),
        'guarded_artifacts_preserved':all(sha(Path(path))==digest for path,digest in plan['guarded_artifact_sha256'].items()),
    }
    summary=[]
    for identity,old in old_res.items():
        wanted=dict(old)
        if identity in expected:
            item=expected[identity]
            wanted.update(status=item['status'],filled=item['cumulative_filled_quantity'],
                          broker_order=item['broker_order_id'],last_evidence_at=item['observed_at'])
            prior=pending_before[identity]
            summary.append({'symbol':prior.symbol,'horizon':prior.horizon,'side':prior.side,
                'quantity':prior.quantity,'old_status':prior.status,'broker_status':item['broker_status'],
                'new_status':item['status'],'confirmed_filled':item['cumulative_filled_quantity'],
                'remaining_reservation':new_res[identity]['quantity']-new_res[identity]['filled'] if new_res[identity]['status'] in {'WORKING','PARTIAL'} else 0})
        checks['exact_reservation_change_'+identity[:12]]=new_res[identity]==wanted
    claims=sorted(str(p) for folder in ['entry-slots','quote-recovery-slots'] for p in (ROOT/'state/independent-stock-trader'/folder).rglob('*') if p.is_file())
    checks['claim_set_preserved']=claims==plan['claim_paths']
    portfolio=receipt['portfolio']
    checks['portfolio_follows_order_evidence']=all(item['observed_at']<portfolio['observed_at'] for item in expected.values())
    checks['newer_coherent_holdings_unchanged']=({s:Decimal(str(v)) for s,v in portfolio['held_shares'].items()}=={s:Decimal(str(v)) for s,v in json.loads(before['snapshots'][-1]['payload'])['held_shares'].items()})
    ownership=_ownership(ROOT,plan['symbols'],plan['account_fingerprint'],portfolio['held_shares'],datetime.now(timezone.utc).isoformat())
    checks['native_readonly_ownership_safe']=ownership['safe_for_planning'] is True
    tests=OUT.parent/'native-reconciliation-tests.txt'
    checks['native_tests86_recorded']='86 passed' in tests.read_text()
    result={'verified_at':datetime.now(timezone.utc).isoformat(),'status':'PASS' if all(checks.values()) else 'FAIL',
            'scope':'INDEPENDENT_LOCAL_READ_ONLY_NO_PROVIDER_CALLS_NO_RECONCILIATION','checks':checks,
            'reservation_results':summary,'saved_broker_portfolio_at':portfolio['observed_at'],
            'planning_ownership':ownership,'current_broker_state':'NOT_RECAPTURED',
            'source_sha256':{str(p):sha(p) for p in [OUT/'native-reconciliation.json',OUT/'reconciliation-plan.json',tests]},
            'native_test_record':str(tests),'orders_submitted':0,'orders_cancelled':0,'orders_replaced':0}
    (OUT/'native-reconciliation-verification.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['status'],'checks':len(checks),'failed':[key for key,value in checks.items() if not value],
                      'reservation_results':summary,'safe_for_planning':ownership['safe_for_planning']}))
    if result['status']!='PASS':
        raise SystemExit(1)


if __name__=='__main__':
    main()
