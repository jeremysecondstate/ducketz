"""Shadow-only Black-Scholes-integrated option-pricing contracts."""

from ml.option_pricing.black_scholes import (
    american_option_bounds,
    black_scholes_price,
    implied_volatility,
    target_years_to_expiration,
)
from ml.option_pricing.policies import (
    BSGPModelPolicy,
    ContractSelectionPolicy,
    PricingPartitionConfig,
    ProjectionPolicy,
)

__all__ = [
    "BSGPModelPolicy",
    "ContractSelectionPolicy",
    "PricingPartitionConfig",
    "ProjectionPolicy",
    "american_option_bounds",
    "black_scholes_price",
    "implied_volatility",
    "target_years_to_expiration",
]
