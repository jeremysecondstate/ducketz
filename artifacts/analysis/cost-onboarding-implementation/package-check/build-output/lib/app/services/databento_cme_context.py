from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pandas as pd

from app.models.market_data import MarketBar
from app.services.databento_market_data import _bars_from_databento_frame

DATABENTO_API_KEY_ENV = "DATABENTO_API_KEY"
DATABENTO_CME_DATASET_ENV = "DATABENTO_CME_DATASET"
DATABENTO_CME_SCHEMA_ENV = "DATABENTO_CME_SCHEMA"
DATABENTO_CME_SCHEMAS_ENV = "DATABENTO_CME_SCHEMAS"
DATABENTO_CME_CONTEXT_SYMBOLS_ENV = "DATABENTO_CME_CONTEXT_SYMBOLS"
DATABENTO_CME_CONTEXT_STYPE_IN_ENV = "DATABENTO_CME_CONTEXT_STYPE_IN"
DATABENTO_CME_CONTRACT_SYMBOLS_ENV = "DATABENTO_CME_CONTRACT_SYMBOLS"
DATABENTO_CME_CONTRACT_STYPE_IN_ENV = "DATABENTO_CME_CONTRACT_STYPE_IN"
DATABENTO_CME_LOOKBACK_DAYS_ENV = "DATABENTO_CME_LOOKBACK_DAYS"
DATABENTO_CME_LIMIT_ENV = "DATABENTO_CME_LIMIT"
DATABENTO_CME_ALLOW_UNLIMITED_ENV = "DATABENTO_CME_ALLOW_UNLIMITED"
DATABENTO_CME_CHUNK_DAYS_ENV = "DATABENTO_CME_CHUNK_DAYS"
DATABENTO_CME_MAX_REQUESTS_ENV = "DATABENTO_CME_MAX_REQUESTS"
DATABENTO_CME_MAX_NON_OHLCV_LOOKBACK_DAYS_ENV = "DATABENTO_CME_MAX_NON_OHLCV_LOOKBACK_DAYS"
DATABENTO_CME_OHLCV_RECENT_HOURS_ENV = "DATABENTO_CME_OHLCV_RECENT_HOURS"
DATABENTO_CME_BBO_RECENT_MINUTES_ENV = "DATABENTO_CME_BBO_RECENT_MINUTES"
DATABENTO_CME_MBP_RECENT_SECONDS_ENV = "DATABENTO_CME_MBP_RECENT_SECONDS"

DEFAULT_CME_CONTEXT_STYPE_IN = "continuous"
DEFAULT_CME_CONTRACT_STYPE_IN = "raw_symbol"
DEFAULT_CME_LOOKBACK_DAYS = 30
DEFAULT_CME_CHUNK_DAYS = 1
DEFAULT_CME_SCHEMA = "ohlcv-1m"
DEFAULT_CME_LIMIT = 5_000
DEFAULT_CME_ALLOW_UNLIMITED = False
DEFAULT_CME_MAX_REQUESTS = 100
DEFAULT_CME_MAX_NON_OHLCV_LOOKBACK_DAYS = 3
DEFAULT_CME_OHLCV_RECENT_HOURS = 12
DEFAULT_CME_BBO_RECENT_MINUTES = 60
DEFAULT_CME_MBP_RECENT_SECONDS = 30
MAX_CME_LATEST_WINDOW_REQUESTS = 6
UNLIMITED_LIMIT_TOKENS = {"0", "none", "unlimited", "max"}
MIN_LATEST_WINDOWS = {"ohlcv": timedelta(minutes=5), "bbo": timedelta(minutes=5), "book": timedelta(seconds=5), "other": timedelta(minutes=5)}


@dataclass(frozen=True)
class DatabentoCmeContextSpec:
    group_key: str
    output_symbol: str
    symbols: tuple[str, ...]
    dataset: str
    schema: str
    stype_in: str
    start: datetime
    end: datetime
    limit: int | None
    limit_saturated: bool = False
    latest_window_shrink_count: int = 0
    initial_start: datetime | None = None
    initial_end: datetime | None = None
    empty_window_expansion_count: int = 0
    latest_event_timestamp: str = ""
    availability_status: str = "CURRENT"

    @property
    def key(self) -> str:
        return f"cme_{self.group_key}_{self.schema}"

    @property
    def symbol(self) -> str:
        return self.output_symbol


