from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.services.market_fetch_specs import DatabentoAnalysisSourceSpec
from datafetching.continuation import latest_normalized_bar_timestamp, normalized_bar_path
from datafetching.databento_archive import (
    ARCHIVE_LINEAGE_VERSION,
    archive_lineage_path,
    archive_lineage_sources,
    materialize_equity_archive_baseline,
)
from datafetching.databento_storage import MARKET_US_EQUITIES, request_directory
from ml.artifacts import file_checksum
from technicals.parquet_io import discover_bar_datasets


def test_verified_equity_archive_seeds_loop_a_and_loop_b_lineage(
    tmp_path: Path,
) -> None:
    _write_cold_partition(
        tmp_path,
        symbol="AAPL",
        rows=pd.DataFrame(
            {
                "ts_event": pd.to_datetime(
                    ["2026-08-14T19:58:00Z", "2026-08-14T19:59:00Z"],
                    utc=True,
                ),
                "symbol": ["AAPL", "AAPL"],
                "open": [100.0, 101.0],
                "high": [101.0, 102.0],
                "low": [99.0, 100.0],
                "close": [100.5, 101.5],
                "volume": [10, 20],
            }
        ).set_index("ts_event"),
    )
    spec = DatabentoAnalysisSourceSpec(
        "source_100d_1m",
        "ohlcv-1m",
        "1m",
        pd.Timedelta(days=100).to_pytimedelta(),
    )

    result = materialize_equity_archive_baseline(
        tmp_path,
        symbols=("AAPL",),
        live_dataset="XNAS.ITCH",
        source_specs=(spec,),
        as_of="2026-08-14T20:01:00Z",
    )

    assert result.materialized_files == 1
    assert result.archive_rows == 2
    target = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="AAPL",
        timeframe="1m",
        request_key="source_100d_1m_ohlcv-1m_1m",
    )
    assert pd.read_parquet(target)["close"].tolist() == [100.5, 101.5]
    assert latest_normalized_bar_timestamp(
        tmp_path,
        source="databento",
        symbol="AAPL",
        timeframe="1m",
        request_key="source_100d_1m_ohlcv-1m_1m",
    ) == pd.Timestamp("2026-08-14T19:59:00Z").to_pydatetime()

    lineage = json.loads(archive_lineage_path(target).read_text(encoding="utf-8"))
    assert lineage["schema_version"] == ARCHIVE_LINEAGE_VERSION
    assert lineage["archive_dataset"] == "XNAS.ITCH"
    inherited = archive_lineage_sources(tmp_path, target)
    assert any("market-data" in path.parts for path in inherited)
    datasets = discover_bar_datasets(
        tmp_path,
        symbol="AAPL",
        providers=("databento",),
        timeframes={"1m"},
    )
    assert len(datasets) == 1
    assert any("market-data" in path.parts for path in datasets[0].source_files)

    reused = materialize_equity_archive_baseline(
        tmp_path,
        symbols=("AAPL",),
        live_dataset="XNAS.ITCH",
        source_specs=(spec,),
        as_of="2026-08-14T20:02:00Z",
    )
    assert reused.reused_files == 1
    assert reused.materialized_files == 0


def test_equity_archive_migration_preserves_mismatched_live_dataset(
    tmp_path: Path,
) -> None:
    _write_cold_partition(
        tmp_path,
        symbol="AAPL",
        rows=pd.DataFrame(
            {
                "ts_event": pd.to_datetime(["2026-08-14T19:59:00Z"], utc=True),
                "symbol": ["AAPL"],
                "open": [101.0],
                "high": [102.0],
                "low": [100.0],
                "close": [101.5],
                "volume": [20],
            }
        ).set_index("ts_event"),
    )
    spec = DatabentoAnalysisSourceSpec(
        "source_100d_1m",
        "ohlcv-1m",
        "1m",
        pd.Timedelta(days=100).to_pytimedelta(),
    )
    target = normalized_bar_path(
        tmp_path,
        source="databento",
        symbol="AAPL",
        timeframe="1m",
        request_key="source_100d_1m_ohlcv-1m_1m",
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-08-13T19:59:00Z"], utc=True),
            "open": [50.0],
            "high": [51.0],
            "low": [49.0],
            "close": [50.5],
            "volume": [1.0],
        }
    ).to_parquet(target, index=False)
    raw = target.parent.parent / "raw" / f"{target.stem}_raw.parquet"
    raw.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"provider_dataset": ["EQUS.MINI"]}).to_parquet(raw, index=False)

    result = materialize_equity_archive_baseline(
        tmp_path,
        symbols=("AAPL",),
        live_dataset="XNAS.ITCH",
        source_specs=(spec,),
        as_of="2026-08-14T20:01:00Z",
    )

    assert result.materialized_files == 1
    assert not raw.exists()
    backups = tuple((target.parent.parent / "migration-backups").glob("*.parquet"))
    assert len(backups) == 2
    assert pd.read_parquet(target)["close"].tolist() == [101.5]


def _write_cold_partition(
    root: Path,
    *,
    symbol: str,
    rows: pd.DataFrame,
) -> Path:
    directory = request_directory(
        root,
        market=MARKET_US_EQUITIES,
        dataset="XNAS.ITCH",
        schema="ohlcv-1m",
        symbol=symbol,
        start="2026-08-14",
        end="2026-08-15",
    )
    directory.mkdir(parents=True, exist_ok=True)
    normalized = directory / "normalized.parquet"
    rows.to_parquet(normalized)
    manifest = {
        "schema_version": "databento-cold-start-partition-v1",
        "request": {
            "request_id": "archive-request",
            "dataset": "XNAS.ITCH",
            "schema": "ohlcv-1m",
            "symbol_scope": [symbol],
            "start": "2026-08-14",
            "end": "2026-08-15",
        },
        "normalized": {
            "path": normalized.name,
            "size_bytes": normalized.stat().st_size,
            "checksum_sha256": file_checksum(normalized),
            "earliest_timestamp": "2026-08-14T19:58:00+00:00",
            "latest_timestamp": "2026-08-14T19:59:00+00:00",
        },
    }
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    receipt = {
        "schema_version": "databento-cold-start-receipt-v1",
        "request_id": "archive-request",
        "manifest_checksum_sha256": file_checksum(manifest_path),
        "normalized_checksum_sha256": file_checksum(normalized),
    }
    (directory / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    return directory
