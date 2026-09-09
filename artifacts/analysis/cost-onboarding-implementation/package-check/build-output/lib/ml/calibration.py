from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

CalibrationMethod = Literal["none", "platt", "isotonic"]
CALIBRATION_ORIENTATION_POLICY_VERSION = (
    "probability-calibration-nondecreasing-or-identity-v1"
)

_EPSILON = 1e-6


class ProbabilityCalibrator(Protocol):
    method: CalibrationMethod

    def predict(self, probabilities: object) -> np.ndarray: ...


@dataclass(frozen=True)
class IdentityCalibrator:
    method: CalibrationMethod = "none"

    def predict(self, probabilities: object) -> np.ndarray:
        return _probability_vector(probabilities)


@dataclass(frozen=True)
class PlattCalibrator:
    model: LogisticRegression
    raw_probability_min: float | None = None
    raw_probability_max: float | None = None
    nondecreasing_constraint_active: bool = False
    method: CalibrationMethod = "platt"

    def predict(self, probabilities: object) -> np.ndarray:
        probability = _probability_vector(probabilities)
        # ``getattr`` preserves compatibility with model artifacts created before
        # calibration-support bounds were persisted on this dataclass.
        lower = getattr(self, "raw_probability_min", None)
        upper = getattr(self, "raw_probability_max", None)
        if lower is not None and upper is not None:
            probability = np.clip(probability, lower, upper)
        logits = _logit(probability).reshape(-1, 1)
        return self.model.predict_proba(logits)[:, 1]


@dataclass(frozen=True)
class IsotonicProbabilityCalibrator:
    model: IsotonicRegression
    method: CalibrationMethod = "isotonic"

    def predict(self, probabilities: object) -> np.ndarray:
        calibrated = np.asarray(
            self.model.predict(_probability_vector(probabilities)), dtype=float
        )
        return np.clip(calibrated, 0.0, 1.0)


def fit_probability_calibrator(
    method: CalibrationMethod,
    probabilities: object,
    target: object,
    *,
    minimum_isotonic_rows: int = 100,
    platt_regularization_c: float = 1.0,
    clip_to_observed_probability_range: bool = False,
    sample_weight: object | None = None,
    require_nondecreasing: bool = False,
) -> ProbabilityCalibrator:
    """Fit calibration only on the caller-provided calibration window.

    Loop B passes only its chronological calibration rows here and applies the
    fitted calibrator to later assessment and live rows. Training and assessment
    outcomes are not used by the calibration fit.
    """

    normalized = str(method).strip().lower()
    if normalized not in {"none", "platt", "isotonic"}:
        raise ValueError("Calibration method must be one of: none, platt, isotonic")

    probability = _probability_vector(probabilities)
    labels = _binary_target(target, expected_rows=len(probability))
    weights = _sample_weight_vector(sample_weight, expected_rows=len(probability))
    if normalized == "none":
        return IdentityCalibrator()
    if np.unique(labels).size != 2:
        raise ValueError("Probability calibration requires both target classes")

    if normalized == "platt":
        regularization_c = float(platt_regularization_c)
        if not np.isfinite(regularization_c) or regularization_c <= 0.0:
            raise ValueError("Platt regularization C must be finite and positive")
        model = LogisticRegression(
            C=regularization_c,
            solver="lbfgs",
            max_iter=2_000,
            random_state=0,
        )
        model.fit(
            _logit(probability).reshape(-1, 1),
            labels,
            sample_weight=weights,
        )
        # Probability calibration may change scale and base rate, but it must
        # not reverse the already-oriented class probability ranking. A
        # negative Platt slope is a calibration-window regime conflict, not
        # evidence that the model's class semantics should be inverted.
        slope = float(np.asarray(model.coef_, dtype=float).reshape(-1)[0])
        constrained = bool(
            require_nondecreasing and (not np.isfinite(slope) or slope <= 0.0)
        )
        if constrained:
            # The maximum-likelihood solution under a nonnegative slope
            # constraint is the boundary slope of zero. Its intercept is the
            # calibration-window base rate, producing a flat, honest map rather
            # than reversing the model's class orientation.
            base_rate = float(
                np.average(labels, weights=weights)
                if weights is not None
                else labels.mean()
            )
            model.coef_ = np.zeros_like(model.coef_, dtype=float)
            model.intercept_ = np.asarray(
                [_logit(np.asarray([base_rate], dtype=float))[0]],
                dtype=float,
            )
        return PlattCalibrator(
            model=model,
            raw_probability_min=(
                float(probability.min())
                if clip_to_observed_probability_range
                else None
            ),
            raw_probability_max=(
                float(probability.max())
                if clip_to_observed_probability_range
                else None
            ),
            nondecreasing_constraint_active=constrained,
        )

    if len(labels) < minimum_isotonic_rows:
        raise ValueError(
            "Isotonic calibration requires at least "
            f"{minimum_isotonic_rows} rows; observed {len(labels)}"
        )
    if np.unique(probability).size < 3:
        raise ValueError("Isotonic calibration requires at least three probability values")
    model = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    model.fit(probability, labels, sample_weight=weights)
    return IsotonicProbabilityCalibrator(model=model)


def _probability_vector(values: object) -> np.ndarray:
    probability = np.asarray(values, dtype=float).reshape(-1)
    if probability.size == 0:
        raise ValueError("Probability vector cannot be empty")
    if not np.isfinite(probability).all():
        raise ValueError("Probability vector must be finite")
    if ((probability < 0.0) | (probability > 1.0)).any():
        raise ValueError("Probabilities must be between 0 and 1")
    return np.clip(probability, _EPSILON, 1.0 - _EPSILON)


def _binary_target(values: object, *, expected_rows: int) -> np.ndarray:
    target = np.asarray(values).reshape(-1)
    if len(target) != expected_rows:
        raise ValueError(
            f"Target row count {len(target)} does not match probabilities {expected_rows}"
        )
    if target.size == 0 or not np.isin(target, [0, 1]).all():
        raise ValueError("Calibration target must contain only binary 0/1 values")
    return target.astype(int)


def _sample_weight_vector(
    values: object | None,
    *,
    expected_rows: int,
) -> np.ndarray | None:
    if values is None:
        return None
    weights = np.asarray(values, dtype=float).reshape(-1)
    if len(weights) != expected_rows:
        raise ValueError(
            f"Sample-weight row count {len(weights)} does not match probabilities "
            f"{expected_rows}"
        )
    if not np.isfinite(weights).all() or (weights < 0.0).any():
        raise ValueError("Sample weights must be finite and nonnegative")
    if not bool((weights > 0.0).any()):
        raise ValueError("At least one sample weight must be positive")
    return weights


def _logit(probability: np.ndarray) -> np.ndarray:
    clipped = np.clip(probability, _EPSILON, 1.0 - _EPSILON)
    return np.log(clipped / (1.0 - clipped))
