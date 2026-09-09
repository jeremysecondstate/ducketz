"""Pure Markdown rendering for the immutable, non-submitting nightly review."""
from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from pathlib import Path

import pandas as pd


_HORIZONS = ("1h", "4h", "1d", "1w")
_REASONS = {
    "NON_ENTRY_CONTEXT": "Outlook",
    "NO_BULLISH_ENTRY_SIGNAL": "Wait",
    "FORECAST_NOT_PROMOTED": "Model assessment pending",
    "PROVISIONAL_BUY": "Enter",
    "NO_EXACT_ROUTE_FITTED_HISTORY": "Wait — more price history needed",
    "ACCOUNT_OR_OWNERSHIP_EVIDENCE_UNAVAILABLE": "Wait — account check needed",
    "HORIZON_ALLOCATION_ALREADY_ACTIVE": "Wait — existing position",
    "REQUIRES_PRIOR_EXIT_CONFIRMATION": "Wait — previous exit not confirmed",
    "ENTRY_PRICE_RANGE_UNAVAILABLE": "Wait — price range unavailable",
    "QUOTE_REFERENCE_UNAVAILABLE": "Wait — quote unavailable",
    "QUOTE_REFERENCE_SPREAD_TOO_WIDE": "Wait — bid/ask spread too wide",
    "QUOTE_REFERENCE_TIME_UNAVAILABLE": "Wait — quote time unavailable",
    "QUOTE_REFERENCE_STALE_OR_FUTURE": "Wait — newer quote needed",
    "LIMIT_REFERENCE_ROUNDING_EXCEEDS_CAP": "Wait — price exceeds limit",
    "REFERENCE_OUTSIDE_HISTORICAL_RANGE": "Earlier preview used a retired price-range check; live pricing uses the current quote",
    "COMBINED_BATCH_ORDER_CAP": "Wait — entry batch is full",
    "INSUFFICIENT_WHOLE_SHARE_BUDGET": "Wait — remaining cash is too small",
}
_CHECKS = {
    "assessment_has_at_least_10_decision_clusters": "assessment decision-cluster minimum",
    "brier_beats_training_base_rate": "Brier score quality check",
    "log_loss_beats_training_base_rate": "log loss quality check",
    "brier_within_baseline_tolerance": "Brier score exceeds its configured baseline tolerance",
    "log_loss_within_baseline_tolerance": "log loss exceeds its configured baseline tolerance",
    "calibration_retains_directional_information": "calibration must retain directional information",
    "expected_calibration_error_at_most_0_15": "calibration error must be at most 0.15",
}


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _text(value) -> str:
    return str(value if value is not None else "Unavailable").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _money(value) -> str:
    number = _number(value)
    return f"${number:,.2f}" if number is not None else "Unavailable"


def _percent(value) -> str:
    number = _number(value)
    return f"{number * 100:.2f}%" if number is not None else "Unavailable"


def _metric(value) -> str:
    number = _number(value)
    return f"{number:.6f}" if number is not None else "Unavailable"


def _count(value) -> str:
    number = _number(value)
    return f"{number:,.0f}" if number is not None else "Unavailable"


def _shares(value) -> str:
    number = _number(value)
    return f"{number:,.6f}".rstrip("0").rstrip(".") if number is not None else "Unavailable"


def _policy_number(policy: Mapping, key: str, default: float) -> float:
    number = _number(policy.get(key))
    return default if number is None else number


def _pacific(value, *, required=False) -> str:
    try:
        stamp = pd.Timestamp(value)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError("A recorded timezone-aware timestamp is required")
        return stamp.tz_convert("America/Los_Angeles").strftime("%b %d, %Y %H:%M %Z")
    except (TypeError, ValueError):
        if required:
            raise ValueError("Frozen target windows require timezone-aware timestamps") from None
        return "Unavailable"


def _table(headers, rows) -> list[str]:
    return ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |",
            *("| " + " | ".join(_text(value) for value in row) + " |" for row in rows), ""]


def _mapping(value) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _quantity(row: Mapping, field: str) -> int | None:
    if field == "projected_trade_quantity" and not row["execution_eligible"]:
        return None
    value = row.get(field, row.get("trade_quantity"))
    number = _number(value)
    if (field == "projected_trade_quantity" and number is None
            and row.get("projected_quantity_reason") in {"PRICE_RANGE_UNAVAILABLE", "ACCOUNT_EVIDENCE_UNAVAILABLE"}):
        return None
    if number is None or number < 0 or number != int(number):
        raise ValueError("Trade review quantities must be nonnegative whole shares")
    return int(number)


