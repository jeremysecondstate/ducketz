from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.exceptions import ConvergenceWarning
from threadpoolctl import threadpool_limits

from ml import hyperliquid_models as models
from ml.hyperliquid_data_pipeline import build_labels
from technicals.hyperliquid_features import FEATURE_SCHEMA_VERSION


@pytest.fixture(autouse=True)
def bounded_native_threads():
    with threadpool_limits(limits=1):
        yield


def snapshot(count=360):
    stamps = pd.date_range("2026-01-01", periods=count, freq="15min", tz="UTC")
    i = np.arange(count)
    close = 100 + 3 * np.sin(i / 7) + 0.001 * i
    raw = pd.DataFrame({
        "timestamp": stamps, "close_time": stamps + pd.Timedelta(minutes=15),
        "symbol": "BTC", "interval": "15m", "open": close + 0.1,
        "high": close + 0.3, "low": close - 0.4, "close": close,
        "volume": 100 + i % 13, "trade_count": 30 + i,
    })
    rng = np.random.default_rng(20)
    values = rng.normal(size=(count, len(models.FEATURE_NAMES)))
    values[:, 0] = np.cos(i / 7)
    featured = pd.concat([raw, pd.DataFrame(values, columns=models.FEATURE_NAMES)], axis=1)
    return models.MarketSnapshot(
        coin="BTC", interval="15m", run_id="20260105T000000Z-1234abcd",
        feature_revision=FEATURE_SCHEMA_VERSION, feature_names=models.FEATURE_NAMES,
        features=featured, labels=build_labels(raw, interval="15m"), run_dir=Path("unused"),
    )


def settings(**overrides):
    return models.ModelSettings(**{
        "min_train_rows": 120, "calibration_rows": 50, "assessment_rows": 60,
        "model_threads": 1, **overrides,
    })


def small_models(config):
    estimators = models._make_estimators(config)
    estimators["extra_trees"].set_params(model__n_estimators=8)
    estimators["hist_gradient_boosting"].set_params(model__max_iter=8)
    estimators["mlp"].set_params(model__max_iter=5, model__batch_size=32)
    return estimators


def logistic_only(config):
    return {"logistic": models._make_estimators(config)["logistic"]}


def write_snapshot(tmp_path, source=None):
    source = source or snapshot()
    dataset = tmp_path / source.coin / source.interval
    run_dir = dataset / "runs" / source.run_id
    run_dir.mkdir(parents=True)
    source.features.to_parquet(run_dir / "features.parquet", index=False)
    source.labels.to_parquet(run_dir / "labels.parquet", index=False)
    (run_dir / "feature_catalog.json").write_text(json.dumps({
        "feature_revision": source.feature_revision,
        "features": [{"name": name} for name in source.feature_names],
    }))
    (run_dir / "summary.json").write_text(json.dumps({
        "run_id": source.run_id, "coin": source.coin, "interval": source.interval,
        "feature_revision": source.feature_revision, "rows": len(source.features),
    }))
    (dataset / "latest.json").write_text(json.dumps({
        "run_id": source.run_id, "files": {"features": "https://untrusted.invalid/data.parquet"},
        "summary": "C:/not-used/summary.json",
    }))
    return dataset, run_dir


def test_load_snapshot_ignores_external_pointer_paths_and_reads_local_schema(tmp_path):
    _, run_dir = write_snapshot(tmp_path)
    loaded = models.load_snapshot(tmp_path, "btc", "15m")
    assert loaded.coin == "BTC"
    assert loaded.run_dir == run_dir
    assert loaded.feature_names == models.FEATURE_NAMES
    assert len(loaded.features) == 360


@pytest.mark.parametrize("run_id", ["../../outside", "C:/other", None, "20260105T000000Z-1234abcd/child"])
def test_load_snapshot_rejects_untrusted_run_id(tmp_path, run_id):
    dataset, _ = write_snapshot(tmp_path)
    (dataset / "latest.json").write_text(json.dumps({"run_id": run_id}))
    with pytest.raises(ValueError, match="run identifier"):
        models.load_snapshot(tmp_path, "BTC", "15m")


