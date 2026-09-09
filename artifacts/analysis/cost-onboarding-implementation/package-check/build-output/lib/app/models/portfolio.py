from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


Source = Literal["schwab", "hyperliquid"]


@dataclass(frozen=True)
class Holding:
    symbol: str
    quantity: float
    price: float
    value: float
    source: Source
    bucket: str
    unrealized_pnl: float | None = None
    day_pnl: float | None = None


@dataclass(frozen=True)
class CashBalance:
    symbol: str
    amount: float
    value: float
    source: Source
    bucket: str


@dataclass(frozen=True)
class PortfolioSnapshot:
    source: Source
    account_label: str
    cash: list[CashBalance] = field(default_factory=list)
    holdings: list[Holding] = field(default_factory=list)
    synced_at: datetime | None = None
    status: str = "not synced"
    reported_total_value: float | None = None
    account_facts: dict[str, object] = field(default_factory=dict)

    @property
    def cash_value(self) -> float:
        return round(sum(cash.value for cash in self.cash), 2)

    @property
    def holdings_value(self) -> float:
        return round(sum(holding.value for holding in self.holdings), 2)

    @property
    def total_value(self) -> float:
        if self.reported_total_value is not None:
            return round(self.reported_total_value, 2)
        return round(self.cash_value + self.holdings_value, 2)

    @property
    def unrealized_pnl(self) -> float | None:
        values = [holding.unrealized_pnl for holding in self.holdings if holding.unrealized_pnl is not None]
        return round(sum(values), 2) if values else None

    @property
    def day_pnl(self) -> float | None:
        values = [holding.day_pnl for holding in self.holdings if holding.day_pnl is not None]
        return round(sum(values), 2) if values else None

    @property
    def has_day_pnl(self) -> bool:
        return self.day_pnl is not None
