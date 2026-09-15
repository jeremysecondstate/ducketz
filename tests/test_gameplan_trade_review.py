from copy import deepcopy
from dataclasses import asdict

import pandas as pd
import pytest

from ml.gameplan_trade_review import render_trade_review
from ml.stock_trader.contracts import StockTraderPolicy


def forecast(**changes):
    row = {
        "id": "AAPL:1h@04:00", "symbol": "AAPL", "route": "1h@04:00",
        "target_window_start": "2026-09-09T11:00:00Z", "target_window_end": "2026-09-09T12:00:00Z",
        "raw_probability": .5470597, "calibrated_probability": .5470597,
        "model_status": "PROMOTED", "direction": "NO_EDGE", "planning_direction": "BULLISH",
        "execution_eligible": True, "trade_quantity": 0, "scheduled_trade_quantity": 0,
        "projected_trade_quantity": 6, "projected_trade_budget": 1944.97,
        "trade_planning_reason": "NO_BULLISH_ENTRY_SIGNAL",
        "trade_price_low": 313.54, "trade_price_high": 318.45,
        "price_band_sample_count": 120, "price_band_status": "AVAILABLE",
    }
    return {**row, **changes}


def report():
    return {
        "action_date": "2026-09-09", "observed_at": "2026-09-09T05:00:00Z", "orders_placed": 0,
        "direction_up_threshold": .54, "direction_down_threshold": .46,
        "planned_notional": 0, "sizing_policy": asdict(StockTraderPolicy()),
        "snapshot": {
            "observed_at": "2026-09-09T04:59:00Z", "status": "OBSERVED", "cash_status": "CASH_ONLY_BOUNDED",
            "account_equity": 129665.06, "available_cash": 112081.48, "gross_exposure": 17583.58,
            "balances": {"cash_balance": 112081.48}, "reserved_cash": 0, "working_order_count": 0,
            "held_shares": {"AAPL": 1.25}, "symbol_exposure": {"AAPL": 396.975},
            "quotes": {"AAPL": {"price_reference": 316.34, "price_reference_source": "SCHWAB_LAST_TRADE",
                                 "price_reference_time": "2026-09-08T23:59:00Z"}},
            "reason_codes": [], "cash_reason_codes": [], "ownership": {"reason_codes": [], "status": "OBSERVED_CONSISTENT"},
        },
    }


def render(tmp_path, rows, *, details=None, model_reports=None):
    return render_trade_review(pd.DataFrame(rows), details or report(), model_reports or {}, source_gameplan=str(tmp_path))


def test_projection_survives_wait_and_model_assessment_without_becoming_a_scheduled_trade(tmp_path):
    rows = [forecast(), forecast(id="AAPL:1d@D+1", route="1d@D+1", model_status="RESEARCH_NOT_PROMOTED",
                                projected_trade_quantity=18, calibrated_probability=.61, planning_direction="BULLISH",
                                target_window_end="2026-09-10T00:00:00Z")]
    text = render(tmp_path, rows)
    assert "| Bullish | 6 | $313.54–$318.45 | Wait |" in text
    assert "| Bullish | 18 | $313.54–$318.45 | Model assessment pending |" in text
    assert "0 scheduled entries" in text and "Scheduled capital reserved: **$0.00**" in text
    assert "Only scheduled entries commit shared cash" in text
    assert "54.00%" in text and "46.00%" in text and "54.71%" in text


def test_fifty_percent_rule_is_rendered_without_ambiguous_inclusive_boundaries(tmp_path):
    details = {**report(), "direction_up_threshold": .5, "direction_down_threshold": .5,
               "direction_policy_version": "stock-direction-50-v2"}
    text = render_trade_review(pd.DataFrame([forecast()]), details, {}, source_gameplan=str(tmp_path))
    assert "Bullish above 50.00%" in text and "Bearish below 50.00%" in text
    assert "Neutral only at exactly 50.00%" in text
    assert "Neutral between them" not in text


