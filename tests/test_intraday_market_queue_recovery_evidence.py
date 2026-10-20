"""Queue-operation recovery evidence with native locks and offline own-session fixtures."""
import json
import socket

from filelock import FileLock, Timeout
import pytest

from ml.intraday.market_collection import CollectionResult
from tests.test_intraday_market_collection_lock_retry import case, task


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network in recovery evidence regression")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


def recovered(retries, snapshots):
    return {"reason": "queue_lock_contention_recovered", "operation": "collect",
            "consecutive_retry_count": retries, "returned_snapshot_count": snapshots,
            "coverage_claim": "Queue collection completed; provider-data success and coverage are not implied"}


@pytest.mark.parametrize("stage", ["enqueue", "step"])
@pytest.mark.parametrize("delivery", ["records", "provider_empty", "no_due", "provider_denied"])
def test_real_queue_recovery_reports_once_after_return_without_claiming_provider_success(
        case, monkeypatch, stage, delivery):
    col, calls = case
    if delivery == "no_due":
        col.step(max_requests=1)
        calls.clear()
    elif delivery != "records":
        def adapter(*args):
            calls.append(1)
            return CollectionResult([], "NOT_AUTHORIZED" if delivery == "provider_denied" else "COMPLETE")
        col.adapters["fmp"] = adapter
    before = col.state_path.read_bytes()
    lock = FileLock(col.lock.lock_file, timeout=0)
    enqueue = col.enqueue
    if stage == "enqueue":
        lock.acquire()
    else:
        def contend_before_step(*args, **kwargs):
            result = enqueue(*args, **kwargs)
            lock.acquire()
            monkeypatch.setattr(col, "enqueue", enqueue)
            return result
        monkeypatch.setattr(col, "enqueue", contend_before_step)
    retries, recoveries, pauses = [], [], []
    stopped = [False]
    normal_pauses = [0]
    def retry(detail):
        retries.append(detail)
        assert recoveries == [] and col.state_path.read_bytes() == before and calls == []
    def recovery(detail):
        assert len(retries) == 3 and not lock.is_locked
        assert calls == ([] if delivery == "no_due" else [1])
        assert task(col)["attempts"] == 1
        recoveries.append(detail)
    def pause(seconds):
        pauses.append(seconds)
        if lock.is_locked:
            if len(retries) == 3:
                lock.release()
        else:
            normal_pauses[0] += 1
            if normal_pauses[0] == 3:
                stopped[0] = True
    try:
        col.run(stopped=lambda: stopped[0], pause=pause, max_requests=1,
                on_retry=retry, on_recovery=recovery)
    finally:
        if lock.is_locked:
            lock.release()
    assert recoveries == [recovered(3, 0 if delivery == "no_due" else 1)]
    assert pauses[:3] == [0.02] * 3 and normal_pauses == [3]
    assert task(col)["status"] == {"records": "COMPLETE", "no_due": "COMPLETE",
                                   "provider_empty": "EMPTY", "provider_denied": "NOT_AUTHORIZED"}[delivery]


def test_recovery_counter_resets_for_next_contention_episode(case):
    col, calls = case
    retries, recoveries, stopped = [], [], [False]
    lock = FileLock(col.lock.lock_file, timeout=0)
    lock.acquire()
    def pause(seconds):
        if lock.is_locked:
            lock.release()
        elif len(recoveries) == 1:
            lock.acquire()
        else:
            stopped[0] = True
    try:
        col.run(stopped=lambda: stopped[0], pause=pause, max_requests=1,
                on_retry=retries.append, on_recovery=recoveries.append)
    finally:
        if lock.is_locked:
            lock.release()
    assert len(retries) == 2 and recoveries == [recovered(1, 1), recovered(1, 0)]
    assert calls == [1] and task(col)["attempts"] == 1


def test_ordinary_collect_never_emits_recovery(case):
    col, calls = case
    recoveries, stopped = [], [False]
    col.run(stopped=lambda: stopped[0], pause=lambda _: stopped.__setitem__(0, True),
            on_recovery=recoveries.append)
    assert calls == [1] and recoveries == []


