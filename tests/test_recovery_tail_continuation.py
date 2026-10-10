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
    # This fixture exercises Atlas's saved account-planning tail. Scout's
    # research-only native reports explicitly freeze a different producer role.
    setup['actor'] = 'Atlas'
    profile_path = Path(setup['local_profile'])
    profile = json.loads(profile_path.read_text())
    profile['actor'] = 'Atlas'
    profile_path.write_text(json.dumps(profile))
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


def scheduled_continuation(recovery, split):
    from ml import nightly_dispatch as dispatch
    from test_nightly_workflow import _write_native
    config, state_path, failed, exception, payload = recovery
    root = Path(config['datastore'])
    state = json.loads(state_path.read_text())
    config['automatic_recovery'] = {'enabled':True, 'authorization':'Standing fixture authority', 'max_attempts':3}
    record = {'schema_version':dispatch.RECOVERY, 'actor':state['actor'], 'workflow_run_id':state['run_id'],
        'source_identity':state['source_identity'], 'datastore':str(root), 'action_date':state['action_date'],
        'source_session':state['source_session'], 'original_deadline_at':state['deadline_at'],
        'approved_at':'2026-10-07T12:00Z', 'expires_at':state['recovery_deadline_at'],
        'authorization':'Standing fixture authority', 'orders_authorized':False, 'native_attempts':{}}
    recovery_path = root/'scheduled-recovery.json'
    recovery_path.write_text(json.dumps(record))
    evidence = {'path':str(recovery_path), 'sha256':file_checksum(recovery_path)}
    state['scheduled_recovery'] = evidence
    report = json.loads((failed/'stage-report.json').read_text())
    proposal = root/'reviewed-proposal.json'
    proposal.write_text('{}')
    state['steps']['model_review']['output']['proposal'] = str(proposal)
    report['enrichment_gameplan']['action_date'] = state['action_date']
    report.update(deadline_at=state['deadline_at'], effective_deadline_at=state['recovery_deadline_at'],
        workflow_recovery=evidence, stats_first=True, review_action_date=state['source_session'],
        stage_order=['gameplan_trade_planning'],
        archive_history=True, probability_target_contract='raw-price-direction-v1',
        model_feedback={'path':str(proposal), 'sha256':file_checksum(proposal)})
    (failed/'stage-report.json').write_text(json.dumps(report))
    receipt = json.loads((failed/'receipt.json').read_text())
    receipt['stage_report_checksum_sha256'] = file_checksum(failed/'stage-report.json')
    (failed/'receipt.json').write_text(json.dumps(receipt))
    payload['failed_native_receipt_sha256'] = file_checksum(failed/'receipt.json')
    source = root/report['enrichment_gameplan']['run_path']
    (source/'manifest.json').write_text(json.dumps({'configuration':{'publication_mode':'LATE_RECOVERY'}}))
    if split:
        state['workflow_layout'] = dispatch.LAYOUT
        state['steps']['datastore_catchup'] = {'status':'COMPLETE', 'output':{'files':{}}}
        state['steps']['local_gameplan'] = state['steps']['train_and_plan']
        training = root/'ml/overnight-runs/completed-predictions'
        pin = {**report['enrichment_gameplan'], 'action_date':state['action_date']}
        _write_native(training, pinned=pin)
        state['steps']['train_and_plan'] = {'status':'COMPLETE', 'output':workflow._native_outputs(training)}
        state['current_step'] = 'local_gameplan'
    exception.write_text(json.dumps(payload))
    state['planning_tail_continuation'] = {'path':str(exception), 'sha256':file_checksum(exception)}
    workflow._write(state_path, state)
    return state


@pytest.mark.parametrize('scheduled,split', [(False,False),(True,False),(True,True)])
def test_automatic_dispatch_and_catchup_use_existing_frozen_continuation(recovery, scheduled, split):
    config, state_path, failed, exception, payload = recovery
    config['automatic_recovery'] = {'enabled':True, 'authorization':'Standing fixture authority', 'max_attempts':3}
    if scheduled:
        state = scheduled_continuation(recovery, split)
    else:
        state = json.loads(state_path.read_text())
        state['planning_tail_continuation'] = {'path':str(exception), 'sha256':file_checksum(exception)}
        workflow._write(state_path, state)
    original = state_path.read_bytes()
    retained = {p:p.read_bytes() for p in (exception, failed/'receipt.json', failed/'stage-report.json')}
    decision = workflow.dispatch_status(config, now='2026-10-07T19:20Z')
    assert decision['dispatch'] and decision['responsibility'] == ('gameplan' if split else 'model')
    assert state_path.read_bytes() == original
    calls = []
    result = workflow.run_workflow(config, catch_up=True, responsibility=decision['responsibility'],
        now='2026-10-07T19:20Z', identity=_identity, supervise=False,
        execute_step=lambda c,s,step,save: calls.append(step) or {'files':{}})
    assert calls == ['local_gameplan' if split else 'train_and_plan']
    assert result['status'] == 'WAITING_PREREQUISITE'
    for key in ('deadline_at','recovery_deadline_at','planning_tail_continuation','scheduled_recovery'):
        assert result.get(key) == state.get(key)
    assert all(p.read_bytes() == value for p,value in retained.items())


