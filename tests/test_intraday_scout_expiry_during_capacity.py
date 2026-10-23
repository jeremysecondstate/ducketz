"""Elapsed fixed targets never become new local attempts or replacements."""
from datetime import timedelta
import json

import pytest

from ml.intraday import scout_local_execution as execution
from ml.intraday.prediction import instant
from test_intraday_scout_local_execution import env, forecast, raw_order, record


@pytest.mark.parametrize('stage', ['first_capture', 'second_capture', 'capacity'])
@pytest.mark.parametrize('offset', [timedelta(0), timedelta(microseconds=1)], ids=['at-target', 'past-target'])
def test_expiry_during_preparation_is_terminal_without_an_order(env, monkeypatch, stage, offset):
    target = instant(env.forecast['target_close'])
    env.state.now = target - timedelta(seconds=10)
    if stage == 'capacity':
        clip = execution.clip_to_capacity

        def delayed_capacity(*args, **kwargs):
            value = clip(*args, **kwargs)
            env.state.now = target + offset
            return value

        monkeypatch.setattr(execution, 'clip_to_capacity', delayed_capacity)
    else:
        capture = env.executor._capture
        delayed = 1 if stage == 'first_capture' else 2

        def delayed_capture():
            value = capture()
            if env.captures == delayed:
                env.state.now = target + offset
            return value

        monkeypatch.setattr(env.executor, '_capture', delayed_capture)

    result = env.executor.step(env.forecast)

    assert result['disposition'] == 'fixed_target_already_elapsed'
    assert result['submission_status'] == 'not_submitted'
    assert result['submitted_quantity'] == result['filled_quantity'] == 0
    assert record(env) is None
    assert env.broker.posts == [] and env.broker.deletes == []
    receipts = [json.loads(path.read_text()) for path in (env.executor.books.root / 'receipts').glob('*.json')]
    assert all(receipt['kind'] != 'local-account-permit' for receipt in receipts)
    original = env.executor.intents.get(env.forecast['forecast_id'])
    if stage == 'first_capture':
        assert original is None and env.captures == 1
    else:
        assert original['desired_quantity'] == 4


def test_first_capture_expiry_preserves_an_existing_saved_sizing(env, monkeypatch):
    original = env.executor.intents.save_sizing(env.forecast, execution.size_forecast(env.forecast))
    env.state.now = instant(env.forecast['target_close']) - timedelta(seconds=10)
    capture = env.executor._capture

    def delayed_capture():
        value = capture()
        env.state.now = instant(env.forecast['target_close'])
        return value

    monkeypatch.setattr(env.executor, '_capture', delayed_capture)
    assert env.executor.step(env.forecast)['disposition'] == 'fixed_target_already_elapsed'
    assert env.executor.intents.get(env.forecast['forecast_id']) == original
    assert env.captures == 1 and record(env) is None
    assert env.broker.posts == [] and env.broker.deletes == []


def test_final_capacity_expiry_precedes_zero_cash_disposition(env, monkeypatch):
    target = instant(env.forecast['target_close'])
    env.state.now = target - timedelta(seconds=10)
    env.cash = 0
    clip = execution.clip_to_capacity

    def delayed_capacity(*args, **kwargs):
        value = clip(*args, **kwargs)
        env.state.now = target
        return value

    monkeypatch.setattr(execution, 'clip_to_capacity', delayed_capacity)
    result = env.executor.step(env.forecast)
    assert result['disposition'] == 'fixed_target_already_elapsed'
    assert result['adjusted_quantity'] == 0
    assert record(env) is None and env.broker.posts == [] and env.broker.deletes == []


def test_one_microsecond_before_target_retains_valid_request_and_single_send(env):
    env.state.now = instant(env.forecast['target_close']) - timedelta(microseconds=1)
    result = env.executor.step(env.forecast)
    assert result['disposition'] == 'submitted'
    request, permit = record(env)['request'], record(env)['grant']
    assert instant(request['reference_at']) < instant(request['issued_at']) < instant(request['valid_until'])
    assert request['valid_until'] == request['target_close_at']
    assert request['issued_at'] == permit['issued_at'] == env.state.now.isoformat()
    assert len(env.broker.posts) == 1 and env.broker.deletes == []
    assert env.executor.step(env.forecast)['disposition'] == 'recover_existing_intention'
    assert len(env.broker.posts) == 1


def test_expiry_during_sizing_keeps_intention_and_does_not_replace_existing_order(env, monkeypatch):
    assert env.executor.step(env.forecast)['disposition'] == 'submitted'
    previous = env.forecast
    env.forecast = forecast(env, advance=True)
    env.state.now = instant(env.forecast['target_close']) - timedelta(seconds=10)
    size = execution.size_forecast

    def delayed_sizing(*args, **kwargs):
        value = size(*args, **kwargs)
        env.state.now = instant(env.forecast['target_close'])
        return value

    monkeypatch.setattr(execution, 'size_forecast', delayed_sizing)
    result = env.executor.step(env.forecast)
    assert result['disposition'] == 'fixed_target_already_elapsed'
    assert env.executor.intents.get(env.forecast['forecast_id'])['desired_quantity'] == 4
    assert record(env) is None and record(env, previous)['state'] == 'ACCEPTED'
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


def test_expiry_during_own_order_recovery_does_not_cancel(env, monkeypatch):
    assert env.executor.step(env.forecast)['disposition'] == 'submitted'
    previous = env.forecast
    env.forecast = forecast(env, advance=True)
    env.state.now = instant(env.forecast['target_close']) - timedelta(seconds=10)
    recover = env.executor._recover_record

    def delayed_recovery(*args, **kwargs):
        value = recover(*args, **kwargs)
        env.state.now = instant(env.forecast['target_close'])
        return value

    monkeypatch.setattr(env.executor, '_recover_record', delayed_recovery)
    result = env.executor.step(env.forecast)
    assert result['disposition'] == 'fixed_target_already_elapsed'
    assert record(env) is None and record(env, previous)['state'] == 'ACCEPTED'
    assert len(env.broker.posts) == 1 and env.broker.deletes == []


def test_expiry_during_cancellation_saved_read_blocks_delete_and_recovery_still_observes_fills(env, monkeypatch):
    assert env.executor.step(env.forecast)['disposition'] == 'submitted'
    previous = env.forecast
    env.forecast = forecast(env, advance=True)
    env.state.now = instant(env.forecast['target_close']) - timedelta(seconds=10)
    saved = execution._saved_forecast

    def delayed_read(*args, **kwargs):
        value = saved(*args, **kwargs)
        prior = record(env, previous)
        state = env.executor.journal.order_state(prior['request']['request_id'])
        if state['cancellation'] is not None:
            env.state.now = instant(env.forecast['target_close'])
        return value

    monkeypatch.setattr(execution, '_saved_forecast', delayed_read)
    result = env.executor.step(env.forecast)
    assert result['disposition'] == 'fixed_target_already_elapsed'
    assert len(env.broker.posts) == 1 and env.broker.deletes == []
    assert record(env) is None
    state = env.executor.journal.order_state(record(env, previous)['request']['request_id'])
    assert state['cancellation'] is not None
    assert not any(event['kind'] == 'cancellation_send_claimed' for event in state['cancellation_events'])
    env.broker.orders = [raw_order(env, status='PARTIALLY_FILLED', filled=1)]
    recovered, = env.executor.recover()
    assert recovered['filled_quantity'] == 1
    assert record(env, previous)['state'] == 'ACCEPTED'
    assert env.executor.step(env.forecast)['disposition'] == 'fixed_target_already_elapsed'
    assert len(env.broker.posts) == 1 and env.broker.deletes == []
