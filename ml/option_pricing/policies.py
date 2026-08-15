from __future__ import annotations

from dataclasses import dataclass

from ml.universe import OPTION_CALL_PUTS, PRODUCTION_OPTION_SYMBOLS


OPTION_PRICING_POLICY_VERSION = "black-scholes-nystroem-rbf-bayesian-ridge-residual-v3"
OPTION_PRICING_TIMING_POLICY_VERSION = "strict-prediction-availability-outcome-clock-v2"
OPTION_PRICING_RATE_POLICY_VERSION = "causal-alfred-fred-live-authority-v3"
OPTION_PRICING_DIVIDEND_POLICY_VERSION = "causal-fmp-declarations-live-offline-fallback-v3"
OPTION_PRICING_VOLATILITY_POLICY_VERSION = "strict-earlier-surface-interpolation-v1"
OPTION_PRICING_EXPIRATION_POLICY_VERSION = "us-equity-option-ny-1600-act365-v1"
OPTION_PRICING_CONTRACT_POLICY_VERSION = "standard-100-share-7-120d-logm25-v1"
OPTION_PRICING_WEIGHTING_POLICY_VERSION = "liquidity-within-equal-target-surface-v2"
OPTION_PRICING_UNCERTAINTY_POLICY_VERSION = "cluster-weighted-80-95-bs-fallback-v2"
OPTION_PRICING_PROJECTION_POLICY_VERSION = "weighted-shape-projection-v1"
OPTION_PRICING_SCHEMA_VERSION = "option-pricing-v3"
OPTION_PRICING_FEATURE_CONTRACT_VERSION = "finite-basis-residual-six-semantic-inputs-v2"

FINITE_BASIS_RESIDUAL_MODEL_NAME = (
    "128-component Nystroem RBF residual model with Bayesian ridge posterior"
)

LOOP_NATIVE_SYMBOLS = PRODUCTION_OPTION_SYMBOLS
LOOP_NATIVE_CALL_PUTS = OPTION_CALL_PUTS
LEGACY_LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION = (
    "loop-native-schwab-causal-residual-materialization-v2"
)
LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION = (
    "loop-native-opra-primary-schwab-fallback-materialization-v3"
)
LOOP_NATIVE_MODEL_POLICY_VERSION = (
    "loop-native-pooled-nystroem-rbf-bayesian-ridge-active-v4"
)
LOOP_NATIVE_SHADOW_SCHEMA_VERSION = "loop-native-finite-basis-active-v3"
LOOP_NATIVE_SURFACE_WEIGHTING_POLICY_VERSION = (
    "equal-symbol-target-call-put-surface-weight-v1"
)
LOOP_NATIVE_CARRY_POLICY_VERSION = "source-chain-american-parity-quality-gated-v1"

SEMANTIC_FEATURE_COLUMNS = (
    "underlying_price",
    "strike",
    "risk_free_rate",
    "lagged_implied_volatility",
    "target_years_to_expiration",
    "dividend_yield",
)

DERIVED_FEATURE_COLUMNS = (
    "log_underlying_price",
    "log_moneyness",
    "risk_free_rate",
    "log_lagged_implied_volatility",
    "sqrt_target_years_to_expiration",
    "dividend_yield",
)


@dataclass(frozen=True)
class ContractSelectionPolicy:
    minimum_days_to_expiration: int = 7
    maximum_days_to_expiration: int = 120
    maximum_absolute_log_moneyness: float = 0.25
    required_multiplier: float = 100.0
    maximum_source_staleness_seconds: int = 20 * 60

    def __post_init__(self) -> None:
        if self.minimum_days_to_expiration < 1:
            raise ValueError("minimum_days_to_expiration must be positive")
        if self.maximum_days_to_expiration < self.minimum_days_to_expiration:
            raise ValueError("maximum_days_to_expiration precedes the minimum")
        if self.maximum_absolute_log_moneyness <= 0.0:
            raise ValueError("maximum_absolute_log_moneyness must be positive")
        if self.required_multiplier <= 0.0:
            raise ValueError("required_multiplier must be positive")
        if self.maximum_source_staleness_seconds < 0:
            raise ValueError("maximum_source_staleness_seconds cannot be negative")


