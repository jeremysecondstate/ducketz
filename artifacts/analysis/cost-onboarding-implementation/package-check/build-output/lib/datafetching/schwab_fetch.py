from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import inspect
from typing import Any, Iterable

import requests

from app.services.market_fetch_specs import SchwabPriceHistorySpec, schwab_price_history_specs
from app.services.schwab import MARKETDATA_BASE_URL, SchwabSession
from app.services.schwab_market_data import SchwabMarketDataProvider
from datafetching import FetchResult
from datafetching.continuation import (
    latest_normalized_bar_timestamp,
)
from datafetching.decision_time import latest_completed_bar_clock
from datafetching.layout import canonical_timeframe
from datafetching.parquet_store import ParquetStore
from datafetching.quote_liquidity import (
    QuoteLiquidityQualityError,
    persist_quote_liquidity,
)
from datafetching.runtime_lock import exclusive_runtime_lock
from options.publication import option_writer_lock_path
from options.snapshot import persist_schwab_option_snapshot

OPTION_CHAIN_STRIKE_COUNT = 100
OPTION_CHAIN_HORIZON_DAYS = 200


class DataFetchingSchwabSession(SchwabSession):
    """Schwab session methods owned by the provider-only data-fetching package."""

    def get_price_history(
        self,
        symbol: str,
        *,
        period_type: str = "year",
        period: int | None = 1,
        frequency_type: str = "daily",
        frequency: int = 1,
        need_extended_hours_data: bool = False,
        start_datetime: datetime | None = None,
        end_datetime: datetime | None = None,
    ) -> Any:
        cleaned_symbol = symbol.strip().upper()
        if not cleaned_symbol:
            raise ValueError("Symbol is required for price history.")
        if start_datetime is not None and end_datetime is not None:
            if _utc_datetime(start_datetime) > _utc_datetime(end_datetime):
                raise ValueError("Price-history start_datetime must not exceed end_datetime")

        params: dict[str, object] = {
            "symbol": cleaned_symbol,
            "periodType": period_type,
            "frequencyType": frequency_type,
            "frequency": frequency,
            "needExtendedHoursData": str(need_extended_hours_data).lower(),
        }
        if start_datetime is None and end_datetime is None:
            if period is not None:
                params["period"] = period
        else:
            if start_datetime is not None:
                params["startDate"] = _epoch_milliseconds(start_datetime)
            if end_datetime is not None:
                params["endDate"] = _epoch_milliseconds(end_datetime)

        response = requests.get(
            f"{MARKETDATA_BASE_URL}/pricehistory",
            headers=self._headers(),
            params=params,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()

    def get_option_chain_snapshot(
        self,
        symbol: str,
        *,
        as_of: datetime | None = None,
        strike_count: int = OPTION_CHAIN_STRIKE_COUNT,
        horizon_days: int = OPTION_CHAIN_HORIZON_DAYS,
    ) -> Any:
        """Fetch a greeks-enabled option surface around the current underlying price."""
        cleaned_symbol = symbol.strip().upper()
        if not cleaned_symbol:
            raise ValueError("Symbol is required for an option-chain snapshot.")
        if strike_count < 2:
            raise ValueError("strike_count must be at least 2")
        if horizon_days < 1:
            raise ValueError("horizon_days must be at least 1")

        observed_at = as_of or datetime.now(timezone.utc)
        start = observed_at.date()
        response = requests.get(
            f"{MARKETDATA_BASE_URL}/chains",
            headers=self._headers(),
            params={
                "symbol": cleaned_symbol,
                "contractType": "ALL",
                "strikeCount": strike_count,
                "includeUnderlyingQuote": "true",
                "strategy": "SINGLE",
                "range": "ALL",
                "fromDate": start.isoformat(),
                "toDate": (start + timedelta(days=horizon_days)).isoformat(),
            },
            timeout=20,
        )
        response.raise_for_status()
        return response.json()


def fetch(
    symbol: str,
    store: ParquetStore,
    *,
    profile: str = "continuation",
    session: DataFetchingSchwabSession | None = None,
    provider: SchwabMarketDataProvider | None = None,
    prefetched_quote: tuple[Any, Any] | None = None,
    include_options: bool = True,
    include_price_history: bool = True,
) -> FetchResult:
    """Fetch Schwab evidence, optionally continuing its price-history series."""
    specs = _specs_for_profile(profile) if include_price_history else ()
    request_observed_at = datetime.now(timezone.utc)
    session = session or DataFetchingSchwabSession()
    provider = provider or SchwabMarketDataProvider(session=session)
    data_files = 0
    error_files = 0
    advisory_files = 0
    failure_counts: Counter[str] = Counter()
    advisory_counts: Counter[str] = Counter()
    continuation_windows = 0

    try:
        quote, raw_quote = (
            prefetched_quote
            if prefetched_quote is not None
            else provider.fetch_quote(symbol)
        )
        if store.save_quote(quote) is not None:
            data_files += 1
        if store.save_raw_payload(
            source="schwab",
            category="quotes",
            symbol=symbol,
            endpoint="quotes",
            payload=raw_quote,
        ) is not None:
            data_files += 1
        try:
            persist_quote_liquidity(store.root_dir, quote)
            data_files += 1
        except QuoteLiquidityQualityError as exc:
            detail = f"{type(exc).__name__}: {exc}"
            store.save_advisory(
                source="schwab",
                category="quotes",
                symbol=symbol,
                request_key="quote_liquidity",
                advisory_type=type(exc).__name__,
                advisory_message=str(exc),
                metadata={
                    "calculation": "quote-liquidity",
                    "provider_payload_reused": True,
                    "provider_rows_preserved": True,
                    "quote_event_at": (
                        quote.quote_event_at.isoformat()
                        if quote.quote_event_at is not None
                        else None
                    ),
                    "fetched_at": quote.fetched_at.isoformat(),
                },
            )
            advisory_counts[detail] += 1
            advisory_files += 1
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        store.save_error(
            source="schwab",
            category="quotes",
            symbol=symbol,
            request_key="quotes",
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        failure_counts[detail] += 1
        error_files += 1

    for spec in specs:
        metadata = _metadata(spec, profile)
        canonical = canonical_timeframe("schwab", spec.key, metadata)
        request_metadata = dict(metadata)
        try:
            latest_stored = _latest_stored_bar_timestamp(store, symbol, spec)
            start_datetime = (
                latest_stored - _continuation_overlap(spec)
                if latest_stored is not None
                else None
            )
            end_datetime = request_observed_at if latest_stored is not None else None
            request_metadata = _request_metadata(
                metadata,
                latest_stored=latest_stored,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
            )
            if latest_stored is None:
                print(
                    f"[{symbol}/schwab/{spec.key}] no stored dataset; "
                    "requesting configured history"
                )
            else:
                continuation_windows += 1
            bars, raw_payload = provider.fetch_bars_for_spec(
                symbol,
                spec,
                start_datetime=start_datetime,
                end_datetime=end_datetime,
            )
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            store.save_error(
                source="schwab",
                category="bars",
                symbol=symbol,
                request_key=spec.key,
                error_type=type(exc).__name__,
                error_message=str(exc),
                metadata={**request_metadata, "canonical_timeframe": canonical},
            )
            failure_counts[detail] += 1
            error_files += 1
            continue

        if store.save_bars(
            "schwab",
            symbol,
            spec.key,
            bars,
            request_key=spec.key,
            metadata=metadata,
            as_of=request_observed_at,
        ) is not None:
            data_files += 1

        if store.save_raw_payload(
            source="schwab",
            category="bars",
            symbol=symbol,
            endpoint=f"pricehistory_{spec.key}",
            payload=raw_payload,
            timeframe=canonical,
            metadata=request_metadata,
        ) is not None:
            data_files += 1

    if continuation_windows:
        print(
            f"[{symbol}/schwab] continuation refreshed "
            f"{continuation_windows} stored request windows; overlap handled "
            "automatically"
        )

    if not include_options:
        _report_request_counts(failure_counts, advisory_counts)
        return FetchResult("schwab", data_files, error_files, advisory_files)

    try:
        with exclusive_runtime_lock(
            option_writer_lock_path(store.root_dir),
            process_name="Duckets inline Options compatibility writer",
        ):
            option_result = _fetch_inline_option_snapshot(
                symbol,
                store,
                session=session,
            )
    except RuntimeError as exc:
        # A conflicting external owner must fail before the inline path performs
        # a slow request or writes any option artifact.
        print(f"[{symbol}/schwab/options] writer conflict: {exc}")
        error_files += 1
    else:
        data_files += option_result.data_files
        error_files += option_result.error_files
        advisory_files += option_result.advisory_files

    _report_request_counts(failure_counts, advisory_counts)

    return FetchResult("schwab", data_files, error_files, advisory_files)


def _fetch_inline_option_snapshot(
    symbol: str,
    store: ParquetStore,
    *,
    session: DataFetchingSchwabSession,
) -> FetchResult:
    data_files = 0
    error_files = 0
    advisory_files = 0
    failure_counts: Counter[str] = Counter()
    advisory_counts: Counter[str] = Counter()
    request_started_at = datetime.now(timezone.utc)
    try:
        option_payload = session.get_option_chain_snapshot(
            symbol,
            as_of=request_started_at,
        )
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        store.save_error(
            source="schwab",
            category="options",
            symbol=symbol,
            request_key="option_chain_snapshot",
            error_type=type(exc).__name__,
            error_message=str(exc),
            metadata={
                "strike_count": OPTION_CHAIN_STRIKE_COUNT,
                "horizon_days": OPTION_CHAIN_HORIZON_DAYS,
                "timestamp_policy": "latest_completed_databento_1m_quarter_hour_bar_end",
            },
        )
        failure_counts[detail] += 1
        error_files += 1
    else:
        fetched_at = datetime.now(timezone.utc)
        option_metadata = {
            "strike_count": OPTION_CHAIN_STRIKE_COUNT,
            "horizon_days": OPTION_CHAIN_HORIZON_DAYS,
            "timestamp_policy": "latest_completed_databento_1m_quarter_hour_bar_end",
            "request_started_at": request_started_at.isoformat(),
            "provider_fetched_at": fetched_at.isoformat(),
        }
        try:
            if store.save_raw_payload(
                source="schwab",
                category="options",
                symbol=symbol,
                endpoint="option_chain_snapshot",
                payload=option_payload,
                metadata=option_metadata,
            ) is not None:
                data_files += 1
            clock = latest_completed_bar_clock(
                store.root_dir,
                symbol=symbol,
                as_of=request_started_at,
            )
            persist_kwargs = {
                "symbol": symbol,
                "payload": option_payload,
                "clock": clock,
                "fetched_at": fetched_at,
                "quote_cutoff_at": request_started_at,
            }
            if "acquire_writer_lock" in inspect.signature(
                persist_schwab_option_snapshot
            ).parameters:
                persist_kwargs["acquire_writer_lock"] = False
            output = persist_schwab_option_snapshot(
                store.root_dir,
                **persist_kwargs,
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            store.save_advisory(
                source="schwab",
                category="options",
                symbol=symbol,
                request_key="option_chain_snapshot",
                advisory_type=type(exc).__name__,
                advisory_message=str(exc),
                metadata={
                    **option_metadata,
                    "provider_payload_reused": True,
                    "provider_rows_preserved": True,
                },
            )
            advisory_counts[detail] += 1
            advisory_files += 1
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            store.save_error(
                source="schwab",
                category="options",
                symbol=symbol,
                request_key="option_chain_snapshot_persistence",
                error_type=type(exc).__name__,
                error_message=str(exc),
                metadata=option_metadata,
            )
            failure_counts[detail] += 1
            error_files += 1
        else:
            data_files += 3
            print(
                f"[{symbol}/schwab/options] {output.contract_rows} contracts at "
                f"decision time {clock.decision_timestamp.isoformat()} -> "
                f"{output.features_path}"
            )

    _report_request_counts(failure_counts, advisory_counts)
    return FetchResult("schwab-options", data_files, error_files, advisory_files)


def _report_request_counts(
    failure_counts: Counter[str],
    advisory_counts: Counter[str],
) -> None:
    if failure_counts:
        print("[schwab] grouped request failures:")
        for detail, count in failure_counts.most_common():
            print(f"[schwab]   {count} x {detail}")
    if advisory_counts:
        print("[schwab] non-blocking local advisories:")
        for detail, count in advisory_counts.most_common():
            print(f"[schwab]   {count} x {detail}")

def fetch_many(
    symbols: Iterable[str],
    store: ParquetStore,
    *,
    profile: str = "continuation",
    include_options: bool = True,
    include_price_history: bool = True,
) -> dict[str, FetchResult]:
    """Fetch a watchlist while sharing Schwab's multi-symbol quote request."""
    clean_symbols = _normalize_symbols(symbols)
    if len(clean_symbols) == 1:
        symbol = clean_symbols[0]
        return {
            symbol: fetch(
                symbol,
                store,
                profile=profile,
                include_options=include_options,
                include_price_history=include_price_history,
            )
        }

    session = DataFetchingSchwabSession()
    provider = SchwabMarketDataProvider(session=session)
    try:
        prefetched_quotes = provider.fetch_quotes(clean_symbols)
    except Exception as exc:
        print(
            f"[schwab/quotes] batch request unavailable "
            f"({type(exc).__name__}: {exc}); using per-symbol quote requests"
        )
        prefetched_quotes = {}

    return {
        symbol: fetch(
            symbol,
            store,
            profile=profile,
            session=session,
            provider=provider,
            prefetched_quote=prefetched_quotes.get(symbol),
            include_options=include_options,
            include_price_history=include_price_history,
        )
        for symbol in clean_symbols
    }


def _normalize_symbols(values: Iterable[str]) -> tuple[str, ...]:
    symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip())
    )
    if not symbols:
        raise ValueError("At least one symbol is required.")
    return symbols


def _specs_for_profile(profile: str) -> tuple[SchwabPriceHistorySpec, ...]:
    normalized = profile.strip().lower()
    if normalized not in {"continuation", "full", "incremental"}:
        raise ValueError(
            "Schwab fetch mode must be continuation; legacy full/incremental aliases "
            "are also accepted."
        )
    specs = schwab_price_history_specs()
    maximal_by_series: dict[
        tuple[str, int, bool],
        SchwabPriceHistorySpec,
    ] = {}
    for spec in specs:
        series = (
            spec.frequency_type.strip().lower(),
            spec.frequency,
            spec.need_extended_hours_data,
        )
        selected = maximal_by_series.get(series)
        if selected is None or _history_coverage_days(spec) > _history_coverage_days(
            selected
        ):
            maximal_by_series[series] = spec
    selected_specs = set(maximal_by_series.values())
    return tuple(spec for spec in specs if spec in selected_specs)


def _history_coverage_days(spec: SchwabPriceHistorySpec) -> int:
    multipliers = {
        "day": 1,
        "month": 31,
        "year": 366,
        "ytd": 366,
    }
    try:
        multiplier = multipliers[spec.period_type.strip().lower()]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported Schwab period type: {spec.period_type}"
        ) from exc
    return multiplier * spec.period


