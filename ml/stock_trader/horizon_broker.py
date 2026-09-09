"""Strict read-only Schwab order evidence for the horizon inventory ledger.

No submitted quantity or top-level filled quantity is treated as a fill. Every
filled share requires complete execution-leg detail, and uncertain/missing or
truncated history fails closed. This module has no submission/cancellation API.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Callable, Mapping, Protocol

from ml.stock_trader.horizon_ledger import (
    FillEvidence, HorizonLedger, LedgerError, OrderEvidence, ReservationState, _utc,
)


ORDER_HISTORY_LIMIT = 3000
_WORKING = {
    "ACCEPTED", "WORKING", "QUEUED", "PENDING_ACTIVATION", "AWAITING_PARENT_ORDER",
    "AWAITING_CONDITION", "AWAITING_STOP_CONDITION", "AWAITING_MANUAL_REVIEW",
    "PENDING_ACKNOWLEDGEMENT", "PENDING_CANCEL", "PENDING_RECALL", "PENDING_REPLACE",
    "AWAITING_RELEASE_TIME", "NEW", "PARTIALLY_FILLED",
}
_CANCELLED = {"CANCELED", "CANCELLED", "EXPIRED"}


class OrderHistoryError(LedgerError):
    pass


class OrderHistorySession(Protocol):
    def stable_account_fingerprint(self) -> str: ...
    def get_orders(self, *, from_entered_time: datetime, to_entered_time: datetime,
                   max_results: int): ...


def _number(value: object, *, whole: bool = False, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise OrderHistoryError("INVALID_BROKER_NUMERIC_EVIDENCE") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise OrderHistoryError("INVALID_BROKER_NUMERIC_EVIDENCE")
    if whole and result != result.to_integral_value():
        raise OrderHistoryError("FRACTIONAL_EXECUTION_IS_NOT_A_WHOLE_SHARE_ORDER")
    return result


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _walk_orders(rows):
    for row in rows:
        if not isinstance(row, Mapping):
            raise OrderHistoryError("ORDER_HISTORY_CONTAINS_INVALID_ROWS")
        yield row
        children = row.get("childOrderStrategies", ())
        if not isinstance(children, (list, tuple)):
            raise OrderHistoryError("ORDER_HISTORY_CHILDREN_ARE_INVALID")
        yield from _walk_orders(children)


def _fills(raw: Mapping, *, broker_id: str, order_leg_id: str, observed: str) -> tuple[FillEvidence, ...]:
    activities = raw.get("orderActivityCollection", ())
    if not isinstance(activities, (list, tuple)):
        raise OrderHistoryError("EXECUTION_ACTIVITY_COLLECTION_REQUIRED")
    records = []
    explicit_ids = set()
    for activity in activities:
        if not isinstance(activity, Mapping):
            raise OrderHistoryError("MALFORMED_EXECUTION_ACTIVITY")
        kind = _text(activity.get("activityType")).upper()
        executions = activity.get("executionLegs", ())
        if kind and kind not in {"EXECUTION", "FILL"}:
            if executions:
                raise OrderHistoryError("NON_EXECUTION_ACTIVITY_CONTAINS_EXECUTION_LEGS")
            continue
        if not isinstance(executions, (list, tuple)) or (kind in {"EXECUTION", "FILL"} and not executions):
            raise OrderHistoryError("COMPLETE_EXECUTION_LEGS_REQUIRED")
        for leg in executions:
            if not isinstance(leg, Mapping):
                raise OrderHistoryError("MALFORMED_EXECUTION_LEG")
            leg_id = _text(leg.get("legId")) or order_leg_id
            if leg_id != order_leg_id:
                raise OrderHistoryError("EXECUTION_LEG_DOES_NOT_MATCH_ORDER_LEG")
            quantity = int(_number(leg.get("quantity"), whole=True, positive=True))
            price = format(_number(leg.get("price"), positive=True).normalize(), "f")
            stamp = leg.get("time") or leg.get("executionTime") or activity.get("executionTime") or activity.get("time")
            if not stamp:
                raise OrderHistoryError("EXACT_EXECUTION_TIMESTAMP_REQUIRED")
            executed_at = _utc(stamp)
            if executed_at > observed:
                raise OrderHistoryError("EXECUTION_POSTDATES_CAPTURE_BOUNDARY")
            execution_id = _text(leg.get("executionId"))
            activity_id = _text(activity.get("activityId"))
            if execution_id:
                # Reusing an explicit execution identity is ambiguous, even if
                # the provider repeats identical economics in this response.
                identity = (broker_id, leg_id, execution_id)
                if identity in explicit_ids:
                    raise OrderHistoryError("DUPLICATE_EXPLICIT_EXECUTION_ID")
                explicit_ids.add(identity)
                key = ("execution", *identity)
            else:
                # Preserve repeated identical fills by ordinal within their
                # complete economics, not their changing list/response order.
                key = ("derived", broker_id, activity_id, leg_id, executed_at, quantity, price)
            records.append((key, quantity, price, executed_at))
    occurrences = Counter()
    output = []
    for key, quantity, price, executed_at in sorted(records, key=lambda value: json.dumps(value[0])):
        ordinal = occurrences[key]
        occurrences[key] += 1
        output.append(FillEvidence("schwab-fill:" + _hash([key, ordinal]), quantity, price, executed_at))
    return tuple(output)


def normalize_order_evidence(raw: Mapping, reservation: ReservationState, *, account_fingerprint: str,
                             observed_at: str) -> OrderEvidence:
    """Normalize one exact tracked single-leg equity order without mutating state."""
    observed = _utc(observed_at)
    broker_id = _text(raw.get("orderId"))
    if not broker_id or broker_id != reservation.broker_order_id:
        raise OrderHistoryError("TRACKED_BROKER_ORDER_ID_MISMATCH")
    legs = raw.get("orderLegCollection")
    if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], Mapping):
        raise OrderHistoryError("EXACT_SINGLE_EQUITY_ORDER_LEG_REQUIRED")
    leg = legs[0]
    instrument = leg.get("instrument")
    if not isinstance(instrument, Mapping) or _text(instrument.get("assetType")).upper() != "EQUITY":
        raise OrderHistoryError("TRACKED_ORDER_IS_NOT_EQUITY")
    if _text(instrument.get("symbol")).upper() != reservation.symbol or _text(leg.get("instruction")).upper() != reservation.side:
        raise OrderHistoryError("TRACKED_ORDER_SYMBOL_OR_SIDE_MISMATCH")
    quantity = int(_number(raw.get("quantity"), whole=True, positive=True))
    leg_quantity = int(_number(leg.get("quantity"), whole=True, positive=True))
    if quantity != reservation.quantity or leg_quantity != quantity:
        raise OrderHistoryError("TRACKED_ORDER_QUANTITY_MISMATCH")
    filled = int(_number(raw.get("filledQuantity"), whole=True))
    remaining = int(_number(raw.get("remainingQuantity"), whole=True))
    if filled > quantity:
        raise OrderHistoryError("BROKER_FILLED_MORE_THAN_RESERVED_QUANTITY")
    broker_status = _text(raw.get("status")).upper()
    if broker_status == "FILLED":
        if filled != quantity or remaining:
            raise OrderHistoryError("FILLED_ORDER_HAS_INCONSISTENT_QUANTITIES")
        status = "FILLED"
    elif broker_status in _CANCELLED or broker_status == "REJECTED":
        if remaining not in {0, quantity - filled} or (broker_status == "REJECTED" and filled):
            raise OrderHistoryError("TERMINAL_ORDER_HAS_INCONSISTENT_QUANTITIES")
        status = "REJECTED" if broker_status == "REJECTED" else "CANCELLED"
        remaining = 0  # Confirmed terminal cancellation has no live reservation.
    elif broker_status in _WORKING:
        if filled >= quantity or remaining != quantity - filled:
            raise OrderHistoryError("WORKING_ORDER_HAS_INCONSISTENT_QUANTITIES")
        status = "PARTIAL" if filled else "WORKING"
    else:
        # Replaced/unknown/suspended identities cannot silently release cash or
        # invent a successor order. Require explicit operator reconciliation.
        raise OrderHistoryError("UNSUPPORTED_OR_UNCERTAIN_BROKER_ORDER_STATUS")
    fills = _fills(raw, broker_id=broker_id, order_leg_id=_text(leg.get("legId")) or "1", observed=observed)
    if sum(item.quantity for item in fills) != filled:
        raise OrderHistoryError("COMPLETE_PER_ORDER_FILL_EVIDENCE_REQUIRED")
    payload = {"reservation_id": reservation.reservation_id, "account_fingerprint": account_fingerprint,
        "observed_at": observed, "broker_order_id": broker_id, "status": status, "order_quantity": quantity,
        "cumulative_filled_quantity": filled, "remaining_quantity": remaining,
        "fills": tuple(asdict(fill) for fill in fills), "broker_status": broker_status}
    return OrderEvidence("schwab-order:" + _hash(payload), reservation.reservation_id, account_fingerprint,
                         observed, broker_id, status, quantity, filled, remaining, fills, broker_status)


def capture_order_evidence(session: OrderHistorySession, ledger: HorizonLedger, *,
                           account_fingerprint: str, as_of: str,
                           observation_clock: Callable[[], object] | None = None) -> tuple[OrderEvidence, ...]:
    """Read bounded history once; the caller next captures a newer portfolio.

    An unknown submission with no broker order ID cannot be matched by symbol,
    price, quantity or proximity. It remains reserved for explicit investigation.
    """
    if account_fingerprint != ledger.account_fingerprint:
        raise OrderHistoryError("ACCOUNT_FINGERPRINT_MISMATCH")
    pending = ledger.pending_reservations()
    if not pending:
        return ()
    if any(not reservation.broker_order_id for reservation in pending):
        raise OrderHistoryError("UNRESOLVED_SUBMISSION_WITHOUT_EXACT_BROKER_ORDER_ID")
    if session.stable_account_fingerprint() != account_fingerprint:
        raise OrderHistoryError("BROKER_ACCOUNT_CHANGED_BEFORE_HISTORY_READ")
    observed = _utc(as_of)
    boundary = datetime.fromisoformat(observed)
    raw = session.get_orders(from_entered_time=boundary - timedelta(days=14),
                             to_entered_time=boundary, max_results=ORDER_HISTORY_LIMIT)
    if session.stable_account_fingerprint() != account_fingerprint:
        raise OrderHistoryError("BROKER_ACCOUNT_CHANGED_DURING_HISTORY_READ")
    if observation_clock is not None:
        completed = _utc(observation_clock())
        if completed < observed:
            raise OrderHistoryError("ORDER_OBSERVATION_CLOCK_MOVED_BACKWARDS")
        observed = completed
    if not isinstance(raw, list):
        raise OrderHistoryError("ORDER_HISTORY_IS_NOT_A_COMPLETE_LIST")
    if len(raw) >= ORDER_HISTORY_LIMIT:
        raise OrderHistoryError("ORDER_HISTORY_MAY_BE_TRUNCATED")
    by_id = {}
    for row in _walk_orders(raw):
        identity = _text(row.get("orderId"))
        if not identity:
            raise OrderHistoryError("ORDER_HISTORY_ROW_MISSING_ID")
        if identity in by_id:
            raise OrderHistoryError("ORDER_HISTORY_CONTAINS_DUPLICATE_ORDER_IDS")
        by_id[identity] = row
    evidence = []
    for reservation in pending:
        row = by_id.get(reservation.broker_order_id)
        if row is None:
            raise OrderHistoryError("TRACKED_ORDER_MISSING_FROM_BOUNDED_HISTORY")
        evidence.append(normalize_order_evidence(row, reservation, account_fingerprint=account_fingerprint,
                                                 observed_at=observed))
    return tuple(evidence)
