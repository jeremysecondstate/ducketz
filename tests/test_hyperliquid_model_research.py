from dataclasses import replace
import json

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression

from datafetching.hyperliquid_candles import INTERVAL_MS
from ml import hyperliquid_model_research as research
from ml.hyperliquid_data_pipeline import build_labels
from ml.hyperliquid_model_research import (
    CURRENT_MEMBERS, _json_safe, blend_probabilities, context_positions, prepare_data, run_experiment,
)
from ml.hyperliquid_models import ModelSettings, _training_rows
from test_hyperliquid_models import snapshot, write_snapshot


def settings():
    return ModelSettings(min_train_rows=120, split_mode="fractions", train_fraction=.7,
                         calibration_fraction=.15, assessment_fraction=.15)


def interval_snapshot(interval, horizon, count=800):
    source = snapshot(count)
    features = source.features.copy()
    step = pd.Timedelta(milliseconds=INTERVAL_MS[interval])
    features["timestamp"] = pd.date_range("2026-01-01", periods=count, freq=step, tz="UTC")
    features["close_time"] = features.timestamp + step
    features["interval"] = interval
    return replace(source, interval=interval, features=features,
                   labels=build_labels(features, interval=interval, horizons=(horizon,)))


def test_cached_sequence_context_is_past_only_and_matches_tabular_decision():
    source = snapshot(400)
    data = prepare_data(source, settings(), sequence_length=32)
    for name, block in data["blocks"].items():
        sequence = data["arrays"][name]["sequence"]
        assert sequence.shape == (len(block), 32, len(source.feature_names))
        np.testing.assert_array_equal(sequence[:, -1, :], data["arrays"][name]["scaled"])
        assert sequence.dtype == np.float32
        assert sequence.flags.c_contiguous
    fit = data["blocks"]["fit"]
    cal = data["blocks"]["calibration"]
    assert fit.label_end_time.max() < cal.close_time.min()
    assert cal.label_end_time.max() < data["blocks"]["assessment"].close_time.min()


def test_sequence_context_cannot_jump_over_a_missing_candle():
    source = snapshot(400)
    features = source.features.drop(index=[280]).reset_index(drop=True)
    source = replace(source, features=features, labels=build_labels(features, interval="15m"))
    rows = _training_rows(source, 4)
    positions, keep = context_positions(source, rows, 32)
    step = pd.Timedelta(minutes=15)
    for pos in positions:
        stamps = source.features.close_time.iloc[pos-31:pos+1]
        assert (stamps.diff().dropna() == step).all()
    assert (~keep).sum() > 31


@pytest.mark.parametrize("interval,horizon", [("15m", 4), ("5m", 1)])
def test_assessment_feature_distribution_does_not_fit_preprocessing(interval, horizon):
    source = interval_snapshot(interval, horizon, 400)
    recipe = replace(settings(), horizon_bars=horizon)
    first = prepare_data(source, recipe)
    boundary = first["blocks"]["assessment"].close_time.min()
    changed = source.features.copy()
    changed.loc[changed.close_time >= boundary, list(source.feature_names)] = 1000000.0
    second = prepare_data(replace(source, features=changed), recipe)
    np.testing.assert_array_equal(first["imputer"].statistics_, second["imputer"].statistics_)
    np.testing.assert_array_equal(first["scaler"].mean_, second["scaler"].mean_)
    np.testing.assert_array_equal(first["arrays"]["fit"]["sequence"], second["arrays"]["fit"]["sequence"])


def test_blends_use_declared_weights_and_do_not_overweight_added_tree_families():
    probabilities = {name: np.array([.2, .8]) for name in CURRENT_MEMBERS}
    probabilities.update({name: np.array([.8, .2]) for name in ("cnn", "gru", "cnn_gru")})
    probabilities["other_tree"] = np.array([.99, .99])
    blends = blend_probabilities(probabilities, [*CURRENT_MEMBERS, "other_tree"])
    np.testing.assert_allclose(blends["current_plus_sequence"], [.5, .5])
    np.testing.assert_allclose(blends["current_plus_cnn_gru"], [.35, .65])
    np.testing.assert_allclose(blends["current_four"], [.2, .8])


def test_current_blend_uses_configured_weights():
    probabilities = {name: np.array([value, 1 - value])
                     for name, value in zip(CURRENT_MEMBERS, (.1, .5, .8, .9))}
    weights = dict(zip(CURRENT_MEMBERS, (.4, .2, .2, .2)))
    result = blend_probabilities(probabilities, CURRENT_MEMBERS, current_weights=weights)
    np.testing.assert_allclose(result["current_four"], [.48, .52])


def test_manifest_serializes_optional_estimator_nan_parameters_without_invalid_json():
    manifest = _json_safe({"missing": np.nan, "seed": np.int64(42), "nested": [np.inf]})
    assert json.loads(json.dumps(manifest, allow_nan=False)) == {"missing": "nan", "seed": 42, "nested": ["inf"]}


@pytest.mark.parametrize("size", [0, -1, 1.5, True])
def test_invalid_context_length_rejected(size):
    source = snapshot(400)
    with pytest.raises(ValueError, match="sequence_length"):
        context_positions(source, _training_rows(source, 4), size)


