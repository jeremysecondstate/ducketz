from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

MarketDataSource = Literal["schwab", "databento"]


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    source: MarketDataSource
    fetched_at: datetime
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    mark: float | None = None
    volume: float | None = None
    note: str = ""
    quote_event_at: datetime | None = None


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    source: MarketDataSource
    timeframe: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