def _latest_stored_bar_timestamp(
    store: ParquetStore,
    symbol: str,
    spec: SchwabPriceHistorySpec,
) -> datetime | None:
    metadata = _metadata(spec, "continuation")
    canonical = canonical_timeframe("schwab", spec.key, metadata)
    return latest_normalized_bar_timestamp(
        store.root_dir,
        source="schwab",
        symbol=symbol,
        timeframe=canonical,
        request_key=spec.key,
    )


def _continuation_overlap(spec: SchwabPriceHistorySpec) -> timedelta:
    frequency_type = spec.frequency_type.strip().lower()
    if frequency_type == "minute":
        return timedelta(minutes=max(60, spec.frequency * 3))
    if frequency_type == "daily":
        return timedelta(days=max(7, spec.frequency * 3))
    if frequency_type == "weekly":
        return timedelta(days=max(21, spec.frequency * 14))
    if frequency_type == "monthly":
        return timedelta(days=max(62, spec.frequency * 62))
    raise ValueError(f"Unsupported Schwab frequency type: {spec.frequency_type}")


def _request_metadata(
    metadata: dict[str, object],
    *,
    latest_stored: datetime | None,
    start_datetime: datetime | None,
    end_datetime: datetime | None,
) -> dict[str, object]:
    return {
        **metadata,
        "continuation_mode": "missing-tail" if latest_stored is not None else "initial-history",
        "latest_stored_timestamp": (
            latest_stored.isoformat() if latest_stored is not None else None
        ),
        "request_start_datetime": (
            start_datetime.isoformat() if start_datetime is not None else None
        ),
        "request_end_datetime": (
            end_datetime.isoformat() if end_datetime is not None else None
        ),
    }


def _metadata(spec: SchwabPriceHistorySpec, profile: str) -> dict[str, object]:
    return {
        "provider_period_type": spec.period_type,
        "provider_period": spec.period,
        "provider_frequency_type": spec.frequency_type,
        "provider_frequency": spec.frequency,
        "need_extended_hours_data": spec.need_extended_hours_data,
        "fetch_profile": profile,
    }


def _epoch_milliseconds(value: datetime) -> int:
    return int(_utc_datetime(value).timestamp() * 1000)


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Schwab price-history datetimes must be timezone-aware")
    return value.astimezone(timezone.utc)
