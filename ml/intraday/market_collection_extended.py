"""Symbol-scoped current research/news and unrestricted requested chain surface.

FMP endpoint contracts: https://site.financialmodelingprep.com/developer/docs
Schwab market endpoint: https://developer.schwab.com/products/trader-api--individual/details/documentation/market-data
The public Schwab portal requires access; optional chain filters and fundamental
projection are also documented by the client maintainer at
https://schwab-py.readthedocs.io/en/latest/client.html .

These helpers use existing authenticated market-data clients only. A bounded
current response is never advertised as complete historical or market coverage.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import hashlib
import math
import re

from .market_collection import CollectionResult, SourceRequest, canonical, classify_error

MAX_PAGES = 3
PAGE_SIZE = 100
FMP_DOCUMENTATION = "https://site.financialmodelingprep.com/developer/docs"
FMP_ENDPOINTS = {
    "analyst-estimates", "ratings-snapshot", "ratings-historical", "price-target-summary",
    "price-target-consensus", "grades", "grades-historical", "grades-consensus",
    "discounted-cash-flow", "levered-discounted-cash-flow", "news/stock", "news/press-releases",
}
PAGED = {"analyst-estimates", "news/stock", "news/press-releases", "sec-filings-search/symbol"}
LIMITED = {"ratings-historical", "grades-historical"}


def _symbol(value):
    value = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", value):
        raise ValueError("An exact equity symbol is required")
    return value


def extended_requests(symbols):
    """Read-only request inventory; actual provider entitlement remains unprobed."""
    requests = []
    for value in symbols:
        symbol = _symbol(value)
        for period in ("annual", "quarter"):
            requests.append(SourceRequest("fmp", "fundamentals", symbol, "stable",
                "analyst-estimates-"+period, "analyst-estimates",
                {"symbol": symbol, "period": period, "page": 0, "limit": PAGE_SIZE}))
        for endpoint in sorted(FMP_ENDPOINTS-{"analyst-estimates"}):
            news = endpoint.startswith("news/")
            params = {"symbols" if news else "symbol": symbol}
            if endpoint in PAGED:
                params.update(page=0, limit=PAGE_SIZE)
                if news:
                    params["lookback_days"] = 7
            elif endpoint in LIMITED:
                params["limit"] = PAGE_SIZE
            requests.append(SourceRequest("fmp", "events" if news or endpoint == "grades" else "fundamentals",
                symbol, "stable", endpoint.replace("/", "-"), endpoint, params))
        requests.append(SourceRequest("schwab", "fundamentals", symbol, "marketdata-v1",
            "instrument-fundamental", "instruments", {"projection": "fundamental"}))
    return tuple(requests)


def handles(request):
    return (request.provider == "fmp" and request.endpoint in FMP_ENDPOINTS | {"sec-filings-search/symbol"}
        or request.provider == "schwab" and request.endpoint == "instruments"
        and request.params.get("projection") == "fundamental")


def _number(value, *, nonnegative=False):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and (not nonnegative or number >= 0) else None


def canonical_fmp_fields(raw, endpoint):
    """No cross-record joins, inferred sentiment or invented missing operands."""
    fields = {}
    if endpoint in {"discounted-cash-flow", "levered-discounted-cash-flow"}:
        value, price = _number(raw.get("dcf")), _number(raw.get("Stock Price", raw.get("stockPrice")))
        ratio = value / price - 1 if value is not None and price is not None and price > 0 else None
        fields.update(dcf_value=value, dcf_upside_fraction=_number(ratio), dcf_reference_price=price)
    if endpoint == "price-target-consensus":
        fields.update({"analyst_target_"+name: _number(raw.get(key), nonnegative=True)
            for name, key in (("consensus", "targetConsensus"), ("high", "targetHigh"),
                              ("low", "targetLow"), ("median", "targetMedian"))})
    if endpoint in {"grades-consensus", "grades-historical"}:
        prefix = "analystRatings" if endpoint == "grades-historical" else ""
        names = (("strong_buy", "StrongBuy"), ("buy", "Buy"), ("hold", "Hold"),
                 ("sell", "Sell"), ("strong_sell", "StrongSell"))
        for name, suffix in names:
            key = prefix+suffix if prefix else suffix[0].lower()+suffix[1:]
            value = _number(raw.get(key), nonnegative=True)
            fields["analyst_"+name] = value if value is not None and value.is_integer() else None
        counts = [fields["analyst_"+name] for name, _ in names]
        total = _number(sum(counts)) if all(value is not None for value in counts) else None
        fields["analyst_count"] = total
        fields["analyst_bullish_fraction"] = ((counts[0]+counts[1])/total if total is not None and total > 0 else None)
    if endpoint == "analyst-estimates":
        fields.update(analyst_eps_estimate=_number(raw.get("epsAvg")),
            analyst_revenue_estimate=_number(raw.get("revenueAvg"), nonnegative=True),
            analyst_estimate_period_end=raw.get("date"))
    if endpoint in {"news/stock", "news/press-releases"}:
        fields["event_type"] = "stock_news" if endpoint == "news/stock" else "press_release"
    return fields


def _current_fmp(request, start, end, max_records, fetch_page):
    symbol = _symbol(request.symbol)
    supplied = request.params.get("symbols", request.params.get("symbol", symbol))
    if supplied != symbol:
        raise ValueError("FMP extension query must match its single configured symbol")
    paged = request.endpoint in PAGED
    limit = min(PAGE_SIZE, max_records)
    maximum_pages = MAX_PAGES if paged else 1
    records, signatures, page_signatures, pages = [], set(), set(), []
    status, reason = "COMPLETE", ""
    for page in range(maximum_pages):
        params = dict(request.params)
        if request.endpoint.startswith("news/"):
            lookback = params.pop("lookback_days", 7)
            if type(lookback) is not int or not 1 <= lookback <= 31:
                raise ValueError("News requests require a bounded 1-31 day lookback")
            params.update({"from": (end.date()-timedelta(days=lookback)).isoformat(),
                           "to": end.date().isoformat()})
        if paged:
            params.update(page=page, limit=limit)
        elif request.endpoint in LIMITED:
            params["limit"] = limit
        try:
            result = fetch_page(replace(request, params=params), start, end, max_records)
        except Exception as exc:
            if not records:
                raise
            failure_status, failure_reason = classify_error(exc)
            pages.append({"page": page, "status": failure_status, "reason": failure_reason})
            status, reason = "PARTIAL", "A later page was unavailable; earlier received records are retained"
            break
        if not isinstance(result, CollectionResult):
            raise TypeError("FMP page adapter must return CollectionResult")
        count = len(result.records)
        pages.append({"page": page, "records": count, "status": result.status,
                      "request_params": params, "evidence": result.evidence})
        if result.status not in {"COMPLETE", "EMPTY"}:
            status, reason = ("PARTIAL", "A page did not establish complete delivery") if records else (result.status, result.reason)
        fingerprint = hashlib.sha256(canonical(result.records)).hexdigest()
        if count and fingerprint in page_signatures:
            status, reason = "PARTIAL", "Provider repeated a page; pagination progress is unverified"
            break
        page_signatures.add(fingerprint)
        for original in result.records:
            row = dict(original)
            if row.get("symbol") != symbol or not isinstance(row.get("raw"), dict):
                raise ValueError("FMP page record lacks exact symbol/raw provenance")
            row.update(canonical_fmp_fields(row["raw"], request.endpoint))
            if request.endpoint == "analyst-estimates":
                # Forecast horizon is not a realized financial statement period
                # and is never an information-publication timestamp.
                row.pop("period_end", None)
            signature = hashlib.sha256(canonical(row)).hexdigest()
            if signature not in signatures:
                signatures.add(signature)
                if len(records) == max_records:
                    status, reason = "PARTIAL", "Local record bound reached; remaining source records are unknown"
                    break
                records.append(row)
        if status != "COMPLETE":
            break
        if (paged or request.endpoint in LIMITED) and count >= limit:
            if len(records) >= max_records or page+1 == maximum_pages:
                status, reason = "PARTIAL", "Bounded current page slice reached; further source records are unknown"
                break
        else:
            break
    return CollectionResult(records, status, reason, evidence={"endpoint": request.endpoint,
        "mode": "bounded_current_symbol_research", "pages": pages,
        # Repeating a fully received fixed 100-row current slice cannot recover
        # its unrequested historical tail. Preserve PARTIAL coverage and refresh
        # on the next quarter; transport/page failures remain retryable.
        "bounded_response_complete": status == "COMPLETE" or (
            reason == "Bounded current page slice reached; further source records are unknown"
            and all(page.get("status") in {"COMPLETE", "EMPTY"} for page in pages)),
        "maximum_pages": maximum_pages, "page_limit": limit if paged or request.endpoint in LIMITED else None,
        "pagination_exhausted_within_request": status == "COMPLETE" and paged,
        "history_claim": "Current bounded response only; no claim to complete history or point-in-time reconstruction",
        "publication_rule": "Actual receipt plus explicit provider timestamp; fiscal/estimate dates never prove publication",
        "documentation": FMP_DOCUMENTATION})


def _market_get(session, endpoint, params, *, timeout=20):
    import requests
    from app.services.schwab import MARKETDATA_BASE_URL
    response = requests.get(f"{MARKETDATA_BASE_URL}/{endpoint}", headers=session._headers(),
        params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def fetch_full_schwab_chain(session, symbol, *, on_response=None):
    """Request every provider-returnable strike/expiry without local slice filters.

    Provider omissions/entitlements still apply. The caller retains the full
    response and marks any downstream record truncation as partial.
    """
    payload = _market_get(session, "chains", {"symbol": _symbol(symbol), "contractType": "ALL",
        "includeUnderlyingQuote": "true", "strategy": "SINGLE", "range": "ALL"})
    # Acquisition consumers can preserve a returned payload even when its
    # native schema/status subsequently fails validation. Never pass headers or
    # authenticated session preferences to this market-data-only callback.
    if on_response is not None:
        on_response(payload)
    if not isinstance(payload, dict):
        raise ValueError("Schwab chain response must be a mapping")
    if payload.get("status") != "SUCCESS":
        raise ValueError("Schwab chain did not report successful delivery")
    if payload.get("symbol") != _symbol(symbol):
        raise ValueError("Schwab chain returned another underlying")
    if any(not isinstance(payload.get(field), dict) for field in ("callExpDateMap", "putExpDateMap")):
        raise ValueError("Schwab chain requires explicit call and put maps")
    return payload


def _schwab_fundamentals(request, session, max_records):
    symbol = _symbol(request.symbol)
    payload = _market_get(session, "instruments", {"symbol": symbol, "projection": "fundamental"})
    if not isinstance(payload, dict) or not isinstance(payload.get("instruments"), list):
        raise ValueError("Schwab instrument fundamentals require an explicit instruments list")
    records = []
    for raw in payload["instruments"]:
        if not isinstance(raw, dict) or raw.get("symbol") != symbol or not isinstance(raw.get("fundamental"), dict):
            raise ValueError("Schwab instrument response differs from the requested symbol or schema")
        fundamental = raw["fundamental"]
        records.append({"symbol": symbol, "raw": raw, "published_at": None, "event_at": None,
            "publication_time_basis": "receipt_only", "period_end": None,
            "pe_ratio": _number(fundamental.get("peRatio")),
            # Keep provider values intact. The accessible primary schema does
            # not establish scaling for these fields; never silently mix a
            # provider's millions/percent values into dollar/ratio features.
            "market_cap": None, "debt_equity": None, "profit_margin": None,
            "provider_fundamental": fundamental})
    limited = len(records) > max_records
    return CollectionResult(records[:max_records], "PARTIAL" if limited else "COMPLETE",
        "Instrument response exceeded local record bound" if limited else "",
        {"mode": "current_instrument_fundamental", "projection": "fundamental",
         "unit_mapping": "PE ratio admitted; marketCap/debt/margin retained raw pending documented units",
         "publication_rule": "Actual receipt only; no historical filing/publication instant inferred"})


def collect_extended(request, start, end, max_records, *, schwab_session=None, fmp_fetch_page=None):
    if type(max_records) is not int or max_records < 1:
        raise ValueError("A positive record bound is required")
    if not handles(request):
        raise ValueError("Not an extended market-data request")
    if request.provider == "fmp":
        if fmp_fetch_page is None:
            raise ValueError("Existing FMP page normalizer is required")
        return _current_fmp(request, start, end, max_records, fmp_fetch_page)
    if schwab_session is None:
        raise ValueError("Existing authenticated Schwab market session is required")
    return _schwab_fundamentals(request, schwab_session, max_records)
