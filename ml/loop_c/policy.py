from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


LOOP_C_POLICY_VERSION = "deterministic-risk-authority-observe-v1"


class LoopCMode(StrEnum):
    OBSERVE = "OBSERVE"
    NORMAL = "NORMAL"
    REDUCE_ONLY = "REDUCE_ONLY"
    FLATTEN = "FLATTEN"
    HALT = "HALT"


@dataclass(frozen=True)
class LoopCRiskLimits:
    """Explicit deterministic limits; model output cannot modify these values."""

    maximum_snapshot_age_seconds: float
    maximum_model_age_seconds: float
    maximum_daily_loss: float
    maximum_gross_exposure: float
    maximum_symbol_exposure: float
    maximum_trade_loss: float
    maximum_open_positions: int
    maximum_working_orders: int
    minimum_calibrated_probability: float
    minimum_sequence_directional_probability: float
    minimum_expected_return_on_risk: float
    maximum_total_uncertainty: float
    uncertainty_penalty: float
    maximum_candidate_quantity: int = 1
    policy_version: str = LOOP_C_POLICY_VERSION

    def __post_init__(self) -> None:
        positive = (
            "maximum_snapshot_age_seconds",
            "maximum_model_age_seconds",
            "maximum_daily_loss",
            "maximum_gross_exposure",
            "maximum_symbol_exposure",
            "maximum_trade_loss",
            "maximum_open_positions",
            "maximum_working_orders",
            "uncertainty_penalty",
            "maximum_candidate_quantity",
        )
        for name in positive:
            if float(getattr(self, name)) <= 0.0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "minimum_calibrated_probability",
            "minimum_sequence_directional_probability",
        ):
            if not 0.0 < float(getattr(self, name)) < 1.0:
                raise ValueError(f"{name} must be in (0, 1)")
        if self.minimum_expected_return_on_risk <= 0.0:
            raise ValueError("minimum_expected_return_on_risk must be positive")
        if self.maximum_total_uncertainty <= 0.0:
            raise ValueError("maximum_total_uncertainty must be positive")
        if self.policy_version != LOOP_C_POLICY_VERSION:
            raise ValueError("Unsupported Loop C risk policy")


__all__ = ["LOOP_C_POLICY_VERSION", "LoopCMode", "LoopCRiskLimits"]
