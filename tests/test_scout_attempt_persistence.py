"""Post-work attempt writes retry exact evidence without repeating model work."""
from copy import deepcopy
from datetime import timedelta
import json
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import live_session, scout_services as candidate
from ml.intraday.prediction import digest, instant
from ml.intraday.runtime import ResearchConfig
from ml.intraday.service_handoff import ReadySpool, ServiceStore
from tests.test_intraday_runtime import trained
from tests.test_scout_service_handoff import prepared


ORIGINAL_CLOCK = "2026-10-28T17:15:36+00:00"
LATER_CLOCK = "2026-10-28T17:46:36+00:00"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Attempt persistence fixtures must never access the network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)


def writer_lock(store):
    locker = sqlite3.connect(store.path, timeout=0, check_same_thread=False)
    locker.execute("BEGIN IMMEDIATE")
    return locker


def make_store(tmp_path, pause, stop_reason=lambda: None):
    store = candidate._ModelServiceStore(tmp_path / "science.sqlite3", object_root=tmp_path / "objects",
        persistence_stop_reason=stop_reason, persistence_pause=pause,
        persistence_diagnostic_binding={"logical_run_id": "manual-scout-15m:fixture",
                                        "generation": 8, "manifest_sha256": "fixture-release"})
    store.connection_timeout = 0
    store.run_binding = {"logical_run_id": "manual-scout-15m:fixture"}
    return store


def receipts(store):
    path = store.object_root / "model-metrics.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("diagnostic_failure", [False, True])
def test_native_attempt_write_freezes_payload_and_clock_despite_diagnostic_failure(tmp_path, monkeypatch, diagnostic_failure):
    original = {"completed_at": ORIGINAL_CLOCK, "retrained_symbols": ["DOCU"],
                "resources_after": {"process_cpu_seconds": 3.5}, "failed_symbols": []}
    frozen = deepcopy(original)
    waits, lockers, clock = [], [], [ORIGINAL_CLOCK]
    def pause(seconds):
        waits.append(seconds)
        clock[0] = LATER_CLOCK
        original["resources_after"]["process_cpu_seconds"] = 9000
        original["retrained_symbols"].append("caller-change")
        lockers[0].rollback()
    store = make_store(tmp_path, pause)
    monkeypatch.setattr(candidate, "utcnow", lambda: clock[0])
    if diagnostic_failure:
        def broken(*args, **kwargs):
            raise OSError("optional receipt unavailable")
        monkeypatch.setattr(candidate, "metric", broken)
    lockers.append(writer_lock(store))
    try:
        store.event("cycle", "cycle_attempt_measured", original)
    finally:
        lockers[0].rollback(); lockers[0].close()
    with store.connect() as db:
        rows = db.execute("SELECT recorded_at,payload FROM events WHERE kind='cycle_attempt_measured'").fetchall()
    assert waits == [.2] and rows == [(ORIGINAL_CLOCK, candidate.encoded(frozen))]
    if diagnostic_failure:
        assert receipts(store) == []
    else:
        started, recovered = receipts(store)
        assert [started["phase"], recovered["phase"]] == ["failure_persistence_started", "failure_persistence_recovered"]
        assert started["operation_id"] == recovered["operation_id"]
        assert recovered["operation"] == "model_cycle_attempt_event" and recovered["busy_count"] == 1
        assert recovered["sqlite_errorcode"] & 0xff == sqlite3.SQLITE_BUSY
        assert recovered["pending_sha256"] == digest({"operation": "model_cycle_attempt_event", "cycle_id": "cycle",
            "symbol": None, "kind": "cycle_attempt_measured", "recorded_at": ORIGINAL_CLOCK, "payload": frozen})
        assert recovered["original_recorded_at"] == ORIGINAL_CLOCK


