"""Real local lock contention with injected providers; no live queue or network."""
from datetime import datetime, timedelta, timezone
import json
import socket

from filelock import FileLock, Timeout
import pytest

from ml.intraday.market_collection import CollectionResult, QuarterHourMarketCollector, SourceRequest


NOW = datetime(2026, 10, 19, 19, 0, 10, tzinfo=timezone.utc)
START = NOW - timedelta(minutes=15, seconds=10)


@pytest.fixture
def case(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No network in queue retry regression")
    monkeypatch.setattr(socket, "socket", forbidden)
    calls = []
    def adapter(*args):
        calls.append(1)
        return CollectionResult([{"symbol": "DOCU", "event_at": NOW.isoformat(), "bid": 10, "ask": 11}])
    request = SourceRequest(provider="fmp", source_kind="stock_quote", symbol="DOCU",
        dataset="stable", schema="quote", endpoint="quote", params={"symbol": "DOCU"})
    col = QuarterHourMarketCollector(tmp_path, symbols=["DOCU"], requests=[request],
        adapters={"fmp": adapter}, clock=lambda: NOW, retry_seconds=0.02)
    col.lock.timeout = 0.01
    col.enqueue(START)
    return col, calls


def task(col):
    values = list(json.loads(col.state_path.read_bytes())["tasks"].values())
    assert len(values) == 1
    return values[0]


@pytest.mark.parametrize("stage", ["enqueue", "step"])
def test_actual_queue_lock_retries_without_attempt_or_data_loss(case, monkeypatch, stage):
    col, calls = case
    before = col.state_path.read_bytes()
    lock = FileLock(col.lock.lock_file, timeout=0)
    enqueue = col.enqueue
    if stage == "enqueue":
        lock.acquire()
    else:
        def enter_step(*args, **kwargs):
            result = enqueue(*args, **kwargs)
            lock.acquire()
            monkeypatch.setattr(col, "enqueue", enqueue)
            return result
        monkeypatch.setattr(col, "enqueue", enter_step)
    retries, pauses, stopped = [], [], [False]
    def retry(detail):
        retries.append(detail)
        assert col.state_path.read_bytes() == before and calls == []
        assert task(col)["attempts"] == 0 and task(col)["snapshots"] == []
    def pause(seconds):
        pauses.append(seconds)
        if lock.is_locked:
            lock.release()
        else:
            stopped[0] = True
    try:
        col.run(stopped=lambda: stopped[0], pause=pause, on_retry=retry, max_requests=1)
    finally:
        if lock.is_locked:
            lock.release()
    assert retries == [{"reason": "queue_lock_contention", "error_type": "Timeout", "retry_after_seconds": 0.02}]
    assert pauses == [0.02, 1.0] and calls == [1]
    assert task(col)["status"] == "COMPLETE" and task(col)["attempts"] == 1
    assert len(task(col)["snapshots"]) == 1


def test_persistent_contention_remains_bounded_and_honors_stop(case):
    col, calls = case
    col.retry_seconds = 600
    pauses, retries, stopped = [], [], [False]
    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 2:
            stopped[0] = True
    with FileLock(col.lock.lock_file, timeout=0):
        col.run(stopped=lambda: stopped[0], pause=pause, on_retry=retries.append)
    assert pauses == [30.0, 30.0] and len(retries) == 2
    assert calls == [] and task(col)["attempts"] == 0


def test_stop_after_contention_does_not_pause_or_fetch(case):
    col, calls = case
    stopped = [False]
    def retry(detail):
        stopped[0] = True
    with FileLock(col.lock.lock_file, timeout=0):
        col.run(stopped=lambda: stopped[0], pause=lambda _: pytest.fail("Pause after stop"), on_retry=retry)
    assert calls == [] and task(col)["attempts"] == 0


@pytest.mark.parametrize("kind", ["wrong_lock", "runtime", "interrupted", "exit"])
def test_unrelated_failures_keep_original_exception(case, monkeypatch, kind):
    col, _ = case
    errors = {"wrong_lock": Timeout(str(col.root / "other.lock")), "runtime": RuntimeError("fixture"),
              "interrupted": InterruptedError("fixture"), "exit": KeyboardInterrupt("fixture")}
    error = errors[kind]
    def fail(**kwargs):
        raise error
    monkeypatch.setattr(col, "collect", fail)
    with pytest.raises(type(error)) as caught:
        col.run(stopped=lambda: False, pause=lambda _: pytest.fail("Unrelated error retried"),
                on_retry=lambda _: pytest.fail("Unrelated error misclassified"))
    assert caught.value is error


def test_task_lock_skips_only_its_owned_window(case, monkeypatch):
    col, calls = case
    key = next(iter(json.loads(col.state_path.read_bytes())["tasks"]))
    with FileLock(str(col.root / (key + ".lock")), timeout=0):
        assert col.step(max_requests=1) == []
    assert calls == [] and task(col)["attempts"] == 0
    error = Timeout(str(col.root / "receipt-index-other.lock"))
    monkeypatch.setattr(col, "_collect", lambda *a: (_ for _ in ()).throw(error))
    with pytest.raises(Timeout) as caught:
        col.step(max_requests=1)
    assert caught.value is error


def test_provider_timeout_is_receipted_without_queue_retry(case):
    col, calls = case
    def provider(*args):
        calls.append(1)
        raise Timeout(col.lock.lock_file)
    col.adapters["fmp"] = provider
    stopped, retries = [False], []
    col.run(stopped=lambda: stopped[0], pause=lambda _: stopped.__setitem__(0, True),
            on_retry=retries.append, max_requests=1)
    assert calls == [1] and retries == []
    saved = task(col)
    assert saved["status"] == "UNAVAILABLE" and saved["attempts"] == 1
    assert len(saved["snapshots"]) == 1


def test_postreceipt_queue_timeout_recovers_without_second_provider_call(case, monkeypatch):
    from ml.intraday import market_collection_index as index
    col, calls = case
    before = col.state_path.read_bytes()
    lock = FileLock(col.lock.lock_file, timeout=0, thread_local=False)
    original_index = index.index_receipt
    receipts, stopped, retries = [], [False], []
    def index_then_contend(root, path, snapshot):
        original_index(root, path, snapshot)
        if not receipts:
            receipts.append((path, path.read_bytes()))
            lock.acquire()
    monkeypatch.setattr(index, "index_receipt", index_then_contend)
    def retry(detail):
        retries.append(detail)
        assert col.state_path.read_bytes() == before
        assert calls == [1] and task(col)["attempts"] == 0
        assert receipts[0][0].read_bytes() == receipts[0][1]
    def pause(seconds):
        if lock.is_locked:
            lock.release()
        else:
            stopped[0] = True
    try:
        col.run(stopped=lambda: stopped[0], pause=pause, on_retry=retry, max_requests=1)
    finally:
        if lock.is_locked:
            lock.release()
    assert len(retries) == 1 and calls == [1]
    assert task(col)["status"] == "COMPLETE" and task(col)["attempts"] == 1
    assert len(task(col)["snapshots"]) == 1 and len(list(col.root.glob("snapshots/**/*.json"))) == 1
    assert receipts[0][0].read_bytes() == receipts[0][1]
    selected = index.selected_receipt_entries(col.root, symbol="DOCU", cutoff=NOW,
                                             since=START)
    assert len(selected) == 1 and selected[0]["sha256"] == json.loads(receipts[0][1])["snapshot_sha256"]
