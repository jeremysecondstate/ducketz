"""User-authorized COST-only Historical/Live diagnostic; no production publication."""
import json, math, os, shutil, sys
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
sys.path.insert(0, str(Path.cwd()))
import databento as db
from datafetching.cme_runtime import load_repository_environment
import datafetching.databento_xnas_replay as replay
# Diagnostic process only: preserve the distinct EQUS.MINI identity.
replay.DATASET = 'EQUS.MINI'
replay.DELIVERY_VERSION = 'diagnostic-equs-mini-live-replay-v1'
from datafetching.databento_xnas_replay import capture_replay, _external_failure
from datafetching.xnas_replay_archive import _normalize

from ml.artifacts import file_checksum

out=Path(__file__).resolve().parent
request={'dataset':'EQUS.MINI','schema':'ohlcv-1m','symbols':['COST'],'stype_in':'raw_symbol','start':'2026-09-10T23:30:00+00:00','end':'2026-09-11T00:00:00+00:00'}
def save(name,obj):
 (out/name).write_text(json.dumps(obj,indent=2,default=str,allow_nan=False)+'\n',encoding='utf-8')
def summary(frame):
 return {'rows':len(frame),'timestamps':frame.timestamp.astype(str).tolist(),'last_close_observed_at':str(frame.timestamp.max()+pd.Timedelta(minutes=1)) if len(frame) else None,'valid_closing_observation':bool(((frame.timestamp+pd.Timedelta(minutes=1))>=pd.Timestamp('2026-09-10T23:55:00Z')).any())}
load_repository_environment()
key=os.environ.get('DATABENTO_API_KEY','').strip()
if not key: raise SystemExit('DATABENTO_API_KEY unavailable')
client=db.Historical(key)
report={'observed_at':datetime.now(timezone.utc).isoformat(),'scope':request,'orders_placed':0,'production_publication':False,'user_authorized_retries':True}
try:
 cost=float(client.metadata.get_cost(**request))
 size=int(client.metadata.get_billable_size(**request))
 count=int(client.metadata.get_record_count(**request))
 free=shutil.disk_usage(out).free
 preflight={'request':request,'estimated_cost_usd':cost,'estimated_bytes':size,'record_count':count,'available_bytes':free,'max_bytes':64*1024**2,'capacity_pass':0<=size<=64*1024**2 and free>5*1024**3+size*4,'timestamp':datetime.now(timezone.utc).isoformat()}
 save('historical-preflight.json',preflight)
 if not math.isfinite(cost) or cost!=0 or not preflight['capacity_pass']: raise ValueError('Exact zero-dollar or capacity preflight failed')
 native=client.timeseries.get_range(**request,path=out/'historical.dbn')
 metadata=native.metadata
 assert str(metadata.dataset)=='EQUS.MINI' and str(metadata.schema)=='ohlcv-1m'
 assert int(metadata.start)==pd.Timestamp(request['start']).value and int(metadata.end)==pd.Timestamp(request['end']).value
 assert not metadata.partial and not metadata.not_found and tuple(metadata.symbols)==('COST',)
 native.reader.close()
 frame=_normalize(out/'historical.dbn',symbol='COST',start=pd.Timestamp(request['start']),end=pd.Timestamp(request['end']))
 frame.to_parquet(out/'historical.parquet',index=False)
 report['historical']={'status':'COMPLETE',**summary(frame),'raw_sha256':file_checksum(out/'historical.dbn'),'normalized_sha256':file_checksum(out/'historical.parquet')}
except Exception as exc:
 report['historical']={'status':'FAILED','error':_external_failure(exc)}
save('comparison.json',report)
print(json.dumps({'historical':report['historical']}),flush=True)
try:
 preflight={'request':request,'historical_quote_is_not_live_entitlement':True,'requested_at':datetime.now(timezone.utc).isoformat()}
 save('live-preflight.json', {**preflight, 'entitlement_evidence': 'docs/databento-plan/DB-US-EQUITIES/STANDARD=PLAN-3.png', 'plan': 'Standard Databento US Equities Mini', 'no_subscription_change': True})
 delivery=capture_replay(key,'ohlcv-1m',['COST'],request['start'],request['end'],out/'live.dbn',64*1024**2,timeout_seconds=60)
 save('live-delivery.json',delivery)
 frame=_normalize(out/'live.dbn',symbol='COST',start=pd.Timestamp(request['start']),end=pd.Timestamp(request['end']))
 frame.to_parquet(out/'live.parquet',index=False)
 report['live']={'status':'COMPLETE',**summary(frame),'raw_sha256':file_checksum(out/'live.dbn'),'normalized_sha256':file_checksum(out/'live.parquet')}
 if report['historical']['status']=='COMPLETE':
  historical=pd.read_parquet(out/'historical.parquet')
  report['both_complete_and_identical']=historical.equals(frame)
except Exception as exc:
 report['live']={'status':'FAILED','error':str(exc) if type(exc).__name__=='ReplayCaptureError' else _external_failure(exc)}
 report['both_complete_and_identical']=False
report['finished_at']=datetime.now(timezone.utc).isoformat()
save('comparison.json',report)
print(json.dumps(report),flush=True)
