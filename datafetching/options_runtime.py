from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from app.services.schwab_retry import call_with_persistent_schwab_retry
from datafetching.bar_readiness import BarReadinessError, read_bar_readiness
from datafetching.decision_time import (
    CycleTargetState,
    completed_bar_clock_for_target,
    cycle_target_decision,
    expected_quarter_hour_target,
)
from datafetching.loop_a_cycle import read_latest_complete_loop_a_cycle
from datafetching.observability import timed_stage
from datafetching.orchestrate import DEFAULT_WATCHLIST, normalize_symbols, read_watchlist
from datafetching.parquet_store import DATASTORE_TARGETS, ParquetStore
from datafetching.pricing_barrier import wait_for_pricing_barrier
from datafetching.runtime_lock import exclusive_runtime_lock
from datafetching.schwab_fetch import DataFetchingSchwabSession
from options.publication import committed_option_snapshots, option_writer_lock_path
from options.snapshot import persist_schwab_option_snapshot


@dataclass(frozen=True)
class OptionsCycleResult:
    published: int
    failed: int
    skipped: int
    target_snapshot_for: object | None = None
    pricing_barrier_status: str = "MISSING"
    pricing_terminal_status: str | None = None
    cycle_mode: str = "ACTIONABLE"
    target_state: str = CycleTargetState.ACTIONABLE_EXACT_TARGET.value
    reason: str = ""
    next_eligible_cycle: object | None = None
    schwab_called: bool = False
    schwab_requests: int = 0


