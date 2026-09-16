"""Chronological, conditional stock Gameplan accounting; no broker authority.

All views derive from one cash ledger. Holdings belong to separate horizon
lots, so an hourly signal cannot spend or sell a longer horizon's shares.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Mapping

import pandas as pd

from ml.stock_direction_policy import stock_direction
from ml.stock_trader.contracts import StockTraderPolicy, utc
from ml.stock_trader.fixed_horizon_budget import FIXED_HORIZON_WEIGHTS


VERSION = "direction-based-gameplan-cash-ledger-v1"
SIGNAL_DRIVEN_HOLDING_POLICY = "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1"
ZERO = Decimal(0)


class UnavailablePlanningPricePath(ValueError):
    """Verified planning inputs explicitly lack a required price estimate."""

    def __init__(self, points: list[dict]):
        self.points = points
        super().__init__(f"Missing observed planning price path: {points[0]['key']}")


def _number(value: object) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Ledger quantities and money must be finite and nonnegative")
    return result


def _time(value: object) -> pd.Timestamp:
    if value is None:
        raise ValueError("Ledger clocks require explicit timezone-aware timestamps")
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("Ledger clocks require explicit timezone-aware timestamps")
    return result.tz_convert("UTC")


def project_direction_trades(trade_rows: pd.DataFrame, snapshot: Mapping,
                             price_path: Mapping, *, policy: StockTraderPolicy | None = None,
                             signal_driven: bool = False
                             ) -> tuple[pd.DataFrame, dict]:
    """Simulate saved directions with conditional fills and shared accounting.

    SELL uses unallocated holdings plus that horizon's lots. BUY creates a lot
    through its target end in the legacy policy. Signal-driven holdings instead
    accumulate on bullish forecasts and sell only on bearish forecasts, without
    model-promotion filtering or timed exits. Row balances are after the entire clock; events retain every
    transaction's before/after balances. Quantity is signed only on row fields.
    """
    policy = policy or StockTraderPolicy()
    policy.validate()
    if trade_rows.empty or trade_rows.id.duplicated().any():
        raise ValueError("The cash ledger requires unique forecast identities")
    ownership = snapshot.get("ownership", {})
    if (snapshot.get("status") != "OBSERVED" or snapshot.get("cash_status") != "CASH_ONLY_BOUNDED"
            or ownership.get("safe_for_planning") is not True):
        raise ValueError("The cash ledger requires a complete current account snapshot")
    records = trade_rows.to_dict("records")
    symbols = sorted(set(map(str, trade_rows.symbol)))
    days = set(map(str, trade_rows.action_date))
    if len(days) != 1:
        raise ValueError("The cash ledger requires one pinned action session")
    day = next(iter(days))
    times = [pd.Timestamp(f"{day} {hour:02d}:00", tz="America/Los_Angeles").tz_convert("UTC")
             for hour in range(4, 18)]
    points, unavailable = {}, []
    for timestamp in times:
        for symbol in symbols:
            key = f"{symbol}|{day}|{timestamp.tz_convert('America/Los_Angeles'):%H:%M}"
            point = price_path["points"].get(key, {})
            if (_time(point.get("timestamp")) != timestamp
                    or point.get("symbol") != symbol or str(point.get("action_date")) != day
                    or point.get("clock_local") != timestamp.tz_convert("America/Los_Angeles").strftime("%H:%M")):
                raise ValueError(f"Missing observed planning price path: {key}")
            if point.get("status") in {"UNAVAILABLE_REFERENCE_PRICE", "UNAVAILABLE_MINIMUM_SAMPLES"}:
                if any(point.get(f"planned_price_{field}") is not None for field in ("low","mid","high")):
                    raise ValueError("Unavailable planning prices cannot carry a numeric estimate")
                unavailable.append({"key":key, **{name:point.get(name) for name in (
                    "symbol","clock_local","status","reason","reference_session","reference_gap_minutes","sample_count")}})
                continue
            if point.get("status") != "AVAILABLE":
                raise ValueError(f"Missing observed planning price path: {key}")
            values = tuple(_number(point[f"planned_price_{field}"]) for field in ("low", "mid", "high"))
            if not ZERO < values[0] <= values[1] <= values[2]:
                raise ValueError(f"Invalid working price range: {key}")
            points[symbol, timestamp] = values
    equity = _number(snapshot["account_equity"])
    if equity <= 0:
        raise ValueError("The cash ledger requires positive account equity")
    starting_cash = _number(snapshot["available_cash"])
    cash = [starting_cash] * 3
    buffer = starting_cash * (1 - _number(policy.maximum_cash_utilization_fraction))
    held = {symbol: _number(snapshot["held_shares"][symbol]) for symbol in symbols}
    initial_held = held.copy()
    pending_sell = {symbol: _number(snapshot["pending_sell_shares"].get(symbol, 0)) for symbol in symbols}
    other = {symbol: _number(snapshot["other_symbol_exposure"][symbol]) for symbol in symbols}
    exposure = {symbol: _number(snapshot["symbol_exposure"][symbol]) for symbol in symbols}
    for symbol in symbols:
        stock_value = _number(snapshot["stock_market_value_by_symbol"][symbol])
        if abs(exposure[symbol] - stock_value - other[symbol]) > Decimal("0.01"):
            raise ValueError("Stock and non-stock exposure must reconcile to the account")
        if pending_sell[symbol] > held[symbol]:
            raise ValueError("Pending sells exceed held stock")
    outside = _number(snapshot["gross_exposure"]) - sum(exposure.values(), ZERO)
    if outside < Decimal("-0.01"):
        raise ValueError("Configured exposures exceed account gross exposure")
    outside = max(ZERO, outside)
    pending_buy = {}
    for symbol, quantity in snapshot.get("pending_buy_shares", {}).items():
        if _number(quantity):
            ask = _number(snapshot["quotes"][symbol]["ask"])
            if ask <= 0:
                raise ValueError("Pending buys need an observed price for exposure accounting")
            pending_buy[symbol] = _number(quantity) * ask
    pending_gross = max(sum(pending_buy.values(), ZERO), _number(snapshot["reserved_cash"]))
    free = held.copy()
    lots = []
    pending_horizons = set()
    owned_reserved = {symbol: ZERO for symbol in symbols}
    for allocation in ownership.get("active_allocations", []):
        symbol, horizon = str(allocation["symbol"]), str(allocation["horizon"])
        if symbol not in free:
            continue
        quantity = _number(allocation["owned_shares"])
        reserved = _number(allocation.get("reserved_sell_shares", 0))
        if _number(allocation.get("reserved_buy_shares", 0)):
            pending_horizons.add((symbol, horizon))
        if reserved > quantity or horizon not in FIXED_HORIZON_WEIGHTS:
            raise ValueError("Invalid active horizon allocation")
        free[symbol] -= quantity
        owned_reserved[symbol] += reserved
        if quantity:
            lots.append({"symbol": symbol, "horizon": horizon, "quantity": quantity - reserved,
                         "reserved": reserved, "end": _time(allocation.get("target_end")),
                         "forecast_id": str(allocation.get("prediction_id", "EXISTING_ALLOCATION"))})
    for symbol in symbols:
        if owned_reserved[symbol] > pending_sell[symbol]:
            raise ValueError("Allocated pending sells exceed broker pending sells")
        free[symbol] -= pending_sell[symbol] - owned_reserved[symbol]
        if free[symbol] < 0:
            raise ValueError("Horizon allocations and pending sales exceed held stock")
    blocked = set(ownership.get("blocked_symbols", []))
    events, hourly = [], []
    rows = {}
    batches = {timestamp: [] for timestamp in times}
    for record in records:
        row = {**record, "direction_based_trade_quantity": None, "direction_based_action": "CONTEXT",
               "direction_based_reason": "NON_ENTRY_CONTEXT", "projected_cash_after_low": None,
               "projected_cash_after_base": None, "projected_cash_after_high": None,
               "projected_shares_after": None, "projected_available_shares_after": None}
        rows[str(record["id"])] = row
        if record["execution_eligible"]:
            timestamp = _time(record["target_window_start"])
            if timestamp not in batches or _time(record["target_window_end"]) <= timestamp:
                raise ValueError("Entry and exit clocks must match the action session contract")
            if record["model_group"] not in FIXED_HORIZON_WEIGHTS:
                raise ValueError("Unknown independent horizon")
            row.update(direction_based_trade_quantity=0, direction_based_action="HOLD",
                       direction_based_reason="NEUTRAL")
            if not signal_driven and record["model_status"] != "PROMOTED":
                row["direction_based_reason"] = "MODEL_NOT_PROMOTED"
            elif record["symbol"] in blocked:
                row["direction_based_reason"] = "SYMBOL_ALLOCATION_UNRESOLVED"
            batches[timestamp].append(row)

    # Validate account, ownership and forecast contracts before classifying an
    # otherwise valid projection as unavailable. No trades have been simulated.
    if unavailable:
        raise UnavailablePlanningPricePath(unavailable)

    def trade(timestamp, symbol, action, quantity, reason, *, row=None, lot=None):
        nonlocal cash
        low, mid, high = points[symbol, timestamp]
        before_cash, before_shares = cash.copy(), held[symbol]
        if action == "BUY":
            changes = [-quantity * high, -quantity * mid, -quantity * low]
            held[symbol] += quantity
        else:
            changes = [quantity * low, quantity * mid, quantity * high]
            held[symbol] -= quantity
        cash = [value + change for value, change in zip(cash, changes)]
        if held[symbol] < pending_sell[symbol] or cash[0] < 0 or not cash[0] <= cash[1] <= cash[2]:
            raise ValueError("Projected trade violates cash or share conservation")
        forecast_id = str(row["id"]) if row else lot["forecast_id"]
        horizon = str(row["model_group"]) if row else lot["horizon"]
        event = {"sequence": len(events) + 1, "timestamp": timestamp.isoformat(), "symbol": symbol,
                 "action": action, "quantity": int(quantity), "reason": reason,
                 "forecast_id": forecast_id, "horizon": horizon,
                 "price_low": float(low), "price_base": float(mid), "price_high": float(high),
                 "cash_change_low": float(changes[0]), "cash_change_base": float(changes[1]),
                 "cash_change_high": float(changes[2]), "cash_before_low": float(before_cash[0]),
                 "cash_before_base": float(before_cash[1]), "cash_before_high": float(before_cash[2]),
                 "cash_low": float(cash[0]), "cash_base": float(cash[1]), "cash_high": float(cash[2]),
                 "shares_before": float(before_shares), "shares_after": float(held[symbol])}
        events.append(event)
        if row is not None:
            row.update(direction_based_trade_quantity=int(quantity) * (1 if action == "BUY" else -1),
                       direction_based_action=action, direction_based_reason=reason)

    for timestamp in times:
        batch = batches[timestamp]
        candidates = [row for row in batch if row["direction_based_reason"] == "NEUTRAL"]
        # Lower horizon wins a tie for an unallocated share. Other horizon lots
        # are protected. This deterministic precedence is disclosed in the plan.
        sellers = sorted((row for row in candidates if stock_direction(row["calibrated_probability"]) == "BEARISH"),
                         key=lambda row: (FIXED_HORIZON_WEIGHTS[row["model_group"]], row["symbol"], str(row["id"])))
        for row in sellers:
            symbol, horizon = row["symbol"], row["model_group"]
            own = [lot for lot in lots if lot["symbol"] == symbol and lot["horizon"] == horizon and lot["quantity"] > 0]
            quantity = Decimal(int(free[symbol] + sum((lot["quantity"] for lot in own), ZERO)))
            if not quantity:
                row["direction_based_reason"] = "NO_AVAILABLE_SHARES_FOR_THIS_HORIZON"
                continue
            remaining = quantity
            taken = min(free[symbol], remaining)
            free[symbol] -= taken
            remaining -= taken
            for lot in own:
                taken = min(lot["quantity"], remaining)
                lot["quantity"] -= taken
                remaining -= taken
            trade(timestamp, symbol, "SELL", quantity, "BEARISH_SELL", row=row)
        for lot in ([] if signal_driven else sorted(lots, key=lambda item: (item["end"], item["symbol"], item["horizon"], item["forecast_id"]))):
            if lot["symbol"] not in blocked and lot["end"] <= timestamp and lot["quantity"] >= 1:
                quantity = Decimal(int(lot["quantity"]))
                lot["quantity"] -= quantity
                trade(timestamp, lot["symbol"], "SELL", quantity, "HORIZON_EXIT", lot=lot)
        buyers = sorted((row for row in candidates if stock_direction(row["calibrated_probability"]) == "BULLISH"),
                        key=lambda row: (-float(row["calibrated_probability"]), FIXED_HORIZON_WEIGHTS[row["model_group"]],
                                         row["symbol"], str(row["id"])))
        for row in buyers:
            symbol, horizon = row["symbol"], row["model_group"]
            if (symbol, horizon) in pending_horizons:
                row["direction_based_reason"] = "HORIZON_BUY_ALREADY_PENDING"
                continue
            if not signal_driven and any(lot["symbol"] == symbol and lot["horizon"] == horizon
                   and lot["quantity"] + lot["reserved"] > 0 for lot in lots):
                row["direction_based_reason"] = "HORIZON_POSITION_ALREADY_HELD"
                continue
            values = {item: held[item] * points[item, timestamp][2] + other[item] for item in symbols}
            gross = outside + sum(values.values(), ZERO) + pending_gross
            ceiling = equity * min(_number(policy.maximum_symbol_equity_fraction) * FIXED_HORIZON_WEIGHTS[horizon] / 10,
                                   _number(policy.maximum_single_order_equity_fraction))
            budget = max(ZERO, min(ceiling, cash[0] - buffer,
                                   equity * _number(policy.maximum_gross_equity_fraction) - gross,
                                   equity * _number(policy.maximum_symbol_equity_fraction) - values[symbol]
                                   - pending_buy.get(symbol, ZERO)))
            price_high = points[symbol, timestamp][2]
            quantity = Decimal(int(budget / price_high))
            if quantity < 1 or quantity * price_high < _number(policy.minimum_order_notional):
                row["direction_based_reason"] = "INSUFFICIENT_CASH_OR_ALLOCATION_FOR_ONE_SHARE"
                continue
            trade(timestamp, symbol, "BUY", quantity, "BULLISH_BUY", row=row)
            lots.append({"symbol": symbol, "horizon": horizon, "quantity": quantity, "reserved": ZERO,
                         "end": _time(row["target_window_end"]), "forecast_id": str(row["id"])})
        held_report = {symbol: float(value) for symbol, value in held.items()}
        available_report = {symbol: float(held[symbol] - pending_sell[symbol]) for symbol in symbols}
        hourly.append({"timestamp": timestamp.isoformat(), "cash_low": float(cash[0]),
                       "cash_base": float(cash[1]), "cash_high": float(cash[2]),
                       "held_shares": held_report, "available_shares": available_report,
                       "event_sequences": [event["sequence"] for event in events if event["timestamp"] == timestamp.isoformat()]})
        for row in batch:
            row.update(projected_cash_after_low=float(cash[0]), projected_cash_after_base=float(cash[1]),
                       projected_cash_after_high=float(cash[2]), projected_shares_after=held_report[row["symbol"]],
                       projected_available_shares_after=available_report[row["symbol"]])
    for symbol in symbols:
        signed = sum((event["quantity"] * (1 if event["action"] == "BUY" else -1)
                      for event in events if event["symbol"] == symbol), 0)
        if held[symbol] != initial_held[symbol] + signed:
            raise ValueError("Ending holdings do not reconcile to chronological events")
    report = {"version": VERSION, "status": "COMPLETE", "events": events, "hourly": hourly,
              "starting_positions": {symbol: float(value) for symbol, value in initial_held.items()},
              "ending_positions": {symbol: float(value) for symbol, value in held.items()},
              "ending_allocations": [{**lot, "quantity": float(lot["quantity"]), "reserved": float(lot["reserved"]),
                                      "end": lot["end"].isoformat()} for lot in lots if lot["quantity"] + lot["reserved"] > 0],
              "summary": {"starting_cash": float(starting_cash), "ending_cash_low": float(cash[0]),
                          "ending_cash_base": float(cash[1]), "ending_cash_high": float(cash[2]),
                          "cash_buffer": float(buffer), "trade_events": len(events),
                          "buy_events": sum(event["action"] == "BUY" for event in events),
                          "sell_events": sum(event["action"] == "SELL" for event in events),
                          "direction_buy_rows": sum(row["direction_based_action"] == "BUY" for row in rows.values()),
                          "direction_sell_rows": sum(row["direction_based_action"] == "SELL" for row in rows.values()),
                          "cash_change_low": float(cash[0] - starting_cash), "cash_change_high": float(cash[2] - starting_cash)},
              "no_fill_baseline": {"cash": float(starting_cash), "held_shares": {symbol: float(value) for symbol, value in initial_held.items()}},
              "assumptions": [
                  "Promoted bullish probabilities above 50% buy; bearish below 50% sell; an exact 50% holds with zero direction quantity.",
                  "One shared cash balance and separate symbol/horizon holdings; all row balances are after the entire hour's batch.",
                  "Each hour processes bearish sales, remaining due horizon exits, then bullish buys. Sales use shorter horizons first, then symbol order; buys use highest probability first, then shorter horizon and symbol.",
                  "Bearish sales use unallocated shares and that horizon's own shares only, never another horizon's allocation; pending sell shares remain reserved.",
                  "Buy sizing uses the full horizon allocation and the conservative remaining cash and exposure. It does not apply the live worker's separate confidence multiplier or six-order batch limit.",
                  f"Cash starts at literal available cash after existing pending reservations; an initial cash buffer of {100 * (1 - policy.maximum_cash_utilization_fraction):g}% remains. Pending buys do not become holdings in this projection.",
                  "The cash projection uses estimated prices and assumed earlier sale proceeds for its planned trades and horizon exits. Price and cash ranges never gate execution: live orders use the current tradable quote and actual available cash and holdings, including outside those estimates. Live purchases may use actual completed sale proceeds only when available at the broker.",
                  f"Working ranges use the observed historical median with a +/-{float(price_path.get('working_half_width_bps', 20)) / 100:g}% fill allowance, rounded outward to cents; they are planning assumptions, not statistical confidence intervals.",
                  "Cash ranges are before fees and taxes: buys subtract upper/lower cost and sells add lower/upper proceeds. The midpoint is a scenario, not an expected return.",
                  "A neutral row adds no trade; a previous trade may separately reach its scheduled horizon exit at that hour. Positions ending after this session remain held at 17:00.",
                  "Unfilled trades leave the corresponding cash and shares unchanged; dependent later quantities must then be recalculated. The no-fill baseline is included separately.",
              ]}
    if signal_driven:
        report["holding_policy"] = SIGNAL_DRIVEN_HOLDING_POLICY
        report["assumptions"] = [
            "Each new bullish forecast adds shares using current capacity; bearish forecasts sell eligible shares in their own horizon. An exact 50% holds.",
            "Research assessment is disclosed separately and does not veto saved manual Gameplan instructions.",
            "Forecast window ends are prediction measurement times, not sale instructions. There are no scheduled expiry sales, including at day end.",
            "Each hour processes bearish sales, then bullish purchases. Holdings remain separated by symbol and horizon; cash and exposure are shared.",
            "Unallocated shares may be assigned to bearish sales; another horizon's shares and pending sells remain protected.",
            "These quantities assume fills at planning estimates and available sale proceeds. Live orders use current quotes, actual cash and confirmed holdings; no fill is guaranteed.",
            "Unfilled trades leave cash and shares unchanged. Subsequent projected quantities then require recalculation.",
        ]
    return pd.DataFrame([rows[str(identifier)] for identifier in trade_rows.id]), report
