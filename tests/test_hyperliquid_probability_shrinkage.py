"""Persisted neutral shrinkage must preserve families and honest assessment."""
from dataclasses import asdict, replace
import json
from types import SimpleNamespace

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.metrics import brier_score_loss, log_loss
from threadpoolctl import threadpool_limits

from ml import hyperliquid_models as models
from ml import hyperliquid_model_artifacts as artifacts
from ml.hyperliquid_data_pipeline import build_labels
from ml.hyperliquid_model_config import ModelConfig, load_config
from ml.hyperliquid_model_runtime import ModelRuntime
from tests.test_hyperliquid_models import snapshot, settings, small_models, logistic_only


FIELD = "ensemble_probability_shrinkage"
WEIGHTS = dict(logistic=.4, extra_trees=.1, hist_gradient_boosting=.1875,
               mlp=.05, random_forest=.2625)


def five_models(recipe):
    result = small_models(recipe)
    result["random_forest"].set_params(model__n_estimators=8)
    return result


@pytest.fixture(scope="module")
def trained_pair():
    source = snapshot()
    features = source.features.copy()
    features["timestamp"] = pd.date_range("2026-01-01", periods=len(features), freq="5min", tz="UTC")
    features["close_time"] = features.timestamp + pd.Timedelta(minutes=5)
    features["interval"] = "5m"
    source = replace(source, interval="5m", features=features,
                     labels=build_labels(features, interval="5m"))
    recipe = settings(horizon_bars=1, split_mode="fractions", train_fraction=.7,
                      calibration_fraction=.15, assessment_fraction=.15,
                      **{name + "_weight": value for name, value in WEIGHTS.items()})
    with threadpool_limits(limits=1):
        raw = models.train_candidate(source, recipe, model_factory=five_models)
        shrunk = models.train_candidate(source, replace(recipe, **{FIELD: .9}), model_factory=five_models)
    return source, recipe, raw, shrunk


@pytest.mark.parametrize("value", [True, False, None, "0.9", 0, -1, 1.00001,
                                    float("inf"), float("nan")])
def test_invalid_shrinkage_fails_at_config_settings_bundle_and_prediction(value):
    bundle = dict(coin="BTC", interval="5m", horizon_bars=1, feature_revision="x",
                  feature_names=(), estimators={}, calibration={},
                  fitted_through_close_utc="", source_run_id="")
    for constructor, kwargs in [(ModelConfig, {}), (models.ModelSettings, {}),
                                (models.ModelBundle, bundle)]:
        with pytest.raises(ValueError, match=FIELD):
            constructor(**kwargs, **{FIELD: value})
    with pytest.raises(ValueError, match=FIELD):
        models._ensemble_probability({"model": np.array([.7])}, {"model": 1}, shrinkage=value)


def test_default_is_bitwise_identity_and_positive_factor_preserves_direction():
    probabilities = {"left": np.array([.1, .25, .5, .75, .9]),
                     "right": np.array([.13, .22, .5, .78, .87])}
    weights = {"left": .4, "right": .6}
    expected = np.average(np.column_stack(list(probabilities.values())), axis=1,
                          weights=list(weights.values()))
    raw = models._ensemble_probability(probabilities, weights)
    np.testing.assert_array_equal(raw, expected)
    np.testing.assert_array_equal(models._ensemble_probability(probabilities, weights, shrinkage=1), raw)
    for factor in (.1, .9):
        shrunk = models._ensemble_probability(probabilities, weights, shrinkage=factor)
        np.testing.assert_array_equal(shrunk, .5 + factor * (raw - .5))
        np.testing.assert_array_equal(np.sign(shrunk - .5), np.sign(raw - .5))
        assert np.all((shrunk > 0) & (shrunk < 1))
    assert ModelConfig().ensemble_probability_shrinkage == 1
    assert models.ModelSettings().ensemble_probability_shrinkage == 1


def test_shrinkage_preserves_all_five_members_and_only_transforms_ensemble(trained_pair):
    _, _, raw, shrunk = trained_pair
    for result in (raw, shrunk):
        assert set(result["bundle"].estimators) == set(WEIGHTS)
        assert result["bundle"].ensemble_weights == WEIGHTS
        assert result["report"]["ensemble_weights"] == WEIGHTS
    for name in WEIGHTS:
        np.testing.assert_array_equal(raw["assessment"][name + "_p_not_down"],
                                      shrunk["assessment"][name + "_p_not_down"])
    expected = .5 + .9 * (raw["assessment"].p_not_down.to_numpy() - .5)
    np.testing.assert_array_equal(shrunk["assessment"].p_not_down, expected)
    np.testing.assert_array_equal(shrunk["assessment"].p_down, 1 - expected)
    assert shrunk["assessment"][FIELD].eq(.9).all()
    assert shrunk["report"][FIELD] == .9
    assert shrunk["report"]["boundary_purged_rows"] == {"fit_to_calibration": 1, "calibration_to_assessment": 1}
    assert shrunk["report"]["refit_after_assessment"] is False