def run_options_cycle(
    store: ParquetStore,
    *,
    symbols: Sequence[str],
    session: DataFetchingSchwabSession | None = None,
    clock: Callable[[], datetime] | None = None,
    writer_lock_held: bool = False,
    reporter: Callable[[str], None] | None = print,
    target_snapshot_for: object | None = None,
    pricing_barrier_timeout_seconds: float = 0.0,
    barrier_sleeper: Callable[[float], None] = time.sleep,
    bar_readiness_mode: str = "required",
    per_symbol_detail: bool = False,
    phase_offset_minutes: int = 2,
) -> OptionsCycleResult:
    """Fetch and atomically commit one Schwab option receipt per symbol."""

    clean_symbols = normalize_symbols(symbols)
    now = clock or (lambda: datetime.now(timezone.utc))
    cycle_started_at = now().astimezone(timezone.utc)
    decision = cycle_target_decision(cycle_started_at)
    supplied_target = (
        expected_quarter_hour_target(target_snapshot_for)
        if target_snapshot_for is not None
        else None
    )
    if not decision.actionable:
        if reporter is not None:
            reporter(
                "Options cycle idle: "
                f"cycle_mode={decision.cycle_mode}; "
                f"target_state={decision.target_state.value}; target=NONE; "
                f"reason={decision.reason}; "
                f"next_eligible_cycle={decision.next_eligible_cycle(phase_offset_minutes=phase_offset_minutes).isoformat()}; "
                "schwab_called=false"
            )
        return OptionsCycleResult(
            published=0,
            failed=0,
            skipped=0,
            target_snapshot_for=None,
            pricing_barrier_status="NOT_APPLICABLE",
            pricing_terminal_status=None,
            cycle_mode=decision.cycle_mode,
            target_state=decision.target_state.value,
            reason=decision.reason,
            next_eligible_cycle=decision.next_eligible_cycle(
                phase_offset_minutes=phase_offset_minutes
            ),
            schwab_called=False,
            schwab_requests=0,
        )
    target = decision.target_snapshot_for
    assert target is not None
    if supplied_target is not None and supplied_target != target:
        raise ValueError(
            "Options target must match the calendar-owned target for cycle start; "
            "older targets cannot be replayed"
        )
    readiness_mode = str(bar_readiness_mode).strip().lower()
    if readiness_mode not in {"required", "exact"}:
        raise ValueError("bar_readiness_mode must be required or exact")

    observed_symbols = {
        symbol
        for symbol in clean_symbols
        if any(
            snapshot.snapshot_for == target
            for snapshot in committed_option_snapshots(store.root_dir, symbol=symbol)
        )
    }
    pending_symbols = tuple(
        symbol for symbol in clean_symbols if symbol not in observed_symbols
    )
    if not pending_symbols:
        observed_decision = decision.with_runtime_state(target_observed=True)
        if reporter is not None:
            reporter(
                "Options cycle idle: "
                f"cycle_mode={observed_decision.cycle_mode}; "
                f"target_state={observed_decision.target_state.value}; "
                f"target={target.isoformat()}; reason={observed_decision.reason}; "
                "schwab_called=false"
            )
        return OptionsCycleResult(
            0,
            0,
            len(clean_symbols),
            target,
            "ALREADY_RECORDED",
            None,
            observed_decision.cycle_mode,
            observed_decision.target_state.value,
            observed_decision.reason,
            decision.next_eligible_cycle(phase_offset_minutes=phase_offset_minutes),
            False,
            0,
        )

    barrier = wait_for_pricing_barrier(
        store.root_dir,
        target_snapshot_for=target,
        required_symbols=clean_symbols,
        timeout_seconds=pricing_barrier_timeout_seconds,
        clock=now,
        sleeper=barrier_sleeper,
    )
    if reporter is not None:
        reporter(
            "Options Pricing barrier: "
            f"target={target.isoformat()}; verification={barrier.status}; "
            f"terminal_outcome={barrier.terminal_status or 'NONE'}; "
            f"observed_at={barrier.observed_at.isoformat()}"
        )

    decision_clocks = {}
    readiness_error = ""
    if readiness_mode == "required":
        try:
            readiness = read_bar_readiness(
                store.root_dir,
                target_snapshot_for=target,
                required_symbols=clean_symbols,
            )
            readiness_observed_at = now().astimezone(timezone.utc)
            if readiness.ready_at > readiness_observed_at:
                raise BarReadinessError(
                    "Loop A readiness carries a future availability clock"
                )
            decision_clocks = {
                symbol: readiness.decision_clock(symbol) for symbol in pending_symbols
            }
        except BarReadinessError as exc:
            readiness_error = f"{type(exc).__name__}: {exc}"
    else:
        exact_errors: dict[str, str] = {}
        for symbol in pending_symbols:
            try:
                decision_clocks[symbol] = completed_bar_clock_for_target(
                    store.root_dir,
                    symbol=symbol,
                    target_snapshot_for=target,
                    as_of=cycle_started_at,
                )
            except Exception as exc:
                exact_errors[symbol] = f"{type(exc).__name__}: {exc}"
        if exact_errors:
            grouped = {}
            for symbol, detail in exact_errors.items():
                grouped.setdefault(detail, []).append(symbol)
            readiness_error = " | ".join(
                f"{detail} (symbols={','.join(symbols_for_reason)})"
                for detail, symbols_for_reason in grouped.items()
            )

    missing_clocks = tuple(
        symbol for symbol in pending_symbols if symbol not in decision_clocks
    )
    if missing_clocks:
        missed = decision.with_runtime_state(
            readiness_available=False,
            deadline_at=cycle_started_at,
            reason=readiness_error or "Exact Loop A readiness is unavailable.",
        )
        if reporter is not None:
            reporter(
                "Options skipped symbols: "
                f"count={len(missing_clocks)}; "
                f"target_state={missed.target_state.value}; "
                f"reason={missed.reason}; schwab_called=false"
            )
            if per_symbol_detail:
                for symbol in missing_clocks:
                    reporter(f"Options symbol {symbol}: skipped; reason={missed.reason}")
        return OptionsCycleResult(
            0,
            0,
            len(clean_symbols),
            target,
            barrier.status,
            barrier.terminal_status,
            missed.cycle_mode,
            missed.target_state.value,
            missed.reason,
            decision.next_eligible_cycle(phase_offset_minutes=phase_offset_minutes),
            False,
            0,
        )

    published = 0
    failed = 0
    skipped = len(observed_symbols)
    schwab_requests = 0
    provider_session = session
    completed_loop_a = read_latest_complete_loop_a_cycle(store.root_dir)
    regime_cutoff = (
        completed_loop_a.finished_at
        if completed_loop_a is not None and completed_loop_a.finished_at is not None
        else datetime(1970, 1, 1, tzinfo=timezone.utc)
    )
    failure_groups: dict[str, list[str]] = {}
    for symbol in pending_symbols:
        request_started_at = now().astimezone(timezone.utc)
        decision_clock = decision_clocks[symbol]
        try:
            if provider_session is None:
                provider_session = DataFetchingSchwabSession()
            with timed_stage(
                "options.fetch-chain",
                symbol=symbol,
                provider="schwab",
                schema="option-chain",
                request_start=request_started_at,
                reporter=reporter,
            ) as timing:
                schwab_requests += 1
                payload = call_with_persistent_schwab_retry(
                    lambda: provider_session.get_option_chain_snapshot(
                        symbol,
                        as_of=request_started_at,
                    ),
                    operation_name=f"{symbol} option-chain snapshot",
                    reporter=reporter,
                    symbol=symbol,
                    schema="option-chain",
                    timing_reporter=reporter,
                )
                timing.annotate(operation="fetched")
            fetched_at = now().astimezone(timezone.utc)
            with timed_stage(
                "options.commit-snapshot",
                symbol=symbol,
                provider="schwab",
                schema="option-chain",
                request_start=request_started_at,
                request_end=fetched_at,
                reporter=reporter,
            ) as timing:
                output = persist_schwab_option_snapshot(
                    store.root_dir,
                    symbol=symbol,
                    payload=payload,
                    clock=decision_clock,
                    fetched_at=fetched_at,
                    quote_cutoff_at=request_started_at,
                    regime_available_not_after=regime_cutoff,
                    pricing_barrier=barrier.as_receipt_metadata(
                        request_started_at=request_started_at
                    ),
                    acquire_writer_lock=not writer_lock_held,
                )
                receipt_published_at = None
                if output.receipt_path is not None and output.receipt_path.is_file():
                    try:
                        receipt_payload = json.loads(
                            output.receipt_path.read_text(encoding="utf-8")
                        )
                        receipt_published_at = receipt_payload.get(
                            "receipt_published_at"
                        )
                    except (OSError, TypeError, ValueError, json.JSONDecodeError):
                        receipt_published_at = None
                timing.annotate(
                    row_count=output.contract_rows,
                    operation="wrote",
                    receipt_path=str(output.receipt_path or ""),
                    snapshot_for=decision_clock.decision_timestamp.isoformat(),
                    available_at=fetched_at.isoformat(),
                    receipt_published_at=receipt_published_at,
                    regime_committed_through=regime_cutoff.isoformat(),
                    loop_a_generation=(
                        completed_loop_a.generation
                        if completed_loop_a is not None
                        else None
                    ),
                    pricing_barrier_status=barrier.status,
                    pricing_run_path=barrier.pricing_run_path,
                    pricing_receipt_checksum_sha256=(
                        barrier.pricing_receipt_checksum_sha256
                    ),
                )
            published += 1
        except Exception as exc:
            failed += 1
            detail = f"{type(exc).__name__}: {exc}"
            failure_groups.setdefault(detail, []).append(symbol)
            _record_failure(
                store,
                symbol=symbol,
                stage="fetch-or-commit",
                exc=exc,
                metadata={
                    "request_started_at": request_started_at.isoformat(),
                    "snapshot_for": decision_clock.decision_timestamp.isoformat(),
                },
            )
    if reporter is not None:
        for detail, affected in failure_groups.items():
            reporter(
                "Options fetch/commit failures: "
                f"count={len(affected)}; reason={detail}"
            )
            if per_symbol_detail:
                reporter(f"Options failure symbols: {', '.join(affected)}")
    return OptionsCycleResult(
        published,
        failed,
        skipped,
        target,
        barrier.status,
        barrier.terminal_status,
        decision.cycle_mode,
        decision.target_state.value,
        decision.reason,
        decision.next_eligible_cycle(phase_offset_minutes=phase_offset_minutes),
        schwab_requests > 0,
        schwab_requests,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the independent Schwab option-chain publication loop."
    )
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--interval-minutes", type=int, default=15)
    parser.add_argument("--phase-offset-minutes", type=int, default=2)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--pricing-barrier-timeout-seconds",
        type=float,
        default=45.0,
        help="Bounded wait for the verified Pricing target outcome before fallback.",
    )
    parser.add_argument(
        "--bar-readiness-mode",
        choices=("required", "exact"),
        default="required",
        help=(
            "Required consumes Loop A's atomic all-symbol receipt; exact is a "
            "standalone compatibility mode that still rejects stale targets."
        ),
    )
    parser.add_argument(
        "--per-symbol-detail",
        action="store_true",
        help="Print per-symbol skip/failure detail in addition to grouped diagnostics.",
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default=None,
    )
    args = parser.parse_args(argv)
    if args.interval_minutes < 1:
        parser.error("--interval-minutes must be at least 1")
    if not 0 <= args.phase_offset_minutes < args.interval_minutes:
        parser.error(
            "--phase-offset-minutes must satisfy 0 <= phase < interval-minutes"
        )
    if args.pricing_barrier_timeout_seconds < 0:
        parser.error("--pricing-barrier-timeout-seconds cannot be negative")
    symbols = normalize_symbols(args.symbols or read_watchlist(args.watchlist))
    if not symbols:
        parser.error("No symbols were configured")
    store = ParquetStore(args.datastore, target=args.datastore_target)

    print("DUCKETS OPTIONS RUNTIME")
    print("=======================")
    print(f"DATASTORE: {store.root_dir}")
    print(f"Watchlist: {', '.join(symbols)}")
    print(f"Interval: {args.interval_minutes} minutes; UTC phase +{args.phase_offset_minutes}")
    print("Ownership: Schwab raw chains, normalized contracts, and option-quality surfaces")
    print("Stop: Ctrl+C")
    print()

    with exclusive_runtime_lock(
        option_writer_lock_path(store.root_dir),
        process_name="Duckets Options runtime",
    ):
        try:
            while True:
                cycle_anchor = datetime.now(timezone.utc)
                if not args.once:
                    boundary = next_boundary(
                        cycle_anchor,
                        interval_minutes=args.interval_minutes,
                        phase_offset_minutes=args.phase_offset_minutes,
                    )
                    print(f"Next Options cycle: {boundary.isoformat()}")
                    time.sleep(
                        max(
                            0.0,
                            (boundary - datetime.now(timezone.utc)).total_seconds(),
                        )
                    )
                    cycle_anchor = boundary
                result = run_options_cycle(
                    store,
                    symbols=symbols,
                    writer_lock_held=True,
                    target_snapshot_for=expected_quarter_hour_target(cycle_anchor),
                    pricing_barrier_timeout_seconds=(
                        args.pricing_barrier_timeout_seconds
                    ),
                    bar_readiness_mode=args.bar_readiness_mode,
                    per_symbol_detail=args.per_symbol_detail,
                    phase_offset_minutes=args.phase_offset_minutes,
                )
                report_options_result(result)
                if args.once:
                    return 1 if result.failed else 0
        except KeyboardInterrupt:
            print("Options runtime stopped.")
            return 0


