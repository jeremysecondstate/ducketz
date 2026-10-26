"""Late preparation uses actual fresh capacity; all IO is fake/temp-ledger only."""
from dataclasses import asdict, replace
import socket

import pandas as pd
import pytest

try:
    from test_intraday_main_account_execution import env, native, advance
except ModuleNotFoundError as exc:
    if exc.name != "test_intraday_main_account_execution":
        raise
    from test_intraday_account_execution import env, native, advance
from ml.account_gameplan.authority import AccountAuthority
from ml.intraday import account_execution as execution
from ml.intraday.prediction import ContractError
from ml.stock_trader.horizon_ledger import LedgerError


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Late-refresh tests must never connect to a provider")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


class PreparationClock:
    """Keep the same real temporary fence during fake preparation latency."""
    def __init__(self, env):
        self.env, self.records = env, []

    def tick(self, seconds):
        advance(self.env, seconds)
        lease = self.env.executor._lease_keeper.renew()
        self.records.append((False, lease.token, lease.epoch, lease.holder_id))

    def slow_preparation(self):
        for seconds in (20, 20, 20, 5):
            self.tick(seconds)


def slow_late_preparation(env, monkeypatch, stage, *, repeat=False, after=None, seconds=65):
    worker = PreparationClock(env)
    owner, name = {
        "capacity_read": (AccountAuthority, "available_intraday_capacity"),
        "capacity_record": (env.executor, "_record"),
        "native_history": (env.executor, "_same_forecast"),
        "authority_history": (AccountAuthority, "read_state"),
        "prepared_context": (env.broker, "prepare_order_submission"),
    }[stage]
    original = getattr(owner, name)
    calls = []

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        trace = env.executor._execution_trace
        eligible = (stage not in {"capacity_record", "native_history", "authority_history"}
            or (stage == "capacity_record" and kwargs.get("kind") == "capacity")
            or (stage == "native_history" and trace.stage == "submission_previous_native_read")
            or (stage == "authority_history" and trace.stage == "submission_authority_read"))
        if eligible and (repeat or not calls):
            calls.append(True)
            remaining = seconds
            while remaining:
                elapsed = min(20, remaining)
                worker.tick(elapsed)
                remaining = round(remaining - elapsed, 6)
            if after is not None:
                after()
        return result

    monkeypatch.setattr(owner, name, delayed)
    return worker, calls


@pytest.mark.parametrize("stage", ["capacity_read", "capacity_record", "native_history", "authority_history", "prepared_context"])
def test_aged_after_capacity_or_submission_preparation_refreshes_before_reserving(env, monkeypatch, stage):
    forecast = env.forecast()
    worker, calls = slow_late_preparation(env, monkeypatch, stage)
    result = env.executor.step(forecast)
    assert result["disposition"] == "submitted", result
    assert env.captures == 2 and calls == [True]
    assert result["desired_quantity"] == 4 and result["adjusted_quantity"] == 2
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations
    reservation, = native(env).snapshot().reservations
    assert reservation.forecast_id == forecast["forecast_id"] and reservation.broker_order_id == "1001"
    assert env.store.forecasts()[0] == forecast
    assert len({row[1:4] for row in worker.records}) == 1


def test_late_refresh_recomputes_live_cash_quote_capacity_without_resizing(env, monkeypatch):
    forecast = env.forecast()
    slow_late_preparation(env, monkeypatch, "capacity_record")
    capture = execution.capture_portfolio_state

    def changed(*args, **kwargs):
        if env.captures == 1:
            env.cash = 150
            env.equity *= 2
        portfolio = capture(*args, **kwargs)
        if env.captures == 2:
            portfolio = replace(portfolio, quotes={symbol: replace(quote, bid=100., ask=100.)
                for symbol, quote in portfolio.quotes.items()})
        return portfolio

    monkeypatch.setattr(execution, "capture_portfolio_state", changed)
    result = env.executor.step(forecast)
    assert result["disposition"] == "submitted" and result["desired_quantity"] == 4
    assert result["adjusted_quantity"] == 1 and env.broker.submissions[0]["price"] == "100.00"
    original = env.executor.intents.get(forecast["forecast_id"])
    assert original["desired_quantity"] == 4
    assert original["sizing"]["reference_context"]["quote"]["observed_at"] != env.executor.portfolio.quotes[forecast["symbol"]].freshness_observed_at
    assert env.store.forecasts()[0] == forecast and env.captures == 2


