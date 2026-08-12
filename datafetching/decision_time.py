from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path

import pandas as pd

from datafetching.bar_schema import (
    legacy_bar_completion_mask,
    normalized_bar_file_sort_key,
    read_bar_timestamp_and_completion,
)
from datafetching.bar_timing import bar_end_timestamps
from datafetching.layout import safe_token

DECISION_SOURCE_PROVIDER = "databento"
DECISION_SOURCE_TIMEFRAME = "1m"
DECISION_BOUNDARY_MINUTES = 15
DECISION_EXCHANGE_CALENDAR = "XNYS"


class CycleTargetState(str, Enum):
    """Operational states for the shared Loop A/Pricing/Options target."""

    ACTIONABLE_EXACT_TARGET = "ACTIONABLE_EXACT_TARGET"
    MARKET_CLOSED_IDLE = "MARKET_CLOSED_IDLE"
    WAITING_FOR_LOOP_A_READINESS = "WAITING_FOR_LOOP_A_READINESS"
    READINESS_DEADLINE_MISSED = "READINESS_DEADLINE_MISSED"
    TARGET_ALREADY_OBSERVED = "TARGET_ALREADY_OBSERVED"


@dataclass(frozen=True)
class CycleTargetDecision:
    """One calendar-owned prospective collection decision.

    Eligible targets are completed quarter-hours strictly after the regular XNYS
    open and no later than the official close.  The strict-open rule makes the
    first target 09:45 America/New_York: a 09:30 boundary would describe a bar
    from outside the regular option-market evidence window.
    """

    observed_at: pd.Timestamp
    cycle_mode: str
    target_state: CycleTargetState
    target_snapshot_for: pd.Timestamp | None
    next_eligible_target: pd.Timestamp
    session_label: str | None
    session_open: pd.Timestamp | None
    session_close: pd.Timestamp | None
    reason: str

    @property
    def actionable(self) -> bool:
        return self.target_snapshot_for is not None and self.cycle_mode == "ACTIONABLE"

    def next_eligible_cycle(self, *, phase_offset_minutes: int = 0) -> pd.Timestamp:
        return self.next_eligible_target + pd.Timedelta(minutes=phase_offset_minutes)

    def with_runtime_state(
        self,
        *,
        readiness_available: bool | None = None,
        deadline_at: object | None = None,
        target_observed: bool = False,
        reason: str | None = None,
    ) -> "CycleTargetDecision":
        """Refine an actionable calendar decision without changing its target."""

        if not self.actionable:
            return self
        if target_observed:
            state = CycleTargetState.TARGET_ALREADY_OBSERVED
            detail = reason or "A verified Options receipt already owns this target."
        elif readiness_available is True:
            state = CycleTargetState.ACTIONABLE_EXACT_TARGET
            detail = reason or "Exact all-symbol Loop A readiness is authoritative."
        elif readiness_available is False:
            deadline = (
                _as_utc_timestamp(deadline_at) if deadline_at is not None else None
            )
            if deadline is not None and self.observed_at >= deadline:
                state = CycleTargetState.READINESS_DEADLINE_MISSED
                detail = reason or "The exact Loop A readiness deadline was missed."
            else:
                state = CycleTargetState.WAITING_FOR_LOOP_A_READINESS
                detail = reason or "Waiting for exact all-symbol Loop A readiness."
        else:
            state = CycleTargetState.ACTIONABLE_EXACT_TARGET
            detail = reason or self.reason
        return replace(
            self,
            cycle_mode="MONITOR_ONLY" if target_observed else self.cycle_mode,
            target_state=state,
            reason=detail,
        )


@dataclass(frozen=True)
class DecisionClock:
    """Completed market bar used as the point-in-time key for a fetched snapshot."""

    decision_timestamp: pd.Timestamp
    bar_timestamp: pd.Timestamp
    provider: str
    timeframe: str
    source_file: Path


def expected_quarter_hour_target(
    value: datetime | pd.Timestamp | None = None,
) -> pd.Timestamp:
    """Return the exact quarter-hour target owned by one scheduled cycle."""

    observed = _as_utc_timestamp(value)
    return observed.floor(f"{DECISION_BOUNDARY_MINUTES}min")


