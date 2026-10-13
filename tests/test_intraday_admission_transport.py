"""Offline transport checks: fake sockets/HTTP peers only, no listeners."""
from email.message import Message
import hashlib
from io import BytesIO
import json
import socket
import ssl
from types import SimpleNamespace

import pytest

from ml.intraday import admission_transport as transport


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network use is forbidden in transport tests")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)


class Service:
    def __init__(self):
        self.calls = []

    def capabilities(self, participant):
        self.calls.append(("capabilities", participant))
        return {"protocol_version": transport.PROTOCOL_VERSION}

    def __getattr__(self, name):
        def call(participant, body):
            self.calls.append((name, participant, body))
            return {"accepted": True}
        return call


def bindings():
    return transport.CertificateParticipants({hashlib.sha256(b"verified-client").hexdigest(): "pc-new"})


def headers(*pairs):
    result = Message()
    for name, value in pairs:
        result[name] = value
    return result


@pytest.mark.parametrize("raw", [
    b'{"x": 1, "x": 2}', b'{"nested": {"x":1,"x":2}}',
    b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}', b'{"x":1e999}',
    b'{}{}', b'{} trailing', b'[]', b'null', b'"string"', b'',
    b'{"x":"\\ud800"}', b'\xff', b'{"x":}',
    b'{"x":' + b'[' * 70 + b'0' + b']' * 70 + b'}',
    b' ' * (transport.MAX_JSON_BYTES + 1),
], ids=lambda value: hashlib.sha256(value).hexdigest()[:12])
def test_json_rejects_ambiguous_or_unbounded_payloads(raw):
    with pytest.raises(transport.AdmissionTransportError, match="^invalid_json$"):
        transport.decode_json(raw)


def test_json_roundtrip_and_exact_size_boundary():
    value = {"nested": [1, "é", None, False], "quantity": "3"}
    assert transport.decode_json(transport.encode_json(value)) == value
    raw = b'{"x":"' + b'x' * (transport.MAX_JSON_BYTES - 8) + b'"}'
    assert len(raw) == transport.MAX_JSON_BYTES
    assert transport.decode_json(raw)["x"] == "x" * (transport.MAX_JSON_BYTES - 8)


@pytest.mark.parametrize("value", [{1: "bad"}, {"x": {False: 3}}, {"x": (1, 2)}, {"x": float("nan")}])
def test_outgoing_json_rejects_non_json_values(value):
    with pytest.raises(transport.AdmissionTransportError):
        transport.encode_json(value)


def test_certificate_identity_is_fixed_immutable_and_exact():
    fingerprint = hashlib.sha256(b"verified-client").hexdigest()
    configured = {fingerprint: "pc-new"}
    identities = transport.CertificateParticipants(configured)
    configured[fingerprint] = "pc-original"
    assert identities.authenticate(b"verified-client") == "pc-new"
    for invalid in (None, b"", b"other-client", "verified-client"):
        with pytest.raises(transport.AdmissionTransportError, match="^unauthorized$"):
            identities.authenticate(invalid)


@pytest.mark.parametrize("value", [{}, {"bad": "pc-new"}, {"a" * 64: "pc new"}, {"A" * 64: "pc-new"}])
def test_bad_certificate_binding_configuration_is_rejected(value):
    with pytest.raises(transport.AdmissionTransportError):
        transport.CertificateParticipants(value)


@pytest.mark.parametrize("path,operation", list(transport._POST_ROUTES.items()))
def test_dispatcher_uses_authenticated_identity_for_every_operation(path, operation):
    service = Service()
    response = transport.AdmissionDispatcher(service).dispatch(
        "pc-new", "POST", path, b'{"participant_id":"pc-original","quantity":"3"}')
    assert response.status == 200
    assert service.calls == [(operation, "pc-new", {"participant_id": "pc-original", "quantity": "3"})]


