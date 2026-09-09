from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from ml.sequence_encoder.contracts import (
    SEQUENCE_DISTRIBUTION_SCHEMA_VERSION,
    SEQUENCE_ENCODER_POLICY_VERSION,
    SUPPORTED_HORIZONS,
    SequenceEncoderConfig,
)


LOOP_C_POLICY_VERSION = "deterministic-risk-authority-observe-v2"
LOOP_C_MODEL_BINDING_SCHEMA_VERSION = "loop-c-sequence-model-binding-v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class LoopCMode(StrEnum):
    OBSERVE = "OBSERVE"
    NORMAL = "NORMAL"
    REDUCE_ONLY = "REDUCE_ONLY"
    FLATTEN = "FLATTEN"
    HALT = "HALT"


@dataclass(frozen=True)
class LoopCPredictiveThresholds:
    """Horizon-specific predictive gates; none of them grants authority."""

    minimum_strategy_calibrated_probability: float
    minimum_sequence_directional_probability: float
    minimum_expected_return_on_risk: float
    maximum_total_uncertainty: float
    uncertainty_penalty: float

    def __post_init__(self) -> None:
        for name in (
            "minimum_strategy_calibrated_probability",
            "minimum_sequence_directional_probability",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or not 0.0 < value < 1.0:
                raise ValueError(f"{name} must be finite and in (0, 1)")
        for name in (
            "minimum_expected_return_on_risk",
            "maximum_total_uncertainty",
            "uncertainty_penalty",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True)
class LoopCSequenceModelBinding:
    """Exact sequence contract that an approved Loop C run may consume."""

    schema_version: str
    model_name: str
    sequence_policy_version: str
    configuration_fingerprint: str
    distribution_schema_version: str
    required_authority: str
    consumer: str
    horizons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != LOOP_C_MODEL_BINDING_SCHEMA_VERSION:
            raise ValueError("Unsupported Loop C model-binding schema")
        if self.model_name != "pooled-causal-sequence-encoder":
            raise ValueError("Loop C is bound to the pooled causal sequence encoder")
        if self.sequence_policy_version != SEQUENCE_ENCODER_POLICY_VERSION:
            raise ValueError("Loop C sequence policy binding changed")
        if not _SHA256_PATTERN.fullmatch(self.configuration_fingerprint):
            raise ValueError("Loop C configuration fingerprint must be SHA-256")
        if self.distribution_schema_version != SEQUENCE_DISTRIBUTION_SCHEMA_VERSION:
            raise ValueError("Loop C distribution schema binding changed")
        if self.required_authority != "SHADOW_ONLY":
            raise ValueError("Loop C v1 model authority must remain SHADOW_ONLY")
        if self.consumer != "LOOP_C_OBSERVE":
            raise ValueError("Loop C model consumer binding changed")
        if tuple(self.horizons) != SUPPORTED_HORIZONS:
            raise ValueError("Loop C must bind the exact supported horizon order")


def expected_sequence_model_binding() -> LoopCSequenceModelBinding:
    config = SequenceEncoderConfig()
    return LoopCSequenceModelBinding(
        schema_version=LOOP_C_MODEL_BINDING_SCHEMA_VERSION,
        model_name="pooled-causal-sequence-encoder",
        sequence_policy_version=SEQUENCE_ENCODER_POLICY_VERSION,
        configuration_fingerprint=config.semantic_fingerprint,
        distribution_schema_version=SEQUENCE_DISTRIBUTION_SCHEMA_VERSION,
        required_authority="SHADOW_ONLY",
        consumer="LOOP_C_OBSERVE",
        horizons=SUPPORTED_HORIZONS,
    )


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
    predictive_thresholds_by_horizon: Mapping[
        str, LoopCPredictiveThresholds | Mapping[str, object]
    ]
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
            "maximum_candidate_quantity",
        )
        for name in positive:
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        raw = self.predictive_thresholds_by_horizon
        if not isinstance(raw, Mapping) or set(raw) != set(SUPPORTED_HORIZONS):
            raise ValueError(
                "predictive_thresholds_by_horizon must contain exactly "
                + ", ".join(SUPPORTED_HORIZONS)
            )
        normalized: dict[str, LoopCPredictiveThresholds] = {}
        for horizon in SUPPORTED_HORIZONS:
            value = raw[horizon]
            normalized[horizon] = (
                value
                if isinstance(value, LoopCPredictiveThresholds)
                else LoopCPredictiveThresholds(**dict(value))
                if isinstance(value, Mapping)
                else _invalid_thresholds(horizon)
            )
        object.__setattr__(self, "predictive_thresholds_by_horizon", normalized)
        if self.policy_version != LOOP_C_POLICY_VERSION:
            raise ValueError("Unsupported Loop C risk policy")

    def thresholds_for(self, horizon: object) -> LoopCPredictiveThresholds:
        clean = str(horizon).strip()
        try:
            return self.predictive_thresholds_by_horizon[clean]  # type: ignore[return-value]
        except KeyError as exc:
            raise ValueError(f"Unsupported Loop C horizon: {clean}") from exc


def _invalid_thresholds(horizon: str) -> LoopCPredictiveThresholds:
    raise ValueError(f"Predictive thresholds for {horizon} must be an object")


__all__ = [
    "LOOP_C_MODEL_BINDING_SCHEMA_VERSION",
    "LOOP_C_POLICY_VERSION",
    "LoopCMode",
    "LoopCPredictiveThresholds",
    "LoopCRiskLimits",
    "LoopCSequenceModelBinding",
    "expected_sequence_model_binding",
]
