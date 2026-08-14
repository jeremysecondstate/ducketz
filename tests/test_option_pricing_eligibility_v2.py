from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ml.option_pricing.eligibility import (
    EligibilityPolicy,
    EligibilityPolicyArtifact,
    build_eligibility_report,
)


def _policy_artifact(tmp_path: Path) -> EligibilityPolicyArtifact:
    directory = tmp_path / "policy"
    directory.mkdir(parents=True)
    (directory / "receipt.json").write_text("{}\n", encoding="utf-8")
    return EligibilityPolicyArtifact(directory, {}, {}, "policy-hash")


def _verified_lineage() -> dict[str, object]:
    return {
        "schema_version": "option-pricing-lineage-verification-v2",
        "stage": "COMPLETED",
        "verified": True,
        "evidence_kind": "REAL_RECEIPT_PROVEN",
        "fixture_test_evidence": False,
        "checks": {},
        "errors": [],
    }


def _verified_operational(policy_hash: str) -> dict[str, object]:
    return {
        "status": "PASS",
        "checked_at": "2026-08-07T19:00:00Z",
        "eligibility_policy_hash": policy_hash,
        "dependency_contract": {"status": "PASS"},
        "configuration": {"status": "PASS"},
        "capacity_and_retention": {"status": "PASS"},
        "pip_check": {"status": "PASS"},
        "publication_and_benchmark": {"status": "PASS"},
        "cli_smoke": {
            name: {"status": "PASS"}
            for name in ("runtime", "opra", "admin", "lockbox")
        },
        "startup_shutdown_crash_recovery_documented": True,
        "reverified_before_publication": True,
        "automated_action_allowed": False,
    }


