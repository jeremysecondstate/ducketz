from dataclasses import replace

import pandas as pd
import pytest

from ml.gameplan_cash_ledger import UnavailablePlanningPricePath, project_direction_trades
from ml.stock_trader.contracts import StockTraderPolicy


POLICY = replace(StockTraderPolicy(), minimum_order_notional=0)


def forecast(symbol="AAPL", hour=4, probability=.54, horizon="1h", *, end=None, promoted=True, execution=True):
    start = pd.Timestamp(f"2026-09-09 {hour:02d}:00", tz="America/Los_Angeles").tz_convert("UTC")
    return {"id": f"{symbol}:{horizon}:{hour}", "action_date": "2026-09-09", "symbol": symbol,
            "model_group": horizon, "execution_eligible": execution, "target_window_start": start,
            "target_window_end": pd.Timestamp(end) if end else start + pd.Timedelta(hours=1),
            "calibrated_probability": probability, "model_status": "PROMOTED" if promoted else "RESEARCH_NOT_PROMOTED",
            "projected_trade_quantity": 1}


def snapshot(symbols=("AAPL",), *, cash=100, equity=1000, held=0):
    return {"status": "OBSERVED", "cash_status": "CASH_ONLY_BOUNDED", "account_equity": equity,
            "available_cash": cash, "reserved_cash": 0, "held_shares": {s: held for s in symbols},
            "symbol_exposure": {s: held * 10 for s in symbols}, "stock_market_value_by_symbol": {s: held * 10 for s in symbols},
            "other_symbol_exposure": {s: 0 for s in symbols}, "gross_exposure": held * 10 * len(symbols),
            "pending_buy_shares": {}, "pending_sell_shares": {}, "quotes": {s: {"ask": 11} for s in symbols},
            "ownership": {"safe_for_planning": True, "active_allocations": [], "blocked_symbols": []}}


def path(symbols=("AAPL",), low=10, mid=10.5, high=11):
    return {"working_half_width_bps": 20, "points": {
        f"{s}|2026-09-09|{hour:02d}:00": {
            "status": "AVAILABLE", "symbol": s, "action_date": "2026-09-09", "clock_local": f"{hour:02d}:00",
            "timestamp": f"2026-09-09T{hour:02d}:00:00-07:00",
            "planned_price_low": low, "planned_price_mid": mid, "planned_price_high": high}
        for s in symbols for hour in range(4, 18)}}


def run(rows, state=None, prices=None, policy=POLICY):
    return project_direction_trades(pd.DataFrame(rows), state or snapshot(), prices or path(), policy=policy)


def unavailable_path():
    prices = path()
    prices['points']['AAPL|2026-09-09|04:00'].update(
        status='UNAVAILABLE_REFERENCE_PRICE', reference_gap_minutes=143,
        planned_price_low=None, planned_price_mid=None, planned_price_high=None)
    return prices


def test_missing_reference_has_typed_diagnostics_and_cannot_simulate_cash():
    with pytest.raises(UnavailablePlanningPricePath) as error:
        run([forecast()], prices=unavailable_path())
    assert error.value.points[0]['symbol'] == 'AAPL'
    assert error.value.points[0]['reference_gap_minutes'] == 143


@pytest.mark.parametrize('invalid', ['ownership', 'exposure', 'allocation', 'clock', 'numeric-missing'])
def test_unavailable_projection_does_not_hide_invalid_inputs(invalid):
    state, prices, row = snapshot(), unavailable_path(), forecast()
    if invalid == 'ownership':
        state['ownership']['safe_for_planning'] = False
    elif invalid == 'exposure':
        state['symbol_exposure']['AAPL'] = 1
    elif invalid == 'allocation':
        state['ownership']['active_allocations'] = [dict(symbol='AAPL', horizon='1h', owned_shares=1,
                                                       target_end='2026-09-09T12:00:00Z')]
    elif invalid == 'clock':
        row['target_window_end'] = row['target_window_start']
    else:
        prices['points']['AAPL|2026-09-09|04:00']['planned_price_mid'] = 10
    with pytest.raises(ValueError) as error:
        run([row], state, prices)
    assert not isinstance(error.value, UnavailablePlanningPricePath)


def test_user_example_cash_range_and_next_hour_bearish_sell():
    rows, report = run([forecast(), forecast(hour=5, probability=.46)])
    assert rows.direction_based_trade_quantity.tolist() == [1, -1]
    assert rows.projected_cash_after_low.tolist() == [89, 99]
    assert rows.projected_cash_after_high.tolist() == [90, 101]
    assert rows.projected_shares_after.tolist() == [1, 0]
    assert [event["reason"] for event in report["events"]] == ["BULLISH_BUY", "BEARISH_SELL"]
    assert report["summary"]["ending_cash_base"] == 100


