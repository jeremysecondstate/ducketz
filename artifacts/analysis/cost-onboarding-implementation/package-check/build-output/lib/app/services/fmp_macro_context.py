from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import requests

FMP_API_KEY_ENV = "FMP_API_KEY"
FMP_BASE_URL_ENV = "FMP_BASE_URL"
FMP_COMMODITY_SYMBOLS_ENV = "DUCKETS_FMP_COMMODITY_SYMBOLS"
FMP_COMMODITY_PROXY_FALLBACKS_ENV = "DUCKETS_FMP_COMMODITY_PROXY_FALLBACKS"

DEFAULT_FMP_BASE_URL = "https://financialmodelingprep.com/stable"
DEFAULT_COMMODITY_PROXY_SYMBOLS = ("CLUSD", "BZUSD", "NGUSD")
DEFAULT_COMMODITY_PROXY_FALLBACKS: dict[str, tuple[str, ...]] = {
    "CLUSD": ("USO",),
    "BZUSD": ("BNO",),
    "NGUSD": ("UNG",),
}
COMMODITY_BATCH_ENDPOINT = "batch-commodity-quotes"

COMMODITY_PROXY_ALIASES: dict[str, tuple[str, ...]] = {
    "CLUSD": ("CLUSD", "WTI", "LIGHT SWEET CRUDE"),
    "BZUSD": ("BZUSD", "BRENT", "BRENT CRUDE"),
    "NGUSD": ("NGUSD", "NATURAL GAS"),
}


@dataclass(frozen=True)
class FmpMacroSeriesDefinition:
    display_name: str
    series_type: str
    unit: str
    change_unit: str
    cadence: str
    freshness_limit_days: int | None
    relevance_limit: str


FMP_MACRO_SERIES_DEFINITIONS: dict[str, FmpMacroSeriesDefinition] = {
    "BZUSD": FmpMacroSeriesDefinition(
        "Brent crude oil",
        "FMP BZUSD Brent crude commodity quote",
        "USD per barrel",
        "USD per barrel",
        "market quote",
        3,
        "Broad energy-price temporal context. Company sensitivity requires a separately measured relationship and threshold.",
    ),
    "CLUSD": FmpMacroSeriesDefinition(
        "WTI crude oil",
        "FMP CLUSD WTI crude commodity quote",
        "USD per barrel",
        "USD per barrel",
        "market quote",
        3,
        "Broad energy-price temporal context. Company sensitivity requires a separately measured relationship and threshold.",
    ),
    "CPI": FmpMacroSeriesDefinition(
        "Consumer Price Index",
        "FMP economic-indicators CPI series: U.S. CPI-U, all items, seasonally adjusted",
        "index points (1982-1984=100)",
        "index points",
        "monthly",
        62,
        "Broad U.S. consumer-price temporal context. Company inflation sensitivity requires a separately measured relationship and threshold.",
    ),
    "GDP": FmpMacroSeriesDefinition(
        "Gross Domestic Product",
        "FMP economic-indicators GDP series: U.S. nominal GDP, seasonally adjusted annual rate",
        "billion USD",
        "billion USD",
        "quarterly",
        140,
        "Broad nominal U.S. activity context. Real-growth analysis requires an inflation-adjusted series; company sensitivity requires a separately measured relationship.",
    ),
    "NGUSD": FmpMacroSeriesDefinition(
        "Natural gas",
        "FMP NGUSD natural-gas commodity quote",
        "USD per MMBtu",
        "USD per MMBtu",
        "market quote",
        3,
        "Broad energy-price temporal context. Company sensitivity requires a separately measured relationship and threshold.",
    ),
    "federalFunds": FmpMacroSeriesDefinition(
        "Effective federal funds rate",
        "FMP economic-indicators federalFunds series: monthly average effective federal funds rate",
        "percent",
        "percentage points",
        "monthly",
        62,
        "Broad short-rate and financing context. Company rate sensitivity requires a separately measured relationship and threshold.",
    ),
    "unemploymentRate": FmpMacroSeriesDefinition(
        "Unemployment rate",
        "FMP economic-indicators unemploymentRate series: U.S. civilian unemployment rate, seasonally adjusted",
        "percent",
        "percentage points",
        "monthly",
        62,
        "Broad U.S. labor-market context. Company demand sensitivity requires a separately measured relationship and threshold.",
    ),
}

