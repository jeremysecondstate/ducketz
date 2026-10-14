"""Human-approved initial independent symbol sizing; pure calculations only.

Approval: October 12, 2026, initial 1.5%-equity scale, symbol TA starter and
nearest-share half-up rounding. There is no stream budget, protected cash,
Gameplan horizon dependency, confidence gate, broker access or submission here.
The caller owns persistence, freshness settings and actual capacity adjustment.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
import math
from typing import Mapping

from .prediction import ContractError, digest, encoded, identity, instant, number
from .technical_sizing import (
    AvailableInput, IndicatorSpec, ScoreRecipe, ScoreTerm, SymbolReference,
    SymbolSizingPolicy, WindowPolicy, evaluate_size, indicator_snapshot,
)


VERSION = "intraday-independent-initial-ta-v1"
EQUITY_SCALE = "0.015"


@dataclass(frozen=True)
class QuoteObservation:
    bid: float
    ask: float
    observed_at: str
    available_at: str
    source: str
    evidence: Mapping[str, object]


def starter_policy(symbol: str) -> SymbolSizingPolicy:
    """Separate versioned symbol policy; inputs and ATR are that symbol's own.

    Shared initial period values are explicit approved seeds, not optimized
    fits or a single computed score copied across symbols. All windows count
    observed bars; gaps/closures remain recorded and are never filled here.
    """
    identity(symbol)
    specifications = (
        IndicatorSpec("ema20", "ema", {"span": 20, "adjust": False}, True),
        IndicatorSpec("sma20", "sma", {"window": 20}, True),
        IndicatorSpec("rsi14", "rsi", {"window": 14, "smoothing": "simple"}, True),
        IndicatorSpec("bb20", "bollinger", {"window": 20, "deviations": 2., "ddof": 0}, True),
        IndicatorSpec("stoch14", "stochastic", {"window": 14, "k_smoothing": 3, "d_smoothing": 3}, True),
        IndicatorSpec("vwap20", "vwap", {"window": 20}, True),
        IndicatorSpec("momentum4", "momentum", {"lag": 4}, True),
        IndicatorSpec("atr14", "atr", {"window": 14, "smoothing": "simple"}, True),
        IndicatorSpec("relative_volume20", "relative_volume", {"window": 20, "baseline": "previous"}, True),
    )
    def recipe(side):
        direction = 1. if side == "BUY" else -1.
        terms = (
            ScoreTerm("ema20.distance", 0., 1., -1., 1., direction, "atr14.relative"),
            ScoreTerm("sma20.distance", 0., 1., -1., 1., direction, "atr14.relative"),
            ScoreTerm("vwap20.distance", 0., 1., -1., 1., direction, "atr14.relative"),
            ScoreTerm("rsi14.value", 50., 50., -1., 1., direction),
            ScoreTerm("bb20.percent_b", .5, .5, -1., 1., direction),
            ScoreTerm("stoch14.k", .5, .5, -1., 1., direction),
            ScoreTerm("momentum4.value", 0., 2., -1., 1., direction, "atr14.relative"),
            # Side orientation gives abs(2p-1) without an execution threshold.
            ScoreTerm("probability_not_down", .5, .5, -1., 1., direction),
            ScoreTerm("relative_volume20.value", 1., 1., -1., 1., 1.),
        )
        return ScoreRecipe(f"{VERSION}:{symbol}:{side}", terms, .5, 0., math.nextafter(1., 0.), 18.)
    return SymbolSizingPolicy(symbol, f"{VERSION}:{symbol}", specifications,
                              WindowPolicy("observed_history"), recipe("BUY"), recipe("SELL")).validate()


def independent_reference(forecast, *, account_equity, quote, calculated_at, maximum_quote_age_seconds):
    """Return (reference, unavailable reason), with no capacity subtraction.

    The fresh BUY ask measures proposed acquisition cost per share; the fresh
    SELL bid measures proposed disposal proceeds per share. This side-specific
    denominator is recorded and is independent of inventory/cash availability.
    """
    original = dict(forecast)
    forecast_id = original.pop("forecast_id", None)
    if forecast_id != digest(original):
        raise ContractError("Independent reference requires an original immutable forecast")
    at = instant(calculated_at)
    if not isinstance(maximum_quote_age_seconds, (int, float)) or isinstance(maximum_quote_age_seconds, bool) or not math.isfinite(maximum_quote_age_seconds) or maximum_quote_age_seconds <= 0:
        raise ContractError("An explicit positive quote freshness setting is required")
    if account_equity is None:
        return None, "account_equity_unavailable"
    if quote is None:
        return None, "fresh_quote_unavailable"
    if instant(account_equity.available_at) > at:
        return None, "account_equity_unavailable_at_sizing"
    if not account_equity.evidence or not account_equity.source:
        return None, "account_equity_evidence_unavailable"
    equity = number(account_equity.value)
    if equity == 0:
        return None, "positive_account_equity_unavailable"
    if not quote.evidence or not quote.source:
        return None, "quote_evidence_unavailable"
    if instant(quote.available_at) > at:
        return None, "quote_unavailable_at_sizing"
    if instant(quote.observed_at) > instant(quote.available_at):
        return None, "quote_observation_after_availability"
    age = (at - instant(quote.observed_at)).total_seconds()
    if not 0 <= age <= maximum_quote_age_seconds:
        return None, "quote_stale_or_future"
    bid, ask = number(quote.bid), number(quote.ask)
    if bid <= 0 or ask < bid:
        return None, "valid_bid_ask_unavailable"
    probability = number(forecast["probability_not_down"])
    if probability > 1:
        raise ContractError("Forecast probability must be in [0,1]")
    side = "BUY" if probability >= Decimal(".5") else "SELL"
    price = ask if side == "BUY" else bid
    with localcontext() as arithmetic:
        arithmetic.prec = 34
        arithmetic.rounding = ROUND_HALF_EVEN
        notional = equity * Decimal(EQUITY_SCALE)
        shares = notional / price
    observed = max(instant(account_equity.available_at), instant(quote.available_at))
    evidence = {"method": "independent_equity_scale_divided_by_side_quote", "equity_fraction": EQUITY_SCALE,
        "account_equity": asdict(account_equity), "quote": asdict(quote), "price_side": "ask" if side == "BUY" else "bid",
        "reference_price": str(price), "reference_notional": str(notional),
        "maximum_quote_age_seconds": maximum_quote_age_seconds, "quote_age_seconds": age,
        "cash_or_inventory_adjustment": "none", "stream_budget_or_set_aside": False,
        "scheduled_gameplan_order_required": False}
    return SymbolReference(str(forecast["symbol"]), side, str(shares), VERSION,
                           observed.isoformat(), evidence), None


def size_forecast(forecast, *, forecast_snapshot, account_equity: AvailableInput | None,
                  quote: QuoteObservation | None, calculated_at, maximum_quote_age_seconds,
                  account_inputs=None):
    """Approved desired size; caller persists it even when later capacity is zero.

    Extra account inputs are contextual evidence; the starter recipe does not
    use cash, holdings, allocations or recent executions to shrink desired size.
    Zero ATR, flat undefined bands and volume-free VWAP remain unavailable.
    """
    policy = starter_policy(str(forecast["symbol"]))
    snapshot_hash = forecast["snapshot_hash"]
    if digest(forecast_snapshot) != snapshot_hash:
        raise ContractError("Original forecast snapshot hash changed")
    indicators = indicator_snapshot(forecast_snapshot["candles"], symbol=policy.symbol,
        reference_at=forecast["reference_at"], data_cutoff=forecast["training_cutoff"],
        indicator_specs=policy.indicators, window_policy=policy.window,
        forecast_snapshot=forecast_snapshot, forecast_snapshot_hash=snapshot_hash)
    reference, reason = independent_reference(forecast, account_equity=account_equity, quote=quote,
        calculated_at=calculated_at, maximum_quote_age_seconds=maximum_quote_age_seconds)
    result = evaluate_size(forecast, indicators, symbol_policy=policy, symbol_reference=reference,
        account_inputs=account_inputs, calculated_at=calculated_at, half_share_ties="half_up",
        rounded_equality="allow", unavailable_reason=reason)
    # Immutable result contains enough local evidence to reconstruct indicators
    # as well as every term. This is private persisted sizing, not telemetry.
    payload = result.payload()
    payload.update(starter_policy_version=VERSION, indicator_snapshot=indicators.payload(),
                   initial_configuration_authority="human_approved_2026_10_12",
                   reference_context={"account_equity": asdict(account_equity) if account_equity is not None else None,
                        "quote": asdict(quote) if quote is not None else None,
                        "maximum_quote_age_seconds": maximum_quote_age_seconds},
                   reference_failure_reason=reason)
    return type(result)(digest(payload), encoded(payload))


__all__ = ["VERSION", "EQUITY_SCALE", "QuoteObservation", "starter_policy", "independent_reference", "size_forecast"]
