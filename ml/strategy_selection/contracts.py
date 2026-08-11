from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping

import pandas as pd


AssetKind = Literal["OPTION", "STOCK"]
PositionSide = Literal["LONG", "SHORT"]
OptionType = Literal["CALL", "PUT"]
ExpirationRole = Literal["FRONT", "BACK"]

STRATEGY_SELECTION_SCHWAB_SPREADS_V1 = "schwab-spreads-v1"

STRATEGY_REGISTRY_VERSION = "schwab-spreads-strategy-registry-v1"
STRATEGY_CANDIDATE_POLICY_VERSION = "schwab-exact-chain-pricing-candidates-v4"
STRATEGY_OUTCOME_POLICY_VERSION = "observed-bbo-pseudo-outcome-v2"
MARKET_STATE_POLICY_VERSION = "point-in-time-market-state-pricing-v2"
STRATEGY_PRIOR_POLICY_VERSION = "pricing-greek-bbo-scenario-prior-v3"
STRATEGY_MODEL_POLICY_VERSION = "pricing-market-state-hgb-platt-return-v5"
STRATEGY_RANKING_POLICY_VERSION = "post-pricing-probability-first-ranking-v4"
STRATEGY_CANDIDATE_SCHEMA_VERSION = "strategy-candidate-v3"
STRATEGY_RESEARCH_TRACE_VERSION = "nyu-hu-uh-trace-v3"

BSGP_CALIBRATED_MODEL_SCORE_BASIS = "BSGP_CALIBRATED_MODEL"
BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS = (
    "BLACK_SCHOLES_CALIBRATED_MODEL"
)
PRICING_SCENARIO_FALLBACK_SCORE_BASIS = "PRICING_SCENARIO_FALLBACK"
# Import-compatible names for downstream code while persisted rows use only the
# three pricing-aware bases above.
CALIBRATED_MODEL_SCORE_BASIS = BSGP_CALIBRATED_MODEL_SCORE_BASIS
SCENARIO_PRIOR_SCORE_BASIS = PRICING_SCENARIO_FALLBACK_SCORE_BASIS


@dataclass(frozen=True)
class LegRule:
    asset: AssetKind
    side: PositionSide
    quantity: int
    option_type: OptionType | None = None
    expiration_role: ExpirationRole | None = None
    strike_offset: int = 0

    def __post_init__(self) -> None:
        if self.quantity < 1:
            raise ValueError("Strategy leg quantity must be positive")
        if self.asset == "OPTION":
            if self.option_type not in {"CALL", "PUT"}:
                raise ValueError("Option legs require CALL or PUT")
            if self.expiration_role not in {"FRONT", "BACK"}:
                raise ValueError("Option legs require FRONT or BACK expiration")
        elif self.option_type is not None or self.expiration_role is not None:
            raise ValueError("Stock legs cannot define option fields")


@dataclass(frozen=True)
class StrategyDefinition:
    name: str
    display_name: str
    family: str
    legs: tuple[LegRule, ...]
    risk_form: str
    expiration_structure: str = "SINGLE"
    stock_requirement: str = "NONE"
    cash_requirement: str = "NORMAL_BUYING_POWER"
    lifecycle: bool = False
    research_basis: tuple[str, ...] = ("UH",)
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.display_name or not self.family:
            raise ValueError("Strategy name, display name, and family are required")
        if not self.legs:
            raise ValueError(f"Strategy {self.name} requires at least one leg")
        if self.expiration_structure not in {"SINGLE", "MULTI"}:
            raise ValueError("expiration_structure must be SINGLE or MULTI")
        if any(leg.expiration_role == "BACK" for leg in self.legs):
            if self.expiration_structure != "MULTI":
                raise ValueError(
                    f"Strategy {self.name} has a BACK leg but is not multi-expiration"
                )

    @property
    def has_short_option(self) -> bool:
        return any(
            leg.asset == "OPTION" and leg.side == "SHORT"
            for leg in self.legs
        )

    @property
    def option_leg_count(self) -> int:
        return sum(leg.asset == "OPTION" for leg in self.legs)


