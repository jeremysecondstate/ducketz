"""Pure combined candidate planning: native target shapes, no broker or model calls."""
from dataclasses import replace
from decimal import Decimal

import pandas as pd
import pytest

from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION, stock_target_windows
from ml.stock_trader.contracts import (
    ActivationIntent, PortfolioState, PredictionSignal, QuoteState,
    STOCK_TRADER_SYMBOLS, StockTraderPolicy, TradeDecision,
)
from ml.stock_trader.fixed_horizon_budget import FIXED_HORIZON_BUDGET_POLICY_VERSION
from ml.stock_trader.fixed_horizon_engine import build_fixed_horizon_trade_decisions


NOW = "2026-09-08T11:01:00Z"
ACTIVATION = ActivationIntent(True, "ACTIVE", "fixture active", "fixture", "activation-hash")
HORIZONS = ("1h", "4h", "1d", "1w")


def inputs(symbols=("AAPL",), *, probability=.75, ask=100., bid=100.):
    windows = {row["model_group"]: row for row in stock_target_windows(pd.Timestamp("2026-09-08").date())
               if row["execution_eligible"] and row["target_window_start"].tz_convert("America/Los_Angeles").hour == 4}
    signals = {}
    for symbol in symbols:
        for group in HORIZONS:
            window = windows[group]
            signals[symbol, group] = PredictionSignal(
                symbol, group, f"native-fixture-{symbol}-{group}", "2026-09-08T08:00:00Z",
                window["target_window_start"].isoformat(), window["target_window_end"].isoformat(),
                "2026-09-08T11:05:00Z", "2026-09-08T09:00:00Z", probability, .001,
                {group: probability}, "qualified-stock-forecast", "fixture-v1", "f" * 64, "PRE",
                STOCK_TARGET_CONTRACT_VERSION, "xnas-itch-archive-v1")
    state = PortfolioState(NOW, 100000., 100000., 0., 0., {}, {}, {}, {}, 0,
        {symbol: QuoteState(symbol, bid, ask, ask, ask, 1000., NOW) for symbol in symbols},
        "portfolio-fixture", "broker-read-context")
    return signals, state


def build(signals, state, **kwargs):
    return build_fixed_horizon_trade_decisions(signals, state, kwargs.pop("activation", ACTIVATION),
        verified_promoted_signals=kwargs.pop("verified_promoted_signals", frozenset(signals)),
        active_allocations=kwargs.pop("active_allocations", frozenset()),
        ledger_ready=kwargs.pop("ledger_ready", True), decided_at=kwargs.pop("decided_at", NOW),
        time_in_force=kwargs.pop("time_in_force", "AM"), **kwargs)


def owned_exit(symbol="AAPL", horizon="1w", *, quantity=5, allocation_id=None, limit_price=99.8):
    """Equivalent to a due, reconciled allocation supplied by the runtime."""
    allocation_id = allocation_id or f"owned-{symbol}-{horizon}"
    return TradeDecision(
        "existing-owned-exit", NOW, symbol, "SELL", "SELL", quantity, quantity,
        "LIMIT", limit_price, None, None, None, None, None, None,
        "HORIZON_TARGET_MATURED", "The reconciled allocation has reached its holding endpoint.",
        "OWNED_HORIZON_LIMIT_EXIT", "Existing bounded native exit.",
        {"prediction_id": f"exit-{allocation_id}", "primary_horizon": horizon,
         "position_purpose": "EXIT", "allocation_id": allocation_id,
         "target_window_start": "2026-09-08T11:00:00Z", "actionable_until": "2026-09-08T11:02:00Z",
         "holding_target_end": "2026-09-08T11:00:00Z", "parent_forecast_id": "earlier-owned-forecast"},
        {}, {}, {}, None, "original-risk-policy", "original-risk-fingerprint", ACTIVATION.checksum_sha256)


def orders(decisions, action=None):
    return [item for item in decisions if item.quantity and (action is None or item.action == action)]


def notional(decisions):
    return sum((item.quantity * Decimal(str(item.limit_price)) for item in decisions), Decimal(0))


