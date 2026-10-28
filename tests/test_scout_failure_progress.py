"""Failure progress retries its event after the cycle row already committed."""
from contextlib import contextmanager
from copy import deepcopy
import json
import socket
import sqlite3

import pytest

from ml.intraday import scout_services as candidate, store as base_store
from ml.intraday.prediction import digest
from ml.intraday.service_handoff import ServiceStore


ORIGINAL_CLOCK = "2026-10-28T17:15:36+00:00"
LATER_CLOCK = "2026-10-28T17:16:36+00:00"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Failure progress fixture reached network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)


def changes():
    return {"failures": {"DOCU": {"reason": "system_1a_snapshot_pending", "retryable": True}},
            "pending_recovery_symbols": ["DOCU"]}


def case(tmp_path, monkeypatch, *, pause, stop_reason=lambda: None):
    store = candidate._ModelServiceStore(tmp_path / "science.sqlite3", object_root=tmp_path / "objects",
        persistence_stop_reason=stop_reason, persistence_pause=pause,
        persistence_diagnostic_binding={"logical_run_id": "manual-scout-15m:fixture",
                                        "generation": 4, "manifest_sha256": "fixture-release"})
    store.connection_timeout = 0
    store.run_binding = {"logical_run_id": "manual-scout-15m:fixture"}
    store.begin_cycle("cycle", {"pc": "Scout", "symbols": ["DOCU"], "status": "running",
                                "started_at": ORIGINAL_CLOCK})
    clock, statements = [ORIGINAL_CLOCK], []
    monkeypatch.setattr(candidate, "utcnow", lambda: clock[0])
    monkeypatch.setattr(base_store, "utcnow", lambda: clock[0])
    connect = store.connect
    @contextmanager
    def traced_connect():
        with connect() as db:
            db.set_trace_callback(statements.append)
            yield db
    monkeypatch.setattr(store, "connect", traced_connect)
    return store, clock, statements


def lock_before_progress(store, monkeypatch):
    event, lockers, calls = store.event, [], []
    def locked_event(cycle_id, kind, payload, **kwargs):
        if kind == "cycle_progress":
            calls.append(deepcopy(payload))
            # This is the real second transaction boundary: the failure row
            # must already have committed before its event contends.
            with store.connect() as db:
                saved = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?", (cycle_id,)).fetchone()[0])
            assert saved["failures"] == payload["failures"]
            if not lockers:
                locker = sqlite3.connect(store.path, timeout=0)
                locker.execute("BEGIN IMMEDIATE")
                lockers.append(locker)
        return event(cycle_id, kind, payload, **kwargs)
    monkeypatch.setattr(store, "event", locked_event)
    return lockers, calls


def rows(store):
    with store.connect() as db:
        cycle = json.loads(db.execute("SELECT payload FROM cycles WHERE id='cycle'").fetchone()[0])
        events = db.execute("SELECT recorded_at,payload FROM events WHERE kind='cycle_progress'").fetchall()
    return cycle, events


def receipts(store):
    path = store.object_root / "model-metrics.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def close(lockers):
    for locker in lockers:
        locker.rollback()
        locker.close()


def test_native_busy_after_failure_row_commit_retries_only_original_progress_event(tmp_path, monkeypatch):
    original, waits, lockers = changes(), [], []
    frozen = deepcopy(original)
    def pause(seconds):
        waits.append(seconds)
        original["failures"]["DOCU"]["reason"] = "caller mutation"
        clock[0] = LATER_CLOCK
        lockers[0].rollback()
    store, clock, statements = case(tmp_path, monkeypatch, pause=pause)
    lockers, calls = lock_before_progress(store, monkeypatch)
    try:
        result = store.update_cycle("cycle", original)
    finally:
        close(lockers)
    cycle, events = rows(store)
    assert waits == [.2] and calls == [frozen]
    assert cycle["failures"] == result["failures"] == frozen["failures"]
    assert events == [(ORIGINAL_CLOCK, candidate.encoded(frozen))]
    assert sum(sql.startswith("UPDATE cycles SET payload=") for sql in statements) == 1
    start, recovered = receipts(store)
    assert [start["phase"], recovered["phase"]] == ["failure_persistence_started", "failure_persistence_recovered"]
    assert start["operation_id"] == recovered["operation_id"]
    assert recovered["operation"] == "model_failure_progress_event"
    assert recovered["pending_sha256"] == digest({"operation": "model_failure_progress_event", "cycle_id": "cycle",
        "symbol": None, "kind": "cycle_progress", "recorded_at": ORIGINAL_CLOCK, "payload": frozen})
    assert recovered["original_recorded_at"] == ORIGINAL_CLOCK and recovered["busy_count"] == 1
    assert recovered["sqlite_errorcode"] & 0xff == sqlite3.SQLITE_BUSY
    assert recovered["generation"] == 4 and recovered["manifest_sha256"] == "fixture-release"


