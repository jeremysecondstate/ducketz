"""Actual independent service loops with local fixtures and prerecorded models."""
from collections import Counter
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from ml.intraday import data, live_model_recipe, live_session, minute_feed, runtime, scout_services
from ml.intraday.prediction import STEP, instant
from ml.intraday.runtime import ResearchConfig
from ml.intraday.service_handoff import ReadySpool, ServiceStore
from tests.test_intraday_runtime import trained
from tests.test_scout_service_handoff import prepared


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Service orchestration fixtures must never access the network")
    monkeypatch.setattr(socket, "socket", forbidden)


def data_case(tmp_path, monkeypatch, *, missing=(), after_deadline=False):
    symbols = ("AAPL", "IONQ", "TSLA", "DOCU")
    snapshots = {symbol: prepared(tmp_path / "templates" / symbol, symbol=symbol)[1]
                 for symbol in symbols}
    reference = instant(snapshots["AAPL"]["inference"]["reference_at"])
    if after_deadline:
        # The final preparation opportunity is16:15 Pacific. The native feed's
        # independent completion will arrive after the fixed17:00 trading end.
        shift = timedelta(hours=5, minutes=30)
        reference += shift
        for snapshot in snapshots.values():
            snapshot["cutoff"] = (instant(snapshot["cutoff"]) + shift).isoformat()
            for key in ("reference_at", "information_available_at"):
                snapshot["inference"][key] = (instant(snapshot["inference"][key]) + shift).isoformat()
            for key in ("label_available_at", "reference_at", "target_close_at"):
                snapshot[key] = [(instant(value) + shift).isoformat() for value in snapshot[key]]
            for candle in snapshot["candles"]:
                for key in ("opened_at", "available_at", "session_open", "session_close"):
                    candle[key] = (instant(candle[key]) + shift).isoformat()
    now = [reference + STEP + timedelta(seconds=33)]
    deadline = live_session.automatic_stop_deadline(now[0])
    finish = threading.Event()
    after_end = threading.Event()
    consumer_release = threading.Event()
    lock = threading.Lock()
    stats = SimpleNamespace(calls=[], published=[], active=0, maximum_active=0,
                            feed_stops=[], source_cycles=[], collector_stops=[], scoring_stops=[])
    config = ResearchConfig(models=("fixture",), symbol_workers=2)
    release = {"release_root": "fixture-release", "manifest_sha256": "fixture-manifest"}
    def configuration(*args, **kwargs):
        return "Scout", symbols, config, {"dataset_builder": object()}, "fixture-recipe", None
    monkeypatch.setattr(scout_services, "_configuration", configuration)
    monkeypatch.setattr(live_model_recipe, "native_interval_symbols_for_runtime", lambda _: ())
    monkeypatch.setattr(data, "load_local_history", lambda *args, **kwargs: [])
    monkeypatch.setattr(data, "AcquisitionJournal", lambda *args, **kwargs: object())
    monkeypatch.setattr(live_session, "prepare_minute_provider", lambda *args, **kwargs: object())
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)

    class Source:
        def bind_cycle(self, identity):
            stats.source_cycles.append(identity)
        def get(self, symbol):
            if symbol in missing:
                raise data.AcquisitionPending("fixture_exact_source_missing", retry_after_seconds=3)
            return deepcopy(snapshots[symbol])

    class Feed:
        def __init__(self, **kwargs):
            self.scoring_history_source = lambda *args, **kwargs: None
        def history_source(self, ref):
            assert ref == reference
            return Source()
        def run(self, *, stopped, pause):
            until = time.monotonic() + 5
            while not finish.is_set():
                if now[0] >= deadline:
                    stats.feed_stops.append(stopped())
                    after_end.set()
                    if stats.published:
                        finish.set()
                        break
                if time.monotonic() >= until:
                    raise AssertionError("Fixture preparation failed to complete independently")
                time.sleep(.002)
            stats.feed_stops.append(stopped())

    monkeypatch.setattr(minute_feed, "QuarterHourMinuteFeed", Feed)

    class Collector:
        def run(self, *, stopped, pause, **kwargs):
            while not stopped():
                stats.collector_stops.append(False)
                time.sleep(.002)
    monkeypatch.setattr(live_session, "prepare_market_collection", lambda *args, **kwargs: Collector())
    def scoring(*args, stopped, **kwargs):
        while not stopped():
            stats.scoring_stops.append(False)
            time.sleep(.002)
    monkeypatch.setattr(live_session, "scoring_loop", scoring)

    class PreparationRunner:
        def __init__(self, *, store, **kwargs):
            self.store = store
        def _symbol(self, identity, symbol, history, ref, cutoff, mode, *args, prepare_only=False):
            assert prepare_only is True and mode == "live"
            with lock:
                stats.calls.append(symbol)
                stats.active += 1
                stats.maximum_active = max(stats.maximum_active, stats.active)
            try:
                snapshot = history()
                time.sleep(.003)
                return self.store.freeze_inputs(identity, symbol, snapshot)
            finally:
                with lock:
                    stats.active -= 1
    monkeypatch.setattr(runtime, "ShadowRunner", PreparationRunner)
    native_publish = ReadySpool.publish
    expected = set(symbols) - set(missing)
    def publish(self, **kwargs):
        value = native_publish(self, **kwargs)
        with lock:
            stats.published.append(kwargs["symbol"])
            if expected <= set(stats.published) and not after_deadline:
                finish.set()
        return value
    monkeypatch.setattr(ReadySpool, "publish", publish)
    moved = [False]
    def sleep(seconds):
        if after_deadline and stats.published and not moved[0]:
            now[0] = deadline + timedelta(seconds=30)
            moved[0] = True
        time.sleep(min(.002, seconds))
    return SimpleNamespace(symbols=symbols, snapshots=snapshots, reference=reference, now=now,
        deadline=deadline, stats=stats, finish=finish, after_end=after_end,
        consumer_release=consumer_release, release=release, sleep=sleep)