def test_load_resolves_latest_only_once(tmp_path, monkeypatch):
    dataset, _ = write_snapshot(tmp_path)
    original = pd.read_parquet
    calls = []

    def reading(path, *args, **kwargs):
        calls.append(Path(path))
        (dataset / "latest.json").write_text(json.dumps({"run_id": "20260106T000000Z-ffffffff"}))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", reading)
    loaded = models.load_snapshot(tmp_path, "BTC", "15m")
    assert loaded.run_id == "20260105T000000Z-1234abcd"
    assert {path.parent.name for path in calls} == {loaded.run_id}


@pytest.mark.parametrize("mutation", ["catalog_order", "feature_label_leak", "feature_order", "label_alignment", "market", "revision"])
def test_load_rejects_schema_and_alignment_errors(tmp_path, mutation):
    source = snapshot()
    dataset, run_dir = write_snapshot(tmp_path, source)
    if mutation in ("catalog_order", "revision"):
        catalog = json.loads((run_dir / "feature_catalog.json").read_text())
        if mutation == "catalog_order":
            catalog["features"].reverse()
        else:
            catalog["feature_revision"] = "another_revision"
        (run_dir / "feature_catalog.json").write_text(json.dumps(catalog))
    elif mutation == "label_alignment":
        labels = source.labels.iloc[::-1]
        labels.to_parquet(run_dir / "labels.parquet", index=False)
    else:
        features = source.features.copy()
        if mutation == "feature_label_leak":
            features["future_return_4bar"] = source.labels["future_return_4bar"]
        elif mutation == "feature_order":
            features = features.loc[:, [*features.columns[:10], *features.columns[10:][::-1]]]
        else:
            features["symbol"] = "ETH"
        features.to_parquet(run_dir / "features.parquet", index=False)
    with pytest.raises(ValueError):
        models.load_snapshot(tmp_path, "BTC", "15m")


def test_training_purges_label_horizons_and_preserves_unknown_tail():
    source = snapshot()
    result = models.train_candidate(source, settings(), model_factory=small_models)
    report, bundle, assessment = result["report"], result["bundle"], result["assessment"]
    splits = report["splits"]
    assert pd.Timestamp(splits["fit"]["last_label_end_utc"]) < pd.Timestamp(splits["calibration"]["first_decision_close_utc"])
    assert pd.Timestamp(splits["calibration"]["last_label_end_utc"]) < pd.Timestamp(splits["assessment"]["first_decision_close_utc"])
    assert splits["calibration"]["rows"] == 50
    assert splits["assessment"]["rows"] == 60
    assert report["purged_rows"] == 8
    assert report["max_train_rows"] is None
    assert report["fit_rows_before_window_cap"] == splits["fit"]["rows"]
    assert report["training_window_omitted_rows"] == 0
    assert report["unknown_or_featureless_rows"] == 4
    assert assessment["label_end_time"].max() == source.features["close_time"].iloc[-1]
    assert source.labels["future_return_4bar"].iloc[-4:].isna().all()
    assert bundle.fitted_through_close_utc == splits["fit"]["last_decision_close_utc"]
    assert report["refit_after_assessment"] is False
    assert set(bundle.estimators) == {"logistic", "extra_trees", "hist_gradient_boosting", "mlp"}
    assert np.allclose(assessment["p_not_down"] + assessment["p_down"], 1)
    expected = assessment[[f"{name}_p_not_down" for name in bundle.estimators]].mean(axis=1)
    assert np.allclose(assessment["p_not_down"], expected)
    # The small MLP deliberately hits its iteration cap; this stays inspectable.
    assert any("ConvergenceWarning" in warning for warning in report["warnings"])
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("cap", [None, 120, 160, 1000])
def test_training_cap_keeps_latest_purged_fit_rows_and_same_later_blocks(cap):
    frame = models._training_rows(snapshot(), 4)
    full = models._split_rows(frame, settings())
    capped = models._split_rows(frame, settings(max_train_rows=cap))
    expected_fit = full["fit"] if cap is None else full["fit"].iloc[-cap:]
    pd.testing.assert_frame_equal(capped["fit"], expected_fit)
    for name in ("calibration", "assessment"):
        pd.testing.assert_frame_equal(capped[name], full[name])
    assert capped["fit"].label_end_time.max() < capped["calibration"].close_time.min()
    assert capped["calibration"].label_end_time.max() < capped["assessment"].close_time.min()


