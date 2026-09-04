from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from app.services.schwab import SchwabSession
from app.services.schwab_retry import is_retryable_schwab_error
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.stock_trader.contracts import (
    PortfolioState,
    PredictionSignal,
    STOCK_TRADER_SYMBOLS,
    StockTraderPolicy,
    TradeDecision,
    utc,
)
from ml.stock_trader.control import read_activation_intent
from ml.stock_trader.engine import build_trade_decisions
from ml.stock_trader.handoff import (
    DEFAULT_CUTOFF_LEAD_SECONDS,
    DEFAULT_MAXIMUM_TARGET_LEAD_SECONDS,
    DEFAULT_POLL_SECONDS,
    consumed_live_prediction_ids,
    wait_for_actionable_prediction,
)
from ml.stock_trader.inputs import (
    PRIMARY_STOCK_HORIZONS,
    load_current_prediction_signals,
)
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
    next_stock_target_start,
    normalize_stock_target_horizon,
    stock_execution_window,
    time_in_force_for_checkpoint,
)
from ml.stock_trader.state import SchwabReadSession, capture_portfolio_state


DEFAULT_BROKER_STATE_RETRY_DELAY_SECONDS = 3.0
DEFAULT_BROKER_STATE_RETRY_MAX_SECONDS = 120.0
DEFAULT_BROKER_STATE_RETRY_MAX_ATTEMPTS = 60
DEFAULT_BROKER_STATE_EXECUTION_LEAD_SECONDS = 15.0


class SchwabTradingSession(SchwabReadSession, Protocol):
    def prepare_order_submission(self) -> object: ...

    def submit_prepared_order(
        self,
        order_payload: dict[str, object],
        context: object,
        *,
        before_post: Callable[[], None] | None = None,
    ) -> str | None: ...


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
    prediction_handoff_status: str | None = None
    target_window_start: str | None = None
    error: str | None = None
    broker_state_capture: Mapping[str, object] | None = None

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
            "prediction_handoff_status": self.prediction_handoff_status,
            "target_window_start": self.target_window_start,
            "error": self.error,
            "broker_state_capture": (
                dict(self.broker_state_capture)
                if self.broker_state_capture is not None
                else None
            ),
        }


class _BrokerStateCaptureFailure(Exception):
    def __init__(self, cause: Exception, metadata: Mapping[str, object]) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.metadata = dict(metadata)


