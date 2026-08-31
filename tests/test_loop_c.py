from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.artifacts import file_checksum, write_manifest
from ml.loop_c.engine import evaluate_loop_c
from ml.loop_c.events import MarketEvent, replay_available_events
from ml.loop_c.inputs import load_loop_c_inputs
from ml.loop_c.policy import (
    LoopCMode,
    LoopCPredictiveThresholds,
    LoopCRiskLimits,
    expected_sequence_model_binding,
)
from ml.loop_c.publication import (
    publish_loop_c_observe_run,
    read_current_loop_c_publication,
)
from ml.loop_c.runtime import _merge_candidates, _validate_sequence_model_binding
from ml.loop_c.rollout import (
    LoopCShadowEvidence,
    evaluate_loop_c_rollout,
)
from ml.sequence_encoder.contracts import SequenceEncoderConfig


NOW = pd.Timestamp("2026-08-03T16:00:00Z")


def _limits() -> LoopCRiskLimits:
    thresholds = {
        horizon: LoopCPredictiveThresholds(
            minimum_strategy_calibrated_probability=0.55,
            minimum_sequence_directional_probability=0.55,
            minimum_expected_return_on_risk=0.01,
            maximum_total_uncertainty=0.05,
            uncertainty_penalty=1.0,
        )
        for horizon in ("1h", "4h", "1d", "1w")
    }
    return LoopCRiskLimits(
        maximum_snapshot_age_seconds=120.0,
        maximum_model_age_seconds=3_600.0,
        maximum_daily_loss=500.0,
        maximum_gross_exposure=10_000.0,
        maximum_symbol_exposure=5_000.0,
        maximum_trade_loss=500.0,
        maximum_open_positions=10,
        maximum_working_orders=5,
        predictive_thresholds_by_horizon=thresholds,
        maximum_candidate_quantity=3,
    )


def _portfolio(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "reconciled": True,
        "observed_at": NOW - pd.Timedelta(seconds=30),
        "daily_pnl": 25.0,
        "gross_exposure": 2_000.0,
        "symbol_exposure": {"AAPL": 100.0},
        "open_positions": 2,
        "available_cash": 1_000.0,
    }
    value.update(changes)
    return value


def _broker(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "reconciled": True,
        "observed_at": NOW - pd.Timedelta(seconds=20),
        "working_orders": 0,
        "unknown_submission_status": False,
    }
    value.update(changes)
    return value


def _candidates() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": ["candidate-a"],
            "symbol": ["AAPL"],
            "horizon": ["1d"],
            "calibrated_probability": [0.70],
            "sequence_directional_probability": [0.65],
            "sequence_expected_return": [0.02],
            "sequence_adverse_return": [-0.015],
            "expected_return_on_risk": [0.10],
            "total_uncertainty": [0.02],
            "max_loss": [100.0],
            "capital_required": [200.0],
        }
    )


def _evaluate(
    *,
    portfolio: dict[str, object] | None = None,
    broker: dict[str, object] | None = None,
    halt_requested: bool = False,
):
    return evaluate_loop_c(
        _candidates(),
        decision_timestamp=NOW,
        mode=LoopCMode.OBSERVE,
        market_session_open=True,
        portfolio=_portfolio() if portfolio is None else portfolio,
        broker=_broker() if broker is None else broker,
        risk_limits=_limits(),
        model_authority="SHADOW_ONLY",
        model_published_at=NOW - pd.Timedelta(minutes=5),
        halt_requested=halt_requested,
    )


def test_observe_mode_can_emit_research_proposal_but_never_order() -> None:
    decision = _evaluate()

    assert decision.action == "RESEARCH_PROPOSAL"
    assert decision.status == "OBSERVE_ONLY"
    assert decision.quantity == 3
    assert decision.modeled_maximum_loss == 300.0
    assert decision.sequence_directional_probability == 0.65
    assert set(decision.reason_codes) == {
        "OBSERVE_MODE",
        "MODEL_AUTHORITY_NOT_ACTIVE",
        "SHADOW_MODEL_NO_ORDER_AUTHORITY",
    }
    assert decision.automated_action_allowed is False
    assert decision.orders_enabled is False
    assert decision.orders_placed == 0


