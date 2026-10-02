"""Independent read-only ledger/arithmetic audit of the October 1 fallback review."""
from pathlib import Path
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import Counter, defaultdict
import hashlib
import json
import re
import sqlite3

ROOT = Path('C:/dev/ducketz')
REVIEW = ROOT/'artifacts/analysis/fallback-session-reviews/20261002T064859Z-20261001'
LEDGER = Path('C:/DATASTORE/state/independent-stock-trader/holdings.sqlite3')
OUT = ROOT/'artifacts/analysis/overnight-20261001/fallback-review-audit'
NOW = datetime.now(timezone.utc)
DAY = date(2026, 10, 1)
PACIFIC = ZoneInfo('America/Los_Angeles')
OPEN = {'RESERVED','SUBMITTED','UNKNOWN','WORKING','PARTIAL'}
HIERARCHY = {'1h':['4h','1d','1w'],'4h':['1d','1w'],'1d':['1w'],'1w':[]}
SLOTS = [(hour,horizon) for hour in range(4,17) for horizon in ('1h','4h','1d')
         if horizon == '1h' or horizon == '4h' and hour in (4,8,12,16) or horizon == '1d' and hour == 4]
WEIGHTS = {'1h':1,'4h':2,'1d':3}

def sha(path):
    with path.open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()

def ref(kind, identity):
    return hashlib.sha256(json.dumps([kind,identity],separators=(',',':')).encode()).hexdigest()

def donor_ref(identity):
    return hashlib.sha256(identity.encode()).hexdigest()

def same_json(a,b):
    return json.dumps(a,sort_keys=True,separators=(',',':')) == json.dumps(b,sort_keys=True,separators=(',',':'))

def in_day(value):
    return datetime.fromisoformat(value).astimezone(PACIFIC).date() == DAY

def fail_if(condition, issue):
    if condition: errors.append(issue)

report = json.loads((REVIEW/'report.json').read_text())
manifest = json.loads((REVIEW/'manifest.json').read_text())
errors=[]
fail_if(sha(REVIEW/'report.json') != manifest['output_files']['report.json']['checksum_sha256'], 'report manifest checksum')
fail_if((REVIEW/'report.json').stat().st_size != manifest['output_files']['report.json']['size'], 'report manifest size')
fail_if(report['action_date'] != str(DAY) or manifest['action_date'] != str(DAY), 'review action date')
fail_if(report['status'] != 'OBSERVED' or manifest['status'] != 'OBSERVED' or report['read_only'] is not True, 'review observed/read-only state')
for key in ('read_started_at','read_completed_at','latest_recorded_snapshot_at','query_snapshot'):
    fail_if(report[key] != manifest['source_observation'][key], 'manifest observation '+key)
fail_if(report['baseline']['source_fingerprint'] != manifest['source_observation']['source_fingerprint'], 'manifest source fingerprint')

