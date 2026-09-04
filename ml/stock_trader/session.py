from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ml.calendars import ExchangeSessionCalendar
from ml.calendars import (
    CHECKPOINT_SESSION_CLOSED,
    CHECKPOINT_SESSION_POST,
    CHECKPOINT_SESSION_PRE,
    CHECKPOINT_SESSION_REGULAR,
    FOUR_HOUR_CHECKPOINT_START_POLICY,
    HYBRID_TARGET_START_POLICY,
    US_EQUITY_ACTIONABLE_TARGET_POLICY,
)
from ml.stock_trader.contracts import utc


STOCK_TARGET_HORIZONS = ("1h", "4h")
_TARGET_START_POLICY_BY_HORIZON = {
    "1h": HYBRID_TARGET_START_POLICY,
    "4h": FOUR_HOUR_CHECKPOINT_START_POLICY,
}


@dataclass(frozen=True)
class StockExecutionWindow:
    executable: bool
    mode: str
    reason: str
    session_open: pd.Timestamp | None
    session_close: pd.Timestamp | None
    checkpoint_session: str = CHECKPOINT_SESSION_CLOSED
    time_in_force: str | None = None
    queue_target_start: pd.Timestamp | None = None


def stock_execution_window(
    as_of: object,
    *,
    allow_open_queue: bool = False,
    allow_premarket_queue: bool = False,
) -> StockExecutionWindow:
    """Resolve exact Schwab PRE, REGULAR, POST, or opening-queue execution."""

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
    actionable_open, _actionable_close = calendar.intraday_target_bounds(
        session,
        session_policy=US_EQUITY_ACTIONABLE_TARGET_POLICY,
    )
    if (
        allow_premarket_queue
        and actionable_open < opened
        and actionable_open - pd.Timedelta(hours=3) <= timestamp < actionable_open
    ):
        return StockExecutionWindow(
            executable=True,
            mode="PREMARKET_QUEUE",
            reason="EXPLICIT_PREMARKET_OPENING_QUEUE",
            session_open=opened,
            session_close=closed,
            checkpoint_session=CHECKPOINT_SESSION_PRE,
            time_in_force="AM",
            queue_target_start=actionable_open,
        )
    if allow_open_queue and opened - pd.Timedelta(hours=3) <= timestamp < opened:
        return StockExecutionWindow(
            executable=True,
            mode="OPEN_QUEUE",
            reason="EXPLICIT_NORMAL_SESSION_OPENING_QUEUE",
            session_open=opened,
            session_close=closed,
            checkpoint_session=CHECKPOINT_SESSION_REGULAR,
            time_in_force="DAY",
            queue_target_start=opened,
        )
    checkpoint_session = calendar.checkpoint_session_at(timestamp)
    if checkpoint_session == CHECKPOINT_SESSION_PRE:
        return StockExecutionWindow(
            executable=True,
            mode="PREMARKET",
            reason="SCHWAB_AM_EXTENDED_SESSION_OPEN",
            session_open=opened,
            session_close=closed,
            checkpoint_session=checkpoint_session,
            time_in_force="AM",
        )
    if checkpoint_session == CHECKPOINT_SESSION_REGULAR:
        return StockExecutionWindow(
            executable=True,
            mode="CORE",
            reason="XNYS_CORE_SESSION_OPEN",
            session_open=opened,
            session_close=closed,
            checkpoint_session=checkpoint_session,
            time_in_force="DAY",
        )
    if checkpoint_session == CHECKPOINT_SESSION_POST:
        return StockExecutionWindow(
            executable=True,
            mode="AFTER_HOURS",
            reason="SCHWAB_PM_EXTENDED_SESSION_OPEN",
            session_open=opened,
            session_close=closed,
            checkpoint_session=checkpoint_session,
            time_in_force="PM",
        )
    return StockExecutionWindow(
        executable=False,
        mode="CLOSED",
        reason="OUTSIDE_US_EQUITY_ACTIONABLE_SESSION",
        session_open=opened,
        session_close=closed,
        checkpoint_session=CHECKPOINT_SESSION_CLOSED,
    )


