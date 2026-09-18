"""Versioned meaning of a frozen Gameplan's positive-class probability.

Historical publications without this field predict a return above round-trip
cost. New YG publications predict a strictly positive raw price return; their
cost-adjusted labels remain separate evidence, never silently relabeled.
"""
from __future__ import annotations

from collections.abc import Mapping
import math

import pandas as pd


RAW_DIRECTION_TARGET = "raw-price-direction-v1"
LEGACY_COST_TARGET = "cost-adjusted-positive-return-v1"


def resolve_probability_target(value=None) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return LEGACY_COST_TARGET
    if value not in (RAW_DIRECTION_TARGET, LEGACY_COST_TARGET):
        raise ValueError(f"Unknown Gameplan probability target: {value!r}")
    return str(value)


def probability_target_contract(metadata: Mapping | pd.DataFrame) -> str:
    if isinstance(metadata, pd.DataFrame):
        if "probability_target_contract" not in metadata:
            return LEGACY_COST_TARGET
        contracts = {resolve_probability_target(value) for value in metadata.probability_target_contract}
        if len(contracts) != 1:
            raise ValueError("Gameplan rows must share one probability target contract")
        return contracts.pop()
    return resolve_probability_target(metadata.get("probability_target_contract"))


def observed_probability_target(raw_return, cost, contract=None) -> int:
    raw = float(raw_return)
    assumed_cost = float(cost)
    if not math.isfinite(raw) or not math.isfinite(assumed_cost) or assumed_cost < 0:
        raise ValueError("Probability outcomes require finite returns and nonnegative costs")
    selected = resolve_probability_target(contract)
    return int(raw > (0.0 if selected == RAW_DIRECTION_TARGET else assumed_cost))


def probability_target_metadata(contract=None) -> dict[str, str]:
    selected = resolve_probability_target(contract)
    return {"probability_target_contract": selected,
            "gameplan_variant": "YG" if selected == RAW_DIRECTION_TARGET else "OG"}
