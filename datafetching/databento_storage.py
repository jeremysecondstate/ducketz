"""Human-readable Databento data and history-state paths.

Checksums remain inside manifests and receipts.  Durable paths describe the
provider scope directly so operators never need to decode a hash or timestamp.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Iterable


HISTORY_PROFILE = "prediction-focused-baseline"
MARKET_OPRA = "opra"
MARKET_CME = "cme"
MARKET_US_EQUITIES = "us-equities"


def databento_root(datastore_root: Path) -> Path:
    return Path(datastore_root).resolve() / "market-data" / "databento"


def dataset_root(datastore_root: Path, *, market: str, dataset: str) -> Path:
    return databento_root(datastore_root) / clean_token(market) / clean_token(dataset)


def symbol_scope_name(symbols: Iterable[str]) -> str:
    clean = tuple(
        sorted(
            {
                clean_token(str(value).strip().upper().replace("/", "-"))
                for value in symbols
                if str(value).strip()
            }
        )
    )
    if not clean:
        return "all-symbols"
    return "_and_".join(clean)


def request_directory(
    datastore_root: Path,
    *,
    market: str,
    dataset: str,
    schema: str,
    symbol: str,
    start: date | str,
    end: date | str,
) -> Path:
    return (
        dataset_root(datastore_root, market=market, dataset=dataset)
        / clean_token(schema)
        / symbol_scope_name((symbol,))
        / "windows"
        / window_name(start, end)
    )


def opra_partition_directory(
    datastore_root: Path,
    *,
    dataset: str,
    schema: str,
    day: str,
    symbols: Iterable[str],
    segment: str | None,
) -> Path:
    return (
        dataset_root(datastore_root, market=MARKET_OPRA, dataset=dataset)
        / clean_token(schema)
        / symbol_scope_name(symbols)
        / "dates"
        / clean_date(day)
        / "segments"
        / clean_token(segment or "full-day")
    )


def coordinator_run_directory(datastore_root: Path, *, as_of: date | str) -> Path:
    return (
        Path(datastore_root).resolve()
        / "state"
        / "databento"
        / "history"
        / HISTORY_PROFILE
        / "as-of"
        / clean_date(as_of)
    )


def history_cursor_path(
    datastore_root: Path,
    *,
    market: str,
    dataset: str,
    schema: str,
    symbol: str,
) -> Path:
    return (
        Path(datastore_root).resolve()
        / "state"
        / "databento"
        / "history-cursors"
        / clean_token(market)
        / clean_token(dataset)
        / clean_token(schema)
        / symbol_scope_name((symbol,))
        / "cursor.json"
    )


def clean_date(value: date | str) -> str:
    text = value.isoformat() if isinstance(value, date) else str(value).strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"Invalid Databento storage date: {value!r}") from exc


def window_name(start: date | str, end: date | str) -> str:
    return f"{clean_date(start)}_to_{clean_date(end)}"


def clean_token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value).strip())
    return cleaned.strip("._-") or "unknown"


__all__ = [
    "HISTORY_PROFILE",
    "MARKET_CME",
    "MARKET_OPRA",
    "MARKET_US_EQUITIES",
    "clean_token",
    "coordinator_run_directory",
    "databento_root",
    "dataset_root",
    "history_cursor_path",
    "opra_partition_directory",
    "request_directory",
    "symbol_scope_name",
    "window_name",
]
