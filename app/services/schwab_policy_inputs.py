from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any


SCHWAB_POLICY_INPUTS_SCHEMA_VERSION = "schwab-observed-policy-inputs/v1"

# Schwab reports exchange-traded funds through the account endpoint as
# COLLECTIVE_INVESTMENT positions.  That is an explicit non-option identity,
# even though these rows are outside the stock-policy calculator's supported
# EQUITY/STOCK universe.
_REVIEWED_NON_OPTION_ASSET_TYPES = frozenset({
    "COLLECTIVE_INVESTMENT",
    "EQUITY",
    "STOCK",
})

_ACCOUNT_VALUE_FIELDS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("liquidation_value", ("liquidationValue", "currentLiquidationValue", "accountValue"), "liquidation/account value"),
    ("cash_balance", ("cashBalance",), "cash balance"),
    ("short_balance", ("shortBalance",), "short-sale cash credit"),
    ("settled_cash", ("settledCash",), "settled cash"),
    ("cash_available_for_trading", ("cashAvailableForTrading",), "cash available for trading"),
    ("cash_available_for_withdrawal", ("cashAvailableForWithdrawal",), "cash available for withdrawal"),
    ("available_funds", ("availableFunds",), "available funds"),
    (
        "available_funds_non_marginable_trade",
        ("availableFundsNonMarginableTrade",),
        "available funds for non-marginable trades",
    ),
    ("buying_power", ("buyingPower",), "buying power"),
    (
        "buying_power_non_marginable_trade",
        ("buyingPowerNonMarginableTrade",),
        "buying power for non-marginable trades",
    ),
    ("day_trading_buying_power", ("dayTradingBuyingPower",), "day-trading buying power"),
    ("margin_balance", ("marginBalance",), "margin balance/debit"),
    ("maintenance_requirement", ("maintenanceRequirement",), "maintenance requirement"),
    ("maintenance_excess", ("maintenanceExcess",), "maintenance excess"),
    ("maintenance_call", ("maintenanceCall",), "maintenance call"),
    ("unsettled_cash", ("unsettledCash",), "unsettled cash"),
)

SCHWAB_CURRENT_ORDER_STATUSES = frozenset({
    "ACCEPTED",
    "AWAITING_CONDITION",
    "AWAITING_MANUAL_REVIEW",
    "AWAITING_PARENT_ORDER",
    "AWAITING_RELEASE_TIME",
    "AWAITING_STOP_CONDITION",
    "AWAITING_UR_OUT",
    "NEW",
    "PENDING_ACKNOWLEDGEMENT",
    "PENDING_ACTIVATION",
    "PENDING_CANCEL",
    "PENDING_RECALL",
    "PENDING_REPLACE",
    "QUEUED",
    "WORKING",
})

SCHWAB_TERMINAL_ORDER_STATUSES = frozenset({
    "CANCELED",
    "EXPIRED",
    "FILLED",
    "REJECTED",
    "REPLACED",
})


def normalize_schwab_policy_inputs(
    account_payload: Any,
    orders_payload: Any,
    *,
    observed_at: datetime,
    orders_error: str | None = None,
) -> dict[str, object]:
    """Normalize the Schwab facts currently consumed by calculated stock policy."""

    account = _securities_account(account_payload)
    timestamp = _iso_timestamp(observed_at)
    return {
        "schema_version": SCHWAB_POLICY_INPUTS_SCHEMA_VERSION,
        "authority": "OBSERVED",
        "source": "Schwab Trader API",
        "observed_at": timestamp,
        "account_values": _normalize_account_values(account),
        "positions": _normalize_positions(account),
        "working_orders": _normalize_working_orders(
            orders_payload,
            orders_error=orders_error,
        ),
    }


def _normalize_account_values(account: dict[str, Any]) -> dict[str, object]:
    balances = account.get("currentBalances")
    result: dict[str, object] = {
        "status": "UNAVAILABLE",
        "source_ref": "Schwab account.currentBalances",
        "source_refs": {},
        "unavailable_by_field": {},
        "unavailable_reasons": [],
    }
    for normalized_name, _, _ in _ACCOUNT_VALUE_FIELDS:
        result[normalized_name] = None

    if not isinstance(balances, dict):
        reason = (
            "Schwab account payload did not include currentBalances; current account values are unavailable."
        )
        result["unavailable_by_field"] = {
            normalized_name: reason for normalized_name, _, _ in _ACCOUNT_VALUE_FIELDS
        }
        result["unavailable_reasons"] = [reason]
        return result

    source_refs: dict[str, str] = {}
    unavailable_by_field: dict[str, str] = {}
    reasons: list[str] = []
    supplemental_reasons: list[str] = []
    for normalized_name, raw_keys, display_name in _ACCOUNT_VALUE_FIELDS:
        value, raw_key = _first_number_and_key(balances, raw_keys)
        alias_values = [
            (raw_key_name, _number(balances.get(raw_key_name)))
            for raw_key_name in raw_keys
            if _number(balances.get(raw_key_name)) is not None
        ]
        if alias_values and any(
            not math.isclose(float(alias_values[0][1]), float(alias_value), abs_tol=0.01)
            for _alias_key, alias_value in alias_values[1:]
        ):
            reason = (
                f"Schwab currentBalances reports conflicting aliases for {display_name}: "
                + ", ".join(f"{key}={alias_value:g}" for key, alias_value in alias_values)
                + "."
            )
            value = None
            raw_key = None
            unavailable_by_field[normalized_name] = reason
            if normalized_name == "maintenance_excess":
                supplemental_reasons.append(reason)
            else:
                reasons.append(reason)
        result[normalized_name] = value
        if raw_key is None and normalized_name not in unavailable_by_field:
            reason = (
                f"Schwab currentBalances did not report {display_name}; "
                f"{normalized_name} remains unavailable."
            )
            unavailable_by_field[normalized_name] = reason
            if normalized_name == "maintenance_excess":
                supplemental_reasons.append(reason)
            else:
                reasons.append(reason)
        elif raw_key is not None:
            source_refs[normalized_name] = f"Schwab account.currentBalances.{raw_key}"

    result.update(
        {
            "status": "PARTIAL" if reasons else "CURRENT",
            "source_refs": source_refs,
            "unavailable_by_field": unavailable_by_field,
            # maintenanceExcess is a calculation-specific field: its absence
            # blocks the margin buffer only when a positive margin debit makes
            # it necessary.  It must not turn otherwise complete account
            # evidence into a generic account-health failure.
            "unavailable_reasons": [*reasons, *supplemental_reasons],
        }
    )
    return result


