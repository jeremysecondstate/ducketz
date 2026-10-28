"""Real handoff/store with prerecorded model outputs and denied network."""
from dataclasses import replace
from datetime import timedelta
import os
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from tests.intraday_split_package_support import require_split_integration
require_split_integration(__file__, files=(
    "ml/intraday/live_session.py", "ml/intraday/account_execution.py", "ml/intraday/split_service.py",
    "ml/intraday/split_model.py", "tools/trader_worker.py", "tests/test_intraday_runtime.py"),
    interfaces=(("ml/intraday/runtime.py", "ShadowRunner", "prepare_cycle", ()),
                ("ml/intraday/runtime.py", "ShadowRunner", "__init__",
                 ("prepared_source", "forecast_preparer", "datastore"))))

from ml.intraday.prediction import STEP, digest, instant
from ml.intraday.runtime import ResearchConfig, ShadowRunner
from ml.intraday.store import ShadowStore
from ml.intraday.split_handoff import ReadySnapshots
from ml.intraday.split_service import DataService
from ml.intraday.split_model import ARCHITECTURE, model_pass
from tests.test_intraday_runtime import REF, dataset, history, trained


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Independent-service fixtures must not contact providers or brokers")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def _environment(tmp_path):
    state = SimpleNamespace(now=REF + timedelta(minutes=16), outputs=[])
    state.clock = lambda: state.now.isoformat()
    state.symbols = ("AAPL", "IONQ")
    state.config = ResearchConfig(models=("fixture_a", "fixture_b"), symbol_workers=2)
    state.data_store = ShadowStore(tmp_path / "system-1a/preparation.sqlite3")
    state.model_store = ShadowStore(tmp_path / "system-1b/science.sqlite3")
    state.science_store = ShadowStore(tmp_path / "system-1b/models.sqlite3")
    state.handoff = ReadySnapshots(tmp_path / "ready", actor="Atlas", symbols=state.symbols, clock=state.clock)

    def build(frame, *, cutoff, reference_at, **kwargs):
        result = dataset(frame, cutoff=cutoff, reference_at=reference_at, **kwargs)
        result.inference["information_available_at"] = (reference_at + STEP).isoformat()
        return result

    def incoming(reference):
        result = {}
        for symbol in state.symbols:
            frame = history(symbol)
            frame["opened_at"] = reference
            frame["available_at"] = reference + STEP
            result[symbol] = frame
        return result

    state.runner = ShadowRunner(store=state.data_store, pc="Atlas", symbols=state.symbols,
        config=state.config, clock=state.clock, dataset_builder=build,
        trainer=lambda *a, **k: pytest.fail("System 1A attempted model work"))
    state.service = DataService(runner=state.runner, handoff=state.handoff, incoming=incoming,
                               session_id="data-service-fixture", clock=state.clock)
    state.session = dict(session_id="manual-session-fixture", actor="Atlas", symbols=list(state.symbols),
        started_at=(state.now - timedelta(seconds=30)).isoformat(),
        close_at=(state.now + timedelta(hours=2)).isoformat(), architecture=ARCHITECTURE)
    state.queue = SimpleNamespace(enqueue=lambda forecast: state.outputs.append(forecast))
    state.runner_factory = lambda **kwargs: ShadowRunner(**kwargs, trainer=trained,
        dataset_builder=lambda *a, **k: pytest.fail("System 1B reconstructed model inputs"))
    state.consume = lambda **kwargs: model_pass(store=state.model_store, queue=state.queue,
        session=state.session, config=state.config, handoff=state.handoff, clock=state.clock,
        science_store=state.science_store,
        runner_factory=kwargs.pop("runner_factory", state.runner_factory), **kwargs)
    return state


@pytest.fixture
def env(tmp_path):
    return _environment(tmp_path)


def test_data_continues_without_consumer_and_models_use_exact_inputs(env):
    first = env.service.pass_once()
    assert set(first["prepared_symbols"]) == set(env.symbols)
    assert env.data_store.forecasts() == [] and env.model_store.forecasts() == []
    result = env.consume()
    assert result["status"] == "PUBLISHED" and len(env.outputs) == 2
    first_forecast = env.outputs[0]
    assert first_forecast["probability_not_down"] == .6
    assert first_forecast["direction"] == "BULLISH"
    source = next(record for record in env.handoff.ready(env.clock(), configuration_hash=digest(
        __import__("dataclasses").asdict(env.config))) if record["symbol"] == first_forecast["symbol"])
    cycle, snapshot = env.handoff.load(source)
    assert env.science_store.inputs(first_forecast["cycle_id"], first_forecast["symbol"]) == (digest(snapshot), snapshot)
    assert env.model_store.inputs(first_forecast["cycle_id"], first_forecast["symbol"]) is None
    assert first_forecast["target_close"] == (REF + 3 * STEP).isoformat()
    second = env.consume(state=result["state"])
    assert second["status"] == "WAITING_FOR_READY" and len(env.outputs) == 2
    assert env.outputs[1]["symbol"] != first_forecast["symbol"]
    env.now += STEP
    assert set(env.service.pass_once()["prepared_symbols"]) == set(env.symbols)
    assert len(env.data_store.forecasts()) == 0