def latest_eligible_option_target(
    value: datetime | pd.Timestamp | None = None,
) -> pd.Timestamp:
    """Return the newest regular-session target available by ``value``.

    Pricing remains calendar-gated because it creates prospective market
    evidence.  The independent Schwab chain owner can nevertheless refresh a
    chain while the market is closed.  Those refreshes stay bound to the newest
    real option-market target rather than inventing an overnight target.
    """

    observed = _as_utc_timestamp(value)
    candidate = expected_quarter_hour_target(observed)
    calendar = _xnys_calendar(
        observed - pd.Timedelta(days=31),
        observed + pd.Timedelta(days=1),
    )
    latest: pd.Timestamp | None = None
    for session in calendar.sessions:
        opened = pd.Timestamp(calendar.session_open(session)).tz_convert("UTC")
        closed = pd.Timestamp(calendar.session_close(session)).tz_convert("UTC")
        upper = min(candidate, closed)
        target = upper.floor(f"{DECISION_BOUNDARY_MINUTES}min")
        if opened < target <= closed and target <= observed:
            latest = target if latest is None else max(latest, target)
    if latest is None:
        raise RuntimeError(
            "XNYS calendar horizon did not contain a prior eligible target"
        )
    return latest


def cycle_target_decision(
    value: datetime | pd.Timestamp | None = None,
    *,
    readiness_available: bool | None = None,
    deadline_at: object | None = None,
    target_observed: bool = False,
) -> CycleTargetDecision:
    """Return the shared calendar-aware target decision for one runtime cycle."""

    observed = _as_utc_timestamp(value)
    candidate = expected_quarter_hour_target(observed)
    calendar = _xnys_calendar(observed - pd.Timedelta(days=14), observed + pd.Timedelta(days=21))
    sessions = calendar.sessions
    for session in sessions:
        opened = pd.Timestamp(calendar.session_open(session)).tz_convert("UTC")
        closed = pd.Timestamp(calendar.session_close(session)).tz_convert("UTC")
        if opened < candidate <= closed:
            next_target = _next_eligible_target(calendar, candidate)
            decision = CycleTargetDecision(
                observed_at=observed,
                cycle_mode="ACTIONABLE",
                target_state=CycleTargetState.ACTIONABLE_EXACT_TARGET,
                target_snapshot_for=candidate,
                next_eligible_target=next_target,
                session_label=pd.Timestamp(session).strftime("%Y-%m-%d"),
                session_open=opened,
                session_close=closed,
                reason=(
                    "The exact completed quarter-hour is inside the regular XNYS "
                    "option-market evidence window."
                ),
            )
            return decision.with_runtime_state(
                readiness_available=readiness_available,
                deadline_at=deadline_at,
                target_observed=target_observed,
            )

    next_target, next_session, next_open, next_close = _first_target_after(
        calendar,
        observed,
    )
    before_first_target = any(
        pd.Timestamp(calendar.session_open(session)).tz_convert("UTC")
        <= observed
        < pd.Timestamp(calendar.session_open(session)).tz_convert("UTC")
        + pd.Timedelta(minutes=DECISION_BOUNDARY_MINUTES)
        for session in calendar.sessions
    )
    if before_first_target:
        reason = (
            "The XNYS regular session is open but has not reached its first "
            "completed eligible quarter-hour."
        )
    else:
        reason = (
            "The regular XNYS option-market evidence window is closed; "
            "extended-hours bars are monitor-only and cannot create a target."
        )
    return CycleTargetDecision(
        observed_at=observed,
        cycle_mode="MONITOR_ONLY",
        target_state=CycleTargetState.MARKET_CLOSED_IDLE,
        target_snapshot_for=None,
        next_eligible_target=next_target,
        session_label=pd.Timestamp(next_session).strftime("%Y-%m-%d"),
        session_open=next_open,
        session_close=next_close,
        reason=reason,
    )


def is_eligible_option_target(value: object) -> bool:
    """Return whether ``value`` is an exact supported regular-session target."""

    target = _as_utc_timestamp(value)
    decision = cycle_target_decision(target)
    return bool(decision.actionable and decision.target_snapshot_for == target)


def eligible_option_market_seconds(start: object, end: object) -> float:
    """Measure only supported prospective collection time between two clocks."""

    lower = _as_utc_timestamp(start)
    upper = _as_utc_timestamp(end)
    if upper <= lower:
        return 0.0
    calendar = _xnys_calendar(lower - pd.Timedelta(days=7), upper + pd.Timedelta(days=7))
    total = pd.Timedelta(0)
    for session in calendar.sessions:
        opened = pd.Timestamp(calendar.session_open(session)).tz_convert("UTC")
        closed = pd.Timestamp(calendar.session_close(session)).tz_convert("UTC")
        eligible_start = opened + pd.Timedelta(minutes=DECISION_BOUNDARY_MINUTES)
        overlap_start = max(lower, eligible_start)
        overlap_end = min(upper, closed)
        if overlap_end > overlap_start:
            total += overlap_end - overlap_start
    return float(total.total_seconds())


def _xnys_calendar(start: pd.Timestamp, end: pd.Timestamp):
    try:
        import exchange_calendars as xcals
    except ImportError as exc:  # pragma: no cover - required project dependency
        raise RuntimeError(
            "exchange-calendars is required for option cycle target selection"
        ) from exc
    return xcals.get_calendar(
        DECISION_EXCHANGE_CALENDAR,
        start=start.date().isoformat(),
        end=end.date().isoformat(),
    )