def decision_targets_open(
    target_window_start: object,
    window: StockExecutionWindow,
) -> bool:
    if not window.executable:
        return False
    try:
        target = utc(target_window_start)
    except (TypeError, ValueError):
        return False
    if window.mode in {"OPEN_QUEUE", "PREMARKET_QUEUE"}:
        return bool(
            window.queue_target_start is not None
            and abs((target - window.queue_target_start).total_seconds()) < 60.0
        )
    if window.session_open is None:
        return False
    target_session_date = target.tz_convert("America/New_York").date()
    execution_session_date = window.session_open.tz_convert(
        "America/New_York"
    ).date()
    if target_session_date != execution_session_date:
        return False
    target_session = checkpoint_session_for_target(target)
    if window.mode == "PREMARKET":
        return target_session == CHECKPOINT_SESSION_PRE
    if window.mode == "CORE":
        # A core-session EXT order may intentionally stage the next POST
        # checkpoint; all other cross-session implicit queues are prohibited.
        return target_session in {
            CHECKPOINT_SESSION_REGULAR,
            CHECKPOINT_SESSION_POST,
        }
    if window.mode == "AFTER_HOURS":
        return target_session == CHECKPOINT_SESSION_POST
    return False


def normalize_stock_target_horizon(value: object | None) -> str | None:
    """Return a canonical optional stock-target horizon or reject it."""

    if value is None:
        return None
    horizon = str(value).strip().lower()
    if horizon not in STOCK_TARGET_HORIZONS:
        expected = ", ".join(STOCK_TARGET_HORIZONS)
        raise ValueError(
            f"Unsupported stock target horizon {value!r}; expected {expected}"
        )
    return horizon


def next_stock_target_start(
    as_of: object,
    *,
    horizon: object | None = None,
) -> pd.Timestamp | None:
    """Return the next requested stock checkpoint strictly after ``as_of``.

    Omitting ``horizon`` retains the combined 1h/four-hour lookup for
    non-scheduled callers. Scheduled owners can bind themselves to one route so
    an earlier checkpoint from the other route cannot be consumed.
    """

    timestamp = utc(as_of)
    clean_horizon = normalize_stock_target_horizon(horizon)
    local_date = timestamp.tz_convert("America/New_York").date()
    calendar = ExchangeSessionCalendar(
        "XNYS",
        start=pd.Timestamp(local_date) - pd.Timedelta(days=7),
        end=pd.Timestamp(local_date) + pd.Timedelta(days=14),
    )
    start_policies = (
        tuple(_TARGET_START_POLICY_BY_HORIZON.values())
        if clean_horizon is None
        else (_TARGET_START_POLICY_BY_HORIZON[clean_horizon],)
    )
    candidates = sorted(
        {
            candidate
            for start_policy in start_policies
            for candidate in calendar.target_start_candidates(
                session_policy=US_EQUITY_ACTIONABLE_TARGET_POLICY,
                start_policy=start_policy,
            )
        }
    )
    return next((candidate for candidate in candidates if candidate > timestamp), None)


def checkpoint_session_for_target(value: object) -> str:
    """Return the stock checkpoint label for an eligible target timestamp."""

    timestamp = utc(value)
    local_date = timestamp.tz_convert("America/New_York").date()
    calendar = ExchangeSessionCalendar(
        "XNYS",
        start=pd.Timestamp(local_date) - pd.Timedelta(days=7),
        end=pd.Timestamp(local_date) + pd.Timedelta(days=7),
    )
    return calendar.checkpoint_session_at(timestamp)


def time_in_force_for_checkpoint(
    checkpoint_session: str,
    *,
    current_checkpoint_session: str | None = None,
) -> str:
    target = str(checkpoint_session or "").strip().upper()
    current = str(current_checkpoint_session or "").strip().upper()
    if target == CHECKPOINT_SESSION_POST and current == CHECKPOINT_SESSION_REGULAR:
        # Schwab PM-only orders are not eligible for entry until 16:05 ET. A
        # SEAMLESS day+extended limit order can be entered during core and
        # remain active for the 16:05 POST segment start.
        return "EXT"
    mapping = {
        CHECKPOINT_SESSION_PRE: "AM",
        CHECKPOINT_SESSION_REGULAR: "DAY",
        CHECKPOINT_SESSION_POST: "PM",
    }
    try:
        return mapping[target]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported stock checkpoint session: {checkpoint_session!r}"
        ) from exc


__all__ = [
    "STOCK_TARGET_HORIZONS",
    "StockExecutionWindow",
    "checkpoint_session_for_target",
    "decision_targets_open",
    "next_stock_target_start",
    "normalize_stock_target_horizon",
    "stock_execution_window",
    "time_in_force_for_checkpoint",
]
