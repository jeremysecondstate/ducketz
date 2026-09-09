"""Finite, fail-closed OPRA intraday replay capture and native evidence checks.

The Live gateway decides whether an explicit start is still retained.  Its
nominal 24-hour window is not a client-side guarantee of session coverage.
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


DATASET = "OPRA.PILLAR"
MODE = "live-intraday-replay"
DELIVERY_VERSION = "databento-opra-live-replay-v1"
SUPPORTED_SCHEMAS = frozenset(("definition", "cbbo-1m", "ohlcv-1h"))
MAX_LOOKBACK_HOURS = 48
MAX_TIMEOUT_SECONDS = 900
_RTYPES = {"definition": 19, "cbbo-1m": 193, "ohlcv-1h": 34}
_CODES = {"subscription_ack": 1, "replay_completed": 3, "heartbeat": 0,
          "slow_reader_warning": 2, "end_of_interval": 4}


class ReplayCaptureError(RuntimeError):
    """Replay could not establish an intact, finite delivery."""


def _utc_now() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def _timestamp(value: object) -> pd.Timestamp:
    try:
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
        raise ReplayCaptureError("Unsupported OPRA replay schema")
    scope = (symbols,) if isinstance(symbols, str) else tuple(symbols)
    if len(scope) != 1 or not re.fullmatch(r"[A-Z][A-Z0-9.]{0,5}\.OPT", scope[0]):
        raise ReplayCaptureError("Replay requires exactly one explicit option parent root")
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
        raise ReplayCaptureError("Replay request exceeds the bounded 48-hour lookback")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ReplayCaptureError("Replay byte budget must be a positive integer")
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS:
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
        failures.append(f"{type(exc).__name__}: {exc}")
        stop()

    def reconnected(*args: object) -> None:
        reconnects.append(True)
        failures.append("Unexpected reconnect during replay")
        stop()

    def record_callback(record: object) -> None:
        if isinstance(record, db.ErrorMsg):
            errors.append(str(record.err))
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

    try:
        factory = client_factory or db.Live
        client = factory(key=api_key, compression=db.Compression.ZSTD,
                         slow_reader_behavior="warn", reconnect_policy="none")
        client.add_callback(record_callback, exception_callback=exception)
        client.add_reconnect_callback(reconnected, exception_callback=exception)
        client.add_stream(stream, exception_callback=exception)
        subscription_id = client.subscribe(dataset=DATASET, schema=schema, symbols=list(scope),
                                           stype_in="parent", start=lower.isoformat())
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
        raise ReplayCaptureError(f"Live replay failed: {type(exc).__name__}: {exc}") from exc
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
        "dataset": DATASET, "schema": schema, "symbols": list(scope), "stype_in": "parent",
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
                                      "symbols": list(scope), "stype_in": "parent",
                                      "start": lower.isoformat(), "end": upper.isoformat()})
    return delivery


def _inspect_native(path: Path, delivery: Mapping[str, object]) -> dict[str, object]:
    import databento as db

    with path.open("rb") as source:
        if source.read(3) != b"DBN":
            raise ReplayCaptureError("Replay raw_format must match uncompressed native DBN bytes")
    schema = str(delivery["schema"])
    parent = _scope(schema, delivery["symbols"])[0]
    root = parent[:-4]
    source_ns = _timestamp(delivery["source_request_start"]).value
    raw_symbol_pattern = re.compile(rf"^{re.escape(root)}\s*\d{{6}}[CP]\d{{8}}$")
    count = data_count = mapping_count = 0
    data_ids: set[int] = set()
    mapped_ids: set[int] = set()
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
            # Live metadata has no Historical API end; completion is proved by
            # retained gateway messages, never an invented metadata end.
            for record in store:
                count += 1
                if isinstance(record, db.ErrorMsg):
                    raise ReplayCaptureError(f"Native replay contains gateway error: {record.err}")
                if isinstance(record, db.SystemMsg):
                    evidence = _system_evidence(record)
                    if evidence["code"] in (1, 3):
                        complete = evidence["code"] == 3
                        if complete and (len(acknowledgements) != 1 or not data_count):
                            raise ReplayCaptureError("Replay completion preceded acknowledgement or data")
                        _check_control(evidence, schema=schema,
                                       subscription_id=int(delivery["subscription_id"]), completion=complete)
                        (completions if complete else acknowledgements).append(evidence)
                    continue
                if isinstance(record, db.SymbolMappingMsg):
                    mapping_count += 1
                    if (str(record.stype_in_symbol) != parent or
                            not raw_symbol_pattern.fullmatch(str(record.stype_out_symbol))):
                        raise ReplayCaptureError("Native replay contains a symbol outside its option root")
                    mapped_ids.add(int(record.instrument_id))
                    continue
                if int(record.rtype) != _RTYPES[schema]:
                    raise ReplayCaptureError("Native replay contains an unexpected data schema")
                data_count += 1
                data_ids.add(int(record.instrument_id))
                if schema == "definition":
                    if not raw_symbol_pattern.fullmatch(str(record.raw_symbol)):
                        raise ReplayCaptureError("Native definition is outside the requested option root")
                    mapped_ids.add(int(record.instrument_id))
                stamp = int(record.ts_recv if schema == "cbbo-1m" else record.ts_event)
                if stamp < source_ns:
                    raise ReplayCaptureError("Native replay contains records before its explicit source start")
                first_ts = stamp if first_ts is None else min(first_ts, stamp)
                last_ts = stamp if last_ts is None else max(last_ts, stamp)
            metadata_start = int(metadata.start)
    except ReplayCaptureError:
        raise
    except Exception as exc:
        raise ReplayCaptureError(f"Native replay DBN verification failed: {type(exc).__name__}: {exc}") from exc
    finally:
        if store is not None:
            store.reader.close()
    if len(acknowledgements) != 1 or len(completions) != 1:
        raise ReplayCaptureError("Native replay lacks a unique subscription acknowledgement and completion")
    if not data_count or data_ids.difference(mapped_ids):
        raise ReplayCaptureError("Native replay is empty or lacks instrument symbol mappings")
    if acknowledgements != delivery.get("subscription_ack") or completions != delivery.get("replay_completed"):
        raise ReplayCaptureError("Native replay controls differ from delivery evidence")
    return {"record_count": count, "data_record_count": data_count,
            "mapping_record_count": mapping_count, "instrument_count": len(data_ids),
            "first_record_timestamp_ns": first_ts, "last_record_timestamp_ns": last_ts,
            "metadata_start_ns": metadata_start}


def validate_replay_delivery(delivery: Mapping[str, object], request: Mapping[str, object],
                             raw_path: Path | str | None = None) -> None:
    """Validate exact scope and, when supplied, independently re-read native DBN.

    A canonical target may start later than the explicit source start (for
    interval warmup).  It must remain inside the captured, completed interval.
    """
    if delivery.get("mode") != MODE or delivery.get("schema_version") != DELIVERY_VERSION:
        raise ReplayCaptureError("Invalid replay delivery version or mode")
    schema = str(request.get("schema", ""))
    scope = _scope(schema, request.get("symbols", ()))
    if request.get("dataset", DATASET) != DATASET or delivery.get("dataset") != DATASET:
        raise ReplayCaptureError("Replay dataset mismatch")
    if delivery.get("schema") != schema or tuple(delivery.get("symbols", ())) != scope:
        raise ReplayCaptureError("Replay schema or parent scope mismatch")
    if request.get("stype_in", "parent") != "parent" or delivery.get("stype_in") != "parent":
        raise ReplayCaptureError("Replay requires parent symbology")
    source = _timestamp(delivery.get("source_request_start"))
    lower = _timestamp(request.get("start", request.get("request_start")))
    upper = _timestamp(request.get("end", request.get("request_end")))
    declared = _timestamp(delivery.get("requested_start"))
    delivered_end = _timestamp(delivery.get("requested_end"))
    connected = _timestamp(delivery.get("connection_started_at"))
    if not 0 < source.value <= declared.value <= lower.value < upper.value <= delivered_end.value <= connected.value:
        raise ReplayCaptureError("Replay bounds do not cover the completed target interval")
    if connected - source > pd.Timedelta(hours=MAX_LOOKBACK_HOURS):
        raise ReplayCaptureError("Replay source is outside bounded lookback")
    if (delivery.get("completed") is not True or delivery.get("error_messages") != [] or
            delivery.get("callback_errors") != [] or delivery.get("reconnect_count") != 0 or
            delivery.get("slow_reader_behavior") != "warn" or delivery.get("reconnect_policy") != "none"):
        raise ReplayCaptureError("Replay did not complete without errors, skips, or reconnections")
    for key, completion in (("subscription_ack", False), ("replay_completed", True)):
        entries = delivery.get(key)
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], Mapping):
            raise ReplayCaptureError("Replay requires one matching acknowledgement and completion")
        _check_control(entries[0], schema=schema, subscription_id=int(delivery["subscription_id"]),
                       completion=completion)
    if delivery.get("raw_format") != "dbn" or delivery.get("transport_compression") != "zstd":
        raise ReplayCaptureError("Replay encoding provenance is invalid")
    if raw_path is not None:
        path = Path(raw_path)
        if path.stat().st_size != delivery.get("raw_bytes") or _checksum(path) != delivery.get("raw_sha256"):
            raise ReplayCaptureError("Native replay file checksum/size differs from receipt")
        native = _inspect_native(path, delivery)
        if any(delivery.get(key) != value for key, value in native.items()):
            raise ReplayCaptureError("Native replay counts or timestamps differ from receipt")


__all__ = ["ReplayCaptureError", "capture_replay", "validate_replay_delivery"]