FMP_EXCHANGE_TRADED_FALLBACKS: dict[str, str] = {
    "BNO": "United States Brent Oil Fund ETF",
    "UNG": "United States Natural Gas Fund ETF",
    "USO": "United States Oil Fund ETF",
}


def fmp_macro_series_definition(
    symbol: str,
    *,
    provider_symbol: str = "",
    proxy_fallback_for: str = "",
    kind: str = "",
) -> FmpMacroSeriesDefinition:
    canonical_symbol = str(symbol).strip()
    matched_symbol = next(
        (key for key in FMP_MACRO_SERIES_DEFINITIONS if key.casefold() == canonical_symbol.casefold()),
        canonical_symbol,
    )
    definition = FMP_MACRO_SERIES_DEFINITIONS.get(matched_symbol)
    if definition is None:
        is_quote = kind == "commodity_proxy_quote"
        definition = FmpMacroSeriesDefinition(
            canonical_symbol or "FMP macro observation",
            (
                f"FMP quote for {provider_symbol or canonical_symbol or 'unspecified symbol'}"
                if is_quote
                else f"FMP economic-indicators series {canonical_symbol or 'with unspecified name'}"
            ),
            "provider-reported units (scale not specified)",
            "provider-reported units",
            "market quote" if is_quote else "provider cadence not specified",
            3 if is_quote else None,
            "Cross-asset temporal context. A company link requires a separately measured relationship and threshold.",
        )

    fallback_symbol = str(provider_symbol).strip().upper()
    fallback_for = str(proxy_fallback_for).strip().upper()
    if not fallback_for:
        return definition

    fallback_name = FMP_EXCHANGE_TRADED_FALLBACKS.get(fallback_symbol)
    if fallback_name is None:
        return replace(
            definition,
            series_type=(
                f"FMP {fallback_symbol or 'unspecified-symbol'} quote used as a configured fallback for {fallback_for}; "
                "the instrument type and price scale are not identified by Duckets"
            ),
            unit="provider-reported quote units (instrument scale not identified)",
            change_unit="provider-reported quote units",
            relevance_limit=(
                "Configured fallback quote with an unidentified instrument scale. A company link requires a separately "
                "measured relationship and threshold."
            ),
        )

    return replace(
        definition,
        series_type=f"FMP {fallback_symbol} {fallback_name} share-price quote used as a fallback for {fallback_for}",
        unit="USD per share",
        change_unit="USD per share",
        relevance_limit=(
            f"{fallback_symbol} is an exchange-traded fund share-price proxy for the commodity. "
            "A company link requires a separately measured relationship and threshold."
        ),
    )


class FmpRequestError(RuntimeError):
    def __init__(
        self,
        *,
        endpoint: str,
        status_code: int | None,
        payload: Any,
    ) -> None:
        self.endpoint = endpoint
        self.status_code = status_code
        self.payload = payload

        status = f"HTTP {status_code}" if status_code is not None else "API error"
        super().__init__(f"FMP {endpoint} request failed with {status}: {_payload_text(payload)}")


class FmpAccessDeniedError(FmpRequestError):
    pass


@dataclass(frozen=True)
class FmpMacroContextSpec:
    key: str
    endpoint: str
    params: Mapping[str, Any]
    output_symbol: str
    kind: str
    provider_symbol: str = ""
    proxy_fallback_for: str = ""


