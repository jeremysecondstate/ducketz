"""Model releases and honest forward outcomes, independent of model algorithms."""
from dataclasses import dataclass
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml import hyperliquid_model_artifacts as artifacts


BASE = pd.Timestamp("2026-09-25T12:00:00Z")
NOW = BASE.timestamp() + 5


@dataclass(frozen=True)
class Recipe:
    horizon_bars: int = 4
    random_state: int = 42


def snapshot(prices=None, times=None, run_id="source-1"):
    if times is None:
        times = pd.date_range(BASE - pd.Timedelta(hours=1), BASE, freq="15min")
    if prices is None:
        prices = [100.0] * len(times)
    return SimpleNamespace(
        coin="BTC", interval="15m", run_id=run_id, feature_revision=1,
        feature_names=("momentum", "ema"),
        features=pd.DataFrame({"close_time": times, "close": prices}),
    )


def publish(root, *, eligible=True, now=NOW, bundle=None):
    result = {
        "bundle": {"fitted_coefficients": [1.5, 2.0]} if bundle is None else bundle,
        "report": {"eligible": eligible, "metric": 0.17},
        "assessment": pd.DataFrame({"p_not_down": [0.2, 0.8], "actual": [0, 1]}),
    }
    return artifacts.publish_candidate(root, snapshot(), Recipe(), result, now=now)


def prediction(decision=BASE, *, p=0.8, horizon=4):
    return {
        "decision_timestamp_utc": (decision - pd.Timedelta(minutes=15)).isoformat(),
        "decision_close_utc": decision.isoformat(),
        "decision_price": 100.0,
        "target_close_utc": (decision + pd.Timedelta(minutes=15 * horizon)).isoformat(),
        "p_not_down": p, "p_down": 1 - p,
        "per_model": {"linear": {"p_not_down": p, "p_down": 1 - p}},
        "data_run_id": "prediction-data-1",
    }


def record(root, metadata=None, *, decision=BASE, now=NOW, p=0.8):
    metadata = metadata or publish(root)
    return artifacts.record_prediction(root, "BTC", "15m", 4, prediction(decision, p=p), metadata, now=now)


def directory(root):
    return artifacts.model_dir(root, "BTC", "15m", 4)


def test_publication_roundtrip_preserves_exact_evaluated_bundle(tmp_path):
    bundle = {"fitted_coefficients": [1.25, 0.33], "fit_cutoff": "earlier-than-assessment"}
    metadata = publish(tmp_path, bundle=bundle)
    loaded = artifacts.load_predictor(tmp_path, "BTC", "15m", 4, now=NOW + 1, max_age_seconds=60)
    assert loaded["bundle"] == bundle
    assert loaded["record"] == metadata
    assert loaded["role"] == "active"
    assert loaded["record"]["settings"] == {"horizon_bars": 4, "random_state": 42}
    assert pd.read_parquet(metadata["assessment_path"])["actual"].tolist() == [0, 1]


def test_unqualified_candidate_does_not_displace_active(tmp_path):
    first = publish(tmp_path)
    candidate = publish(tmp_path, eligible=False, now=NOW + 10)
    loaded = artifacts.load_predictor(tmp_path, "BTC", "15m", 4, now=NOW + 20, max_age_seconds=30)
    assert loaded["record"]["model_id"] == first["model_id"]
    assert loaded["role"] == "active"
    loaded = artifacts.load_predictor(tmp_path, "BTC", "15m", 4, now=NOW + 35, max_age_seconds=30)
    assert loaded["record"]["model_id"] == candidate["model_id"]
    assert loaded["role"] == "research_candidate"


def test_model_age_expires_without_returning_stale_bundle(tmp_path):
    publish(tmp_path, eligible=False)
    assert artifacts.load_predictor(tmp_path, "BTC", "15m", 4, now=NOW + 61, max_age_seconds=60) is None
    assert artifacts.load_predictor(tmp_path, "BTC", "15m", 4, now=NOW - 1, max_age_seconds=60) is None


def test_schema_change_can_use_new_candidate_instead_of_old_active(tmp_path):
    active = publish(tmp_path)
    data = snapshot()
    data.feature_revision = 2
    candidate = artifacts.publish_candidate(tmp_path, data, Recipe(), {
        "bundle": {"fitted_coefficients": [2]}, "report": {"eligible": False},
        "assessment": pd.DataFrame({"actual": [0, 1]}),
    }, now=NOW + 10)
    loaded = artifacts.load_predictor(
        tmp_path, "BTC", "15m", 4, now=NOW + 20, max_age_seconds=60,
        expected_feature_revision=2,
    )
    assert loaded["record"]["model_id"] == candidate["model_id"]
    assert loaded["record"]["model_id"] != active["model_id"]
    assert loaded["role"] == "research_candidate"
    assert artifacts.load_predictor(
        tmp_path, "BTC", "15m", 4, now=NOW + 20, max_age_seconds=60,
        expected_feature_revision=3,
    ) is None


