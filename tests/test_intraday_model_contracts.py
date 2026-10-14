"""Cross-producer model API checks using generated, offline observations."""
from datetime import datetime, timedelta, timezone

import joblib
import numpy as np
import pytest

from ml.intraday.models import (
    ModelSpec, configured_models, fit_calibrated_estimator, fit_ensemble,
    probability_not_down, purged_chronological_split,
)
from ml.intraday.prediction import ContractError

REF = datetime(2026, 10, 8, 17, tzinfo=timezone.utc)


def inputs(n=120):
    X = np.random.default_rng(123).normal(size=(n, 2))
    y = np.arange(n) % 2
    refs = [REF + i * timedelta(minutes=15) for i in range(n)]
    targets = [v + timedelta(minutes=45) for v in refs]
    available = [v + timedelta(seconds=10) for v in targets]
    return X, y, dict(reference_at=refs, target_close_at=targets,
                      label_available_at=available, training_cutoff=available[-1])


@pytest.mark.parametrize("calibration", [None, "none"])
def test_both_published_uncalibrated_configurations_retain_default_behavior(calibration):
    X, y, kwargs = inputs()
    spec = configured_models([dict(name="compatible", family="logistic",
                                   probability_calibration=calibration)])[0]
    actual, evidence = fit_calibrated_estimator(spec, X, y, **kwargs)
    expected, _ = fit_calibrated_estimator(ModelSpec("default", family="logistic"), X, y, **kwargs)
    assert probability_not_down(actual, X[-1:]) == pytest.approx(probability_not_down(expected, X[-1:]))
    assert evidence["calibration"] is None


@pytest.mark.parametrize("family", ["logistic", "ridge", "passive_aggressive"])
def test_calibration_metadata_preserves_both_published_receipt_keys(family):
    X, y, kwargs = inputs()
    result = fit_ensemble(X, y, X[-1:], config=[ModelSpec("compatible", family=family,
                          probability_calibration="sigmoid")], **kwargs)
    assert result["complete"], result
    calibration = result["outputs"][0]["metadata"]["calibration"]
    assert calibration["base_latest_label_available"] == calibration["base_latest_label_available_at"]
    assert calibration["base_latest_label_available"] <= calibration["first_calibration_reference"]


@pytest.mark.parametrize("calibration", ["none", "sigmoid"])
@pytest.mark.parametrize("fault", ["unordered", "target_horizon", "early_receipt"])
def test_direct_fitter_validates_complete_clock_inputs_before_row_window(calibration, fault):
    X, y, kwargs = inputs()
    if fault == "unordered":
        for key in ("reference_at", "target_close_at", "label_available_at"):
            kwargs[key][0], kwargs[key][1] = kwargs[key][1], kwargs[key][0]
    elif fault == "target_horizon":
        kwargs["target_close_at"][0] += timedelta(seconds=1)
    else:
        kwargs["label_available_at"][0] = kwargs["target_close_at"][0] - timedelta(seconds=1)
    spec = ModelSpec("bounded", family="logistic", probability_calibration=calibration, training_max_rows=80)
    with pytest.raises(ContractError):
        fit_calibrated_estimator(spec, X, y, **kwargs)


def test_direct_fitter_allows_late_nonmonotonic_receipts_that_are_observable():
    X, y, kwargs = inputs()
    kwargs["label_available_at"][0] = kwargs["label_available_at"][-1]
    spec = ModelSpec("late_receipt", family="logistic", probability_calibration="sigmoid")
    fitted, evidence = fit_calibrated_estimator(spec, X, y, **kwargs)
    train, _ = purged_chronological_split(kwargs["reference_at"], kwargs["target_close_at"],
                                         label_available_at=kwargs["label_available_at"])
    assert 0 not in train
    assert evidence["calibration"]["base_rows"] == len(train)
    assert 0 < probability_not_down(fitted, X[-1:]) < 1


def test_selected_calibration_fits_preprocessing_only_before_purged_boundary(tmp_path):
    # Atlas PR128 regression retained without its unrelated runtime imports.
    rng = np.random.default_rng(123)
    X = rng.normal(size=(120, 2)); y = np.arange(120) % 2
    references = [REF + i * timedelta(minutes=15) for i in range(120)]
    targets = [v + timedelta(minutes=45) for v in references]
    train, cal = purged_chronological_split(references, targets)
    X[cal, 0] += 200
    result = fit_ensemble(X, y, [[10., 0.]], config=[ModelSpec("smooth", family="gaussian_nb",
        probability_calibration="sigmoid")], training_cutoff=targets[-1],
        label_available_at=targets, reference_at=references, target_close_at=targets,
        artifact_dir=tmp_path, run_id="calibration-check")
    assert result["complete"], result
    output = result["outputs"][0]
    fitted = joblib.load(output["artifact_path"])["estimator"]
    assert fitted.base.named_steps["scale"].mean_[0] == pytest.approx(X[train, 0].mean())
    assert output["metadata"]["calibration"]["base_latest_target"] <= output["metadata"]["calibration"]["first_calibration_reference"]
    assert 0 < result["probability_not_down"] < 1


def test_calibrated_knn_caps_neighbors_to_base_fit_not_all_rows():
    # Atlas PR128 regression retained without its unrelated runtime imports.
    X = np.arange(80).reshape(-1, 1); y = np.arange(80) % 2
    refs = [REF + i * timedelta(minutes=15) for i in range(80)]
    targets = [v + timedelta(minutes=45) for v in refs]
    result = fit_ensemble(X, y, [[81]], config=[ModelSpec("knn_cal", family="knn",
        parameters={"n_neighbors": 500}, probability_calibration="sigmoid")],
        training_cutoff=targets[-1], label_available_at=targets, reference_at=refs, target_close_at=targets)
    assert result["complete"], result
    metadata = result["outputs"][0]["metadata"]
    assert metadata["effective_parameters"]["n_neighbors"] == metadata["calibration"]["base_rows"]