class DatabentoCmeContextProvider:
    source = "databento"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        dataset: str | None = None,
        schema: str | None = None,
        schemas: str | tuple[str, ...] | None = None,
        context_symbols: str | tuple[str, ...] | None = None,
        context_stype_in: str | None = None,
        contract_symbols: str | tuple[str, ...] | None = None,
        contract_stype_in: str | None = None,
        symbols: str | tuple[str, ...] | None = None,
        stype_in: str | None = None,
        lookback_days: int | None = None,
        limit: int | None = None,
        chunk_days: int | None = None,
        allow_unlimited: bool | None = None,
        max_requests: int | None = None,
        max_non_ohlcv_lookback_days: int | None = None,
        ohlcv_recent_hours: int | None = None,
        bbo_recent_minutes: int | None = None,
        mbp_recent_seconds: int | None = None,
    ) -> None:
        legacy_context_symbols = symbols if context_symbols is None else context_symbols
        legacy_context_stype_in = stype_in if context_stype_in is None else context_stype_in
        configured_schema = schema if schema is not None else os.getenv(DATABENTO_CME_SCHEMA_ENV, "").strip()
        configured_schemas = schemas if schemas is not None else os.getenv(DATABENTO_CME_SCHEMAS_ENV, "").strip()
        self.api_key = api_key if api_key is not None else os.getenv(DATABENTO_API_KEY_ENV, "").strip()
        self.dataset = dataset if dataset is not None else os.getenv(DATABENTO_CME_DATASET_ENV, "").strip()
        self.schemas = _configured_schemas(configured_schemas, configured_schema)
        self.schema = self.schemas[0] if self.schemas else ""
        self.context_symbols = _configured_symbols(legacy_context_symbols if legacy_context_symbols is not None else os.getenv(DATABENTO_CME_CONTEXT_SYMBOLS_ENV, ""))
        self.context_stype_in = (legacy_context_stype_in if legacy_context_stype_in is not None else os.getenv(DATABENTO_CME_CONTEXT_STYPE_IN_ENV, "").strip()) or DEFAULT_CME_CONTEXT_STYPE_IN
        self.contract_symbols = _configured_symbols(contract_symbols if contract_symbols is not None else os.getenv(DATABENTO_CME_CONTRACT_SYMBOLS_ENV, ""))
        self.contract_stype_in = (contract_stype_in if contract_stype_in is not None else os.getenv(DATABENTO_CME_CONTRACT_STYPE_IN_ENV, "").strip()) or DEFAULT_CME_CONTRACT_STYPE_IN
        self.lookback_days = lookback_days if lookback_days is not None else _configured_int(DATABENTO_CME_LOOKBACK_DAYS_ENV, DEFAULT_CME_LOOKBACK_DAYS)
        self.allow_unlimited = allow_unlimited if allow_unlimited is not None else _configured_bool(DATABENTO_CME_ALLOW_UNLIMITED_ENV, DEFAULT_CME_ALLOW_UNLIMITED)
        self.limit = _configured_limit(limit if limit is not None else os.getenv(DATABENTO_CME_LIMIT_ENV, "").strip(), default=DEFAULT_CME_LIMIT, allow_unlimited=self.allow_unlimited)
        self.chunk_days = chunk_days if chunk_days is not None else _configured_optional_int(DATABENTO_CME_CHUNK_DAYS_ENV, DEFAULT_CME_CHUNK_DAYS)
        self.max_requests = max_requests if max_requests is not None else _configured_int(DATABENTO_CME_MAX_REQUESTS_ENV, DEFAULT_CME_MAX_REQUESTS)
        self.max_non_ohlcv_lookback_days = max_non_ohlcv_lookback_days if max_non_ohlcv_lookback_days is not None else _configured_int(DATABENTO_CME_MAX_NON_OHLCV_LOOKBACK_DAYS_ENV, DEFAULT_CME_MAX_NON_OHLCV_LOOKBACK_DAYS)
        self.ohlcv_recent_hours = ohlcv_recent_hours if ohlcv_recent_hours is not None else _configured_int(DATABENTO_CME_OHLCV_RECENT_HOURS_ENV, DEFAULT_CME_OHLCV_RECENT_HOURS)
        self.bbo_recent_minutes = bbo_recent_minutes if bbo_recent_minutes is not None else _configured_int(DATABENTO_CME_BBO_RECENT_MINUTES_ENV, DEFAULT_CME_BBO_RECENT_MINUTES)
        self.mbp_recent_seconds = mbp_recent_seconds if mbp_recent_seconds is not None else _configured_int(DATABENTO_CME_MBP_RECENT_SECONDS_ENV, DEFAULT_CME_MBP_RECENT_SECONDS)

    @property
    def symbols(self) -> tuple[str, ...]:
        return (*self.context_symbols, *self.contract_symbols)

    @property
    def stype_in(self) -> str:
        if self.context_symbols and self.contract_symbols:
            return "mixed"
        if self.context_symbols:
            return self.context_stype_in
        if self.contract_symbols:
            return self.contract_stype_in
        return ""

    def specs(self) -> tuple[DatabentoCmeContextSpec, ...]:
        self._validate_config()
        specs: list[DatabentoCmeContextSpec] = []
        for schema in self.schemas:
            end = self._query_end(schema)
            start = self._query_start(schema, end)
            if self.context_symbols:
                specs.append(DatabentoCmeContextSpec("context", "CME_CONTEXT", self.context_symbols, self.dataset, schema, self.context_stype_in, start, end, self.limit))
            if self.contract_symbols:
                specs.append(DatabentoCmeContextSpec("contracts", "CME_CONTRACTS", self.contract_symbols, self.dataset, schema, self.contract_stype_in, start, end, self.limit))
        return tuple(specs)

    def fetch_all_cme_context(self) -> list[tuple[DatabentoCmeContextSpec, list[dict[str, Any]], pd.DataFrame | None, Exception | None]]:
        results: list[tuple[DatabentoCmeContextSpec, list[dict[str, Any]], pd.DataFrame | None, Exception | None]] = []
        for spec in self.specs():
            try:
                rows, raw_frame, effective_spec = self.fetch_cme_context(spec)
                results.append((effective_spec, rows, raw_frame, None))
            except Exception as exc:
                results.append((spec, [], None, exc))
        return results

    def fetch_cme_context(self, spec: DatabentoCmeContextSpec) -> tuple[list[dict[str, Any]], pd.DataFrame, DatabentoCmeContextSpec]:
        client = self._client()
        spec = replace(spec, initial_start=spec.initial_start or spec.start, initial_end=spec.initial_end or spec.end)
        raw_frames: list[pd.DataFrame] = []
        effective_specs: list[DatabentoCmeContextSpec] = []
        for request_spec in self._request_specs(spec):
            effective_spec, frame = self._fetch_latest_frame_for_spec(client, request_spec)
            raw_frames.append(frame.reset_index())
            effective_specs.append(effective_spec)
        raw_frame = _deduplicate_provider_rows(_combined_raw_frame(raw_frames))
        context_spec = _combined_effective_spec(spec, effective_specs)
        rows = _context_rows_from_frame(context_spec, raw_frame)
        if not rows:
            rows = [_no_current_rows_status_row(context_spec)]
        return rows, raw_frame, context_spec

    def fetch_cme_context_exact(self, spec: DatabentoCmeContextSpec) -> tuple[list[dict[str, Any]], pd.DataFrame, DatabentoCmeContextSpec]:
        """Fetch exactly one bounded range without latest-state window adjustment."""

        prepared = replace(
            spec,
            initial_start=spec.initial_start or spec.start,
            initial_end=spec.initial_end or spec.end,
        )
        frame = self._fetch_frame(self._client(), prepared)
        raw_frame = _deduplicate_provider_rows(frame.reset_index())
        effective = replace(
            prepared,
            limit_saturated=(
                prepared.limit is not None and len(frame) >= prepared.limit
            ),
            latest_event_timestamp=_latest_event_timestamp(frame),
            availability_status="NO CURRENT ROWS" if frame.empty else "CURRENT",
        )
        rows = _context_rows_from_frame(effective, raw_frame)
        if not rows:
            rows = [_no_current_rows_status_row(effective)]
        return rows, raw_frame, effective

    def _fetch_latest_frame_for_spec(self, client: Any, spec: DatabentoCmeContextSpec) -> tuple[DatabentoCmeContextSpec, pd.DataFrame]:
        latest_spec = spec
        frame = self._fetch_frame(client, latest_spec)
        if spec.limit is None:
            return replace(
                latest_spec,
                latest_event_timestamp=_latest_event_timestamp(frame),
                availability_status="NO CURRENT ROWS" if frame.empty else "CURRENT",
            ), frame
        request_count = 1
        request_bound = min(self.max_requests, MAX_CME_LATEST_WINDOW_REQUESTS)
        expansion_count = 0
        maximum_window = timedelta(
            days=self.lookback_days if _is_ohlcv_schema(spec.schema) else self.max_non_ohlcv_lookback_days
        )
        while frame.empty and request_count < request_bound and latest_spec.end - latest_spec.start < maximum_window:
            expansion_count += 1
            request_count += 1
            next_window = min((latest_spec.end - latest_spec.start) * 2, maximum_window)
            latest_spec = replace(latest_spec, start=latest_spec.end - next_window)
            frame = self._fetch_frame(client, latest_spec)
        availability_status = "NO CURRENT ROWS" if frame.empty else ("BACKTRACKED" if expansion_count else "CURRENT")
        latest_spec = replace(
            latest_spec,
            empty_window_expansion_count=expansion_count,
            latest_event_timestamp=_latest_event_timestamp(frame),
            availability_status=availability_status,
        )
        if frame.empty or expansion_count:
            return replace(latest_spec, limit_saturated=len(frame) >= spec.limit), frame
        shrink_count = 0
        minimum_window = _minimum_latest_window(spec.schema)
        while len(frame) >= spec.limit and latest_spec.end - latest_spec.start > minimum_window and request_count < request_bound:
            shrink_count += 1
            request_count += 1
            next_window = _max_timedelta((latest_spec.end - latest_spec.start) / 2, minimum_window)
            latest_spec = replace(latest_spec, start=latest_spec.end - next_window)
            frame = self._fetch_frame(client, latest_spec)
        return replace(
            latest_spec,
            limit_saturated=len(frame) >= spec.limit,
            latest_window_shrink_count=shrink_count,
            latest_event_timestamp=_latest_event_timestamp(frame),
            availability_status="NO CURRENT ROWS" if frame.empty else "CURRENT",
        ), frame

    def _fetch_frame(self, client: Any, spec: DatabentoCmeContextSpec) -> pd.DataFrame:
        # Empty CME windows are part of normal cursor probing and are handled below
        # as explicit NO CURRENT ROWS evidence.  The SDK otherwise writes its
        # expected BentoWarning to stderr, which makes the production log monitor
        # report a false runtime incident even though the request succeeded.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=(
                    r"^No data found for the request you submitted\."
                    r"(?: The request time range falls entirely inside a weekend\.)?$"
                ),
            )
            if _is_likely_empty_cme_weekend(spec.start, spec.end):
                warnings.filterwarnings(
                    "ignore",
                    message=(
                        r"^The streaming request had one or more symbols which "
                        r"did not resolve: .+\.$"
                    ),
                )
            store = client.timeseries.get_range(**_get_range_kwargs(spec))
        frame = store.to_df()
        return frame if isinstance(frame, pd.DataFrame) else pd.DataFrame(frame)

    def _request_specs(self, spec: DatabentoCmeContextSpec) -> tuple[DatabentoCmeContextSpec, ...]:
        if spec.limit is not None or self.chunk_days is None:
            return (spec,)
        requests: list[DatabentoCmeContextSpec] = []
        start = spec.start
        chunk_delta = timedelta(days=self.chunk_days)
        while start < spec.end:
            end = min(start + chunk_delta, spec.end)
            if not _is_likely_empty_cme_weekend(start, end):
                requests.append(replace(spec, start=start, end=end))
            start = end
        return tuple(requests[-self.max_requests:]) if len(requests) > self.max_requests else tuple(requests)

    def _query_start(self, schema: str, end: datetime) -> datetime:
        if self.limit is not None:
            return end - self._latest_context_window(schema)
        lookback_days = self.lookback_days if _is_ohlcv_schema(schema) else min(self.lookback_days, self.max_non_ohlcv_lookback_days)
        return end - timedelta(days=lookback_days)

    def _latest_context_window(self, schema: str) -> timedelta:
        kind = _schema_kind(schema)
        if kind == "ohlcv":
            return timedelta(hours=self.ohlcv_recent_hours)
        if kind == "bbo":
            return timedelta(minutes=self.bbo_recent_minutes)
        if kind == "book":
            return timedelta(seconds=self.mbp_recent_seconds)
        return timedelta(minutes=self.bbo_recent_minutes)

    def _query_end(self, schema: str) -> datetime:
        now = datetime.now(timezone.utc)
        available_end = self._available_end_for_schema(schema)
        return min(now, available_end) if available_end is not None else now

    def _available_end_for_schema(self, schema: str) -> datetime | None:
        payload = self._client().metadata.get_dataset_range(dataset=self.dataset)
        if not isinstance(payload, Mapping):
            return None
        schema_payload = payload.get("schema")
        if isinstance(schema_payload, Mapping):
            selected = schema_payload.get(schema)
            if isinstance(selected, Mapping):
                end = _datetime_from_value(selected.get("end"))
                if end is not None:
                    return end
        return _datetime_from_value(payload.get("end"))

    def _client(self) -> Any:
        import databento as db
        return db.Historical(self.api_key)

    def _validate_config(self) -> None:
        positive_values = {
            DATABENTO_CME_LOOKBACK_DAYS_ENV: self.lookback_days,
            DATABENTO_CME_MAX_REQUESTS_ENV: self.max_requests,
            DATABENTO_CME_MAX_NON_OHLCV_LOOKBACK_DAYS_ENV: self.max_non_ohlcv_lookback_days,
            DATABENTO_CME_OHLCV_RECENT_HOURS_ENV: self.ohlcv_recent_hours,
            DATABENTO_CME_BBO_RECENT_MINUTES_ENV: self.bbo_recent_minutes,
            DATABENTO_CME_MBP_RECENT_SECONDS_ENV: self.mbp_recent_seconds,
        }
        if not self.api_key:
            raise RuntimeError(f"Missing required environment variable: {DATABENTO_API_KEY_ENV}")
        if not self.dataset:
            raise RuntimeError(f"Missing required environment variable: {DATABENTO_CME_DATASET_ENV}")
        if not self.schemas:
            raise RuntimeError(f"Missing required environment variable: {DATABENTO_CME_SCHEMA_ENV} or {DATABENTO_CME_SCHEMAS_ENV}")
        if not self.context_symbols and not self.contract_symbols:
            raise RuntimeError(f"Missing required environment variable: {DATABENTO_CME_CONTEXT_SYMBOLS_ENV} or {DATABENTO_CME_CONTRACT_SYMBOLS_ENV}")
        if self.limit is not None and self.limit <= 0:
            raise RuntimeError(f"{DATABENTO_CME_LIMIT_ENV} must be greater than zero, blank/unset, or explicitly unlimited.")
        if self.chunk_days is not None and self.chunk_days <= 0:
            raise RuntimeError(f"{DATABENTO_CME_CHUNK_DAYS_ENV} must be greater than zero, zero/blank, or unset.")
        for env_name, value in positive_values.items():
            if value <= 0:
                raise RuntimeError(f"{env_name} must be greater than zero.")


