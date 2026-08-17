from __future__ import annotations

import hashlib
import gc
import itertools
import json
import math
import shutil
import time
import warnings
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

from datafetching.databento_storage import (
    MARKET_OPRA,
    clean_token,
    dataset_root,
    opra_partition_directory,
    symbol_scope_name,
    window_name,
)
from ml.artifacts import file_checksum, utc_timestamp


DATASET = "OPRA.PILLAR"
PROVIDER = "databento-opra"
SYNC_VERSION = "databento-opra-standard-sync-v1"
PARTITION_VERSION = "databento-opra-partition-v1"
RECEIPT_VERSION = "databento-opra-partition-receipt-v1"
ENTITLEMENT_VERSION = "databento-opra-entitlement-v2"
HEALTH_VERSION = "databento-opra-health-v1"

L0_SCHEMAS = (
    "ohlcv-1s",
    "ohlcv-1m",
    "ohlcv-1h",
    "ohlcv-1d",
    "definition",
    "statistics",
    "status",
)
L1_SCHEMAS = ("cmbp-1", "tcbbo", "cbbo-1s", "cbbo-1m", "trades")
STANDARD_SCHEMAS = (*L0_SCHEMAS, *L1_SCHEMAS)

METADATA_TIMEOUT_SECONDS = 30
TIMESERIES_TIMEOUT_SECONDS = 300
DOWNLOAD_MAX_ATTEMPTS = 3
STORAGE_RESERVE_BYTES = 5 * 1024**3
STORAGE_EXPANSION_FACTOR = 2.0
MAX_EXACT_VALIDATION_ROWS = 25_000_000
TARGET_HIGH_VOLUME_PARTITION_ROWS = 20_000_000
MINIMUM_TIME_PARTITION_SECONDS = 1
HIGH_VOLUME_SCHEMAS = frozenset(("cmbp-1", "cbbo-1s"))
STANDARD_PLAN_AUTHORITY = "docs/databento-plan/databento_standard_plan_data_access.md"


class OpraSyncError(RuntimeError):
    """Canonical OPRA synchronization failed closed."""


class OpraCapacityError(OpraSyncError):
    """The requested canonical OPRA scope cannot fit on its destination volume."""


class OpraNoDataError(OpraSyncError):
    """A structurally valid provider response has no records for the request."""


@dataclass(frozen=True)
class SyncScope:
    schemas: tuple[str, ...] = STANDARD_SCHEMAS
    start: str | None = None
    end: str | None = None
    symbols: tuple[str, ...] = ()
    max_partitions: int | None = None


@dataclass(frozen=True)
class SyncResult:
    status: str
    completed_partitions: int
    skipped_partitions: int
    completed_rows: int
    completed_bytes: int
    errors: Mapping[str, str]
    health_path: Path


def canonical_root(datastore_root: Path) -> Path:
    return dataset_root(datastore_root, market=MARKET_OPRA, dataset=DATASET)


def configure_client(client: object) -> None:
    for endpoint_name, timeout in (
        ("metadata", METADATA_TIMEOUT_SECONDS),
        ("timeseries", TIMESERIES_TIMEOUT_SECONDS),
    ):
        endpoint = getattr(client, endpoint_name, None)
        if endpoint is None or not hasattr(endpoint, "TIMEOUT"):
            raise TypeError(f"Databento client has no {endpoint_name} timeout control")
        setattr(endpoint, "TIMEOUT", timeout)


def discover_standard_entitlement(
    client: object,
    *,
    datastore_root: Path,
    observed_at: object | None = None,
) -> Mapping[str, object]:
    """Discover dataset and account bounds without issuing a timeseries download."""

    configure_client(client)
    metadata = getattr(client, "metadata")
    dataset_range = _retry(
        metadata.get_dataset_range,
        kwargs={"dataset": DATASET},
        operation="dataset range",
    )
    schemas = tuple(
        _retry(
            metadata.list_schemas,
            kwargs={"dataset": DATASET},
            operation="schema list",
        )
    )
    missing = sorted(set(STANDARD_SCHEMAS).difference(schemas))
    if missing:
        raise OpraSyncError(
            "Databento did not advertise required OPRA schemas: " + ", ".join(missing)
        )
    if not isinstance(dataset_range, Mapping):
        raise OpraSyncError("Databento returned a malformed OPRA dataset range")
    schema_ranges = dataset_range.get("schema")
    if not isinstance(schema_ranges, Mapping):
        raise OpraSyncError("Databento returned no schema-specific OPRA ranges")

    entitlements: dict[str, dict[str, object]] = {}
    for schema in STANDARD_SCHEMAS:
        raw_range = schema_ranges.get(schema)
        if not isinstance(raw_range, Mapping):
            raise OpraSyncError(f"Databento returned no range for {schema}")
        dataset_start = _date_text(raw_range["start"])
        dataset_end = _date_text(raw_range["end"])
        included_policy = standard_plan_history_policy(schema)
        plan_start = _history_window_start(dataset_end, included_policy)
        entitled_start = max(dataset_start, plan_start)
        if entitled_start >= dataset_end:
            raise OpraSyncError(
                f"Provider range does not overlap the configured Standard-plan "
                f"window for {schema}: provider_start={dataset_start} "
                f"included_start={plan_start} end={dataset_end}"
            )
        entitlements[schema] = {
            "level": "L0" if schema in L0_SCHEMAS else "L1",
            "dataset_start": dataset_start,
            "configured_included_start": plan_start,
            "entitled_start": entitled_start,
            "entitled_end": dataset_end,
            "included_history_policy": included_policy,
        }

    timestamp = utc_timestamp(observed_at)
    payload: dict[str, object] = {
        "schema_version": ENTITLEMENT_VERSION,
        "provider": PROVIDER,
        "dataset": DATASET,
        "observed_at": timestamp.isoformat(),
        "provider_dataset_range": dataset_range,
        "provider_schema_names": list(schemas),
        "standard_schema_names": list(STANDARD_SCHEMAS),
        "entitlement_authority": STANDARD_PLAN_AUTHORITY,
        "entitlements": entitlements,
    }
    payload["semantic_checksum_sha256"] = _semantic_checksum(payload)
    destination = canonical_root(datastore_root) / "metadata" / "entitlement.json"
    _write_json_atomic(destination, payload)
    return {**payload, "path": destination}


