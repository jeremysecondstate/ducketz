"""Pure, research-only native OHLCV features; never registered for production.

Input timestamps identify native hourly interval opens or native UTC-day opens.
Native daily aggregates are not regular-session candles. This module does not
read data, write artifacts, fit models,
fill omitted bars, or publish anything.  Its fixed specification is deliberately
small; changing a window requires a new experiment and semantic version.
"""

from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pandas as pd

from ml.contracts import (
    IMPLEMENTED_BUT_QUARANTINED,
    NEEDS_NORMALIZATION,
    FeatureSpec,
)

SEMANTIC_VERSION = "native-ohlcv-research-v2"
RANGE_LOOKBACK = 20
RANGE_MIN_PERIODS = 10
PATH_LOOKBACK = 10
PROCESSING_DELAY = pd.Timedelta(minutes=5)
EXCHANGE_CALENDAR = "XNYS"
FEATURE_FAMILIES = {
    "activity": ("rx_activity__slot_range_log_ratio",),
    "path": (
        "rx_path__downside_variation_share_10",
        "rx_path__movement_concentration_10",
    ),
    "interaction": ("rx_interaction__return_persistence_10",),
}
FEATURE_COLUMNS = tuple(name for names in FEATURE_FAMILIES.values() for name in names)


def _feature_spec(name: str) -> FeatureSpec:
    family, column = name.split("__", 1)
    return FeatureSpec(
        name=name,
        source_family=family,
        source_column=column,
        applicable_horizons=("1h", "4h", "1d", "1w"),
        provider_policy="explicit-native-databento-ohlcv-only-v1",
        source_timeframe="native-hourly-or-daily-explicit-experiment-choice",
        source_grain="completed-native-bar",
        required_calculation_versions=(SEMANTIC_VERSION,),
        availability_rule="native-hour-or-utc-day-end-plus-five-minutes-no-later-than-decision",
        availability_rule_version="native-ohlcv-interval-end-v2",
        processing_delay_seconds=300,
        freshness_by_horizon=tuple(
            (horizon, "exact-source-bar-declared-in-research-membership")
            for horizon in ("1h", "4h", "1d", "1w")
        ),
        missing_policy="no-fill-no-derived-bars-fold-training-only-imputation-v1",
        transform_version="identity-v1",
        coverage_policy="report-sparse-native-coverage-no-admission-from-validation-v1",
        readiness_policy_version="isolated-research-only-v1",
        audit_classification=NEEDS_NORMALIZATION,
        activation_status=IMPLEMENTED_BUT_QUARANTINED,
    )


RESEARCH_FEATURE_SPECS = tuple(_feature_spec(name) for name in FEATURE_COLUMNS)


