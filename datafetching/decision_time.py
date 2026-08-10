from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from datafetching.bar_schema import (
    legacy_bar_completion_mask,
    normalized_bar_file_sort_key,
    read_bar_timestamp_and_completion,
)
from datafetching.bar_timing import bar_end_timestamps
from datafetching.layout import safe_token

DECISION_SOURCE_PROVIDER = "databento"
DECISION_SOURCE_TIMEFRAME = "1m"
DECISION_BOUNDARY_MINUTES = 15


@dataclass(frozen=True)
class DecisionClock:
    """Completed market bar used as the point-in-time key for a fetched snapshot."""

    decision_timestamp: pd.Timestamp
    bar_timestamp: pd.Timestamp
    provider: str
    timeframe: str
    source_file: Path


def expected_quarter_hour_target(
    value: datetime | pd.Timestamp | None = None,
) -> pd.Timestamp:
    """Return the exact quarter-hour target owned by one scheduled cycle."""

    observed = _as_utc_timestamp(value)
    return observed.floor(f"{DECISION_BOUNDARY_MINUTES}min")


def latest_completed_bar_clock(
    datastore_root: Path,
    *,
    symbol: str,
    as_of: datetime | pd.Timestamp | None = None,
) -> DecisionClock:
    """Return the newest qualifying Databento 1m bar available by ``as_of``.

    Duckets fetches option surfaces on a 15-minute cadence. The source of truth is the
    normalized Databento 1m Parquet, so ``decision_timestamp`` is the newest completed
    1m ``bar_end_timestamp`` that lands exactly on a wall-clock quarter-hour boundary
    (:00, :15, :30, or :45). Derived higher-timeframe Parquets are not consulted.
    """

    clean_symbol = symbol.strip().upper()
    if not clean_symbol:
        raise ValueError("Symbol is required.")

    observed_at = _as_utc_timestamp(as_of)
    normalized_root = (
        Path(datastore_root)
        / "stocks"
        / safe_token(clean_symbol)
        / "bars"
        / DECISION_SOURCE_TIMEFRAME
        / DECISION_SOURCE_PROVIDER
        / "normalized"
    )
    if not normalized_root.is_dir():
        raise FileNotFoundError(
            f"No normalized Databento 1m OHLCV folder exists for {clean_symbol}: "
            f"{normalized_root}"
        )

    paths = sorted(
        normalized_root.glob("*.parquet"),
        key=normalized_bar_file_sort_key,
    )
    if not paths:
        raise FileNotFoundError(
            f"No normalized Databento 1m OHLCV Parquet exists for {clean_symbol}: "
            f"{normalized_root}"
        )

    candidate = _latest_from_files(paths, observed_at=observed_at)
    if candidate is None:
        raise FileNotFoundError(
            f"No completed Databento 1m bar ending on a "
            f"{DECISION_BOUNDARY_MINUTES}-minute boundary was available for "
            f"{clean_symbol} by {observed_at.isoformat()}."
        )

    return candidate


def completed_bar_clock_for_target(
    datastore_root: Path,
    *,
    symbol: str,
    target_snapshot_for: datetime | pd.Timestamp,
    as_of: datetime | pd.Timestamp | None = None,
) -> DecisionClock:
    """Resolve exactly ``target_snapshot_for``; never substitute an older bar."""

    clean_symbol = symbol.strip().upper()
    if not clean_symbol:
        raise ValueError("Symbol is required.")
    target = _as_utc_timestamp(target_snapshot_for)
    if target != expected_quarter_hour_target(target):
        raise ValueError("Decision target must be an exact quarter-hour boundary")
    observed_at = _as_utc_timestamp(as_of)
    if target > observed_at:
        raise FileNotFoundError(
            f"Target {target.isoformat()} is not complete by {observed_at.isoformat()}."
        )
    normalized_root = (
        Path(datastore_root)
        / "stocks"
        / safe_token(clean_symbol)
        / "bars"
        / DECISION_SOURCE_TIMEFRAME
        / DECISION_SOURCE_PROVIDER
        / "normalized"
    )
    paths = (
        sorted(normalized_root.glob("*.parquet"), key=normalized_bar_file_sort_key)
        if normalized_root.is_dir()
        else []
    )
    candidate = _clock_for_exact_target(
        paths,
        observed_at=observed_at,
        target=target,
    )
    if candidate is None:
        raise FileNotFoundError(
            f"Exact completed Databento 1m target {target.isoformat()} was not "
            f"available for {clean_symbol} by {observed_at.isoformat()}."
        )
    return candidate


