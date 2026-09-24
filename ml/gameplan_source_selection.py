"""Causal prior-session features for the independent stock Gameplan.

Rolling-model target clocks and labels do not decide whether a source feature
can serve the next Gameplan. This contract applies prospectively; callers must
retain the legacy selector when reconstructing older published artifacts.
"""
from __future__ import annotations

from datetime import date
from typing import Sequence

import exchange_calendars as xcals
import pandas as pd


GAMEPLAN_SOURCE_SELECTION_VERSION = "independent-gameplan-prior-session-features-v1"
ARCHIVE_SOURCE_SELECTION_VERSION = "xnas-archive-prior-session-features-v1"
SOURCE_SELECTION_VERSIONS = frozenset((GAMEPLAN_SOURCE_SELECTION_VERSION, ARCHIVE_SOURCE_SELECTION_VERSION))
GAMEPLAN_SOURCE_TIMEZONE = "America/Los_Angeles"
SOURCE_SELECTION_COLUMNS = (
    "source_session", "source_selection_contract", "source_feature_cutoff",
    "source_effective_cutoff", "source_action_start", "source_regular_close",
    "source_bar_timestamp", "source_bar_end_timestamp",
)
_CLOCK_COLUMNS = (
    "bar_timestamp", "bar_end_timestamp", "information_available_at", "decision_timestamp",
)
_REQUIRED_COLUMNS = (
    "symbol", "horizon", "timeframe", "exchange_calendar", "exchange_session", *_CLOCK_COLUMNS,
)


def source_selection_contract(frame: pd.DataFrame) -> str | None:
    """Distinguish explicit prospective metadata from legacy, unversioned rows."""
    if "source_selection_contract" not in frame.columns:
        return None
    values = frame["source_selection_contract"]
    if values.isna().any() or len(values.unique()) != 1 or str(values.iloc[0]) not in SOURCE_SELECTION_VERSIONS:
        raise ValueError("Unsupported or inconsistent Gameplan source selection contract")
    return str(values.iloc[0])


def _clock(day: date, hour: int, minute: int = 0) -> pd.Timestamp:
    return pd.Timestamp(year=day.year, month=day.month, day=day.day, hour=hour,
                        minute=minute, tz=GAMEPLAN_SOURCE_TIMEZONE).tz_convert("UTC")


def _aware(value: object) -> bool:
    try:
        timestamp = pd.Timestamp(value)
        return not pd.isna(timestamp) and timestamp.tzinfo is not None
    except (TypeError, ValueError, OverflowError):
        return False


def _aware_series(values: pd.Series) -> pd.Series:
    """Parse UTC instants without silently assigning UTC to naive inputs."""
    parsed = pd.to_datetime(values, utc=True, errors="coerce")
    timezone = getattr(values.dtype, "tz", None)
    if timezone is None:
        timezone = getattr(getattr(values.dtype, "pyarrow_dtype", None), "tz", None)
    return parsed if timezone is not None else parsed.where(values.map(_aware))


