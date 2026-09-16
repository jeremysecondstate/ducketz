"""Synthetic current-market decisions; no broker or trading-control calls."""
from dataclasses import replace
from decimal import Decimal

import pandas as pd
import pytest

from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION, stock_target_windows
from ml.stock_trader.contracts import ActivationIntent, PortfolioState, PredictionSignal, QuoteState, StockTraderPolicy, TradeDecision
from ml.stock_trader.gameplan_direction_engine import build_gameplan_direction_trade_decisions
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY, SIZING_POLICIES
from ml.universe import PRODUCTION_LOOPS_SYMBOLS


NOW = "2026-09-08T11:01:00Z"
ACTIVATION = ActivationIntent(True, "ACTIVE", "synthetic activation", "fixture", "activation-fixture")
HORIZONS = ("1h", "4h", "1d", "1w")


def inputs(symbols=("AAPL",), *, probability=.5471, ask=100., bid=100.):
    windows = {row["model_group"]: row for row in stock_target_windows(pd.Timestamp("2026-09-08").date())
               if row["execution_eligible"] and row["target_window_start"].tz_convert("America/Los_Angeles").hour == 4}
    signals = {}
    for symbol in symbols:
        for horizon in HORIZONS:
            window = windows[horizon]
            signals[symbol, horizon] = PredictionSignal(
                symbol, horizon, f"frozen-{symbol}-{horizon}", "2026-09-08T08:00:00Z",
                window["target_window_start"].isoformat(), window["target_window_end"].isoformat(),
                "2026-09-08T11:05:00Z", "2026-09-08T09:00:00Z", probability, .001,
                {horizon: probability}, "qualified-stock-forecast", "synthetic-v1", "f" * 64, "PRE",
                STOCK_TARGET_CONTRACT_VERSION, "xnas-itch-archive-v1")
    state = PortfolioState(NOW, 100000., 100000., 0., 0., {}, {}, {}, {}, 0,
        {symbol: QuoteState(symbol, bid, ask, ask, ask, 1000., NOW) for symbol in symbols},
        "portfolio-fixture", "broker-fixture")
    return signals, state


def build(signals, portfolio, **kwargs):
    return build_gameplan_direction_trade_decisions(signals, portfolio, kwargs.pop("activation", ACTIVATION),
        verified_promoted_signals=kwargs.pop("verified_promoted_signals", frozenset(signals)),
        active_allocations=kwargs.pop("active_allocations", frozenset()),
        ledger_ready=kwargs.pop("ledger_ready", True),
        bearish_sell_capacities=kwargs.pop("bearish_sell_capacities", {}),
        decided_at=kwargs.pop("decided_at", NOW), time_in_force=kwargs.pop("time_in_force", "AM"), **kwargs)


def orders(decisions, action=None):
    return [item for item in decisions if item.quantity and (action is None or item.action == action)]


def test_explicit_late_opening_keeps_cash_quote_and_end_guards():
    signals, portfolio = inputs()
    later = '2026-09-08T11:30:00Z'
    signals = {key:replace(signal, actionable_until='2026-09-08T12:00:00Z') for key,signal in signals.items()}
    portfolio = replace(portfolio, observed_at=later,
                        quotes={s:replace(q, observed_at=later) for s,q in portfolio.quotes.items()})
    assert orders(build(signals, portfolio, decided_at=later))  # Saved instructions remain eligible through their supplied deadline.
    late = build(signals, portfolio, decided_at=later, late_opening_date='2026-09-08')
    assert orders(late) and all(d.prediction['target_window_end'] == signals[d.symbol,d.prediction['primary_horizon']].target_window_end for d in late)
    assert not orders(build(signals, portfolio, decided_at=later, late_opening_date='2026-09-08', ledger_ready=False))
    stale = replace(portfolio, quotes={s:replace(q, observed_at=NOW) for s,q in portfolio.quotes.items()})
    assert not orders(build(signals, stale, decided_at=later, late_opening_date='2026-09-08'))
    with pytest.raises(ValueError, match='late-opening date'):
        build(signals, portfolio, decided_at='2026-09-08T12:00:00Z', late_opening_date='2026-09-08')


def notional(decisions):
    return sum((item.quantity * Decimal(str(item.limit_price)) for item in decisions), Decimal(0))


