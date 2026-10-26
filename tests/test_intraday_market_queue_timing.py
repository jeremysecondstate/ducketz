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
    assert set(snap["recent"][0]) == {"phase", "outcome", "wait_seconds", "hold_seconds", "thread_cpu_seconds"}
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

