from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

from app.services.schwab_retry import call_with_persistent_schwab_retry
from datafetching.decision_time import (
    completed_bar_clock_for_target,
    expected_quarter_hour_target,
)
from datafetching.loop_a_cycle import read_latest_complete_loop_a_cycle
from datafetching.observability import timed_stage
from datafetching.orchestrate import DEFAULT_WATCHLIST, normalize_symbols, read_watchlist
from datafetching.parquet_store import DATASTORE_TARGETS, ParquetStore
from datafetching.pricing_barrier import wait_for_pricing_barrier
from datafetching.runtime_lock import exclusive_runtime_lock
from datafetching.schwab_fetch import DataFetchingSchwabSession
from options.publication import option_writer_lock_path
from options.snapshot import persist_schwab_option_snapshot


@dataclass(frozen=True)
class OptionsCycleResult:
    published: int
    failed: int
    skipped: int
    target_snapshot_for: object | None = None
    pricing_barrier_status: str = "MISSING"


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
) -> OptionsCycleResult:
    """Fetch and atomically commit one Schwab option receipt per symbol."""

    clean_symbols = normalize_symbols(symbols)
    provider_session = session or DataFetchingSchwabSession()
    now = clock or (lambda: datetime.now(timezone.utc))
    cycle_started_at = now().astimezone(timezone.utc)
    target = (
        expected_quarter_hour_target(target_snapshot_for)
        if target_snapshot_for is not None
        else expected_quarter_hour_target(cycle_started_at)
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
            f"target={target.isoformat()}; status={barrier.status}; "
            f"terminal={barrier.terminal_status or 'NONE'}; "
            f"observed_at={barrier.observed_at.isoformat()}"
        )
    published = 0
    failed = 0
    skipped = 0
    completed_loop_a = read_latest_complete_loop_a_cycle(store.root_dir)
    regime_cutoff = (
        completed_loop_a.finished_at
        if completed_loop_a is not None and completed_loop_a.finished_at is not None
        else datetime(1970, 1, 1, tzinfo=timezone.utc)
    )
    for symbol in clean_symbols:
        request_started_at = now().astimezone(timezone.utc)
        try:
            decision_clock = completed_bar_clock_for_target(
                store.root_dir,
                symbol=symbol,
                target_snapshot_for=target,
                as_of=request_started_at,
            )
        except Exception as exc:
            skipped += 1
            _record_failure(
                store,
                symbol=symbol,
                stage="decision-clock",
                exc=exc,
            )
            continue

        try:
            with timed_stage(
                "options.fetch-chain",
                symbol=symbol,
                provider="schwab",
                schema="option-chain",
                request_start=request_started_at,
                reporter=reporter,
            ) as timing:
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
    return OptionsCycleResult(
        published,
        failed,
        skipped,
        target,
        barrier.status,
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
                )
                print(
                    "Options cycle complete: "
                    f"target={result.target_snapshot_for}; "
                    f"pricing_barrier={result.pricing_barrier_status}; "
                    f"published={result.published}; failed={result.failed}; "
                    f"skipped={result.skipped}"
                )
                if args.once:
                    return 1 if result.failed else 0
        except KeyboardInterrupt:
            print("Options runtime stopped.")
            return 0


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
