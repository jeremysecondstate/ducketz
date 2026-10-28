"""Correlated native retry receipts never alter the owned write or its timing."""
from contextlib import contextmanager
from datetime import timedelta
import json
import socket
import sqlite3

import pytest

from ml.intraday import scout_services as candidate
from ml.intraday.prediction import digest, instant
from ml.intraday.service_handoff import ServiceStore


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Retry receipt fixture reached network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)


def writer_lock(store):
    locker = sqlite3.connect(store.path, timeout=0)
    locker.execute("BEGIN IMMEDIATE")
    return locker


def queue_case(tmp_path, sink=None):
    store = ServiceStore(tmp_path / "science.sqlite3", object_root=tmp_path / "objects")
    receipts = []
    def emit(value):
        receipts.append({"generation": 7, "loaded_release": {"manifest_sha256": "fixture-release"}, **value})
    queue = candidate.ServiceExecutionQueue(store, "manual-scout-15m:fixture", pc="Scout",
                                             event_sink=sink or emit)
    return store, queue, receipts


def test_native_queue_retry_receipts_bind_exact_floor_and_confirm_commit(tmp_path):
    store, queue, receipts = queue_case(tmp_path)
    now = instant("2026-10-28T17:00:00Z")
    floor = (now + timedelta(seconds=120)).isoformat()
    locker = writer_lock(store)
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            queue.broker_wait(now=now, seconds=120)
        for _ in range(2):
            with pytest.raises(sqlite3.OperationalError):
                queue.broker_due(now + timedelta(seconds=150))
        assert [row["event"] for row in receipts] == ["broker_wait_persistence_started"]
        assert receipts[0]["busy_count"] == 1
        assert caught.value.sqlite_errorcode & 0xff == sqlite3.SQLITE_BUSY
    finally:
        locker.rollback(); locker.close()
    assert queue.broker_due(now + timedelta(seconds=30)) is False
    with store.connect() as db:
        assert db.execute("SELECT next_attempt_at FROM intraday_live_retry").fetchone() == (floor,)
    start, recovered = receipts
    assert recovered["event"] == "broker_wait_persistence_recovered"
    assert start["operation_id"] == recovered["operation_id"]
    assert recovered["pending_sha256"] == digest({"operation": "broker_retry_floor", "scope": "broker",
                                                   "next_attempt_at": floor})
    assert recovered["next_attempt_at"] == floor and recovered["busy_count"] == 3
    assert recovered["sqlite_errorcode"] & 0xff == sqlite3.SQLITE_BUSY
    assert recovered["sqlite_errorname"] == "SQLITE_BUSY"
    assert recovered["logical_run_id"] == queue.session_id and recovered["generation"] == 7
    assert recovered["loaded_release"]["manifest_sha256"] == "fixture-release"
    assert queue._pending_broker_wait is None and queue._broker_write_retry is None
    assert queue.broker_due(now + timedelta(seconds=120)) is True and len(receipts) == 2


def test_extended_pending_floor_supersedes_episode_without_false_recovery(tmp_path):
    store, queue, receipts = queue_case(tmp_path)
    now = instant("2026-10-28T17:00:00Z")
    locker = writer_lock(store)
    try:
        with pytest.raises(sqlite3.OperationalError): queue.broker_wait(now=now, seconds=60)
        with pytest.raises(sqlite3.OperationalError): queue.broker_wait(now=now, seconds=120)
    finally:
        locker.rollback(); locker.close()
    assert queue.broker_due(now + timedelta(seconds=1)) is False
    assert [row["event"] for row in receipts] == ["broker_wait_persistence_started",
        "broker_wait_persistence_superseded", "broker_wait_persistence_started", "broker_wait_persistence_recovered"]
    first, superseded, second, recovered = receipts
    assert first["operation_id"] == superseded["operation_id"]
    assert first["operation_id"] != second["operation_id"] == recovered["operation_id"]
    assert first["next_attempt_at"] == (now + timedelta(seconds=60)).isoformat()
    assert superseded["replacement_next_attempt_at"] == recovered["next_attempt_at"]
    assert recovered["next_attempt_at"] == (now + timedelta(seconds=120)).isoformat()
    assert recovered["pending_sha256"] != first["pending_sha256"]


def test_queue_diagnostic_io_failure_cannot_replay_committed_sql(tmp_path, monkeypatch):
    def broken_sink(value): raise OSError("fixture receipt disk unavailable")
    store, queue, _ = queue_case(tmp_path, broken_sink)
    now = instant("2026-10-28T17:00:00Z")
    locker = writer_lock(store)
    try:
        with pytest.raises(sqlite3.OperationalError): queue.broker_wait(now=now, seconds=120)
    finally:
        locker.rollback(); locker.close()
    statements = []
    connect = store.connect
    @contextmanager
    def traced_connect():
        with connect() as db:
            db.set_trace_callback(statements.append)
            yield db
    monkeypatch.setattr(store, "connect", traced_connect)
    assert queue.broker_due(now + timedelta(seconds=1)) is False
    assert queue.broker_due(now + timedelta(seconds=150)) is True
    assert sum(sql.startswith("INSERT INTO intraday_live_retry") for sql in statements) == 1
    assert queue._pending_broker_wait is None and queue._broker_write_retry is None


