from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Mapping, Sequence

import pandas as pd

from app.models.market_data import MarketQuote


DAY_ONLY = "Day only"
GOOD_UNTIL_CANCELED = "Good until canceled"
MARKET_ORDER = "Market"
LIMIT_ORDER = "Limit"
NET_DEBIT_LIMIT = "Net debit limit"
NET_CREDIT_LIMIT = "Net credit limit"

STRATEGY_ORDER_DRAFT_VERSION = "schwab-strategy-order-draft-v1"


@dataclass(frozen=True)
class SchwabPositionContext:
    symbol: str
    observed_at: datetime | None
    shares: float
    option_contracts: float
    working_option_orders: int
    available_cash: float | None
    available_cash_source: str = "Available Funds"
    non_marginable_funds: float | None = None
    cash_balance: float | None = None
    buying_power: float | None = None
    liquidation_value: float | None = None
    account_values_status: str = "UNAVAILABLE"
    underlying_price: float | None = None


@dataclass(frozen=True)
class StrategyOrderLeg:
    asset_type: str
    symbol: str
    instruction: str
    quantity: int
    display_name: str
    bid: float | None
    ask: float | None
    multiplier: float
    option_type: str | None = None
    strike: float | None = None
    expiration: str | None = None


@dataclass(frozen=True)
class StrategyOrderComponent:
    display_name: str
    complex_order_strategy_type: str
    suggested_order_method: str
    suggested_limit_price: float | None
    duration: str
    legs: tuple[StrategyOrderLeg, ...]

    @property
    def order_method_choices(self) -> tuple[str, ...]:
        if self.suggested_order_method == MARKET_ORDER:
            return (MARKET_ORDER,)
        return (self.suggested_order_method, MARKET_ORDER)


@dataclass(frozen=True)
class StrategyOrderDraft:
    candidate_id: str
    symbol: str
    strategy_name: str
    strategy_display_name: str
    legs: tuple[StrategyOrderLeg, ...]
    orders: tuple[StrategyOrderComponent, ...]
    uses_existing_shares: bool
    shares_required_per_strategy: float
    shares_available: float
    version: str = STRATEGY_ORDER_DRAFT_VERSION

    @property
    def order_count(self) -> int:
        return len(self.orders)


def schwab_position_context(
    account_facts: Mapping[str, object] | None,
    *,
    symbol: str,
    observed_at: datetime | None = None,
) -> SchwabPositionContext:
    clean_symbol = str(symbol).strip().upper()
    shares = 0.0
    option_contracts = 0.0
    working_option_orders = 0
    available_cash: float | None = None
    available_cash_source = "Available Funds"
    non_marginable_funds: float | None = None
    cash_balance: float | None = None
    buying_power: float | None = None
    liquidation_value: float | None = None
    account_values_status = "UNAVAILABLE"
    underlying_price: float | None = None
    facts = account_facts if isinstance(account_facts, Mapping) else {}

    positions = facts.get("positions")
    if isinstance(positions, Mapping):
        items = positions.get("items")
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                asset_type = str(item.get("asset_type") or "").strip().upper()
                item_symbol = str(item.get("symbol") or "").strip().upper()
                underlying = str(
                    item.get("underlying_symbol") or item_symbol
                ).strip().upper()
                quantity = _finite_number(item.get("net_quantity"))
                if quantity is None:
                    continue
                if asset_type in {"EQUITY", "STOCK"} and item_symbol == clean_symbol:
                    shares += quantity
                    observed_price = _finite_number(item.get("price"))
                    if observed_price is None and quantity != 0.0:
                        market_value = _finite_number(item.get("market_value"))
                        if market_value is not None:
                            observed_price = abs(market_value / quantity)
                    if observed_price is not None and observed_price > 0.0:
                        underlying_price = observed_price
                elif "OPTION" in asset_type and underlying == clean_symbol:
                    option_contracts += abs(quantity)

    working = facts.get("working_orders")
    if isinstance(working, Mapping):
        items = working.get("items")
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                asset_type = str(item.get("asset_type") or "").strip().upper()
                underlying = str(
                    item.get("underlying_symbol")
                    or item.get("symbol")
                    or ""
                ).strip().upper()
                if "OPTION" in asset_type and underlying == clean_symbol:
                    working_option_orders += 1

    account_values = facts.get("account_values")
    if isinstance(account_values, Mapping):
        account_values_status = str(
            account_values.get("status") or "UNAVAILABLE"
        ).strip().upper()
        for field, label in (
            ("available_funds", "Available Funds"),
            ("cash_available_for_trading", "Cash Available for Trading"),
            ("cash_available_for_withdrawal", "Cash Available for Withdrawal"),
            ("cash_balance", "Cash Balance"),
        ):
            available_cash = _finite_number(account_values.get(field))
            if available_cash is not None:
                available_cash_source = label
                break
        non_marginable_funds = _finite_number(
            account_values.get("available_funds_non_marginable_trade")
        )
        cash_balance = _finite_number(account_values.get("cash_balance"))
        buying_power = _finite_number(account_values.get("buying_power"))
        liquidation_value = _finite_number(
            account_values.get("liquidation_value")
        )

    return SchwabPositionContext(
        symbol=clean_symbol,
        observed_at=observed_at,
        shares=round(shares, 8),
        option_contracts=round(option_contracts, 8),
        working_option_orders=working_option_orders,
        available_cash=available_cash,
        available_cash_source=available_cash_source,
        non_marginable_funds=non_marginable_funds,
        cash_balance=cash_balance,
        buying_power=buying_power,
        liquidation_value=liquidation_value,
        account_values_status=account_values_status,
        underlying_price=underlying_price,
    )