@pytest.mark.parametrize("stop_stage", ["retry", "pause"])
def test_manual_stop_during_contention_has_no_recovery_or_provider_attempt(case, stop_stage):
    col, calls = case
    recoveries, stopped = [], [False]
    def retry(detail):
        if stop_stage == "retry":
            stopped[0] = True
    def pause(seconds):
        assert stop_stage == "pause"
        stopped[0] = True
    with FileLock(col.lock.lock_file, timeout=0):
        col.run(stopped=lambda: stopped[0], pause=pause, on_retry=retry,
                on_recovery=recoveries.append)
    assert calls == [] and recoveries == [] and task(col)["attempts"] == 0


@pytest.mark.parametrize("kind", ["other_lock", "permission", "interrupted", "exit"])
def test_post_contention_failure_propagates_original_without_recovery(case, monkeypatch, kind):
    col, calls = case
    error = {"other_lock": Timeout(str(col.root / "unrelated.lock")),
             "permission": PermissionError("fixture"), "interrupted": InterruptedError("fixture"),
             "exit": KeyboardInterrupt("fixture")}[kind]
    lock = FileLock(col.lock.lock_file, timeout=0)
    lock.acquire()
    retries, recoveries = [], []
    def pause(seconds):
        lock.release()
        def fail(**kwargs):
            raise error
        monkeypatch.setattr(col, "collect", fail)
    try:
        with pytest.raises(type(error)) as caught:
            col.run(stopped=lambda: False, pause=pause, on_retry=retries.append,
                    on_recovery=recoveries.append)
        assert caught.value is error
    finally:
        if lock.is_locked:
            lock.release()
    assert len(retries) == 1 and recoveries == [] and calls == []


def test_recovery_callback_timeout_is_not_misclassified_as_collection_contention(case):
    col, calls = case
    error = Timeout(col.lock.lock_file)
    lock = FileLock(col.lock.lock_file, timeout=0)
    retries = []
    def recovery(detail):
        assert detail == recovered(1, 1)
        raise error
    lock.acquire()
    try:
        with pytest.raises(Timeout) as caught:
            col.run(stopped=lambda: False, pause=lambda _: lock.release(),
                    on_retry=retries.append, on_recovery=recovery)
        assert caught.value is error
    finally:
        if lock.is_locked:
            lock.release()
    assert len(retries) == 1 and calls == [1]


def test_optional_recovery_callback_preserves_existing_caller(case):
    col, calls = case
    stopped = [False]
    lock = FileLock(col.lock.lock_file, timeout=0)
    lock.acquire()
    def pause(seconds):
        if lock.is_locked:
            lock.release()
        else:
            stopped[0] = True
    try:
        col.run(stopped=lambda: stopped[0], pause=pause)
    finally:
        if lock.is_locked:
            lock.release()
    assert calls == [1] and task(col)["attempts"] == 1


def test_recovery_event_is_persisted_only_in_scout_own_session(tmp_path, monkeypatch):
    from ml.intraday import live_session, minute_feed
    from ml.intraday.store import ShadowStore
    from tests.test_intraday_live_session import scout_manual_fixture
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    class FixtureFeed:
        history_source = None
        def __init__(self, **kwargs):
            pass
        def run(self, **kwargs):
            pass
    monkeypatch.setattr(minute_feed, "QuarterHourMinuteFeed", FixtureFeed)
    retry = {"reason": "queue_lock_contention", "error_type": "Timeout", "retry_after_seconds": 30.0}
    detail = recovered(2, 0)
    class Collector:
        def run(self, *, stopped, pause, on_retry, on_recovery=None):
            assert not stopped()
            on_retry(retry)
            on_retry(retry)
            if on_recovery is not None:
                on_recovery(detail)
            assert not stopped()
    monkeypatch.setattr(live_session, "prepare_market_collection", lambda *a, **k: Collector())
    result = live_session.run_manual(**kwargs)
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert result["status"] == "STOPPED" and status["stop_reason"] == "manual_stop"
    assert status.get("worker_errors", []) == [] and "step" in calls
    assert not executors[0].kwargs["manual_authority"]()
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    with store.connect() as db:
        rows = db.execute("SELECT cycle_id,payload FROM events WHERE kind='market_context_queue_recovered'").fetchall()
        retry_count = db.execute("SELECT count(*) FROM events WHERE kind='market_context_queue_retry'").fetchone()[0]
        failures = db.execute("SELECT count(*) FROM events WHERE kind='market_context_worker_stopped'").fetchone()[0]
    assert rows == [(result["session_id"], json.dumps(detail, sort_keys=True, separators=(",", ":")))]
    assert retry_count == 2 and failures == 0
