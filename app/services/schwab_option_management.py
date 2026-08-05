from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
from functools import reduce
from math import gcd

from app.models.option_management import (
    ClosingOrderDraft,
    ClosingOrderLeg,
    ClosingOrderSubmission,
    ManagedOptionOrder,
    ManagedOrderLeg,
    OptionPositionBook,
    OptionPositionLeg,
    OptionPositionSummary,
)
from app.models.portfolio import PortfolioSnapshot
from app.services.schwab_policy_inputs import SCHWAB_TERMINAL_ORDER_STATUSES
from app.services.schwab_strategy_orders import DAY_ONLY, GOOD_UNTIL_CANCELED


LOCAL_POSITION_MARK_SOURCE = "Current Schwab option quote marks (local estimate)"


def enrich_option_position_quotes(
    account_facts: dict[str, object],
    quotes: Mapping[str, Mapping[str, object]],
    *,
    observed_at: datetime,
) -> int:
    positions = account_facts.get("positions")
    if not isinstance(positions, dict):
        return 0
    rows = positions.get("items")
    if not isinstance(rows, list):
        return 0
    enriched = 0
    for row in rows:
        if not isinstance(row, dict) or "OPTION" not in str(row.get("asset_type") or "").upper():
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        quote = quotes.get(symbol)
        if not isinstance(quote, Mapping):
            continue
        bid = _first_number(quote, ("bidPrice", "bid"))
        ask = _first_number(quote, ("askPrice", "ask"))
        mark = _first_number(quote, ("mark", "markPrice"))
        if mark is None and bid is not None and ask is not None:
            mark = (bid + ask) / 2.0
        quote_at = _timestamp_from_milliseconds(quote.get("quoteTime")) or observed_at
        row["bid"] = _rounded(bid, 8)
        row["ask"] = _rounded(ask, 8)
        row["price"] = _rounded(mark, 8)
        row["delta"] = _rounded(_finite_number(quote.get("delta")), 8)
        row["theta"] = _rounded(_finite_number(quote.get("theta")), 8)
        row["quote_observed_at"] = quote_at.isoformat()
        source_refs = row.get("source_refs")
        if not isinstance(source_refs, dict):
            source_refs = {}
            row["source_refs"] = source_refs
        source_refs.update(
            {
                "bid": f"Schwab quote[{symbol}].bidPrice",
                "ask": f"Schwab quote[{symbol}].askPrice",
                "price": f"Schwab quote[{symbol}].mark",
                "delta": f"Schwab quote[{symbol}].delta",
                "theta": f"Schwab quote[{symbol}].theta",
                "quote_observed_at": f"Schwab quote[{symbol}].quoteTime",
            }
        )
        if _finite_number(row.get("contract_multiplier")) is None:
            multiplier = _reconciled_contract_multiplier(row, mark)
            if multiplier is not None:
                row["contract_multiplier"] = multiplier
                source_refs["contract_multiplier"] = (
                    "abs(normalized market_value / (normalized net_quantity * Schwab quote mark))"
                )
        enriched += 1
    positions["option_quote_status"] = "CURRENT" if enriched else "UNAVAILABLE"
    positions["option_quotes_observed_at"] = observed_at.isoformat()
    return enriched