def test_failed_bundle_publication_preserves_both_pointers(tmp_path, monkeypatch):
    first = publish(tmp_path)
    pointer_before = (directory(tmp_path) / "candidate.json").read_bytes()
    def fail(*args, **kwargs):
        raise OSError("write failed")
    monkeypatch.setattr(artifacts.joblib, "dump", fail)
    with pytest.raises(OSError, match="write failed"):
        publish(tmp_path, now=NOW + 10)
    assert (directory(tmp_path) / "candidate.json").read_bytes() == pointer_before
    assert json.loads((directory(tmp_path) / "active.json").read_text())["model_id"] == first["model_id"]


def test_failed_assessment_publication_does_not_promote(tmp_path, monkeypatch):
    first = publish(tmp_path)
    def fail(*args, **kwargs):
        raise OSError("parquet failed")
    monkeypatch.setattr(pd.DataFrame, "to_parquet", fail)
    with pytest.raises(OSError, match="parquet failed"):
        publish(tmp_path, now=NOW + 10)
    assert json.loads((directory(tmp_path) / "candidate.json").read_text())["model_id"] == first["model_id"]


def test_bundle_load_ignores_pointer_paths_and_uses_owned_run(tmp_path):
    metadata = publish(tmp_path)
    pointer = directory(tmp_path) / "active.json"
    altered = dict(metadata, bundle_path="https://example.invalid/untrusted.joblib")
    pointer.write_text(json.dumps(altered))
    loaded = artifacts.load_predictor(tmp_path, "BTC", "15m", 4, now=NOW + 1, max_age_seconds=60)
    assert loaded["record"]["bundle_path"] == metadata["bundle_path"]


@pytest.mark.parametrize("bad_id", ["../foreign", "C:/foreign", "https://example.invalid", "not-a-run"])
def test_bundle_load_rejects_traversal_and_foreign_ids(tmp_path, bad_id):
    publish(tmp_path)
    for name in ("candidate.json", "active.json"):
        (directory(tmp_path) / name).write_text(json.dumps({"model_id": bad_id}))
    assert artifacts.load_predictor(tmp_path, "BTC", "15m", 4, now=NOW + 1, max_age_seconds=60) is None


def test_prediction_dedup_preserves_first_probability_model_and_creation(tmp_path):
    original = record(tmp_path)
    another = publish(tmp_path, now=NOW + 2)
    duplicate = record(tmp_path, another, now=NOW + 3, p=0.1)
    assert duplicate == original
    assert len(pd.read_parquet(directory(tmp_path) / "predictions.parquet")) == 1
    assert json.loads((directory(tmp_path) / "latest_prediction.json").read_text()) == original


@pytest.mark.parametrize("projection_state", ["missing", "stale"])
def test_duplicate_repairs_latest_projection_without_regressing_to_older_forecast(tmp_path, projection_state):
    metadata = publish(tmp_path)
    first = record(tmp_path, metadata)
    later_time = BASE + pd.Timedelta(minutes=15)
    latest = record(tmp_path, metadata, decision=later_time, now=later_time.timestamp() + 5, p=0.7)
    journal = directory(tmp_path) / "predictions.parquet"
    journal_before = journal.read_bytes()
    projection = directory(tmp_path) / "latest_prediction.json"
    if projection_state == "missing":
        projection.unlink()
    else:
        projection.write_text(json.dumps(first), encoding="utf-8")
    duplicate = record(tmp_path, metadata, now=later_time.timestamp() + 10, p=0.1)
    assert duplicate == first
    assert json.loads(projection.read_text()) == latest
    assert journal.read_bytes() == journal_before


def test_prediction_keeps_original_per_model_probabilities(tmp_path):
    original = record(tmp_path)
    row = pd.read_parquet(directory(tmp_path) / "predictions.parquet").iloc[0]
    assert json.loads(row["per_model_json"]) == original["per_model"]


def test_research_forecast_is_explicitly_unqualified(tmp_path):
    metadata = publish(tmp_path, eligible=False)
    written = record(tmp_path, metadata)
    assert written["role"] == "research_candidate"
    assert written["qualified"] is False


@pytest.mark.parametrize("decision, now", [
    (BASE + pd.Timedelta(minutes=15), NOW),
    (BASE, BASE.timestamp() + 3600),
    (BASE, BASE.timestamp() + 3601),
])
def test_does_not_create_future_decision_or_backdated_forecast(tmp_path, decision, now):
    with pytest.raises(ValueError, match="still-future"):
        record(tmp_path, decision=decision, now=now)
    assert not (directory(tmp_path) / "predictions.parquet").exists()


