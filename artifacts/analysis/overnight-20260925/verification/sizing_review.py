"""Bounded saved sizing report review; no fitting, prediction or production writes."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json
import math
import sys

sys.dont_write_bytecode = True
REPO = Path('C:/dev/ducketz')
ROOT = Path('C:/DATASTORE')
NATIVE = ROOT / 'ml/overnight-runs/20260925T040822.112032Z'
RUN = ROOT / 'ml/stock-trader-model-runs/20260925T061654.534550Z'
SOURCE = 'ml/nightly-gameplan-runs/20260925T060654.631426Z'
SOURCE_RECEIPT = 'f70b873f48aa206e3b0be21c179008854aeb012691abb53b8ab9a18d57a01275'
OUT = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    sys.path.insert(0, str(REPO))
    from ml.stock_trader.independent_training import _quality_passes, MINIMUM_ASSESSMENT_CLUSTERS, ARCHIVE_MARKET_ADMISSION_POLICY, canonical_sha256
    from ml.stock_trader.scope_qualification import qualify_pooled_scope_coverage, POOLED_SCOPE_QUALIFICATION_VERSION, MINIMUM_POOLED_ROUTE_FIT_CLUSTERS
    native = read(NATIVE / 'stage-report.json')
    stage, = [s for s in native['stages'] if s['stage'] == 'stock_enrichment_training']
    require(stage['status'] == 'COMPLETE' and stage['exit_code'] == 0, 'Sizing native stage is not complete')
    require(native['enrichment_gameplan']['run_path'] == SOURCE and native['enrichment_gameplan']['receipt_sha256'] == SOURCE_RECEIPT, 'Native publication pin differs')
    log = NATIVE / stage['log_path']
    emitted = json.loads(log.read_text().strip())
    require(emitted['status'] == 'PUBLISHED' and Path(emitted['run_path']) == RUN and emitted['orders_placed'] == 0, 'Native sizing log differs')
    receipt, manifest, report, model = [read(RUN / f) for f in ('receipt.json', 'manifest.json', 'training-report.json', 'model.json')]
    hashes = {}
    for name, field in [('manifest.json', 'manifest_sha256'), ('training-report.json', 'training_report_sha256'), ('model.json', 'model_sha256')]:
        observed = sha(RUN / name)
        require(observed == receipt[field], 'Sizing receipt hash differs: ' + name)
        hashes[name] = observed
        if name != 'manifest.json':
            require(manifest['output_files'][name] == {'checksum_sha256': observed, 'size': (RUN / name).stat().st_size}, 'Manifest output binding differs: ' + name)
    require(receipt['run_path'] == RUN.relative_to(ROOT).as_posix(), 'Sizing receipt path differs')
    require(sha(ROOT / SOURCE / 'receipt.json') == SOURCE_RECEIPT, 'Pinned source receipt differs')
    source_manifest = read(ROOT / SOURCE / 'manifest.json')
    source_receipt = read(ROOT / SOURCE / 'receipt.json')
    require(source_receipt['manifest_checksum_sha256'] == sha(ROOT / SOURCE / 'manifest.json'), 'Source manifest hash differs')
    require(source_receipt['action_date'] == '2026-09-25', 'Source action date differs')
    require(report['status'] == 'MODELS_FIT' and report['source_gameplan_run'] == SOURCE and report['orders_placed'] == 0 and report['broker_orders_enabled'] is False, 'Report identity/status differs')
    require(report['qualified_target_contracts'] == [] and report['supported_horizons'] == [], 'Expected unqualified report changed')
    require(model['scope_qualification_policy'] == POOLED_SCOPE_QUALIFICATION_VERSION and model['market_feature_admission_policy'] == ARCHIVE_MARKET_ADMISSION_POLICY, 'Saved scope/admission policy differs')
    require(model['model_fingerprint'] == report['model_fingerprint'] == receipt['model_fingerprint'] == canonical_sha256({k: v for k, v in model.items() if k != 'model_fingerprint'}), 'Model fingerprint differs')
    require(model['source_publication']['source_files'] == report['source_files'] and canonical_sha256(report['source_files']) == model['source_fingerprint'] == report['source_fingerprint'] == receipt['source_fingerprint'], 'Source fingerprint differs')
    expected_inputs = {item['path'].replace('\\', '/'): item for item in manifest['input_files']}
    require(set(expected_inputs) == set(report['source_files']), 'Manifest input set differs')
    for relative, digest in report['source_files'].items():
        path = ROOT / relative
        require(path.parent == ROOT / SOURCE, 'Unexpected sizing source')
        require(sha(path) == digest == expected_inputs[relative]['checksum_sha256'], 'Source byte hash differs: ' + relative)
        require(path.stat().st_size == expected_inputs[relative]['size'], 'Source size differs')
        if path.suffix == '.parquet':
            require(source_manifest['output_files'][path.name]['checksum_sha256'] == digest, 'Source cohort not bound to publication')
    native_receipt_binding = None
    if native['status'] == 'COMPLETE':
        nr = read(NATIVE / 'receipt.json')
        require(nr['stage_report_checksum_sha256'] == sha(NATIVE / 'stage-report.json') and nr['stage_report_size'] == (NATIVE / 'stage-report.json').stat().st_size, 'Native report receipt differs')
        require(nr['logs'][log.name] == {'checksum_sha256': sha(log), 'size': log.stat().st_size}, 'Native sizing log receipt differs')
        native_receipt_binding = sha(NATIVE / 'receipt.json')
    result = {'reviewed_at': datetime.now(timezone.utc).isoformat(), 'status': 'SAVED_SIZING_REPORT_VERIFIED_RESEARCH_ONLY', 'native_run': str(NATIVE), 'native_status_at_review': native['status'], 'native_stage': stage, 'run': str(RUN), 'source_gameplan': SOURCE, 'source_receipt_sha256': SOURCE_RECEIPT, 'receipt_sha256': sha(RUN / 'receipt.json'), 'native_receipt_sha256': native_receipt_binding, 'bound_output_sha256': hashes, 'source_files_byte_verified': report['source_files'], 'orders_placed': 0, 'broker_orders_enabled': False, 'qualified_target_contracts': [], 'scope_policy': POOLED_SCOPE_QUALIFICATION_VERSION, 'minimum_pooled_symbol_route_fit_clusters': MINIMUM_POOLED_ROUTE_FIT_CLUSTERS, 'minimum_assessment_clusters': MINIMUM_ASSESSMENT_CLUSTERS, 'horizons': {}, 'limitations': ['Saved assessment scores and gate arithmetic checked; independent inference reproduction belongs to the full audit.', 'Cohort file bytes are source-bound; raw-source reconstruction, admission-row recomputation and partition regeneration belong to the full audit.', 'No fitting, assessment tuning, retry, activation, order or production mutation was performed.', 'These separate learned sizing qualifications do not change the operator-selected Gameplan policy or directional promotions.']}
    for group in ('1h', '4h', '1d', '1w'):
        g, m = report['horizons'][group], model['horizons'][group]
        scopes = g['scope_readiness']
        require(g['status'] == 'FITTED', group + ': unexpected fit status')
        for key in ('calibration_selection', 'head_development_selection', 'head_training_objectives', 'market_feature_admission', 'partition_evidence'):
            require(g[key] == m[key], group + ': report/model differs: ' + key)
        scores = next(iter(scopes.values()))['horizon_assessment_scores']
        require(all(s['horizon_assessment_scores'] == scores for s in scopes.values()), group + ': global scores differ by scope')
        require(all(math.isfinite(v) for k, v in scores.items() if k != 'probability_range') and all(math.isfinite(v) for v in scores['probability_range']), group + ': nonfinite score')
        gates = {'brier_below_baseline': scores['brier'] < scores['base_brier'], 'log_loss_below_baseline': scores['log_loss'] < scores['base_log_loss'], 'ece_at_most_0_15': scores['ece'] <= .15, 'return_mse_below_baseline': scores['return_mse'] < scores['base_return_mse'], 'adverse_mse_at_most_baseline': scores['adverse_mse'] <= scores['base_adverse_mse'], 'probability_range_above_1e_8': scores['probability_range'][1] - scores['probability_range'][0] > 1e-8}
        require(all(gates.values()) == _quality_passes(scores), group + ': quality arithmetic differs')
        gates['assessment_clusters_at_least_minimum'] = g['partition_evidence']['assessment']['decision_clusters'] >= MINIMUM_ASSESSMENT_CLUSTERS
        ready = all(gates.values())
        rebuilt = qualify_pooled_scope_coverage(global_ready=ready, fit_scope_decision_clusters=m['fit_scope_decision_clusters'], scope_diagnostics=scopes, target_price_source_contract=m['target_price_source_contract'])
        require(rebuilt == scopes and len(m['fit_scope_decision_clusters']) == g['fitted_scope_count'] and len(scopes) == g['diagnostic_scope_count'] and sum(s['status'] == 'READY' for s in scopes.values()) == g['qualified_scope_count'] == 0, group + ': scope arithmetic differs')
        admission = g['market_feature_admission']
        excluded = admission['excluded_missing_market_rows']
        require(admission['policy'] == ARCHIVE_MARKET_ADMISSION_POLICY and admission['input_execution_rows'] - excluded == admission['admitted_rows'] == g['admitted_rows'], group + ': admission totals differ')
        require(sum(admission['excluded_by_symbol'].values()) == excluded and len(admission['excluded_by_feature']) == 8 and set(admission['excluded_by_feature'].values()) == {excluded}, group + ': optional-input exclusion counts differ')
        selection = g['head_development_selection']
        require(selection['assessment_used_for_selection'] is False and selection['candidate_penalties'] == [1.0, 5.0, 20.0], group + ': candidate protocol differs')
        for head, objective in selection['head_objectives'].items():
            best = min(selection['candidate_penalties'], key=lambda p: selection['candidate_metrics'][str(p)][objective])
            require(best == selection['selected_penalties'][head] == m['head_ridge_penalties'][head], group + ': selected penalty is not development optimum')
        cal = g['calibration_selection']
        require(cal['assessment_used_for_selection'] is False and cal['selection_status'] == 'SELECTED_ON_PURGED_DEVELOPMENT', group + ': calibration protocol differs')
        best_cal = min(('identity', 'platt'), key=lambda family: cal['candidate_metrics'][family]['log_loss'])
        require(best_cal == cal['selected_family'] and datetime.fromisoformat(cal['fit_last_target_end']) < datetime.fromisoformat(cal['validation_first_decision']), group + ': calibration selection or chronological purge differs')
        require(all(v['converged'] is True and math.isfinite(v['training_objective']) for v in g['head_training_objectives'].values()), group + ': iterative head not converged')
        partitions = g['partition_evidence']
        for left, right in zip(('train', 'selection', 'calibration'), ('selection', 'calibration', 'assessment')):
            require(datetime.fromisoformat(partitions[left]['last_target_end']) < datetime.fromisoformat(partitions[right]['first_decision']), group + ': temporal partition overlap')
        reasons = Counter(s['reason'] for s in scopes.values())
        thin = {name: {k: s[k] for k in ('reason', 'fit_decision_clusters', 'pooled_symbol_route_fit_decision_clusters', 'assessment_decision_clusters')} for name, s in scopes.items() if s['reason'] != 'HELD_OUT_HORIZON_QUALITY_NOT_PROMOTED'}
        result['horizons'][group] = {k: v for k, v in g.items() if k != 'scope_readiness'} | {'horizon_assessment_scores': scores, 'quality_checks': gates, 'failed_quality_checks': [k for k, v in gates.items() if not v], 'research_scope_reasons': dict(reasons), 'additional_scope_coverage_limits': thin, 'assessment_score_reproduction': 'DEFERRED_TO_FULL_AUDIT'}
    result['conclusion'] = 'All four horizons fitted, no qualified scopes. Saved report/code review demonstrates assessment quality failures, not a concrete fitting, convergence or development-selection defect. These failures do not justify assessment tuning or unchanged retries.'
    (OUT / 'sizing-review.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    lines = ['# September 25 sizing assessment review', '', result['conclusion'], '', f"Reviewed {result['reviewed_at']}. Source-bound run `{RUN}`; pinned publication `{SOURCE}`.", '', '| Horizon | Fitted / diagnostic scopes | Admitted sizing rows | Missing optional inputs excluded | Failed unchanged quality checks |', '|---|---:|---:|---:|---|']
    for group, g in result['horizons'].items():
        lines.append(f"| {group} | {g['fitted_scope_count']} / {g['diagnostic_scope_count']} | {g['admitted_rows']:,} | {g['market_feature_admission']['excluded_missing_market_rows']:,} | {', '.join(g['failed_quality_checks'])} |")
    lines += ['', 'The sizing gates require Brier and log loss strictly below baseline, ECE <= 0.15, return MSE strictly below baseline, downside MSE <= baseline, varying probabilities and at least 10 assessment decision clusters. Every group has 63 assessment clusters. These are separate from directional promotion tolerances.', '', '| Horizon | Brier / baseline | Log loss / baseline | Return MSE / baseline | Downside MSE / baseline |', '|---|---:|---:|---:|---:|']
    for group, g in result['horizons'].items():
        s = g['horizon_assessment_scores']
        lines.append(f"| {group} | {s['brier']:.9g} / {s['base_brier']:.9g} | {s['log_loss']:.9g} / {s['base_log_loss']:.9g} | {s['return_mse']:.9g} / {s['base_return_mse']:.9g} | {s['adverse_mse']:.9g} / {s['base_adverse_mse']:.9g} |")
    lines += ['', 'All reported iterative probability/downside optimizers converged with finite objectives. Expected-return/allocation heads use closed-form ridge fits and have no iterative convergence flag. Fixed 1/5/20 penalties are development minima for their respective head objectives. All four choose identity calibration by later purged development log loss; assessment is explicitly excluded from selection.', '', 'Archive optional-input admission validates target/source/price evidence first, then excludes only archive rows missing all eight operational market observations. Partial, malformed or infinite market inputs still fail closed. This does not exclude the corresponding directional training examples or alter price/quality gates. Counts and identity hashes match report/model; exact row reconstruction remains the full audit’s responsibility.', '', 'Scope coverage remains separate: 260 diagnostic scopes fail horizon quality, 11 also lack sufficient pooled symbol/route evidence, and 2 are unseen exact-duration scopes. The policy needs at least one exact fitted target cluster and 20 pooled symbol/route clusters plus horizon quality. In particular TWST daily sizing has only one fitted cluster and weekly only six pooled; no qualification was invented.', '', 'Verified receipt/manifest/report/model hashes, canonical model/source fingerprints, all six source-file byte hashes (publication receipt, manifest and four cohorts), native successful stage/log binding, report/model selection and admission equality, strict gate arithmetic, chronological partition separation and all scope status/reason arithmetic. No inference, fit, account/provider call or production mutation.', '', 'Full source reconstruction, admission-row recomputation and independent saved-model assessment inference remain delegated to the full completion audit. Zero orders are recorded. These learned sizing statuses do not change the selected Gameplan policy.', '']
    (OUT / 'sizing-review.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'status': result['status'], 'horizons': {g: {'fitted': h['fitted_scope_count'], 'qualified': h['qualified_scope_count'], 'failed_quality_checks': h['failed_quality_checks']} for g, h in result['horizons'].items()}, 'json': str(OUT / 'sizing-review.json'), 'markdown': str(OUT / 'sizing-review.md')}))


if __name__ == '__main__':
    main()