def test_second_aged_preparation_keeps_guard_without_reservation_or_third_capture(env, monkeypatch):
    forecast = env.forecast()
    _, calls = slow_late_preparation(env, monkeypatch, "prepared_context", repeat=True)
    with pytest.raises(LedgerError, match="STALE_OR_FUTURE_PORTFOLIO_EVIDENCE"):
        env.executor.step(forecast)
    assert env.captures == 2 and calls == [True, True]
    assert native(env).snapshot().reservations == ()
    assert not env.broker.submissions and not env.broker.cancellations


@pytest.mark.parametrize("seconds,captures", [(60, 1), (60.000001, 2)])
def test_late_snapshot_age_boundary_remains_inclusive(env, monkeypatch, seconds, captures):
    forecast = env.forecast()
    slow_late_preparation(env, monkeypatch, "prepared_context", seconds=seconds)
    assert env.executor.step(forecast)["disposition"] == "submitted"
    assert env.captures == captures and len(env.broker.submissions) == 1


def test_manual_stop_during_late_preparation_prevents_refresh_and_reservation(env, monkeypatch):
    forecast = env.forecast()
    slow_late_preparation(env, monkeypatch, "capacity_record", after=lambda: setattr(env, "active", False))
    with pytest.raises(ContractError, match="Manual 15m session is no longer active"):
        env.executor.step(forecast)
    assert env.captures == 1 and native(env).snapshot().reservations == ()
    assert not env.broker.submissions and env.executor.intents.get(forecast["forecast_id"])["desired_quantity"] == 4


def test_expiry_during_late_preparation_never_refreshes_or_replays(env, monkeypatch):
    forecast = env.forecast()
    env.now = pd.Timestamp(forecast["target_close"]) - pd.Timedelta(seconds=30)
    slow_late_preparation(env, monkeypatch, "prepared_context")
    result = env.executor.step(forecast)
    assert result["disposition"] == "fixed_target_already_elapsed"
    assert env.captures == 1 and native(env).snapshot().reservations == ()
    assert not env.broker.submissions


def test_expiry_during_late_capture_preserves_no_send(env, monkeypatch):
    forecast = env.forecast()
    env.now = pd.Timestamp(forecast["target_close"]) - pd.Timedelta(seconds=100)
    worker, _ = slow_late_preparation(env, monkeypatch, "prepared_context")
    capture = execution.capture_portfolio_state

    def delayed(*args, **kwargs):
        if env.captures == 1:
            worker.slow_preparation()
        return capture(*args, **kwargs)

    monkeypatch.setattr(execution, "capture_portfolio_state", delayed)
    assert env.executor.step(forecast)["disposition"] == "fixed_target_already_elapsed"
    assert env.captures == 2 and native(env).snapshot().reservations == ()
    assert not env.broker.submissions


def test_late_refresh_retains_unknown_order_identity_and_commitment(env, monkeypatch):
    env.cash = 500
    uncertain = env.forecast(symbol="AMZN")
    env.broker.unknown_submission = True
    assert env.executor.step(uncertain)["disposition"] == "submission_recovery_required"
    env.broker.unknown_submission = False
    unknown, = native(env).snapshot().reservations
    retained = asdict(unknown)
    advance(env)
    forecast = env.forecast(symbol="AAPL")
    slow_late_preparation(env, monkeypatch, "prepared_context")
    assert env.executor.step(forecast)["disposition"] == "submitted"
    assert asdict(native(env).lookup_reservation(unknown.reservation_id)) == retained
    assert unknown.broker_order_id is None and env.captures == 3
    assert len(env.broker.submissions) == 2


def test_no_refresh_after_ambiguous_submission(env, monkeypatch):
    forecast = env.forecast()
    slow_late_preparation(env, monkeypatch, "prepared_context")
    env.broker.unknown_submission = True
    assert env.executor.step(forecast)["disposition"] == "submission_recovery_required"
    assert env.captures == 2 and len(env.broker.submissions) == 1
    assert env.executor.step(forecast)["disposition"] == "recover_existing_intention"
    assert env.captures == 3 and len(env.broker.submissions) == 1