def service_case(tmp_path, monkeypatch, *, stop_reason=None, broken_diagnostics=False):
    root = tmp_path / "intraday/services"
    forecast, _, _, _, _, envelope, cutoff = prepared(root)
    clock, event_clock, stops = [cutoff + timedelta(seconds=30)], [ORIGINAL_CLOCK], [None]
    symbols = ("AAPL", "IONQ")
    config = ResearchConfig(models=("prerecorded",), symbol_workers=2)
    calls, publications, checkpoints, waits, lockers, attempts, stores = [], [], [], [], [], [], []
    def trainer(*args, **kwargs):
        calls.append(kwargs.get("config"))
        return trained(*args, **kwargs)
    def no_preparation(*args, **kwargs):
        raise AssertionError("Saved ready input must not be prepared or fetched again")
    monkeypatch.setattr(candidate, "_configuration", lambda *args, **kwargs:
        ("Scout", symbols, config, {"trainer": trainer, "dataset_builder": no_preparation}, "fixture-recipe", None))
    spool = ReadySpool(root, pc="Scout", symbols=symbols, recipe_hash="fixture-recipe", clock=lambda: clock[0])
    spool.publish(symbol="AAPL", reference_at=forecast["reference_at"], envelope=envelope, source_session="fixture-1a")
    native_enqueue = candidate.ServiceExecutionQueue.enqueue
    def enqueue(self, value):
        publications.append(deepcopy(value))
        return native_enqueue(self, value)
    monkeypatch.setattr(candidate.ServiceExecutionQueue, "enqueue", enqueue)
    native_event = candidate._ModelServiceStore.event
    def event(self, cycle_id, kind, payload, **kwargs):
        if kind == "cycle_attempt_measured":
            stores.append(self)
            assert len(calls) == len(publications) == 1
            with self.connect() as db:
                assert db.execute("SELECT COUNT(*) FROM forecasts").fetchone() == (1,)
                assert db.execute("SELECT COUNT(*) FROM intraday_live_queue").fetchone() == (1,)
            attempts.append(deepcopy(payload))
            self.connection_timeout = 0
            lockers.append(writer_lock(self))
        return native_event(self, cycle_id, kind, payload, **kwargs)
    monkeypatch.setattr(candidate._ModelServiceStore, "event", event)
    monkeypatch.setattr(candidate, "utcnow", lambda: event_clock[0])
    def pause(seconds):
        waits.append(seconds)
        event_clock[0] = LATER_CLOCK
        clock[0] += timedelta(minutes=31)
        if stop_reason is not None:
            stops[0] = stop_reason
            lockers[0].rollback()
        elif len(waits) == 2:
            lockers[0].rollback()
    lifecycle = SimpleNamespace(previous_checkpoint=None, session_id="manual-scout-15m:attempt-fixture",
        started_at=(instant(forecast["reference_at"]) - timedelta(minutes=15)).isoformat(),
        wake_at=(instant(forecast["reference_at"]) - timedelta(minutes=15)).isoformat(),
        deadline=live_session.automatic_stop_deadline(clock[0]).isoformat(), generation=8,
        release={"manifest_sha256": "fixture-release"}, stop_reason=lambda: stops[0],
        ready=lambda: None, checkpoint=checkpoints.append)
    if broken_diagnostics:
        native_metric = candidate.metric
        def metric(root, role, phase, **values):
            if phase.startswith("failure_persistence_"):
                raise OSError("optional retry diagnostic unavailable")
            return native_metric(root, role, phase, **values)
        monkeypatch.setattr(candidate, "metric", metric)
    def run():
        try:
            return candidate.run_model_service(repository=tmp_path, datastore=tmp_path, release={},
                lifecycle=lifecycle, clock=lambda: clock[0], sleep=pause)
        finally:
            for locker in lockers:
                locker.rollback(); locker.close()
    return SimpleNamespace(run=run, calls=calls, publications=publications, checkpoints=checkpoints,
        waits=waits, attempts=attempts, stores=stores, lifecycle=lifecycle)


