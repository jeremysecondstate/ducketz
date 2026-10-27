from contextlib import contextmanager
import threading

from filelock import FileLock, Timeout
import pytest

from ml.intraday import market_collection_timing as MOD


@contextmanager
def unlocked():
    yield "native-enter-value"


def test_native_success_preserves_release_and_enter_value(tmp_path):
    timing = MOD.QueueLockTiming()
    lock = FileLock(str(tmp_path / "private.lock"), timeout=0)
    with timing.observe(lock, "enqueue") as entered:
        assert entered is not None
        assert lock.is_locked
        active = timing.snapshot()["active_local"]
        assert active[0]["phase"] == "enqueue" and active[0]["state"] == "holding"
    assert not lock.is_locked
    row = timing.snapshot()["recent"][-1]
    assert row["outcome"] == "completed" and row["wait_seconds"] >= 0
    assert row["hold_seconds"] >= 0
    assert timing.snapshot()["active_local"] == []


def test_native_contended_timeout_preserves_exception_and_held_lock(tmp_path):
    path = str(tmp_path / "private.lock")
    holder, contender = FileLock(path, timeout=0), FileLock(path, timeout=0)
    timing = MOD.QueueLockTiming()
    with holder:
        with pytest.raises(Timeout) as raised:
            with timing.observe(contender, "task_recheck"):
                pytest.fail("Contended native lock entered")
        assert raised.value.lock_file == path
        assert holder.is_locked and not contender.is_locked
    row = timing.snapshot()["recent"][-1]
    assert row["phase"] == "task_recheck" and row["outcome"] == "acquire_error"
    assert row["hold_seconds"] is None


@pytest.mark.parametrize("error", [ValueError("body"), Timeout("private-only"), KeyboardInterrupt("body")])
def test_body_exception_identity_preserved_and_native_lock_released(tmp_path, error):
    lock = FileLock(str(tmp_path / "private.lock"), timeout=0)
    timing = MOD.QueueLockTiming()
    with pytest.raises(type(error)) as raised:
        with timing.observe(lock, "receipt_commit"):
            raise error
    assert raised.value is error
    assert not lock.is_locked
    assert timing.snapshot()["recent"][-1]["outcome"] == "body_error"


def test_original_native_reentrant_depth_preserved(tmp_path):
    lock = FileLock(str(tmp_path / "private.lock"), timeout=0)
    timing = MOD.QueueLockTiming()
    with timing.observe(lock, "enqueue"):
        with timing.observe(lock, "step_due"):
            assert lock.lock_counter == 2 and len(timing.snapshot()["active_local"]) == 2
        assert lock.lock_counter == 1 and len(timing.snapshot()["active_local"]) == 1
    assert lock.lock_counter == 0 and len(timing.snapshot()["recent"]) == 2


def test_clock_observer_failure_does_not_replace_original_error():
    original = ValueError("original body")
    def broken():
        raise RuntimeError("observer-only failure")
    timing = MOD.QueueLockTiming(monotonic=broken)
    with pytest.raises(ValueError) as raised:
        with timing.observe(unlocked(), "enqueue"):
            raise original
    assert raised.value is original
    snap = timing.snapshot()
    assert snap["observer_error"] is True
    assert snap["recent"][-1]["wait_seconds"] is None


def test_record_observer_failure_preserves_native_release_and_error(tmp_path):
    class BrokenEvents:
        def append(self, value):
            raise MemoryError("observer-only failure")
        def __iter__(self):
            raise RuntimeError("observer snapshot failure")
    timing = MOD.QueueLockTiming()
    timing._events = BrokenEvents()
    lock = FileLock(str(tmp_path / "private.lock"), timeout=0)
    original = RuntimeError("body original")
    with pytest.raises(RuntimeError) as raised:
        with timing.observe(lock, "source_block"):
            raise original
    assert raised.value is original and not lock.is_locked
    assert timing.snapshot()["observer_error"] is True


def test_native_release_error_identity_preserved():
    original = RuntimeError("release original")
    class Lock:
        def __enter__(self):
            return "original enter value"
        def __exit__(self, *args):
            raise original
    timing = MOD.QueueLockTiming()
    with pytest.raises(RuntimeError) as raised:
        with timing.observe(Lock(), "orphan_commit") as value:
            assert value == "original enter value"
    assert raised.value is original
    assert timing.snapshot()["recent"][-1]["outcome"] == "release_error"