def _normalize_positions(account: dict[str, Any]) -> dict[str, object]:
    raw_positions = account.get("positions")
    if not isinstance(raw_positions, list):
        reason = (
            "Schwab account payload did not include a positions list; current positions are unavailable."
        )
        return {
            "status": "UNAVAILABLE",
            "items": [],
            "source_ref": "Schwab account.positions",
            "stock_policy_row_set_complete": False,
            "stock_policy_unavailable_reasons": [reason],
            "option_row_set_complete": False,
            "option_unavailable_reasons": [reason],
            "unavailable_reasons": [reason],
        }

    items: list[dict[str, object]] = []
    section_reasons: list[str] = []
    stock_policy_row_set_reasons: list[str] = []
    option_row_set_reasons: list[str] = []
    for index, raw_position in enumerate(raw_positions):
        source_ref = f"Schwab account.positions[{index}]"
        if not isinstance(raw_position, dict):
            reason = f"{source_ref} was not an object; that position could not be normalized."
            section_reasons.append(reason)
            stock_policy_row_set_reasons.append(
                f"{reason} The unknown row could contain current-purpose stock exposure."
            )
            option_row_set_reasons.append(
                f"{reason} The unknown row could contain current-purpose option exposure."
            )
            continue
        item = _normalize_position(raw_position, source_ref)
        items.append(item)
        section_reasons.extend(str(value) for value in item["unavailable_reasons"])
        asset_types = _normalized_asset_types(item)
        if len(asset_types) != 1:
            option_row_set_reasons.append(
                f"{source_ref} has missing or conflicting asset identity, so the complete row set cannot prove whether it is an option position."
            )
        elif "OPTION" in asset_types[0]:
            option_row_set_reasons.extend(
                str(value) for value in item.get("option_unavailable_reasons", [])
            )
        elif asset_types[0] not in _REVIEWED_NON_OPTION_ASSET_TYPES:
            option_row_set_reasons.append(
                f"{source_ref} reports unreviewed asset type {asset_types[0]}, so the complete row set cannot prove it is not an option position."
            )

    return {
        "status": "INCOMPLETE" if section_reasons else "CURRENT",
        "items": items,
        "source_ref": "Schwab account.positions",
        "stock_policy_row_set_complete": not stock_policy_row_set_reasons,
        "stock_policy_unavailable_reasons": stock_policy_row_set_reasons,
        "option_row_set_complete": not option_row_set_reasons,
        "option_unavailable_reasons": list(dict.fromkeys(option_row_set_reasons)),
        "unavailable_reasons": section_reasons,
    }