def test_queue_uncontended_and_nonbusy_writes_emit_no_retry_receipt(tmp_path):
    store, queue, receipts = queue_case(tmp_path)
    now = instant("2026-10-28T17:00:00Z")
    queue.broker_wait(now=now, seconds=3)
    assert receipts == []
    with store.connect() as db: db.execute("DROP TABLE intraday_live_retry")
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        queue.broker_wait(now=now, seconds=30)
    assert receipts == []


def model_case(tmp_path, pause):
    binding = {"logical_run_id": "manual-scout-15m:fixture", "generation": 4,
               "manifest_sha256": "fixture-model-release"}
    store = candidate._ModelServiceStore(tmp_path / "science.sqlite3", object_root=tmp_path / "objects",
        persistence_stop_reason=lambda: None, persistence_pause=pause,
        persistence_diagnostic_binding=binding)
    store.run_binding = {"logical_run_id": binding["logical_run_id"], "original_execution_deadline": "fixture"}
    store.begin_cycle("cycle", {"pc": "Scout", "status": "running", "symbols": ["DOCU"],
                                "started_at": "2026-10-28T17:15:35+00:00"})
    return store


def model_receipts(tmp_path):
    return [json.loads(line) for line in (tmp_path / "objects/model-metrics.jsonl").read_text().splitlines()]


def test_native_model_event_retry_has_bounded_receipts_and_immutable_digest(tmp_path):
    waits, lockers = [], []
    def pause(seconds):
        waits.append(seconds)
        if len(waits) == 2: lockers[0].rollback()
    store = model_case(tmp_path, pause)
    binding_before = dict(store.run_binding)
    lockers.append(writer_lock(store))
    payload = {"reason": "system_1a_handoff_busy", "recorded_at": "2026-10-28T17:15:35+00:00",
               "retryable": True, "diagnostics": {"retained": "private-original"}}
    recorded = "2026-10-28T17:15:36+00:00"
    try:
        store.event("cycle", "symbol_failure", payload, symbol="DOCU", recorded_at=recorded)
    finally:
        lockers[0].rollback(); lockers[0].close()
    start, recovered = model_receipts(tmp_path)
    assert start["phase"] == "failure_persistence_started"
    assert recovered["phase"] == "failure_persistence_recovered" and recovered["busy_count"] == 2
    assert start["operation_id"] == recovered["operation_id"]
    assert recovered["pending_sha256"] == digest({"operation": "symbol_failure_event", "cycle_id": "cycle",
        "symbol": "DOCU", "kind": "symbol_failure", "recorded_at": recorded, "payload": payload})
    assert recovered["original_recorded_at"] == recorded and recovered["symbol"] == "DOCU"
    assert recovered["generation"] == 4 and recovered["manifest_sha256"] == "fixture-model-release"
    assert recovered["logical_run_id"] == store.run_binding["logical_run_id"]
    assert recovered["sqlite_errorcode"] & 0xff == sqlite3.SQLITE_BUSY
    assert recovered["sqlite_errorname"] == "SQLITE_BUSY"
    assert "payload" not in recovered and "private-original" not in json.dumps(recovered)
    assert waits == [.2, .2] and store.run_binding == binding_before
    with store.connect() as db:
        rows = db.execute("SELECT recorded_at,payload FROM events WHERE kind='symbol_failure'").fetchall()
    assert rows == [(recorded, candidate.encoded(payload))]


def test_model_diagnostic_failure_after_commit_does_not_repeat_original_event(tmp_path, monkeypatch):
    lockers, writes, attempted_receipts = [], [], []
    def pause(seconds): lockers[0].rollback()
    store = model_case(tmp_path, pause)
    original = ServiceStore.event
    def counted_event(self, *args, **kwargs):
        writes.append(args[1])
        return original(self, *args, **kwargs)
    def broken_metric(root, role, phase, **values):
        if role == "model":
            attempted_receipts.append(phase)
            raise OSError("fixture diagnostics unavailable")
    monkeypatch.setattr(ServiceStore, "event", counted_event)
    monkeypatch.setattr(candidate, "metric", broken_metric)
    lockers.append(writer_lock(store))
    try:
        store.event("cycle", "symbol_failure", {"reason": "original"}, symbol="DOCU")
    finally:
        lockers[0].rollback(); lockers[0].close()
    assert writes == ["symbol_failure", "symbol_failure"]  # One BUSY, one successful commit.
    assert attempted_receipts == ["failure_persistence_started", "failure_persistence_recovered"]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='symbol_failure'").fetchone() == (1,)


def test_native_model_cycle_update_receipt_binds_exact_original_changes(tmp_path):
    lockers = []
    store = model_case(tmp_path, lambda seconds: lockers[0].rollback())
    changes = {"failures": {"DOCU": {"reason": "system_1a_snapshot_pending", "retryable": True}},
               "pending_recovery_symbols": ["DOCU"]}
    lockers.append(writer_lock(store))
    try: result = store.update_cycle("cycle", changes)
    finally:
        lockers[0].rollback(); lockers[0].close()
    start, recovered = model_receipts(tmp_path)
    assert recovered["operation"] == "model_failure_cycle_update" and recovered["cycle_id"] == "cycle"
    assert recovered["pending_sha256"] == digest({"operation": "model_failure_cycle_update",
                                                   "cycle_id": "cycle", "changes": changes})
    assert start["operation_id"] == recovered["operation_id"]
    assert recovered["busy_count"] == 1 and result["failures"] == changes["failures"]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='cycle_progress'").fetchone() == (1,)