def test_deterministic_portfolio_broker_and_halt_gates_retain_authority() -> None:
    stale = _evaluate(
        broker=_broker(observed_at=NOW - pd.Timedelta(minutes=10))
    )
    unknown = _evaluate(broker=_broker(unknown_submission_status=True))
    daily_loss = _evaluate(portfolio=_portfolio(daily_pnl=-500.0))
    halt = _evaluate(halt_requested=True)
    projected_symbol_limit = _evaluate(
        portfolio=_portfolio(symbol_exposure={"AAPL": 4_950.0})
    )

    assert stale.action == "NO_TRADE"
    assert "BROKER_STATE_STALE" in stale.reason_codes
    assert unknown.action == "NO_TRADE"
    assert "UNKNOWN_BROKER_SUBMISSION" in unknown.reason_codes
    assert daily_loss.action == "HALT"
    assert daily_loss.mode == LoopCMode.HALT
    assert halt.action == "HALT"
    assert "HALT_REQUESTED" in halt.reason_codes
    assert projected_symbol_limit.action == "NO_TRADE"
    assert projected_symbol_limit.reason_codes == (
        "NO_CANDIDATE_PASSED_RISK_GATES",
    )
    for decision in (stale, unknown, daily_loss, halt, projected_symbol_limit):
        assert decision.orders_placed == 0


def test_predictive_thresholds_are_selected_by_exact_horizon() -> None:
    base = _limits()
    strict = LoopCRiskLimits(
        **{
            **asdict(base),
            "predictive_thresholds_by_horizon": {
                **asdict(base)["predictive_thresholds_by_horizon"],
                "1h": {
                    **asdict(base)["predictive_thresholds_by_horizon"]["1h"],
                    "minimum_strategy_calibrated_probability": 0.80,
                },
            },
        }
    )
    candidate = _candidates().assign(horizon="1h")

    decision = evaluate_loop_c(
        candidate,
        decision_timestamp=NOW,
        mode=LoopCMode.OBSERVE,
        market_session_open=True,
        portfolio=_portfolio(),
        broker=_broker(),
        risk_limits=strict,
        model_authority="SHADOW_ONLY",
        model_published_at=NOW - pd.Timedelta(minutes=5),
    )

    assert decision.action == "NO_TRADE"
    assert decision.reason_codes == ("OPTIONS_SHADOW_HORIZON_BELOW_1D",)


def test_exact_sequence_model_binding_rejects_configuration_drift() -> None:
    binding = expected_sequence_model_binding()
    publication = SimpleNamespace(
        manifest={
            "model_name": binding.model_name,
            "configuration": {
                "policy_version": binding.sequence_policy_version,
                "configuration": SequenceEncoderConfig().semantic_contract(),
                "consumers": [binding.consumer],
            },
        },
        receipt={"authority": binding.required_authority},
    )

    summary = _validate_sequence_model_binding(publication, binding)
    assert summary["configuration_fingerprint"] == binding.configuration_fingerprint

    publication.manifest["configuration"]["configuration"] = {  # type: ignore[index]
        **SequenceEncoderConfig().semantic_contract(),
        "hidden_size": 31,
    }

    with pytest.raises(ValueError, match="differs from the approved"):
        _validate_sequence_model_binding(publication, binding)


def test_loop_c_consumes_strategy_probability_and_sequence_distribution_separately() -> None:
    decision = pd.Timestamp("2026-08-03T15:35:00Z")
    candidates = pd.DataFrame(
        {
            "id": ["bull", "bear", "neutral"],
            "symbol": ["AAPL"] * 3,
            "horizon": ["1d"] * 3,
            "decision_timestamp": [decision] * 3,
            "calibrated_profit_probability": [0.70, 0.70, 0.70],
            "net_delta": [0.40, -0.40, 0.0],
            "expected_return_on_risk": [0.10, 0.10, 0.10],
            "max_loss": [100.0] * 3,
            "capital_required": [200.0] * 3,
        }
    )
    distributions = pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "horizon": ["1d"],
            "decision_timestamp": [decision],
            "calibrated_probability_up": [0.80],
            "expected_return": [0.02],
            "return_quantile_10": [-0.01],
            "return_quantile_90": [0.03],
            "total_uncertainty": [0.04],
        }
    )

    merged = _merge_candidates(candidates, distributions).set_index("id")

    assert merged.loc["bull", "calibrated_probability"] == 0.70
    assert merged.loc["bull", "sequence_directional_probability"] == 0.80
    assert merged.loc["bear", "sequence_directional_probability"] == pytest.approx(
        0.20
    )
    assert merged.loc["neutral", "sequence_directional_probability"] == 1.0
    assert merged.loc["bull", "sequence_adverse_return"] == -0.01
    assert merged.loc["bear", "sequence_adverse_return"] == -0.03
    assert merged.loc["neutral", "sequence_adverse_return"] == -0.03


