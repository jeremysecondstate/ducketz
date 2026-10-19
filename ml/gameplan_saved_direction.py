"""Read frozen Gameplan calls with their original stock direction policy."""
from __future__ import annotations

from typing import Mapping

import pandas as pd

from ml.stock_direction_policy import (
    BEARISH_PROBABILITY, BULLISH_PROBABILITY, STOCK_DIRECTION_POLICY_VERSION,
    stock_direction,
)

DIRECTION_POLICY_FIELDS = (
    "direction_policy_version", "direction_up_threshold", "direction_down_threshold",
)


def saved_gameplan_direction(forecast: Mapping, probability: float, *,
                            missing_legacy_direction: str | None = None) -> tuple[str, dict]:
    """Use saved historical calls; infer absent calls under their declared policy.

    Legacy publications without a policy retain the evaluator's historical 50%
    fallback, or the caller's original no-call fallback for actuals reviews.
    New publications must agree with the inclusive 50.01% / 49.99%
    policy. A no-history research forecast deliberately has no directional call.
    Binary probability targets and Brier scores are independent of these bands.
    """
    metadata = {key: None if pd.isna(forecast.get(key)) else forecast.get(key)
                for key in DIRECTION_POLICY_FIELDS}
    policy = metadata["direction_policy_version"]
    direction = forecast.get("direction")
    current = policy == STOCK_DIRECTION_POLICY_VERSION
    if current:
        for key, expected in (("direction_up_threshold", BULLISH_PROBABILITY),
                              ("direction_down_threshold", BEARISH_PROBABILITY)):
            if metadata[key] is not None and float(metadata[key]) != expected:
                raise ValueError("Saved Gameplan direction cutoffs disagree with its policy")
            metadata[key] = expected
        expected = ("NO_EDGE" if forecast.get("model_status") == "RESEARCH_NO_TARGET_HISTORY"
                    else stock_direction(probability))
    elif policy in (None, "stock-direction-50-v2"):
        expected = "BULLISH" if probability > .5 else "BEARISH" if probability < .5 else "NO_EDGE"
        if policy is None and missing_legacy_direction is not None:
            expected = missing_legacy_direction
    else:
        expected = None
    if pd.isna(direction):
        if expected is None:
            raise ValueError("Cannot infer a saved Gameplan direction under an unknown policy")
        direction = expected
    if direction not in {"BULLISH", "BEARISH", "NO_EDGE"}:
        raise ValueError("Saved Gameplan contains an invalid direction")
    if current and direction != expected:
        raise ValueError("Saved Gameplan direction disagrees with its declared policy")
    return direction, metadata
