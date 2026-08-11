from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from datafetching.cme_runtime import load_repository_environment
from datafetching.fred_vintage_import import (
    FRED_ALFRED_SUPPORTED_SERIES,
    FredAlfredClient,
    FredVintageImportError,
    import_fred_alfred_vintages,
)
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Import immutable FRED/ALFRED real-time vintage evidence. "
            "All four date bounds are explicit; current-revised history is rejected."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument(
        "--series",
        nargs="+",
        choices=FRED_ALFRED_SUPPORTED_SERIES,
        default=["FEDFUNDS"],
        help="Explicit ALFRED series scope (default: FEDFUNDS).",
    )
    parser.add_argument(
        "--realtime-start",
        required=True,
        help="First provider real-time/vintage date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--realtime-end",
        required=True,
        help="Last provider real-time/vintage date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--observation-start",
        required=True,
        help="First economic observation date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--observation-end",
        required=True,
        help="Last economic observation date (YYYY-MM-DD).",
    )
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    load_repository_environment()
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        parser.error(
            "FRED_API_KEY is required in the process environment or repository .env"
        )
    try:
        client = FredAlfredClient(api_key)
        with exclusive_runtime_lock(
            root / ".ducketz-fred-alfred-import.lock",
            process_name="Duckets FRED/ALFRED vintage importer",
        ):
            result = import_fred_alfred_vintages(
                root,
                client=client,
                series_ids=args.series,
                realtime_start=args.realtime_start,
                realtime_end=args.realtime_end,
                observation_start=args.observation_start,
                observation_end=args.observation_end,
            )
    except FredVintageImportError as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "status": "IMPORTED_NOT_YET_COVERAGE_VERIFIED",
                "datastore": str(root),
                "evidence_directory": str(result.evidence_directory),
                "series_count": result.series_count,
                "row_count": result.row_count,
                "vintage_partition_paths": [
                    str(path) for path in result.vintage_partition_paths
                ],
                "rate_feature_paths": [
                    str(path) for path in result.rate_feature_paths
                ],
                "current_revised_history_used": False,
                "historical_coverage_status": "NOT_EVALUATED",
                "automated_action_allowed": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
