from __future__ import annotations

from typing import Mapping


def order_confirmation_message(
    payload: Mapping[str, object],
    *,
    account_label: str = "",
    strategy_label: str = "",
    acknowledgment_copy: str = "",
) -> str:
    lines = ["Review this LIVE Schwab order before submitting:", ""]
    if account_label:
        lines.append(f"Account: {account_label}")
    if strategy_label:
        lines.append(f"Strategy: {strategy_label}")
    if account_label or strategy_label:
        lines.append("")
    lines.extend(_order_summary_lines(payload))
    if acknowledgment_copy:
        lines.extend(("", "By choosing Yes, you confirm:", acknowledgment_copy))
    lines.extend(("", "Submit this order to Schwab now?"))
    return "\n".join(lines)


def order_submitted_message(
    payload: Mapping[str, object],
    location: str | None,
) -> str:
    order_id = _order_id_from_location(location)
    return "\n".join(
        [
            "Schwab accepted the order.",
            "",
            f"Order ID: {order_id}",
            "",
            *_order_summary_lines(payload),
            "",
            f"Location: {location or '--'}",
        ]
    )


def order_replacement_confirmation_message(
    original_order_id: object,
    payload: Mapping[str, object],
) -> str:
    return "\n".join(
        [
            "Review this LIVE Schwab order replacement:",
            "",
            f"Original order ID: {str(original_order_id).strip() or '--'}",
            "Schwab will cancel the original order and create a replacement order.",
            "",
            *_order_summary_lines(payload),
            "",
            "Replace this order now?",
        ]
    )


def order_replaced_message(
    original_order_id: object,
    payload: Mapping[str, object],
    location: str | None,
) -> str:
    replacement_order_id = _order_id_from_location(location)
    return "\n".join(
        [
            "Schwab accepted the replacement.",
            "",
            f"Original order ID: {str(original_order_id).strip() or '--'}",
            f"Replacement order ID: {replacement_order_id}",
            "",
            *_order_summary_lines(payload),
            "",
            f"Location: {location or '--'}",
        ]
    )


def _order_summary_lines(payload: Mapping[str, object]) -> list[str]:
    order_type = _human_value(payload.get("orderType"))
    session = _human_value(payload.get("session"))
    duration = _human_value(payload.get("duration"))
    lines = [
        f"Order type: {order_type}",
        f"Session: {session}",
        f"Duration: {duration}",
    ]
    price = payload.get("price")
    stop_price = payload.get("stopPrice")
    if price:
        lines.append(f"Limit price: ${price}")
    if stop_price:
        lines.append(f"Stop price: ${stop_price}")
    special_instruction = payload.get("specialInstruction")
    if special_instruction:
        lines.append(f"Special instruction: {_human_value(special_instruction)}")
    estimated_value = _estimated_order_value(payload)
    if estimated_value:
        lines.append(f"Estimated value: {estimated_value}")
    lines.extend(("", "Legs:"))
    legs = payload.get("orderLegCollection")
    if not isinstance(legs, list) or not legs:
        lines.append("- --")
        return lines
    for leg in legs:
        if not isinstance(leg, Mapping):
            continue
        instrument = leg.get("instrument")
        instrument = instrument if isinstance(instrument, Mapping) else {}
        instruction = _human_value(leg.get("instruction"))
        quantity = str(leg.get("quantity") or "--")
        symbol = str(instrument.get("symbol") or "--")
        asset_type = str(instrument.get("assetType") or "--")
        unit = "contract(s)" if asset_type == "OPTION" else "share(s)"
        position_effect = leg.get("positionEffect")
        position_copy = f" · {_human_value(position_effect)}" if position_effect else ""
        lines.append(f"- {instruction} {quantity} {unit} {symbol}{position_copy}")
    return lines


def _estimated_order_value(payload: Mapping[str, object]) -> str:
    price = _to_float(payload.get("price"))
    if price is None:
        return ""
    legs = payload.get("orderLegCollection")
    if not isinstance(legs, list):
        return ""
    option_quantities: list[float] = []
    equity_quantities: list[float] = []
    for leg in legs:
        if not isinstance(leg, Mapping):
            continue
        quantity = _to_float(leg.get("quantity"))
        if quantity is None:
            continue
        instrument = leg.get("instrument")
        instrument = instrument if isinstance(instrument, Mapping) else {}
        if str(instrument.get("assetType") or "") == "OPTION":
            option_quantities.append(quantity)
        else:
            equity_quantities.append(quantity)
    if option_quantities:
        strategy_quantity = _to_float(payload.get("quantity")) or min(
            option_quantities
        )
        total = strategy_quantity * price * 100.0
    elif equity_quantities:
        total = sum(equity_quantities) * price
    else:
        return ""
    return f"~${total:,.2f}" if total > 0.0 else ""


def _order_id_from_location(location: str | None) -> str:
    if not location:
        return "--"
    return str(location).rstrip("/").rsplit("/", 1)[-1]


def _to_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _human_value(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "--"
    labels = {
        "DAY": "Day only",
        "GOOD_TILL_CANCEL": "Good until canceled",
        "NORMAL": "Normal session",
        "SEAMLESS": "Extended session",
        "NET_DEBIT": "Net debit",
        "NET_CREDIT": "Net credit",
        "STOP_LIMIT": "Stop limit",
        "BUY_TO_OPEN": "Buy to open",
        "SELL_TO_OPEN": "Sell to open",
        "BUY_TO_CLOSE": "Buy to close",
        "SELL_TO_CLOSE": "Sell to close",
    }
    return labels.get(text.upper(), text.replace("_", " ").title())


__all__ = [
    "order_confirmation_message",
    "order_replacement_confirmation_message",
    "order_replaced_message",
    "order_submitted_message",
]
