"""Bounded FMP GET requests; all outputs stay in the research datastore."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re

from app.services.fmp_corporate_data import FmpCorporateDataProvider

from .policy import symbol
from .storage import ResearchStore

FINANCIAL_ENDPOINTS = {
    "profile": ("profile", {}),
    "income_annual": ("income-statement", {"period": "annual", "limit": 5}),
    "income_quarterly": ("income-statement", {"period": "quarter", "limit": 8}),
    "balance_quarterly": ("balance-sheet-statement", {"period": "quarter", "limit": 8}),
    "cashflow_annual": ("cash-flow-statement", {"period": "annual", "limit": 5}),
    "cashflow_quarterly": ("cash-flow-statement", {"period": "quarter", "limit": 8}),
    "ratios": ("ratios-ttm", {}),
    "metrics": ("key-metrics-ttm", {}),
    "filings": ("sec-filings-search/symbol", {"limit": 30, "page": 0}),
}


def request(provider: FmpCorporateDataProvider, endpoint: str, params: dict) -> dict:
    try:
        rows = provider._get_json(endpoint, params)
        if not isinstance(rows, list):
            raise ValueError("Expected provider rows")
        result = {"rows": rows, "error": None}
    except Exception as exc:
        # Provider/network exception messages may embed authenticated URLs.
        match = re.search(r"HTTP (\d{3})", str(exc))
        status = f" (HTTP {match.group(1)})" if match else ""
        result = {"rows": [], "error": f"{type(exc).__name__}: provider request unavailable{status}"}
    return {"provider": "fmp", "endpoint": endpoint, "params": params,
            "fetched_at": datetime.now(timezone.utc).isoformat(), **result}


def fetch_prices(store: ResearchStore, tickers: list[str], start: str, end: str) -> dict:
    provider = FmpCorporateDataProvider(timeout_seconds=20)
    result = {}
    for ticker in sorted(set(tickers)):
        symbol(ticker)
        payload = request(provider, "historical-price-eod/dividend-adjusted",
                          {"symbol": ticker, "from": start, "to": end})
        payload["basis"] = "fmp_dividend_adjusted_ohlc"
        payload["symbol"] = ticker
        result[ticker] = store.snapshot(payload)
    return result


def fetch_companies(store: ResearchStore, tickers: list[str], start: str, end: str) -> dict:
    if len(set(tickers)) > 12:
        raise ValueError("Investigate at most 12 new companies per fetch")
    provider = FmpCorporateDataProvider(timeout_seconds=20)
    companies = {}
    for ticker in sorted(set(tickers)):
        symbol(ticker)
        data = {}
        for key, (endpoint, params) in FINANCIAL_ENDPOINTS.items():
            if key == "filings":
                params = {**params, "from": (date.fromisoformat(end) - timedelta(days=370)).isoformat(), "to": end}
            data[key] = request(provider, endpoint, {**params, "symbol": ticker})
        data["daily_prices"] = request(provider, "historical-price-eod/full",
                                        {"symbol": ticker, "from": start, "to": end})
        payload = {"symbol": ticker, "fetched_at": datetime.now(timezone.utc).isoformat(), "datasets": data}
        companies[ticker] = store.snapshot(payload)
    return companies
