"""Nearest-share mechanics; trading references and score mapping stay explicit."""
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP

from .prediction import ContractError, UnresolvedPolicy, identity, number, quantity


@dataclass(frozen=True)
class SizingExplanation:
    symbol: str
    side: str
    reference_version: str
    score_version: str
    hourly_reference: int
    score: str
    raw_quantity: str
    desired_quantity: int
    half_share_ties: str | None
    rounded_equality: str | None
    inputs: dict

    def payload(self):
        return asdict(self)


def size(*, symbol, side, hourly_reference, reference_version, score, score_version,
         inputs, half_share_ties=None, rounded_equality=None):
    if side not in {"BUY", "SELL"}:
        raise ContractError("BUY or SELL required")
    for value in (symbol, reference_version, score_version):
        identity(value)
    hourly = quantity(hourly_reference, positive=True)
    value = number(score)
    if value >= 1:
        raise ContractError("Raw 15m sizing score must be below one")
    raw = hourly * value
    if half_share_ties not in {None, "half_even", "half_up"}:
        raise ContractError("Unsupported half-share rule")
    if raw % 1 == number("0.5") and half_share_ties is None:
        raise UnresolvedPolicy("Half-share ties require an explicit sizing rule")
    rounding = ROUND_HALF_UP if half_share_ties == "half_up" else ROUND_HALF_EVEN
    desired = int(raw.to_integral_value(rounding=rounding))
    if rounded_equality not in {None, "allow"}:
        raise ContractError("No flooring or minimum-trade veto is selected")
    if desired == hourly and rounded_equality is None:
        raise UnresolvedPolicy("Nearest rounding equals hourly size; explicit handling required")
    return SizingExplanation(symbol, side, reference_version, score_version, hourly, str(value), str(raw),
                             desired, half_share_ties, rounded_equality, dict(inputs))