class _SubmissionSafetyStop(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    allow_premarket_queue: bool = False,
    wait_for_prediction: bool = False,
    target_horizon: object | None = None,
    prediction_poll_seconds: float = DEFAULT_POLL_SECONDS,
    prediction_cutoff_lead_seconds: float = DEFAULT_CUTOFF_LEAD_SECONDS,
    prediction_maximum_target_lead_seconds: float = (
        DEFAULT_MAXIMUM_TARGET_LEAD_SECONDS
    ),
    broker_state_retry_delay_seconds: float = (
        DEFAULT_BROKER_STATE_RETRY_DELAY_SECONDS
    ),
    broker_state_retry_max_seconds: float = DEFAULT_BROKER_STATE_RETRY_MAX_SECONDS,
    broker_state_retry_max_attempts: int = DEFAULT_BROKER_STATE_RETRY_MAX_ATTEMPTS,
    broker_state_execution_lead_seconds: float = (
        DEFAULT_BROKER_STATE_EXECUTION_LEAD_SECONDS
    ),
    broker_state_retry_sleep: Callable[[float], None] = time.sleep,
    broker_state_retry_clock: Callable[[], float] = time.monotonic,
    runtime_clock: Callable[[], object] | None = None,
) -> StockTraderRunResult:
    """Run one hourly stock decision cycle.

    Real submission requires both a deployment-level ``execute=True`` and the
    operator text file set to TRUE.  The ordinary/default call is non-mutating.
    """

    root = Path(datastore_root).resolve()
    if allow_open_queue and allow_premarket_queue:
        raise ValueError(
            "allow_open_queue and allow_premarket_queue are mutually exclusive"
        )
    clean_target_horizon = normalize_stock_target_horizon(target_horizon)
    queue_requested = allow_open_queue or allow_premarket_queue
    if queue_requested and clean_target_horizon not in {None, "1h"}:
        raise ValueError("Opening queues require target_horizon='1h'")
    effective_target_horizon = "1h" if queue_requested else clean_target_horizon
    if execute and decided_at is not None and runtime_clock is None:
        raise ValueError(
            "decided_at cannot be used for live execution without an explicit "
            "runtime_clock; live mutation gates must use the current wall clock"
        )
    use_live_clock = decided_at is None
    timestamp = utc(decided_at)
    active_policy = policy or StockTraderPolicy()
    active_policy.validate()
    if broker_state_retry_delay_seconds < 0.0:
        raise ValueError("broker_state_retry_delay_seconds cannot be negative")
    if broker_state_retry_max_seconds < 0.0:
        raise ValueError("broker_state_retry_max_seconds cannot be negative")
    if broker_state_retry_max_attempts < 1:
        raise ValueError("broker_state_retry_max_attempts must be at least 1")
    if broker_state_execution_lead_seconds < 0.0:
        raise ValueError("broker_state_execution_lead_seconds cannot be negative")
    if prediction_maximum_target_lead_seconds <= 0.0:
        raise ValueError("prediction maximum target lead must be positive")
    activation = read_activation_intent(root)
    prediction_handoff: dict[str, object] = {}
    handoff_status: str | None = None
    target_window_start: str | None = None
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
            expected_target_window_start = None
            if queue_requested:
                queue_window = stock_execution_window(
                    timestamp,
                    allow_open_queue=allow_open_queue,
                    allow_premarket_queue=allow_premarket_queue,
                )
                expected_target_window_start = queue_window.queue_target_start
                if expected_target_window_start is None:
                    queue_name = (
                        "premarket-opening"
                        if allow_premarket_queue
                        else "regular-opening"
                    )
                    raise ValueError(
                        f"The {queue_name} queue is not open at {timestamp.isoformat()}"
                    )
            if wait_for_prediction:
                handoff = wait_for_actionable_prediction(
                    root,
                    started_at=timestamp,
                    expected_target_window_start=expected_target_window_start,
                    target_horizon=effective_target_horizon,
                    poll_seconds=prediction_poll_seconds,
                    cutoff_lead_seconds=prediction_cutoff_lead_seconds,
                    maximum_target_lead_seconds=(
                        prediction_maximum_target_lead_seconds
                    ),
                )
                timestamp = handoff.completed_at
                prediction_handoff = handoff.to_dict()
                prediction_handoff["target_horizon"] = effective_target_horizon
                handoff_status = handoff.status
                target_window_start = (
                    handoff.expected_target_window_start.isoformat()
                    if handoff.expected_target_window_start is not None
                    else None
                )
                signals = dict(handoff.signals)
                prediction_sources = handoff.source_files
                if not signals:
                    publication = publish_decision_run(
                        root,
                        (),
                        decided_at=timestamp,
                        activation=activation,
                        policy=active_policy,
                        execution_requested=execute,
                        source_files=(
                            prediction_sources or _prediction_pointer_sources(root)
                        ),
                        status=handoff.status,
                        prediction_handoff=prediction_handoff,
                    )
                    return StockTraderRunResult(
                        status=handoff.status,
                        run_directory=publication.run_directory,
                        selected_orders=0,
                        submitted_orders=0,
                        duplicate_suppressions=0,
                        stopped_after_error=False,
                        execution_requested=execute,
                        activation_active=True,
                        prediction_handoff_status=handoff.status,
                        target_window_start=target_window_start,
                        error=handoff.last_error,
                    )
                activation = read_activation_intent(root)
                if not activation.active:
                    status = "TRADER_INACTIVE_AFTER_PREDICTION_WAIT"
                    publication = publish_decision_run(
                        root,
                        (),
                        decided_at=timestamp,
                        activation=activation,
                        policy=active_policy,
                        execution_requested=execute,
                        source_files=prediction_sources,
                        status=status,
                        prediction_handoff=prediction_handoff,
                    )
                    return StockTraderRunResult(
                        status=status,
                        run_directory=publication.run_directory,
                        selected_orders=0,
                        submitted_orders=0,
                        duplicate_suppressions=0,
                        stopped_after_error=False,
                        execution_requested=execute,
                        activation_active=False,
                        prediction_handoff_status=handoff.status,
                        target_window_start=target_window_start,
                    )
            else:
                signals, prediction_sources = load_current_prediction_signals(
                    root,
                    as_of=timestamp,
                    target_horizon=effective_target_horizon,
                )
                if (
                    execute
                    or effective_target_horizon is not None
                    or expected_target_window_start is not None
                ):
                    (
                        signals,
                        prediction_handoff,
                        handoff_status,
                        target_window_start,
                    ) = _gate_direct_execution_signals(
                        root,
                        signals,
                        as_of=timestamp,
                        maximum_target_lead_seconds=(
                            prediction_maximum_target_lead_seconds
                        ),
                        target_horizon=effective_target_horizon,
                        expected_target_window_start=(
                            expected_target_window_start
                        ),
                    )
                    if not signals:
                        publication = publish_decision_run(
                            root,
                            (),
                            decided_at=timestamp,
                            activation=activation,
                            policy=active_policy,
                            execution_requested=True,
                            source_files=prediction_sources,
                            status=handoff_status,
                            prediction_handoff=prediction_handoff,
                        )
                        return StockTraderRunResult(
                            status=str(handoff_status),
                            run_directory=publication.run_directory,
                            selected_orders=0,
                            submitted_orders=0,
                            duplicate_suppressions=0,
                            stopped_after_error=False,
                            execution_requested=True,
                            activation_active=True,
                            prediction_handoff_status=handoff_status,
                            target_window_start=target_window_start,
                        )
            _signal_time_in_force(
                signals,
                as_of=timestamp,
                allow_open_queue=allow_open_queue,
                allow_premarket_queue=allow_premarket_queue,
            )
            actionable_until = _earliest_actionable_until(signals)
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
                prediction_handoff=prediction_handoff,
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
                prediction_handoff_status=handoff_status,
                target_window_start=target_window_start,
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
        timestamp = _runtime_now(
            timestamp,
            use_live_clock=use_live_clock,
            clock=runtime_clock,
        )
        if (
            actionable_until is not None
            and timestamp
            >= actionable_until
            - timedelta(seconds=broker_state_execution_lead_seconds)
        ):
            broker_state_capture = _broker_state_capture_metadata(
                status="NOT_STARTED_TARGET_TOO_CLOSE",
                attempts=0,
                transient_failures=0,
                retry_delay_seconds=broker_state_retry_delay_seconds,
                retry_wait_seconds=0.0,
                maximum_retry_seconds=0.0,
                elapsed_seconds=0.0,
                last_error_type=None,
            )
            publication = publish_decision_run(
                root,
                (),
                decided_at=timestamp,
                activation=activation,
                policy=active_policy,
                execution_requested=execute,
                source_files=(*prediction_sources, *model_sources),
                status="PREDICTION_EXECUTION_DEADLINE_PASSED",
                prediction_handoff=prediction_handoff,
                broker_state_capture=broker_state_capture,
            )
            return StockTraderRunResult(
                status="PREDICTION_EXECUTION_DEADLINE_PASSED",
                run_directory=publication.run_directory,
                selected_orders=0,
                submitted_orders=0,
                duplicate_suppressions=0,
                stopped_after_error=False,
                execution_requested=execute,
                activation_active=True,
                prediction_handoff_status=handoff_status,
                target_window_start=target_window_start,
                error="The prediction target was too close for a safe broker read and submission.",
                broker_state_capture=broker_state_capture,
            )
        broker = session or SchwabSession()
        maximum_retry_seconds = broker_state_retry_max_seconds
        if actionable_until is not None:
            remaining_safe_seconds = max(
                0.0,
                (
                    actionable_until
                    - timestamp
                    - timedelta(seconds=broker_state_execution_lead_seconds)
                ).total_seconds(),
            )
            maximum_retry_seconds = min(
                maximum_retry_seconds, remaining_safe_seconds
            )
        try:
            portfolio, timestamp, broker_state_capture = (
                _capture_portfolio_state_with_retry(
                    broker,
                    observed_at=timestamp,
                    parallel=parallel_state,
                    retry_delay_seconds=broker_state_retry_delay_seconds,
                    maximum_retry_seconds=maximum_retry_seconds,
                    maximum_attempts=broker_state_retry_max_attempts,
                    sleep=broker_state_retry_sleep,
                    monotonic=broker_state_retry_clock,
                )
            )
            timestamp = _runtime_now(
                timestamp,
                use_live_clock=use_live_clock,
                clock=runtime_clock,
            )
        except _BrokerStateCaptureFailure as failure:
            exc = failure.cause
            broker_state_capture = failure.metadata
            timestamp = timestamp + timedelta(
                seconds=float(broker_state_capture.get("elapsed_seconds", 0.0))
            )
            timestamp = _runtime_now(
                timestamp,
                use_live_clock=use_live_clock,
                clock=runtime_clock,
            )
            activation = read_activation_intent(root)
            if not activation.active:
                status = "TRADER_INACTIVE_DURING_BROKER_STATE_CAPTURE"
            elif (
                actionable_until is not None
                and timestamp
                >= actionable_until
                - timedelta(seconds=broker_state_execution_lead_seconds)
            ):
                status = "PREDICTION_EXECUTION_DEADLINE_PASSED"
            else:
                status = "BROKER_STATE_UNAVAILABLE"
            detail = f"{type(exc).__name__}: {exc}"
            publication = publish_decision_run(
                root,
                (),
                decided_at=timestamp,
                activation=activation,
                policy=active_policy,
                execution_requested=execute,
                source_files=(*prediction_sources, *model_sources),
                status=status,
                prediction_handoff=prediction_handoff,
                broker_state_capture=broker_state_capture,
            )
            return StockTraderRunResult(
                status=status,
                run_directory=publication.run_directory,
                selected_orders=0,
                submitted_orders=0,
                duplicate_suppressions=0,
                stopped_after_error=False,
                execution_requested=execute,
                activation_active=activation.active,
                prediction_handoff_status=handoff_status,
                target_window_start=target_window_start,
                error=detail,
                broker_state_capture=broker_state_capture,
            )
        activation = read_activation_intent(root)
        if not activation.active:
            status = "TRADER_INACTIVE_AFTER_BROKER_STATE_CAPTURE"
            publication = publish_decision_run(
                root,
                (),
                decided_at=timestamp,
                activation=activation,
                policy=active_policy,
                execution_requested=execute,
                source_files=(*prediction_sources, *model_sources),
                status=status,
                prediction_handoff=prediction_handoff,
                broker_state_capture=broker_state_capture,
            )
            return StockTraderRunResult(
                status=status,
                run_directory=publication.run_directory,
                selected_orders=0,
                submitted_orders=0,
                duplicate_suppressions=0,
                stopped_after_error=False,
                execution_requested=execute,
                activation_active=False,
                prediction_handoff_status=handoff_status,
                target_window_start=target_window_start,
                broker_state_capture=broker_state_capture,
            )
        if (
            actionable_until is not None
            and timestamp
            >= actionable_until
            - timedelta(seconds=broker_state_execution_lead_seconds)
        ):
            status = "PREDICTION_EXECUTION_DEADLINE_PASSED"
            publication = publish_decision_run(
                root,
                (),
                decided_at=timestamp,
                activation=activation,
                policy=active_policy,
                execution_requested=execute,
                source_files=(*prediction_sources, *model_sources),
                status=status,
                prediction_handoff=prediction_handoff,
                broker_state_capture=broker_state_capture,
            )
            return StockTraderRunResult(
                status=status,
                run_directory=publication.run_directory,
                selected_orders=0,
                submitted_orders=0,
                duplicate_suppressions=0,
                stopped_after_error=False,
                execution_requested=execute,
                activation_active=True,
                prediction_handoff_status=handoff_status,
                target_window_start=target_window_start,
                error="Broker-state retries reached the target safety boundary.",
                broker_state_capture=broker_state_capture,
            )
        execution_time_in_force = _signal_time_in_force(
            signals,
            as_of=timestamp,
            allow_open_queue=allow_open_queue,
            allow_premarket_queue=allow_premarket_queue,
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
            time_in_force=execution_time_in_force,
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
                time_in_force=execution_time_in_force,
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
            prediction_handoff=prediction_handoff,
            broker_state_capture=broker_state_capture,
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
                prediction_handoff_status=handoff_status,
                target_window_start=target_window_start,
                broker_state_capture=broker_state_capture,
            )
        execution_window = stock_execution_window(
            timestamp,
            allow_open_queue=allow_open_queue,
            allow_premarket_queue=allow_premarket_queue,
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
                prediction_handoff_status=handoff_status,
                target_window_start=target_window_start,
                error=execution_window.reason,
                broker_state_capture=broker_state_capture,
            )
        result = _submit_selected_orders(
            root,
            broker,
            decisions,
            publication,
            execution_window=execution_window,
            allow_open_queue=allow_open_queue,
            allow_premarket_queue=allow_premarket_queue,
            execution_lead_seconds=broker_state_execution_lead_seconds,
            clock=lambda: _runtime_now(
                timestamp,
                use_live_clock=use_live_clock,
                clock=runtime_clock,
            ),
        )
        return replace(
            result,
            prediction_handoff_status=handoff_status,
            target_window_start=target_window_start,
            broker_state_capture=broker_state_capture,
        )


