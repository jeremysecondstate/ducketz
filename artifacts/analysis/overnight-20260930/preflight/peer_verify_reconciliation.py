"""Independent peer audit of already-completed maintenance; read-only local data."""
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import sqlite3
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,'C:/dev/ducketz')
sys.path.insert(0,str(Path(__file__).resolve().parent))
from ml.stock_trader.contracts import canonical_sha256
from ml.gameplan_trade_snapshot import _ownership
from capture_preservation import read,sha,digest

OUT=Path(__file__).resolve().parent
MAINT=OUT/'reconciliation'
ROOT=Path('C:/DATASTORE')

def tables(path):
    conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=3)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        names=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name:[dict(r) for r in conn.execute('SELECT * FROM "'+name+'" ORDER BY rowid')] for name in names}
    finally:
        conn.close()

def main():
    initial=read(OUT/'initial-baseline.json')
    post=read(OUT/'post-reconciliation-baseline.json')
    receipt=read(MAINT/'native-reconciliation.json')
    plan=read(MAINT/'reconciliation-plan.json')
    native_check=read(MAINT/'native-reconciliation-verification.json')
    before=tables(Path(receipt['backup_path']))
    after=tables(ROOT/'state/independent-stock-trader/holdings.sqlite3')
    rehearsal=tables(MAINT/'holdings-native-rehearsal.sqlite3')
    evidence=receipt['order_evidence']
    item=evidence[0]
    pending=plan['pending'][0]
    snapshot=after['snapshots'][-1]
    old_snap=json.loads(before['snapshots'][-1]['payload'])
    new_snap=json.loads(snapshot['payload'])
    checks={
        'one_exact_terminal_order':len(evidence)==len(plan['pending'])==1 and pending['symbol']=='CROX' and pending['horizon']=='4h' and pending['side']=='BUY' and pending['quantity']==1 and item['reservation_id']==pending['reservation_id']=='d4ceb6a1afda4d215d445b362cbf21a4414f46a0ef94a8e5bf9771b26662c6ab' and item['broker_order_id']==pending['broker_order_id']=='1008110897619',
        'native_expired_not_operator_cancelled':item['broker_status']=='EXPIRED' and item['status']=='CANCELLED' and item['remaining_quantity']==item['cumulative_filled_quantity']==0 and not item['fills'],
        'matching_account':item['account_fingerprint']==plan['account_fingerprint']==new_snap['account_fingerprint'],
        'receipt_success':receipt['status']=='RECONCILED' and receipt['result']['ready'] is True and not receipt['result']['reasons'],
        'native_verifier_success':native_check['status']=='PASS' and all(native_check['checks'].values()),
        'backup_physical_checksum':sha(Path(receipt['backup_path']))==receipt['backup_sha256'],
        'plan_binding':sha(MAINT/'reconciliation-plan.json')==receipt['plan_sha256'],
        'initial_baseline_matches_exact_backup':all(digest(rows)==initial['ledger_table_sha256'][name] and len(rows)==initial['ledger_table_counts'][name] for name,rows in before.items()),
        'before_native_hash':canonical_sha256(before)==receipt['before_logical_sha256']==plan['ledger_logical_sha256'],
        'after_native_hash':canonical_sha256(after)==receipt['after_logical_sha256'],
        'post_baseline_matches_current':all(digest(rows)==post['ledger_table_sha256'][name] and len(rows)==post['ledger_table_counts'][name] for name,rows in after.items()),
        'production_equals_copy_rehearsal':after==rehearsal and canonical_sha256(rehearsal)==receipt['rehearsal_logical_sha256'],
        'no_new_fills_or_assignments':before['fills']==after['fills'] and before['inventory_assignments']==after['inventory_assignments'],
        'only_expected_table_set':set(before)==set(after),
        'prior_snapshots_preserved':after['snapshots'][:-1]==before['snapshots'],
        'one_ready_snapshot_added':len(after['snapshots'])==len(before['snapshots'])+1 and snapshot['id']==receipt['result']['snapshot_id'] and snapshot['ready']==1 and not json.loads(snapshot['reasons']),
        'unchanged_held_shares':new_snap['held_shares']==old_snap['held_shares'] and {k:Decimal(str(v)) for k,v in new_snap['held_shares'].items()}=={k:Decimal(str(v)) for k,v in receipt['portfolio']['held_shares'].items()},
        'newer_portfolio_follows_terminal_order':datetime.fromisoformat(receipt['portfolio']['observed_at'])>datetime.fromisoformat(item['observed_at']),
        'zero_execution_budgets':receipt['execution_budgets']=='ZERO' and all(float(v)==0 for v in new_snap['symbol_budgets'].values()),
        'zero_order_actions':all(receipt[k]==0 for k in ['orders_submitted','orders_cancelled','orders_replaced']),
        'broker_get_only':receipt['broker_data_http_methods']==['GET'],
        'no_current_broker_working_orders':receipt['portfolio']['working_order_count']==0 and not any(receipt['portfolio']['pending_buy_shares'].values()) and not any(receipt['portfolio']['pending_sell_shares'].values()),
        'all_source_guards_preserved':all(sha(path)==value for path,value in plan['source_artifact_sha256'].items()),
        'all_control_claim_guards_preserved':all(sha(path)==value for path,value in plan['guarded_artifact_sha256'].items()),
    }
    expected_res=[]
    for row in before['reservations']:
        wanted=dict(row)
        if row['id']==item['reservation_id']:
            wanted.update(status='CANCELLED',last_evidence_at=item['observed_at'])
        expected_res.append(wanted)
    checks['exact_reservation_changes']=expected_res==after['reservations']
    expected_alloc=[]
    for row in before['allocations']:
        wanted=dict(row)
        if row['id']==pending['allocation_id']:
            wanted['status']='CLOSED'
        expected_alloc.append(wanted)
    checks['only_empty_crox_allocation_closed']=expected_alloc==after['allocations']
    checks['no_pending_ledger_reservations']=not any(r['status'] in ['RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL'] for r in after['reservations'])
    checks['no_persistent_blocks']=not after['blocks']
    old_events={r['id']:r for r in before['evidence']}
    new_events={r['id']:r for r in after['evidence']}
    checks['old_audit_events_preserved']=all(new_events.get(i)==r for i,r in old_events.items())
    checks['exact_two_audit_events_added']=set(new_events)-set(old_events)=={item['evidence_id'],'portfolio:'+snapshot['id']}
    for name in ['metadata','blocks','cancellations']:
        checks[name+'_preserved']=before[name]==after[name]
    for key in ['control_hashes','automation_hashes','claim_hashes','windows_task_configuration','watchlist_sha256','terminal_session_sha256','source_hashes','last_stock_decision_run']:
        checks[key+'_preserved']=initial[key]==post[key]
    ownership=_ownership(ROOT,plan['symbols'],plan['account_fingerprint'],receipt['portfolio']['held_shares'],datetime.now(timezone.utc).isoformat())
    checks['native_readonly_ownership_safe']=ownership['safe_for_planning'] is True and not ownership['reason_codes']
    result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'status':'PASS' if all(checks.values()) else 'FAIL',
        'scope':'INDEPENDENT_PEER_LOCAL_READ_ONLY_NO_BROKER_CALLS_OR_PRODUCTION_WRITES',
        'checks':checks,'issues':[k for k,v in checks.items() if not v],
        'verified_terminal_order':item,'native_result':receipt['result'],'planning_ownership':ownership,
        'table_count_deltas':{k:len(after[k])-len(before[k]) for k in before},
        'observed_change':'Native EXPIRED BUY1 released with zero fills; one empty CROX4h allocation closed; one ready snapshot and exactly two audit events appended.',
        'source_sha256':{str(p):sha(p) for p in [OUT/'initial-baseline.json',OUT/'post-reconciliation-baseline.json',MAINT/'native-reconciliation.json',MAINT/'native-reconciliation-verification.json',MAINT/'reconciliation-plan.json']},
        'broker_calls':0,'provider_calls':0,'order_actions':0,'production_writes':0}
    (OUT/'reconciliation-peer-review.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'status':result['status'],'checks':len(checks),'issues':result['issues'],'table_count_deltas':result['table_count_deltas']}))
    if result['status']!='PASS':raise SystemExit(1)

if __name__=='__main__':main()