db=sqlite3.connect(LEDGER.as_uri()+'?mode=ro',uri=True,isolation_level=None)
db.row_factory=sqlite3.Row
db.execute('PRAGMA query_only=ON')
db.execute('BEGIN')
try:
    query_only = db.execute('PRAGMA query_only').fetchone()[0]
    baseline_rows = db.execute('SELECT payload FROM fallback_days WHERE action_date=?',(str(DAY),)).fetchall()
    if len(baseline_rows) != 1: raise RuntimeError('Expected one recorded baseline')
    baseline=json.loads(baseline_rows[0]['payload'])
    latest_snapshot=db.execute('SELECT MAX(observed_at) FROM snapshots').fetchone()[0]
    fail_if(latest_snapshot != report['latest_recorded_snapshot_at'], 'latest snapshot advanced since review')
    expected_baseline = {'baseline_ref':ref('baseline',baseline['baseline_id']), 'observed_at':baseline['observed_at'],
        'source_fingerprint':baseline['source_fingerprint'], 'policy_version':baseline['policy']['policy_version'],
        'symbols':baseline['symbols'], 'donors':[{'donor_allocation_id_sha256':donor_ref(identity),
            **{k:record[k] for k in ('symbol','horizon','initial_owned_shares','daily_cap')}}
            for identity,record in sorted(baseline['donors'].items())]}
    fail_if(not same_json(expected_baseline,report['baseline']), 'baseline differs from ledger')
    policy=baseline['policy']
    fail_if(policy['daily_cap_numerator'] != 1 or policy['daily_cap_denominator'] != 2
        or policy['donor_daily_cap_numerator'] != 1 or policy['donor_daily_cap_denominator'] != 2
        or policy['slot_horizon_weights'] != WEIGHTS or policy['total_slot_weight'] != 24
        or policy['slots_per_symbol'] != 18 or policy['hierarchy'] != HIERARCHY, 'baseline policy differs')
    reservations=[dict(x) for x in db.execute('''SELECT r.id,r.allocation,r.side,r.quantity,r.filled,r.status,r.request,r.last_evidence_at,
        a.symbol,a.horizon,s.observed_at AS snapshot_at FROM reservations r JOIN allocations a ON a.id=r.allocation
        LEFT JOIN snapshots s ON s.id=json_extract(r.request,'$.snapshot') WHERE r.side='SELL' ''')]
    all_fills=defaultdict(list)
    for row in db.execute('SELECT id,reservation,quantity,price,executed_at FROM fills ORDER BY executed_at,id'):
        all_fills[row['reservation']].append(dict(row))
    relevant=[]
    for row in reservations:
        request=json.loads(row['request']);row['request']=request
        fills_today=[x for x in all_fills[row['id']] if in_day(x['executed_at'])]
        is_fallback=request.get('kind')=='fallback-direction-exit'
        requested_today=request.get('action_date')==str(DAY) if is_fallback else bool(row['snapshot_at'] and in_day(row['snapshot_at']))
        if requested_today or fills_today or (is_fallback and row['status'] in OPEN):
            row['today_fills']=fills_today;row['requested_today']=requested_today;row['is_fallback']=is_fallback;relevant.append(row)
    needed_ids={x['id'] for x in relevant}
    latest_order={}
    for e in db.execute("SELECT payload FROM evidence WHERE kind='order'"):
        event=json.loads(e['payload']);identity=event['reservation_id']
        if identity in needed_ids and (identity not in latest_order or event['observed_at'] > latest_order[identity]['observed_at']):latest_order[identity]=event
    snapshot_read_completed=datetime.now(timezone.utc).isoformat()
finally:
    db.rollback();db.close()

