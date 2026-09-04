from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from app.models.market_data import MarketBar
from datafetching.bar_timing import annotate_bar_timing

DERIVED_INTRADAY_FREQUENCIES = ("5m", "10m", "15m", "30m", "1h")
DAILY_DERIVATION_CALENDAR = "XNAS"
INTRADAY_DERIVATION_CALENDAR = "XNAS"
DAILY_SESSION_BOUNDARY_TOLERANCE = pd.Timedelta(minutes=15)
STANDARD_EXTENDED_SESSION_OPEN = pd.Timedelta(hours=4)
STANDARD_EXTENDED_SESSION_CLOSE = pd.Timedelta(hours=20)
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


@dataclass(frozen=True)
class CompletedEquitySession:
    session_label: pd.Timestamp
    open_timestamp: pd.Timestamp
    close_timestamp: pd.Timestamp


@dataclass(frozen=True)
class DerivedDailyMarketBar:
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
    bar_end_timestamp: datetime
    session_date: str
    source_bar_count: int


def derive_intraday_bars(
    symbol: str,
    source_bars: list[MarketBar],
    output_frequency: str,
    *,
    as_of: datetime | pd.Timestamp | None = None,
    coverage_start: datetime | pd.Timestamp | None = None,
    coverage_end: datetime | pd.Timestamp | None = None,
) -> list[DerivedMarketBar]:
    """Aggregate completed 1-minute bars under versioned session rules.

    The current 1-minute candle is excluded before aggregation, and a derived
    candle is emitted only after its own 5/10/15/30/60-minute interval has
    closed and every expected one-minute constituent is present. This makes
    polling at arbitrary offsets safe and prevents an overlapping continuation
    tail from replacing a complete derived candle with a partial one. The 1h
    lane may instead use an explicit successful provider-request range. Within
    that proven range, provider-omitted no-trade minutes are valid sparse
    evidence; a wholly empty eligible hour carries only a strictly prior close.
    Those coverage-proven hours follow the continuous v6 04:00--20:00 Eastern
    source envelope, including the 09:00--10:00 open-boundary clock hour.
    """
    frequency = output_frequency.strip().lower()
    try:
        rule = _RESAMPLE_RULES[frequency]
    except KeyError as exc:
        choices = ", ".join(DERIVED_INTRADAY_FREQUENCIES)
        raise ValueError(f"Unsupported derived intraday frequency {frequency!r}; use {choices}.") from exc
    coverage_supplied = coverage_start is not None or coverage_end is not None
    if coverage_supplied and frequency != "1h":
        raise ValueError("Provider-range sparse derivation is supported only for 1h")
    if (coverage_start is None) != (coverage_end is None):
        raise ValueError("Provider-range sparse derivation requires start and end")
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

    if coverage_supplied:
        assert coverage_start is not None and coverage_end is not None
        return _derive_covered_hour_bars(
            symbol,
            frame,
            observed_at=observed_at,
            coverage_start=_as_utc_timestamp(coverage_start),
            coverage_end=_as_utc_timestamp(coverage_end),
        )

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


