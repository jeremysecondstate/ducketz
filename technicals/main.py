from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from datafetching.layout import safe_token
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from technicals import TechnicalOutput
from technicals.calculations import (
    CALCULATIONS,
    DEFAULT_CALCULATIONS,
    WeeklyContextNotReady,
    calculation_accepts_input,
    calculation_output_timeframe,
)
from technicals.parquet_io import discover_bar_datasets, write_technical_parquet

DEFAULT_PROVIDERS = ("databento", "schwab")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run custom technical calculations over normalized bar Parquets."
    )
    parser.add_argument("symbol", help="Equity symbol, for example GOOG.")
    parser.add_argument(
        "--calculations",
        nargs="+",
        choices=tuple(CALCULATIONS),
        default=list(DEFAULT_CALCULATIONS),
        help=(
            "Calculations to run. Defaults to all Loop A technical outputs; "
            "provider/timeframe-specific families skip inapplicable inputs."
        ),
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=DEFAULT_PROVIDERS,
        default=list(DEFAULT_PROVIDERS),
        help="Normalized bar providers to analyze.",
    )
    parser.add_argument(
        "--timeframes",
        nargs="+",
        default=None,
        help="Optional canonical timeframe filter, for example 1m 5m 1h 1d.",
    )
    datastore_group = parser.add_mutually_exclusive_group()
    datastore_group.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default=None,
        help="Named input datastore target: pc or local.",
    )
    datastore_group.add_argument(
        "--datastore",
        type=Path,
        default=None,
        help="Custom input datastore path.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help=(
            "Custom technical output directory. Defaults to "
            "<datastore>/stocks/<SYMBOL>/technicals."
        ),
    )
    args = parser.parse_args(argv)

    symbol = args.symbol.strip().upper()
    if not symbol:
        parser.error("symbol is required")

    datastore_root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=args.datastore_target,
    )
    output_root = (
        args.output_root.expanduser()
        if args.output_root
        else datastore_root / "stocks" / safe_token(symbol) / "technicals"
    )
    datasets = discover_bar_datasets(
        datastore_root,
        symbol=symbol,
        providers=args.providers,
        timeframes=set(args.timeframes or []),
    )

    print("DUCKETS TECHNICALS")
    print("==================")
    print(f"Symbol: {symbol}")
    print(f"Input datastore: {datastore_root}")
    print(f"Output root: {output_root}")
    print(f"Providers: {', '.join(args.providers)}")
    print(f"Calculations: {', '.join(args.calculations)}")
    print()

    if not datasets:
        print("No normalized bar Parquets matched the requested symbol/providers/timeframes.")
        return 1

    outputs: list[TechnicalOutput] = []
    failures: list[str] = []
    skipped_not_ready: list[str] = []
    for dataset in datasets:
        print(
            f"[{dataset.provider}/{dataset.timeframe}] price adjustment: "
            f"{dataset.adjustment_status}; split events: {dataset.split_event_count}"
        )
        for calculation_name in args.calculations:
            if not calculation_accepts_input(
                calculation_name,
                provider=dataset.provider,
                timeframe=dataset.timeframe,
            ):
                continue
            calculation: Callable[..., pd.DataFrame] = CALCULATIONS[calculation_name]
            label = f"{dataset.provider}/{dataset.timeframe}/{calculation_name}"
            try:
                result = calculation(
                    dataset.frame,
                    symbol=dataset.symbol,
                    provider=dataset.provider,
                    timeframe=dataset.timeframe,
                )
                path = write_technical_parquet(
                    output_root,
                    calculation=calculation_name,
                    dataset=dataset,
                    frame=result,
                )
                output = TechnicalOutput(
                    calculation=calculation_name,
                    provider=dataset.provider,
                    timeframe=calculation_output_timeframe(
                        calculation_name,
                        input_timeframe=dataset.timeframe,
                    ),
                    rows=len(result),
                    path=path,
                )
                outputs.append(output)
                print(f"[{label}] {len(result)} rows -> {path}")
            except WeeklyContextNotReady as exc:
                detail = f"{label}: {exc}"
                skipped_not_ready.append(detail)
                print(f"[{label}] SKIPPED: {exc}")
            except Exception as exc:
                detail = f"{label}: {type(exc).__name__}: {exc}"
                failures.append(detail)
                print(f"[{label}] ERROR: {type(exc).__name__}: {exc}")

    print()
    print("TECHNICALS SUMMARY")
    print("==================")
    print(f"Output parquet files: {len(outputs)}")
    print(f"Not-ready calculations skipped: {len(skipped_not_ready)}")
    print(f"Failed calculations: {len(failures)}")
    return 1 if failures or (not outputs and not skipped_not_ready) else 0


if __name__ == "__main__":
    raise SystemExit(main())
