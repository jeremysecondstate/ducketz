from __future__ import annotations

import argparse
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datafetching.loop_a_cycle import (
    begin_loop_a_cycle,
    datastore_cycle_lock,
    finish_loop_a_cycle,
)
from datafetching.main import (
    FETCH_PROFILES,
    PROVIDERS,
    run_symbol_fetch,
    run_symbols_fetch,
)
from datafetching.bar_readiness import publish_bar_readiness
from datafetching.cme_runtime import load_repository_environment
from datafetching.databento_fetch import recover_historical_minute_target
from datafetching.databento_opra_history import OPRA_STRATEGY_HISTORY_SCHEMAS
from datafetching.decision_time import cycle_target_decision
from datafetching.observability import timed_stage
from datafetching.parquet_store import DATASTORE_TARGETS, ParquetStore
from datafetching.readiness_lane import running_readiness_lane
from datafetching.runtime_lock import exclusive_runtime_lock
from fundamentals.main import main as run_fundamentals
from signals.main import main as run_signals
from technicals.main import main as run_technicals

DEFAULT_WATCHLIST = Path(__file__).resolve().parent / "watchlist.txt"

# Recurring Loop A uses Schwab only for best-effort equity quote/liquidity
# enrichment.  Databento remains the authoritative price/bar lane, while the
# stock trader captures and validates current broker state independently before
# it can submit an order.  A Schwab capture outage must therefore remain
# observable without invalidating an otherwise usable directional generation.
NON_BLOCKING_QUOTE_ONLY_PROVIDERS = frozenset({"schwab"})