def test_slow_model_does_not_block_data_or_manual_stop(env):
    env.service.pass_once()
    entered, finish, stop = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def slow(*args, **kwargs):
        entered.set()
        assert finish.wait(10)
        return trained(*args, **kwargs)

    def factory(**kwargs):
        return ShadowRunner(**kwargs, trainer=slow)

    def consume():
        try:
            env.consume(stopped=stop.is_set, runner_factory=factory)
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    assert entered.wait(10)
    env.now += STEP
    result = env.service.pass_once()
    assert set(result["prepared_symbols"]) == set(env.symbols)
    stop.set()  # Stop affects only 1B; the data producer retains its own lifecycle.
    assert env.consume(stopped=stop.is_set)["status"] == "STOPPED"
    assert env.service.pass_once()["status"] == "prepared"
    finish.set()
    worker.join(10)
    assert not worker.is_alive() and not errors
    assert env.outputs == []


def test_acquisition_wait_and_corrupt_symbol_cannot_block_valid_model_work(env):
    env.service.pass_once()
    records = env.handoff.ready(env.clock(), configuration_hash=digest(__import__("dataclasses").asdict(env.config)))
    first = records[0]
    path = env.handoff._path("snapshots", first["snapshot_hash"])
    raw = path.read_bytes()
    path.write_bytes(raw.replace(b'"y":[0,1,0,1]', b'"y":[1,1,0,1]'))
    result = env.consume()
    assert result["status"] == "PUBLISHED"
    assert result["symbols"] != [first["symbol"]]
    assert len(env.outputs) == 1


def test_unavailable_acquisition_does_not_hold_another_ready_symbol(env):
    from ml.intraday.data import AcquisitionPending
    original = env.service.incoming
    class Waiting(dict):
        def get(self, symbol):
            if symbol == "AAPL":
                raise AcquisitionPending("fixture_provider_wait", retry_after_seconds=120)
            return super().get(symbol)
    env.service.incoming = lambda reference: Waiting(original(reference))
    result = env.service.pass_once()
    assert result["status"] == "partial_preparation" and result["prepared_symbols"] == ["IONQ"]
    model = env.consume()
    assert model["status"] == "PUBLISHED" and model["symbols"] == ["IONQ"]
    assert len(env.outputs) == 1


def test_late_ready_symbol_runs_while_another_model_lane_remains_busy(env, tmp_path):
    from ml.intraday.data import AcquisitionPending
    waiting = {"IONQ": True}
    original = env.service.incoming
    class Waiting(dict):
        def get(self, symbol):
            if waiting.get(symbol):
                raise AcquisitionPending("fixture_late_symbol", retry_after_seconds=120)
            return super().get(symbol)
    env.service.incoming = lambda reference: Waiting(original(reference))
    assert env.service.pass_once()["prepared_symbols"] == ["AAPL"]
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    errors = []
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(15)
        return trained(*args, **kwargs)
    def work():
        try:
            env.consume(assigned_symbols=("AAPL",), stopped=stopped.is_set,
                runner_factory=lambda **kwargs: ShadowRunner(**kwargs, trainer=slow))
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=work)
    worker.start()
    try:
        assert entered.wait(10)
        waiting["IONQ"] = False
        env.now += timedelta(seconds=121)
        assert env.service.pass_once()["status"] == "prepared"
        result = model_pass(store=env.model_store, queue=env.queue, session=env.session,
            config=env.config, handoff=env.handoff, clock=env.clock, assigned_symbols=("IONQ",),
            science_store=ShadowStore(tmp_path / "lane-ionq/models.sqlite3"), runner_factory=env.runner_factory)
        assert result["symbols"] == ["IONQ"] and worker.is_alive()
        assert [forecast["symbol"] for forecast in env.outputs] == ["IONQ"]
    finally:
        stopped.set()
        release.set()
        worker.join(15)
    assert not errors and not worker.is_alive()


