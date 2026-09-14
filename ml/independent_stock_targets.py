"""Versioned stock targets derived from observed extended-session equity prices.

Exchange sessions choose eligible dates. The stock action clock is 04:00-17:00
Pacific, independently of the exchange's regular-session opening/closing clock.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Sequence
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from ml.stock_target_prices import CANONICAL_STOCK_PRICE_SOURCE, stock_price_dataset


STOCK_TARGET_CONTRACT_VERSION = "independent-stock-targets-v1"
STOCK_TARGET_BOUNDARY_TOLERANCE = pd.Timedelta(minutes=5)
STOCK_CALENDAR_FEATURE_CONTRACT = "independent-stock-known-calendar-inputs-v1"
STOCK_CALENDAR_FEATURE_NAMES = (
    "target__entry_clock_sin", "target__entry_clock_cos",
    "target__log_elapsed_minutes", "target__weekday_sin", "target__weekday_cos",
    "target__trading_hours", "target__closed_market_hours",
)
STOCK_TIMEZONE = ZoneInfo("America/Los_Angeles")
GROUPS = ("1h", "4h", "1d", "1w")


def stock_target_calendar_features(window: dict) -> dict[str, float]:
    """Known exposure geometry only: never read prices, outcomes, or market inputs."""
    start = pd.Timestamp(window["target_window_start"])
    end = pd.Timestamp(window["target_window_end"])
    if pd.isna(start) or pd.isna(end) or start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Stock calendar features require timezone-aware target boundaries")
    elapsed_hours = (end - start).total_seconds() / 3600
    trading_hours = float(window["trading_hours"])
    if not np.isfinite(trading_hours) or elapsed_hours <= 0 or not 0 <= trading_hours <= elapsed_hours:
        raise ValueError("Stock calendar features have invalid declared exposure hours")
    local = start.tz_convert(STOCK_TIMEZONE)
    clock_angle = 2 * np.pi * (local.hour * 60 + local.minute) / (24 * 60)
    weekday_angle = 2 * np.pi * local.weekday() / 7
    values = (np.sin(clock_angle), np.cos(clock_angle), np.log1p(elapsed_hours * 60),
              np.sin(weekday_angle), np.cos(weekday_angle), trading_hours,
              elapsed_hours - trading_hours)
    return dict(zip(STOCK_CALENDAR_FEATURE_NAMES, map(float, values)))


def with_stock_calendar_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Recompute from declared windows so training and current rows share one map."""
    result = frame.copy()
    values = pd.DataFrame([stock_target_calendar_features(row) for row in frame.to_dict("records")],
                          index=frame.index, columns=STOCK_CALENDAR_FEATURE_NAMES)
    result[list(STOCK_CALENDAR_FEATURE_NAMES)] = values
    result["target_calendar_feature_contract"] = STOCK_CALENDAR_FEATURE_CONTRACT
    return result


def _clock(day: date, hour: int) -> pd.Timestamp:
    return pd.Timestamp(year=day.year, month=day.month, day=day.day,
                        hour=hour, tz=STOCK_TIMEZONE).tz_convert("UTC")


def _calendar(first: date, last: date):
    return xcals.get_calendar("XNYS", start=pd.Timestamp(first) - pd.Timedelta(days=10),
                              end=pd.Timestamp(last) + pd.Timedelta(days=24))


def stock_target_windows(action_date: date, *, calendar=None) -> tuple[dict, ...]:
    """Return the exact 24-row contract, including explicit non-entry research rows."""
    day = pd.Timestamp(action_date).date()
    calendar = calendar if calendar is not None else _calendar(day, day)
    label = pd.Timestamp(day)
    if not calendar.is_session(label):
        raise ValueError(f"Independent stock action date is not an XNYS session: {day}")
    dates = [pd.Timestamp(value).date() for value in calendar.sessions_window(label, 5)]
    previous = pd.Timestamp(calendar.previous_session(label)).date()
    rows = []

    def add(group, route, start, end, role, segments, semantics):
        rows.append({
            "model_group": group, "route": route,
            "forecast_anchor_local": "Opening gap (research)" if role == "OPENING_GAP_RESEARCH" else route.split("@", 1)[1],
            "target_role": role, "execution_eligible": role == "EXECUTION",
            "target_window_start": start, "target_window_end": end,
            "target_semantics": semantics,
            "target_contract_version": STOCK_TARGET_CONTRACT_VERSION,
            "trading_hours": sum((right - left).total_seconds() / 3600 for left, right in segments),
            "trading_segments_json": json.dumps([[left.isoformat(), right.isoformat()] for left, right in segments]),
        })

    for hour in range(4, 17):
        start, end = _clock(day, hour), _clock(day, hour + 1)
        add("1h", f"1h@{hour:02d}:00", start, end, "EXECUTION", [(start, end)],
            "forward_one_hour_stock_action_return")
    add("1h", "1h@gap", _clock(previous, 17), _clock(day, 4), "OPENING_GAP_RESEARCH", [],
        "prior_17_close_to_04_open_gap_research_not_an_entry")
    for hour in (4, 8, 12, 16):
        start = _clock(day, hour)
        end = _clock(day, hour + 4) if hour < 16 else _clock(dates[1], 7)
        segments = [(start, end)] if hour < 16 else [(start, _clock(day, 17)), (_clock(dates[1], 4), end)]
        add("4h", f"4h@{hour:02d}:00", start, end, "EXECUTION", segments,
            "entry_to_exit_return_over_four_action_hours_including_any_overnight_exposure")
    for offset, session in enumerate(dates, 1):
        start, end = _clock(session, 4), _clock(session, 17)
        add("1d", f"1d@D+{offset}", start, end, "EXECUTION" if offset == 1 else "OUTLOOK",
            [(start, end)], "extended_04_to_17_stock_session_return")
    start, end = _clock(dates[0], 4), _clock(dates[-1], 17)
    add("1w", "1w@D+5", start, end, "EXECUTION",
        [(_clock(session, 4), _clock(session, 17)) for session in dates],
        "first_session_04_to_fifth_session_17_stock_return_including_overnight_exposure")
    return tuple(rows)