def _normalize_position(row: dict[str, Any], source_ref: str) -> dict[str, object]:
    instrument = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}
    instrument_symbol = str(instrument.get("symbol") or "").strip().upper() or None
    row_symbol = str(row.get("symbol") or "").strip().upper() or None
    stock_policy_reasons: list[str] = []
    if (
        instrument_symbol is not None
        and row_symbol is not None
        and instrument_symbol != row_symbol
    ):
        stock_policy_reasons.append(
            f"{source_ref} reports conflicting symbols: instrument.symbol={instrument_symbol} "
            f"but symbol={row_symbol}."
        )
    symbol = instrument_symbol or row_symbol
    symbol_source = (
        f"{source_ref}.instrument.symbol"
        if instrument_symbol is not None
        else f"{source_ref}.symbol"
        if row_symbol is not None
        else None
    )
    instrument_asset_type = str(instrument.get("assetType") or "").strip().upper() or None
    row_asset_type = str(row.get("assetType") or "").strip().upper() or None
    if (
        instrument_asset_type is not None
        and row_asset_type is not None
        and instrument_asset_type != row_asset_type
    ):
        stock_policy_reasons.append(
            f"{source_ref} reports conflicting asset types: "
            f"instrument.assetType={instrument_asset_type} but assetType={row_asset_type}."
        )
    asset_type = instrument_asset_type or row_asset_type
    asset_type_source = (
        f"{source_ref}.instrument.assetType"
        if instrument_asset_type is not None
        else f"{source_ref}.assetType"
        if row_asset_type is not None
        else None
    )
    contract_multiplier, contract_multiplier_key = _first_number_and_key(
        instrument,
        ("multiplier", "contractMultiplier"),
    )
    is_option = asset_type is not None and "OPTION" in asset_type
    option_identity = _normalized_option_identity(
        instrument,
        row,
        symbol=symbol,
        source_ref=source_ref,
    ) if is_option else {
        "underlying_symbol": None,
        "option_type": None,
        "strike": None,
        "expiration": None,
        "delta": None,
        "underlying_price": None,
        "source_refs": {},
        "unavailable_reasons": [],
    }
    if (
        asset_type is not None
        and asset_type not in {"EQUITY", "STOCK"}
        and not is_option
    ):
        stock_policy_reasons.append(
            f"{source_ref} reports unreviewed asset type {asset_type}; the stock-policy "
            "normalizer cannot classify it as stock or a defined option position."
        )
    fallback_multiplier = contract_multiplier if is_option else 1.0
    fallback_reasons: list[str] = []

    long_quantity = _number(row.get("longQuantity"))
    short_quantity = _number(row.get("shortQuantity"))
    quantity_net = _number(row.get("quantity"))
    named_net = _number(row.get("netQuantity"))
    if (
        quantity_net is not None
        and named_net is not None
        and not math.isclose(quantity_net, named_net, rel_tol=0.0, abs_tol=1e-8)
    ):
        stock_policy_reasons.append(
            f"{source_ref} reports conflicting direct net quantity aliases: "
            f"quantity={quantity_net:g} but netQuantity={named_net:g}."
        )
    if quantity_net is not None:
        direct_net = quantity_net
        direct_net_key = "quantity"
    else:
        direct_net = named_net
        direct_net_key = "netQuantity" if named_net is not None else None
    if (
        direct_net is not None
        and long_quantity is not None
        and short_quantity is not None
        and not math.isclose(
            direct_net,
            long_quantity - short_quantity,
            rel_tol=0.0,
            abs_tol=1e-8,
        )
    ):
        stock_policy_reasons.append(
            f"{source_ref} reports conflicting net quantity representations: "
            f"{direct_net_key}={direct_net:g} but longQuantity-shortQuantity="
            f"{long_quantity - short_quantity:g}."
        )
    if (
        direct_net is not None
        and long_quantity is not None
        and short_quantity is None
        and direct_net > long_quantity + 1e-8
    ):
        stock_policy_reasons.append(
            f"{source_ref} reports {direct_net_key}={direct_net:g} and longQuantity="
            f"{long_quantity:g} with no shortQuantity; reconciling those facts would imply a "
            "negative shortQuantity."
        )
    if (
        direct_net is not None
        and short_quantity is not None
        and long_quantity is None
        and direct_net < -short_quantity - 1e-8
    ):
        stock_policy_reasons.append(
            f"{source_ref} reports {direct_net_key}={direct_net:g} and shortQuantity="
            f"{short_quantity:g} with no longQuantity; reconciling those facts would imply a "
            "negative longQuantity."
        )
    if direct_net is not None:
        net_quantity = direct_net
        net_source = f"{source_ref}.{direct_net_key}"
    elif long_quantity is not None and short_quantity is not None:
        net_quantity = long_quantity - short_quantity
        net_source = f"{source_ref}.longQuantity - {source_ref}.shortQuantity"
    else:
        net_quantity = None
        net_source = None

    settled_long = _number(row.get("settledLongQuantity"))
    settled_short = _number(row.get("settledShortQuantity"))
    direct_settled, direct_settled_key = _first_number_and_key(row, ("settledQuantity",))
    settled_conflict_reason: str | None = None
    if (
        direct_settled is not None
        and settled_long is not None
        and settled_short is not None
        and not math.isclose(
            direct_settled,
            settled_long - settled_short,
            rel_tol=0.0,
            abs_tol=1e-8,
        )
    ):
        settled_conflict_reason = (
            f"{source_ref} reports conflicting settled quantity representations: "
            f"settledQuantity={direct_settled:g} but settledLongQuantity-settledShortQuantity="
            f"{settled_long - settled_short:g}."
        )
    if direct_settled is not None:
        settled_quantity = direct_settled
        settled_source = f"{source_ref}.{direct_settled_key}"
    elif settled_long is not None and settled_short is not None:
        settled_quantity = settled_long - settled_short
        settled_source = f"{source_ref}.settledLongQuantity - {source_ref}.settledShortQuantity"
    else:
        settled_quantity = None
        settled_source = None

    market_value, market_value_key = _first_number_and_key(row, ("marketValue",))
    price, price_key = _first_number_and_key(
        row,
        ("marketPrice", "lastPrice", "currentPrice", "markPrice"),
    )
    if (
        price is None
        and market_value is not None
        and net_quantity not in (None, 0.0)
        and fallback_multiplier is not None
        and fallback_multiplier > 0
    ):
        price = abs(market_value / (net_quantity * fallback_multiplier))
        price_source = (
            f"abs({source_ref}.marketValue / (normalized net_quantity * "
            f"{fallback_multiplier:g} normalized contract multiplier))"
        )
    else:
        price_source = f"{source_ref}.{price_key}" if price_key is not None else None
    if price is None and market_value is not None and net_quantity not in (None, 0.0) and is_option:
        if contract_multiplier is None or contract_multiplier <= 0:
            fallback_reasons.append(
                f"{source_ref} cannot derive option price from market value without an explicit positive contract multiplier; no 100-share multiplier is assumed."
            )
    if (
        market_value is None
        and price is not None
        and net_quantity is not None
        and fallback_multiplier is not None
        and fallback_multiplier > 0
    ):
        market_value = price * net_quantity * fallback_multiplier
        market_value_source = (
            "normalized price * normalized net_quantity * "
            f"{fallback_multiplier:g} normalized contract multiplier from {source_ref}"
        )
    else:
        market_value_source = (
            f"{source_ref}.{market_value_key}" if market_value_key is not None else None
        )
    if market_value is None and price is not None and net_quantity is not None and is_option:
        if contract_multiplier is None or contract_multiplier <= 0:
            fallback_reasons.append(
                f"{source_ref} cannot derive option market value without an explicit positive contract multiplier; no 100-share multiplier is assumed."
            )

    if price is not None and market_value is not None and net_quantity is not None:
        if fallback_multiplier is None or fallback_multiplier <= 0:
            stock_policy_reasons.append(
                f"{source_ref} cannot reconcile option price and market value without an explicit positive contract multiplier."
            )
        else:
            expected_market_value = price * net_quantity * fallback_multiplier
            if not math.isclose(
                market_value,
                expected_market_value,
                rel_tol=1e-6,
                abs_tol=0.02,
            ):
                stock_policy_reasons.append(
                    f"{source_ref} reports conflicting valuation representations: marketValue={market_value:g} "
                    f"but price*net_quantity*contract_multiplier={expected_market_value:g}."
                )
    if asset_type in {"EQUITY", "STOCK"} and net_quantity not in (None, 0.0):
        if price is not None and price <= 0:
            stock_policy_reasons.append(
                f"{source_ref} reports nonpositive stock price {price:g} for nonzero net quantity "
                f"{net_quantity:g}; current stock exposure requires a positive observed position price."
            )
        if market_value is not None and market_value * net_quantity <= 0:
            stock_policy_reasons.append(
                f"{source_ref} reports stock market value {market_value:g} whose sign does not match "
                f"nonzero net quantity {net_quantity:g}; current stock exposure cannot be valued at zero "
                "or with the opposite sign."
            )

    cost_basis, cost_basis_key = _first_number_and_key(
        row,
        ("costBasis", "longCostValue", "shortCostValue"),
    )
    average_price, average_price_key = _first_number_and_key(
        row,
        ("averagePrice", "averageLongPrice", "averageShortPrice"),
    )
    if (
        cost_basis is None
        and average_price is not None
        and net_quantity is not None
        and fallback_multiplier is not None
        and fallback_multiplier > 0
    ):
        cost_basis = average_price * abs(net_quantity) * fallback_multiplier
        cost_basis_source = (
            f"{source_ref}.{average_price_key} * abs(normalized net_quantity) * "
            f"{fallback_multiplier:g} normalized contract multiplier"
        )
    else:
        cost_basis_source = f"{source_ref}.{cost_basis_key}" if cost_basis_key is not None else None
    if cost_basis is None and average_price is not None and net_quantity is not None and is_option:
        if contract_multiplier is None or contract_multiplier <= 0:
            fallback_reasons.append(
                f"{source_ref} cannot derive option cost basis without an explicit positive contract multiplier; no 100-share multiplier is assumed."
            )
    cost_basis_conflict_reason: str | None = None
    if (
        cost_basis is not None
        and average_price is not None
        and net_quantity is not None
        and fallback_multiplier is not None
        and fallback_multiplier > 0
    ):
        expected_cost_basis = average_price * abs(net_quantity) * fallback_multiplier
        if not math.isclose(
            cost_basis,
            expected_cost_basis,
            rel_tol=1e-6,
            abs_tol=0.02,
        ):
            cost_basis_conflict_reason = (
                f"{source_ref} reports conflicting cost-basis representations: costBasis={cost_basis:g} "
                f"but averagePrice*abs(net_quantity)*contract_multiplier={expected_cost_basis:g}."
            )

    unrealized_pnl, unrealized_source = _position_unrealized_pnl(row, source_ref)
    day_pnl, day_pnl_key = _first_number_and_key(
        row,
        ("currentDayProfitLoss", "dayProfitLoss"),
    )
    day_pnl_source = f"{source_ref}.{day_pnl_key}" if day_pnl_key is not None else None

    source_refs: dict[str, str] = {}
    for name, ref in (
        ("symbol", symbol_source),
        ("asset_type", asset_type_source),
        (
            "contract_multiplier",
            f"{source_ref}.instrument.{contract_multiplier_key}"
            if contract_multiplier_key is not None
            else None,
        ),
        ("long_quantity", f"{source_ref}.longQuantity" if long_quantity is not None else None),
        ("short_quantity", f"{source_ref}.shortQuantity" if short_quantity is not None else None),
        ("net_quantity", net_source),
        (
            "settled_long_quantity",
            f"{source_ref}.settledLongQuantity" if settled_long is not None else None,
        ),
        (
            "settled_short_quantity",
            f"{source_ref}.settledShortQuantity" if settled_short is not None else None,
        ),
        ("settled_quantity", settled_source),
        ("price", price_source),
        ("market_value", market_value_source),
        ("cost_basis", cost_basis_source),
        ("unrealized_pnl", unrealized_source),
        ("day_pnl", day_pnl_source),
    ):
        if ref is not None:
            source_refs[name] = ref
    option_source_refs = option_identity.get("source_refs")
    if isinstance(option_source_refs, dict):
        source_refs.update(
            {str(name): str(ref) for name, ref in option_source_refs.items() if str(ref).strip()}
        )

    reasons: list[str] = list(dict.fromkeys(fallback_reasons))
    if settled_conflict_reason:
        reasons.append(settled_conflict_reason)
    if cost_basis_conflict_reason:
        reasons.append(cost_basis_conflict_reason)
    for field_name, value in (
        ("symbol", symbol),
        ("asset type", asset_type),
        ("net quantity", net_quantity),
        ("settled quantity", settled_quantity),
        ("price", price),
        ("market value", market_value),
        ("cost basis", cost_basis),
        ("unrealized P/L", unrealized_pnl),
        ("day P/L", day_pnl),
    ):
        if value is None:
            reason = f"{source_ref} did not provide enough data to normalize {field_name}."
            reasons.append(reason)
            if field_name in {"symbol", "asset type", "net quantity", "market value"} or (
                field_name == "price" and net_quantity not in (None, 0.0)
            ):
                stock_policy_reasons.append(reason)
    for quantity_name, quantity_value in (
        ("longQuantity", long_quantity),
        ("shortQuantity", short_quantity),
    ):
        if quantity_value is not None and quantity_value < 0:
            stock_policy_reasons.append(
                f"{source_ref} reports negative {quantity_name}={quantity_value:g}."
            )
    reasons = list(dict.fromkeys([*reasons, *stock_policy_reasons]))
    stock_policy_reasons = list(dict.fromkeys(stock_policy_reasons))
    option_reasons = list(
        dict.fromkeys(
            str(value)
            for value in option_identity.get("unavailable_reasons", [])
            if str(value).strip()
        )
    )

    return {
        "status": "INCOMPLETE" if reasons else "CURRENT",
        "symbol": symbol,
        "asset_type": asset_type,
        "contract_multiplier": contract_multiplier,
        "underlying_symbol": option_identity.get("underlying_symbol"),
        "option_type": option_identity.get("option_type"),
        "strike": option_identity.get("strike"),
        "expiration": option_identity.get("expiration"),
        "delta": option_identity.get("delta"),
        "underlying_price": option_identity.get("underlying_price"),
        "long_quantity": long_quantity,
        "short_quantity": short_quantity,
        "net_quantity": net_quantity,
        "settled_long_quantity": settled_long,
        "settled_short_quantity": settled_short,
        "settled_quantity": settled_quantity,
        "price": _rounded(price, 8),
        "market_value": _rounded(market_value, 2),
        "cost_basis": _rounded(cost_basis, 2),
        "unrealized_pnl": _rounded(unrealized_pnl, 2),
        "day_pnl": _rounded(day_pnl, 2),
        "source_ref": source_ref,
        "source_refs": source_refs,
        "stock_policy_identity_candidates": {
            "symbols": list(
                dict.fromkeys(
                    value
                    for value in (instrument_symbol, row_symbol)
                    if value is not None
                )
            ),
            "asset_types": list(
                dict.fromkeys(
                    value
                    for value in (instrument_asset_type, row_asset_type)
                    if value is not None
                )
            ),
        },
        "stock_policy_fields_complete": not stock_policy_reasons,
        "stock_policy_unavailable_reasons": stock_policy_reasons,
        "option_fields_complete": not option_reasons if is_option else True,
        "option_unavailable_reasons": option_reasons,
        "unavailable_reasons": reasons,
    }


