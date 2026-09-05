from __future__ import annotations

import argparse
import hashlib
import json
import os
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pandas as pd
import pyarrow.parquet as pq

from datafetching.bar_consolidation import consolidate_shadowed_derived_bars
from datafetching.databento_opra_history import OPRA_STRATEGY_HISTORY_SCHEMAS
from datafetching.equity_dataset_migration import DEFAULT_SYMBOLS, NATIVE_REQUESTS
from datafetching.orchestrate import DEFAULT_WATCHLIST, normalize_symbols, read_watchlist
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock, interprocess_file_lock


CATALOG_VERSION = "ducketz-market-data-catalog-v1"
CLEANUP_PLAN_VERSION = "ducketz-datastore-cleanup-plan-v1"
CLEANUP_RECEIPT_VERSION = "ducketz-datastore-cleanup-receipt-v1"
PRODUCTION_OPRA_HISTORY_SCHEMAS = frozenset(OPRA_STRATEGY_HISTORY_SCHEMAS)
STAGING_RELATIVE_ROOTS = (
    Path("market-data/databento/.staging"),
    Path("market-data/databento/opra/OPRA.PILLAR/.staging"),
)


class DatastoreHygieneError(RuntimeError):
    """The requested audit or cleanup could not be proven safe."""


def audit_datastore(
    datastore_root: Path,
    *,
    symbols: Iterable[str] = DEFAULT_SYMBOLS,
    observed_at: datetime | None = None,
    write: bool = True,
) -> dict[str, object]:
    root = Path(datastore_root).resolve()
    clean_symbols = normalize_symbols(symbols)
    now = observed_at or datetime.now(timezone.utc)
    operational = _operational_bar_inventory(root, clean_symbols)
    cold_equities = _cold_equity_inventory(root, clean_symbols)
    opra = _opra_inventory(root, now=now)
    snapshots = _option_snapshot_inventory(root, clean_symbols)
    cleanup = _cleanup_inventory(root, now=now)
    comparisons = _daily_equity_comparisons(
        root,
        clean_symbols,
        operational=operational,
        cold_equities=cold_equities,
    )
    payload: dict[str, object] = {
        "schema_version": CATALOG_VERSION,
        "observed_at": now.astimezone(timezone.utc).isoformat(),
        "datastore_root": str(root),
        "symbols": list(clean_symbols),
        "authorities": {
            "loop_a_equity_ohlcv": {
                "provider": "databento",
                "dataset": "EQUS.MINI",
                "root": str(root / "stocks" / "<SYMBOL>" / "bars"),
                "policy": (
                    "Native bars are canonical; 1m-derived 1h/1d rows are retained "
                    "only while the matching native timestamp is absent."
                ),
            },
            "schwab_equity_history": {
                "provider": "schwab",
                "dataset": "SCHWAB_PRICE_HISTORY",
                "role": "secondary historical/research series; not recurring Loop A OHLCV",
            },
            "xnas_equity_archive": {
                "provider": "databento",
                "dataset": "XNAS.ITCH",
                "role": "venue-specific immutable cold archive; not Loop A input",
            },
            "opra_history": {
                "provider": "databento-opra",
                "dataset": "OPRA.PILLAR",
                "production_maintenance_schemas": sorted(
                    PRODUCTION_OPRA_HISTORY_SCHEMAS
                ),
                "other_schema_policy": (
                    "retained research history; no freshness promise until an explicit "
                    "research retention/fetch decision"
                ),
            },
        },
        "operational_equity_bars": operational,
        "cold_xnas_equity_ohlcv": cold_equities,
        "daily_equity_overlap_comparisons": comparisons,
        "opra_history": opra,
        "prospective_option_snapshots": snapshots,
        "cleanup_candidates": cleanup,
        "conclusions": [
            (
                "XNAS.ITCH, EQUS.MINI, and Schwab bars must not be timestamp-merged "
                "as if they were one homogeneous feed."
            ),
            (
                "Databento native and normalized files are distinct provider evidence "
                "encodings; OPRA date partitions are the atomic verification boundary."
            ),
            (
                "Migration rollback copies and abandoned unverified staging attempts "
                "are the safe reclaimable classes."
            ),
        ],
    }
    if write:
        destination = root / "catalog" / "market-data"
        destination.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(destination / "current.json", payload)
        _write_text_atomic(destination / "current.md", _catalog_markdown(payload))
    return payload


