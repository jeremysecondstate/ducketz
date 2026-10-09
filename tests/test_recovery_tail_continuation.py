"""Late continuation reuses verified numerical work and retains missed deadlines."""
import json
from pathlib import Path

import pandas as pd
import pytest

from ml import nightly_workflow as workflow, overnight_runtime as native
from ml.artifacts import file_checksum
from ml.preparation_deadline import RECOVERY_VERSION
from tests.test_nightly_workflow import setup, _identity


@pytest.fixture
def recovery(setup, monkeypatch):
    root = Path(setup['datastore'])
    source = root/'ml/nightly-gameplan-runs/source'
    source.mkdir(parents=True)
    (source/'receipt.json').write_text('{"action_date":"2026-10-07"}')
    pin = dict(run_path=source.relative_to(root).as_posix(),
               receipt_sha256=file_checksum(source/'receipt.json'), action_date='2026-10-07')
    failed = root/'ml/overnight-runs/failed'; failed.mkdir(parents=True)
    report = dict(status='FAILED', failed_stage='gameplan_trade_planning',
        deadline_at='2026-10-07T19:00:00Z', late_action_date='2026-10-07',
        stage_order=['gameplan_trade_planning','gameplan_actuals_review'],
        preparation_scope='STOCK_ONLY', stock_only=True, independent_stock_horizons=True,
        stock_price_source='xnas-itch-archive-v1', enrichment_gameplan=pin, stages=[],
        completed_stages_from_previous_attempt=['gameplan_publication','stock_enrichment_training'])
    (failed/'stage-report.json').write_text(json.dumps(report))
    (failed/'receipt.json').write_text(json.dumps(dict(schema_version=native.OVERNIGHT_RUNTIME_VERSION,
        run_path=failed.relative_to(root).as_posix(), status='FAILED', orders_placed=0,
        broker_orders_enabled=False, stage_report_checksum_sha256=file_checksum(failed/'stage-report.json'))))
    def fail(config, state, step, save):
        if step == 'train_and_plan':
            state['steps'][step]['native_run'] = str(failed); save()
            raise RuntimeError('fixture account capture failure')
        return {'files': {}}
    with pytest.raises(RuntimeError, match='fixture'):
        workflow.run_workflow(setup, recover_action_date='2026-10-07', recovery_deadline='2026-10-07T19:00Z',
            now='2026-10-07T12:30Z', identity=_identity, supervise=False, execute_step=fail)
    state_path = Path(setup['state_root'])/'runs/2026-10-07/state.json'
    state = json.loads(state_path.read_text())
    payload = dict(schema_version=RECOVERY_VERSION, scope='PINNED_STOCK_PLANNING_AND_ACTUALS_ONLY',
        operator_authorized=True, orders_authorized=False, authorization_text='Complete the existing fallback tail.',
        authorization_source='offline test', action_date='2026-10-07', gameplan_run=pin['run_path'],
        gameplan_receipt_sha256=pin['receipt_sha256'], original_session_deadline_at=state['deadline_at'],
        original_deadline_at=state['recovery_deadline_at'], approved_at='2026-10-07T19:10Z', expires_at='2026-10-07T21:00Z',
        workflow_run_id=state['run_id'], workflow_source_identity=state['source_identity'],
        failed_native_run=failed.relative_to(root).as_posix(), failed_native_receipt_sha256=file_checksum(failed/'receipt.json'))
    exception = root/'continuation.json'; exception.write_text(json.dumps(payload))
    monkeypatch.setattr(native, 'utc_timestamp', lambda value=None: pd.Timestamp(value or '2026-10-07T19:20Z'))
    return setup, state_path, failed, exception, payload


def resume(config, path, callback):
    return workflow.run_workflow(config, resume_action_date='2026-10-07', planning_tail_exception=path,
        now='2026-10-07T19:20Z', identity=_identity, supervise=False, execute_step=callback)