def test_native_scheduled_continuation_preserves_both_cutoffs_and_split_prediction_pin(recovery, monkeypatch):
    config, state_path, failed, exception, payload = recovery
    state = scheduled_continuation(recovery, True)
    original = {p:p.read_bytes() for p in (exception, failed/'receipt.json', failed/'stage-report.json')}
    pin = json.loads((failed/'stage-report.json').read_text())['enrichment_gameplan']
    monkeypatch.setattr(native, '_pin_stock_gameplan', lambda *args,**kwargs: kwargs['pinned'])
    calls = []
    def stage(command, **kwargs):
        assert kwargs['deadline'] == pd.Timestamp(payload['expires_at'])
        assert command[command.index('--deadline')+1] == '2026-10-07T19:00:00+00:00'
        kwargs['log_path'].write_text('offline unchanged tail')
        calls.append(command[3])
        return 0
    monkeypatch.setattr(native, '_run_stage', stage)
    output = workflow._run_native(config, state, 'local_gameplan', lambda:None)
    report = json.loads((Path(output['native_run'])/'stage-report.json').read_text())
    assert report['deadline_at'] == '2026-10-07T11:00:00+00:00'
    assert report['effective_deadline_at'] == '2026-10-07T21:00:00+00:00'
    assert report['workflow_recovery'] == state['scheduled_recovery']
    assert calls == ['ml.gameplan_trade_planning']
    assert all(p.read_bytes() == value for p,value in original.items())


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


def interrupted_continuation(recovery, *, completed=False, receipt_present=False):
    """Persist the two independent save boundaries of a resumed native tail."""
    config, state_path, failed, exception, _ = recovery
    root = Path(config['datastore'])
    state = json.loads(state_path.read_text())
    workflow._planning_tail_continuation(config, state, exception, pd.Timestamp('2026-10-07T19:20Z'))
    current = root/'ml/overnight-runs/interrupted'; current.mkdir()
    proposal = root/'reviewed-proposal.json'; proposal.write_text('{}')
    state['steps']['model_review']['output'].update(proposal=str(proposal), files={str(proposal):file_checksum(proposal)})
    state['status'] = 'RUNNING'
    state['steps']['train_and_plan'].update(status='RUNNING', native_run=str(current))
    report = json.loads((failed/'stage-report.json').read_text())
    report.update(status='COMPLETE' if receipt_present else 'RUNNING', failed_stage=None,
        owner_pid=99999999, owner_created_at=42.0, child_pid=None,
        stage_order=['gameplan_trade_planning'],
        stages=[{'stage':'gameplan_trade_planning', 'status':'COMPLETE'}] if completed else [],
        probability_target_contract='raw-price-direction-v1', archive_history=True,
        stats_first=True, review_action_date=state['source_session'],
        model_feedback={'path':str(proposal.resolve()), 'sha256':file_checksum(proposal)},
        effective_deadline_at='2026-10-07T21:00:00+00:00',
        deadline_exception={'path':str(exception.resolve()), 'sha256':file_checksum(exception)})
    (current/'stage-report.json').write_text(json.dumps(report))
    if receipt_present:
        (current/'receipt.json').write_text(json.dumps(dict(schema_version=native.OVERNIGHT_RUNTIME_VERSION,
            run_path=current.relative_to(root).as_posix(), status='COMPLETE', orders_placed=0,
            broker_orders_enabled=False, stage_report_checksum_sha256=file_checksum(current/'stage-report.json'))))
    state_path.write_text(json.dumps(state))
    return config, state_path, current, state


