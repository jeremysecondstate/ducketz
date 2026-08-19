"""Verified bridges from the Databento cold archive into recurring Loops.

The cold-start coordinator deliberately owns immutable provider evidence while
the recurring owners write operational views.  This module is the missing
handoff between those two storage contracts: it verifies cold partitions,
materializes equity OHLCV into Loop A's canonical continuation files, and
exposes CME archive rows/cursors to the independent CME owner.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping, Sequence

import pandas as pd
import pyarrow.parquet as pq

from app.services.market_fetch_specs import DatabentoAnalysisSourceSpec
from datafetching.bar_schema import (
    NORMALIZED_BAR_VALUE_COLUMNS,
    project_normalized_bar_frame,
    read_normalized_bar_parquet,
    write_normalized_bar_parquet,
)
from datafetching.bar_timing import annotate_bar_timing
from datafetching.continuation import normalized_bar_path
from datafetching.databento_storage import (
    MARKET_CME,
    MARKET_US_EQUITIES,
    dataset_root,
    history_cursor_path,
)
from ml.artifacts import file_checksum

if TYPE_CHECKING:
    from app.services.databento_cme_context import DatabentoCmeContextSpec
    from datafetching.cme_history import CmeCursor


ARCHIVE_BRIDGE_VERSION = "databento-archive-bridge-v1"
ARCHIVE_LINEAGE_VERSION = "databento-archive-lineage-v1"
COLD_PARTITION_VERSION = "databento-cold-start-partition-v1"
COLD_RECEIPT_VERSION = "databento-cold-start-receipt-v1"
EQUITY_ARCHIVE_DATASET_ENV = "DATABENTO_EQUITIES_HISTORY_DATASET"
DEFAULT_EQUITY_ARCHIVE_DATASET = "XNAS.ITCH"
_CME_ARCHIVE_BATCH_ROWS = 50_000


class DatabentoArchiveError(RuntimeError):
    """A cold archive partition or bridge contract failed verification."""


@dataclass(frozen=True)
class VerifiedArchivePartition:
    directory: Path
    normalized_path: Path
    manifest_path: Path
    receipt_path: Path
    manifest: Mapping[str, object]
    receipt: Mapping[str, object]

    @property
    def request(self) -> Mapping[str, object]:
        request = self.manifest.get("request")
        if not isinstance(request, Mapping):
            raise DatabentoArchiveError(
                f"Archive manifest has no request identity: {self.manifest_path}"
            )
        return request

    @property
    def fingerprint(self) -> str:
        return ":".join(
            (
                str(self.receipt.get("manifest_checksum_sha256") or ""),
                str(self.receipt.get("normalized_checksum_sha256") or ""),
                str(self.receipt.get("request_id") or ""),
            )
        )

    @property
    def source_files(self) -> tuple[Path, ...]:
        return (self.normalized_path, self.manifest_path, self.receipt_path)


@dataclass(frozen=True)
class EquityArchiveBridgeResult:
    dataset: str
    partitions: int
    materialized_files: int
    reused_files: int
    archive_rows: int
    output_rows: int
    lineage_files: tuple[Path, ...]


def discover_archive_partitions(
    datastore_root: Path,
    *,
    market: str,
    dataset: str,
    schema: str | None = None,
    symbol: str | None = None,
    verify_payload: bool = False,
) -> tuple[VerifiedArchivePartition, ...]:
    """Return receipt-verified generic cold partitions in deterministic order."""

    root = dataset_root(Path(datastore_root), market=market, dataset=dataset)
    if not root.is_dir():
        return ()
    candidates = root.rglob("manifest.json")
    partitions: list[VerifiedArchivePartition] = []
    clean_symbol = str(symbol or "").strip().upper()
    clean_schema = str(schema or "").strip()
    for manifest_path in sorted(candidates):
        if ".staging" in manifest_path.parts:
            continue
        directory = manifest_path.parent
        receipt_path = directory / "receipt.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DatabentoArchiveError(
                f"Archive metadata is unreadable: {directory}"
            ) from exc
        if not isinstance(manifest, Mapping) or not isinstance(receipt, Mapping):
            raise DatabentoArchiveError(f"Archive metadata is malformed: {directory}")
        request = manifest.get("request")
        normalized = manifest.get("normalized")
        if not isinstance(request, Mapping) or not isinstance(normalized, Mapping):
            raise DatabentoArchiveError(
                f"Archive manifest lacks request/normalized metadata: {directory}"
            )
        request_symbols = request.get("symbol_scope")
        if not isinstance(request_symbols, list) or len(request_symbols) != 1:
            raise DatabentoArchiveError(
                f"Archive request has invalid symbol scope: {directory}"
            )
        request_symbol = str(request_symbols[0]).strip().upper()
        if clean_schema and str(request.get("schema") or "") != clean_schema:
            continue
        if clean_symbol and request_symbol != clean_symbol:
            continue
        if str(request.get("dataset") or "") != dataset:
            raise DatabentoArchiveError(
                f"Archive dataset identity does not match its root: {directory}"
            )
        if (
            manifest.get("schema_version") != COLD_PARTITION_VERSION
            or receipt.get("schema_version") != COLD_RECEIPT_VERSION
            or receipt.get("request_id") != request.get("request_id")
            or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
            or receipt.get("normalized_checksum_sha256")
            != normalized.get("checksum_sha256")
        ):
            raise DatabentoArchiveError(
                f"Archive receipt does not verify its manifest: {directory}"
            )
        normalized_path = directory / str(normalized.get("path") or "")
        if (
            not normalized_path.is_file()
            or normalized_path.stat().st_size
            != int(normalized.get("size_bytes") or -1)
        ):
            raise DatabentoArchiveError(
                f"Archive normalized payload is missing or has changed size: {directory}"
            )
        partition = VerifiedArchivePartition(
            directory=directory,
            normalized_path=normalized_path,
            manifest_path=manifest_path,
            receipt_path=receipt_path,
            manifest=manifest,
            receipt=receipt,
        )
        if verify_payload:
            verify_archive_payload(partition)
        partitions.append(partition)
    return tuple(
        sorted(
            partitions,
            key=lambda item: (
                str(item.request.get("start") or ""),
                str(item.request.get("end") or ""),
                item.directory.as_posix(),
            ),
        )
    )


def verify_archive_payload(partition: VerifiedArchivePartition) -> None:
    expected = str(partition.receipt.get("normalized_checksum_sha256") or "")
    if not expected or file_checksum(partition.normalized_path) != expected:
        raise DatabentoArchiveError(
            "Archive normalized checksum verification failed: "
            f"{partition.normalized_path}"
        )


def configured_equity_archive_dataset() -> str:
    return (
        os.getenv(EQUITY_ARCHIVE_DATASET_ENV, "").strip()
        or DEFAULT_EQUITY_ARCHIVE_DATASET
    )


def materialize_equity_archive_baseline(
    datastore_root: Path,
    *,
    symbols: Iterable[str],
    live_dataset: str,
    source_specs: Sequence[DatabentoAnalysisSourceSpec],
    archive_dataset: str | None = None,
    as_of: object | None = None,
) -> EquityArchiveBridgeResult:
    """Seed Loop A native OHLCV files from matching verified cold history.

    A live dataset and its baseline must have the same provider identity.  This
    avoids silently timestamp-merging venue-specific XNAS observations with a
    consolidated EQUS feed.  The existing operational file is retained only
    when its provenance matches; a mismatched legacy file is copied into a
    recoverable migration-backup directory before replacement.
    """

    root = Path(datastore_root).resolve()
    selected_dataset = str(
        archive_dataset or configured_equity_archive_dataset()
    ).strip()
    live = str(live_dataset).strip()
    archive_root = dataset_root(
        root,
        market=MARKET_US_EQUITIES,
        dataset=selected_dataset,
    )
    if not archive_root.is_dir():
        return EquityArchiveBridgeResult(selected_dataset, 0, 0, 0, 0, 0, ())
    if selected_dataset != live:
        raise DatabentoArchiveError(
            "Loop A cannot build on a different Databento equity dataset: "
            f"archive={selected_dataset}, live={live}. Set "
            f"DATABENTO_EQUITIES_DATASET={selected_dataset} or fetch a matching "
            "historical baseline."
        )

    observed_at = _utc(as_of)
    clean_symbols = tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in symbols
            if str(value).strip()
        )
    )
    partition_count = materialized = reused = archive_rows = output_rows = 0
    lineage_paths: list[Path] = []
    for symbol in clean_symbols:
        for spec in source_specs:
            partitions = discover_archive_partitions(
                root,
                market=MARKET_US_EQUITIES,
                dataset=selected_dataset,
                schema=spec.schema,
                symbol=symbol,
            )
            if not partitions:
                continue
            partition_count += len(partitions)
            request_key = f"{spec.key}_{spec.schema}_{spec.frequency}"
            target = normalized_bar_path(
                root,
                source="databento",
                symbol=symbol,
                timeframe=spec.frequency,
                request_key=request_key,
            )
            lineage_path = archive_lineage_path(target)
            lineage_paths.append(lineage_path)
            fingerprint = _partition_fingerprint(partitions)
            prior_lineage = _read_json(lineage_path)
            if (
                target.is_file()
                and prior_lineage.get("schema_version") == ARCHIVE_LINEAGE_VERSION
                and prior_lineage.get("archive_fingerprint") == fingerprint
                and prior_lineage.get("archive_dataset") == selected_dataset
                and prior_lineage.get("live_dataset") == live
            ):
                reused += 1
                continue

            frames: list[pd.DataFrame] = []
            for partition in partitions:
                verify_archive_payload(partition)
                frames.append(
                    _canonical_equity_bars(
                        pd.read_parquet(partition.normalized_path),
                        symbol=symbol,
                        timeframe=spec.frequency,
                        as_of=observed_at,
                    )
                )
            historical = (
                pd.concat(frames, ignore_index=True, sort=False)
                .sort_values("timestamp", kind="stable")
                .drop_duplicates("timestamp", keep="last")
                .reset_index(drop=True)
            )
            archive_rows += len(historical)
            existing = pd.DataFrame(columns=NORMALIZED_BAR_VALUE_COLUMNS)
            existing_datasets = _existing_bar_datasets(target)
            migrated_from: list[str] = []
            if target.is_file():
                if existing_datasets and existing_datasets != {live}:
                    migrated_from = sorted(existing_datasets)
                    _backup_mismatched_bar(target, existing_datasets)
                else:
                    existing, _schema = read_normalized_bar_parquet(
                        target,
                        include_ids=False,
                    )
            combined = (
                pd.concat([historical, existing], ignore_index=True, sort=False)
                .sort_values("timestamp", kind="stable")
                .drop_duplicates("timestamp", keep="last")
                .reset_index(drop=True)
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(f".tmp-{os.getpid()}.parquet")
            try:
                write_normalized_bar_parquet(combined, temporary)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            output_rows += len(combined)
            materialized += 1
            _write_json_atomic(
                lineage_path,
                {
                    "schema_version": ARCHIVE_LINEAGE_VERSION,
                    "bridge_version": ARCHIVE_BRIDGE_VERSION,
                    "archive_dataset": selected_dataset,
                    "live_dataset": live,
                    "symbol": symbol,
                    "schema": spec.schema,
                    "timeframe": spec.frequency,
                    "request_key": request_key,
                    "archive_fingerprint": fingerprint,
                    "archive_rows": len(historical),
                    "output_rows_at_materialization": len(combined),
                    "earliest_archive_timestamp": historical["timestamp"].min().isoformat(),
                    "latest_archive_timestamp": historical["timestamp"].max().isoformat(),
                    "materialized_at": observed_at.isoformat(),
                    "migrated_from_datasets": migrated_from,
                    "target_path": target.relative_to(root).as_posix(),
                    "source_files": _source_inventory(root, partitions),
                },
            )
    return EquityArchiveBridgeResult(
        selected_dataset,
        partition_count,
        materialized,
        reused,
        archive_rows,
        output_rows,
        tuple(dict.fromkeys(lineage_paths)),
    )


def archive_lineage_path(parquet_path: Path) -> Path:
    return parquet_path.with_suffix(".archive-lineage.json")


def archive_lineage_sources(
    datastore_root: Path,
    parquet_path: Path,
) -> tuple[Path, ...]:
    """Resolve immutable archive inputs declared by a materialized view."""

    root = Path(datastore_root).resolve()
    lineage_path = archive_lineage_path(parquet_path)
    payload = _read_json(lineage_path)
    if payload.get("schema_version") != ARCHIVE_LINEAGE_VERSION:
        return ()
    sources: list[Path] = [lineage_path]
    inventory = payload.get("source_files")
    if not isinstance(inventory, list):
        raise DatabentoArchiveError(f"Archive lineage has no source inventory: {lineage_path}")
    for item in inventory:
        if not isinstance(item, Mapping):
            raise DatabentoArchiveError(f"Archive lineage inventory is invalid: {lineage_path}")
        path = (root / str(item.get("path") or "")).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise DatabentoArchiveError(
                f"Archive lineage source escapes the datastore: {path}"
            ) from exc
        if not path.is_file() or file_checksum(path) != str(
            item.get("checksum_sha256") or ""
        ):
            raise DatabentoArchiveError(
                f"Archive lineage source failed verification: {path}"
            )
        sources.append(path)
    return tuple(dict.fromkeys(sources))


def archive_lineage_metadata(parquet_path: Path) -> Mapping[str, object]:
    payload = _read_json(archive_lineage_path(parquet_path))
    if payload.get("schema_version") != ARCHIVE_LINEAGE_VERSION:
        return {}
    return payload


def publish_archive_lineage(
    datastore_root: Path,
    parquet_path: Path,
    *,
    archive_dataset: str,
    live_dataset: str,
    source_files: Sequence[Path],
    metadata: Mapping[str, object] | None = None,
) -> Path:
    """Publish checksum-bound cold-source lineage for a derived Parquet."""

    root = Path(datastore_root).resolve()
    target = Path(parquet_path).resolve()
    sources = tuple(dict.fromkeys(Path(value).resolve() for value in source_files))
    inventory: list[dict[str, object]] = []
    for source in sources:
        try:
            relative = source.relative_to(root)
        except ValueError as exc:
            raise DatabentoArchiveError(
                f"Archive lineage source escapes the datastore: {source}"
            ) from exc
        if not source.is_file():
            raise DatabentoArchiveError(
                f"Archive lineage source does not exist: {source}"
            )
        inventory.append(
            {
                "path": relative.as_posix(),
                "size_bytes": source.stat().st_size,
                "checksum_sha256": file_checksum(source),
            }
        )
    payload: dict[str, object] = {
        "schema_version": ARCHIVE_LINEAGE_VERSION,
        "bridge_version": ARCHIVE_BRIDGE_VERSION,
        "archive_dataset": str(archive_dataset),
        "live_dataset": str(live_dataset),
        "target_path": target.relative_to(root).as_posix(),
        "source_files": inventory,
    }
    payload.update(dict(metadata or {}))
    path = archive_lineage_path(target)
    _write_json_atomic(path, payload)
    return path


def load_cme_archive_frame(
    datastore_root: Path,
    *,
    dataset: str,
    schema: str,
    symbols: Sequence[str],
) -> tuple[pd.DataFrame, tuple[Path, ...], str]:
    """Load verified CME cold rows in the live context calculation contract."""

    root = Path(datastore_root).resolve()
    frames: list[pd.DataFrame] = []
    source_files: list[Path] = []
    fingerprints: list[str] = []
    for symbol in symbols:
        partitions = discover_archive_partitions(
            root,
            market=MARKET_CME,
            dataset=dataset,
            schema=schema,
            symbol=symbol,
        )
        for partition in partitions:
            verify_archive_payload(partition)
            frame = _load_cme_archive_partition(
                partition.normalized_path,
                dataset=dataset,
                schema=schema,
            )
            if not frame.empty:
                frames.append(frame)
            source_files.extend(partition.source_files)
            fingerprints.append(partition.fingerprint)
    combined = (
        pd.concat(frames, ignore_index=True, sort=False)
        if frames
        else pd.DataFrame()
    )
    return (
        combined,
        tuple(dict.fromkeys(source_files)),
        _fingerprint_values(fingerprints),
    )


def cme_archive_source_inventory(
    datastore_root: Path,
    *,
    dataset: str,
    schema: str,
    symbols: Sequence[str],
) -> tuple[tuple[Path, ...], str]:
    """Return cheap receipt lineage and an immutable archive fingerprint."""

    root = Path(datastore_root).resolve()
    metadata_files: list[Path] = []
    fingerprints: list[str] = []
    for symbol in symbols:
        partitions = discover_archive_partitions(
            root,
            market=MARKET_CME,
            dataset=dataset,
            schema=schema,
            symbol=symbol,
        )
        for partition in partitions:
            metadata_files.extend((partition.manifest_path, partition.receipt_path))
            fingerprints.append(partition.fingerprint)
    return (
        tuple(dict.fromkeys(metadata_files)),
        _fingerprint_values(fingerprints),
    )


def cme_archive_cursor_for_spec(
    datastore_root: Path,
    spec: "DatabentoCmeContextSpec",
) -> "CmeCursor | None":
    """Construct the first live cursor from the complete cold scope, if present."""

    from datafetching.cme_history import CmeCursor

    root = Path(datastore_root).resolve()
    completed: list[pd.Timestamp] = []
    successful: list[pd.Timestamp] = []
    latest_events: list[pd.Timestamp] = []
    for symbol in spec.symbols:
        cursor_path = history_cursor_path(
            root,
            market=MARKET_CME,
            dataset=spec.dataset,
            schema=spec.schema,
            symbol=symbol,
        )
        cursor = _read_json(cursor_path)
        if cursor.get("schema_version") != "databento-cold-start-request-cursor-v1":
            return None
        if (
            cursor.get("dataset") != spec.dataset
            or cursor.get("schema") != spec.schema
            or cursor.get("symbol_scope") != [symbol]
        ):
            raise DatabentoArchiveError(
                f"CME archive cursor identity is invalid: {cursor_path}"
            )
        through = pd.to_datetime(
            cursor.get("completed_through"), utc=True, errors="coerce"
        )
        if pd.isna(through):
            raise DatabentoArchiveError(
                f"CME archive cursor has no completion boundary: {cursor_path}"
            )
        completed.append(pd.Timestamp(through))
        completed_at = pd.to_datetime(
            cursor.get("completed_at"), utc=True, errors="coerce"
        )
        if not pd.isna(completed_at):
            successful.append(pd.Timestamp(completed_at))
        partitions = discover_archive_partitions(
            root,
            market=MARKET_CME,
            dataset=spec.dataset,
            schema=spec.schema,
            symbol=symbol,
        )
        for partition in partitions:
            normalized = partition.manifest.get("normalized")
            if isinstance(normalized, Mapping):
                latest = pd.to_datetime(
                    normalized.get("latest_timestamp"), utc=True, errors="coerce"
                )
                if not pd.isna(latest):
                    latest_events.append(pd.Timestamp(latest))
    if not completed:
        return None
    queried_through = min(completed)
    return CmeCursor(
        spec.group_key,
        spec.schema,
        queried_through,
        max(successful) if successful else queried_through,
        max(latest_events) if latest_events else None,
        0,
    )


def _canonical_equity_bars(
    raw: pd.DataFrame,
    *,
    symbol: str,
    timeframe: str,
    as_of: pd.Timestamp,
) -> pd.DataFrame:
    frame = raw.reset_index()
    timestamp_column = next(
        (column for column in ("ts_event", "timestamp") if column in frame.columns),
        None,
    )
    if timestamp_column is None:
        raise DatabentoArchiveError("Equity OHLCV archive has no event timestamp")
    if "symbol" in frame.columns:
        frame = frame.loc[
            frame["symbol"].astype("string").str.upper().eq(symbol)
        ].copy()
    required = {"open", "high", "low", "close"}
    if missing := sorted(required.difference(frame.columns)):
        raise DatabentoArchiveError(
            "Equity OHLCV archive is missing columns: " + ", ".join(missing)
        )
    output = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                frame[timestamp_column], utc=True, errors="coerce"
            ),
            "open": pd.to_numeric(frame["open"], errors="coerce"),
            "high": pd.to_numeric(frame["high"], errors="coerce"),
            "low": pd.to_numeric(frame["low"], errors="coerce"),
            "close": pd.to_numeric(frame["close"], errors="coerce"),
            "volume": (
                pd.to_numeric(frame["volume"], errors="coerce")
                if "volume" in frame
                else pd.Series(0.0, index=frame.index)
            ),
        }
    ).dropna(subset=["timestamp", "open", "high", "low", "close"])
    annotated = annotate_bar_timing(output, timeframe=timeframe, as_of=as_of)
    complete = annotated["bar_complete"].fillna(False).astype(bool)
    return project_normalized_bar_frame(
        annotated.loc[complete, list(NORMALIZED_BAR_VALUE_COLUMNS)],
        include_ids=False,
    )


def _canonical_cme_rows(
    frame: pd.DataFrame,
    *,
    dataset: str,
    schema: str,
) -> pd.DataFrame:
    output = frame.copy()
    event_column = next(
        (column for column in ("ts_event", "timestamp", "ts_recv") if column in output),
        None,
    )
    if event_column is None or "symbol" not in output:
        raise DatabentoArchiveError(
            f"CME {schema} archive has no event timestamp or symbol"
        )
    event = pd.to_datetime(output[event_column], utc=True, errors="coerce")
    if schema.startswith("ohlcv-"):
        duration = _schema_duration(schema.removeprefix("ohlcv-"))
        available = event + duration
        output["timeframe"] = schema.removeprefix("ohlcv-")
    else:
        receive_column = "ts_recv" if "ts_recv" in output else event_column
        available = pd.to_datetime(
            output[receive_column], utc=True, errors="coerce"
        )
    output["timestamp"] = event
    output["fetched_at"] = available
    output["provider_symbol"] = output["symbol"].astype("string")
    output["provider_dataset"] = dataset
    output["provider_schema"] = schema
    return output.loc[
        output["timestamp"].notna() & output["fetched_at"].notna()
    ].reset_index(drop=True)


def _read_cme_partition(path: Path, *, schema: str) -> pd.DataFrame:
    available = set(pq.ParquetFile(path).schema_arrow.names)
    selected = _cme_partition_columns(available, schema=schema)
    return pd.read_parquet(path, columns=selected).reset_index()


def _load_cme_archive_partition(
    path: Path,
    *,
    dataset: str,
    schema: str,
) -> pd.DataFrame:
    """Read high-volume CME events without expanding a partition in memory.

    BBO and MBP history only contributes the final event for each symbol/hour
    to the hourly context calculation.  Compacting every bounded Arrow batch,
    then compacting those retained rows once more, preserves that exact result
    while keeping memory proportional to the batch size instead of the archive.
    """

    if schema not in {"bbo-1m", "mbp-10"}:
        raw = _read_cme_partition(path, schema=schema)
        frame = _canonical_cme_rows(raw, dataset=dataset, schema=schema)
        return _compact_cme_context_rows(frame, schema=schema)

    parquet = pq.ParquetFile(path)
    selected = _cme_partition_columns(
        set(parquet.schema_arrow.names),
        schema=schema,
    )
    compacted: list[pd.DataFrame] = []
    for batch in parquet.iter_batches(
        batch_size=_CME_ARCHIVE_BATCH_ROWS,
        columns=selected,
        use_threads=True,
    ):
        raw = batch.to_pandas()
        if raw.index.name is not None and raw.index.name not in raw.columns:
            raw = raw.reset_index()
        else:
            raw = raw.reset_index(drop=True)
        frame = _canonical_cme_rows(raw, dataset=dataset, schema=schema)
        frame = _compact_cme_context_rows(frame, schema=schema)
        if not frame.empty:
            compacted.append(frame)
    if not compacted:
        return pd.DataFrame()
    return _compact_cme_context_rows(
        pd.concat(compacted, ignore_index=True, sort=False),
        schema=schema,
    )


def _cme_partition_columns(
    available: set[str],
    *,
    schema: str,
) -> list[str]:
    if schema == "ohlcv-1m":
        wanted = (
            "ts_event",
            "symbol",
            "open",
            "high",
            "low",
            "close",
            "volume",
        )
    elif schema == "bbo-1m":
        wanted = (
            "ts_event",
            "ts_recv",
            "symbol",
            "sequence",
            "bid_px_00",
            "ask_px_00",
        )
    elif schema == "mbp-10":
        wanted = (
            "ts_event",
            "ts_recv",
            "symbol",
            "sequence",
            *(f"bid_sz_{depth:02d}" for depth in range(10)),
            *(f"ask_sz_{depth:02d}" for depth in range(10)),
        )
    else:
        wanted = tuple(available)
    return [column for column in wanted if column in available]


def _compact_cme_context_rows(frame: pd.DataFrame, *, schema: str) -> pd.DataFrame:
    if frame.empty or schema == "ohlcv-1m":
        return frame
    if schema not in {"bbo-1m", "mbp-10"}:
        return frame
    output = frame.copy()
    output["__window"] = pd.to_datetime(
        output["timestamp"], utc=True, errors="coerce"
    ).dt.floor("1h")
    symbol_column = "provider_symbol" if "provider_symbol" in output else "symbol"
    output = (
        output.sort_values("timestamp", kind="stable")
        .drop_duplicates([symbol_column, "__window"], keep="last")
        .drop(columns="__window")
        .reset_index(drop=True)
    )
    return output


def _schema_duration(value: str) -> pd.Timedelta:
    text = str(value).strip().lower()
    if len(text) < 2 or not text[:-1].isdigit():
        raise DatabentoArchiveError(f"Unsupported CME OHLCV schema duration: {value}")
    units = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
    unit = units.get(text[-1])
    if unit is None:
        raise DatabentoArchiveError(f"Unsupported CME OHLCV schema duration: {value}")
    return pd.Timedelta(**{unit: int(text[:-1])})


def _existing_bar_datasets(target: Path) -> set[str]:
    raw_path = (
        target.parent.parent
        / "raw"
        / f"{target.stem}_raw.parquet"
    )
    if not raw_path.is_file():
        return set()
    try:
        values = pd.read_parquet(raw_path, columns=["provider_dataset"])
    except (OSError, ValueError, KeyError):
        return set()
    return {
        str(value).strip()
        for value in values["provider_dataset"].dropna().unique()
        if str(value).strip()
    }


def _backup_mismatched_bar(target: Path, datasets: set[str]) -> Path:
    checksum = file_checksum(target)[:16]
    label = "_and_".join(sorted(value.replace("/", "-") for value in datasets))
    backup = (
        target.parent.parent
        / "migration-backups"
        / f"{target.stem}.{label}.{checksum}.parquet"
    )
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.is_file():
        shutil.copy2(target, backup)
    raw_path = target.parent.parent / "raw" / f"{target.stem}_raw.parquet"
    if raw_path.is_file():
        raw_backup = backup.with_name(f"{backup.stem}_raw.parquet")
        if not raw_backup.is_file():
            shutil.copy2(raw_path, raw_backup)
        raw_path.unlink()
    return backup


def _partition_fingerprint(
    partitions: Sequence[VerifiedArchivePartition],
) -> str:
    return _fingerprint_values(partition.fingerprint for partition in partitions)


def _fingerprint_values(values: Iterable[str]) -> str:
    import hashlib

    digest = hashlib.sha256()
    found = False
    for value in sorted(str(item) for item in values):
        digest.update(value.encode("utf-8"))
        found = True
    return digest.hexdigest() if found else ""


def _source_inventory(
    root: Path,
    partitions: Sequence[VerifiedArchivePartition],
) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    for partition in partitions:
        for path in partition.source_files:
            inventory.append(
                {
                    "path": path.resolve().relative_to(root).as_posix(),
                    "size_bytes": path.stat().st_size,
                    "checksum_sha256": file_checksum(path),
                }
            )
    return inventory


def _read_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatabentoArchiveError(f"Archive bridge JSON is unreadable: {path}") from exc
    if not isinstance(payload, dict):
        raise DatabentoArchiveError(f"Archive bridge JSON is malformed: {path}")
    return payload


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _utc(value: object | None) -> pd.Timestamp:
    if value is None:
        return pd.Timestamp.now(tz="UTC")
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("Archive bridge timestamp is invalid")
    return pd.Timestamp(timestamp)


__all__ = [
    "ARCHIVE_BRIDGE_VERSION",
    "ARCHIVE_LINEAGE_VERSION",
    "DatabentoArchiveError",
    "EquityArchiveBridgeResult",
    "VerifiedArchivePartition",
    "archive_lineage_path",
    "archive_lineage_metadata",
    "archive_lineage_sources",
    "cme_archive_source_inventory",
    "cme_archive_cursor_for_spec",
    "configured_equity_archive_dataset",
    "discover_archive_partitions",
    "load_cme_archive_frame",
    "materialize_equity_archive_baseline",
    "publish_archive_lineage",
    "verify_archive_payload",
]