def _route_label(row: Mapping) -> str:
    route = str(row["route"])
    start = pd.Timestamp(row["target_window_start"])
    end = pd.Timestamp(row["target_window_end"])
    # Required-window validation is also performed when rendering the window.
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Frozen target windows require timezone-aware timestamps")
    start, end = start.tz_convert("America/Los_Angeles"), end.tz_convert("America/Los_Angeles")
    if route.startswith("1d@"):
        offset = route.removeprefix("1d@D+")
        day_label = f"Day {int(offset) + 1} · " if offset.isdigit() else ""
        return f"Daily · {day_label}{start:%b %d}"
    if route.startswith("1w@"):
        return f"Weekly · Days 2–6 · {start:%b %d}–{end:%b %d}"
    if route == "1h@gap":
        return "Opening gap"
    if route.startswith("1h@"):
        return f"Hourly · {start:%H:%M}"
    if route.startswith("4h@"):
        return f"4-hour · {start:%H:%M}"
    return route


def _direction(row: Mapping) -> str:
    direction = str(row.get("planning_direction", row.get("direction", "Unavailable")))
    return {"BULLISH": "Bullish", "BEARISH": "Bearish", "NO_EDGE": "Neutral", "NEUTRAL": "Neutral"}.get(direction.upper(), direction)


def _entry_decision(row: Mapping) -> str:
    if not row["execution_eligible"]:
        return "Outlook"
    if row["model_status"] != "PROMOTED":
        return "Model assessment pending"
    if _quantity(row, "scheduled_trade_quantity") > 0:
        return "Enter"
    return _REASONS.get(str(row.get("trade_planning_reason")), "Wait")


def _direction_quantity(row: Mapping) -> str:
    if not row["execution_eligible"]:
        return "—"
    quantity = _number(row.get("direction_based_trade_quantity"))
    if quantity is None:
        return "Unavailable"
    if quantity != int(quantity):
        raise ValueError("Direction-based trade quantities must be signed whole shares")
    if quantity > 0:
        return f"BUY {int(quantity):,}"
    if quantity < 0:
        return f"SELL {int(-quantity):,}"
    return "0"


def _cash_range(low, high) -> str:
    if _number(low) is None or _number(high) is None:
        return "Unavailable"
    return _money(low) if _number(low) == _number(high) else f"{_money(low)}–{_money(high)}"


def _signed_money(value) -> str:
    number = _number(value)
    if number is None:
        return "Unavailable"
    return ("+" if number > 0 else "−" if number < 0 else "") + _money(abs(number))


def _plain_reason(value) -> str:
    known = {
        "NEUTRAL": "Hold", "NEUTRAL_NO_TRADE": "Hold", "NON_ENTRY_CONTEXT": "Outlook",
        "NO_AVAILABLE_SHARES": "No eligible shares to sell", "NO_HELD_SHARES": "No shares to sell",
        "INSUFFICIENT_CASH": "Insufficient cash", "INSUFFICIENT_WHOLE_SHARE_CAPACITY": "Insufficient whole-share capacity",
        "HORIZON_EXPIRY": "Horizon expiry", "EXPIRY": "Horizon expiry", "EXPIRING_LOT": "Horizon expiry",
        "BULLISH_BUY": "Buy with available cash", "BEARISH_SELL": "Sell eligible held shares",
        "BEARISH_SELL_HELD": "Sell eligible held shares", "PRICE_RANGE_UNAVAILABLE": "Price range unavailable",
        "MODEL_NOT_PROMOTED": "Model assessment pending",
        "SYMBOL_ALLOCATION_UNRESOLVED": "Hold — position allocation needs checking",
        "NO_AVAILABLE_SHARES_FOR_THIS_HORIZON": "Hold — no eligible shares for this horizon",
        "HORIZON_POSITION_ALREADY_HELD": "Hold — this horizon already owns shares",
        "INSUFFICIENT_CASH_OR_ALLOCATION_FOR_ONE_SHARE": "Hold — insufficient capacity for one share",
        "HORIZON_EXIT": "Horizon expiry",
    }
    if value is None:
        return "Unavailable"
    code = str(value)
    return known.get(code, code.replace("_", " ").capitalize())


