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
from dataclasses import dataclass
import re
import sqlite3
from typing import Protocol

from .prediction import ContractError
from .scout_journal import JournalStateError, ScoutJournal
from .scout_protocol import Grant, ProtocolError, Request, canonical_sha256


class ClientGateError(ValueError):
    """An identity, clock, or final submission prerequisite did not pass."""


def _saved_forecast_retry_details(error):
    """Allow only this version's bounded numeric observations, never free text."""
    if not isinstance(error, sqlite3.OperationalError):
        return None
    code = getattr(error, "sqlite_errorcode", None)
    if type(code) is not int or code < 0 or code & 255 not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return None
    value = getattr(error, "scout_saved_forecast_retry", None)
    fields = {"version", "attempt_count", "maximum_attempts", "retry_budget_seconds",
              "retry_pause_seconds", "elapsed_seconds", "sleep_count",
              "sleep_requested_seconds", "stop_code"}
    if (type(value) is not dict or set(value) != fields
            or type(value["version"]) is not str or value["version"] != "saved-forecast-read-retry-v1"):
        return None
    if any(type(value[name]) is not int for name in
           ("attempt_count", "maximum_attempts", "sleep_count", "stop_code")):
        return None
    if (value["maximum_attempts"] != 8 or not 1 <= value["attempt_count"] <= 8
            or not 0 <= value["sleep_count"] <= 7 or value["stop_code"] not in {1, 2, 3}):
        return None
    for name, upper in (("retry_budget_seconds", .25), ("retry_pause_seconds", .01),
                        ("elapsed_seconds", 3600), ("sleep_requested_seconds", .07 + 1e-12)):
        number = value[name]
        if type(number) not in {int, float} or not 0 <= number <= upper:
            return None
    if value["retry_budget_seconds"] != .25 or value["retry_pause_seconds"] != .01:
        return None
    attempts, sleeps, stop = value["attempt_count"], value["sleep_count"], value["stop_code"]
    if (sleeps != attempts - (0 if stop == 3 else 1)
            or (stop == 2) != (attempts == 8)
            or stop in {1, 3} and value["elapsed_seconds"] < .25
            or value["sleep_requested_seconds"] > sleeps * .01 + 1e-12
            or (sleeps == 0) != (value["sleep_requested_seconds"] == 0)):
        return None
    return dict(value)


def _local_evidence_freshness_details(error):
    """Allow only consistent bounded numbers from a rejected local gate."""
    if not isinstance(error, ContractError):
        return None
    value = getattr(error, "scout_local_evidence_freshness", None)
    fields = {"version", "account_age_seconds", "quote_age_seconds",
              "maximum_age_seconds", "gate_since_request_seconds", "failed_inputs"}
    if (type(value) is not dict or set(value) != fields
            or type(value["version"]) is not str
            or value["version"] != "scout-local-evidence-freshness-v1"):
        return None
    maximum = value["maximum_age_seconds"]
    failed = value["failed_inputs"]
    if (type(maximum) is not int or not 1 <= maximum <= 300
            or type(failed) is not int or failed not in {1, 2, 3}):
        return None
    for name in ("account_age_seconds", "quote_age_seconds", "gate_since_request_seconds"):
        number = value[name]
        if type(number) not in {int, float} or not -86400 <= number <= 86400:
            return None
    expected = (int(not 0 <= value["account_age_seconds"] <= maximum)
                + 2 * int(not 0 <= value["quote_age_seconds"] <= maximum))
    if failed != expected:
        return None
    return dict(value)


