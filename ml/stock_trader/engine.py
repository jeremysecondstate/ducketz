from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Mapping

from app.services.schwab_stock_orders import build_schwab_stock_order_payload
from ml.stock_trader.contracts import (
    ActivationIntent,
    EnrichmentOutput,
    PortfolioState,
    PredictionSignal,
    QuoteState,
    STOCK_TRADER_SYMBOLS,
    StockTraderPolicy,
    TradeDecision,
    decision_identifier,
    utc,
)
from ml.stock_trader.model import EnrichmentModel, build_feature_values


@dataclass
class _Candidate:
    symbol: str
    signal: PredictionSignal
    quote: QuoteState
    enrichment: EnrichmentOutput
    side: str
    hypothetical_quantity: int
    quantity: int
    eligibility_code: str
    eligibility_reason: str
    order_type: str | None = None
    limit_price: float | None = None
    order_style_code: str = "NO_ORDER"
    order_style_reason: str = "No order was selected."

    @property
    def reference_price(self) -> float:
        return self.quote.midpoint

    @property
    def expected_net_dollars(self) -> float:
        return (
            self.quantity
            * self.reference_price
            * self.enrichment.expected_net_return
        )


def build_trade_decisions(
    signals: Mapping[str, PredictionSignal],
    portfolio: PortfolioState,
    model: EnrichmentModel | None,
    activation: ActivationIntent,
    *,
    policy: StockTraderPolicy | None = None,
    decided_at: object,
    model_unavailable_reason: str | None = None,
    decision_lane: str = "LIVE",
) -> tuple[TradeDecision, ...]:
    """Create six auditable decisions from one shared state snapshot.

    The ML model supplies target allocation/liquidation fraction and execution
    controls.  Deterministic arithmetic only makes those outputs feasible given
    owned shares, cash, exposure and working-order state.
    """

    active_policy = policy or StockTraderPolicy()
    active_policy.validate()
    lane = str(decision_lane or "").upper()
    if lane not in {"LIVE", "SHADOW"}:
        raise ValueError("decision_lane must be LIVE or SHADOW")
    timestamp = utc(decided_at)
    decided_at_iso = timestamp.isoformat()
    candidates: dict[str, _Candidate] = {}
    direct_decisions: dict[str, TradeDecision] = {}
    for symbol in STOCK_TRADER_SYMBOLS:
        signal = signals.get(symbol)
        quote = portfolio.quotes.get(symbol)
        if not activation.active:
            direct_decisions[symbol] = _direct_no_trade(
                symbol,
                signal,
                quote,
                portfolio,
                activation,
                active_policy,
                decided_at_iso,
                decision_lane=lane,
                code="TRADER_INACTIVE",
                reason=f"Active trading is disabled: {activation.reason}.",
            )
            continue
        if signal is None:
            direct_decisions[symbol] = _direct_no_trade(
                symbol,
                None,
                quote,
                portfolio,
                activation,
                active_policy,
                decided_at_iso,
                decision_lane=lane,
                code="ACTIONABLE_1H_PREDICTION_UNAVAILABLE",
                reason="No current actionable LIVE 1h Loop B prediction was available.",
            )
            continue
        if quote is None:
            direct_decisions[symbol] = _direct_no_trade(
                symbol,
                signal,
                None,
                portfolio,
                activation,
                active_policy,
                decided_at_iso,
                decision_lane=lane,
                code="USABLE_QUOTE_UNAVAILABLE",
                reason="The shared Schwab snapshot had no usable bid/ask quote.",
            )
            continue
        if model is None:
            features = build_feature_values(signal, portfolio, quote, as_of=timestamp)
            direct_decisions[symbol] = _direct_no_trade(
                symbol,
                signal,
                quote,
                portfolio,
                activation,
                active_policy,
                decided_at_iso,
                decision_lane=lane,
                code="ENRICHMENT_MODEL_UNAVAILABLE",
                reason=(
                    "No verified stock-trader enrichment model was published."
                    + (
                        f" Loader detail: {model_unavailable_reason}"
                        if model_unavailable_reason
                        else ""
                    )
                ),
                enrichment={
                    "model_name": None,
                    "model_version": None,
                    "model_fingerprint": None,
                    "feature_values": features,
                },
            )
            continue
        try:
            features = build_feature_values(signal, portfolio, quote, as_of=timestamp)
            enrichment = model.predict(features)
        except (ArithmeticError, TypeError, ValueError) as exc:
            direct_decisions[symbol] = _direct_no_trade(
                symbol,
                signal,
                quote,
                portfolio,
                activation,
                active_policy,
                decided_at_iso,
                decision_lane=lane,
                code="ENRICHMENT_INFERENCE_FAILED",
                reason=f"Enrichment inference failed: {type(exc).__name__}: {exc}",
            )
            continue
        side = signal.suggested_action
        hypothetical_quantity = _model_quantity(
            side,
            symbol,
            enrichment,
            portfolio,
            quote,
            active_policy,
        )
        code, reason = _eligibility(
            enrichment,
            hypothetical_quantity,
            quote,
            active_policy,
        )
        quantity = hypothetical_quantity if code == "ELIGIBLE" else 0
        candidates[symbol] = _Candidate(
            symbol=symbol,
            signal=signal,
            quote=quote,
            enrichment=enrichment,
            side=side,
            hypothetical_quantity=hypothetical_quantity,
            quantity=quantity,
            eligibility_code=code,
            eligibility_reason=reason,
        )

    _jointly_clamp_buys(candidates, portfolio, active_policy)
    _limit_order_count(candidates, active_policy)
    for candidate in candidates.values():
        if candidate.quantity > 0:
            (
                candidate.order_type,
                candidate.limit_price,
                candidate.order_style_code,
                candidate.order_style_reason,
            ) = _order_style(candidate, active_policy)
        else:
            candidate.order_style_code, candidate.order_style_reason = _no_order_style(
                candidate.eligibility_code
            )
    return tuple(
        direct_decisions.get(symbol)
        or _decision_from_candidate(
            candidates[symbol],
            portfolio,
            activation,
            active_policy,
            decided_at_iso,
            decision_lane=lane,
        )
        for symbol in STOCK_TRADER_SYMBOLS
    )