@pytest.mark.parametrize("participant,method,path,body,status", [
    (None, "GET", "/v1/capabilities", b"", 401),
    ("", "POST", "/v1/reserve", b"{}", 401),
    ("pc-new", "PUT", "/v1/reserve", b"{}", 405),
    ("pc-new", "GET", "/v1/reserve", b"", 405),
    ("pc-new", "POST", "/v1/capabilities", b"{}", 405),
    ("pc-new", "GET", "/v1/capabilities?private=secret", b"", 404),
    ("pc-new", "POST", "/v1//reserve", b"{}", 404),
    ("pc-new", "POST", "https://host/v1/reserve", b"{}", 404),
    ("pc-new", "GET", "/v1/capabilities", b"{}", 400),
    ("pc-new", "POST", "/v1/reserve", b'{"x":1,"x":2}', 400),
])
def test_rejected_dispatch_never_calls_service(participant, method, path, body, status):
    service = Service()
    response = transport.AdmissionDispatcher(service).dispatch(participant, method, path, body)
    assert response.status == status
    assert service.calls == []
    assert b"secret" not in response.body


def test_capabilities_calls_only_read_only_service_operation():
    service = Service()
    response = transport.AdmissionDispatcher(service).dispatch("pc-new", "GET", "/v1/capabilities")
    assert response.status == 200
    assert service.calls == [("capabilities", "pc-new")]


def test_service_exception_details_are_not_disclosed():
    class Broken:
        def reserve(self, *_args):
            raise RuntimeError("private account payload and local endpoint")
    response = transport.AdmissionDispatcher(Broken()).dispatch("pc-new", "POST", "/v1/reserve", b"{}")
    assert response.status == 503
    assert json.loads(response.body) == {"error": "service_unavailable", "protocol_version": transport.PROTOCOL_VERSION}


def tls_context(server=False):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def handler(*, method="POST", path="/v1/reserve", raw=b"{}", supplied_headers=None,
            certificate=b"verified-client", version="TLSv1.3"):
    service = Service()
    instance = transport.AdmissionRequestHandler.__new__(transport.AdmissionRequestHandler)
    instance.command, instance.path = method, path
    instance.connection = SimpleNamespace(version=lambda: version,
        getpeercert=lambda binary_form: certificate)
    instance.server = SimpleNamespace(participants=bindings(), dispatcher=transport.AdmissionDispatcher(service))
    instance.headers = supplied_headers if supplied_headers is not None else headers(
        ("Content-Type", "application/json"), ("Content-Length", str(len(raw))),
        ("X-Admission-Protocol", transport.PROTOCOL_VERSION))
    instance.rfile = BytesIO(raw)
    responses = []
    instance._reply = responses.append
    return instance, service, responses


@pytest.mark.parametrize("supplied_headers,raw", [
    (headers(("Content-Type", "application/json")), b"{}"),
    (headers(("Content-Length", "2")), b"{}"),
    (headers(("Content-Length", "2"), ("Content-Length", "2"), ("Content-Type", "application/json")), b"{}"),
    (headers(("Content-Length", "2"), ("Content-Type", "application/json"), ("Content-Type", "application/json")), b"{}"),
    (headers(("Content-Length", "2"), ("Content-Type", "text/plain")), b"{}"),
    (headers(("Content-Length", "2"), ("Content-Type", "application/json"), ("Transfer-Encoding", "chunked")), b"{}"),
    (headers(("Content-Length", "2"), ("Content-Type", "application/json"), ("Content-Encoding", "gzip")), b"{}"),
    (headers(("Content-Length", "2"), ("Content-Type", "application/json"), ("Expect", "100-continue")), b"{}"),
    (headers(("Content-Length", "-1"), ("Content-Type", "application/json")), b"{}"),
    (headers(("Content-Length", "02"), ("Content-Type", "application/json")), b"{}"),
    (headers(("Content-Length", "1048577"), ("Content-Type", "application/json")), b"{}"),
    (headers(("Content-Length", "3"), ("Content-Type", "application/json")), b"{}"),
])
def test_http_rejects_ambiguous_framing_before_service(supplied_headers, raw):
    supplied_headers["X-Admission-Protocol"] = transport.PROTOCOL_VERSION
    instance, service, responses = handler(supplied_headers=supplied_headers, raw=raw)
    instance._handle()
    assert responses[0].status == 400
    assert service.calls == []


