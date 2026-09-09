"""Joint stock sizing for independently owned forecast horizons.

This module plans orders; it never calls a broker or treats a submission as a
fill. Holdings supplied here must come from the reconciled horizon ledger.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Mapping

from ml.stock_trader.contracts import (
    ActivationIntent, EnrichmentOutput, PortfolioState, PredictionSignal,
    StockTraderPolicy, TradeDecision, utc,
)
from ml.stock_trader.engine import (
    _Candidate, _decision_from_candidate, _direct_no_trade, _eligibility,
    _no_order_style, _order_style,
)
from ml.stock_trader.model import EnrichmentModel, build_feature_values

HORIZON_WEIGHTS = {"1h": 1, "4h": 2, "1d": 3, "1w": 4}
HORIZON_ALLOCATION_POLICY = "independent-stock-allocation-1-2-3-4-v1"


def build_independent_trade_decisions(
    signals: Mapping[tuple[str, str], PredictionSignal],
    portfolio: PortfolioState,
    model: EnrichmentModel | None,
    activation: ActivationIntent,
    *,
    active_allocations: frozenset[tuple[str, str]],
    ledger_ready: bool,
    decided_at: object,
    policy: StockTraderPolicy | None = None,
    time_in_force: str = "DAY",
    exit_decisions: tuple[TradeDecision, ...] = (),
) -> tuple[TradeDecision, ...]:
    """Size one combined batch without multiplying existing account limits.

    Each horizon owns a fixed share of the existing per-symbol allocation
    ceiling. The trained enrichment allocation can only reduce that ceiling.
    Bearish entries never borrow shares from another horizon or open shorts.
    Authorized exits take precedence over new entries in the shared order cap.
    """
    from ml.stock_trader.model import require_enrichment_signal_support

    active_policy = policy or StockTraderPolicy()
    active_policy.validate()
    if time_in_force not in {"DAY", "AM", "PM", "EXT", "GTC_EXT"}:
        raise ValueError("Unsupported stock time in force")
    timestamp = utc(decided_at)
    candidates: dict[tuple[str, str], _Candidate] = {}
    direct: dict[tuple[str, str], TradeDecision] = {}
    for key, signal in sorted(signals.items()):
        symbol, horizon = key
        if key != (signal.symbol, signal.primary_horizon) or horizon not in HORIZON_WEIGHTS:
            raise ValueError("Independent signal key does not match its symbol and horizon")
        quote = portfolio.quotes.get(symbol)
        code = reason = None
        if not activation.active:
            code, reason = "TRADER_INACTIVE", activation.reason
        elif not ledger_ready:
            code, reason = "HORIZON_LEDGER_UNRECONCILED", "Independent share ownership has not been reconciled."
        elif key in active_allocations:
            code, reason = "HORIZON_ALLOCATION_ALREADY_ACTIVE", "This horizon already owns or has reserved an allocation."
        elif signal.suggested_action == "SELL":
            code, reason = "NO_OWNED_HORIZON_SHARES", "A bearish entry cannot sell another horizon's shares or open a short."
        elif not utc(signal.target_window_start) <= timestamp < utc(signal.actionable_until):
            code, reason = "ENTRY_WINDOW_CLOSED", "The exact forecast entry window is not open."
        elif quote is None:
            code, reason = "USABLE_QUOTE_UNAVAILABLE", "A usable current stock quote is required."
        elif model is None:
            code, reason = "ENRICHMENT_MODEL_UNAVAILABLE", "No verified enrichment model was supplied."
        else:
            try:
                require_enrichment_signal_support(model, signal)
            except ValueError as exc:
                code, reason = "ENRICHMENT_HORIZON_NOT_QUALIFIED", str(exc)
        if code is not None:
            direct[key] = _direct_no_trade(
                symbol, signal, quote, portfolio, activation, active_policy,
                timestamp.isoformat(), decision_lane="LIVE", code=code, reason=str(reason),
            )
            continue
        try:
            enrichment = model.predict(build_feature_values(signal, portfolio, quote, as_of=timestamp))
            fraction = HORIZON_WEIGHTS[horizon] / sum(HORIZON_WEIGHTS.values())
            cap = portfolio.account_equity * min(
                active_policy.maximum_symbol_equity_fraction * fraction,
                active_policy.maximum_single_order_equity_fraction,
            )
            allocated = cap * min(1.0, max(0.0, enrichment.allocation_fraction))
            quantity = max(0, math.floor(allocated / quote.ask))
            code, reason = _eligibility(
                enrichment, quantity, quote, active_policy,
                extended_session=time_in_force != "DAY",
            )
            candidate = _Candidate(
                symbol, signal, quote, enrichment, "BUY", quantity,
                quantity if code == "ELIGIBLE" else 0, code, reason,
            )
            if candidate.quantity:
                (candidate.order_type, candidate.limit_price,
                 candidate.order_style_code, candidate.order_style_reason) = _order_style(
                    candidate, active_policy, force_limit=True,
                )
                # Whole-share feasibility uses the actual limit, including any
                # spread/offset, rather than the cheaper midpoint.
                candidate.quantity = min(candidate.quantity, math.floor(allocated / candidate.limit_price))
            candidates[key] = candidate
        except (ArithmeticError, TypeError, ValueError) as exc:
            direct[key] = _direct_no_trade(
                symbol, signal, quote, portfolio, activation, active_policy,
                timestamp.isoformat(), decision_lane="LIVE", code="ENRICHMENT_INFERENCE_FAILED",
                reason=f"Enrichment inference failed: {type(exc).__name__}: {exc}",
            )

    # Exits never fund entries until their fills appear in a later coherent
    # portfolio snapshot. Working-order reservations are already in this one.
    cash = max(0.0, portfolio.available_cash * active_policy.maximum_cash_utilization_fraction)
    gross = max(0.0, portfolio.account_equity * active_policy.maximum_gross_equity_fraction - portfolio.gross_exposure)
    available = min(cash, gross)
    symbol_remaining = {
        symbol: max(0.0, portfolio.account_equity * active_policy.maximum_symbol_equity_fraction
                    - max(0.0, float(portfolio.symbol_exposure.get(symbol, 0)))
                    - max(0.0, float(portfolio.pending_buy_shares.get(symbol, 0))) * quote.ask)
        for symbol, quote in portfolio.quotes.items()
    }
    eligible_exits = tuple(d for d in exit_decisions if d.quantity > 0)
    if any(d.action != "SELL" or d.decision_lane != "LIVE" for d in eligible_exits):
        raise ValueError("Only reconciled owned-share exits may precede entries")
    # One combined cap, even at 04:00 when all four horizons are due.
    exit_results = []
    sell_remaining = {symbol: int(portfolio.available_sell_shares(symbol)) for symbol in portfolio.held_shares}
    exit_count = 0
    for decision in exit_decisions:
        if decision.quantity > 0:
            quantity = min(decision.quantity, sell_remaining.get(decision.symbol, 0))
            if exit_count >= active_policy.maximum_orders_per_wake:
                decision = _suppress(decision, "LOWER_RANKED_THAN_WAKE_ORDER_CAP")
            elif quantity <= 0:
                decision = _suppress(decision, "JOINT_OWNED_SHARE_CAP_EXHAUSTED")
            else:
                if quantity != decision.quantity:
                    from app.services.schwab_stock_orders import build_schwab_stock_order_payload
                    decision = replace(decision, quantity=quantity, order_payload=build_schwab_stock_order_payload(
                        symbol=decision.symbol, instruction="SELL", order_type="LIMIT", time_in_force=time_in_force,
                        position_effect="CLOSING", quantity=quantity, price=decision.limit_price,
                    ))
                sell_remaining[decision.symbol] -= quantity
                exit_count += 1
        exit_results.append(decision)
    exits = tuple(exit_results)
    capacity = max(0, active_policy.maximum_orders_per_wake - sum(d.quantity > 0 for d in exits))
    ranked = sorted(
        candidates.values(),
        key=lambda c: (-c.expected_net_dollars * c.enrichment.trade_probability,
                       c.symbol, -HORIZON_WEIGHTS[c.signal.primary_horizon]),
    )
    for candidate in ranked:
        if not candidate.quantity:
            continue
        price = float(candidate.limit_price)
        if capacity == 0:
            candidate.quantity = 0
            candidate.eligibility_code = "LOWER_RANKED_THAN_WAKE_ORDER_CAP"
        else:
            candidate.quantity = min(
                candidate.quantity, max(0, math.floor(available / price)),
                max(0, math.floor(symbol_remaining[candidate.symbol] / price)),
            )
            if candidate.quantity * price < active_policy.minimum_order_notional:
                candidate.quantity = 0
                candidate.eligibility_code = "JOINT_PORTFOLIO_BUDGET_EXHAUSTED"
            else:
                notional = candidate.quantity * price
                available -= notional
                symbol_remaining[candidate.symbol] -= notional
                capacity -= 1
        if not candidate.quantity:
            candidate.eligibility_reason = "The combined exit and entry batch exhausted its existing order or capital limit."
    for key, candidate in candidates.items():
        if not candidate.quantity:
            candidate.order_type = None
            candidate.limit_price = None
            candidate.order_style_code, candidate.order_style_reason = _no_order_style(candidate.eligibility_code)
        decision = _decision_from_candidate(
            candidate, portfolio, activation, active_policy, timestamp.isoformat(),
            decision_lane="LIVE", time_in_force=time_in_force,
        )
        direct[key] = replace(decision, prediction={
            **decision.prediction,
            "allocation_policy": HORIZON_ALLOCATION_POLICY,
            "horizon_weight": HORIZON_WEIGHTS[key[1]],
            "horizon_budget_fraction": HORIZON_WEIGHTS[key[1]] / 10,
            "position_purpose": "ENTRY",
        })
    return (*exits, *(direct[key] for key in sorted(direct)))


def _suppress(decision: TradeDecision, code: str) -> TradeDecision:
    return replace(
        decision, action="NO_TRADE", quantity=0, order_type=None,
        limit_price=None, order_payload=None, protective_price=None,
        decision_reason_code=code,
        decision_reason="The combined batch reached its existing order cap; this exit remains due.",
    )