def test_native_exception_suppression_is_preserved():
    class Lock:
        def __enter__(self):
            return None
        def __exit__(self, *args):
            return True
    timing = MOD.QueueLockTiming()
    with timing.observe(Lock(), "source_block"):
        raise ValueError("suppressed by original context")
    assert timing.snapshot()["recent"][-1]["outcome"] == "body_error"


def test_snapshot_observes_another_local_thread_without_locking_it():
    timing = MOD.QueueLockTiming()
    entered, proceed = threading.Event(), threading.Event()
    def hold():
        with timing.observe(unlocked(), "step_due"):
            entered.set()
            assert proceed.wait(3)
    worker = threading.Thread(target=hold)
    worker.start()
    assert entered.wait(3)
    try:
        snap = timing.snapshot()
        assert len(snap["active_local"]) == 1
        assert snap["active_local"][0]["phase"] == "step_due"
        assert snap["active_local"][0]["state"] == "holding"
        assert snap["active_local"][0]["elapsed_seconds"] >= 0
    finally:
        proceed.set()
        worker.join(3)
    assert not worker.is_alive() and timing.snapshot()["active_local"] == []


def test_duration_fields_are_bounded_numbers_and_ring_has_fixed_bound():
    timing = MOD.QueueLockTiming()
    for _ in range(40):
        with timing.observe(unlocked(), "enqueue"):
            pass
    snap = timing.snapshot()
    assert len(snap["recent"]) == 32
    assert set(snap["recent"][0]) == {"phase", "outcome", "wait_seconds", "hold_seconds", "thread_cpu_seconds",
        "event_id", "thread_id", "started_monotonic", "acquired_monotonic", "ended_monotonic"}
    for row in snap["recent"]:
        for field in ("wait_seconds", "hold_seconds", "thread_cpu_seconds"):
            assert row[field] >= 0


def test_invalid_free_text_phase_is_not_recorded():
    timing = MOD.QueueLockTiming()
    with timing.observe(unlocked(), "sensitive-user-input"):
        pass
    assert timing.snapshot()["recent"][0]["phase"] == "other"
    assert "sensitive-user-input" not in str(timing.snapshot())


def test_missing_or_failed_observer_snapshot_is_safe():
    assert MOD.queue_lock_timing_snapshot(object())["observer_error"] is True
    class Broken:
        def snapshot(self):
            raise KeyboardInterrupt("observer-only")
    class Collector:
        queue_lock_timing = Broken()
    assert MOD.queue_lock_timing_snapshot(Collector())["observer_error"] is True


def test_backward_or_nonfinite_observer_clock_does_not_invent_durations():
    assert MOD._elapsed(5, 4) is None
    assert MOD._elapsed(float("nan"), 4) is None
    assert MOD._elapsed(None, 4) is None
    assert MOD._elapsed(4, 5) == 1


def test_concurrent_snapshots_retain_bounded_evidence_without_observer_errors():
    import sys
    timing = MOD.QueueLockTiming()
    barrier = threading.Barrier(5)
    errors = []
    def work():
        try:
            barrier.wait(3)
            for _ in range(4000):
                with timing.observe(unlocked(), "task_recheck"):
                    pass
        except BaseException as exc:
            errors.append(type(exc).__name__)
    threads = [threading.Thread(target=work) for _ in range(4)]
    original = sys.getswitchinterval()
    try:
        sys.setswitchinterval(.000001)
        for worker in threads:
            worker.start()
        barrier.wait(3)
        for _ in range(4000):
            snap = timing.snapshot()
            assert snap["observer_error"] is False
            assert len(snap["recent"]) <= 32 and len(snap["active_local"]) <= 4
    finally:
        sys.setswitchinterval(original)
        for worker in threads:
            worker.join(10)
    assert all(not worker.is_alive() for worker in threads)
    assert errors == []
    assert timing.snapshot()["observer_error"] is False
    assert len(timing.snapshot()["recent"]) == 32


