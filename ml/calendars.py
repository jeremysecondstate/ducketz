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

# Intraday source and target clocks are deliberately separate. Databento's
# standard US-equity context runs from 04:00 through 20:00 Eastern, while the
# broker-actionable stock windows begin at 07:00 and pause for Schwab's
# five-minute AM/core and core/PM transitions. ``REGULAR`` retains its exact
# exchange meaning for options, Pricing, and every non-US calendar.
REGULAR_INTRADAY_SOURCE_POLICY = "regular-session-source-v1"
US_EQUITY_EXTENDED_SOURCE_POLICY = (
    "us-equity-standard-extended-source-0400-2000-et-v1"
)
REGULAR_INTRADAY_TARGET_POLICY = "regular-session-target-v1"
US_EQUITY_ACTIONABLE_TARGET_POLICY = (
    "us-equity-actionable-target-0700-0925-0930-1600-1605-2000-et-v1"
)
HYBRID_TARGET_START_POLICY = (
    "segment-open-plus-full-local-clock-hour-start-v1"
)
FOUR_HOUR_CHECKPOINT_START_POLICY = (
    "four-hour-checkpoints-0730-1130-1530-1930-et-v1"
)

CHECKPOINT_SESSION_PRE = "PRE"
CHECKPOINT_SESSION_REGULAR = "REGULAR"
CHECKPOINT_SESSION_POST = "POST"
CHECKPOINT_SESSION_CLOSED = "CLOSED"
CHECKPOINT_SESSION_LABELS = (
    CHECKPOINT_SESSION_PRE,
    CHECKPOINT_SESSION_REGULAR,
    CHECKPOINT_SESSION_POST,
)