def test_assessment_and_deployed_predict_match_on_the_same_completed_rows(trained_pair):
    source, _, _, shrunk = trained_pair
    assessment = shrunk["assessment"]
    with threadpool_limits(limits=1):
        for row in assessment.iloc[[0, len(assessment) // 2, -1]].itertuples():
            selected = source.features.close_time <= row.close_time
            prefix = replace(source, features=source.features.loc[selected].copy(),
                             labels=source.labels.loc[selected].copy())
            forecast = models.predict_bundle(shrunk["bundle"], prefix)
            assert forecast[FIELD] == .9
            assert forecast["p_not_down"] == pytest.approx(row.p_not_down, abs=1e-15)
            assert forecast["p_down"] == pytest.approx(row.p_down, abs=1e-15)
            assert forecast["ensemble_weights"] == WEIGHTS


def test_joblib_legacy_missing_factor_uses_exact_unshrunk_prediction(trained_pair, tmp_path):
    source, _, _, shrunk = trained_pair
    path = tmp_path / "bundle.joblib"
    joblib.dump(shrunk["bundle"], path)
    loaded = joblib.load(path)
    with threadpool_limits(limits=1):
        forecast = models.predict_bundle(loaded, source)
        assert forecast[FIELD] == .9
        assert loaded.ensemble_probability_shrinkage == .9
        for invalid in (0, True, float("nan")):
            loaded.ensemble_probability_shrinkage = invalid
            with pytest.raises(ValueError, match=FIELD):
                models.predict_bundle(loaded, source)
        del loaded.ensemble_probability_shrinkage
        joblib.dump(loaded, path)
        legacy = joblib.load(path)
        assert FIELD not in vars(legacy)
        unshrunk = models.predict_bundle(legacy, source)
    expected = models._ensemble_probability(
        {name: np.array([p["p_not_down"]]) for name, p in unshrunk["per_model"].items()},
        WEIGHTS,
    )[0]
    assert unshrunk["p_not_down"] == expected
    assert unshrunk[FIELD] == 1.0
    assert set(legacy.estimators) == set(WEIGHTS)


def test_qualification_recomputed_from_transformed_assessment(trained_pair, monkeypatch):
    source, recipe, _, _ = trained_pair
    blocks = models._split_rows(models._training_rows(source, 1), recipe)
    labels = blocks["assessment"].y_not_down.to_numpy()
    raw_probability = np.where(labels == 1, .9, .1)
    # Deliberately overconfident test predictions: 40% have the wrong direction.
    wrong = np.arange(len(labels)) % 5 < 2
    raw_probability[wrong] = 1 - raw_probability[wrong]
    monkeypatch.setattr(models, "_calibrated_probability", lambda bundle, name, values: raw_probability.copy())
    with threadpool_limits(limits=1):
        raw = models.train_candidate(source, recipe, model_factory=logistic_only)
        shrunk = models.train_candidate(source, replace(recipe, **{FIELD: .1}), model_factory=logistic_only)
    assert raw["report"]["eligible"] is False
    expected = .5 + .1 * (raw_probability - .5)
    assert shrunk["report"]["metrics"]["ensemble"]["log_loss"] == pytest.approx(log_loss(labels, expected))
    assert shrunk["report"]["metrics"]["ensemble"]["brier_score"] == pytest.approx(brier_score_loss(labels, expected))
    metrics = shrunk["report"]["metrics"]
    qualifies = all(metrics["ensemble"][metric] <= metrics[baseline][metric] + 1e-9
                    for baseline in ("neutral_baseline", "prior_baseline")
                    for metric in ("log_loss", "brier_score"))
    assert shrunk["report"]["eligible"] is qualifies is True


def test_native_publication_binds_factor_and_truthful_role(trained_pair, tmp_path):
    source, recipe, _, shrunk = trained_pair
    recipe = replace(recipe, **{FIELD: .9})
    now = source.features.close_time.iloc[-1].timestamp() + 1
    record = artifacts.publish_candidate(tmp_path, source, recipe, shrunk, now=now)
    assert record["settings"][FIELD] == .9
    assert record["eligible"] == shrunk["report"]["eligible"]
    assert pd.read_parquet(record["assessment_path"])[FIELD].eq(.9).all()
    assert json.loads(open(record["report_path"], encoding="utf-8").read())[FIELD] == .9
    predictor = artifacts.load_predictor(tmp_path, source.coin, "5m", 1, now=now, max_age_seconds=60)
    with threadpool_limits(limits=1):
        forecast = models.predict_bundle(predictor["bundle"], source)
    stored = artifacts.record_prediction(tmp_path, source.coin, "5m", 1, forecast, predictor, now=now)
    assert stored["p_not_down"] == forecast["p_not_down"]
    assert stored["qualified"] == shrunk["report"]["eligible"]
    assert stored["role"] == ("active" if stored["qualified"] else "research_candidate")


def test_json_factor_is_part_of_recipe_and_runtime_retraining_trigger(tmp_path):
    path = tmp_path / "model.json"
    path.write_text(json.dumps({"version": 1, FIELD: .9}), encoding="utf-8")
    config = load_config(path)
    assert config.model_settings(4).ensemble_probability_shrinkage == .9
    assert json.loads(json.dumps(asdict(config.model_settings(4)))) == asdict(config.model_settings(4))
    worker = object.__new__(ModelRuntime)
    worker.config = config
    worker._stopping = worker._once = worker.force_train = False
    worker._attempted = set()
    worker.clock = lambda: 2_000_000_000.0
    source = SimpleNamespace(run_id="same", feature_revision="same")
    matching = dict(settings=asdict(config.model_settings(4)), feature_revision="same",
                    trained_at_utc=pd.Timestamp(worker.clock(), unit="s", tz="UTC").isoformat(),
                    source_run_id="same")
    slot = dict(candidate=matching, retry_at=0)
    assert worker._is_due(("BTC", "5m", 4), source, slot) is False
    for old_factor in (1.0, None):
        prior_settings = dict(matching["settings"])
        if old_factor is None:
            prior_settings.pop(FIELD)
        else:
            prior_settings[FIELD] = old_factor
        slot["candidate"] = {**matching, "settings": prior_settings}
        assert worker._is_due(("BTC", "5m", 4), source, slot) is True
    worker.config_path = path
    path.write_text(json.dumps({"version": 1, FIELD: .8}), encoding="utf-8")
    worker._refresh_universe()
    assert worker._config_error["type"] == "ValueError"
    assert "Restart the model runtime" in worker._config_error["message"]