def exception_details(error: BaseException) -> dict:
    """Bounded diagnostic context without broker URLs, credentials or locals.

    Arbitrary exception text can contain account IDs or authorization headers.
    Retain exact standard SQLite messages only; stack locations, error codes and
    causal types identify other failures without persisting private response text.
    """
    safe_sqlite_messages = {
        "database is locked", "database table is locked", "database schema is locked",
        "database or disk is full", "disk I/O error", "unable to open database file",
        "attempt to write a readonly database", "cannot start a transaction within a transaction",
        "cannot commit - no transaction is active", "database disk image is malformed",
        "interrupted", "out of memory",
    }
    chain, seen, current = [], set(), error
    while current is not None and id(current) not in seen and len(chain) < 4:
        seen.add(id(current))
        kind = type(current).__name__[:80]
        message = "Exception text withheld; inspect error type and stack locations"
        if isinstance(current, sqlite3.Error) and str(current) in safe_sqlite_messages:
            message = str(current)
        frames, trace = [], current.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            # Basenames only: no local account paths or frame values.
            frames.append(dict(file=code.co_filename.replace("\\", "/").rsplit("/", 1)[-1][:120],
                               function=code.co_name[:80], line=trace.tb_lineno))
            trace = trace.tb_next
        item = dict(type=kind, message=message, frames=frames[-8:])
        for name in ("sqlite_errorcode", "errno"):
            value = getattr(current, name, None)
            if type(value) is int:
                item[name] = value
        try:
            retry = _saved_forecast_retry_details(current)
        except BaseException:
            retry = None
        if retry is not None:
            item["saved_forecast_retry"] = retry
        try:
            freshness = _local_evidence_freshness_details(current)
        except BaseException:
            freshness = None
        if freshness is not None:
            item["local_evidence_freshness"] = freshness
        chain.append(item)
        current = current.__cause__ if current.__cause__ is not None else (
            None if current.__suppress_context__ else current.__context__)
    return dict(chain=chain, chain_truncated=current is not None)


@dataclass(frozen=True)
class ClientFinalGateFailure:
    """A completed post-claim failure before this client's sender was entered.

    This witness does not establish that an arbitrary gate was free of effects.
    Only the independent local executor can qualify its exact trusted callback.
    """

    client: object
    request_sha256: str
    grant_sha256: str
    sending_record_sha256: str
    final_gate: object
    send: object
    error: BaseException


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
        self.final_gate_failure = None

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
        self.final_gate_failure = None
        record = self.journal.get(request_id)
        if record is None or record["state"] != "GRANTED":
            raise JournalStateError("Only an unclaimed durable grant can be sent")
        request = Request.from_payload(record["request"])
        grant = Grant.from_payload(record["grant"])
        self._gate(request, grant, final_gate)
        token = self.journal.claim_send(request_id)
        stage, order_id = "final_gate", None
        try:
            self._gate(request, grant, final_gate)
            stage = "send"
            order_id = send(request, grant)
            stage = "validate_receipt"
            # Proposed sender contract: an exact positive decimal order ID,
            # never a placeholder, URL or guessed match. A production adapter
            # must validate the broker response/account before extracting it.
            if type(order_id) is not str or re.fullmatch(r"[1-9][0-9]{0,63}", order_id, re.ASCII) is None:
                raise ProtocolError("Sender did not return a canonical exact order ID")
            stage = "record_acceptance"
            return self.journal.outcome(request_id, token, order_id)
        except Exception as original:
            if stage == "final_gate":
                try:
                    # No additional journal IO or gate invocation. Retain the
                    # exact callback/error identities; generic gates stay opaque.
                    self.final_gate_failure = ClientFinalGateFailure(self,
                        request.sha256, grant.sha256,
                        canonical_sha256(dict(record, state="SENDING", send_token=token)),
                        final_gate, send, original)
                except BaseException:
                    pass  # An optional witness must preserve the original error.
            # Even a callback which failed before sending might have opaque
            # side effects. Preserve ambiguity; never arm another submission.
            # Journal outcome(None) preserves an already accepted exact ID.
            details = dict(stage=stage, original=exception_details(original))
            if stage == "record_acceptance":
                # This is the validated sender receipt, not a guessed match or
                # proof that its journal write committed. Preserve it for exact
                # recovery without changing the durable one-send fence.
                details["returned_broker_order_id"] = order_id
            try:
                self.journal.outcome(request_id, token, None)
            except Exception as persistence_error:
                details["uncertainty_persistence_error"] = exception_details(persistence_error)
            original.scout_submission_failure = details
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
