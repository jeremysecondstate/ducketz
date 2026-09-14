"""Read-only, small JSON availability probes; stores sizes and dates only."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import concurrent.futures
import dataclasses
from datetime import datetime,timedelta,timezone
import json,os
from dotenv import load_dotenv
load_dotenv(ROOT/'.env',override=False)
from app.services.fmp_corporate_data import FmpCorporateDataProvider,FmpCorporateDataSpec
from datafetching.schwab_fetch import DataFetchingSchwabSession,_specs_for_profile

symbols=('CROX','PATH','TWST','IONQ')
listing={'CROX':'2018-01-01','PATH':'2021-04-21','TWST':'2018-10-31','IONQ':'2021-10-01'}
floor='2018-01-01'
out={'checked_at':datetime.now(timezone.utc).isoformat(),'scope':'Only metadata, counts, dates and response lengths retained; no publication to DATASTORE. Financial periods and SEC filing dates filtered to 2018 or later.','fmp':[],'schwab':[]}
dest=ROOT/'artifacts/analysis/stock-research-other-providers-2018-20260913.json'

def save(): dest.write_text(json.dumps(out,indent=2),encoding='utf-8')
def describe(rows):
    dates=sorted(str(r.get('date',r.get('filingDate','')))[:10] for r in rows if r.get('date',r.get('filingDate')))
    return {'rows':len(rows),'first_date':dates[0] if dates else None,'last_date':dates[-1] if dates else None,'json_bytes':len(json.dumps(rows,separators=(',',':'),default=str).encode())}

fmp=FmpCorporateDataProvider()
tasks=[]
for symbol in symbols:
    for spec in fmp.corporate_specs(symbol):
        params=dict(spec.params)
        if 'statement' in spec.endpoint and not spec.endpoint.endswith('growth'): params['limit']=1000
        tasks.append((symbol,dataclasses.replace(spec,params=params)))
    tasks.append((symbol,FmpCorporateDataSpec('daily_prices_2018','historical-price-eod/full',{'symbol':symbol,'from':listing[symbol],'to':'2026-09-11'})))
    tasks.append((symbol,FmpCorporateDataSpec('intraday_5min_entitlement_sample','historical-chart/5min',{'symbol':symbol,'from':'2026-09-10','to':'2026-09-11'})))

def fmp_probe(task):
    symbol,spec=task
    row={'symbol':symbol,'endpoint':spec.endpoint,'key':spec.key}
    try:
        payload=fmp._get_json(spec.endpoint,spec.params)
        if (payload is None or payload==[]) and spec.fallback_endpoint:
            payload=fmp._get_json(spec.fallback_endpoint,spec.fallback_params or {'symbol':symbol})
            row['endpoint']=spec.fallback_endpoint
        raw=payload if isinstance(payload,list) else [payload] if isinstance(payload,dict) else []
        selected=[r for r in raw if isinstance(r,dict) and (not r.get('date') or str(r['date'])[:10]>=floor)]
        row.update(status='ok',**describe(selected))
    except Exception as exc: row.update(status='unavailable',error_type=type(exc).__name__)
    return row

with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    for n,row in enumerate(pool.map(fmp_probe,tasks),1):
        out['fmp'].append(row)
        if n%20==0: save();print(json.dumps({'fmp_checked':n,'total':len(tasks)}),flush=True)
save()
session=DataFetchingSchwabSession()
end=datetime(2026,9,12,tzinfo=timezone.utc)
for symbol in symbols:
    for spec in _specs_for_profile('continuation'):
        start=datetime.fromisoformat(listing[symbol]).replace(tzinfo=timezone.utc) if spec.frequency_type!='minute' else end-timedelta(days=10)
        row={'symbol':symbol,'key':spec.key,'frequency_type':spec.frequency_type,'frequency':spec.frequency,'requested_start':start.isoformat(),'requested_end':end.isoformat()}
        try:
            payload=session.get_price_history(symbol,period_type=spec.period_type,period=None,frequency_type=spec.frequency_type,frequency=spec.frequency,need_extended_hours_data=spec.need_extended_hours_data,start_datetime=start,end_datetime=end)
            candles=payload.get('candles',[])
            dates=sorted(datetime.fromtimestamp(r['datetime']/1000,tz=timezone.utc).isoformat() for r in candles)
            row.update(status='ok',rows=len(candles),first_date=dates[0] if dates else None,last_date=dates[-1] if dates else None,json_bytes=len(json.dumps(payload,separators=(',',':')).encode()))
        except Exception as exc: row.update(status='unavailable',error_type=type(exc).__name__)
        out['schwab'].append(row)
    row={'symbol':symbol,'key':'current_option_chain_snapshot'}
    try:
        payload=session.get_option_chain_snapshot(symbol)
        contracts=sum(len(cs) for side in ('callExpDateMap','putExpDateMap') for strikes in payload.get(side,{}).values() for cs in strikes.values())
        row.update(status='ok',contracts=contracts,json_bytes=len(json.dumps(payload,separators=(',',':')).encode()))
    except Exception as exc: row.update(status='unavailable',error_type=type(exc).__name__)
    out['schwab'].append(row)
    save()
    print(json.dumps({'schwab_checked_symbol':symbol}),flush=True)

sec=json.loads((ROOT/'artifacts/analysis/stock-research-sec-history-inventory-20260913.json').read_text())
out['sec']=[]
for c in sec['companies']:
    selected=[r for r in c['all_rows'] if r.get('filingDate','')>=floor]
    out['sec'].append({'symbol':c['symbol'],'filings':len(selected),'first_filing':min(r['filingDate'] for r in selected),'last_filing':max(r['filingDate'] for r in selected),'reported_complete_submission_bytes':sum(int(r.get('size') or 0) for r in selected),'note':'SEC-reported full submission size, not measured compressed local storage; includes available pre-IPO filings from 2018 onward.'})
out['totals']={provider:{'calls':len(out[provider]),'json_bytes':sum(r.get('json_bytes',0) for r in out[provider]),'unavailable':[{'symbol':r['symbol'],'key':r['key'],'error_type':r.get('error_type')} for r in out[provider] if r['status']!='ok']} for provider in ('fmp','schwab')}
out['completed_at']=datetime.now(timezone.utc).isoformat()
save()
print(json.dumps({'totals':out['totals'],'sec':out['sec']},indent=2),flush=True)
