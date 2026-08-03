from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datafetching import FetchResult
from datafetching.databento_fetch import fetch as fetch_databento
from datafetching.databento_fetch import fetch_many as fetch_databento_many
from datafetching.fmp_fetch import fetch as fetch_fmp
from datafetching.fmp_fetch import fetch_many as fetch_fmp_many
from datafetching.fred_fetch import fetch as fetch_fred
from datafetching.layout import safe_token
from datafetching.parquet_store import DATASTORE_TARGETS, ParquetStore
from datafetching.schwab_fetch import fetch as fetch_schwab
from datafetching.schwab_fetch import fetch_many as fetch_schwab_many
from datafetching.sec_fetch import fetch as fetch_sec

PROVIDERS = ("databento", "fmp", "fred", "schwab", "sec")
FETCH_PROFILES = ("continuation", "full", "incremental")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch provider data and upsert raw/normalized Parquet datasets."
    )
    parser.add_argument("symbol", help="Equity symbol, for example NVDA.")
    parser.add_argument(
        "--providers",
        nargs="+",
        choices=PROVIDERS,
        default=list(PROVIDERS),
        help="Provider lanes to run. Defaults to all providers.",
    )
    parser.add_argument(
        "--profile",
        choices=FETCH_PROFILES,
        default="continuation",
        help=(
            "Continue every persisted dataset from its own latest timestamp. "
            "The legacy full and incremental names are accepted as aliases."
        ),
    )
    datastore_group = parser.add_mutually_exclusive_group()
    datastore_group.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default=None,
        help=(
            "Named output target: 'pc' writes to C:\\DATASTORE; "
            "'local' writes to datafetching\\datastore in this checkout."
        ),
    )
    datastore_group.add_argument(
        "--datastore",
        type=Path,
        default=None,
        help=(
            "Custom output path. Without an explicit target/path, "
            "DUCKETS_DATASTORE_DIR, DUCKETS_OHLCV_PARQUET_DIR, or C:\\DATASTORE is used."
        ),
    )
    parser.add_argument(
        "--skip-cme",
        action="store_true",
        help="Skip the Databento CME context fetch.",
    )
    args = parser.parse_args(argv)

    symbol = args.symbol.strip().upper()
    if not symbol:
        parser.error("symbol is required")

    store = ParquetStore(args.datastore, target=args.datastore_target)
    profile = normalize_fetch_profile(args.profile)
    print("DUCKETS DATA FETCHING")
    print("=====================")
    print(f"Symbol: {symbol}")
    print(f"DATASTORE: {store.root_dir}")
    print(f"Stock folder: {store.root_dir / 'stocks' / safe_token(symbol)}")
    print(f"Shared pools: {store.root_dir / 'pools'}")
    print(f"Providers: {', '.join(args.providers)}")
    print(f"Fetch mode: {profile}")
    print()

    results = run_symbol_fetch(
        symbol,
        store,
        providers=args.providers,
        profile=profile,
        include_cme=not args.skip_cme,
        include_fmp_macro=True,
    )
    for result in results:
        print(
            f"[{result.provider}] changed parquet files: {result.data_files}; "
            f"hard failures: {result.error_files}; "
            f"local advisories: {result.advisory_files}"
        )

    total_data = sum(result.data_files for result in results)
    total_errors = sum(result.error_files for result in results)
    total_advisories = sum(result.advisory_files for result in results)
    print()
    print("FETCH SUMMARY")
    print("=============")
    print(f"Changed parquet files: {total_data}")
    print(f"Hard failures: {total_errors}")
    print(f"Local advisories: {total_advisories}")
    return 1 if total_errors else 0


def run_symbol_fetch(
    symbol: str,
    store: ParquetStore,
    *,
    providers: Iterable[str] = PROVIDERS,
    profile: str = "continuation",
    include_cme: bool = True,
    include_fmp_macro: bool = True,
) -> tuple[FetchResult, ...]:
    effective_profile = normalize_fetch_profile(profile)
    results: list[FetchResult] = []
    for provider in providers:
        print(f"[{symbol}/{provider}] fetching...")
        result = run_provider_fetch(
            provider,
            symbol,
            store,
            profile=effective_profile,
            include_cme=include_cme,
            include_fmp_macro=include_fmp_macro,
        )
        results.append(result)
    return tuple(results)


