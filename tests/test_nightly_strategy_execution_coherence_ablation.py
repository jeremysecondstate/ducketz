from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml.nightly_strategy_execution_coherence_ablation import (
    ABLATION_SCHEMA_VERSION,
    ELIGIBLE_SESSION,
    EXPECTED_GATE_SOURCE_SET_SHA256,
    EXPECTED_SOURCE_SET_SHA256,
    PREREGISTRATION_ID,
    PREREGISTRATION_SHA256,
    SOURCE_GENERATION,
    _aggregate_records,
    _deduplicate_exact_constructions,
    _find_existing_result,
    _metric_nondegradation_gates,
    _probability_metrics,
    _project_exact_outcomes,
    _publish_result,
    _result_store_has_material,
    _validate_implementation_sources,
)


def test_aggregate_records_is_sorted_lf_without_trailing_lf() -> None:
    assert _aggregate_records(["b|2", "a|1"]) == _aggregate_records(
        ["a|1", "b|2"]
    )
    assert _aggregate_records(["a|1", "b|2"]) != _aggregate_records(
        ["a|1", "b|2", ""]
    )


def test_alias_analysis_retains_lexicographic_key_and_reports_score_mismatch() -> None:
    first = _alias_row(candidate_key="z-alias", raw=0.20, calibrated=0.25)
    second = _alias_row(candidate_key="a-alias", raw=0.21, calibrated=0.26)

    exact, evidence, proof = _deduplicate_exact_constructions(
        pd.DataFrame([first, second]),
        validate_preregistered_counts=False,
    )

    assert exact["candidate_key"].tolist() == ["a-alias"]
    assert evidence["duplicate_groups"] == 1
    assert evidence["duplicate_rows_removed"] == 1
    assert evidence["all_aliases_equal"] is False
    assert evidence["by_horizon"]["1d"]["non_probability_mismatch_groups"] == 0
    assert evidence["by_horizon"]["1d"]["raw_probability_mismatch_groups"] == 1
    assert (
        evidence["by_horizon"]["1d"][
            "calibrated_probability_mismatch_groups"
        ]
        == 1
    )
    assert proof.loc[0, "maximum_raw_probability_delta"] == pytest.approx(0.01)


def test_alias_analysis_rejects_non_probability_disagreement() -> None:
    first = _alias_row(candidate_key="a", raw=0.2, calibrated=0.3)
    second = _alias_row(candidate_key="b", raw=0.2, calibrated=0.3)
    second["net_profit"] = -11.0

    _, evidence, _ = _deduplicate_exact_constructions(
        pd.DataFrame([first, second]),
        validate_preregistered_counts=False,
    )

    assert evidence["all_aliases_equal"] is False
    assert evidence["by_horizon"]["1d"]["non_probability_mismatch_groups"] == 1


def test_projection_enforces_fee_adjusted_bounds_and_preserves_other_rows() -> None:
    frame = pd.DataFrame(
        [
            _projection_row("lower", net_profit=-70.0, max_profit=100.0),
            _projection_row("upper", net_profit=120.0, max_profit=100.0),
            _projection_row("valid", net_profit=-10.0, max_profit=100.0),
            _projection_row("unbounded", net_profit=120.0, max_profit=np.nan),
            _projection_row(
                "path",
                net_profit=-70.0,
                max_profit=np.nan,
                risk_status="PATH_DEPENDENT_CONSERVATIVE_ASSIGNMENT_BOUND",
            ),
        ]
    )

    projected = _project_exact_outcomes(frame, fee_per_leg=0.65).set_index(
        "candidate_key"
    )

    assert projected.loc["lower", "candidate_net_profit"] == pytest.approx(-51.3)
    assert projected.loc["upper", "candidate_net_profit"] == pytest.approx(98.7)
    assert projected.loc["lower", "candidate_return_on_risk"] == pytest.approx(
        -1.026
    )
    assert projected.loc["valid", "candidate_net_profit"] == -10.0
    assert projected.loc["unbounded", "candidate_net_profit"] == 120.0
    assert projected.loc["unbounded", "unbounded_upper"]
    assert projected.loc["path", "candidate_net_profit"] == -70.0
    assert projected["candidate_lower_breach"].sum() == 0
    assert projected["candidate_upper_breach"].sum() == 0
    assert projected["label_sign_changed"].sum() == 0
    assert projected["projection_applied"].sum() == 2


def test_probability_metrics_and_nondegradation_gate_are_independent() -> None:
    metrics = _probability_metrics([0, 1, 0, 1], [0.1, 0.9, 0.2, 0.8])
    assert metrics["brier_score"] == pytest.approx(0.025)
    assert metrics["roc_auc"] == 1.0
    assert metrics["probability_coverage"] == 1.0
    gates = _metric_nondegradation_gates(
        {
            "brier_score": 0.03,
            "log_loss": 0.08,
            "expected_calibration_error_10_bin": 0.01,
        },
        {
            "brier_score": 0.02,
            "log_loss": 0.09,
            "expected_calibration_error_10_bin": 0.02,
        },
    )
    assert gates == {
        "brier_score": True,
        "log_loss": False,
        "expected_calibration_error_10_bin": False,
    }


