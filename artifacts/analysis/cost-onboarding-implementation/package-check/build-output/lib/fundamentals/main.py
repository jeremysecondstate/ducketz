from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from fundamentals.calculation import calculate_fundamental_direction
from fundamentals.parquet_io import discover_statement_frames, write_fundamental_parquet
from fundamentals.point_in_time import (
    calculate_point_in_time_fundamentals,
    persist_point_in_time_fundamentals,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build point-in-time fundamental direction Parquets from corporate statements."
    )
    parser.add_argument("symbol", help="Equity symbol, for example NVDA.")
    parser.add_argument(
        "--period-types",
        nargs="+",
        choices=("quarterly", "annual"),
        default=["quarterly", "annual"],
    )
    datastore_group = parser.add_mutually_exclusive_group()
    datastore_group.add_argument("--datastore-target", choices=tuple(DATASTORE_TARGETS), default=None)
    datastore_group.add_argument("--datastore", type=Path, default=None)
    args = parser.parse_args(argv)

    symbol = args.symbol.strip().upper()
    datastore_root = resolve_datastore_dir(root_dir=args.datastore, target=args.datastore_target)
    outputs = 0
    failures: list[str] = []

    print("DUCKETS FUNDAMENTALS")
    print("====================")
    print(f"Symbol: {symbol}")
    print(f"Input datastore: {datastore_root}")
    print()

    for period_type in args.period_types:
        try:
            income, balance, cash_flow, source_files = discover_statement_frames(
                datastore_root,
                symbol=symbol,
                period_type=period_type,
            )
            result = calculate_fundamental_direction(
                income,
                balance,
                cash_flow,
                symbol=symbol,
                period_type=period_type,
            )
            path = write_fundamental_parquet(
                datastore_root,
                symbol=symbol,
                period_type=period_type,
                source="fmp",
                frame=result,
                source_files=source_files,
            )
            outputs += 1
            print(f"[{period_type}] {len(result)} rows -> {path}")
            point_in_time = calculate_point_in_time_fundamentals(
                income,
                balance,
                cash_flow,
                symbol=symbol,
                period_type=period_type,
            )
            point_in_time_path = persist_point_in_time_fundamentals(
                datastore_root,
                point_in_time,
                symbol=symbol,
                period_type=period_type,
            )
            outputs += 1
            print(
                f"[{period_type}/point-in-time] {len(point_in_time)} rows -> "
                f"{point_in_time_path}"
            )
        except Exception as exc:
            detail = f"{period_type}: {type(exc).__name__}: {exc}"
            failures.append(detail)
            print(f"[{period_type}] ERROR: {type(exc).__name__}: {exc}")

    print()
    print(f"Fundamental outputs: {outputs}; failures: {len(failures)}")
    return 0 if outputs else 1


if __name__ == "__main__":
    raise SystemExit(main())