def test_event_replay_depends_on_event_clocks_not_input_order_or_replay_speed() -> None:
    events = (
        MarketEvent.create(
            event_timestamp="2026-08-03T15:00:00Z",
            available_at="2026-08-03T15:00:03Z",
            source_sequence=2,
            event_type="QUOTE",
            payload={"value": 2},
        ),
        MarketEvent.create(
            event_timestamp="2026-08-03T15:00:00Z",
            available_at="2026-08-03T15:00:01Z",
            source_sequence=1,
            event_type="TRADE",
            payload={"value": 1},
        ),
        MarketEvent.create(
            event_timestamp="2026-08-03T15:01:00Z",
            available_at="2026-08-03T15:01:10Z",
            source_sequence=3,
            event_type="QUOTE",
            payload={"value": 3},
        ),
    )

    forward = replay_available_events(
        events,
        decision_time="2026-08-03T15:00:05Z",
    )
    reverse = replay_available_events(
        reversed(events),
        decision_time="2026-08-03T15:00:05Z",
    )

    assert forward == reverse
    assert [event.source_sequence for event in forward] == [1, 2]


def test_loop_c_publication_rejects_any_order_authority_drift(
    tmp_path: Path,
) -> None:
    run = tmp_path / "ml" / "loop-c-runs" / "20260803T160000Z"
    run.mkdir(parents=True)
    decisions = pd.DataFrame(
        {
            "automated_action_allowed": [False],
            "orders_enabled": [False],
            "orders_placed": [0],
        }
    )
    decisions.to_parquet(run / "decisions.parquet", index=False)
    (run / "report.json").write_text("{}\n", encoding="utf-8")
    write_manifest(
        run,
        run_timestamp=NOW,
        input_files=(),
        output_files=("decisions.parquet", "report.json"),
        configuration={
            "authority": "OBSERVE_ONLY",
            "orders_enabled": False,
            "orders_placed": 0,
        },
        datastore_root=tmp_path,
    )

    publication = publish_loop_c_observe_run(
        tmp_path,
        run_directory=run,
        published_at=NOW,
    )
    assert publication.receipt["authority"] == "OBSERVE_ONLY"
    assert read_current_loop_c_publication(tmp_path).run_directory == run

    decisions.assign(orders_placed=1).to_parquet(
        run / "decisions.parquet", index=False
    )
    with pytest.raises(Exception):
        read_current_loop_c_publication(tmp_path)


def test_explicit_approved_inputs_validate_without_account_identifiers(
    tmp_path: Path,
) -> None:
    portfolio_receipt = tmp_path / "accounts" / "portfolio" / "receipt.json"
    broker_receipt = tmp_path / "accounts" / "broker" / "receipt.json"
    portfolio_receipt.parent.mkdir(parents=True)
    broker_receipt.parent.mkdir(parents=True)
    portfolio_receipt.write_text("{}\n", encoding="utf-8")
    broker_receipt.write_text("{}\n", encoding="utf-8")
    paths = {
        name: tmp_path / f"{name}.json"
        for name in ("risk", "portfolio", "broker", "halt")
    }
    payloads = {
        "risk": {
            "schema_version": "loop-c-risk-approval-v2",
            "approval": {
                "status": "APPROVED",
                "approval_id": "operator-review-17",
                "approved_by": "operator",
                "approved_at": "2026-08-03T15:00:00Z",
                "expires_at": "2026-08-04T15:00:00Z",
                "scope": "LOOP_C_OBSERVE_ONLY",
                "rationale": "Bounded observe-only validation.",
            },
            "model_binding": {
                **asdict(expected_sequence_model_binding()),
                "horizons": list(expected_sequence_model_binding().horizons),
            },
            "limits": asdict(_limits()),
        },
        "portfolio": {
            "schema_version": "loop-c-portfolio-snapshot-v2",
            "authority": "OBSERVED_READ_ONLY",
            "observed_at": "2026-08-03T15:59:30Z",
            "reconciled": True,
            "account_equity": 12_000.0,
            "daily_pnl": 25.0,
            "gross_exposure": 2_000.0,
            "symbol_exposure": {"AAPL": 100.0},
            "open_positions": 2,
            "available_cash": 1_000.0,
            "available_cash_source": "MIN_NONMARGINABLE_AVAILABLE",
            "trade_history_status": "CURRENT_CONTEXT_ONLY",
            "source": {
                "policy_version": "loop-c-schwab-read-only-snapshot-v1",
                "receipt_path": str(portfolio_receipt.relative_to(tmp_path)),
                "receipt_sha256": file_checksum(portfolio_receipt),
            },
        },
        "broker": {
            "schema_version": "loop-c-broker-snapshot-v2",
            "authority": "OBSERVED_READ_ONLY",
            "observed_at": "2026-08-03T15:59:40Z",
            "reconciled": True,
            "working_orders": 0,
            "reserved_cash": 0.0,
            "unknown_submission_status": False,
            "source": {
                "policy_version": "loop-c-schwab-read-only-snapshot-v1",
                "receipt_path": str(broker_receipt.relative_to(tmp_path)),
                "receipt_sha256": file_checksum(broker_receipt),
            },
        },
        "halt": {
            "schema_version": "loop-c-halt-control-v2",
            "control_id": "open-session-17",
            "issued_at": "2026-08-03T15:00:00Z",
            "expires_at": "2026-08-04T15:00:00Z",
            "halt_requested": False,
            "set_by": "operator",
        },
    }
    for name, payload in payloads.items():
        paths[name].write_text(json.dumps(payload), encoding="utf-8")

    inputs = load_loop_c_inputs(
        tmp_path,
        risk_limits_path=paths["risk"],
        portfolio_snapshot_path=paths["portfolio"],
        broker_snapshot_path=paths["broker"],
        halt_control_path=paths["halt"],
        as_of=NOW,
    )

    assert inputs.public_summary["risk_approval_id"] == "operator-review-17"
    assert inputs.portfolio["available_cash"] == 1_000.0
    assert inputs.broker["working_orders"] == 0
    assert inputs.halt_requested is False
    assert portfolio_receipt.resolve() in inputs.source_files
    assert "approved_by" not in inputs.public_summary