def test_expired_or_superseded_records_never_become_new_model_work(env):
    env.service.pass_once()
    env.now += STEP
    assert env.consume()["status"] == "WAITING_FOR_READY"
    env.now += STEP
    assert env.consume()["status"] == "WAITING_FOR_READY"
    assert env.outputs == []


def test_prepared_cycle_reuses_small_index_after_data_process_recovery(env, monkeypatch):
    first = env.service.pass_once()
    monkeypatch.setattr(env.handoff, "publish", lambda *a, **k: pytest.fail("Repeated full snapshot publication"))
    monkeypatch.setattr(env.handoff, "load", lambda *a, **k: pytest.fail("Idle pass read a training matrix"))
    monkeypatch.setattr(env.service, "incoming", lambda *a: pytest.fail("Idle pass reopened provider inputs"))
    env.service.session_id = "new-data-process-lifecycle"
    second = env.service.pass_once()
    assert second["cycle_id"] == first["cycle_id"] and second["reused_ready_index"]


def test_data_publication_retry_reuses_original_snapshot_without_repreparing(env, monkeypatch):
    publish = env.handoff.publish
    failed = []
    def interrupted(cycle_id, symbol, cycle, snapshot):
        if symbol == "AAPL" and not failed:
            failed.append(snapshot[0])
            raise OSError("offline partial publication fixture")
        return publish(cycle_id, symbol, cycle, snapshot)
    monkeypatch.setattr(env.handoff, "publish", interrupted)
    first = env.service.pass_once()
    assert first["status"] == "partial_preparation"
    monkeypatch.setattr(env.runner, "dataset_builder", lambda *a, **k: pytest.fail("Retry reconstructed frozen model inputs"))
    second = env.service.pass_once()
    assert second["status"] == "prepared"
    record = next(row for row in env.handoff.ready(env.clock(), configuration_hash=digest(__import__("dataclasses").asdict(env.config)))
                  if row["symbol"] == "AAPL")
    assert record["snapshot_hash"] == failed[0]


def test_compact_publication_failure_reuses_original_forecast_without_refit(env):
    env.service.pass_once()
    calls, attempts, accepted = [], [], []
    def fit(*args, **kwargs):
        calls.append(1)
        return trained(*args, **kwargs)
    def enqueue(forecast):
        attempts.append(forecast)
        if len(attempts) == 1:
            raise OSError("offline queue commit interruption")
        accepted.append(forecast)
    env.queue.enqueue = enqueue
    factory = lambda **kwargs: ShadowRunner(**kwargs, trainer=fit)
    first = env.consume(assigned_symbols=("AAPL",), runner_factory=factory)
    assert first["state"]["attempted"]["AAPL"]["disposition"] == "publication_pending"
    env.now += timedelta(seconds=6)
    second = env.consume(assigned_symbols=("AAPL",), runner_factory=factory, state=first["state"])
    assert second["symbols"] == ["AAPL"] and calls == [1]
    assert len(accepted) == 1 and attempts[0] == accepted[0]


def test_partition_with_multiple_symbols_admits_one_fit_per_turn(env):
    env.service.pass_once()
    first = env.consume(max_symbols=1)
    assert len(first["symbols"]) == 1 and len(env.outputs) == 1
    second = env.consume(max_symbols=1, state=first["state"])
    assert len(second["symbols"]) == 1 and len(env.outputs) == 2
    assert first["symbols"] != second["symbols"]


def test_worker_count_override_preserves_scientific_handoff_and_other_changes_reject(env):
    env.service.pass_once()
    changed_science = replace(env.config, lookback_days=30)
    rejected = model_pass(store=env.model_store, queue=env.queue, session=env.session,
        config=changed_science, handoff=env.handoff, clock=env.clock,
        science_store=env.science_store, runner_factory=env.runner_factory)
    assert rejected["status"] == "WAITING_FOR_READY"
    worker_override = replace(env.config, symbol_workers=1)
    result = model_pass(store=env.model_store, queue=env.queue, session=env.session,
        config=worker_override, handoff=env.handoff, clock=env.clock, max_symbols=1,
        science_store=env.science_store, runner_factory=env.runner_factory)
    assert result["status"] == "PUBLISHED" and len(env.outputs) == 1
    forecast = env.outputs[0]
    records = env.handoff.ready(env.clock(), configuration_hash=digest(__import__("dataclasses").asdict(env.config)))
    source = next(record for record in records if record["symbol"] == forecast["symbol"])
    _, snapshot = env.handoff.load(source)
    assert env.science_store.inputs(forecast["cycle_id"], forecast["symbol"]) == (digest(snapshot), snapshot)
    assert forecast["target_close"] == source["target_close"]


