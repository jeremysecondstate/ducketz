from __future__ import annotations

import json
import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Mapping

import numpy as np
import pandas as pd

from ml.strategy_selection.contracts import (
    MARKET_STATE_POLICY_VERSION,
    SCENARIO_PRIOR_SCORE_BASIS,
    STRATEGY_CANDIDATE_SCHEMA_VERSION,
    STRATEGY_MODEL_POLICY_VERSION,
    STRATEGY_PRIOR_POLICY_VERSION,
    STRATEGY_RANKING_POLICY_VERSION,
    StrategySelectionPolicy,
)


_SCENARIO_COUNT = 129
_HALF_NORMAL_MEAN = math.sqrt(2.0 / math.pi)
_NORMAL = NormalDist()
_SCENARIO_QUANTILES = (np.arange(_SCENARIO_COUNT, dtype=float) + 0.5) / float(
    _SCENARIO_COUNT
)
_HALF_NORMAL_MAGNITUDES = np.asarray(
    [_NORMAL.inv_cdf(0.5 + 0.5 * value) for value in _SCENARIO_QUANTILES],
    dtype=float,
) / _HALF_NORMAL_MEAN


@dataclass(frozen=True)
class MarketState:
    direction_probability_up: float | None
    expected_absolute_move: float
    expected_realized_volatility: float | None
    uncertainty: float
    trend_persistence: float | None
    mean_reversion_tendency: float | None
    holding_days: float

    @property
    def effective_probability_up(self) -> float:
        return (
            self.direction_probability_up
            if self.direction_probability_up is not None
            else 0.5
        )


def infer_market_state(
    sample: Mapping[str, object] | pd.Series,
    *,
    surface: Mapping[str, object] | pd.Series,
    probability_up: float | None,
) -> MarketState:
    direction = _probability(probability_up)
    holding_days = max(
        (
            _utc(sample["target_window_end"])
            - _utc(sample["decision_timestamp"])
        ).total_seconds()
        / 86_400.0,
        1.0 / 1_440.0,
    )
    expected_move = _expected_absolute_move(
        sample,
        surface=surface,
        holding_days=holding_days,
    )
    expected_realized_volatility = _first_finite(
        surface.get("realized_volatility_20d"),
        sample.get("opt__realized_volatility_20d"),
    )
    trend_persistence = _mean_available(
        _centered_strength(sample.get("mr__trend_score")),
        _unit_score(sample.get("mr__regime_strength")),
        _unit_score(sample.get("bp__breakout_strength_score")),
    )
    mean_reversion_tendency = _mean_available(
        _range_extremity(sample.get("mr__range_position")),
        _unit_score(sample.get("bp__range_contraction_score")),
        1.0 - trend_persistence if trend_persistence is not None else None,
    )
    return MarketState(
        direction_probability_up=direction,
        expected_absolute_move=expected_move,
        expected_realized_volatility=expected_realized_volatility,
        uncertainty=_binary_uncertainty(direction),
        trend_persistence=trend_persistence,
        mean_reversion_tendency=mean_reversion_tendency,
        holding_days=holding_days,
    )


