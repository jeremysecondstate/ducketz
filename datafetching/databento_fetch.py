from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

import pandas as pd
import pyarrow.parquet as pq
from dotenv import load_dotenv

from app.models.market_data import MarketBar
from app.services.databento_cme_context import (
    DatabentoCmeContextProvider,
    DatabentoCmeContextSpec,
)
from app.services.databento_market_data import DatabentoMarketDataProvider
from app.services.databento_retry import call_with_persistent_databento_retry
from datafetching import FetchResult
from datafetching.bar_timing import (
    completed_market_bars,
    finalize_normalized_bar_parquets,
)
from datafetching.continuation import (
    latest_normalized_bar_timestamp,
    normalized_bar_path,
)
from datafetching.cme_cross_asset_context import (
    CmeCrossAssetNotReady,
    CmeCrossAssetQualityError,
    materialize_cme_cross_asset_context,
)
from datafetching.databento_archive import (
    configured_equity_archive_dataset,
    materialize_equity_archive_baseline,
)
from datafetching.decision_time import completed_bar_clock_for_target
from datafetching.cme_history import cme_writer_lock_path
from datafetching.derived_bars import (
    DERIVED_INTRADAY_FREQUENCIES,
    derive_daily_bars,
    derive_intraday_bars,
    latest_completed_equity_session,
)
from datafetching.layout import pool_data_folder, safe_token
from datafetching.observability import timed_stage
from datafetching.parquet_store import ParquetStore
from datafetching.runtime_lock import exclusive_runtime_lock

DATABENTO_MAX_SYMBOLS_PER_REQUEST = 2_000
DATABENTO_HISTORICAL_TARGET_MAX_LAG_SECONDS = 20 * 60
REPOSITORY_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class DatabentoTargetRecoveryError(RuntimeError):
    """The bounded Historical-API target recovery could not prove exact bars."""


