"""Current direction-only Gameplan instructions derived from saved predictions.

This pure helper preserves saved labels as provenance. It neither sizes orders
nor reads another trader's state, providers, models or account bindings.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import math
from typing import Mapping

from ml.stock_direction_policy import (
    STOCK_DIRECTION_POLICY_VERSION, STOCK_DIRECTION_POLICY_ALIASES,
    BULLISH_PROBABILITY, BEARISH_PROBABILITY, stock_action, stock_direction,
    autonomous_horizon_sales, AUTONOMOUS_HORIZON_SALES_EFFECTIVE_DATE,
)

GAMEPLAN_RECIPE_VERSION = "direction-live-quote-v1"
DIRECTION_ONLY_STATUS = "DIRECTION_ONLY"


def is_current_gameplan_recipe(value: Mapping) -> bool:
    return (value.get("gameplan_recipe_version") == GAMEPLAN_RECIPE_VERSION
            and value.get("execution_quantity_basis") == "live_broker_capacity")


def current_gameplan_policy_metadata(action_date) -> dict:
    day = action_date.isoformat() if isinstance(action_date, date) else str(action_date)
    date.fromisoformat(day)
    return {
        "gameplan_recipe_version": GAMEPLAN_RECIPE_VERSION,
        "execution_quantity_basis": "live_broker_capacity",
        "direction_policy_version": STOCK_DIRECTION_POLICY_VERSION,
        "direction_up_threshold": BULLISH_PROBABILITY,
        "direction_down_threshold": BEARISH_PROBABILITY,
        "stock_execution_policy": {
            "probability_precision": "unrounded", "buy_comparison": ">=", "sell_comparison": "<=",
            "hold_comparison": "strictly_between_thresholds",
            "ownership_policy_version": "account-wide-1h-4h-directional-ownership-v1",
            "ownership_effective_action_date": AUTONOMOUS_HORIZON_SALES_EFFECTIVE_DATE,
            "autonomous_horizons": ["15m", *(h for h in ("1h", "4h") if autonomous_horizon_sales(h, day))],
            "unchanged_horizons": ["1d", "1w"], "reserved_sell_shares": "unavailable",
            "price_basis": "live_bid_ask_midpoint",
        },
    }


def direction_only_rows(forecasts, *, action_date, snapshot: Mapping | None = None):
    """Derive current actions without altering historical probability or labels."""
    import pandas as pd

    metadata = current_gameplan_policy_metadata(action_date)
    scalar = {key: metadata[key] for key in ("gameplan_recipe_version", "execution_quantity_basis",
        "direction_policy_version", "direction_up_threshold", "direction_down_threshold")}

    def observed(value):
        if isinstance(value, bool):
            return None
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if math.isfinite(result) and result >= 0 else None

    records = []
    for saved in forecasts.to_dict("records"):
        row = {key: deepcopy(value) for key, value in saved.items()
               if not key.startswith(("direction_based_", "projected_", "trade_", "scheduled_trade_",
                   "historical_price_", "planning_", "price_band_", "price_reference_", "fallback_"))
               and key != "stock_execution_policy"}
        # An immutable scientific label is evidence, not a second current gate.
        original_direction = saved.get("source_saved_direction")
        has_original_direction = original_direction is not None and not pd.isna(original_direction)
        row["source_saved_direction"] = original_direction if has_original_direction else saved.get("direction")
        source_policy = saved.get("source_saved_direction_policy_version") if has_original_direction else saved.get("direction_policy_version")
        row["source_saved_direction_policy_version"] = None if source_policy is None or pd.isna(source_policy) else source_policy
        probability = saved["calibrated_probability"]
        action, direction = stock_action(probability), stock_direction(probability)
        reason = {"BUY": "BUY_AT_CURRENT_ACCOUNT_CAPACITY", "SELL": "SELL_FROM_CURRENT_ELIGIBLE_HOLDINGS",
                  "HOLD": "NEUTRAL"}[action]
        if not bool(saved["execution_eligible"]):
            action, reason = "CONTEXT", "NON_ENTRY_CONTEXT"
        row.update(scalar, direction=direction, direction_based_action=action,
                   direction_based_trade_quantity=0 if action == "HOLD" else None,
                   direction_based_reason=reason)
        if snapshot is not None:
            held = observed(snapshot.get("held_shares", {}).get(row["symbol"]))
            reserved = observed(snapshot.get("pending_sell_shares", {}).get(row["symbol"], 0))
            row["observed_available_account_shares"] = max(0., held - reserved) if held is not None and reserved is not None else None
            row["account_observed_at"] = snapshot.get("observed_at")
            quote = snapshot.get("quotes", {}).get(row["symbol"], {})
            for key in ("bid", "ask", "midpoint", "mark"):
                row["observed_" + key] = observed(quote.get(key))
        records.append(row)
    ledger = {**metadata, "status": DIRECTION_ONLY_STATUS, "events": [], "hourly": [],
        "ending_positions": {}, "summary": {}, "orders_placed": 0, "broker_orders_enabled": False,
        "holding_policy": "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1",
        "reason": "Saved directional instructions; quantities use live cash, eligible holdings and bid/ask quotes."}
    return pd.DataFrame(records), ledger


def validate_direction_only_plan(plan: Mapping) -> None:
    """Validate the current recipe without reclassifying original publications."""
    import pandas as pd

    for item in (plan, plan.get("ledger", {})):
        if (not is_current_gameplan_recipe(item)
                or item.get("direction_policy_version") not in STOCK_DIRECTION_POLICY_ALIASES
                or item.get("direction_up_threshold") != BULLISH_PROBABILITY
                or item.get("direction_down_threshold") != BEARISH_PROBABILITY):
            raise ValueError("Current direction policy metadata differs")
    ledger = plan["ledger"]
    if (ledger.get("status") != DIRECTION_ONLY_STATUS or ledger.get("events") != []
            or ledger.get("hourly") != [] or ledger.get("ending_positions") != {}
            or ledger.get("summary") != {} or ledger.get("orders_placed") != 0
            or ledger.get("broker_orders_enabled") is not False):
        raise ValueError("Direction-only plan cannot carry projected fills or orders")
    rows = plan.get("forecasts")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Current plan requires its original forecasts")
    expected, _ = direction_only_rows(pd.DataFrame(rows), action_date=plan["action_date"])
    for row, derived in zip(rows, expected.to_dict("records")):
        if (not is_current_gameplan_recipe(row)
                or row.get("direction_policy_version") not in STOCK_DIRECTION_POLICY_ALIASES
                or row.get("direction_up_threshold") != BULLISH_PROBABILITY
                or row.get("direction_down_threshold") != BEARISH_PROBABILITY
                or row.get("direction") != derived["direction"]
                or row.get("direction_based_action") != derived["direction_based_action"]
                or row.get("direction_based_reason") != derived["direction_based_reason"]
                or row.get("direction_based_trade_quantity") != (0 if derived["direction_based_action"] == "HOLD" else None)):
            raise ValueError("Current directional instruction differs from its saved probability")