def build_strategy_order_draft(
    candidate: Mapping[str, object],
    *,
    position: SchwabPositionContext,
) -> StrategyOrderDraft:
    symbol = _required_text(candidate.get("symbol"), "Candidate symbol").upper()
    if symbol != position.symbol:
        raise ValueError(
            f"Candidate symbol {symbol} does not match position context "
            f"{position.symbol}."
        )
    strategy_name = _required_text(
        candidate.get("strategy_name"), "Strategy name"
    ).lower()
    display_name = _required_text(
        candidate.get("strategy_display_name"), "Strategy display name"
    )
    candidate_id = _required_text(candidate.get("id"), "Candidate id")
    stock_requirement = str(candidate.get("stock_requirement") or "NONE").upper()
    raw_legs = _candidate_legs(candidate.get("legs_json"))
    stock_leg_quantity = sum(
        float(leg.get("quantity") or 0.0)
        for leg in raw_legs
        if str(leg.get("asset") or "").upper() == "STOCK"
        and str(leg.get("side") or "").upper() == "LONG"
    )
    uses_existing_shares = False
    include_stock_legs = True
    if stock_requirement == "EXISTING_100_SHARES":
        include_stock_legs = False
        uses_existing_shares = True
    elif stock_requirement == "EXISTING_OR_ATOMIC_100_SHARES":
        uses_existing_shares = position.shares >= stock_leg_quantity > 0.0
        include_stock_legs = not uses_existing_shares

    legs: list[StrategyOrderLeg] = []
    for raw_leg in raw_legs:
        asset = str(raw_leg.get("asset") or "").strip().upper()
        side = str(raw_leg.get("side") or "").strip().upper()
        if asset == "STOCK" and not include_stock_legs:
            continue
        quantity = _positive_int(raw_leg.get("quantity"), "Leg quantity")
        bid = _finite_number(raw_leg.get("bid"))
        ask = _finite_number(raw_leg.get("ask"))
        if asset == "OPTION":
            contract_symbol = _required_text(
                raw_leg.get("contract_symbol"), "Option contract symbol"
            ).upper()
            instruction = (
                "BUY_TO_OPEN" if side == "LONG" else "SELL_TO_OPEN"
            )
            multiplier = _positive_number(
                raw_leg.get("multiplier"), "Option multiplier"
            )
            option_type = _required_text(
                raw_leg.get("option_type"), "Option type"
            ).upper()
            if option_type not in {"CALL", "PUT"}:
                raise ValueError(f"Unsupported option type: {option_type}")
            strike = _positive_number(raw_leg.get("strike"), "Option strike")
            expiration = _required_text(
                raw_leg.get("expiration_date"), "Option expiration"
            )
            display = _option_display_name(symbol, raw_leg)
            legs.append(
                StrategyOrderLeg(
                    asset_type="OPTION",
                    symbol=contract_symbol,
                    instruction=instruction,
                    quantity=quantity,
                    display_name=display,
                    bid=bid,
                    ask=ask,
                    multiplier=multiplier,
                    option_type=option_type,
                    strike=strike,
                    expiration=expiration,
                )
            )
        elif asset == "STOCK":
            instruction = "BUY" if side == "LONG" else "SELL"
            legs.append(
                StrategyOrderLeg(
                    asset_type="EQUITY",
                    symbol=symbol,
                    instruction=instruction,
                    quantity=quantity,
                    display_name=f"{symbol} shares",
                    bid=bid,
                    ask=ask,
                    multiplier=1.0,
                )
            )
        else:
            raise ValueError(f"Unsupported strategy leg asset: {asset or 'missing'}")
    if not legs:
        raise ValueError("Strategy candidate did not produce an order leg.")

    orders = _order_components(
        strategy_name,
        tuple(legs),
        stock_included=include_stock_legs and stock_leg_quantity > 0.0,
    )
    return StrategyOrderDraft(
        candidate_id=candidate_id,
        symbol=symbol,
        strategy_name=strategy_name,
        strategy_display_name=display_name,
        legs=tuple(legs),
        orders=orders,
        uses_existing_shares=uses_existing_shares,
        shares_required_per_strategy=stock_leg_quantity if uses_existing_shares else 0.0,
        shares_available=position.shares,
    )