def test_collector_timeout_retains_native_phase_and_adds_safe_timing(tmp_path):
    from ml.intraday import market_collection as collection
    collector = collection.QuarterHourMarketCollector.__new__(collection.QuarterHourMarketCollector)
    collector.lock = FileLock(str(tmp_path / "private.lock"), timeout=0)
    collector.queue_lock_timing = MOD.QueueLockTiming()
    with FileLock(collector.lock.lock_file, timeout=0):
        with pytest.raises(Timeout) as raised:
            collector.step(max_requests=1)
    detail = collection.collection_failure_diagnostics(collector, raised.value)
    assert detail["collector_phase"] == "step_queue_lock_acquire"
    row = detail["queue_lock_timing"]["recent"][-1]
    assert row["phase"] == "step_due" and row["outcome"] == "acquire_error"
    assert row["wait_seconds"] >= 0 and row["hold_seconds"] is None
    assert str(tmp_path) not in str(detail)


def test_other_lock_in_original_critical_section_is_not_queue_acquisition(tmp_path):
    from ml.intraday import market_collection as collection
    collector = collection.QuarterHourMarketCollector.__new__(collection.QuarterHourMarketCollector)
    collector.lock = FileLock(str(tmp_path / "queue-private.lock"), timeout=0)
    collector.queue_lock_timing = MOD.QueueLockTiming()
    other = FileLock(str(tmp_path / "other-private.lock"), timeout=0)
    def state():
        with other:
            pytest.fail("Other native lock unexpectedly entered")
    collector._state = state
    with FileLock(other.lock_file, timeout=0):
        with pytest.raises(Timeout) as raised:
            collector.step(max_requests=1)
    detail = collection.collection_failure_diagnostics(collector, raised.value)
    assert detail["collector_phase"] == "step"
    assert detail["queue_lock_path_matches"] is False
    row = detail["queue_lock_timing"]["recent"][-1]
    assert row["outcome"] == "body_error" and row["hold_seconds"] >= 0


def test_malformed_trace_observer_cannot_raise():
    class Poison:
        @property
        def tb_frame(self):
            raise KeyboardInterrupt("observer only")
    assert MOD.observed_lock_acquire_trace(Poison(), object()) is False



def test_failure_time_holding_sibling_survives_actual_late_retry_callback(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone
    from ml.intraday import market_collection as collection
    path = str(tmp_path / "private-queue.lock")
    timing = MOD.QueueLockTiming()
    holder, contender = FileLock(path, timeout=0), FileLock(path, timeout=0)
    entered, release = threading.Event(), threading.Event()
    native_errors, callbacks = [], []
    collector = collection.QuarterHourMarketCollector.__new__(collection.QuarterHourMarketCollector)
    collector.lock, collector.queue_lock_timing = contender, timing
    collector.clock = lambda: datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc)
    collector.retry_seconds = 30

    def hold():
        with timing.observe(holder, "receipt_commit"):
            entered.set()
            assert release.wait(3)

    def fail():
        assert entered.wait(3)
        try:
            with timing.observe(contender, "task_recheck"):
                pytest.fail("Contended native lock entered")
        except Timeout as error:
            native_errors.append(error)
            raise
        finally:
            release.set()

    def collect(**kwargs):
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                holder_future = pool.submit(hold)
                failed_future = pool.submit(fail)
                failed_future.result()
                holder_future.result()
        except Timeout as error:
            assert error is native_errors[0]
            assert timing.snapshot()["active_local"] == []
            raise

    collector.collect = collect
    stopped = threading.Event()
    monkeypatch.setattr(collection, "collection_window_open", lambda now: True)
    collector.run(stopped.is_set, lambda delay: stopped.set(), on_retry=callbacks.append)
    assert len(native_errors) == len(callbacks) == 1
    assert native_errors[0].lock_file == path
    assert not holder.is_locked and not contender.is_locked
    snap = callbacks[0]["queue_lock_timing"]
    assert snap["active_local"] == []
    failed = next(row for row in snap["recent"] if row["outcome"] == "acquire_error")
    held = next(row for row in failed["failure_active_local"] if row["state"] == "holding")
    waiting = next(row for row in failed["failure_active_local"] if row["state"] == "waiting")
    assert held["phase"] == "receipt_commit" and waiting["phase"] == "task_recheck"
    assert waiting["event_id"] == failed["event_id"]
    assert held["thread_id"] != failed["thread_id"] == waiting["thread_id"]
    assert held["started_monotonic"] <= held["acquired_monotonic"] <= failed["ended_monotonic"]
    assert waiting["started_monotonic"] == failed["started_monotonic"]
    assert waiting["acquired_monotonic"] is None
    assert failed["failure_snapshot_monotonic"] == failed["ended_monotonic"]
    assert failed["failure_capture_stage"] == "native_acquisition_error_before_pool_drain"
    assert snap["observer_error"] is False
    assert str(tmp_path) not in str(snap)
    assert snap["evidence_version"] == "failure-time-correlation-v1"
    assert snap["clock_scope"] == "Monotonic clock within this process lifetime only"
    assert type(snap["process_id"]) is type(snap["collector_id"]) is int
    assert snap["process_id"] > 0 and snap["collector_id"] > 0


