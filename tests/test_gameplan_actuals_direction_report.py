"""Saved recipe metadata controls report presentation without changing scores."""
from copy import deepcopy

import pandas as pd
import pytest

from ml.gameplan_actuals_review import render_actuals_review


def saved_results():
    common = {
        "symbol": "AAPL", "direction": "BULLISH", "calibrated_probability": .7,
        "model_status": "PROMOTED", "probability_target_contract": "raw-price-direction-v1",
        "gameplan_recipe_version": "direction-live-quote-v1",
        "execution_quantity_basis": "live_broker_capacity",
        "actual_start_price": 100., "actual_end_price": None, "actual_return": None,
        "actual_start_status": "OBSERVED", "actual_end_status": "NO_OBSERVATION",
        "actual_end_source_coverage": "UNKNOWN", "direction_correct": None,
        "direction_result": "Awaiting data", "model_brier_score": None,
    }
    forecasts = pd.DataFrame([
        {**common, "id": "scored", "route": "1h@04:00",
         "target_window_start": "2026-10-27T11:00Z", "target_window_end": "2026-10-27T12:00Z",
         "actuals_status": "EVALUATED", "actual_end_price": 105., "actual_return": .05,
         "actual_end_status": "OBSERVED", "direction_correct": True,
         "direction_result": "Correct", "model_brier_score": .09},
        {**common, "id": "missing", "route": "1h@05:00",
         "target_window_start": "2026-10-27T12:00Z", "target_window_end": "2026-10-27T13:00Z",
         "actuals_status": "MATURE_AWAITING_DATA"},
        {**common, "id": "pending", "route": "1w@D+5",
         "target_window_start": "2026-10-27T11:00Z", "target_window_end": "2026-11-03T01:00Z",
         "actuals_status": "PENDING_MATURITY", "actual_end_status": "PENDING_MATURITY"},
    ])
    prices = pd.DataFrame([
        {"symbol": "AAPL", "clock_local": "04:00", "comparison_status": "COMPARED",
         "planned_price_low": 99., "planned_price_mid": 100., "planned_price_high": 101.,
         "actual_price": 100., "actual_observed_at": "2026-10-27T11:00Z",
         "price_error": 0., "price_error_fraction": 0., "in_planned_range": True,
         "actual_status": "OBSERVED", "actual_source_coverage": "UNKNOWN"},
        {"symbol": "AAPL", "clock_local": "06:00", "comparison_status": "MATURE_AWAITING_DATA",
         "planned_price_low": 99., "planned_price_mid": 100., "planned_price_high": 101.,
         "actual_price": None, "actual_observed_at": None,
         "price_error": None, "price_error_fraction": None, "in_planned_range": None,
         "actual_status": "NO_OBSERVATION", "actual_source_coverage": "UNKNOWN"},
    ])
    report = {
        "action_date": "2026-10-27", "review_mode": "completed-session-before-training",
        "source_trade_plan_path": "saved/trade-plan",
        "forecasts": {"evaluated": 1, "pending_maturity": 1, "mature_awaiting_data": 1},
    }
    return forecasts, prices, report


ESTIMATE_TEXT = (
    "saved price estimates", "planning estimates", "with prices and quantities",
    "Mean absolute price error", "Prices within range", "Planned prices vs actual prices",
    "Saved price range", "Saved midpoint", "Actual − midpoint", "Difference %", "Range result",
)


def test_direction_only_report_omits_estimates_and_preserves_saved_results():
    forecasts, prices, report = saved_results()
    original_forecasts, original_prices, original_report = forecasts.copy(deep=True), prices.copy(deep=True), deepcopy(report)
    rendered = render_actuals_review(forecasts, prices, report)

    assert all(text not in rendered for text in ESTIMATE_TEXT)
    assert "[Original Gameplan](saved/trade-plan/Gameplan.md)" in rendered
    assert "| Stock | Correct / scored approved calls | Direction accuracy |" in rendered
    assert "| AAPL | 1 / 1 | 100.00% |" in rendered
    assert "1 evaluated · 1 still pending · 1 missing eligible price observations" in rendered
    assert "| Actual start | Actual end | Price move | Result |" in rendered
    assert "$100.00 | $105.00 | +5.00% | Correct" in rendered
    assert "Pending target end" in rendered
    assert "| Price 06:00 | No observed price on the required side of the clock; source request coverage is unknown |" in rendered
    assert "| 1h@05:00 end | No observed price on the required side of the clock; source request coverage is unknown |" in rendered
    pd.testing.assert_frame_equal(forecasts, original_forecasts)
    pd.testing.assert_frame_equal(prices, original_prices)
    assert report == original_report


def test_direction_only_report_does_not_require_estimate_columns():
    forecasts, prices, report = saved_results()
    prices = prices.drop(columns=["planned_price_low", "planned_price_mid", "planned_price_high",
                                  "price_error", "price_error_fraction", "in_planned_range"])
    rendered = render_actuals_review(forecasts, prices, report)
    assert "| AAPL | 1 / 1 | 100.00% |" in rendered
    assert "Price 06:00" in rendered
    assert all(text not in rendered for text in ESTIMATE_TEXT)


@pytest.mark.parametrize("recipe,basis", [
    (None, None), ("estimated-price-v1", "projected-ledger"),
    ("direction-live-quote-v1", None), ("direction-live-quote-v1", "projected-ledger"),
])
def test_historical_or_unrecorded_recipe_keeps_saved_price_report(recipe, basis):
    forecasts, prices, report = saved_results()
    for field, value in (("gameplan_recipe_version", recipe), ("execution_quantity_basis", basis)):
        if value is None:
            forecasts = forecasts.drop(columns=field)
        else:
            forecasts[field] = value
    # Current workflow policy must not replace the original forecast's saved recipe.
    report.update(gameplan_recipe_version="direction-live-quote-v1", execution_quantity_basis="live_broker_capacity")
    rendered = render_actuals_review(forecasts, prices, report)
    assert all(text in rendered for text in ESTIMATE_TEXT)
    assert "$99.00–$101.00 | $100.00 | $100.00" in rendered
    assert "1 evaluated · 1 still pending · 1 missing eligible price observations" in rendered


@pytest.mark.parametrize("saved_value", [pd.NA, "estimated-price-v1"])
def test_partial_recipe_metadata_does_not_hide_historical_estimates(saved_value):
    forecasts, prices, report = saved_results()
    forecasts["gameplan_recipe_version"] = forecasts["gameplan_recipe_version"].astype("string")
    forecasts.loc[1, "gameplan_recipe_version"] = saved_value
    rendered = render_actuals_review(forecasts, prices, report)
    assert all(text in rendered for text in ESTIMATE_TEXT)
