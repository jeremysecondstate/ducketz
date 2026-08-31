from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Mapping

import pandas as pd

from ml.universe import PRODUCTION_LOOPS_SYMBOLS


STOCK_TRADER_SYMBOLS: tuple[str, ...] = PRODUCTION_LOOPS_SYMBOLS
STOCK_TRADER_DECISION_SCHEMA_VERSION = "stock-trader-decision-v2"
STOCK_TRADER_DECISION_RUN_SCHEMA_VERSION = "stock-trader-decision-run-v2"
STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION = "stock-trader-execution-event-v1"
STOCK_TRADER_OUTCOME_SCHEMA_VERSION = "stock-trader-outcome-v1"
STOCK_TRADER_WEEKLY_AUDIT_SCHEMA_VERSION = "stock-trader-weekly-audit-v1"


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def finite(value: object, *, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


@dataclass(frozen=True)
class ActivationIntent:
    active: bool
    status: str
    reason: str
    path: str
    checksum_sha256: str | None


@dataclass(frozen=True)
class QuoteState:
    symbol: str
    bid: float
    ask: float
    last: float | None
    mark: float | None
    volume: float | None
    observed_at: str

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def relative_spread(self) -> float:
        return self.spread / self.midpoint if self.midpoint > 0 else 0.0


@dataclass(frozen=True)
class PortfolioState:
    observed_at: str
    account_equity: float
    available_cash: float
    gross_exposure: float
    daily_pnl: float
    held_shares: Mapping[str, float]
    symbol_exposure: Mapping[str, float]
    pending_buy_shares: Mapping[str, float]
    pending_sell_shares: Mapping[str, float]
    working_order_count: int
    quotes: Mapping[str, QuoteState]
    source_fingerprint: str

    def effective_shares(self, symbol: str) -> float:
        return (
            float(self.held_shares.get(symbol, 0.0))
            + float(self.pending_buy_shares.get(symbol, 0.0))
            - float(self.pending_sell_shares.get(symbol, 0.0))
        )

    def available_sell_shares(self, symbol: str) -> float:
        return max(
            0.0,
            float(self.held_shares.get(symbol, 0.0))
            - float(self.pending_sell_shares.get(symbol, 0.0)),
        )


@dataclass(frozen=True)
class PredictionSignal:
    symbol: str
    primary_horizon: str
    prediction_id: str
    decision_timestamp: str
    target_window_start: str
    target_window_end: str
    actionable_until: str
    prediction_created_at: str
    calibrated_probability: float
    assumed_round_trip_cost: float
    horizon_probabilities: Mapping[str, float]
    model_name: str
    model_version: str
    source_fingerprint: str

    @property
    def suggested_action(self) -> str:
        return "BUY" if self.calibrated_probability >= 0.5 else "SELL"


@dataclass(frozen=True)
class EnrichmentOutput:
    model_name: str
    model_version: str
    model_fingerprint: str
    trade_probability: float
    allocation_fraction: float
    expected_net_return: float
    adverse_return: float
    execution_urgency: float
    limit_offset_bps: float
    protective_distance_pct: float
    expected_holding_minutes: float
    feature_values: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class StockTraderPolicy:
    policy_version: str = "stock-trader-bootstrap-policy-v1"
    minimum_trade_probability: float = 0.55
    minimum_expected_net_return: float = 0.0
    maximum_symbol_equity_fraction: float = 0.15
    maximum_gross_equity_fraction: float = 1.30
    maximum_single_order_equity_fraction: float = 0.05
    maximum_cash_utilization_fraction: float = 0.95
    minimum_order_notional: float = 25.0
    maximum_orders_per_wake: int = 6
    allow_market_orders: bool = False
    maximum_limit_offset_bps: float = 20.0
    maximum_protective_distance_fraction: float = 0.15
    passive_urgency_ceiling: float = 0.25
    midpoint_urgency_ceiling: float = 0.55
    marketable_urgency_ceiling: float = 0.85
    price_decimals: int = 2

    def validate(self) -> None:
        probability_fields = (
            self.minimum_trade_probability,
            self.maximum_symbol_equity_fraction,
            self.maximum_single_order_equity_fraction,
            self.maximum_cash_utilization_fraction,
            self.passive_urgency_ceiling,
            self.midpoint_urgency_ceiling,
            self.marketable_urgency_ceiling,
        )
        if any(not 0.0 <= value <= 1.0 for value in probability_fields):
            raise ValueError("Stock trader policy fractions must be within [0, 1]")
        if not (
            self.passive_urgency_ceiling
            < self.midpoint_urgency_ceiling
            < self.marketable_urgency_ceiling
        ):
            raise ValueError("Stock trader urgency thresholds must increase")
        if self.maximum_gross_equity_fraction <= 0.0:
            raise ValueError("maximum_gross_equity_fraction must be positive")
        if self.maximum_orders_per_wake < 1:
            raise ValueError("maximum_orders_per_wake must be positive")
        if self.minimum_order_notional < 0.0:
            raise ValueError("minimum_order_notional cannot be negative")
        if self.maximum_limit_offset_bps < 0.0:
            raise ValueError("maximum_limit_offset_bps cannot be negative")
        if not 0.0 < self.maximum_protective_distance_fraction < 1.0:
            raise ValueError(
                "maximum_protective_distance_fraction must be within (0, 1)"
            )

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(asdict(self))


@dataclass(frozen=True)
class TradeDecision:
    decision_id: str
    decided_at: str
    symbol: str
    action: str
    suggested_action: str
    quantity: int
    hypothetical_quantity: int
    order_type: str | None
    limit_price: float | None
    protective_price: float | None
    expected_net_return: float | None
    expected_net_dollars: float | None
    trade_probability: float | None
    allocation_fraction: float | None
    execution_urgency: float | None
    decision_reason_code: str
    decision_reason: str
    order_style_reason_code: str
    order_style_reason: str
    prediction: Mapping[str, object]
    enrichment: Mapping[str, object]
    portfolio: Mapping[str, object]
    quote: Mapping[str, object]
    order_payload: Mapping[str, object] | None
    policy_version: str
    policy_fingerprint: str
    activation_checksum_sha256: str | None
    decision_lane: str = "LIVE"

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["schema_version"] = STOCK_TRADER_DECISION_SCHEMA_VERSION
        return payload


def decision_identifier(payload: Mapping[str, object]) -> str:
    prediction_id = (
        payload.get("prediction", {}).get("prediction_id")
        if isinstance(payload.get("prediction"), Mapping)
        else None
    )
    decided_at = payload.get("decided_at")
    try:
        hourly_anchor = utc(decided_at).floor("h").isoformat()
    except (TypeError, ValueError):
        hourly_anchor = str(decided_at)
    decision_anchor = f"{prediction_id or 'NO_PREDICTION'}@{hourly_anchor}"
    identity = {
        "schema_version": STOCK_TRADER_DECISION_SCHEMA_VERSION,
        "symbol": payload.get("symbol"),
        # Repeated attempts in one hourly decision slot receive the same
        # identifier even if a scheduler retry starts seconds later. A later
        # hourly slot remains a separate auditable decision, including when an
        # upstream prediction is temporarily unchanged.
        "decision_anchor": decision_anchor,
        "policy_fingerprint": payload.get("policy_fingerprint"),
        "activation_checksum_sha256": payload.get("activation_checksum_sha256"),
        "decision_lane": str(payload.get("decision_lane") or "LIVE").upper(),
    }
    return canonical_sha256(identity)


def utc(value: object | None = None) -> pd.Timestamp:
    timestamp = pd.Timestamp.now(tz="UTC") if value is None else pd.to_datetime(value, utc=True)
    if pd.isna(timestamp):
        raise ValueError("timestamp is invalid")
    return pd.Timestamp(timestamp)


__all__ = [
    "ActivationIntent",
    "EnrichmentOutput",
    "PortfolioState",
    "PredictionSignal",
    "QuoteState",
    "STOCK_TRADER_DECISION_RUN_SCHEMA_VERSION",
    "STOCK_TRADER_DECISION_SCHEMA_VERSION",
    "STOCK_TRADER_EXECUTION_EVENT_SCHEMA_VERSION",
    "STOCK_TRADER_OUTCOME_SCHEMA_VERSION",
    "STOCK_TRADER_SYMBOLS",
    "STOCK_TRADER_WEEKLY_AUDIT_SCHEMA_VERSION",
    "StockTraderPolicy",
    "TradeDecision",
    "canonical_json",
    "canonical_sha256",
    "decision_identifier",
    "finite",
    "utc",
]
