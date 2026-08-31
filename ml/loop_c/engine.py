from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from ml.loop_c.policy import LOOP_C_POLICY_VERSION, LoopCMode, LoopCRiskLimits


LOOP_C_OPTION_SHADOW_HORIZONS: tuple[str, ...] = ("1d", "1w")


@dataclass(frozen=True)
class LoopCDecision:
    decision_timestamp: pd.Timestamp
    mode: LoopCMode
    action: str
    status: str
    reason_codes: tuple[str, ...]
    candidate_id: str | None
    symbol: str | None
    horizon: str | None
    quantity: int
    calibrated_probability: float | None
    sequence_directional_probability: float | None
    sequence_expected_return: float | None
    sequence_adverse_return: float | None
    expected_return_on_risk: float | None
    total_uncertainty: float | None
    expected_utility: float | None
    modeled_maximum_loss: float | None
    automated_action_allowed: bool = False
    orders_enabled: bool = False
    orders_placed: int = 0
    policy_version: str = LOOP_C_POLICY_VERSION

    def as_record(self) -> dict[str, object]:
        payload = asdict(self)
        payload["mode"] = self.mode.value
        payload["reason_codes"] = list(self.reason_codes)
        return payload


def evaluate_loop_c(
    candidates: pd.DataFrame,
    *,
    decision_timestamp: object,
    mode: LoopCMode | str,
    market_session_open: bool,
    portfolio: Mapping[str, object] | None,
    broker: Mapping[str, object] | None,
    risk_limits: LoopCRiskLimits,
    model_authority: str,
    model_published_at: object | None,
    halt_requested: bool = False,
) -> LoopCDecision:
    """Evaluate an hourly decision under deterministic, non-ML authority.

    Version 1 is observe-only by construction.  It may identify a research
    proposal, reduce-only condition, or halt condition, but contains no broker
    submission path and always reports ``orders_placed=0``.
    """

    now = _utc(decision_timestamp, "decision_timestamp")
    selected_mode = LoopCMode(str(mode).upper())
    global_reasons = _global_gates(
        now=now,
        mode=selected_mode,
        market_session_open=market_session_open,
        portfolio=portfolio,
        broker=broker,
        risk_limits=risk_limits,
        model_authority=str(model_authority).upper(),
        model_published_at=model_published_at,
        halt_requested=halt_requested,
    )
    if "HALT_REQUESTED" in global_reasons or "DAILY_LOSS_LIMIT" in global_reasons:
        return _empty_decision(now, LoopCMode.HALT, "HALT", global_reasons)
    if selected_mode == LoopCMode.FLATTEN:
        return _empty_decision(now, selected_mode, "FLATTEN_REVIEW", global_reasons)
    research_only_reasons = {"OBSERVE_MODE", "MODEL_AUTHORITY_NOT_ACTIVE"}
    blocking_reasons = tuple(
        reason for reason in global_reasons if reason not in research_only_reasons
    )
    if blocking_reasons:
        action = "REDUCE_ONLY_REVIEW" if selected_mode == LoopCMode.REDUCE_ONLY else "NO_TRADE"
        return _empty_decision(now, selected_mode, action, blocking_reasons)

    required = {
        "id",
        "symbol",
        "horizon",
        "calibrated_probability",
        "sequence_directional_probability",
        "sequence_expected_return",
        "sequence_adverse_return",
        "expected_return_on_risk",
        "total_uncertainty",
        "max_loss",
        "capital_required",
    }
    missing = sorted(required.difference(candidates.columns))
    if missing:
        return _empty_decision(
            now,
            selected_mode,
            "NO_TRADE",
            ("CANDIDATE_SCHEMA_INVALID",),
        )
    candidate_horizons = candidates["horizon"].astype("string").str.lower()
    horizon_eligible = candidate_horizons.isin(LOOP_C_OPTION_SHADOW_HORIZONS)
    if not bool(horizon_eligible.any()):
        return _empty_decision(
            now,
            selected_mode,
            "NO_TRADE",
            ("OPTIONS_SHADOW_HORIZON_BELOW_1D",),
        )
    eligible: list[dict[str, object]] = []
    for row in candidates.loc[horizon_eligible].to_dict(orient="records"):
        screened = _screen_candidate(row, risk_limits=risk_limits, portfolio=portfolio or {})
        if screened is not None:
            eligible.append(screened)
    if not eligible:
        return _empty_decision(
            now,
            selected_mode,
            "NO_TRADE",
            ("NO_CANDIDATE_PASSED_RISK_GATES",),
        )
    chosen = sorted(
        eligible,
        key=lambda row: (-float(row["expected_utility"]), str(row["id"])),
    )[0]
    return LoopCDecision(
        decision_timestamp=now,
        mode=selected_mode,
        action="RESEARCH_PROPOSAL",
        status="OBSERVE_ONLY",
        reason_codes=tuple(
            dict.fromkeys((*global_reasons, "SHADOW_MODEL_NO_ORDER_AUTHORITY"))
        ),
        candidate_id=str(chosen["id"]),
        symbol=str(chosen["symbol"]),
        horizon=str(chosen["horizon"]),
        quantity=int(chosen["quantity"]),
        calibrated_probability=float(chosen["calibrated_probability"]),
        sequence_directional_probability=float(
            chosen["sequence_directional_probability"]
        ),
        sequence_expected_return=float(chosen["sequence_expected_return"]),
        sequence_adverse_return=float(chosen["sequence_adverse_return"]),
        expected_return_on_risk=float(chosen["expected_return_on_risk"]),
        total_uncertainty=float(chosen["total_uncertainty"]),
        expected_utility=float(chosen["expected_utility"]),
        modeled_maximum_loss=float(chosen["max_loss"]) * int(chosen["quantity"]),
    )


