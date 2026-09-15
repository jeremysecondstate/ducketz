"""Opt-in Gameplan directions sized and priced from the current broker snapshot.

Planning price and cash ranges are estimates and are deliberately not inputs.
This pure builder neither adopts shares nor submits orders. Its caller supplies
reconciled sell capacities, excluding allocations already supplied as due exits,
and must reserve the chosen quantities atomically before sending an order.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Mapping

from app.services.schwab_stock_orders import build_schwab_stock_order_payload
from ml.stock_direction_policy import STOCK_DIRECTION_POLICY_VERSION, stock_direction
from ml.stock_trader.contracts import (
    ActivationIntent, PortfolioState, PredictionSignal, StockTraderPolicy,
    TradeDecision, canonical_sha256, decision_identifier, finite, utc,
)
from ml.stock_trader.engine import _portfolio_summary
from ml.stock_trader.fixed_horizon_budget import FIXED_HORIZON_WEIGHTS
from ml.stock_trader.fixed_horizon_engine import _joint_budgets, _money, _shares
from ml.stock_trader.sizing_policy import GAMEPLAN_SIZING_POLICY


_RANKING_POLICY = "owned-due-exits-then-bearish-horizon-symbol-then-bullish-probability-horizon-symbol-v1"
_ZERO = Decimal(0)


def _fingerprint(policy: StockTraderPolicy, maximum_quote_age_seconds: float = 60.) -> str:
    return canonical_sha256({
        "sizing_policy": GAMEPLAN_SIZING_POLICY, "risk_policy_fingerprint": policy.fingerprint,
        "direction_policy": STOCK_DIRECTION_POLICY_VERSION, "horizon_weights": FIXED_HORIZON_WEIGHTS,
        "ranking_policy": _RANKING_POLICY, "entry_budget_utilization": 1,
        "order_pricing": "current-ask-buy-current-bid-sell-wide-spread-midpoint-v1",
        "quote_freshness_policy": "realtime-nbbo-response-with-provider-update-time-v1",
        "planning_ranges_have_execution_authority": False,
        "maximum_quote_age_seconds": maximum_quote_age_seconds,
    })


def _metadata(policy: StockTraderPolicy, maximum_quote_age_seconds: float = 60.) -> dict:
    return {
        "sizing_policy": GAMEPLAN_SIZING_POLICY, "direction_policy": STOCK_DIRECTION_POLICY_VERSION,
        "sizing_basis": "current_cash_and_full_horizon_capacity_or_reconciled_sell_inventory",
        "risk_policy_version": policy.policy_version, "risk_policy_fingerprint": policy.fingerprint,
        "model_name": None, "model_version": None, "model_fingerprint": None,
        "entry_budget_utilization": 1., "ranking_policy": _RANKING_POLICY,
        "planning_ranges_have_execution_authority": False,
        "pending_sales_fund_this_batch": False,
        "pricing_basis": "current_ask_buy_bid_sell_with_wide_spread_midpoint_v1",
        "quote_freshness_policy": "realtime-nbbo-response-with-provider-update-time-v1",
        "maximum_quote_age_seconds": maximum_quote_age_seconds,
    }


def _uses_midpoint(quote, policy, action, time_in_force):
    return (quote is not None and (action == "BUY" or time_in_force != "DAY")
            and quote.relative_spread > policy.maximum_extended_relative_spread)


def _current_price(symbol, portfolio, policy, timestamp, action, time_in_force, maximum_quote_age_seconds):
    """Use the supplied current quote, never a forecast-derived limit."""
    quote = portfolio.quotes.get(symbol)
    if quote is None or quote.symbol != symbol:
        return None, "USABLE_QUOTE_UNAVAILABLE"
    bid, ask = finite(quote.bid), finite(quote.ask)
    if quote.realtime is False:
        return None, "REALTIME_QUOTE_UNAVAILABLE"
    try:
        if bid is None or ask is None or not 0 < bid <= ask or utc(quote.observed_at) > timestamp:
            return None, "USABLE_QUOTE_UNAVAILABLE"
        age = (timestamp - utc(quote.freshness_observed_at)).total_seconds()
        if age < 0:
            return None, "USABLE_QUOTE_UNAVAILABLE"
        if age > maximum_quote_age_seconds:
            return None, "CURRENT_QUOTE_TOO_OLD"
    except (TypeError, ValueError):
        return None, "USABLE_QUOTE_UNAVAILABLE"
    raw = ((_money(bid) + _money(ask)) / 2 if _uses_midpoint(quote, policy, action, time_in_force)
           else _money(ask if action == "BUY" else bid))
    tick = Decimal(1).scaleb(-policy.price_decimals)
    price = raw.quantize(tick, rounding=ROUND_CEILING if action == "BUY" else ROUND_FLOOR)
    if price <= 0 or abs(price / raw - 1) * 10000 > _money(policy.maximum_limit_offset_bps) + Decimal("1e-9"):
        return None, "LIMIT_PRICE_OFFSET_EXCEEDS_CAP"
    return price, None


def _make_decision(signal, portfolio, activation, policy, timestamp, *, action, quantity,
                   hypothetical_quantity, price, code, reason, time_in_force, sell_capacity=0,
                   horizon_ceiling=0, maximum_quote_age_seconds=60.):
    probability = finite(signal.calibrated_probability)
    direction = stock_direction(probability) if probability is not None and 0 <= probability <= 1 else "UNAVAILABLE"
    purpose = "DIRECTION_EXIT" if direction == "BEARISH" else "ENTRY" if direction == "BULLISH" else "HOLD"
    prediction = {**asdict(signal), "position_purpose": purpose,
        "direction": direction, "sizing_policy": GAMEPLAN_SIZING_POLICY,
        "allocation_policy": "independent-stock-allocation-1-2-3-4-v1",
        "horizon_weight": FIXED_HORIZON_WEIGHTS[signal.primary_horizon],
        "horizon_budget_fraction": FIXED_HORIZON_WEIGHTS[signal.primary_horizon] / 10,
        "authorized_sell_capacity": sell_capacity}
    fingerprint = _fingerprint(policy, maximum_quote_age_seconds)
    identifier = decision_identifier({"decided_at": timestamp.isoformat(), "symbol": signal.symbol,
        "prediction": prediction, "policy_fingerprint": fingerprint,
        "activation_checksum_sha256": activation.checksum_sha256, "decision_lane": "LIVE"})
    selected_price = float(price) if quantity else None
    payload = (build_schwab_stock_order_payload(symbol=signal.symbol, instruction=action, order_type="LIMIT",
        time_in_force=time_in_force, position_effect="OPENING" if action == "BUY" else "CLOSING",
        quantity=quantity, price=selected_price) if quantity else None)
    quote = portfolio.quotes.get(signal.symbol)
    midpoint = _uses_midpoint(quote, policy, action, time_in_force)
    return TradeDecision(
        decision_id=identifier, decided_at=timestamp.isoformat(), symbol=signal.symbol,
        action=action if quantity else "NO_TRADE", suggested_action={"BULLISH": "BUY", "BEARISH": "SELL"}.get(direction, "HOLD"),
        quantity=quantity, hypothetical_quantity=hypothetical_quantity,
        order_type="LIMIT" if quantity else None, limit_price=selected_price, protective_price=None,
        expected_net_return=None, expected_net_dollars=None, trade_probability=None,
        allocation_fraction=None, execution_urgency=None, decision_reason_code=code, decision_reason=reason,
        order_style_reason_code=f"GAMEPLAN_CURRENT_{'MIDPOINT' if midpoint else 'ASK' if action == 'BUY' else 'BID'}_LIMIT" if quantity else f"NO_ORDER_{code}",
        order_style_reason=("The limit uses the current bid/ask midpoint because the spread exceeds the working spread threshold."
                            if midpoint else "The limit uses the current ask for buys or current bid for sells, rounded to the permitted price increment.") if quantity else "No order was selected.",
        prediction=prediction, enrichment={**_metadata(policy, maximum_quote_age_seconds), "horizon_notional_ceiling": float(horizon_ceiling),
            "current_order_notional": float(quantity * price) if quantity else 0.},
        portfolio=_portfolio_summary(portfolio, signal.symbol), quote=asdict(quote) if quote is not None else {},
        order_payload=payload, policy_version=GAMEPLAN_SIZING_POLICY, policy_fingerprint=fingerprint,
        activation_checksum_sha256=activation.checksum_sha256, decision_lane="LIVE")


def _due_exit(decision, portfolio, activation, policy, timestamp, *, active_allocations, ledger_ready,
              seen, remaining, capacity, time_in_force, maximum_quote_age_seconds):
    quantity, code = decision.quantity, decision.decision_reason_code
    if type(quantity) is not int or quantity < 0:
        raise ValueError("Due exits require nonnegative whole-share quantities")
    price = None
    if quantity:
        allocation = decision.prediction.get("allocation_id")
        key = (decision.symbol, decision.prediction.get("primary_horizon"))
        if (decision.action != "SELL" or decision.decision_lane != "LIVE"
                or decision.prediction.get("position_purpose") != "EXIT" or not allocation
                or key not in active_allocations
                or decision.activation_checksum_sha256 != activation.checksum_sha256):
            raise ValueError("Only existing reconciled horizon exits may precede Gameplan decisions")
        if allocation in seen:
            quantity, code = 0, "DUPLICATE_OWNED_EXIT"
        elif not activation.active:
            quantity, code = 0, "TRADER_INACTIVE"
        elif not ledger_ready:
            quantity, code = 0, "HORIZON_LEDGER_UNRECONCILED"
        elif not (utc(decision.prediction["target_window_start"]) <= timestamp < utc(decision.prediction["actionable_until"])):
            quantity, code = 0, "OWNED_EXIT_WINDOW_CLOSED"
        elif not capacity:
            quantity, code = 0, "LOWER_RANKED_THAN_WAKE_ORDER_CAP"
        else:
            price, quote_error = _current_price(decision.symbol, portfolio, policy, timestamp, "SELL", time_in_force, maximum_quote_age_seconds)
            if quote_error:
                quantity, code = 0, quote_error
            else:
                single_cap = _shares(_money(portfolio.account_equity) * _money(policy.maximum_single_order_equity_fraction),
                    max(price, _money(portfolio.quotes[decision.symbol].ask)))
                quantity = min(quantity, remaining.get(decision.symbol, 0), single_cap)
                if not quantity:
                    code = "JOINT_OWNED_SHARE_CAP_EXHAUSTED"
                else:
                    remaining[decision.symbol] -= quantity
                    capacity -= 1
        seen.add(allocation)
    prediction = {**decision.prediction, "sizing_policy": GAMEPLAN_SIZING_POLICY}
    fingerprint = _fingerprint(policy, maximum_quote_age_seconds)
    identifier = decision_identifier({"decided_at": timestamp.isoformat(), "symbol": decision.symbol,
        "prediction": prediction, "policy_fingerprint": fingerprint,
        "activation_checksum_sha256": activation.checksum_sha256, "decision_lane": "LIVE"})
    payload = (build_schwab_stock_order_payload(symbol=decision.symbol, instruction="SELL", order_type="LIMIT",
        time_in_force=time_in_force, position_effect="CLOSING", quantity=quantity, price=float(price)) if quantity else None)
    quote = portfolio.quotes.get(decision.symbol)
    result = replace(decision, decision_id=identifier, decided_at=timestamp.isoformat(), quantity=quantity,
        action="SELL" if quantity else "NO_TRADE", order_type="LIMIT" if quantity else None,
        limit_price=float(price) if quantity else None, protective_price=None, order_payload=payload,
        expected_net_return=None, expected_net_dollars=None, trade_probability=None, allocation_fraction=None,
        execution_urgency=None, decision_reason_code=code,
        decision_reason=decision.decision_reason if quantity else "The due exit remains pending because its current ownership, quote, window, or batch limit is unavailable.",
        order_style_reason_code=("GAMEPLAN_CURRENT_MIDPOINT_LIMIT" if _uses_midpoint(quote, policy, "SELL", time_in_force) else "GAMEPLAN_CURRENT_BID_LIMIT") if quantity else f"NO_ORDER_{code}",
        order_style_reason=("The due exit uses the current bid/ask midpoint for a wide spread." if _uses_midpoint(quote, policy, "SELL", time_in_force) else "The due exit is repriced from the current bid.") if quantity else "No order was selected.",
        prediction=prediction, enrichment={**_metadata(policy, maximum_quote_age_seconds), "position_purpose": "EXIT"},
        portfolio=_portfolio_summary(portfolio, decision.symbol), quote=asdict(quote) if quote is not None else {},
        policy_version=GAMEPLAN_SIZING_POLICY, policy_fingerprint=fingerprint)
    return result, capacity


def build_gameplan_direction_trade_decisions(
    signals: Mapping[tuple[str, str], PredictionSignal], portfolio: PortfolioState, activation: ActivationIntent,
    *, verified_promoted_signals: frozenset[tuple[str, str]], active_allocations: frozenset[tuple[str, str]],
    ledger_ready: bool, bearish_sell_capacities: Mapping[tuple[str, str], int], decided_at: object,
    policy: StockTraderPolicy | None = None, time_in_force: str = "DAY",
    exit_decisions: tuple[TradeDecision, ...] = (),
    maximum_quote_age_seconds: float = 60.,
    late_opening_date: str | None = None,
    recovered_forecast_ids: frozenset[str] = frozenset(),
) -> tuple[TradeDecision, ...]:
    """Apply 54/46 directions using actual capital, inventory, and current quotes.

    Bearish capacities are an explicit caller attestation of authorized,
    disjoint inventory slices. They must exclude pending sells, protected other
    horizons, and every allocation represented in ``exit_decisions``. The batch
    additionally caps their sum at current held shares minus pending sells.
    Submitted sells never increase the cash or exposure budgets for this batch.
    """
    active_policy = policy or StockTraderPolicy()
    active_policy.validate()
    if late_opening_date is not None:
        from ml.stock_trader.gameplan import validate_late_opening_date
        validate_late_opening_date(late_opening_date, decided_at)
    if (not isinstance(recovered_forecast_ids, frozenset)
            or not recovered_forecast_ids.issubset({s.prediction_id for s in signals.values() if s.primary_horizon == "1h"})):
        raise ValueError("Quote recovery must identify supplied one-hour forecasts")
    if time_in_force not in {"DAY", "AM", "PM", "EXT", "GTC_EXT"}:
        raise ValueError("Unsupported Gameplan stock time in force")
    if not isinstance(active_allocations, frozenset) or type(ledger_ready) is not bool:
        raise ValueError("Gameplan planning requires explicit reconciled allocation state")
    if type(active_policy.maximum_orders_per_wake) is not int:
        raise ValueError("The combined stock order cap must be an integer")
    if type(active_policy.price_decimals) is not int or not 0 <= active_policy.price_decimals <= 8:
        raise ValueError("Invalid stock limit-price precision")
    if finite(maximum_quote_age_seconds) is None or not 0 < maximum_quote_age_seconds <= 300:
        raise ValueError("Current quote age limit must be within (0, 300] seconds")
    if (not isinstance(bearish_sell_capacities, Mapping) or not set(bearish_sell_capacities).issubset(signals)
            or any(type(qty) is not int or qty < 0 for qty in bearish_sell_capacities.values())):
        raise ValueError("Bearish capacities must be nonnegative whole shares for supplied signal keys")
    for key, signal in signals.items():
        if key != (signal.symbol, signal.primary_horizon) or signal.primary_horizon not in FIXED_HORIZON_WEIGHTS:
            raise ValueError("Gameplan signal key differs from its stock/horizon")
    timestamp = utc(decided_at)
    capacity = active_policy.maximum_orders_per_wake
    sell_remaining = {symbol: _shares(max(_ZERO, _money(held) - _money(portfolio.pending_sell_shares.get(symbol, 0))), Decimal(1))
                      for symbol, held in portfolio.held_shares.items()}
    results, seen = [], set()
    for decision in exit_decisions:
        result, capacity = _due_exit(decision, portfolio, activation, active_policy, timestamp,
            active_allocations=active_allocations, ledger_ready=ledger_ready, seen=seen,
            remaining=sell_remaining, capacity=capacity, time_in_force=time_in_force,
            maximum_quote_age_seconds=maximum_quote_age_seconds)
        results.append(result)
    try:
        available, symbol_remaining = _joint_budgets(portfolio, active_policy)
        budget_error = False
    except (TypeError, ValueError, ArithmeticError):
        available, symbol_remaining, budget_error = _ZERO, {}, True

    def rank(key):
        probability = finite(signals[key].calibrated_probability)
        direction = stock_direction(probability) if probability is not None and 0 <= probability <= 1 else "NO_EDGE"
        return (0 if direction == "BEARISH" else 1 if direction == "BULLISH" else 2,
                -probability if direction == "BULLISH" else 0, FIXED_HORIZON_WEIGHTS[key[1]], key[0])

    for key in sorted(signals, key=rank):
        signal = signals[key]
        probability = finite(signal.calibrated_probability)
        ready = probability is not None and 0 <= probability <= 1
        code = "ELIGIBLE"
        reason = "The Gameplan direction uses current cash, eligible shares, and the current quote."
        quantity = hypothetical = 0
        price = None
        ceiling = _ZERO
        direction = stock_direction(signal.calibrated_probability) if ready else "UNAVAILABLE"
        action = "SELL" if direction == "BEARISH" else "BUY" if direction == "BULLISH" else "HOLD"
        if not activation.active:
            code, reason = "TRADER_INACTIVE", activation.reason
        elif not ledger_ready:
            code = "HORIZON_LEDGER_UNRECONCILED"
        elif not ready:
            code = "FORECAST_PROBABILITY_INVALID"
        elif direction == "NO_EDGE":
            code, reason = "NEUTRAL_HOLD", "An exact 50% probability has no directional signal."
        else:
            start, end = utc(signal.target_window_start), utc(signal.target_window_end)
            deadline = min(utc(signal.actionable_until), end)
            if not start <= timestamp < deadline:
                code = "ENTRY_WINDOW_CLOSED"
            elif action == "BUY" and key in active_allocations:
                code = "HORIZON_ALLOCATION_ALREADY_ACTIVE"
            else:
                price, quote_error = _current_price(signal.symbol, portfolio, active_policy, timestamp, action, time_in_force, maximum_quote_age_seconds)
                if quote_error:
                    code = quote_error
                elif action == "SELL":
                    single_cap = _shares(_money(portfolio.account_equity) * _money(active_policy.maximum_single_order_equity_fraction),
                        max(price, _money(portfolio.quotes[signal.symbol].ask)))
                    hypothetical = min(bearish_sell_capacities.get(key, 0), single_cap)
                    quantity = min(hypothetical, sell_remaining.get(signal.symbol, 0))
                    if not quantity:
                        code = "NO_AUTHORIZED_SELL_SHARES"
                elif budget_error:
                    code = "JOINT_PORTFOLIO_BUDGET_INVALID"
                else:
                    ceiling = _money(portfolio.account_equity) * min(
                        _money(active_policy.maximum_symbol_equity_fraction) * FIXED_HORIZON_WEIGHTS[key[1]] / 10,
                        _money(active_policy.maximum_single_order_equity_fraction))
                    # The native inventory ledger values exposure at the ask,
                    # even when the operator's limit is the lower midpoint.
                    valuation = max(price, _money(portfolio.quotes[signal.symbol].ask))
                    hypothetical = _shares(ceiling, valuation)
                    quantity = min(hypothetical, _shares(available, price),
                                   _shares(symbol_remaining.get(signal.symbol, _ZERO), valuation))
                    if not quantity or quantity * price < _money(active_policy.minimum_order_notional):
                        quantity, code = 0, "JOINT_PORTFOLIO_BUDGET_EXHAUSTED"
                if quantity and not capacity:
                    quantity, code = 0, "LOWER_RANKED_THAN_WAKE_ORDER_CAP"
                if quantity:
                    if action == "SELL":
                        sell_remaining[signal.symbol] -= quantity
                    else:
                        available -= quantity * price
                        symbol_remaining[signal.symbol] -= quantity * max(price, _money(portfolio.quotes[signal.symbol].ask))
                    capacity -= 1
        if not quantity and code not in {"TRADER_INACTIVE", "NEUTRAL_HOLD"}:
            reason = "The current forecast, entry window, quote, reconciled inventory, actual capital, or configured batch limit does not permit this order."
        decision = _make_decision(signal, portfolio, activation, active_policy, timestamp,
            action=action, quantity=quantity, hypothetical_quantity=hypothetical, price=price,
            code=code, reason=reason, time_in_force=time_in_force,
            sell_capacity=bearish_sell_capacities.get(key, 0), horizon_ceiling=ceiling,
            maximum_quote_age_seconds=maximum_quote_age_seconds)
        if signal.prediction_id in recovered_forecast_ids:
            decision = replace(decision, prediction={**decision.prediction, "quote_recovery": True})
        results.append(decision)
    return tuple(results)


__all__ = ["build_gameplan_direction_trade_decisions"]
