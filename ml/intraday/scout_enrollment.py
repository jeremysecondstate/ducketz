"""Construct Scout's inert v2 binding from an accepted private enrollment.

The caller pins the file digest of its retained ``receive_enrollment`` receipt
and supplies the original locally reviewed identities. A peer-provided digest
alone is not local trust. This module does not receive packets, enroll/sign keys,
connect, start a service, or change trading controls. Accepted v1 artifacts and
keys remain immutable; a separate destination is created once or checked on an
identical retry. Certificate validity is current, while an accepted offer's
short enrollment window remains historical evidence.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path

from .scout_authority import BINDING_VERSION, ScoutAuthorityBinding, load_binding
from .scout_journal import _safe_path


_PARAMETERS = {"directory", "offer_receipt_path", "offer_receipt_sha256", "enrollment_id",
               "action_date", "review_session", "server_identity", "account_fingerprint", "symbols", "destination"}
_RECEIPT = {"kind", "enrollment_id", "action_date", "review_session", "packet_sha256",
            "payload", "trusted_payload_sha256"}


class ScoutEnrollmentError(ValueError):
    """The accepted enrollment or separate local binding failed validation."""


def _require(value, message):
    if not value:
        raise ScoutEnrollmentError(message)


def _read_json(enrollment, path):
    raw = enrollment._read(path)
    value = enrollment._json(raw)
    return raw, value


def _trusted_offer(enrollment, *, offer_receipt_path, offer_receipt_sha256,
                   enrollment_id, action_date, review_session, server_identity,
                   account_fingerprint, symbols):
    _require(enrollment._matches(offer_receipt_sha256, enrollment._HEX),
             "Independently pinned private receipt SHA256 required")
    raw, receipt = _read_json(enrollment, offer_receipt_path)
    _require(hashlib.sha256(raw).hexdigest() == offer_receipt_sha256,
             "Retained private exchange receipt bytes changed")
    _require(set(receipt) == _RECEIPT and receipt["kind"] == "admission_offer",
             "Exact retained offer receipt required")
    for value in (action_date, review_session):
        _require(type(value) is str and date.fromisoformat(value).isoformat() == value,
                 "Original enrollment exchange dates required")
    _require(all(receipt[name] == value for name, value in
                 (("enrollment_id", enrollment_id), ("action_date", action_date), ("review_session", review_session)))
             and enrollment._matches(receipt["packet_sha256"], enrollment._HEX),
             "Retained receipt differs from original enrollment identity")
    offer = enrollment.validate_public_packet("admission_offer", receipt["payload"])
    _require(receipt["trusted_payload_sha256"] == enrollment.payload_sha256(offer),
             "Retained offer payload digest differs")
    _require(offer["enrollment_id"] == enrollment_id and offer["server_identity"] == server_identity
             and offer["account_fingerprint"] == account_fingerprint and offer["symbols"] == symbols,
             "Retained offer differs from reviewed local identity or symbols")
    return offer


def construct_v2_binding(directory, *, offer_receipt_path, offer_receipt_sha256,
                         enrollment_id, action_date, review_session, server_identity,
                         account_fingerprint, symbols, destination, now=None):
    """Validate accepted files, write a separate immutable v2 binding, return it.

    ``offer_receipt_sha256`` must come from the caller's private intake review,
    independently of the offer. ``destination`` must differ from every original
    artifact. No missing accepted state is recreated and no key is generated.
    The optional aware clock is for isolated tests; the CLI always uses now.
    """
    from . import admission_enrollment as enrollment

    directory, destination = Path(directory), _safe_path(Path(destination))
    receipt_path = _safe_path(Path(offer_receipt_path))
    _safe_path(directory / "enrollment.lock")
    _require(directory.is_dir(), "Existing accepted enrollment directory required")
    enrollment._symbols(symbols)
    _require(enrollment._matches(enrollment_id, enrollment._ENROLL)
             and enrollment._matches(server_identity, enrollment._IDENTITY)
             and enrollment._matches(account_fingerprint, enrollment._HEX),
             "Exact original local enrollment identity required")
    moment = enrollment._now(now)
    offer = _trusted_offer(enrollment, offer_receipt_path=receipt_path,
        offer_receipt_sha256=offer_receipt_sha256, enrollment_id=enrollment_id,
        action_date=action_date, review_session=review_session, server_identity=server_identity,
        account_fingerprint=account_fingerprint, symbols=symbols)
    names = ("offer.json", "request.json", "response.json", "scout-binding.json", "client.pem", "ca.pem", "client.key")
    _require(destination not in {receipt_path, directory / "enrollment.lock", *(directory / name for name in names)},
             "Separate v2 binding destination required")
    # The published lock verifies the pre-existing owner-only directory ACL.
    # It excludes another acceptance operation but does not establish trust.
    with enrollment._lock(directory):
        originals = {name: enrollment._read(directory / name) for name in names if name != "client.key"}
        parsed = {name: enrollment._json(originals[name]) for name in names[:4]}
        _require(all(originals[name] == enrollment.encoded(parsed[name]) for name in names[:4])
                 and parsed["offer.json"] == offer, "Accepted enrollment artifacts differ from their immutable originals")
        _require(enrollment._timestamp(offer["issued_at"]) <= moment,
                 "Accepted enrollment offer is in the local future")
        request, response = parsed["request.json"], parsed["response.json"]
        certificate = enrollment._response(offer, request, response, moment)
        enrollment._valid(enrollment._cert(offer["server_certificate"]), moment)
        # Retain only an ephemeral digest for the final drift check; it is
        # never returned, logged, or included in any public/private receipt.
        key_digest = hashlib.sha256(enrollment._read(directory / "client.key")).digest()
        key = enrollment._key(directory / "client.key")
        _require(enrollment._public_key(key.public_key()) == enrollment._public_key(certificate.public_key()),
                 "Accepted client certificate differs from the local private key")
        expected = dict(version="scout-atlas-authority-binding-v1", participant_id="pc-new", stream="scout-15m",
            endpoint=offer["endpoint"], server_identity=offer["server_identity"], account_fingerprint=offer["account_fingerprint"],
            symbols=offer["symbols"], client_certificate=str(directory / "client.pem"), private_key=str(directory / "client.key"),
            server_ca=str(directory / "ca.pem"), client_certificate_sha256=hashlib.sha256(response["client_certificate"].encode("ascii")).hexdigest(),
            server_ca_sha256=hashlib.sha256(offer["ca_certificate"].encode("ascii")).hexdigest(), timeout_seconds=10.0)
        _require(originals["scout-binding.json"] == enrollment.encoded(expected)
                 and originals["client.pem"] == response["client_certificate"].encode("ascii")
                 and originals["ca.pem"] == offer["ca_certificate"].encode("ascii"),
                 "Accepted v1 binding or public certificate bytes changed")
        value = dict(expected, version=BINDING_VERSION, server_certificate_sha256=offer["server_sha256"])
        fixed = {"version", "participant_id", "stream"}
        binding = ScoutAuthorityBinding(**{name: tuple(data) if name == "symbols" else data
            for name, data in value.items() if name not in fixed})
        # Check every accepted public artifact again before the immutable write.
        _require(all(enrollment._read(directory / name) == data for name, data in originals.items())
                 and hashlib.sha256(enrollment._read(directory / "client.key")).digest() == key_digest
                 and hashlib.sha256(enrollment._read(receipt_path)).hexdigest() == offer_receipt_sha256,
                 "Accepted enrollment changed during conversion")
        enrollment._write(destination, enrollment.encoded(value))
        _require(load_binding(destination) == binding, "Constructed v2 binding failed local validation")
        return binding


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare an inert Scout v2 binding from accepted private enrollment")
    parser.add_argument("--input", type=Path, required=True, help="Private parameters including pinned receipt and separate destination")
    args = parser.parse_args(argv)
    try:
        from . import admission_enrollment as enrollment
        _, parameters = _read_json(enrollment, args.input)
        _require(set(parameters) == _PARAMETERS, "Exact private conversion parameters required")
        construct_v2_binding(**parameters)
    except Exception as error:
        print(json.dumps(dict(status="FAILED", error_type=type(error).__name__, error="scout_binding_conversion_failed")))
        return 1
    print(json.dumps(dict(status="SCOUT_V2_BINDING_PREPARED", orders_or_commitments_created=0)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