@pytest.mark.parametrize("disposition", ["submitted", "submission_recovery_required"])
def test_execution_timing_does_not_claim_unknown_sends_are_submitted(env, disposition):
    from ml.intraday.live_session import _renewal_execution_pass
    class Checkpoint(BaseException):
        pass
    def checkpoint(*args):
        raise Checkpoint
    session = {**env.session, "architecture": ARCHITECTURE}
    renewal = SimpleNamespace(session=session, state={}, stopped=lambda: False, checkpoint=checkpoint)
    forecast = dict(forecast_id="a" * 64, symbol="AAPL", issued_at=(REF + STEP).isoformat(),
                    target_close=(REF + 3 * STEP).isoformat())
    queue = SimpleNamespace(due=lambda now: [forecast], broker_due=lambda now: True,
        complete=lambda *args: None, broker_wait=lambda **kwargs: None)
    executor = SimpleNamespace(step=lambda forecast: dict(kind="execution", disposition=disposition, evidence={}),
                               recover=lambda: None)
    with pytest.raises(Checkpoint):
        _renewal_execution_pass(store=env.model_store, queue=queue, executor=executor,
                                renewal=renewal, clock=env.clock)
    with env.model_store.connect() as db:
        event = json.loads(db.execute("SELECT payload FROM events WHERE kind='system_1b_execution_measured'").fetchone()[0])
    assert event["forecast_to_step_seconds"] == 60.
    assert (event["forecast_to_order_result_seconds"] is not None) == (disposition == "submitted")
    assert event["order_result"] == ("submitted" if disposition == "submitted" else "unknown_or_recovery_required")


def test_atlas_forecast_lane_branches_before_provider_or_history_setup(tmp_path, monkeypatch):
    from ml.intraday import live_session, split_model
    from ml.intraday.local_execution import LocalExecutionBinding
    from dataclasses import asdict
    binding = LocalExecutionBinding("Atlas", "pc-original", ("AAPL",), "a" * 64, "binding")
    session = dict(session_id="manual", actor="Atlas", symbols=["AAPL"],
        started_at=(REF + STEP).isoformat(), close_at=(REF + 4 * STEP).isoformat(),
        config=asdict(ResearchConfig()), binding_fingerprint="binding", architecture=ARCHITECTURE)
    renewal = SimpleNamespace(session=session, stopped=lambda: False, wait_due=lambda **k: None)
    monkeypatch.setattr(live_session, "local_symbols", lambda *a: ("Atlas", ("AAPL",)))
    monkeypatch.setattr(__import__("ml.intraday.local_execution", fromlist=["load_local_binding"]),
        "load_local_binding", lambda *a: binding)
    monkeypatch.setattr(live_session, "_renewal_histories", lambda *a: pytest.fail("Model lane loaded acquisition history"))
    monkeypatch.setattr(split_model, "renewal_model_pass", lambda **kwargs: {"status": "FIXTURE_READY"})
    result = live_session.run_renewal_lane("forecast", repository=tmp_path, datastore=tmp_path,
        profile=tmp_path / "profile", watchlist=tmp_path / "watchlist", renewal=renewal,
        clock=lambda: (REF + timedelta(minutes=16)).isoformat())
    assert result == {"status": "FIXTURE_READY"}


def test_data_service_keeps_its_daily_close_and_finishes_catchup_independently(tmp_path, monkeypatch):
    from ml.intraday import split_service, candle_collection
    from app.services import databento_market_data, schwab_token_store
    import dotenv
    now = [instant("2026-10-08T23:59:59+00:00")]
    collectors = []
    class OfflineCollector:
        def __init__(self, store, **kwargs):
            self.store = store
            collectors.append(kwargs)
            with store.connect() as db:
                db.execute("CREATE TABLE IF NOT EXISTS intraday_candle_collection(session_id TEXT,symbol TEXT,status TEXT)")
        def pass_once(self):
            return {"final_complete": True}
    monkeypatch.setattr(candle_collection, "CandleCollector", OfflineCollector)
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider",
                        lambda: SimpleNamespace(dataset="EQUS.MINI"))
    monkeypatch.setattr(schwab_token_store, "configure_local_repository", lambda *a: None)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(split_service, "local_symbols", lambda *a: ("Atlas", ("AAPL",)))
    monkeypatch.setenv("DUCKETS_PRODUCTION_WATCHLIST", os.environ.get("DUCKETS_PRODUCTION_WATCHLIST", ""))
    stop = tmp_path / "intraday/manual-session.json"
    stop.parent.mkdir()
    stop.write_text('{"status":"STOPPED"}')
    pauses = []
    def pause(seconds):
        pauses.append(seconds)
        assert len(pauses) < 500
        now[0] += timedelta(seconds=3)
        time.sleep(.01)
    result = split_service.run_data_service(repository=tmp_path, datastore=tmp_path,
        profile=tmp_path / "profile", watchlist=tmp_path / "watchlist", config=ResearchConfig(models=("fixture",)),
        clock=lambda: now[0].isoformat(), pause=pause)
    assert result == {"status": "COMPLETED", "final_collection_complete": True}
    assert collectors and all(row["close_at"] == instant("2026-10-09T00:00:00+00:00") for row in collectors)
    assert {row["session_id"] for row in collectors} == {"system-1a:atlas:2026-10-08:collection"}
    assert stop.read_text() == '{"status":"STOPPED"}'