def recover_historical_minute_target(
    symbols: Iterable[str],
    store: ParquetStore,
    *,
    target_snapshot_for: object,
    timeout_seconds: float,
    poll_seconds: float = 10.0,
    provider: DatabentoMarketDataProvider | None = None,
    clock: Callable[[], object] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> Mapping[str, object]:
    """Recover one exact all-symbol 1m boundary after Historical publication lag.

    Databento Historical availability is polled before any retry request.  The
    retry is therefore tied to the provider's advertised ``ohlcv-1m`` end bound,
    not to an arbitrary sleep.  The function persists only completed bars and
    returns only after every requested symbol resolves the exact target; it does
    not publish Loop A readiness itself.
    """

    if timeout_seconds < 0:
        raise ValueError("Databento target recovery timeout cannot be negative")
    if poll_seconds <= 0:
        raise ValueError("Databento target recovery poll interval must be positive")
    clean_symbols = _normalize_symbols(symbols)
    target = pd.to_datetime(target_snapshot_for, utc=True, errors="coerce")
    if pd.isna(target):
        raise ValueError("Databento target recovery requires a valid UTC target")
    target = pd.Timestamp(target)
    now = clock or (lambda: datetime.now(timezone.utc))
    started_at = _utc_timestamp(now(), label="recovery clock")
    causal_deadline = target + pd.Timedelta(
        seconds=DATABENTO_HISTORICAL_TARGET_MAX_LAG_SECONDS
    )
    configured_deadline = started_at + pd.Timedelta(seconds=float(timeout_seconds))
    deadline = min(causal_deadline, configured_deadline)
    monotonic_deadline = monotonic_clock() + max(
        0.0, (deadline - started_at).total_seconds()
    )
    _load_repository_environment()
    effective_provider = provider or DatabentoMarketDataProvider()
    minute_specs = tuple(
        spec
        for spec in effective_provider.native_specs()
        if str(getattr(spec, "frequency", "")).strip().lower() == "1m"
    )
    if len(minute_specs) != 1:
        raise DatabentoTargetRecoveryError(
            "Databento Historical recovery requires exactly one native 1m spec"
        )
    spec = minute_specs[0]
    attempts = 0
    last_state = "PROVIDER_RANGE_NOT_CHECKED"
    advertised_end: pd.Timestamp | None = None

    while True:
        observed_at = _utc_timestamp(now(), label="recovery clock")
        if observed_at > causal_deadline:
            last_state = "CAUSAL_DEADLINE_EXCEEDED"
        else:
            try:
                range_payload = effective_provider.dataset_range()
                available_range = effective_provider.available_range_for_schema(
                    spec.schema,
                    dataset_range=range_payload,
                )
                advertised_end = _utc_timestamp(
                    available_range.end,
                    label="Databento available range end",
                )
                if advertised_end >= target:
                    attempts += 1
                    request_start = max(
                        _utc_timestamp(
                            available_range.start,
                            label="Databento available range start",
                        ),
                        target - _continuation_overlap("1m"),
                    )
                    request_spec = replace(
                        spec,
                        lookback=pd.Timedelta(
                            max(
                                _continuation_overlap("1m"),
                                advertised_end - request_start,
                            )
                        ).to_pytimedelta(),
                    )
                    fetched, selected_range = effective_provider.fetch_native_bars_many(
                        clean_symbols,
                        request_spec,
                        available_range=available_range,
                    )
                    missing = tuple(
                        symbol for symbol in clean_symbols if symbol not in fetched
                    )
                    if missing:
                        raise DatabentoTargetRecoveryError(
                            "Databento recovery omitted symbol(s): "
                            + ", ".join(missing)
                        )
                    persisted_at = max(observed_at, target)
                    for symbol in clean_symbols:
                        bars, raw_frame = fetched[symbol]
                        _persist_native_results(
                            symbol,
                            store,
                            provider=effective_provider,
                            profile="continuation",
                            observed_at=persisted_at.to_pydatetime(),
                            native_results=(
                                (spec, bars, raw_frame, selected_range, None),
                            ),
                            batch_watchlist_symbol_count=len(clean_symbols),
                            run_derived=False,
                        )
                    for symbol in clean_symbols:
                        completed_bar_clock_for_target(
                            store.root_dir,
                            symbol=symbol,
                            target_snapshot_for=target,
                            as_of=persisted_at,
                        )
                    return {
                        "status": "EXACT_TARGET_RECOVERED",
                        "target_snapshot_for": target.isoformat(),
                        "provider_schema": spec.schema,
                        "provider_available_end": advertised_end.isoformat(),
                        "attempts": attempts,
                        "symbols": clean_symbols,
                        "observed_at": persisted_at.isoformat(),
                    }
                last_state = "PROVIDER_TARGET_NOT_YET_AVAILABLE"
            except DatabentoTargetRecoveryError:
                raise
            except Exception as exc:
                last_state = f"{type(exc).__name__}: {exc}"

        remaining = monotonic_deadline - monotonic_clock()
        if remaining <= 0 or observed_at >= deadline:
            advertised = (
                advertised_end.isoformat() if advertised_end is not None else "UNKNOWN"
            )
            raise DatabentoTargetRecoveryError(
                "Databento Historical exact-target recovery deadline missed; "
                f"target={target.isoformat()}; advertised_end={advertised}; "
                f"state={last_state}; attempts={attempts}"
            )
        sleeper(min(float(poll_seconds), remaining))


def _utc_timestamp(value: object, *, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError(f"Invalid {label}")
    return pd.Timestamp(timestamp)


def fetch(
    symbol: str,
    store: ParquetStore,
    *,
    include_cme: bool = True,
    profile: str = "continuation",
    minute_bars_completed: Callable[[Mapping[str, FetchResult]], None] | None = None,
) -> FetchResult:
    """Fetch Databento data, continuing every native bar dataset from stored time.

    Raw provider frames are retained as returned, including a potentially active
    candle. Normalized equity-bar Parquets are a finalized-candle dataset and
    therefore contain only intervals completed as of this fetch cycle.
    """
    _load_repository_environment()
    provider = DatabentoMarketDataProvider()
    observed_at = datetime.now(timezone.utc)
    _materialize_archive_baseline(
        store,
        provider=provider,
        symbols=(symbol,),
        observed_at=observed_at,
    )
    minute_result: FetchResult | None = None

    def on_spec_completed(spec: object, results: list[tuple]) -> None:
        nonlocal minute_result
        if getattr(spec, "frequency", None) != "1m":
            return
        minute_result = _persist_native_results(
            symbol,
            store,
            provider=provider,
            profile=profile,
            observed_at=observed_at,
            native_results=results,
            run_derived=False,
        )
        if minute_bars_completed is not None:
            minute_bars_completed({symbol: minute_result})

    native_results = list(
        _fetch_native_results(
            provider,
            symbol,
            profile,
            store,
            spec_completed=on_spec_completed,
        )
    )
    result = _persist_native_results(
        symbol,
        store,
        provider=provider,
        profile=profile,
        observed_at=observed_at,
        native_results=native_results,
        skip_native_frequencies=(
            frozenset(("1m",)) if minute_result is not None else frozenset()
        ),
    )
    if minute_result is not None:
        result = _combine_results(minute_result, result)

    if include_cme:
        result = _with_shared_cme_result(result, store)
    return result


def fetch_many(
    symbols: Iterable[str],
    store: ParquetStore,
    *,
    include_cme: bool = True,
    profile: str = "continuation",
    minute_bars_completed: Callable[[Mapping[str, FetchResult]], None] | None = None,
) -> dict[str, FetchResult]:
    """Fetch Databento bars for a watchlist using multi-symbol requests."""
    requested_symbols = tuple(symbols)
    with timed_stage(
        "loop-a.databento-watchlist",
        provider="databento",
        reporter=print,
        extra={
            "symbol_count": len(requested_symbols),
            "cme_mode": "inline" if include_cme else "external",
        },
    ) as timing:
        results = _fetch_many(
            requested_symbols,
            store,
            include_cme=include_cme,
            profile=profile,
            minute_bars_completed=minute_bars_completed,
        )
        timing.annotate(row_count=len(results), operation="wrote")
        return results


def _fetch_many(
    symbols: Iterable[str],
    store: ParquetStore,
    *,
    include_cme: bool = True,
    profile: str = "continuation",
    minute_bars_completed: Callable[[Mapping[str, FetchResult]], None] | None = None,
) -> dict[str, FetchResult]:
    clean_symbols = _normalize_symbols(symbols)
    if len(clean_symbols) == 1:
        symbol = clean_symbols[0]
        return {
            symbol: fetch(
                symbol,
                store,
                include_cme=include_cme,
                profile=profile,
                minute_bars_completed=minute_bars_completed,
            )
        }

    _load_repository_environment()
    provider = DatabentoMarketDataProvider()
    observed_at = datetime.now(timezone.utc)
    _materialize_archive_baseline(
        store,
        provider=provider,
        symbols=clean_symbols,
        observed_at=observed_at,
    )
    minute_results: dict[str, FetchResult] = {}

    def on_spec_completed(
        spec: object,
        results: Mapping[str, list[tuple]],
    ) -> None:
        if getattr(spec, "frequency", None) != "1m":
            return
        for symbol in clean_symbols:
            minute_results[symbol] = _persist_native_results(
                symbol,
                store,
                provider=provider,
                profile=profile,
                observed_at=observed_at,
                native_results=results[symbol],
                batch_watchlist_symbol_count=len(clean_symbols),
                run_derived=False,
            )
        if minute_bars_completed is not None:
            minute_bars_completed(dict(minute_results))

    native_results = _fetch_native_results_many(
        provider,
        clean_symbols,
        profile,
        store,
        spec_completed=on_spec_completed,
    )
    results = {
        symbol: _persist_native_results(
            symbol,
            store,
            provider=provider,
            profile=profile,
            observed_at=observed_at,
            native_results=native_results[symbol],
            batch_watchlist_symbol_count=len(clean_symbols),
            skip_native_frequencies=(
                frozenset(("1m",))
                if symbol in minute_results
                else frozenset()
            ),
        )
        for symbol in clean_symbols
    }
    for symbol, minute_result in minute_results.items():
        results[symbol] = _combine_results(minute_result, results[symbol])
    if include_cme:
        first_symbol = clean_symbols[0]
        results[first_symbol] = _with_shared_cme_result(
            results[first_symbol],
            store,
        )
    return results


def _persist_native_results(
    symbol: str,
    store: ParquetStore,
    *,
    provider: DatabentoMarketDataProvider,
    profile: str,
    observed_at: datetime,
    native_results: Iterable[tuple],
    batch_watchlist_symbol_count: int = 1,
    skip_native_frequencies: frozenset[str] = frozenset(),
    run_derived: bool = True,
) -> FetchResult:
    data_files = 0
    error_files = 0
    advisory_files = 0
    source_bars_by_frequency: dict[str, list] = {}
    source_specs_by_frequency = {}
    source_ranges_by_frequency = {}
    declared_specs_by_frequency = {}

    for spec, bars, raw_frame, available_range, exc in native_results:
        declared_specs_by_frequency[spec.frequency] = spec
        metadata = {
            "provider_dataset": provider.dataset,
            "source_schema": spec.schema,
            "source_frequency": spec.frequency,
            "output_frequency": spec.frequency,
            "aggregation_method": "native",
            "fetch_profile": profile,
            "price_basis": "unadjusted_market_scale",
            "volume_basis": "unadjusted_market_scale",
            "corporate_action_adjustment": "none",
            "normalized_bar_policy": "completed_intervals_only",
            "batch_watchlist_symbol_count": batch_watchlist_symbol_count,
        }
        if available_range is not None:
            metadata.update(
                {
                    "range_start": available_range.start.isoformat(),
                    "range_end": available_range.end.isoformat(),
                }
            )

        if spec.frequency in skip_native_frequencies:
            if exc is None:
                completed_bars = completed_market_bars(
                    bars,
                    timeframe=spec.frequency,
                    as_of=observed_at,
                )
                source_bars_by_frequency[spec.frequency] = completed_bars
                source_specs_by_frequency[spec.frequency] = spec
                source_ranges_by_frequency[spec.frequency] = available_range
            continue

        request_key = f"{spec.key}_{spec.schema}_{spec.frequency}"
        if exc is not None:
            store.save_error(
                source="databento",
                category="bars",
                symbol=symbol,
                request_key=request_key,
                error_type=type(exc).__name__,
                error_message=str(exc),
                metadata=metadata,
            )
            error_files += 1
            continue

        completed_bars = completed_market_bars(
            bars,
            timeframe=spec.frequency,
            as_of=observed_at,
        )
        source_bars_by_frequency[spec.frequency] = completed_bars
        source_specs_by_frequency[spec.frequency] = spec
        source_ranges_by_frequency[spec.frequency] = available_range

        raw_path = None
        if raw_frame is not None:
            raw_path = store.save_raw_frame(
                source="databento",
                category="bars",
                symbol=symbol,
                endpoint=f"{request_key}_raw",
                frame=raw_frame,
                timeframe=spec.frequency,
                metadata=metadata,
            )
        if raw_path is not None:
            data_files += 1

        if store.save_bars(
            "databento",
            symbol,
            spec.frequency,
            completed_bars,
            request_key=request_key,
            metadata=metadata,
            as_of=observed_at,
        ) is not None:
            data_files += 1

        # Clean any active candle persisted by an earlier code version. This is
        # intentionally performed even when the current fetch had no complete rows.
        finalize_normalized_bar_parquets(
            store.root_dir,
            source="databento",
            symbol=symbol,
            timeframe=spec.frequency,
            as_of=observed_at,
        )

    if run_derived:
        derived_files, derived_errors = _save_derived_intraday_bars(
            symbol,
            store,
            provider=provider,
            profile=profile,
            source_bars=source_bars_by_frequency.get("1m", []),
            source_spec=source_specs_by_frequency.get("1m"),
            source_range=source_ranges_by_frequency.get("1m"),
            observed_at=observed_at,
        )
        data_files += derived_files
        error_files += derived_errors

        daily_files, daily_errors = _save_derived_daily_bars(
            symbol,
            store,
            provider=provider,
            profile=profile,
            minute_source_spec=source_specs_by_frequency.get("1m"),
            daily_source_spec=declared_specs_by_frequency.get("1d"),
            observed_at=observed_at,
        )
        data_files += daily_files
        error_files += daily_errors

    return FetchResult("databento", data_files, error_files, advisory_files)


def _materialize_archive_baseline(
    store: ParquetStore,
    *,
    provider: DatabentoMarketDataProvider,
    symbols: Iterable[str],
    observed_at: datetime,
) -> None:
    live_dataset = str(getattr(provider, "dataset", "") or "").strip()
    native_specs = getattr(provider, "native_specs", None)
    if not live_dataset or not callable(native_specs):
        return
    archive_dataset = configured_equity_archive_dataset()
    if archive_dataset != live_dataset:
        print(
            "[databento/archive-bridge] cold archive retained as provenance; "
            f"operational_dataset={live_dataset}; archive_dataset={archive_dataset}"
        )
        return
    result = materialize_equity_archive_baseline(
        store.root_dir,
        symbols=symbols,
        live_dataset=live_dataset,
        source_specs=native_specs(),
        archive_dataset=archive_dataset,
        as_of=observed_at,
    )
    if result.materialized_files:
        print(
            "[databento/archive-bridge] seeded "
            f"{result.materialized_files} Loop A files with "
            f"{result.archive_rows:,} verified {result.dataset} rows"
        )


def _load_repository_environment() -> None:
    load_dotenv(dotenv_path=REPOSITORY_ENV_FILE, override=False)


def _with_shared_cme_result(result: FetchResult, store: ParquetStore) -> FetchResult:
    data_files, error_files, advisory_files = _fetch_cme(store)
    return FetchResult(
        "databento",
        result.data_files + data_files,
        result.error_files + error_files,
        result.advisory_files + advisory_files,
    )


def _save_derived_intraday_bars(
    symbol: str,
    store: ParquetStore,
    *,
    provider: DatabentoMarketDataProvider,
    profile: str,
    source_bars: list,
    source_spec,
    source_range,
    observed_at: datetime,
) -> tuple[int, int]:
    if not source_bars or source_spec is None:
        return 0, 0

    data_files = 0
    error_files = 0
    for output_frequency in DERIVED_INTRADAY_FREQUENCIES:
        request_key = f"derived_1m_{output_frequency}"
        use_proven_sparse_hour = output_frequency == "1h" and source_range is not None
        metadata = {
            "provider_dataset": provider.dataset,
            "source_schema": source_spec.schema,
            "source_frequency": "1m",
            "output_frequency": output_frequency,
            "aggregation_method": (
                "coverage_proven_sparse_hour_from_complete_1m"
                if use_proven_sparse_hour
                else "session_resampled_from_complete_1m"
            ),
            "fetch_profile": profile,
            "price_basis": "unadjusted_market_scale",
            "volume_basis": (
                "summed_trade_volume_or_zero_for_proven_empty_hour"
                if use_proven_sparse_hour
                else "summed_from_complete_1m"
            ),
            "corporate_action_adjustment": "none",
            "normalized_bar_policy": (
                "completed_coverage_proven_sparse_intervals_only"
                if use_proven_sparse_hour
                else "completed_intervals_only"
            ),
        }
        if use_proven_sparse_hour:
            metadata.update(
                {
                    "range_start": source_range.start.isoformat(),
                    "range_end": source_range.end.isoformat(),
                    "no_trade_price_policy": "strictly_prior_close_never_future_fill",
                }
            )
        try:
            bars = derive_intraday_bars(
                symbol,
                source_bars,
                output_frequency,
                as_of=observed_at,
                coverage_start=(
                    source_range.start if use_proven_sparse_hour else None
                ),
                coverage_end=(
                    source_range.end if use_proven_sparse_hour else None
                ),
            )
            if store.save_bars(
                "databento",
                symbol,
                output_frequency,
                bars,
                request_key=request_key,
                metadata=metadata,
                as_of=observed_at,
            ) is not None:
                data_files += 1
            finalize_normalized_bar_parquets(
                store.root_dir,
                source="databento",
                symbol=symbol,
                timeframe=output_frequency,
                as_of=observed_at,
            )
        except Exception as exc:
            store.save_error(
                source="databento",
                category="bars",
                symbol=symbol,
                request_key=request_key,
                error_type=type(exc).__name__,
                error_message=str(exc),
                metadata=metadata,
            )
            error_files += 1
    return data_files, error_files


def _save_derived_daily_bars(
    symbol: str,
    store: ParquetStore,
    *,
    provider: DatabentoMarketDataProvider,
    profile: str,
    minute_source_spec,
    daily_source_spec,
    observed_at: datetime,
) -> tuple[int, int]:
    """Fill the post-close native-daily publication lag from complete 1m bars."""

    if minute_source_spec is None or daily_source_spec is None:
        return 0, 0
    completed_session = latest_completed_equity_session(observed_at)
    if completed_session is None:
        return 0, 0

    minute_request_key = (
        f"{minute_source_spec.key}_{minute_source_spec.schema}_"
        f"{minute_source_spec.frequency}"
    )
    native_daily_request_key = (
        f"{daily_source_spec.key}_{daily_source_spec.schema}_"
        f"{daily_source_spec.frequency}"
    )
    derived_request_key = "derived_1m_1d"
    latest_daily_labels = [
        value
        for value in (
            latest_normalized_bar_timestamp(
                store.root_dir,
                source="databento",
                symbol=symbol,
                timeframe="1d",
                request_key=native_daily_request_key,
            ),
            latest_normalized_bar_timestamp(
                store.root_dir,
                source="databento",
                symbol=symbol,
                timeframe="1d",
                request_key=derived_request_key,
            ),
        )
        if value is not None
    ]
    if latest_daily_labels and max(
        _utc_session_label(value) for value in latest_daily_labels
    ) >= completed_session.session_label:
        return 0, 0

    source_path = normalized_bar_path(
        store.root_dir,
        source="databento",
        symbol=symbol,
        timeframe="1m",
        request_key=minute_request_key,
    )
    if not source_path.is_file():
        return 0, 0

    metadata = {
        "provider_dataset": provider.dataset,
        "source_schema": minute_source_spec.schema,
        "source_frequency": "1m",
        "output_frequency": "1d",
        "aggregation_method": "regular_session_trade_ohlcv_resampled_from_1m",
        "fetch_profile": profile,
        "price_basis": "unadjusted_market_scale",
        "volume_basis": "summed_from_regular_session_trade_1m",
        "corporate_action_adjustment": "none",
        "normalized_bar_policy": "completed_exchange_sessions_only",
        "exchange_calendar": "XNAS",
    }
    try:
        frame = pd.read_parquet(
            source_path,
            columns=("timestamp", "open", "high", "low", "close", "volume"),
            filters=[
                (
                    "timestamp",
                    ">=",
                    completed_session.open_timestamp.to_pydatetime(),
                ),
                (
                    "timestamp",
                    "<",
                    completed_session.close_timestamp.to_pydatetime(),
                ),
            ],
        )
        source_bars = [
            MarketBar(
                symbol=symbol,
                source="databento",
                timeframe="1m",
                timestamp=pd.Timestamp(row.timestamp).to_pydatetime(),
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume or 0.0),
            )
            for row in frame.itertuples(index=False)
        ]
        bars = derive_daily_bars(
            symbol,
            source_bars,
            as_of=observed_at,
        )
        if not bars:
            return 0, 0
        changed = store.save_bars(
            "databento",
            symbol,
            "1d",
            bars,
            request_key=derived_request_key,
            metadata=metadata,
            as_of=observed_at,
        )
        finalize_normalized_bar_parquets(
            store.root_dir,
            source="databento",
            symbol=symbol,
            timeframe="1d",
            as_of=observed_at,
        )
        return int(changed is not None), 0
    except Exception as exc:
        store.save_error(
            source="databento",
            category="bars",
            symbol=symbol,
            request_key=derived_request_key,
            error_type=type(exc).__name__,
            error_message=str(exc),
            metadata=metadata,
        )
        return 0, 1


def _utc_session_label(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    timestamp = (
        timestamp.tz_localize("UTC")
        if timestamp.tzinfo is None
        else timestamp.tz_convert("UTC")
    )
    return timestamp.normalize()


def _fetch_native_results(
    provider: DatabentoMarketDataProvider,
    symbol: str,
    profile: str,
    store: ParquetStore,
    *,
    spec_completed: Callable[[object, list[tuple]], None] | None = None,
):
    normalized_profile = profile.strip().lower()
    if normalized_profile not in {"continuation", "full", "incremental"}:
        raise ValueError(
            "Databento fetch mode must be continuation; legacy full/incremental "
            "aliases are also accepted."
        )
    # Every native schema is refreshed on every iteration from that request's own
    # latest persisted timestamp.
    specs = tuple(
        sorted(
            provider.native_specs(),
            key=lambda spec: (0 if spec.frequency == "1m" else 1),
        )
    )
    try:
        range_payload = call_with_persistent_databento_retry(
            provider.dataset_range,
            operation_name="equities dataset-range metadata",
            schema="dataset-range",
            timing_reporter=print,
        )
    except Exception as exc:
        failed = [(spec, [], None, None, exc) for spec in specs]
        if spec_completed is not None:
            minute = [row for row in failed if row[0].frequency == "1m"]
            if minute:
                spec_completed(minute[0][0], minute)
        return failed

    results = []
    for spec in specs:
        request_key = f"{spec.key}_{spec.schema}_{spec.frequency}"
        try:
            available_range = provider.available_range_for_schema(
                spec.schema,
                dataset_range=range_payload,
            )
            latest_stored = latest_normalized_bar_timestamp(
                store.root_dir,
                source="databento",
                symbol=symbol,
                timeframe=spec.frequency,
                request_key=request_key,
            )
            request_spec = spec
            if latest_stored is not None:
                overlap = _continuation_overlap(spec.frequency)
                requested_start = min(
                    available_range.end,
                    max(available_range.start, latest_stored - overlap),
                )
                request_spec = replace(
                    spec,
                    lookback=max(overlap, available_range.end - requested_start),
                )
                print(
                    f"[{symbol}/databento/{request_key}] latest stored "
                    f"{latest_stored.isoformat()}; requesting missing tail from "
                    f"{requested_start.isoformat()}"
                )
            else:
                print(
                    f"[{symbol}/databento/{request_key}] no stored dataset; "
                    "requesting configured history"
                )
            bars, raw_frame, selected_range = call_with_persistent_databento_retry(
                lambda request_spec=request_spec, available_range=available_range: (
                    provider.fetch_native_bars(
                        symbol,
                        request_spec,
                        available_range=available_range,
                    )
                ),
                operation_name=f"{symbol} {spec.schema}/{spec.frequency}",
                symbol=symbol,
                schema=spec.schema,
                request_start=max(
                    available_range.start,
                    available_range.end - request_spec.lookback,
                ),
                request_end=available_range.end,
                timing_reporter=print,
            )
            results.append((spec, bars, raw_frame, selected_range, None))
        except Exception as exc:
            results.append((spec, [], None, None, exc))
        if spec_completed is not None:
            spec_completed(spec, [results[-1]])
    return results


def _combine_results(left: FetchResult, right: FetchResult) -> FetchResult:
    return FetchResult(
        "databento",
        left.data_files + right.data_files,
        left.error_files + right.error_files,
        left.advisory_files + right.advisory_files,
    )


def _fetch_native_results_many(
    provider: DatabentoMarketDataProvider,
    symbols: tuple[str, ...],
    profile: str,
    store: ParquetStore,
    *,
    spec_completed: Callable[[object, Mapping[str, list[tuple]]], None] | None = None,
) -> dict[str, list[tuple]]:
    normalized_profile = profile.strip().lower()
    if normalized_profile not in {"continuation", "full", "incremental"}:
        raise ValueError(
            "Databento fetch mode must be continuation; legacy full/incremental "
            "aliases are also accepted."
        )
    specs = tuple(
        sorted(
            provider.native_specs(),
            key=lambda spec: (0 if spec.frequency == "1m" else 1),
        )
    )
    results: dict[str, list[tuple]] = {symbol: [] for symbol in symbols}

    try:
        range_payload = call_with_persistent_databento_retry(
            provider.dataset_range,
            operation_name="equities dataset-range metadata",
            schema="dataset-range",
            timing_reporter=print,
        )
    except Exception as exc:
        for symbol in symbols:
            results[symbol].extend(
                (spec, [], None, None, exc) for spec in specs
            )
        if spec_completed is not None:
            minute_spec = next(
                (spec for spec in specs if spec.frequency == "1m"), None
            )
            if minute_spec is not None:
                spec_completed(
                    minute_spec,
                    {
                        symbol: [
                            next(
                                row
                                for row in results[symbol]
                                if row[0].frequency == "1m"
                            )
                        ]
                        for symbol in symbols
                    },
                )
        return results

    for spec in specs:
        request_key = f"{spec.key}_{spec.schema}_{spec.frequency}"
        try:
            available_range = provider.available_range_for_schema(
                spec.schema,
                dataset_range=range_payload,
            )
        except Exception as exc:
            for symbol in symbols:
                results[symbol].append((spec, [], None, None, exc))
            if spec_completed is not None:
                spec_completed(
                    spec,
                    {symbol: [results[symbol][-1]] for symbol in symbols},
                )
            continue

        symbols_by_start: dict[datetime, list[str]] = {}
        request_specs_by_start = {}
        for symbol in symbols:
            latest_stored = latest_normalized_bar_timestamp(
                store.root_dir,
                source="databento",
                symbol=symbol,
                timeframe=spec.frequency,
                request_key=request_key,
            )
            request_spec = spec
            if latest_stored is not None:
                overlap = _continuation_overlap(spec.frequency)
                requested_start = min(
                    available_range.end,
                    max(available_range.start, latest_stored - overlap),
                )
                request_spec = replace(
                    spec,
                    lookback=max(overlap, available_range.end - requested_start),
                )
                print(
                    f"[{symbol}/databento/{request_key}] latest stored "
                    f"{latest_stored.isoformat()}; requesting missing tail from "
                    f"{requested_start.isoformat()}"
                )
            else:
                print(
                    f"[{symbol}/databento/{request_key}] no stored dataset; "
                    "requesting configured history"
                )
            actual_start = max(
                available_range.start,
                available_range.end - request_spec.lookback,
            )
            symbols_by_start.setdefault(actual_start, []).append(symbol)
            request_specs_by_start[actual_start] = request_spec
        for actual_start, grouped_symbols in symbols_by_start.items():
            request_spec = request_specs_by_start[actual_start]
            for symbol_chunk in _symbol_chunks(grouped_symbols):
                try:
                    fetched, selected_range = call_with_persistent_databento_retry(
                        lambda symbol_chunk=symbol_chunk, request_spec=request_spec: (
                            provider.fetch_native_bars_many(
                                symbol_chunk,
                                request_spec,
                                available_range=available_range,
                            )
                        ),
                        operation_name=(
                            f"{len(symbol_chunk)} symbols "
                            f"{spec.schema}/{spec.frequency}"
                        ),
                        schema=spec.schema,
                        request_start=actual_start,
                        request_end=available_range.end,
                        timing_reporter=print,
                    )
                except Exception as exc:
                    if _is_symbol_batch_error(exc) and len(symbol_chunk) > 1:
                        print(
                            f"[databento/{request_key}] batch rejected; "
                            "isolating symbols with individual requests"
                        )
                        _append_isolated_native_results(
                            results,
                            provider=provider,
                            symbols=symbol_chunk,
                            spec=spec,
                            request_spec=request_spec,
                            available_range=available_range,
                        )
                    else:
                        for symbol in symbol_chunk:
                            results[symbol].append((spec, [], None, None, exc))
                    continue

                missing = set(symbol_chunk).difference(fetched)
                if missing:
                    exc = RuntimeError(
                        "Databento batch response omitted requested symbol(s): "
                        + ", ".join(sorted(missing))
                    )
                for symbol in symbol_chunk:
                    if symbol in missing:
                        results[symbol].append((spec, [], None, None, exc))
                        continue
                    bars, raw_frame = fetched[symbol]
                    results[symbol].append(
                        (spec, bars, raw_frame, selected_range, None)
                    )

        if spec_completed is not None:
            spec_completed(
                spec,
                {symbol: [results[symbol][-1]] for symbol in symbols},
            )

    return results


def _append_isolated_native_results(
    results: dict[str, list[tuple]],
    *,
    provider: DatabentoMarketDataProvider,
    symbols: tuple[str, ...],
    spec,
    request_spec,
    available_range,
) -> None:
    for symbol in symbols:
        try:
            bars, raw_frame, selected_range = call_with_persistent_databento_retry(
                lambda symbol=symbol: provider.fetch_native_bars(
                    symbol,
                    request_spec,
                    available_range=available_range,
                ),
                operation_name=f"{symbol} {spec.schema}/{spec.frequency}",
                symbol=symbol,
                schema=spec.schema,
                request_start=max(
                    available_range.start,
                    available_range.end - request_spec.lookback,
                ),
                request_end=available_range.end,
                timing_reporter=print,
            )
            results[symbol].append(
                (spec, bars, raw_frame, selected_range, None)
            )
        except Exception as exc:
            results[symbol].append((spec, [], None, None, exc))


def _symbol_chunks(symbols: list[str]) -> Iterable[tuple[str, ...]]:
    for start in range(0, len(symbols), DATABENTO_MAX_SYMBOLS_PER_REQUEST):
        yield tuple(symbols[start : start + DATABENTO_MAX_SYMBOLS_PER_REQUEST])


def _is_symbol_batch_error(exc: Exception) -> bool:
    message = str(exc).strip().lower()
    status = None
    for attribute in ("status_code", "status", "http_status", "http_status_code"):
        try:
            status = int(getattr(exc, attribute, None))
            break
        except (TypeError, ValueError):
            continue
    return status in {400, 404, 422} and any(
        marker in message for marker in ("symbol", "symbology", "instrument")
    )


def _normalize_symbols(values: Iterable[str]) -> tuple[str, ...]:
    symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip())
    )
    if not symbols:
        raise ValueError("At least one symbol is required.")
    return symbols