# Bound the Loop A-owned update for the shared production strategy-history
# schema contract declared by the canonical OPRA synchronizer.
# 17:00 PDT is 00:00 UTC on the following date.  The post-close owner starts
# just after that boundary so an exclusive OPRA cursor can include the entire
# most recently completed options session before modeling begins.
DEFAULT_OPRA_HISTORY_UTC_HOUR = 0
DEFAULT_OPRA_HISTORY_MAX_DOWNLOAD_BYTES = 20_000_000_000
DEFAULT_OPRA_HISTORY_MAX_COST_USD = 1.0
DEFAULT_OPRA_HISTORY_MAX_CATCHUP_DAYS = 30


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run Duckets fetching, fundamental calculations, technical calculations, "
            "and cross-domain signals for a watchlist now, then repeat on the "
            "configured interval. Loop B reads these current values independently."
        )
    )
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=DEFAULT_WATCHLIST,
        help="Text file containing one equity symbol per line.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Optional symbols that override the watchlist file.",
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=PROVIDERS,
        default=list(PROVIDERS),
        help="Provider lanes to run for the watchlist.",
    )
    parser.add_argument(
        "--profile",
        choices=("auto", *FETCH_PROFILES),
        default="auto",
        help=(
            "Compatibility option. Auto, continuation, full, and incremental all use "
            "the same per-dataset continuation behavior."
        ),
    )
    parser.add_argument(
        "--interval-minutes",
        type=int,
        default=15,
        help="Polling interval in minutes.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one orchestration cycle and exit.",
    )
    parser.add_argument(
        "--skip-cme",
        action="store_true",
        help="Skip shared Databento CME context.",
    )
    parser.add_argument(
        "--bar-readiness-recovery-timeout-seconds",
        type=float,
        default=420.0,
        help=(
            "Bounded provider-aware wait for Databento Historical ohlcv-1m "
            "availability when the exact Loop A target is initially absent."
        ),
    )
    parser.add_argument(
        "--bar-readiness-recovery-poll-seconds",
        type=float,
        default=10.0,
        help="Databento Historical metadata poll interval during target recovery.",
    )
    parser.add_argument(
        "--cme-mode",
        choices=("external", "inline"),
        default="external",
        help="External uses datafetching.cme_runtime; inline is compatibility mode.",
    )
    parser.add_argument(
        "--options-mode",
        choices=("external", "inline"),
        default="external",
        help="External uses datafetching.options_runtime; inline is compatibility mode.",
    )
    parser.add_argument(
        "--opra-history-mode",
        choices=("off", "daily"),
        default="off",
        help=(
            "Run the production OPRA strategy-history catch-up once per UTC date "
            "after its configured hour. The canonical production launcher uses daily."
        ),
    )
    parser.add_argument(
        "--opra-history-utc-hour",
        type=int,
        default=DEFAULT_OPRA_HISTORY_UTC_HOUR,
        help="UTC hour after which Loop A owns the daily OPRA history catch-up.",
    )
    parser.add_argument(
        "--opra-history-max-estimated-download-bytes",
        type=int,
        default=DEFAULT_OPRA_HISTORY_MAX_DOWNLOAD_BYTES,
    )
    parser.add_argument(
        "--opra-history-max-estimated-cost-usd",
        type=float,
        default=DEFAULT_OPRA_HISTORY_MAX_COST_USD,
    )
    parser.add_argument(
        "--opra-history-max-catchup-days",
        type=int,
        default=DEFAULT_OPRA_HISTORY_MAX_CATCHUP_DAYS,
    )
    parser.add_argument(
        "--skip-fundamentals",
        action="store_true",
        help="Fetch data without rebuilding point-in-time fundamental outputs.",
    )
    parser.add_argument(
        "--skip-technicals",
        action="store_true",
        help="Fetch data without recalculating technical outputs.",
    )
    parser.add_argument(
        "--skip-signals",
        action="store_true",
        help="Fetch data without rebuilding cross-domain signal outputs.",
    )
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
    args = parser.parse_args(argv)

    if args.interval_minutes < 1:
        parser.error("--interval-minutes must be at least 1")
    if args.bar_readiness_recovery_timeout_seconds < 0:
        parser.error("--bar-readiness-recovery-timeout-seconds cannot be negative")
    if args.bar_readiness_recovery_poll_seconds <= 0:
        parser.error("--bar-readiness-recovery-poll-seconds must be positive")
    if not 0 <= args.opra_history_utc_hour <= 23:
        parser.error("--opra-history-utc-hour must be in [0, 23]")
    if args.opra_history_max_estimated_download_bytes < 1:
        parser.error("--opra-history-max-estimated-download-bytes must be positive")
    if args.opra_history_max_estimated_cost_usd < 0:
        parser.error("--opra-history-max-estimated-cost-usd cannot be negative")
    if args.opra_history_max_catchup_days < 1:
        parser.error("--opra-history-max-catchup-days must be positive")
    symbols = normalize_symbols(args.symbols or read_watchlist(args.watchlist))
    if not symbols:
        parser.error("No symbols were found. Add one to the watchlist or pass --symbols.")

    store = ParquetStore(args.datastore, target=args.datastore_target)
    print("DUCKETS ORCHESTRATION")
    print("=====================")
    print(f"Watchlist: {', '.join(symbols)}")
    print(f"DATASTORE: {store.root_dir}")
    print(f"Providers: {', '.join(args.providers)}")
    print("Fetch mode: continuation")
    print(f"Interval: {args.interval_minutes} minutes")
    print(
        "OPRA Strategy history: "
        f"{args.opra_history_mode}; schemas={','.join(OPRA_STRATEGY_HISTORY_SCHEMAS)}; "
        f"utc_hour={args.opra_history_utc_hour}"
    )
    print("Loop B input: current normalized, fundamental, technical, and signal Parquets")
    print("Stop: Ctrl+C")
    print()

    lock_path = store.root_dir / ".ducketz-orchestration.lock"
    readiness_context = (
        running_readiness_lane(
            store.root_dir,
            symbols=symbols,
            deadline_seconds=args.bar_readiness_recovery_timeout_seconds,
            poll_seconds=args.bar_readiness_recovery_poll_seconds,
        )
        if "databento" in args.providers and not args.once
        else nullcontext()
    )
    if args.opra_history_mode == "daily":
        load_repository_environment()
    last_opra_history_attempt: date | None = None
    with orchestration_lock(lock_path), readiness_context:
        try:
            while True:
                cycle_scheduled_at = datetime.now(timezone.utc)
                with datastore_cycle_lock(store.root_dir, reporter=print):
                    # This is the truthful start of the heavyweight generation.
                    # The independent readiness lane remains active while this
                    # acquisition waits behind a Loop B reader.
                    cycle_started_at = datetime.now(timezone.utc)
                    cycle = begin_loop_a_cycle(
                        store.root_dir,
                        symbols=symbols,
                        providers=args.providers,
                        now=cycle_started_at,
                    )
                    try:
                        failures = run_cycle(
                            symbols,
                            store,
                            providers=args.providers,
                            requested_profile=args.profile,
                            include_cme=(
                                args.cme_mode == "inline" and not args.skip_cme
                            ),
                            include_options=args.options_mode == "inline",
                            run_technical_calculations=not args.skip_technicals,
                            datastore_target=args.datastore_target,
                            datastore_path=args.datastore,
                            run_fundamental_calculations=not args.skip_fundamentals,
                            run_signal_calculations=not args.skip_signals,
                            cycle_started_at=cycle_started_at,
                            loop_a_generation=cycle.generation,
                            bar_readiness_recovery_timeout_seconds=(
                                args.bar_readiness_recovery_timeout_seconds
                            ),
                            bar_readiness_recovery_poll_seconds=(
                                args.bar_readiness_recovery_poll_seconds
                            ),
                            publish_bar_readiness_from_cycle=args.once,
                        )
                    except BaseException:
                        finish_loop_a_cycle(
                            store.root_dir,
                            cycle,
                            failure_count=1,
                        )
                        raise
                    else:
                        terminal = finish_loop_a_cycle(
                            store.root_dir,
                            cycle,
                            failure_count=failures,
                        )
                        print(
                            f"Loop A datastore cycle {terminal.generation}: "
                            f"{terminal.status}"
                        )
                history_exit_code = 0
                maintenance_clock = datetime.now(timezone.utc)
                if (
                    args.opra_history_mode == "daily"
                    and opra_history_maintenance_due(
                        maintenance_clock,
                        last_attempt=last_opra_history_attempt,
                        utc_hour=args.opra_history_utc_hour,
                    )
                ):
                    last_opra_history_attempt = maintenance_clock.date()
                    history_exit_code = run_opra_history_maintenance_once(
                        store,
                        symbols=symbols,
                        max_estimated_download_bytes=(
                            args.opra_history_max_estimated_download_bytes
                        ),
                        max_estimated_cost_usd=(
                            args.opra_history_max_estimated_cost_usd
                        ),
                        max_incremental_catchup_days=(
                            args.opra_history_max_catchup_days
                        ),
                    )
                    print(
                        "Loop A OPRA Strategy history maintenance: "
                        f"exit_code={history_exit_code}; "
                        f"attempt_date={last_opra_history_attempt.isoformat()}"
                    )
                if args.once:
                    return 1 if failures or history_exit_code else 0

                # Preserve the cadence owned by this cycle.  If a provider-aware
                # recovery crosses the following boundary, scheduling from the
                # completion clock would silently skip that target.  A past-due
                # boundary intentionally produces an immediate bounded catch-up.
                next_run = next_boundary(
                    cycle_scheduled_at,
                    interval_minutes=args.interval_minutes,
                )
                print(
                    f"Cycle completed at {datetime.now(timezone.utc).isoformat()} "
                    f"with {failures} failure(s)."
                )
                print(f"Next cycle: {next_run.isoformat()}")
                print()
                time.sleep(max(0.0, (next_run - datetime.now(timezone.utc)).total_seconds()))
                print("20s Pause Pre-Start")
                time.sleep(20)
        except KeyboardInterrupt:
            print("Orchestration stopped.")
            return 0

