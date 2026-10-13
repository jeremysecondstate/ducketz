"""Explicit private mTLS enrollment, transported by the existing nightly exchange.

No network, broker, service start or automatic credential rotation occurs here.
Keys stay in host-local files. An offer is trusted only when its exact digest
comes from the locally authorized private exchange (or explicit local review),
never because a response packet asserts a CA fingerprint. A new enrollment
directory and explicit predecessor are required for rotation; old identities
and bindings remain immutable. Interrupted, incomplete state fails closed.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from filelock import FileLock

from .scout_journal import _safe_path


HOST_VERSION = "intraday-admission-host-binding-v1"
VERSIONS = {"admission_offer": "intraday-admission-enrollment-offer-v1",
            "admission_csr": "intraday-admission-enrollment-request-v1",
            "admission_cert": "intraday-admission-enrollment-response-v1"}
COMMON = {"version", "enrollment_id", "nonce", "previous_enrollment_id", "server_identity",
          "account_fingerprint", "participant_id", "stream", "symbols"}
FIELDS = {"admission_offer": COMMON | {"endpoint", "ca_certificate", "ca_sha256", "server_certificate",
                                      "server_sha256", "issued_at", "expires_at"},
          "admission_csr": COMMON | {"offer_sha256", "csr"},
          "admission_cert": COMMON | {"offer_sha256", "request_sha256", "client_certificate"}}
HOST_FIELDS = {"schema_version", "server_identity", "account_fingerprint", "scout_symbols", "bind_host", "bind_port",
               "endpoint", "server_certificate", "server_private_key", "client_ca", "client_certificate_sha256",
               "client_participant_id", "enrollment_id"}
CLIENT_URI = "urn:ducketz:participant:pc-new"
_HEX = re.compile(r"[0-9a-f]{64}", re.ASCII)
_ENROLL = re.compile(r"[0-9a-f]{32}", re.ASCII)
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
MAX_BYTES = 65536


class EnrollmentError(ValueError):
    """Enrollment did not satisfy its immutable local trust boundary."""


def _require(condition, message):
    if not condition:
        raise EnrollmentError(message)


def _matches(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def payload_sha256(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def _json(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            _require(key not in value, "Duplicate enrollment field")
            value[key] = item
        return value
    _require(len(data) <= MAX_BYTES, "Enrollment document exceeds its size limit")
    value = json.loads(data, object_pairs_hook=unique,
                       parse_constant=lambda _: (_ for _ in ()).throw(EnrollmentError("Nonfinite enrollment value")))
    _require(type(value) is dict, "Enrollment must be a JSON object")
    return value


def _now(now):
    value = datetime.now(timezone.utc) if now is None else now
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None,
             "Aware enrollment clock required")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _timestamp(value):
    _require(type(value) is str, "Enrollment timestamp required")
    parsed = datetime.fromisoformat(value)
    _require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0), "UTC timestamp required")
    return parsed


def _endpoint(value):
    try:
        result = urlsplit(value)
        valid = (type(value) is str and result.scheme == "https" and result.hostname
                 and result.username is None and result.password is None and result.path in {"", "/"}
                 and not result.query and not result.fragment and result.port != 0
                 and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value))
    except (TypeError, ValueError, AttributeError):
        valid = False
    _require(valid, "Exact HTTPS authority origin required")
    return result


def _symbols(value):
    _require(type(value) is list and len(value) == 11 and len(set(value)) == 11
             and all(type(s) is str and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,14}", s, re.ASCII) for s in value),
             "Scout's exact eleven symbols are required")


def _public_key(value):
    return value.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _cert(pem):
    _require(type(pem) is str and len(pem) < 8192, "Bounded public certificate required")
    certificate = x509.load_pem_x509_certificate(pem.encode("ascii"))
    _require(certificate.public_bytes(serialization.Encoding.PEM).decode("ascii") == pem,
             "One canonical public certificate is required")
    _require(isinstance(certificate.public_key(), ec.EllipticCurvePublicKey)
             and isinstance(certificate.public_key().curve, ec.SECP256R1), "P-256 certificate required")
    _require(isinstance(certificate.signature_hash_algorithm, hashes.SHA256), "SHA-256 certificate signature required")
    return certificate


def _pem(certificate):
    return certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _fingerprint(certificate):
    return certificate.fingerprint(hashes.SHA256()).hex()


def _valid(certificate, now):
    _require(certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc,
             "Certificate is not currently valid")


def _ca(certificate, now=None):
    _require(certificate.subject == certificate.issuer, "Enrollment CA must be self-issued")
    certificate.verify_directly_issued_by(certificate)
    _require(certificate.extensions.get_extension_for_class(x509.BasicConstraints).value == x509.BasicConstraints(True, 0),
             "Enrollment CA cannot delegate signing authority")
    usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    _require(usage.key_cert_sign and usage.crl_sign, "CA signing usage required")
    if now is not None:
        _valid(certificate, now)


def _leaf(certificate, ca, *, uri, purpose, host=None, now=None):
    certificate.verify_directly_issued_by(ca)
    _require(certificate.not_valid_before_utc >= ca.not_valid_before_utc
             and certificate.not_valid_after_utc <= ca.not_valid_after_utc, "Certificate exceeds CA validity")
    _require(certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca is False,
             "Leaf certificate cannot be a CA")
    _require(list(certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == [purpose],
             "Certificate purpose differs")
    usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
    _require(usage.digital_signature and not usage.key_cert_sign and not usage.crl_sign, "Leaf key usage differs")
    names = [x509.UniformResourceIdentifier(uri)]
    if host is not None:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            names.append(x509.DNSName(host))
    _require(list(certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value) == names,
             "Certificate identity or endpoint SAN differs")
    if now is not None:
        _valid(certificate, now)


def validate_public_packet(kind, value):
    """Strict export allowlist, also checked on receipt before any cache write."""
    _require(kind in FIELDS and type(value) is dict and set(value) == FIELDS[kind], "Exact enrollment packet fields required")
    _require(len(encoded(value)) <= MAX_BYTES and b"PRIVATE KEY" not in encoded(value), "Private keys cannot enter an exchange packet")
    _require(value["version"] == VERSIONS[kind] and value["participant_id"] == "pc-new"
             and value["stream"] == "scout-15m", "Enrollment participant or protocol differs")
    _require(_matches(value["enrollment_id"], _ENROLL) and _matches(value["nonce"], _HEX), "Enrollment identity and nonce required")
    previous = value["previous_enrollment_id"]
    _require(previous is None or (_matches(previous, _ENROLL) and previous != value["enrollment_id"]), "Explicit distinct rotation predecessor required")
    _require(_matches(value["server_identity"], _IDENTITY) and _matches(value["account_fingerprint"], _HEX), "Pinned authority scope required")
    _symbols(value["symbols"])
    if kind == "admission_offer":
        host = _endpoint(value["endpoint"]).hostname
        ca, server = _cert(value["ca_certificate"]), _cert(value["server_certificate"])
        _require(value["ca_sha256"] == _fingerprint(ca) and value["server_sha256"] == _fingerprint(server), "Certificate fingerprint differs")
        _ca(ca)
        _leaf(server, ca, uri="urn:ducketz:authority:" + value["server_identity"], purpose=ExtendedKeyUsageOID.SERVER_AUTH, host=host)
        issued, expires = _timestamp(value["issued_at"]), _timestamp(value["expires_at"])
        _require(timedelta(0) < expires - issued <= timedelta(days=1)
                 and server.not_valid_before_utc <= issued < expires <= server.not_valid_after_utc,
                 "Enrollment offer window differs")
    elif kind == "admission_csr":
        _require(_matches(value["offer_sha256"], _HEX), "Exact offer hash required")
        _csr(value["csr"])
    else:
        _require(_matches(value["offer_sha256"], _HEX) and _matches(value["request_sha256"], _HEX), "Exact offer and request hashes required")
        _cert(value["client_certificate"])
    return value


def _csr(pem):
    _require(type(pem) is str and len(pem) < 8192, "Bounded public CSR required")
    csr = x509.load_pem_x509_csr(pem.encode("ascii"))
    _require(csr.public_bytes(serialization.Encoding.PEM).decode("ascii") == pem and csr.is_signature_valid,
             "One canonical signed CSR required")
    _require(csr.subject == x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pc-new")])
             and len(csr.extensions) == 1
             and list(csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value) == [x509.UniformResourceIdentifier(CLIENT_URI)],
             "CSR must bind only pc-new")
    _require(isinstance(csr.public_key(), ec.EllipticCurvePublicKey)
             and isinstance(csr.public_key().curve, ec.SECP256R1)
             and isinstance(csr.signature_hash_algorithm, hashes.SHA256), "P-256/SHA-256 CSR required")
    return csr


def _private_permissions(path, *, create=False, directory=False):
    """Protect before writing secrets, and verify existing keys without repair."""
    if os.name != "nt":
        if create:
            os.chmod(path, 0o700 if directory else 0o600)
        _require(stat.S_IMODE(path.stat().st_mode) == (0o700 if directory else 0o600), "Private key requires owner-only permissions")
        _require(path.stat().st_uid == os.getuid(), "Private key owner differs")
        return
    # Pass the path only as data. No key bytes, shell interpolation or remote ACLs.
    script = r"""
