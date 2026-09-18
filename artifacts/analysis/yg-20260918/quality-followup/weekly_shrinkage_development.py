"""One preregistered probability-shrinkage development comparison; no assessment."""
import json
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

from ml.artifacts import file_checksum
from ml.nightly_gameplan import _estimator, _model_frame, _proper_scores

OUT = Path('C:/dev/ducketz/artifacts/analysis/yg-20260918/quality-followup')
RUN = Path('C:/DATASTORE/ml/nightly-gameplan-runs/20260918T072555.813034Z')
preregistered = json.loads((OUT/'weekly-shrinkage-preregistration.json').read_text())
report = json.loads((RUN/'model-reports.json').read_text())['1w']
path = RUN/'training-cohort-1w.parquet'
clocks = pd.read_parquet(path, columns=['decision_timestamp', 'target_window_start', 'target_window_end'])
clusters = pd.Index(clocks.decision_timestamp.unique()).sort_values()
holdout = min(63, max(10, len(clusters)//8))
if len(clusters)-3*holdout < 20:
    holdout = max(5, (len(clusters)-20)//3)
train_end, selection_end, calibration_end = len(clusters)-3*holdout, len(clusters)-2*holdout, len(clusters)-holdout
assessment_start = clusters[calibration_end]
boundary = clocks.loc[clocks.decision_timestamp.ge(assessment_start), 'target_window_start'].min()
del clocks
numeric, categorical = tuple(report['features']['admitted']), ('symbol', 'route')
columns = list(dict.fromkeys([*numeric, 'symbol', 'route', 'target', 'decision_timestamp', 'target_window_start', 'target_window_end']))
data = pd.read_parquet(path, columns=columns, filters=[('decision_timestamp', '<', assessment_start)])
assert data.decision_timestamp.lt(assessment_start).all()
parts = {name: data.loc[data.decision_timestamp.isin(values)].copy() for name, values in {
    'train': clusters[:train_end], 'selection': clusters[train_end:selection_end], 'calibration': clusters[selection_end:calibration_end]}.items()}
for left, right in (('train', 'selection'), ('selection', 'calibration')):
    parts[left] = parts[left].loc[parts[left].target_window_end.lt(parts[right].target_window_start.min())]
parts['calibration'] = parts['calibration'].loc[parts['calibration'].target_window_end.lt(boundary)]
assert {name: len(frame) for name, frame in parts.items()} == {name: report['partitions'][f'{name}_rows'] for name in parts}
train, selection = parts['train'], parts['selection']
matrices = {name: _model_frame(frame, numeric, categorical) for name, frame in parts.items()}
prior = float(train.target.mean())
predictions, elapsed = {}, {}
with threadpool_limits(limits=4):
    for family in ('tree', 'neural'):
        begin = time.perf_counter()
        model = _estimator(family, numeric, categorical)
        model.fit(matrices['train'], train.target.astype(int))
        key = 'hist-gradient' if family == 'tree' else 'mlp'
        predictions[key] = model.predict_proba(matrices['selection'])[:, 1]
        elapsed[key] = time.perf_counter()-begin
    for weight in (.25, .5, .75):
        predictions[f'hist-gradient-mlp-{weight:.2f}'] = (1-weight)*predictions['hist-gradient']+weight*predictions['mlp']
    for c in (.001, .01, .1, 1.):
        begin = time.perf_counter()
        model = _estimator('logistic', numeric, categorical)
        model.set_params(classifier__C=c)
        model.fit(matrices['train'], train.target.astype(int))
        key = f'regularized-logistic-c{c:g}'
        predictions[key] = model.predict_proba(matrices['selection'])[:, 1]
        elapsed[key] = time.perf_counter()-begin
assert set(predictions) == set(preregistered['families'])
records = []
saved = selection[['symbol', 'route', 'decision_timestamp', 'target_window_start', 'target_window_end', 'target']].copy()
for name, raw in predictions.items():
    if name in report['selection_metrics']:
        assert abs(_proper_scores(selection.target.astype(int), raw)['log_loss']-report['selection_metrics'][name]['log_loss']) < 1e-9, name
    saved[name] = raw
    for weight in (1., .75, .5, .25):
        probability = weight*raw+(1-weight)*prior
        metrics = _proper_scores(selection.target.astype(int), probability)
        records.append({'family': name, 'weight': weight, **metrics,
                        'probability_min': float(probability.min()), 'probability_max': float(probability.max()),
                        'probability_std': float(probability.std())})
best = min(records, key=lambda item: item['log_loss'])
baseline = _proper_scores(selection.target.astype(int), np.full(len(selection), prior))
distribution = {}
for name, frame in parts.items():
    distribution[name] = {'rows': len(frame), 'positive_rate': float(frame.target.mean()),
                         'decision_clusters': int(frame.decision_timestamp.nunique())}
# Existing final estimator is read solely to describe untouched DEVELOPMENT calibration.
payload = joblib.load(RUN/'models/1w/model.joblib')
raw_calibration = payload['estimator'].predict_proba(matrices['calibration'])[:, 1]
distribution['saved_final_model_calibration'] = {'probability_min':float(raw_calibration.min()),
    'probability_max':float(raw_calibration.max()),'probability_mean':float(raw_calibration.mean()),
    'probability_std':float(raw_calibration.std())}
saved.to_parquet(OUT/'weekly-shrinkage-selection-predictions.parquet', index=False)
result = {'created_at': pd.Timestamp.now(tz='UTC').isoformat(), 'preregistration_sha256':file_checksum(OUT/'weekly-shrinkage-preregistration.json'),
    'assessment_labels_or_features_loaded':False, 'development_cutoff_exclusive':str(assessment_start),
    'cohort_sha256':file_checksum(path), 'training_base_rate':prior,'selection_base_rate_scores':baseline,
    'partition_distributions':distribution,'fit_elapsed_seconds':elapsed,'candidates':records,'selected':best}
(OUT/'weekly-shrinkage-development.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps({'selected':best,'baseline':baseline,'fit_elapsed_seconds':elapsed}),flush=True)