def _model_quantity(
    side: str,
    symbol: str,
    enrichment: EnrichmentOutput,
    portfolio: PortfolioState,
    quote: QuoteState,
    policy: StockTraderPolicy,
) -> int:
    allocation = min(1.0, max(0.0, enrichment.allocation_fraction))
    if side == "SELL":
        model_quantity = portfolio.available_sell_shares(symbol) * allocation
        single_order_cap = (
            portfolio.account_equity * policy.maximum_single_order_equity_fraction
        )
        return max(
            0,
            math.floor(min(model_quantity, single_order_cap / quote.midpoint)),
        )
    equity = portfolio.account_equity
    desired_symbol_exposure = (
        equity * policy.maximum_symbol_equity_fraction * allocation
    )
    observed_symbol_exposure = max(
        0.0, float(portfolio.symbol_exposure.get(symbol, 0.0))
    )
    pending_buy_value = (
        max(0.0, float(portfolio.pending_buy_shares.get(symbol, 0.0)))
        * quote.midpoint
    )
    desired_increment = max(
        0.0,
        desired_symbol_exposure - observed_symbol_exposure - pending_buy_value,
    )
    single_order_cap = equity * policy.maximum_single_order_equity_fraction
    return max(0, math.floor(min(desired_increment, single_order_cap) / quote.midpoint))