$ErrorActionPreference = 'Stop'
$p = $env:DUCKETZ_ENROLLMENT_PATH
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
if ($env:DUCKETZ_ENROLLMENT_CREATE -eq '1') {
  if ($env:DUCKETZ_ENROLLMENT_DIRECTORY -eq '1') { $a = New-Object Security.AccessControl.DirectorySecurity }
  else { $a = New-Object Security.AccessControl.FileSecurity }
  $a.SetOwner($sid)
  $a.SetAccessRuleProtection($true, $false)
  if ($env:DUCKETZ_ENROLLMENT_DIRECTORY -eq '1') {
    $a.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    [System.IO.Directory]::SetAccessControl($p, $a)
  } else {
    $a.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')))
    [System.IO.File]::SetAccessControl($p, $a)
  }
}
if ($env:DUCKETZ_ENROLLMENT_DIRECTORY -eq '1') { $a = [System.IO.Directory]::GetAccessControl($p) }
else { $a = [System.IO.File]::GetAccessControl($p) }
$rules = @($a.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
if (-not $a.AreAccessRulesProtected -or $a.GetOwner([Security.Principal.SecurityIdentifier]).Value -ne $sid.Value -or $rules.Count -ne 1 -or $rules[0].IdentityReference.Value -ne $sid.Value -or $rules[0].AccessControlType -ne 'Allow' -or $rules[0].FileSystemRights -ne 'FullControl') { throw 'Key ACL differs' }
"""
    environment = dict(os.environ, DUCKETZ_ENROLLMENT_PATH=str(path), DUCKETZ_ENROLLMENT_CREATE="1" if create else "0",
                       DUCKETZ_ENROLLMENT_DIRECTORY="1" if directory else "0")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                             base64.b64encode(script.encode("utf-16-le")).decode("ascii")],
                            env=environment, capture_output=True, timeout=20, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _require(result.returncode == 0, "Private key ACL could not be established or verified")


def _read(path):
    path = _safe_path(Path(path))
    with path.open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    _require(len(data) <= MAX_BYTES, "Local enrollment file exceeds its size limit")
    return data


def _write(path, data, *, secret=False):
    path = _safe_path(Path(path))
    _require(len(data) <= MAX_BYTES, "Local enrollment file exceeds its size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(path)
    if secret:
        # The parent is protected *before* opening a new empty key, so no other
        # principal can retain a readable handle across its file ACL change.
        _private_permissions(path.parent, directory=True)
    if path.exists():
        if secret:
            _private_permissions(path)
        _require(_read(path) == data, "Existing enrollment bytes are immutable")
        return path
    # Exclusivity preserves interrupted identities; partial files are never
    # replaced, retried with a fresh key, or accepted as completed credentials.
    with path.open("xb") as stream:
        if secret:
            _private_permissions(path, create=True)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def _key(path):
    path = _safe_path(Path(path))
    _private_permissions(path)
    key = serialization.load_pem_private_key(_read(path), password=None)
    _require(isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1), "Local P-256 private key required")
    return key


def _new_key(path):
    key = ec.generate_private_key(ec.SECP256R1())
    _write(path, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()), secret=True)
    return key


def _lock(directory):
    directory = Path(directory)
    path = _safe_path(directory / "enrollment.lock")
    try:
        directory.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        _private_permissions(directory, directory=True)
    else:
        _private_permissions(directory, create=True, directory=True)
    return FileLock(str(path), timeout=0)


def _common(value, kind):
    return {**{k: value[k] for k in COMMON}, "version": VERSIONS[kind]}


def _offer(value, now):
    validate_public_packet("admission_offer", value)
    _require(_timestamp(value["issued_at"]) <= now < _timestamp(value["expires_at"]), "Enrollment offer expired or is not current")
    _ca(_cert(value["ca_certificate"]), now)
    _valid(_cert(value["server_certificate"]), now)


def _bound(offer, packet, kind):
    validate_public_packet(kind, packet)
    _require(all(packet[k] == offer[k] for k in COMMON - {"version"})
             and packet["offer_sha256"] == payload_sha256(offer), "Enrollment response scope, nonce or offer binding differs")


def _make_certificate(subject, key, issuer, issuer_key, start, end, *, ca=False, uri=None, host=None, purpose=None):
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(key)
               .serial_number(x509.random_serial_number()).not_valid_before(start).not_valid_after(end)
               .add_extension(x509.BasicConstraints(ca, 0 if ca else None), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False)
               .add_extension(x509.KeyUsage(digital_signature=not ca, content_commitment=False, key_encipherment=False,
                                           data_encipherment=False, key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                                           encipher_only=False, decipher_only=False), critical=True))
    if not ca:
        names = [x509.UniformResourceIdentifier(uri)]
        if host is not None:
            try:
                names.append(x509.IPAddress(ipaddress.ip_address(host)))
            except ValueError:
                names.append(x509.DNSName(host))
        builder = (builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
                   .add_extension(x509.ExtendedKeyUsage([purpose]), critical=False))
    return builder.sign(issuer_key, hashes.SHA256())


def create_offer(directory, *, enrollment_id, server_identity, endpoint, account_fingerprint, symbols,
                 previous_enrollment_id=None, now=None):
    """Atlas-only explicit provisioning, into a fresh host-local directory."""
    directory, now = Path(directory), _now(now)
    parameters = dict(enrollment_id=enrollment_id, server_identity=server_identity, endpoint=endpoint,
                      account_fingerprint=account_fingerprint, symbols=list(symbols), previous_enrollment_id=previous_enrollment_id)
    _require(_matches(enrollment_id, _ENROLL) and _matches(server_identity, _IDENTITY)
             and _matches(account_fingerprint, _HEX), "Exact local enrollment scope required")
    _require(previous_enrollment_id is None or (_matches(previous_enrollment_id, _ENROLL) and previous_enrollment_id != enrollment_id), "Explicit distinct rotation predecessor required")
    _symbols(parameters["symbols"])
    host = _endpoint(endpoint).hostname
    with _lock(directory):
        if (directory / "offer.json").exists():
            value = _json(_read(directory / "offer.json"))
            _require(all(value[k] == v for k, v in parameters.items()), "Existing offer parameters changed; use explicit rotation")
            _offer(value, now)
            _require(_public_key(_key(directory / "ca.key").public_key()) == _public_key(_cert(value["ca_certificate"]).public_key())
                     and _public_key(_key(directory / "server.key").public_key()) == _public_key(_cert(value["server_certificate"]).public_key()), "Existing offer key differs")
            return value
        _require(not any(p.name != "enrollment.lock" for p in directory.iterdir()), "Incomplete enrollment must be recovered, never overwritten")
        ca_key, server_key = _new_key(directory / "ca.key"), _new_key(directory / "server.key")
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ducketz-admission-" + enrollment_id)])
        start, end = now - timedelta(minutes=5), now + timedelta(days=30)
        ca = _make_certificate(name, ca_key.public_key(), name, ca_key, start, end, ca=True)
        server = _make_certificate(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, server_identity)]), server_key.public_key(),
                                   name, ca_key, start, end, uri="urn:ducketz:authority:" + server_identity,
                                   host=host, purpose=ExtendedKeyUsageOID.SERVER_AUTH)
        value = dict(version=VERSIONS["admission_offer"], nonce=secrets.token_hex(32), participant_id="pc-new", stream="scout-15m",
                     **parameters, ca_certificate=_pem(ca), ca_sha256=_fingerprint(ca), server_certificate=_pem(server),
                     server_sha256=_fingerprint(server), issued_at=now.isoformat(), expires_at=(now + timedelta(days=1)).isoformat())
        validate_public_packet("admission_offer", value)
        _write(directory / "ca.pem", value["ca_certificate"].encode("ascii"))
        _write(directory / "server.pem", value["server_certificate"].encode("ascii"))
        _write(directory / "offer.json", encoded(value))
        return value


def create_request(directory, offer, *, trusted_offer_sha256, expected_server_identity, account_fingerprint, symbols,
                   previous_enrollment_id=None, now=None):
    """Scout creates its key after exact private-channel provenance is checked."""
    directory, now = Path(directory), _now(now)
    _offer(offer, now)
    _require(_matches(trusted_offer_sha256, _HEX) and payload_sha256(offer) == trusted_offer_sha256,
             "Offer differs from the locally authorized private-channel receipt")
    _require(offer["server_identity"] == expected_server_identity and offer["account_fingerprint"] == account_fingerprint
             and offer["symbols"] == list(symbols) and offer["previous_enrollment_id"] == previous_enrollment_id,
             "Offer differs from local scope or explicit rotation authority")
    with _lock(directory):
        if (directory / "request.json").exists():
            _require(_read(directory / "offer.json") == encoded(offer), "Previously trusted offer changed")
            value = _json(_read(directory / "request.json"))
            _bound(offer, value, "admission_csr")
            _require(_public_key(_key(directory / "client.key").public_key()) == _public_key(_csr(value["csr"]).public_key()), "Request key differs")
            return value
        _require(not any(p.name != "enrollment.lock" for p in directory.iterdir()), "Incomplete enrollment must be recovered, never overwritten")
        key = _new_key(directory / "client.key")
        csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pc-new")]))
               .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(CLIENT_URI)]), critical=False)
               .sign(key, hashes.SHA256()))
        value = dict(**_common(offer, "admission_csr"), offer_sha256=trusted_offer_sha256,
                     csr=csr.public_bytes(serialization.Encoding.PEM).decode("ascii"))
        validate_public_packet("admission_csr", value)
        _write(directory / "offer.json", encoded(offer))
        _write(directory / "request.json", encoded(value))
        return value


def _response(offer, request, response, now):
    _bound(offer, request, "admission_csr")
    _bound(offer, response, "admission_cert")
    _require(response["request_sha256"] == payload_sha256(request), "Response request binding differs")
    ca, cert = _cert(offer["ca_certificate"]), _cert(response["client_certificate"])
    _ca(ca, now)
    _leaf(cert, ca, uri=CLIENT_URI, purpose=ExtendedKeyUsageOID.CLIENT_AUTH, now=now)
    _require(cert.subject == x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pc-new")])
             and _public_key(cert.public_key()) == _public_key(_csr(request["csr"]).public_key()), "Signed client identity or key differs")
    return cert


def sign_request(directory, request, *, now=None):
    """Atlas signs only the CSR bound to its own immutable local offer."""
    directory, now = Path(directory), _now(now)
    with _lock(directory):
        offer = _json(_read(directory / "offer.json"))
        _offer(offer, now)
        _bound(offer, request, "admission_csr")
        if (directory / "response.json").exists():
            _require(_read(directory / "request.json") == encoded(request), "A different request cannot reuse enrollment identity")
            value = _json(_read(directory / "response.json"))
            _response(offer, request, value, now)
            return value
        if (directory / "request.json").exists():
            _require(_read(directory / "request.json") == encoded(request), "Pending enrollment request changed")
        ca, key = _cert(offer["ca_certificate"]), _key(directory / "ca.key")
        _require(_public_key(key.public_key()) == _public_key(ca.public_key()), "Local CA key differs")
        csr = _csr(request["csr"])
        client = _make_certificate(csr.subject, csr.public_key(), ca.subject, key,
                                   now - timedelta(minutes=5), ca.not_valid_after_utc,
                                   uri=CLIENT_URI, purpose=ExtendedKeyUsageOID.CLIENT_AUTH)
        value = dict(**_common(offer, "admission_cert"), offer_sha256=payload_sha256(offer),
                     request_sha256=payload_sha256(request), client_certificate=_pem(client))
        _response(offer, request, value, now)
        _write(directory / "request.json", encoded(request))
        _write(directory / "response.json", encoded(value))
        return value


def accept_response(directory, response, *, now=None):
    """Write an inert Scout binding; caller must separately authorize its use."""
    directory, now = Path(directory), _now(now)
    with _lock(directory):
        offer, request = (_json(_read(directory / name)) for name in ("offer.json", "request.json"))
        _offer(offer, now)
        cert = _response(offer, request, response, now)
        _require(_public_key(_key(directory / "client.key").public_key()) == _public_key(cert.public_key()), "Response differs from local private key")
        ca_data, client_data = offer["ca_certificate"].encode("ascii"), response["client_certificate"].encode("ascii")
        value = dict(version="scout-atlas-authority-binding-v1", participant_id="pc-new", stream="scout-15m",
                     endpoint=offer["endpoint"], server_identity=offer["server_identity"], account_fingerprint=offer["account_fingerprint"],
                     symbols=offer["symbols"], client_certificate=str(directory / "client.pem"), private_key=str(directory / "client.key"),
                     server_ca=str(directory / "ca.pem"), client_certificate_sha256=hashlib.sha256(client_data).hexdigest(),
                     server_ca_sha256=hashlib.sha256(ca_data).hexdigest(), timeout_seconds=10.0)
        _write(directory / "response.json", encoded(response))
        _write(directory / "ca.pem", ca_data)
        _write(directory / "client.pem", client_data)
        _write(directory / "scout-binding.json", encoded(value))
        return value


def write_host_binding(directory, *, bind_host, bind_port, now=None):
    """Materialize a separately deployable Atlas binding, never start it."""
    directory, now = Path(directory), _now(now)
    with _lock(directory):
        offer, request, response = (_json(_read(directory / name)) for name in ("offer.json", "request.json", "response.json"))
        validate_public_packet("admission_offer", offer)
        cert = _response(offer, request, response, now)
        value = dict(schema_version=HOST_VERSION, server_identity=offer["server_identity"], account_fingerprint=offer["account_fingerprint"],
                     scout_symbols=offer["symbols"], bind_host=bind_host, bind_port=bind_port, endpoint=offer["endpoint"],
                     server_certificate=str(directory / "server.pem"), server_private_key=str(directory / "server.key"),
                     client_ca=str(directory / "ca.pem"), client_certificate_sha256=_fingerprint(cert),
                     client_participant_id="pc-new", enrollment_id=offer["enrollment_id"])
        _validate_host(value, now)
        _write(directory / "host-binding.json", encoded(value))
        return value


def _validate_host(value, now):
    _require(type(value) is dict and set(value) == HOST_FIELDS and value["schema_version"] == HOST_VERSION,
             "Exact host binding fields required")
    _require(value["client_participant_id"] == "pc-new" and _matches(value["enrollment_id"], _ENROLL)
             and _matches(value["server_identity"], _IDENTITY) and _matches(value["account_fingerprint"], _HEX)
             and _matches(value["client_certificate_sha256"], _HEX), "Host binding identity differs")
    _symbols(value["scout_symbols"])
    _require(type(value["bind_host"]) is str and bool(value["bind_host"])
             and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value["bind_host"])
             and type(value["bind_port"]) is int and 0 < value["bind_port"] <= 65535, "Explicit host address and port required")
    host = _endpoint(value["endpoint"]).hostname
    paths = [value[k] for k in ("server_certificate", "server_private_key", "client_ca")]
    _require(len(set(paths)) == 3 and all(type(p) is str for p in paths), "Distinct host-local certificate paths required")
    ca, server = (_cert(_read(value[k]).decode("ascii")) for k in ("client_ca", "server_certificate"))
    _ca(ca, now)
    _leaf(server, ca, uri="urn:ducketz:authority:" + value["server_identity"], purpose=ExtendedKeyUsageOID.SERVER_AUTH, host=host, now=now)
    _require(_public_key(_key(value["server_private_key"]).public_key()) == _public_key(server.public_key()), "Server private key differs from certificate")


def load_host_binding(path, *, now=None):
    """Validate explicit local credentials only; no listener/provider construction."""
    value = _json(_read(path))
    _validate_host(value, _now(now))
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit inert private admission enrollment; no service start")
    parser.add_argument("operation", choices=("offer", "request", "sign", "accept", "host-binding"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True, help="Local operation JSON parameters or received public packet")
    args = parser.parse_args(argv)
    try:
        value = _json(_read(args.input))
        if args.operation == "offer":
            create_offer(args.directory, **value)
        elif args.operation == "request":
            create_request(args.directory, **value)
        elif args.operation == "sign":
            sign_request(args.directory, value)
        elif args.operation == "accept":
            accept_response(args.directory, value)
        else:
            write_host_binding(args.directory, **value)
    except Exception as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__, "error": "enrollment_failed"}))
        return 1
    print("Enrollment step recorded locally; no service or trading control changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