_US_EQUITY_CALENDARS = frozenset({"XNAS", "XNYS"})
_FOUR_HOUR_CHECKPOINT_OFFSETS = (
    pd.Timedelta(hours=7, minutes=30),
    pd.Timedelta(hours=11, minutes=30),
    pd.Timedelta(hours=15, minutes=30),
    pd.Timedelta(hours=19, minutes=30),
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
        self._target_start_candidates_cache: dict[
            tuple[str, str], tuple[pd.Timestamp, ...]
        ] = {}
        self._eligible_minute_timestamps_cache: dict[
            str, tuple[pd.Timestamp, ...]
        ] = {}
        self._eligible_minute_positions_cache: dict[
            str, dict[pd.Timestamp, int]
        ] = {}

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
        session_policy: str = REGULAR_INTRADAY_TARGET_POLICY,
        start_policy: str = HYBRID_TARGET_START_POLICY,
    ) -> tuple[pd.Timestamp, ...]:
        """Return versioned starts for one intraday target policy.

        The ordinary hybrid policy contributes each continuous segment start
        plus every complete exchange-local clock hour wholly inside it. The
        four-hour stock policy contributes the explicit 07:30, 11:30, 15:30,
        and 19:30 Eastern checkpoints when they lie inside an eligible segment.
        Non-US calendars always retain their regular-session hybrid behavior.
        """

        clean_session_policy = _intraday_target_policy(session_policy)
        clean_start_policy = _intraday_start_policy(start_policy)
        if self.exchange_calendar not in _US_EQUITY_CALENDARS:
            clean_session_policy = REGULAR_INTRADAY_TARGET_POLICY
            clean_start_policy = HYBRID_TARGET_START_POLICY
        use_cache = start_session is None and end_session is None
        cache_key = (clean_session_policy, clean_start_policy)
        if use_cache and cache_key in self._target_start_candidates_cache:
            return self._target_start_candidates_cache[cache_key]

        candidates: set[pd.Timestamp] = set()
        segments = self._intraday_target_segments(
            start_session=start_session,
            end_session=end_session,
            session_policy=clean_session_policy,
        )
        if clean_start_policy == FOUR_HOUR_CHECKPOINT_START_POLICY:
            by_session: dict[
                pd.Timestamp, list[tuple[pd.Timestamp, pd.Timestamp]]
            ] = {}
            for session, segment_start, segment_end, _label in segments:
                by_session.setdefault(session, []).append(
                    (segment_start, segment_end)
                )
            for session, session_segments in by_session.items():
                local_midnight = session.tz_localize(self.exchange_timezone)
                for offset in _FOUR_HOUR_CHECKPOINT_OFFSETS:
                    candidate = (local_midnight + offset).tz_convert("UTC")
                    if any(
                        segment_start <= candidate < segment_end
                        for segment_start, segment_end in session_segments
                    ):
                        candidates.add(candidate)
        else:
            for _session, segment_start, segment_end, _label in segments:
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
            self._target_start_candidates_cache[cache_key] = result
        return result

    def eligible_minute_timestamps(
        self,
        *,
        start_session: object | None = None,
        end_session: object | None = None,
        session_policy: str = REGULAR_INTRADAY_TARGET_POLICY,
    ) -> tuple[pd.Timestamp, ...]:
        """Return exact interval-open timestamps for the target session policy."""

        clean_policy = _intraday_target_policy(session_policy)
        if self.exchange_calendar not in _US_EQUITY_CALENDARS:
            clean_policy = REGULAR_INTRADAY_TARGET_POLICY
        use_cache = start_session is None and end_session is None
        if use_cache and clean_policy in self._eligible_minute_timestamps_cache:
            return self._eligible_minute_timestamps_cache[clean_policy]

        timestamps: list[pd.Timestamp] = []
        minute = pd.Timedelta(minutes=1)
        for _session, segment_start, segment_end, _label in self._intraday_target_segments(
            start_session=start_session,
            end_session=end_session,
            session_policy=clean_policy,
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
            self._eligible_minute_timestamps_cache[clean_policy] = result
        return result

    def target_window_after(
        self,
        information_available_at: object,
        *,
        eligible_minute_count: int,
        session_policy: str = REGULAR_INTRADAY_TARGET_POLICY,
        start_policy: str = HYBRID_TARGET_START_POLICY,
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
        clean_session_policy = _intraday_target_policy(session_policy)
        if self.exchange_calendar not in _US_EQUITY_CALENDARS:
            clean_session_policy = REGULAR_INTRADAY_TARGET_POLICY
            start_policy = HYBRID_TARGET_START_POLICY
        clean_start_policy = _intraday_start_policy(start_policy)
        candidates = self.target_start_candidates(
            session_policy=clean_session_policy,
            start_policy=clean_start_policy,
        )
        location = bisect_right(candidates, available)
        if location >= len(candidates):
            raise MLContractError(
                "Exchange calendar padding did not cover a target start "
                "strictly after information availability"
            )
        target_start = candidates[location]
        eligible_minutes = self.eligible_minute_timestamps(
            session_policy=clean_session_policy
        )
        if clean_session_policy not in self._eligible_minute_positions_cache:
            self._eligible_minute_positions_cache[clean_session_policy] = {
                timestamp: index
                for index, timestamp in enumerate(eligible_minutes)
            }
        try:
            minute_location = self._eligible_minute_positions_cache[
                clean_session_policy
            ][target_start]
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

    def _intraday_target_segments(
        self,
        *,
        start_session: object | None = None,
        end_session: object | None = None,
        session_policy: str,
    ) -> tuple[
        tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp, str], ...
    ]:
        clean_policy = _intraday_target_policy(session_policy)
        regular = tuple(
            (session, start, end, CHECKPOINT_SESSION_REGULAR)
            for session, start, end in self._regular_segments(
                start_session=start_session,
                end_session=end_session,
            )
        )
        if (
            clean_policy == REGULAR_INTRADAY_TARGET_POLICY
            or self.exchange_calendar not in _US_EQUITY_CALENDARS
        ):
            return regular

        records = list(regular)
        sessions = self.sessions
        if start_session is not None:
            sessions = sessions[sessions >= _session_label(start_session)]
        if end_session is not None:
            sessions = sessions[sessions <= _session_label(end_session)]
        for session in sessions:
            if not self._is_standard_us_equity_session(session):
                # The broker does not promise ordinary extended sessions on
                # exchange early-close days. Retain only the official core.
                continue
            local_midnight = pd.Timestamp(session).tz_localize(
                self.exchange_timezone
            )
            records.extend(
                (
                    (
                        pd.Timestamp(session),
                        (local_midnight + pd.Timedelta(hours=7)).tz_convert("UTC"),
                        (
                            local_midnight
                            + pd.Timedelta(hours=9, minutes=25)
                        ).tz_convert("UTC"),
                        CHECKPOINT_SESSION_PRE,
                    ),
                    (
                        pd.Timestamp(session),
                        (
                            local_midnight
                            + pd.Timedelta(hours=16, minutes=5)
                        ).tz_convert("UTC"),
                        (local_midnight + pd.Timedelta(hours=20)).tz_convert("UTC"),
                        CHECKPOINT_SESSION_POST,
                    ),
                )
            )
        return tuple(sorted(records, key=lambda item: item[1]))

    def _is_standard_us_equity_session(self, session: object) -> bool:
        if self.exchange_calendar not in _US_EQUITY_CALENDARS:
            return False
        opened = self.session_open(session).tz_convert(self.exchange_timezone)
        closed = self.session_close(session).tz_convert(self.exchange_timezone)
        return (
            opened.hour,
            opened.minute,
            closed.hour,
            closed.minute,
        ) == (9, 30, 16, 0)

    def checkpoint_session_at(
        self,
        value: object,
        *,
        session_policy: str = US_EQUITY_ACTIONABLE_TARGET_POLICY,
    ) -> str:
        """Classify one instant as PRE, REGULAR, POST, or CLOSED."""

        timestamp = _utc_timestamp(value)
        local_session = (
            timestamp.tz_convert(self.exchange_timezone)
            .tz_localize(None)
            .normalize()
        )
        if local_session not in self.sessions:
            return CHECKPOINT_SESSION_CLOSED
        for _session, start, end, label in self._intraday_target_segments(
            start_session=local_session,
            end_session=local_session,
            session_policy=session_policy,
        ):
            if start <= timestamp < end:
                return label
        return CHECKPOINT_SESSION_CLOSED

    def intraday_target_bounds(
        self,
        session: object,
        *,
        session_policy: str = REGULAR_INTRADAY_TARGET_POLICY,
    ) -> tuple[pd.Timestamp, pd.Timestamp]:
        """Return the first open and final close for one target policy day."""

        label = self._require_session(session)
        segments = self._intraday_target_segments(
            start_session=label,
            end_session=label,
            session_policy=session_policy,
        )
        if not segments:  # pragma: no cover - exchange-calendar invariant
            raise MLContractError(
                f"No intraday target segments exist for {label.date()}."
            )
        return min(item[1] for item in segments), max(item[2] for item in segments)

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
    include_extended_hours: bool | None = None,
    source_session_policy: str | None = None,
) -> pd.DataFrame:
    """Attach exchange sessions to eligible full-hour decision intervals.

    Regular-market intervals are always eligible. Under the explicit US-equity
    extended source policy, completed full hours wholly inside 04:00--09:30 or
    the official close--20:00 exchange-local context are eligible too. Source
    rows are labeled ``PRE``, ``REGULAR``, or ``POST``. Intervals crossing a
    boundary or extending beyond the bounds remain excluded.
    """

    if processing_delay < pd.Timedelta(0):
        raise ValueError("processing_delay cannot be negative")
    if source_session_policy is None:
        clean_source_policy = (
            US_EQUITY_EXTENDED_SOURCE_POLICY
            if bool(include_extended_hours)
            else REGULAR_INTRADAY_SOURCE_POLICY
        )
    else:
        clean_source_policy = _intraday_source_policy(source_session_policy)
        if include_extended_hours is not None and bool(include_extended_hours) != (
            clean_source_policy == US_EQUITY_EXTENDED_SOURCE_POLICY
        ):
            raise ValueError(
                "include_extended_hours conflicts with source_session_policy"
            )
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
            (item.start_timestamp, item.end_timestamp): (
                item.exchange_session,
                CHECKPOINT_SESSION_REGULAR,
            )
            for item in calendar.eligible_hour_intervals(
                start_session=local_labels.min() - pd.Timedelta(days=7),
                end_session=local_labels.max() + pd.Timedelta(days=7),
            )
        }

        part = group.copy()
        resolved_sessions: list[pd.Timestamp | None] = []
        checkpoint_sessions: list[str] = []
        eligible: list[bool] = []
        for start, end in zip(
            part[bar_timestamp_column],
            part[bar_end_column],
            strict=True,
        ):
            start_timestamp = pd.Timestamp(start)
            end_timestamp = pd.Timestamp(end)
            resolved = interval_lookup.get((start_timestamp, end_timestamp))
            session = resolved[0] if resolved is not None else None
            checkpoint_session = (
                resolved[1] if resolved is not None else CHECKPOINT_SESSION_CLOSED
            )
            if (
                session is None
                and clean_source_policy == US_EQUITY_EXTENDED_SOURCE_POLICY
                and str(calendar_name).upper() in _US_EQUITY_CALENDARS
                and end_timestamp - start_timestamp == pd.Timedelta(hours=1)
            ):
                local_start = start_timestamp.tz_convert(
                    calendar.exchange_timezone
                )
                session_candidate = local_start.tz_localize(None).normalize()
                if (
                    session_candidate in calendar.sessions
                    and calendar._is_standard_us_equity_session(session_candidate)
                ):
                    official_open = calendar.session_open(session_candidate)
                    official_close = calendar.session_close(session_candidate)
                    local_midnight = session_candidate.tz_localize(
                        calendar.exchange_timezone
                    )
                    extended_open = local_midnight + pd.Timedelta(hours=4)
                    extended_close = local_midnight + pd.Timedelta(hours=20)
                    is_premarket = (
                        start_timestamp >= extended_open
                        and end_timestamp <= official_open
                    )
                    is_aftermarket = (
                        start_timestamp >= official_close
                        and end_timestamp <= extended_close
                    )
                    if is_premarket or is_aftermarket:
                        session = session_candidate
                        checkpoint_session = (
                            CHECKPOINT_SESSION_PRE
                            if is_premarket
                            else CHECKPOINT_SESSION_POST
                        )
            resolved_sessions.append(session)
            checkpoint_sessions.append(checkpoint_session)
            eligible.append(session is not None)
        part["intraday_interval_eligible"] = eligible
        part["exchange_session"] = resolved_sessions
        part["checkpoint_session"] = checkpoint_sessions
        part["intraday_source_session_policy"] = clean_source_policy
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