def build_stock_current_groups(
    sources: pd.DataFrame, *, feature_columns: Sequence[str],
    price_source_contract: str = CANONICAL_STOCK_PRICE_SOURCE,
) -> dict[str, pd.DataFrame]:
    if sources.empty:
        raise RuntimeError("Independent stock targets require causal source rows")
    from ml.gameplan_source_selection import SOURCE_SELECTION_COLUMNS, source_selection_contract
    source_selection_contract(sources)
    dates = [pd.Timestamp(value).date() for value in sources["action_date"]]
    calendar = _calendar(min(dates), max(dates))
    windows = {day: stock_target_windows(day, calendar=calendar) for day in set(dates)}
    rows = []
    for source in sources.to_dict("records"):
        day = pd.Timestamp(source["action_date"]).date()
        decision = pd.to_datetime(source["decision_timestamp"], utc=True)
        information = pd.to_datetime(source["information_available_at"], utc=True)
        if pd.isna(decision) or pd.isna(information) or max(decision, information) >= _clock(day, 4):
            raise RuntimeError("Independent stock features must be available before the action session")
        base = {key: source.get(key) for key in feature_columns}
        base.update({key: source[key] for key in SOURCE_SELECTION_COLUMNS if key in source})
        base.update(symbol=str(source["symbol"]).upper(), action_date=day,
                    decision_timestamp=decision, information_available_at=information,
                    assumed_round_trip_cost=source.get("assumed_round_trip_cost", 0.001),
                    target_price_source_contract=price_source_contract,
                    target_price_dataset=stock_price_dataset(price_source_contract))
        rows.extend({**base, **window} for window in windows[day])
    frame = with_stock_calendar_features(pd.DataFrame(rows))
    return {group: frame.loc[frame.model_group.eq(group)].reset_index(drop=True) for group in GROUPS}


