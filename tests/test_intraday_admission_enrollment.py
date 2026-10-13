from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path

import pytest

from ml.intraday import admission_enrollment as enrollment


NOW = datetime(2026, 10, 13, 6, tzinfo=timezone.utc)
SYMBOLS = [f"S{i}" for i in range(11)]
SCOPE = "a" * 64


def offer(root, **kwargs):
    parameters = dict(enrollment_id="1" * 32, server_identity="atlas-admission-test", endpoint="https://atlas.test:9443",
                      account_fingerprint=SCOPE, symbols=SYMBOLS, now=NOW)
    parameters.update(kwargs)
    return enrollment.create_offer(root, **parameters)


def request(root, value, **kwargs):
    parameters = dict(trusted_offer_sha256=enrollment.payload_sha256(value), expected_server_identity="atlas-admission-test",
                      account_fingerprint=SCOPE, symbols=SYMBOLS, now=NOW)
    parameters.update(kwargs)
    return enrollment.create_request(root, value, **parameters)


@pytest.fixture
def local(tmp_path, monkeypatch):
    # Cryptography and immutable files are real in every case. ACL behavior is
    # exercised separately without repeating subprocesses in protocol cases.
    monkeypatch.setattr(enrollment, "_private_permissions", lambda *args, **kwargs: None)
    atlas, scout = tmp_path / "atlas", tmp_path / "scout"
    value = offer(atlas)
    req = request(scout, value)
    response = enrollment.sign_request(atlas, req, now=NOW)
    return atlas, scout, value, req, response


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and p.name != "enrollment.lock"}


def test_end_to_end_inert_bindings_retries_and_local_keys(local, monkeypatch):
    import json
    import socket
    from ml.intraday.scout_enrollment import construct_v2_binding
    from ml.intraday.scout_authority import load_binding
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("Enrollment must never connect"))
    atlas, scout, value, req, response = local
    client = enrollment.accept_response(scout, response, now=NOW)
    host = enrollment.write_host_binding(atlas, bind_host="127.0.0.1", bind_port=9443, now=NOW)
    assert enrollment.load_host_binding(atlas / "host-binding.json", now=NOW) == host
    # The original accepted v1 artifact remains immutable. Scout's reviewed
    # consumer constructs a separate v2 binding from the retained verified
    # offer receipt; its DER leaf pin is not a PEM-file or CA fingerprint.
    receipt = dict(kind="admission_offer", enrollment_id=value["enrollment_id"],
        action_date="2026-10-13", review_session="2026-10-12", packet_sha256="f" * 64,
        payload=value, trusted_payload_sha256=enrollment.payload_sha256(value))
    receipt_path = scout.parent / "verified-offer-receipt.json"
    receipt_path.write_bytes(enrollment.encoded(receipt))
    conversion = dict(directory=scout, offer_receipt_path=receipt_path,
        offer_receipt_sha256=hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        enrollment_id=value["enrollment_id"], action_date=receipt["action_date"], review_session=receipt["review_session"],
        server_identity=value["server_identity"], account_fingerprint=value["account_fingerprint"], symbols=value["symbols"],
        destination=scout / "scout-binding-v2.json", now=NOW)
    converted = construct_v2_binding(**conversion)
    assert load_binding(conversion["destination"]).payload() == dict(client,
        version="scout-atlas-authority-binding-v2", server_certificate_sha256=value["server_sha256"])
    assert json.loads((scout / "scout-binding.json").read_bytes()) == client
    assert host["client_certificate_sha256"] == enrollment._fingerprint(enrollment._cert(response["client_certificate"]))
    assert client["client_certificate_sha256"] == hashlib.sha256(response["client_certificate"].encode()).hexdigest()
    assert host["client_participant_id"] == "pc-new"
    before = snapshot(atlas.parent)
    assert offer(atlas) == value
    assert request(scout, value) == req
    assert enrollment.sign_request(atlas, req, now=NOW) == response
    assert enrollment.accept_response(scout, response, now=NOW) == client
    assert enrollment.write_host_binding(atlas, bind_host="127.0.0.1", bind_port=9443, now=NOW) == host
    assert construct_v2_binding(**conversion) == converted
    assert snapshot(atlas.parent) == before
    assert b"PRIVATE KEY" not in b"".join(enrollment.encoded(v) for v in (value, req, response))
    assert (scout / "client.key").is_file() and not (atlas / "client.key").exists()
    assert (atlas / "ca.key").is_file() and not (scout / "ca.key").exists()


