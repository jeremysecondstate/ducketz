"""Dormant HTTPS/mTLS transport for the shared account admission service.

Importing this module opens no sockets or books. A caller must supply private
certificate bindings and explicitly construct/serve the server. Certificate
identity is independent of request bodies; a client cannot select its identity.
No broker calls, redirects, automatic retries or deployment hooks live here.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import re
import ssl
import _ssl
from types import MappingProxyType
from urllib.parse import urlsplit


PROTOCOL_VERSION = "intraday-shared-admission-v1"
MAX_JSON_BYTES = 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10.0
_POST_ROUTES = MappingProxyType({
    "/v1/reserve": "reserve",
    "/v1/authorize-send": "authorize_send",
    "/v1/status": "status",
    "/v1/evidence": "evidence",
    "/v1/cancel-intent": "cancel_intent",
    "/v1/release-unsent": "release_unsent",
    "/v1/readiness": "readiness",
    "/v1/accounting": "accounting",
})
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
_FINGERPRINT = re.compile(r"[a-f0-9]{64}", re.ASCII)


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


class CertificateParticipants:
    """Immutable, locally configured SHA256(DER leaf certificate) bindings."""

    def __init__(self, bindings: Mapping[str, str]):
        if not isinstance(bindings, Mapping) or not bindings:
            raise _fail("invalid_certificate_bindings")
        copied = dict(bindings)
        if any(type(key) is not str or not _FINGERPRINT.fullmatch(key)
               or type(value) is not str or not _IDENTIFIER.fullmatch(value)
               for key, value in copied.items()):
            raise _fail("invalid_certificate_bindings")
        self._bindings = MappingProxyType(copied)

    def authenticate(self, verified_der: bytes | None) -> str:
        """Call only with a certificate from a verified TLS client connection."""
        if type(verified_der) is not bytes or not verified_der:
            raise _fail("unauthorized")
        participant = self._bindings.get(hashlib.sha256(verified_der).hexdigest())
        if participant is None:
            raise _fail("unauthorized")
        return participant


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    body: bytes


def error_response(status: int, code: str) -> TransportResponse:
    return TransportResponse(status, encode_json({
        "error": code, "protocol_version": PROTOCOL_VERSION,
    }))


class AdmissionDispatcher:
    """Pure routing; participant must already be authenticated by the caller."""

    def __init__(self, service):
        self.service = service

    def dispatch(self, participant: str | None, method: str, path: str,
                 body: bytes = b"") -> TransportResponse:
        if type(participant) is not str or not _IDENTIFIER.fullmatch(participant):
            return error_response(401, "unauthorized")
        if method not in {"GET", "POST"}:
            return error_response(405, "method_not_allowed")
        if path != "/v1/capabilities" and path not in _POST_ROUTES:
            return error_response(404, "not_found")
        if (path == "/v1/capabilities") != (method == "GET"):
            return error_response(405, "method_not_allowed")
        try:
            if method == "GET":
                if body:
                    raise _fail("invalid_request")
                result = self.service.capabilities(participant)
            else:
                parsed = decode_json(body)
                result = getattr(self.service, _POST_ROUTES[path])(participant, parsed)
            return TransportResponse(200, encode_json(result))
        except AdmissionTransportError:
            return error_response(400, "invalid_request")
        except Exception:
            # Do not echo account identifiers, payloads or exception text.
            return error_response(503, "service_unavailable")


def _require_native_server_context(context_type: type) -> None:
    # Client trust wrappers can temporarily disable OpenSSL verification and
    # apply serverAuth policy after the handshake. That cannot authenticate an
    # inbound client, even when their reported verify_mode is CERT_REQUIRED.
    # Accept only the native stdlib class, never a wrapper/subclass. Do not
    # extract or replace a process-wide TLS injection to make a listener work.
    if (not isinstance(context_type, type) or context_type.__module__ != "ssl"
            or context_type.__qualname__ != "SSLContext"
            or context_type.__bases__ != (_ssl._SSLContext,)):
        raise _fail("unsupported_server_tls_context")


def _validate_context(context: ssl.SSLContext, *, server: bool) -> None:
    if server:
        _require_native_server_context(type(context))
    required = ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT
    if ((not server and not isinstance(context, ssl.SSLContext)) or context.protocol != required
            or context.verify_mode != ssl.CERT_REQUIRED
            or context.minimum_version < ssl.TLSVersion.TLSv1_2
            or (not server and not context.check_hostname)):
        raise _fail("unsafe_tls_configuration")


def server_tls_context(*, certificate: str, private_key: str,
                       client_ca: str) -> ssl.SSLContext:
    """Load private paths only on explicit setup; TLS verifies client chains."""
    context_type = ssl.SSLContext
    _require_native_server_context(context_type)
    context = context_type(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=client_ca)
    context.load_cert_chain(certfile=certificate, keyfile=private_key)
    _validate_context(context, server=True)
    return context


def client_tls_context(*, certificate: str, private_key: str,
                       server_ca: str) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=server_ca)
    context.load_cert_chain(certfile=certificate, keyfile=private_key)
    return context


def _one_header(headers, name: str, *, required: bool = False):
    values = headers.get_all(name, [])
    if len(values) > 1 or (required and len(values) != 1):
        raise _fail("invalid_request")
    return values[0] if values else None


def _json_content_type(value: str | None) -> bool:
    return type(value) is str and value.lower().replace(" ", "") in {
        "application/json", "application/json;charset=utf-8",
    }


class AdmissionRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # One request per connection; no pipelining.

    def parse_request(self):
        valid = super().parse_request()
        if valid and self.requestline.split()[1] != self.path:
            # BaseHTTPRequestHandler normalizes a leading //; require the
            # actual wire target to match an exact route instead.
            self.send_error(400)
            return False
        return valid

    def log_message(self, _format, *args):
        pass  # No paths, private payloads or certificate identities in default logs.

    def send_error(self, code, message=None, explain=None):
        self._reply(error_response(code, "invalid_http_request"))

    def _reply(self, response: TransportResponse):
        self.close_connection = True
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Admission-Protocol", PROTOCOL_VERSION)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(response.body)

    def _handle(self):
        try:
            if self.connection.version() not in {"TLSv1.2", "TLSv1.3"}:
                raise _fail("unauthorized")
            participant = self.server.participants.authenticate(
                self.connection.getpeercert(binary_form=True))
        except Exception:
            self._reply(error_response(401, "unauthorized"))
            return
        if self.command not in {"GET", "POST"}:
            self._reply(error_response(405, "method_not_allowed"))
            return
        try:
            if (self.command == "POST" and _one_header(
                    self.headers, "X-Admission-Protocol", required=True) != PROTOCOL_VERSION):
                raise _fail("invalid_request")
            if any(self.headers.get_all(name) for name in (
                    "Transfer-Encoding", "Content-Encoding", "Expect")):
                raise _fail("invalid_request")
            length_value = _one_header(self.headers, "Content-Length",
                                       required=self.command == "POST")
            if length_value is not None and not re.fullmatch(r"0|[1-9][0-9]{0,6}", length_value):
                raise _fail("invalid_request")
            length = int(length_value or 0)
            if length > MAX_JSON_BYTES or (self.command == "GET" and length):
                raise _fail("invalid_request")
            if self.command == "POST" and not _json_content_type(
                    _one_header(self.headers, "Content-Type", required=True)):
                raise _fail("invalid_request")
            body = self.rfile.read(length) if length else b""
            if len(body) != length:
                raise _fail("invalid_request")
        except Exception:
            self._reply(error_response(400, "invalid_request"))
            return
        self._reply(self.server.dispatcher.dispatch(
            participant, self.command, self.path, body))

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _handle
    do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _handle


class AdmissionHTTPSServer(HTTPServer):
    """Explicitly constructed, serialized server; never starts itself.

    A caller supplies the private bind address and later calls serve_forever.
    The service must still use the account's shared lock across local writers.
    """

    def __init__(self, address: tuple[str, int], *, service,
                 participants: CertificateParticipants, tls_context: ssl.SSLContext,
                 timeout: float = REQUEST_TIMEOUT_SECONDS):
        _validate_context(tls_context, server=True)
        if not isinstance(participants, CertificateParticipants):
            raise _fail("invalid_certificate_bindings")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise _fail("invalid_timeout")
        self.participants = participants
        self.tls_context = tls_context
        self.request_timeout = timeout
        self.dispatcher = AdmissionDispatcher(service)
        super().__init__(address, AdmissionRequestHandler)

    def get_request(self):
        _validate_context(self.tls_context, server=True)
        connection, address = super().get_request()
        try:
            connection.settimeout(self.request_timeout)
            wrapped = self.tls_context.wrap_socket(connection, server_side=True)
            return wrapped, address
        except Exception:
            connection.close()
            raise

    def handle_error(self, request, client_address):
        # No stdlib traceback containing private request or service data.
        pass


class AdmissionHTTPSClient:
    """One verified HTTPS call per attempt, with no automatic retry or redirect.

    Lost replies remain ambiguous. The durable submission journal and service
    status operation must resolve them; this transport never repeats a POST.
    """

    def __init__(self, endpoint: str, *, tls_context: ssl.SSLContext,
                 timeout: float = REQUEST_TIMEOUT_SECONDS,
                 server_certificate_sha256: str | None = None):
        _validate_context(tls_context, server=False)
        try:
            parsed = urlsplit(endpoint)
            if (parsed.scheme != "https" or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                    or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in endpoint)
                    or parsed.port == 0):
                raise ValueError
            self.host, self.port = parsed.hostname, parsed.port or 443
        except (ValueError, TypeError):
            raise _fail("invalid_endpoint") from None
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise _fail("invalid_timeout")
        self.tls_context, self.timeout = tls_context, timeout
        if (server_certificate_sha256 is not None
                and (type(server_certificate_sha256) is not str
                     or not _FINGERPRINT.fullmatch(server_certificate_sha256))):
            raise _fail("invalid_certificate_pin")
        self.server_certificate_sha256 = server_certificate_sha256

    def call(self, method: str, path: str, payload: dict | None = None) -> dict:
        _validate_context(self.tls_context, server=False)
        if not ((method == "GET" and path == "/v1/capabilities" and payload is None)
                or (method == "POST" and path in _POST_ROUTES and type(payload) is dict)):
            raise _fail("invalid_request")
        body = None if payload is None else encode_json(payload)
        connection = None
        try:
            connection = http.client.HTTPSConnection(
                self.host, self.port, timeout=self.timeout, context=self.tls_context)
            if self.server_certificate_sha256 is not None:
                # Verify enrolled leaf before transmitting even a read-only
                # request. CA and hostname validation still run during connect.
                connection.connect()
                certificate = connection.sock.getpeercert(binary_form=True)
                if (type(certificate) is not bytes or not certificate
                        or hashlib.sha256(certificate).hexdigest() != self.server_certificate_sha256):
                    raise _fail("server_certificate_mismatch")
            connection.request(method, path, body=body, headers={
                "Content-Type": "application/json", "Accept": "application/json",
                "Connection": "close", "X-Admission-Protocol": PROTOCOL_VERSION,
            })
            response = connection.getresponse()
            if response.status != 200:
                raise _fail("remote_rejected")
            if (_one_header(response.headers, "X-Admission-Protocol", required=True) != PROTOCOL_VERSION
                    or not _json_content_type(_one_header(response.headers, "Content-Type", required=True))
                    or response.headers.get_all("Content-Encoding")
                    or response.headers.get_all("Transfer-Encoding")):
                raise _fail("invalid_response")
            length = _one_header(response.headers, "Content-Length", required=True)
            if not re.fullmatch(r"[1-9][0-9]{0,6}", length) or int(length) > MAX_JSON_BYTES:
                raise _fail("invalid_response")
            raw = response.read(MAX_JSON_BYTES + 1)
            if len(raw) != int(length):
                raise _fail("invalid_response")
            return decode_json(raw)
        except Exception:
            raise _fail("admission_call_failed") from None
        finally:
            try:
                if connection is not None:
                    connection.close()
            except Exception:
                pass
