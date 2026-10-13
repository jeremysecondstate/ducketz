"""Scout's authenticated Atlas admission client; no broker effects or stores.

Private JSON binding format is exactly ``ScoutAuthorityBinding.payload()``.
Certificate/CA SHA-256 values pin their file bytes; private-key contents are
never read by this module or stored in receipts. Paths must be absolute on a
fixed local drive without redirected ancestors. Loading a binding is explicit;
constructing the client performs no IO. TLS construction is deferred to a call.

Only a successful authorize_send call in the original local claim invocation
can precede a POST. A saved receipt or status reply is recovery evidence, never
permission to resume a send after interruption. The executor owns that fence.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit

from .admission_transport import AdmissionHTTPSClient, client_tls_context, decode_json
from .capacity import clip_to_capacity
from .scout_books import VERSION as BOOKS_VERSION
from .scout_journal import ScoutJournal, _safe_path
from .scout_protocol import Grant, ProtocolError, Request, canonical_sha256, _timestamp


PROTOCOL_VERSION = "intraday-shared-admission-v1"
BINDING_VERSION = "scout-atlas-authority-binding-v1"
RECEIPT_VERSION = "scout-atlas-authority-receipt-v1"
CLAIM_VERSION = "scout-account-send-claim-v1"
NEGATIVE_ADMISSION_VERSION = "intraday-negative-admission-v1"
OPERATIONS = ("capabilities", "reserve", "authorize_send", "status", "evidence", "cancel_intent", "release_unsent")
PARTICIPANT, STREAM = "pc-new", "scout-15m"
_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
_ORDER = re.compile(r"[1-9][0-9]{0,63}", re.ASCII)
_STATUSES = frozenset({"RESERVED", "SUBMITTING", "UNKNOWN", "SUBMITTED", "WORKING", "PARTIAL", "FILLED", "CANCELLED", "REJECTED", "NOT_SUBMITTED"})
_BASE = {"protocol_version", "status", "request_id", "request_sha256"}


class AuthorityProtocolError(ProtocolError):
    """An authenticated response does not satisfy the exact local contract."""


def _require(condition, message):
    if not condition:
        raise AuthorityProtocolError(message)


def _matches(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _instant(value):
    return datetime.fromisoformat(_timestamp(value, "authority timestamp"))


def _now(value):
    _require(isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None,
             "Aware local clock required")
    return value.astimezone(timezone.utc)


def _local_path(value):
    _require(type(value) is str and value and not value.startswith(("\\\\", "//")), "Absolute host-local binding path required")
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts and path.name not in {"", ".", ".."}, "Absolute host-local binding path required")
    _require(not any(":" in part for part in path.parts[1:]), "Alternate data streams are not binding files")
    return path


@dataclass(frozen=True)
class ScoutAuthorityBinding:
    endpoint: str
    server_identity: str
    account_fingerprint: str
    symbols: tuple[str, ...]
    client_certificate: str
    private_key: str
    server_ca: str
    client_certificate_sha256: str
    server_ca_sha256: str
    timeout_seconds: float = 10.0

    def __post_init__(self):
        try:
            parsed = urlsplit(self.endpoint)
            valid = (type(self.endpoint) is str and parsed.scheme == "https" and parsed.hostname
                     and parsed.username is None and parsed.password is None and parsed.path in {"", "/"}
                     and not parsed.query and not parsed.fragment and parsed.port != 0
                     and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in self.endpoint))
        except (TypeError, ValueError, AttributeError):
            valid = False
        _require(valid, "Explicit HTTPS authority origin required")
        _require(_matches(self.server_identity, _ID), "Explicit server identity required")
        for value in (self.account_fingerprint, self.client_certificate_sha256, self.server_ca_sha256):
            _require(_matches(value, _HASH), "Exact account and public certificate hashes required")
        _require(type(self.symbols) is tuple and self.symbols
                 and all(type(s) is str and re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", s, re.ASCII) for s in self.symbols)
                 and len(set(self.symbols)) == len(self.symbols),
                 "Explicit unique Scout symbols required")
        for value in (self.client_certificate, self.private_key, self.server_ca):
            _local_path(value)
        _require(len({self.client_certificate, self.private_key, self.server_ca}) == 3, "Distinct certificate, key and CA files required")
        _require(type(self.timeout_seconds) in (float, int) and math.isfinite(self.timeout_seconds)
                 and 0 < self.timeout_seconds <= 60, "Finite bounded authority timeout required")

    def payload(self):
        value = asdict(self)
        value["symbols"] = list(self.symbols)
        return dict(version=BINDING_VERSION, participant_id=PARTICIPANT, stream=STREAM, **value)

    @property
    def sha256(self):
        return canonical_sha256(self.payload())


def _verify_binding_files(binding):
    for name in ("client_certificate", "private_key", "server_ca"):
        path = _safe_path(_local_path(getattr(binding, name)))
        _require(path.is_file(), "Required private authority binding file is unavailable")
        if name != "private_key":
            _require(hashlib.sha256(path.read_bytes()).hexdigest() == getattr(binding, name + "_sha256"),
                     "Public certificate file differs from reviewed binding")


def load_binding(path):
    """Read an explicit private config; never provision credentials or connect."""
    try:
        value = decode_json(_safe_path(_local_path(str(path))).read_bytes())
        fixed = dict(version=BINDING_VERSION, participant_id=PARTICIPANT, stream=STREAM)
        names = set(ScoutAuthorityBinding.__dataclass_fields__)
        _require(set(value) == names | set(fixed) and all(value[k] == v for k, v in fixed.items()),
                 "Exact Scout authority binding fields required")
        _require(type(value["symbols"]) is list, "Binding symbols must be a JSON list")
        binding = ScoutAuthorityBinding(**{k: tuple(value[k]) if k == "symbols" else value[k] for k in names})
        _verify_binding_files(binding)
        return binding
    except (OSError, TypeError, ValueError, KeyError) as exc:
        raise AuthorityProtocolError("Private Scout authority binding failed validation") from exc


@dataclass(frozen=True)
class AuthorityReceipt:
    """Immutable validated bytes plus their successful ScoutBooks write handle."""
    _json: str
    sha256: str
    path: str

    @property
    def payload(self):
        return json.loads(self._json)

    @property
    def response(self):
        return self.payload["response"]

    @property
    def saved(self):
        return dict(sha256=self.sha256, path=self.path)

    @property
    def grant(self):
        # A historical decision inside status/reserve recovery is not a fresh grant.
        value = self.response
        return Grant.from_payload(value["grant"]) if value.get("status") == "GRANTED" and "grant" in value else None


def _record(value):
    _require(type(value) is dict and set(value) == {"request", "request_sha256", "grant", "grant_sha256", "state",
             "send_token", "broker_order_id", "revoke_reason"}, "Exact audited journal record required")
    request = Request.from_payload(value["request"])
    _require(request.sha256 == value["request_sha256"], "Journal request digest differs")
    grant = None if value["grant"] is None else Grant.from_payload(value["grant"]).validate(request)
    _require(value["grant_sha256"] == (None if grant is None else grant.sha256), "Journal grant digest differs")
    state = value["state"]
    _require(type(state) is str and state in {"PREPARED", "GRANTED", "NOT_SENT", "SENDING", "UNKNOWN", "ACCEPTED"}, "Unknown journal state")
    sent = state in {"SENDING", "UNKNOWN", "ACCEPTED"}
    _require((_matches(value["send_token"], _HASH) if sent else value["send_token"] is None)
             and (grant is not None if state in {"GRANTED", "SENDING", "UNKNOWN", "ACCEPTED"} else True)
             and (_matches(value["broker_order_id"], _ORDER) if state == "ACCEPTED" else value["broker_order_id"] is None),
             "Journal send identity is inconsistent")
    return request, grant


def claim_id(record):
    """Stable public claim identity; never transmit the local journal token."""
    _record(record)
    _require(_matches(record["send_token"], _HASH), "A durable local send claim is required")
    return "scout-claim:" + hashlib.sha256(record["send_token"].encode("ascii")).hexdigest()


def _claim(value, request, grant, expected_claim, *, now):
    _require(type(value) is dict and set(value) == {"version", "reservation_id", "participant_id", "request_sha256",
             "claim_id", "authority_epoch", "holder_id", "issued_at", "expires_at", "snapshot_id"}, "Exact authority claim fields required")
    _require(value["version"] == CLAIM_VERSION and value["participant_id"] == PARTICIPANT
             and value["request_sha256"] == request.sha256 and value["reservation_id"] == grant.reservation_id
             and value["claim_id"] == expected_claim, "Authority claim identity differs")
    _require(type(value["authority_epoch"]) is int and grant.authority_epoch <= value["authority_epoch"] <= 2**63-1,
             "Authority claim epoch predates original grant")
    for name in ("holder_id", "snapshot_id"):
        _require(type(value[name]) is str and 0 < len(value[name]) <= 256
                 and value[name].strip() and not any(ord(c) < 32 for c in value[name]), "Authority claim provenance missing")
    issued, expires = _instant(value["issued_at"]), _instant(value["expires_at"])
    _require(_instant(grant.issued_at) <= issued <= now and issued < expires <= _instant(grant.expires_at),
             "Authority claim times differ from original grant")


def _base(response, request):
    _require(type(response) is dict and _BASE <= set(response) and response["protocol_version"] == PROTOCOL_VERSION
             and type(response["status"]) is str
             and response["request_id"] == request.request_id and response["request_sha256"] == request.sha256,
             "Authority response protocol or request identity differs")


def _bound(request):
    return dict(request_id=request.request_id, request_sha256=request.sha256, session_id=request.session_id)


def _capacity(value, request, *, now):
    try:
        _require(type(value) is dict, "Exact capacity decision required")
        expected = clip_to_capacity(symbol=request.symbol, side=request.side, desired_quantity=request.quantity,
            limit_price=request.limit_price, available_cash=value["available_cash"], available_shares=value["available_shares"],
            capacity_evidence_id=value["capacity_evidence_id"], observed_at=value["observed_at"], calculated_at=value["calculated_at"]).payload()
        _require(canonical_sha256(value) == canonical_sha256(expected)
                 and _instant(request.issued_at) <= _instant(value["calculated_at"]) <= now,
                 "Authority capacity decision differs from exact request")
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorityProtocolError("Invalid authority capacity decision") from exc
    return value["adjusted_quantity"]


def _negative_decision(response, request, server_identity, *, now):
    """Validate Atlas's terminal proof of absence, never a capacity release."""
    _base(response, request)
    proof = response.get("negative_admission_proof")
    fixed = dict(version=NEGATIVE_ADMISSION_VERSION, server_identity=server_identity,
        account_fingerprint=request.account_fingerprint, participant_id=PARTICIPANT, stream=STREAM,
        request_id=request.request_id, request_sha256=request.sha256, session_id=request.session_id,
        valid_until=request.valid_until, canonical_account_absent=True, canonical_native_absent=True,
        send_claim_absent=True)
    _require(type(proof) is dict and set(proof) == set(fixed) | {"decided_at"},
             "Exact negative admission proof fields required")
    _require(canonical_sha256(proof) == canonical_sha256(dict(**fixed, decided_at=proof["decided_at"])),
             "Negative admission proof scope or absence facts differ")
    try:
        decided = _instant(proof["decided_at"])
    except (ValueError, TypeError) as exc:
        raise AuthorityProtocolError("Invalid negative admission decision time") from exc
    # Atlas can replay an immutable old proof after its own clock rollback.
    # A decision in Scout's local future still fails closed; it never rearms a
    # send. Recovery can retry when the local clock reaches the decision time.
    _require(_instant(request.valid_until) <= decided <= now, "Negative admission proof is premature or future-dated")
    expected = dict(protocol_version=PROTOCOL_VERSION, status="DECLINED", request_id=request.request_id,
        request_sha256=request.sha256, reason="expired_before_admission", terminal=True, admitted=False,
        session_id=request.session_id, send_authorized=False, capacity_released=False,
        negative_admission_proof=proof, negative_admission_proof_sha256=canonical_sha256(proof))
    _require(canonical_sha256(response) == canonical_sha256(expected), "Negative admission response or proof hash differs")