class FmpMacroContextProvider:
    source = "fmp"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 30,
        commodity_symbols: str | Iterable[str] | None = None,
        commodity_proxy_fallbacks: str | Mapping[str, Iterable[str] | str] | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv(FMP_API_KEY_ENV, "").strip()
        configured_base_url = base_url if base_url is not None else os.getenv(FMP_BASE_URL_ENV, "").strip()
        configured_commodity_symbols = (
            commodity_symbols
            if commodity_symbols is not None
            else os.getenv(FMP_COMMODITY_SYMBOLS_ENV, "").strip()
        )
        configured_fallbacks = (
            commodity_proxy_fallbacks
            if commodity_proxy_fallbacks is not None
            else os.getenv(FMP_COMMODITY_PROXY_FALLBACKS_ENV, "").strip()
        )

        self.base_url = (configured_base_url or DEFAULT_FMP_BASE_URL).rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.commodity_symbols = _commodity_symbols(configured_commodity_symbols)
        self.commodity_proxy_fallbacks = _commodity_proxy_fallbacks(configured_fallbacks)

    def macro_specs(self) -> tuple[FmpMacroContextSpec, ...]:
        return (*self.economic_indicator_specs(), *self.commodity_proxy_specs())

    def economic_indicator_specs(self) -> tuple[FmpMacroContextSpec, ...]:
        return (
            FmpMacroContextSpec("economic_indicator", "economic-indicators", {"name": "GDP"}, "GDP", "economic_indicator"),
            FmpMacroContextSpec("economic_indicator", "economic-indicators", {"name": "CPI"}, "CPI", "economic_indicator"),
            FmpMacroContextSpec("economic_indicator", "economic-indicators", {"name": "unemploymentRate"}, "unemploymentRate", "economic_indicator"),
            FmpMacroContextSpec("economic_indicator", "economic-indicators", {"name": "federalFunds"}, "federalFunds", "economic_indicator"),
        )

    def commodity_proxy_specs(self) -> tuple[FmpMacroContextSpec, ...]:
        return tuple(
            FmpMacroContextSpec(
                "quote",
                COMMODITY_BATCH_ENDPOINT,
                {},
                symbol,
                "commodity_proxy_quote",
                provider_symbol=symbol,
            )
            for symbol in self.commodity_symbols
        )

    def fetch_all_macro_context(
        self,
    ) -> list[tuple[FmpMacroContextSpec, list[dict[str, Any]], Any | None, Exception | None]]:
        results: list[tuple[FmpMacroContextSpec, list[dict[str, Any]], Any | None, Exception | None]] = []

        for spec in self.economic_indicator_specs():
            try:
                rows, raw_payload = self.fetch_macro_context(spec)
                results.append((spec, rows, raw_payload, None))
            except Exception as exc:
                results.append((spec, [], None, exc))

        commodity_specs = self.commodity_proxy_specs()
        if commodity_specs:
            results.extend(self.fetch_commodity_proxy_quotes(commodity_specs))

        return results

    def fetch_macro_context(self, spec: FmpMacroContextSpec) -> tuple[list[dict[str, Any]], Any]:
        payload = self._get_json(spec.endpoint, spec.params)
        rows = _macro_rows_from_payload(spec=spec, payload=payload)
        if not rows:
            raise RuntimeError(f"FMP {spec.output_symbol} {spec.key} returned no data.")
        return rows, payload

    def fetch_commodity_proxy_quotes(
        self,
        specs: tuple[FmpMacroContextSpec, ...],
    ) -> list[tuple[FmpMacroContextSpec, list[dict[str, Any]], Any | None, Exception | None]]:
        try:
            payload = self._get_json(COMMODITY_BATCH_ENDPOINT, {})
        except Exception:
            return [self._fetch_single_symbol_commodity_proxy(spec) for spec in specs]

        results: list[tuple[FmpMacroContextSpec, list[dict[str, Any]], Any | None, Exception | None]] = []
        for spec in specs:
            raw_payload = _commodity_payload_for_symbol(payload, spec.output_symbol)
            rows = _macro_rows_from_payload(spec=spec, payload=raw_payload)
            if rows:
                results.append((spec, rows, raw_payload, None))
                continue

            results.append(self._fetch_single_symbol_commodity_proxy(spec))

        return results

    def _fetch_single_symbol_commodity_proxy(
        self,
        spec: FmpMacroContextSpec,
    ) -> tuple[FmpMacroContextSpec, list[dict[str, Any]], Any | None, Exception | None]:
        direct_spec = _single_symbol_quote_spec(spec, spec.output_symbol)
        try:
            rows, raw_payload = self.fetch_macro_context(direct_spec)
            return (direct_spec, rows, raw_payload, None)
        except FmpAccessDeniedError as access_exc:
            return self._fetch_fallback_symbol_commodity_proxy(spec, access_exc)
        except Exception as exc:
            return (direct_spec, [], None, exc)

    def _fetch_fallback_symbol_commodity_proxy(
        self,
        spec: FmpMacroContextSpec,
        access_exc: FmpAccessDeniedError,
    ) -> tuple[FmpMacroContextSpec, list[dict[str, Any]], Any | None, Exception | None]:
        fallback_symbols = self.commodity_proxy_fallbacks.get(spec.output_symbol, ())
        if not fallback_symbols:
            direct_spec = _single_symbol_quote_spec(spec, spec.output_symbol)
            return (direct_spec, [], None, access_exc)

        last_exc: Exception = access_exc
        for fallback_symbol in fallback_symbols:
            fallback_spec = _single_symbol_quote_spec(
                spec,
                fallback_symbol,
                proxy_fallback_for=spec.output_symbol,
            )
            try:
                rows, raw_payload = self.fetch_macro_context(fallback_spec)
                return (fallback_spec, rows, raw_payload, None)
            except Exception as exc:
                last_exc = exc

        last_fallback_spec = _single_symbol_quote_spec(
            spec,
            fallback_symbols[-1],
            proxy_fallback_for=spec.output_symbol,
        )
        return (last_fallback_spec, [], None, last_exc)

    def _get_json(self, endpoint: str, params: Mapping[str, Any]) -> Any:
        self._validate_config()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        request_params = {**dict(params), "apikey": self.api_key}

        response = requests.get(url, params=request_params, timeout=self.timeout_seconds)
        try:
            payload = response.json()
        except ValueError:
            payload = response.text

        if response.status_code >= 400:
            error_cls = FmpAccessDeniedError if _looks_like_access_denied(response.status_code, payload) else FmpRequestError
            raise error_cls(endpoint=endpoint, status_code=response.status_code, payload=payload)

        if _looks_like_error_payload(payload):
            error_cls = FmpAccessDeniedError if _looks_like_access_denied(None, payload) else FmpRequestError
            raise error_cls(endpoint=endpoint, status_code=None, payload=payload)

        return payload

    def _validate_config(self) -> None:
        if not self.api_key:
            raise RuntimeError(f"Missing required environment variable: {FMP_API_KEY_ENV}")
        if not self.base_url:
            raise RuntimeError(f"Missing required environment variable: {FMP_BASE_URL_ENV}")


