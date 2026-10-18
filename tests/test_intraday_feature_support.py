"""Offline fitting fixtures only; no market or provider calls."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import joblib
import numpy as np
import pandas as pd
import pytest

from ml.intraday.feature_support import MarketVariationSupport, attach_market_support, VERSION
from ml.intraday.models import ModelSpec, build_estimator, fit_calibrated_estimator, fit_ensemble, freeze_model_spec
from ml.intraday.prediction import ContractError


def fixture(rows=100):
    references = [datetime(2026, 1, 1, tzinfo=timezone.utc)+timedelta(hours=i) for i in range(rows)]
    targets = [value+timedelta(minutes=45) for value in references]
    X = np.column_stack([np.arange(rows) % 7, np.zeros(rows), np.ones(rows)])
    return X, np.arange(rows) % 2, dict(reference_at=references, target_close_at=targets,
        label_available_at=targets, training_cutoff=targets[-1])


@pytest.mark.parametrize("prefix", ["market_", "ctx_"])
def test_training_constants_and_missing_indicators_stay_inert_without_changing_raw(prefix):
    names = ("price", prefix+"value", prefix+"value_missing", prefix+"varying")
    support = MarketVariationSupport(names).fit([[1, 0, 1, 3], [1, 0, 1, 4]])
    raw = np.array([[7, 1e15, 0, 9]], dtype=float)
    assert support.transform(raw).tolist() == [[7, 0, 1, 9]]
    assert raw.tolist() == [[7, 1e15, 0, 9]]
    assert support.evidence()["constant_columns"] == list(names[1:3])
    assert support.evidence()["version"] == VERSION
    assert len(support.evidence()["source_file_sha256"]) == 64


def test_observed_constant_values_are_model_preprocessing_not_market_fills():
    support = MarketVariationSupport(("price", "ctx_value", "ctx_source_available")).fit([[1, 55, 1], [2, 55, 1]])
    raw = [[3, 0, 0]]
    assert support.transform(raw).tolist() == [[3, 55, 1]]
    assert raw == [[3, 0, 0]] and support.evidence()["raw_snapshot_preserved"]


def test_price_only_recipes_keep_original_pipeline():
    base = build_estimator(ModelSpec("test", family="logistic"))
    assert attach_market_support(base, ("price", "volume")) is base
    assert "market_support" not in base.named_steps


@pytest.mark.parametrize("family", ["mlp", "gaussian_nb", "random_forest"])
def test_fitted_models_cannot_react_to_context_never_varied_in_base_training(family):
    X, y, clocks = fixture(80)
    params = {"hidden_layer_sizes": (4,), "max_iter": 60} if family == "mlp" else {"n_estimators": 4} if family == "random_forest" else {}
    spec = freeze_model_spec(ModelSpec("test", family=family, parameters=params))
    frozen = asdict(spec)
    estimator, evidence = fit_calibrated_estimator(spec, X, y, feature_names=("price", "ctx_value", "ctx_value_missing"), **clocks)
    assert np.array_equal(estimator.predict_proba([[2, 0, 1]]), estimator.predict_proba([[2, 1e15, 0]]))
    assert evidence["market_feature_support"]["base_training_rows"] == 80
    assert asdict(spec) == frozen


def test_calibration_and_validation_cannot_supply_base_context_variation():
    X, y, clocks = fixture()
    X[80:, 1] = np.arange(20)+1
    X[80:, 2] = 0
    names = ("price", "ctx_value", "ctx_value_missing")
    model, evidence = fit_calibrated_estimator(ModelSpec("test", family="logistic", probability_calibration="sigmoid"),
        X, y, feature_names=names, **clocks)
    guard = model.base.named_steps["market_support"]
    assert guard.training_rows_ == 80
    assert guard.evidence()["constant_columns"] == list(names[1:])
    assert np.array_equal(model.predict_proba([[2, 0, 1]]), model.predict_proba([[2, 999999, 0]]))


def test_newest_training_window_does_not_learn_variation_in_excluded_old_rows():
    X, y, clocks = fixture()
    X[:20, 1] = np.arange(20)
    names = ("price", "ctx_value", "ctx_value_missing")
    model, evidence = fit_calibrated_estimator(ModelSpec("test", family="logistic", training_max_rows=80),
        X, y, feature_names=names, **clocks)
    assert evidence["market_feature_support"]["base_training_rows"] == 80
    assert evidence["market_feature_support"]["constant_columns"] == list(names[1:])


def test_next_fresh_fit_admits_new_base_training_variation():
    support = MarketVariationSupport(("price", "ctx_value", "ctx_value_missing")).fit([[1, 0, 1], [2, 5, 0], [3, 8, 0]])
    assert support.indices_.size == 0
    assert support.transform([[4, 13, 0]]).tolist() == [[4, 13, 0]]


def test_imputation_and_frozen_constructor_contract_preserved():
    X, y, clocks = fixture()
    X[:, 1] = np.nan
    spec = freeze_model_spec(ModelSpec("test", family="logistic"))
    original = build_estimator(spec)
    guarded, evidence = fit_calibrated_estimator(spec, pd.DataFrame(X, columns=("price", "ctx_value", "ctx_value_missing")), y, **clocks)
    for name in ("impute", "scale", "model"):
        actual, expected = guarded.named_steps[name].get_params(), original.named_steps[name].get_params()
        assert actual.keys() == expected.keys()
        for key in actual:
            assert actual[key] == expected[key] or (isinstance(actual[key], float) and np.isnan(actual[key]) and np.isnan(expected[key]))
    assert evidence["market_feature_support"]["constant_values"] == [0, 1]


def test_ensemble_artifact_serializes_support_and_raw_training_binding(tmp_path):
    X, y, clocks = fixture()
    raw = X.copy()
    result = fit_ensemble(X, y, [[2, 1e15, 0]], config=[ModelSpec("test", family="logistic")],
        feature_names=("price", "ctx_value", "ctx_value_missing"), artifact_dir=tmp_path, run_id="support-test", **clocks)
    assert result["complete"]
    output = result["outputs"][0]
    saved = joblib.load(output["artifact_path"])
    assert saved["metadata"] == output["metadata"]
    assert saved["metadata"]["market_feature_support"]["version"] == VERSION
    model = saved["estimator"]
    assert np.array_equal(model.predict_proba([[2, 0, 1]]), model.predict_proba([[2, 1e15, 0]]))
    assert np.array_equal(X, raw)


def test_selection_fold_threads_names_and_validation_never_teaches_support():
    from ml.intraday.model_selection import SelectionConfig, _fit_predict
    X, y, clocks = fixture()
    X[80:, 1] = np.arange(20)+1
    X[80:, 2] = 0
    data = (X, y, clocks["reference_at"], clocks["target_close_at"], clocks["label_available_at"], ["fixture"]*100, np.arange(100), clocks["training_cutoff"])
    partition = (np.arange(80), np.arange(80, 100), {"training_cutoff": clocks["reference_at"][80].isoformat()})
    config = SelectionConfig(min_training_rows=4, min_training_sessions=1, min_class_count=1)
    _, evidence = _fit_predict(ModelSpec("test", family="logistic"), partition, data, config, float("inf"),
        feature_names=("price", "ctx_value", "ctx_value_missing"))
    support = evidence["fit_evidence"]["market_feature_support"]
    assert support["base_training_rows"] == 80 and support["constant_columns"] == ["ctx_value", "ctx_value_missing"]


def test_mismatched_names_or_duplicate_attachment_fail_explicitly():
    with pytest.raises(ContractError, match="matching"):
        MarketVariationSupport(("ctx_value",)).fit([[1, 0], [2, 0]])
    base = attach_market_support(build_estimator(ModelSpec("test", family="logistic")), ("price", "ctx_value"))
    with pytest.raises(ContractError, match="established"):
        attach_market_support(base, ("price", "ctx_value"))
