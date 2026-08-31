from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from ml.contracts import MLContractError


# This is the shared six-underlying Loops universe. Stock and option consumers
# use distinct execution contracts even though they share these symbols.
PRODUCTION_LOOPS_SYMBOLS: tuple[str, ...] = (
    "AAPL",
    "AMZN",
    "GOOG",
    "MU",
    "NVDA",
    "SNDK",
)
# Compatibility name for the existing options stack.
PRODUCTION_OPTION_SYMBOLS: tuple[str, ...] = PRODUCTION_LOOPS_SYMBOLS
OPTION_CALL_PUTS: tuple[str, ...] = ("CALL", "PUT")
PRODUCTION_OPTION_ROUTES: tuple[tuple[str, str], ...] = tuple(
    (symbol, call_put)
    for symbol in PRODUCTION_OPTION_SYMBOLS
    for call_put in OPTION_CALL_PUTS
)
PRODUCTION_OPTION_ROUTE_COUNT = len(PRODUCTION_OPTION_ROUTES)

# SPY is intentionally a methodology/approximation benchmark only.  Code that
# needs a production symbol must use PRODUCTION_OPTION_SYMBOLS, never the union.
RESEARCH_OPTION_BENCHMARK_SYMBOLS: tuple[str, ...] = ("SPY",)


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


def canonical_production_option_symbols(
    symbols: Sequence[str],
    *,
    label: str = "option universe",
) -> tuple[str, ...]:
    """Validate an exact production scope and return canonical symbol order."""

    observed = tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in symbols
            if str(value).strip()
        )
    )
    expected = PRODUCTION_OPTION_SYMBOLS
    if len(observed) != len(expected) or set(observed) != set(expected):
        missing = sorted(set(expected).difference(observed))
        extra = sorted(set(observed).difference(expected))
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("extra=" + ",".join(extra))
        raise MLContractError(
            f"{label} must contain exactly {', '.join(expected)}"
            + (f" ({'; '.join(detail)})" if detail else "")
        )
    return expected


def production_option_routes(
    symbols: Sequence[str] = PRODUCTION_OPTION_SYMBOLS,
) -> tuple[tuple[str, str], ...]:
    """Return CALL/PUT routes after enforcing the production universe."""

    canonical = canonical_production_option_symbols(symbols)
    return tuple(
        (symbol, call_put)
        for symbol in canonical
        for call_put in OPTION_CALL_PUTS
    )


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


__all__ = [
    "INITIAL_RESEARCH_INSTRUMENTS",
    "OPTION_CALL_PUTS",
    "PRODUCTION_OPTION_ROUTE_COUNT",
    "PRODUCTION_OPTION_ROUTES",
    "PRODUCTION_OPTION_SYMBOLS",
    "PRODUCTION_LOOPS_SYMBOLS",
    "RESEARCH_OPTION_BENCHMARK_SYMBOLS",
    "ResearchInstrument",
    "canonical_production_option_symbols",
    "initial_universe_membership",
    "production_option_routes",
    "read_watchlist",
]
