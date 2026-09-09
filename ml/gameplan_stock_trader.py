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
from ml.stock_trader.sizing_policy import LEARNED_SIZING_POLICY, SIZING_POLICIES, validate_sizing_policy


def run_gameplan_stock_trader_once(
    datastore_root: Path,
    *,
    decided_at: object | None = None,
    execute: bool = False,
    session: object | None = None,
    shadow_observe: bool = True,
    target_horizon: str = "1h",
    runtime_clock=None,
    sizing_policy: str = LEARNED_SIZING_POLICY,
) -> StockTraderRunResult:
    """Run the proven stock execution engine from the immutable gameplan."""

    sizing_policy = validate_sizing_policy(sizing_policy)
    if target_horizon != "all" and sizing_policy != LEARNED_SIZING_POLICY:
        raise ValueError("Fixed horizon sizing requires --target-horizon all")
    if target_horizon == "all":
        from ml.stock_trader.independent_runtime import run_independent_stock_trader_once
        return run_independent_stock_trader_once(
            datastore_root, decided_at=decided_at, execute=execute, session=session,
            runtime_clock=runtime_clock,
            **({"sizing_policy": sizing_policy} if sizing_policy != LEARNED_SIZING_POLICY else {}),
        )

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
            "Run one live-or-dry-run stock decision for the configured universe from the "
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
        choices=("1h", "4h", "all"),
        default="1h",
        help="Select one legacy horizon, or all due independent horizons with separate share ownership.",
    )
    parser.add_argument("--run-session", action="store_true",
                        help="With --target-horizon all, manage this exchange day's independent entries and owned-share exits until 17:00 Pacific.")
    parser.add_argument("--wait-for-open", action="store_true",
                        help="With --run-session --target-horizon all, wait without broker activity until 04:00 Pacific on the next supported session; an open session starts immediately.")
    parser.add_argument("--sizing-policy", choices=SIZING_POLICIES, default=LEARNED_SIZING_POLICY,
                        help="Explicitly select qualified learned sizing or conservative fixed horizon budgets.")
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
        if args.wait_for_open and not args.run_session:
            raise ValueError("--wait-for-open requires --run-session --target-horizon all")
        if args.run_session:
            if args.target_horizon != "all" or args.decided_at is not None:
                raise ValueError("--run-session requires --target-horizon all and the current wall clock")
            from ml.stock_trader.independent_session import run_independent_stock_session
            sizing_options = {"sizing_policy": args.sizing_policy} if args.sizing_policy != LEARNED_SIZING_POLICY else {}
            wait_options = {"wait_for_open": True} if args.wait_for_open else {}
            result = run_independent_stock_session(root, execute=bool(args.execute), **sizing_options, **wait_options)
            print(json.dumps(result, sort_keys=True))
            return 1 if result["status"] in {
                "SESSION_FINISHED_WITH_ERRORS", "NOOP_STOCK_FORECASTS_NOT_QUALIFIED",
                "NOOP_ENRICHMENT_NOT_QUALIFIED",
            } else 0
        result = run_gameplan_stock_trader_once(
            root,
            decided_at=args.decided_at,
            execute=bool(args.execute),
            shadow_observe=not args.no_shadow_observe,
            target_horizon=args.target_horizon,
            **({"sizing_policy": args.sizing_policy} if args.sizing_policy != LEARNED_SIZING_POLICY else {}),
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
            "INDEPENDENT_TARGET_PLAN_UNAVAILABLE",
            "HORIZON_BROKER_RECONCILIATION_UNAVAILABLE",
        }
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_gameplan_stock_trader_once"]
