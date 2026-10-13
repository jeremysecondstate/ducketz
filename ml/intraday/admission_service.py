"""Atlas-local admission book for authenticated Scout RPC adapters.

This module has no broker, network listener, live launcher or provider adapter.
The host supplies a reviewed context and fresh account capture; Scout retains
its own manual session and actual submission/cancellation effects. All book
operations hold the existing common stock-trader lock. Capabilities is read-only.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.account_gameplan.authority import (
    AccountAuthority, AuthorityBinding, AuthorityError, Fill,
    IntradayReservationRequest, Lease, OrderEvidence,
)
from ml.stock_trader.horizon_ledger import (
    FillEvidence, HorizonLedger, OrderEvidence as NativeOrderEvidence,
)
from .capacity import clip_to_capacity
from .scout_protocol import Grant, Request, canonical_sha256


PROTOCOL_VERSION = "intraday-shared-admission-v1"
PARTICIPANT = "pc-new"
STREAM = "scout-15m"
TARGET = "intraday-15m-open-to-third-close-not-down-v1"
OPERATIONS = ("capabilities", "reserve", "authorize_send", "status", "evidence", "cancel_intent", "release_unsent")


class AdmissionError(ValueError):
    pass


@dataclass(frozen=True)
class BookOperation:
    authority: AccountAuthority
    ledger: HorizonLedger
    lease: Lease


@dataclass(frozen=True)
class BookSnapshot:
    snapshot_id: str
    observed_at: str
    pending_sell_shares: Mapping[str, int] = field(default_factory=dict)
    broker_pending_sell_orders: Mapping[str, Mapping[str, int]] = field(default_factory=dict)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _packed(value):
    return _encoded({"payload": value, "sha256": canonical_sha256(value)})


def _unpacked(raw):
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {"payload", "sha256"}
            or canonical_sha256(value["payload"]) != value["sha256"]):
        raise AdmissionError("IMMUTABLE_JOURNAL_VALUE_CHANGED")
    return value["payload"]


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise AdmissionError("INVALID_OPERATION_IDENTITY")
    return value


class AdmissionService:
    def __init__(self, *, datastore, binding: AuthorityBinding, server_identity: str,
                 compatibility: Callable, context_factory: Callable, refresh: Callable,
                 clock=lambda: datetime.now(timezone.utc)):
        if str(datastore).startswith(("\\\\", "//")):
            raise AdmissionError("HOST_LOCAL_DATASTORE_REQUIRED")
        self.root = Path(datastore).resolve()
        self.binding = AccountAuthority._binding(binding)
        if self.binding["coordinator_id"] != "pc-original" or PARTICIPANT not in self.binding["participants"]:
            raise AdmissionError("EXPLICIT_ATLAS_AND_SCOUT_BINDING_REQUIRED")
        self.server_identity = _name(server_identity)
        self.compatibility, self.context_factory, self.refresh, self.clock = compatibility, context_factory, refresh, clock
        self.path = self.root / "state/intraday-admission/journal.sqlite3"

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise AdmissionError("AWARE_AUTHORITY_CLOCK_REQUIRED")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _participant(participant):
        if participant != PARTICIPANT:
            raise AdmissionError("AUTHENTICATED_SCOUT_PARTICIPANT_REQUIRED")

    def capabilities(self, participant):
        self._participant(participant)
        return {"protocol_version": PROTOCOL_VERSION, "server_identity": self.server_identity,
                "participant_id": PARTICIPANT, "stream": STREAM,
                "account_fingerprint": self.binding["account_fingerprint"],
                "operations": list(OPERATIONS), "read_only": True}

    @contextmanager
    def _journal(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=EXTRA")
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("CREATE TABLE IF NOT EXISTS binding(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,payload TEXT NOT NULL,sha TEXT UNIQUE NOT NULL,plan TEXT,response TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,payload TEXT NOT NULL,sha TEXT NOT NULL,response TEXT)")
            binding = _encoded({"protocol_version": PROTOCOL_VERSION, "server_identity": self.server_identity, **self.binding})
            prior = db.execute("SELECT payload FROM binding WHERE id=1").fetchone()
            if prior is not None and prior[0] != binding:
                raise AdmissionError("IMMUTABLE_SERVICE_BINDING_MISMATCH")
            db.execute("INSERT OR IGNORE INTO binding VALUES(1,?)", (binding,))
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _operation(self, participant):
        self._participant(participant)
        with exclusive_runtime_lock(self.root / "locks/stock-trader-hourly.lock", process_name="stock-trader-hourly"):
            proof = self.compatibility()
            if (not isinstance(proof, Mapping) or proof.get("ready") is not True
                    or proof.get("atlas_ready") is not True or proof.get("gameplan_ready") is not True):
                raise AdmissionError("ALL_LOADED_WRITERS_MUST_SUPPORT_SHARED_SCOUT_BOOK")
            with self.context_factory() as operation:
                if (not isinstance(operation, BookOperation) or operation.authority.binding != self.binding
                        or operation.ledger.account_fingerprint != self.binding["account_fingerprint"]
                        or operation.authority.path != self.root / "state/account-gameplan/authority.sqlite3"
                        or operation.ledger.path != self.root / "state/independent-stock-trader/holdings.sqlite3"):
                    raise AdmissionError("CANONICAL_ATLAS_BOOKS_REQUIRED")
                yield operation

    def _scope(self, request):
        reference, issued, target, valid = (datetime.fromisoformat(getattr(request, name)) for name in
                                            ("reference_at", "issued_at", "target_close_at", "valid_until"))
        if (request.account_fingerprint != self.binding["account_fingerprint"]
                or request.symbol not in self.binding["participants"][PARTICIPANT]):
            raise AdmissionError("REQUEST_OUTSIDE_SCOUT_ACCOUNT_SCOPE")
        if (reference.minute % 15 or reference.second or reference.microsecond
                or target != reference + timedelta(minutes=45) or issued < reference + timedelta(minutes=15)
                or Decimal(request.limit_price) != Decimal(request.limit_price).quantize(Decimal(".01"))):
            raise AdmissionError("EXACT_TARGET_AND_CENT_PRICE_REQUIRED")

    def _current(self, request):
        self._scope(request)
        now = self._now()
        issued, valid = (datetime.fromisoformat(getattr(request, name)) for name in ("issued_at", "valid_until"))
        if not issued <= now < valid:
            raise AdmissionError("REQUEST_NOT_CURRENT")

    @staticmethod
    def _assert_never_admitted(operation, request):
        """Check current and retained book evidence before certifying absence.

        The caller holds the common account lock. A missing live row alone is
        insufficient: interrupted/corrupt books can retain reservation history
        or a once-issued send claim. This path does not release either book.
        """
        key = STREAM + ":" + request.sha256
        account_id = hashlib.sha256(_encoded([request.account_fingerprint, key]).encode()).hexdigest()
        accounts = operation.authority.read_state()["reservations"]
        account_ids = {row["reservation_id"] for row in accounts}
        natives = operation.ledger.snapshot().reservations
        native_ids = {row.reservation_id for row in natives}

        def check_reference(identity, known, expected=None):
            if not isinstance(identity, str) or not identity or identity == expected or identity not in known:
                raise AdmissionError("RETAINED_OR_ORPHAN_EFFECT_PREVENTS_NEGATIVE_PROOF")

        for row in accounts:
            if (row["reservation_id"] == account_id or row["request"].get("idempotency_key") == key
                    or row["request"].get("remote_request_sha256") == request.sha256):
                raise AdmissionError("PRIOR_ADMISSION_EVIDENCE_PREVENTS_NEGATIVE_PROOF")
        for native in natives:
            if native.idempotency_key == key:
                raise AdmissionError("PRIOR_ADMISSION_EVIDENCE_PREVENTS_NEGATIVE_PROOF")
        for receipt in operation.ledger.history():
            value = json.loads(receipt.payload_json)
            if not isinstance(value, dict):
                raise AdmissionError("NATIVE_RESERVATION_HISTORY_INVALID")
            if (receipt.kind == "reservation" and value.get("stream") == STREAM and value.get("forecast") == request.forecast_id
                    and value.get("batch") == request.session_id):
                raise AdmissionError("PRIOR_ADMISSION_EVIDENCE_PREVENTS_NEGATIVE_PROOF")
            if "reservation_id" in value or receipt.kind in {"submission", "order"}:
                check_reference(value.get("reservation_id"), native_ids)
        # Native allocation reuse prevents deriving its exact reservation ID
        # from the wire alone. Any orphan effect therefore blocks absence proof.
        with closing(sqlite3.connect(operation.ledger.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            for table in ("fills", "cancellations", "order_submission_times", "order_timeouts"):
                for (identity,) in db.execute(f"SELECT reservation FROM {table}"):
                    check_reference(identity, native_ids)
        # The public authority projection intentionally omits claim/history
        # tables. Read the canonical local book without creating or editing it.
        with closing(sqlite3.connect(operation.authority.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            for reservation, raw in db.execute("SELECT reservation,payload FROM remote_submission_claims"):
                value = json.loads(raw)
                if (not isinstance(value, dict) or value.get("reservation_id") != reservation
                        or not {"request_sha256", "participant_id", "claim_id"} <= value.keys()):
                    raise AdmissionError("REMOTE_CLAIM_HISTORY_INVALID")
                if reservation == account_id or value["request_sha256"] == request.sha256:
                    raise AdmissionError("PRIOR_ADMISSION_EVIDENCE_PREVENTS_NEGATIVE_PROOF")
                check_reference(reservation, account_ids, account_id)
            for table in ("submission_receipts", "fills", "remote_no_send_receipts"):
                for (identity,) in db.execute(f"SELECT reservation FROM {table}"):
                    check_reference(identity, account_ids, account_id)
            for (raw,) in db.execute("SELECT payload FROM order_evidence"):
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise AdmissionError("AUTHORITY_HISTORY_INVALID")
                check_reference(value.get("reservation_id"), account_ids, account_id)
            for kind, raw in db.execute("SELECT kind,payload FROM events"):
                value = json.loads(raw)
                if not isinstance(value, dict):
                    raise AdmissionError("AUTHORITY_HISTORY_INVALID")
                original = value.get("request", {})
                if kind == "intraday-reserved" and not isinstance(original, dict):
                    raise AdmissionError("AUTHORITY_HISTORY_INVALID")
                if (value.get("reservation_id") == account_id or value.get("request_sha256") == request.sha256
                        or (kind == "intraday-reserved" and (original.get("idempotency_key") == key
                            or original.get("remote_request_sha256") == request.sha256))):
                    raise AdmissionError("PRIOR_ADMISSION_EVIDENCE_PREVENTS_NEGATIVE_PROOF")
                if "reservation_id" in value:
                    check_reference(value["reservation_id"], account_ids, account_id)

    def _negative_admission(self, operation, request, plan, response):
        """Persist one terminal no-admission result for an expired exact request."""
        self._scope(request)
        if plan is not None:
            raise AdmissionError("PLANNED_ADMISSION_REQUIRES_EXISTING_RECOVERY")
        fixed = {"version": "intraday-negative-admission-v1", "server_identity": self.server_identity,
                 "account_fingerprint": request.account_fingerprint, "participant_id": PARTICIPANT,
                 "stream": STREAM, "request_id": request.request_id, "request_sha256": request.sha256,
                 "session_id": request.session_id, "valid_until": request.valid_until,
                 "canonical_account_absent": True, "canonical_native_absent": True, "send_claim_absent": True}
        if response is not None:
            proof = response.get("negative_admission_proof")
            if (not isinstance(proof, dict) or set(proof) != {*fixed, "decided_at"}
                    or any(type(proof[key]) is not type(value) or proof[key] != value for key, value in fixed.items())
                    or datetime.fromisoformat(proof["decided_at"]) < datetime.fromisoformat(request.valid_until)
                    or response != self._response(request, "DECLINED", reason="expired_before_admission", terminal=True,
                        admitted=False, session_id=request.session_id, send_authorized=False, capacity_released=False,
                        negative_admission_proof=proof, negative_admission_proof_sha256=canonical_sha256(proof))):
                raise AdmissionError("NEGATIVE_ADMISSION_PROOF_CHANGED")
        elif self._now() < datetime.fromisoformat(request.valid_until):
            raise AdmissionError("REQUEST_HAS_NOT_EXPIRED")
        self._assert_never_admitted(operation, request)
        if response is None:
            proof = {**fixed, "decided_at": self._now().isoformat()}
            response = self._response(request, "DECLINED", reason="expired_before_admission", terminal=True,
                admitted=False, session_id=request.session_id, send_authorized=False, capacity_released=False,
                negative_admission_proof=proof, negative_admission_proof_sha256=canonical_sha256(proof))
            self._save(request, "response", response)
        return response

    def _request(self, request):
        with self._journal() as db:
            prior = db.execute("SELECT * FROM requests WHERE id=?", (request.request_id,)).fetchone()
            if prior is None:
                db.execute("INSERT INTO requests VALUES(?,?,?,NULL,NULL)", (request.request_id, _encoded(request.payload()), request.sha256))
            elif prior["payload"] != _encoded(request.payload()) or prior["sha"] != request.sha256:
                raise AdmissionError("REQUEST_ID_REUSED_WITH_DIFFERENT_BYTES")
        return self._stored(request.request_id)

    def _stored(self, request_id):
        _name(request_id)
        with self._journal() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise AdmissionError("UNKNOWN_REQUEST")
            result = dict(row)
            request = Request.from_payload(json.loads(result["payload"]))
            if request.sha256 != result["sha"]:
                raise AdmissionError("REQUEST_JOURNAL_CHANGED")
            return request, _unpacked(result["plan"]) if result["plan"] else None, _unpacked(result["response"]) if result["response"] else None

    def _save(self, request, column, value):
        if column not in {"plan", "response"}:
            raise AssertionError("Internal journal column")
        with self._journal() as db:
            old = db.execute(f"SELECT {column} FROM requests WHERE id=?", (request.request_id,)).fetchone()[0]
            encoded = _packed(value)
            if old is not None and old != encoded:
                raise AdmissionError("IMMUTABLE_ADMISSION_DECISION_CHANGED")
            db.execute(f"UPDATE requests SET {column}=? WHERE id=?", (encoded, request.request_id))

    @staticmethod
    def _response(request, status, **values):
        return {"protocol_version": PROTOCOL_VERSION, "status": status,
                "request_id": request.request_id, "request_sha256": request.sha256, **values}

    def _bound(self, body, extras):
        if not isinstance(body, Mapping) or set(body) != {"request_id", "request_sha256", "session_id", *extras}:
            raise AdmissionError("EXACT_OPERATION_FIELDS_REQUIRED")
        request, plan, response = self._stored(body["request_id"])
        if body["request_sha256"] != request.sha256 or body["session_id"] != request.session_id:
            raise AdmissionError("OPERATION_REQUEST_OR_SESSION_MISMATCH")
        return request, plan, response

    @staticmethod
    def _identity(request):
        return {"participant_id": PARTICIPANT, "request_sha256": request.sha256}

    @staticmethod
    def _native(operation, request):
        key = STREAM + ":" + request.sha256
        return next((r for r in operation.ledger.snapshot().reservations if r.idempotency_key == key), None)

    def _pair(self, operation, request):
        native = self._native(operation, request)
        rows = [r for r in operation.authority.read_state()["reservations"]
                if r["request"].get("remote_request_sha256") == request.sha256]
        if len(rows) > 1 or (rows and native is None):
            raise AdmissionError("CANONICAL_RESERVATION_PAIR_MISSING")
        account = operation.authority.reservation(rows[0]["reservation_id"]) if rows else None
        if native is not None:
            if (operation.ledger.reservation_stream(native.reservation_id) != STREAM
                    or native.symbol != request.symbol or native.side != request.side
                    or native.forecast_id != request.forecast_id or Decimal(native.limit_price) != Decimal(request.limit_price)):
                raise AdmissionError("CANONICAL_NATIVE_REQUEST_MISMATCH")
        if account is not None:
            if (account.request["native_reservation_id"] != native.reservation_id
                    or account.request["native_allocation_id"] != native.allocation_id
                    or account.request["quantity"] != native.quantity):
                raise AdmissionError("CANONICAL_RESERVATION_PAIR_MISMATCH")
        return native, account

    @staticmethod
    def _assert_pairs(operation):
        """An interrupted two-book write cannot disappear from fresh capacity."""
        natives = {r.reservation_id: r for r in operation.ledger.snapshot().reservations
                   if operation.ledger.reservation_stream(r.reservation_id) == STREAM}
        accounts = {r["request"]["native_reservation_id"]: r for r in operation.authority.read_state()["reservations"]
                    if r["request"].get("stream") == STREAM}
        for identity in accounts.keys() | natives.keys():
            native, account = natives.get(identity), accounts.get(identity)
            if account is None and native is not None and (native.status == "REJECTED"
                    and native.broker_order_id is None and native.filled_quantity == 0 and native.submitted_at is None):
                continue
            if (native is None or account is None or native.allocation_id != account["request"]["native_allocation_id"]
                    or native.quantity != account["request"]["quantity"] or native.filled_quantity != account["filled_quantity"]
                    or native.broker_order_id != account["broker_order_id"]
                    or (account["status"] == "NOT_SUBMITTED" and native.status != "REJECTED")
                    or (account["status"] in {"FILLED", "CANCELLED", "REJECTED"} and native.status != account["status"])):
                raise AdmissionError("INTERRUPTED_SHARED_BOOK_WRITE_REQUIRES_RECOVERY")

    def _projection(self, operation, request, native, account):
        claim = operation.authority.remote_claim(account.reservation_id, **self._identity(request))
        if claim is not None and native.status == "RESERVED":
            operation.ledger.mark_submission(native.reservation_id, status="UNKNOWN", observed_at=claim["issued_at"],
                evidence_id="scout-claimed:" + account.reservation_id)
            native = operation.ledger.lookup_reservation(native.reservation_id)
        if account.status == "NOT_SUBMITTED":
            if claim is not None:
                raise AdmissionError("RELEASED_RESERVATION_HAS_SEND_CLAIM")
            self._reject_native(operation, native, "proven-never-submitted")
        elif account.broker_order_id:
            if native.broker_order_id and native.broker_order_id != account.broker_order_id:
                raise AdmissionError("CANONICAL_BROKER_ORDER_ID_MISMATCH")
            if not native.broker_order_id:
                # This projects an already durable send/receipt. Recovery time
                # is not a new broker observation and must not make intervening
                # exact fills appear older than native last_evidence_at.
                operation.ledger.mark_submission(native.reservation_id, status="SUBMITTED", observed_at=native.last_evidence_at or claim["issued_at"],
                    evidence_id="scout-accepted:" + account.reservation_id, broker_order_id=account.broker_order_id)
        return operation.ledger.lookup_reservation(native.reservation_id), claim

    def _reject_native(self, operation, native, reason):
        if native is None:
            return
        if native.status == "REJECTED" and not native.broker_order_id and not native.filled_quantity and native.submitted_at is None:
            return
        if native.status != "RESERVED" or native.broker_order_id or native.filled_quantity or native.submitted_at is not None:
            raise AdmissionError("POSSIBLE_NATIVE_SEND_CANNOT_BE_REVOKED")
        operation.ledger.mark_submission(native.reservation_id, status="REJECTED", observed_at=self._now().isoformat(),
            evidence_id="scout-never-sent:" + native.reservation_id)

    def _grant(self, request, plan, account):
        grant = Grant("scout-grant:" + request.sha256, request.request_id, request.sha256, request.account_fingerprint,
                      request.session_id, plan["authority_epoch"], account.request["quantity"], account.reservation_id,
                      plan["issued_at"], plan["expires_at"])
        return grant.validate(request)

    def reserve(self, participant, body):
        request = Request.from_payload(body)
        with self._operation(participant) as operation:
            request, old_plan, response = self._request(request)
            native, account = self._pair(operation, request)
            if response is not None:
                if "negative_admission_proof" in response:
                    return self._negative_admission(operation, request, old_plan, response)
                if response["status"] == "GRANTED" and (native is None or account is None):
                    raise AdmissionError("CANONICAL_RESERVATION_PAIR_MISSING")
                if account is not None:
                    self._projection(operation, request, native, account)
                    if self._now() >= datetime.fromisoformat(request.valid_until) and account.status == "RESERVED":
                        return self._expire(operation, request, native, account)
                    if account.status != "RESERVED":
                        return self._response(request, account.status, decision=response, send_authorized=False)
                    grant = Grant.from_payload(response["grant"]).validate(request)
                    if grant.reservation_id != account.reservation_id or grant.quantity != native.quantity:
                        raise AdmissionError("STORED_GRANT_DIFFERS_FROM_BOOKS")
                return response
            if old_plan is not None:
                if account is None:
                    self._reject_native(operation, native, "interrupted-unpaired-admission")
                    response = self._response(request, "DECLINED", reason="interrupted_before_paired_admission")
                elif self._now() >= datetime.fromisoformat(request.valid_until):
                    response = self._expire(operation, request, native, account)
                else:
                    self._current(request)
                    _, claim = self._projection(operation, request, native, account)
                    if claim is not None or account.status != "RESERVED":
                        raise AdmissionError("INTERRUPTED_ADMISSION_ALREADY_USED")
                    response = self._response(request, "GRANTED", grant=self._grant(request, old_plan, account).payload(), capacity=old_plan["capacity"])
                self._save(request, "response", response)
                return response
            if self._now() >= datetime.fromisoformat(request.valid_until):
                return self._negative_admission(operation, request, old_plan, response)
            self._assert_pairs(operation)
            self._current(request)
            snapshot = self.refresh(operation)
            if not isinstance(snapshot, BookSnapshot):
                raise AdmissionError("REVIEWED_FRESH_BOOK_SNAPSHOT_REQUIRED")
            available = operation.authority.available_intraday_capacity(operation.lease.token, snapshot.snapshot_id)
            capacity = clip_to_capacity(symbol=request.symbol, side=request.side, desired_quantity=request.quantity,
                limit_price=request.limit_price, available_cash=available["cash_available"],
                available_shares=available["eligible_shares"][request.symbol], capacity_evidence_id=snapshot.snapshot_id,
                observed_at=snapshot.observed_at, calculated_at=self._now())
            plan = {"authority_epoch": operation.lease.epoch, "issued_at": self._now().isoformat(),
                    "expires_at": request.valid_until, "capacity": capacity.payload()}
            self._save(request, "plan", plan)
            if not capacity.adjusted_quantity:
                response = self._response(request, "DECLINED", reason=capacity.disposition, capacity=capacity.payload())
            else:
                self._current(request)
                key = STREAM + ":" + request.sha256
                native = operation.ledger.reserve_intraday(symbol=request.symbol, forecast_id=request.forecast_id,
                    side=request.side, quantity=capacity.adjusted_quantity, limit_price=request.limit_price,
                    snapshot_id=snapshot.snapshot_id, idempotency_key=key, batch_id=request.session_id,
                    as_of=self._now().isoformat(), reference_at=request.reference_at, target_close_at=request.target_close_at,
                    forecast_sha256=request.forecast_sha256, forecast_snapshot_sha256=request.snapshot_sha256,
                    local_symbols=tuple(self.binding["participants"][PARTICIPANT]), stream=STREAM,
                    pending_sell_shares=snapshot.pending_sell_shares.get(request.symbol, 0),
                    broker_pending_sell_orders=snapshot.broker_pending_sell_orders.get(request.symbol, {}))
                value = IntradayReservationRequest(key, snapshot.snapshot_id, request.account_fingerprint,
                    self._now().astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat(), PARTICIPANT,
                    request.symbol, request.forecast_id, request.side, capacity.adjusted_quantity,
                    request.limit_price, request.limit_price, native.reservation_id, native.allocation_id,
                    request.forecast_sha256, request.snapshot_sha256, request.issued_at, request.reference_at,
                    request.target_close_at, TARGET, tuple(self.binding["participants"][PARTICIPANT]), stream=STREAM)
                account = operation.authority.reserve_remote_intraday(operation.lease.token, value, request_sha256=request.sha256)
                self._pair(operation, request)
                response = self._response(request, "GRANTED", grant=self._grant(request, plan, account).payload(), capacity=capacity.payload())
            self._save(request, "response", response)
            return response

    def _expire(self, operation, request, native, account):
        proof = canonical_sha256({"kind": "expired-before-claim", "request_sha256": request.sha256, "valid_until": request.valid_until})
        operation.authority.release_remote_unsubmitted(operation.lease.token, account.reservation_id,
                                                       **self._identity(request), proof_sha256=proof)
        self._reject_native(operation, native, "expired-before-claim")
        return self._response(request, "EXPIRED", reason="expired_before_send_claim")

    def authorize_send(self, participant, body):
        with self._operation(participant) as operation:
            request, _, response = self._bound(body, {"claim_id"})
            _name(body["claim_id"])
            if response is None or response["status"] != "GRANTED":
                raise AdmissionError("DURABLE_GRANT_REQUIRED")
            self._current(request)
            native, account = self._pair(operation, request)
            if native is None or account is None:
                raise AdmissionError("CANONICAL_RESERVATION_PAIR_MISSING")
            grant = Grant.from_payload(response["grant"]).validate(request)
            if grant.reservation_id != account.reservation_id or grant.quantity != native.quantity:
                raise AdmissionError("STORED_GRANT_DIFFERS_FROM_BOOKS")
            self._assert_pairs(operation)
            snapshot = self.refresh(operation)
            claim = operation.authority.claim_remote_send(operation.lease.token, account.reservation_id,
                **self._identity(request), claim_id=body["claim_id"], expires_at=request.valid_until, snapshot_id=snapshot.snapshot_id)
            self._projection(operation, request, native, account)
            return self._response(request, "SEND_AUTHORIZED_ONCE", claim=claim)

    def status(self, participant, body):
        with self._operation(participant) as operation:
            request, plan, response = self._bound(body, set())
            native, account = self._pair(operation, request)
            if response is not None and "negative_admission_proof" in response:
                return self._negative_admission(operation, request, plan, response)
            if account is None:
                if plan is None and response is None and self._now() >= datetime.fromisoformat(request.valid_until):
                    return self._negative_admission(operation, request, plan, response)
                return self._response(request, "PENDING" if response is None else response["status"], decision=response)
            native, claim = self._projection(operation, request, native, account)
            if account.status == "RESERVED" and self._now() >= datetime.fromisoformat(request.valid_until):
                return self._expire(operation, request, native, account)
            return self._response(request, account.status, reservation_id=account.reservation_id,
                broker_order_id=account.broker_order_id, filled_quantity=account.filled_quantity,
                native_status=native.status, claim=claim, send_authorized=False)

    def _event(self, body):
        _name(body["event_id"])
        digest = canonical_sha256(body)
        with self._journal() as db:
            old = db.execute("SELECT * FROM events WHERE id=?", (body["event_id"],)).fetchone()
            if old is not None:
                if old["sha"] != digest or old["payload"] != _encoded(body):
                    raise AdmissionError("EVENT_ID_REUSED_WITH_DIFFERENT_BYTES")
                return _unpacked(old["response"]) if old["response"] else None
            db.execute("INSERT INTO events VALUES(?,?,?,NULL)", (body["event_id"], _encoded(body), digest))
        return None

    def _event_done(self, body, response):
        with self._journal() as db:
            encoded = _packed(response)
            old = db.execute("SELECT response FROM events WHERE id=?", (body["event_id"],)).fetchone()[0]
            if old is not None and old != encoded:
                raise AdmissionError("IMMUTABLE_EVENT_RESPONSE_CHANGED")
            db.execute("UPDATE events SET response=? WHERE id=?", (encoded, body["event_id"]))
        return response

    @staticmethod
    def _native_covers_accepted_evidence(operation, native, account, evidence):
        """Prove an older accepted event is contained in newer complete books.

        The authority has already verified the immutable old evidence ID/bytes.
        A status/quantity match alone is insufficient: the native book must also
        retain a complete newer order receipt containing every exact older fill.
        This read cannot rewrite or synthesize native history.
        """
        if (native.status != account.status or native.filled_quantity != account.filled_quantity
                or native.broker_order_id != account.broker_order_id or native.quantity != evidence.order_quantity
                or native.broker_order_id != evidence.broker_order_id or not native.last_evidence_at
                or datetime.fromisoformat(native.last_evidence_at) <= datetime.fromisoformat(evidence.observed_at)):
            return False

        def fill_identity(fill):
            return (fill["quantity"], str(Decimal(str(fill["price"]))),
                    datetime.fromisoformat(fill["executed_at"].replace("Z", "+00:00")).astimezone(timezone.utc).isoformat())

        for receipt in reversed(operation.ledger.history()):
            if receipt.kind != "order":
                continue
            value = json.loads(receipt.payload_json)
            if (value["reservation_id"] != native.reservation_id or value["account_fingerprint"] != evidence.account_fingerprint
                    or value["observed_at"] != native.last_evidence_at or value["status"] != native.status
                    or value["broker_order_id"] != native.broker_order_id or value["order_quantity"] != native.quantity
                    or value["cumulative_filled_quantity"] != native.filled_quantity):
                continue
            fills = {fill["fill_id"]: fill_identity(fill) for fill in value["fills"]}
            return (sum(fill["quantity"] for fill in value["fills"]) == native.filled_quantity
                    and all(fills.get(fill.fill_id) == fill_identity(asdict(fill)) for fill in evidence.fills))
        return False

    def evidence(self, participant, body):
        if not isinstance(body, Mapping) or body.get("kind") not in {"ACCEPTED", "UNKNOWN", "ORDER_STATUS"}:
            raise AdmissionError("EXACT_EVIDENCE_KIND_REQUIRED")
        extras = {"event_id", "kind", "claim_id"}
        if body["kind"] == "ACCEPTED":
            extras.add("broker_order_id")
        elif body["kind"] == "ORDER_STATUS":
            extras.add("order_evidence")
        with self._operation(participant) as operation:
            request, _, _ = self._bound(body, extras)
            native, account = self._pair(operation, request)
            if account is None:
                raise AdmissionError("CANONICAL_RESERVATION_PAIR_MISSING")
            prior = self._event(body)
            if prior is not None:
                return prior
            identity = {**self._identity(request), "claim_id": body["claim_id"]}
            if body["kind"] == "ACCEPTED":
                account = operation.authority.record_remote_submission(operation.lease.token, account.reservation_id,
                    **identity, broker_order_id=body["broker_order_id"])
                self._projection(operation, request, native, account)
            elif body["kind"] == "UNKNOWN":
                account = operation.authority.mark_remote_unknown(operation.lease.token, account.reservation_id, **identity)
                self._projection(operation, request, native, account)
            else:
                raw = dict(body["order_evidence"])
                raw["fills"] = tuple(Fill(**item) for item in raw["fills"])
                value = OrderEvidence(**raw)
                if value.reservation_id != account.reservation_id:
                    raise AdmissionError("EVIDENCE_RESERVATION_SCOPE_MISMATCH")
                native, _ = self._projection(operation, request, native, account)
                account = operation.authority.remote_reconcile(operation.lease.token, value, **identity)
                if not self._native_covers_accepted_evidence(operation, native, account, value):
                    operation.ledger.apply_stream_order_evidence(NativeOrderEvidence(value.evidence_id, native.reservation_id,
                        value.account_fingerprint, value.observed_at, value.broker_order_id, value.status, value.order_quantity,
                        value.cumulative_filled_quantity, value.remaining_quantity,
                        tuple(FillEvidence(f.fill_id, f.quantity, f.price, f.executed_at) for f in value.fills),
                        fill_time_provenance=value.fill_time_provenance), stream=STREAM)
            return self._event_done(body, self._response(request, account.status,
                event_id=body["event_id"], event_sha256=canonical_sha256(body), broker_order_id=account.broker_order_id,
                filled_quantity=account.filled_quantity))

    def cancel_intent(self, participant, body):
        with self._operation(participant) as operation:
            request, _, _ = self._bound(body, {"event_id", "claim_id", "broker_order_id", "reason"})
            _name(body["reason"])
            native, account = self._pair(operation, request)
            if account is None or account.broker_order_id is None or account.broker_order_id != body["broker_order_id"]:
                raise AdmissionError("EXACT_OWNED_ORDER_REQUIRED")
            claim = operation.authority.remote_claim(account.reservation_id, **self._identity(request))
            if claim is None or claim["claim_id"] != body["claim_id"]:
                raise AdmissionError("EXACT_REMOTE_CLAIM_REQUIRED")
            prior = self._event(body)
            if prior is not None:
                return prior
            return self._event_done(body, self._response(request, "CANCEL_INTENT_RECORDED",
                broker_order_id=account.broker_order_id, order_status=account.status,
                capacity_released=False, broker_action_performed=False))

    def release_unsent(self, participant, body):
        with self._operation(participant) as operation:
            request, _, _ = self._bound(body, {"event_id", "proof_sha256"})
            native, account = self._pair(operation, request)
            if account is None or native is None:
                raise AdmissionError("CANONICAL_RESERVATION_PAIR_MISSING")
            prior = self._event(body)
            if prior is not None:
                return prior
            if native.status not in {"RESERVED", "REJECTED"} or native.broker_order_id or native.filled_quantity or native.submitted_at is not None:
                raise AdmissionError("POSSIBLE_NATIVE_SEND_CANNOT_BE_REVOKED")
            account = operation.authority.release_remote_unsubmitted(operation.lease.token, account.reservation_id,
                **self._identity(request), proof_sha256=body["proof_sha256"])
            self._reject_native(operation, native, "authenticated-revocation")
            return self._event_done(body, self._response(request, account.status, capacity_released=True))
