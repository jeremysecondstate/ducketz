from __future__ import annotations

from datetime import datetime, timezone
from math import sqrt

import numpy as np
import pandas as pd

CALCULATION_NAME = "breakout-pressure"
CALCULATION_VERSION = "1.0.0"
MINIMUM_INPUT_BARS = 15
FULL_HISTORY_MINIMUM_BARS = 60
REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")

FULL_WINDOWS = {
    "atr": 14,
    "atr_min_periods": 14,
    "volatility": 20,
    "volatility_min_periods": 20,
    "channel": 20,
    "channel_min_periods": 20,
    "volume": 20,
    "volume_min_periods": 10,
    "baseline": 100,
    "baseline_min_periods": 30,
    "momentum": 10,
}
BOOTSTRAP_WINDOWS = {
    "atr": 14,
    "atr_min_periods": 4,
    "volatility": 10,
    "volatility_min_periods": 5,
    "channel": 10,
    "channel_min_periods": 10,
    "volume": 10,
    "volume_min_periods": 5,
    "baseline": 15,
    "baseline_min_periods": 5,
    "momentum": 5,
}


def calculate_breakout_pressure(
    bars: pd.DataFrame,
    *,
    symbol: str,
    provider: str,
    timeframe: str,
) -> pd.DataFrame:
    """Measure volatility compression, boundary pressure, and confirmed breakouts."""
    missing = [column for column in REQUIRED_COLUMNS if column not in bars.columns]
    if missing:
        raise ValueError(f"Breakout pressure input is missing columns: {', '.join(missing)}")

    frame = bars.loc[:, REQUIRED_COLUMNS].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = (
        frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
        .sort_values("timestamp")
        .drop_duplicates(subset=["timestamp"], keep="last")
        .reset_index(drop=True)
    )
    frame["volume"] = frame["volume"].fillna(0.0)

    if len(frame) < MINIMUM_INPUT_BARS:
        raise ValueError(
            f"Breakout pressure requires at least {MINIMUM_INPUT_BARS} bars; received {len(frame)}."
        )

    full_history = len(frame) >= FULL_HISTORY_MINIMUM_BARS
    calculation_mode = "FULL" if full_history else "BOOTSTRAP"
    windows = FULL_WINDOWS if full_history else BOOTSTRAP_WINDOWS

    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    volume = frame["volume"]
    previous_close = close.shift(1)
    return_1 = close.pct_change()

    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_14 = true_range.ewm(
        alpha=1 / windows["atr"],
        adjust=False,
        min_periods=windows["atr_min_periods"],
    ).mean()
    atr_percent = 100.0 * atr_14 / close.where(close != 0)

    rolling_mean = close.rolling(
        windows["volatility"],
        min_periods=windows["volatility_min_periods"],
    ).mean()
    rolling_std = close.rolling(
        windows["volatility"],
        min_periods=windows["volatility_min_periods"],
    ).std(ddof=0)
    bollinger_bandwidth = 400.0 * rolling_std / rolling_mean.abs().where(rolling_mean != 0)
    realized_volatility = (
        return_1.rolling(
            windows["volatility"],
            min_periods=windows["volatility_min_periods"],
        ).std(ddof=0)
        * sqrt(windows["volatility"])
    )

    prior_channel_high = (
        high.rolling(
            windows["channel"],
            min_periods=windows["channel_min_periods"],
        )
        .max()
        .shift(1)
    )
    prior_channel_low = (
        low.rolling(
            windows["channel"],
            min_periods=windows["channel_min_periods"],
        )
        .min()
        .shift(1)
    )
    channel_width = (prior_channel_high - prior_channel_low).where(
        prior_channel_high > prior_channel_low
    )
    channel_width_percent = 100.0 * channel_width / close.abs().where(close != 0)
    channel_position = ((close - prior_channel_low) / channel_width).clip(0.0, 1.0)

    atr_baseline = atr_percent.rolling(
        windows["baseline"],
        min_periods=windows["baseline_min_periods"],
    ).median()
    bandwidth_baseline = bollinger_bandwidth.rolling(
        windows["baseline"],
        min_periods=windows["baseline_min_periods"],
    ).median()
    channel_width_baseline = channel_width_percent.rolling(
        windows["baseline"],
        min_periods=windows["baseline_min_periods"],
    ).median()

    atr_compression_ratio = atr_percent / atr_baseline.where(atr_baseline > 0)
    bandwidth_compression_ratio = (
        bollinger_bandwidth / bandwidth_baseline.where(bandwidth_baseline > 0)
    )
    range_contraction_ratio = (
        channel_width_percent / channel_width_baseline.where(channel_width_baseline > 0)
    )

    atr_compression_score = _inverse_ratio_score(atr_compression_ratio, scale=0.55)
    bandwidth_compression_score = _inverse_ratio_score(
        bandwidth_compression_ratio, scale=0.55
    )
    compression_score = pd.concat(
        [
            atr_compression_score.rename("atr"),
            bandwidth_compression_score.rename("bandwidth"),
        ],
        axis=1,
    ).mean(axis=1, skipna=True)
    range_contraction_score = _inverse_ratio_score(
        range_contraction_ratio, scale=0.65
    )

    bar_width = (high - low).where(high > low)
    close_location = ((close - low) / bar_width).clip(0.0, 1.0)
    momentum_return = close.pct_change(windows["momentum"])
    momentum_volatility = (
        return_1.rolling(
            windows["momentum"],
            min_periods=max(3, windows["momentum"] // 2),
        ).std(ddof=0)
        * sqrt(windows["momentum"])
    )
    momentum_risk_adjusted = momentum_return / momentum_volatility.where(
        momentum_volatility > 0
    )
    momentum_direction_score = _bounded_score(
        momentum_risk_adjusted, scale=1.25
    )
    direction_score = (
        channel_position * 100.0 * 0.55
        + close_location.fillna(0.5) * 100.0 * 0.20
        + momentum_direction_score.fillna(50.0) * 0.25
    ).clip(0.0, 100.0)

    boundary_proximity_score = (
        channel_position.sub(0.5).abs() * 200.0
    ).clip(0.0, 100.0)
    breakout_readiness_score = (
        compression_score * 0.50
        + range_contraction_score * 0.25
        + boundary_proximity_score * 0.25
    ).clip(0.0, 100.0)

    positive_volume = volume.where(volume > 0)
    volume_median = positive_volume.rolling(
        windows["volume"],
        min_periods=windows["volume_min_periods"],
    ).median()
    volume_ratio = volume / volume_median.where(volume_median > 0)
    volume_participation_score = _bounded_score(
        np.log(volume_ratio.clip(lower=0.1, upper=10.0)),
        scale=0.75,
    )
    volume_available = volume_ratio.notna() & np.isfinite(volume_ratio)
    volume_participation_score = volume_participation_score.where(
        volume_available, 50.0
    )

    breakout_up = close > prior_channel_high
    breakout_down = close < prior_channel_low
    breakout_direction = pd.Series(
        np.select(
            [breakout_up, breakout_down],
            ["UP", "DOWN"],
            default="NONE",
        ),
        index=frame.index,
        dtype="object",
    )
    breakout_distance = pd.Series(0.0, index=frame.index, dtype=float)
    breakout_distance.loc[breakout_up] = (
        close.loc[breakout_up] - prior_channel_high.loc[breakout_up]
    )
    breakout_distance.loc[breakout_down] = (
        prior_channel_low.loc[breakout_down] - close.loc[breakout_down]
    )
    breakout_magnitude_atr = (
        breakout_distance / atr_14.where(atr_14 > 0)
    ).clip(lower=0.0)
    magnitude_score = (
        100.0 * np.tanh(breakout_magnitude_atr / 1.50)
    ).clip(0.0, 100.0)
    breakout_strength_score = pd.Series(0.0, index=frame.index, dtype=float)
    confirmed = breakout_up | breakout_down
    breakout_strength_score.loc[confirmed] = (
        magnitude_score.loc[confirmed] * 0.65
        + volume_participation_score.loc[confirmed] * 0.35
    ).clip(0.0, 100.0)

    upside_pressure_score = direction_score
    downside_pressure_score = 100.0 - direction_score
    breakout_state = _classify_state(
        breakout_up=breakout_up,
        breakout_down=breakout_down,
        readiness=breakout_readiness_score,
        direction=direction_score,
        compression=compression_score,
    )

    history_bars = pd.Series(
        np.arange(1, len(frame) + 1), index=frame.index, dtype=float
    )
    history_maturity = (
        history_bars / (100.0 if full_history else FULL_HISTORY_MINIMUM_BARS)
    ).clip(0.0, 1.0)
    baseline_available = (
        atr_baseline.notna()
        & bandwidth_baseline.notna()
        & channel_width_baseline.notna()
    ).astype(float)
    metric_coverage = pd.concat(
        [
            compression_score.rename("compression"),
            range_contraction_score.rename("range"),
            direction_score.rename("direction"),
            breakout_readiness_score.rename("readiness"),
        ],
        axis=1,
    ).notna().mean(axis=1)
    confidence_score = 100.0 * (
        history_maturity * 0.40
        + metric_coverage * 0.30
        + baseline_available * 0.20
        + pd.Series(
            np.where(volume_available, 1.0, 0.5),
            index=frame.index,
            dtype=float,
        )
        * 0.10
    )
    confidence_cap = pd.Series(100.0, index=frame.index, dtype=float)
    if not full_history:
        progress = (
            (history_bars - MINIMUM_INPUT_BARS)
            / (FULL_HISTORY_MINIMUM_BARS - MINIMUM_INPUT_BARS)
        ).clip(0.0, 1.0)
        confidence_cap = 45.0 + 25.0 * progress
        confidence_score = pd.concat(
            [
                confidence_score.rename("base"),
                confidence_cap.rename("cap"),
            ],
            axis=1,
        ).min(axis=1)
    confidence_score = confidence_score.clip(0.0, 100.0)

    setup_quality = pd.Series(
        breakout_readiness_score * 0.65 + confidence_score * 0.35,
        index=frame.index,
        dtype=float,
    )
    setup_quality.loc[confirmed] = (
        breakout_strength_score.loc[confirmed] * 0.65
        + confidence_score.loc[confirmed] * 0.35
    )
    setup_quality = setup_quality.clip(0.0, 100.0)

    result = frame.copy()
    result.insert(0, "symbol", symbol.strip().upper())
    result.insert(1, "provider", provider.strip().lower())
    result.insert(2, "timeframe", timeframe)
    result["atr_14"] = atr_14
    result["atr_percent"] = atr_percent
    result["bollinger_bandwidth"] = bollinger_bandwidth
    result["realized_volatility"] = realized_volatility
    result["prior_channel_high"] = prior_channel_high
    result["prior_channel_low"] = prior_channel_low
    result["channel_width_percent"] = channel_width_percent
    result["channel_position"] = channel_position
    result["atr_compression_ratio"] = atr_compression_ratio
    result["bandwidth_compression_ratio"] = bandwidth_compression_ratio
    result["range_contraction_ratio"] = range_contraction_ratio
    result["compression_score"] = compression_score
    result["range_contraction_score"] = range_contraction_score
    result["boundary_proximity_score"] = boundary_proximity_score
    result["momentum_return"] = momentum_return
    result["momentum_risk_adjusted"] = momentum_risk_adjusted
    result["direction_score"] = direction_score
    result["upside_pressure_score"] = upside_pressure_score
    result["downside_pressure_score"] = downside_pressure_score
    result["volume_median"] = volume_median
    result["volume_ratio"] = volume_ratio
    result["volume_participation_score"] = volume_participation_score
    result["breakout_readiness_score"] = breakout_readiness_score
    result["breakout_direction"] = breakout_direction
    result["breakout_magnitude_atr"] = breakout_magnitude_atr
    result["breakout_strength_score"] = breakout_strength_score
    result["breakout_state"] = breakout_state
    result["setup_quality"] = setup_quality
    result["confidence_score"] = confidence_score
    result["confidence_cap"] = confidence_cap
    result["calculation_mode"] = calculation_mode
    result["history_bars"] = history_bars.astype("int64")
    result["minimum_input_bars"] = MINIMUM_INPUT_BARS
    result["full_history_minimum_bars"] = FULL_HISTORY_MINIMUM_BARS
    result["atr_period"] = windows["atr"]
    result["atr_min_periods"] = windows["atr_min_periods"]
    result["volatility_period"] = windows["volatility"]
    result["channel_period"] = windows["channel"]
    result["volume_period"] = windows["volume"]
    result["baseline_period"] = windows["baseline"]
    result["momentum_period"] = windows["momentum"]
    result["calculation"] = CALCULATION_NAME
    result["calculation_version"] = CALCULATION_VERSION
    result["generated_at"] = datetime.now(timezone.utc).isoformat()

    core_valid = (
        compression_score.notna()
        & range_contraction_score.notna()
        & direction_score.notna()
        & prior_channel_high.notna()
        & prior_channel_low.notna()
    )
    result = result.loc[core_valid].reset_index(drop=True)
    if result.empty:
        raise ValueError("Breakout pressure produced no fully initialized rows.")

    state_changed = result["breakout_state"].ne(result["breakout_state"].shift())
    result["bars_since_state_change"] = result.groupby(
        state_changed.cumsum()
    ).cumcount()
    result["readiness_change_5"] = result["breakout_readiness_score"].diff(5)
    return result


def _classify_state(
    *,
    breakout_up: pd.Series,
    breakout_down: pd.Series,
    readiness: pd.Series,
    direction: pd.Series,
    compression: pd.Series,
) -> pd.Series:
    conditions = [
        breakout_up,
        breakout_down,
        (readiness >= 65.0) & (direction >= 60.0),
        (readiness >= 65.0) & (direction <= 40.0),
        readiness >= 65.0,
        (compression <= 35.0) & (direction >= 60.0),
        (compression <= 35.0) & (direction <= 40.0),
    ]
    choices = [
        "BREAKOUT_UP",
        "BREAKOUT_DOWN",
        "COILED_UP",
        "COILED_DOWN",
        "COILED_NEUTRAL",
        "EXPANDING_UP",
        "EXPANDING_DOWN",
    ]
    return pd.Series(
        np.select(conditions, choices, default="NO_SETUP"),
        index=readiness.index,
        dtype="object",
    )


def _inverse_ratio_score(values: pd.Series, *, scale: float) -> pd.Series:
    return pd.Series(
        50.0 + 50.0 * np.tanh((1.0 - values.to_numpy(dtype=float)) / scale),
        index=values.index,
        dtype=float,
    ).clip(0.0, 100.0)


def _bounded_score(values: pd.Series, *, scale: float) -> pd.Series:
    return pd.Series(
        50.0 + 50.0 * np.tanh(values.to_numpy(dtype=float) / scale),
        index=values.index,
        dtype=float,
    ).clip(0.0, 100.0)