def test_capped_fit_report_separates_old_rows_from_horizon_purging():
    source = snapshot()
    frame = models._training_rows(source, 4)
    full = models._split_rows(frame, settings())
    result = models.train_candidate(source, settings(max_train_rows=160), model_factory=logistic_only)
    report = result["report"]
    assert report["splits"]["fit"]["rows"] == 160
    assert report["max_train_rows"] == 160
    assert report["fit_rows_before_window_cap"] == len(full["fit"])
    assert report["training_window_omitted_rows"] == len(full["fit"]) - 160
    assert report["purged_rows"] == 8
    assert report["mature_usable_rows"] == (
        sum(block["rows"] for block in report["splits"].values())
        + report["purged_rows"] + report["training_window_omitted_rows"])
    for name in ("calibration", "assessment"):
        assert report["splits"][name] == models._block_summary(full[name])
    latest_fit = full["fit"].iloc[-160:]
    fitted = result["bundle"].estimators["logistic"]
    np.testing.assert_allclose(fitted["imputer"].statistics_, latest_fit[list(models.FEATURE_NAMES)].median())
    np.testing.assert_allclose(fitted["scaler"].mean_, latest_fit[list(models.FEATURE_NAMES)].mean())
    assert report["splits"]["fit"]["first_decision_close_utc"] == latest_fit.close_time.iloc[0].isoformat()
    json.dumps(report, allow_nan=False)


def test_training_cap_rechecks_classes_in_retained_rows():
    frame = models._training_rows(snapshot(), 4)
    original_fit = models._split_rows(frame, settings())["fit"]
    frame.loc[original_fit.index[-120:], "y_not_down"] = 1
    assert models._split_rows(frame, settings())["fit"].y_not_down.nunique() == 2
    with pytest.raises(ValueError, match="both down and not-down"):
        models._split_rows(frame, settings(max_train_rows=120))


@pytest.mark.parametrize("cap", [True, False, 0, -1, 119, 120.0, "120"])
def test_training_cap_validates_type_and_minimum(cap):
    with pytest.raises(ValueError, match="max_train_rows"):
        settings(max_train_rows=cap)


def test_training_cap_does_not_bypass_minimum_mature_fit_history():
    with pytest.raises(ValueError, match="training rows"):
        models.train_candidate(snapshot(180), settings(max_train_rows=120), model_factory=logistic_only)


def test_fit_preprocessing_does_not_observe_later_features():
    source = snapshot()
    first = models.train_candidate(source, settings(), model_factory=logistic_only)
    cutoff = pd.Timestamp(first["bundle"].fitted_through_close_utc)
    changed = source.features.copy()
    changed.loc[changed["close_time"] > cutoff, list(models.FEATURE_NAMES)] += 100_000
    second = models.train_candidate(replace(source, features=changed), settings(), model_factory=logistic_only)
    original_pipe = first["bundle"].estimators["logistic"]
    changed_pipe = second["bundle"].estimators["logistic"]
    fit_values = source.features.loc[source.features.close_time <= cutoff, list(models.FEATURE_NAMES)]
    assert np.allclose(original_pipe["imputer"].statistics_, fit_values.median())
    assert np.allclose(original_pipe["scaler"].mean_, fit_values.mean())
    np.testing.assert_array_equal(original_pipe["scaler"].mean_, changed_pipe["scaler"].mean_)
    np.testing.assert_array_equal(original_pipe["model"].coef_, changed_pipe["model"].coef_)


