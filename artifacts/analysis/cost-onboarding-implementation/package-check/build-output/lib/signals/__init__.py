"""Cross-domain Duckets signal calculations."""

from signals.calculation import (
    CALCULATION_NAME,
    CALCULATION_VERSION,
    calculate_fundamental_technical_lifecycle,
)
from signals.consensus import TIMEFRAME_WEIGHTS

__all__ = [
    "CALCULATION_NAME",
    "CALCULATION_VERSION",
    "TIMEFRAME_WEIGHTS",
    "calculate_fundamental_technical_lifecycle",
]
