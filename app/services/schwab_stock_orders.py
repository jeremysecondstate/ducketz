from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from app.services.schwab_order_fields import (
    SCHWAB_EQUITY_ORDER_TYPE_CHOICES,
    schwab_equity_session_duration,
    schwab_equity_tif_from_api,
    schwab_equity_tif_requires_limit_order,
)
from app.services.schwab_policy_inputs import SCHWAB_TERMINAL_ORDER_STATUSES


SCHWAB_EQUITY_POSITION_EFFECT_CHOICES = ("AUTO", "OPENING", "CLOSING")
SCHWAB_EQUITY_SPECIAL_INSTRUCTION_CHOICES = (
    "NONE",
    "ALL_OR_NONE",
    "DO_NOT_REDUCE",
    "ALL_OR_NONE_DO_NOT_REDUCE",
)

_SCHWAB_EQUITY_INSTRUCTIONS = {
    "BUY",
    "SELL",
    "BUY_TO_COVER",
    "SELL_SHORT",
}
_POSITION_EFFECT_API_MAP = {
    "AUTO": "AUTOMATIC",
    "OPENING": "OPENING",
    "CLOSING": "CLOSING",
}


@dataclass(frozen=True)
class SchwabStockOrderEdit:
    order_id: str
    status: str
    symbol: str
    instruction: str
    order_type: str
    time_in_force: str
    position_effect: str
    quantity: int
    price: str
    stop_price: str
    special_instruction: str


def build_schwab_stock_order_payload(
    *,
    symbol: object,
    instruction: object,
    order_type: object,
    time_in_force: object,
    position_effect: object,
    quantity: object,
    price: object = "",
    stop_price: object = "",
    special_instruction: object = "NONE",
) -> dict[str, object]:
    clean_symbol = str(symbol or "").strip().upper()
    if not clean_symbol:
        raise ValueError("Stock / ETF symbol is required.")

    clean_instruction = str(instruction or "").strip().upper()
    if clean_instruction not in _SCHWAB_EQUITY_INSTRUCTIONS:
        raise ValueError(f"Unsupported Schwab equity instruction: {clean_instruction or '--'}.")

    clean_order_type = str(order_type or "").strip().upper()
    if clean_order_type not in SCHWAB_EQUITY_ORDER_TYPE_CHOICES:
        raise ValueError(f"Unsupported Schwab equity order type: {clean_order_type or '--'}.")

    clean_tif = str(time_in_force or "").strip().upper()
    session, duration = schwab_equity_session_duration(clean_tif)
    if schwab_equity_tif_requires_limit_order(clean_tif) and clean_order_type != "LIMIT":
        raise ValueError("Extended-hours Schwab equity orders must use the LIMIT order type.")

    clean_position_effect = str(position_effect or "AUTO").strip().upper()
    try:
        api_position_effect = _POSITION_EFFECT_API_MAP[clean_position_effect]
    except KeyError:
        raise ValueError(
            f"Unsupported Schwab equity position effect: {clean_position_effect or '--'}."
        ) from None

    clean_special_instruction = str(special_instruction or "NONE").strip().upper()
    if clean_special_instruction not in SCHWAB_EQUITY_SPECIAL_INSTRUCTION_CHOICES:
        raise ValueError(
            f"Unsupported Schwab special instruction: {clean_special_instruction or '--'}."
        )

    leg: dict[str, object] = {
        "instruction": clean_instruction,
        "positionEffect": api_position_effect,
        "quantity": _positive_whole_number(quantity, "Quantity"),
        "instrument": {
            "symbol": clean_symbol,
            "assetType": "EQUITY",
        },
    }
    payload: dict[str, object] = {
        "orderType": clean_order_type,
        "session": session,
        "duration": duration,
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [leg],
    }

    if clean_special_instruction != "NONE":
        payload["specialInstruction"] = clean_special_instruction
    if clean_order_type in {"LIMIT", "STOP_LIMIT"}:
        payload["price"] = _positive_price(price, "Limit price")
    if clean_order_type in {"STOP", "STOP_LIMIT"}:
        payload["stopPrice"] = _positive_price(stop_price, "Stop price")

    return payload


