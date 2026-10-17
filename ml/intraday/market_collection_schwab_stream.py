"""Bounded Schwab market streams, with private native receipts and exact scopes.

Protocol/field mapping: primary client maintainer's schwab/streaming.py at
https://github.com/alexgolec/schwab-py (field-map source SHA recorded in audit).
Schwab's authenticated userPreference response supplies session IDs and URL;
no IDs are guessed. Only market services plus ADMIN login/logout are allowed.
This is a current observation window, never historical quarter reconstruction.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlparse
import uuid

from .market_collection import CollectionFailure, CollectionResult, SourceRequest, canonical, classify_error, instant, _atomic, _sha

VERSION = "intraday-schwab-current-stream-v1"
NORMALIZATION_VERSION = "intraday-schwab-normalization-v2"
SCOPE_VERSION = "intraday-schwab-fair-option-scope-v2"
BOOTSTRAP_VERSION = "intraday-schwab-chain-bootstrap-receipt-v1"
MAX_BOOTSTRAP_BYTES = 32 * 1024 * 1024
FIELDS = {"LEVELONE_EQUITIES": 52, "LEVELONE_OPTIONS": 56,
          "LEVELONE_FUTURES": 41, "CHART_EQUITY": 9, "CHART_FUTURES": 7,
          "NYSE_BOOK": 4, "NASDAQ_BOOK": 4, "OPTIONS_BOOK": 4}
BOOKS = {"NYSE_BOOK", "NASDAQ_BOOK", "OPTIONS_BOOK"}
OPTION_SCOPE_LIMITS = {"LEVELONE_OPTIONS": 1000, "OPTIONS_BOOK": 100}


def _store_bootstrap(root, *, symbol, started_at, ended_at, start, end, payload, response_received, status, reason):
    """Retain exact decoded provider JSON, including unusable returned payloads.

    HTTP failures have no returned JSON here; their sanitized failure receipt
    remains distinct from an empty successful market response. No session,
    preference, header, token, or private error response is serialized.
    """
    value = {"contract": BOOTSTRAP_VERSION, "provider": "schwab", "symbol": symbol,
        "source_id": "schwab:marketdata:chains:full-chain-bootstrap:v1",
        "endpoint": "chains", "params": {"symbol": symbol, "contractType": "ALL",
            "includeUnderlyingQuote": "true", "strategy": "SINGLE", "range": "ALL"},
        "scheduling_window": {"start": instant(start).isoformat(), "end": instant(end).isoformat()},
        "request_started_at": started_at, "request_ended_at": ended_at, "receipt_at": ended_at,
        "status": status, "reason": reason, "raw_encoding": "canonical_provider_json",
        "raw_payload": payload, "response_received": response_received,
        "scope": "subscription identifier discovery; no historical or model-input claim"}
    raw = canonical(value)
    if len(raw) > MAX_BOOTSTRAP_BYTES:
        raise CollectionFailure("CAPACITY_BLOCKED", "Option bootstrap response exceeds local immutable archive bound",
            {"unpersisted_bytes": len(raw), "unpersisted_sha256": hashlib.sha256(raw).hexdigest()})
    path = Path(root)/"raw"/instant(ended_at).date().isoformat()/("schwab-chain-bootstrap-"+uuid.uuid4().hex+".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        import os
        os.fsync(handle.fileno())
    return {"symbol": symbol, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw), "status": status, "request_started_at": started_at, "request_ended_at": ended_at,
        "raw_payload_present": response_received, "contract": BOOTSTRAP_VERSION}


def _verify_bootstrap(evidence):
    for symbol, receipt in evidence.get("chain_bootstrap_receipts", {}).items():
        path = Path(receipt["path"])
        if not 0 < path.stat().st_size == receipt["bytes"] <= MAX_BOOTSTRAP_BYTES:
            raise ValueError("Option bootstrap archive size changed")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != receipt["sha256"]:
            raise ValueError("Option bootstrap archive checksum changed")
        value = json.loads(raw)
        if (value.get("contract") != BOOTSTRAP_VERSION or value.get("symbol") != symbol
                or value.get("request_started_at") != receipt["request_started_at"]
                or value.get("request_ended_at") != receipt["request_ended_at"]):
            raise ValueError("Option bootstrap archive identity changed")


def _scope_keys(service, scopes, observed_at):
    keys = sorted(scopes)
    if "OPTIONS" not in service:
        return keys
    # Spread any provider-imposed capacity across configured underlyings and
    # rotate the first requested contract each quarter. Omitted or rejected
    # tails remain explicitly unavailable, never full coverage.
    groups = {}
    for key in keys:
        groups.setdefault(scopes[key], []).append(key)
    quarter = int(instant(observed_at).timestamp()//900)
    if service == "OPTIONS_BOOK":
        groups = [values for _, values in sorted(groups.items())]
        # Rotating a flattened unequal-chain universe can land in its long
        # tail and omit every contract for a smaller underlying. Advance each
        # underlying independently, then share the bounded slot count fairly.
        stride = max(1, OPTION_SCOPE_LIMITS[service]//len(groups))
        groups = [values[(quarter*stride) % len(values):]+values[:(quarter*stride) % len(values)]
            for values in groups]
        first = quarter % len(groups)
        groups = groups[first:]+groups[:first]
        return [values[index] for index in range(max(map(len, groups)))
            for values in groups if index < len(values)]
    offset = quarter*250
    groups = [values[offset % len(values):]+values[:offset % len(values)]
        for _, values in sorted(groups.items())]
    return [values[index] for index in range(max(map(len, groups)))
        for values in groups if index < len(values)]


def stream_requests(symbols, *, futures=()):
    result = []
    for symbol in symbols:
        for service, kind in (("LEVELONE_EQUITIES", "stock_quote"),
                ("CHART_EQUITY", "stock_bars"), ("NYSE_BOOK", "stock_quote"),
                ("NASDAQ_BOOK", "stock_quote"), ("LEVELONE_OPTIONS", "options"),
                ("OPTIONS_BOOK", "options")):
            result.append(SourceRequest("schwab", kind, symbol, "streamer", service,
                "stream", {"projection_version": VERSION, "mode": "current_stream_snapshot",
                "scope": "all_returned_chain_contracts" if "OPTIONS" in service else "configured_equity",
                "capture_seconds": 5, "fields": list(range(FIELDS[service]))}))
    for service in ("LEVELONE_FUTURES", "CHART_FUTURES"):
        if futures:
            result.append(SourceRequest("schwab", "cross_asset", "*", "streamer", service,
                "stream", {"projection_version": VERSION, "mode": "current_stream_snapshot",
                "symbols": list(futures), "capture_seconds": 5,
                "fields": list(range(FIELDS[service]))}))
    return tuple(result)


def _utc_ms(value):
    from .market_collection_adapters import _utc
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    return _utc(value, unit="ms", local_zone="UTC")


def _safe_status(code, message):
    if code == 0:
        return "COMPLETE", "Acknowledged market subscription"
    if code == 19:
        return "CAPACITY_BLOCKED", "Provider rejected the requested subscription capacity"
    # Provider text is used only for categorization; native incoming messages
    # remain private, while safe receipts never echo tokens or IDs.
    status, reason = classify_error(RuntimeError(str(message)))
    return status, reason if status != "ERROR" else "Provider rejected the market stream request"


def normalize(service, native, *, receipt_at, scope):
    """One actual delta only; never merge a prior price into a later event."""
    key = str(native.get("key", native.get("0", ""))).strip()
    if key not in scope:
        raise ValueError("Market stream returned an unrequested symbol")
    equity = scope[key]
    row = {"symbol": equity or key, "raw": native, "provider_symbol": key,
           "stream_receipt_at": receipt_at, "projection_version": VERSION,
           "normalization_version": NORMALIZATION_VERSION,
           "is_delayed": native.get("delayed")}
    if service in BOOKS:
        row["event_at"] = _utc_ms(native.get("1"))
        row["book_timestamp"] = row["event_at"]
        for side, field in (("bid", "2"), ("ask", "3")):
            levels = native.get(field)
            if levels is not None and not isinstance(levels, list):
                raise ValueError("Unexpected market book level schema")
            usable = [r for r in levels or [] if isinstance(r, dict)
                and isinstance(r.get("0"), (float, int)) and r["0"] > 0]
            if usable:
                top = (max if side == "bid" else min)(usable, key=lambda x: x["0"])
                row[side], row[side+"_size"] = top["0"], top.get("1")
                row[side+"_depth_levels"] = len(usable)
                volumes = [r.get("1") for r in usable]
                row[side+"_depth_volume"] = sum(volumes) if all(isinstance(v, (float, int)) and v >= 0 for v in volumes) else None
        row["book_basis"] = "actual provider book message; venue/service scope only"
    elif service.startswith("CHART_"):
        shift = 0 if service == "CHART_EQUITY" else -1
        row.update({name: native.get(str(number+shift))
            for name, number in (("open", 2), ("high", 3), ("low", 4), ("close", 5), ("volume", 6))})
        row["event_at"] = _utc_ms(native.get("7" if service == "CHART_EQUITY" else "1"))
        # Futures chart fields start at 2, unlike equity's extra sequence field.
        if service == "CHART_FUTURES":
            row.update({name: native.get(str(number)) for name, number in
                (("open", 2), ("high", 3), ("low", 4), ("close", 5), ("volume", 6))})
        row["bar_duration_seconds"] = 60
        row["bar_completion_basis"] = "consumer requires start plus 60 seconds before cutoff"
    else:
        option = service == "LEVELONE_OPTIONS"
        future = service == "LEVELONE_FUTURES"
        fields = ({"bid": 2, "ask": 3, "last": 4, "bid_size": 16, "ask_size": 17}
            if option else {"bid": 1, "ask": 2, "last": 3, "bid_size": 4, "ask_size": 5})
        row.update({name: native.get(str(number)) for name, number in fields.items()})
        row["quote_timestamp"] = _utc_ms(native.get("38" if option else "10" if future else "34"))
        row["trade_timestamp"] = _utc_ms(native.get("39" if option else "11" if future else "35"))
        row["event_at"] = row["quote_timestamp"] or row["trade_timestamp"]
        row["day_volume"] = native.get("8")  # cumulative, never treated as quarter volume
        row["volume_basis"] = "provider cumulative session total; not quarter trade volume"
        if option:
            from .market_collection_adapters import _greek
            row.update({name: _greek(native.get(str(number))) for name, number in
                (("delta", 28), ("gamma", 29), ("theta", 30), ("vega", 31), ("rho", 32))})
            vol = native.get("10")
            row.update(open_interest=native.get("9"), implied_volatility=vol/100
                if isinstance(vol, (float, int)) and vol >= 0 else None,
                implied_volatility_unit="decimal", option_type={"C": "call", "CALL": "call",
                    "P": "put", "PUT": "put"}.get(str(native.get("21", "")).upper()))
        if future:
            active = native.get("34")
            root = re.match(r"^/(ES|NQ|RTY|CL|GC|ZN|6E)(?:[FGHJKMNQUVXZ]\d+)?$", key)
            if active and (not root or not re.fullmatch("/"+re.escape(root.group(1))+r"[FGHJKMNQUVXZ](?:\d{1,2}|\d{4})", str(active))):
                raise ValueError("Active futures identity differs from the subscribed native root")
            row["active_contract_symbol"] = active
            if active:
                row["symbol"] = str(active)
    if "OPTIONS" in service:
        row.update(underlying_symbol=equity, contract_symbol=key)
        if not equity:
            raise ValueError("Option stream lacks explicit chain-derived underlying scope")
    if service in {"LEVELONE_FUTURES", "CHART_FUTURES"}:
        root = re.match(r"^/(ES|NQ|RTY|CL|GC|ZN|6E)(?:[FGHJKMNQUVXZ]\d+)?$", key)
        row["root_symbol"] = root.group(1) if root else None
    return row


def capture(session, subscriptions, raw_path, *, capture_seconds=5, timeout_seconds=60,
            max_bytes=64*1024*1024, http_get=None, socket_factory=None, monotonic=time.monotonic):
    """One connection for all market services; preserve every incoming message.

    subscriptions maps service -> {native_symbol: equity_underlying_or_None}.
    Partial delta fields stay missing. Messages arriving while ACKs are awaited
    are retained and processed. No account services are accepted or requested.
    """
    if not 0 < capture_seconds <= 30 or not capture_seconds < timeout_seconds <= 120:
        raise ValueError("Invalid stream observation/deadline bounds")
    if not subscriptions or set(subscriptions).difference(FIELDS):
        raise ValueError("Only explicitly allowlisted market services are permitted")
    for service, scope in subscriptions.items():
        if not scope or len(scope) > 50_000 or any(not isinstance(key, str) or not key or "," in key
                or "\n" in key or len(key) > 40 for key in scope):
            raise ValueError("Invalid bounded native market subscription scope")
    import requests
    import websocket
    from app.services.schwab import TRADER_BASE_URL
    headers = session._headers()
    response = (http_get or requests.get)(TRADER_BASE_URL+"/userPreference", headers=headers, timeout=10)
    response.raise_for_status()
    payload = response.json()
    infos = payload.get("streamerInfo") if isinstance(payload, dict) else None
    if not isinstance(infos, list) or len(infos) != 1:
        raise CollectionFailure("UNAVAILABLE", "No unique streamer bootstrap was returned")
    required = ("schwabClientCorrelId", "schwabClientCustomerId", "schwabClientChannel", "schwabClientFunctionId", "streamerSocketUrl")
    info = {name: infos[0].get(name) for name in required}
    if any(not isinstance(v, str) or not v for v in info.values()):
        raise ValueError("Incomplete streamer bootstrap")
    url = urlparse(info["streamerSocketUrl"])
    if url.scheme != "wss" or url.username or url.password or not url.hostname or not any(
            url.hostname == suffix or url.hostname.endswith("."+suffix) for suffix in ("schwab.com", "schwabapi.com")):
        raise ValueError("Streamer bootstrap supplied an unexpected websocket origin")
    raw_path = Path(raw_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic(raw_path.with_suffix(".bootstrap.json"), {"received_at": datetime.now(timezone.utc).isoformat(),
        "streamerInfo": info, "scope": "only necessary session-specific market bootstrap; account preferences omitted"})
    socket, sequence, total_bytes = None, 0, 0
    acknowledgements, results, rows = {}, {}, {service: [] for service in subscriptions}
    followup_controls = []
    began, started_at = monotonic(), datetime.now(timezone.utc).isoformat()
    pending = {}
    observed_count = {service: 0 for service in subscriptions}
    first, latest = {}, {}  # actual first/last deltas by service/key/field pattern
    future_identities = {}
    chart_rows = {}
    digest = hashlib.sha256()

    def send(service, command, params):
        nonlocal sequence
        sequence += 1
        request_id = str(sequence)
        pending[request_id] = (service, command)
        socket.send(json.dumps({"requests": [{"service": service, "requestid": request_id,
            "command": command, "SchwabClientCustomerId": info["schwabClientCustomerId"],
            "SchwabClientCorrelId": info["schwabClientCorrelId"], "parameters": params}]}))
        return request_id

    def receive(handle):
        nonlocal total_bytes
        if monotonic()-began >= timeout_seconds:
            raise TimeoutError("Market stream capture deadline exceeded")
        try:
            message = socket.recv()
        except (TimeoutError, websocket.WebSocketTimeoutException):
            return False
        if not message:
            raise ConnectionError("Market stream disconnected")
        message = message.decode("utf-8") if isinstance(message, bytes) else message
        value = json.loads(message)
        if not isinstance(value, dict) or set(value).difference({"response", "data", "notify"}):
            raise ValueError("Invalid native market stream message")
        receipt = datetime.now(timezone.utc).isoformat()
        # Validate scope before persistence. Keep actual market payloads, but
        # sanitize controls because a server error may echo LOGIN credentials.
        safe = {}
        for ack in value.get("response", []):
            key = str(ack.get("requestid"))
            content = ack.get("content", {})
            if not isinstance(content, dict) or type(content.get("code")) is not int:
                raise ValueError("Market stream ACK requires an integer result code")
            expected = pending.pop(key, None)
            previous = acknowledgements.get(key)
            # The live gateway sends follow-up controls after a rejected
            # request (observed19 then24, and19 then0). They cannot authenticate
            # or establish coverage: retain them for that exact request only.
            if (expected is None and previous and previous["code"] == 19
                    and content["code"] in {0, 19, 24} and previous["service"] != "ADMIN"
                    and (previous["service"], previous["command"]) == (ack.get("service"), ack.get("command"))):
                control = {"requestid": key, "service": previous["service"],
                    "command": previous["command"], "code": content["code"],
                    "status": "NON_AUTHORITATIVE_FOLLOWUP", "reason": "Additional control for a capacity-rejected market request; original rejection remains authoritative"}
                followup_controls.append(control)
                safe.setdefault("response", []).append(control)
                continue
            if expected != (ack.get("service"), ack.get("command")):
                raise ValueError("Market stream ACK does not match requested service/command/id")
            status, reason = _safe_status(content.get("code"), content.get("msg"))
            acknowledgements[key] = {"service": expected[0], "command": expected[1],
                "code": content.get("code"), "status": status, "reason": reason}
            safe.setdefault("response", []).append({"requestid": key, **acknowledgements[key]})
        for block in value.get("data", []):
            service = block.get("service")
            if service not in subscriptions:
                raise ValueError("Unrequested service in market stream")
            for native_row in block.get("content", []):
                if not isinstance(native_row, dict):
                    raise ValueError("Malformed market stream record")
                fields = {str(i) for i in range(FIELDS[service])} | {"key", "seq", "delayed", "assetMainType", "assetSubType", "cusip"}
                if set(native_row).difference(fields):
                    raise ValueError("Unexpected fields in native market service payload")
                row = normalize(service, native_row, receipt_at=receipt, scope=subscriptions[service])
                row["stream_message_timestamp"] = _utc_ms(block.get("timestamp"))
                observed_count[service] += 1
                row["sequence"] = observed_count[service]
                row["identity_basis"] = "connection-local received market record sequence; not an exchange sequence"
                key = (service, row["provider_symbol"], tuple(sorted(native_row)))
                if service.startswith("CHART_"):
                    chart_rows[(service, row["provider_symbol"], row.get("event_at"))] = row
                else:
                    first.setdefault(key, row)
                    latest[key] = row
                if service == "LEVELONE_FUTURES" and row.get("active_contract_symbol"):
                    future_identities.setdefault(row["provider_symbol"], []).append({
                        "contract": row["active_contract_symbol"], "receipt_at": receipt,
                        "event_at": row.get("event_at")})
                if len(latest)*2+len(chart_rows) > 100_000:
                    raise CollectionFailure("CAPACITY_BLOCKED", "Stream projection record bound reached")
            safe.setdefault("data", []).append({name: block[name] for name in
                ("service", "timestamp", "command", "content") if name in block})
        for notify in value.get("notify", []):
            if not isinstance(notify, dict) or set(notify) != {"heartbeat"}:
                raise ValueError("Unrequested market stream notification")
            safe.setdefault("notify", []).append(notify)
        native = canonical({"received_at": receipt, "message": safe})+b"\n"
        if total_bytes + len(native) > max_bytes:
            raise CollectionFailure("CAPACITY_BLOCKED", "Native market stream byte bound reached")
        handle.write(native)
        total_bytes += len(native)
        digest.update(native)
        return True

    def await_ack(handle, request_id):
        while request_id not in acknowledgements:
            receive(handle)
        return acknowledgements[request_id]

    try:
        socket = (socket_factory or websocket.create_connection)(info["streamerSocketUrl"], timeout=1)
        with raw_path.open("xb") as handle:
            auth = headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                raise ValueError("Existing session did not supply bearer authentication")
            login = send("ADMIN", "LOGIN", {"Authorization": auth[7:],
                "SchwabClientChannel": info["schwabClientChannel"],
                "SchwabClientFunctionId": info["schwabClientFunctionId"]})
            ack = await_ack(handle, login)
            if ack["status"] != "COMPLETE":
                raise CollectionFailure(ack["status"], "Market stream login was not authorized")
            for service in sorted(subscriptions, key=lambda name: "OPTIONS" in name):
                scopes = subscriptions[service]
                all_keys = _scope_keys(service, scopes, started_at)
                # Actual isolated and loaded captures acknowledged 100 book
                # keys; an isolated 250-key book request was rejected. 100
                # is the largest tested working scope, not a proven
                # maximum or a claim about paid entitlement. Level-one options
                # retain their separately acknowledged 1,000-key bound.
                scope_limit = OPTION_SCOPE_LIMITS.get(service, len(all_keys))
                keys = all_keys[:scope_limit]
                accepted = 0
                submitted = 0
                status, reason = (("PARTIAL", "Bounded rotating option stream subset; full returnable chain exceeds local concurrent scope")
                    if len(keys) < len(all_keys) else ("COMPLETE", "Acknowledged requested current observation window"))
                # ADD chunks avoid oversized single protocol messages. This is
                # a transport bound, not a claim of the provider's key limit.
                for offset in range(0, len(keys), 250):
                    request_id = send(service, "SUBS" if offset == 0 else "ADD",
                        {"keys": ",".join(keys[offset:offset+250]),
                         "fields": ",".join(str(i) for i in range(FIELDS[service]))})
                    submitted += len(keys[offset:offset+250])
                    ack = await_ack(handle, request_id)
                    if ack["status"] != "COMPLETE":
                        status, reason = ack["status"], ack["reason"]
                        if accepted:
                            status, reason = "PARTIAL", "A later scope chunk was rejected"
                        break
                    accepted += len(keys[offset:offset+250])
                results[service] = {"status": status, "reason": reason,
                    "requested_symbols": len(all_keys), "submitted_symbols": submitted, "acknowledged_symbols": accepted,
                    "concurrent_scope_limit": scope_limit,
                    "scope_limit_basis": "observed 100-key book acceptance; exact provider maximum unverified" if service == "OPTIONS_BOOK" else "local bounded scope",
                    "requested_scope_sha256": _sha(all_keys),
                    "submitted_scope": keys[:submitted], "acknowledged_scope": keys[:accepted],
                    "omitted_scope_count": len(all_keys)-accepted,
                    "rotation_quarter_start": instant(started_at).floor("15min").isoformat(),
                    "acknowledged_scope_sha256": _sha(keys[:accepted]),
                    "scope_order_basis": "round-robin underlying with quarter rotation" if "OPTIONS" in service else "sorted configured scope"}
            observation_started = monotonic()
            until = observation_started+capture_seconds
            if until > began+timeout_seconds:
                raise TimeoutError("Subscription acknowledgements left insufficient observation time")
            while monotonic() < until:
                receive(handle)
            actual_observation_seconds = monotonic()-observation_started
            handle.flush()
            import os
            os.fsync(handle.fileno())
    except Exception as exc:
        status, reason = classify_error(exc)
        raise CollectionFailure(status, reason, {"mode": "current_stream_snapshot",
            "raw_path": str(raw_path), "raw_sha256": digest.hexdigest(), "raw_bytes": total_bytes,
            "capture_started_at": started_at, "completed": False,
            "service_results": results, "acknowledgements": list(acknowledgements.values()),
            "followup_controls": followup_controls}) from None
    finally:
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass
    for key, row in first.items():
        if row.get("event_at") != latest[key].get("event_at"):
            rows[key[0]].append(row)
    for row_key, row in {**latest, **chart_rows}.items():
        rows[row_key[0]].append(row)
    for service in ("LEVELONE_FUTURES", "CHART_FUTURES"):
        for row in rows.get(service, []):
            identities = future_identities.get(row["provider_symbol"], [])
            contracts = {item["contract"] for item in identities}
            prior = [item for item in identities if item["event_at"] and row.get("event_at")
                and instant(item["event_at"]) <= instant(row["event_at"])
                and instant(item["receipt_at"]) <= instant(row["stream_receipt_at"])]
            if len(contracts) == 1 and prior:
                row.update(symbol=prior[-1]["contract"], active_contract_symbol=prior[-1]["contract"])
            else:
                row["root_symbol"] = None  # no backdated identity or across-roll returns
    return rows, {"mode": "current_stream_snapshot", "projection_version": VERSION,
        "capture_started_at": started_at, "capture_finished_at": datetime.now(timezone.utc).isoformat(),
        "completed": True, "capture_seconds": capture_seconds, "raw_path": str(raw_path),
        "actual_post_ack_observation_seconds": actual_observation_seconds,
        "raw_sha256": digest.hexdigest(), "raw_bytes": total_bytes,
        "service_results": results, "observed_native_records": observed_count,
        "delayed_record_counts": {service: sum(r.get("is_delayed") is True for r in values) for service, values in rows.items()},
        "acknowledgements": list(acknowledgements.values()),
        "followup_controls": followup_controls,
        "coverage_claim": "Actual current stream observation window only; no complete quarter, market, or missed-event replay claim",
        "projection_policy": "First/last actual delta for each field pattern; validated native market messages retained, control messages sanitized; no cross-message price filling"}


class SchwabStreamCollector:
    def __init__(self, root, requests, *, capture_fn=capture, chain_fetch=None):
        self.root, self.requests = Path(root), tuple(r for r in requests if r.endpoint == "stream")
        for request in self.requests:
            if (request.provider != "schwab" or request.schema not in FIELDS
                    or request.params.get("projection_version") != VERSION
                    or request.params.get("mode") != "current_stream_snapshot"
                    or list(request.params.get("fields", [])) != list(range(FIELDS[request.schema]))
                    or request.params.get("capture_seconds") != 5):
                raise ValueError("Schwab stream request differs from the frozen protocol contract")
        self.capture_fn, self.chain_fetch = capture_fn, chain_fetch
        self.lock, self.cache, self.failures = threading.Lock(), {}, {}

    def __call__(self, session, request, start, end, max_records):
        from .market_collection_extended import fetch_full_schwab_chain
        identity = _sha({"start": start, "end": end,
            "normalization_version": NORMALIZATION_VERSION,
            "bootstrap_version": BOOTSTRAP_VERSION,
            "scope_version": SCOPE_VERSION, "scope_limits": OPTION_SCOPE_LIMITS,
            "requests": [r.identity for r in self.requests]})
        with self.lock:
            failed = self.failures.get(identity)
            if failed and time.monotonic()-failed[0] < 60:
                raise failed[1]
            if identity not in self.cache:
                response_path = self.root/"responses"/start.date().isoformat()/("schwab-stream-"+identity+".json")
                if response_path.exists():
                    saved = json.loads(response_path.read_text(encoding="utf-8"))
                    claimed = saved.pop("response_sha256")
                    if claimed != _sha(saved):
                        raise ValueError("Schwab stream response checksum mismatch")
                    _verify_bootstrap(saved["evidence"])
                    self.cache[identity] = saved["rows"], saved["evidence"]
                else:
                    subscriptions, chains, excluded, bootstrap, archive_errors = {}, {}, {}, {}, {}
                    option_symbols = sorted({r.symbol for r in self.requests if "OPTIONS" in r.schema})

                    def fetch_scope(symbol):
                        began = datetime.now(timezone.utc).isoformat()
                        captured = {}
                        def received(payload):
                            captured.update(payload=payload, ended_at=datetime.now(timezone.utc).isoformat())
                        keys, failure = None, None
                        try:
                            if self.chain_fetch is None:
                                raw = fetch_full_schwab_chain(session, symbol, on_response=received)
                            else:
                                raw = self.chain_fetch(session, symbol)
                                received(raw)
                            keys = []
                            for name in ("callExpDateMap", "putExpDateMap"):
                                for strikes in raw.get(name, {}).values():
                                    for contracts in strikes.values():
                                        keys.extend(c["symbol"] for c in contracts if isinstance(c.get("symbol"), str))
                            if not keys:
                                failure = {"status": "UNAVAILABLE", "reason": "Full current chain returned no option stream identifiers"}
                            elif any(not re.fullmatch(re.escape(symbol)+r"\s*\d{6}[CP]\d{8}", key) for key in keys):
                                raise ValueError("Option chain stream scope differs from underlying")
                            else:
                                keys = sorted(set(keys))
                        except Exception as exc:
                            status, reason = classify_error(exc)
                            failure = {"status": status, "reason": reason}
                        try:
                            receipt = _store_bootstrap(self.root, symbol=symbol, started_at=began,
                                ended_at=captured.get("ended_at", datetime.now(timezone.utc).isoformat()),
                                start=start, end=end, payload=captured.get("payload"),
                                response_received="payload" in captured,
                                status=failure["status"] if failure else "COMPLETE",
                                reason=failure["reason"] if failure else "Validated full current chain identifiers")
                        except Exception as exc:
                            status, reason = classify_error(exc)
                            receipt = None
                            failure = {"status": status, "reason": "Bootstrap archive unavailable: "+reason,
                                "stage": "immutable_chain_archive", "raw_preserved": False,
                                "response_received": "payload" in captured,
                                "request_started_at": began,
                                "request_ended_at": captured.get("ended_at", datetime.now(timezone.utc).isoformat()),
                                **{k:v for k,v in getattr(exc,"collection_evidence",{}).items()
                                   if k in {"unpersisted_bytes","unpersisted_sha256"}}}
                        return symbol, keys if not failure else None, failure, receipt

                    # Two bounded market-data requests at a time. A failed chain
                    # keeps its own missing scope without delaying every other
                    # underlying serially; token refresh is session-lock guarded.
                    from concurrent.futures import ThreadPoolExecutor
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        for symbol, keys, failure, receipt in pool.map(fetch_scope, option_symbols):
                            if receipt is not None:
                                bootstrap[symbol] = receipt
                            else:
                                archive_errors[symbol] = failure
                            if failure:
                                excluded[symbol] = failure
                            else:
                                chains[symbol] = keys
                    for configured in self.requests:
                        scope = subscriptions.setdefault(configured.schema, {})
                        if "OPTIONS" in configured.schema:
                            if configured.symbol in excluded:
                                continue
                            scope.update({key: configured.symbol for key in chains[configured.symbol]})
                        elif configured.symbol == "*":
                            scope.update({key: None for key in configured.params["symbols"]})
                        else:
                            scope[configured.symbol] = configured.symbol
                    path = self.root/"raw"/datetime.now(timezone.utc).date().isoformat()/("schwab-stream-"+uuid.uuid4().hex+".jsonl")
                    subscriptions = {name: scope for name, scope in subscriptions.items() if scope}
                    try:
                        if subscriptions:
                            rows, evidence = self.capture_fn(session, subscriptions, path,
                                capture_seconds=request.params.get("capture_seconds", 5))
                        else:
                            rows, evidence = {}, {"mode": "current_stream_snapshot", "completed": False,
                                "service_results": {}, "reason": "No native option identifiers were available"}
                    except Exception as exc:
                        status, reason = classify_error(exc)
                        failure = CollectionFailure(status, reason, {
                            **getattr(exc, "collection_evidence", {}), "chain_bootstrap_receipts": bootstrap,
                            "bootstrap_archive_errors": archive_errors,
                            "bootstrap_version": BOOTSTRAP_VERSION})
                        self.failures[identity] = time.monotonic(), failure
                        raise failure from None
                    evidence = {**evidence, "excluded_option_scopes": excluded,
                        "normalization_version": NORMALIZATION_VERSION, "scope_version": SCOPE_VERSION,
                        "bootstrap_version": BOOTSTRAP_VERSION, "chain_bootstrap_receipts": bootstrap,
                        "bootstrap_archive_errors": archive_errors}
                    saved = {"rows": rows, "evidence": evidence}
                    saved["response_sha256"] = _sha(saved)
                    _atomic(response_path, saved)
                    self.cache[identity] = rows, evidence
                    if len(self.cache) > 8:
                        self.cache.pop(next(iter(self.cache)))
            rows, evidence = self.cache[identity]
        records = [r for r in rows.get(request.schema, []) if request.symbol == "*" or r.get("symbol") == request.symbol]
        if "OPTIONS" in request.schema and request.symbol in evidence.get("excluded_option_scopes", {}):
            excluded = evidence["excluded_option_scopes"][request.symbol]
            return CollectionResult([], excluded["status"], excluded["reason"], evidence)
        service = evidence["service_results"].get(request.schema, {})
        status = service.get("status", "UNAVAILABLE")
        reason = service.get("reason", "No service acknowledgement was captured")
        return CollectionResult(records, status, reason, evidence)
