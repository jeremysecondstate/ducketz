"""Optional deterministic entry planning with one shared stock risk budget.

Only the native, checksum-verified promoted-forecast reader may supply the
verified key set. Exits must be precomputed from the reconciled horizon ledger;
this engine never adopts account/manual shares or treats a sell as filled cash.
No broker, model promotion, account configuration, or runtime switch is here.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Mapping

from app.services.schwab_stock_orders import build_schwab_stock_order_payload
from ml.stock_trader.contracts import (
    ActivationIntent, PortfolioState, PredictionSignal, StockTraderPolicy,
    TradeDecision, canonical_sha256, decision_identifier, utc,
)
from ml.stock_trader.engine import _portfolio_summary
from ml.stock_trader.fixed_horizon_budget import (
    FIXED_HORIZON_BUDGET_POLICY_VERSION, FIXED_HORIZON_WEIGHTS,
    FixedHorizonBudgetSizing, size_fixed_horizon_budget,
)


MAXIMUM_COMBINED_ORDERS = 6
_HORIZON_ORDER = {name: index for index, name in enumerate(FIXED_HORIZON_WEIGHTS)}
_RANKING_POLICY = "owned-exits-first-then-forecast-probability-symbol-horizon-v1"


def _money(value, *, allow_negative=False) -> Decimal:
    try:
        result = Decimal(str(value))
    except ArithmeticError as exc:
        raise ValueError("Fixed horizon engine requires finite portfolio budget amounts") from exc
    if not result.is_finite() or (not allow_negative and result < 0):
        raise ValueError("Fixed horizon engine requires finite portfolio budget amounts")
    return result


def _shares(budget: Decimal, price: Decimal) -> int:
    return max(0, int((max(Decimal(0), budget) / price).to_integral_value(rounding=ROUND_FLOOR)))


def _strategy_fingerprint(policy: StockTraderPolicy) -> str:
    return canonical_sha256({
        "sizing_policy": FIXED_HORIZON_BUDGET_POLICY_VERSION,
        "risk_policy_fingerprint": policy.fingerprint,
        "horizon_weights": FIXED_HORIZON_WEIGHTS,
        "maximum_combined_orders": min(MAXIMUM_COMBINED_ORDERS, policy.maximum_orders_per_wake),
        "ranking_policy": _RANKING_POLICY,
        "entry_order_style": "ask-rounded-up-within-existing-offset-cap",
    })


def _policy_metadata(policy: StockTraderPolicy) -> dict:
    return {"sizing_policy": FIXED_HORIZON_BUDGET_POLICY_VERSION,
            "sizing_basis": "deterministic_budget_from_qualified_stock_forecast",
            "model_name": None, "model_version": None, "model_fingerprint": None,
            "risk_policy_version": policy.policy_version, "risk_policy_fingerprint": policy.fingerprint,
            "ranking_policy": _RANKING_POLICY, "pending_buy_gross_valuation": "current_rounded_ask"}


def _entry_decision(signal, sizing, portfolio, activation, policy, timestamp, *, quantity, code, reason, time_in_force):
    price = sizing.limit_price if quantity else None
    prediction = {**asdict(signal), "position_purpose": "ENTRY",
                  "allocation_policy": "independent-stock-allocation-1-2-3-4-v1",
                  "sizing_policy": FIXED_HORIZON_BUDGET_POLICY_VERSION,
                  "horizon_weight": sizing.horizon_weight, "horizon_budget_fraction": sizing.horizon_budget_fraction}
    fingerprint = _strategy_fingerprint(policy)
    identifier = decision_identifier({"decided_at": timestamp.isoformat(), "symbol": signal.symbol,
        "prediction": prediction, "policy_fingerprint": fingerprint,
        "activation_checksum_sha256": activation.checksum_sha256, "decision_lane": "LIVE"})
    payload = (build_schwab_stock_order_payload(symbol=signal.symbol, instruction="BUY", order_type="LIMIT",
        time_in_force=time_in_force, position_effect="OPENING", quantity=quantity, price=price) if quantity else None)
    quote = portfolio.quotes.get(signal.symbol)
    return TradeDecision(
        decision_id=identifier, decided_at=timestamp.isoformat(), symbol=signal.symbol,
        action="BUY" if quantity else "NO_TRADE", suggested_action=signal.suggested_action,
        quantity=quantity, hypothetical_quantity=sizing.quantity,
        order_type="LIMIT" if quantity else None, limit_price=price, protective_price=None,
        expected_net_return=None, expected_net_dollars=None, trade_probability=None,
        allocation_fraction=None, execution_urgency=None,
        decision_reason_code=code, decision_reason=reason,
        order_style_reason_code="FIXED_HORIZON_ASK_LIMIT" if quantity else f"NO_ORDER_{code}",
        order_style_reason="A rounded ask limit is bounded by the existing offset and capital ceilings." if quantity else "No order was selected.",
        prediction=prediction,
        enrichment={**_policy_metadata(policy), "policy_output": sizing.to_dict(),
                    "combined_quantity": quantity, "combined_order_notional": quantity * price if quantity else 0.},
        portfolio=_portfolio_summary(portfolio, signal.symbol), quote=asdict(quote) if quote is not None else {},
        order_payload=payload, policy_version=FIXED_HORIZON_BUDGET_POLICY_VERSION,
        policy_fingerprint=fingerprint, activation_checksum_sha256=activation.checksum_sha256, decision_lane="LIVE")


def _exit_result(decision, policy, *, quantity, code=None, reason=None, time_in_force):
    fingerprint = _strategy_fingerprint(policy)
    prediction = {**decision.prediction, "sizing_policy": FIXED_HORIZON_BUDGET_POLICY_VERSION}
    payload = (build_schwab_stock_order_payload(symbol=decision.symbol, instruction="SELL", order_type="LIMIT",
        time_in_force=time_in_force, position_effect="CLOSING", quantity=quantity, price=decision.limit_price) if quantity else None)
    identifier = decision_identifier({"decided_at": decision.decided_at, "symbol": decision.symbol,
        "prediction": prediction, "policy_fingerprint": fingerprint,
        "activation_checksum_sha256": decision.activation_checksum_sha256, "decision_lane": "LIVE"})
    return replace(decision, decision_id=identifier, quantity=quantity, action="SELL" if quantity else "NO_TRADE",
        order_type="LIMIT" if quantity else None, limit_price=decision.limit_price if quantity else None,
        protective_price=None, order_payload=payload, expected_net_return=None, expected_net_dollars=None,
        trade_probability=None, allocation_fraction=None, execution_urgency=None,
        decision_reason_code=code or decision.decision_reason_code, decision_reason=reason or decision.decision_reason,
        order_style_reason_code=decision.order_style_reason_code if quantity else f"NO_ORDER_{code or decision.decision_reason_code}",
        prediction=prediction, enrichment={**_policy_metadata(policy), "position_purpose": "EXIT",
            "sizing_basis": "existing_reconciled_owned_horizon_exit"},
        policy_version=FIXED_HORIZON_BUDGET_POLICY_VERSION, policy_fingerprint=fingerprint)


def _joint_budgets(portfolio, policy):
    equity = _money(portfolio.account_equity)
    cash = max(Decimal(0), _money(portfolio.available_cash, allow_negative=True)) * _money(policy.maximum_cash_utilization_fraction)
    gross = equity * _money(policy.maximum_gross_equity_fraction) - _money(portfolio.gross_exposure)
    tick = Decimal(1).scaleb(-policy.price_decimals)
    pending_values = {}
    for symbol, shares in portfolio.pending_buy_shares.items():
        shares = _money(shares)
        if shares:
            quote = portfolio.quotes.get(symbol)
            if (quote is None or quote.symbol != symbol
                    or not 0 < _money(quote.bid) <= _money(quote.ask)
                    or utc(quote.observed_at) > utc(portfolio.observed_at)):
                raise ValueError("Pending buy exposure has no usable current quote")
            pending_values[symbol] = shares * _money(quote.ask).quantize(tick, rounding=ROUND_CEILING)
    gross -= sum(pending_values.values(), Decimal(0))
    symbols = {symbol: max(Decimal(0), equity * _money(policy.maximum_symbol_equity_fraction)
        - _money(portfolio.symbol_exposure.get(symbol, 0)) - pending_values.get(symbol, Decimal(0)))
        for symbol in portfolio.quotes}
    return max(Decimal(0), min(cash, gross)), symbols


def build_fixed_horizon_trade_decisions(
    signals: Mapping[tuple[str, str], PredictionSignal], portfolio: PortfolioState, activation: ActivationIntent,
    *, verified_promoted_signals: frozenset[tuple[str, str]], active_allocations: frozenset[tuple[str, str]],
    ledger_ready: bool, decided_at: object, policy: StockTraderPolicy | None = None,
    time_in_force: str = "DAY", exit_decisions: tuple[TradeDecision, ...] = (),
) -> tuple[TradeDecision, ...]:
    """Plan one combined batch with genuine forecast evidence and no ML heads.

    The verified key set is an explicit attestation by the native source loader,
    not a promotion performed here. The exit input is already bounded by each
    ledger allocation's filled shares; account holdings alone never authorize an
    exit. Working exits retain their allocation and do not finance new entries.
    """
    active_policy = policy or StockTraderPolicy()
    active_policy.validate()
    if time_in_force not in {"DAY", "AM", "PM", "EXT", "GTC_EXT"}:
        raise ValueError("Unsupported fixed horizon stock time in force")
    if not isinstance(verified_promoted_signals, frozenset) or not verified_promoted_signals.issubset(signals):
        raise ValueError("Verified promoted keys must name supplied native signals")
    if not isinstance(active_allocations, frozenset) or not isinstance(ledger_ready, bool):
        raise ValueError("Fixed horizon planning requires explicit reconciled allocation state")
    if type(active_policy.maximum_orders_per_wake) is not int:
        raise ValueError("The combined stock order cap must be an integer")
    if type(active_policy.price_decimals) is not int or not 0 <= active_policy.price_decimals <= 8:
        raise ValueError("Invalid stock limit-price precision")
    timestamp = utc(decided_at)
    capacity = min(MAXIMUM_COMBINED_ORDERS, active_policy.maximum_orders_per_wake)
    results = []
    sell_remaining = {symbol: _shares(_money(portfolio.available_sell_shares(symbol)), Decimal(1))
                      for symbol in portfolio.held_shares}
    seen_exits = set()
    for decision in exit_decisions:
        key = (decision.symbol, decision.prediction.get("primary_horizon"))
        allocation = decision.prediction.get("allocation_id")
        quantity, code, reason = decision.quantity, None, None
        if type(quantity) is not int or quantity < 0:
            raise ValueError("Owned exits require nonnegative whole-share quantities")
        if quantity:
            if (decision.action != "SELL" or decision.decision_lane != "LIVE"
                    or decision.prediction.get("position_purpose") != "EXIT" or not allocation
                    or key not in active_allocations
                    or decision.activation_checksum_sha256 != activation.checksum_sha256):
                raise ValueError("Only existing reconciled horizon exits may precede fixed entries")
            if allocation in seen_exits:
                quantity, code = 0, "DUPLICATE_OWNED_EXIT"
            elif not activation.active:
                quantity, code = 0, "TRADER_INACTIVE"
            elif not ledger_ready:
                quantity, code = 0, "HORIZON_LEDGER_UNRECONCILED"
            elif not capacity:
                quantity, code = 0, "LOWER_RANKED_THAN_WAKE_ORDER_CAP"
            else:
                quote = portfolio.quotes.get(decision.symbol)
                price = _money(decision.limit_price or 0)
                # Keep the native exit policy: the spread ceiling applies in
                # extended sessions, while DAY exits may reduce a wider market.
                usable_quote = (quote is not None and quote.symbol == decision.symbol
                    and 0 < _money(quote.bid) <= _money(quote.ask)
                    and utc(quote.observed_at) <= timestamp)
                minimum_price = (_money(round(max(.01, quote.bid *
                    (1 - active_policy.maximum_limit_offset_bps / 10000)), active_policy.price_decimals))
                    if usable_quote else Decimal(0))
                tick = Decimal(1).scaleb(-active_policy.price_decimals)
                if (decision.order_type != "LIMIT" or price <= 0 or not usable_quote
                        or price < minimum_price or price != price.quantize(tick)
                        or (time_in_force != "DAY"
                            and quote.relative_spread > active_policy.maximum_extended_relative_spread)):
                    quantity, code = 0, "OWNED_EXIT_QUOTE_OR_LIMIT_UNAVAILABLE"
                elif not (utc(decision.prediction["target_window_start"]) <= timestamp
                          < utc(decision.prediction["actionable_until"])):
                    quantity, code = 0, "OWNED_EXIT_WINDOW_CLOSED"
                else:
                    single_cap = _shares(_money(portfolio.account_equity) * _money(active_policy.maximum_single_order_equity_fraction),
                                         max(price, _money(quote.ask)))
                    quantity = min(quantity, sell_remaining.get(decision.symbol, 0), single_cap)
                    if quantity:
                        sell_remaining[decision.symbol] -= quantity
                        capacity -= 1
                    else:
                        code = "JOINT_OWNED_SHARE_CAP_EXHAUSTED"
            seen_exits.add(allocation)
            if code:
                reason = "The existing owned exit remains due; a combined ownership, execution, or order limit blocked this attempt."
        results.append(_exit_result(decision, active_policy, quantity=quantity, code=code, reason=reason, time_in_force=time_in_force))

    # An unvalued working buy cannot fund another entry. It does not erase
    # independently reconciled shares already selected for a risk-reducing exit.
    budget_error = False
    try:
        available, symbol_remaining = _joint_budgets(portfolio, active_policy)
    except ValueError:
        available, symbol_remaining, budget_error = Decimal(0), {}, True
    candidates: dict[tuple[str, str], FixedHorizonBudgetSizing] = {}
    for key, signal in sorted(signals.items()):
        if key != (signal.symbol, signal.primary_horizon) or signal.primary_horizon not in _HORIZON_ORDER:
            raise ValueError("Fixed horizon signal key differs from its stock/horizon")
        candidates[key] = size_fixed_horizon_budget(signal, portfolio, portfolio.quotes.get(signal.symbol), activation,
            forecast_promoted=key in verified_promoted_signals, ledger_ready=ledger_ready,
            has_active_allocation=key in active_allocations, decided_at=timestamp, policy=active_policy)
    ranked = sorted(candidates, key=lambda key: (-(candidates[key].forecast_probability or 0), key[0], _HORIZON_ORDER[key[1]]))
    entries = {}
    for key in ranked:
        sizing = candidates[key]
        quantity, code, reason = sizing.quantity, sizing.reason_code, sizing.reason
        if quantity:
            price = _money(sizing.limit_price)
            if budget_error:
                quantity, code = 0, "JOINT_PORTFOLIO_BUDGET_INVALID"
            elif not capacity:
                quantity, code = 0, "LOWER_RANKED_THAN_WAKE_ORDER_CAP"
            else:
                quantity = min(quantity, _shares(available, price), _shares(symbol_remaining.get(key[0], Decimal(0)), price))
                notional = quantity * price
                if not quantity or notional < _money(active_policy.minimum_order_notional):
                    quantity, code = 0, "JOINT_PORTFOLIO_BUDGET_EXHAUSTED"
                else:
                    available -= notional
                    symbol_remaining[key[0]] -= notional
                    capacity -= 1
            if not quantity:
                reason = ("The portfolio snapshot cannot safely value all existing working-buy exposure."
                    if code == "JOINT_PORTFOLIO_BUDGET_INVALID" else
                    "The combined batch exhausted the existing cash, gross, symbol, minimum-notional, or order limit.")
        entries[key] = _entry_decision(signals[key], sizing, portfolio, activation, active_policy, timestamp,
            quantity=quantity, code=code, reason=reason, time_in_force=time_in_force)
    return (*results, *(entries[key] for key in ranked))


__all__ = ["build_fixed_horizon_trade_decisions"]
