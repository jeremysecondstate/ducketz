from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from ml.artifacts import write_manifest
from ml.loop_c.publication import publish_loop_c_observe_run
from ml.loop_c.risk_proposal import build_pending_risk_proposal
from ml.loop_c.operator_controls import issue_loop_c_operator_controls
from ml.loop_c.schwab_snapshot import capture_schwab_read_only_state
from ml.loop_c.weekly_review import (
    build_loop_c_weekly_review,
    read_current_loop_c_weekly_review,
    resolve_weekly_review_window,
)


def _fixture() -> dict[str, Any]:
    path = Path(__file__).parent / "fixtures" / "schwab_account_and_orders.json"
    return json.loads(path.read_text(encoding="utf-8"))


class _ReadOnlySession:
    def __init__(self) -> None:
        self.payload = _fixture()
        self.calls: list[str] = []

    def get_account(self) -> object:
        self.calls.append("get_account")
        return self.payload["account_payload"]

    def get_open_orders(self) -> object:
        self.calls.append("get_open_orders")
        return self.payload["open_orders_payload"]

    def get_orders(self, **_kwargs: object) -> list[object]:
        self.calls.append("get_orders")
        return []

    def get_transactions(self, **_kwargs: object) -> list[object]:
        self.calls.append("get_transactions")
        return []

    def submit_order(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("The read-only snapshot must never submit an order")

    def replace_order(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("The read-only snapshot must never replace an order")

    def cancel_order(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("The read-only snapshot must never cancel an order")


def test_schwab_snapshot_is_sanitized_receipted_and_get_only(tmp_path: Path) -> None:
    session = _ReadOnlySession()

    result = capture_schwab_read_only_state(
        tmp_path,
        observed_at="2026-08-03T16:00:00Z",
        session_factory=lambda: session,
    )

    assert result.reconciled is True
    assert set(session.calls) == {
        "get_account",
        "get_open_orders",
        "get_orders",
        "get_transactions",
    }
    portfolio = json.loads(result.portfolio_snapshot_path.read_text(encoding="utf-8"))
    broker = json.loads(result.broker_snapshot_path.read_text(encoding="utf-8"))
    facts = (result.run_directory / "sanitized-account-facts.json").read_text(
        encoding="utf-8"
    )
    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))

    assert portfolio["account_equity"] == pytest.approx(30_300.0)
    assert portfolio["gross_exposure"] == pytest.approx(10_300.0)
    assert portfolio["available_cash"] == pytest.approx(14_815.0)
    assert portfolio["available_cash_source"] == "MIN_NONMARGINABLE_AVAILABLE"
    assert portfolio["open_positions"] == 2
    assert broker["working_orders"] == 3
    assert broker["reserved_cash"] == pytest.approx(1_685.0)
    assert "12345678" not in facts
    assert "9001" not in facts
    assert "9002" not in facts
    assert "9003" not in facts
    assert receipt["sanitization"]["account_identifiers_persisted"] is False
    assert receipt["safety"]["broker_data_http_methods"] == ["GET"]
    assert receipt["safety"]["orders_placed"] == 0


def test_pending_proposal_derives_caps_but_cannot_approve_itself(tmp_path: Path) -> None:
    session = _ReadOnlySession()
    capture_schwab_read_only_state(
        tmp_path,
        observed_at="2026-08-03T16:00:00Z",
        session_factory=lambda: session,
    )

    result = build_pending_risk_proposal(
        tmp_path,
        as_of="2026-08-03T16:01:00Z",
    )
    approval = json.loads(Path(result["risk_approval_path"]).read_text(encoding="utf-8"))
    calculus = json.loads(Path(result["calculus_path"]).read_text(encoding="utf-8"))

    assert result["status"] == "PENDING_OPERATOR_APPROVAL"
    assert approval["approval"]["status"] == "PENDING_OPERATOR_APPROVAL"
    assert approval["approval"]["approved_by"] is None
    assert approval["limits"]["maximum_daily_loss"] == pytest.approx(151.50)
    assert approval["limits"]["maximum_trade_loss"] == pytest.approx(37.88)
    assert approval["limits"]["maximum_candidate_quantity"] == 1
    assert set(approval["limits"]["predictive_thresholds_by_horizon"]) == {
        "1h",
        "4h",
        "1d",
        "1w",
    }
    assert len(approval["model_binding"]["configuration_fingerprint"]) == 64
    assert calculus["history_governance"]["usable_to_increase_risk"] is False
    assert calculus["safety"]["orders_enabled"] is False
    assert calculus["safety"]["orders_placed"] == 0


def test_operator_controls_issue_only_the_verified_weekly_observe_lease(
    tmp_path: Path,
) -> None:
    session = _ReadOnlySession()
    capture_schwab_read_only_state(
        tmp_path,
        observed_at="2026-08-03T16:00:00Z",
        session_factory=lambda: session,
    )
    pending = build_pending_risk_proposal(
        tmp_path,
        as_of="2026-08-03T16:01:00Z",
    )

    result = issue_loop_c_operator_controls(
        tmp_path,
        pending_risk_approval_path=Path(pending["risk_approval_path"]),
        approved_by="operator:pytest-owner",
        approved_at="2026-08-03T16:02:00Z",
        expires_at="2026-08-08T00:00:00Z",
        rationale="Approve the immutable weekly observe-only proposal.",
        halt_requested=False,
    )
    approval = json.loads(
        Path(result["risk_approval_path"]).read_text(encoding="utf-8")
    )
    halt = json.loads(Path(result["halt_control_path"]).read_text(encoding="utf-8"))
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))

    assert result["status"] == "OBSERVE_ONLY_LEASE_ISSUED"
    assert approval["approval"]["status"] == "APPROVED"
    assert approval["approval"]["scope"] == "LOOP_C_OBSERVE_ONLY"
    assert approval["limits"]["maximum_daily_loss"] == pytest.approx(151.50)
    assert halt["halt_requested"] is False
    assert receipt["safety"] == {
        "authority": "OBSERVE_ONLY",
        "automated_action_allowed": False,
        "broker_submission_path_present": False,
        "orders_enabled": False,
        "orders_placed": 0,
    }