def option_position_book(snapshot: PortfolioSnapshot) -> OptionPositionBook:
    facts = snapshot.account_facts if isinstance(snapshot.account_facts, Mapping) else {}
    positions = facts.get("positions")
    section = positions if isinstance(positions, Mapping) else {}
    items = section.get("items")
    rows = items if _is_sequence(items) else ()
    observed_at = snapshot.synced_at or _parse_datetime(facts.get("observed_at"))

    legs: list[OptionPositionLeg] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        asset_type = str(row.get("asset_type") or "").strip().upper()
        if "OPTION" not in asset_type:
            continue
        quantity = _finite_number(row.get("net_quantity"))
        if quantity is not None and math.isclose(quantity, 0.0, abs_tol=1e-9):
            continue
        leg = _position_leg(snapshot.account_label, observed_at, row)
        legs.append(leg)

    duplicate_symbols = {
        symbol for symbol, count in Counter(leg.symbol for leg in legs if leg.symbol).items() if count > 1
    }
    if duplicate_symbols:
        legs = [
            replace(
                leg,
                close_disabled_reason=(
                    f"Schwab returned more than one open row for {leg.symbol}; "
                    "the exact close quantity is ambiguous."
                    if leg.symbol in duplicate_symbols
                    else leg.close_disabled_reason
                ),
            )
            for leg in legs
        ]

    legs.sort(key=lambda leg: (leg.underlying_symbol, leg.expiration, leg.strike, leg.option_type, leg.symbol))
    account_values = facts.get("account_values")
    values = account_values if isinstance(account_values, Mapping) else {}
    available_funds = _first_number(
        values,
        ("available_funds", "cash_available_for_withdrawal", "cash_balance"),
    )
    buying_power = _first_number(values, ("buying_power",))
    summary = OptionPositionSummary(
        net_market_value=_complete_sum(leg.market_value for leg in legs),
        unrealized_pnl=_complete_sum(leg.unrealized_pnl for leg in legs),
        day_pnl=_complete_sum(leg.day_pnl for leg in legs),
        theta_per_day=_complete_sum(
            None
            if leg.theta is None
            else leg.theta * leg.contract_multiplier * leg.net_quantity
            for leg in legs
        ),
        available_funds=available_funds,
        buying_power=buying_power,
    )
    if "option_unavailable_reasons" in section:
        reasons = section.get("option_unavailable_reasons") or ()
    else:
        reasons = section.get("unavailable_reasons") or ()
    quote_reasons = section.get("option_quote_unavailable_reasons") or ()
    return OptionPositionBook(
        account_label=snapshot.account_label,
        observed_at=observed_at,
        status=_option_position_status(section),
        legs=tuple(legs),
        summary=summary,
        unavailable_reasons=tuple(
            dict.fromkeys(
                str(reason)
                for reason in (*reasons, *quote_reasons)
                if str(reason).strip()
            )
        ),
    )


def filter_option_positions(
    book: OptionPositionBook,
    *,
    symbol: str | None = None,
    expiration: str | None = None,
) -> tuple[OptionPositionLeg, ...]:
    clean_symbol = str(symbol or "").strip().upper()
    clean_expiration = str(expiration or "").strip()
    return tuple(
        leg
        for leg in book.legs
        if (not clean_symbol or leg.underlying_symbol == clean_symbol)
        and (not clean_expiration or leg.expiration == clean_expiration)
    )


def build_closing_order_draft(
    book: OptionPositionBook,
    selected_symbols: Iterable[str],
    *,
    duration: str = DAY_ONLY,
    limit_price: object | None = None,
) -> ClosingOrderDraft:
    symbols = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in selected_symbols if str(symbol).strip()))
    if not symbols:
        raise ValueError("Select at least one exact option leg to close.")
    by_symbol = {leg.symbol: leg for leg in book.legs}
    missing = [symbol for symbol in symbols if symbol not in by_symbol]
    if missing:
        raise ValueError("Selected option position is no longer available: " + ", ".join(missing))
    selected = tuple(by_symbol[symbol] for symbol in symbols)
    disabled = [leg.close_disabled_reason for leg in selected if leg.close_disabled_reason]
    if disabled:
        raise ValueError(str(disabled[0]))
    underlyings = {leg.underlying_symbol for leg in selected}
    if len(underlyings) != 1:
        raise ValueError(
            "Selected legs must share one underlying symbol to be sent as a single custom option order."
        )

    quantities = tuple(_whole_quantity(leg.absolute_quantity, f"{leg.symbol} close quantity") for leg in selected)
    order_quantity = reduce(gcd, quantities)
    multipliers = {round(leg.contract_multiplier, 8) for leg in selected}
    if len(multipliers) != 1:
        raise ValueError("Selected legs have different contract multipliers and cannot be priced as one net order.")
    common_multiplier = next(iter(multipliers))

    cash_per_package = 0.0
    close_legs: list[ClosingOrderLeg] = []
    for leg, total_quantity in zip(selected, quantities, strict=True):
        if leg.mark is None:
            raise ValueError(f"{leg.symbol} does not have a current position mark.")
        ratio = total_quantity // order_quantity
        cash_sign = 1.0 if leg.close_instruction == "SELL_TO_CLOSE" else -1.0
        cash_per_package += cash_sign * leg.mark * ratio
        close_legs.append(
            ClosingOrderLeg(
                symbol=leg.symbol,
                display_name=_position_display_name(leg),
                underlying_symbol=leg.underlying_symbol,
                expiration=leg.expiration,
                strike=leg.strike,
                option_type=leg.option_type,
                instruction=leg.close_instruction,
                quantity=total_quantity,
                ratio_quantity=ratio,
                before_quantity=leg.net_quantity,
                after_quantity=0.0,
                bid=leg.bid,
                ask=leg.ask,
                mark=leg.mark,
                contract_multiplier=leg.contract_multiplier,
                quote_observed_at=leg.quote_observed_at,
            )
        )

    if len(close_legs) == 1:
        api_order_type = "LIMIT"
        suggested_price = max(close_legs[0].mark, 0.01)
        direction = 1.0 if close_legs[0].instruction == "SELL_TO_CLOSE" else -1.0
        complex_type = None
    else:
        if math.isclose(cash_per_package, 0.0, abs_tol=0.005):
            raise ValueError(
                "The selected legs have a zero net position mark, so debit versus credit cannot be determined safely."
            )
        api_order_type = "NET_CREDIT" if cash_per_package > 0 else "NET_DEBIT"
        suggested_price = abs(cash_per_package)
        direction = 1.0 if api_order_type == "NET_CREDIT" else -1.0
        complex_type = "CUSTOM"

    chosen_price = suggested_price if limit_price in (None, "") else _positive_price(limit_price)
    if duration not in {DAY_ONLY, GOOD_UNTIL_CANCELED}:
        raise ValueError(f"Unsupported order duration: {duration or 'missing'}")
    normalized_price = round(chosen_price, 2)
    estimated_cash = direction * normalized_price * common_multiplier * order_quantity
    warnings = [
        "Price and proceeds are local estimates from position marks; the current Schwab service has no wired preview call.",
        "Exact OCC symbols and quantities will be re-read immediately before placement.",
    ]
    if len(close_legs) > 1:
        warnings.append("Selected legs will be sent together as one custom net order; partial fills may be possible.")
    return ClosingOrderDraft(
        account_label=book.account_label,
        reviewed_position_at=book.observed_at,
        oldest_quote_at=min(
            (leg.quote_observed_at for leg in close_legs if leg.quote_observed_at is not None),
            default=None,
        ),
        scope_label="Entire position" if len(close_legs) == 1 else f"{len(close_legs)} selected exact legs",
        legs=tuple(close_legs),
        api_order_type=api_order_type,
        complex_order_strategy_type=complex_type,
        order_quantity=order_quantity,
        limit_price=normalized_price,
        duration=duration,
        estimated_cash_effect=round(estimated_cash, 2),
        price_source=LOCAL_POSITION_MARK_SOURCE,
        warnings=tuple(warnings),
    )