def semantic_contract() -> dict[str, object]:
    """Return the complete portable specification, including formulas and limits."""
    return {
        "semantic_version": SEMANTIC_VERSION,
        "production_registered": False,
        "calendar": EXCHANGE_CALENDAR,
        "range_lookback_sessions": RANGE_LOOKBACK,
        "range_minimum_native_comparators": RANGE_MIN_PERIODS,
        "path_lookback_returns": PATH_LOOKBACK,
        "processing_delay_seconds": int(PROCESSING_DELAY.total_seconds()),
        "availability": "hourly interval end or native daily UTC-midnight start plus 24 hours, plus 5 minutes",
        "session_policy": (
            "Hourly range: same New York clock slot, session composition and regular-session "
            "duration among the previous 20 scheduled sessions; full 04:00-20:00 hours on "
            "normal sessions, only wholly regular hours on shortened sessions. Native daily "
            "range: UTC-date aggregates whose labels are scheduled XNYS session dates, "
            "compared with the same scheduled regular-session duration, UTC opening minute "
            "and previous-calendar-date session presence among the previous 20 scheduled "
            "sessions. These conservative keys distinguish DST/extended-session composition. "
            "Daily input includes all native UTC-day trades, not just regular-session trades. "
            "No fallback."
        ),
        "path_policy": (
            "Hourly: 11 consecutive scheduled whole hours entirely within regular trading "
            "(normally starts 10:00-15:00 New York); daily: 11 consecutive native UTC-day "
            "bars whose labels are XNYS session dates. Daily holiday/weekend-label bars are "
            "excluded even if they contain a preceding session's extended-hours tail. "
            "Overnight and scheduled closures are skipped; missing native scheduled bars are "
            "not skipped. Returns across adjacent sessions include their overnight move."
        ),
        "provenance_policy": (
            "Explicit is_native=true only. Derived, carried-price and unknown-provenance "
            "bars never contribute. Native zero-range/zero-volume bars are not assumed carried."
        ),
        "price_scale_policy": (
            "Dimensionless and invariant to uniform positive price rescaling. No corporate-"
            "action adjustment is inferred; raw native split jumps remain in returns and "
            "must be disclosed as an experiment limitation. Uniform rescaling invariance "
            "is not split adjustment."
        ),
        "provider_timing_reference": "https://databento.com/docs/schemas-and-data-formats/ohlcv",
        "missing_policy": (
            "Nonpositive/invalid prices, negative or nonfinite volume, incomplete/unavailable "
            "bars, insufficient warm-up, stale/missing scheduled constituents or zero "
            "denominators produce missing features; no backfill or synthetic observations."
        ),
        "formulas": {
            FEATURE_COLUMNS[0]: "log(((high-low)/close) / median(prior eligible same-slot normalized ranges))",
            FEATURE_COLUMNS[1]: "sum(min(r_i,0)^2) / sum(r_i^2), r_i=log(close_i/close_(i-1)), 10 returns",
            FEATURE_COLUMNS[2]: "sum(abs(r_i)^2) / sum(abs(r_i))^2, 10 returns",
            FEATURE_COLUMNS[3]: "sum(r_i) * abs(mean(sign(r_i))), 10 returns",
        },
        "units": {name: "dimensionless" for name in FEATURE_COLUMNS},
        "hypotheses": {
            "activity": "Unusual same-slot range may distinguish informed movement from ordinary intraday seasonality.",
            "path": "Downside variance balance and movement concentration may distinguish directional pressure from isolated shocks.",
            "interaction": "A signed cumulative move with persistent return signs may have different continuation probability.",
        },
        "existing_overlap": {
            "activity": "Existing breakout v1.1 has same-slot volume participation, not same-slot normalized range; volume clone omitted.",
            "path": "Existing regime volatility and momentum share inputs but omit downside variance share and movement concentration.",
            "interaction": "Existing momentum overlaps cumulative return; this explicitly multiplies it by unsigned sign persistence.",
        },
        "features": [asdict(spec) for spec in RESEARCH_FEATURE_SPECS],
    }


