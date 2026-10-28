"""Offline regressions for startup, original data day and model boundaries."""
from datetime import timedelta
import json
import socket
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from ml.intraday import data, live_session, minute_feed, runtime, scout_services
from ml.intraday.prediction import PACIFIC, STEP, instant
from ml.intraday.runtime import ResearchConfig
from ml.intraday.service_handoff import ServiceStore
from tests.test_scout_services import data_case


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Lifecycle fixtures must never access the network")
    monkeypatch.setattr(socket, "socket", forbidden)


def test_cold_history_is_bounded_parallel_and_reports_progress(tmp_path, monkeypatch):
    case = data_case(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, maximum, calls = [0], [0], []
    def history(datastore, symbol, **kwargs):
        with lock:
            active[0] += 1
            maximum[0] = max(maximum[0], active[0])
            calls.append(symbol)
            if active[0] == 2:
                entered.set()
        try:
            assert release.wait(5)
            return []
        finally:
            with lock:
                active[0] -= 1
    monkeypatch.setattr(data, "load_local_history", history)
    result, failures = [], []
    def run():
        try:
            result.append(scout_services.run_data_service(repository=tmp_path, datastore=tmp_path,
                release=case.release, clock=lambda:case.now[0], sleep=case.sleep))
        except BaseException as exc:
            failures.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(3)  # The second symbol progresses while the first waits.
        state = json.loads((tmp_path / "intraday/services/data-status.json").read_bytes())
        assert state["status"] == "STARTING"
        assert state["startup_progress"]["stage"] == "history_loading"
        assert state["startup_progress"]["history_pending"] == list(case.symbols)
        assert not case.stats.published
    finally:
        release.set()
        worker.join(8)
    assert not worker.is_alive() and not failures
    assert result[0]["status"] == "STOPPED"
    assert maximum == [2] and set(calls) == set(case.symbols)
    state = json.loads((tmp_path / "intraday/services/data-status.json").read_bytes())
    assert set(state["startup_progress"]["history_complete"]) == set(case.symbols)
    assert state["startup_progress"]["history_failed"] == []
    metrics = [json.loads(line) for line in (tmp_path / "intraday/services/data-metrics.jsonl").read_text().splitlines()]
    histories = [row for row in metrics if row["phase"] == "cold_history"]
    assert set(row["symbol"] for row in histories) == set(case.symbols)
    assert all(row["seconds"] >= 0 for row in histories)


def test_after_close_recovery_pins_original_day_across_midnight_and_scores_saved_targets(tmp_path, monkeypatch):
    case = data_case(tmp_path, monkeypatch)
    case.now[0] = case.deadline + timedelta(hours=6, minutes=59)
    original_day = case.now[0].astimezone(PACIFIC).date()
    bindings = []
    def forbidden_history(*args, **kwargs):
        raise AssertionError("Expired data day must not rebuild model training histories")
    monkeypatch.setattr(data, "load_local_history", forbidden_history)
    monkeypatch.setattr(live_session, "prepare_market_collection", forbidden_history)
    monkeypatch.setattr(live_session, "scoring_loop", forbidden_history)
    class Feed:
        closed = True
        def __init__(self, **kwargs):
            bindings.append(kwargs)
            case.now[0] += timedelta(minutes=2)
            self.scoring_history_source = forbidden_history
        def run(self, *, stopped, pause):
            assert not stopped()
            return {"status": "closed"}
    monkeypatch.setattr(minute_feed, "QuarterHourMinuteFeed", Feed)
    store = ServiceStore(tmp_path / "intraday/shadow.sqlite3", object_root=tmp_path / "intraday/services")
    store.begin_cycle("saved-original", {"pc":"Scout", "mode":"live", "symbols":list(case.symbols),
        "reference_at":case.reference.isoformat(), "started_at":case.reference.isoformat(),
        "experiment":"original-trading-run"})
    # Exercise the actual provider-free final scorer, not the legacy facade that
    # was missing from the installed release. No saved forecast is manufactured.
    monkeypatch.delattr(live_session, "score_saved_session_outcomes")
    result = scout_services.run_data_service(repository=tmp_path, datastore=tmp_path,
        release=case.release, clock=lambda:case.now[0], sleep=case.sleep)
    assert result["status"] == "STOPPED"
    assert case.now[0].astimezone(PACIFIC).date() > original_day
    assert bindings[0]["start_reference"].astimezone(PACIFIC).date() == original_day
    assert bindings[0]["histories"] == {} and not case.stats.calls
    status = json.loads((tmp_path / "intraday/services/data-status.json").read_bytes())
    assert status["data_day"] == str(original_day)
    assert status["after_close_recovery"] is True
    assert instant(status["deadline"]) == case.deadline
    with scout_services.ReadySpool(tmp_path / "intraday/services", pc="Scout", symbols=case.symbols,
            recipe_hash="fixture-recipe").connect() as db:
        outcomes = [json.loads(raw) for raw, in db.execute("SELECT payload FROM recovery WHERE kind='final_saved_outcomes'")]
        days = [json.loads(raw) for raw, in db.execute("SELECT payload FROM recovery WHERE kind='data_day_outcome'")]
    assert outcomes == [{"session_id":"original-trading-run", "scored":0, "synthetic":0, "unavailable":0}]
    assert days[0]["data_day"] == str(original_day) and days[0]["native_feed_closed"] is True


def model_case(tmp_path, monkeypatch, *, pending=False):
    reference = instant("2026-10-08T17:45:00+00:00")
    now = [reference + STEP + timedelta(seconds=35)]
    symbols = ("AAPL", "IONQ")
    saved = {"reference_at":reference.isoformat(), "status":"partial_failure", "failures":{
        "IONQ":{"retryable":True, "reason":"system_1a_snapshot_pending" if pending else "fixture_due",
                "retry_at":(now[0]-timedelta(seconds=1)).isoformat()}}}
    monkeypatch.setattr(scout_services, "_configuration", lambda *args, **kwargs:
        ("Scout", symbols, ResearchConfig(models=("fixture",)), {}, "fixture", None))
    monkeypatch.setattr(live_session, "_session_cycles", lambda *args, **kwargs:[("old-cycle", saved)])
    monkeypatch.setattr(scout_services, "_model_checkpoint", lambda store, lifecycle, result, symbols:
        {"boundary_cycle_id":result["cycle_id"]})
    calls, checkpoints = [], []
    class Runner:
        def __init__(self, **kwargs):
            pass
        def run_cycle(self, histories, *, reference_at, retry_symbols, **kwargs):
            calls.append((reference_at, retry_symbols))
            if retry_symbols is not None:
                saved.update(status="completed", failures={})
                now[0] += STEP
                return {"cycle_id":"old-cycle", "status":"completed", "forecasts":{}}
            return {"cycle_id":"fresh-full-cycle", "status":"partial_failure", "forecasts":{}}
    monkeypatch.setattr(runtime, "ShadowRunner", Runner)
    lifecycle = SimpleNamespace(previous_checkpoint=None, session_id="fixture-run", started_at=now[0],
        wake_at=now[0], deadline=live_session.automatic_stop_deadline(now[0]),
        stop_reason=lambda:None, ready=lambda:None, checkpoint=checkpoints.append)
    return reference, now, calls, checkpoints, lifecycle


def test_subset_retry_does_not_checkpoint_or_replace_worker(tmp_path, monkeypatch):
    reference, now, calls, checkpoints, lifecycle = model_case(tmp_path, monkeypatch)
    result = scout_services.run_model_service(repository=tmp_path, datastore=tmp_path,
        release={}, lifecycle=lifecycle, clock=lambda:now[0])
    assert result["status"] == "PLANNED_RESTART"
    assert calls == [(reference, ["IONQ"]), (reference+STEP, None)]
    assert checkpoints == [{"boundary_cycle_id":"fresh-full-cycle"}]
    metrics = [json.loads(line) for line in (tmp_path / "intraday/services/model-metrics.jsonl").read_text().splitlines()]
    assert [row["cycle_id"] for row in metrics if row["phase"] == "full_scope_iteration"] == ["fresh-full-cycle"]
    assert [row["cycle_id"] for row in metrics if row["phase"] == "subset_retry"] == ["old-cycle"]


def test_missing_ready_snapshot_waits_without_repeating_failed_model_attempt(tmp_path, monkeypatch):
    reference, now, calls, checkpoints, lifecycle = model_case(tmp_path, monkeypatch, pending=True)
    sleeps = []
    def pause(seconds):
        if not sleeps:
            now[0] += STEP
        sleeps.append(seconds)
        time.sleep(min(seconds, .01))
    scout_services.run_model_service(repository=tmp_path, datastore=tmp_path,
        release={}, lifecycle=lifecycle, clock=lambda:now[0], sleep=pause)
    assert sleeps and calls == [(reference+STEP, None)]
    assert checkpoints == [{"boundary_cycle_id":"fresh-full-cycle"}]


def test_busy_handoff_defers_one_symbol_and_preserves_independent_progress(tmp_path, monkeypatch):
    case = data_case(tmp_path, monkeypatch, missing=("AAPL",))
    original_ready = scout_services.ReadySpool.ready
    original_record = scout_services.ReadySpool.record
    def ready(self, symbol, reference, **kwargs):
        if symbol == "AAPL":
            raise sqlite3.OperationalError("database is locked")
        return original_ready(self, symbol, reference, **kwargs)
    def record(self, kind, payload):
        if kind == "preparation_deferred":
            raise sqlite3.OperationalError("database is locked")
        return original_record(self, kind, payload)
    monkeypatch.setattr(scout_services.ReadySpool, "ready", ready)
    monkeypatch.setattr(scout_services.ReadySpool, "record", record)
    result = scout_services.run_data_service(repository=tmp_path, datastore=tmp_path,
        release=case.release, clock=lambda:case.now[0], sleep=case.sleep)
    assert result["status"] == "STOPPED"
    assert set(case.stats.published) == {"IONQ", "TSLA", "DOCU"}
    fallback = list((tmp_path / "intraday/services/recovery").glob("*.json"))
    assert len(fallback) == 1
    saved = json.loads(fallback[0].read_bytes())
    assert saved["kind"] == "preparation_deferred"
    assert saved["database_error"] == "database is locked"
    assert saved["payload"]["symbol"] == "AAPL"
    assert saved["payload"]["retry_after_seconds"] == 3
    assert instant(saved["payload"]["retry_at"]) > case.now[0]
    assert saved["payload"]["reference_at"] == case.reference.isoformat()