def test_content_addressed_receipt_replays_once_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    implementation = _validate_implementation_sources(repo_root)
    report = _terminal_test_report()
    proof = pd.DataFrame(
        [
            {
                "error_type": "SyntheticError",
                "error": "synthetic",
                "source_fingerprint_sha256": PREREGISTRATION_SHA256,
                "terminal_blocked": True,
            }
        ]
    )
    first = _publish_result(
        tmp_path,
        repo_root=repo_root,
        created=pd.Timestamp("2026-08-29T10:00:00Z"),
        report=report,
        proof=proof,
        source_inventory=(),
        gate_inventory=(),
        implementation_inventory=implementation,
    )

    assert first.directory.name == PREREGISTRATION_SHA256
    assert first.decision == "BLOCKED"
    assert not (first.directory.parent / "current.json").exists()
    assert _result_store_has_material(tmp_path)
    replay = _find_existing_result(tmp_path, repo_root=repo_root)
    assert replay is not None
    assert replay.status == "UNCHANGED_SKIPPED"
    assert replay.receipt_path == first.receipt_path

    competing = first.directory.parent / "competing" / "receipt.json"
    competing.parent.mkdir()
    competing.write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Unexpected committed"):
        _find_existing_result(tmp_path, repo_root=repo_root)

    report_path = first.report_path
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["decision"] = "PROPOSAL_ONLY"
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        _find_existing_result(tmp_path, repo_root=repo_root)


def test_content_addressed_receipt_rejects_semantic_identity_tampering(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    implementation = _validate_implementation_sources(repo_root)
    report = _terminal_test_report()
    proof = pd.DataFrame(
        [
            {
                "error_type": "SyntheticError",
                "error": "synthetic",
                "source_fingerprint_sha256": PREREGISTRATION_SHA256,
                "terminal_blocked": True,
            }
        ]
    )
    result = _publish_result(
        tmp_path,
        repo_root=repo_root,
        created=pd.Timestamp("2026-08-29T10:00:00Z"),
        report=report,
        proof=proof,
        source_inventory=(),
        gate_inventory=(),
        implementation_inventory=implementation,
    )
    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    receipt["source_set_sha256"] = "0" * 64
    result.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(RuntimeError, match="identity changed"):
        _find_existing_result(tmp_path, repo_root=repo_root)


def _terminal_test_report() -> dict[str, object]:
    return {
        "schema_version": ABLATION_SCHEMA_VERSION,
        "status": "COMPLETE_SHADOW_ONLY",
        "decision": "BLOCKED",
        "proof_kind": "TERMINAL_VALIDATION_OR_RUNTIME_ROLLBACK",
        "eligible_session": ELIGIBLE_SESSION,
        "preregistration": {
            "id": PREREGISTRATION_ID,
            "sha256": PREREGISTRATION_SHA256,
        },
        "source": {
            "authority_generation": SOURCE_GENERATION,
            "source_set_sha256": EXPECTED_SOURCE_SET_SHA256,
        },
        "checked_in_gates": {
            "gate_source_set_sha256": EXPECTED_GATE_SOURCE_SET_SHA256,
        },
        "failed_gates": {"GLOBAL": ["synthetic"]},
        "terminal_rollback": {
            "triggered": True,
            "retry_allowed": False,
            "reinterpretation_allowed": False,
        },
        "safety": {
            "real_lockbox_opened": False,
            "opra_archive_rescan_performed": False,
            "fit_performed": False,
            "calibration_performed": False,
            "threshold_selection_performed": False,
            "ranking_performed": False,
            "account_or_portfolio_read_performed": False,
            "production_mutation": False,
            "production_candidate_mutation": False,
            "production_model_authority_mutation": False,
            "production_authority_mutation": False,
            "runtime_mutation": False,
            "ui_or_ranking_mutation": False,
            "promotion_performed": False,
            "orders_enabled": False,
            "orders_placed": 0,
        },
    }


def _alias_row(
    *,
    candidate_key: str,
    raw: float,
    calibrated: float,
) -> dict[str, object]:
    return {
        "horizon": "1d",
        "target_window_start": pd.Timestamp("2026-01-02T15:00:00.000000001Z"),
        "decision_timestamp": pd.Timestamp("2026-01-01T21:05:00Z"),
        "symbol": "AAPL",
        "strategy_family": "synthetic",
        "candidate_key": candidate_key,
        "legs_json": '[{"asset":"OPTION","side":"LONG"}]',
        "risk_calculation_status": "EXPIRATION_PAYOFF_EXACT",
        "outcome_status": "COMPLETE",
        "outcome_reason": "",
        "exit_available_at": pd.Timestamp("2026-01-02T21:00:00Z"),
        "exit_cash_flow": -5.0,
        "net_profit": -10.0,
        "return_on_risk": -0.2,
        "profitable": 0,
        "max_profit": 100.0,
        "max_loss": 50.0,
        "entry_cash_flow": -5.0,
        "entry_fees": 1.3,
        "capital_required": 50.0,
        "leg_count": 2,
        "raw_profit_probability": raw,
        "calibrated_profit_probability": calibrated,
        "outcome_policy_version": "test",
        "execution_evidence_type": "MODELED_OPRA_OHLCV_1H",
        "execution_quality_pass": True,
    }


def _projection_row(
    candidate_key: str,
    *,
    net_profit: float,
    max_profit: float,
    risk_status: str = "EXPIRATION_PAYOFF_EXACT",
) -> dict[str, object]:
    return {
        "candidate_key": candidate_key,
        "risk_calculation_status": risk_status,
        "leg_count": 2,
        "max_loss": 50.0,
        "max_profit": max_profit,
        "capital_required": 50.0,
        "net_profit": net_profit,
        "return_on_risk": net_profit / 50.0,
        "profitable": int(net_profit > 0.0),
    }
