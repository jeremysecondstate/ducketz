from __future__ import annotations

from datetime import datetime, timezone
from math import sqrt

import numpy as np
import pandas as pd

CALCULATION_NAME = "market-regime"
CALCULATION_VERSION = "1.2.0"
MINIMUM_INPUT_BARS = 15
FULL_HISTORY_MINIMUM_BARS = 60
REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")

FULL_WINDOWS = {
    "atr": 14,
    "atr_min_periods": 14,
    "ema_fast": 20,
    "ema_fast_min_periods": 20,
    "ema_slow": 50,
    "ema_slow_min_periods": 50,
    "momentum": 20,
    "range": 50,
    "volume": 20,
    "volume_min_periods": 10,
    "volatility_baseline": 100,
    "volatility_baseline_min_periods": 30,
}
BOOTSTRAP_WINDOWS = {
    # Preserve the nominal indicator identities while allowing early estimates.
    "atr": 14,
    "atr_min_periods": 4,
    "ema_fast": 20,
    "ema_fast_min_periods": 5,
    "ema_slow": 50,
    "ema_slow_min_periods": 6,
    # Fixed-lookback components still use honest shorter bootstrap windows.
    "momentum": 5,
    "range": 15,
    "volume": 10,
    "volume_min_periods": 5,
    "volatility_baseline": 15,
    "volatility_baseline_min_periods": 5,
}


