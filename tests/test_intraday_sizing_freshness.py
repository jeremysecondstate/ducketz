"""Preparation latency must not consume a still-valid forecast's broker evidence."""
from dataclasses import replace
import socket

import pandas as pd
import pytest

from ml.intraday import account_execution as execution
from ml.intraday.prediction import ContractError, instant
from test_intraday_account_execution import env, native
from test_intraday_lease_maintenance import WorkerClock


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Sizing freshness regressions must never connect to a provider")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def delay_preparation(env, monkeypatch, stage, *, after_delay=None):
    worker = WorkerClock(env, monkeypatch)
    owner, name = {
        "native_history": (env.executor, "_owned_native_reservations"),
        "intentions": (env.executor.intents, "processed_at_or_after"),
        "frozen_inputs": (env.store, "inputs"),
    }[stage]
    original = getattr(owner, name)
    calls = []

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(True)
        worker.slow_preparation()
        if after_delay is not None:
            after_delay()
        return result

    monkeypatch.setattr(owner, name, delayed)
    return worker, calls


@pytest.mark.parametrize("stage", ["native_history", "intentions", "frozen_inputs"])
def test_still_valid_forecast_recaptures_once_after_slow_preparation(env, monkeypatch, stage):
    forecast = env.forecast()
    worker, calls = delay_preparation(env, monkeypatch, stage)
    result = env.executor.step(forecast)
    assert result["disposition"] == "submitted", result
    assert calls == [True] and env.captures == 2
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations
    assert len({row[1:4] for row in worker.records}) == 1
    intended = env.executor.intents.get(forecast["forecast_id"])
    context = intended["sizing"]["reference_context"]
    assert context["account_equity"]["available_at"] == env.executor.portfolio.observed_at
    assert context["quote"]["observed_at"] == env.executor.portfolio.quotes[forecast["symbol"]].freshness_observed_at
    assert context["quote"]["evidence"]["snapshot_id"] == env.executor.snapshot_id
    assert instant(context["quote"]["observed_at"]) > instant(forecast["issued_at"])
    assert env.store.forecasts()[0] == forecast
    reservation, = native(env).snapshot().reservations
    assert reservation.forecast_id == forecast["forecast_id"]
    with env.store.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='sizing_unavailable_diagnostics'").fetchone()[0] == 0


def test_current_evidence_needs_only_the_existing_capture(env):
    forecast = env.forecast()
    assert env.executor.step(forecast)["disposition"] == "submitted"
    assert env.captures == 1 and len(env.broker.submissions) == 1


@pytest.mark.parametrize("quote_age,reason,captures", [(65, "quote_stale_or_future", 2),
    (-1, "quote_observation_after_availability", 1)])
def test_repeat_capture_retains_genuinely_stale_or_future_provider_quotes(env, quote_age, reason, captures):
    forecast = env.forecast()
    env.quote_age = quote_age
    result = env.executor.step(forecast)
    assert env.captures == captures
    assert result["kind"] == "sizing_unavailable" and result["disposition"] == reason
    assert env.executor.intents.get(forecast["forecast_id"]) is None
    assert env.broker.submissions == [] and env.broker.cancellations == []
    saved = result["evidence"]["sizing"]["reference_context"]["quote"]
    assert saved["observed_at"] == env.executor.portfolio.quotes[forecast["symbol"]].freshness_observed_at
    assert instant(saved["available_at"]) - instant(saved["observed_at"]) == pd.Timedelta(seconds=quote_age)


def test_account_observation_expiry_alone_requires_actual_recapture(env, monkeypatch):
    forecast = env.forecast()
    def quote_remains_fresh():
        portfolio = env.executor.portfolio
        quote = portfolio.quotes[forecast["symbol"]]
        env.executor.portfolio = replace(portfolio, quotes={**portfolio.quotes,
            forecast["symbol"]: replace(quote, observed_at=env.clock())})
    delay_preparation(env, monkeypatch, "frozen_inputs", after_delay=quote_remains_fresh)
    assert env.executor.step(forecast)["disposition"] == "submitted"
    assert env.captures == 2


@pytest.mark.parametrize("stop", ["manual", "stopped"])
def test_manual_stop_during_preparation_prevents_refresh_and_submission(env, monkeypatch, stop):
    forecast = env.forecast()
    def stop_after_delay():
        if stop == "manual":
            env.active = False
        else:
            env.executor.stopped = lambda: True
    delay_preparation(env, monkeypatch, "frozen_inputs", after_delay=stop_after_delay)
    with pytest.raises(ContractError, match="Manual 15m session is no longer active"):
        env.executor.step(forecast)
    assert env.captures == 1
    assert env.executor.intents.get(forecast["forecast_id"]) is None
    assert env.broker.submissions == [] and env.broker.cancellations == []


