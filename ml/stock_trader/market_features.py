"""Causal market inputs shared by frozen forecasts and stock sizing fits."""
from __future__ import annotations

import json
from typing import Mapping

from ml.stock_trader.contracts import finite


INDEPENDENT_MARKET_FEATURE_NAMES = (
    "mr__trend_atr", "mr__momentum_risk_adjusted", "mr__range_position",
    "mr__volume_score", "mr__volatility_ratio", "mr__atr_percent",
    "bp__compression_score", "bp__direction_score",
)
INDEPENDENT_MARKET_FEATURE_CONTRACT = "frozen-causal-stock-market-inputs-v1"


def frozen_market_feature_values(row: Mapping, *, required: bool = True) -> dict[str, float]:
    values = {name: finite(row.get(name)) for name in INDEPENDENT_MARKET_FEATURE_NAMES}
    missing = [name for name, value in values.items() if value is None]
    if missing:
        if required:
            raise ValueError("Frozen stock market inputs missing or nonfinite: " + ", ".join(missing))
        return {}
    return {name: float(value) for name, value in values.items()}


def read_frozen_market_feature_values(row: Mapping) -> dict[str, float]:
    """Read only values embedded in the verified immutable forecast row.

    Older publications have no market inputs. They remain readable, but a model
    fitted with this contract must reject their absent inputs before execution.
    """
    raw = row.get("enrichment_feature_values_json")
    if raw is None:
        return {}
    if not isinstance(raw, str):
        raise ValueError("Frozen stock market inputs must be a JSON object string")
    values = json.loads(raw)
    if not isinstance(values, dict):
        raise ValueError("Frozen stock market inputs must be an object")
    if not values:
        return {}
    if row.get("enrichment_feature_contract") != INDEPENDENT_MARKET_FEATURE_CONTRACT:
        raise ValueError("Frozen stock market input contract version differs")
    if set(values) != set(INDEPENDENT_MARKET_FEATURE_NAMES):
        raise ValueError("Frozen stock market input names differ from their contract")
    return frozen_market_feature_values(values)
