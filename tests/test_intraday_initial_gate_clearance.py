"""Exact pretransport failure recovery; no broker/network or history rewrite."""
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

from ml.intraday import initial_gate_clearance as clearance
from ml.intraday import scout_local_execution as execution
from ml.intraday import scout_journal
from ml.intraday.prediction import ContractError, instant
from ml.intraday.scout_client import ScoutClient
from ml.intraday.scout_broker import ScoutBrokerError
from ml.intraday.scout_journal import JournalStateError
from ml.intraday.scout_protocol import Grant, Request, canonical_sha256
from test_intraday_scout_local_execution import env, forecast, record


def native_transport(env, monkeypatch):
    """Run the real native POST boundary with a synthetic context/HTTP stub."""
    from types import SimpleNamespace
    from app.services import schwab
    old = env.broker
    broker = object.__new__(schwab.SchwabSession)
    for name in ('stable_account_fingerprint', 'verify_read_snapshot', 'get_orders'):
        setattr(broker, name, getattr(old, name))
    broker.prepare_order_submission = lambda: schwab.SchwabOrderSubmissionContext(
        account_hash='synthetic-account', access_token='offline-placeholder',
        identity_fingerprint=old.generation)
    broker.posts = old.posts
    def post(*args, **kwargs):
        old.posts.append(kwargs['json'])
        if old.post_error:
            raise old.post_error
        return SimpleNamespace(raise_for_status=lambda: None, headers={'Location': old.location})
    monkeypatch.setattr(schwab.requests, 'post', post)
    env.executor.broker = env.broker = broker
    return old


@pytest.mark.parametrize('gate_number', [4, 5], ids=['native-authorization', 'native-final'])
def test_native_before_post_expiry_has_exact_proof_and_never_replays(env, monkeypatch, gate_number):
    native_transport(env, monkeypatch)
    calls = expire_at(env, monkeypatch, gate_number)
    result = env.executor.step(env.forecast)
    old = record(env)
    assert len(calls) == gate_number and env.broker.posts == []
    assert old['state'] == 'UNKNOWN' and old['broker_order_id'] is None
    assert result['evidence']['pretransport_clearance'] == load(env)
    before = clearance._audit(env.executor.books, env.executor.journal, old)[1]
    env.executor.recover()
    env.executor.step(env.forecast)
    assert record(env) == old and clearance._audit(env.executor.books, env.executor.journal, old)[1] == before
    env.cash = 105
    env.forecast = forecast(env, advance=True)
    current = env.executor.step(env.forecast)
    assert current['disposition'] == 'submitted' and current['adjusted_quantity'] == 1
    assert len(env.broker.posts) == 1 and env.executor.journal.get(old['request']['request_id']) == old


@pytest.mark.parametrize('failure', ['transport', 'receipt', 'overridden_sender', 'mutated_class_sender'])
def test_native_proof_excludes_post_response_and_untrusted_senders(env, monkeypatch, failure):
    from types import MethodType
    from app.services.schwab import SchwabSession
    old = native_transport(env, monkeypatch)
    if failure == 'transport':
        old.post_error = TimeoutError('offline uncertain POST')
    elif failure == 'receipt':
        old.location = 'missing-exact-receipt'
    else:
        native = SchwabSession.submit_prepared_order
        def opaque(self, payload, context, *, before_post):
            # An arbitrary wrapper can produce an effect before invoking its
            # callback. Its later gate failure is not native boundary proof.
            self.posts.append('opaque-side-effect')
            return native(self, payload, context, before_post=before_post)
        if failure == 'overridden_sender':
            env.broker.submit_prepared_order = MethodType(opaque, env.broker)
        else:
            monkeypatch.setattr(SchwabSession, 'submit_prepared_order', opaque)
        expire_at(env, monkeypatch, 7)
    result = env.executor.step(env.forecast)
    old_record = record(env)
    assert old_record['state'] == 'UNKNOWN' and len(env.broker.posts) == 1
    assert load(env) is None and 'pretransport_clearance' not in result['evidence']
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
    assert len(env.broker.posts) == 1 and env.executor.journal.get(old_record['request']['request_id']) == old_record