def test_four_horizon_weights_share_the_existing_symbol_budget_without_fake_model_heads():
    signals, state = inputs()
    decisions = build(signals, state)
    assert [item.prediction["primary_horizon"] for item in orders(decisions)] == list(HORIZONS)
    assert [item.quantity for item in decisions] == [7, 15, 22, 25]
    assert notional(orders(decisions)) == Decimal(6900)
    assert notional(orders(decisions)) <= Decimal(15000)
    for item in decisions:
        assert item.prediction["calibrated_probability"] == .75
        assert item.prediction["model_name"] == "qualified-stock-forecast"
        assert item.enrichment["model_name"] is None
        assert item.enrichment["sizing_policy"] == FIXED_HORIZON_BUDGET_POLICY_VERSION
        assert all(getattr(item, field) is None for field in (
            "expected_net_return", "expected_net_dollars", "trade_probability", "allocation_fraction",
            "execution_urgency", "protective_price"))
        assert item.order_type == "LIMIT" and item.order_payload["orderType"] == "LIMIT"
        assert item.order_payload["orderLegCollection"][0]["instruction"] == "BUY"


def test_all_symbols_opening_candidates_and_due_exits_share_six_order_cap():
    signals, state = inputs(STOCK_TRADER_SYMBOLS)
    state = replace(state, held_shares={"AAPL": 5., "AMZN": 5.}, gross_exposure=1000.,
                    symbol_exposure={"AAPL": 500., "AMZN": 500.})
    exits = (owned_exit("AAPL"), owned_exit("AMZN"))
    decisions = build(signals, state, active_allocations=frozenset({("AAPL", "1w"), ("AMZN", "1w")}),
                      exit_decisions=exits)
    assert len(signals) == 4 * len(STOCK_TRADER_SYMBOLS)
    assert len(decisions) == len(signals) + len(exits)
    assert [item.action for item in decisions[:2]] == ["SELL", "SELL"]
    assert len(orders(decisions)) == 6
    assert [(item.symbol, item.prediction["primary_horizon"]) for item in orders(decisions, "BUY")] == [
        ("AAPL", "1h"), ("AAPL", "4h"), ("AAPL", "1d"), ("AMZN", "1h")]
    assert all(item.order_payload is None for item in decisions if not item.quantity)


def test_global_cash_is_joint_and_exit_proceeds_are_unavailable_until_confirmed_fill():
    signals, state = inputs(("AAPL", "COST"))
    state = replace(state, available_cash=0., held_shares={"AAPL": 5.}, gross_exposure=500., symbol_exposure={"AAPL": 500.})
    decisions = build(signals, state, exit_decisions=(owned_exit(),), active_allocations=frozenset({("AAPL", "1w")}))
    assert len(orders(decisions, "SELL")) == 1
    assert not orders(decisions, "BUY")
    state = replace(state, available_cash=1000.)
    decisions = build(signals, state, exit_decisions=(owned_exit(),), active_allocations=frozenset({("AAPL", "1w")}))
    assert notional(orders(decisions, "BUY")) == Decimal(900)
    assert notional(orders(decisions, "BUY")) <= Decimal(1000) * Decimal(".95")


def test_pending_buy_value_reduces_gross_headroom_for_every_symbol():
    signals, state = inputs(("AAPL", "COST"))
    state = replace(state, gross_exposure=129000., pending_buy_shares={"COST": 8.})
    decisions = build(signals, state)
    assert notional(orders(decisions, "BUY")) == Decimal(200)
    assert Decimal(129000) + Decimal(800) + notional(orders(decisions, "BUY")) <= Decimal(130000)


def test_joint_symbol_ceiling_includes_manual_and_pending_shares_without_adopting_them():
    signals, state = inputs(("AAPL", "COST"))
    state = replace(state, held_shares={"AAPL": 145.}, gross_exposure=14500.,
                    symbol_exposure={"AAPL": 14500.}, pending_buy_shares={"AAPL": 2.})
    decisions = build(signals, state)
    assert notional([item for item in orders(decisions, "BUY") if item.symbol == "AAPL"]) == Decimal(300)
    assert not orders(decisions, "SELL")


def test_limit_rounding_uses_actual_rounded_price_in_all_joint_budgets():
    signals, state = inputs(("AAPL", "COST"), ask=100.001)
    state = replace(state, available_cash=2105.263157894737, gross_exposure=128000.)
    decisions = build(signals, state)
    assert all(item.limit_price == 100.01 for item in orders(decisions))
    assert notional(orders(decisions)) == Decimal("1900.19")
    assert notional(orders(decisions)) <= Decimal(2000)


