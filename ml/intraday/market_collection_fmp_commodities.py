"""Exact native commodity quotes through FMP's documented single-symbol API.

Each configured symbol has a separate source identity and denial history. A
rejected symbol cannot suppress a different, available commodity. This module
does not replace native symbols with funds or manufacture a current quote from
the provider's previous close.
"""
from __future__ import annotations

import re

from .market_collection import CollectionFailure, CollectionResult, SourceRequest, classify_error

POLICY = "fmp-native-single-commodity-quote-v1"


def native_commodity_requests(symbols):
    symbols = tuple(symbols)
    if len(set(symbols)) != len(symbols) or any(
        not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9._/-]{1,30}", symbol)
        for symbol in symbols
    ):
        raise ValueError("Native commodity symbols must be exact, valid and unique")
    return tuple(SourceRequest("fmp", "cross_asset", "*", "stable", "commodity-quote",
        "quote", {"symbol": symbol, "configured_symbols": [symbol], "quote_policy": POLICY})
        for symbol in symbols)


def handles(request):
    return request.provider == "fmp" and request.source_kind == "cross_asset" and request.schema == "commodity-quote"


def collect(request, start, end, max_records, *, provider_factory):
    """Collect one native identity. Legacy bulk requests fail before client I/O."""
    if not handles(request):
        raise ValueError("Not a native commodity request")
    if request.endpoint == "batch-commodity-quotes":
        return CollectionResult([], "UNSUPPORTED",
            "Legacy commodity batch transport is outside the reviewed Premium request policy; migrate to distinct single-symbol quote sources",
            {"endpoint": request.endpoint, "provider_called": False, "request_policy": POLICY})
    symbol = request.params.get("symbol")
    if (request.symbol != "*" or request.dataset != "stable" or request.endpoint != "quote"
            or request.params != {"symbol": symbol, "configured_symbols": [symbol], "quote_policy": POLICY}
            or not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9._/-]{1,30}", symbol)):
        raise ValueError("Invalid exact single-symbol native commodity request")
    evidence = {"endpoint": "quote", "resolved_params": {"symbol": symbol},
        "configured_symbols": [symbol], "request_policy": POLICY,
        "mode": "current_rest_snapshot", "provider_called": True}
    try:
        payload = provider_factory()._get_json("quote", {"symbol": symbol})
    except Exception as exc:
        status, reason = classify_error(exc)
        http_status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(http_status, int):
            evidence["http_status"] = http_status
        if status == "NOT_AUTHORIZED":
            reason = "Provider denied this exact native commodity quote request; no entitlement inferred for other symbols"
        raise CollectionFailure(status, reason, evidence) from None
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise CollectionFailure("ERROR", "Unexpected FMP native commodity result shape", evidence)
    if any(row.get("symbol") != symbol for row in payload):
        raise CollectionFailure("ERROR", "FMP returned a commodity outside the exact requested scope", evidence)
    from .market_collection_adapters import _utc, quote_record
    records = [quote_record(row, symbol, event_at=_utc(row.get("timestamp"), unit="s", local_zone="UTC"))
        for row in payload]
    evidence["missing_configured_symbols"] = [] if records else [symbol]
    evidence["returned_records"] = len(records)
    return CollectionResult(records, evidence=evidence)
