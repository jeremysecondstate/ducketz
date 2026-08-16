from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence

import pandas as pd

from app.services.schwab_retry import call_with_persistent_schwab_retry
from datafetching.bar_readiness import BarReadinessError, read_bar_readiness
from datafetching.cme_runtime import load_repository_environment
from datafetching.decision_time import (
    CycleTargetState,
    completed_bar_clock_for_target,
    cycle_target_decision,
    expected_quarter_hour_target,
    latest_eligible_option_target,
)
from datafetching.databento_opra_history import (
    STANDARD_SCHEMAS,
    OpraCapacityError,
    SyncScope,
    canonical_root,
    discover_standard_entitlement,
    synchronize,
)
from datafetching.loop_a_cycle import read_latest_complete_loop_a_cycle
from datafetching.observability import timed_stage
from datafetching.orchestrate import DEFAULT_WATCHLIST, normalize_symbols, read_watchlist
from datafetching.parquet_store import DATASTORE_TARGETS, ParquetStore
from datafetching.pricing_barrier import wait_for_pricing_barrier
from datafetching.runtime_lock import exclusive_runtime_lock
from datafetching.schwab_fetch import DataFetchingSchwabSession
from options.publication import committed_option_snapshots, option_writer_lock_path
from options.databento_live import (
    DatabentoOpraIntegrityError,
    DatabentoOpraLiveAdapter,
)
from options.providers import (
    OptionMarketDataAdapter,
    OptionProviderUnavailable,
    validate_canonical_opra_adapter,
)
from options.pending_capture import (
    PendingOptionCaptureError,
    begin_pending_option_request,
    complete_pending_option_capture,
    pending_option_capture_counts,
    pending_option_capture_directory,
    read_pending_option_request,
    reconcile_pending_option_captures,
    record_pending_request_failure,
)
from options.snapshot import (
    normalize_databento_opra_option_snapshot,
    persist_provider_option_snapshot,
    persist_schwab_option_snapshot,
)
from ml.universe import canonical_production_option_symbols


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
    opra_called: bool = False
    opra_requests: int = 0
    schwab_fallbacks: int = 0
    pending_captures: int = 0
    reconciled_captures: int = 0
    expired_captures: int = 0
    failed_captures: int = 0
    pricing_surface_diagnostics: dict[str, object] | None = None


@dataclass(frozen=True)
class OptionHistorySyncSummary:
    requested_scopes: int = 0
    completed_scopes: int = 0
    capacity_blocked_scopes: int = 0
    failed_scopes: int = 0
    bootstrap_required_scopes: int = 0


