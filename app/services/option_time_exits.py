from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from dateutil.tz import tzlocal

from app.models.option_management import SavedTimeBasedExitRule, TimeBasedExitRule


BEFORE_EXPIRATION = "BEFORE_EXPIRATION"
SPECIFIC_DATE_TIME = "SPECIFIC_DATE_TIME"
TIME_EXIT_RULE_TYPES = (BEFORE_EXPIRATION, SPECIFIC_DATE_TIME)

DEFAULT_OPTION_CALENDAR = "XNYS"
DEFAULT_MARKET_TIMEZONE = "America/New_York"
DEFAULT_MINUTES_BEFORE_CLOSE = 30

MARKET_CLOSE_CHOICES: tuple[tuple[str, int], ...] = (
    ("At scheduled session close", 0),
    ("30 minutes before scheduled close", 30),
    ("60 minutes before scheduled close", 60),
)

TIME_EXIT_CAPABILITY_REASON = (
    "Planning only: timed Schwab option-order execution is not verified. "
    "No order is scheduled or sent."
)
TIME_EXIT_HELP = (
    "A time-based exit closes the selected coverage because a date or time is reached, "
    "not because the position mark reaches a profit or loss level. A time trigger does "
    "not guarantee a fill."
)


@dataclass(frozen=True)
class TimeExitPresentation:
    summary: str
    resolved_time: str
    local_equivalent: str | None
    expiration_basis: str | None


def resolve_before_expiration_time_exit(
    expirations: Iterable[object],
    *,
    sessions_before_expiration: object,
    minutes_before_session_close: object = DEFAULT_MINUTES_BEFORE_CLOSE,
    now: datetime,
    calendar_name: str = DEFAULT_OPTION_CALENDAR,
) -> TimeBasedExitRule:
    """Resolve a relative rule against official exchange sessions and closes."""

    current = _aware_datetime(now, "Current time")
    selected_expirations = _expiration_values(expirations)
    earliest = date.fromisoformat(selected_expirations[0])
    session_count = _positive_integer(
        sessions_before_expiration,
        "Trading sessions before expiration",
    )
    close_offset = _nonnegative_integer(
        minutes_before_session_close,
        "Minutes before scheduled session close",
    )
    calendar = _calendar(
        calendar_name,
        start=earliest - timedelta(days=max(45, session_count * 4 + 14)),
        end=earliest,
    )
    expiration_label = pd.Timestamp(earliest)
    prior_sessions = calendar.sessions[calendar.sessions < expiration_label]
    if len(prior_sessions) < session_count:
        raise ValueError(
            "The exchange calendar does not contain enough prior trading sessions "
            "for this rule. Choose fewer sessions before expiration."
        )
    target_session = prior_sessions[-session_count]
    session_open = _python_datetime(calendar.opens.loc[target_session])
    session_close = _python_datetime(calendar.closes.loc[target_session])
    trigger_at = session_close - timedelta(minutes=close_offset)
    if trigger_at < session_open:
        raise ValueError(
            "The selected time before close falls before the scheduled session open. "
            "Choose a smaller close offset."
        )
    _require_future(trigger_at, current)
    return TimeBasedExitRule(
        rule_type=BEFORE_EXPIRATION,
        trigger_at=trigger_at.astimezone(timezone.utc),
        timezone_name=str(calendar.tz),
        calendar_name=str(calendar.name),
        sessions_before_expiration=session_count,
        minutes_before_session_close=close_offset,
        expiration_basis=earliest.isoformat(),
        selected_expirations=selected_expirations,
    )


def resolve_specific_time_exit(
    expirations: Iterable[object],
    *,
    specific_date: object,
    specific_time: object,
    timezone_name: object,
    now: datetime,
    calendar_name: str = DEFAULT_OPTION_CALENDAR,
) -> TimeBasedExitRule:
    """Resolve an explicitly zoned wall-clock value without accepting naive time."""

    current = _aware_datetime(now, "Current time")
    selected_expirations = _expiration_values(expirations)
    selected_date = _date_value(specific_date, "Specific exit date")
    selected_time = _time_value(specific_time, "Specific exit time")
    zone_name = str(timezone_name or "").strip()
    if not zone_name:
        raise ValueError("Specific exit timezone is required; choose an explicit timezone.")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"Specific exit timezone {zone_name!r} is not recognized. Choose a valid timezone."
        ) from exc
    trigger_at = _localize_wall_time(selected_date, selected_time, zone)
    _require_future(trigger_at, current)

    earliest = date.fromisoformat(selected_expirations[0])
    calendar = _calendar(
        calendar_name,
        start=earliest - timedelta(days=21),
        end=earliest,
    )
    expiration_label = pd.Timestamp(earliest)
    eligible_sessions = calendar.sessions[calendar.sessions <= expiration_label]
    if not len(eligible_sessions):
        raise ValueError("The earliest selected expiration has no prior exchange session.")
    expiration_close = _python_datetime(calendar.closes.loc[eligible_sessions[-1]])
    if trigger_at.astimezone(timezone.utc) > expiration_close.astimezone(timezone.utc):
        raise ValueError(
            "The specific exit time is after the earliest selected expiration closes. "
            "Choose a date and time no later than that expiration session."
        )
    return TimeBasedExitRule(
        rule_type=SPECIFIC_DATE_TIME,
        trigger_at=trigger_at.astimezone(timezone.utc),
        timezone_name=zone_name,
        calendar_name=None,
        sessions_before_expiration=None,
        minutes_before_session_close=None,
        expiration_basis=None,
        selected_expirations=selected_expirations,
    )