def _plan_action(row: Mapping) -> str:
    if not row["execution_eligible"]:
        return "Outlook"
    if row.get("direction_based_reason") is not None:
        return _plain_reason(row["direction_based_reason"])
    quantity = _number(row.get("direction_based_trade_quantity"))
    if quantity == 0 and _direction(row) == "Neutral":
        return "Hold"
    return _plain_reason(row.get("direction_based_action"))


def _remaining_shares(row: Mapping) -> str:
    if not row["execution_eligible"]:
        return "—"
    total, available = _number(row.get("projected_shares_after")), _number(row.get("projected_available_shares_after"))
    text = _shares(total)
    if total is not None and available is not None and total != available:
        text += f" ({_shares(available)} after open orders)"
    return text


def _projection_tables(projection: Mapping, symbols: list[str], snapshot: Mapping) -> tuple[list[str], list[str]]:
    """Present the supplied shared ledger once; never recompute fills or cash."""
    if not projection:
        return [], []
    summary = _mapping(projection.get("summary"))
    ending = _mapping(projection.get("ending_positions"))
    main = ["## Projected end of day", "",
            "Cash is estimated before fees and taxes using the planned transactions and estimated prices. "
            "Price and cash ranges do not limit execution; each order uses the current quote and actual available cash and holdings. "
            "Holdings with later overnight or weekly expiries remain held at 17:00.", ""]
    main += _table(["Cash measure", "Low", "Base", "High"], [
        ("Starting available cash", *[_money(summary.get("starting_cash", snapshot.get("available_cash")))] * 3),
        ("End-of-day cash", _money(summary.get("ending_cash_low")), _money(summary.get("ending_cash_base")), _money(summary.get("ending_cash_high"))),
    ])
    position_rows = []
    for symbol in symbols:
        value = ending.get(symbol)
        if isinstance(value, Mapping):
            value = value.get("shares", value.get("held_shares"))
        position_rows.append((symbol, _shares(_mapping(snapshot.get("held_shares")).get(symbol)), _shares(value)))
    main += _table(["Stock", "Starting shares", "Projected closing shares"], position_rows)
    if _number(summary.get("cash_buffer")) is not None:
        main += [f"Purchases preserve a cash buffer of {_money(summary['cash_buffer'])} throughout this projection.", ""]
    main += ["## Cash and holdings by hour", "",
             "Each line shows the balance after that hour's entire planned batch: bearish sales, expiring positions, then bullish purchases. "
             "The same balance applies to forecast rows at that hour. Neutral forecasts add no trade; separate horizon expiries may still change that hour's balance.", ""]
    hourly = list(projection.get("hourly") or [])
    hourly.sort(key=lambda row: pd.Timestamp(row["timestamp"]))
    hourly_rows = []
    for row in hourly:
        held = _mapping(row.get("held_shares"))
        hourly_rows.append((_pacific(row.get("timestamp"), required=True), _money(row.get("cash_low")),
                            _money(row.get("cash_base")), _money(row.get("cash_high")),
                            *(_shares(held.get(symbol)) for symbol in symbols)))
    main += _table(["Hour, Pacific", "Cash low", "Cash base", "Cash high", *(f"{symbol} shares" for symbol in symbols)], hourly_rows)
    details = ["<details>", "<summary>Chronological buys, sales and expiries</summary>", "",
               "Each event below is one planned transaction. Sale proceeds and purchase costs enter the shared balance once; "
               "expired lots appear as sales here. These are projected events, not reported fills.", ""]
    events = list(projection.get("events") or [])
    events.sort(key=lambda row: pd.Timestamp(row["timestamp"]))
    event_rows = []
    for row in events:
        event_rows.append((_pacific(row.get("timestamp"), required=True), row.get("symbol"), row.get("horizon", "—"),
                           f"{str(row.get('action', 'Unavailable')).upper()} {_shares(row.get('quantity'))}",
                           _cash_range(row.get("price_low"), row.get("price_high")),
                           _cash_range(row.get("cash_before_low"), row.get("cash_before_high")),
                           _signed_money(row.get("cash_change_low")), _signed_money(row.get("cash_change_base")),
                           _signed_money(row.get("cash_change_high")),
                           _cash_range(row.get("cash_low"), row.get("cash_high")), _shares(row.get("shares_after")),
                           _plain_reason(row.get("reason"))))
    details += _table(["Time, Pacific", "Stock", "Horizon", "Plan", "Price range", "Cash before, range", "Cash change low", "Cash change base", "Cash change high",
                       "Cash after, range", "Shares after", "Reason"], event_rows)
    assumptions = projection.get("assumptions") or []
    if isinstance(assumptions, Mapping):
        assumptions = [f"{_plain_reason(key)}: {value}" for key, value in assumptions.items()]
    if assumptions:
        details += ["Projection assumptions:", "", *(f"- {_text(value)}" for value in assumptions), ""]
    details += ["</details>", ""]
    return main, details