def _eligibility(
    enrichment: EnrichmentOutput,
    quantity: int,
    quote: QuoteState,
    policy: StockTraderPolicy,
) -> tuple[str, str]:
    if enrichment.trade_probability < policy.minimum_trade_probability:
        return (
            "LOW_TRADE_PROBABILITY",
            "The enrichment model's trade probability did not clear the policy threshold.",
        )
    if enrichment.expected_net_return <= policy.minimum_expected_net_return:
        return (
            "WEAK_EXPECTED_VALUE_AFTER_WAITING_AND_SLIPPAGE",
            "Expected value after modeled waiting, spread, slippage and costs was not positive enough.",
        )
    if quantity <= 0:
        return (
            "NO_FEASIBLE_MODEL_SIZED_QUANTITY",
            "The model-sized action became zero against current cash, exposure or owned shares.",
        )
    if quantity * quote.midpoint < policy.minimum_order_notional:
        return (
            "BELOW_MINIMUM_ORDER_NOTIONAL",
            "The model-sized action was below the minimum order notional.",
        )
    return "ELIGIBLE", "The enriched prediction and model-sized quantity are eligible."


def _jointly_clamp_buys(
    candidates: Mapping[str, _Candidate],
    portfolio: PortfolioState,
    policy: StockTraderPolicy,
) -> None:
    buys = [
        candidate
        for candidate in candidates.values()
        if candidate.side == "BUY" and candidate.quantity > 0
    ]
    if not buys:
        return
    cash_budget = max(
        0.0, portfolio.available_cash * policy.maximum_cash_utilization_fraction
    )
    gross_budget = max(
        0.0,
        portfolio.account_equity * policy.maximum_gross_equity_fraction
        - portfolio.gross_exposure,
    )
    budget = min(cash_budget, gross_budget)
    desired = sum(candidate.quantity * candidate.reference_price for candidate in buys)
    scale = min(1.0, budget / desired) if desired > 0.0 else 0.0
    for candidate in buys:
        quantity = math.floor(candidate.quantity * scale)
        candidate.quantity = max(0, quantity)
        if candidate.quantity <= 0:
            candidate.eligibility_code = "JOINT_PORTFOLIO_BUDGET_EXHAUSTED"
            candidate.eligibility_reason = (
                "Joint six-symbol cash/gross allocation left no whole-share quantity."
            )
        elif candidate.quantity * candidate.reference_price < policy.minimum_order_notional:
            candidate.quantity = 0
            candidate.eligibility_code = "BELOW_MINIMUM_ORDER_NOTIONAL_AFTER_JOINT_SIZING"
            candidate.eligibility_reason = (
                "Joint six-symbol sizing reduced the action below minimum notional."
            )


def _limit_order_count(
    candidates: Mapping[str, _Candidate], policy: StockTraderPolicy
) -> None:
    eligible = [candidate for candidate in candidates.values() if candidate.quantity > 0]
    ranked = sorted(
        eligible,
        key=lambda item: (
            item.expected_net_dollars * item.enrichment.trade_probability,
            item.enrichment.trade_probability,
            item.symbol,
        ),
        reverse=True,
    )
    for candidate in ranked[policy.maximum_orders_per_wake :]:
        candidate.quantity = 0
        candidate.eligibility_code = "LOWER_RANKED_THAN_WAKE_ORDER_CAP"
        candidate.eligibility_reason = (
            "A higher-value set of model-sized actions filled the per-wake order capacity."
        )