def test_fixed_native_intervals_keep_original_wall_and_cpu_meaning():
    from itertools import chain, repeat
    monotonic = iter(chain((10, 12, 15), repeat(20)))
    cpu = iter(chain((1, 2, 4), repeat(5)))
    timing = MOD.QueueLockTiming(monotonic=lambda: next(monotonic), thread_cpu=lambda: next(cpu))
    with timing.observe(unlocked(), "enqueue"):
        pass
    row = timing.snapshot()["recent"][0]
    assert row["started_monotonic"] == 10
    assert row["acquired_monotonic"] == 12
    assert row["ended_monotonic"] == 15
    assert row["wait_seconds"] == 2 and row["hold_seconds"] == 3
    assert row["thread_cpu_seconds"] == 3
    assert "failure_active_local" not in row


def test_failed_native_enter_keeps_identity_and_captures_waiting_interval():
    from itertools import chain, repeat
    original = KeyboardInterrupt("private original error")
    calls = []
    class Lock:
        def __enter__(self):
            calls.append("enter")
            raise original
        def __exit__(self, *args):
            calls.append("exit")
    monotonic = iter(chain((10, 15), repeat(20)))
    timing = MOD.QueueLockTiming(monotonic=lambda: next(monotonic), thread_cpu=lambda: 1)
    with pytest.raises(KeyboardInterrupt) as caught:
        with timing.observe(Lock(), "source_block"):
            pytest.fail("Native failing enter yielded")
    assert caught.value is original and calls == ["enter"]
    row = timing.snapshot()["recent"][0]
    assert row["started_monotonic"] == 10 and row["ended_monotonic"] == 15
    assert row["acquired_monotonic"] is None and row["hold_seconds"] is None
    assert row["wait_seconds"] == 5
    assert len(row["failure_active_local"]) == 1
    waiting = row["failure_active_local"][0]
    assert waiting["state"] == "waiting" and waiting["elapsed_seconds"] == 5
    assert waiting["event_id"] == row["event_id"]
    assert "private original error" not in str(timing.snapshot())


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"),
                                  True, "private-clock", object(), 10 ** 1000])
def test_unsafe_clock_values_are_none_and_never_replace_native_error(value):
    original = ValueError("native-body")
    timing = MOD.QueueLockTiming(monotonic=lambda: value, thread_cpu=lambda: value)
    with pytest.raises(ValueError) as caught:
        with timing.observe(unlocked(), "enqueue"):
            raise original
    assert caught.value is original
    snap = timing.snapshot()
    assert snap["observer_error"] is True
    row = snap["recent"][0]
    for key in ("started_monotonic", "acquired_monotonic", "ended_monotonic",
                "wait_seconds", "hold_seconds", "thread_cpu_seconds"):
        assert row[key] is None
    import json
    assert "private-clock" not in json.dumps(snap, allow_nan=False)


def test_finite_endpoint_duration_overflow_does_not_publish_infinity():
    assert MOD._elapsed(-1e308, 1e308) is None
    assert MOD._elapsed(10 ** 1000, 10 ** 1000 + 1) is None