def opra_history_maintenance_due(
    now: datetime,
    *,
    last_attempt: date | None,
    utc_hour: int = DEFAULT_OPRA_HISTORY_UTC_HOUR,
) -> bool:
    """Return whether this Loop A owner owes today's OPRA maintenance attempt."""

    if not 0 <= utc_hour <= 23:
        raise ValueError("OPRA history UTC hour must be in [0, 23]")
    current = now.astimezone(timezone.utc)
    return current.hour >= utc_hour and last_attempt != current.date()


def run_opra_history_maintenance_once(
    store: ParquetStore,
    *,
    symbols: Iterable[str],
    max_estimated_download_bytes: int = DEFAULT_OPRA_HISTORY_MAX_DOWNLOAD_BYTES,
    max_estimated_cost_usd: float = DEFAULT_OPRA_HISTORY_MAX_COST_USD,
    max_incremental_catchup_days: int = DEFAULT_OPRA_HISTORY_MAX_CATCHUP_DAYS,
) -> int:
    """Run the guarded production OPRA dependency update in a separate process."""

    clean_symbols = tuple(
        str(value).strip().upper() for value in symbols if str(value).strip()
    )
    command = [
        sys.executable,
        "-u",
        "-m",
        "datafetching.options_history",
        "--datastore",
        str(store.root_dir),
        "--symbols",
        *clean_symbols,
        "--schemas",
        *OPRA_STRATEGY_HISTORY_SCHEMAS,
        "--incremental-only",
        "--max-estimated-download-bytes",
        str(int(max_estimated_download_bytes)),
        "--max-estimated-cost-usd",
        str(float(max_estimated_cost_usd)),
        "--max-incremental-catchup-days",
        str(int(max_incremental_catchup_days)),
    ]
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        check=False,
    )
    if completed.returncode:
        return int(completed.returncode)
    catalog_command = [
        sys.executable,
        "-u",
        "-m",
        "datafetching.datastore_hygiene",
        "--datastore",
        str(store.root_dir),
        "--symbols",
        *clean_symbols,
    ]
    catalog = subprocess.run(
        catalog_command,
        cwd=Path(__file__).resolve().parents[1],
        check=False,
    )
    return int(catalog.returncode)


