from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from numbers import Integral
from typing import Any

import pandas as pd

from ml.contracts import MLContractError

SESSION_POLICY_VERSION = "exchange-calendars-regular-session-v1"
DAILY_BAR_LABEL_POLICY_VERSION = "provider-utc-session-date-v1"
TARGET_MINUTE_POLICY_VERSION = (
    "session-open-break-resume-plus-full-local-clock-anchor-v1"
)


@dataclass(frozen=True)
class SessionHorizon:
    decision_session: pd.Timestamp
    official_close_timestamp: pd.Timestamp
    entry_session: pd.Timestamp
    entry_timestamp: pd.Timestamp
    exit_session: pd.Timestamp
    exit_timestamp: pd.Timestamp
    future_sessions: tuple[pd.Timestamp, ...]


@dataclass(frozen=True)
class EligibleMarketInterval:
    exchange_session: pd.Timestamp
    start_timestamp: pd.Timestamp
    end_timestamp: pd.Timestamp


@dataclass(frozen=True)
class EligibleMinuteTargetWindow:
    start_timestamp: pd.Timestamp
    end_timestamp: pd.Timestamp
    constituent_timestamps: tuple[pd.Timestamp, ...]


class ExchangeSessionCalendar:
    """Pinned exchange-session view used by Duckets daily ML contracts."""

    def __init__(
        self,
        exchange_calendar: str,
        *,
        start: object,
        end: object,
    ) -> None:
        calendar_name = str(exchange_calendar or "").strip().upper()
        if not calendar_name:
            raise MLContractError("exchange_calendar is required")
        start_session = _session_label(start)
        end_session = _session_label(end)
        if end_session < start_session:
            raise ValueError("calendar end precedes start")

        xcals = _exchange_calendars_module()
        try:
            self._calendar = xcals.get_calendar(
                calendar_name,
                start=start_session,
                end=end_session,
            )
        except Exception as exc:
            raise MLContractError(
                f"Could not load exchange calendar {calendar_name!r} from "
                f"{start_session.date()} through {end_session.date()}: {exc}"
            ) from exc

        self.exchange_calendar = calendar_name
        self.exchange_calendar_name = str(self._calendar.name)
        self.exchange_calendar_version = _exchange_calendars_version()
        self.exchange_timezone = str(self._calendar.tz)
        self.session_policy_version = SESSION_POLICY_VERSION
        self.daily_bar_label_policy_version = DAILY_BAR_LABEL_POLICY_VERSION
        self.target_minute_policy_version = TARGET_MINUTE_POLICY_VERSION
        self._target_start_candidates_cache: tuple[pd.Timestamp, ...] | None = None
        self._eligible_minute_timestamps_cache: tuple[pd.Timestamp, ...] | None = None
        self._eligible_minute_positions_cache: dict[pd.Timestamp, int] | None = None

    @property
    def sessions(self) -> pd.DatetimeIndex:
        return self._calendar.sessions

    def session_for_daily_bar(self, bar_timestamp: object) -> pd.Timestamp:
        """Map a provider daily label to an exact exchange session label.

        The initial Duckets providers label daily bars with a UTC timestamp whose
        UTC calendar date is the represented market session. The returned session
        label is timezone-naive, matching ``exchange_calendars`` session indexes.
        """

        label = _session_label(bar_timestamp)
        if label not in self.sessions:
            raise MLContractError(
                f"Daily bar timestamp {pd.Timestamp(bar_timestamp)!s} maps to "
                f"{label.date()}, which is not a session on "
                f"{self.exchange_calendar}."
            )
        return label

    def session_open(self, session: object) -> pd.Timestamp:
        label = self._require_session(session)
        return _utc_timestamp(self._calendar.opens.loc[label])

    def session_close(self, session: object) -> pd.Timestamp:
        label = self._require_session(session)
        return _utc_timestamp(self._calendar.closes.loc[label])

    def eligible_hour_intervals(
        self,
        *,
        start_session: object | None = None,
        end_session: object | None = None,
    ) -> tuple[EligibleMarketInterval, ...]:
        """Return full, exchange-local clock-hour intervals inside sessions.

        Partial opening intervals, session breaks, and partial closing intervals
        are excluded. Exchange schedule timestamps own holiday, early-close, and
        daylight-saving behavior.
        """

        sessions = self.sessions
        if start_session is not None:
            start_label = _session_label(start_session)
            sessions = sessions[sessions >= start_label]
        if end_session is not None:
            end_label = _session_label(end_session)
            sessions = sessions[sessions <= end_label]

        records: list[EligibleMarketInterval] = []
        break_starts = getattr(self._calendar, "break_starts", None)
        break_ends = getattr(self._calendar, "break_ends", None)
        for session in sessions:
            session_open = self.session_open(session)
            session_close = self.session_close(session)
            break_start = (
                _optional_utc_timestamp(break_starts.loc[session])
                if break_starts is not None and session in break_starts.index
                else None
            )
            break_end = (
                _optional_utc_timestamp(break_ends.loc[session])
                if break_ends is not None and session in break_ends.index
                else None
            )
            segments = (
                ((session_open, break_start), (break_end, session_close))
                if break_start is not None and break_end is not None
                else ((session_open, session_close),)
            )
            for segment_start, segment_end in segments:
                if segment_start is None or segment_end is None:
                    continue
                cursor = _ceil_exchange_hour(
                    segment_start,
                    timezone_name=self.exchange_timezone,
                )
                while cursor + pd.Timedelta(hours=1) <= segment_end:
                    records.append(
                        EligibleMarketInterval(
                            exchange_session=pd.Timestamp(session),
                            start_timestamp=cursor,
                            end_timestamp=cursor + pd.Timedelta(hours=1),
                        )
                    )
                    cursor += pd.Timedelta(hours=1)
        return tuple(records)

    def target_start_candidates(
        self,
        *,
        start_session: object | None = None,
        end_session: object | None = None,
    ) -> tuple[pd.Timestamp, ...]:
        """Return the versioned hybrid starts for intraday target windows.

        Each continuous regular-session segment contributes its exact start
        (the official session open or post-break resume) plus every complete
        exchange-local clock hour wholly contained in that segment.  The
        calendar fixes these candidates before any target-price lookup.
        """

        use_cache = start_session is None and end_session is None
        if use_cache and self._target_start_candidates_cache is not None:
            return self._target_start_candidates_cache

        candidates: set[pd.Timestamp] = set()
        for _session, segment_start, segment_end in self._regular_segments(
            start_session=start_session,
            end_session=end_session,
        ):
            candidates.add(segment_start)
            cursor = _ceil_exchange_hour(
                segment_start,
                timezone_name=self.exchange_timezone,
            )
            while cursor + pd.Timedelta(hours=1) <= segment_end:
                candidates.add(cursor)
                cursor += pd.Timedelta(hours=1)
        result = tuple(sorted(candidates))
        if use_cache:
            self._target_start_candidates_cache = result
        return result

    def eligible_minute_timestamps(
        self,
        *,
        start_session: object | None = None,
        end_session: object | None = None,
    ) -> tuple[pd.Timestamp, ...]:
        """Return exact interval-open timestamps for regular-session minutes."""

        use_cache = start_session is None and end_session is None
        if use_cache and self._eligible_minute_timestamps_cache is not None:
            return self._eligible_minute_timestamps_cache

        timestamps: list[pd.Timestamp] = []
        minute = pd.Timedelta(minutes=1)
        for _session, segment_start, segment_end in self._regular_segments(
            start_session=start_session,
            end_session=end_session,
        ):
            if (
                segment_start.floor("min") != segment_start
                or segment_end.floor("min") != segment_end
            ):
                raise MLContractError(
                    f"Calendar {self.exchange_calendar} contains a regular "
                    "segment that is not aligned to native one-minute bars: "
                    f"{segment_start} through {segment_end}."
                )
            cursor = segment_start
            while cursor + minute <= segment_end:
                timestamps.append(cursor)
                cursor += minute
        result = tuple(timestamps)
        if use_cache:
            self._eligible_minute_timestamps_cache = result
        return result

    def target_window_after(
        self,
        information_available_at: object,
        *,
        eligible_minute_count: int,
    ) -> EligibleMinuteTargetWindow:
        """Select the first hybrid start strictly after information availability.

        Once selected, exactly ``eligible_minute_count`` predetermined regular
        one-minute intervals are accumulated. Exchange breaks and closed periods
        pause accumulation; they never cause the start or a missing constituent
        to be shifted.
        """

        if eligible_minute_count < 1:
            raise ValueError("eligible_minute_count must be positive")
        available = _utc_timestamp(information_available_at)
        candidates = self.target_start_candidates()
        location = bisect_right(candidates, available)
        if location >= len(candidates):
            raise MLContractError(
                "Exchange calendar padding did not cover a target start "
                "strictly after information availability"
            )
        target_start = candidates[location]
        eligible_minutes = self.eligible_minute_timestamps()
        if self._eligible_minute_positions_cache is None:
            self._eligible_minute_positions_cache = {
                timestamp: index
                for index, timestamp in enumerate(eligible_minutes)
            }
        try:
            minute_location = self._eligible_minute_positions_cache[target_start]
        except KeyError as exc:  # pragma: no cover - invariant guard
            raise MLContractError(
                f"Target start {target_start} is not an eligible regular "
                "one-minute interval."
            ) from exc
        selected = eligible_minutes[
            minute_location : minute_location + eligible_minute_count
        ]
        if len(selected) != eligible_minute_count:
            raise MLContractError(
                "Exchange calendar padding did not cover every selected "
                f"target minute; required {eligible_minute_count}, "
                f"observed {len(selected)}."
            )
        return EligibleMinuteTargetWindow(
            start_timestamp=target_start,
            end_timestamp=selected[-1] + pd.Timedelta(minutes=1),
            constituent_timestamps=tuple(selected),
        )

    def _regular_segments(
        self,
        *,
        start_session: object | None = None,
        end_session: object | None = None,
    ) -> tuple[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp], ...]:
        sessions = self.sessions
        if start_session is not None:
            start_label = _session_label(start_session)
            sessions = sessions[sessions >= start_label]
        if end_session is not None:
            end_label = _session_label(end_session)
            sessions = sessions[sessions <= end_label]

        records: list[tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]] = []
        break_starts = getattr(self._calendar, "break_starts", None)
        break_ends = getattr(self._calendar, "break_ends", None)
        for session in sessions:
            session_open = self.session_open(session)
            session_close = self.session_close(session)
            break_start = (
                _optional_utc_timestamp(break_starts.loc[session])
                if break_starts is not None and session in break_starts.index
                else None
            )
            break_end = (
                _optional_utc_timestamp(break_ends.loc[session])
                if break_ends is not None and session in break_ends.index
                else None
            )
            segments = (
                ((session_open, break_start), (break_end, session_close))
                if break_start is not None and break_end is not None
                else ((session_open, session_close),)
            )
            for segment_start, segment_end in segments:
                if (
                    segment_start is not None
                    and segment_end is not None
                    and segment_start < segment_end
                ):
                    records.append(
                        (
                            pd.Timestamp(session),
                            segment_start,
                            segment_end,
                        )
                    )
        return tuple(records)

    def is_final_session_of_exchange_week(self, session: object) -> bool:
        label = self._require_session(session)
        location = self.sessions.get_loc(label)
        if not isinstance(location, Integral):
            raise MLContractError(f"Calendar session {label.date()} is ambiguous")
        if location + 1 >= len(self.sessions):
            raise MLContractError(
                f"Calendar {self.exchange_calendar} has no session after "
                f"{label.date()} to determine the exchange-week boundary."
            )
        return _exchange_week_key(self.sessions[location + 1]) != _exchange_week_key(
            label
        )

    def next_exchange_week(
        self,
        session: object,
    ) -> tuple[pd.Timestamp, ...]:
        label = self._require_session(session)
        location = self.sessions.get_loc(label)
        if not isinstance(location, Integral):
            raise MLContractError(f"Calendar session {label.date()} is ambiguous")
        next_location = location + 1
        if next_location >= len(self.sessions):
            raise MLContractError(
                f"Calendar {self.exchange_calendar} has no future exchange week "
                f"after {label.date()}."
            )
        next_key = _exchange_week_key(self.sessions[next_location])
        future: list[pd.Timestamp] = []
        for candidate in self.sessions[next_location:]:
            if _exchange_week_key(candidate) != next_key:
                break
            future.append(pd.Timestamp(candidate))
        if not future:
            raise MLContractError(
                f"Calendar {self.exchange_calendar} did not resolve the next "
                f"exchange week after {label.date()}."
            )
        return tuple(future)

    def horizon(
        self,
        *,
        decision_session: object,
        decision_timestamp: object,
        future_session_count: int = 5,
    ) -> SessionHorizon:
        if future_session_count < 1:
            raise ValueError("future_session_count must be positive")
        session = self._require_session(decision_session)
        location = self.sessions.get_loc(session)
        if not isinstance(location, Integral):
            raise MLContractError(f"Calendar session {session.date()} is ambiguous")

        entry_location = location + 1
        exit_location = location + future_session_count
        if exit_location >= len(self.sessions):
            raise MLContractError(
                f"Calendar {self.exchange_calendar} does not cover the "
                f"{future_session_count}-session horizon after {session.date()}."
            )

        official_close = self.session_close(session)
        future_sessions = tuple(self.sessions[entry_location : exit_location + 1])
        entry_session = future_sessions[0]
        exit_session = future_sessions[-1]
        entry_timestamp = self.session_open(entry_session)
        exit_timestamp = self.session_close(exit_session)
        decision = _utc_timestamp(decision_timestamp)

        if decision < official_close:
            raise MLContractError(
                f"decision_timestamp {decision} precedes the official close "
                f"{official_close} for {session.date()}."
            )
        if decision >= entry_timestamp:
            raise MLContractError(
                f"decision_timestamp {decision} is not earlier than the next "
                f"eligible session open {entry_timestamp}."
            )

        return SessionHorizon(
            decision_session=session,
            official_close_timestamp=official_close,
            entry_session=entry_session,
            entry_timestamp=entry_timestamp,
            exit_session=exit_session,
            exit_timestamp=exit_timestamp,
            future_sessions=future_sessions,
        )

    def _require_session(self, value: object) -> pd.Timestamp:
        label = _session_label(value)
        if label not in self.sessions:
            raise MLContractError(
                f"{label.date()} is not a session on {self.exchange_calendar}."
            )
        return label