def calculate_market_regime(
    bars: pd.DataFrame,
    *,
    symbol: str,
    provider: str,
    timeframe: str,
) -> pd.DataFrame:
    """Calculate the Duckets Market Regime Composite for one bar timeframe.

    Mature histories use the original fully initialized 14/20/50-bar model.
    Histories with 15-59 usable bars use the same ATR-14 and EMA-20/50
    smoothing parameters with earlier 4/5/6-bar warm-up gates, plus honest
    shorter fixed-lookback momentum/range/volume components. Bootstrap
    confidence remains capped. The directional 0-100 score combines:

    - ATR-normalized EMA trend (40%)
    - volatility-adjusted momentum (30%)
    - trailing-range location (20%)
    - signed volume confirmation (10%)
    """
    missing = [column for column in REQUIRED_COLUMNS if column not in bars.columns]
    if missing:
        raise ValueError(f"Market regime input is missing columns: {', '.join(missing)}")

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
            f"Market regime requires at least {MINIMUM_INPUT_BARS} bars; received {len(frame)}."
        )

    full_history = len(frame) >= FULL_HISTORY_MINIMUM_BARS
    regime_mode = "FULL" if full_history else "BOOTSTRAP"
    windows = FULL_WINDOWS if full_history else BOOTSTRAP_WINDOWS

    close = frame["close"]
    high = frame["high"]
    low = frame["low"]
    volume = frame["volume"]
    components = _market_components(
        close,
        high,
        low,
        volume,
        atr_period=windows["atr"],
        atr_min_periods=windows["atr_min_periods"],
        ema_fast_period=windows["ema_fast"],
        ema_fast_min_periods=windows["ema_fast_min_periods"],
        ema_slow_period=windows["ema_slow"],
        ema_slow_min_periods=windows["ema_slow_min_periods"],
        momentum_period=windows["momentum"],
        range_period=windows["range"],
        volume_period=windows["volume"],
        volume_min_periods=windows["volume_min_periods"],
    )

    history_bars = pd.Series(np.arange(1, len(frame) + 1), index=frame.index, dtype=float)
    maturity_denominator = 100.0 if full_history else float(FULL_HISTORY_MINIMUM_BARS)
    history_maturity = (history_bars / maturity_denominator).clip(0.0, 1.0)
    volume_quality = pd.Series(
        np.where(components["volume_available"], 1.0, 0.5),
        index=frame.index,
        dtype=float,
    )
    confidence_score = 100.0 * (
        history_maturity * 0.50
        + components["component_agreement"].fillna(0.0) * 0.35
        + volume_quality * 0.15
    )
    confidence_cap = pd.Series(100.0, index=frame.index, dtype=float)
    if not full_history:
        bootstrap_progress = (
            (history_bars - MINIMUM_INPUT_BARS)
            / (FULL_HISTORY_MINIMUM_BARS - MINIMUM_INPUT_BARS)
        ).clip(0.0, 1.0)
        confidence_cap = 45.0 + 25.0 * bootstrap_progress
        confidence_score = pd.concat(
            [confidence_score.rename("base"), confidence_cap.rename("cap")], axis=1
        ).min(axis=1)
    confidence_score = confidence_score.clip(0.0, 100.0)

    atr_percent = 100.0 * components["atr"] / close.where(close != 0)
    atr_percent_baseline = atr_percent.rolling(
        windows["volatility_baseline"],
        min_periods=windows["volatility_baseline_min_periods"],
    ).median()
    volatility_ratio = atr_percent / atr_percent_baseline.where(atr_percent_baseline > 0)
    volatility_regime = pd.Series(
        np.select(
            [volatility_ratio < 0.80, volatility_ratio > 1.25],
            ["COMPRESSED", "EXPANDED"],
            default="NORMAL",
        ),
        index=frame.index,
        dtype="object",
    ).where(volatility_ratio.notna(), "UNAVAILABLE")

    technical_score = components["technical_score"]
    regime_label = pd.cut(
        technical_score,
        bins=[-np.inf, 20.0, 40.0, 60.0, 80.0, np.inf],
        labels=[
            "STRONG_BEARISH",
            "BEARISH",
            "NEUTRAL",
            "BULLISH",
            "STRONG_BULLISH",
        ],
        right=False,
    ).astype("object")

    result = frame.copy()
    result.insert(0, "symbol", symbol.strip().upper())
    result.insert(1, "provider", provider.strip().lower())
    result.insert(2, "timeframe", timeframe)

    # These retain their nominal 14/20/50 formulas in both modes. Bootstrap
    # merely lowers the output warm-up gate and is identified by metadata.
    result["atr_14"] = components["atr"]
    result["ema_20"] = components["ema_fast"]
    result["ema_50"] = components["ema_slow"]

    # Fixed-lookback names remain mature-only because bootstrap uses genuinely
    # shorter lookbacks and must not place a 5-bar value in a 20-bar column.
    result["return_20"] = components["return_window"] if full_history else np.nan
    result["realized_volatility_20"] = (
        components["realized_volatility"] if full_history else np.nan
    )
    result["range_high_50"] = components["range_high"] if full_history else np.nan
    result["range_low_50"] = components["range_low"] if full_history else np.nan
    result["range_position_50"] = components["range_position"] if full_history else np.nan
    result["volume_median_20"] = components["volume_median"] if full_history else np.nan
    result["volume_ratio_20"] = components["volume_ratio"] if full_history else np.nan

    # Generic effective columns are valid in both FULL and BOOTSTRAP modes.
    result["atr_effective"] = components["atr"]
    result["atr_percent"] = atr_percent
    result["ema_fast"] = components["ema_fast"]
    result["ema_slow"] = components["ema_slow"]
    result["trend_atr"] = components["trend_atr"]
    result["return_effective"] = components["return_window"]
    result["realized_volatility_effective"] = components["realized_volatility"]
    result["momentum_risk_adjusted"] = components["momentum_risk_adjusted"]
    result["range_high"] = components["range_high"]
    result["range_low"] = components["range_low"]
    result["range_position"] = components["range_position"]
    result["volume_median"] = components["volume_median"]
    result["volume_ratio"] = components["volume_ratio"]
    result["trend_score"] = components["trend_score"]
    result["momentum_score"] = components["momentum_score"]
    result["range_score"] = components["range_score"]
    result["volume_score"] = components["volume_score"]
    result["technical_score"] = technical_score
    result["regime_strength"] = ((technical_score - 50.0).abs() * 2.0).clip(0.0, 100.0)
    result["confidence_score"] = confidence_score
    result["confidence_cap"] = confidence_cap
    result["component_agreement"] = components["component_agreement"] * 100.0
    result["volatility_ratio"] = volatility_ratio
    result["volatility_regime"] = volatility_regime
    result["regime_label"] = regime_label
    result["regime_mode"] = regime_mode
    result["history_bars"] = history_bars.astype("int64")
    result["minimum_input_bars"] = MINIMUM_INPUT_BARS
    result["full_history_minimum_bars"] = FULL_HISTORY_MINIMUM_BARS
    result["atr_period"] = windows["atr"]
    result["atr_min_periods"] = windows["atr_min_periods"]
    result["ema_fast_period"] = windows["ema_fast"]
    result["ema_fast_min_periods"] = windows["ema_fast_min_periods"]
    result["ema_slow_period"] = windows["ema_slow"]
    result["ema_slow_min_periods"] = windows["ema_slow_min_periods"]
    result["momentum_period"] = windows["momentum"]
    result["range_period"] = windows["range"]
    result["volume_period"] = windows["volume"]
    result["calculation"] = CALCULATION_NAME
    result["calculation_version"] = CALCULATION_VERSION
    result["generated_at"] = datetime.now(timezone.utc).isoformat()

    core_valid = (
        components["trend_score"].notna()
        & components["momentum_score"].notna()
        & components["range_score"].notna()
    )
    result = result.loc[core_valid].reset_index(drop=True)
    if result.empty:
        raise ValueError("Market regime produced no fully initialized rows.")

    changed = result["regime_label"].ne(result["regime_label"].shift())
    result["bars_since_regime_change"] = result.groupby(changed.cumsum()).cumcount()
    result["technical_score_change_5"] = result["technical_score"].diff(5)
    return result