def run_cycle(
    symbols: Iterable[str],
    store: ParquetStore,
    *,
    providers: Iterable[str],
    requested_profile: str,
    include_cme: bool,
    run_technical_calculations: bool,
    datastore_target: str | None,
    datastore_path: Path | None,
    include_options: bool = False,
    include_schwab_price_history: bool = False,
    run_fundamental_calculations: bool = True,
    run_signal_calculations: bool = True,
    cycle_started_at: datetime | None = None,
    loop_a_generation: str | None = None,
    bar_readiness_clock: Callable[[], object] | None = None,
    bar_readiness_recovery_timeout_seconds: float = 0.0,
    bar_readiness_recovery_poll_seconds: float = 10.0,
    publish_bar_readiness_from_cycle: bool = True,
) -> int:
    if bar_readiness_recovery_timeout_seconds < 0:
        raise ValueError("Bar readiness recovery timeout cannot be negative")
    if bar_readiness_recovery_poll_seconds <= 0:
        raise ValueError("Bar readiness recovery poll interval must be positive")
    failures = 0
    providers_tuple = tuple(providers)
    fetch_providers_tuple = (
        ("databento",)
        + tuple(provider for provider in providers_tuple if provider != "databento")
        if "databento" in providers_tuple
        else providers_tuple
    )
    symbols_tuple = tuple(symbols)
    started_at = cycle_started_at or datetime.now(timezone.utc)
    target_decision = cycle_target_decision(started_at)
    quote_only_capture = not include_options and not include_schwab_price_history
    print(f"CYCLE {started_at.isoformat()}")
    print("-" * 48)
    print(
        "Loop A target decision: "
        f"cycle_mode={target_decision.cycle_mode}; "
        f"target_state={target_decision.target_state.value}; "
        f"target={target_decision.target_snapshot_for.isoformat() if target_decision.target_snapshot_for is not None else 'NONE'}; "
        f"reason={target_decision.reason}; "
        f"next_eligible_cycle={target_decision.next_eligible_cycle().isoformat()}"
    )
    if not symbols_tuple:
        return 0

    profiles = {
        symbol: resolve_profile(store, symbol, requested_profile)
        for symbol in symbols_tuple
    }
    for symbol in symbols_tuple:
        print(f"[{symbol}] fetch mode: {profiles[symbol]}")

    readiness_attempted = False

    def provider_completed(
        provider: str,
        _results: Mapping[str, object],
    ) -> None:
        nonlocal readiness_attempted
        if provider != "databento" or readiness_attempted:
            return
        readiness_attempted = True
        if not publish_bar_readiness_from_cycle:
            print(
                "Loop A heavyweight Databento stage completed; immutable target "
                "readiness is owned by the independent exact-bar lane."
            )
            return
        if not target_decision.actionable:
            print(
                "Loop A bar readiness idle: "
                f"cycle_mode={target_decision.cycle_mode}; "
                f"target_state={target_decision.target_state.value}; target=NONE; "
                f"reason={target_decision.reason}; "
                f"next_eligible_cycle={target_decision.next_eligible_cycle().isoformat()}"
            )
            return
        target = target_decision.target_snapshot_for
        assert target is not None
        try:
            observed_at = (
                bar_readiness_clock()
                if bar_readiness_clock is not None
                else datetime.now(timezone.utc)
            )
            readiness = publish_bar_readiness(
                store.root_dir,
                target_snapshot_for=target,
                symbols=symbols_tuple,
                loop_a_generation=(
                    loop_a_generation
                    or f"standalone-{started_at.strftime('%Y%m%dT%H%M%S.%fZ')}"
                ),
                as_of=observed_at,
                clock=(lambda: observed_at),
            )
            print(
                "Loop A bars ready: "
                f"cycle_mode=ACTIONABLE; "
                f"target_state=ACTIONABLE_EXACT_TARGET; "
                f"target={readiness.target_snapshot_for.isoformat()}; "
                f"ready_at={readiness.ready_at.isoformat()}; "
                f"symbols={len(readiness.symbols)}"
            )
        except FileNotFoundError as initial_exc:
            if bar_readiness_recovery_timeout_seconds <= 0:
                print(
                    "Loop A bars waiting: "
                    f"cycle_mode=ACTIONABLE; "
                    "target_state=WAITING_FOR_LOOP_A_READINESS; "
                    f"target={target.isoformat()}; "
                    f"reason={type(initial_exc).__name__}: {initial_exc}"
                )
                return
            try:
                recovery = recover_historical_minute_target(
                    symbols_tuple,
                    store,
                    target_snapshot_for=target,
                    timeout_seconds=bar_readiness_recovery_timeout_seconds,
                    poll_seconds=bar_readiness_recovery_poll_seconds,
                    clock=bar_readiness_clock,
                )
                observed_at = (
                    bar_readiness_clock()
                    if bar_readiness_clock is not None
                    else datetime.now(timezone.utc)
                )
                readiness = publish_bar_readiness(
                    store.root_dir,
                    target_snapshot_for=target,
                    symbols=symbols_tuple,
                    loop_a_generation=(
                        loop_a_generation
                        or f"standalone-{started_at.strftime('%Y%m%dT%H%M%S.%fZ')}"
                    ),
                    as_of=observed_at,
                    clock=(lambda: observed_at),
                )
                print(
                    "Loop A bars ready after provider-aware recovery: "
                    f"target_state=ACTIONABLE_EXACT_TARGET; "
                    f"target={readiness.target_snapshot_for.isoformat()}; "
                    f"ready_at={readiness.ready_at.isoformat()}; "
                    f"provider_available_end={recovery['provider_available_end']}; "
                    f"attempts={recovery['attempts']}"
                )
            except Exception as exc:
                print(
                    "Loop A bars waiting: "
                    f"cycle_mode=ACTIONABLE; "
                    "target_state=HISTORICAL_RECOVERY_DEADLINE_MISSED; "
                    f"target={target.isoformat()}; "
                    f"reason={type(exc).__name__}: {exc}"
                )
        except Exception as exc:
            # Corrupt or contradictory readiness is not eligible for a network
            # retry.  The existing authority fails closed for operator repair.
            print(
                "Loop A bars waiting: "
                f"cycle_mode=ACTIONABLE; "
                "target_state=READINESS_INTEGRITY_FAILURE; "
                f"target={target.isoformat()}; "
                f"reason={type(exc).__name__}: {exc}"
            )

    def minute_bars_completed(_results: Mapping[str, object]) -> None:
        provider_completed("databento", _results)

    if len(symbols_tuple) == 1:
        symbol = symbols_tuple[0]
        fetch_results = {
            symbol: run_symbol_fetch(
                symbol,
                store,
                providers=fetch_providers_tuple,
                profile=profiles[symbol],
                include_cme=include_cme,
                include_fmp_macro=True,
                include_options=include_options,
                include_schwab_price_history=include_schwab_price_history,
                provider_completed=provider_completed,
                databento_minute_bars_completed=minute_bars_completed,
            )
        }
    else:
        fetch_results = run_symbols_fetch(
            symbols_tuple,
            store,
            providers=fetch_providers_tuple,
            profile=profiles[symbols_tuple[0]],
            include_cme=include_cme,
            include_fmp_macro=True,
            include_options=include_options,
            include_schwab_price_history=include_schwab_price_history,
            provider_completed=provider_completed,
            databento_minute_bars_completed=minute_bars_completed,
        )

    # Test doubles and older in-process callers may not expose the provider
    # completion callback. The fallback preserves correctness, while the real
    # provider loop publishes immediately after its Databento lane.
    if "databento" in providers_tuple and not readiness_attempted:
        provider_completed("databento", {})

    for index, symbol in enumerate(symbols_tuple):
        symbol_providers = providers_tuple if index == 0 else tuple(
            provider for provider in providers_tuple if provider != "fred"
        )
        results = fetch_results[symbol]
        changed = sum(result.data_files for result in results)
        provider_errors = sum(result.error_files for result in results)
        blocking_provider_errors = sum(
            result.error_files
            for result in results
            if _provider_failure_blocks_loop_a(
                result.provider,
                quote_only_capture=quote_only_capture,
            )
        )
        optional_capture_errors = provider_errors - blocking_provider_errors
        local_advisories = sum(result.advisory_files for result in results)
        failures += blocking_provider_errors
        blocking_error_breakdown = ", ".join(
            f"{result.provider}={result.error_files}"
            for result in results
            if result.error_files
            and _provider_failure_blocks_loop_a(
                result.provider,
                quote_only_capture=quote_only_capture,
            )
        )
        blocking_detail = (
            f" ({blocking_error_breakdown})" if blocking_error_breakdown else ""
        )
        optional_error_breakdown = ", ".join(
            f"{result.provider}={result.error_files}"
            for result in results
            if result.error_files
            and not _provider_failure_blocks_loop_a(
                result.provider,
                quote_only_capture=quote_only_capture,
            )
        )
        optional_detail = (
            f" ({optional_error_breakdown})" if optional_error_breakdown else ""
        )
        advisory_breakdown = ", ".join(
            f"{result.provider}={result.advisory_files}"
            for result in results
            if result.advisory_files
        )
        advisory_detail = (
            f" ({advisory_breakdown})" if advisory_breakdown else ""
        )
        print(
            f"[{symbol}] changed parquet files: {changed}; "
            f"blocking provider failures: "
            f"{blocking_provider_errors}{blocking_detail}; "
            f"optional capture failures: "
            f"{optional_capture_errors}{optional_detail}; "
            f"local advisories: {local_advisories}{advisory_detail}"
        )

        if run_fundamental_calculations and "fmp" in symbol_providers:
            fundamental_args = [symbol]
            if datastore_target:
                fundamental_args.extend(["--datastore-target", datastore_target])
            elif datastore_path:
                fundamental_args.extend(["--datastore", str(datastore_path)])
            with timed_stage(
                "loop-a.fundamentals",
                symbol=symbol,
                provider="fmp",
                reporter=print,
            ) as timing:
                fundamental_exit = run_fundamentals(fundamental_args)
                timing.annotate(
                    operation="wrote" if not fundamental_exit else "failed"
                )
            if fundamental_exit:
                failures += 1
                print(f"[{symbol}] fundamental calculations reported a failure.")

        if run_technical_calculations:
            technical_args = [symbol]
            if datastore_target:
                technical_args.extend(["--datastore-target", datastore_target])
            elif datastore_path:
                technical_args.extend(["--datastore", str(datastore_path)])
            with timed_stage(
                "loop-a.technicals",
                symbol=symbol,
                provider="calculated",
                reporter=print,
            ) as timing:
                technical_exit = run_technicals(technical_args)
                timing.annotate(
                    operation="wrote" if not technical_exit else "failed"
                )
            if technical_exit:
                failures += 1
                print(f"[{symbol}] technical calculations reported a failure.")

        if run_signal_calculations:
            signal_args = [symbol]
            if datastore_target:
                signal_args.extend(["--datastore-target", datastore_target])
            elif datastore_path:
                signal_args.extend(["--datastore", str(datastore_path)])
            with timed_stage(
                "loop-a.signals",
                symbol=symbol,
                provider="calculated",
                reporter=print,
            ) as timing:
                signal_exit = run_signals(signal_args)
                timing.annotate(
                    operation="wrote" if not signal_exit else "failed"
                )
            if signal_exit:
                failures += 1
                print(f"[{symbol}] signal calculations reported a failure.")

    return failures


