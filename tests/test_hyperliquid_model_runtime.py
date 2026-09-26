"""Exercise scheduling and lifecycle without live data, accounts, or real fits."""
from dataclasses import asdict
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from filelock import FileLock, Timeout
import pandas as pd
import pytest

import ml.hyperliquid_model_runtime as runtime


class Harness:
    def __init__(self, tmp_path, monkeypatch, symbols=("BTC", "ETH")):
        self.root = tmp_path / "data"
        self.markets_path = tmp_path / "markets.json"
        self.config_path = tmp_path / "models.json"
        self.now = 2_000_000_000.0
        self.snapshots = {}
        self.forecasts = {}
        self.prediction_calls = []
        self.score_calls = []
        self.published = []
        self.loaded = []
        self.trained = []
        self.write_markets(symbols)
        self.config_path.write_text(json.dumps({
            "version": 1, "markets_config": "markets.json", "poll_seconds": 0.01,
            "retrain_seconds": 100, "retry_seconds": 10,
        }), encoding="utf-8")
        for symbol in symbols:
            self.new_snapshot(symbol)
        monkeypatch.setattr(runtime, "model_dir", self.model_dir)
        monkeypatch.setattr(runtime, "publish_candidate", self.publish)
        monkeypatch.setattr(runtime, "load_predictor", self.load_predictor)
        monkeypatch.setattr(runtime, "record_prediction", self.record_prediction)
        monkeypatch.setattr(runtime, "score_matured", self.score_matured)

    def write_markets(self, symbols):
        self.markets_path.write_text(json.dumps({
            "version": 1, "symbols": list(symbols), "interval": "15m",
            "output_root": str(self.root),
        }), encoding="utf-8")

    def new_snapshot(self, symbol, run_id=None):
        previous = self.snapshots.get(symbol)
        number = getattr(previous, "number", 0) + 1
        snapshot = SimpleNamespace(
            coin=symbol, interval="15m", run_id=run_id or f"{symbol}-run-{number}",
            feature_revision="test-features-v1", number=number,
            decision_close=f"decision-{number}",
        )
        self.snapshots[symbol] = snapshot
        path = self.root / symbol / "15m" / "latest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"run_id": snapshot.run_id}), encoding="utf-8")
        return snapshot

    def model_dir(self, output_root, coin, interval, horizon):
        assert Path(output_root) == self.root
        return self.root / "_models" / coin / interval / f"h{horizon}"

    def loader(self, output_root, coin, interval):
        assert Path(output_root) == self.root
        self.loaded.append((coin, interval))
        return self.snapshots[coin]

    def train(self, snapshot, settings):
        self.trained.append((snapshot.coin, snapshot.run_id))
        return {"bundle": snapshot.coin, "report": {"eligible": True, "metrics": {}, "timings": {}}}

    def candidate(self, worker, symbol, *, age=0, active=True, **changes):
        snapshot = self.snapshots[symbol]
        key = (symbol, "15m", 4)
        record = {
            "model_id": f"{symbol}-model-{len(self.published)}",
            "trained_at_utc": pd.Timestamp(self.now - age, unit="s", tz="UTC").isoformat(),
            "settings": asdict(worker.config.model_settings(4)),
            "source_run_id": snapshot.run_id, "feature_revision": snapshot.feature_revision,
            "eligible": active, **changes,
        }
        path = self.model_dir(self.root, *key)
        path.mkdir(parents=True, exist_ok=True)
        (path / "candidate.json").write_text(json.dumps(record), encoding="utf-8")
        if active:
            (path / "active.json").write_text(json.dumps(record), encoding="utf-8")
        return record

    def publish(self, output_root, snapshot, settings, result, *, now):
        self.published.append(snapshot.coin)
        record = {
            "model_id": f"{snapshot.coin}-new-{len(self.published)}",
            "trained_at_utc": pd.Timestamp(now, unit="s", tz="UTC").isoformat(),
            "settings": asdict(settings), "source_run_id": snapshot.run_id,
            "feature_revision": snapshot.feature_revision, "eligible": result["report"]["eligible"],
        }
        path = self.model_dir(output_root, snapshot.coin, snapshot.interval, settings.horizon_bars)
        path.mkdir(parents=True, exist_ok=True)
        (path / "candidate.json").write_text(json.dumps(record), encoding="utf-8")
        if record["eligible"]:
            (path / "active.json").write_text(json.dumps(record), encoding="utf-8")
        return record

    def load_predictor(self, output_root, coin, interval, horizon, *, now, max_age_seconds,
                       expected_feature_revision=None):
        directory = self.model_dir(output_root, coin, interval, horizon)
        for filename, role in (("active.json", "active"), ("candidate.json", "research_candidate")):
            path = directory / filename
            if path.exists():
                record = json.loads(path.read_text(encoding="utf-8"))
                if (0 <= now - pd.Timestamp(record["trained_at_utc"]).timestamp() <= max_age_seconds
                        and record.get("feature_revision") == expected_feature_revision):
                    return {"bundle": coin, "record": record, "role": role}
        return None

    def predict(self, bundle, snapshot):
        assert bundle == snapshot.coin
        self.prediction_calls.append((snapshot.coin, snapshot.run_id))
        return {"decision_close": snapshot.decision_close, "probability_not_down": 0.6}

    def record_prediction(self, output_root, coin, interval, horizon, prediction, metadata, *, now):
        identity = (coin, interval, horizon, prediction["decision_close"])
        self.forecasts.setdefault(identity, {**prediction, "model_id": metadata["model_id"], "role": metadata["role"]})
        return self.forecasts[identity]

    def score_matured(self, output_root, snapshot, horizon, *, now):
        self.score_calls.append((snapshot.coin, snapshot.run_id))
        return {"scored_rows": 0}

    def worker(self, **kwargs):
        return runtime.ModelRuntime(
            self.config_path, train_fn=kwargs.pop("train_fn", self.train),
            predict_fn=self.predict, snapshot_loader=self.loader,
            clock=lambda: self.now, **kwargs,
        )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def collect_finished_job(worker):
    """Wait for the one submitted fit, then let the runtime collect it."""
    assert worker._job is not None
    try:
        worker._job["future"].result(timeout=5)
    except RuntimeError:
        pass
    return worker.tick()


