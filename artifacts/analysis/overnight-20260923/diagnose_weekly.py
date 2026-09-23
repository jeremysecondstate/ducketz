"""Read-only current weekly model diagnosis; saved inference only, never fitting."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
REPO = Path('C:/dev/ducketz')
ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
CURRENT = ROOT/'ml/nightly-gameplan-runs/20260923T054513.490247Z'
PREVIOUS = ROOT/'ml/nightly-gameplan-runs/20260922T054940.677673Z'
NATIVE = ROOT/'ml/overnight-runs/20260923T040814.422433Z'
sys.path.insert(0, str(REPO))

import joblib
import numpy as np
import pandas as pd
from ml.nightly_gameplan import _chronological_partitions, _model_frame, _proper_scores, _verify_probability_cohort
from ml.gameplan_promotion import build_promotion_gate
from ml.gameplan_estimators import PriorProbabilityShrinkage

KEYS = ['symbol', 'route', 'decision_timestamp', 'target_window_start', 'target_window_end']
FIELDS = KEYS + ['observed_return', 'target', 'target_start_gap_seconds', 'target_end_gap_seconds']


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def records(frame):
    return frame.to_dict(orient='records')


def main():
    native = read(NATIVE/'stage-report.json')
    pin = native.get('enrichment_gameplan', {})
    require(pin.get('run_path') == CURRENT.relative_to(ROOT).as_posix()
        and pin.get('receipt_sha256') == sha(CURRENT/'receipt.json'), 'Current immutable source is not native-pinned')
    current_report = read(CURRENT/'model-reports.json')['1w']
    old_report = read(PREVIOUS/'model-reports.json')['1w']
    for report in (current_report, old_report):
        require(report['probability_target_contract'] == 'raw-price-direction-v1'
            and report['gameplan_variant'] == 'YG', 'Weekly target identity differs')
    current = pd.read_parquet(CURRENT/'training-cohort-1w.parquet')
    old = pd.read_parquet(PREVIOUS/'training-cohort-1w.parquet')
    for frame in (current, old):
        _verify_probability_cohort(frame, 'raw-price-direction-v1')
        require(not frame.duplicated(KEYS).any(), 'Duplicate weekly target identity')
    ci, oi = current.set_index(KEYS).sort_index(), old.set_index(KEYS).sort_index()
    common = ci.index.intersection(oi.index)
    added, removed = ci.index.difference(oi.index), oi.index.difference(ci.index)
    changed = {}
    for column in ci.columns.intersection(oi.columns):
        left, right = oi.loc[common, column], ci.loc[common, column]
        different = ~(left.eq(right) | (left.isna() & right.isna()))
        if different.any():
            changed[column] = {'count': int(different.sum()), 'examples': [list(x) for x in common[different][:5]]}
    partitions, old_parts = _chronological_partitions(current, group='1w'), _chronological_partitions(old, group='1w')
    partition_delta = {}
    for name, frame in partitions.items():
        prior = old_parts[name]
        x, y = frame.set_index(KEYS), prior.set_index(KEYS)
        partition_delta[name] = {'old_rows': len(prior), 'new_rows': len(frame),
            'old_clusters': int(prior.decision_timestamp.nunique()), 'new_clusters': int(frame.decision_timestamp.nunique()),
            'entered_rows': len(x.index.difference(y.index)), 'left_rows': len(y.index.difference(x.index)),
            'old_positive_rate': float(prior.target.mean()), 'new_positive_rate': float(frame.target.mean()),
            'first_decision': frame.decision_timestamp.min().isoformat(),
            'last_decision': frame.decision_timestamp.max().isoformat(),
            'last_target_end': frame.target_window_end.max().isoformat()}
    delta = {'old_rows': len(old), 'new_rows': len(current), 'common_rows': len(common),
        'added': records(ci.loc[added].reset_index()[FIELDS]), 'removed': records(oi.loc[removed].reset_index()[FIELDS]),
        'common_columns_changed': changed, 'common_column_count': len(ci.columns.intersection(oi.columns)),
        'new_columns': sorted(set(ci.columns)-set(oi.columns)), 'removed_columns': sorted(set(oi.columns)-set(ci.columns)),
        'partitions': partition_delta}
    (OUT/'weekly-cohort-delta.json').write_text(json.dumps(delta, indent=2, default=str)+'\n', encoding='utf-8')

    model_path = CURRENT/current_report['model_file']['path']
    payload = joblib.load(model_path)
    require(pd.to_datetime(current.target_window_end, utc=True).le(pd.Timestamp(payload['trained_at'])).all(),
        'Weekly cohort includes labels not mature at fit')
    fit = pd.concat([partitions['train'], partitions['selection']], ignore_index=True)
    assessment = partitions['assessment']
    x = _model_frame(assessment, payload['feature_columns'], payload['categorical_columns'])
    raw = payload['estimator'].predict_proba(x)[:, 1]
    probability = payload['calibrator'].predict(raw)
    scores = _proper_scores(assessment.target.to_numpy(), probability)
    raw_scores = _proper_scores(assessment.target.to_numpy(), raw)
    baseline = _proper_scores(assessment.target.to_numpy(), np.full(len(assessment), fit.target.mean()))
    errors = [abs(float(actual[k])-float(expected[k])) for actual, expected in (
        (scores, current_report['assessment']), (raw_scores, current_report['assessment_raw_scores']),
        (baseline, current_report['training_base_rate_assessment'])) for k in expected]
    require(max(errors) < 1e-12, 'Saved weekly estimator scores do not reproduce')
    estimator = payload['estimator']
    require(isinstance(estimator, PriorProbabilityShrinkage), 'Weekly expected shrinkage wrapper missing')
    require(estimator.prior_probability == float(fit.target.mean())
        and estimator.weight == current_report['selected_probability_shrinkage_weight'], 'Weekly fitted prior or shrinkage changed')
    expected_raw = estimator.weight * estimator.estimator.predict_proba(x)[:, 1] + (1-estimator.weight)*fit.target.mean()
    require(np.allclose(raw, expected_raw, rtol=0, atol=1e-15), 'Weekly shrinkage formula differs')
    calibration = partitions['calibration']
    calibration_x = _model_frame(calibration, payload['feature_columns'], payload['categorical_columns'])
    calibration_raw = estimator.predict_proba(calibration_x)[:, 1]
    calibration_prob = payload['calibrator'].predict(calibration_raw)
    ranges = {'raw_probability_range': [float(calibration_raw.min()), float(calibration_raw.max())],
        'calibrated_probability_range': [float(calibration_prob.min()), float(calibration_prob.max())],
        'assessment_probability_range': [float(probability.min()), float(probability.max())]}
    require(all(np.allclose(value, current_report['calibration_diagnostics'][name], rtol=0, atol=1e-15)
                for name, value in ranges.items()), 'Weekly probability ranges differ')
    gate = build_promotion_gate(scores, baseline, current_report['calibration_diagnostics'],
        assessment.decision_timestamp.nunique(), policy_version=current_report['promotion_gate']['policy_version'])
    require(gate == current_report['promotion_gate'], 'Weekly gate arithmetic differs')
    choices = sorted(current_report['selection_metrics'].items(), key=lambda item: item[1]['log_loss'])
    require(choices[0][0] == current_report['selected_family'], 'Weekly development winner differs')
    selection = current_report['calibration_selection']
    eligible = [name for name, value in selection['candidate_eligibility'].items() if value['eligible']]
    chosen = min(eligible, key=lambda name: selection['candidate_metrics'][name]['log_loss'])
    require(chosen == selection['selected_family'] and selection['assessment_used_for_selection'] is False,
        'Calibration selection is not the eligible development winner')
    require(selection.get('full_refit_eligibility', {}).get('eligible') is True, 'Current Platt refit lost directional information')
    # Reproduce the recorded development split without fitting either candidate.
    clusters = pd.Index(calibration.decision_timestamp.unique()).sort_values()
    midpoint = len(clusters)//2
    development_fit = calibration.loc[calibration.decision_timestamp.isin(clusters[:midpoint])]
    validation = calibration.loc[calibration.decision_timestamp.isin(clusters[midpoint:])]
    before_purge = len(development_fit)
    development_fit = development_fit.loc[development_fit.target_window_end.lt(validation.decision_timestamp.min())]
    require((len(development_fit), len(validation), before_purge-len(development_fit)) == (
        selection['fit_rows'], selection['validation_rows'], selection['purged_rows']), 'Calibration development split differs')
    identity_scores = _proper_scores(validation.target.to_numpy(), calibration_raw[calibration.index.isin(validation.index)])
    require(abs(identity_scores['log_loss']-selection['candidate_metrics']['identity']['log_loss'])<1e-12,
        'Identity calibration development score differs')
    platt = payload['calibrator']
    platt_details = {'method': platt.method, 'slope': float(platt.model.coef_.reshape(-1)[0]),
        'intercept': float(platt.model.intercept_[0]), 'raw_probability_min': platt.raw_probability_min,
        'raw_probability_max': platt.raw_probability_max, 'nondecreasing_constraint_active': platt.nondecreasing_constraint_active,
        'calibration_positive_rate': float(calibration.target.mean()), 'assessment_positive_rate': float(assessment.target.mean()),
        'assessment_probability_range': ranges['assessment_probability_range'],
        'assessment_probability_span': float(np.ptp(probability)),
        'all_current_weekly_forecasts_below_half': bool(pd.read_parquet(CURRENT/'forecasts.parquet').query("model_group == '1w'").calibrated_probability.lt(.5).all())}
    events=[]
    for line in (NATIVE/'gameplan_publication.log').read_text(encoding='utf-8').splitlines():
        if line.startswith('{'):
            try:
                item=json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get('fit', '').startswith('gameplan/1w/'):
                events.append(item)
    completed=[item for item in events if item.get('training_event')=='FIT_COMPLETE']
    warnings=[item for item in events if item.get('training_event')=='FIT_WARNING']
    result={'reviewed_at':datetime.now(timezone.utc).isoformat(),'publication':str(CURRENT),'previous_publication':str(PREVIOUS),
        'status':'VALID_RESEARCH_NOT_PROMOTED_NO_CONCRETE_REPAIR_IDENTIFIED' if not changed and not len(removed) else 'COHORT_CHANGE_REQUIRES_REVIEW',
        'probability_target_contract':'raw-price-direction-v1','gameplan_variant':'YG','gate':gate,
        'assessment_recomputed':scores,'raw_assessment_recomputed':raw_scores,'baseline_recomputed':baseline,
        'inference_score_max_abs_error':max(errors),'cohort_delta':delta,
        'admitted_features_unchanged':current_report['features']==old_report['features'],
        'selected_family':current_report['selected_family'],'previous_selected_family':old_report['selected_family'],
        'development_candidate_count':len(choices),'development_top_candidates':[{'name':name,**value} for name,value in choices[:5]],
        'development_runner_up_margin':choices[1][1]['log_loss']-choices[0][1]['log_loss'],
        'calibration_selection':selection,'previous_calibration_selection':old_report['calibration_selection'],
        'calibration_diagnostics':current_report['calibration_diagnostics'],'saved_platt_details':platt_details,
        'fit_steps_completed':len(completed),'fit_warning_count':sum(item.get('warnings',0) for item in completed),
        'training_warnings':warnings,'models_fitted_or_relabelled':False,'provider_or_broker_calls':False,
        'evidence_sha256':{str(path):sha(path) for path in (CURRENT/'receipt.json',CURRENT/'model-reports.json',
             CURRENT/'training-cohort-1w.parquet',model_path,PREVIOUS/'training-cohort-1w.parquet',Path(__file__))}}
    (OUT/'weekly-diagnosis.json').write_text(json.dumps(result,indent=2,default=str,allow_nan=False)+'\n',encoding='utf-8')
    print(json.dumps({key:result[key] for key in ('status','inference_score_max_abs_error','selected_family','development_runner_up_margin',
        'fit_steps_completed','fit_warning_count','saved_platt_details')},indent=2))
    print(json.dumps({'old_rows':len(old),'new_rows':len(current),'common_rows':len(common),'added_rows':len(added),
        'removed_rows':len(removed),'changed_columns':changed,'partitions':partition_delta},indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
