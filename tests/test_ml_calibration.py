from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pytest

from ml.calibration import (
    PlattCalibrator,
    fit_probability_calibrator,
)


def test_bounded_platt_calibration_does_not_extrapolate_past_fit_support(
    tmp_path: Path,
) -> None:
    probabilities = np.array(
        [0.05, 0.10, 0.20, 0.35, 0.50, 0.70, 0.80, 0.90]
    )
    target = np.array([0, 0, 0, 1, 0, 1, 1, 1])

    calibrator = fit_probability_calibrator(
        "platt",
        probabilities,
        target,
        platt_regularization_c=0.1,
        clip_to_observed_probability_range=True,
    )

    assert isinstance(calibrator, PlattCalibrator)
    assert calibrator.model.C == 0.1
    assert calibrator.raw_probability_min == 0.05
    assert calibrator.raw_probability_max == 0.90
    below, at_minimum, at_maximum, above = calibrator.predict(
        [0.001, 0.05, 0.90, 0.999]
    )
    assert below == pytest.approx(at_minimum)
    assert above == pytest.approx(at_maximum)

    artifact = tmp_path / "calibrator.joblib"
    joblib.dump(calibrator, artifact)
    reloaded = joblib.load(artifact)
    np.testing.assert_allclose(
        reloaded.predict([0.001, 0.05, 0.90, 0.999]),
        [below, at_minimum, at_maximum, above],
    )


@pytest.mark.parametrize("regularization_c", [0.0, -1.0, np.inf, np.nan])
def test_platt_calibration_rejects_invalid_regularization(
    regularization_c: float,
) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        fit_probability_calibrator(
            "platt",
            [0.1, 0.2, 0.8, 0.9],
            [0, 0, 1, 1],
            platt_regularization_c=regularization_c,
        )


def test_platt_calibration_never_reverses_probability_ranking() -> None:
    probabilities = np.array([0.05, 0.15, 0.80, 0.95])
    # This calibration-only cohort conflicts with the base model orientation.
    target = np.array([1, 1, 0, 0])

    calibrator = fit_probability_calibrator(
        "platt",
        probabilities,
        target,
        require_nondecreasing=True,
    )

    assert isinstance(calibrator, PlattCalibrator)
    assert calibrator.nondecreasing_constraint_active is True
    calibrated = calibrator.predict(probabilities)
    np.testing.assert_allclose(calibrated, np.full(len(probabilities), 0.5))
    assert np.all(np.diff(calibrated) >= 0.0)
