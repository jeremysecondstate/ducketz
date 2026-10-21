"""Normal-operation clocks with temporary own ledgers and injected transports."""
from contextlib import contextmanager
from dataclasses import asdict
from types import SimpleNamespace
import json
import sqlite3

import pandas as pd
import pytest

from test_intraday_account_execution import env, native
from test_intraday_unknown_commitment_containment import uncertain
from ml.intraday import account_execution as execution
from ml.intraday.prediction import ContractError, instant


SESSION = "manual-atlas-15m:" + "a" * 32
KIND = "execution_operation_diagnostics"


def observations(state, operation=None):
    with state.store.connect() as db:
        rows = db.execute("SELECT cycle_id,symbol,payload FROM events WHERE kind=? ORDER BY id", (KIND,)).fetchall()
    values = [(cycle, symbol, json.loads(raw)) for cycle, symbol, raw in rows]
    return [row for row in values if operation is None or row[2]["operation"] == operation]


def bind_session(state):
    state.executor.diagnostic_session_id = SESSION


@pytest.mark.parametrize("history", ["empty", "terminal", "uncertain"])
def test_slow_normal_recovery_has_fresh_session_timing_without_changing_originals(env, monkeypatch, history):
    bind_session(env)
    if history == "terminal":
        forecast = env.forecast()
        env.executor.step(forecast)
        env.orders["1001"]["status"] = "CANCELLED"
        env.executor.recover()
    elif history == "uncertain":
        uncertain(env)
    before = tuple(asdict(row) for row in native(env).snapshot().reservations)
    submissions = len(env.broker.submissions)
    originals = env.store.forecasts()
    previous = len(observations(env, "recover"))
    ticks = [0.]
    monkeypatch.setattr(execution.time, "monotonic", lambda: ticks[0])
    original = env.executor._owned_native_reservations

    def slow_read(db):
        ticks[0] += 75
        return original(db)

    monkeypatch.setattr(env.executor, "_owned_native_reservations", slow_read)
    first = env.executor.recover()
    second = env.executor.recover()
    assert second == first  # Existing intention identities and clocks remain idempotent.
    assert len(first) == (0 if history == "empty" else 1)
    assert tuple(asdict(row) for row in native(env).snapshot().reservations) == before
    assert env.store.forecasts() == originals
    assert len(env.broker.submissions) == submissions and not env.broker.cancellations
    saved = observations(env, "recover")[previous:]
    assert len(saved) == 2
    for cycle, symbol, value in saved:
        assert cycle == value["session_id"] == SESSION and symbol is None
        assert value["actor"] == "Atlas" and value["operation_returned_normally"] is True
        diagnostic = value["diagnostics"]
        assert diagnostic["execution_stage"] == "operation_complete"
        assert diagnostic["execution_elapsed_seconds"] >= 75
        assert next(p for p in diagnostic["execution_phases"]
                    if p["stage"] == "recovery_native_read")["elapsed_seconds"] == 75
    assert not any("diagnostics" in row["evidence"] for row in first)
    assert env.executor._execution_trace is env.executor._lease_keeper is None


def test_expired_normal_step_keeps_exact_intention_and_has_own_timing(env, monkeypatch):
    bind_session(env)
    forecast = env.forecast()
    env.now = pd.Timestamp(forecast["target_close"]) + pd.Timedelta(seconds=1)
    ticks = [0.]
    monkeypatch.setattr(execution.time, "monotonic", lambda: ticks[0])
    original = env.executor.intents.processed_at_or_after

    def slow_chronology(value):
        ticks[0] += 42
        return original(value)

    monkeypatch.setattr(env.executor.intents, "processed_at_or_after", slow_chronology)
    first = env.executor.step(forecast)
    second = env.executor.step(forecast)
    assert first == second and first["disposition"] == "fixed_target_already_elapsed"
    assert env.store.forecasts() == [forecast]
    assert env.captures == 0 and not env.broker.submissions and not env.broker.cancellations
    saved = observations(env, "step")
    assert len(saved) == 2
    for cycle, symbol, value in saved:
        assert (cycle, symbol) == (forecast["cycle_id"], forecast["symbol"])
        assert value["forecast_id"] == forecast["forecast_id"] and value["session_id"] == SESSION
        assert value["diagnostics"]["execution_elapsed_seconds"] == 42
        assert "diagnostics" not in first["evidence"]


