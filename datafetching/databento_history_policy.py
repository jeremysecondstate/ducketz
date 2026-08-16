"""Shared Databento historical bootstrap limits.

Interval suffixes provide the defaults, with explicit schema overrides for
prediction value and storage density (for example OHLCV versus CBBO).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Mapping


INTERVAL_LOOKBACK_DAYS: Mapping[str, int] = {
    "1s": 3,
    "1m": 100,
    "1h": 1_825,
    "1d": 2_555,
}

SCHEMA_LOOKBACK_DAYS: Mapping[str, int] = {
    "ohlcv-1s": 10,
    "cbbo-1s": 1,
    "cbbo-1m": 20,
}

DEFINITION_LOOKBACK_DAYS = 100

HEAVY_BOOK_LOOKBACK_DAYS: Mapping[str, int] = {
    "cmbp-1": 1,
    "mbp-10": 1,
    "mbo": 1,
}


def interval_lookback_days(schema: str) -> int | None:
    """Return the configured day cap for a timestamp-interval schema."""

    clean_schema = str(schema).strip().lower()
    if clean_schema in SCHEMA_LOOKBACK_DAYS:
        return SCHEMA_LOOKBACK_DAYS[clean_schema]
    interval = clean_schema.rsplit("-", maxsplit=1)[-1]
    return INTERVAL_LOOKBACK_DAYS.get(interval)


def interval_lookback_policy(schema: str) -> dict[str, object] | None:
    days = interval_lookback_days(schema)
    return None if days is None else {"unit": "days", "value": days}


def interval_lookback(schema: str) -> timedelta | None:
    days = interval_lookback_days(schema)
    return None if days is None else timedelta(days=days)


def heavy_book_lookback_policy(schema: str) -> dict[str, object] | None:
    """Return the bounded initial baseline for dense book-event schemas."""

    days = HEAVY_BOOK_LOOKBACK_DAYS.get(str(schema).strip().lower())
    return None if days is None else {"unit": "days", "value": days}


__all__ = [
    "DEFINITION_LOOKBACK_DAYS",
    "HEAVY_BOOK_LOOKBACK_DAYS",
    "INTERVAL_LOOKBACK_DAYS",
    "SCHEMA_LOOKBACK_DAYS",
    "heavy_book_lookback_policy",
    "interval_lookback",
    "interval_lookback_days",
    "interval_lookback_policy",
]