def test_probability_then_symbol_then_semantic_horizon_rank_is_repeatable():
    signals, state = inputs(("COST", "AAPL"))
    signals["COST", "1w"] = replace(signals["COST", "1w"], calibrated_probability=.8)
    policy = StockTraderPolicy(maximum_orders_per_wake=3)
    forward = build(signals, state, policy=policy)
    backward = build(dict(reversed(list(signals.items()))), state, policy=policy)
    assert [item.to_dict() for item in forward] == [item.to_dict() for item in backward]
    assert [(item.symbol, item.prediction["primary_horizon"]) for item in orders(forward)] == [
        ("COST", "1w"), ("AAPL", "1h"), ("AAPL", "4h")]


@pytest.mark.parametrize("maximum,expected", [(2, 2), (6, 6), (100, 6)])
def test_global_six_order_limit_never_expands_with_candidate_count(maximum, expected):
    signals, state = inputs(STOCK_TRADER_SYMBOLS)
    assert len(orders(build(signals, state, policy=StockTraderPolicy(maximum_orders_per_wake=maximum)))) == expected


def test_fractional_order_cap_cannot_bypass_zero_remaining_capacity():
    signals, state = inputs(STOCK_TRADER_SYMBOLS)
    with pytest.raises(ValueError, match="order cap must be an integer"):
        build(signals, state, policy=StockTraderPolicy(maximum_orders_per_wake=2.5))


def test_existing_weekly_reservation_blocks_only_weekly_and_never_finances_its_replacement():
    signals, state = inputs()
    decisions = build(signals, state, active_allocations=frozenset({("AAPL", "1w")}))
    assert len(orders(decisions)) == 3
    weekly = next(item for item in decisions if item.prediction["primary_horizon"] == "1w")
    assert weekly.decision_reason_code == "HORIZON_ALLOCATION_ALREADY_ACTIVE"
    assert weekly.order_payload is None


def test_bearish_forecasts_never_sell_existing_manual_or_other_horizon_shares():
    signals, state = inputs(probability=.3)
    state = replace(state, held_shares={"AAPL": 500.})
    decisions = build(signals, state, active_allocations=frozenset({("AAPL", "1w")}))
    assert not orders(decisions)
    assert all(item.action == "NO_TRADE" for item in decisions)


@pytest.mark.parametrize("kwargs,reason", [
    ({"verified_promoted_signals": frozenset()}, "FORECAST_NOT_PROMOTED"),
    ({"ledger_ready": False}, "HORIZON_LEDGER_UNRECONCILED"),
    ({"activation": replace(ACTIVATION, active=False)}, "TRADER_INACTIVE"),
    ({"decided_at": "2026-09-08T11:05:00Z"}, "ENTRY_WINDOW_CLOSED"),
    ({"decided_at": "2026-09-08T10:59:00Z"}, "ENTRY_WINDOW_CLOSED"),
])
def test_entry_authority_and_bounded_window_cannot_be_bypassed(kwargs, reason):
    signals, state = inputs()
    decisions = build(signals, state, **kwargs)
    assert not orders(decisions)
    assert {item.decision_reason_code for item in decisions} == {reason}


def test_qualified_probability_must_exceed_fifty_percent():
    signals, state = inputs(probability=.5)
    assert not orders(build(signals, state, policy=StockTraderPolicy(minimum_trade_probability=.1)))


@pytest.mark.parametrize("bad_exit", [
    replace(owned_exit(), prediction={**owned_exit().prediction, "position_purpose": "ENTRY"}),
    replace(owned_exit(), prediction={**owned_exit().prediction, "allocation_id": None}),
    replace(owned_exit(), action="BUY"),
    replace(owned_exit(), activation_checksum_sha256="other-activation"),
])
def test_exit_requires_existing_owned_allocation_and_current_activation(bad_exit):
    _, state = inputs()
    state = replace(state, held_shares={"AAPL": 500.})
    with pytest.raises(ValueError, match="existing reconciled horizon exits"):
        build({}, state, active_allocations=frozenset({("AAPL", "1w")}), exit_decisions=(bad_exit,))


