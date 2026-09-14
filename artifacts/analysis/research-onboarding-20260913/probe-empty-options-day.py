"""Retain provider evidence for the failed, approved historical interval."""
import json
from pathlib import Path
import databento
import os
from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_cold_start import _request_kwargs
from datafetching.symbol_onboarding import load_plan

load_repository_environment()
folder = Path(__file__).resolve().parent
plan = load_plan(folder/'CROX/plan.json')
request = next(r for r in plan['requests'] if r['dataset']=='OPRA.PILLAR' and r['schema']=='cbbo-1s')
client = databento.Historical(os.environ['DATABENTO_API_KEY'])
quote = client.metadata.get_cost(**_request_kwargs(request))
assert quote == 0
kw = {**_request_kwargs(request), 'start':'2025-11-27', 'end':'2025-11-28'}
out = {'request':kw, 'approved_scope_cost_usd':quote}
out['conditions'] = client.metadata.get_dataset_condition(dataset=kw['dataset'], start_date=kw['start'], end_date=kw['end'])
for label, fn, args in (
    ('count',client.metadata.get_record_count,kw),
    ('download',client.timeseries.get_range,{**kw,'path':folder/'empty-day-probe.dbn.zst'}),
    ('symbology',client.symbology.resolve,{
        'dataset':'OPRA.PILLAR','symbols':['CROX.OPT'],'stype_in':'parent',
        'stype_out':'instrument_id','start_date':kw['start'],'end_date':kw['end']}),
):
    try:
        result = fn(**args)
        out[label] = {'status':'RETURNED','result':result if isinstance(result,(dict,list,int,float,str)) else str(type(result))}
    except Exception as exc:
        out[label] = {'status':'ERROR','error_type':type(exc).__name__,'error':str(exc)}
(folder/'empty-options-day-evidence.json').write_text(json.dumps(out,indent=2)+'\n',encoding='utf-8')
print(json.dumps(out))