def report_options_result(
    result: OptionsCycleResult,
    *,
    reporter: Callable[[str], None] = print,
) -> None:
    reporter(
        "Options cycle complete: "
        f"cycle_mode={result.cycle_mode}; "
        f"target_state={result.target_state}; "
        f"target={result.target_snapshot_for if result.target_snapshot_for is not None else 'NONE'}; "
        f"reason={result.reason}; "
        f"next_eligible_cycle={result.next_eligible_cycle if result.next_eligible_cycle is not None else 'UNKNOWN'}; "
        f"pricing_barrier_verification={result.pricing_barrier_status}; "
        f"pricing_terminal_outcome={result.pricing_terminal_status or 'NONE'}; "
        f"schwab_called={str(result.schwab_called).lower()}; "
        f"schwab_requests={result.schwab_requests}; "
        f"published={result.published}; failed={result.failed}; "
        f"skipped={result.skipped}"
    )


def next_boundary(
    now: datetime,
    *,
    interval_minutes: int,
    phase_offset_minutes: int,
) -> datetime:
    current = now.astimezone(timezone.utc)
    anchor = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        minutes=phase_offset_minutes
    )
    if current < anchor:
        return anchor
    intervals = int((current - anchor).total_seconds() // (interval_minutes * 60))
    return anchor + timedelta(minutes=(intervals + 1) * interval_minutes)


def _record_failure(
    store: ParquetStore,
    *,
    symbol: str,
    stage: str,
    exc: Exception,
    metadata: dict[str, object] | None = None,
) -> None:
    try:
        store.save_error(
            source="schwab",
            category="options",
            symbol=symbol,
            request_key=f"options_runtime_{stage}",
            error_type=type(exc).__name__,
            error_message=str(exc),
            metadata=metadata,
        )
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
