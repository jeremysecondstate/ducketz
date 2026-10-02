"""Two explicit metadata GETs only; no timeseries, jobs, downloads or production writes."""
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import sys

sys.dont_write_bytecode=True
logging.disable(logging.CRITICAL)
from dotenv import load_dotenv
load_dotenv(Path('<LOCAL_CHECKOUT>/.env'),override=False)
from app.services.databento_cme_context import DatabentoCmeContextProvider

out=Path(__file__).resolve().parent
provider=DatabentoCmeContextProvider()
assert provider.dataset=='GLBX.MDP3'
assert provider.api_key
client=provider._client()
started=datetime.now(timezone.utc)
result={'started_at':started.isoformat(),'scope':'READ_ONLY_METADATA_ONLY','dataset':provider.dataset,'calls':[],'timeseries_calls':0,'batch_calls':0,'broker_calls':0,'production_writes':0}
for name,kwargs in [('get_dataset_condition',{'dataset':'GLBX.MDP3','start_date':'2026-09-27','end_date':'2026-09-30'}),('get_dataset_range',{'dataset':'GLBX.MDP3'})]:
    entry={'method':'GET','endpoint':'metadata.'+name,'parameters':kwargs,'started_at':datetime.now(timezone.utc).isoformat()}
    try:
        entry['response']=getattr(client.metadata,name)(**kwargs)
        entry['status']='SUCCESS'
    except Exception as exc:
        entry['status']='ERROR'; entry['exception_type']=type(exc).__name__
    entry['finished_at']=datetime.now(timezone.utc).isoformat()
    result['calls'].append(entry)
result['finished_at']=datetime.now(timezone.utc).isoformat()
path=out/('quality-metadata-'+started.strftime('%Y%m%dT%H%M%SZ')+'.json')
with path.open('x',encoding='utf-8') as handle:handle.write(json.dumps(result,indent=2,default=str)+'\n')
print(json.dumps({'path':str(path),'calls':result['calls']},indent=2,default=str))