def test_daily_dates_weekly_expiry_and_context_quantities_are_unambiguous(tmp_path):
    rows = [
        forecast(id="AAPL:1d@D+1", route="1d@D+1", target_window_end="2026-09-10T00:00:00Z"),
        forecast(id="AAPL:1d@D+2", route="1d@D+2", execution_eligible=False, projected_trade_quantity=None,
                 target_window_start="2026-09-10T11:00:00Z", target_window_end="2026-09-11T00:00:00Z"),
        forecast(id="AAPL:1w@D+5", route="1w@D+5", target_window_end="2026-09-16T00:00:00Z"),
        forecast(id="AAPL:4h@16:00", route="4h@16:00", target_window_start="2026-09-09T23:00:00Z",
                 target_window_end="2026-09-10T14:00:00Z"),
    ]
    text = render(tmp_path, rows)
    assert "| Daily · Day 2 · Sep 09 |" in text and "| Daily · Day 3 · Sep 10 |" in text
    assert "| Weekly · Days 2–6 · Sep 09–Sep 15 |" in text
    assert "Sep 15, 2026 17:00 PDT" in text and "Sep 10, 2026 07:00 PDT" in text
    context = next(line for line in text.splitlines() if line.startswith("| Daily · Day 3 · Sep 10 |"))
    assert "| Bullish | — |" in context and context.endswith("| Outlook |")
    assert "Day 1 is the completed session used to build this plan; the next exchange session is Day 2." in text
    assert "| 1d@D+1 |" not in text and "| 1d@D+2 |" not in text


def test_main_review_keeps_cash_and_prices_without_backend_status_or_repeated_qualifiers(tmp_path):
    text = render(tmp_path, [forecast()])
    main = text.split("<details>", 1)[0]
    assert "$112,081.48" in main and "$129,665.06" in main and "$17,583.58" in main
    assert "| AAPL | 1.25 | $396.98 | $316.34 | Sep 08, 2026 16:59 PDT |" in main
    assert "Projected Trade Quantity" in main and "Entry decision" in main
    for technical in ("(reference only)", "Historical samples", "SHA-256", "source receipt", "PROMOTED", "OBSERVED_CONSISTENT"):
        assert technical not in main
    assert "(reference only)" not in text
    assert f"<{tmp_path.as_posix()}/forecasts.parquet>" in text


def test_renderer_preserves_all_symbols_and_inputs_and_supports_older_row_fields(tmp_path):
    symbols = ["AAPL", "AMZN", "COST", "GOOG", "MU", "NVDA", "SNDK"]
    rows = [forecast(id=f"{symbol}:{route}", symbol=symbol, route=route) for symbol in symbols for route in ("1h@04:00", "4h@04:00")]
    data = pd.DataFrame(rows).drop(columns=["projected_trade_quantity", "scheduled_trade_quantity", "planning_direction"])
    before, details = data.copy(deep=True), report()
    original = deepcopy(details)
    text = render_trade_review(data, details, {}, source_gameplan=str(tmp_path))
    assert "7 stocks · 14 forecasts" in text
    assert sum(text.count(f"### {symbol}\n") for symbol in symbols) == 7
    assert text.count("| Neutral | 0 |") == 14
    pd.testing.assert_frame_equal(data, before)
    assert details == original


def test_renderer_rejects_fractional_projected_execution_quantity(tmp_path):
    with pytest.raises(ValueError, match="whole shares"):
        render(tmp_path, [forecast(projected_trade_quantity=1.25)])


def test_missing_projection_evidence_is_unavailable_without_inventing_zero_capacity(tmp_path):
    text = render(tmp_path, [forecast(projected_trade_quantity=None, projected_quantity_reason="PRICE_RANGE_UNAVAILABLE",
                                     trade_price_low=None, trade_price_high=None)])
    assert "| Bullish | Unavailable | — | Wait |" in text