def build_closing_order_payload(draft: ClosingOrderDraft) -> dict[str, object]:
    price = _positive_price(draft.limit_price)
    payload: dict[str, object] = {
        "orderType": draft.api_order_type,
        "session": "NORMAL",
        "duration": "DAY" if draft.duration == DAY_ONLY else "GOOD_TILL_CANCEL",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {
                "instruction": leg.instruction,
                "quantity": leg.quantity,
                "instrument": {"symbol": leg.symbol, "assetType": "OPTION"},
            }
            for leg in draft.legs
        ],
        "price": round(price, 2),
    }
    if len(draft.legs) > 1:
        payload["complexOrderStrategyType"] = draft.complex_order_strategy_type or "CUSTOM"
        payload["quantity"] = draft.order_quantity
    return payload


def validate_closing_position_drift(
    draft: ClosingOrderDraft,
    latest_snapshot: PortfolioSnapshot,
) -> None:
    latest = option_position_book(latest_snapshot)
    if latest.account_label != draft.account_label:
        raise ValueError("The current Schwab account no longer matches the reviewed account; review the order again.")
    if latest.status != "CURRENT":
        raise ValueError(
            "The current Schwab option row-set is unavailable or incomplete; "
            "the closing order was not submitted."
        )
    by_symbol = {leg.symbol: leg for leg in latest.legs}
    for reviewed in draft.legs:
        current = by_symbol.get(reviewed.symbol)
        if current is None:
            raise ValueError(f"Position drift: {reviewed.symbol} is no longer held. Review the order again.")
        if current.close_disabled_reason:
            raise ValueError(f"Position drift: {current.close_disabled_reason}")
        if not math.isclose(current.net_quantity, reviewed.before_quantity, rel_tol=0.0, abs_tol=1e-8):
            raise ValueError(
                f"Position drift: {reviewed.symbol} changed from {reviewed.before_quantity:g} "
                f"to {current.net_quantity:g}. Review the order again."
            )


def submit_validated_closing_order(
    draft: ClosingOrderDraft,
    *,
    snapshot_loader: Callable[[], PortfolioSnapshot],
    session_factory: Callable[[], object],
) -> ClosingOrderSubmission:
    latest_snapshot = snapshot_loader()
    validate_closing_position_drift(draft, latest_snapshot)
    payload = build_closing_order_payload(draft)
    session = session_factory()
    submit = getattr(session, "submit_order", None)
    if not callable(submit):
        raise TypeError("Schwab session does not provide submit_order.")
    location = submit(payload)
    return ClosingOrderSubmission(payload=payload, location=location)