def _macro_rows_from_payload(*, spec: FmpMacroContextSpec, payload: Any) -> list[dict[str, Any]]:
    mappings = _payload_mappings(payload)
    fetched_at = datetime.now(timezone.utc).isoformat()

    return [
        _macro_row(
            spec=spec,
            row_index=index,
            fetched_at=fetched_at,
            payload_row=mapping,
        )
        for index, mapping in enumerate(mappings)
    ]


def _macro_row(
    *,
    spec: FmpMacroContextSpec,
    row_index: int,
    fetched_at: str,
    payload_row: Mapping[str, Any],
) -> dict[str, Any]:
    definition = fmp_macro_series_definition(
        spec.output_symbol,
        provider_symbol=spec.provider_symbol or str(spec.params.get("symbol", spec.output_symbol)),
        proxy_fallback_for=spec.proxy_fallback_for,
        kind=spec.kind,
    )
    row: dict[str, Any] = {
        "symbol": spec.output_symbol,
        "source": "fmp",
        "endpoint": spec.endpoint,
        "request_key": spec.key,
        "macro_context_kind": spec.kind,
        "provider_symbol": spec.provider_symbol or str(spec.params.get("symbol", spec.output_symbol)),
        "proxy_fallback_for": spec.proxy_fallback_for,
        "is_proxy_fallback": bool(spec.proxy_fallback_for),
        "series_label": definition.display_name,
        "series_type": definition.series_type,
        "unit": definition.unit,
        "change_unit": definition.change_unit,
        "cadence": definition.cadence,
        "freshness_limit_days": definition.freshness_limit_days,
        "relevance_limit": definition.relevance_limit,
        "row_index": row_index,
        "fetched_at": fetched_at,
    }

    for key, value in payload_row.items():
        clean_key = _column_name(str(key))
        if clean_key in row:
            clean_key = f"fmp_{clean_key}"
        row[clean_key] = _parquet_value(value)

    return row


