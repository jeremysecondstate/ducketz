"""Strict exact-order status evidence for independent trader adapters.

The caller supplies its existing broker, tracked reservation and account binding.
This reader neither sends/cancels orders nor changes execution or capacity state.
Extracted unchanged from cancellation; caller adoption is a separate source unit.
"""
from __future__ import annotations

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