def test_actual_service_retries_postwork_attempt_without_replaying_model_or_publication(tmp_path, monkeypatch):
    case = service_case(tmp_path, monkeypatch)
    result = case.run()
    assert result["status"] == "PLANNED_RESTART" and result["cycle_status"] == "partial_failure"
    assert len(case.calls) == len(case.publications) == len(case.attempts) == len(case.checkpoints) == 1
    assert case.waits == [.2, .2]
    store, original = case.stores[0], case.attempts[0]
    with store.connect() as db:
        rows = db.execute("SELECT recorded_at,payload FROM events WHERE kind='cycle_attempt_measured'").fetchall()
        cycle = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?", (result["cycle_id"],)).fetchone()[0])
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='cycle_progress' AND json_extract(payload,'$.attempt_count')=1").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM forecasts").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM intraday_live_queue").fetchone() == (1,)
    assert rows == [(ORIGINAL_CLOCK, candidate.encoded(original))]
    assert cycle["attempt_count"] == 1 and cycle["latest_attempt"] == original
    assert cycle["completed_at"] == original["completed_at"]
    assert cycle["successful_symbols"] == cycle["failed_symbols"] == 1
    assert store.forecast(result["cycle_id"], "AAPL") == case.publications[0]
    checkpoint = case.checkpoints[0]
    assert checkpoint["owned_workers_drained"] is True and checkpoint["deadline"] == case.lifecycle.deadline
    assert checkpoint["inspection"]["forecasts"]["AAPL"]["forecast_id"] == case.publications[0]["forecast_id"]
    pair = [row for row in receipts(store) if row.get("operation") == "model_cycle_attempt_event"]
    assert len(pair) == 2 and pair[0]["operation_id"] == pair[1]["operation_id"]
    assert pair[1]["busy_count"] == 2 and pair[0]["pending_sha256"] == pair[1]["pending_sha256"]


@pytest.mark.parametrize("reason,broken_diagnostics", [("manual_stop", False), ("session_end", True)])
def test_service_stop_during_attempt_preserves_forecast_without_summary_or_checkpoint(tmp_path, monkeypatch, reason, broken_diagnostics):
    case = service_case(tmp_path, monkeypatch, stop_reason=reason, broken_diagnostics=broken_diagnostics)
    result = case.run()
    assert result == {"status": "STOPPED", "stop_reason": reason, "failure_persistence_pending": True}
    assert len(case.calls) == len(case.publications) == len(case.attempts) == 1
    assert case.waits == [.2] and case.checkpoints == []
    store = case.stores[0]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='cycle_attempt_measured'").fetchone() == (0,)
        cycle = json.loads(db.execute("SELECT payload FROM cycles").fetchone()[0])
        assert "attempt_count" not in cycle and "latest_attempt" not in cycle
        assert db.execute("SELECT COUNT(*) FROM intraday_live_queue").fetchone() == (1,)
    assert store.forecast(case.publications[0]["cycle_id"], "AAPL") == case.publications[0]
    rows = [row for row in receipts(store) if row["phase"] == "failure_persistence_stopped"]
    if broken_diagnostics:
        assert rows == []
    else:
        assert len(rows) == 1 and rows[0]["stop_reason"] == reason
        pending = rows[0]["pending_write"]
        assert pending["operation"] == "model_cycle_attempt_event"
        assert pending["payload"] == case.attempts[0] and pending["recorded_at"] == ORIGINAL_CLOCK
        assert rows[0]["diagnostics"]["exception_chain"][0]["sqlite_errorcode"] & 0xff == sqlite3.SQLITE_BUSY


def test_nonbusy_attempt_error_propagates_even_if_message_says_locked(tmp_path, monkeypatch):
    waits = []
    store = make_store(tmp_path, waits.append)
    original = sqlite3.OperationalError("database is locked")
    original.sqlite_errorcode, original.sqlite_errorname = sqlite3.SQLITE_IOERR, "SQLITE_IOERR"
    def fail(self, cycle_id, kind, payload, **kwargs):
        raise original
    monkeypatch.setattr(ServiceStore, "event", fail)
    with pytest.raises(sqlite3.OperationalError) as caught:
        store.event("cycle", "cycle_attempt_measured", {"original": True})
    assert caught.value is original and waits == [] and receipts(store) == []


def test_ordinary_event_busy_retains_passthrough_behavior(tmp_path):
    waits = []
    store = make_store(tmp_path, waits.append)
    locker = writer_lock(store)
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            store.event("cycle", "unrelated_diagnostic", {"original": True})
        assert caught.value.sqlite_errorcode & 0xff == sqlite3.SQLITE_BUSY
    finally:
        locker.rollback(); locker.close()
    assert waits == [] and receipts(store) == []