def run_symbols_fetch(
    symbols: Iterable[str],
    store: ParquetStore,
    *,
    providers: Iterable[str] = PROVIDERS,
    profile: str = "continuation",
    include_cme: bool = True,
    include_fmp_macro: bool = True,
) -> dict[str, tuple[FetchResult, ...]]:
    """Fetch a watchlist provider-by-provider, batching supported requests."""
    clean_symbols = tuple(
        dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in symbols
            if str(symbol).strip()
        )
    )
    if not clean_symbols:
        raise ValueError("At least one symbol is required.")
    if len(clean_symbols) == 1:
        symbol = clean_symbols[0]
        return {
            symbol: run_symbol_fetch(
                symbol,
                store,
                providers=providers,
                profile=profile,
                include_cme=include_cme,
                include_fmp_macro=include_fmp_macro,
            )
        }

    effective_profile = normalize_fetch_profile(profile)
    results: dict[str, list[FetchResult]] = {
        symbol: [] for symbol in clean_symbols
    }
    for provider in providers:
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider: {provider}")
        if provider == "fred":
            first_symbol = clean_symbols[0]
            print(f"[{first_symbol}/fred] fetching shared macro series...")
            results[first_symbol].append(
                run_provider_fetch(
                    provider,
                    first_symbol,
                    store,
                    profile=effective_profile,
                    include_cme=False,
                    include_fmp_macro=False,
                )
            )
            continue

        print(
            f"[{provider}] fetching {len(clean_symbols)} symbols "
            "with provider batching where supported..."
        )
        try:
            if provider == "databento":
                batch_results = fetch_databento_many(
                    clean_symbols,
                    store,
                    include_cme=include_cme,
                    profile=effective_profile,
                )
            elif provider == "fmp":
                batch_results = fetch_fmp_many(
                    clean_symbols,
                    store,
                    include_macro=include_fmp_macro,
                )
            elif provider == "schwab":
                batch_results = fetch_schwab_many(
                    clean_symbols,
                    store,
                    profile=effective_profile,
                )
            else:
                batch_results = {}
        except Exception as exc:
            print(
                f"[{provider}] batch lane failed before completion "
                f"({type(exc).__name__}: {exc}); retrying symbols separately"
            )
            batch_results = {}

        for index, symbol in enumerate(clean_symbols):
            result = batch_results.get(symbol)
            if result is None:
                result = run_provider_fetch(
                    provider,
                    symbol,
                    store,
                    profile=effective_profile,
                    include_cme=include_cme and index == 0,
                    include_fmp_macro=include_fmp_macro and index == 0,
                )
            results[symbol].append(result)

    return {
        symbol: tuple(symbol_results)
        for symbol, symbol_results in results.items()
    }


def run_provider_fetch(
    provider: str,
    symbol: str,
    store: ParquetStore,
    *,
    profile: str,
    include_cme: bool,
    include_fmp_macro: bool,
) -> FetchResult:
    effective_profile = normalize_fetch_profile(profile)
    runners: dict[str, Callable[[], FetchResult]] = {
        "databento": lambda: fetch_databento(
            symbol,
            store,
            include_cme=include_cme,
            profile=effective_profile,
        ),
        "fmp": lambda: fetch_fmp(symbol, store, include_macro=include_fmp_macro),
        "fred": lambda: fetch_fred(symbol, store),
        "schwab": lambda: fetch_schwab(symbol, store, profile=effective_profile),
        "sec": lambda: fetch_sec(symbol, store),
    }
    if provider not in runners:
        raise ValueError(f"Unknown provider: {provider}")

    try:
        return runners[provider]()
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        print(f"[{symbol}/{provider}] provider lane failed: {detail}")
        try:
            error_path = store.save_error(
                source=provider,
                category="orchestration",
                symbol=symbol,
                request_key="provider_fetch",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            if error_path is not None:
                print(f"[{symbol}/{provider}] orchestration error upserted: {error_path}")
        except Exception as store_exc:
            print(
                f"[{symbol}/{provider}] could not persist the orchestration error: "
                f"{type(store_exc).__name__}: {store_exc}"
            )
        return FetchResult(provider, 0, 1)


def normalize_fetch_profile(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in FETCH_PROFILES:
        choices = ", ".join(FETCH_PROFILES)
        raise ValueError(f"Fetch profile must be one of: {choices}")
    return "continuation"


if __name__ == "__main__":
    raise SystemExit(main())