def storage_preflight(
    client: object,
    *,
    datastore_root: Path,
    entitlement: Mapping[str, object],
    scope: SyncScope,
) -> Mapping[str, object]:
    requests = _scope_ranges(entitlement, scope)
    metadata = getattr(client, "metadata")
    estimates: dict[str, dict[str, object]] = {}
    for schema, start, end in requests:
        kwargs = _metadata_request_kwargs(
            schema=schema,
            start=start,
            end=end,
            symbols=scope.symbols,
        )
        estimated_download_size = int(
            _retry(
                metadata.get_billable_size,
                kwargs=kwargs,
                operation=f"{schema} estimated download size",
            )
        )
        records = int(
            _retry(
                metadata.get_record_count,
                kwargs=kwargs,
                operation=f"{schema} record count",
            )
        )
        estimates[schema] = {
            "start": start,
            "end": end,
            "symbols": list(scope.symbols) or ["ALL_SYMBOLS"],
            "estimated_download_size_bytes": estimated_download_size,
            "record_count": records,
        }
    download_size_total = sum(
        int(item["estimated_download_size_bytes"]) for item in estimates.values()
    )
    record_total = sum(int(item["record_count"]) for item in estimates.values())
    required = (
        math.ceil(download_size_total * STORAGE_EXPANSION_FACTOR)
        + STORAGE_RESERVE_BYTES
    )
    usage = shutil.disk_usage(canonical_root(datastore_root).anchor)
    return {
        "schema_version": "databento-opra-storage-preflight-v2",
        "provider": PROVIDER,
        "dataset": DATASET,
        "scope": {
            "schemas": list(scope.schemas),
            "symbols": list(scope.symbols) or ["ALL_SYMBOLS"],
            "start": min((start for _schema, start, _end in requests), default=None),
            "end": max((end for _schema, _start, end in requests), default=None),
        },
        "estimates": estimates,
        "estimated_download_size_bytes": download_size_total,
        "record_count": record_total,
        "storage_expansion_factor": STORAGE_EXPANSION_FACTOR,
        "storage_reserve_bytes": STORAGE_RESERVE_BYTES,
        "required_free_bytes": required,
        "available_free_bytes": usage.free,
        "capacity_pass": usage.free >= required,
        "shortfall_bytes": max(required - usage.free, 0),
    }


def publish_storage_preflight(
    datastore_root: Path,
    preflight: Mapping[str, object],
) -> Mapping[str, object]:
    """Publish an immutable, checksummed record of a provider metadata preflight."""

    timestamp = utc_timestamp()
    payload = {
        **preflight,
        "generated_at": timestamp.isoformat(),
    }
    payload["semantic_checksum_sha256"] = _semantic_checksum(payload)
    destination = _storage_preflight_path(datastore_root, payload)
    _write_json_atomic(destination, payload)
    return {**payload, "path": destination}


def synchronize(
    client: object,
    *,
    datastore_root: Path,
    entitlement: Mapping[str, object],
    scope: SyncScope = SyncScope(),
    reporter: Callable[[str], None] | None = print,
    storage_preflight_receipt: Mapping[str, object] | None = None,
    fail_fast: bool = False,
) -> SyncResult:
    """Download, normalize, publish, and verify immutable OPRA partitions."""

    root = canonical_root(datastore_root)
    root.mkdir(parents=True, exist_ok=True)
    configure_client(client)
    if storage_preflight_receipt is None:
        preflight = storage_preflight(
            client,
            datastore_root=datastore_root,
            entitlement=entitlement,
            scope=scope,
        )
        published_preflight = publish_storage_preflight(datastore_root, preflight)
    else:
        preflight = _validate_storage_preflight_receipt(
            storage_preflight_receipt,
            entitlement=entitlement,
            scope=scope,
        )
        published_preflight = {
            **preflight,
            "path": str(
                preflight.get(
                    "source",
                    "checksum-verified cold-start coordinator preflight",
                )
            ),
        }
    if not bool(preflight["capacity_pass"]):
        health = publish_health(datastore_root)
        raise OpraCapacityError(
            "OPRA storage preflight failed: "
            f"required_free_bytes={preflight['required_free_bytes']} "
            f"available_free_bytes={preflight['available_free_bytes']} "
            f"shortfall_bytes={preflight['shortfall_bytes']} "
            f"preflight={published_preflight['path']} health={health}"
        )

    plan = _partition_plan(client, entitlement=entitlement, scope=scope)
    tasks: Iterable[tuple[str, str, str, str, str | None]] = (
        (schema, day, request_start, request_end, segment)
        for schema, day in plan
        for request_start, request_end, segment in _partition_time_segments(
            client,
            schema=schema,
            day=day,
            symbols=scope.symbols,
        )
    )
    if scope.max_partitions is not None:
        if scope.max_partitions < 1:
            raise ValueError("max_partitions must be positive")
        tasks = itertools.islice(tasks, scope.max_partitions)
    complete = skipped = rows = byte_count = 0
    errors: dict[str, str] = {}
    for schema, day, request_start, request_end, segment in tasks:
        key = f"{schema}/{day}" + (f"/{segment}" if segment else "")
        destination = partition_directory(
            datastore_root,
            schema=schema,
            day=day,
            symbols=scope.symbols,
            segment=segment,
        )
        try:
            if destination.is_dir():
                verified = verify_partition(destination, datastore_root=datastore_root)
                skipped += 1
                rows += int(verified["manifest"]["normalized"]["row_count"])
                byte_count += int(verified["manifest"]["normalized"]["size_bytes"])
                if reporter:
                    reporter(f"VERIFIED_EXISTING {key}")
                continue
            manifest = _download_partition(
                client,
                datastore_root=datastore_root,
                entitlement=entitlement,
                schema=schema,
                day=day,
                symbols=scope.symbols,
                request_start=request_start,
                request_end=request_end,
                segment=segment,
            )
            complete += 1
            rows += int(manifest["normalized"]["row_count"])
            byte_count += int(manifest["normalized"]["size_bytes"])
            _publish_cursor(datastore_root, schema=schema)
            if reporter:
                reporter(
                    f"PUBLISHED {key} rows={manifest['normalized']['row_count']} "
                    f"bytes={manifest['normalized']['size_bytes']}"
                )
        except OpraNoDataError as exc:
            skipped += 1
            if reporter:
                reporter(f"NO_DATA {key} {exc}")
        except Exception as exc:
            errors[key] = f"{type(exc).__name__}: {exc}"
            if reporter:
                reporter(f"FAILED {key} {errors[key]}")
            if fail_fast:
                raise
    health = publish_health(datastore_root)
    return SyncResult(
        status="COMPLETE" if not errors else "PARTIAL",
        completed_partitions=complete,
        skipped_partitions=skipped,
        completed_rows=rows,
        completed_bytes=byte_count,
        errors=errors,
        health_path=health,
    )


