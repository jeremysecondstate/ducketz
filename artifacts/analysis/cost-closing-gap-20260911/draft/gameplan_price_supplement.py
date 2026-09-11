"""Pure, source-explicit supplemental observations for a small planning gap.

This module never acquires data, reads/writes files, changes source frames or
grants execution/model authority. Callers verify acquisition receipt files and
pass their hashes. XNAS remains the primary source; supplemental rows and their
provenance are returned separately, never relabeled or spliced into its history.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Mapping, Sequence

import pandas as pd

from ml.independent_stock_targets import STOCK_TARGET_BOUNDARY_TOLERANCE, STOCK_TIMEZONE
from ml.stock_target_prices import XNAS_STOCK_PRICE_SOURCE


SUPPLEMENT_VERSION = "schwab-observed-planning-minute-supplement-v1"
REFERENCE_VERSION = "observed-small-gap-planning-reference-v1"
SCHWAB_PROVIDER_ID = "schwab-price-history-v1"
SCHWAB_DATASET = "SCHWAB_PRICE_HISTORY"
DEFAULT_MAX_PRIMARY_CLOSE_GAP_MINUTES = 15
_MINUTE = pd.Timedelta(minutes=1)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _time(value: object, label: str) -> pd.Timestamp:
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be a timezone-aware timestamp") from exc
    _require(not pd.isna(timestamp) and timestamp.tzinfo is not None,
             f"{label} must be a timezone-aware timestamp")
    return timestamp.tz_convert("UTC")


def _minute_time(value: object, label: str) -> pd.Timestamp:
    timestamp = _time(value, label)
    _require(timestamp == timestamp.floor("min"), f"{label} must align to a one-minute boundary")
    return timestamp


def _symbol(value: object) -> str:
    _require(isinstance(value, str) and bool(value) and value == value.strip().upper()
             and value.replace(".", "").isalnum(), "Symbol must be an exact uppercase equity symbol")
    return value


def _session(value: str) -> tuple[str, pd.Timestamp, pd.Timestamp]:
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Session must be an exact YYYY-MM-DD date") from exc
    _require(day.isoformat() == value, "Session must be an exact YYYY-MM-DD date")
    local = pd.Timestamp(day).tz_localize(STOCK_TIMEZONE)
    return value, local.replace(hour=4).tz_convert("UTC"), local.replace(hour=17).tz_convert("UTC")


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256(value: object, label: str) -> str:
    _require(isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value),
             f"{label} must be a lowercase SHA256")
    return value


def _positive(value: object, label: str) -> float:
    try:
        _require(not isinstance(value, bool) and value is not None, f"{label} must be positive and finite")
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be positive and finite") from exc
    _require(number.is_finite() and number > 0, f"{label} must be positive and finite")
    result = float(number)
    _require(float("-inf") < result < float("inf"), f"{label} must be positive and finite")
    return result


def normalize_schwab_minute_supplement(
    raw_payload: bytes | str, *, symbol: str, session: str,
    request_start: object, request_end: object, fetched_at: object,
    acquisition_receipt_sha256: str, provider_id: str = SCHWAB_PROVIDER_ID,
    frequency_minutes: int = 1,
) -> dict:
    """Validate an exact saved Schwab price-history response without filling gaps.

    Raw bytes/string are hashed exactly as supplied. All returned candles must
    belong to the declared request and completed action session. Missing or
    zero volume is rejected rather than manufactured into a trade-bearing bar.
    Identical repeated candles retain their original row indexes; conflicting
    repeated timestamps are rejected. Provider-specific receipt integrity is a
    caller responsibility; its already-verified hash is retained verbatim.
    """
    symbol = _symbol(symbol)
    session, opening, boundary = _session(session)
    _require(provider_id == SCHWAB_PROVIDER_ID, "Unsupported supplemental provider identity")
    _require(type(frequency_minutes) is int and frequency_minutes == 1,
             "Supplemental observations must be native one-minute candles")
    start, end = _minute_time(request_start, "request_start"), _minute_time(request_end, "request_end")
    fetched = _time(fetched_at, "fetched_at")
    _require(opening <= start < end <= boundary, "Supplemental request interval must stay inside the exact action session")
    _require(end <= fetched, "Supplemental request must be complete at fetched_at")
    receipt_hash = _sha256(acquisition_receipt_sha256, "acquisition_receipt_sha256")
    _require(isinstance(raw_payload, (bytes, str)), "Pass exact saved raw JSON bytes or text")
    raw = raw_payload.encode("utf-8") if isinstance(raw_payload, str) else raw_payload
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Supplemental response must be valid raw JSON") from exc
    _require(isinstance(payload, dict) and payload.get("symbol") == symbol,
             "Supplemental response symbol differs from its request")
    candles = payload.get("candles")
    _require(isinstance(candles, list), "Supplemental response needs an explicit candles list")
    if "empty" in payload:
        _require(type(payload["empty"]) is bool and payload["empty"] == (not candles),
                 "Supplemental response empty flag contradicts its candles")
    by_time: dict[pd.Timestamp, dict] = {}
    for index, candle in enumerate(candles):
        _require(isinstance(candle, dict), "Supplemental candle must be an object")
        _require({"datetime", "open", "high", "low", "close", "volume"}.issubset(candle),
                 "Supplemental candle must contain actual timestamp and OHLCV")
        milliseconds = candle["datetime"]
        _require(type(milliseconds) is int and milliseconds >= 0,
                 "Schwab candle datetime must be integer epoch milliseconds")
        timestamp = pd.Timestamp(milliseconds, unit="ms", tz="UTC")
        _require(timestamp == timestamp.floor("min"), "Supplemental candle must align to a one-minute boundary")
        observed = timestamp + _MINUTE
        _require(start <= timestamp and observed <= end and observed <= boundary and observed <= fetched,
                 "Supplemental candle escapes request, session boundary or fetch completion")
        values = {field: _positive(candle[field], "Supplemental " + field)
                  for field in ("open", "high", "low", "close", "volume")}
        _require(values["low"] <= min(values["open"], values["close"])
                 <= max(values["open"], values["close"]) <= values["high"], "Supplemental OHLC ordering is invalid")
        row = {"symbol": symbol, "timestamp": timestamp.isoformat(), "observed_at": observed.isoformat(),
               **values, "provider_id": provider_id, "provider_dataset": SCHWAB_DATASET,
               "raw_row_indexes": [index], "raw_row_sha256": [_digest(candle)]}
        previous = by_time.get(timestamp)
        if previous is not None:
            _require(all(previous[field] == row[field] for field in values),
                     "Conflicting supplemental candles share a timestamp")
            previous["raw_row_indexes"].append(index)
            previous["raw_row_sha256"].append(_digest(candle))
        else:
            by_time[timestamp] = row
    result = {"schema_version": SUPPLEMENT_VERSION, "provider_id": provider_id,
              "provider_dataset": SCHWAB_DATASET, "symbol": symbol, "session": session,
              "request_start": start.isoformat(), "request_end": end.isoformat(),
              "fetched_at": fetched.isoformat(), "interval_seconds": 60,
              "raw_payload_sha256": hashlib.sha256(raw).hexdigest(), "raw_payload_bytes": len(raw),
              "acquisition_receipt_sha256": receipt_hash, "raw_candle_count": len(candles),
              "rows": [by_time[key] for key in sorted(by_time)],
              "source_policy": "actual_positive_volume_provider_candles_no_fill_no_native_replacement"}
    result["semantic_sha256"] = _digest(result)
    return result


def _verified_attempts(
    attempts: Sequence[Mapping], *, symbol: str, session: str, opening: pd.Timestamp,
    boundary: pd.Timestamp, native_close: pd.Timestamp, observed_at: pd.Timestamp,
    allow_live_unavailable: bool,
) -> list[dict]:
    _require(type(allow_live_unavailable) is bool, "allow_live_unavailable must be an explicit boolean")
    _require(len(attempts) == 2 and all(isinstance(item, Mapping) for item in attempts),
             "Exactly one Historical and one Live Databento attempt are required")
    by_mode = {item.get("delivery_mode"): item for item in attempts}
    _require(set(by_mode) == {"historical", "live"}, "Databento attempt delivery modes must be Historical and Live")
    verified = []
    for mode in ("historical", "live"):
        item = by_mode[mode]
        _require(item.get("provider") == "databento" and item.get("dataset") == "XNAS.ITCH"
                 and item.get("schema") == "ohlcv-1m" and item.get("symbol") == symbol
                 and item.get("session") == session, "Databento attempt source or symbol/session differs")
        _sha256(item.get("receipt_sha256"), "Databento receipt_sha256")
        start = _minute_time(item.get("request_start"), "Databento request_start")
        end = _minute_time(item.get("request_end"), "Databento request_end")
        completed = _time(item.get("completed_at"), "Databento completed_at")
        _require(opening <= start <= native_close < end == boundary and boundary <= completed <= observed_at,
                 "Databento exact interval must cover the complete native closing gap before observation")
        status = item.get("status")
        denied = mode == "live" and status == "LIVE_ACCESS_DENIED"
        if denied:
            _require(allow_live_unavailable, "Live access denial is not matching coverage without explicit authorization")
            _require(item.get("confirmed_gap") is False and not item.get("observed_bar_starts"),
                     "Denied Live access cannot claim an observed gap or coverage")
            coverage = []
        else:
            _require(status == "COMPLETE" and item.get("confirmed_gap") is True,
                     "Databento retries must successfully confirm the missing closing observations")
            raw_coverage = item.get("observed_bar_starts")
            _require(isinstance(raw_coverage, (list, tuple)), "Databento attempt needs explicit observed bar starts")
            coverage_times = [_minute_time(value, "Databento observed bar") for value in raw_coverage]
            _require(len(coverage_times) == len(set(coverage_times)), "Databento attempt coverage duplicates a minute")
            _require(all(start <= value and value + _MINUTE <= native_close for value in coverage_times),
                     "Databento retry contains a newer bar or an out-of-request observation; refresh primary evidence")
            coverage = [value.isoformat() for value in sorted(coverage_times)]
        verified.append({"delivery_mode": mode, "status": status, "request_start": start.isoformat(),
                         "request_end": end.isoformat(), "completed_at": completed.isoformat(),
                         "receipt_sha256": item["receipt_sha256"], "observed_bar_starts": coverage,
                         "coverage_basis": "ACCESS_UNAVAILABLE_EXPLICITLY_ALLOWED" if denied else "OBSERVED_CONFIRMED_GAP"})
    historical, live = verified
    _require(all(historical[key] == live[key] for key in ("request_start", "request_end")),
             "Historical and Live retry intervals must match exactly")
    if live["status"] == "COMPLETE":
        _require(historical["observed_bar_starts"] == live["observed_bar_starts"],
                 "Historical and Live retry coverage differs")
    return verified


def resolve_small_gap_reference(
    primary_prices: pd.DataFrame, supplement: Mapping, *, symbol: str, session: str,
    observed_at: object, databento_attempts: Sequence[Mapping],
    max_primary_close_gap_minutes: int = DEFAULT_MAX_PRIMARY_CLOSE_GAP_MINUTES,
    allow_live_unavailable: bool = False,
) -> dict:
    """Resolve a close reference while returning actual absent timestamps apart.

    Fifteen minutes bounds the *primary coverage gap*, not the accepted close's
    distance from 17:00. Both primary and fallback observations obey the unchanged
    five-minute endpoint tolerance. A sparse response remains sparse. If its real
    last candle still cannot supply the close, return UNAVAILABLE_REFERENCE_PRICE.
    No returned supplemental row replaces an existing primary timestamp.
    """
    symbol = _symbol(symbol)
    session, opening, boundary = _session(session)
    observed = _time(observed_at, "observed_at")
    _require(observed >= boundary, "Planning observation must follow the completed source-session boundary")
    _require(type(max_primary_close_gap_minutes) is int and max_primary_close_gap_minutes > 0,
             "Maximum primary close gap must be a positive integer number of minutes")
    source = primary_prices.attrs.get("stock_price_source", {})
    _require(source.get("source_contract") == XNAS_STOCK_PRICE_SOURCE and source.get("dataset") == "XNAS.ITCH",
             "Primary prices must retain the verified XNAS source identity")
    _require({"symbol", "timestamp", "open", "close"}.issubset(primary_prices), "Primary prices need symbol/timestamp/open/close")
    native = primary_prices.loc[primary_prices.symbol.eq(symbol)].copy(deep=True)
    if "provider_dataset" in native:
        _require(set(native.provider_dataset.dropna()) <= {"XNAS.ITCH"}, "Primary row provider identity differs from XNAS")
    native["timestamp"] = [_minute_time(value, "Primary timestamp") for value in native.timestamp]
    native = native.loc[native.timestamp.ge(opening) & native.timestamp.add(_MINUTE).le(boundary)
                        & native.timestamp.add(_MINUTE).le(observed)]
    native = native.drop_duplicates(["timestamp", "open", "close"]).sort_values("timestamp")
    _require(not native.empty and not native.timestamp.duplicated().any(), "Primary closing history is missing or has conflicting minute observations")
    for field in ("open", "close"):
        for value in native[field]:
            _positive(value, "Primary " + field)
    last = native.iloc[-1]
    native_close = last.timestamp + _MINUTE
    gap = boundary - native_close
    result = {"schema_version": REFERENCE_VERSION, "symbol": symbol, "session": session,
              "boundary": boundary.isoformat(), "observed_at": observed.isoformat(),
              "primary_source_contract": XNAS_STOCK_PRICE_SOURCE, "primary_dataset": "XNAS.ITCH",
              "native_close_observed_at": native_close.isoformat(), "native_close_gap_seconds": gap.total_seconds(),
              "maximum_primary_close_gap_seconds": max_primary_close_gap_minutes * 60,
              "boundary_tolerance_seconds": STOCK_TARGET_BOUNDARY_TOLERANCE.total_seconds(),
              "supplemented_rows": [], "primary_prices_modified": False}
    if gap <= STOCK_TARGET_BOUNDARY_TOLERANCE:
        return {**result, "status": "PRIMARY_AVAILABLE", "reference_price": float(last.close),
                "reference_bar_start": last.timestamp.isoformat(), "reference_observed_at": native_close.isoformat(),
                "reference_provider": "databento", "reference_provider_dataset": "XNAS.ITCH",
                "reference_source_contract": XNAS_STOCK_PRICE_SOURCE, "reference_row_provenance": None}
    _require(gap <= pd.Timedelta(minutes=max_primary_close_gap_minutes), "Primary close gap exceeds the configured small-gap limit")
    attempts = _verified_attempts(databento_attempts, symbol=symbol, session=session, opening=opening,
                                 boundary=boundary, native_close=native_close, observed_at=observed,
                                 allow_live_unavailable=allow_live_unavailable)
    _require(supplement.get("schema_version") == SUPPLEMENT_VERSION
             and supplement.get("provider_id") == SCHWAB_PROVIDER_ID and supplement.get("provider_dataset") == SCHWAB_DATASET
             and supplement.get("symbol") == symbol and supplement.get("session") == session
             and supplement.get("interval_seconds") == 60, "Supplemental source, symbol/session or minute contract differs")
    _require(supplement.get("semantic_sha256") == _digest({key: value for key, value in supplement.items() if key != "semantic_sha256"}),
             "Supplemental evidence semantic checksum differs")
    _sha256(supplement.get("raw_payload_sha256"), "Supplemental raw payload SHA256")
    _sha256(supplement.get("acquisition_receipt_sha256"), "Supplemental acquisition receipt SHA256")
    fetched = _time(supplement.get("fetched_at"), "Supplemental fetched_at")
    start = _minute_time(supplement.get("request_start"), "Supplemental request_start")
    end = _minute_time(supplement.get("request_end"), "Supplemental request_end")
    _require(opening <= start <= native_close < end <= boundary <= fetched <= observed,
             "Supplemental exact interval or acquisition time cannot cover the missing closing reference")
    native_times = set(native.timestamp)
    eligible = []
    for item in supplement["rows"]:
        timestamp = _minute_time(item["timestamp"], "Supplemental row timestamp")
        if timestamp not in native_times and native_close <= timestamp < boundary:
            eligible.append(json.loads(json.dumps(item)))
    result.update(databento_attempts=attempts, allow_live_unavailable=allow_live_unavailable,
                  supplemental_raw_payload_sha256=supplement["raw_payload_sha256"],
                  supplemental_acquisition_receipt_sha256=supplement["acquisition_receipt_sha256"],
                  supplemented_rows=eligible)
    candidates = [row for row in eligible if boundary - _time(row["observed_at"], "Supplemental close") <= STOCK_TARGET_BOUNDARY_TOLERANCE]
    if not candidates:
        return {**result, "status": "UNAVAILABLE_REFERENCE_PRICE", "reference_price": None,
                "reference_bar_start": None, "reference_observed_at": None,
                "reference_provider": None, "reference_provider_dataset": None, "reference_source_contract": None,
                "reference_row_provenance": None, "reason": "No actual supplemental close within the unchanged five-minute tolerance",
                "last_supplemental_close_observed_at": eligible[-1]["observed_at"] if eligible else None}
    chosen = max(candidates, key=lambda row: row["timestamp"])
    return {**result, "status": "SUPPLEMENTED", "reference_price": chosen["close"],
            "reference_bar_start": chosen["timestamp"], "reference_observed_at": chosen["observed_at"],
            "reference_provider": "schwab", "reference_provider_dataset": SCHWAB_DATASET,
            "reference_source_contract": SUPPLEMENT_VERSION,
            "reference_row_provenance": {"raw_payload_sha256": supplement["raw_payload_sha256"],
                                         "acquisition_receipt_sha256": supplement["acquisition_receipt_sha256"],
                                         "raw_row_indexes": chosen["raw_row_indexes"], "raw_row_sha256": chosen["raw_row_sha256"],
                                         "fetched_at": fetched.isoformat()}}
