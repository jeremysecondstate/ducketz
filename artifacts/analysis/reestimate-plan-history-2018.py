"""Read-only provider metadata estimate; never submits historical data jobs."""
from __future__ import annotations

import concurrent.futures
import copy
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import threading
import time

import databento
from dateutil.relativedelta import relativedelta
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'artifacts/analysis/stock-research-plan-aware-2018-preflight-20260913.json'
load_dotenv(ROOT / '.env', override=False)
previous = json.loads((ROOT / 'artifacts/analysis/stock-research-full-history-preflight-20260913.json').read_text())
now = datetime.now(timezone.utc)
floor = '2018-01-01'
as_of = now.date()
old = {r['id']: r for r in previous['requests']}
local = threading.local()

def client():
    if not hasattr(local, 'api'):
        local.api = databento.Historical(os.environ['DATABENTO_API_KEY'])
    return local.api

ranges = {d: client().metadata.get_dataset_range(dataset=d) for d in ('OPRA.PILLAR','XNAS.ITCH')}
rows = []
for rid in previous['scenarios']['full_available']:
    r = copy.deepcopy(old[rid])
    level = 'L3' if r['schema'] == 'imbalance' else r['level']
    available = ranges[r['dataset']]['schema'][r['schema']]
    start = max(floor, previous['listing_dates'][r['symbol']], available['start'][:10])
    if level == 'L1':
        start = max(start, (as_of - relativedelta(months=12)).isoformat())
    if level in ('L2','L3'):
        start = max(start, (as_of - relativedelta(months=1)).isoformat())
    end = available['end'][:10]
    request_id = '|'.join([r['symbol'],r['dataset'],r['schema'],start,end])
    if request_id in old:
        r = copy.deepcopy(old[request_id])
        r['metadata_origin'] = 'Earlier metadata preflight in this session; identical request'
        r['level'] = level
    else:
        r = {k:r[k] for k in ('symbol','dataset','schema','reference_bytes_per_record')}
        r.update(id=request_id,level=level,start=start,end=end,metadata_origin='Fresh account metadata request')
    rows.append(r)

result = {
    'checked_at':now.isoformat(), 'scope':'Four symbols; existing 25 Databento schemas; plan-included windows; no dates before 2018-01-01. Metadata only, no data download jobs.',
    'cutoff':floor,'as_of_utc_date':as_of.isoformat(),'listing_dates':previous['listing_dates'],
    'dataset_ranges':ranges,
    'window_policy':{'L0':'max(2018-01-01, listing date, schema availability)','L1':(as_of-relativedelta(months=12)).isoformat(),'L2_L3':(as_of-relativedelta(months=1)).isoformat(),'imbalance':'L3; corrected using the actual US Equities Standard account table'},
    'method':previous['storage_method'],
    'requests':rows,
}

def save():
    OUT.write_text(json.dumps(result,indent=2,default=str),encoding='utf-8')

def fetch(r):
    kw=dict(dataset=r['dataset'],schema=r['schema'],start=r['start'],end=r['end'],
        symbols=[r['symbol']+'.OPT' if r['dataset']=='OPRA.PILLAR' else r['symbol']],
        stype_in='parent' if r['dataset']=='OPRA.PILLAR' else 'raw_symbol')
    try:
        r['records'] = client().metadata.get_record_count(**kw)
        time.sleep(.15)
        r['billable_uncompressed_bytes'] = client().metadata.get_billable_size(**kw)
        time.sleep(.15)
        r['quoted_cost_usd'] = client().metadata.get_cost(**kw)
        r['estimated_raw_plus_parquet_bytes'] = round(r['records']*r['reference_bytes_per_record'])
    except Exception as exc:
        r['error_type'] = type(exc).__name__
    return r

save()
pending=[r for r in rows if 'records' not in r]
print(json.dumps({'requests':len(rows),'reused':len(rows)-len(pending),'fresh':len(pending),'windows':result['window_policy']}),flush=True)
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    for n,future in enumerate(concurrent.futures.as_completed([pool.submit(fetch,r) for r in pending]),1):
        r=future.result()
        save()
        if n%10==0 or n==len(pending):
            print(json.dumps({'completed_new':n,'total_new':len(pending),'last_symbol':r['symbol'],'last_schema':r['schema'],'last_cost':r.get('quoted_cost_usd'),'error':r.get('error_type')}),flush=True)

summaries=[]
for symbol in ('CROX','PATH','TWST','IONQ'):
    rs=[r for r in rows if r['symbol']==symbol]
    value={'symbol':symbol,'estimated_archive_gib':sum(r.get('estimated_raw_plus_parquet_bytes',0) for r in rs)/2**30,
        'quoted_cost_usd':sum(r.get('quoted_cost_usd',0) for r in rs),
        'billable_uncompressed_gib':sum(r.get('billable_uncompressed_bytes',0) for r in rs)/2**30,
        'by_dataset_gib':{d:sum(r.get('estimated_raw_plus_parquet_bytes',0) for r in rs if r['dataset']==d)/2**30 for d in ranges},
        'by_level_gib':{level:sum(r.get('estimated_raw_plus_parquet_bytes',0) for r in rs if r['level']==level)/2**30 for level in ('L0','L1','L2','L3')}}
    summaries.append(value)
result['summaries']=summaries
result['errors']=[r['id'] for r in rows if 'error_type' in r]
result['totals']={k:sum(s[k] for s in summaries) for k in ('estimated_archive_gib','quoted_cost_usd','billable_uncompressed_gib')}
result['completed_at']=datetime.now(timezone.utc).isoformat()
save()
print(json.dumps({'summaries':summaries,'totals':result['totals'],'errors':result['errors']},indent=2),flush=True)