def owned_exit(*, quantity=5, allocation_id="existing-weekly", price=1.):
    return TradeDecision(
        "old-exit-id", NOW, "AAPL", "SELL", "SELL", quantity, quantity, "LIMIT", price,
        None, None, None, None, None, None, "HORIZON_TARGET_MATURED", "An existing allocation is due.",
        "OWNED_EXIT", "Old supplied price is deliberately obsolete.",
        {"prediction_id": f"exit-{allocation_id}", "primary_horizon": "1w", "position_purpose": "EXIT",
         "allocation_id": allocation_id, "target_window_start": "2026-09-08T11:00:00Z",
         "actionable_until": "2026-09-08T11:02:00Z"}, {}, {}, {}, None,
        "old-policy", "old-fingerprint", ACTIVATION.checksum_sha256)


def test_new_policy_is_opt_in_and_buys_full_horizon_capacity_without_confidence_scaling():
    signals, portfolio = inputs()
    decisions = build(signals, portfolio)
    assert GAMEPLAN_SIZING_POLICY in SIZING_POLICIES
    assert [decision.quantity for decision in decisions] == [15, 30, 45, 50]
    assert notional(orders(decisions)) == Decimal(14000)
    for decision in decisions:
        assert decision.policy_version == GAMEPLAN_SIZING_POLICY
        assert decision.prediction["calibrated_probability"] == .5471
        assert decision.enrichment["entry_budget_utilization"] == 1.
        assert decision.enrichment["planning_ranges_have_execution_authority"] is False
        assert decision.prediction["position_purpose"] == "ENTRY"
        assert decision.order_payload["orderLegCollection"][0]["instruction"] == "BUY"
        assert all(getattr(decision, field) is None for field in (
            "expected_net_return", "expected_net_dollars", "trade_probability", "allocation_fraction", "protective_price"))


@pytest.mark.parametrize("current_price", [50., 150.])
@pytest.mark.parametrize("probability,expected_action", [(.54, "BUY"), (.46, "SELL")])
def test_estimate_price_and_cash_ranges_cannot_veto_or_price_current_market_orders(current_price, probability, expected_action):
    signals, portfolio = inputs(probability=probability, ask=current_price, bid=current_price)
    signals = {("AAPL", "1h"): replace(signals["AAPL", "1h"], enrichment_feature_values={
        "planned_price_low": 95., "planned_price_high": 105., "projected_cash_after_low": 0.,
        "projected_cash_after_high": 1., "direction_based_trade_quantity": 999.})}
    portfolio = replace(portfolio, held_shares={"AAPL": 1.}, symbol_exposure={"AAPL": current_price}, gross_exposure=current_price)
    decision = build(signals, portfolio, bearish_sell_capacities={key: 1 for key in signals})[0]
    assert decision.action == expected_action and decision.quantity > 0
    assert decision.limit_price == current_price
    assert decision.quantity != 999


@pytest.mark.parametrize("probability,expected", [(.0, "SELL"), (.46, "SELL"), (.460001, "SELL"),
    (.499999, "SELL"), (.5, "NO_TRADE"), (.500001, "BUY"), (.539999, "BUY"), (.54, "BUY"), (1., "BUY")])
def test_50_percent_direction_split_ignores_learned_probability_setting(probability, expected):
    signals, portfolio = inputs(probability=probability)
    signals = {("AAPL", "1h"): signals["AAPL", "1h"]}
    portfolio = replace(portfolio, held_shares={"AAPL": 1.})
    decision = build(signals, portfolio, bearish_sell_capacities={key: 1 for key in signals},
        policy=StockTraderPolicy(minimum_trade_probability=.99))[0]
    assert decision.action == expected
    if expected == "NO_TRADE":
        assert decision.quantity == 0 and decision.order_payload is None
        assert decision.prediction["position_purpose"] == "HOLD"
        assert decision.suggested_action == "HOLD"