def _provider_failure_blocks_loop_a(
    provider: object,
    *,
    quote_only_capture: bool,
) -> bool:
    normalized = str(provider).strip().lower()
    return not (
        quote_only_capture and normalized in NON_BLOCKING_QUOTE_ONLY_PROVIDERS
    )


def read_watchlist(path: Path) -> tuple[str, ...]:
    if not path.is_file():
        raise FileNotFoundError(f"Watchlist file does not exist: {path}")
    symbols: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if value:
            symbols.append(value)
    return normalize_symbols(symbols)


def normalize_symbols(values: Iterable[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        symbol = value.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        normalized.append(symbol)
    return tuple(normalized)


def resolve_profile(store: ParquetStore, symbol: str, requested_profile: str) -> str:
    del store, symbol
    normalized = requested_profile.strip().lower()
    allowed = {"auto", *FETCH_PROFILES}
    if normalized not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"Orchestration fetch mode must be one of: {choices}")
    return "continuation"


def next_boundary(now: datetime, *, interval_minutes: int) -> datetime:
    current = now.astimezone(timezone.utc)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_minutes = int((current - midnight).total_seconds() // 60)
    next_slot = ((elapsed_minutes // interval_minutes) + 1) * interval_minutes
    boundary = midnight + timedelta(minutes=next_slot)
    if boundary <= current:
        boundary += timedelta(minutes=interval_minutes)
    return boundary


@contextmanager
def orchestration_lock(path: Path) -> Iterator[None]:
    with exclusive_runtime_lock(
        path,
        process_name="Duckets orchestration process",
    ):
        yield


if __name__ == "__main__":
    raise SystemExit(main())