def _sizing_failures(details: Mapping) -> str:
    explicit = details.get("failed_checks")
    if explicit:
        return "; ".join(_text(value) for value in explicit)
    scopes = _mapping(details.get("scope_readiness"))
    first = next(iter(scopes.values()), {})
    scores = _mapping(_mapping(first).get("horizon_assessment_scores"))
    failures = []
    for key, baseline, label in (
        ("brier", "base_brier", "Brier score does not beat baseline"),
        ("log_loss", "base_log_loss", "log loss does not beat baseline"),
        ("return_mse", "base_return_mse", "return error does not beat baseline"),
        ("adverse_mse", "base_adverse_mse", "adverse-return error exceeds baseline"),
    ):
        value, base = _number(scores.get(key)), _number(scores.get(baseline))
        if value is not None and base is not None and (value > base if key == "adverse_mse" else value >= base):
            failures.append(label)
    ece = _number(scores.get("ece"))
    if ece is not None and ece > .15:
        failures.append("calibration error exceeds 0.15")
    bounds = scores.get("probability_range")
    if isinstance(bounds, (tuple, list)) and len(bounds) == 2:
        low, high = _number(bounds[0]), _number(bounds[1])
        if low is not None and high is not None and high - low <= 1e-8:
            failures.append("probability output has insufficient variation")
    if failures:
        return "; ".join(failures)
    reasons = sorted({str(_mapping(scope).get("reason")) for scope in scopes.values() if _mapping(scope).get("reason")})
    return "; ".join(reasons) or str(details.get("reason") or "No failed checks supplied")


