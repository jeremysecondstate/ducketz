from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from ml.artifacts import file_checksum
from ml.strategy_selection.contracts import StrategyModel
from ml.strategy_value_challenger import (
    STRATEGY_VALUE_CHALLENGER_RECEIPT_VERSION,
    _acceptance_gates,
    _artifact_policy,
    _exact_expiration_stress,
    _heuristic_reason_counts,
    _prior_support,
    _publish,
)


def _model_with_prior_support(lower: float, upper: float) -> StrategyModel:
    clipper = SimpleNamespace(
        lower_bounds_=[lower],
        upper_bounds_=[upper],
    )
    numeric = SimpleNamespace(named_steps={"clip": clipper})
    preprocess = SimpleNamespace(named_transformers_={"numeric": numeric})
    return_estimator = SimpleNamespace(named_steps={"preprocess": preprocess})
    return StrategyModel(
        horizon="1d",
        estimator=object(),
        return_estimator=return_estimator,
        calibrator=object(),
        numeric_features=("strategy_prior__expected_return_on_risk",),
        categorical_features=(),
        artifact_directory=Path("model"),
        offline_evaluation={},
    )


def test_prior_support_comes_from_train_fitted_return_pipeline() -> None:
    assert _prior_support(_model_with_prior_support(-0.8, -0.02)) == (
        -0.8,
        -0.02,
    )


def test_artifact_policy_is_replayed_from_promoted_manifest(tmp_path: Path) -> None:
    model = _model_with_prior_support(-0.8, -0.02)
    model = StrategyModel(
        **{
            **model.__dict__,
            "artifact_directory": tmp_path,
        }
    )
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "policy": {
                    "policy_id": "opra-first-spreads-v2",
                    "account_approval": "SPREADS",
                    "minimum_train_decisions": 252,
                    "calibration_decisions": 63,
                    "assessment_decisions": 63,
                    "candidate_width_steps": [1, 2],
                    "maximum_expiration_choices": 2,
                    "maximum_relative_bid_ask_spread": 0.35,
                    "minimum_open_interest": 1.0,
                    "maximum_quote_staleness_seconds": 900.0,
                    "per_contract_fee": 0.65,
                    "fee_schedule": "schwab-online-options-standard",
                    "fee_schedule_verified_on": "2026-08-01",
                    "buy_to_close_fee_waiver_applied": False,
                    "variable_exchange_regulatory_fees_included": False,
                    "random_state": 20260801,
                }
            }
        ),
        encoding="utf-8",
    )

    policy = _artifact_policy(model)

    assert policy.per_contract_fee == 0.65
    assert policy.candidate_width_steps == (1, 2)
    assert policy.assessment_decisions == 63


def test_exact_expiration_stress_uses_exact_terminal_payoff() -> None:
    row = {
        "lifecycle": False,
        "expiration_structure": "SINGLE",
        "risk_calculation_status": "EXPIRATION_PAYOFF_EXACT",
        "decision_timestamp": "2026-08-20T20:00:00Z",
        "target_window_end": "2026-08-21T20:00:00Z",
        "front_expiration": "2026-08-21T00:00:00Z",
        "direction_probability_up": 0.5,
        "market_expected_absolute_move": 0.0,
        "market_expected_realized_volatility": 0.2,
        "market_uncertainty": 1.0,
        "market_trend_persistence": 0.0,
        "market_mean_reversion_tendency": 0.0,
        "underlying_price": 100.0,
        "entry_cash_flow": -100.0,
        "capital_required": 100.0,
        "legs_json": json.dumps(
            [
                {
                    "asset": "OPTION",
                    "side": "LONG",
                    "quantity": 1,
                    "multiplier": 100.0,
                    "option_type": "CALL",
                    "strike": 90.0,
                }
            ]
        ),
    }

    result = _exact_expiration_stress(row)

    assert result["exact_expiration_stress_status"] == "AVAILABLE"
    assert result["exact_expiration_stress_net_profit"] == 900.0
    assert result["exact_expiration_stress_return_on_risk"] == 9.0
    assert result["exact_expiration_profitable_scenario_fraction"] == 1.0


