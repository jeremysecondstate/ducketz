from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from datafetching.layout import safe_token, stock_data_folder


def normalized_bar_path(
    datastore_root: Path,
    *,
    source: str,
    symbol: str,
    timeframe: str,
    request_key: str,
) -> Path:
    """Return the canonical request-specific normalized-bar Parquet path."""

    clean_symbol = safe_token(symbol.strip().upper().replace("/", "-"))
    folder = stock_data_folder(
        Path(datastore_root),
        symbol=clean_symbol,
        category="bars",
        source=source,
        scope="normalized",
        timeframe=timeframe,
    )
    return folder / f"{clean_symbol}_{safe_token(request_key)}.parquet"


def latest_normalized_bar_timestamp(
    datastore_root: Path,
    *,
    source: str,
    symbol: str,
    timeframe: str,
    request_key: str,
) -> datetime | None:
    """Read the latest timestamp already persisted for one provider request.

    Stable and legacy timestamp-suffixed files are considered together so the next
    fetch can continue safely before the store compacts any older layout.
    """

    path = normalized_bar_path(
        datastore_root,
        source=source,
        symbol=symbol,
        timeframe=timeframe,
        request_key=request_key,
    )
    legacy = tuple(
        sorted(candidate for candidate in path.parent.glob(f"{path.stem}_*.parquet"))
    )
    paths = ([path] if path.is_file() else []) + list(legacy)
    if not paths:
        return None

    latest: pd.Timestamp | None = None
    for existing_path in paths:
        try:
            frame = pd.read_parquet(existing_path, columns=["timestamp"])
        except Exception as exc:
            raise RuntimeError(
                f"Could not read existing normalized bar dataset {existing_path}: {exc}"
            ) from exc
        if "timestamp" not in frame.columns:
            raise ValueError(
                f"Existing normalized bar dataset is missing timestamp: {existing_path}"
            )
        parsed = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce").dropna()
        if parsed.empty:
            continue
        candidate = parsed.max()
        latest = candidate if latest is None or candidate > latest else latest

    return None if latest is None else latest.to_pydatetime()
