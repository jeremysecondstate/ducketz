from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
import hashlib
import json

import pytest

from ml.intraday.scout_protocol import (
    Grant, MAX_INTEGER, ProtocolError, Request, canonical_sha256,
)


def request(**changes):
    values = dict(request_id="request:1", account_fingerprint="a" * 64,
        session_id="manual-scout:1", symbol="DOCU", side="BUY", quantity=10,
        limit_price="42.5", forecast_id="b" * 64, forecast_sha256="b" * 64,
        snapshot_sha256="c" * 64, sizing_sha256="d" * 64,
        reference_at="2026-10-12T14:00:00Z", issued_at="2026-10-12T14:15:01Z",
        target_close_at="2026-10-12T14:45:00Z", valid_until="2026-10-12T14:16:00Z")
    return Request(**(values | changes))


def grant(req=None, **changes):
    req = req or request()
    values = dict(grant_id="grant:1", request_id=req.request_id, request_sha256=req.sha256,
        account_fingerprint=req.account_fingerprint, session_id=req.session_id,
        authority_epoch=1, quantity=3, reservation_id="reservation:1",
        issued_at="2026-10-12T14:15:02Z", expires_at="2026-10-12T14:15:30Z")
    return Grant(**(values | changes))


def test_roundtrip_normalizes_attributes_and_has_stable_independent_hash():
    req = request(issued_at="2026-10-12T07:15:01-07:00")
    assert req == request()
    assert req.issued_at == "2026-10-12T14:15:01+00:00"
    raw = json.dumps(req.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert req.sha256 == hashlib.sha256(raw).hexdigest()
    assert canonical_sha256(dict(reversed(list(req.payload().items())))) == req.sha256
    assert Request.from_payload(json.loads(raw)) == req
    reply = grant(req, issued_at="2026-10-12T07:15:02-07:00")
    assert reply.validate(req) is reply
    assert Grant.from_payload(json.loads(json.dumps(reply.payload()))) == reply
    assert reply.sha256 == canonical_sha256(reply.payload())
    assert reply.payload()["stream"] == "scout-15m"
    assert req.payload()["participant_id"] == "pc-new"
    with pytest.raises(FrozenInstanceError):
        req.quantity = 20
    changed = req.payload()
    changed["quantity"] = 20
    assert req.quantity == 10


@pytest.mark.parametrize("changes", [
    {"request_id": ""}, {"request_id": "r" * 129}, {"request_id": "x\ny"},
    {"session_id": "é"}, {"session_id": "session name"}, {"symbol": "docu"},
    {"symbol": "A" * 17}, {"side": "buy"}, {"side": True},
    {"quantity": True}, {"quantity": 1.0}, {"quantity": 0}, {"quantity": -1},
    {"quantity": MAX_INTEGER + 1}, {"account_fingerprint": "A" * 64},
    {"forecast_id": "c" * 64}, {"forecast_sha256": "short"},
    {"snapshot_sha256": None}, {"sizing_sha256": "x" * 64},
    {"limit_price": 42.5}, {"limit_price": True}, {"limit_price": Decimal("42.5")},
    {"limit_price": "0"}, {"limit_price": "-1"}, {"limit_price": "01"},
    {"limit_price": "1.0"}, {"limit_price": "+1"}, {"limit_price": "1e2"},
    {"limit_price": "NaN"}, {"limit_price": "Infinity"}, {"limit_price": ".5"},
    {"limit_price": "0.000000001"}, {"limit_price": "1" * 33},
    {"issued_at": "2026-10-12T14:15:01"}, {"issued_at": "2026-10-12T14:15:01-00:00"},
    {"issued_at": "2026-10-12 14:15:01Z"}, {"issued_at": "2026-02-30T14:15:01Z"},
    {"issued_at": "2026-10-12T14:15:01+00:99"}, {"issued_at": "2026-10-12T14:15:01+01:60"},
    {"issued_at": "2026-10-12T14:15:01+24:00"}, {"issued_at": "2026-10-12T14:15:01-24:00"},
    {"issued_at": True}, {"reference_at": "2026-10-12T14:15:01Z"},
    {"valid_until": "2026-10-12T14:15:01Z"}, {"valid_until": "2026-10-12T14:45:01Z"},
    {"target_close_at": "2026-10-12T14:15:01Z"},
])
def test_request_rejects_invalid_identity_types_prices_and_time(changes):
    with pytest.raises(ProtocolError):
        request(**changes)


def test_positive_bounds_and_target_equal_validity_are_explicit():
    req = request(quantity=MAX_INTEGER, limit_price="0.00000001",
                  valid_until="2026-10-12T14:45:00Z")
    assert Request.from_payload(req.payload()) == req
    assert grant(req, quantity=MAX_INTEGER, authority_epoch=MAX_INTEGER).validate(req)


@pytest.mark.parametrize("factory,reader", [(request, Request.from_payload), (grant, Grant.from_payload)])
@pytest.mark.parametrize("change", ["unknown", "missing", "schema", "participant", "stream", "kind", "authentication"])
def test_wire_envelope_rejects_unknown_missing_and_forged_scope(factory, reader, change):
    payload = factory().payload()
    if change == "unknown":
        payload["side_override"] = "SELL"
    elif change == "missing":
        del payload["session_id"]
    elif change == "authentication":
        payload["authenticated"] = True
    else:
        key = {"schema": "schema_version", "participant": "participant_id"}.get(change, change)
        payload[key] = {"schema": "accepted-v1", "participant": "pc-original",
                        "stream": "atlas-15m", "kind": "other"}[change]
    with pytest.raises(ProtocolError):
        reader(payload)


@pytest.mark.parametrize("changes", [
    {"request_id": "different"}, {"request_sha256": "e" * 64},
    {"account_fingerprint": "f" * 64}, {"session_id": "another-session"},
    {"quantity": 11}, {"issued_at": "2026-10-12T14:15:00Z"},
    {"expires_at": "2026-10-12T14:16:01Z"},
])
def test_grant_rejects_wrong_request_expanded_quantity_or_validity(changes):
    with pytest.raises(ProtocolError):
        grant(**changes).validate(request())


@pytest.mark.parametrize("changes", [
    {"quantity": True}, {"quantity": 0}, {"quantity": MAX_INTEGER + 1},
    {"authority_epoch": True}, {"authority_epoch": 1.0}, {"authority_epoch": 0},
    {"authority_epoch": MAX_INTEGER + 1}, {"grant_id": "../outside"},
    {"reservation_id": " "}, {"request_sha256": "E" * 64},
    {"expires_at": "2026-10-12T14:15:02Z"},
    {"expires_at": "2026-10-12T14:15:01Z"}, {"expires_at": "2026-10-12T14:15:30"},
])
def test_grant_rejects_invalid_types_identity_and_lifetime(changes):
    with pytest.raises(ProtocolError):
        grant(**changes)


@pytest.mark.parametrize("changes", [
    {"side": "SELL"}, {"limit_price": "42.51"}, {"quantity": 11},
    {"sizing_sha256": "e" * 64}, {"snapshot_sha256": "e" * 64},
])
def test_same_request_id_with_mutated_content_cannot_reuse_grant(changes):
    req = request()
    with pytest.raises(ProtocolError, match="identity"):
        grant(req).validate(replace(req, **changes))


@pytest.mark.parametrize("value", [
    {"x": float("nan")}, {"x": float("inf")}, {"x": -float("inf")},
    {"x": Decimal("1")}, {"x": object()}, {1: "value"}, {"x": (1, 2)},
    {"x": MAX_INTEGER + 1}, {"x": "\ud800"}, ["not-an-object"],
])
def test_canonical_hash_rejects_non_json_or_nonfinite_values(value):
    with pytest.raises(ProtocolError):
        canonical_sha256(value)


def test_cyclic_or_oversize_evidence_and_nonobject_wire_are_rejected():
    cycle = {}
    cycle["self"] = cycle
    for value in (cycle, {"large": "x" * (1024 * 1024)}):
        with pytest.raises(ProtocolError):
            canonical_sha256(value)
    for reader in (Request.from_payload, Grant.from_payload):
        with pytest.raises(ProtocolError):
            reader([])
    with pytest.raises(ProtocolError):
        grant().validate(request().payload())
