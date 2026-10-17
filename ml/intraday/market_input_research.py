"""Bounded FMP research context; every value is available at actual receipt only.

Pure request/extraction helpers. The collector owns authentication, fair request
scheduling, immutable raw persistence BEFORE extraction, and as-of joins. These
features never substitute for observed price bars or historical release vintages.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import math
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

VERSION = "intraday-fmp-research-receipts-v1"
UTC = timezone.utc
NY = ZoneInfo("America/New_York")
INDICATORS = ("sma", "ema", "wma", "dema", "tema", "rsi", "adx", "williams", "standarddeviation")
MACROS = {"gdp": "GDP", "cpi": "CPI", "unemployment": "unemploymentRate", "federal_funds": "federalFunds"}
MARKERS = ("synthetic", "is_synthetic", "no_trade", "is_no_trade", "source_empty", "is_source_empty")
MAX_RESPONSE_ROWS = 5000
PAGE_LIMIT = 100
SOURCE_FIELDS = {
    "fmp_estimates_annual": ("eps", "revenue", "analysts"),
    "fmp_estimates_quarter": ("eps", "revenue", "analysts"),
    "fmp_rating_snapshot": ("overall_score",),
    "fmp_rating_history": ("overall_score",),
    "fmp_target_summary": ("month_target", "month_count"),
    "fmp_target_consensus": ("consensus", "high", "low"),
    "fmp_grade_changes": ("items_7d", "upgrades_7d", "downgrades_7d"),
    "fmp_grade_history": ("analyst_count", "bullish_fraction"),
    "fmp_grade_consensus": ("analyst_count", "bullish_fraction"),
    "fmp_dcf": ("value", "upside_fraction"),
    "fmp_dcf_levered": ("value", "upside_fraction"),
    "fmp_research_news": ("items_7d", "latest_age_hours"),
    "fmp_press_releases": ("items_7d", "latest_age_hours"),
    "fmp_sec_filings": ("items_30d", "latest_age_hours"),
    **{f"fmp_technical_{name}": ("value",) for name in INDICATORS},
    **{f"fmp_macro_{name}": ("value",) for name in MACROS},
}
SOURCE_TTL_SECONDS = {source: 172800 for source in SOURCE_FIELDS}
SOURCE_REFRESH_SECONDS = {source: 21600 for source in SOURCE_FIELDS}
for _source in SOURCE_FIELDS:
    if _source.startswith("fmp_technical_"):
        SOURCE_TTL_SECONDS[_source] = 1800
        SOURCE_REFRESH_SECONDS[_source] = 900
    elif _source in {"fmp_research_news", "fmp_press_releases", "fmp_grade_changes", "fmp_sec_filings"}:
        SOURCE_TTL_SECONDS[_source] = 7200
        SOURCE_REFRESH_SECONDS[_source] = 900


def _instant(value: Any) -> datetime:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("An explicit aware receipt timestamp is required")
    return stamp.tz_convert("UTC").to_pydatetime()


def _symbol(value: str) -> str:
    value = str(value).strip().upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", value):
        raise ValueError("An exact configured equity symbol is required")
    return value


def research_specs(symbol: str, now: datetime, *, include_global: bool = False) -> tuple[tuple[str, str, dict], ...]:
    """One bounded request per job; one page only, never exhaustive history.

    Global macro jobs must be scheduled once, then their exact receipt fanned
    out to equity consumers. A 90-day slice avoids an older slice replacing a
    newer macro value. Fiscal periods are never treated as publication clocks.
    """
    symbol = _symbol(symbol)
    today = _instant(now).astimezone(NY).date()
    base = {"symbol": symbol}
    jobs = [
        (f"fmp_estimates_{period}", "analyst-estimates", base | {"period": period, "page": 0, "limit": PAGE_LIMIT})
        for period in ("annual", "quarter")
    ]
    for source, endpoint in (
        ("fmp_rating_snapshot", "ratings-snapshot"), ("fmp_rating_history", "ratings-historical"),
        ("fmp_target_summary", "price-target-summary"), ("fmp_target_consensus", "price-target-consensus"),
        ("fmp_grade_changes", "grades"), ("fmp_grade_history", "grades-historical"),
        ("fmp_grade_consensus", "grades-consensus"), ("fmp_dcf", "discounted-cash-flow"),
        ("fmp_dcf_levered", "levered-discounted-cash-flow"),
    ):
        params = base | ({"limit": PAGE_LIMIT} if source in {"fmp_rating_history", "fmp_grade_history"} else {})
        jobs.append((source, endpoint, params))
    for source, endpoint in (("fmp_research_news", "news/stock"), ("fmp_press_releases", "news/press-releases")):
        jobs.append((source, endpoint, {"symbols": symbol, "page": 0, "limit": PAGE_LIMIT,
                    "from": (today-timedelta(days=7)).isoformat(), "to": today.isoformat()}))
    jobs.append(("fmp_sec_filings", "sec-filings-search/symbol", base | {"page": 0, "limit": PAGE_LIMIT,
                 "from": (today-timedelta(days=30)).isoformat(), "to": today.isoformat()}))
    jobs.extend((f"fmp_technical_{name}", f"technical-indicators/{name}", base | {
        "timeframe": "15min", "periodLength": 10, "from": (today-timedelta(days=14)).isoformat(), "to": today.isoformat()})
        for name in INDICATORS)
    if include_global:
        jobs.extend((f"fmp_macro_{key}", "economic-indicators", {"name": name,
            "from": (today-timedelta(days=89)).isoformat(), "to": today.isoformat()}) for key, name in MACROS.items())
    return tuple(jobs)


def _native(row: Mapping) -> bool:
    return all(key not in row or row[key] is False for key in MARKERS)


def empty_macro_fallback_spec(source: str, params: Mapping, payload: Any) -> tuple[str, str, dict] | None:
    """One preceding GDP slice after an explicitly empty current response.

    The caller permits at most one fallback, links its first receipt, retains
    both raw responses, and uses the second actual receipt as availability.
    Never call recursively for an empty fallback response. Other macro series,
    errors and filtered/marked populations do not authorize this fallback.
    """
    if source != "fmp_macro_gdp" or not isinstance(params, Mapping) or set(params) != {"name", "from", "to"} or params.get("name") != "GDP":
        return None
    if isinstance(payload, Mapping):
        if not _native(payload) or any(str(key).lower() in {"error", "error message", "error_message"} for key in payload):
            return None
        payload = payload.get("data")
    if not isinstance(payload, list) or payload:
        return None
    try:
        first, last = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
    except (ValueError, TypeError):
        return None
    if (last-first).days != 89:
        return None
    try:
        preceding_first, preceding_last = first-timedelta(days=90), first-timedelta(days=1)
    except OverflowError:
        return None
    return source, "economic-indicators", {"name": "GDP", "from": preceding_first.isoformat(), "to": preceding_last.isoformat()}


def _number(value: Any, *, minimum: float | None = None, maximum: float | None = None, integer: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or (minimum is not None and value < minimum) or (maximum is not None and value > maximum) or (integer and not value.is_integer()):
        return None
    return value


def safe_error(exc: Exception) -> dict:
    """Do not persist exception text, request URLs, tokens, or provider messages."""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if not isinstance(code, int):
        match = re.search(r"\bHTTP (\d{3})\b", str(exc))
        code = int(match.group(1)) if match else None
    return {"error_type": type(exc).__name__, "http_status": code,
            "category": "entitlement_or_permission" if code in {402, 403} else "authentication" if code == 401 else "request_unavailable"}


def _provider_time(value: Any, *, full: bool = False) -> datetime | None:
    if not isinstance(value, str) or (full and not re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}", value)):
        return None
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp):
            return None
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("America/New_York", ambiguous="NaT", nonexistent="NaT")
        if pd.isna(stamp):
            return None
        return stamp.tz_convert("UTC").to_pydatetime()
    except (TypeError, ValueError, OverflowError):
        return None


def _publication(row: Mapping) -> tuple[datetime | None, bool]:
    # Economic/fiscal/estimate dates do not appear in this list.
    for key in ("publishedDate", "published_at", "releaseDate", "acceptedDate", "accepted_at"):
        if row.get(key) is not None:
            return _provider_time(row[key], full=True), True
    return None, False


def _clean_rows(payload: Any, source: str, receipt: datetime, symbol: str | None, params: Mapping) -> tuple[list[dict], dict]:
    evidence = {"raw_rows": 0, "marker_rejected": 0, "future_rejected": 0, "invalid_rejected": 0,
                "duplicate_rows": 0, "clock_basis": "actual_receipt_only", "coverage": "bounded_current_response_not_full_history"}
    if isinstance(payload, dict):
        if not _native(payload):
            evidence["marker_rejected"] = 1
            return [], evidence
        if any(str(key).lower() in {"error", "error message", "error_message"} for key in payload):
            evidence["provider_error_payload"] = True
            return [], evidence
        raw = payload.get("data", payload.get("historical", [payload]))
    else:
        raw = payload
    if not isinstance(raw, list) or any(not isinstance(row, dict) for row in raw):
        evidence["invalid_payload"] = True
        return [], evidence
    evidence["raw_rows"] = len(raw)
    if len(raw) > MAX_RESPONSE_ROWS:
        evidence["response_bound_exceeded"] = True
        return [], evidence
    rows, seen = [], set()
    for row in raw:
        if not _native(row):
            evidence["marker_rejected"] += 1
            continue
        if symbol and row.get("symbol", symbol) != symbol:
            evidence["invalid_rejected"] += 1
            continue
        publication, supplied = _publication(row)
        if supplied and publication is None:
            evidence["invalid_rejected"] += 1
            continue
        if publication and publication > receipt:
            evidence["future_rejected"] += 1
            continue
        # Forecast fiscal periods can be future; current snapshots/observations
        # cannot. This check does not promote a date to an availability time.
        if row.get("date") and not source.startswith("fmp_estimates_"):
            date = _provider_time(row["date"])
            if date is None:
                evidence["invalid_rejected"] += 1
                continue
            if date > receipt:
                evidence["future_rejected"] += 1
                continue
        signature = json.dumps(row, sort_keys=True, default=str, separators=(",", ":"))
        if signature in seen:
            evidence["duplicate_rows"] += 1
            continue
        seen.add(signature)
        rows.append(row)
    evidence["retained_rows"] = len(rows)
    evidence["page_bound_reached"] = len(raw) >= int(params.get("limit", PAGE_LIMIT)) if "limit" in params else False
    return rows, evidence


def _latest(rows: list[dict], keys=("date",)) -> tuple[dict | None, bool]:
    dated = [(next((str(row[key]) for key in keys if row.get(key)), ""), row) for row in rows]
    if not dated:
        return None, False
    latest = max(stamp for stamp, _ in dated)
    tied = [row for stamp, row in dated if stamp == latest]
    return (tied[0], False) if len(tied) == 1 else (None, True)


def extract_context(source: str, payload: Any, received_at: datetime, *, symbol: str | None = None,
                    request_params: Mapping | None = None) -> dict:
    """Derive reviewed scalars after the caller durably retains the raw payload.

    Returns features/status/event_at/evidence. ``event_at`` describes the source
    observation only; availability is ALWAYS the actual received_at. Missing
    features remain absent, never synthetic zeros. Genuine observed counts may
    be zero when at least one eligible record establishes a response population.
    """
    if source not in SOURCE_FIELDS:
        raise ValueError("Unsupported research source")
    receipt = _instant(received_at)
    symbol = _symbol(symbol) if symbol and symbol != "*" else None
    params = dict(request_params or {})
    rows, evidence = _clean_rows(payload, source, receipt, symbol, params)
    evidence.update(version=VERSION, available_at=receipt.isoformat(), raw_retention_owner="collector")
    features, event = {}, None
    if source.startswith("fmp_technical_"):
        name = source.removeprefix("fmp_technical_")
        valid = []
        for row in rows:
            stamp = _provider_time(row.get("date"), full=True)
            if stamp is None or stamp.minute % 15 or stamp.second or stamp.microsecond:
                evidence["invalid_rejected"] += 1
                continue
            local_day = stamp.astimezone(NY).date().isoformat()
            if (params.get("from") and local_day < params["from"]) or (params.get("to") and local_day > params["to"]):
                evidence["invalid_rejected"] += 1
                continue
            end = stamp+timedelta(minutes=15)
            if end > receipt:
                evidence["future_rejected"] += 1
                continue
            valid.append((end, row))
        if valid:
            event = max(stamp for stamp, _ in valid)
            tied = [row for stamp, row in valid if stamp == event]
            if len(tied) > 1:
                evidence["conflicting_latest_records"] = len(tied)
            elif receipt-event > timedelta(seconds=SOURCE_TTL_SECONDS[source]):
                evidence["stale_event"] = True
            else:
                low, high = ((0, 100) if name in {"rsi", "adx"} else (-100, 0) if name == "williams" else (0, None) if name == "standarddeviation" else (math.nextafter(0, 1), None))
                value = tied[0].get(name, tied[0].get("standardDeviation") if name == "standarddeviation" else None)
                features["value"] = _number(value, minimum=low, maximum=high)
        evidence["clock_basis"] = "15min_bar_end_NY_exchange_clock_available_only_at_actual_receipt"
        evidence["population"] = "provider_indicator_adjustment_session_warmup_not_native_price_population"
    elif source.startswith("fmp_macro_"):
        name = MACROS[source.removeprefix("fmp_macro_")]
        valid = [row for row in rows if row.get("name") == name and row.get("date") and
                 (not params.get("from") or str(row["date"]) >= params["from"]) and
                 (not params.get("to") or str(row["date"]) <= params["to"])]
        row, conflict = _latest(valid)
        if conflict:
            evidence["conflicting_latest_records"] = True
        if row:
            features["value"] = _number(row.get("value"), minimum=0, maximum=100 if name == "unemploymentRate" else None)
            event, _ = _publication(row)
            evidence["period_date"] = row["date"]
        evidence["clock_basis"] = "receipt_only_current_series_no_historical_vintage"
    elif source in {"fmp_research_news", "fmp_press_releases", "fmp_sec_filings", "fmp_grade_changes"}:
        days = 30 if source == "fmp_sec_filings" else 7
        eligible = []
        for row in rows:
            stamp, _ = _publication(row)
            if stamp is None and source == "fmp_grade_changes":
                stamp = _provider_time(row.get("date"))
            if stamp and receipt-timedelta(days=days) <= stamp <= receipt:
                eligible.append((stamp, row))
        if eligible:
            event = max(stamp for stamp, _ in eligible)
            features = {f"items_{days}d": float(len(eligible))}
            if source == "fmp_grade_changes":
                features.update(upgrades_7d=float(sum(str(row.get("action", "")).lower() == "upgrade" for _, row in eligible)),
                                downgrades_7d=float(sum(str(row.get("action", "")).lower() == "downgrade" for _, row in eligible)))
            else:
                features["latest_age_hours"] = (receipt-event).total_seconds()/3600
        evidence["event_window_days"] = days
        evidence["count_scope"] = "eligible_unique_records_in_received_bounded_page"
    else:
        if source.startswith("fmp_estimates_"):
            # Select the nearest future fiscal period, not a historical actual.
            future = [row for row in rows if _provider_time(row.get("date")) and str(row["date"])[:10] >= receipt.astimezone(NY).date().isoformat()]
            first = min((str(row["date"]) for row in future), default=None)
            selected = [row for row in future if str(row["date"]) == first]
            row, conflict = (selected[0], False) if len(selected) == 1 else (None, len(selected) > 1)
        else:
            row, conflict = _latest(rows)
        if conflict:
            evidence["conflicting_latest_records"] = True
        if row:
            event, _ = _publication(row)
            if source.startswith("fmp_estimates_"):
                features = {"eps": _number(row.get("epsAvg")), "revenue": _number(row.get("revenueAvg"), minimum=0),
                            "analysts": _number(row.get("numAnalystsEps"), minimum=0, integer=True)}
                evidence["estimate_period_end"] = row.get("date")
            elif source in {"fmp_rating_snapshot", "fmp_rating_history"}:
                features = {"overall_score": _number(row.get("overallScore"), minimum=0, maximum=5)}
            elif source == "fmp_target_summary":
                features = {"month_target": _number(row.get("lastMonthAvgPriceTarget"), minimum=0),
                            "month_count": _number(row.get("lastMonthCount"), minimum=0, integer=True)}
            elif source == "fmp_target_consensus":
                features = {key: _number(row.get(field), minimum=0) for key, field in (("consensus", "targetConsensus"), ("high", "targetHigh"), ("low", "targetLow"))}
                if features["high"] is not None and features["low"] is not None and features["high"] < features["low"]:
                    features = {}
            elif source in {"fmp_grade_history", "fmp_grade_consensus"}:
                names = ("StrongBuy", "Buy", "Hold", "Sell", "StrongSell")
                counts = [_number(row.get("analystRatings"+name if source == "fmp_grade_history" else name[0].lower()+name[1:]), minimum=0, integer=True) for name in names]
                if all(value is not None for value in counts):
                    total = sum(counts)
                    features = {"analyst_count": total, "bullish_fraction": (counts[0]+counts[1])/total if total > 0 else None}
            elif source in {"fmp_dcf", "fmp_dcf_levered"}:
                value, price = _number(row.get("dcf")), _number(row.get("Stock Price", row.get("stockPrice")), minimum=0)
                features = {"value": value, "upside_fraction": _number(value/price-1) if value is not None and price and price > 0 else None}
    features = {key: value for key, value in features.items() if key in SOURCE_FIELDS[source] and _number(value) is not None}
    status = "ok" if features else "stale" if evidence.get("stale_event") else "unavailable" if any(evidence.get(key) for key in ("provider_error_payload", "invalid_payload", "response_bound_exceeded", "conflicting_latest_records")) else "empty"
    return {"features": features, "status": status, "event_at": event.isoformat() if event else None, "evidence": evidence}


def extract_features(source: str, payload: Any, received_at: datetime, *, symbol: str | None = None,
                     request_params: Mapping | None = None) -> dict:
    return extract_context(source, payload, received_at, symbol=symbol, request_params=request_params)["features"]
