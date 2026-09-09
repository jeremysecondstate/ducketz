from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class PositionOutcome(str, Enum):
    WIN = "Win"
    LOSS = "Loss"
    BREAKEVEN = "Breakeven"


@dataclass(frozen=True)
class OptionContract:
    occ_symbol: str
    underlying_symbol: str
    expiration: date
    strike: float
    option_type: str
    multiplier: float


@dataclass(frozen=True)
class ExecutionFill:
    execution_id: str
    account_label: str
    order_id: str
    package_id: str
    activity_id: str | None
    executed_at: datetime
    instruction: str
    quantity: float
    price: float
    contract: OptionContract
    fees: float | None
    package_strategy: str | None
    package_leg_ratio: float
    order_entered_at: datetime | None
    provenance: tuple[str, ...]
    transaction_type: str | None = None

    @property
    def gross_cash_flow(self) -> float:
        sign = 1.0 if self.instruction.startswith("SELL") else -1.0
        return sign * self.quantity * self.price * self.contract.multiplier

    @property
    def net_cash_flow(self) -> float:
        return self.gross_cash_flow - (self.fees or 0.0)

    @property
    def is_opening(self) -> bool:
        return self.instruction in {"BUY_TO_OPEN", "SELL_TO_OPEN"}

    @property
    def is_closing(self) -> bool:
        return self.instruction in {"BUY_TO_CLOSE", "SELL_TO_CLOSE"}


@dataclass(frozen=True)
class PositionTimelineEvent:
    label: str
    occurred_at: datetime
    detail: str
    provenance: str


@dataclass(frozen=True)
class ClosedPositionLeg:
    contract: OptionContract
    opening_instruction: str
    closing_instruction: str
    quantity: float
    entry_price: float | None
    exit_price: float | None
    opening_cash_flow: float | None
    closing_cash_flow: float | None


@dataclass(frozen=True)
class ClosedPosition:
    position_id: str
    account_label: str
    underlying_symbol: str
    strategy_label: str
    open_time: datetime | None
    close_time: datetime | None
    quantity: float
    opening_cash_flow: float | None
    closing_cash_flow: float | None
    realized_pnl: float | None
    return_fraction: float | None
    holding_days: float | None
    outcome: PositionOutcome | None
    legs: tuple[ClosedPositionLeg, ...]
    timeline: tuple[PositionTimelineEvent, ...]
    order_ids: tuple[str, ...]
    fees: float | None
    fees_complete: bool
    close_reason: str | None
    notes: str | None
    max_profit: float | None
    max_loss: float | None
    eligible: bool
    unavailable_reasons: tuple[str, ...]
    provenance: tuple[str, ...]

    @property
    def expiration(self) -> date | None:
        expirations = {leg.contract.expiration for leg in self.legs}
        return next(iter(expirations)) if len(expirations) == 1 else None


@dataclass(frozen=True)
class HistoryCoverage:
    order_count: int = 0
    transaction_count: int = 0
    fill_count: int = 0
    duplicate_fill_count: int = 0
    non_option_count: int = 0
    invalid_execution_count: int = 0
    ambiguous_package_count: int = 0
    unmatched_open_quantity: float = 0.0
    unmatched_close_quantity: float = 0.0
    excluded_position_count: int = 0
    fees_unavailable_count: int = 0
    messages: tuple[str, ...] = ()

    @property
    def excluded_count(self) -> int:
        return (
            self.invalid_execution_count
            + self.ambiguous_package_count
            + self.excluded_position_count
        )

    @property
    def summary(self) -> str:
        parts = [f"{self.fill_count:,} option fills"]
        if self.excluded_count:
            parts.append(f"{self.excluded_count:,} incomplete or ambiguous record(s) excluded")
        if self.unmatched_close_quantity:
            parts.append(f"{self.unmatched_close_quantity:g} unmatched closing contract(s)")
        if self.unmatched_open_quantity:
            parts.append(f"{self.unmatched_open_quantity:g} open residual contract(s) omitted")
        if self.fees_unavailable_count:
            parts.append(
                f"fees unavailable for {self.fees_unavailable_count:,} closed position(s)"
            )
        return " · ".join(parts)


@dataclass(frozen=True)
class PastPositionsSnapshot:
    positions: tuple[ClosedPosition, ...]
    coverage: HistoryCoverage
    range_start: date
    range_end: date
    observed_at: datetime
    status: str
    stale: bool = False
    refresh_error: str | None = None


@dataclass(frozen=True)
class PastPositionFilters:
    account: str = "All Accounts"
    date_range: str = "YTD"
    symbol: str = ""
    strategy: str = "All Strategies"
    group_by: str = "Month"


@dataclass(frozen=True)
class CumulativePnlPoint:
    closed_at: datetime
    value: float


@dataclass(frozen=True)
class StrategyPerformance:
    strategy_label: str
    realized_pnl: float
    position_count: int


@dataclass(frozen=True)
class PerformanceSummary:
    net_realized_pnl: float | None
    win_count: int
    loss_count: int
    breakeven_count: int
    win_rate: float | None
    gross_profit: float
    gross_loss: float
    profit_factor: float | None
    average_days_held: float | None
    holding_time_count: int
    included_position_count: int
    excluded_position_count: int
    cumulative_pnl: tuple[CumulativePnlPoint, ...]
    strategy_performance: tuple[StrategyPerformance, ...]


__all__ = [
    "ClosedPosition",
    "ClosedPositionLeg",
    "CumulativePnlPoint",
    "ExecutionFill",
    "HistoryCoverage",
    "OptionContract",
    "PastPositionFilters",
    "PastPositionsSnapshot",
    "PerformanceSummary",
    "PositionOutcome",
    "PositionTimelineEvent",
    "StrategyPerformance",
]
