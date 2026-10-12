"""Whole-share opportunity sizing after independent desired sizing.

The account authority supplies already reconciled, uncommitted capacity. This
calculation never reserves a budget, applies horizon allocations or changes the
original intention. Admission must recheck it atomically before an order.
"""
from dataclasses import asdict, dataclass
from decimal import ROUND_FLOOR

from .prediction import ContractError, digest, identity, instant, number, quantity

VERSION = "intraday-available-account-capacity-v1"


@dataclass(frozen=True)
class CapacityDecision:
    symbol: str
    side: str
    desired_quantity: int
    adjusted_quantity: int
    limit_price: str
    available_cash: str
    available_shares: int
    capacity_evidence_id: str
    observed_at: str
    calculated_at: str
    disposition: str
    version: str = VERSION

    def payload(self):
        return asdict(self)

    @property
    def decision_id(self):
        return digest(self.payload())


def clip_to_capacity(*, symbol, side, desired_quantity, limit_price,
                     available_cash, available_shares, capacity_evidence_id,
                     observed_at, calculated_at):
    """Take the full intention or the smaller whole-share trade possible.

    Cash is already net of pending BUY commitments and shares are already net
    of pending SELL commitments. Subtracting them again here would be wrong.
    Zero capacity is a recorded result, not a lost forecast or fabricated fill.
    """
    identity(symbol); identity(capacity_evidence_id)
    if side not in {"BUY", "SELL"}:
        raise ContractError("BUY or SELL required")
    desired = quantity(desired_quantity)
    cash, price = number(available_cash), number(limit_price, positive=True)
    shares = quantity(available_shares)
    observed, calculated = instant(observed_at), instant(calculated_at)
    if observed > calculated:
        raise ContractError("Capacity was not available at decision time")
    possible = int((cash / price).to_integral_value(rounding=ROUND_FLOOR)) if side == "BUY" else shares
    adjusted = min(desired, possible)
    if desired == 0:
        disposition = "sizing_produced_zero"
    elif adjusted == 0:
        disposition = "no_purchasing_capacity" if side == "BUY" else "no_eligible_uncommitted_shares"
    elif adjusted < desired:
        disposition = "smaller_trade_possible"
    else:
        disposition = "full_intention_possible"
    return CapacityDecision(symbol, side, desired, adjusted, str(price), str(cash), shares,
                            capacity_evidence_id, observed.isoformat(), calculated.isoformat(), disposition)
