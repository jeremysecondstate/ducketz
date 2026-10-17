"""One owner for current Schwab market streams; no trader or account actions.

Protocol/field-map lead: Ducketz peer commit
28ef18efe477c43375a363dc436f5780ea1a1494, market_collection_schwab_stream.py.
This adapter keeps Atlas receipt semantics; it imports no peer recipe or queue.
Network construction happens only in the daemon started explicitly by start().
"""
from __future__ import annotations

from collections import deque
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from statistics import median
import threading
import time
from urllib.parse import urlparse
import uuid

VERSION = "intraday-schwab-continuous-inputs-v1"
UTC = timezone.utc
FIELDS = {"LEVELONE_EQUITIES": 52, "LEVELONE_OPTIONS": 56,
          "LEVELONE_FUTURES": 41, "CHART_EQUITY": 9, "CHART_FUTURES": 7,
          "NYSE_BOOK": 4, "NASDAQ_BOOK": 4, "OPTIONS_BOOK": 4}
BOOKS = {"NYSE_BOOK", "NASDAQ_BOOK", "OPTIONS_BOOK"}
ROOTS = ("ES", "NQ", "RTY", "CL", "GC", "ZN", "6E")
QUOTE_FIELDS = ("spread_relative", "quote_imbalance", "last", "day_volume")
BAR_FIELDS = ("return", "range_relative", "volume", "observed_bars")
BOOK_FIELDS = ("spread_relative", "depth_imbalance", "bid_depth", "ask_depth", "bid_levels", "ask_levels")
OPTION_FIELDS = ("iv", "delta", "gamma", "theta", "vega", "rho", "contracts", "requested_contracts", "acknowledged_contracts")
ADDITIONAL_SOURCE_FIELDS = {
    "schwab_stream_quote": QUOTE_FIELDS, "schwab_stream_bars": BAR_FIELDS,
    "schwab_stream_nyse_book": BOOK_FIELDS, "schwab_stream_nasdaq_book": BOOK_FIELDS,
    "schwab_stream_options": OPTION_FIELDS,
    "schwab_stream_options_book": (*BOOK_FIELDS, "contracts", "requested_contracts", "acknowledged_contracts"),
}
for _root in ROOTS:
    ADDITIONAL_SOURCE_FIELDS["schwab_future_" + _root.lower() + "_quote"] = QUOTE_FIELDS
    ADDITIONAL_SOURCE_FIELDS["schwab_future_" + _root.lower() + "_bars"] = BAR_FIELDS
