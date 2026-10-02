import json, hashlib
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter
import pandas as pd

OUT=Path(__file__).parent
d=json.loads((OUT/'diagnosis-raw.json').read_text())
comparison=OUT/'round12-endpoint-20260930T090600Z.json'
c=json.loads(comparison.read_text())
decisions=d['decision_examples_by_reason']
con=__import__('sqlite3').connect('file:C:/DATASTORE/hyperliquid/_paper/ledger.sqlite3?mode=ro',uri=True)
con.row_factory=__import__('sqlite3').Row
unavailable=[]
for row in con.execute("SELECT * FROM decisions WHERE reason='qualified_forecast_unavailable' AND timestamp_utc<=? ORDER BY rowid",(d['captured_at_utc'],)):
    row=dict(row)
    details=json.loads(row['details_json'])
    row={k:v for k,v in row.items() if k!='details_json'}
    row['specific_error']=details.get('policy',{}).get('reason')
    coin=row['coin']
    publications=d['models'][coin]['predictions']
    nexts=[p for p in publications if pd.Timestamp(p['created_at_utc'])>pd.Timestamp(row['timestamp_utc'])]
    prior=[p for p in publications if pd.Timestamp(p['created_at_utc'])<=pd.Timestamp(row['timestamp_utc'])]
    row['previous_prediction']={k:prior[-1].get(k) for k in ['prediction_id','created_at_utc','target_close_utc']} if prior else None
    row['next_prediction']={k:nexts[0].get(k) for k in ['prediction_id','created_at_utc','decision_close_utc']} if nexts else None
    unavailable.append(row)
models={}
for coin,item in d['models'].items():
    p=item['prediction'];r=item['report']; ps=item['predictions']
    lags=[(pd.Timestamp(x['created_at_utc'])-pd.Timestamp(x['decision_close_utc'])).total_seconds() for x in ps]
    in_round=[x for x in ps if pd.Timestamp(x['created_at_utc'])>=pd.Timestamp(c['seed_at_utc'])]
    live_lags=[(pd.Timestamp(x['created_at_utc'])-pd.Timestamp(x['decision_close_utc'])).total_seconds() for x in in_round]
    models[coin]={'model_id':p['model_id'],'prediction_id':p['prediction_id'],'decision_close_utc':p['decision_close_utc'],
        'created_at_utc':p['created_at_utc'],'target_close_utc':p['target_close_utc'],'p_not_down':p['p_not_down'],
        'qualification':p['qualified'],'eligibility_reasons':r['eligibility_reasons'],
        'ensemble_weights':r['ensemble_weights'],'metrics':r['metrics'],'per_model':p['per_model'],
        'published_count':len(ps),'qualified_count':sum(x['qualified'] is True for x in ps),
        'in_round_published_count':len(in_round),
        'in_round_delay_after_close_seconds':{'min':min(live_lags),'max':max(live_lags),'mean':sum(live_lags)/len(live_lags)},
        'created_delay_after_close_seconds':{'min':min(lags),'max':max(lags),'mean':sum(lags)/len(lags)},
        'p_not_down_range':[min(x['p_not_down'] for x in ps),max(x['p_not_down'] for x in ps)],
        'forward_metrics':item['forward_metrics']}
due=pd.Timestamp('2026-09-30T09:05:03.926644+00:00')
receipt={'captured_at_utc':d['captured_at_utc'],'diagnostic_only':True,'no_scoring_or_runtime_mutation':True,
    'decision_counts':d['counts'],'decision_reasons':d['decision_reasons'],'decision_actions':d['decision_actions'],
    'missing_forecast_specific_errors':dict(Counter(x['specific_error'] for x in unavailable)),
    'unavailable_observations':unavailable,'models':models,
    'endpoint':{'path':str(comparison.resolve()),'sha256':hashlib.sha256(comparison.read_bytes()).hexdigest(),
        'paper_observed_at_utc':c['paper']['observed_at_utc'],'actual_completed_at_utc':c['actual']['completed_at_utc'],
        'paper_seconds_after_due':(pd.Timestamp(c['paper']['observed_at_utc'])-due).total_seconds(),
        'actual_seconds_after_due':(pd.Timestamp(c['actual']['completed_at_utc'])-due).total_seconds(),
        'comparison':c['comparison'],'forecast_evidence':c['paper']['forecast_evidence']}}
(OUT/'diagnosis.json').write_text(json.dumps(receipt,indent=2),encoding='utf-8')
print(json.dumps({'errors':receipt['missing_forecast_specific_errors'],'sample_0200':[x for x in unavailable if '09:00:' in x['timestamp_utc']],'model_timings':{k:{x:v[x] for x in ['published_count','qualified_count','created_delay_after_close_seconds','p_not_down_range']} for k,v in models.items()},'endpoint':receipt['endpoint']},indent=2))