def _submit_selected_orders(
    root: Path,
    broker: SchwabTradingSession,
    decisions: Sequence[TradeDecision],
    publication: DecisionPublication,
    *,
    execution_window: StockExecutionWindow,
    allow_open_queue: bool,
    allow_premarket_queue: bool,
    execution_lead_seconds: float,
    clock: Callable[[], object],
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
    safety_stop_reason: str | None = None
    submission_error: str | None = None
    for decision in selected:
        if decision.action not in {"BUY", "SELL"} or not isinstance(
            decision.order_payload, Mapping
        ):
            continue
        current_timestamp = utc(clock())
        safety_stop_reason = _submission_safety_reason(
            root,
            decision,
            as_of=current_timestamp,
            planned_window=execution_window,
            allow_open_queue=allow_open_queue,
            allow_premarket_queue=allow_premarket_queue,
            execution_lead_seconds=execution_lead_seconds,
        )
        if safety_stop_reason:
            break
        prepare_submission = getattr(broker, "prepare_order_submission", None)
        submit_prepared = getattr(broker, "submit_prepared_order", None)
        use_prepared_submission = callable(prepare_submission) and callable(
            submit_prepared
        )
        if not use_prepared_submission:
            safety_stop_reason = "BROKER_IDENTITY_BINDING_UNAVAILABLE"
            break
        prepared_context: object | None = None
        try:
            prepared_context = prepare_submission()
        except Exception as exc:
            submission_error = (
                f"{type(exc).__name__}: {exc} during order submission preflight"
            )
            stopped = True
            break
        safety_stop_reason = _submission_identity_safety_reason(
            decision,
            prepared_context,
        )
        if safety_stop_reason:
            break
        current_timestamp = utc(clock())
        safety_stop_reason = _submission_safety_reason(
            root,
            decision,
            as_of=current_timestamp,
            planned_window=execution_window,
            allow_open_queue=allow_open_queue,
            allow_premarket_queue=allow_premarket_queue,
            execution_lead_seconds=execution_lead_seconds,
        )
        if safety_stop_reason:
            break
        event = reserve_execution_intent(
            root,
            decision,
            submitted_at=current_timestamp,
            decision_publication=publication,
        )
        if event is None:
            duplicates += 1
            continue
        current_timestamp = utc(clock())
        safety_stop_reason = _submission_safety_reason(
            root,
            decision,
            as_of=current_timestamp,
            planned_window=execution_window,
            allow_open_queue=allow_open_queue,
            allow_premarket_queue=allow_premarket_queue,
            execution_lead_seconds=execution_lead_seconds,
        )
        if safety_stop_reason:
            record_execution_result(
                event,
                status="NOT_SUBMITTED_SAFETY_CHECK",
                completed_at=current_timestamp,
                error=safety_stop_reason,
            )
            break

        def enforce_immediate_submission_safety() -> None:
            identity_reason = _submission_identity_safety_reason(
                decision,
                prepared_context,
            )
            if identity_reason:
                raise _SubmissionSafetyStop(identity_reason)
            reason = _submission_safety_reason(
                root,
                decision,
                as_of=utc(clock()),
                planned_window=execution_window,
                allow_open_queue=allow_open_queue,
                allow_premarket_queue=allow_premarket_queue,
                execution_lead_seconds=execution_lead_seconds,
            )
            if reason:
                raise _SubmissionSafetyStop(reason)

        try:
            location = submit_prepared(
                dict(decision.order_payload),
                prepared_context,
                before_post=enforce_immediate_submission_safety,
            )
        except _SubmissionSafetyStop as exc:
            safety_stop_reason = exc.reason
            record_execution_result(
                event,
                status="NOT_SUBMITTED_SAFETY_CHECK",
                completed_at=utc(clock()),
                error=safety_stop_reason,
            )
            break
        except Exception as exc:
            record_execution_result(
                event,
                status="SUBMISSION_FAILED_OR_UNKNOWN",
                completed_at=utc(clock()),
                error=f"{type(exc).__name__}: {exc}",
            )
            stopped = True
            break
        record_execution_result(
            event,
            status="SUBMITTED",
            completed_at=utc(clock()),
            broker_location=location,
        )
        submitted += 1
    return StockTraderRunResult(
        status=(
            "SUBMISSION_STOPPED_AFTER_ERROR"
            if stopped
            else "SUBMISSION_STOPPED_SAFETY_CHECK"
            if safety_stop_reason
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
        activation_active=(
            safety_stop_reason != "OPERATOR_INTENT_NOT_ACTIVE_AT_SUBMISSION"
        ),
        error=safety_stop_reason or submission_error,
    )


def _submission_identity_safety_reason(
    decision: TradeDecision,
    prepared_context: object | None,
) -> str | None:
    expected = str(
        decision.portfolio.get("broker_identity_fingerprint") or ""
    ).strip()
    if not expected:
        return "BROKER_IDENTITY_BINDING_UNAVAILABLE"
    observed = str(
        getattr(prepared_context, "identity_fingerprint", "") or ""
    ).strip()
    if observed != expected:
        return "BROKER_ACCOUNT_IDENTITY_CHANGED_BEFORE_SUBMISSION"
    return None


def _gate_direct_execution_signals(
    root: Path,
    signals: Mapping[str, PredictionSignal],
    *,
    as_of: object,
    maximum_target_lead_seconds: float,
    target_horizon: object | None = None,
    expected_target_window_start: object | None = None,
) -> tuple[dict[str, PredictionSignal], dict[str, object], str, str | None]:
    """Apply handoff freshness/identity rules to non-waiting live calls."""

    timestamp = utc(as_of)
    clean_target_horizon = normalize_stock_target_horizon(target_horizon)
    target = (
        utc(expected_target_window_start)
        if expected_target_window_start is not None
        else next_stock_target_start(timestamp, horizon=clean_target_horizon)
    )
    if expected_target_window_start is not None:
        resolved_target = next_stock_target_start(
            target - timedelta(seconds=1),
            horizon=clean_target_horizon,
        )
        if resolved_target != target:
            label = clean_target_horizon or "1h/4h"
            raise ValueError(
                f"Expected target {target.isoformat()} is not a {label} checkpoint"
            )
    target_iso = target.isoformat() if target is not None else None
    matching: dict[str, PredictionSignal] = {}
    if target is not None:
        for symbol in STOCK_TRADER_SYMBOLS:
            signal = signals.get(symbol)
            if signal is None or signal.primary_horizon not in PRIMARY_STOCK_HORIZONS:
                continue
            if (
                clean_target_horizon is not None
                and signal.primary_horizon != clean_target_horizon
            ):
                continue
            try:
                signal_target = utc(signal.target_window_start)
                actionable_until = utc(signal.actionable_until)
            except (TypeError, ValueError):
                continue
            if (
                abs((signal_target - target).total_seconds()) < 1.0
                and actionable_until > timestamp
            ):
                matching[symbol] = signal

    consumed = consumed_live_prediction_ids(root)
    consumed_seen = {
        signal.prediction_id
        for signal in matching.values()
        if signal.prediction_id in consumed
    }
    available = {
        symbol: signal
        for symbol, signal in matching.items()
        if signal.prediction_id not in consumed
    }
    if target is None:
        status = "NO_UPCOMING_INTRADAY_TARGET"
        available = {}
    elif (target - timestamp).total_seconds() > maximum_target_lead_seconds:
        status = "NO_NEAR_TERM_INTRADAY_TARGET"
        available = {}
    elif available:
        status = "DIRECT_ACTIONABLE_RECEIPT_VALIDATED"
    elif consumed_seen:
        status = "PREDICTION_GENERATION_ALREADY_CONSUMED"
    else:
        status = "NO_MATCHING_ACTIONABLE_PREDICTION"

    metadata: dict[str, object] = {
        "schema_version": "stock-trader-direct-prediction-gate-v1",
        "status": status,
        "started_at": timestamp.isoformat(),
        "completed_at": timestamp.isoformat(),
        "expected_target_window_start": target_iso,
        "target_horizon": clean_target_horizon,
        "maximum_target_lead_seconds": float(maximum_target_lead_seconds),
        "selected_prediction_ids": [
            available[symbol].prediction_id
            for symbol in STOCK_TRADER_SYMBOLS
            if symbol in available
        ],
        "consumed_prediction_ids": sorted(consumed_seen),
        "missing_symbols": [
            symbol for symbol in STOCK_TRADER_SYMBOLS if symbol not in available
        ],
        "fallback_used": False,
    }
    return available, metadata, status, target_iso


def _capture_portfolio_state_with_retry(
    broker: SchwabReadSession,
    *,
    observed_at: object,
    parallel: bool,
    retry_delay_seconds: float,
    maximum_retry_seconds: float,
    maximum_attempts: int,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> tuple[PortfolioState, object, dict[str, object]]:
    """Retry only a complete read-only Schwab state snapshot.

    Every retry recaptures account, working orders, and quotes together.  Order
    submission is deliberately outside this helper because an ambiguous broker
    write must never be repeated automatically.
    """

    base_timestamp = utc(observed_at)
    started = float(monotonic())
    attempts = 0
    transient_failures = 0
    retry_wait_seconds = 0.0
    last_error_type: str | None = None
    last_error_operation: str | None = None

    while True:
        attempts += 1
        attempt_elapsed = max(0.0, float(monotonic()) - started)
        attempt_timestamp = base_timestamp + timedelta(seconds=attempt_elapsed)
        try:
            portfolio = capture_portfolio_state(
                broker,
                observed_at=attempt_timestamp,
                parallel=parallel,
            )
        except Exception as exc:
            elapsed = max(0.0, float(monotonic()) - started)
            retryable = is_retryable_schwab_error(exc)
            if retryable:
                transient_failures += 1
            last_error_type = type(exc).__name__
            last_error_operation = getattr(exc, "stock_trader_operation", None)
            metadata = _broker_state_capture_metadata(
                status=(
                    "UNAVAILABLE_AFTER_RETRIES"
                    if attempts > 1
                    else "UNAVAILABLE"
                ),
                attempts=attempts,
                transient_failures=transient_failures,
                retry_delay_seconds=retry_delay_seconds,
                retry_wait_seconds=retry_wait_seconds,
                maximum_retry_seconds=maximum_retry_seconds,
                elapsed_seconds=elapsed,
                last_error_type=last_error_type,
                last_error_operation=last_error_operation,
            )
            if (
                not retryable
                or attempts >= maximum_attempts
                or elapsed + retry_delay_seconds > maximum_retry_seconds
            ):
                raise _BrokerStateCaptureFailure(exc, metadata) from exc
            sleep(retry_delay_seconds)
            retry_wait_seconds += retry_delay_seconds
            continue

        elapsed = max(0.0, float(monotonic()) - started)
        completed_at = base_timestamp + timedelta(seconds=elapsed)
        metadata = _broker_state_capture_metadata(
            status="CURRENT_AFTER_RETRY" if transient_failures else "CURRENT",
            attempts=attempts,
            transient_failures=transient_failures,
            retry_delay_seconds=retry_delay_seconds,
            retry_wait_seconds=retry_wait_seconds,
            maximum_retry_seconds=maximum_retry_seconds,
            elapsed_seconds=elapsed,
            last_error_type=last_error_type,
            last_error_operation=last_error_operation,
        )
        return portfolio, completed_at, metadata


def _broker_state_capture_metadata(
    *,
    status: str,
    attempts: int,
    transient_failures: int,
    retry_delay_seconds: float,
    retry_wait_seconds: float,
    maximum_retry_seconds: float,
    elapsed_seconds: float,
    last_error_type: str | None,
    last_error_operation: str | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "stock-trader-broker-state-capture-v1",
        "status": status,
        "attempts": int(attempts),
        "transient_failures": int(transient_failures),
        "retry_delay_seconds": float(retry_delay_seconds),
        "retry_wait_seconds": float(retry_wait_seconds),
        "maximum_retry_seconds": float(maximum_retry_seconds),
        "elapsed_seconds": float(elapsed_seconds),
        "last_error_type": last_error_type,
        "last_error_operation": last_error_operation,
    }


def _runtime_now(
    fallback: object,
    *,
    use_live_clock: bool,
    clock: Callable[[], object] | None,
):
    if clock is not None:
        return utc(clock())
    if use_live_clock:
        return utc()
    return utc(fallback)


def _decision_is_actionable(
    decision: TradeDecision,
    *,
    as_of: object,
    execution_lead_seconds: float,
) -> bool:
    actionable_until = decision.prediction.get("actionable_until")
    if actionable_until is None:
        return False
    try:
        deadline = utc(actionable_until)
    except (TypeError, ValueError):
        return False
    return utc(as_of) < deadline - timedelta(seconds=execution_lead_seconds)


def _submission_safety_reason(
    root: Path,
    decision: TradeDecision,
    *,
    as_of: object,
    planned_window: StockExecutionWindow,
    allow_open_queue: bool,
    allow_premarket_queue: bool,
    execution_lead_seconds: float,
) -> str | None:
    if not read_activation_intent(root).active:
        return "OPERATOR_INTENT_NOT_ACTIVE_AT_SUBMISSION"
    if not _decision_is_actionable(
        decision,
        as_of=as_of,
        execution_lead_seconds=execution_lead_seconds,
    ):
        return "PREDICTION_TARGET_SAFETY_BOUNDARY_REACHED"
    current_window = stock_execution_window(
        as_of,
        allow_open_queue=allow_open_queue,
        allow_premarket_queue=allow_premarket_queue,
    )
    if not current_window.executable:
        return current_window.reason
    if current_window.checkpoint_session != planned_window.checkpoint_session:
        return "EXECUTION_SESSION_CHANGED_BEFORE_SUBMISSION"
    if not decision_targets_open(
        decision.prediction.get("target_window_start"), current_window
    ):
        return "QUEUE_TARGET_NO_LONGER_MATCHES_CURRENT_WINDOW"
    return None


def _earliest_actionable_until(signals: Mapping[str, object]):
    deadlines = [
        utc(getattr(signal, "actionable_until"))
        for signal in signals.values()
        if getattr(signal, "actionable_until", None) is not None
    ]
    return min(deadlines) if deadlines else None


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


def _signal_time_in_force(
    signals: Mapping[str, object],
    *,
    as_of: object,
    allow_open_queue: bool,
    allow_premarket_queue: bool,
) -> str:
    checkpoint_sessions = {
        str(getattr(signal, "checkpoint_session", "") or "").strip().upper()
        for signal in signals.values()
    }
    checkpoint_sessions.discard("")
    if not checkpoint_sessions:
        return "DAY"
    if len(checkpoint_sessions) != 1:
        raise ValueError(
            "Actionable stock signals disagree on checkpoint session: "
            + ", ".join(sorted(checkpoint_sessions))
        )
    current = stock_execution_window(
        as_of,
        allow_open_queue=allow_open_queue,
        allow_premarket_queue=allow_premarket_queue,
    ).checkpoint_session
    return time_in_force_for_checkpoint(
        next(iter(checkpoint_sessions)),
        current_checkpoint_session=current,
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
        "--queue-at-premarket-open",
        action="store_true",
        help=(
            "Permit AM limit orders before the Schwab PRE open only when "
            "their prediction target starts at that 07:00 Eastern boundary."
        ),
    )
    parser.add_argument(
        "--wait-for-actionable-prediction",
        action="store_true",
        help=(
            "Wait for an unconsumed checksum-verified 1h/4h Loop B receipt for "
            "the next target, then execute before its bounded cutoff."
        ),
    )
    parser.add_argument(
        "--target-horizon",
        choices=PRIMARY_STOCK_HORIZONS,
        help=(
            "Constrain target resolution and prediction selection to exactly "
            "one scheduled Loop B horizon. Opening queues always use 1h."
        ),
    )
    parser.add_argument(
        "--prediction-poll-seconds",
        type=float,
        default=DEFAULT_POLL_SECONDS,
    )
    parser.add_argument(
        "--prediction-cutoff-lead-seconds",
        type=float,
        default=DEFAULT_CUTOFF_LEAD_SECONDS,
    )
    parser.add_argument(
        "--prediction-maximum-target-lead-seconds",
        type=float,
        default=DEFAULT_MAXIMUM_TARGET_LEAD_SECONDS,
    )
    parser.add_argument(
        "--no-shadow-observe",
        action="store_true",
        help="Disable the paired shadow challenger for this invocation.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.queue_at_open and args.queue_at_premarket_open:
        raise SystemExit(
            "--queue-at-open and --queue-at-premarket-open are mutually exclusive"
        )
    if (
        (args.queue_at_open or args.queue_at_premarket_open)
        and args.target_horizon not in {None, "1h"}
    ):
        raise SystemExit("Opening queues require --target-horizon 1h")
    if args.wait_for_actionable_prediction and args.decided_at:
        raise SystemExit(
            "--decided-at cannot be combined with --wait-for-actionable-prediction"
        )
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
            allow_premarket_queue=bool(args.queue_at_premarket_open),
            wait_for_prediction=bool(args.wait_for_actionable_prediction),
            target_horizon=args.target_horizon,
            prediction_poll_seconds=args.prediction_poll_seconds,
            prediction_cutoff_lead_seconds=args.prediction_cutoff_lead_seconds,
            prediction_maximum_target_lead_seconds=(
                args.prediction_maximum_target_lead_seconds
            ),
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
            "PREDICTION_DEADLINE_EXPIRED",
            "PREDICTION_EXECUTION_DEADLINE_PASSED",
            "BROKER_STATE_UNAVAILABLE",
            "SUBMISSION_STOPPED_AFTER_ERROR",
            "SUBMISSION_STOPPED_SAFETY_CHECK",
        }
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["StockTraderRunResult", "main", "run_stock_trader_once"]
