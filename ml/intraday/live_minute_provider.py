"""15-minute-only native EQUS.MINI minute acquisition from independent Live replay.

The compatibility range methods describe local request bounds, never historical
API availability or market-wide coverage. A successful fetch requires a fresh,
fully decoded native replay with durable controls and instrument mappings.
Every fetch makes its own capture; cached receipts cannot provide a second
sparse-interval stability confirmation. No account/order endpoint exists here.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import threading
import uuid

import pandas as pd

from app.services.databento_market_data import DatabentoAvailableRange, _bars_from_databento_frame
from app.services.market_fetch_specs import databento_analysis_source_specs
from .market_collection import SourceRequest, canonical, instant
from .prediction import ContractError

VERSION = "intraday-live-minute-delivery-v1"


def _now():
    return datetime.now(timezone.utc)


class LiveMinuteReplayProvider:
    source = "databento"
    dataset = "EQUS.MINI"
    schema = "ohlcv-1m"

    def __init__(self, *, datastore, pc, symbols, clock=_now, api_key=None, capture=None):
        if str(pc).lower() not in {"scout", "atlas"}:
            raise ContractError("Live minutes require an explicit local PC identity")
        self.pc = str(pc).lower()
        self.symbols = tuple(str(s).strip().upper() for s in symbols)
        if (not self.symbols or len(self.symbols) != len(set(self.symbols))
                or any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,19}", s) for s in self.symbols)):
            raise ContractError("Live minutes require an explicit unique symbol scope")
        # SourceRequest performs the shared non-secret symbol validation.
        for symbol in self.symbols:
            SourceRequest("databento", "stock_bars", symbol, self.dataset, self.schema)
        self.root = Path(datastore) / "intraday" / self.pc / "live-minute-replay"
        self.clock, self.api_key, self._capture_override = clock, api_key, capture
        self._last_delivery = None
        self._lock = threading.Lock()

    def native_specs(self):
        return tuple(s for s in databento_analysis_source_specs() if s.schema == self.schema and s.frequency == "1m")

    def dataset_range(self):
        now = instant(self.clock())
        return {"start": (now - timedelta(hours=48)).isoformat(), "end": now.isoformat(),
                "schema": {self.schema: {"start": (now - timedelta(hours=48)).isoformat(), "end": now.isoformat()}},
                "range_basis": "local_live_replay_request_bounds_not_provider_availability",
                "successful_delivery_requires_fresh_native_replay": True}

    def available_range_for_schema(self, schema, *, dataset_range=None):
        if schema != self.schema:
            raise ContractError("Primary live minutes cannot substitute another schema")
        value = dataset_range if dataset_range is not None else self.dataset_range()
        if value.get("range_basis") != "local_live_replay_request_bounds_not_provider_availability":
            raise ContractError("Live replay bounds cannot come from historical metadata")
        selected = value["schema"][schema]
        return DatabentoAvailableRange(schema, instant(selected["start"]), instant(selected["end"]))

    @property
    def last_delivery_evidence(self):
        return deepcopy(self._last_delivery)

    def fetch_native_bars_range(self, symbols, spec, *, start, end, available_range=None):
        with self._lock:
            return self._fetch(symbols, spec, start=start, end=end, available_range=available_range)

    def _fetch(self, symbols, spec, *, start, end, available_range):
        self._last_delivery = None
        symbols = tuple(str(s).strip().upper() for s in symbols)
        if not symbols or len(symbols) != len(set(symbols)) or not set(symbols).issubset(self.symbols):
            raise ContractError("Live minute request exceeds this PC's frozen symbol scope")
        op, cl, started = instant(start), instant(end), instant(self.clock())
        if (spec not in self.native_specs() or op.minute % 15 or op.second or op.microsecond
                or cl - op != timedelta(minutes=15) or cl > started or started - op > timedelta(hours=48)):
            raise ContractError("Live minute replay requires its exact native spec and one completed recent quarter")
        if available_range is not None and (available_range.schema != self.schema
                or instant(available_range.start) > op or instant(available_range.end) < cl):
            raise ContractError("Requested quarter exceeds the local live replay request bounds")
        key = self.api_key if self.api_key is not None else os.getenv("DATABENTO_API_KEY", "").strip()
        if not key:
            raise RuntimeError("Missing existing Databento API credentials")
        from .market_collection_replay import capture, PROJECTION_VERSION
        request = SourceRequest("databento", "stock_bars", "*", self.dataset, self.schema,
            params={"symbols": list(symbols), "stype_in": "raw_symbol", "projection_version": PROJECTION_VERSION})
        attempt = uuid.uuid4().hex
        directory = self.root / op.date().isoformat() / attempt
        raw_path = directory / "native.dbn"
        # No shared response cache: each success is an independent gateway read.
        rows, evidence = (self._capture_override or capture)(key, request, op, cl, raw_path,
            max_records=len(symbols) * 15, timeout_seconds=30)
        received = instant(self.clock())
        if received < started:
            raise ContractError("Live minute receipt clock moved backwards")
        if (evidence.get("mode") != "live_intraday_replay" or evidence.get("completed") is not True
                or evidence.get("dataset") != self.dataset or evidence.get("schema") != self.schema
                or evidence.get("stype_in") != "raw_symbol" or set(evidence.get("symbols", [])) != set(symbols)
                or instant(evidence["requested_start"]) != op or instant(evidence["requested_end"]) != cl
                or not evidence.get("subscription_ack") or not evidence.get("replay_completed")
                or evidence.get("selected_quarter_records") != len(rows)
                or evidence.get("selected_quarter_native_records") != len(rows)
                or evidence.get("projection_version") != PROJECTION_VERSION
                or not started <= instant(evidence["connection_started_at"]) <= instant(evidence["finished_at"]) <= received
                or Path(evidence.get("raw_path", "")).resolve() != raw_path.resolve()
                or not raw_path.is_file()):
            raise ContractError("Primary live minute response lacks complete exact native delivery evidence")
        hasher = hashlib.sha256()
        with raw_path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
        if evidence.get("raw_sha256") != hasher.hexdigest() or evidence.get("raw_bytes") != raw_path.stat().st_size:
            raise ContractError("Primary live minute native artifact differs from its delivery hash")
        frame = pd.DataFrame(rows)
        if rows and ("symbol" not in frame or not set(frame.symbol).issubset(symbols)):
            raise ContractError("Primary live minute response includes a foreign or unmapped symbol")
        results = {}
        from .minute_persistence import _validate_symbol
        for symbol in symbols:
            raw = frame.loc[frame.symbol.eq(symbol)].copy() if rows else pd.DataFrame(
                columns=["symbol", "ts_event", "open", "high", "low", "close", "volume"])
            bars = _bars_from_databento_frame(symbol, "1m", raw)
            _validate_symbol(symbol, bars, raw, op=pd.Timestamp(op), cl=pd.Timestamp(cl))
            results[symbol] = (bars, raw)
        delivery = {"schema_version": VERSION, "attempt_id": attempt, "pc": self.pc,
            "symbols": list(symbols), "source_id": "databento:EQUS.MINI:ohlcv-1m",
            "source_dataset": self.dataset, "source_schema": self.schema,
            "requested_start": op.isoformat(), "requested_end": cl.isoformat(),
            "request_started_at": started.isoformat(), "receipt_at": received.isoformat(),
            "independent_capture": True, "response_cache_used": False,
            "raw_delivery": evidence, "observed_minutes": {s: len(v[0]) for s, v in results.items()},
            "availability_basis": "actual_independent_native_replay_receipt",
            "missing_observations": "unavailable; no market-wide no-trade inference"}
        delivery["delivery_sha256"] = hashlib.sha256(canonical(delivery)).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "receipt.json"
        with path.open("xb") as stream:
            stream.write(canonical(delivery))
            stream.flush()
            os.fsync(stream.fileno())
        self._last_delivery = {**delivery, "receipt_path": str(path),
            "receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for _, raw in results.values():
            raw.attrs["live_replay_delivery"] = deepcopy(self._last_delivery)
        return results, DatabentoAvailableRange(self.schema, op, cl)