def attach_official_daily_sessions(
    frame: pd.DataFrame,
    *,
    calendar_column: str = "exchange_calendar",
    bar_timestamp_column: str = "bar_timestamp",
    processing_delay: pd.Timedelta = pd.Timedelta(0),
    future_padding_days: int = 45,
) -> pd.DataFrame:
    """Attach calendar-authoritative session closes and decision timestamps."""

    if processing_delay < pd.Timedelta(0):
        raise ValueError("processing_delay cannot be negative")
    required = {calendar_column, bar_timestamp_column}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise MLContractError(
            "Calendar attachment is missing columns: " + ", ".join(missing)
        )
    if frame.empty:
        return frame.copy()

    base = frame.copy()
    base[bar_timestamp_column] = pd.to_datetime(
        base[bar_timestamp_column], utc=True, errors="coerce"
    )
    if base[bar_timestamp_column].isna().any():
        raise MLContractError("Daily bar timestamps must be valid UTC timestamps")
    base[calendar_column] = (
        base[calendar_column].astype("string").str.strip().str.upper()
    )
    if base[calendar_column].isna().any() or base[calendar_column].eq("").any():
        raise MLContractError("exchange_calendar cannot be missing")

    base = base.reset_index(drop=False).rename(columns={"index": "__calendar_order"})
    parts: list[pd.DataFrame] = []
    for calendar_name, group in base.groupby(
        calendar_column, sort=False, dropna=False
    ):
        labels = group[bar_timestamp_column].map(_session_label)
        start = labels.min() - pd.Timedelta(days=14)
        end = labels.max() + pd.Timedelta(days=future_padding_days)
        calendar = ExchangeSessionCalendar(calendar_name, start=start, end=end)

        invalid = ~labels.isin(calendar.sessions)
        if invalid.any():
            bad = sorted({value.date().isoformat() for value in labels.loc[invalid]})
            raise MLContractError(
                f"Daily bars map to non-session dates on {calendar_name}: "
                + ", ".join(bad[:10])
            )

        part = group.copy()
        session_index = pd.DatetimeIndex(labels.to_numpy())
        closes = calendar._calendar.closes.reindex(session_index)
        if closes.isna().any():
            raise MLContractError(
                f"Calendar {calendar_name} did not resolve every official close."
            )
        part["exchange_session"] = session_index.to_numpy()
        part["bar_end_timestamp"] = pd.DatetimeIndex(closes.to_numpy())
        part["decision_timestamp"] = part["bar_end_timestamp"] + processing_delay
        part["exchange_calendar_name"] = calendar.exchange_calendar_name
        part["exchange_calendar_version"] = calendar.exchange_calendar_version
        part["exchange_timezone"] = calendar.exchange_timezone
        part["session_policy_version"] = calendar.session_policy_version
        part["daily_bar_label_policy_version"] = (
            calendar.daily_bar_label_policy_version
        )
        parts.append(part)

    result = pd.concat(parts, ignore_index=True, sort=False)
    result = result.sort_values("__calendar_order").drop(columns="__calendar_order")
    return result.reset_index(drop=True)