@pytest.mark.parametrize("now,expected", [
    ("2026-10-09T10:54:59+00:00", "2026-10-09T00:00:00+00:00"),
    ("2026-10-09T10:55:00+00:00", "2026-10-10T00:00:00+00:00"),
    ("2026-10-10T11:16:00+00:00", "2026-10-09T00:00:00+00:00"),
    ("2026-10-11T11:16:00+00:00", "2026-10-09T00:00:00+00:00"),
    ("2026-10-12T11:16:00+00:00", "2026-10-13T00:00:00+00:00"),
])
def test_data_window_rollover_uses_only_explicit_local_schedule(now, expected):
    from ml.intraday.split_service import data_window_close
    original = "2026-10-09T00:00:00+00:00"
    assert data_window_close(now, original, daily_data_wake="03:55", data_weekdays=(0, 1, 2, 3, 4)) == instant(expected)
    assert data_window_close(now, original) == instant(original)


@pytest.mark.parametrize("wake,weekdays", [("03:55", ()), (None, (0,)), ("24:00", (0,)), ("3:55", (0,)), ("03:55", (7,))])
def test_data_window_rejects_incomplete_or_invalid_local_binding(wake, weekdays):
    from ml.intraday.split_service import data_window_close
    from ml.intraday.prediction import ContractError
    with pytest.raises(ContractError):
        data_window_close("2026-10-09T11:16:00Z", "2026-10-09T00:00:00Z",
                          daily_data_wake=wake, data_weekdays=weekdays)


def test_old_collector_exit_after_rollover_restarts_data_collection_on_next_turn():
    from ml.intraday.split_service import _ensure_collection_thread
    calls, alive = [], iter((True, False))
    old = SimpleNamespace(is_alive=lambda: next(alive))
    replacement = SimpleNamespace(is_alive=lambda: True)
    finished = threading.Event()
    start = lambda: calls.append("new-data-only-worker") or replacement
    now, close = instant("2026-10-09T11:16:00Z"), instant("2026-10-10T00:00:00Z")
    assert _ensure_collection_thread(old, once=False, now=now, close_at=close,
        finished=finished, start=start) is old
    # Old collector completes after rollover saw it alive, then returns.
    finished.set()
    assert _ensure_collection_thread(old, once=False, now=now, close_at=close,
        finished=finished, start=start) is replacement
    assert calls == ["new-data-only-worker"] and not finished.is_set()
    # A normally completed original close still exits without an extra worker.
    finished.set()
    assert _ensure_collection_thread(replacement, once=False, now=close, close_at=close,
        finished=finished, start=lambda: pytest.fail("Completed data window restarted")) is replacement


