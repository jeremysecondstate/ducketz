"""Versioned, numerically verified directional-model operating qualifications.

The v2 tolerance is an operator-authorized operating rule, not a claim that a
model beats its baseline. Immutable earlier publications retain strict v1.
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation


STRICT_PROMOTION_POLICY = "independent-stock-directional-promotion-v1"
DIRECTIONAL_PROMOTION_POLICY = "independent-stock-directional-promotion-v2"
BASELINE_TOLERANCES = {"brier_score": 0.005, "log_loss": 0.01}


def _number(value):
    if isinstance(value, bool):
        raise ValueError("Promotion metrics must be numeric")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Promotion metrics must be finite and nonnegative") from exc
    if not number.is_finite() or number < 0:
        raise ValueError("Promotion metrics must be finite and nonnegative")
    return number


def _varies(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    try:
        lower, upper = map(_number, value)
        return upper <= 1 and upper - lower > Decimal("1e-12")
    except ValueError:
        return False


def build_promotion_gate(assessment: Mapping, baseline: Mapping, diagnostics: Mapping,
                         assessment_decision_clusters: int, *,
                         policy_version: str = DIRECTIONAL_PROMOTION_POLICY) -> dict:
    """Compute facts under one explicit policy; no parameters are optimized."""
    if policy_version not in {STRICT_PROMOTION_POLICY, DIRECTIONAL_PROMOTION_POLICY}:
        raise ValueError("Unknown directional promotion policy")
    metrics = {key: _number(assessment[key]) for key in ("brier_score", "log_loss", "expected_calibration_error_10_bin")}
    prior = {key: _number(baseline[key]) for key in ("brier_score", "log_loss")}
    if metrics["brier_score"] > 1 or prior["brier_score"] > 1 or metrics["expected_calibration_error_10_bin"] > 1:
        raise ValueError("Promotion probability scores are outside their valid range")
    clusters = _number(assessment_decision_clusters)
    if clusters != clusters.to_integral_value():
        raise ValueError("Promotion assessment cluster count must be integral")
    information = diagnostics.get("information_available") is True
    if policy_version == DIRECTIONAL_PROMOTION_POLICY:
        # Recompute the existing varying-probability condition instead of
        # trusting a bare information_available label in a new publication.
        information = (information and _varies(diagnostics.get("calibrated_probability_range"))
            and _varies(diagnostics.get("assessment_probability_range"))
            and diagnostics.get("nondecreasing_constraint_active") is False
            and 0 < _number(diagnostics.get("calibration_positive_rate")) < 1)
    checks = {
        "calibration_retains_directional_information": bool(information),
        "assessment_has_at_least_10_decision_clusters": clusters >= 10,
        "expected_calibration_error_at_most_0_15": metrics["expected_calibration_error_10_bin"] <= Decimal("0.15"),
    }
    if policy_version == STRICT_PROMOTION_POLICY:
        checks.update(brier_beats_training_base_rate=metrics["brier_score"] < prior["brier_score"],
                      log_loss_beats_training_base_rate=metrics["log_loss"] < prior["log_loss"])
    else:
        checks.update(brier_within_baseline_tolerance=metrics["brier_score"] <= prior["brier_score"] + Decimal("0.005"),
                      log_loss_within_baseline_tolerance=metrics["log_loss"] <= prior["log_loss"] + Decimal("0.01"))
    result = {"policy_version": policy_version, "status": "PROMOTED" if all(checks.values()) else "RESEARCH_NOT_PROMOTED",
        "checks": checks}
    if policy_version == DIRECTIONAL_PROMOTION_POLICY:
        result.update(baseline_tolerances=dict(BASELINE_TOLERANCES),
            assessment_minus_baseline={key: float(metrics[key] - prior[key]) for key in prior},
            assessment_beats_baseline={key: metrics[key] < prior[key] for key in prior},
            interpretation="Qualification within operator-authorized baseline-error tolerances; not statistical or measured baseline outperformance.",
            policy_adoption_context="Authorized prospectively after the reference development candidate was locked and its assessment results were known. Model selection does not use assessment outcomes.")
    return result


def validate_promoted_report(report: Mapping) -> dict:
    """Verify new tolerant and immutable legacy strict reports from metrics."""
    try:
        gate = report["promotion_gate"]
        if not isinstance(gate, Mapping) or gate.get("status") != "PROMOTED":
            raise ValueError("A promoted forecast model does not pass its recorded assessment gates")
        version = gate.get("policy_version", STRICT_PROMOTION_POLICY)
        if version == DIRECTIONAL_PROMOTION_POLICY:
            tolerances = gate.get("baseline_tolerances")
            if (not isinstance(tolerances, Mapping) or set(tolerances) != set(BASELINE_TOLERANCES)
                    or any(_number(tolerances[key]) != _number(value) for key, value in BASELINE_TOLERANCES.items())):
                raise ValueError("Directional promotion tolerances differ from the versioned operating policy")
        elif "baseline_tolerances" in gate:
            raise ValueError("Legacy strict promotion cannot adopt baseline tolerances")
        expected = build_promotion_gate(report["assessment"], report["training_base_rate_assessment"],
            report["calibration_diagnostics"], report["partition_decision_clusters"]["assessment"], policy_version=version)
        checks = gate.get("checks")
        if (not isinstance(checks, Mapping) or set(checks) != set(expected["checks"])
                or any(type(value) is not bool for value in checks.values())
                or dict(checks) != expected["checks"] or expected["status"] != "PROMOTED"):
            raise ValueError("Promoted model metrics do not satisfy the recorded assessment gates")
        if version == DIRECTIONAL_PROMOTION_POLICY:
            for field in ("assessment_minus_baseline", "assessment_beats_baseline"):
                if gate.get(field) != expected[field]:
                    raise ValueError("Promoted model baseline comparison differs from its numeric evidence")
        return expected
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("Promoted model lacks valid assessment evidence") from exc
