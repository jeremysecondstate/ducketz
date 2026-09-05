from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_opra_history import STANDARD_SCHEMAS
from datafetching.options_runtime import (
    OPRA_SYMBOL_HISTORY_SCHEMA_ORDER,
    opra_history_lookback_label,
    synchronize_option_history,
)
from datafetching.orchestrate import DEFAULT_WATCHLIST, normalize_symbols, read_watchlist
from datafetching.parquet_store import DATASTORE_TARGETS, ParquetStore


DEFAULT_MAX_ESTIMATED_DOWNLOAD_BYTES = 2_000_000_000
DEFAULT_MAX_ESTIMATED_COST_USD = 5.0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a provider-preflighted, budget-bounded OPRA Standard history "
            "bootstrap or incremental catch-up for one or more parent symbols."
        )
    )
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument(
        "--schemas",
        nargs="+",
        choices=STANDARD_SCHEMAS,
        default=list(OPRA_SYMBOL_HISTORY_SCHEMA_ORDER),
        help=(
            "Schemas to bootstrap; defaults to the prediction-focused baseline. "
            "Research-only cmbp-1 remains available as an explicit choice."
        ),
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default=None,
    )
    parser.add_argument(
        "--incremental-only",
        action="store_true",
        help="Maintain valid existing cursors and leave missing scopes bootstrap-required.",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Publish provider size/cost preflights without downloading history.",
    )
    parser.add_argument(
        "--max-estimated-download-bytes",
        type=_positive_int,
        default=None,
        help=(
            "Maximum provider-estimated bytes selected in this run. Defaults to "
            "DATABENTO_MAX_FETCH_BYTES or 2000000000."
        ),
    )
    parser.add_argument(
        "--max-estimated-cost-usd",
        type=_nonnegative_float,
        default=None,
        help=(
            "Maximum provider-estimated USD selected in this run. Defaults to "
            "DATABENTO_MAX_FETCH_COST_USD or 5."
        ),
    )
    parser.add_argument(
        "--max-incremental-catchup-days",
        type=_positive_int,
        default=2,
        help=(
            "Maximum calendar days by which an existing cursor may advance in one "
            "run; defaults to 2 so large gaps remain budgetable and resumable."
        ),
    )
    args = parser.parse_args(argv)
    try:
        symbols = normalize_symbols(args.symbols or read_watchlist(args.watchlist))
    except Exception as exc:
        parser.error(str(exc))
    if not symbols:
        parser.error("At least one parent symbol is required")

    load_repository_environment()
    api_key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not api_key:
        parser.error("DATABENTO_API_KEY is required; history bootstrap was not started")
    try:
        maximum_bytes = (
            args.max_estimated_download_bytes
            if args.max_estimated_download_bytes is not None
            else _environment_positive_int(
                "DATABENTO_MAX_FETCH_BYTES",
                DEFAULT_MAX_ESTIMATED_DOWNLOAD_BYTES,
            )
        )
        maximum_cost = (
            args.max_estimated_cost_usd
            if args.max_estimated_cost_usd is not None
            else _environment_nonnegative_float(
                "DATABENTO_MAX_FETCH_COST_USD",
                DEFAULT_MAX_ESTIMATED_COST_USD,
            )
        )
    except ValueError as exc:
        parser.error(str(exc))
    store = ParquetStore(args.datastore, target=args.datastore_target)
    print("DUCKETS OPTIONS HISTORY MAINTENANCE")
    print("===================================")
    print(f"DATASTORE: {store.root_dir}")
    print(f"Parent symbols: {', '.join(symbols)}")
    print(f"Schemas: {', '.join(args.schemas)}")
    print(
        "Mode: "
        + ("preflight only" if args.preflight_only else "download and verify")
        + ("; incremental cursors only" if args.incremental_only else "; bootstrap allowed")
    )
    print(
        f"Run budget: bytes<={maximum_bytes}; estimated_cost_usd<={maximum_cost:.2f}"
    )
    print(f"Maximum incremental cursor advance: {args.max_incremental_catchup_days} days")
    print(
        "Lookbacks: "
        + "; ".join(
            f"{schema}={opra_history_lookback_label(schema)}"
            for schema in args.schemas
        )
    )
    summary = synchronize_option_history(
        store,
        api_key=api_key,
        symbols=symbols,
        schemas=args.schemas,
        reporter=print,
        bootstrap_missing=not args.incremental_only,
        preflight_only=args.preflight_only,
        max_estimated_download_bytes=maximum_bytes,
        max_estimated_cost_usd=maximum_cost,
        max_incremental_catchup_days=args.max_incremental_catchup_days,
    )
    print(
        "Options history maintenance finished: "
        f"requested_scopes={summary.requested_scopes}; "
        f"completed_scopes={summary.completed_scopes}; "
        f"capacity_blocked_scopes={summary.capacity_blocked_scopes}; "
        f"failed_scopes={summary.failed_scopes}; "
        f"bootstrap_required_scopes={summary.bootstrap_required_scopes}; "
        f"preflighted_scopes={summary.preflighted_scopes}; "
        f"deferred_scopes={summary.deferred_scopes}; "
        f"selected_estimated_download_bytes={summary.selected_estimated_download_bytes}; "
        "selected_estimated_cost_usd="
        f"{summary.selected_estimated_cost_usd if summary.selected_estimated_cost_usd is not None else 'UNKNOWN'}"
    )
    if summary.capacity_blocked_scopes or summary.failed_scopes:
        return 1
    if not args.preflight_only and summary.deferred_scopes and not summary.completed_scopes:
        return 2
    return 0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def _environment_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _environment_nonnegative_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be nonnegative") from exc
    if value < 0:
        raise ValueError(f"{name} must be nonnegative")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
