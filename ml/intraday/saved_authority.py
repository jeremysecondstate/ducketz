"""Pure validation of retained historical authority receipts and bindings.

This leaf preserves the existing saved evidence format without a transport,
client, file loader, account query or runtime admission check. A validated
historical view never grants permission to send an order or start a trader.
The original modules and their caller class identities remain unchanged until
those callers are reviewed together; this additive leaf is not wired into them.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from .scout_protocol import Grant, ProtocolError, canonical_sha256, _timestamp
from .scout_session_books import VERSION as BOOKS_VERSION


MAX_JSON_BYTES = 1024 * 1024


class AdmissionTransportError(ValueError):
    """Sanitized transport failure; an uncertain send must be reconciled."""


def _fail(code: str) -> AdmissionTransportError:
    return AdmissionTransportError(code)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _fail("invalid_json")
        result[key] = value
    return result


def _constant(_value):
    raise _fail("invalid_json")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise _fail("invalid_json")
    return number


def encode_json(value: dict) -> bytes:
    """Encode a finite JSON object, bounded on the wire, without private errors."""
    try:
        if type(value) is not dict:
            raise _fail("invalid_json")
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 64:
                raise _fail("invalid_json")
            if type(item) is dict:
                if any(type(key) is not str for key in item):
                    raise _fail("invalid_json")
                pending.extend((child, depth + 1) for child in item.values())
            elif type(item) is list:
                pending.extend((child, depth + 1) for child in item)
            elif item is not None and type(item) not in (str, int, float, bool):
                raise _fail("invalid_json")
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False,
                         separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_JSON_BYTES:
            raise _fail("json_too_large")
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _fail("invalid_json") from None


def decode_json(raw: bytes) -> dict:
    """Reject duplicate keys, nonfinite values, trailing data and non-objects."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_JSON_BYTES:
        raise _fail("invalid_json")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                           parse_constant=_constant, parse_float=_float)
        # This also rejects escaped lone surrogates before the service sees them.
        encode_json(value)
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise _fail("invalid_json") from None


PROTOCOL_VERSION = "intraday-shared-admission-v1"


BINDING_VERSION = "scout-atlas-authority-binding-v2"


RECEIPT_VERSION = "scout-atlas-authority-receipt-v1"


PRESTART_RECEIPT_VERSION = "scout-prestart-authority-receipt-v1"


VIEW_FIELDS = frozenset({"version", "protocol_version", "server_identity", "participant_id", "stream",
    "account_fingerprint", "request_nonce", "observed_at", "expires_at", "authenticated", "read_only",
    "orders_or_commitments_created", "protocol_compatible"})


PARTICIPANT, STREAM = "pc-new", "scout-15m"


_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)


class AuthorityProtocolError(ProtocolError):
    """An authenticated response does not satisfy the exact local contract."""


def _require(condition, message):
    if not condition:
        raise AuthorityProtocolError(message)


def _matches(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _instant(value):
    return datetime.fromisoformat(_timestamp(value, "authority timestamp"))


def _now(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None,
             "Aware local clock required")
    return value.astimezone(timezone.utc)


def _local_path(value):
    _require(type(value) is str and value and not value.startswith(("\\\\", "//")), "Absolute host-local binding path required")
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts and path.name not in {"", ".", ".."}, "Absolute host-local binding path required")
    _require(not any(":" in part for part in path.parts[1:]), "Alternate data streams are not binding files")
    return path