def build_strategy_order_payload(
    draft: StrategyOrderDraft,
    *,
    order_index: int = 0,
    strategy_quantity: int,
    order_method: str,
    limit_price: object | None,
    duration: str,
) -> dict[str, object]:
    if not 0 <= order_index < draft.order_count:
        raise ValueError(
            f"Order component {order_index + 1} is outside this "
            f"{draft.order_count}-order strategy."
        )
    component = draft.orders[order_index]
    quantity = _positive_int(strategy_quantity, "Strategy quantity")
    method = str(order_method).strip()
    if method not in component.order_method_choices:
        raise ValueError(f"Unsupported order method: {method or 'missing'}")
    if duration not in {DAY_ONLY, GOOD_UNTIL_CANCELED}:
        raise ValueError(f"Unsupported order duration: {duration or 'missing'}")
    required_shares = draft.shares_required_per_strategy * quantity
    if draft.uses_existing_shares and draft.shares_available < required_shares:
        raise ValueError(
            f"{draft.strategy_display_name} requires {required_shares:g} "
            f"{draft.symbol} shares for {quantity:g} strategy unit(s); the "
            f"account currently reports {draft.shares_available:g}."
        )

    api_order_type = {
        LIMIT_ORDER: "LIMIT",
        NET_DEBIT_LIMIT: "NET_DEBIT",
        NET_CREDIT_LIMIT: "NET_CREDIT",
        MARKET_ORDER: "MARKET",
    }[method]
    payload: dict[str, object] = {
        "orderType": api_order_type,
        "session": "NORMAL",
        "duration": (
            "DAY" if duration == DAY_ONLY else "GOOD_TILL_CANCEL"
        ),
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": leg.instruction,
                "quantity": leg.quantity * quantity,
                "instrument": {
                    "symbol": leg.symbol,
                    "assetType": leg.asset_type,
                },
            }
            for leg in component.legs
        ],
    }
    if len(component.legs) > 1:
        payload["complexOrderStrategyType"] = (
            component.complex_order_strategy_type
        )
        payload["quantity"] = quantity
    if api_order_type != "MARKET":
        price = _positive_number(limit_price, "Limit price")
        payload["price"] = round(price, 2)
    return payload


