from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from app.models.market_data import MarketBar
from datafetching.bar_schema import (
    NORMALIZED_BAR_COLUMNS,
    NORMALIZED_BAR_PRICE_COLUMNS,
    normalized_bar_schema_is_canonical,
    write_normalized_bar_parquet,
)
from datafetching.bar_timing import finalize_normalized_bar_parquets
from datafetching.continuation import normalized_bar_path
from datafetching.decision_time import latest_completed_bar_clock
from datafetching.parquet_store import ParquetStore
from technicals.parquet_io import discover_bar_datasets


def _bar(
    timestamp: str,
    *,
    open_price: float = 100.0,
    close_price: float = 100.5,
) -> MarketBar:
    parsed = pd.Timestamp(timestamp).to_pydatetime()
    return MarketBar(
        symbol="GOOG",
        source="databento",
        timeframe="1m",
        timestamp=parsed,
        open=open_price,
        high=max(open_price, close_price) + 0.25,
        low=min(open_price, close_price) - 0.25,
        close=close_price,
        volume=1_000.0,
    )


def _wide_frame(timestamps: list[str]) -> pd.DataFrame:
    frame = pd.DataFrame(
        {
            "symbol": "GOOG",
            "source": "databento",
            "timeframe": "1m",
            "timestamp": pd.to_datetime(timestamps, utc=True),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1_000.0,
            "provider_timeframe": "1m",
            "canonical_timeframe": "1m",
            "request_key": "source_1000d_1m_ohlcv-1m_1m",
            "provider_dataset": "EQUS.MINI",
            "bar_complete": True,
            "session_type": "REGULAR",
        }
    )
    return frame


def test_save_bars_writes_only_canonical_columns_and_compacts_legacy(
    tmp_path: Path,
) -> None:
    request_key = "source_1000d_1m_ohlcv-1m_1m"
    path = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        request_key=request_key,
    )
    path.parent.mkdir(parents=True)
    _wide_frame(["2026-07-29T15:00:00Z"]).to_parquet(path, index=False)

    store = ParquetStore(tmp_path)
    written = store.save_bars(
        "databento",
        "GOOG",
        "1m",
        [
            _bar(
                "2026-07-29T15:01:00Z",
                open_price=100.5,
                close_price=101.0,
            )
        ],
        request_key=request_key,
        metadata={
            "provider_dataset": "EQUS.MINI",
            "source_schema": "ohlcv-1m",
            "normalized_bar_policy": "completed_intervals_only",
        },
    )

    assert written == path
    stored = pd.read_parquet(path)
    assert tuple(stored.columns) == NORMALIZED_BAR_COLUMNS
    assert stored["id"].tolist() == [
        "2026-07-29T15:00:00Z",
        "2026-07-29T15:01:00Z",
    ]
    assert len(stored) == 2
    assert stored["timestamp"].tolist() == [
        pd.Timestamp("2026-07-29T15:00:00Z"),
        pd.Timestamp("2026-07-29T15:01:00Z"),
    ]


def test_finalization_filters_incomplete_rows_and_compacts_wide_file(
    tmp_path: Path,
) -> None:
    path = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        request_key="source_1000d_1m_ohlcv-1m_1m",
    )
    path.parent.mkdir(parents=True)
    _wide_frame(
        [
            "2026-07-29T15:00:00Z",
            "2026-07-29T15:01:00Z",
        ]
    ).to_parquet(path, index=False)

    removed = finalize_normalized_bar_parquets(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        as_of=datetime(2026, 7, 29, 15, 1, 30, tzinfo=timezone.utc),
    )

    assert removed == 1
    stored = pd.read_parquet(path)
    assert tuple(stored.columns) == NORMALIZED_BAR_COLUMNS
    assert stored["timestamp"].tolist() == [
        pd.Timestamp("2026-07-29T15:00:00Z")
    ]