@pytest.mark.parametrize("field,replacement", [("participant_id", "pc-original"), ("stream", "atlas-15m"),
    ("nonce", "0" * 64), ("account_fingerprint", "b" * 64), ("enrollment_id", "2" * 32),
    ("server_identity", "other-server"), ("offer_sha256", "0" * 64), ("previous_enrollment_id", "3" * 32)])
def test_sign_rejects_scope_nonce_and_replay(local, field, replacement):
    atlas, _, _, req, _ = local
    before = snapshot(atlas)
    changed = deepcopy(req)
    changed[field] = replacement
    with pytest.raises(ValueError):
        enrollment.sign_request(atlas, changed, now=NOW)
    assert snapshot(atlas) == before


@pytest.mark.parametrize("parameter,replacement", [("trusted_offer_sha256", "0" * 64),
    ("expected_server_identity", "other-server"), ("account_fingerprint", "b" * 64),
    ("symbols", [f"Z{i}" for i in range(11)]), ("previous_enrollment_id", "3" * 32)])
def test_request_requires_separate_local_trust(local, tmp_path, parameter, replacement):
    _, _, value, _, _ = local
    target = tmp_path / "rejected"
    with pytest.raises(ValueError):
        request(target, value, **{parameter: replacement})
    assert not target.exists()


@pytest.mark.parametrize("field,replacement", [("request_sha256", "f" * 64), ("nonce", "f" * 64),
    ("participant_id", "pc-original"), ("server_identity", "other")])
def test_response_binding_rejects_modified_packets(local, field, replacement):
    _, scout, _, _, response = local
    before = snapshot(scout)
    changed = deepcopy(response)
    changed[field] = replacement
    with pytest.raises(ValueError):
        enrollment.accept_response(scout, changed, now=NOW)
    assert snapshot(scout) == before


def test_wrong_local_key_is_not_replaced(local):
    atlas, scout, _, _, response = local
    (scout / "client.key").write_bytes((atlas / "server.key").read_bytes())
    before = snapshot(scout)
    with pytest.raises(ValueError, match="private key"):
        enrollment.accept_response(scout, response, now=NOW)
    assert snapshot(scout) == before


def test_csr_cannot_request_other_participant(local, tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    atlas, _, _, req, _ = local
    csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pc-original")]))
           .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(enrollment.CLIENT_URI)]), critical=False)
           .sign(ec.generate_private_key(ec.SECP256R1()), hashes.SHA256()))
    changed = dict(req, csr=csr.public_bytes(serialization.Encoding.PEM).decode())
    with pytest.raises(ValueError, match="pc-new"):
        enrollment.sign_request(atlas, changed, now=NOW)


def test_other_issuer_and_certificate_rejected(local, tmp_path):
    _, scout, _, _, response = local
    other_offer = offer(tmp_path / "other-atlas", enrollment_id="2" * 32)
    other_request = request(tmp_path / "other-scout", other_offer)
    other_response = enrollment.sign_request(tmp_path / "other-atlas", other_request, now=NOW)
    changed = dict(response, client_certificate=other_response["client_certificate"])
    from cryptography.exceptions import InvalidSignature
    with pytest.raises((ValueError, InvalidSignature)):
        enrollment.accept_response(scout, changed, now=NOW)
    assert not (scout / "scout-binding.json").exists()


@pytest.mark.parametrize("shift", [timedelta(days=1), timedelta(seconds=-1)])
def test_offer_window_is_enforced(local, tmp_path, shift):
    _, scout, value, _, response = local
    with pytest.raises(ValueError, match="expired|current"):
        request(tmp_path / "late", value, now=NOW + shift)
    with pytest.raises(ValueError, match="expired|current"):
        enrollment.accept_response(scout, response, now=NOW + shift)