def test_pending_yesterday_collection_cannot_hold_up_next_authorized_data_window(tmp_path, monkeypatch):
    from ml.intraday import split_service, candle_collection, market_inputs, market_input_worker
    from app.services import databento_market_data, schwab_token_store
    from dataclasses import asdict
    import dotenv
    now = [instant("2026-10-08T23:59:59Z")]
    constructor_calls, context_calls, context_stops, prep_at = [], [], [], []
    class FixtureDone(BaseException):
        pass
    class PendingCollector:
        def __init__(self, store, **kwargs):
            constructor_calls.append(kwargs)
            with store.connect() as db:
                db.execute("CREATE TABLE IF NOT EXISTS intraday_candle_collection(session_id TEXT,symbol TEXT,reference_at TEXT,status TEXT,next_attempt_at TEXT,last_error TEXT,PRIMARY KEY(session_id,symbol,reference_at))")
                db.execute("INSERT OR IGNORE INTO intraday_candle_collection VALUES(?,?,?,'pending',?,NULL)",
                    (kwargs["session_id"], "AAPL", (kwargs["close_at"] - STEP).isoformat(), now[0].isoformat()))
        def pass_once(self):
            return {"final_complete": False}
    class ContextCollector:
        def __init__(self, *args):
            pass
        def start(self):
            context_calls.append("start")
            return {}
        def close(self):
            context_calls.append("close")
    class Incoming:
        def __init__(self, **kwargs):
            self.reference = kwargs["reference_at"]
        def bind_cycle(self, *args):
            pass
        def get(self, symbol):
            frame = history(symbol)
            frame["opened_at"], frame["available_at"] = self.reference, self.reference + STEP
            return frame
    def build(frame, *, cutoff, reference_at, **kwargs):
        value = dataset(frame, cutoff=cutoff, reference_at=reference_at, **kwargs)
        value.inference["information_available_at"] = (reference_at + STEP).isoformat()
        prep_at.append(now[0])
        return value
    monkeypatch.setattr(candle_collection, "CandleCollector", PendingCollector)
    monkeypatch.setattr(market_inputs, "MarketInputCollector", ContextCollector)
    monkeypatch.setattr(market_input_worker, "run_market_input_worker", lambda **kwargs: context_stops.append(kwargs["stopped"]))
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider", lambda: SimpleNamespace(dataset="EQUS.MINI"))
    monkeypatch.setattr(schwab_token_store, "configure_local_repository", lambda *args: None)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(split_service, "local_symbols", lambda *a: ("Atlas", ("AAPL",)))
    monkeypatch.setattr(split_service, "ShadowRunner", lambda **kwargs: ShadowRunner(**kwargs, dataset_builder=build,
        trainer=lambda *a, **k: pytest.fail("Data rollover trained a model")))
    monkeypatch.setattr(split_service, "LiveHistories", Incoming)
    monkeypatch.setenv("DUCKETS_PRODUCTION_WATCHLIST", os.environ.get("DUCKETS_PRODUCTION_WATCHLIST", ""))
    config = ResearchConfig(models=("fixture",), market_inputs_enabled=True)
    pauses = []
    def pause(seconds):
        pauses.append(seconds)
        assert len(pauses) < 100
        if len(pauses) == 1:
            now[0] = instant("2026-10-09T10:54:00Z")
        elif len(pauses) == 2:
            assert not prep_at  # Prior catch-up did not create a pre-wake window.
            now[0] = instant("2026-10-09T11:16:00Z")
        else:
            ready = ReadySnapshots(tmp_path / "intraday/system-1a/ready", actor="Atlas", symbols=("AAPL",),
                                   clock=lambda: now[0].isoformat())
            records = ready.ready(now[0], configuration_hash=digest(asdict(config)))
            if records:
                assert prep_at == [instant("2026-10-09T11:16:00Z")]
                assert records[0]["reference_at"] == "2026-10-09T11:00:00+00:00"
                with __import__("sqlite3").connect(tmp_path / "intraday/system-1a/collection.sqlite3") as db:
                    old = db.execute("SELECT status,close_at FROM system_1a_collection_days WHERE identity=?",
                        ("system-1a:atlas:2026-10-08:collection",)).fetchone()
                    assert old == ("pending", "2026-10-09T00:00:00+00:00")
                assert context_calls.count("start") == 2
                assert len(context_stops) == 2 and context_stops[0]() and not context_stops[1]()
                raise FixtureDone
        time.sleep(.02)
    with pytest.raises(FixtureDone):
        split_service.run_data_service(repository=tmp_path, datastore=tmp_path, profile=tmp_path / "profile",
            watchlist=tmp_path / "watchlist", config=config, clock=lambda: now[0].isoformat(), pause=pause,
            daily_data_wake="03:55", data_weekdays=(0, 1, 2, 3, 4))
    assert context_calls == ["start", "close", "start", "close"]


