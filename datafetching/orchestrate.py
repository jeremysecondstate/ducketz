from __future__ import annotations

import argparse
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

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
from datafetching.parquet_store import DATASTORE_TARGETS, ParquetStore
from fundamentals.main import main as run_fundamentals
from signals.main import main as run_signals
from technicals.main import main as run_technicals

DEFAULT_WATCHLIST = Path(__file__).resolve().parent / "watchlist.txt"


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
    print("Loop B input: current normalized, fundamental, technical, and signal Parquets")
    print("Stop: Ctrl+C")
    print()

    lock_path = store.root_dir / ".ducketz-orchestration.lock"
    with orchestration_lock(lock_path):
        try:
            while True:
                cycle_started_at = datetime.now(timezone.utc)
                with datastore_cycle_lock(store.root_dir, reporter=print):
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
                            include_cme=not args.skip_cme,
                            run_technical_calculations=not args.skip_technicals,
                            datastore_target=args.datastore_target,
                            datastore_path=args.datastore,
                            run_fundamental_calculations=not args.skip_fundamentals,
                            run_signal_calculations=not args.skip_signals,
                            cycle_started_at=cycle_started_at,
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
                if args.once:
                    return 1 if failures else 0

                next_run = next_boundary(
                    datetime.now(timezone.utc),
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
    run_fundamental_calculations: bool = True,
    run_signal_calculations: bool = True,
    cycle_started_at: datetime | None = None,
) -> int:
    failures = 0
    providers_tuple = tuple(providers)
    symbols_tuple = tuple(symbols)
    started_at = cycle_started_at or datetime.now(timezone.utc)
    print(f"CYCLE {started_at.isoformat()}")
    print("-" * 48)
    if not symbols_tuple:
        return 0

    profiles = {
        symbol: resolve_profile(store, symbol, requested_profile)
        for symbol in symbols_tuple
    }
    for symbol in symbols_tuple:
        print(f"[{symbol}] fetch mode: {profiles[symbol]}")

    if len(symbols_tuple) == 1:
        symbol = symbols_tuple[0]
        fetch_results = {
            symbol: run_symbol_fetch(
                symbol,
                store,
                providers=providers_tuple,
                profile=profiles[symbol],
                include_cme=include_cme,
                include_fmp_macro=True,
            )
        }
    else:
        fetch_results = run_symbols_fetch(
            symbols_tuple,
            store,
            providers=providers_tuple,
            profile=profiles[symbols_tuple[0]],
            include_cme=include_cme,
            include_fmp_macro=True,
        )

    for index, symbol in enumerate(symbols_tuple):
        symbol_providers = providers_tuple if index == 0 else tuple(
            provider for provider in providers_tuple if provider != "fred"
        )
        results = fetch_results[symbol]
        changed = sum(result.data_files for result in results)
        provider_errors = sum(result.error_files for result in results)
        local_advisories = sum(result.advisory_files for result in results)
        failures += provider_errors
        error_breakdown = ", ".join(
            f"{result.provider}={result.error_files}"
            for result in results
            if result.error_files
        )
        detail = f" ({error_breakdown})" if error_breakdown else ""
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
            f"hard failures: {provider_errors}{detail}; "
            f"local advisories: {local_advisories}{advisory_detail}"
        )

        if run_fundamental_calculations and "fmp" in symbol_providers:
            fundamental_args = [symbol]
            if datastore_target:
                fundamental_args.extend(["--datastore-target", datastore_target])
            elif datastore_path:
                fundamental_args.extend(["--datastore", str(datastore_path)])
            fundamental_exit = run_fundamentals(fundamental_args)
            if fundamental_exit:
                failures += 1
                print(f"[{symbol}] fundamental calculations reported a failure.")

        if run_technical_calculations:
            technical_args = [symbol]
            if datastore_target:
                technical_args.extend(["--datastore-target", datastore_target])
            elif datastore_path:
                technical_args.extend(["--datastore", str(datastore_path)])
            technical_exit = run_technicals(technical_args)
            if technical_exit:
                failures += 1
                print(f"[{symbol}] technical calculations reported a failure.")

        if run_signal_calculations:
            signal_args = [symbol]
            if datastore_target:
                signal_args.extend(["--datastore-target", datastore_target])
            elif datastore_path:
                signal_args.extend(["--datastore", str(datastore_path)])
            signal_exit = run_signals(signal_args)
            if signal_exit:
                failures += 1
                print(f"[{symbol}] signal calculations reported a failure.")

    return failures


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
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        payload = (
            f"pid={os.getpid()}\n"
            f"started_at={datetime.now(timezone.utc).isoformat()}\n"
        ).encode("utf-8")
        os.write(descriptor, payload)
        os.close(descriptor)
        descriptor = None
    except FileExistsError as exc:
        detail = (
            path.read_text(encoding="utf-8", errors="replace")
            if path.is_file()
            else ""
        )
        raise RuntimeError(
            f"Another Duckets orchestration process appears to be running. "
            f"Lock: {path}\n{detail}"
        ) from exc

    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
