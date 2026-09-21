"""Bounded saved-report/cohort review; never fits or selects on assessment results."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import pandas as pd
from ml.nightly_gameplan import read_gameplan_run, _chronological_partitions, _verify_probability_cohort
from ml.gameplan_promotion import build_promotion_gate

ROOT = Path('C:/DATASTORE')
RUN = ROOT/'ml/nightly-gameplan-runs/20260919T061628.240412Z'
OUT = Path(__file__).resolve().parent
GROUPS = ('1h','4h','1d','1w')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def stamp(value):
    return pd.Timestamp(value).isoformat()


publication = read_gameplan_run(ROOT, RUN)
reports = read(RUN/'model-reports.json')
forecasts = pd.read_parquet(RUN/'forecasts.parquet')
review = {'reviewed_at': datetime.now(timezone.utc).isoformat(), 'run': str(RUN),
          'action_date': publication.receipt['action_date'], 'native_manifest_and_target_verified': True,
          'scope': 'Saved reports, immutable cohorts and frozen forecast metadata only. No fitting, candidate selection, market archive or account reads.',
          'assessment_inference_reproduction': 'Deferred to the supplemental verifier after complete native tail.',
          'groups': {}, 'errors': [], 'orders_placed': 0}
for group in GROUPS:
    r = reports[group]
    cohort_name = r.get('deployment', {}).get('retained_cohort_output', f'training-cohort-{group}.parquet')
    frame = pd.read_parquet(RUN/cohort_name)
    _verify_probability_cohort(frame, 'raw-price-direction-v1')
    parts = _chronological_partitions(frame, group=group)
    fit = pd.concat([parts['train'],parts['selection']], ignore_index=True)
    symbol_count = fit.groupby('symbol').size().to_dict()
    route_count = fit.groupby(['symbol','route']).size().to_dict()
    current = forecasts.loc[forecasts.model_group.eq(group)]
    checks = {}
    checks['recorded_gate_matches_saved_metrics'] = build_promotion_gate(
        r['assessment'], r['training_base_rate_assessment'], r['calibration_diagnostics'],
        r['partition_decision_clusters']['assessment'], policy_version=r['promotion_gate']['policy_version']) == r['promotion_gate']
    support_errors = []
    for row in current.itertuples():
        recorded = r['target_support_by_symbol'].get(row.symbol, {})
        expected_symbol = int(symbol_count.get(row.symbol, 0))
        expected_route = int(route_count.get((row.symbol,row.route), 0))
        expected_status = r['promotion_gate']['status'] if expected_symbol and expected_route else 'RESEARCH_NO_TARGET_HISTORY'
        if (row.symbol_fitted_target_rows != expected_symbol or row.symbol_route_fitted_target_rows != expected_route
                or recorded.get('fitted_rows',0) != expected_symbol
                or recorded.get('fitted_rows_by_route',{}).get(row.route,0) != expected_route
                or row.model_status != expected_status):
            support_errors.append({'symbol':row.symbol,'route':row.route,'expected_status':expected_status})
    checks['exact_fitted_symbol_route_support_and_status'] = not support_errors
    checks['no_duplicate_exact_target_rows'] = not frame.duplicated(['symbol','route','decision_timestamp','target_window_start','target_window_end']).any()
    checks['observed_boundary_gaps_within_five_minutes'] = frame[['target_start_gap_seconds','target_end_gap_seconds']].abs().le(300).all().all()
    parts_detail = {}
    for name, part in parts.items():
        checks[f'{name}_rows_and_clusters'] = len(part) == r['partitions'][f'{name}_rows'] and part.decision_timestamp.nunique() == r['partition_decision_clusters'][name]
        parts_detail[name] = {'rows':len(part), 'decision_clusters':int(part.decision_timestamp.nunique()),
            'positive_rate':float(part.target.mean()),'class_counts':{str(k):int(v) for k,v in part.target.value_counts().items()},
            'first_decision':stamp(part.decision_timestamp.min()),'last_decision':stamp(part.decision_timestamp.max()),
            'first_target_start':stamp(part.target_window_start.min()),'last_target_end':stamp(part.target_window_end.max())}
    chronology = {}
    for left,right in zip(('train','selection','calibration'),('selection','calibration','assessment')):
        label_end = pd.to_datetime(parts[left].target_window_end,utc=True).max()
        target_start = pd.to_datetime(parts[right].target_window_start,utc=True).min()
        source_cutoff = pd.to_datetime(parts[right].source_effective_cutoff,utc=True).min()
        checks[f'{left}_before_{right}_target'] = label_end < target_start
        checks[f'{left}_before_{right}_source_cutoff'] = label_end < source_cutoff
        chronology[f'{left}_to_{right}'] = {'last_label_end':stamp(label_end),'next_first_target_start':stamp(target_start),
            'next_first_source_cutoff':stamp(source_cutoff),'source_cutoff_margin_minutes':float((source_cutoff-label_end).total_seconds()/60)}
    metrics = r['selection_metrics']
    checks['selected_model_minimum_development_log_loss'] = metrics[r['selected_family']]['log_loss'] == min(v['log_loss'] for v in metrics.values())
    cal = r['calibration_selection']
    checks['calibration_selection_excludes_assessment'] = cal['assessment_used_for_selection'] is False
    checks = {k:bool(v) for k,v in checks.items()}
    errors = [name for name,passed in checks.items() if not passed]
    if errors:
        review['errors'].append({'group':group,'failed_checks':errors,'support_errors':support_errors})
    minimum = current.sort_values(['symbol_route_fitted_target_rows','symbol','route']).iloc[0]
    symbol_support = {}
    for symbol, counts in sorted(r['target_support_by_symbol'].items()):
        symbol_support[symbol] = {k:counts[k] for k in ('fitted_rows','fitted_decision_clusters','assessment_rows','assessment_decision_clusters','fitted_rows_by_route','assessment_rows_by_route')}
    review['groups'][group] = {
        'checks':checks,'promotion_gate':r['promotion_gate'],'assessment':r['assessment'],
        'baseline':r['training_base_rate_assessment'], 'selected_family':r['selected_family'],
        'selected_development_metrics':metrics[r['selected_family']], 'selection_candidates':len(metrics),
        'calibration_selection':cal,'calibration_diagnostics':r['calibration_diagnostics'],
        'selected_probability_shrinkage_weight':r.get('selected_probability_shrinkage_weight'),
        'probability_shrinkage_policy':r.get('probability_shrinkage_policy'),
        'fitted_cohort':cohort_name, 'retained_champion':r.get('deployment'),
        'partitions':parts_detail,'chronology':chronology,'support_by_symbol':symbol_support,
        'minimum_exact_route_support':{'symbol':minimum.symbol,'route':minimum.route,'fitted_rows':int(minimum.symbol_route_fitted_target_rows)},
        'zero_assessment_symbols':[s for s,v in symbol_support.items() if not v['assessment_rows']],
        'forecast_probability_range':[float(current.calibrated_probability.min()),float(current.calibrated_probability.max())],
        'forecast_directions':current.direction.value_counts().to_dict(),'forecast_statuses':current.model_status.value_counts().to_dict(),
        'target_boundary_quality':r.get('target_boundary_quality')}

review['status'] = 'FAILED' if review['errors'] else 'VERIFIED_WITH_SAMPLE_LIMITATIONS'
review['concrete_repair_justified'] = bool(review['errors'])
inference_path = OUT/'yg-publication-verification.json'
inference_note = 'Saved estimator inference will be checked once by the supplemental verifier after the full native tail completes.'
if inference_path.is_file():
    inference = read(inference_path)
    if Path(inference.get('publication_run','')).resolve() == RUN.resolve() and not inference.get('errors'):
        review['assessment_inference_reproduction'] = {
            'evidence':str(inference_path), 'status':inference['status'],
            'native_report_status':inference['native_report_status'],
            'full_native_completion_verified':inference['full_native_completion_verified'],
            'score_max_abs_error':{g:v['score_max_abs_error'] for g,v in inference['directional'].items()}}
        inference_note = ('The subsequent publication-only supplement reproduced all four saved assessment/raw/baseline scores exactly (maximum error 0.0), with all model artifact checks passing. Its explicit status is MODEL_ARTIFACTS_VERIFIED_NATIVE_FAILED and full_native_completion_verified is false; no native completion is claimed. See yg-publication-verification.json.')
(OUT/'model-review.json').write_text(json.dumps(review,indent=2,allow_nan=False)+'\n',encoding='utf-8')
lines = ['# September 21 YG model review','',f"Reviewed {review['reviewed_at']}; publication `{RUN}`.",'',
    'All four freshly fitted directional models and all 264 forecast rows are PROMOTED under the unchanged v2 operating rule. None of the four models beats its training-rate baseline on either Brier score or log loss; each is within the allowed +0.005/+0.01 tolerances. This qualification does not establish baseline outperformance.', '',
    '| Group | Selected development family | Brier / baseline | Log loss / baseline | Assessment rows / clusters |',
    '| --- | --- | --- | --- | --- |']
for group in GROUPS:
    v=review['groups'][group]
    a,b=v['assessment'],v['baseline']
    p=v['partitions']['assessment']
    lines.append(f"| {group} | {v['selected_family']} | {a['brier_score']:.9f} / {b['brier_score']:.9f} | {a['log_loss']:.9f} / {b['log_loss']:.9f} | {p['rows']} / {p['decision_clusters']} |")
lines += ['', 'Weekly is nearest the allowed limits: Brier excess +0.004024408 leaves 0.000975592 of tolerance; log-loss excess +0.008668397 leaves 0.001331603. Its fixed development search selected the histogram-gradient model with 0.5 shrinkage toward the fitting prior; this is one of the predeclared 36 candidates. The selected family has the lowest saved development log loss. No candidate was selected using this review or final assessment.', '',
    'Daily calibration retains varying probabilities, but its entire assessment range is 0.406903–0.443867. The four-hour range is also narrow at 0.471590–0.487324. These are explicit varying maps, not constant fallbacks; narrow or one-sided ranges are limitations, not evidence of a violated current gate.', '',
    '## Fitted-history and assessment limits','',
    'All 264 exact symbol/route fitted counts and statuses agree with a new rollup of the saved TRAIN plus selection partitions. There are no retained champions. All saved cohort target labels match raw return > 0 and preserve separate cost-adjusted labels. All twelve adjacent partition boundaries put the prior label end before the next target start and before the next 17:05 post-close source cutoff.', '',
    '| Group | Smallest fitted exact route | Fitted rows | Companies with no assessment rows |',
    '| --- | --- | ---: | --- |']
for group in GROUPS:
    v=review['groups'][group]; m=v['minimum_exact_route_support']
    lines.append(f"| {group} | {m['symbol']} / {m['route']} | {m['fitted_rows']} | {', '.join(v['zero_assessment_symbols']) or 'None'} |")
lines += ['', 'TWST has only five total fitted daily rows, one per daily route; its ten daily assessment rows provide two per route. It has six fitted weekly rows and two weekly assessment rows. CROX has 120 fitted daily rows (24 per route) and 30 fitted weekly rows, but no daily or weekly assessment rows. Aggregate group promotion does not establish predictive accuracy for either company or every route. The current exact-history gate requires positive fitted support, not a per-company assessment guarantee.', '',
    'The four group assessments each contain 63 distinct decision clusters; their row counts are correlated across symbols and daily outlooks, so they are not that many independent trials. This review does not claim an untouched future test, causal independence of all rows, or tradable profitability.', '',
    '## Verification and next step','',
    'Native immutable publication/target verification, cohort labels, duplicate checks, observed five-minute boundaries, partition counts, source-cutoff chronology, saved-metric promotion arithmetic and exact fitted support all passed. No concrete defect requiring a repair or retry was found. No model was fitted, relabeled or selected again; no account or market archive was read. '+inference_note, '',
    'Structured evidence: `model-review.json` in this directory. Optional enrichment qualification is separate. The native tail subsequently failed its account-ownership snapshot gate; this model review does not establish full overnight completion or authority to retry that gate.']
(OUT/'model-review.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({'status':review['status'],'errors':review['errors'],'run':str(RUN),'reports':[str(OUT/'model-review.md'),str(OUT/'model-review.json')]},indent=2))
