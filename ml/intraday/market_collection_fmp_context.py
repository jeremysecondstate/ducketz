"""Bounded FMP technical context and the repository's configured macro series.

Endpoint/parameter documentation: https://site.financialmodelingprep.com/developer/docs
Exchange-local clocks and calculation-window caveats:
https://site.financialmodelingprep.com/faqs?code=data
https://site.financialmodelingprep.com/faqs?code=chart

Provider indicators are separate receipt-timed features, never replacements for
native training prices. Macro period dates are not publication or receipt dates.
"""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import math
import re

import pandas as pd

from .market_collection import CollectionResult, SourceRequest, canonical, classify_error, instant

INDICATORS = ("sma", "ema", "wma", "dema", "tema", "rsi", "adx", "williams", "standarddeviation")
PERIOD_LENGTH = 10
TECHNICAL_LOOKBACK_DAYS = 14
MACRO_LOOKBACK_DAYS = 180
MACRO_SLICE_DAYS = 90
MACRO_NAMES = ("GDP", "CPI", "unemploymentRate", "federalFunds")
DOCUMENTATION = "https://site.financialmodelingprep.com/developer/docs"


def _symbol(value):
    value = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", value):
        raise ValueError("An exact configured US equity symbol is required")
    return value


def context_requests(symbols):
    """Build only existing local equities and existing macro-series definitions."""
    from app.services.fmp_macro_context import FmpMacroContextProvider
    symbols = tuple(_symbol(value) for value in symbols)
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError("Explicit unique local symbol scope is required")
    requests = [SourceRequest("fmp", "technical", symbol, "stable",
        f"technical-{name}-15min-p{PERIOD_LENGTH}", "technical-indicators/"+name,
        {"symbol": symbol, "timeframe": "15min", "periodLength": PERIOD_LENGTH,
         "lookback_days": TECHNICAL_LOOKBACK_DAYS, "timestamp_zone": "America/New_York",
         "completed_bars_only": True}) for symbol in symbols for name in INDICATORS]
    for spec in FmpMacroContextProvider().economic_indicator_specs():
        if spec.endpoint != "economic-indicators" or spec.params.get("name") not in MACRO_NAMES:
            raise ValueError("Unreviewed configured macro source requires explicit adapter support")
        requests.append(SourceRequest("fmp", "macro", "*", "stable", "economic-"+spec.output_symbol,
            spec.endpoint, {"name": spec.params["name"], "lookback_days": MACRO_LOOKBACK_DAYS,
                            "slice_days": MACRO_SLICE_DAYS}))
    return tuple(requests)


def handles(request):
    return request.provider == "fmp" and (
        request.source_kind == "technical" and request.endpoint in {"technical-indicators/"+n for n in INDICATORS}
        or request.source_kind == "macro" and request.endpoint == "economic-indicators")


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _rows(payload):
    rows = payload if isinstance(payload, list) else payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("FMP context response requires an explicit list of records")
    return rows


def _stamp(value):
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", value):
        raise ValueError("Technical bar requires a provider date string")
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("Technical bar timestamp is missing")
    # This source is scoped to the existing US-listed equities. FMP documents
    # exchange-region wall clocks; DST uses the NY exchange, not fixed UTC-5.
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("America/New_York", ambiguous="raise", nonexistent="raise")
    stamp = stamp.tz_convert("UTC")
    if stamp != stamp.floor("15min"):
        raise ValueError("Technical bar is not aligned to its requested 15-minute timeframe")
    return stamp.to_pydatetime()


def _aware_publication(raw):
    value = raw.get("publishedDate") or raw.get("published_at") or raw.get("releaseDate")
    if value is None or isinstance(value, (int, float, bool)):
        return None
    try:
        stamp = pd.Timestamp(value)
        return stamp.tz_convert("UTC").isoformat() if not pd.isna(stamp) and stamp.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def _deduplicate(records, key):
    unique, seen, values, duplicates, conflicts = [], set(), {}, 0, 0
    for row in records:
        digest = hashlib.sha256(canonical(row)).hexdigest()
        if digest in seen:
            duplicates += 1
            continue
        identity = key(row)
        if identity in values and values[identity] != digest:
            conflicts += 1
        seen.add(digest)
        values[identity] = digest
        unique.append(row)
    return unique, duplicates, conflicts


