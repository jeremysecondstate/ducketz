"""Bounded pinned sizing report arithmetic; no cohorts, inference, fit or account calls."""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import hashlib
import json
import math
import sys

OUT = Path(__file__).resolve().parent
REPO = Path('C:/dev/ducketz')
ROOT = Path('C:/DATASTORE')
NATIVE = ROOT/'ml/overnight-runs/20260926T040723.834511Z'
sys.dont_write_bytecode = True


def read(path):
    if path.stat().st_size > 16*1024*1024:
        raise ValueError('Bounded report read exceeded 16 MiB: '+str(path))
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    if path.stat().st_size > 16*1024*1024:
        raise ValueError('Bounded report hash exceeded 16 MiB: '+str(path))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def main():
    native = read(NATIVE/'stage-report.json')
    stages = [s for s in native.get('stages', []) if s['stage'] == 'stock_enrichment_training']
    if len(stages) != 1 or stages[0].get('status') != 'COMPLETE' or stages[0].get('exit_code') != 0:
        print(json.dumps({'status': 'WAITING_FOR_COMPLETED_PINNED_ENRICHMENT', 'native_status': native.get('status')}))
        return 2
    from audit_environment import verify_environment
    implementation = verify_environment()
    sys.path.insert(0, str(REPO))
    from ml.stock_trader.independent_training import _quality_passes, MINIMUM_ASSESSMENT_CLUSTERS, ARCHIVE_MARKET_ADMISSION_POLICY
    from ml.stock_trader.contracts import canonical_sha256
    from ml.stock_trader.scope_qualification import qualify_pooled_scope_coverage, POOLED_SCOPE_QUALIFICATION_VERSION
    pin = native['enrichment_gameplan']
    game = (ROOT/pin['run_path']).resolve()
    require(sha(game/'receipt.json') == pin['receipt_sha256'], 'Pinned source receipt mismatch')
    source_receipt, source_manifest = read(game/'receipt.json'), read(game/'manifest.json')
    require(source_receipt['manifest_checksum_sha256'] == sha(game/'manifest.json')
            and source_receipt['action_date'] == '2026-09-28', 'Source manifest or action date mismatch')
    log = NATIVE/stages[0]['log_path']
    emitted = read(log)
    run = Path(emitted['run_path']).resolve()
    require(run.parent == (ROOT/'ml/stock-trader-model-runs').resolve()
            and emitted['status'] == 'PUBLISHED' and emitted['orders_placed'] == 0, 'Native sizing log identity differs')
    receipt, manifest, report, model = (read(run/name) for name in ('receipt.json', 'manifest.json', 'training-report.json', 'model.json'))
    hashes = {}
    for name, field in (('manifest.json', 'manifest_sha256'), ('training-report.json', 'training_report_sha256'), ('model.json', 'model_sha256')):
        hashes[name] = sha(run/name)
        require(hashes[name] == receipt[field], 'Sizing receipt hash mismatch: '+name)
        if name != 'manifest.json':
            require(manifest['output_files'][name] == {'checksum_sha256': hashes[name], 'size': (run/name).stat().st_size}, 'Sizing manifest output differs: '+name)
    require(report['source_gameplan_run'] == model['source_publication']['source_gameplan_run'] == pin['run_path'], 'Sizing source differs from native pin')
    require(report['orders_placed'] == 0 and report['broker_orders_enabled'] is False, 'Sizing order authority differs')
    require(model['scope_qualification_policy'] == POOLED_SCOPE_QUALIFICATION_VERSION
            and model['market_feature_admission_policy'] == ARCHIVE_MARKET_ADMISSION_POLICY, 'Saved qualification/admission policy differs')
    require(model['model_fingerprint'] == report['model_fingerprint'] == receipt['model_fingerprint']
            == canonical_sha256({k:v for k,v in model.items() if k != 'model_fingerprint'}), 'Model fingerprint differs')
    require(model['source_publication']['source_files'] == report['source_files']
            and canonical_sha256(report['source_files']) == model['source_fingerprint'] == report['source_fingerprint'] == receipt['source_fingerprint'], 'Source fingerprint differs')
    inputs = {item['path'].replace('\\', '/'): item for item in manifest['input_files']}
    require(set(inputs) == set(report['source_files']), 'Sizing source inventory differs')
    for relative, digest in report['source_files'].items():
        path = ROOT/relative
        require(path.parent == game and digest == inputs[relative]['checksum_sha256'], 'Unexpected or inconsistent sizing source')
        if path.suffix == '.parquet':
            require(source_manifest['output_files'][path.name] == {'checksum_sha256': digest, 'size': inputs[relative]['size']}, 'Cohort saved manifest binding differs')
        else:
            require(sha(path) == digest, 'Small source file byte hash differs')
    result = {'reviewed_at': datetime.now(timezone.utc).isoformat(), 'status': 'SAVED_SIZING_REPORTS_VERIFY',
        'native_run': str(NATIVE), 'native_status_at_review': native['status'], 'implementation': implementation,
        'run': str(run), 'source_gameplan': str(game), 'pinned_receipt_sha256': pin['receipt_sha256'],
        'bound_output_sha256': hashes, 'native_log_sha256_at_review': sha(log),
        'scope_policy': model['scope_qualification_policy'], 'admission_policy': model['market_feature_admission_policy'],
        'qualified_target_contracts': report['qualified_target_contracts'], 'supported_horizons': report['supported_horizons'],
        'horizons': {}, 'errors': [], 'orders_placed': 0, 'provider_calls': 0,
        'full_native_completion_verified': False, 'cohort_and_inference_reproduction': 'DEFERRED_TO_FINAL_COMPLETION'}
    for group in ('1h', '4h', '1d', '1w'):
        record, payload = report['horizons'][group], model['horizons'][group]
        if record['status'] != 'FITTED':
            result['horizons'][group] = {'fit_status': record['status'], 'reason': record.get('reason'), 'qualified_scopes': record.get('qualified_scope_count', 0)}
            continue
        scopes = record['scope_readiness']
        for field in ('calibration_selection', 'head_development_selection', 'head_training_objectives', 'market_feature_admission', 'partition_evidence'):
            require(record[field] == payload[field], group+': model/report disagreement '+field)
        scores = next(iter(scopes.values()))['horizon_assessment_scores']
        require(all(s['horizon_assessment_scores'] == scores for s in scopes.values()), group+': pooled horizon score disagreement')
        require(all(math.isfinite(v) for k,v in scores.items() if k != 'probability_range')
                and all(math.isfinite(v) for v in scores['probability_range']), group+': nonfinite scores')
        gates = {'brier_below_baseline': scores['brier'] < scores['base_brier'],
            'log_loss_below_baseline': scores['log_loss'] < scores['base_log_loss'],
            'ece_at_most_0_15': scores['ece'] <= .15,
            'return_mse_below_baseline': scores['return_mse'] < scores['base_return_mse'],
            'adverse_mse_at_most_baseline': scores['adverse_mse'] <= scores['base_adverse_mse'],
            'varying_probability': scores['probability_range'][1]-scores['probability_range'][0] > 1e-8}
        require(all(gates.values()) == _quality_passes(scores), group+': strict quality arithmetic mismatch')
        gates['assessment_clusters_at_least_minimum'] = record['partition_evidence']['assessment']['decision_clusters'] >= MINIMUM_ASSESSMENT_CLUSTERS
        reproduced = qualify_pooled_scope_coverage(global_ready=all(gates.values()), fit_scope_decision_clusters=payload['fit_scope_decision_clusters'],
            scope_diagnostics=scopes, target_price_source_contract=payload['target_price_source_contract'])
        require(reproduced == scopes and sum(s['status'] == 'READY' for s in scopes.values()) == record['qualified_scope_count'], group+': scope qualification mismatch')
        admission = record['market_feature_admission']
        excluded = admission['excluded_missing_market_rows']
        require(admission['policy'] == ARCHIVE_MARKET_ADMISSION_POLICY
                and admission['input_execution_rows']-excluded == admission['admitted_rows'] == record['admitted_rows']
                and sum(admission['excluded_by_symbol'].values()) == excluded
                and len(admission['excluded_by_feature']) == 8 and set(admission['excluded_by_feature'].values()) == {excluded}, group+': admission count mismatch')
        selection = record['head_development_selection']
        require(selection['assessment_used_for_selection'] is False and selection['candidate_penalties'] == [1.0, 5.0, 20.0], group+': development-only penalty selection differs')
        for head, objective in selection['head_objectives'].items():
            selected = min(selection['candidate_penalties'], key=lambda penalty: selection['candidate_metrics'][str(penalty)][objective])
            require(selected == selection['selected_penalties'][head] == payload['head_ridge_penalties'][head], group+': penalty not development minimum')
        calibration = record['calibration_selection']
        best_calibrator = min(('identity', 'platt'), key=lambda family: calibration['candidate_metrics'][family]['log_loss'])
        require(calibration['assessment_used_for_selection'] is False and best_calibrator == calibration['selected_family']
                and datetime.fromisoformat(calibration['fit_last_target_end']) < datetime.fromisoformat(calibration['validation_first_decision']), group+': calibration selection/purge mismatch')
        require(all(v['converged'] is True and math.isfinite(v['training_objective']) for v in record['head_training_objectives'].values()), group+': iterative head convergence failed')
        partitions = record['partition_evidence']
        for left, right in zip(('train', 'selection', 'calibration'), ('selection', 'calibration', 'assessment')):
            require(datetime.fromisoformat(partitions[left]['last_target_end']) < datetime.fromisoformat(partitions[right]['first_decision']), group+': chronological partition overlap')
        result['horizons'][group] = {'fit_status': record['status'], 'fitted_scopes': record['fitted_scope_count'],
            'diagnostic_scopes': record['diagnostic_scope_count'], 'qualified_scopes': record['qualified_scope_count'],
            'assessment_scores': scores, 'quality_checks': gates, 'failed_quality_checks': [key for key,passed in gates.items() if not passed],
            'scope_reasons': dict(Counter(s['reason'] for s in scopes.values())),
            'market_feature_admission': admission, 'partitions': partitions,
            'development_selection': selection, 'calibration_selection': calibration,
            'iterative_optimizers_converged': True,
            'thin_scope_evidence': {name:s for name,s in scopes.items() if s['reason'] != 'HELD_OUT_HORIZON_QUALITY_NOT_PROMOTED'}}
    result['conclusion'] = 'Saved strict quality, development selection, chronological partitions, admission arithmetic and scope-status evidence agree. No concrete fitting/data defect is demonstrated by this bounded review; inference and exact cohort admission reconstruction remain pending.'
    (OUT/'preliminary-sizing-review.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    lines = ['# September 28 preliminary sizing review', '', result['conclusion'], '',
        '| Horizon | Fitted / diagnostic / qualified scopes | Admitted / excluded optional rows | Failed quality checks |',
        '|---|---:|---:|---|']
    for group,detail in result['horizons'].items():
        admission = detail.get('market_feature_admission', {})
        lines.append(f"| {group} | {detail.get('fitted_scopes', 0)} / {detail.get('diagnostic_scopes', 0)} / {detail['qualified_scopes']} | {admission.get('admitted_rows')} / {admission.get('excluded_missing_market_rows')} | {', '.join(detail.get('failed_quality_checks', [])) or detail.get('reason', 'None')} |")
    lines += ['', 'Qualification is separate from model fitting and directional promotion. The strict sizing gates and all saved unavailable/research statuses are preserved.', '',
        'Only small bound reports/model JSON and source receipt/manifest were read. No cohort, archive, estimator inference, fit, account call, live action or production mutation was performed.']
    (OUT/'preliminary-sizing-review.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(json.dumps({'status': result['status'], 'run': str(run), 'horizons': {group:{key:value for key,value in detail.items() if key in ('fit_status','qualified_scopes','failed_quality_checks')} for group,detail in result['horizons'].items()}}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
