from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.models.market_data import MarketBar
from datafetching.derived_bars import (
    DERIVED_INTRADAY_FREQUENCIES,
    derive_intraday_bars,
)
from datafetching.parquet_store import ParquetStore
from technicals.parquet_io import discover_bar_datasets


def test_hourly_fallback_requires_one_complete_sixty_minute_window() -> None:
    bars = _minute_bars("2026-08-03T13:30:00Z", periods=90)

    derived = derive_intraday_bars(
        "NVDA",
        bars,
        "1h",
        as_of=pd.Timestamp("2026-08-03T15:00:00Z"),
    )

    assert "1h" in DERIVED_INTRADAY_FREQUENCIES
    assert len(derived) == 1
    assert derived[0].timestamp == pd.Timestamp(
        "2026-08-03T14:00:00Z"
    ).to_pydatetime()
    assert derived[0].bar_end_timestamp == pd.Timestamp(
        "2026-08-03T15:00:00Z"
    ).to_pydatetime()
    assert derived[0].source_bar_count == 60


def test_hourly_fallback_rejects_partial_continuation_tail() -> None:
    bars = _minute_bars("2026-08-03T14:09:00Z", periods=51)

    derived = derive_intraday_bars(
        "NVDA",
        bars,
        "1h",
        as_of=pd.Timestamp("2026-08-03T15:15:00Z"),
    )

    assert derived == []


def test_native_hour_wins_duplicates_while_derived_hour_fills_lag(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    as_of = pd.Timestamp("2024-07-29T17:30:00Z")
    derived = [
        _hour_bar("2024-07-29T14:00:00Z", close=101.0),
        _hour_bar("2024-07-29T15:00:00Z", close=102.0),
    ]
    native = [_hour_bar("2024-07-29T14:00:00Z", close=201.0)]

    store.save_bars(
        "databento",
        "NVDA",
        "1h",
        derived,
        request_key="derived_1m_1h",
        as_of=as_of,
    )
    store.save_bars(
        "databento",
        "NVDA",
        "1h",
        native,
        request_key="source_2000d_1h_ohlcv-1h_1h",
        as_of=as_of,
    )

    datasets = discover_bar_datasets(
        tmp_path,
        symbol="NVDA",
        providers=("databento",),
        timeframes=("1h",),
    )

    assert len(datasets) == 1
    frame = datasets[0].frame.set_index("timestamp")
    assert frame.loc[pd.Timestamp("2024-07-29T14:00:00Z"), "close"] == 201.0
    assert frame.loc[pd.Timestamp("2024-07-29T15:00:00Z"), "close"] == 102.0


def _minute_bars(start: str, *, periods: int) -> list[MarketBar]:
    timestamps = pd.date_range(start, periods=periods, freq="1min")
    return [
        MarketBar(
            symbol="NVDA",
            source="databento",
            timeframe="1m",
            timestamp=timestamp.to_pydatetime(),
            open=100.0 + index / 100.0,
            high=100.2 + index / 100.0,
            low=99.8 + index / 100.0,
            close=100.1 + index / 100.0,
            volume=1000.0 + index,
        )
        for index, timestamp in enumerate(timestamps)
    ]


def _hour_bar(timestamp: str, *, close: float) -> MarketBar:
    return MarketBar(
        symbol="NVDA",
        source="databento",
        timeframe="1h",
        timestamp=pd.Timestamp(timestamp).to_pydatetime(),
        open=100.0,
        high=max(100.0, close) + 1.0,
        low=min(100.0, close) - 1.0,
        close=close,
        volume=10_000.0,
    )
