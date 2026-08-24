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


_RISK_SCENARIO_MOVES = (-0.20, -0.10, 0.0, 0.10, 0.20)


@dataclass(frozen=True)
class StrategyPriceScenario:
    """One expiration-price scenario for current shares and the selected ticket."""

    label: str
    underlying_price: float
    price_change: float
    price_change_percent: float
    existing_shares_profit_loss: float
    ticket_profit_loss: float
    combined_profit_loss: float


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
    reference_price: float | None
    reference_price_basis: str
    risk_expiration: str | None
    risk_status: str
    published_strategy_max_loss: float | None
    ticket_max_loss: float | None
    ticket_max_loss_unbounded: bool
    ticket_worst_case_price: float | None
    combined_max_loss: float | None
    combined_max_loss_unbounded: bool
    combined_worst_case_price: float | None
    risk_basis: str
    price_scenarios: tuple[StrategyPriceScenario, ...]

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
    risk = _strategy_risk_profile(
        candidate_row,
        component=component,
        position=position,
        strategy_quantity=quantity,
        opening_cash_flow=opening_cash_flow,
        opening_cash_flow_basis=cash_flow_basis,
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
        reference_price=risk.reference_price,
        reference_price_basis=risk.reference_price_basis,
        risk_expiration=risk.risk_expiration,
        risk_status=risk.risk_status,
        published_strategy_max_loss=risk.published_strategy_max_loss,
        ticket_max_loss=risk.ticket_max_loss,
        ticket_max_loss_unbounded=risk.ticket_max_loss_unbounded,
        ticket_worst_case_price=risk.ticket_worst_case_price,
        combined_max_loss=risk.combined_max_loss,
        combined_max_loss_unbounded=risk.combined_max_loss_unbounded,
        combined_worst_case_price=risk.combined_worst_case_price,
        risk_basis=risk.risk_basis,
        price_scenarios=risk.price_scenarios,
    )


@dataclass(frozen=True)
class _StrategyRiskProfile:
    reference_price: float | None
    reference_price_basis: str
    risk_expiration: str | None
    risk_status: str
    published_strategy_max_loss: float | None
    ticket_max_loss: float | None
    ticket_max_loss_unbounded: bool
    ticket_worst_case_price: float | None
    combined_max_loss: float | None
    combined_max_loss_unbounded: bool
    combined_worst_case_price: float | None
    risk_basis: str
    price_scenarios: tuple[StrategyPriceScenario, ...]


