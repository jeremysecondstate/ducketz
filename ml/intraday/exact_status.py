"""Strict exact-order status evidence for independent trader adapters.

The caller supplies its existing broker, tracked reservation and account binding.
This reader neither sends/cancels orders nor changes execution or capacity state.
Status and retry-delay helpers preserve the original cancellation semantics.
"""
from __future__ import annotations

from datetime import timezone
from email.utils import parsedate_to_datetime
import math

from ml.stock_trader.horizon_broker import capture_order_evidence
from .prediction import ContractError, instant

TERMINAL = frozenset({"FILLED", "CANCELLED", "REJECTED"})


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
