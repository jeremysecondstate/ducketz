from __future__ import annotations

from typing import Any, Iterable, Mapping

from app.services.fmp_corporate_data import (
    FmpCorporateDataProvider,
    FmpCorporateDataSpec,
    _corporate_rows_from_payload,
)
from app.services.fmp_macro_context import FmpMacroContextProvider
from datafetching import FetchResult
from datafetching.fmp_energy_context import (
    FmpEnergyContextNotReady,
    FmpEnergyContextQualityError,
    materialize_fmp_energy_context,
    normalize_fmp_quote_timestamps,
)
from datafetching.parquet_store import ParquetStore


def fetch(
    symbol: str,
    store: ParquetStore,
    *,
    include_macro: bool = True,
    corporate_provider: FmpCorporateDataProvider | None = None,
    prefetched_corporate: Mapping[
        str,
        tuple[list[dict[str, Any]], Any, str],
    ]
    | None = None,
) -> FetchResult:
    """Fetch FMP corporate data and optional shared commodity proxies."""
    data_files = 0
    error_files = 0
    advisory_files = 0

    corporate = corporate_provider or FmpCorporateDataProvider()
    corporate_specs = (
        *corporate.corporate_specs(symbol),
        FmpCorporateDataSpec("stock_splits", "splits", {"symbol": symbol}),
    )
    for spec in corporate_specs:
        if spec.key == "sec_filings_search_symbol":
            continue
        is_statement = "_statement_" in f"_{spec.key}_"

        endpoint_used = spec.endpoint
        try:
            prefetched = (prefetched_corporate or {}).get(spec.key)
            if prefetched is not None:
                rows, raw_payload, endpoint_used = prefetched
            elif spec.key == "stock_splits":
                raw_payload = corporate._get_json(spec.endpoint, spec.params)
                payload_for_rows = raw_payload
                if isinstance(raw_payload, dict) and isinstance(raw_payload.get("historical"), list):
                    payload_for_rows = raw_payload["historical"]
                rows = _corporate_rows_from_payload(
                    symbol=symbol,
                    request_key=spec.key,
                    endpoint=spec.endpoint,
                    payload=payload_for_rows,
                )
            else:
                rows, raw_payload, endpoint_used = corporate.fetch_corporate_data(symbol, spec)
        except Exception as exc:
            store.save_error(
                source="fmp",
                category="corporate",
                symbol=symbol,
                request_key=spec.key,
                error_type=type(exc).__name__,
                error_message=str(exc),
                metadata={
                    "provider_base_url": corporate.base_url,
                    "endpoint": endpoint_used,
                    "provider_endpoint": spec.endpoint,
                },
            )
            error_files += 1
            continue

        metadata = {
            "provider_base_url": corporate.base_url,
            "endpoint": endpoint_used,
            "provider_endpoint": spec.endpoint,
        }
        if spec.key == "stock_splits":
            metadata.update(
                {
                    "corporate_action_kind": "stock_split",
                    "ratio_semantics": "numerator_new_shares_per_denominator_old_shares",
                }
            )
        if store.save_corporate_rows(
            "fmp",
            symbol,
            spec.key,
            rows,
            metadata=metadata,
            keys=(
                (
                    "date",
                    "period",
                    "available_at",
                )
                if is_statement
                else None
            ),
            mode="append_if_revised" if is_statement else "upsert",
        ) is not None:
            data_files += 1

        if store.save_raw_payload(
            source="fmp",
            category="corporate",
            symbol=symbol,
            endpoint=spec.key,
            payload=raw_payload,
            metadata=metadata,
        ) is not None:
            data_files += 1

    if not include_macro:
        return FetchResult("fmp", data_files, error_files, advisory_files)

    macro = FmpMacroContextProvider()
    commodity_specs = macro.commodity_proxy_specs()
    for spec, rows, raw_payload, exc in macro.fetch_commodity_proxy_quotes(commodity_specs):
        metadata = {
            "provider_base_url": macro.base_url,
            "endpoint": spec.endpoint,
            "macro_context_kind": spec.kind,
            "macro_authority": "commodity_proxy_only",
        }
        if exc is not None:
            store.save_error(
                source="fmp",
                category="macro",
                symbol=spec.output_symbol,
                request_key=spec.key,
                error_type=type(exc).__name__,
                error_message=str(exc),
                metadata=metadata,
                pool="macro",
            )
            error_files += 1
            continue

        rows = normalize_fmp_quote_timestamps(rows)
        if store.save_macro_rows(
            "fmp",
            spec.output_symbol,
            spec.key,
            rows,
            metadata=metadata,
            pool="macro",
            mode="append_if_changed",
        ) is not None:
            data_files += 1

        if store.save_raw_payload(
            source="fmp",
            category="macro",
            symbol=spec.output_symbol,
            endpoint=spec.key,
            dataset_key=spec.key,
            payload=raw_payload,
            metadata=metadata,
            pool="macro",
        ) is not None:
            data_files += 1

    try:
        calculated_path = materialize_fmp_energy_context(store.root_dir)
    except FmpEnergyContextNotReady:
        calculated_path = None
    except FmpEnergyContextQualityError as exc:
        store.save_advisory(
            source="fmp",
            category="macro",
            symbol="ENERGY_CONTEXT",
            request_key="energy-context",
            advisory_type=type(exc).__name__,
            advisory_message=str(exc),
            metadata={
                "calculation": "energy-context",
                "input_policy": "persisted_rows_only",
                "provider_rows_preserved": True,
            },
            pool="macro",
        )
        advisory_files += 1
        calculated_path = None
    except Exception as exc:
        store.save_error(
            source="fmp",
            category="macro",
            symbol="ENERGY_CONTEXT",
            request_key="energy-context",
            error_type=type(exc).__name__,
            error_message=str(exc),
            metadata={
                "calculation": "energy-context",
                "input_policy": "persisted_rows_only",
            },
            pool="macro",
        )
        error_files += 1
        calculated_path = None
    if calculated_path is not None:
        data_files += 1

    return FetchResult("fmp", data_files, error_files, advisory_files)


