from __future__ import annotations

import json

import pandas as pd
import pytest

from ml.strategy_pricing_canary import (
    StrategyPricingCanaryError,
    _candidate_target,
    _strategy_checks,
)
from ml.strategy_selection.contracts import (
    OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
    SCENARIO_PRIOR_SCORE_BASIS,
)


def _heuristic_row() -> dict[str, object]:
    return {
        "score_basis": SCENARIO_PRIOR_SCORE_BASIS,
        "scenario_coverage_score": 1.0,
        "raw_profit_probability": float("nan"),
        "calibrated_profit_probability": float("nan"),
        "decision_score": float("nan"),
        "pricing_status": "Black-Scholes fallback",
        "pricing_source": "BLACK_SCHOLES",
        "pricing_leg_coverage": 1.0,
        "pricing_missing_reason": "",
        "surface_quality_pass": False,
        "liquidity_policy_pass": False,
        "all_option_quotes_valid": True,
    }


def test_canary_treats_all_positive_scenario_as_nonprobabilistic() -> None:
    report = _strategy_checks(pd.DataFrame([_heuristic_row()]))

    assert report["all_positive_scenario_rows_without_probability"] == 1
    assert report["calibrated_candidate_rows"] == 0
    assert report["fully_priced_candidate_rows"] == 1
    assert report["quality_warning_rows"] == 1


def test_canary_rejects_heuristic_probability_masquerade() -> None:
    row = _heuristic_row()
    row["decision_score"] = 1.0

    with pytest.raises(StrategyPricingCanaryError, match="masquerading"):
        _strategy_checks(pd.DataFrame([row]))


def test_canary_accepts_quality_passing_opra_execution_probability() -> None:
    row = _heuristic_row()
    row.update(
        {
            "score_basis": OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
            "raw_profit_probability": 0.25,
            "calibrated_profit_probability": 0.2,
            "decision_score": 0.2,
            "pricing_status": "Unavailable",
            "pricing_source": "UNAVAILABLE",
            "pricing_leg_coverage": 0.0,
            "pricing_mode": "ACTIVE",
            "max_relative_spread": 0.1,
            "maximum_quote_staleness_seconds": 600.0,
            "minimum_open_interest": 5.0,
            "total_volume": 0.0,
        }
    )

    report = _strategy_checks(pd.DataFrame([row]))

    assert report["fully_priced_candidate_rows"] == 0
    assert report["opra_execution_scored_rows"] == 1
    assert report["opra_execution_quality_passing_rows"] == 1
    assert report["calibrated_evidence_quality_rows"] == 1


def test_canary_requires_one_exact_target_across_option_legs() -> None:
    exact = json.dumps(
        [
            {
                "asset": "OPTION",
                "target_snapshot_for": "2026-08-18T20:00:00Z",
            },
            {
                "asset": "OPTION",
                "target_snapshot_for": "2026-08-18T20:00:00Z",
            },
        ]
    )
    mismatch = json.dumps(
        [
            {
                "asset": "OPTION",
                "target_snapshot_for": "2026-08-18T20:00:00Z",
            },
            {
                "asset": "OPTION",
                "target_snapshot_for": "2026-08-18T20:15:00Z",
            },
        ]
    )

    assert _candidate_target(exact) == pd.Timestamp("2026-08-18T20:00:00Z")
    assert _candidate_target(mismatch) is None