def test_normal_trace_includes_cleanup_and_lock_exit_before_diagnostic_write(env, monkeypatch):
    bind_session(env)
    ticks, locked = [0.], [False]
    monkeypatch.setattr(execution.time, "monotonic", lambda: ticks[0])
    original_lock = execution.exclusive_runtime_lock
    original_stop = execution._LeaseKeeper.stop
    original_release = execution.AccountAuthority.release_lease
    original_write = env.executor._write_operation_diagnostics

    @contextmanager
    def lock(*args, **kwargs):
        with original_lock(*args, **kwargs):
            locked[0] = True
            yield
        locked[0] = False
        ticks[0] += 7

    def stop(keeper):
        original_stop(keeper)
        ticks[0] += 18

    def release(authority, token):
        original_release(authority, token)
        ticks[0] += 11

    def write(*args, **kwargs):
        assert not locked[0]
        assert env.executor._execution_trace is env.executor._lease_keeper is None
        original_write(*args, **kwargs)

    monkeypatch.setattr(execution, "exclusive_runtime_lock", lock)
    monkeypatch.setattr(execution._LeaseKeeper, "stop", stop)
    monkeypatch.setattr(execution.AccountAuthority, "release_lease", release)
    monkeypatch.setattr(env.executor, "_write_operation_diagnostics", write)
    assert env.executor.recover() == []
    diagnostic = observations(env)[0][2]["diagnostics"]
    phases = {item["stage"]: item["elapsed_seconds"] for item in diagnostic["execution_phases"]}
    assert diagnostic["execution_elapsed_seconds"] == 36
    assert phases["lease_keeper_stop"] == 18 and phases["lease_release"] == 11
    assert phases["execution_lock_release"] == 7


@pytest.mark.parametrize("failure", ["write", "snapshot", "record_clock"])
def test_diagnostic_failure_does_not_change_success_or_leave_trace_state(env, monkeypatch, failure):
    bind_session(env)
    def unavailable(*args, **kwargs):
        raise RuntimeError("diagnostic-only fixture")
    if failure == "write":
        monkeypatch.setattr(env.executor, "_write_operation_diagnostics", unavailable)
    elif failure == "snapshot":
        monkeypatch.setattr(execution._ExecutionTrace, "snapshot", unavailable)
    else:
        original_stop = execution._LeaseKeeper.stop
        def stop(keeper):
            original_stop(keeper)
            # All operational clock reads have finished; only the new event
            # timestamp fails. A diagnostics clock must not become a gate.
            monkeypatch.setattr(env.executor, "clock", unavailable)
        monkeypatch.setattr(execution._LeaseKeeper, "stop", stop)
        original_release = execution.AccountAuthority.release_lease
        def release(authority, token):
            # Authority owns the same clock; release it before injecting the
            # event-only failure so this remains a successful operation.
            monkeypatch.setattr(env.executor, "clock", env.clock)
            result = original_release(authority, token)
            monkeypatch.setattr(env.executor, "clock", unavailable)
            return result
        monkeypatch.setattr(execution.AccountAuthority, "release_lease", release)
    assert env.executor.recover() == []
    assert observations(env) == []
    assert env.executor._execution_trace is env.executor._lease_keeper is None
    assert not env.broker.submissions and not env.broker.cancellations


def test_diagnostics_do_not_wait_for_competing_shadow_writer(env):
    bind_session(env)
    # Recovery owns separate execution ledgers and no intentions in this case;
    # only the optional new telemetry competes with this held shadow writer.
    connection = sqlite3.connect(env.store.path, timeout=0)
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert env.executor.recover() == []
    finally:
        connection.rollback()
        connection.close()
    assert observations(env) == []
    assert env.executor.recover() == [] and len(observations(env)) == 1


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "bad", RuntimeError("clock fixture")])
def test_unavailable_monotonic_diagnostics_do_not_change_recovery(env, monkeypatch, value):
    bind_session(env)
    def clock():
        if isinstance(value, Exception):
            raise value
        return value
    # Scope the failed diagnostic clock to this module. Authority lease clocks
    # remain valid operational inputs and must not be changed by this fixture.
    monkeypatch.setattr(execution, "time", SimpleNamespace(monotonic=clock))
    assert env.executor.recover() == []
    diagnostic = observations(env)[0][2]["diagnostics"]
    assert diagnostic["execution_diagnostics_unavailable"] is True
    assert diagnostic["execution_elapsed_seconds"] is None
    assert env.executor._execution_trace is env.executor._lease_keeper is None


