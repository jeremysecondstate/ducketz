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
    ResearchInstrument("DE", "NYSE", "USD", "XNYS"),
    ResearchInstrument("CAT", "NYSE", "USD", "XNYS"),
    ResearchInstrument("SNDK", "NASDAQ", "USD", "XNAS"),
    ResearchInstrument("PLAB", "NASDAQ", "USD", "XNAS"),
    ResearchInstrument("MU", "NASDAQ", "USD", "XNAS"),
    ResearchInstrument("GOOG", "NASDAQ", "USD", "XNAS"),
    ResearchInstrument("NVDA", "NASDAQ", "USD", "XNAS"),
)

_RESEARCH_METADATA_BY_SYMBOL = {
    instrument.symbol: instrument for instrument in INITIAL_RESEARCH_INSTRUMENTS
}


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
        try:
            instrument = _RESEARCH_METADATA_BY_SYMBOL[symbol]
        except KeyError as exc:
            supported = ", ".join(sorted(_RESEARCH_METADATA_BY_SYMBOL))
            raise MLContractError(
                f"No research metadata is registered for {symbol!r}; "
                f"supported symbols: {supported}."
            ) from exc
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
