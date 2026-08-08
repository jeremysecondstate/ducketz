from __future__ import annotations

import importlib.metadata
import json
import math
import os
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

import exchange_calendars as xcals
import numpy as np
import pandas as pd
from scipy.stats import t as student_t

from ml.artifacts import (
    file_checksum,
    semantic_metadata_fingerprint,
    utc_timestamp,
)
from ml.option_pricing.policies import (
    BSGPModelPolicy,
    ContractSelectionPolicy,
    OPTION_PRICING_CONTRACT_POLICY_VERSION,
    OPTION_PRICING_DIVIDEND_POLICY_VERSION,
    OPTION_PRICING_EXPIRATION_POLICY_VERSION,
    OPTION_PRICING_FEATURE_CONTRACT_VERSION,
    OPTION_PRICING_POLICY_VERSION,
    OPTION_PRICING_PROJECTION_POLICY_VERSION,
    OPTION_PRICING_RATE_POLICY_VERSION,
    OPTION_PRICING_SCHEMA_VERSION,
    OPTION_PRICING_TIMING_POLICY_VERSION,
    OPTION_PRICING_UNCERTAINTY_POLICY_VERSION,
    OPTION_PRICING_VOLATILITY_POLICY_VERSION,
    OPTION_PRICING_WEIGHTING_POLICY_VERSION,
    PricingPartitionConfig,
    ProjectionPolicy,
)


ELIGIBILITY_PROTOCOL_VERSION = "option-pricing-eligibility-v2"
ELIGIBILITY_POLICY_VERSION = "option-pricing-eligibility-policy-v2"
ELIGIBILITY_POLICY_RECEIPT_VERSION = "option-pricing-eligibility-policy-receipt-v1"
ELIGIBILITY_REPORT_VERSION = "option-pricing-eligibility-report-v2"
ELIGIBILITY_REPORT_RECEIPT_VERSION = "option-pricing-eligibility-report-receipt-v1"
ELIGIBILITY_REPORT_POINTER_VERSION = "option-pricing-eligibility-report-pointer-v1"

REQUIRED_SYMBOLS = ("NVDA", "GOOG", "MU")
REQUIRED_CALL_PUTS = ("CALL", "PUT")
EVIDENCE_LANES = (
    "OFFLINE_TRAIN_CALIBRATION",
    "UNTOUCHED_OFFLINE_ASSESSMENT",
    "CLOSED_LOCKBOX",
    "PROSPECTIVE_SCHWAB",
    "STRATEGY_SHADOW_OUTCOMES",
    "FIXTURE_TEST_ONLY",
)

_FIXTURE_PROVIDERS = {
    "fixture",
    "fixtures",
    "synthetic",
    "test",
    "mock",
    "generated",
}
class EligibilityError(RuntimeError):
    """Eligibility evidence or an immutable eligibility artifact failed closed."""