def _assessment_records() -> list[dict[str, object]]:
    start = pd.Timestamp("2025-01-02T15:00:00Z")
    return [
        {
            "target_snapshot_for": (
                start + pd.Timedelta(days=index // 3, minutes=index % 3)
            ).isoformat(),
            "finite_basis_residual_normalized_squared_error": 0.01,
            "black_scholes_normalized_squared_error": 0.04,
            "constant_residual_normalized_squared_error": 0.03,
            "finite_basis_price_comparator_normalized_squared_error": 0.02,
            "interval_80_coverage": float(index < 51),
            "interval_95_coverage": float(index < 60),
            "contract_count": 5,
        }
        for index in range(63)
    ]


def _passing_inputs(
    *,
    monotonic_edge: bool = True,
) -> tuple[dict[str, object], pd.DataFrame, pd.DataFrame]:
    policy = EligibilityPolicy()
    reports: dict[str, object] = {}
    predictions: list[dict[str, object]] = []
    evaluations: list[dict[str, object]] = []
    assessment = _assessment_records()
    for symbol, call_put in policy.required_routes:
        route_name = f"{symbol}/{call_put.lower()}"
        reports[route_name] = {
            "status": "MODEL_FIT",
            "source_provider": "databento-opra",
            "evidence_kind": "REAL_RECEIPT_PROVEN",
            "partition_contract": {
                "cluster_counts": {
                    "train": 252,
                    "calibration": 63,
                    "assessment": 63,
                    "lockbox": 126,
                },
                "calendar_span_months": 8,
                "lockbox_status": "CLOSED_UNTOUCHED_UNSCORED",
                "lockbox_target_values_reported": False,
            },
            "assessment_metrics": {
                "paired_snapshot_losses": assessment,
            },
        }
        for index in range(63):
            predictions.append(
                {
                    "symbol": symbol,
                    "call_put": call_put,
                    "contract_symbol": f"{symbol}-{call_put}-constraint-{index}",
                    "target_snapshot_for": pd.Timestamp("2025-02-01T15:00:00Z")
                    + pd.Timedelta(days=index),
                    "prediction_mode": "OFFLINE",
                    "source_provider": "databento-opra",
                    "prediction_status": "AVAILABLE",
                    "constrained_bound_violation": False,
                    "constrained_monotonicity_violation": False,
                    "constrained_convexity_violation": False,
                    "projection_correction": 0.0,
                    "underlying_price": 100.0,
                }
            )
        for bucket_index, edge in enumerate((-3.0, -1.5, -0.5, 0.5, 1.5, 3.0)):
            desired = (
                float(bucket_index + 1)
                if monotonic_edge
                else float(6 - bucket_index)
            )
            for index in range(20):
                target = pd.Timestamp("2025-05-01T14:00:00Z") + pd.Timedelta(
                    days=index, minutes=bucket_index
                )
                baseline = 10.0
                half_spread = 0.10
                direction = -1.0 if edge < 0 else 1.0
                observed = baseline + direction * (desired + 10.65) / 100.0
                evaluations.append(
                    {
                        "symbol": symbol,
                        "call_put": call_put,
                        "contract_symbol": (
                            f"{symbol}-{call_put}-edge-{bucket_index}-{index}"
                        ),
                        "target_snapshot_for": target,
                        "prediction_created_at": target,
                        "prediction_mode": "OFFLINE",
                        "source_provider": "databento-opra",
                        "evaluation_status": "COMPLETE",
                        "prospective_eligible": False,
                        "constrained_fair_value": baseline + edge * half_spread,
                        "black_scholes_price": baseline,
                        "observed_mid": observed,
                        "bid_ask_spread": 2.0 * half_spread,
                        "multiplier": 100.0,
                    }
                )
        for index in range(60):
            target = pd.Timestamp("2026-01-05T15:00:00Z") + pd.Timedelta(
                days=index // 3, minutes=index % 3
            )
            evaluations.append(
                {
                    "symbol": symbol,
                    "call_put": call_put,
                    "contract_symbol": f"{symbol}-{call_put}-live-{index}",
                    "target_snapshot_for": target,
                    "prediction_created_at": target + pd.Timedelta(seconds=1),
                    "prediction_mode": "LIVE",
                    "source_provider": "databento-opra",
                    "outcome_provider": "databento-opra",
                    "evidence_lane": "PROSPECTIVE_OPRA",
                    "evaluation_status": "COMPLETE",
                    "prospective_eligible": True,
                }
            )
    return {"model_reports": reports}, pd.DataFrame(predictions), pd.DataFrame(evaluations)


def _build(
    tmp_path: Path,
    *,
    model_reports: dict[str, object],
    predictions: pd.DataFrame,
    evaluations: pd.DataFrame,
    operational_checked_at: str = "2026-08-07T19:00:00Z",
) -> dict[str, object]:
    artifact = _policy_artifact(tmp_path)
    strategy_routes = {
        f"{symbol}/{call_put.lower()}": {
            "status": "PASS",
            "paired_candidate_count": 60,
            "distinct_sessions": 20,
        }
        for symbol, call_put in EligibilityPolicy().required_routes
    }
    operational = _verified_operational(artifact.policy_hash)
    operational["checked_at"] = operational_checked_at
    return build_eligibility_report(
        policy_artifact=artifact,
        policy=EligibilityPolicy(),
        evaluations=evaluations,
        predictions=predictions,
        model_reports=model_reports,
        lineage_report=_verified_lineage(),
        strategy_report={
            "status": "PASS",
            "evidence_kind": "REAL_RECEIPT_PROVEN",
            "same_candidate_cohort": True,
            "rankings_changed": False,
            "order_construction_changed": False,
            "automated_action_allowed": False,
            "paired_candidate_count": 60,
            "distinct_sessions": 20,
            "lower_confidence_bound_usd": 1.0,
            "uncertainty_coverage": 0.95,
            "routes": strategy_routes,
        },
        frozen_candidate={
            "status": "FROZEN",
            "candidate_id": "candidate-1",
            "eligibility_policy_hash": artifact.policy_hash,
            "source_evidence_kind": "REAL_RECEIPT_PROVEN",
            "production_evidence_eligible": True,
            "fresh_future_lockbox": {"status": "PASS"},
            "retraining_allowed": False,
            "hyperparameter_changes_allowed": False,
            "permanently_invalidated": False,
        },
        lockbox_result={
            "status": "PASS",
            "candidate_id": "candidate-1",
            "eligibility_policy_hash": artifact.policy_hash,
            "one_time_score": True,
            "evidence_kind": "REAL_RECEIPT_PROVEN",
            "production_evidence_eligible": True,
            "all_required_routes_pass": True,
            "all_required_buckets_pass": True,
        },
        operational_report=operational,
        generated_at="2026-08-07T20:00:00Z",
    )


def test_only_complete_real_twelve_route_evidence_can_be_production_eligible(
    tmp_path: Path,
) -> None:
    model_reports, predictions, evaluations = _passing_inputs()
    report = _build(
        tmp_path,
        model_reports=model_reports,
        predictions=predictions,
        evaluations=evaluations,
    )
    assert report["gate_status"] == "PRODUCTION_ELIGIBLE"
    assert report["automated_action_allowed"] is False
    assert len(report["routes"]) == 12
    assert all(gate["status"] == "PASS" for gate in report["gates"])
    assert all(
        route["prospective"]["completed_predictions"] == 60
        and route["prospective"]["distinct_sessions"] == 20
        for route in report["routes"].values()
    )


def test_fixture_mixed_source_and_omitted_route_each_fail_closed(
    tmp_path: Path,
) -> None:
    model_reports, predictions, evaluations = _passing_inputs()
    fixture_reports = json.loads(json.dumps(model_reports))
    fixture_reports["model_reports"]["NVDA/call"][
        "evidence_kind"
    ] = "FIXTURE_TEST_ONLY"
    fixture = _build(
        tmp_path / "fixture",
        model_reports=fixture_reports,
        predictions=predictions,
        evaluations=evaluations,
    )
    assert fixture["gate_status"] == "NOT_PRODUCTION_ELIGIBLE"
    assert fixture["gates"][0]["status"] == "FAIL"

    mixed_predictions = predictions.copy()
    mixed_predictions.loc[mixed_predictions.index[0], "source_provider"] = "schwab"
    mixed = _build(
        tmp_path / "mixed",
        model_reports=model_reports,
        predictions=mixed_predictions,
        evaluations=evaluations,
    )
    assert mixed["gate_status"] == "NOT_PRODUCTION_ELIGIBLE"
    assert mixed["evidence_lanes"]["OFFLINE_TRAIN_CALIBRATION"]["isolated"] is False

    missing_reports = {
        "model_reports": dict(model_reports["model_reports"]),
    }
    missing_reports["model_reports"].pop("MU/put")
    missing = _build(
        tmp_path / "missing",
        model_reports=missing_reports,
        predictions=predictions,
        evaluations=evaluations,
    )
    assert "MU/put" in missing["routes"]
    assert missing["routes"]["MU/put"]["partition"]["status"] == "NOT_PROVEN"
    assert missing["gate_status"] == "NOT_PRODUCTION_ELIGIBLE"


def test_edge_gate_requires_economics_and_all_predeclared_buckets(
    tmp_path: Path,
) -> None:
    model_reports, predictions, evaluations = _passing_inputs(monotonic_edge=False)
    report = _build(
        tmp_path,
        model_reports=model_reports,
        predictions=predictions,
        evaluations=evaluations,
    )
    edge = report["routes"]["NVDA/call"]["economic_edge"]
    assert edge["bucket_minima_pass"] is True
    assert edge["bucket_monotonic"] is False
    assert len(edge["buckets"]) == 6
    assert edge["inference"]["lower_confidence_bound"] > 0.0
    assert report["gates"][7]["status"] == "NOT_PROVEN"
    assert report["gate_status"] == "NOT_PRODUCTION_ELIGIBLE"


def test_stale_operational_readiness_cannot_promote(tmp_path: Path) -> None:
    model_reports, predictions, evaluations = _passing_inputs()
    report = _build(
        tmp_path,
        model_reports=model_reports,
        predictions=predictions,
        evaluations=evaluations,
        operational_checked_at="2026-08-06T18:59:59Z",
    )
    assert report["operational_promotion"]["status"] == "NOT_PROVEN"
    assert report["gate_status"] == "NOT_PRODUCTION_ELIGIBLE"


def test_collecting_and_eligible_states_never_activate_automation(
    tmp_path: Path,
) -> None:
    model_reports, predictions, evaluations = _passing_inputs()
    prospective = evaluations["prediction_mode"].eq("LIVE")
    reduced = evaluations.loc[~prospective].copy()
    artifact = _policy_artifact(tmp_path / "collecting")
    common = {
        "policy_artifact": artifact,
        "policy": EligibilityPolicy(),
        "evaluations": reduced,
        "predictions": predictions,
        "model_reports": model_reports,
        "lineage_report": _verified_lineage(),
        "strategy_report": {
            "status": "PASS",
            "evidence_kind": "REAL_RECEIPT_PROVEN",
            "same_candidate_cohort": True,
            "rankings_changed": False,
            "order_construction_changed": False,
            "automated_action_allowed": False,
            "paired_candidate_count": 60,
            "distinct_sessions": 20,
            "lower_confidence_bound_usd": 1.0,
            "uncertainty_coverage": 0.95,
            "routes": {
                f"{symbol}/{call_put.lower()}": {
                    "status": "PASS",
                    "paired_candidate_count": 60,
                    "distinct_sessions": 20,
                }
                for symbol, call_put in EligibilityPolicy().required_routes
            },
        },
        "frozen_candidate": {
            "status": "FROZEN",
            "candidate_id": "candidate-1",
            "eligibility_policy_hash": artifact.policy_hash,
            "source_evidence_kind": "REAL_RECEIPT_PROVEN",
            "production_evidence_eligible": True,
            "fresh_future_lockbox": {"status": "PASS"},
            "retraining_allowed": False,
            "hyperparameter_changes_allowed": False,
            "permanently_invalidated": False,
        },
        "lockbox_result": None,
        "operational_report": _verified_operational(artifact.policy_hash),
        "generated_at": "2026-08-07T20:00:00Z",
    }
    collecting = build_eligibility_report(**common)
    assert collecting["gate_status"] == "COLLECTING_PROSPECTIVE_EVIDENCE"
    assert collecting["automated_action_allowed"] is False
    assert collecting["activation_status"] == "SEPARATE_OPERATOR_AUTHORIZATION_REQUIRED"

    eligible = _build(
        tmp_path / "eligible",
        model_reports=model_reports,
        predictions=predictions,
        evaluations=evaluations,
    )
    assert eligible["gate_status"] == "PRODUCTION_ELIGIBLE"
    assert eligible["automated_action_allowed"] is False
    assert eligible["activation_status"] == "SEPARATE_OPERATOR_AUTHORIZATION_REQUIRED"
