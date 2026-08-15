from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_opra_history import (
    STANDARD_SCHEMAS,
    OpraCapacityError,
    SyncScope,
    canonical_root,
    discover_standard_entitlement,
    publish_health,
    publish_storage_preflight,
    storage_preflight,
    synchronize,
)
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize account-included OPRA Standard history into immutable "
            "provider-native DBN and normalized Parquet partitions. The default "
            "scope is every included schema and the full OPRA universe."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target", choices=tuple(DATASTORE_TARGETS), default="pc"
    )
    parser.add_argument(
        "--schemas",
        nargs="+",
        choices=STANDARD_SCHEMAS,
        default=list(STANDARD_SCHEMAS),
        help="Schema subset for a resumable maintenance batch (default: all).",
    )
    parser.add_argument(
        "--start",
        default=None,
        help="Optional inclusive UTC partition date; never expands entitlement bounds.",
    )
    parser.add_argument(
        "--end",
        default=None,
        help="Optional exclusive UTC partition date; never expands entitlement bounds.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help=(
            "Optional parent-symbol maintenance scope. Omit for the required full "
            "OPRA universe."
        ),
    )
    parser.add_argument(
        "--max-partitions",
        type=int,
        default=None,
        help="Bound one resumable invocation without changing the recorded scope.",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Refresh provider-confirmed entitlement and capacity metadata only.",
    )
    parser.add_argument(
        "--health-only",
        action="store_true",
        help="Verify local partitions and publish actual file/row/timestamp health.",
    )
    args = parser.parse_args(argv)
    if args.max_partitions is not None and args.max_partitions < 1:
        parser.error("--max-partitions must be positive")

    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    if args.health_only:
        print(publish_health(root))
        return 0
    load_repository_environment()
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key:
        parser.error("DATABENTO_API_KEY is required")
    import databento as db

    scope = SyncScope(
        schemas=tuple(args.schemas),
        start=args.start,
        end=args.end,
        symbols=tuple(args.symbols or ()),
        max_partitions=args.max_partitions,
    )
    client = db.Historical(key)
    with exclusive_runtime_lock(
        canonical_root(root) / "state" / "sync.lock",
        process_name="Databento OPRA history synchronizer",
    ):
        entitlement = discover_standard_entitlement(client, datastore_root=root)
        if args.metadata_only:
            preflight = storage_preflight(
                client,
                datastore_root=root,
                entitlement=entitlement,
                scope=scope,
            )
            preflight = publish_storage_preflight(root, preflight)
            print(json.dumps(preflight, indent=2, sort_keys=True, default=str))
            return 0 if bool(preflight["capacity_pass"]) else 2
        try:
            result = synchronize(
                client,
                datastore_root=root,
                entitlement=entitlement,
                scope=scope,
            )
        except OpraCapacityError as exc:
            print(f"CAPACITY_BLOCKED {exc}")
            return 2
    print(
        f"{result.status} completed_partitions={result.completed_partitions} "
        f"skipped_partitions={result.skipped_partitions} rows={result.completed_rows} "
        f"parquet_bytes={result.completed_bytes} health={result.health_path}"
    )
    if result.errors:
        print(json.dumps(result.errors, indent=2, sort_keys=True))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