def _market_components(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    volume: pd.Series,
    *,
    atr_period: int,
    atr_min_periods: int,
    ema_fast_period: int,
    ema_fast_min_periods: int,
    ema_slow_period: int,
    ema_slow_min_periods: int,
    momentum_period: int,
    range_period: int,
    volume_period: int,
    volume_min_periods: int,
) -> dict[str, pd.Series]:
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(
        alpha=1 / atr_period,
        adjust=False,
        min_periods=atr_min_periods,
    ).mean()
    atr_denominator = atr.where(atr > 0)

    ema_fast = close.ewm(
        span=ema_fast_period,
        adjust=False,
        min_periods=ema_fast_min_periods,
    ).mean()
    ema_slow = close.ewm(
        span=ema_slow_period,
        adjust=False,
        min_periods=ema_slow_min_periods,
    ).mean()
    trend_atr = (ema_fast - ema_slow) / atr_denominator
    trend_score = _bounded_score(trend_atr, scale=2.0)

    return_1 = close.pct_change()
    return_window = close.pct_change(momentum_period)
    realized_volatility = (
        return_1.rolling(momentum_period, min_periods=momentum_period).std(ddof=0)
        * sqrt(momentum_period)
    )
    momentum_risk_adjusted = return_window / realized_volatility.where(
        realized_volatility > 0
    )
    momentum_score = _bounded_score(momentum_risk_adjusted, scale=1.25)

    range_high = high.rolling(range_period, min_periods=range_period).max()
    range_low = low.rolling(range_period, min_periods=range_period).min()
    range_width = (range_high - range_low).where(range_high > range_low)
    range_position = ((close - range_low) / range_width).clip(0.0, 1.0)
    range_score = range_position * 100.0

    positive_volume = volume.where(volume > 0)
    volume_median = positive_volume.rolling(
        volume_period,
        min_periods=volume_min_periods,
    ).median()
    volume_ratio = volume / volume_median.where(volume_median > 0)
    signed_volume_impulse = np.sign(return_1) * np.log(
        volume_ratio.clip(lower=0.1, upper=10.0)
    )
    raw_volume_score = _bounded_score(signed_volume_impulse, scale=0.75)
    volume_available = volume_ratio.notna() & np.isfinite(volume_ratio)
    volume_score = raw_volume_score.where(volume_available, 50.0)

    technical_score = (
        trend_score * 0.40
        + momentum_score * 0.30
        + range_score * 0.20
        + volume_score * 0.10
    ).clip(0.0, 100.0)
    agreement_components = pd.concat(
        [
            trend_score.rename("trend"),
            momentum_score.rename("momentum"),
            range_score.rename("range"),
            raw_volume_score.where(volume_available).rename("volume"),
        ],
        axis=1,
    )
    component_dispersion = agreement_components.std(axis=1, ddof=0)
    component_agreement = (1.0 - component_dispersion / 50.0).clip(0.0, 1.0)

    return {
        "atr": atr,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "trend_atr": trend_atr,
        "return_window": return_window,
        "realized_volatility": realized_volatility,
        "momentum_risk_adjusted": momentum_risk_adjusted,
        "range_high": range_high,
        "range_low": range_low,
        "range_position": range_position,
        "volume_median": volume_median,
        "volume_ratio": volume_ratio,
        "trend_score": trend_score,
        "momentum_score": momentum_score,
        "range_score": range_score,
        "volume_score": volume_score,
        "volume_available": volume_available,
        "technical_score": technical_score,
        "component_agreement": component_agreement,
    }


def _bounded_score(values: pd.Series, *, scale: float) -> pd.Series:
    return pd.Series(
        50.0 + 50.0 * np.tanh(values.to_numpy(dtype=float) / scale),
        index=values.index,
        dtype=float,
    )
