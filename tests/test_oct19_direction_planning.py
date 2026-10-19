"""October 19 global direction and autonomous hourly ownership regressions."""
from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from ml.gameplan_cash_ledger import project_direction_trades
from ml.stock_direction_policy import stock_direction, stock_direction_for_policy
from ml.stock_trader.contracts import StockTraderPolicy
from ml.stock_trader.cross_horizon_fallback import policy_for_action_date, slot_quota


SYMBOLS = ("AAPL", "ABCL", "AMZN", "COST", "CROX", "DBX", "DOCU", "GLOB", "GOOG", "IONQ", "MRNA",
           "MU", "NVDA", "OUST", "PATH", "PDYN", "PYPL", "QBTS", "RR", "SDGR", "SNDK", "TWST")
DAY = "2026-10-19"


def project(symbol="ABCL", horizon="1h", probability=.4872, *, holdings=None, reserved=0, extra=()):
    holdings = holdings or {"1w": 527}
    total = sum(holdings.values())
    row = {"id": "trigger", "symbol": symbol, "action_date": DAY, "model_group": horizon,
           "execution_eligible": True, "target_window_start": f"{DAY}T04:00:00-07:00",
           "target_window_end": f"{DAY}T05:00:00-07:00", "calibrated_probability": probability,
           "direction": stock_direction(probability), "direction_policy_version": "stock-direction-50-01-v3",
           "model_status": "PROMOTED"}
    snapshot = {"status": "OBSERVED", "cash_status": "CASH_ONLY_BOUNDED", "observed_at": f"{DAY}T10:00:00Z",
        "account_equity": 100000, "available_cash": 0, "reserved_cash": 0,
        "held_shares": {symbol: total}, "symbol_exposure": {symbol: total * 10},
        "stock_market_value_by_symbol": {symbol: total * 10}, "other_symbol_exposure": {symbol: 0},
        "gross_exposure": total * 10, "pending_buy_shares": {}, "pending_sell_shares": {symbol: reserved},
        "quotes": {symbol: {"ask": 10}}, "ownership": {"safe_for_planning": True, "blocked_symbols": [],
            "active_allocations": [{"symbol": symbol, "horizon": h, "owned_shares": q,
                "reserved_sell_shares": reserved if i == 0 else 0, "target_end": "2026-10-26T00:00:00Z",
                "allocation_id_sha256": str(i + 1) * 64}
                for i, (h, q) in enumerate(holdings.items())]}}
    price_path = {"points": {f"{symbol}|{DAY}|{hour:02d}:00": {"status": "AVAILABLE", "symbol": symbol,
        "action_date": DAY, "clock_local": f"{hour:02d}:00", "timestamp": f"{DAY}T{hour:02d}:00:00-07:00",
        "planned_price_low": 10, "planned_price_mid": 10, "planned_price_high": 10} for hour in range(4, 18)}}
    other_rows = [{**row, **item} for item in extra]
    return project_direction_trades(pd.DataFrame([row, *other_rows]), snapshot, price_path,
        policy=replace(StockTraderPolicy(), minimum_order_notional=0), signal_driven=True,
        cross_horizon_fallback_policy=policy_for_action_date(DAY))


@pytest.mark.parametrize("symbol", SYMBOLS)
@pytest.mark.parametrize("horizon", ["1h", "4h"])
def test_bearish_abcl_case_sells_any_horizon_without_fallback_quota(symbol, horizon):
    rows, report = project(symbol, horizon)
    assert rows.iloc[0].direction_based_action == "SELL"
    assert rows.iloc[0].direction_based_trade_quantity == -527
    assert report["ending_positions"][symbol] == 0
    assert report["cross_horizon_fallback"]["symbol_used"][symbol] == 0
    assert report["events"][0]["reason"] == "BEARISH_SELL"


def test_four_hour_sells_shorter_and_longer_inventory_and_keeps_pending_shares():
    rows, report = project(horizon="4h", holdings={"1h": 5, "4h": 10, "1d": 20, "1w": 30}, reserved=2)
    assert rows.iloc[0].direction_based_trade_quantity == -63
    assert report["ending_positions"]["ABCL"] == 2
    assert sum(lot["reserved"] for lot in report["ending_allocations"]) == 2


def test_bullish_owner_cannot_veto_bearish_hourly_sale_and_ties_do_not_double_sell():
    rows, report = project(extra=({"id": "weekly-bull", "model_group": "1w", "calibrated_probability": .7,
                                  "direction": "BULLISH"},
                                 {"id": "four-hour-bear", "model_group": "4h"}))
    assert rows.iloc[0].direction_based_trade_quantity == -527
    assert rows.iloc[2].direction_based_trade_quantity == 0
    assert sum(event["quantity"] for event in report["events"] if event["action"] == "SELL") == 527


def test_daily_bounded_fallback_and_weekly_ownership_stay_unchanged():
    rows, report = project(horizon="1d")
    assert rows.iloc[0].direction_based_trade_quantity == -slot_quota(263, "1d", f"{DAY}T04:00:00-07:00", DAY)
    assert report["events"][0]["reason"] == "BEARISH_CROSS_HORIZON_FALLBACK"
    rows, report = project(horizon="1w", holdings={"1d": 527})
    assert rows.iloc[0].direction_based_trade_quantity == 0
    assert report["events"] == []


@pytest.mark.parametrize("probability,expected", [(.4999, "BEARISH"), (.49999, "NO_EDGE"), (.5, "NO_EDGE"),
                                                (.50001, "NO_EDGE"), (.5001, "BULLISH")])
def test_inclusive_new_boundaries_and_preserved_historical_labels(probability, expected):
    assert stock_direction(probability) == expected
    old = "BULLISH" if probability > .5 else "BEARISH" if probability < .5 else "NO_EDGE"
    assert stock_direction_for_policy(probability, "stock-direction-50-v2") == old
