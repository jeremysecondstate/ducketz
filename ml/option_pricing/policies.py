from __future__ import annotations

from dataclasses import dataclass


OPTION_PRICING_POLICY_VERSION = "black-scholes-rbf-residual-v1"
OPTION_PRICING_TIMING_POLICY_VERSION = "pre-quote-quarter-hour-v1"
OPTION_PRICING_RATE_POLICY_VERSION = "lagged-provider-then-point-in-time-v1"
OPTION_PRICING_DIVIDEND_POLICY_VERSION = "lagged-provider-then-parity-v1"
OPTION_PRICING_VOLATILITY_POLICY_VERSION = "strict-earlier-surface-interpolation-v1"
OPTION_PRICING_EXPIRATION_POLICY_VERSION = "us-equity-option-ny-1600-act365-v1"
OPTION_PRICING_CONTRACT_POLICY_VERSION = "standard-100-share-7-120d-logm25-v1"
OPTION_PRICING_WEIGHTING_POLICY_VERSION = "equal-target-snapshot-weight-v1"
OPTION_PRICING_UNCERTAINTY_POLICY_VERSION = "cluster-weighted-80-95-v1"
OPTION_PRICING_PROJECTION_POLICY_VERSION = "weighted-shape-projection-v1"
OPTION_PRICING_SCHEMA_VERSION = "option-pricing-shadow-v1"
OPTION_PRICING_FEATURE_CONTRACT_VERSION = "bsgp-six-semantic-inputs-v1"

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
class BSGPModelPolicy:
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