def refresh_strategy_order_quotes(
    draft: StrategyOrderDraft,
    quotes: Mapping[str, MarketQuote],
) -> StrategyOrderDraft:
    """Rebuild a strategy draft from current exact-leg Schwab quotes."""
    refreshed_legs: list[StrategyOrderLeg] = []
    for leg in draft.legs:
        quote = quotes.get(leg.symbol)
        if quote is None:
            raise ValueError(f"Schwab did not return a current quote for {leg.symbol}.")
        bid = _finite_number(quote.bid)
        ask = _finite_number(quote.ask)
        if bid is None or ask is None or bid < 0.0 or ask <= 0.0 or ask < bid:
            raise ValueError(
                f"Schwab returned an unusable bid/ask for {leg.symbol}."
            )
        refreshed_legs.append(replace(leg, bid=bid, ask=ask))

    legs = tuple(refreshed_legs)
    orders = _order_components(
        draft.strategy_name,
        legs,
        stock_included=any(leg.asset_type == "EQUITY" for leg in legs),
    )
    return replace(draft, legs=legs, orders=orders)


def _order_components(
    strategy_name: str,
    legs: tuple[StrategyOrderLeg, ...],
    *,
    stock_included: bool,
) -> tuple[StrategyOrderComponent, ...]:
    if strategy_name == "twin_peak_fly":
        if len(legs) != 5 or legs[2].quantity != 2:
            raise ValueError(
                "Twin-Peak Fly requires five exact legs with two shared "
                "middle-strike calls."
            )
        shared_wing = replace(legs[2], quantity=1)
        return (
            _order_component(
                "Lower-price butterfly",
                (legs[0], legs[1], shared_wing),
                complex_order_strategy_type="BUTTERFLY",
            ),
            _order_component(
                "Higher-price butterfly",
                (shared_wing, legs[3], legs[4]),
                complex_order_strategy_type="BUTTERFLY",
            ),
        )
    if strategy_name == "range_to_trend_relay":
        if len(legs) != 6:
            raise ValueError(
                "Range-to-Trend Relay requires four near-expiration "
                "iron-condor legs and two later-expiration strangle legs."
            )
        return (
            _order_component(
                "Near-expiration iron condor",
                legs[:4],
                complex_order_strategy_type="IRON_CONDOR",
            ),
            _order_component(
                "Later-expiration long strangle",
                legs[4:],
                complex_order_strategy_type="STRANGLE",
            ),
        )
    return (
        _order_component(
            "Complete strategy",
            legs,
            complex_order_strategy_type=_complex_order_type(
                strategy_name,
                legs,
                stock_included=stock_included,
            ),
        ),
    )


def _order_component(
    display_name: str,
    legs: tuple[StrategyOrderLeg, ...],
    *,
    complex_order_strategy_type: str,
) -> StrategyOrderComponent:
    order_method, suggested_price = _suggested_order_terms(legs)
    return StrategyOrderComponent(
        display_name=display_name,
        complex_order_strategy_type=complex_order_strategy_type,
        suggested_order_method=order_method,
        suggested_limit_price=suggested_price,
        duration=DAY_ONLY,
        legs=legs,
    )


def _candidate_legs(value: object) -> list[Mapping[str, object]]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Candidate exact legs are not valid JSON.") from exc
    if not isinstance(parsed, list) or not parsed:
        raise ValueError("Candidate exact legs must be a non-empty list.")
    if not all(isinstance(item, Mapping) for item in parsed):
        raise ValueError("Candidate exact legs contain an invalid item.")
    return list(parsed)


