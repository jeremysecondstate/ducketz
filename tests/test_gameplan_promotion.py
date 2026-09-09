from copy import deepcopy

import pytest

from ml.gameplan_promotion import (
    DIRECTIONAL_PROMOTION_POLICY, STRICT_PROMOTION_POLICY,
    build_promotion_gate, validate_promoted_report,
)


def report(*, brier=.25087258379960936, logloss=.6949023888969003):
    value = {"assessment": {"brier_score": brier, "log_loss": logloss,
            "expected_calibration_error_10_bin": .04534695546813606},
        "training_base_rate_assessment": {"brier_score": .2498128387290934, "log_loss": .6927728559855708},
        "calibration_diagnostics": {"information_available": True,
            "calibrated_probability_range": [.40503080573803774, .5745672135927844],
            "assessment_probability_range": [.34590436464839813, .5744373410647435],
            "calibration_positive_rate": .4376391982182628, "nondecreasing_constraint_active": False},
        "partition_decision_clusters": {"assessment": 51}}
    value["promotion_gate"] = build_promotion_gate(value["assessment"], value["training_base_rate_assessment"],
        value["calibration_diagnostics"], value["partition_decision_clusters"]["assessment"])
    return value


def test_measured_daily_candidate_qualifies_without_claiming_baseline_outperformance():
    result = report()
    gate = validate_promoted_report(result)
    assert gate["policy_version"] == DIRECTIONAL_PROMOTION_POLICY
    assert gate["status"] == "PROMOTED"
    assert gate["assessment_beats_baseline"] == {"brier_score": False, "log_loss": False}
    assert gate["assessment_minus_baseline"]["brier_score"] > 0
    assert "brier_beats_training_base_rate" not in gate["checks"]
    assert "results were known" in gate["policy_adoption_context"]


@pytest.mark.parametrize("brier,logloss,status", [
    (.255, .70, "PROMOTED"), (.2550000001, .70, "RESEARCH_NOT_PROMOTED"),
    (.255, .7000000001, "RESEARCH_NOT_PROMOTED"), (.249, .689, "PROMOTED"),
])
def test_tolerance_bounds_are_inclusive_and_not_silently_widened(brier, logloss, status):
    value = report()
    gate = build_promotion_gate({"brier_score": brier, "log_loss": logloss, "expected_calibration_error_10_bin": .1},
        {"brier_score": .25, "log_loss": .69}, value["calibration_diagnostics"], 10)
    assert gate["status"] == status


@pytest.mark.parametrize("change", [
    {"baseline_tolerances": {"brier_score": .006, "log_loss": .01}},
    {"baseline_tolerances": {"brier_score": .005, "log_loss": .02}},
    {"baseline_tolerances": {"brier_score": True, "log_loss": .01}},
    {"baseline_tolerances": {"brier_score": .005}},
    {"policy_version": "unknown"},
    {"assessment_beats_baseline": {"brier_score": True, "log_loss": True}},
    {"assessment_minus_baseline": {"brier_score": 0, "log_loss": 0}},
])
def test_version_tolerance_and_outperformance_tampering_is_rejected(change):
    value = report()
    value["promotion_gate"].update(change)
    with pytest.raises(ValueError):
        validate_promoted_report(value)


@pytest.mark.parametrize("field,bad", [("brier_score", float("nan")), ("log_loss", -1),
    ("brier_score", .9), ("expected_calibration_error_10_bin", .16)])
def test_saved_true_checks_cannot_override_actual_scores(field, bad):
    value = report()
    value["assessment"][field] = bad
    with pytest.raises(ValueError):
        validate_promoted_report(value)


@pytest.mark.parametrize("change", [{"calibrated_probability_range": [.5, .5]},
    {"assessment_probability_range": [.5, .5]}, {"nondecreasing_constraint_active": True},
    {"information_available": False}, {"calibration_positive_rate": 1}])
def test_directional_information_is_still_required(change):
    value = report()
    value["calibration_diagnostics"].update(change)
    with pytest.raises(ValueError):
        validate_promoted_report(value)


def test_ten_independent_decision_clusters_are_still_required():
    value = report()
    value["partition_decision_clusters"]["assessment"] = 9
    with pytest.raises(ValueError):
        validate_promoted_report(value)


def test_immutable_legacy_reports_keep_strict_baseline_requirements():
    value = report(brier=.2, logloss=.5)
    value["promotion_gate"] = build_promotion_gate(value["assessment"], value["training_base_rate_assessment"],
        value["calibration_diagnostics"], 51, policy_version=STRICT_PROMOTION_POLICY)
    del value["promotion_gate"]["policy_version"]
    before = deepcopy(value)
    assert validate_promoted_report(value)["status"] == "PROMOTED"
    assert value == before
    value["assessment"]["brier_score"] = .25
    with pytest.raises(ValueError):
        validate_promoted_report(value)
