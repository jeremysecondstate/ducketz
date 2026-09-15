"""User-selected stock direction bands, separate from model assessment."""
from __future__ import annotations

import math

STOCK_DIRECTION_POLICY_VERSION = "stock-direction-50-v2"
BULLISH_PROBABILITY = 0.50
BEARISH_PROBABILITY = 0.50


def stock_direction(probability: float) -> str:
    value = float(probability)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Stock direction requires a probability in [0, 1]")
    if value > BULLISH_PROBABILITY:
        return "BULLISH"
    if value < BEARISH_PROBABILITY:
        return "BEARISH"
    return "NO_EDGE"
