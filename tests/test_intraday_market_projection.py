"""Real estimator checks for an explicitly selected local preprocessing policy."""
from dataclasses import asdict, replace

import joblib
import numpy as np
import pytest

from ml.intraday.feature_support import MarketVariationSupport, MarketVariationProjection, PROJECTION_POLICY
from ml.intraday.models import ModelSpec, fit_calibrated_estimator, freeze_model_spec, with_market_feature_projection
from ml.intraday.prediction import ContractError


def test_unsupported_columns_do_not_change_forest_feature_sampling():
    random = np.random.default_rng(24)
    X = random.normal(size=(200, 16))
    y = ((X[:, 0] + X[:, 1] * X[:, 2]) > 0).astype(int)
    expanded = np.column_stack([X, np.zeros((len(X), 200))])
    names = tuple(f"base_{i}" for i in range(16)) + tuple(f"ctx_missing_{i}" for i in range(200))
    spec = freeze_model_spec(ModelSpec("forest", family="random_forest", parameters={"n_estimators": 16, "max_depth": 3}))
    original = asdict(spec)
    baseline, _ = fit_calibrated_estimator(spec, X, y, feature_names=names[:16])
    legacy, legacy_evidence = fit_calibrated_estimator(spec, expanded, y, feature_names=names)
    projected, evidence = fit_calibrated_estimator(with_market_feature_projection(spec), expanded, y, feature_names=names)
    assert baseline.named_steps["model"].n_features_in_ == projected.named_steps["model"].n_features_in_ == 16
    assert legacy.named_steps["model"].n_features_in_ == 216
    assert legacy.named_steps["model"].estimators_[0].max_features_ == 14
    assert projected.named_steps["model"].estimators_[0].max_features_ == 4
    assert np.array_equal(projected.predict_proba(expanded), baseline.predict_proba(X))
    assert evidence["market_feature_support"]["projection_policy"] == PROJECTION_POLICY
    assert "projection_policy" not in legacy_evidence["market_feature_support"]
    assert asdict(spec) == original


def test_projection_cannot_learn_from_later_rows_and_survives_reload(tmp_path):
    model = MarketVariationProjection(("price", "ctx_absent", "ctx_varying"))
    model.fit([[1, 0, 2], [2, 0, 3]])
    assert model.transform([[3, 999, 4]]).tolist() == [[3, 4]]
    path = tmp_path / "projected.joblib"
    joblib.dump(model, path)
    assert joblib.load(path).transform([[3, 999, 4]]).tolist() == [[3, 4]]
    legacy = MarketVariationSupport(("price", "ctx_absent")).fit([[1, 0], [2, 0]])
    joblib.dump(legacy, path)
    assert joblib.load(path).transform([[3, 999]]).tolist() == [[3, 0]]


def test_unknown_frozen_projection_is_rejected():
    spec = freeze_model_spec(ModelSpec("logistic"))
    with pytest.raises(ContractError, match="local review"):
        freeze_model_spec(replace(spec, runtime_contract={**spec.runtime_contract, "market_feature_projection": "unreviewed"}))
