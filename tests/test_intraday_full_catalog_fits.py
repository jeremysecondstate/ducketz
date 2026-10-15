"""Real adapters on synthetic exact-target candles; no production data or calls.

Small estimator budgets keep this an adapter/calibration contract check, not
evidence of forecast accuracy, model selection advantage or trading profit.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import warnings

import numpy as np
import pytest
from threadpoolctl import threadpool_limits

from ml.intraday.model_selection import default_candidates
from ml.intraday.models import (
    UnsupportedModelError, fit_calibrated_estimator, probability_not_down,
    purged_chronological_split, registry_status,
)


def _small_spec(spec):
    parameters = dict(spec.parameters)
    if spec.family in {"random_forest", "extra_trees", "gradient_boosting", "adaboost", "xgboost", "lightgbm"}:
        parameters["n_estimators"] = 12
    elif spec.family == "catboost":
        parameters["iterations"] = 12
    elif spec.family == "hist_gradient_boosting":
        parameters["max_iter"] = 12
    elif spec.family in {"mlp", "passive_aggressive"}:
        parameters["max_iter"] = 40
    return replace(spec, parameters=parameters)


@pytest.fixture(scope="module")
def synthetic_causal_candles():
    rng = np.random.default_rng(20261014)
    rows = 360
    step = timedelta(minutes=15)
    returns = .0025*np.sin(np.arange(rows+3)*.23) + rng.normal(0, .001, rows+3)
    opens = 100*np.exp(np.concatenate(([0.], np.cumsum(returns[:-1]))))
    closes = opens*np.exp(returns)
    volume = rng.integers(1000, 10000, rows+3)
    spread = rng.uniform(.0002, .001, rows+3)
    highs = np.maximum(opens, closes)*(1+spread)
    lows = np.minimum(opens, closes)*(1-spread)
    # Every feature is observable by the end of its reference candle. The
    # target is the close of the third candle after that reference opening.
    X = np.column_stack((returns[:rows], np.concatenate(([0.], returns[:rows-1])),
                         np.log(volume[:rows]), (highs[:rows]-lows[:rows])/opens[:rows]))
    y = (closes[2:rows+2] >= opens[:rows]).astype(np.uint8)
    start = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
    references = tuple(start+number*step for number in range(rows))
    target_closes = tuple(reference+3*step for reference in references)
    available = tuple(target+timedelta(seconds=5) for target in target_closes)
    holdout = np.arange(300, rows)
    boundary = references[holdout[0]]
    pool = np.asarray([i for i in range(holdout[0]) if available[i] <= boundary])
    return X, y, pool, holdout, references, target_closes, available


def test_full_catalog_specs_cover_every_genuine_family():
    specs = default_candidates()
    implemented = {item["family"] for item in registry_status() if item["status"] != "adapter_not_implemented"}
    assert len(specs) == 21 and len(implemented) == 16
    assert {spec.family for spec in specs} == implemented
    assert all(spec.probability_calibration == "sigmoid" for spec in specs)


@pytest.mark.parametrize("spec", [pytest.param(_small_spec(spec), id=spec.name) for spec in default_candidates()])
def test_every_candidate_genuinely_fits_and_calibrates(spec, synthetic_causal_candles, record_property):
    X, y, pool, holdout, references, target_closes, available = synthetic_causal_candles
    train, calibration = purged_chronological_split(
        [references[i] for i in pool], [target_closes[i] for i in pool],
        fraction=spec.calibration_fraction, label_available_at=[available[i] for i in pool])
    train, calibration = pool[train], pool[calibration]
    assert max(available[i] for i in train) <= references[calibration[0]]
    assert max(available[i] for i in calibration) <= references[holdout[0]]
    assert set(np.unique(y[train])) == set(np.unique(y[calibration])) == {0, 1}
    fit = lambda: fit_calibrated_estimator(spec, X[pool], y[pool],
        reference_at=[references[i] for i in pool], target_close_at=[target_closes[i] for i in pool],
        label_available_at=[available[i] for i in pool], training_cutoff=references[holdout[0]])
    status = next(item for item in registry_status() if item["family"] == spec.family)
    record_property("family", spec.family)
    record_property("adapter_status", status["status"])
    if status["status"] == "dependency_unavailable":
        with pytest.raises(UnsupportedModelError, match="not installed; no substitute was fitted"):
            fit()
        pytest.skip(f"{spec.family}: genuine adapter dependency {status['dependency']} is unavailable")
    assert status["status"] == "available"
    with threadpool_limits(limits=1), warnings.catch_warnings(record=True) as observed_warnings:
        warnings.simplefilter("always")
        model, evidence = fit()
    probabilities = model.predict_proba(X[holdout])[:, 1]
    assert probabilities.shape == (len(holdout),)
    assert np.isfinite(probabilities).all()
    assert ((probabilities >= 0) & (probabilities <= 1)).all()
    np.testing.assert_allclose(model.predict_proba(X[holdout]).sum(axis=1), 1)
    assert model.sigmoid_.classes_.tolist() == [0, 1]
    assert model.classes_.tolist() == [0, 1]
    assert probability_not_down(model, X[holdout[:1]]) == pytest.approx(probabilities[0])
    assert evidence["calibration"]["method"] == "purged_chronological_sigmoid"
    assert evidence["calibration"]["base_rows"] == len(train)
    assert evidence["calibration"]["calibration_rows"] == len(calibration)
    assert evidence["calibration"]["base_latest_label_available_at"] <= evidence["calibration"]["first_calibration_reference"]
    assert model.base.named_steps["impute"].n_features_in_ == X.shape[1]
    if "scale" in model.base.named_steps:
        np.testing.assert_allclose(model.base.named_steps["scale"].mean_, X[train].mean(axis=0))
    record_property("fit_warning_count", len(observed_warnings))
