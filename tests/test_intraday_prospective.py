import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from ml.intraday.data import IntervalResponse, SessionBounds, FEATURE_NAMES
from ml.intraday.native_interval import resolve_native_interval
from ml.intraday.prediction import ContractError, STEP, digest, instant
from ml.intraday.prospective import (Collector, FrozenModels, RetainedProvider,
    REQUIRED_SOURCES, SCHEMA, bounded_existing_provider, slots)
from ml.intraday.prospective_worker import process_lock


class Clock:
    def __init__(self, value="2026-10-19T11:15:32Z"):
        self.now = instant(value)

    def __call__(self):
        return self.now


class Models:
    def __init__(self):
        self.calls = 0

    def predict(self, symbol, values):
        self.calls += 1
        assert len(values) == 16 and np.isfinite(values).all()
        return {"members": [{"name": "one", "artifact_sha256": "a" * 64,
            "version": "v1", "probability_not_down": .6}, {"name": "two",
            "artifact_sha256": "b" * 64, "version": "v2", "probability_not_down": .4}],
            "probability_not_down": .5, "aggregation": "arithmetic_mean_all_frozen_manual_members"}


def native(symbol, opened, received, *, sparse=False, factor=1):
    op, end = instant(opened), instant(opened) + STEP
    session = SessionBounds(op.date().isoformat(), op.replace(hour=8, minute=0).isoformat(),
                            (op.replace(hour=0, minute=0) + timedelta(days=1)).isoformat())
    rows = tuple({"opened_at": (op + timedelta(minutes=i)).isoformat(), "open": (100 + i / 100) * factor,
        "high": 102. * factor, "low": 99. * factor, "close": (101 + i / 100) * factor, "volume": 10 + i}
        for i in range(3 if sparse else 15))
    response = IntervalResponse("attempt2", symbol, "databento:EQUS.MINI:ohlcv-1m", "EQUS.MINI",
        op.isoformat(), end.isoformat(), (instant(received) - timedelta(seconds=1)).isoformat(),
        instant(received).isoformat(), "success", rows, op.isoformat(), end.isoformat())
    prior = replace(response, attempt_id="attempt1", request_started_at=(instant(received) - timedelta(seconds=8)).isoformat(),
                    request_ended_at=(instant(received) - timedelta(seconds=7)).isoformat())
    return resolve_native_interval([prior, response], session=session)["candle"]