def _strategy_risk_profile(
    candidate_row: Mapping[str, object],
    *,
    component: StrategyOrderComponent,
    position: SchwabPositionContext,
    strategy_quantity: int,
    opening_cash_flow: float | None,
    opening_cash_flow_basis: str,
) -> _StrategyRiskProfile:
    position_price = _positive_number(getattr(position, "underlying_price", None))
    candidate_price = _positive_number(candidate_row.get("underlying_price"))
    if position_price is not None:
        reference_price = position_price
        reference_price_basis = "Current Schwab position price"
    else:
        reference_price = candidate_price
        reference_price_basis = (
            "Candidate snapshot underlying price"
            if candidate_price is not None
            else "Underlying price unavailable"
        )
    published_loss = _nonnegative_number(candidate_row.get("max_loss"))
    published_strategy_max_loss = (
        None
        if published_loss is None
        else round(published_loss * strategy_quantity, 2)
    )

    if opening_cash_flow is None:
        return _unavailable_risk_profile(
            reference_price=reference_price,
            reference_price_basis=reference_price_basis,
            published_strategy_max_loss=published_strategy_max_loss,
            status="OPENING_CASH_FLOW_UNAVAILABLE",
            basis=(
                "Max-loss scenarios need a complete configured opening cash flow. "
                "Schwab order review remains authoritative."
            ),
        )

    option_legs = tuple(
        leg for leg in component.legs if leg.asset_type.upper() == "OPTION"
    )
    incomplete = tuple(
        leg.display_name
        for leg in option_legs
        if leg.option_type not in {"CALL", "PUT"}
        or leg.strike is None
        or not math.isfinite(float(leg.strike))
        or float(leg.strike) <= 0.0
        or not str(leg.expiration or "").strip()
    )
    if incomplete:
        return _unavailable_risk_profile(
            reference_price=reference_price,
            reference_price_basis=reference_price_basis,
            published_strategy_max_loss=published_strategy_max_loss,
            status="OPTION_CONTRACT_DETAILS_UNAVAILABLE",
            basis=(
                "Exact max loss needs strike, type, and expiration for every "
                "selected option leg."
            ),
        )

    expirations = {
        _expiration_key(leg.expiration)
        for leg in option_legs
        if leg.expiration is not None
    }
    if len(expirations) > 1:
        return _unavailable_risk_profile(
            reference_price=reference_price,
            reference_price_basis=reference_price_basis,
            published_strategy_max_loss=published_strategy_max_loss,
            status="MULTI_EXPIRATION_REQUIRES_TIME_MODEL",
            basis=(
                "The selected order spans multiple expirations; one expiration "
                "payoff would be misleading. Use Schwab review and the published "
                "whole-strategy risk estimate instead."
            ),
        )

    risk_expiration = next(iter(expirations), None)
    strikes = sorted(
        {
            float(leg.strike)
            for leg in option_legs
            if leg.strike is not None
        }
    )
    knots = tuple(sorted({0.0, *strikes}))
    ticket_values = tuple(
        _ticket_expiration_profit_loss(
            component,
            strategy_quantity=strategy_quantity,
            underlying_price=price,
            opening_cash_flow=opening_cash_flow,
        )
        for price in knots
    )
    ticket_slope = _ticket_right_tail_slope(
        component,
        strategy_quantity=strategy_quantity,
    )
    ticket_unbounded = ticket_slope < -1e-12
    ticket_max_loss, ticket_worst_price = _finite_max_loss(
        knots,
        ticket_values,
        unbounded=ticket_unbounded,
    )

    shares_held = float(getattr(position, "shares", 0.0))
    combined_max_loss: float | None = None
    combined_unbounded = False
    combined_worst_price: float | None = None
    scenarios: tuple[StrategyPriceScenario, ...] = ()
    if reference_price is not None:
        combined_values = tuple(
            ticket_value + shares_held * (price - reference_price)
            for price, ticket_value in zip(knots, ticket_values)
        )
        combined_slope = ticket_slope + shares_held
        combined_unbounded = combined_slope < -1e-12
        combined_max_loss, combined_worst_price = _finite_max_loss(
            knots,
            combined_values,
            unbounded=combined_unbounded,
        )
        scenarios = tuple(
            _price_scenario(
                component,
                strategy_quantity=strategy_quantity,
                opening_cash_flow=opening_cash_flow,
                shares_held=shares_held,
                reference_price=reference_price,
                move=move,
            )
            for move in _RISK_SCENARIO_MOVES
        )

    expiration_label = (
        _expiration_display(risk_expiration)
        if risk_expiration is not None
        else "the selected order horizon"
    )
    exclusions = (
        "Existing option positions and working orders are excluded. Fees, dividends, "
        "taxes, early assignment, and pre-expiration time value are not modeled."
    )
    reference_text = (
        f" Existing shares are measured from ${reference_price:,.2f} "
        f"({reference_price_basis.lower()})."
        if reference_price is not None
        else " Existing-share P/L is unavailable without a reference stock price."
    )
    return _StrategyRiskProfile(
        reference_price=reference_price,
        reference_price_basis=reference_price_basis,
        risk_expiration=risk_expiration,
        risk_status="SINGLE_EXPIRATION_PAYOFF",
        published_strategy_max_loss=published_strategy_max_loss,
        ticket_max_loss=ticket_max_loss,
        ticket_max_loss_unbounded=ticket_unbounded,
        ticket_worst_case_price=ticket_worst_price,
        combined_max_loss=combined_max_loss,
        combined_max_loss_unbounded=combined_unbounded,
        combined_worst_case_price=combined_worst_price,
        risk_basis=(
            f"Expiration payoff at {expiration_label} using "
            f"{opening_cash_flow_basis.lower()}.{reference_text} {exclusions}"
        ),
        price_scenarios=scenarios,
    )


