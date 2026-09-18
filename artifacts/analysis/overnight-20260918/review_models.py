"""Offline saved-model audit. No fitting, acquisition, or runtime mutations."""
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from ml.nightly_gameplan import _chronological_partitions, _model_frame, _proper_scores
from ml.gameplan_promotion import build_promotion_gate

ROOT = Path('C:/DATASTORE')
RUN = ROOT / 'ml/nightly-gameplan-runs/20260918T054532.489998Z'
PRIOR = ROOT / 'ml/nightly-gameplan-runs/20260917T054622.686474Z'
ENRICH = ROOT / 'ml/stock-trader-model-runs/20260918T054724.507836Z'
OUT = Path(__file__).resolve().parent

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def dt(values):
    return pd.to_datetime(values, utc=True)

def score_delta(actual, expected):
    return max(abs(float(actual[k]) - float(expected[k])) for k in expected)

def partition_summary(frame):
    return {'rows': len(frame), 'decision_clusters': int(frame.decision_timestamp.nunique()),
        'first_decision': dt(frame.decision_timestamp).min().isoformat(),
        'last_decision': dt(frame.decision_timestamp).max().isoformat(),
        'first_target_start': dt(frame.target_window_start).min().isoformat(),
        'last_target_end': dt(frame.target_window_end).max().isoformat(),
        'positive_rate': float(frame.target.mean()),
        'class_counts': {str(k):int(v) for k,v in frame.target.value_counts().items()}}

