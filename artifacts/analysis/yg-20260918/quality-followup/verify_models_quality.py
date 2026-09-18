"""Saved-artifact verification of the locked YG quality repair; no fitting."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys
import argparse

import joblib
import numpy as np
import pandas as pd

OUT=Path(__file__).resolve().parent
BASE_PATH=OUT.parent/'verify_yg_models.py'
spec=importlib.util.spec_from_file_location('yg_base_saved_model_verifier',BASE_PATH)
base=importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
from ml.nightly_gameplan import _chronological_partitions, _model_frame

ORIGINAL_YG=base.ROOT/'ml/nightly-gameplan-runs/20260918T072555.813034Z'
GRID=[0.001,0.01,0.1,1.0]
WEIGHTS=[0.25,0.5,0.75,1.0]
REGULARIZATION='independent-stock-raw-direction-logistic-regularization-v2'
CALIBRATION='independent-stock-information-retaining-development-selection-v2'
SHRINKAGE='independent-stock-weekly-prior-probability-shrinkage-v1'


def repaired_group(run,group,reports,manifest,forecasts):
    # Reuse arithmetic/source/label verification; replace only policy-specific expectations.
    result=base.audit_group(run,group,reports,manifest,forecasts)
    r=reports[group]
    payload=joblib.load(run/r['model_file']['path'])
    cohort_name=r.get('deployment',{}).get('retained_cohort_output',f'training-cohort-{group}.parquet')
    cohort=pd.read_parquet(run/cohort_name)
    parts=_chronological_partitions(cohort,group=group)
    fresh=not bool(r.get('deployment'))
    expected_reg='independent-stock-daily-logistic-regularization-v1' if group=='1d' else REGULARIZATION
    result['checks']['declared_C_grid_matches_authorization']=r['logistic_regularization_candidates']==GRID
    result['checks']['regularization_policy_matches_contract']=(
        r.get('logistic_regularization_policy')==expected_reg and payload.get('logistic_regularization_policy')==expected_reg)
    c=r['calibration_selection']
    if c['policy']==CALIBRATION:
        eligibility=c.get('candidate_eligibility',{})
        candidates=[name for name in ('identity','platt') if eligibility.get(name,{}).get('eligible')]
        selected=min(candidates,key=lambda name:c['candidate_metrics'][name]['log_loss']) if candidates else 'identity'
        fallback=(c.get('selection_status')=='IDENTITY_AFTER_INELIGIBLE_FULL_DEVELOPMENT_REFIT')
        if fallback:
            selection_valid=(selected=='platt' and c['selected_family']=='identity'
                and c.get('full_refit_eligibility',{}).get('eligible') is False
                and eligibility.get('identity',{}).get('eligible') is True)
        else:
            selection_valid=selected==c['selected_family']
        result['checks']['calibrator_selected_by_minimum_development_log_loss']=selection_valid
        result['eligible_calibration_minimum_family']=selected
        result['calibration_full_refit_fallback']=fallback
    result['checks']['new_information_retaining_calibration_policy']=(
        c['policy']==CALIBRATION and r.get('development_selection_policy')==CALIBRATION
        and payload.get('development_selection_policy')==CALIBRATION)
    original=base.read(ORIGINAL_YG/'model-reports.json')[group]
    result['checks']['source_timing_features_not_integrated']=r['features']['admitted']==original['features']['admitted']
    repair_checks={}
    if group=='1w':
        fields=('selected_base_family','probability_shrinkage_policy','selected_probability_shrinkage_weight',
            'selection_shrinkage_prior','fitted_shrinkage_prior','probability_shrinkage_weights')
        repair_checks['report_payload_shrinkage_metadata_agree']=all(r.get(k)==payload.get(k) for k in fields)
        repair_checks['fixed_shrinkage_policy']=r.get('probability_shrinkage_policy')==SHRINKAGE
        repair_checks['fixed_nonzero_weight_grid']=r.get('probability_shrinkage_weights')==WEIGHTS
        weight=r.get('selected_probability_shrinkage_weight')
        repair_checks['selected_weight_in_fixed_grid']=weight in WEIGHTS
        train_prior=float(parts['train'].target.mean())
        fitted_prior=float(pd.concat([parts['train'],parts['selection']]).target.mean())
        repair_checks['selection_prior_from_train_only']=np.isclose(r.get('selection_shrinkage_prior',np.nan),train_prior,rtol=0,atol=1e-15)
        repair_checks['final_prior_from_train_plus_selection']=np.isclose(r.get('fitted_shrinkage_prior',np.nan),fitted_prior,rtol=0,atol=1e-15)
        families=['hist-gradient','mlp',*[f'hist-gradient-mlp-{w:.2f}' for w in (.25,.5,.75)],
            *[f'regularized-logistic-c{v:g}' for v in GRID]]
        names={family if w==1 else f'{family}-prior-shrinkage-w{w:g}' for family in families for w in WEIGHTS}
        repair_checks['all_36_fixed_candidates_recorded']=set(r['selection_metrics'])==names
        expected_name=(r.get('selected_base_family') if weight==1 else
            f"{r.get('selected_base_family')}-prior-shrinkage-w{weight:g}") if weight is not None else None
        repair_checks['selection_name_matches_base_and_weight']=r['selected_family']==expected_name
        estimator=payload['estimator']
        from ml.gameplan_estimators import PriorProbabilityShrinkage
        if weight is not None and weight<1:
            repair_checks['portable_wrapper_class']=isinstance(estimator,PriorProbabilityShrinkage)
            repair_checks['wrapper_weight_matches_report']=getattr(estimator,'weight',None)==weight
            repair_checks['wrapper_prior_matches_fitted_rows']=np.isclose(getattr(estimator,'prior_probability',np.nan),fitted_prior,rtol=0,atol=1e-15)
            x=_model_frame(parts['assessment'],payload['feature_columns'],payload['categorical_columns'])
            expected=weight*estimator.estimator.predict_proba(x)[:,1]+(1-weight)*fitted_prior
            observed=estimator.predict_proba(x)[:,1]
            repair_checks['wrapper_prediction_matches_fixed_formula']=bool(np.allclose(observed,expected,rtol=0,atol=1e-15))
        else:
            repair_checks['weight_one_has_no_shrinkage_wrapper']=not isinstance(estimator,PriorProbabilityShrinkage)
        result['shrinkage']={**{k:r.get(k) for k in fields},'train_prior_recomputed':train_prior,
            'fitted_prior_recomputed':fitted_prior,'checks':{k:bool(v) for k,v in repair_checks.items()}}
    else:
        repair_checks['weekly_shrinkage_not_applied_to_other_groups']=r.get('probability_shrinkage_policy') is None
    result['repair_checks']={k:bool(v) for k,v in repair_checks.items()}
    result['freshly_fitted_in_this_publication']=fresh
    result['all_pass']=all(result['checks'].values()) and all(result['source_checks'].values()) and all(result['contract_checks'].values()) and all(result['chronology_checks'].values()) and all(result['repair_checks'].values())
    return result


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--run',required=True,type=Path)
    parser.add_argument('--enrichment-run',type=Path)
    args=parser.parse_args()
    run=args.run.resolve()
    if run.parent!=(base.ROOT/'ml/nightly-gameplan-runs').resolve() or run in (ORIGINAL_YG.resolve(),base.OG.resolve()):
        raise ValueError('Expected a new native quality-repair publication')
    reports,manifest,receipt,plan=[base.read(run/name) for name in ('model-reports.json','manifest.json','receipt.json','gameplan.json')]
    forecasts=pd.read_parquet(run/'forecasts.parquet')
    og=base.check_og()
    original_manifest=base.read(ORIGINAL_YG/'manifest.json')
    original_checks={'manifest_matches_original_receipt':base.sha(ORIGINAL_YG/'manifest.json')==base.read(ORIGINAL_YG/'receipt.json')['manifest_checksum_sha256'],
        'all_first_YG_outputs_unchanged':all(base.sha(ORIGINAL_YG/name)==item['checksum_sha256'] for name,item in original_manifest['output_files'].items())}
    checks={'manifest_matches_receipt':base.sha(run/'manifest.json')==receipt['manifest_checksum_sha256'],
        'all_outputs_match_manifest':all(base.sha(run/name)==item['checksum_sha256'] for name,item in manifest['output_files'].items()),
        'YG_contract_everywhere':all(item.get('probability_target_contract')==base.CONTRACT and item.get('gameplan_variant')=='YG' for item in (manifest['configuration'],receipt,plan)),
        'raw_direction_manifest_target':manifest['target_column']=='target_raw_price_direction',
        'correct_action_date':receipt['action_date']==plan['action_date']==manifest['configuration']['action_date']=='2026-09-18',
        'full_forecast_grid':len(forecasts)==24*len(plan['symbols']),
        'zero_orders':receipt['orders_placed']==0 and receipt['broker_orders_enabled'] is False}
    result={'verified_at':datetime.now(timezone.utc).isoformat(),'run_path':str(run),'checks':checks,
        'original_OG':og,'original_YG_preservation':original_checks,'directional':{},
        'validation_scope':'Saved-model inference and arithmetic only; no candidate refitting or tuning.',
        'assessment_caveat':'This is a rerun against already-seen assessment observations after a development-selected repair, not a fresh untouched external test. The repair was locked before its new assessment results.',
        'execution_note':'Current manual policy consumes saved instructions independently of promotion; legacy fixed/qualified policies retain their gates. This audit places no orders.'}
    for group in base.GROUPS:
        result['directional'][group]=repaired_group(run,group,reports,manifest,forecasts)
    if args.enrichment_run:
        result['enrichment']=base.audit_enrichment(args.enrichment_run.resolve(),run)
    result['all_artifact_checks_pass']=all(checks.values()) and og['all_pass'] and all(original_checks.values()) and all(v['all_pass'] for v in result['directional'].values()) and result.get('enrichment',{}).get('all_pass',True)
    result['all_four_directional_groups_pass']=all(v['promotion_status']=='PROMOTED' for v in result['directional'].values())
    result['all_forecasts_have_promoted_exact_history']=bool(forecasts.model_status.eq('PROMOTED').all() and forecasts.symbol_route_fitted_target_rows.gt(0).all())
    result['directional_quality_goal_satisfied']=result['all_artifact_checks_pass'] and result['all_four_directional_groups_pass'] and result['all_forecasts_have_promoted_exact_history']
    result['optional_enrichment_all_scopes_qualified']=bool(result.get('enrichment') and all(v['qualified_scope_count']==v['fitted_scope_count'] and v['fitted_scope_count']>0 for v in result['enrichment']['horizons'].values()))
    (OUT/'model-quality-verification.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    lines=['# Locked YG quality-repair saved-model verification','',f"Run `{run}`; verified {result['verified_at']}.",'',
        f"Artifact checks pass: **{result['all_artifact_checks_pass']}**. All four directional groups and exact forecast histories pass: **{result['directional_quality_goal_satisfied']}**.",'',
        result['assessment_caveat'],'',
        '| Group | Selected family | Status | Failed promotion checks | Reproduced-score max error |','|---|---|---|---|---:|']
    for group,v in result['directional'].items():
        lines.append(f"| {group} | {v['selected_family']} | {v['promotion_status']} | {', '.join(v['failed_promotion_checks']) or 'None'} | {v['score_max_abs_error']} |")
    lines+=['','The separate JSON verifies raw-return labels, preserved cost labels, all four C grids, information-retaining development calibration, exact fitted symbol/route support, and the weekly wrapper’s fixed candidate set, selected weight and train-only/final-fit priors. Source timing was not integrated. OG and the first YG remain immutable.']
    if 'enrichment' in result:
        e=result['enrichment']
        lines+=['','## Separate optional enrichment','',f"Run `{e['run_path']}`. All fitted scopes qualified: **{result['optional_enrichment_all_scopes_qualified']}**.",'',
            '| Horizon | Fitted | Fitted scopes | Qualified scopes |','|---|---|---:|---:|']
        for group in base.GROUPS:
            v=e['horizons'][group]
            lines.append(f"| {group} | {v['fitted']} | {v['fitted_scope_count']} | {v['qualified_scope_count']} |")
        lines+=['',e['probability_semantics']]
    lines+=['',result['validation_scope'],'',result['execution_note']]
    (OUT/'model-quality-verification.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps({'all_artifact_checks_pass':result['all_artifact_checks_pass'],
        'directional_quality_goal_satisfied':result['directional_quality_goal_satisfied'],
        'directional':{g:{k:v[k] for k in ('promotion_status','failed_promotion_checks','all_pass','score_max_abs_error')} for g,v in result['directional'].items()},
        'optional_enrichment_all_scopes_qualified':result['optional_enrichment_all_scopes_qualified']},indent=2))


if __name__=='__main__':
    main()
