from __future__ import annotations

import re

import numpy as np
import pandas as pd

from datafetching.bar_timing import bar_end_timestamps

CALCULATION_NAME = "bar-shape"
CALCULATION_VERSION = "1.0.0"
AVAILABILITY_RULE_VERSION = "completed-bar-plus-5m-v1"
PROCESSING_DELAY = pd.Timedelta(minutes=5)
ATR_PERIOD = 14
ATR_MIN_PERIODS = 14
EXCHANGE_CALENDAR = "XNYS"
REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close")
OUTPUT_COLUMNS = (
    "symbol",
    "provider",
    "timeframe",
    "bar_timestamp",
    "bar_end_timestamp",
    "bar_complete",
    "available_at",
    "calculation",
    "calculation_version",
    "overnight_gap_atr",
    "intrabar_range_atr",
    "close_location",
)

_TIMEFRAME_PATTERN = re.compile(r"^[1-9]\d*(?:s|m|h|d)$")


def calculate_bar_shape(
    bars: pd.DataFrame,
    *,
    symbol: str,
    provider: str,
    timeframe: str,
) -> pd.DataFrame:
    """Calculate causal, price-scale-free shape values for completed bars.

    ATR uses the same causal Wilder-style exponentially weighted definition as
    the existing technical calculations. It includes the current completed bar,
    which is known at this row's decision time, and never uses a later row.

    ``overnight_gap_atr`` is intentionally populated only for daily bars.
    Intraday rows retain it as missing rather than relabeling an ordinary
    between-bar move as an overnight gap.
    """

    clean_timeframe = str(timeframe or "").strip().lower()
    if _TIMEFRAME_PATTERN.fullmatch(clean_timeframe) is None:
        raise ValueError(f"Bar shape does not support timeframe {timeframe!r}.")

    frame = _validated_bars(bars)
    bar_end = _safe_bar_end_timestamps(frame, timeframe=clean_timeframe)

    previous_close = frame["close"].shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.ewm(
        alpha=1 / ATR_PERIOD,
        adjust=False,
        min_periods=ATR_MIN_PERIODS,
    ).mean()
    atr_denominator = atr.where(atr > 0)

    overnight_gap = pd.Series(np.nan, index=frame.index, dtype=float)
    if clean_timeframe == "1d":
        overnight_gap = (frame["open"] - previous_close) / atr_denominator

    intrabar_range = (frame["high"] - frame["low"]) / atr_denominator
    bar_range = (frame["high"] - frame["low"]).where(
        frame["high"] > frame["low"]
    )
    close_location = ((frame["close"] - frame["low"]) / bar_range).clip(0.0, 1.0)

    result = pd.DataFrame(
        {
            "symbol": symbol.strip().upper(),
            "provider": provider.strip().lower(),
            "timeframe": clean_timeframe,
            "bar_timestamp": frame["timestamp"],
            "bar_end_timestamp": bar_end,
            "bar_complete": True,
            "available_at": bar_end + PROCESSING_DELAY,
            "calculation": CALCULATION_NAME,
            "calculation_version": CALCULATION_VERSION,
            "overnight_gap_atr": overnight_gap,
            "intrabar_range_atr": intrabar_range,
            "close_location": close_location,
        }
    )
    return result.loc[:, OUTPUT_COLUMNS]


def _validated_bars(bars: pd.DataFrame) -> pd.DataFrame:
    missing = [column for column in REQUIRED_COLUMNS if column not in bars.columns]
    if missing:
        raise ValueError(f"Bar shape input is missing columns: {', '.join(missing)}")

    frame = bars.loc[
        :,
        [
            *REQUIRED_COLUMNS,
            *(
                column
                for column in ("bar_end_timestamp", "bar_complete")
                if column in bars.columns
            ),
        ],
    ].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if frame[list(REQUIRED_COLUMNS)].isna().any(axis=None):
        raise ValueError("Bar shape requires complete timestamp and OHLC evidence.")
    if frame["timestamp"].duplicated(keep=False).any():
        raise ValueError("Bar shape input contains duplicate bar timestamps.")
    if frame["high"].lt(frame["low"]).any():
        raise ValueError("Bar shape input contains a high below its low.")

    if "bar_complete" in frame.columns:
        complete = _explicit_true(frame["bar_complete"])
        if not bool(complete.all()):
            raise ValueError("Bar shape requires completed canonical bars.")

    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def _safe_bar_end_timestamps(
    frame: pd.DataFrame,
    *,
    timeframe: str,
) -> pd.Series:
    if timeframe == "1d":
        return _official_daily_closes(frame["timestamp"])

    if "bar_end_timestamp" in frame.columns:
        provided = pd.to_datetime(
            frame["bar_end_timestamp"], utc=True, errors="coerce"
        )
        if provided.notna().all():
            return provided.reset_index(drop=True)

    derived = bar_end_timestamps(frame["timestamp"], timeframe)
    if derived.isna().any():
        raise ValueError(f"Could not derive bar ends for timeframe {timeframe}.")
    return derived


def _official_daily_closes(timestamps: pd.Series) -> pd.Series:
    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise RuntimeError(
            "exchange-calendars is required for daily bar-shape timing"
        ) from exc

    labels = (
        timestamps.dt.tz_convert("UTC").dt.tz_localize(None).dt.normalize()
    )
    calendar = xcals.get_calendar(
        EXCHANGE_CALENDAR,
        start=labels.min() - pd.Timedelta(days=14),
        end=labels.max() + pd.Timedelta(days=14),
    )
    invalid = sorted(
        label.date().isoformat() for label in labels if label not in calendar.sessions
    )
    if invalid:
        raise ValueError(
            f"Daily bar-shape timestamps are not {EXCHANGE_CALENDAR} sessions: "
            + ", ".join(invalid[:10])
        )
    return pd.Series(
        [calendar.session_close(label) for label in labels],
        index=timestamps.index,
        dtype="datetime64[ns, UTC]",
    )


def _explicit_true(series: pd.Series) -> pd.Series:
    strings = series.astype("string").str.strip().str.lower()
    numeric = pd.to_numeric(series, errors="coerce")
    return (
        strings.isin({"true", "1", "yes"}) | numeric.eq(1)
    ).fillna(False)
