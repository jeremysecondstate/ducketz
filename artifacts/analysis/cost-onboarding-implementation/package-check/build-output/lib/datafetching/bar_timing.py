from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, TypeVar
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from datafetching.bar_schema import (
    NORMALIZED_BAR_COLUMNS,
    legacy_bar_completion_mask,
    normalized_bar_canonical_path,
    normalized_bar_file_sort_key,
    normalized_bar_schema_is_canonical,
    read_normalized_bar_parquet,
    write_normalized_bar_parquet,
)
from datafetching.layout import safe_token

BAR_TIMING_VERSION = "1.2.0"
MARKET_TIMEZONE = ZoneInfo("America/New_York")
DAILY_BAR_EXCHANGE_CALENDAR = "XNAS"
_TIMEFRAME_PATTERN = re.compile(r"^(\d+)(s|m|h|d|w|mo)$")
_BarT = TypeVar("_BarT")


def annotate_bar_timing(
    frame: pd.DataFrame,
    *,
    timeframe: str,
    as_of: datetime | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """Attach point-in-time completion and US-equity session metadata.

    Bar timestamps are treated as interval-open timestamps. ``bar_complete`` is
    true only after the corresponding interval end has passed. Intraday session
    labels use America/New_York so daylight-saving transitions are handled by
    the standard-library timezone database.
    """
    output = frame.copy()
    if "timestamp" not in output.columns:
        return output

    timestamps = pd.to_datetime(output["timestamp"], utc=True, errors="coerce")
    clean_timeframe = str(timeframe or "").strip().lower()
    end_timestamps = bar_end_timestamps(timestamps, clean_timeframe)
    observed_at = _as_utc_timestamp(as_of)

    output["timestamp"] = timestamps
    output["bar_end_timestamp"] = end_timestamps
    output["bar_complete"] = end_timestamps.notna() & end_timestamps.le(observed_at)
    output["bar_is_current"] = (
        timestamps.notna()
        & timestamps.le(observed_at)
        & end_timestamps.gt(observed_at)
    )
    output["bar_complete_as_of"] = observed_at.isoformat()
    output["bar_duration_seconds"] = _duration_seconds(clean_timeframe)
    output["bar_timing_version"] = BAR_TIMING_VERSION

    session = session_metadata(timestamps, clean_timeframe)
    for column in session.columns:
        output[column] = session[column].to_numpy()
    return output


def completed_market_bars(
    bars: Iterable[_BarT],
    *,
    timeframe: str,
    as_of: datetime | pd.Timestamp | None = None,
) -> list[_BarT]:
    """Return only provider bars whose full interval has elapsed.

    This helper intentionally operates before normalized persistence. Raw provider
    frames may retain the current candle, while normalized OHLCV receives only
    finalized candles.
    """
    materialized = list(bars)
    if not materialized:
        return []
    rows = []
    for bar in materialized:
        row = {"timestamp": _bar_value(bar, "timestamp")}
        for column in ("bar_complete", "bar_is_current"):
            value = _bar_value(bar, column)
            if value is not None:
                row[column] = value
        rows.append(row)
    timestamps = pd.DataFrame(rows)
    legacy_complete = legacy_bar_completion_mask(timestamps)
    annotated = annotate_bar_timing(
        timestamps,
        timeframe=timeframe,
        as_of=as_of,
    )
    complete = (
        annotated["bar_complete"].fillna(False).astype(bool) & legacy_complete
    ).to_numpy(dtype=bool)
    return [bar for bar, keep in zip(materialized, complete, strict=True) if keep]


def finalize_normalized_bar_parquets(
    datastore_root: Path,
    *,
    source: str,
    symbol: str,
    timeframe: str,
    as_of: datetime | pd.Timestamp | None = None,
) -> int:
    """Remove incomplete rows and compact normalized Parquets to canonical OHLCV.

    Completion and session context are derived from the folder timeframe and each
    row's timestamp when consumed; they are intentionally not persisted per row.

    Returns the number of incomplete rows removed across matching files.
    """
    folder = (
        Path(datastore_root)
        / "stocks"
        / safe_token(symbol.strip().upper())
        / "bars"
        / safe_token(timeframe.strip().lower())
        / safe_token(source.strip().lower())
        / "normalized"
    )
    if not folder.is_dir():
        return 0

    grouped_paths: dict[Path, list[Path]] = {}
    for path in folder.glob("*.parquet"):
        grouped_paths.setdefault(normalized_bar_canonical_path(path), []).append(path)

    removed_total = 0
    for canonical_path, group_paths in sorted(
        grouped_paths.items(),
        key=lambda item: str(item[0]),
    ):
        ordered_paths = sorted(group_paths, key=normalized_bar_file_sort_key)
        frames: list[pd.DataFrame] = []
        schemas = []
        for file_order, path in enumerate(ordered_paths):
            frame, physical_schema = read_normalized_bar_parquet(
                path,
                include_legacy_completion=True,
            )
            frame["_file_order"] = file_order
            frames.append(frame)
            schemas.append(physical_schema)

        combined = pd.concat(frames, ignore_index=True, sort=False)
        before_deduplication = len(combined)
        combined = combined.drop_duplicates("timestamp", keep="last")
        legacy_complete = legacy_bar_completion_mask(combined)
        annotated = annotate_bar_timing(
            combined,
            timeframe=timeframe,
            as_of=as_of,
        )
        derived_complete = annotated["bar_complete"].fillna(False).astype(bool)
        complete_mask = legacy_complete & derived_complete
        removed_total += int((~complete_mask).sum())
        output = (
            annotated.loc[complete_mask, list(NORMALIZED_BAR_COLUMNS)]
            .copy()
            .sort_values("timestamp", kind="stable")
            .reset_index(drop=True)
        )
        needs_rewrite = (
            ordered_paths != [canonical_path]
            or any(
                not normalized_bar_schema_is_canonical(schema)
                for schema in schemas
            )
            or len(combined) != before_deduplication
            or not bool(complete_mask.all())
        )
        if not needs_rewrite:
            continue

        temporary = canonical_path.with_suffix(".tmp.parquet")
        write_normalized_bar_parquet(output, temporary)
        temporary.replace(canonical_path)
        for obsolete in ordered_paths:
            if obsolete != canonical_path:
                obsolete.unlink(missing_ok=True)
    return removed_total


def bar_end_timestamps(timestamps: pd.Series, timeframe: str) -> pd.Series:
    """Return interval-close timestamps for canonical Duckets timeframes."""
    parsed = _parse_timeframe(timeframe)
    if parsed is None:
        return pd.Series(pd.NaT, index=timestamps.index, dtype="datetime64[ns, UTC]")
    amount, unit = parsed
    if amount == 1 and unit == "d":
        return _daily_bar_end_timestamps(timestamps)
    if unit == "mo":
        return timestamps + pd.DateOffset(months=amount)
    seconds = {
        "s": amount,
        "m": amount * 60,
        "h": amount * 3_600,
        "d": amount * 86_400,
        "w": amount * 7 * 86_400,
    }[unit]
    return timestamps + pd.to_timedelta(seconds, unit="s")


def _daily_bar_end_timestamps(timestamps: pd.Series) -> pd.Series:
    """Resolve provider daily labels to their official exchange-session close.

    Databento labels a US-equity daily candle at midnight UTC on the represented
    session date.  Treating that label as a literal 24-hour interval delays the
    completed candle until midnight, even though its regular session finished
    hours earlier.  The exchange schedule also owns early closes and holidays.
    """

    result = pd.Series(
        pd.NaT,
        index=timestamps.index,
        dtype="datetime64[ns, UTC]",
    )
    valid = timestamps.notna()
    if not bool(valid.any()):
        return result

    try:
        import exchange_calendars as xcals
    except ImportError as exc:  # pragma: no cover - required project dependency
        raise RuntimeError(
            "exchange-calendars is required to finalize daily equity bars"
        ) from exc

    labels = (
        timestamps.loc[valid]
        .dt.tz_convert("UTC")
        .dt.tz_localize(None)
        .dt.normalize()
    )
    calendar = xcals.get_calendar(
        DAILY_BAR_EXCHANGE_CALENDAR,
        start=labels.min() - pd.Timedelta(days=14),
        end=labels.max() + pd.Timedelta(days=14),
    )
    closes = labels.map(calendar.closes)
    result.loc[valid] = pd.to_datetime(closes, utc=True, errors="coerce")
    return result


def session_metadata(timestamps: pd.Series, timeframe: str) -> pd.DataFrame:
    """Classify bars as premarket, regular, after-hours, or multi-session."""
    parsed = _parse_timeframe(timeframe)
    intraday = parsed is not None and parsed[1] in {"s", "m", "h"}
    result = pd.DataFrame(index=timestamps.index)
    if not intraday:
        result["session_type"] = "MULTI_SESSION"
        result["session_date"] = timestamps.dt.strftime("%Y-%m-%d")
        result["session_minute"] = np.nan
        result["session_progress"] = np.nan
        return result

    local = timestamps.dt.tz_convert(MARKET_TIMEZONE)
    weekday = local.dt.weekday.lt(5)
    minute_of_day = (
        local.dt.hour.astype(float) * 60.0
        + local.dt.minute.astype(float)
        + local.dt.second.astype(float) / 60.0
    )

    premarket = weekday & minute_of_day.ge(240.0) & minute_of_day.lt(570.0)
    regular = weekday & minute_of_day.ge(570.0) & minute_of_day.lt(960.0)
    after_hours = weekday & minute_of_day.ge(960.0) & minute_of_day.lt(1_200.0)
    session_type = np.select(
        [~weekday, premarket, regular, after_hours],
        ["CLOSED", "PREMARKET", "REGULAR", "AFTER_HOURS"],
        default="OVERNIGHT",
    )

    session_minute = pd.Series(np.nan, index=timestamps.index, dtype=float)
    session_progress = pd.Series(np.nan, index=timestamps.index, dtype=float)
    session_minute.loc[premarket] = minute_of_day.loc[premarket] - 240.0
    session_progress.loc[premarket] = session_minute.loc[premarket] / 330.0
    session_minute.loc[regular] = minute_of_day.loc[regular] - 570.0
    session_progress.loc[regular] = session_minute.loc[regular] / 390.0
    session_minute.loc[after_hours] = minute_of_day.loc[after_hours] - 960.0
    session_progress.loc[after_hours] = session_minute.loc[after_hours] / 240.0

    result["session_type"] = session_type
    result["session_date"] = local.dt.strftime("%Y-%m-%d")
    result["session_minute"] = session_minute
    result["session_progress"] = session_progress.clip(0.0, 1.0)
    return result


def _parse_timeframe(timeframe: str) -> tuple[int, str] | None:
    match = _TIMEFRAME_PATTERN.fullmatch(str(timeframe or "").strip().lower())
    if match is None:
        return None
    amount = int(match.group(1))
    return (amount, match.group(2)) if amount > 0 else None


def _duration_seconds(timeframe: str) -> float:
    parsed = _parse_timeframe(timeframe)
    if parsed is None:
        return float("nan")
    amount, unit = parsed
    multiplier = {
        "s": 1,
        "m": 60,
        "h": 3_600,
        "d": 86_400,
        "w": 7 * 86_400,
    }.get(unit)
    return float(amount * multiplier) if multiplier is not None else float("nan")


def _as_utc_timestamp(value: datetime | pd.Timestamp | None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp.now(tz="UTC")
    parsed = pd.Timestamp(value)
    return parsed.tz_localize("UTC") if parsed.tzinfo is None else parsed.tz_convert("UTC")


def _bar_value(bar: object, name: str) -> object:
    if isinstance(bar, Mapping):
        return bar.get(name)
    return getattr(bar, name, None)
