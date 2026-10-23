"""Persist actual completed native minutes before intraday aggregation or fitting.

This adapter uses an existing configured provider. Immutable local response
receipts precede canonical upserts, so later model failure cannot lose fetched
minutes. Missing minutes stay missing; the existing 15m resolver remains the
only authority for its observed/synthetic working-frame representation.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import threading
import uuid

import numpy as np
import pandas as pd

from app.services.databento_market_data import _bars_from_databento_frame
from datafetching.parquet_store import ParquetStore
from .prediction import ContractError, STEP, instant


VERSION = "intraday-native-minute-persistence-v1"
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _now():
    return datetime.now(timezone.utc)


def _scope(pc, symbols):
    symbols = tuple(symbols)
    if (pc not in {"Atlas", "Scout"} or not symbols or len(symbols) != len(set(symbols))
            or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", s)
                   for s in symbols)):
        raise ContractError("Minute persistence requires the PC's explicit unique symbol scope")
    return symbols


def _request(*, pc, symbols, spec, dataset, start, end, observed_at, allow_catchup=False):
    symbols = _scope(pc, symbols)
    op, cl, observed = instant(start), instant(end), instant(observed_at)
    if (dataset != "EQUS.MINI" or getattr(spec, "schema", None) != "ohlcv-1m"
            or getattr(spec, "frequency", None) != "1m"
            or not re.fullmatch(r"[A-Za-z0-9_-]+", str(getattr(spec, "key", "")))):
        raise ContractError("Minute persistence requires the existing native EQUS.MINI ohlcv-1m source")
    if type(allow_catchup) is not bool:
        raise ContractError("Catchup range selection must be an explicit boolean")
    interval = cl - op
    if (op.minute % 15 or op.second or op.microsecond or cl.minute % 15 or cl.second or cl.microsecond
            or interval <= timedelta(0) or observed < cl
            or (not allow_catchup and interval != STEP)
            or (allow_catchup and interval > timedelta(days=1))):
        raise ContractError("Minute persistence requires completed whole quarters; live reads use one quarter "
                            "and explicit catchup is limited to one day")
    return symbols, op, cl, observed


def _jsonable(value):
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, np.generic):
        return _jsonable(value.item())
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return {"nonfinite_native_value": repr(value)}
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _write_bytes(path, payload):
    # A final name exposes complete fsynced bytes exclusively. response.json is
    # the archive commit point, before any canonical mutation.
    path = Path(path)
    temporary = path.with_name(path.name + ".pending-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path, value):
    payload = json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8") + b"\n"
    _write_bytes(path, payload)


def _archive(results, selected, *, datastore, request):
    attempt = uuid.uuid4().hex
    directory = Path(datastore) / "intraday" / request["pc"].lower() / "minute-fetches" / attempt
    directory.mkdir(parents=True, exist_ok=False)
    entries = []
    for index, (symbol, result) in enumerate(results.items()):
        bars, raw = result
        raw_evidence = None
        if raw is not None:
            if not isinstance(raw, pd.DataFrame):
                raise ContractError("Native minute raw response must be a DataFrame or None")
            buffer = io.BytesIO()
            raw.to_parquet(buffer, index=None)
            payload = buffer.getvalue()
            filename = f"raw-{index:03d}.parquet"
            _write_bytes(directory / filename, payload)
            raw_evidence = {"file": filename, "sha256": hashlib.sha256(payload).hexdigest(),
                            "rows": len(raw), "columns": list(raw.columns)}
        entries.append({"symbol": symbol, "raw": raw_evidence, "bars": _jsonable(bars)})
    response = {"version": VERSION, "attempt_id": attempt, "request": request,
        "selected_range": {"schema": getattr(selected, "schema", None),
                           "start": getattr(selected, "start", None), "end": getattr(selected, "end", None)},
        "symbols": entries, "canonical_persistence": "recorded_separately_in_persistence.json"}
    _write_json(directory / "response.json", response)
    return directory


def _timestamp(value):
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ContractError("Native minute timestamps must be explicit timezone-aware event times")
    return timestamp.tz_convert("UTC")


def _validate_symbol(symbol, bars, raw, *, op, cl):
    if raw is None:
        if bars:
            raise ContractError("Observed native minutes require their original raw response")
        return
    if len(raw) != len(bars):
        raise ContractError("Native raw rows were omitted during parsing; no filled or partial substitute is admitted")
    if raw.empty:
        return
    if "symbol" not in raw or not raw.symbol.map(lambda v: str(v).strip().upper()).eq(symbol).all():
        raise ContractError("Native minute raw response differs from its requested symbol")
    for field in ("synthetic", "no_trade"):
        if field in raw and raw[field].fillna(False).astype(bool).any():
            raise ContractError("Canonical native minutes cannot contain generated or no-trade fills")
    column = "ts_event" if "ts_event" in raw else "timestamp" if "timestamp" in raw else None
    if column is None:
        raise ContractError("Native minute raw response requires event timestamps")
    raw_times = tuple(_timestamp(v) for v in raw[column])
    if len(set(raw_times)) != len(raw_times):
        raise ContractError("Duplicate native minute timestamps in one symbol response")
    for timestamp in raw_times:
        if timestamp != timestamp.floor("1min") or timestamp < op or timestamp + pd.Timedelta(minutes=1) > cl:
            raise ContractError("Native minute lies outside the exact completed interval")
    parsed = _bars_from_databento_frame(symbol, "1m", raw)
    if len(parsed) != len(bars):
        raise ContractError("Native minute raw OHLCV cannot be decoded completely")
    ordered = sorted(bars, key=lambda bar: _timestamp(bar.timestamp))
    for bar, native in zip(ordered, parsed):
        if (bar.symbol != symbol or bar.source != "databento" or bar.timeframe != "1m"
                or _timestamp(bar.timestamp) != _timestamp(native.timestamp)):
            raise ContractError("Native minute parsed identity differs from its raw response")
        values = {field: float(getattr(bar, field)) for field in ("open", "high", "low", "close", "volume")}
        if (not all(math.isfinite(v) for v in values.values()) or values["volume"] < 0
                or min(values[f] for f in ("open", "high", "low", "close")) <= 0
                or values["high"] < max(values["open"], values["close"], values["low"])
                or values["low"] > min(values["open"], values["close"])):
            raise ContractError("Native minute has nonfinite or invalid OHLCV")
        if any(values[field] != float(getattr(native, field)) for field in values):
            raise ContractError("Native minute parsed OHLCV differs from original provider values")


def _writer_lock(datastore):
    key = str(Path(datastore).resolve()).casefold()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def persist_native_minutes(results, selected, *, datastore, pc, symbols, spec, dataset,
                           start, end, requested_at, observed_at, allow_catchup=False,
                           source_omission_contract=None):
    """Archive an actual exact-range response, then upsert only observed minutes.

    This function makes no provider call and is also usable for authorized
    catchup with explicit ``allow_catchup=True`` for whole quarters up to one day.
    Both successful and failed persistence have immutable separate
    receipts. Canonical writes serialize adapters in this Python process; other
    processes using ParquetStore must retain their existing writer ownership.
    """
    symbols, op, cl, observed = _request(pc=pc, symbols=symbols, spec=spec, dataset=dataset,
        start=start, end=end, observed_at=observed_at, allow_catchup=allow_catchup)
    requested = instant(requested_at)
    if requested > observed or requested < cl:
        raise ContractError("Native minute request must follow interval completion and precede response receipt")
    request = {"pc": pc, "symbols": list(symbols), "dataset": dataset, "schema": spec.schema,
        "frequency": spec.frequency, "spec_key": spec.key, "start": op.isoformat(), "end": cl.isoformat(),
        "requested_at": requested.isoformat(), "observed_at": observed.isoformat(),
        "range_policy": "explicit_catchup_up_to_one_day" if allow_catchup else "single_completed_quarter"}
    directory = _archive(results, selected, datastore=datastore, request=request)
    response_bytes = (directory / "response.json").read_bytes()
    archived = json.loads(response_bytes)
    raw_by_symbol = {item["symbol"]: item["raw"] for item in archived["symbols"]}
    status = {"version": VERSION, "response_receipt": str(directory / "response.json"),
        "response_receipt_sha256": hashlib.sha256(response_bytes).hexdigest(),
        "persistence_receipt": str(directory / "persistence.json"), "pc": pc,
        "start": op.isoformat(), "end": cl.isoformat(), "status": "failed", "symbols": {}, "error": None}
    try:
        if (set(results) != set(symbols) or getattr(selected, "schema", None) != spec.schema
                or instant(selected.start) != op or instant(selected.end) != cl):
            raise ContractError("Native response did not preserve the exact requested interval or symbol set")
        for symbol in symbols:
            _validate_symbol(symbol, *results[symbol], op=pd.Timestamp(op), cl=pd.Timestamp(cl))
        request_key = f"{spec.key}_{spec.schema}_{spec.frequency}"
        metadata = {"provider_dataset": dataset, "source_schema": spec.schema,
            "source_frequency": spec.frequency, "output_frequency": spec.frequency,
            "aggregation_method": "native", "fetch_profile": "intraday-manual-minute-persistence",
            "price_basis": "unadjusted_market_scale", "volume_basis": "unadjusted_market_scale",
            "corporate_action_adjustment": "none", "normalized_bar_policy": "completed_intervals_only",
            "batch_watchlist_symbol_count": len(symbols), "range_start": op.isoformat(), "range_end": cl.isoformat()}
        with _writer_lock(datastore):
            store = ParquetStore(datastore)
            for symbol in symbols:
                bars, raw = results[symbol]
                expected_minutes = int((cl - op).total_seconds() // 60)
                status["symbols"][symbol] = {"observed_minutes": len(bars),
                    "expected_minutes": expected_minutes, "missing_minutes": expected_minutes - len(bars),
                    "raw_path": None, "normalized_path": None, "status": "persisting"}
                evidence = status["symbols"][symbol]
                # A successful complete parse and retained raw bytes are
                # distinct from advertised dataset availability. Empty frames
                # do not establish symbol resolution and remain unknown.
                from .no_trade import CONTRACT
                raw_evidence = raw_by_symbol[symbol]
                if (source_omission_contract == CONTRACT and raw_evidence is not None
                        and len(bars) > 0 and raw_evidence["rows"] == len(bars)):
                    evidence["source_response"] = {"contract": CONTRACT, "complete": True,
                        "symbol_resolution": "returned_native_raw_symbol", "raw_rows": len(raw),
                        "parsed_rows": len(bars), "raw_sha256": raw_evidence["sha256"],
                        "raw_file": raw_evidence["file"],
                        "requested_at": requested.isoformat(), "received_at": observed.isoformat()}
                if raw is not None and not raw.empty:
                    target = store.save_raw_frame(source="databento", category="bars", symbol=symbol,
                        endpoint=f"{request_key}_raw", frame=raw, timeframe=spec.frequency, metadata=metadata)
                    evidence["raw_path"] = str(target or store.target_path(scope="raw", source="databento",
                        category="bars", symbol=symbol, suffix=f"{request_key}_raw", timeframe=spec.frequency,
                        dataset_key=f"{request_key}_raw"))
                if bars:
                    target = store.save_bars("databento", symbol, spec.frequency, bars,
                        request_key=request_key, metadata=metadata, as_of=observed)
                    evidence["normalized_path"] = str(target or store.target_path(scope="normalized",
                        source="databento", category="bars", symbol=symbol,
                        suffix=request_key, timeframe=spec.frequency))
                evidence["status"] = "complete"
        status["status"] = "complete"
    except Exception as exc:
        status["error"] = {"type": type(exc).__name__, "message": str(exc)}
        try:
            _write_json(directory / "persistence.json", status)
        finally:
            try:
                exc.minute_persistence_receipt = deepcopy(status)
            except Exception:
                pass
        raise
    _write_json(directory / "persistence.json", status)
    return status


class PersistedMinuteProvider:
    """Delegate the existing provider while persisting each exact minute read."""
    def __init__(self, provider, *, datastore, pc, symbols, clock=_now):
        self._provider = provider
        self._datastore = Path(datastore)
        self._pc, self._symbols = pc, _scope(pc, symbols)
        self._clock = clock
        self._last_receipt = None

    def __getattr__(self, name):
        return getattr(self._provider, name)

    @property
    def last_receipt(self):
        return deepcopy(self._last_receipt)

    def fetch_native_bars_range(self, symbols, spec, *, start, end, available_range=None):
        symbols = tuple(symbols)
        if not set(symbols).issubset(self._symbols):
            raise ContractError("Native minute request is outside this PC's own symbol scope")
        requested = instant(self._clock())
        _request(pc=self._pc, symbols=symbols, spec=spec, dataset=self.dataset,
                 start=start, end=end, observed_at=requested)
        self._last_receipt = None
        results, selected = self._provider.fetch_native_bars_range(symbols, spec,
            start=start, end=end, available_range=available_range)
        try:
            from app.services.databento_market_data import DatabentoMarketDataProvider
            from .no_trade import CONTRACT
            # Only the reviewed adapter's unmodified full-range decode earns
            # this scope contract; arbitrary provider delegates keep working
            # but cannot turn absence into zero-trade evidence.
            source_contract = (CONTRACT if type(self._provider) is DatabentoMarketDataProvider
                and "fetch_native_bars_range" not in vars(self._provider) else None)
            self._last_receipt = persist_native_minutes(results, selected, datastore=self._datastore,
                pc=self._pc, symbols=symbols, spec=spec, dataset=self.dataset, start=start, end=end,
                requested_at=requested, observed_at=self._clock(), source_omission_contract=source_contract)
        except Exception as exc:
            self._last_receipt = getattr(exc, "minute_persistence_receipt", None)
            raise
        return results, selected