def _normalize_working_orders(
    orders_payload: Any,
    *,
    orders_error: str | None,
) -> dict[str, object]:
    base: dict[str, object] = {
        "status": "UNAVAILABLE",
        "items": [],
        "reserved_cash": None,
        "pending_buy_shares_by_symbol": None,
        "pending_sell_shares_by_symbol": None,
        "active_option_orders": [],
        "option_row_set_complete": False,
        "option_unavailable_reasons": [],
        "source_ref": "Schwab working orders",
        "unavailable_reasons": [],
    }
    if orders_error:
        base["unavailable_reasons"] = [
            f"Schwab working-order read failed; reserves and pending shares are unavailable: {orders_error}"
        ]
        return base
    if not isinstance(orders_payload, list):
        base["unavailable_reasons"] = [
            "Schwab working-order payload was missing or was not a list; reserves and pending shares are unavailable."
        ]
        return base

    items: list[dict[str, object]] = []
    active_option_orders: list[dict[str, object]] = []
    option_row_set_reasons: list[str] = []
    section_reasons: list[str] = []
    reserved_cash = 0.0
    pending_buys: dict[str, float] = {}
    pending_sells: dict[str, float] = {}
    for index, raw_order in enumerate(orders_payload):
        source_ref = f"Schwab working orders[{index}]"
        if not isinstance(raw_order, dict):
            section_reasons.append(
                f"{source_ref} was not an object; reserves and pending shares cannot be completed."
            )
            option_row_set_reasons.append(
                f"{source_ref} was not an object; the unknown active row could contain option legs."
            )
            continue
        raw_status = str(raw_order.get("status") or "").strip().upper()
        if raw_status in SCHWAB_TERMINAL_ORDER_STATUSES:
            continue
        raw_legs = raw_order.get("orderLegCollection")
        all_option_legs = (
            isinstance(raw_legs, list)
            and len(raw_legs) > 1
            and all(
                isinstance(leg, dict)
                and isinstance(leg.get("instrument"), dict)
                and "OPTION" in str(leg["instrument"].get("assetType") or "").upper()
                for leg in raw_legs
            )
        )
        item = (
            _normalize_multi_leg_option_order(raw_order, source_ref)
            if all_option_legs
            else _normalize_working_order(raw_order, source_ref)
        )
        items.append(item)
        asset_types = _normalized_asset_types(item)
        if len(asset_types) != 1:
            option_row_set_reasons.append(
                f"{source_ref} has missing or conflicting asset identity, so the active row set cannot prove whether it contains option legs."
            )
        elif "OPTION" in asset_types[0]:
            active_option_orders.append(item)
            option_row_set_reasons.extend(
                str(value) for value in item.get("option_unavailable_reasons", [])
            )
        elif asset_types[0] not in _REVIEWED_NON_OPTION_ASSET_TYPES:
            option_row_set_reasons.append(
                f"{source_ref} reports unreviewed asset type {asset_types[0]}, so the active row set cannot prove it contains no option legs."
            )
        item_reasons = [str(value) for value in item["unavailable_reasons"]]
        section_reasons.extend(item_reasons)
        if item_reasons:
            continue
        item_reserved_cash = item["reserved_cash"]
        if isinstance(item_reserved_cash, (int, float)):
            reserved_cash += float(item_reserved_cash)
        symbol = item["symbol"]
        pending_effect = item["pending_stock_share_effect"]
        if isinstance(symbol, str) and isinstance(pending_effect, (int, float)):
            if pending_effect > 0:
                pending_buys[symbol] = pending_buys.get(symbol, 0.0) + float(pending_effect)
            elif pending_effect < 0:
                pending_sells[symbol] = pending_sells.get(symbol, 0.0) + abs(float(pending_effect))

    if section_reasons:
        base.update(
            {
                "status": "INCOMPLETE",
                "items": items,
                "active_option_orders": active_option_orders,
                "option_row_set_complete": not option_row_set_reasons,
                "option_unavailable_reasons": list(dict.fromkeys(option_row_set_reasons)),
                "unavailable_reasons": section_reasons,
            }
        )
        return base

    base.update(
        {
            "status": "CURRENT",
            "items": items,
            "reserved_cash": round(reserved_cash, 2),
            "pending_buy_shares_by_symbol": {
                symbol: round(quantity, 8) for symbol, quantity in sorted(pending_buys.items())
            },
            "pending_sell_shares_by_symbol": {
                symbol: round(quantity, 8) for symbol, quantity in sorted(pending_sells.items())
            },
            "active_option_orders": active_option_orders,
            "option_row_set_complete": not option_row_set_reasons,
            "option_unavailable_reasons": list(dict.fromkeys(option_row_set_reasons)),
            "unavailable_reasons": [],
        }
    )
    return base


