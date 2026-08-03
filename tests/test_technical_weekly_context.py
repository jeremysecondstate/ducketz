from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from technicals.calculations import (
    DEFAULT_CALCULATIONS,
    calculation_accepts_input,
)
from technicals.calculations.market_regime import calculate_market_regime
from technicals.calculations.session_aware_breakout import (
    calculate_breakout_pressure,
)
from technicals.calculations.weekly_context import (
    aggregate_completed_exchange_weeks,
    calculate_weekly_context,
)
from technicals.parquet_io import BarDataset, write_technical_parquet


def test_weekly_context_uses_existing_calculators_and_is_causal() -> None:
    bars = _daily_bars("2025-05-05", "2026-12-11")
    weekly = aggregate_completed_exchange_weeks(bars)
    result = calculate_weekly_context(
        bars,
        symbol="GOOG",
        provider="databento",
        timeframe="1d",
    )

    expected_market = calculate_market_regime(
        weekly,
        symbol="GOOG",
        provider="databento",
        timeframe="1w",
    ).loc[:, ["timestamp", "technical_score", "technical_score_change_5"]]
    expected_breakout = calculate_breakout_pressure(
        weekly,
        symbol="GOOG",
        provider="databento",
        timeframe="1w",
    ).loc[:, ["timestamp", "breakout_readiness_score"]]
    expected = expected_market.merge(
        expected_breakout,
        on="timestamp",
        validate="one_to_one",
    )
    observed = result.rename(columns={"bar_timestamp": "timestamp"}).loc[
        :,
        [
            "timestamp",
            "technical_score",
            "technical_score_change_5",
            "breakout_readiness_score",
        ],
    ]
    pdt.assert_frame_equal(
        observed.reset_index(drop=True),
        expected.reset_index(drop=True),
    )

    prefix_bars = bars.loc[
        bars["timestamp"].le(pd.Timestamp("2026-10-30T00:00:00Z"))
    ]
    prefix = calculate_weekly_context(
        prefix_bars,
        symbol="GOOG",
        provider="databento",
        timeframe="1d",
    )
    pdt.assert_frame_equal(
        prefix,
        result.iloc[: len(prefix)].reset_index(drop=True),
    )


def test_weekly_context_uses_holiday_and_early_close_schedule() -> None:
    bars = _daily_bars("2025-05-05", "2026-12-11")
    weekly = aggregate_completed_exchange_weeks(bars)

    thanksgiving = weekly.loc[
        weekly["week_end_session"].eq(pd.Timestamp("2026-11-27T00:00:00Z"))
    ].iloc[0]
    assert thanksgiving["constituent_session_count"] == 4
    assert thanksgiving["bar_end_timestamp"] == pd.Timestamp(
        "2026-11-27T18:00:00Z"
    )
    assert thanksgiving["available_at"] == pd.Timestamp("2026-11-27T18:05:00Z")

    independence_day = weekly.loc[
        weekly["week_end_session"].eq(pd.Timestamp("2026-07-02T00:00:00Z"))
    ].iloc[0]
    assert independence_day["constituent_session_count"] == 4
    assert independence_day["bar_end_timestamp"] == pd.Timestamp(
        "2026-07-02T20:00:00Z"
    )
    assert independence_day["available_at"] == pd.Timestamp(
        "2026-07-02T20:05:00Z"
    )

    incomplete = bars.loc[
        ~bars["timestamp"].eq(pd.Timestamp("2026-11-24T00:00:00Z"))
    ]
    incomplete_weekly = aggregate_completed_exchange_weeks(incomplete)
    assert not incomplete_weekly["week_end_session"].eq(
        pd.Timestamp("2026-11-27T00:00:00Z")
    ).any()


def test_weekly_context_persists_strict_schema_and_readable_id(
    tmp_path: Path,
) -> None:
    bars = _daily_bars("2025-05-05", "2026-12-11")
    result = calculate_weekly_context(
        bars,
        symbol="GOOG",
        provider="databento",
        timeframe="1d",
    )
    dataset = BarDataset(
        provider="databento",
        timeframe="1d",
        symbol="GOOG",
        frame=bars,
        source_files=(),
        adjustment_status="NO_SPLIT_EVENTS_IN_RANGE",
        split_event_count=0,
        split_events_json="[]",
    )
    path = write_technical_parquet(
        tmp_path / "technicals",
        calculation="weekly-context",
        dataset=dataset,
        frame=result,
    )
    assert path == (
        tmp_path
        / "technicals"
        / "weekly-context"
        / "databento"
        / "1w.parquet"
    )

    stored = pd.read_parquet(path)
    assert stored.columns.tolist() == [
        "id",
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
        "price_adjustment_status",
        "split_event_count",
        "constituent_session_count",
        "constituent_complete",
        "technical_score",
        "technical_score_change_5",
        "breakout_readiness_score",
    ]
    assert stored.columns.tolist().count("id") == 1
    assert stored["id"].is_unique
    assert stored["id"].str.startswith("GOOG|databento|1w|").all()
    assert stored["available_at"].equals(
        stored["bar_end_timestamp"] + pd.Timedelta(minutes=5)
    )
    assert stored["constituent_complete"].all()


def test_new_calculations_are_default_but_skip_inapplicable_inputs() -> None:
    assert {"bar-shape", "weekly-context"}.issubset(DEFAULT_CALCULATIONS)
    assert calculation_accepts_input(
        "bar-shape",
        provider="databento",
        timeframe="1h",
    )
    assert calculation_accepts_input(
        "weekly-context",
        provider="databento",
        timeframe="1d",
    )
    assert not calculation_accepts_input(
        "bar-shape",
        provider="schwab",
        timeframe="1d",
    )
    assert not calculation_accepts_input(
        "weekly-context",
        provider="databento",
        timeframe="1h",
    )


def test_weekly_context_rejects_noncanonical_inputs() -> None:
    bars = _daily_bars("2025-05-05", "2026-12-11")
    with pytest.raises(ValueError, match="canonical Databento"):
        calculate_weekly_context(
            bars,
            symbol="GOOG",
            provider="schwab",
            timeframe="1d",
        )

    duplicate = pd.concat([bars, bars.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate bar timestamps"):
        aggregate_completed_exchange_weeks(duplicate)


def test_weekly_context_fails_closed_before_full_calculator_history() -> None:
    short_history = _daily_bars("2026-03-02", "2026-12-11")
    with pytest.raises(ValueError, match="at least 60 complete exchange weeks"):
        calculate_weekly_context(
            short_history,
            symbol="GOOG",
            provider="databento",
            timeframe="1d",
        )


def _daily_bars(start: str, end: str) -> pd.DataFrame:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XNYS", start=start, end=end)
    sessions = calendar.sessions_in_range(start, end)
    timestamps = pd.DatetimeIndex(sessions).tz_localize("UTC")
    row = np.arange(len(timestamps), dtype=float)
    open_price = 90.0 + row * 0.11 + np.sin(row / 7.0)
    close = open_price + np.where(row % 2 == 0, 0.8, -0.5)
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": open_price,
            "high": np.maximum(open_price, close) + 0.65,
            "low": np.minimum(open_price, close) - 0.60,
            "close": close,
            "volume": 500_000.0 + row * 1_250.0,
            "bar_complete": True,
        }
    )
