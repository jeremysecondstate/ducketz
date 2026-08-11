from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

import pandas as pd

from ml.artifacts import file_checksum, semantic_metadata_fingerprint, utc_timestamp
from ml.option_pricing.policies import (
    LOOP_NATIVE_CALL_PUTS,
    LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION,
    LOOP_NATIVE_MODEL_POLICY_VERSION,
    LOOP_NATIVE_SHADOW_SCHEMA_VERSION,
    LOOP_NATIVE_SURFACE_WEIGHTING_POLICY_VERSION,
    LOOP_NATIVE_SYMBOLS,
    ContractSelectionPolicy,
    LoopNativeModelPolicy,
)


LOOP_NATIVE_ELIGIBILITY_PROTOCOL_VERSION = "option-pricing-loop-native-eligibility-v3"
LOOP_NATIVE_ELIGIBILITY_POLICY_VERSION = (
    "option-pricing-loop-native-eligibility-policy-v3"
)
LOOP_NATIVE_ELIGIBILITY_POLICY_RECEIPT_VERSION = (
    "option-pricing-loop-native-eligibility-policy-receipt-v1"
)
LOOP_NATIVE_ELIGIBILITY_POLICY_POINTER_VERSION = (
    "option-pricing-loop-native-eligibility-policy-pointer-v1"
)
LOOP_NATIVE_ELIGIBILITY_REPORT_VERSION = (
    "option-pricing-loop-native-eligibility-report-v1"
)
LOOP_NATIVE_ELIGIBILITY_REPORT_RECEIPT_VERSION = (
    "option-pricing-loop-native-eligibility-report-receipt-v1"
)
LOOP_NATIVE_ELIGIBILITY_REPORT_POINTER_VERSION = (
    "option-pricing-loop-native-eligibility-report-pointer-v1"
)


class LoopNativeEligibilityError(RuntimeError):
    """A v3 Loop-native eligibility artifact failed closed."""


@dataclass(frozen=True)
class LoopNativeEligibilityPolicy:
    required_symbols: tuple[str, ...] = LOOP_NATIVE_SYMBOLS
    required_call_puts: tuple[str, ...] = LOOP_NATIVE_CALL_PUTS
    minimum_training_sessions: int = 60
    minimum_calibration_sessions: int = 15
    minimum_assessment_sessions: int = 15
    minimum_comparison_sessions_per_route: int = 15
    minimum_constraint_sessions_per_route: int = 15
    minimum_liquidity_sessions_per_route: int = 15
    minimum_prospective_predictions_per_route: int = 60
    minimum_prospective_sessions_per_route: int = 20
    minimum_strategy_pairs: int = 60
    minimum_strategy_sessions: int = 20
    per_contract_fee_usd: float = 0.65
    maximum_constrained_violation_rate: float = 0.0
    maximum_operational_readiness_age_hours: int = 24

    def __post_init__(self) -> None:
        if tuple(self.required_symbols) != LOOP_NATIVE_SYMBOLS:
            raise ValueError("Eligibility v3 requires the exact ten-symbol watchlist")
        if tuple(self.required_call_puts) != LOOP_NATIVE_CALL_PUTS:
            raise ValueError("Eligibility v3 requires CALL and PUT for every symbol")
        minimums = {
            "minimum_training_sessions": 60,
            "minimum_calibration_sessions": 15,
            "minimum_assessment_sessions": 15,
            "minimum_comparison_sessions_per_route": 15,
            "minimum_constraint_sessions_per_route": 15,
            "minimum_liquidity_sessions_per_route": 15,
            "minimum_prospective_predictions_per_route": 60,
            "minimum_prospective_sessions_per_route": 20,
            "minimum_strategy_pairs": 60,
            "minimum_strategy_sessions": 20,
        }
        weakened = [
            name
            for name, minimum in minimums.items()
            if int(getattr(self, name)) < minimum
        ]
        if weakened:
            raise ValueError(
                "Eligibility v3 thresholds cannot be weakened: "
                + ", ".join(weakened)
            )
        if self.per_contract_fee_usd < 0.65:
            raise ValueError("Eligibility v3 contract friction cannot be weakened")
        if self.maximum_constrained_violation_rate != 0.0:
            raise ValueError("Eligibility v3 permits no constrained violations")
        if self.maximum_operational_readiness_age_hours > 24:
            raise ValueError("Operational evidence cannot be older than 24 hours")

    @property
    def required_routes(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (symbol, call_put)
            for symbol in self.required_symbols
            for call_put in self.required_call_puts
        )


