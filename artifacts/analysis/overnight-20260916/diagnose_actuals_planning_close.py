"""Read-only reproduction of the failed pre-repair saved-point identity predicate."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import json,hashlib
import pandas as pd

OUT=Path(__file__).resolve().parent
RUN=Path('C:/DATASTORE/ml/gameplan-trade-plan-runs/20260915T054710.644968Z')
SOURCE=Path('C:/DATASTORE/ml/nightly-gameplan-runs/20260915T054345.154531Z')
path=RUN/'planning-price-path.json'
before=path.read_bytes()
payload=json.loads(before)
manifest=json.loads((RUN/'manifest.json').read_text())
receipt=json.loads((RUN/'receipt.json').read_text())
report=json.loads((RUN/'report.json').read_text())
forecasts=pd.read_parquet(SOURCE/'forecasts.parquet',columns=['symbol'])
manifest_hash=hashlib.sha256((RUN/'manifest.json').read_bytes()).hexdigest()
path_hash=hashlib.sha256(before).hexdigest()
assert receipt['manifest_sha256']==manifest_hash
assert manifest['output_files']['planning-price-path.json']['checksum_sha256']==path_hash
assert manifest['output_files']['planning-price-path.json']['size']==len(before)
assert receipt['source_receipt_sha256']==hashlib.sha256((SOURCE/'receipt.json').read_bytes()).hexdigest()
day=receipt['action_date']
differences=[]
missing=[]
for symbol in sorted(forecasts.symbol.unique()):
 for hour in range(4,18):
  clock=f'{hour:02}:00'
  key=f'{symbol}|{day}|{clock}'
  point=payload['points'].get(key)
  if point is None:
   missing.append(key)
   continue
  expected={'symbol':symbol,'action_date':day,'clock_local':clock,
   'timestamp':pd.Timestamp(day).tz_localize('America/Los_Angeles').replace(hour=hour).tz_convert('UTC'),
   'endpoint_kind':'observed_close' if hour==17 else 'observed_open'}
  mismatch={}
  for field,value in expected.items():
   actual=pd.Timestamp(point.get(field)) if field=='timestamp' else point.get(field)
   if actual!=value: mismatch[field]={'actual':str(actual),'expected':str(value)}
  if mismatch:
   differences.append({'key':key,'differences':mismatch,'point':{k:point.get(k) for k in ['symbol','action_date','clock_local','timestamp','endpoint_kind','status','reason','planned_price_low','planned_price_mid','planned_price_high','sample_count','observed_only_sample_count','synthetic_close_sample_count','reference_is_synthetic','reference_completion_status','reference_completion_reason']}})
assert len(differences)==11 and all(set(x['differences'])=={'endpoint_kind'} for x in differences)
assert not missing
result={'observed_at':datetime.now(timezone.utc).isoformat(),'status':'REPRODUCED_WRITER_READER_ENDPOINT_KIND_VERSION_MISMATCH',
 'source_trade_plan':str(RUN),'source_gameplan':str(SOURCE),'saved_source_bindings':{'manifest_sha256':manifest_hash,'planning_path_sha256':path_hash,'source_receipt_sha256':receipt['source_receipt_sha256'],'all_match':True},
 'writer_contract':payload['contract_version'],'reference_completion_contract':payload.get('reference_completion',{}).get('contract_version'),
 'trade_plan_schema':receipt['schema_version'],'saved_observed_at':payload['observed_at'],'source_contract':payload['price_source_contract'],'dataset':payload['price_dataset'],
 'point_count':len(payload['points']),'point_status_counts':dict(Counter(p['status'] for p in payload['points'].values())),
 'saved_projection_status':report['direction_projection_status'],
 'unavailable_symbols':sorted(set(p['symbol'] for p in payload['points'].values() if p['status']!='AVAILABLE')),
 'unavailable_price_values_all_null':all(all(p[k] is None for k in ['planned_price_low','planned_price_mid','planned_price_high']) for p in payload['points'].values() if p['status']!='AVAILABLE'),
 'failed_identity_predicate':'Pre-repair compare_price_points requires observed_close at17:00 for all path versions; v3 writer deliberately emits planning_close for every17:00 point, even unavailable or observed-only estimates.',
 'first_failure_key':differences[0]['key'],'differences':differences,'missing_points':missing,
 'field_conclusion':'All154 symbol/date/clock/timestamp identities agree; only11 endpoint_kind labels differ, each at17:00. Current writer labels planning methodology, while actuals expects legacy endpoint label.',
 'compatibility':'Permit the declared v3 planning_close estimate identity at17:00 while retaining strict clock/source checks and observed-close actual lookup with five-minute tolerance. Unavailable saved points must retain null estimates, with observed actuals allowed but no price error/range comparison. Do not rewrite historical points or supply synthetic actuals.',
 'no_mutations':path.read_bytes()==before}
(OUT/'actuals-planning-close-diagnosis.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k!='differences'},indent=2))
print(json.dumps({'first_difference':differences[0]},indent=2))
