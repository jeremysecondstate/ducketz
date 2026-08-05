from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class OrderReviewOperation(str, Enum):
    CLOSE = "CLOSE"
    ROLL = "ROLL"
    EXIT_PLAN = "EXIT_PLAN"


class OrderReviewCashDirection(str, Enum):
    CREDIT = "CREDIT"
    DEBIT = "DEBIT"
    REFERENCE = "REFERENCE"
    UNAVAILABLE = "UNAVAILABLE"


class OrderReviewQuoteState(str, Enum):
    LIVE = "LIVE"
    AGING = "AGING"
    STALE = "STALE"
    UPDATING = "UPDATING"
    UNAVAILABLE = "UNAVAILABLE"


class OrderReviewNoticeSeverity(str, Enum):
    INFORMATION = "INFORMATION"
    WARNING = "WARNING"
    BLOCKING = "BLOCKING"


class OrderReviewPlacementCapability(str, Enum):
    SUPPORTED = "SUPPORTED"
    REVIEW_ONLY = "REVIEW_ONLY"
    UNAVAILABLE = "UNAVAILABLE"


class OrderReviewPlacementState(str, Enum):
    READY = "READY"
    REVALIDATING = "REVALIDATING"
    PREVIEWING = "PREVIEWING"
    FALLBACK = "FALLBACK"
    SUBMITTING = "SUBMITTING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class OrderReviewOutcomeStatus(str, Enum):
    ACCEPTED = "ACCEPTED"
    BLOCKED = "BLOCKED"
    INVALIDATED = "INVALIDATED"
    PREVIEW_REJECTED = "PREVIEW_REJECTED"
    REJECTED = "REJECTED"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    NETWORK_FAILED = "NETWORK_FAILED"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"


@dataclass(frozen=True)
class OptionOrderReviewLeg:
    role: str
    action: str
    quantity: int | None
    contract_label: str
    symbol: str
    bid: float | None
    ask: float | None
    mark: float | None
    before_quantity: float | None
    after_quantity: float | None
    quote_observed_at: datetime | None


@dataclass(frozen=True)
class OptionOrderReviewPriceRail:
    bid: float
    midpoint: float
    ask: float
    selected: float


@dataclass(frozen=True)
class OptionOrderReviewMetric:
    label: str
    before: str
    after: str
    provenance: str
    before_tone: str = "neutral"
    after_tone: str = "neutral"


@dataclass(frozen=True)
class OptionOrderReviewCost:
    label: str
    value: str
    provenance: str
    tone: str = "neutral"
    estimated: bool = False


@dataclass(frozen=True)
class OptionOrderReviewNotice:
    severity: OrderReviewNoticeSeverity
    title: str
    detail: str
    blocking: bool = False


@dataclass(frozen=True)
class OptionOrderReview:
    operation: OrderReviewOperation
    title: str
    subtitle: str
    account_display_label: str
    strategy_label: str
    instruction: str
    order_type: str
    duration: str
    execution_mode: str
    legs: tuple[OptionOrderReviewLeg, ...]
    package_quantity: int
    price_title: str
    net_price: float | None
    cash_direction: OrderReviewCashDirection
    price_rail: OptionOrderReviewPriceRail | None
    price_editable: bool
    price_editor_explanation: str
    estimated_cash_effect: float | None
    estimated_cash_label: str
    price_provenance: str
    display_quote_at: datetime | None
    validation_quote_at: datetime | None
    max_quote_age_seconds: float
    quote_state: OrderReviewQuoteState
    metrics: tuple[OptionOrderReviewMetric, ...]
    costs: tuple[OptionOrderReviewCost, ...]
    notices: tuple[OptionOrderReviewNotice, ...]
    acknowledgment_copy: str
    safety_copy: str
    placement_capability: OrderReviewPlacementCapability
    placement_disabled_reason: str | None
    primary_action_label: str
    broker_preview_status: str
    internal_valid: bool

    @property
    def has_blocking_notice(self) -> bool:
        return any(notice.blocking for notice in self.notices)


@dataclass(frozen=True)
class OrderReviewPlacementOutcome:
    status: OrderReviewOutcomeStatus
    message: str
    submission: ClosingOrderSubmission | None = None
    retryable: bool = False