@pytest.mark.parametrize("certificate,version", [(None, "TLSv1.3"), (b"unbound", "TLSv1.3"), (b"verified-client", "TLSv1.1")])
def test_http_auth_failure_does_not_read_body_or_call_service(certificate, version):
    instance, service, responses = handler(certificate=certificate, version=version)
    instance._handle()
    assert responses[0].status == 401
    assert instance.rfile.tell() == 0
    assert service.calls == []


def test_http_unsupported_method_does_not_read_body():
    instance, service, responses = handler(method="DELETE")
    instance._handle()
    assert responses[0].status == 405
    assert instance.rfile.tell() == 0
    assert service.calls == []


def test_http_exact_json_request_and_capability_probe():
    instance, service, responses = handler()
    instance._handle()
    assert responses[0].status == 200
    assert service.calls == [("reserve", "pc-new", {})]
    instance, service, responses = handler(method="GET", path="/v1/capabilities", raw=b"")
    instance._handle()
    assert responses[0].status == 200
    assert service.calls == [("capabilities", "pc-new")]


@pytest.mark.parametrize("path", list(transport._POST_ROUTES))
@pytest.mark.parametrize("protocol_headers", [
    (),
    (("X-Admission-Protocol", ""),),
    (("X-Admission-Protocol", "intraday-shared-admission-v2"),),
    (("X-Admission-Protocol", transport.PROTOCOL_VERSION),
     ("x-admission-protocol", transport.PROTOCOL_VERSION)),
    (("X-Admission-Protocol", transport.PROTOCOL_VERSION),
     ("X-Admission-Protocol", "intraday-shared-admission-v2")),
], ids=["missing", "empty", "incompatible", "duplicate-same", "duplicate-conflicting"])
def test_every_post_requires_one_exact_protocol_header_before_body_or_service(path, protocol_headers):
    supplied = headers(("Content-Type", "application/json"), ("Content-Length", "2"), *protocol_headers)
    instance, service, responses = handler(path=path, supplied_headers=supplied)
    instance._handle()
    assert responses[0].status == 400
    assert service.calls == []
    assert instance.rfile.tell() == 0


@pytest.mark.parametrize("path,operation", list(transport._POST_ROUTES.items()))
def test_every_post_accepts_exact_protocol_header_after_authentication(path, operation):
    instance, service, responses = handler(path=path)
    instance._handle()
    assert responses[0].status == 200
    assert service.calls == [(operation, "pc-new", {})]


def test_read_only_capabilities_remains_available_without_protocol_header():
    instance, service, responses = handler(method="GET", path="/v1/capabilities", raw=b"",
                                           supplied_headers=headers(("Content-Length", "0")))
    instance._handle()
    assert responses[0].status == 200
    assert service.calls == [("capabilities", "pc-new")]


@pytest.mark.parametrize("server", [False, True])
def test_tls_rejects_missing_peer_verification(server):
    context = tls_context(server)
    if not server:
        context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(transport.AdmissionTransportError, match="unsafe_tls_configuration"):
        transport._validate_context(context, server=server)


def test_client_requires_hostname_verification_and_exact_tls_role():
    context = tls_context()
    context.check_hostname = False
    with pytest.raises(transport.AdmissionTransportError):
        transport._validate_context(context, server=False)
    with pytest.raises(transport.AdmissionTransportError):
        transport._validate_context(tls_context(True), server=False)


def test_context_builders_require_mutual_authentication(monkeypatch):
    loaded = []
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", lambda self, **kw: loaded.append(("chain", kw)))
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", lambda self, **kw: loaded.append(("trust", kw)))
    server = transport.server_tls_context(certificate="certificate", private_key="key", client_ca="client-ca")
    client = transport.client_tls_context(certificate="certificate", private_key="key", server_ca="server-ca")
    transport._validate_context(server, server=True)
    transport._validate_context(client, server=False)
    assert loaded == [("trust", {"cafile": "client-ca"}), ("chain", {"certfile": "certificate", "keyfile": "key"}),
                      ("trust", {"cafile": "server-ca"}), ("chain", {"certfile": "certificate", "keyfile": "key"})]