def fetch_many(
    symbols: Iterable[str],
    store: ParquetStore,
    *,
    include_macro: bool = True,
) -> dict[str, FetchResult]:
    """Fetch an FMP watchlist, sharing endpoints that accept many symbols."""
    clean_symbols = _normalize_symbols(symbols)
    if len(clean_symbols) == 1:
        symbol = clean_symbols[0]
        return {symbol: fetch(symbol, store, include_macro=include_macro)}

    corporate = FmpCorporateDataProvider()
    prefetched = _fetch_batched_corporate_data(corporate, clean_symbols)
    return {
        symbol: fetch(
            symbol,
            store,
            include_macro=include_macro and index == 0,
            corporate_provider=corporate,
            prefetched_corporate=prefetched[symbol],
        )
        for index, symbol in enumerate(clean_symbols)
    }


def _fetch_batched_corporate_data(
    provider: FmpCorporateDataProvider,
    symbols: tuple[str, ...],
) -> dict[str, dict[str, tuple[list[dict[str, Any]], Any, str]]]:
    prefetched: dict[
        str,
        dict[str, tuple[list[dict[str, Any]], Any, str]],
    ] = {symbol: {} for symbol in symbols}
    for request_key, endpoint in (
        ("quote", "batch-quote"),
        ("market_capitalization", "market-capitalization-batch"),
    ):
        try:
            payload = provider._get_json(
                endpoint,
                {"symbols": ",".join(symbols)},
            )
        except Exception as exc:
            print(
                f"[fmp/{endpoint}] batch request unavailable "
                f"({type(exc).__name__}: {exc}); using per-symbol requests"
            )
            continue

        for row in _batch_rows(payload):
            symbol = str(row.get("symbol") or row.get("ticker") or "").strip().upper()
            if symbol not in prefetched or request_key in prefetched[symbol]:
                continue
            symbol_payload = [dict(row)]
            rows = _corporate_rows_from_payload(
                symbol=symbol,
                request_key=request_key,
                endpoint=endpoint,
                payload=symbol_payload,
            )
            if rows:
                prefetched[symbol][request_key] = (
                    rows,
                    symbol_payload,
                    endpoint,
                )
    return prefetched


def _batch_rows(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, Mapping)]
        return [payload]
    return []


def _normalize_symbols(values: Iterable[str]) -> tuple[str, ...]:
    symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip())
    )
    if not symbols:
        raise ValueError("At least one symbol is required.")
    return symbols