def test_bearish_inventory_requires_explicit_authority_and_never_shorts_or_multiplies_a_share():
    signals, portfolio = inputs(probability=.3)
    portfolio = replace(portfolio, held_shares={"AAPL": 2.5}, pending_sell_shares={"AAPL": 1.})
    assert not orders(build(signals, portfolio))
    decisions = build(signals, portfolio, bearish_sell_capacities={key: 50 for key in signals})
    assert sum(item.quantity for item in orders(decisions)) == 1
    selected = orders(decisions)[0]
    assert selected.prediction["primary_horizon"] == "1h"
    assert selected.prediction["position_purpose"] == "DIRECTION_EXIT"
    assert selected.order_payload["orderLegCollection"][0]["instruction"] == "SELL"
    assert not orders(build(signals, replace(portfolio, held_shares={}), bearish_sell_capacities={key: 1 for key in signals}))


def test_same_horizon_inventory_can_be_sold_or_increased_by_a_new_bullish_signal():
    signals, portfolio = inputs(probability=.3)
    key = ("AAPL", "1w")
    signals = {key: signals[key]}
    portfolio = replace(portfolio, held_shares={"AAPL": 2.})
    assert build(signals, portfolio, active_allocations=frozenset({key}), bearish_sell_capacities={key: 2})[0].quantity == 2
    signals[key] = replace(signals[key], calibrated_probability=.8)
    decision = build(signals, portfolio, active_allocations=frozenset({key}))[0]
    assert decision.decision_reason_code == "ELIGIBLE"
    assert decision.action == "BUY" and decision.quantity > 0


def test_all_symbols_bearish_opening_sales_fit_the_explicit_new_strategy_order_policy():
    signals, portfolio = inputs(PRODUCTION_LOOPS_SYMBOLS, probability=.3)
    signals = {key: signal for key, signal in signals.items() if key[1] == "1h"}
    portfolio = replace(portfolio, held_shares={symbol: 1. for symbol in PRODUCTION_LOOPS_SYMBOLS})
    decisions = build(signals, portfolio, bearish_sell_capacities={key: 1 for key in signals},
        policy=StockTraderPolicy(maximum_orders_per_wake=28))
    assert len(orders(decisions, "SELL")) == len(PRODUCTION_LOOPS_SYMBOLS)


def test_cash_is_shared_between_buys_and_estimated_cash_never_becomes_actual_cash():
    signals, portfolio = inputs(("AAPL", "COST"))
    portfolio = replace(portfolio, available_cash=1000.)
    decisions = build(signals, portfolio)
    assert notional(orders(decisions)) == Decimal(900)
    assert notional(orders(decisions)) <= Decimal(1000) * Decimal(".95")
    assert not orders(build(signals, replace(portfolio, available_cash=0.)))


def test_unfilled_direction_sales_never_fund_buys_or_free_exposure_in_same_batch():
    signals, portfolio = inputs(("AAPL", "COST"))
    signals = {key: replace(signal, calibrated_probability=.3) if key[0] == "AAPL" else signal
               for key, signal in signals.items()}
    portfolio = replace(portfolio, available_cash=0., held_shares={"AAPL": 10.}, gross_exposure=1000., symbol_exposure={"AAPL": 1000.})
    decisions = build(signals, portfolio, bearish_sell_capacities={("AAPL", "1h"): 10})
    assert len(orders(decisions, "SELL")) == 1 and not orders(decisions, "BUY")
    portfolio = replace(portfolio, available_cash=100000., gross_exposure=130000.)
    decisions = build(signals, portfolio, bearish_sell_capacities={("AAPL", "1h"): 10})
    assert len(orders(decisions, "SELL")) == 1 and not orders(decisions, "BUY")


def test_pending_buy_exposure_and_existing_symbol_exposure_are_reserved_jointly():
    signals, portfolio = inputs(("AAPL", "COST"))
    portfolio = replace(portfolio, gross_exposure=129000., pending_buy_shares={"COST": 8.})
    assert notional(orders(build(signals, portfolio), "BUY")) == Decimal(200)
    portfolio = replace(portfolio, gross_exposure=14900., symbol_exposure={"AAPL": 14900.}, pending_buy_shares={})
    assert notional([d for d in orders(build(signals, portfolio), "BUY") if d.symbol == "AAPL"]) == Decimal(100)


def test_current_quote_rounding_is_used_for_whole_share_affordability():
    signals, portfolio = inputs(ask=100.001, bid=100.)
    portfolio = replace(portfolio, available_cash=1000.)
    decisions = build(signals, portfolio)
    assert len(orders(decisions)) == 1
    assert orders(decisions)[0].quantity == 9 and orders(decisions)[0].limit_price == 100.01
    assert notional(orders(decisions)) == Decimal("900.09")


