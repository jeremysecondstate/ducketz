from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.models.market_data import MarketBar
from datafetching.datastore_hygiene import (
    CLEANUP_RECEIPT_VERSION,
    DatastoreHygieneError,
    PRODUCTION_OPRA_HISTORY_SCHEMAS,
    clean_datastore,
)
from datafetching.equity_dataset_migration import NATIVE_REQUESTS
from datafetching.parquet_store import ParquetStore


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)
OLD_TIMESTAMP = datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp()


def test_opra_production_history_scope_includes_strategy_hourly_bars() -> None:
    assert PRODUCTION_OPRA_HISTORY_SCHEMAS == frozenset(
        ("ohlcv-1h", "cbbo-1m", "definition")
    )


def test_cleanup_removes_only_planned_old_staging_and_writes_receipt(
    tmp_path: Path,
) -> None:
    root = _datastore_root(tmp_path)
    first = root / "market-data/databento/.staging/request/attempt-001/provider.dbn.zst"
    second = (
        root
        / "market-data/databento/opra/OPRA.PILLAR/.staging/cbbo-1m/AAPL.OPT/attempt-001/normalized.parquet"
    )
    for path, payload in ((first, b"partial"), (second, b"also partial")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        os.utime(path, (OLD_TIMESTAMP, OLD_TIMESTAMP))

    result = clean_datastore(
        root,
        symbols=("AAPL",),
        clean_staging=True,
        confirm=True,
        observed_at=NOW,
    )

    assert result["schema_version"] == CLEANUP_RECEIPT_VERSION
    assert result["status"] == "COMPLETE"
    assert result["deleted_file_count"] == 2
    assert result["deleted_bytes"] == len(b"partial") + len(b"also partial")
    assert not first.exists()
    assert not second.exists()
    receipt = Path(str(result["receipt_path"]))
    assert receipt.is_file()
    assert json.loads(receipt.read_text(encoding="utf-8"))["deleted_bytes_recoverable"] is False
    assert (root / "catalog/market-data/current.json").is_file()


def test_cleanup_preserves_staging_with_publication_evidence(tmp_path: Path) -> None:
    root = _datastore_root(tmp_path)
    attempt = root / "market-data/databento/.staging/request/attempt-001"
    attempt.mkdir(parents=True)
    manifest = attempt / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    os.utime(manifest, (OLD_TIMESTAMP, OLD_TIMESTAMP))

    with pytest.raises(DatastoreHygieneError, match="manifest/receipt evidence"):
        clean_datastore(
            root,
            symbols=("AAPL",),
            clean_staging=True,
            confirm=True,
            observed_at=NOW,
        )

    assert manifest.is_file()


def test_migration_backups_require_complete_current_equs_pairs(
    tmp_path: Path,
) -> None:
    root = _datastore_root(tmp_path)
    backup = (
        root
        / "stocks/AAPL/bars/1d/databento/migration-backups/old.parquet"
    )
    backup.parent.mkdir(parents=True)
    pd.DataFrame({"value": [1]}).to_parquet(backup, index=False)
    os.utime(backup, (OLD_TIMESTAMP, OLD_TIMESTAMP))
    migration = root / "catalog/migrations/loop-a-equities-to-equs-mini-test.json"
    migration.parent.mkdir(parents=True)
    migration.write_text("{}", encoding="utf-8")

    with pytest.raises(DatastoreHygieneError, match="pair is incomplete"):
        clean_datastore(
            root,
            symbols=("AAPL",),
            retire_migration_backups=True,
            confirm=True,
            observed_at=NOW,
        )

    assert backup.is_file()


def test_migration_backups_retire_after_current_equs_validation(
    tmp_path: Path,
) -> None:
    root = _datastore_root(tmp_path)
    store = ParquetStore(root)
    migration = root / "catalog/migrations/loop-a-equities-to-equs-mini-test.json"
    migration.parent.mkdir(parents=True)
    migration.write_text("{}", encoding="utf-8")
    for timeframe, request_key in NATIVE_REQUESTS.items():
        bar = MarketBar(
            symbol="AAPL",
            source="databento",
            timeframe=timeframe,
            timestamp=pd.Timestamp("2026-07-31T14:00:00Z").to_pydatetime(),
            open=1.0,
            high=2.0,
            low=0.5,
            close=1.5,
            volume=100.0,
        )
        store.save_bars(
            "databento",
            "AAPL",
            timeframe,
            [bar],
            request_key=request_key,
            as_of=pd.Timestamp("2026-08-02T00:00:00Z"),
        )
        raw = (
            root
            / "stocks"
            / "AAPL"
            / "bars"
            / timeframe
            / "databento/raw"
            / f"AAPL_{request_key}_raw.parquet"
        )
        raw.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"provider_dataset": ["EQUS.MINI"]}).to_parquet(
            raw,
            index=False,
        )
    backup = (
        root
        / "stocks/AAPL/bars/1d/databento/migration-backups/old.parquet"
    )
    backup.parent.mkdir(parents=True)
    pd.DataFrame({"value": [1]}).to_parquet(backup, index=False)
    os.utime(backup, (OLD_TIMESTAMP, OLD_TIMESTAMP))

    result = clean_datastore(
        root,
        symbols=("AAPL",),
        retire_migration_backups=True,
        confirm=True,
        observed_at=NOW,
    )

    assert result["deleted_file_count"] == 1
    assert not backup.exists()


def _datastore_root(tmp_path: Path) -> Path:
    root = tmp_path / "DATASTORE"
    (root / "stocks").mkdir(parents=True)
    (root / "market-data/databento").mkdir(parents=True)
    return root