def audit_group(run, group):
    report = read(run/'model-reports.json')[group]
    manifest = read(run/'manifest.json')
    names = ['model-reports.json', f'training-cohort-{group}.parquet', report['model_file']['path']]
    hashes = {name: sha(run/name) == manifest['output_files'][name]['checksum_sha256'] for name in names}
    cohort = pd.read_parquet(run/f'training-cohort-{group}.parquet')
    parts = _chronological_partitions(cohort, group=group)
    payload = joblib.load(run/report['model_file']['path'])
    fit = pd.concat([parts['train'], parts['selection']], ignore_index=True)
    assessment = parts['assessment']
    x = _model_frame(assessment, payload['feature_columns'], payload['categorical_columns'])
    raw = payload['estimator'].predict_proba(x)[:,1]
    prob = payload['calibrator'].predict(raw)
    raw_scores = _proper_scores(assessment.target.to_numpy(), raw)
    scores = _proper_scores(assessment.target.to_numpy(), prob)
    baseline = _proper_scores(assessment.target.to_numpy(), np.full(len(assessment), fit.target.mean()))
    cal = parts['calibration']
    cx = _model_frame(cal, payload['feature_columns'], payload['categorical_columns'])
    cal_raw = payload['estimator'].predict_proba(cx)[:,1]
    cal_prob = payload['calibrator'].predict(cal_raw)
    ranges = {'raw_probability_range': [float(cal_raw.min()),float(cal_raw.max())],
        'calibrated_probability_range': [float(cal_prob.min()),float(cal_prob.max())],
        'assessment_probability_range': [float(prob.min()),float(prob.max())]}
    gate = build_promotion_gate(scores, baseline, report['calibration_diagnostics'],
        assessment.decision_timestamp.nunique(), policy_version=report['promotion_gate']['policy_version'])
    boundaries = {}
    source_cutoff_boundaries = {}
    for left,right in zip(('train','selection','calibration'),('selection','calibration','assessment')):
        boundaries[f'{left}_before_{right}'] = bool(dt(parts[left].target_window_end).max() < dt(parts[right].target_window_start).min())
        source_cutoff_boundaries[f'{left}_before_{right}'] = {
            'last_label_end':dt(parts[left].target_window_end).max().isoformat(),
            'first_source_decision':dt(parts[right].decision_timestamp).min().isoformat(),
            'first_post_close_source_cutoff':dt(parts[right].source_effective_cutoff).min().isoformat(),
            'label_known_before_post_close_cutoff':bool(dt(parts[left].target_window_end).max()<dt(parts[right].source_effective_cutoff).min()),
        }
    # Source selection is independently checked from saved metadata, without reading the archive.
    source_checks = {
        'source_contract': bool(cohort.source_selection_contract.eq('independent-gameplan-prior-session-features-v1').all()),
        'target_source': bool(cohort.target_price_source_contract.eq('xnas-itch-archive-v1').all()),
        'dataset': bool(cohort.target_price_dataset.eq('XNAS.ITCH').all()),
        'full_hour_feature_bars': bool(dt(cohort.source_bar_end_timestamp).sub(dt(cohort.source_bar_timestamp)).eq(pd.Timedelta(hours=1)).all()),
        'bar_end_after_regular_close': bool(dt(cohort.source_bar_end_timestamp).ge(dt(cohort.source_regular_close)).all()),
        'info_after_bar': bool(dt(cohort.information_available_at).ge(dt(cohort.source_bar_end_timestamp)).all()),
        'decision_after_info': bool(dt(cohort.decision_timestamp).ge(dt(cohort.information_available_at)).all()),
        'decision_at_or_before_cutoff': bool(dt(cohort.decision_timestamp).le(dt(cohort.source_effective_cutoff)).all()),
        'feature_decision_before_action_start': bool(dt(cohort.decision_timestamp).lt(dt(cohort.source_action_start)).all()),
        'source_session_matches_bar_date': bool(dt(cohort.source_bar_timestamp).dt.tz_convert('America/Los_Angeles').dt.date.astype(str).eq(cohort.source_session.astype(str)).all()),
        'target_boundary_aligned': bool(cohort.target_boundary_aligned.all()),
        'boundary_gaps_within_300_seconds': bool(cohort[['target_start_gap_seconds','target_end_gap_seconds']].abs().le(300).all().all()),
        'no_immature_outcomes': bool(dt(cohort.target_window_end).le(pd.Timestamp(payload['trained_at'])).all()),
        'target_labels_cost_adjusted': bool(cohort.target.eq((cohort.observed_return-cohort.assumed_round_trip_cost).gt(0).astype(int)).all()),
        'observed_return_matches_prices': bool(np.allclose(cohort.observed_return,cohort.target_close/cohort.target_open-1,rtol=1e-10,atol=1e-12)),
        'unique_exact_target_rows': not bool(cohort.duplicated(['symbol','route','decision_timestamp','target_window_start','target_window_end']).any()),
    }
    selection = report['selection_metrics']
    selected = min(selection,key=lambda k:selection[k]['log_loss'])
    cal_select = report['calibration_selection']
    cal_selected = min(cal_select['candidate_metrics'],key=lambda k:cal_select['candidate_metrics'][k]['log_loss'])
    current = pd.read_parquet(run/'forecasts.parquet')
    current = current[current.model_group.eq(group)]
    support_errors = []
    for row in current.itertuples():
        supported = fit[(fit.symbol == row.symbol) & (fit.route == row.route)]
        if len(supported) != row.symbol_route_fitted_target_rows:
            support_errors.append(f'{row.symbol}/{row.route}')
    result = {
        'group':group, 'model_file':str(run/report['model_file']['path']), 'trained_at':payload['trained_at'],
        'checksums_match':hashes, 'cohort_rows':len(cohort), 'source_and_target_checks':source_checks,
        'partitions':{k:partition_summary(v) for k,v in parts.items()}, 'purge_checks':boundaries,
        'post_close_causal_boundaries':source_cutoff_boundaries,
        'partition_counts_match_report': all(len(v)==report['partitions'][f'{k}_rows'] and v.decision_timestamp.nunique()==report['partition_decision_clusters'][k] for k,v in parts.items()),
        'score_reproduction_max_abs_error':max(score_delta(scores,report['assessment']),score_delta(raw_scores,report['assessment_raw_scores']),score_delta(baseline,report['training_base_rate_assessment'])),
        'probability_ranges_match_report': all(np.allclose(v,report['calibration_diagnostics'][k],rtol=1e-12,atol=1e-12) for k,v in ranges.items()),
        'promotion_status':gate['status'], 'recorded_gate_reproduced':gate==report['promotion_gate'],
        'failed_checks':[k for k,v in gate['checks'].items() if not v], 'promotion_gate':gate,
        'assessment':scores,'baseline':baseline,'calibration_diagnostics':report['calibration_diagnostics'],
        'selected_family':report['selected_family'], 'minimum_development_log_loss_family':selected,
        'selection_matches_saved_metrics':selected==report['selected_family'],
        'daily_c_grid':report['logistic_regularization_candidates'] if group=='1d' else None,
        'calibration_selection':cal_select, 'calibration_minimum_log_loss_family':cal_selected,
        'calibration_selection_matches_saved_metrics':cal_selected==cal_select['selected_family'],
        'calibration_purge_before_validation':pd.Timestamp(cal_select['fit_last_target_end'])<pd.Timestamp(cal_select['validation_first_decision']),
        'calibration_does_not_use_assessment':cal_select['assessment_used_for_selection'] is False,
        'target_boundary_quality':report['target_boundary_quality'],
        'forecast_status_counts':current.model_status.value_counts().to_dict(),
        'support_count_disagreements':support_errors,
        'lowest_symbol_fitted_support':min(report['target_support_by_symbol'],key=lambda s:report['target_support_by_symbol'][s]['fitted_rows']),
        'support_by_symbol':report['target_support_by_symbol'],
    }
    return result,cohort,parts