def test_predictions_continue_for_new_candles_while_single_background_fit_is_blocked(harness):
    started, release = threading.Event(), threading.Event()

    def train(snapshot, settings):
        harness.trained.append((snapshot.coin, snapshot.run_id))
        started.set()
        assert release.wait(5)
        return {"bundle": snapshot.coin, "report": {"eligible": True}}

    worker = harness.worker(train_fn=train)
    harness.candidate(worker, "BTC", age=101)
    harness.candidate(worker, "ETH", age=101)
    harness.new_snapshot("BTC")
    harness.new_snapshot("ETH")
    try:
        worker.tick()
        assert started.wait(2)
        assert worker._job["key"] == ("BTC", "15m", 4)
        harness.new_snapshot("BTC")
        harness.new_snapshot("ETH")
        state = worker.tick()
        assert harness.prediction_calls[-2:] == [("BTC", "BTC-run-3"), ("ETH", "ETH-run-3")]
        assert state["markets"]["BTC/15m/h4"]["training"] is True
        assert harness.trained == [("BTC", "BTC-run-2")]
        assert state["markets"]["ETH/15m/h4"]["training_attempts"] == 0
    finally:
        release.set()
        worker.shutdown()
    assert harness.published == []


def test_dispatch_is_fair_and_uses_latest_snapshot_when_training_slot_opens(harness):
    started, release = threading.Event(), threading.Event()

    def train(snapshot, settings):
        if snapshot.coin == "BTC":
            started.set()
            assert release.wait(5)
        return harness.train(snapshot, settings)

    worker = harness.worker(train_fn=train)
    try:
        worker.tick()
        assert started.wait(2)
        harness.new_snapshot("ETH")
        harness.new_snapshot("ETH")
        worker.tick()
        release.set()
        state = collect_finished_job(worker)
        assert state["training_market"] == "ETH/15m/h4"
        assert worker._job["snapshot"].run_id == "ETH-run-3"
        collect_finished_job(worker)
        assert harness.trained == [("BTC", "BTC-run-1"), ("ETH", "ETH-run-3")]
    finally:
        release.set()
        worker.shutdown()