def _first_target_after(
    calendar: object,
    observed: pd.Timestamp,
) -> tuple[pd.Timestamp, object, pd.Timestamp, pd.Timestamp]:
    for session in calendar.sessions:  # type: ignore[attr-defined]
        opened = pd.Timestamp(calendar.session_open(session)).tz_convert("UTC")  # type: ignore[attr-defined]
        closed = pd.Timestamp(calendar.session_close(session)).tz_convert("UTC")  # type: ignore[attr-defined]
        target = opened + pd.Timedelta(minutes=DECISION_BOUNDARY_MINUTES)
        if target > observed and target <= closed:
            return target, session, opened, closed
    raise RuntimeError("XNYS calendar horizon did not contain a next eligible target")


def _next_eligible_target(calendar: object, target: pd.Timestamp) -> pd.Timestamp:
    candidate = target + pd.Timedelta(minutes=DECISION_BOUNDARY_MINUTES)
    for session in calendar.sessions:  # type: ignore[attr-defined]
        opened = pd.Timestamp(calendar.session_open(session)).tz_convert("UTC")  # type: ignore[attr-defined]
        closed = pd.Timestamp(calendar.session_close(session)).tz_convert("UTC")  # type: ignore[attr-defined]
        if opened < candidate <= closed:
            return candidate
        first = opened + pd.Timedelta(minutes=DECISION_BOUNDARY_MINUTES)
        if first > target:
            return first
    raise RuntimeError("XNYS calendar horizon did not contain a later eligible target")


def latest_completed_bar_clock(
    datastore_root: Path,
    *,
    symbol: str,
    as_of: datetime | pd.Timestamp | None = None,
) -> DecisionClock:
    """Return the newest qualifying Databento 1m bar available by ``as_of``.

    Duckets fetches option surfaces on a 15-minute cadence. The source of truth is the
    normalized Databento 1m Parquet, so ``decision_timestamp`` is the newest completed
    1m ``bar_end_timestamp`` that lands exactly on a wall-clock quarter-hour boundary
    (:00, :15, :30, or :45). Derived higher-timeframe Parquets are not consulted.
    """

    clean_symbol = symbol.strip().upper()
    if not clean_symbol:
        raise ValueError("Symbol is required.")

    observed_at = _as_utc_timestamp(as_of)
    normalized_root = (
        Path(datastore_root)
        / "stocks"
        / safe_token(clean_symbol)
        / "bars"
        / DECISION_SOURCE_TIMEFRAME
        / DECISION_SOURCE_PROVIDER
        / "normalized"
    )
    if not normalized_root.is_dir():
        raise FileNotFoundError(
            f"No normalized Databento 1m OHLCV folder exists for {clean_symbol}: "
            f"{normalized_root}"
        )

    paths = sorted(
        normalized_root.glob("*.parquet"),
        key=normalized_bar_file_sort_key,
    )
    if not paths:
        raise FileNotFoundError(
            f"No normalized Databento 1m OHLCV Parquet exists for {clean_symbol}: "
            f"{normalized_root}"
        )

    candidate = _latest_from_files(paths, observed_at=observed_at)
    if candidate is None:
        raise FileNotFoundError(
            f"No completed Databento 1m bar ending on a "
            f"{DECISION_BOUNDARY_MINUTES}-minute boundary was available for "
            f"{clean_symbol} by {observed_at.isoformat()}."
        )

    return candidate


def completed_bar_clock_for_target(
    datastore_root: Path,
    *,
    symbol: str,
    target_snapshot_for: datetime | pd.Timestamp,
    as_of: datetime | pd.Timestamp | None = None,
) -> DecisionClock:
    """Resolve exactly ``target_snapshot_for``; never substitute an older bar."""

    clean_symbol = symbol.strip().upper()
    if not clean_symbol:
        raise ValueError("Symbol is required.")
    target = _as_utc_timestamp(target_snapshot_for)
    if target != expected_quarter_hour_target(target):
        raise ValueError("Decision target must be an exact quarter-hour boundary")
    observed_at = _as_utc_timestamp(as_of)
    if target > observed_at:
        raise FileNotFoundError(
            f"Target {target.isoformat()} is not complete by {observed_at.isoformat()}."
        )
    normalized_root = (
        Path(datastore_root)
        / "stocks"
        / safe_token(clean_symbol)
        / "bars"
        / DECISION_SOURCE_TIMEFRAME
        / DECISION_SOURCE_PROVIDER
        / "normalized"
    )
    paths = (
        sorted(normalized_root.glob("*.parquet"), key=normalized_bar_file_sort_key)
        if normalized_root.is_dir()
        else []
    )
    candidate = _clock_for_exact_target(
        paths,
        observed_at=observed_at,
        target=target,
    )
    if candidate is None:
        raise FileNotFoundError(
            f"Exact completed Databento 1m target {target.isoformat()} was not "
            f"available for {clean_symbol} by {observed_at.isoformat()}."
        )
    return candidate