def _continuation_overlap(frequency: str) -> timedelta:
    normalized = frequency.strip().lower()
    overlaps = {
        "1s": timedelta(minutes=5),
        "1m": timedelta(hours=1),
        "1h": timedelta(hours=6),
        "1d": timedelta(days=7),
    }
    try:
        return overlaps[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported Databento native frequency: {frequency}") from exc


def _fetch_cme(store: ParquetStore) -> tuple[int, int, int]:
    with exclusive_runtime_lock(
        cme_writer_lock_path(store.root_dir),
        process_name="Duckets inline CME compatibility writer",
    ):
        return _fetch_cme_unlocked(store)


def _fetch_cme_unlocked(store: ParquetStore) -> tuple[int, int, int]:
    provider = DatabentoCmeContextProvider()
    data_files = 0
    error_files = 0
    advisory_files = 0

    try:
        specs = provider.specs()
    except Exception as exc:
        store.save_error(
            source="databento",
            category="macro",
            symbol="CME_CONTEXT",
            request_key="cme_context",
            error_type=type(exc).__name__,
            error_message=str(exc),
            pool="cme",
        )
        return 0, 1, 0

    for requested_spec in specs:
        spec = requested_spec
        rows: list[dict[str, object]] = []
        raw_frame = None
        exc: Exception | None = None
        try:
            rows, raw_frame, spec = call_with_persistent_databento_retry(
                lambda requested_spec=requested_spec: provider.fetch_cme_context(
                    requested_spec
                ),
                operation_name=f"CME {requested_spec.key}",
                symbol=requested_spec.symbol,
                schema=requested_spec.schema,
                request_start=requested_spec.start,
                request_end=requested_spec.end,
                timing_reporter=print,
            )
        except Exception as caught:
            exc = caught

        metadata = {
            "provider_dataset": spec.dataset,
            "provider_schema": spec.schema,
            "provider_symbol": spec.symbol,
            "provider_stype_in": spec.stype_in,
            "cme_context_group": spec.group_key,
            "cme_request_key": spec.key,
            "range_start": spec.start.isoformat(),
            "range_end": spec.end.isoformat(),
            "initial_range_start": (spec.initial_start or spec.start).isoformat(),
            "initial_range_end": (spec.initial_end or spec.end).isoformat(),
            "effective_range_start": spec.start.isoformat(),
            "effective_range_end": spec.end.isoformat(),
            "empty_window_expansion_count": spec.empty_window_expansion_count,
            "latest_event_timestamp": spec.latest_event_timestamp,
            "cme_schema_status": spec.availability_status,
            "macro_context_kind": "cme_futures_cross_asset",
            "limit": spec.limit,
        }
        if exc is not None:
            store.save_error(
                source="databento",
                category="macro",
                symbol=spec.symbol,
                request_key=spec.key,
                error_type=type(exc).__name__,
                error_message=str(exc),
                metadata=metadata,
                pool="cme",
            )
            error_files += 1
            continue

        status_only = _is_cme_status_only(rows)
        normalized_request_key = (
            f"{spec.key}_status" if status_only else spec.key
        )
        normalized_target = _cme_target_path(
            store,
            spec,
            scope="normalized",
            suffix=normalized_request_key,
            dataset_key=normalized_request_key,
        )
        try:
            normalized_path = store.save_macro_rows(
                "databento",
                spec.symbol,
                normalized_request_key,
                rows,
                metadata=metadata,
                pool="cme",
                mode="snapshot" if status_only else "upsert",
            )
        except Exception as persistence_exc:
            _record_cme_persistence_error(
                store,
                spec,
                stage="normalized",
                target=normalized_target,
                frame=rows,
                exc=persistence_exc,
                metadata=metadata,
            )
            error_files += 1
        else:
            if normalized_path is not None:
                data_files += 1

        if raw_frame is not None:
            raw_endpoint = f"{spec.key}_raw"
            raw_target = _cme_target_path(
                store,
                spec,
                scope="raw",
                suffix=raw_endpoint,
                dataset_key=spec.key,
            )
            try:
                raw_path = store.save_raw_frame(
                    source="databento",
                    category="macro",
                    symbol=spec.symbol,
                    endpoint=raw_endpoint,
                    dataset_key=spec.key,
                    frame=raw_frame,
                    metadata=metadata,
                    pool="cme",
                )
            except Exception as persistence_exc:
                _record_cme_persistence_error(
                    store,
                    spec,
                    stage="raw",
                    target=raw_target,
                    frame=raw_frame,
                    exc=persistence_exc,
                    metadata=metadata,
                )
                error_files += 1
            else:
                if raw_path is not None:
                    data_files += 1

    try:
        calculated_path = materialize_cme_cross_asset_context(store.root_dir)
    except CmeCrossAssetNotReady:
        calculated_path = None
    except CmeCrossAssetQualityError as exc:
        advisory_path = store.save_advisory(
            source="databento",
            category="macro",
            symbol="CME_CONTEXT",
            request_key="cross-asset-context",
            advisory_type=type(exc).__name__,
            advisory_message=str(exc),
            metadata={
                "calculation": "cross-asset-context",
                "input_policy": "persisted_rows_only",
                "provider_rows_preserved": True,
            },
            pool="cme",
        )
        recorded = advisory_path if advisory_path is not None else "existing advisory"
        print(f"[CME/cross-asset-context] advisory recorded: {recorded}")
        advisory_files += 1
        calculated_path = None
    except Exception as exc:
        store.save_error(
            source="databento",
            category="macro",
            symbol="CME_CONTEXT",
            request_key="cross-asset-context",
            error_type=type(exc).__name__,
            error_message=str(exc),
            metadata={
                "calculation": "cross-asset-context",
                "input_policy": "persisted_rows_only",
            },
            pool="cme",
        )
        error_files += 1
        calculated_path = None
    if calculated_path is not None:
        data_files += 1

    return data_files, error_files, advisory_files


def _cme_target_path(
    store: ParquetStore,
    spec: DatabentoCmeContextSpec,
    *,
    scope: str,
    suffix: str,
    dataset_key: str,
) -> Path:
    target_path = getattr(store, "target_path", None)
    if callable(target_path):
        return Path(
            target_path(
                scope=scope,
                source="databento",
                category="macro",
                symbol=spec.symbol,
                suffix=suffix,
                dataset_key=dataset_key,
                pool="cme",
            )
        )
    folder = pool_data_folder(
        Path(store.root_dir),
        pool="cme",
        symbol=spec.symbol,
        category="macro",
        source="databento",
        scope=scope,
        dataset_key=dataset_key,
    )
    stem = safe_token(spec.symbol.strip().upper().replace("/", "-"))
    if suffix:
        stem += f"_{safe_token(suffix)}"
    return folder / f"{stem}.parquet"


def _record_cme_persistence_error(
    store: ParquetStore,
    spec: DatabentoCmeContextSpec,
    *,
    stage: str,
    target: Path,
    frame: object,
    exc: Exception,
    metadata: dict[str, object],
) -> None:
    message = (
        f"CME {stage} persistence failed: group={spec.group_key}; "
        f"schema={spec.schema}; request_key={spec.key}; target={target}; "
        f"{type(exc).__name__}: {exc}"
    )
    error_metadata = {
        **metadata,
        "persistence_stage": stage,
        "persistence_target_file": str(target),
        "persistence_request_key": spec.key,
        "incoming_row_count": _cme_frame_row_count(frame),
        "incoming_schema": _cme_frame_schema(frame),
        "target_schema": _cme_target_schema(target),
    }
    try:
        error_path = store.save_error(
            source="databento",
            category="macro",
            symbol=spec.symbol,
            request_key=spec.key,
            error_type=type(exc).__name__,
            error_message=message,
            metadata=error_metadata,
            pool="cme",
        )
    except Exception as error_exc:
        raise RuntimeError(
            f"{message}; additionally failed to persist the CME-specific error: "
            f"{type(error_exc).__name__}: {error_exc}"
        ) from error_exc
    recorded = error_path if error_path is not None else "existing error row"
    print(f"[CME/{spec.group_key}/{spec.schema}] persistence error recorded: {recorded}")


def _cme_frame_row_count(frame: object) -> int:
    try:
        return len(frame)  # type: ignore[arg-type]
    except Exception:
        return 0


def _cme_frame_schema(frame: object) -> str:
    try:
        prepared = frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)
    except Exception as exc:
        return json.dumps(
            {
                "schema_error": f"{type(exc).__name__}: {exc}",
                "python_type": type(frame).__name__,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    return json.dumps(
        {str(column): str(dtype) for column, dtype in prepared.dtypes.items()},
        sort_keys=True,
        separators=(",", ":"),
    )


def _cme_target_schema(target: Path) -> str:
    if not target.is_file():
        return "<missing>"
    try:
        return str(pq.read_schema(target))
    except Exception as exc:
        return f"<unreadable: {type(exc).__name__}: {exc}>"


def _is_cme_status_only(rows: list[dict[str, object]]) -> bool:
    return (
        len(rows) == 1
        and rows[0].get("cme_row_kind") == "schema_status"
    )
