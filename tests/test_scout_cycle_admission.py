"""Scout cycle admission retries exact writes without replaying model work."""
from copy import deepcopy
from datetime import timedelta
import json
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import runtime, scout_services as candidate
from ml.intraday.data import AcquisitionPending
from ml.intraday.prediction import digest, instant
from ml.intraday.runtime import ResearchConfig, ShadowRunner
from ml.intraday.service_handoff import ServiceStore


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Cycle admission fixture reached network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)


def make_store(tmp_path, *, pause, stop_reason=lambda: None):
    store = candidate._ModelServiceStore(tmp_path / "science.sqlite3", object_root=tmp_path / "objects",
        persistence_stop_reason=stop_reason, persistence_pause=pause,
        persistence_diagnostic_binding={"logical_run_id": "manual-scout-15m:fixture", "generation": 8,
                                        "manifest_sha256": "fixture-release"})
    store.run_binding = {"logical_run_id": "manual-scout-15m:fixture"}
    return store


def writer_lock(store):
    locker = sqlite3.connect(store.path, timeout=0, check_same_thread=False)
    locker.execute("BEGIN IMMEDIATE")
    return locker


def initial_payload():
    return {"pc": "Scout", "symbols": ["DOCU"], "mode": "live", "status": "running",
            "configuration_hash": "fixture", "reference_at": "2026-10-28T17:00:00+00:00",
            "started_at": "2026-10-28T17:15:35+00:00", "training_cutoff": "2026-10-28T17:15:35+00:00",
            "configuration": {"models": ["fixture"], "recipe": {"retained": [1, 2]}}}


def receipts(store):
    path = store.object_root / "model-metrics.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def saved_rows(store):
    with store.connect() as db:
        cycles = db.execute("SELECT id,started_at,payload FROM cycles").fetchall()
        starts = db.execute("SELECT cycle_id,recorded_at,payload FROM events WHERE kind='cycle_started'").fetchall()
    return cycles, starts


def test_native_begin_contention_preserves_original_deep_payload_and_single_start(tmp_path, monkeypatch):
    original = initial_payload()
    frozen = deepcopy(original)
    waits, lockers = [], []
    def pause(seconds):
        waits.append(seconds)
        original["configuration"]["recipe"]["retained"].append("caller-change")
        original["training_cutoff"] = "2026-10-28T18:00:00+00:00"
        lockers[0].rollback()
    store = make_store(tmp_path, pause=pause)
    recorded = "2026-10-28T17:15:36+00:00"
    monkeypatch.setattr(candidate, "utcnow", lambda: recorded)
    lockers.append(writer_lock(store))
    try:
        result = store.begin_cycle("cycle", original)
    finally:
        lockers[0].rollback(); lockers[0].close()
    cycles, starts = saved_rows(store)
    assert waits == [.2] and result == frozen
    assert cycles == [("cycle", frozen["started_at"], candidate.encoded(frozen))]
    assert starts == [("cycle", recorded, candidate.encoded(frozen))]
    assert store.begin_cycle("cycle", deepcopy(frozen)) == frozen
    assert saved_rows(store) == (cycles, starts)
    start, recovered = receipts(store)
    assert [start["phase"], recovered["phase"]] == ["failure_persistence_started", "failure_persistence_recovered"]
    assert start["operation_id"] == recovered["operation_id"]
    assert recovered["operation"] == "model_cycle_begin" and recovered["busy_count"] == 1
    assert recovered["pending_sha256"] == digest({"operation": "model_cycle_begin", "cycle_id": "cycle", "payload": frozen})
    assert recovered["sqlite_errorcode"] & 0xff == sqlite3.SQLITE_BUSY
    assert recovered["generation"] == 8 and recovered["manifest_sha256"] == "fixture-release"