def select_prior_session_sources(
    samples: pd.DataFrame,
    *,
    symbols: Sequence[str],
    available_at: object,
    feature_columns: Sequence[str],
) -> pd.DataFrame:
    """Select one completed, sufficiently recent feature row per symbol/session.

    A source bar must be a full hour on its declared XNAS/XNYS session, start
    after the regular open, and end between the actual regular close and 17:00
    Pacific. Thus the final regular-session bar is sufficient; an after-hours
    bar is not mandatory. Features must be known by the earlier of the run's
    as-of time and that source session's 17:05 Pacific cutoff. Each source maps
    only to the immediately following exchange session, never across a missing
    trading day. The original source clocks and rolling targets are preserved.

    Repeated rolling targets may carry identical source features. Collapse those
    copies, but fail on contradictory copies of the same eligible source bar.
    """
    missing = sorted(set((*_REQUIRED_COLUMNS, *feature_columns)) - set(samples.columns))
    if missing:
        raise ValueError(f"Gameplan source selection is missing columns: {missing}")
    if not _aware(available_at):
        raise ValueError("Gameplan source selection requires a timezone-aware as-of time")
    now = pd.Timestamp(available_at).tz_convert("UTC")
    requested = {str(symbol).strip().upper() for symbol in symbols}
    scoped = samples.loc[
        samples["horizon"].astype("string").eq("1h")
        & samples["timeframe"].astype("string").eq("1h")
        & samples["symbol"].astype("string").str.strip().str.upper().isin(requested)
    ].copy().reset_index(drop=True)
    if scoped.empty:
        raise RuntimeError("No independent Gameplan prior-session source rows are available")
    scoped["symbol"] = scoped["symbol"].astype("string").str.strip().str.upper()
    for column in _CLOCK_COLUMNS:
        scoped[column] = _aware_series(scoped[column])
    sessions = pd.to_datetime(scoped["exchange_session"], utc=True, errors="coerce")
    source_dates = scoped["bar_timestamp"].dt.tz_convert(GAMEPLAN_SOURCE_TIMEZONE).dt.date
    clock_valid = (
        scoped[list(_CLOCK_COLUMNS)].notna().all(axis=1)
        & sessions.notna() & sessions.eq(sessions.dt.normalize())
        & sessions.dt.date.eq(source_dates)
        & scoped["exchange_calendar"].astype("string").isin(("XNAS", "XNYS"))
        & scoped["bar_end_timestamp"].sub(scoped["bar_timestamp"]).eq(pd.Timedelta(hours=1))
        & scoped["information_available_at"].ge(scoped["bar_end_timestamp"])
        & scoped["decision_timestamp"].ge(scoped["information_available_at"])
    )
    valid = scoped.loc[clock_valid].copy()
    if valid.empty:
        raise RuntimeError("No independent Gameplan prior-session source rows are available")
    valid["source_session"] = source_dates.loc[valid.index]
    first, last = min(valid.source_session), max(valid.source_session)
    calendars = {
        name: xcals.get_calendar(name, start=pd.Timestamp(first) - pd.Timedelta(days=7),
                                 end=pd.Timestamp(last) + pd.Timedelta(days=14))
        for name in valid["exchange_calendar"].unique()
    }
    schedule = {}
    for name, day in valid[["exchange_calendar", "source_session"]].drop_duplicates().itertuples(index=False):
        calendar = calendars[name]
        label = pd.Timestamp(day)
        if not calendar.is_session(label):
            continue
        next_day = pd.Timestamp(calendar.next_session(label)).date()
        schedule[(name, day)] = (
            pd.Timestamp(calendar.session_open(label)), pd.Timestamp(calendar.session_close(label)),
            _clock(day, 17), _clock(day, 17, 5), next_day, _clock(next_day, 4),
        )
    metadata = pd.DataFrame([
        schedule.get((name, day), (pd.NaT, pd.NaT, pd.NaT, pd.NaT, None, pd.NaT))
        for name, day in zip(valid["exchange_calendar"], valid["source_session"])
    ], index=valid.index, columns=[
        "source_regular_open", "source_regular_close", "source_extended_close",
        "source_feature_cutoff", "action_date", "source_action_start",
    ])
    for column in metadata.columns:
        valid[column] = (metadata[column] if column == "action_date"
                         else pd.to_datetime(metadata[column], utc=True))
    eligible = valid.loc[
        valid["source_action_start"].notna()
        & valid["bar_timestamp"].ge(valid["source_regular_open"])
        & valid["bar_end_timestamp"].ge(valid["source_regular_close"])
        & valid["bar_end_timestamp"].le(valid["source_extended_close"])
        & valid["decision_timestamp"].le(valid["source_feature_cutoff"])
        & valid["decision_timestamp"].le(now)
        & valid["decision_timestamp"].lt(valid["source_action_start"])
    ].copy()
    if eligible.empty:
        raise RuntimeError("No independent Gameplan prior-session source rows are available")

    # A rolling sample ID or label may differ for repeated targets of one source
    # bar. Only source identity, causal clocks and model inputs define a copy.
    identity = [column for column in (
        "symbol", "venue", "currency", "provider", "timeframe", "exchange_calendar",
        "exchange_session", *_CLOCK_COLUMNS, "assumed_round_trip_cost", *feature_columns,
    ) if column in eligible.columns]
    copies = eligible.drop_duplicates(list(dict.fromkeys(identity)))
    conflict = copies.duplicated(["symbol", "bar_timestamp"], keep=False)
    if conflict.any():
        raise RuntimeError(
            "Conflicting independent Gameplan source features for "
            f"{int(conflict.sum())} copies of eligible symbol/bar observations"
        )
    result = (
        copies.sort_values(["symbol", "action_date", "bar_end_timestamp", "decision_timestamp"],
                           kind="stable")
        .groupby(["symbol", "action_date"], sort=False, as_index=False).tail(1)
        .reset_index(drop=True)
    )
    result["source_selection_contract"] = GAMEPLAN_SOURCE_SELECTION_VERSION
    result["source_effective_cutoff"] = result["source_feature_cutoff"].clip(upper=now)
    result["source_bar_timestamp"] = result["bar_timestamp"]
    result["source_bar_end_timestamp"] = result["bar_end_timestamp"]
    result.attrs["source_selection"] = {
        "source_selection_contract": GAMEPLAN_SOURCE_SELECTION_VERSION,
        "as_of": now.isoformat(),
        "calendar_policy": "immediately-next-declared-XNAS-or-XNYS-session",
        "freshness_policy": "completed-hour-ending-at-or-after-source-session-regular-close",
        "cutoff_policy": "min(run-as-of,source-session-17:05-America/Los_Angeles)",
        "source_rows": len(scoped),
        "eligible_expanded_rows": len(eligible),
        "unique_eligible_observations": len(copies),
        "selected_rows": len(result),
        "selected_rows_by_symbol": {
            str(symbol): int(count) for symbol, count in result.groupby("symbol").size().items()
        },
        "action_date_start": str(min(result.action_date)),
        "action_date_end": str(max(result.action_date)),
    }
    return result