def _decision(response, request, *, now, server_identity, allow_recovery=True):
    _base(response, request)
    status = response["status"]
    if status == "GRANTED":
        _require(set(response) == _BASE | {"grant", "capacity"}, "Exact grant decision required")
        grant = Grant.from_payload(response["grant"]).validate(request)
        _require(grant.grant_id == "scout-grant:" + request.sha256 and _instant(grant.issued_at) <= now
                 and _capacity(response["capacity"], request, now=now) == grant.quantity,
                 "Grant does not match capacity decision")
    elif status == "DECLINED":
        if response.get("reason") == "expired_before_admission" or "negative_admission_proof" in response:
            _negative_decision(response, request, server_identity, now=now)
        elif set(response) == _BASE | {"reason"}:
            _require(response["reason"] == "interrupted_before_paired_admission", "Unknown decline reason")
        else:
            _require(set(response) == _BASE | {"reason", "capacity"}
                     and _capacity(response["capacity"], request, now=now) == 0
                     and response["reason"] == response["capacity"]["disposition"], "Invalid zero capacity decline")
    elif status == "EXPIRED":
        _require(set(response) == _BASE | {"reason"} and response["reason"] == "expired_before_send_claim",
                 "Invalid expiration receipt")
    else:
        _require(allow_recovery and status in _STATUSES and set(response) == _BASE | {"decision", "send_authorized"}
                 and response["send_authorized"] is False, "Recovery decision cannot authorize a send")
        _decision(response["decision"], request, now=now, server_identity=server_identity, allow_recovery=False)


