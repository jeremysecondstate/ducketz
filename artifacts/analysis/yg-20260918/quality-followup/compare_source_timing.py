"""One preregistered source-timing experiment on weekly train/selection only."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import warnings

import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

sys.path.insert(0,str(Path(__file__).resolve().parents[4]))
from ml.nightly_gameplan import _estimator, _model_frame, _proper_scores

RUN=Path('C:/DATASTORE/ml/nightly-gameplan-runs/20260918T072555.813034Z')
OUT=Path(__file__).resolve().parent
PREFIX='weekly-source-timing'
NEW=('source__hours_after_regular_close','source__hours_until_action_start')
GRID=(0.001,0.01,0.1,1.0)


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path,data):
    path.write_text(json.dumps(data,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def add_timing(frame):
    result=frame.copy()
    end=pd.to_datetime(result.source_bar_end_timestamp,utc=True)
    regular=pd.to_datetime(result.source_regular_close,utc=True)
    action=pd.to_datetime(result.source_action_start,utc=True)
    result[NEW[0]]=(end-regular).dt.total_seconds()/3600
    result[NEW[1]]=(action-end).dt.total_seconds()/3600
    if not np.isfinite(result[list(NEW)]).all().all() or not result[NEW[0]].ge(0).all() or not result[NEW[1]].gt(0).all():
        raise RuntimeError('Causal source timing fields are invalid')
    return result


result_path=OUT/f'{PREFIX}-result.json'
spec_path=OUT/f'{PREFIX}-preregister.json'
if result_path.exists() or spec_path.exists():
    raise RuntimeError('This fixed experiment was already registered; no implicit rerun or expansion')
cohort_path=RUN/'training-cohort-1w.parquet'
report=read(RUN/'model-reports.json')['1w']
clocks=pd.read_parquet(cohort_path,columns=['decision_timestamp','target_window_start'])
clusters=pd.Index(clocks.decision_timestamp.unique()).sort_values()
holdout=min(63,max(10,len(clusters)//8))
if len(clusters)-3*holdout<20:
    holdout=max(5,(len(clusters)-20)//3)
end0,end1=len(clusters)-3*holdout,len(clusters)-2*holdout
train_clusters=clusters[:end0]
selection_clusters=clusters[end0:end1]
calibration_clusters=clusters[end1:len(clusters)-holdout]
selection_start=clocks[clocks.decision_timestamp.isin(selection_clusters)].target_window_start.min()
calibration_start=clocks[clocks.decision_timestamp.isin(calibration_clusters)].target_window_start.min()
frame=pd.read_parquet(cohort_path,filters=[('decision_timestamp','<',calibration_clusters[0])])
train=frame[frame.decision_timestamp.isin(train_clusters) & frame.target_window_end.lt(selection_start)].copy()
selection=frame[frame.decision_timestamp.isin(selection_clusters) & frame.target_window_end.lt(calibration_start)].copy()
assert len(train)==report['partitions']['train_rows']==1561
assert len(selection)==report['partitions']['selection_rows']==174
train,selection=add_timing(train),add_timing(selection)
features=tuple(report['features']['admitted'])+NEW
spec={'registered_at':datetime.now(timezone.utc).isoformat(),'status':'PREREGISTERED_BEFORE_ANY_FIT',
    'source_run':str(RUN),'cohort_sha256':sha(cohort_path),'train_rows':len(train),'selection_rows':len(selection),
    'change':'Add exactly two causal source timing fields to the original admitted weekly features; all original features retained.',
    'feature_derivations':{NEW[0]:'(source_bar_end_timestamp-source_regular_close).total_seconds()/3600',
        NEW[1]:'(source_action_start-source_bar_end_timestamp).total_seconds()/3600'},
    'unchanged_families':['hist-gradient','mlp','hist-gradient-mlp-0.25','hist-gradient-mlp-0.50','hist-gradient-mlp-0.75'],
    'fixed_logistic_C_grid':list(GRID),'selection_basis':'minimum original selection-partition log loss; fixed original family order breaks ties',
    'assessment_policy':'No assessment features or labels loaded. Only label-free global clocks set the original chronological boundaries.',
    'calibration_policy':'No calibration fit, refit, or score in this experiment.',
    'independence':'No probability-shrinkage candidate, no combination with the other independent development study.',
    'source_file_sha256':{'ml/nightly_gameplan.py':sha(Path('ml/nightly_gameplan.py'))},
    'authorization':'Root explicitly authorized one fixed DEVELOPMENT-only weekly comparison; no production feature integration until review.'}
write(spec_path,spec)
x_train=_model_frame(train,features,('symbol','route'))
x_selection=_model_frame(selection,features,('symbol','route'))
y=train.target.to_numpy(dtype=int)
predictions={}
fit_warnings=[]
with threadpool_limits(limits=2):
    with warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter('always')
        tree=_estimator('tree',features,('symbol','route'))
        neural=_estimator('neural',features,('symbol','route'))
        tree.fit(x_train,y)
        neural.fit(x_train,y)
        predictions['hist-gradient']=tree.predict_proba(x_selection)[:,1]
        predictions['mlp']=neural.predict_proba(x_selection)[:,1]
        for weight in (0.25,0.50,0.75):
            predictions[f'hist-gradient-mlp-{weight:.2f}']=(1-weight)*predictions['hist-gradient']+weight*predictions['mlp']
        for c in GRID:
            model=_estimator('logistic',features,('symbol','route'))
            model.set_params(classifier__C=c)
            model.fit(x_train,y)
            predictions[f'regularized-logistic-c{c:g}']=model.predict_proba(x_selection)[:,1]
        fit_warnings=[str(w.message) for w in observed]
metrics={name:{**_proper_scores(selection.target.to_numpy(),prob),
    'probability_range':[float(prob.min()),float(prob.max())],
    'probability_quantiles':np.quantile(prob,[.05,.25,.5,.75,.95]).tolist(),
    'probability_std':float(np.std(prob))} for name,prob in predictions.items()}
winner=min(metrics,key=lambda name:metrics[name]['log_loss'])
baseline_family=min(report['selection_metrics'],key=lambda name:report['selection_metrics'][name]['log_loss'])
baseline_score=report['selection_metrics'][baseline_family]
current=pd.read_parquet(RUN/'forecasts.parquet',columns=['symbol','source_bar_end_timestamp','source_regular_close','source_action_start']).drop_duplicates('symbol')
current=add_timing(current)
out={'completed_at':datetime.now(timezone.utc).isoformat(),'preregistration_sha256':sha(spec_path),
    'source_run':str(RUN),'train_rows':len(train),'selection_rows':len(selection),
    'original_selected_family':baseline_family,'original_selection_scores':baseline_score,
    'timing_selected_family':winner,'timing_selection_scores':metrics[winner],
    'selection_log_loss_change':metrics[winner]['log_loss']-baseline_score['log_loss'],
    'source_timing_candidate_beats_original_development_winner':metrics[winner]['log_loss']<baseline_score['log_loss'],
    'candidate_metrics':metrics,'fit_warnings':fit_warnings,'final_assessment_touched':False,
    'calibration_touched':False,'production_changed':False,
    'current_feature_values':json.loads(current[['symbol',*NEW]].to_json(orient='records')),
    'interpretation':'One independent fixed development experiment. Any selection improvement is not evidence of final-assessment success. No post-hoc source/shrinkage combination or candidate expansion was performed.'}
table=selection[['symbol','route','decision_timestamp','target_window_start','target_window_end','target']].reset_index(drop=True)
for name,prob in predictions.items():
    table[name]=prob
table.to_parquet(OUT/f'{PREFIX}-selection-predictions.parquet',index=False)
write(result_path,out)
lines=['# Fixed weekly source-timing development comparison','',
    f"Preregistered {spec['registered_at']}; completed {out['completed_at']}.",'',
    'Train 1,561 rows; selection 174 rows. Calibration and final assessment were not loaded or scored. Original chronology, model hyperparameters, admitted features and target labels are retained; exactly two source timing features were added. No shrinkage combination was tested.','',
    f"Best source-timing candidate: **{winner}**, development log loss **{metrics[winner]['log_loss']:.12f}**; original winner {baseline_family}, **{baseline_score['log_loss']:.12f}**. Change **{out['selection_log_loss_change']:+.12f}**.",'',
    '| Candidate | Selection log loss | Brier | Probability min–max |','|---|---:|---:|---|']
for name,m in metrics.items():
    lines.append(f"| {name} | {m['log_loss']:.12f} | {m['brier_score']:.12f} | {m['probability_range'][0]:.6f}–{m['probability_range'][1]:.6f} |")
lines+=['',out['interpretation'],'',
    'At the frozen current source, every configured symbol has source__hours_after_regular_close=4 and source__hours_until_action_start=11. The same formula uses saved historical/current timestamps and requires no fetch or prices. All feature availability evidence and prediction distributions are in the JSON; selection predictions are retained in the Parquet.']
(OUT/f'{PREFIX}-result.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({k:out[k] for k in ('timing_selected_family','timing_selection_scores','selection_log_loss_change','source_timing_candidate_beats_original_development_winner','fit_warnings')},indent=2))