def completed_bar_close(clock: DecisionClock) -> float:
    """Read the one canonical close selected by a verified decision clock."""

    frame = pd.read_parquet(clock.source_file, columns=["timestamp", "close"])
    timestamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    selected = pd.to_numeric(
        frame.loc[timestamps.eq(pd.Timestamp(clock.bar_timestamp)), "close"],
        errors="coerce",
    ).dropna()
    if len(selected) != 1:
        raise ValueError("Canonical target boundary did not resolve exactly one close")
    value = float(selected.iloc[0])
    if not pd.notna(value) or value <= 0.0:
        raise ValueError("Canonical target close must be finite and positive")
    return value


def _latest_from_files(
    paths: list[Path],
    *,
    observed_at: pd.Timestamp,
) -> DecisionClock | None:
    frames: list[pd.DataFrame] = []
    for file_order, path in enumerate(paths):
        try:
            frame, _physical_schema = read_bar_timestamp_and_completion(path)
        except Exception as exc:
            raise RuntimeError(
                f"Could not read normalized bar parquet {path}: {exc}"
            ) from exc
        if frame.empty:
            continue
        frame["_source_file"] = str(path)
        frame["_file_order"] = file_order
        frames.append(frame)
    if not frames:
        return None

    frame = (
        pd.concat(frames, ignore_index=True, sort=False)
        .sort_values(["timestamp", "_file_order"], kind="stable")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )
    timestamps = frame["timestamp"]
    ends = bar_end_timestamps(timestamps, DECISION_SOURCE_TIMEFRAME)

    quarter_hour_boundary = (
        ends.notna()
        & ends.dt.second.eq(0)
        & ends.dt.microsecond.eq(0)
        & ends.dt.minute.mod(DECISION_BOUNDARY_MINUTES).eq(0)
    )
    complete = (
        ends.notna()
        & ends.le(observed_at)
        & quarter_hour_boundary
        & legacy_bar_completion_mask(frame)
    )
    valid = complete & timestamps.notna()
    if not valid.any():
        return None

    latest_index = ends.loc[valid].idxmax()
    return DecisionClock(
        decision_timestamp=pd.Timestamp(ends.loc[latest_index]).tz_convert("UTC"),
        bar_timestamp=pd.Timestamp(timestamps.loc[latest_index]).tz_convert("UTC"),
        provider=DECISION_SOURCE_PROVIDER,
        timeframe=DECISION_SOURCE_TIMEFRAME,
        source_file=Path(str(frame.loc[latest_index, "_source_file"])),
    )


def _clock_for_exact_target(
    paths: list[Path],
    *,
    observed_at: pd.Timestamp,
    target: pd.Timestamp,
) -> DecisionClock | None:
    if not paths:
        return None
    frames: list[pd.DataFrame] = []
    for file_order, path in enumerate(paths):
        try:
            frame, _physical_schema = read_bar_timestamp_and_completion(path)
        except Exception as exc:
            raise RuntimeError(
                f"Could not read normalized bar parquet {path}: {exc}"
            ) from exc
        if frame.empty:
            continue
        frame["_source_file"] = str(path)
        frame["_file_order"] = file_order
        frames.append(frame)
    if not frames:
        return None
    frame = (
        pd.concat(frames, ignore_index=True, sort=False)
        .sort_values(["timestamp", "_file_order"], kind="stable")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )
    ends = bar_end_timestamps(frame["timestamp"], DECISION_SOURCE_TIMEFRAME)
    valid = (
        ends.eq(target)
        & ends.le(observed_at)
        & legacy_bar_completion_mask(frame)
        & frame["timestamp"].notna()
    )
    if valid.sum() != 1:
        return None
    index = valid.loc[valid].index[0]
    return DecisionClock(
        decision_timestamp=target,
        bar_timestamp=pd.Timestamp(frame.loc[index, "timestamp"]).tz_convert("UTC"),
        provider=DECISION_SOURCE_PROVIDER,
        timeframe=DECISION_SOURCE_TIMEFRAME,
        source_file=Path(str(frame.loc[index, "_source_file"])),
    )


def _as_utc_timestamp(value: datetime | pd.Timestamp | None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp.now(tz="UTC")
    parsed = pd.Timestamp(value)
    return parsed.tz_localize("UTC") if parsed.tzinfo is None else parsed.tz_convert("UTC")