@dataclass(frozen=True)
class LoopNativeEligibilityArtifact:
    directory: Path
    payload: Mapping[str, object]
    receipt: Mapping[str, object]
    policy_hash: str


def loop_native_eligibility_policy_payload(
    policy: LoopNativeEligibilityPolicy | None = None,
    *,
    model_policy: LoopNativeModelPolicy | None = None,
    contract_policy: ContractSelectionPolicy | None = None,
) -> dict[str, object]:
    effective = policy or LoopNativeEligibilityPolicy()
    model = model_policy or LoopNativeModelPolicy()
    contracts = contract_policy or ContractSelectionPolicy()
    routes = [
        {"symbol": symbol, "call_put": call_put}
        for symbol, call_put in effective.required_routes
    ]
    return {
        "schema_version": LOOP_NATIVE_ELIGIBILITY_POLICY_VERSION,
        "protocol_version": LOOP_NATIVE_ELIGIBILITY_PROTOCOL_VERSION,
        "required_universe": {
            "symbols": list(effective.required_symbols),
            "call_puts": list(effective.required_call_puts),
            "routes": routes,
            "required_route_count": 20,
            "missing_route_behavior": "NOT_PROVEN_AND_EXPLICITLY_RETAINED",
        },
        "architecture": {
            "control_model": "causal-black-scholes",
            "shadow_model": "pooled-call-put-nystroem-bayesian-ridge-gp-residual",
            "equation": "american_option_shadow=causal_black_scholes+gp_residual",
            "baseline_constrained_fair_value_behavior_changed": False,
            "strategy_rankings_changed": False,
            "order_construction_changed": False,
            "model_training_stage": "strictly-after-fast-target-publication",
            "model_use_rule": "published_at<prediction_created_at",
        },
        "versions": {
            "materialization": LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION,
            "model": LOOP_NATIVE_MODEL_POLICY_VERSION,
            "shadow_schema": LOOP_NATIVE_SHADOW_SCHEMA_VERSION,
            "surface_weighting": LOOP_NATIVE_SURFACE_WEIGHTING_POLICY_VERSION,
        },
        "historical_evidence": {
            "required_provider": "schwab-committed-local-receipts",
            "natural_snapshot_key": ["symbol", "snapshot_for"],
            "duplicate_policy": "earliest-valid-receipt-per-natural-target",
            "offline_lane": "OFFLINE_SCHWAB_BOOTSTRAP",
            "offline_increments_prospective_counts": False,
            "prospective_lane": "PROSPECTIVE_SCHWAB",
            "paid_opra": "OPTIONAL_EXTERNAL_BENCHMARK_ONLY",
            "paid_opra_prerequisite": False,
            "runtime_provider_requests_for_training": 0,
            "current_revised_rate_history_for_historical_targets": "REJECTED",
        },
        "causal_feature_contract": {
            "features": [
                "underlying_price",
                "strike_or_log_moneyness",
                "verified_rate_or_discount",
                "lagged_source_implied_volatility",
                "time_to_expiration",
                "verified_dividend_carry_or_forward",
            ],
            "target_time_implied_volatility_allowed_as_feature": False,
            "target_snapshot_allowed_as_feature": False,
            "later_receipt_allowed_as_feature": False,
            "missing_input_behavior": "MISSING_OR_BASELINE_FALLBACK",
        },
        "contract_filters": {
            **asdict(contracts),
            "standard_non_mini_100_share_only": True,
            "source_and_target_exact_quote_clocks_required": True,
            "maximum_target_receipt_to_quote_staleness_seconds": (
                contracts.maximum_source_staleness_seconds
            ),
            "finite_positive_uncrossed_spread_required": True,
            "semantic_contract_continuity_required": True,
            "rejection_reasons_retained": True,
        },
        "chronological_partitions": {
            "unit": "distinct-XNYS-regular-session",
            "minimum_training_sessions": effective.minimum_training_sessions,
            "minimum_calibration_sessions": effective.minimum_calibration_sessions,
            "minimum_assessment_sessions": effective.minimum_assessment_sessions,
            "causal_boundary_purging": True,
            "assessment_used_for_training": False,
            "assessment_used_for_calibration": False,
            "lockbox_read_during_ordinary_training": False,
        },
        "model_policy": asdict(model),
        "required_comparators": [
            "black_scholes",
            "constant_residual",
            "standard_gp",
        ],
        "thresholds": {
            "minimum_comparison_sessions_per_route": (
                effective.minimum_comparison_sessions_per_route
            ),
            "minimum_constraint_sessions_per_route": (
                effective.minimum_constraint_sessions_per_route
            ),
            "minimum_liquidity_sessions_per_route": (
                effective.minimum_liquidity_sessions_per_route
            ),
            "minimum_prospective_predictions_per_route": (
                effective.minimum_prospective_predictions_per_route
            ),
            "minimum_prospective_sessions_per_route": (
                effective.minimum_prospective_sessions_per_route
            ),
            "minimum_strategy_pairs": effective.minimum_strategy_pairs,
            "minimum_strategy_sessions": effective.minimum_strategy_sessions,
            "per_contract_fee_usd": effective.per_contract_fee_usd,
            "maximum_constrained_violation_rate": (
                effective.maximum_constrained_violation_rate
            ),
            "maximum_operational_readiness_age_hours": (
                effective.maximum_operational_readiness_age_hours
            ),
        },
        "gates": {
            "1": "complete immutable causal lineage and timing",
            "2": "real non-fixture Schwab source and target receipt coverage",
            "3": "session-blocked BSGP improvement over all comparators",
            "4": "interval calibration and constraint compliance",
            "5": "liquidity coverage and all twenty routes retained",
            "6": "operational latency capacity and failure recovery",
            "7": "Strategy shadow outcomes net of fees and spread",
            "8": "twenty distinct prospective sessions per required route",
            "9": "explicit candidate freeze and untouched lockbox prerequisites",
            "10": "explicit operator production authorization",
        },
        "research_caveats": {
            "paper_universe_period": "SPY May-June 2019",
            "paper_mse_transferable_to_2026_ten_equities": False,
            "paper_one_week_example_is_production_horizon_proof": False,
            "cross_sectional_rows_are_independent_sessions": False,
            "deviations": [
                "lagged source-only implied volatility",
                "session-blocked chronological assessment",
                "equal symbol-target-call-put surface weighting",
                "explicit predictive-interval calibration",
                "prospective Loop outcome evaluation",
            ],
        },
        "candidate_frozen": False,
        "lockbox_open": False,
        "production_authorized": False,
        "automated_action_allowed": False,
    }