def test_loop_c_inputs_fail_closed_on_pending_stale_or_unmanifested_state(
    tmp_path: Path,
) -> None:
    risk = tmp_path / "risk.json"
    risk.write_text(
        json.dumps(
            {
                "schema_version": "loop-c-risk-approval-v2",
                "approval": {
                    "status": "PENDING_OPERATOR_APPROVAL",
                    "approval_id": "pending-1",
                    "approved_by": "operator",
                    "approved_at": "2026-08-03T15:00:00Z",
                    "expires_at": "2026-08-04T15:00:00Z",
                    "scope": "LOOP_C_OBSERVE_ONLY",
                    "rationale": "Not approved yet.",
                },
                "model_binding": {
                    **asdict(expected_sequence_model_binding()),
                    "horizons": list(expected_sequence_model_binding().horizons),
                },
                "limits": asdict(_limits()),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="explicit APPROVED"):
        load_loop_c_inputs(
            tmp_path,
            risk_limits_path=risk,
            portfolio_snapshot_path=tmp_path / "missing-portfolio.json",
            broker_snapshot_path=tmp_path / "missing-broker.json",
            halt_control_path=tmp_path / "missing-halt.json",
            as_of=NOW,
        )


def test_rollout_gate_allows_review_only_after_prospective_evidence() -> None:
    evidence = LoopCShadowEvidence(
        completed_xnys_sessions=40,
        mature_decision_clusters={"1h": 60, "4h": 60, "1d": 30},
        nonoverlapping_weekly_cohorts=8,
        reconciled_observations=20,
        halt_drills_passed=2,
        rollback_drills_passed=1,
        calibration_gates_passed=True,
        interval_coverage_gates_passed=True,
        cost_latency_missing_data_stress_passed=True,
        symbol_and_regime_stability_passed=True,
        publication_integrity_passed=True,
        paper_broker_reconciliation_passed=True,
        deterministic_gate_violations=0,
        orders_placed=0,
    )

    immature = evaluate_loop_c_rollout(
        LoopCShadowEvidence(
            **{
                **evidence.__dict__,
                "completed_xnys_sessions": 39,
                "nonoverlapping_weekly_cohorts": 7,
            }
        )
    )
    ready = evaluate_loop_c_rollout(evidence)

    assert immature.status == "OBSERVE_ONLY_EVIDENCE_ACCUMULATING"
    assert "MINIMUM_40_COMPLETED_XNYS_SESSIONS" in immature.failed_gates
    assert "MINIMUM_8_NONOVERLAPPING_WEEKLY_COHORTS" in immature.failed_gates
    assert ready.status == "ELIGIBLE_FOR_OPERATOR_REVIEW"
    assert ready.eligible_for_operator_review is True
    assert ready.authority_expansion_allowed is False
    assert ready.automatic_promotion_allowed is False
