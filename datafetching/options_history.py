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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the resumable one-time OPRA Standard history bootstrap for "
            "one or more option parent symbols."
        )
    )
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument(
        "--schemas",
        nargs="+",
        choices=STANDARD_SCHEMAS,
        default=list(OPRA_SYMBOL_HISTORY_SCHEMA_ORDER),
        help="Schemas to bootstrap; defaults to every OPRA Standard schema.",
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default=None,
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
    store = ParquetStore(args.datastore, target=args.datastore_target)
    print("DUCKETS OPTIONS HISTORY BOOTSTRAP")
    print("=================================")
    print(f"DATASTORE: {store.root_dir}")
    print(f"Parent symbols: {', '.join(symbols)}")
    print(f"Schemas: {', '.join(args.schemas)}")
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
        bootstrap_missing=True,
    )
    print(
        "Options history bootstrap complete: "
        f"requested_scopes={summary.requested_scopes}; "
        f"completed_scopes={summary.completed_scopes}; "
        f"capacity_blocked_scopes={summary.capacity_blocked_scopes}; "
        f"failed_scopes={summary.failed_scopes}"
    )
    return 1 if summary.capacity_blocked_scopes or summary.failed_scopes else 0


if __name__ == "__main__":
    raise SystemExit(main())
