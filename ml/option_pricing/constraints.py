from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, minimize

from ml.option_pricing.policies import ProjectionPolicy


class ProjectionError(RuntimeError):
    """A surface could not be projected into the declared shape contract."""


@dataclass(frozen=True)
class ShapeViolations:
    bound: np.ndarray
    monotonicity: np.ndarray
    convexity: np.ndarray

    @property
    def any(self) -> np.ndarray:
        return self.bound | self.monotonicity | self.convexity

    @property
    def rate(self) -> float:
        return float(np.mean(self.any)) if len(self.any) else 0.0


@dataclass(frozen=True)
class ShapeProjection:
    raw: np.ndarray
    constrained: np.ndarray
    lower_bounds: np.ndarray
    upper_bounds: np.ndarray
    raw_violations: ShapeViolations
    constrained_violations: ShapeViolations
    correction: np.ndarray


def shape_violations(
    strikes: np.ndarray,
    prices: np.ndarray,
    lower_bounds: np.ndarray,
    upper_bounds: np.ndarray,
    call_put: str,
    *,
    tolerance: float = 1e-8,
) -> ShapeViolations:
    k, y, lower, upper = _validated_arrays(
        strikes,
        prices,
        lower_bounds,
        upper_bounds,
    )
    option_type = _call_put(call_put)
    bound = (y < lower - tolerance) | (y > upper + tolerance)
    monotonicity = np.zeros(len(y), dtype=bool)
    differences = np.diff(y)
    bad_monotonicity = (
        differences > tolerance
        if option_type == "CALL"
        else differences < -tolerance
    )
    for index in np.flatnonzero(bad_monotonicity):
        monotonicity[index : index + 2] = True
    convexity = np.zeros(len(y), dtype=bool)
    if len(y) >= 3:
        slopes = differences / np.diff(k)
        bad_convexity = np.diff(slopes) < -tolerance
        for index in np.flatnonzero(bad_convexity):
            convexity[index : index + 3] = True
    return ShapeViolations(bound, monotonicity, convexity)


def project_surface_values(
    strikes: np.ndarray,
    raw_prices: np.ndarray,
    lower_bounds: np.ndarray,
    upper_bounds: np.ndarray,
    call_put: str,
    *,
    weights: np.ndarray | None = None,
    policy: ProjectionPolicy | None = None,
) -> ShapeProjection:
    """Deterministic weighted least-squares projection by strike."""

    effective = policy or ProjectionPolicy()
    k, raw, lower, upper = _validated_arrays(
        strikes,
        raw_prices,
        lower_bounds,
        upper_bounds,
    )
    option_type = _call_put(call_put)
    objective_weights = (
        np.ones(len(raw), dtype=float)
        if weights is None
        else np.asarray(weights, dtype=float)
    )
    if (
        objective_weights.shape != raw.shape
        or not np.isfinite(objective_weights).all()
        or np.any(objective_weights <= 0.0)
    ):
        raise ValueError("Projection weights must be finite and positive")
    raw_diagnostics = shape_violations(
        k,
        raw,
        lower,
        upper,
        option_type,
        tolerance=effective.tolerance,
    )
    initial = np.clip(raw, lower, upper)
    if not raw_diagnostics.any.any():
        constrained = raw.copy()
    else:
        matrix = _shape_constraint_matrix(k, option_type)

        def objective(values: np.ndarray) -> float:
            difference = values - raw
            return 0.5 * float(np.dot(objective_weights, difference * difference))

        def gradient(values: np.ndarray) -> np.ndarray:
            return objective_weights * (values - raw)

        constraints = (
            (LinearConstraint(matrix, 0.0, np.inf),)
            if matrix.size
            else ()
        )
        result = minimize(
            objective,
            initial,
            jac=gradient,
            method="SLSQP",
            bounds=Bounds(lower, upper),
            constraints=constraints,
            options={
                "ftol": effective.tolerance,
                "maxiter": effective.maximum_iterations,
                "disp": False,
            },
        )
        if not result.success or not np.isfinite(result.x).all():
            raise ProjectionError(f"Surface shape projection failed: {result.message}")
        constrained = np.asarray(result.x, dtype=float)
    constrained_diagnostics = shape_violations(
        k,
        constrained,
        lower,
        upper,
        option_type,
        tolerance=max(effective.tolerance * 10.0, 1e-7),
    )
    if constrained_diagnostics.any.any():
        raise ProjectionError("Projected surface still violates the shape contract")
    return ShapeProjection(
        raw=raw,
        constrained=constrained,
        lower_bounds=lower,
        upper_bounds=upper,
        raw_violations=raw_diagnostics,
        constrained_violations=constrained_diagnostics,
        correction=np.abs(constrained - raw),
    )