def schwab_stock_order_edit(order: Mapping[str, object]) -> SchwabStockOrderEdit:
    order_id = str(order.get("orderId") or "").strip()
    if not order_id:
        raise ValueError("The selected Schwab order has no Order ID.")

    status = str(order.get("status") or "").strip().upper()
    if status in SCHWAB_TERMINAL_ORDER_STATUSES:
        raise ValueError(f"Order {order_id} is already {status.replace('_', ' ').lower()}.")

    if _optional_bool(order.get("editable")) is False:
        raise ValueError(f"Schwab marks order {order_id} as not editable.")

    filled_quantity = _optional_number(order.get("filledQuantity")) or 0.0
    if filled_quantity > 0:
        raise ValueError(
            f"Order {order_id} is partially filled. Modify partially filled orders in Schwab or thinkorswim."
        )

    strategy = str(order.get("orderStrategyType") or "SINGLE").strip().upper()
    if strategy != "SINGLE" or _nonempty_list(order.get("childOrderStrategies")):
        raise ValueError("Only single, non-conditional Stock/ETF orders can be modified here.")

    complex_strategy = str(order.get("complexOrderStrategyType") or "NONE").strip().upper()
    if complex_strategy not in {"", "NONE"}:
        raise ValueError("Complex Schwab orders cannot be modified with the Stock/ETF editor.")

    legs = order.get("orderLegCollection")
    if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], Mapping):
        raise ValueError("Only single-leg Stock/ETF orders can be modified here.")

    leg = legs[0]
    instrument = leg.get("instrument")
    if not isinstance(instrument, Mapping):
        raise ValueError("The selected Schwab order has no instrument details.")

    asset_type = str(instrument.get("assetType") or "").strip().upper()
    if asset_type not in {"EQUITY", "ETF"}:
        raise ValueError("Select a Stock/ETF order; option orders use their dedicated workflow.")

    symbol = str(instrument.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("The selected Schwab order has no symbol.")

    instruction = str(leg.get("instruction") or "").strip().upper()
    if instruction not in _SCHWAB_EQUITY_INSTRUCTIONS:
        raise ValueError(f"Unsupported Schwab equity instruction: {instruction or '--'}.")

    order_type = str(order.get("orderType") or "").strip().upper()
    if order_type not in SCHWAB_EQUITY_ORDER_TYPE_CHOICES:
        raise ValueError(
            f"The Stock/ETF editor does not support {order_type.replace('_', ' ') or '--'} orders."
        )

    raw_position_effect = str(leg.get("positionEffect") or "AUTOMATIC").strip().upper()
    position_effect = "AUTO" if raw_position_effect in {"", "AUTO", "AUTOMATIC"} else raw_position_effect
    if position_effect not in SCHWAB_EQUITY_POSITION_EFFECT_CHOICES:
        raise ValueError(f"Unsupported Schwab position effect: {raw_position_effect or '--'}.")

    raw_special_instruction = str(order.get("specialInstruction") or "NONE").strip().upper()
    special_instruction = raw_special_instruction or "NONE"
    if special_instruction not in SCHWAB_EQUITY_SPECIAL_INSTRUCTION_CHOICES:
        raise ValueError(f"Unsupported Schwab special instruction: {special_instruction}.")

    return SchwabStockOrderEdit(
        order_id=order_id,
        status=status,
        symbol=symbol,
        instruction=instruction,
        order_type=order_type,
        time_in_force=schwab_equity_tif_from_api(order.get("session"), order.get("duration")),
        position_effect=position_effect,
        quantity=_positive_whole_number(leg.get("quantity"), "Quantity"),
        price=_display_number(order.get("price")),
        stop_price=_display_number(order.get("stopPrice")),
        special_instruction=special_instruction,
    )


def build_schwab_stock_replacement_payload(
    original: SchwabStockOrderEdit,
    *,
    order_type: object,
    time_in_force: object,
    position_effect: object,
    quantity: object,
    price: object,
    stop_price: object,
    special_instruction: object,
) -> dict[str, object]:
    return build_schwab_stock_order_payload(
        symbol=original.symbol,
        instruction=original.instruction,
        order_type=order_type,
        time_in_force=time_in_force,
        position_effect=position_effect,
        quantity=quantity,
        price=price,
        stop_price=stop_price,
        special_instruction=special_instruction,
    )


def _positive_whole_number(value: object, label: str) -> int:
    number = _optional_number(value)
    if number is None or number <= 0 or not number.is_integer():
        raise ValueError(f"{label} must be a positive whole number.")
    return int(number)


def _positive_price(value: object, label: str) -> str:
    number = _optional_number(value)
    if number is None or number <= 0:
        raise ValueError(f"{label} must be a positive number.")
    return f"{number:.2f}"


def _optional_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
    return None


def _nonempty_list(value: object) -> bool:
    return isinstance(value, list) and bool(value)


def _display_number(value: object) -> str:
    number = _optional_number(value)
    if number is None or number == 0:
        return ""
    return f"{number:.8f}".rstrip("0").rstrip(".")


__all__ = [
    "SCHWAB_EQUITY_POSITION_EFFECT_CHOICES",
    "SCHWAB_EQUITY_SPECIAL_INSTRUCTION_CHOICES",
    "SchwabStockOrderEdit",
    "build_schwab_stock_order_payload",
    "build_schwab_stock_replacement_payload",
    "schwab_stock_order_edit",
]