def test_model_assessment_displays_recorded_tolerance_without_claiming_baseline_outperformance(tmp_path):
    models = {"1d": {
        "promotion_gate": {"status": "PROMOTED", "baseline_tolerances": {"brier_score": .005, "log_loss": .01},
                           "checks": {"brier_within_baseline_tolerance": True, "log_loss_within_baseline_tolerance": True}},
        "assessment": {"brier_score": .253, "log_loss": .7, "direction_accuracy_at_0_5": .52},
        "training_base_rate_assessment": {"brier_score": .25, "log_loss": .693},
    }}
    text = render(tmp_path, [forecast()], model_reports=models)
    assert "| 1d | Passed |" in text and "0.253000 / 0.250000" in text
    assert "0.005000 for Brier score and 0.010000 for log loss" in text
    assert "passing this rule does not imply baseline outperformance" in text
    assert "must beat" not in text


def direction_projection():
    held = {"AAPL": 1, "COST": 1, "MU": 2}
    return {
        "summary": {"starting_cash": 1000, "ending_cash_low": 1101,
                    "ending_cash_base": 1106, "ending_cash_high": 1111},
        "ending_positions": {"AAPL": 1, "COST": 1, "MU": 0},
        "hourly": [{"timestamp": f"2026-09-09T{hour + 7:02d}:00:00Z" if hour < 17 else "2026-09-10T00:00:00Z",
                    "cash_low": 997 if hour == 4 else 1101, "cash_base": 1000 if hour == 4 else 1106,
                    "cash_high": 1003 if hour == 4 else 1111,
                    "held_shares": held if hour == 4 else {**held, "MU": 0}} for hour in range(4, 18)],
        "events": [
            {"timestamp": "2026-09-09T11:00:00Z", "symbol": "AAPL", "action": "SELL", "quantity": 1,
             "price_low": 99, "price_base": 100, "price_high": 101,
             "cash_change_low": 99, "cash_change_base": 100, "cash_change_high": 101,
             "cash_low": 1099, "cash_base": 1100, "cash_high": 1101, "shares_after": 1, "reason": "BEARISH_SELL"},
            {"timestamp": "2026-09-09T11:00:00Z", "symbol": "MU", "action": "BUY", "quantity": 2,
             "price_low": 49, "price_base": 50, "price_high": 51,
             "cash_change_low": -102, "cash_change_base": -100, "cash_change_high": -98,
             "cash_low": 997, "cash_base": 1000, "cash_high": 1003, "shares_after": 2, "reason": "BULLISH_BUY"},
            {"timestamp": "2026-09-09T12:00:00Z", "symbol": "MU", "action": "SELL", "quantity": 2,
             "price_low": 52, "price_base": 53, "price_high": 54,
             "cash_change_low": 104, "cash_change_base": 106, "cash_change_high": 108,
             "cash_low": 1101, "cash_base": 1106, "cash_high": 1111, "shares_after": 0, "reason": "HORIZON_EXPIRY"},
        ],
        "assumptions": ["COST's weekly share remains allocated beyond this session."],
    }


def direction_rows():
    shared = {"projected_cash_after_low": 997, "projected_cash_after_base": 1000, "projected_cash_after_high": 1003}
    return [
        forecast(**shared, planning_direction="BEARISH", direction_based_trade_quantity=-1,
                 direction_based_action="SELL", direction_based_reason="BEARISH_SELL", projected_shares_after=1,
                 projected_available_shares_after=.5),
        forecast(**shared, id="COST:1h@04:00", symbol="COST", planning_direction="NEUTRAL", direction_based_trade_quantity=0,
                 direction_based_action="HOLD", direction_based_reason="NEUTRAL", projected_shares_after=1,
                 projected_available_shares_after=1),
        forecast(**shared, id="MU:1h@04:00", symbol="MU", direction_based_trade_quantity=2,
                 direction_based_action="BUY", direction_based_reason="BULLISH_BUY", projected_shares_after=2,
                 projected_available_shares_after=2),
        forecast(id="AAPL:1d@D+2", route="1d@D+2", execution_eligible=False,
                 target_window_start="2026-09-10T11:00:00Z", target_window_end="2026-09-11T00:00:00Z",
                 projected_trade_quantity=None, direction_based_trade_quantity=None,
                 direction_based_reason="NON_ENTRY_CONTEXT"),
    ]


