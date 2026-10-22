"""User-selected stock direction bands, separate from model assessment."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

STOCK_DIRECTION_POLICY_VERSION = "stock-direction-5001-4999-v3"
BULLISH_PROBABILITY = 0.5001
BEARISH_PROBABILITY = 0.4999


def stock_direction(probability: float) -> str:
    try:
        value = Decimal(str(probability))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Stock direction requires a probability in [0, 1]") from exc
    if not value.is_finite() or not 0 <= value <= 1:
        raise ValueError("Stock direction requires a probability in [0, 1]")
    if value >= Decimal(str(BULLISH_PROBABILITY)):
        return "BULLISH"
    if value <= Decimal(str(BEARISH_PROBABILITY)):
        return "BEARISH"
    return "NO_EDGE"


def stock_action(probability: float) -> str:
    """Inclusive 50.01% BUY / 49.99% SELL, using the unrounded probability."""
    return {"BULLISH": "BUY", "BEARISH": "SELL", "NO_EDGE": "HOLD"}[stock_direction(probability)]


def saved_stock_direction(probability: float, policy_version: object = None) -> str:
    """Retain the documented strict-50 historical definition when recorded."""
    direction = stock_direction(probability)
    if policy_version == "stock-direction-50-v2":
        value = Decimal(str(probability))
        return "BULLISH" if value > Decimal(".5") else "BEARISH" if value < Decimal(".5") else "NO_EDGE"
    return direction