def _order_style(
    candidate: _Candidate, policy: StockTraderPolicy
) -> tuple[str, float | None, str, str]:
    urgency = candidate.enrichment.execution_urgency
    quote = candidate.quote
    if urgency < policy.passive_urgency_ceiling:
        price = quote.bid if candidate.side == "BUY" else quote.ask
        return (
            "LIMIT",
            round(price, policy.price_decimals),
            "LOW_URGENCY_STABLE_PREDICTION_PASSIVE_LIMIT",
            "Low urgency and a stable prediction selected a passive limit order.",
        )
    if urgency < policy.midpoint_urgency_ceiling:
        return (
            "LIMIT",
            round(quote.midpoint, policy.price_decimals),
            "MODERATE_URGENCY_MIDPOINT_LIMIT",
            "Moderate urgency selected a midpoint limit order.",
        )
    offset = candidate.enrichment.limit_offset_bps / 10_000.0
    offset = min(offset, policy.maximum_limit_offset_bps / 10_000.0)
    marketable = quote.ask * (1.0 + offset) if candidate.side == "BUY" else quote.bid * (
        1.0 - offset
    )
    marketable = max(10 ** (-policy.price_decimals), marketable)
    if urgency < policy.marketable_urgency_ceiling:
        return (
            "LIMIT",
            round(marketable, policy.price_decimals),
            "HIGH_URGENCY_DECAYING_OPPORTUNITY_MARKETABLE_LIMIT",
            "High urgency and faster opportunity decay selected a marketable limit order.",
        )
    if policy.allow_market_orders:
        return (
            "MARKET",
            None,
            "VERY_HIGH_URGENCY_MARKET_ORDER",
            "Very high urgency selected an explicitly enabled market order.",
        )
    return (
        "LIMIT",
        round(marketable, policy.price_decimals),
        "VERY_HIGH_URGENCY_MARKETABLE_LIMIT_MARKET_DISABLED",
        "Very high urgency selected a marketable limit because market orders are disabled.",
    )


def _no_order_style(eligibility_code: str) -> tuple[str, str]:
    if eligibility_code == "WEAK_EXPECTED_VALUE_AFTER_WAITING_AND_SLIPPAGE":
        return (
            "NO_ORDER_WEAK_EXPECTED_VALUE_AFTER_WAITING_AND_SLIPPAGE",
            "Weak expected value after waiting, spread, slippage and costs selected no order.",
        )
    return (
        f"NO_ORDER_{eligibility_code}",
        "No order was selected because the enriched decision was not eligible.",
    )


def _decision_from_candidate(
    candidate: _Candidate,
    portfolio: PortfolioState,
    activation: ActivationIntent,
    policy: StockTraderPolicy,
    decided_at: str,
    *,
    decision_lane: str,
) -> TradeDecision:
    action = candidate.side if candidate.quantity > 0 else "NO_TRADE"
    protective_price = (
        round(
            candidate.reference_price
            * (
                1.0
                - min(
                    candidate.enrichment.protective_distance_pct,
                    policy.maximum_protective_distance_fraction,
                )
            ),
            policy.price_decimals,
        )
        if candidate.side == "BUY"
        and candidate.quantity > 0
        and 0.0 < candidate.enrichment.protective_distance_pct < 1.0
        else None
    )
    order_payload = None
    if candidate.quantity > 0 and candidate.order_type is not None:
        order_payload = build_schwab_stock_order_payload(
            symbol=candidate.symbol,
            instruction=candidate.side,
            order_type=candidate.order_type,
            time_in_force="DAY",
            position_effect="AUTO" if candidate.side == "BUY" else "CLOSING",
            quantity=candidate.quantity,
            price=candidate.limit_price if candidate.limit_price is not None else "",
        )
    prediction = asdict(candidate.signal)
    enrichment = asdict(candidate.enrichment)
    quote = asdict(candidate.quote)
    portfolio_summary = _portfolio_summary(portfolio, candidate.symbol)
    base: dict[str, object] = {
        "decided_at": decided_at,
        "symbol": candidate.symbol,
        "prediction": prediction,
        "policy_fingerprint": policy.fingerprint,
        "activation_checksum_sha256": activation.checksum_sha256,
        "decision_lane": decision_lane,
    }
    decision_id = decision_identifier(base)
    expected_dollars = (
        candidate.quantity
        * candidate.reference_price
        * candidate.enrichment.expected_net_return
        if candidate.quantity > 0
        else candidate.hypothetical_quantity
        * candidate.reference_price
        * candidate.enrichment.expected_net_return
    )
    return TradeDecision(
        decision_id=decision_id,
        decided_at=decided_at,
        symbol=candidate.symbol,
        action=action,
        suggested_action=candidate.side,
        quantity=candidate.quantity,
        hypothetical_quantity=candidate.hypothetical_quantity,
        order_type=candidate.order_type,
        limit_price=candidate.limit_price,
        protective_price=protective_price,
        expected_net_return=candidate.enrichment.expected_net_return,
        expected_net_dollars=expected_dollars,
        trade_probability=candidate.enrichment.trade_probability,
        allocation_fraction=candidate.enrichment.allocation_fraction,
        execution_urgency=candidate.enrichment.execution_urgency,
        decision_reason_code=candidate.eligibility_code,
        decision_reason=candidate.eligibility_reason,
        order_style_reason_code=candidate.order_style_code,
        order_style_reason=candidate.order_style_reason,
        prediction=prediction,
        enrichment=enrichment,
        portfolio=portfolio_summary,
        quote=quote,
        order_payload=order_payload,
        policy_version=policy.policy_version,
        policy_fingerprint=policy.fingerprint,
        activation_checksum_sha256=activation.checksum_sha256,
        decision_lane=decision_lane,
    )