def test_exact_expiration_stress_fails_closed_before_expiry() -> None:
    result = _exact_expiration_stress(
        {
            "lifecycle": False,
            "expiration_structure": "SINGLE",
            "risk_calculation_status": "EXPIRATION_PAYOFF_EXACT",
            "target_window_end": "2026-08-20T20:00:00Z",
            "front_expiration": "2026-08-21T00:00:00Z",
        }
    )

    assert result["exact_expiration_stress_status"] == (
        "TARGET_NOT_EXPIRATION_SESSION"
    )
    assert pd.isna(result["exact_expiration_stress_return_on_risk"])


def test_acceptance_blocks_absent_causal_greek_support() -> None:
    live = {
        "probability_parity_max_absolute_error": 0.0,
        "production_return_parity_max_absolute_error": 0.0,
        "production_candidate_fields_changed": False,
        "production_candidate_ranks_changed": False,
        "production_value_audit": {"alert_rows": 10},
        "shadow_value_audit": {"alert_rows": 0},
    }
    assessment = {
        "production_expected_return": {
            "mean_absolute_error": 0.3,
            "root_mean_squared_error": 0.4,
        },
        "challenger_expected_return": {
            "mean_absolute_error": 0.2,
            "root_mean_squared_error": 0.3,
        },
        "production_probability_first_ranking": {
            "mean_realized_return_on_risk": -0.01,
            "total_net_profit": -10.0,
        },
        "challenger_probability_first_ranking": {
            "mean_realized_return_on_risk": -0.01,
            "total_net_profit": -10.0,
        },
    }
    no_greeks = {"positive_prior_rows": 0, "all_greeks_finite_rows": 0}

    result = _acceptance_gates(
        live_report=live,
        assessment_report=assessment,
        training_coverage=no_greeks,
        calibration_coverage=no_greeks,
        assessment_coverage=no_greeks,
    )

    assert result["status"] == "BLOCKED_KEEP_CURRENT_AUTHORITY"
    assert result["promotion_eligible"] is False
    assert "INSUFFICIENT_CAUSAL_GREEK_PRIOR_SUPPORT" in result[
        "blocking_reasons"
    ]
    assert result["automatic_promotion_allowed"] is False


def test_heuristic_missing_reasons_are_aggregated_without_contract_symbols() -> None:
    counts = _heuristic_reason_counts(
        pd.Series(
            [
                "AAPL  260824C00100000:TARGET_EVENT_STALE;"
                "AAPL  260824P00100000:PREDICTION_MISSING",
                "MSFT  260824C00400000:TARGET_EVENT_STALE",
            ]
        )
    )

    assert counts == {"PREDICTION_MISSING": 1, "TARGET_EVENT_STALE": 2}


def test_publish_creates_receipted_shadow_run_without_authority_pointer(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("immutable evidence\n", encoding="utf-8")
    result = _publish(
        tmp_path,
        created=pd.Timestamp("2026-08-24T10:00:00Z"),
        report={
            "schema_version": "test",
            "status": "COMPLETE_SHADOW_ONLY",
            "orders_placed": 0,
        },
        shadow_candidates=pd.DataFrame({"candidate_key": ["one"]}),
        assessment=pd.DataFrame({"candidate_key": ["past"]}),
        source_files=(source,),
    )

    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert receipt["schema_version"] == STRATEGY_VALUE_CHALLENGER_RECEIPT_VERSION
    assert receipt["orders_placed"] == 0
    assert receipt["promotion_performed"] is False
    assert receipt["authority_pointer_written"] is False
    assert receipt["manifest_checksum_sha256"] == file_checksum(
        result.manifest_path
    )
    assert manifest["production_authority_mutation"] is False
    assert not (tmp_path / "ml" / "strategy-profit-training-latest").exists()
