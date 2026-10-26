"""15m cancellation policy using the existing prepared Schwab transport.

No broker session is constructed here. Callers inject the existing adapter,
coherent native/account reconciliation, final authority checks and durable logs.
A DELETE acknowledgment is never a terminal status or a release of commitment.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import time

from ml.stock_trader.horizon_broker import capture_order_evidence
from ml.stock_trader.horizon_ledger import OrderEvidence

from .prediction import ContractError, instant, number

ATLAS_STREAM = "atlas-15m"
GAMEPLAN_STREAM = "gameplan"
TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED"})


def reservation_stream(ledger, reservation_id):
    """Legacy rows stay Gameplan; explicit unknown/new streams never fall back."""
    reader = getattr(ledger, "reservation_stream", None)
    return GAMEPLAN_STREAM if reader is None else reader(reservation_id)


def allocation_stream(ledger, allocation_id):
    reader = getattr(ledger, "allocation_stream", None)
    return GAMEPLAN_STREAM if reader is None else reader(allocation_id)


def gameplan_reservation(ledger, reservation):
    return reservation_stream(ledger, reservation.reservation_id) == GAMEPLAN_STREAM


def gameplan_allocation(ledger, allocation_id):
    return allocation_stream(ledger, allocation_id) == GAMEPLAN_STREAM


def temporary_status_failure(exc):
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(exc, (TimeoutError, ConnectionError)) or status in {408, 429, 500, 502, 503, 504}:
        return True
    if type(exc).__name__ in {"Timeout", "ReadTimeout", "ConnectTimeout", "ConnectionError", "TemporaryFailure"}:
        return True
    # Missing bounded history proves neither rejection nor nonacceptance.
    return str(exc) in {"TRACKED_ORDER_MISSING_FROM_BOUNDED_HISTORY", "ORDER_HISTORY_MAY_BE_TRUNCATED"}


def retry_delay(exc, now):
    delay = 3.0
    value = getattr(exc, "retry_after", None)
    headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
    if value is None:
        value = headers.get("Retry-After")
    if value is not None:
        try:
            proposed = float(value)
        except (ValueError, TypeError):
            try:
                proposed = (parsedate_to_datetime(value).astimezone(timezone.utc)-instant(now)).total_seconds()
            except (ValueError, TypeError, AttributeError, OverflowError):
                proposed = 0
        if math.isfinite(proposed):
            delay = max(delay, proposed)
    return delay


@dataclass(frozen=True)
class CancellationResult:
    state: str
    reservation_id: str
    status: str
    reason_code: str
    complete_terminal_evidence: bool = False
    retry_after_seconds: float | None = None


def read_exact_status(broker, reservation, *, account_fingerprint, clock):
    """Use the existing exact-ID history reader and strict fill normalizer."""
    class Scope:
        def pending_reservations(self):
            return (reservation,)
    scope = Scope()
    scope.account_fingerprint = account_fingerprint
    evidence = capture_order_evidence(broker, scope, account_fingerprint=account_fingerprint,
                                     as_of=instant(clock()).isoformat(), observation_clock=clock)
    if len(evidence) != 1:
        raise ContractError("Exact tracked status evidence required")
    return evidence[0]


class CancellationService:
    """Operate only on exact atlas-15m reservations, retaining native identities.

    before_cancel(context, reservation) runs before every DELETE, including inside
    the existing adapter's before_delete gate. apply_evidence(evidence) must first
    reconcile complete broker fills with a newer coherent account/native snapshot.
    record_event(kind, payload) persists recovery evidence before the next sleep.
    All temporary failures retry with sleep until stopped; no attempt/deadline cap.
    """
    def __init__(self, *, broker, ledger, account_fingerprint, before_cancel,
                 apply_evidence, record_event, clock=lambda: datetime.now(timezone.utc),
                 sleep=time.sleep, stopped=lambda: False, observe=None,
                 is_temporary=temporary_status_failure, stream=ATLAS_STREAM):
        if stream not in {"atlas-15m", "scout-15m"}:
            raise ContractError("Explicit local intraday stream required")
        self.stream = stream
        self.broker, self.ledger, self.account_fingerprint = broker, ledger, account_fingerprint
        self.before_cancel, self.apply_evidence, self.record_event = before_cancel, apply_evidence, record_event
        self.clock, self.sleep, self.stopped = clock, sleep, stopped
        self.observe, self.is_temporary = observe, is_temporary

    def _record(self, kind, **payload):
        self.record_event(kind, {**payload, "observed_at": instant(self.clock()).isoformat(), "stream": self.stream})

    def _owned(self, reservation_id):
        reservation = self.ledger.lookup_reservation(reservation_id)
        if reservation_stream(self.ledger, reservation_id) != self.stream:
            raise ContractError("Cancellation outside the local 15m stream")
        if self.ledger.account_fingerprint != self.account_fingerprint:
            raise ContractError("Cancellation account binding changed")
        return reservation

    def _result(self, state, reservation_id, reason, *, terminal=False):
        current = self._owned(reservation_id)
        return CancellationResult(state, reservation_id, current.status, reason, terminal)

    def _retry(self, operation, *, reservation_id, stage):
        while not self.stopped():
            try:
                return True, operation()
            except Exception as exc:
                if not self.is_temporary(exc):
                    raise
                delay = retry_delay(exc, self.clock())
                self._record("cancellation_temporary_failure", reservation_id=reservation_id,
                             stage=stage, error_type=type(exc).__name__, retry_after_seconds=delay)
                if not self.stopped():
                    self.sleep(delay)
        return False, None

    def _observe(self, reservation_id):
        reservation = self._owned(reservation_id)
        if not reservation.broker_order_id:
            raise ContractError("Unknown submission retains identity without exact broker order ID")
        if self.broker.stable_account_fingerprint() != self.account_fingerprint:
            raise ContractError("Broker account changed before status read")
        evidence = (self.observe(reservation) if self.observe is not None else
                    read_exact_status(self.broker, reservation, account_fingerprint=self.account_fingerprint, clock=self.clock))
        if evidence is None:
            return None
        if (not isinstance(evidence, OrderEvidence) or evidence.reservation_id != reservation_id
                or evidence.broker_order_id != reservation.broker_order_id
                or evidence.account_fingerprint != self.account_fingerprint
                or evidence.order_quantity != reservation.quantity
                or sum(fill.quantity for fill in evidence.fills) != evidence.cumulative_filled_quantity
                or evidence.status not in TERMINAL | {"WORKING", "PARTIAL"}
                or instant(evidence.observed_at) > instant(self.clock())):
            raise ContractError("Complete matching order evidence required")
        filled, total, remaining = evidence.cumulative_filled_quantity, evidence.order_quantity, evidence.remaining_quantity
        if (any(type(value) is not int for value in (filled, total, remaining)) or not 0 <= filled <= total
                or remaining < 0 or len({fill.fill_id for fill in evidence.fills}) != len(evidence.fills)
                or any(type(fill.quantity) is not int or fill.quantity <= 0
                       or instant(fill.executed_at) > instant(evidence.observed_at)
                       or number(fill.price, positive=True) <= 0 for fill in evidence.fills)
                or (evidence.status == "FILLED" and (filled != total or remaining))
                or (evidence.status in {"CANCELLED", "REJECTED"} and remaining)
                or (evidence.status == "REJECTED" and filled)
                or (evidence.status in {"WORKING", "PARTIAL"} and (filled >= total or remaining != total-filled))
                or (evidence.status == "WORKING" and filled) or (evidence.status == "PARTIAL" and not filled)
                or (reservation.cancel_requested_at is not None
                    and instant(evidence.observed_at) < instant(reservation.cancel_requested_at))):
            raise ContractError("Complete cumulative fill and terminal quantity evidence required")
        self.apply_evidence(evidence)
        current = self._owned(reservation_id)
        if (current.status != evidence.status or current.filled_quantity != evidence.cumulative_filled_quantity
                or not current.last_evidence_at or instant(current.last_evidence_at) < instant(evidence.observed_at)):
            raise ContractError("Native reconciliation has not persisted the exact evidence")
        self._record("cancellation_status_reconciled", reservation_id=reservation_id,
                     evidence_id=evidence.evidence_id, status=evidence.status,
                     cumulative_filled_quantity=evidence.cumulative_filled_quantity)
        return evidence

    def step(self, reservation_id, *, reason, evidence_id, trigger_forecast_id=None, retry_send=False):
        """One bounded status/DELETE step; the durable outer worker sleeps unlocked.

        A persisted marker defaults to status recovery, preventing repeated DELETE
        acknowledgments. retry_send is only for a caller's durably recorded failed
        send, after it has respected provider waiting rules. The request identity
        and exact broker ID remain unchanged. No sleeps or retry loops occur here.
        """
        current = self._owned(reservation_id)
        if current.status in TERMINAL:
            return self._result("terminal", reservation_id, "ALREADY_RECORDED_TERMINAL", terminal=True)
        if self.stopped():
            return self._result("stopped", reservation_id, "RECOVERY_STOPPED_WITH_COMMITMENT_RETAINED")
        if not current.broker_order_id:
            return self._result("reconciliation_required", reservation_id, "EXACT_BROKER_ORDER_ID_UNAVAILABLE")
        stage = "status"
        try:
            evidence = self._observe(reservation_id)
            current = self._owned(reservation_id)
            if evidence is not None and current.status in TERMINAL:
                return self._result("terminal", reservation_id, "EXACT_TERMINAL_EVIDENCE", terminal=True)
            if evidence is None or current.status not in {"WORKING", "PARTIAL"}:
                return CancellationResult("pending", reservation_id, current.status, "EXACT_STATUS_PENDING", False, 3.)
            if current.cancel_requested_at is not None and not retry_send:
                self._record("cancellation_status_pending", reservation_id=reservation_id, result=current.status)
                return CancellationResult("pending", reservation_id, current.status, "PERSISTED_CANCELLATION_AWAITS_TERMINAL_STATUS", False, 3.)
            if evidence.broker_status == "PENDING_CANCEL":
                return CancellationResult("pending", reservation_id, current.status, "BROKER_CANCELLATION_PENDING", False, 3.)
            self.ledger.reserve_stream_cancellation(reservation_id, stream=self.stream, reason=reason,
                                                   as_of=instant(self.clock()).isoformat(), evidence_id=evidence_id)
            self._record("cancellation_requested", reservation_id=reservation_id, evidence_id=evidence_id,
                         trigger_forecast_id=trigger_forecast_id, reason=reason)
            broker_id = current.broker_order_id
            stage = "delete_exact_order"
            context = self.broker.prepare_order_submission()
            def gate():
                selected = self._owned(reservation_id)
                if (self.stopped() or selected.broker_order_id != broker_id or selected.status not in {"WORKING", "PARTIAL"}
                        or self.broker.stable_account_fingerprint() != self.account_fingerprint):
                    raise ContractError("Cancellation identity or worker state changed")
                self.before_cancel(context, selected)
            gate()
            self.broker.cancel_prepared_order(broker_id, context, before_delete=gate)
            self._record("cancellation_acknowledged", reservation_id=reservation_id, acknowledgment_is_terminal=False)
            return CancellationResult("pending", reservation_id, current.status, "ACKNOWLEDGMENT_REQUIRES_STATUS_RECONCILIATION", False, 3.)
        except Exception as exc:
            if self.is_temporary(exc):
                delay = retry_delay(exc, self.clock())
                self._record("cancellation_temporary_failure", reservation_id=reservation_id, stage=stage,
                             error_type=type(exc).__name__, retry_after_seconds=delay)
                return CancellationResult("pending", reservation_id, self._owned(reservation_id).status,
                                          "TEMPORARY_FAILURE_COMMITMENT_RETAINED", False, delay)
            self._record("cancellation_reconciliation_required", reservation_id=reservation_id,
                         stage=stage, error_type=type(exc).__name__)
            return self._result("reconciliation_required", reservation_id, "CANCELLATION_OR_EVIDENCE_NOT_CONFIRMED")

    def request(self, reservation_id, *, reason, evidence_id, trigger_forecast_id=None):
        """Persist the cancellation identity, then invoke the existing final gate."""
        current = self._owned(reservation_id)
        if current.status in TERMINAL:
            return self._result("terminal", reservation_id, "ALREADY_RECORDED_TERMINAL", terminal=True)
        if self.stopped():
            return self._result("stopped", reservation_id, "RECOVERY_STOPPED_WITH_COMMITMENT_RETAINED")
        if not current.broker_order_id:
            return self._result("reconciliation_required", reservation_id, "EXACT_BROKER_ORDER_ID_UNAVAILABLE")
        try:
            ok, _ = self._retry(lambda: self._observe(reservation_id), reservation_id=reservation_id, stage="status_before_cancel")
            if not ok:
                return self._result("stopped", reservation_id, "RECOVERY_STOPPED_WITH_COMMITMENT_RETAINED")
            current = self._owned(reservation_id)
            if current.status in TERMINAL:
                return self._result("terminal", reservation_id, "EXACT_TERMINAL_EVIDENCE", terminal=True)
            if current.status not in {"WORKING", "PARTIAL"}:
                return self._result("reconciliation_required", reservation_id, "WORKING_STATUS_NOT_CONFIRMED")
            self.ledger.reserve_stream_cancellation(reservation_id, stream=self.stream, reason=reason,
                                                   as_of=instant(self.clock()).isoformat(), evidence_id=evidence_id)
            self._record("cancellation_requested", reservation_id=reservation_id, evidence_id=evidence_id,
                         trigger_forecast_id=trigger_forecast_id, reason=reason)
            broker_id = current.broker_order_id
            def send():
                context = self.broker.prepare_order_submission()
                def gate():
                    selected = self._owned(reservation_id)
                    if self.stopped() or selected.broker_order_id != broker_id or selected.status not in {"WORKING", "PARTIAL"}:
                        raise ContractError("Cancellation identity or worker state changed")
                    if self.broker.stable_account_fingerprint() != self.account_fingerprint:
                        raise ContractError("Broker account changed before cancellation")
                    self.before_cancel(context, selected)
                gate()
                self.broker.cancel_prepared_order(broker_id, context, before_delete=gate)
            ok, _ = self._retry(send, reservation_id=reservation_id, stage="delete_exact_order")
            if not ok:
                return self._result("stopped", reservation_id, "RECOVERY_STOPPED_WITH_COMMITMENT_RETAINED")
            self._record("cancellation_acknowledged", reservation_id=reservation_id,
                         acknowledgment_is_terminal=False)
            return self._result("requested", reservation_id, "ACKNOWLEDGMENT_REQUIRES_STATUS_RECONCILIATION")
        except Exception as exc:
            self._record("cancellation_reconciliation_required", reservation_id=reservation_id, error_type=type(exc).__name__)
            return self._result("reconciliation_required", reservation_id, "CANCELLATION_OR_EVIDENCE_NOT_CONFIRMED")

    def recover(self, reservation_id):
        """Wait for exact terminal status; requests/empty history release nothing."""
        while not self.stopped():
            current = self._owned(reservation_id)
            if current.status in TERMINAL:
                return self._result("terminal", reservation_id, "EXACT_TERMINAL_EVIDENCE", terminal=True)
            if not current.broker_order_id:
                return self._result("reconciliation_required", reservation_id, "EXACT_BROKER_ORDER_ID_UNAVAILABLE")
            try:
                ok, evidence = self._retry(lambda: self._observe(reservation_id), reservation_id=reservation_id, stage="terminal_status")
                if not ok:
                    break
                if evidence is not None and evidence.status in TERMINAL:
                    return self._result("terminal", reservation_id, "EXACT_TERMINAL_EVIDENCE", terminal=True)
                self._record("cancellation_status_pending", reservation_id=reservation_id,
                             result="unresolved" if evidence is None else evidence.status)
                if not self.stopped():
                    self.sleep(3.0)
            except Exception as exc:
                self._record("cancellation_reconciliation_required", reservation_id=reservation_id, error_type=type(exc).__name__)
                return self._result("reconciliation_required", reservation_id, "COMPLETE_TERMINAL_EVIDENCE_UNAVAILABLE")
        return self._result("stopped", reservation_id, "RECOVERY_STOPPED_WITH_COMMITMENT_RETAINED")

    def cancel_and_reconcile(self, reservation_id, **kwargs):
        result = self.request(reservation_id, **kwargs)
        return self.recover(reservation_id) if result.state == "requested" else result