def test_unchanged_snapshot_does_not_duplicate_predictions_or_reload_data(harness):
    worker = harness.worker()
    for symbol in ("BTC", "ETH"):
        harness.candidate(worker, symbol)
    try:
        worker.tick()
        first_scores = list(harness.score_calls)
        worker.tick()
        harness.now += 50
        worker.tick()
        assert harness.prediction_calls == [("BTC", "BTC-run-1"), ("ETH", "ETH-run-1")]
        assert harness.loaded == [("BTC", "15m"), ("ETH", "15m")]
        assert harness.score_calls == first_scores
        assert harness.trained == []
    finally:
        worker.shutdown()


def test_new_snapshot_before_retraining_cadence_only_predicts(harness):
    worker = harness.worker()
    for symbol in ("BTC", "ETH"):
        harness.candidate(worker, symbol)
    try:
        worker.tick()
        harness.now += 50
        harness.new_snapshot("ETH")
        worker.tick()
        assert harness.prediction_calls[-1] == ("ETH", "ETH-run-2")
        assert harness.trained == []
    finally:
        worker.shutdown()


@pytest.mark.parametrize(("cadence", "expected_due"), [(900, True), (3600, False)])
def test_retraining_cadence_uses_source_candle_progress_not_publication_delay(harness, cadence, expected_due):
    config = json.loads(harness.config_path.read_text(encoding="utf-8"))
    config["retrain_seconds"] = cadence
    harness.config_path.write_text(json.dumps(config), encoding="utf-8")
    current_close = pd.Timestamp(harness.now, unit="s", tz="UTC")
    source_close = current_close - pd.Timedelta(minutes=15)
    harness.snapshots["BTC"].features = pd.DataFrame({"close_time": [current_close]})
    worker = harness.worker()
    harness.candidate(worker, "ETH")
    # Fitting finished five minutes after the source candle, ten minutes ago.
    harness.candidate(
        worker, "BTC", age=600, source_run_id="earlier-candle-snapshot",
        source_last_close_utc=source_close.isoformat(),
    )
    try:
        state = worker.tick()
        assert bool(state["training_market"]) is expected_due
        if expected_due:
            assert state["training_market"] == "BTC/15m/h4"
        assert state["markets"]["BTC/15m/h4"]["last_prediction"]
    finally:
        worker.shutdown()


@pytest.mark.parametrize("source_close", ["invalid-close", "NaT", "2033-05-18T03:33:20", "2100-01-01T00:00:00Z"])
def test_invalid_source_close_requests_retraining_without_crashing_other_markets(harness, source_close):
    harness.snapshots["BTC"].features = pd.DataFrame({
        "close_time": [pd.Timestamp(harness.now, unit="s", tz="UTC")],
    })
    worker = harness.worker()
    harness.candidate(worker, "ETH")
    harness.candidate(
        worker, "BTC", source_run_id="earlier-candle-snapshot", source_last_close_utc=source_close,
    )
    try:
        state = worker.tick()
        assert state["training_market"] == "BTC/15m/h4"
        assert state["markets"]["ETH/15m/h4"]["last_prediction"]
    finally:
        worker.shutdown()


def test_persisted_fresh_candidates_prevent_refit_after_runtime_restart(harness):
    original = harness.worker()
    for symbol in ("BTC", "ETH"):
        harness.candidate(original, symbol)
    original.shutdown()
    restarted = harness.worker()
    try:
        restarted.tick()
        assert harness.trained == []
        assert len(harness.forecasts) == 2
    finally:
        restarted.shutdown()


def test_old_candidate_does_not_retrain_without_new_source_data(harness):
    worker = harness.worker()
    for symbol in ("BTC", "ETH"):
        harness.candidate(worker, symbol, age=1000)
    try:
        worker.tick()
        assert harness.trained == []
        harness.new_snapshot("ETH")
        worker.tick()
        assert worker._job["key"] == ("ETH", "15m", 4)
    finally:
        worker.shutdown()


def test_add_remove_universe_preserves_artifacts_and_discards_removed_fit(harness):
    started, release = threading.Event(), threading.Event()

    def train(snapshot, settings):
        if snapshot.coin == "BTC":
            started.set()
            assert release.wait(5)
        return harness.train(snapshot, settings)

    worker = harness.worker(train_fn=train)
    sentinel = harness.root.parent / "schwab-sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    try:
        worker.tick()
        assert started.wait(2)
        harness.write_markets(["ETH", "HYPE"])
        harness.new_snapshot("HYPE")
        release.set()
        state = collect_finished_job(worker)
        assert list(state["markets"]) == ["ETH/15m/h4", "HYPE/15m/h4"]
        assert "BTC" not in harness.published
        assert ("BTC", "15m") not in worker._snapshots
        collect_finished_job(worker)
        collect_finished_job(worker)
        assert sorted(harness.published) == ["ETH", "HYPE"]
        assert sentinel.read_text(encoding="utf-8") == "unchanged"
        assert (harness.root / "BTC" / "15m" / "latest.json").exists()
    finally:
        release.set()
        worker.shutdown()