def test_server_construction_is_explicit_and_does_not_call_service(monkeypatch):
    captured = []
    monkeypatch.setattr(transport.HTTPServer, "__init__", lambda self, *args: captured.append(args))
    service = Service()
    server = transport.AdmissionHTTPSServer(("test.invalid", 4443), service=service,
        participants=bindings(), tls_context=tls_context(True))
    assert captured == [(("test.invalid", 4443), transport.AdmissionRequestHandler)]
    assert server.dispatcher.service is service
    assert service.calls == []


def test_server_sets_handshake_timeout_and_closes_failed_tls_socket(monkeypatch):
    events = []
    raw = SimpleNamespace(settimeout=lambda value: events.append(("timeout", value)), close=lambda: events.append("closed"))
    monkeypatch.setattr(transport.HTTPServer, "get_request", lambda self: (raw, ("test.invalid", 4443)))
    def failed_wrap(connection, *, server_side):
        assert connection is raw and server_side
        raise ssl.SSLError("certificate verification failed")
    server = transport.AdmissionHTTPSServer.__new__(transport.AdmissionHTTPSServer)
    server.request_timeout = 10
    server.tls_context = tls_context(True)
    monkeypatch.setattr(server.tls_context, "wrap_socket", failed_wrap)
    with pytest.raises(ssl.SSLError):
        server.get_request()
    assert events == [("timeout", 10), "closed"]


class FakeResponse:
    def __init__(self, *, status=200, raw=b'{"accepted":true}', changed_headers=None):
        self.status, self.raw = status, raw
        self.headers = changed_headers if changed_headers is not None else headers(
            ("Content-Type", "application/json"), ("Content-Length", str(len(raw))),
            ("X-Admission-Protocol", transport.PROTOCOL_VERSION))

    def read(self, limit):
        return self.raw[:limit]


def fake_client(monkeypatch, *, response=None, fail=False):
    events = []
    class Connection:
        def __init__(self, host, port, **kwargs):
            events.append(("connect", host, port, kwargs))
        def request(self, *args, **kwargs):
            events.append(("request", args, kwargs))
        def getresponse(self):
            if fail:
                raise OSError("private endpoint and payload")
            return response or FakeResponse()
        def close(self):
            events.append("closed")
    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    client = transport.AdmissionHTTPSClient("https://test.invalid:4443", tls_context=tls_context())
    return client, events


@pytest.mark.parametrize("endpoint", ["http://test.invalid", "https://user:pass@test.invalid", "https://test.invalid/path", "https://test.invalid?query=1", "https://test.invalid/#x", "https://test.invalid:bad", "https://test.invalid:0", "https://test.invalid\n", "https://test.invalid\x00", "https://test.invalid\x7f", ""])
def test_client_rejects_non_exact_https_endpoint(endpoint):
    with pytest.raises(transport.AdmissionTransportError, match="invalid_endpoint"):
        transport.AdmissionHTTPSClient(endpoint, tls_context=tls_context())


def test_client_verified_call_and_no_retry_after_lost_reply(monkeypatch):
    client, events = fake_client(monkeypatch)
    assert client.call("POST", "/v1/reserve", {"quantity": "3"}) == {"accepted": True}
    assert sum(isinstance(e, tuple) and e[0] == "request" for e in events) == 1
    assert events[-1] == "closed"
    assert events[0][3]["context"].check_hostname
    client, events = fake_client(monkeypatch, fail=True)
    with pytest.raises(transport.AdmissionTransportError, match="^admission_call_failed$"):
        client.call("POST", "/v1/authorize-send", {"id": "request:1"})
    assert sum(isinstance(e, tuple) and e[0] == "request" for e in events) == 1
    assert events[-1] == "closed"


