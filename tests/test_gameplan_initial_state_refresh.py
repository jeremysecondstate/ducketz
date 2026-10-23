"""Stale initial Gameplan decisions recapture before claiming an intention."""
from dataclasses import replace
import json
import socket

import pandas as pd
import pytest

from test_account_gameplan_execution import account_env, run, native, state
from test_independent_stock_runtime import environment
from ml.stock_trader import independent_runtime as runtime


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('Initial refresh regressions must never connect to a provider')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'create_connection', denied)


def delay_first_publication(env, monkeypatch, seconds=65):
    original = runtime.publish_decision_run
    publications = []
    def publish(*args, **kwargs):
        result = original(*args, **kwargs)
        publications.append(result)
        if len(publications) == 1:
            env.now += pd.Timedelta(seconds=seconds)
        return result
    monkeypatch.setattr(runtime, 'publish_decision_run', publish)
    return publications


def test_stale_first_intention_recaptures_and_submits_original_forecasts_once(account_env, monkeypatch):
    env = account_env
    original = {s.prediction_id: (s.calibrated_probability, s.target_window_start, s.target_window_end)
                for s in env.signals.values()}
    publications = delay_first_publication(env, monkeypatch)
    first = run(env)
    assert first.status == 'BROKER_STATE_REFRESH_REQUIRED', first.error
    assert first.error is None and first.submitted_orders == 0
    assert env.broker.submissions == env.broker.cancellations == []
    assert 'prepare' not in env.broker.calls and 'post' not in env.broker.calls
    assert native(env).snapshot().reservations == () and state(env)['reservations'] == []
    events = env.root / 'ml/stock-trader-execution-events'
    assert not events.exists() or not list(events.iterdir())
    frozen = (publications[0].run_directory / 'decisions.json').read_bytes()
    env.now += pd.Timedelta(milliseconds=1)
    assert all(env.now < pd.Timestamp(s.actionable_until) for s in env.signals.values())
    second = run(env)
    assert second.status == 'ORDERS_SUBMITTED', second.error
    assert second.submitted_orders == len(env.broker.submissions) == 2
    records = native(env).snapshot().reservations
    assert len(records) == len({r.forecast_id for r in records}) == 2
    assert {r.forecast_id for r in records} == set(original)
    assert (publications[0].run_directory / 'decisions.json').read_bytes() == frozen
    for publication in publications:
        for decision in json.loads((publication.run_directory / 'decisions.json').read_bytes())['decisions']:
            prediction = decision['prediction']
            if prediction['prediction_id'] in original:
                assert (prediction['calibrated_probability'], prediction['target_window_start'],
                        prediction['target_window_end']) == original[prediction['prediction_id']]


def test_stale_first_intention_never_clears_a_manual_stop(account_env, monkeypatch):
    env = account_env
    delay_first_publication(env, monkeypatch)
    original = runtime._submit_batch
    def submit(*args, **kwargs):
        env.active = False
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime, '_submit_batch', submit)
    first = run(env)
    assert first.status == 'BROKER_STATE_REFRESH_REQUIRED' and first.error is None
    assert not env.active and env.broker.submissions == env.broker.cancellations == []
    assert native(env).snapshot().reservations == () and state(env)['reservations'] == []
    second = run(env)
    assert second.submitted_orders == 0 and not env.active
    assert env.broker.submissions == []


def test_refresh_does_not_replay_an_expired_target(account_env, monkeypatch):
    env = account_env
    env.signals = {key: replace(s, actionable_until=(env.now+pd.Timedelta(seconds=30)).isoformat())
                   for key, s in env.signals.items()}
    delay_first_publication(env, monkeypatch)
    first = run(env)
    assert first.status == 'BROKER_STATE_REFRESH_REQUIRED' and first.error is None
    second = run(env)
    assert second.status == 'HORIZON_BROKER_RECONCILIATION_UNAVAILABLE'
    assert second.error == 'ValueError: BROKER_READ_EXECUTION_WINDOW_CLOSED'
    assert env.broker.submissions == []
    assert native(env).snapshot().reservations == () and state(env)['reservations'] == []


@pytest.mark.parametrize('observed,error', [(None, 'CURRENT_QUOTE_TIME_UNAVAILABLE'),
    ('future', 'CURRENT_QUOTE_TOO_OLD_FOR_SUBMISSION'), ('invalid', None), ('malformed', None)])
def test_bad_quote_retains_original_final_gate_failure(account_env, monkeypatch, observed, error):
    env = account_env
    from ml.stock_trader import gameplan_direction_engine
    build = gameplan_direction_engine.build_gameplan_direction_trade_decisions
    def decisions(*args, **kwargs):
        result = build(*args, **kwargs)
        stamp = ((env.now+pd.Timedelta(seconds=1)).isoformat() if observed == 'future' else
                 'invalid' if observed == 'invalid' else None)
        quote_changes = {'observed_at':stamp}
        if observed == 'malformed':
            quote_changes['unexpected_field'] = True
        return tuple(replace(d, quote={**d.quote, **quote_changes}) if d.quantity else d for d in result)
    monkeypatch.setattr(gameplan_direction_engine, 'build_gameplan_direction_trade_decisions', decisions)
    result = run(env)
    assert result.status == 'SUBMISSION_STOPPED_SAFETY_CHECK'
    assert result.error and (error is None or error in result.error)
    assert env.broker.submissions == env.broker.cancellations == []