@dataclass(frozen=True)
class EligibilityPolicy:
    required_symbols: tuple[str, ...] = REQUIRED_SYMBOLS
    required_call_puts: tuple[str, ...] = REQUIRED_CALL_PUTS
    minimum_train_clusters: int = 252
    calibration_clusters: int = 63
    assessment_clusters: int = 63
    lockbox_clusters: int = 126
    minimum_calendar_months: int = 6
    statistical_method: str = "paired-session-blocked-student-t-v1"
    confidence_level: float = 0.95
    minimum_comparison_snapshots: int = 63
    minimum_comparison_sessions: int = 15
    interval_80_minimum: float = 0.72
    interval_80_maximum: float = 0.88
    interval_95_minimum: float = 0.90
    interval_95_maximum: float = 0.99
    maximum_constrained_violation_rate: float = 0.0
    maximum_median_normalized_projection: float = 0.0025
    edge_bucket_boundaries: tuple[float, ...] = (
        -math.inf,
        -2.0,
        -1.0,
        0.0,
        1.0,
        2.0,
        math.inf,
    )
    minimum_constraint_snapshots: int = 63
    minimum_constraint_sessions: int = 15
    minimum_edge_bucket_snapshots: int = 5
    minimum_edge_bucket_sessions: int = 3
    minimum_edge_cohort_snapshots: int = 63
    minimum_edge_cohort_sessions: int = 15
    minimum_net_realized_value_per_contract: float = 0.0
    per_contract_fee_usd: float = 0.65
    minimum_strategy_pairs: int = 60
    minimum_strategy_sessions: int = 20
    minimum_strategy_improvement_usd: float = 0.0
    strategy_uncertainty_coverage_minimum: float = 0.90
    strategy_uncertainty_coverage_maximum: float = 0.99
    minimum_prospective_predictions_per_route: int = 60
    minimum_prospective_sessions_per_route: int = 20
    maximum_operational_readiness_age_hours: int = 24

    def __post_init__(self) -> None:
        symbols = tuple(str(value).strip().upper() for value in self.required_symbols)
        call_puts = tuple(str(value).strip().upper() for value in self.required_call_puts)
        if symbols != REQUIRED_SYMBOLS:
            raise ValueError("Eligibility v2 requires exactly NVDA, GOOG, and MU")
        if call_puts != REQUIRED_CALL_PUTS:
            raise ValueError("Eligibility v2 requires independent CALL and PUT routes")
        for name in (
            "minimum_train_clusters",
            "calibration_clusters",
            "assessment_clusters",
            "lockbox_clusters",
            "minimum_comparison_snapshots",
            "minimum_comparison_sessions",
            "minimum_constraint_snapshots",
            "minimum_constraint_sessions",
            "minimum_edge_bucket_snapshots",
            "minimum_edge_bucket_sessions",
            "minimum_edge_cohort_snapshots",
            "minimum_edge_cohort_sessions",
            "minimum_strategy_pairs",
            "minimum_strategy_sessions",
            "minimum_prospective_predictions_per_route",
            "minimum_prospective_sessions_per_route",
            "maximum_operational_readiness_age_hours",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")
        minimums = {
            "minimum_train_clusters": 252,
            "calibration_clusters": 63,
            "assessment_clusters": 63,
            "lockbox_clusters": 126,
            "minimum_comparison_snapshots": 63,
            "minimum_comparison_sessions": 15,
            "minimum_constraint_snapshots": 63,
            "minimum_constraint_sessions": 15,
            "minimum_edge_bucket_snapshots": 5,
            "minimum_edge_bucket_sessions": 3,
            "minimum_edge_cohort_snapshots": 63,
            "minimum_edge_cohort_sessions": 15,
            "minimum_strategy_pairs": 60,
            "minimum_strategy_sessions": 20,
            "minimum_prospective_predictions_per_route": 60,
            "minimum_prospective_sessions_per_route": 20,
        }
        weakened = [
            name for name, minimum in minimums.items() if int(getattr(self, name)) < minimum
        ]
        if weakened:
            raise ValueError(
                "Eligibility v2 thresholds cannot be weakened: " + ", ".join(weakened)
            )
        if self.minimum_calendar_months < 6:
            raise ValueError("Eligibility v2 cannot weaken the six-month span")
        if not 0.5 < self.confidence_level < 1.0:
            raise ValueError("confidence_level must be between 0.5 and 1")
        if self.statistical_method != "paired-session-blocked-student-t-v1":
            raise ValueError("Eligibility v2 requires paired session-blocked inference")
        if self.confidence_level < 0.95:
            raise ValueError("Eligibility v2 confidence cannot be below 95%")
        if not (
            0.0 <= self.interval_80_minimum <= self.interval_80_maximum <= 1.0
            and 0.0 <= self.interval_95_minimum <= self.interval_95_maximum <= 1.0
        ):
            raise ValueError("Interval tolerances are invalid")
        if self.maximum_constrained_violation_rate != 0.0:
            raise ValueError("Eligibility v2 permits no constrained arbitrage violations")
        if self.maximum_median_normalized_projection > 0.0025:
            raise ValueError("Eligibility v2 cannot weaken the projection threshold")
        if self.interval_80_minimum < 0.72 or self.interval_80_maximum > 0.88:
            raise ValueError("Eligibility v2 cannot widen the 80% interval tolerance")
        if self.interval_95_minimum < 0.90 or self.interval_95_maximum > 0.99:
            raise ValueError("Eligibility v2 cannot widen the 95% interval tolerance")
        if self.per_contract_fee_usd < 0.65:
            raise ValueError("Eligibility v2 contract friction cannot be weakened")
        if self.minimum_net_realized_value_per_contract < 0.0:
            raise ValueError("Eligibility v2 realized-value threshold cannot be negative")
        if self.minimum_strategy_improvement_usd < 0.0:
            raise ValueError("Eligibility v2 Strategy threshold cannot be negative")
        if self.maximum_operational_readiness_age_hours > 24:
            raise ValueError("Eligibility v2 operational evidence cannot be older than 24 hours")
        if (
            self.strategy_uncertainty_coverage_minimum < 0.90
            or self.strategy_uncertainty_coverage_maximum > 0.99
        ):
            raise ValueError("Eligibility v2 Strategy coverage range cannot be widened")
        if not (
            0.0
            <= self.strategy_uncertainty_coverage_minimum
            <= self.strategy_uncertainty_coverage_maximum
            <= 1.0
        ):
            raise ValueError("Strategy uncertainty coverage tolerances are invalid")
        boundaries = tuple(float(value) for value in self.edge_bucket_boundaries)
        if len(boundaries) < 3 or any(
            right <= left for left, right in zip(boundaries, boundaries[1:])
        ):
            raise ValueError("edge_bucket_boundaries must be strictly increasing")

    @property
    def required_routes(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (symbol, call_put)
            for symbol in self.required_symbols
            for call_put in self.required_call_puts
        )


@dataclass(frozen=True)
class EligibilityPolicyArtifact:
    directory: Path
    policy: Mapping[str, object]
    receipt: Mapping[str, object]
    policy_hash: str


@dataclass(frozen=True)
class EligibilityReportArtifact:
    directory: Path
    report: Mapping[str, object]
    receipt: Mapping[str, object]


def eligibility_policy_payload(
    policy: EligibilityPolicy | None = None,
    *,
    partition_config: PricingPartitionConfig | None = None,
    model_policy: BSGPModelPolicy | None = None,
    contract_policy: ContractSelectionPolicy | None = None,
    projection_policy: ProjectionPolicy | None = None,
) -> dict[str, object]:
    """Build the exact pre-lockbox policy without a mutable publication time."""

    effective = policy or EligibilityPolicy()
    effective_partitions = partition_config or PricingPartitionConfig()
    effective_model = model_policy or BSGPModelPolicy()
    effective_contract = contract_policy or ContractSelectionPolicy()
    effective_projection = projection_policy or ProjectionPolicy()
    versions = {
        "pricing_policy": OPTION_PRICING_POLICY_VERSION,
        "schema": OPTION_PRICING_SCHEMA_VERSION,
        "feature_contract": OPTION_PRICING_FEATURE_CONTRACT_VERSION,
        "timing": OPTION_PRICING_TIMING_POLICY_VERSION,
        "rate": OPTION_PRICING_RATE_POLICY_VERSION,
        "dividend": OPTION_PRICING_DIVIDEND_POLICY_VERSION,
        "volatility": OPTION_PRICING_VOLATILITY_POLICY_VERSION,
        "expiration": OPTION_PRICING_EXPIRATION_POLICY_VERSION,
        "contract_selection": OPTION_PRICING_CONTRACT_POLICY_VERSION,
        "weighting": OPTION_PRICING_WEIGHTING_POLICY_VERSION,
        "uncertainty": OPTION_PRICING_UNCERTAINTY_POLICY_VERSION,
        "projection": OPTION_PRICING_PROJECTION_POLICY_VERSION,
        "eligibility_protocol": ELIGIBILITY_PROTOCOL_VERSION,
    }
    return {
        "schema_version": ELIGIBILITY_POLICY_VERSION,
        "required_universe": {
            "symbols": list(effective.required_symbols),
            "call_put_routes": list(effective.required_call_puts),
            "routes": [
                {"symbol": symbol, "call_put": call_put}
                for symbol, call_put in effective.required_routes
            ],
            "missing_route_behavior": "NOT_PROVEN_AND_RETAINED_IN_GLOBAL_AGGREGATION",
        },
        "versions": versions,
        "chronological_partitions": {
            "training_clusters": effective.minimum_train_clusters,
            "calibration_clusters": effective.calibration_clusters,
            "untouched_assessment_clusters": effective.assessment_clusters,
            "closed_lockbox_clusters": effective.lockbox_clusters,
            "minimum_calendar_months": effective.minimum_calendar_months,
            "cluster_unit": "complete-target_snapshot_for",
            "causal_boundary_purging": (
                "every label and all information required to construct it must be "
                "strictly available before the next partition begins"
            ),
        },
        "historical_evidence_acquisition": {
            "provider": "databento",
            "dataset": "OPRA.PILLAR",
            "definition_schema": "definition",
            "quote_schema": "cbbo-1m",
            "session_calendar": "XNYS",
            "target_timezone": "America/New_York",
            "target_market_times": ["10:00", "11:30", "13:30", "15:00"],
            "early_close_points_after_session_close_removed": True,
            "definition_request_boundary": "whole UTC day",
            "cbbo_source_backward_minutes": 5,
            "cbbo_target_forward_minutes": 5,
            "raw_symbol_filtering_before_paid_cbbo_request": True,
            "exact_completed_underlying_bar_required": True,
            "point_in_time_rate_required": True,
            "lagged_dividend_then_causal_put_call_parity": True,
            "missing_input_behavior": "NOT_PROVEN_NO_REQUEST_WIDENING",
        },
        "thresholds": {
            "comparators": {
                "required": ["black_scholes", "constant_residual", "standard_gp"],
                "minimum_snapshots_per_route": effective.minimum_comparison_snapshots,
                "minimum_sessions_per_route": effective.minimum_comparison_sessions,
                "required_mean_paired_improvement": ">0",
                "required_lower_confidence_bound": ">0",
            },
            "intervals": {
                "coverage_80": [
                    effective.interval_80_minimum,
                    effective.interval_80_maximum,
                ],
                "coverage_95": [
                    effective.interval_95_minimum,
                    effective.interval_95_maximum,
                ],
                "minimum_snapshots_per_route": effective.minimum_comparison_snapshots,
                "minimum_sessions_per_route": effective.minimum_comparison_sessions,
            },
            "projection": {
                "maximum_constrained_violation_rate": (
                    effective.maximum_constrained_violation_rate
                ),
                "maximum_median_normalized_projection": (
                    effective.maximum_median_normalized_projection
                ),
                "minimum_snapshots_per_route": effective.minimum_constraint_snapshots,
                "minimum_sessions_per_route": effective.minimum_constraint_sessions,
            },
            "economic_edge": {
                "bucket_boundaries_in_half_spreads": list(
                    _json_safe(effective.edge_bucket_boundaries)
                ),
                "minimum_bucket_snapshots": effective.minimum_edge_bucket_snapshots,
                "minimum_bucket_sessions": effective.minimum_edge_bucket_sessions,
                "minimum_cohort_snapshots": effective.minimum_edge_cohort_snapshots,
                "minimum_cohort_sessions": effective.minimum_edge_cohort_sessions,
                "minimum_net_realized_value_per_contract_usd": (
                    effective.minimum_net_realized_value_per_contract
                ),
                "per_contract_fee_usd": effective.per_contract_fee_usd,
                "directional_signal": "sign(constrained_fair_value-black_scholes_price)",
                "realized_value": (
                    "direction*(observed_mid-black_scholes_price)*multiplier"
                    "-half_spread*multiplier-per_contract_fee"
                ),
                "required_behavior": (
                    "session-blocked lower confidence bound above threshold and "
                    "nondecreasing predeclared bucket outcomes"
                ),
            },
            "strategy": {
                "minimum_paired_candidates_per_symbol_call_put_route": (
                    effective.minimum_strategy_pairs
                ),
                "minimum_sessions_per_symbol_call_put_route": (
                    effective.minimum_strategy_sessions
                ),
                "minimum_improvement_usd": effective.minimum_strategy_improvement_usd,
                "required_lower_confidence_bound": ">minimum_improvement_usd",
                "uncertainty_coverage": [
                    effective.strategy_uncertainty_coverage_minimum,
                    effective.strategy_uncertainty_coverage_maximum,
                ],
                "same_candidate_cohort_required": True,
                "fees_and_adverse_bbo_required": True,
            },
            "prospective": {
                "minimum_completed_predictions_per_symbol_call_put_route": (
                    effective.minimum_prospective_predictions_per_route
                ),
                "minimum_distinct_sessions_per_route": (
                    effective.minimum_prospective_sessions_per_route
                ),
                "earliest_valid_prediction_only": True,
                "offline_backfill_counted": False,
            },
            "operational_readiness": {
                "maximum_age_hours": effective.maximum_operational_readiness_age_hours,
                "exact_eligibility_policy_hash_required": True,
                "dependency_configuration_capacity_cli_and_benchmark_must_pass": True,
            },
        },
        "statistics": {
            "method": effective.statistical_method,
            "confidence_level": effective.confidence_level,
            "pairing_unit": "exact natural target within route",
            "blocking_unit": "America/New_York exchange session",
        },
        "evidence_lanes": {
            "lanes": list(EVIDENCE_LANES),
            "offline_train_calibration_provider": "databento-opra",
            "untouched_assessment_provider": "databento-opra",
            "prospective_provider": "schwab",
            "fixture_rule": "FIXTURE_TEST_ONLY can never satisfy a production gate",
            "mixed_lane_rule": "mixed providers within one evidence lane are NOT_PROVEN",
            "opra_rule": "OPRA is always OFFLINE and never increments prospective counts",
        },
        "lockbox": {
            "initial_status": "CLOSED_UNTOUCHED_UNSCORED",
            "authorization_action": "OPEN_AND_SCORE_OPTION_PRICING_LOCKBOX_ONCE",
            "frozen_candidate_hash_required": True,
            "eligibility_policy_hash_required": True,
            "all_routes_and_required_buckets_must_pass": True,
            "failure_rule": "candidate permanently invalidated; same lockbox cannot be rescored",
            "changed_model_or_policy_rule": (
                "new candidate and genuinely fresh future lockbox required"
            ),
        },
        "invalidation_and_rollback": {
            "invalidate_on": [
                "source, schema, checksum, receipt-chain, or timestamp verification failure",
                "code, dependency, model, feature, timing, or policy hash mismatch",
                "candidate retraining or hyperparameter mutation after freeze",
                "lockbox failure or second score attempt",
                "stale or unavailable required route/cohort/bucket",
            ],
            "pointer_rollback": (
                "restore a previously verified pointer record atomically; never delete evidence"
            ),
            "automated_action_allowed": False,
        },
        "runtime": {
            "python": {
                "implementation": platform.python_implementation(),
                "version": platform.python_version(),
            },
            "dependencies": _installed_versions(),
        },
        "model_contract": {
            "partitions": _json_safe(asdict(effective_partitions)),
            "bsgp": _json_safe(asdict(effective_model)),
            "contract_selection": _json_safe(asdict(effective_contract)),
            "projection": _json_safe(asdict(effective_projection)),
        },
        "code_inventory": _code_inventory(),
        "policy_values": _json_safe(asdict(effective)),
    }


def publish_eligibility_policy(
    datastore_root: Path,
    *,
    policy: EligibilityPolicy | None = None,
    partition_config: PricingPartitionConfig | None = None,
    model_policy: BSGPModelPolicy | None = None,
    contract_policy: ContractSelectionPolicy | None = None,
    projection_policy: ProjectionPolicy | None = None,
    published_at: object | None = None,
) -> EligibilityPolicyArtifact:
    """Publish/reuse the immutable pre-lockbox policy before evidence is opened."""

    root = Path(datastore_root).resolve()
    payload = eligibility_policy_payload(
        policy,
        partition_config=partition_config,
        model_policy=model_policy,
        contract_policy=contract_policy,
        projection_policy=projection_policy,
    )
    policy_hash = semantic_metadata_fingerprint(payload)
    parent = root / "ml" / "option-pricing-eligibility-policies"
    directory = parent / policy_hash
    if directory.exists():
        return read_eligibility_policy(directory, datastore_root=root)
    parent.mkdir(parents=True, exist_ok=True)
    staging = parent / f".{policy_hash}.tmp-{os.getpid()}"
    if staging.exists():
        raise EligibilityError(f"Eligibility policy staging path already exists: {staging}")
    staging.mkdir()
    try:
        policy_path = staging / "policy.json"
        _write_json_atomic(policy_path, payload)
        receipt = {
            "schema_version": ELIGIBILITY_POLICY_RECEIPT_VERSION,
            "policy_hash": policy_hash,
            "policy_path": (
                directory.relative_to(root) / "policy.json"
            ).as_posix(),
            "policy_checksum_sha256": file_checksum(policy_path),
            "published_at": utc_timestamp(published_at).isoformat(),
            "lockbox_target_read_before_publication": False,
            "automated_action_allowed": False,
        }
        _write_json_atomic(staging / "receipt.json", receipt)
        staging.replace(directory)
    except BaseException:
        # A failed staging directory has no authority and is never discovered.
        raise
    return read_eligibility_policy(directory, datastore_root=root)


def read_eligibility_policy(
    directory: Path,
    *,
    datastore_root: Path,
) -> EligibilityPolicyArtifact:
    root = Path(datastore_root).resolve()
    policy_root = (root / "ml" / "option-pricing-eligibility-policies").resolve()
    resolved = Path(directory).resolve()
    if resolved.parent != policy_root:
        raise EligibilityError("Eligibility policy path escapes its immutable root")
    try:
        policy = json.loads((resolved / "policy.json").read_text(encoding="utf-8"))
        receipt = json.loads((resolved / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EligibilityError("Eligibility policy artifact is unreadable") from exc
    if not isinstance(policy, Mapping) or not isinstance(receipt, Mapping):
        raise EligibilityError("Eligibility policy artifact is malformed")
    observed_hash = semantic_metadata_fingerprint(policy)
    expected_policy_path = (resolved.relative_to(root) / "policy.json").as_posix()
    published = pd.to_datetime(receipt.get("published_at"), utc=True, errors="coerce")
    if (
        policy.get("schema_version") != ELIGIBILITY_POLICY_VERSION
        or receipt.get("schema_version") != ELIGIBILITY_POLICY_RECEIPT_VERSION
        or observed_hash != resolved.name
        or receipt.get("policy_hash") != observed_hash
        or receipt.get("policy_path") != expected_policy_path
        or receipt.get("policy_checksum_sha256")
        != file_checksum(resolved / "policy.json")
        or pd.isna(published)
        or receipt.get("lockbox_target_read_before_publication") is not False
        or receipt.get("automated_action_allowed") is not False
    ):
        raise EligibilityError("Eligibility policy receipt verification failed")
    return EligibilityPolicyArtifact(resolved, policy, receipt, observed_hash)


def paired_session_inference(
    values: pd.DataFrame,
    *,
    difference_column: str,
    timestamp_column: str,
    confidence_level: float,
) -> dict[str, object]:
    """One-sided paired inference after equal-weight session blocking."""

    if difference_column not in values or timestamp_column not in values:
        return {
            "status": "NOT_PROVEN",
            "reason": "required paired inference columns are missing",
            "snapshot_count": 0,
            "session_count": 0,
        }
    frame = values.loc[:, [difference_column, timestamp_column]].copy()
    frame[difference_column] = pd.to_numeric(
        frame[difference_column], errors="coerce"
    )
    frame[timestamp_column] = pd.to_datetime(
        frame[timestamp_column], utc=True, errors="coerce"
    )
    frame = frame.dropna()
    if frame.empty:
        return {
            "status": "NOT_PROVEN",
            "reason": "no finite paired observations",
            "snapshot_count": 0,
            "session_count": 0,
        }
    frame["session"] = frame[timestamp_column].dt.tz_convert(
        "America/New_York"
    ).dt.date
    session_values = frame.groupby("session", sort=True)[difference_column].mean()
    session_count = len(session_values)
    mean = float(session_values.mean())
    if session_count < 2:
        lower = None
        standard_error = None
    else:
        standard_error = float(session_values.std(ddof=1) / math.sqrt(session_count))
        critical = float(student_t.ppf(confidence_level, df=session_count - 1))
        lower = mean - critical * standard_error
    return {
        "status": "INFERRED" if lower is not None else "NOT_PROVEN",
        "method": "paired-session-blocked-student-t-v1",
        "confidence_level": confidence_level,
        "snapshot_count": len(frame),
        "session_count": session_count,
        "mean_difference": mean,
        "standard_error": standard_error,
        "lower_confidence_bound": lower,
        "session_values": {
            str(index): float(value) for index, value in session_values.items()
        },
    }


def build_eligibility_report(
    *,
    policy_artifact: EligibilityPolicyArtifact,
    policy: EligibilityPolicy,
    evaluations: pd.DataFrame,
    predictions: pd.DataFrame,
    model_reports: Mapping[str, object],
    lineage_report: Mapping[str, object],
    strategy_report: Mapping[str, object] | None,
    frozen_candidate: Mapping[str, object] | None,
    lockbox_result: Mapping[str, object] | None,
    operational_report: Mapping[str, object] | None,
    generated_at: object,
) -> dict[str, object]:
    """Evaluate every configured route; absent evidence remains visible."""

    fixture_guard = _evidence_lane_guard(
        evaluations,
        predictions,
        model_reports,
        lockbox_result=lockbox_result,
        strategy_report=strategy_report,
    )
    routes = evaluate_required_route_performance(
        policy=policy,
        evaluations=evaluations,
        predictions=predictions,
        model_reports=model_reports,
        include_partitions=True,
        include_prospective=True,
    )

    lineage_pass = bool(
        lineage_report.get("verified")
        and lineage_report.get("schema_version")
        == "option-pricing-lineage-verification-v2"
        and lineage_report.get("stage") == "COMPLETED"
        and lineage_report.get("evidence_kind") == "REAL_RECEIPT_PROVEN"
        and lineage_report.get("fixture_test_evidence") is False
        and fixture_guard["pass"]
    )
    gates = [
        _gate(
            1,
            "lineage_timing_schema_publication",
            lineage_pass,
            {"lineage": dict(lineage_report), "evidence_lane_guard": fixture_guard},
        ),
        _gate_from_routes(
            2,
            "chronological_partitions_and_closed_lockbox",
            routes,
            "partition",
        ),
        _gate_from_routes(3, "bsgp_beats_black_scholes", routes, "black_scholes"),
        _gate_from_routes(
            4,
            "bsgp_beats_constant_residual",
            routes,
            "constant_residual",
        ),
        _gate_from_routes(5, "bsgp_beats_standard_gp", routes, "standard_gp"),
        _gate_from_routes(6, "interval_coverage", routes, "intervals"),
        _gate_from_routes(7, "constraints_and_projection", routes, "constraints"),
        _gate_from_routes(
            8,
            "edge_after_spread_and_monotonicity",
            routes,
            "economic_edge",
        ),
        _strategy_gate(strategy_report, policy),
        _gate_from_routes(10, "prospective_predictions", routes, "prospective"),
    ]
    candidate_status = _candidate_status(
        frozen_candidate, policy_hash=policy_artifact.policy_hash
    )
    lockbox_status = _lockbox_status(
        lockbox_result,
        policy_hash=policy_artifact.policy_hash,
        candidate=frozen_candidate,
    )
    operational_status = _promotion_status(
        operational_report,
        name="operational_readiness",
        policy_hash=policy_artifact.policy_hash,
        generated_at=generated_at,
        maximum_age_hours=policy.maximum_operational_readiness_age_hours,
    )
    all_gates_pass = all(gate["status"] == "PASS" for gate in gates)
    promotions_pass = all(
        value["status"] == "PASS"
        for value in (candidate_status, lockbox_status, operational_status)
    )
    if all_gates_pass and promotions_pass:
        gate_status = "PRODUCTION_ELIGIBLE"
    elif (
        all(gate["status"] == "PASS" for gate in gates[:9])
        and gates[9]["status"] in {"NOT_PROVEN", "FAIL"}
        and candidate_status["status"] == "PASS"
        and operational_status["status"] == "PASS"
    ):
        gate_status = "COLLECTING_PROSPECTIVE_EVIDENCE"
    else:
        gate_status = "NOT_PRODUCTION_ELIGIBLE"
    prospective_summary = _prospective_summary(
        routes,
        generated_at=generated_at,
        policy=policy,
    )
    return {
        "schema_version": ELIGIBILITY_REPORT_VERSION,
        "eligibility_protocol_version": ELIGIBILITY_PROTOCOL_VERSION,
        "generated_at": utc_timestamp(generated_at).isoformat(),
        "eligibility_policy": {
            "policy_hash": policy_artifact.policy_hash,
            "path": str(policy_artifact.directory),
            "receipt_checksum_sha256": file_checksum(
                policy_artifact.directory / "receipt.json"
            ),
        },
        "gate_status": gate_status,
        "automated_action_allowed": False,
        "activation_status": "SEPARATE_OPERATOR_AUTHORIZATION_REQUIRED",
        "gates": gates,
        "routes": routes,
        "prospective_summary": prospective_summary,
        "frozen_candidate": candidate_status,
        "closed_lockbox": lockbox_status,
        "operational_promotion": operational_status,
        "evidence_lanes": fixture_guard["lanes"],
        "limitations": [
            "Eligibility approves only a separately authorized production canary.",
            "Eligibility never enables trading or changes Strategy rankings or orders.",
            "Fixture/test evidence is structurally ineligible for production gates.",
        ],
    }


def _prospective_summary(
    routes: Mapping[str, Mapping[str, object]],
    *,
    generated_at: object,
    policy: EligibilityPolicy,
) -> dict[str, object]:
    route_counts: dict[str, dict[str, object]] = {}
    maximum_missing_sessions = 0
    for symbol, call_put in policy.required_routes:
        name = _route_name(symbol, call_put)
        route = routes.get(name, {})
        prospective = route.get("prospective", {}) if isinstance(route, Mapping) else {}
        prospective = prospective if isinstance(prospective, Mapping) else {}
        completed = int(prospective.get("completed_predictions", 0))
        sessions = int(prospective.get("distinct_sessions", 0))
        missing_sessions = max(
            0, policy.minimum_prospective_sessions_per_route - sessions
        )
        maximum_missing_sessions = max(maximum_missing_sessions, missing_sessions)
        route_counts[name] = {
            "completed_predictions": completed,
            "distinct_sessions": sessions,
            "remaining_predictions": max(
                0, policy.minimum_prospective_predictions_per_route - completed
            ),
            "remaining_sessions": missing_sessions,
            "first_target": prospective.get("first_target"),
            "last_target": prospective.get("last_target"),
            "status": prospective.get("status", "NOT_PROVEN"),
        }

    earliest = None
    if maximum_missing_sessions:
        generated = utc_timestamp(generated_at).tz_convert("America/New_York")
        calendar = xcals.get_calendar("XNYS")
        start = (generated + pd.Timedelta(days=1)).normalize().tz_localize(None)
        end = start + pd.Timedelta(days=max(60, maximum_missing_sessions * 4))
        sessions = calendar.sessions_in_range(start, end)
        if len(sessions) >= maximum_missing_sessions:
            earliest = pd.Timestamp(sessions[maximum_missing_sessions - 1]).date().isoformat()

    return {
        "routes": route_counts,
        "earliest_possible_session_threshold_date": earliest,
        "earliest_date_assumption": (
            "Conservative next-session collection with no backfill; the prediction-count "
            "threshold must also be met through causally committed live predictions."
        ),
    }


def evaluate_required_route_performance(
    *,
    policy: EligibilityPolicy,
    evaluations: pd.DataFrame,
    predictions: pd.DataFrame,
    model_reports: Mapping[str, object],
    include_partitions: bool,
    include_prospective: bool,
) -> dict[str, dict[str, object]]:
    """Evaluate the fixed six-route cohort without dropping absent routes."""

    routes: dict[str, dict[str, object]] = {}
    route_reports = model_reports.get("model_reports", model_reports)
    route_reports = route_reports if isinstance(route_reports, Mapping) else {}
    for symbol, call_put in policy.required_routes:
        name = _route_name(symbol, call_put)
        raw = route_reports.get(name)
        report = raw if isinstance(raw, Mapping) else {}
        evidence = {
            "black_scholes": _route_comparator_evidence(
                report, "black_scholes", policy
            ),
            "constant_residual": _route_comparator_evidence(
                report, "constant_residual", policy
            ),
            "standard_gp": _route_comparator_evidence(
                report, "standard_gp", policy
            ),
            "intervals": _route_interval_evidence(report, policy),
            "constraints": _route_constraint_evidence(
                predictions, symbol=symbol, call_put=call_put, policy=policy
            ),
            "economic_edge": _route_economic_edge_evidence(
                evaluations, symbol=symbol, call_put=call_put, policy=policy
            ),
        }
        if include_partitions:
            evidence["partition"] = _route_partition_evidence(report, policy)
        if include_prospective:
            evidence["prospective"] = _route_prospective_evidence(
                evaluations, symbol=symbol, call_put=call_put, policy=policy
            )
        routes[name] = evidence
    return routes


def publish_eligibility_report(
    datastore_root: Path,
    *,
    report: Mapping[str, object],
    pricing_run: Path,
    published_at: object,
) -> EligibilityReportArtifact:
    """Publish a post-receipt report and atomically point to it."""

    root = Path(datastore_root).resolve()
    run = Path(pricing_run).resolve()
    allowed_run_root = (root / "ml" / "option-pricing-runs").resolve()
    if run.parent != allowed_run_root:
        raise EligibilityError("Eligibility report references a Pricing run outside authority")
    policy_reference = report.get("eligibility_policy")
    policy_reference = (
        policy_reference if isinstance(policy_reference, Mapping) else {}
    )
    raw_policy_path = policy_reference.get("path")
    if not isinstance(raw_policy_path, str) or not raw_policy_path:
        raise EligibilityError("Eligibility report has no policy reference")
    from ml.option_pricing.lineage import verify_completed_option_pricing_lineage

    policy_path = Path(raw_policy_path)
    if not policy_path.is_absolute():
        policy_path = root / policy_path
    policy_artifact = read_eligibility_policy(policy_path, datastore_root=root)
    final_lineage = verify_completed_option_pricing_lineage(
        root,
        run_directory=run,
        policy_artifact=policy_artifact,
    )
    gates = report.get("gates")
    gate_one = next(
        (
            gate
            for gate in gates
            if isinstance(gate, Mapping) and gate.get("number") == 1
        ),
        None,
    ) if isinstance(gates, Sequence) and not isinstance(gates, (str, bytes)) else None
    gate_one_evidence = (
        gate_one.get("evidence") if isinstance(gate_one, Mapping) else None
    )
    recorded_lineage = (
        gate_one_evidence.get("lineage")
        if isinstance(gate_one_evidence, Mapping)
        else None
    )
    if (
        not final_lineage.get("verified")
        or recorded_lineage != final_lineage
        or gate_one.get("status") != "PASS"
    ):
        raise EligibilityError(
            "Final staged publication lineage differs from gate-one evidence"
        )
    if report.get("gate_status") == "PRODUCTION_ELIGIBLE":
        _verify_production_report_evidence(
            root,
            run=run,
            report=report,
            policy_artifact=policy_artifact,
            final_lineage=final_lineage,
        )
    reports_root = root / "ml" / "option-pricing-eligibility-reports"
    reports_root.mkdir(parents=True, exist_ok=True)
    timestamp = utc_timestamp(published_at)
    base = timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    directory = reports_root / base
    suffix = 2
    while directory.exists():
        directory = reports_root / f"{base}-{suffix}"
        suffix += 1
    staging = reports_root / f".{directory.name}.tmp-{os.getpid()}"
    staging.mkdir()
    try:
        report_path = staging / "eligibility-report.json"
        _write_json_atomic(report_path, report)
        receipt = {
            "schema_version": ELIGIBILITY_REPORT_RECEIPT_VERSION,
            "report_path": (
                directory.relative_to(root) / report_path.name
            ).as_posix(),
            "report_checksum_sha256": file_checksum(report_path),
            "pricing_run_path": run.relative_to(root).as_posix(),
            "pricing_manifest_checksum_sha256": file_checksum(run / "manifest.json"),
            "pricing_receipt_checksum_sha256": file_checksum(run / "publication.json"),
            "published_at": timestamp.isoformat(),
            "gate_status": report.get("gate_status"),
            "automated_action_allowed": False,
        }
        _write_json_atomic(staging / "receipt.json", receipt)
        staging.replace(directory)
    except BaseException:
        raise
    pointer = {
        "schema_version": ELIGIBILITY_REPORT_POINTER_VERSION,
        "current": {
            **receipt,
            "receipt_checksum_sha256": file_checksum(directory / "receipt.json"),
        },
    }
    _write_json_atomic(
        root / "ml" / "option-pricing-eligibility-latest" / "report.json",
        pointer,
    )
    return read_current_eligibility_report(root)


def _verify_production_report_evidence(
    root: Path,
    *,
    run: Path,
    report: Mapping[str, object],
    policy_artifact: EligibilityPolicyArtifact,
    final_lineage: Mapping[str, object],
) -> None:
    """Rebuild a production claim only from independently verified current artifacts."""

    from ml.option_pricing.candidate import read_current_candidate
    from ml.option_pricing.lockbox import read_lockbox_result
    from ml.option_pricing.operations import read_current_operational_readiness
    from ml.option_pricing.publication import OPTION_PRICING_REPORT_NAME
    from ml.option_pricing.strategy_outcomes import (
        read_current_strategy_outcome_evidence,
    )

    policy_values = policy_artifact.policy.get("policy_values")
    if not isinstance(policy_values, Mapping):
        raise EligibilityError("Production report policy values are unavailable")
    try:
        policy = EligibilityPolicy(**dict(policy_values))
        evaluations = pd.read_parquet(run / "pricing-evaluations.parquet")
        predictions = pd.read_parquet(run / "pricing-predictions.parquet")
        model_reports = json.loads(
            (run / OPTION_PRICING_REPORT_NAME).read_text(encoding="utf-8")
        )
        candidate = read_current_candidate(root)
        strategy = read_current_strategy_outcome_evidence(root)
        operational = read_current_operational_readiness(root)
        lockbox = (
            read_lockbox_result(root, candidate_id=str(candidate["candidate_id"]))
            if candidate is not None
            else None
        )
    except Exception as exc:
        raise EligibilityError(
            "Production report source evidence did not independently verify"
        ) from exc
    rebuilt = build_eligibility_report(
        policy_artifact=policy_artifact,
        policy=policy,
        evaluations=evaluations,
        predictions=predictions,
        model_reports=model_reports,
        lineage_report=final_lineage,
        strategy_report=strategy,
        frozen_candidate=candidate,
        lockbox_result=lockbox,
        operational_report=operational,
        generated_at=report.get("generated_at"),
    )
    claimed = dict(report)
    claimed.pop("closed_lockbox_inventory", None)
    if (
        rebuilt.get("gate_status") != "PRODUCTION_ELIGIBLE"
        or semantic_metadata_fingerprint(rebuilt)
        != semantic_metadata_fingerprint(claimed)
    ):
        raise EligibilityError(
            "Production claim was not reproduced from verified current evidence"
        )


def read_current_eligibility_report(datastore_root: Path) -> EligibilityReportArtifact:
    root = Path(datastore_root).resolve()
    pointer_path = (
        root / "ml" / "option-pricing-eligibility-latest" / "report.json"
    )
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EligibilityError("Eligibility report pointer is unreadable") from exc
    if (
        not isinstance(pointer, Mapping)
        or pointer.get("schema_version") != ELIGIBILITY_REPORT_POINTER_VERSION
        or not isinstance(pointer.get("current"), Mapping)
    ):
        raise EligibilityError("Eligibility report pointer is malformed")
    current = pointer["current"]
    raw_report_path = current.get("report_path")
    if not isinstance(raw_report_path, str) or not raw_report_path:
        raise EligibilityError("Eligibility report path is invalid")
    relative = Path(raw_report_path)
    report_path = (root / relative).resolve()
    allowed = (root / "ml" / "option-pricing-eligibility-reports").resolve()
    if relative.is_absolute() or report_path.parent.parent != allowed:
        raise EligibilityError("Eligibility report path escapes its immutable root")
    directory = report_path.parent
    receipt_path = directory / "receipt.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EligibilityError("Eligibility report artifact is unreadable") from exc
    expected_current = {
        **receipt,
        "receipt_checksum_sha256": file_checksum(receipt_path),
    }
    pricing_run = (root / str(receipt.get("pricing_run_path", ""))).resolve()
    pricing_root = (root / "ml" / "option-pricing-runs").resolve()
    if pricing_run.parent != pricing_root:
        raise EligibilityError("Eligibility report Pricing reference escapes authority")
    if (
        not isinstance(report, Mapping)
        or not isinstance(receipt, Mapping)
        or report.get("schema_version") != ELIGIBILITY_REPORT_VERSION
        or receipt.get("schema_version") != ELIGIBILITY_REPORT_RECEIPT_VERSION
        or dict(current) != expected_current
        or receipt.get("report_checksum_sha256") != file_checksum(report_path)
        or receipt.get("pricing_manifest_checksum_sha256")
        != file_checksum(pricing_run / "manifest.json")
        or receipt.get("pricing_receipt_checksum_sha256")
        != file_checksum(pricing_run / "publication.json")
        or report.get("gate_status") != receipt.get("gate_status")
        or report.get("automated_action_allowed") is not False
        or receipt.get("automated_action_allowed") is not False
    ):
        raise EligibilityError("Eligibility report receipt verification failed")
    return EligibilityReportArtifact(directory, report, receipt)


def _route_partition_evidence(
    report: Mapping[str, object], policy: EligibilityPolicy
) -> dict[str, object]:
    partition = report.get("partition_contract")
    partition = partition if isinstance(partition, Mapping) else {}
    counts = partition.get("cluster_counts")
    counts = counts if isinstance(counts, Mapping) else {}
    required = {
        "train": policy.minimum_train_clusters,
        "calibration": policy.calibration_clusters,
        "assessment": policy.assessment_clusters,
        "lockbox": policy.lockbox_clusters,
    }
    observed = {name: _finite(counts.get(name)) for name in required}
    provider = report.get("source_provider")
    passed = bool(
        report.get("status") == "MODEL_FIT"
        and provider == "databento-opra"
        and all(
            observed[name] is not None and observed[name] >= minimum
            for name, minimum in required.items()
        )
        and _finite(partition.get("calendar_span_months")) is not None
        and float(partition["calendar_span_months"]) >= policy.minimum_calendar_months
        and partition.get("lockbox_status") == "CLOSED_UNTOUCHED_UNSCORED"
        and partition.get("lockbox_target_values_reported") is False
    )
    return {
        "status": "PASS" if passed else "NOT_PROVEN",
        "source_provider": provider,
        "observed_cluster_counts": observed,
        "required_cluster_counts": required,
        "calendar_span_months": partition.get("calendar_span_months"),
        "lockbox_status": partition.get("lockbox_status"),
    }


def _route_comparator_evidence(
    report: Mapping[str, object], comparator: str, policy: EligibilityPolicy
) -> dict[str, object]:
    assessment = report.get("assessment_metrics")
    assessment = assessment if isinstance(assessment, Mapping) else {}
    raw = assessment.get("paired_snapshot_losses")
    records = raw if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else []
    rows: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        bsgp = _finite(record.get("bsgp_normalized_squared_error"))
        other = _finite(record.get(f"{comparator}_normalized_squared_error"))
        if bsgp is None or other is None:
            continue
        rows.append(
            {
                "target_snapshot_for": record.get("target_snapshot_for"),
                "paired_improvement": other - bsgp,
            }
        )
    inference = paired_session_inference(
        pd.DataFrame(rows),
        difference_column="paired_improvement",
        timestamp_column="target_snapshot_for",
        confidence_level=policy.confidence_level,
    )
    lower = _finite(inference.get("lower_confidence_bound"))
    passed = bool(
        inference.get("snapshot_count", 0) >= policy.minimum_comparison_snapshots
        and inference.get("session_count", 0) >= policy.minimum_comparison_sessions
        and _finite(inference.get("mean_difference")) is not None
        and float(inference["mean_difference"]) > 0.0
        and lower is not None
        and lower > 0.0
    )
    return {
        "status": "PASS" if passed else "NOT_PROVEN",
        "comparator": comparator,
        "input_snapshot_records": len(records),
        "excluded_incomplete_records": len(records) - len(rows),
        "minimum_snapshots": policy.minimum_comparison_snapshots,
        "minimum_sessions": policy.minimum_comparison_sessions,
        "inference": inference,
    }


def _route_interval_evidence(
    report: Mapping[str, object], policy: EligibilityPolicy
) -> dict[str, object]:
    assessment = report.get("assessment_metrics")
    assessment = assessment if isinstance(assessment, Mapping) else {}
    raw = assessment.get("paired_snapshot_losses")
    records = [value for value in raw if isinstance(value, Mapping)] if isinstance(raw, list) else []
    required = {
        "target_snapshot_for",
        "interval_80_coverage",
        "interval_95_coverage",
    }
    complete_records = bool(records) and all(
        required.issubset(record) for record in records
    )
    if complete_records:
        frame = pd.DataFrame(records)
        snapshots = len(frame)
        timestamps = pd.to_datetime(
            frame.get("target_snapshot_for"), utc=True, errors="coerce"
        )
        sessions = int(
            timestamps.dt.tz_convert("America/New_York").dt.date.nunique()
        ) if timestamps.notna().any() else 0
        observed80 = pd.to_numeric(frame["interval_80_coverage"], errors="coerce")
        observed95 = pd.to_numeric(frame["interval_95_coverage"], errors="coerce")
        complete_records = bool(
            timestamps.notna().all()
            and observed80.notna().all()
            and observed95.notna().all()
        )
        coverage80 = _finite(observed80.mean())
        coverage95 = _finite(observed95.mean())
    else:
        snapshots = sessions = 0
        coverage80 = coverage95 = None
    passed = bool(
        complete_records
        and snapshots >= policy.minimum_comparison_snapshots
        and sessions >= policy.minimum_comparison_sessions
        and coverage80 is not None
        and policy.interval_80_minimum <= coverage80 <= policy.interval_80_maximum
        and coverage95 is not None
        and policy.interval_95_minimum <= coverage95 <= policy.interval_95_maximum
    )
    return {
        "status": "PASS" if passed else "NOT_PROVEN",
        "snapshot_count": snapshots,
        "session_count": sessions,
        "coverage_80": coverage80,
        "coverage_95": coverage95,
        "required_80": [policy.interval_80_minimum, policy.interval_80_maximum],
        "required_95": [policy.interval_95_minimum, policy.interval_95_maximum],
        "complete_snapshot_records": complete_records,
    }


def _route_constraint_evidence(
    predictions: pd.DataFrame,
    *,
    symbol: str,
    call_put: str,
    policy: EligibilityPolicy,
) -> dict[str, object]:
    if predictions.empty:
        return {"status": "NOT_PROVEN", "prediction_rows": 0}
    route = predictions.loc[
        predictions.get("symbol", pd.Series(index=predictions.index, dtype="string"))
        .astype("string")
        .str.upper()
        .eq(symbol)
        & predictions.get(
            "call_put", pd.Series(index=predictions.index, dtype="string")
        )
        .astype("string")
        .str.upper()
        .eq(call_put)
        & predictions.get(
            "prediction_mode", pd.Series(index=predictions.index, dtype="string")
        )
        .astype("string")
        .str.upper()
        .eq("OFFLINE")
        & predictions.get(
            "source_provider", pd.Series(index=predictions.index, dtype="string")
        )
        .astype("string")
        .str.lower()
        .eq("databento-opra")
    ].copy()
    route = route.loc[
        route.get("prediction_status", pd.Series(index=route.index)).isin(
            ("AVAILABLE", "CREATED")
        )
    ]
    if route.empty:
        return {"status": "NOT_PROVEN", "prediction_rows": 0}
    required = {
        "projection_correction",
        "underlying_price",
        "constrained_bound_violation",
        "constrained_monotonicity_violation",
        "constrained_convexity_violation",
        "target_snapshot_for",
    }
    if missing := sorted(required.difference(route.columns)):
        return {
            "status": "NOT_PROVEN",
            "prediction_rows": len(route),
            "reason": "required constraint columns are missing: " + ", ".join(missing),
        }
    violation = pd.Series(False, index=route.index)
    for column in (
        "constrained_bound_violation",
        "constrained_monotonicity_violation",
        "constrained_convexity_violation",
    ):
        if column not in route or route[column].isna().any():
            return {
                "status": "NOT_PROVEN",
                "prediction_rows": len(route),
                "reason": f"{column} is absent or unknown",
            }
        violation |= route[column].astype(bool)
    normalized = pd.to_numeric(
        route.get("projection_correction"), errors="coerce"
    ).abs() / pd.to_numeric(route.get("underlying_price"), errors="coerce").replace(
        0.0, np.nan
    )
    median = _finite(normalized.median())
    rate = float(violation.mean())
    timestamps = pd.to_datetime(
        route.get("target_snapshot_for"), utc=True, errors="coerce"
    )
    snapshots = int(timestamps.nunique()) if timestamps is not None else 0
    sessions = int(
        timestamps.dt.tz_convert("America/New_York").dt.date.nunique()
    ) if timestamps is not None and timestamps.notna().any() else 0
    passed = bool(
        snapshots >= policy.minimum_constraint_snapshots
        and sessions >= policy.minimum_constraint_sessions
        and rate <= policy.maximum_constrained_violation_rate
        and median is not None
        and median <= policy.maximum_median_normalized_projection
    )
    return {
        "status": "PASS" if passed else "FAIL",
        "prediction_rows": len(route),
        "snapshot_count": snapshots,
        "session_count": sessions,
        "minimum_snapshots": policy.minimum_constraint_snapshots,
        "minimum_sessions": policy.minimum_constraint_sessions,
        "constrained_violation_rate": rate,
        "maximum_constrained_violation_rate": (
            policy.maximum_constrained_violation_rate
        ),
        "median_normalized_projection": median,
        "maximum_median_normalized_projection": (
            policy.maximum_median_normalized_projection
        ),
    }


def _route_economic_edge_evidence(
    evaluations: pd.DataFrame,
    *,
    symbol: str,
    call_put: str,
    policy: EligibilityPolicy,
) -> dict[str, object]:
    route = _real_offline_route(evaluations, symbol=symbol, call_put=call_put)
    if route.empty:
        return {
            "status": "NOT_PROVEN",
            "completed_rows": 0,
            "snapshot_denominator": 0,
            "session_denominator": 0,
            "contract_denominator": 0,
            "buckets": _empty_edge_buckets(policy),
        }
    for column in (
        "constrained_fair_value",
        "black_scholes_price",
        "observed_mid",
        "bid_ask_spread",
        "multiplier",
        "target_snapshot_for",
        "contract_symbol",
    ):
        if column not in route:
            return {
                "status": "NOT_PROVEN",
                "completed_rows": len(route),
                "reason": f"{column} is missing",
                "buckets": _empty_edge_buckets(policy),
            }
    fair = pd.to_numeric(route["constrained_fair_value"], errors="coerce")
    baseline = pd.to_numeric(route["black_scholes_price"], errors="coerce")
    observed = pd.to_numeric(route["observed_mid"], errors="coerce")
    spread = pd.to_numeric(route["bid_ask_spread"], errors="coerce")
    multiplier = pd.to_numeric(route["multiplier"], errors="coerce")
    half_spread = spread / 2.0
    signal = fair - baseline
    direction = np.sign(signal)
    route["predicted_edge_in_half_spreads"] = signal / half_spread.replace(0.0, np.nan)
    route["realized_value_after_friction"] = (
        direction * (observed - baseline) * multiplier
        - half_spread * multiplier
        - policy.per_contract_fee_usd
    )
    input_rows = len(route)
    zero_signal_rows = int(direction.eq(0).sum())
    finite = (
        np.isfinite(route["predicted_edge_in_half_spreads"])
        & np.isfinite(route["realized_value_after_friction"])
    )
    nonfinite_rows = int((~finite).sum())
    route = route.loc[
        finite & direction.ne(0)
    ].copy()
    if route.empty:
        return {
            "status": "NOT_PROVEN",
            "completed_rows": 0,
            "input_completed_rows": input_rows,
            "excluded_zero_signal_rows": zero_signal_rows,
            "excluded_nonfinite_or_zero_spread_rows": nonfinite_rows,
            "snapshot_denominator": 0,
            "session_denominator": 0,
            "contract_denominator": 0,
            "buckets": _empty_edge_buckets(policy),
        }
    route["session"] = pd.to_datetime(
        route["target_snapshot_for"], utc=True, errors="coerce"
    ).dt.tz_convert("America/New_York").dt.date
    snapshot_values = (
        route.groupby("target_snapshot_for", sort=True)
        .agg(
            predicted_edge_in_half_spreads=(
                "predicted_edge_in_half_spreads",
                "mean",
            ),
            realized_value_after_friction=(
                "realized_value_after_friction",
                "mean",
            ),
            session=("session", "first"),
            contracts=("contract_symbol", "nunique"),
        )
        .reset_index()
    )
    inference = paired_session_inference(
        snapshot_values,
        difference_column="realized_value_after_friction",
        timestamp_column="target_snapshot_for",
        confidence_level=policy.confidence_level,
    )
    bins = pd.cut(
        snapshot_values["predicted_edge_in_half_spreads"],
        bins=policy.edge_bucket_boundaries,
        include_lowest=True,
    )
    bucket_reports: dict[str, object] = {}
    ordered_means: list[float] = []
    bucket_minima_pass = True
    categories = list(bins.cat.categories)
    for bucket in categories:
        group = snapshot_values.loc[bins.eq(bucket)]
        mean = (
            float(group["realized_value_after_friction"].mean())
            if not group.empty
            else None
        )
        sessions = int(group["session"].nunique())
        snapshots = len(group)
        bucket_pass = bool(
            snapshots >= policy.minimum_edge_bucket_snapshots
            and sessions >= policy.minimum_edge_bucket_sessions
        )
        bucket_minima_pass &= bucket_pass
        if mean is not None:
            ordered_means.append(mean)
        bucket_reports[str(bucket)] = {
            "snapshot_count": snapshots,
            "session_count": sessions,
            "contract_denominator": int(group["contracts"].sum()),
            "mean_realized_value_after_friction_usd": mean,
            "minimum_size_pass": bucket_pass,
        }
    monotonic = len(ordered_means) >= 2 and all(
        right >= left
        for left, right in zip(ordered_means, ordered_means[1:])
    )
    lower = _finite(inference.get("lower_confidence_bound"))
    passed = bool(
        len(snapshot_values) >= policy.minimum_edge_cohort_snapshots
        and snapshot_values["session"].nunique()
        >= policy.minimum_edge_cohort_sessions
        and lower is not None
        and lower > policy.minimum_net_realized_value_per_contract
        and bucket_minima_pass
        and monotonic
    )
    return {
        "status": "PASS" if passed else "NOT_PROVEN",
        "completed_rows": len(route),
        "input_completed_rows": input_rows,
        "excluded_zero_signal_rows": zero_signal_rows,
        "excluded_nonfinite_or_zero_spread_rows": nonfinite_rows,
        "snapshot_denominator": len(snapshot_values),
        "session_denominator": int(snapshot_values["session"].nunique()),
        "contract_denominator": int(snapshot_values["contracts"].sum()),
        "fees_usd_per_contract": policy.per_contract_fee_usd,
        "inference": inference,
        "bucket_monotonic": monotonic,
        "bucket_minima_pass": bucket_minima_pass,
        "buckets": bucket_reports,
        "snapshot_evidence": [
            {
                "target_snapshot_for": pd.Timestamp(row.target_snapshot_for).isoformat(),
                "predicted_edge_in_half_spreads": float(
                    row.predicted_edge_in_half_spreads
                ),
                "realized_value_after_friction_usd": float(
                    row.realized_value_after_friction
                ),
                "session": str(row.session),
                "contract_count": int(row.contracts),
            }
            for row in snapshot_values.itertuples(index=False)
        ],
    }


def _empty_edge_buckets(policy: EligibilityPolicy) -> dict[str, object]:
    probe = pd.Series([0.0])
    categories = pd.cut(
        probe,
        bins=policy.edge_bucket_boundaries,
        include_lowest=True,
    ).cat.categories
    return {
        str(bucket): {
            "snapshot_count": 0,
            "session_count": 0,
            "contract_denominator": 0,
            "mean_realized_value_after_friction_usd": None,
            "minimum_size_pass": False,
        }
        for bucket in categories
    }


def _route_prospective_evidence(
    evaluations: pd.DataFrame,
    *,
    symbol: str,
    call_put: str,
    policy: EligibilityPolicy,
) -> dict[str, object]:
    if evaluations.empty:
        return {
            "status": "NOT_PROVEN",
            "completed_predictions": 0,
            "distinct_sessions": 0,
            "minimum_completed_predictions": (
                policy.minimum_prospective_predictions_per_route
            ),
            "minimum_distinct_sessions": policy.minimum_prospective_sessions_per_route,
            "first_target": None,
            "last_target": None,
        }
    eligible = evaluations.get(
        "prospective_eligible", pd.Series(False, index=evaluations.index)
    ).fillna(False).astype(bool)
    route = evaluations.loc[
        eligible
        & evaluations.get(
            "symbol", pd.Series(index=evaluations.index, dtype="string")
        )
        .astype("string")
        .str.upper()
        .eq(symbol)
        & evaluations.get(
            "call_put", pd.Series(index=evaluations.index, dtype="string")
        )
        .astype("string")
        .str.upper()
        .eq(call_put)
        & evaluations.get(
            "source_provider", pd.Series(index=evaluations.index, dtype="string")
        )
        .astype("string")
        .str.lower()
        .eq("schwab")
        & evaluations.get(
            "prediction_mode", pd.Series(index=evaluations.index, dtype="string")
        )
        .astype("string")
        .str.upper()
        .eq("LIVE")
    ].copy()
    required = {
        "symbol",
        "target_snapshot_for",
        "contract_symbol",
        "prediction_created_at",
    }
    if not route.empty and (missing := sorted(required.difference(route.columns))):
        return {
            "status": "NOT_PROVEN",
            "completed_predictions": 0,
            "distinct_sessions": 0,
            "minimum_completed_predictions": (
                policy.minimum_prospective_predictions_per_route
            ),
            "minimum_distinct_sessions": policy.minimum_prospective_sessions_per_route,
            "first_target": None,
            "last_target": None,
            "reason": "required prospective columns are missing: " + ", ".join(missing),
        }
    route = route.sort_values("prediction_created_at", kind="stable").drop_duplicates(
        ["symbol", "target_snapshot_for", "contract_symbol"], keep="first"
    ) if not route.empty else route
    timestamps = pd.to_datetime(
        route.get("target_snapshot_for"), utc=True, errors="coerce"
    )
    sessions = int(
        timestamps.dt.tz_convert("America/New_York").dt.date.nunique()
    ) if timestamps.notna().any() else 0
    passed = bool(
        len(route) >= policy.minimum_prospective_predictions_per_route
        and sessions >= policy.minimum_prospective_sessions_per_route
    )
    return {
        "status": "PASS" if passed else "NOT_PROVEN",
        "completed_predictions": len(route),
        "distinct_sessions": sessions,
        "minimum_completed_predictions": (
            policy.minimum_prospective_predictions_per_route
        ),
        "minimum_distinct_sessions": policy.minimum_prospective_sessions_per_route,
        "first_target": timestamps.min().isoformat() if timestamps.notna().any() else None,
        "last_target": timestamps.max().isoformat() if timestamps.notna().any() else None,
    }


def _strategy_gate(
    report: Mapping[str, object] | None, policy: EligibilityPolicy
) -> dict[str, object]:
    evidence = dict(report or {})
    routes = evidence.get("routes")
    routes = routes if isinstance(routes, Mapping) else {}
    required_route_names = {
        _route_name(symbol, call_put) for symbol, call_put in policy.required_routes
    }
    routes_pass = set(routes) == required_route_names and all(
        isinstance(value, Mapping)
        and value.get("status") == "PASS"
        and int(value.get("paired_candidate_count", 0))
        >= policy.minimum_strategy_pairs
        and int(value.get("distinct_sessions", 0))
        >= policy.minimum_strategy_sessions
        for value in routes.values()
    )
    passed = bool(
        evidence.get("status") == "PASS"
        and evidence.get("evidence_kind") == "REAL_RECEIPT_PROVEN"
        and evidence.get("same_candidate_cohort") is True
        and evidence.get("rankings_changed") is False
        and evidence.get("order_construction_changed") is False
        and evidence.get("automated_action_allowed") is False
        and int(evidence.get("paired_candidate_count", 0))
        >= policy.minimum_strategy_pairs
        and int(evidence.get("distinct_sessions", 0))
        >= policy.minimum_strategy_sessions
        and _finite(evidence.get("lower_confidence_bound_usd")) is not None
        and float(evidence["lower_confidence_bound_usd"])
        > policy.minimum_strategy_improvement_usd
        and _finite(evidence.get("uncertainty_coverage")) is not None
        and policy.strategy_uncertainty_coverage_minimum
        <= float(evidence["uncertainty_coverage"])
        <= policy.strategy_uncertainty_coverage_maximum
        and routes_pass
    )
    return _gate(9, "strategy_shadow_improves_prior", passed if report else None, evidence)


def _evidence_lane_guard(
    evaluations: pd.DataFrame,
    predictions: pd.DataFrame,
    model_reports: Mapping[str, object],
    *,
    lockbox_result: Mapping[str, object] | None,
    strategy_report: Mapping[str, object] | None,
) -> dict[str, object]:
    frames = [frame for frame in (evaluations, predictions) if not frame.empty]
    providers: set[str] = set()
    fixtures: set[str] = set()
    for frame in frames:
        if "source_provider" not in frame:
            continue
        observed = {
            str(value).strip().lower()
            for value in frame["source_provider"].dropna().unique()
        }
        providers.update(observed)
        fixtures.update(observed.intersection(_FIXTURE_PROVIDERS))
    report_fixture = False
    route_reports = model_reports.get("model_reports", model_reports)
    if isinstance(route_reports, Mapping):
        report_fixture = any(
            isinstance(value, Mapping)
            and str(value.get("evidence_kind", "")).upper().startswith("FIXTURE")
            for value in route_reports.values()
        )
    offline = set()
    prospective = set()
    for frame in frames:
        if "source_provider" not in frame or "prediction_mode" not in frame:
            continue
        modes = frame["prediction_mode"].astype("string").str.upper()
        provider_values = frame["source_provider"].astype("string").str.lower()
        offline.update(provider_values.loc[modes.eq("OFFLINE")].dropna().unique())
        prospective.update(provider_values.loc[modes.eq("LIVE")].dropna().unique())
    offline_pass = not offline or offline == {"databento-opra"}
    prospective_pass = not prospective or prospective == {"schwab"}
    passed = not fixtures and not report_fixture and offline_pass and prospective_pass
    return {
        "pass": passed,
        "fixture_providers": sorted(fixtures),
        "all_observed_providers": sorted(providers),
        "lanes": {
            "OFFLINE_TRAIN_CALIBRATION": {
                "providers": sorted(offline),
                "isolated": offline_pass,
            },
            "UNTOUCHED_OFFLINE_ASSESSMENT": {
                "providers": sorted(offline),
                "isolated": offline_pass,
            },
            "CLOSED_LOCKBOX": {
                "opened": lockbox_result is not None,
                "status": (
                    lockbox_result.get("status")
                    if isinstance(lockbox_result, Mapping)
                    else "CLOSED_UNTOUCHED_UNSCORED"
                ),
                "counted_elsewhere": False,
            },
            "PROSPECTIVE_SCHWAB": {
                "providers": sorted(prospective),
                "isolated": prospective_pass,
            },
            "STRATEGY_SHADOW_OUTCOMES": {
                "verified_separately": strategy_report is not None,
                "status": (
                    strategy_report.get("status")
                    if isinstance(strategy_report, Mapping)
                    else "NOT_PROVEN"
                ),
            },
            "FIXTURE_TEST_ONLY": {
                "providers": sorted(fixtures),
                "production_eligible": False,
            },
        },
    }


def _real_offline_route(
    evaluations: pd.DataFrame, *, symbol: str, call_put: str
) -> pd.DataFrame:
    if evaluations.empty:
        return evaluations.copy()
    return evaluations.loc[
        evaluations.get(
            "evaluation_status", pd.Series(index=evaluations.index, dtype="string")
        ).isin(("EVALUATED", "COMPLETE"))
        & evaluations.get(
            "symbol", pd.Series(index=evaluations.index, dtype="string")
        )
        .astype("string")
        .str.upper()
        .eq(symbol)
        & evaluations.get(
            "call_put", pd.Series(index=evaluations.index, dtype="string")
        )
        .astype("string")
        .str.upper()
        .eq(call_put)
        & evaluations.get(
            "prediction_mode", pd.Series(index=evaluations.index, dtype="string")
        )
        .astype("string")
        .str.upper()
        .eq("OFFLINE")
        & evaluations.get(
            "source_provider", pd.Series(index=evaluations.index, dtype="string")
        )
        .astype("string")
        .str.lower()
        .eq("databento-opra")
    ].copy()


def _candidate_status(
    candidate: Mapping[str, object] | None, *, policy_hash: str
) -> dict[str, object]:
    evidence = dict(candidate or {})
    passed = bool(
        evidence.get("status") == "FROZEN"
        and evidence.get("eligibility_policy_hash") == policy_hash
        and evidence.get("source_evidence_kind") == "REAL_RECEIPT_PROVEN"
        and evidence.get("production_evidence_eligible") is True
        and isinstance(evidence.get("fresh_future_lockbox"), Mapping)
        and evidence["fresh_future_lockbox"].get("status") == "PASS"
        and evidence.get("retraining_allowed") is False
        and evidence.get("hyperparameter_changes_allowed") is False
        and evidence.get("permanently_invalidated") is False
    )
    return {
        "status": "PASS" if passed else "NOT_PROVEN",
        "evidence": evidence,
    }


def _lockbox_status(
    result: Mapping[str, object] | None,
    *,
    policy_hash: str,
    candidate: Mapping[str, object] | None,
) -> dict[str, object]:
    evidence = dict(result or {})
    candidate_id = candidate.get("candidate_id") if candidate else None
    passed = bool(
        evidence.get("status") == "PASS"
        and evidence.get("one_time_score") is True
        and evidence.get("evidence_kind") == "REAL_RECEIPT_PROVEN"
        and evidence.get("production_evidence_eligible") is True
        and evidence.get("eligibility_policy_hash") == policy_hash
        and evidence.get("candidate_id") == candidate_id
        and evidence.get("all_required_routes_pass") is True
        and evidence.get("all_required_buckets_pass") is True
    )
    return {
        "status": "PASS" if passed else "NOT_PROVEN",
        "closed_lockbox_scored": bool(result),
        "evidence": evidence,
    }


def _promotion_status(
    report: Mapping[str, object] | None,
    *,
    name: str,
    policy_hash: str,
    generated_at: object,
    maximum_age_hours: int,
) -> dict[str, object]:
    evidence = dict(report or {})
    checked = pd.to_datetime(evidence.get("checked_at"), utc=True, errors="coerce")
    generated = utc_timestamp(generated_at)
    cli = evidence.get("cli_smoke")
    cli = cli if isinstance(cli, Mapping) else {}
    required_checks = (
        "dependency_contract",
        "configuration",
        "capacity_and_retention",
        "pip_check",
        "publication_and_benchmark",
    )
    passed = bool(
        evidence.get("status") == "PASS"
        and evidence.get("automated_action_allowed") is False
        and evidence.get("reverified_before_publication") is True
        and evidence.get("eligibility_policy_hash") == policy_hash
        and all(
            isinstance(evidence.get(check), Mapping)
            and evidence[check].get("status") == "PASS"
            for check in required_checks
        )
        and len(cli) == 4
        and all(
            isinstance(value, Mapping) and value.get("status") == "PASS"
            for value in cli.values()
        )
        and evidence.get("startup_shutdown_crash_recovery_documented") is True
        and not pd.isna(checked)
        and checked <= generated
        and generated - checked <= pd.Timedelta(hours=maximum_age_hours)
    )
    return {
        "name": name,
        "status": "PASS" if passed else "NOT_PROVEN",
        "evidence": evidence,
    }


def _gate_from_routes(
    number: int,
    name: str,
    routes: Mapping[str, Mapping[str, object]],
    evidence_key: str,
) -> dict[str, object]:
    evidence = {
        route: dict(details.get(evidence_key, {}))
        for route, details in routes.items()
    }
    statuses = [value.get("status") for value in evidence.values()]
    passed: bool | None
    if statuses and all(value == "PASS" for value in statuses):
        passed = True
    elif any(value == "FAIL" for value in statuses):
        passed = False
    else:
        passed = None
    return _gate(number, name, passed, evidence)


def _gate(
    number: int, name: str, passed: bool | None, evidence: object
) -> dict[str, object]:
    return {
        "number": number,
        "name": name,
        "status": "PASS" if passed is True else "FAIL" if passed is False else "NOT_PROVEN",
        "evidence": evidence,
    }


def _installed_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    lock_path = Path(__file__).resolve().parents[2] / "requirements-ml-runtime.lock"
    names = []
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        token = line.strip()
        if token and not token.startswith("#") and "==" in token:
            names.append(token.split("==", 1)[0].strip())
    for name in sorted(names, key=str.lower):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _code_inventory() -> list[dict[str, object]]:
    repository = Path(__file__).resolve().parents[2]
    paths = sorted((repository / "ml" / "option_pricing").glob("*.py"))
    paths.extend(
        path
        for path in (
            repository / "ml" / "artifacts.py",
            repository / "ml" / "option_pricing_runtime.py",
            repository / "ml" / "option_pricing_opra.py",
            repository / "ml" / "option_pricing_admin.py",
            repository / "ml" / "option_pricing_lockbox.py",
            repository / "pyproject.toml",
            repository / "requirements-ml-runtime.lock",
        )
        if path.is_file()
    )
    return [
        {
            "path": path.relative_to(repository).as_posix(),
            "size": path.stat().st_size,
            "checksum_sha256": file_checksum(path),
        }
        for path in sorted(set(paths))
    ]


def _route_name(symbol: str, call_put: str) -> str:
    return f"{symbol.strip().upper()}/{call_put.strip().lower()}"


def _finite(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _json_safe(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return "NEGATIVE_INFINITY" if value < 0 else "POSITIVE_INFINITY"
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "ELIGIBILITY_POLICY_RECEIPT_VERSION",
    "ELIGIBILITY_POLICY_VERSION",
    "ELIGIBILITY_PROTOCOL_VERSION",
    "ELIGIBILITY_REPORT_VERSION",
    "EVIDENCE_LANES",
    "EligibilityError",
    "EligibilityPolicy",
    "EligibilityPolicyArtifact",
    "EligibilityReportArtifact",
    "REQUIRED_CALL_PUTS",
    "REQUIRED_SYMBOLS",
    "build_eligibility_report",
    "eligibility_policy_payload",
    "paired_session_inference",
    "publish_eligibility_policy",
    "publish_eligibility_report",
    "read_current_eligibility_report",
    "read_eligibility_policy",
]
