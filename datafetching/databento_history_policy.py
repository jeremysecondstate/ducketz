"""Shared Databento historical bootstrap limits.

The interval suffix is the policy authority.  This intentionally covers every
Databento interval schema (for example ``ohlcv-1s``, ``bbo-1s``, and
``cbbo-1m``), rather than applying limits only to OHLCV bars.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Mapping


INTERVAL_LOOKBACK_DAYS: Mapping[str, int] = {
    "1s": 5,
    "1m": 100,
    "1h": 1_825,
    "1d": 2_555,
}


def interval_lookback_days(schema: str) -> int | None:
    """Return the configured day cap for a timestamp-interval schema."""

    interval = str(schema).strip().lower().rsplit("-", maxsplit=1)[-1]
    return INTERVAL_LOOKBACK_DAYS.get(interval)


def interval_lookback_policy(schema: str) -> dict[str, object] | None:
    days = interval_lookback_days(schema)
    return None if days is None else {"unit": "days", "value": days}


def interval_lookback(schema: str) -> timedelta | None:
    days = interval_lookback_days(schema)
    return None if days is None else timedelta(days=days)


__all__ = [
    "INTERVAL_LOOKBACK_DAYS",
    "interval_lookback",
    "interval_lookback_days",
    "interval_lookback_policy",
]