def test_assessment_labels_do_not_fit_models_or_calibrators():
    source = snapshot()
    first = models.train_candidate(source, settings(), model_factory=logistic_only)
    cutoff = pd.Timestamp(first["report"]["splits"]["calibration"]["last_label_end_utc"])
    changed = source.features.copy()
    # Outcomes beyond the calibration label cutoff cannot affect fitting.
    changed.loc[changed.close_time > cutoff, "close"] += 4
    labels = build_labels(changed, interval="15m")
    second = models.train_candidate(replace(source, features=changed, labels=labels), settings(), model_factory=logistic_only)
    for name in first["bundle"].estimators:
        np.testing.assert_array_equal(first["bundle"].estimators[name]["model"].coef_, second["bundle"].estimators[name]["model"].coef_)
        np.testing.assert_array_equal(first["bundle"].calibration[name].coef_, second["bundle"].calibration[name].coef_)


def test_not_down_includes_exact_ties_and_excludes_unknown_outcomes():
    source = snapshot()
    frame = source.features.copy()
    frame.loc[4, "close"] = frame.loc[0, "close"]
    labels = build_labels(frame, interval="15m")
    rows = models._training_rows(replace(source, features=frame, labels=labels), 4)
    assert labels.loc[0, "target_up_4bar"] == 0
    assert rows.loc[0, "y_not_down"] == 1
    assert len(rows) == len(frame) - 4


def test_exact_time_label_validation_handles_gaps_and_bad_future_values():
    source = snapshot()
    frame = source.features.drop(index=[70, 71, 72]).reset_index(drop=True)
    labels = build_labels(frame, interval="15m")
    changed = replace(source, features=frame, labels=labels)
    result = models.train_candidate(changed, settings(), model_factory=logistic_only)
    assert result["report"]["unknown_or_featureless_rows"] == 7
    labels = labels.copy()
    labels.loc[67, "future_return_4bar"] = 0.3
    with pytest.raises(ValueError, match="exact-time"):
        models.train_candidate(replace(changed, labels=labels), settings(), model_factory=logistic_only)


@pytest.mark.parametrize("error", ["future_known", "wrong_return"])
def test_bad_labels_cannot_become_training_targets(error):
    source = snapshot()
    labels = source.labels.copy()
    labels.loc[len(labels) - 1 if error == "future_known" else 0, "future_return_4bar"] = 0.25
    with pytest.raises(ValueError, match="outcome|exact-time"):
        models.train_candidate(replace(source, labels=labels), settings(), model_factory=logistic_only)


def test_prediction_uses_latest_incomplete_feature_row_and_saved_imputer():
    source = snapshot()
    trained = models.train_candidate(source, settings(), model_factory=logistic_only)
    latest = source.features.copy()
    latest.loc[len(latest) - 1, models.FEATURE_NAMES[0]] = np.nan
    predicted = models.predict_bundle(trained["bundle"], replace(source, features=latest))
    assert predicted["decision_close_utc"] == latest.close_time.iloc[-1].isoformat()
    assert predicted["decision_timestamp_utc"] == latest.timestamp.iloc[-1].isoformat()
    assert predicted["decision_price"] == latest.close.iloc[-1]
    assert predicted["missing_features_imputed"] == [models.FEATURE_NAMES[0]]
    assert predicted["target_close_utc"] == (latest.close_time.iloc[-1] + pd.Timedelta(hours=1)).isoformat()
    assert predicted["p_not_down"] + predicted["p_down"] == pytest.approx(1)
    assert predicted["per_model"]["logistic"]["p_not_down"] == predicted["p_not_down"]
    assert predicted["data_run_id"] == source.run_id
    latest.loc[len(latest) - 1, list(models.FEATURE_NAMES)] = np.nan
    with pytest.raises(ValueError, match="older row is not substituted"):
        models.predict_bundle(trained["bundle"], replace(source, features=latest))