def attach_official_intraday_sessions(
    frame: pd.DataFrame,
    *,
    calendar_column: str = "exchange_calendar",
    bar_timestamp_column: str = "bar_timestamp",
    bar_end_column: str = "operational_bar_end_timestamp",
    processing_delay: pd.Timedelta = pd.Timedelta(0),
    future_padding_days: int = 45,
) -> pd.DataFrame:
    """Attach exchange sessions and validate full regular-market clock hours."""

    if processing_delay < pd.Timedelta(0):
        raise ValueError("processing_delay cannot be negative")
    required = {calendar_column, bar_timestamp_column, bar_end_column}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise MLContractError(
            "Intraday calendar attachment is missing columns: "
            + ", ".join(missing)
        )
    if frame.empty:
        return frame.copy()

    base = frame.copy()
    for column in (bar_timestamp_column, bar_end_column):
        base[column] = pd.to_datetime(base[column], utc=True, errors="coerce")
    if base[[bar_timestamp_column, bar_end_column]].isna().any().any():
        raise MLContractError("Intraday bar timestamps must be valid UTC timestamps")
    base[calendar_column] = (
        base[calendar_column].astype("string").str.strip().str.upper()
    )
    if base[calendar_column].isna().any() or base[calendar_column].eq("").any():
        raise MLContractError("exchange_calendar cannot be missing")

    base = base.reset_index(drop=False).rename(columns={"index": "__calendar_order"})
    parts: list[pd.DataFrame] = []
    for calendar_name, group in base.groupby(
        calendar_column, sort=False, dropna=False
    ):
        rough_labels = group[bar_timestamp_column].dt.tz_convert(
            "UTC"
        ).dt.tz_localize(None).dt.normalize()
        calendar = ExchangeSessionCalendar(
            str(calendar_name),
            start=rough_labels.min() - pd.Timedelta(days=14),
            end=rough_labels.max() + pd.Timedelta(days=future_padding_days),
        )
        local_labels = group[bar_timestamp_column].dt.tz_convert(
            calendar.exchange_timezone
        ).dt.tz_localize(None).dt.normalize()
        interval_lookup = {
            (item.start_timestamp, item.end_timestamp): item.exchange_session
            for item in calendar.eligible_hour_intervals(
                start_session=local_labels.min() - pd.Timedelta(days=7),
                end_session=local_labels.max() + pd.Timedelta(days=7),
            )
        }

        part = group.copy()
        resolved_sessions: list[pd.Timestamp | None] = []
        eligible: list[bool] = []
        for start, end in zip(
            part[bar_timestamp_column],
            part[bar_end_column],
            strict=True,
        ):
            session = interval_lookup.get((pd.Timestamp(start), pd.Timestamp(end)))
            resolved_sessions.append(session)
            eligible.append(session is not None)
        part["intraday_interval_eligible"] = eligible
        part["exchange_session"] = resolved_sessions
        part["bar_end_timestamp"] = part[bar_end_column]
        part["decision_timestamp"] = (
            part["bar_end_timestamp"] + processing_delay
        )
        part["session_open_timestamp"] = [
            calendar.session_open(session) if session is not None else pd.NaT
            for session in resolved_sessions
        ]
        part["session_close_timestamp"] = [
            calendar.session_close(session) if session is not None else pd.NaT
            for session in resolved_sessions
        ]
        part["exchange_calendar_name"] = calendar.exchange_calendar_name
        part["exchange_calendar_version"] = calendar.exchange_calendar_version
        part["exchange_timezone"] = calendar.exchange_timezone
        part["session_policy_version"] = calendar.session_policy_version
        part["daily_bar_label_policy_version"] = (
            calendar.daily_bar_label_policy_version
        )
        parts.append(part)

    result = pd.concat(parts, ignore_index=True, sort=False)
    result = result.sort_values("__calendar_order").drop(columns="__calendar_order")
    return result.reset_index(drop=True)