def clean_datastore(
    datastore_root: Path,
    *,
    symbols: Iterable[str] = DEFAULT_SYMBOLS,
    clean_staging: bool = False,
    retire_migration_backups: bool = False,
    consolidate_derived: bool = False,
    confirm: bool = False,
    minimum_age_days: int = 7,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    root = _validated_cleanup_root(datastore_root)
    clean_symbols = normalize_symbols(symbols)
    now = observed_at or datetime.now(timezone.utc)
    if minimum_age_days < 1:
        raise DatastoreHygieneError("Cleanup minimum age must be at least one day")
    if not any((clean_staging, retire_migration_backups, consolidate_derived)):
        raise DatastoreHygieneError("At least one cleanup class must be selected")

    selected_roots: list[tuple[str, Path]] = []
    if clean_staging:
        for relative in STAGING_RELATIVE_ROOTS:
            candidate = (root / relative).resolve()
            if candidate.is_dir():
                _validate_staging_candidate(candidate, now, minimum_age_days)
                selected_roots.append(("abandoned_staging", candidate))
    if retire_migration_backups:
        _validate_operational_equity_dataset(root, clean_symbols)
        for candidate in sorted(
            (root / "stocks").glob("*/bars/*/databento/migration-backups")
        ):
            resolved = candidate.resolve()
            _validate_aged_tree(resolved, now, minimum_age_days)
            selected_roots.append(("migration_backup", resolved))

    planned_files: list[dict[str, object]] = []
    for category, candidate_root in selected_roots:
        for path in _files_beneath(candidate_root):
            stat = path.stat()
            planned_files.append(
                {
                    "category": category,
                    "path": str(path),
                    "relative_to_datastore": path.relative_to(root).as_posix(),
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(
                        stat.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    "sha256": _sha256(path),
                }
            )
    planned_files.sort(key=lambda item: str(item["relative_to_datastore"]))
    if not confirm:
        return {
            "schema_version": CLEANUP_PLAN_VERSION,
            "status": "DRY_RUN",
            "datastore_root": str(root),
            "selected_cleanup_classes": _selected_cleanup_classes(
                clean_staging=clean_staging,
                retire_migration_backups=retire_migration_backups,
                consolidate_derived=consolidate_derived,
            ),
            "file_count": len(planned_files),
            "bytes": sum(int(item["size_bytes"]) for item in planned_files),
            "files": planned_files,
        }

    cleanup_id = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    cleanup_root = root / "catalog" / "cleanups" / cleanup_id
    cleanup_root.mkdir(parents=True, exist_ok=False)
    plan: dict[str, object] = {
        "schema_version": CLEANUP_PLAN_VERSION,
        "cleanup_id": cleanup_id,
        "created_at": now.astimezone(timezone.utc).isoformat(),
        "datastore_root": str(root),
        "selected_cleanup_classes": _selected_cleanup_classes(
            clean_staging=clean_staging,
            retire_migration_backups=retire_migration_backups,
            consolidate_derived=consolidate_derived,
        ),
        "minimum_age_days": minimum_age_days,
        "file_count": len(planned_files),
        "bytes": sum(int(item["size_bytes"]) for item in planned_files),
        "files": planned_files,
        "recoverability": (
            "Deleted bytes are not retained; this plan preserves exact paths, sizes, "
            "modification clocks, and SHA-256 identities only."
        ),
    }
    plan_path = cleanup_root / "plan.json"
    _write_json_atomic(plan_path, plan)
    deleted_files: list[str] = []
    consolidation_rows: list[dict[str, object]] = []

    with ExitStack() as locks:
        locks.enter_context(
            exclusive_runtime_lock(
                root / ".ducketz-datastore-hygiene.lock",
                process_name="Duckets datastore hygiene",
            )
        )
        if clean_staging:
            locks.enter_context(
                exclusive_runtime_lock(
                    root / ".ducketz-databento-cold-start.lock",
                    process_name="Duckets Databento cold-start coordinator",
                )
            )
            locks.enter_context(
                exclusive_runtime_lock(
                    root
                    / "market-data/databento/opra/OPRA.PILLAR/state/sync.lock",
                    process_name="Options-owned OPRA symbol history synchronizer",
                )
            )
        if consolidate_derived:
            locks.enter_context(
                interprocess_file_lock(root / ".ducketz-loop-a-cycle.lock")
            )

        for item in planned_files:
            path = Path(str(item["path"])).resolve()
            _require_under_any(path, tuple(candidate for _category, candidate in selected_roots))
            if not path.is_file():
                raise DatastoreHygieneError(
                    f"Cleanup target changed after planning; missing file: {path}"
                )
            if path.stat().st_size != int(item["size_bytes"]) or _sha256(path) != str(
                item["sha256"]
            ):
                raise DatastoreHygieneError(
                    f"Cleanup target changed after planning; preserving it: {path}"
                )
            path.unlink()
            deleted_files.append(str(path))

        for _category, selected_root in selected_roots:
            _remove_empty_directories(selected_root)

        if consolidate_derived:
            for symbol in clean_symbols:
                for result in consolidate_shadowed_derived_bars(
                    root,
                    symbol=symbol,
                ):
                    consolidation_rows.append(
                        {
                            "symbol": result.symbol,
                            "timeframe": result.timeframe,
                            "native_path": str(result.native_path),
                            "derived_path": str(result.derived_path),
                            "native_rows": result.native_rows,
                            "derived_rows_before": result.derived_rows_before,
                            "shadowed_rows_removed": result.shadowed_rows_removed,
                            "derived_rows_retained": result.derived_rows_retained,
                            "bytes_before": result.bytes_before,
                            "bytes_after": result.bytes_after,
                        }
                    )

    completed_at = datetime.now(timezone.utc)
    receipt: dict[str, object] = {
        "schema_version": CLEANUP_RECEIPT_VERSION,
        "status": "COMPLETE",
        "cleanup_id": cleanup_id,
        "completed_at": completed_at.isoformat(),
        "datastore_root": str(root),
        "plan_path": str(plan_path),
        "plan_checksum_sha256": _sha256(plan_path),
        "deleted_file_count": len(deleted_files),
        "deleted_bytes": sum(int(item["size_bytes"]) for item in planned_files),
        "deleted_files": deleted_files,
        "derived_consolidations": consolidation_rows,
        "shadowed_derived_rows_removed": sum(
            int(item["shadowed_rows_removed"]) for item in consolidation_rows
        ),
        "derived_bytes_reclaimed": sum(
            int(item["bytes_before"]) - int(item["bytes_after"])
            for item in consolidation_rows
        ),
        "deleted_bytes_recoverable": False,
    }
    receipt_path = cleanup_root / "receipt.json"
    _write_json_atomic(receipt_path, receipt)
    pointer = {
        "schema_version": "ducketz-datastore-cleanup-pointer-v1",
        "cleanup_id": cleanup_id,
        "receipt_path": str(receipt_path),
        "receipt_checksum_sha256": _sha256(receipt_path),
        "completed_at": completed_at.isoformat(),
    }
    _write_json_atomic(root / "catalog" / "cleanups" / "current.json", pointer)
    audit_datastore(root, symbols=clean_symbols, write=True)
    return {**receipt, "receipt_path": str(receipt_path)}


def _operational_bar_inventory(
    root: Path,
    symbols: Sequence[str],
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        bars_root = root / "stocks" / symbol / "bars"
        if not bars_root.is_dir():
            continue
        for timeframe_root in sorted(path for path in bars_root.iterdir() if path.is_dir()):
            for provider in ("databento", "schwab"):
                provider_root = timeframe_root / provider
                normalized_root = provider_root / "normalized"
                if not normalized_root.is_dir():
                    continue
                raw_paths = sorted((provider_root / "raw").glob("*.parquet"))
                raw_bytes = sum(path.stat().st_size for path in raw_paths)
                for path in sorted(normalized_root.glob("*.parquet")):
                    summary = _parquet_summary(path, timestamp_candidates=("timestamp",))
                    role = (
                        "derived_latency_bridge"
                        if "_derived_" in path.name
                        else "native_loop_a_authority"
                        if provider == "databento"
                        else "secondary_schwab_history"
                    )
                    rows.append(
                        {
                            "symbol": symbol,
                            "timeframe": timeframe_root.name.lower(),
                            "provider": provider,
                            "dataset": (
                                "EQUS.MINI"
                                if provider == "databento"
                                else "SCHWAB_PRICE_HISTORY"
                            ),
                            "role": role,
                            "path": str(path),
                            **summary,
                            "provider_raw_file_count": len(raw_paths),
                            "provider_raw_bytes": raw_bytes,
                        }
                    )
    return rows


def _cold_equity_inventory(
    root: Path,
    symbols: Sequence[str],
) -> list[dict[str, object]]:
    archive = root / "market-data/databento/us-equities/XNAS.ITCH"
    rows: list[dict[str, object]] = []
    if not archive.is_dir():
        return rows
    for schema_root in sorted(archive.glob("ohlcv-*")):
        for symbol in symbols:
            for path in sorted((schema_root / symbol / "windows").glob("*/normalized.parquet")):
                rows.append(
                    {
                        "symbol": symbol,
                        "schema": schema_root.name,
                        "provider": "databento",
                        "dataset": "XNAS.ITCH",
                        "role": "venue_specific_cold_archive",
                        "path": str(path),
                        **_parquet_summary(
                            path,
                            timestamp_candidates=("ts_event", "timestamp"),
                        ),
                    }
                )
    return rows


def _daily_equity_comparisons(
    root: Path,
    symbols: Sequence[str],
    *,
    operational: Sequence[Mapping[str, object]],
    cold_equities: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for symbol in symbols:
        operational_databento = _first_path(
            operational,
            symbol=symbol,
            timeframe="1d",
            provider="databento",
            role="native_loop_a_authority",
        )
        schwab = _first_path(
            operational,
            symbol=symbol,
            timeframe="1d",
            provider="schwab",
            role="secondary_schwab_history",
        )
        xnas = _first_path(
            cold_equities,
            symbol=symbol,
            schema="ohlcv-1d",
        )
        if operational_databento is None:
            continue
        authority = _daily_frame(operational_databento)
        row: dict[str, object] = {"symbol": symbol}
        if xnas is not None:
            row["xnas_vs_equs"] = _compare_ohlcv(
                _daily_frame(xnas),
                authority,
            )
        if schwab is not None:
            row["schwab_vs_equs"] = _compare_ohlcv(
                _daily_frame(schwab),
                authority,
            )
        output.append(row)
    return output


def _opra_inventory(root: Path, *, now: datetime) -> dict[str, object]:
    canonical = root / "market-data/databento/opra/OPRA.PILLAR"
    health_path = canonical / "health/current.json"
    health = _read_json(health_path)
    health_schemas = health.get("schemas", {}) if isinstance(health, Mapping) else {}
    schema_names = sorted(
        set(str(value) for value in health_schemas)
        | {
            path.stem
            for path in (canonical / "state").glob("*.json")
            if path.name != "consumer-usage.json"
        }
    )
    rows: list[dict[str, object]] = []
    today = pd.Timestamp(now).tz_convert("UTC").normalize()
    for schema in schema_names:
        cursor = _read_json(canonical / "state" / f"{schema}.json")
        latest = cursor.get("latest_completed_partition_date")
        parsed = pd.to_datetime(latest, utc=True, errors="coerce")
        health_row = (
            health_schemas.get(schema, {})
            if isinstance(health_schemas, Mapping)
            else {}
        )
        rows.append(
            {
                "schema": schema,
                "maintenance_policy": (
                    "loop_a_production_strategy_history"
                    if schema in PRODUCTION_OPRA_HISTORY_SCHEMAS
                    else "retained_research_history_not_freshness_managed"
                ),
                "latest_completed_partition_date": latest,
                "calendar_days_behind_observation": (
                    int((today - pd.Timestamp(parsed).normalize()).days)
                    if not pd.isna(parsed)
                    else None
                ),
                "partition_count": int(health_row.get("partition_count", 0) or 0),
                "row_count": int(health_row.get("row_count", 0) or 0),
                "raw_bytes": int(health_row.get("raw_bytes", 0) or 0),
                "parquet_bytes": int(health_row.get("parquet_bytes", 0) or 0),
                "consumer_read_count": int(
                    health_row.get("consumer_read_count", 0) or 0
                ),
            }
        )
    cursors: dict[str, dict[str, object]] = {}
    for path in sorted((canonical / "state/symbol-history").glob("*/*.json")):
        payload = _read_json(path)
        schema = str(payload.get("schema", path.stem))
        completed = str(payload.get("completed_through", ""))
        item = cursors.setdefault(
            schema,
            {"cursor_count": 0, "earliest_completed_through": None, "latest_completed_through": None},
        )
        item["cursor_count"] = int(item["cursor_count"]) + 1
        item["earliest_completed_through"] = _minimum_text_date(
            item["earliest_completed_through"], completed
        )
        item["latest_completed_through"] = _maximum_text_date(
            item["latest_completed_through"], completed
        )
    return {
        "canonical_root": str(canonical),
        "health_path": str(health_path),
        "health_observed_at": health.get("observed_at"),
        "total_partitions": int(health.get("total_partitions", 0) or 0),
        "total_rows": int(health.get("total_rows", 0) or 0),
        "total_raw_bytes": int(health.get("total_raw_bytes", 0) or 0),
        "total_parquet_bytes": int(health.get("total_parquet_bytes", 0) or 0),
        "schemas": rows,
        "symbol_history_cursors": cursors,
    }


def _option_snapshot_inventory(
    root: Path,
    symbols: Sequence[str],
) -> dict[str, object]:
    providers: dict[str, dict[str, object]] = {}
    for symbol in symbols:
        snapshots = root / "stocks" / symbol / "options/snapshots"
        for receipt_path in sorted(snapshots.glob("*/*/receipt.json")):
            receipt = _read_json(receipt_path)
            provider = str(receipt.get("provider", receipt_path.parents[1].name))
            target = str(receipt.get("target_snapshot_for", ""))
            item = providers.setdefault(
                provider,
                {"receipt_count": 0, "earliest_target": None, "latest_target": None},
            )
            item["receipt_count"] = int(item["receipt_count"]) + 1
            item["earliest_target"] = _minimum_text_date(item["earliest_target"], target)
            item["latest_target"] = _maximum_text_date(item["latest_target"], target)
    return {
        "root_pattern": str(root / "stocks/<SYMBOL>/options/snapshots/<PROVIDER>/<TARGET>"),
        "providers": providers,
        "meaning": (
            "Prospective option-chain evidence is separate from the historical OPRA "
            "partition archive and can continue through a labeled Schwab fallback."
        ),
    }


def _cleanup_inventory(root: Path, *, now: datetime) -> dict[str, object]:
    categories: dict[str, dict[str, object]] = {}
    staging_files: list[Path] = []
    for relative in STAGING_RELATIVE_ROOTS:
        candidate = root / relative
        if candidate.is_dir():
            staging_files.extend(_files_beneath(candidate))
    migration_files = [
        path
        for candidate in sorted(
            (root / "stocks").glob("*/bars/*/databento/migration-backups")
        )
        for path in _files_beneath(candidate)
    ]
    for name, files in (
        ("abandoned_staging", staging_files),
        ("migration_backups", migration_files),
    ):
        newest = max((path.stat().st_mtime for path in files), default=None)
        categories[name] = {
            "file_count": len(files),
            "bytes": sum(path.stat().st_size for path in files),
            "newest_modified_at": (
                datetime.fromtimestamp(newest, tz=timezone.utc).isoformat()
                if newest is not None
                else None
            ),
            "age_days": (
                (now.astimezone(timezone.utc) - datetime.fromtimestamp(newest, tz=timezone.utc)).total_seconds()
                / 86_400.0
                if newest is not None
                else None
            ),
        }
    return categories


def _catalog_markdown(payload: Mapping[str, object]) -> str:
    operational = payload.get("operational_equity_bars", [])
    comparisons = payload.get("daily_equity_overlap_comparisons", [])
    opra = payload.get("opra_history", {})
    cleanup = payload.get("cleanup_candidates", {})
    lines = [
        "# Duckets market-data catalog",
        "",
        f"Generated: `{payload.get('observed_at')}`",
        "",
        "## Authority map",
        "",
        "| Family | Canonical role | Merge policy |",
        "|---|---|---|",
        "| `stocks/<SYMBOL>/bars/.../databento` | Loop A operational `EQUS.MINI` | Native wins; derived 1h/1d keeps gaps only |",
        "| `stocks/<SYMBOL>/bars/.../schwab` | Secondary historical/research price history | Keep provider-separated |",
        "| `market-data/databento/us-equities/XNAS.ITCH` | Venue-specific cold archive | Keep dataset-separated |",
        "| `market-data/databento/opra/OPRA.PILLAR` | Immutable OPRA history | Keep verified date/segment partitions; raw and normalized are paired evidence |",
        "| `stocks/<SYMBOL>/options/snapshots` | Prospective chain receipts | Separate from historical OPRA |",
        "",
        "## Equity coverage",
        "",
        "| Symbol | Source | Rows | Earliest | Latest |",
        "|---|---:|---:|---|---|",
    ]
    for item in operational if isinstance(operational, list) else []:
        if item.get("timeframe") != "1d" or item.get("role") == "derived_latency_bridge":
            continue
        lines.append(
            "| {symbol} | {provider} / {dataset} | {row_count:,} | {earliest} | {latest} |".format(
                symbol=item.get("symbol"),
                provider=item.get("provider"),
                dataset=item.get("dataset"),
                row_count=int(item.get("row_count", 0)),
                earliest=item.get("earliest_timestamp") or "—",
                latest=item.get("latest_timestamp") or "—",
            )
        )
    lines.extend(
        [
            "",
            "The similarly shaped daily files are not exact duplicates:",
            "",
            "| Symbol | Pair | Overlap | Exact OHLC | Exact OHLCV | Merge-safe |",
            "|---|---|---:|---:|---:|---|",
        ]
    )
    for item in comparisons if isinstance(comparisons, list) else []:
        for label, pair in (
            ("XNAS vs EQUS", item.get("xnas_vs_equs")),
            ("Schwab vs EQUS", item.get("schwab_vs_equs")),
        ):
            if not isinstance(pair, Mapping):
                continue
            lines.append(
                f"| {item.get('symbol')} | {label} | {pair.get('overlap_rows', 0):,} | "
                f"{pair.get('exact_ohlc_rows', 0):,} | {pair.get('exact_ohlcv_rows', 0):,} | "
                f"{'yes' if pair.get('merge_safe') else 'no'} |"
            )
    lines.extend(
        [
            "",
            "## OPRA history",
            "",
            "`ohlcv-1h`, `cbbo-1m`, and `definition` are Loop A-maintained production strategy-history scopes. Other schemas remain labeled research history rather than pretending they are current.",
            "",
            "| Schema | Local latest | Partitions | Raw | Parquet | Reads | Policy |",
            "|---|---|---:|---:|---:|---:|---|",
        ]
    )
    schemas = opra.get("schemas", []) if isinstance(opra, Mapping) else []
    for item in schemas if isinstance(schemas, list) else []:
        lines.append(
            f"| {item.get('schema')} | {item.get('latest_completed_partition_date') or '—'} | "
            f"{int(item.get('partition_count', 0)):,} | {_human_bytes(int(item.get('raw_bytes', 0)))} | "
            f"{_human_bytes(int(item.get('parquet_bytes', 0)))} | "
            f"{int(item.get('consumer_read_count', 0)):,} | {item.get('maintenance_policy')} |"
        )
    lines.extend(
        [
            "",
            "## Safe cleanup classes",
            "",
            "| Class | Files | Size | Newest modification |",
            "|---|---:|---:|---|",
        ]
    )
    if isinstance(cleanup, Mapping):
        for name, item in cleanup.items():
            if not isinstance(item, Mapping):
                continue
            lines.append(
                f"| {name} | {int(item.get('file_count', 0)):,} | "
                f"{_human_bytes(int(item.get('bytes', 0)))} | "
                f"{item.get('newest_modified_at') or '—'} |"
            )
    lines.extend(
        [
            "",
            "Generated from live filesystem metadata by `python -m datafetching.datastore_hygiene`. The JSON beside this file is the machine-readable authority.",
            "",
        ]
    )
    return "\n".join(lines)


def _parquet_summary(
    path: Path,
    *,
    timestamp_candidates: Sequence[str],
) -> dict[str, object]:
    metadata = pq.ParquetFile(path).metadata
    schema = pq.read_schema(path)
    timestamp_column = next(
        (name for name in timestamp_candidates if name in schema.names),
        None,
    )
    earliest = latest = None
    if timestamp_column is not None and metadata.num_rows:
        frame = pd.read_parquet(path, columns=[timestamp_column])
        source = (
            frame[timestamp_column]
            if timestamp_column in frame.columns
            else pd.Series(frame.index, index=frame.index)
        )
        values = pd.to_datetime(source, utc=True, errors="coerce").dropna()
        if not values.empty:
            earliest = pd.Timestamp(values.min()).isoformat()
            latest = pd.Timestamp(values.max()).isoformat()
    return {
        "row_count": metadata.num_rows,
        "size_bytes": path.stat().st_size,
        "timestamp_column": timestamp_column,
        "earliest_timestamp": earliest,
        "latest_timestamp": latest,
        "modified_at": datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc
        ).isoformat(),
    }


def _daily_frame(path: Path) -> pd.DataFrame:
    schema = pq.read_schema(path)
    timestamp = "timestamp" if "timestamp" in schema.names else "ts_event"
    columns = [timestamp, "open", "high", "low", "close", "volume"]
    frame = pd.read_parquet(path, columns=columns)
    if timestamp not in frame.columns and frame.index.name == timestamp:
        frame = frame.reset_index()
    frame = frame.rename(columns={timestamp: "timestamp"})
    frame["date"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce").dt.date
    return (
        frame.dropna(subset=["date"])
        .sort_values("timestamp")
        .drop_duplicates("date", keep="last")
        .set_index("date")
    )


def _compare_ohlcv(left: pd.DataFrame, right: pd.DataFrame) -> dict[str, object]:
    common = left.index.intersection(right.index)
    if common.empty:
        return {
            "overlap_rows": 0,
            "exact_ohlc_rows": 0,
            "exact_ohlcv_rows": 0,
            "merge_safe": False,
        }
    left_values = left.loc[common, ["open", "high", "low", "close", "volume"]]
    right_values = right.loc[common, ["open", "high", "low", "close", "volume"]]
    equal = left_values.eq(right_values) | (
        left_values.isna() & right_values.isna()
    )
    exact_ohlc = equal[["open", "high", "low", "close"]].all(axis=1)
    exact_ohlcv = equal.all(axis=1)
    close_denominator = right_values["close"].abs().replace(0.0, pd.NA)
    close_relative = (
        (left_values["close"] - right_values["close"]).abs() / close_denominator
    )
    volume_denominator = right_values["volume"].replace(0.0, pd.NA)
    volume_ratio = left_values["volume"] / volume_denominator
    return {
        "overlap_rows": len(common),
        "exact_ohlc_rows": int(exact_ohlc.sum()),
        "exact_ohlcv_rows": int(exact_ohlcv.sum()),
        "median_absolute_close_difference_fraction": _finite_float(
            close_relative.median(skipna=True)
        ),
        "median_left_to_right_volume_ratio": _finite_float(
            volume_ratio.median(skipna=True)
        ),
        "merge_safe": bool(exact_ohlcv.all()),
    }


def _first_path(
    rows: Sequence[Mapping[str, object]],
    **expected: str,
) -> Path | None:
    for item in rows:
        if all(str(item.get(key)) == value for key, value in expected.items()):
            return Path(str(item["path"]))
    return None


def _validated_cleanup_root(value: Path) -> Path:
    root = Path(value).resolve()
    if (
        root.name.upper() != "DATASTORE"
        or not (root / "stocks").is_dir()
        or not (root / "market-data/databento").is_dir()
    ):
        raise DatastoreHygieneError(
            f"Refusing destructive cleanup outside a recognized DATASTORE root: {root}"
        )
    return root


def _validate_staging_candidate(
    root: Path,
    now: datetime,
    minimum_age_days: int,
) -> None:
    _validate_aged_tree(root, now, minimum_age_days)
    protected = [
        path
        for name in ("manifest.json", "receipt.json")
        for path in root.rglob(name)
    ]
    if protected:
        raise DatastoreHygieneError(
            f"Staging contains manifest/receipt evidence and will be preserved: {root}"
        )


def _validate_aged_tree(
    root: Path,
    now: datetime,
    minimum_age_days: int,
) -> None:
    files = _files_beneath(root)
    cutoff = now.astimezone(timezone.utc) - timedelta(days=minimum_age_days)
    for path in files:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if modified > cutoff:
            raise DatastoreHygieneError(
                f"Cleanup target is newer than the {minimum_age_days}-day floor: {path}"
            )


def _validate_operational_equity_dataset(
    root: Path,
    symbols: Sequence[str],
) -> None:
    receipts = sorted(
        (root / "catalog/migrations").glob("loop-a-equities-to-equs-mini-*.json")
    )
    if not receipts:
        raise DatastoreHygieneError(
            "Migration backups cannot be retired without the EQUS.MINI migration receipt"
        )
    for symbol in symbols:
        for timeframe, request_key in NATIVE_REQUESTS.items():
            raw = (
                root
                / "stocks"
                / symbol
                / "bars"
                / timeframe
                / "databento/raw"
                / f"{symbol}_{request_key}_raw.parquet"
            )
            normalized = (
                root
                / "stocks"
                / symbol
                / "bars"
                / timeframe
                / "databento/normalized"
                / f"{symbol}_{request_key}.parquet"
            )
            if not raw.is_file() or not normalized.is_file():
                raise DatastoreHygieneError(
                    f"Operational EQUS.MINI pair is incomplete: {normalized}"
                )
            schema = pq.read_schema(raw)
            if "provider_dataset" not in schema.names:
                raise DatastoreHygieneError(
                    f"Operational raw file has no dataset identity: {raw}"
                )
            values = {
                str(value).strip()
                for value in pd.read_parquet(raw, columns=["provider_dataset"])[
                    "provider_dataset"
                ].dropna()
            }
            if values != {"EQUS.MINI"}:
                raise DatastoreHygieneError(
                    f"Operational dataset is not exclusively EQUS.MINI: {raw}: {sorted(values)}"
                )


def _files_beneath(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise DatastoreHygieneError(f"Cleanup tree contains a symbolic link: {path}")
        if not path.is_file():
            continue
        resolved = path.resolve()
        _require_under_any(resolved, (resolved_root,))
        files.append(resolved)
    return sorted(files)


def _require_under_any(path: Path, roots: Sequence[Path]) -> None:
    if not any(_is_relative_to(path, root) for root in roots):
        raise DatastoreHygieneError(f"Cleanup path escaped every selected root: {path}")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _remove_empty_directories(root: Path) -> None:
    if not root.exists():
        return
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in (*directories, root):
        try:
            path.rmdir()
        except OSError:
            continue


def _selected_cleanup_classes(
    *,
    clean_staging: bool,
    retire_migration_backups: bool,
    consolidate_derived: bool,
) -> list[str]:
    return [
        name
        for enabled, name in (
            (clean_staging, "abandoned_staging"),
            (retire_migration_backups, "migration_backups"),
            (consolidate_derived, "shadowed_derived_bars"),
        )
        if enabled
    ]


def _read_json(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    _write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_text_atomic(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _minimum_text_date(left: object, right: object) -> str | None:
    values = [str(value) for value in (left, right) if str(value or "").strip()]
    return min(values) if values else None


def _maximum_text_date(left: object, right: object) -> str | None:
    values = [str(value) for value in (left, right) if str(value or "").strip()]
    return max(values) if values else None


def _finite_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if pd.notna(result) and abs(result) != float("inf") else None


def _human_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024.0 or unit == "TiB":
            return f"{amount:.2f} {unit}"
        amount /= 1024.0
    raise AssertionError("unreachable")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit Duckets market-data authority and optionally retire only "
            "receipt-planned staging/migration debris."
        )
    )
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    parser.add_argument("--symbols", nargs="+", default=None)
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default=None,
    )
    parser.add_argument("--clean-abandoned-staging", action="store_true")
    parser.add_argument("--retire-migration-backups", action="store_true")
    parser.add_argument("--consolidate-derived-bars", action="store_true")
    parser.add_argument("--confirm-cleanup", action="store_true")
    parser.add_argument("--minimum-age-days", type=int, default=7)
    args = parser.parse_args(argv)

    symbols = normalize_symbols(args.symbols or read_watchlist(args.watchlist))
    root = resolve_datastore_dir(root_dir=args.datastore, target=args.datastore_target)
    cleanup_requested = any(
        (
            args.clean_abandoned_staging,
            args.retire_migration_backups,
            args.consolidate_derived_bars,
        )
    )
    try:
        if cleanup_requested:
            result = clean_datastore(
                root,
                symbols=symbols,
                clean_staging=args.clean_abandoned_staging,
                retire_migration_backups=args.retire_migration_backups,
                consolidate_derived=args.consolidate_derived_bars,
                confirm=args.confirm_cleanup,
                minimum_age_days=args.minimum_age_days,
            )
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        catalog = audit_datastore(root, symbols=symbols, write=True)
        destination = Path(str(catalog["datastore_root"])) / "catalog/market-data"
        print(
            "Market-data catalog refreshed: "
            f"{destination / 'current.json'}; {destination / 'current.md'}"
        )
        return 0
    except DatastoreHygieneError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CATALOG_VERSION",
    "CLEANUP_PLAN_VERSION",
    "CLEANUP_RECEIPT_VERSION",
    "DatastoreHygieneError",
    "audit_datastore",
    "clean_datastore",
    "main",
]
