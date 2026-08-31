from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Protocol, Sequence

from app.services.schwab import SchwabSession
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.stock_trader.contracts import StockTraderPolicy, TradeDecision, utc
from ml.stock_trader.control import read_activation_intent
from ml.stock_trader.engine import build_trade_decisions
from ml.stock_trader.inputs import load_current_prediction_signals
from ml.stock_trader.model import load_current_enrichment_model
from ml.stock_trader.publication import (
    DecisionPublication,
    publish_decision_run,
    record_execution_result,
    reserve_execution_intent,
)
from ml.stock_trader.session import (
    StockExecutionWindow,
    decision_targets_open,
    stock_execution_window,
)
from ml.stock_trader.state import SchwabReadSession, capture_portfolio_state


class SchwabTradingSession(SchwabReadSession, Protocol):
    def submit_order(self, order_payload: dict[str, object]) -> str | None: ...


@dataclass(frozen=True)
class StockTraderRunResult:
    status: str
    run_directory: Path
    selected_orders: int
    submitted_orders: int
    duplicate_suppressions: int
    stopped_after_error: bool
    execution_requested: bool
    activation_active: bool
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "run_directory": str(self.run_directory),
            "selected_orders": self.selected_orders,
            "submitted_orders": self.submitted_orders,
            "duplicate_suppressions": self.duplicate_suppressions,
            "stopped_after_error": self.stopped_after_error,
            "execution_requested": self.execution_requested,
            "activation_active": self.activation_active,
            "error": self.error,
        }