def _normalized_asset_types(item: dict[str, object]) -> tuple[str, ...]:
    """Return every reported normalized asset type so conflicts stay fail-closed."""

    identity_candidates = item.get("stock_policy_identity_candidates")
    if isinstance(identity_candidates, dict):
        candidates = identity_candidates.get("asset_types")
        if isinstance(candidates, list):
            normalized = tuple(
                dict.fromkeys(
                    str(value).strip().upper()
                    for value in candidates
                    if str(value).strip()
                )
            )
            if normalized:
                return normalized
    asset_type = str(item.get("asset_type") or "").strip().upper()
    return (asset_type,) if asset_type else ()


def _normalized_option_identity(
    instrument: dict[str, Any],
    row: dict[str, Any],
    *,
    symbol: str | None,
    source_ref: str,
) -> dict[str, object]:
    """Preserve exact option identity, including deterministic OCC decoding."""

    parsed: dict[str, object] = {}
    compact_symbol = re.sub(r"\s+", "", str(symbol or "").upper())
    match = re.fullmatch(r"([A-Z0-9.]{1,6})(\d{6})([CP])(\d{8})", compact_symbol)
    if match:
        expiration = match.group(2)
        parsed = {
            "underlying_symbol": match.group(1),
            "expiration": f"20{expiration[:2]}-{expiration[2:4]}-{expiration[4:6]}",
            "option_type": "CALL" if match.group(3) == "C" else "PUT",
            "strike": int(match.group(4)) / 1000.0,
        }

    def text_candidates(*pairs: tuple[object, str]) -> tuple[str | None, str | None, list[str]]:
        values = [(str(value).strip().upper(), ref) for value, ref in pairs if str(value or "").strip()]
        unique = list(dict.fromkeys(value for value, _ref in values))
        return (
            (unique[0], values[0][1], unique) if unique else (None, None, [])
        )

    underlying, underlying_ref, underlying_values = text_candidates(
        (instrument.get("underlyingSymbol"), f"{source_ref}.instrument.underlyingSymbol"),
        (row.get("underlyingSymbol"), f"{source_ref}.underlyingSymbol"),
        (parsed.get("underlying_symbol"), f"{source_ref}.instrument.symbol OCC identity"),
    )
    raw_type, option_type_ref, option_type_values = text_candidates(
        (instrument.get("putCall"), f"{source_ref}.instrument.putCall"),
        (instrument.get("optionType"), f"{source_ref}.instrument.optionType"),
        (row.get("putCall"), f"{source_ref}.putCall"),
        (parsed.get("option_type"), f"{source_ref}.instrument.symbol OCC identity"),
    )
    option_type = {"C": "CALL", "CALL": "CALL", "P": "PUT", "PUT": "PUT"}.get(raw_type or "")

    explicit_strike, strike_key = _first_number_and_key(instrument, ("strikePrice", "strike"))
    row_strike, row_strike_key = _first_number_and_key(row, ("strikePrice", "strike"))
    strike_values = [value for value in (explicit_strike, row_strike, _number(parsed.get("strike"))) if value is not None]
    strike = strike_values[0] if strike_values else None
    strike_ref = (
        f"{source_ref}.instrument.{strike_key}" if strike_key else
        f"{source_ref}.{row_strike_key}" if row_strike_key else
        f"{source_ref}.instrument.symbol OCC identity" if parsed.get("strike") is not None else None
    )
    raw_expiration, expiration_ref, expiration_values = text_candidates(
        (instrument.get("expirationDate"), f"{source_ref}.instrument.expirationDate"),
        (instrument.get("maturityDate"), f"{source_ref}.instrument.maturityDate"),
        (row.get("expirationDate"), f"{source_ref}.expirationDate"),
        (parsed.get("expiration"), f"{source_ref}.instrument.symbol OCC identity"),
    )
    expiration = raw_expiration[:10] if raw_expiration else None
    delta, delta_key = _first_number_and_key(row, ("delta", "optionDelta"))
    if delta is None:
        delta, delta_key = _first_number_and_key(instrument, ("delta", "optionDelta"))
        delta_ref = f"{source_ref}.instrument.{delta_key}" if delta_key else None
    else:
        delta_ref = f"{source_ref}.{delta_key}" if delta_key else None
    underlying_price, underlying_price_key = _first_number_and_key(
        row,
        ("underlyingPrice", "underlyingMark", "underlyingLastPrice"),
    )

    reasons: list[str] = []
    if len(underlying_values) > 1:
        reasons.append(f"{source_ref} reports conflicting option underlying identities: {underlying_values}.")
    canonical_types = {
        {"C": "CALL", "CALL": "CALL", "P": "PUT", "PUT": "PUT"}.get(value, value)
        for value in option_type_values
    }
    if len(canonical_types) > 1:
        reasons.append(f"{source_ref} reports conflicting option-type identities: {option_type_values}.")
    if strike_values and any(not math.isclose(strike_values[0], value, abs_tol=1e-8) for value in strike_values[1:]):
        reasons.append(f"{source_ref} reports conflicting option strikes: {strike_values}.")
    canonical_expirations = {value[:10] for value in expiration_values}
    if len(canonical_expirations) > 1:
        reasons.append(f"{source_ref} reports conflicting option expirations: {expiration_values}.")
    for field, value in (
        ("underlying", underlying),
        ("option type", option_type),
        ("strike", strike),
        ("expiration", expiration),
    ):
        if value in (None, ""):
            reasons.append(f"{source_ref} option identity is missing {field}.")
    if strike is not None and strike <= 0:
        reasons.append(f"{source_ref} option strike must be positive.")

    refs = {
        name: ref
        for name, ref in (
            ("underlying_symbol", underlying_ref),
            ("option_type", option_type_ref),
            ("strike", strike_ref),
            ("expiration", expiration_ref),
            ("delta", delta_ref),
            (
                "underlying_price",
                f"{source_ref}.{underlying_price_key}" if underlying_price_key else None,
            ),
        )
        if ref
    }
    return {
        "underlying_symbol": underlying,
        "option_type": option_type,
        "strike": _rounded(strike, 8),
        "expiration": expiration,
        "delta": _rounded(delta, 8),
        "underlying_price": _rounded(underlying_price, 8),
        "source_refs": refs,
        "unavailable_reasons": reasons,
    }