def test_lean_bar_file_supports_technical_discovery_and_decision_clock(
    tmp_path: Path,
) -> None:
    path = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        request_key="source_1000d_1m_ohlcv-1m_1m",
    )
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2026-07-29T15:14:00Z",
                    "2026-07-29T15:29:00Z",
                ],
                utc=True,
            ),
            "open": [100.0, 100.5],
            "high": [101.0, 101.5],
            "low": [99.5, 100.0],
            "close": [100.5, 101.0],
            "volume": [1_000.0, 1_100.0],
        },
        columns=NORMALIZED_BAR_COLUMNS,
    ).to_parquet(path, index=False)

    datasets = discover_bar_datasets(
        tmp_path,
        symbol="GOOG",
        providers=("databento",),
        timeframes={"1m"},
    )
    assert len(datasets) == 1
    assert datasets[0].symbol == "GOOG"
    assert datasets[0].provider == "databento"
    assert datasets[0].timeframe == "1m"
    assert {
        "bar_end_timestamp",
        "bar_complete",
        "session_type",
        "session_date",
    }.issubset(datasets[0].frame.columns)

    clock = latest_completed_bar_clock(
        tmp_path,
        symbol="GOOG",
        as_of=pd.Timestamp("2026-07-29T15:30:00Z"),
    )
    assert clock.bar_timestamp == pd.Timestamp("2026-07-29T15:29:00Z")
    assert clock.decision_timestamp == pd.Timestamp("2026-07-29T15:30:00Z")
    assert clock.provider == "databento"
    assert clock.timeframe == "1m"


def test_canonical_file_wins_over_legacy_snapshot_for_all_readers(
    tmp_path: Path,
) -> None:
    request_key = "source_1000d_1m_ohlcv-1m_1m"
    canonical_path = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        request_key=request_key,
    )
    canonical_path.parent.mkdir(parents=True)
    legacy_path = canonical_path.with_name(
        f"{canonical_path.stem}_20260729T150500.000000Z.parquet"
    )
    canonical = _wide_frame(["2024-07-29T15:14:00Z"])
    canonical["close"] = 200.0
    legacy = _wide_frame(["2024-07-29T15:14:00Z"])
    legacy["close"] = 100.0
    canonical.to_parquet(canonical_path, index=False)
    legacy.to_parquet(legacy_path, index=False)

    datasets = discover_bar_datasets(
        tmp_path,
        symbol="GOOG",
        providers=("databento",),
        timeframes={"1m"},
    )
    assert len(datasets) == 1
    assert datasets[0].frame["close"].tolist() == [200.0]

    clock = latest_completed_bar_clock(
        tmp_path,
        symbol="GOOG",
        as_of=pd.Timestamp("2024-07-29T15:15:00Z"),
    )
    assert clock.source_file == canonical_path

    store = ParquetStore(tmp_path)
    store.save_bars(
        "databento",
        "GOOG",
        "1m",
        [_bar("2024-07-29T15:15:00Z")],
        request_key=request_key,
        as_of=pd.Timestamp("2024-07-29T15:17:00Z"),
    )
    stored = pd.read_parquet(canonical_path)
    assert stored.loc[
        stored["timestamp"].eq(pd.Timestamp("2024-07-29T15:14:00Z")),
        "close",
    ].item() == 200.0
    assert not legacy_path.exists()


