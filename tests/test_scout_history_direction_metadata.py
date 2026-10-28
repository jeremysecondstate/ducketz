"""Offline saved-package metadata checks; all rows are invented fixtures."""
from __future__ import annotations

import hashlib
import json

import pytest

from app.ui.gameplan_stats_data import _outcome, prediction_metrics
from ml.gameplan_stats_handoff import export_stats_package, read_stats_package
from tests.gameplan_stats_fixture import forecast, write_review


@pytest.fixture
def package(tmp_path):
    row = forecast("TEST", direction="BULLISH", probability=.2, change=.02)
    row.update(direction_policy_version="synthetic-historical-policy",
               direction_up_threshold=.9, direction_down_threshold=.1)
    root = tmp_path / "review"
    write_review(root, [row])
    return export_stats_package(root, producer="Scout", symbols=["TEST"],
                                destination=tmp_path / "stats.json")


def read(path):
    return read_stats_package(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def test_saved_direction_probability_target_and_policy_roundtrip_without_relabeling(package):
    original = package.read_bytes()
    payload, frame = read(package)
    row = frame.iloc[0]
    assert row.direction == "BULLISH" and row.calibrated_probability == .2
    assert row.direction_policy_version == "synthetic-historical-policy"
    assert row.direction_up_threshold == .9 and row.direction_down_threshold == .1
    assert row.probability_target_contract == payload["probability_target_contract"]
    assert row.model_observed_target == payload["rows"][0]["model_observed_target"]
    assert row.model_brier_score == payload["rows"][0]["model_brier_score"]
    outcomes = tuple(_outcome(item) for item in frame.to_dict("records"))
    assert outcomes[0].direction_policy_version == "synthetic-historical-policy"
    metrics = prediction_metrics(outcomes)
    assert metrics.scored == metrics.correct == 1
    assert metrics.brier == pytest.approx(.64)
    assert package.read_bytes() == original


def test_legacy_package_without_policy_metadata_is_retained(package):
    payload = json.loads(package.read_text())
    for key in ("direction_policy_version", "direction_up_threshold", "direction_down_threshold"):
        payload["rows"][0].pop(key)
    package.write_text(json.dumps(payload), encoding="utf-8")
    restored, frame = read(package)
    assert restored == payload
    assert frame.iloc[0].direction == "BULLISH"
    assert frame.iloc[0].calibrated_probability == .2
    assert "direction_policy_version" not in restored["rows"][0]
    assert _outcome(frame.iloc[0].to_dict()).direction_policy_version is None


@pytest.mark.parametrize("key", ["direction_up_threshold", "direction_down_threshold"])
@pytest.mark.parametrize("value", [True, "0.7", float("nan"), float("inf"), -.1, 1.1])
def test_invalid_saved_thresholds_are_rejected_without_rewriting(package, key, value):
    payload = json.loads(package.read_text())
    payload["rows"][0][key] = value
    package.write_text(json.dumps(payload), encoding="utf-8")
    original = package.read_bytes()
    with pytest.raises(ValueError, match="invalid saved direction thresholds"):
        read(package)
    assert package.read_bytes() == original


def test_reversed_saved_thresholds_are_rejected(package):
    payload = json.loads(package.read_text())
    payload["rows"][0].update(direction_up_threshold=.2, direction_down_threshold=.8)
    package.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="reversed saved direction thresholds"):
        read(package)


@pytest.mark.parametrize("thresholds", [
    {"direction_up_threshold": None, "direction_down_threshold": None},
    {"direction_up_threshold": 1, "direction_down_threshold": 0},
    {"direction_up_threshold": .5, "direction_down_threshold": .5},
    {"direction_up_threshold": .9},
    {"direction_down_threshold": .1},
])
def test_optional_and_inclusive_historical_thresholds_preserve_saved_call(package, thresholds):
    payload = json.loads(package.read_text())
    row = payload["rows"][0]
    row.pop("direction_up_threshold")
    row.pop("direction_down_threshold")
    row.update(thresholds)
    package.write_text(json.dumps(payload), encoding="utf-8")
    original = package.read_bytes()
    restored, frame = read(package)
    assert restored == payload
    assert frame.iloc[0].direction == "BULLISH"
    assert frame.iloc[0].calibrated_probability == .2
    assert package.read_bytes() == original


@pytest.mark.parametrize("status", ["RESEARCH_NOT_PROMOTED", "RESEARCH_NO_TARGET_HISTORY"])
def test_excluded_research_rows_still_reject_invalid_saved_thresholds(package, status):
    payload = json.loads(package.read_text())
    payload["rows"][0].update(model_status=status, direction_up_threshold=True)
    package.write_text(json.dumps(payload), encoding="utf-8")
    original = package.read_bytes()
    with pytest.raises(ValueError, match="invalid saved direction thresholds"):
        read(package)
    assert package.read_bytes() == original


def test_known_current_policy_alias_keeps_identifier_and_explicit_description(package):
    payload = json.loads(package.read_text())
    payload["rows"][0].update(direction_policy_version="stock-direction-50-01-v3",
                              direction_up_threshold=.5001, direction_down_threshold=.4999,
                              direction="BEARISH", direction_correct=False)
    package.write_text(json.dumps(payload), encoding="utf-8")
    _, frame = read(package)
    outcome = _outcome(frame.iloc[0].to_dict())
    assert outcome.direction_policy_version == "stock-direction-50-01-v3"
    assert "BUY at 50.01% or higher; SELL at 49.99% or lower" in outcome.direction_policy_description
