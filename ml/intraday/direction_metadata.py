"""Frozen stock actions, separate from scientific down/not-down classification.

Absent historical metadata stays absent. Only a genuinely new forecast receives
current policy; recovery projects the original action and documented cutoffs.
"""
from decimal import Decimal

from ml.stock_direction_policy import (
    STOCK_DIRECTION_POLICY_VERSION, BULLISH_PROBABILITY, BEARISH_PROBABILITY,
    stock_action,
)
from .prediction import ContractError, number

FIELDS = ("stock_action", "direction_policy_version",
          "direction_up_threshold", "direction_down_threshold")


def current_direction(probability):
    return dict(stock_action=stock_action(probability),
                direction_policy_version=STOCK_DIRECTION_POLICY_VERSION,
                direction_up_threshold=BULLISH_PROBABILITY,
                direction_down_threshold=BEARISH_PROBABILITY)


def recorded_direction(forecast):
    """Validate saved policy evidence; missing historical evidence is unrecorded."""
    present = set(FIELDS).intersection(forecast)
    if not present:
        return {}
    if present != set(FIELDS):
        raise ContractError("Stock direction policy metadata is incomplete")
    value = {key: forecast[key] for key in FIELDS}
    version = value["direction_policy_version"]
    # Only these identities have definitions here. Unknown names do not imply
    # equivalence, and today's execution policy cannot reinterpret a saved one.
    # The original main definition in commit11475171364b4d50a9aa72cb3fae9fb57ecc0969
    # explicitly binds this saved identity to the same exact inclusive cutoffs.
    contracts = {"stock-direction-5001-4999-v3": (Decimal("0.5001"), Decimal("0.4999"), True),
                 "stock-direction-50-01-v3": (Decimal("0.5001"), Decimal("0.4999"), True),
                 "stock-direction-50-v2": (Decimal("0.5"), Decimal("0.5"), False)}
    if not isinstance(version, str) or version not in contracts:
        raise ContractError("Unknown recorded stock direction policy")
    up, down, inclusive = contracts[version]
    if (number(value["direction_up_threshold"]) != up
            or number(value["direction_down_threshold"]) != down):
        raise ContractError("Stock direction cutoffs disagree with recorded policy")
    p = number(forecast["probability_not_down"])
    if p > 1:
        raise ContractError("Invalid stock direction probability")
    side = ("BUY" if (p >= up if inclusive else p > up) else
            "SELL" if (p <= down if inclusive else p < down) else "HOLD")
    if value["stock_action"] != side:
        raise ContractError("Stock action disagrees with recorded policy")
    return value


def stamp_new_direction(forecast):
    expected = current_direction(forecast["probability_not_down"])
    if any(key in forecast and forecast[key] != expected[key] for key in FIELDS):
        raise ContractError("New stock direction metadata conflicts with current policy")
    return {**forecast, **expected}


def recorded_action(forecast, *, legacy_side=None):
    recorded = recorded_direction(forecast)
    if recorded:
        return recorded["stock_action"]
    if legacy_side is not None:
        if legacy_side not in {"BUY", "SELL", "HOLD"}:
            raise ContractError("Invalid original legacy stock action")
        return legacy_side
    return stock_action(forecast["probability_not_down"])
