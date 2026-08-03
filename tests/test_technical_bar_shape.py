from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pandas.testing as pdt
import pytest

from technicals.calculations.bar_shape import (
    ATR_MIN_PERIODS,
    calculate_bar_shape,
)
from technicals.parquet_io import BarDataset, write_technical_parquet
from technicals.split_adjustments import SplitEvent, apply_split_adjustments


def test_bar_shape_is_causal_and_keeps_initialization_missing() -> None:
    bars = _daily_bars("2026-01-05", "2026-02-13")
    zero_range_index = 16
    zero_price = float(bars.loc[zero_range_index, "close"])
    bars.loc[
        zero_range_index,
        ["open", "high", "low", "close"],
    ] = zero_price

    full = calculate_bar_shape(
        bars,
        symbol="GOOG",
        provider="databento",
        timeframe="1d",
    )
    prefix = calculate_bar_shape(
        bars.iloc[:22],
        symbol="GOOG",
        provider="databento",
        timeframe="1d",
    )

    pdt.assert_frame_equal(
        prefix,
        full.iloc[: len(prefix)].reset_index(drop=True),
    )
    assert full.loc[: ATR_MIN_PERIODS - 2, "intrabar_range_atr"].isna().all()
    assert pd.notna(full.loc[ATR_MIN_PERIODS - 1, "intrabar_range_atr"])
    assert pd.isna(full.loc[0, "overnight_gap_atr"])
    assert pd.isna(full.loc[zero_range_index, "close_location"])
    assert full["available_at"].equals(
        full["bar_end_timestamp"] + pd.Timedelta(minutes=5)
    )


def test_bar_shape_uses_split_adjusted_prices_and_actual_early_close(
    tmp_path: Path,
) -> None:
    bars = _daily_bars("2026-10-19", "2026-12-04", constant_price=200.0)
    split_index = 15
    split_session = bars.loc[split_index, "timestamp"]
    bars.loc[:, ["open", "close"]] = 200.0
    bars.loc[:, "high"] = 201.0
    bars.loc[:, "low"] = 199.0
    bars.loc[:, "volume"] = 1_000.0
    bars.loc[split_index:, ["open", "close"]] = 100.0
    bars.loc[split_index:, "high"] = 101.0
    bars.loc[split_index:, "low"] = 99.0
    bars.loc[split_index:, "volume"] = 2_000.0

    adjustment = apply_split_adjustments(
        bars,
        events=(
            SplitEvent(
                ex_date=split_session,
                numerator=2.0,
                denominator=1.0,
                source_file=Path("split-fixture.parquet"),
            ),
        ),
        provider="databento",
        timeframe="1d",
    )
    assert adjustment.status == "SPLIT_ADJUSTED"

    result = calculate_bar_shape(
        adjustment.frame,
        symbol="GOOG",
        provider="databento",
        timeframe="1d",
    )
    assert result.loc[split_index, "overnight_gap_atr"] == pytest.approx(0.0)

    early_close = result.loc[
        result["bar_timestamp"].eq(pd.Timestamp("2026-11-27T00:00:00Z"))
    ].iloc[0]
    assert early_close["bar_end_timestamp"] == pd.Timestamp(
        "2026-11-27T18:00:00Z"
    )
    assert early_close["available_at"] == pd.Timestamp("2026-11-27T18:05:00Z")

    dataset = BarDataset(
        provider="databento",
        timeframe="1d",
        symbol="GOOG",
        frame=adjustment.frame,
        source_files=(),
        adjustment_status=adjustment.status,
        split_event_count=adjustment.event_count,
        split_events_json=adjustment.metadata_json,
    )
    path = write_technical_parquet(
        tmp_path / "technicals",
        calculation="bar-shape",
        dataset=dataset,
        frame=result,
    )
    assert path == (
        tmp_path
        / "technicals"
        / "bar-shape"
        / "databento"
        / "1d.parquet"
    )

    stored = pd.read_parquet(path)
    assert stored.columns.tolist() == [
        "id",
        "symbol",
        "provider",
        "timeframe",
        "bar_timestamp",
        "bar_end_timestamp",
        "bar_complete",
        "available_at",
        "calculation",
        "calculation_version",
        "price_adjustment_status",
        "split_event_count",
        "overnight_gap_atr",
        "intrabar_range_atr",
        "close_location",
    ]
    assert stored.columns.tolist().count("id") == 1
    assert stored["id"].is_unique
    assert stored["id"].str.startswith("GOOG|databento|1d|").all()
    assert stored["split_event_count"].eq(1).all()
    assert stored["price_adjustment_status"].eq("SPLIT_ADJUSTED").all()


def test_bar_shape_rejects_incomplete_or_duplicate_input() -> None:
    bars = _daily_bars("2026-01-05", "2026-01-30")
    bars.loc[3, "bar_complete"] = False
    with pytest.raises(ValueError, match="completed canonical bars"):
        calculate_bar_shape(
            bars,
            symbol="GOOG",
            provider="databento",
            timeframe="1d",
        )

    duplicate = pd.concat([bars.drop(columns="bar_complete"), bars.iloc[[0]]])
    with pytest.raises(ValueError, match="duplicate bar timestamps"):
        calculate_bar_shape(
            duplicate,
            symbol="GOOG",
            provider="databento",
            timeframe="1d",
        )


def test_hourly_bar_shape_uses_completed_bar_end_and_does_not_fake_overnight_gap() -> None:
    timestamps = pd.date_range(
        "2026-07-27T13:30:00Z",
        periods=18,
        freq="1h",
    )
    row = np.arange(len(timestamps), dtype=float)
    bars = pd.DataFrame(
        {
            "timestamp": timestamps,
            "bar_end_timestamp": timestamps + pd.Timedelta(hours=1),
            "bar_complete": True,
            "open": 100.0 + row,
            "high": 101.0 + row,
            "low": 99.0 + row,
            "close": 100.5 + row,
        }
    )
    result = calculate_bar_shape(
        bars,
        symbol="GOOG",
        provider="databento",
        timeframe="1h",
    )

    assert result["overnight_gap_atr"].isna().all()
    assert result["bar_end_timestamp"].equals(bars["bar_end_timestamp"])
    assert result["available_at"].equals(
        bars["bar_end_timestamp"] + pd.Timedelta(minutes=5)
    )


def _daily_bars(
    start: str,
    end: str,
    *,
    constant_price: float | None = None,
) -> pd.DataFrame:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XNYS", start=start, end=end)
    sessions = calendar.sessions_in_range(start, end)
    timestamps = pd.DatetimeIndex(sessions).tz_localize("UTC")
    row = np.arange(len(timestamps), dtype=float)
    base = (
        np.full(len(timestamps), constant_price, dtype=float)
        if constant_price is not None
        else 100.0 + row * 0.4
    )
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": base,
            "high": base + 1.0,
            "low": base - 1.0,
            "close": base + np.where(row % 2 == 0, 0.25, -0.20),
            "volume": 1_000.0 + row * 10.0,
            "bar_complete": True,
        }
    )