def test_direction_plan_displays_signed_actions_and_the_same_post_hour_cash_without_scheduling_orders(tmp_path):
    details = report()
    details["direction_based_projection"] = direction_projection()
    details["planning_price_path"] = {"working_half_width_bps": 25}
    data = pd.DataFrame(direction_rows())
    before, original = data.copy(deep=True), deepcopy(details)
    text = render_trade_review(data, details, {}, source_gameplan=str(tmp_path))
    assert "Projected Trade Quantity | Direction Based Trade Qty |" in text
    assert "Cash available after (range) | Shares remaining | Plan action |" in text
    assert "Scheduled entry" not in text.split("<details>", 1)[0]
    forecast_lines = [line for line in text.splitlines() if line.startswith("| Hourly ·")]
    assert len(forecast_lines) == 3
    assert all("$997.00–$1,003.00" in line for line in forecast_lines)
    assert "| Bearish | 6.0 | SELL 1 |" not in text
    assert "| Bearish | 6 | SELL 1 |" in text and "| Bullish | 6 | BUY 2 |" in text
    assert "| Neutral | 6 | 0 |" in text
    assert "| 1 (0.5 after open orders) | Sell eligible held shares |" in text
    assert "| 1 | Hold |" in text
    context = next(line for line in text.splitlines() if line.startswith("| Daily · Day 3"))
    assert "| Bullish | — | — |" in context and context.endswith("| — | — | Outlook |")
    assert "0 scheduled entries" in text and "0 orders submitted" in text
    assert "projected purchases and sales are not submitted orders or actual fills" in text
    assert "±25 basis points" in text
    assert "Price and cash ranges are estimates only, never execution limits." in text
    assert "Trades may proceed outside either range" in text
    assert "Estimated prices and balances do not veto an order." in text
    assert "checked again or skipped" not in text
    pd.testing.assert_frame_equal(data, before)
    assert details == original


def test_hourly_and_end_of_day_balances_retain_weekly_holdings_and_list_each_event_once(tmp_path):
    details = report()
    details["snapshot"]["available_cash"] = 1000
    details["snapshot"]["held_shares"] = {"AAPL": 2, "COST": 1, "MU": 0}
    details["direction_based_projection"] = direction_projection()
    text = render(tmp_path, direction_rows(), details=details)
    assert "| End-of-day cash | $1,101.00 | $1,106.00 | $1,111.00 |" in text
    assert "| COST | 1 | 1 |" in text
    hourly = text.split("## Cash and holdings by hour", 1)[1].split("## Forecasts", 1)[0]
    assert len([line for line in hourly.splitlines() if line.startswith("| Sep")]) == 14
    assert "| Sep 09, 2026 04:00 PDT | $997.00 | $1,000.00 | $1,003.00 | 1 | 1 | 2 |" in hourly
    assert "| Sep 09, 2026 17:00 PDT | $1,101.00 | $1,106.00 | $1,111.00 | 1 | 1 | 0 |" in hourly
    events = text.split("<summary>Chronological buys, sales and expiries</summary>", 1)[1].split("</details>", 1)[0]
    assert len([line for line in events.splitlines() if line.startswith("| Sep")]) == 3
    assert events.index("| AAPL | — | SELL 1 |") < events.index("| MU | — | BUY 2 |") < events.index("| MU | — | SELL 2 |")
    assert "| +$104.00 | +$106.00 | +$108.00 | $1,101.00–$1,111.00 | 0 | Horizon expiry |" in events
    assert "| COST |" not in events
    assert "overnight or weekly expiries remain held at 17:00" in text


def test_direction_quantity_does_not_invent_zero_when_evidence_is_missing(tmp_path):
    text = render(tmp_path, [forecast(direction_based_trade_quantity=None)])
    assert "| Bullish | 6 | Unavailable |" in text
    with pytest.raises(ValueError, match="signed whole shares"):
        render(tmp_path, [forecast(direction_based_trade_quantity=-1.25)])