def project_prediction_intervals(
    strikes: np.ndarray,
    raw_mean: np.ndarray,
    raw_interval_80_lower: np.ndarray,
    raw_interval_80_upper: np.ndarray,
    raw_interval_95_lower: np.ndarray,
    raw_interval_95_upper: np.ndarray,
    lower_bounds: np.ndarray,
    upper_bounds: np.ndarray,
    call_put: str,
    *,
    weights: np.ndarray | None = None,
    policy: ProjectionPolicy | None = None,
) -> dict[str, ShapeProjection]:
    """Project means and nested interval endpoints without relabeling raw values."""

    mean = project_surface_values(
        strikes,
        raw_mean,
        lower_bounds,
        upper_bounds,
        call_put,
        weights=weights,
        policy=policy,
    )
    low80 = project_surface_values(
        strikes,
        raw_interval_80_lower,
        lower_bounds,
        mean.constrained,
        call_put,
        weights=weights,
        policy=policy,
    )
    high80 = project_surface_values(
        strikes,
        raw_interval_80_upper,
        mean.constrained,
        upper_bounds,
        call_put,
        weights=weights,
        policy=policy,
    )
    low95 = project_surface_values(
        strikes,
        raw_interval_95_lower,
        lower_bounds,
        low80.constrained,
        call_put,
        weights=weights,
        policy=policy,
    )
    high95 = project_surface_values(
        strikes,
        raw_interval_95_upper,
        high80.constrained,
        upper_bounds,
        call_put,
        weights=weights,
        policy=policy,
    )
    return {
        "mean": mean,
        "interval_80_lower": low80,
        "interval_80_upper": high80,
        "interval_95_lower": low95,
        "interval_95_upper": high95,
    }


def _shape_constraint_matrix(strikes: np.ndarray, call_put: str) -> np.ndarray:
    rows: list[np.ndarray] = []
    for index in range(len(strikes) - 1):
        row = np.zeros(len(strikes), dtype=float)
        if call_put == "CALL":
            row[index] = 1.0
            row[index + 1] = -1.0
        else:
            row[index] = -1.0
            row[index + 1] = 1.0
        rows.append(row)
    for index in range(len(strikes) - 2):
        left_width = strikes[index + 1] - strikes[index]
        right_width = strikes[index + 2] - strikes[index + 1]
        row = np.zeros(len(strikes), dtype=float)
        row[index] = 1.0 / left_width
        row[index + 1] = -(1.0 / left_width + 1.0 / right_width)
        row[index + 2] = 1.0 / right_width
        rows.append(row)
    return np.vstack(rows) if rows else np.empty((0, len(strikes)), dtype=float)


def _validated_arrays(
    strikes: np.ndarray,
    prices: np.ndarray,
    lower_bounds: np.ndarray,
    upper_bounds: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    arrays = tuple(
        np.asarray(value, dtype=float)
        for value in (strikes, prices, lower_bounds, upper_bounds)
    )
    if any(array.ndim != 1 for array in arrays):
        raise ValueError("Surface projection inputs must be one-dimensional")
    if len({len(array) for array in arrays}) != 1 or not len(arrays[0]):
        raise ValueError("Surface projection inputs must have one non-empty length")
    if not all(np.isfinite(array).all() for array in arrays):
        raise ValueError("Surface projection inputs must be finite")
    strikes, prices, lower, upper = arrays
    if np.any(np.diff(strikes) <= 0.0):
        raise ValueError("Surface strikes must be strictly increasing")
    if np.any(lower > upper):
        raise ValueError("Surface pointwise bounds are inconsistent")
    return strikes, prices, lower, upper


def _call_put(value: object) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"CALL", "C"}:
        return "CALL"
    if normalized in {"PUT", "P"}:
        return "PUT"
    raise ValueError("call_put must be CALL or PUT")


__all__ = [
    "ProjectionError",
    "ShapeProjection",
    "ShapeViolations",
    "project_prediction_intervals",
    "project_surface_values",
    "shape_violations",
]