counts={k:0 for k in report['sales_counts']}
reconstructed=[]
normal=[]
symbol_use=Counter();donor_use=Counter();slot_use=Counter();slot_requests=Counter();terminal_evidence=[]
for row in relevant:
    req=row['request'];fallback=row['is_fallback'];kind='fallback' if fallback else 'normal_sell'
    fills=row['today_fills'];filled_today=sum(x['quantity'] for x in fills)
    counts[kind+'_orders_requested']+=int(row['requested_today']);counts[kind+'_orders_with_fills']+=int(bool(fills));counts[kind+'_shares_filled']+=filled_today
    order=latest_order.get(row['id']);event_ok=order is not None and order['status']==row['status'] and order['cumulative_filled_quantity']==row['filled'] and order['order_quantity']==row['quantity']
    if order:
        ledgerfills={(x['id'],x['quantity'],x['price'],x['executed_at']) for x in all_fills[row['id']]}
        evidencefills={(x['fill_id'],x['quantity'],str(x['price']),x['executed_at']) for x in order['fills']}
        event_ok = event_ok and ledgerfills == evidencefills and sum(x['quantity'] for x in all_fills[row['id']])==row['filled']
    fail_if(not event_ok,'latest order/fill evidence differs for '+ref('reservation',row['id']))
    terminal_evidence.append({'reservation_ref':ref('reservation',row['id']),'kind':kind,'status':row['status'],
        'recorded_broker_status':order.get('broker_status') if order else None,'observed_at':order.get('observed_at') if order else None,
        'requested_quantity':row['quantity'],'filled_all_dates':row['filled'],'filled_on_action_date':filled_today,
        'matches_recorded_cumulative_order_evidence':event_ok})
    if not fallback:
        normal.append({'reservation_ref':ref('reservation',row['id']),'requested_today':row['requested_today'],
            'status':row['status'],'filled_on_action_date':filled_today,'requested_quantity':row['quantity']})
        continue
    remaining=row['quantity']-row['filled'] if row['status'] in OPEN else 0
    counts['outstanding_fallback_orders']+=int(remaining>0);counts['outstanding_fallback_shares']+=remaining
    used=filled_today+remaining;symbol_use[row['symbol']]+=used;donor_use[row['allocation']]+=used
    rf=ref('reservation',row['id']);trigger=req['trigger_forecast']
    parsed=re.fullmatch(r'(\d{4}-\d{2}-\d{2}):([A-Z]+):(1h|4h|1d|1w)@(\d{2}):(\d{2})',trigger)
    fail_if(not parsed,'invalid trigger '+rf)
    if parsed:
        day,symbol,horizon,hour,minute=parsed.groups();key=(int(hour),horizon)
        fail_if(day!=str(DAY) or symbol!=row['symbol'] or horizon!=req['trigger_horizon'] or minute!='00' or key not in SLOTS,'trigger identity '+rf)
        index=SLOTS.index(key);before=sum(WEIGHTS[h] for _,h in SLOTS[:index]);budget=baseline['symbols'][symbol]['daily_cap']
        quota=budget*(before+WEIGHTS[horizon])//24-budget*before//24
        fail_if(req['slot_quota']!=quota or row['quantity']>quota,'slot quota '+rf)
        slot_use[trigger]+=used;slot_requests[trigger]+=1
        fail_if(slot_use[trigger]>quota,'slot filled/reserved cap '+rf)
        start=datetime.fromisoformat(req['start']).astimezone(PACIFIC)
        fail_if(start.date()!=DAY or (start.hour,start.minute)!=(int(hour),0),'slot start '+rf)
    donor=baseline['donors'].get(row['allocation'])
    fail_if(donor is None or donor['symbol']!=row['symbol'] or donor['horizon']!=row['horizon'],'donor binding '+rf)
    fail_if(row['horizon'] not in HIERARCHY[req['trigger_horizon']],'donor hierarchy '+rf)
    fail_if(req['donor_allocation_id']!=row['allocation'] or req['owner_horizon']!=row['horizon'],'donor identity '+rf)
    fail_if(req['action_date']!=str(DAY) or req['baseline_id']!=baseline['baseline_id'] or req['source_fingerprint']!=baseline['source_fingerprint'],'baseline source/date '+rf)
    fail_if(row['horizon'] in req['excluded_donor_horizons'],'excluded donor horizon '+rf)
    reconstructed.append({'reservation_ref':rf,'symbol':row['symbol'],'origin_action_date':req['action_date'],
        'trigger_forecast_id':trigger,'trigger_horizon':req['trigger_horizon'],'donor_horizon':row['horizon'],
        'donor_allocation_id_sha256':donor_ref(row['allocation']),'requested_quantity':row['quantity'],'slot_quota':req['slot_quota'],
        'status':row['status'],'filled_quantity_all_dates':row['filled'],'filled_quantity_on_action_date':filled_today,
        'outstanding_reserved_quantity':remaining,'unknown_reserved_quantity':remaining if row['status']=='UNKNOWN' else 0,
        'terminal_unfilled_released_quantity':row['quantity']-row['filled'] if row['status'] in {'CANCELLED','REJECTED'} else 0,
        'fills_on_action_date':[{k:x[k] for k in ('quantity','price','executed_at')} for x in fills]})