def calculate_research_ohlcv_features(
    bars: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    as_of: object | None = None,
) -> pd.DataFrame:
    """Calculate four fixed features while retaining every observed input row.

    ``is_native`` must explicitly identify provenance, with true, false or null
    booleans. Optional ``bar_complete`` likewise must be boolean. Null is never
    treated as affirmative evidence. Provided bar ends must match the native
    interval contract, including UTC day end for daily bars. ``as_of`` only masks
    bars unavailable at that instant; it cannot
    change earlier results. Consumers must additionally match the exact source
    bar and enforce ``available_at <= decision_timestamp``.
    """
    if timeframe not in {"1h", "1d"}:
        raise ValueError("Research OHLCV supports only native 1h and 1d bars.")
    frame = bars.copy()
    if "timestamp" not in frame and "bar_timestamp" in frame:
        frame = frame.rename(columns={"bar_timestamp": "timestamp"})
    required = {"timestamp", "open", "high", "low", "close", "volume", "is_native"}
    if missing := sorted(required.difference(frame.columns)):
        raise ValueError("Research OHLCV is missing required columns: " + ", ".join(missing))
    if not str(symbol).strip():
        raise ValueError("Research OHLCV requires a symbol.")
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="raise")
    if frame["timestamp"].isna().any() or frame["timestamp"].duplicated().any():
        raise ValueError("Research OHLCV requires nonmissing unique timestamps.")
    frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
    for column in ("is_native", "bar_complete"):
        if column in frame:
            nonmissing = frame[column].dropna()
            if not nonmissing.map(lambda value: isinstance(value, (bool, np.bool_))).all():
                raise ValueError(f"{column} must contain explicit boolean provenance/completion evidence.")
    if frame.empty:
        return _empty_result()

    schedule = _schedule(frame["timestamp"], timeframe)
    timing = _timing(frame["timestamp"], timeframe, schedule)
    if "bar_end_timestamp" in frame:
        supplied = pd.to_datetime(frame["bar_end_timestamp"], utc=True, errors="raise")
        mismatch = supplied.notna() & timing["bar_end_timestamp"].notna() & supplied.ne(timing["bar_end_timestamp"])
        if mismatch.any():
            raise ValueError("Provided bar ends conflict with the native interval timing contract.")
    result = timing.copy()
    result.insert(0, "symbol", str(symbol).strip().upper())
    result.insert(1, "bar_timestamp", frame["timestamp"])
    result["available_at"] = result["bar_end_timestamp"] + PROCESSING_DELAY
    result["timeframe"] = timeframe
    result["calculation_version"] = SEMANTIC_VERSION
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    numeric = frame[["open", "high", "low", "close", "volume"]]
    valid = pd.Series(np.isfinite(numeric).all(axis=1), index=frame.index)
    valid &= numeric[["open", "high", "low", "close"]].gt(0).all(axis=1)
    valid &= frame["volume"].ge(0)
    valid &= frame["high"].ge(numeric[["open", "low", "close"]].max(axis=1))
    valid &= frame["low"].le(numeric[["open", "high", "close"]].min(axis=1))
    valid &= frame["is_native"].eq(True).fillna(False)
    valid &= result["session_eligible"]
    if "bar_complete" in frame:
        valid &= frame["bar_complete"].eq(True).fillna(False)
    if as_of is not None:
        cutoff = pd.Timestamp(as_of)
        if cutoff.tzinfo is None:
            raise ValueError("as_of must include its timezone.")
        valid &= result["available_at"].le(cutoff.tz_convert("UTC"))
    result["native_input_eligible"] = valid
    normalized_range = ((frame["high"] - frame["low"]) / frame["close"]).where(valid)
    result[FEATURE_COLUMNS[0]], result["range_comparator_count"] = _range_anomaly(
        normalized_range, result, len(schedule)
    )

    path_grid = _path_grid(schedule, timeframe)
    close = pd.Series(frame["close"].where(valid).to_numpy(), index=pd.DatetimeIndex(frame["timestamp"]))
    grid_close = close.reindex(path_grid)
    returns = np.log(grid_close / grid_close.shift(1))
    squares = returns.pow(2).rolling(PATH_LOOKBACK, min_periods=PATH_LOOKBACK).sum()
    downside = returns.clip(upper=0).pow(2).rolling(PATH_LOOKBACK, min_periods=PATH_LOOKBACK).sum()
    absolute_sum = returns.abs().rolling(PATH_LOOKBACK, min_periods=PATH_LOOKBACK).sum()
    persistence = np.sign(returns).rolling(PATH_LOOKBACK, min_periods=PATH_LOOKBACK).mean().abs()
    path_values = (
        downside / squares.where(squares > 0),
        squares / absolute_sum.pow(2).where(absolute_sum > 0),
        returns.rolling(PATH_LOOKBACK, min_periods=PATH_LOOKBACK).sum() * persistence,
    )
    for name, values in zip(FEATURE_COLUMNS[1:], path_values, strict=True):
        result[name] = values.reindex(pd.DatetimeIndex(frame["timestamp"])).to_numpy()
    result["path_slot_eligible"] = frame["timestamp"].isin(path_grid)
    return result.drop(columns=["session_number", "slot_key"])


def _schedule(timestamps: pd.Series, timeframe: str) -> pd.DataFrame:
    import exchange_calendars as xcals

    labels = timestamps.dt.tz_localize(None).dt.normalize() if timeframe == "1d" else timestamps.dt.tz_convert("America/New_York").dt.tz_localize(None).dt.normalize()
    calendar = xcals.get_calendar(EXCHANGE_CALENDAR, start=labels.min() - pd.Timedelta(days=40), end=labels.max() + pd.Timedelta(days=7))
    sessions = calendar.schedule.loc[labels.min():labels.max(), ["open", "close"]].copy()
    sessions["duration"] = (sessions["close"] - sessions["open"]).dt.total_seconds() / 60
    sessions["utc_open_minute"] = sessions["open"].dt.hour * 60 + sessions["open"].dt.minute
    sessions["previous_date_has_session"] = (sessions.index - pd.Timedelta(days=1)).isin(calendar.sessions)
    sessions["session_number"] = np.arange(len(sessions))
    return sessions