def test_completed_current_collection_yields_immediately_to_older_pending_day(tmp_path, monkeypatch):
    from ml.intraday import split_service, candle_collection
    from app.services import databento_market_data, schwab_token_store
    from dataclasses import asdict
    import dotenv
    config = ResearchConfig(models=("fixture",))
    source = ShadowStore(tmp_path / "intraday/system-1a/collection.sqlite3")
    old, current = "system-1a:atlas:2026-10-07:collection", "system-1a:atlas:2026-10-08:collection"
    with source.connect() as db:
        db.execute("CREATE TABLE system_1a_collection_days(identity TEXT PRIMARY KEY,close_at TEXT NOT NULL,configuration TEXT NOT NULL,symbols TEXT NOT NULL,status TEXT NOT NULL,last_attempt_at TEXT NOT NULL)")
        db.execute("CREATE TABLE intraday_candle_collection(session_id TEXT,symbol TEXT,reference_at TEXT,status TEXT,next_attempt_at TEXT,last_error TEXT)")
        for identity, close, status in ((old, "2026-10-08T00:00:00+00:00", "pending"),
                                        (current, "2026-10-09T00:00:00+00:00", "complete")):
            db.execute("INSERT INTO system_1a_collection_days VALUES(?,?,?,?,?,?)",
                (identity, close, json.dumps(asdict(config)), '["AAPL"]', status, close))
        db.execute("INSERT INTO intraday_candle_collection VALUES(?,?,?,'pending',?,NULL)",
            (old, "AAPL", "2026-10-07T23:45:00+00:00", "2026-10-08T00:00:00+00:00"))
    seen = []
    class FixtureDone(BaseException):
        pass
    class OfflineCollector:
        def __init__(self, store, **kwargs):
            seen.append(kwargs["session_id"])
        def pass_once(self):
            return {"final_complete": False}
    monkeypatch.setattr(candle_collection, "CandleCollector", OfflineCollector)
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider", lambda: SimpleNamespace(dataset="EQUS.MINI"))
    monkeypatch.setattr(schwab_token_store, "configure_local_repository", lambda *args: None)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(split_service, "local_symbols", lambda *a: ("Atlas", ("AAPL",)))
    monkeypatch.setenv("DUCKETS_PRODUCTION_WATCHLIST", os.environ.get("DUCKETS_PRODUCTION_WATCHLIST", ""))
    attempts = []
    def pause(seconds):
        attempts.append(1)
        assert len(attempts) < 200
        if seen:
            assert seen == [old]
            raise FixtureDone
        time.sleep(.01)
    with pytest.raises(FixtureDone):
        split_service.run_data_service(repository=tmp_path, datastore=tmp_path, profile=tmp_path / "profile",
            watchlist=tmp_path / "watchlist", config=config, clock=lambda: "2026-10-09T00:01:00+00:00", pause=pause)


def test_new_atlas_worker_lifecycle_owns_no_data_roles(tmp_path, monkeypatch):
    from tools import trader_worker
    from ml.intraday import live_session, runtime, resource_policy
    import dotenv
    from app.services import schwab_token_store
    monkeypatch.setenv("DUCKETS_PRODUCTION_WATCHLIST", os.environ.get("DUCKETS_PRODUCTION_WATCHLIST", ""))
    monkeypatch.setattr(schwab_token_store, "TOKEN_CACHE_PATH", schwab_token_store.TOKEN_CACHE_PATH)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "local_symbols", lambda *a: ("Atlas", ("AAPL",)))
    monkeypatch.setattr(resource_policy, "manual_symbol_workers", lambda *a, **k: 1)
    monkeypatch.setattr(live_session, "initialize_renewal_session",
        lambda **kwargs: {"session_id": "existing-manual", "architecture": ARCHITECTURE})
    renewal = SimpleNamespace(envelope=dict(repository=str(tmp_path), datastore=str(tmp_path / "data"),
        trader="intraday", profile=str(tmp_path / "profile"), watchlist=str(tmp_path / "watchlist"),
        options=dict(symbol_workers=None)), request={"role": "initialize"}, stopped=lambda: False)
    result = trader_worker.run(renewal)
    assert result["roles"] == ["execution", "forecast-0", "scoring"]


def _process_fixture(role, root):
    """Spawned fixture entrypoint; every data/model interface is prerecorded."""
    root = Path(root)
    def denied(*args, **kwargs):
        raise AssertionError("Offline child attempted provider/broker IO")
    socket.socket.connect = denied
    socket.create_connection = denied
    def wait_for(name):
        deadline = time.monotonic() + 20
        while not (root / name).exists():
            if time.monotonic() >= deadline:
                raise AssertionError("Offline process handshake timed out: " + name)
            time.sleep(.01)
    if role == "data":
        state = _environment(root)
        measurements = [state.service.pass_once()]
        (root / "data-ready").touch()
        wait_for("model-started")
        state.now += STEP
        measurements.append(state.service.pass_once())
        began = time.perf_counter()
        with state.data_store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
        writer_wait = time.perf_counter() - began
        (root / "data-while-model-busy").touch()
        wait_for("manual-stop")
        state.now += STEP
        measurements.append(state.service.pass_once())
        (root / "data-after-stop").touch()
        (root / "data-measurements.json").write_text(json.dumps({
            "preparation_pass_seconds": [row["preparation_pass_seconds"] for row in measurements],
            "preparation_db_writer_seconds_while_model_busy": writer_wait,
            "deadline_misses": sum(row["deadline_missed"] for row in measurements),
            "provider_or_broker_calls": 0, "model_fits": "prerecorded_fixture_only"}))
        return
    if role == "model":
        wait_for("data-ready")
        state = _environment(root)
        def slow(*args, **kwargs):
            (root / "model-started").touch()
            wait_for("finish-model")
            return trained(*args, **kwargs)
        began = time.perf_counter()
        result = state.consume(stopped=lambda: (root / "manual-stop").exists(),
            runner_factory=lambda **kwargs: ShadowRunner(**kwargs, trainer=slow))
        (root / "model-measurements.json").write_text(json.dumps({"model_pass_seconds": time.perf_counter()-began,
            "outputs_after_manual_stop": len(state.outputs), "status": result["status"]}))
        return
    if role == "recovery":
        wait_for("model-started")
        iterations = 0
        began = time.perf_counter()
        while not (root / "manual-stop").exists():
            iterations += 1  # Prerecorded fake order observation; no broker effect.
            (root / "recovery-responsive").write_text(str(iterations))
            time.sleep(.01)
        (root / "recovery-stop-seen").touch()
        (root / "recovery-measurements.json").write_text(json.dumps({"iterations": iterations,
            "loop_seconds": time.perf_counter()-began, "broker_effects": 0}))
        return
    raise AssertionError("Unknown offline child role")