def score_market_state_prior(
    candidates: pd.DataFrame,
    *,
    state: MarketState,
    policy: StrategySelectionPolicy,
) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    output = candidates.copy()
    output["market_expected_absolute_move"] = state.expected_absolute_move
    output["market_expected_realized_volatility"] = (
        state.expected_realized_volatility
    )
    output["market_uncertainty"] = state.uncertainty
    output["market_trend_persistence"] = state.trend_persistence
    output["market_mean_reversion_tendency"] = state.mean_reversion_tendency

    prior_rows = [
        _candidate_prior(row, state=state, policy=policy)
        for row in output.to_dict("records")
    ]
    prior = pd.DataFrame(prior_rows, index=output.index)
    output["strategy_prior__profit_probability"] = prior["probability"]
    output["strategy_prior__expected_net_profit"] = prior["expected_net_profit"]
    output["strategy_prior__expected_return_on_risk"] = prior[
        "expected_return_on_risk"
    ]
    probability = pd.to_numeric(prior["probability"], errors="coerce")
    expected_return = pd.to_numeric(
        prior["expected_return_on_risk"], errors="coerce"
    )
    if (
        not np.isfinite(probability.to_numpy(dtype=float)).all()
        or not probability.between(0.0, 1.0).all()
    ):
        raise ValueError("Strategy scenario-prior probabilities must be finite in [0, 1]")
    if not np.isfinite(expected_return.to_numpy(dtype=float)).all():
        raise ValueError("Strategy scenario-prior expected returns must be finite")
    output["raw_profit_probability"] = probability
    output["calibrated_profit_probability"] = np.nan
    probability_direction = state.effective_probability_up
    output["direction_probability_up"] = probability_direction
    output["direction_alignment"] = np.sign(
        pd.to_numeric(output["net_delta"], errors="coerce").fillna(0.0)
    ) * (2.0 * probability_direction - 1.0)
    output["expected_net_profit"] = prior["expected_net_profit"]
    output["expected_return_on_risk"] = expected_return
    output["decision_score"] = probability
    output["score_basis"] = SCENARIO_PRIOR_SCORE_BASIS
    output["schema_version"] = STRATEGY_CANDIDATE_SCHEMA_VERSION
    output["model_version"] = STRATEGY_PRIOR_POLICY_VERSION
    output["model_policy_version"] = STRATEGY_MODEL_POLICY_VERSION
    output["ranking_policy_version"] = STRATEGY_RANKING_POLICY_VERSION
    output["model_status"] = "MARKET_STATE_PRIOR"
    output = output.sort_values(
        ["decision_score", "expected_return_on_risk", "candidate_key"],
        ascending=[False, False, True],
        kind="mergesort",
    ).reset_index(drop=True)
    output["candidate_rank"] = np.arange(1, len(output) + 1, dtype=int)
    return output


def _candidate_prior(
    candidate: Mapping[str, object],
    *,
    state: MarketState,
    policy: StrategySelectionPolicy,
) -> dict[str, float]:
    probability_up = state.effective_probability_up
    positive_returns = state.expected_absolute_move * _HALF_NORMAL_MAGNITUDES
    scenario_returns = np.concatenate((positive_returns, -positive_returns))
    weights = np.concatenate(
        (
            np.full(_SCENARIO_COUNT, probability_up / _SCENARIO_COUNT),
            np.full(_SCENARIO_COUNT, (1.0 - probability_up) / _SCENARIO_COUNT),
        )
    )
    underlying = _required_finite(candidate.get("underlying_price"), "underlying")
    underlying_change = underlying * scenario_returns
    delta = _finite_or_default(candidate.get("net_delta"))
    gamma = _finite_or_default(candidate.get("net_gamma"))
    theta = _finite_or_default(candidate.get("net_theta"))
    friction = _round_trip_friction(candidate, policy=policy)
    profit = (
        delta * underlying_change
        + 0.5 * gamma * np.square(underlying_change)
        + theta * state.holding_days
        - friction
    )
    maximum_loss = _required_finite(candidate.get("max_loss"), "maximum loss")
    profit = np.maximum(profit, -maximum_loss)
    maximum_profit = _finite(candidate.get("max_profit"))
    if maximum_profit is not None:
        profit = np.minimum(profit, maximum_profit)
    expected_net_profit = float(np.sum(weights * profit))
    capital = _required_finite(candidate.get("capital_required"), "capital")
    if capital <= 0.0:
        raise ValueError("Strategy prior requires positive capital")
    return {
        "probability": float(np.sum(weights * (profit > 0.0))),
        "expected_net_profit": expected_net_profit,
        "expected_return_on_risk": expected_net_profit / capital,
    }