def test_explicit_legacy_incomplete_flags_are_conservative_everywhere(
    tmp_path: Path,
) -> None:
    path = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        request_key="source_1000d_1m_ohlcv-1m_1m",
    )
    path.parent.mkdir(parents=True)
    frame = _wide_frame(
        [
            "2024-07-29T15:14:00Z",
            "2024-07-29T15:29:00Z",
            "2024-07-29T15:44:00Z",
        ]
    )
    frame["bar_complete"] = [True, False, True]
    frame["bar_is_current"] = [False, False, True]
    frame.to_parquet(path, index=False)

    datasets = discover_bar_datasets(
        tmp_path,
        symbol="GOOG",
        providers=("databento",),
        timeframes={"1m"},
    )
    assert len(datasets) == 1
    assert datasets[0].frame["timestamp"].tolist() == [
        pd.Timestamp("2024-07-29T15:14:00Z")
    ]
    assert datasets[0].incomplete_bar_count == 2

    clock = latest_completed_bar_clock(
        tmp_path,
        symbol="GOOG",
        as_of=pd.Timestamp("2024-07-30T00:00:00Z"),
    )
    assert clock.decision_timestamp == pd.Timestamp("2024-07-29T15:15:00Z")

    removed = finalize_normalized_bar_parquets(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        as_of=pd.Timestamp("2024-07-30T00:00:00Z"),
    )
    assert removed == 2
    stored = pd.read_parquet(path)
    assert tuple(stored.columns) == NORMALIZED_BAR_COLUMNS
    assert stored["timestamp"].tolist() == [
        pd.Timestamp("2024-07-29T15:14:00Z")
    ]


def test_canonical_incomplete_marker_tombstones_completed_legacy_overlap(
    tmp_path: Path,
) -> None:
    canonical_path = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        request_key="source_1000d_1m_ohlcv-1m_1m",
    )
    canonical_path.parent.mkdir(parents=True)
    legacy_path = canonical_path.with_name(
        f"{canonical_path.stem}_20260729T150500.000000Z.parquet"
    )
    canonical = _wide_frame(
        [
            "2024-07-29T15:14:00Z",
            "2024-07-29T15:29:00Z",
        ]
    )
    canonical["bar_complete"] = [True, False]
    canonical["bar_is_current"] = [False, True]
    legacy = _wide_frame(["2024-07-29T15:29:00Z"])
    canonical.to_parquet(canonical_path, index=False)
    legacy.to_parquet(legacy_path, index=False)

    datasets = discover_bar_datasets(
        tmp_path,
        symbol="GOOG",
        providers=("databento",),
        timeframes={"1m"},
    )
    assert datasets[0].frame["timestamp"].tolist() == [
        pd.Timestamp("2024-07-29T15:14:00Z")
    ]
    clock = latest_completed_bar_clock(
        tmp_path,
        symbol="GOOG",
        as_of=pd.Timestamp("2024-07-30T00:00:00Z"),
    )
    assert clock.decision_timestamp == pd.Timestamp("2024-07-29T15:15:00Z")

    finalize_normalized_bar_parquets(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        as_of=pd.Timestamp("2024-07-30T00:00:00Z"),
    )
    assert not legacy_path.exists()
    assert pd.read_parquet(canonical_path)["timestamp"].tolist() == [
        pd.Timestamp("2024-07-29T15:14:00Z")
    ]


def test_discovery_skips_unrequested_canonical_timeframes_before_reading(
    tmp_path: Path,
) -> None:
    selected_path = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        request_key="source_1000d_1m_ohlcv-1m_1m",
    )
    selected_path.parent.mkdir(parents=True)
    _wide_frame(["2026-07-29T15:14:00Z"]).to_parquet(
        selected_path,
        index=False,
    )

    unrequested_path = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1s",
        request_key="source_5d_1s_ohlcv-1s_1s",
    )
    unrequested_path.parent.mkdir(parents=True)
    unrequested_path.write_bytes(b"not a parquet file")

    datasets = discover_bar_datasets(
        tmp_path,
        symbol="GOOG",
        providers=("databento",),
        timeframes={"1m"},
    )

    assert len(datasets) == 1
    assert datasets[0].timeframe == "1m"