def calendar_for_horizon(
    exchange_calendar: str,
    *,
    minimum_session: object,
    maximum_session: object,
    future_padding_days: int = 45,
) -> ExchangeSessionCalendar:
    start = _session_label(minimum_session) - pd.Timedelta(days=14)
    end = _session_label(maximum_session) + pd.Timedelta(days=future_padding_days)
    return ExchangeSessionCalendar(exchange_calendar, start=start, end=end)


def _exchange_calendars_module() -> Any:
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise RuntimeError(
            "exchange-calendars is required for ML calendar resolution; "
            "install the project with the 'ml' optional dependencies."
        ) from exc
    return xcals


def _exchange_calendars_version() -> str:
    try:
        return version("exchange-calendars")
    except PackageNotFoundError:
        return "unknown"


def _session_label(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("UTC").tz_localize(None)
    return timestamp.normalize()


def _utc_timestamp(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _optional_utc_timestamp(value: object) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    return _utc_timestamp(value)


def _ceil_exchange_hour(
    value: object,
    *,
    timezone_name: str,
) -> pd.Timestamp:
    timestamp = _utc_timestamp(value)
    local = timestamp.tz_convert(timezone_name)
    floored = local.floor("h")
    if local != floored:
        floored += pd.Timedelta(hours=1)
    return floored.tz_convert("UTC")


def _exchange_week_key(value: object) -> pd.Timestamp:
    label = _session_label(value)
    return (label - pd.Timedelta(days=int(label.weekday()))).normalize()
