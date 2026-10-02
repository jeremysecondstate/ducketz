"""Bounded report arithmetic and 264-row direction review; no fit or model inference."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json
import sys

OUT = Path(__file__).resolve().parent
REPO = Path('<LOCAL_CHECKOUT>')
ROOT = Path('<LOCAL_DATASTORE>')
GAME = ROOT/'ml/nightly-gameplan-runs/20261001T064119.779051Z'
SIZING = ROOT/'ml/stock-trader-model-runs/20261001T065134.405601Z'
sys.path.insert(0, str(REPO))
import pandas as pd
from ml.stock_direction_policy import stock_direction

read = lambda p: json.loads(p.read_text(encoding='utf-8-sig'))
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
report = read(SIZING/'training-report.json')
receipt = read(SIZING/'receipt.json')
for name, key in (('manifest.json','manifest_sha256'), ('training-report.json','training_report_sha256')):
    assert sha(SIZING/name) == receipt[key]
assert report['source_gameplan_run'] == GAME.relative_to(ROOT).as_posix()
assert report['orders_placed'] == 0 and report['broker_orders_enabled'] is False
groups = {}
for group, info in report['horizons'].items():
    assert info['status'] == 'FITTED'
    support = info['scope_readiness']
    score = next(iter(support.values()))['horizon_assessment_scores']
    assert all(item['horizon_assessment_scores'] == score for item in support.values())
    quality = {'brier_below_baseline': score['brier'] < score['base_brier'],
               'logloss_below_baseline': score['log_loss'] < score['base_log_loss'],
               'ece_at_most_0_15': score['ece'] <= .15,
               'return_mse_below_baseline': score['return_mse'] < score['base_return_mse'],
               'adverse_mse_at_most_baseline': score['adverse_mse'] <= score['base_adverse_mse'],
               'varying_probability': score['probability_range'][1]-score['probability_range'][0] > 1e-8,
               'assessment_at_least_10_clusters': info['partition_evidence']['assessment']['decision_clusters'] >= 10}
    global_ready = all(quality.values())
    for scope, item in support.items():
        ready = item['fit_decision_clusters'] >= 1 and item['pooled_symbol_route_fit_decision_clusters'] >= 20 and global_ready
        assert (item['status'] == 'READY') == ready
    assert info['qualified_scope_count'] == sum(x['status']=='READY' for x in support.values())
    assert info['fitted_scope_count'] == sum(x['fit_decision_clusters'] > 0 for x in support.values())
    head = info['head_development_selection']
    assert head['assessment_used_for_selection'] is False
    for name, objective in head['head_objectives'].items():
        selected = min(head['candidate_penalties'], key=lambda p: head['candidate_metrics'][str(p)][objective])
        assert head['selected_penalties'][name] == selected
    calibration = info['calibration_selection']
    assert calibration['assessment_used_for_selection'] is False
    selected = min(('identity','platt'), key=lambda name: calibration['candidate_metrics'][name]['log_loss'])
    assert selected == calibration['selected_family']
    assert all(value['converged'] for value in info['head_training_objectives'].values())
    parts = info['partition_evidence']
    for left, right in zip(('train','selection','calibration'), ('selection','calibration','assessment')):
        assert pd.Timestamp(parts[left]['last_target_end']) < pd.Timestamp(parts[right]['first_decision'])
    admission = info['market_feature_admission']
    assert admission['input_execution_rows'] == admission['admitted_rows'] + admission['excluded_missing_market_rows']
    assert admission['admitted_rows'] == info['admitted_rows']
    groups[group] = {'status': info['status'], 'fitted_scope_count': info['fitted_scope_count'],
        'qualified_scope_count': info['qualified_scope_count'], 'reason_counts': dict(Counter(x['reason'] for x in support.values())),
        'horizon_assessment_scores': score, 'quality_checks': quality,
        'failed_quality_checks': [name for name, passed in quality.items() if not passed],
        'development_selection_reproduced_from_saved_metrics': True, 'calibration_selection': selected,
        'reported_convergence': info['head_training_objectives'], 'partition_evidence': parts,
        'market_feature_admission': admission,
        'repair_diagnosis': 'Saved selectors, convergence, partition summaries and strict qualification arithmetic agree. No concrete implementation defect is established by these quality shortfalls. Re-selecting on final assessment or weakening a criterion is not a justified repair.'}

forecasts = pd.read_parquet(GAME/'forecasts.parquet')
models = read(GAME/'model-reports.json')
expected = forecasts.calibrated_probability.map(stock_direction)
assert forecasts.direction.eq(expected).all()
assert forecasts.direction_policy_version.eq('stock-direction-50-v2').all()
assert forecasts.direction_up_threshold.eq(.5).all() and forecasts.direction_down_threshold.eq(.5).all()
assert forecasts.model_status.eq('PROMOTED').all() and len(forecasts) == 264
assert forecasts.symbol_fitted_target_rows.gt(0).all() and forecasts.symbol_route_fitted_target_rows.gt(0).all()
for row in forecasts.itertuples():
    support = models[row.model_group]['target_support_by_symbol'][row.symbol]
    assert row.symbol_fitted_target_rows == support['fitted_rows']
    assert row.symbol_route_fitted_target_rows == support['fitted_rows_by_route'][row.route]
record = {'reviewed_at': datetime.now(timezone.utc).isoformat(), 'status': 'BOUNDED_SAVED_REPORT_CHECKS_PASS_WITH_RESEARCH_SIZING',
    'gameplan_run':str(GAME), 'enrichment_run':str(SIZING), 'source_gameplan_match':True,
    'manual_direction': {'policy':'stock-direction-50-v2', 'rows':len(forecasts),
        'full_saved_probability_classification':'STRICTLY_ABOVE_BELOW_50_EXACT_TIE_NEUTRAL',
        'counts':forecasts.direction.value_counts().to_dict(), 'all_forecasts_promoted':True,
        'all_exact_fitted_symbol_and_route_support_positive':True,
        'minimum_fitted_route_rows_by_group':forecasts.groupby('model_group').symbol_route_fitted_target_rows.min().to_dict(),
        'saved_report_and_forecast_support_agree':True},
    'learned_sizing': {'status':report['status'], 'supported_horizons':report['supported_horizons'],
        'qualified_target_contracts':report['qualified_target_contracts'], 'horizons':groups},
    'repair_finding':'No concrete data/fitting implementation repair identified in bounded report evidence. All directional groups pass current v2; all learned sizing groups remain honestly research under stricter gates.',
    'limits':['No duplicate model inference, cohort reproduction or training; root final audit remains required.',
              'Saved report arithmetic is not independent empirical assessment or proof of a profitable strategy.',
              'Some exact directional support is very sparse; the current contract admits positive support and reports its count.'],
    'evidence':{str(p):{'sha256':sha(p),'bytes':p.stat().st_size} for p in
        (SIZING/'receipt.json',SIZING/'manifest.json',SIZING/'training-report.json',GAME/'model-reports.json',GAME/'forecasts.parquet')},
    'source_modifications':0,'provider_calls':0,'heavy_final_audit_started':False,'orders_placed':0}
destination=OUT/'fresh-sizing-and-direction-review.json'
assert not destination.exists()
destination.write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
print(json.dumps({'status':record['status'],'manual_direction':record['manual_direction'],
    'sizing':{h:{'fit':g['fitted_scope_count'],'qualified':g['qualified_scope_count'], 'failed':g['failed_quality_checks']} for h,g in groups.items()},
    'output':str(destination)},indent=2))