def validate_time_exit_rule(
    rule: TimeBasedExitRule,
    *,
    selected_expirations: Iterable[object],
    now: datetime,
) -> None:
    if not isinstance(rule, TimeBasedExitRule):
        raise ValueError("The configured time-based exit rule is malformed.")
    current = _aware_datetime(now, "Current time")
    _require_future(rule.trigger_at, current)
    expirations = _expiration_values(selected_expirations)
    if rule.selected_expirations != expirations:
        raise ValueError(
            "The selected coverage or expiration changed. Re-resolve the time-based exit rule."
        )
    if rule.rule_type == BEFORE_EXPIRATION:
        expected_basis = expirations[0]
        if (
            rule.expiration_basis != expected_basis
            or rule.sessions_before_expiration is None
            or rule.sessions_before_expiration < 1
            or rule.minutes_before_session_close is None
            or rule.minutes_before_session_close < 0
            or not rule.calendar_name
        ):
            raise ValueError(
                "The before-expiration rule is incomplete. Choose a positive session count "
                "and a scheduled-close offset."
            )
    elif rule.rule_type == SPECIFIC_DATE_TIME:
        if (
            rule.expiration_basis is not None
            or rule.sessions_before_expiration is not None
            or rule.minutes_before_session_close is not None
            or not rule.timezone_name
        ):
            raise ValueError(
                "The specific date-and-time rule is malformed. Choose a date, time, and timezone."
            )
    else:
        raise ValueError(f"Unknown time-based exit rule type: {rule.rule_type or 'missing'}")


def saved_time_exit_from_rule(rule: TimeBasedExitRule) -> SavedTimeBasedExitRule:
    if rule.rule_type != BEFORE_EXPIRATION:
        raise ValueError(
            "A specific absolute date and time cannot be saved as a timeless reusable "
            "template. Remove it or switch to Before expiration."
        )
    if (
        rule.sessions_before_expiration is None
        or rule.minutes_before_session_close is None
        or not rule.calendar_name
    ):
        raise ValueError("The relative time-based exit rule is incomplete and cannot be saved.")
    return SavedTimeBasedExitRule(
        rule_type=BEFORE_EXPIRATION,
        sessions_before_expiration=rule.sessions_before_expiration,
        minutes_before_session_close=rule.minutes_before_session_close,
        calendar_name=rule.calendar_name,
    )


