from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from app.models.market_data import MarketBar
from datafetching.bar_timing import annotate_bar_timing

DERIVED_INTRADAY_FREQUENCIES = ("5m", "10m", "15m", "30m", "1h")
_RESAMPLE_RULES = {
    "5m": "5min",
    "10m": "10min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
}


@dataclass(frozen=True)
class DerivedMarketBar:
    symbol: str
    source: str
    timeframe: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    bar_complete: bool
    session_type: str
    session_date: str
    source_bar_count: int

    @property
    def bar_end_timestamp(self) -> datetime:
        """Return the interval end without serializing it into the upsert row.

        Normalized-bar finalization persists the canonical Arrow timestamp after
        the upsert. Keeping this as a property preserves the convenient runtime
        API while preventing an ISO-string value from colliding with the stored
        timestamp column on the next fetch cycle.
        """
        minutes = _frequency_minutes(self.timeframe)
        return self.timestamp + timedelta(minutes=minutes)


def derive_intraday_bars(
    symbol: str,
    source_bars: list[MarketBar],
    output_frequency: str,
    *,
    as_of: datetime | pd.Timestamp | None = None,
) -> list[DerivedMarketBar]:
    """Aggregate completed 1-minute bars without crossing market sessions.

    The current 1-minute candle is excluded before aggregation, and a derived
    candle is emitted only after its own 5/10/15/30/60-minute interval has
    closed and every expected one-minute constituent is present. This makes
    polling at arbitrary offsets safe and prevents an overlapping continuation
    tail from replacing a complete derived candle with a partial one.
    """
    frequency = output_frequency.strip().lower()
    try:
        rule = _RESAMPLE_RULES[frequency]
    except KeyError as exc:
        choices = ", ".join(DERIVED_INTRADAY_FREQUENCIES)
        raise ValueError(f"Unsupported derived intraday frequency {frequency!r}; use {choices}.") from exc
    if not source_bars:
        return []

    observed_at = _as_utc_timestamp(as_of)
    frame = pd.DataFrame(
        [
            {
                "timestamp": bar.timestamp,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in source_bars
        ]
    )
    frame = annotate_bar_timing(frame, timeframe="1m", as_of=observed_at)
    frame = (
        frame.loc[frame["bar_complete"]]
        .dropna(subset=["timestamp", "open", "high", "low", "close"])
        .sort_values("timestamp")
        .drop_duplicates("timestamp", keep="last")
    )
    if frame.empty:
        return []

    pieces: list[pd.DataFrame] = []
    grouping = ["session_date", "session_type"]
    for _, session_frame in frame.groupby(grouping, sort=True, dropna=False):
        indexed = session_frame.set_index("timestamp").sort_index()
        aggregated = (
            indexed.resample(rule, origin="start_day", label="left", closed="left")
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
                source_bar_count=("close", "count"),
            )
            .dropna(subset=["open", "high", "low", "close"])
            .reset_index()
        )
        if not aggregated.empty:
            pieces.append(aggregated)
    if not pieces:
        return []

    derived = pd.concat(pieces, ignore_index=True, sort=False).sort_values("timestamp")
    derived = annotate_bar_timing(derived, timeframe=frequency, as_of=observed_at)
    expected_source_bar_count = _frequency_minutes(frequency)
    derived = derived.loc[
        derived["bar_complete"]
        & derived["source_bar_count"].eq(expected_source_bar_count)
    ].reset_index(drop=True)

    clean_symbol = symbol.strip().upper()
    return [
        DerivedMarketBar(
            symbol=clean_symbol,
            source="databento",
            timeframe=frequency,
            timestamp=row.timestamp.to_pydatetime(),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume or 0.0),
            bar_complete=bool(row.bar_complete),
            session_type=str(row.session_type),
            session_date=str(row.session_date),
            source_bar_count=int(row.source_bar_count),
        )
        for row in derived.itertuples(index=False)
    ]


def _as_utc_timestamp(value: datetime | pd.Timestamp | None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp.now(tz="UTC")
    parsed = pd.Timestamp(value)
    return parsed.tz_localize("UTC") if parsed.tzinfo is None else parsed.tz_convert("UTC")


def _frequency_minutes(value: str) -> int:
    frequency = str(value).strip().lower()
    if frequency == "1h":
        return 60
    if frequency.endswith("m") and frequency[:-1].isdigit():
        minutes = int(frequency[:-1])
        if minutes > 0:
            return minutes
    choices = ", ".join(DERIVED_INTRADAY_FREQUENCIES)
    raise ValueError(f"Unsupported derived intraday frequency {value!r}; use {choices}.")