@pytest.mark.parametrize("interval,fractions", [("5m", (.6, .2, .2)), ("1h", (.8, .1, .1))])
def test_named_protocol_rejects_other_fractions_before_input_reads(tmp_path, interval, fractions):
    market_path = tmp_path / "markets.json"
    market_path.write_text(json.dumps({"version": 1, "symbols": ["BTC"], "interval": interval,
                                      "output_root": str(tmp_path / "absent-data")}))
    config = tmp_path / "models.json"
    config.write_text(json.dumps({"version": 1, "markets_config": str(market_path), "split_mode": "fractions",
                                  "train_fraction": fractions[0], "calibration_fraction": fractions[1],
                                  "assessment_fraction": fractions[2]}))
    with pytest.raises(ValueError, match="protocol requires"):
        run_experiment(config)
    assert not (tmp_path / "absent-data").exists()


@pytest.mark.parametrize("interval,horizon", [("5m", 1), ("15m", 4), ("30m", 2)])
def test_experiment_uses_configured_interval_horizon_and_recipe_without_changing_source(
        tmp_path, monkeypatch, interval, horizon):
    source = interval_snapshot(interval, horizon)
    root = tmp_path / "data"
    dataset, _ = write_snapshot(root, source)
    original_pointer = (dataset / "latest.json").read_bytes()
    markets = tmp_path / "markets.json"
    markets.write_text(json.dumps({"version": 1, "symbols": ["BTC"], "interval": interval,
                                   "output_root": str(root)}))
    config = tmp_path / "models.json"
    config.write_text(json.dumps({
        "version": 1, "markets_config": str(markets), "horizons_bars": [horizon],
        "min_train_rows": 120, "calibration_rows": 50, "assessment_rows": 60,
        "model_threads": 1, "split_mode": "fractions", "train_fraction": .7,
        "calibration_fraction": .15, "assessment_fraction": .15,
        "calibration_c": .1, "logistic_weight": .4, "extra_trees_weight": .2,
        "hist_gradient_boosting_weight": .2, "mlp_weight": .2,
    }))
    # Exercise research plumbing and real fitting/calibration with inexpensive
    # estimators. Production controls below still use the real four-model recipe.
    monkeypatch.setattr(research, "make_classical_estimators", lambda **kw: (
        {name: {"estimator": LogisticRegression(max_iter=100, random_state=42), "scaled": True}
         for name in CURRENT_MEMBERS}, {}))
    calibration_cs = []
    def calibrator(**kwargs):
        calibration_cs.append(kwargs["C"])
        return LogisticRegression(**kwargs)
    monkeypatch.setattr(research, "LogisticRegression", calibrator)
    control_settings = []
    real_train = research.train_candidate
    def train_control(pinned, recipe):
        control_settings.append(recipe)
        return real_train(pinned, recipe)
    monkeypatch.setattr(research, "train_candidate", train_control)

    output = root / "_model_research" / "test"
    summary = run_experiment(config, output=output, include_sequences=False, sequence_length=4)
    protocol = json.loads((output / "protocol.json").read_text())
    report = json.loads((output / "BTC" / "report.json").read_text())
    assessment = pd.read_parquet(output / "BTC" / "assessment.parquet")
    for result in (summary, protocol, report):
        assert result["interval"] == interval
        assert result["horizon_bars"] == horizon
        assert result["horizon_minutes"] == horizon * INTERVAL_MS[interval] / 60_000
    assert (assessment.label_end_time - assessment.close_time ==
            pd.Timedelta(milliseconds=horizon * INTERVAL_MS[interval])).all()
    assert report["split_details"]["boundary_purged_rows"] == {
        "fit_to_calibration": horizon, "calibration_to_assessment": horizon}
    fit, cal, assess = (report["splits"][name] for name in ("fit", "calibration", "assessment"))
    assert pd.Timestamp(fit["last_label_end_utc"]) < pd.Timestamp(cal["first_decision_close_utc"])
    assert pd.Timestamp(cal["last_label_end_utc"]) < pd.Timestamp(assess["first_decision_close_utc"])
    assert calibration_cs == [.1] * len(CURRENT_MEMBERS)
    fixed, fractional = control_settings
    assert replace(fixed, split_mode="fractions", train_fraction=.7,
                   calibration_fraction=.15, assessment_fraction=.15) == fractional
    assert fixed.calibration_rows == 50 and fixed.assessment_rows == 60
    weights = dict(zip(CURRENT_MEMBERS, (.4, .2, .2, .2)))
    assert report["current_four_weights"] == weights
    expected = sum(assessment[f"{name}_p_not_down"] * weight for name, weight in weights.items())
    np.testing.assert_allclose(assessment.current_four_p_not_down, expected)
    assert (dataset / "latest.json").read_bytes() == original_pointer
    assert not (root / "_models").exists()
    assert not (root / "_paper").exists()


@pytest.mark.parametrize("horizons,error", [([1, 4], "exactly one"), ([12], "no future_return_12bar")])
def test_research_rejects_ambiguous_or_unavailable_horizons_before_output(tmp_path, horizons, error):
    root = tmp_path / "data"
    write_snapshot(root, interval_snapshot("5m", 1))
    markets = tmp_path / "markets.json"
    markets.write_text(json.dumps({"version": 1, "symbols": ["BTC"], "interval": "5m",
                                   "output_root": str(root)}))
    config = tmp_path / "models.json"
    config.write_text(json.dumps({"version": 1, "markets_config": str(markets),
                                  "horizons_bars": horizons, "split_mode": "fractions",
                                  "train_fraction": .7, "calibration_fraction": .15,
                                  "assessment_fraction": .15}))
    with pytest.raises(ValueError, match=error):
        run_experiment(config)
    assert not (root / "_model_research").exists()