def test_host_certificate_expiry_identity_endpoint_and_key(local):
    atlas, _, _, _, _ = local
    host = enrollment.write_host_binding(atlas, bind_host="127.0.0.1", bind_port=9443, now=NOW)
    path = atlas / "host-binding.json"
    with pytest.raises(ValueError, match="valid"):
        enrollment.load_host_binding(path, now=NOW + timedelta(days=30))
    for field, value in [("server_identity", "another"), ("endpoint", "https://another.test:9443"),
                         ("client_participant_id", "pc-original"), ("bind_port", True), ("extra", "unexpected")]:
        path.write_bytes(enrollment.encoded(dict(host, **{field: value})))
        with pytest.raises(ValueError):
            enrollment.load_host_binding(path, now=NOW)
    path.write_bytes(enrollment.encoded(host))
    (atlas / "server.key").write_bytes((atlas / "ca.key").read_bytes())
    with pytest.raises(ValueError, match="private key"):
        enrollment.load_host_binding(path, now=NOW)


def test_private_key_export_and_extra_pem_rejected(local):
    atlas, _, value, req, response = local
    for kind, original, field in [("admission_offer", value, "ca_certificate"), ("admission_csr", req, "csr"),
                                  ("admission_cert", response, "client_certificate")]:
        with pytest.raises(ValueError, match="Private keys"):
            enrollment.validate_public_packet(kind, dict(original, **{field: (atlas / "ca.key").read_text()}))
        with pytest.raises(ValueError):
            enrollment.validate_public_packet(kind, dict(original, **{field: original[field] + original[field]}))


def test_rotation_requires_fresh_directory_and_explicit_predecessor(local, tmp_path):
    atlas, scout, _, _, _ = local
    with pytest.raises(ValueError, match="rotation"):
        offer(atlas, enrollment_id="2" * 32, previous_enrollment_id="1" * 32)
    new_offer = offer(tmp_path / "rotated-atlas", enrollment_id="2" * 32, previous_enrollment_id="1" * 32)
    with pytest.raises(ValueError, match="rotation"):
        request(tmp_path / "rotated-scout", new_offer)
    value = request(tmp_path / "rotated-scout", new_offer, previous_enrollment_id="1" * 32)
    assert value["previous_enrollment_id"] == "1" * 32
    assert (atlas / "ca.key").exists() and (scout / "client.key").exists()


def test_incomplete_state_never_regenerates_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(enrollment, "_private_permissions", lambda *a, **k: None)
    target = tmp_path / "interrupted"
    target.mkdir()
    key = target / "ca.key"
    key.write_bytes(b"partial")
    with pytest.raises(ValueError, match="Incomplete"):
        offer(target)
    assert key.read_bytes() == b"partial" and not (target / "offer.json").exists()