def _round_trip_friction(
    candidate: Mapping[str, object],
    *,
    policy: StrategySelectionPolicy,
) -> float:
    try:
        legs = json.loads(str(candidate.get("legs_json") or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Strategy prior requires readable exact legs") from exc
    if not isinstance(legs, list) or not legs:
        raise ValueError("Strategy prior requires readable exact legs")
    friction = 0.0
    for leg in legs:
        if not isinstance(leg, Mapping):
            raise ValueError("Strategy prior received an invalid exact leg")
        quantity = _required_finite(leg.get("quantity"), "leg quantity")
        multiplier = _required_finite(leg.get("multiplier"), "leg multiplier")
        bid = _required_finite(leg.get("bid"), "leg bid")
        ask = _required_finite(leg.get("ask"), "leg ask")
        friction += quantity * multiplier * max(ask - bid, 0.0)
        if str(leg.get("asset") or "").upper() == "OPTION":
            friction += 2.0 * quantity * policy.per_contract_fee
    return friction


def _expected_absolute_move(
    sample: Mapping[str, object] | pd.Series,
    *,
    surface: Mapping[str, object] | pd.Series,
    holding_days: float,
) -> float:
    horizon_move = _first_finite(
        surface.get("realized_expected_absolute_move_atm_horizon"),
        surface.get("atm_straddle_implied_move"),
    )
    horizon_days = _finite(surface.get("atm_days_to_expiration"))
    if horizon_move is not None and horizon_move >= 0.0:
        base_days = max(horizon_days or holding_days, 1.0 / 1_440.0)
        return float(np.clip(horizon_move * math.sqrt(holding_days / base_days), 0.0, 1.0))
    atr_percent = _finite(sample.get("mr__atr_percent"))
    if atr_percent is None or atr_percent < 0.0:
        return 0.0
    return float(np.clip((atr_percent / 100.0) * math.sqrt(holding_days), 0.0, 1.0))


def _binary_uncertainty(probability: float | None) -> float:
    if probability is None:
        return 1.0
    if probability <= 0.0 or probability >= 1.0:
        return 0.0
    return float(
        -(
            probability * math.log(probability)
            + (1.0 - probability) * math.log(1.0 - probability)
        )
        / math.log(2.0)
    )


def _probability(value: object) -> float | None:
    number = _finite(value)
    return float(np.clip(number, 0.0, 1.0)) if number is not None else None


def _unit_score(value: object) -> float | None:
    number = _finite(value)
    return float(np.clip(number / 100.0, 0.0, 1.0)) if number is not None else None


def _centered_strength(value: object) -> float | None:
    number = _finite(value)
    return (
        float(np.clip(abs(number - 50.0) / 50.0, 0.0, 1.0))
        if number is not None
        else None
    )


def _range_extremity(value: object) -> float | None:
    number = _finite(value)
    return (
        float(np.clip(abs(number - 0.5) * 2.0, 0.0, 1.0))
        if number is not None
        else None
    )


def _mean_available(*values: float | None) -> float | None:
    available = [value for value in values if value is not None]
    return float(np.mean(available)) if available else None


def _first_finite(*values: object) -> float | None:
    for value in values:
        number = _finite(value)
        if number is not None:
            return number
    return None


def _finite_or_default(value: object, default: float = 0.0) -> float:
    number = _finite(value)
    return number if number is not None else default


def _required_finite(value: object, label: str) -> float:
    number = _finite(value)
    if number is None:
        raise ValueError(f"Strategy prior requires finite {label}")
    return number


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _utc(value: object) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("Market-state timestamp is invalid")
    return pd.Timestamp(timestamp)


__all__ = [
    "MarketState",
    "infer_market_state",
    "score_market_state_prior",
    "MARKET_STATE_POLICY_VERSION",
    "STRATEGY_PRIOR_POLICY_VERSION",
]