def test_spawned_services_have_independent_lifetimes_and_recovery(tmp_path):
    """Separate interpreters prove no shared model/GIL/lifetime dependency."""
    from ml.intraday import live_session
    marker = tmp_path / "manual-stop"
    command = ("import sys,json; sys.path[:]=json.loads(sys.argv[3]); "
        "from tests.test_intraday_split_services import _process_fixture; "
        "_process_fixture(sys.argv[1],sys.argv[2])")
    children = []
    def wait_for(name):
        end = time.monotonic() + 30
        while not (tmp_path / name).exists():
            for child in children:
                if child.poll() not in (None, 0):
                    stdout, stderr = child.communicate(timeout=1)
                    pytest.fail("Offline child failed before handshake: " + (stdout + stderr).decode(errors="replace"))
            assert time.monotonic() < end, name
            time.sleep(.01)
    try:
        for role in ("data", "model", "recovery"):
            children.append(subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", command,
                role, str(tmp_path), json.dumps(sys.path)],
                cwd=Path(live_session.__file__).resolve().parents[2],
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        wait_for("data-while-model-busy")
        wait_for("recovery-responsive")
        assert children[1].poll() is None  # Model remains occupied in its own process.
        marker.touch()
        wait_for("recovery-stop-seen")
        wait_for("data-after-stop")
        assert children[1].poll() is None
        (tmp_path / "finish-model").touch()
        for child in children:
            stdout, stderr = child.communicate(timeout=30)
            assert child.returncode == 0, (stdout + stderr).decode(errors="replace")
        assert json.loads((tmp_path / "model-measurements.json").read_text())["outputs_after_manual_stop"] == 0
        assert json.loads((tmp_path / "data-measurements.json").read_text())["deadline_misses"] == 0
        assert json.loads((tmp_path / "recovery-measurements.json").read_text())["iterations"] > 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)


def test_reproducible_offline_stage_measurements(env, tmp_path):
    """Measured fixture overhead; prerecorded outputs do not claim fit speed."""
    saved_at, fake_order_delays = {}, []
    save_forecast = env.science_store.save_forecast
    def saved(payload, **kwargs):
        value = save_forecast(payload, **kwargs)
        saved_at[value["forecast_id"]] = time.perf_counter()
        return value
    env.science_store.save_forecast = saved
    def fake_order(forecast):
        fake_order_delays.append(time.perf_counter() - saved_at[forecast["forecast_id"]])
        env.outputs.append(forecast)
    env.queue.enqueue = fake_order
    prepared = env.service.pass_once()
    env.now += timedelta(seconds=1)
    env.consume()
    with env.model_store.connect() as db:
        event = json.loads(db.execute("SELECT payload FROM events WHERE kind='system_1b_model_pass'").fetchone()[0])
    report = {"kind": "offline_prerecorded_two_symbol_fixture", "production_training": False,
        "preparation_seconds": prepared["preparation_pass_seconds"],
        "queue_wait_seconds_by_symbol": event["queue_wait_seconds_by_symbol"],
        "model_pass_seconds": event["model_pass_seconds"],
        "forecast_to_fake_order_seconds": fake_order_delays,
        "deadline_misses": int(event["deadline_missed"]), "broker_effects": 0}
    (tmp_path / "stage-measurements.json").write_text(json.dumps(report, indent=2))
    assert len(fake_order_delays) == 2 and all(delay >= 0 for delay in fake_order_delays)
    assert report["deadline_misses"] == 0