def test_predictions_accept_new_data_version_but_reject_market_schema_and_time_mismatch():
    source = snapshot()
    bundle = models.train_candidate(source, settings(), model_factory=logistic_only)["bundle"]
    new_data = replace(source, run_id="20260105T001500Z-ff123456")
    assert models.predict_bundle(bundle, new_data)["data_run_id"] == new_data.run_id
    for bad in (
        replace(bundle, coin="ETH"),
        replace(bundle, interval="1h"),
        replace(bundle, feature_names=tuple(reversed(bundle.feature_names))),
        replace(bundle, feature_revision="obsolete"),
        replace(bundle, horizon_bars=0),
        replace(bundle, fitted_through_close_utc="2030-01-01T00:00:00+00:00"),
        replace(bundle, calibration={}),
    ):
        with pytest.raises(ValueError):
            models.predict_bundle(bad, source)


class ReversedClasses(ClassifierMixin, BaseEstimator):
    classes_ = np.array([1, 0])

    def predict_proba(self, values):
        return np.tile([0.8, 0.2], (len(values), 1))


def test_probability_extraction_uses_actual_class_order():
    assert models._positive_probability(ReversedClasses(), np.zeros((3, 2))).tolist() == [0.8] * 3


def test_single_class_calibration_records_identity_and_auc_can_be_null():
    source = snapshot()
    first = models.train_candidate(source, settings(), model_factory=logistic_only)
    start = pd.Timestamp(first["report"]["splits"]["calibration"]["first_decision_close_utc"])
    frame = source.features.copy()
    selected = frame.close_time >= start
    frame.loc[selected, "close"] = 105 + np.arange(selected.sum())
    altered = replace(source, features=frame, labels=build_labels(frame, interval="15m"))
    result = models.train_candidate(altered, settings(), model_factory=logistic_only)
    assert result["bundle"].calibration["logistic"] is None
    assert result["report"]["calibration_methods"]["logistic"] == "identity_single_class_calibration"
    assert result["report"]["metrics"]["ensemble"]["roc_auc"] is None
    assert any("one class" in warning for warning in result["report"]["warnings"])


def test_eligibility_compares_both_metrics_to_both_baselines():
    result = models.train_candidate(snapshot(), settings(), model_factory=logistic_only)
    metrics = result["report"]["metrics"]
    expected = all(metrics["ensemble"][metric] <= metrics[baseline][metric] + 1e-9
                   for metric in ("log_loss", "brier_score")
                   for baseline in ("prior_baseline", "neutral_baseline"))
    assert result["report"]["eligibility"] is expected
    assert isinstance(result["report"]["eligible"], bool)
    assert result["report"]["eligible"] is expected
    fit = result["report"]["splits"]["fit"]
    calibration = result["report"]["splits"]["calibration"]
    prior = (fit["rows"] * fit["not_down_fraction"] + calibration["rows"] * calibration["not_down_fraction"]) / (fit["rows"] + calibration["rows"])
    assert result["report"]["prior_baseline_probability"] == pytest.approx(prior)


@pytest.mark.parametrize("change", [{"horizon_bars": 0}, {"calibration_rows": 1}, {"assessment_rows": True}, {"min_train_rows": -1}, {"random_state": -1}, {"model_threads": 0}])
def test_settings_reject_invalid_counts(change):
    with pytest.raises(ValueError):
        settings(**change)


def test_insufficient_mature_history_reports_clear_error():
    with pytest.raises(ValueError, match="training rows"):
        models.train_candidate(snapshot(180), settings(), model_factory=logistic_only)


def test_unknown_horizon_and_single_class_training_fail_clearly():
    source = snapshot()
    with pytest.raises(ValueError, match="future_return_2bar"):
        models.train_candidate(source, settings(horizon_bars=2), model_factory=logistic_only)
    frame = source.features.copy()
    frame["close"] = np.arange(len(frame)) + 100.0
    with pytest.raises(ValueError, match="both down and not-down"):
        models.train_candidate(replace(source, features=frame, labels=build_labels(frame, interval="15m")), settings(), model_factory=logistic_only)
