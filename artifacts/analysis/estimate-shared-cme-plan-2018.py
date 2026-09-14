"""Optional shared CME cold-archive expansion; metadata only."""
from pathlib import Path
from datetime import datetime,timezone
from dateutil.relativedelta import relativedelta
import collections,concurrent.futures,json,os,threading,time
from dotenv import load_dotenv
import databento

ROOT=Path(__file__).resolve().parents[2]
load_dotenv(ROOT/'.env',override=False)
archive=Path('C:/DATASTORE/market-data/databento/cme/GLBX.MDP3')
groups=collections.defaultdict(list)
for p in archive.rglob('manifest.json'):
    m=json.loads(p.read_text());q=m['request']
    groups[(q['stype_in'],q['schema'])].append(m)
asof=datetime.now(timezone.utc).date()
api=databento.Historical(os.environ['DATABENTO_API_KEY'])
available=api.metadata.get_dataset_range(dataset='GLBX.MDP3')
requests=[]
for (stype,schema),manifests in groups.items():
    level='L2' if schema=='mbp-10' else 'L3' if schema=='mbo' else 'L1' if schema in ('bbo-1m','bbo-1s','tbbo','trades') else 'L0'
    start=max('2018-01-01',available['schema'][schema]['start'][:10])
    if level=='L1':start=max(start,(asof-relativedelta(months=12)).isoformat())
    if level in ('L2','L3'):start=max(start,(asof-relativedelta(months=1)).isoformat())
    end=available['schema'][schema]['end'][:10]
    config_key='DATABENTO_CME_CONTEXT_SYMBOLS' if stype=='continuous' else 'DATABENTO_CME_CONTRACT_SYMBOLS'
    symbols=sorted(json.loads(os.environ[config_key]))
    n=sum(m['normalized']['row_count'] for m in manifests)
    size=sum(m['raw']['size_bytes']+m['normalized']['size_bytes'] for m in manifests)
    reusable=sum(m['raw']['size_bytes']+m['normalized']['size_bytes'] for m in manifests if m['request']['start']>=start and m['request']['end']<=end and set(m['request']['symbol_scope']).issubset(symbols))
    requests.append({'dataset':'GLBX.MDP3','schema':schema,'level':level,'stype_in':stype,'symbols':symbols,'start':start,'end':end,'calibration_bytes_per_record':size/n,'existing_cold_bytes_inside_target':reusable})
out={'checked_at':datetime.now(timezone.utc).isoformat(),'scope':'OPTIONAL expansion of the shared CME cold archive using the existing 13 cold schemas and the nine currently configured continuous/dated scopes. This is not required just to add four equities. Metadata only.','dataset_range':available,'requests':requests,'method':'Metadata records times measured native DBN plus normalized Parquet bytes/row for the matching CME schema and symbology group; compression calibration may differ for rolled contracts. Incremental estimate subtracts existing cold partitions wholly inside the requested range for currently selected symbols; existing runtime cache and other contracts remain shared and unchanged. Continuous/dated overlaps are retained because the current archive stores both.'}
dest=ROOT/'artifacts/analysis/stock-research-shared-cme-2018-preflight-20260913.json'
local=threading.local()
def save():dest.write_text(json.dumps(out,indent=2,default=str),encoding='utf-8')
def run(r):
    try:
        if not hasattr(local,'api'):local.api=databento.Historical(os.environ['DATABENTO_API_KEY'])
        kw={k:r[k] for k in ('dataset','schema','stype_in','symbols','start','end')}
        r['records']=local.api.metadata.get_record_count(**kw);time.sleep(.15)
        r['billable_uncompressed_bytes']=local.api.metadata.get_billable_size(**kw);time.sleep(.15)
        r['quoted_cost_usd']=local.api.metadata.get_cost(**kw)
        r['estimated_full_cold_bytes']=round(r['records']*r['calibration_bytes_per_record'])
        r['estimated_incremental_cold_bytes']=max(0,r['estimated_full_cold_bytes']-r['existing_cold_bytes_inside_target'])
    except Exception as exc:r['error_type']=type(exc).__name__
    return r
save()
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    for n,r in enumerate(pool.map(run,requests),1):
        save()
        if n%5==0:print(json.dumps({'completed':n,'total':len(requests)}),flush=True)
out['errors']=[r['schema']+' '+r['stype_in'] for r in requests if 'error_type' in r]
out['totals']={k:sum(r.get(k,0) for r in requests) for k in ('estimated_full_cold_bytes','estimated_incremental_cold_bytes','existing_cold_bytes_inside_target','billable_uncompressed_bytes','quoted_cost_usd')}
out['totals']['by_level_incremental_gib']={lv:sum(r.get('estimated_incremental_cold_bytes',0) for r in requests if r['level']==lv)/2**30 for lv in ('L0','L1','L2','L3')}
out['completed_at']=datetime.now(timezone.utc).isoformat()
save()
print(json.dumps({'totals':out['totals'],'errors':out['errors']},indent=2),flush=True)