def publish_loop_native_eligibility_policy(
    datastore_root: Path,
    *,
    policy: LoopNativeEligibilityPolicy | None = None,
    model_policy: LoopNativeModelPolicy | None = None,
    contract_policy: ContractSelectionPolicy | None = None,
    published_at: object | None = None,
) -> LoopNativeEligibilityArtifact:
    root = Path(datastore_root).resolve()
    payload = loop_native_eligibility_policy_payload(
        policy,
        model_policy=model_policy,
        contract_policy=contract_policy,
    )
    policy_hash = semantic_metadata_fingerprint(payload)
    timestamp = utc_timestamp(published_at)
    parent = root / "ml" / "option-pricing-loop-native-eligibility-policies"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    suffix = 2
    while destination.exists():
        destination = parent / f"{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{suffix}"
        suffix += 1
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.tmp-{os.getpid()}-", dir=parent)
    )
    try:
        _write_json(staging / "policy.json", payload)
        receipt = {
            "schema_version": LOOP_NATIVE_ELIGIBILITY_POLICY_RECEIPT_VERSION,
            "run_path": destination.relative_to(root).as_posix(),
            "published_at": timestamp.isoformat(),
            "policy_hash_sha256": policy_hash,
            "policy_checksum_sha256": file_checksum(staging / "policy.json"),
            "automated_action_allowed": False,
        }
        _write_json(staging / "receipt.json", receipt)
        staging.replace(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    artifact = read_loop_native_eligibility_policy(destination, datastore_root=root)
    _write_json_atomic(
        root
        / "ml"
        / "option-pricing-loop-native-eligibility-policy-latest"
        / "run.json",
        {
            "schema_version": LOOP_NATIVE_ELIGIBILITY_POLICY_POINTER_VERSION,
            "current": {
                "run_path": destination.relative_to(root).as_posix(),
                "published_at": timestamp.isoformat(),
                "policy_hash_sha256": policy_hash,
                "receipt_checksum_sha256": file_checksum(destination / "receipt.json"),
            },
        },
    )
    return artifact


def read_loop_native_eligibility_policy(
    directory: Path,
    *,
    datastore_root: Path,
) -> LoopNativeEligibilityArtifact:
    root = Path(datastore_root).resolve()
    run = Path(directory).resolve()
    allowed = (root / "ml" / "option-pricing-loop-native-eligibility-policies").resolve()
    if run.parent != allowed:
        raise LoopNativeEligibilityError("Eligibility policy path escapes its root")
    try:
        payload = json.loads((run / "policy.json").read_text(encoding="utf-8"))
        receipt = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LoopNativeEligibilityError("Eligibility policy is unreadable") from exc
    if not isinstance(payload, Mapping) or not isinstance(receipt, Mapping):
        raise LoopNativeEligibilityError("Eligibility policy metadata is malformed")
    policy_hash = semantic_metadata_fingerprint(payload)
    if (
        payload.get("schema_version") != LOOP_NATIVE_ELIGIBILITY_POLICY_VERSION
        or payload.get("automated_action_allowed") is not False
        or receipt.get("schema_version")
        != LOOP_NATIVE_ELIGIBILITY_POLICY_RECEIPT_VERSION
        or receipt.get("run_path") != run.relative_to(root).as_posix()
        or receipt.get("policy_hash_sha256") != policy_hash
        or receipt.get("policy_checksum_sha256") != file_checksum(run / "policy.json")
        or receipt.get("automated_action_allowed") is not False
    ):
        raise LoopNativeEligibilityError("Eligibility policy verification failed")
    return LoopNativeEligibilityArtifact(run, payload, receipt, policy_hash)


def read_current_loop_native_eligibility_policy(
    datastore_root: Path,
) -> LoopNativeEligibilityArtifact:
    """Read the atomic v3 policy pointer and verify its complete target."""

    root = Path(datastore_root).resolve()
    pointer_path = (
        root
        / "ml"
        / "option-pricing-loop-native-eligibility-policy-latest"
        / "run.json"
    )
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LoopNativeEligibilityError(
            "Loop-native eligibility policy pointer is unreadable"
        ) from exc
    current = pointer.get("current") if isinstance(pointer, Mapping) else None
    if (
        not isinstance(pointer, Mapping)
        or pointer.get("schema_version")
        != LOOP_NATIVE_ELIGIBILITY_POLICY_POINTER_VERSION
        or not isinstance(current, Mapping)
    ):
        raise LoopNativeEligibilityError(
            "Loop-native eligibility policy pointer schema is invalid"
        )
    run_path = str(current.get("run_path", ""))
    run = (root / run_path).resolve()
    allowed = (
        root / "ml" / "option-pricing-loop-native-eligibility-policies"
    ).resolve()
    if run.parent != allowed:
        raise LoopNativeEligibilityError(
            "Loop-native eligibility policy pointer path escapes its root"
        )
    artifact = read_loop_native_eligibility_policy(run, datastore_root=root)
    if (
        current.get("run_path") != artifact.receipt.get("run_path")
        or current.get("published_at") != artifact.receipt.get("published_at")
        or current.get("policy_hash_sha256") != artifact.policy_hash
        or current.get("receipt_checksum_sha256")
        != file_checksum(artifact.directory / "receipt.json")
    ):
        raise LoopNativeEligibilityError(
            "Loop-native eligibility policy pointer verification failed"
        )
    return artifact


def build_loop_native_eligibility_report(
    *,
    policy_artifact: LoopNativeEligibilityArtifact,
    materialization_report: Mapping[str, object] | None,
    model_manifest: Mapping[str, object] | None,
    operational_report: Mapping[str, object] | None,
    strategy_report: Mapping[str, object] | None,
    generated_at: object,
    capture_lineage_verified: bool,
) -> dict[str, object]:
    """Build a conservative v3 report; policy migration is never evidence."""

    materialization = dict(materialization_report or {})
    model = dict(model_manifest or {})
    route_materialization = materialization.get("routes")
    route_model = model.get("route_support_statistics")
    route_materialization = (
        route_materialization if isinstance(route_materialization, Mapping) else {}
    )
    route_model = route_model if isinstance(route_model, Mapping) else {}
    routes: dict[str, object] = {}
    prospective_complete = True
    for symbol in LOOP_NATIVE_SYMBOLS:
        for call_put in LOOP_NATIVE_CALL_PUTS:
            name = f"{symbol}/{call_put.lower()}"
            observed = route_materialization.get(name)
            observed = observed if isinstance(observed, Mapping) else {}
            modeled = route_model.get(name)
            modeled = modeled if isinstance(modeled, Mapping) else {}
            prospective_sessions = int(observed.get("prospective_sessions", 0) or 0)
            prospective_rows = int(observed.get("prospective_rows", 0) or 0)
            route_pass = prospective_sessions >= 20 and prospective_rows >= 60
            prospective_complete &= route_pass
            routes[name] = {
                "status": "PRESENT" if observed.get("status") == "PRESENT" else "MISSING",
                "materialized_rows": int(observed.get("available_row_count", 0) or 0),
                "independent_sessions": int(observed.get("distinct_sessions", 0) or 0),
                "offline_rows": int(observed.get("offline_rows", 0) or 0),
                "prospective_rows": prospective_rows,
                "prospective_sessions": prospective_sessions,
                "model_support_sessions": int(modeled.get("sessions", 0) or 0),
                "twenty_session_requirement": "PASS" if route_pass else "NOT_PROVEN",
            }
    gate1 = "PASS" if capture_lineage_verified else "NOT_PROVEN"
    gates = {
        "1": {
            "status": gate1,
            "name": "immutable-lineage-and-causal-timing",
            "reason": (
                "The versioned policy, materialization, model, and target sidecar chain verified."
                if gate1 == "PASS"
                else "Complete capture-chain verification is not yet available."
            ),
        }
    }
    names = {
        "2": "real-schwab-source-target-coverage",
        "3": "session-blocked-comparator-improvement",
        "4": "calibrated-uncertainty-and-constraints",
        "5": "liquidity-coverage-and-route-retention",
        "6": "operational-latency-and-capacity",
        "7": "strategy-shadow-net-outcomes",
        "8": "twenty-prospective-sessions-per-route",
        "9": "candidate-and-closed-lockbox",
        "10": "explicit-operator-production-authorization",
    }
    for number, name in names.items():
        gates[number] = {
            "status": "NOT_PROVEN",
            "name": name,
            "reason": (
                "Policy migration and offline bootstrap evidence do not satisfy this gate."
            ),
        }
    gates["8"]["all_required_routes_threshold_met"] = prospective_complete
    return {
        "schema_version": LOOP_NATIVE_ELIGIBILITY_REPORT_VERSION,
        "generated_at": utc_timestamp(generated_at).isoformat(),
        "policy": {
            "run_path": policy_artifact.receipt.get("run_path"),
            "policy_hash_sha256": policy_artifact.policy_hash,
        },
        "required_route_count": 20,
        "routes": routes,
        "gates": gates,
        "gate_status": (
            "NOT_PROVEN" if any(gate["status"] != "PASS" for gate in gates.values()) else "PASS"
        ),
        "capture_ready": bool(capture_lineage_verified),
        "research_gate_eligible": False,
        "production_authorized": False,
        "candidate_frozen": False,
        "lockbox_open": False,
        "paid_opra_required": False,
        "paid_opra_used": False,
        "operational_evidence_present": operational_report is not None,
        "strategy_evidence_present": strategy_report is not None,
        "automated_action_allowed": False,
    }


def verify_loop_native_capture_lineage(
    *,
    policy_artifact: LoopNativeEligibilityArtifact,
    target_publication: object,
    materialization: object,
    model_load: object,
) -> Mapping[str, object]:
    """Verify the reachable capture chain without granting a research gate."""

    errors: list[str] = []
    if policy_artifact.payload.get("automated_action_allowed") is not False:
        errors.append("POLICY_AUTOMATION_GUARD_INVALID")
    shadow_path = getattr(target_publication, "shadow_predictions_path", None)
    predictions_method = getattr(target_publication, "predictions", None)
    shadow_method = getattr(target_publication, "shadow_predictions", None)
    if shadow_path is None or not callable(predictions_method) or not callable(shadow_method):
        errors.append("TARGET_SHADOW_SIDECAR_MISSING")
        baseline = pd.DataFrame()
        shadow = pd.DataFrame()
    else:
        baseline = predictions_method(include_proof=False)
        shadow = shadow_method()
        if baseline.empty or shadow.empty or len(baseline) != len(shadow):
            errors.append("TARGET_SHADOW_CAPTURE_ROWS_MISSING")
        elif shadow["automated_action_allowed"].fillna(True).astype(bool).any():
            errors.append("TARGET_SHADOW_AUTOMATION_GUARD_INVALID")
    materialization_report = getattr(materialization, "report", {})
    materialization_report = (
        materialization_report
        if isinstance(materialization_report, Mapping)
        else {}
    )
    if (
        materialization_report.get("external_provider_requests") != 0
        or materialization_report.get("paid_opra_used") is not False
        or materialization_report.get("automated_action_allowed") is not False
    ):
        errors.append("MATERIALIZATION_GUARDS_INVALID")
    generation = getattr(model_load, "generation", None)
    created = getattr(target_publication, "created_at", None)
    if generation is not None:
        if created is None or not generation.published_at < utc_timestamp(created):
            errors.append("MODEL_NOT_STRICTLY_EARLIER_THAN_PREDICTION")
        if not shadow.empty:
            paths = set(
                shadow["bsgp_shadow_model_generation_path"]
                .astype("string")
                .dropna()
            )
            expected = str(generation.receipt.get("run_path", ""))
            if paths != {expected}:
                errors.append("SHADOW_MODEL_LINEAGE_DISAGREES")
    elif not shadow.empty and shadow[
        "bsgp_shadow_model_generation_path"
    ].astype("string").fillna("").str.strip().ne("").any():
        errors.append("UNVERIFIED_MODEL_LINEAGE_PRESENT")
    return {
        "schema_version": "loop-native-capture-lineage-verification-v1",
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "baseline_rows": len(baseline),
        "shadow_rows": len(shadow),
        "model_generation": (
            generation.receipt.get("run_path") if generation is not None else None
        ),
        "paid_opra_used": False,
        "automated_action_allowed": False,
    }


def publish_loop_native_eligibility_report(
    datastore_root: Path,
    *,
    report: Mapping[str, object],
    published_at: object | None = None,
) -> LoopNativeEligibilityArtifact:
    root = Path(datastore_root).resolve()
    timestamp = utc_timestamp(published_at)
    payload = dict(report)
    if (
        payload.get("schema_version") != LOOP_NATIVE_ELIGIBILITY_REPORT_VERSION
        or payload.get("automated_action_allowed") is not False
        or payload.get("production_authorized") is not False
    ):
        raise LoopNativeEligibilityError("Eligibility report is not fail-closed")
    report_hash = semantic_metadata_fingerprint(payload)
    parent = root / "ml" / "option-pricing-loop-native-eligibility-reports"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    suffix = 2
    while destination.exists():
        destination = parent / f"{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}-{suffix}"
        suffix += 1
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.tmp-{os.getpid()}-", dir=parent)
    )
    try:
        _write_json(staging / "report.json", payload)
        receipt = {
            "schema_version": LOOP_NATIVE_ELIGIBILITY_REPORT_RECEIPT_VERSION,
            "run_path": destination.relative_to(root).as_posix(),
            "published_at": timestamp.isoformat(),
            "report_hash_sha256": report_hash,
            "report_checksum_sha256": file_checksum(staging / "report.json"),
            "automated_action_allowed": False,
        }
        _write_json(staging / "receipt.json", receipt)
        staging.replace(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    artifact = _read_loop_native_eligibility_report(destination, datastore_root=root)
    _write_json_atomic(
        root
        / "ml"
        / "option-pricing-loop-native-eligibility-latest"
        / "run.json",
        {
            "schema_version": LOOP_NATIVE_ELIGIBILITY_REPORT_POINTER_VERSION,
            "current": {
                "run_path": destination.relative_to(root).as_posix(),
                "published_at": timestamp.isoformat(),
                "report_hash_sha256": report_hash,
                "receipt_checksum_sha256": file_checksum(destination / "receipt.json"),
            },
        },
    )
    return artifact


def _read_loop_native_eligibility_report(
    directory: Path,
    *,
    datastore_root: Path,
) -> LoopNativeEligibilityArtifact:
    root = Path(datastore_root).resolve()
    run = Path(directory).resolve()
    allowed = (root / "ml" / "option-pricing-loop-native-eligibility-reports").resolve()
    if run.parent != allowed:
        raise LoopNativeEligibilityError("Eligibility report path escapes its root")
    try:
        payload = json.loads((run / "report.json").read_text(encoding="utf-8"))
        receipt = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LoopNativeEligibilityError("Eligibility report is unreadable") from exc
    if not isinstance(payload, Mapping) or not isinstance(receipt, Mapping):
        raise LoopNativeEligibilityError("Eligibility report metadata is malformed")
    report_hash = semantic_metadata_fingerprint(payload)
    if (
        payload.get("schema_version") != LOOP_NATIVE_ELIGIBILITY_REPORT_VERSION
        or payload.get("automated_action_allowed") is not False
        or payload.get("production_authorized") is not False
        or receipt.get("schema_version")
        != LOOP_NATIVE_ELIGIBILITY_REPORT_RECEIPT_VERSION
        or receipt.get("run_path") != run.relative_to(root).as_posix()
        or receipt.get("report_hash_sha256") != report_hash
        or receipt.get("report_checksum_sha256") != file_checksum(run / "report.json")
        or receipt.get("automated_action_allowed") is not False
    ):
        raise LoopNativeEligibilityError("Eligibility report verification failed")
    return LoopNativeEligibilityArtifact(run, payload, receipt, report_hash)


def read_current_loop_native_eligibility_report(
    datastore_root: Path,
) -> LoopNativeEligibilityArtifact:
    """Read the atomic v3 report pointer and verify its complete target."""

    root = Path(datastore_root).resolve()
    pointer_path = (
        root / "ml" / "option-pricing-loop-native-eligibility-latest" / "run.json"
    )
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LoopNativeEligibilityError(
            "Loop-native eligibility report pointer is unreadable"
        ) from exc
    current = pointer.get("current") if isinstance(pointer, Mapping) else None
    if (
        not isinstance(pointer, Mapping)
        or pointer.get("schema_version")
        != LOOP_NATIVE_ELIGIBILITY_REPORT_POINTER_VERSION
        or not isinstance(current, Mapping)
    ):
        raise LoopNativeEligibilityError(
            "Loop-native eligibility report pointer schema is invalid"
        )
    run_path = str(current.get("run_path", ""))
    run = (root / run_path).resolve()
    allowed = (
        root / "ml" / "option-pricing-loop-native-eligibility-reports"
    ).resolve()
    if run.parent != allowed:
        raise LoopNativeEligibilityError(
            "Loop-native eligibility report pointer path escapes its root"
        )
    artifact = _read_loop_native_eligibility_report(run, datastore_root=root)
    if (
        current.get("run_path") != artifact.receipt.get("run_path")
        or current.get("published_at") != artifact.receipt.get("published_at")
        or current.get("report_hash_sha256") != artifact.policy_hash
        or current.get("receipt_checksum_sha256")
        != file_checksum(artifact.directory / "receipt.json")
    ):
        raise LoopNativeEligibilityError(
            "Loop-native eligibility report pointer verification failed"
        )
    return artifact


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    try:
        _write_json(temporary, payload)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "LOOP_NATIVE_ELIGIBILITY_POLICY_VERSION",
    "LOOP_NATIVE_ELIGIBILITY_PROTOCOL_VERSION",
    "LOOP_NATIVE_ELIGIBILITY_REPORT_VERSION",
    "LoopNativeEligibilityArtifact",
    "LoopNativeEligibilityError",
    "LoopNativeEligibilityPolicy",
    "build_loop_native_eligibility_report",
    "loop_native_eligibility_policy_payload",
    "publish_loop_native_eligibility_policy",
    "publish_loop_native_eligibility_report",
    "read_current_loop_native_eligibility_policy",
    "read_current_loop_native_eligibility_report",
    "read_loop_native_eligibility_policy",
    "verify_loop_native_capture_lineage",
]
