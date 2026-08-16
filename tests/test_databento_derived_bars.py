from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from app.models.market_data import MarketBar
from app.services.market_fetch_specs import DatabentoAnalysisSourceSpec
from datafetching import databento_fetch
from datafetching.continuation import normalized_bar_path
from datafetching.derived_bars import (
    DERIVED_INTRADAY_FREQUENCIES,
    derive_daily_bars,
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


def test_daily_fallback_accepts_provider_omitted_no_trade_minute() -> None:
    complete = _minute_bars("2026-08-04T13:30:00Z", periods=390)

    derived = derive_daily_bars(
        "NVDA",
        complete,
        as_of=pd.Timestamp("2026-08-04T20:00:00Z"),
    )

    assert len(derived) == 1
    assert derived[0].timestamp == pd.Timestamp(
        "2026-08-04T00:00:00Z"
    ).to_pydatetime()
    assert derived[0].bar_end_timestamp == pd.Timestamp(
        "2026-08-04T20:00:00Z"
    ).to_pydatetime()
    assert derived[0].source_bar_count == 390
    assert derived[0].open == complete[0].open
    assert derived[0].close == complete[-1].close

    missing_minute = complete[:100] + complete[101:]
    sparse = derive_daily_bars(
        "NVDA",
        missing_minute,
        as_of=pd.Timestamp("2026-08-04T20:01:00Z"),
    )
    assert len(sparse) == 1
    assert sparse[0].source_bar_count == 389


def test_daily_fallback_rejects_stale_session_tail() -> None:
    stale_tail = _minute_bars("2026-08-04T13:30:00Z", periods=200)

    assert derive_daily_bars(
        "NVDA",
        stale_tail,
        as_of=pd.Timestamp("2026-08-04T20:01:00Z"),
    ) == []


def test_daily_fallback_uses_early_close_session_length() -> None:
    bars = _minute_bars("2026-11-27T14:30:00Z", periods=210)

    derived = derive_daily_bars(
        "NVDA",
        bars,
        as_of=pd.Timestamp("2026-11-27T18:00:00Z"),
    )

    assert len(derived) == 1
    assert derived[0].bar_end_timestamp == pd.Timestamp(
        "2026-11-27T18:00:00Z"
    ).to_pydatetime()
    assert derived[0].source_bar_count == 210


def test_databento_fetch_persists_daily_fallback_once_after_close(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    minute_spec = DatabentoAnalysisSourceSpec(
        key="source_1000d_1m",
        schema="ohlcv-1m",
        frequency="1m",
        lookback=pd.Timedelta(days=1000),
    )
    daily_spec = DatabentoAnalysisSourceSpec(
        key="source_2920d_1d",
        schema="ohlcv-1d",
        frequency="1d",
        lookback=pd.Timedelta(days=2920),
    )
    observed_at = pd.Timestamp("2026-08-04T20:01:00Z")
    minute_request_key = "source_1000d_1m_ohlcv-1m_1m"
    store.save_bars(
        "databento",
        "NVDA",
        "1m",
        _minute_bars("2026-08-04T13:30:00Z", periods=390),
        request_key=minute_request_key,
        as_of=observed_at,
    )

    first = databento_fetch._save_derived_daily_bars(
        "NVDA",
        store,
        provider=SimpleNamespace(dataset="EQUS.MINI"),
        profile="continuation",
        minute_source_spec=minute_spec,
        daily_source_spec=daily_spec,
        observed_at=observed_at.to_pydatetime(),
    )
    second = databento_fetch._save_derived_daily_bars(
        "NVDA",
        store,
        provider=SimpleNamespace(dataset="EQUS.MINI"),
        profile="continuation",
        minute_source_spec=minute_spec,
        daily_source_spec=daily_spec,
        observed_at=observed_at.to_pydatetime(),
    )

    path = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="NVDA",
        timeframe="1d",
        request_key="derived_1m_1d",
    )
    stored = pd.read_parquet(path)
    assert first == (1, 0)
    assert second == (0, 0)
    assert stored["timestamp"].tolist() == [
        pd.Timestamp("2026-08-04T00:00:00Z")
    ]


def test_native_daily_wins_duplicate_while_derived_daily_fills_lag(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    as_of = pd.Timestamp("2024-07-31T00:00:00Z")
    store.save_bars(
        "databento",
        "NVDA",
        "1d",
        [
            _day_bar("2024-07-29T00:00:00Z", close=101.0),
            _day_bar("2024-07-30T00:00:00Z", close=102.0),
        ],
        request_key="derived_1m_1d",
        as_of=as_of,
    )
    store.save_bars(
        "databento",
        "NVDA",
        "1d",
        [_day_bar("2024-07-29T00:00:00Z", close=201.0)],
        request_key="source_2920d_1d_ohlcv-1d_1d",
        as_of=as_of,
    )

    datasets = discover_bar_datasets(
        tmp_path,
        symbol="NVDA",
        providers=("databento",),
        timeframes=("1d",),
    )

    assert len(datasets) == 1
    frame = datasets[0].frame.set_index("timestamp")
    assert frame.loc[pd.Timestamp("2024-07-29T00:00:00Z"), "close"] == 201.0
    assert frame.loc[pd.Timestamp("2024-07-30T00:00:00Z"), "close"] == 102.0


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


def _day_bar(timestamp: str, *, close: float) -> MarketBar:
    return MarketBar(
        symbol="NVDA",
        source="databento",
        timeframe="1d",
        timestamp=pd.Timestamp(timestamp).to_pydatetime(),
        open=100.0,
        high=max(100.0, close) + 1.0,
        low=min(100.0, close) - 1.0,
        close=close,
        volume=1_000_000.0,
    )