def run_stock_trader_once(
    datastore_root: Path,
    *,
    decided_at: object | None = None,
    execute: bool = False,
    session: SchwabTradingSession | None = None,
    policy: StockTraderPolicy | None = None,
    parallel_state: bool = True,
    shadow_observe: bool = True,
    allow_open_queue: bool = False,
) -> StockTraderRunResult:
    """Run one hourly stock decision cycle.

    Real submission requires both a deployment-level ``execute=True`` and the
    operator text file set to TRUE.  The ordinary/default call is non-mutating.
    """

    root = Path(datastore_root).resolve()
    timestamp = utc(decided_at)
    active_policy = policy or StockTraderPolicy()
    active_policy.validate()
    activation = read_activation_intent(root)
    with exclusive_runtime_lock(
        root / "locks" / "stock-trader-hourly.lock",
        process_name="stock-trader-hourly",
    ):
        if not activation.active:
            publication = publish_decision_run(
                root,
                (),
                decided_at=timestamp,
                activation=activation,
                policy=active_policy,
                execution_requested=execute,
                status="TRADER_INACTIVE",
            )
            return StockTraderRunResult(
                status="TRADER_INACTIVE",
                run_directory=publication.run_directory,
                selected_orders=0,
                submitted_orders=0,
                duplicate_suppressions=0,
                stopped_after_error=False,
                execution_requested=execute,
                activation_active=False,
            )
        try:
            signals, prediction_sources = load_current_prediction_signals(
                root, as_of=timestamp
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            publication = publish_decision_run(
                root,
                (),
                decided_at=timestamp,
                activation=activation,
                policy=active_policy,
                execution_requested=execute,
                source_files=_prediction_pointer_sources(root),
                status="PREDICTION_INPUTS_UNAVAILABLE",
            )
            return StockTraderRunResult(
                status="PREDICTION_INPUTS_UNAVAILABLE",
                run_directory=publication.run_directory,
                selected_orders=0,
                submitted_orders=0,
                duplicate_suppressions=0,
                stopped_after_error=False,
                execution_requested=execute,
                activation_active=True,
                error=detail,
            )
        try:
            model = load_current_enrichment_model(root)
            model_sources = _model_source_files(root)
            model_error = None
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            model = None
            model_sources = ()
            model_error = f"{type(exc).__name__}: {exc}"
        broker = session or SchwabSession()
        try:
            portfolio = capture_portfolio_state(
                broker, observed_at=timestamp, parallel=parallel_state
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            publication = publish_decision_run(
                root,
                (),
                decided_at=timestamp,
                activation=activation,
                policy=active_policy,
                execution_requested=execute,
                source_files=(*prediction_sources, *model_sources),
                status="BROKER_STATE_UNAVAILABLE",
            )
            return StockTraderRunResult(
                status="BROKER_STATE_UNAVAILABLE",
                run_directory=publication.run_directory,
                selected_orders=0,
                submitted_orders=0,
                duplicate_suppressions=0,
                stopped_after_error=False,
                execution_requested=execute,
                activation_active=True,
                error=detail,
            )
        live_decisions = build_trade_decisions(
            signals,
            portfolio,
            model,
            activation,
            policy=active_policy,
            decided_at=timestamp,
            model_unavailable_reason=model_error,
            decision_lane="LIVE",
        )
        shadow_decisions: tuple[TradeDecision, ...] = ()
        if shadow_observe:
            shadow_policy = replace(
                active_policy,
                policy_version="stock-trader-shadow-challenger-v1",
                minimum_trade_probability=max(
                    0.0, active_policy.minimum_trade_probability - 0.10
                ),
                maximum_symbol_equity_fraction=min(
                    1.0, active_policy.maximum_symbol_equity_fraction * 1.25
                ),
                maximum_single_order_equity_fraction=min(
                    1.0, active_policy.maximum_single_order_equity_fraction * 1.50
                ),
            )
            shadow_decisions = build_trade_decisions(
                signals,
                portfolio,
                model,
                activation,
                policy=shadow_policy,
                decided_at=timestamp,
                model_unavailable_reason=model_error,
                decision_lane="SHADOW",
            )
        decisions = (*live_decisions, *shadow_decisions)
        publication = publish_decision_run(
            root,
            decisions,
            decided_at=timestamp,
            activation=activation,
            policy=active_policy,
            execution_requested=execute,
            source_files=(*prediction_sources, *model_sources),
        )
        if not execute:
            return StockTraderRunResult(
                status=(
                    "DRY_RUN_ORDERS_SELECTED"
                    if any(decision.quantity > 0 for decision in live_decisions)
                    else "DRY_RUN_NO_TRADE"
                ),
                run_directory=publication.run_directory,
                selected_orders=sum(
                    decision.quantity > 0 for decision in live_decisions
                ),
                submitted_orders=0,
                duplicate_suppressions=0,
                stopped_after_error=False,
                execution_requested=False,
                activation_active=True,
            )
        execution_window = stock_execution_window(
            timestamp, allow_open_queue=allow_open_queue
        )
        if not execution_window.executable:
            return StockTraderRunResult(
                status="EXECUTION_WINDOW_CLOSED_SHADOW_RECORDED",
                run_directory=publication.run_directory,
                selected_orders=sum(
                    decision.quantity > 0 for decision in live_decisions
                ),
                submitted_orders=0,
                duplicate_suppressions=0,
                stopped_after_error=False,
                execution_requested=True,
                activation_active=True,
                error=execution_window.reason,
            )
        return _submit_selected_orders(
            root,
            broker,
            decisions,
            publication,
            timestamp=timestamp,
            execution_window=execution_window,
        )


def _submit_selected_orders(
    root: Path,
    broker: SchwabTradingSession,
    decisions: Sequence[TradeDecision],
    publication: DecisionPublication,
    *,
    timestamp: object,
    execution_window: StockExecutionWindow,
) -> StockTraderRunResult:
    selected = [
        decision
        for decision in decisions
        if decision.decision_lane == "LIVE"
        and decision.quantity > 0
        and decision_targets_open(
            decision.prediction.get("target_window_start"), execution_window
        )
    ]
    submitted = 0
    duplicates = 0
    stopped = False
    for decision in selected:
        if decision.action not in {"BUY", "SELL"} or not isinstance(
            decision.order_payload, Mapping
        ):
            continue
        event = reserve_execution_intent(
            root,
            decision,
            submitted_at=timestamp,
            decision_publication=publication,
        )
        if event is None:
            duplicates += 1
            continue
        try:
            location = broker.submit_order(dict(decision.order_payload))
        except Exception as exc:
            record_execution_result(
                event,
                status="SUBMISSION_FAILED_OR_UNKNOWN",
                completed_at=utc(),
                error=f"{type(exc).__name__}: {exc}",
            )
            stopped = True
            break
        record_execution_result(
            event,
            status="SUBMITTED",
            completed_at=utc(),
            broker_location=location,
        )
        submitted += 1
    return StockTraderRunResult(
        status=(
            "SUBMISSION_STOPPED_AFTER_ERROR"
            if stopped
            else "ORDERS_SUBMITTED"
            if submitted
            else "NO_ORDERS_SUBMITTED"
        ),
        run_directory=publication.run_directory,
        selected_orders=len(selected),
        submitted_orders=submitted,
        duplicate_suppressions=duplicates,
        stopped_after_error=stopped,
        execution_requested=True,
        activation_active=True,
    )


def _model_source_files(root: Path) -> tuple[Path, ...]:
    pointer = root / "ml" / "stock-trader-model-latest" / "run.json"
    if not pointer.is_file():
        return ()
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    run_path = payload.get("run_path") if isinstance(payload, dict) else None
    if not isinstance(run_path, str):
        return (pointer,)
    run = (root / run_path).resolve()
    return tuple(
        path
        for path in (pointer, run / "manifest.json", run / "model.json")
        if path.is_file()
    )


def _prediction_pointer_sources(root: Path) -> tuple[Path, ...]:
    pointer = root / "ml" / "latest" / "run.json"
    return (pointer,) if pointer.is_file() else ()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the six-symbol hourly ML-enriched stock trader."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))
    parser.add_argument(
        "--execute",
        action="store_true",
        help=(
            "Enable broker submission for this deployment. The separate "
            "CONFIRM_ACTIVE_TRADING=TRUE operator file is also required."
        ),
    )
    parser.add_argument("--decided-at")
    parser.add_argument(
        "--queue-at-open",
        action="store_true",
        help=(
            "Permit NORMAL/DAY orders during the XNYS pre-open only when "
            "their prediction target starts at the core open."
        ),
    )
    parser.add_argument(
        "--no-shadow-observe",
        action="store_true",
        help="Disable the paired shadow challenger for this invocation.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(
            root_dir=args.root_dir, target=args.datastore_target
        )
        result = run_stock_trader_once(
            root,
            decided_at=args.decided_at,
            execute=bool(args.execute),
            shadow_observe=not args.no_shadow_observe,
            allow_open_queue=bool(args.queue_at_open),
        )
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(result.to_dict(), sort_keys=True))
    return (
        2
        if result.status
        in {
            "PREDICTION_INPUTS_UNAVAILABLE",
            "BROKER_STATE_UNAVAILABLE",
            "SUBMISSION_STOPPED_AFTER_ERROR",
        }
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["StockTraderRunResult", "main", "run_stock_trader_once"]
