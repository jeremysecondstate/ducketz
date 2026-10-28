"""Forecast postcommit evidence retries preserve the saved forecast and stop authority."""
from copy import deepcopy
from datetime import timedelta
import json
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import scout_services as candidate
from ml.intraday.prediction import digest, instant
from ml.intraday.runtime import ResearchConfig
from ml.intraday.service_handoff import ReadySpool, ServiceStore
from tests.test_intraday_runtime import trained
from tests.test_scout_service_handoff import prepared


ORIGINAL_CLOCK = "2026-10-08T18:00:33+00:00"
LATER_CLOCK = "2026-10-08T18:01:01+00:00"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Forecast event fixtures must never access the network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)


def writer_lock(store):
    locker = sqlite3.connect(store.path, timeout=0, check_same_thread=False)
    locker.execute("BEGIN IMMEDIATE")
    return locker


def receipts(store):
    path = store.object_root / "model-metrics.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("broken_diagnostics", [False, True])
def test_native_forecast_event_retry_freezes_original_payload_and_row_clock(tmp_path, monkeypatch, broken_diagnostics):
    original = {"forecast_id": "saved-forecast", "persisted_at": ORIGINAL_CLOCK,
                "persistence_seconds": 1.25, "nested": {"unchanged": True}}
    frozen, clock, lockers, waits = deepcopy(original), [ORIGINAL_CLOCK], [], []
    def pause(seconds):
        waits.append(seconds)
        original["nested"]["unchanged"] = False
        clock[0] = LATER_CLOCK
        lockers[0].rollback()
    store = candidate._ModelServiceStore(tmp_path / "science.sqlite3", object_root=tmp_path / "objects",
        persistence_stop_reason=lambda: None, persistence_pause=pause)
    store.connection_timeout = 0
    monkeypatch.setattr(candidate, "utcnow", lambda: clock[0])
    if broken_diagnostics:
        def broken(*args, **kwargs):
            raise OSError("optional metric unavailable")
        monkeypatch.setattr(candidate, "metric", broken)
    lockers.append(writer_lock(store))
    try:
        store.event("cycle", "forecast_persisted", original, symbol="AAPL")
    finally:
        lockers[0].rollback(); lockers[0].close()
    with store.connect() as db:
        rows = db.execute("SELECT symbol,recorded_at,payload FROM events WHERE kind='forecast_persisted'").fetchall()
    assert rows == [("AAPL", ORIGINAL_CLOCK, candidate.encoded(frozen))]
    assert waits == [.2]
    if broken_diagnostics:
        assert receipts(store) == []
    else:
        started, recovered = receipts(store)
        assert [started["phase"], recovered["phase"]] == ["failure_persistence_started", "failure_persistence_recovered"]
        assert started["operation_id"] == recovered["operation_id"]
        assert recovered["sqlite_errorcode"] & 0xff == sqlite3.SQLITE_BUSY
        assert recovered["operation"] == "model_forecast_persisted_event"
        assert recovered["pending_sha256"] == digest({"operation": "model_forecast_persisted_event",
            "cycle_id": "cycle", "symbol": "AAPL", "kind": "forecast_persisted",
            "recorded_at": ORIGINAL_CLOCK, "payload": frozen})


def service_case(tmp_path, monkeypatch, *, stop_reason=None, release_lock=True, broken_diagnostics=False):
    root = tmp_path / "intraday/services"
    forecast, _, _, _, _, envelope, cutoff = prepared(tmp_path / "preparation")
    clock, event_clock, stop = [cutoff + timedelta(seconds=30)], [ORIGINAL_CLOCK], [None]
    fits, saves, publications, checkpoints, waits, lockers, stores, pending = [], [], [], [], [], [], [], []
    def trainer(*args, **kwargs):
        fits.append("stub-fit")
        return trained(*args, **kwargs)
    def no_preparation(*args, **kwargs):
        raise AssertionError("Ready scientific input must not be prepared again")
    monkeypatch.setattr(candidate, "_configuration", lambda *args, **kwargs:
        ("Scout", ("AAPL",), ResearchConfig(models=("fixture",), symbol_workers=1),
         {"trainer": trainer, "dataset_builder": no_preparation}, "fixture-recipe", None))
    spool = ReadySpool(root, pc="Scout", symbols=("AAPL",), recipe_hash="fixture-recipe", clock=lambda: clock[0])
    spool.publish(symbol="AAPL", reference_at=forecast["reference_at"], envelope=envelope, source_session="fixture-1a")
    native_init = candidate._ModelServiceStore.__init__
    def initialize(self, *args, **kwargs):
        native_init(self, *args, **kwargs)
        self.connection_timeout = 0
        stores.append(self)
    monkeypatch.setattr(candidate._ModelServiceStore, "__init__", initialize)
    native_save = candidate._ModelServiceStore.save_forecast
    def save(self, *args, **kwargs):
        value = native_save(self, *args, **kwargs)
        saves.append(deepcopy(value))
        return value
    monkeypatch.setattr(candidate._ModelServiceStore, "save_forecast", save)
    native_event = candidate._ModelServiceStore.event
    def event(self, cycle_id, kind, payload, **kwargs):
        if kind == "forecast_persisted":
            pending.append(deepcopy(payload))
            lockers.append(writer_lock(self))
        return native_event(self, cycle_id, kind, payload, **kwargs)
    monkeypatch.setattr(candidate._ModelServiceStore, "event", event)
    native_enqueue = candidate.ServiceExecutionQueue.enqueue
    def enqueue(self, value):
        publications.append(deepcopy(value))
        return native_enqueue(self, value)
    monkeypatch.setattr(candidate.ServiceExecutionQueue, "enqueue", enqueue)
    monkeypatch.setattr(candidate, "utcnow", lambda: event_clock[0])
    if broken_diagnostics:
        native_metric = candidate.metric
        def metric(root, role, phase, **values):
            if phase.startswith("failure_persistence_"):
                raise OSError("optional metric unavailable")
            return native_metric(root, role, phase, **values)
        monkeypatch.setattr(candidate, "metric", metric)
    def pause(seconds):
        waits.append(seconds)
        event_clock[0] = LATER_CLOCK
        if release_lock:
            lockers[0].rollback()
        if stop_reason is not None:
            stop[0] = stop_reason
    lifecycle = SimpleNamespace(previous_checkpoint=None, session_id="manual-scout-15m:forecast-event-fixture",
        started_at=ORIGINAL_CLOCK, wake_at=ORIGINAL_CLOCK, deadline="2026-10-09T00:00:00+00:00",
        generation=8, release={"manifest_sha256": "fixture-release"},
        stop_reason=lambda: stop[0], ready=lambda: None, checkpoint=checkpoints.append)
    def run():
        try:
            return candidate.run_model_service(repository=tmp_path, datastore=tmp_path, release={},
                lifecycle=lifecycle, clock=lambda: clock[0], sleep=pause)
        finally:
            for locker in lockers:
                locker.rollback(); locker.close()
    return SimpleNamespace(run=run, fits=fits, saves=saves, publications=publications, checkpoints=checkpoints,
        waits=waits, stores=stores, pending=pending, lifecycle=lifecycle)