def validate_negative_admission_receipt(record, receipt, binding, *, now):
    """Pure validation of saved exact absence evidence for a grantless request.

    This cannot resolve a local send claim or override any known grant/order.
    The caller may close PREPARED/NOT_SENT only after this successful check.
    """
    request, grant = _record(record)
    current = _now(now)
    _require(type(binding) is ScoutAuthorityBinding and type(receipt) is AuthorityReceipt
             and grant is None and record["state"] in {"PREPARED", "NOT_SENT"}
             and request.account_fingerprint == binding.account_fingerprint and request.symbol in binding.symbols,
             "Negative admission proof requires the original bound grantless request")
    payload = receipt.payload
    _require(set(payload) == {"version", "binding_sha256", "operation", "request", "response", "received_at"}
             and payload["version"] == RECEIPT_VERSION and payload["binding_sha256"] == binding.sha256
             and payload["operation"] in {"reserve", "status"}, "Negative admission receipt binding differs")
    expected_request = request.payload() if payload["operation"] == "reserve" else _bound(request)
    _require(canonical_sha256(payload["request"]) == canonical_sha256(expected_request),
             "Negative admission receipt request differs")
    envelope = dict(version=BOOKS_VERSION, account_fingerprint=binding.account_fingerprint, participant_id=PARTICIPANT,
        stream=STREAM, kind="authority:" + payload["operation"], request_id=request.request_id, payload=payload)
    _require(canonical_sha256(envelope) == receipt.sha256 and type(receipt.path) is str and bool(receipt.path),
             "Saved negative admission receipt digest differs")
    _negative_decision(payload["response"], request, binding.server_identity, now=current)
    _require(_instant(payload["response"]["negative_admission_proof"]["decided_at"])
             <= _instant(payload["received_at"]) <= current, "Negative admission receipt time differs")
    return True