def test_held_shares_without_active_allocation_never_authorize_an_exit():
    _, state = inputs()
    state = replace(state, held_shares={"AAPL": 500.})
    with pytest.raises(ValueError, match="existing reconciled horizon exits"):
        build({}, state, exit_decisions=(owned_exit(),))


def test_duplicate_exit_and_multiple_horizons_cannot_oversell_account_or_pending_sell_shares():
    _, state = inputs()
    state = replace(state, held_shares={"AAPL": 9.}, pending_sell_shares={"AAPL": 2.})
    exits = (owned_exit(), owned_exit(), owned_exit(horizon="1h"))
    decisions = build({}, state, active_allocations=frozenset({("AAPL", "1w"), ("AAPL", "1h")}), exit_decisions=exits)
    assert [item.quantity for item in decisions] == [5, 0, 2]
    assert decisions[1].decision_reason_code == "DUPLICATE_OWNED_EXIT"
    assert sum(item.quantity for item in decisions) == 7
    assert all(item.order_payload["orderLegCollection"][0]["instruction"] == "SELL" for item in orders(decisions))


@pytest.mark.parametrize("kwargs,reason", [
    ({"ledger_ready": False}, "HORIZON_LEDGER_UNRECONCILED"),
    ({"activation": replace(ACTIVATION, active=False)}, "TRADER_INACTIVE"),
    ({"decided_at": "2026-09-08T11:02:00Z"}, "OWNED_EXIT_WINDOW_CLOSED"),
])
def test_owned_exits_retain_reconciliation_activation_and_time_checks(kwargs, reason):
    _, state = inputs()
    state = replace(state, held_shares={"AAPL": 5.})
    decisions = build({}, state, active_allocations=frozenset({("AAPL", "1w")}), exit_decisions=(owned_exit(),), **kwargs)
    assert not orders(decisions) and decisions[0].decision_reason_code == reason


def test_unvalued_working_buy_blocks_entries_but_preserves_independent_owned_exit():
    signals, state = inputs()
    state = replace(state, held_shares={"AAPL": 5.}, pending_buy_shares={"COST": 2.})
    decisions = build(signals, state, active_allocations=frozenset({("AAPL", "1w")}), exit_decisions=(owned_exit(),))
    assert len(orders(decisions, "SELL")) == 1 and not orders(decisions, "BUY")
    assert {item.decision_reason_code for item in decisions[1:]} == {
        "JOINT_PORTFOLIO_BUDGET_INVALID", "HORIZON_ALLOCATION_ALREADY_ACTIVE"}


def test_regular_session_owned_exit_preserves_native_spread_distinction():
    _, state = inputs(ask=102., bid=100.)
    state = replace(state, held_shares={"AAPL": 5.})
    kwargs = {"active_allocations": frozenset({("AAPL", "1w")}), "exit_decisions": (owned_exit(),)}
    assert len(orders(build({}, state, time_in_force="DAY", **kwargs))) == 1
    assert not orders(build({}, state, time_in_force="AM", **kwargs))


def test_exit_single_order_limit_and_price_offset_are_preserved():
    _, state = inputs()
    state = replace(state, held_shares={"AAPL": 1000.})
    decisions = build({}, state, active_allocations=frozenset({("AAPL", "1w")}), exit_decisions=(owned_exit(quantity=1000),))
    assert decisions[0].quantity == 50
    too_low = build({}, state, active_allocations=frozenset({("AAPL", "1w")}), exit_decisions=(owned_exit(limit_price=50.),))
    assert not orders(too_low)


def test_strategy_dedup_identity_is_separate_from_risk_policy_and_stable_within_slot():
    signals, state = inputs()
    policy = StockTraderPolicy()
    decision = build(signals, state, policy=policy)[0]
    retry = build(signals, state, policy=policy, decided_at="2026-09-08T11:01:30Z")[0]
    changed = build(signals, state, policy=replace(policy, maximum_cash_utilization_fraction=.9))[0]
    assert decision.policy_version == FIXED_HORIZON_BUDGET_POLICY_VERSION
    assert decision.policy_fingerprint != policy.fingerprint
    assert decision.enrichment["risk_policy_fingerprint"] == policy.fingerprint
    assert decision.decision_id == retry.decision_id
    assert decision.decision_id != changed.decision_id
