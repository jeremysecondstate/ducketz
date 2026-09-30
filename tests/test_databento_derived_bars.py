from __future__ import annotations

from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from app.models.market_data import MarketBar
from app.services.market_fetch_specs import DatabentoAnalysisSourceSpec
from datafetching import databento_fetch
from datafetching.bar_consolidation import consolidate_shadowed_derived_bars
from datafetching.continuation import normalized_bar_path
from datafetching.derived_bars import (
    DERIVED_INTRADAY_FREQUENCIES,
    derive_daily_bars,
    derive_intraday_bars,
)
from datafetching.parquet_store import ParquetStore
from technicals.parquet_io import discover_bar_datasets


def test_consolidation_removes_only_native_shadowed_derived_rows(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    as_of = pd.Timestamp("2026-09-03T23:30:00Z")
    store.save_bars(
        "databento",
        "NVDA",
        "1h",
        [
            _hour_bar("2026-09-03T20:00:00Z", close=200.0),
            _hour_bar("2026-09-03T21:00:00Z", close=201.0),
        ],
        request_key="source_1825d_1h_ohlcv-1h_1h",
        as_of=as_of,
    )
    store.save_bars(
        "databento",
        "NVDA",
        "1h",
        [
            _hour_bar("2026-09-03T21:00:00Z", close=101.0),
            _hour_bar("2026-09-03T22:00:00Z", close=102.0),
        ],
        request_key="derived_1m_1h",
        as_of=as_of,
    )

    results = consolidate_shadowed_derived_bars(
        tmp_path,
        symbol="NVDA",
        timeframes=("1h",),
    )

    assert len(results) == 1
    assert results[0].shadowed_rows_removed == 1
    assert results[0].derived_rows_retained == 1
    retained = pd.read_parquet(results[0].derived_path)
    assert retained["timestamp"].tolist() == [
        pd.Timestamp("2026-09-03T22:00:00Z")
    ]
    datasets = discover_bar_datasets(
        tmp_path,
        symbol="NVDA",
        providers=("databento",),
        timeframes=("1h",),
    )
    assert datasets[0].frame["close"].tolist() == [200.0, 201.0, 102.0]


def test_consolidation_removes_empty_derived_bridge(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    as_of = pd.Timestamp("2026-09-03T22:30:00Z")
    bar = _hour_bar("2026-09-03T21:00:00Z", close=201.0)
    store.save_bars(
        "databento",
        "NVDA",
        "1h",
        [bar],
        request_key="source_1825d_1h_ohlcv-1h_1h",
        as_of=as_of,
    )
    store.save_bars(
        "databento",
        "NVDA",
        "1h",
        [bar],
        request_key="derived_1m_1h",
        as_of=as_of,
    )

    result = consolidate_shadowed_derived_bars(
        tmp_path,
        symbol="NVDA",
        timeframes=("1h",),
    )[0]

    assert result.shadowed_rows_removed == 1
    assert result.derived_rows_retained == 0
    assert result.bytes_after == 0
    assert not result.derived_path.exists()


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


def test_hourly_fallback_aggregates_sparse_trades_with_proven_coverage() -> None:
    bars = [
        _minute_bar("2026-09-03T20:02:00Z", close=101.0, volume=200.0),
        _minute_bar("2026-09-03T20:47:00Z", close=103.0, volume=300.0),
    ]

    derived = derive_intraday_bars(
        "AMZN",
        bars,
        "1h",
        as_of=pd.Timestamp("2026-09-03T21:05:00Z"),
        coverage_start=pd.Timestamp("2026-09-03T20:00:00Z"),
        coverage_end=pd.Timestamp("2026-09-03T21:00:00Z"),
    )

    assert len(derived) == 1
    assert derived[0].timestamp == pd.Timestamp(
        "2026-09-03T20:00:00Z"
    ).to_pydatetime()
    assert derived[0].open == 100.9
    assert derived[0].high == 103.2
    assert derived[0].low == 100.8
    assert derived[0].close == 103.0
    assert derived[0].volume == 500.0
    assert derived[0].source_bar_count == 2


def test_hourly_fallback_carries_prior_close_without_future_or_trailing_fill() -> None:
    bars = [
        _minute_bar("2026-09-03T20:10:00Z", close=101.0),
        _minute_bar("2026-09-03T22:10:00Z", close=999.0),
    ]

    derived = derive_intraday_bars(
        "SNDK",
        bars,
        "1h",
        as_of=pd.Timestamp("2026-09-03T22:30:00Z"),
        coverage_start=pd.Timestamp("2026-09-03T20:00:00Z"),
        coverage_end=pd.Timestamp("2026-09-03T23:00:00Z"),
    )

    assert [bar.timestamp for bar in derived] == list(
        pd.to_datetime(
            ["2026-09-03T20:00:00Z", "2026-09-03T21:00:00Z"],
            utc=True,
        ).to_pydatetime()
    )
    no_trade = derived[1]
    assert (no_trade.open, no_trade.high, no_trade.low, no_trade.close) == (
        101.0,
        101.0,
        101.0,
        101.0,
    )
    assert no_trade.volume == 0.0
    assert no_trade.source_bar_count == 0


def test_hourly_fallback_omits_partial_leading_coverage_hour() -> None:
    bars = [_minute_bar("2026-09-03T20:20:00Z", close=101.0)]

    derived = derive_intraday_bars(
        "AMZN",
        bars,
        "1h",
        as_of=pd.Timestamp("2026-09-03T22:00:00Z"),
        coverage_start=pd.Timestamp("2026-09-03T20:15:00Z"),
        coverage_end=pd.Timestamp("2026-09-03T22:00:00Z"),
    )

    assert [bar.timestamp for bar in derived] == [
        pd.Timestamp("2026-09-03T21:00:00Z").to_pydatetime()
    ]
    assert derived[0].source_bar_count == 0


def test_hourly_fallback_retains_continuous_nine_eastern_source_hour() -> None:
    bars = [
        _minute_bar("2026-09-03T12:15:00Z", close=101.0),
        _minute_bar("2026-09-03T14:15:00Z", close=103.0),
    ]

    derived = derive_intraday_bars(
        "AMZN",
        bars,
        "1h",
        as_of=pd.Timestamp("2026-09-03T15:05:00Z"),
        coverage_start=pd.Timestamp("2026-09-03T12:00:00Z"),
        coverage_end=pd.Timestamp("2026-09-03T15:00:00Z"),
    )

    assert [bar.timestamp for bar in derived] == list(
        pd.to_datetime(
            [
                "2026-09-03T12:00:00Z",
                "2026-09-03T13:00:00Z",
                "2026-09-03T14:00:00Z",
            ],
            utc=True,
        ).to_pydatetime()
    )
    boundary_hour = derived[1]
    assert boundary_hour.close == 101.0
    assert boundary_hour.source_bar_count == 0


def test_hourly_fallback_never_synthesizes_weekends_or_holidays() -> None:
    prior = [_minute_bar("2026-09-04T23:30:00Z", close=101.0)]

    weekend = derive_intraday_bars(
        "AMZN",
        prior,
        "1h",
        as_of=pd.Timestamp("2026-09-06T00:00:00Z"),
        coverage_start=pd.Timestamp("2026-09-05T08:00:00Z"),
        coverage_end=pd.Timestamp("2026-09-06T00:00:00Z"),
    )
    labor_day = derive_intraday_bars(
        "AMZN",
        prior,
        "1h",
        as_of=pd.Timestamp("2026-09-08T00:00:00Z"),
        coverage_start=pd.Timestamp("2026-09-07T08:00:00Z"),
        coverage_end=pd.Timestamp("2026-09-08T00:00:00Z"),
    )

    assert weekend == []
    assert labor_day == []


def test_hourly_fallback_uses_only_regular_hours_on_early_close() -> None:
    bars = [_minute_bar("2026-11-27T14:30:00Z", close=101.0)]

    derived = derive_intraday_bars(
        "AMZN",
        bars,
        "1h",
        as_of=pd.Timestamp("2026-11-28T01:00:00Z"),
        coverage_start=pd.Timestamp("2026-11-27T14:00:00Z"),
        coverage_end=pd.Timestamp("2026-11-28T01:00:00Z"),
    )

    assert [bar.timestamp for bar in derived] == list(
        pd.to_datetime(
            [
                "2026-11-27T15:00:00Z",
                "2026-11-27T16:00:00Z",
                "2026-11-27T17:00:00Z",
            ],
            utc=True,
        ).to_pydatetime()
    )


def test_successful_minute_selected_range_reaches_hourly_derivation(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    spec = DatabentoAnalysisSourceSpec(
        key="source_100d_1m",
        schema="ohlcv-1m",
        frequency="1m",
        lookback=pd.Timedelta(days=100),
    )
    selected_range = SimpleNamespace(
        start=pd.Timestamp("2026-09-03T20:00:00Z").to_pydatetime(),
        end=pd.Timestamp("2026-09-03T21:00:00Z").to_pydatetime(),
    )
    bars = [
        _minute_bar("2026-09-03T20:02:00Z", close=101.0),
        _minute_bar("2026-09-03T20:47:00Z", close=103.0),
    ]

    result = databento_fetch._persist_native_results(
        "AMZN",
        store,
        provider=SimpleNamespace(dataset="EQUS.MINI"),
        profile="continuation",
        observed_at=pd.Timestamp("2026-09-03T21:05:00Z").to_pydatetime(),
        native_results=((spec, bars, None, selected_range, None),),
        skip_native_frequencies=frozenset(("1m",)),
    )

    path = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="AMZN",
        timeframe="1h",
        request_key="derived_1m_1h",
    )
    stored = pd.read_parquet(path)
    assert result.error_files == 0
    assert stored["timestamp"].tolist() == [
        pd.Timestamp("2026-09-03T20:00:00Z")
    ]
    assert stored.iloc[0]["close"] == 103.0


def test_covered_half_hours_aggregate_only_observed_trades_without_empty_fill() -> None:
    bars = [
        _minute_bar("2026-09-03T20:02:00Z", close=101.0, volume=200.0),
        _minute_bar("2026-09-03T20:27:00Z", close=103.0, volume=300.0),
        _minute_bar("2026-09-03T21:32:00Z", close=99.0, volume=50.0),
    ]
    options = dict(as_of=pd.Timestamp("2026-09-03T22:05:00Z"))
    assert derive_intraday_bars("RR", bars, "30m", **options) == []
    derived = derive_intraday_bars(
        "RR", bars, "30m", **options,
        coverage_start=pd.Timestamp("2026-09-03T20:00:00Z"),
        coverage_end=pd.Timestamp("2026-09-03T22:00:00Z"),
    )
    assert [bar.timestamp for bar in derived] == list(pd.to_datetime(
        ["2026-09-03T20:00:00Z", "2026-09-03T21:30:00Z"], utc=True).to_pydatetime())
    first = derived[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (
        100.9, 103.2, 100.8, 103.0, 500.0)
    assert [bar.source_bar_count for bar in derived] == [2, 1]
    assert all(bar.bar_complete and bar.timeframe == "30m" for bar in derived)
    assert first.bar_end_timestamp == pd.Timestamp("2026-09-03T20:30:00Z")


@pytest.mark.parametrize("observed,expected", [
    ("2026-09-03T21:20:00Z", ["2026-09-03T20:30:00Z"]),
    ("2026-09-03T22:00:00Z", ["2026-09-03T20:30:00Z", "2026-09-03T21:00:00Z"]),
])
def test_covered_half_hours_exclude_partial_request_edges_and_open_intervals(observed, expected) -> None:
    bars = [_minute_bar(stamp, close=101.0) for stamp in (
        "2026-09-03T20:20:00Z", "2026-09-03T20:47:00Z",
        "2026-09-03T21:17:00Z", "2026-09-03T21:32:00Z")]
    derived = derive_intraday_bars(
        "RR", bars, "30m", as_of=pd.Timestamp(observed),
        coverage_start=pd.Timestamp("2026-09-03T20:15:00Z"),
        coverage_end=pd.Timestamp("2026-09-03T21:45:00Z"),
    )
    assert [bar.timestamp for bar in derived] == list(pd.to_datetime(expected, utc=True).to_pydatetime())


@pytest.mark.parametrize("day", ["2026-09-05", "2026-09-07"])
def test_covered_half_hours_exclude_weekends_and_holidays_even_with_rows(day) -> None:
    start = pd.Timestamp(day, tz="UTC")
    assert derive_intraday_bars(
        "RR", [_minute_bar(str(start + pd.Timedelta(hours=15)), close=100.)], "30m",
        as_of=start + pd.Timedelta(days=1), coverage_start=start,
        coverage_end=start + pd.Timedelta(days=1),
    ) == []


def test_covered_half_hours_obey_early_close_and_regular_open_boundary() -> None:
    bars = [_minute_bar(stamp, close=100.) for stamp in (
        "2026-11-27T14:15:00Z", "2026-11-27T14:35:00Z",
        "2026-11-27T17:55:00Z", "2026-11-27T18:10:00Z")]
    derived = derive_intraday_bars(
        "RR", bars, "30m", as_of=pd.Timestamp("2026-11-28T01:00:00Z"),
        coverage_start=pd.Timestamp("2026-11-27T14:00:00Z"),
        coverage_end=pd.Timestamp("2026-11-28T01:00:00Z"),
    )
    assert [bar.timestamp for bar in derived] == list(pd.to_datetime(
        ["2026-11-27T14:30:00Z", "2026-11-27T17:30:00Z"], utc=True).to_pydatetime())


@pytest.mark.parametrize("day,opening", [("2026-10-30", 8), ("2026-11-02", 9)])
def test_covered_half_hours_keep_four_eastern_open_across_dst(day, opening) -> None:
    start = pd.Timestamp(day, tz="UTC")
    bars = [_minute_bar(str(start + pd.Timedelta(hours=opening, minutes=m)), close=100.) for m in (-5, 5)]
    derived = derive_intraday_bars(
        "RR", bars, "30m", as_of=start + pd.Timedelta(hours=12),
        coverage_start=start, coverage_end=start + pd.Timedelta(hours=12),
    )
    assert [bar.timestamp for bar in derived] == [(start + pd.Timedelta(hours=opening)).to_pydatetime()]


@pytest.mark.parametrize("changes", [
    {"close": float("nan")}, {"volume": -1.}, {"open": float("inf")},
    {"low": 200.}, {"high": 1.},
    {"timestamp": pd.Timestamp("2026-09-03T20:02:01Z").to_pydatetime()},
    {"timestamp": pd.Timestamp("2026-09-03T19:59:00Z").to_pydatetime()},
])
def test_covered_half_hours_reject_bad_rows_instead_of_treating_them_as_no_trades(changes) -> None:
    bars = [_minute_bar("2026-09-03T20:02:00Z", close=100.)]
    bars.append(replace(_minute_bar("2026-09-03T20:17:00Z", close=101.), **changes))
    with pytest.raises(ValueError, match="intact, valid"):
        derive_intraday_bars("RR", bars, "30m", as_of=pd.Timestamp("2026-09-03T21:00:00Z"),
            coverage_start=pd.Timestamp("2026-09-03T20:00:00Z"),
            coverage_end=pd.Timestamp("2026-09-03T21:00:00Z"))


def test_covered_half_hours_reject_duplicate_source_rows() -> None:
    bar = _minute_bar("2026-09-03T20:02:00Z", close=100.)
    with pytest.raises(ValueError, match="intact, valid"):
        derive_intraday_bars("RR", [bar, bar], "30m", as_of=pd.Timestamp("2026-09-03T21:00:00Z"),
            coverage_start=pd.Timestamp("2026-09-03T20:00:00Z"),
            coverage_end=pd.Timestamp("2026-09-03T21:00:00Z"))


@pytest.mark.parametrize("raw_rows,expected_rows", [(2, 2), (3, 0), (None, 0)])
def test_half_hour_sparse_derivation_requires_a_successful_intact_minute_response(tmp_path, raw_rows, expected_rows) -> None:
    bars = [_minute_bar(stamp, close=101.) for stamp in (
        "2026-09-03T20:02:00Z", "2026-09-03T20:47:00Z")]
    spec = DatabentoAnalysisSourceSpec("source_100d_1m", "ohlcv-1m", "1m", pd.Timedelta(days=100))
    coverage = SimpleNamespace(start=pd.Timestamp("2026-09-03T20:00:00Z").to_pydatetime(),
                               end=pd.Timestamp("2026-09-03T21:00:00Z").to_pydatetime())
    raw = None if raw_rows is None else pd.DataFrame({"test_row": range(raw_rows)})
    result = databento_fetch._persist_native_results("RR", ParquetStore(tmp_path),
        provider=SimpleNamespace(dataset="EQUS.MINI"), profile="continuation",
        observed_at=pd.Timestamp("2026-09-03T21:05:00Z").to_pydatetime(),
        native_results=((spec, bars, raw, coverage, None),),
        skip_native_frequencies=frozenset(("1m",)))
    path = normalized_bar_path(tmp_path, source="databento", symbol="RR",
        timeframe="30m", request_key="derived_1m_30m")
    assert result.error_files == 0
    assert (len(pd.read_parquet(path)) if path.exists() else 0) == expected_rows


def test_failed_minute_request_cannot_authorize_sparse_half_hours(tmp_path) -> None:
    spec = DatabentoAnalysisSourceSpec("source_100d_1m", "ohlcv-1m", "1m", pd.Timedelta(days=100))
    bars = [_minute_bar("2026-09-03T20:02:00Z", close=101.)]
    coverage = SimpleNamespace(start=pd.Timestamp("2026-09-03T20:00:00Z").to_pydatetime(),
                               end=pd.Timestamp("2026-09-03T21:00:00Z").to_pydatetime())
    databento_fetch._persist_native_results("RR", ParquetStore(tmp_path),
        provider=SimpleNamespace(dataset="EQUS.MINI"), profile="continuation",
        observed_at=pd.Timestamp("2026-09-03T21:05:00Z").to_pydatetime(),
        native_results=((spec, bars, pd.DataFrame({"test_row": [0]}), coverage, RuntimeError("truncated")),))
    path = normalized_bar_path(tmp_path, source="databento", symbol="RR",
        timeframe="30m", request_key="derived_1m_30m")
    assert not path.exists()


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
        request_key="source_1825d_1h_ohlcv-1h_1h",
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
        key="source_100d_1m",
        schema="ohlcv-1m",
        frequency="1m",
        lookback=pd.Timedelta(days=100),
    )
    daily_spec = DatabentoAnalysisSourceSpec(
        key="source_2555d_1d",
        schema="ohlcv-1d",
        frequency="1d",
        lookback=pd.Timedelta(days=2555),
    )
    observed_at = pd.Timestamp("2026-08-04T20:01:00Z")
    minute_request_key = "source_100d_1m_ohlcv-1m_1m"
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
        request_key="source_2555d_1d_ohlcv-1d_1d",
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


def _minute_bar(
    timestamp: str,
    *,
    close: float,
    volume: float = 100.0,
) -> MarketBar:
    return MarketBar(
        symbol="NVDA",
        source="databento",
        timeframe="1m",
        timestamp=pd.Timestamp(timestamp).to_pydatetime(),
        open=close - 0.1,
        high=close + 0.2,
        low=close - 0.2,
        close=close,
        volume=volume,
    )


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