@dataclass(frozen=True)
class PricingPartitionConfig:
    minimum_train_clusters: int = 252
    calibration_clusters: int = 63
    assessment_clusters: int = 63
    lockbox_clusters: int = 126
    minimum_calendar_months: int = 6

    def __post_init__(self) -> None:
        for name in (
            "minimum_train_clusters",
            "calibration_clusters",
            "assessment_clusters",
            "lockbox_clusters",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")
        if self.minimum_calendar_months < 0:
            raise ValueError("minimum_calendar_months cannot be negative")


@dataclass(frozen=True)
class FiniteBasisResidualModelPolicy:
    component_count: int = 128
    gamma_grid: tuple[float, ...] = (0.1, 0.3, 1.0, 3.0)
    random_state: int = 1729
    minimum_predictive_standard_deviation: float = 1e-6

    def __post_init__(self) -> None:
        if self.component_count < 1:
            raise ValueError("component_count must be positive")
        if not self.gamma_grid or any(value <= 0.0 for value in self.gamma_grid):
            raise ValueError("gamma_grid must contain positive values")
        if len(set(self.gamma_grid)) != len(self.gamma_grid):
            raise ValueError("gamma_grid values must be unique")
        if self.minimum_predictive_standard_deviation <= 0.0:
            raise ValueError(
                "minimum_predictive_standard_deviation must be positive"
            )


# Compatibility alias for immutable v1/v2 manifests and external imports.
BSGPModelPolicy = FiniteBasisResidualModelPolicy


@dataclass(frozen=True)
class LoopNativeModelPolicy:
    """Bounded capture model policy; eligibility thresholds remain separate."""

    component_count: int = 128
    gamma_grid: tuple[float, ...] = (0.1, 0.3, 1.0, 3.0)
    random_state: int = 271_828
    maximum_training_rows: int = 250_000
    minimum_fit_sessions: int = 3
    minimum_calibration_sessions: int = 2
    minimum_assessment_sessions: int = 1
    minimum_calibrated_sessions: int = 15
    minimum_route_support_sessions: int = 3
    maximum_age_hours: int = 72
    maximum_predictive_standard_deviation_normalized: float = 0.20
    black_scholes_fallback_standard_deviation_normalized: float = 0.10
    support_margin_fraction: float = 0.05
    minimum_predictive_standard_deviation: float = 1e-6
    offline_emulation_delay_seconds: int = 60

    def __post_init__(self) -> None:
        if self.component_count < 1:
            raise ValueError("component_count must be positive")
        if not self.gamma_grid or any(value <= 0.0 for value in self.gamma_grid):
            raise ValueError("gamma_grid must contain positive values")
        if len(set(self.gamma_grid)) != len(self.gamma_grid):
            raise ValueError("gamma_grid values must be unique")
        for name in (
            "maximum_training_rows",
            "minimum_fit_sessions",
            "minimum_calibration_sessions",
            "minimum_assessment_sessions",
            "minimum_calibrated_sessions",
            "minimum_route_support_sessions",
            "maximum_age_hours",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")
        if self.maximum_predictive_standard_deviation_normalized <= 0.0:
            raise ValueError(
                "maximum_predictive_standard_deviation_normalized must be positive"
            )
        if not (
            0.0
            < self.black_scholes_fallback_standard_deviation_normalized
            <= self.maximum_predictive_standard_deviation_normalized
        ):
            raise ValueError(
                "black_scholes_fallback_standard_deviation_normalized must be "
                "positive and no greater than the maximum predictive uncertainty"
            )
        if self.support_margin_fraction < 0.0:
            raise ValueError("support_margin_fraction cannot be negative")
        if self.minimum_predictive_standard_deviation <= 0.0:
            raise ValueError("minimum_predictive_standard_deviation must be positive")
        if self.offline_emulation_delay_seconds < 0:
            raise ValueError("offline_emulation_delay_seconds cannot be negative")


@dataclass(frozen=True)
class ProjectionPolicy:
    tolerance: float = 1e-8
    maximum_iterations: int = 2_000
    maximum_material_correction: float = 0.25

    def __post_init__(self) -> None:
        if self.tolerance <= 0.0:
            raise ValueError("Projection tolerance must be positive")
        if self.maximum_iterations < 1:
            raise ValueError("Projection maximum_iterations must be positive")
        if self.maximum_material_correction < 0.0:
            raise ValueError("maximum_material_correction cannot be negative")


__all__ = [
    "BSGPModelPolicy",
    "ContractSelectionPolicy",
    "DERIVED_FEATURE_COLUMNS",
    "FINITE_BASIS_RESIDUAL_MODEL_NAME",
    "FiniteBasisResidualModelPolicy",
    "LOOP_NATIVE_CALL_PUTS",
    "LOOP_NATIVE_CARRY_POLICY_VERSION",
    "LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION",
    "LEGACY_LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION",
    "LOOP_NATIVE_MODEL_POLICY_VERSION",
    "LOOP_NATIVE_SHADOW_SCHEMA_VERSION",
    "LOOP_NATIVE_SURFACE_WEIGHTING_POLICY_VERSION",
    "LOOP_NATIVE_SYMBOLS",
    "LoopNativeModelPolicy",
    "OPTION_PRICING_CONTRACT_POLICY_VERSION",
    "OPTION_PRICING_DIVIDEND_POLICY_VERSION",
    "OPTION_PRICING_EXPIRATION_POLICY_VERSION",
    "OPTION_PRICING_FEATURE_CONTRACT_VERSION",
    "OPTION_PRICING_POLICY_VERSION",
    "OPTION_PRICING_PROJECTION_POLICY_VERSION",
    "OPTION_PRICING_RATE_POLICY_VERSION",
    "OPTION_PRICING_SCHEMA_VERSION",
    "OPTION_PRICING_TIMING_POLICY_VERSION",
    "OPTION_PRICING_UNCERTAINTY_POLICY_VERSION",
    "OPTION_PRICING_VOLATILITY_POLICY_VERSION",
    "OPTION_PRICING_WEIGHTING_POLICY_VERSION",
    "PricingPartitionConfig",
    "ProjectionPolicy",
    "SEMANTIC_FEATURE_COLUMNS",
]