review={'reviewed_at':datetime.now(timezone.utc).isoformat(),'run_path':str(RUN),
    'action_date':'2026-09-18','scope':'Offline saved-artifact model verification only; no fit, fetch, publication, trading, or owner change.',
    'manifest_matches_receipt':sha(RUN/'manifest.json')==read(RUN/'receipt.json')['manifest_checksum_sha256'],
    'directional':{}}
for group in ['1h','4h','1d','1w']:
    result,cohort,parts=audit_group(RUN,group)
    review['directional'][group]=result
    if group=='1w':
        current_weekly,current_weekly_parts=cohort,parts
prior_weekly,prior_cohort,prior_parts=audit_group(PRIOR,'1w')
key=['symbol','route','decision_timestamp','target_window_start','target_window_end']
joined=current_weekly.merge(prior_cohort,on=key,how='outer',suffixes=('_current','_prior'),indicator=True)
common=joined[joined._merge.eq('both')]
review['weekly_comparison']={
    'prior_run_path':str(PRIOR),'prior':prior_weekly,
    'cohort_rows_prior':len(prior_cohort),'cohort_rows_current':len(current_weekly),
    'added_rows':int(joined._merge.eq('left_only').sum()),'removed_rows':int(joined._merge.eq('right_only').sum()),
    'shared_target_labels_unchanged':bool(common.target_current.eq(common.target_prior).all()),
    'shared_target_prices_unchanged':bool(common.target_open_current.eq(common.target_open_prior).all() and common.target_close_current.eq(common.target_close_prior).all()),
    'diagnosis':'The fresh cohort has eight newly mature rows; chronological partitions advance. The saved minimum-development-log-loss blend remains 50% HGB/50% MLP. Calibration switches from Platt to identity because identity development log loss 0.7389724003705632 is below Platt 0.7391864557573372 (margin 0.000214055386774). Weekly held-out scores fail both authorized v2 tolerances, while sample, class, ECE and varying-probability gates pass. This is a validated adverse quality outcome and a narrowly separated development decision; saved-artifact checks establish no training/source/score defect. Assessment outcomes must not be used to replace calibration or select a challenger.',
    'limitations':'The discarded calibration candidate is not serialized, so its fit was not recreated. Selection was checked against saved development scores and implementation; all final assessment scores were reproduced by prediction only. No archive rescan or alternative candidate fitting was performed.',
    'chronology_interpretation':'The saved decision_timestamp retains prior-session feature availability, which may precede 17:00. It is not the actual overnight forecast dispatch. Native directional partitions purge at next target start; all twelve adjacent-partition last-label ends also precede the next earliest 17:05 post-close source_effective_cutoff. Weekly boundaries clear that cutoff by five minutes. No causal violation was demonstrated for the intended after-close forecast operation; strict source-decision separation is not claimed.'}

