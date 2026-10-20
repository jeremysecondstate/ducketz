"""User-selected stock direction bands, separate from model assessment."""
from __future__ import annotations

import math
from datetime import date
from typing import Iterable, Mapping

STOCK_DIRECTION_POLICY_VERSION = "stock-direction-50-01-v3"
STOCK_DIRECTION_POLICY_ALIASES = frozenset({STOCK_DIRECTION_POLICY_VERSION, "stock-direction-5001-4999-v3"})
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
    if policy_version not in STOCK_DIRECTION_POLICY_ALIASES:
        raise ValueError("Unsupported saved stock direction policy")
    return current


def stock_action(probability: float) -> str:
    """Map an original probability to the inclusive current execution rule."""
    return {"BULLISH": "BUY", "BEARISH": "SELL", "NO_EDGE": "HOLD"}[stock_direction(probability)]


def saved_direction_policy_summary(records: Iterable[Mapping]) -> dict:
    """Describe recorded forecast cutoffs without changing historical labels.

    A missing version is deliberately left unrecorded. Older publications may
    predate either named policy, so today's default supplies no historical rule.
    """
    versions, unrecorded = set(), 0
    for row in records:
        version = row.get("direction_policy_version")
        if version is None or (isinstance(version, float) and math.isnan(version)):
            unrecorded += 1
        else:
            versions.add(str(version))
    known = {
        "stock-direction-50-v2": {"bullish_probability": .5, "bearish_probability": .5,
                                  "boundaries": "exclusive", "neutral": "exactly_0.5"},
        STOCK_DIRECTION_POLICY_VERSION: {"bullish_probability": BULLISH_PROBABILITY,
            "bearish_probability": BEARISH_PROBABILITY, "boundaries": "inclusive",
            "neutral": "strictly_between_bearish_and_bullish"},
    }
    for version in STOCK_DIRECTION_POLICY_ALIASES:
        known[version] = dict(known[STOCK_DIRECTION_POLICY_VERSION])
    return {"direction_policy_evaluation": "saved_frozen_direction_labels",
            "direction_policy_versions": sorted(versions),
            "unrecorded_direction_policy_forecasts": unrecorded,
            "direction_policy_cutoffs": {version: known[version] for version in sorted(versions) if version in known}}