def test_secret_permission_failure_writes_no_key_bytes(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise enrollment.EnrollmentError("permission failure")
    monkeypatch.setattr(enrollment, "_private_permissions", refuse)
    key = tmp_path / "blocked.key"
    with pytest.raises(ValueError, match="permission"):
        enrollment._new_key(key)
    assert not key.exists()


def test_real_private_key_permissions_and_content(tmp_path):
    directory = tmp_path / "protected"
    with enrollment._lock(directory):
        pass
    key_path = directory / "protected.key"
    key = enrollment._new_key(key_path)
    assert enrollment._public_key(enrollment._key(key_path).public_key()) == enrollment._public_key(key.public_key())
    enrollment._private_permissions(key_path)
    enrollment._private_permissions(directory, directory=True)
    if os.name != "nt":
        os.chmod(key_path, 0o644)
        with pytest.raises(ValueError, match="permissions"):
            enrollment._key(key_path)


@pytest.fixture
def tls_peers(tmp_path, monkeypatch):
    import ssl
    import socket
    from ml.intraday.admission_transport import client_tls_context
    from tests.intraday_tls_fixture import native_server_context
    monkeypatch.setattr(enrollment, "_private_permissions", lambda *a, **k: None)
    forbidden = lambda *a, **k: pytest.fail("Offline TLS must not open a socket")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    atlas, scout = tmp_path / "atlas", tmp_path / "scout"
    now = datetime.now(timezone.utc)
    value = offer(atlas, now=now)
    req = request(scout, value, now=now)
    response = enrollment.sign_request(atlas, req, now=now)
    enrollment.accept_response(scout, response, now=now)
    original_context = ssl.SSLContext
    server = native_server_context(monkeypatch, certificate=str(atlas / "server.pem"),
        private_key=str(atlas / "server.key"), client_ca=str(atlas / "ca.pem"))
    client = client_tls_context(certificate=str(scout / "client.pem"), private_key=str(scout / "client.key"),
                                server_ca=str(scout / "ca.pem"))
    monkeypatch.setattr(socket, "socket", forbidden)
    server.verify_flags |= ssl.VERIFY_X509_STRICT
    client.verify_flags |= ssl.VERIFY_X509_STRICT
    yield atlas, scout, response, client, server
    assert ssl.SSLContext is original_context


def test_strict_mutual_tls_handshake_in_memory_without_sockets(tls_peers):
    from tests.intraday_tls_fixture import handshake
    atlas, _, response, client, server = tls_peers
    client_ssl, server_ssl = handshake(client, server)
    assert client_ssl.version() == server_ssl.version() == "TLSv1.3"
    assert hashlib.sha256(server_ssl.getpeercert(binary_form=True)).hexdigest() == enrollment._fingerprint(enrollment._cert(response["client_certificate"]))
    assert hashlib.sha256(client_ssl.getpeercert(binary_form=True)).hexdigest() == enrollment._fingerprint(enrollment._cert((atlas / "server.pem").read_text()))


def test_actual_client_rejects_wrong_hostname(tls_peers):
    import ssl
    from tests.intraday_tls_fixture import handshake
    _, _, _, client, server = tls_peers
    with pytest.raises(ssl.SSLCertVerificationError) as result:
        handshake(client, server, hostname='foreign.test')
    assert result.value.peer_side == 'client'


def test_actual_client_rejects_foreign_ca(tls_peers, tmp_path):
    import ssl
    from ml.intraday.admission_transport import client_tls_context
    from tests.intraday_tls_fixture import handshake
    _, scout, _, _, server = tls_peers
    foreign = tmp_path / 'foreign'
    offer(foreign, now=datetime.now(timezone.utc))
    client = client_tls_context(certificate=str(scout / 'client.pem'), private_key=str(scout / 'client.key'),
                               server_ca=str(foreign / 'ca.pem'))
    with pytest.raises(ssl.SSLCertVerificationError) as result:
        handshake(client, server)
    assert result.value.peer_side == 'client'


def test_native_server_rejects_missing_client_certificate(tls_peers):
    import ssl
    from tests.intraday_tls_fixture import handshake
    _, scout, _, _, server = tls_peers
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.verify_mode = ssl.CERT_REQUIRED
    client.check_hostname = True
    client.load_verify_locations(cafile=str(scout / 'ca.pem'))
    with pytest.raises(ssl.SSLError) as result:
        handshake(client, server)
    assert result.value.peer_side == 'server'


def test_actual_server_factory_requires_supported_native_environment():
    import ssl
    import _ssl
    from ml.intraday import admission_transport as transport
    original = ssl.SSLContext
    native = (original.__module__ == 'ssl' and original.__qualname__ == 'SSLContext'
              and original.__bases__ == (_ssl._SSLContext,))
    if native:
        transport._require_native_server_context(original)
    else:
        with pytest.raises(transport.AdmissionTransportError, match='^unsupported_server_tls_context$'):
            transport.server_tls_context(certificate='missing-private-certificate',
                private_key='missing-private-key', client_ca='missing-private-ca')
    assert ssl.SSLContext is original


@pytest.mark.parametrize("endpoint", ["http://atlas.test", "https://user:password@atlas.test", "https://atlas.test/path",
                                      "https://atlas.test:0", "https://atlas.test\x00", "https://atlas.test?secret=x"])
def test_bad_endpoint_rejected_before_key_creation(tmp_path, endpoint):
    with pytest.raises(ValueError):
        offer(tmp_path / "bad", endpoint=endpoint)
    assert not (tmp_path / "bad").exists()


def test_enrollment_cli_error_redacts_private_input_details(tmp_path, monkeypatch, capsys):
    path = tmp_path / "private-parameters.json"
    path.write_bytes(b"{}\n")
    def fail(*args, **kwargs):
        raise ValueError("private endpoint https://private.test:9443 and C:/private/private.key")
    monkeypatch.setattr(enrollment, "create_offer", fail)
    assert enrollment.main(["offer", "--directory", str(tmp_path / "keys"), "--input", str(path)]) == 1
    output = capsys.readouterr()
    assert output.err == "" and "private" not in output.out and "https://" not in output.out
    assert enrollment._json(output.out.encode())["error"] == "enrollment_failed"
