from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from ml.contracts import MLContractError


@dataclass(frozen=True)
class ResearchInstrument:
    """Readable market and session metadata for one research symbol."""

    symbol: str
    venue: str
    currency: str
    exchange_calendar: str


INITIAL_RESEARCH_INSTRUMENTS: tuple[ResearchInstrument, ...] = (
    ResearchInstrument("AAPL", "NASDAQ", "USD", "XNAS"),
    ResearchInstrument("AMZN", "NASDAQ", "USD", "XNAS"),
    ResearchInstrument("SNDK", "NASDAQ", "USD", "XNAS"),
    ResearchInstrument("MU", "NASDAQ", "USD", "XNAS"),
    ResearchInstrument("GOOG", "NASDAQ", "USD", "XNAS"),
    ResearchInstrument("NVDA", "NASDAQ", "USD", "XNAS"),
)

_RESEARCH_METADATA_BY_SYMBOL = {
    instrument.symbol: instrument for instrument in INITIAL_RESEARCH_INSTRUMENTS
}

# Loop B operates on explicitly selected symbols under the datastore's ``stocks``
# namespace.  An unregistered ticker is therefore still a usable US-equity
# research instrument; the generic venue remains visible in persisted metadata,
# while XNYS supplies the common regular-session schedule already used by the
# canonical technical pipeline.  Exact venue metadata above wins when available.
_DEFAULT_US_EQUITY_VENUE = "US_EQUITY"
_DEFAULT_US_EQUITY_CURRENCY = "USD"
_DEFAULT_US_EQUITY_CALENDAR = "XNYS"


def read_watchlist(path: Path) -> tuple[str, ...]:
    """Read one symbol per line while ignoring comments and blank lines."""

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"Watchlist does not exist: {path}")
    symbols: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        value = raw_line.split("#", 1)[0].strip().upper()
        if value and value not in symbols:
            symbols.append(value)
    if not symbols:
        raise MLContractError(f"Watchlist contains no symbols: {path}")
    return tuple(symbols)


def initial_universe_membership(
    symbols: Sequence[str],
    *,
    effective_from_by_symbol: Mapping[str, object],
) -> pd.DataFrame:
    """Build one explicit fixed-watchlist membership table for a thin research run.

    The effective interval begins at the first aligned technical observation used by
    the run. This describes a fixed selected-instrument study; it does not claim a
    historically representative investable universe.
    """

    rows: list[dict[str, object]] = []
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().upper()
        instrument = _RESEARCH_METADATA_BY_SYMBOL.get(symbol)
        if instrument is None:
            instrument = ResearchInstrument(
                symbol,
                _DEFAULT_US_EQUITY_VENUE,
                _DEFAULT_US_EQUITY_CURRENCY,
                _DEFAULT_US_EQUITY_CALENDAR,
            )
        if symbol not in effective_from_by_symbol:
            raise MLContractError(f"Missing universe effective_from for {symbol}")
        effective_from = pd.Timestamp(effective_from_by_symbol[symbol])
        if effective_from.tzinfo is None:
            effective_from = effective_from.tz_localize("UTC")
        else:
            effective_from = effective_from.tz_convert("UTC")
        rows.append(
            {
                "id": instrument.symbol,
                "symbol": instrument.symbol,
                "venue": instrument.venue,
                "currency": instrument.currency,
                "exchange_calendar": instrument.exchange_calendar,
                "effective_from": effective_from,
                "effective_to": pd.NaT,
            }
        )
    return pd.DataFrame(rows)