@dataclass(frozen=True)
class OptionPositionLeg:
    account_label: str
    symbol: str
    underlying_symbol: str
    option_type: str
    expiration: str
    strike: float
    net_quantity: float
    settled_quantity: float | None
    contract_multiplier: float
    bid: float | None
    ask: float | None
    mark: float | None
    market_value: float | None
    unrealized_pnl: float | None
    day_pnl: float | None
    delta: float | None
    theta: float | None
    observed_at: datetime | None
    quote_observed_at: datetime | None
    source_ref: str
    close_disabled_reason: str | None = None

    @property
    def close_instruction(self) -> str:
        return "SELL_TO_CLOSE" if self.net_quantity > 0 else "BUY_TO_CLOSE"

    @property
    def absolute_quantity(self) -> float:
        return abs(self.net_quantity)


@dataclass(frozen=True)
class OptionPositionSummary:
    net_market_value: float | None
    unrealized_pnl: float | None
    day_pnl: float | None
    theta_per_day: float | None
    available_funds: float | None
    buying_power: float | None


@dataclass(frozen=True)
class OptionPositionBook:
    account_label: str
    observed_at: datetime | None
    status: str
    legs: tuple[OptionPositionLeg, ...]
    summary: OptionPositionSummary
    unavailable_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClosingOrderLeg:
    symbol: str
    display_name: str
    underlying_symbol: str
    expiration: str
    strike: float
    option_type: str
    instruction: str
    quantity: int
    ratio_quantity: int
    before_quantity: float
    after_quantity: float
    bid: float | None
    ask: float | None
    mark: float
    contract_multiplier: float
    quote_observed_at: datetime | None


@dataclass(frozen=True)
class ClosingOrderDraft:
    account_label: str
    reviewed_position_at: datetime | None
    oldest_quote_at: datetime | None
    scope_label: str
    legs: tuple[ClosingOrderLeg, ...]
    api_order_type: str
    complex_order_strategy_type: str | None
    order_quantity: int
    limit_price: float
    duration: str
    estimated_cash_effect: float
    price_source: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ExitPlanBranch:
    branch_id: str
    label: str
    enabled: bool
    trigger_basis: str
    trigger_operator: str
    trigger_percent: float | None
    trigger_price: float | None
    order_type: str
    limit_price: float | None
    limit_offset: float | None
    duration: str
    quantity_fraction: float
    closing_order: ClosingOrderDraft | None = None


@dataclass(frozen=True)
class ExitPlanDraft:
    template_id: str
    template_name: str
    account_label: str
    underlying_symbol: str
    coverage_label: str
    position_symbols: tuple[str, ...]
    position_mark: float
    price_source: str
    protected_quantity: int
    relationship: str
    branches: tuple[ExitPlanBranch, ...]
    executable: bool
    capability_reason: str | None
    conflicting_order_ids: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def placeable(self) -> bool:
        return self.executable and not self.conflicting_order_ids

    @property
    def take_profit(self) -> ExitPlanBranch | None:
        return next((branch for branch in self.branches if branch.branch_id.startswith("target")), None)

    @property
    def stop_loss(self) -> ExitPlanBranch | None:
        return next((branch for branch in self.branches if branch.branch_id == "stop"), None)


@dataclass(frozen=True)
class SavedExitPlanTemplate:
    name: str
    base_template_id: str
    target_percent: float
    stop_percent: float
    limit_offset: float
    duration: str


@dataclass(frozen=True)
class OptionChainContract:
    """One exact broker contract returned by an option-chain read."""

    symbol: str
    underlying_symbol: str
    option_type: str
    expiration: str
    strike: float
    bid: float | None
    ask: float | None
    mark: float | None
    delta: float | None
    theta: float | None
    contract_multiplier: float | None
    quote_observed_at: datetime | None


@dataclass(frozen=True)
class RollChainSnapshot:
    underlying_symbol: str
    underlying_price: float | None
    observed_at: datetime
    contracts: tuple[OptionChainContract, ...]
    unavailable_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class RollOrderLeg:
    role: str
    source_position_symbol: str
    symbol: str
    underlying_symbol: str
    option_type: str
    expiration: str
    strike: float
    instruction: str
    signed_quantity: int
    quantity: int
    ratio_quantity: int
    before_quantity: float
    after_quantity: float
    bid: float
    ask: float
    mark: float
    delta: float | None
    theta: float | None
    contract_multiplier: float
    quote_observed_at: datetime


@dataclass(frozen=True)
class RollPriceRail:
    bid: float
    midpoint: float
    ask: float
    selected: float


