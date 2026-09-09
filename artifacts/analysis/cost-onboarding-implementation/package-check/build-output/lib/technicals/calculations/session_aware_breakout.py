from __future__ import annotations

import re

import numpy as np
import pandas as pd

from datafetching.bar_timing import session_metadata
from technicals.calculations.breakout_pressure import (
    CALCULATION_NAME,
    calculate_breakout_pressure as calculate_base_breakout_pressure,
)

CALCULATION_VERSION = "1.1.0"
_TIMEFRAME_PATTERN = re.compile(r"^(\d+)(s|m|h)$")


def calculate_breakout_pressure(
    bars: pd.DataFrame,
    *,
    symbol: str,
    provider: str,
    timeframe: str,
) -> pd.DataFrame:
    """Run breakout pressure with session-aware historical volume baselines."""
    result = calculate_base_breakout_pressure(
        bars,
        symbol=symbol,
        provider=provider,
        timeframe=timeframe,
    )
    if result.empty:
        return result

    timestamps = pd.to_datetime(result["timestamp"], utc=True, errors="coerce")
    sessions = session_metadata(timestamps, timeframe)
    result["session_type"] = sessions["session_type"].to_numpy()
    result["session_date"] = sessions["session_date"].to_numpy()
    result["session_minute"] = sessions["session_minute"].to_numpy()
    result["session_progress"] = sessions["session_progress"].to_numpy()

    volume = pd.to_numeric(result["volume"], errors="coerce")
    positive_volume = volume.where(volume > 0)
    window = int(pd.to_numeric(result["volume_period"], errors="coerce").dropna().iloc[-1])
    bootstrap = result["calculation_mode"].astype(str).eq("BOOTSTRAP")
    minimum = 5 if bootstrap.any() else 10

    session_type = result["session_type"].astype(str)
    slot_width = _timeframe_minutes(timeframe)
    session_slot = np.floor(
        pd.to_numeric(result["session_minute"], errors="coerce") / slot_width
    )
    result["volume_session_slot"] = session_slot

    slot_baseline = positive_volume.groupby(
        [session_type, session_slot],
        sort=False,
        dropna=False,
    ).transform(lambda values: values.shift(1).rolling(window, min_periods=minimum).median())
    session_baseline = positive_volume.groupby(
        session_type,
        sort=False,
        dropna=False,
    ).transform(lambda values: values.shift(1).rolling(window, min_periods=minimum).median())
    global_baseline = positive_volume.shift(1).rolling(window, min_periods=minimum).median()

    volume_median = slot_baseline.combine_first(session_baseline).combine_first(global_baseline)
    baseline_method = pd.Series(
        np.select(
            [slot_baseline.notna(), session_baseline.notna(), global_baseline.notna()],
            ["SESSION_SLOT", "SESSION_TYPE", "GLOBAL"],
            default="UNAVAILABLE",
        ),
        index=result.index,
        dtype="object",
    )
    volume_ratio = volume / volume_median.where(volume_median > 0)
    participation = _bounded_score(
        np.log(volume_ratio.clip(lower=0.1, upper=10.0)),
        scale=0.75,
    )
    available = volume_ratio.notna() & np.isfinite(volume_ratio)
    participation = participation.where(available, 50.0)

    confirmed = result["breakout_direction"].astype(str).ne("NONE")
    magnitude = pd.to_numeric(result["breakout_magnitude_atr"], errors="coerce").fillna(0.0)
    magnitude_score = (100.0 * np.tanh(magnitude / 1.50)).clip(0.0, 100.0)
    strength = pd.to_numeric(result["breakout_strength_score"], errors="coerce").fillna(0.0)
    strength.loc[confirmed] = (
        magnitude_score.loc[confirmed] * 0.65
        + participation.loc[confirmed] * 0.35
    ).clip(0.0, 100.0)

    confidence = pd.to_numeric(result["confidence_score"], errors="coerce").fillna(0.0)
    setup_quality = pd.to_numeric(result["setup_quality"], errors="coerce").fillna(0.0)
    setup_quality.loc[confirmed] = (
        strength.loc[confirmed] * 0.65
        + confidence.loc[confirmed] * 0.35
    ).clip(0.0, 100.0)

    result["volume_median"] = volume_median
    result["volume_ratio"] = volume_ratio
    result["volume_participation_score"] = participation
    result["volume_baseline_method"] = baseline_method
    result["volume_session_aware"] = True
    result["breakout_strength_score"] = strength
    result["setup_quality"] = setup_quality
    result["calculation_version"] = CALCULATION_VERSION
    return result


def _timeframe_minutes(timeframe: str) -> float:
    match = _TIMEFRAME_PATTERN.fullmatch(str(timeframe or "").strip().lower())
    if match is None:
        return 1.0
    amount = float(match.group(1))
    unit = match.group(2)
    if unit == "s":
        return max(amount / 60.0, 1.0 / 60.0)
    if unit == "h":
        return amount * 60.0
    return amount


def _bounded_score(values: pd.Series, *, scale: float) -> pd.Series:
    return pd.Series(
        50.0 + 50.0 * np.tanh(values.to_numpy(dtype=float) / scale),
        index=values.index,
        dtype=float,
    ).clip(0.0, 100.0)