def _normalize_multi_leg_option_order(
    row: dict[str, Any],
    source_ref: str,
) -> dict[str, object]:
    """Normalize one complete active same-order option structure for post-fill loss."""

    order_id = str(row.get("orderId") or row.get("order_id") or "").strip() or None
    order_label = f"Schwab order {order_id}" if order_id else source_ref
    status = str(row.get("status") or "").strip().upper() or None
    order_type = str(row.get("orderType") or "").strip().upper() or None
    strategy_type = str(row.get("orderStrategyType") or "").strip().upper() or None
    complex_type = str(row.get("complexOrderStrategyType") or "").strip().upper() or None
    reasons: list[str] = []
    if order_id is None:
        reasons.append(f"{order_label} did not report orderId.")
    if status not in SCHWAB_CURRENT_ORDER_STATUSES:
        reasons.append(
            f"{order_label} status {status or 'missing'} is not a recognized current working status."
        )
    if strategy_type != "SINGLE":
        reasons.append(
            f"{order_label} uses orderStrategyType {strategy_type or 'missing'}; explicit SINGLE metadata is required."
        )
    children = row.get("childOrderStrategies")
    if children is not None and (not isinstance(children, list) or children):
        reasons.append(
            f"{order_label} contains malformed or nested child order strategies; post-fill option structure is unavailable."
        )
    if complex_type in {None, "", "NONE"}:
        reasons.append(
            f"{order_label} has multiple option legs but no explicit complexOrderStrategyType."
        )
    if order_type not in {"NET_DEBIT", "NET_CREDIT"}:
        reasons.append(
            f"{order_label} multi-leg orderType {order_type or 'missing'} does not supply an explicit net debit/credit limit."
        )

    quantity = _number(row.get("quantity"))
    filled = _number(row.get("filledQuantity"))
    remaining = _number(row.get("remainingQuantity"))
    if remaining is None and quantity is not None and filled is not None:
        remaining = quantity - filled
    if filled is None and quantity is not None and remaining is not None:
        filled = quantity - remaining
    if quantity is None or quantity <= 0 or filled is None or filled < 0 or remaining is None or remaining <= 0:
        reasons.append(
            f"{order_label} requires positive quantity/remainingQuantity and nonnegative filledQuantity."
        )
    elif not math.isclose(quantity, filled + remaining, abs_tol=1e-8):
        reasons.append(
            f"{order_label} quantity {quantity:g} does not equal filledQuantity {filled:g} plus remainingQuantity {remaining:g}."
        )
    price, price_key = _first_number_and_key(row, ("price", "limitPrice"))
    if price is None or price <= 0:
        reasons.append(f"{order_label} requires a positive explicit net debit/credit limit price.")

    raw_legs = row.get("orderLegCollection")
    legs: list[dict[str, object]] = []
    underlyings: set[str] = set()
    multipliers: set[float] = set()
    if not isinstance(raw_legs, list) or len(raw_legs) < 2:
        reasons.append(f"{order_label} does not contain a complete multi-leg option collection.")
        raw_legs = []
    for index, raw_leg in enumerate(raw_legs):
        if not isinstance(raw_leg, dict):
            reasons.append(f"{order_label} option leg {index + 1} is not structured.")
            continue
        instrument = raw_leg.get("instrument") if isinstance(raw_leg.get("instrument"), dict) else {}
        symbol = str(instrument.get("symbol") or "").strip().upper() or None
        instruction = str(raw_leg.get("instruction") or "").strip().upper() or None
        leg_quantity = _number(raw_leg.get("quantity"))
        multiplier, _multiplier_key = _first_number_and_key(
            instrument,
            ("multiplier", "contractMultiplier"),
        )
        identity = _normalized_option_identity(
            instrument,
            raw_leg,
            symbol=symbol,
            source_ref=f"{source_ref}.orderLegCollection[{index}]",
        )
        reasons.extend(str(value) for value in identity["unavailable_reasons"])
        if instruction not in {"BUY_TO_OPEN", "BUY_TO_CLOSE", "SELL_TO_OPEN", "SELL_TO_CLOSE"}:
            reasons.append(
                f"{order_label} option leg {index + 1} has unsupported instruction {instruction or 'missing'}."
            )
        if leg_quantity is None or leg_quantity <= 0 or quantity is None or quantity <= 0 or remaining is None:
            reasons.append(
                f"{order_label} option leg {index + 1} lacks a positive exact leg/order quantity."
            )
            remaining_leg_quantity = None
        else:
            remaining_leg_quantity = leg_quantity * remaining / quantity
        if multiplier is None or multiplier <= 0:
            reasons.append(
                f"{order_label} option leg {index + 1} lacks a positive explicit contract multiplier."
            )
        else:
            multipliers.add(multiplier)
        underlying = str(identity.get("underlying_symbol") or "").upper()
        if underlying:
            underlyings.add(underlying)
        legs.append(
            {
                "symbol": symbol,
                "underlying_symbol": identity.get("underlying_symbol"),
                "option_type": identity.get("option_type"),
                "strike": identity.get("strike"),
                "expiration": identity.get("expiration"),
                "instruction": instruction,
                "remaining_quantity": _rounded(remaining_leg_quantity, 8),
                "contract_multiplier": multiplier,
                "source_ref": f"{source_ref}.orderLegCollection[{index}]",
                "source_refs": identity.get("source_refs", {}),
            }
        )
    if len(underlyings) != 1:
        reasons.append(f"{order_label} option legs do not share one exact underlying.")
    if len(multipliers) != 1:
        reasons.append(
            f"{order_label} explicit net price cannot be converted to dollars because leg multipliers differ or are incomplete."
        )
    common_multiplier = next(iter(multipliers)) if len(multipliers) == 1 else None
    remaining_value = (
        remaining * price * common_multiplier
        if remaining is not None
        and price is not None
        and common_multiplier is not None
        else None
    )
    remaining_net_debit = _rounded(remaining_value, 2) if order_type == "NET_DEBIT" else None
    remaining_net_credit = _rounded(remaining_value, 2) if order_type == "NET_CREDIT" else None
    reserved_cash = remaining_net_debit if remaining_net_debit is not None else 0.0 if remaining_net_credit is not None else None
    reasons = list(dict.fromkeys(reasons))
    return {
        "status": "INCOMPLETE" if reasons else "CURRENT",
        "order_id": order_id,
        "order_status": status,
        "order_type": order_type,
        "order_strategy_type": strategy_type,
        "complex_order_strategy_type": complex_type,
        "symbol": next(iter(underlyings)) if len(underlyings) == 1 else None,
        "underlying_symbol": next(iter(underlyings)) if len(underlyings) == 1 else None,
        "asset_type": "OPTION",
        "instruction": "MULTI_LEG",
        "quantity": quantity,
        "filled_quantity": filled,
        "remaining_quantity": remaining,
        "limit_price": price,
        "contract_multiplier": common_multiplier,
        "reserved_cash": _rounded(reserved_cash, 2),
        "pending_stock_share_effect": None,
        "legs": legs,
        "remaining_net_debit": remaining_net_debit,
        "remaining_net_credit": remaining_net_credit,
        "source_ref": source_ref,
        "source_refs": {
            **({"order_id": f"{source_ref}.orderId"} if order_id is not None else {}),
            **({"limit_price": f"{source_ref}.{price_key}"} if price_key else {}),
        },
        "option_fields_complete": not reasons,
        "option_unavailable_reasons": reasons,
        "unavailable_reasons": reasons,
    }