def validate_send_receipt(record, receipt, binding, *, now):
    """Pure last-moment validation, only inside the original send invocation.

    This never renews the lease/claim or proves a recovered receipt is unused.
    The executor must not call it to resume SENDING/UNKNOWN after interruption.
    """
    request, grant = _record(record)
    current = _now(now)
    _require(type(binding) is ScoutAuthorityBinding and type(receipt) is AuthorityReceipt and record["state"] == "SENDING"
             and request.account_fingerprint == binding.account_fingerprint and request.symbol in binding.symbols,
             "Original bound SENDING record required")
    payload = receipt.payload
    _require(set(payload) == {"version", "binding_sha256", "operation", "request", "response", "received_at"}
             and payload["version"] == RECEIPT_VERSION and payload["binding_sha256"] == binding.sha256
             and payload["operation"] == "authorize_send"
             and payload["request"] == dict(**_bound(request), claim_id=claim_id(record)), "Send receipt binding differs")
    envelope = dict(version=BOOKS_VERSION, account_fingerprint=binding.account_fingerprint, participant_id=PARTICIPANT,
                    stream=STREAM, kind="authority:authorize_send", request_id=request.request_id, payload=payload)
    _require(canonical_sha256(envelope) == receipt.sha256 and type(receipt.path) is str and bool(receipt.path),
             "Saved send receipt digest differs")
    response = payload["response"]
    _base(response, request)
    _require(set(response) == _BASE | {"claim"} and response["status"] == "SEND_AUTHORIZED_ONCE",
             "Only a fresh one-use authorization response can permit sending")
    _claim(response["claim"], request, grant, claim_id(record), now=current)
    _require(_instant(response["claim"]["issued_at"]) <= _instant(payload["received_at"]) <= current
             and _instant(request.issued_at) <= current < _instant(request.valid_until)
             and _instant(grant.issued_at) <= current < _instant(grant.expires_at)
             and current < _instant(response["claim"]["expires_at"]), "Send authorization expired or is in the future")
    return True


