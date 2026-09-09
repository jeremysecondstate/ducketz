"""Qualification architecture tests using counts only; no training or providers."""
from copy import deepcopy

import pytest

from ml.stock_trader.scope_qualification import (
    POOLED_SCOPE_QUALIFICATION_VERSION, qualify_pooled_scope_coverage,
)


MONDAY = "COST|1w@D+5|6540m"
OVERNIGHT = "COST|1w@D+5|9420m"
UNSEEN_DURATION = "COST|1w@D+5|9480m"


def qualify(counts, diagnostics=None, *, ready=True, source="xnas-itch-archive-v1"):
    return qualify_pooled_scope_coverage(global_ready=ready, fit_scope_decision_clusters=counts,
                                        scope_diagnostics=diagnostics or {}, target_price_source_contract=source)


def test_weekly_three_sample_subset_uses_qualified_pooled_model_and_observed_coverage():
    scores = {"brier": .4, "base_brier": .25, "probability_range": [.6, .6]}
    diagnostics = {MONDAY: {"assessment_decision_clusters": 3, "assessment_scores": scores,
                             "horizon_assessment_scores": {"brier": .2}, "status": "RESEARCH"}}
    before = deepcopy(diagnostics)
    result = qualify({MONDAY: 3, OVERNIGHT: 17}, diagnostics)
    assert result[MONDAY]["status"] == "READY"
    assert result[MONDAY]["fit_decision_clusters"] == 3
    assert result[MONDAY]["pooled_symbol_route_fit_decision_clusters"] == 20
    assert result[MONDAY]["qualification_policy_version"] == POOLED_SCOPE_QUALIFICATION_VERSION
    assert result[MONDAY]["assessment_decision_clusters"] == 3
    assert result[MONDAY]["assessment_scores"] == scores
    assert diagnostics == before


def test_no_exact_assessment_is_diagnostic_only_when_fitted_scope_and_global_gate_pass():
    result = qualify({MONDAY: 1, OVERNIGHT: 19}, {MONDAY: {"assessment_scores": None}})
    assert result[MONDAY]["status"] == "READY"
    assert result[MONDAY]["assessment_decision_clusters"] == 0
    assert result[MONDAY]["assessment_scores"] is None


def test_unseen_duration_and_unseen_cross_symbol_combination_never_qualify():
    counts = {MONDAY: 3, OVERNIGHT: 17, "AAPL|1w@D+5|9480m": 30}
    result = qualify(counts, {UNSEEN_DURATION: {"status": "READY", "assessment_decision_clusters": 40}})
    assert result[UNSEEN_DURATION]["status"] == "RESEARCH"
    assert result[UNSEEN_DURATION]["reason"] == "UNSEEN_EXACT_TARGET_SCOPE"
    assert result[UNSEEN_DURATION]["exact_scope_observed_in_fit"] is False
    assert UNSEEN_DURATION not in qualify(counts)


def test_zero_fit_count_is_not_observed_scope():
    result = qualify({MONDAY: 0, OVERNIGHT: 20})
    assert result[MONDAY]["status"] == "RESEARCH"
    assert result[MONDAY]["reason"] == "UNSEEN_EXACT_TARGET_SCOPE"


def test_missing_cost_route_cannot_borrow_other_route_or_symbol_coverage():
    missing = "COST|4h@16:00|900m"
    result = qualify({"COST|4h@08:00|240m": 100, "AAPL|4h@16:00|900m": 100},
                     {missing: {"assessment_decision_clusters": 20}})
    assert result[missing]["status"] == "RESEARCH"
    assert result[missing]["pooled_symbol_route_fit_decision_clusters"] == 0


def test_nineteen_fitted_route_clusters_remain_insufficient():
    result = qualify({MONDAY: 3, OVERNIGHT: 16})
    assert result[MONDAY]["status"] == "RESEARCH"
    assert result[MONDAY]["reason"] == "INSUFFICIENT_POOLED_SYMBOL_ROUTE_EVIDENCE"


def test_bad_global_gate_cannot_be_overridden_by_perfect_subgroup_diagnostics():
    result = qualify({MONDAY: 30, OVERNIGHT: 50},
                     {MONDAY: {"status": "READY", "assessment_decision_clusters": 100,
                                "assessment_scores": {"brier": 0., "log_loss": 0.}}}, ready=False)
    assert all(row["status"] == "RESEARCH" for row in result.values())
    assert result[MONDAY]["reason"] == "HELD_OUT_HORIZON_QUALITY_NOT_PROMOTED"


@pytest.mark.parametrize("count", [-1, 3.5, True, "30"])
def test_invalid_counts_cannot_inflate_coverage(count):
    with pytest.raises(ValueError, match="counts"):
        qualify({MONDAY: count})


@pytest.mark.parametrize("scope", ["COST|1w@D+5|6540.0m", "COST|1h@gap|660m", "COST|1d@D+2|780m", "FAKE|1w@D+5|6540m"])
def test_noncanonical_or_context_scopes_cannot_inflate_coverage(scope):
    with pytest.raises(ValueError, match="canonical exact execution"):
        qualify({scope: 30})


def test_separate_horizons_cannot_share_a_global_gate():
    with pytest.raises(ValueError, match="one horizon"):
        qualify({MONDAY: 30, "COST|1d@D+1|780m": 30})


def test_source_mismatch_is_rejected():
    with pytest.raises(ValueError, match="mix target price sources"):
        qualify({MONDAY: 30}, {MONDAY: {"target_price_source_contract": "canonical-equity-minute-v1"}})
    with pytest.raises(ValueError, match="Unsupported stock price source"):
        qualify({MONDAY: 30}, source="unverified-feed")


def test_gate_must_be_a_boolean_and_empty_support_stays_empty():
    with pytest.raises(ValueError, match="boolean horizon gate"):
        qualify({MONDAY: 30}, ready="false")
    assert qualify({}) == {}
