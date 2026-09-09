from __future__ import annotations

import math
from datetime import date
from typing import Sequence

from app.models.option_management import OptionExerciseAnalysis, OptionPositionLeg


EXERCISE_CAPABILITY_REASON = (
    "Schwab option-exercise submission is not verified in this application. "
    "Use this analysis to review the exact leg, then contact Schwab or use a verified broker workflow."
)


def exercise_action_disabled_reason(legs: Sequence[OptionPositionLeg]) -> str | None:
    if not legs:
        return "Select one exact long option leg to analyze exercise."
    if len(legs) != 1:
        return "Exercise analysis is per exact long leg; select one contract row."
    leg = legs[0]
    if leg.close_disabled_reason:
        return leg.close_disabled_reason
    if leg.net_quantity <= 0:
        return "Short option legs cannot be exercised; assignment is controlled by the holder."
    if not math.isclose(leg.net_quantity, round(leg.net_quantity), abs_tol=1e-8):
        return "Exercise quantity must be a positive whole number of contracts."
    if leg.option_type.upper() not in {"CALL", "PUT"}:
        return "Call/put identity is required for exercise analysis."
    try:
        expiration = date.fromisoformat(leg.expiration[:10])
    except (TypeError, ValueError):
        return "A valid expiration is required for exercise analysis."
    observed_date = leg.observed_at.astimezone().date() if leg.observed_at else date.today()
    if expiration < observed_date:
        return "The selected option is expired and cannot be analyzed as an open exercise request."
    if not leg.symbol or leg.strike <= 0 or leg.contract_multiplier <= 0:
        return "Exact OCC identity, strike, and contract multiplier are required for exercise analysis."
    return None


def build_option_exercise_analysis(
    leg: OptionPositionLeg,
    *,
    related_position_legs: Sequence[OptionPositionLeg] = (),
    underlying_price: float | None = None,
) -> OptionExerciseAnalysis:
    reason = exercise_action_disabled_reason((leg,))
    if reason:
        raise ValueError(reason)
    quantity = int(round(leg.net_quantity))
    share_quantity = quantity * leg.contract_multiplier
    is_call = leg.option_type.upper() == "CALL"
    resulting_stock_quantity = share_quantity if is_call else -share_quantity
    strike_cash_effect = -leg.strike * share_quantity if is_call else leg.strike * share_quantity

    clean_underlying = _positive_number(underlying_price)
    intrinsic: float | None = None
    extrinsic: float | None = None
    if clean_underlying is not None:
        intrinsic = max(clean_underlying - leg.strike, 0.0) if is_call else max(leg.strike - clean_underlying, 0.0)
        if leg.mark is not None:
            extrinsic = max(float(leg.mark) - intrinsic, 0.0)

    warnings = [
        "Exercise exchanges the option at its strike; it is not a market order that sells or buys the option to close.",
        "A broker-confirmed exercise cutoff, settlement result, buying-power effect, and dividend treatment are unavailable here.",
    ]
    if extrinsic is None:
        warnings.append("Intrinsic and extrinsic value are unavailable because no verified underlying quote was supplied.")
    elif extrinsic > 0:
        warnings.append(f"Exercising would forfeit approximately ${extrinsic:,.2f} per share of displayed extrinsic value.")
    related = tuple(item for item in related_position_legs if item.symbol != leg.symbol)
    if related:
        warnings.append(
            f"{len(related)} other option leg{'s' if len(related) != 1 else ''} remain open; "
            "exercise can change the strategy's assignment and stock exposure."
        )

    return OptionExerciseAnalysis(
        account_label=leg.account_label,
        symbol=leg.symbol,
        contract_label=(
            f"{leg.underlying_symbol} {leg.expiration} {leg.strike:g} {leg.option_type.title()}"
        ),
        option_type=leg.option_type.upper(),
        strike=leg.strike,
        expiration=leg.expiration,
        quantity=quantity,
        resulting_stock_quantity=resulting_stock_quantity,
        strike_cash_effect=round(strike_cash_effect, 2),
        underlying_price=clean_underlying,
        intrinsic_value_per_share=None if intrinsic is None else round(intrinsic, 4),
        extrinsic_value_per_share=None if extrinsic is None else round(extrinsic, 4),
        settlement=None,
        capability_reason=EXERCISE_CAPABILITY_REASON,
        warnings=tuple(warnings),
    )


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


__all__ = [
    "EXERCISE_CAPABILITY_REASON",
    "build_option_exercise_analysis",
    "exercise_action_disabled_reason",
]
