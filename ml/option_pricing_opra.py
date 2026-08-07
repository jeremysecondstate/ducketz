from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

import pandas as pd

from datafetching.decision_time import latest_completed_bar_clock
from datafetching.cme_runtime import load_repository_environment
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.option_pricing.opra import (
    DEFAULT_MARKET_TIMES,
    DEFAULT_SYMBOLS,
    normalize_definition_records,
    read_opra_import,
    resolve_market_schedule,
    run_import_phase,
)
from ml.option_pricing.causal import completed_bar_close


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Estimate or explicitly import narrow historical OPRA.PILLAR evidence. "
            "The default is cost estimation only; paid requests require both "
            "--execute and --max-cost-usd."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument("--symbols", nargs="+", default=list(DEFAULT_SYMBOLS))
    parser.add_argument(
        "--start-date",
        default=None,
        help="First XNYS session date (default: six calendar months before end).",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="Last XNYS session date (default: yesterday in America/New_York).",
    )
    parser.add_argument(
        "--market-times",
        nargs="+",
        default=list(DEFAULT_MARKET_TIMES),
        help="America/New_York HH:MM observations; early-close times are removed.",
    )
    parser.add_argument(
        "--definition-evidence",
        type=Path,
        default=None,
        help=(
            "Verified definition-phase evidence directory. When omitted, the "
            "latest matching verified definition import is used if available."
        ),
    )
    parser.add_argument(
        "--definitions-only",
        action="store_true",
        help="Estimate/import definitions even when prior definition evidence exists.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute this bounded phase after all request costs pass the ceiling.",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help="Required explicit aggregate ceiling for --execute.",
    )
    args = parser.parse_args(argv)
    if args.execute and args.max_cost_usd is None:
        parser.error("--execute requires --max-cost-usd")
    if args.max_cost_usd is not None and args.max_cost_usd < 0:
        parser.error("--max-cost-usd must be non-negative")

    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    today_ny = pd.Timestamp.now(tz="America/New_York").date()
    end = pd.Timestamp(args.end_date).date() if args.end_date else today_ny - pd.Timedelta(days=1)
    start = (
        pd.Timestamp(args.start_date).date()
        if args.start_date
        else (pd.Timestamp(end) - pd.DateOffset(months=6)).date()
    )
    schedule = resolve_market_schedule(
        symbols=args.symbols,
        start_date=start,
        end_date=end,
        market_times=args.market_times,
    )
    if not schedule:
        parser.error("The requested scope resolves to no eligible XNYS market times")

    load_repository_environment()
    key = os.environ.get("DATABENTO_API_KEY")
    if not key:
        parser.error("DATABENTO_API_KEY is required for metadata cost estimation")
    import databento as db

    definitions = None
    definition_directory = None
    reference_underlyings: dict[tuple[str, str], float] = {}
    if not args.definitions_only:
        definition_directory = args.definition_evidence or _latest_definition_import(root)
        if definition_directory is not None:
            definitions = _load_definition_evidence(
                definition_directory,
                datastore_root=root,
                databento_module=db,
            )
            for point in schedule:
                try:
                    target = pd.Timestamp(point.target_snapshot_for)
                    decision = latest_completed_bar_clock(
                        root,
                        symbol=point.symbol,
                        as_of=target,
                    )
                    if pd.Timestamp(decision.decision_timestamp) == target:
                        reference_underlyings[(point.symbol, point.target_snapshot_for)] = (
                            completed_bar_close(decision)
                        )
                except Exception:
                    # The narrow CBBO planner skips observations without exact
                    # completed underlying evidence; it never widens the chain.
                    continue

    print("DUCKETS OPRA HISTORICAL EVIDENCE")
    print("=================================")
    print(f"DATASTORE: {root}")
    print("Dataset: OPRA.PILLAR")
    print(f"Symbols: {', '.join(dict.fromkeys(value.upper() for value in args.symbols))}")
    print(f"Sessions: {start} through {end}")
    print(f"America/New_York times: {', '.join(args.market_times)}")
    print(f"Resolved symbol-time observations: {len(schedule)}")
    print("Mode: PAID EXECUTION" if args.execute else "Mode: ESTIMATE ONLY (no get_range)")
    if definition_directory is not None:
        print(f"Definition evidence: {definition_directory}")
        print(
            "Completed underlying observations usable for moneyness filtering: "
            f"{len(reference_underlyings)}"
        )
    print()

    result = run_import_phase(
        root,
        client=db.Historical(key),
        schedule=schedule,
        execute=args.execute,
        max_cost_usd=args.max_cost_usd,
        normalized_definitions=definitions,
        reference_underlyings=reference_underlyings,
    )
    print()
    print(
        f"{result.status}: phase={result.phase}; requests={result.request_count}; "
        f"estimated_cost_usd={result.estimated_cost_usd:.6f}; "
        f"downloaded={result.downloaded_count}"
    )
    if result.evidence_directory is not None:
        print(f"Immutable evidence: {result.evidence_directory}")
    if result.phase == "definitions" and result.status in {"IMPORTED", "ALREADY_COMMITTED"}:
        print("Next step: rerun estimate-only to filter definitions and cost CBBO requests.")
    return 0


def _latest_definition_import(datastore_root: Path) -> Path | None:
    evidence = Path(datastore_root) / "ml" / "option-pricing-evidence" / "opra"
    for receipt in sorted(evidence.glob("*/receipt.json"), reverse=True):
        try:
            payload = read_opra_import(receipt.parent, datastore_root=datastore_root)
        except Exception:
            continue
        manifest = payload["manifest"]
        if manifest.get("phase") == "definitions":
            return receipt.parent
    return None


def _load_definition_evidence(
    directory: Path,
    *,
    datastore_root: Path,
    databento_module: object,
) -> pd.DataFrame:
    payload = read_opra_import(directory, datastore_root=datastore_root)
    manifest = payload["manifest"]
    if manifest.get("phase") != "definitions":
        raise RuntimeError("--definition-evidence is not a definition-phase import")
    outputs = manifest.get("outputs", {})
    frames: list[pd.DataFrame] = []
    for name in sorted(outputs):
        path = Path(directory) / str(name)
        store = databento_module.DBNStore.from_file(path)
        frames.append(
            store.to_df(
                price_type="fixed",
                pretty_ts=False,
                map_symbols=True,
            ).reset_index()
        )
    raw = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    return normalize_definition_records(raw)


if __name__ == "__main__":
    raise SystemExit(main())