def _payload_mappings(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, list):
            data_rows = [item for item in data if isinstance(item, Mapping)]
            if data_rows:
                return data_rows
        return [payload]

    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]

    return []


def _commodity_payload_for_symbol(payload: Any, symbol: str) -> list[Mapping[str, Any]]:
    return [
        mapping
        for mapping in _payload_mappings(payload)
        if _mapping_matches_symbol(mapping, symbol)
    ]


def _single_symbol_quote_spec(
    spec: FmpMacroContextSpec,
    provider_symbol: str,
    *,
    proxy_fallback_for: str = "",
) -> FmpMacroContextSpec:
    output_symbol = proxy_fallback_for or spec.output_symbol
    return FmpMacroContextSpec(
        key=spec.key,
        endpoint="quote",
        params={"symbol": provider_symbol},
        output_symbol=output_symbol,
        kind=spec.kind,
        provider_symbol=provider_symbol,
        proxy_fallback_for=proxy_fallback_for,
    )


def _mapping_matches_symbol(mapping: Mapping[str, Any], symbol: str) -> bool:
    target = _symbol_token(symbol)
    aliases = {_symbol_token(alias) for alias in COMMODITY_PROXY_ALIASES.get(target, (symbol,))}
    aliases.add(target)

    for key in ("symbol", "ticker"):
        value = mapping.get(key)
        if value is not None and _symbol_token(str(value)) == target:
            return True

    for value in mapping.values():
        if isinstance(value, (dict, list, tuple)):
            continue

        candidate = _symbol_token(str(value))
        if not candidate:
            continue

        if candidate == target:
            return True

        if any(alias and (alias == candidate or alias in candidate or candidate in alias) for alias in aliases):
            return True

    return False


def _looks_like_error_payload(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False

    error_keys = {"error", "Error Message", "message"}
    return any(key in payload and payload[key] for key in error_keys)


def _looks_like_access_denied(status_code: int | None, payload: Any) -> bool:
    if status_code in {401, 402, 403}:
        return True

    text = _payload_text(payload).lower()
    access_denied_fragments = (
        "not available under your current subscription",
        "premium query parameter",
        "restricted endpoint",
        "limited access",
        "unauthorized",
        "forbidden",
        "access denied",
    )
    return any(fragment in text for fragment in access_denied_fragments)


def _commodity_symbols(value: str | Iterable[str] | None) -> tuple[str, ...]:
    if value is None:
        candidates: Iterable[str] = DEFAULT_COMMODITY_PROXY_SYMBOLS
    elif isinstance(value, str):
        stripped = value.strip()
        candidates = DEFAULT_COMMODITY_PROXY_SYMBOLS if not stripped else stripped.replace(";", ",").split(",")
    else:
        candidates = value

    symbols: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        symbol = str(candidate).strip().upper()
        if not symbol or symbol in seen:
            continue
        symbols.append(symbol)
        seen.add(symbol)

    return tuple(symbols)


def _commodity_proxy_fallbacks(value: str | Mapping[str, Iterable[str] | str] | None) -> dict[str, tuple[str, ...]]:
    if value is None or value == "":
        return dict(DEFAULT_COMMODITY_PROXY_FALLBACKS)

    if isinstance(value, Mapping):
        return {
            str(key).strip().upper(): _commodity_symbols(symbols)
            for key, symbols in value.items()
            if str(key).strip()
        }

    fallbacks: dict[str, tuple[str, ...]] = {}
    for item in value.replace("|", ";").split(";"):
        if not item.strip() or "=" not in item:
            continue
        key, symbols = item.split("=", 1)
        output_symbol = key.strip().upper()
        if not output_symbol:
            continue
        fallbacks[output_symbol] = _commodity_symbols(symbols)

    return fallbacks


def _payload_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, default=str)


def _parquet_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str)
    return value


def _column_name(value: str) -> str:
    clean = "".join(character.lower() if character.isalnum() else "_" for character in value.strip())
    while "__" in clean:
        clean = clean.replace("__", "_")
    return clean.strip("_") or "value"


def _symbol_token(value: str) -> str:
    return "".join(character.upper() for character in value if character.isalnum())