@dataclass(frozen=True)
class StrategySelectionPolicy:
    policy_id: str = STRATEGY_SELECTION_SCHWAB_SPREADS_V1
    account_approval: str = "SPREADS"
    minimum_train_decisions: int = 252
    calibration_decisions: int = 63
    assessment_decisions: int = 63
    candidate_width_steps: tuple[int, ...] = (1, 2)
    maximum_expiration_choices: int = 2
    maximum_relative_bid_ask_spread: float = 0.35
    minimum_open_interest: float = 1.0
    maximum_quote_staleness_seconds: float = 15.0 * 60.0
    per_contract_fee: float = 0.65
    fee_schedule: str = "schwab-online-options-standard"
    fee_schedule_verified_on: str = "2026-08-01"
    buy_to_close_fee_waiver_applied: bool = False
    variable_exchange_regulatory_fees_included: bool = False
    random_state: int = 20260801

    def __post_init__(self) -> None:
        if self.policy_id != STRATEGY_SELECTION_SCHWAB_SPREADS_V1:
            raise ValueError("StrategySelectionPolicy requires schwab-spreads-v1")
        if self.account_approval != "SPREADS":
            raise ValueError("This policy is scoped to Schwab SPREADS approval")
        for name in (
            "minimum_train_decisions",
            "calibration_decisions",
            "assessment_decisions",
            "maximum_expiration_choices",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")
        if not self.candidate_width_steps or any(
            int(value) < 1 for value in self.candidate_width_steps
        ):
            raise ValueError("candidate_width_steps must contain positive values")
        if not 0.0 < self.maximum_relative_bid_ask_spread < 1.0:
            raise ValueError("maximum_relative_bid_ask_spread must be in (0, 1)")
        if self.minimum_open_interest < 0.0:
            raise ValueError("minimum_open_interest cannot be negative")
        if self.maximum_quote_staleness_seconds < 0.0:
            raise ValueError("maximum_quote_staleness_seconds cannot be negative")
        if self.per_contract_fee < 0.0:
            raise ValueError("per_contract_fee cannot be negative")


@dataclass(frozen=True)
class StrategyPartitions:
    train: pd.DataFrame
    calibration: pd.DataFrame
    assessment: pd.DataFrame
    train_decisions: int
    calibration_decisions: int
    assessment_decisions: int
    purged_rows: int


@dataclass(frozen=True)
class StrategyModel:
    horizon: str
    estimator: object
    return_estimator: object
    calibrator: object
    numeric_features: tuple[str, ...]
    categorical_features: tuple[str, ...]
    artifact_directory: Path
    offline_evaluation: Mapping[str, object]
    reused: bool = False


@dataclass(frozen=True)
class StrategySelectionRun:
    candidates: pd.DataFrame
    audit: pd.DataFrame
    source_files: tuple[Path, ...] = ()
    model_reports: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    models_trained: int = 0
    models_reused: int = 0
    pricing_report: Mapping[str, object] = field(default_factory=dict)


__all__ = [
    "AssetKind",
    "BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS",
    "BSGP_CALIBRATED_MODEL_SCORE_BASIS",
    "CALIBRATED_MODEL_SCORE_BASIS",
    "ExpirationRole",
    "LegRule",
    "MARKET_STATE_POLICY_VERSION",
    "OptionType",
    "PositionSide",
    "PRICING_SCENARIO_FALLBACK_SCORE_BASIS",
    "STRATEGY_CANDIDATE_POLICY_VERSION",
    "STRATEGY_CANDIDATE_SCHEMA_VERSION",
    "STRATEGY_MODEL_POLICY_VERSION",
    "STRATEGY_OUTCOME_POLICY_VERSION",
    "STRATEGY_PRIOR_POLICY_VERSION",
    "STRATEGY_RANKING_POLICY_VERSION",
    "STRATEGY_REGISTRY_VERSION",
    "STRATEGY_RESEARCH_TRACE_VERSION",
    "STRATEGY_SELECTION_SCHWAB_SPREADS_V1",
    "SCENARIO_PRIOR_SCORE_BASIS",
    "StrategyDefinition",
    "StrategyModel",
    "StrategyPartitions",
    "StrategySelectionPolicy",
    "StrategySelectionRun",
]