@pytest.mark.parametrize('completed,receipt_present', [(False,False), (True,False), (True,True)])
def test_saved_continuation_recovers_native_save_boundaries_without_duplicate_work(recovery, monkeypatch, completed, receipt_present):
    config, state_path, current, before = interrupted_continuation(
        recovery, completed=completed, receipt_present=receipt_present)
    _, _, origin, exception, _ = recovery
    original = {p.name:p.read_bytes() for p in origin.iterdir()}
    authorization = exception.read_bytes()
    pin = json.loads((origin/'stage-report.json').read_text())['enrichment_gameplan']
    monkeypatch.setattr(native, '_process_created_at', lambda pid: None)
    monkeypatch.setattr(native, '_pin_stock_gameplan', lambda *a, **k: pin)
    stages = []
    def complete(command, **kwargs):
        assert command[3] == 'ml.gameplan_trade_planning'
        assert kwargs['deadline'] == pd.Timestamp('2026-10-07T21:00Z')
        kwargs['log_path'].write_text('offline interrupted tail completed')
        stages.append(command[3]); return 0
    monkeypatch.setattr(native, '_run_stage', complete)
    calls = []
    def execute(c, s, step, save):
        calls.append(step)
        return workflow._run_native(c, s, step, save) if step == 'train_and_plan' else {'files':{}}
    result = workflow.run_workflow(config, resume_action_date='2026-10-07',
        now='2026-10-07T19:20Z', identity=_identity, supervise=False, execute_step=execute)
    assert result['status'] == 'LOCAL_COMPLETE_PEER_SETUP_PENDING'
    assert stages == ([] if completed else ['ml.gameplan_trade_planning'])
    assert calls == ['train_and_plan','verify_display','local_handoff']
    for key in ('run_id','deadline_at','recovery_deadline_at','planning_tail_continuation'):
        assert result[key] == before[key]
    for step in ('prepare_stats','model_review'):
        assert result['steps'][step] == before['steps'][step]
    assert {p.name:p.read_bytes() for p in origin.iterdir()} == original
    assert exception.read_bytes() == authorization
    receipt = json.loads((current/'receipt.json').read_text())
    assert receipt['status'] == ('COMPLETE' if completed else 'CANCELLED')
    assert len(list((Path(config['datastore'])/'ml/overnight-runs').iterdir())) == (2 if completed else 3)
    workflow.run_workflow(config, resume_action_date='2026-10-07', now='2026-10-07T19:20Z',
        identity=_identity, supervise=False, execute_step=lambda *a: pytest.fail('Completed tail dispatched again'))


def test_saved_continuation_cannot_recover_a_live_native_owner(recovery, monkeypatch):
    config, _, current, _ = interrupted_continuation(recovery)
    original = (current/'stage-report.json').read_bytes()
    monkeypatch.setattr(native, '_process_created_at', lambda pid: 42.0 if pid == 99999999 else None)
    monkeypatch.setattr(native, '_run_stage', lambda *a, **k: pytest.fail('Live native owner duplicated'))
    with pytest.raises(RuntimeError, match='owner is still alive'):
        workflow.run_workflow(config, resume_action_date='2026-10-07', now='2026-10-07T19:20Z',
            identity=_identity, supervise=False, execute_step=workflow._run_native)
    assert (current/'stage-report.json').read_bytes() == original
    assert not (current/'receipt.json').exists()


def test_saved_continuation_still_rejects_changed_failed_retry_evidence(recovery, monkeypatch):
    config, _, current, _ = interrupted_continuation(recovery)
    monkeypatch.setattr(native, '_process_created_at', lambda pid: None)
    native.recover_interrupted_run(Path(config['datastore']), current, 'Offline fixture owner exited')
    (current/'stage-report.json').write_text('{}')
    monkeypatch.setattr(native, '_run_stage', lambda *a, **k: pytest.fail('Changed native evidence dispatched'))
    with pytest.raises(RuntimeError, match='Only a verified failed or stopped'):
        workflow.run_workflow(config, resume_action_date='2026-10-07', now='2026-10-07T19:20Z',
            identity=_identity, supervise=False, execute_step=workflow._run_native)


def test_saved_continuation_preserves_native_run_directory_boundary(recovery):
    config, state_path, _, state = interrupted_continuation(recovery)
    state['steps']['train_and_plan']['native_run'] = str(Path(config['datastore'])/'other-run')
    state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match='escapes its expected directory'):
        workflow.run_workflow(config, resume_action_date='2026-10-07', now='2026-10-07T19:20Z',
            identity=_identity, supervise=False,
            execute_step=lambda *a: pytest.fail('Native directory boundary bypassed'))


def record_source_repair(root, state, target):
    number = len(state.get('source_repairs', []))
    snapshot = root/f'failed-state-{number}.json'
    snapshot.write_text(json.dumps(state))
    audit_path = root/f'repair-{number}.json'
    audit = dict(at='2026-10-07T19:21:00Z', authorization='Offline reviewed source repair',
        completion_record=f'offline-repair-{number}', failed_state=str(snapshot),
        failed_state_sha256=file_checksum(snapshot), original_source_identity=state['source_identity'],
        reviewed_source_identity=target)
    audit_path.write_text(json.dumps(audit))
    edge = {key:audit[key] for key in ('at','original_source_identity','reviewed_source_identity')}
    edge.update(evidence=str(audit_path), evidence_sha256=file_checksum(audit_path))
    state.setdefault('source_repairs', []).append(edge)
    state['source_identity'] = target
    return audit_path, snapshot