def test_invalid_live_market_edit_keeps_last_valid_universe(harness):
    worker = harness.worker()
    for symbol in ("BTC", "ETH"):
        harness.candidate(worker, symbol)
    try:
        worker.tick()
        harness.markets_path.write_text("{", encoding="utf-8")
        harness.new_snapshot("ETH")
        state = worker.tick()
        assert state["config_error"]["type"] == "JSONDecodeError"
        assert list(state["markets"]) == ["BTC/15m/h4", "ETH/15m/h4"]
        assert harness.prediction_calls[-1] == ("ETH", "ETH-run-2")
        harness.write_markets(["ETH"])
        assert worker.tick()["config_error"] is None
    finally:
        worker.shutdown()


def test_failure_in_one_symbol_does_not_prevent_other_training_or_predictions(harness):
    def train(snapshot, settings):
        if snapshot.coin == "BTC":
            raise RuntimeError("synthetic fit failure")
        return harness.train(snapshot, settings)

    worker = harness.worker(train_fn=train)
    try:
        worker.tick()
        state = collect_finished_job(worker)
        assert state["markets"]["BTC/15m/h4"]["last_error"]["message"] == "synthetic fit failure"
        assert state["training_market"] == "ETH/15m/h4"
        state = collect_finished_job(worker)
        assert state["markets"]["ETH/15m/h4"]["last_prediction"]
        assert harness.published == ["ETH"]
        worker.tick()
        assert worker._job is None
    finally:
        worker.shutdown()


@pytest.mark.parametrize("fit_fails", [False, True])
def test_runtime_status_retains_only_latest_hundred_completed_jobs(harness, fit_fails):
    def train(snapshot, settings):
        if fit_fails:
            raise RuntimeError("synthetic fit failure")
        return harness.train(snapshot, settings)

    worker = harness.worker(train_fn=train)
    worker._completed_jobs = [{"sequence": number, "market": "old-market"} for number in range(100)]
    try:
        worker.tick()
        state = collect_finished_job(worker)
        history = state["completed_jobs"]
        assert len(history) == 100
        assert history[0]["sequence"] == 1
        assert history[-1]["market"] == "BTC/15m/h4"
        assert ("error" in history[-1]) is fit_fails
        saved = json.loads((worker.directory / "status.json").read_text(encoding="utf-8"))
        assert saved["completed_jobs"] == history
    finally:
        worker.shutdown()


def test_missing_market_data_does_not_hold_up_other_market(harness):
    (harness.root / "BTC" / "15m" / "latest.json").unlink()
    worker = harness.worker()
    try:
        state = worker.tick()
        assert state["markets"]["BTC/15m/h4"]["status"] == "waiting_for_data"
        assert state["training_market"] == "ETH/15m/h4"
        state = collect_finished_job(worker)
        assert state["markets"]["ETH/15m/h4"]["last_prediction"]
    finally:
        worker.shutdown()


@pytest.mark.parametrize("timestamp", ["not-a-timestamp", None, "NaT", "2033-05-18T03:33:20", "2100-01-01T00:00:00Z"])
def test_invalid_candidate_timestamp_does_not_crash_other_markets(harness, timestamp):
    worker = harness.worker()
    harness.candidate(worker, "BTC", trained_at_utc=timestamp)
    harness.candidate(worker, "ETH")
    try:
        state = worker.tick()
        assert state["markets"]["ETH/15m/h4"]["last_prediction"]
        assert state["status"] == "running"
    finally:
        worker.shutdown()


@pytest.mark.parametrize("value", [["bad-record"], "bad-record", True, None])
def test_nonobject_candidate_can_be_replaced_without_affecting_other_markets(harness, value):
    worker = harness.worker()
    for symbol in ("BTC", "ETH"):
        harness.candidate(worker, symbol)
    candidate_path = harness.model_dir(harness.root, "BTC", "15m", 4) / "candidate.json"
    candidate_path.write_text(json.dumps(value), encoding="utf-8")
    try:
        state = worker.tick()
        assert state["markets"]["ETH/15m/h4"]["last_prediction"]
        assert state["training_market"] == "BTC/15m/h4"
        collect_finished_job(worker)
        assert harness.published == ["BTC"]
    finally:
        worker.shutdown()