@pytest.mark.parametrize("consumer_state", ["busy", "stopped"])
def test_actual_1a_service_continues_without_1b_and_does_not_activate_trading(tmp_path, monkeypatch, consumer_state):
    case = data_case(tmp_path, monkeypatch, missing=("AAPL",))
    entered = threading.Event()
    def busy_model():
        entered.set()
        case.consumer_release.wait(8)
    consumer = threading.Thread(target=busy_model) if consumer_state == "busy" else None
    if consumer is not None:
        consumer.start()
        assert entered.wait(1)
    try:
        result = scout_services.run_data_service(repository=tmp_path, datastore=tmp_path,
            release=case.release, clock=lambda:case.now[0], sleep=case.sleep)
        assert result["status"] == "STOPPED"
        assert set(case.stats.published) == {"IONQ", "TSLA", "DOCU"}
        assert Counter(case.stats.calls) == Counter({symbol:1 for symbol in case.symbols})
        assert 1 <= case.stats.maximum_active <= 2
        assert case.stats.active == 0
        if consumer is not None:
            assert consumer.is_alive()  # Collection/preparation never waited for its model consumer.
        spool = ReadySpool(tmp_path / "intraday/services", pc="Scout", symbols=case.symbols,
            recipe_hash="fixture-recipe", clock=lambda:case.now[0])
        with spool.connect() as db:
            failures = [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM recovery WHERE kind='preparation_deferred'")]
            assert db.execute("SELECT count(*) FROM ready WHERE status='ready'").fetchone()[0] == 3
        assert len(failures) == 1 and failures[0]["symbol"] == "AAPL"
        assert failures[0]["reason"] == "fixture_exact_source_missing"
        status = json.loads((spool.root / "data-status.json").read_bytes())
        assert status["source_session"] == result["source_session"]
        assert status["symbols"] == list(case.symbols) and status["errors"] == []
        assert not (tmp_path / "intraday/scout/controller.json").exists()
        for path in (spool.root / "preparation.sqlite3", tmp_path / "intraday/shadow.sqlite3"):
            store = ServiceStore(path, object_root=spool.root)
            assert store.forecasts() == []
            with store.connect() as db:
                assert db.execute("SELECT name FROM sqlite_master WHERE name='intraday_live_queue'").fetchone() is None
    finally:
        case.consumer_release.set()
        if consumer is not None:
            consumer.join(2)


def test_data_day_completion_remains_independent_of_trading_17_deadline(tmp_path, monkeypatch):
    case = data_case(tmp_path, monkeypatch, after_deadline=True)
    result = scout_services.run_data_service(repository=tmp_path, datastore=tmp_path,
        release=case.release, clock=lambda:case.now[0], sleep=case.sleep)
    assert result["status"] == "STOPPED"
    assert case.after_end.is_set() and case.now[0] > case.deadline
    assert case.stats.feed_stops and not any(case.stats.feed_stops)
    status = json.loads((tmp_path / "intraday/services/data-status.json").read_bytes())
    assert instant(status["deadline"]) == case.deadline
    assert instant(status["heartbeat_at"]) > case.deadline
    assert result["source_session"].startswith("scout-data-2026-10-08-")
    assert case.stats.published  # Source completion follows an actual immutable publication.
    assert status["errors"] == []


def test_actual_model_service_publishes_valid_symbol_and_full_scope_missing_checkpoint(tmp_path, monkeypatch):
    root = tmp_path / "intraday/services"
    forecast, snapshot, _, _, _, envelope, cutoff = prepared(root)
    now = cutoff + timedelta(seconds=30)
    symbols = ("AAPL", "IONQ")
    config = ResearchConfig(models=("prerecorded",), symbol_workers=2)
    def forbidden_preparation(*args, **kwargs):
        raise AssertionError("1B must consume verified ready inputs without data preparation")
    monkeypatch.setattr(scout_services, "_configuration", lambda *args, **kwargs:
        ("Scout", symbols, config, {"trainer":trained, "dataset_builder":forbidden_preparation},
         "fixture-recipe", None))
    spool = ReadySpool(root, pc="Scout", symbols=symbols, recipe_hash="fixture-recipe", clock=lambda:now)
    spool.publish(symbol="AAPL", reference_at=forecast["reference_at"], envelope=envelope, source_session="fixture-1a")
    checkpoints, ready = [], []
    lifecycle = SimpleNamespace(previous_checkpoint=None, session_id="manual-fixture-1b",
        started_at=(instant(forecast["reference_at"]) - STEP).isoformat(),
        wake_at=(instant(forecast["reference_at"]) - STEP).isoformat(),
        deadline=live_session.automatic_stop_deadline(now).isoformat(),
        stop_reason=lambda:None, ready=lambda:ready.append(True), checkpoint=checkpoints.append)
    result = scout_services.run_model_service(repository=tmp_path, datastore=tmp_path,
        release={}, lifecycle=lifecycle, clock=lambda:now)
    assert result["status"] == "PLANNED_RESTART" and result["cycle_status"] == "partial_failure"
    assert ready == [True] and len(checkpoints) == 1
    checkpoint = checkpoints[0]
    assert checkpoint["symbols"] == list(symbols) and checkpoint["owned_workers_drained"] is True
    assert checkpoint["boundary_kind"] == "full_symbol_attempt_with_durable_deferrals"
    assert set(checkpoint["inspection"]["forecasts"]) == {"AAPL"}
    assert checkpoint["inspection"]["failures"]["IONQ"]["reason"] == "system_1a_snapshot_pending"
    assert checkpoint["deadline"] == lifecycle.deadline
    store = ServiceStore(tmp_path / "intraday/shadow.sqlite3", object_root=root)
    saved = store.forecast(result["cycle_id"], "AAPL")
    assert saved["probability_not_down"] == .6 and saved["snapshot_hash"] == forecast["snapshot_hash"]
    assert instant(saved["target_close"]) == instant(forecast["target_close"])
    with store.connect() as db:
        queued = db.execute("SELECT forecast_id,symbol,status FROM intraday_live_queue").fetchall()
    assert queued == [(saved["forecast_id"], "AAPL", "pending")]
    queue = scout_services.ServiceExecutionQueue(store, lifecycle.session_id, pc="Scout", event_sink=lambda event:None)
    assert [f["forecast_id"] for f in queue.due(now)] == [saved["forecast_id"]]
    assert queue.due(saved["target_close"]) == []
    with store.connect() as db:
        assert db.execute("SELECT status FROM intraday_live_queue").fetchone()[0] == "expired"
    assert store.forecast(result["cycle_id"], "AAPL") == saved
    assert store.scientific_forecast(saved)["probability_not_down"] == .6
    assert not (tmp_path / "intraday/scout/controller.json").exists()