def test_neutral_quantity_zero_and_earlier_expiry_is_a_separate_event():
    rows, report = run([forecast(), forecast(hour=5, probability=.5287)])
    assert rows.direction_based_trade_quantity.tolist() == [1, 0]
    assert rows.iloc[1].direction_based_reason == "NEUTRAL"
    assert report["events"][1]["reason"] == "HORIZON_EXIT"
    assert rows.iloc[1].projected_shares_after == 0


def test_hold_only_leaves_cash_and_shares_unchanged():
    rows, report = run([forecast(probability=.5)], snapshot(held=1))
    assert rows.iloc[0].direction_based_trade_quantity == 0
    assert report["events"] == []
    assert report["summary"]["ending_cash_low"] == report["summary"]["ending_cash_high"] == 100
    assert report["ending_positions"] == {"AAPL": 1}


def test_all_horizons_cannot_sell_the_same_initial_share_twice():
    items = [forecast(probability=.4, horizon=h) for h in ("1h", "4h", "1d", "1w")]
    rows, report = run(items, snapshot(held=1))
    assert rows.direction_based_trade_quantity.tolist() == [-1, 0, 0, 0]
    assert len(report["events"]) == 1
    assert report["ending_positions"]["AAPL"] == 0


def test_all_seven_bearish_sales_are_accounted_without_live_order_batch_cap():
    symbols = ("AAPL", "AMZN", "COST", "GOOG", "MU", "NVDA", "SNDK")
    rows, report = run([forecast(s, probability=.4) for s in symbols], snapshot(symbols, held=1), path(symbols))
    assert rows.direction_based_trade_quantity.eq(-1).all()
    assert rows.projected_cash_after_low.eq(170).all()
    assert rows.projected_cash_after_high.eq(177).all()
    assert len(report["events"]) == 7
    assert report["events"][-1]["cash_low"] == 170


def test_simultaneous_buys_share_one_cash_balance_and_have_identical_post_hour_balance():
    symbols = ("AAPL", "AMZN")
    rows, report = run([forecast("AAPL", probability=.6), forecast("AMZN", probability=.7)],
                       snapshot(symbols, cash=20), path(symbols))
    assert rows.direction_based_trade_quantity.tolist() == [0, 1]
    assert rows.projected_cash_after_low.tolist() == [9, 9]
    assert report["events"][0]["symbol"] == "AMZN"


def test_cash_after_sale_can_fund_later_buy_only_within_conditional_scenario():
    symbols = ("AAPL", "AMZN")
    state = snapshot(symbols, cash=2)
    state["held_shares"]["AAPL"] = 2
    state["symbol_exposure"]["AAPL"] = state["stock_market_value_by_symbol"]["AAPL"] = 20
    state["gross_exposure"] = 20
    rows, report = run([forecast("AAPL", probability=.4), forecast("AMZN", hour=5)], state, path(symbols))
    assert rows.direction_based_trade_quantity.tolist() == [-2, 1]
    assert report["no_fill_baseline"]["cash"] == 2
    assert any("assumed earlier sale proceeds" in text for text in report["assumptions"])


def test_pending_sells_reserve_stock_and_pending_buys_never_become_held():
    state = snapshot(held=2.5)
    state["pending_sell_shares"] = {"AAPL": 1}
    state["pending_buy_shares"] = {"AAPL": 3}
    state["reserved_cash"] = 33
    rows, report = run([forecast(probability=.4)], state)
    assert rows.iloc[0].direction_based_trade_quantity == -1
    assert report["ending_positions"]["AAPL"] == 1.5
    assert rows.iloc[0].projected_available_shares_after == .5


def test_other_horizon_inventory_is_protected_and_remains_held_past_session():
    state = snapshot(held=1)
    state["ownership"]["active_allocations"] = [{"symbol": "AAPL", "horizon": "1w", "owned_shares": 1,
                                                 "target_end": "2026-09-16T00:00:00Z"}]
    rows, report = run([forecast(probability=.4)], state)
    assert rows.iloc[0].direction_based_trade_quantity == 0
    assert report["ending_positions"] == {"AAPL": 1}
    assert len(report["ending_allocations"]) == 1


def test_reserved_allocation_sale_is_not_subtracted_twice_or_sold_again():
    state = snapshot(held=2)
    state["pending_sell_shares"] = {"AAPL": 1}
    state["ownership"]["active_allocations"] = [{"symbol": "AAPL", "horizon": "1h", "owned_shares": 1,
                                                 "reserved_sell_shares": 1, "target_end": "2026-09-09T12:00:00Z"}]
    rows, report = run([forecast(probability=.4)], state)
    assert rows.iloc[0].direction_based_trade_quantity == -1
    assert report["ending_positions"]["AAPL"] == 1
    assert len(report["events"]) == 1