def test_native_observation_same_error_and_failed_reuse_cannot_mint_another_proof(env, monkeypatch):
    native_transport(env, monkeypatch)
    adapters = []
    factory = env.executor._adapter
    def capture(*args, **kwargs):
        value = factory(*args, **kwargs)
        adapters.append(value)
        return value
    monkeypatch.setattr(env.executor, '_adapter', capture)
    expire_at(env, monkeypatch, 5)
    env.executor.step(env.forecast)
    old, adapter = record(env), adapters[-1]
    observation = adapter.native_before_post_failure
    assert observation is not None and adapter.initial_gate_failure is None
    with pytest.raises(ContractError, match='retained adapter'):
        clearance.create_clearance(env.executor.books, env.executor.journal, old,
            adapter=adapter, error=ContractError('different'), as_of=env.state.now)
    with pytest.raises(ScoutBrokerError):
        adapter.send(Request.from_payload(old['request']), Grant.from_payload(old['grant']),
            expected_snapshot_identity='unused', authorization_gate=lambda *_: True,
            final_gate=lambda *_: True)
    assert adapter.native_before_post_failure is None
    with pytest.raises(ContractError, match='retained adapter'):
        clearance.create_clearance(env.executor.books, env.executor.journal, old,
            adapter=adapter, error=observation.error, as_of=env.state.now)
    assert record(env) == old and env.broker.posts == []


def test_native_proof_write_failure_keeps_exact_original_exception_and_block(env, monkeypatch):
    native_transport(env, monkeypatch)
    expire_at(env, monkeypatch, 5)
    monkeypatch.setattr(clearance, '_write_once', lambda *_: (_ for _ in ()).throw(OSError('offline proof failure')))
    result = env.executor.step(env.forecast)
    assert record(env)['state'] == 'UNKNOWN' and env.broker.posts == [] and load(env) is None
    assert result['evidence']['submission_failure']['original']['chain'][0]['type'] == 'ContractError'
    assert result['evidence']['clearance_persistence_failure']['chain'][0]['type'] == 'OSError'
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'


@pytest.mark.parametrize('damage', ['sender', 'stage', 'version', 'request', 'journal'])
def test_native_saved_proof_reaudits_exact_boundary_and_original(env, monkeypatch, damage):
    native_transport(env, monkeypatch)
    expire_at(env, monkeypatch, 5)
    env.executor.step(env.forecast)
    path = clearance._path(env.executor.books, record(env))
    envelope = json.loads(path.read_text())
    payload = envelope['payload']
    if damage == 'sender': payload['observation']['native_sender'] = 'opaque-wrapper'
    elif damage == 'stage': payload['observation']['stage'] = 'adapter_initial_gate'
    elif damage == 'version': payload['version'] = clearance.VERSION
    elif damage == 'request': payload['observation']['request_sha256'] = 'f' * 64
    elif damage == 'journal': payload['journal_events_sha256'] = 'f' * 64
    envelope['sha256'] = canonical_sha256(payload)
    path.write_text(json.dumps(envelope))
    with pytest.raises(ContractError):
        load(env)
    assert env.broker.posts == [] and record(env)['state'] == 'UNKNOWN'


@pytest.mark.parametrize('guard', ['manual_stop', 'target_expiry'])
def test_native_clearance_keeps_fresh_forecast_control_boundaries(env, monkeypatch, guard):
    native_transport(env, monkeypatch)
    expire_at(env, monkeypatch, 5)
    env.executor.step(env.forecast)
    assert load(env) is not None
    old = record(env)
    env.forecast = forecast(env, advance=True)
    if guard == 'manual_stop':
        env.state.stop = True
        with pytest.raises(ContractError):
            env.executor.step(env.forecast)
    else:
        env.state.now = instant(env.forecast['target_close'])
        assert env.executor.step(env.forecast)['disposition'] != 'submitted'
    assert env.broker.posts == [] and env.executor.journal.get(old['request']['request_id']) == old