def _normalize_working_order(row: dict[str, Any], source_ref: str) -> dict[str, object]:
    raw_order_id = str(row.get("orderId") or "").strip() or None
    normalized_order_id = str(row.get("order_id") or "").strip() or None
    order_id = raw_order_id or normalized_order_id
    order_id_source = (
        f"{source_ref}.orderId"
        if raw_order_id is not None
        else f"{source_ref}.order_id"
        if normalized_order_id is not None
        else None
    )
    order_label = f"Schwab order {order_id}" if order_id is not None else source_ref
    status = str(row.get("status") or "").strip().upper() or None
    order_type = str(row.get("orderType") or "").strip().upper() or None
    strategy_type = str(row.get("orderStrategyType") or "").strip().upper() or None
    complex_type = str(row.get("complexOrderStrategyType") or "").strip().upper() or None
    reasons: list[str] = []

    children = row.get("childOrderStrategies")
    if children is not None and not isinstance(children, list):
        reasons.append(
            f"{order_label} childOrderStrategies is present but is not a list; order shape is malformed."
        )
    elif isinstance(children, list) and children:
        reasons.append(
            f"{order_label} contains nested/child order strategies; reserves and pending shares are not normalized for nested/OCO orders."
        )
    if strategy_type != "SINGLE":
        reasons.append(
            f"{order_label} uses orderStrategyType {strategy_type or 'missing'}; explicit SINGLE metadata is required."
        )
    if complex_type != "NONE":
        reasons.append(
            f"{order_label} uses complexOrderStrategyType {complex_type or 'missing'}; explicit NONE metadata is required."
        )
    if order_id is None:
        reasons.append(
            f"{order_label} did not report orderId; the order cannot be safely deduplicated across Schwab history windows."
        )
    if status not in SCHWAB_CURRENT_ORDER_STATUSES:
        reasons.append(
            f"{order_label} status {status or 'missing'} is not a recognized current working status."
        )

    legs = row.get("orderLegCollection")
    if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict):
        leg_count = len(legs) if isinstance(legs, list) else "missing"
        reasons.append(
            f"{order_label} has {leg_count} order legs; exactly one equity or bounded-debit option leg is required."
        )
        leg: dict[str, Any] = {}
    else:
        leg = legs[0]

    instrument = leg.get("instrument") if isinstance(leg.get("instrument"), dict) else {}
    asset_type = str(instrument.get("assetType") or "").strip().upper() or None
    symbol = str(instrument.get("symbol") or "").strip().upper() or None
    instruction = str(leg.get("instruction") or "").strip().upper() or None

    row_quantity, row_quantity_key = _first_number_and_key(row, ("quantity",))
    leg_quantity, leg_quantity_key = _first_number_and_key(leg, ("quantity",))
    if (
        row_quantity is not None
        and leg_quantity is not None
        and not math.isclose(row_quantity, leg_quantity, rel_tol=0.0, abs_tol=1e-8)
    ):
        reasons.append(
            f"{order_label} reports conflicting quantities: quantity {row_quantity:g} but "
            f"orderLegCollection[0].quantity {leg_quantity:g}."
        )
    if row_quantity is not None:
        quantity = row_quantity
        quantity_source = f"{source_ref}.{row_quantity_key}"
    else:
        quantity = leg_quantity
        quantity_source = (
            f"{source_ref}.orderLegCollection[0].{leg_quantity_key}"
            if leg_quantity_key
            else None
        )
    filled_quantity, filled_key = _first_number_and_key(row, ("filledQuantity",))
    remaining_quantity, remaining_key = _first_number_and_key(row, ("remainingQuantity",))
    remaining_source: str | None = None
    filled_source = f"{source_ref}.{filled_key}" if filled_key else None
    if filled_quantity is None and quantity is not None and remaining_quantity is not None:
        filled_quantity = max(0.0, quantity - remaining_quantity)
        filled_source = f"{source_ref}.quantity - {source_ref}.remainingQuantity"
    if quantity is None and filled_quantity is not None and remaining_quantity is not None:
        quantity = filled_quantity + remaining_quantity
        quantity_source = f"{source_ref}.filledQuantity + {source_ref}.remainingQuantity"
    if remaining_quantity is not None:
        remaining_source = f"{source_ref}.{remaining_key}"
    elif quantity is not None and filled_quantity is not None:
        remaining_quantity = max(0.0, quantity - filled_quantity)
        remaining_source = f"{source_ref}.quantity - {source_ref}.filledQuantity"
    else:
        reasons.append(
            f"{order_label} did not report remainingQuantity or both quantity and filledQuantity; the unfilled amount is unavailable."
        )
    for field_name, value in (
        ("quantity", quantity),
        ("filledQuantity", filled_quantity),
        ("remainingQuantity", remaining_quantity),
    ):
        if value is not None and value < 0:
            reasons.append(f"{order_label} reported negative {field_name} {value:g}.")
    if (
        quantity is not None
        and filled_quantity is not None
        and remaining_quantity is not None
        and not math.isclose(
            quantity,
            filled_quantity + remaining_quantity,
            rel_tol=0.0,
            abs_tol=1e-8,
        )
    ):
        reasons.append(
            f"{order_label} has inconsistent fill quantities: quantity {quantity:g} does not equal "
            f"filledQuantity {filled_quantity:g} plus remainingQuantity {remaining_quantity:g}."
        )

    limit_price, price_key = _first_number_and_key(row, ("price", "limitPrice"))
    multiplier, multiplier_key = _first_number_and_key(
        instrument,
        ("multiplier", "contractMultiplier"),
    )
    option_identity = _normalized_option_identity(
        instrument,
        leg,
        symbol=symbol,
        source_ref=f"{source_ref}.orderLegCollection[0]",
    ) if asset_type == "OPTION" else {}
    option_reasons = [
        str(value)
        for value in option_identity.get("unavailable_reasons", [])
        if str(value).strip()
    ]
    if asset_type == "OPTION":
        if remaining_quantity is None or remaining_quantity <= 0:
            option_reasons.append(
                f"{order_label} option leg requires a positive exact remaining quantity."
            )
        if multiplier is None or multiplier <= 0:
            option_reasons.append(
                f"{order_label} option leg requires an explicit positive contract multiplier."
            )
    reserved_cash: float | None = None
    pending_effect: float | None = None

    if symbol is None:
        reasons.append(f"{order_label} did not report an instrument symbol.")
    if asset_type == "EQUITY":
        if instruction == "BUY":
            pending_effect = remaining_quantity
            if order_type not in {"LIMIT", "STOP_LIMIT"}:
                reasons.append(
                    f"{order_label} is an equity buy with {order_type or 'missing'} pricing; a bounded buy reserve requires LIMIT or STOP_LIMIT pricing."
                )
            elif limit_price is None or limit_price <= 0:
                reasons.append(
                    f"{order_label} did not report a positive limit price; its equity buy reserve is unavailable."
                )
            elif remaining_quantity is not None:
                reserved_cash = remaining_quantity * limit_price
        elif instruction == "SELL":
            pending_effect = -remaining_quantity if remaining_quantity is not None else None
            reserved_cash = 0.0
        elif instruction in {"BUY_TO_COVER", "SELL_SHORT"}:
            reasons.append(
                f"{order_label} uses margin instruction {instruction}; short/margin order effects are not bounded by this stock-policy normalizer."
            )
        else:
            reasons.append(
                f"{order_label} uses unsupported equity instruction {instruction or 'missing'}; expected BUY or SELL."
            )
    elif asset_type == "OPTION":
        if instruction in {"BUY_TO_OPEN", "BUY_TO_CLOSE"}:
            if order_type not in {"LIMIT", "STOP_LIMIT"}:
                reasons.append(
                    f"{order_label} is an option debit with {order_type or 'missing'} pricing; a bounded debit reserve requires LIMIT or STOP_LIMIT pricing."
                )
            elif limit_price is None or limit_price <= 0:
                reasons.append(
                    f"{order_label} did not report a positive limit price; its option debit reserve is unavailable."
                )
            if multiplier is None or multiplier <= 0:
                reasons.append(
                    f"{order_label} did not report a positive contract multiplier; no 100-share multiplier is assumed for possibly adjusted contracts."
                )
            if (
                remaining_quantity is not None
                and limit_price is not None
                and limit_price > 0
                and multiplier is not None
                and multiplier > 0
            ):
                reserved_cash = remaining_quantity * limit_price * multiplier
        elif instruction == "SELL_TO_CLOSE":
            reserved_cash = 0.0
        elif instruction == "SELL_TO_OPEN":
            # Cash reserve is zero here; boundedness belongs to the exact
            # post-fill option payoff gate, which rejects uncovered risk.
            reserved_cash = 0.0
        else:
            reasons.append(
                f"{order_label} uses unsupported option instruction {instruction or 'missing'}; only bounded debit buys and SELL_TO_CLOSE are normalized."
            )
    else:
        reasons.append(
            f"{order_label} asset type {asset_type or 'missing'} is unsupported; expected EQUITY or OPTION."
        )

    source_refs: dict[str, str] = {}
    for name, ref in (
        ("order_id", order_id_source),
        ("status", f"{source_ref}.status" if status is not None else None),
        ("order_type", f"{source_ref}.orderType" if order_type is not None else None),
        ("symbol", f"{source_ref}.orderLegCollection[0].instrument.symbol" if symbol else None),
        ("asset_type", f"{source_ref}.orderLegCollection[0].instrument.assetType" if asset_type else None),
        ("instruction", f"{source_ref}.orderLegCollection[0].instruction" if instruction else None),
        ("quantity", quantity_source),
        ("filled_quantity", filled_source),
        ("remaining_quantity", remaining_source),
        ("limit_price", f"{source_ref}.{price_key}" if price_key else None),
        (
            "contract_multiplier",
            f"{source_ref}.orderLegCollection[0].instrument.{multiplier_key}"
            if multiplier_key
            else None,
        ),
    ):
        if ref is not None:
            source_refs[name] = ref
    option_identity_refs = option_identity.get("source_refs")
    if isinstance(option_identity_refs, dict):
        source_refs.update(
            {str(name): str(ref) for name, ref in option_identity_refs.items() if str(ref).strip()}
        )

    normalized_option_legs = []
    remaining_net_debit = None
    remaining_net_credit = None
    if asset_type == "OPTION":
        normalized_option_legs = [
            {
                "symbol": symbol,
                "underlying_symbol": option_identity.get("underlying_symbol"),
                "option_type": option_identity.get("option_type"),
                "strike": option_identity.get("strike"),
                "expiration": option_identity.get("expiration"),
                "instruction": instruction,
                "remaining_quantity": remaining_quantity,
                "contract_multiplier": multiplier,
                "source_ref": f"{source_ref}.orderLegCollection[0]",
                "source_refs": option_identity_refs if isinstance(option_identity_refs, dict) else {},
            }
        ]
        executable_value = (
            remaining_quantity * limit_price * multiplier
            if remaining_quantity is not None
            and limit_price is not None
            and limit_price > 0
            and multiplier is not None
            and multiplier > 0
            else None
        )
        if instruction in {"BUY_TO_OPEN", "BUY_TO_CLOSE"}:
            remaining_net_debit = _rounded(executable_value, 2)
        elif instruction in {"SELL_TO_OPEN", "SELL_TO_CLOSE"}:
            remaining_net_credit = _rounded(executable_value, 2)

    return {
        "status": "INCOMPLETE" if reasons else "CURRENT",
        "order_id": order_id,
        "order_status": status,
        "order_type": order_type,
        "order_strategy_type": strategy_type,
        "complex_order_strategy_type": complex_type,
        "symbol": symbol,
        "asset_type": asset_type,
        "instruction": instruction,
        "quantity": quantity,
        "filled_quantity": filled_quantity,
        "remaining_quantity": remaining_quantity,
        "limit_price": limit_price,
        "contract_multiplier": multiplier,
        "underlying_symbol": option_identity.get("underlying_symbol"),
        "option_type": option_identity.get("option_type"),
        "strike": option_identity.get("strike"),
        "expiration": option_identity.get("expiration"),
        "legs": normalized_option_legs,
        "remaining_net_debit": remaining_net_debit,
        "remaining_net_credit": remaining_net_credit,
        "reserved_cash": _rounded(reserved_cash, 2),
        "pending_stock_share_effect": _rounded(pending_effect, 8),
        "source_ref": source_ref,
        "source_refs": source_refs,
        "option_fields_complete": not option_reasons if asset_type == "OPTION" else True,
        "option_unavailable_reasons": list(dict.fromkeys(option_reasons)),
        "unavailable_reasons": reasons,
    }


