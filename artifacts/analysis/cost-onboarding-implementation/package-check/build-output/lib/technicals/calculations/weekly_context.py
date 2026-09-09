from __future__ import annotations

import pandas as pd

from technicals.calculations.breakout_pressure import (
    FULL_HISTORY_MINIMUM_BARS as BREAKOUT_PRESSURE_FULL_HISTORY_MINIMUM,
)
from technicals.calculations.market_regime import (
    CALCULATION_VERSION as MARKET_REGIME_CALCULATION_VERSION,
    FULL_HISTORY_MINIMUM_BARS as MARKET_REGIME_FULL_HISTORY_MINIMUM,
    calculate_market_regime,
)
from technicals.calculations.session_aware_breakout import (
    CALCULATION_VERSION as BREAKOUT_PRESSURE_CALCULATION_VERSION,
    calculate_breakout_pressure,
)

CALCULATION_NAME = "weekly-context"
CALCULATION_VERSION = "1.0.0"
AVAILABILITY_RULE_VERSION = "xnys-final-session-close-plus-5m-v1"
EXCHANGE_CALENDAR = "XNYS"
PROCESSING_DELAY = pd.Timedelta(minutes=5)
SOURCE_TIMEFRAME = "1d"
OUTPUT_TIMEFRAME = "1w"
MINIMUM_COMPLETED_WEEKS = max(
    MARKET_REGIME_FULL_HISTORY_MINIMUM,
    BREAKOUT_PRESSURE_FULL_HISTORY_MINIMUM,
)
REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close")
WEEKLY_BAR_COLUMNS = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "week_start_session",
    "week_end_session",
    "bar_end_timestamp",
    "available_at",
    "constituent_session_count",
    "constituent_complete",
)
OUTPUT_COLUMNS = (
    "symbol",
    "provider",
    "timeframe",
    "source_timeframe",
    "exchange_calendar",
    "week_start_session",
    "week_end_session",
    "bar_timestamp",
    "bar_end_timestamp",
    "bar_complete",
    "available_at",
    "calculation",
    "calculation_version",
    "market_regime_calculation_version",
    "breakout_pressure_calculation_version",
    "availability_rule_version",
    "constituent_session_count",
    "constituent_complete",
    "technical_score",
    "technical_score_change_5",
    "breakout_readiness_score",
)


class WeeklyContextNotReady(ValueError):
    """Raised when canonical history is valid but not yet calculation-ready."""


def calculate_weekly_context(
    bars: pd.DataFrame,
    *,
    symbol: str,
    provider: str,
    timeframe: str,
) -> pd.DataFrame:
    """Build canonical exchange weeks and run existing technical calculators.

    Only weeks containing every eligible XNYS session are eligible. A weekly
    row becomes available five minutes after the actual close of its final
    session, including holiday-shortened weeks and early closes.
    """

    clean_provider = provider.strip().lower()
    clean_timeframe = str(timeframe or "").strip().lower()
    if clean_provider != "databento":
        raise ValueError("Weekly context requires canonical Databento daily bars.")
    if clean_timeframe != SOURCE_TIMEFRAME:
        raise ValueError("Weekly context requires canonical 1d source bars.")

    weekly_bars = aggregate_completed_exchange_weeks(bars)
    if len(weekly_bars) < MINIMUM_COMPLETED_WEEKS:
        raise WeeklyContextNotReady(
            "Weekly context requires at least "
            f"{MINIMUM_COMPLETED_WEEKS} complete exchange weeks; "
            f"received {len(weekly_bars)}."
        )

    market_regime = calculate_market_regime(
        weekly_bars,
        symbol=symbol,
        provider=clean_provider,
        timeframe=OUTPUT_TIMEFRAME,
    ).loc[
        :,
        ["timestamp", "technical_score", "technical_score_change_5"],
    ]
    breakout_pressure = calculate_breakout_pressure(
        weekly_bars,
        symbol=symbol,
        provider=clean_provider,
        timeframe=OUTPUT_TIMEFRAME,
    ).loc[:, ["timestamp", "breakout_readiness_score"]]

    values = market_regime.merge(
        breakout_pressure,
        on="timestamp",
        how="inner",
        validate="one_to_one",
    )
    context = weekly_bars.loc[
        :,
        [
            "timestamp",
            "week_start_session",
            "week_end_session",
            "bar_end_timestamp",
            "available_at",
            "constituent_session_count",
            "constituent_complete",
        ],
    ]
    result = values.merge(context, on="timestamp", how="inner", validate="one_to_one")
    if result.empty:
        raise ValueError("Weekly context produced no initialized technical rows.")

    result["symbol"] = symbol.strip().upper()
    result["provider"] = clean_provider
    result["timeframe"] = OUTPUT_TIMEFRAME
    result["source_timeframe"] = SOURCE_TIMEFRAME
    result["exchange_calendar"] = EXCHANGE_CALENDAR
    result["bar_timestamp"] = result["timestamp"]
    result["bar_complete"] = True
    result["calculation"] = CALCULATION_NAME
    result["calculation_version"] = CALCULATION_VERSION
    result["market_regime_calculation_version"] = (
        MARKET_REGIME_CALCULATION_VERSION
    )
    result["breakout_pressure_calculation_version"] = (
        BREAKOUT_PRESSURE_CALCULATION_VERSION
    )
    result["availability_rule_version"] = AVAILABILITY_RULE_VERSION
    return result.loc[:, OUTPUT_COLUMNS].reset_index(drop=True)