def test_native_callback_process_interrupt_never_mints_completion_proof(env, monkeypatch):
    native_transport(env, monkeypatch)
    original, calls = execution._saved_forecast, []
    def interrupt(*args, **kwargs):
        calls.append(1)
        if len(calls) == 5:
            raise KeyboardInterrupt('offline interrupted callback')
        return original(*args, **kwargs)
    monkeypatch.setattr(execution, '_saved_forecast', interrupt)
    with pytest.raises(KeyboardInterrupt):
        env.executor.step(env.forecast)
    assert record(env)['state'] == 'SENDING' and load(env) is None and env.broker.posts == []
    monkeypatch.setattr(execution, '_saved_forecast', original)
    env.executor.recover()
    assert record(env)['state'] == 'UNKNOWN' and load(env) is None


@pytest.fixture(autouse=True)
def journal_clock(env, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return env.state.now
    monkeypatch.setattr(scout_journal, 'datetime', Clock)


def expire_at(env, monkeypatch, number):
    saved_read = execution._saved_forecast
    calls = []
    def read(*args, **kwargs):
        calls.append(1)
        if len(calls) == number:
            env.state.now += timedelta(seconds=61)
        return saved_read(*args, **kwargs)
    monkeypatch.setattr(execution, '_saved_forecast', read)
    return calls


def load(env, original=None):
    return clearance.load_clearance(env.executor.books, env.executor.journal,
        original or record(env), as_of=env.state.now)


@pytest.mark.parametrize('gate_number', [4, 5], ids=['authorization', 'final'])
def test_outer_gate_expiry_has_durable_clearance_without_rearming_original(env, monkeypatch, gate_number):
    calls = expire_at(env, monkeypatch, gate_number)
    result = env.executor.step(env.forecast)
    old = record(env)
    assert len(calls) == gate_number and env.broker.posts == []
    assert old['state'] == 'UNKNOWN' and old['broker_order_id'] is None
    assert result['evidence']['submission_failure']['stage'] == 'send'
    assert result['evidence']['pretransport_clearance'] == load(env)
    assert load(env)['commitment'] == 'no_order_transport_entered'
    assert env.executor.journal.order_state(old['request']['request_id'])['evidence'] is None
    before = clearance._audit(env.executor.books, env.executor.journal, old)[1]
    env.executor.recover()
    env.executor.step(env.forecast)
    assert record(env) == old and clearance._audit(env.executor.books, env.executor.journal, old)[1] == before
    client = ScoutClient(env.executor.journal, None, account_fingerprint=old['request']['account_fingerprint'],
        session_id=env.executor.session_id, symbols=frozenset({'DOCU'}), authority_epoch=1,
        clock=lambda: env.state.now)
    with pytest.raises(JournalStateError):
        client.submit(old['request']['request_id'], send=lambda *_: pytest.fail('No replay'), final_gate=lambda *_: True)
    captures = env.captures
    env.cash = 105
    env.forecast = forecast(env, advance=True)
    current = env.executor.step(env.forecast)
    assert current['disposition'] == 'submitted' and current['adjusted_quantity'] == 1
    assert env.captures > captures and len(env.broker.posts) == 1
    assert env.executor.journal.get(old['request']['request_id']) == old
    assert load(env, old)['commitment'] == 'no_order_transport_entered'


@pytest.mark.parametrize('gate_number,state', [(2, 'GRANTED'), (3, 'UNKNOWN'), (6, 'UNKNOWN'), (7, 'UNKNOWN')])
def test_other_gate_failures_do_not_acquire_outer_gate_clearance(env, monkeypatch, gate_number, state):
    expire_at(env, monkeypatch, gate_number)
    entered = []
    env.broker.before_transport = lambda: entered.append(True)
    result = env.executor.step(env.forecast)
    assert record(env)['state'] == state and env.broker.posts == []
    assert 'pretransport_clearance' not in result['evidence'] and load(env) is None
    assert bool(entered) == (gate_number >= 6)
    if state == 'UNKNOWN':
        env.forecast = forecast(env, advance=True)
        assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
        assert env.broker.posts == []


@pytest.mark.parametrize('failure', ['post', 'receipt', 'acceptance_write', 'forged_exception'])
def test_transport_and_receipt_failures_remain_blocking(env, monkeypatch, failure):
    if failure == 'receipt':
        env.broker.location = 'not-an-order-receipt'
    elif failure == 'acceptance_write':
        outcome = env.executor.journal.outcome
        def reject_acceptance(request_id, token, broker_order_id):
            if broker_order_id is not None:
                raise OSError('offline failed acceptance write')
            return outcome(request_id, token, broker_order_id)
        monkeypatch.setattr(env.executor.journal, 'outcome', reject_acceptance)
    else:
        error = TimeoutError('offline lost response')
        if failure == 'forged_exception':
            error.initial_gate_failure = True
            error.scout_no_order_transport_entered = True
        env.broker.post_error = error
    result = env.executor.step(env.forecast)
    old = record(env)
    assert old['state'] == 'UNKNOWN' and len(env.broker.posts) == 1
    assert load(env) is None and 'pretransport_clearance' not in result['evidence']
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
    assert len(env.broker.posts) == 1 and env.executor.journal.get(old['request']['request_id']) == old


def test_uncertainty_persistence_failure_does_not_create_clearance(env, monkeypatch):
    expire_at(env, monkeypatch, 5)
    def fail_outcome(*args, **kwargs):
        raise OSError('offline uncertainty persistence failure')
    monkeypatch.setattr(env.executor.journal, 'outcome', fail_outcome)
    result = env.executor.step(env.forecast)
    assert record(env)['state'] == 'SENDING' and env.broker.posts == []
    assert load(env) is None and 'pretransport_clearance' not in result['evidence']
    assert 'uncertainty_persistence_error' in result['evidence']['submission_failure']


@pytest.mark.parametrize('mutation', ['hash', 'record', 'events', 'request', 'grant', 'token', 'stage', 'future', 'before_unknown'])
def test_tampered_receipt_fails_closed_even_with_recomputed_envelope(env, monkeypatch, mutation):
    expire_at(env, monkeypatch, 5)
    env.executor.step(env.forecast)
    path = clearance._path(env.executor.books, record(env))
    envelope = json.loads(path.read_text())
    payload = envelope['payload']
    if mutation in {'record', 'events'}:
        payload['original_record_sha256' if mutation == 'record' else 'journal_events_sha256'] = 'f' * 64
    elif mutation in {'request', 'grant', 'token'}:
        payload['observation'][{'request': 'request_sha256', 'grant': 'grant_sha256',
                                'token': 'sending_record_sha256'}[mutation]] = 'f' * 64
    elif mutation == 'stage':
        payload['observation']['stage'] = 'inner_before_post'
    elif mutation == 'future':
        payload['created_at'] = (env.state.now + timedelta(seconds=1)).isoformat()
    elif mutation == 'before_unknown':
        payload['created_at'] = (env.state.now - timedelta(seconds=1)).isoformat()
    envelope['sha256'] = 'f' * 64 if mutation == 'hash' else canonical_sha256(payload)
    path.write_text(json.dumps(envelope))
    with pytest.raises(ContractError):
        load(env)
    env.forecast = forecast(env, advance=True)
    if mutation != 'future':
        with pytest.raises(ContractError):
            env.executor.step(env.forecast)
        assert env.broker.posts == []


def test_receipt_cannot_be_applied_to_changed_record(env, monkeypatch):
    expire_at(env, monkeypatch, 5)
    env.executor.step(env.forecast)
    old = record(env)
    changed = deepcopy(old)
    changed['send_token'] = 'f' * 64
    with pytest.raises(ContractError):
        load(env, changed)
    changed = deepcopy(old)
    changed['state'] = 'ACCEPTED'
    changed['broker_order_id'] = '123'
    with pytest.raises(ContractError):
        load(env, changed)
    assert record(env) == old and env.broker.posts == []


@pytest.mark.parametrize('guard', ['manual_stop', 'target_expiry'])
def test_clearance_keeps_new_forecast_manual_and_target_guards(env, monkeypatch, guard):
    expire_at(env, monkeypatch, 5)
    env.executor.step(env.forecast)
    assert load(env) is not None
    env.forecast = forecast(env, advance=True)
    if guard == 'manual_stop':
        env.state.stop = True
        with pytest.raises(ContractError, match='manual Scout session'):
            env.executor.step(env.forecast)
    else:
        env.state.now = instant(env.forecast['target_close'])
        result = env.executor.step(env.forecast)
        assert result['disposition'] != 'submitted'
    assert env.broker.posts == []


def test_failed_receipt_write_preserves_unknown_and_global_block(env, monkeypatch):
    expire_at(env, monkeypatch, 5)
    def fail_write(*args, **kwargs):
        raise OSError('offline receipt write failure')
    monkeypatch.setattr(clearance, '_write_once', fail_write)
    result = env.executor.step(env.forecast)
    assert result['evidence']['submission_failure']['stage'] == 'send'
    assert result['evidence']['submission_failure']['original']['chain'][0]['type'] == 'ContractError'
    assert result['evidence']['clearance_persistence_failure']['chain'][0]['type'] == 'OSError'
    old = record(env)
    assert old['state'] == 'UNKNOWN' and load(env) is None
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
    assert env.broker.posts == [] and env.executor.journal.get(old['request']['request_id']) == old


def test_proof_requires_same_retained_adapter_error_and_clears_stale_observation(env, monkeypatch):
    adapters = []
    original_factory = env.executor._adapter
    def factory(*args, **kwargs):
        adapter = original_factory(*args, **kwargs)
        adapters.append(adapter)
        return adapter
    monkeypatch.setattr(env.executor, '_adapter', factory)
    expire_at(env, monkeypatch, 5)
    env.executor.step(env.forecast)
    old, adapter = record(env), adapters[-1]
    observation = adapter.initial_gate_failure
    assert observation is not None
    with pytest.raises(ContractError, match='retained adapter'):
        clearance.create_clearance(env.executor.books, env.executor.journal, old,
            adapter=adapter, error=ContractError('different error'), as_of=env.state.now)
    assert load(env) is not None
    # A rejected second invocation cannot leave a usable old observation.
    with pytest.raises(ScoutBrokerError, match='durable journal state'):
        adapter.send(Request.from_payload(old['request']), Grant.from_payload(old['grant']),
            expected_snapshot_identity='unused', authorization_gate=lambda *_: True,
            final_gate=lambda *_: True)
    assert adapter.initial_gate_failure is None
    with pytest.raises(ContractError, match='retained adapter'):
        clearance.create_clearance(env.executor.books, env.executor.journal, old,
            adapter=adapter, error=observation.error, as_of=env.state.now)
    assert record(env) == old and env.broker.posts == []


def test_process_exit_at_initial_gate_never_mints_proof(env, monkeypatch):
    saved_read, calls = execution._saved_forecast, []
    def interrupt(*args, **kwargs):
        calls.append(1)
        if len(calls) == 5:
            raise KeyboardInterrupt('offline interrupted outer gate')
        return saved_read(*args, **kwargs)
    monkeypatch.setattr(execution, '_saved_forecast', interrupt)
    with pytest.raises(KeyboardInterrupt):
        env.executor.step(env.forecast)
    assert record(env)['state'] == 'SENDING' and load(env) is None and env.broker.posts == []
    monkeypatch.setattr(execution, '_saved_forecast', saved_read)
    env.executor.recover()
    assert record(env)['state'] == 'UNKNOWN' and load(env) is None
    env.forecast = forecast(env, advance=True)
    assert env.executor.step(env.forecast)['disposition'] == 'submission_recovery_required'
    assert env.broker.posts == []


def test_later_exact_broker_evidence_invalidates_original_clearance(env, monkeypatch):
    expire_at(env, monkeypatch, 5)
    env.executor.step(env.forecast)
    old = record(env)
    # Synthetic evidence transition tests the loader's full-chain invariant;
    # no production journal, broker response or fill is fabricated by this test.
    env.executor.journal.outcome(old['request']['request_id'], old['send_token'], '123')
    with pytest.raises(ContractError):
        load(env, old)
    with pytest.raises(ContractError):
        load(env, record(env))
    assert env.broker.posts == []
