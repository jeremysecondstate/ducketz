from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import requests
import pandas as pd

FMP_API_KEY_ENV = "FMP_API_KEY"
FMP_BASE_URL_ENV = "FMP_BASE_URL"

DEFAULT_FMP_BASE_URL = "https://financialmodelingprep.com/stable"
SEC_FILINGS_LOOKBACK_DAYS = 370
SEC_FILINGS_LIMIT = 100
ANNUAL_STATEMENT_LIMIT = 20
QUARTERLY_STATEMENT_LIMIT = 48


@dataclass(frozen=True)
class FmpCorporateDataSpec:
    key: str
    endpoint: str
    params: Mapping[str, Any]
    fallback_endpoint: str = ""
    fallback_params: Mapping[str, Any] | None = None


class FmpCorporateDataProvider:
    source = "fmp"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv(FMP_API_KEY_ENV, "").strip()
        configured_base_url = base_url if base_url is not None else os.getenv(FMP_BASE_URL_ENV, "").strip()
        self.base_url = (configured_base_url or DEFAULT_FMP_BASE_URL).rstrip("/")
        self.timeout_seconds = timeout_seconds

    def corporate_specs(self, symbol: str) -> tuple[FmpCorporateDataSpec, ...]:
        clean_symbol = _symbol(symbol)
        today = datetime.now(timezone.utc).date()
        filings_start = today - timedelta(days=SEC_FILINGS_LOOKBACK_DAYS)

        return (
            FmpCorporateDataSpec("profile", "profile", {"symbol": clean_symbol}),
            FmpCorporateDataSpec("quote", "quote", {"symbol": clean_symbol}),
            FmpCorporateDataSpec(
                "market_capitalization",
                "market-capitalization-batch",
                {"symbols": clean_symbol},
                fallback_endpoint="market-capitalization",
                fallback_params={"symbol": clean_symbol},
            ),
            FmpCorporateDataSpec("shares_float", "shares-float", {"symbol": clean_symbol}),
            FmpCorporateDataSpec("key_metrics", "key-metrics", {"symbol": clean_symbol}),
            FmpCorporateDataSpec("key_metrics_ttm", "key-metrics-ttm", {"symbol": clean_symbol}),
            FmpCorporateDataSpec("ratios_ttm", "ratios-ttm", {"symbol": clean_symbol}),
            *_statement_specs(clean_symbol),
            FmpCorporateDataSpec(
                "cash_flow_statement_growth",
                "cash-flow-statement-growth",
                {"symbol": clean_symbol},
            ),
            FmpCorporateDataSpec(
                "income_statement_growth",
                "income-statement-growth",
                {"symbol": clean_symbol},
            ),
            FmpCorporateDataSpec("financial_growth", "financial-growth", {"symbol": clean_symbol}),
            FmpCorporateDataSpec(
                "sec_filings_search_symbol",
                "sec-filings-search/symbol",
                {
                    "symbol": clean_symbol,
                    "from": filings_start.isoformat(),
                    "to": today.isoformat(),
                    "page": 0,
                    "limit": SEC_FILINGS_LIMIT,
                },
            ),
        )

    def fetch_all_corporate_data(
        self,
        symbol: str,
    ) -> list[tuple[FmpCorporateDataSpec, list[dict[str, Any]], Any | None, str, Exception | None]]:
        results: list[tuple[FmpCorporateDataSpec, list[dict[str, Any]], Any | None, str, Exception | None]] = []

        for spec in self.corporate_specs(symbol):
            endpoint_used = spec.endpoint
            try:
                rows, raw_payload, endpoint_used = self.fetch_corporate_data(symbol, spec)
                results.append((spec, rows, raw_payload, endpoint_used, None))
            except Exception as exc:
                results.append((spec, [], None, endpoint_used, exc))

        return results

    def fetch_corporate_data(
        self,
        symbol: str,
        spec: FmpCorporateDataSpec,
    ) -> tuple[list[dict[str, Any]], Any, str]:
        clean_symbol = _symbol(symbol)
        endpoint_used = spec.endpoint
        payload = self._get_json(spec.endpoint, spec.params)

        if _empty_payload(payload) and spec.fallback_endpoint:
            endpoint_used = spec.fallback_endpoint
            payload = self._get_json(spec.fallback_endpoint, spec.fallback_params or {"symbol": clean_symbol})

        rows = _corporate_rows_from_payload(
            symbol=clean_symbol,
            request_key=spec.key,
            endpoint=endpoint_used,
            payload=payload,
        )
        if not rows:
            raise RuntimeError(f"FMP {spec.key} returned no data for {clean_symbol}.")

        return rows, payload, endpoint_used

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
            raise RuntimeError(f"FMP {endpoint} request failed with HTTP {response.status_code}: {payload}")

        if _looks_like_error_payload(payload):
            raise RuntimeError(f"FMP {endpoint} request failed: {payload}")

        return payload

    def _validate_config(self) -> None:
        if not self.api_key:
            raise RuntimeError(f"Missing required environment variable: {FMP_API_KEY_ENV}")
        if not self.base_url:
            raise RuntimeError(f"Missing required environment variable: {FMP_BASE_URL_ENV}")


