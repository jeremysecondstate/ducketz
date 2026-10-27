"""Scheduled optional market-context collection, independent of traders and fitting.

Current snapshots retain their actual receipt clocks. Scheduling a completed
quarter is an acquisition queue identity, never permission to backdate context.
The worker owns only its local scheduling evidence; the existing collector owns
its queue, receipts, adapters, exact entitlement blocks and per-request locks.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from filelock import FileLock, Timeout

from .market_collection import (QuarterHourMarketCollector, SourceRequest, _atomic,
    _lane, canonical, collection_root, instant, load_local_collection_config,
    snapshot_digest)

VERSION = "intraday-prospective-context-config-v1"
REQUIRED_SOURCES = ("prospective_context", "market_collection", "market_collection_adapters",
    "market_collection_schwab_stream", "market_collection_extended",
    "market_collection_fmp_commodities", "market_collection_index", "market_state")


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _save_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(canonical(value))
        handle.flush()
        os.fsync(handle.fileno())


def _save_outcome(path, value):
    sealed = {**value, "outcome_sha256": hashlib.sha256(canonical(value)).hexdigest()}
    _save_new(path, sealed)


def _verify_outcome(path, identity, slot):
    value = _read(path)
    expected = value.pop("outcome_sha256", None)
    if (expected != hashlib.sha256(canonical(value)).hexdigest()
            or value.get("binding_sha256") != identity or value.get("slot") != slot.isoformat()):
        raise ValueError("Saved context outcome identity or checksum changed")


def load_binding(path):
    value = _read(path)
    if value.get("schema_version") != VERSION:
        raise ValueError("Unsupported prospective context configuration")
    scope = value.get("symbols")
    if (not isinstance(scope, list) or not scope or len(scope) != len(set(scope))
            or any(not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", x) for x in scope)
            or value.get("pc") != "scout"):
        raise ValueError("Explicit Scout symbol ownership is required")
    if not 0 < float(value.get("poll_seconds", 30)) <= 30:
        raise ValueError("Context poll interval must be positive and at most 30 seconds")
    if not 30 <= int(value.get("collection_deadline_seconds", 180)) <= 600:
        raise ValueError("Context collection start deadline exceeds its bounded window")
    datetime.strptime(value["start_session"], "%Y-%m-%d")
    validate_binding(value)
    return value


def validate_binding(value):
    root = Path(value["source_root"]).resolve()
    if root != Path(__file__).resolve().parents[2]:
        raise ValueError("Context worker is not loaded from its bound source root")
    hashes = value.get("source_sha256", {})
    if not all("ml/intraday/" + name + ".py" in hashes for name in REQUIRED_SOURCES):
        raise ValueError("Context worker dependency binding is incomplete")
    for name, expected in hashes.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path.suffix != ".py" or _sha(path) != expected:
            raise ValueError("Context worker source binding changed")
    config_path = collection_root(value["datastore"], value["pc"]) / "configuration.json"
    if not config_path.is_file() or _sha(config_path) != value["configuration_file_sha256"]:
        raise ValueError("Existing collection configuration is missing or changed")
    config = load_local_collection_config(value["datastore"], value["pc"], value["symbols"])
    if config["configuration_sha256"] != value["configuration_sha256"]:
        raise ValueError("Existing collection source identities changed")
    return config


def normal_requests(config):
    """The established normal capture subset, using unchanged exact requests."""
    selected = [SourceRequest(**r) for r in config["requests"] if (
        r["provider"] == "schwab" and r["endpoint"] in {"stream", "quotes", "chains"}) or (
        r["provider"] == "fmp" and r["params"].get("symbol") == "BZUSD"
        and r["schema"] == "commodity-quote" and r["endpoint"] == "quote")]
    expected = {(symbol, schema) for symbol in config["symbols"]
        for schema in ("LEVELONE_EQUITIES", "CHART_EQUITY", "NYSE_BOOK", "NASDAQ_BOOK",
                       "LEVELONE_OPTIONS", "OPTIONS_BOOK", "quote", "chain-greeks-full")}
    actual = {(r.symbol, r.schema) for r in selected if r.provider == "schwab" and r.symbol != "*"}
    if actual != expected or len({r.identity for r in selected}) != len(selected):
        raise ValueError("Normal context subset lacks exact configured instrument services")
    if any(r.provider == "fmp" and r.params.get("symbol") in {"CLUSD", "NGUSD"} for r in selected):
        raise ValueError("Denied instruments cannot enter this normal context subset")
    return tuple(sorted(selected, key=lambda r: (r.endpoint == "stream", r.identity)))


def regular_slots(first, last):
    """Actual XNYS regular sessions, including holidays and early closes."""
    import exchange_calendars as xcals
    import pandas as pd
    calendar = xcals.get_calendar("XNYS", start=first - timedelta(days=7),
                                 end=last + timedelta(days=7))
    slots = []
    for label, row in calendar.schedule.iterrows():
        day = pd.Timestamp(label).date()
        if first <= day <= last:
            slot, close = instant(row["open"]), instant(row["close"])
            while slot < close:
                slots.append(slot)
                slot += timedelta(minutes=15)
    return slots


_NATIVE_STATE_READER = getattr(QuarterHourMarketCollector, "_native_state_reader",
                               QuarterHourMarketCollector._state)
_NATIVE_ATOMIC = _atomic
_JSON_SURROGATE = re.compile(r"[\ud800-\udfff]")


def _native_json_tree(value):
    """Decline the no-write optimization if canonical would normalize anything."""
    parents = [iter((value,))]
    while parents:
        try:
            current = next(parents[-1])
        except StopIteration:
            parents.pop()
            continue
        kind = type(current)
        if current is None or kind is int or kind is bool:
            continue
        if kind is str:
            if _JSON_SURROGATE.search(current):
                return False
            continue
        if kind is float:
            if not math.isfinite(current):
                return False
            continue
        if kind is dict:
            if any(type(key) is not str or _JSON_SURROGATE.search(key) for key in current):
                return False
            children = iter(current.values())
        elif kind is list:
            children = iter(current)
        else:
            return False
        # This bound only declines an optimization. Deep input keeps the exact
        # original canonical publication, including its errors and normalization.
        if len(parents) >= 64:
            return False
        parents.append(children)
    return True


def _existing_receipt(collector, key, request, start):
    folder = collector.root / "snapshots" / start.date().isoformat() / key
    paths = sorted(folder.glob("*.json"))
    if len(paths) > 32:
        raise ValueError("Context slot exceeds bounded receipt recovery inventory")
    receipts = []
    for path in paths:
        receipt = _read(path)
        if (receipt.get("snapshot_sha256") != snapshot_digest(receipt)
                or receipt.get("request_identity") != request.identity
                or receipt.get("source_id") != request.source_id
                or receipt.get("configuration_sha256") != collector.configuration_sha256):
            raise ValueError("Context receipt recovery identity changed")
        receipts.append((path, receipt))
    if receipts:
        path, receipt = max(receipts, key=lambda item: (item[1]["receipt_at"], item[1]["snapshot_id"]))
        # A process can die after the exclusive snapshot write but before either
        # derived receipt index or queue state is committed. Repair projections
        # from immutable bytes; do not reacquire and change observation time.
        from .market_collection_index import index_receipt
        index_receipt(collector.root, path, receipt)
        with collector.lock:
            state_reader = collector._state
            state = state_reader()
            task = state["tasks"][key]
            relative = str(path.relative_to(collector.root))
            native = (type(collector) is QuarterHourMarketCollector
                and getattr(state_reader, "__func__", None) is _NATIVE_STATE_READER
                and getattr(collector._state, "__func__", None) is _NATIVE_STATE_READER
                and _atomic is _NATIVE_ATOMIC
                and type(state) is dict and type(task) is dict
                and type(task.get("snapshots")) is list)
            if native:
                projection_keys_present = "status" in task and "window_finalized" in task
                relative_present = relative in task["snapshots"]
                previous_status = task.get("status")
                previous_finalized = task.get("window_finalized")
                blocks = state.get("source_blocks")
                block_present = type(blocks) is dict and request.identity in blocks
            if relative not in task["snapshots"]:
                task["snapshots"].append(relative)
                task["attempts"] += 1
            task["status"] = receipt["status"]
            task["window_finalized"] = receipt.get("window_finalized", False)
            if receipt["status"] == "NOT_AUTHORIZED":
                state.setdefault("source_blocks", {}).setdefault(request.identity,
                    {"observed_at": receipt["receipt_at"], "snapshot": relative})
            # Keep every transition and receipt-index repair. Only an exact
            # native JSON projection with no changed fields can avoid publishing
            # the entire shared queue again. Types matter: False differs from 0.
            unchanged = (native and projection_keys_present and relative_present
                and type(previous_status) is type(task["status"])
                and previous_status == task["status"]
                and type(previous_finalized) is type(task["window_finalized"])
                and previous_finalized == task["window_finalized"]
                and (receipt["status"] != "NOT_AUTHORIZED" or block_present)
                and _native_json_tree(state))
            if not unchanged:
                _atomic(collector.state_path, state)
        return receipt
    return None


def _receipt_summary(receipt):
    service = receipt.get("evidence", {}).get("service_results", {}).get(receipt["schema"], {})
    symbol = receipt["symbol"]
    def scoped(values):
        return [v for v in values if symbol == "*" or v == symbol or v.strip().startswith(symbol + " ")]
    return {"request_identity": receipt["request_identity"], "source_id": receipt["source_id"],
        "symbol": symbol, "schema": receipt["schema"], "status": receipt["status"],
        "snapshot_id": receipt["snapshot_id"], "snapshot_sha256": receipt["snapshot_sha256"],
        "receipt_at": receipt["receipt_at"], "available_at": receipt["available_at"],
        "records": len(receipt["records"]), "coverage": {name: scoped(service.get(name + "_scope", []))
            for name in ("submitted", "acknowledged", "observed", "confirmed_current")}}


class ContextWorker:
    def __init__(self, binding, *, clock=None, collector_factory=QuarterHourMarketCollector):
        self.binding = binding
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.collector_factory = collector_factory
        self.root = Path(binding["state_root"]).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.owner_lock = self.root / "worker.lock"
        self.identity = hashlib.sha256(canonical(binding)).hexdigest()
        self.root = self.root / self.identity
        self.root.mkdir(exist_ok=True)
        self.collector = None

    def _outcome(self, slot, **fields):
        return {"schema_version": "intraday-prospective-context-outcome-v1",
            "binding_sha256": self.identity, "slot": slot.isoformat(),
            "recorded_at": instant(self.clock()).isoformat(), "non_trading": True,
            "optional_context": True, "historical_backfill": False, **fields}

    def _get_collector(self, config, requests):
        if self.collector is None:
            self.collector = self.collector_factory(collection_root(self.binding["datastore"], self.binding["pc"]),
                symbols=self.binding["symbols"], requests=requests,
                configuration_sha256=config["configuration_sha256"], clock=self.clock)
        return self.collector

    def _recover_only(self, config, slot):
        requests = normal_requests(config)
        collector = self._get_collector(config, requests)
        start = slot - timedelta(minutes=15)
        collector.enqueue(start)
        results = []
        for request in requests:
            key = start.strftime("%Y%m%dT%H%M%SZ") + "-" + request.identity
            try:
                with FileLock(str(collector.root / (key + ".lock")), timeout=0):
                    receipt = _existing_receipt(collector, key, request, start)
                    results.append({**_receipt_summary(receipt), "recovered": True} if receipt else
                        {"request_identity": request.identity, "status": "INTERRUPTED_NO_RECEIPT"})
            except Timeout:
                results.append({"request_identity": request.identity, "status": "OWNED_ELSEWHERE"})
            except Exception as error:
                results.append({"request_identity": request.identity, "status": "ERROR",
                    "error_type": type(error).__name__})
        return results

    def tick(self):
        try:
            with FileLock(str(self.owner_lock), timeout=0):
                return self._tick()
        except Timeout:
            return {"status": "BUSY", "binding_sha256": self.identity}

    def _tick(self):
        config = validate_binding(self.binding)
        now = instant(self.clock())
        today = now.tz_convert("America/New_York").date()
        state_path = self.root / "cursor.json"
        state = _read(state_path) if state_path.exists() else {}
        first = datetime.strptime(state.get("next_session_date", self.binding["start_session"]), "%Y-%m-%d").date()
        last = min(today, first + timedelta(days=6))
        slots = regular_slots(first, last) if first <= last else []
        current_slot = None
        for slot in slots:
            if slot > now:
                continue
            path = self.root / "outcomes" / (slot.strftime("%Y%m%dT%H%M%SZ") + ".json")
            if path.exists():
                _verify_outcome(path, self.identity, slot)
                continue
            if now < slot + timedelta(seconds=int(self.binding.get("collection_deadline_seconds", 180))):
                current_slot = (slot, path)
            else:
                attempt = self.root / "attempts" / slot.strftime("%Y%m%dT%H%M%SZ")
                recovered = self._recover_only(config, slot) if attempt.exists() else []
                _save_outcome(path, self._outcome(slot, status="MISSED", reason=(
                    "interrupted_collection_no_completed_slot_outcome" if attempt.exists()
                    else "no_collection_started_within_window"),
                    preserved_attempt_evidence=str(attempt) if attempt.exists() else None,
                    recovered_results=recovered,
                    receipt_evidence="Existing immutable collector receipts remain at actual receipt times; no late reacquisition"))
        result = {"status": "CLOSED_OR_NO_DUE_SLOT", "observed_at": now.isoformat(),
            "binding_sha256": self.identity, "provider_calls_this_tick": 0}
        if current_slot is not None:
            slot, path = current_slot
            requests = normal_requests(config)
            collector = self._get_collector(config, requests)
            start = slot - timedelta(minutes=15)
            collector.enqueue(start)
            attempt = self.root / "attempts" / slot.strftime("%Y%m%dT%H%M%SZ")
            attempt.mkdir(parents=True, exist_ok=True)
            def perform(request):
                key = start.strftime("%Y%m%dT%H%M%SZ") + "-" + request.identity
                marker = attempt / (request.identity + ".json")
                try:
                    with FileLock(str(collector.root / (key + ".lock")), timeout=0):
                        existing = _existing_receipt(collector, key, request, start)
                        if existing is not None:
                            return {**_receipt_summary(existing), "recovered": True}
                        if marker.exists():
                            return {"request_identity": request.identity, "status": "INTERRUPTED_NO_RECEIPT"}
                        _save_new(marker, self._outcome(slot, request_identity=request.identity,
                            source_id=request.source_id, status="STARTED"))
                        with collector.lock:
                            task = collector._state()["tasks"][key]
                        with collector.provider_limits[_lane(task["request"])]:
                            receipt = collector._collect(key, task)
                        return {**_receipt_summary(receipt), "recovered": False}
                except Timeout:
                    return {"request_identity": request.identity, "status": "OWNED_ELSEWHERE"}
                except Exception as error:
                    return {"request_identity": request.identity, "status": "ERROR",
                        "error_type": type(error).__name__}
            with ThreadPoolExecutor(max_workers=4) as pool:
                receipts = list(pool.map(perform, requests))
            # Source mutation cannot invalidate raw truth; preserve it explicitly.
            source_unchanged = True
            try:
                validate_binding(self.binding)
            except (ValueError, OSError):
                source_unchanged = False
            complete = all(r["status"] == "COMPLETE" for r in receipts)
            result = self._outcome(slot, status="COMPLETE" if complete and source_unchanged else "LIMITED",
                requests=len(requests), results=receipts, source_binding_unchanged=source_unchanged,
                context_availability="Each snapshot's actual receipt/availability; no feature or training eligibility implied")
            _save_outcome(path, result)
        # Recover at most seven calendar days per tick. Today is rescanned for
        # due quarters, but exclusive outcome files prevent duplicate execution.
        cursor = min(today, last + timedelta(days=1)) if first <= last else first
        _atomic(state_path, {"next_session_date": cursor.isoformat(), "last_tick_at": now.isoformat()})
        _atomic(self.root / "health.json", result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    args = parser.parse_args(argv)
    binding = load_binding(args.config)
    # Existing credentials remain private and are consumed only by lazy adapters.
    from dotenv import load_dotenv
    load_dotenv(Path(binding["source_root"]) / ".env", override=False)
    worker = ContextWorker(binding)
    while True:
        try:
            result = worker.tick()
            print(json.dumps({k: result[k] for k in ("status", "binding_sha256", "slot", "recorded_at")
                if k in result}), flush=True)
        except Exception as error:
            result = {"status": "ERROR", "error_type": type(error).__name__,
                "observed_at": datetime.now(timezone.utc).isoformat(), "binding_sha256": worker.identity}
            _atomic(worker.root / "health.json", result)
            print(json.dumps(result), flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(float(binding.get("poll_seconds", 30)))


if __name__ == "__main__":
    raise SystemExit(main())