def _get_range_kwargs(spec: DatabentoCmeContextSpec) -> dict[str, Any]:
    kwargs = {"dataset": spec.dataset, "schema": spec.schema, "symbols": list(spec.symbols), "stype_in": spec.stype_in, "start": spec.start.isoformat(), "end": spec.end.isoformat()}
    if spec.limit is not None:
        kwargs["limit"] = spec.limit
    return kwargs


def _combined_raw_frame(raw_frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not raw_frames:
        return pd.DataFrame()
    return raw_frames[0] if len(raw_frames) == 1 else pd.concat(raw_frames, ignore_index=True)


def _deduplicate_provider_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Collapse exact provider duplicates before Duckets adds row metadata."""

    return frame.drop_duplicates().reset_index(drop=True)


def _combined_effective_spec(spec: DatabentoCmeContextSpec, effective_specs: list[DatabentoCmeContextSpec]) -> DatabentoCmeContextSpec:
    if not effective_specs:
        return spec
    return replace(
        spec,
        start=min(request.start for request in effective_specs),
        end=max(request.end for request in effective_specs),
        limit_saturated=any(request.limit_saturated for request in effective_specs),
        latest_window_shrink_count=sum(request.latest_window_shrink_count for request in effective_specs),
        empty_window_expansion_count=sum(request.empty_window_expansion_count for request in effective_specs),
        latest_event_timestamp=max((request.latest_event_timestamp for request in effective_specs), default=""),
        availability_status=(
            "NO CURRENT ROWS"
            if all(request.availability_status == "NO CURRENT ROWS" for request in effective_specs)
            else "BACKTRACKED"
            if any(request.availability_status == "BACKTRACKED" for request in effective_specs)
            else "CURRENT"
        ),
    )


def _context_rows_from_frame(spec: DatabentoCmeContextSpec, raw_frame: pd.DataFrame) -> list[dict[str, Any]]:
    if raw_frame.empty:
        return []
    request_fetched_at = datetime.now(timezone.utc).isoformat()
    return (
        _ohlcv_context_rows_from_frame(spec, raw_frame, request_fetched_at)
        if _is_ohlcv_schema(spec.schema)
        else _generic_context_rows_from_frame(spec, raw_frame, request_fetched_at)
    )


def _ohlcv_context_rows_from_frame(
    spec: DatabentoCmeContextSpec,
    raw_frame: pd.DataFrame,
    request_fetched_at: str,
) -> list[dict[str, Any]]:
    symbol_column = _symbol_column(raw_frame)
    if symbol_column is None and len(spec.symbols) == 1:
        bars = _bars_from_databento_frame(spec.symbols[0], _timeframe_from_schema(spec.schema), raw_frame)
        return _context_rows_from_bars(spec, spec.symbols[0], bars, 0, request_fetched_at)
    if symbol_column is None:
        raise RuntimeError("Databento CME multi-symbol OHLCV response did not include a symbol column.")
    rows: list[dict[str, Any]] = []
    row_index = 0
    for symbol in spec.symbols:
        symbol_frame = raw_frame[raw_frame[symbol_column].astype(str) == symbol]
        if symbol_frame.empty:
            continue
        symbol_rows = _context_rows_from_bars(
            spec,
            symbol,
            _bars_from_databento_frame(symbol, _timeframe_from_schema(spec.schema), symbol_frame),
            row_index,
            request_fetched_at,
        )
        rows.extend(symbol_rows)
        row_index += len(symbol_rows)
    return rows


def _generic_context_rows_from_frame(
    spec: DatabentoCmeContextSpec,
    raw_frame: pd.DataFrame,
    request_fetched_at: str,
) -> list[dict[str, Any]]:
    symbol_column = _symbol_column(raw_frame)
    rows: list[dict[str, Any]] = []
    for row_index, record in enumerate(raw_frame.to_dict(orient="records")):
        # Sampled BBO can carry book state without a matching-engine event.
        # Preserve those provider rows raw; normalized CME rows stay event-timestamped.
        if "ts_event" in record and _timestamp_from_value(record["ts_event"]) is None:
            continue
        provider_symbol = _provider_symbol_from_record(record, symbol_column, spec)
        row = _base_context_row(spec, provider_symbol, row_index, request_fetched_at)
        row["timestamp"] = _timestamp_text_from_record(record)
        for key, value in record.items():
            clean_key = _column_name(str(key))
            row[clean_key if clean_key not in row else f"databento_{clean_key}"] = _parquet_value(value)
        rows.append(row)
    return rows


def _context_rows_from_bars(
    spec: DatabentoCmeContextSpec,
    symbol: str,
    bars: list[MarketBar],
    start_index: int,
    request_fetched_at: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, bar in enumerate(bars):
        row = _base_context_row(spec, symbol, start_index + index, request_fetched_at)
        row.update({"timestamp": bar.timestamp.astimezone(timezone.utc).isoformat(), "timeframe": bar.timeframe, "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close, "volume": bar.volume})
        rows.append(row)
    return rows


def _base_context_row(spec: DatabentoCmeContextSpec, provider_symbol: str, row_index: int, fetched_at: str) -> dict[str, Any]:
    initial_start = spec.initial_start or spec.start
    initial_end = spec.initial_end or spec.end
    return {"symbol": provider_symbol, "source": "databento", "endpoint": "timeseries.get_range", "request_key": spec.key, "macro_context_kind": "cme_futures_cross_asset", "cme_context_group": spec.group_key, "provider_dataset": spec.dataset, "provider_schema": spec.schema, "provider_symbol": provider_symbol, "provider_stype_in": spec.stype_in, "range_start": spec.start.isoformat(), "range_end": spec.end.isoformat(), "initial_range_start": initial_start.isoformat(), "initial_range_end": initial_end.isoformat(), "effective_range_start": spec.start.isoformat(), "effective_range_end": spec.end.isoformat(), "limit": spec.limit, "request_limit_saturated": spec.limit_saturated, "latest_window_shrink_count": spec.latest_window_shrink_count, "empty_window_expansion_count": spec.empty_window_expansion_count, "latest_event_timestamp": spec.latest_event_timestamp, "cme_schema_status": spec.availability_status, "row_index": row_index, "fetched_at": fetched_at}


def _no_current_rows_status_row(spec: DatabentoCmeContextSpec) -> dict[str, Any]:
    fetched_at = datetime.now(timezone.utc).isoformat()
    row = _base_context_row(spec, spec.output_symbol, 0, fetched_at)
    row["timestamp"] = fetched_at
    row["cme_row_kind"] = "schema_status"
    return row


def _latest_event_timestamp(frame: pd.DataFrame) -> str:
    if frame.empty:
        return ""
    reset_frame = frame.reset_index()
    timestamps = [
        timestamp
        for record in reset_frame.to_dict(orient="records")
        if (timestamp := _timestamp_from_value(_first_timestamp_value(record))) is not None
    ]
    return "" if not timestamps else max(timestamps).isoformat()


def _first_timestamp_value(record: Mapping[str, Any]) -> Any:
    for key in ("ts_event", "ts_recv", "timestamp", "time", "datetime", "index"):
        if key in record:
            value = record.get(key)
            if key == "index" and isinstance(value, (int, float)):
                return None
            return value
    return None


def _provider_symbol_from_record(record: Mapping[str, Any], symbol_column: str | None, spec: DatabentoCmeContextSpec) -> str:
    if symbol_column is not None and record.get(symbol_column) not in (None, ""):
        return str(record.get(symbol_column))
    return spec.symbols[0] if len(spec.symbols) == 1 else spec.output_symbol


def _timestamp_text_from_record(record: Mapping[str, Any]) -> str:
    for key in ("ts_event", "ts_recv", "timestamp", "time", "datetime", "index"):
        if key in record:
            timestamp = _timestamp_text_from_value(record.get(key))
            if timestamp:
                return timestamp
    return ""


def _symbol_column(frame: pd.DataFrame) -> str | None:
    return next((column for column in ("symbol", "raw_symbol", "stype_in_symbol") if column in frame.columns), None)


def _is_ohlcv_schema(schema: str) -> bool:
    return schema.startswith("ohlcv-")


def _schema_kind(schema: str) -> str:
    if _is_ohlcv_schema(schema):
        return "ohlcv"
    if schema.startswith("bbo-"):
        return "bbo"
    if schema.startswith("mbp-") or schema == "mbo" or schema.startswith("mbo-"):
        return "book"
    return "other"


def _minimum_latest_window(schema: str) -> timedelta:
    return MIN_LATEST_WINDOWS[_schema_kind(schema)]


def _is_likely_empty_cme_weekend(start: datetime, end: datetime) -> bool:
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    return start_utc.weekday() == 5 or (start_utc.weekday() == 6 and end_utc.hour < 22)


def _timeframe_from_schema(schema: str) -> str:
    return schema[len("ohlcv-"):] if schema.startswith("ohlcv-") else schema


def _configured_schemas(value: str | tuple[str, ...] | None, fallback: str) -> tuple[str, ...]:
    if value is not None and not isinstance(value, tuple) and value.strip():
        return _configured_symbols(value)
    if isinstance(value, tuple) and value:
        return _configured_symbols(value)
    return _configured_symbols(fallback) if fallback.strip() else (DEFAULT_CME_SCHEMA,)


def _configured_symbols(value: str | tuple[str, ...]) -> tuple[str, ...]:
    candidates = value if isinstance(value, tuple) else _configured_symbol_candidates(value)
    values: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        item = _strip_env_quotes(str(candidate).strip())
        if item and item not in seen:
            values.append(item)
            seen.add(item)
    return tuple(values)


def _configured_symbol_candidates(value: str) -> tuple[str, ...] | list[str]:
    configured = _strip_env_quotes(value.strip())
    if not configured:
        return ()
    if configured.startswith("["):
        loaded = json.loads(configured)
        if not isinstance(loaded, list):
            raise ValueError("Databento symbol/schema JSON must be a list.")
        return [str(item) for item in loaded]
    return configured.replace(";", ",").split(",")


def _configured_int(env_name: str, default: int) -> int:
    value = os.getenv(env_name, "").strip()
    return default if not value else int(value)


def _configured_bool(env_name: str, default: bool) -> bool:
    value = os.getenv(env_name, "").strip().lower()
    return default if not value else value in {"1", "true", "yes", "y", "on"}


def _configured_limit(value: int | str | None, *, default: int, allow_unlimited: bool) -> int | None:
    if value is None:
        return default
    if isinstance(value, int):
        return None if value == 0 and allow_unlimited else (default if value == 0 else value)
    cleaned = value.strip().lower()
    if not cleaned:
        return default
    if cleaned in UNLIMITED_LIMIT_TOKENS:
        return None if allow_unlimited else default
    return int(cleaned)


def _configured_optional_int(env_name: str, default: int | None) -> int | None:
    value = os.getenv(env_name, "").strip().lower()
    if not value:
        return default
    return None if value in UNLIMITED_LIMIT_TOKENS else int(value)


def _datetime_from_value(value: Any) -> datetime | None:
    timestamp = _timestamp_from_value(value)
    if timestamp is None:
        return None
    try:
        return timestamp.to_pydatetime(warn=False)
    except TypeError:
        return timestamp.to_pydatetime()


def _timestamp_from_value(value: Any) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    try:
        timestamp = pd.Timestamp(value)
    except Exception:
        return None
    try:
        if pd.isna(timestamp):
            return None
    except Exception:
        pass
    return timestamp.tz_localize(timezone.utc) if timestamp.tzinfo is None else timestamp.tz_convert(timezone.utc)


def _timestamp_text_from_value(value: Any) -> str:
    timestamp = _timestamp_from_value(value)
    return "" if timestamp is None else timestamp.isoformat()


def _parquet_value(value: Any) -> Any:
    if value in (None, ""):
        return None if value is None else value
    if isinstance(value, pd.Timestamp):
        return _timestamp_text_from_value(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat() if value.tzinfo else value.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str)
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _column_name(value: str) -> str:
    clean = "".join(character.lower() if character.isalnum() else "_" for character in value.strip())
    while "__" in clean:
        clean = clean.replace("__", "_")
    return clean.strip("_") or "value"


def _strip_env_quotes(value: str) -> str:
    if len(value) < 2:
        return value
    quote = value[0]
    return value[1:-1] if quote == value[-1] and quote in ("'", '"') else value


def _max_timedelta(left: timedelta, right: timedelta) -> timedelta:
    return left if left >= right else right
