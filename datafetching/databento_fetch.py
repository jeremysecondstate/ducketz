from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

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
from datafetching.continuation import latest_normalized_bar_timestamp
from datafetching.cme_cross_asset_context import (
    CmeCrossAssetNotReady,
    CmeCrossAssetQualityError,
    materialize_cme_cross_asset_context,
)
from datafetching.derived_bars import DERIVED_INTRADAY_FREQUENCIES, derive_intraday_bars
from datafetching.layout import pool_data_folder, safe_token
from datafetching.parquet_store import ParquetStore


def fetch(
    symbol: str,
    store: ParquetStore,
    *,
    include_cme: bool = True,
    profile: str = "continuation",
) -> FetchResult:
    """Fetch Databento data, continuing every native bar dataset from stored time.

    Raw provider frames are retained as returned, including a potentially active
    candle. Normalized equity-bar Parquets are a finalized-candle dataset and
    therefore contain only intervals completed as of this fetch cycle.
    """
    data_files = 0
    error_files = 0
    advisory_files = 0
    provider = DatabentoMarketDataProvider()
    observed_at = datetime.now(timezone.utc)
    native_results = list(_fetch_native_results(provider, symbol, profile, store))
    source_bars_by_frequency: dict[str, list] = {}
    source_specs_by_frequency = {}

    for spec, bars, raw_frame, available_range, exc in native_results:
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
        }
        if available_range is not None:
            metadata.update(
                {
                    "range_start": available_range.start.isoformat(),
                    "range_end": available_range.end.isoformat(),
                }
            )

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

    derived_files, derived_errors = _save_derived_intraday_bars(
        symbol,
        store,
        provider=provider,
        profile=profile,
        source_bars=source_bars_by_frequency.get("1m", []),
        source_spec=source_specs_by_frequency.get("1m"),
        observed_at=observed_at,
    )
    data_files += derived_files
    error_files += derived_errors

    if include_cme:
        cme_data_files, cme_error_files, cme_advisory_files = _fetch_cme(store)
        data_files += cme_data_files
        error_files += cme_error_files
        advisory_files += cme_advisory_files

    return FetchResult("databento", data_files, error_files, advisory_files)


def _save_derived_intraday_bars(
    symbol: str,
    store: ParquetStore,
    *,
    provider: DatabentoMarketDataProvider,
    profile: str,
    source_bars: list,
    source_spec,
    observed_at: datetime,
) -> tuple[int, int]:
    if not source_bars or source_spec is None:
        return 0, 0

    data_files = 0
    error_files = 0
    for output_frequency in DERIVED_INTRADAY_FREQUENCIES:
        request_key = f"derived_1m_{output_frequency}"
        metadata = {
            "provider_dataset": provider.dataset,
            "source_schema": source_spec.schema,
            "source_frequency": "1m",
            "output_frequency": output_frequency,
            "aggregation_method": "session_resampled_from_complete_1m",
            "fetch_profile": profile,
            "price_basis": "unadjusted_market_scale",
            "volume_basis": "summed_from_complete_1m",
            "corporate_action_adjustment": "none",
            "normalized_bar_policy": "completed_intervals_only",
        }
        try:
            bars = derive_intraday_bars(
                symbol,
                source_bars,
                output_frequency,
                as_of=observed_at,
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


def _fetch_native_results(
    provider: DatabentoMarketDataProvider,
    symbol: str,
    profile: str,
    store: ParquetStore,
):
    normalized_profile = profile.strip().lower()
    if normalized_profile not in {"continuation", "full", "incremental"}:
        raise ValueError(
            "Databento fetch mode must be continuation; legacy full/incremental "
            "aliases are also accepted."
        )
    # Every native schema is refreshed on every iteration from that request's own
    # latest persisted timestamp.
    specs = provider.native_specs()

    try:
        range_payload = call_with_persistent_databento_retry(
            provider.dataset_range,
            operation_name="equities dataset-range metadata",
        )
    except Exception as exc:
        return [(spec, [], None, None, exc) for spec in specs]

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
            )
            results.append((spec, bars, raw_frame, selected_range, None))
        except Exception as exc:
            results.append((spec, [], None, None, exc))
    return results


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