def _technical(request, start, end, max_records, provider):
    symbol, name = _symbol(request.symbol), request.endpoint.rsplit("/", 1)[1]
    expected = {"symbol": symbol, "timeframe": "15min", "periodLength": PERIOD_LENGTH,
        "lookback_days": TECHNICAL_LOOKBACK_DAYS, "timestamp_zone": "America/New_York", "completed_bars_only": True}
    if dict(request.params) != expected:
        raise ValueError("Technical context differs from its reviewed period, timeframe, symbol or clock contract")
    local_day = pd.Timestamp(end).tz_convert("America/New_York").date()
    first = local_day - timedelta(days=TECHNICAL_LOOKBACK_DAYS)
    params = {"symbol": symbol, "timeframe": "15min", "periodLength": PERIOD_LENGTH,
              "from": first.isoformat(), "to": local_day.isoformat()}
    payload = provider._get_json(request.endpoint, params)
    raw_rows = _rows(payload)
    records, excluded = [], []
    invalid_values = 0
    for raw in raw_rows:
        if raw.get("symbol", symbol) != symbol:
            raise ValueError("Technical context returned another symbol")
        stamp = _stamp(raw.get("date"))
        finish = stamp + timedelta(minutes=15)
        if not first <= pd.Timestamp(stamp).tz_convert("America/New_York").date() <= local_day:
            raise ValueError("Technical response exceeded requested date bounds")
        if finish > end:
            excluded.append(raw)
            continue
        value = _number(raw.get(name, raw.get("standardDeviation") if name == "standarddeviation" else None))
        if value is not None and (name in {"rsi", "adx"} and not 0 <= value <= 100
                or name == "williams" and not -100 <= value <= 0
                or name == "standarddeviation" and value < 0):
            value = None
        invalid_values += int(value is None)
        records.append({"symbol": symbol, "raw": raw, "event_at": stamp.isoformat(),
            "bar_end_at": finish.isoformat(), "available_at": finish.isoformat(),
            "technical_name": "standard_deviation" if name == "standarddeviation" else name,
            "technical_value": value, "period_length": PERIOD_LENGTH, "timeframe": "15min",
            "technical_reference_close": _number(raw.get("close")),
            "timestamp_basis": "provider bar opening; exchange-region America/New_York for naive US equity clock",
            "population_basis": "FMP provider-derived indicator; adjustment/session/input population not native EQUS.MINI"})
    records, duplicates, conflicts = _deduplicate(records, lambda r: r["event_at"])
    records.sort(key=lambda r: r["event_at"], reverse=True)
    capped = len(records) > max_records
    return CollectionResult(records[:max_records], "PARTIAL" if capped or conflicts else "COMPLETE",
        "Record bound or conflicting same-bar provider values" if capped or conflicts else "",
        {"mode": "bounded_provider_technical_context", "request_params": params, "raw_record_count": len(raw_rows),
         "response_sha256": hashlib.sha256(canonical(payload)).hexdigest(), "duplicates_removed": duplicates,
         "conflicting_bar_records": conflicts, "missing_or_invalid_values": invalid_values,
         "excluded_unfinished_records": len(excluded), "excluded_unfinished_raw": excluded[:max_records],
         "excluded_raw_truncated": len(excluded) > max_records,
         "calculation_window": "Requested date window influences provider warmup; historical availability is never inferred",
         "coverage_claim": "Received provider indicator rows only; no complete-market or native-population claim",
         "documentation": DOCUMENTATION})


