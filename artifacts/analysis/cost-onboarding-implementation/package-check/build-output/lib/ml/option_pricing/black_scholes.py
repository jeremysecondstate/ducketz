from __future__ import annotations

import math
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from ml.option_pricing.policies import OPTION_PRICING_EXPIRATION_POLICY_VERSION


_SQRT_TWO = math.sqrt(2.0)
_NEAR_ZERO = 1e-12
_NEW_YORK = ZoneInfo("America/New_York")


def black_scholes_price(
    underlying_price: float,
    strike: float,
    risk_free_rate: float,
    volatility: float,
    years_to_expiration: float,
    dividend_yield: float,
    call_put: str,
) -> float:
    """Return a dividend-adjusted European Black-Scholes value.

    Expiry and a deterministic, near-zero-volatility terminal distribution are
    handled as explicit limits. Invalid or non-finite inputs fail closed.
    """

    option_type = _call_put(call_put)
    values = (
        underlying_price,
        strike,
        risk_free_rate,
        volatility,
        years_to_expiration,
        dividend_yield,
    )
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("Black-Scholes inputs must be finite")
    s = float(underlying_price)
    k = float(strike)
    r = float(risk_free_rate)
    sigma = float(volatility)
    years = float(years_to_expiration)
    q = float(dividend_yield)
    if s <= 0.0 or k <= 0.0:
        raise ValueError("Black-Scholes spot and strike must be positive")
    if sigma < 0.0 or years < 0.0:
        raise ValueError("Black-Scholes volatility and time cannot be negative")
    if years <= _NEAR_ZERO:
        return _intrinsic(s, k, option_type)

    discounted_spot = s * math.exp(-q * years)
    discounted_strike = k * math.exp(-r * years)
    sigma_sqrt_t = sigma * math.sqrt(years)
    if sigma_sqrt_t <= _NEAR_ZERO:
        deterministic = discounted_spot - discounted_strike
        return max(deterministic, 0.0) if option_type == "CALL" else max(-deterministic, 0.0)

    d1 = (
        math.log(s / k) + (r - q + 0.5 * sigma * sigma) * years
    ) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    if option_type == "CALL":
        value = discounted_spot * _normal_cdf(d1) - discounted_strike * _normal_cdf(d2)
    else:
        value = discounted_strike * _normal_cdf(-d2) - discounted_spot * _normal_cdf(-d1)
    if not math.isfinite(value):
        raise ValueError("Black-Scholes calculation was non-finite")
    return max(float(value), 0.0)


def implied_volatility(
    observed_price: float,
    underlying_price: float,
    strike: float,
    risk_free_rate: float,
    years_to_expiration: float,
    dividend_yield: float,
    call_put: str,
    *,
    minimum_volatility: float = 1e-8,
    maximum_volatility: float = 5.0,
    tolerance: float = 1e-10,
    maximum_iterations: int = 200,
) -> float:
    """Solve a European implied volatility with bounded bisection."""

    if not math.isfinite(float(observed_price)) or observed_price <= 0.0:
        raise ValueError("Observed option price must be finite and positive")
    if minimum_volatility < 0.0 or maximum_volatility <= minimum_volatility:
        raise ValueError("Invalid implied-volatility bracket")
    if tolerance <= 0.0 or maximum_iterations < 1:
        raise ValueError("Invalid implied-volatility solver policy")
    low_price = black_scholes_price(
        underlying_price,
        strike,
        risk_free_rate,
        minimum_volatility,
        years_to_expiration,
        dividend_yield,
        call_put,
    )
    high_price = black_scholes_price(
        underlying_price,
        strike,
        risk_free_rate,
        maximum_volatility,
        years_to_expiration,
        dividend_yield,
        call_put,
    )
    target = float(observed_price)
    if target < low_price - tolerance or target > high_price + tolerance:
        raise ValueError("Observed price is outside the bounded volatility support")
    if abs(target - low_price) <= tolerance:
        return float(minimum_volatility)
    if abs(target - high_price) <= tolerance:
        return float(maximum_volatility)

    low = float(minimum_volatility)
    high = float(maximum_volatility)
    for _ in range(maximum_iterations):
        middle = (low + high) / 2.0
        price = black_scholes_price(
            underlying_price,
            strike,
            risk_free_rate,
            middle,
            years_to_expiration,
            dividend_yield,
            call_put,
        )
        if abs(price - target) <= tolerance:
            return middle
        if price < target:
            low = middle
        else:
            high = middle
    result = (low + high) / 2.0
    final_price = black_scholes_price(
        underlying_price,
        strike,
        risk_free_rate,
        result,
        years_to_expiration,
        dividend_yield,
        call_put,
    )
    if abs(final_price - target) > max(tolerance * 10.0, 1e-8):
        raise ValueError("Implied-volatility solver did not converge")
    return result


def american_option_bounds(
    underlying_price: float,
    strike: float,
    risk_free_rate: float,
    volatility: float,
    years_to_expiration: float,
    dividend_yield: float,
    call_put: str,
) -> tuple[float, float]:
    """Return the configured pointwise American equity-option bounds."""

    option_type = _call_put(call_put)
    european = black_scholes_price(
        underlying_price,
        strike,
        risk_free_rate,
        volatility,
        years_to_expiration,
        dividend_yield,
        option_type,
    )
    intrinsic = _intrinsic(float(underlying_price), float(strike), option_type)
    lower = max(intrinsic, european)
    upper = float(underlying_price) if option_type == "CALL" else float(strike)
    if lower > upper + 1e-9:
        raise ValueError("Configured American option bounds are inconsistent")
    return min(lower, upper), upper


def expiration_instant(expiration: object) -> pd.Timestamp:
    """Map a date-precision U.S. equity-option expiration to 16:00 New York."""

    if isinstance(expiration, date) and not isinstance(expiration, datetime):
        expiration_date = expiration
    else:
        parsed = pd.to_datetime(expiration, errors="coerce")
        if pd.isna(parsed):
            raise ValueError("Option expiration date is invalid")
        expiration_date = pd.Timestamp(parsed).date()
    local = datetime.combine(expiration_date, time(16, 0), tzinfo=_NEW_YORK)
    return pd.Timestamp(local).tz_convert("UTC")


def target_years_to_expiration(
    target_snapshot_for: object,
    expiration: object,
) -> float:
    """ACT/365 calendar time under the versioned expiration policy."""

    target = pd.to_datetime(target_snapshot_for, utc=True, errors="coerce")
    if pd.isna(target):
        raise ValueError("Target snapshot timestamp is invalid")
    seconds = (expiration_instant(expiration) - pd.Timestamp(target)).total_seconds()
    return max(float(seconds), 0.0) / (365.0 * 24.0 * 60.0 * 60.0)


def expiration_policy_version() -> str:
    return OPTION_PRICING_EXPIRATION_POLICY_VERSION


def _normal_cdf(value: float) -> float:
    return 0.5 * math.erfc(-float(value) / _SQRT_TWO)


def _intrinsic(underlying: float, strike: float, call_put: str) -> float:
    return max(
        underlying - strike if call_put == "CALL" else strike - underlying,
        0.0,
    )


def _call_put(value: object) -> str:
    normalized = str(value or "").strip().upper()
    aliases = {"C": "CALL", "CALL": "CALL", "P": "PUT", "PUT": "PUT"}
    try:
        return aliases[normalized]
    except KeyError as exc:
        raise ValueError("call_put must be CALL or PUT") from exc


__all__ = [
    "american_option_bounds",
    "black_scholes_price",
    "expiration_instant",
    "expiration_policy_version",
    "implied_volatility",
    "target_years_to_expiration",
]