class ScoutAuthorityClient:
    def __init__(self, binding, books, *, transport=None, clock=lambda: datetime.now(timezone.utc)):
        _require(type(binding) is ScoutAuthorityBinding and books.account_fingerprint == binding.account_fingerprint,
                 "Authority client and private books must share exact account binding")
        self.binding, self.books, self.clock = binding, books, clock
        self._transport, self._capabilities = transport, None
        self._send_attempts = set()

    claim_id = staticmethod(claim_id)

    def _call(self, method, path, body=None):
        if self._transport is None:
            _verify_binding_files(self.binding)
            context = client_tls_context(certificate=self.binding.client_certificate,
                private_key=self.binding.private_key, server_ca=self.binding.server_ca)
            _verify_binding_files(self.binding)
            self._transport = AdmissionHTTPSClient(self.binding.endpoint, tls_context=context, timeout=self.binding.timeout_seconds)
        # An injected offline transport must implement the same exact, no-retry call.
        response = self._transport.call(method, path, deepcopy(body))
        _require(type(response) is dict, "Authority response must be an exact JSON object")
        canonical_sha256(response)
        return deepcopy(response)

    def _request(self, request):
        _require(type(request) is Request and request.account_fingerprint == self.binding.account_fingerprint
                 and request.symbol in self.binding.symbols, "Request outside reviewed Scout account/symbol binding")
        return request

    def _save(self, operation, request_id, body, response):
        payload = dict(version=RECEIPT_VERSION, binding_sha256=self.binding.sha256, operation=operation,
                       request=deepcopy(body), response=deepcopy(response), received_at=_now(self.clock()).isoformat())
        saved = self.books.save_receipt(kind="authority:" + operation, request_id=request_id, payload=payload)
        return AuthorityReceipt(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False), saved["sha256"], saved["path"])

    def capabilities(self):
        """Authenticated identity/protocol only; explicitly not runtime readiness."""
        response = self._call("GET", "/v1/capabilities")
        expected = dict(protocol_version=PROTOCOL_VERSION, server_identity=self.binding.server_identity,
            participant_id=PARTICIPANT, stream=STREAM, account_fingerprint=self.binding.account_fingerprint,
            operations=list(OPERATIONS), read_only=True)
        _require(canonical_sha256(response) == canonical_sha256(expected), "Authority capability identity or protocol differs")
        receipt = self._save("capabilities", "capabilities:" + self.binding.sha256, None, response)
        self._capabilities = receipt
        return receipt

    def readiness(self):
        """The published v1 capability route does not prove loaded readiness."""
        raise AuthorityProtocolError("Atlas readiness route and schema have not been published")

    def _ready_transport(self):
        if self._capabilities is None:
            self.capabilities()

    def reserve(self, request):
        self._request(request)
        self._ready_transport()
        response = self._call("POST", "/v1/reserve", request.payload())
        _decision(response, request, now=_now(self.clock()), server_identity=self.binding.server_identity)
        return self._save("reserve", request.request_id, request.payload(), response)

    def authorize_send(self, record):
        request, grant = _record(record)
        self._request(request)
        _require(record["state"] == "SENDING", "Local journal must consume its one send claim first")
        identity = claim_id(record)
        _require(identity not in self._send_attempts, "An authorization attempt cannot be retried; recover status")
        current = _now(self.clock())
        _require(_instant(request.issued_at) <= current < _instant(request.valid_until)
                 and _instant(grant.issued_at) <= current < _instant(grant.expires_at), "Current request and grant required")
        self._ready_transport()
        body = dict(**_bound(request), claim_id=identity)
        self._send_attempts.add(identity)
        response = self._call("POST", "/v1/authorize-send", body)
        _base(response, request)
        _require(set(response) == _BASE | {"claim"} and response["status"] == "SEND_AUTHORIZED_ONCE",
                 "Fresh one-use authority send response required")
        _claim(response["claim"], request, grant, identity, now=_now(self.clock()))
        receipt = self._save("authorize_send", request.request_id, body, response)
        validate_send_receipt(record, receipt, self.binding, now=self.clock())
        return receipt

    def status(self, record):
        request, grant = _record(record)
        self._request(request)
        self._ready_transport()
        body = _bound(request)
        response = self._call("POST", "/v1/status", body)
        _base(response, request)
        if "negative_admission_proof" in response or response.get("reason") == "expired_before_admission":
            _require(grant is None and record["state"] in {"PREPARED", "NOT_SENT"},
                     "Negative admission proof contradicts a known grant or send")
            _negative_decision(response, request, self.binding.server_identity, now=_now(self.clock()))
        elif "decision" in response:
            _require(set(response) == _BASE | {"decision"} and response["status"] in {"PENDING", "GRANTED", "DECLINED", "EXPIRED"},
                     "Invalid pending authority decision")
            if response["decision"] is None:
                _require(response["status"] == "PENDING", "Missing authority decision")
            else:
                _decision(response["decision"], request, now=_now(self.clock()),
                          server_identity=self.binding.server_identity, allow_recovery=False)
                _require(response["status"] == response["decision"]["status"], "Status decision differs")
        elif response["status"] == "EXPIRED":
            _decision(response, request, now=_now(self.clock()), server_identity=self.binding.server_identity)
        else:
            _require(grant is not None and set(response) == _BASE | {"reservation_id", "broker_order_id", "filled_quantity", "native_status", "claim", "send_authorized"}
                     and response["status"] in _STATUSES and response["send_authorized"] is False
                     and response["reservation_id"] == grant.reservation_id, "Status cannot authorize sending or change reservation")
            self._order_result(response, record, grant)
            _require(type(response["native_status"]) is str
                     and response["native_status"] in _STATUSES - {"SUBMITTING", "NOT_SUBMITTED"}, "Unknown native status")
            if response["claim"] is not None:
                _claim(response["claim"], request, grant, claim_id(record), now=_now(self.clock()))
            else:
                _require(response["status"] in {"RESERVED", "NOT_SUBMITTED"}, "Submitted status lacks its durable claim")
        return self._save("status", request.request_id, body, response)

    @staticmethod
    def _order_result(response, record, grant):
        order = response["broker_order_id"]
        _require((order is None or _matches(order, _ORDER)) and type(response["filled_quantity"]) is int
                 and 0 <= response["filled_quantity"] <= grant.quantity,
                 "Invalid exact order identity or cumulative quantity")
        _require(record["broker_order_id"] is None or order == record["broker_order_id"], "Authority exact accepted order differs")
        _require(not response["filled_quantity"] or order is not None, "Fills require an exact accepted order")
        status, filled = response["status"], response["filled_quantity"]
        if status in {"RESERVED", "SUBMITTING", "UNKNOWN", "NOT_SUBMITTED"}:
            _require(order is None and filled == 0, "Unaccepted authority state contains an order or fills")
        else:
            _require(order is not None, "Accepted authority state is missing exact order identity")
        _require(not (status in {"SUBMITTED", "WORKING", "REJECTED"} and filled)
                 and not (status == "PARTIAL" and not 0 < filled < grant.quantity)
                 and not (status == "FILLED" and filled != grant.quantity), "Authority cumulative status is inconsistent")

    def _event(self, record, operation, extras, *, event_id=None):
        request, grant = _record(record)
        self._request(request)
        body = {**_bound(request), **deepcopy(extras)}
        event_id = event_id or "scout-event:" + canonical_sha256(dict(operation=operation, body=body))
        _require(_matches(event_id, _ID), "Exact immutable operation event ID required")
        body["event_id"] = event_id
        self._ready_transport()
        # Preserve the exact wire attempt even when the response is lost. This
        # is evidence for the executor's durable retry policy, not a new outbox.
        self._save("attempt:" + operation, request.request_id, body, {"delivery": "not_confirmed"})
        response = self._call("POST", "/v1/" + operation.replace("_", "-"), body)
        _base(response, request)
        if operation == "evidence":
            _require(set(response) == _BASE | {"event_id", "event_sha256", "broker_order_id", "filled_quantity"}
                     and response["status"] in _STATUSES and response["event_id"] == event_id
                     and response["event_sha256"] == canonical_sha256(body), "Authority event identity differs")
            self._order_result(response, record, grant)
            if extras["kind"] == "ORDER_STATUS":
                supplied = extras["order_evidence"]
                _require(response["filled_quantity"] >= supplied["cumulative_filled_quantity"],
                         "Authority acknowledgment regressed supplied cumulative fills")
                if supplied["status"] in {"FILLED", "CANCELLED", "REJECTED"}:
                    _require(response["status"] == supplied["status"]
                             and response["filled_quantity"] == supplied["cumulative_filled_quantity"],
                             "Authority acknowledgment differs from supplied terminal evidence")
        elif operation == "cancel_intent":
            _require(set(response) == _BASE | {"broker_order_id", "order_status", "capacity_released", "broker_action_performed"}
                     and response["status"] == "CANCEL_INTENT_RECORDED" and response["broker_order_id"] == record["broker_order_id"]
                     and type(response["order_status"]) is str and response["order_status"] in _STATUSES and response["capacity_released"] is False
                     and response["broker_action_performed"] is False, "Cancellation intent is not a terminal or effect receipt")
        else:
            _require(set(response) == _BASE | {"capacity_released"} and response["status"] == "NOT_SUBMITTED"
                     and response["capacity_released"] is True, "Explicit authority release receipt required")
        return self._save(operation, request.request_id, body, response)

    def accepted(self, record, *, event_id=None):
        _record(record)
        _require(record["state"] == "ACCEPTED", "Durable exact accepted journal order required")
        return self._event(record, "evidence", dict(kind="ACCEPTED", claim_id=claim_id(record),
                           broker_order_id=record["broker_order_id"]), event_id=event_id)

    def unknown(self, record, *, event_id=None):
        _record(record)
        _require(record["state"] == "UNKNOWN", "Explicit durable UNKNOWN outcome required")
        return self._event(record, "evidence", dict(kind="UNKNOWN", claim_id=claim_id(record)), event_id=event_id)

    def order_status(self, record, evidence, *, event_id=None):
        _record(record)
        from ml.stock_trader.horizon_ledger import OrderEvidence
        _require(type(evidence) is OrderEvidence, "Exact saved native order evidence required")
        native = asdict(evidence)
        native["fills"] = list(native["fills"])
        ScoutJournal._validate_order_payload(native, record)
        _require(_instant(evidence.observed_at) <= _now(self.clock()), "Order evidence is in the future")
        value = {k: v for k, v in native.items() if k not in {"broker_status", "broker_status_description"}}
        value["complete"] = True
        return self._event(record, "evidence", dict(kind="ORDER_STATUS", claim_id=claim_id(record), order_evidence=value), event_id=event_id)

    def cancel_intent(self, record, *, reason, event_id=None):
        _record(record)
        _require(record["state"] == "ACCEPTED" and type(reason) is str and reason in {"new-forecast", "repricing", "manual-stop"},
                 "Exact accepted order and recognized cancellation intent required")
        return self._event(record, "cancel_intent", dict(claim_id=claim_id(record),
            broker_order_id=record["broker_order_id"], reason=reason), event_id=event_id)

    def release_unsent(self, record, *, status_receipt=None, event_id=None):
        request, grant = _record(record)
        _require(grant is not None, "A durable grant is required for paired release")
        if record["state"] == "NOT_SENT":
            _require(record["send_token"] is None, "Revoked grant must be unclaimed locally")
            proof = canonical_sha256(dict(kind="scout-durable-not-sent", record=record))
        else:
            # UNKNOWN is a permanent local no-retry fence. An ordinary SENDING
            # record can still belong to its original caller and is insufficient.
            _require(record["state"] == "UNKNOWN" and type(status_receipt) is AuthorityReceipt,
                     "Durable UNKNOWN fence and authenticated unclaimed status required")
            payload = status_receipt.payload
            _require(set(payload) == {"version", "binding_sha256", "operation", "request", "response", "received_at"},
                     "Exact saved status receipt fields required")
            expected = dict(version=BOOKS_VERSION, account_fingerprint=self.binding.account_fingerprint,
                participant_id=PARTICIPANT, stream=STREAM, kind="authority:status", request_id=request.request_id, payload=payload)
            _require(canonical_sha256(expected) == status_receipt.sha256
                     and payload["version"] == RECEIPT_VERSION and payload["binding_sha256"] == self.binding.sha256
                     and payload["operation"] == "status" and payload["request"] == _bound(request),
                     "Exact saved status receipt required for abandoned local claim")
            result = payload["response"]
            _base(result, request)
            _require(result.get("status") == "RESERVED" and result.get("reservation_id") == grant.reservation_id
                     and result.get("native_status") == "RESERVED" and result.get("claim") is None
                     and result.get("broker_order_id") is None and result.get("filled_quantity") == 0
                     and result.get("send_authorized") is False, "Authority has not confirmed absence of a remote claim")
            proof = canonical_sha256(dict(kind="scout-abandoned-local-claim", record=record, status_sha256=status_receipt.sha256))
        return self._event(record, "release_unsent", dict(proof_sha256=proof), event_id=event_id)
