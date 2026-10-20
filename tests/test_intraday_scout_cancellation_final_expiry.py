"""Final cancellation I/O cannot extend a replacement forecast's fixed target."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.services import schwab
from ml.intraday import scout_local_execution as execution
from ml.intraday.prediction import instant
from test_intraday_scout_broker import Broker
from test_intraday_scout_local_execution import env, forecast, raw_order, record


class NativeCancellationBroker(Broker, schwab.SchwabSession):
    cancel_prepared_order = schwab.SchwabSession.cancel_prepared_order


def native_transport(env, monkeypatch, *, error=None):
    monkeypatch.setattr(env.broker, '__class__', NativeCancellationBroker)

    def prepared():
        return schwab.SchwabOrderSubmissionContext(account_hash='synthetic-account',
            access_token='synthetic-token', identity_fingerprint=env.broker.generation)

    def delete(url, **kwargs):
        env.broker.deletes.append((url, kwargs))
        if error is not None:
            raise error
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(env.broker, 'prepare_order_submission', prepared)
    monkeypatch.setattr(schwab.requests, 'delete', delete)


def replacement(env):
    assert env.executor.step(env.forecast)['disposition'] == 'submitted'
    previous = env.forecast
    env.forecast = forecast(env, advance=True)
    target = instant(env.forecast['target_close'])
    env.state.now = target - timedelta(seconds=10)
    return previous, target


def delay_io(env, monkeypatch, stage, target):
    journal_type = type(env.executor.journal)
    observe, state = journal_type.observe_order, journal_type.order_state
    claim = journal_type.record_cancellation_event
    progress = dict(observe=0, state=0, crossed=False)

    def delayed_observe(journal, *args, **kwargs):
        value = observe(journal, *args, **kwargs)
        if state(journal, args[0])['cancellation'] is not None:
            progress['observe'] += 1
            if stage == 'final_observe' and progress['observe'] == 4:
                env.state.now, progress['crossed'] = target, True
        return value

    def delayed_state(journal, *args, **kwargs):
        value = state(journal, *args, **kwargs)
        if value['cancellation'] is not None:
            progress['state'] += 1
            if stage == 'final_state' and progress['state'] == 4:
                env.state.now, progress['crossed'] = target, True
        return value

    def delayed_claim(journal, event_id, kind, payload):
        value = claim(journal, event_id, kind, payload)
        if stage == 'claim' and kind == 'cancellation_send_claimed' and not progress['crossed']:
            env.state.now, progress['crossed'] = target, True
        return value

    monkeypatch.setattr(journal_type, 'observe_order', delayed_observe)
    monkeypatch.setattr(journal_type, 'order_state', delayed_state)
    monkeypatch.setattr(journal_type, 'record_cancellation_event', delayed_claim)
    return progress


def cancellation_state(env, previous):
    return env.executor.journal.order_state(record(env, previous)['request']['request_id'])


def next_forecast(env, target, *, probability=.72):
    env.forecast = forecast(env, advance=True, probability=probability)
    env.state.now = max(env.state.now, target + timedelta(seconds=1))
    return env.forecast


@pytest.mark.parametrize('stage', ['final_observe', 'final_state', 'claim'])
@pytest.mark.parametrize('offset', [timedelta(0), timedelta(microseconds=1)], ids=['at', 'past'])
def test_final_native_io_expiry_skips_delete_and_preserves_known_order_recovery(env, monkeypatch, stage, offset):
    previous, target = replacement(env)
    native_transport(env, monkeypatch)
    progress = delay_io(env, monkeypatch, stage, target + offset)
    result = env.executor.step(env.forecast)
    assert progress['crossed'] is True
    assert result['disposition'] == 'fixed_target_already_elapsed'
    assert result['submission_status'] == 'not_submitted'
    assert env.broker.deletes == [] and len(env.broker.posts) == 1
    assert record(env) is None and record(env, previous)['state'] == 'ACCEPTED'
    events = cancellation_state(env, previous)['cancellation_events']
    assert any(item['kind'] == 'cancellation_send_claimed' for item in events) is (stage == 'claim')
    assert any(item['kind'] == 'cancellation_temporary_failure' for item in events) is (stage == 'claim')
    env.broker.orders = [raw_order(env, status='PARTIALLY_FILLED', filled=1)]
    recovered, = env.executor.recover()
    assert recovered['filled_quantity'] == 1 and record(env, previous)['state'] == 'ACCEPTED'
    assert env.executor.step(env.forecast)['disposition'] == 'fixed_target_already_elapsed'
    assert env.broker.deletes == [] and len(env.broker.posts) == 1


def test_exact_native_unsent_proof_allows_only_a_different_eligible_forecast(env, monkeypatch):
    previous, target = replacement(env)
    native_transport(env, monkeypatch)
    delay_io(env, monkeypatch, 'claim', target)
    expired = env.forecast
    assert env.executor.step(expired)['disposition'] == 'fixed_target_already_elapsed'
    assert env.executor.step(expired)['disposition'] == 'fixed_target_already_elapsed'
    next_forecast(env, target)
    env.state.now = target = instant(env.forecast['target_close'])
    assert env.executor.step(env.forecast)['disposition'] == 'fixed_target_already_elapsed'
    assert env.broker.deletes == []
    next_forecast(env, target)
    assert env.executor.step(env.forecast)['disposition'] == 'previous_intention_reconciliation'
    assert len(env.broker.deletes) == 1 and len(env.broker.posts) == 1
    assert env.executor.step(env.forecast)['disposition'] == 'previous_intention_reconciliation'
    assert len(env.broker.deletes) == 1
    assert record(env, previous)['state'] == 'ACCEPTED'


@pytest.mark.parametrize('field,value', [
    ('reservation_id', 'wrong'), ('broker_order_id', '999'), ('symbol', 'WRONG'),
    ('request_sha256', 'f' * 64), ('native_method', 'wrapper'), ('stage', 'after-delete'),
    ('no_delete_entered', False), ('no_delete_entered', 1), ('claim_sha256', 'f' * 64),
    ('attempt_id', 'other'), ('schema_version', 'other'), ('forecast_id', 'invalid'),
    ('forecast_id', 'f' * 64), ('cycle_id', 'other'), ('observed_at', 'invalid'),
    ('observed_at', '2026-10-12T19:00:00Z'), ('target_close', 'invalid'),
    ('target_close', '2026-10-12T17:59:59Z'), ('disposition', 'other')])
def test_malformed_or_differently_bound_unsent_proof_cannot_rearm(env, monkeypatch, field, value):
    previous, target = replacement(env)
    native_transport(env, monkeypatch)
    delay_io(env, monkeypatch, 'claim', target)
    assert env.executor.step(env.forecast)['disposition'] == 'fixed_target_already_elapsed'
    next_forecast(env, target)
    journal_type = type(env.executor.journal)
    state = journal_type.order_state

    def changed_state(journal, *args, **kwargs):
        result = state(journal, *args, **kwargs)
        for item in result['cancellation_events']:
            if item['kind'] == 'cancellation_temporary_failure':
                item['payload'][field] = value
        return result

    monkeypatch.setattr(journal_type, 'order_state', changed_state)
    assert env.executor.step(env.forecast)['disposition'] == 'previous_intention_reconciliation'
    assert env.broker.deletes == [] and record(env, previous)['state'] == 'ACCEPTED'


def test_custom_transport_expiry_leaves_the_claim_fenced_without_native_proof(env, monkeypatch):
    previous, target = replacement(env)
    delay_io(env, monkeypatch, 'claim', target)
    assert env.executor.step(env.forecast)['disposition'] == 'fixed_target_already_elapsed'
    assert env.broker.deletes == []
    events = cancellation_state(env, previous)['cancellation_events']
    assert [item['kind'] for item in events] == ['cancellation_send_claimed']
    next_forecast(env, target)
    assert env.executor.step(env.forecast)['disposition'] == 'previous_intention_reconciliation'
    assert env.broker.deletes == []


def test_ambiguous_native_delete_is_never_treated_as_unsent(env, monkeypatch):
    previous, target = replacement(env)
    native_transport(env, monkeypatch, error=TimeoutError('synthetic lost DELETE response'))
    with pytest.raises(TimeoutError):
        env.executor.step(env.forecast)
    assert len(env.broker.deletes) == 1
    events = cancellation_state(env, previous)['cancellation_events']
    assert [item['kind'] for item in events] == ['cancellation_send_claimed']
    next_forecast(env, target)
    assert env.executor.step(env.forecast)['disposition'] == 'previous_intention_reconciliation'
    assert len(env.broker.deletes) == 1


def test_claim_commit_then_exception_preserves_the_crash_fence(env, monkeypatch):
    previous, target = replacement(env)
    native_transport(env, monkeypatch)
    journal_type = type(env.executor.journal)
    claim = journal_type.record_cancellation_event

    def committed_then_failed(journal, event_id, kind, payload):
        result = claim(journal, event_id, kind, payload)
        if kind == 'cancellation_send_claimed':
            raise OSError('synthetic interruption after durable claim')
        return result

    monkeypatch.setattr(journal_type, 'record_cancellation_event', committed_then_failed)
    with pytest.raises(OSError):
        env.executor.step(env.forecast)
    assert env.broker.deletes == []
    events = cancellation_state(env, previous)['cancellation_events']
    assert [item['kind'] for item in events] == ['cancellation_send_claimed']
    next_forecast(env, target)
    assert env.executor.step(env.forecast)['disposition'] == 'previous_intention_reconciliation'
    assert env.broker.deletes == []


def test_valid_native_cancellation_remains_single_send(env, monkeypatch):
    previous, _ = replacement(env)
    native_transport(env, monkeypatch)
    assert env.executor.step(env.forecast)['disposition'] == 'previous_intention_reconciliation'
    assert len(env.broker.deletes) == 1 and len(env.broker.posts) == 1
    assert env.executor.step(env.forecast)['disposition'] == 'previous_intention_reconciliation'
    assert len(env.broker.deletes) == 1
    assert record(env, previous)['state'] == 'ACCEPTED'


def test_prior_manual_sessions_native_unsent_proof_preserves_known_order_identity(env, monkeypatch):
    previous, target = replacement(env)
    native_transport(env, monkeypatch)
    delay_io(env, monkeypatch, 'claim', target)
    assert env.executor.step(env.forecast)['disposition'] == 'fixed_target_already_elapsed'
    old = env.executor
    env.reference = target
    env.state.now = target + timedelta(minutes=16, seconds=10)
    env.executor = execution.ScoutLocalAccountExecutor(repository=old.repository, datastore=old.root,
        profile=old.profile, store=env.store, broker=env.broker, local_symbols=old.symbols,
        session_started_at=target + timedelta(seconds=1), session_id='second-manual-local-test',
        clock=lambda: env.state.now.isoformat(), manual_authority=lambda: env.state.manual,
        stopped=lambda: env.state.stop, books=old.books)
    try:
        env.executor.preflight()
        env.forecast = forecast(env)
        assert env.executor.step(env.forecast)['disposition'] == 'previous_intention_reconciliation'
        assert len(env.broker.deletes) == 1 and len(env.broker.posts) == 1
        assert old.journal.get(execution.PREFIX + previous['forecast_id'])['state'] == 'ACCEPTED'
    finally:
        old.journal.close()


def test_typed_transport_exception_after_delete_is_not_native_callback_proof(env, monkeypatch):
    previous, target = replacement(env)
    native_transport(env, monkeypatch,
        error=execution._CancellationForecastIneligible('fixed_target_already_elapsed'))
    assert env.executor.step(env.forecast)['disposition'] == 'fixed_target_already_elapsed'
    assert len(env.broker.deletes) == 1
    events = cancellation_state(env, previous)['cancellation_events']
    assert [item['kind'] for item in events] == ['cancellation_send_claimed']
    next_forecast(env, target)
    assert env.executor.step(env.forecast)['disposition'] == 'previous_intention_reconciliation'
    assert len(env.broker.deletes) == 1
