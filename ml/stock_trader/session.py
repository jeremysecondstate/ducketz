from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ml.calendars import ExchangeSessionCalendar
from ml.stock_trader.contracts import utc


@dataclass(frozen=True)
class StockExecutionWindow:
    executable: bool
    mode: str
    reason: str
    session_open: pd.Timestamp | None
    session_close: pd.Timestamp | None


def stock_execution_window(
    as_of: object,
    *,
    allow_open_queue: bool = False,
) -> StockExecutionWindow:
    """Resolve the exact XNYS core or explicitly requested opening queue window."""

    timestamp = utc(as_of)
    local_date = timestamp.tz_convert("America/New_York").date()
    calendar = ExchangeSessionCalendar(
        "XNYS",
        start=pd.Timestamp(local_date) - pd.Timedelta(days=7),
        end=pd.Timestamp(local_date) + pd.Timedelta(days=7),
    )
    session = pd.Timestamp(local_date)
    if session not in calendar.sessions:
        return StockExecutionWindow(
            executable=False,
            mode="CLOSED",
            reason="TODAY_IS_NOT_AN_XNYS_SESSION",
            session_open=None,
            session_close=None,
        )
    opened = calendar.session_open(session)
    closed = calendar.session_close(session)
    if opened <= timestamp < closed:
        return StockExecutionWindow(
            executable=True,
            mode="CORE",
            reason="XNYS_CORE_SESSION_OPEN",
            session_open=opened,
            session_close=closed,
        )
    if allow_open_queue and opened - pd.Timedelta(hours=3) <= timestamp < opened:
        return StockExecutionWindow(
            executable=True,
            mode="OPEN_QUEUE",
            reason="EXPLICIT_NORMAL_SESSION_OPENING_QUEUE",
            session_open=opened,
            session_close=closed,
        )
    return StockExecutionWindow(
        executable=False,
        mode="CLOSED",
        reason="OUTSIDE_XNYS_CORE_SESSION",
        session_open=opened,
        session_close=closed,
    )


def decision_targets_open(
    target_window_start: object,
    window: StockExecutionWindow,
) -> bool:
    if window.mode != "OPEN_QUEUE" or window.session_open is None:
        return True
    try:
        target = utc(target_window_start)
    except (TypeError, ValueError):
        return False
    return abs((target - window.session_open).total_seconds()) < 60.0


def next_stock_target_start(as_of: object) -> pd.Timestamp | None:
    """Return the next exchange-defined intraday target start after ``as_of``."""

    timestamp = utc(as_of)
    local_date = timestamp.tz_convert("America/New_York").date()
    calendar = ExchangeSessionCalendar(
        "XNYS",
        start=pd.Timestamp(local_date) - pd.Timedelta(days=7),
        end=pd.Timestamp(local_date) + pd.Timedelta(days=14),
    )
    return next(
        (
            candidate
            for candidate in calendar.target_start_candidates()
            if candidate > timestamp
        ),
        None,
    )


__all__ = [
    "StockExecutionWindow",
    "decision_targets_open",
    "next_stock_target_start",
    "stock_execution_window",
]
