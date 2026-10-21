"""Already expired instructions with no own order work need no live capture."""
from dataclasses import asdict, replace

import pandas as pd
import pytest

from ml.account_gameplan.authority import AccountAuthority, AuthorityError
from ml.intraday import account_execution as execution
from ml.intraday.prediction import ContractError
from test_intraday_account_execution import env, native, account, advance
from test_intraday_unknown_commitment_containment import uncertain


def expired(env, *, offset=0, symbol="AAPL"):
    forecast = env.forecast(offset=offset, symbol=symbol)
    env.now = pd.Timestamp(forecast["target_close"]) + pd.Timedelta(seconds=60)
    return forecast


def no_capture(env, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Expired no-order work must not request broker evidence")
    monkeypatch.setattr(execution, "capture_order_evidence", forbidden)
    monkeypatch.setattr(execution, "capture_portfolio_state", forbidden)
    return env.captures


def terminal(env, *, symbol="AMZN"):
    forecast = env.forecast(symbol=symbol)
    env.now = pd.Timestamp(forecast["issued_at"])
    env.executor.step(forecast)
    order = next(r for r in native(env).snapshot().reservations
                 if r.forecast_id == forecast["forecast_id"])
    env.orders[order.broker_order_id]["status"] = "CANCELLED"
    advance(env)
    env.executor.recover()
    return forecast, native(env).lookup_reservation(order.reservation_id)


def test_expired_empty_order_history_records_without_any_broker_capture(env, monkeypatch):
    forecast = expired(env)
    captures = no_capture(env, monkeypatch)
    result = env.executor.step(forecast)
    assert result["disposition"] == "fixed_target_already_elapsed"
    assert result["evidence"]["no_pending_own_order_work"] is True
    assert env.captures == captures and not env.broker.submissions and not env.broker.cancellations
    assert native(env).snapshot().reservations == ()
    assert account(env).read_state()["reservations"] == []
    assert env.executor.intents.get(forecast["forecast_id"]) is None
    assert env.store.forecast(forecast["cycle_id"], forecast["symbol"]) == forecast


def test_unavailable_provider_is_irrelevant_to_expired_empty_order_history(env):
    forecast = expired(env)
    env.capture_fail = TimeoutError("synthetic unavailable portfolio")
    assert env.executor.step(forecast)["disposition"] == "fixed_target_already_elapsed"
    assert env.captures == 0 and not env.broker.submissions and not env.broker.cancellations


def test_saved_positive_desired_size_is_immutable_when_expired_without_order(env, monkeypatch):
    env.cash = 0
    forecast = env.forecast()
    first = env.executor.step(forecast)
    assert first["desired_quantity"] > 0 and first["adjusted_quantity"] == 0
    original = env.executor.intents.get(forecast["forecast_id"])
    history = env.executor.intents.history(forecast["forecast_id"])
    env.now = pd.Timestamp(forecast["target_close"]) + pd.Timedelta(seconds=60)
    captures = no_capture(env, monkeypatch)
    result = env.executor.step(forecast)
    assert result["disposition"] == "fixed_target_already_elapsed"
    assert result["desired_quantity"] == original["desired_quantity"]
    assert env.executor.intents.get(forecast["forecast_id"]) == original
    assert env.executor.intents.history(forecast["forecast_id"])[:len(history)] == history
    assert env.captures == captures and not env.broker.submissions


def test_same_original_terminal_order_still_takes_exact_recovery_capture(env):
    forecast, order = terminal(env, symbol="AAPL")
    env.now = pd.Timestamp(forecast["target_close"]) + pd.Timedelta(seconds=60)
    captures = env.captures
    result = env.executor.step(forecast)
    assert result["disposition"] == "recover_existing_intention"
    assert env.captures == captures + 1
    assert native(env).lookup_reservation(order.reservation_id).status == "CANCELLED"
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


def test_expired_newer_forecast_still_cancels_exact_older_working_order(env):
    old = env.forecast()
    env.executor.step(old)
    order = native(env).snapshot().reservations[0]
    forecast = expired(env, offset=15)
    result = env.executor.step(forecast)
    assert result["disposition"] == "previous_intention_reconciliation"
    assert env.broker.cancellations == [order.broker_order_id]
    assert len(env.broker.submissions) == 1
    assert native(env).lookup_reservation(order.reservation_id).broker_order_id == order.broker_order_id


def test_expired_newer_same_symbol_never_releases_or_reposts_unknown(env):
    _, order = uncertain(env)
    before_native = asdict(order)
    before_authority = account(env).read_state()["reservations"][0]
    result = env.executor.step(expired(env, offset=15))
    assert result["disposition"] == "previous_intention_reconciliation"
    assert asdict(native(env).lookup_reservation(order.reservation_id)) == before_native
    assert account(env).read_state()["reservations"][0] == before_authority
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


@pytest.mark.parametrize("native_status", ["UNKNOWN", "RESERVED"])
def test_other_symbol_unknown_and_terminal_history_remain_exact_without_capture(env, monkeypatch, native_status):
    terminal(env, symbol="AAPL")
    env.cash = 250
    _, unknown = uncertain(env, symbol="AMZN")
    if native_status == "RESERVED":
        with native(env)._transaction() as db:
            db.execute("UPDATE reservations SET status='RESERVED' WHERE id=?", (unknown.reservation_id,))
    before_native = asdict(native(env).lookup_reservation(unknown.reservation_id))
    before_authority = account(env).read_state()["reservations"]
    forecast = expired(env, offset=30)
    captures = no_capture(env, monkeypatch)
    assert env.executor.step(forecast)["disposition"] == "fixed_target_already_elapsed"
    assert env.captures == captures
    assert asdict(native(env).lookup_reservation(unknown.reservation_id)) == before_native
    assert account(env).read_state()["reservations"] == before_authority
    assert len(env.broker.submissions) == 2 and not env.broker.cancellations


@pytest.mark.parametrize("damage", ["native_id", "allocation_id", "broker_id", "stream", "quantity", "forecast", "account", "symbols", "malformed_price"])
def test_expired_shortcut_defers_mismatched_own_authority_graph_to_existing_capture(env, monkeypatch, damage):
    terminal(env)
    forecast = expired(env)
    original = AccountAuthority.read_state
    def changed(authority):
        result = original(authority)
        row = result["reservations"][0]
        if damage == "broker_id": row["broker_order_id"] = "different"
        else:
            key, value = {"native_id":("native_reservation_id", "missing"),
                "allocation_id":("native_allocation_id", "different"), "stream":("stream", "scout-15m"),
                "quantity":("quantity", row["request"]["quantity"] + 1), "forecast":("forecast_id", "f"*64),
                "account":("account_fingerprint", "f"*64), "symbols":("local_symbols", ["AAPL"]),
                "malformed_price":("limit_price", None)}[damage]
            row["request"][key] = value
        return result
    monkeypatch.setattr(AccountAuthority, "read_state", changed)
    calls=[]
    def capture(*args, **kwargs):
        calls.append(True)
        raise TimeoutError("synthetic normal path marker")
    monkeypatch.setattr(env.executor, "_capture", capture)
    with pytest.raises(TimeoutError, match="normal path marker"):
        env.executor.step(forecast)
    assert calls == [True]
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations


def test_expired_shortcut_defers_unmatched_known_native_to_existing_capture(env, monkeypatch):
    terminal(env)
    forecast = expired(env)
    original = AccountAuthority.read_state
    def missing(authority):
        result = original(authority); result["reservations"] = []; return result
    monkeypatch.setattr(AccountAuthority, "read_state", missing)
    calls=[]
    def capture(*args, **kwargs):
        calls.append(True)
        raise TimeoutError("synthetic normal path marker")
    monkeypatch.setattr(env.executor, "_capture", capture)
    with pytest.raises(TimeoutError, match="normal path marker"):
        env.executor.step(forecast)
    assert calls == [True]


def test_unrelated_native_authority_status_asymmetry_uses_normal_recovery_capture(env, monkeypatch):
    terminal(env)
    forecast = expired(env)
    original = AccountAuthority.read_state
    def earlier(authority):
        result = original(authority);result["reservations"][0]["status"]="WORKING";return result
    monkeypatch.setattr(AccountAuthority, "read_state", earlier)
    calls=[]
    def capture(*args, **kwargs):
        calls.append(True)
        raise TimeoutError("synthetic normal path marker")
    monkeypatch.setattr(env.executor, "_capture", capture)
    with pytest.raises(TimeoutError, match="normal path marker"):
        env.executor.step(forecast)
    assert calls == [True]


def test_foreign_native_stream_cannot_make_expired_shortcut_silently_ignore_order(env, monkeypatch):
    _, order = terminal(env)
    with native(env)._transaction() as db:
        db.execute("UPDATE intraday_streams SET stream='scout-15m' WHERE kind='reservation' AND id=?", (order.reservation_id,))
    forecast = expired(env)
    calls=[]
    def capture(*args, **kwargs):
        calls.append(True)
        raise TimeoutError("synthetic normal path marker")
    monkeypatch.setattr(env.executor, "_capture", capture)
    with pytest.raises(TimeoutError, match="normal path marker"):
        env.executor.step(forecast)
    assert calls == [True]


def test_definitive_native_only_unsent_rejection_does_not_add_recovery_gate(env, monkeypatch):
    forecast = env.forecast(symbol="AMZN")
    def unavailable_admission(*args, **kwargs):
        raise TimeoutError("synthetic pre-admission failure")
    with monkeypatch.context() as scoped:
        scoped.setattr(AccountAuthority, "reserve_intraday", unavailable_admission)
        assert env.executor.step(forecast)["disposition"] == "not_submitted"
    rejected, = native(env).snapshot().reservations
    assert rejected.status == "REJECTED" and rejected.broker_order_id is None
    assert account(env).read_state()["reservations"] == []
    expired_forecast = expired(env, offset=15)
    no_capture(env, monkeypatch)
    assert env.executor.step(expired_forecast)["disposition"] == "fixed_target_already_elapsed"
    assert native(env).lookup_reservation(rejected.reservation_id) == rejected
    assert not env.broker.submissions and not env.broker.cancellations


@pytest.mark.parametrize("stop", ["manual", "stopped"])
def test_expired_empty_path_still_obeys_manual_stops(env, monkeypatch, stop):
    forecast = expired(env)
    if stop == "manual": env.active = False
    else: env.executor.stopped = lambda: True
    no_capture(env, monkeypatch)
    with pytest.raises(ContractError, match="Manual 15m session is no longer active"):
        env.executor.step(forecast)
    assert not env.executor.intents.history(forecast["forecast_id"])


@pytest.mark.parametrize("change", ["binding", "manual", "expired_fence"])
def test_expired_path_rechecks_binding_manual_authority_and_same_fence_after_local_reads(env, monkeypatch, change):
    forecast = expired(env)
    original = env.executor._newer_forecasts
    def changed(value):
        result = original(value)
        if change == "binding": env.config = replace(env.config, fingerprint="f"*64)
        elif change == "manual": env.active = False
        else: advance(env, 61)
        return result
    monkeypatch.setattr(env.executor, "_newer_forecasts", changed)
    no_capture(env, monkeypatch)
    with pytest.raises((ContractError, AuthorityError)):
        env.executor.step(forecast)
    assert not env.executor.intents.history(forecast["forecast_id"])


def test_valid_direction_keeps_existing_live_capture_and_submission(env):
    forecast = env.forecast()
    result = env.executor.step(forecast)
    assert result["disposition"] == "submitted"
    assert env.captures == 1 and len(env.broker.submissions) == 1


def test_expiry_receipt_survives_new_executor_and_does_not_replay(env, monkeypatch):
    forecast = expired(env)
    no_capture(env, monkeypatch)
    first = env.executor.step(forecast)
    advance(env)
    env.executor = execution.IntradayAccountExecutor(datastore=env.root, store=env.store,
        broker=env.broker, local_symbols=env.config.symbols, session_started_at=env.session_started_at,
        clock=env.clock, manual_authority=lambda: env.active)
    assert env.executor.step(forecast) == first
    assert env.executor.intents.history(forecast["forecast_id"]) == [first]
    assert native(env).snapshot().reservations == () and not env.broker.submissions


def test_newer_saved_forecast_keeps_durable_supersession_without_sizing(env, monkeypatch):
    older = env.forecast()
    newer = expired(env, offset=15)
    no_capture(env, monkeypatch)
    newer_result = env.executor.step(newer)
    older_result = env.executor.step(older)
    assert newer_result["disposition"] == "fixed_target_already_elapsed"
    assert older_result["disposition"] == "newer_or_same_reference_intention_already_processed"
    assert older_result["evidence"]["superseding_forecast_ids"] == [newer["forecast_id"]]
    assert env.executor.intents.get(older["forecast_id"]) is None
    assert env.executor.intents.get(newer["forecast_id"]) is None
    assert not env.broker.submissions and not env.broker.cancellations


def test_exact_newer_native_remains_a_chronology_fence_without_other_projections(env, monkeypatch):
    newer = env.forecast(offset=15)
    env.now = pd.Timestamp(newer["issued_at"])
    env.executor.step(newer)
    order, = native(env).snapshot().reservations
    env.orders[order.broker_order_id]["status"] = "CANCELLED"
    advance(env)
    env.executor.recover()
    older = expired(env)
    monkeypatch.setattr(env.executor, "_newer_forecasts", lambda _: [])
    monkeypatch.setattr(env.executor.intents, "processed_at_or_after", lambda _: [])
    no_capture(env, monkeypatch)
    result = env.executor.step(older)
    assert result["disposition"] == "newer_or_same_reference_intention_already_processed"
    assert result["evidence"]["native_reservation_ids"] == [order.reservation_id]
    assert result["evidence"]["processed_forecast_ids"] == []
    assert len(env.broker.submissions) == 1 and not env.broker.cancellations