def _global_gates(
    *,
    now: pd.Timestamp,
    mode: LoopCMode,
    market_session_open: bool,
    portfolio: Mapping[str, object] | None,
    broker: Mapping[str, object] | None,
    risk_limits: LoopCRiskLimits,
    model_authority: str,
    model_published_at: object | None,
    halt_requested: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if halt_requested or mode == LoopCMode.HALT:
        reasons.append("HALT_REQUESTED")
    if mode == LoopCMode.OBSERVE:
        reasons.append("OBSERVE_MODE")
    if not market_session_open:
        reasons.append("MARKET_SESSION_CLOSED")
    if model_authority != "ACTIVE":
        reasons.append("MODEL_AUTHORITY_NOT_ACTIVE")
    if model_published_at is None:
        reasons.append("MODEL_PUBLICATION_UNAVAILABLE")
    else:
        model_age = (now - _utc(model_published_at, "model_published_at")).total_seconds()
        if model_age < 0.0 or model_age > risk_limits.maximum_model_age_seconds:
            reasons.append("MODEL_STALE")
    if not isinstance(portfolio, Mapping):
        reasons.append("PORTFOLIO_UNAVAILABLE")
    else:
        if portfolio.get("reconciled") is not True:
            reasons.append("PORTFOLIO_NOT_RECONCILED")
        if not isinstance(portfolio.get("symbol_exposure"), Mapping):
            reasons.append("SYMBOL_EXPOSURE_UNAVAILABLE")
        observed = portfolio.get("observed_at")
        if observed is None or not _fresh(
            observed, now=now, maximum_age=risk_limits.maximum_snapshot_age_seconds
        ):
            reasons.append("PORTFOLIO_STALE")
        daily_pnl = _number(portfolio.get("daily_pnl"))
        if daily_pnl is None:
            reasons.append("DAILY_PNL_UNAVAILABLE")
        elif daily_pnl <= -risk_limits.maximum_daily_loss:
            reasons.append("DAILY_LOSS_LIMIT")
        gross = _number(portfolio.get("gross_exposure"))
        if gross is None or gross > risk_limits.maximum_gross_exposure:
            reasons.append("GROSS_EXPOSURE_LIMIT")
        positions = _integer(portfolio.get("open_positions"))
        if positions is None or positions >= risk_limits.maximum_open_positions:
            reasons.append("POSITION_COUNT_LIMIT")
    if not isinstance(broker, Mapping):
        reasons.append("BROKER_RECONCILIATION_UNAVAILABLE")
    else:
        if broker.get("reconciled") is not True:
            reasons.append("BROKER_NOT_RECONCILED")
        observed = broker.get("observed_at")
        if observed is None or not _fresh(
            observed, now=now, maximum_age=risk_limits.maximum_snapshot_age_seconds
        ):
            reasons.append("BROKER_STATE_STALE")
        orders = _integer(broker.get("working_orders"))
        if orders is None or orders >= risk_limits.maximum_working_orders:
            reasons.append("WORKING_ORDER_LIMIT")
        if str(broker.get("unknown_submission_status") or "").upper() in {
            "TRUE",
            "1",
            "YES",
        } or broker.get("unknown_submission_status") is True:
            reasons.append("UNKNOWN_BROKER_SUBMISSION")
    return tuple(dict.fromkeys(reasons))


def _screen_candidate(
    row: Mapping[str, object],
    *,
    risk_limits: LoopCRiskLimits,
    portfolio: Mapping[str, object],
) -> dict[str, object] | None:
    try:
        thresholds = risk_limits.thresholds_for(row.get("horizon"))
    except ValueError:
        return None
    probability = _number(row.get("calibrated_probability"))
    sequence_probability = _number(row.get("sequence_directional_probability"))
    sequence_expected_return = _number(row.get("sequence_expected_return"))
    sequence_adverse_return = _number(row.get("sequence_adverse_return"))
    expected_return = _number(row.get("expected_return_on_risk"))
    uncertainty = _number(row.get("total_uncertainty"))
    maximum_loss = _number(row.get("max_loss"))
    capital_required = _number(row.get("capital_required"))
    if None in {
        probability,
        sequence_probability,
        sequence_expected_return,
        sequence_adverse_return,
        expected_return,
        uncertainty,
        maximum_loss,
        capital_required,
    }:
        return None
    assert probability is not None
    assert sequence_probability is not None
    assert expected_return is not None
    assert uncertainty is not None
    assert maximum_loss is not None
    assert capital_required is not None
    if (
        probability < thresholds.minimum_strategy_calibrated_probability
        or sequence_probability
        < thresholds.minimum_sequence_directional_probability
        or expected_return < thresholds.minimum_expected_return_on_risk
        or uncertainty > thresholds.maximum_total_uncertainty
        or maximum_loss <= 0.0
        or maximum_loss > risk_limits.maximum_trade_loss
        or capital_required <= 0.0
    ):
        return None
    available_cash = _number(portfolio.get("available_cash"))
    gross_exposure = _number(portfolio.get("gross_exposure"))
    raw_symbol_exposure = portfolio.get("symbol_exposure")
    symbol = str(row.get("symbol") or "").strip().upper()
    current_symbol_exposure = (
        _number(raw_symbol_exposure.get(symbol))
        if isinstance(raw_symbol_exposure, Mapping)
        else None
    )
    if (
        available_cash is None
        or gross_exposure is None
        or current_symbol_exposure is None
    ):
        return None
    remaining_gross = risk_limits.maximum_gross_exposure - gross_exposure
    remaining_symbol = (
        risk_limits.maximum_symbol_exposure - abs(current_symbol_exposure)
    )
    quantity = min(
        int(math.floor(risk_limits.maximum_trade_loss / maximum_loss)),
        int(math.floor(available_cash / capital_required)),
        int(math.floor(remaining_gross / capital_required)),
        int(math.floor(remaining_symbol / capital_required)),
        risk_limits.maximum_candidate_quantity,
    )
    if quantity < 1:
        return None
    utility = expected_return - thresholds.uncertainty_penalty * uncertainty
    if not np.isfinite(utility) or utility <= 0.0:
        return None
    output = dict(row)
    output["quantity"] = quantity
    output["expected_utility"] = float(utility)
    return output


def _empty_decision(
    now: pd.Timestamp,
    mode: LoopCMode,
    action: str,
    reasons: tuple[str, ...],
) -> LoopCDecision:
    return LoopCDecision(
        decision_timestamp=now,
        mode=mode,
        action=action,
        status="BLOCKED" if reasons else "NO_ACTION",
        reason_codes=reasons,
        candidate_id=None,
        symbol=None,
        horizon=None,
        quantity=0,
        calibrated_probability=None,
        sequence_directional_probability=None,
        sequence_expected_return=None,
        sequence_adverse_return=None,
        expected_return_on_risk=None,
        total_uncertainty=None,
        expected_utility=None,
        modeled_maximum_loss=None,
    )


def _fresh(value: object, *, now: pd.Timestamp, maximum_age: float) -> bool:
    observed = _utc(value, "observed_at")
    age = (now - observed).total_seconds()
    return 0.0 <= age <= maximum_age


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    number = _number(value)
    if number is None or number < 0.0 or not number.is_integer():
        return None
    return int(number)


def _utc(value: object, label: str) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{label} must be a valid timestamp")
    return pd.Timestamp(parsed)


__all__ = [
    "LOOP_C_OPTION_SHADOW_HORIZONS",
    "LoopCDecision",
    "evaluate_loop_c",
]