@pytest.mark.parametrize("reason", ["manual_stop", "session_end"])
@pytest.mark.parametrize("stop_after_pause", [False, True])
def test_native_progress_stop_preserves_committed_failure_and_original_pending_event(
        tmp_path, monkeypatch, reason, stop_after_pause):
    stop, waits, lockers = [None if stop_after_pause else reason], [], []
    def pause(seconds):
        waits.append(seconds)
        lockers[0].rollback()
        clock[0] = LATER_CLOCK
        stop[0] = reason
    store, clock, statements = case(tmp_path, monkeypatch, pause=pause, stop_reason=lambda: stop[0])
    lockers, calls = lock_before_progress(store, monkeypatch)
    try:
        with pytest.raises(candidate._ModelFailurePersistenceStopped) as caught:
            store.update_cycle("cycle", changes())
    finally:
        close(lockers)
    cycle, events = rows(store)
    assert caught.value.reason == reason and isinstance(caught.value.__cause__, sqlite3.OperationalError)
    assert waits == ([.2] if stop_after_pause else []) and calls == [changes()]
    assert cycle["failures"] == changes()["failures"] and events == []
    assert sum(sql.startswith("UPDATE cycles SET payload=") for sql in statements) == 1
    started, stopped = receipts(store)
    assert [started["phase"], stopped["phase"]] == ["failure_persistence_started", "failure_persistence_stopped"]
    assert stopped["stop_reason"] == reason
    assert stopped["pending_write"] == {"operation": "model_failure_progress_event", "cycle_id": "cycle",
        "symbol": None, "kind": "cycle_progress", "recorded_at": ORIGINAL_CLOCK, "payload": changes()}


def test_nonbusy_progress_error_remains_fatal_after_committed_failure_row(tmp_path, monkeypatch):
    waits = []
    store, _, statements = case(tmp_path, monkeypatch, pause=waits.append)
    event = ServiceStore.event
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode, error.sqlite_errorname = sqlite3.SQLITE_IOERR, "SQLITE_IOERR"
    def failed_event(self, cycle_id, kind, payload, **kwargs):
        if kind == "cycle_progress":
            raise error
        return event(self, cycle_id, kind, payload, **kwargs)
    monkeypatch.setattr(ServiceStore, "event", failed_event)
    with pytest.raises(sqlite3.OperationalError) as caught:
        store.update_cycle("cycle", changes())
    assert caught.value is error and waits == [] and receipts(store) == []
    cycle, events = rows(store)
    assert cycle["failures"] == changes()["failures"] and events == []
    assert sum(sql.startswith("UPDATE cycles SET payload=") for sql in statements) == 1


def test_ordinary_progress_retains_base_native_busy_behavior(tmp_path, monkeypatch):
    waits, lockers = [], []
    store, _, statements = case(tmp_path, monkeypatch, pause=waits.append)
    event = store.event
    def locked_event(cycle_id, kind, payload, **kwargs):
        if kind == "cycle_progress":
            locker = sqlite3.connect(store.path, timeout=0)
            locker.execute("BEGIN IMMEDIATE")
            lockers.append(locker)
        return event(cycle_id, kind, payload, **kwargs)
    monkeypatch.setattr(store, "event", locked_event)
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            store.update_cycle("cycle", {"attempt_count": 2})
    finally:
        close(lockers)
    assert caught.value.sqlite_errorcode & 0xff == sqlite3.SQLITE_BUSY
    assert waits == [] and receipts(store) == []
    cycle, events = rows(store)
    assert cycle["attempt_count"] == 2 and events == []
    assert sum(sql.startswith("UPDATE cycles SET payload=") for sql in statements) == 1


def test_progress_diagnostic_failure_cannot_replay_committed_row_or_event(tmp_path, monkeypatch):
    waits, lockers, attempted = [], [], []
    def pause(seconds):
        waits.append(seconds)
        lockers[0].rollback()
    store, _, statements = case(tmp_path, monkeypatch, pause=pause)
    lockers, calls = lock_before_progress(store, monkeypatch)
    metric = candidate.metric
    def broken_metric(root, role, phase, **values):
        if role == "model":
            attempted.append(phase)
            raise OSError("fixture diagnostics unavailable")
        return metric(root, role, phase, **values)
    monkeypatch.setattr(candidate, "metric", broken_metric)
    try:
        store.update_cycle("cycle", changes())
    finally:
        close(lockers)
    assert waits == [.2] and calls == [changes()]
    assert attempted == ["failure_persistence_started", "failure_persistence_recovered"]
    assert rows(store)[1] == [(ORIGINAL_CLOCK, candidate.encoded(changes()))]
    assert sum(sql.startswith("UPDATE cycles SET payload=") for sql in statements) == 1
    assert sum(sql.startswith("INSERT INTO events") for sql in statements) == 2  # One BUSY, one commit.
