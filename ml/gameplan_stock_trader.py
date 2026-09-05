from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.stock_trader.gameplan import (
    gameplan_prediction_pointer_sources,
    gate_gameplan_execution_signals,
    load_current_gameplan_prediction_signals,
    read_gameplan_stock_activation_intent,
)
from ml.stock_trader.runtime import StockTraderRunResult, run_stock_trader_once


def run_gameplan_stock_trader_once(
    datastore_root: Path,
    *,
    decided_at: object | None = None,
    execute: bool = False,
    session: object | None = None,
    shadow_observe: bool = True,
    target_horizon: str = "1h",
    runtime_clock=None,
) -> StockTraderRunResult:
    """Run the proven stock execution engine from the immutable gameplan."""

    return run_stock_trader_once(
        datastore_root,
        decided_at=decided_at,
        execute=execute,
        session=session,
        shadow_observe=shadow_observe,
        target_horizon=target_horizon,
        runtime_clock=runtime_clock,
        prediction_loader=load_current_gameplan_prediction_signals,
        direct_signal_gate=gate_gameplan_execution_signals,
        activation_reader=read_gameplan_stock_activation_intent,
        prediction_source_locator=gameplan_prediction_pointer_sources,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one live-or-dry-run six-symbol stock decision from the "
            "immutable nightly gameplan."
        )
    )
    datastore = parser.add_mutually_exclusive_group(required=True)
    datastore.add_argument("--root-dir", type=Path)
    datastore.add_argument(
        "--datastore-target",
        choices=sorted(DATASTORE_TARGETS),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Permit Schwab stock-order submission only when both persistent "
            "operator switches are TRUE."
        ),
    )
    parser.add_argument("--decided-at")
    parser.add_argument(
        "--target-horizon",
        choices=("1h", "4h"),
        default="1h",
        help="Execute only the due route for this exact forecast horizon.",
    )
    parser.add_argument(
        "--no-shadow-observe",
        action="store_true",
        help="Disable the paired non-submitting shadow lane.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(
            root_dir=args.root_dir,
            target=args.datastore_target,
        )
        result = run_gameplan_stock_trader_once(
            root,
            decided_at=args.decided_at,
            execute=bool(args.execute),
            shadow_observe=not args.no_shadow_observe,
            target_horizon=args.target_horizon,
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result.to_dict(), sort_keys=True))
    return (
        2
        if result.status
        in {
            "PREDICTION_INPUTS_UNAVAILABLE",
            "PREDICTION_EXECUTION_DEADLINE_PASSED",
            "BROKER_STATE_UNAVAILABLE",
            "SUBMISSION_STOPPED_AFTER_ERROR",
            "SUBMISSION_STOPPED_SAFETY_CHECK",
        }
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_gameplan_stock_trader_once"]