def test_expired_target_does_not_trigger_refresh_or_replay(env, monkeypatch):
    forecast = env.forecast()
    env.now = pd.Timestamp(forecast["target_close"]) - pd.Timedelta(seconds=30)
    delay_preparation(env, monkeypatch, "frozen_inputs")
    result = env.executor.step(forecast)
    assert env.captures == 1
    assert result["disposition"] == "quote_stale_or_future"
    assert result["evidence"]["sizing"]["calculated_after_target"]
    assert env.broker.submissions == [] and env.broker.cancellations == []


def test_existing_desired_size_is_not_recalculated_and_order_is_not_duplicated(env, monkeypatch):
    forecast = env.forecast()
    env.cash = 0
    env.executor.step(forecast)
    original = env.executor.intents.get(forecast["forecast_id"])
    assert original is not None and not env.broker.submissions
    env.cash = 150
    env.equity *= 2
    delay_preparation(env, monkeypatch, "intentions")
    capture = execution.capture_portfolio_state
    def changed_quote(*args, **kwargs):
        portfolio = capture(*args, **kwargs)
        if env.captures >= 3:
            portfolio = replace(portfolio, quotes={symbol: replace(quote, bid=100., ask=100.)
                for symbol, quote in portfolio.quotes.items()})
        return portfolio
    monkeypatch.setattr(execution, "capture_portfolio_state", changed_quote)
    result = env.executor.step(forecast)
    assert result["disposition"] == "submitted" and result["adjusted_quantity"] == 1
    assert env.broker.submissions[0]["price"] == "100.00"
    assert env.executor.intents.get(forecast["forecast_id"]) == original
    assert env.executor.step(forecast)["disposition"] == "recover_existing_intention"
    assert len(env.broker.submissions) == 1 and env.captures == 4


def test_refresh_failure_keeps_original_exception_and_never_saves_fake_sizing(env, monkeypatch):
    forecast = env.forecast()
    failure = TimeoutError("synthetic recapture unavailable")
    delay_preparation(env, monkeypatch, "frozen_inputs", after_delay=lambda: setattr(env, "capture_fail", failure))
    with pytest.raises(TimeoutError) as raised:
        env.executor.step(forecast)
    assert raised.value is failure
    assert env.captures == 1
    assert env.executor.intents.get(forecast["forecast_id"]) is None
    assert env.broker.submissions == [] and env.broker.cancellations == []


def test_target_expiring_during_recapture_never_submits_or_refreshes_again(env, monkeypatch):
    forecast = env.forecast()
    env.now = pd.Timestamp(forecast["target_close"]) - pd.Timedelta(seconds=100)
    worker, _ = delay_preparation(env, monkeypatch, "frozen_inputs")
    capture = env.executor._capture
    calls = []
    def delayed_capture(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            worker.slow_preparation()
        return capture(*args, **kwargs)
    monkeypatch.setattr(env.executor, "_capture", delayed_capture)
    result = env.executor.step(forecast)
    assert result["disposition"] == "fixed_target_already_elapsed"
    assert env.captures == 2 and len(calls) == 2
    assert env.broker.submissions == [] and env.broker.cancellations == []
    assert env.store.forecasts()[0] == forecast


def test_stop_during_refreshed_observation_prevents_sizing_and_submission(env, monkeypatch):
    forecast = env.forecast()
    delay_preparation(env, monkeypatch, "frozen_inputs")
    capture = execution.capture_portfolio_state
    def stop_during_capture(*args, **kwargs):
        result = capture(*args, **kwargs)
        if env.captures == 2:
            env.active = False
        return result
    monkeypatch.setattr(execution, "capture_portfolio_state", stop_during_capture)
    with pytest.raises(ContractError, match="Manual 15m session is no longer active"):
        env.executor.step(forecast)
    assert env.captures == 2 and env.executor.intents.get(forecast["forecast_id"]) is None
    assert env.broker.submissions == [] and env.broker.cancellations == []


def test_initial_terminal_fill_is_preserved_when_refresh_no_longer_queries_it(env, monkeypatch):
    old = env.forecast()
    assert env.executor.step(old)["disposition"] == "submitted"
    reservation, = native(env).snapshot().reservations
    original_order_id = reservation.broker_order_id
    env.orders[original_order_id].update(status="FILLED", filled=reservation.quantity, fill_at=env.clock())
    env.held[old["symbol"]] = reservation.quantity
    current = env.forecast(offset=15)
    env.now = pd.Timestamp(current["issued_at"])
    delay_preparation(env, monkeypatch, "frozen_inputs")
    result = env.executor.step(current)
    assert result["disposition"] == "submitted" and env.captures == 3
    retained = native(env).lookup_reservation(reservation.reservation_id)
    assert retained.broker_order_id == original_order_id
    assert retained.status == "FILLED" and retained.filled_quantity == reservation.quantity
    assert len(env.broker.submissions) == 2 and env.broker.cancellations == []
    with native(env)._transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM fills WHERE reservation=?", (reservation.reservation_id,)).fetchone()[0] == 1