def test_existing_tail_completes_without_retraining_or_replacing_deadlines(recovery):
    config, state_path, failed, exception, _ = recovery
    before = json.loads(state_path.read_text())
    originals = {p.name:p.read_bytes() for p in failed.iterdir()}
    calls = []
    result = resume(config, exception, lambda c, s, step, save: calls.append(step) or {'files': {}})
    assert calls == ['train_and_plan', 'verify_display', 'local_handoff']
    assert result['status'] == 'LOCAL_COMPLETE_PEER_SETUP_PENDING'
    for field in ('run_id', 'deadline_at', 'recovery_deadline_at'):
        assert result[field] == before[field]
    assert result['steps']['prepare_stats'] == before['steps']['prepare_stats']
    assert result['steps']['model_review'] == before['steps']['model_review']
    assert {p.name:p.read_bytes() for p in failed.iterdir()} == originals
    resume(config, exception, lambda *a: pytest.fail('Completed run must not replay'))


@pytest.mark.parametrize('damage', ['run','source','receipt','deadline','session','expired','orders','early-stage','log'])
def test_invalid_continuation_cannot_dispatch_a_stage(recovery, damage):
    config, state_path, failed, exception, payload = recovery
    if damage == 'run': payload['workflow_run_id'] = 'other'
    if damage == 'source': payload['workflow_source_identity'] = {}
    if damage == 'receipt': payload['failed_native_receipt_sha256'] = '0'*64
    if damage == 'deadline': payload['original_deadline_at'] = '2026-10-07T19:01Z'
    if damage == 'session': payload['original_session_deadline_at'] = '2026-10-07T12:00Z'
    if damage == 'expired': payload['expires_at'] = '2026-10-07T19:15Z'
    if damage == 'orders': payload['orders_authorized'] = True
    if damage == 'early-stage':
        state = json.loads(state_path.read_text()); state['steps']['model_review']['status'] = 'FAILED'
        state_path.write_text(json.dumps(state))
    if damage == 'log': (failed/'stage-report.json').write_text('{}')
    exception.write_text(json.dumps(payload))
    with pytest.raises((ValueError, RuntimeError)):
        resume(config, exception, lambda *a: pytest.fail('Invalid continuation dispatched'))


def test_retry_cannot_replace_frozen_continuation(recovery):
    config, _, _, exception, payload = recovery
    with pytest.raises(RuntimeError, match='tail failure'):
        resume(config, exception, lambda *a: (_ for _ in ()).throw(RuntimeError('tail failure')))
    payload['expires_at'] = '2026-10-07T22:00Z'; exception.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='cannot be replaced'):
        resume(config, exception, lambda *a: pytest.fail('Replaced continuation dispatched'))


def test_native_continuation_dispatches_only_pinned_tail_with_effective_cutoff(recovery, monkeypatch):
    config, _, failed, exception, _ = recovery
    root = Path(config['datastore'])
    original = {p.name:p.read_bytes() for p in failed.iterdir()}
    pin = json.loads((failed/'stage-report.json').read_text())['enrichment_gameplan']
    monkeypatch.setattr(native, '_pin_stock_gameplan', lambda *a, **k: pin)
    calls = []
    def complete(command, **kwargs):
        assert kwargs['deadline'] == pd.Timestamp('2026-10-07T21:00Z')
        assert command[command.index('--deadline')+1] == '2026-10-07T19:00:00+00:00'
        assert command[-2:] == ('--deadline-exception', str(exception.resolve()))
        if command[3] == 'ml.gameplan_trade_planning':
            assert command[command.index('--late-action-date')+1] == '2026-10-07'
        kwargs['log_path'].write_text('offline tail passed')
        calls.append(command[3]); return 0
    monkeypatch.setattr(native, '_run_stage', complete)
    run = native.run_overnight_pipeline(root, datastore_argument=('--datastore',str(root)),
        repository_root=Path(config['repository']), reporter=None, resume_run=failed, deadline_exception=exception)
    report = json.loads((run/'stage-report.json').read_text())
    assert report['status'] == 'COMPLETE'
    assert report['deadline_at'] == '2026-10-07T19:00:00+00:00'
    assert report['effective_deadline_at'] == '2026-10-07T21:00:00+00:00'
    assert calls == ['ml.gameplan_trade_planning', 'ml.gameplan_actuals_review']
    assert {p.name:p.read_bytes() for p in failed.iterdir()} == original