def test_wrong_six_column_dtypes_are_coerced_and_rewritten(
    tmp_path: Path,
) -> None:
    request_key = "source_1000d_1m_ohlcv-1m_1m"
    path = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="GOOG",
        timeframe="1m",
        request_key=request_key,
    )
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": ["2024-07-29T15:00:00Z"],
            "open": ["100.0"],
            "high": [101],
            "low": ["99.0"],
            "close": [100],
            "volume": ["1000"],
        },
        columns=NORMALIZED_BAR_COLUMNS,
    ).to_parquet(path, index=False)
    assert not normalized_bar_schema_is_canonical(pq.read_schema(path))

    store = ParquetStore(tmp_path)
    store.save_bars(
        "databento",
        "GOOG",
        "1m",
        [_bar("2024-07-29T15:01:00Z")],
        request_key=request_key,
        as_of=pd.Timestamp("2024-07-29T15:03:00Z"),
    )

    assert normalized_bar_schema_is_canonical(pq.read_schema(path))
    stored = pd.read_parquet(path)
    assert str(stored["timestamp"].dtype) == "datetime64[ns, UTC]"
    assert str(stored["id"].dtype) in {"string", "object"}
    assert all(
        str(stored[column].dtype) == "float64"
        for column in NORMALIZED_BAR_PRICE_COLUMNS
    )
    assert len(stored) == 2


def test_schwab_style_save_filters_forming_candle_at_request_time(
    tmp_path: Path,
) -> None:
    store = ParquetStore(tmp_path)
    request_key = "day_1_minute_1"
    written = store.save_bars(
        "schwab",
        "GOOG",
        request_key,
        [
            _bar("2024-07-29T15:00:00Z"),
            _bar("2024-07-29T15:01:00Z"),
        ],
        request_key=request_key,
        metadata={
            "provider_frequency_type": "minute",
            "provider_frequency": 1,
        },
        as_of=pd.Timestamp("2024-07-29T15:01:30Z"),
    )

    assert written is not None
    stored = pd.read_parquet(written)
    assert stored["timestamp"].tolist() == [
        pd.Timestamp("2024-07-29T15:00:00Z")
    ]


def test_schwab_upsert_filters_unflagged_forming_legacy_candle(
    tmp_path: Path,
) -> None:
    request_key = "day_1_minute_1"
    path = normalized_bar_path(
        tmp_path,
        source="schwab",
        symbol="GOOG",
        timeframe="1m",
        request_key=request_key,
    )
    path.parent.mkdir(parents=True)
    legacy = _wide_frame(
        [
            "2024-07-29T15:00:00Z",
            "2024-07-29T15:01:00Z",
        ]
    ).drop(columns="bar_complete")
    legacy.to_parquet(path, index=False)

    store = ParquetStore(tmp_path)
    store.save_bars(
        "schwab",
        "GOOG",
        request_key,
        [_bar("2024-07-29T15:00:00Z", close_price=101.0)],
        request_key=request_key,
        metadata={
            "provider_frequency_type": "minute",
            "provider_frequency": 1,
        },
        as_of=pd.Timestamp("2024-07-29T15:01:30Z"),
    )

    stored = pd.read_parquet(path)
    assert tuple(stored.columns) == NORMALIZED_BAR_COLUMNS
    assert stored["timestamp"].tolist() == [
        pd.Timestamp("2024-07-29T15:00:00Z")
    ]
    assert stored["close"].tolist() == [101.0]


def test_schwab_upsert_rewrites_canonical_file_when_all_bars_are_forming(
    tmp_path: Path,
) -> None:
    request_key = "day_1_minute_1"
    path = normalized_bar_path(
        tmp_path,
        source="schwab",
        symbol="GOOG",
        timeframe="1m",
        request_key=request_key,
    )
    path.parent.mkdir(parents=True)
    forming = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2024-07-29T15:01:00Z"),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1_000.0,
            }
        ]
    )
    write_normalized_bar_parquet(forming, path)
    assert normalized_bar_schema_is_canonical(pq.read_schema(path))

    store = ParquetStore(tmp_path)
    store.save_bars(
        "schwab",
        "GOOG",
        request_key,
        [_bar("2024-07-29T15:01:00Z")],
        request_key=request_key,
        metadata={
            "provider_frequency_type": "minute",
            "provider_frequency": 1,
        },
        as_of=pd.Timestamp("2024-07-29T15:01:30Z"),
    )

    stored = pd.read_parquet(path)
    assert tuple(stored.columns) == NORMALIZED_BAR_COLUMNS
    assert stored.empty