def completed_bar_close(clock: DecisionClock) -> float:
    """Read the one canonical close selected by a verified decision clock."""

    frame = pd.read_parquet(clock.source_file, columns=["timestamp", "close"])
    timestamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    selected = pd.to_numeric(
        frame.loc[timestamps.eq(pd.Timestamp(clock.bar_timestamp)), "close"],
        errors="coerce",
    ).dropna()
    if len(selected) != 1:
        raise ValueError("Canonical target boundary did not resolve exactly one close")
    value = float(selected.iloc[0])
    if not pd.notna(value) or value <= 0.0:
        raise ValueError("Canonical target close must be finite and positive")
    return value


def _latest_from_files(
    paths: list[Path],
    *,
    observed_at: pd.Timestamp,
) -> DecisionClock | None:
    frames: list[pd.DataFrame] = []
    for file_order, path in enumerate(paths):
        try:
            frame, _physical_schema = read_bar_timestamp_and_completion(path)
        except Exception as exc:
            raise RuntimeError(
                f"Could not read normalized bar parquet {path}: {exc}"
            ) from exc
        if frame.empty:
            continue
        frame["_source_file"] = str(path)
        frame["_file_order"] = file_order
        frames.append(frame)
    if not frames:
        return None

    frame = (
        pd.concat(frames, ignore_index=True, sort=False)
        .sort_values(["timestamp", "_file_order"], kind="stable")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )
    timestamps = frame["timestamp"]
    ends = bar_end_timestamps(timestamps, DECISION_SOURCE_TIMEFRAME)

    quarter_hour_boundary = (
        ends.notna()
        & ends.dt.second.eq(0)
        & ends.dt.microsecond.eq(0)
        & ends.dt.minute.mod(DECISION_BOUNDARY_MINUTES).eq(0)
    )
    complete = (
        ends.notna()
        & ends.le(observed_at)
        & quarter_hour_boundary
        & legacy_bar_completion_mask(frame)
    )
    valid = complete & timestamps.notna()
    if not valid.any():
        return None

    latest_index = ends.loc[valid].idxmax()
    return DecisionClock(
        decision_timestamp=pd.Timestamp(ends.loc[latest_index]).tz_convert("UTC"),
        bar_timestamp=pd.Timestamp(timestamps.loc[latest_index]).tz_convert("UTC"),
        provider=DECISION_SOURCE_PROVIDER,
        timeframe=DECISION_SOURCE_TIMEFRAME,
        source_file=Path(str(frame.loc[latest_index, "_source_file"])),
    )


def _clock_for_exact_target(
    paths: list[Path],
    *,
    observed_at: pd.Timestamp,
    target: pd.Timestamp,
) -> DecisionClock | None:
    if not paths:
        return None
    frames: list[pd.DataFrame] = []
    for file_order, path in enumerate(paths):
        try:
            frame, _physical_schema = read_bar_timestamp_and_completion(path)
        except Exception as exc:
            raise RuntimeError(
                f"Could not read normalized bar parquet {path}: {exc}"
            ) from exc
        if frame.empty:
            continue
        frame["_source_file"] = str(path)
        frame["_file_order"] = file_order
        frames.append(frame)
    if not frames:
        return None
    frame = (
        pd.concat(frames, ignore_index=True, sort=False)
        .sort_values(["timestamp", "_file_order"], kind="stable")
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )
    ends = bar_end_timestamps(frame["timestamp"], DECISION_SOURCE_TIMEFRAME)
    valid = (
        ends.eq(target)
        & ends.le(observed_at)
        & legacy_bar_completion_mask(frame)
        & frame["timestamp"].notna()
    )
    if valid.sum() != 1:
        return None
    index = valid.loc[valid].index[0]
    return DecisionClock(
        decision_timestamp=target,
        bar_timestamp=pd.Timestamp(frame.loc[index, "timestamp"]).tz_convert("UTC"),
        provider=DECISION_SOURCE_PROVIDER,
        timeframe=DECISION_SOURCE_TIMEFRAME,
        source_file=Path(str(frame.loc[index, "_source_file"])),
    )


def _as_utc_timestamp(value: datetime | pd.Timestamp | None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp.now(tz="UTC")
    parsed = pd.Timestamp(value)
    return parsed.tz_localize("UTC") if parsed.tzinfo is None else parsed.tz_convert("UTC")
