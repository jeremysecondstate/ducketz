"""Own recovery stays within its existing fence without replaying broker writes."""
from contextlib import contextmanager
import json
import time

import pandas as pd
import pytest

from test_intraday_account_execution import env, advance
from ml.account_gameplan.authority import AccountAuthority, AuthorityError
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


@pytest.mark.parametrize("slow_stage", ["native_reconcile", "native_identity_snapshot"])
def test_expired_recovery_retains_slow_stage_and_exact_failure(env, monkeypatch, slow_stage):
    """Delayed local work remains a real fence error, with its missing diagnosis."""
    terminal_orders(env, 1)
    submissions = len(env.broker.submissions)
    original_reconcile = execution.HorizonLedger.reconcile
    original_snapshot = execution.HorizonLedger.snapshot
    after_reconcile = {"value": False}
    monkeypatch.setattr(time, "monotonic", lambda: env.now.timestamp())

    def reconcile(ledger, *args, **kwargs):
        result = original_reconcile(ledger, *args, **kwargs)
        after_reconcile["value"] = True
        if slow_stage == "native_reconcile":
            advance(env, 65)
        return result

    def snapshot(ledger):
        result = original_snapshot(ledger)
        if after_reconcile["value"] and slow_stage == "native_identity_snapshot":
            after_reconcile["value"] = False
            advance(env, 65)
        return result

    monkeypatch.setattr(execution.HorizonLedger, "reconcile", reconcile)
    monkeypatch.setattr(execution.HorizonLedger, "snapshot", snapshot)
    advance(env)
    with pytest.raises(AuthorityError, match="CURRENT_FENCED_LEASE_REQUIRED") as failure:
        env.executor.recover()
    evidence = execution.execution_failure_evidence(failure.value)
    assert evidence["execution_stage"] == "capacity_publish"
    assert evidence["execution_elapsed_seconds"] >= 65
    assert next(item for item in evidence["execution_phases"] if item["stage"] == slow_stage)["elapsed_seconds"] >= 65
    assert evidence["root_cause"] == {"error_type": "AuthorityError", "message": "CURRENT_FENCED_LEASE_REQUIRED"}
    assert evidence["snapshot_observed_at"] and evidence["lease_expires_at"]
    assert env.executor.lease.token not in json.dumps(evidence)
    assert len(env.broker.submissions) == submissions


def test_capture_exception_and_cleanup_failure_retain_private_safe_original_cause(env, monkeypatch):
    root = TimeoutError("https://private-account.example/?token=do-not-persist")
    failure = AuthorityError("CURRENT_FENCED_LEASE_REQUIRED")
    failure.__cause__ = root
    env.capture_fail = failure

    def failed_release(authority, token):
        raise OSError("private-cleanup-path-and-token")

    monkeypatch.setattr(AccountAuthority, "release_lease", failed_release)
    with pytest.raises(AuthorityError) as raised:
        env.executor.recover()
    assert raised.value is failure and raised.value.__cause__ is root
    evidence = execution.execution_failure_evidence(raised.value)
    assert evidence["execution_stage"] == "portfolio_capture"
    assert evidence["root_cause"] == {"error_type": "TimeoutError"}
    assert evidence["cleanup_failure"]["execution_stage"] == "lease_release"
    assert evidence["cleanup_failure"]["exception_chain"][0] == {"error_type": "OSError"}
    encoded = json.dumps(evidence)
    assert "private-account" not in encoded and "do-not-persist" not in encoded
    assert "private-cleanup" not in encoded and env.executor.lease.token not in encoded
    assert env.broker.submissions == []


def test_unannotated_error_keeps_bounded_safe_chain_without_trusting_attributes():
    error = TimeoutError("private provider text")
    error.intraday_execution_failure = {"execution_stage": "private-token"}
    evidence = execution.execution_failure_evidence(error)
    assert evidence["execution_stage"] == "unclassified"
    assert evidence["root_cause"] == {"error_type": "TimeoutError"}
    assert "private" not in json.dumps(evidence)


def test_reused_exception_gets_current_attempt_stage_and_bounded_timings(env, monkeypatch):
    error = TimeoutError("private reused transport error")
    env.capture_fail = error
    with pytest.raises(TimeoutError):
        env.executor.recover()
    first = execution.execution_failure_evidence(error)
    assert first["execution_stage"] == "portfolio_capture"

    def fail_own_orders(*args, **kwargs):
        raise error

    monkeypatch.setattr(execution, "capture_order_evidence", fail_own_orders)
    advance(env)
    with pytest.raises(TimeoutError):
        env.executor.recover()
    current = execution.execution_failure_evidence(error)
    assert current is not first and current["execution_stage"] == "order_evidence_capture"
    assert len(current["execution_phases"]) <= 32
    assert env.broker.submissions == []


