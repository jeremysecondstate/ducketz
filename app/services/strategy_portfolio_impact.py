from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.services.schwab_strategy_orders import (
    LIMIT_ORDER,
    MARKET_ORDER,
    NET_CREDIT_LIMIT,
    NET_DEBIT_LIMIT,
    SchwabPositionContext,
    StrategyOrderComponent,
    StrategyOrderDraft,
)


@dataclass(frozen=True)
class StrategyPortfolioImpact:
    """Current Schwab balances and conservative local entry-order estimates."""

    account_label: str
    observed_at: datetime | None
    applicable_funds_label: str
    applicable_funds: float | None
    available_funds: float | None
    non_marginable_funds: float | None
    cash_balance: float | None
    buying_power: float | None
    liquidation_value: float | None
    account_values_status: str
    estimated_funds_required: float | None
    estimated_capital_at_risk: float | None
    estimated_opening_cash_flow: float | None
    opening_cash_flow_basis: str
    funds_after_estimate: float | None
    coverage_ratio: float | None
    requirement_basis: str
    shares_held: float
    shares_required: float
    shares_after_estimate: float
    option_contracts: float
    working_option_orders: int

    @property
    def has_funds_shortfall(self) -> bool:
        return bool(
            self.applicable_funds is not None
            and self.estimated_funds_required is not None
            and self.applicable_funds + 1e-9 < self.estimated_funds_required
        )

    @property
    def has_share_shortfall(self) -> bool:
        return self.shares_after_estimate < -1e-9


def calculate_strategy_portfolio_impact(
    candidate_row: Mapping[str, object],
    *,
    order_draft: StrategyOrderDraft,
    position: SchwabPositionContext,
    order_index: int,
    strategy_quantity: object,
    order_method: str,
    limit_price: object | None,
    account_label: str,
) -> StrategyPortfolioImpact:
    """Calculate ticket impact from the same normalized Schwab account facts as Duckets."""
    quantity = _positive_int(strategy_quantity, "Strategy quantity")
    if not 0 <= int(order_index) < order_draft.order_count:
        raise ValueError("The selected Schwab order component is unavailable.")
    component = order_draft.orders[int(order_index)]
    opening_cash_flow, cash_flow_basis = _opening_cash_flow(
        component,
        strategy_quantity=quantity,
        order_method=order_method,
        limit_price=limit_price,
    )

    model_capital_per_strategy = _positive_number(
        candidate_row.get("capital_required")
    )
    model_capital = (
        None
        if model_capital_per_strategy is None
        else round(model_capital_per_strategy * quantity, 2)
    )
    collateral_per_strategy = _cash_collateral(candidate_row)
    collateral = (
        None
        if collateral_per_strategy is None
        else round(collateral_per_strategy * quantity, 2)
    )
    opening_debit = (
        None
        if opening_cash_flow is None or opening_cash_flow >= 0.0
        else abs(opening_cash_flow)
    )

    requirement_candidates: list[tuple[float, str]] = []
    if collateral is not None:
        requirement_candidates.append(
            (collateral, "Cash-secured collateral estimate")
        )
    if model_capital is not None and not order_draft.uses_existing_shares:
        requirement_candidates.append(
            (model_capital, "Published strategy risk-capital estimate")
        )
    if opening_debit is not None:
        requirement_candidates.append(
            (opening_debit, "Configured opening debit")
        )

    if requirement_candidates:
        estimated_requirement, requirement_basis = max(
            requirement_candidates,
            key=lambda item: item[0],
        )
    elif order_draft.uses_existing_shares:
        estimated_requirement = 0.0
        requirement_basis = "Held-share coverage; no additional cash estimate"
    else:
        estimated_requirement = None
        requirement_basis = "Unavailable until Schwab order review"

    non_marginable_funds = getattr(position, "non_marginable_funds", None)
    available_cash = getattr(position, "available_cash", None)
    if collateral is not None and non_marginable_funds is not None:
        applicable_funds = non_marginable_funds
        applicable_label = "Non-Marginable Funds"
    else:
        applicable_funds = available_cash
        applicable_label = str(
            getattr(position, "available_cash_source", "Available Funds")
        )
        if collateral is not None and applicable_funds is not None:
            applicable_label += " (Collateral Fallback)"

    funds_after = (
        None
        if applicable_funds is None or estimated_requirement is None
        else round(applicable_funds - estimated_requirement, 2)
    )
    coverage_ratio = (
        None
        if applicable_funds is None
        or estimated_requirement is None
        or estimated_requirement <= 0.0
        else applicable_funds / estimated_requirement
    )
    shares_required = round(
        order_draft.shares_required_per_strategy * quantity,
        8,
    )
    shares_held = float(
        getattr(position, "shares", order_draft.shares_available)
    )

    return StrategyPortfolioImpact(
        account_label=str(account_label or "Schwab"),
        observed_at=getattr(position, "observed_at", None),
        applicable_funds_label=applicable_label,
        applicable_funds=applicable_funds,
        available_funds=available_cash,
        non_marginable_funds=non_marginable_funds,
        cash_balance=getattr(position, "cash_balance", None),
        buying_power=getattr(position, "buying_power", None),
        liquidation_value=getattr(position, "liquidation_value", None),
        account_values_status=str(
            getattr(position, "account_values_status", "UNAVAILABLE")
        ),
        estimated_funds_required=estimated_requirement,
        estimated_capital_at_risk=model_capital,
        estimated_opening_cash_flow=opening_cash_flow,
        opening_cash_flow_basis=cash_flow_basis,
        funds_after_estimate=funds_after,
        coverage_ratio=coverage_ratio,
        requirement_basis=requirement_basis,
        shares_held=shares_held,
        shares_required=shares_required,
        shares_after_estimate=round(shares_held - shares_required, 8),
        option_contracts=float(getattr(position, "option_contracts", 0.0)),
        working_option_orders=int(
            getattr(position, "working_option_orders", 0)
        ),
    )


