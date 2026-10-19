"""Own recovery stays within its existing fence without replaying broker writes."""
from contextlib import contextmanager
import json

import pandas as pd
import pytest

from test_intraday_account_execution import env, advance
from ml.account_gameplan.authority import AccountAuthority
from ml.intraday import account_execution as execution
from ml.intraday.prediction import ContractError
from ml.stock_trader.horizon_ledger import LedgerError


def terminal_orders(state, count=4):
    forecasts = []
    for ordinal in range(count):
        forecast = state.forecast(offset=15 * ordinal)
        state.now = pd.Timestamp(forecast["issued_at"])
        assert state.executor.step(forecast)["disposition"] == "submitted"
        order_id = next(reversed(state.orders))
        state.orders[order_id]["status"] = "CANCELLED"
        advance(state)
        state.executor.recover()
        forecasts.append(forecast)
    return forecasts


def test_terminal_history_validation_preserves_unexpired_capacity_fence(env, monkeypatch):
    terminal_orders(env)
    submissions = len(env.broker.submissions)
    phase = {"active": False, "charged_seconds": 0, "published": False}
    original_transaction = execution.HorizonLedger._transaction
    original_reconcile = execution.HorizonLedger.reconcile
    original_publish = AccountAuthority.publish_intraday_snapshot

    @contextmanager
    def costly_transaction(ledger):
        # Reproduce storage latency specifically between the native snapshot
        # and authority capacity publication, without real sleeps or live IO.
        if phase["active"]:
            advance(env, 17)
            phase["charged_seconds"] += 17
        with original_transaction(ledger) as db:
            yield db

    def reconciled(ledger, *args, **kwargs):
        result = original_reconcile(ledger, *args, **kwargs)
        phase["active"] = True
        return result

    def publish(authority, *args, **kwargs):
        result = original_publish(authority, *args, **kwargs)
        phase.update(active=False, published=True)
        return result

    monkeypatch.setattr(execution.HorizonLedger, "_transaction", costly_transaction)
    monkeypatch.setattr(execution.HorizonLedger, "reconcile", reconciled)
    monkeypatch.setattr(AccountAuthority, "publish_intraday_snapshot", publish)
    advance(env)
    result = env.executor.recover()
    assert phase["published"] and 0 < phase["charged_seconds"] < 60
    assert len(result) == 4
    assert {item["evidence"]["status"] for item in result} == {"CANCELLED"}
    assert len(env.broker.submissions) == submissions


@pytest.mark.parametrize("empty_inventory", [False, True])
def test_recovery_decodes_only_referenced_own_forecasts(env, monkeypatch, empty_inventory):
    required = [] if empty_inventory else terminal_orders(env, 2)
    for offset in (60, 75, 90):
        env.forecast(offset=offset)
    expected = {forecast["forecast_id"] for forecast in required}
    decoded = []
    original = env.store._forecast_value

    def validated(row):
        decoded.append(row[0])
        assert row[0] in expected, "Recovery decoded an unrelated historical forecast"
        return original(row)

    monkeypatch.setattr(env.store, "_forecast_value", validated)
    submissions = len(env.broker.submissions)
    advance(env)
    result = env.executor.recover()
    # Intention recording also validates its referenced forecast; both paths
    # must stay scoped to the required originals.
    assert set(decoded) == expected
    assert len(result) == len(expected)
    assert len(env.broker.submissions) == submissions


def test_stream_classification_avoids_transaction_per_historical_order(env, monkeypatch):
    terminal_orders(env)
    submissions = len(env.broker.submissions)
    phase = {"active": False, "charged_seconds": 0}
    original_transaction = execution.HorizonLedger._transaction
    original_publish = AccountAuthority.publish_intraday_snapshot

    @contextmanager
    def costly_transaction(ledger):
        if phase["active"]:
            advance(env, 17)
            phase["charged_seconds"] += 17
        with original_transaction(ledger) as db:
            yield db

    def publish(authority, *args, **kwargs):
        result = original_publish(authority, *args, **kwargs)
        phase["active"] = True
        return result

    monkeypatch.setattr(execution.HorizonLedger, "_transaction", costly_transaction)
    monkeypatch.setattr(AccountAuthority, "publish_intraday_snapshot", publish)
    advance(env)
    result = env.executor.recover()
    assert len(result) == 4 and 0 < phase["charged_seconds"] < 60
    assert len(env.broker.submissions) == submissions


@pytest.mark.parametrize("damage", ["payload", "indexed_symbol", "indexed_actor", "missing"])
def test_recovery_retains_referenced_forecast_validation(env, damage):
    forecast = terminal_orders(env, 1)[0]
    with env.store.connect() as db:
        if damage == "payload":
            value = dict(forecast, probability_not_down=0.99)
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (json.dumps(value), forecast["forecast_id"]))
        elif damage == "missing":
            db.execute("DELETE FROM forecasts WHERE id=?", (forecast["forecast_id"],))
        else:
            column, value = ("symbol", "MSFT") if damage == "indexed_symbol" else ("pc", "Scout")
            db.execute(f"UPDATE forecasts SET {column}=? WHERE id=?", (value, forecast["forecast_id"]))
    submissions = len(env.broker.submissions)
    advance(env)
    with pytest.raises(ContractError):
        env.executor.recover()
    assert len(env.broker.submissions) == submissions


@pytest.mark.parametrize("damage", ["native_id", "allocation_id", "broker_id"])
def test_batch_history_keeps_every_native_authority_identity_check(env, monkeypatch, damage):
    terminal_orders(env, 2)
    original = AccountAuthority.read_state

    def changed(authority):
        value = original(authority)
        row = value["reservations"][-1]
        if damage == "native_id":
            row["request"]["native_reservation_id"] = "missing-native-id"
        elif damage == "allocation_id":
            row["request"]["native_allocation_id"] = "different-allocation"
        else:
            row["broker_order_id"] = "different-broker-id"
        return value

    monkeypatch.setattr(AccountAuthority, "read_state", changed)
    submissions = len(env.broker.submissions)
    advance(env)
    error = LedgerError if damage == "native_id" else ContractError
    reason = "UNKNOWN_RESERVATION" if damage == "native_id" else "Local authority/native identity mismatch"
    with pytest.raises(error, match=reason):
        env.executor.recover()
    assert len(env.broker.submissions) == submissions
