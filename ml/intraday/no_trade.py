"""Source-scoped omission evidence; legacy prose never authorizes new fills.

Databento documents native OHLCV omission in
https://databento.com/docs/schemas-and-data-formats/ohlcv. This says nothing
about trades on venues outside the selected dataset. A completed, fully decoded
response and a second identical response are required in addition to that
schema convention. Missing symbol resolution, raw bytes or receipt stays unknown.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
import hashlib
import json
import math
import re

from .prediction import ContractError, instant

VERSION = "intraday-verified-source-omission-v1"
CONTRACT = "databento-equs-mini-native-ohlcv-1m-omission-v1"
DOCUMENTATION = "https://databento.com/docs/schemas-and-data-formats/ohlcv"


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def response_rows(response):
    """Canonical complete observed rows; never drop malformed rows for proof."""
    opened, closed = instant(_get(response, "interval_open")), instant(_get(response, "interval_close"))
    rows = []
    for row in _get(response, "rows", ()):
        if not isinstance(row, Mapping):
            raise ContractError("Native response rows must retain their original fields")
        at = instant(row.get("opened_at", row.get("timestamp")))
        values = {key: float(row[key]) for key in ("open", "high", "low", "close", "volume")}
        if (at.second or at.microsecond or at < opened or at + timedelta(minutes=1) > closed
                or not all(math.isfinite(v) for v in values.values()) or values["volume"] < 0
                or min(values[k] for k in ("open", "high", "low", "close")) <= 0
                or values["low"] > min(values["open"], values["close"])
                or values["high"] < max(values["open"], values["close"], values["low"])):
            raise ContractError("Invalid native response cannot establish source omission")
        rows.append({"opened_at": at.isoformat(), **values})
    rows.sort(key=lambda row: row["opened_at"])
    if len({row["opened_at"] for row in rows}) != len(rows):
        raise ContractError("Duplicate response minutes cannot establish source omission")
    return rows


def rows_hash(response):
    return hashlib.sha256(json.dumps(response_rows(response), sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _sha(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def make_omission_evidence(response, receipt):
    """Bind a reviewed adapter's persisted complete response to one attempt.

    The persistence adapter alone supplies the source contract after retaining
    and validating raw bytes. Empty unresolved symbol frames do not qualify.
    This function makes no network request or file mutation.
    """
    try:
        source = receipt["symbols"][_get(response, "symbol")]["source_response"]
        if (receipt.get("status") != "complete"
                or receipt["symbols"][_get(response, "symbol")].get("status") != "complete"
                or instant(receipt["start"]) != instant(_get(response, "interval_open"))
                or instant(receipt["end"]) != instant(_get(response, "interval_close"))
                or source.get("contract") != CONTRACT or source.get("complete") is not True):
            return None
        value = {"version": VERSION, "contract": CONTRACT, "documentation": DOCUMENTATION,
            "claim": "no_qualifying_trades_recorded_by_selected_source_in_omitted_minutes",
            "scope": "selected_dataset_only_not_market_wide", "complete_response": True,
            "symbol_resolution": source["symbol_resolution"],
            **{k: _get(response, k) for k in ("attempt_id", "symbol", "source_id", "source_dataset",
                "interval_open", "interval_close", "request_started_at", "request_ended_at")},
            "rows_sha256": rows_hash(response), "raw_sha256": source["raw_sha256"],
            "response_receipt_sha256": receipt["response_receipt_sha256"],
            "response_receipt": receipt["response_receipt"], "raw_file": source["raw_file"],
            "source_requested_at": source["requested_at"], "source_received_at": source["received_at"],
            "raw_rows": source["raw_rows"], "parsed_rows": source["parsed_rows"]}
        return value if verified_response(response, proof=value) else None
    except (KeyError, TypeError, ValueError, AttributeError, ContractError):
        return None


def verified_response(response, *, proof=None):
    """Check an exact completed native source response, not advertised range alone."""
    value = _get(response, "provider_no_trade_evidence") if proof is None else proof
    try:
        if (not isinstance(value, Mapping) or value.get("version") != VERSION
                or value.get("contract") != CONTRACT or value.get("documentation") != DOCUMENTATION
                or value.get("scope") != "selected_dataset_only_not_market_wide"
                or value.get("claim") != "no_qualifying_trades_recorded_by_selected_source_in_omitted_minutes"
                or value.get("complete_response") is not True
                or value.get("symbol_resolution") != "returned_native_raw_symbol"
                or not isinstance(value.get("response_receipt"), str)
                or value["response_receipt"].replace("\\", "/").rsplit("/", 1)[-1] != "response.json"
                or not isinstance(value.get("raw_file"), str)
                or not re.fullmatch(r"raw-\d{3,}\.parquet", value["raw_file"])
                or _get(response, "source_dataset") != "EQUS.MINI"
                or _get(response, "source_id") != "databento:EQUS.MINI:ohlcv-1m"
                or _get(response, "status") != "success"):
            return False
        if any(value.get(k) != _get(response, k) for k in ("attempt_id", "symbol", "source_id", "source_dataset",
                "interval_open", "interval_close", "request_started_at", "request_ended_at")):
            return False
        opened, closed = instant(value["interval_open"]), instant(value["interval_close"])
        if not (instant(_get(response, "coverage_open")) <= opened < closed <= instant(_get(response, "coverage_close"))
                and closed <= instant(value["request_started_at"]) <= instant(value["source_requested_at"])
                <= instant(value["source_received_at"]) <= instant(value["request_ended_at"])):
            return False
        rows = response_rows(response)
        # This adapter cannot prove that an entirely empty response resolved the
        # requested symbol. A trade-schema or explicit mapping receipt can be
        # added as a separately reviewed future contract, not inferred here.
        if not rows or any(type(value.get(k)) is not int or value[k] != len(rows) for k in ("raw_rows", "parsed_rows")):
            return False
        return (all(_sha(value.get(k)) for k in ("raw_sha256", "response_receipt_sha256", "rows_sha256"))
                and value["rows_sha256"] == rows_hash(response))
    except (KeyError, TypeError, ValueError, OverflowError, ContractError):
        return False


def stable_success_pair(responses):
    """Only the last consecutive complete successes can establish stability."""
    if len(responses) < 2:
        return None
    previous, final = responses[-2:]
    try:
        keys = ("symbol", "source_id", "source_dataset", "interval_open", "interval_close")
        if (any(_get(previous, k) != _get(final, k) for k in keys)
                or _get(previous, "attempt_id") == _get(final, "attempt_id")
                or not _get(previous, "attempt_id") or not _get(final, "attempt_id")):
            return None
        for response in (previous, final):
            retry = _get(response, "retry_after_seconds", 0)
            if (isinstance(retry, bool) or not isinstance(retry, (int, float)) or not math.isfinite(retry) or retry < 0
                    or _get(response, "status") != "success"
                    or instant(_get(response, "coverage_open")) > instant(_get(response, "interval_open"))
                    or instant(_get(response, "coverage_close")) < instant(_get(response, "interval_close"))
                    or instant(_get(response, "request_started_at")) < instant(_get(response, "interval_close"))
                    or instant(_get(response, "request_ended_at")) < instant(_get(response, "request_started_at"))):
                return None
        if ((instant(_get(final, "request_started_at")) - instant(_get(previous, "request_ended_at"))).total_seconds()
                < max(4.0, _get(previous, "retry_after_seconds", 0))
                or response_rows(previous) != response_rows(final)):
            return None
        return previous, final
    except (KeyError, TypeError, ValueError, OverflowError, ContractError):
        return None


def verified_omission_pair(responses, *, opened_at, duration=timedelta(minutes=1)):
    pair = stable_success_pair(responses)
    if pair is None or not all(verified_response(r) for r in pair):
        return False
    if (_get(pair[0], "provider_no_trade_evidence")["raw_sha256"]
            != _get(pair[1], "provider_no_trade_evidence")["raw_sha256"]):
        return False
    opened, closed = instant(opened_at), instant(opened_at) + duration
    if not (instant(_get(pair[-1], "interval_open")) <= opened < closed <= instant(_get(pair[-1], "interval_close"))):
        return False
    return not any(opened <= instant(row["opened_at"]) < closed for row in response_rows(pair[-1]))
