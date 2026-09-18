"""Read-only YG artifact/model verification. Never fit, fetch, or contact a broker."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from ml.nightly_gameplan import _chronological_partitions, _model_frame, _proper_scores
from ml.gameplan_promotion import build_promotion_gate

ROOT = Path('C:/DATASTORE')
OUT = Path(__file__).resolve().parent
OG = ROOT / 'ml/nightly-gameplan-runs/20260918T054532.489998Z'
OG_EXPECTED_MANIFEST = 'dea097efa941b0610a6a018a05beecf870ec239cbf3e39a27d3433e849fc8987'
OG_EXPECTED_RECEIPT = 'ba53f5f4b62443c426190da8cad96cf7e18644ee8c3f278b0fa5092c93b3de97'
CONTRACT = 'raw-price-direction-v1'
GROUPS = ('1h', '4h', '1d', '1w')
GRID = (0.001, 0.01, 0.1, 1.0)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat()


def utc(series):
    return pd.to_datetime(series, utc=True)


def record(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def check_og(*, prepare=False):
    manifest = read(OG/'manifest.json')
    names = ['manifest.json', 'receipt.json', *manifest['output_files']]
    observed = {name: sha(OG/name) for name in names}
    checks = {
        'original_manifest_hash':observed['manifest.json'] == OG_EXPECTED_MANIFEST,
        'original_receipt_hash':observed['receipt.json'] == OG_EXPECTED_RECEIPT,
        'all_original_outputs_match_manifest':all(observed[name] == item['checksum_sha256']
            for name,item in manifest['output_files'].items()),
        'original_manifest_has_legacy_probability_target':manifest['configuration'].get(
            'probability_target_contract', 'cost-adjusted-positive-return-v1') == 'cost-adjusted-positive-return-v1',
    }
    baseline = OUT/'og-frozen-hashes.json'
    if prepare:
        if not all(checks.values()):
            raise RuntimeError(f'Original OG evidence changed: {checks}')
        if baseline.exists():
            if read(baseline)['sha256'] != observed:
                raise RuntimeError('Refusing to replace different frozen OG hashes')
        else:
            record(baseline, {'captured_at':stamp(), 'run_path':str(OG), 'sha256':observed,
                'checks':checks, 'scope':'Frozen same-action-date OG comparison only; never YG champion authority.'})
    if baseline.exists():
        checks['unchanged_since_verifier_preparation'] = read(baseline)['sha256'] == observed
    elif not prepare:
        raise RuntimeError('Run --prepare-og before the YG final verification')
    return {'run_path':str(OG), 'checks':checks, 'all_pass':all(checks.values()), 'sha256':observed}


def summary(frame):
    return {'rows':len(frame), 'decision_clusters':int(frame.decision_timestamp.nunique()),
        'first_decision':utc(frame.decision_timestamp).min().isoformat(),
        'last_decision':utc(frame.decision_timestamp).max().isoformat(),
        'first_target_start':utc(frame.target_window_start).min().isoformat(),
        'last_target_end':utc(frame.target_window_end).max().isoformat(),
        'positive_rate':float(frame.target.mean()),
        'class_counts':{str(k):int(v) for k,v in frame.target.value_counts().items()}}


def score_error(actual, expected):
    return max(abs(float(actual[k])-float(expected[k])) for k in expected)


def diagnose_flat_calibration(calibration, raw_probability, report):
    """Validate a slope-zero boundary analytically, without fitting any candidate."""
    frame=calibration.copy().reset_index(drop=True)
    frame['raw_probability']=raw_probability
    clusters=pd.Index(frame.decision_timestamp.unique()).sort_values()
    midpoint=len(clusters)//2
    development_fit=frame[frame.decision_timestamp.isin(clusters[:midpoint])].copy()
    validation=frame[frame.decision_timestamp.isin(clusters[midpoint:])].copy()
    development_fit=development_fit[development_fit.target_window_end.lt(validation.decision_timestamp.min())]
    diagnostics={}
    for name,data in [('development_fit',development_fit),('full_calibration',frame)]:
        raw=np.clip(data.raw_probability.to_numpy(dtype=float),1e-6,1-1e-6)
        y=data.target.to_numpy(dtype=float)
        logits=np.log(raw/(1-raw))
        rate=float(y.mean())
        gradient=float(np.mean(logits*(rate-y)))
        diagnostics[name]={'rows':len(data),'positive_rate':rate,
            'raw_range':[float(raw.min()),float(raw.max())],
            'logit_label_covariance':float(np.mean((logits-logits.mean())*(y-y.mean()))),
            'negative_log_likelihood_slope_derivative_at_zero':gradient,
            'nonnegative_slope_boundary_supported':gradient>=0,
            'interpretation':'With intercept at observed base rate, nonnegative slope has its constrained minimum at zero when this convex-loss derivative is nonnegative. L2 slope penalty contributes zero derivative at zero.'}
    rate=float(development_fit.target.mean())
    constant_scores=_proper_scores(validation.target.to_numpy(),np.full(len(validation),rate))
    identity_scores=_proper_scores(validation.target.to_numpy(),validation.raw_probability.to_numpy())
    recorded=report['calibration_selection']['candidate_metrics']
    diagnostics['development_validation']={'rows':len(validation),
        'constant_at_development_fit_rate':rate,'constant_scores':constant_scores,'identity_scores':identity_scores,
        'constant_matches_saved_Platt_scores':all(abs(constant_scores[k]-recorded['platt'][k])<1e-12 for k in ('brier_score','log_loss')),
        'identity_matches_saved_scores':all(abs(identity_scores[k]-recorded['identity'][k])<1e-12 for k in ('brier_score','log_loss'))}
    diagnostics['conclusion']=('The fixed development policy permits the constrained Platt base-rate boundary as a calibration candidate; it wins saved development log loss, then fails the separate directional-information promotion gate. Analytical derivatives and saved-model predictions support the flat result. This is a valid weak-model outcome, not a detected fit/source/label defect; replacing it after reading assessment would change the established selection policy.')
    diagnostics['no_fit_performed']=True
    return diagnostics


def audit_group(run, group, reports, manifest, forecasts):
    r = reports[group]
    # A retained YG champion must use its own saved cohort for score reproduction.
    cohort_name = r.get('deployment', {}).get('retained_cohort_output', f'training-cohort-{group}.parquet')
    cohort = pd.read_parquet(run/cohort_name)
    current_cohort = pd.read_parquet(run/f'training-cohort-{group}.parquet')
    payload = joblib.load(run/r['model_file']['path'])
    parts = _chronological_partitions(cohort, group=group)
    fit = pd.concat([parts['train'],parts['selection']],ignore_index=True)
    assessment = parts['assessment']
    matrix = _model_frame(assessment,payload['feature_columns'],payload['categorical_columns'])
    raw = payload['estimator'].predict_proba(matrix)[:,1]
    prob = payload['calibrator'].predict(raw)
    scores = _proper_scores(assessment.target.to_numpy(),prob)
    raw_scores = _proper_scores(assessment.target.to_numpy(),raw)
    baseline = _proper_scores(assessment.target.to_numpy(),np.full(len(assessment),fit.target.mean()))
    gate = build_promotion_gate(scores,baseline,r['calibration_diagnostics'],
        assessment.decision_timestamp.nunique(),policy_version=r['promotion_gate']['policy_version'])
    score_max_error = max(score_error(scores,r['assessment']),score_error(raw_scores,r['assessment_raw_scores']),
        score_error(baseline,r['training_base_rate_assessment']))
    cal = parts['calibration']
    cal_raw = payload['estimator'].predict_proba(_model_frame(cal,payload['feature_columns'],payload['categorical_columns']))[:,1]
    cal_prob = payload['calibrator'].predict(cal_raw)
    ranges = {'raw_probability_range':[float(cal_raw.min()),float(cal_raw.max())],
        'calibrated_probability_range':[float(cal_prob.min()),float(cal_prob.max())],
        'assessment_probability_range':[float(prob.min()),float(prob.max())]}
    scoped_forecasts = forecasts[forecasts.model_group.eq(group)]
    expected_grid = list(GRID) if group in ('1h','4h','1d') else [1.0]
    expected_policy = ('independent-stock-raw-direction-logistic-regularization-v1' if group in ('1h','4h')
        else 'independent-stock-daily-logistic-regularization-v1' if group=='1d' else None)
    selection = r['selection_metrics']
    minimum_family = min(selection,key=lambda name:selection[name]['log_loss'])
    calibration_selection = r['calibration_selection']
    cal_metrics = calibration_selection['candidate_metrics']
    minimum_calibration = min(cal_metrics,key=lambda name:cal_metrics[name]['log_loss']) if cal_metrics else 'identity'
    source_checks = {
        'same_XNAS_target_source':bool(cohort.target_price_source_contract.eq('xnas-itch-archive-v1').all()
            and cohort.target_price_dataset.eq('XNAS.ITCH').all()),
        'same_prior_session_feature_contract':bool(cohort.source_selection_contract.eq('independent-gameplan-prior-session-features-v1').all()),
        'feature_hour_duration':bool(utc(cohort.source_bar_end_timestamp).sub(utc(cohort.source_bar_timestamp)).eq(pd.Timedelta(hours=1)).all()),
        'information_after_feature_bar':bool(utc(cohort.information_available_at).ge(utc(cohort.source_bar_end_timestamp)).all()),
        'decision_after_information':bool(utc(cohort.decision_timestamp).ge(utc(cohort.information_available_at)).all()),
        'decision_before_effective_cutoff':bool(utc(cohort.decision_timestamp).le(utc(cohort.source_effective_cutoff)).all()),
        'source_before_action':bool(utc(cohort.decision_timestamp).lt(utc(cohort.source_action_start)).all()),
        'labels_mature_before_fitting':bool(utc(cohort.target_window_end).le(pd.Timestamp(payload['trained_at'])).all()),
        'boundary_aligned_within_five_minutes':bool(cohort.target_boundary_aligned.eq(True).all()
            and cohort[['target_start_gap_seconds','target_end_gap_seconds']].abs().le(300).all().all()),
        'returns_match_observed_prices':bool(np.allclose(cohort.observed_return,cohort.target_close/cohort.target_open-1,rtol=1e-10,atol=1e-12)),
        'unique_target_rows':not bool(cohort.duplicated(['symbol','route','decision_timestamp','target_window_start','target_window_end']).any()),
    }
    contract_checks = {}
    for name,item in [('report',r),('payload',payload)]:
        contract_checks[f'{name}_YG_raw_contract'] = item.get('probability_target_contract')==CONTRACT and item.get('gameplan_variant')=='YG'
    for name,frame in [('cohort',cohort),('fresh_cohort',current_cohort),('forecasts',scoped_forecasts)]:
        contract_checks[f'{name}_YG_raw_contract'] = bool(frame.probability_target_contract.eq(CONTRACT).all() and frame.gameplan_variant.eq('YG').all())
    for name,frame in [('cohort',cohort),('fresh_cohort',current_cohort)]:
        contract_checks[f'{name}_raw_target'] = bool(frame.target.eq(frame.observed_return.gt(0).astype(int)).all()
            and frame.target_raw_price_direction.eq(frame.observed_return.gt(0).astype(int)).all())
        contract_checks[f'{name}_separate_cost_label'] = bool(frame.target_cost_adjusted_positive.eq(
            frame.observed_return.gt(frame.assumed_round_trip_cost).astype(int)).all())
    deployment = r.get('deployment')
    if deployment:
        champion = ROOT/deployment['source_run']
        champion_report = read(champion/'model-reports.json')[group]
        contract_checks['retained_champion_same_YG_contract'] = (
            champion_report.get('probability_target_contract')==CONTRACT and champion_report.get('gameplan_variant')=='YG'
            and champion.resolve()!=OG.resolve())
    else:
        contract_checks['no_OG_champion_retained'] = not bool(scoped_forecasts.get('model_source_run',pd.Series(dtype=str)).eq(OG.relative_to(ROOT).as_posix()).any())
    partitions = {name:summary(frame) for name,frame in parts.items()}
    boundary_checks = {}
    for left,right in zip(('train','selection','calibration'),('selection','calibration','assessment')):
        boundary_checks[f'{left}_labels_before_{right}_target'] = bool(utc(parts[left].target_window_end).max()<utc(parts[right].target_window_start).min())
        boundary_checks[f'{left}_labels_before_{right}_post_close_cutoff'] = bool(utc(parts[left].target_window_end).max()<utc(parts[right].source_effective_cutoff).min())
    supported = {(str(symbol),str(route)):int(count) for (symbol,route),count in fit.groupby(['symbol','route']).size().items()}
    support_errors = [f'{row.symbol}/{row.route}' for row in scoped_forecasts.itertuples()
        if supported.get((row.symbol,row.route),0)!=row.symbol_route_fitted_target_rows]
    checks = {
        'recorded_final_scores_reproduced':score_max_error<1e-12,
        'native_promotion_gate_reproduced':gate==r['promotion_gate'],
        'probability_ranges_reproduced':all(np.allclose(value,r['calibration_diagnostics'][key],rtol=1e-12,atol=1e-12) for key,value in ranges.items()),
        'partition_counts_reproduced':all(len(frame)==r['partitions'][f'{name}_rows'] and frame.decision_timestamp.nunique()==r['partition_decision_clusters'][name] for name,frame in parts.items()),
        'declared_C_grid_matches_authorization':r['logistic_regularization_candidates']==expected_grid,
        'regularization_policy_matches_contract':r.get('logistic_regularization_policy')==expected_policy and payload.get('logistic_regularization_policy')==expected_policy,
        'all_C_candidates_have_development_scores':all(f'regularized-logistic-c{c:g}' in selection for c in expected_grid),
        'model_selected_by_minimum_development_log_loss':minimum_family==r['selected_family'],
        'calibrator_selected_by_minimum_development_log_loss':minimum_calibration==calibration_selection['selected_family'],
        'calibration_selection_excludes_assessment':calibration_selection['assessment_used_for_selection'] is False,
        'calibration_fit_purged_before_validation':calibration_selection.get('fit_last_target_end') is None or pd.Timestamp(calibration_selection['fit_last_target_end'])<pd.Timestamp(calibration_selection['validation_first_decision']),
        'exact_forecast_symbol_route_support_matches':not support_errors,
    }
    result = {'checks':checks,'source_checks':source_checks,'contract_checks':contract_checks,'chronology_checks':boundary_checks,
        'all_pass':all(checks.values()) and all(source_checks.values()) and all(contract_checks.values()) and all(boundary_checks.values()),
        'fitted':True,'selected_family':r['selected_family'],'promotion_status':gate['status'],
        'failed_promotion_checks':[name for name,passed in gate['checks'].items() if not passed],
        'assessment':scores,'baseline':baseline,'raw_assessment':raw_scores,'score_max_abs_error':score_max_error,
        'promotion_gate':gate,'calibration_diagnostics':r['calibration_diagnostics'],'calibration_selection':calibration_selection,
        'C_grid':r['logistic_regularization_candidates'],'selected_C':r.get('selected_logistic_regularization_c'),
        'partitions':partitions,'trained_at':payload['trained_at'],'cohort_rows':len(cohort),
        'raw_vs_cost_label_disagreements':int(cohort.target_raw_price_direction.ne(cohort.target_cost_adjusted_positive).sum()),
        'status_counts':scoped_forecasts.model_status.value_counts().to_dict(),
        'support_errors':support_errors,'deployment':deployment,
        'target_boundary_quality':r.get('target_boundary_quality')}
    if r['calibration_diagnostics']['status']=='FLAT_CALIBRATION':
        result['flat_calibration_diagnosis']=diagnose_flat_calibration(cal,cal_raw,r)
    return result


def audit_enrichment(run, gameplan):
    report,model,manifest,receipt = [read(run/name) for name in ('training-report.json','model.json','manifest.json','receipt.json')]
    checks = {'bound_to_YG_run':ROOT/report['source_gameplan_run']==gameplan,
        'manifest_hash':sha(run/'manifest.json')==receipt['manifest_sha256'],
        'model_hash':sha(run/'model.json')==receipt['model_sha256'],
        'report_hash':sha(run/'training-report.json')==receipt['training_report_sha256'],
        'source_files_unchanged':all(sha(ROOT/name)==value for name,value in report['source_files'].items()),
        'zero_orders':report['orders_placed']==0 and report['broker_orders_enabled'] is False}
    result={'run_path':str(run),'checks':checks,'all_pass':all(checks.values()),'status':report['status'],
        'supported_horizons':report['supported_horizons'],'qualified_target_contracts':report['qualified_target_contracts'],
        'probability_semantics':'Independent enrichment intentionally rebuilds net-return>0 labels; it does not relabel its profitability head as YG raw direction.',
        'horizons':{}}
    for group,horizon in report['horizons'].items():
        scopes=horizon.get('scope_readiness',{})
        scores=next((v['horizon_assessment_scores'] for v in scopes.values() if v.get('horizon_assessment_scores')),None)
        result['horizons'][group]={'fitted':model['horizons'][group].get('fitted',False),
            **{k:horizon.get(k) for k in ('status','fitted_scope_count','qualified_scope_count','admitted_rows')},
            'reason_counts':dict(Counter(v['reason'] for v in scopes.values())), 'horizon_assessment_scores':scores}
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--prepare-og',action='store_true')
    parser.add_argument('--run',type=Path)
    parser.add_argument('--enrichment-run',type=Path)
    args=parser.parse_args()
    og=check_og(prepare=args.prepare_og)
    if args.run is None:
        if not args.prepare_og:
            parser.error('--run or --prepare-og is required')
        print(json.dumps({'status':'PREPARED','og_checks':og['checks'],'baseline':str(OUT/'og-frozen-hashes.json')}))
        return
    run=args.run.resolve()
    if run.parent != (ROOT/'ml/nightly-gameplan-runs').resolve() or run==OG.resolve():
        raise RuntimeError('Expected a new YG publication under the native Gameplan run root')
    reports,manifest,receipt,plan=[read(run/name) for name in ('model-reports.json','manifest.json','receipt.json','gameplan.json')]
    forecasts=pd.read_parquet(run/'forecasts.parquet')
    output_hashes={name:sha(run/name)==item['checksum_sha256'] for name,item in manifest['output_files'].items()}
    top_checks={'all_outputs_match_manifest':all(output_hashes.values()),
        'manifest_matches_receipt':sha(run/'manifest.json')==receipt['manifest_checksum_sha256'],
        'exact_action_date':receipt['action_date']==plan['action_date']==manifest['configuration']['action_date']=='2026-09-18',
        'manifest_raw_fitting_target':manifest['target_column']=='target_raw_price_direction',
        'YG_metadata_bound_everywhere':all(item.get('probability_target_contract')==CONTRACT and item.get('gameplan_variant')=='YG'
            for item in (receipt,plan,manifest['configuration'])),
        'zero_publication_orders':receipt['orders_placed']==0 and receipt['broker_orders_enabled'] is False,
        'expected_forecast_count':len(forecasts)==24*len(plan['symbols'])}
    result={'verified_at':stamp(),'run_path':str(run),'action_date':'2026-09-18','probability_target_contract':CONTRACT,
        'gameplan_variant':'YG','scope':'Offline read-only verification; no training, provider, broker, ownership, or production writes.',
        'checks':top_checks,'output_checksums':output_hashes,'original_OG':og,'directional':{},
        'execution_note':'Current manual Gameplan execution consumes saved instructions independently of promotion. Legacy fixed/qualified policies retain their gates. No execution is performed or authorized by this audit.',
        'chronology_note':'decision_timestamp is a prior-session feature clock. Checks retain native target-window purging and also require preceding labels before the next post-close source cutoff; it is not the live dispatch clock.',
        'limitations':'Assessment and calibrated outputs are recomputed from saved models only; discarded development candidates are not refitted. Their recorded selection metrics and source code determine selection verification.'}
    for group in GROUPS:
        result['directional'][group]=audit_group(run,group,reports,manifest,forecasts)
    if args.enrichment_run:
        result['enrichment']=audit_enrichment(args.enrichment_run.resolve(),run)
    result['all_artifact_checks_pass']=all(top_checks.values()) and og['all_pass'] and all(v['all_pass'] for v in result['directional'].values()) and result.get('enrichment',{}).get('all_pass',True)
    record(OUT/'model-review.json',result)
    lines=['# YG September 18 saved-model verification','',f"Verified {result['verified_at']}; publication `{run}`.",'',
        f"Artifact checks: **{'PASS' if result['all_artifact_checks_pass'] else 'FAIL'}**. Original OG publication and every manifest-bound output remain unchanged: **{og['all_pass']}**.",'',
        'YG predicts strictly positive raw observed price return. Cost-adjusted-positive labels remain separate evidence. The authorized C grid is 0.001, 0.01, 0.1, 1 for 1h/4h/1d and 1 for 1w; selection uses development log loss. OG is frozen comparison evidence only and cannot serve as a retained YG champion.','',
        '| Horizon | Selected family | Directional status | Brier / baseline | Log loss / baseline | Score reproduction max error |',
        '|---|---|---|---|---|---|']
    for group,value in result['directional'].items():
        a,b=value['assessment'],value['baseline']
        lines.append(f"| {group} | {value['selected_family']} | {value['promotion_status']} | {a['brier_score']:.12f} / {b['brier_score']:.12f} | {a['log_loss']:.12f} / {b['log_loss']:.12f} | {value['score_max_abs_error']} |")
    for group,value in result['directional'].items():
        if value['failed_promotion_checks']:
            lines += ['',f"{group} failed promotion checks: {', '.join(value['failed_promotion_checks'])}."]
        if 'flat_calibration_diagnosis' in value:
            d=value['flat_calibration_diagnosis']
            v=d['development_validation']
            lines += ['',f"{group} flat-calibration diagnosis: {d['conclusion']}",
                f"Its development fit contains {d['development_fit']['rows']} rows at positive rate {d['development_fit']['positive_rate']}; the constant development prediction {v['constant_at_development_fit_rate']} reproduces saved Platt log loss {v['constant_scores']['log_loss']:.12f}, below identity {v['identity_scores']['log_loss']:.12f}. Full calibration reproduces its base rate {d['full_calibration']['positive_rate']:.12f}. Nonnegative slope-boundary derivatives: development {d['development_fit']['negative_log_likelihood_slope_derivative_at_zero']:.12g}, full calibration {d['full_calibration']['negative_log_likelihood_slope_derivative_at_zero']:.12g}. No candidate was fitted."]
    lines += ['', 'Qualification uses each publication’s recorded policy and is distinct from beating the baseline. The JSON retains exact sample counts, calibration ranges, causal/source checks, and raw-versus-cost label disagreements.', '',result['chronology_note'],'',result['limitations']]
    if 'enrichment' in result:
        e=result['enrichment']
        lines += ['','## Separate independent enrichment','',f"Run `{e['run_path']}`; {e['probability_semantics']}",'',
            '| Horizon | Fitted | Fitted scopes | Qualified scopes |','|---|---|---:|---:|']
        for group in GROUPS:
            value=e['horizons'][group]
            lines.append(f"| {group} | {value['fitted']} | {value['fitted_scope_count']} | {value['qualified_scope_count']} |")
    lines += ['',result['execution_note'],'','Audit wrote only analysis artifacts in its own YG directory; the prior OG audit was preserved.']
    (OUT/'model-review.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'all_artifact_checks_pass':result['all_artifact_checks_pass'],
        'directional':{g:{k:v[k] for k in ('promotion_status','failed_promotion_checks','selected_family','score_max_abs_error','raw_vs_cost_label_disagreements')} for g,v in result['directional'].items()},
        'enrichment':result.get('enrichment'),'reports':[str(OUT/'model-review.md'),str(OUT/'model-review.json')]},indent=2))


if __name__ == '__main__':
    main()