def _statement_specs(symbol: str) -> tuple[FmpCorporateDataSpec, ...]:
    specs: list[FmpCorporateDataSpec] = []
    for key, endpoint in (
        ("income_statement", "income-statement"),
        ("balance_sheet_statement", "balance-sheet-statement"),
        ("cash_flow_statement", "cash-flow-statement"),
    ):
        specs.extend(
            [
                FmpCorporateDataSpec(
                    f"{key}_annual",
                    endpoint,
                    {"symbol": symbol, "period": "annual", "limit": ANNUAL_STATEMENT_LIMIT},
                ),
                FmpCorporateDataSpec(
                    f"{key}_quarterly",
                    endpoint,
                    {"symbol": symbol, "period": "quarter", "limit": QUARTERLY_STATEMENT_LIMIT},
                ),
            ]
        )
    return tuple(specs)


def _corporate_rows_from_payload(
    *,
    symbol: str,
    request_key: str,
    endpoint: str,
    payload: Any,
) -> list[dict[str, Any]]:
    mappings = _payload_mappings(payload)
    fetched_at = datetime.now(timezone.utc).isoformat()

    return [
        _corporate_row(
            symbol=symbol,
            request_key=request_key,
            endpoint=endpoint,
            fetched_at=fetched_at,
            row_index=index,
            payload_row=mapping,
        )
        for index, mapping in enumerate(mappings)
    ]


def _corporate_row(
    *,
    symbol: str,
    request_key: str,
    endpoint: str,
    fetched_at: str,
    row_index: int,
    payload_row: Mapping[str, Any],
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": symbol,
        "source": "fmp",
        "endpoint": endpoint,
        "request_key": request_key,
        "row_index": row_index,
        "fetched_at": fetched_at,
    }

    for key, value in payload_row.items():
        clean_key = _column_name(str(key))
        if clean_key in row:
            clean_key = f"fmp_{clean_key}"
        row[clean_key] = _parquet_value(value)

    if "_statement_" in f"_{request_key}_":
        publication = _statement_publication_timestamp(row)
        receipt = pd.Timestamp(fetched_at)
        receipt = (
            receipt.tz_localize("UTC")
            if receipt.tzinfo is None
            else receipt.tz_convert("UTC")
        )
        row["published_at"] = publication
        row["available_at"] = max(
            timestamp
            for timestamp in (publication, receipt)
            if timestamp is not None
        )
        row["effective_date_estimated"] = publication is None

    return row


def _statement_publication_timestamp(
    row: Mapping[str, Any],
) -> pd.Timestamp | None:
    for column in (
        "accepted_date",
        "accepteddate",
        "accepted_at",
        "acceptedat",
        "published_date",
        "publisheddate",
        "published_at",
        "publishedat",
        "filling_date",
        "fillingdate",
        "filing_date",
        "filingdate",
    ):
        text = str(row.get(column) or "").strip()
        if not text:
            continue
        parsed = pd.to_datetime(text, utc=True, errors="coerce")
        if pd.isna(parsed):
            continue
        timestamp = pd.Timestamp(parsed)
        if len(text) <= 10:
            timestamp = timestamp.normalize() + pd.Timedelta(days=1)
        return timestamp
    return None


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


def _empty_payload(payload: Any) -> bool:
    if payload in (None, ""):
        return True
    if isinstance(payload, list) and not payload:
        return True
    if isinstance(payload, Mapping) and not payload:
        return True
    return False


def _looks_like_error_payload(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False

    error_keys = {"error", "Error Message", "message"}
    return any(key in payload and payload[key] for key in error_keys)


def _parquet_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str)
    return value


def _column_name(value: str) -> str:
    clean = "".join(character.lower() if character.isalnum() else "_" for character in value.strip())
    while "__" in clean:
        clean = clean.replace("__", "_")
    return clean.strip("_") or "value"


def _symbol(value: str) -> str:
    cleaned = value.strip().upper()
    if not cleaned:
        raise ValueError("Symbol is required.")
    return cleaned
