"""Pure wire validation for authenticated, noncommitting authority views.

Readiness is a short-lived observation, never an order grant. These helpers do
not open files, connect, enroll certificates, or acquire account reservations.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re

READINESS_VERSION = "intraday-shared-readiness-v1"
ACCOUNTING_VERSION = "intraday-canonical-accounting-v1"
PROTOCOL_VERSION = "intraday-shared-admission-v1"
RESPONSE_MAX_AGE_SECONDS = 30
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
_SHA = re.compile(r"[0-9a-f]{64}", re.ASCII)
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
                   r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])", re.ASCII)


class ViewContractError(ValueError):
    """Sanitized invalid or stale observation; caller must fail closed."""


def _identifier(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ViewContractError("invalid_view_request")
    return value


def readiness_request(body: dict) -> str:
    if type(body) is not dict or set(body) != {"request_nonce"}:
        raise ViewContractError("invalid_view_request")
    return _identifier(body["request_nonce"])


def accounting_request(body: dict) -> dict:
    fields = {"request_nonce", "request_id", "request_sha256", "session_id"}
    if type(body) is not dict or set(body) != fields:
        raise ViewContractError("invalid_view_request")
    for name in ("request_nonce", "request_id", "session_id"):
        _identifier(body[name])
    if type(body["request_sha256"]) is not str or not _SHA.fullmatch(body["request_sha256"]):
        raise ViewContractError("invalid_view_request")
    return dict(body)


def _time(value):
    try:
        if type(value) is not str or not _TIME.fullmatch(value) or value.endswith("-00:00"):
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise ViewContractError("invalid_view_response") from None


def validate_view_envelope(value: dict, *, version: str, request_nonce: str,
                           server_identity: str, account_fingerprint: str,
                           now: datetime) -> dict:
    """Validate a response received over authenticated TLS against local pins.

No self-reported field substitutes for transport certificate verification.
Time skew is deliberately not accepted: fix private host clocks, then retry.
"""
    if type(value) is not dict or now.tzinfo is None or now.utcoffset() is None:
        raise ViewContractError("invalid_view_response")
    expected = {"version": version, "protocol_version": PROTOCOL_VERSION,
                "request_nonce": request_nonce, "server_identity": server_identity,
                "account_fingerprint": account_fingerprint,
                "participant_id": "pc-new", "stream": "scout-15m"}
    if any(type(value.get(k)) is not str or value[k] != v for k, v in expected.items()):
        raise ViewContractError("view_binding_mismatch")
    if (value.get("authenticated") is not True or value.get("read_only") is not True
            or type(value.get("orders_or_commitments_created")) is not int
            or value["orders_or_commitments_created"] != 0):
        raise ViewContractError("invalid_view_response")
    observed, expires = _time(value.get("observed_at")), _time(value.get("expires_at"))
    if not (0 < (expires-observed).total_seconds() <= RESPONSE_MAX_AGE_SECONDS
            and observed <= now.astimezone(timezone.utc) < expires):
        raise ViewContractError("stale_view_response")
    return value
