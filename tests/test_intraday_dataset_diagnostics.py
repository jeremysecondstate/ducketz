"""Offline evidence: diagnostics retain failures without changing fit inputs."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import numpy as np
import pandas as pd
import pytest

from ml.intraday import dataset_diagnostics as diagnostics
from ml.intraday import live_model_recipe as recipes
from ml.intraday import market_collection, market_context, market_context_cache, native_interval
from ml.intraday.prediction import digest
from ml.intraday.runtime import ResearchConfig, ShadowRunner, _symbol_exception_diagnostics
from ml.intraday.store import ShadowStore
from tests.test_intraday_market_context import CUTOFF, example_dataset, sealed
from tests.test_intraday_market_recipe import candidate, frame, save
from tests.test_intraday_runtime import REF, dataset, history, trained


class FixtureFailure(RuntimeError):
    pass


def assert_same_dataset(left, right):
    for name in ("X", "y", "inference_X"):
        first, second = np.asarray(getattr(left, name)), np.asarray(getattr(right, name))
        assert first.dtype == second.dtype and first.shape == second.shape
        assert first.tobytes() == second.tobytes()
    assert left.feature_names == right.feature_names
    for name in ("reference_at", "target_close_at", "label_available_at"):
        assert getattr(left, name).equals(getattr(right, name))
    assert left.inference == right.inference
    assert left.manifest == right.manifest


def augment(cache_path=None):
    base = example_dataset()
    decisions = {reference: reference + pd.Timedelta(minutes=15, seconds=10)
                 for reference in base.reference_at}
    return market_context.augment_dataset(base, [sealed()],
        training_decision_at=decisions, inference_cutoff=CUTOFF, cache_path=cache_path)


def test_uncaptured_instrumentation_never_reads_clocks_or_cache(monkeypatch):
    def forbidden():
        raise AssertionError("An uncaptured operation must not read clocks")
    monkeypatch.setattr(diagnostics, "_clocks", forbidden)
    with diagnostics.dataset_phase("unused"):
        pass
    diagnostics.record_context_cache(object())


def test_failed_phase_retains_times_and_original_exception_without_private_payload(monkeypatch):
    clocks = iter([(10.0, 2.0, 5.0), (12.5, 2.75, 9.0)])
    monkeypatch.setattr(diagnostics, "_clocks", lambda: next(clocks))
    original = FixtureFailure("private fixture payload must not enter profiling")
    with pytest.raises(FixtureFailure) as caught:
        with diagnostics.capture_dataset_build() as profile:
            with diagnostics.dataset_phase("base_dataset"):
                raise original
    assert caught.value is original
    assert profile["status"] == "error" and profile["cpu_scope"] == "current_thread"
    assert profile["phases"] == [{"phase": "base_dataset", "status": "error",
        "wall_seconds": 2.5, "thread_cpu_seconds": .75, "process_cpu_seconds": 4.0,
        "error_type": "FixtureFailure"}]
    assert "private fixture" not in json.dumps(profile)


@pytest.mark.parametrize("failure_call", [1, 2])
def test_observer_clock_failure_preserves_body_result_and_exception(monkeypatch, failure_call):
    calls = 0
    def clock():
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise ValueError("private timer detail")
        return 1.0, 1.0, 1.0
    monkeypatch.setattr(diagnostics, "_clocks", clock)
    original = FixtureFailure("original")
    with pytest.raises(FixtureFailure) as caught:
        with diagnostics.capture_dataset_build() as profile:
            with diagnostics.dataset_phase("base_dataset"):
                raise original
    assert caught.value is original
    assert profile["observer_errors"] == ["ValueError"]
    assert profile["phases"] == []
    with diagnostics.capture_dataset_build() as success:
        with diagnostics.dataset_phase("base_dataset"):
            result = 42
    assert result == 42 and success["status"] == "success"


def test_nested_and_concurrent_captures_restore_and_isolate_state():
    barrier = threading.Barrier(2)
    def build(name):
        with diagnostics.capture_dataset_build() as profile:
            barrier.wait(timeout=10)
            with diagnostics.dataset_phase(name):
                sum(range(100))
        return profile
    with diagnostics.capture_dataset_build() as outer:
        with diagnostics.dataset_phase("before"):
            pass
        with diagnostics.capture_dataset_build() as inner:
            with diagnostics.dataset_phase("nested"):
                pass
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(build, name) for name in ("first", "second")]
            concurrent = [future.result(timeout=10) for future in futures]
        with diagnostics.dataset_phase("after"):
            pass
    assert [p["phase"] for p in outer["phases"]] == ["before", "after"]
    assert [p["phase"] for p in inner["phases"]] == ["nested"]
    assert [[p["phase"] for p in result["phases"]] for result in concurrent] == [["first"], ["second"]]
    assert diagnostics._current.get() is None


def test_profile_records_are_bounded_and_bad_counter_does_not_escape():
    with diagnostics.capture_dataset_build() as profile:
        for _ in range(40):
            with diagnostics.dataset_phase("base_dataset"):
                pass
            diagnostics.record_context_cache(None)
            diagnostics.record_context_cache(object())
    assert len(profile["phases"]) == 32
    assert len(profile["context_caches"]) == 4
    assert len(profile["observer_errors"]) == 8


@pytest.mark.parametrize("has_original_error", [False, True])
def test_cache_observer_control_interrupt_distinguishes_existing_failure(has_original_error):
    interrupt = SystemExit("fixture observer interrupt")
    original = FixtureFailure("fixture original failure")
    class InterruptedCounter:
        @property
        def hits(self):
            raise interrupt
    with pytest.raises(FixtureFailure if has_original_error else SystemExit) as caught:
        with diagnostics.capture_dataset_build() as profile:
            try:
                if has_original_error:
                    raise original
            finally:
                diagnostics.record_context_cache(InterruptedCounter())
    assert caught.value is (original if has_original_error else interrupt)
    assert profile["observer_errors"] == (["SystemExit"] if has_original_error else [])


def test_new_control_interrupt_during_phase_entry_propagates(monkeypatch):
    interrupt = KeyboardInterrupt("fixture phase entry interrupted")
    def clock():
        raise interrupt
    monkeypatch.setattr(diagnostics, "_clocks", clock)
    with pytest.raises(KeyboardInterrupt) as caught:
        with diagnostics.capture_dataset_build() as profile:
            with diagnostics.dataset_phase("base_dataset"):
                pytest.fail("The manual interrupt must precede the operation")
    assert caught.value is interrupt and profile["error_type"] == "KeyboardInterrupt"
    assert diagnostics._current.get() is None


def test_cold_warm_cache_and_uncaptured_dataset_inputs_are_identical(tmp_path):
    uncached = augment()
    cache_path = tmp_path / "contexts.sqlite3"
    with diagnostics.capture_dataset_build() as cold:
        fresh = augment(cache_path)
    with diagnostics.capture_dataset_build() as warm:
        cached = augment(cache_path)
    assert_same_dataset(uncached, fresh)
    assert_same_dataset(uncached, cached)
    count = len(uncached.y) + 1
    assert cold["context_caches"] == [{"enabled": True, "hits": 0, "misses": count,
        "invalid_entries": 0, "read_skips": 0, "write_skips": 0}]
    assert warm["context_caches"] == [{"enabled": True, "hits": count, "misses": 0,
        "invalid_entries": 0, "read_skips": 0, "write_skips": 0}]
    assert "diagnostics" not in json.dumps(fresh.manifest)


def test_cache_counters_survive_context_failure_and_include_exit_skips(tmp_path, monkeypatch):
    original = FixtureFailure("private context cause")
    class FailingCache:
        def __init__(self, path):
            self.hits = self.misses = self.invalid_entries = self.read_skips = self.write_skips = 0
        def __enter__(self):
            return self
        def build(self, *args, **kwargs):
            self.misses += 1
            raise original
        def __exit__(self, *args):
            self.write_skips += 1
    monkeypatch.setattr(market_context_cache, "ContextCache", FailingCache)
    with pytest.raises(FixtureFailure) as caught:
        with diagnostics.capture_dataset_build() as profile:
            augment(tmp_path / "unused.sqlite3")
    assert caught.value is original
    assert profile["context_caches"] == [{"enabled": True, "hits": 0, "misses": 1,
        "invalid_entries": 0, "read_skips": 0, "write_skips": 1}]


@pytest.fixture(scope="module")
def native_frame():
    return frame()


def recipe_builder(tmp_path, data):
    save(tmp_path, candidate())
    _, adapters = recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path,
        pc="Scout", symbols=("TEST",))
    reference = pd.Timestamp(data.opened_at.iloc[-1])
    return adapters["dataset_builder"], dict(cutoff=reference + pd.Timedelta(minutes=16),
        reference_at=reference, lookback_days=None, synthetic_policy="exclude")


def test_recipe_phases_preserve_complete_frozen_dataset_and_recipe(tmp_path, native_frame):
    builder, kwargs = recipe_builder(tmp_path, native_frame)
    baseline = builder(native_frame, **kwargs)
    with diagnostics.capture_dataset_build() as profile:
        captured = builder(native_frame, **kwargs)
    assert_same_dataset(baseline, captured)
    assert [row["phase"] for row in profile["phases"]] == ["base_dataset",
        "assessment_reservation", "causal_filter_and_window", "training_support",
        "context_snapshot_load", "context_augmentation"]
    assert all(row["status"] == "success" and row["wall_seconds"] >= 0
               and row["thread_cpu_seconds"] >= 0 for row in profile["phases"])
    assert captured.manifest["manual_model_recipe"] == candidate()


@pytest.mark.parametrize("phase", ["base_dataset", "assessment_reservation",
    "causal_filter_and_window", "training_support", "context_snapshot_load", "context_augmentation"])
def test_recipe_failure_retains_affected_phase_and_original_cause(tmp_path, native_frame, monkeypatch, phase):
    from ml.intraday import assessment_reservation
    builder, kwargs = recipe_builder(tmp_path, native_frame)
    original = FixtureFailure("private original input")
    def fail(*args, **kwargs):
        raise original
    targets = {"base_dataset": (native_interval, "build_native_interval_dataset"),
        "assessment_reservation": (assessment_reservation, "protect_dataset"),
        "causal_filter_and_window": (recipes, "instant"),
        "training_support": (recipes, "_training_support"),
        "context_snapshot_load": (market_collection, "load_snapshots"),
        "context_augmentation": (market_context, "augment_dataset")}
    monkeypatch.setattr(*targets[phase], fail)
    with pytest.raises(FixtureFailure) as caught:
        with diagnostics.capture_dataset_build() as profile:
            builder(native_frame, **kwargs)
    assert caught.value is original and profile["status"] == "error"
    assert profile["phases"][-1]["phase"] == phase
    assert profile["phases"][-1]["status"] == "error"
    assert "private original input" not in json.dumps(profile)


def runtime_runner(tmp_path, builder):
    return ShadowRunner(store=ShadowStore(tmp_path / "shadow.sqlite3"), pc="Scout", symbols=("TEST",),
        config=ResearchConfig(models=("logistic",)), dataset_builder=builder, trainer=trained,
        clock=lambda: (REF + pd.Timedelta(minutes=16)).isoformat())


@pytest.mark.parametrize("event_write_fails", [False, True, "base_exception"])
def test_runtime_retains_partial_failure_even_when_new_event_write_fails(tmp_path, monkeypatch, event_write_fails):
    original = FixtureFailure("private operation cause")
    def builder(*args, **kwargs):
        with diagnostics.dataset_phase("base_dataset"):
            raise original
    runner = runtime_runner(tmp_path, builder)
    real_event = runner.store.event
    def event(cycle_id, kind, payload, **kwargs):
        if kind == "dataset_build_profile" and event_write_fails:
            if event_write_fails == "base_exception":
                raise KeyboardInterrupt("fixture diagnostic interrupt")
            raise OSError("fixture event persistence failed")
        return real_event(cycle_id, kind, payload, **kwargs)
    monkeypatch.setattr(runner.store, "event", event)
    with pytest.raises(FixtureFailure) as caught:
        runner._symbol("fixture-cycle", "TEST", history("TEST"), REF,
            REF + pd.Timedelta(minutes=16), "live", None, None, time.perf_counter())
    assert caught.value is original
    profile = _symbol_exception_diagnostics(original)["dataset_build_profile"]
    assert profile["status"] == "error"
    assert [row["phase"] for row in profile["phases"]] == ["base_dataset", "dataset_build"]
    with runner.store.connect() as connection:
        rows = connection.execute("SELECT payload FROM events WHERE kind='dataset_build_profile'").fetchall()
    assert len(rows) == (0 if event_write_fails else 1)
    if rows:
        assert json.loads(rows[0][0]) == profile


def test_runtime_final_clock_failure_preserves_original_builder_baseexception(tmp_path, monkeypatch):
    from ml.intraday import runtime
    original = KeyboardInterrupt("fixture original operation interrupted")
    failed = False
    real_clock = time.perf_counter
    def clock():
        if failed:
            raise SystemExit("fixture diagnostic clock failed")
        return real_clock()
    def builder(*args, **kwargs):
        nonlocal failed
        failed = True
        raise original
    runner = runtime_runner(tmp_path, builder)
    monkeypatch.setattr(runtime.time, "perf_counter", clock)
    with pytest.raises(KeyboardInterrupt) as caught:
        runner._symbol("fixture-cycle", "TEST", history("TEST"), REF,
            REF + pd.Timedelta(minutes=16), "live", None, None, real_clock())
    assert caught.value is original
    profile = _symbol_exception_diagnostics(original)["dataset_build_profile"]
    assert profile["status"] == "error" and profile["error_type"] == "KeyboardInterrupt"
    assert profile["observer_errors"] == ["SystemExit"]


@pytest.mark.parametrize("stage", ["clock", "event"])
def test_new_control_interrupt_on_successful_build_propagates(tmp_path, monkeypatch, stage):
    from ml.intraday import runtime
    interrupt = KeyboardInterrupt("fixture new human interrupt")
    completed = False
    def builder(*args, **kwargs):
        nonlocal completed
        result = dataset(*args, **kwargs)
        completed = True
        return result
    runner = runtime_runner(tmp_path, builder)
    real_clock = time.perf_counter
    original_event = runner.store.event
    def clock():
        if completed:
            raise interrupt
        return real_clock()
    def event(cycle_id, kind, payload, **kwargs):
        if kind == "dataset_build_profile":
            raise interrupt
        return original_event(cycle_id, kind, payload, **kwargs)
    if stage == "clock":
        monkeypatch.setattr(runtime.time, "perf_counter", clock)
    else:
        monkeypatch.setattr(runner.store, "event", event)
    with pytest.raises(KeyboardInterrupt) as caught:
        runner._symbol("fixture-cycle", "TEST", history("TEST"), REF,
            REF + pd.Timedelta(minutes=16), "live", None, None, real_clock())
    assert caught.value is interrupt and completed
    assert diagnostics._current.get() is None


@pytest.mark.parametrize("event_write_fails", [False, True])
def test_runtime_success_keeps_profiles_outside_frozen_snapshot(tmp_path, monkeypatch, event_write_fails):
    def instrumented(*args, **kwargs):
        with diagnostics.dataset_phase("base_dataset"):
            return dataset(*args, **kwargs)
    runner = runtime_runner(tmp_path / "captured", instrumented)
    control = runtime_runner(tmp_path / "control", dataset)
    original_event = runner.store.event
    def event(cycle_id, kind, payload, **kwargs):
        if kind == "dataset_build_profile" and event_write_fails:
            raise OSError("fixture diagnostic persistence failed")
        return original_event(cycle_id, kind, payload, **kwargs)
    monkeypatch.setattr(runner.store, "event", event)
    first = runner.run_cycle({"TEST": history("TEST")}, reference_at=REF, mode="live")
    second = control.run_cycle({"TEST": history("TEST")}, reference_at=REF, mode="live")
    assert first["failed_symbols"] == second["failed_symbols"] == 0
    result = first["forecasts"]["TEST"]
    profile = result["preprocessing_profile"]["dataset_build_profile"]
    assert profile["status"] == "success"
    frozen_hash, frozen = runner.store.inputs(first["cycle_id"], "TEST")
    control_hash, control_frozen = control.store.inputs(second["cycle_id"], "TEST")
    assert frozen == control_frozen and frozen_hash == control_hash == digest(frozen)
    assert "dataset_build_profile" not in json.dumps(frozen)
    assert diagnostics._current.get() is None


def test_process_cpu_scope_and_worker_identity_are_serializable(monkeypatch):
    clocks = iter([(10.0, 2.0, 5.0), (12.5, 2.75, 9.0)])
    monkeypatch.setattr(diagnostics, "_clocks", lambda: next(clocks))
    monkeypatch.setattr(diagnostics.threading, "get_native_id", lambda: 12345)
    monkeypatch.setattr(diagnostics.threading, "current_thread",
        lambda: type("Worker", (), {"name": "dataset worker/" + "x" * 200})())
    with diagnostics.capture_dataset_build() as profile:
        with diagnostics.dataset_phase("base_dataset"):
            pass
    restored = json.loads(json.dumps(profile))
    assert restored["cpu_scope"] == "current_thread"
    assert restored["process_cpu_scope"] == "all_process_threads"
    assert restored["worker"] == {"thread_name": "dataset_worker_" + "x" * 81,
        "native_thread_id": 12345}
    phase = restored["phases"][0]
    assert phase["wall_seconds"] == 2.5 and phase["thread_cpu_seconds"] == .75
    # Aggregate native CPU may exceed wall time; it must not be capped to wall.
    assert phase["process_cpu_seconds"] == 4.0


@pytest.mark.parametrize("failure_call", [1, 2])
@pytest.mark.parametrize("has_original_error", [False, True])
def test_process_clock_failure_never_replaces_operation(monkeypatch, failure_call, has_original_error):
    calls = 0
    def process_clock():
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise OSError("private process clock detail")
        return 1.0
    monkeypatch.setattr(diagnostics.time, "process_time", process_clock)
    original = FixtureFailure("original body")
    if has_original_error:
        with pytest.raises(FixtureFailure) as caught:
            with diagnostics.capture_dataset_build() as profile:
                with diagnostics.dataset_phase("base_dataset"):
                    raise original
        assert caught.value is original
    else:
        with diagnostics.capture_dataset_build() as profile:
            with diagnostics.dataset_phase("base_dataset"):
                result = 42
        assert result == 42 and profile["status"] == "success"
    assert profile["observer_errors"] == ["OSError"] and profile["phases"] == []
    assert "private process clock detail" not in json.dumps(profile)


def test_worker_identity_failure_is_bounded_and_does_not_mask_body(monkeypatch):
    def fail():
        raise ValueError("private worker detail")
    monkeypatch.setattr(diagnostics.threading, "get_native_id", fail)
    original = FixtureFailure("original operation")
    with pytest.raises(FixtureFailure) as caught:
        with diagnostics.capture_dataset_build() as profile:
            with diagnostics.dataset_phase("base_dataset"):
                raise original
    assert caught.value is original and profile["observer_errors"] == ["ValueError"]
    assert "worker" not in profile and "private worker detail" not in json.dumps(profile)


def test_new_manual_interrupt_while_reading_worker_identity_propagates(monkeypatch):
    original = KeyboardInterrupt("manual stop fixture")
    def fail():
        raise original
    monkeypatch.setattr(diagnostics.threading, "get_native_id", fail)
    with pytest.raises(KeyboardInterrupt) as caught:
        with diagnostics.capture_dataset_build():
            pytest.fail("manual interrupt must precede body")
    assert caught.value is original and diagnostics._current.get() is None


def test_worker_identity_captures_concurrent_build_thread_not_caller():
    barrier = threading.Barrier(2)
    def build():
        with diagnostics.capture_dataset_build() as profile:
            barrier.wait(timeout=10)
            with diagnostics.dataset_phase("base_dataset"):
                pass
        return profile
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="diagnostic-fixture") as pool:
        futures = [pool.submit(build) for _ in range(2)]
        profiles = [future.result(timeout=10) for future in futures]
    identities = [p["worker"] for p in profiles]
    assert all(i["thread_name"].startswith("diagnostic-fixture") for i in identities)
    assert len({i["native_thread_id"] for i in identities}) == 2
    assert threading.get_native_id() not in {i["native_thread_id"] for i in identities}


def test_worker_registry_projection_never_controls_threads_and_bounds_errors():
    class Worker:
        native_id = 123
        def start(self):
            pytest.fail("metadata must not start workers")
        join = is_alive = start
    class Broken:
        @property
        def native_id(self):
            raise ValueError("private native identity")
    registry = [("forecast", "intraday-manual-forecast", Worker()),
        ("scoring", "intraday-manual-scoring", object()),
        ("minute_data", "intraday-manual-minute-data", Broken()),
        ("market_context", "intraday-manual-market-context", type("Bad", (), {"native_id": True})()),
        ("extra", "not emitted", Worker())]
    records = json.loads(json.dumps(diagnostics.worker_threads(registry)))
    assert len(records) == 4 and records[0]["native_thread_id"] == 123
    assert records[1] == {"role": "scoring", "thread_name": "intraday-manual-scoring"}
    assert records[2]["observer_error"] == records[3]["observer_error"] == "ValueError"
    assert "private native identity" not in json.dumps(records)


@pytest.mark.parametrize("has_original_error", [False, True])
def test_worker_registry_control_interrupt_keeps_existing_exception(has_original_error):
    interrupt = KeyboardInterrupt("fixture manual stop")
    original = FixtureFailure("fixture original error")
    class Interrupted:
        @property
        def native_id(self):
            raise interrupt
    registry = [("scoring", "intraday-manual-scoring", Interrupted())]
    if has_original_error:
        with pytest.raises(FixtureFailure) as caught:
            try:
                raise original
            finally:
                records = diagnostics.worker_threads(registry)
        assert caught.value is original and records[0]["observer_error"] == "KeyboardInterrupt"
    else:
        with pytest.raises(KeyboardInterrupt) as caught:
            diagnostics.worker_threads(registry)
        assert caught.value is interrupt
