"""Offline receipt/retry contention; no providers, application state or trading."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import multiprocessing
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from ml.intraday.market_inputs import MarketInputStore
from ml.intraday.provider_recovery import ProviderRequestError, RetryLedger
from ml.intraday.market_input_worker import _failure_details, run_market_input_worker


NOW = datetime(2026, 10, 19, 15, 0, tzinfo=timezone.utc)


def _receipt(store, symbol):
    value = {"symbol": symbol, "source": "fmp_quote", "received_at": NOW.isoformat(),
             "status": "ok", "features": {"price": 10.0}}
    raw = json.dumps(value, sort_keys=True).encode()
    identity = hashlib.sha256(raw).hexdigest()
    path = store.root / "objects" / (identity + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    item = (NOW, identity, "ok", value["features"], str(path.relative_to(store.root)))
    info = path.stat()
    return item, (identity, info.st_mtime_ns, info.st_size), value


def test_receipt_eviction_cannot_invalidate_an_in_progress_cache_hit(tmp_path):
    store = MarketInputStore(tmp_path)
    first, key, expected = _receipt(store, "AAPL")
    second, _, _ = _receipt(store, "AMZN")
    hit, eviction_started, eviction_finished = (threading.Event() for _ in range(3))

    class InterleavedCache(OrderedDict):
        def __contains__(self, candidate):
            present = super().__contains__(candidate)
            if candidate == key:
                hit.set()
                assert eviction_started.wait(5)
                # Old code lets the second verifier evict this key here. With
                # synchronization it proceeds after this verifier keeps a ref.
                eviction_finished.wait(.1)
            return present

    store._verified = InterleavedCache([(key, expected)])
    store._verified.update((str(number), {}) for number in range(4095))

    def evict():
        assert hit.wait(5)
        eviction_started.set()
        try:
            return store._verify_receipt(second, "AMZN", "fmp_quote")
        finally:
            eviction_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first_result = pool.submit(store._verify_receipt, first, "AAPL", "fmp_quote")
        second_result = pool.submit(evict)
        assert first_result.result(timeout=5) == expected
        assert second_result.result(timeout=5)["symbol"] == "AMZN"
    assert len(store._verified) == 4096
    assert key not in dict(store._verified)


def test_separate_instances_merge_different_keys_without_locking_requests(tmp_path):
    path = tmp_path / "retry.json"
    first, second = RetryLedger(path, clock=lambda: NOW), RetryLedger(path, clock=lambda: NOW)
    assert first.state("first") == second.state("second") == {}
    requests_started = threading.Barrier(2)
    events = []

    def request(value):
        requests_started.wait(timeout=5)
        return value

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first.run, "first", lambda: request("a"), events.append, max_attempts=1)
        b = pool.submit(second.run, "second", lambda: request("b"), events.append, max_attempts=1)
        assert a.result(timeout=5)["status"] == b.result(timeout=5)["status"] == "ok"
    assert set(json.loads(path.read_text())) == {"first", "second"}
    assert first.state("second")["attempt_count"] == second.state("first")["attempt_count"] == 1
    assert [event["attempt_number"] for event in events] == [1, 1]


def test_same_key_coalesces_and_preserves_attempts_and_retry_after(tmp_path):
    now = [NOW]
    first = RetryLedger(tmp_path / "retry.json", clock=lambda: now[0])
    second = RetryLedger(tmp_path / "nested" / ".." / "retry.json", clock=lambda: now[0])
    assert first.state("job") == second.state("job") == {}
    started, release = threading.Event(), threading.Event()
    events, duplicate_calls = [], []

    def unavailable():
        started.set()
        assert release.wait(5)
        raise ProviderRequestError(status=429, retry_after="30", cause="fixture throttled")

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(first.run, "job", unavailable, events.append, max_attempts=1)
        assert started.wait(5)
        try:
            result = second.run("job", lambda: duplicate_calls.append(True), events.append, max_attempts=1)
            assert result["status"] == "deferred" and result["attempts"] == 0
            assert duplicate_calls == []
        finally:
            release.set()
        assert running.result(timeout=5)["status"] == "retry_pending"
    due = (NOW + timedelta(seconds=30)).isoformat()
    assert second.state("job")["next_retry_at"] == due
    assert second.run("job", lambda: "unused", events.append, max_attempts=1)["next_retry_at"] == due
    now[0] += timedelta(seconds=31)
    assert second.run("job", lambda: "recovered", events.append, max_attempts=1)["attempt_number"] == 2
    first.reset("job")
    assert first.run("job", lambda: "changed scope", events.append, max_attempts=1)["attempt_number"] == 3
    second.defer("job", seconds=12)
    assert first.state("job")["attempt_count"] == 3
    assert first.state("job")["next_retry_at"] == (now[0] + timedelta(seconds=12)).isoformat()
    assert [event["attempt_number"] for event in events] == [1, 2, 3]


def test_defer_and_due_keys_observe_other_instance_without_losing_rejection(tmp_path):
    now = [NOW]
    path = tmp_path / "retry.json"
    first, second = (RetryLedger(path, clock=lambda: now[0]) for _ in range(2))
    assert first.due_keys() == second.due_keys() == ()

    def rejected():
        raise ProviderRequestError(status=403, cause="fixture forbidden")

    first.run("rejected", rejected, lambda event: None, max_attempts=1)
    second.defer("partition", seconds=12)
    now[0] += timedelta(seconds=13)
    assert first.due_keys() == ("partition",)
    assert second.state("rejected")["status"] == "request_rejected"
    assert second.run("rejected", lambda: pytest.fail("permanent rejection retried"),
                      lambda event: None, max_attempts=1)["attempts"] == 0


def test_stop_and_attempt_callback_failure_release_only_the_request_lock(tmp_path):
    ledger = RetryLedger(tmp_path / "retry.json", clock=lambda: NOW)
    stop = threading.Event()
    stop.set()
    assert ledger.run("job", lambda: pytest.fail("manual stop ignored"), lambda event: None,
                      stop_event=stop, max_attempts=1) == {"status": "stopped", "attempts": 0}

    def broken_receipt(event):
        raise OSError("fixture receipt write failure")

    with pytest.raises(OSError, match="fixture receipt"):
        ledger.run("job", lambda: "ok", broken_receipt, max_attempts=1)
    assert ledger.run("job", lambda: "ok", lambda event: None, max_attempts=1)["status"] == "ok"


def test_retry_wait_preserves_provider_delay_and_allows_another_key(tmp_path):
    now = [NOW]
    path = tmp_path / "retry.json"
    other = RetryLedger(path, clock=lambda: now[0])
    delays, events = [], []

    def wait(seconds):
        delays.append(seconds)
        assert other.run("other", lambda: "ok", lambda event: None, max_attempts=1)["status"] == "ok"
        now[0] += timedelta(seconds=seconds)
        return False

    ledger = RetryLedger(path, clock=lambda: now[0], wait=wait)

    def request():
        if not events:
            raise ProviderRequestError(status=429, retry_after="30", cause="fixture throttled")
        return "recovered"

    assert ledger.run("job", request, events.append, max_attempts=2)["attempts"] == 2
    assert delays == [30.0]
    assert [event["attempt_number"] for event in events] == [1, 2]
    assert set(json.loads(path.read_text())) == {"job", "other"}


def _process_write(path, key, barrier):
    ledger = RetryLedger(path, clock=lambda: NOW)
    ledger.state(key)
    barrier.wait(timeout=10)
    for index in range(4):
        ledger.defer(key + str(index), seconds=2)


def test_processes_preserve_all_keys_in_the_original_journal(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = tmp_path / "retry.json"
    barrier = context.Barrier(2)
    workers = [context.Process(target=_process_write, args=(str(path), key, barrier)) for key in ("a", "b")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=20)
        assert not worker.is_alive() and worker.exitcode == 0
    assert set(json.loads(path.read_text())) == {key + str(index) for key in ("a", "b") for index in range(4)}
    assert not list(tmp_path.glob("*.tmp"))


def _process_hold_request(path, started, release):
    ledger = RetryLedger(path, clock=lambda: NOW)

    def request():
        started.set()
        assert release.wait(10)
        return "original"

    assert ledger.run("job", request, lambda event: None, max_attempts=1)["attempt_number"] == 1


def test_same_key_is_coalesced_between_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    started, release = context.Event(), context.Event()
    path = tmp_path / "retry.json"
    worker = context.Process(target=_process_hold_request, args=(str(path), started, release))
    worker.start()
    try:
        assert started.wait(10)
        ledger = RetryLedger(path, clock=lambda: NOW)
        result = ledger.run("job", lambda: pytest.fail("concurrent process duplicated request"),
                            lambda event: None, max_attempts=1)
        assert result["status"] == "deferred" and result["attempts"] == 0
    finally:
        release.set()
        worker.join(timeout=20)
    assert not worker.is_alive() and worker.exitcode == 0
    assert ledger.run("job", lambda: "next", lambda event: None, max_attempts=1)["attempt_number"] == 2


def test_failed_collection_retains_original_reference_across_new_quarter():
    now, calls, events, pauses, recoveries = [NOW], [], [], [], []
    secret = "fixture-private-fmp-value"

    class Collector:
        fmp = SimpleNamespace(api_key=secret)

        def collect(self, reference):
            calls.append(reference)
            if len(calls) == 1:
                try:
                    cause = sqlite3.OperationalError("database is locked " + secret)
                    cause.sqlite_errorcode, cause.sqlite_errorname = sqlite3.SQLITE_BUSY, "SQLITE_BUSY"
                    raise cause
                except sqlite3.OperationalError as exc:
                    raise RuntimeError("fixture catalog append https://fixture.test/?apikey=" + secret) from exc
            return {"reference_at": reference.isoformat()}

        def recover_pending(self):
            recoveries.append(True)
            return None

    def pause(seconds):
        pauses.append(seconds)
        now[0] += timedelta(minutes=15)

    run_market_input_worker(Collector(), clock=lambda: now[0], stopped=lambda: len(pauses) == 5,
        pause=pause, reference_for=lambda value: value, event=lambda kind, payload: events.append((kind, payload)))
    assert calls == [NOW, NOW, NOW + timedelta(minutes=15)]
    assert pauses == [1] * 5 and len(recoveries) == 2
    error = events[0][1]
    assert error["reference_at"] == NOW.isoformat() and error["collection_pending"] is True
    assert error["operation"] == "collect" and error["stage"] == "collector_call"
    assert [item["error_type"] for item in error["exception_chain"]] == ["RuntimeError", "OperationalError"]
    assert error["exception_chain"][1]["sqlite_errorcode"] == sqlite3.SQLITE_BUSY
    assert error["exception_chain"][1]["sqlite_errorname"] == "SQLITE_BUSY"
    assert "database is locked" in error["exception_chain"][1]["message"]
    assert error["exception_chain"][1]["frames"][-1]["function"] == "collect"
    assert secret not in json.dumps(events) and "https://" not in json.dumps(events)
    assert [kind for kind, _ in events] == ["market_inputs_error", "market_inputs_collected", "market_inputs_collected"]


def test_persistent_old_collection_failure_rotates_behind_new_quarter():
    now, calls, events, pauses, recoveries = [NOW], [], [], [], []
    newer = NOW + timedelta(minutes=15)

    def collect(reference):
        calls.append(reference)
        if reference == NOW:
            raise sqlite3.OperationalError("fixture old reference remains unavailable")
        return {"reference_at": reference.isoformat()}

    collector = SimpleNamespace(collect=collect, recover_pending=lambda: recoveries.append(True) or None)

    def pause(seconds):
        pauses.append(seconds)
        now[0] = newer

    run_market_input_worker(collector, clock=lambda: now[0], stopped=lambda: len(pauses) == 7,
        pause=pause, reference_for=lambda value: value, event=lambda kind, payload: events.append((kind, payload)))
    assert calls == [NOW, NOW, newer, NOW]
    assert [payload["reference_at"] for kind, payload in events if kind == "market_inputs_collected"] == [newer.isoformat()]
    assert events[-1][0] == "market_inputs_error"
    assert events[-1][1]["reference_at"] == NOW.isoformat()
    assert events[-1][1]["collection_pending"] is True
    assert events[-1][1]["pending_collection_count"] == 1
    assert pauses == [1] * 7 and len(recoveries) == 3


def test_durable_jobs_progress_between_persistent_collection_failures():
    operations, events, pauses = [], [], []

    def collect(reference):
        operations.append("collect")
        raise sqlite3.OperationalError("fixture collection catalog unavailable")

    def recover():
        operations.append("recover")
        return {"status": "completed", "receipts": [{"id": "fixture-retained-retry-receipt"}]}

    collector = SimpleNamespace(collect=collect, recover_pending=recover)
    run_market_input_worker(collector, clock=lambda: NOW, stopped=lambda: len(pauses) == 4,
        pause=pauses.append, reference_for=lambda value: value,
        event=lambda kind, payload: events.append((kind, payload)))
    assert operations == ["collect", "recover", "collect", "recover"]
    assert [kind for kind, _ in events] == ["market_inputs_error", "market_inputs_retry_completed",
                                          "market_inputs_error", "market_inputs_retry_completed"]
    assert all(payload["collection_pending"] for kind, payload in events if kind == "market_inputs_error")
    assert pauses == [1] * 4


def test_collection_event_failure_does_not_repeat_successful_collection():
    calls, recovered, events, pauses = [], [], [], []
    collector = SimpleNamespace(collect=lambda reference: calls.append(reference) or {"ok": True},
        recover_pending=lambda: recovered.append(True) or None)

    def event(kind, payload):
        if kind == "market_inputs_collected":
            raise sqlite3.OperationalError("fixture event catalog unavailable")
        events.append((kind, payload))

    run_market_input_worker(collector, clock=lambda: NOW, stopped=lambda: len(pauses) == 2,
        pause=pauses.append, reference_for=lambda now: now, event=event)
    assert calls == [NOW] and recovered == [True] and pauses == [1, 1]
    assert events[0][1]["stage"] == "event_write"
    assert events[0][1]["collection_pending"] is False
    assert "fixture event catalog unavailable" in events[0][1]["message"]


def test_manual_stop_prevents_failed_collection_retry():
    calls, events = [], []
    stop = threading.Event()

    def collect(reference):
        calls.append(reference)
        stop.set()
        raise sqlite3.OperationalError("fixture catalog unavailable")

    collector = SimpleNamespace(collect=collect, recover_pending=lambda: pytest.fail("stopped worker ran"))
    run_market_input_worker(collector, clock=lambda: NOW, stopped=stop.is_set, pause=lambda seconds: None,
        reference_for=lambda now: now, event=lambda kind, payload: events.append((kind, payload)))
    assert calls == [NOW] and len(events) == 1
    assert events[0][1]["collection_pending"] is True


def test_worker_exception_chain_is_bounded_and_keeps_implicit_context():
    error = None
    for index in range(6):
        try:
            if error is not None:
                raise error
            raise OSError("first fixture failure")
        except Exception:
            try:
                raise RuntimeError("fixture failure " + str(index))
            except RuntimeError as exc:
                error = exc
    detail = _failure_details(error, NOW)
    assert len(detail["exception_chain"]) == 4
    assert detail["exception_chain_truncated"] is True
    assert all(len(item["frames"]) <= 8 for item in detail["exception_chain"])
    assert detail["exception_chain"][0]["message"] == "fixture failure 5"