def _macro(request, start, end, max_records, provider):
    from app.services.fmp_macro_context import fmp_macro_series_definition
    name = request.params.get("name")
    expected = {"name": name, "lookback_days": MACRO_LOOKBACK_DAYS, "slice_days": MACRO_SLICE_DAYS}
    if request.symbol != "*" or name not in MACRO_NAMES or dict(request.params) != expected:
        raise ValueError("Macro context differs from the repository's configured bounded series")
    definition = fmp_macro_series_definition(name)
    last_day = end.date()
    records, slices = [], []
    partial = False
    for offset in (0, MACRO_SLICE_DAYS):
        last = last_day-timedelta(days=offset)
        first = last-timedelta(days=MACRO_SLICE_DAYS-1)
        params = {"name": name, "from": first.isoformat(), "to": last.isoformat()}
        try:
            payload = provider._get_json(request.endpoint, params)
            rows = _rows(payload)
        except Exception as exc:
            if not records:
                raise
            status, reason = classify_error(exc)
            slices.append({"request_params": params, "status": status, "reason": reason})
            partial = True
            break
        slices.append({"request_params": params, "status": "COMPLETE", "raw_record_count": len(rows),
                       "response_sha256": hashlib.sha256(canonical(payload)).hexdigest()})
        for raw in rows:
            if raw.get("name") != name:
                raise ValueError("Macro response differs from the requested named series")
            period = date.fromisoformat(str(raw.get("date")))
            if not first <= period <= last:
                raise ValueError("Macro response exceeds its exact requested date slice")
            published = _aware_publication(raw)
            records.append({"symbol": "*", "provider_symbol": name, "raw": raw, "macro_series": name,
                "macro_value": _number(raw.get("value")), "macro_period_date": period.isoformat(),
                "macro_unit": definition.unit, "macro_cadence": definition.cadence,
                "event_at": published, "published_at": published,
                "publication_time_basis": "provider_timestamp" if published else "receipt_only",
                "revision_basis": "current provider series; no historical vintage reconstruction"})
    records, duplicates, conflicts = _deduplicate(records, lambda r: (r["macro_series"], r["macro_period_date"]))
    records.sort(key=lambda r: r["macro_period_date"], reverse=True)
    partial = partial or bool(conflicts) or len(records) > max_records
    return CollectionResult(records[:max_records], "PARTIAL" if partial else "COMPLETE",
        "Bounded series response is incomplete or contains conflicting period values" if partial else "",
        {"mode": "current_configured_macro_series", "slices": slices, "maximum_slices": 2,
         "duplicates_removed": duplicates, "conflicting_period_records": conflicts,
         "coverage_claim": "Two requested current-series date slices; no complete history or publication vintage claim",
         "period_date_rule": "Economic period date never proves publication or prior availability",
         "documentation": DOCUMENTATION})


def collect_context(request, start, end, max_records, *, fmp_provider):
    if not handles(request) or type(max_records) is not int or max_records < 1:
        raise ValueError("A supported context request and positive record bound are required")
    start, end = instant(start), instant(end)
    if end-start != timedelta(minutes=15) or start.minute % 15 or start.second or start.microsecond:
        raise ValueError("Context collection requires one exact quarter")
    if request.source_kind == "technical":
        return _technical(request, start, end, max_records, fmp_provider)
    return _macro(request, start, end, max_records, fmp_provider)


def context_catalog():
    return {"implemented": {"technical": list(INDICATORS), "macro": list(MACRO_NAMES)},
        "technical_parameters": {"timeframe": "15min", "periodLength": PERIOD_LENGTH,
                                 "lookback_days": TECHNICAL_LOOKBACK_DAYS},
        "already_collected": ["existing configured commodity quotes"],
        "not_configured": ["treasury-rates", "economic-calendar", "market-risk-premium"],
        "not_listed_in_verified_stable_indicator_catalog": ["MACD", "ATR", "Bollinger Bands"],
        "redundancy": "Provider SMA/EMA/RSI etc may overlap local price features; they retain independent source selectors and are not replacements",
        "entitlement": "UNPROBED: official catalog existence and plan text do not establish account access"}