def render_trade_review(trade_rows: pd.DataFrame, report: Mapping, model_reports: Mapping,
                        *, source_gameplan: str) -> str:
    """Render supplied verified evidence without I/O, broker calls or mutations."""
    required = {"id", "symbol", "route", "target_window_start", "target_window_end", "raw_probability",
                "calibrated_probability", "model_status", "execution_eligible", "trade_quantity"}
    if trade_rows.empty or not required.issubset(trade_rows.columns) or trade_rows.id.duplicated().any():
        raise ValueError("Trade review requires unique forecast rows and their planning columns")
    source = Path(source_gameplan)
    if not source.is_absolute():
        raise ValueError("Trade review source Gameplan must be an absolute directory")
    rows = trade_rows.to_dict("records")
    for row in rows:
        _quantity(row, "projected_trade_quantity")
        _quantity(row, "scheduled_trade_quantity")
    symbols = list(dict.fromkeys(str(row["symbol"]) for row in rows))
    snapshot = _mapping(report.get("snapshot"))
    policy = _mapping(report.get("sizing_policy"))
    projection = _mapping(report.get("direction_based_projection"))
    has_direction_plan = bool(projection) or "direction_based_trade_quantity" in trade_rows.columns
    promoted_entries = sum(bool(row["execution_eligible"]) and row["model_status"] == "PROMOTED" for row in rows)
    entry_count = sum(bool(row["execution_eligible"]) for row in rows)
    positive = sum(_quantity(row, "scheduled_trade_quantity") > 0 for row in rows)
    planned = report.get("planned_notional", sum(_number(row.get("trade_notional_reserved")) or 0 for row in rows))
    threshold = _policy_number(report, "direction_up_threshold", _policy_number(policy, "minimum_trade_probability", .54))
    down_threshold = _policy_number(report, "direction_down_threshold", 1 - threshold)
    lines = [f"# {_text(report.get('action_date'))} Gameplan — quantities and planning prices", "",
             f"**{len(symbols)} stocks · {len(rows):,} forecasts · " + ("" if has_direction_plan else f"{positive} scheduled entries · ") +
             f"{_count(report.get('orders_placed'))} orders submitted**", "",
             f"Review generated {_pacific(report.get('observed_at'))}. All target windows and recorded times below are Pacific.", "",
             "**Projected Trade Quantity** is the share capacity available for each opportunity using current cash and holdings. "
             + ("A positive capacity can accompany a Sell or Hold plan action. " if has_direction_plan else
                "It can be positive beside **Wait** or **Model assessment pending**. ") +
             "Each capacity estimate stands alone and is not added to other rows as a simultaneous order.", "",
             ("" if has_direction_plan else f"Scheduled capital reserved: **{_money(planned)}**. ") +
             f"{_count(promoted_entries)} of {_count(entry_count)} entry windows have passed model assessment. "
             f"Direction is Bullish at {_percent(threshold)} P(up) or higher, Bearish at {_percent(down_threshold)} or lower, and Neutral between them. "
             "Direction and model approval are separate checks.", "", "## Cash and holdings", "",
             f"Account information captured {_pacific(snapshot.get('observed_at'))}.", ""]
    if has_direction_plan:
        lines[8:8] = [
            "**Direction Based Trade Qty** applies those directions in time order: Bullish buys with available cash, Bearish sells eligible held shares, and Neutral holds. "
            "This projection uses one shared cash balance and separate holdings for each stock across the session. "
            "The existing scheduled-entry preview is in the expandable details below; "
            "projected purchases and sales are not submitted orders or actual fills.", "",
        ]
    else:
        lines[8:8] = ["Only scheduled entries commit shared cash; projected quantities are separate capacity estimates.", ""]
    balances = _mapping(snapshot.get("balances"))
    lines += _table(["Account measure", "Recorded value"], [
        ("Account value", _money(snapshot.get("account_equity"))),
        ("Available cash after existing commitments", _money(snapshot.get("available_cash"))),
        ("Reported cash balance", _money(balances.get("cash_balance"))),
        ("Reported settled cash", _money(balances.get("settled_cash"))),
        ("Cash reserved for existing orders", _money(snapshot.get("reserved_cash"))),
        ("Current total investment / exposure", _money(snapshot.get("gross_exposure"))),
        ("Open orders", _count(snapshot.get("working_order_count"))),
    ])
    ownership = _mapping(snapshot.get("ownership"))
    issues = [*snapshot.get("reason_codes", []), *snapshot.get("cash_reason_codes", []), *ownership.get("reason_codes", [])]
    if issues:
        lines += ["Some account or position information is incomplete. Affected entries wait for that information to be checked.", ""]
    account_rows = []
    for symbol in symbols:
        quote = _mapping(_mapping(snapshot.get("quotes")).get(symbol))
        account_rows.append((symbol, _shares(_mapping(snapshot.get("held_shares")).get(symbol)),
                             _money(_mapping(snapshot.get("symbol_exposure")).get(symbol)),
                             _money(quote.get("price_reference")),
                             _pacific(quote.get("price_reference_time"))))
    lines += _table(["Stock", "Current shares", "Current investment / exposure", "Last recorded price", "Price time, Pacific"], account_rows)
    lines += ["Investment values include any options attributed to the stock. Price times show when the broker last observed the price.", ""]
    projection_main, projection_details = _projection_tables(projection, symbols, snapshot)
    lines += projection_main
    symbol_cap = _policy_number(policy, "maximum_symbol_equity_fraction", .15)
    order_cap = _policy_number(policy, "maximum_single_order_equity_fraction", .05)
    cash_cap = _policy_number(policy, "maximum_cash_utilization_fraction", .95)
    gross_cap = _policy_number(policy, "maximum_gross_equity_fraction", 1.30)
    model_details = ["<details>", "<summary>Model assessment details</summary>", "", "### Directional models", ""]
    assessment_rows = []
    for horizon in (*_HORIZONS, *(key for key in model_reports if key not in _HORIZONS)):
        if horizon not in model_reports:
            continue
        details = _mapping(model_reports[horizon])
        scores, base = _mapping(details.get("assessment")), _mapping(details.get("training_base_rate_assessment"))
        partitions, gate = _mapping(details.get("partitions")), _mapping(details.get("promotion_gate"))
        assessment_rows.append((horizon, "Passed" if gate.get("status") == "PROMOTED" else "Model assessment pending", " / ".join(_count(partitions.get(key)) for key in
                               ("train_rows", "selection_rows", "calibration_rows", "assessment_rows")),
                               f"{_metric(scores.get('brier_score'))} / {_metric(base.get('brier_score'))}",
                               f"{_metric(scores.get('log_loss'))} / {_metric(base.get('log_loss'))}",
                               _percent(scores.get("direction_accuracy_at_0_5"))))
    model_details += _table(["Horizon", "Assessment", "Train / select / calibrate / assess rows", "Brier: model / baseline", "Log loss: model / baseline", "Direction accuracy"], assessment_rows)
    model_details += ["Pending means this saved model has not passed the required checks. Lower Brier score and log loss are better. "
              "The baseline is a constant probability estimated from training and selection data. "
              "Assessment rows are held out; these figures are separate from prior published-forecast evaluation and realized trading returns.", ""]
    for horizon, details in model_reports.items():
        gate = _mapping(_mapping(details).get("promotion_gate"))
        tolerances = _mapping(gate.get("baseline_tolerances"))
        if tolerances:
            model_details += [f"**{_text(horizon)} assessment rule:** the allowed difference above baseline is "
                              f"{_metric(tolerances.get('brier_score'))} for Brier score and {_metric(tolerances.get('log_loss'))} for log loss. "
                              "The actual model and baseline scores are shown above; passing this rule does not imply baseline outperformance.", ""]
        failures = [str(key) for key, passed in _mapping(gate.get("checks")).items() if passed is False]
        if failures:
            model_details += [f"**{_text(horizon)} — model assessment pending:** "
                      + "; ".join(_CHECKS.get(check, check) for check in failures) + ". "
                      "This records failed promotion criteria; it does not establish the underlying cause of weaker performance.", ""]
    enrichment = _mapping(report.get("enrichment_summary"))
    if enrichment:
        horizons = _mapping(enrichment.get("horizons", enrichment))
        sizing_rows = [(horizon, details.get("status"), f"{_count(details.get('fitted_scope_count'))} / {_count(details.get('qualified_scope_count'))}",
                        _sizing_failures(details)) for horizon in _HORIZONS if isinstance((details := horizons.get(horizon)), Mapping)]
        model_details += ["### Independent sizing models", ""]
        model_details += _table(["Horizon", "Fit status", "Fitted / qualified scopes", "Failed checks / recorded reason"], sizing_rows)
        model_details += ["Fitted and qualified are separate outcomes. Scheduled entries require approved directional probabilities; "
                  "learned sizing remains a separate assessment lane and retains its recorded research status.", ""]
    model_details += ["</details>", ""]
    lines += ["## Forecasts and trade planning by stock", "",
              "Day 1 is the completed session used to build this plan; the next exchange session is Day 2. "
              "Daily and weekly rows show their actual dates. Probabilities retain the published values.", ""]
    for symbol in symbols:
        stock_rows = [row for row in rows if row["symbol"] == symbol]
        lines += [f"### {_text(symbol)}", ""]
        forecast_rows = []
        for row in stock_rows:
            low, high = _number(row.get("trade_price_low")), _number(row.get("trade_price_high"))
            price = f"{_money(low)}–{_money(high)}" if low is not None and high is not None else "—"
            values = (_route_label(row), f"{_pacific(row['target_window_start'], required=True)} → {_pacific(row['target_window_end'], required=True)}",
                      _percent(row["raw_probability"]), _percent(row["calibrated_probability"]),
                      _direction(row), _quantity(row, "projected_trade_quantity") if row["execution_eligible"] else "—")
            if has_direction_plan:
                values += (_direction_quantity(row), price,
                           _cash_range(row.get("projected_cash_after_low"), row.get("projected_cash_after_high")) if row["execution_eligible"] else "—",
                           _remaining_shares(row), _plan_action(row))
            else:
                values += (price, _entry_decision(row))
            forecast_rows.append(values)
        headers = ["Forecast", "Target window, Pacific", "Raw P(up)", "Published P(up)", "Direction", "Projected Trade Quantity"]
        if has_direction_plan:
            headers += ["Direction Based Trade Qty", "Trade Price (planning range)", "Cash available after (range)",
                        "Shares remaining", "Plan action"]
        else:
            headers += ["Trade Price (planning range)", "Entry decision"]
        lines += _table(headers, forecast_rows)
    lines += ["## How quantities and prices are planned", "",
              f"Each projected size starts with `account value × min({symbol_cap:g} × horizon weight / 10, {order_cap:g})`, "
              "using weights 1, 2, 3 and 4 for hourly, four-hour, daily and weekly opportunities. "
              "Current cash, total exposure and the stock's existing investment can reduce that capacity. "
              "Whole-share quantities use the upper end of the planning price range.", ""]
    scheduled_explanation = [
              f"Scheduled entries apply the forecast-confidence rule and share one cash budget, capped at {_percent(cash_cap)} of available cash, "
              f"{_percent(gross_cap)} gross account exposure and {_percent(symbol_cap)} per-stock exposure. "
              f"The minimum order is {_money(policy.get('minimum_order_notional', 25))}, with at most {min(6, int(policy.get('maximum_orders_per_wake', 6)))} "
              "entries in a batch. Expected future sale proceeds are excluded from this scheduled-entry budget.", ""]
    scheduled_details = []
    if has_direction_plan:
        scheduled_details = ["<details>", "<summary>Existing scheduled-entry preview</summary>", "",
                             f"{positive} scheduled entries. Scheduled capital reserved: **{_money(planned)}**. "
                             "This separate long-only preview retains its model and entry checks; it is not the direction-based buy/sell projection.", "",
                             *scheduled_explanation]
        scheduled_rows = [(row["symbol"], _route_label(row), _quantity(row, "scheduled_trade_quantity"),
                           _cash_range(row.get("scheduled_trade_price_low", row.get("trade_price_low")),
                                       row.get("scheduled_trade_price_high", row.get("trade_price_high"))) if row["execution_eligible"] else "—",
                           _entry_decision(row)) for row in rows]
        scheduled_details += _table(["Stock", "Forecast", "Scheduled entry quantity", "Scheduled entry planning range", "Entry decision"], scheduled_rows)
        scheduled_details += ["</details>", ""]
    else:
        lines += scheduled_explanation
    if has_direction_plan:
        lines += ["The direction projection instead carries its conditional sale proceeds and purchase costs through one chronological balance. "
                  "Existing open-order reservations and allocations to later horizons remain protected. Shares remaining includes protected holdings; "
                  "a smaller amount in parentheses is the total after open-order reservations. Only eligible shares may be sold.", "",
                  "Planning prices follow the observed historical median move from the prior session's close to each clock, with a working range of "
                  f"±{_policy_number(_mapping(report.get('planning_price_path')), 'working_half_width_bps', 20):g} basis points. "
                  "Price and cash ranges are estimates only, never execution limits. Trades may proceed outside either range using the current "
                  "tradable quote, actual available cash and shares held. The wider historical range is retained for stress analysis.", ""]
    else:
        lines += ["Planning prices use the middle 90% of observed historical moves from the prior session's close to the entry time.", ""]
    lines += ["At order time, buys use the current ask and sells use the current bid to set their live limit prices; "
              "quantities are recalculated from actual available cash and holdings. Estimated prices and balances do not veto an order.", "",
              *projection_details, *scheduled_details, *model_details]
    evaluation = _mapping(report.get("evaluation_summary"))
    if evaluation:
        totals = _mapping(evaluation.get("all_saved_gameplans", evaluation))
        lines += ["## Prior published-forecast evaluation", "",
                  f"{_count(totals.get('forecasts'))} prior forecasts: {_count(totals.get('evaluated'))} evaluated, "
                  f"{_count(totals.get('mature_awaiting_data'))} mature awaiting valid data and {_count(totals.get('pending_maturity'))} pending maturity. "
                  f"Observed direction accuracy {_percent(totals.get('direction_accuracy'))}; mean Brier score {_metric(totals.get('mean_brier_score'))}. "
                  "These are prior forecast scores, not realized trading returns.", ""]
    lines += ["<details>", "<summary>Source files</summary>", ""]
    for label, filename in (("Saved Gameplan", "gameplan.json"), ("Published forecasts", "forecasts.parquet"),
                            ("Model assessments", "model-reports.json")):
        lines.append(f"- [{label}](<{(source / filename).as_posix()}>)")
    lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


__all__ = ["render_trade_review"]