def build_stock_training_groups(sources: pd.DataFrame, *, feature_columns: Sequence[str],
                                minute_bars: pd.DataFrame, available_at: object,
                                price_source_contract: str | None = None) -> dict[str, pd.DataFrame]:
    """Use the native five-minute boundary policy and genuine equity observations."""
    source_report = minute_bars.attrs.get("stock_price_source", {})
    selected_source = price_source_contract or source_report.get("source_contract", CANONICAL_STOCK_PRICE_SOURCE)
    if source_report and source_report.get("source_contract") != selected_source:
        raise RuntimeError("Independent target labels disagree with the selected minute price source")
    groups = build_stock_current_groups(sources, feature_columns=feature_columns,
                                        price_source_contract=selected_source)
    now = pd.to_datetime(available_at, utc=True)
    bars = minute_bars.loc[:, ["symbol", "timestamp", "open", "close"]].copy()
    bars["symbol"] = bars.symbol.astype("string").str.upper()
    bars["timestamp"] = pd.to_datetime(bars.timestamp, utc=True, errors="coerce")
    bars = bars.loc[bars.timestamp.notna() & bars.timestamp.add(pd.Timedelta(minutes=1)).le(now)]
    bars = bars.drop_duplicates(["symbol", "timestamp", "open", "close"])
    # Conflicting canonical observations are unavailable, never chosen arbitrarily.
    conflicts = bars.duplicated(["symbol", "timestamp"], keep=False)
    bars = bars.loc[~conflicts]
    by_symbol = {str(symbol): frame.sort_values("timestamp", kind="stable")
                 for symbol, frame in bars.groupby("symbol", sort=False)}
    output = {}
    for group, targets in groups.items():
        targets = targets.loc[targets.target_window_end.le(now)].copy()
        observations = _target_observations(targets, by_symbol=by_symbol)
        entry_prices, exit_prices = observations["entry_price"], observations["exit_price"]
        start_gap = (observations.observed_open_timestamp - targets.target_window_start).abs()
        end_gap = (observations.observed_close_timestamp - targets.target_window_end).abs()
        valid = (np.isfinite(entry_prices) & np.isfinite(exit_prices)
                 & entry_prices.gt(0) & exit_prices.gt(0)
                 & start_gap.le(STOCK_TARGET_BOUNDARY_TOLERANCE)
                 & end_gap.le(STOCK_TARGET_BOUNDARY_TOLERANCE)
                 & observations.observed_open_timestamp.lt(observations.observed_close_timestamp))
        admitted = targets.loc[valid].copy()
        admitted["observed_return"] = exit_prices.loc[valid] / entry_prices.loc[valid] - 1.0
        cost = pd.to_numeric(admitted.assumed_round_trip_cost, errors="coerce").fillna(0.001)
        admitted["target"] = admitted.observed_return.sub(cost).gt(0).astype(int)
        admitted["target_open"] = entry_prices.loc[valid]
        admitted["target_close"] = exit_prices.loc[valid]
        admitted["observed_open_timestamp"] = observations.loc[valid, "observed_open_timestamp"]
        admitted["observed_close_timestamp"] = observations.loc[valid, "observed_close_timestamp"]
        admitted["target_boundary_aligned"] = True
        admitted["target_start_gap_seconds"] = start_gap.loc[valid].dt.total_seconds()
        admitted["target_end_gap_seconds"] = end_gap.loc[valid].dt.total_seconds()
        admitted = admitted.sort_values(["decision_timestamp", "symbol", "route"], kind="stable").reset_index(drop=True)
        admitted.attrs["target_boundary_quality"] = {
            "policy": "clock-window-boundaries-within-five-minutes-v1", "enforced": True,
            "maximum_boundary_gap_seconds": STOCK_TARGET_BOUNDARY_TOLERANCE.total_seconds(), "candidate_rows": len(targets),
            "aligned_rows": int(valid.sum()), "excluded_rows": int((~valid).sum()),
            "excluded_rows_by_route": {str(key): int(value) for key, value in targets.loc[~valid].groupby("route").size().items()},
            "admitted_rows_by_symbol": {str(key): int(value) for key, value in admitted.groupby("symbol").size().items()},
            "admitted_rows_by_route": {str(key): int(value) for key, value in admitted.groupby("route").size().items()},
            "conflicting_minute_rows_excluded": int(conflicts.sum()),
            "target_contract_version": STOCK_TARGET_CONTRACT_VERSION,
            "target_price_source_contract": selected_source,
            "target_price_dataset": stock_price_dataset(selected_source),
        }
        output[group] = admitted
    return output


def _target_observations(targets: pd.DataFrame, *, by_symbol: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Find directional boundary observations, never fill prices across missing bars.

    Forward returns start inside their window and end inside their window. The
    explicitly non-entry opening-gap row retains the native prior-close/current-
    open observation convention. The caller enforces the same five-minute bound
    for both conventions, and bars have already been checked for availability.
    """
    result = pd.DataFrame(index=targets.index)
    result["entry_price"] = np.nan
    result["exit_price"] = np.nan
    for column in ("observed_open_timestamp", "observed_close_timestamp"):
        result[column] = pd.Series(pd.NaT, index=targets.index, dtype="datetime64[ns, UTC]")
    for symbol, selected in targets.groupby("symbol", sort=False):
        bars = by_symbol.get(str(symbol))
        if bars is None or bars.empty:
            continue
        timestamps = pd.DatetimeIndex(bars.timestamp)
        gap = selected.target_role.eq("OPENING_GAP_RESEARCH").to_numpy()
        entry_offset = pd.to_timedelta(gap.astype(int), unit="m")
        exit_offset = pd.to_timedelta((~gap).astype(int), unit="m")
        entry_clock = pd.DatetimeIndex(selected.target_window_start) - entry_offset
        exit_clock = pd.DatetimeIndex(selected.target_window_end) - exit_offset
        entry_position = np.where(gap, timestamps.searchsorted(entry_clock, side="right") - 1,
                                  timestamps.searchsorted(entry_clock, side="left"))
        exit_position = np.where(gap, timestamps.searchsorted(exit_clock, side="left"),
                                 timestamps.searchsorted(exit_clock, side="right") - 1)
        exists = ((entry_position >= 0) & (entry_position < len(bars))
                  & (exit_position >= 0) & (exit_position < len(bars)))
        if not exists.any():
            continue
        index = selected.index[exists]
        entry = bars.iloc[entry_position[exists]]
        exit_rows = bars.iloc[exit_position[exists]]
        result.loc[index, "entry_price"] = np.where(gap[exists], entry["close"], entry["open"])
        result.loc[index, "exit_price"] = np.where(gap[exists], exit_rows["open"], exit_rows["close"])
        result.loc[index, "observed_open_timestamp"] = pd.DatetimeIndex(entry.timestamp) + entry_offset[exists]
        result.loc[index, "observed_close_timestamp"] = pd.DatetimeIndex(exit_rows.timestamp) + exit_offset[exists]
    return result