@pytest.mark.parametrize("value", [False, -1, 0, 2 ** 64, "private-id", object()])
def test_unsafe_process_collector_thread_identity_fails_closed(monkeypatch, value):
    from types import SimpleNamespace
    monkeypatch.setattr(MOD, "os", SimpleNamespace(getpid=lambda: value))
    monkeypatch.setattr(MOD, "threading", SimpleNamespace(get_ident=lambda: value))
    monkeypatch.setattr(MOD, "id", lambda self: value, raising=False)
    timing = MOD.QueueLockTiming()
    with timing.observe(unlocked(), "enqueue"):
        pass
    snap = timing.snapshot()
    assert snap["observer_error"] is True
    assert snap["process_id"] is snap["collector_id"] is None
    assert snap["recent"][0]["thread_id"] is None
    assert "private-id" not in str(snap)


def test_identity_observer_baseexception_cannot_change_body_or_release(monkeypatch):
    from types import SimpleNamespace
    def broken():
        raise KeyboardInterrupt("private diagnostic failure")
    monkeypatch.setattr(MOD, "os", SimpleNamespace(getpid=broken))
    monkeypatch.setattr(MOD, "threading", SimpleNamespace(get_ident=broken))
    timing = MOD.QueueLockTiming()
    calls = []
    class Lock:
        def __enter__(self):
            calls.append("enter")
            return "native value"
        def __exit__(self, *args):
            calls.append("exit")
    with timing.observe(Lock(), "enqueue") as value:
        assert value == "native value"
        calls.append("body")
    assert calls == ["enter", "body", "exit"]
    assert timing.snapshot()["observer_error"] is True


def test_failure_snapshot_error_cannot_replace_native_error_or_leave_stale_active(monkeypatch):
    timing = MOD.QueueLockTiming()
    original = RuntimeError("native acquisition")
    class Lock:
        def __enter__(self):
            raise original
        def __exit__(self, *args):
            pytest.fail("Failed native enter must not exit")
    def broken(now):
        raise MemoryError("private snapshot failure")
    with monkeypatch.context() as scoped:
        scoped.setattr(timing, "_active_rows", broken)
        with pytest.raises(RuntimeError) as caught:
            with timing.observe(Lock(), "task_recheck"):
                pytest.fail("Failed native enter yielded")
    assert caught.value is original
    snap = timing.snapshot()
    assert snap["observer_error"] is True and snap["active_local"] == []
    assert snap["recent"][0]["failure_active_local"] == []


def test_failure_history_nested_rows_and_identities_are_bounded_and_detached():
    from contextlib import ExitStack
    original = Timeout("private-only")
    class Lock:
        def __enter__(self):
            raise original
        def __exit__(self, *args):
            pytest.fail("Failed enter must not exit")
    timing = MOD.QueueLockTiming()
    with ExitStack() as held:
        for _ in range(40):
            held.enter_context(timing.observe(unlocked(), "receipt_commit"))
        for _ in range(40):
            with pytest.raises(Timeout) as caught:
                with timing.observe(Lock(), "task_recheck"):
                    pytest.fail("Contended lock entered")
            assert caught.value is original
        snap = timing.snapshot()
        assert len(snap["recent"]) == len(snap["active_local"]) == 32
        assert all(len(row["failure_active_local"]) == 32 for row in snap["recent"])
        assert all(row["event_id"] >= 0 for row in snap["recent"])
        snap["recent"][0]["failure_active_local"][0]["phase"] = "private mutation"
        snap["recent"][0]["event_id"] = -99
        snap["active_local"][0]["phase"] = "private active mutation"
        fresh = timing.snapshot()
        assert "private mutation" not in str(fresh)
        assert "private active mutation" not in str(fresh)
        assert fresh["recent"][0]["event_id"] >= 0
    assert timing.snapshot()["active_local"] == []


def test_collector_and_event_identity_separate_process_local_observers():
    left, right = MOD.QueueLockTiming(), MOD.QueueLockTiming()
    for timing in (left, right):
        with timing.observe(unlocked(), "enqueue"):
            pass
    a, b = left.snapshot(), right.snapshot()
    assert a["process_id"] == b["process_id"]
    assert a["collector_id"] != b["collector_id"]
    assert a["recent"][0]["event_id"] == b["recent"][0]["event_id"] == 0
    assert a["recent"][0]["thread_id"] == b["recent"][0]["thread_id"]