def test_native_busy_after_cycle_commit_retries_original_start_event_only(tmp_path, monkeypatch):
    original = initial_payload()
    frozen = deepcopy(original)
    waits, lockers, event_calls = [], [], []
    clocks = ["2026-10-28T17:15:36+00:00"]
    def pause(seconds):
        waits.append(seconds)
        clocks[0] = "2026-10-28T18:10:00+00:00"
        original["configuration"]["recipe"]["retained"].append("caller-change")
        lockers[0].rollback()
    store = make_store(tmp_path, pause=pause)
    monkeypatch.setattr(candidate, "utcnow", lambda: clocks[0])
    original_event = store.event
    def lock_before_start(cycle_id, kind, payload, **kwargs):
        event_calls.append(kind)
        if kind == "cycle_started":
            with store.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM cycles").fetchone() == (1,)
            lockers.append(writer_lock(store))
        return original_event(cycle_id, kind, payload, **kwargs)
    monkeypatch.setattr(store, "event", lock_before_start)
    try:
        result = store.begin_cycle("cycle", original)
    finally:
        for locker in lockers:
            locker.rollback(); locker.close()
    cycles, starts = saved_rows(store)
    assert result == frozen and waits == [.2] and event_calls == ["cycle_started"]
    assert cycles == [("cycle", frozen["started_at"], candidate.encoded(frozen))]
    assert starts == [("cycle", "2026-10-28T17:15:36+00:00", candidate.encoded(frozen))]
    start, recovered = receipts(store)
    assert start["operation_id"] == recovered["operation_id"]
    assert recovered["operation"] == "model_cycle_started_event"
    assert recovered["pending_sha256"] == digest({"operation": "model_cycle_started_event", "cycle_id": "cycle",
        "symbol": None, "kind": "cycle_started", "recorded_at": "2026-10-28T17:15:36+00:00", "payload": frozen})
    assert recovered["sqlite_errorcode"] & 0xff == sqlite3.SQLITE_BUSY
    assert recovered["original_recorded_at"] == "2026-10-28T17:15:36+00:00"


@pytest.mark.parametrize("code,name", [(sqlite3.SQLITE_BUSY, "SQLITE_BUSY"),
                                       (sqlite3.SQLITE_LOCKED | 0x100, "SQLITE_LOCKED_SHAREDCACHE")])
def test_admission_classifies_native_primary_and_extended_codes(tmp_path, monkeypatch, code, name):
    waits, calls = [], []
    store = make_store(tmp_path, pause=waits.append)
    original_begin = ServiceStore.begin_cycle
    def first_busy(self, cycle_id, payload):
        calls.append(cycle_id)
        if len(calls) == 1:
            error = sqlite3.OperationalError("nonmatching native message")
            error.sqlite_errorcode, error.sqlite_errorname = code, name
            raise error
        return original_begin(self, cycle_id, payload)
    monkeypatch.setattr(ServiceStore, "begin_cycle", first_busy)
    assert store.begin_cycle("cycle", initial_payload()) == initial_payload()
    assert waits == [.2] and calls == ["cycle", "cycle"]
    assert receipts(store)[1]["sqlite_errorcode"] == code
    assert len(saved_rows(store)[0]) == len(saved_rows(store)[1]) == 1


def test_nonbusy_native_admission_error_is_fatal_even_with_locked_message(tmp_path, monkeypatch):
    waits = []
    store = make_store(tmp_path, pause=waits.append)
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode, error.sqlite_errorname = sqlite3.SQLITE_IOERR, "SQLITE_IOERR"
    def fail(self, cycle_id, payload):
        raise error
    monkeypatch.setattr(ServiceStore, "begin_cycle", fail)
    with pytest.raises(sqlite3.OperationalError) as caught:
        store.begin_cycle("cycle", initial_payload())
    assert caught.value is error and waits == [] and receipts(store) == []
    assert saved_rows(store) == ([], [])


