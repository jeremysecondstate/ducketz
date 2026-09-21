"""Offline YG supplement to verify_completed_run.py; never fits or reads archives/accounts.

Run only after the native terminal attempt is COMPLETE. The output distinguishes
artifact integrity from model qualification: a faithfully reported research model
is a coverage note, not an audit failure. No target, policy, score or state changes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
ROOT = Path('C:/DATASTORE')
REPOSITORY = Path('C:/dev/ducketz')
TARGET = 'raw-price-direction-v1'
VARIANT = 'YG'
GROUPS = ('1h', '4h', '1d', '1w')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def target_matches(item):
    return item.get('probability_target_contract') == TARGET and item.get('gameplan_variant') == VARIANT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--overnight-run', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--publication-only', action='store_true',
                        help='Verify frozen models after a terminal tail failure; explicitly does not verify native completion')
    parser.add_argument('--expected-original-run', default='20260919T040748.657031Z')
    parser.add_argument('--expected-action-date', default='2026-09-21')
    parser.add_argument('--expected-deadline', default='2026-09-21T11:00:00Z')
    args = parser.parse_args()
    output = args.output.resolve()
    require(output.is_relative_to((REPOSITORY/'artifacts/analysis/overnight-20260919').resolve()),
            'Audit output must remain in this operation artifact directory')
    native = args.overnight_run.resolve()
    require(native.parent == (ROOT/'ml/overnight-runs').resolve(), 'Unexpected native run directory')
    result = {'verified_at': datetime.now(timezone.utc).isoformat(), 'overnight_run': str(native),
              'scope': 'Read-only saved-model inference and arithmetic; no fit, archive/account/provider/broker reads.',
              'orders_placed': 0, 'errors': [], 'coverage_notes': [], 'directional': {}}
    report = read(native/'stage-report.json')
    receipt = read(native/'receipt.json') if (native/'receipt.json').is_file() else {}
    result.update(native_report_status=report.get('status'), native_receipt_status=receipt.get('status', 'MISSING'),
                  verification_scope='PUBLICATION_MODELS_ONLY' if args.publication_only else 'NATIVE_COMPLETION_AND_MODELS',
                  full_native_completion_verified=False)
    ready = (report.get('status') == receipt.get('status') and report.get('status') in
             ({'COMPLETE', 'FAILED', 'CANCELLED'} if args.publication_only else {'COMPLETE'}))
    if not ready:
        result.update(status='NOT_READY_FOR_FINAL_VERIFICATION', heavy_checks_started=False,
                      native_report_status=report.get('status'), native_receipt_status=receipt.get('status', 'MISSING'))
        output.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({key: result[key] for key in ('status', 'heavy_checks_started')}))
        return 2

    # Imports happen after the small native completion guard.
    sys.path.insert(0, str(REPOSITORY))
    import joblib
    import numpy as np
    import pandas as pd
    from ml.nightly_gameplan import read_gameplan_run, _chronological_partitions, _model_frame, _proper_scores, _verify_probability_cohort
    from ml.gameplan_promotion import build_promotion_gate, DIRECTIONAL_PROMOTION_POLICY
    from ml.stock_trader.independent_signals import verified_promoted_model_groups, _validated_independent_forecasts
    from ml.gameplan_development_selection import (
        logistic_regularization_candidates, logistic_regularization_policy, development_selection_policy,
        WEEKLY_PROBABILITY_SHRINKAGE_POLICY, WEEKLY_PROBABILITY_SHRINKAGE_WEIGHTS,
    )
    from ml.gameplan_estimators import PriorProbabilityShrinkage
    from datafetching.symbol_universe import read_symbols

    try:
        current, seen, chain, successful_stages = native, set(), [], set()
        pin = report.get('enrichment_gameplan')
        require(pin and pin.get('run_path'), 'Native completion lacks a pinned Gameplan')
        while current:
            require(current.parent == (ROOT/'ml/overnight-runs').resolve() and current not in seen,
                    'Invalid or cyclic native ancestry')
            seen.add(current)
            r, rc = read(current/'stage-report.json'), read(current/'receipt.json')
            require(target_matches(r), 'Native ancestry changed YG target identity')
            require(rc.get('status') == r.get('status') and rc.get('run_path') == current.relative_to(ROOT).as_posix(),
                    'Native receipt status/path disagrees')
            require(rc.get('stage_report_checksum_sha256') == sha(current/'stage-report.json')
                    and rc.get('stage_report_size') == (current/'stage-report.json').stat().st_size,
                    'Native receipt does not bind report checksum and size')
            require(all(item.get('orders_placed') == 0 and item.get('broker_orders_enabled') is False for item in (r, rc)),
                    'Native attempt does not explicitly record zero orders')
            require(pd.Timestamp(r['deadline_at']) == pd.Timestamp(args.expected_deadline)
                    and pd.Timestamp(r.get('effective_deadline_at', r['deadline_at'])) == pd.Timestamp(args.expected_deadline)
                    and r.get('deadline_exception') is None, 'Original deadline changed or exception applied')
            require(r.get('enrichment_gameplan') in (None, pin), 'Native ancestry changed pinned publication')
            # Native receipts carry target identity through their immutable report checksum;
            # their current schema intentionally has no duplicate target fields.
            if 'probability_target_contract' in rc or 'gameplan_variant' in rc:
                require(target_matches(rc), 'Explicit native receipt target disagrees')
            chain.append({'run': str(current), 'status': r['status'], 'target': TARGET,
                          'receipt_binds_target_via_report_checksum': True})
            successful_stages.update(item['stage'] for item in r.get('stages', [])
                                     if item.get('status') == 'COMPLETE' and item.get('exit_code') == 0)
            current = Path(r['resumed_from']).resolve() if r.get('resumed_from') else None
        require(Path(chain[-1]['run']).name == args.expected_original_run, 'Native root attempt differs')
        require('gameplan_publication' in successful_stages, 'Native ancestry lacks completed publication evidence')
        run = (ROOT/pin['run_path']).resolve()
        require(sha(run/'receipt.json') == pin['receipt_sha256'], 'Pinned receipt differs')
        publication = read_gameplan_run(ROOT, run)
        cfg, receipt = publication.manifest['configuration'], publication.receipt
        require(target_matches(cfg) and target_matches(receipt) and target_matches(read(run/'gameplan.json')),
                'Publication is not the required raw-direction YG target')
        require(receipt['action_date'] == cfg['action_date'] == args.expected_action_date, 'Publication action date differs')
        require(pd.Timestamp(receipt['published_at']) < pd.Timestamp(args.expected_deadline), 'Publication missed original deadline')
        require(cfg['target_price_source_contract'] == 'xnas-itch-archive-v1' and cfg['target_price_dataset'] == 'XNAS.ITCH',
                'Publication price source differs')
        symbols = tuple(read_symbols(REPOSITORY/'datafetching/watchlist.txt'))
        require(set(cfg['symbols']) == set(symbols), 'Publication universe differs from production watchlist')
        forecasts = _validated_independent_forecasts(pd.read_parquet(run/'forecasts.parquet'),
                    action_date=args.expected_action_date, symbols=symbols)
        require(len(forecasts) == 24*len(symbols) and forecasts.groupby('symbol').size().eq(24).all(), 'Forecast grid differs')
        require(forecasts.probability_target_contract.eq(TARGET).all() and forecasts.gameplan_variant.eq(VARIANT).all(),
                'Forecast target metadata differs')
        reports = read(run/'model-reports.json')
        promoted = verified_promoted_model_groups(publication)
        result.update(publication_run=str(run), native_ancestry=chain, expected_action_date=args.expected_action_date,
                      probability_target_contract=TARGET, gameplan_variant=VARIANT, forecast_rows=len(forecasts),
                      symbols=list(symbols), verified_promoted_groups=sorted(promoted), heavy_checks_started=True,
                      native_probability_metadata_verifier_passed=True,
                      all_forecasts_promoted_exact_history=bool(forecasts.model_status.eq('PROMOTED').all()
                          and forecasts.symbol_fitted_target_rows.gt(0).all()
                          and forecasts.symbol_route_fitted_target_rows.gt(0).all()))
        unsupported = forecasts.loc[forecasts.model_status.eq('RESEARCH_NO_TARGET_HISTORY'), ['symbol','route','model_group']]
        if len(unsupported):
            result['coverage_notes'].append({'exact_fitted_history_unavailable': unsupported.to_dict('records')})
    except Exception as exc:
        result['errors'].append({'check': 'native_and_publication', 'error': f'{type(exc).__name__}: {exc}'})
        run = None

    if run is not None:
        for group in GROUPS:
            try:
                r = reports[group]
                cohort_name = r.get('deployment', {}).get('retained_cohort_output', f'training-cohort-{group}.parquet')
                model_name = r['model_file']['path']
                for name in (cohort_name, model_name):
                    require(name in publication.manifest['output_files'] and (run/name).resolve().is_relative_to(run),
                            'Model/cohort path is not manifest-bound')
                cohort = pd.read_parquet(run/cohort_name)
                fresh = pd.read_parquet(run/f'training-cohort-{group}.parquet')
                payload = joblib.load(run/model_name)
                require(target_matches(r) and target_matches(payload), 'Report/model target differs')
                _verify_probability_cohort(cohort, TARGET)
                _verify_probability_cohort(fresh, TARGET)
                parts = _chronological_partitions(cohort, group=group)
                fit = pd.concat([parts['train'], parts['selection']], ignore_index=True)
                assessment = parts['assessment']
                x = _model_frame(assessment, payload['feature_columns'], payload['categorical_columns'])
                raw = payload['estimator'].predict_proba(x)[:, 1]
                prob = payload['calibrator'].predict(raw)
                scores = _proper_scores(assessment.target.to_numpy(), prob)
                raw_scores = _proper_scores(assessment.target.to_numpy(), raw)
                baseline = _proper_scores(assessment.target.to_numpy(), np.full(len(assessment), fit.target.mean()))
                errors = [abs(float(actual[k])-float(expected[k])) for actual, expected in
                          ((scores, r['assessment']), (raw_scores, r['assessment_raw_scores']),
                           (baseline, r['training_base_rate_assessment'])) for k in expected]
                require(max(errors) < 1e-12, 'Saved model assessment scores do not reproduce')
                cal = parts['calibration']
                cal_raw = payload['estimator'].predict_proba(_model_frame(cal, payload['feature_columns'], payload['categorical_columns']))[:, 1]
                cal_prob = payload['calibrator'].predict(cal_raw)
                ranges = {'raw_probability_range': [float(cal_raw.min()), float(cal_raw.max())],
                          'calibrated_probability_range': [float(cal_prob.min()), float(cal_prob.max())],
                          'assessment_probability_range': [float(prob.min()), float(prob.max())]}
                require(all(np.allclose(v, r['calibration_diagnostics'][k], rtol=1e-12, atol=1e-12) for k,v in ranges.items()),
                        'Saved probability variation does not reproduce')
                gate = build_promotion_gate(scores, baseline, r['calibration_diagnostics'],
                            assessment.decision_timestamp.nunique(), policy_version=r['promotion_gate']['policy_version'])
                require(gate == r['promotion_gate'], 'Recorded numerical promotion gate does not reproduce')
                checks = {}
                for name, frame in parts.items():
                    checks[f'{name}_counts'] = (len(frame) == r['partitions'][f'{name}_rows']
                        and frame.decision_timestamp.nunique() == r['partition_decision_clusters'][name])
                for left, right in zip(('train','selection','calibration'), ('selection','calibration','assessment')):
                    checks[f'{left}_before_{right}_target'] = pd.to_datetime(parts[left].target_window_end, utc=True).max() < pd.to_datetime(parts[right].target_window_start, utc=True).min()
                    checks[f'{left}_before_{right}_source_cutoff'] = pd.to_datetime(parts[left].target_window_end, utc=True).max() < pd.to_datetime(parts[right].source_effective_cutoff, utc=True).min()
                checks['same_source'] = (cohort.target_price_source_contract.eq('xnas-itch-archive-v1').all()
                                        and cohort.target_price_dataset.eq('XNAS.ITCH').all())
                checks['observed_boundaries_within_five_minutes'] = (cohort.target_boundary_aligned.eq(True).all()
                                        and cohort[['target_start_gap_seconds','target_end_gap_seconds']].abs().le(300).all().all())
                checks['returns_match_observed_prices'] = np.allclose(cohort.observed_return, cohort.target_close/cohort.target_open-1, rtol=1e-10, atol=1e-12)
                checks['labels_mature_at_fit'] = pd.to_datetime(cohort.target_window_end, utc=True).le(pd.Timestamp(payload['trained_at'])).all()
                grid = list(logistic_regularization_candidates(group, probability_target=TARGET))
                reg = logistic_regularization_policy(group, probability_target=TARGET)
                checks['fixed_logistic_grid'] = r['logistic_regularization_candidates'] == grid
                checks['regularization_policy'] = r.get('logistic_regularization_policy') == payload.get('logistic_regularization_policy') == reg
                checks['v2_promotion_policy'] = gate['policy_version'] == DIRECTIONAL_PROMOTION_POLICY
                selection = r['selection_metrics']
                checks['fixed_C_candidates_present'] = all(f'regularized-logistic-c{c:g}' in selection for c in grid)
                selected = r['selected_family']
                min_loss = min(float(v['log_loss']) for v in selection.values())
                checks['minimum_development_log_loss'] = float(selection[selected]['log_loss']) == min_loss
                c = r['calibration_selection']
                eligible = [name for name in ('identity','platt') if c.get('candidate_eligibility', {}).get(name, {}).get('eligible')]
                selected_cal = min(eligible, key=lambda name: c['candidate_metrics'][name]['log_loss']) if eligible else 'identity'
                fallback = c.get('selection_status') == 'IDENTITY_AFTER_INELIGIBLE_FULL_DEVELOPMENT_REFIT'
                checks['eligible_calibration_selection'] = ((selected_cal == 'platt' and c['selected_family'] == 'identity'
                    and c.get('full_refit_eligibility', {}).get('eligible') is False
                    and c.get('candidate_eligibility', {}).get('identity', {}).get('eligible') is True)
                    if fallback else selected_cal == c['selected_family'])
                checks['development_calibration_policy'] = c['policy'] == r.get('development_selection_policy') == payload.get('development_selection_policy') == development_selection_policy(TARGET)
                checks['assessment_not_used_in_calibration_selection'] = c['assessment_used_for_selection'] is False
                support_symbols = fit.groupby('symbol').size().to_dict()
                support_routes = fit.groupby(['symbol','route']).size().to_dict()
                support_errors = []
                for row in forecasts.loc[forecasts.model_group.eq(group)].itertuples():
                    symbol_count = int(support_symbols.get(row.symbol, 0))
                    route_count = int(support_routes.get((row.symbol, row.route), 0))
                    expected_status = gate['status'] if symbol_count and route_count else 'RESEARCH_NO_TARGET_HISTORY'
                    saved = r['target_support_by_symbol'].get(row.symbol, {})
                    if (row.symbol_fitted_target_rows != symbol_count or row.symbol_route_fitted_target_rows != route_count
                            or saved.get('fitted_rows', 0) != symbol_count
                            or saved.get('fitted_rows_by_route', {}).get(row.route, 0) != route_count
                            or row.model_status != expected_status or row.model_artifact != model_name):
                        support_errors.append({'symbol': row.symbol, 'route': row.route, 'expected_status': expected_status})
                checks['exact_symbol_route_support_and_status'] = not support_errors
                deployment = r.get('deployment')
                if deployment:
                    champion = read_gameplan_run(ROOT, ROOT/deployment['source_run'])
                    champion_report = read(champion.run_directory/'model-reports.json')[group]
                    checks['retained_champion_same_target'] = target_matches(champion.manifest['configuration']) and target_matches(champion_report)
                shrinkage = None
                if group == '1w':
                    fields = ('selected_base_family','probability_shrinkage_policy','selected_probability_shrinkage_weight',
                              'selection_shrinkage_prior','fitted_shrinkage_prior','probability_shrinkage_weights')
                    checks['shrinkage_payload_matches_report'] = all(r.get(k) == payload.get(k) for k in fields)
                    checks['fixed_weekly_shrinkage_policy'] = r.get('probability_shrinkage_policy') == WEEKLY_PROBABILITY_SHRINKAGE_POLICY
                    checks['fixed_weekly_weight_grid'] = r.get('probability_shrinkage_weights') == list(WEEKLY_PROBABILITY_SHRINKAGE_WEIGHTS)
                    weight = r.get('selected_probability_shrinkage_weight')
                    checks['selected_weight_in_grid'] = weight in WEEKLY_PROBABILITY_SHRINKAGE_WEIGHTS
                    families = ['hist-gradient', 'mlp', *[f'hist-gradient-mlp-{w:.2f}' for w in (.25,.5,.75)],
                                *[f'regularized-logistic-c{c:g}' for c in grid]]
                    candidate_names = {family if w == 1 else f'{family}-prior-shrinkage-w{w:g}'
                                       for family in families for w in WEEKLY_PROBABILITY_SHRINKAGE_WEIGHTS}
                    checks['all_fixed_weekly_candidates_recorded'] = set(selection) == candidate_names
                    checks['no_shrinkage_wins_exact_loss_ties'] = (weight == 1 or not any(
                        float(selection[name]['log_loss']) == min_loss for name in families))
                    checks['selected_name_matches_family_and_weight'] = (selected == (r.get('selected_base_family')
                        if weight == 1 else f"{r.get('selected_base_family')}-prior-shrinkage-w{weight:g}")) if weight is not None else False
                    checks['selection_prior_train_only'] = np.isclose(r.get('selection_shrinkage_prior', np.nan), parts['train'].target.mean(), rtol=0, atol=1e-15)
                    checks['fitted_prior_train_plus_selection'] = np.isclose(r.get('fitted_shrinkage_prior', np.nan), fit.target.mean(), rtol=0, atol=1e-15)
                    estimator = payload['estimator']
                    if weight is not None and weight < 1:
                        checks['portable_shrinkage_wrapper'] = isinstance(estimator, PriorProbabilityShrinkage)
                        expected_raw = weight*estimator.estimator.predict_proba(x)[:,1] + (1-weight)*fit.target.mean()
                        checks['weekly_shrinkage_formula'] = np.allclose(raw, expected_raw, rtol=0, atol=1e-15)
                    else:
                        checks['no_wrapper_for_weight_one'] = not isinstance(estimator, PriorProbabilityShrinkage)
                    shrinkage = {k: r.get(k) for k in fields}
                failed = [name for name, passed in checks.items() if not bool(passed)]
                detail = {'status': 'VERIFIED' if not failed else 'FAILED', 'checks': {k: bool(v) for k,v in checks.items()},
                    'cohort_output': cohort_name, 'retained_champion': deployment, 'fitted_rows': len(fit),
                    'score_max_abs_error': max(errors), 'assessment': scores, 'raw_assessment': raw_scores, 'baseline': baseline,
                    'promotion_status': gate['status'], 'promotion_policy': gate['policy_version'],
                    'failed_promotion_checks': [k for k,v in gate['checks'].items() if not v],
                    'forecast_status_counts': forecasts.loc[forecasts.model_group.eq(group), 'model_status'].value_counts().to_dict(),
                    'support_errors': support_errors, 'selected_family': selected, 'shrinkage': shrinkage}
                result['directional'][group] = detail
                if failed:
                    result['errors'].append({'check': group, 'failed_checks': failed})
                if gate['status'] != 'PROMOTED':
                    result['coverage_notes'].append({'group': group, 'status': gate['status'], 'failed_promotion_checks': detail['failed_promotion_checks']})
            except Exception as exc:
                result['errors'].append({'check': group, 'error': f'{type(exc).__name__}: {exc}'})
    result['all_directional_groups_promoted'] = len(result['directional']) == 4 and all(v['promotion_status'] == 'PROMOTED' for v in result['directional'].values())
    if args.publication_only:
        result['status'] = ('MODEL_ARTIFACT_VERIFICATION_FAILED' if result['errors'] else
                            'MODEL_ARTIFACTS_VERIFIED_NATIVE_' + str(report.get('status', 'UNKNOWN')))
    else:
        result['status'] = 'FAILED' if result['errors'] else 'VERIFIED_WITH_COVERAGE_NOTES' if result['coverage_notes'] else 'VERIFIED'
        result['full_native_completion_verified'] = not bool(result['errors'])
    result['verifier'] = {'path': str(Path(__file__).resolve()), 'sha256': sha(Path(__file__))}
    output.write_text(json.dumps(result, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'all_directional_groups_promoted': result['all_directional_groups_promoted'],
                      'errors': result['errors'], 'output': str(output)}, indent=2))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