def test_operator_controls_reject_non_friday_pilot_expiry(tmp_path: Path) -> None:
    session = _ReadOnlySession()
    capture_schwab_read_only_state(
        tmp_path,
        observed_at="2026-08-03T16:00:00Z",
        session_factory=lambda: session,
    )
    pending = build_pending_risk_proposal(
        tmp_path,
        as_of="2026-08-03T16:01:00Z",
    )

    with pytest.raises(ValueError, match="Friday at 17:00"):
        issue_loop_c_operator_controls(
            tmp_path,
            pending_risk_approval_path=Path(pending["risk_approval_path"]),
            approved_by="operator:pytest-owner",
            approved_at="2026-08-03T16:02:00Z",
            expires_at="2026-08-07T23:00:00Z",
            rationale="This expiry is intentionally one hour early.",
            halt_requested=False,
        )


def test_weekly_review_separates_account_context_and_never_changes_controls(
    tmp_path: Path,
) -> None:
    session = _ReadOnlySession()
    snapshot = capture_schwab_read_only_state(
        tmp_path,
        observed_at="2026-08-08T16:00:00Z",
        review_period_start=pd.Timestamp("2026-08-03").date(),
        session_factory=lambda: session,
    )
    pending = build_pending_risk_proposal(
        tmp_path,
        as_of="2026-08-08T16:01:00Z",
    )

    result = build_loop_c_weekly_review(
        tmp_path,
        reviewed_at="2026-08-08T16:01:00Z",
        schwab_run_directory=snapshot.run_directory,
        pending_risk_proposal=pending,
    )
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    pointer = json.loads(
        (
            tmp_path / "ml" / "loop-c-weekly-review-latest" / "review.json"
        ).read_text(encoding="utf-8")
    )

    assert result.status == "INSUFFICIENT_LOOP_C_OBSERVATIONS"
    assert report["window"]["first_completed_session"] == "2026-08-03"
    assert report["window"]["last_completed_session"] == "2026-08-07"
    assert (
        report["actual_account_context"]["attribution"]
        == "ACCOUNT_OPTIONS_CONTEXT_NOT_LOOP_C_ATTRIBUTED"
    )
    assert report["loop_c"]["actual_broker_performance"] == {
        "status": "NOT_APPLICABLE_OBSERVE_ONLY",
        "attributed_trade_count": 0,
        "attributed_realized_pnl": 0.0,
        "reason": "Loop C has no broker submission path and every verified run placed zero orders.",
    }
    assert report["pending_risk_proposal"]["status"] == "PENDING_OPERATOR_APPROVAL"
    assert report["operator_review"]["automatic_change_allowed"] is False
    assert report["safety"]["orders_placed"] == 0
    assert pointer["current"]["status"] == result.status
    assert read_current_loop_c_weekly_review(tmp_path).run_directory == result.run_directory
    assert not (tmp_path / "controls" / "loop-c" / "current" / "risk-approval.json").exists()
    assert not (tmp_path / "controls" / "loop-c" / "current" / "halt-control.json").exists()


