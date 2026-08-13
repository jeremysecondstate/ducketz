from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path
from typing import Sequence

import pandas as pd

from datafetching.cme_runtime import load_repository_environment
from datafetching.fred_alfred_readiness import (
    FredAlfredReadinessError,
    FredAlfredRequestPlan,
    derive_fred_alfred_backfill_plan,
    derive_fred_alfred_incremental_plan,
    verify_and_publish_fred_alfred_readiness,
)
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
            "Import immutable FRED/ALFRED real-time vintage evidence and "
            "publish a separate verified Loop B readiness receipt."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--backfill",
        action="store_true",
        help=(
            "One-time complete import; derive request bounds from the earliest "
            "eligible daily/weekly model decision and macro lags."
        ),
    )
    mode.add_argument(
        "--incremental",
        action="store_true",
        help="Bounded overlapping update after the one-time backfill.",
    )
    parser.add_argument(
        "--series",
        nargs="+",
        choices=FRED_ALFRED_SUPPORTED_SERIES,
        default=list(FRED_ALFRED_SUPPORTED_SERIES),
        help="ALFRED series scope (default: the complete four-series contract).",
    )
    parser.add_argument(
        "--realtime-start",
        default=None,
        help="First provider real-time/vintage date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--realtime-end",
        default=None,
        help="Last provider real-time/vintage date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--observation-start",
        default=None,
        help="First economic observation date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--observation-end",
        default=None,
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
        plan = _request_plan(root, args=args)
    except (FredAlfredReadinessError, ValueError) as exc:
        parser.error(str(exc))
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
                realtime_start=plan.realtime_start,
                realtime_end=plan.realtime_end,
                observation_start=plan.observation_start,
                observation_end=plan.observation_end,
            )
            readiness = verify_and_publish_fred_alfred_readiness(
                root,
                import_result=result,
            )
    except (FredAlfredReadinessError, FredVintageImportError, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "status": "VERIFIED_READY",
                "mode": plan.mode,
                "datastore": str(root),
                "evidence_directory": str(result.evidence_directory),
                "readiness_directory": str(readiness.directory),
                "series_count": result.series_count,
                "row_count": result.row_count,
                "vintage_partition_paths": [
                    str(path) for path in result.vintage_partition_paths
                ],
                "release_feature_paths": [
                    str(path) for path in result.release_feature_paths
                ],
                "current_revised_history_used": False,
                "historical_coverage_status": "PASS",
                "loop_b_consumption_authorized": True,
                "automated_action_allowed": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _request_plan(root: Path, *, args: argparse.Namespace) -> FredAlfredRequestPlan:
    complete_series = tuple(dict.fromkeys(args.series))
    if set(complete_series) != set(FRED_ALFRED_SUPPORTED_SERIES):
        raise ValueError(
            "Verified macro ingestion requires FEDFUNDS, CPIAUCSL, UNRATE, and GDP"
        )
    supplied_bounds = (
        args.realtime_start,
        args.realtime_end,
        args.observation_start,
        args.observation_end,
    )
    if args.backfill:
        if any(value is not None for value in supplied_bounds):
            raise ValueError("--backfill derives all bounds; do not supply manual dates")
        return derive_fred_alfred_backfill_plan(root)
    if args.incremental:
        if any(value is not None for value in supplied_bounds):
            raise ValueError(
                "--incremental derives bounded request dates; do not supply manual dates"
            )
        return derive_fred_alfred_incremental_plan(root)
    if any(value is None for value in supplied_bounds):
        raise ValueError(
            "Select --backfill or --incremental, or provide all four explicit date bounds"
        )
    decision_source, decisions = _manual_decision_context(root)
    return FredAlfredRequestPlan(
        mode="MANUAL",
        realtime_start=date.fromisoformat(str(args.realtime_start)),
        realtime_end=date.fromisoformat(str(args.realtime_end)),
        observation_start=date.fromisoformat(str(args.observation_start)),
        observation_end=date.fromisoformat(str(args.observation_end)),
        earliest_eligible_decision=decisions,
        decision_source=decision_source,
    )


def _manual_decision_context(root: Path) -> tuple[Path, pd.Timestamp]:
    plan = derive_fred_alfred_backfill_plan(root)
    return plan.decision_source, plan.earliest_eligible_decision


if __name__ == "__main__":
    raise SystemExit(main())
