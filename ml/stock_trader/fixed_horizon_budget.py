"""Reviewable deterministic stock sizing, separate from learned enrichment.

This module plans one long-only entry. It does not activate a trader, load or
promote a model, call a broker, manage exits, or replace the combined-batch cash,
owned-share, and six-order accounting in the independent execution engine.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING

from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION, STOCK_TIMEZONE, stock_target_windows
from ml.stock_target_prices import stock_price_dataset
from ml.stock_direction_policy import BULLISH_PROBABILITY, stock_direction
from ml.stock_trader.contracts import (
    ActivationIntent, PortfolioState, PredictionSignal, QuoteState,
    STOCK_TRADER_SYMBOLS, StockTraderPolicy, finite, utc,
)


FIXED_HORIZON_BUDGET_POLICY_VERSION = "independent-stock-fixed-confidence-budget-v1"
FIXED_HORIZON_WEIGHTS = {"1h": 1, "4h": 2, "1d": 3, "1w": 4}
MAXIMUM_HORIZON_BUDGET_UTILIZATION = .5


@dataclass(frozen=True)
class FixedHorizonBudgetSizing:
    """Policy amounts, not fitted return estimates or second-model probabilities."""

    status: str
    reason_code: str
    reason: str
    symbol: str
    horizon: str
    prediction_id: str
    forecast_probability: float | None
    horizon_weight: int
    horizon_budget_fraction: float
    confidence_budget_fraction: float
    horizon_notional_ceiling: float
    confidence_notional_budget: float
    available_notional_budget: float
    quantity: int
    limit_price: float | None
    order_notional: float
    target_window_end: str
    policy_version: str = FIXED_HORIZON_BUDGET_POLICY_VERSION
    sizing_basis: str = "deterministic_budget_from_qualified_stock_forecast"
    combined_batch_accounting_required: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def fixed_budget_forecast_readiness(
    signal: PredictionSignal, *, forecast_promoted: bool, policy: StockTraderPolicy | None = None,
) -> dict[str, object]:
    """The caller must derive promotion from a verified frozen stock publication.

    Qualified bearish/neutral forecasts mean the policy is ready with no long
    entry signal. That is distinct from research or missing forecast authority.
    No comparison with another horizon is performed.
    """
    active_policy = policy or StockTraderPolicy()
    active_policy.validate()
    probability = finite(signal.calibrated_probability)
    threshold = max(BULLISH_PROBABILITY, active_policy.minimum_trade_probability)
    reason = None
    if forecast_promoted is not True:
        reason = "FORECAST_NOT_PROMOTED"
    elif signal.symbol not in STOCK_TRADER_SYMBOLS or signal.primary_horizon not in FIXED_HORIZON_WEIGHTS:
        reason = "FORECAST_SYMBOL_OR_HORIZON_UNSUPPORTED"
    elif signal.target_definition_version != STOCK_TARGET_CONTRACT_VERSION:
        reason = "FORECAST_TARGET_CONTRACT_MISMATCH"
    elif probability is None or not 0 <= probability <= 1:
        reason = "FORECAST_PROBABILITY_INVALID"
    else:
        try:
            stock_price_dataset(signal.target_price_source_contract)
            start, end = utc(signal.target_window_start), utc(signal.target_window_end)
            valid_window = any(spec["execution_eligible"] and spec["model_group"] == signal.primary_horizon
                and utc(spec["target_window_start"]) == start and utc(spec["target_window_end"]) == end
                for spec in stock_target_windows(start.tz_convert(STOCK_TIMEZONE).date()))
            if not valid_window:
                reason = "FORECAST_EXACT_TARGET_WINDOW_INVALID"
            elif utc(signal.prediction_created_at) > start or utc(signal.decision_timestamp) > start:
                reason = "FORECAST_CONTAINS_FUTURE_INFORMATION"
        except (TypeError, ValueError, KeyError):
            reason = "FORECAST_TARGET_OR_SOURCE_INVALID"
    if reason is not None:
        return {"status": "NOT_READY", "reason": reason, "entry_signal": False,
                "forecast_probability": probability, "minimum_forecast_probability": threshold}
    bullish = stock_direction(probability) == "BULLISH" and probability >= threshold
    return {"status": "READY" if bullish else "READY_WITH_NO_ENTRY_SIGNAL",
            "reason": "QUALIFIED_BULLISH_STOCK_FORECAST" if bullish else "NO_BULLISH_ENTRY_SIGNAL",
            "entry_signal": bullish, "forecast_probability": probability,
            "minimum_forecast_probability": threshold,
            "policy_version": FIXED_HORIZON_BUDGET_POLICY_VERSION}


def size_fixed_horizon_budget(
    signal: PredictionSignal,
    portfolio: PortfolioState,
    quote: QuoteState | None,
    activation: ActivationIntent,
    *,
    forecast_promoted: bool,
    ledger_ready: bool,
    has_active_allocation: bool,
    decided_at: object,
    policy: StockTraderPolicy | None = None,
) -> FixedHorizonBudgetSizing:
    """Produce a whole-share LIMIT entry candidate within existing risk ceilings.

    The maximum horizon amount is the existing symbol ceiling weighted 1:2:3:4
    and capped by the existing single-order ceiling. Confidence uses 2*p-1,
    clipped to [0, 0.5], solely as an explicit capital-utilization rule.
    Remaining cash/gross/symbol amounts are conservative single-candidate caps;
    the caller must reserve them jointly across the full batch before any POST.
    Existing owned allocations and bearish forecasts never become short sales.
    """
    active_policy = policy or StockTraderPolicy()
    active_policy.validate()
    probability = finite(signal.calibrated_probability)
    weight = FIXED_HORIZON_WEIGHTS.get(signal.primary_horizon, 0)
    fraction = weight / 10
    utilization_decimal = min(Decimal("0.5"), max(Decimal(0), 2 * Decimal(str(probability)) - 1)) if probability is not None else Decimal(0)
    utilization = float(utilization_decimal)
    ceiling = confidence_budget = available_budget = 0.

    def result(code: str, reason: str, *, quantity: int = 0, limit_price: float | None = None):
        return FixedHorizonBudgetSizing(
            "ELIGIBLE" if quantity else "NO_TRADE", code, reason, signal.symbol, signal.primary_horizon,
            signal.prediction_id, probability, weight, fraction, utilization, ceiling,
            confidence_budget, available_budget, quantity, limit_price,
            quantity * limit_price if quantity and limit_price is not None else 0., signal.target_window_end)

    if not activation.active:
        return result("TRADER_INACTIVE", activation.reason)
    if not ledger_ready:
        return result("HORIZON_LEDGER_UNRECONCILED", "Independent share ownership must be reconciled.")
    if has_active_allocation:
        return result("HORIZON_ALLOCATION_ALREADY_ACTIVE", "This horizon already owns or has reserved an allocation.")
    readiness = fixed_budget_forecast_readiness(signal, forecast_promoted=forecast_promoted, policy=active_policy)
    if readiness["status"] == "NOT_READY":
        return result(str(readiness["reason"]), "The frozen stock forecast is not eligible for this policy.")
    if not readiness["entry_signal"]:
        return result("NO_BULLISH_ENTRY_SIGNAL", "The qualified forecast does not request a long-stock entry.")
    try:
        timestamp, start, end = utc(decided_at), utc(signal.target_window_start), utc(signal.target_window_end)
        from ml.stock_trader.independent_signals import _entry_deadline
        deadline = min(utc(signal.actionable_until), _entry_deadline(start), end)
        if not start <= timestamp < deadline:
            return result("ENTRY_WINDOW_CLOSED", "The exact forecast entry window is not open.")
        if quote is None or quote.symbol != signal.symbol:
            return result("USABLE_QUOTE_UNAVAILABLE", "A fresh quote for the forecast symbol is required.")
        bid, ask = finite(quote.bid), finite(quote.ask)
        if bid is None or ask is None or not 0 < bid <= ask or utc(quote.observed_at) > timestamp:
            return result("USABLE_QUOTE_UNAVAILABLE", "The stock quote is invalid or contains future information.")
        if quote.relative_spread > active_policy.maximum_extended_relative_spread:
            return result("STOCK_SPREAD_TOO_WIDE", "The quote exceeds the existing spread ceiling.")
        if not isinstance(active_policy.price_decimals, int) or not 0 <= active_policy.price_decimals <= 8:
            return result("PRICE_PRECISION_INVALID", "The existing limit-price precision is invalid.")
        tick = Decimal(1).scaleb(-active_policy.price_decimals)
        limit_price = float(Decimal(str(ask)).quantize(tick, rounding=ROUND_CEILING))
        if (limit_price / ask - 1) * 10000 > active_policy.maximum_limit_offset_bps + 1e-9:
            return result("LIMIT_PRICE_OFFSET_EXCEEDS_CAP", "Rounding the ask would exceed the existing limit offset ceiling.")
        equity, cash, gross = (finite(value) for value in (portfolio.account_equity, portfolio.available_cash, portfolio.gross_exposure))
        exposure = finite(portfolio.symbol_exposure.get(signal.symbol, 0.))
        pending_buy = finite(portfolio.pending_buy_shares.get(signal.symbol, 0.))
        if any(value is None for value in (equity, cash, gross, exposure, pending_buy)) or equity <= 0 or gross < 0 or exposure < 0 or pending_buy < 0:
            return result("PORTFOLIO_BUDGET_INVALID", "The portfolio budget snapshot is invalid.")
        as_decimal = lambda value: Decimal(str(value))
        equity_decimal, limit_decimal = as_decimal(equity), as_decimal(limit_price)
        ceiling_decimal = equity_decimal * min(as_decimal(active_policy.maximum_symbol_equity_fraction) * Decimal(weight) / 10,
                                               as_decimal(active_policy.maximum_single_order_equity_fraction))
        confidence_decimal = ceiling_decimal * utilization_decimal
        available_decimal = max(Decimal(0), min(confidence_decimal,
            as_decimal(cash) * as_decimal(active_policy.maximum_cash_utilization_fraction),
            equity_decimal * as_decimal(active_policy.maximum_gross_equity_fraction) - as_decimal(gross),
            equity_decimal * as_decimal(active_policy.maximum_symbol_equity_fraction) - as_decimal(exposure) - as_decimal(pending_buy) * limit_decimal))
        ceiling, confidence_budget, available_budget = map(float, (ceiling_decimal, confidence_decimal, available_decimal))
        quantity = max(0, int(available_decimal / limit_decimal))
        if quantity < 1 or quantity * limit_decimal < as_decimal(active_policy.minimum_order_notional):
            return result("FIXED_BUDGET_TOO_SMALL", "The remaining budget cannot fund the minimum whole-share entry.")
        return result("ELIGIBLE", "Qualified stock forecast with a deterministic confidence-scaled horizon budget.",
                      quantity=quantity, limit_price=limit_price)
    except (ArithmeticError, ValueError, TypeError) as exc:
        return result("FIXED_BUDGET_INPUT_INVALID", f"The policy input is invalid: {type(exc).__name__}.")


__all__ = ["FIXED_HORIZON_BUDGET_POLICY_VERSION", "FIXED_HORIZON_WEIGHTS", "MAXIMUM_HORIZON_BUDGET_UTILIZATION",
           "FixedHorizonBudgetSizing", "fixed_budget_forecast_readiness", "size_fixed_horizon_budget"]