ADDITIONAL_SOURCE_TTL_SECONDS = {name: 1800 for name in ADDITIONAL_SOURCE_FIELDS}
MARKERS = ("synthetic", "is_synthetic", "no_trade", "is_no_trade", "source_empty", "is_source_empty")
# Conservative local envelope, not an assertion about Schwab account limits.
CONNECTION_KEY_BUDGET = 500
OPTION_SERVICES = ("LEVELONE_OPTIONS", "OPTIONS_BOOK")


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("An aware receipt clock is required")
    return value.astimezone(UTC)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _ms(value):
    number = _number(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number / 1000, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _genuine(row):
    return isinstance(row, dict) and all(name not in row or row[name] is False for name in MARKERS)


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()
                if not re.search(r"api.?key|access.?token|refresh.?token|authorization|password|secret", str(k), re.I)}
    if isinstance(value, (tuple, list)):
        return [_clean(v) for v in value]
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str):
        return re.sub(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)=([^&\s\"<>]+)", r"\1=[REDACTED]", value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _bytes(value):
    return json.dumps(_clean(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _ratio(a, b):
    return a / b if a is not None and b is not None and b != 0 else None


def _quote(row):
    bid, ask = row.get("bid"), row.get("ask")
    spread = (ask - bid) / ((ask + bid) / 2) if bid is not None and ask is not None and 0 < bid <= ask else None
    a, b = row.get("bid_size"), row.get("ask_size")
    imbalance = _ratio(a - b, a + b) if a is not None and b is not None and min(a, b) >= 0 else None
    return {"spread_relative": spread, "quote_imbalance": imbalance,
            "last": row.get("last"), "day_volume": row.get("day_volume")}


def normalize(service, native, *, received_at, scope):
    """One genuine message delta; no old fields are merged into a new event."""
    if service not in FIELDS or not _genuine(native) or native.get("delayed") is True:
        return None
    key = str(native.get("key", native.get("0", ""))).strip()
    if key not in scope:
        return None
    row = {"symbol": scope[key] or key, "provider_symbol": key, "service": service,
           "received_at": _utc(received_at), "event_at": None}
    if service in BOOKS:
        row["event_at"] = _ms(native.get("1"))
        for side, field in (("bid", "2"), ("ask", "3")):
            levels = native.get(field)
            if levels is not None and not isinstance(levels, list):
                return None
            usable = []
            for level in levels or []:
                if not _genuine(level):
                    continue
                price, size = _number(level.get("0")), _number(level.get("1"))
                if price is not None and price > 0 and size is not None and size >= 0:
                    usable.append((price, size))
            if usable:
                row[side] = (max if side == "bid" else min)(p for p, _ in usable)
                row[side + "_depth"] = sum(s for _, s in usable)
                row[side + "_levels"] = len(usable)
    elif service.startswith("CHART_"):
        row.update({name: _number(native.get(str(field))) for name, field in
                    (("open", 2), ("high", 3), ("low", 4), ("close", 5), ("volume", 6))})
        row["event_at"] = _ms(native.get("7" if service == "CHART_EQUITY" else "1"))
        if row["event_at"] is not None:
            row["bar_end_at"] = row["event_at"] + timedelta(minutes=1)
        o, h, low, c, volume = (row[k] for k in ("open", "high", "low", "close", "volume"))
        if any(v is None for v in (o, h, low, c, volume)) or min(o, h, low, c) <= 0 or volume <= 0 or not low <= min(o, c) <= max(o, c) <= h:
            return None
    else:
        option, future = service == "LEVELONE_OPTIONS", service == "LEVELONE_FUTURES"
        mapping = {"bid": 2, "ask": 3, "last": 4, "bid_size": 16, "ask_size": 17} if option else {
            "bid": 1, "ask": 2, "last": 3, "bid_size": 4, "ask_size": 5}
        row.update({name: _number(native.get(str(field))) for name, field in mapping.items()})
        row["event_at"] = _ms(native.get("38" if option else "10" if future else "34")) or _ms(native.get("39" if option else "11" if future else "35"))
        clocks = [_ms(native.get("38" if option else "10" if future else "34")),
                  _ms(native.get("39" if option else "11" if future else "35"))]
        if any(stamp is not None and stamp > row["received_at"] for stamp in clocks):
            return None
        row["day_volume"] = _number(native.get("8"))
        if row.get("day_volume") is not None and row["day_volume"] < 0:
            row["day_volume"] = None
        if row.get("last") is not None and row["last"] <= 0:
            row["last"] = None
        if option:
            row.update({name: _number(native.get(str(field))) for name, field in
                        (("delta", 28), ("gamma", 29), ("theta", 30), ("vega", 31), ("rho", 32))})
            for name in ("delta", "gamma", "theta", "vega", "rho"):
                if row[name] == -999:
                    row[name] = None
            vol = _number(native.get("10"))
            row["iv"] = vol / 100 if vol is not None and vol >= 0 else None
        if future:
            row["active_contract"] = native.get("34")
    if row["event_at"] is None or row["event_at"] > row["received_at"]:
        return None
    if row.get("bar_end_at", row["event_at"]) > row["received_at"]:
        return None  # A partial chart message is never made final by waiting.
    if "OPTIONS" in service:
        row["contract_symbol"] = key
    if "FUTURES" in service:
        match = re.fullmatch(r"/(ES|NQ|RTY|CL|GC|ZN|6E)(?:[FGHJKMNQUVXZ]\d{1,4})?", key)
        if not match:
            return None
        row["root"] = match.group(1)
        active = row.get("active_contract")
        if active and not re.fullmatch("/" + row["root"] + r"[FGHJKMNQUVXZ]\d{1,4}", str(active)):
            return None
    return row


class SchwabMarketStream:
    """One asynchronous, market-only connection owned by an existing collector.

    ``root`` is the private market-input root. Raw JSONL prefixes are flushed
    before projection; snapshot/close seal them and record immutable full hashes.
    An injected persist_raw(payload, metadata) callback may replace this journal.
    Neither start nor snapshot waits on provider bootstrap or subscriptions.
    """
    def __init__(self, root, symbols, futures=(), *, session_factory=None, http_get=None,
                 socket_factory=None, clock=None, monotonic=None, persist_raw=None,
                 max_option_contracts=1000, chain_refresh_seconds=900, reconnect_seconds=30):
        self.root = Path(root) / "schwab-stream"
        self.symbols = tuple(dict.fromkeys(str(s).strip().upper() for s in symbols))
        if not self.symbols or any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", s) for s in self.symbols):
            raise ValueError("Explicit configured equity symbols required")
        self.futures = tuple(dict.fromkeys(futures))
        if any(not re.fullmatch(r"/(ES|NQ|RTY|CL|GC|ZN|6E)(?:[FGHJKMNQUVXZ]\d{1,4})?", s) for s in self.futures):
            raise ValueError("Explicit supported Schwab native futures symbols required")
        roots = [re.match(r"/(ES|NQ|RTY|CL|GC|ZN|6E)", s).group(1) for s in self.futures]
        if len(set(roots)) != len(roots):
            raise ValueError("Only one explicitly selected native scope per futures root")
        if type(max_option_contracts) is not int or not 1 <= max_option_contracts <= 1000:
            raise ValueError("A bounded option subscription scope is required")
        if chain_refresh_seconds < 60 or reconnect_seconds <= 0:
            raise ValueError("Positive bounded refresh and retry intervals required")
        self.session_factory, self.http_get, self.socket_factory = session_factory, http_get, socket_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.persist_raw = persist_raw
        self.max_option_contracts = max_option_contracts
        self.chain_refresh_seconds, self.reconnect_seconds = chain_refresh_seconds, reconnect_seconds
        self._lock, self._stop = threading.RLock(), threading.Event()
        self._thread, self._socket, self._session = None, None, None
        self._chain_thread = None
        self._chain_generation = 0
        self._chain_round_slot = None
        self._chain_round_pending = deque()
        self._chain_attempts = {}
        self._completed_chains = {}
        self._started_at = None
        self._status, self._connection = "not_started", None
        self._scopes, self._acks, self._pending = {}, {}, {}
        self._ack_history = {}
        self._sensitive = set()
        self._rows, self._future_ids = {}, {}
        self._chains, self._chain_times, self._chain_refs = {}, {}, {}
        self._chain_details = {}
        self._scope_counts = {}
        self._capacity_limits = {}
        self._subscription_attempts = {}
        self._sequence = 0
        self._journal = None
        self._segments = {}
        self._callback_refs = deque(maxlen=256)
        self._diagnostics = deque(maxlen=100)
        self._gaps = deque(maxlen=50)
        self._gap_started_at = None
        self._native_counts, self._delayed_counts = {}, {}
        self._last_raw_by_service = {}
        self._rejected = 0

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return {"status": self._status, "owner": "existing", "version": VERSION}
            if self._stop.is_set():
                return {"status": "closed", "owner": "stopped", "version": VERSION}
            self._started_at = _utc(self.clock())
            self._status = "starting"
            self._thread = threading.Thread(target=self._run, name="ducketz-schwab-market", daemon=True)
            self._chain_thread = threading.Thread(target=self._chain_loop, name="ducketz-schwab-chain", daemon=True)
            self._thread.start()
            self._chain_thread.start()
            return {"status": "starting", "owner": "new", "version": VERSION}

    def close(self):
        self._stop.set()
        socket = self._socket
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        if self._chain_thread is not None and self._chain_thread is not threading.current_thread():
            self._chain_thread.join(timeout=.1)
        with self._lock:
            self._seal()
            self._status = "closed"

    def _diagnostic(self, reason, **extra):
        with self._lock:
            self._diagnostics.append({"at": _utc(self.clock()).isoformat(), "reason": reason, **extra})

    def _seal(self):
        import os
        current = self._journal
        if current is None:
            return
        current["handle"].flush()
        os.fsync(current["handle"].fileno())
        current["handle"].close()
        item = {k: current[k] for k in ("path", "bytes", "messages", "first_received_at", "last_received_at")}
        item["sha256"] = current["digest"].hexdigest()
        with Path(current["path"]).with_suffix(".manifest.json").open("xb") as handle:
            handle.write(_bytes(item))
        self._segments[current["id"]] = item
        self._journal = None

    def _persist(self, payload, *, kind, received, request=None):
        """Called under lock and before any payload fields become features."""
        import os
        metadata = {"version": VERSION, "kind": kind, "received_at": received.isoformat(),
                    "connection_id": self._connection, "request": request or {}}
        def redact(value):
            if isinstance(value, dict):
                return {k: redact(v) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(v) for v in value]
            if isinstance(value, str):
                for secret in sorted(self._sensitive, key=len, reverse=True):
                    value = value.replace(secret, "[REDACTED]")
            return value
        payload = redact(_clean(payload))
        if self.persist_raw is not None:
            result = self.persist_raw(payload, metadata)
            self._callback_refs.append(result)
            return {"callback_receipt": result}
        line = _bytes({"metadata": metadata, "payload": payload}) + b"\n"
        if len(line) > 64 * 1024 * 1024:
            raise ValueError("Market payload exceeds explicit byte bound")
        if self._journal and (self._journal["bytes"] + len(line) > 8 * 1024 * 1024 or
                received - _utc(self._journal["first_received_at"]) >= timedelta(seconds=60)):
            self._seal()
        if self._journal is None:
            identity = uuid.uuid4().hex
            folder = self.root / "raw" / received.strftime("%Y/%m/%d")
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (identity + ".jsonl")
            self._journal = {"id": identity, "path": str(path), "handle": path.open("xb"),
                             "digest": hashlib.sha256(), "bytes": 0, "messages": 0,
                             "first_received_at": received.isoformat(), "last_received_at": received.isoformat()}
        current = self._journal
        current["handle"].write(line)
        current["handle"].flush()
        os.fsync(current["handle"].fileno())
        current["digest"].update(line)
        current["bytes"] += len(line)
        current["messages"] += 1
        current["last_received_at"] = received.isoformat()
        return {"segment_id": current["id"], "message_sha256": hashlib.sha256(line).hexdigest()}

    def _http(self, endpoint, params=None, *, trader=False):
        if self._stop.is_set():
            raise InterruptedError("Owner stopped")
        import requests
        from app.services.schwab import MARKETDATA_BASE_URL, TRADER_BASE_URL
        headers = self._session._headers()
        if self._stop.is_set():
            raise InterruptedError("Owner stopped")
        response = (self.http_get or requests.get)((TRADER_BASE_URL if trader else MARKETDATA_BASE_URL) + "/" + endpoint,
                    headers=headers, params=params, timeout=10)
        response.raise_for_status()
        return response.json()

    def _refresh_one_chain(self):
        """One attempt in a coalesced round; failures count toward completion."""
        now = _utc(self.clock())
        with self._lock:
            slot = int(now.timestamp() // self.chain_refresh_seconds)
            if not self._chain_round_pending:
                if self._chain_round_slot == slot:
                    return False
                self._chain_round_slot = slot
                self._chain_round_pending.extend(self.symbols)
                self._chain_attempts = {}
            symbol = self._chain_round_pending.popleft()
            self._chain_times[symbol] = now
            self._chain_attempts[symbol] = {"started_at": now.isoformat(), "status": "pending"}
        outcome = "unavailable"
        params = {"symbol": symbol, "contractType": "ALL", "includeUnderlyingQuote": "true", "strategy": "SINGLE", "range": "ALL"}
        try:
            raw = self._http("chains", params)
            received = _utc(self.clock())
            with self._lock:
                ref = self._persist(raw, kind="option_chain", received=received, request={"endpoint": "chains", "params": params})
                self._chain_refs[symbol] = ref
            if self._stop.is_set():
                with self._lock:
                    self._seal()
                return False
            if not _genuine(raw) or raw.get("status") != "SUCCESS" or raw.get("symbol") != symbol:
                raise ValueError("Invalid chain identity/status/marker")
            keys, examined = [], 0
            for side in ("callExpDateMap", "putExpDateMap"):
                if not isinstance(raw.get(side), dict):
                    raise ValueError("Missing chain map")
                for strikes in raw[side].values():
                    for contracts in strikes.values():
                        for row in contracts:
                            examined += 1
                            key = row.get("symbol") if _genuine(row) else None
                            if isinstance(key, str) and re.fullmatch(re.escape(symbol) + r"\s*\d{6}[CP]\d{8}", key):
                                keys.append(key)
            with self._lock:
                self._chains[symbol] = sorted(set(keys))
                self._chain_details[symbol] = {"provider_contract_rows": examined,
                    "recognized_native_keys": len(set(keys)), "excluded_or_duplicate_rows": examined - len(set(keys)),
                    "received_at": received.isoformat(), "scope_rule": "matching underlying OCC-format native keys; full original response retained"}
            outcome = "ok"
            return True
        except InterruptedError:
            raise
        except Exception as exc:
            outcome = type(exc).__name__
            self._diagnostic("chain_unavailable", symbol=symbol, error_type=type(exc).__name__)
            return False
        finally:
            with self._lock:
                self._chain_attempts[symbol].update(status=outcome, completed_at=_utc(self.clock()).isoformat())
                if not self._chain_round_pending and not self._stop.is_set():
                    self._completed_chains = copy.deepcopy(self._chains)
                    self._chain_generation += 1

    def _chain_loop(self):
        # A slow REST response must not stop receipt of continuous market data.
        while not self._stop.is_set():
            if self._status == "connected":
                try:
                    self._refresh_one_chain()
                except InterruptedError:
                    return
                except Exception as exc:
                    self._diagnostic("chain_worker_unavailable", error_type=type(exc).__name__)
            self._stop.wait(.25)

    def _option_budget(self):
        # Each service-key membership consumes the local envelope, even when
        # the same native option is requested from both quote and book services.
        base = 4 * len(self.symbols) + 2 * len(self.futures)
        total = min(self.max_option_contracts, max(0, CONNECTION_KEY_BUDGET - base))
        return {"combined_option_limit": total, "base_service_memberships": base,
                "connection_key_envelope": CONNECTION_KEY_BUDGET,
                "service_limits": {"LEVELONE_OPTIONS": (total + 1) // 2, "OPTIONS_BOOK": total // 2},
                "scope": "conservative local budget; actual entitlement established only by service ACKs"}

    def _option_scope(self, service="LEVELONE_OPTIONS"):
        groups = [self._completed_chains.get(symbol, []) for symbol in self.symbols]
        quarter = int(_utc(self.clock()).timestamp() // 900)
        rotated = [g[(quarter * 250) % len(g):] + g[:(quarter * 250) % len(g)] if g else [] for g in groups]
        order = self.symbols[quarter % len(self.symbols):] + self.symbols[:quarter % len(self.symbols)]
        by_symbol = dict(zip(self.symbols, rotated))
        all_keys = [(keys[index], symbol) for index in range(max([len(g) for g in rotated] or [0]))
                    for symbol in order for keys in [by_symbol[symbol]] if index < len(keys)]
        budget = min(self._option_budget()["service_limits"][service], self._capacity_limits.get(service, 1000))
        chosen = dict(all_keys[:budget])
        for symbol in self.symbols:
            counts = self._scope_counts.setdefault(symbol, {})
            counts["requested"] = len(self._completed_chains.get(symbol, []))
            counts.setdefault("submitted_by_service", {})[service] = sum(v == symbol for v in chosen.values())
            counts["submitted"] = sum(counts["submitted_by_service"].values())
        return chosen

    def _sync_option_subscriptions(self, last_generation):
        with self._lock:
            generation = self._chain_generation
            if generation == 0 or generation == last_generation:
                return last_generation
            scopes = {service: self._option_scope(service) for service in OPTION_SERVICES}
        for service, scope in scopes.items():
            if scope != self._scopes.get(service, {}):
                if scope:
                    self._subscribe_options(service, scope)
                elif self._scopes.get(service):
                    identity = self._send(service, "UNSUBS", {"keys": ",".join(self._scopes[service])})
                    self._await(identity)
                    self._scopes[service] = {}
                    self._acks[service] = {"status": "unavailable", "requested": 0, "keys": []}
        return generation

    def _subscribe_options(self, service, scope):
        """At most three scopes after code19; denial is never an endless retry."""
        quarter = int(_utc(self.clock()).timestamp() // 900)
        order = self.symbols[quarter % len(self.symbols):] + self.symbols[:quarter % len(self.symbols)]
        first = {next(key for key, owner in scope.items() if owner == symbol): symbol
                 for symbol in order if symbol in scope.values()}
        alternatives = [scope, first, dict(list(first.items())[:1])]
        previous_size = None
        for allocated in alternatives:
            if previous_size is not None and len(allocated) >= previous_size:
                continue
            previous_size = len(allocated)
            with self._lock:
                request_ref = self._persist({"service": service, "native_scope": list(allocated),
                    "original_allocation": len(scope), "reason": "initial_or_bounded_code19_fallback"},
                    kind="option_subscription_scope", received=_utc(self.clock()))
            self._subscribe(service, allocated)
            with self._lock:
                ack = copy.deepcopy(self._acks.get(service, {}))
                self._subscription_attempts.setdefault(service, deque(maxlen=12)).append({
                    "allocated": len(allocated), "status": ack.get("status"), "codes": ack.get("codes", []),
                    "acknowledged": len(ack.get("keys", [])), "request_raw_ref": request_ref,
                    "ack_raw_ref": ack.get("raw_ref"), "at": _utc(self.clock()).isoformat()})
                for symbol in self.symbols:
                    counts = self._scope_counts.setdefault(symbol, {})
                    counts.setdefault("submitted_by_service", {})[service] = sum(owner == symbol for owner in allocated.values())
                    counts["submitted"] = sum(counts["submitted_by_service"].values())
            if ack.get("status") == "ok":
                if len(allocated) < len(scope):
                    self._capacity_limits[service] = len(allocated)
                return
            if ack.get("status") != "capacity_blocked" or self._stop.is_set():
                return
        # Keep the smallest attempted scope for this connection, even if it was
        # rejected. A scheduled new scope may be tried; no immediate retry loop.
        self._capacity_limits[service] = previous_size or 0

    def _send(self, service, command, parameters):
        if self._stop.is_set():
            raise InterruptedError("Owner stopped")
        self._sequence += 1
        identity = str(self._sequence)
        self._pending[identity] = (service, command, parameters.get("keys", "").split(",") if parameters.get("keys") else [])
        self._socket.send(json.dumps({"requests": [{"service": service, "requestid": identity,
            "command": command, "SchwabClientCustomerId": self._info["schwabClientCustomerId"],
            "SchwabClientCorrelId": self._info["schwabClientCorrelId"], "parameters": parameters}]}))
        return identity

    def _subscribe(self, service, scope):
        if not scope:
            return
        self._scopes[service] = scope
        self._acks[service] = {"status": "pending", "requested": len(scope), "keys": [], "codes": []}
        keys = list(scope)
        for offset in range(0, len(keys), 250):
            identity = self._send(service, "SUBS" if offset == 0 else "ADD",
                                  {"keys": ",".join(keys[offset:offset + 250]), "fields": ",".join(map(str, range(FIELDS[service])))})
            self._await(identity)
            if self._acks[service]["status"] != "ok":
                break

    def _await(self, identity):
        deadline = self.monotonic() + 10
        while identity in self._pending:
            if self._stop.is_set():
                raise InterruptedError("Owner stopped")
            if self.monotonic() >= deadline:
                raise TimeoutError("Subscription ACK deadline")
            self._receive()

    def _receive(self):
        if self._stop.is_set():
            raise InterruptedError("Owner stopped")
        import websocket
        try:
            message = self._socket.recv()
        except (TimeoutError, websocket.WebSocketTimeoutException):
            return
        if not message:
            raise ConnectionError("Market stream disconnected")
        if len(message) > 64 * 1024 * 1024:
            raise ValueError("Market message exceeds explicit bound")
        try:
            value = json.loads(message)
        except (ValueError, UnicodeError):
            raw = message.decode("utf-8", errors="replace") if isinstance(message, bytes) else message
            for secret in sorted(self._sensitive, key=len, reverse=True):
                raw = raw.replace(secret, "[REDACTED]")
            with self._lock:
                self._persist({"unparseable_message_redacted": raw}, kind="parse_failure", received=_utc(self.clock()))
            raise ValueError("Invalid market message JSON") from None
        self.ingest(value, received_at=self.clock())

    def ingest(self, value, *, received_at):
        """Injectable boundary for one native message; useful for offline tests."""
        received = _utc(received_at)
        if not isinstance(value, dict) or set(value).difference({"response", "data", "notify"}):
            raise ValueError("Invalid market stream envelope")
        safe = {"response": [], "data": [], "notify": []}
        for ack in value.get("response", []):
            content = ack.get("content", {})
            code = content.get("code") if isinstance(content, dict) else None
            if type(code) is not int:
                raise ValueError("Invalid ACK result code")
            safe["response"].append({"requestid": str(ack.get("requestid")), "service": ack.get("service"),
                                     "command": ack.get("command"), "code": code})
        for block in value.get("data", []):
            if block.get("service") not in self._scopes or block.get("service") not in FIELDS:
                raise ValueError("Unrequested market service; account data is prohibited")
            if not isinstance(block.get("content"), list):
                raise ValueError("Invalid native market records")
            safe["data"].append(block)
        for item in value.get("notify", []):
            if not isinstance(item, dict) or set(item) != {"heartbeat"}:
                raise ValueError("Unrequested stream notification")
            safe["notify"].append(item)
        with self._lock:
            ref = self._persist(safe, kind="stream_message", received=received)
            for ack in safe["response"]:
                identity = ack["requestid"]
                pending = self._pending.get(identity)
                if pending is None:
                    old = self._ack_history.get(identity)
                    if old and old["code"] == 19 and old["service"] != "ADMIN" and ack["code"] in {0, 19, 24} and (old["service"], old["command"]) == (ack["service"], ack["command"]):
                        self._diagnostic("non_authoritative_capacity_followup", service=ack["service"], code=ack["code"])
                        continue
                    raise ValueError("Unmatched market acknowledgement")
                if pending[:2] != (ack["service"], ack["command"]):
                    raise ValueError("Market acknowledgement scope mismatch")
                self._pending.pop(identity)
                self._ack_history[identity] = ack
                service, command, keys = pending
                if service == "ADMIN":
                    if ack["code"] != 0:
                        raise PermissionError("Stream login rejected")
                    continue
                evidence = self._acks[service]
                evidence["received_at"] = received.isoformat()
                evidence["raw_ref"] = ref
                evidence["codes"].append(ack["code"])
                if ack["code"] == 0:
                    evidence["keys"].extend(keys)
                    evidence["status"] = "ok"
                else:
                    evidence["status"] = "capacity_blocked" if ack["code"] == 19 else "not_authorized"
            for block in safe["data"]:
                service = block["service"]
                self._last_raw_by_service[service] = ref
                for native in block["content"]:
                    self._native_counts[service] = self._native_counts.get(service, 0) + 1
                    if isinstance(native, dict) and native.get("delayed") is True:
                        self._delayed_counts[service] = self._delayed_counts.get(service, 0) + 1
                    row = normalize(service, native, received_at=received, scope=self._scopes[service])
                    if row is None:
                        self._rejected += 1
                        continue
                    row["raw_ref"] = ref
                    row["native_sha256"] = hashlib.sha256(_bytes(native)).hexdigest()
                    row["connection_id"] = self._connection
                    if "FUTURES" in service:
                        key = row["provider_symbol"]
                        if row.get("active_contract"):
                            previous = self._future_ids.get(key)
                            candidate = (row["active_contract"], row["event_at"], received)
                            if previous is None or candidate[1:] > previous[1:]:
                                self._future_ids[key] = candidate
                            elif candidate[1:] == previous[1:] and (previous[0] is None or previous[0] != candidate[0]):
                                self._future_ids[key] = (None, row["event_at"], received)
                                self._diagnostic("conflicting_futures_identity", native_symbol=key)
                        identity = self._future_ids.get(key)
                        if re.fullmatch("/" + row["root"] + r"[FGHJKMNQUVXZ]\d{1,4}", key):
                            row["contract_symbol"] = key
                        elif identity and identity[0] is not None and identity[1] <= row["event_at"] and identity[2] <= received:
                            row["contract_symbol"] = identity[0]
                        else:
                            continue
                    key = (service, row["provider_symbol"], row["event_at"] if service.startswith("CHART_") else None)
                    old = self._rows.get(key)
                    if old and (row["event_at"], received) == (old["event_at"], old["received_at"]) and (old.get("conflicting") or old["native_sha256"] != row["native_sha256"]):
                        row["conflicting"] = True
                        self._diagnostic("conflicting_same_event_and_receipt", service=service)
                    if old is None or (row["event_at"], received) >= (old["event_at"], old["received_at"]):
                        self._rows[key] = row
            lower = received - timedelta(minutes=30)
            self._rows = {k: r for k, r in self._rows.items() if r["received_at"] >= lower}
            if len(self._rows) > 20000:
                self._rows = dict(sorted(self._rows.items(), key=lambda item: item[1]["received_at"])[-20000:])
                self._diagnostic("projection_record_bound", retained=20000)

    def _run(self):
        gap_start = _utc(self.clock())
        while not self._stop.is_set():
            try:
                if self._session is None:
                    from datafetching.schwab_fetch import DataFetchingSchwabSession
                    self._session = (self.session_factory or DataFetchingSchwabSession)()
                bootstrap = self._http("userPreference", trader=True)
                infos = bootstrap.get("streamerInfo") if isinstance(bootstrap, dict) else None
                if not isinstance(infos, list) or len(infos) != 1:
                    raise ValueError("No unique market streamer bootstrap")
                required = ("schwabClientCorrelId", "schwabClientCustomerId", "schwabClientChannel", "schwabClientFunctionId", "streamerSocketUrl")
                self._info = {key: infos[0].get(key) for key in required}
                if any(not isinstance(v, str) or not v for v in self._info.values()):
                    raise ValueError("Invalid market bootstrap")
                self._sensitive.update(self._info.values())
                url = urlparse(self._info["streamerSocketUrl"])
                if url.scheme != "wss" or url.username or url.password or not url.hostname or not any(url.hostname == d or url.hostname.endswith("." + d) for d in ("schwab.com", "schwabapi.com")):
                    raise ValueError("Unexpected market websocket origin")
                if self._stop.is_set():
                    break
                import websocket
                self._socket = (self.socket_factory or websocket.create_connection)(self._info["streamerSocketUrl"], timeout=1)
                with self._lock:
                    self._connection = uuid.uuid4().hex
                    self._scopes, self._acks, self._pending, self._rows, self._future_ids = {}, {}, {}, {}, {}
                    self._ack_history = {}
                    self._capacity_limits, self._subscription_attempts = {}, {}
                    self._status = "subscribing"
                auth = self._session._headers().get("Authorization", "")
                if not auth.startswith("Bearer "):
                    raise ValueError("Missing existing bearer session")
                self._sensitive.update((auth, auth[7:]))
                identity = self._send("ADMIN", "LOGIN", {"Authorization": auth[7:], "SchwabClientChannel": self._info["schwabClientChannel"], "SchwabClientFunctionId": self._info["schwabClientFunctionId"]})
                self._await(identity)
                equities = {s: s for s in self.symbols}
                for service in ("LEVELONE_EQUITIES", "CHART_EQUITY", "NYSE_BOOK", "NASDAQ_BOOK"):
                    self._subscribe(service, equities)
                for service in ("LEVELONE_FUTURES", "CHART_FUTURES"):
                    self._subscribe(service, {s: None for s in self.futures})
                with self._lock:
                    self._status = "connected"
                    self._gaps.append({"from": gap_start.isoformat(), "to": _utc(self.clock()).isoformat(), "reason": "startup_or_reconnect", "replayed": False})
                    self._gap_started_at = None
                last_chain_generation = -1
                while not self._stop.is_set():
                    self._receive()
                    last_chain_generation = self._sync_option_subscriptions(last_chain_generation)
            except InterruptedError:
                pass
            except Exception as exc:
                self._diagnostic("connection_unavailable", error_type=type(exc).__name__)
            finally:
                socket, self._socket = self._socket, None
                if socket is not None:
                    try:
                        socket.close()
                    except Exception:
                        pass
                with self._lock:
                    self._seal()
                    self._status = "closed" if self._stop.is_set() else "reconnecting"
                gap_start = _utc(self.clock())
                self._gap_started_at = gap_start
            if not self._stop.is_set():
                self._stop.wait(self.reconnect_seconds)

    def snapshot(self):
        """Return current observed subsets, never market-wide completeness."""
        now = _utc(self.clock())
        with self._lock:
            self._seal()
            rows = [r for r in self._rows.values() if not r.get("conflicting") and r["event_at"] >= now - timedelta(minutes=30)
                    and r["received_at"] <= now and r.get("bar_end_at", r["event_at"]) <= now]
            results = []
            pairs = [(s, symbol) for s in ("LEVELONE_EQUITIES", "CHART_EQUITY", "NYSE_BOOK", "NASDAQ_BOOK", "LEVELONE_OPTIONS", "OPTIONS_BOOK") for symbol in self.symbols]
            pairs += [(s, key) for s in ("LEVELONE_FUTURES", "CHART_FUTURES") for key in self.futures]
            for service, symbol in pairs:
                group = [r for r in rows if r["service"] == service and (r["provider_symbol"] == symbol if "FUTURES" in service else r["symbol"] == symbol)]
                ack = copy.deepcopy(self._acks.get(service, {}))
                accepted = set(ack.get("keys", []))
                group = [r for r in group if r["provider_symbol"] in accepted]
                name = {"LEVELONE_EQUITIES": "schwab_stream_quote", "CHART_EQUITY": "schwab_stream_bars", "NYSE_BOOK": "schwab_stream_nyse_book", "NASDAQ_BOOK": "schwab_stream_nasdaq_book", "LEVELONE_OPTIONS": "schwab_stream_options", "OPTIONS_BOOK": "schwab_stream_options_book"}.get(service)
                if "FUTURES" in service:
                    root = re.match(r"/(ES|NQ|RTY|CL|GC|ZN|6E)", symbol).group(1).lower()
                    name = "schwab_future_" + root + ("_bars" if service.startswith("CHART_") else "_quote")
                    contracts = {r.get("contract_symbol") for r in group}
                    if len(contracts) != 1:
                        group = []
                features = self._features(service, group)
                if "OPTIONS" in service:
                    counts = self._scope_counts.get(symbol, {})
                    features.update(requested_contracts=counts.get("requested"), acknowledged_contracts=sum(self._scopes.get(service, {}).get(key) == symbol for key in accepted))
                usable = bool(group) and self._status == "connected" and any(v is not None for v in features.values())
                received = max([*(r["received_at"] for r in group),
                                *([_utc(ack["received_at"])] if ack.get("received_at") else [])], default=now) if usable else now
                events = [r.get("bar_end_at", r["event_at"]) for r in group]
                used_segments = {r["raw_ref"].get("segment_id") for r in group}
                used_segments.add(ack.get("raw_ref", {}).get("segment_id"))
                used_segments.add(self._last_raw_by_service.get(service, {}).get("segment_id"))
                for attempt in self._subscription_attempts.get(service, []):
                    used_segments.add((attempt.get("request_raw_ref") or {}).get("segment_id"))
                    used_segments.add((attempt.get("ack_raw_ref") or {}).get("segment_id"))
                chain_ref = self._chain_refs.get(symbol)
                if chain_ref:
                    used_segments.add(chain_ref.get("segment_id"))
                payload = {"version": VERSION, "connection_id": self._connection, "connection_status": self._status,
                    "service": service, "native_scope": sorted(self._scopes.get(service, {})),
                    "acknowledgement": ack, "option_scope": self._scope_counts.get(symbol),
                    "raw_segments": [self._segments[k] for k in used_segments if k in self._segments],
                    "raw_callback_receipts": list(self._callback_refs) if self.persist_raw else [],
                    "chain_raw_ref": chain_ref, "observed_records": len(group), "rejected_records": self._rejected,
                    "latest_service_raw_ref": self._last_raw_by_service.get(service),
                    "cumulative_received_native_records": self._native_counts.get(service, 0),
                    "cumulative_delayed_records": self._delayed_counts.get(service, 0),
                    "chain_scope_evidence": self._chain_details.get(symbol),
                    "chain_attempt": copy.deepcopy(self._chain_attempts.get(symbol)),
                    "chain_round": {"slot": self._chain_round_slot, "completed_generation": self._chain_generation,
                                    "completed_attempts": sum(v["status"] != "pending" for v in self._chain_attempts.values()),
                                    "configured_symbols": len(self.symbols)},
                    "option_subscription_budget": self._option_budget(),
                    "option_subscription_attempts": list(self._subscription_attempts.get(service, [])),
                    "accepted_or_minimal_capacity_bound": self._capacity_limits.get(service),
                    "gaps": list(self._gaps), "diagnostics": list(self._diagnostics),
                    "open_gap_started_at": self._gap_started_at.isoformat() if self._gap_started_at else None,
                    "snapshot_at": now.isoformat(), "numeric_scope": "acknowledged observed subset only; never full chain/market",
                    "coverage": "Actual received current service scope; option scope can be bounded; no complete market/quarter or missed-event replay claim",
                    "projection": "Latest actual quote/book delta per instrument; completed positive-volume chart records; no missing price carry"}
                results.append({"symbol": None if "FUTURES" in service else symbol, "source": name,
                    "started_at": (self._started_at or received).isoformat(), "received_at": received.isoformat(),
                    "event_at": max(events).isoformat() if events else None,
                    "bar_end_at": max(events).isoformat() if events and service.startswith("CHART_") else None,
                    "status": "ok" if usable else "unavailable", "features": features if usable else {},
                    "request": {"provider": "schwab", "dataset": "streamer", "service": service, "projection_version": VERSION}, "payload": payload})
            used = {r["raw_ref"].get("segment_id") for r in self._rows.values()}
            used.update(ref.get("segment_id") for ref in self._chain_refs.values())
            used.update(ack.get("raw_ref", {}).get("segment_id") for ack in self._acks.values())
            used.update(ref.get("segment_id") for ref in self._last_raw_by_service.values())
            for attempts in self._subscription_attempts.values():
                for attempt in attempts:
                    used.add((attempt.get("request_raw_ref") or {}).get("segment_id"))
                    used.add((attempt.get("ack_raw_ref") or {}).get("segment_id"))
            self._segments = {k: v for k, v in self._segments.items() if k in used}
            return results

    def _features(self, service, rows):
        if not rows:
            return {}
        rows = sorted(rows, key=lambda r: (r["event_at"], r["received_at"]))
        if service.startswith("CHART_"):
            return {"return": rows[-1]["close"] / rows[0]["open"] - 1,
                    "range_relative": (max(r["high"] for r in rows) - min(r["low"] for r in rows)) / rows[0]["open"],
                    "volume": sum(r["volume"] for r in rows), "observed_bars": len(rows)}
        if service in BOOKS:
            projected = []
            for row in rows if service == "OPTIONS_BOOK" else rows[-1:]:
                bid, ask = row.get("bid_depth"), row.get("ask_depth")
                projected.append({"spread_relative": _quote(row)["spread_relative"],
                    "depth_imbalance": _ratio(bid - ask, bid + ask) if bid is not None and ask is not None else None,
                    **{k: row.get(k) for k in ("bid_depth", "ask_depth", "bid_levels", "ask_levels")}})
            features = {key: median([p[key] for p in projected if p[key] is not None]) if any(p[key] is not None for p in projected) else None for key in BOOK_FIELDS}
            if service == "OPTIONS_BOOK":
                features["contracts"] = len(rows)
            return features
        if service == "LEVELONE_OPTIONS":
            return {**{k: median([r[k] for r in rows if r.get(k) is not None]) if any(r.get(k) is not None for r in rows) else None
                       for k in ("iv", "delta", "gamma", "theta", "vega", "rho")}, "contracts": len(rows)}
        return _quote(rows[-1])