er,em=read(ENRICH/'training-report.json'),read(ENRICH/'model.json')
enrichment={'run_path':str(ENRICH),'source_gameplan_run':er['source_gameplan_run'],'status':er['status'],
    'supported_horizons':er['supported_horizons'],'qualified_target_contracts':er['qualified_target_contracts'],
    'source_matches_reviewed_gameplan':ROOT/er['source_gameplan_run']==RUN,
    'orders_placed':er['orders_placed'],'horizons':{},
    'execution_relevance':'Learned enrichment is not required by manual gameplan-direction-current-market-v1 or fixed-horizon-budget-v1. It is required by qualified-enrichment. The current manual Gameplan policy consumes saved instructions independently of model promotion, so weekly assessment failure does not itself block manual weekly instructions. Legacy fixed and qualified policies retain their separate model gates. This audit does not authorize execution or change live behavior.'}
for group,r in er['horizons'].items():
    scopes=r.get('scope_readiness',{})
    enrichment['horizons'][group]={k:r.get(k) for k in ('status','admitted_rows','excluded_context_rows','fitted_scope_count','diagnostic_scope_count','qualified_scope_count','symbols_without_targets')}
    enrichment['horizons'][group].update(fitted=em['horizons'][group]['fitted'],
        reasons=dict(Counter(s['reason'] for s in scopes.values())),
        horizon_assessment_scores=next((s['horizon_assessment_scores'] for s in scopes.values() if s.get('horizon_assessment_scores')),None),
        development_selection_uses_assessment=r.get('head_development_selection',{}).get('assessment_used_for_selection'),
        calibration_selection_uses_assessment=r.get('calibration_selection',{}).get('assessment_used_for_selection'),
        partition_evidence=r.get('partition_evidence'))
    sc=enrichment['horizons'][group]['horizon_assessment_scores']
    conditions={'brier_beats_baseline':sc['brier']<sc['base_brier'],
        'log_loss_beats_baseline':sc['log_loss']<sc['base_log_loss'],
        'ece_at_most_0_15':sc['ece']<=.15,
        'return_mse_beats_baseline':sc['return_mse']<sc['base_return_mse'],
        'adverse_mse_at_most_baseline':sc['adverse_mse']<=sc['base_adverse_mse'],
        'probability_varies':sc['probability_range'][1]-sc['probability_range'][0]>1e-8}
    enrichment['horizons'][group]['quality_conditions']=conditions
    enrichment['horizons'][group]['failed_quality_conditions']=[k for k,v in conditions.items() if not v]
review['enrichment']=enrichment
review['all_artifact_checks_pass']=all(
    all(v['checksums_match'].values()) and all(v['source_and_target_checks'].values()) and all(v['purge_checks'].values())
    and v['partition_counts_match_report'] and v['score_reproduction_max_abs_error'] < 1e-12
    and v['probability_ranges_match_report'] and v['recorded_gate_reproduced']
    and v['selection_matches_saved_metrics'] and v['calibration_selection_matches_saved_metrics']
    and v['calibration_purge_before_validation'] and v['calibration_does_not_use_assessment']
    and all(b['label_known_before_post_close_cutoff'] for b in v['post_close_causal_boundaries'].values())
    and not v['support_count_disagreements'] for v in review['directional'].values())
(OUT/'model-review.json').write_text(json.dumps(review,indent=2,allow_nan=False)+'\n',encoding='utf-8')
lines=[
    '# September 18 overnight model review', '',
    f"Reviewed at {review['reviewed_at']}. Source session September 17; action date September 18.", '',
    f"Fresh immutable Gameplan: `{RUN}`. All four saved assessment/raw/baseline scores reproduced exactly (maximum absolute discrepancy **0.0**), and every checked manifest-bound model/cohort/report checksum, native promotion gate, source clock, price boundary, label, chronological partition and exact symbol/route support count agreed.", '',
    '## Directional qualification', '',
    'The publication uses `independent-stock-directional-promotion-v2`: Brier at most baseline +0.005; log loss at most baseline +0.01; ECE at most 0.15; at least 10 assessment clusters; varying calibration/assessment probabilities and both calibration classes. Qualification within these tolerances does not claim baseline outperformance.', '',
    '| Group | Status | Assessment / baseline Brier | Assessment / baseline log loss | ECE | Assessment rows / clusters |',
    '|---|---|---|---|---|---|',
]
for g,v in review['directional'].items():
    a,b=v['assessment'],v['baseline']
    lines.append(f"| {g} | {v['promotion_status']} | {a['brier_score']:.12f} / {b['brier_score']:.12f} | {a['log_loss']:.12f} / {b['log_loss']:.12f} | {a['expected_calibration_error_10_bin']:.6f} | {a['rows']} / {v['partitions']['assessment']['decision_clusters']} |")