@pytest.mark.parametrize("kwargs,reason", [
    ({"ledger_ready": False}, "HORIZON_LEDGER_UNRECONCILED"),
    ({"activation": replace(ACTIVATION, active=False)}, "TRADER_INACTIVE"),
    ({"decided_at": "2026-09-08T10:59:00Z"}, "ENTRY_WINDOW_CLOSED"),
    ({"decided_at": "2026-09-08T11:05:00Z"}, "ENTRY_WINDOW_CLOSED"),
])
@pytest.mark.parametrize("probability", [.3, .7])
def test_buys_and_bearish_sells_preserve_activation_reconciliation_and_holding_window(kwargs, reason, probability):
    signals, portfolio = inputs(probability=probability)
    portfolio = replace(portfolio, held_shares={"AAPL": 5.})
    decisions = build(signals, portfolio, bearish_sell_capacities={key: 1 for key in signals}, **kwargs)
    assert not orders(decisions)
    assert {item.decision_reason_code for item in decisions} == {reason}


@pytest.mark.parametrize("bid,ask,observed,code", [
    (101., 100., NOW, "USABLE_QUOTE_UNAVAILABLE"),
    (100., float("nan"), NOW, "USABLE_QUOTE_UNAVAILABLE"),
    (100., 100., "2026-09-08T11:02:00Z", "USABLE_QUOTE_UNAVAILABLE"),
    (100., 100., "2026-09-08T10:59:59Z", "CURRENT_QUOTE_TOO_OLD"),
])
def test_actual_quote_checks_are_retained_for_both_sides(bid, ask, observed, code):
    for probability in (.3, .7):
        signals, portfolio = inputs(probability=probability, ask=ask, bid=bid)
        portfolio = replace(portfolio, held_shares={"AAPL": 5.}, quotes={"AAPL": replace(portfolio.quotes["AAPL"], observed_at=observed)})
        decisions = build(signals, portfolio, bearish_sell_capacities={key: 1 for key in signals})
        assert not orders(decisions)
        assert {item.decision_reason_code for item in decisions} == {code}


@pytest.mark.parametrize("probability", [.3, .7])
def test_wide_live_quote_uses_authorized_midpoint_for_both_sides(probability):
    signals, portfolio = inputs(probability=probability, bid=123.66, ask=124.75)
    portfolio = replace(portfolio, held_shares={"AAPL": 5.}, quotes={"AAPL": replace(
        portfolio.quotes["AAPL"], observed_at="2026-09-08T10:48:40Z",
        received_at=NOW, realtime=True, quote_type="NBBO")})
    selected = orders(build(signals, portfolio, bearish_sell_capacities={key: 1 for key in signals}))
    assert selected
    assert all(d.limit_price == (124.21 if probability > .5 else 124.20) for d in selected)
    assert all(d.order_style_reason_code == "GAMEPLAN_CURRENT_MIDPOINT_LIMIT" for d in selected)
    assert all(d.quote["observed_at"] == "2026-09-08T10:48:40Z" for d in selected)


@pytest.mark.parametrize("realtime,quote_type,received", [
    (False, "NBBO", NOW), (None, "NBBO", NOW), ("true", "NBBO", NOW),
    (True, "INDICATIVE", NOW), (True, "NBBO", "2026-09-08T10:59:59Z"),
    (True, "NBBO", "2026-09-08T11:02:00Z"),
])
def test_unverified_or_expired_response_cannot_relabel_an_old_quote(realtime, quote_type, received):
    signals, portfolio = inputs()
    portfolio = replace(portfolio, quotes={"AAPL": replace(portfolio.quotes["AAPL"],
        observed_at="2026-09-08T10:48:40Z", received_at=received,
        realtime=realtime, quote_type=quote_type)})
    assert not orders(build(signals, portfolio))