def _intraday_source_policy(value: object) -> str:
    clean = str(value or "").strip().lower()
    allowed = {
        REGULAR_INTRADAY_SOURCE_POLICY,
        US_EQUITY_EXTENDED_SOURCE_POLICY,
    }
    if clean not in allowed:
        raise ValueError(
            "Unsupported intraday source session policy: "
            f"{value!r}; expected {', '.join(sorted(allowed))}."
        )
    return clean


def _intraday_target_policy(value: object) -> str:
    clean = str(value or "").strip().lower()
    allowed = {
        REGULAR_INTRADAY_TARGET_POLICY,
        US_EQUITY_ACTIONABLE_TARGET_POLICY,
    }
    if clean not in allowed:
        raise ValueError(
            "Unsupported intraday target session policy: "
            f"{value!r}; expected {', '.join(sorted(allowed))}."
        )
    return clean


def _intraday_start_policy(value: object) -> str:
    clean = str(value or "").strip().lower()
    allowed = {
        HYBRID_TARGET_START_POLICY,
        FOUR_HOUR_CHECKPOINT_START_POLICY,
    }
    if clean not in allowed:
        raise ValueError(
            "Unsupported intraday target start policy: "
            f"{value!r}; expected {', '.join(sorted(allowed))}."
        )
    return clean


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
