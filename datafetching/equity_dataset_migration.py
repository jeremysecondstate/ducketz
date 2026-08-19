from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

import pandas as pd

from app.models.market_data import MarketBar
from datafetching.bar_schema import (
    NORMALIZED_BAR_VALUE_COLUMNS,
    read_normalized_bar_parquet,
)
from datafetching.bar_timing import finalize_normalized_bar_parquets
from datafetching.continuation import normalized_bar_path
from datafetching.derived_bars import (
    DERIVED_INTRADAY_FREQUENCIES,
    derive_intraday_bars,
)
from datafetching.parquet_store import ParquetStore


DEFAULT_SYMBOLS = ("AAPL", "SNDK", "NVDA", "MU", "GOOG", "AMZN")
NATIVE_REQUESTS: Mapping[str, str] = {
    "1s": "source_10d_1s_ohlcv-1s_1s",
    "1m": "source_100d_1m_ohlcv-1m_1m",
    "1h": "source_1825d_1h_ohlcv-1h_1h",
    "1d": "source_2555d_1d_ohlcv-1d_1d",
}


class EquityDatasetMigrationError(RuntimeError):
    """The operational equity dataset could not be switched without data loss."""


@dataclass(frozen=True)
class MigrationResult:
    datastore_root: Path
    target_dataset: str
    symbols: tuple[str, ...]
    native_files_restored: int
    derived_files_rebuilt: int
    prior_files_backed_up: int
    receipt_path: Path | None
    dry_run: bool


def migrate_operational_equities(
    datastore_root: Path,
    *,
    symbols: Iterable[str] = DEFAULT_SYMBOLS,
    target_dataset: str = "EQUS.MINI",
    confirm: bool = False,
    observed_at: datetime | None = None,
) -> MigrationResult:
    """Restore a same-dataset operational baseline and rebuild derived bars.

    Published cold-archive files are never edited. Every displaced operational
    Parquet is copied into its adjacent ``migration-backups`` directory before
    replacement. The replacement source must itself have a raw Parquet proving
    exactly one matching ``provider_dataset`` value.
    """

    root = Path(datastore_root).resolve()
    if root.name.upper() != "DATASTORE" or not (root / "stocks").is_dir():
        raise EquityDatasetMigrationError(
            f"Refusing non-DATASTORE migration root: {root}"
        )
    clean_symbols = tuple(
        dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in symbols
            if str(symbol).strip()
        )
    )
    if not clean_symbols:
        raise EquityDatasetMigrationError("At least one equity symbol is required")
    dataset = str(target_dataset).strip()
    if not dataset:
        raise EquityDatasetMigrationError("A target Databento dataset is required")

    plans = [
        _native_restore_plan(root, symbol, timeframe, request_key, dataset)
        for symbol in clean_symbols
        for timeframe, request_key in NATIVE_REQUESTS.items()
    ]
    changed_plans = [plan for plan in plans if plan["requires_restore"]]
    now = observed_at or datetime.now(timezone.utc)
    if not confirm:
        return MigrationResult(
            datastore_root=root,
            target_dataset=dataset,
            symbols=clean_symbols,
            native_files_restored=len(changed_plans),
            derived_files_rebuilt=len(clean_symbols)
            * len(DERIVED_INTRADAY_FREQUENCIES),
            prior_files_backed_up=sum(
                int(bool(plan["current_normalized_exists"]))
                + int(bool(plan["current_raw_exists"]))
                + int(bool(plan["lineage_exists"]))
                for plan in changed_plans
            ),
            receipt_path=None,
            dry_run=True,
        )

    records: list[dict[str, object]] = []
    backed_up = 0
    restored = 0
    for plan in changed_plans:
        result = _execute_native_restore(plan, dataset=dataset)
        records.append(result)
        backed_up += int(result["backed_up_file_count"])
        restored += 1

    rebuilt = 0
    for symbol in clean_symbols:
        derived_records = _rebuild_intraday_derivatives(
            root,
            symbol=symbol,
            dataset=dataset,
            observed_at=now,
        )
        records.extend(derived_records)
        rebuilt += len(derived_records)
        backed_up += sum(
            int(record.get("backed_up_file_count", 0))
            for record in derived_records
        )
        records.extend(
            _retire_mismatched_daily_derivative(
                root,
                symbol=symbol,
                dataset=dataset,
            )
        )

    receipt_path = _write_receipt(
        root,
        dataset=dataset,
        symbols=clean_symbols,
        observed_at=now,
        records=records,
    )
    return MigrationResult(
        datastore_root=root,
        target_dataset=dataset,
        symbols=clean_symbols,
        native_files_restored=restored,
        derived_files_rebuilt=rebuilt,
        prior_files_backed_up=backed_up,
        receipt_path=receipt_path,
        dry_run=False,
    )


