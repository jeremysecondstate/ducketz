"""User-selected stock direction bands, separate from model assessment."""
from __future__ import annotations

import math

STOCK_DIRECTION_POLICY_VERSION = "stock-direction-5001-4999-v3"
BULLISH_PROBABILITY = 0.5001
BEARISH_PROBABILITY = 0.4999


def stock_direction(probability: float) -> str:
    value = float(probability)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Stock direction requires a probability in [0, 1]")
    if value >= BULLISH_PROBABILITY:
        return "BULLISH"
    if value <= BEARISH_PROBABILITY:
        return "BEARISH"
    return "NO_EDGE"


def stock_action(probability: float) -> str:
    """User-selected inclusive 50.01% BUY / 49.99% SELL rule for every trader."""
    return {"BULLISH": "BUY", "BEARISH": "SELL", "NO_EDGE": "HOLD"}[stock_direction(probability)]


def saved_stock_direction(probability: float, policy_version: object = None) -> str:
    """Validate a saved label without changing the current execution cutoffs."""
    direction = stock_direction(probability)
    if policy_version == "stock-direction-50-v2":
        return "BULLISH" if probability > .5 else "BEARISH" if probability < .5 else "NO_EDGE"
    return direction