def _validate_storage_preflight_receipt(
    receipt: Mapping[str, object],
    *,
    entitlement: Mapping[str, object],
    scope: SyncScope,
) -> Mapping[str, object]:
    """Validate coordinator-owned estimates without calling provider metadata again."""

    if receipt.get("provider") != PROVIDER or receipt.get("dataset") != DATASET:
        raise OpraSyncError("Supplied OPRA storage preflight belongs to another provider scope")
    requests = _scope_ranges(entitlement, scope)
    estimates = receipt.get("estimates")
    if not isinstance(estimates, Mapping):
        raise OpraSyncError("Supplied OPRA storage preflight has no estimate map")
    expected_schemas = {schema for schema, _start, _end in requests}
    if set(str(schema) for schema in estimates) != expected_schemas:
        raise OpraSyncError("Supplied OPRA storage preflight has the wrong schema scope")
    total_size = 0
    total_records = 0
    expected_symbols = list(scope.symbols) or ["ALL_SYMBOLS"]
    for schema, start, end in requests:
        estimate = estimates.get(schema)
        if not isinstance(estimate, Mapping):
            raise OpraSyncError("Supplied OPRA storage preflight estimate is malformed")
        if (
            estimate.get("start") != start
            or estimate.get("end") != end
            or estimate.get("symbols") != expected_symbols
        ):
            raise OpraSyncError("Supplied OPRA storage preflight does not match the request")
        try:
            size = int(estimate["estimated_download_size_bytes"])
            records = int(estimate["record_count"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OpraSyncError("Supplied OPRA storage preflight totals are malformed") from exc
        if size < 0 or records < 0:
            raise OpraSyncError("Supplied OPRA storage preflight contains negative values")
        total_size += size
        total_records += records
    required = math.ceil(total_size * STORAGE_EXPANSION_FACTOR) + STORAGE_RESERVE_BYTES
    expected_totals = {
        "estimated_download_size_bytes": total_size,
        "record_count": total_records,
        "storage_expansion_factor": STORAGE_EXPANSION_FACTOR,
        "storage_reserve_bytes": STORAGE_RESERVE_BYTES,
        "required_free_bytes": required,
    }
    for key, expected in expected_totals.items():
        if receipt.get(key) != expected:
            raise OpraSyncError(f"Supplied OPRA storage preflight has inconsistent {key}")
    try:
        available = int(receipt["available_free_bytes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise OpraSyncError("Supplied OPRA storage preflight has no free-space check") from exc
    if bool(receipt.get("capacity_pass")) != (available >= required):
        raise OpraSyncError("Supplied OPRA storage preflight capacity result is inconsistent")
    if int(receipt.get("shortfall_bytes", -1)) != max(required - available, 0):
        raise OpraSyncError("Supplied OPRA storage preflight shortfall is inconsistent")
    return dict(receipt)


def partition_directory(
    datastore_root: Path,
    *,
    schema: str,
    day: str,
    symbols: Sequence[str] = (),
    segment: str | None = None,
) -> Path:
    return opra_partition_directory(
        datastore_root,
        dataset=DATASET,
        schema=schema,
        day=day,
        symbols=symbols,
        segment=segment,
    )


def symbol_bucket(symbols: Sequence[str]) -> str:
    """Return the readable scope component retained in manifests for compatibility."""

    return symbol_scope_name(symbols)


def verify_partition(directory: Path, *, datastore_root: Path) -> Mapping[str, object]:
    root = canonical_root(datastore_root).resolve()
    run = Path(directory).resolve()
    if root not in run.parents:
        raise OpraSyncError("OPRA partition escapes the canonical root")
    manifest_path = run / "manifest.json"
    receipt_path = run / "receipt.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpraSyncError("OPRA partition metadata is unreadable") from exc
    if (
        manifest.get("schema_version") != PARTITION_VERSION
        or receipt.get("schema_version") != RECEIPT_VERSION
        or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
    ):
        raise OpraSyncError("OPRA partition receipt does not match its manifest")
    for name in ("raw", "normalized"):
        info = manifest.get(name)
        if not isinstance(info, Mapping):
            raise OpraSyncError(f"OPRA partition has no {name} inventory")
        path = run / str(info.get("path"))
        if (
            not path.is_file()
            or path.stat().st_size != int(info.get("size_bytes", -1))
            or file_checksum(path) != info.get("checksum_sha256")
        ):
            raise OpraSyncError(f"OPRA partition {name} checksum verification failed")
    parquet_path = run / str(manifest["normalized"]["path"])
    validation = validate_parquet(parquet_path, schema=str(manifest["schema"]))
    expected = manifest["normalized"]
    for key in (
        "row_count",
        "earliest_event_timestamp",
        "latest_event_timestamp",
        "partition_timestamp_column",
        "earliest_partition_timestamp",
        "latest_partition_timestamp",
        "duplicate_natural_key_rows",
    ):
        if key in expected and validation.get(key) != expected.get(key):
            raise OpraSyncError(f"OPRA partition normalized {key} changed")
    return {"manifest": manifest, "receipt": receipt, "directory": run}


def validate_parquet(path: Path, *, schema: str) -> Mapping[str, object]:
    parquet = pq.ParquetFile(path)
    names = tuple(parquet.schema_arrow.names)
    event_column = _event_column(names)
    partition_column = (
        "ts_recv" if schema in HIGH_VOLUME_SCHEMAS and "ts_recv" in names else event_column
    )
    row_count = int(parquet.metadata.num_rows)
    bounds: dict[str, list[pd.Timestamp | None]] = {
        column: [None, None]
        for column in dict.fromkeys(
            column for column in (event_column, partition_column) if column is not None
        )
    }
    null_counts = {name: 0 for name in names}
    for batch in parquet.iter_batches(batch_size=250_000):
        for index, name in enumerate(names):
            null_counts[name] += int(batch.column(index).null_count)
        for timestamp_column, timestamp_bounds in bounds.items():
            column = batch.column(names.index(timestamp_column))
            if len(column) and column.null_count < len(column):
                minimum = pc.min(column).as_py()
                maximum = pc.max(column).as_py()
                minimum_ts = pd.to_datetime(minimum, utc=True, errors="coerce")
                maximum_ts = pd.to_datetime(maximum, utc=True, errors="coerce")
                if not pd.isna(minimum_ts):
                    timestamp_bounds[0] = (
                        min(timestamp_bounds[0], minimum_ts)
                        if timestamp_bounds[0] is not None
                        else minimum_ts
                    )
                if not pd.isna(maximum_ts):
                    timestamp_bounds[1] = (
                        max(timestamp_bounds[1], maximum_ts)
                        if timestamp_bounds[1] is not None
                        else maximum_ts
                    )
    event_bounds = bounds.get(event_column, [None, None])
    partition_bounds = bounds.get(partition_column, [None, None])
    natural_key = tuple(name for name in _natural_key(schema) if name in names)
    duplicate_rows = _duplicate_rows(path, columns=natural_key, row_count=row_count)
    return {
        "parquet_schema": str(parquet.schema_arrow),
        "columns": list(names),
        "row_count": row_count,
        "event_timestamp_column": event_column,
        "earliest_event_timestamp": (
            event_bounds[0].isoformat() if event_bounds[0] is not None else None
        ),
        "latest_event_timestamp": (
            event_bounds[1].isoformat() if event_bounds[1] is not None else None
        ),
        "partition_timestamp_column": partition_column,
        "earliest_partition_timestamp": (
            partition_bounds[0].isoformat() if partition_bounds[0] is not None else None
        ),
        "latest_partition_timestamp": (
            partition_bounds[1].isoformat() if partition_bounds[1] is not None else None
        ),
        "null_counts": null_counts,
        "null_rates": {
            name: (count / row_count if row_count else 0.0)
            for name, count in null_counts.items()
        },
        "natural_key_columns": list(natural_key),
        "duplicate_natural_key_rows": duplicate_rows,
        "duplicate_natural_key_rate": duplicate_rows / row_count if row_count else 0.0,
    }


def iter_verified_partitions(
    datastore_root: Path,
    *,
    schemas: Iterable[str] | None = None,
) -> Iterable[Mapping[str, object]]:
    root = canonical_root(datastore_root)
    selected = set(schemas or STANDARD_SCHEMAS)
    for schema in sorted(selected):
        for receipt in sorted(root.glob(f"{clean_token(schema)}/*/dates/*/segments/*/receipt.json")):
            try:
                yield verify_partition(receipt.parent, datastore_root=datastore_root)
            except OpraSyncError:
                continue


def publish_health(datastore_root: Path) -> Path:
    root = canonical_root(datastore_root)
    by_schema: dict[str, dict[str, object]] = {}
    for verified in iter_verified_partitions(datastore_root):
        manifest = verified["manifest"]
        schema = str(manifest["schema"])
        normalized = manifest["normalized"]
        item = by_schema.setdefault(
            schema,
            {
                "partition_count": 0,
                "row_count": 0,
                "raw_bytes": 0,
                "parquet_bytes": 0,
                "earliest_event_timestamp": None,
                "latest_event_timestamp": None,
                "consumer_read_count": 0,
            },
        )
        item["partition_count"] = int(item["partition_count"]) + 1
        item["row_count"] = int(item["row_count"]) + int(normalized["row_count"])
        item["raw_bytes"] = int(item["raw_bytes"]) + int(manifest["raw"]["size_bytes"])
        item["parquet_bytes"] = int(item["parquet_bytes"]) + int(normalized["size_bytes"])
        item["earliest_event_timestamp"] = _minimum_timestamp_text(
            item["earliest_event_timestamp"], normalized["earliest_event_timestamp"]
        )
        item["latest_event_timestamp"] = _maximum_timestamp_text(
            item["latest_event_timestamp"], normalized["latest_event_timestamp"]
        )
    usage_path = root / "state" / "consumer-usage.json"
    if usage_path.is_file():
        try:
            usage = json.loads(usage_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            usage = {}
        if isinstance(usage, Mapping):
            for schema, count in usage.get("schema_read_counts", {}).items():
                if schema in by_schema:
                    by_schema[schema]["consumer_read_count"] = int(count)
    payload = {
        "schema_version": HEALTH_VERSION,
        "provider": PROVIDER,
        "dataset": DATASET,
        "observed_at": utc_timestamp().isoformat(),
        "canonical_root": root.as_posix(),
        "schemas": by_schema,
        "total_partitions": sum(int(item["partition_count"]) for item in by_schema.values()),
        "total_rows": sum(int(item["row_count"]) for item in by_schema.values()),
        "total_raw_bytes": sum(int(item["raw_bytes"]) for item in by_schema.values()),
        "total_parquet_bytes": sum(int(item["parquet_bytes"]) for item in by_schema.values()),
    }
    path = root / "health" / "current.json"
    _write_json_atomic(path, payload)
    return path


def record_consumer_usage(
    datastore_root: Path,
    *,
    consumer: str,
    schemas: Sequence[str],
    rows: int,
    source_files: Sequence[Path],
) -> Path:
    root = canonical_root(datastore_root)
    path = root / "state" / "consumer-usage.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except json.JSONDecodeError:
        payload = {}
    counts = dict(payload.get("schema_read_counts", {}))
    for schema in schemas:
        counts[schema] = int(counts.get(schema, 0)) + 1
    events = list(payload.get("events", ()))
    events.append(
        {
            "consumer": str(consumer),
            "consumed_at": utc_timestamp().isoformat(),
            "schemas": list(schemas),
            "rows": int(rows),
            "source_files": [Path(value).resolve().as_posix() for value in source_files],
        }
    )
    _write_json_atomic(
        path,
        {
            "schema_version": "databento-opra-consumer-usage-v1",
            "schema_read_counts": counts,
            "events": events[-1_000:],
        },
    )
    publish_health(datastore_root)
    return path


def _download_partition(
    client: object,
    *,
    datastore_root: Path,
    entitlement: Mapping[str, object],
    schema: str,
    day: str,
    symbols: Sequence[str],
    request_start: str | None = None,
    request_end: str | None = None,
    segment: str | None = None,
) -> Mapping[str, object]:
    destination = partition_directory(
        datastore_root,
        schema=schema,
        day=day,
        symbols=symbols,
        segment=segment,
    )
    staging_root = canonical_root(datastore_root) / ".staging"
    staging = _next_attempt_directory(
        staging_root
        / clean_token(schema)
        / symbol_scope_name(symbols)
        / clean_token(day)
        / clean_token(segment or "full-day")
    )
    staging.mkdir(parents=True, exist_ok=False)
    raw_path = staging / "provider.dbn.zst"
    parquet_path = staging / "normalized.parquet"
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    effective_start = request_start or day
    effective_end = request_end or next_day
    request = {
        "dataset": DATASET,
        "schema": schema,
        "start": effective_start,
        "end": effective_end,
        "symbols": list(symbols) if symbols else ["ALL_SYMBOLS"],
        "stype_in": "parent" if symbols else "raw_symbol",
    }
    kwargs = dict(request)
    kwargs["path"] = raw_path
    try:
        try:
            store = _retry(
                getattr(client, "timeseries").get_range,
                kwargs=kwargs,
                operation=f"{schema} {day} download",
            )
        except OpraSyncError as exc:
            message = str(exc)
            if (
                symbols
                and "symbology_invalid_request" in message
                and "Could not resolve smart symbols" in message
            ):
                raise OpraNoDataError(
                    "provider returned no resolvable parent-symbol data"
                ) from None
            raise
        if not raw_path.is_file() or raw_path.stat().st_size == 0:
            raise OpraSyncError("Databento returned no provider-native DBN file")
        if store is None:
            import databento as db

            store = db.DBNStore.from_file(raw_path)
        try:
            store.to_parquet(parquet_path, map_symbols=True)
        except Exception as exc:
            raise OpraSyncError("Databento DBN normalization failed") from exc
        finally:
            # Databento's DBN reader retains an open file handle on Windows.
            # Release it before inspecting the native file or atomically
            # publishing the completed directory.
            store = None
            gc.collect()
        if not parquet_path.is_file():
            _classify_missing_parquet(raw_path, request=request)
        provider_duplicates_removed = _deduplicate_normalized_parquet(
            parquet_path,
            schema=schema,
        )
        validation = validate_parquet(parquet_path, schema=schema)
        start_ts = pd.to_datetime(effective_start, utc=True)
        end_ts = pd.to_datetime(effective_end, utc=True)
        earliest = pd.to_datetime(
            validation["earliest_partition_timestamp"], utc=True, errors="coerce"
        )
        latest = pd.to_datetime(
            validation["latest_partition_timestamp"], utc=True, errors="coerce"
        )
        if int(validation["row_count"]) < 1:
            raise OpraSyncError("Databento normalized Parquet is empty")
        if pd.isna(earliest) or pd.isna(latest) or earliest < start_ts or latest >= end_ts:
            raise OpraSyncError(
                "Databento partition-clock timestamps escape the requested interval"
            )
        if int(validation["duplicate_natural_key_rows"]) != 0:
            raise OpraSyncError("Databento partition contains duplicate natural keys")
        raw_inventory = {
            "path": raw_path.name,
            "size_bytes": raw_path.stat().st_size,
            "checksum_sha256": file_checksum(raw_path),
        }
        normalized_inventory = {
            "path": parquet_path.name,
            "size_bytes": parquet_path.stat().st_size,
            "checksum_sha256": file_checksum(parquet_path),
            "provider_duplicate_rows_removed": provider_duplicates_removed,
            **validation,
        }
        manifest = {
            "schema_version": PARTITION_VERSION,
            "sync_version": SYNC_VERSION,
            "provider": PROVIDER,
            "dataset": DATASET,
            "schema": schema,
            "level": "L0" if schema in L0_SCHEMAS else "L1",
            "partition_date": day,
            "partition_start": start_ts.isoformat(),
            "partition_end": end_ts.isoformat(),
            "time_segment": segment,
            "symbol_scope": symbol_scope_name(symbols),
            "request": request,
            "provider_entitlement": dict(entitlement["entitlements"][schema]),
            "published_at": utc_timestamp().isoformat(),
            "raw": raw_inventory,
            "normalized": normalized_inventory,
        }
        manifest_path = staging / "manifest.json"
        _write_json_exclusive(manifest_path, manifest)
        receipt = {
            "schema_version": RECEIPT_VERSION,
            "provider": PROVIDER,
            "dataset": DATASET,
            "schema": schema,
            "partition_date": day,
            "partition_start": manifest["partition_start"],
            "partition_end": manifest["partition_end"],
            "time_segment": segment,
            "published_at": manifest["published_at"],
            "manifest_checksum_sha256": file_checksum(manifest_path),
            "raw_checksum_sha256": raw_inventory["checksum_sha256"],
            "normalized_checksum_sha256": normalized_inventory["checksum_sha256"],
            "provider_duplicate_rows_removed": provider_duplicates_removed,
        }
        _write_json_exclusive(staging / "receipt.json", receipt)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise OpraSyncError("An OPRA partition appeared during atomic publication")
        staging.replace(destination)
        return verify_partition(destination, datastore_root=datastore_root)["manifest"]
    except Exception:
        # Preserve failed staging in place for diagnosis. Moving it while the
        # SDK still owns a Windows file handle can mask the original exception.
        raise


def _classify_missing_parquet(
    raw_path: Path,
    *,
    request: Mapping[str, object],
) -> None:
    """Raise NO_DATA only for a request-matching DBN that cleanly has no records."""

    reopened = None
    records = None
    try:
        with warnings.catch_warnings(record=True) as parser_warnings:
            warnings.simplefilter("always")
            reopened = _load_dbn_store(raw_path)
            _validate_dbn_request_metadata(reopened, request=request)
            records = iter(reopened)
            sentinel = object()
            first_record = next(records, sentinel)
        if parser_warnings:
            raise OpraSyncError(
                "Databento DBN parsing emitted warnings after normalization "
                "produced no Parquet"
            )
        if first_record is not sentinel:
            raise OpraSyncError(
                "Databento DBN contains records but normalization produced no Parquet"
            )
    except OpraSyncError:
        raise
    except Exception as exc:
        raise OpraSyncError(
            "Databento DBN is unreadable after normalization produced no Parquet"
        ) from exc
    finally:
        records = None
        reopened = None
        gc.collect()
    raise OpraNoDataError("provider returned a readable zero-record DBN")


def _load_dbn_store(path: Path) -> object:
    import databento as db

    return db.DBNStore.from_file(path)


def _validate_dbn_request_metadata(
    store: object,
    *,
    request: Mapping[str, object],
) -> None:
    """Fail closed unless native DBN metadata identifies the exact request."""

    metadata = getattr(store, "metadata")
    expected_start = pd.to_datetime(request["start"], utc=True, errors="raise")
    expected_end = pd.to_datetime(request["end"], utc=True, errors="raise")
    actual_start = pd.to_datetime(getattr(store, "start"), utc=True, errors="raise")
    actual_end = pd.to_datetime(getattr(store, "end"), utc=True, errors="raise")
    expected_symbols = tuple(str(value) for value in request["symbols"])
    actual_symbols = tuple(str(value) for value in getattr(store, "symbols"))
    matches_request = (
        str(getattr(store, "dataset")) == str(request["dataset"])
        and str(getattr(store, "schema")) == str(request["schema"])
        and actual_start == expected_start
        and actual_end == expected_end
        and actual_symbols == expected_symbols
        and str(getattr(store, "stype_in")) == str(request["stype_in"])
        and str(getattr(store, "stype_out")) == "instrument_id"
        and getattr(store, "limit") is None
    )
    version = getattr(metadata, "version", None)
    partial = getattr(metadata, "partial", None)
    not_found = getattr(metadata, "not_found", None)
    structurally_complete = (
        isinstance(version, int)
        and version >= 1
        and isinstance(partial, (list, tuple))
        and not partial
        and isinstance(not_found, (list, tuple))
        and not not_found
        and getattr(metadata, "ts_out", None) is False
    )
    if not matches_request or not structurally_complete:
        raise OpraSyncError(
            "Databento DBN request metadata does not match the normalization request"
        )


def _partition_plan(
    client: object,
    *,
    entitlement: Mapping[str, object],
    scope: SyncScope,
) -> list[tuple[str, str]]:
    metadata = getattr(client, "metadata")
    output: list[tuple[str, str]] = []
    for schema, start, end in _scope_ranges(entitlement, scope):
        conditions = _retry(
            metadata.get_dataset_condition,
            kwargs={"dataset": DATASET, "start_date": start, "end_date": end},
            operation=f"{schema} dataset conditions",
        )
        for condition in conditions:
            condition_date = str(condition.get("date"))
            if (
                str(condition.get("condition")) == "available"
                and start <= condition_date < end
            ):
                output.append((schema, condition_date))
    return output


def _partition_time_segments(
    client: object,
    *,
    schema: str,
    day: str,
    symbols: Sequence[str],
) -> list[tuple[str, str, str | None]]:
    """Split exceptionally dense daily schemas into deterministic UTC intervals."""

    start = pd.Timestamp(day, tz="UTC")
    end = start + pd.Timedelta(days=1)
    if schema not in HIGH_VOLUME_SCHEMAS:
        return [(start.isoformat(), end.isoformat(), None)]

    metadata = getattr(client, "metadata")
    output: list[tuple[str, str, str | None]] = []

    def visit(interval_start: pd.Timestamp, interval_end: pd.Timestamp, *, split: bool) -> None:
        record_count = int(
            _retry(
                getattr(metadata, "get_record_count"),
                kwargs=_metadata_request_kwargs(
                    schema=schema,
                    start=interval_start.isoformat(),
                    end=interval_end.isoformat(),
                    symbols=symbols,
                ),
                operation=f"{schema} time-partition record count",
            )
        )
        if record_count == 0:
            return
        duration_seconds = (interval_end - interval_start).total_seconds()
        if record_count <= TARGET_HIGH_VOLUME_PARTITION_ROWS or (
            duration_seconds <= 60 and record_count <= MAX_EXACT_VALIDATION_ROWS
        ):
            segment = None
            if split:
                segment = _time_segment_token(interval_start, interval_end)
            output.append(
                (interval_start.isoformat(), interval_end.isoformat(), segment)
            )
            return

        if duration_seconds <= MINIMUM_TIME_PARTITION_SECONDS:
            raise OpraSyncError(
                f"{schema} has {record_count:,} records in the minimum "
                f"{MINIMUM_TIME_PARTITION_SECONDS}-second interval "
                f"{interval_start.isoformat()} to {interval_end.isoformat()}"
            )
        midpoint = (interval_start + (interval_end - interval_start) / 2).floor("s")
        if midpoint <= interval_start or midpoint >= interval_end:
            raise OpraSyncError(
                f"Unable to split dense {schema} interval "
                f"{interval_start.isoformat()} to {interval_end.isoformat()}"
            )
        visit(interval_start, midpoint, split=True)
        visit(midpoint, interval_end, split=True)

    visit(start, end, split=False)
    return output


def _time_segment_token(start: pd.Timestamp, end: pd.Timestamp) -> str:
    def token(value: pd.Timestamp) -> str:
        text = value.strftime("%H%M%S%f")
        return text[:6] if text.endswith("000000") else text.rstrip("0")

    return f"{token(start)}-{token(end)}"


def _scope_ranges(
    entitlement: Mapping[str, object], scope: SyncScope
) -> list[tuple[str, str, str]]:
    entitlements = entitlement.get("entitlements")
    if not isinstance(entitlements, Mapping):
        raise OpraSyncError("Entitlement receipt has no schema bounds")
    schemas = tuple(dict.fromkeys(str(value) for value in scope.schemas))
    invalid = sorted(set(schemas).difference(STANDARD_SCHEMAS))
    if invalid:
        raise ValueError("Unsupported OPRA schemas: " + ", ".join(invalid))
    output: list[tuple[str, str, str]] = []
    for schema in schemas:
        item = entitlements[schema]
        if not isinstance(item, Mapping):
            raise OpraSyncError(f"Entitlement receipt has malformed bounds for {schema}")
        entitled_start = date.fromisoformat(str(item["entitled_start"]))
        entitled_end = date.fromisoformat(str(item["entitled_end"]))
        requested_start = (
            date.fromisoformat(scope.start) if scope.start else entitled_start
        )
        requested_end = date.fromisoformat(scope.end) if scope.end else entitled_end
        if requested_start < entitled_start or requested_end > entitled_end:
            raise OpraSyncError(
                f"Requested OPRA {schema} scope is outside the configured included "
                f"Standard-plan window: requested={requested_start.isoformat()}.."
                f"{requested_end.isoformat()} included={entitled_start.isoformat()}.."
                f"{entitled_end.isoformat()} authority={STANDARD_PLAN_AUTHORITY}"
            )
        if requested_start >= requested_end:
            raise OpraSyncError(
                f"Requested OPRA {schema} scope is empty or reversed: "
                f"{requested_start.isoformat()}..{requested_end.isoformat()}"
            )
        output.append(
            (schema, requested_start.isoformat(), requested_end.isoformat())
        )
    if not output:
        raise OpraSyncError("Requested OPRA scope does not overlap the entitlement")
    return output


def standard_plan_history_policy(schema: str) -> dict[str, object]:
    """Return the conservative included OPRA window from the plan authority."""

    if schema in L0_SCHEMAS:
        return {"unit": "years", "value": 13}
    if schema in L1_SCHEMAS:
        return {"unit": "months", "value": 12}
    raise ValueError(f"Unsupported OPRA schema: {schema}")


def _history_window_start(end: str, policy: Mapping[str, object]) -> str:
    anchor = pd.Timestamp(end)
    amount = int(policy["value"])
    unit = str(policy["unit"])
    if unit == "years":
        start = anchor - pd.DateOffset(years=amount)
    elif unit == "months":
        start = anchor - pd.DateOffset(months=amount)
    else:
        raise ValueError(f"Unsupported OPRA included-history policy: {policy}")
    return start.date().isoformat()


def _metadata_request_kwargs(
    *, schema: str, start: str, end: str, symbols: Sequence[str]
) -> dict[str, object]:
    return {
        "dataset": DATASET,
        "schema": schema,
        "start": start,
        "end": end,
        "symbols": list(symbols) if symbols else "ALL_SYMBOLS",
        "stype_in": "parent" if symbols else "raw_symbol",
    }


def _publish_cursor(datastore_root: Path, *, schema: str) -> None:
    manifests = list(
        canonical_root(datastore_root).glob(
            f"{clean_token(schema)}/*/dates/*/segments/*/manifest.json"
        )
    )
    completed_dates = sorted({path.parents[2].name for path in manifests})
    payload = {
        "schema_version": "databento-opra-cursor-v1",
        "provider": PROVIDER,
        "dataset": DATASET,
        "schema": schema,
        "updated_at": utc_timestamp().isoformat(),
        "completed_partition_dates": completed_dates,
        "latest_completed_partition_date": completed_dates[-1] if completed_dates else None,
    }
    _write_json_atomic(canonical_root(datastore_root) / "state" / f"{schema}.json", payload)


def _duplicate_rows(path: Path, *, columns: Sequence[str], row_count: int) -> int:
    if not columns or row_count == 0:
        return 0
    # Exact validation is deliberately bounded to partitions small enough for a
    # practical normalized publication. Larger partitions must be split before
    # publication instead of receiving an approximate duplicate report.
    if row_count > MAX_EXACT_VALIDATION_ROWS:
        raise OpraSyncError(
            f"Normalized partition exceeds the {MAX_EXACT_VALIDATION_ROWS:,}-row "
            "exact-validation bound; "
            "reduce the date/symbol partition"
        )
    keys = pd.read_parquet(path, columns=list(columns))
    if keys.index.name in columns:
        keys = keys.reset_index()
    return int(keys.duplicated(list(columns), keep=False).sum())


def _deduplicate_normalized_parquet(path: Path, *, schema: str) -> int:
    """Remove exact provider duplicates from canonical CMBP while retaining raw DBN."""

    if schema != "cmbp-1":
        return 0
    frame = pd.read_parquet(path)
    index_name = frame.index.name
    if index_name:
        frame = frame.reset_index()
    key = tuple(name for name in _natural_key(schema) if name in frame.columns)
    if not key:
        raise OpraSyncError("CMBP normalized Parquet has no deduplication key")
    original_rows = len(frame)
    frame = frame.drop_duplicates(list(key), keep="first")
    removed = original_rows - len(frame)
    if removed:
        if index_name and index_name in frame.columns:
            frame = frame.set_index(index_name)
        temporary = _next_pending_file(path)
        frame.to_parquet(temporary, compression="zstd")
        temporary.replace(path)
    return removed


def _natural_key(schema: str) -> tuple[str, ...]:
    if schema.startswith("ohlcv-"):
        return ("ts_event", "publisher_id", "instrument_id", "symbol")
    if schema == "definition":
        return ("ts_recv", "publisher_id", "instrument_id", "symbol")
    if schema == "cmbp-1":
        # The Python SDK's normalized CMBP Parquet does not expose the DBN
        # sequence field. Preserve distinct book updates using every normalized
        # message field and remove only byte-for-byte-equivalent market records.
        return (
            "ts_recv",
            "publisher_id",
            "instrument_id",
            "symbol",
            "ts_event",
            "rtype",
            "action",
            "side",
            "price",
            "size",
            "flags",
            "ts_in_delta",
            "bid_px_00",
            "ask_px_00",
            "bid_sz_00",
            "ask_sz_00",
            "bid_pb_00",
            "ask_pb_00",
        )
    if schema in {"trades", "tcbbo"}:
        return ("ts_recv", "publisher_id", "instrument_id", "sequence", "symbol")
    if schema.startswith("cbbo-"):
        return ("ts_recv", "publisher_id", "instrument_id", "symbol")
    if schema == "statistics":
        return ("ts_recv", "publisher_id", "instrument_id", "stat_type", "sequence", "symbol")
    return ("ts_recv", "publisher_id", "instrument_id", "symbol")


def _event_column(columns: Sequence[str]) -> str | None:
    return next((name for name in ("ts_event", "ts_recv") if name in columns), None)


def _retry(
    function: Callable[..., object],
    *,
    kwargs: Mapping[str, object],
    operation: str,
    maximum_attempts: int = DOWNLOAD_MAX_ATTEMPTS,
) -> object:
    last_error: Exception | None = None
    for attempt in range(1, maximum_attempts + 1):
        try:
            return function(**kwargs)
        except Exception as exc:
            last_error = exc
            if attempt < maximum_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
    assert last_error is not None
    raise OpraSyncError(
        f"{operation} failed after {maximum_attempts} attempts: "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def _date_text(value: object) -> str:
    return pd.to_datetime(value, utc=True).date().isoformat()


def _semantic_checksum(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _storage_preflight_path(
    datastore_root: Path,
    payload: Mapping[str, object],
) -> Path:
    scope = payload.get("scope")
    if not isinstance(scope, Mapping):
        raise OpraSyncError("OPRA storage preflight lacks readable scope metadata")
    schemas = tuple(str(value) for value in scope.get("schemas", ()))
    schema_name = (
        "all-standard-schemas"
        if set(schemas) == set(STANDARD_SCHEMAS)
        else "_and_".join(clean_token(value) for value in schemas)
    ) or "no-schemas"
    raw_symbols = tuple(str(value) for value in scope.get("symbols", ()))
    symbols = () if raw_symbols == ("ALL_SYMBOLS",) else raw_symbols
    start = scope.get("start")
    end = scope.get("end")
    if not start or not end:
        raise OpraSyncError("OPRA storage preflight lacks readable date bounds")
    return (
        canonical_root(datastore_root)
        / "metadata"
        / "preflights"
        / schema_name
        / symbol_scope_name(symbols)
        / window_name(str(start), str(end))
        / "preflight.json"
    )


def _next_attempt_directory(base: Path) -> Path:
    for attempt in range(1, 10_000):
        candidate = base / f"attempt-{attempt:03d}"
        if not candidate.exists():
            return candidate
    raise OpraSyncError(f"Too many retained OPRA staging attempts beneath {base}")


def _write_json_exclusive(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(dict(payload), handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _next_pending_file(path)
    _write_json_exclusive(temporary, payload)
    temporary.replace(path)


def _next_pending_file(path: Path) -> Path:
    for attempt in range(1, 10_000):
        candidate = path.with_name(f"{path.name}.pending-{attempt:03d}")
        if not candidate.exists():
            return candidate
    raise OpraSyncError(f"Too many retained pending files beside {path}")


def _minimum_timestamp_text(left: object, right: object) -> str | None:
    values = [pd.to_datetime(value, utc=True, errors="coerce") for value in (left, right)]
    valid = [value for value in values if not pd.isna(value)]
    return min(valid).isoformat() if valid else None


def _maximum_timestamp_text(left: object, right: object) -> str | None:
    values = [pd.to_datetime(value, utc=True, errors="coerce") for value in (left, right)]
    valid = [value for value in values if not pd.isna(value)]
    return max(valid).isoformat() if valid else None


__all__ = [
    "DATASET",
    "DOWNLOAD_MAX_ATTEMPTS",
    "ENTITLEMENT_VERSION",
    "L0_SCHEMAS",
    "L1_SCHEMAS",
    "OpraCapacityError",
    "OpraSyncError",
    "PROVIDER",
    "STANDARD_SCHEMAS",
    "SyncResult",
    "SyncScope",
    "canonical_root",
    "configure_client",
    "discover_standard_entitlement",
    "iter_verified_partitions",
    "partition_directory",
    "publish_health",
    "publish_storage_preflight",
    "standard_plan_history_policy",
    "record_consumer_usage",
    "storage_preflight",
    "symbol_bucket",
    "synchronize",
    "validate_parquet",
    "verify_partition",
]