def _direct_no_trade(
    symbol: str,
    signal: PredictionSignal | None,
    quote: QuoteState | None,
    portfolio: PortfolioState,
    activation: ActivationIntent,
    policy: StockTraderPolicy,
    decided_at: str,
    *,
    decision_lane: str,
    code: str,
    reason: str,
    enrichment: Mapping[str, object] | None = None,
) -> TradeDecision:
    prediction = asdict(signal) if signal is not None else {}
    base: dict[str, object] = {
        "decided_at": decided_at,
        "symbol": symbol,
        "prediction": prediction,
        "policy_fingerprint": policy.fingerprint,
        "activation_checksum_sha256": activation.checksum_sha256,
        "decision_lane": decision_lane,
    }
    return TradeDecision(
        decision_id=decision_identifier(base),
        decided_at=decided_at,
        symbol=symbol,
        action="NO_TRADE",
        suggested_action=signal.suggested_action if signal is not None else "NONE",
        quantity=0,
        hypothetical_quantity=0,
        order_type=None,
        limit_price=None,
        protective_price=None,
        expected_net_return=None,
        expected_net_dollars=None,
        trade_probability=None,
        allocation_fraction=None,
        execution_urgency=None,
        decision_reason_code=code,
        decision_reason=reason,
        order_style_reason_code=f"NO_ORDER_{code}",
        order_style_reason="No order was selected.",
        prediction=prediction,
        enrichment=dict(enrichment or {}),
        portfolio=_portfolio_summary(portfolio, symbol),
        quote=asdict(quote) if quote is not None else {},
        order_payload=None,
        policy_version=policy.policy_version,
        policy_fingerprint=policy.fingerprint,
        activation_checksum_sha256=activation.checksum_sha256,
        decision_lane=decision_lane,
    )


def _portfolio_summary(portfolio: PortfolioState, symbol: str) -> dict[str, object]:
    return {
        "observed_at": portfolio.observed_at,
        "account_equity": portfolio.account_equity,
        "available_cash": portfolio.available_cash,
        "gross_exposure": portfolio.gross_exposure,
        "daily_pnl": portfolio.daily_pnl,
        "held_shares": float(portfolio.held_shares.get(symbol, 0.0)),
        "effective_shares": portfolio.effective_shares(symbol),
        "available_sell_shares": portfolio.available_sell_shares(symbol),
        "symbol_exposure": float(portfolio.symbol_exposure.get(symbol, 0.0)),
        "pending_buy_shares": float(portfolio.pending_buy_shares.get(symbol, 0.0)),
        "pending_sell_shares": float(portfolio.pending_sell_shares.get(symbol, 0.0)),
        "working_order_count": portfolio.working_order_count,
        "source_fingerprint": portfolio.source_fingerprint,
    }


__all__ = ["build_trade_decisions"]
