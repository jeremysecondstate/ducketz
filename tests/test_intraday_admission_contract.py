from datetime import datetime, timedelta, timezone

import pytest

from ml.intraday.admission_contract import (
    READINESS_VERSION, ViewContractError, accounting_request,
    readiness_request, validate_view_envelope,
)


@pytest.mark.parametrize("body", [None, {}, {"request_nonce": 1},
    {"request_nonce": "x", "participant_id": "pc-original"},
    {"request_nonce": ""}, {"request_nonce": "a" * 129},
    {"request_nonce": "hidden\n"}, {"request_nonce": "../path"}])
def test_readiness_rejects_ambiguous_requests(body):
    with pytest.raises(ViewContractError):
        readiness_request(body)


def test_accounting_binds_exact_original_identity_and_returns_copy():
    body = dict(request_nonce="nonce:1", request_id="request:1",
                request_sha256="a" * 64, session_id="session:1")
    assert readiness_request({"request_nonce": "nonce:1"}) == "nonce:1"
    assert accounting_request(body) == body
    assert accounting_request(body) is not body
    for name in body:
        invalid = body.copy()
        invalid.pop(name)
        with pytest.raises(ViewContractError):
            accounting_request(invalid)
    for update in ({"request_sha256": "A" * 64}, {"request_sha256": True},
                   {"session_id": []}, {"participant_id": "pc-original"}):
        with pytest.raises(ViewContractError):
            accounting_request(body | update)


def fixture():
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    proof = dict(version=READINESS_VERSION, protocol_version="intraday-shared-admission-v1",
        request_nonce="n", server_identity="test-authority", account_fingerprint="a" * 64,
        participant_id="pc-new", stream="scout-15m", authenticated=True, read_only=True,
        orders_or_commitments_created=0, observed_at=now.isoformat(),
        expires_at=(now+timedelta(seconds=30)).isoformat())
    args = dict(version=READINESS_VERSION, request_nonce="n", server_identity="test-authority",
                account_fingerprint="a" * 64, now=now)
    return proof, args


def test_envelope_does_not_turn_readiness_into_authority():
    proof, args = fixture()
    proof["ready"] = False
    assert validate_view_envelope(proof, **args)["ready"] is False


@pytest.mark.parametrize("change", [
    {"request_nonce": "old"}, {"server_identity": "another"},
    {"account_fingerprint": "b" * 64}, {"participant_id": "pc-original"},
    {"stream": "atlas-15m"}, {"version": "unknown"}, {"authenticated": 1},
    {"read_only": False}, {"orders_or_commitments_created": False},
    {"orders_or_commitments_created": 1}, {"observed_at": "2026-10-13T00:00:00"},
    {"observed_at": "2026-10-13T00:00:00-00:00"},
    {"observed_at": "2026-10-13 00:00:00+00:00"},
    {"observed_at": "2026-10-13T00:00:01+00:00"},
    {"expires_at": "2026-10-13T00:00:00+00:00"},
    {"expires_at": "2026-10-13T00:00:31+00:00"},
])
def test_replay_cross_binding_and_non_observation_rejected(change):
    proof, args = fixture()
    with pytest.raises(ViewContractError):
        validate_view_envelope(proof | change, **args)


def test_expiry_is_exclusive():
    proof, args = fixture()
    args["now"] += timedelta(seconds=30)
    with pytest.raises(ViewContractError, match="stale"):
        validate_view_envelope(proof, **args)