def _timing(timestamps: pd.Series, timeframe: str, schedule: pd.DataFrame) -> pd.DataFrame:
    local = timestamps.dt.tz_convert("America/New_York")
    labels = timestamps.dt.tz_localize(None).dt.normalize() if timeframe == "1d" else local.dt.tz_localize(None).dt.normalize()
    rows = schedule.reindex(pd.DatetimeIndex(labels)).reset_index(drop=True)
    if timeframe == "1d":
        aligned = timestamps.eq(timestamps.dt.normalize())
        end = timestamps + pd.Timedelta(days=1)
        eligible = aligned & rows["open"].notna()
        slot_key = (
            "UTC_DAILY:" + rows["utc_open_minute"].astype(str)
            + ":" + rows["previous_date_has_session"].astype(str)
        )
        session_type = pd.Series("NATIVE_UTC_DAILY", index=timestamps.index)
    else:
        end = timestamps + pd.Timedelta(hours=1)
        whole_hour = local.dt.minute.eq(0) & local.dt.second.eq(0) & local.dt.microsecond.eq(0) & local.dt.nanosecond.eq(0)
        wholly_regular = timestamps.ge(rows["open"]) & end.le(rows["close"])
        normal_extended = rows["duration"].eq(390) & local.dt.hour.ge(4) & local.dt.hour.lt(20)
        eligible = whole_hour & rows["open"].notna() & (normal_extended | wholly_regular)
        session_type = pd.Series(np.select(
            [end.le(rows["open"]), wholly_regular, timestamps.ge(rows["close"])],
            ["PREMARKET", "REGULAR", "POSTMARKET"], default="CROSS_OPEN"
        ), index=timestamps.index)
        slot_key = local.dt.hour.astype(str) + ":" + session_type
    return pd.DataFrame({
        "bar_end_timestamp": end,
        "session_date": labels.dt.strftime("%Y-%m-%d"),
        "session_type": session_type.where(eligible, "INELIGIBLE"),
        "session_duration_minutes": rows["duration"],
        "session_number": rows["session_number"],
        "slot_key": slot_key + ":" + rows["duration"].astype(str),
        "session_eligible": eligible,
    })


def _range_anomaly(normalized_range: pd.Series, timing: pd.DataFrame, session_count: int) -> tuple[pd.Series, pd.Series]:
    anomaly = pd.Series(np.nan, index=timing.index, dtype=float)
    counts = pd.Series(0, index=timing.index, dtype="int64")
    eligible = timing.loc[timing["session_eligible"]]
    for _, indexes in eligible.groupby("slot_key", sort=False).groups.items():
        positions = timing.loc[indexes, "session_number"].astype(int)
        values = pd.Series(normalized_range.loc[indexes].to_numpy(), index=positions)
        if values.index.has_duplicates:
            raise ValueError("Duplicate research range slot within an exchange session.")
        grid = values.reindex(pd.RangeIndex(session_count))
        prior = grid.shift(1).rolling(RANGE_LOOKBACK, min_periods=RANGE_MIN_PERIODS)
        median = prior.median()
        count = grid.shift(1).rolling(RANGE_LOOKBACK, min_periods=1).count()
        ratio = grid / median.where(median > 0)
        output = np.log(ratio.where(ratio > 0))
        anomaly.loc[indexes] = output.reindex(positions).to_numpy()
        counts.loc[indexes] = count.reindex(positions).fillna(0).astype(int).to_numpy()
    return anomaly, counts


def _path_grid(schedule: pd.DataFrame, timeframe: str) -> pd.DatetimeIndex:
    if timeframe == "1d":
        return pd.DatetimeIndex(schedule.index).tz_localize("UTC")
    slots: list[pd.Timestamp] = []
    for row in schedule.itertuples():
        start = row.open.ceil("h")
        last_start = row.close - pd.Timedelta(hours=1)
        if start <= last_start:
            slots.extend(pd.date_range(start, last_start, freq="h"))
    return pd.DatetimeIndex(slots, tz="UTC")


def _empty_result() -> pd.DataFrame:
    columns = ["symbol", "bar_timestamp", "bar_end_timestamp", "available_at", "session_date", "session_type", "session_duration_minutes", "session_eligible", "timeframe", "calculation_version", "native_input_eligible", "range_comparator_count", "path_slot_eligible", *FEATURE_COLUMNS]
    return pd.DataFrame(columns=columns)