@pytest.mark.parametrize("broken_diagnostics", [False, True])
def test_full_service_retries_only_postcommit_event_then_delivers_once(tmp_path, monkeypatch, broken_diagnostics):
    case = service_case(tmp_path, monkeypatch, broken_diagnostics=broken_diagnostics)
    result = case.run()
    assert result["status"] == "PLANNED_RESTART" and result["cycle_status"] == "completed"
    assert len(case.fits) == len(case.saves) == len(case.publications) == len(case.checkpoints) == 1
    assert case.saves == case.publications and case.waits == [.2]
    store, forecast = case.stores[0], case.saves[0]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM forecasts").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM intraday_live_queue").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='symbol_failure'").fetchone() == (0,)
        assert db.execute("SELECT recorded_at,payload FROM events WHERE kind='forecast_persisted'").fetchall() == [
            (ORIGINAL_CLOCK, candidate.encoded(case.pending[0]))]
    assert store.forecast(result["cycle_id"], "AAPL") == forecast
    checkpoint = case.checkpoints[0]
    assert checkpoint["owned_workers_drained"] is True
    assert checkpoint["deadline"] == case.lifecycle.deadline
    assert checkpoint["inspection"]["forecasts"]["AAPL"]["forecast_id"] == forecast["forecast_id"]


@pytest.mark.parametrize("reason", ["manual_stop", "session_end"])
@pytest.mark.parametrize("release_lock,broken_diagnostics", [(True, False), (False, False), (True, True)])
def test_stop_in_symbol_future_is_control_flow_and_preserves_pending_evidence(
        tmp_path, monkeypatch, reason, release_lock, broken_diagnostics):
    case = service_case(tmp_path, monkeypatch, stop_reason=reason,
                        release_lock=release_lock, broken_diagnostics=broken_diagnostics)
    result = case.run()
    assert result == {"status": "STOPPED", "stop_reason": reason, "failure_persistence_pending": True}
    assert len(case.fits) == len(case.saves) == 1
    assert case.publications == case.checkpoints == [] and case.waits == [.2]
    store, forecast = case.stores[0], case.saves[0]
    assert store.forecast(forecast["cycle_id"], "AAPL") == forecast
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM intraday_live_queue").fetchone() == (0,)
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind IN ('forecast_persisted','symbol_failure','cycle_attempt_measured')").fetchone() == (0,)
        cycle = json.loads(db.execute("SELECT payload FROM cycles").fetchone()[0])
    assert cycle["status"] == "running" and "attempt_count" not in cycle and "failures" not in cycle
    stopped = [row for row in receipts(store) if row["phase"] == "failure_persistence_stopped"]
    if broken_diagnostics:
        assert stopped == []
    else:
        assert len(stopped) == 1 and stopped[0]["stop_reason"] == reason
        pending = stopped[0]["pending_write"]
        assert pending == {"operation": "model_forecast_persisted_event", "cycle_id": forecast["cycle_id"],
            "symbol": "AAPL", "kind": "forecast_persisted", "recorded_at": ORIGINAL_CLOCK, "payload": case.pending[0]}


def test_nonbusy_event_error_is_not_retried_or_replaced(tmp_path, monkeypatch):
    waits = []
    store = candidate._ModelServiceStore(tmp_path / "science.sqlite3", object_root=tmp_path / "objects",
        persistence_stop_reason=lambda: None, persistence_pause=waits.append)
    original = sqlite3.OperationalError("database is locked")
    original.sqlite_errorcode, original.sqlite_errorname = sqlite3.SQLITE_IOERR, "SQLITE_IOERR"
    def fail(self, *args, **kwargs):
        raise original
    monkeypatch.setattr(ServiceStore, "event", fail)
    with pytest.raises(sqlite3.OperationalError) as caught:
        store.event("cycle", "forecast_persisted", {"forecast_id": "original"}, symbol="AAPL")
    assert caught.value is original and waits == [] and receipts(store) == []