def test_due_exits_are_repriced_from_current_bid_and_share_inventory_with_bearish_sells():
    signals, portfolio = inputs(probability=.3, ask=100.002, bid=100.001)
    portfolio = replace(portfolio, held_shares={"AAPL": 6.})
    decisions = build(signals, portfolio, active_allocations=frozenset({("AAPL", "1w")}),
        exit_decisions=(owned_exit(), owned_exit()), bearish_sell_capacities={("AAPL", "1h"): 1})
    assert [item.quantity for item in decisions[:2]] == [5, 0]
    assert decisions[0].limit_price == 100. and decisions[0].prediction["position_purpose"] == "EXIT"
    assert decisions[1].decision_reason_code == "DUPLICATE_OWNED_EXIT"
    assert sum(item.quantity for item in orders(decisions, "SELL")) == 6
    assert len(orders(decisions, "SELL")) == 2


def test_quote_age_limit_is_explicit_and_applies_to_due_exits_too():
    signals, portfolio = inputs()
    portfolio = replace(portfolio, held_shares={"AAPL": 5.}, quotes={
        "AAPL": replace(portfolio.quotes["AAPL"], observed_at="2026-09-08T11:00:00Z")})
    assert orders(build(signals, portfolio))
    decisions = build(signals, portfolio, maximum_quote_age_seconds=59.,
        active_allocations=frozenset({("AAPL", "1w")}), exit_decisions=(owned_exit(),))
    assert not orders(decisions)
    assert decisions[0].decision_reason_code == "CURRENT_QUOTE_TOO_OLD"
    assert decisions[0].enrichment["maximum_quote_age_seconds"] == 59.


def test_negative_pending_sell_evidence_cannot_create_extra_available_shares():
    signals, portfolio = inputs(probability=.3)
    portfolio = replace(portfolio, held_shares={"AAPL": 1.}, pending_sell_shares={"AAPL": -1.})
    with pytest.raises(ValueError, match="finite portfolio budget amounts"):
        build(signals, portfolio, bearish_sell_capacities={("AAPL", "1h"): 2})


def test_unvalued_pending_buy_blocks_buys_but_does_not_prevent_authorized_sell():
    signals, portfolio = inputs(probability=.3)
    portfolio = replace(portfolio, held_shares={"AAPL": 1.}, pending_buy_shares={"COST": 1.})
    decisions = build(signals, portfolio, bearish_sell_capacities={("AAPL", "1h"): 1})
    assert len(orders(decisions, "SELL")) == 1
    signals = {key: replace(signal, calibrated_probability=.8) for key, signal in signals.items()}
    assert {item.decision_reason_code for item in build(signals, portfolio)} == {"JOINT_PORTFOLIO_BUDGET_INVALID"}


def test_rank_and_idempotency_are_stable_and_configuration_remains_explicit():
    signals, portfolio = inputs(("COST", "AAPL"))
    signals["COST", "1w"] = replace(signals["COST", "1w"], calibrated_probability=.9)
    policy = StockTraderPolicy(maximum_orders_per_wake=2)
    forward = build(signals, portfolio, policy=policy)
    backward = build(dict(reversed(list(signals.items()))), portfolio, policy=policy)
    assert [item.to_dict() for item in forward] == [item.to_dict() for item in backward]
    assert [(item.symbol, item.prediction["primary_horizon"]) for item in orders(forward)] == [("COST", "1w"), ("AAPL", "1h")]
    retry = build(signals, portfolio, policy=policy, decided_at="2026-09-08T11:01:30Z")
    assert [item.decision_id for item in forward] == [item.decision_id for item in retry]
    changed = build(signals, portfolio, policy=replace(policy, maximum_cash_utilization_fraction=.9))
    assert forward[0].decision_id != changed[0].decision_id


@pytest.mark.parametrize("capacities", [{("AAPL", "1h"): -1}, {("AAPL", "1h"): 1.5}, {("AAPL", "1h"): True}, {("COST", "1h"): 1}])
def test_invalid_sell_capacity_attestation_is_rejected(capacities):
    signals, portfolio = inputs()
    with pytest.raises(ValueError, match="Bearish capacities"):
        build(signals, portfolio, bearish_sell_capacities=capacities)


def test_expiry_exit_without_reconciled_allocation_is_rejected():
    _, portfolio = inputs()
    portfolio = replace(portfolio, held_shares={"AAPL": 100.})
    with pytest.raises(ValueError, match="existing reconciled horizon exits"):
        build({}, portfolio, exit_decisions=(owned_exit(),))