def test_newer_order_scan_preserves_fresh_sizing_without_per_order_transactions(env, monkeypatch):
    terminal_orders(env, 4)
    previous = set(env.orders)
    submissions = len(env.broker.submissions)
    forecast = env.forecast(offset=60)
    env.now = pd.Timestamp(forecast["issued_at"])
    phase = {"active": False, "charged_seconds": 0}
    original_transaction = execution.HorizonLedger._transaction
    original_publish = AccountAuthority.publish_intraday_snapshot
    original_sizing = execution.size_forecast

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

    def sizing(*args, **kwargs):
        phase["active"] = False
        return original_sizing(*args, **kwargs)

    monkeypatch.setattr(execution.HorizonLedger, "_transaction", costly_transaction)
    monkeypatch.setattr(AccountAuthority, "publish_intraday_snapshot", publish)
    monkeypatch.setattr(execution, "size_forecast", sizing)
    result = env.executor.step(forecast)
    assert result["disposition"] == "submitted", result
    assert 0 < phase["charged_seconds"] < 60
    assert len(env.broker.submissions) == submissions + 1
    assert previous < set(env.orders) and len(set(env.orders) - previous) == 1
    with env.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='sizing_unavailable_diagnostics'").fetchone()[0] == 0


def test_normal_stale_sizing_persists_slow_phase_without_changing_intention(env, monkeypatch):
    forecast = env.forecast()
    original_scan = env.executor.intents.processed_at_or_after
    monkeypatch.setattr(time, "monotonic", lambda: env.now.timestamp())

    def slow_scan(value):
        result = original_scan(value)
        advance(env, 65)
        return result

    monkeypatch.setattr(env.executor.intents, "processed_at_or_after", slow_scan)
    result = env.executor.step(forecast)
    assert result["kind"] == "sizing_unavailable"
    assert result["disposition"] == "quote_stale_or_future"
    assert result == env.executor.intents.history(forecast["forecast_id"])[-1]
    assert env.executor.intents.get(forecast["forecast_id"]) is None
    assert env.store.forecasts()[0] == forecast
    assert env.broker.submissions == [] and env.broker.cancellations == []
    with env.store.connect() as db:
        rows = db.execute("SELECT cycle_id,symbol,payload FROM events WHERE kind='sizing_unavailable_diagnostics'").fetchall()
    assert len(rows) == 1
    cycle_id, symbol, raw = rows[0]
    saved = json.loads(raw)
    assert (cycle_id, symbol) == (forecast["cycle_id"], forecast["symbol"])
    assert saved["forecast_id"] == forecast["forecast_id"]
    assert saved["sizing_id"] == execution.digest(result["evidence"]["sizing"])
    assert saved["disposition"] == result["disposition"]
    diagnostic = saved["diagnostics"]
    assert diagnostic["execution_stage"] == "sizing"
    assert diagnostic["execution_elapsed_seconds"] >= 65
    assert next(phase for phase in diagnostic["execution_phases"]
                if phase["stage"] == "newer_intention_scan")["elapsed_seconds"] >= 65
    assert diagnostic["snapshot_observed_at"] == env.executor.portfolio.observed_at
    assert len(diagnostic["execution_phases"]) <= 32
    assert set(diagnostic) == {"execution_stage", "execution_elapsed_seconds",
        "execution_stage_elapsed_seconds", "execution_phases", "lease_expires_at", "snapshot_observed_at"}
    assert env.executor.lease.token not in raw and env.config.account_fingerprint not in raw
    assert "account_equity" not in raw and "quote" not in json.dumps(diagnostic)


def test_unavailable_sizing_diagnostics_survive_previous_intention_cancellation(env, monkeypatch):
    from ml.intraday.technical_sizing import SizingResult
    from ml.intraday.prediction import digest, encoded

    old = env.forecast()
    assert env.executor.step(old)["disposition"] == "submitted"
    new = env.forecast(offset=15, probability=.25)
    env.now = pd.Timestamp(new["issued_at"])

    def unavailable(forecast, **kwargs):
        value = dict(forecast_id=forecast["forecast_id"], symbol="AAPL", side="SELL", status="unavailable",
            desired_quantity=None, calculated_at=env.clock(), reason="undefined_indicator_fixture")
        return SizingResult(digest(value), encoded(value))

    monkeypatch.setattr(execution, "size_forecast", unavailable)
    result = env.executor.step(new)
    assert result["disposition"] == "previous_intention_reconciliation"
    assert env.broker.cancellations == ["1001"] and len(env.broker.submissions) == 1
    history = env.executor.intents.history(new["forecast_id"])
    unavailable_entry = next(entry for entry in history if entry["kind"] == "sizing_unavailable")
    with env.store.connect() as db:
        rows = db.execute("SELECT payload FROM events WHERE kind='sizing_unavailable_diagnostics'").fetchall()
    assert len(rows) == 1
    saved = json.loads(rows[0][0])
    assert saved["forecast_id"] == new["forecast_id"]
    assert saved["sizing_id"] == digest(unavailable_entry["evidence"]["sizing"])
    assert saved["disposition"] == "undefined_indicator_fixture"
    assert saved["diagnostics"]["execution_stage"] == "sizing"
    assert "diagnostics" not in unavailable_entry["evidence"]


def test_execution_timing_snapshot_is_bounded_and_detached(monkeypatch):
    now = [0.]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    trace = execution._ExecutionTrace()
    for _ in range(40):
        now[0] += 1
        trace.advance("sizing")
    saved = trace.snapshot()
    assert len(saved["execution_phases"]) == 32
    assert saved["execution_elapsed_seconds"] == 40
    trace.advance("execution_after_sizing")
    assert saved["execution_stage"] == "sizing"
    saved["execution_phases"][-1]["elapsed_seconds"] = 999
    assert trace.snapshot()["execution_phases"][-2]["elapsed_seconds"] == 1