def _unavailable_risk_profile(
    *,
    reference_price: float | None,
    reference_price_basis: str,
    published_strategy_max_loss: float | None,
    status: str,
    basis: str,
) -> _StrategyRiskProfile:
    return _StrategyRiskProfile(
        reference_price=reference_price,
        reference_price_basis=reference_price_basis,
        risk_expiration=None,
        risk_status=status,
        published_strategy_max_loss=published_strategy_max_loss,
        ticket_max_loss=None,
        ticket_max_loss_unbounded=False,
        ticket_worst_case_price=None,
        combined_max_loss=None,
        combined_max_loss_unbounded=False,
        combined_worst_case_price=None,
        risk_basis=basis,
        price_scenarios=(),
    )


def _expiration_key(value: object) -> str:
    clean = str(value or "").strip()
    try:
        return datetime.fromisoformat(clean.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return clean


def _expiration_display(value: str) -> str:
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return observed.strftime("%b %d, %Y").replace(" 0", " ")


def _ticket_expiration_profit_loss(
    component: StrategyOrderComponent,
    *,
    strategy_quantity: int,
    underlying_price: float,
    opening_cash_flow: float,
) -> float:
    total = float(opening_cash_flow)
    for leg in component.legs:
        sign = 1.0 if leg.instruction.startswith("BUY") else -1.0
        units = leg.quantity * leg.multiplier * strategy_quantity
        if leg.asset_type.upper() == "EQUITY":
            total += sign * units * underlying_price
            continue
        if leg.option_type == "CALL":
            intrinsic = max(underlying_price - float(leg.strike), 0.0)
        else:
            intrinsic = max(float(leg.strike) - underlying_price, 0.0)
        total += sign * units * intrinsic
    return total


def _ticket_right_tail_slope(
    component: StrategyOrderComponent,
    *,
    strategy_quantity: int,
) -> float:
    slope = 0.0
    for leg in component.legs:
        sign = 1.0 if leg.instruction.startswith("BUY") else -1.0
        units = leg.quantity * leg.multiplier * strategy_quantity
        if leg.asset_type.upper() == "EQUITY" or leg.option_type == "CALL":
            slope += sign * units
    return slope


def _finite_max_loss(
    prices: Sequence[float],
    values: Sequence[float],
    *,
    unbounded: bool,
) -> tuple[float | None, float | None]:
    if unbounded:
        return None, None
    worst_price, worst_value = min(
        zip(prices, values),
        key=lambda item: (item[1], item[0]),
    )
    return round(max(0.0, -worst_value), 2), round(float(worst_price), 4)


def _price_scenario(
    component: StrategyOrderComponent,
    *,
    strategy_quantity: int,
    opening_cash_flow: float,
    shares_held: float,
    reference_price: float,
    move: float,
) -> StrategyPriceScenario:
    scenario_price = max(0.0, reference_price * (1.0 + move))
    price_change = scenario_price - reference_price
    shares_profit_loss = shares_held * price_change
    ticket_profit_loss = _ticket_expiration_profit_loss(
        component,
        strategy_quantity=strategy_quantity,
        underlying_price=scenario_price,
        opening_cash_flow=opening_cash_flow,
    )
    label = (
        "Unchanged"
        if math.isclose(move, 0.0, abs_tol=1e-12)
        else f"{'Up' if move > 0.0 else 'Down'} {abs(move) * 100:.0f}%"
    )
    return StrategyPriceScenario(
        label=label,
        underlying_price=round(scenario_price, 4),
        price_change=round(price_change, 4),
        price_change_percent=move,
        existing_shares_profit_loss=round(shares_profit_loss, 2),
        ticket_profit_loss=round(ticket_profit_loss, 2),
        combined_profit_loss=round(shares_profit_loss + ticket_profit_loss, 2),
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


def _nonnegative_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0.0 else None


__all__ = [
    "StrategyPriceScenario",
    "StrategyPortfolioImpact",
    "calculate_strategy_portfolio_impact",
]
