"""User-selected stock direction bands, separate from model assessment."""
from __future__ import annotations

import math
from datetime import date

STOCK_DIRECTION_POLICY_VERSION = "stock-direction-50-01-v3"
BULLISH_PROBABILITY = 0.5001
BEARISH_PROBABILITY = 0.4999
AUTONOMOUS_HORIZON_SALES_EFFECTIVE_DATE = "2026-10-19"


def autonomous_horizon_sales(horizon: str, action_date: object) -> bool:
    """Apply the October 19 prospective ownership rule to hourly horizons."""
    return (horizon in {"1h", "4h"}
            and date.fromisoformat(str(action_date)) >= date.fromisoformat(AUTONOMOUS_HORIZON_SALES_EFFECTIVE_DATE))


def stock_direction(probability: float) -> str:
    value = float(probability)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Stock direction requires a probability in [0, 1]")
    if value >= BULLISH_PROBABILITY:
        return "BULLISH"
    if value <= BEARISH_PROBABILITY:
        return "BEARISH"
    return "NO_EDGE"


def stock_direction_for_policy(probability: float, policy_version: object = None) -> str:
    """Validate a frozen label using its original, recorded direction policy."""
    current = stock_direction(probability)
    if policy_version in (None, "stock-direction-50-v2"):
        value = float(probability)
        return "BULLISH" if value > .5 else "BEARISH" if value < .5 else "NO_EDGE"
    if policy_version != STOCK_DIRECTION_POLICY_VERSION:
        raise ValueError("Unsupported saved stock direction policy")
    return current
