"""One combined decision batch for independently owned stock horizons."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import pandas as pd

from app.services.schwab import SchwabSession
from app.services.schwab_stock_orders import build_schwab_stock_order_payload
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.stock_trader.contracts import PredictionSignal, QuoteState, StockTraderPolicy, canonical_sha256, utc
from ml.stock_trader.engine import _direct_no_trade
from ml.stock_trader.gameplan import _entry_deadline, read_gameplan_stock_activation_intent
from ml.stock_trader.gameplan_execution import GameplanDeploymentUnavailable, _assert_execution_deployment
from ml.stock_trader.horizon_ledger import HorizonLedger, PortfolioEvidence
from ml.stock_trader.independent_engine import build_independent_trade_decisions
from ml.stock_trader.independent_signals import load_current_independent_gameplan_signals
from ml.stock_trader.model import enrichment_signal_readiness, load_current_enrichment_model
from ml.stock_trader.publication import publish_decision_run, reserve_execution_intent, record_execution_result
from ml.stock_trader.runtime import (
    DEFAULT_BROKER_STATE_EXECUTION_LEAD_SECONDS,
    DEFAULT_BROKER_STATE_RETRY_DELAY_SECONDS,
    DEFAULT_BROKER_STATE_RETRY_MAX_ATTEMPTS,
    DEFAULT_BROKER_STATE_RETRY_MAX_SECONDS,
    StockTraderRunResult, _BrokerStateCaptureFailure,
    _capture_portfolio_state_with_retry,
    _submission_identity_safety_reason, _submission_safety_reason,
)
from ml.stock_trader.session import stock_execution_window
from ml.stock_trader.state import capture_portfolio_state
from ml.stock_trader.sizing_policy import LEARNED_SIZING_POLICY, FIXED_SIZING_POLICY, GAMEPLAN_SIZING_POLICY, validate_sizing_policy
from ml.stock_direction_policy import stock_direction

LEDGER_RELATIVE_PATH = Path("state/independent-stock-trader/holdings.sqlite3")
CLOSE_EXIT_LEAD_SECONDS = 60
MAXIMUM_QUOTE_CLOCK_WAIT_SECONDS = 5.


def _wait_for_current_quote_timestamps(portfolio, *, clock, sleep, deadline):
    """Let a recently delivered quote reach local time without relabeling it.

    A slow host clock can otherwise reject each newly captured BBO forever.
    Waiting is bounded and does not make a future, old, or malformed quote
    valid: the existing decision and submission gates still check its actual
    timestamp. Neither the portfolio timestamp nor any deadline is advanced.
    """
    now = utc(clock())
    future = []
    for quote in portfolio.quotes.values():
        if not quote.observed_at:
            continue
        try:
            observed = utc(quote.observed_at)
            if observed > now:
                future.append(observed)
        except (ValueError, TypeError):
            continue
    result = {"status": "NOT_NEEDED", "requested_wait_seconds": 0.,
              "maximum_wait_seconds": MAXIMUM_QUOTE_CLOCK_WAIT_SECONDS}
    if not future:
        return result
    latest = max(future)
    wait = (latest - now).total_seconds() + .001
    budget = min(MAXIMUM_QUOTE_CLOCK_WAIT_SECONDS, max(0.,
        (utc(deadline) - now).total_seconds() - DEFAULT_BROKER_STATE_EXECUTION_LEAD_SECONDS))
    result.update(latest_quote_at=latest.isoformat(), ahead_seconds=(latest - now).total_seconds())
    if wait > budget:
        return {**result, "status": "OUTSIDE_WAIT_BUDGET"}
    sleep(wait)
    return {**result, "status": "CURRENT_AFTER_WAIT" if utc(clock()) >= latest else "STILL_FUTURE",
            "requested_wait_seconds": wait}


def _has_inventory(path: Path) -> bool:
    if not path.exists():
        return False
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return db.execute("SELECT 1 FROM allocations WHERE status='ACTIVE' LIMIT 1").fetchone() is not None


def _claim_entry_slot(root: Path, timestamp) -> bool:
    """One entry batch for all horizons per action hour, including restarts.

    A failed attempt stays consumed. Exits can still run on later wakes; they
    never convert a missed entry into a later opportunity.
    """
    key = utc(timestamp).floor("h").strftime("%Y%m%dT%H0000Z")
    path = root / "state/independent-stock-trader/entry-slots" / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"action_slot": key, "claimed_at": utc(timestamp).isoformat()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return True


def _loaded_gameplan_run(root: Path, signals, sources, *, execution_ready_plan: bool) -> Path | None:
    """Retain the loaded source identity; never infer it from a newer pointer."""
    run_root = (root / "ml/nightly-gameplan-runs").resolve()
    runs = {Path(source).resolve().parent for source in sources
            if Path(source).name == "receipt.json" and Path(source).resolve().parent.parent == run_root}
    if len(runs) > 1:
        raise ValueError("Loaded stock signals refer to multiple Gameplan publications")
    if runs:
        return next(iter(runs))
    if execution_ready_plan and signals:
        fingerprints = {signal.source_fingerprint for signal in signals.values()}
        if len(fingerprints) != 1:
            raise ValueError("Loaded stock instructions refer to multiple Gameplan publications")
        run = (run_root / next(iter(fingerprints))).resolve()
        if run.parent != run_root:
            raise ValueError("Loaded Gameplan identity escapes its saved directory")
        return run
    return None


def run_independent_stock_trader_once(
    datastore_root: Path, *, decided_at=None, execute: bool = False,
    session=None, policy: StockTraderPolicy | None = None,
    runtime_clock: Callable[[], object] | None = None, entries: bool = True,
    session_managed: bool = False,
    sizing_policy: str = LEARNED_SIZING_POLICY,
    late_opening_date: str | None = None,
    resume_quote_run: str | None = None,
    resume_quote_symbol: str | None = None,
    broker_state_retry_delay_seconds: float = DEFAULT_BROKER_STATE_RETRY_DELAY_SECONDS,
    broker_state_retry_max_seconds: float = DEFAULT_BROKER_STATE_RETRY_MAX_SECONDS,
    broker_state_retry_max_attempts: int = DEFAULT_BROKER_STATE_RETRY_MAX_ATTEMPTS,
    broker_state_retry_sleep: Callable[[float], None] = time.sleep,
    broker_state_retry_clock: Callable[[], float] = time.monotonic,
) -> StockTraderRunResult:
    """Consume exact independent forecasts and manage only their owned shares.

    Both stock controls, current broker cash/shares, and durable reservations
    apply. Gameplan execution uses saved instructions directly; learned and
    fixed strategies retain their model qualification. Exits remain available
    when a newer plan is unavailable because they derive from filled holdings.
    Live entries additionally require the bounded session worker so opening a
    longer holding cannot silently omit its subsequent exit management.
    """
    if execute and decided_at is not None and runtime_clock is None:
        raise ValueError("Historical clocks cannot be used for live execution")
    if not isinstance(session_managed, bool):
        raise TypeError("session_managed must be an explicit boolean")
    if broker_state_retry_delay_seconds < 0 or broker_state_retry_max_seconds < 0:
        raise ValueError("Broker read retry delays and budgets cannot be negative")
    if broker_state_retry_max_attempts < 1:
        raise ValueError("broker_state_retry_max_attempts must be at least 1")
    root = Path(datastore_root).resolve()
    sizing_policy = validate_sizing_policy(sizing_policy)
    clock = runtime_clock or (lambda: utc() if decided_at is None else utc(decided_at))
    timestamp = utc(clock())
    if resume_quote_run is not None or resume_quote_symbol is not None:
        if not (resume_quote_run and resume_quote_symbol and session_managed and sizing_policy == GAMEPLAN_SIZING_POLICY) or late_opening_date:
            raise ValueError("Quote recovery requires a run, symbol and the managed Gameplan session")
    if late_opening_date is not None:
        from ml.stock_trader.gameplan import validate_late_opening_date
        if sizing_policy != GAMEPLAN_SIZING_POLICY or not session_managed:
            raise ValueError('Late opening requires the explicitly selected managed Gameplan policy')
        validate_late_opening_date(late_opening_date, timestamp)
    active_policy = policy or StockTraderPolicy()
    if policy is None and sizing_policy == GAMEPLAN_SIZING_POLICY:
        # One exit and one directional decision per configured symbol/horizon
        # fits in this opt-in batch. The legacy six-order strategy is unchanged.
        from ml.stock_trader.contracts import STOCK_TRADER_SYMBOLS
        active_policy = replace(active_policy, policy_version=GAMEPLAN_SIZING_POLICY,
                                maximum_orders_per_wake=len(STOCK_TRADER_SYMBOLS) * 4 * 2)
    active_policy.validate()
    activation = read_gameplan_stock_activation_intent(root)
    sources = ()
    broker_state_capture = None
    metadata = {"mode": "INDEPENDENT_STOCK_HORIZONS", "allocation_weights": {"1h": 1, "4h": 2, "1d": 3, "1w": 4},
                "session_managed": session_managed, "sizing_policy": sizing_policy}
    entry_allowed = entries and (not execute or session_managed)
    if entries and execute and not session_managed:
        metadata["entry_management_status"] = "ENTRY_REQUIRES_SESSION_MANAGER"

    def finish(status, *, decisions=(), error=None, submitted=0, duplicates=0, publication=None):
        output = publication or publish_decision_run(
            root, decisions, decided_at=utc(clock()), activation=activation,
            policy=active_policy, execution_requested=execute, source_files=sources,
            status=status, prediction_handoff=metadata, broker_state_capture=broker_state_capture,
        )
        return StockTraderRunResult(
            status, output.run_directory, sum(d.quantity > 0 for d in decisions),
            submitted, duplicates, status.startswith("SUBMISSION_STOPPED"),
            execute, activation.active, error=error, broker_state_capture=broker_state_capture,
        )

    with exclusive_runtime_lock(root / "locks/stock-trader-hourly.lock", process_name="stock-trader-hourly"):
        if not activation.active:
            return finish("TRADER_INACTIVE")
        window = stock_execution_window(timestamp)
        if not window.executable:
            return finish("EXECUTION_WINDOW_CLOSED", error=window.reason)
        signals = {}
        source_gameplan_run = None
        try:
            if entry_allowed:
                signal_options = ({"execution_ready_plan": True} if sizing_policy == GAMEPLAN_SIZING_POLICY else
                                  {"require_promoted_model_reports": True} if sizing_policy == FIXED_SIZING_POLICY else {})
                if late_opening_date is not None:
                    signal_options['late_opening_date'] = late_opening_date
                    metadata['late_opening_date'] = late_opening_date
                if resume_quote_run:
                    from ml.stock_trader.quote_recovery import load_quote_recovery
                    signals, sources, metadata["quote_recovery"] = load_quote_recovery(
                        root, resume_quote_run, resume_quote_symbol, as_of=timestamp)
                else:
                    signals, sources = load_current_independent_gameplan_signals(root, as_of=timestamp, **signal_options)
                if signals:
                    source_gameplan_run = _loaded_gameplan_run(root, signals, sources,
                        execution_ready_plan=sizing_policy == GAMEPLAN_SIZING_POLICY and not resume_quote_run)
                    _assert_execution_deployment(root, source_gameplan_run,
                        action_date=timestamp.tz_convert("America/Los_Angeles").date().isoformat())
                    metadata["source_gameplan_run"] = (source_gameplan_run.relative_to(root).as_posix()
                                                       if source_gameplan_run is not None else None)
        except (OSError, ValueError, RuntimeError) as exc:
            signals = {}
            metadata["entry_input_error"] = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, GameplanDeploymentUnavailable):
                metadata["gameplan_deployment_error"] = str(exc)
        try:
            model = load_current_enrichment_model(root) if sizing_policy == LEARNED_SIZING_POLICY else None
        except (OSError, ValueError, RuntimeError) as exc:
            model = None
            metadata["enrichment_error"] = f"{type(exc).__name__}: {exc}"
        unsupported = {
            f"{symbol}/{horizon}": enrichment_signal_readiness(model, signal)
            for (symbol, horizon), signal in signals.items() if model is not None
        }
        qualified = (dict(signals) if sizing_policy in {FIXED_SIZING_POLICY, GAMEPLAN_SIZING_POLICY} else
                     {key: signal for key, signal in signals.items()
                      if unsupported.get(f"{key[0]}/{key[1]}", {}).get("status") == "READY"})
        metadata["directional_signals"] = len(signals)
        metadata["qualified_entry_signals"] = len(qualified)
        metadata["enrichment_readiness"] = unsupported
        ledger_path = root / LEDGER_RELATIVE_PATH
        if not qualified and not _has_inventory(ledger_path):
            if metadata.get("entry_management_status"):
                return finish("ENTRY_REQUIRES_SESSION_MANAGER",
                              error="Live independent stock entries require the bounded session worker; one-shot calls can manage existing owned exits.")
            if metadata.get("entry_input_error"):
                return finish("INDEPENDENT_TARGET_PLAN_UNAVAILABLE", error=metadata["entry_input_error"])
            return finish("NO_DIRECTIONAL_STOCK_ENTRY_SIGNAL" if sizing_policy in {FIXED_SIZING_POLICY, GAMEPLAN_SIZING_POLICY}
                          else "NO_QUALIFIED_INDEPENDENT_STOCK_ENTRIES")
        if execute and qualified and (resume_quote_run or sizing_policy != GAMEPLAN_SIZING_POLICY):
            if resume_quote_run:
                from ml.stock_trader.quote_recovery import claim_quote_recovery
                claimed = claim_quote_recovery(root, metadata["quote_recovery"], as_of=timestamp)
            else:
                claimed = _claim_entry_slot(root, timestamp)
            if not claimed:
                qualified = {}
                metadata["entry_slot_already_consumed"] = True
        broker = session or SchwabSession()
        from ml.stock_trader.horizon_broker import capture_order_evidence
        stable_identity = None
        ledger = None
        evidence = ()
        capture_started = utc(clock())
        local_day = capture_started.tz_convert("America/Los_Angeles").normalize()
        read_deadline = local_day + pd.Timedelta(hours=17)
        if qualified:
            read_deadline = min(read_deadline, *(utc(signal.actionable_until) for signal in qualified.values()))
        maximum_retry_seconds = min(broker_state_retry_max_seconds, max(0.,
            (read_deadline - capture_started).total_seconds() - DEFAULT_BROKER_STATE_EXECUTION_LEAD_SECONDS))

        def capture_snapshot(_attempt_timestamp):
            nonlocal stable_identity, ledger, evidence
            if not read_gameplan_stock_activation_intent(root).active:
                raise ValueError("TRADER_INACTIVE_DURING_BROKER_CAPTURE")
            now = utc(clock())
            if now >= read_deadline or not stock_execution_window(now).executable:
                raise ValueError("BROKER_READ_EXECUTION_WINDOW_CLOSED")
            identity = broker.stable_account_fingerprint()
            if stable_identity is not None and identity != stable_identity:
                raise ValueError("STOCK_ACCOUNT_CHANGED_DURING_CAPTURE")
            stable_identity = identity
            if ledger is None:
                ledger = HorizonLedger(ledger_path, stable_identity)
            # Every retry must observe fills before capturing a newer complete
            # portfolio. No reconciliation or broker write belongs in this loop.
            current_evidence = capture_order_evidence(broker, ledger, account_fingerprint=stable_identity,
                as_of=utc(clock()), observation_clock=clock)
            current_portfolio = capture_portfolio_state(broker, observed_at=utc(clock()), parallel=True,
                **({"literal_cash_only": True, "use_actual_quote_timestamps": True}
                   if sizing_policy == GAMEPLAN_SIZING_POLICY else {}))
            if sizing_policy == GAMEPLAN_SIZING_POLICY:
                metadata["quote_timestamp_wait"] = _wait_for_current_quote_timestamps(
                    current_portfolio, clock=clock, sleep=broker_state_retry_sleep, deadline=read_deadline)
                if not read_gameplan_stock_activation_intent(root).active:
                    raise ValueError("TRADER_INACTIVE_DURING_BROKER_CAPTURE")
            if stable_identity != broker.stable_account_fingerprint():
                raise ValueError("STOCK_ACCOUNT_CHANGED_DURING_CAPTURE")
            broker.verify_read_snapshot(current_portfolio.broker_identity_fingerprint)
            if (utc(clock()) - utc(current_portfolio.observed_at)).total_seconds() > ledger.maximum_evidence_age_seconds:
                raise ValueError("BROKER_PORTFOLIO_CAPTURE_TOO_OLD")
            evidence = current_evidence
            return current_portfolio

        try:
            def quote_refresh_targets(current):
                from ml.stock_trader.gameplan_direction_engine import _current_price
                now = utc(clock())
                # Do not delay other stocks for bearish signals with no shares.
                required = {signal.symbol: "BUY" if stock_direction(signal.calibrated_probability) == "BULLISH" else "SELL"
                            for signal in qualified.values()
                            if ((stock_direction(signal.calibrated_probability) == "BULLISH" and current.available_cash > 0)
                                or (stock_direction(signal.calibrated_probability) == "BEARISH"
                                    and current.held_shares.get(signal.symbol, 0) > current.pending_sell_shares.get(signal.symbol, 0)))}
                for allocation in (() if sizing_policy == GAMEPLAN_SIZING_POLICY else ledger.snapshot().allocations):
                    if allocation.status == "ACTIVE" and allocation.filled_shares > 0 and utc(allocation.target_end) <= now + pd.Timedelta(seconds=CLOSE_EXIT_LEAD_SECONDS):
                        required[allocation.symbol] = "SELL"
                return tuple(sorted(symbol for symbol, action in required.items()
                    if _current_price(symbol, current, active_policy, now, action, stock_execution_window(now).time_in_force,
                                      ledger.maximum_evidence_age_seconds)[1] is not None))

            portfolio, _, broker_state_capture = _capture_portfolio_state_with_retry(
                broker, observed_at=capture_started, parallel=True,
                retry_delay_seconds=broker_state_retry_delay_seconds,
                maximum_retry_seconds=maximum_retry_seconds,
                maximum_attempts=broker_state_retry_max_attempts,
                sleep=broker_state_retry_sleep, monotonic=broker_state_retry_clock,
                capture_snapshot=capture_snapshot,
                **({"refresh_snapshot": quote_refresh_targets} if sizing_policy == GAMEPLAN_SIZING_POLICY else {}),
            )
            timestamp = utc(clock())
            snapshot_id = canonical_sha256([stable_identity, timestamp.isoformat(), portfolio.source_fingerprint])
            reconciliation = ledger.reconcile(PortfolioEvidence(
                snapshot_id, stable_identity, portfolio.observed_at, portfolio.held_shares,
                {s: q.ask for s, q in portfolio.quotes.items()},
                {s: portfolio.account_equity * active_policy.maximum_symbol_equity_fraction for s in portfolio.held_shares},
                portfolio.source_fingerprint,
            ), order_evidence=evidence)
        except _BrokerStateCaptureFailure as failure:
            broker_state_capture = failure.metadata
            return finish("HORIZON_BROKER_RECONCILIATION_UNAVAILABLE",
                          error=f"{type(failure.cause).__name__}: {failure.cause}")
        except Exception as exc:
            return finish("HORIZON_BROKER_RECONCILIATION_UNAVAILABLE", error=f"{type(exc).__name__}: {exc}")
        metadata["inventory_reconciliation"] = {"ready": reconciliation.ready, "reasons": list(reconciliation.reasons)}
        activation = read_gameplan_stock_activation_intent(root)
        if not activation.active:
            return finish("TRADER_INACTIVE_AFTER_BROKER_CAPTURE")
        window = stock_execution_window(timestamp)
        if not window.executable:
            return finish("EXECUTION_WINDOW_CLOSED_AFTER_BROKER_CAPTURE", error=window.reason)
        state = ledger.snapshot()
        if qualified:
            try:
                _assert_execution_deployment(root, source_gameplan_run,
                    action_date=timestamp.tz_convert("America/Los_Angeles").date().isoformat())
            except (OSError, ValueError, RuntimeError) as exc:
                qualified = {}
                metadata["entry_input_error"] = f"{type(exc).__name__}: {exc}"
                metadata["gameplan_deployment_error"] = str(exc)
        if sizing_policy == GAMEPLAN_SIZING_POLICY:
            # A read failure or quote skip does not consume a forecast. Existing
            # allocations, including completed ones, suppress its resubmission.
            recorded = {allocation.forecast_id for allocation in state.allocations}
            recorded.update(reservation.forecast_id for reservation in state.reservations)
            qualified = {key: signal for key, signal in qualified.items() if signal.prediction_id not in recorded}
        if late_opening_date is not None and utc(clock()).tz_convert('America/Los_Angeles').hour != 4:
            qualified = {}
            late_opening_date = None
            metadata['late_opening_expired_during_capture'] = True
        if execute:
            cancelled, cancellation_error = _cancel_expired_entries(
                root, broker, ledger, state, portfolio, stable_identity, clock,
                gameplan_entries=sizing_policy == GAMEPLAN_SIZING_POLICY,
                **({'late_opening_date':late_opening_date} if late_opening_date is not None else {}),
            )
            if cancelled or cancellation_error:
                metadata["entry_cancellations_requested"] = cancelled
                return finish("ENTRY_CANCELLATION_AWAITING_RECONCILIATION", error=cancellation_error)
        # Manual Gameplan holdings persist across forecast boundaries. Only a
        # new bearish instruction sells from that horizon's inventory.
        exits = (() if sizing_policy == GAMEPLAN_SIZING_POLICY else
                 _exit_decisions(ledger, portfolio, activation, active_policy, timestamp, snapshot_id, window.time_in_force))
        if sizing_policy == GAMEPLAN_SIZING_POLICY:
            metadata["holding_policy"] = "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1"
        decision_options = dict(
            active_allocations=frozenset((a.symbol, a.horizon) for a in state.allocations if a.status == "ACTIVE"),
            ledger_ready=reconciliation.ready, decided_at=timestamp, policy=active_policy,
            time_in_force=window.time_in_force, exit_decisions=exits,
        )
        if sizing_policy == GAMEPLAN_SIZING_POLICY:
            from ml.stock_trader.gameplan_direction_engine import build_gameplan_direction_trade_decisions
            capacities = _direction_sell_capacities(qualified, state, portfolio, exits)
            metadata["planning_ranges_have_execution_authority"] = False
            metadata["direction_sell_capacities"] = {f"{s}/{h}": q for (s, h), q in capacities.items()}
            metadata["pricing_basis"] = "current_ask_buy_bid_sell_with_wide_spread_midpoint_v1"
            decisions = build_gameplan_direction_trade_decisions(
                qualified, portfolio, activation, verified_promoted_signals=frozenset(qualified),
                bearish_sell_capacities=capacities, maximum_quote_age_seconds=ledger.maximum_evidence_age_seconds,
                **decision_options, **({'late_opening_date':late_opening_date} if late_opening_date is not None else {}),
                **({"recovered_forecast_ids":frozenset(s.prediction_id for s in qualified.values())} if resume_quote_run else {}))
        elif sizing_policy == FIXED_SIZING_POLICY:
            from ml.stock_trader.fixed_horizon_engine import build_fixed_horizon_trade_decisions
            decisions = build_fixed_horizon_trade_decisions(
                qualified, portfolio, activation, verified_promoted_signals=frozenset(qualified), **decision_options)
        else:
            decisions = build_independent_trade_decisions(qualified, portfolio, model, activation, **decision_options)
        blocked_exit_quotes = [d.symbol for d in decisions
            if d.prediction.get("position_purpose") == "EXIT" and (d.hypothetical_quantity or 0) > 0
            and d.quantity == 0 and d.decision_reason_code in {"USABLE_QUOTE_UNAVAILABLE", "CURRENT_QUOTE_TOO_OLD"}]
        if blocked_exit_quotes:
            metadata["blocked_owned_exit_quotes"] = sorted(set(blocked_exit_quotes))
        if sizing_policy == GAMEPLAN_SIZING_POLICY:
            from ml.stock_trader.price_comparison import attach_price_comparisons
            decisions = attach_price_comparisons(root, decisions)
        publication = publish_decision_run(
            root, decisions, decided_at=timestamp, activation=activation, policy=active_policy,
            execution_requested=execute, source_files=sources, prediction_handoff=metadata,
            broker_state_capture=broker_state_capture,
        )
        if not execute:
            return finish("DRY_RUN_INDEPENDENT_STOCK_DECISIONS", decisions=decisions, publication=publication)
        result = _submit_batch(root, broker, ledger, decisions, publication, window, snapshot_id, stable_identity, clock, finish,
                               source_gameplan_run=source_gameplan_run)
        if metadata.get("gameplan_deployment_error") and not result.error:
            return replace(result,
                status="OWNED_EXITS_SUBMITTED_WITH_ENTRY_PLAN_UNAVAILABLE" if result.submitted_orders else "INDEPENDENT_TARGET_PLAN_UNAVAILABLE",
                error=metadata["gameplan_deployment_error"])
        if blocked_exit_quotes and not result.error:
            return replace(result, status="HORIZON_EXIT_QUOTE_UNAVAILABLE",
                           error="Due owned exit requires a current quote: " + ", ".join(sorted(set(blocked_exit_quotes))))
        blocked_entries = sorted({d.symbol for d in decisions if d.quantity == 0
            and d.decision_reason_code in {"USABLE_QUOTE_UNAVAILABLE", "CURRENT_QUOTE_TOO_OLD", "REALTIME_QUOTE_UNAVAILABLE"}
            and ((d.suggested_action == "BUY" and portfolio.available_cash > 0)
                 or (d.suggested_action == "SELL" and portfolio.held_shares.get(d.symbol, 0) > portfolio.pending_sell_shares.get(d.symbol, 0)))})
        if sizing_policy == GAMEPLAN_SIZING_POLICY and blocked_entries and not result.error:
            return replace(result, status="ORDERS_SUBMITTED_WITH_QUOTE_UNAVAILABLE" if result.submitted_orders else "HORIZON_ENTRY_QUOTE_UNAVAILABLE",
                           error="Scheduled signals still lack usable quotes after bounded live refresh: " + ", ".join(blocked_entries))
        return result


def _direction_sell_capacities(signals, state, portfolio, exits):
    """Partition current eligible shares once; never use projected inventory."""
    from ml.stock_trader.fixed_horizon_budget import FIXED_HORIZON_WEIGHTS
    allocations = {(a.symbol, a.horizon): a for a in state.allocations if a.status == "ACTIVE"}
    due_ids = {d.prediction.get("allocation_id") for d in exits}
    free = {}
    for symbol, held in portfolio.held_shares.items():
        owned = sum(a.filled_shares for a in state.allocations if a.symbol == symbol)
        reserved = sum(a.reserved_sell_shares for a in state.allocations if a.symbol == symbol)
        external_pending = max(0, portfolio.pending_sell_shares.get(symbol, 0) - reserved)
        free[symbol] = max(0, int(held - owned - external_pending))
    capacities = {}
    for key in sorted(signals, key=lambda item: (FIXED_HORIZON_WEIGHTS[item[1]], item[0])):
        if stock_direction(signals[key].calibrated_probability) != "BEARISH":
            continue
        symbol, _ = key
        allocation = allocations.get(key)
        if allocation and (allocation.reserved_buy_shares or allocation.allocation_id in due_ids):
            capacities[key] = 0
            continue
        own = max(0, allocation.filled_shares - allocation.reserved_sell_shares) if allocation else 0
        capacities[key] = int(own + free.get(symbol, 0))
        free[symbol] = 0
    return capacities


def _exit_decisions(ledger, portfolio, activation, policy, timestamp, snapshot_id, time_in_force, *, gameplan_pricing=False):
    decisions = []
    state = ledger.snapshot()
    for plan in ledger.due_exits(as_of=timestamp.isoformat(), snapshot_id=snapshot_id,
                                 exit_lead_seconds=CLOSE_EXIT_LEAD_SECONDS):
        quote = portfolio.quotes.get(plan.symbol)
        attempts = sum(r.allocation_id == plan.allocation_id and r.side == "SELL" for r in state.reservations)
        # A terminal partial/cancelled exit can leave a new residual to close.
        # An uncertain/working exit retains its shares and cannot be retried.
        exit_id = canonical_sha256(["horizon-exit", plan.allocation_id, attempts])
        deadline = timestamp + pd.Timedelta(seconds=60)
        local = timestamp.tz_convert("America/Los_Angeles")
        close = local.normalize() + pd.Timedelta(hours=17)
        deadline = min(deadline, close.tz_convert("UTC"))
        signal = PredictionSignal(
            plan.symbol, plan.horizon, exit_id, timestamp.isoformat(),
            timestamp.isoformat(), deadline.isoformat(), deadline.isoformat(), timestamp.isoformat(),
            0., 0., {}, "scheduled-owned-stock-exit", "v1", portfolio.source_fingerprint,
        )
        decision = _direct_no_trade(
            plan.symbol, signal, quote, portfolio, activation, policy, timestamp.isoformat(),
            decision_lane="LIVE", code="HORIZON_EXIT_BLOCKED",
            reason="; ".join(plan.reasons) if not plan.ready else "A current quote is required for the due exit.",
        )
        prediction = {**decision.prediction, "position_purpose": "EXIT", "allocation_id": plan.allocation_id,
                      "holding_target_end": plan.target_end, "parent_forecast_id": plan.forecast_id}
        decision = replace(decision, prediction=prediction)
        if not plan.ready or quote is None:
            decisions.append(decision)
            continue
        if not gameplan_pricing and time_in_force != "DAY" and quote.relative_spread > policy.maximum_extended_relative_spread:
            decisions.append(replace(decision, decision_reason_code="EXTENDED_SPREAD_TOO_WIDE",
                                     decision_reason="The extended-hours spread exceeds the existing exit execution limit."))
            continue
        price = round(max(.01, quote.bid * (1 - policy.maximum_limit_offset_bps / 10_000)), policy.price_decimals)
        quantity = min(plan.quantity, int(portfolio.available_sell_shares(plan.symbol)),
                       int(portfolio.account_equity * policy.maximum_single_order_equity_fraction / max(price, quote.ask)))
        if quantity <= 0:
            decisions.append(decision)
            continue
        decisions.append(replace(
            decision, action="SELL", suggested_action="SELL", quantity=quantity,
            hypothetical_quantity=quantity, order_type="LIMIT", limit_price=price,
            decision_reason_code="HORIZON_EXIT_DUE",
            decision_reason="Close only this horizon's reconciled shares at its scheduled expiry; fills are not guaranteed.",
            order_style_reason_code="OWNED_HORIZON_EXIT_MARKETABLE_LIMIT",
            order_style_reason="A bounded stock limit order closes the expired allocation.",
            order_payload=build_schwab_stock_order_payload(
                symbol=plan.symbol, instruction="SELL", order_type="LIMIT", time_in_force=time_in_force,
                position_effect="CLOSING", quantity=quantity, price=price,
            ),
        ))
    return tuple(decisions)


class _SubmissionStopped(RuntimeError):
    pass


def _cancel_expired_entries(root, broker, ledger, state, portfolio, stable_identity, clock, *, late_opening_date=None, gameplan_entries=False):
    requested = 0
    allocations = {a.allocation_id: a for a in state.allocations}
    for reservation in state.reservations:
        if (reservation.side != "BUY" or reservation.status not in {"WORKING", "PARTIAL"}
                or not reservation.broker_order_id or reservation.cancel_requested_at is not None):
            continue
        allocation = allocations[reservation.allocation_id]
        from ml.stock_trader.quote_recovery import recovered_entry_deadline
        deadline = (min(utc(reservation.target_start or allocation.target_start) + pd.Timedelta(hours=1),
                        utc(reservation.target_end or allocation.target_end))
                    if gameplan_entries else recovered_entry_deadline(root, allocation,
                        _entry_deadline(utc(allocation.target_start), late_opening_date=late_opening_date)))
        if utc(clock()) < deadline:
            continue
        try:
            context = broker.prepare_order_submission()

            def gate():
                if not read_gameplan_stock_activation_intent(root).active:
                    raise _SubmissionStopped("OPERATOR_INTENT_NOT_ACTIVE_AT_CANCELLATION")
                if (context.identity_fingerprint != portfolio.broker_identity_fingerprint
                        or broker.stable_account_fingerprint() != stable_identity):
                    raise _SubmissionStopped("STOCK_ACCOUNT_CHANGED_BEFORE_CANCELLATION")
                broker.verify_read_snapshot(portfolio.broker_identity_fingerprint)
                age = (utc(clock()) - utc(portfolio.observed_at)).total_seconds()
                if not 0 <= age <= ledger.maximum_evidence_age_seconds:
                    raise _SubmissionStopped("BROKER_PORTFOLIO_TOO_OLD_FOR_CANCELLATION")

            gate()
            if ledger.reserve_cancellation(reservation.reservation_id, as_of=utc(clock()).isoformat(),
                                           evidence_id=reservation.reservation_id + ":cancel"):
                broker.cancel_prepared_order(reservation.broker_order_id, context, before_delete=gate)
                requested += 1
        except Exception as exc:
            return requested, "Tracked entry cancellation requires reconciliation: " + type(exc).__name__
    return requested, None


def _submit_batch(root, broker, ledger, decisions, publication, window, snapshot_id, stable_identity, clock, finish,
                  *, source_gameplan_run=None):
    submitted = duplicates = 0
    batch_id = publication.run_directory.name
    for decision in decisions:
        if decision.quantity <= 0:
            continue
        reservation = event = None
        try:
            context = broker.prepare_order_submission()

            def final_gate():
                if decision.prediction.get("position_purpose") != "EXIT":
                    try:
                        _assert_execution_deployment(root, source_gameplan_run,
                            action_date=utc(decision.prediction["target_window_start"]).tz_convert(
                                "America/Los_Angeles").date().isoformat())
                    except (OSError, ValueError, RuntimeError) as exc:
                        raise _SubmissionStopped("GAMEPLAN_DEPLOYMENT_NOT_APPROVED: " + str(exc)) from exc
                reason = _submission_identity_safety_reason(decision, context)
                if reason:
                    raise _SubmissionStopped(reason)
                if broker.stable_account_fingerprint() != stable_identity:
                    raise _SubmissionStopped("STOCK_INVENTORY_ACCOUNT_CHANGED")
                broker.verify_read_snapshot(decision.portfolio["broker_identity_fingerprint"])
                age = (utc(clock()) - utc(decision.portfolio["observed_at"])).total_seconds()
                if not 0 <= age <= ledger.maximum_evidence_age_seconds:
                    raise _SubmissionStopped("BROKER_PORTFOLIO_TOO_OLD_FOR_SUBMISSION")
                if decision.prediction.get("sizing_policy") == GAMEPLAN_SIZING_POLICY:
                    quote_at = QuoteState(**decision.quote).freshness_observed_at
                    if not quote_at:
                        raise _SubmissionStopped("CURRENT_QUOTE_TIME_UNAVAILABLE")
                    quote_age = (utc(clock()) - utc(quote_at)).total_seconds()
                    if not 0 <= quote_age <= ledger.maximum_evidence_age_seconds:
                        raise _SubmissionStopped("CURRENT_QUOTE_TOO_OLD_FOR_SUBMISSION")
                reason = _submission_safety_reason(
                    root, decision, as_of=utc(clock()), planned_window=window,
                    allow_open_queue=False, allow_premarket_queue=False, execution_lead_seconds=5,
                    activation_reader=read_gameplan_stock_activation_intent,
                    allow_target_session_transition=decision.prediction.get("sizing_policy") == GAMEPLAN_SIZING_POLICY,
                )
                if reason:
                    raise _SubmissionStopped(reason)

            final_gate()
            event = reserve_execution_intent(root, decision, submitted_at=utc(clock()), decision_publication=publication)
            if event is None:
                duplicates += 1
                continue
            common = dict(
                quantity=decision.quantity, limit_price=decision.limit_price, snapshot_id=snapshot_id,
                idempotency_key=decision.decision_id, batch_id=batch_id, as_of=utc(clock()).isoformat(),
            )
            if decision.prediction.get("position_purpose") == "DIRECTION_EXIT":
                reservation = ledger.reserve_direction_exit(
                    symbol=decision.symbol, horizon=decision.prediction["primary_horizon"],
                    forecast_id=decision.prediction["prediction_id"],
                    target_start=decision.prediction["target_window_start"],
                    target_end=decision.prediction["target_window_end"],
                    pending_sell_shares=decision.portfolio.get("pending_sell_shares", 0), **common,
                )
            elif decision.prediction.get("position_purpose") == "EXIT":
                reservation = ledger.reserve_exit(
                    allocation_id=decision.prediction["allocation_id"], exit_lead_seconds=CLOSE_EXIT_LEAD_SECONDS, **common,
                )
            else:
                reservation = ledger.reserve_entry(
                    symbol=decision.symbol, horizon=decision.prediction["primary_horizon"],
                    forecast_id=decision.prediction["prediction_id"],
                    target_start=decision.prediction["target_window_start"],
                    target_end=decision.prediction["target_window_end"], **common,
                    **({"allow_accumulation": True} if decision.policy_version == GAMEPLAN_SIZING_POLICY else {}),
                )
            final_gate()
        except Exception as exc:
            if event is not None:
                record_execution_result(event, status="NOT_SUBMITTED_SAFETY_CHECK", completed_at=utc(clock()), error=type(exc).__name__ + ": " + str(exc))
            if reservation is not None:
                ledger.mark_submission(reservation.reservation_id, status="REJECTED", observed_at=utc(clock()).isoformat(), evidence_id=decision.decision_id + ":not-submitted")
            return finish("SUBMISSION_STOPPED_SAFETY_CHECK", decisions=decisions, submitted=submitted,
                          duplicates=duplicates, error=type(exc).__name__ + ": " + str(exc), publication=publication)
        try:
            location = broker.submit_prepared_order(dict(decision.order_payload), context, before_post=final_gate)
        except _SubmissionStopped as exc:
            record_execution_result(event, status="NOT_SUBMITTED_SAFETY_CHECK", completed_at=utc(clock()), error=str(exc))
            ledger.mark_submission(reservation.reservation_id, status="REJECTED", observed_at=utc(clock()).isoformat(), evidence_id=decision.decision_id + ":not-submitted")
            return finish("SUBMISSION_STOPPED_SAFETY_CHECK", decisions=decisions, submitted=submitted,
                          error=str(exc), publication=publication)
        except Exception as exc:
            record_execution_result(event, status="SUBMISSION_FAILED_OR_UNKNOWN", completed_at=utc(clock()), error=type(exc).__name__)
            ledger.mark_submission(reservation.reservation_id, status="UNKNOWN", observed_at=utc(clock()).isoformat(), evidence_id=decision.decision_id + ":unknown")
            return finish("SUBMISSION_STOPPED_AFTER_ERROR", decisions=decisions, submitted=submitted,
                          error="Order acceptance is uncertain; its reservation remains in place.", publication=publication)
        broker_id = urlparse(str(location or "")).path.rstrip("/").split("/")[-1]
        if not broker_id.isdigit():
            ledger.mark_submission(reservation.reservation_id, status="UNKNOWN", observed_at=utc(clock()).isoformat(), evidence_id=decision.decision_id + ":missing-order-id")
            record_execution_result(event, status="SUBMISSION_FAILED_OR_UNKNOWN", completed_at=utc(clock()), error="BROKER_ORDER_ID_UNAVAILABLE")
            return finish("SUBMISSION_STOPPED_AFTER_ERROR", decisions=decisions, submitted=submitted,
                          error="No broker order ID was returned; do not repeat the submission.", publication=publication)
        try:
            ledger.mark_submission(reservation.reservation_id, status="SUBMITTED", observed_at=utc(clock()).isoformat(),
                                   evidence_id=decision.decision_id + ":submitted", broker_order_id=broker_id)
            record_execution_result(event, status="SUBMITTED", completed_at=utc(clock()), broker_location=location)
        except Exception:
            return finish("SUBMISSION_STOPPED_AFTER_ERROR", decisions=decisions, submitted=submitted + 1,
                          error="Broker accepted the order but local recording failed; reconcile the reserved intent before continuing.", publication=publication)
        submitted += 1
    return finish("ORDERS_SUBMITTED" if submitted else "NO_ORDERS_SUBMITTED", decisions=decisions,
                  submitted=submitted, duplicates=duplicates, publication=publication)