DISCOVERY_CYCLE_MODE = "DISCOVERY_REFRESH"
DISCOVERY_TARGET_STATE = "DISCOVERY_CHAIN_TARGET"
DISCOVERY_ALREADY_REFRESHED_STATE = "DISCOVERY_CHAIN_ALREADY_REFRESHED"
OPRA_DEFAULT_CATCHUP_OVERLAP_DAYS = 3
OPRA_SCHEMA_CATCHUP_OVERLAP_DAYS: dict[str, int] = {
    "ohlcv-1s": 1,
    "ohlcv-1m": 2,
    "ohlcv-1h": 5,
    "ohlcv-1d": 10,
}
OPRA_SYMBOL_HISTORY_CURSOR_VERSION = "options-opra-symbol-history-v5"
OPRA_LEGACY_SYMBOL_HISTORY_CURSOR_VERSION = "options-opra-symbol-history-v4"
OPRA_SYMBOL_HISTORY_SCHEMA_ORDER = (
    "definition",
    "ohlcv-1d",
    "ohlcv-1h",
    "ohlcv-1m",
    "ohlcv-1s",
    "status",
    "statistics",
    "trades",
    "tcbbo",
    "cbbo-1m",
    "cbbo-1s",
    "cmbp-1",
)
OPRA_SCHEMA_HISTORY_LOOKBACK_POLICY: dict[str, tuple[str, int]] = {
    "definition": ("years", 13),
    "ohlcv-1d": ("years", 13),
    "ohlcv-1h": ("days", 2_000),
    "ohlcv-1m": ("days", 100),
    "ohlcv-1s": ("days", 5),
    "status": ("months", 6),
    "statistics": ("months", 6),
    "trades": ("months", 6),
    "tcbbo": ("months", 6),
    "cbbo-1m": ("months", 6),
    "cbbo-1s": ("months", 6),
    "cmbp-1": ("months", 1),
}
OPRA_LEGACY_SCHEMA_HISTORY_LOOKBACK_POLICY: dict[str, tuple[str, int]] = {
    **OPRA_SCHEMA_HISTORY_LOOKBACK_POLICY,
    "definition": ("days", 5_000),
    "ohlcv-1d": ("days", 5_000),
}
DISCOVERY_PENDING_STATE = "DISCOVERY_CHAIN_PENDING_READINESS"
OPRA_CANONICAL_MODE = "opra-canonical"
SCHWAB_COMPATIBILITY_MODE = "schwab-only-compatibility"


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
    phase_offset_minutes: int = 6,
    canonical_market_adapter: OptionMarketDataAdapter | None = None,
) -> OptionsCycleResult:
    """Fetch one chain per cycle, including closed-market discovery refreshes."""

    clean_symbols = normalize_symbols(symbols)
    if canonical_market_adapter is not None:
        validate_canonical_opra_adapter(canonical_market_adapter)
    now = clock or (lambda: datetime.now(timezone.utc))
    cycle_started_at = now().astimezone(timezone.utc)
    readiness_mode = str(bar_readiness_mode).strip().lower()
    if readiness_mode not in {"required", "exact"}:
        raise ValueError("bar_readiness_mode must be required or exact")

    # Reconciliation is intentionally first and runs during closed-market calls.
    initial_reconciliation = reconcile_pending_option_captures(
        store.root_dir,
        reconciled_at=cycle_started_at,
        persist=persist_schwab_option_snapshot,
        acquire_writer_lock=not writer_lock_held,
    )
    reconciled_this_cycle = initial_reconciliation.newly_reconciled
    inventory = initial_reconciliation
    decision = cycle_target_decision(cycle_started_at)
    supplied_target = (
        expected_quarter_hour_target(target_snapshot_for)
        if target_snapshot_for is not None
        else None
    )
    discovery_refresh = not decision.actionable
    target = (
        latest_eligible_option_target(cycle_started_at)
        if discovery_refresh
        else decision.target_snapshot_for
    )
    assert target is not None
    if (
        not discovery_refresh
        and supplied_target is not None
        and supplied_target != target
    ):
        raise ValueError(
            "Options target must match the calendar-owned target for cycle start; "
            "older targets cannot be replayed"
        )

    cycle_boundary = expected_quarter_hour_target(cycle_started_at)
    next_cycle = (
        cycle_boundary + timedelta(minutes=15 + phase_offset_minutes)
        if discovery_refresh
        else decision.next_eligible_cycle(
            phase_offset_minutes=phase_offset_minutes
        )
    )
    discovery_reason = (
        "The regular option market is closed; Schwab chain discovery is being "
        f"refreshed against the latest eligible target {target.isoformat()}."
    )

    # A natural target committed by either supported provider is terminal for
    # ordinary capture.  Compatibility mode disables new OPRA acquisition; it
    # does not authorize a second Schwab request for an already committed OPRA
    # target.
    committed_providers = ("databento-opra", "schwab")
    observed_symbols = {
        symbol
        for symbol in clean_symbols
        if any(
            snapshot.snapshot_for == target
            for provider in committed_providers
            for snapshot in committed_option_snapshots(
                store.root_dir, symbol=symbol, provider=provider
            )
        )
    }
    claimed_symbols: set[str] = set()
    for symbol in clean_symbols:
        if symbol in observed_symbols or discovery_refresh:
            continue
        directory = pending_option_capture_directory(
            store.root_dir,
            symbol=symbol,
            target_snapshot_for=target,
        )
        if directory.exists():
            request = read_pending_option_request(directory)
            if (
                request.symbol != symbol
                or request.target_snapshot_for != target
                or frozenset(request.required_symbols) != frozenset(clean_symbols)
            ):
                raise PendingOptionCaptureError(
                    "Existing pending Options request does not match the runtime scope"
                )
            claimed_symbols.add(symbol)
    fetch_symbols = tuple(
        symbol
        for symbol in clean_symbols
        if symbol not in observed_symbols and symbol not in claimed_symbols
    )
    if not fetch_symbols:
        inventory = pending_option_capture_counts(store.root_dir)
        all_observed = len(observed_symbols) == len(clean_symbols)
        if discovery_refresh:
            return OptionsCycleResult(
                published=reconciled_this_cycle,
                failed=0,
                skipped=len(clean_symbols),
                target_snapshot_for=target,
                pricing_barrier_status=(
                    "ALREADY_RECORDED" if all_observed else "PENDING_READINESS"
                ),
                cycle_mode=DISCOVERY_CYCLE_MODE,
                target_state=(
                    DISCOVERY_ALREADY_REFRESHED_STATE
                    if all_observed
                    else DISCOVERY_PENDING_STATE
                ),
                reason=(
                    discovery_reason
                    if all_observed
                    else "A durable discovery response is pending exact Loop A readiness."
                ),
                next_eligible_cycle=next_cycle,
                pending_captures=inventory.pending,
                reconciled_captures=inventory.reconciled,
                expired_captures=inventory.expired,
                failed_captures=inventory.failed,
            )
        final_decision = (
            decision.with_runtime_state(target_observed=True)
            if all_observed
            else decision.with_runtime_state(
                readiness_available=False,
                deadline_at=target + timedelta(seconds=1_200),
                reason="A durable Schwab response is pending exact Loop A readiness.",
            )
        )
        return OptionsCycleResult(
            published=reconciled_this_cycle,
            failed=0,
            skipped=len(clean_symbols),
            target_snapshot_for=target,
            pricing_barrier_status=(
                "ALREADY_RECORDED" if all_observed else "PENDING_READINESS"
            ),
            cycle_mode=final_decision.cycle_mode,
            target_state=final_decision.target_state.value,
            reason=final_decision.reason,
            next_eligible_cycle=next_cycle,
            pending_captures=inventory.pending,
            reconciled_captures=inventory.reconciled,
            expired_captures=inventory.expired,
            failed_captures=inventory.failed,
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

    decision_clocks: dict[str, object] = {}
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
                symbol: readiness.decision_clock(symbol) for symbol in fetch_symbols
            }
        except BarReadinessError as exc:
            readiness_error = f"{type(exc).__name__}: {exc}"
            if discovery_refresh:
                exact_errors: dict[str, str] = {}
                for symbol in fetch_symbols:
                    try:
                        decision_clocks[symbol] = completed_bar_clock_for_target(
                            store.root_dir,
                            symbol=symbol,
                            target_snapshot_for=target,
                            as_of=cycle_started_at,
                        )
                    except Exception as exact_exc:
                        exact_errors[symbol] = (
                            f"{type(exact_exc).__name__}: {exact_exc}"
                        )
                if exact_errors:
                    readiness_error += (
                        " | exact discovery clocks unavailable: "
                        + "; ".join(
                            f"{symbol}={detail}"
                            for symbol, detail in exact_errors.items()
                        )
                    )
    else:
        exact_errors: dict[str, str] = {}
        for symbol in fetch_symbols:
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
            grouped: dict[str, list[str]] = {}
            for symbol, detail in exact_errors.items():
                grouped.setdefault(detail, []).append(symbol)
            readiness_error = " | ".join(
                f"{detail} (symbols={','.join(symbols_for_reason)})"
                for detail, symbols_for_reason in grouped.items()
            )
    readiness_complete = all(symbol in decision_clocks for symbol in fetch_symbols)

    published = reconciled_this_cycle
    failed = 0
    schwab_requests = 0
    opra_requests = 0
    schwab_fallbacks = 0
    provider_session = session
    completed_loop_a = read_latest_complete_loop_a_cycle(store.root_dir)
    regime_cutoff = (
        completed_loop_a.finished_at
        if completed_loop_a is not None and completed_loop_a.finished_at is not None
        else datetime(1970, 1, 1, tzinfo=timezone.utc)
    )
    failure_groups: dict[str, list[str]] = {}
    for symbol in fetch_symbols:
        request_started_at = now().astimezone(timezone.utc)
        pending_request = None
        fallback_reason: str | None = None
        failure_source = "schwab"
        try:
            barrier_metadata = barrier.as_receipt_metadata(
                request_started_at=request_started_at
            )
            # Canonical OPRA capture is independent of Loop A readiness: the
            # quote/definition evidence carries its own point-in-time clocks and
            # can be committed before Pricing has a usable underlying row.  The
            # pending quarantine is retained for the Schwab fallback lane, whose
            # legacy normalizer requires the eventual exact Loop A decision clock.
            if canonical_market_adapter is not None:
                try:
                    failure_source = "databento-opra"
                    opra_requests += 1
                    evidence = canonical_market_adapter.fetch_snapshot(
                        symbol=symbol,
                        target_snapshot_for=pd.Timestamp(target),
                        requested_at=pd.Timestamp(request_started_at),
                    )
                    if (
                        str(evidence.provider).strip().lower() != "databento-opra"
                        or str(evidence.dataset).strip().upper() != "OPRA.PILLAR"
                        or str(evidence.schema).strip().lower() != "cbbo-1s"
                        or str(evidence.symbol).strip().upper() != symbol
                        or pd.Timestamp(evidence.target_snapshot_for) != pd.Timestamp(target)
                    ):
                        raise ValueError(
                            "Canonical adapter returned evidence outside the requested OPRA target"
                        )
                except OptionProviderUnavailable as exc:
                    # Only a bounded availability failure can cross into the
                    # broker fallback lane. Integrity and contract failures are
                    # rejected for this target rather than silently substituted.
                    fallback_reason = _safe_provider_failure_code(exc)
                    schwab_fallbacks += 1
                    _record_failure(
                        store,
                        symbol=symbol,
                        source="databento-opra",
                        stage="canonical-capture-unavailable",
                        exc=exc,
                        safe_message=fallback_reason,
                        metadata={
                            "request_started_at": request_started_at.isoformat(),
                            "snapshot_for": target.isoformat(),
                            "fallback_provider": "schwab",
                        },
                    )
                    if reporter is not None:
                        reporter(
                            "Canonical OPRA capture unavailable; using explicit "
                            f"Schwab fallback for {symbol}: {fallback_reason}"
                        )
                except DatabentoOpraIntegrityError:
                    raise
                except Exception:
                    raise DatabentoOpraIntegrityError(
                        "OPRA_ADAPTER_UNEXPECTED_FAILURE"
                    ) from None
                else:
                    publication_at = now().astimezone(timezone.utc)
                    evidence_received_at = pd.to_datetime(
                        evidence.received_at,
                        utc=True,
                        errors="coerce",
                    )
                    if (
                        pd.isna(evidence_received_at)
                        or evidence_received_at > pd.Timestamp(publication_at)
                    ):
                        raise DatabentoOpraIntegrityError(
                            "OPRA_EVIDENCE_HAS_INVALID_OR_FUTURE_LOCAL_RECEIPT"
                        )
                    normalized = normalize_databento_opra_option_snapshot(
                        evidence.quotes,
                        evidence.definitions,
                        symbol=symbol,
                        target_snapshot_for=target,
                        received_at=evidence.received_at,
                        dataset=evidence.dataset,
                        schema=evidence.schema,
                    )
                    persist_provider_option_snapshot(
                        store.root_dir,
                        provider="databento-opra",
                        dataset=evidence.dataset,
                        symbol=symbol,
                        raw=normalized,
                        contracts=normalized,
                        features=normalized,
                        request_started_at=request_started_at,
                        pricing_barrier=barrier_metadata,
                        receipt_published_at=publication_at,
                        acquire_writer_lock=not writer_lock_held,
                    )
                    published += 1
                    continue
            if symbol not in decision_clocks:
                pending_request, created = begin_pending_option_request(
                    store.root_dir,
                    symbol=symbol,
                    target_snapshot_for=target,
                    request_started_at=request_started_at,
                    required_symbols=clean_symbols,
                    bar_readiness_mode=readiness_mode,
                    regime_available_not_after=regime_cutoff,
                    pricing_barrier=barrier_metadata,
                )
                if not created:
                    continue
            failure_source = "schwab"
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
            if pending_request is not None:
                complete_pending_option_capture(
                    pending_request,
                    payload=payload,
                    response_received_at=fetched_at,
                )
                continue
            decision_clock = decision_clocks[symbol]
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
                    pricing_barrier=barrier_metadata,
                    capture_provenance={
                        "capture_mode": (
                            DISCOVERY_CYCLE_MODE if discovery_refresh else "ACTIONABLE"
                        ),
                        "response_received_at": fetched_at,
                        "canonical_provider": (
                            "databento-opra"
                            if canonical_market_adapter is not None
                            else None
                        ),
                        "fallback_used": fallback_reason is not None,
                        "fallback_reason": fallback_reason,
                    },
                    update_legacy_monthly_mirrors=not discovery_refresh,
                    acquire_writer_lock=not writer_lock_held,
                )
                timing.annotate(
                    row_count=output.contract_rows,
                    operation="wrote",
                    receipt_path=str(output.receipt_path or ""),
                    snapshot_for=decision_clock.decision_timestamp.isoformat(),
                    available_at=fetched_at.isoformat(),
                    regime_committed_through=regime_cutoff.isoformat(),
                    pricing_barrier_status=barrier.status,
                )
            published += 1
        except Exception as exc:
            failed += 1
            if pending_request is not None:
                record_pending_request_failure(
                    pending_request,
                    failed_at=now().astimezone(timezone.utc),
                    exc=exc,
                )
            detail = f"{type(exc).__name__}: {exc}"
            failure_groups.setdefault(detail, []).append(symbol)
            _record_failure(
                store,
                symbol=symbol,
                source=failure_source,
                stage="fetch-or-commit",
                exc=exc,
                safe_message=(
                    _safe_provider_failure_code(exc)
                    if failure_source == "databento-opra"
                    else None
                ),
                metadata={
                    "request_started_at": request_started_at.isoformat(),
                    "snapshot_for": target.isoformat(),
                },
            )

    if not readiness_complete:
        reconciliation_at = now().astimezone(timezone.utc)
        final_reconciliation = reconcile_pending_option_captures(
            store.root_dir,
            reconciled_at=reconciliation_at,
            persist=persist_schwab_option_snapshot,
            acquire_writer_lock=not writer_lock_held,
        )
        published += final_reconciliation.newly_reconciled
        inventory = final_reconciliation
    else:
        inventory = pending_option_capture_counts(store.root_dir)
    if reporter is not None:
        for detail, affected in failure_groups.items():
            reporter(
                "Options fetch/commit failures: "
                f"count={len(affected)}; reason={detail}"
            )
            if per_symbol_detail:
                reporter(f"Options failure symbols: {', '.join(affected)}")
    if discovery_refresh:
        cycle_mode = DISCOVERY_CYCLE_MODE
        target_state = (
            DISCOVERY_TARGET_STATE if readiness_complete else DISCOVERY_PENDING_STATE
        )
        reason = (
            discovery_reason
            if readiness_complete
            else (
                "Pricing lacks exact Loop A readiness; Schwab discovery fallback is PENDING_READINESS; "
                + (readiness_error or "exact Loop A readiness is unavailable.")
            )
        )
    else:
        final_decision = (
            decision
            if readiness_complete
            else decision.with_runtime_state(
                readiness_available=False,
                deadline_at=target + timedelta(seconds=1_200),
                reason=(
                    "Pricing lacks exact Loop A readiness; Schwab fallback is PENDING_READINESS; "
                    + (readiness_error or "exact Loop A readiness is unavailable.")
                ),
            )
        )
        cycle_mode = final_decision.cycle_mode
        target_state = final_decision.target_state.value
        reason = final_decision.reason
    return OptionsCycleResult(
        published=published,
        failed=failed,
        skipped=len(observed_symbols) + len(claimed_symbols),
        target_snapshot_for=target,
        pricing_barrier_status=barrier.status,
        pricing_terminal_status=barrier.terminal_status,
        cycle_mode=cycle_mode,
        target_state=target_state,
        reason=reason,
        next_eligible_cycle=next_cycle,
        schwab_called=schwab_requests > 0,
        schwab_requests=schwab_requests,
        opra_called=opra_requests > 0,
        opra_requests=opra_requests,
        schwab_fallbacks=schwab_fallbacks,
        pending_captures=inventory.pending,
        reconciled_captures=inventory.reconciled,
        expired_captures=inventory.expired,
        failed_captures=inventory.failed,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the independent canonical-OPRA option publication loop."
    )
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    parser.add_argument("--symbols", nargs="+", default=None)
    parser.add_argument("--interval-minutes", type=int, default=15)
    parser.add_argument("--phase-offset-minutes", type=int, default=6)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--provider-mode",
        choices=(OPRA_CANONICAL_MODE, SCHWAB_COMPATIBILITY_MODE),
        default=OPRA_CANONICAL_MODE,
        help=(
            "opra-canonical requires a live OPRA adapter and uses labeled Schwab "
            "fallback per unavailable target; schwab-only-compatibility explicitly "
            "disables OPRA and is not the production mode."
        ),
    )
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
    parser.add_argument(
        "--skip-historical-catchup",
        action="store_true",
        help=(
            "Disable Options-owned daily incremental OPRA cursor catch-up."
        ),
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
    try:
        symbols = canonical_production_option_symbols(
            normalize_symbols(args.symbols or read_watchlist(args.watchlist)),
            label="Options Capture production watchlist",
        )
    except Exception as exc:
        parser.error(str(exc))
    store = ParquetStore(args.datastore, target=args.datastore_target)

    with exclusive_runtime_lock(
        option_writer_lock_path(store.root_dir),
        process_name="Duckets Options runtime",
    ):
        canonical_adapter: DatabentoOpraLiveAdapter | None = None
        try:
            if args.provider_mode == OPRA_CANONICAL_MODE:
                load_repository_environment()
                api_key = os.environ.get("DATABENTO_API_KEY", "").strip()
                if not api_key:
                    parser.error(
                        "DATABENTO_API_KEY is required for --provider-mode "
                        "opra-canonical; Options Capture was not started"
                    )
                try:
                    canonical_adapter = DatabentoOpraLiveAdapter(
                        api_key=api_key,
                        symbols=symbols,
                    )
                except Exception:
                    parser.error(
                        "Canonical OPRA transport could not be constructed; "
                        "Options Capture was not started"
                    )

            print("DUCKETS OPTIONS RUNTIME")
            print("=======================")
            print(f"DATASTORE: {store.root_dir}")
            print(f"Watchlist: {', '.join(symbols)}")
            print(
                f"Interval: {args.interval_minutes} minutes; "
                f"UTC phase +{args.phase_offset_minutes}"
            )
            print(f"Provider mode: {args.provider_mode}")
            print(
                "Ownership: canonical prospective OPRA evidence with explicit "
                "Schwab fallback/broker evidence"
                if canonical_adapter is not None
                else "Ownership: explicit Schwab-only compatibility evidence"
            )
            if canonical_adapter is not None and not args.skip_historical_catchup:
                print(
                    "Historical OPRA: incremental cursor maintenance; "
                    "schema-specific incremental overlap"
                )
                print(
                    "Historical OPRA bootstrap: run python -m "
                    "datafetching.options_history once for new symbols"
                )
            print("Stop: Ctrl+C")
            print()

            last_catchup_date = None

            while True:
                cycle_anchor = datetime.now(timezone.utc)
                catchup_date = cycle_anchor.date()
                if (
                    canonical_adapter is not None
                    and not args.skip_historical_catchup
                    and catchup_date != last_catchup_date
                ):
                    synchronize_option_history(
                        store,
                        api_key=api_key,
                        symbols=symbols,
                        reporter=print,
                        bootstrap_missing=False,
                    )
                    last_catchup_date = catchup_date
                if not args.once:
                    boundary = next_boundary(
                        cycle_anchor,
                        interval_minutes=args.interval_minutes,
                        phase_offset_minutes=args.phase_offset_minutes,
                    )
                    print(f"Next Options cycle: {boundary.isoformat()}")
                    _wait_for_options_boundary(
                        store,
                        boundary=boundary,
                        reporter=print,
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
                    canonical_market_adapter=canonical_adapter,
                )
                report_options_result(result)
                if args.once:
                    return 1 if result.failed else 0
        except KeyboardInterrupt:
            print("Options runtime stopped.")
            return 0
        finally:
            if canonical_adapter is not None:
                canonical_adapter.close()


def synchronize_option_history(
    store: ParquetStore,
    *,
    api_key: str,
    symbols: Sequence[str],
    reporter: Callable[[str], None] | None,
    bootstrap_missing: bool = True,
    schemas: Sequence[str] = OPRA_SYMBOL_HISTORY_SCHEMA_ORDER,
) -> OptionHistorySyncSummary:
    """Bootstrap or incrementally maintain Standard history per parent symbol."""

    import databento as db

    clean_schemas = tuple(dict.fromkeys(str(schema) for schema in schemas))
    invalid = sorted(set(clean_schemas).difference(STANDARD_SCHEMAS))
    if invalid:
        raise ValueError("Unsupported OPRA schemas: " + ", ".join(invalid))
    requested = completed = blocked = failed = needs_bootstrap = 0
    client = db.Historical(api_key)
    try:
        entitlement = discover_standard_entitlement(
            client, datastore_root=store.root_dir
        )
        history_root = canonical_root(store.root_dir)
        with exclusive_runtime_lock(
            history_root / "state" / "sync.lock",
            process_name="Options-owned OPRA symbol history synchronizer",
        ):
            clean_symbols = normalize_symbols(symbols)
            for schema in clean_schemas:
                end = str(entitlement["entitlements"][schema]["entitled_end"])
                lookback_label = opra_history_lookback_label(schema)
                full_start = opra_history_start(schema, end=end)
                for symbol in clean_symbols:
                    requested += 1
                    provider_symbol = f"{symbol}.OPT"
                    marker = _read_opra_symbol_history_cursor(
                        store.root_dir,
                        symbol=symbol,
                        schema=schema,
                    )
                    if marker is None:
                        if not bootstrap_missing:
                            needs_bootstrap += 1
                            if reporter is not None:
                                reporter(
                                    "OPRA symbol/schema bootstrap required: "
                                    f"symbol={symbol}; schema={schema}; command=python -m "
                                    "datafetching.options_history"
                                )
                            continue
                        start = full_start
                        mode = "INITIAL_" + lookback_label.upper().replace(" ", "_")
                    else:
                        completed_end = pd.Timestamp(
                            str(marker["completed_through"])
                        )
                        start = max(
                            pd.Timestamp(full_start),
                            completed_end
                            - pd.Timedelta(days=opra_history_overlap_days(schema)),
                        ).date().isoformat()
                        mode = "INCREMENTAL_CATCHUP"
                    try:
                        result = synchronize(
                            client,
                            datastore_root=store.root_dir,
                            entitlement=entitlement,
                            scope=SyncScope(
                                schemas=(schema,),
                                start=start,
                                end=end,
                                symbols=(provider_symbol,),
                            ),
                            reporter=reporter,
                        )
                    except OpraCapacityError as exc:
                        blocked += 1
                        if reporter is not None:
                            reporter(
                                "OPRA symbol/schema history capacity blocked: "
                                f"symbol={symbol}; schema={schema}; mode={mode}; {exc}"
                            )
                        continue
                    if result.errors or result.completed_rows < 1:
                        failed += 1
                        if reporter is not None:
                            reporter(
                                f"OPRA symbol/schema history incomplete: symbol={symbol}; "
                                f"schema={schema}; mode={mode}; "
                                f"errors={len(result.errors)}; rows={result.completed_rows}; "
                                f"health={result.health_path}"
                            )
                        continue
                    completed += 1
                    _write_opra_symbol_history_cursor(
                        store.root_dir,
                        symbol=symbol,
                        schema=schema,
                        completed_through=end,
                    )
                    if reporter is not None:
                        reporter(
                            f"OPRA symbol/schema history: symbol={symbol}; "
                            f"schema={schema}; mode={mode}; start={start}; end={end}; "
                            f"status={result.status}; "
                            f"completed={result.completed_partitions}; "
                            f"verified_existing={result.skipped_partitions}; "
                            f"rows={result.completed_rows}; health={result.health_path}"
                        )
    except Exception as exc:
        failed += 1
        if reporter is not None:
            reporter(
                "OPRA historical catch-up failed: "
                f"{type(exc).__name__}: {exc}"
            )
    return OptionHistorySyncSummary(
        requested_scopes=requested,
        completed_scopes=completed,
        capacity_blocked_scopes=blocked,
        failed_scopes=failed,
        bootstrap_required_scopes=needs_bootstrap,
    )


def _opra_symbol_history_cursor_path(
    datastore_root: Path,
    *,
    symbol: str,
    schema: str,
) -> Path:
    return (
        canonical_root(datastore_root)
        / "state"
        / "symbol-history"
        / symbol.strip().upper()
        / f"{schema}.json"
    )


def _read_opra_symbol_history_cursor(
    datastore_root: Path,
    *,
    symbol: str,
    schema: str,
) -> dict[str, object] | None:
    path = _opra_symbol_history_cursor_path(
        datastore_root,
        symbol=symbol,
        schema=schema,
    )
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        completed = pd.Timestamp(str(payload["completed_through"]))
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if (
        payload.get("symbol") != symbol.strip().upper()
        or payload.get("provider_symbol") != f"{symbol.strip().upper()}.OPT"
        or payload.get("schema") != schema
        or pd.isna(completed)
    ):
        return None
    version = payload.get("schema_version")
    policy = payload.get("lookback_policy")
    if version == OPRA_LEGACY_SYMBOL_HISTORY_CURSOR_VERSION:
        legacy_unit, legacy_value = OPRA_LEGACY_SCHEMA_HISTORY_LOOKBACK_POLICY[
            schema
        ]
        if policy != {"unit": legacy_unit, "value": legacy_value}:
            return None
    elif version == OPRA_SYMBOL_HISTORY_CURSOR_VERSION:
        if not _valid_opra_history_policy(policy):
            return None
        requested_start = pd.Timestamp(str(payload.get("requested_start", "")))
        if pd.isna(requested_start) or requested_start > completed:
            return None
    else:
        return None
    return payload


def _write_opra_symbol_history_cursor(
    datastore_root: Path,
    *,
    symbol: str,
    schema: str,
    completed_through: str,
    requested_start: str | None = None,
    lookback_policy: dict[str, object] | None = None,
    bootstrap_manifest_id: str | None = None,
) -> Path:
    clean_symbol = symbol.strip().upper()
    policy = lookback_policy or opra_history_lookback_policy(schema)
    if not _valid_opra_history_policy(policy):
        raise ValueError(f"Invalid OPRA history lookback policy for {schema}")
    start = requested_start or opra_history_start(schema, end=completed_through)
    parsed_start = pd.Timestamp(start)
    parsed_end = pd.Timestamp(completed_through)
    if pd.isna(parsed_start) or pd.isna(parsed_end) or parsed_start > parsed_end:
        raise ValueError("OPRA history cursor bounds are invalid")
    path = _opra_symbol_history_cursor_path(
        datastore_root,
        symbol=clean_symbol,
        schema=schema,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": OPRA_SYMBOL_HISTORY_CURSOR_VERSION,
        "provider": "databento-opra",
        "dataset": "OPRA.PILLAR",
        "symbol": clean_symbol,
        "provider_symbol": f"{clean_symbol}.OPT",
        "schema": schema,
        "lookback_policy": dict(policy),
        "requested_start": parsed_start.date().isoformat(),
        "completed_through": completed_through,
        "updated_at": pd.Timestamp.now(tz="UTC").isoformat(),
    }
    if bootstrap_manifest_id:
        payload["bootstrap_manifest_id"] = str(bootstrap_manifest_id)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def publish_opra_symbol_history_cursor(
    datastore_root: Path,
    *,
    symbol: str,
    schema: str,
    requested_start: str,
    completed_through: str,
    lookback_policy: dict[str, object],
    bootstrap_manifest_id: str,
) -> Path:
    """Publish a verified bootstrap cursor without granting snapshot authority.

    The Options runtime owns forward overlap maintenance.  This helper only
    records that a separate, checksum-verified historical bootstrap completed;
    it never writes option snapshots, live pointers, or the Options writer lock.
    """

    if schema not in STANDARD_SCHEMAS:
        raise ValueError(f"Unsupported OPRA schema: {schema}")
    return _write_opra_symbol_history_cursor(
        datastore_root,
        symbol=symbol,
        schema=schema,
        requested_start=requested_start,
        completed_through=completed_through,
        lookback_policy=lookback_policy,
        bootstrap_manifest_id=bootstrap_manifest_id,
    )


def opra_history_lookback_policy(schema: str) -> dict[str, object]:
    try:
        unit, value = OPRA_SCHEMA_HISTORY_LOOKBACK_POLICY[schema]
    except KeyError as exc:
        raise ValueError(f"Unsupported OPRA schema: {schema}") from exc
    return {"unit": unit, "value": value}


def _valid_opra_history_policy(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    unit = value.get("unit")
    amount = value.get("value")
    return (
        unit in {"days", "months", "years"}
        and isinstance(amount, int)
        and not isinstance(amount, bool)
        and amount > 0
    )


def opra_history_start(schema: str, *, end: str) -> str:
    policy = opra_history_lookback_policy(schema)
    anchor = pd.Timestamp(end)
    value = int(policy["value"])
    if policy["unit"] == "days":
        start = anchor - pd.Timedelta(days=value)
    elif policy["unit"] == "years":
        start = anchor - pd.DateOffset(years=value)
    else:
        start = anchor - pd.DateOffset(months=value)
    return start.date().isoformat()


def opra_history_lookback_label(schema: str) -> str:
    policy = opra_history_lookback_policy(schema)
    value = int(policy["value"])
    unit = str(policy["unit"])
    if value == 1:
        unit = unit.removesuffix("s")
    return f"{value} {unit}"


def opra_history_overlap_days(schema: str) -> int:
    if schema not in STANDARD_SCHEMAS:
        raise ValueError(f"Unsupported OPRA schema: {schema}")
    return OPRA_SCHEMA_CATCHUP_OVERLAP_DAYS.get(
        schema,
        OPRA_DEFAULT_CATCHUP_OVERLAP_DAYS,
    )


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
        f"opra_called={str(result.opra_called).lower()}; "
        f"opra_requests={result.opra_requests}; "
        f"schwab_fallbacks={result.schwab_fallbacks}; "
        f"published={result.published}; failed={result.failed}; "
        f"skipped={result.skipped}; "
        f"pending_captures={result.pending_captures}; "
        f"reconciled_captures={result.reconciled_captures}; "
        f"expired_captures={result.expired_captures}; "
        f"failed_captures={result.failed_captures}"
    )
    if result.cycle_mode == "MONITOR_ONLY" and result.pricing_surface_diagnostics:
        diagnostic = result.pricing_surface_diagnostics
        fresh = diagnostic.get("fresh_horizons") or ()
        reporter(
            "Options carried Pricing authority: "
            f"path={diagnostic.get('authority_path') or 'NONE'}; "
            f"publication_version={diagnostic.get('publication_version') or 'NONE'}; "
            f"surface_version={diagnostic.get('surface_version') or 'NONE'}; "
            f"published_at={diagnostic.get('published_at') or 'NONE'}; "
            f"publication_age_seconds={diagnostic.get('publication_age_seconds') if diagnostic.get('publication_age_seconds') is not None else 'UNKNOWN'}; "
            f"legacy_normalization_used={str(bool(diagnostic.get('legacy_normalization_used'))).lower()}; "
            f"fresh_horizons={','.join(str(value) for value in fresh) or 'NONE'}"
        )


def _wait_for_options_boundary(
    store: ParquetStore,
    *,
    boundary: datetime,
    reporter: Callable[[str], None] | None,
    poll_seconds: float = 15.0,
    clock: Callable[[], datetime] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Poll only the pending authority while waiting for the next fetch target."""

    if poll_seconds <= 0:
        raise ValueError("Options reconciliation poll interval must be positive")
    now = clock or (lambda: datetime.now(timezone.utc))
    while True:
        observed = now().astimezone(timezone.utc)
        remaining = (boundary - observed).total_seconds()
        if remaining <= 0:
            return
        summary = reconcile_pending_option_captures(
            store.root_dir,
            reconciled_at=observed,
            persist=persist_schwab_option_snapshot,
            acquire_writer_lock=False,
        )
        if reporter is not None and summary.newly_reconciled:
            reporter(
                "Options pending reconciliation: "
                f"newly_reconciled={summary.newly_reconciled}; "
                f"pending={summary.pending}; expired={summary.expired}"
            )
        sleeper(min(poll_seconds, remaining))


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
    source: str = "schwab",
    stage: str,
    exc: Exception,
    safe_message: str | None = None,
    metadata: dict[str, object] | None = None,
) -> None:
    try:
        store.save_error(
            source=source,
            category="options",
            symbol=symbol,
            request_key=f"options_runtime_{stage}",
            error_type=type(exc).__name__,
            error_message=safe_message if safe_message is not None else str(exc),
            metadata=metadata,
        )
    except Exception:
        pass


def _safe_provider_failure_code(exc: Exception) -> str:
    value = str(exc).strip().upper()
    if value and len(value) <= 96 and all(
        character.isalnum() or character == "_" for character in value
    ):
        return value
    return type(exc).__name__.upper()


if __name__ == "__main__":
    raise SystemExit(main())
