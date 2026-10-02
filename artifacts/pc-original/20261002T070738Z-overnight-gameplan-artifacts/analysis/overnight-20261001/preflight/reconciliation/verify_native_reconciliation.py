"""Independent local-only verification; never fetches or reconciles."""
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
import sqlite3
import sys
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True
sys.path.insert(0,'C:/dev/ducketz')
from ml.gameplan_trade_snapshot import _ownership
from ml.stock_trader.horizon_ledger import HorizonLedger, _identity
from ml.stock_trader.contracts import canonical_sha256, utc

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
    old_fills={row['id']:row for row in before['fills']}
    wanted_fills=dict(old_fills)
    assignments={row['id']:row for row in before.get('inventory_assignments',[])}
    old_releases={row['id']:row for row in before.get('inventory_assignment_releases',[])}
    wanted_releases=dict(old_releases)
    latest_ready=sorted((row for row in before['snapshots'] if row['ready']==1),key=lambda row:row['observed_at'])[-1]
    allocation_deltas={row.allocation_id:0 for row in before_native.allocations}
    expected_held={s:Decimal(str(v)) for s,v in json.loads(before['snapshots'][-1]['payload'])['held_shares'].items()}
    for identity,item in expected.items():
        old=old_res[identity]
        delta=(item['cumulative_filled_quantity']-old['filled'])*(1 if old['side']=='BUY' else -1)
        allocation_deltas[old['allocation']]+=delta
        expected_held[pending_before[identity].symbol]+=delta
        for fill in item['fills']:
            row={'id':fill['fill_id'],'reservation':identity,'quantity':fill['quantity'],
                 'price':str(Decimal(str(fill['price']))),'executed_at':utc(fill['executed_at']).isoformat()}
            if fill['fill_id'] in old_fills and old_fills[fill['fill_id']]!=row:
                raise RuntimeError('OLD_FILL_CHANGED')
            wanted_fills[fill['fill_id']]=row
        assignment_id=_identity(['gameplan-observed-opening-stock',old['idempotency_key']])
        assignment=assignments.get(assignment_id)
        if old['side']=='SELL' and assignment is not None:
            released=assignment['quantity']-max(0,item['cumulative_filled_quantity']-(old['quantity']-assignment['quantity']))
            if released>0:
                release_id=_identity(['gameplan-unsold-opening-stock-release',identity])
                wanted_releases.setdefault(release_id,{'id':release_id,'assignment':assignment_id,
                    'reservation':identity,'snapshot_id':latest_ready['id'],'quantity':released,'observed_at':item['observed_at']})
    for identity,row in wanted_releases.items():
        if identity not in old_releases:
            allocation_deltas[assignments[row['assignment']]['allocation']]-=row['quantity']
    checks={
        'receipt_success':receipt['status']=='RECONCILED' and receipt['result']['ready'] is True,
        'plan_binding':sha(OUT/'reconciliation-plan.json')==receipt['plan_sha256'],
        'source_binding':receipt['reviewed_source_sha256']==plan['source_artifact_sha256'] and all(sha(Path(path))==digest for path,digest in plan['source_artifact_sha256'].items()),
        'backup_hash':sha(Path(receipt['backup_path']))==receipt['backup_sha256'],
        'before_logical_hash':canonical_sha256(before)==receipt['before_logical_sha256']==plan['ledger_logical_sha256'],
        'after_logical_hash':canonical_sha256(after)==receipt['after_logical_sha256'],
        'rehearsal_logical_hash':canonical_sha256(rehearsed)==receipt['rehearsal_logical_sha256'],
        'production_equals_native_rehearsal':after==rehearsed and after_native==rehearsed_native,
        'exact_pending_set':set(expected)==set(pending_before)=={row['reservation_id'] for row in plan['pending']},
        'no_reservations_added_or_removed':set(old_res)==set(new_res),
        'all_old_fills_preserved_exact_new_fills':wanted_fills=={row['id']:row for row in after['fills']},
        'frozen_fallback_baseline_preserved':before.get('fallback_days',[])==after.get('fallback_days',[]),
        'fallback_reservation_metadata_preserved':all(new_res[key]['request']==row['request'] for key,row in old_res.items()),
        'assignments_preserved':before.get('inventory_assignments')==after.get('inventory_assignments'),
        'exact_native_assignment_releases':wanted_releases=={row['id']:row for row in after.get('inventory_assignment_releases',[])},
        'no_pending_reservations':not any(row.reserved_quantity or row.status in {'RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL'} for row in after_native.reservations),
        'no_persistent_blocks':not after_native.persistent_blocks,
        'owned_allocations_exact_evidence_delta':{row.allocation_id:row.filled_shares+allocation_deltas[row.allocation_id] for row in before_native.allocations}=={row.allocation_id:row.filled_shares for row in after_native.allocations},
        'no_order_actions':all(receipt[key]==0 for key in ['orders_submitted','orders_cancelled','orders_replaced']),
        'zero_execution_budgets':receipt['execution_budgets']=='ZERO' and all(Decimal(str(value))==0 for value in json.loads(after['snapshots'][-1]['payload'])['symbol_budgets'].values()),
        'guarded_artifacts_preserved':all(sha(Path(path))==digest for path,digest in plan['guarded_artifact_sha256'].items()),
        'broker_data_get_only':receipt['broker_data_http_methods']==['GET'],
        'terminal_order_evidence':all(item['status'] in {'FILLED','CANCELLED','REJECTED'} and item['remaining_quantity']==0 for item in expected.values()),
        'exact_order_identity_and_fills':all(item['account_fingerprint']==plan['account_fingerprint'] and item['broker_order_id']==pending_before[identity].broker_order_id and item['order_quantity']==pending_before[identity].quantity and pending_before[identity].filled_quantity<=item['cumulative_filled_quantity']<=item['order_quantity'] and sum(f['quantity'] for f in item['fills'])==item['cumulative_filled_quantity'] for identity,item in expected.items()),
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
    old_allocations={row.allocation_id:row for row in before_native.allocations}
    new_allocations={row.allocation_id:row for row in after_native.allocations}
    checks['allocation_identities_preserved']=set(old_allocations)==set(new_allocations)
    for identity,old in old_allocations.items():
        new=new_allocations[identity]
        old_data,new_data=asdict(old),asdict(new)
        for key in ['status','filled_shares','reserved_buy_shares','reserved_sell_shares']:
            old_data.pop(key); new_data.pop(key)
        checks['allocation_windows_preserved_'+identity[:12]]=old_data==new_data and new.reserved_buy_shares==0 and new.reserved_sell_shares==0 and (old.status==new.status or (old.status=='ACTIVE' and new.status=='CLOSED' and new.filled_shares==0))
    for table in before:
        if table not in {'reservations','fills','allocations','snapshots','evidence','inventory_assignment_releases'}:
            checks['preserved_table_'+table]=before[table]==after[table]
    checks['prior_snapshots_preserved']=after['snapshots'][:-1]==before['snapshots'] and len(after['snapshots'])==len(before['snapshots'])+1
    checks['new_snapshot_ready']=after['snapshots'][-1]['id']==receipt['result']['snapshot_id'] and after['snapshots'][-1]['ready']==1 and json.loads(after['snapshots'][-1]['reasons'])==[]
    old_events={row['id']:row for row in before['evidence']}
    new_events={row['id']:row for row in after['evidence']}
    checks['old_audit_evidence_preserved']=all(new_events.get(identity)==row for identity,row in old_events.items())
    allowed={item['evidence_id'] for item in expected.values()}|{'portfolio:'+after['snapshots'][-1]['id']}|(set(wanted_releases)-set(old_releases))
    checks['exact_audit_evidence_additions']=set(new_events)-set(old_events)==allowed
    claims=sorted(str(p) for folder in ['entry-slots','quote-recovery-slots'] for p in (ROOT/'state/independent-stock-trader'/folder).rglob('*') if p.is_file())
    checks['claim_set_preserved']=claims==plan['claim_paths']
    portfolio=receipt['portfolio']
    checks['portfolio_follows_order_evidence']=all(utc(item['observed_at'])<utc(portfolio['observed_at']) for item in expected.values())
    checks['newer_coherent_holdings_exact_fill_delta']=({s:Decimal(str(v)) for s,v in portfolio['held_shares'].items()}==expected_held)
    checks['no_broker_working_orders']=portfolio['working_order_count']==0 and not any(portfolio['pending_buy_shares'].values()) and not any(portfolio['pending_sell_shares'].values())
    ownership=_ownership(ROOT,plan['symbols'],plan['account_fingerprint'],portfolio['held_shares'],datetime.now(timezone.utc).isoformat())
    checks['native_readonly_ownership_safe']=ownership['safe_for_planning'] is True
    tests=OUT/'native-reconciliation-tests.xml'
    suites=list(ET.parse(tests).getroot().iter('testsuite'))
    checks['relevant_native_tests_pass']=bool(suites) and sum(int(s.get('tests',0)) for s in suites)>=86 and all(int(s.get('failures',0))==int(s.get('errors',0))==0 for s in suites)
    result={'verified_at':datetime.now(timezone.utc).isoformat(),'status':'PASS' if all(checks.values()) else 'FAIL',
            'scope':'INDEPENDENT_LOCAL_READ_ONLY_NO_PROVIDER_CALLS_NO_RECONCILIATION','checks':checks,
            'reservation_results':summary,'saved_broker_portfolio_at':portfolio['observed_at'],
            'new_fills':len(wanted_fills)-len(old_fills),'new_assignment_releases':len(wanted_releases)-len(old_releases),
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