def _opening_cash_flow(
    component: StrategyOrderComponent,
    *,
    strategy_quantity: int,
    order_method: str,
    limit_price: object | None,
) -> tuple[float | None, str]:
    method = str(order_method).strip()
    if method == MARKET_ORDER:
        total = 0.0
        for leg in component.legs:
            is_buy = leg.instruction.startswith("BUY")
            quote = leg.ask if is_buy else leg.bid
            if quote is None or not math.isfinite(float(quote)) or float(quote) < 0.0:
                return None, "Current exact-leg BBO unavailable"
            total += (
                (-1.0 if is_buy else 1.0)
                * float(quote)
                * leg.quantity
                * leg.multiplier
                * strategy_quantity
            )
        return round(total, 2), "Current exact-leg BBO estimate"

    if method not in {LIMIT_ORDER, NET_DEBIT_LIMIT, NET_CREDIT_LIMIT}:
        raise ValueError(f"Unsupported order method: {method or 'missing'}")
    price = _positive_number(limit_price)
    if price is None:
        raise ValueError("Limit price must be a positive number.")
    if len(component.legs) == 1:
        leg = component.legs[0]
        amount = (
            price
            * leg.quantity
            * leg.multiplier
            * strategy_quantity
        )
        is_credit = leg.instruction.startswith("SELL")
    else:
        amount = price * 100.0 * strategy_quantity
        is_credit = method == NET_CREDIT_LIMIT
    return (
        round(amount if is_credit else -amount, 2),
        "Configured ticket limit",
    )


def _cash_collateral(candidate_row: Mapping[str, object]) -> float | None:
    cash_requirement = str(
        candidate_row.get("cash_requirement") or ""
    ).strip().upper()
    if "STRIKE_TIMES_MULTIPLIER" not in cash_requirement:
        return None
    try:
        legs = json.loads(str(candidate_row.get("legs_json") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(legs, Sequence) or isinstance(legs, (str, bytes)):
        return None
    requirements: list[float] = []
    for leg in legs:
        if not isinstance(leg, Mapping):
            continue
        if str(leg.get("asset") or "").strip().upper() != "OPTION":
            continue
        if str(leg.get("option_type") or "").strip().upper() != "PUT":
            continue
        if str(leg.get("side") or "").strip().upper() != "SHORT":
            continue
        strike = _positive_number(leg.get("strike"))
        multiplier = _positive_number(leg.get("multiplier"))
        quantity = _positive_number(leg.get("quantity"))
        if strike is not None and multiplier is not None and quantity is not None:
            requirements.append(strike * multiplier * quantity)
    if requirements:
        return max(requirements)
    return _positive_number(candidate_row.get("capital_required"))


def _positive_int(value: object, label: str) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive whole number.") from exc
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{label} must be a positive whole number.")
    integer = int(number)
    if not math.isclose(number, integer, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(f"{label} must be a positive whole number.")
    return integer


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0.0 else None


__all__ = [
    "StrategyPortfolioImpact",
    "calculate_strategy_portfolio_impact",
]
