"""Durable, data-only quarter-hour market context collection.

This module never loads a trader, calls an account/order endpoint, changes a
model recipe, or installs a subscription. Provider adapters are lazy and the
CLI defaults to a capability inventory until --execute is supplied.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid
from typing import Callable, Mapping, Sequence

from filelock import FileLock, Timeout

VERSION = "intraday-market-snapshot-v1"
QUEUE_VERSION = "intraday-market-collection-v1"
STEP = timedelta(minutes=15)
SUCCESS = {"COMPLETE", "EMPTY"}
TERMINAL = SUCCESS | {"UNSUPPORTED", "NOT_AUTHORIZED", "EXPIRED", "CAPACITY_BLOCKED"}
KINDS = {"stock_quote", "stock_trades", "stock_bars", "options", "fundamentals",
         "events", "cross_asset", "opra_options", "technical", "macro"}


def _settled(value):
    """Queue completion is separate from complete source coverage."""
    return value["status"] in TERMINAL or (
        value["status"] == "PARTIAL" and value.get("window_finalized") is True)


def _lane(request):
    # A bounded stream bootstrap can need eleven chains plus acknowledgements.
    # It must not own the REST quote/chain request slot for that whole interval.
    if request["provider"] == "schwab" and request.get("endpoint") == "stream":
        return "schwab_stream"
    return request["provider"]


def _select_tasks(due, maximum):
    """Reserve current price/options, background progress and oldest recovery."""
    if not due:
        return []
    market = {"stock_quote", "stock_trades", "stock_bars", "options", "opra_options"}
    ordered = sorted(due, key=lambda item: (
        item[1]["quarter_start"], item[1]["request"]["source_kind"] in market,
        -item[1].get("attempts", 0), item[0]), reverse=True)
    selected = []
    # Round-robin the independent provider lanes within each priority cohort.
    # A hash ordering can otherwise fill every worker with one slow provider.
    for quarter in sorted({t["quarter_start"] for _, t in due}, reverse=True):
        for current in (True, False):
            buckets = {}
            for item in ordered:
                request = item[1]["request"]
                if item[1]["quarter_start"] == quarter and (request["source_kind"] in market) == current:
                    buckets.setdefault(_lane(request), []).append(item)
            while any(buckets.values()):
                for bucket in buckets.values():
                    if bucket:
                        selected.append(bucket.pop(0))
    selected = selected[:maximum]
    if len(due) > maximum and maximum > 1:
        latest = max(t["quarter_start"] for _, t in due)
        background = [item for item in ordered if item[1]["quarter_start"] == latest
                      and item[1]["request"]["source_kind"] not in market and item not in selected]
        if background and maximum >= 4:
            selected[-2] = background[0]
        # Preserve an oldest recovery slot, including background-only workloads.
        remaining = [item for item in due if item not in selected[:-1]]
        selected[-1] = min(remaining, key=lambda item: (
            item[1]["quarter_start"], item[1].get("attempts", 0), item[0]))
    return selected


def instant(value) -> datetime:
    import pandas as pd
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("Collection clocks must be timezone-aware")
    return stamp.tz_convert("UTC")  # retain sub-microsecond event/cutoff precision


def _now():
    return datetime.now(timezone.utc)


def _plain(value):
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    if isinstance(value, datetime):
        import pandas as pd
        if pd.isna(value):
            return None
        stamp = pd.Timestamp(value)
        if stamp.tzinfo is None:
            return stamp.isoformat()  # raw provider wall clock remains explicitly naive
        return stamp.tz_convert("UTC").isoformat()  # preserve provider nanoseconds
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "item"):
        return _plain(value.item())
    # pandas NaT/NA and provider sentinel objects never become invented values.
    import pandas as pd
    if pd.isna(value):
        return None
    return str(value)


def canonical(value) -> bytes:
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def snapshot_digest(value):
    return _sha({k: v for k, v in value.items() if k != "snapshot_sha256"})


def configuration_digest(value):
    return _sha({k: v for k, v in value.items() if k != "configuration_sha256"})


def _atomic(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(canonical(value))
            handle.flush()
            import os
            os.fsync(handle.fileno())
        # A Windows reader can briefly deny replacement without changing the
        # destination's permissions. Retry only publication of these already
        # flushed bytes, while the caller retains its queue lock; never repeat
        # collection or expose a partially written queue.
        delays = (0.05, 0.1, 0.2, 0.4)
        for attempt in range(len(delays) + 1):
            try:
                temporary.replace(path)
                break
            except PermissionError as exc:
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == len(delays):
                    raise
                time.sleep(delays[attempt])
    except BaseException:
        # Cleanup can also meet an open Windows handle. Preserve the original
        # publication failure and its diagnostic chain if cleanup is denied.
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@dataclass(frozen=True)
class SourceRequest:
    provider: str
    source_kind: str
    symbol: str
    dataset: str
    schema: str
    endpoint: str = ""
    params: Mapping = field(default_factory=dict)
    capability: str = "UNPROBED"
    capability_reason: str = "Provider entitlement is verified by its response, not assumed from a plan name"

    def __post_init__(self):
        if self.provider not in {"databento", "schwab", "fmp"} or self.source_kind not in KINDS:
            raise ValueError("Unsupported collection provider or source kind")
        if not re.fullmatch(r"[A-Z0-9.*_/-]{1,40}", self.symbol):
            raise ValueError("Invalid collection symbol")
        if not self.schema or not self.dataset:
            raise ValueError("An exact dataset and schema identity is required")
        # Secret material must stay in provider clients, never persisted requests.
        if any(re.search(r"key|token|secret|password|authorization", str(k), re.I) for k in self.params):
            raise ValueError("Collection parameters must not contain credentials")

    @property
    def source_id(self):
        semantic = dict(self.params)
        if self.symbol != "*":
            for key in ("symbol", "symbols"):
                semantic.pop(key, None)
        # Model selectors may span this PC's symbols, but must never collapse
        # differing endpoints, periods, symbology, or context scopes.
        suffix = _sha({"endpoint": self.endpoint, "params": semantic})[:16]
        return f"{self.provider}:{self.dataset}:{self.schema}:{suffix}"

    @property
    def identity(self):
        return _sha(asdict(self))[:24]


@dataclass
class CollectionResult:
    records: list[dict]
    status: str = "COMPLETE"
    reason: str = ""
    evidence: dict = field(default_factory=dict)


class CollectionFailure(RuntimeError):
    """Safe structured failure; no authenticated URLs or raw provider errors."""
    def __init__(self, status, reason, evidence=None):
        super().__init__(reason)
        self.collection_status = status
        self.collection_reason = reason
        self.collection_evidence = evidence or {}


def _http_status(exc):
    code = getattr(getattr(exc, "response", None), "status_code", None)
    if type(code) is int and 100 <= code <= 599:
        return code
    # The existing FMP client wraps HTTP errors in RuntimeError. Recover only
    # its numeric status, never persist the payload or authenticated URL.
    matched = re.search(r"\bHTTP\s+([1-5][0-9]{2})\b", str(exc), re.I)
    return int(matched.group(1)) if matched else None


def classify_error(exc: Exception) -> tuple[str, str]:
    """Persist a category, never exception strings containing authenticated URLs."""
    if isinstance(exc, CollectionFailure):
        return exc.collection_status, exc.collection_reason
    from requests.exceptions import Timeout as RequestTimeout
    if isinstance(exc, (TimeoutError, RequestTimeout)):
        return "UNAVAILABLE", "Source did not establish complete delivery within the bounded deadline"
    code = _http_status(exc)
    message = str(exc).lower()  # inspected only; never written or printed
    if code == 429 or "429" in message or "rate limit" in message:
        return "RATE_LIMITED", "Provider rate limit; retry after bounded backoff"
    if code == 401:
        return "NOT_AUTHORIZED", "Provider rejected authentication (HTTP 401); existing credentials require attention"
    if code == 402:
        return "NOT_AUTHORIZED", "Provider denied this exact product or symbol under the current subscription (HTTP 402)"
    if code in {401, 402, 403} or any(x in message for x in ("http 401", "http 402", "http 403", "not entitled", "not authorized",
            "not licensed", "license required", "access denied", "subscription required", "requires a subscription",
            "subscription not authorized", "authentication failed")):
        return "NOT_AUTHORIZED", "Provider denied credentials or this dataset/schema entitlement"
    if "budget" in message or "record cap" in message:
        return "CAPACITY_BLOCKED", "Source response exceeded the explicit record/byte budget; this window needs a larger reviewed collector or segmented recovery"
    if any(x in message for x in ("missing required", "missing credential", "missing api", "missing token")):
        return "MISSING_CREDENTIALS", "Existing provider credentials are unavailable"
    if "live replay not supported" in message:
        return "UNSUPPORTED", "This native schema does not support replay; current stream collection is required"
    if any(x in message for x in ("not available", "availability", "data_end_after_available_end", "not yet", "invalid start time")):
        return "UNAVAILABLE", "Requested source interval is not available yet"
    return "ERROR", "Provider request failed (" + type(exc).__name__ + "); no success or coverage inferred"


class QuarterHourMarketCollector:
    """Independent resumable source queue; model success cannot gate collection."""

    def __init__(self, root: Path | str, *, symbols: Sequence[str], requests=None,
                 adapters=None, clock: Callable = _now, retry_seconds=30,
                 maximum_retry_seconds=1800, max_records=100_000, configuration_sha256=None):
        self.root = Path(root).resolve()
        self.symbols = tuple(str(x).strip().upper() for x in symbols)
        if not self.symbols or len(set(self.symbols)) != len(self.symbols):
            raise ValueError("An explicit unique local symbol scope is required")
        self.clock = clock
        self.retry_seconds, self.maximum_retry_seconds = float(retry_seconds), float(maximum_retry_seconds)
        if not (0 < self.retry_seconds <= self.maximum_retry_seconds):
            raise ValueError("Invalid collection retry bounds")
        if isinstance(max_records, bool) or not isinstance(max_records, int) or max_records < 1:
            raise ValueError("A positive record bound is required")
        self.max_records = max_records
        import threading
        self.provider_limits = {"schwab": threading.Semaphore(1), "fmp": threading.Semaphore(2),
                                "databento": threading.Semaphore(2), "schwab_stream": threading.Semaphore(1)}
        from .market_collection_adapters import default_requests, default_adapters
        self.requests = tuple(requests if requests is not None else default_requests(self.symbols))
        self.configuration_sha256 = configuration_sha256 or _sha({"symbols": self.symbols,
            "requests": [asdict(r) for r in self.requests]})
        if len({r.identity for r in self.requests}) != len(self.requests):
            raise ValueError("Duplicate provider collection identities")
        if any(r.symbol != "*" and r.symbol not in self.symbols for r in self.requests):
            raise ValueError("A request exceeds this PC's configured equity scope")
        self.adapters = adapters if adapters is not None else default_adapters(self.root, self.requests)
        self.state_path = self.root / "queue.json"
        self.lock = FileLock(str(self.root / "queue.lock"), timeout=10)
        self.root.mkdir(parents=True, exist_ok=True)
        from .market_collection_index import initialize_receipt_index
        initialize_receipt_index(self.root)
        with self.lock:
            state = self._state()
            if state.get("symbols") != list(self.symbols):
                raise ValueError("Saved collection scope belongs to another symbol configuration")

    def _state(self):
        if self.state_path.exists():
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            if value.get("schema_version") != QUEUE_VERSION:
                raise ValueError("Unsupported market collection queue version")
            return value
        return {"schema_version": QUEUE_VERSION, "symbols": list(self.symbols), "tasks": {}, "source_blocks": {}}

    def enqueue(self, quarter_start) -> int:
        start, now = instant(quarter_start), instant(self.clock())
        if start.minute % 15 or start.second or start.microsecond or start.nanosecond or start + STEP > now:
            raise ValueError("Only exact, completed quarter-hour windows may be enqueued")
        added = 0
        with self.lock:
            state = self._state()
            # Compact terminal task indexes after seven days; immutable receipts
            # and raw evidence are never deleted. Unfinished old work expires.
            lower = (now - timedelta(days=7)).isoformat()
            previous_count = len(state["tasks"])
            state["tasks"] = {k: v for k, v in state["tasks"].items()
                              if v["quarter_end"] >= lower or not _settled(v)}
            for request in self.requests:
                key = start.strftime("%Y%m%dT%H%M%SZ") + "-" + request.identity
                if key not in state["tasks"]:
                    state["tasks"][key] = {"request": asdict(request), "quarter_start": start.isoformat(),
                        "quarter_end": (start + STEP).isoformat(), "status": "QUEUED", "attempts": 0,
                        "due_at": now.isoformat(), "snapshots": []}
                    added += 1
            if added or previous_count != len(state["tasks"]):
                _atomic(self.state_path, state)
        return added

    def collect(self, reference_at=None, *, max_requests=8):
        now = instant(reference_at if reference_at is not None else self.clock())
        end = now.floor("15min")
        self.enqueue(end - STEP)
        return self.step(max_requests=max_requests)

    def run(self, stopped, pause, *, max_requests=8):
        """The host supplies cancellation and sleep; this worker has no trader dependency."""
        while not stopped():
            if collection_window_open(self.clock()):
                results = self.collect(max_requests=max_requests)
                pause(1.0 if results else 30.0)
            else:
                pause(30.0)

    def step(self, *, max_requests=8) -> list[dict]:
        if isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests < 1:
            raise ValueError("max_requests must be a positive integer")
        with self.lock:
            state = self._state()
            now = instant(self.clock())
            due = [(key, task) for key, task in state["tasks"].items()
                   if not _settled(task) and instant(task["due_at"]) <= now]
        selected = _select_tasks(due, max_requests)

        def perform(item):
            key, _ = item
            task_lock = FileLock(str(self.root / (key + ".lock")), timeout=0)
            try:
                with task_lock:
                    with self.lock:
                        task = self._state()["tasks"][key]
                    if _settled(task) or instant(task["due_at"]) > instant(self.clock()):
                        return None
                    with self.provider_limits[_lane(task["request"])]:
                        return self._collect(key, task)
            except Timeout:
                return None  # another collector owns exactly this provider/window
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(4, max_requests)) as pool:
            return [value for value in pool.map(perform, selected) if value is not None]

    def _collect(self, key, task):
        request = SourceRequest(**task["request"])
        started = instant(self.clock())
        # Recover a crash after durable receipt creation but before queue commit.
        folder = self.root / "snapshots" / instant(task["quarter_start"]).date().isoformat() / key
        for path in sorted(folder.glob("*.json"), reverse=True):
            recovered = json.loads(path.read_text(encoding="utf-8"))
            if recovered.get("snapshot_sha256") != snapshot_digest(recovered):
                raise ValueError("Orphan market receipt failed checksum validation")
            from .market_collection_index import index_receipt
            index_receipt(self.root, path, recovered)
            if _settled(recovered) and recovered.get("source_id") == request.source_id:
                with self.lock:
                    state = self._state()
                    saved = state["tasks"][key]
                    relative = str(path.relative_to(self.root))
                    if relative not in saved["snapshots"]:
                        saved["snapshots"].append(relative)
                        saved["attempts"] += 1
                    saved["status"] = recovered["status"]
                    saved["window_finalized"] = recovered.get("window_finalized", False)
                    _atomic(self.state_path, state)
                return recovered
        with self.lock:
            blocked = self._state().get("source_blocks", {}).get(request.identity)
        if started - instant(task["quarter_end"]) > timedelta(hours=48):
            result = CollectionResult([], "EXPIRED", "Unfinished source window exceeded the bounded 48-hour recovery horizon")
        elif blocked:
            result = CollectionResult([], "NOT_AUTHORIZED", "Earlier request denied this exact source configuration; explicit reprobe required")
        elif request.capability in {"UNSUPPORTED", "NOT_AUTHORIZED"}:
            result = CollectionResult([], request.capability, request.capability_reason)
        elif request.provider not in self.adapters:
            result = CollectionResult([], "UNPROBED", "No adapter was installed for this provider")
        else:
            try:
                result = self.adapters[request.provider](request, instant(task["quarter_start"]),
                    instant(task["quarter_end"]), self.max_records)
                if not isinstance(result, CollectionResult):
                    raise TypeError("Provider adapter must return CollectionResult")
            except Exception as exc:
                status, reason = classify_error(exc)
                evidence = dict(getattr(exc, "collection_evidence", {}))
                code = _http_status(exc)
                if type(code) is int and 100 <= code <= 599:
                    evidence["http_status"] = code
                result = CollectionResult([], status, reason, evidence)
        receipt = instant(self.clock())
        if receipt < started:
            raise ValueError("Collection clock moved backwards")
        if len(result.records) > self.max_records:
            result = CollectionResult(result.records[:self.max_records], "PARTIAL", "Record bound reached", result.evidence)
        # Exact duplicates are removed; differing rows at the same event clock
        # remain evidence. No arbitrary last-write resolution or OHLC fill.
        records, seen = [], set()
        for raw in result.records:
            row = _plain(raw)
            signature = _sha(row)
            if signature not in seen:
                row["receipt_at"] = receipt.isoformat()
                if row.get("publication_time_basis") == "receipt_only":
                    row["published_at"] = receipt.isoformat()
                    row["event_at"] = receipt.isoformat()
                clocks = [receipt]
                # A malformed future market event is a field-quality problem,
                # not proof that an already-received response arrives later.
                # Preserve its original timestamp for per-field admission.
                event_availability = request.source_kind in {"fundamentals", "events", "macro", "technical"}
                for name in ("available_at", "published_at", *(("event_at",) if event_availability else ())):
                    if row.get(name) is not None:
                        try:
                            clocks.append(instant(row[name]))
                        except (ValueError, TypeError):
                            row[name + "_invalid"] = True
                row["available_at"] = max(clocks).isoformat()
                records.append(row)
                seen.add(signature)
        status = "EMPTY" if result.status == "COMPLETE" and not records else result.status
        allowed = TERMINAL | {"UNPROBED", "PARTIAL", "ERROR", "RATE_LIMITED", "MISSING_CREDENTIALS", "UNAVAILABLE"}
        if status not in allowed:
            raise ValueError("Unknown provider result status")
        available = max([receipt, *(instant(r["available_at"]) for r in records)])
        snapshot = {"schema_version": VERSION, "snapshot_id": uuid.uuid4().hex,
            "provider": request.provider, "dataset": request.dataset, "schema": request.schema,
            "source_id": request.source_id, "source_kind": request.source_kind, "symbol": request.symbol,
            "request_identity": request.identity, "configuration_sha256": self.configuration_sha256,
            "quarter_start": task["quarter_start"], "quarter_end": task["quarter_end"],
            "requested_start": task["quarter_start"], "requested_end": task["quarter_end"],
            "request_started_at": started.isoformat(), "receipt_at": receipt.isoformat(),
            "available_at": available.isoformat(), "status": status, "reason": result.reason,
            "records": records, "record_count": len(records),
            "window_finalized": bool(request.provider == "schwab" and request.endpoint == "stream"
                and result.evidence.get("mode") == "current_stream_snapshot"
                and result.evidence.get("completed") is True
                or request.provider == "fmp"
                and result.evidence.get("mode") == "bounded_current_symbol_research"
                and result.evidence.get("bounded_response_complete") is True),
            "exact_duplicates_removed": len(result.records) - len(records),
            "coverage_claim": "Request delivery only; no claim of market-wide observation completeness",
            "evidence": _plain(result.evidence)}
        snapshot["snapshot_sha256"] = _sha(snapshot)
        folder = self.root / "snapshots" / task["quarter_start"][:10] / key
        path = folder / (snapshot["snapshot_id"] + ".json")
        folder.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(canonical(snapshot))
            handle.flush()
            import os
            os.fsync(handle.fileno())
        from .market_collection_index import index_receipt
        index_receipt(self.root, path, snapshot)
        with self.lock:
            state = self._state()
            saved = state["tasks"][key]
            saved["attempts"] += 1
            saved["status"] = status
            saved["window_finalized"] = snapshot["window_finalized"]
            saved["snapshots"].append(str(path.relative_to(self.root)))
            if status == "NOT_AUTHORIZED":
                state.setdefault("source_blocks", {})[request.identity] = {"observed_at": receipt.isoformat(),
                    "snapshot": str(path.relative_to(self.root))}
            delay = min(self.maximum_retry_seconds, self.retry_seconds * 2 ** min(saved["attempts"] - 1, 16))
            saved["due_at"] = (receipt + timedelta(seconds=delay)).isoformat()
            _atomic(self.state_path, state)
        return snapshot

    def capability_manifest(self):
        from .market_collection_adapters import capability_catalog
        with self.lock:
            tasks = self._state()["tasks"]
        latest = {}
        for task in tasks.values():
            request = SourceRequest(**task["request"])
            previous = latest.get(request.identity)
            if previous is None or task["quarter_start"] > previous["quarter_start"]:
                latest[request.identity] = task
        return {"schema_version": QUEUE_VERSION, "observed_at": instant(self.clock()).isoformat(),
            "symbols": list(self.symbols), "sources": [{**asdict(r), "source_id": r.source_id,
                "observed_status": latest.get(r.identity, {}).get("status", r.capability),
                "attempts": latest.get(r.identity, {}).get("attempts", 0)} for r in self.requests],
            "catalog": capability_catalog(),
            "claim": "Configured capabilities; UNPROBED is not an entitlement or successful collection claim"}


def load_snapshots(root: Path | str, *, symbol: str, cutoff, pc=None, since=None,
                   decision_cutoffs=None, source_ids=None, lazy=False) -> tuple[dict, ...]:
    """Read-only indexed immutable receipts, including latest failure statuses.

    Decision/source selectors return only the union of latest causal receipts
    needed for those decisions, preserving same-clock conflicts. Existing small
    legacy fixtures have a bounded fallback; large legacy stores require an
    explicit receipt-index migration, never an unbounded model-time file scan.
    """
    root, upper = Path(root), instant(cutoff)
    if pc is not None:
        root = collection_root(root, pc)
    lower = instant(since) if since is not None else upper - timedelta(days=7)
    if lower > upper:
        raise ValueError("Snapshot lower bound exceeds cutoff")
    from .market_collection_index import selected_receipt_entries, read_indexed_snapshots, legacy_snapshots
    selected = selected_receipt_entries(root, symbol=symbol, cutoff=upper, since=lower,
        decision_cutoffs=decision_cutoffs, source_ids=source_ids)
    if selected is None:
        return legacy_snapshots(root, symbol=symbol, cutoff=upper, since=lower,
            decision_cutoffs=decision_cutoffs, source_ids=source_ids)
    return read_indexed_snapshots(root, selected, lazy=lazy)


def collection_root(datastore, pc):
    pc = str(pc).strip().lower()
    if pc not in {"scout", "atlas"}:
        raise ValueError("An explicit Scout or Atlas collection identity is required")
    return Path(datastore) / "intraday" / pc / "market-data"


def collection_window_open(reference_at):
    """Calendar gate for automatic looping only; --once remains an explicit probe."""
    import exchange_calendars as xcals
    import pandas as pd
    stamp = pd.Timestamp(instant(reference_at)) - pd.Timedelta(seconds=1)
    eastern = stamp.tz_convert("America/New_York")
    equity = xcals.get_calendar("XNYS")
    if equity.is_session(eastern.date().isoformat()) and 4 <= eastern.hour < 20:
        return True
    cme = xcals.get_calendar("CMES")
    return bool(cme.is_open_on_minute(stamp.floor("min"), ignore_breaks=False))


def load_local_collection_config(datastore, pc, symbols):
    """Read/freeze local sources without touching a provider or model recipe."""
    from .market_collection_adapters import default_requests
    root = collection_root(datastore, pc)
    path = root / "configuration.json"
    scope = [str(s).upper() for s in symbols]
    if path.exists():
        config = json.loads(path.read_text(encoding="utf-8"))
        if config.get("symbols") != scope or config.get("pc") != str(pc).lower():
            raise ValueError("Collection configuration does not match this PC and symbols")
        if config.get("schema_version") != QUEUE_VERSION:
            raise ValueError("Unsupported frozen collection configuration")
        if config.get("configuration_sha256") != configuration_digest(config):
            raise ValueError("Frozen collection configuration checksum mismatch")
        requests = [SourceRequest(**r) for r in config["requests"]]
        if config.get("source_ids") != [r.source_id for r in requests]:
            raise ValueError("Collection source identities do not match frozen requests")
        return config
    requests = default_requests(scope)
    config = {"schema_version": QUEUE_VERSION, "pc": str(pc).lower(), "symbols": scope,
              "requests": [asdict(r) for r in requests], "source_ids": [r.source_id for r in requests]}
    config["configuration_sha256"] = configuration_digest(config)
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "configuration.lock"), timeout=10):
        if path.exists():
            return load_local_collection_config(datastore, pc, symbols)
        _atomic(path, config)
    return config


def collector_from_config(config, datastore, pc, **kwargs):
    if config.get("pc") != str(pc).lower():
        raise ValueError("Collection configuration belongs to another PC")
    if config.get("configuration_sha256") != configuration_digest(config):
        raise ValueError("Frozen collection configuration checksum mismatch")
    return QuarterHourMarketCollector(collection_root(datastore, pc), symbols=config["symbols"],
        requests=[SourceRequest(**r) for r in config["requests"]],
        configuration_sha256=config["configuration_sha256"], **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--quarter-start")
    parser.add_argument("--max-requests", type=int, default=8)
    parser.add_argument("--execute", action="store_true", help="Perform bounded market-data requests only")
    parser.add_argument("--once", action="store_true", help="Collect one bounded batch for the latest completed quarter")
    parser.add_argument("--run", action="store_true", help="Continue collecting quarters until interrupted")
    parser.add_argument("--pc", choices=("scout", "atlas"), required=True)
    args = parser.parse_args(argv)
    from dotenv import load_dotenv
    load_dotenv()
    config = load_local_collection_config(args.datastore, args.pc, args.symbols)
    collector = collector_from_config(config, args.datastore, args.pc)
    if args.run:
        import time
        collector.run(lambda: False, time.sleep, max_requests=args.max_requests)
    elif args.execute or args.once:
        if args.quarter_start:
            collector.enqueue(args.quarter_start)
            results = collector.step(max_requests=args.max_requests)
        else:
            results = collector.collect(max_requests=args.max_requests)
        print(json.dumps({"attempted": len(results), "statuses": [r["status"] for r in results]}))
    else:
        print(json.dumps(collector.capability_manifest(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