def _native_restore_plan(
    root: Path,
    symbol: str,
    timeframe: str,
    request_key: str,
    dataset: str,
) -> dict[str, object]:
    target = normalized_bar_path(
        root,
        source="databento",
        symbol=symbol,
        timeframe=timeframe,
        request_key=request_key,
    )
    raw_target = _raw_bar_path(target)
    current_datasets = _raw_datasets(raw_target)
    requires_restore = current_datasets != {dataset}
    backup_normalized: Path | None = None
    backup_raw: Path | None = None
    if requires_restore:
        backup_normalized, backup_raw = _matching_backup_pair(target, dataset)
        _validate_restore_pair(
            backup_normalized,
            backup_raw,
            expected_dataset=dataset,
        )
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "request_key": request_key,
        "target": target,
        "raw_target": raw_target,
        "lineage": target.with_suffix(".archive-lineage.json"),
        "current_datasets": sorted(current_datasets),
        "current_normalized_exists": target.is_file(),
        "current_raw_exists": raw_target.is_file(),
        "lineage_exists": target.with_suffix(".archive-lineage.json").is_file(),
        "requires_restore": requires_restore,
        "backup_normalized": backup_normalized,
        "backup_raw": backup_raw,
    }


def _matching_backup_pair(target: Path, dataset: str) -> tuple[Path, Path]:
    backup_root = target.parent.parent / "migration-backups"
    pattern = f"{target.stem}.{dataset}.*.parquet"
    candidates = sorted(
        (
            path
            for path in backup_root.glob(pattern)
            if not path.stem.endswith("_raw")
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    for normalized in candidates:
        raw = normalized.with_name(f"{normalized.stem}_raw.parquet")
        if raw.is_file():
            return normalized, raw
    raise EquityDatasetMigrationError(
        f"No complete {dataset} migration-backup pair exists for {target}"
    )


def _validate_restore_pair(
    normalized: Path,
    raw: Path,
    *,
    expected_dataset: str,
) -> None:
    datasets = _raw_datasets(raw)
    if datasets != {expected_dataset}:
        raise EquityDatasetMigrationError(
            f"Restore raw dataset mismatch for {raw}: {sorted(datasets)}"
        )
    frame, _schema = read_normalized_bar_parquet(normalized, include_ids=False)
    if frame.empty or frame["timestamp"].isna().any():
        raise EquityDatasetMigrationError(
            f"Restore normalized Parquet is empty or has invalid timestamps: {normalized}"
        )
    if frame["timestamp"].duplicated().any():
        raise EquityDatasetMigrationError(
            f"Restore normalized Parquet has duplicate timestamps: {normalized}"
        )


def _execute_native_restore(
    plan: Mapping[str, object],
    *,
    dataset: str,
) -> dict[str, object]:
    target = Path(plan["target"])
    raw_target = Path(plan["raw_target"])
    lineage = Path(plan["lineage"])
    source = Path(plan["backup_normalized"])
    raw_source = Path(plan["backup_raw"])
    backup_root = target.parent.parent / "migration-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    backups: list[Path] = []
    for current in (target, raw_target, lineage):
        if current.is_file():
            backups.append(
                _copy_recoverable_backup(
                    current,
                    backup_root,
                    label="pre-" + dataset.replace("/", "-"),
                )
            )
    _atomic_copy(source, target)
    _atomic_copy(raw_source, raw_target)
    if lineage.is_file():
        lineage.unlink()
    if _raw_datasets(raw_target) != {dataset}:
        raise EquityDatasetMigrationError(
            f"Post-restore dataset verification failed for {raw_target}"
        )
    return {
        "action": "restore-native-operational-bars",
        "symbol": plan["symbol"],
        "timeframe": plan["timeframe"],
        "request_key": plan["request_key"],
        "prior_datasets": plan["current_datasets"],
        "target_dataset": dataset,
        "source_backup": source.as_posix(),
        "source_raw_backup": raw_source.as_posix(),
        "target": target.as_posix(),
        "target_checksum_sha256": _checksum(target),
        "raw_target_checksum_sha256": _checksum(raw_target),
        "backups": [path.as_posix() for path in backups],
        "backed_up_file_count": len(backups),
    }


def _rebuild_intraday_derivatives(
    root: Path,
    *,
    symbol: str,
    dataset: str,
    observed_at: datetime,
) -> list[dict[str, object]]:
    minute_path = normalized_bar_path(
        root,
        source="databento",
        symbol=symbol,
        timeframe="1m",
        request_key=NATIVE_REQUESTS["1m"],
    )
    minute_frame, _schema = read_normalized_bar_parquet(
        minute_path,
        include_ids=False,
    )
    bars = [
        MarketBar(
            symbol=symbol,
            source="databento",
            timeframe="1m",
            timestamp=pd.Timestamp(row.timestamp).to_pydatetime(),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume or 0.0),
        )
        for row in minute_frame.itertuples(index=False)
    ]
    store = ParquetStore(root)
    records: list[dict[str, object]] = []
    for frequency in DERIVED_INTRADAY_FREQUENCIES:
        request_key = f"derived_1m_{frequency}"
        target = normalized_bar_path(
            root,
            source="databento",
            symbol=symbol,
            timeframe=frequency,
            request_key=request_key,
        )
        backup_paths: list[Path] = []
        if target.is_file():
            backup = _copy_recoverable_backup(
                target,
                target.parent.parent / "migration-backups",
                label="pre-" + dataset.replace("/", "-"),
            )
            backup_paths.append(backup)
            target.unlink()
        derived = derive_intraday_bars(
            symbol,
            bars,
            frequency,
            as_of=observed_at,
        )
        saved = store.save_bars(
            "databento",
            symbol,
            frequency,
            derived,
            request_key=request_key,
            metadata={
                "provider_dataset": dataset,
                "source_schema": "ohlcv-1m",
                "source_frequency": "1m",
                "output_frequency": frequency,
                "aggregation_method": "session_resampled_from_complete_1m",
                "normalized_bar_policy": "completed_intervals_only",
            },
            as_of=observed_at,
        )
        finalize_normalized_bar_parquets(
            root,
            source="databento",
            symbol=symbol,
            timeframe=frequency,
            as_of=observed_at,
        )
        if saved is None or not target.is_file():
            raise EquityDatasetMigrationError(
                f"Derived {frequency} rebuild produced no output for {symbol}"
            )
        records.append(
            {
                "action": "rebuild-derived-operational-bars",
                "symbol": symbol,
                "timeframe": frequency,
                "target_dataset": dataset,
                "source": minute_path.as_posix(),
                "target": target.as_posix(),
                "target_checksum_sha256": _checksum(target),
                "backups": [path.as_posix() for path in backup_paths],
                "backed_up_file_count": len(backup_paths),
            }
        )
    return records


def _retire_mismatched_daily_derivative(
    root: Path,
    *,
    symbol: str,
    dataset: str,
) -> list[dict[str, object]]:
    target = normalized_bar_path(
        root,
        source="databento",
        symbol=symbol,
        timeframe="1d",
        request_key="derived_1m_1d",
    )
    if not target.is_file():
        return []
    backup = _copy_recoverable_backup(
        target,
        target.parent.parent / "migration-backups",
        label="pre-" + dataset.replace("/", "-"),
    )
    target.unlink()
    return [
        {
            "action": "retire-derived-daily-before-dataset-switch",
            "symbol": symbol,
            "timeframe": "1d",
            "target_dataset": dataset,
            "target": target.as_posix(),
            "backups": [backup.as_posix()],
            "backed_up_file_count": 1,
        }
    ]


def _raw_bar_path(normalized: Path) -> Path:
    return (
        normalized.parent.parent
        / "raw"
        / f"{normalized.stem}_raw.parquet"
    )


def _raw_datasets(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        frame = pd.read_parquet(path, columns=["provider_dataset"])
    except (KeyError, OSError, ValueError) as exc:
        raise EquityDatasetMigrationError(
            f"Cannot prove provider_dataset in {path}"
        ) from exc
    return {
        str(value).strip()
        for value in frame["provider_dataset"].dropna().unique()
        if str(value).strip()
    }


def _copy_recoverable_backup(source: Path, directory: Path, *, label: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    suffix = "".join(source.suffixes) or source.suffix
    stem = source.name[: -len(suffix)] if suffix else source.name
    destination = directory / f"{stem}.{label}.{_checksum(source)[:16]}{suffix}"
    if not destination.is_file():
        shutil.copy2(source, destination)
    elif _checksum(destination) != _checksum(source):
        raise EquityDatasetMigrationError(
            f"Existing migration backup diverged: {destination}"
        )
    return destination


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.tmp-{os.getpid()}"
    )
    if temporary.exists():
        raise EquityDatasetMigrationError(
            f"Migration temporary path already exists: {temporary}"
        )
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_receipt(
    root: Path,
    *,
    dataset: str,
    symbols: tuple[str, ...],
    observed_at: datetime,
    records: list[dict[str, object]],
) -> Path:
    timestamp = observed_at.astimezone(timezone.utc)
    directory = root / "catalog" / "migrations"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (
        "loop-a-equities-to-"
        + dataset.lower().replace(".", "-")
        + "-"
        + timestamp.strftime("%Y%m%dT%H%M%SZ")
        + ".json"
    )
    payload = {
        "schema_version": "ducketz-equity-dataset-migration-v1",
        "migration": "loop-a-operational-equity-dataset",
        "target_dataset": dataset,
        "symbols": list(symbols),
        "completed_at": timestamp.isoformat(),
        "records": records,
    }
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Safely switch Loop A operational Databento bars to one dataset by "
            "restoring verified migration backups and rebuilding derived bars."
        )
    )
    parser.add_argument("--datastore", type=Path, default=Path(r"C:\DATASTORE"))
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument("--target-dataset", default="EQUS.MINI")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Apply the migration. Without this flag, validation is dry-run only.",
    )
    args = parser.parse_args(argv)
    result = migrate_operational_equities(
        args.datastore,
        symbols=args.symbols,
        target_dataset=args.target_dataset,
        confirm=args.confirm,
    )
    print(json.dumps({
        "datastore_root": str(result.datastore_root),
        "target_dataset": result.target_dataset,
        "symbols": result.symbols,
        "native_files_restored": result.native_files_restored,
        "derived_files_rebuilt": result.derived_files_rebuilt,
        "prior_files_backed_up": result.prior_files_backed_up,
        "receipt_path": str(result.receipt_path) if result.receipt_path else None,
        "dry_run": result.dry_run,
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
