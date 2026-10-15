"""Durable Scout submission orchestration with injected effects only.

The independent local executor prepares its own broker-capacity permit in the
journal and calls submit without an authority transport. The legacy admit and
flush_outbox methods remain available for explicitly injected adapters; neither
method is a prerequisite for independent submission. Historical Request/Grant
and authority field names preserve existing serialized identities. This module
creates no broker, starts no trader and supplies no default network transport.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import re
from typing import Protocol

from .scout_journal import JournalStateError, ScoutJournal
from .scout_protocol import Grant, ProtocolError, Request


class ClientGateError(ValueError):
    """An identity, clock, or final submission prerequisite did not pass."""


class AuthorityTransport(Protocol):
    """Future authenticated adapter; responses alone cannot prove authenticity."""

    def admit(self, request: dict) -> Mapping: ...

    def deliver(self, event: dict) -> Mapping: ...


class ScoutClient:
    """Persist before admission; claim once before any possible broker effect.

    The independent caller supplies its own durable local permit and a gate
    checking current manual controls, its broker account and current forecast.
    Only admit and flush_outbox use the explicitly injected authority transport;
    submit can use None after the caller journals its own local permit. A timeout
    returns control with durable state. Recovery preserves the original request
    and must never turn an unknown send into another POST.
    """

    def __init__(
        self,
        journal: ScoutJournal,
        authority: AuthorityTransport,
        *,
        account_fingerprint: str,
        session_id: str,
        symbols: frozenset[str],
        authority_epoch: int,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not account_fingerprint or not session_id:
            raise ClientGateError("Explicit account and manual-session bindings required")
        if not symbols or any(type(symbol) is not str or not symbol for symbol in symbols):
            raise ClientGateError("Explicit nonempty local symbol scope required")
        if type(authority_epoch) is not int or not 0 < authority_epoch <= 2**63 - 1:
            raise ClientGateError("Explicit positive authority epoch required")
        self.journal = journal
        self.authority = authority
        self.account_fingerprint = account_fingerprint
        self.session_id = session_id
        self.symbols = frozenset(symbols)
        self.authority_epoch = authority_epoch
        self.clock = clock

    def _bound(self, request: Request) -> None:
        if (
            request.account_fingerprint != self.account_fingerprint
            or request.session_id != self.session_id
            or request.symbol not in self.symbols
        ):
            raise ClientGateError("Request does not belong to this bound Scout client")

    def _now(self) -> datetime:
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ClientGateError("Clock must provide an aware datetime")
        return now.astimezone(timezone.utc)

    def _current(self, request: Request, grant: Grant | None = None) -> None:
        self._bound(request)
        now = self._now()
        if not datetime.fromisoformat(request.issued_at) <= now < datetime.fromisoformat(request.valid_until):
            raise ClientGateError("Request is not currently valid")
        if grant is not None:
            grant.validate(request)
            if grant.authority_epoch != self.authority_epoch:
                raise ClientGateError("Grant belongs to another authority epoch")
            if not datetime.fromisoformat(grant.issued_at) <= now < datetime.fromisoformat(grant.expires_at):
                raise ClientGateError("Grant is not currently valid")

    def admit(self, request: Request) -> Grant:
        """A repeated request retains its exact payload after a lost reply."""
        self._current(request)
        self.journal.prepare(request)
        record = self.journal.get(request.request_id)
        if record["state"] == "NOT_SENT":
            raise JournalStateError("A revoked request cannot be readmitted")
        if record["grant"] is not None:
            grant = Grant.from_payload(record["grant"])
        else:
            # Network uncertainty leaves PREPARED. Retry this identity/payload,
            # never generate a replacement request to evade an unknown grant.
            grant = Grant.from_payload(self.authority.admit(request.payload()))
            self._current(request, grant)
            self.journal.grant(grant)
        self._current(request, grant)
        return grant

    def _gate(self, request: Request, grant: Grant, final_gate: Callable[[Request, Grant], bool]) -> None:
        self._current(request, grant)
        if final_gate(request, grant) is not True:
            raise ClientGateError("Final runtime gate did not explicitly pass")
        # A slow gate cannot preserve permission past the grant's expiry.
        self._current(request, grant)

    def submit(
        self,
        request_id: str,
        *,
        send: Callable[[Request, Grant], str],
        final_gate: Callable[[Request, Grant], bool],
    ) -> dict:
        """Invoke the injected sender at most once for this durable request.

        final_gate must check current manual authority, the caller's own broker
        capacity, newest live forecast, quote, fixed target, session/cutoff and
        resolution of its own prior order immediately before send.
        The sender must submit the bound symbol/side/price and granted quantity.
        This callback contract is not a production implementation of those gates.
        """
        record = self.journal.get(request_id)
        if record is None or record["state"] != "GRANTED":
            raise JournalStateError("Only an unclaimed durable grant can be sent")
        request = Request.from_payload(record["request"])
        grant = Grant.from_payload(record["grant"])
        self._gate(request, grant, final_gate)
        token = self.journal.claim_send(request_id)
        try:
            self._gate(request, grant, final_gate)
            order_id = send(request, grant)
            # Proposed sender contract: an exact positive decimal order ID,
            # never a placeholder, URL or guessed match. A production adapter
            # must validate the broker response/account before extracting it.
            if type(order_id) is not str or re.fullmatch(r"[1-9][0-9]{0,63}", order_id, re.ASCII) is None:
                raise ProtocolError("Sender did not return a canonical exact order ID")
            return self.journal.outcome(request_id, token, order_id)
        except Exception:
            # Even a callback which failed before sending might have opaque
            # side effects. Preserve ambiguity; never arm another submission.
            # Journal outcome(None) preserves an already accepted exact ID.
            self.journal.outcome(request_id, token, None)
            raise
        # A process exit/BaseException leaves SENDING durably fenced as well.

    def flush_outbox(self) -> int:
        """Deliver immutable evidence; an ACK never releases account capacity."""
        count = 0
        for envelope in self.journal.pending_events():
            event_id, event_sha256 = envelope["event_id"], envelope["event_sha256"]
            ack = self.authority.deliver(envelope)
            if not isinstance(ack, Mapping) or set(ack) != {"event_id", "event_sha256"}:
                raise ProtocolError("Exact event acknowledgement required")
            if ack["event_id"] != event_id or ack["event_sha256"] != event_sha256:
                raise ProtocolError("Acknowledgement does not match this event")
            self.journal.acknowledge(event_id, event_sha256)
            count += 1
        return count