@dataclass(frozen=True)
class ScoutAuthorityBinding:
    endpoint: str
    server_identity: str
    account_fingerprint: str
    symbols: tuple[str, ...]
    client_certificate: str
    private_key: str
    server_ca: str
    client_certificate_sha256: str
    server_ca_sha256: str
    server_certificate_sha256: str
    timeout_seconds: float = 10.0

    def __post_init__(self):
        try:
            parsed = urlsplit(self.endpoint)
            valid = (type(self.endpoint) is str and parsed.scheme == "https" and parsed.hostname
                     and parsed.username is None and parsed.password is None and parsed.path in {"", "/"}
                     and not parsed.query and not parsed.fragment and parsed.port != 0
                     and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.endpoint))
        except (TypeError, ValueError, AttributeError):
            valid = False
        _require(valid, "Explicit HTTPS authority origin required")
        _require(_matches(self.server_identity, _ID), "Explicit server identity required")
        for value in (self.account_fingerprint, self.client_certificate_sha256, self.server_ca_sha256,
                      self.server_certificate_sha256):
            _require(_matches(value, _HASH), "Exact account and public certificate hashes required")
        _require(type(self.symbols) is tuple and self.symbols
                 and all(type(s) is str and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", s, re.ASCII) for s in self.symbols)
                 and len(set(self.symbols)) == len(self.symbols),
                 "Explicit unique Scout symbols required")
        for value in (self.client_certificate, self.private_key, self.server_ca):
            _local_path(value)
        _require(len({self.client_certificate, self.private_key, self.server_ca}) == 3, "Distinct certificate, key and CA files required")
        _require(type(self.timeout_seconds) in (float, int) and math.isfinite(self.timeout_seconds)
                 and 0 < self.timeout_seconds <= 60, "Finite bounded authority timeout required")

    def payload(self):
        value = asdict(self)
        value["symbols"] = list(self.symbols)
        return dict(version=BINDING_VERSION, participant_id=PARTICIPANT, stream=STREAM, **value)

    @property
    def sha256(self):
        return canonical_sha256(self.payload())


@dataclass(frozen=True)
class AuthorityReceipt:
    """Immutable validated bytes plus their successful ScoutBooks write handle."""
    _json: str
    sha256: str
    path: str

    @property
    def payload(self):
        return json.loads(self._json)

    @property
    def response(self):
        return self.payload["response"]

    @property
    def saved(self):
        return dict(sha256=self.sha256, path=self.path)

    @property
    def grant(self):
        # A historical decision inside status/reserve recovery is not a fresh grant.
        value = self.response
        return Grant.from_payload(value["grant"]) if value.get("status") == "GRANTED" and "grant" in value else None


def validate_view_receipt(receipt, binding, *, operation, request_id, request, now, allow_prestart=False):
    """Validate a saved view envelope without reading files or creating stores.

    The caller must then validate the exact response schema and its current
    observation window. A view receipt never grants send/cancel authority.
    """
    current = _now(now)
    _require(type(binding) is ScoutAuthorityBinding and type(receipt) is AuthorityReceipt,
             "Exact authority view receipt and binding required")
    payload = _view_payload(receipt)
    _require(set(payload) == {"version", "binding_sha256", "operation", "request", "response", "received_at", "requested_at"}
             and payload["version"] == RECEIPT_VERSION and payload["binding_sha256"] == binding.sha256
             and payload["operation"] == operation and payload["request"] == request,
             "Authority view receipt binding differs")
    _require(not allow_prestart or operation == "readiness", "Only readiness accepts a nontrading prestart sink")
    versions = (BOOKS_VERSION, PRESTART_RECEIPT_VERSION) if allow_prestart else (BOOKS_VERSION,)
    envelopes = [dict(version=version, account_fingerprint=binding.account_fingerprint, participant_id=PARTICIPANT,
                      stream=STREAM, kind="authority:" + operation, request_id=request_id, payload=payload)
                 for version in versions]
    _require(any(canonical_sha256(envelope) == receipt.sha256 for envelope in envelopes)
             and type(receipt.path) is str and bool(receipt.path),
             "Saved authority view receipt digest differs")
    requested, received = _instant(payload["requested_at"]), _instant(payload["received_at"])
    _require(requested <= received <= current, "Authority view receipt times are reversed or future dated")
    return deepcopy(payload["response"]), requested, received


def _view_payload(receipt):
    _require(type(receipt) is AuthorityReceipt and type(receipt._json) is str, "Saved authority view receipt required")
    try:
        return decode_json(receipt._json.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as error:
        raise AuthorityProtocolError("Saved authority view payload is not an exact bounded JSON object") from error


def validate_view_envelope(response, binding, *, version, request_nonce, requested_at, received_at, now):
    """Pure pinned-identity, freshness and zero-effect validation for both views.

    Callers additionally enforce their route's exact field set and content.
    A successful read-only observation conveys no order or capacity authority.
    """
    _require(type(response) is dict and VIEW_FIELDS <= set(response)
             and type(binding) is ScoutAuthorityBinding and _matches(request_nonce, _ID),
             "Complete bound authority view envelope required")
    expected = dict(version=version, protocol_version=PROTOCOL_VERSION,
        server_identity=binding.server_identity, participant_id=PARTICIPANT, stream=STREAM,
        account_fingerprint=binding.account_fingerprint, request_nonce=request_nonce)
    _require(all(type(response[key]) is str and response[key] == value for key, value in expected.items()),
             "Authority view identity, protocol or nonce differs")
    _require(all(response[key] is True for key in ("authenticated", "read_only", "protocol_compatible"))
             and type(response["orders_or_commitments_created"]) is int
             and response["orders_or_commitments_created"] == 0,
             "Authority view must be authenticated, compatible and strictly noncommitting")
    requested, received, current = _now(requested_at), _now(received_at), _now(now)
    observed, expires = _instant(response["observed_at"]), _instant(response["expires_at"])
    _require(requested <= observed <= received <= current < expires
             and 0 < (expires - observed).total_seconds() <= 30,
             "Authority view observation is stale, future dated or outside its bounded lifetime")
    return deepcopy(response)
