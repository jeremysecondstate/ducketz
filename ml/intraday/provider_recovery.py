"""Stop-aware, durable retries for explicitly owned market-data requests.

No network or filesystem work occurs on import. A job is retried only for an
observed transport/temporary HTTP failure. Empty successful responses remain
empty; scheduling a different data window belongs to the source contract.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
from typing import Mapping
import uuid

UTC = timezone.utc
TRANSIENT_HTTP = frozenset({408, 425, 429, 500, 502, 503, 504})


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("An aware actual clock is required")
    return value.astimezone(UTC)


def sanitize_cause(value, secrets=()):
    """Keep bounded provider diagnostics, never URL queries or credentials."""
    value = str(value)
    for secret in sorted((str(s) for s in secrets if s), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"https?://[^\s'\"<>]+", "[URL REDACTED]", value, flags=re.I)
    value = re.sub(r"(?i)(?:bearer\s+)[\w.\-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|client[_-]?secret|password)[\"']?\s*[:=]\s*)[\"']?[^\s,;}\"']+", r"\1[REDACTED]", value)
    return value[:1200]


def retry_after_seconds(value, now):
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (_utc(parsedate_to_datetime(str(value))) - _utc(now)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0., seconds) if math.isfinite(seconds) else None


class ProviderRequestError(RuntimeError):
    def __init__(self, *, status=None, cause="Provider request failed", retry_after=None):
        super().__init__(cause)
        self.http_status, self.retry_after = status, retry_after


def safe_failure(exc, now=None, *, secrets=()):
    now = _utc(now or datetime.now(UTC))
    response = getattr(exc, "response", None)
    code = getattr(exc, "http_status", None) or getattr(response, "status_code", None)
    if not isinstance(code, int):
        match = re.search(r"\bHTTP\s+(\d{3})\b", str(exc))
        code = int(match.group(1)) if match else None
    header = getattr(exc, "retry_after", None)
    if header is None and response is not None:
        header = getattr(response, "headers", {}).get("Retry-After")
    transport = isinstance(exc, (TimeoutError, ConnectionError)) or any(
        name in {"Timeout", "ReadTimeout", "ConnectTimeout", "ConnectionError", "ChunkedEncodingError"}
        for name in (cls.__name__ for cls in type(exc).__mro__))
    transient = code in TRANSIENT_HTTP if code is not None else transport
    cause = getattr(response, "text", None) or str(exc)
    scope_too_large = "protocol.http.TooBigBody" in cause or "Body buffer overflow" in cause
    if scope_too_large:
        transient = False  # The same oversized request cannot be repaired by waiting.
    return {"error_type": type(exc).__name__, "http_status": code,
            "cause": sanitize_cause(cause, secrets), "transient": transient,
            "category": "request_scope_too_large" if scope_too_large else "temporary_request_failure" if transient else "request_rejected" if code is not None else "request_failure",
            "retry_after_seconds": retry_after_seconds(header, now)}


def fmp_request(provider, endpoint, params, *, http_get=None):
    """Use established provider credentials/settings, retaining safe HTTP cause.

    The existing _get_json discards Retry-After. This equivalent market-only
    transport preserves it without modifying that shared application adapter.
    """
    import requests
    from app.services.fmp_corporate_data import _looks_like_error_payload
    provider._validate_config()
    if not re.fullmatch(r"[a-zA-Z0-9_\-/]+", endpoint) or ".." in endpoint:
        raise ValueError("A relative FMP endpoint is required")
    response = (http_get or requests.get)(provider.base_url + "/" + endpoint.lstrip("/"),
        params={**dict(params), "apikey": provider.api_key}, timeout=provider.timeout_seconds)
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    if response.status_code >= 400 or _looks_like_error_payload(payload):
        raise ProviderRequestError(status=response.status_code,
            cause=sanitize_cause(payload, (provider.api_key,)), retry_after=response.headers.get("Retry-After"))
    return payload


def request_key(provider, symbol, source, params):
    return hashlib.sha256(json.dumps([provider, symbol, source, params], sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class RetryLedger:
    """One collector owns this journal; failed jobs survive process restarts.

    run() never holds the state lock over a provider request or wait. The caller
    persists each attempt in on_attempt before its payload can become features.
    max_attempts bounds work per invocation, not the lifetime of a transient job.
    """
    def __init__(self, path, *, clock=None, wait=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.wait = wait
        self._lock = threading.RLock()
        self._state = None

    def _load(self):
        if self._state is None:
            self._state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
            if not isinstance(self._state, dict) or len(self._state) > 2000:
                raise ValueError("Invalid or unbounded provider retry journal")
        return self._state

    def _save(self, key, value):
        with self._lock:
            state = self._load()
            if key not in state and len(state) >= 2000:
                raise ValueError("Provider retry journal job bound exceeded")
            state[key] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)

    def state(self, key):
        with self._lock:
            return dict(self._load().get(key, {}))

    def due_keys(self):
        now = _utc(self.clock())
        with self._lock:
            due=[(value["next_retry_at"],key) for key,value in self._load().items()
                if value.get("status") == "retry_pending" and _utc(value["next_retry_at"]) <= now]
            return tuple(key for _,key in sorted(due))  # Oldest due work cannot be starved.

    def reset(self, key):
        """Explicit changed-scope recovery; previous attempts remain immutable."""
        self._save(key, {"status":"reset", "attempt_count":self.state(key).get("attempt_count",0)})

    def defer(self, key, *, seconds=2, reason="next_bounded_partition"):
        state=self.state(key)
        self._save(key,{"status":"retry_pending","attempt_count":state.get("attempt_count",0),
            "next_retry_at":(_utc(self.clock())+timedelta(seconds=max(2.,seconds))).isoformat(),"reason":reason})

    def run(self, key, request, on_attempt, *, stop_event=None, max_attempts=2, deadline=None, secrets=()):
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("An explicit bounded per-pass attempt count is required")
        stop = stop_event or threading.Event()
        state = self.state(key)
        if state.get("status") == "request_rejected":
            return {"status": "request_rejected", "error": state.get("error"), "attempts": 0}
        for local_attempt in range(max_attempts):
            if stop.is_set():
                return {"status": "stopped", "attempts": local_attempt}
            now = _utc(self.clock())
            due = _utc(state.get("next_retry_at", now))
            delay = max(0., (due-now).total_seconds())
            if delay and max_attempts==1:
                return {"status":"deferred","next_retry_at":due.isoformat(),"attempts":0}
            if deadline is not None and max(now, due) >= _utc(deadline):
                return {"status": "deferred", "next_retry_at": due.isoformat(), "attempts": local_attempt}
            if delay and ((self.wait or stop.wait)(delay) or stop.is_set()):
                return {"status": "stopped", "attempts": local_attempt}
            started = _utc(self.clock())
            number = int(state.get("attempt_count", 0)) + 1
            try:
                payload = request()
            except InterruptedError:
                return {"status": "stopped", "attempts": local_attempt + 1}
            except Exception as exc:
                received = _utc(self.clock())
                error = safe_failure(exc, received, secrets=secrets)
                event = {"status": "error", "started_at": started.isoformat(), "received_at": received.isoformat(),
                         "attempt_number": number, "error": error}
                on_attempt(event)
                state = {"status": "retry_pending" if error["transient"] else "request_rejected",
                         "attempt_count": number, "last_received_at": received.isoformat(), "error": error}
                if error["transient"]:
                    state["next_retry_at"] = (received + timedelta(seconds=max(2., error["retry_after_seconds"] or 0.))).isoformat()
                self._save(key, state)
                if not error["transient"] or local_attempt + 1 == max_attempts:
                    return {"status": state["status"], "error": error, "attempts": local_attempt + 1,
                            "next_retry_at": state.get("next_retry_at")}
                continue
            received = _utc(self.clock())
            event = {"status": "ok", "started_at": started.isoformat(), "received_at": received.isoformat(),
                     "attempt_number": number, "payload": payload}
            on_attempt(event)
            self._save(key, {"status": "ok", "attempt_count": number, "last_received_at": received.isoformat()})
            return {**event, "attempts": local_attempt + 1}