def _derive_covered_hour_bars(
    symbol: str,
    source: pd.DataFrame,
    *,
    observed_at: pd.Timestamp,
    coverage_start: pd.Timestamp,
    coverage_end: pd.Timestamp,
) -> list[DerivedMarketBar]:
    """Build only clock hours proven covered by one successful 1m request."""

    if coverage_end <= coverage_start:
        raise ValueError("Provider coverage end must be after coverage start")
    proven_end = min(coverage_end, observed_at)
    if proven_end <= coverage_start:
        return []

    intervals = _covered_equity_hour_intervals(
        coverage_start=coverage_start,
        coverage_end=proven_end,
    )
    if not intervals:
        return []

    ordered = source.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    ordered_timestamps = pd.DatetimeIndex(ordered["timestamp"])
    by_hour = {
        pd.Timestamp(hour): group
        for hour, group in ordered.groupby(
            ordered["timestamp"].dt.floor("h"),
            sort=False,
        )
    }
    rows: list[dict[str, object]] = []
    for start, _end in intervals:
        constituent = by_hour.get(start)
        if constituent is not None and not constituent.empty:
            volume = pd.to_numeric(
                constituent["volume"], errors="coerce"
            ).fillna(0.0).sum()
            rows.append(
                {
                    "timestamp": start,
                    "open": float(constituent.iloc[0]["open"]),
                    "high": float(constituent["high"].max()),
                    "low": float(constituent["low"].min()),
                    "close": float(constituent.iloc[-1]["close"]),
                    "volume": float(volume),
                    "source_bar_count": len(constituent),
                }
            )
            continue

        prior_location = ordered_timestamps.searchsorted(start, side="left") - 1
        if prior_location < 0:
            continue
        prior_close = float(ordered.iloc[prior_location]["close"])
        rows.append(
            {
                "timestamp": start,
                "open": prior_close,
                "high": prior_close,
                "low": prior_close,
                "close": prior_close,
                "volume": 0.0,
                "source_bar_count": 0,
            }
        )
    if not rows:
        return []

    derived = annotate_bar_timing(
        pd.DataFrame(rows),
        timeframe="1h",
        as_of=observed_at,
    )
    clean_symbol = symbol.strip().upper()
    return [
        DerivedMarketBar(
            symbol=clean_symbol,
            source="databento",
            timeframe="1h",
            timestamp=row.timestamp.to_pydatetime(),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            bar_complete=bool(row.bar_complete),
            session_type=str(row.session_type),
            session_date=str(row.session_date),
            source_bar_count=int(row.source_bar_count),
        )
        for row in derived.itertuples(index=False)
    ]


def _covered_equity_hour_intervals(
    *,
    coverage_start: pd.Timestamp,
    coverage_end: pd.Timestamp,
) -> tuple[tuple[pd.Timestamp, pd.Timestamp], ...]:
    """Return v6 continuous-source hours wholly inside proven coverage."""

    local_start = coverage_start.tz_convert("America/New_York")
    local_end = coverage_end.tz_convert("America/New_York")
    calendar = _exchange_calendar(
        INTRADAY_DERIVATION_CALENDAR,
        start=local_start.normalize().tz_localize(None) - pd.Timedelta(days=7),
        end=local_end.normalize().tz_localize(None) + pd.Timedelta(days=7),
    )
    records: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    for session, schedule in calendar.schedule.iterrows():
        session_open = pd.Timestamp(schedule["open"])
        session_close = pd.Timestamp(schedule["close"])
        local_open = session_open.tz_convert(calendar.tz)
        local_close = session_close.tz_convert(calendar.tz)
        standard_session = (
            local_open.hour,
            local_open.minute,
            local_close.hour,
            local_close.minute,
        ) == (9, 30, 16, 0)
        if standard_session:
            session_label = pd.Timestamp(session)
            local_midnight = (
                session_label.tz_localize(calendar.tz)
                if session_label.tzinfo is None
                else session_label.tz_convert(calendar.tz).normalize()
            )
            interval_start = local_midnight + STANDARD_EXTENDED_SESSION_OPEN
            interval_limit = local_midnight + STANDARD_EXTENDED_SESSION_CLOSE
        else:
            interval_start = local_open.ceil("h")
            interval_limit = local_close

        while interval_start + pd.Timedelta(hours=1) <= interval_limit:
            start = interval_start.tz_convert("UTC")
            end = start + pd.Timedelta(hours=1)
            if start >= coverage_start and end <= coverage_end:
                records.append((start, end))
            interval_start += pd.Timedelta(hours=1)
    return tuple(sorted(records))


def latest_completed_equity_session(
    as_of: datetime | pd.Timestamp | None = None,
    *,
    exchange_calendar: str = DAILY_DERIVATION_CALENDAR,
) -> CompletedEquitySession | None:
    """Return the latest regular session whose official close has passed."""

    observed_at = _as_utc_timestamp(as_of)
    calendar = _exchange_calendar(
        exchange_calendar,
        start=observed_at.normalize() - pd.Timedelta(days=14),
        end=observed_at.normalize() + pd.Timedelta(days=7),
    )
    closes = pd.to_datetime(calendar.schedule["close"], utc=True)
    completed = calendar.schedule.loc[closes.le(observed_at)]
    if completed.empty:
        return None
    session = pd.Timestamp(completed.index[-1])
    return CompletedEquitySession(
        session_label=_utc_session_label(session),
        open_timestamp=pd.Timestamp(completed.iloc[-1]["open"]),
        close_timestamp=pd.Timestamp(completed.iloc[-1]["close"]),
    )


