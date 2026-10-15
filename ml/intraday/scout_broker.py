"""Scout-local effects through the existing prepared Schwab transport.

This adapter owns no account capacity, native ledger, retry loop or credentials.
The caller supplies an existing broker and the already claimed local journal.
The independent caller supplies its durable local account-capacity permit.
An exception after a local send claim never authorizes a second POST or releases
capacity. Cancellation acknowledgments likewise carry no terminal evidence.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import math
import re
from urllib.parse import urlsplit

from app.services.schwab_stock_orders import build_schwab_stock_order_payload
from ml.stock_trader.horizon_ledger import OrderEvidence
from ml.stock_trader.session import stock_execution_window

from .exact_status import TERMINAL, read_exact_status
from .prediction import PACIFIC, instant
from .scout_journal import ScoutJournal
from .scout_protocol import Grant, ProtocolError, Request


class ScoutBrokerError(ValueError):
    """A local identity, durable claim, time or final effect gate failed."""


@dataclass(frozen=True)
class _TrackedOrder:
    reservation_id: str
    broker_order_id: str
    symbol: str
    side: str
    quantity: int


@dataclass(frozen=True)
class ScoutCancellationObservation:
    evidence: OrderEvidence
    delete_acknowledged: bool


class ScoutBrokerAdapter:
    """Bound local order effects; never constructs a broker or production store.

    Use ``send`` only as ScoutClient's sender after its durable one-shot claim.
    ``authorization_gate`` verifies the durable permission already committed for
    this exact request/grant and journal claim. The independent Scout caller
    supplies its saved local account-capacity permit using its own broker.
    ``final_gate`` checks current manual authority, the caller's broker account,
    forecast/quote freshness, fixed target and previous same-symbol resolution.
    Both callbacks must explicitly return True and remain safe to call twice.
    ``final_gate`` uses local, nonblocking evidence only. Obtain any needed broker
    refresh before invoking ``send`` and retain the exact durable claim.
    The final local stop/eligibility check runs after account verification IO.

    Status and cancellation remain available after submission expiry/cutoff.
    Cancellation's authorization gate additionally receives exact status evidence;
    it must reconcile/persist that evidence and a durable cancellation marker,
    and enforce the caller's acknowledgment/retry policy before permitting DELETE.
    No method here releases the caller's capacity commitment.
    """

    def __init__(self, broker, journal: ScoutJournal, *, account_fingerprint: str,
                 session_id: str, symbols: frozenset[str], authority_epoch: int,
                 maximum_evidence_age_seconds: float,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        binding = journal.binding
        if (binding.get("account_fingerprint") != account_fingerprint
                or binding.get("session_id") != session_id
                or binding.get("participant_id") != "pc-new"
                or binding.get("stream") != "scout-15m"):
            raise ScoutBrokerError("Adapter differs from the local Scout journal binding")
        if (not symbols or any(type(symbol) is not str or not symbol for symbol in symbols)
                or type(authority_epoch) is not int or authority_epoch <= 0
                or isinstance(maximum_evidence_age_seconds, bool)
                or not math.isfinite(maximum_evidence_age_seconds)
                or maximum_evidence_age_seconds <= 0):
            raise ScoutBrokerError("Explicit symbols, authority epoch and evidence age required")
        self.broker, self.journal = broker, journal
        self.account_fingerprint, self.session_id = account_fingerprint, session_id
        self.symbols, self.authority_epoch = frozenset(symbols), authority_epoch
        self.maximum_evidence_age_seconds, self.clock = maximum_evidence_age_seconds, clock

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ScoutBrokerError("Clock must supply an aware datetime")
        return value.astimezone(timezone.utc)

    def _bound(self, request, grant):
        if type(request) is not Request or type(grant) is not Grant:
            raise ScoutBrokerError("Exact request and grant required")
        grant.validate(request)
        if (request.account_fingerprint != self.account_fingerprint
                or request.session_id != self.session_id or request.symbol not in self.symbols
                or grant.authority_epoch != self.authority_epoch):
            raise ScoutBrokerError("Request or grant differs from the bound Scout session")

    def _record(self, request, grant, state):
        self._bound(request, grant)
        record = self.journal.get(request.request_id)
        if (record is None or record["state"] != state
                or record["request"] != request.payload() or record["grant"] != grant.payload()):
            raise ScoutBrokerError("Exact durable journal state required before broker access")
        return record

    def _current(self, request, grant):
        now = self._now()
        if (not instant(request.issued_at) <= now < instant(request.valid_until)
                or not instant(grant.issued_at) <= now < instant(grant.expires_at)):
            raise ScoutBrokerError("Request or grant is no longer current")
        local = now.astimezone(PACIFIC)
        if (local.hour, local.minute) >= (16, 45):
            raise ScoutBrokerError("Strict 16:45 Pacific submission cutoff")
        window = stock_execution_window(now)
        if not window.executable or window.time_in_force is None:
            raise ScoutBrokerError("Stock execution session is closed")
        return window

    def _context(self, context, expected_snapshot_identity):
        if (type(expected_snapshot_identity) is not str or not expected_snapshot_identity
                or getattr(context, "identity_fingerprint", None) != expected_snapshot_identity):
            raise ScoutBrokerError("Prepared account context differs from the coherent snapshot")
        if self.broker.stable_account_fingerprint() != self.account_fingerprint:
            raise ScoutBrokerError("Local broker account changed")
        self.broker.verify_read_snapshot(expected_snapshot_identity)

    @staticmethod
    def _accepted_id(location, context):
        # The Location is the response from the prepared, frozen account POST.
        # A bare number, other account, arbitrary URL or guessed order is not a
        # receipt. Do not turn an ambiguous successful response into a retry.
        if type(location) is not str or location != location.strip():
            raise ProtocolError("Exact Schwab acceptance Location required")
        parsed = urlsplit(location)
        if (parsed.scheme != "https" or parsed.netloc != "api.schwabapi.com"
                or parsed.query or parsed.fragment):
            raise ProtocolError("Acceptance Location is not the expected Schwab endpoint")
        prefix = "/trader/v1/accounts/" + str(context.account_hash) + "/orders/"
        identity = parsed.path.removeprefix(prefix)
        if not parsed.path.startswith(prefix) or re.fullmatch(r"[1-9][0-9]{0,63}", identity, re.ASCII) is None:
            raise ProtocolError("Acceptance Location differs from the exact prepared account/order")
        return identity

    def send(self, request, grant, *, expected_snapshot_identity, authorization_gate, final_gate):
        """Perform one prepared POST for the exact locally claimed grant quantity."""
        self._record(request, grant, "SENDING")
        window = self._current(request, grant)
        context = self.broker.prepare_order_submission()
        payload = build_schwab_stock_order_payload(symbol=request.symbol, instruction=request.side,
            order_type="LIMIT", time_in_force=window.time_in_force, position_effect="AUTO",
            quantity=grant.quantity, price=request.limit_price)
        # The existing builder rounds prices for Schwab. Never silently change
        # the economic limit the caller recorded and the journal made immutable.
        if Decimal(payload["price"]) != Decimal(request.limit_price):
            raise ScoutBrokerError("Prepared price differs from the authorized limit")
        if payload["orderLegCollection"][0]["quantity"] != grant.quantity:
            raise ScoutBrokerError("Prepared quantity differs from the authorized grant")

        def gate():
            self._record(request, grant, "SENDING")
            self._context(context, expected_snapshot_identity)
            if authorization_gate(request, grant) is not True:
                raise ScoutBrokerError("Durable authority send authorization did not pass")
            # Authorization can block on IO; finish account verification before
            # the last local stop/forecast check, then recheck the exact clock.
            self._record(request, grant, "SENDING")
            self._context(context, expected_snapshot_identity)
            if self._current(request, grant).time_in_force != window.time_in_force:
                raise ScoutBrokerError("Stock execution session changed before POST")
            if final_gate(request, grant) is not True:
                raise ScoutBrokerError("Final Scout runtime gate did not pass")
            if self._current(request, grant).time_in_force != window.time_in_force:
                raise ScoutBrokerError("Stock execution session changed before POST")

        gate()
        location = self.broker.submit_prepared_order(payload, context, before_post=gate)
        return self._accepted_id(location, context)

    def _accepted(self, request_id):
        record = self.journal.get(request_id)
        if record is None or record["state"] != "ACCEPTED":
            raise ScoutBrokerError("Exact accepted journal order required for status or cancellation")
        request, grant = Request.from_payload(record["request"]), Grant.from_payload(record["grant"])
        self._record(request, grant, "ACCEPTED")
        identity = record["broker_order_id"]
        if type(identity) is not str or re.fullmatch(r"[1-9][0-9]{0,63}", identity, re.ASCII) is None:
            raise ScoutBrokerError("Journal lacks a canonical exact accepted broker ID")
        return request, grant, _TrackedOrder(grant.reservation_id, identity,
                                            request.symbol, request.side, grant.quantity)

    def status(self, request_id) -> OrderEvidence:
        """Normalize complete exact-ID history; missing/unknown history raises."""
        request, _, tracked = self._accepted(request_id)
        evidence = read_exact_status(self.broker, tracked,
            account_fingerprint=self.account_fingerprint, clock=self._now)
        if not instant(request.issued_at) <= instant(evidence.observed_at) <= self._now():
            raise ScoutBrokerError("Order evidence lies outside the local observation clock")
        return evidence

    def cancel(self, request_id, *, expected_snapshot_identity, authorization_gate,
               final_gate) -> ScoutCancellationObservation:
        """One exact status/DELETE step using the same prepared cancellation API.

        No retry, replacement, capacity release or journal demotion occurs here.
        The caller retains status evidence and cancellation intent before DELETE;
        a lost reply retains the same identity and requires subsequent recovery.
        """
        request, grant, tracked = self._accepted(request_id)
        evidence = self.status(request_id)
        if evidence.status in TERMINAL or evidence.broker_status == "PENDING_CANCEL":
            return ScoutCancellationObservation(evidence, False)
        if evidence.status not in {"WORKING", "PARTIAL"}:
            raise ScoutBrokerError("Exact working status required before cancellation")
        context = self.broker.prepare_order_submission()

        def gate():
            _, _, current = self._accepted(request_id)
            if current != tracked:
                raise ScoutBrokerError("Exact cancellation identity changed")
            self._context(context, expected_snapshot_identity)
            if authorization_gate(request, grant, evidence) is not True:
                raise ScoutBrokerError("Durable cancellation authorization did not pass")
            self._context(context, expected_snapshot_identity)
            if not 0 <= (self._now() - instant(evidence.observed_at)).total_seconds() <= self.maximum_evidence_age_seconds:
                raise ScoutBrokerError("Exact order status became stale before DELETE")
            if final_gate(request, grant, evidence) is not True:
                raise ScoutBrokerError("Final Scout cancellation gate did not pass")
            if not 0 <= (self._now() - instant(evidence.observed_at)).total_seconds() <= self.maximum_evidence_age_seconds:
                raise ScoutBrokerError("Exact order status became stale before DELETE")

        gate()
        self.broker.cancel_prepared_order(tracked.broker_order_id, context, before_delete=gate)
        return ScoutCancellationObservation(evidence, True)
