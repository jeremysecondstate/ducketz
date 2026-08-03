from __future__ import annotations

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
) -> FetchResult:
    """Fetch FMP corporate data and optional shared commodity proxies."""
    data_files = 0
    error_files = 0
    advisory_files = 0

    corporate = FmpCorporateDataProvider()
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
            if spec.key == "stock_splits":
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