def derive_daily_bars(
    symbol: str,
    source_bars: list[MarketBar],
    *,
    as_of: datetime | pd.Timestamp | None = None,
    exchange_calendar: str = DAILY_DERIVATION_CALENDAR,
) -> list[DerivedDailyMarketBar]:
    """Aggregate complete regular sessions from trade-bearing one-minute bars.

    Databento intentionally omits an OHLCV interval when no trade occurs.  Such
    gaps do not alter a session's OHLC or summed volume, so they remain absent
    here.  A session is emitted only after its official close and when observed
    trade bars reach both session boundaries within a conservative tolerance.
    Extended-hours observations, stale continuation tails, holidays, and
    early-close minutes cannot leak into the daily candle.
    """

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

    first_label = frame["timestamp"].min().normalize().tz_localize(None)
    final_label = max(
        frame["timestamp"].max().normalize().tz_localize(None),
        observed_at.normalize().tz_localize(None),
    )
    calendar = _exchange_calendar(
        exchange_calendar,
        start=first_label - pd.Timedelta(days=7),
        end=final_label + pd.Timedelta(days=7),
    )
    indexed = frame.set_index("timestamp").sort_index()
    clean_symbol = symbol.strip().upper()
    source = str(source_bars[0].source or "databento")
    records: list[DerivedDailyMarketBar] = []
    for session, schedule in calendar.schedule.iterrows():
        session_open = pd.Timestamp(schedule["open"])
        session_close = pd.Timestamp(schedule["close"])
        if session_close > observed_at:
            continue
        constituent = indexed.loc[
            (indexed.index >= session_open) & (indexed.index < session_close)
        ]
        if constituent.empty:
            continue
        first_observed = pd.Timestamp(constituent.index[0])
        last_observed_end = pd.Timestamp(constituent.index[-1]) + pd.Timedelta(
            minutes=1
        )
        if (
            first_observed > session_open + DAILY_SESSION_BOUNDARY_TOLERANCE
            or last_observed_end
            < session_close - DAILY_SESSION_BOUNDARY_TOLERANCE
        ):
            continue
        records.append(
            DerivedDailyMarketBar(
                symbol=clean_symbol,
                source=source,
                timeframe="1d",
                timestamp=_utc_session_label(session).to_pydatetime(),
                open=float(constituent.iloc[0]["open"]),
                high=float(constituent["high"].max()),
                low=float(constituent["low"].min()),
                close=float(constituent.iloc[-1]["close"]),
                volume=float(
                    pd.to_numeric(
                        constituent["volume"], errors="coerce"
                    ).fillna(0.0).sum()
                ),
                bar_complete=True,
                bar_end_timestamp=session_close.to_pydatetime(),
                session_date=pd.Timestamp(session).date().isoformat(),
                source_bar_count=len(constituent),
            )
        )
    return records


def _as_utc_timestamp(value: datetime | pd.Timestamp | None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp.now(tz="UTC")
    parsed = pd.Timestamp(value)
    return parsed.tz_localize("UTC") if parsed.tzinfo is None else parsed.tz_convert("UTC")


def _exchange_calendar(name: str, *, start: object, end: object):
    try:
        import exchange_calendars as xcals
    except ImportError as exc:  # pragma: no cover - required project dependency
        raise RuntimeError(
            "exchange-calendars is required to derive daily equity bars"
        ) from exc
    return xcals.get_calendar(
        name,
        start=_calendar_date(start),
        end=_calendar_date(end),
    )


def _calendar_date(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp.normalize()


def _utc_session_label(value: object) -> pd.Timestamp:
    label = pd.Timestamp(value).normalize()
    return label.tz_localize("UTC") if label.tzinfo is None else label.tz_convert("UTC")


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