def option_orders_from_snapshot(snapshot: PortfolioSnapshot) -> tuple[ManagedOptionOrder, ...]:
    facts = snapshot.account_facts if isinstance(snapshot.account_facts, Mapping) else {}
    working = facts.get("working_orders")
    section = working if isinstance(working, Mapping) else {}
    items = section.get("active_option_orders")
    if not _is_sequence(items):
        items = ()
    return _option_orders_from_rows(items, normalized=True)


def option_orders_from_payload(payload: object) -> tuple[ManagedOptionOrder, ...]:
    return _option_orders_from_rows(payload, normalized=False)


def _option_orders_from_rows(rows: object, *, normalized: bool) -> tuple[ManagedOptionOrder, ...]:
    if not _is_sequence(rows):
        return ()
    orders: list[ManagedOptionOrder] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        order = _managed_order(row, normalized=normalized)
        if order is not None:
            orders.append(order)
    orders.sort(key=lambda item: (item.entered_time, item.order_id), reverse=True)
    return tuple(orders)


def _managed_order(row: Mapping[str, object], *, normalized: bool) -> ManagedOptionOrder | None:
    if normalized and "OPTION" not in str(row.get("asset_type") or "").upper():
        return None
    raw_legs = row.get("legs") if normalized else row.get("orderLegCollection")
    if not _is_sequence(raw_legs):
        return None
    legs: list[ManagedOrderLeg] = []
    for raw_leg in raw_legs:
        if not isinstance(raw_leg, Mapping):
            continue
        instrument = raw_leg.get("instrument")
        instrument_row = instrument if isinstance(instrument, Mapping) else {}
        asset_type = str(
            row.get("asset_type")
            if normalized
            else instrument_row.get("assetType") or raw_leg.get("asset_type") or ""
        ).upper()
        if "OPTION" not in asset_type:
            continue
        symbol = str(raw_leg.get("symbol") or instrument_row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        legs.append(
            ManagedOrderLeg(
                symbol=symbol,
                instruction=str(raw_leg.get("instruction") or "").strip().upper(),
                quantity=_finite_number(
                    raw_leg.get("remaining_quantity") if normalized else raw_leg.get("quantity")
                ),
            )
        )
    if not legs:
        return None
    order_id = str(_order_value(row, normalized, "order_id", "orderId") or "").strip()
    status = str(_order_value(row, normalized, "order_status", "status") or "").strip().upper()
    terminal = status in SCHWAB_TERMINAL_ORDER_STATUSES
    can_cancel = bool(order_id and not terminal)
    reason = None
    if not order_id:
        reason = "Order ID is unavailable."
    elif terminal:
        reason = f"{status.replace('_', ' ').title()} orders cannot be canceled."
    return ManagedOptionOrder(
        order_id=order_id,
        status=status or "UNKNOWN",
        entered_time=str(_order_value(row, normalized, "entered_time", "enteredTime") or "").strip(),
        order_type=str(_order_value(row, normalized, "order_type", "orderType") or "").strip().upper(),
        complex_order_strategy_type=str(
            _order_value(
                row,
                normalized,
                "complex_order_strategy_type",
                "complexOrderStrategyType",
            )
            or ""
        ).strip().upper(),
        duration=str(row.get("duration") or "").strip().upper(),
        remaining_quantity=_finite_number(
            _order_value(row, normalized, "remaining_quantity", "remainingQuantity")
        ),
        limit_price=_finite_number(
            row.get("limit_price")
            if normalized
            else row.get("price") or row.get("limitPrice")
        ),
        stop_price=_finite_number(_order_value(row, normalized, "stop_price", "stopPrice")),
        legs=tuple(legs),
        can_cancel=can_cancel,
        cancel_disabled_reason=reason,
    )


def _position_leg(
    account_label: str,
    observed_at: datetime | None,
    row: Mapping[str, object],
) -> OptionPositionLeg:
    symbol = str(row.get("symbol") or "").strip().upper()
    underlying = str(row.get("underlying_symbol") or "").strip().upper()
    option_type = str(row.get("option_type") or "").strip().upper()
    expiration = str(row.get("expiration") or "").strip()[:10]
    strike = _finite_number(row.get("strike"))
    quantity = _finite_number(row.get("net_quantity"))
    multiplier = _finite_number(row.get("contract_multiplier"))
    mark = _finite_number(row.get("price"))
    option_complete = bool(row.get("option_fields_complete"))
    reason: str | None = None
    if not option_complete or not all((symbol, underlying, option_type, expiration)) or strike is None:
        reason = "Exact option identity is incomplete; refresh the Schwab position before closing."
    elif quantity is None or math.isclose(quantity, 0.0, abs_tol=1e-9):
        reason = "Current option quantity is unavailable."
    elif not math.isclose(abs(quantity), round(abs(quantity)), rel_tol=0.0, abs_tol=1e-8):
        reason = "Current option quantity is not a whole number of contracts."
    elif multiplier is None or multiplier <= 0:
        reason = "The contract multiplier is unavailable."
    elif mark is None or mark < 0:
        reason = "The current position mark is unavailable."
    return OptionPositionLeg(
        account_label=account_label,
        symbol=symbol,
        underlying_symbol=underlying,
        option_type=option_type,
        expiration=expiration,
        strike=float(strike or 0.0),
        net_quantity=float(quantity or 0.0),
        settled_quantity=_finite_number(row.get("settled_quantity")),
        contract_multiplier=float(multiplier or 0.0),
        bid=_finite_number(row.get("bid")),
        ask=_finite_number(row.get("ask")),
        mark=mark,
        market_value=_finite_number(row.get("market_value")),
        unrealized_pnl=_finite_number(row.get("unrealized_pnl")),
        day_pnl=_finite_number(row.get("day_pnl")),
        delta=_finite_number(row.get("delta")),
        theta=_finite_number(row.get("theta")),
        observed_at=observed_at,
        quote_observed_at=_parse_datetime(row.get("quote_observed_at")) or observed_at,
        source_ref=str(row.get("source_ref") or ""),
        close_disabled_reason=reason,
    )


def _position_display_name(leg: OptionPositionLeg) -> str:
    strike = f"{leg.strike:g}"
    return f"{leg.underlying_symbol} {leg.expiration} {strike} {leg.option_type.title()} ({leg.symbol})"


def _whole_quantity(value: float, label: str) -> int:
    if value <= 0 or not math.isclose(value, round(value), rel_tol=0.0, abs_tol=1e-8):
        raise ValueError(f"{label} must be a positive whole number.")
    return int(round(value))


def _positive_price(value: object) -> float:
    number = _finite_number(value)
    if number is None or number <= 0:
        raise ValueError("Limit price must be a positive number.")
    return number


def _complete_sum(values: Iterable[float | None]) -> float | None:
    materialized = tuple(values)
    if not materialized:
        return 0.0
    if any(value is None for value in materialized):
        return None
    return round(sum(float(value) for value in materialized if value is not None), 2)


def _first_number(row: Mapping[str, object], keys: Sequence[str]) -> float | None:
    for key in keys:
        number = _finite_number(row.get(key))
        if number is not None:
            return number
    return None


def _order_value(
    row: Mapping[str, object],
    normalized: bool,
    normalized_key: str,
    raw_key: str,
) -> object:
    return row.get(normalized_key if normalized else raw_key)


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _timestamp_from_milliseconds(value: object) -> datetime | None:
    milliseconds = _finite_number(value)
    if milliseconds is None or milliseconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _reconciled_contract_multiplier(
    row: Mapping[str, object],
    mark: float | None,
) -> float | None:
    market_value = _finite_number(row.get("market_value"))
    quantity = _finite_number(row.get("net_quantity"))
    if market_value is None or quantity in (None, 0.0) or mark is None or mark <= 0:
        return None
    derived = abs(market_value / (quantity * mark))
    nearest = round(derived)
    if nearest <= 0 or not math.isclose(derived, nearest, rel_tol=0.0, abs_tol=0.1):
        return None
    return float(nearest)


def _rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _option_position_status(section: Mapping[str, object]) -> str:
    """Return readiness for option-position identity, not unrelated account rows."""

    status = str(section.get("status") or "UNAVAILABLE").strip().upper()
    if status == "UNAVAILABLE":
        return status
    option_row_set_complete = section.get("option_row_set_complete")
    if option_row_set_complete is True:
        return "CURRENT"
    if option_row_set_complete is False:
        return "INCOMPLETE"
    return status


__all__ = [
    "LOCAL_POSITION_MARK_SOURCE",
    "build_closing_order_draft",
    "build_closing_order_payload",
    "enrich_option_position_quotes",
    "filter_option_positions",
    "option_orders_from_payload",
    "option_orders_from_snapshot",
    "option_position_book",
    "submit_validated_closing_order",
    "validate_closing_position_drift",
]