def _suggested_order_terms(
    legs: Sequence[StrategyOrderLeg],
) -> tuple[str, float | None]:
    if len(legs) == 1:
        leg = legs[0]
        price = leg.ask if leg.instruction.startswith("BUY") else leg.bid
        return LIMIT_ORDER, round(max(float(price or 0.0), 0.01), 2)

    package_cash = 0.0
    for leg in legs:
        if leg.instruction.startswith("BUY"):
            quote = leg.ask
            sign = 1.0
        else:
            quote = leg.bid
            sign = -1.0
        if quote is None or quote < 0.0:
            raise ValueError(
                f"{leg.display_name} does not have a usable opening quote."
            )
        package_cash += sign * quote * leg.quantity * leg.multiplier
    price = max(abs(package_cash) / 100.0, 0.01)
    method = NET_DEBIT_LIMIT if package_cash >= 0.0 else NET_CREDIT_LIMIT
    return method, round(price, 2)


def _complex_order_type(
    strategy_name: str,
    legs: Sequence[StrategyOrderLeg],
    *,
    stock_included: bool,
) -> str:
    if len(legs) == 1:
        return "NONE"
    if stock_included:
        if strategy_name in {"covered_call", "buy_write"}:
            return "COVERED"
        if strategy_name in {"collar", "phoenix_collar"}:
            return "COLLAR_WITH_STOCK"
        return "CUSTOM"
    exact = {
        "long_straddle": "STRADDLE",
        "long_strangle": "STRANGLE",
        "covered_strangle": "STRANGLE",
        "bull_call_spread": "VERTICAL",
        "bear_put_spread": "VERTICAL",
        "bull_put_spread": "VERTICAL",
        "bear_call_spread": "VERTICAL",
        "long_call_butterfly": "BUTTERFLY",
        "long_put_butterfly": "BUTTERFLY",
        "short_call_butterfly": "BUTTERFLY",
        "short_put_butterfly": "BUTTERFLY",
        "iron_butterfly": "BUTTERFLY",
        "reverse_iron_butterfly": "BUTTERFLY",
        "long_call_condor": "CONDOR",
        "long_put_condor": "CONDOR",
        "iron_condor": "IRON_CONDOR",
        "reverse_iron_condor": "IRON_CONDOR",
        "long_call_calendar": "CALENDAR",
        "long_put_calendar": "CALENDAR",
        "bull_call_diagonal": "DIAGONAL",
        "poor_mans_covered_call": "DIAGONAL",
        "bear_put_diagonal": "DIAGONAL",
        "double_diagonal": "DOUBLE_DIAGONAL",
        "call_ratio_backspread": "BACK_RATIO",
        "put_ratio_backspread": "BACK_RATIO",
        "collar": "COLLAR_SYNTHETIC",
    }
    return exact.get(strategy_name, "CUSTOM")


def _option_display_name(symbol: str, leg: Mapping[str, object]) -> str:
    expiration = pd.to_datetime(
        leg.get("expiration_date"), utc=True, errors="coerce"
    )
    expiration_text = (
        pd.Timestamp(expiration).strftime("%b %d, %Y").replace(" 0", " ")
        if not pd.isna(expiration)
        else "Unknown expiration"
    )
    strike = _finite_number(leg.get("strike"))
    strike_text = f"${strike:g}" if strike is not None else "Unknown strike"
    option_type = str(leg.get("option_type") or "Option").strip().title()
    return f"{symbol} {expiration_text} {strike_text} {option_type}"


def _required_text(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required.")
    return text


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_number(value: object, label: str) -> float:
    number = _finite_number(value)
    if number is None or number <= 0.0:
        raise ValueError(f"{label} must be a positive number.")
    return number


def _positive_int(value: object, label: str) -> int:
    number = _positive_number(value, label)
    integer = int(number)
    if not math.isclose(number, integer, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError(f"{label} must be a whole number.")
    return integer


__all__ = [
    "DAY_ONLY",
    "GOOD_UNTIL_CANCELED",
    "LIMIT_ORDER",
    "MARKET_ORDER",
    "NET_CREDIT_LIMIT",
    "NET_DEBIT_LIMIT",
    "STRATEGY_ORDER_DRAFT_VERSION",
    "SchwabPositionContext",
    "StrategyOrderComponent",
    "StrategyOrderDraft",
    "refresh_strategy_order_quotes",
    "StrategyOrderLeg",
    "build_strategy_order_draft",
    "build_strategy_order_payload",
    "schwab_position_context",
]