def test_weekly_window_uses_last_completed_exchange_session() -> None:
    window = resolve_weekly_review_window("2026-08-08T16:00:00Z")

    assert window.calendar_week_start.isoformat() == "2026-08-03"
    assert window.first_session.isoformat() == "2026-08-03"
    assert window.last_session.isoformat() == "2026-08-07"


def test_weekly_review_keeps_immature_shadow_proposals_out_of_pnl(
    tmp_path: Path,
) -> None:
    session = _ReadOnlySession()
    snapshot = capture_schwab_read_only_state(
        tmp_path,
        observed_at="2026-08-08T16:00:00Z",
        review_period_start=pd.Timestamp("2026-08-03").date(),
        session_factory=lambda: session,
    )
    run = tmp_path / "ml" / "loop-c-runs" / "20260803T160000.000000Z"
    run.mkdir(parents=True)
    decision = {
        "decision_timestamp": "2026-08-03T16:00:00Z",
        "mode": "OBSERVE",
        "action": "RESEARCH_PROPOSAL",
        "status": "OBSERVE_ONLY",
        "reason_codes": ["OBSERVE_MODE", "SHADOW_MODEL_NO_ORDER_AUTHORITY"],
        "candidate_id": "candidate-1",
        "symbol": "AAPL",
        "horizon": "1d",
        "quantity": 1,
        "calibrated_probability": 0.65,
        "sequence_directional_probability": 0.60,
        "sequence_expected_return": 0.02,
        "sequence_adverse_return": -0.01,
        "expected_return_on_risk": 0.10,
        "total_uncertainty": 0.03,
        "expected_utility": 0.055,
        "modeled_maximum_loss": 100.0,
        "automated_action_allowed": False,
        "orders_enabled": False,
        "orders_placed": 0,
        "policy_version": "loop-c-hourly-policy-v2",
    }
    pd.DataFrame([decision]).to_parquet(run / "decisions.parquet", index=False)
    report = {
        "schema_version": "loop-c-observe-report-v1",
        "status": "OBSERVE_ONLY",
        "decision": decision,
        "sequence_consumer": {
            "status": "READY_SHADOW",
            "model_binding": {
                "model_name": "pooled-causal-sequence-encoder",
                "configuration_fingerprint": "a" * 64,
            },
        },
        "input_contracts": {
            "sequence_configuration_fingerprint": "a" * 64,
            "risk_approval_id": "weekly-approval-1",
            "risk_approval_expires_at": "2026-08-08T00:00:00Z",
        },
        "safety": {
            "authority": "OBSERVE_ONLY",
            "orders_enabled": False,
            "orders_placed": 0,
            "broker_submission_path_present": False,
            "halt_requested": False,
        },
    }
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    write_manifest(
        run,
        run_timestamp="2026-08-03T16:00:00Z",
        input_files=(),
        output_files=("decisions.parquet", "report.json"),
        configuration={
            "authority": "OBSERVE_ONLY",
            "orders_enabled": False,
            "orders_placed": 0,
            "risk_limits": {
                "policy_version": "loop-c-hourly-policy-v2",
                "maximum_trade_loss": 100.0,
                "predictive_thresholds_by_horizon": {
                    "1d": {"minimum_strategy_calibrated_probability": 0.62}
                },
            },
            "strategy_source": {"run_path": "ml/strategy-runs/source-1"},
        },
        datastore_root=tmp_path,
    )
    publish_loop_c_observe_run(
        tmp_path,
        run_directory=run,
        published_at="2026-08-03T16:00:01Z",
    )

    result = build_loop_c_weekly_review(
        tmp_path,
        reviewed_at="2026-08-08T16:01:00Z",
        schwab_run_directory=snapshot.run_directory,
    )
    weekly = json.loads(result.report_path.read_text(encoding="utf-8"))
    shadow = weekly["loop_c"]["shadow_counterfactual_performance"]

    assert result.status == "INSUFFICIENT_MATURE_LOOP_C_OUTCOMES"
    assert shadow["proposal_count"] == 1
    assert shadow["mature_receipt_matched_proposals"] == 0
    assert shadow["pending_or_unavailable_proposals"] == 1
    assert shadow["net_counterfactual_pnl"] == 0.0
    assert (
        shadow["observations"][0]["outcome_status"]
        == "PENDING_OR_UNAVAILABLE"
    )
    assert weekly["safety"]["orders_placed"] == 0