def test_pending_horizon_buy_does_not_get_duplicate_projection():
    state = snapshot()
    state["pending_buy_shares"] = {"AAPL": 1}
    state["reserved_cash"] = 11
    state["ownership"]["active_allocations"] = [{"symbol": "AAPL", "horizon": "1h", "owned_shares": 0,
                                                 "reserved_buy_shares": 1}]
    rows, report = run([forecast()], state)
    assert rows.iloc[0].direction_based_reason == "HORIZON_BUY_ALREADY_PENDING"
    assert report["events"] == []


@pytest.mark.parametrize("end", [None, "not-a-date", "2026-09-09T12:00:00", "NaT"])
def test_missing_or_ambiguous_existing_expiry_is_not_replaced_by_current_time(end):
    state = snapshot(held=1)
    state["ownership"]["active_allocations"] = [{"symbol": "AAPL", "horizon": "1h", "owned_shares": 1,
                                                 "target_end": end}]
    with pytest.raises(ValueError):
        run([forecast(probability=.5)], state)


def test_nonstock_exposure_is_retained_after_sale_and_can_limit_rebuy():
    state = snapshot(held=1)
    state["other_symbol_exposure"] = {"AAPL": 145}
    state["symbol_exposure"] = {"AAPL": 155}
    state["gross_exposure"] = 155
    rows, report = run([forecast(probability=.4), forecast(hour=5)], state)
    assert rows.direction_based_trade_quantity.tolist() == [-1, 0]
    assert report["ending_positions"]["AAPL"] == 0


def test_exit_at_close_and_next_week_position_carry_are_distinct():
    rows, report = run([forecast(horizon="1d", end="2026-09-10T00:00:00Z"),
                        forecast(hour=5, horizon="1w", end="2026-09-16T00:00:00Z")])
    assert report["events"][-1]["timestamp"] == "2026-09-10T00:00:00+00:00"
    assert report["events"][-1]["horizon"] == "1d"
    assert all(item["horizon"] == "1w" for item in report["ending_allocations"])
    assert report["ending_positions"]["AAPL"] == rows.iloc[1].direction_based_trade_quantity


def test_later_hour_recalculates_capacity_after_expiry():
    rows, report = run([forecast(), forecast(hour=5)])
    assert rows.direction_based_trade_quantity.tolist() == [1, 1]
    assert [e["action"] for e in report["events"]] == ["BUY", "SELL", "BUY", "SELL"]


def test_unpromoted_and_context_rows_do_not_trade():
    rows, report = run([forecast(promoted=False), forecast(hour=5, execution=False)])
    assert rows.iloc[0].direction_based_trade_quantity == 0
    assert pd.isna(rows.iloc[1].direction_based_trade_quantity)
    assert report["events"] == []


@pytest.mark.parametrize("failure", ["missing_clock", "wrong_symbol", "bad_range", "missing_exposure", "bad_allocation", "bad_cash"])
def test_incomplete_or_inconsistent_evidence_does_not_invent_balances(failure):
    state, prices = snapshot(), path()
    if failure == "missing_clock":
        del prices["points"]["AAPL|2026-09-09|17:00"]
    elif failure == "wrong_symbol":
        prices["points"]["AAPL|2026-09-09|04:00"]["symbol"] = "AMZN"
    elif failure == "bad_range":
        prices["points"]["AAPL|2026-09-09|04:00"]["planned_price_low"] = 12
    elif failure == "missing_exposure":
        state["stock_market_value_by_symbol"]["AAPL"] = 5
    elif failure == "bad_allocation":
        state["ownership"]["active_allocations"] = [{"symbol": "AAPL", "horizon": "1h", "owned_shares": 1,
                                                     "target_end": "2026-09-09T12:00:00Z"}]
    else:
        state["available_cash"] = -1
    with pytest.raises(ValueError):
        run([forecast()], state, prices)


def test_events_reconcile_every_cash_endpoint_and_symbol_inventory():
    symbols = ("AAPL", "AMZN")
    rows, report = run([forecast("AAPL"), forecast("AMZN", hour=5), forecast("AAPL", hour=6)],
                       snapshot(symbols), path(symbols))
    for field in ("low", "base", "high"):
        balance = report["summary"]["starting_cash"]
        for event in report["events"]:
            assert event[f"cash_before_{field}"] == pytest.approx(balance)
            balance += event[f"cash_change_{field}"]
            assert event[f"cash_{field}"] == pytest.approx(balance)
        assert report["summary"][f"ending_cash_{field}"] == pytest.approx(balance)
    for symbol in symbols:
        assert report["ending_positions"][symbol] == report["starting_positions"][symbol] + sum(
            event["quantity"] * (1 if event["action"] == "BUY" else -1) for event in report["events"] if event["symbol"] == symbol)