def _position_unrealized_pnl(
    row: dict[str, Any],
    source_ref: str,
) -> tuple[float | None, str | None]:
    direct_value, direct_key = _first_number_and_key(
        row,
        ("openProfitLoss", "unrealizedProfitLoss", "unrealizedPnl"),
    )
    if direct_value is not None:
        return direct_value, f"{source_ref}.{direct_key}"

    long_pnl = _number(row.get("longOpenProfitLoss"))
    short_pnl = _number(row.get("shortOpenProfitLoss"))
    long_quantity = _number(row.get("longQuantity"))
    short_quantity = _number(row.get("shortQuantity"))
    if long_pnl is not None and short_pnl is not None:
        return (
            long_pnl + short_pnl,
            f"{source_ref}.longOpenProfitLoss + {source_ref}.shortOpenProfitLoss",
        )
    if long_pnl is not None and short_quantity == 0:
        return long_pnl, f"{source_ref}.longOpenProfitLoss"
    if short_pnl is not None and long_quantity == 0:
        return short_pnl, f"{source_ref}.shortOpenProfitLoss"
    return None, None


def _securities_account(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected Schwab account response.")
    account = payload.get("securitiesAccount") or payload
    if not isinstance(account, dict):
        raise RuntimeError("Unexpected Schwab account response; missing securitiesAccount.")
    return account


def _first_number_and_key(
    row: dict[str, Any],
    keys: tuple[str, ...],
) -> tuple[float | None, str | None]:
    for key in keys:
        value = _number(row.get(key))
        if value is not None:
            return value, key
    return None, None


def _number(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _rounded(value: float | None, digits: int) -> float | None:
    return round(value, digits) if value is not None else None


def _iso_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