def test_shutdown_waits_for_running_fit_and_discards_its_result(harness):
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def train(snapshot, settings):
        started.set()
        assert release.wait(5)
        return harness.train(snapshot, settings)

    worker = harness.worker(train_fn=train)
    worker.tick()
    assert started.wait(2)

    def shutdown():
        worker.shutdown()
        stopped.set()

    closer = threading.Thread(target=shutdown)
    closer.start()
    try:
        assert not stopped.wait(0.05)
        release.set()
        assert stopped.wait(2)
        assert harness.published == []
        assert worker._status == "stopped"
        assert worker._slots[("BTC", "15m", 4)]["status"] == "discarded_after_removal_or_stop"
    finally:
        release.set()
        closer.join(timeout=5)


def test_once_skips_fresh_candidates_but_force_train_updates_each_market(harness):
    worker = harness.worker()
    for symbol in ("BTC", "ETH"):
        harness.candidate(worker, symbol)
    first = worker.run(once=True)
    assert first["reason"] == "once"
    assert harness.trained == []
    forced = harness.worker(force_train=True)
    state = forced.run(once=True)
    assert state["reason"] == "once"
    assert harness.trained == [("BTC", "BTC-run-1"), ("ETH", "ETH-run-1")]
    assert len(harness.forecasts) == 2


def test_runtime_lock_prevents_second_owner_without_touching_its_status(harness):
    worker = harness.worker()
    worker.directory.mkdir(parents=True, exist_ok=True)
    status_path = worker.directory / "status.json"
    status_path.write_text('{"status":"owner-sentinel"}', encoding="utf-8")
    with FileLock(str(worker.directory / ".runtime.lock"), timeout=0):
        with pytest.raises(Timeout):
            worker.run(once=True)
        assert json.loads(status_path.read_text(encoding="utf-8"))["status"] == "owner-sentinel"


