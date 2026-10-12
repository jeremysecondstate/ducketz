"""Proposed Scout admission wire values; dormant, with no IO or trading hooks.

These values validate syntax and immutable request/grant relationships only.
A Grant does not authenticate Atlas or authorize a broker send. Authenticated
transport, durable single-send fencing, current manual authority, quote/cutoff
checks and exact recovery remain separate integration requirements. Scout keeps
local order-submission ownership; Atlas keeps the one account-capacity pool.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
import hashlib
import json
import math
import re


SCHEMA_VERSION = "scout-intraday-admission-proposal-v1"
PARTICIPANT_ID = "pc-new"
STREAM = "scout-15m"
MAX_INTEGER = 2**63 - 1
MAX_DECIMAL_LENGTH = 32
MAX_DECIMAL_SCALE = 8
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,15}", re.ASCII)
_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?", re.ASCII)
_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", re.ASCII)


class ProtocolError(ValueError):
    """A proposed wire value or its immutable binding is invalid."""


def _string(value, pattern, name):
    if type(value) is not str or not pattern.fullmatch(value):
        raise ProtocolError(f"Invalid {name}")
    return value


def _positive_integer(value, name):
    if type(value) is not int or not 1 <= value <= MAX_INTEGER:
        raise ProtocolError(f"{name} must be a positive signed-64-bit integer")


def _timestamp(value, name):
    _string(value, _TIME, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # -00:00 signifies an unknown offset in RFC 3339, not a UTC claim.
        if parsed.utcoffset() is None or value.endswith("-00:00"):
            raise ValueError("Known timezone required")
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError) as exc:
        raise ProtocolError(f"Invalid timezone-aware {name}") from exc


def _json_value(value):
    if value is None or type(value) in (str, bool):
        return value
    if type(value) is int and -MAX_INTEGER - 1 <= value <= MAX_INTEGER:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise ProtocolError("Canonical JSON requires string object keys")
        return {key: _json_value(item) for key, item in value.items()}
    if type(value) is list:
        return [_json_value(item) for item in value]
    raise ProtocolError("Canonical JSON requires finite, bounded JSON values")


def canonical_sha256(payload: Mapping) -> str:
    """Hash sorted, compact UTF-8 JSON, not an authentication proof.

    Wire DTOs contain no floating-point fields. This helper also supports finite
    JSON values for surrounding evidence; it is not an RFC 8785 implementation.
    """
    if not isinstance(payload, Mapping):
        raise ProtocolError("Canonical payload must be an object")
    try:
        raw = json.dumps(_json_value(payload), sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > 1024 * 1024:
            raise ProtocolError("Canonical payload exceeds one MiB")
        return hashlib.sha256(raw).hexdigest()
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        if isinstance(exc, ProtocolError):
            raise
        raise ProtocolError("Invalid canonical JSON") from exc


def _payload(value, kind):
    return {"schema_version": SCHEMA_VERSION, "kind": kind,
            "participant_id": PARTICIPANT_ID, "stream": STREAM, **asdict(value)}


def _from_payload(cls, payload, kind):
    if not isinstance(payload, Mapping):
        raise ProtocolError("Wire payload must be an object")
    names = {field.name for field in fields(cls)}
    fixed = {"schema_version": SCHEMA_VERSION, "kind": kind,
             "participant_id": PARTICIPANT_ID, "stream": STREAM}
    if set(payload) != names | set(fixed):
        raise ProtocolError("Wire payload has missing or unknown fields")
    if any(type(payload[key]) is not str or payload[key] != expected
           for key, expected in fixed.items()):
        raise ProtocolError("Wire schema or participant/stream identity differs")
    return cls(**{name: payload[name] for name in names})


@dataclass(frozen=True, slots=True)
class Request:
    request_id: str
    account_fingerprint: str
    session_id: str
    symbol: str
    side: str
    quantity: int
    limit_price: str
    forecast_id: str
    forecast_sha256: str
    snapshot_sha256: str
    sizing_sha256: str
    reference_at: str
    issued_at: str
    target_close_at: str
    valid_until: str

    def __post_init__(self):
        for name in ("request_id", "session_id"):
            _string(getattr(self, name), _ID, name)
        for name in ("account_fingerprint", "forecast_id", "forecast_sha256",
                     "snapshot_sha256", "sizing_sha256"):
            _string(getattr(self, name), _HASH, name)
        if self.forecast_id != self.forecast_sha256:
            raise ProtocolError("Forecast ID must equal immutable forecast SHA-256")
        _string(self.symbol, _SYMBOL, "symbol")
        if type(self.side) is not str or self.side not in {"BUY", "SELL"}:
            raise ProtocolError("side must be BUY or SELL")
        _positive_integer(self.quantity, "quantity")
        _string(self.limit_price, _DECIMAL, "canonical limit_price")
        if (self.limit_price == "0" or len(self.limit_price) > MAX_DECIMAL_LENGTH
                or len(self.limit_price.partition(".")[2]) > MAX_DECIMAL_SCALE):
            raise ProtocolError("limit_price must be positive, at most 32 characters and scale 8")
        for name in ("reference_at", "issued_at", "target_close_at", "valid_until"):
            object.__setattr__(self, name, _timestamp(getattr(self, name), name))
        reference, issued, target, valid = (datetime.fromisoformat(getattr(self, name))
            for name in ("reference_at", "issued_at", "target_close_at", "valid_until"))
        if not reference < issued < valid <= target:
            raise ProtocolError("Require reference < issued < valid_until <= target_close")

    def payload(self) -> dict:
        return _payload(self, "request")

    @classmethod
    def from_payload(cls, payload: Mapping) -> Request:
        return _from_payload(cls, payload, "request")

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.payload())


@dataclass(frozen=True, slots=True)
class Grant:
    grant_id: str
    request_id: str
    request_sha256: str
    account_fingerprint: str
    session_id: str
    authority_epoch: int
    quantity: int
    reservation_id: str
    issued_at: str
    expires_at: str

    def __post_init__(self):
        for name in ("grant_id", "request_id", "session_id", "reservation_id"):
            _string(getattr(self, name), _ID, name)
        for name in ("request_sha256", "account_fingerprint"):
            _string(getattr(self, name), _HASH, name)
        for name in ("authority_epoch", "quantity"):
            _positive_integer(getattr(self, name), name)
        for name in ("issued_at", "expires_at"):
            object.__setattr__(self, name, _timestamp(getattr(self, name), name))
        if datetime.fromisoformat(self.issued_at) >= datetime.fromisoformat(self.expires_at):
            raise ProtocolError("Grant must expire after issuance")

    def payload(self) -> dict:
        return _payload(self, "grant")

    @classmethod
    def from_payload(cls, payload: Mapping) -> Grant:
        return _from_payload(cls, payload, "grant")

    @property
    def sha256(self) -> str:
        return canonical_sha256(self.payload())

    def validate(self, request: Request) -> Grant:
        """Bind exactly; does not check current time or authenticate the issuer."""
        if type(request) is not Request:
            raise ProtocolError("Exact Request required for grant validation")
        if (self.request_id != request.request_id or self.request_sha256 != request.sha256
                or self.account_fingerprint != request.account_fingerprint
                or self.session_id != request.session_id):
            raise ProtocolError("Grant differs from exact request identity")
        if self.quantity > request.quantity:
            raise ProtocolError("Grant quantity exceeds requested quantity")
        if (datetime.fromisoformat(self.issued_at) < datetime.fromisoformat(request.issued_at)
                or datetime.fromisoformat(self.expires_at) > datetime.fromisoformat(request.valid_until)):
            raise ProtocolError("Grant validity lies outside the request")
        return self