def aggregate_completed_exchange_weeks(bars: pd.DataFrame) -> pd.DataFrame:
    """Aggregate complete canonical daily history into exact XNYS weeks."""

    frame = _validated_daily_bars(bars)
    if frame.empty:
        return pd.DataFrame(columns=WEEKLY_BAR_COLUMNS)

    try:
        import exchange_calendars as xcals
    except ImportError as exc:
        raise RuntimeError(
            "exchange-calendars is required for canonical weekly context"
        ) from exc

    labels = (
        frame["timestamp"].dt.tz_convert("UTC").dt.tz_localize(None).dt.normalize()
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
            f"Weekly context found non-{EXCHANGE_CALENDAR} daily sessions: "
            + ", ".join(invalid[:10])
        )
    if labels.duplicated(keep=False).any():
        raise ValueError("Weekly context contains duplicate exchange sessions.")

    frame["_session_label"] = labels
    frame["_week_start"] = labels - pd.to_timedelta(labels.dt.weekday, unit="D")

    records: list[dict[str, object]] = []
    for week_start, group in frame.groupby("_week_start", sort=True):
        week_end = pd.Timestamp(week_start) + pd.Timedelta(days=6)
        expected = calendar.sessions_in_range(week_start, week_end)
        actual = pd.DatetimeIndex(group["_session_label"].sort_values())
        if expected.empty or not actual.equals(expected):
            continue

        ordered = group.sort_values("_session_label", kind="stable")
        first_session = expected[0]
        final_session = expected[-1]
        final_close = calendar.session_close(final_session)
        records.append(
            {
                "timestamp": _utc_session_label(first_session),
                "open": float(ordered.iloc[0]["open"]),
                "high": float(ordered["high"].max()),
                "low": float(ordered["low"].min()),
                "close": float(ordered.iloc[-1]["close"]),
                "volume": float(ordered["volume"].sum(min_count=1)),
                "week_start_session": _utc_session_label(first_session),
                "week_end_session": _utc_session_label(final_session),
                "bar_end_timestamp": final_close,
                "available_at": final_close + PROCESSING_DELAY,
                "constituent_session_count": len(expected),
                "constituent_complete": True,
            }
        )

    return pd.DataFrame.from_records(records, columns=WEEKLY_BAR_COLUMNS)


def _validated_daily_bars(bars: pd.DataFrame) -> pd.DataFrame:
    missing = [column for column in REQUIRED_COLUMNS if column not in bars.columns]
    if missing:
        raise ValueError(
            f"Weekly context input is missing columns: {', '.join(missing)}"
        )

    selected = [
        *REQUIRED_COLUMNS,
        *(
            column
            for column in ("volume", "bar_complete")
            if column in bars.columns
        ),
    ]
    frame = bars.loc[:, selected].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)

    if frame[list(REQUIRED_COLUMNS)].isna().any(axis=None):
        raise ValueError("Weekly context requires complete timestamp and OHLC evidence.")
    if frame["timestamp"].duplicated(keep=False).any():
        raise ValueError("Weekly context input contains duplicate bar timestamps.")
    if frame["high"].lt(frame["low"]).any():
        raise ValueError("Weekly context input contains a high below its low.")
    if "bar_complete" in frame.columns:
        complete = _explicit_true(frame["bar_complete"])
        if not bool(complete.all()):
            raise ValueError("Weekly context requires completed canonical daily bars.")

    return frame.sort_values("timestamp", kind="stable").reset_index(drop=True)


def _utc_session_label(value: pd.Timestamp) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _explicit_true(series: pd.Series) -> pd.Series:
    strings = series.astype("string").str.strip().str.lower()
    numeric = pd.to_numeric(series, errors="coerce")
    return (
        strings.isin({"true", "1", "yes"}) | numeric.eq(1)
    ).fillna(False)