def test_status_and_stop_controls_do_not_parse_invalid_config(harness, capsys):
    harness.config_path.write_text("{broken", encoding="utf-8")
    assert runtime.main(["--config", str(harness.config_path), "--output-root", str(harness.root), "--status"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "not_started"
    assert runtime.main(["--config", str(harness.config_path), "--output-root", str(harness.root), "--stop"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_stopped"
    assert not (runtime._runtime_dir(harness.root) / "stop.request").exists()


def test_stop_marker_is_consumed_and_runtime_lock_released(harness):
    worker = harness.worker()
    worker.directory.mkdir(parents=True, exist_ok=True)
    marker = worker.directory / "stop.request"
    marker.write_text("{}", encoding="utf-8")
    state = worker.run()
    assert state["reason"] == "stop_requested"
    assert not marker.exists()
    assert harness.trained == []
    assert runtime.read_status(harness.root)["runtime_running"] is False
    with FileLock(str(worker.directory / ".runtime.lock"), timeout=0):
        assert runtime.request_stop(harness.root)["status"] == "stop_requested"
        assert marker.exists()
    assert runtime.request_stop(harness.root)["status"] == "already_stopped"
    assert not marker.exists()


@pytest.mark.parametrize("eligible", [True, False])
def test_worker_timing_excludes_collection_wait_and_persists_publication_timing(harness, monkeypatch, eligible):
    harness.write_markets(["BTC"])
    timer = SimpleNamespace(value=0.0)

    def train(snapshot, settings):
        timer.value += 2.0
        return {"bundle": snapshot.coin, "report": {
            "eligible": eligible, "total_seconds": 1.9,
            "model_timings": {"test": {"fit_seconds": 1.0, "calibration_seconds": 0.5,
                                       "assessment_seconds": 0.3, "total_seconds": 1.8}},
        }}

    def publish(*args, **kwargs):
        timer.value += 0.25
        return harness.publish(*args, **kwargs)

    monkeypatch.setattr(runtime, "publish_candidate", publish)
    worker = harness.worker(train_fn=train, timer=lambda: timer.value)
    try:
        worker.tick()
        worker._job["future"].result(timeout=5)
        # Simulate a slow polling/collection cycle after the worker finished.
        timer.value = 10.0
        state = worker.tick()
        slot = state["markets"]["BTC/15m/h4"]
        timing = slot["last_training_timing"]
        assert timing["worker_seconds"] == 2.0
        assert timing["queue_wait_seconds"] == 0.0
        assert timing["collection_wait_seconds"] == 8.0
        assert timing["scheduler_observed_seconds"] == slot["last_training_seconds"] == 10.0
        assert timing["publication_seconds"] == 0.25
        assert timing["operation_seconds"] == 2.25
        assert timing["end_to_end_seconds"] == 10.25
        assert state["completed_jobs"][-1]["timing"] == timing
        event_path = worker.directory / "training_events.jsonl"
        events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
        assert len(events) == 1
        event = events[0]
        assert event["outcome"] == ("promoted" if eligible else "research_only")
        assert event["timing"] == timing
        assert event["model_id"] == slot["candidate"]["model_id"]
        assert event["source_run_id"] == "BTC-run-1"
        assert event["report_total_seconds"] == 1.9
        assert event["model_timings"]["test"]["calibration_seconds"] == 0.5
        # No duplicate timing events just because the runtime polls again.
        worker.tick()
        assert len(event_path.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        worker.shutdown()
    restarted = harness.worker()
    try:
        restarted.tick()
        assert len(event_path.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        restarted.shutdown()


@pytest.mark.parametrize("failure_stage", ["training", "publication"])
def test_failed_training_or_publication_keeps_measured_work_and_failure_event(harness, monkeypatch, failure_stage):
    harness.write_markets(["BTC"])
    timer = SimpleNamespace(value=0.0)

    def train(snapshot, settings):
        timer.value += 2.0
        if failure_stage == "training":
            raise RuntimeError("synthetic fit failure")
        return harness.train(snapshot, settings)

    def publish(*args, **kwargs):
        timer.value += 0.5
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(runtime, "publish_candidate", publish)
    worker = harness.worker(train_fn=train, timer=lambda: timer.value)
    try:
        worker.tick()
        try:
            worker._job["future"].result(timeout=5)
        except RuntimeError:
            pass
        timer.value = 10.0
        state = worker.tick()
        slot = state["markets"]["BTC/15m/h4"]
        timing = slot["last_training_timing"]
        assert timing["worker_seconds"] == 2.0
        assert timing["collection_wait_seconds"] == 8.0
        assert timing["publication_seconds"] == (None if failure_stage == "training" else 0.5)
        assert slot["status"] == "training_failed"
        assert slot["last_error"]["type"] == ("RuntimeError" if failure_stage == "training" else "OSError")
        event = json.loads((worker.directory / "training_events.jsonl").read_text(encoding="utf-8"))
        assert event["outcome"] == f"{failure_stage}_failed"
        assert event["timing"] == timing
        assert "model_id" not in event
        assert state["training_market"] is None
    finally:
        worker.shutdown()


def test_discarded_fit_keeps_timing_without_claiming_publication(harness):
    harness.write_markets(["BTC"])
    worker = harness.worker()
    worker.tick()
    worker._job["future"].result(timeout=5)
    state = worker.shutdown()
    event = json.loads((worker.directory / "training_events.jsonl").read_text(encoding="utf-8"))
    assert event["outcome"] == "discarded_after_removal_or_stop"
    assert event["timing"]["worker_seconds"] >= 0
    assert event["timing"]["publication_seconds"] is None
    assert event["timing"]["operation_seconds"] is None
    assert state["completed_jobs"][-1]["timing"] == event["timing"]
    assert harness.published == []


def test_timing_journal_failure_does_not_reclassify_published_model(harness, monkeypatch):
    harness.write_markets(["BTC"])
    original_open = Path.open

    def open_with_failed_journal(path, *args, **kwargs):
        if path.name == "training_events.jsonl":
            raise PermissionError("synthetic telemetry failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_with_failed_journal)
    worker = harness.worker()
    try:
        worker.tick()
        state = collect_finished_job(worker)
        assert harness.published == ["BTC"]
        assert state["markets"]["BTC/15m/h4"]["last_error"] is None
        assert state["markets"]["BTC/15m/h4"]["last_prediction"]
        assert state["completed_jobs"][-1]["outcome"] == "promoted"
        assert state["timing_write_error"]["type"] == "PermissionError"
    finally:
        worker.shutdown()
