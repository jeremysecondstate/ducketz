"""Local execution timing cannot change manual controls or order behavior."""
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import dataset_diagnostics as diagnostics
from ml.intraday import scout_local_execution as execution
from ml.intraday.prediction import ContractError, instant
from test_intraday_scout_local_execution import env


def profiles(env):
    with env.store.connect() as db:
        rows = db.execute("SELECT recorded_at,payload FROM events WHERE cycle_id=? AND kind=? ORDER BY rowid",
            (env.executor.session_id, "local_execution_profile")).fetchall()
    return [(at, json.loads(raw)) for at, raw in rows]


def test_profile_measures_phases_with_own_thread_cpu_and_separate_completion_clock(env, monkeypatch):
    ticks = iter(range(1000))
    def clocks():
        value = next(ticks)
        return value * 2., value * 1., value * 3.
    monkeypatch.setattr(diagnostics, "_clocks", clocks)
    completion = instant("2026-10-22T14:20:00Z")
    monkeypatch.setattr(execution, "utcnow", lambda: completion)
    result = env.executor.step(env.forecast)
    assert result["disposition"] == "submitted"
    assert env.captures == 2 and len(env.broker.posts) == 1
    (at, profile), = profiles(env)
    assert at == completion.isoformat() and at != env.state.now.isoformat()
    assert profile["schema_version"] == "intraday-local-execution-profile-v1"
    assert profile["status"] == "success" and profile["cpu_scope"] == "current_thread"
    assert profile["forecast_id"] == env.forecast["forecast_id"]
    phases = {row["phase"] for row in profile["phases"]}
    assert {"total_step", "snapshot_validation", "chronology_check", "initial_capture",
        "snapshot_load", "sizing", "save_sizing", "own_journal_recovery", "second_capture",
        "capacity", "authorization_validation", "submission", "disposition_persist"} <= phases
    assert len(profile["phases"]) <= 32
    for row in profile["phases"]:
        assert row["wall_seconds"] >= 0 and row["thread_cpu_seconds"] >= 0
        assert row["wall_seconds"] == row["thread_cpu_seconds"] * 2
    raw = json.dumps(profile)
    assert env.executor.account_fingerprint not in raw
    assert "desired_quantity" not in raw and "broker_order_id" not in raw


def test_profile_is_persisted_after_account_context_exits(env, monkeypatch):
    active = [False]
    original_account = env.executor._account
    original_record = env.executor._record_execution_profile
    @contextmanager
    def account(**kwargs):
        with original_account(**kwargs):
            active[0] = True
            try:
                yield
            finally:
                active[0] = False
    def record(forecast, profile):
        assert active[0] is False
        original_record(forecast, profile)
    monkeypatch.setattr(env.executor, "_account", account)
    monkeypatch.setattr(env.executor, "_record_execution_profile", record)
    assert env.executor.step(env.forecast)["disposition"] == "submitted"
    assert len(profiles(env)) == 1


def test_early_expiry_profile_does_not_add_trader_clock_reads(env, monkeypatch):
    env.state.now = instant(env.forecast["target_close"])
    original_clock = env.executor.clock
    reads = []
    def clock():
        reads.append(True)
        return original_clock()
    monkeypatch.setattr(env.executor, "clock", clock)
    baseline = env.executor._step(env.forecast)
    expected_reads = len(reads)
    reads.clear()
    actual = env.executor.step(env.forecast)
    assert actual == baseline
    assert len(reads) == expected_reads
    assert actual["disposition"] == "fixed_target_already_elapsed"
    assert env.captures == 0 and env.broker.posts == []
    assert profiles(env)[0][1]["status"] == "success"


def test_exception_profile_preserves_exact_original_exception(env, monkeypatch):
    original = ContractError("fixture exact operation failed")
    def failed(forecast):
        raise original
    monkeypatch.setattr(env.executor, "_step", failed)
    with pytest.raises(ContractError) as raised:
        env.executor.step(env.forecast)
    assert raised.value is original
    (_, profile), = profiles(env)
    assert profile["status"] == "error" and profile["error_type"] == "ContractError"
    assert profile["phases"][-1]["status"] == "error"
    assert env.captures == 0 and env.broker.posts == []


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("database is locked"), OSError("fixture unavailable")])
def test_diagnostic_sink_is_nonblocking_and_cannot_change_result(env, monkeypatch, failure):
    calls = []
    def connect(path, **kwargs):
        calls.append((path, kwargs))
        assert path.endswith("?mode=rw") and kwargs == {"uri": True, "timeout": 0}
        raise failure
    monkeypatch.setattr(execution, "sqlite3", SimpleNamespace(connect=connect))
    assert env.executor.step(env.forecast)["disposition"] == "submitted"
    assert len(calls) == 1 and profiles(env) == []
    assert env.captures == 2 and len(env.broker.posts) == 1


def test_diagnostic_failure_does_not_replace_pending_original_exception(env, monkeypatch):
    original = ContractError("fixture original failure")
    def failed(forecast):
        raise original
    def connect(*args, **kwargs):
        raise KeyboardInterrupt("fixture diagnostic interruption")
    monkeypatch.setattr(env.executor, "_step", failed)
    monkeypatch.setattr(execution, "sqlite3", SimpleNamespace(connect=connect))
    with pytest.raises(ContractError) as raised:
        env.executor.step(env.forecast)
    assert raised.value is original and env.broker.posts == []


def test_clock_observer_failure_preserves_execution(env, monkeypatch):
    def failed():
        raise ValueError("fixture diagnostic timer unavailable")
    monkeypatch.setattr(diagnostics, "_clocks", failed)
    assert env.executor.step(env.forecast)["disposition"] == "submitted"
    (_, profile), = profiles(env)
    assert profile["status"] == "success"
    assert profile["observer_errors"] and profile["phases"] == []
    assert env.captures == 2 and len(env.broker.posts) == 1


def test_manual_stop_is_never_bypassed_for_profile(env):
    env.state.stop = True
    with pytest.raises(ContractError, match="Current manual Scout session authority required"):
        env.executor.step(env.forecast)
    assert env.captures == 0 and env.broker.posts == []
    assert profiles(env)[0][1]["status"] == "error"


@pytest.mark.parametrize("malformed", [{}, None])
def test_optional_profile_metadata_cannot_replace_manual_control_error(env, malformed):
    env.state.stop = True
    with pytest.raises(ContractError, match="Current manual Scout session authority required"):
        env.executor.step(malformed)
    assert env.captures == 0 and env.broker.posts == []
