"""Finite, fail-closed XNAS intraday replay capture and native evidence checks.

The Live gateway decides whether an explicit start is still retained. Its
nominal 24-hour window is not a guarantee of session coverage. A request is
never narrowed to fit retention. Raw capture includes replay through connection
time; only bars inside the exact requested [start, end) interval count as data.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd


DATASET = "XNAS.ITCH"
MODE = "live-intraday-replay"
DELIVERY_VERSION = "databento-xnas-live-replay-v1"
SUPPORTED_SCHEMAS = frozenset(("ohlcv-1m",))
MAX_LOOKBACK_HOURS = 24
MAX_TIMEOUT_SECONDS = 900
_RTYPES = {"ohlcv-1m": 33}
_CODES = {"subscription_ack": 1, "replay_completed": 3, "heartbeat": 0,
          "slow_reader_warning": 2, "end_of_interval": 4}
_ERROR_PHRASES = (
    ("LIVE_ACCESS_DENIED", (
        r"\bnot (?:authorized|authorised|entitled|permitted)\b",
        r"\b(?:access|permission) denied\b",
        r"\b(?:authentication|authorization|authorisation) (?:failed|failure|rejected|denied|required)\b",
        r"\bfailed to (?:authenticate|authorize|authorise)\b",
        r"\b(?:invalid|incorrect|rejected|expired) (?:api[ _-]?key|credentials?|token)\b",
        r"\b(?:insufficient|missing|required) (?:permissions?|entitlements?|licen[cs]e)\b",
        r"\b(?:licen[cs]e|entitlement) (?:is )?(?:required|missing|expired|denied|not found)\b",
        r"\b(?:does not|do not|doesn't|don't) have (?:a |the )?(?:required )?(?:licen[cs]e|entitlement)\b",
    )),
    ("REPLAY_RANGE_UNAVAILABLE", (
        r"\b(?:start(?: time| timestamp)?|replay(?: start)?) (?:is )?(?:before|older than|too old|outside)\b",
        r"\bstart(?: time| timestamp)? (?:must|needs? to) be (?:later|after|greater)\b",
        r"\boutside (?:the )?(?:replay|retention) (?:window|range)\b",
        r"\b(?:replay|data) (?:is )?no longer (?:available|retained)\b",
        r"\breplay (?:is )?(?:unavailable|not available)\b",
        r"\bexceeds (?:the )?(?:replay |data )?retention\b",
    )),
    ("LIVE_NETWORK_TIMEOUT", (
        r"\b(?:connection|connect|socket|network|gateway|read|request|authentication) (?:has |operation )?(?:timed out|timeout)\b",
        r"\bconnection to \S+ timed out\b",
        r"\bauthentication with \S+ timed out\b",
        r"\btimeout (?:while|during) (?:connecting|reading|connection|authentication)\b",
        r"\btimed out (?:while |when )?(?:connecting|reading|waiting for (?:the )?(?:connection|gateway|response))\b",
    )),
    ("LIVE_CONNECTION_FAILED", (
        r"\bconnection (?:refused|reset|aborted|lost|closed|failed)\b",
        r"\bconnection to \S+ failed\b",
        r"\b(?:could not|couldn't|cannot|unable to|failed to) connect\b",
        r"\b(?:network is unreachable|host is unreachable|getaddrinfo failed)\b",
        r"\b(?:name resolution|dns resolution) (?:failed|failure)\b",
    )),
)


class ReplayCaptureError(RuntimeError):
    """Replay could not establish an intact, finite delivery."""


def _provider_error_category(messages: Sequence[str]) -> str | None:
    """Return only an allowlisted category supported by an explicit phrase.

    Mere references to authentication, licenses or connections are not proof of
    a failure category. Unrecognized provider text is deliberately discarded.
    """
    normalized = [" ".join(message[:4096].casefold().split()) for message in messages]
    for category, patterns in _ERROR_PHRASES:
        if any(re.search(pattern, message) for pattern in patterns for message in normalized):
            return category
    return None


def _external_failure(exc: Exception) -> str:
    # Explicit causal chains can carry the provider reason under a generic SDK
    # wrapper. Never render those messages, URLs, keys or traceback context.
    messages, seen = [], set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(messages) < 4:
        seen.add(id(current))
        messages.append(str(current))
        current = current.__cause__
    category = _provider_error_category(messages)
    return type(exc).__name__ + (f" [{category}]" if category else "")


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def _timestamp(value: object) -> pd.Timestamp:
    try:
        if value is None:
            raise ValueError("missing timestamp")
        result = pd.Timestamp(value)
        if pd.isna(result):
            raise ValueError("missing timestamp")
        return result.tz_localize("UTC") if result.tzinfo is None else result.tz_convert("UTC")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReplayCaptureError("Invalid replay timestamp") from exc


def _code(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        text = str(value).lower().replace("-", "_").split(".")[-1]
        return _CODES.get(text, -1)


def _checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scope(schema: str, symbols: Sequence[str] | str) -> tuple[str, ...]:
    if schema not in SUPPORTED_SCHEMAS:
        raise ReplayCaptureError("Unsupported XNAS replay schema")
    try:
        scope = (symbols,) if isinstance(symbols, str) else tuple(symbols)
    except TypeError as exc:
        raise ReplayCaptureError("Replay requires exactly one explicit raw equity symbol") from exc
    if (len(scope) != 1 or not isinstance(scope[0], str) or
            not re.fullmatch(r"[A-Z][A-Z0-9.]{0,5}", scope[0])):
        raise ReplayCaptureError("Replay requires exactly one explicit raw equity symbol")
    return scope


def _system_evidence(record: object) -> dict[str, object]:
    return {"code": _code(getattr(record, "code", None)),
            "msg": str(getattr(record, "msg", "")),
            "ts_event": int(getattr(record, "ts_event", 0))}


def _check_control(evidence: Mapping[str, object], *, schema: str,
                   subscription_id: int, completion: bool) -> None:
    expected = (f"Finished {schema} replay" if completion else
                f"Subscription request {subscription_id} for {schema} data succeeded")
    if evidence.get("code") != (3 if completion else 1) or evidence.get("msg") != expected:
        raise ReplayCaptureError("Replay control message does not match the subscribed schema/request")


class _CappedStream:
    def __init__(self, path: Path, limit: int) -> None:
        self._stream = path.open("xb")
        self.limit = limit
        self.bytes_written = 0

    @property
    def closed(self) -> bool:
        return self._stream.closed

    def writable(self) -> bool:
        return True

    def write(self, data: bytes) -> int:
        if self.bytes_written + len(data) > self.limit:
            raise ReplayCaptureError("Replay capture exceeded its byte budget")
        count = self._stream.write(data)
        if count != len(data):
            raise ReplayCaptureError("Replay capture encountered a short write")
        self.bytes_written += count
        return count

    def flush(self) -> None:
        self._stream.flush()

    def close(self) -> None:
        self._stream.close()


def capture_replay(
    api_key: str,
    schema: str,
    symbols: Sequence[str] | str,
    start: object,
    end: object,
    raw_path: Path | str,
    max_bytes: int,
    timeout_seconds: float = 300,
    client_factory: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Capture one explicit, completed interval; preserve failed raw files.

    The output is uncompressed native DBN, including every gateway control and
    mapping record.  Zstandard applies only to the network transport.  No data
    is published here, and a supplied ``.zst`` suffix does not change the bytes.
    """
    import databento as db

    scope = _scope(schema, symbols)
    lower, upper, connected_at = _timestamp(start), _timestamp(end), _utc_now()
    if not (0 < lower.value < upper.value <= connected_at.value):
        raise ReplayCaptureError("Replay must cover a nonempty completed interval with explicit start")
    if connected_at - lower > pd.Timedelta(hours=MAX_LOOKBACK_HOURS):
        raise ReplayCaptureError("Replay request exceeds the bounded 24-hour lookback")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ReplayCaptureError("Replay byte budget must be a positive integer")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or
            not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS):
        raise ReplayCaptureError("Replay timeout must be finite and at most 900 seconds")
    destination = Path(raw_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    stream = _CappedStream(destination, max_bytes)
    acknowledgements: list[dict[str, object]] = []
    completions: list[dict[str, object]] = []
    failures: list[str] = []
    errors: list[str] = []
    reconnects: list[object] = []
    client = None
    subscription_id = -1
    started = time.monotonic()

    def stop() -> None:
        if client is not None:
            try:
                client.stop()
            except Exception as exc:
                failures.append(f"stop failed: {type(exc).__name__}")

    def exception(exc: Exception) -> None:
        failures.append(str(exc) if isinstance(exc, ReplayCaptureError) else _external_failure(exc))
        stop()

    def reconnected(*args: object) -> None:
        reconnects.append(True)
        failures.append("Unexpected reconnect during replay")
        stop()

    def record_callback(record: object) -> None:
        if isinstance(record, db.ErrorMsg):
            category = _provider_error_category((str(record.err),))
            errors.append("ErrorMsg" + (f" [{category}]" if category else ""))
            stop()
        elif isinstance(record, db.SystemMsg):
            evidence = _system_evidence(record)
            if evidence["code"] == 1:
                _check_control(evidence, schema=schema, subscription_id=subscription_id, completion=False)
                acknowledgements.append(evidence)
            elif evidence["code"] == 3:
                _check_control(evidence, schema=schema, subscription_id=subscription_id, completion=True)
                completions.append(evidence)
                stop()
            elif evidence["code"] not in (0, 4):
                failures.append(f"Unexpected system message code {evidence['code']}")
                stop()
            # END_OF_INTERVAL only describes an interval schema publication.
            # Retain it in native DBN; it never substitutes for REPLAY_COMPLETED
            # or stops replay. See Databento Live basics/encodings SystemCode.

    try:
        factory = client_factory or db.Live
        client = factory(key=api_key, compression=db.Compression.ZSTD,
                         slow_reader_behavior="warn", reconnect_policy="none")
        client.add_callback(record_callback, exception_callback=exception)
        client.add_reconnect_callback(reconnected, exception_callback=exception)
        client.add_stream(stream, exception_callback=exception)
        subscription_id = client.subscribe(dataset=DATASET, schema=schema, symbols=list(scope),
                                           stype_in="raw_symbol", start=lower.isoformat())
        if isinstance(subscription_id, bool) or not isinstance(subscription_id, int) or subscription_id < 0:
            raise ReplayCaptureError("Live replay returned an invalid subscription identifier")
        client.start()
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise ReplayCaptureError("Replay timed out while subscribing")
        client.block_for_close(timeout=remaining)
        if time.monotonic() - started >= timeout_seconds:
            raise ReplayCaptureError("Replay exceeded its capture timeout")
        if errors or failures or reconnects:
            raise ReplayCaptureError("Replay failed: " + "; ".join(errors + failures))
        if len(acknowledgements) != 1 or len(completions) != 1:
            raise ReplayCaptureError("Replay closed or timed out without one acknowledged complete delivery")
        stream.flush()
    except Exception as exc:
        if isinstance(exc, ReplayCaptureError):
            raise
        # Provider exception bodies can include credential-bearing URLs. Do not
        # retain their text or expose it via an automatically rendered traceback.
        raise ReplayCaptureError(f"Live replay failed: {_external_failure(exc)}") from None
    finally:
        if client is not None:
            try:
                client.terminate()
                if client.is_connected():
                    client.block_for_close(timeout=5.0)
            except Exception:
                pass
        stream.close()

    delivery: dict[str, object] = {
        "schema_version": DELIVERY_VERSION, "mode": MODE, "delivery_mode": MODE,
        "dataset": DATASET, "schema": schema, "symbols": list(scope), "stype_in": "raw_symbol",
        "source_request_start": lower.isoformat(), "requested_start": lower.isoformat(),
        "requested_end": upper.isoformat(), "connection_started_at": connected_at.isoformat(),
        "finished_at": _utc_now().isoformat(), "subscription_id": subscription_id,
        "subscription_ack": acknowledgements, "replay_completed": completions,
        "error_messages": errors, "callback_errors": failures, "reconnect_count": len(reconnects),
        "completed": True, "sdk_version": db.__version__, "raw_format": "dbn",
        "transport_compression": "zstd", "slow_reader_behavior": "warn",
        "reconnect_policy": "none", "raw_bytes": destination.stat().st_size,
        "raw_sha256": _checksum(destination), "max_bytes": max_bytes,
        "timeout_seconds": timeout_seconds,
    }
    delivery.update(_inspect_native(destination, delivery))
    validate_replay_delivery(delivery, {"dataset": DATASET, "schema": schema,
                                      "symbols": list(scope), "stype_in": "raw_symbol",
                                      "start": lower.isoformat(), "end": upper.isoformat()})
    return delivery


def _mapping_interval(record: object, symbol: str) -> tuple[int, int, int]:
    """Require an exact raw-symbol mapping, including its instrument identity."""
    instrument_id = int(record.instrument_id)
    if (instrument_id <= 0 or str(record.stype_in) != "raw_symbol" or
            str(record.stype_in_symbol) != symbol):
        raise ReplayCaptureError("Native replay contains a mapping outside its raw symbol")
    output_type, output_symbol = str(record.stype_out), str(record.stype_out_symbol)
    if not ((output_type == "instrument_id" and output_symbol == str(instrument_id)) or
            (output_type == "raw_symbol" and output_symbol == symbol)):
        raise ReplayCaptureError("Native replay symbol mapping has inconsistent instrument identity")
    undefined = 2**64 - 1
    lower, upper = int(record.start_ts), int(record.end_ts)
    lower = 0 if lower == undefined else lower
    if not 0 <= lower < upper:
        raise ReplayCaptureError("Native replay mapping has an invalid interval")
    return instrument_id, lower, upper


def _inspect_native(path: Path, delivery: Mapping[str, object]) -> dict[str, object]:
    import databento as db

    with path.open("rb") as source:
        if source.read(3) != b"DBN":
            raise ReplayCaptureError("Replay raw_format must match uncompressed native DBN bytes")
    schema = str(delivery["schema"])
    symbol = _scope(schema, delivery["symbols"])[0]
    source_ns = _timestamp(delivery["source_request_start"]).value
    end_ns = _timestamp(delivery["requested_end"]).value
    finished_ns = _timestamp(delivery["finished_at"]).value
    count = data_count = raw_data_count = mapping_count = interval_marker_count = 0
    data_ids: set[int] = set()
    intervals: dict[int, list[tuple[int, int]]] = {}
    seen_bars: set[tuple[int, int]] = set()
    acknowledgements: list[dict[str, object]] = []
    completions: list[dict[str, object]] = []
    first_ts = last_ts = None
    store = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            store = db.DBNStore.from_file(path)
            metadata = store.metadata
            if metadata.dataset != DATASET or (metadata.schema is not None and str(metadata.schema) != schema):
                raise ReplayCaptureError("Native replay metadata dataset/schema differs from request")
            if int(metadata.start) != source_ns:
                raise ReplayCaptureError("Native replay metadata start differs from explicit source start")
            if metadata.end is not None:
                raise ReplayCaptureError("Native Live replay unexpectedly declares a historical end")
            if metadata.partial or metadata.not_found:
                raise ReplayCaptureError("Native replay metadata contains unresolved symbols")
            if ((metadata.stype_in is not None and str(metadata.stype_in) != "raw_symbol") or
                    str(metadata.stype_out) != "instrument_id" or
                    (metadata.symbols and tuple(metadata.symbols) != (symbol,)) or metadata.mappings):
                raise ReplayCaptureError("Native replay metadata symbology differs from its raw symbol")
            # Live metadata may omit schema and input symbols. Exact identity is
            # then supplied by each retained mapping and each data record rtype.
            for record in store:
                count += 1
                if isinstance(record, db.ErrorMsg):
                    raise ReplayCaptureError("Native replay contains a gateway error")
                if isinstance(record, db.SystemMsg):
                    evidence = _system_evidence(record)
                    code = evidence["code"]
                    if code in (1, 3):
                        complete = code == 3
                        if complete and (len(acknowledgements) != 1 or not data_count):
                            raise ReplayCaptureError("Replay completion preceded acknowledgement or requested data")
                        if (not complete and (acknowledgements or raw_data_count or completions)) or completions:
                            raise ReplayCaptureError("Native replay contains duplicate or unordered controls")
                        _check_control(evidence, schema=schema,
                                       subscription_id=int(delivery["subscription_id"]), completion=complete)
                        (completions if complete else acknowledgements).append(evidence)
                    elif code == 4:
                        interval_marker_count += 1
                    elif code != 0:
                        raise ReplayCaptureError("Native replay contains an unexpected system warning/control")
                    continue
                if isinstance(record, db.SymbolMappingMsg):
                    mapping_count += 1
                    instrument_id, lower, upper = _mapping_interval(record, symbol)
                    intervals.setdefault(instrument_id, []).append((lower, upper))
                    continue
                if int(record.rtype) != _RTYPES[schema]:
                    raise ReplayCaptureError("Native replay contains an unexpected data schema")
                if len(acknowledgements) != 1:
                    raise ReplayCaptureError("Native replay data preceded its acknowledgement")
                instrument_id, stamp = int(record.instrument_id), int(record.ts_event)
                if not source_ns <= stamp <= finished_ns:
                    raise ReplayCaptureError("Native replay contains records outside its explicit source interval")
                if not any(lower <= stamp < upper for lower, upper in intervals.get(instrument_id, ())):
                    raise ReplayCaptureError("Native replay data lacks a valid preceding instrument mapping")
                key = instrument_id, stamp
                if key in seen_bars:
                    raise ReplayCaptureError("Native replay contains duplicate minute bars")
                seen_bars.add(key)
                raw_data_count += 1
                if stamp >= end_ns:
                    continue
                # Live continues to connection time. Retain those bytes, but do
                # not count them as target coverage or publish them as target rows.
                data_count += 1
                data_ids.add(instrument_id)
                first_ts = stamp if first_ts is None else min(first_ts, stamp)
                last_ts = stamp if last_ts is None else max(last_ts, stamp)
            metadata_start = int(metadata.start)
    except ReplayCaptureError:
        raise
    except Exception as exc:
        raise ReplayCaptureError(f"Native replay DBN verification failed: {type(exc).__name__}") from exc
    finally:
        if store is not None:
            store.reader.close()
    if len(acknowledgements) != 1 or len(completions) != 1:
        raise ReplayCaptureError("Native replay lacks a unique subscription acknowledgement and completion")
    if not data_count:
        raise ReplayCaptureError("Native replay contains no data in the requested interval")
    if acknowledgements != delivery.get("subscription_ack") or completions != delivery.get("replay_completed"):
        raise ReplayCaptureError("Native replay controls differ from delivery evidence")
    return {"record_count": count, "data_record_count": data_count,
            "raw_data_record_count": raw_data_count,
            "out_of_window_record_count": raw_data_count - data_count,
            "mapping_record_count": mapping_count, "instrument_count": len(data_ids),
            "interval_marker_count": interval_marker_count,
            "first_record_timestamp_ns": first_ts, "last_record_timestamp_ns": last_ts,
            "metadata_start_ns": metadata_start,
            "normalized_window_start": _timestamp(source_ns).isoformat(),
            "normalized_window_end": _timestamp(end_ns).isoformat()}


def validate_replay_delivery(delivery: Mapping[str, object], request: Mapping[str, object],
                             raw_path: Path | str | None = None) -> None:
    """Validate exact requested scope and optionally re-read all retained DBN.

    Unlike the OPRA warmup path, an XNAS request cannot be narrowed after
    acquisition. Native file revalidation is required at publication time.
    Counts and first/last timestamps describe only requested [start, end) bars.
    """
    if (delivery.get("mode") != MODE or delivery.get("delivery_mode") != MODE or
            delivery.get("schema_version") != DELIVERY_VERSION):
        raise ReplayCaptureError("Invalid replay delivery version or mode")
    schema = str(request.get("schema", ""))
    scope = _scope(schema, request.get("symbols", ()))
    if request.get("dataset", DATASET) != DATASET or delivery.get("dataset") != DATASET:
        raise ReplayCaptureError("Replay dataset mismatch")
    if delivery.get("schema") != schema or tuple(delivery.get("symbols", ())) != scope:
        raise ReplayCaptureError("Replay schema or raw symbol scope mismatch")
    if request.get("stype_in", "raw_symbol") != "raw_symbol" or delivery.get("stype_in") != "raw_symbol":
        raise ReplayCaptureError("Replay requires raw_symbol symbology")
    source = _timestamp(delivery.get("source_request_start"))
    lower = _timestamp(request.get("start", request.get("request_start")))
    upper = _timestamp(request.get("end", request.get("request_end")))
    declared = _timestamp(delivery.get("requested_start"))
    delivered_end = _timestamp(delivery.get("requested_end"))
    connected = _timestamp(delivery.get("connection_started_at"))
    finished = _timestamp(delivery.get("finished_at"))
    if not (0 < source.value == declared.value == lower.value < upper.value ==
            delivered_end.value <= connected.value <= finished.value):
        raise ReplayCaptureError("Replay bounds do not exactly match the completed target interval")
    if connected - source > pd.Timedelta(hours=MAX_LOOKBACK_HOURS):
        raise ReplayCaptureError("Replay source is outside bounded lookback")
    if (delivery.get("completed") is not True or delivery.get("error_messages") != [] or
            delivery.get("callback_errors") != [] or delivery.get("reconnect_count") != 0 or
            delivery.get("slow_reader_behavior") != "warn" or delivery.get("reconnect_policy") != "none"):
        raise ReplayCaptureError("Replay did not complete without errors, skips, or reconnections")
    subscription_id = delivery.get("subscription_id")
    if isinstance(subscription_id, bool) or not isinstance(subscription_id, int) or subscription_id < 0:
        raise ReplayCaptureError("Replay subscription identifier is invalid")
    for key, completion in (("subscription_ack", False), ("replay_completed", True)):
        entries = delivery.get(key)
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], Mapping):
            raise ReplayCaptureError("Replay requires one matching acknowledgement and completion")
        _check_control(entries[0], schema=schema, subscription_id=subscription_id, completion=completion)
    if delivery.get("raw_format") != "dbn" or delivery.get("transport_compression") != "zstd":
        raise ReplayCaptureError("Replay encoding provenance is invalid")
    budget, size, timeout = delivery.get("max_bytes"), delivery.get("raw_bytes"), delivery.get("timeout_seconds")
    if (isinstance(budget, bool) or not isinstance(budget, int) or
            isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= budget or
            not re.fullmatch(r"[a-f0-9]{64}", str(delivery.get("raw_sha256", "")))):
        raise ReplayCaptureError("Replay native file size/hash or byte budget is invalid")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
            not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS):
        raise ReplayCaptureError("Replay timeout evidence is invalid")
    count = delivery.get("data_record_count")
    first, last = delivery.get("first_record_timestamp_ns"), delivery.get("last_record_timestamp_ns")
    if (isinstance(count, bool) or not isinstance(count, int) or count < 1 or
            isinstance(first, bool) or not isinstance(first, int) or
            isinstance(last, bool) or not isinstance(last, int) or
            not lower.value <= first <= last < upper.value):
        raise ReplayCaptureError("Replay requested-window record evidence is empty or invalid")
    if raw_path is not None:
        path = Path(raw_path)
        if path.stat().st_size != size or _checksum(path) != delivery.get("raw_sha256"):
            raise ReplayCaptureError("Native replay file checksum/size differs from receipt")
        native = _inspect_native(path, delivery)
        # Older v1 receipts predate marker counts. They remain valid only when
        # their native stream has no markers; counted evidence cannot be omitted
        # from a stream that actually contains END_OF_INTERVAL records.
        if any(delivery.get(key, 0 if key == "interval_marker_count" else None) != value
               for key, value in native.items()):
            raise ReplayCaptureError("Native replay counts or timestamps differ from receipt")


__all__ = ["ReplayCaptureError", "capture_replay", "validate_replay_delivery"]

