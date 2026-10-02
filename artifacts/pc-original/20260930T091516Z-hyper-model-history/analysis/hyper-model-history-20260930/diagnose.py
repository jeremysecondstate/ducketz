import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

ROOT=Path('C:/DATASTORE/hyperliquid')
OUT=Path(__file__).parent
def read(p): return json.loads(p.read_text(encoding='utf-8'))
con=sqlite3.connect(f'file:{ROOT.as_posix()}/_paper/ledger.sqlite3?mode=ro',uri=True)
con.row_factory=sqlite3.Row
tables={t:[dict(r) for r in con.execute(f'SELECT * FROM {t} ORDER BY rowid')] for t in ['decisions','fills','cycles']}
for t in tables:
    for r in tables[t]:
        for k in list(r):
            if k.endswith('_json'):
                r[k[:-5]]=json.loads(r.pop(k))
decisions=tables['decisions']
models={}
for coin in ['BTC','ETH','HYPE','ZEC']:
    base=ROOT/'_models'/coin/'5m'/'h1'
    p=read(base/'latest_prediction.json')
    report=read(base/'runs'/p['model_id']/'report.json')
    predictions=pd.read_parquet(base/'predictions.parquet')
    models[coin]={'prediction':p,'report':report,'forward_metrics':read(base/'forward_metrics.json'),
        'prediction_columns':list(predictions.columns),'predictions':predictions.to_dict(orient='records')}
receipt={'captured_at_utc':datetime.now(timezone.utc).isoformat(),
    'policy':read(Path('configs/hyperliquid-paper.json')),
    'counts':{t:len(v) for t,v in tables.items()},
    'decision_reasons':dict(Counter(r.get('reason') for r in decisions)),
    'decision_actions':dict(Counter(r.get('action') for r in decisions)),
    'decision_examples_by_reason':{k:[r for r in decisions if r.get('reason')==k][-3:] for k in set(r.get('reason') for r in decisions)},
    'all_fills':tables['fills'],'models':models,
    'maintenance':read(ROOT/'_operations'/'paper-maintenance.json'),
    'runtime_paper':read(ROOT/'_paper'/'_runtime'/'status.json'),
    'runtime_models':read(ROOT/'_models'/'_runtime'/'status.json')}
(OUT/'diagnosis-raw.json').write_text(json.dumps(receipt,indent=2,default=str),encoding='utf-8')
print(json.dumps({k:receipt[k] for k in ['captured_at_utc','counts','decision_reasons','decision_actions']},indent=2))
for c,m in models.items():
    p,r=m['prediction'],m['report']
    print(c,json.dumps({'time':p['created_at_utc'],'model':p['model_id'],'p':p['p_not_down'],'per_model':p['per_model'],'eligible':r['eligible'],'reasons':r['eligibility_reasons'],'metrics':r['metrics'],'forward_metrics':m['forward_metrics']}))
print('example_keys',list(decisions[-1]),'last_example',json.dumps(decisions[-1]))
