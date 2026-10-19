"""Portable audit metadata for newly written Gameplan artifacts.

Saved artifacts and their policy labels are never rewritten by this helper.
The metadata records existing direction and ownership behavior; it adds no
execution permission, model choice or nightly operating prerequisite.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from typing import Mapping

from ml.stock_direction_policy import (
    STOCK_DIRECTION_POLICY_VERSION, BULLISH_PROBABILITY, BEARISH_PROBABILITY,
)
from ml.stock_trader.cross_horizon_fallback import (
    AUTONOMOUS_OWNERSHIP_POLICY_VERSION, AUTONOMY_EFFECTIVE_ACTION_DATE, autonomous_horizon,
)


POLICY_FIELDS = ("direction_policy_version", "direction_up_threshold", "direction_down_threshold",
                 "stock_execution_policy")


def current_gameplan_policy_metadata(action_date) -> dict:
    """Describe current new-decision cutoffs and the dated ownership policy."""
    day = action_date.isoformat() if isinstance(action_date, date) else str(action_date)
    return {
        "direction_policy_version": STOCK_DIRECTION_POLICY_VERSION,
        "direction_up_threshold": BULLISH_PROBABILITY,
        "direction_down_threshold": BEARISH_PROBABILITY,
        "stock_execution_policy": {
            "probability_precision": "unrounded",
            "buy_comparison": ">=", "sell_comparison": "<=",
            "hold_comparison": "strictly_between_thresholds",
            "ownership_policy_version": AUTONOMOUS_OWNERSHIP_POLICY_VERSION,
            "ownership_effective_action_date": AUTONOMY_EFFECTIVE_ACTION_DATE,
            "autonomous_horizons": ["15m", *(h for h in ("1h", "4h") if autonomous_horizon(h, day))],
            "unchanged_horizons": ["1d", "1w"],
            "reserved_sell_shares": "unavailable",
            "price_basis": "live_bid_ask_midpoint",
        },
    }


def saved_gameplan_policy_metadata(configuration: Mapping) -> dict:
    """Copy optional saved audit fields without assigning a policy to history."""
    return {key: deepcopy(configuration[key]) for key in POLICY_FIELDS if key in configuration}
