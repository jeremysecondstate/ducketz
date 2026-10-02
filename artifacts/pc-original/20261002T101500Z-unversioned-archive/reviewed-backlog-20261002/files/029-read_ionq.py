"""Bounded read-only local ownership snapshot; no broker or trader APIs."""
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal
import hashlib,json,sqlite3
import pandas as pd

ROOT=Path('<LOCAL_DATASTORE>'); OUT=Path(__file__).resolve().parent
DB=ROOT/'state/independent-stock-trader/holdings.sqlite3'
assert not Path(str(DB)+'-wal').exists() or Path(str(DB)+'-shm').exists(), 'WAL_READ_STATE_UNAVAILABLE'
started=datetime.now(timezone.utc)
connection=sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True,timeout=1)
connection.row_factory=sqlite3.Row
try:
    connection.execute('PRAGMA query_only=ON')
    connection.execute('BEGIN')
    metadata=dict(connection.execute('SELECT key,value FROM metadata'))
    assert metadata['version']=='independent-stock-horizon-ledger-v1'
    tables={row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    allocations=[dict(row) for row in connection.execute('SELECT id,symbol,horizon,forecast,start,end,status FROM allocations WHERE symbol=? AND account=?',('IONQ',metadata['account']))]
    reservations=[dict(row) for row in connection.execute('SELECT r.id,r.allocation,r.side,r.quantity,r.price,r.filled,r.status,r.last_evidence_at FROM reservations r JOIN allocations a ON a.id=r.allocation WHERE a.symbol=? AND a.account=?',('IONQ',metadata['account']))]
    assignments=[dict(row) for row in connection.execute('SELECT i.id,i.allocation,i.quantity,i.observed_at FROM inventory_assignments i JOIN allocations a ON a.id=i.allocation WHERE a.symbol=? AND a.account=?',('IONQ',metadata['account']))]
    releases=[]
    if 'inventory_assignment_releases' in tables:
        releases=[dict(row) for row in connection.execute('SELECT r.assignment,r.quantity,r.observed_at FROM inventory_assignment_releases r JOIN inventory_assignments i ON i.id=r.assignment JOIN allocations a ON a.id=i.allocation WHERE a.symbol=? AND a.account=?',('IONQ',metadata['account']))]
    fills=[dict(row) for row in connection.execute('SELECT f.id,f.reservation,f.quantity,f.price,f.executed_at FROM fills f JOIN reservations r ON r.id=f.reservation JOIN allocations a ON a.id=r.allocation WHERE a.symbol=? AND a.account=?',('IONQ',metadata['account']))]
    latest=dict(connection.execute('SELECT id,observed_at,ready,reasons,payload,owned FROM snapshots ORDER BY observed_at DESC,rowid DESC LIMIT 1').fetchone())
    blocks=connection.execute('SELECT count(*) FROM blocks WHERE symbol=?',('IONQ',)).fetchone()[0]
finally:
    connection.close()
finished=datetime.now(timezone.utc)
by_id={row['id']:{**row,'owned_shares':0,'pending_buy':0,'pending_sell':0} for row in allocations}
by_assignment={row['id']:row for row in assignments}
for row in assignments:by_id[row['allocation']]['owned_shares']+=row['quantity']
for row in releases:by_id[by_assignment[row['assignment']]['allocation']]['owned_shares']-=row['quantity']
open_statuses={'RESERVED','SUBMITTED','WORKING','PARTIAL','UNKNOWN'}
for row in reservations:
    item=by_id[row['allocation']]
    item['owned_shares']+=row['filled']*(1 if row['side']=='BUY' else -1)
    if row['status'] in open_statuses:item['pending_'+row['side'].lower()]+=row['quantity']-row['filled']
payload=json.loads(latest['payload']); saved_owned=json.loads(latest['owned'])
current_owned=sum(x['owned_shares'] for x in by_id.values())
fingerprint_matches=payload.get('account_fingerprint')==metadata['account']
saved_held=Decimal(str(payload['held_shares']['IONQ']))
owned_at_snapshot=saved_owned.get('IONQ',0)
pointer_path=ROOT/'ml/gameplan-trade-plan-latest/run.json'
pointer=json.loads(pointer_path.read_text())['current']; plan=ROOT/pointer['run_path']
snapshot=json.loads((plan/'account-snapshot.json').read_text())
table=pd.read_parquet(plan/'trade-plan.parquet'); table=table[table.symbol.eq('IONQ')]
cols=['id','route','model_group','action_anchor_local','direction','calibrated_probability','current_held_shares','projected_trade_quantity','direction_based_trade_quantity','direction_based_action','direction_based_reason','projected_shares_after']
ledger=json.loads((plan/'direction-ledger.json').read_text())
manifest=json.loads((plan/'manifest.json').read_text()); receipt=json.loads((plan/'receipt.json').read_text())
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
assert receipt['manifest_sha256']==sha(plan/'manifest.json')
assert pointer['receipt_sha256']==sha(plan/'receipt.json')
for name in ['trade-plan.parquet','account-snapshot.json','direction-ledger.json']:
    assert manifest['output_files'][name]['checksum_sha256']==sha(plan/name)
active=[row for row in by_id.values() if row['status']=='ACTIVE' or row['owned_shares'] or row['pending_buy'] or row['pending_sell']]
result={
    'read_started_at':started.isoformat(),'read_finished_at':finished.isoformat(),'read_seconds':(finished-started).total_seconds(),
    'scope':'SQLITE_MODE_RO_QUERY_ONLY_SHORT_COHERENT_READ_TRANSACTION; no application lock or mutation',
    'database_path':str(DB),'latest_saved_snapshot':{'id':latest['id'],'observed_at':latest['observed_at'],'ready':bool(latest['ready']),'reasons':json.loads(latest['reasons']),'account_matches_ledger':fingerprint_matches,'ionq_held_shares':float(saved_held),'ionq_owned_shares_at_snapshot':owned_at_snapshot,'ionq_unallocated_at_snapshot':float(saved_held-Decimal(str(owned_at_snapshot)))},
    'current_local_ledger_ionq_owned_shares':current_owned,'active_or_nonempty_allocations':active,'reservation_status_counts':reservations,'inventory_assignments':assignments,'assignment_releases':releases,'fills':fills,'persistent_block_count':blocks,
    'snapshot_owned_matches_current_ledger':owned_at_snapshot==current_owned,
    'planning':{'path':str(plan),'snapshot_at':snapshot['observed_at'],'snapshot_held_shares':snapshot['held_shares']['IONQ'],'snapshot_allocations':[x for x in snapshot['ownership']['active_allocations'] if x['symbol']=='IONQ'],'rows':json.loads(table[cols].to_json(orient='records')),'events':[x for x in ledger['events'] if x['symbol']=='IONQ'],'holding_policy':ledger['holding_policy']},
    'limitations':['Latest saved holdings are timestamped broker evidence already recorded by the worker, not a fresh broker query or screenshot balance.','Ledger fills and reservations are a coherent local read; only broker evidence can establish current actual inventory and fills.','Saved planning rows and447shares are conditional projections; they are not execution receipts.','Manual Gameplan policy can assign eligible unallocated actual stock to a bearish sell; other horizons remain protected. No hierarchical fallback currently applies.'],
    'production_writes':0,'broker_calls':0,'provider_calls':0,'trader_actions':0,
}
path=OUT/('ionq-review-'+started.strftime('%Y%m%dT%H%M%SZ')+'.json')
path.write_text(json.dumps(result,indent=2,default=str)+'\n',encoding='utf-8')
print(json.dumps({'path':str(path),'sha256':sha(path),'read_seconds':result['read_seconds'],'saved_snapshot':result['latest_saved_snapshot'],'current_owned':current_owned,'active':active,'last_fill':max((x['executed_at'] for x in fills),default=None),'pending':[{k:x[k] for k in ['side','quantity','filled','status','last_evidence_at']} for x in reservations if x['status'] in open_statuses],'snapshot_owned_matches_current':result['snapshot_owned_matches_current_ledger']},indent=2))
