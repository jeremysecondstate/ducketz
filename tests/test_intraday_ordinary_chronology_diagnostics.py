"""Temporary own ledgers and injected clocks; no transport or trading calls."""
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from test_intraday_main_account_execution import env, native
from ml.intraday import ordinary_diagnostics as diagnostics
from ml.intraday import account_execution as execution
from ml.intraday.execution_intentions import IntentionStore
from ml.intraday.prediction import ContractError, encoded


SESSION = "manual-atlas-15m:" + "a" * 32


def rows(state):
    with state.store.connect() as db:
        return [json.loads(raw) for raw, in db.execute(
            "SELECT payload FROM events WHERE kind='execution_operation_diagnostics' ORDER BY id")]


def expired(state):
    state.executor.diagnostic_session_id = SESSION
    forecast = state.forecast()
    state.now = pd.Timestamp(forecast["target_close"]) + pd.Timedelta(seconds=1)
    return forecast


def clocks(monkeypatch):
    ticks = [0., 0.]
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(
        monotonic=lambda: ticks[0], thread_time=lambda: ticks[1]))
    return ticks


@pytest.mark.parametrize("owner,name,phase", [
    ("executor", "_newer_forecasts", "newer_forecast_scan"),
    ("intentions", "processed_at_or_after", "processed_scan"),
    ("executor", "_renew_current_lease", "fence_renewal"),
    ("store", "_forecast_value", "preflight_payload_validate"),
    ("intentions", "_ordinary_forecast_marker", "preflight_marker"),
    ("intentions", "_revision_sizing", "sizing_read"),
    ("intentions", "_append_validated", "projection_append"),
])
def test_natural_expired_path_attributes_slow_subcall_without_changing_receipt(env, monkeypatch, owner, name, phase):
    forecast = expired(env)
    ticks = clocks(monkeypatch)
    target = {"executor": env.executor, "intentions": env.executor.intents, "store": env.store}[owner]
    original = getattr(target, name)
    def slow(*args, **kwargs):
        ticks[0] += 17
        ticks[1] += 3
        return original(*args, **kwargs)
    monkeypatch.setattr(target, name, slow)
    first = env.executor.step(forecast)
    assert first == env.executor.step(forecast)
    assert first["disposition"] == "fixed_target_already_elapsed"
    assert first["submitted_quantity"] == first["filled_quantity"] == 0
    assert env.captures == 0 and not env.broker.submissions and not env.broker.cancellations
    assert native(env).snapshot().reservations == ()
    assert env.store.forecasts() == [forecast]
    saved = rows(env)
    assert len(saved) == 2
    for row in saved:
        assert row["operation_returned_normally"] and row["session_id"] == SESSION
        details = row["diagnostics"]["ordinary_intention_details"]
        value = details["phases"][phase]
        assert value == dict(count=1, total_seconds=17., maximum_seconds=17.,
            thread_cpu_count=1, thread_cpu_total_seconds=3., thread_cpu_maximum_seconds=3.)
        assert details["schema_version"] == "intraday-ordinary-intention-details-v1"
        assert details["totals_are_inclusive"] is True
        assert not details["elapsed_unavailable"] and not details["thread_cpu_unavailable"]
        assert details["workload"]["forecast_payload_reads"] == 1
        serialized = json.dumps(details)
        assert forecast["forecast_id"] not in serialized and str(env.root) not in serialized
    assert "ordinary_intention_details" not in json.dumps(first)


def test_context_entry_exit_and_writer_lock_are_measured_separately(env, monkeypatch):
    forecast = expired(env)
    ticks = clocks(monkeypatch)
    connect = env.store.connect
    class Connection:
        def __init__(self, db): self.db = db
        def execute(self, sql, *args):
            if sql == "BEGIN IMMEDIATE":
                ticks[0] += 11
            return self.db.execute(sql, *args)
        def __getattr__(self, name): return getattr(self.db, name)
    @contextmanager
    def delayed_connection():
        ticks[0] += 2
        with connect() as db:
            yield Connection(db)
        ticks[0] += 5
    monkeypatch.setattr(env.store, "connect", delayed_connection)
    result = env.executor.step(forecast)
    assert result["disposition"] == "fixed_target_already_elapsed"
    phases = rows(env)[0]["diagnostics"]["ordinary_intention_details"]["phases"]
    assert phases["preflight_connection_open"]["total_seconds"] == 2
    assert phases["preflight_connection_close"]["total_seconds"] == 5
    assert phases["writer_connection_open"]["total_seconds"] == 2
    assert phases["writer_lock"]["total_seconds"] == 11
    assert phases["writer_commit_close"]["total_seconds"] == 5
    assert not env.broker.submissions


def test_failed_append_keeps_exact_error_rolls_back_and_clears_capture(env, monkeypatch):
    forecast = expired(env)
    failure = RuntimeError("offline-original-append-failure")
    previous = getattr(env.executor.intents, "_recovery_row_highwater", None)
    def fail(*args, **kwargs): raise failure
    monkeypatch.setattr(env.executor.intents, "_append_validated", fail)
    with pytest.raises(RuntimeError) as caught:
        env.executor.step(forecast)
    assert caught.value is failure
    assert env.executor.intents.history(forecast["forecast_id"]) == []
    assert getattr(env.executor.intents, "_recovery_row_highwater", None) == previous
    assert env.store.forecasts() == [forecast] and not env.broker.submissions
    assert diagnostics._CURRENT.get() is None
    evidence = execution.execution_failure_evidence(failure)
    assert "projection_append" in evidence["ordinary_intention_details"]["phases"]