@dataclass(frozen=True)
class RollMetricSnapshot:
    max_profit: float | None
    max_profit_unbounded: bool
    max_loss: float | None
    max_loss_unbounded: bool
    breakevens: tuple[float, ...] | None
    delta: float | None
    theta_per_day: float | None
    buying_power: float | None


@dataclass(frozen=True)
class RollPayoffCurve:
    prices: tuple[float, ...]
    profit_loss: tuple[float, ...]
    unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.prices) and not self.unavailable_reason


@dataclass(frozen=True)
class RollAnalysis:
    underlying_price: float | None
    before_curve: RollPayoffCurve
    after_curve: RollPayoffCurve
    before_metrics: RollMetricSnapshot
    after_metrics: RollMetricSnapshot
    estimated_realized_pnl: float | None
    days_extended: int
    estimated_fees: float | None


@dataclass(frozen=True)
class RollOrderComponent:
    label: str
    legs: tuple[RollOrderLeg, ...]
    api_order_type: str
    complex_order_strategy_type: str | None
    order_quantity: int
    limit_price: float
    estimated_cash_effect: float


@dataclass(frozen=True)
class RollOrderDraft:
    account_label: str
    underlying_symbol: str
    reviewed_position_at: datetime | None
    oldest_quote_at: datetime
    position_symbols: tuple[str, ...]
    reviewed_position_quantities: tuple[tuple[str, float], ...]
    close_symbols: tuple[str, ...]
    scope_mode: str
    scope_label: str
    replacement_expiration: str
    keep_strike_widths: bool
    close_legs: tuple[RollOrderLeg, ...]
    replacement_legs: tuple[RollOrderLeg, ...]
    api_order_type: str
    complex_order_strategy_type: str | None
    order_quantity: int
    limit_price: float
    duration: str
    price_policy: str
    price_rail: RollPriceRail
    estimated_cash_effect: float
    execution_mode: str
    execution_detail: str
    atomic_order_supported: bool
    components: tuple[RollOrderComponent, ...]
    analysis: RollAnalysis
    price_source: str
    warnings: tuple[str, ...]
    review_blockers: tuple[str, ...]

    @property
    def review_eligible(self) -> bool:
        return not self.review_blockers

    @property
    def all_legs(self) -> tuple[RollOrderLeg, ...]:
        return self.close_legs + self.replacement_legs

    @property
    def is_credit(self) -> bool:
        return self.api_order_type == "NET_CREDIT"


@dataclass(frozen=True)
class SavedRollTemplate:
    name: str
    days_forward: int
    keep_strike_widths: bool
    duration: str
    price_policy: str


@dataclass(frozen=True)
class ClosingOrderSubmission:
    payload: dict[str, object]
    location: str | None


@dataclass(frozen=True)
class ManagedOrderLeg:
    symbol: str
    instruction: str
    quantity: float | None


@dataclass(frozen=True)
class ManagedOptionOrder:
    order_id: str
    status: str
    entered_time: str
    order_type: str
    complex_order_strategy_type: str
    duration: str
    remaining_quantity: float | None
    limit_price: float | None
    stop_price: float | None
    legs: tuple[ManagedOrderLeg, ...]
    can_cancel: bool
    cancel_disabled_reason: str | None


__all__ = [
    "ClosingOrderDraft",
    "ClosingOrderLeg",
    "ClosingOrderSubmission",
    "ExitPlanBranch",
    "ExitPlanDraft",
    "ManagedOptionOrder",
    "ManagedOrderLeg",
    "OptionChainContract",
    "OptionOrderReview",
    "OptionOrderReviewCost",
    "OptionOrderReviewLeg",
    "OptionOrderReviewMetric",
    "OptionOrderReviewNotice",
    "OptionOrderReviewPriceRail",
    "OptionPositionBook",
    "OptionPositionLeg",
    "OptionPositionSummary",
    "RollAnalysis",
    "RollChainSnapshot",
    "RollMetricSnapshot",
    "RollOrderComponent",
    "RollOrderDraft",
    "RollOrderLeg",
    "RollPayoffCurve",
    "RollPriceRail",
    "SavedExitPlanTemplate",
    "SavedRollTemplate",
    "OrderReviewCashDirection",
    "OrderReviewNoticeSeverity",
    "OrderReviewOperation",
    "OrderReviewOutcomeStatus",
    "OrderReviewPlacementCapability",
    "OrderReviewPlacementOutcome",
    "OrderReviewPlacementState",
    "OrderReviewQuoteState",
]
