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
    "REFERENCE_OUTSIDE_HISTORICAL_RANGE": "Wait — price outside planning range",
    "COMBINED_BATCH_ORDER_CAP": "Wait — entry batch is full",
    "INSUFFICIENT_WHOLE_SHARE_BUDGET": "Wait — remaining cash is too small",
}
_CHECKS = {
    "assessment_has_at_least_10_decision_clusters": "assessment decision-cluster minimum",
    "brier_beats_training_base_rate": "Brier score must beat the training/selection baseline",
    "log_loss_beats_training_base_rate": "log loss must beat the training/selection baseline",
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


def _quantity(row: Mapping, field: str) -> int:
    value = row.get(field, row.get("trade_quantity"))
    number = _number(value)
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
        return f"Daily · {start:%b %d}"
    if route.startswith("1w@"):
        return f"Weekly · {start:%b %d}–{end:%b %d}"
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
    promoted_entries = sum(bool(row["execution_eligible"]) and row["model_status"] == "PROMOTED" for row in rows)
    entry_count = sum(bool(row["execution_eligible"]) for row in rows)
    positive = sum(_quantity(row, "scheduled_trade_quantity") > 0 for row in rows)
    planned = report.get("planned_notional", sum(_number(row.get("trade_notional_reserved")) or 0 for row in rows))
    threshold = _policy_number(report, "direction_up_threshold", _policy_number(policy, "minimum_trade_probability", .54))
    down_threshold = _policy_number(report, "direction_down_threshold", 1 - threshold)
    lines = [f"# {_text(report.get('action_date'))} Gameplan — quantities and planning prices", "",
             f"**{len(symbols)} stocks · {len(rows):,} forecasts · {positive} scheduled entries · "
             f"{_count(report.get('orders_placed'))} orders submitted**", "",
             f"Review generated {_pacific(report.get('observed_at'))}. All target windows and recorded times below are Pacific.", "",
             "**Projected Trade Quantity** is the share capacity available for each opportunity using current cash and holdings. "
             "It can be positive beside **Wait** or **Model assessment pending**. Each projection stands alone; "
             "only scheduled entries commit shared cash, so projected quantities are not added together as simultaneous orders.", "",
             f"Scheduled capital reserved: **{_money(planned)}**. {_count(promoted_entries)} of {_count(entry_count)} entry windows have passed model assessment. "
             f"Direction is Bullish at {_percent(threshold)} P(up) or higher, Bearish at {_percent(down_threshold)} or lower, and Neutral between them. "
             "Direction and model approval are separate checks.", "", "## Cash and holdings", "",
             f"Account information captured {_pacific(snapshot.get('observed_at'))}.", ""]
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
              "Daily rows use calendar dates: the former D+1 label meant the next exchange session after the completed session. "
              "Weekly rows show their start and expiry dates. Probabilities retain the published values.", ""]
    for symbol in symbols:
        stock_rows = [row for row in rows if row["symbol"] == symbol]
        lines += [f"### {_text(symbol)}", ""]
        forecast_rows = []
        for row in stock_rows:
            low, high = _number(row.get("trade_price_low")), _number(row.get("trade_price_high"))
            price = f"{_money(low)}–{_money(high)}" if low is not None and high is not None else "—"
            forecast_rows.append((_route_label(row), f"{_pacific(row['target_window_start'], required=True)} → {_pacific(row['target_window_end'], required=True)}",
                                  _percent(row["raw_probability"]), _percent(row["calibrated_probability"]),
                                  _direction(row), _quantity(row, "projected_trade_quantity"), price, _entry_decision(row)))
        lines += _table(["Forecast", "Target window, Pacific", "Raw P(up)", "Published P(up)", "Direction", "Projected Trade Quantity",
                         "Trade Price (planning range)", "Entry decision"], forecast_rows)
    lines += ["## How quantities and prices are planned", "",
              f"Each projected size starts with `account value × min({symbol_cap:g} × horizon weight / 10, {order_cap:g})`, "
              "using weights 1, 2, 3 and 4 for hourly, four-hour, daily and weekly opportunities. "
              "Current cash, total exposure and the stock's existing investment can reduce that capacity. "
              "Whole-share quantities use the upper end of the planning price range.", "",
              f"Scheduled entries also apply the forecast-confidence rule and share one cash budget, capped at {_percent(cash_cap)} of available cash, "
              f"{_percent(gross_cap)} gross account exposure and {_percent(symbol_cap)} per-stock exposure. "
              f"The minimum order is {_money(policy.get('minimum_order_notional', 25))}, with at most {min(6, int(policy.get('maximum_orders_per_wake', 6)))} "
              "entries in a batch. Expected future sale proceeds are excluded.", "",
              "Planning prices use the middle 90% of observed historical moves from the prior session's close to the entry time. "
              "An actual entry rechecks the ask LIMIT price, cash, holdings and trading controls at its scheduled time.", "",
              *model_details]
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
