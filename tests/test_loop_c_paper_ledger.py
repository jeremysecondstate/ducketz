from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ml.loop_c.paper_ledger import (
    _attach_outcome,
    _summarize,
    build_paper_trade_snapshot,
    paper_candidate_has_bounded_exit,
    read_current_paper_ledger,
    track_options_strategy_paper_trades,
)


def _candidate() -> dict[str, object]:
    return {
        "id": "AAPL|1w|candidate",
        "candidate_key": "iron_condor|front=2026-09-11",
        "symbol": "AAPL",
        "horizon": "1w",
        "decision_timestamp": "2026-09-04T20:05:00Z",
        "target_window_start": "2026-09-08T13:30:00Z",
        "target_window_end": "2026-09-11T20:00:00Z",
        "entry_available_at": "2026-09-04T20:05:30Z",
        "strategy_name": "iron_condor",
        "strategy_display_name": "Iron Condor",
        "strategy_family": "RANGE",
        "candidate_rank": 1,
        "legs_json": json.dumps(
            [
                {
                    "asset": "OPTION",
                    "contract_symbol": "AAPL_TEST_CALL",
                    "side": "SHORT",
                    "option_type": "CALL",
                    "expiration_date": "2026-09-11T00:00:00Z",
                    "quantity": 1,
                    "multiplier": 100.0,
                    "bid": 2.0,
                    "ask": 2.1,
                },
                {
                    "asset": "OPTION",
                    "contract_symbol": "AAPL_TEST_CALL_WING",
                    "side": "LONG",
                    "option_type": "CALL",
                    "expiration_date": "2026-09-11T00:00:00Z",
                    "quantity": 1,
                    "multiplier": 100.0,
                    "bid": 0.8,
                    "ask": 0.9,
                },
            ],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "entry_cash_flow": 100.0,
        "entry_net_credit": 98.70,
        "entry_net_debit": 0.0,
        "entry_fees": 1.30,
        "capital_required": 400.0,
        "max_loss": 400.0,
        "score_basis": "OPRA_EXECUTION_CALIBRATED_MODEL",
        "pricing_source": "BSGP",
        "pricing_status": "ACTIVE",
        "pricing_leg_coverage": 1.0,
        "model_version": "model-1",
        "candidate_policy_version": "candidate-1",
        "model_policy_version": "model-policy-1",
        "ranking_policy_version": "ranking-1",
    }


def _decision() -> dict[str, object]:
    return {
        "decision_timestamp": "2026-09-04T20:06:00Z",
        "candidate_id": "AAPL|1w|candidate",
        "quantity": 2,
        "calibrated_probability": 0.68,
        "sequence_directional_probability": 0.63,
        "sequence_expected_return": 0.02,
        "sequence_adverse_return": -0.01,
        "expected_return_on_risk": 0.12,
        "total_uncertainty": 0.03,
        "expected_utility": 0.0675,
    }


def test_paper_trade_snapshot_freezes_exact_generated_strategy() -> None:
    snapshot = build_paper_trade_snapshot(
        _candidate(),
        decision=_decision(),
        strategy_run_path="ml/strategy-runs/source-1",
        loop_c_run_path="ml/loop-c-runs/observe-1",
    )

    assert snapshot["horizon"] == "1w"
    assert snapshot["horizon_label"] == "Remaining-Week Aggregate"
    assert snapshot["strategy_display_name"] == "Iron Condor"
    assert len(snapshot["exact_legs"]) == 2
    assert snapshot["entry_assumptions"]["total_capital_required"] == 800.0
    assert snapshot["entry_assumptions"]["total_entry_fees"] == 2.60
    assert (
        snapshot["expiration_and_assignment"][
            "gross_potential_share_obligation_total"
        ]
        == 400.0
    )
    obligations = snapshot["expiration_and_assignment"]["option_leg_obligations"]
    assert obligations[0]["exercise_or_assignment_event"] == "ASSIGNMENT"
    assert obligations[0]["potential_share_change_direction"] == "SELL_SHARES"
    assert obligations[0]["potential_signed_share_change_total"] == -200.0
    assert obligations[1]["exercise_or_assignment_event"] == "EXERCISE"
    assert obligations[1]["potential_signed_share_change_total"] == 200.0
    assert (
        snapshot["expiration_and_assignment"][
            "net_share_change_if_every_leg_exercised_or_assigned_total"
        ]
        == 0.0
    )
    assert snapshot["expiration_and_assignment"]["paper_only_no_assignment_occurs"]
    assert snapshot["expiration_and_assignment"]["planned_exit_at"] == (
        "2026-09-11T20:00:00+00:00"
    )
    assert snapshot["expiration_and_assignment"]["future_live_exit_buffer_required"]
    assert (
        snapshot["expiration_and_assignment"][
            "future_live_options_authority_present"
        ]
        is False
    )
    assert len(snapshot["paper_trade_id"]) == 64
    assert snapshot["orders_enabled"] is False
    assert snapshot["orders_placed"] == 0
    assert paper_candidate_has_bounded_exit(_candidate()) is True

    lifecycle = {**_candidate(), "lifecycle": True}
    assert paper_candidate_has_bounded_exit(lifecycle) is False

    expired = _candidate()
    expired["target_window_end"] = "2026-09-14T20:00:00Z"
    assert paper_candidate_has_bounded_exit(expired) is False


def test_daily_tracker_publishes_empty_zero_order_ledger(tmp_path: Path) -> None:
    publication = track_options_strategy_paper_trades(
        tmp_path, tracked_at="2026-09-05T07:17:00Z"
    )
    reread = read_current_paper_ledger(tmp_path)

    assert publication.report["status"] == "NO_PAPER_TRADES_YET"
    assert publication.report["summary"]["paper_trade_count"] == 0
    assert publication.report["summary"]["orders_placed"] == 0
    assert publication.report["safety"]["broker_contact_performed"] is False
    assert reread.run_directory == publication.run_directory


def test_daily_tracker_matches_only_exact_mature_strategy_outcome() -> None:
    snapshot = build_paper_trade_snapshot(
        _candidate(),
        decision=_decision(),
        strategy_run_path="ml/strategy-runs/source-1",
        loop_c_run_path="ml/loop-c-runs/observe-1",
    )
    outcomes = pd.DataFrame(
        [
            {
                "strategy_run_path": "ml/strategy-runs/source-1",
                "symbol": "AAPL",
                "horizon": "1w",
                "decision_timestamp": "2026-09-04T20:05:00Z",
                "candidate_key": "iron_condor|front=2026-09-11",
                "realized_net_profit_usd": 125.0,
                "exit_available_at": "2026-09-11T20:00:05Z",
                "outcome_policy_version": "strategy-outcome-1",
                "round_trip_contract_fees_usd": 2.60,
                "total_bid_ask_spread_usd": 40.0,
            }
        ]
    )

    tracked = _attach_outcome(
        snapshot,
        outcomes=outcomes,
        tracked_at=pd.Timestamp("2026-09-12T07:17:00Z"),
    )

    assert tracked["tracking"]["lifecycle_status"] == "MATURE_RECEIPT_MATCHED"
    assert tracked["tracking"]["counterfactual_realized_net_pnl"] == 250.0
    assert (
        tracked["tracking"]["attribution"]
        == "LOOP_C_COUNTERFACTUAL_NOT_BROKER_EXECUTION"
    )


def test_paper_ledger_summarizes_open_assignment_obligations() -> None:
    snapshot = build_paper_trade_snapshot(
        _candidate(),
        decision=_decision(),
        strategy_run_path="ml/strategy-runs/source-1",
        loop_c_run_path="ml/loop-c-runs/observe-1",
    )
    tracked = _attach_outcome(
        snapshot,
        outcomes=pd.DataFrame(),
        tracked_at=pd.Timestamp("2026-09-05T07:17:00Z"),
    )

    summary = _summarize([tracked], run_summary={"verified_observe_runs": 1})

    assert summary["open_paper_trade_count"] == 1
    assert summary["open_gross_potential_share_obligation"] == 400.0
    assert summary["maximum_single_open_trade_gross_share_obligation"] == 400.0
    assert summary["open_potential_buy_share_obligation"] == 200.0
    assert summary["open_potential_sell_share_obligation"] == 200.0
    assert summary["earliest_open_option_expiration"] == (
        "2026-09-11T00:00:00+00:00"
    )
    assert summary["future_live_exit_buffer_required"] is True
