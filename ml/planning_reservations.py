"""Conservative read-only planning of saved reservations absent at the broker.

This never reconciles a ledger or releases an execution reservation. A current
zero-order broker observation permits an informational plan to keep local
intent quantities and cash unavailable until the execution worker reconciles.
"""
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING


PENDING = "PENDING_LEDGER_RESERVATIONS_REQUIRE_RECONCILIATION"
BASIS = "ZERO_BROKER_ORDERS_WITH_LOCAL_RESERVATIONS_RETAINED"


def _number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean reservation amount")
    number = Decimal(str(value))
    if not number.is_finite() or number < 0:
        raise ValueError("Invalid reservation amount")
    return number


def retain_local_reservations(snapshot):
    """Return an independent planning copy; other uncertainty stays blocking."""
    result = deepcopy(snapshot)
    ownership = result.get("ownership", {})
    if (ownership.get("safe_for_planning") is True
            or ownership.get("reason_codes") != [PENDING]
            or ownership.get("account_matches") is not True
            or ownership.get("last_saved_reconciliation_ready") is not True
            or ownership.get("current_broker_reconciliation_performed") is not False
            or ownership.get("blocked_symbols") != []
            or result.get("status") != "OBSERVED"
            or result.get("cash_status") != "CASH_ONLY_BOUNDED"
            or type(result.get("working_order_count")) is not int
            or result["working_order_count"] != 0):
        return result
    symbols = set(result["held_shares"])
    for field in ("pending_buy_shares", "pending_sell_shares"):
        values = result.get(field)
        if not isinstance(values, dict) or set(values) != symbols or any(_number(v) for v in values.values()):
            return result
    if _number(result["reserved_cash"]) != 0:
        return result
    buys, sells = dict.fromkeys(symbols, Decimal(0)), dict.fromkeys(symbols, Decimal(0))
    for row in ownership["active_allocations"]:
        symbol = row["symbol"]
        buys[symbol] += _number(row["reserved_buy_shares"])
        sells[symbol] += _number(row["reserved_sell_shares"])
    if not any(buys.values()) and not any(sells.values()):
        return result
    original_cash = _number(result["available_cash"])
    recorded_cost = ownership.get("reserved_buy_cash_by_symbol")
    if not isinstance(recorded_cost, dict) or set(recorded_cost) != symbols:
        return result
    reserve = Decimal(0)
    for symbol in symbols:
        cost = _number(recorded_cost[symbol])
        if buys[symbol]:
            ask = _number(result["quotes"][symbol]["ask"])
            if ask <= 0 or cost <= 0:
                return result
            reserve += max(cost, buys[symbol] * ask)
        elif cost:
            return result
    reserve = reserve.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    result["available_cash"] = float(max(Decimal(0), original_cash - reserve))
    result["reserved_cash"] = float(reserve)
    result["pending_buy_shares"] = {s: float(v) for s, v in buys.items()}
    result["pending_sell_shares"] = {s: float(v) for s, v in sells.items()}
    result["planning_reservation_evidence"] = {
        "basis": BASIS, "broker_working_order_count": 0,
        "broker_pending_buy_shares": deepcopy(snapshot["pending_buy_shares"]),
        "broker_pending_sell_shares": deepcopy(snapshot["pending_sell_shares"]),
        "available_cash_before_local_reserve": float(original_cash),
        "local_cash_withheld": float(reserve), "original_reason_codes": [PENDING],
        "runtime_reconciliation_required": True,
    }
    ownership.update(status="OBSERVED_CONSISTENT", safe_for_planning=True, reason_codes=[])
    # Allocations, fills, reserved quantities, and saved reconciliation time are
    # deliberately unchanged. Pending maps above include local planning holds;
    # the evidence explicitly distinguishes them from actual broker orders.
    return result
