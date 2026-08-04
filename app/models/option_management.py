from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
    "OptionPositionBook",
    "OptionPositionLeg",
    "OptionPositionSummary",
    "SavedExitPlanTemplate",
]