@pytest.mark.parametrize("changes", [
    {"p_not_down": 1.2, "p_down": -0.2},
    {"p_not_down": 0.9, "p_down": 0.9},
    {"p_not_down": float("nan")},
    {"decision_price": 0},
    {"target_close_utc": (BASE + pd.Timedelta(hours=2)).isoformat()},
])
def test_rejects_inconsistent_prediction_values(tmp_path, changes):
    item = prediction()
    item.update(changes)
    with pytest.raises(ValueError):
        artifacts.record_prediction(tmp_path, "BTC", "15m", 4, item, publish(tmp_path), now=NOW)


def test_model_identity_must_match_prediction_market(tmp_path):
    metadata = publish(tmp_path)
    metadata["coin"] = "ETH"
    with pytest.raises(ValueError, match="different market"):
        record(tmp_path, metadata)


def test_score_requires_exact_target_not_the_next_observed_candle(tmp_path):
    record(tmp_path)
    target = BASE + pd.Timedelta(hours=1)
    incomplete = snapshot(times=[BASE, target - pd.Timedelta(minutes=15), target + pd.Timedelta(minutes=15)])
    metrics = artifacts.score_matured(tmp_path, incomplete, 4, now=target.timestamp() + 1000)
    assert metrics["n"] == 0
    complete = snapshot(times=[BASE, target], prices=[100.0, 110.0])
    metrics = artifacts.score_matured(tmp_path, complete, 4, now=target.timestamp())
    assert metrics["n"] == 1
    assert metrics["accuracy"] == 1
    assert metrics["brier"] == pytest.approx(0.04)
    assert metrics["by_role"]["active"]["n"] == 1


def test_score_ignores_future_snapshot_rows_until_wall_clock_maturity(tmp_path):
    record(tmp_path)
    target = BASE + pd.Timedelta(hours=1)
    future = snapshot(times=[BASE, target], prices=[100, 105])
    assert artifacts.score_matured(tmp_path, future, 4, now=target.timestamp() - 1)["n"] == 0


def test_score_preserves_original_outcome_and_probability_across_revisions(tmp_path):
    forecast = record(tmp_path)
    target = BASE + pd.Timedelta(hours=1)
    data = snapshot(times=[BASE, target], prices=[999, 100], run_id="outcome-source-1")
    metrics = artifacts.score_matured(tmp_path, data, 4, now=target.timestamp() + 1)
    assert metrics["n"] == 1
    outcome = pd.read_parquet(directory(tmp_path) / "outcomes.parquet").iloc[0]
    assert outcome["prediction_id"] == forecast["prediction_id"]
    assert outcome["target_not_down"] == 1  # Flat is not-down, consistently.
    assert outcome["actual_return"] == 0  # Original decision price, not revised 999.
    assert outcome["p_not_down"] == forecast["p_not_down"]
    revised = snapshot(times=[BASE, target], prices=[100, 90], run_id="revised")
    second = artifacts.score_matured(tmp_path, revised, 4, now=target.timestamp() + 2)
    assert second["new_outcomes"] == 0
    assert second["brier"] == metrics["brier"]
    assert pd.read_parquet(directory(tmp_path) / "outcomes.parquet").iloc[0]["outcome_data_run_id"] == "outcome-source-1"


def test_prevents_scoring_a_journal_forecast_created_after_target(tmp_path):
    record(tmp_path)
    path = directory(tmp_path) / "predictions.parquet"
    frame = pd.read_parquet(path)
    target = BASE + pd.Timedelta(hours=1)
    frame["created_at_utc"] = target.isoformat()
    frame.to_parquet(path, index=False)
    data = snapshot(times=[BASE, target], prices=[100, 110])
    assert artifacts.score_matured(tmp_path, data, 4, now=target.timestamp() + 10)["n"] == 0


def test_parquet_replace_retries_transient_windows_reader(tmp_path, monkeypatch):
    original = artifacts.os.replace
    attempts = []
    def locked_once(source, destination):
        attempts.append(destination)
        if len(attempts) == 1:
            raise PermissionError("reader still open")
        return original(source, destination)
    monkeypatch.setattr(artifacts.os, "replace", locked_once)
    monkeypatch.setattr(artifacts.time, "sleep", lambda _: None)
    path = tmp_path / "test.parquet"
    artifacts._atomic_parquet(path, pd.DataFrame({"close": [100.0]}))
    assert len(attempts) == 2
    assert pd.read_parquet(path)["close"].tolist() == [100.0]
    assert len(list(tmp_path.iterdir())) == 1


def test_permanent_parquet_replace_failure_preserves_previous_journal(tmp_path, monkeypatch):
    path = tmp_path / "test.parquet"
    pd.DataFrame({"close": [100.0]}).to_parquet(path, index=False)
    original = path.read_bytes()
    def always_locked(*_):
        raise PermissionError("locked")
    monkeypatch.setattr(artifacts.os, "replace", always_locked)
    monkeypatch.setattr(artifacts.time, "sleep", lambda _: None)
    with pytest.raises(PermissionError):
        artifacts._atomic_parquet(path, pd.DataFrame({"close": [110.0]}))
    assert path.read_bytes() == original
    assert len(list(tmp_path.iterdir())) == 1