@pytest.mark.parametrize("reason", ["manual_stop", "session_end"])
def test_service_authority_end_during_admission_returns_stopped_without_checkpoint(tmp_path, monkeypatch, reason):
    now = instant("2026-10-28T17:15:35Z")
    stop, waits, lockers, checkpoints, calls = [None], [], [], [], []
    monkeypatch.setattr(candidate, "_configuration", lambda *args, **kwargs:
        ("Scout", ("DOCU",), ResearchConfig(models=("fixture",)), {}, "fixture", None))
    class Runner:
        def __init__(self, store, **kwargs):
            self.store = store
        def run_cycle(self, *args, **kwargs):
            calls.append(kwargs["reference_at"])
            lockers.append(writer_lock(self.store))
            self.store.begin_cycle("cycle", initial_payload())
            raise AssertionError("Stopped admission must not start symbol work")
    monkeypatch.setattr(runtime, "ShadowRunner", Runner)
    lifecycle = SimpleNamespace(previous_checkpoint=None, session_id="manual-scout-15m:fixture",
        started_at=now, wake_at=now, deadline=now + timedelta(hours=5), generation=8,
        release={"manifest_sha256": "fixture-release"}, stop_reason=lambda: stop[0],
        ready=lambda: None, checkpoint=checkpoints.append)
    def pause(seconds):
        waits.append(seconds)
        lockers[0].rollback()
        stop[0] = reason
    try:
        result = candidate.run_model_service(repository=tmp_path, datastore=tmp_path,
            release={}, lifecycle=lifecycle, clock=lambda: now, sleep=pause)
    finally:
        for locker in lockers:
            locker.rollback(); locker.close()
    assert result == {"status": "STOPPED", "stop_reason": reason, "failure_persistence_pending": True}
    assert checkpoints == [] and waits == [.2] and len(calls) == 1
    with sqlite3.connect(tmp_path / "intraday/shadow.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM cycles").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM forecasts").fetchone() == (0,)
    rows = [json.loads(line) for line in (tmp_path / "intraday/services/model-metrics.jsonl").read_text().splitlines()]
    assert [row["phase"] for row in rows] == ["failure_persistence_started", "failure_persistence_stopped"]
    pending = rows[1]
    assert pending["pending_write"] == {"operation": "model_cycle_begin", "cycle_id": "cycle", "payload": initial_payload()}
    assert pending["stop_reason"] == reason and pending["diagnostics"]["exception_chain"]


def test_actual_runner_retries_admission_without_reference_shift_or_symbol_replay(tmp_path, monkeypatch):
    clock = [instant("2026-10-28T17:15:35Z")]
    original_reference, original_cutoff = instant("2026-10-28T17:00:00Z"), clock[0]
    waits, lockers, symbols = [], [], []
    def pause(seconds):
        waits.append(seconds)
        clock[0] += timedelta(minutes=31)
        lockers[0].rollback()
    store = make_store(tmp_path, pause=pause)
    runner = ShadowRunner(store=store, pc="Scout", symbols=("DOCU",),
        config=ResearchConfig(models=("fixture",), symbol_workers=1), clock=lambda: clock[0])
    def symbol(cycle_id, name, history, reference, cutoff, *args, **kwargs):
        symbols.append((cycle_id, name, reference, cutoff))
        raise AcquisitionPending("system_1a_snapshot_pending", retry_after_seconds=3)
    monkeypatch.setattr(runner, "_symbol", symbol)
    lockers.append(writer_lock(store))
    try:
        result = runner.run_cycle(object(), reference_at=original_reference,
                                  mode="live", experiment="manual-scout-15m:fixture")
    finally:
        lockers[0].rollback(); lockers[0].close()
    assert waits == [.2] and len(symbols) == 1
    assert symbols[0][1:] == ("DOCU", original_reference, original_cutoff)
    assert result["status"] == "partial_failure" and result["forecasts"] == {}
    cycles, starts = saved_rows(store)
    saved = json.loads(cycles[0][2])
    assert len(cycles) == len(starts) == 1
    assert saved["reference_at"] == original_reference.isoformat()
    assert saved["started_at"] == saved["training_cutoff"] == original_cutoff.isoformat()
    assert saved["attempt_count"] == 1 and saved["failed_symbols"] == 1
    assert result["failures"]["DOCU"]["reason"] == "system_1a_snapshot_pending"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM forecasts").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='symbol_failure'").fetchone() == (1,)
