"""Preregistered development-only calibration selection for independent stocks.

The final assessment is deliberately absent from this API. Identity and the
existing constrained Platt specification are compared on the later half of
calibration decision clusters after purging earlier labels at that boundary.
No alternative split or calibration parameter is searched when support is low.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from ml.gameplan_probability_target import RAW_DIRECTION_TARGET, resolve_probability_target

from ml.calibration import IdentityCalibrator, fit_probability_calibrator


DEVELOPMENT_SELECTION_POLICY = "independent-stock-development-selection-v1"
RAW_DIRECTION_DEVELOPMENT_SELECTION_POLICY = "independent-stock-information-retaining-development-selection-v2"
DAILY_LOGISTIC_REGULARIZATION_POLICY = "independent-stock-daily-logistic-regularization-v1"
# Fixed before scoring the final assessment: the daily cohort's correlated
# market features need a low-complexity option alongside the existing C=1 fit.
# The native chronological selection partition alone chooses among candidates.
DAILY_LOGISTIC_REGULARIZATION_CANDIDATES = (0.001, 0.01, 0.1, 1.0)
RAW_DIRECTION_LOGISTIC_REGULARIZATION_POLICY = "independent-stock-raw-direction-logistic-regularization-v2"
WEEKLY_PROBABILITY_SHRINKAGE_POLICY = "independent-stock-weekly-prior-probability-shrinkage-v1"
WEEKLY_PROBABILITY_SHRINKAGE_WEIGHTS = (0.25, 0.5, 0.75, 1.0)


def logistic_regularization_candidates(group: str, *, probability_target=None) -> tuple[float, ...]:
    expanded = resolve_probability_target(probability_target) == RAW_DIRECTION_TARGET and group in {"1h", "4h", "1w"}
    return DAILY_LOGISTIC_REGULARIZATION_CANDIDATES if group == "1d" or expanded else (1.0,)


def logistic_regularization_policy(group: str, *, probability_target=None) -> str | None:
    if resolve_probability_target(probability_target) == RAW_DIRECTION_TARGET and group in {"1h", "4h", "1w"}:
        return RAW_DIRECTION_LOGISTIC_REGULARIZATION_POLICY
    return DAILY_LOGISTIC_REGULARIZATION_POLICY if group == "1d" else None


def development_selection_policy(probability_target=None) -> str:
    return (RAW_DIRECTION_DEVELOPMENT_SELECTION_POLICY
            if resolve_probability_target(probability_target) == RAW_DIRECTION_TARGET else DEVELOPMENT_SELECTION_POLICY)


def _information_eligibility(calibrator, raw_probability) -> dict:
    prediction = np.asarray(calibrator.predict(raw_probability), dtype=float)
    finite = bool(np.isfinite(prediction).all())
    spread = float(np.ptp(prediction)) if finite and prediction.size else 0.0
    constrained = bool(getattr(calibrator, "nondecreasing_constraint_active", False))
    slope = (float(np.asarray(calibrator.model.coef_, dtype=float).reshape(-1)[0])
             if getattr(calibrator, "method", "none") == "platt" else None)
    orientation = slope is None or bool(np.isfinite(slope) and slope > 0)
    eligible = finite and spread > 1e-12 and not constrained and orientation
    return {"eligible": bool(eligible), "probability_span": spread,
            "nondecreasing_constraint_active": constrained, "platt_slope": slope,
            "reason": "INFORMATION_RETAINED" if eligible else "CONSTANT_OR_ORIENTATION_CONSTRAINED"}


def select_development_calibrator(calibration: pd.DataFrame, raw_probability: object, *, probability_target=None):
    """Choose on purged development observations; refit only the selected family."""
    frame = calibration.copy().reset_index(drop=True)
    information_required = resolve_probability_target(probability_target) == RAW_DIRECTION_TARGET
    probability = np.asarray(raw_probability, dtype=float)
    if (frame.empty or probability.ndim != 1 or len(probability) != len(frame)
            or not np.isfinite(probability).all() or ((probability < 0) | (probability > 1)).any()):
        raise ValueError("Calibration development probabilities are invalid")
    if not frame["target"].isin([0, 1]).all():
        raise ValueError("Calibration development labels are invalid")
    for column in ("decision_timestamp", "target_window_start", "target_window_end"):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="raise")
        if frame[column].isna().any():
            raise ValueError("Calibration development timestamps are invalid")
    frame["raw_probability"] = probability
    clusters = pd.Index(frame.decision_timestamp.unique()).sort_values()
    midpoint = len(clusters) // 2
    fit = frame.loc[frame.decision_timestamp.isin(clusters[:midpoint])].copy()
    validation = frame.loc[frame.decision_timestamp.isin(clusters[midpoint:])].copy()
    before_purge = len(fit)
    if not validation.empty:
        fit = fit.loc[fit.target_window_end.lt(validation.decision_timestamp.min())].copy()
    report = {
        "policy": development_selection_policy(probability_target),
        "selection_basis": "minimum_later_calibration_development_log_loss_identity_wins_ties",
        "assessment_used_for_selection": False,
        "split": "first_half_vs_second_half_decision_clusters",
        "fit_rows": len(fit), "validation_rows": len(validation),
        "purged_rows": before_purge - len(fit),
        "fit_decision_clusters": int(fit.decision_timestamp.nunique()),
        "validation_decision_clusters": int(validation.decision_timestamp.nunique()),
        "fit_last_target_end": fit.target_window_end.max().isoformat() if len(fit) else None,
        "validation_first_decision": validation.decision_timestamp.min().isoformat() if len(validation) else None,
        "fit_class_counts": {str(label): int(count) for label, count in fit.target.value_counts().sort_index().items()},
        "candidate_metrics": {}, "selected_family": "identity", "full_calibration_refit_rows": 0,
    }
    if (fit.decision_timestamp.nunique() < 2 or validation.decision_timestamp.nunique() < 2
            or fit.target.nunique() != 2):
        report["selection_status"] = "IDENTITY_INSUFFICIENT_PURGED_DEVELOPMENT_SUPPORT"
        return IdentityCalibrator(), report
    fitted_platt = _fit_platt(fit.raw_probability.to_numpy(), fit.target.to_numpy())
    candidates = {"identity": IdentityCalibrator(), "platt": fitted_platt}
    target = validation.target.to_numpy(dtype=int)
    for family, candidate in candidates.items():
        prediction = np.clip(candidate.predict(validation.raw_probability.to_numpy()), 1e-6, 1 - 1e-6)
        report["candidate_metrics"][family] = {
            "log_loss": float(-np.mean(target * np.log(prediction) + (1 - target) * np.log(1 - prediction))),
            "brier_score": float(np.mean((prediction - target) ** 2)),
            "rows": len(validation),
        }
    eligible = list(candidates)
    if information_required:
        report["selection_basis"] = "minimum_later_calibration_development_log_loss_among_information_retaining_candidates_identity_wins_ties"
        report["candidate_eligibility"] = {
            family: _information_eligibility(candidate, validation.raw_probability.to_numpy())
            for family, candidate in candidates.items()}
        eligible = [family for family in candidates if report["candidate_eligibility"][family]["eligible"]]
        if not eligible:
            report["selection_status"] = "NO_INFORMATION_RETAINING_DEVELOPMENT_CANDIDATE"
            return IdentityCalibrator(), report  # The unchanged promotion gate rejects a flat raw map.
    selected = min(eligible, key=lambda family: report["candidate_metrics"][family]["log_loss"])
    report.update(selected_family=selected, selection_status="SELECTED_ON_PURGED_DEVELOPMENT")
    if selected == "identity":
        return IdentityCalibrator(), report
    report["full_calibration_refit_rows"] = len(frame)
    fitted = _fit_platt(probability, frame.target.to_numpy())
    if information_required:
        report["full_refit_eligibility"] = _information_eligibility(fitted, probability)
        if not report["full_refit_eligibility"]["eligible"]:
            if not report["candidate_eligibility"]["identity"]["eligible"]:
                raise ValueError("Full development calibration refit lost information and identity is ineligible")
            report.update(selected_family="identity", selection_status="IDENTITY_AFTER_INELIGIBLE_FULL_DEVELOPMENT_REFIT")
            return IdentityCalibrator(), report
    return fitted, report


def _fit_platt(probability, target):
    return fit_probability_calibrator(
        "platt", probability, target, platt_regularization_c=0.1,
        clip_to_observed_probability_range=True, require_nondecreasing=True,
    )
