"""Offline ownership selection helpers with synthetic local account state."""
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.stock_trader.independent_runtime import _direction_sell_capacities, _direction_fallback_plans
from ml.stock_trader.cross_horizon_fallback import policy_for_action_date
from ml.stock_trader.contracts import StockTraderPolicy


DAY = "2026-10-19"


def signal(horizon, probability=.48):
    return SimpleNamespace(symbol="AAPL", primary_horizon=horizon,
        calibrated_probability=probability, prediction_id="abcl-" + horizon,
        target_window_start=DAY+"T11:00:00Z", target_window_end=DAY+"T15:00:00Z")


def allocation(horizon, held, *, buys=0, sells=0):
    return SimpleNamespace(symbol="AAPL", horizon=horizon, status="ACTIVE", allocation_id="owner-" + horizon,
        filled_shares=held, reserved_buy_shares=buys, reserved_sell_shares=sells)


def portfolio(*, pending=0):
    return SimpleNamespace(observed_at=DAY+"T11:01:00Z", held_shares={"AAPL":503},
        pending_sell_shares={"AAPL":pending}, pending_buy_shares={})


@pytest.mark.parametrize("horizon", ["1h", "4h"])
def test_bearish_horizon_can_sell_503_from_all_owners_despite_pending_own_buy(horizon):
    own = allocation(horizon, 20, buys=2)
    donors = [allocation("1d", 100), allocation("1w", 200), allocation("15m", 176)]
    state = SimpleNamespace(allocations=[own, *donors])
    signals = {("AAPL", horizon): signal(horizon), ("AAPL", "1w"):signal("1w", .8)}
    capacities = _direction_sell_capacities(signals, state, portfolio(), ())
    assert capacities == {("AAPL", horizon):503}


@pytest.mark.parametrize("horizon", ["1h", "4h"])
def test_pending_sells_and_already_selected_exits_reduce_actual_capacity(horizon):
    state = SimpleNamespace(allocations=[allocation("1w", 503, sells=17)])
    exit_decision = SimpleNamespace(symbol="AAPL", action="SELL", quantity=100,
                                    prediction={"allocation_id":"other-owner"})
    assert _direction_sell_capacities({("AAPL",horizon):signal(horizon)}, state,
        portfolio(pending=17), (exit_decision,)) == {("AAPL",horizon):386}


def test_daily_and_weekly_keep_own_plus_unallocated_stock_only():
    state = SimpleNamespace(allocations=[allocation("1h", 476), allocation("1d", 10), allocation("1w", 10)])
    signals = {("AAPL",h):signal(h) for h in ("1d", "1w")}
    assert _direction_sell_capacities(signals, state, portfolio(), ()) == {("AAPL","1d"):17, ("AAPL","1w"):10}


def test_historical_hourly_rule_does_not_reinterpret_original_ownership():
    state = SimpleNamespace(allocations=[allocation("1w", 503)])
    signals = {("AAPL","1h"):signal("1h")}
    assert _direction_sell_capacities(signals, state, portfolio(), (), action_date="2026-10-18") == {("AAPL","1h"):0}


def test_hourly_and_four_hour_never_request_a_fallback_slot_or_donor():
    class NoFallback:
        def fallback_direction_plan(self, **kwargs):
            raise AssertionError("Autonomous signal must not request frozen fallback capacity")
    signals = {("AAPL",h):signal(h) for h in ("1h", "4h")}
    assert _direction_fallback_plans(signals, signals, SimpleNamespace(allocations=[]), portfolio(), (),
        ledger=NoFallback(), policy=policy_for_action_date(DAY), source_fingerprint="f"*64,
        snapshot_id="synthetic", timestamp=pd.Timestamp(DAY+"T11:01:00Z"), preview=True) == {}


def test_daily_fallback_keeps_same_clock_bullish_owner_exclusion():
    class CapturingFallback:
        def fallback_direction_plan(self, **kwargs):
            self.kwargs = kwargs
            return {"eligible":False}
    ledger = CapturingFallback()
    state = SimpleNamespace(allocations=[allocation("1w",503)])
    signals = {("AAPL","1d"):signal("1d")}
    all_signals = {**signals, ("AAPL","1w"):signal("1w", .8)}
    assert _direction_fallback_plans(signals, all_signals, state, portfolio(), (), ledger=ledger,
        policy=policy_for_action_date(DAY), source_fingerprint="f"*64, snapshot_id="synthetic",
        timestamp=pd.Timestamp(DAY+"T11:01:00Z"), preview=True) == {}
    assert ledger.kwargs["excluded_donor_horizons"] == ("1w",)


def test_order_caps_leave_actual_remaining_stock_for_the_next_competing_horizon():
    state = SimpleNamespace(allocations=[allocation("1h",20), allocation("1w",483)])
    observed = portfolio()
    observed.account_equity = 150000
    observed.quotes = {"AAPL":SimpleNamespace(ask=20.0)}
    signals = {("AAPL",h):signal(h) for h in ("1h","4h")}
    assert _direction_sell_capacities(signals, state, observed, (), policy=StockTraderPolicy()) == {
        ("AAPL","1h"):375, ("AAPL","4h"):128}


def test_historical_estimated_quantity_does_not_cap_actual_autonomous_capacity():
    state = SimpleNamespace(allocations=[allocation("1h",20), allocation("1d",483)])
    hourly = signal("1h")
    hourly.planned_quantity = 30
    signals = {("AAPL","1h"):hourly, ("AAPL","1d"):signal("1d")}
    assert _direction_sell_capacities(signals, state, portfolio(), ()) == {
        ("AAPL","1h"):503, ("AAPL","1d"):0}


def test_daily_fallback_counts_autonomous_owner_slices_as_inventory_and_not_daily_usage():
    class CapturingFallback:
        def fallback_direction_plan(self, **kwargs):
            self.kwargs = kwargs
            return {"eligible":False}
    ledger = CapturingFallback()
    state = SimpleNamespace(allocations=[allocation("1h",20), allocation("1w",483)])
    signals = {("AAPL",h):signal(h) for h in ("1h","1d")}
    sale = SimpleNamespace(symbol="AAPL", action="SELL", quantity=418, prediction={"primary_horizon":"1h"})
    assert _direction_fallback_plans(signals, signals, state, portfolio(), (sale,), ledger=ledger,
        policy=policy_for_action_date(DAY), source_fingerprint="f"*64, snapshot_id="synthetic",
        timestamp=pd.Timestamp(DAY+"T11:01:00Z"), preview=True) == {}
    assert ledger.kwargs["reserved_inventory_shares"] == {"1h":20,"1w":398}
    assert ledger.kwargs["reserved_donor_shares"] == {}
    assert ledger.kwargs["excluded_donor_horizons"] == ("1h",)

