"""Saved receipt validation uses synthetic bytes, never a client or live books."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket

import pytest

from ml.intraday import saved_authority as saved
from ml.intraday.scout_protocol import Grant, ProtocolError, canonical_sha256


NOW = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)
BODY = {"request_nonce": "saved-nonce:1"}
REQUEST_ID = "saved-request:1"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Saved evidence must not use network connections")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)


@pytest.fixture
def binding(tmp_path):
    return saved.ScoutAuthorityBinding(
        endpoint="https://saved-authority.example.test:8443",
        server_identity="saved-authority:1", account_fingerprint="a" * 64,
        symbols=("DOCU", "DBX"), client_certificate=str(tmp_path / "client.pem"),
        private_key=str(tmp_path / "client.key"), server_ca=str(tmp_path / "ca.pem"),
        client_certificate_sha256="b" * 64, server_ca_sha256="c" * 64,
        server_certificate_sha256="d" * 64,
    )


def view(binding, **changes):
    return dict(
        version="saved-accounting-view-v1", protocol_version=saved.PROTOCOL_VERSION,
        server_identity=binding.server_identity, participant_id=saved.PARTICIPANT,
        stream=saved.STREAM, account_fingerprint=binding.account_fingerprint,
        request_nonce=BODY["request_nonce"], observed_at=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=30)).isoformat(), authenticated=True,
        read_only=True, orders_or_commitments_created=0, protocol_compatible=True,
        historical_detail={"fills": [{"quantity": 2, "price": "42.5"}]},
    ) | changes


def receipt(binding, *, operation="accounting", response=None, payload_changes=None,
            envelope_changes=None):
    payload = dict(
        version=saved.RECEIPT_VERSION, binding_sha256=binding.sha256,
        operation=operation, request=BODY.copy(),
        response=view(binding) if response is None else response,
        requested_at=NOW.isoformat(), received_at=NOW.isoformat(),
    )
    payload.update(payload_changes or {})
    envelope = dict(
        version=saved.BOOKS_VERSION, account_fingerprint=binding.account_fingerprint,
        participant_id=saved.PARTICIPANT, stream=saved.STREAM,
        kind="authority:" + operation, request_id=REQUEST_ID, payload=payload,
    )
    envelope.update(envelope_changes or {})
    return saved.AuthorityReceipt(json.dumps(payload), canonical_sha256(envelope),
                                  "synthetic-saved-receipt.json")


def validate_receipt(value, binding, **changes):
    arguments = dict(operation="accounting", request_id=REQUEST_ID, request=BODY, now=NOW)
    arguments.update(changes)
    return saved.validate_view_receipt(value, binding, **arguments)


def validate_view(value, binding, **changes):
    arguments = dict(version="saved-accounting-view-v1", request_nonce=BODY["request_nonce"],
                     requested_at=NOW, received_at=NOW, now=NOW)
    arguments.update(changes)
    return saved.validate_view_envelope(value, binding, **arguments)


def test_binding_and_receipt_roundtrip_preserve_exact_saved_identity(binding):
    payload = binding.payload()
    assert payload["version"] == saved.BINDING_VERSION
    assert payload["symbols"] == ["DOCU", "DBX"]
    assert binding.sha256 == canonical_sha256(payload)
    reconstructed = saved.ScoutAuthorityBinding(**{
        name: tuple(payload[name]) if name == "symbols" else payload[name]
        for name in saved.ScoutAuthorityBinding.__dataclass_fields__
    })
    assert reconstructed == binding and reconstructed.sha256 == binding.sha256
    value = receipt(binding)
    response, requested, received = validate_receipt(value, reconstructed)
    assert requested == received == NOW
    assert validate_view(response, reconstructed) == view(binding)
    response["historical_detail"]["fills"][0]["quantity"] = 999
    assert value.response["historical_detail"]["fills"][0]["quantity"] == 2
    assert value.saved == {"sha256": value.sha256, "path": value.path}
    assert value.grant is None
    with pytest.raises(FrozenInstanceError):
        value.path = "changed"


@pytest.mark.parametrize("changes", [
    {"endpoint": "http://example.test"}, {"endpoint": "https://example.test/path"},
    {"endpoint": "https://u:p@example.test"}, {"endpoint": "https://example.test:0"},
    {"endpoint": "https://example.test?x=1"}, {"endpoint": "https://example.test\n"},
    {"endpoint": 42}, {"server_identity": ""}, {"account_fingerprint": "a" * 63},
    {"server_certificate_sha256": "D" * 64}, {"symbols": ("DOCU", "DOCU")},
    {"symbols": ("docu",)}, {"symbols": []}, {"symbols": ()}, {"symbols": ([],)},
    {"timeout_seconds": True}, {"timeout_seconds": float("nan")},
    {"timeout_seconds": float("inf")}, {"timeout_seconds": 0}, {"timeout_seconds": 61},
    {"private_key": "relative.key"}, {"server_ca": r"\\peer\share\ca.pem"},
])
def test_binding_rejects_ambiguous_scope_without_loading_credentials(binding, changes):
    with pytest.raises(saved.AuthorityProtocolError):
        replace(binding, **changes)


def test_binding_requires_distinct_files_and_accepts_bounded_timeout(binding):
    with pytest.raises(saved.AuthorityProtocolError):
        replace(binding, private_key=binding.client_certificate)
    assert replace(binding, timeout_seconds=60).timeout_seconds == 60


@pytest.mark.parametrize("changes", [
    {"version": "foreign"}, {"binding_sha256": "e" * 64}, {"operation": "status"},
    {"request": {"request_nonce": "foreign"}}, {"requested_at": "2026-10-12T16:00:01Z"},
    {"received_at": "2026-10-12T16:00:01Z"}, {"received_at": "2026-10-12T15:59:59Z"},
    {"extra": None},
])
def test_rehashed_receipt_cannot_change_binding_schema_or_time(binding, changes):
    with pytest.raises(saved.AuthorityProtocolError):
        validate_receipt(receipt(binding, payload_changes=changes), binding)


@pytest.mark.parametrize("changes", [
    {"version": "foreign-books"}, {"account_fingerprint": "e" * 64},
    {"participant_id": "pc-original"}, {"stream": "atlas-15m"},
    {"kind": "authority:status"}, {"request_id": "foreign-request"}, {"extra": None},
])
def test_saved_outer_envelope_must_match_original_scope(binding, changes):
    with pytest.raises(saved.AuthorityProtocolError):
        validate_receipt(receipt(binding, envelope_changes=changes), binding)


@pytest.mark.parametrize("changes", [{"sha256": "e" * 64}, {"path": ""}, {"path": None}])
def test_saved_handle_requires_matching_digest_and_path(binding, changes):
    with pytest.raises(saved.AuthorityProtocolError):
        validate_receipt(replace(receipt(binding), **changes), binding)


def test_missing_start_and_rebound_certificate_reject_saved_receipt(binding):
    value = receipt(binding)
    payload = value.payload
    del payload["requested_at"]
    with pytest.raises(saved.AuthorityProtocolError):
        validate_receipt(replace(value, _json=json.dumps(payload)), binding)
    with pytest.raises(saved.AuthorityProtocolError):
        validate_receipt(value, replace(binding, server_certificate_sha256="e" * 64))


def test_prestart_sink_requires_explicit_readiness_operation(binding):
    value = receipt(binding, operation="readiness",
                    envelope_changes={"version": saved.PRESTART_RECEIPT_VERSION})
    assert validate_receipt(value, binding, operation="readiness", allow_prestart=True)[0] == view(binding)
    with pytest.raises(saved.AuthorityProtocolError):
        validate_receipt(value, binding, operation="readiness")
    with pytest.raises(saved.AuthorityProtocolError):
        validate_receipt(receipt(binding), binding, allow_prestart=True)


@pytest.mark.parametrize("changes", [
    {"version": "foreign"}, {"protocol_version": "foreign"},
    {"server_identity": "foreign"}, {"participant_id": "pc-original"},
    {"stream": "atlas-15m"}, {"account_fingerprint": "e" * 64},
    {"request_nonce": "foreign"}, {"request_nonce": 1},
    {"authenticated": False}, {"authenticated": 1}, {"read_only": False},
    {"read_only": 1}, {"protocol_compatible": False}, {"protocol_compatible": 1},
    {"orders_or_commitments_created": False}, {"orders_or_commitments_created": True},
    {"orders_or_commitments_created": 0.0}, {"orders_or_commitments_created": 1},
    {"observed_at": "2026-10-12T16:00:00"}, {"observed_at": "2026-10-12T16:00:00-00:00"},
])
def test_view_identity_and_zero_effect_claims_are_exact(binding, changes):
    with pytest.raises(ProtocolError):
        validate_view(view(binding, **changes), binding)


@pytest.mark.parametrize("nonce", ["", "a" * 129, "_first", "has space", "nonascii-é", [], None, True])
def test_expected_nonce_is_bounded_and_typed(binding, nonce):
    with pytest.raises(saved.AuthorityProtocolError):
        validate_view(view(binding), binding, request_nonce=nonce)


@pytest.mark.parametrize("observed,expires,requested,received,current", [
    (-1, 20, 0, 0, 0), (1, 20, 0, 0, 0), (0, 0, 0, 0, 0),
    (0, -1, 0, 0, 0), (0, 30.000001, 0, 0, 0), (0, 30, 0, 0, 30),
    (0, 30, 0, 1, 0), (0, 30, 1, 1, 1),
])
def test_observation_window_is_bounded_and_ordered(binding, observed, expires, requested, received, current):
    at = lambda seconds: NOW + timedelta(seconds=seconds)
    value = view(binding, observed_at=at(observed).isoformat(), expires_at=at(expires).isoformat())
    with pytest.raises(saved.AuthorityProtocolError):
        validate_view(value, binding, requested_at=at(requested), received_at=at(received), now=at(current))


def test_exact_expiry_boundary_and_naive_clock(binding):
    assert validate_view(view(binding), binding, now=NOW + timedelta(seconds=29.999999)) == view(binding)
    with pytest.raises(saved.AuthorityProtocolError):
        validate_view(view(binding), binding, now=NOW.replace(tzinfo=None))
    value = view(binding)
    del value["read_only"]
    with pytest.raises(saved.AuthorityProtocolError):
        validate_view(value, binding)


def test_exact_class_identity_requires_explicit_consumer_composition(binding):
    class ForeignBinding(saved.ScoutAuthorityBinding):
        pass
    class ForeignReceipt(saved.AuthorityReceipt):
        pass
    foreign_binding = ForeignBinding(**{name: getattr(binding, name)
                                       for name in saved.ScoutAuthorityBinding.__dataclass_fields__})
    value = receipt(binding)
    foreign_receipt = ForeignReceipt(value._json, value.sha256, value.path)
    assert foreign_binding.payload() == binding.payload()
    assert foreign_receipt.payload == value.payload
    with pytest.raises(saved.AuthorityProtocolError):
        validate_receipt(value, foreign_binding)
    with pytest.raises(saved.AuthorityProtocolError):
        validate_receipt(foreign_receipt, binding)
    with pytest.raises(saved.AuthorityProtocolError):
        validate_view(view(binding), foreign_binding)


def test_validators_never_read_files_or_construct_network_state(binding, monkeypatch):
    value = receipt(binding)
    def forbidden(*args, **kwargs):
        raise AssertionError("Pure saved evidence validator performed file IO")
    with monkeypatch.context() as guarded:
        for name in ("read_bytes", "read_text", "open", "stat", "lstat"):
            guarded.setattr(Path, name, forbidden)
        guarded.setattr("builtins.open", forbidden)
        reconstructed = replace(binding)
        response, _, _ = validate_receipt(value, reconstructed)
        assert validate_view(response, reconstructed) == view(binding)
    assert not hasattr(saved, "ScoutAuthorityClient")
    assert not hasattr(saved, "load_binding")
    assert not hasattr(saved, "validate_send_receipt")


@pytest.mark.parametrize("raw", [
    b'{"x":1,"x":2}', b'{"nested":{"x":1,"x":2}}', b'{"x":NaN}',
    b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e999}', b'{}{}', b'{} trailing',
    b'[]', b'null', b'"string"', b'', b'{"x":"\\ud800"}', b'\xff', b'{"x":}',
    b'{"x":' + b'[' * 70 + b'0' + b']' * 70 + b'}',
    b' ' * (saved.MAX_JSON_BYTES + 1), "{}", bytearray(b"{}"),
], ids=lambda raw: type(raw).__name__ + "-" + str(len(raw)))
def test_saved_json_rejects_ambiguous_or_unbounded_bytes(raw):
    with pytest.raises(saved.AdmissionTransportError, match="^invalid_json$"):
        saved.decode_json(raw)


def test_saved_json_roundtrip_and_exact_size_and_depth_boundaries():
    value = {"nested": [1, "é", None, False], "quantity": "3"}
    assert saved.decode_json(saved.encode_json(value)) == value
    raw = b'{"x":"' + b'x' * (saved.MAX_JSON_BYTES - 8) + b'"}'
    assert len(raw) == saved.MAX_JSON_BYTES
    assert saved.decode_json(raw)["x"] == "x" * (saved.MAX_JSON_BYTES - 8)
    bounded = b'{"x":' + b'[' * 63 + b'0' + b']' * 63 + b'}'
    assert saved.decode_json(bounded) == json.loads(bounded)
    with pytest.raises(saved.AdmissionTransportError):
        saved.decode_json(b'{"x":' + b'[' * 64 + b'0' + b']' * 64 + b'}')


@pytest.mark.parametrize("value", [{1: "bad"}, {"x": {False: 3}}, {"x": (1, 2)},
                                       {"x": float("nan")}, {"x": float("inf")}, []])
def test_saved_json_encoder_rejects_non_json_values(value):
    with pytest.raises(saved.AdmissionTransportError):
        saved.encode_json(value)


@pytest.mark.parametrize("raw", ['[]', 'null', '{"same":1,"same":2}', '{', '"text"',
                                   '{"x":NaN}', '{"x":1e999}', '{"x":"\\ud800"}'])
def test_malformed_saved_receipt_is_a_protocol_error(binding, raw):
    value = saved.AuthorityReceipt(raw, "f" * 64, "synthetic-path")
    with pytest.raises(saved.AuthorityProtocolError):
        validate_receipt(value, binding)


def test_receipt_grant_property_preserves_historical_status_distinction(binding):
    grant = Grant("saved-grant:1", REQUEST_ID, "e" * 64, binding.account_fingerprint,
                  "saved-session:1", 1, 3, "saved-reservation:1", NOW.isoformat(),
                  (NOW + timedelta(seconds=30)).isoformat())
    granted = receipt(binding, response={"status": "GRANTED", "grant": grant.payload()})
    historical = receipt(binding, response={"status": "RESERVED", "grant": grant.payload()})
    assert granted.grant == grant
    assert historical.grant is None