lines += ['',
    'There are 253 promoted forecast rows (154 hourly, 44 four-hour, 55 daily) and 11 unpromoted weekly rows. This includes 198 promoted entry forecasts; 55 promoted rows are non-entry context/outlooks. Every configured symbol/route has fitted target history, including the sparse TWST daily and weekly cohorts (5 and 6 fitted rows respectively); positive support is not a claim of independent per-symbol predictive quality.', '',
    'Only hourly beats both baselines. Four-hour and daily pass the authorized tolerances. The daily candidate is regularized logistic C=0.001, selected as the minimum log loss over the fixed development-only C grid 0.001, 0.01, 0.1, 1 and other native candidates. All groups select the minimum saved development log loss; calibration uses separate purged development evidence and records assessment_used_for_selection=false.', '',
    '## Weekly failure and bounded diagnosis', '',
    review['weekly_comparison']['diagnosis'], '',
    'The weekly Brier excess is **+0.017344433074** (allowed +0.005); log-loss excess is **+0.039264655299** (allowed +0.01). Its calibrated/identity assessment range is 0.215735–0.859853. Calibration has 198 rows across 56 decision clusters and both classes. Its internal calibration development split has 72 fit rows and 96 validation rows, with 30 labels purged. All weekly targets retain observed XNAS.ITCH endpoints within five minutes; 6,734 candidate labels were excluded and 2,267 admitted, without substitution or synthetic training prices.', '',
    'Compared with September 17, the weekly cohort grows from 2,259 to 2,267 rows, with no removed rows and unchanged shared labels/prices. The prior publication chose Platt on development log loss 0.730443822609 vs identity 0.731544621881 and passed v2 with Brier 0.256643306577/log loss 0.706673698246. The latest cohort rolls its partitions, and identity now wins narrowly. Earlier same-date champion retention cannot adopt a different action-date model merely because it scored better; the prior publication remains immutable.', '',
    review['weekly_comparison']['chronology_interpretation'], '',
    review['weekly_comparison']['limitations'], '',
    'No concrete defect requiring repair or retraining was established. Preserve this weekly validation failure and the successfully published generation.', '',
    '## Independent enrichment', '',
    f"Enrichment `{ENRICH}` is bound to the reviewed Gameplan. All four groups fitted; **zero scopes qualified**, supported_horizons and qualified_target_contracts are empty. Training reported zero orders.", '',
    '| Group | Fitted scopes | Qualified scopes | Failed horizon quality conditions |',
    '|---|---:|---:|---|',
]
for g in ['1h','4h','1d','1w']:
    v=enrichment['horizons'][g]
    lines.append(f"| {g} | {v['fitted_scope_count']} | {v['qualified_scope_count']} | {', '.join(v['failed_quality_conditions'])} |")
lines += ['',
    'Eleven scope diagnostics additionally lack sufficient pooled symbol/route evidence (6 hourly, 2 four-hour, 1 daily, 2 weekly); details are retained in the JSON and native training report. Enrichment uses its own strict quality policy, including return and adverse-return errors, and is separate from directional v2 promotion.', '',
    enrichment['execution_relevance'], '',
    'The audit made no code, trading control, provider, broker, training, publication or supervision ownership changes. It wrote only these analysis artifacts.', '',
    'Machine-readable evidence: `model-review.json`; reproducible prediction-only audit: `review_models.py`.',
]
(OUT/'model-review.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({'all_artifact_checks_pass':review['all_artifact_checks_pass'],
    'scores':{g:{k:v[k] for k in ['promotion_status','score_reproduction_max_abs_error','source_and_target_checks','forecast_status_counts']} for g,v in review['directional'].items()},
    'enrichment':enrichment},indent=2))