def time_exit_presentation(
    rule: TimeBasedExitRule,
    *,
    local_timezone: tzinfo | None = None,
) -> TimeExitPresentation:
    try:
        configured_zone = ZoneInfo(rule.timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Time-based exit timezone {rule.timezone_name!r} is not recognized.") from exc
    scheduled = rule.trigger_at.astimezone(configured_zone)
    if rule.rule_type == BEFORE_EXPIRATION:
        count = rule.sessions_before_expiration or 0
        basis = date.fromisoformat(str(rule.expiration_basis))
        summary = (
            f"Time-based exit \u00b7 {count} trading session{'s' if count != 1 else ''} "
            f"before {_short_date(basis)} at {_clock(scheduled)} "
            f"{_summary_zone(rule.timezone_name, scheduled)} \u00b7 Review only"
        )
        expiration_basis = (
            f"Earliest of {len(rule.selected_expirations)} selected expiration"
            f"{'s' if len(rule.selected_expirations) != 1 else ''}: {_long_date(basis)}"
        )
    else:
        summary = (
            f"Time-based exit \u00b7 {_short_date(scheduled.date())} at {_clock(scheduled)} "
            f"{scheduled.tzname() or rule.timezone_name} \u00b7 Review only"
        )
        expiration_basis = None
    resolved = (
        f"{_long_date(scheduled.date())} at {_clock(scheduled)} "
        f"{scheduled.tzname() or ''} ({rule.timezone_name})"
    ).replace("  ", " ")
    local_zone = local_timezone or system_local_timezone()
    local = rule.trigger_at.astimezone(local_zone)
    if local.replace(tzinfo=None) == scheduled.replace(tzinfo=None) and local.utcoffset() == scheduled.utcoffset():
        local_equivalent = None
    else:
        local_equivalent = (
            f"{_long_date(local.date())} at {_clock(local)} "
            f"{local.tzname() or 'local time'}"
        )
    return TimeExitPresentation(
        summary=summary,
        resolved_time=resolved,
        local_equivalent=local_equivalent,
        expiration_basis=expiration_basis,
    )


def system_local_timezone() -> tzinfo:
    """Return the OS local zone with daylight-saving rules, not a fixed offset."""

    return tzlocal()


def _calendar(calendar_name: object, *, start: date, end: date) -> object:
    name = str(calendar_name or "").strip().upper()
    if not name:
        raise ValueError("An exchange calendar is required for a time-based exit.")
    try:
        import exchange_calendars as xcals

        return xcals.get_calendar(name, start=start, end=end)
    except Exception as exc:
        raise ValueError(
            f"Could not resolve exchange calendar {name!r} from {start} through {end}: {exc}"
        ) from exc


def _expiration_values(values: Iterable[object]) -> tuple[str, ...]:
    expirations: set[date] = set()
    for value in values:
        expirations.add(_date_value(value, "Selected-leg expiration"))
    if not expirations:
        raise ValueError("A time-based exit requires at least one selected-leg expiration.")
    return tuple(item.isoformat() for item in sorted(expirations))


def _date_value(value: object, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str("" if value is None else value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must use YYYY-MM-DD.") from exc


def _time_value(value: object, label: str) -> time:
    if isinstance(value, time):
        if value.tzinfo is not None:
            raise ValueError(f"{label} must use a separate explicit timezone control.")
        return value.replace(second=0, microsecond=0)
    text = str(value or "").strip()
    try:
        parsed = time.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} must use HH:MM in 24-hour time.") from exc
    if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
        raise ValueError(f"{label} must use HH:MM in 24-hour time.")
    return parsed


def _positive_integer(value: object, label: str) -> int:
    number = _integer(value, label)
    if number < 1:
        raise ValueError(f"{label} must be a positive whole number.")
    return number


def _nonnegative_integer(value: object, label: str) -> int:
    number = _integer(value, label)
    if number < 0:
        raise ValueError(f"{label} cannot be negative.")
    return number


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number.")
    text = str("" if value is None else value).strip()
    try:
        number = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a whole number.") from exc
    if text not in {str(number), f"+{number}"}:
        raise ValueError(f"{label} must be a whole number.")
    return number


def _aware_datetime(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware.")
    return value.astimezone(timezone.utc)


def _require_future(trigger_at: datetime, now: datetime) -> None:
    trigger = _aware_datetime(trigger_at, "Time-based exit trigger")
    current = _aware_datetime(now, "Current time")
    if trigger <= current:
        raise ValueError(
            "The time-based exit schedule is in the past. Choose a future date or time."
        )


def _localize_wall_time(selected_date: date, selected_time: time, zone: ZoneInfo) -> datetime:
    naive = datetime.combine(selected_date, selected_time)
    candidates: dict[datetime, datetime] = {}
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive:
            candidates[candidate.astimezone(timezone.utc)] = candidate
    if not candidates:
        raise ValueError(
            "The selected local time does not exist because of a daylight-saving transition. "
            "Choose another time."
        )
    if len(candidates) > 1:
        raise ValueError(
            "The selected local time is ambiguous because of a daylight-saving transition. "
            "Choose another time."
        )
    return next(iter(candidates.values()))


def _python_datetime(value: object) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.to_pydatetime()


def _clock(value: datetime) -> str:
    hour = value.strftime("%I").lstrip("0") or "0"
    return f"{hour}:{value:%M %p}"


def _short_date(value: date) -> str:
    return f"{value:%b} {value.day}"


def _long_date(value: date) -> str:
    return f"{value:%b} {value.day}, {value.year}"


def _summary_zone(timezone_name: str, value: datetime) -> str:
    if timezone_name == DEFAULT_MARKET_TIMEZONE:
        return "ET"
    return value.tzname() or timezone_name


__all__ = [
    "BEFORE_EXPIRATION",
    "DEFAULT_MARKET_TIMEZONE",
    "DEFAULT_MINUTES_BEFORE_CLOSE",
    "DEFAULT_OPTION_CALENDAR",
    "MARKET_CLOSE_CHOICES",
    "SPECIFIC_DATE_TIME",
    "TIME_EXIT_CAPABILITY_REASON",
    "TIME_EXIT_HELP",
    "TIME_EXIT_RULE_TYPES",
    "TimeExitPresentation",
    "resolve_before_expiration_time_exit",
    "resolve_specific_time_exit",
    "saved_time_exit_from_rule",
    "system_local_timezone",
    "time_exit_presentation",
    "validate_time_exit_rule",
]
