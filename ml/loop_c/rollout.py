from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


LOOP_C_ROLLOUT_POLICY_VERSION = "loop-c-options-1d-plus-observe-evidence-gate-v2"


@dataclass(frozen=True)
class LoopCRolloutPolicy:
    """Minimum evidence for a proposal; never an authority grant."""

    minimum_completed_xnys_sessions: int = 40
    minimum_daily_clusters: int = 30
    minimum_nonoverlapping_weekly_cohorts: int = 8
    minimum_reconciled_observations: int = 20
    minimum_halt_drills: int = 2
    minimum_rollback_drills: int = 1
    policy_version: str = LOOP_C_ROLLOUT_POLICY_VERSION


@dataclass(frozen=True)
class LoopCShadowEvidence:
    completed_xnys_sessions: int
    mature_decision_clusters: Mapping[str, int]
    nonoverlapping_weekly_cohorts: int
    reconciled_observations: int
    halt_drills_passed: int
    rollback_drills_passed: int
    calibration_gates_passed: bool
    interval_coverage_gates_passed: bool
    cost_latency_missing_data_stress_passed: bool
    symbol_and_regime_stability_passed: bool
    publication_integrity_passed: bool
    paper_broker_reconciliation_passed: bool
    deterministic_gate_violations: int
    orders_placed: int


@dataclass(frozen=True)
class LoopCRolloutAssessment:
    status: str
    eligible_for_operator_review: bool
    authority_expansion_allowed: bool
    automatic_promotion_allowed: bool
    failed_gates: tuple[str, ...]
    policy_version: str


def evaluate_loop_c_rollout(
    evidence: LoopCShadowEvidence,
    *,
    policy: LoopCRolloutPolicy | None = None,
) -> LoopCRolloutAssessment:
    """Evaluate maturity while permanently withholding activation authority."""

    rules = policy or LoopCRolloutPolicy()
    failed: list[str] = []
    if evidence.completed_xnys_sessions < rules.minimum_completed_xnys_sessions:
        failed.append("MINIMUM_40_COMPLETED_XNYS_SESSIONS")
    if int(evidence.mature_decision_clusters.get("1d", 0)) < (
        rules.minimum_daily_clusters
    ):
        failed.append("MINIMUM_30_1D_MATURE_CLUSTERS")
    if (
        evidence.nonoverlapping_weekly_cohorts
        < rules.minimum_nonoverlapping_weekly_cohorts
    ):
        failed.append("MINIMUM_8_NONOVERLAPPING_WEEKLY_COHORTS")
    if evidence.reconciled_observations < rules.minimum_reconciled_observations:
        failed.append("MINIMUM_20_RECONCILED_OBSERVATIONS")
    if evidence.halt_drills_passed < rules.minimum_halt_drills:
        failed.append("MINIMUM_2_HALT_DRILLS")
    if evidence.rollback_drills_passed < rules.minimum_rollback_drills:
        failed.append("MINIMUM_1_ROLLBACK_DRILL")
    boolean_gates = {
        "CALIBRATION_GATES": evidence.calibration_gates_passed,
        "INTERVAL_COVERAGE_GATES": evidence.interval_coverage_gates_passed,
        "COST_LATENCY_MISSING_DATA_STRESS": (
            evidence.cost_latency_missing_data_stress_passed
        ),
        "SYMBOL_AND_REGIME_STABILITY": evidence.symbol_and_regime_stability_passed,
        "PUBLICATION_INTEGRITY": evidence.publication_integrity_passed,
        "PAPER_BROKER_RECONCILIATION": (
            evidence.paper_broker_reconciliation_passed
        ),
    }
    failed.extend(name for name, passed in boolean_gates.items() if not passed)
    if evidence.deterministic_gate_violations != 0:
        failed.append("ZERO_DETERMINISTIC_GATE_VIOLATIONS")
    if evidence.orders_placed != 0:
        failed.append("ZERO_ORDERS_DURING_OBSERVE_ONLY")

    eligible = not failed
    return LoopCRolloutAssessment(
        status=(
            "ELIGIBLE_FOR_OPERATOR_REVIEW"
            if eligible
            else "OBSERVE_ONLY_EVIDENCE_ACCUMULATING"
        ),
        eligible_for_operator_review=eligible,
        # This evaluator can produce a proposal gate only. No favorable metric,
        # scheduler stage, or input file can turn it into order authority.
        authority_expansion_allowed=False,
        automatic_promotion_allowed=False,
        failed_gates=tuple(failed),
        policy_version=rules.policy_version,
    )


__all__ = [
    "LOOP_C_ROLLOUT_POLICY_VERSION",
    "LoopCRolloutAssessment",
    "LoopCRolloutPolicy",
    "LoopCShadowEvidence",
    "evaluate_loop_c_rollout",
]
