"""Custom technical calculation registry."""

from collections.abc import Callable

import pandas as pd

from technicals.calculations.bar_shape import (
    CALCULATION_NAME as BAR_SHAPE_NAME,
    CALCULATION_VERSION as BAR_SHAPE_VERSION,
    calculate_bar_shape,
)
from technicals.calculations.market_regime import (
    CALCULATION_NAME as MARKET_REGIME_NAME,
    CALCULATION_VERSION as MARKET_REGIME_VERSION,
    calculate_market_regime,
)
from technicals.calculations.session_aware_breakout import (
    CALCULATION_NAME as BREAKOUT_PRESSURE_NAME,
    CALCULATION_VERSION as BREAKOUT_PRESSURE_VERSION,
    calculate_breakout_pressure,
)
from technicals.calculations.weekly_context import (
    CALCULATION_NAME as WEEKLY_CONTEXT_NAME,
    CALCULATION_VERSION as WEEKLY_CONTEXT_VERSION,
    OUTPUT_TIMEFRAME as WEEKLY_CONTEXT_OUTPUT_TIMEFRAME,
    WeeklyContextNotReady,
    calculate_weekly_context,
)

Calculation = Callable[..., pd.DataFrame]

CALCULATIONS: dict[str, Calculation] = {
    MARKET_REGIME_NAME: calculate_market_regime,
    BREAKOUT_PRESSURE_NAME: calculate_breakout_pressure,
    BAR_SHAPE_NAME: calculate_bar_shape,
    WEEKLY_CONTEXT_NAME: calculate_weekly_context,
}

DEFAULT_CALCULATIONS = (
    MARKET_REGIME_NAME,
    BREAKOUT_PRESSURE_NAME,
    BAR_SHAPE_NAME,
    WEEKLY_CONTEXT_NAME,
)

_INPUT_POLICIES = {
    BAR_SHAPE_NAME: (frozenset({"databento"}), frozenset({"1h", "1d"})),
    WEEKLY_CONTEXT_NAME: (frozenset({"databento"}), frozenset({"1d"})),
}


def calculation_accepts_input(
    calculation: str,
    *,
    provider: str,
    timeframe: str,
) -> bool:
    policy = _INPUT_POLICIES.get(calculation)
    if policy is None:
        return True
    providers, timeframes = policy
    return (
        provider.strip().lower() in providers
        and timeframe.strip().lower() in timeframes
    )


def calculation_output_timeframe(
    calculation: str,
    *,
    input_timeframe: str,
) -> str:
    if calculation == WEEKLY_CONTEXT_NAME:
        return WEEKLY_CONTEXT_OUTPUT_TIMEFRAME
    return input_timeframe.strip().lower()


__all__ = [
    "CALCULATIONS",
    "DEFAULT_CALCULATIONS",
    "MARKET_REGIME_NAME",
    "MARKET_REGIME_VERSION",
    "BREAKOUT_PRESSURE_NAME",
    "BREAKOUT_PRESSURE_VERSION",
    "BAR_SHAPE_NAME",
    "BAR_SHAPE_VERSION",
    "WEEKLY_CONTEXT_NAME",
    "WEEKLY_CONTEXT_VERSION",
    "WeeklyContextNotReady",
    "calculation_accepts_input",
    "calculation_output_timeframe",
    "calculate_market_regime",
    "calculate_breakout_pressure",
    "calculate_bar_shape",
    "calculate_weekly_context",
]