fail_if(not same_json(counts,report['sales_counts']),'sales counts differ')
fail_if(not same_json(sorted(reconstructed,key=lambda x:x['reservation_ref']),sorted(report['fallback_orders'],key=lambda x:x['reservation_ref'])),'fallback order detail differs')
fail_if(any(n!=1 for n in slot_requests.values()),'duplicate triggering forecast reservation')
symbol_summary=[];donor_summary=[]
for symbol,b in baseline['symbols'].items():
    donors=[d for d in baseline['donors'].values() if d['symbol']==symbol]
    fail_if(sum(d['initial_owned_shares'] for d in donors)!=b['initial_owned_longer_horizon_shares'],'initial symbol/donor conservation '+symbol)
    fail_if(b['daily_cap']!=b['initial_owned_longer_horizon_shares']//2 or symbol_use[symbol]>b['daily_cap'],'symbol half cap '+symbol)
    expected={'daily_cap':b['daily_cap'],'filled_plus_reserved':symbol_use[symbol],'remaining':b['daily_cap']-symbol_use[symbol]}
    fail_if(not same_json(expected,report['daily_budget_usage']['symbols'][symbol]),'symbol usage '+symbol)
    quotas=[];before=0
    for hour,horizon in SLOTS:
        weight=WEIGHTS[horizon];quota=b['daily_cap']*(before+weight)//24-b['daily_cap']*before//24;before+=weight
        quotas.append({'hour':hour,'horizon':horizon,'quota':quota})
    fail_if(sum(x['quota'] for x in quotas)!=b['daily_cap'],'slot conservation '+symbol)
    symbol_summary.append({'symbol':symbol,**expected,'slot_quotas':quotas})
reported_donors={x['donor_allocation_id_sha256']:x for x in report['daily_budget_usage']['donors']}
for identity,d in baseline['donors'].items():
    fail_if(d['daily_cap']!=d['initial_owned_shares']//2 or donor_use[identity]>d['daily_cap'],'donor half cap '+donor_ref(identity))
    expected={'donor_allocation_id_sha256':donor_ref(identity),'symbol':d['symbol'],'horizon':d['horizon'],
        'daily_cap':d['daily_cap'],'filled_plus_reserved':donor_use[identity],'remaining':d['daily_cap']-donor_use[identity]}
    fail_if(not same_json(expected,reported_donors[donor_ref(identity)]),'donor usage '+donor_ref(identity));donor_summary.append(expected)

summary={'sales_counts':counts,'fallback_status_counts':dict(Counter(x['status'] for x in reconstructed)),
    'normal_sale_status_counts':dict(Counter(x['status'] for x in normal)),
    'fallback_requested_shares':sum(x['requested_quantity'] for x in reconstructed),
    'fallback_terminal_unfilled_released_shares':sum(x['terminal_unfilled_released_quantity'] for x in reconstructed),
    'fallback_unknown_shares':sum(x['unknown_reserved_quantity'] for x in reconstructed),
    'total_symbol_daily_cap':sum(x['daily_cap'] for x in baseline['symbols'].values()),
    'total_donor_daily_cap':sum(x['daily_cap'] for x in baseline['donors'].values())}
result={'schema_version':1,'status':'VERIFIED' if not errors else 'FAILED','observed_at_utc':NOW.isoformat(),
    'completed_at_utc':datetime.now(timezone.utc).isoformat(),'action_date':str(DAY),
    'review_report':{'path':str(REVIEW/'report.json'),'sha256':sha(REVIEW/'report.json')},
    'review_manifest':{'path':str(REVIEW/'manifest.json'),'sha256':sha(REVIEW/'manifest.json')},
    'ledger_observation':{'mode':'SQLite mode=ro, PRAGMA query_only=ON, one BEGIN transaction rolled back',
        'query_only':query_only,'read_completed_at':snapshot_read_completed,'latest_snapshot_at':latest_snapshot,
        'raw_account_broker_reservation_allocation_and_fill_ids_omitted':True},
    'errors':errors,'summary':summary,'symbol_caps':symbol_summary,'donor_caps':donor_summary,
    'recorded_terminal_and_fill_evidence':terminal_evidence,'normal_sales_separate':normal,
    'checks':{'report_hash_size_and_observation_binding':True,'baseline_matches_native_record':True,
        'symbol_initial_donor_conservation_and_half_caps':True,'donor_half_caps':True,'exact_18_slot_24_weight_integer_quotas':True,
        'unique_trigger_no_retry_quota_reset':True,'per_order_and_aggregate_slot_limits':True,'all_counts_match_independent_ledger_projection':True,
        'terminal_state_and_cumulative_fill_evidence':True,'actual_fallback_sales_separate_from_normal_sales':True} if not errors else {},
    'limitations':['Ledger-recorded broker evidence is verified offline; no fresh broker confirmation requested.',
        'Audit proves observed caps and saved ownership/source/slot bindings; it does not replay every historical quote, risk or route-absence decision.',
        'Normal sales are outside fallback budgets. Counts are not hypothetical planning, realized profit, or evidence to adjust 50%.'],
    'provider_calls':0,'broker_calls':0,'ledger_mutations':0,'source_changes':0,'orders':0}
OUT.mkdir(parents=True,exist_ok=True)
destination=OUT/('audit-'+NOW.strftime('%Y%m%dT%H%M%SZ')+'.json')
destination.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'path':str(destination),'sha256':sha(destination),'status':result['status'],'errors':errors,'summary':summary},indent=2))
raise SystemExit(0 if not errors else 1)