def completed_tail_before_display_repair(recovery):
    config, state_path, current, state = interrupted_continuation(recovery, completed=True, receipt_present=True)
    state['status'] = 'FAILED'
    state['current_step'] = 'verify_display'
    state['steps']['train_and_plan'].update(status='COMPLETE', output=workflow._native_outputs(current))
    state['steps']['verify_display'] = {'status':'FAILED'}
    return config, state_path, current, state


@pytest.mark.parametrize('repair_count', [1, 2])
def test_reviewed_source_repairs_reuse_completed_native_tail_and_frozen_continuation(recovery, repair_count):
    config, state_path, _, state = completed_tail_before_display_repair(recovery)
    _, _, origin, exception, _ = recovery
    before = json.loads(json.dumps(state))
    original = {p.name:p.read_bytes() for p in origin.iterdir()}
    authorization = exception.read_bytes()
    for number in range(repair_count):
        record_source_repair(Path(config['datastore']), state,
            {'commit':str(number+1)*40, 'source_sha256':str(number+2)*64})
    # Equivalent zoned representations preserve the same frozen deadlines.
    state['deadline_at'] = '2026-10-07T04:00:00-07:00'
    state['recovery_deadline_at'] = '2026-10-07T12:00:00-07:00'
    before['deadline_at'], before['recovery_deadline_at'] = state['deadline_at'], state['recovery_deadline_at']
    state_path.write_text(json.dumps(state))
    calls = []
    result = workflow.run_workflow(config, resume_action_date='2026-10-07', now='2026-10-07T19:25Z',
        identity=lambda _:state['source_identity'], supervise=False,
        execute_step=lambda c,s,step,save: calls.append(step) or {'files':{}})
    assert result['status'] == 'LOCAL_COMPLETE_PEER_SETUP_PENDING'
    assert calls == ['verify_display','local_handoff']
    for key in ('run_id','deadline_at','recovery_deadline_at','planning_tail_continuation'):
        assert result[key] == before[key]
    for step in ('prepare_stats','model_review','train_and_plan'):
        assert result['steps'][step] == before['steps'][step]
    assert exception.read_bytes() == authorization
    assert {p.name:p.read_bytes() for p in origin.iterdir()} == original


@pytest.mark.parametrize('damage', ['unaudited','audit-hash','snapshot-hash','audit-identity',
    'authorization','run','deadline','continuation','prefix','disconnected','checkout'])
def test_continuation_rejects_unaudited_or_tampered_source_transitions(recovery, damage):
    config, state_path, _, state = completed_tail_before_display_repair(recovery)
    target = {'commit':'c'*40, 'source_sha256':'d'*64}
    audit_path, snapshot = record_source_repair(Path(config['datastore']), state, target)
    audit = json.loads(audit_path.read_text())
    previous = json.loads(snapshot.read_text())
    if damage == 'unaudited': state['source_repairs'] = []
    elif damage == 'audit-hash': audit_path.write_text('{}')
    elif damage == 'snapshot-hash': snapshot.write_text('{}')
    else:
        if damage == 'audit-identity': audit['reviewed_source_identity'] = {}
        if damage == 'authorization': audit['authorization'] = ''
        if damage == 'run': previous['run_id'] = 'different-recovery'
        if damage == 'deadline': previous['recovery_deadline_at'] = '2026-10-07T22:00Z'
        if damage == 'continuation': previous['planning_tail_continuation']['sha256'] = '0'*64
        if damage == 'prefix': previous['source_repairs'] = [{'unreviewed':'earlier'}]
        if damage == 'disconnected': state['source_identity'] = {'commit':'e'*40, 'source_sha256':'f'*64}
        snapshot.write_text(json.dumps(previous))
        audit['failed_state_sha256'] = file_checksum(snapshot)
        audit_path.write_text(json.dumps(audit))
        state['source_repairs'][-1]['evidence_sha256'] = file_checksum(audit_path)
    state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError):
        workflow.run_workflow(config, resume_action_date='2026-10-07', now='2026-10-07T19:25Z',
            identity=_identity if damage == 'checkout' else lambda _:state['source_identity'], supervise=False,
            execute_step=lambda *a: pytest.fail('Unaudited source dispatched a stage'))
