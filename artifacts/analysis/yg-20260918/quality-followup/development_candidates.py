"""Fixed, development-only repair evidence. Never loads assessment labels/features."""
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from ml.artifacts import file_checksum
from ml.gameplan_development_selection import select_development_calibrator
from ml.gameplan_probability_target import RAW_DIRECTION_TARGET
from ml.nightly_gameplan import _estimator, _model_frame, _proper_scores

ROOT = Path('C:/dev/ducketz/artifacts/analysis/yg-20260918/quality-followup')
RUN = Path('C:/DATASTORE/ml/nightly-gameplan-runs/20260918T072555.813034Z')
reports = json.loads((RUN/'model-reports.json').read_text())


def partitions(group):
    path = RUN/f'training-cohort-{group}.parquet'
    # Only already-known clock geometry is read across the entire saved cohort.
    clocks = pd.read_parquet(path, columns=['decision_timestamp', 'target_window_start', 'target_window_end'])
    clusters = pd.Index(clocks.decision_timestamp.unique()).sort_values()
    holdout = min(63, max(10, len(clusters)//8))
    if len(clusters)-3*holdout < 20:
        holdout = max(5, (len(clusters)-20)//3)
    train_end, select_end, calibration_end = len(clusters)-3*holdout, len(clusters)-2*holdout, len(clusters)-holdout
    assessment_start = clusters[calibration_end]
    boundary = clocks.loc[clocks.decision_timestamp.ge(assessment_start), 'target_window_start'].min()
    del clocks
    report = reports[group]
    columns = list(dict.fromkeys([*report['features']['admitted'], 'symbol', 'route', 'target',
                                 'decision_timestamp', 'target_window_start', 'target_window_end']))
    development = pd.read_parquet(path, columns=columns, filters=[('decision_timestamp', '<', assessment_start)])
    assert development.decision_timestamp.lt(assessment_start).all()
    parts = {
        'train': development.loc[development.decision_timestamp.isin(clusters[:train_end])].copy(),
        'selection': development.loc[development.decision_timestamp.isin(clusters[train_end:select_end])].copy(),
        'calibration': development.loc[development.decision_timestamp.isin(clusters[select_end:calibration_end])].copy(),
    }
    for left, right in (('train', 'selection'), ('selection', 'calibration')):
        parts[left] = parts[left].loc[parts[left].target_window_end.lt(parts[right].target_window_start.min())]
    parts['calibration'] = parts['calibration'].loc[parts['calibration'].target_window_end.lt(boundary)]
    assert {name: len(frame) for name, frame in parts.items()} == {
        name: report['partitions'][f'{name}_rows'] for name in parts}
    return parts, {'assessment_labels_or_features_loaded': False,
                   'development_decision_cutoff_exclusive': str(assessment_start),
                   'development_rows': {name: len(frame) for name, frame in parts.items()},
                   'cohort_sha256': file_checksum(path)}


result = {'created_at': pd.Timestamp.now(tz='UTC').isoformat(), 'source_run': str(RUN),
          'report_sha256': file_checksum(RUN/'model-reports.json'),
          'assessment_used_for_selection': False,
          'fixed_grid': [.001, .01, .1, 1.], 'groups': {}}
for group in ('1h', '1w'):
    parts, evidence = partitions(group)
    report = reports[group]
    numeric, categorical = tuple(report['features']['admitted']), ('symbol', 'route')
    if group == '1w':
        scores = {}
        for c in result['fixed_grid']:
            candidate = _estimator('logistic', numeric, categorical)
            candidate.set_params(classifier__C=c)
            candidate.fit(_model_frame(parts['train'], numeric, categorical), parts['train'].target.astype(int))
            probability = candidate.predict_proba(_model_frame(parts['selection'], numeric, categorical))[:, 1]
            scores[f'regularized-logistic-c{c:g}'] = _proper_scores(parts['selection'].target.astype(int), probability)
            print(json.dumps({'group': group, 'C': c, 'development_log_loss': scores[f'regularized-logistic-c{c:g}']['log_loss']}), flush=True)
        assert abs(scores['regularized-logistic-c1']['log_loss']-report['selection_metrics']['regularized-logistic-c1']['log_loss']) < 1e-10
        combined = {**report['selection_metrics'], **scores}
        chosen = min(combined, key=lambda name: combined[name]['log_loss'])
        evidence.update(fixed_grid_selection_metrics=scores, selected_family_on_development=chosen,
                        selected_development_log_loss=combined[chosen]['log_loss'],
                        previous_selected_family=report['selected_family'])
    else:
        # The saved estimator was fitted on train+selection; calibration is its
        # untouched development calibration cohort. No estimator is fitted here.
        payload = joblib.load(RUN/'models/1h/model.joblib')
        calibration = parts['calibration']
        raw = payload['estimator'].predict_proba(_model_frame(calibration, numeric, categorical))[:, 1]
        calibrator, new_selection = select_development_calibrator(
            calibration, raw, probability_target=RAW_DIRECTION_TARGET)
        old = payload['calibrator'].predict(raw)
        evidence.update(previous_selected_family=report['calibration_selection']['selected_family'],
                        previous_full_calibration_probability_span=float(np.ptp(old)),
                        raw_full_calibration_probability_span=float(np.ptp(raw)),
                        repaired_selection=new_selection,
                        repaired_full_calibration_probability_span=float(np.ptp(calibrator.predict(raw))))
    result['groups'][group] = evidence
(ROOT/'development-evidence.json').write_text(json.dumps(result, indent=2, default=str)+'\n')
print(json.dumps({'status': 'DEVELOPMENT_EVIDENCE_COMPLETE', 'output': str(ROOT/'development-evidence.json')}), flush=True)
