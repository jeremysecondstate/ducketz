from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datafetching.layout import safe_token
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from fundamentals.parquet_io import load_fundamental_parquet
from signals.calculation import calculate_fundamental_technical_lifecycle
from signals.parquet_io import load_market_regime_outputs, write_signal_parquet
from signals.technical_lifecycle import (
    calculate_technical_lifecycle_snapshot,
    persist_technical_lifecycle,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build the point-in-time Duckets Fundamental-Technical Lifecycle signal "
            "from fundamental-direction and market-regime Parquets."
        )
    )
    parser.add_argument("symbol", help="Equity symbol, for example MU.")
    datastore_group = parser.add_mutually_exclusive_group()
    datastore_group.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default=None,
        help="Named datastore target: pc or local.",
    )
    datastore_group.add_argument(
        "--datastore",
        type=Path,
        default=None,
        help="Custom datastore path.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help=(
            "Custom signal output directory. Defaults to "
            "<datastore>/stocks/<SYMBOL>/signals."
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
    stock_root = datastore_root / "stocks" / safe_token(symbol)
    technicals_root = stock_root / "technicals"
    fundamentals_root = stock_root / "fundamentals"
    output_root = (
        args.output_root.expanduser()
        if args.output_root
        else stock_root / "signals"
    )

    technical_frames = load_market_regime_outputs(technicals_root)
    fundamentals = load_fundamental_parquet(
        fundamentals_root,
        period_type="quarterly",
        source="fmp",
    )

    print("DUCKETS SIGNALS")
    print("===============")
    print(f"Symbol: {symbol}")
    print(f"Input datastore: {datastore_root}")
    print(f"Technical series: {len(technical_frames)}")
    print(f"Quarterly fundamental rows: {len(fundamentals)}")
    print()

    if not technical_frames:
        print("No supported market-regime Parquets were found.")
        return 1
    outputs = 0
    if fundamentals.empty:
        print("No quarterly fundamental-direction Parquet; legacy lifecycle skipped.")
    else:
        try:
            result = calculate_fundamental_technical_lifecycle(
                technical_frames,
                fundamentals,
                symbol=symbol,
            )
            path = write_signal_parquet(output_root, frame=result)
            latest = result.iloc[-1]
            print(f"Rows: {len(result)}")
            print(f"Latest phase: {latest['lifecycle_phase']}")
            print(f"Latest setup quality: {latest['setup_quality']:.2f}")
            print(f"Output: {path}")
            outputs += 1
        except Exception as exc:
            print(f"LEGACY ERROR: {type(exc).__name__}: {exc}")

    try:
        technical = calculate_technical_lifecycle_snapshot(
            technical_frames,
            symbol=symbol,
        )
        technical_path = persist_technical_lifecycle(output_root, technical)
        print(f"Technical lifecycle rows: {len(technical)}")
        print(f"Technical lifecycle output: {technical_path}")
        outputs += 1
    except Exception as exc:
        print(f"TECHNICAL LIFECYCLE ERROR: {type(exc).__name__}: {exc}")

    return 0 if outputs else 1


if __name__ == "__main__":
    raise SystemExit(main())
