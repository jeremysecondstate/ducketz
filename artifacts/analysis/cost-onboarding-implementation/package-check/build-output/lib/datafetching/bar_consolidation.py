from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from datafetching.bar_schema import (
    read_normalized_bar_parquet,
    write_normalized_bar_parquet,
)
from datafetching.continuation import normalized_bar_path


NATIVE_DATABENTO_REQUESTS = {
    "1h": "source_1825d_1h_ohlcv-1h_1h",
    "1d": "source_2555d_1d_ohlcv-1d_1d",
}
DERIVED_DATABENTO_REQUESTS = {
    "1h": "derived_1m_1h",
    "1d": "derived_1m_1d",
}


@dataclass(frozen=True)
class DerivedBarConsolidation:
    symbol: str
    timeframe: str
    native_path: Path
    derived_path: Path
    native_rows: int
    derived_rows_before: int
    shadowed_rows_removed: int
    derived_rows_retained: int
    bytes_before: int
    bytes_after: int


def consolidate_shadowed_derived_bars(
    datastore_root: Path,
    *,
    symbol: str,
    timeframes: Iterable[str] = ("1h", "1d"),
) -> tuple[DerivedBarConsolidation, ...]:
    """Keep only derived Databento bars that native bars do not yet provide.

    The 1-minute-derived hourly and daily files are latency bridges. Once the
    same timestamp is present in the native EQUS.MINI file, readers already
    prefer the native row and retaining the derived copy adds no information.
    Derived-only gap rows remain in place so the bridge keeps doing its job.
    """

    clean_symbol = str(symbol).strip().upper()
    if not clean_symbol:
        raise ValueError("Symbol is required")

    results: list[DerivedBarConsolidation] = []
    for raw_timeframe in tuple(dict.fromkeys(timeframes)):
        timeframe = str(raw_timeframe).strip().lower()
        if timeframe not in NATIVE_DATABENTO_REQUESTS:
            choices = ", ".join(sorted(NATIVE_DATABENTO_REQUESTS))
            raise ValueError(
                f"Unsupported derived-bar consolidation timeframe {timeframe!r}; "
                f"use one of: {choices}"
            )
        native_path = normalized_bar_path(
            datastore_root,
            source="databento",
            symbol=clean_symbol,
            timeframe=timeframe,
            request_key=NATIVE_DATABENTO_REQUESTS[timeframe],
        )
        derived_path = normalized_bar_path(
            datastore_root,
            source="databento",
            symbol=clean_symbol,
            timeframe=timeframe,
            request_key=DERIVED_DATABENTO_REQUESTS[timeframe],
        )
        if not native_path.is_file() or not derived_path.is_file():
            continue

        native, _native_schema = read_normalized_bar_parquet(native_path)
        derived, _derived_schema = read_normalized_bar_parquet(derived_path)
        _validate_timestamp_identity(native, path=native_path)
        _validate_timestamp_identity(derived, path=derived_path)

        native_timestamps = pd.Index(native["timestamp"])
        shadowed = derived["timestamp"].isin(native_timestamps)
        removed = int(shadowed.sum())
        if removed == 0:
            continue

        retained = derived.loc[~shadowed].sort_values("timestamp").reset_index(drop=True)
        bytes_before = derived_path.stat().st_size
        if retained.empty:
            derived_path.unlink()
            bytes_after = 0
        else:
            temporary = derived_path.with_suffix(
                derived_path.suffix + f".consolidating-{os.getpid()}"
            )
            try:
                write_normalized_bar_parquet(retained, temporary)
                temporary.replace(derived_path)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise
            bytes_after = derived_path.stat().st_size

        results.append(
            DerivedBarConsolidation(
                symbol=clean_symbol,
                timeframe=timeframe,
                native_path=native_path,
                derived_path=derived_path,
                native_rows=len(native),
                derived_rows_before=len(derived),
                shadowed_rows_removed=removed,
                derived_rows_retained=len(retained),
                bytes_before=bytes_before,
                bytes_after=bytes_after,
            )
        )
    return tuple(results)


def _validate_timestamp_identity(frame: pd.DataFrame, *, path: Path) -> None:
    if frame.empty:
        return
    if frame["timestamp"].isna().any():
        raise ValueError(f"Bar consolidation found null timestamps in {path}")
    if frame["timestamp"].duplicated(keep=False).any():
        raise ValueError(f"Bar consolidation found duplicate timestamps in {path}")


__all__ = [
    "DERIVED_DATABENTO_REQUESTS",
    "NATIVE_DATABENTO_REQUESTS",
    "DerivedBarConsolidation",
    "consolidate_shadowed_derived_bars",
]