@pytest.mark.parametrize("response", [
    FakeResponse(status=302), FakeResponse(status=503), FakeResponse(raw=b'{"x":1,"x":2}'),
    FakeResponse(changed_headers=headers(("Content-Type", "application/json"), ("Content-Length", "17"))),
    FakeResponse(changed_headers=headers(("Content-Type", "text/html"), ("Content-Length", "17"), ("X-Admission-Protocol", transport.PROTOCOL_VERSION))),
    FakeResponse(changed_headers=headers(("Content-Type", "application/json"), ("Content-Length", "1"), ("X-Admission-Protocol", transport.PROTOCOL_VERSION))),
    FakeResponse(changed_headers=headers(("Content-Type", "application/json"), ("Content-Length", "1048577"), ("X-Admission-Protocol", transport.PROTOCOL_VERSION))),
    FakeResponse(changed_headers=headers(("Content-Type", "application/json"), ("Content-Length", "17"), ("X-Admission-Protocol", transport.PROTOCOL_VERSION), ("Transfer-Encoding", "chunked"))),
])
def test_client_rejects_redirect_bad_framing_or_bad_json_without_retry(monkeypatch, response):
    client, events = fake_client(monkeypatch, response=response)
    with pytest.raises(transport.AdmissionTransportError, match="^admission_call_failed$"):
        client.call("GET", "/v1/capabilities")
    assert sum(isinstance(e, tuple) and e[0] == "request" for e in events) == 1
    assert events[-1] == "closed"


def test_client_rejects_arbitrary_paths_before_constructing_connection(monkeypatch):
    client, events = fake_client(monkeypatch)
    with pytest.raises(transport.AdmissionTransportError):
        client.call("POST", "/broker/orders", {})
    assert events == []


def test_mutated_client_context_is_rejected_before_connection(monkeypatch):
    client, events = fake_client(monkeypatch)
    client.tls_context.check_hostname = False
    with pytest.raises(transport.AdmissionTransportError, match="unsafe_tls_configuration"):
        client.call("GET", "/v1/capabilities")
    assert events == []


def test_wire_target_is_not_silently_normalized():
    instance, service, responses = handler()
    instance.raw_requestline = b"GET //v1/capabilities HTTP/1.1\r\n"
    instance.rfile = BytesIO(b"Host: test.invalid\r\n\r\n")
    assert instance.parse_request() is False
    assert responses[0].status == 400
    assert service.calls == []


def test_connection_constructor_failure_never_discloses_endpoint(monkeypatch):
    calls = []
    def failed_constructor(*args, **kwargs):
        calls.append(args)
        raise transport.http.client.InvalidURL("private endpoint or host detail")
    monkeypatch.setattr(transport.http.client, "HTTPSConnection", failed_constructor)
    client = transport.AdmissionHTTPSClient("https://test.invalid", tls_context=tls_context())
    with pytest.raises(transport.AdmissionTransportError, match="^admission_call_failed$"):
        client.call("GET", "/v1/capabilities")
    assert len(calls) == 1


@pytest.mark.parametrize("peer", [b"enrolled", b"other", b"", None])
def test_enrolled_leaf_pin_is_checked_before_request(monkeypatch, peer):
    events = []
    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = SimpleNamespace(getpeercert=lambda binary_form: peer)
        def connect(self):
            events.append("tls")
        def request(self, *args, **kwargs):
            events.append("request")
        def getresponse(self):
            return FakeResponse()
        def close(self):
            events.append("closed")
    monkeypatch.setattr(transport.http.client, "HTTPSConnection", Connection)
    client = transport.AdmissionHTTPSClient("https://test.invalid", tls_context=tls_context(),
        server_certificate_sha256=hashlib.sha256(b"enrolled").hexdigest())
    if peer == b"enrolled":
        client.call("POST", "/v1/readiness", {"request_nonce": "fresh"})
        assert events == ["tls", "request", "closed"]
    else:
        with pytest.raises(transport.AdmissionTransportError, match="admission_call_failed"):
            client.call("POST", "/v1/readiness", {"request_nonce": "fresh"})
        assert events == ["tls", "closed"]


def test_invalid_pin_rejected_before_connection():
    with pytest.raises(transport.AdmissionTransportError, match="invalid_certificate_pin"):
        transport.AdmissionHTTPSClient("https://test.invalid", tls_context=tls_context(),
                                       server_certificate_sha256="not-a-pin")


def test_older_service_cannot_report_view_success_or_call_mutation():
    class Older:
        def reserve(self, *args):
            raise AssertionError("view must never reserve")
    for route in ("/v1/readiness", "/v1/accounting"):
        response = transport.AdmissionDispatcher(Older()).dispatch(
            "pc-new", "POST", route, b'{"request_nonce":"fresh"}')
        assert response.status == 503
        assert json.loads(response.body)["error"] == "service_unavailable"
