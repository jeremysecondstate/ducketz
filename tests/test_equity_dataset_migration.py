from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from datafetching.bar_schema import write_normalized_bar_parquet
from datafetching.continuation import normalized_bar_path
from datafetching.equity_dataset_migration import (
    NATIVE_REQUESTS,
    migrate_operational_equities,
)


def _seed_dataset_pair(
    root: Path,
    *,
    symbol: str,
    timeframe: str,
    request_key: str,
    current_dataset: str,
    restore_dataset: str,
) -> Path:
    target = normalized_bar_path(
        root,
        source="databento",
        symbol=symbol,
        timeframe=timeframe,
        request_key=request_key,
    )
    raw = target.parent.parent / "raw" / f"{target.stem}_raw.parquet"
    target.parent.mkdir(parents=True, exist_ok=True)
    raw.parent.mkdir(parents=True, exist_ok=True)
    timestamps = pd.date_range(
        "2026-08-19T15:00:00Z",
        periods=61,
        freq="1min",
    )
    current = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 10.0,
        }
    )
    restored = current.assign(close=200.5)
    write_normalized_bar_parquet(current, target)
    pd.DataFrame({"provider_dataset": [current_dataset]}).to_parquet(
        raw,
        index=False,
    )
    target.with_suffix(".archive-lineage.json").write_text(
        json.dumps({"live_dataset": current_dataset}),
        encoding="utf-8",
    )

    backup_root = target.parent.parent / "migration-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backup = backup_root / f"{target.stem}.{restore_dataset}.fixture.parquet"
    backup_raw = backup.with_name(f"{backup.stem}_raw.parquet")
    write_normalized_bar_parquet(restored, backup)
    pd.DataFrame({"provider_dataset": [restore_dataset]}).to_parquet(
        backup_raw,
        index=False,
    )
    return target


def test_equity_dataset_migration_restores_one_dataset_and_rebuilds_views(
    tmp_path: Path,
) -> None:
    root = tmp_path / "DATASTORE"
    (root / "stocks").mkdir(parents=True)
    targets = {
        timeframe: _seed_dataset_pair(
            root,
            symbol="AAPL",
            timeframe=timeframe,
            request_key=request_key,
            current_dataset="XNAS.ITCH",
            restore_dataset="EQUS.MINI",
        )
        for timeframe, request_key in NATIVE_REQUESTS.items()
    }

    dry_run = migrate_operational_equities(
        root,
        symbols=("AAPL",),
        target_dataset="EQUS.MINI",
    )
    assert dry_run.dry_run is True
    assert dry_run.native_files_restored == 4
    assert dry_run.receipt_path is None

    result = migrate_operational_equities(
        root,
        symbols=("AAPL",),
        target_dataset="EQUS.MINI",
        confirm=True,
        observed_at=datetime(2026, 8, 19, 17, 0, tzinfo=timezone.utc),
    )

    assert result.dry_run is False
    assert result.native_files_restored == 4
    assert result.derived_files_rebuilt == 5
    assert result.receipt_path is not None and result.receipt_path.is_file()
    for target in targets.values():
        restored = pd.read_parquet(target)
        assert set(restored["close"]) == {200.5}
        raw = target.parent.parent / "raw" / f"{target.stem}_raw.parquet"
        assert set(pd.read_parquet(raw)["provider_dataset"]) == {"EQUS.MINI"}
        assert not target.with_suffix(".archive-lineage.json").exists()
        assert any(
            "pre-EQUS.MINI" in path.name
            for path in (target.parent.parent / "migration-backups").iterdir()
        )
    for frequency in ("5m", "10m", "15m", "30m", "1h"):
        assert normalized_bar_path(
            root,
            source="databento",
            symbol="AAPL",
            timeframe=frequency,
            request_key=f"derived_1m_{frequency}",
        ).is_file()

    receipt = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    assert receipt["target_dataset"] == "EQUS.MINI"
    assert receipt["symbols"] == ["AAPL"]