def test_manual_stop_and_real_exception_keep_original_failure_and_no_normal_event(env):
    bind_session(env)
    env.active = False
    with pytest.raises(ContractError, match="Manual 15m") as stopped:
        env.executor.recover()
    saved = execution.execution_failure_evidence(stopped.value)
    assert saved["execution_stage"] == "local_binding"
    assert observations(env) == [] and not env.broker.submissions
    env.active = True
    error = RuntimeError("original capture fixture")
    env.capture_fail = error
    with pytest.raises(RuntimeError) as captured:
        env.executor.recover()
    assert captured.value is error
    assert execution.execution_failure_evidence(error)["execution_stage"] == "portfolio_capture"
    assert observations(env) == [] and not env.broker.submissions and not env.broker.cancellations
    assert env.executor._execution_trace is env.executor._lease_keeper is None


def test_normal_payload_is_detached_bounded_and_excludes_account_values(env, monkeypatch):
    bind_session(env)
    original = env.executor._owned_native_reservations
    ticks = [0.]
    traces = []
    monkeypatch.setattr(execution.time, "monotonic", lambda: ticks[0])
    def many_phases(db):
        traces.append(env.executor._execution_trace)
        for _ in range(50):
            ticks[0] += 1
            env.executor._execution_stage("recovery_native_read")
        return original(db)
    monkeypatch.setattr(env.executor, "_owned_native_reservations", many_phases)
    assert env.executor.recover() == []
    payload = observations(env)[0][2]
    diagnostic = payload["diagnostics"]
    assert len(diagnostic["execution_phases"]) == 32 and diagnostic["execution_elapsed_seconds"] == 50
    text = json.dumps(payload)
    assert env.executor.lease.token not in text and env.config.account_fingerprint not in text
    assert all(secret not in text for secret in ("account_equity", "available_cash", "limit_price", "quote", "held_shares"))
    traces[0].phases[-1]["elapsed_seconds"] = 999
    assert observations(env)[0][2] == payload


@pytest.mark.parametrize("session", [None, "secret-private-input", "manual-scout-15m:" + "a" * 32])
def test_unbound_or_foreign_session_does_not_emit_ambiguous_recovery(env, session):
    env.executor.diagnostic_session_id = session
    assert env.executor.recover() == []
    assert observations(env) == []


def test_manual_orchestration_binds_exact_generated_session_without_live_transport(tmp_path, monkeypatch, offline_databento_sdk):
    from types import SimpleNamespace
    from ml.intraday import live_session, local_execution, data
    from app.services import schwab, databento_market_data
    import dotenv
    now = [instant("2026-10-08T17:17:00Z")]
    constructed = []
    monkeypatch.setattr(live_session, "local_symbols", lambda *args: ("Atlas", ("AAPL",)))
    monkeypatch.setattr(local_execution, "load_local_binding", lambda *args: SimpleNamespace(actor="Atlas", symbols=("AAPL",)))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(data, "load_local_history", lambda *args, **kwargs: object())
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider", lambda: SimpleNamespace(dataset="EQUS.MINI"))
    monkeypatch.setattr(schwab, "SchwabSession", lambda: object())
    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
        def is_alive(self):
            return False
        def join(self, **kwargs):
            pass
    monkeypatch.setattr(live_session.threading, "Thread", InlineThread)
    monkeypatch.setattr(live_session, "forecasting_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(live_session, "scoring_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(live_session, "collection_loop", lambda *args, **kwargs: None)
    def executor(**kwargs):
        constructed.append(kwargs)
        return SimpleNamespace(step=lambda *args: pytest.fail("No forecast/order should exist"), recover=lambda: [])
    monkeypatch.setattr(execution, "IntradayAccountExecutor", executor)
    def sleep(seconds):
        raise KeyboardInterrupt()
    result = live_session.run_manual(repository=tmp_path, datastore=tmp_path, profile=tmp_path / "profile.json",
        watchlist=tmp_path / "watchlist.txt", config=live_session.ResearchConfig(), clock=lambda: now[0], sleep=sleep)
    assert result["status"] == "STOPPED" and len(constructed) == 1
    assert constructed[0]["diagnostic_session_id"] == result["session_id"]
    assert result["session_id"].startswith("manual-atlas-15m:")