def bind(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    warm = []
    for n in range(30):
        opened = instant("2026-10-16T10:00:00Z") + n * STEP
        row = native("GLOB", opened, opened + STEP + timedelta(seconds=30), factor=1 + n / 1000)
        warm.append(row)
    config = {"schema": SCHEMA, "experiment_id": "offline-mechanics", "state_root": str(tmp_path / "state"),
        "symbols": ["GLOB"], "source_dataset": "EQUS.MINI", "session_dates": ["2026-10-19"],
        "decision_start_local": "04:15", "decision_end_local": "04:15", "publication_offset_seconds": 30,
        "protocol": bind(tmp_path / "protocol.json", {"offline": True}),
        "recipe": bind(tmp_path / "recipe.json", {"offline": True}),
        "warmup": {"GLOB": {**bind(tmp_path / "warmup.json", {"candles": warm}),
                              "available_at": "2026-10-18T12:00:00Z"}},
        "source_sha256": {name: hashlib.sha256((Path(__file__).resolve().parents[1] / name).read_bytes()).hexdigest()
                          for name in REQUIRED_SOURCES},
        "outcome_recovery_seconds": 172800}
    model = Models()
    calls = []

    def acquire(symbols, slot, interval):
        calls.append(interval)
        return {symbol: native(symbol, interval, clock(), sparse=True) for symbol in symbols}

    collector = Collector(config, models=model, acquire=acquire, clock=clock)
    for n in range(12):
        interval = instant("2026-10-19T08:00:00Z") + n * STEP
        collector._save_candles({"GLOB": native("GLOB", interval, interval + STEP + timedelta(seconds=8))},
                                interval.isoformat())
    # Most cases isolate publication mechanics. Explicit background cases below
    # exercise the normal method without changing any production configuration.
    collector.collect_background = lambda: None
    return collector, config, model, clock, calls


def test_timely_publication_both_members_restart_duplicate_prevention(setup):
    collector, config, model, clock, calls = setup
    collector.tick()
    terminal = collector.store.rows("terminals")[0]
    assert terminal["status"] == "published"
    assert instant(terminal["published_at"]) < instant(terminal["target_open"])
    payload = collector.store.rows("publications")[0]
    assert len(payload["members"]) == 2
    assert payload["inputs_sha256"] == digest(payload["inputs"])
    assert len(payload["inputs"]["candles"]) == 21
    assert payload["inputs"]["candles"][-1]["source_empty_constituents"] == 12
    restarted = Collector(config, models=model, acquire=collector.acquire, clock=clock)
    restarted.collect_background = lambda: None
    restarted.tick()
    assert model.calls == 1 and len(calls) == 1
    assert len(restarted.store.rows("publications")) == 1


def test_late_start_records_missing_without_acquiring_or_predicting(setup):
    collector, config, model, clock, calls = setup
    clock.now = instant("2026-10-19T11:30:00Z")
    collector.tick()
    assert model.calls == 0 and not calls
    assert collector.store.rows("terminals")[0]["status"] == "missing_prediction"
    assert not collector.store.rows("publications")


def test_waits_until_scheduled_offset(setup):
    collector, _, model, clock, calls = setup
    clock.now = instant("2026-10-19T11:15:29Z")
    collector.tick()
    assert not calls and not model.calls and not collector.store.rows("terminals")


def test_inference_crossing_deadline_preserves_missing(setup):
    collector, _, model, clock, calls = setup
    original = model.predict

    def slow(symbol, values):
        result = original(symbol, values)
        clock.now = instant("2026-10-19T11:30:00Z")
        return result

    model.predict = slow
    collector.tick()
    assert collector.store.rows("terminals")[0]["reason"] == "inference_finished_after_deadline"
    assert not collector.store.rows("publications")


def test_publication_commit_crossing_deadline_is_unscorable_candidate(setup, monkeypatch):
    collector, _, _, clock, _ = setup
    original = collector.store.put

    def late_commit(table, slot, payload):
        original(table, slot, payload)
        if table == "publications":
            clock.now = instant("2026-10-19T11:30:00Z")

    monkeypatch.setattr(collector.store, "put", late_commit)
    collector.tick()
    assert collector.store.rows("terminals")[0]["reason"] == "publication_commit_completed_after_deadline"
    assert len(collector.store.rows("publications")) == 1
    assert not collector.store.rows("outcomes")


def test_interrupted_publication_is_never_backfilled(setup, monkeypatch):
    collector, config, model, clock, _ = setup
    original = collector.store.put

    def interrupted(table, slot, payload):
        original(table, slot, payload)
        if table == "publications":
            raise KeyboardInterrupt("crash after commit")

    monkeypatch.setattr(collector.store, "put", interrupted)
    with pytest.raises(KeyboardInterrupt):
        collector.tick()
    restarted = Collector(config, models=model, acquire=collector.acquire, clock=clock)
    restarted.collect_background = lambda: None
    restarted.tick()
    assert restarted.store.rows("terminals")[0]["reason"] == "interrupted_before_publication_commit_receipt"
    assert model.calls == 1


def test_acquisition_failure_retries_then_expires_truthfully(setup):
    collector, _, model, clock, _ = setup
    collector.acquire = lambda *args: {}
    collector.tick()
    assert not collector.store.rows("terminals") and not model.calls
    clock.now = instant("2026-10-19T11:30:00Z")
    collector.tick()
    assert collector.store.rows("terminals")[0]["status"] == "missing_prediction"


def test_future_or_wrong_source_candle_not_admitted(setup):
    collector, _, model, clock, _ = setup
    slot = collector.schedule[0]
    count_before = len(collector.store.rows("candles"))
    candle = native("GLOB", slot["reference_at"], clock())
    for change in ({"source_dataset": "FMP"}, {"available_at": "2026-10-19T11:20:00Z"},
                   {"source_id": "databento:XNAS.ITCH:ohlcv-1m"}, {"native_interval_contract": "fake"}):
        with pytest.raises(ContractError):
            collector._save_candles({"GLOB": {**candle, **change}}, slot["reference_at"])
    assert len(collector.store.rows("candles")) == count_before


def test_outcomes_collected_after_target_close_without_fit_or_score(setup):
    collector, _, model, clock, calls = setup
    collector.tick()
    clock.now = instant("2026-10-19T11:45:32Z")
    collector.tick()
    outcomes = collector.store.rows("outcomes")
    assert len(outcomes) == 1 and outcomes[0]["status"] == "observed"
    assert outcomes[0]["scored"] is False and model.calls == 1
    assert calls == ["2026-10-19T11:00:00+00:00", "2026-10-19T11:30:00+00:00"]
    collector.tick()
    assert len(calls) == 2


def test_outcomes_missing_at_fixed_grace_not_window_extension(setup):
    collector, _, _, clock, _ = setup
    collector.tick()
    collector.acquire = lambda *args: {}
    clock.now = instant("2026-10-21T11:45:00Z")
    collector.tick()
    assert collector.store.rows("outcomes")[0]["status"] == "missing_outcome"


def test_late_restart_retains_outcome_genuinely_received_before_grace(setup):
    collector, _, _, clock, _ = setup
    collector.tick()
    clock.now = instant("2026-10-19T11:45:32Z")
    interval = collector.schedule[0]["target_open"]
    collector._save_candles({"GLOB": native("GLOB", interval, clock())}, interval)
    clock.now = instant("2026-10-22T11:45:32Z")
    collector.tick()
    assert collector.store.rows("outcomes")[0]["status"] == "observed"


def test_outcome_received_after_grace_remains_late_missing(setup):
    collector, _, _, clock, _ = setup
    collector.tick()
    clock.now = instant("2026-10-22T11:45:32Z")
    interval = collector.schedule[0]["target_open"]
    collector._save_candles({"GLOB": native("GLOB", interval, clock())}, interval)
    collector.tick()
    assert collector.store.rows("outcomes")[0]["status"] == "missing_outcome"


def test_unexamined_premarket_interval_blocks_feature_population(setup):
    original, config, model, clock, _ = setup
    # A forward current-session interval is missing from both the known-source
    # coverage and observed candle store; do not skip it as if source-empty.
    collector = Collector({**config, "state_root": config["state_root"] + "-missing-premarket"},
                          models=model, clock=clock, acquire=original.acquire)
    collector.collect_background = lambda: None
    collector.tick()
    assert not model.calls and not collector.store.rows("publications")
    assert any("unexamined interval" in e.get("reason", "") for e in collector.store.rows("events"))


def test_background_capture_full_envelope_is_bounded_and_restart_safe(setup):
    collector, config, model, clock, calls = setup
    clock.now = instant("2026-10-19T11:45:32Z")
    Collector.collect_background(collector)
    assert len(calls) == 2
    assert calls == ["2026-10-19T11:00:00+00:00", "2026-10-19T11:15:00+00:00"]
    restarted = Collector(config, models=model, acquire=collector.acquire, clock=clock)
    restarted.collect_background()
    assert calls[-1] == "2026-10-19T11:30:00+00:00" and len(calls) == 3
    assert not restarted.store.rows("publications")


def test_conflicting_native_observation_is_reported_not_silently_ignored(setup):
    collector, _, _, clock, _ = setup
    interval = collector.schedule[0]["reference_at"]
    collector._save_candles({"GLOB": native("GLOB", interval, clock())}, interval)
    with pytest.raises(ContractError, match="Conflicting immutable"):
        collector._save_candles({"GLOB": native("GLOB", interval, clock(), factor=2)}, interval)


def test_background_pauses_before_pending_prediction_deadline(setup):
    collector, _, _, clock, calls = setup
    clock.now = instant("2026-10-19T11:29:00Z")
    Collector.collect_background(collector)
    assert not calls


def test_background_rechecks_runway_between_serial_groups(setup):
    original_collector, config, model, clock, calls = setup
    collector = Collector({**config, "state_root": config["state_root"] + "-background-budget"},
                          models=model, clock=clock, acquire=original_collector.acquire)
    clock.now = instant("2026-10-19T11:27:00Z")
    original = collector.acquire

    def takes_time(*args):
        result = original(*args)
        clock.now = instant("2026-10-19T11:28:10Z")
        return result

    collector.acquire = takes_time
    Collector.collect_background(collector)
    assert len(calls) == 1


def test_existing_provider_client_has_local_bounded_timeout(monkeypatch):
    from app.services.databento_market_data import DatabentoMarketDataProvider
    class API:
        TIMEOUT = 100
    client = SimpleNamespace(metadata=API(), timeseries=API())
    monkeypatch.setattr(DatabentoMarketDataProvider, "_client", lambda self: client)
    provider = bounded_existing_provider()
    returned = provider._client()
    assert returned.metadata.TIMEOUT == returned.timeseries.TIMEOUT == 5
    assert API.TIMEOUT == 100


def test_raw_response_retains_timestamp_index(setup, tmp_path):
    collector, _, _, clock, _ = setup
    frame = pd.DataFrame({"price": [1.0]}, index=pd.DatetimeIndex([clock()], name="ts_event"))
    provider = SimpleNamespace(dataset="EQUS.MINI", fetch_native_bars_range=lambda *a, **kw:
        ({"GLOB": ([], frame)}, "range"))
    wrapped = RetainedProvider(provider, tmp_path / "raw", collector.store, clock)
    wrapped.fetch_native_bars_range(["GLOB"], SimpleNamespace(schema="ohlcv-1m"),
        start=clock(), end=clock() + STEP)
    stored = pd.read_parquet(next((tmp_path / "raw").glob("*.parquet")))
    pd.testing.assert_frame_equal(stored, frame)


def test_frozen_identity_and_sql_immutability(setup):
    collector, config, model, clock, _ = setup
    collector.tick()
    changed = {**config, "experiment_id": "changed"}
    with pytest.raises(ContractError, match="different frozen experiment"):
        Collector(changed, models=model, acquire=collector.acquire, clock=clock)
    with collector.store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM publications")


def test_source_change_blocks_operation(setup, tmp_path):
    collector, _, _, _, _ = setup
    collector.config["source_sha256"]["ml/intraday/prospective.py"] = "0" * 64
    with pytest.raises(ContractError, match="implementation changed"):
        collector.tick()


def test_closed_day_no_slots_and_original_window_preserved(setup):
    _, config, _, _, _ = setup
    assert not slots({**config, "session_dates": ["2026-10-18"]})
    full = slots({**config, "decision_end_local": "16:30"})
    assert len(full) == 50
    assert instant(full[-1]["target_close"]) == instant("2026-10-20T00:00:00Z")
    with pytest.raises(ContractError):
        slots({**config, "decision_end_local": "16:45"})


def test_process_lock_releases_after_interruption(tmp_path):
    path = tmp_path / "worker.lock"
    with process_lock(path):
        with pytest.raises(ContractError, match="already owns"):
            with process_lock(path):
                pass
    with process_lock(path):
        pass


def test_no_trader_or_training_calls_in_worker_source():
    root = Path(__file__).resolve().parents[1]
    for name in ("prospective.py", "prospective_worker.py"):
        tree = ast.parse((root / "ml/intraday" / name).read_text())
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        assert not any(isinstance(n.func, ast.Attribute) and n.func.attr in {
            "fit", "fit_ensemble", "fit_calibrated_estimator", "place_order", "cancel_order"} for n in calls)


class FittedFixture:
    """Offline fitted-state fixture; fit is deliberately unavailable."""
    classes_ = np.array([0, 1])

    def __init__(self):
        self.base = SimpleNamespace(named_steps={"market_support":
            SimpleNamespace(retained_indices_=np.arange(16))})

    def predict_proba(self, X):
        assert np.isnan(X[:, 16:]).all()
        return np.array([[.3, .7]])


@pytest.fixture
def frozen_config(tmp_path):
    import joblib
    from ml.intraday.prediction import TARGET_VERSION
    recipe = {"target_version": TARGET_VERSION, "models": [{"name": "one"}, {"name": "two"}], "sha256": "fixture"}
    config = {"recipe": bind(tmp_path / "recipe.json", recipe),
        "experiment_id": "fixture", "decision_start_local": "04:15", "decision_end_local": "16:30",
        "publication_offset_seconds": 30, "session_dates": ["2026-10-19"],
        "symbols": ["GLOB"], "model_artifacts": {"GLOB": []}}
    artifacts = []
    for name in ("one", "two"):
        metadata = {"target_version": TARGET_VERSION, "spec": {"name": name},
            "dataset_metadata": {"manual_recipe_symbol": "GLOB", "manual_model_recipe": recipe,
                                 "native_interval_contract": "intraday-covered-native-interval-v1"},
            "training_cutoff": "2026-10-18T01:00:00Z", "latest_label_available_at": "2026-10-16T20:00:00Z",
            "feature_names": [*FEATURE_NAMES, "ctx_optional_missing"],
            "market_feature_support": {"retained_columns": list(FEATURE_NAMES)},
            "calibration": {"method": "purged_chronological_sigmoid", "base_rows": 100, "calibration_rows": 40}}
        path = tmp_path / (name + ".joblib")
        joblib.dump({"estimator": FittedFixture(), "metadata": metadata, "version": digest(metadata)}, path)
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        config["model_artifacts"]["GLOB"].append({"name": name, "path": str(path), "sha256": checksum,
                                                "version": digest(metadata)})
        artifacts.append({"name": name, "artifact_sha256": checksum, "status": "success"})
    evidence = bind(tmp_path / "fit.json", {"complete": True, "qualification": "not_claimed", "artifacts": artifacts})
    for model in config["model_artifacts"]["GLOB"]:
        model["fit_evidence"] = evidence
    config["protocol"] = bind(tmp_path / "protocol.json", {"qualification": "not_assessed",
        "experiment_id": "fixture", "symbols": ["GLOB"], "model_artifacts": config["model_artifacts"],
        "recipe_sha256": "fixture", "assessment_sessions": {"collection_dates": config["session_dates"]},
        "decision_window": {"timezone": "America/Los_Angeles", "first_decision": "04:15",
            "last_decision": "16:30", "offset_seconds": 30, "cadence_minutes": 15}})
    return config


def test_unqualified_but_fitted_models_collect_with_optional_context_missing(frozen_config):
    frozen = FrozenModels(frozen_config)
    result = frozen.predict("GLOB", np.zeros(16))
    assert len(result["members"]) == 2 and result["probability_not_down"] == .7
    assert result["optional_context"] == "missing_and_discarded_by_frozen_training_only_projection"


def test_wrong_artifact_bytes_fail_before_deserialization(frozen_config):
    frozen_config["model_artifacts"]["GLOB"][0]["sha256"] = "0" * 64
    with pytest.raises(ContractError):
        FrozenModels(frozen_config)


def test_missing_manual_member_rejected(frozen_config):
    frozen_config["model_artifacts"]["GLOB"].pop()
    with pytest.raises(ContractError, match="protocol scope"):
        FrozenModels(frozen_config)


def test_model_mutation_after_load_is_detected(frozen_config):
    frozen = FrozenModels(frozen_config)
    path = Path(frozen_config["model_artifacts"]["GLOB"][0]["path"])
    path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ContractError, match="changed after loading"):
        frozen.predict("GLOB", np.zeros(16))
