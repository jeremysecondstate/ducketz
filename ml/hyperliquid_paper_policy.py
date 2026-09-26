"""Deterministic position targets for the separate Hyperliquid paper ledger.

Probability supplies conviction, not an expected return or a Kelly fraction.
Dollar-volatility targets are bounded by symbol and pool gross exposure. The
paper runtime separately enforces each account's cash, collateral and reserve
limits when planning transfers and simulated fills.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import json
import math
from pathlib import Path
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAPER_CONFIG_PATH = PROJECT_ROOT / "configs" / "hyperliquid-paper.json"
DEFAULT_MODEL_CONFIG_PATH = PROJECT_ROOT / "configs" / "hyperliquid-models.json"
ACCOUNTS = ("alex", "jeremy", "clearpond")
CONFIG_VERSION = 1


def _finite(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    return float(value)


@dataclass(frozen=True)
class PaperConfig:
    mode: str = "paper"
    seed_mode: str = "mirror"
    data_root: Path = Path("C:/DATASTORE/hyperliquid")
    model_config: Path = DEFAULT_MODEL_CONFIG_PATH
    # These amounts apply only to an explicitly selected manual seed. Mirror
    # bootstrap must fail visibly if unavailable; it cannot use these as fallback.
    initial_cash: dict[str, float] = field(default_factory=lambda: {
        "alex": 10000.0, "jeremy": 10000.0, "clearpond": 10000.0,
    })
    require_qualified_forecasts: bool = False
    poll_seconds: float = 30.0
    entry_band: float = 0.05
    exit_band: float = 0.02
    saturation_band: float = 0.15
    volatility_budget_fraction: float = 0.001
    sigma_floor: float = 0.005
    per_symbol_gross_fraction: float = 0.15
    pool_gross_fraction: float = 0.6
    bullish_spot_fraction: float = 0.6
    account_utilization: float = 0.8
    min_trade_notional: float = 25.0
    rebalance_min_delta_fraction: float = 0.10
    perp_fee_rate: float = 0.00045
    spot_fee_rate: float = 0.0007
    # Fills use the executable book VWAP; extra modeled slippage is opt-in.
    slippage_bps: float = 0.0
    stop_loss_fraction: float = 0.03
    max_forecast_age_seconds: float = 900.0
    max_model_age_seconds: float = 86400.0
    max_quote_age_seconds: float = 45.0
    transfer_min_amount: float = 100.0
    reserve_cash_fraction: float = 0.10

    def __post_init__(self):
        if self.mode != "paper":
            raise ValueError("This component supports mode='paper' only; live execution is unavailable.")
        if not isinstance(self.seed_mode, str) or self.seed_mode not in {"mirror", "manual"}:
            raise ValueError("seed_mode must be 'mirror' or explicitly selected 'manual'.")
        if type(self.require_qualified_forecasts) is not bool:
            raise ValueError("require_qualified_forecasts must be a boolean.")
        for name in ("data_root", "model_config"):
            value = getattr(self, name)
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ValueError(f"{name} must be a nonempty filesystem path.")
            object.__setattr__(self, name, Path(value).resolve())
        if not isinstance(self.initial_cash, dict) or set(self.initial_cash) != set(ACCOUNTS):
            raise ValueError("initial_cash must state alex, jeremy and clearpond exactly.")
        capital = {name: _finite(self.initial_cash[name], f"initial_cash.{name}") for name in ACCOUNTS}
        if any(value < 0 for value in capital.values()) or sum(capital.values()) <= 0:
            raise ValueError("Manual initial_cash amounts must be nonnegative with a positive total.")
        object.__setattr__(self, "initial_cash", capital)
        for name in (
            "poll_seconds", "volatility_budget_fraction", "sigma_floor",
            "min_trade_notional", "max_forecast_age_seconds",
            "max_model_age_seconds", "max_quote_age_seconds", "transfer_min_amount",
        ):
            if _finite(getattr(self, name), name) <= 0:
                raise ValueError(f"{name} must be positive.")
        if self.volatility_budget_fraction > 1:
            raise ValueError("volatility_budget_fraction cannot exceed one.")
        for name in (
            "per_symbol_gross_fraction", "pool_gross_fraction", "bullish_spot_fraction",
            "rebalance_min_delta_fraction", "reserve_cash_fraction",
        ):
            if not 0 <= _finite(getattr(self, name), name) <= 1:
                raise ValueError(f"{name} must be in [0, 1].")
        if not 0 < _finite(self.account_utilization, "account_utilization") <= 1:
            raise ValueError("account_utilization must be in (0, 1]; paper leverage cannot exceed one.")
        for name in ("perp_fee_rate", "spot_fee_rate"):
            if not 0 <= _finite(getattr(self, name), name) < 1:
                raise ValueError(f"{name} must be in [0, 1).")
        if not 0 <= _finite(self.slippage_bps, "slippage_bps") < 10000:
            raise ValueError("slippage_bps must be in [0, 10000).")
        if not 0 < _finite(self.stop_loss_fraction, "stop_loss_fraction") < 1:
            raise ValueError("stop_loss_fraction must be in (0, 1).")
        for name in ("entry_band", "exit_band", "saturation_band"):
            _finite(getattr(self, name), name)
        if not 0 <= self.exit_band <= self.entry_band < self.saturation_band <= 0.5:
            raise ValueError("Require 0 <= exit_band <= entry_band < saturation_band <= 0.5.")

    @property
    def paper_root(self) -> Path:
        return self.data_root / "_paper"


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"Duplicate paper configuration key: {name}")
        result[name] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Non-finite JSON numbers are unsupported: {value}")


def load_config(path: str | Path = DEFAULT_PAPER_CONFIG_PATH) -> PaperConfig:
    """Read strict JSON without opening credentials, accounts or model artifacts."""
    config_path = Path(path).resolve()
    values = json.loads(config_path.read_text(encoding="utf-8-sig"),
                        object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(values, dict):
        raise ValueError("Paper configuration must be a JSON object.")
    known_keys = {item.name for item in fields(PaperConfig)} | {"version"}
    if extra := values.keys() - known_keys:
        raise ValueError(f"Unknown paper configuration keys: {', '.join(sorted(extra))}")
    version = values.pop("version", None)
    if type(version) is not int or version != CONFIG_VERSION:
        raise ValueError(f"Paper configuration requires version: {CONFIG_VERSION}.")
    for name in ("data_root", "model_config"):
        if name not in values:
            continue
        value = values[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty filesystem path string.")
        resolved = Path(value)
        values[name] = resolved if resolved.is_absolute() else config_path.parent / resolved
    return PaperConfig(**values)


def _current_positions(current_notionals: Mapping[str, float]) -> dict[str, float]:
    if not isinstance(current_notionals, Mapping) or set(current_notionals) - set(ACCOUNTS):
        raise ValueError("Current notionals may contain only alex, jeremy and clearpond.")
    values = {name: _finite(current_notionals.get(name, 0.0), f"current_notionals.{name}") for name in ACCOUNTS}
    if values["alex"] > 0 or values["jeremy"] < 0 or values["clearpond"] < 0:
        raise ValueError("Alex must be short/nonpositive; Jeremy and Clear Pond long/nonnegative.")
    return values


def target_notionals(
    probability: float, sigma: float, pooled_equity: float,
    current_notionals: Mapping[str, float], other_gross: float, config: PaperConfig,
) -> dict:
    """Return one directional allocation for a coin, plus its sizing explanation.

    ``sigma`` is already scaled to the forecast horizon (the initial recipe is
    volatility_log_return_20 * sqrt(4)). ``other_gross`` sums absolute notionals
    across every other coin/account; opposing positions never cancel there.
    Existing matching exposure determines the hysteresis threshold, not net
    exposure. Opposing inherited legs are explicitly targeted for reduction.
    These are desired targets; the runtime applies actual cash/margin limits.
    """
    if not isinstance(config, PaperConfig):
        raise ValueError("config must be a validated PaperConfig.")
    p = _finite(probability, "probability")
    volatility = _finite(sigma, "sigma")
    equity = _finite(pooled_equity, "pooled_equity")
    other = _finite(other_gross, "other_gross")
    if not 0 <= p <= 1:
        raise ValueError("probability must be in [0, 1].")
    if volatility < 0 or other < 0:
        raise ValueError("sigma and other_gross must be nonnegative.")
    current = _current_positions(current_notionals)
    positive_gross = current["jeremy"] + current["clearpond"]
    negative_gross = -current["alex"]
    signed_edge = p - 0.5
    direction = 1 if signed_edge > 0 else -1 if signed_edge < 0 else 0
    edge = abs(signed_edge)
    matching_gross = positive_gross if direction > 0 else negative_gross if direction < 0 else 0.0
    opposing_gross = negative_gross if direction > 0 else positive_gross if direction < 0 else positive_gross + negative_gross
    active = False
    reason = "neutral_band"
    if equity <= 0:
        reason = "nonpositive_pool_equity"
    elif direction and config.exit_band == config.entry_band:
        # A shared boundary has no hysteresis: flat and held positions use
        # the same inclusive threshold, avoiding an entry/exit alternation.
        active = edge + 1e-12 >= config.entry_band
        reason = "entry_threshold_met" if active else "exit_band" if matching_gross else "entry_deadband"
    elif direction and matching_gross > 0:
        active = edge > config.exit_band + 1e-12
        reason = "hold_with_hysteresis" if active else "exit_band"
    elif direction:
        active = edge + 1e-12 >= config.entry_band
        reason = "entry_threshold_met" if active else "entry_deadband"
    # In shared-threshold mode, size conviction from neutral. Subtracting
    # that same threshold would assign zero size to an eligible boundary.
    # Preserve the existing sizing curve for every unequal-band policy.
    confidence_floor = 0.0 if config.exit_band == config.entry_band else config.exit_band
    confidence = (
        min(1.0, max(0.0, (edge - confidence_floor) / (config.saturation_band - confidence_floor)))
        if active else 0.0
    )
    effective_sigma = max(volatility, config.sigma_floor)
    positive_equity = max(0.0, equity)
    volatility_budget = positive_equity * config.volatility_budget_fraction * confidence
    uncapped = volatility_budget / effective_sigma
    symbol_cap = positive_equity * config.per_symbol_gross_fraction
    pool_cap = positive_equity * config.pool_gross_fraction
    pool_room = max(0.0, pool_cap - other)
    gross = min(uncapped, symbol_cap, pool_room)
    caps = []
    if uncapped > symbol_cap:
        caps.append("per_symbol_gross")
    if uncapped > pool_room:
        caps.append("pool_gross")
    if active and gross == 0:
        reason = "gross_capacity_exhausted"
    targets = {name: 0.0 for name in ACCOUNTS}
    if direction > 0 and gross > 0:
        targets["clearpond"] = gross * config.bullish_spot_fraction
        targets["jeremy"] = gross - targets["clearpond"]
    elif direction < 0 and gross > 0:
        targets["alex"] = -gross
    return {
        "targets": targets,
        "details": {
            "reason": reason,
            "probability_not_down": p,
            "direction": "long" if direction > 0 and gross > 0 else "short" if direction < 0 and gross > 0 else "flat",
            "absolute_probability_edge": edge,
            "confidence": confidence,
            "confidence_floor_band": confidence_floor,
            "existing_matching_gross": matching_gross,
            "existing_opposing_gross_to_reduce": opposing_gross,
            "current_coin_gross": positive_gross + negative_gross,
            "current_coin_net": sum(current.values()),
            "input_horizon_sigma": volatility,
            "effective_horizon_sigma": effective_sigma,
            "dollar_volatility_budget": volatility_budget,
            "uncapped_target_gross": uncapped,
            "symbol_gross_cap": symbol_cap,
            "pool_gross_cap": pool_cap,
            "other_gross": other,
            "pool_remaining_gross_capacity": pool_room,
            "target_gross": gross,
            "binding_caps": caps,
            "account_cash_constraints_applied": False,
            "sizing_meaning": "Probability conviction scaled by trailing volatility; not expected return or maximum-loss protection.",
        },
    }


def should_rebalance(
    current_notional: float, target_notional: float, config: PaperConfig, *, force_reduce: bool = False,
) -> bool:
    """Ignore small target adjustments; complete exits and forced reductions pass."""
    if not isinstance(config, PaperConfig):
        raise ValueError("config must be a validated PaperConfig.")
    current = _finite(current_notional, "current_notional")
    target = _finite(target_notional, "target_notional")
    if type(force_reduce) is not bool:
        raise ValueError("force_reduce must be a boolean.")
    if current == target:
        return False
    if target == 0 and current != 0:
        return True
    if force_reduce and current * target >= 0 and abs(target) < abs(current):
        return True
    threshold = max(config.min_trade_notional, config.rebalance_min_delta_fraction * abs(target))
    return abs(target - current) + 1e-12 >= threshold