@pytest.mark.parametrize("clock_name", ["monotonic", "thread_time"])
def test_diagnostic_clock_failure_preserves_normal_result_and_original_exception(env, monkeypatch, clock_name):
    forecast = expired(env)
    def unavailable(): raise RuntimeError("diagnostic clock only")
    clock = SimpleNamespace(monotonic=lambda: 1., thread_time=lambda: 1.)
    setattr(clock, clock_name, unavailable)
    monkeypatch.setattr(diagnostics, "time", clock)
    assert env.executor.step(forecast)["disposition"] == "fixed_target_already_elapsed"
    details = rows(env)[0]["diagnostics"]["ordinary_intention_details"]
    assert details["elapsed_unavailable" if clock_name == "monotonic" else "thread_cpu_unavailable"]
    failure = ContractError("Original forecast validation fixture")
    monkeypatch.setattr(env.executor.intents, "_ordinary_forecast_preflight", lambda *_: (_ for _ in ()).throw(failure))
    with pytest.raises(ContractError) as caught:
        env.executor.step(forecast)
    assert caught.value is failure and diagnostics._CURRENT.get() is None


@pytest.mark.parametrize("change", ["caller", "stored", "between_snapshots"])
def test_diagnostics_preserve_full_original_caller_and_concurrent_recheck(env, monkeypatch, change):
    forecast = expired(env)
    original = env.executor.intents._ordinary_forecast_preflight
    altered = forecast | {"retained_original_extra": {"private_fixture": "changed"}}
    if change == "caller":
        forecast = altered
    elif change == "stored":
        with env.store.connect() as db:
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (encoded(altered), forecast["forecast_id"]))
    else:
        def changed(value):
            result = original(value)
            with env.store.connect() as db:
                db.execute("UPDATE forecasts SET payload=? WHERE id=?", (encoded(altered), forecast["forecast_id"]))
            return result
        monkeypatch.setattr(env.executor.intents, "_ordinary_forecast_preflight", changed)
    with pytest.raises(ContractError):
        env.executor.step(forecast)
    assert env.executor.intents.history(forecast["forecast_id"]) == []
    assert not env.broker.submissions and diagnostics._CURRENT.get() is None


@pytest.mark.parametrize("suppresses", [False, True])
def test_timed_context_keeps_exception_identity_and_suppression(suppresses):
    failure = RuntimeError("original")
    seen = []
    class Manager:
        def __enter__(self): return "original-value"
        def __exit__(self, *args):
            seen.append(args)
            return suppresses
    def work():
        with diagnostics.context(Manager(), "writer_connection_open", "writer_commit_close") as value:
            assert value == "original-value"
            raise failure
    if suppresses:
        work()
    else:
        with pytest.raises(RuntimeError) as caught: work()
        assert caught.value is failure
    assert seen[0][0] is RuntimeError and seen[0][1] is failure


def test_fixed_schema_aggregate_is_bounded_and_has_no_caller_values():
    timings = diagnostics.OrdinaryTimings()
    with diagnostics.capture(timings):
        for _ in range(50):
            with diagnostics.phase("writer_lock"): pass
        with diagnostics.phase("secret-account-value"): pass
        diagnostics.count("secret-forecast-id", 1)
    value = timings.snapshot()
    assert list(value["phases"]) == ["writer_lock"]
    assert value["phases"]["writer_lock"]["count"] == 50
    assert value["elapsed_unavailable"] and "secret" not in json.dumps(value)


def test_optional_marker_uncertainty_still_fully_validates_writer_original(env, monkeypatch):
    forecast = expired(env)
    monkeypatch.setattr(env.executor.intents, "_ordinary_forecast_marker", lambda *_: None)
    assert env.executor.step(forecast)["disposition"] == "fixed_target_already_elapsed"
    details = rows(env)[0]["diagnostics"]["ordinary_intention_details"]
    assert details["workload"]["forecast_payload_reads"] == 2
    assert details["phases"]["writer_payload_validate"]["count"] == 1
    assert details["phases"]["writer_caller_compare"]["count"] == 1
    assert env.store.forecasts() == [forecast] and not env.broker.submissions


def test_cache_lock_acquisition_is_separate_from_validating_while_held(env, monkeypatch):
    forecast = expired(env)
    ticks = clocks(monkeypatch)
    lock = env.store._execution_forecast_lock
    class DelayedLock:
        def __enter__(self):
            value = lock.__enter__()
            ticks[0] += 13
            return value
        def __exit__(self, *args): return lock.__exit__(*args)
    monkeypatch.setattr(env.store, "_execution_forecast_lock", DelayedLock())
    assert env.executor.step(forecast)["disposition"] == "fixed_target_already_elapsed"
    phases = rows(env)[0]["diagnostics"]["ordinary_intention_details"]["phases"]
    assert phases["record_cache_lock"]["total_seconds"] == 13
    assert phases["preflight_payload_validate"]["total_seconds"] == 0
    assert phases["writer_commit_close"]["total_seconds"] == 0


def test_call_local_capture_does_not_inherit_into_another_thread():
    timings = diagnostics.OrdinaryTimings()
    def unrelated():
        assert diagnostics._CURRENT.get() is None
        with diagnostics.phase("writer_lock"): pass
    with diagnostics.capture(timings):
        thread = threading.Thread(target=unrelated)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert timings.snapshot()["phases"] == {} and diagnostics._CURRENT.get() is None
