"""Read-only presentation of saved Gameplan forecasts and conditional trades.

The direction ledger, not standalone capacity columns, supplies trade quantities.
Remaining allocations expose later expiries without inventing future sale prices.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import re
from typing import Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from datafetching.parquet_store import resolve_datastore_dir
from ml.artifacts import file_checksum, verify_manifest
from ml.gameplan_probability_target import (
    LEGACY_COST_TARGET, RAW_DIRECTION_TARGET, probability_target_contract, probability_target_metadata,
)

VERSION = "cash-aware-gameplan-trade-planning-v4"
LEDGER_VERSION = "direction-based-gameplan-cash-ledger-v1"
SIGNAL_DRIVEN_HOLDING_POLICY = "accumulate_bullish_sell_on_bearish_no_scheduled_expiry_v1"
UNAVAILABLE_PROJECTION = "UNAVAILABLE_PRICE_REFERENCES"
SHARED_PROJECTION_UNAVAILABLE = "UNAVAILABLE_SHARED_ACCOUNT_PROJECTION"
PACIFIC = ZoneInfo("America/Los_Angeles")
HORIZONS = ("1h", "4h", "1d", "1w")
REASONS = {
    "BULLISH_BUY": "Bullish entry", "BEARISH_SELL": "Bearish sale",
    "HORIZON_EXIT": "Scheduled horizon exit", "LATER_EXPIRY": "Later horizon expiry",
    "UNRESOLVED_EXPIRY": "Unresolved horizon expiry", "NEUTRAL": "Neutral forecast",
    "NO_AVAILABLE_SHARES_FOR_THIS_HORIZON": "No eligible shares in this horizon",
    "BEARISH_CROSS_HORIZON_FALLBACK": "Capped bearish sale from a longer horizon",
    "FALLBACK_OWN_OR_UNALLOCATED_INVENTORY_PROTECTED": "Own or unallocated shares protected by prior use or open orders",
    "FALLBACK_SLOT_OR_DAILY_CAP_EXHAUSTED": "No fallback quota at this entry",
    "FALLBACK_NO_ELIGIBLE_LONGER_DONOR": "No eligible longer-horizon donor",
    "FALLBACK_EXTERNAL_PENDING_ORDER": "Pending order is outside horizon ownership",
    "NON_ENTRY_CONTEXT": "Forecast context only", "MODEL_NOT_PROMOTED": "Model not promoted",
    "SYMBOL_ALLOCATION_UNRESOLVED": "Allocation needs resolution",
    "HORIZON_BUY_ALREADY_PENDING": "Buy already pending in this horizon",
    "HORIZON_POSITION_ALREADY_HELD": "Position already held in this horizon",
    "INSUFFICIENT_CASH_OR_ALLOCATION_FOR_ONE_SHARE": "Insufficient cash or allocation",
    "PRICE_REFERENCES_UNAVAILABLE": "Cash projection unavailable",
    "UNAVAILABLE_SHARED_ACCOUNT_PROJECTION": "Shared account projection is prepared by the coordinator",
}


class GameplanError(ValueError):
    """A saved plan is missing, incomplete, unsupported or inconsistent."""


def reason_text(reason: str, donor_horizon: str | None = None) -> str:
    if reason == "BEARISH_CROSS_HORIZON_FALLBACK" and donor_horizon:
        return f"Bearish fallback sale from {donor_horizon} holdings"
    return REASONS.get(reason, reason.replace("_", " ").capitalize())


@dataclass(frozen=True)
class PlanForecast:
    forecast_id: str
    symbol: str
    horizon: str
    route: str
    role: str
    eligible: bool
    model_status: str
    probability: float | None
    direction: str
    action: str
    quantity: float | None
    reason: str
    start: datetime
    end: datetime
    price: float | None
    fallback_donor_horizon: str | None = None
    fallback_donor_allocation_id_sha256: str | None = None
    producer_id: str | None = None
    original_forecast_id: str | None = None
    source_receipt_sha256: str | None = None
    cash_after_low: float | None = None
    cash_after_base: float | None = None
    cash_after_high: float | None = None


@dataclass(frozen=True)
class PlannedAction:
    key: str
    sequence: int
    when: datetime
    symbol: str
    horizon: str
    action: str
    quantity: float
    price: float | None
    reason: str
    forecast_id: str
    source: str = "ledger"
    reserved: float = 0
    fallback_donor_horizon: str | None = None
    fallback_donor_allocation_id_sha256: str | None = None


@dataclass(frozen=True)
class ExecutionQuote:
    forecast_id: str
    midpoint: float
    observed_at: datetime
    limit_price: float | None
    quantity: float | None
    is_exit: bool = False


@dataclass(frozen=True)
class Gameplan:
    session: str
    saved_at: datetime
    completed_at: datetime
    run_directory: Path
    report_path: Path
    forecasts: tuple[PlanForecast, ...]
    actions: tuple[PlannedAction, ...]
    projection_status: str = "COMPLETE"
    projection_note: str = ""
    planning_note: str = ""
    execution_quotes: tuple[ExecutionQuote, ...] = ()
    holding_policy: str = "fixed_target_expiry"
    probability_target_contract: str = LEGACY_COST_TARGET
    producer_id: str | None = None
    source_provenance: tuple[Mapping, ...] = ()
    account_ledger: Mapping | None = None

    @property
    def account_hourly(self) -> tuple[Mapping, ...]:
        """Full account post-clock cash; a producer view never recomputes it."""
        return tuple((self.account_ledger or {}).get("hourly", ()))

    @property
    def display_name(self) -> str:
        return "Yung Gameplan (YG)" if self.probability_target_contract == RAW_DIRECTION_TARGET else "OG Gameplan"

    @property
    def projection_available(self) -> bool:
        return self.projection_status == "COMPLETE"

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted({row.symbol for row in self.forecasts}))

    def rows(self, horizon: str = "all", symbol: str | None = None) -> tuple[PlanForecast, ...]:
        _filter_horizon(horizon)
        return tuple(row for row in self.forecasts if (horizon == "all" or row.horizon == horizon)
                     and (symbol is None or row.symbol == symbol))

    def trades(self, horizon: str = "all", symbol: str | None = None) -> tuple[PlannedAction, ...]:
        _filter_horizon(horizon)
        return tuple(row for row in self.actions if (horizon == "all" or row.horizon == horizon)
                     and (symbol is None or row.symbol == symbol))

    def forecast(self, identifier: str) -> PlanForecast | None:
        return next((row for row in self.forecasts if row.forecast_id == identifier), None)

    def execution_quote(self, identifier: str, *, is_exit=False) -> ExecutionQuote | None:
        return next((row for row in self.execution_quotes if row.forecast_id == identifier and row.is_exit == is_exit), None)


def _execution_quotes(root: Path, forecasts: tuple[PlanForecast, ...]) -> tuple[ExecutionQuote, ...]:
    from ml.stock_trader.price_comparison import quote_comparison
    identifiers = {row.forecast_id for row in forecasts}
    recorded = {}
    if not identifiers:
        return ()
    earliest = min(row.start for row in forecasts).strftime("%Y%m%d")
    latest = max(row.start for row in forecasts).strftime("%Y%m%d")
    for run in sorted((root / "ml/stock-trader-decision-runs").glob("*")):
        if not earliest <= run.name[:8] <= latest:
            continue
        try:
            document = _json(run / "decisions.json")
            for decision in document.get("decisions", []):
                prediction = decision.get("prediction") or {}
                identifier = prediction.get("parent_forecast_id", prediction.get("prediction_id"))
                if identifier not in identifiers:
                    continue
                comparison = quote_comparison(decision)
                if comparison["live_midpoint"] is None:
                    continue
                is_exit = prediction.get("position_purpose") == "EXIT"
                quote = ExecutionQuote(identifier, comparison["live_midpoint"],
                    _timestamp(comparison["quote_received_at"] or comparison["quote_updated_at"]),
                    _number(decision.get("limit_price"), optional=True), _number(decision.get("quantity"), optional=True), is_exit)
                key = (identifier, is_exit)
                if key not in recorded or quote.observed_at > recorded[key].observed_at:
                    recorded[key] = quote
        except (OSError, ValueError, TypeError, KeyError):
            # A missing optional execution record does not hide the plan.
            continue
    return tuple(recorded.values())


def _filter_horizon(horizon: str):
    if horizon not in ("all", *HORIZONS):
        raise ValueError("Unknown forecast horizon")


def _json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise GameplanError(f"Invalid saved metadata: {path.name}")
    return value


def _session(value: object) -> str:
    text = str(value)
    if date.fromisoformat(text).isoformat() != text:
        raise GameplanError("Invalid Gameplan session date")
    return text


def _timestamp(value: object) -> datetime:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise GameplanError("Saved plan clocks must include a timezone")
    return stamp.tz_convert(PACIFIC).to_pydatetime()


def _number(value: object, *, optional=False, minimum=0) -> float | None:
    if optional and (value is None or pd.isna(value)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise GameplanError("Invalid number in the saved plan")
    return number


def _run_path(root: Path, value: object) -> Path:
    run = (root / str(value)).resolve()
    if run.parent != (root / "ml/gameplan-trade-plan-runs").resolve():
        raise GameplanError("Gameplan points outside its saved run directory")
    return run


def _latest(root: Path) -> dict | None:
    path = root / "ml/gameplan-trade-plan-latest/run.json"
    if not path.is_file():
        return None
    value = _json(path)
    current = value.get("current")
    if value.get("schema_version") != VERSION or not isinstance(current, dict):
        raise GameplanError("This saved plan predates the direction-ledger view or uses an unsupported format")
    _session(current.get("action_date"))
    _run_path(root, current.get("run_path"))
    return current


def _history(root: Path) -> list[tuple[str, datetime, Path]]:
    # The trade-plan publisher has a latest pointer, but no dated pointers.
    # List completed receipts only; validate all outputs when a run is selected.
    result = []
    for path in (root / "ml/gameplan-trade-plan-runs").glob("*/receipt.json"):
        try:
            receipt = _json(path)
            if receipt.get("schema_version") == VERSION and receipt.get("status") == "COMPLETE":
                run = _run_path(root, receipt["run_path"])
                if run == path.parent.resolve():
                    result.append((_session(receipt["action_date"]), _timestamp(receipt["completed_at"]), run))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return result


def plan_sessions(datastore_root: Path | None = None) -> tuple[str, ...]:
    root = resolve_datastore_dir(root_dir=datastore_root).resolve()
    from ml.joint_capital_adoption import accepted_sessions, read_accepted_joint_plan
    joint_dates = accepted_sessions(root)
    current_local = _latest(root)
    latest_joint = read_accepted_joint_plan(root, joint_dates[0]) if joint_dates else None
    account_pointer = _account_pointer(root, accepted_joint=bool(latest_joint and (
        not current_local or joint_dates[0] >= current_local["action_date"])))
    sessions = set(joint_dates)
    if account_pointer is not None:
        try:
            current = _load_account_view(root, account_pointer)
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
            raise GameplanError(f"Could not verify the account Gameplan: {exc}") from exc
        sessions.update({current.session, *(item[0] for item in _account_history(root))})
        # Keep historical account selections intact while exposing replacement
        # local sessions prepared after that publication during rollout.
        sessions.update(item[0] for item in _history(root) if item[0] > current.session)
        if current_local and current_local["action_date"] > current.session:
            sessions.add(current_local["action_date"])
    else:
        sessions.update(item[0] for item in _history(root))
        if current_local:
            sessions.add(current_local["action_date"])
    return tuple(sorted(sessions, reverse=True))


def _forecast(row: dict, *, projection_available: bool = True, unavailable_reason: str = "PRICE_REFERENCES_UNAVAILABLE") -> PlanForecast:
    probability = _number(row["calibrated_probability"], optional=True)
    eligible = row["execution_eligible"]
    if not isinstance(eligible, bool) or (probability is not None and probability > 1):
        raise GameplanError("Invalid forecast probability or entry status")
    if row["model_status"] == "PROMOTED" and probability is None:
        raise GameplanError("A promoted forecast is missing its probability")
    horizon, direction = row["model_group"], row["direction"]
    if projection_available:
        action = row["direction_based_action"]
        quantity = _number(row["direction_based_trade_quantity"], optional=True, minimum=-math.inf)
        reason = str(row["direction_based_reason"])
    else:
        # Missing simulation is not a HOLD decision or a zero-share trade.
        action, quantity = ("UNAVAILABLE" if eligible else "CONTEXT"), None
        reason = unavailable_reason if eligible else "NON_ENTRY_CONTEXT"
    if horizon not in HORIZONS or direction not in {"BULLISH", "BEARISH", "NO_EDGE"}:
        raise GameplanError("Unsupported forecast horizon or direction")
    allowed = {"BUY", "SELL", "HOLD", "CONTEXT"} if projection_available else {"UNAVAILABLE", "CONTEXT"}
    if (action not in allowed
            or (eligible and action == "CONTEXT") or (not eligible and action != "CONTEXT")):
        raise GameplanError("Forecast context cannot be a projected trade")
    start, end = _timestamp(row["target_window_start"]), _timestamp(row["target_window_end"])
    if start >= end:
        raise GameplanError("Invalid forecast target window")
    if ((action == "BUY" and (quantity is None or quantity <= 0))
            or (action == "SELL" and (quantity is None or quantity >= 0))
            or (action == "HOLD" and quantity != 0)
            or (action == "CONTEXT" and quantity is not None)):
        raise GameplanError("Projected action and direction-based quantity disagree")
    price = _number(row.get("trade_price_mid"), optional=True)
    if price is not None and price <= 0:
        raise GameplanError("Saved planning prices must be positive when available")
    donor_horizon = donor_allocation = None
    if reason == "BEARISH_CROSS_HORIZON_FALLBACK":
        from ml.stock_trader.cross_horizon_fallback import donor_horizons
        donor_horizon, donor_allocation = row.get("fallback_donor_horizon"), row.get("fallback_donor_allocation_id_sha256")
        if (action != "SELL" or donor_horizon not in donor_horizons(horizon)
                or not isinstance(donor_allocation, str) or len(donor_allocation) != 64
                or any(char not in "0123456789abcdef" for char in donor_allocation)
                or row.get("fallback_trigger_forecast_id") != str(row["id"])
                or row.get("fallback_trigger_horizon") != horizon):
            raise GameplanError("Invalid fallback donor or triggering forecast")
    return PlanForecast(str(row["id"]), str(row["symbol"]), horizon, str(row["route"]),
                        str(row["target_role"]), eligible, str(row["model_status"]), probability,
                        direction, action, quantity, reason, start, end,
                        price, donor_horizon, donor_allocation,
                        row.get("producer_id"), row.get("original_forecast_id"), row.get("source_receipt_sha256"),
                        *(_number(row.get(f"projected_cash_after_{key}"), optional=True) for key in ("low", "base", "high")))


def _projection_metadata(ledger: dict, report: dict, frame: pd.DataFrame) -> tuple[str, str]:
    status = ledger.get("status")
    if report.get("direction_projection_status", "COMPLETE") != status:
        raise GameplanError("Saved report and direction ledger projection statuses disagree")
    if status == "COMPLETE":
        return status, ""
    if status == SHARED_PROJECTION_UNAVAILABLE:
        if (report.get("publication_mode") not in {"ACCOUNT_PRODUCER_SOURCE", "RESEARCH_PRODUCER_SOURCE"}
                or report.get("snapshot") is not None or report.get("snapshot_status") != "NOT_CAPTURED_PRODUCER_ONLY"
                or report.get("direction_based_projection") != ledger
                or any(ledger.get(key) != empty for key, empty in (("events", []), ("hourly", []), ("ending_positions", {}), ("summary", {})))
                or ledger.get("orders_placed") != 0 or ledger.get("broker_orders_enabled") is not False):
            raise GameplanError("Producer-only preparation contains a shared-account projection")
        columns = [name for name in frame if name.startswith(("direction_based_", "projected_cash_after_"))
                   or name in {"trade_quantity", "scheduled_trade_quantity", "projected_trade_quantity", "projected_shares_after"}]
        if columns and frame[columns].notna().any().any():
            raise GameplanError("Producer-only preparation cannot invent quantities or cash")
        return status, "Shared account projection is prepared by the coordinator. This producer view contains frozen forecasts and saved price estimates only."
    if status != UNAVAILABLE_PROJECTION or report.get("direction_based_projection") != ledger:
        raise GameplanError("Unsupported or inconsistent unavailable cash projection")
    # Only the publisher's explicit unavailable state can omit direction columns.
    # Integrity verification still applies to every original artifact.
    if (any(ledger.get(key) != empty for key, empty in (
            ("events", []), ("hourly", []), ("ending_positions", {}), ("summary", {})))
            or ledger.get("ending_allocations", []) != []
            or any(value is not None for key, value in ledger.items() if key.startswith("ending_cash"))
            or ledger.get("orders_placed") != 0 or ledger.get("broker_orders_enabled") is not False):
        raise GameplanError("Unavailable cash projection contains projected trades or balances")
    columns = [name for name in frame if name.startswith(("direction_based_", "projected_cash_after_"))]
    if columns and frame[columns].notna().any().any():
        raise GameplanError("Unavailable cash projection contains projected forecast actions or cash")
    points, reason = ledger.get("unavailable_points"), ledger.get("reason")
    if (not isinstance(reason, str) or not reason.strip() or not isinstance(points, list) or not points
            or any(not isinstance(point, dict) or point.get("symbol") not in set(frame.symbol)
                   or not isinstance(point.get("reason"), str) or not point["reason"].strip()
                   for point in points)):
        raise GameplanError("Unavailable cash projection is missing its price-reference explanation")
    missing = sorted({point["symbol"] for point in points
                      if point.get("status") == "UNAVAILABLE_REFERENCE_PRICE"})
    sparse = []
    for point in points:
        if point.get("status") != "UNAVAILABLE_MINIMUM_SAMPLES":
            continue
        label = str(point["symbol"])
        clock = point.get("clock_local")
        if isinstance(clock, str) and len(clock) == 5 and clock[2] == ":" and clock.replace(":", "").isdigit():
            label += f" at {clock} Pacific"
        count = point.get("sample_count")
        if type(count) is int and count >= 0:
            label += f" ({count} historical {'pair' if count == 1 else 'pairs'})"
        sparse.append(label)
    reasons = []
    if missing:
        reasons.append("missing closing references for " + ", ".join(missing))
    if sparse:
        labels = sorted(set(sparse))
        reasons.append("too few historical pairs for " + ", ".join(labels[:4])
                       + (f" and {len(labels) - 4} more points" if len(labels) > 4 else ""))
    other = sorted({point["symbol"] for point in points if point.get("status") not in
                    {"UNAVAILABLE_REFERENCE_PRICE", "UNAVAILABLE_MINIMUM_SAMPLES"}})
    if other:
        reasons.append("incomplete planning price evidence for " + ", ".join(other))
    return status, "Cash projection unavailable: " + "; ".join(reasons) + ". Saved forecasts remain available."


def _validate_fallback_accounting(ledger: dict, session: str) -> None:
    from ml.stock_trader.cross_horizon_fallback import validate_fallback_policy, donor_horizons, slot_quota
    policy = validate_fallback_policy(ledger.get("cross_horizon_fallback_policy"), session)
    if policy is None:
        return
    fallback = ledger["cross_horizon_fallback"]
    donors, caps = fallback["donors"], fallback["symbol_daily_caps"]
    if fallback.get("action_date") != session:
        raise GameplanError("Fallback accounting action date differs")
    for identifier, donor in donors.items():
        initial = _number(donor["initial_shares"])
        if (not isinstance(identifier, str) or len(identifier) != 64
                or any(char not in "0123456789abcdef" for char in identifier)
                or donor["horizon"] not in HORIZONS or donor["daily_cap"] != int(initial / 2)):
            raise GameplanError("Fallback donor baseline or daily cap is invalid")
    expected_caps = {symbol: int(sum(_number(d["initial_shares"]) for d in donors.values()
        if d["symbol"] == symbol and d["horizon"] in donor_horizons("1h")) / 2) for symbol in caps}
    if caps != expected_caps:
        raise GameplanError("Fallback symbol daily cap differs from its initial holdings")
    symbol_used, donor_used, slots = dict.fromkeys(caps, 0), dict.fromkeys(donors, 0), set()
    count = 0
    for event in ledger["events"]:
        if event["reason"] != "BEARISH_CROSS_HORIZON_FALLBACK":
            continue
        count += 1
        attribution = event["cross_horizon_fallback"]
        symbol, horizon, amount = event["symbol"], event["horizon"], event["quantity"]
        identifier = attribution["donor_allocation_id_sha256"]
        donor = donors[identifier]
        key = (symbol, horizon, event["timestamp"])
        quota = slot_quota(caps[symbol], horizon, event["timestamp"], session)
        if (type(amount) is not int or amount <= 0 or key in slots or amount > quota
                or donor["symbol"] != symbol or donor["horizon"] not in donor_horizons(horizon)
                or attribution.get("policy_version") != policy["policy_version"]
                or attribution.get("action_date") != session
                or attribution.get("slot_quota") != quota
                or attribution.get("symbol_daily_cap") != caps[symbol]
                or attribution.get("donor_daily_cap") != donor["daily_cap"]
                or attribution.get("symbol_used_before") != symbol_used[symbol]
                or attribution.get("donor_used_before") != donor_used[identifier]):
            raise GameplanError("Fallback event exceeds or disagrees with its frozen quota")
        symbol_used[symbol] += amount
        donor_used[identifier] += amount
        slots.add(key)
        if (symbol_used[symbol] > caps[symbol] or donor_used[identifier] > donor["daily_cap"]
                or attribution.get("symbol_used_after") != symbol_used[symbol]
                or attribution.get("donor_used_after") != donor_used[identifier]
                or _number(attribution["donor_shares_before"]) - amount != _number(attribution["donor_shares_after"])):
            raise GameplanError("Fallback cumulative sales or donor shares do not reconcile")
    if (fallback["symbol_used"] != symbol_used or fallback["events"] != count
            or any(donors[identifier]["used"] != used for identifier, used in donor_used.items())):
        raise GameplanError("Fallback summary disagrees with its attributed sales")


def _actions(ledger: dict, forecasts: tuple[PlanForecast, ...], session: str) -> tuple[PlannedAction, ...]:
    if ledger.get("version") != LEDGER_VERSION or ledger.get("status") != "COMPLETE":
        raise GameplanError("The direction ledger is not complete or supported")
    _validate_fallback_accounting(ledger, session)
    by_id = {row.forecast_id: row for row in forecasts}
    symbols = {row.symbol for row in forecasts}
    actions = []
    sequences = set()
    traded_ids = set()
    for event in ledger["events"]:
        sequence = event["sequence"]
        if not isinstance(sequence, int) or sequence < 1 or sequence in sequences:
            raise GameplanError("Duplicate or invalid trade sequence")
        sequences.add(sequence)
        action, reason = event["action"], event["reason"]
        if (action, reason) not in {("BUY", "BULLISH_BUY"), ("SELL", "BEARISH_SELL"), ("SELL", "HORIZON_EXIT"),
                                   ("SELL", "BEARISH_CROSS_HORIZON_FALLBACK")}:
            raise GameplanError("Unsupported projected trade action")
        symbol, horizon, identifier = str(event["symbol"]), event["horizon"], str(event["forecast_id"])
        if symbol not in symbols or horizon not in HORIZONS:
            raise GameplanError("Unknown trade company or horizon")
        quantity = _number(event["quantity"])
        if quantity <= 0:
            raise GameplanError("Projected trade quantities must be positive")
        when = _timestamp(event["timestamp"])
        forecast = by_id.get(identifier)
        if forecast and (forecast.symbol != symbol or forecast.horizon != horizon or not forecast.eligible):
            raise GameplanError("Trade and forecast identities disagree")
        if reason != "HORIZON_EXIT":
            if (not forecast or forecast.action != action or forecast.start != when
                    or abs(forecast.quantity) != quantity or identifier in traded_ids):
                raise GameplanError("Direction ledger disagrees with its forecast quantity or clock")
            traded_ids.add(identifier)
        price = _number(event["price_base"])
        if price <= 0:
            raise GameplanError("Saved trade prices must be positive")
        attribution = event.get("cross_horizon_fallback", {})
        if reason == "BEARISH_CROSS_HORIZON_FALLBACK":
            if (ledger.get("cross_horizon_fallback_policy") is None
                    or not isinstance(attribution, dict)
                    or attribution.get("trigger_forecast_id") != identifier
                    or attribution.get("trigger_horizon") != horizon
                    or attribution.get("donor_horizon") != forecast.fallback_donor_horizon
                    or attribution.get("donor_allocation_id_sha256") != forecast.fallback_donor_allocation_id_sha256
                    or attribution.get("quantity") != quantity):
                raise GameplanError("Fallback event attribution disagrees with its source forecast")
        elif attribution:
            raise GameplanError("Ordinary projected trade cannot use a fallback donor")
        actions.append(PlannedAction(f"trade:{sequence}", sequence, when, symbol, horizon, action,
                                     quantity, price, reason, identifier,
                                     fallback_donor_horizon=attribution.get("donor_horizon"),
                                     fallback_donor_allocation_id_sha256=attribution.get("donor_allocation_id_sha256")))
    expected = {row.forecast_id for row in forecasts if row.action in {"BUY", "SELL"}}
    if expected != traded_ids:
        raise GameplanError("A projected forecast trade is missing from the direction ledger")
    summary = ledger["summary"]
    if (summary["trade_events"] != len(actions)
            or summary["buy_events"] != sum(row.action == "BUY" for row in actions)
            or summary["sell_events"] != sum(row.action == "SELL" for row in actions)):
        raise GameplanError("Direction ledger counts disagree with its events")
    close = pd.Timestamp(f"{session} 17:00", tz=PACIFIC).to_pydatetime()
    # These are expiry obligations, NOT additional simulated sell events. Reserved
    # shares stay disclosed so an existing pending sale is never counted twice.
    for index, lot in enumerate([] if ledger.get("holding_policy") == SIGNAL_DRIVEN_HOLDING_POLICY else ledger["ending_allocations"]):
        quantity, reserved = _number(lot["quantity"]), _number(lot["reserved"])
        if quantity + reserved == 0:
            continue
        symbol, horizon = str(lot["symbol"]), lot["horizon"]
        if symbol not in symbols or horizon not in HORIZONS:
            raise GameplanError("Unknown remaining allocation")
        end = _timestamp(lot["end"])
        forecast = by_id.get(str(lot["forecast_id"]))
        if forecast and (forecast.symbol != symbol or forecast.horizon != horizon or not forecast.eligible):
            raise GameplanError("Remaining allocation and forecast identities disagree")
        actions.append(PlannedAction(f"expiry:{index}", len(sequences) + index + 1, end, symbol, horizon,
                                     "EXPIRY", quantity + reserved, None,
                                     "LATER_EXPIRY" if end > close else "UNRESOLVED_EXPIRY",
                                     str(lot["forecast_id"]), "remaining_allocation", reserved))
    return tuple(sorted(actions, key=lambda item: (item.when, item.sequence)))


def _planning_note(report: dict, symbols: set[str]) -> str:
    completion = report.get("reference_completion", {})
    carried = []
    for reference in completion.get("references", {}).values():
        if reference.get("status") != "AVAILABLE_SYNTHETIC":
            continue
        symbol = reference["symbol"]
        gap = _number(reference["gap_minutes"])
        actual = _timestamp(reference["observed_at"])
        effective = _timestamp(reference["effective_at"])
        if symbol not in symbols or not 5 < gap <= 240 or abs((effective - actual).total_seconds() / 60 - gap) > 1e-6:
            raise GameplanError("Invalid carried planning close provenance")
        carried.append(f"{symbol} ({gap:g} min)")
    if carried:
        return "Planning estimates use carried closes: " + ", ".join(sorted(carried)) + ". See the report for source times."
    if any(ref.get("status") == "AVAILABLE_SYNTHETIC" for ref in completion.get("historical_references", {}).values()):
        return "Planning estimates include carried historical closes. See the report for source times."
    return ""


def _account_run_path(root: Path, value: object) -> Path:
    if (not isinstance(value, str) or "\\" in value or ":" in value
            or not value.startswith("ml/account-gameplan-runs/") or ".." in value.split("/")):
        raise GameplanError("Account Gameplan points outside its saved run directory")
    run = (root / value).resolve()
    if run.parent != (root / "ml/account-gameplan-runs").resolve():
        raise GameplanError("Account Gameplan points outside its saved run directory")
    return run


def _account_hash(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise GameplanError("Account Gameplan requires its explicit manifest SHA256")
    return value


def _account_pointer(root: Path, *, accepted_joint: bool = False) -> dict | None:
    path = root / "ml/account-gameplan-latest/run.json"
    if not path.exists():
        from ml.account_gameplan.config import load_account_config
        try:
            config = load_account_config(root)
            if config is not None and config.activation["status"] == "ACTIVE" and not accepted_joint:
                raise GameplanError("The shared account Gameplan is not available yet.")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise GameplanError(f"Could not verify the shared account configuration: {exc}") from exc
        return None
    try:
        pointer = _json(path)
        from ml.account_gameplan.config import load_account_config, VERSION as ACCOUNT_CONFIG_VERSION
        config = load_account_config(root)
        if config is not None and (pointer.get("schema_version") != ACCOUNT_CONFIG_VERSION
                or pointer.get("status") != "SELECTED" or pointer.get("config_sha256") != config.fingerprint
                or pointer.get("producer_id") != config.machine_id):
            raise GameplanError("Account view differs from this PC's configured producer binding")
        current, producer = pointer.get("current"), pointer.get("producer_id")
        if (not isinstance(current, dict) or not isinstance(producer, str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", producer) is None):
            raise GameplanError("Invalid account Gameplan pointer or producer view")
        _account_run_path(root, current.get("run_path"))
        _account_hash(current.get("manifest_sha256"))
        return pointer
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise GameplanError(f"Could not verify the account Gameplan pointer: {exc}") from exc


def _account_history(root: Path) -> list[tuple[str, datetime, Path, str]]:
    from ml.account_gameplan.planner import VERSION as ACCOUNT_VERSION
    history = []
    for file in (root / "ml/account-gameplan-runs").glob("*/receipt.json"):
        try:
            receipt = _json(file)
            if receipt.get("schema_version") == ACCOUNT_VERSION and receipt.get("status") == "COMPLETE":
                run = _account_run_path(root, file.parent.relative_to(root).as_posix())
                history.append((_session(receipt["action_date"]), _timestamp(receipt["completed_at"]),
                                run, _account_hash(receipt["manifest_sha256"])))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return history


def _load_account_view(root: Path, pointer: dict, requested: str | None = None) -> Gameplan:
    from ml.account_gameplan.planner import read_account_plan, account_forecast_id
    current, producer = pointer["current"], pointer["producer_id"]
    # Validate the current pin even when selecting history. An invalid active
    # account pointer never silently routes this PC back to its legacy ledger.
    plan = read_account_plan(_account_run_path(root, current["run_path"]),
                             expected_manifest_sha256=current["manifest_sha256"])
    if requested is not None and requested != plan.report["action_date"]:
        candidates = [item for item in _account_history(root) if item[0] == requested]
        if not candidates:
            raise GameplanError(f"No completed account Gameplan is saved for {requested}")
        _, _, run, manifest_hash = max(candidates, key=lambda item: (item[1], item[2].name))
        plan = read_account_plan(run, expected_manifest_sha256=manifest_hash)
    report, ledger, frame = dict(plan.report), dict(plan.ledger), plan.rows
    session = _session(report["action_date"])
    sources = report["sources"]
    registry = {source["producer_id"]: source for source in sources}
    from ml.account_gameplan.config import load_account_config
    config = load_account_config(root)
    if config is not None and (report.get("account_fingerprint") != config.account_fingerprint
            or {identity: tuple(sorted(source["symbols"])) for identity, source in registry.items()} != config.participants):
        raise GameplanError("Account view universe or account differs from this PC's binding")
    if len(registry) != 2 or len(sources) != 2 or producer not in registry:
        raise GameplanError("Account Gameplan requires two distinct producer identities and this PC's view")
    membership = {}
    for identity, source in registry.items():
        _account_hash(source["source_receipt_sha256"])
        _account_hash(source["bundle_manifest_sha256"])
        symbols = source["symbols"]
        if not symbols or len(set(symbols)) != len(symbols) or set(symbols) & set(membership):
            raise GameplanError("Account Gameplan producer universes overlap or are invalid")
        membership.update({symbol: identity for symbol in symbols})
    if (report["symbol_producers"] != membership or set(frame.symbol) != set(membership)
            or len(frame) != 24 * len(membership) or not frame.groupby("symbol").size().eq(24).all()
            or frame.id.isna().any() or frame.id.duplicated().any()
            or frame.duplicated(["symbol", "route"]).any() or not frame.action_date.astype(str).eq(session).all()):
        raise GameplanError("Account Gameplan forecast coverage differs from its producer registry")
    for row in frame.to_dict("records"):
        identity = membership[row["symbol"]]
        if (row.get("producer_id") != identity or row.get("source_receipt_sha256") != registry[identity]["source_receipt_sha256"]
                or row["id"] != account_forecast_id(identity, Path(registry[identity]["source_gameplan_run"]).name,
                                                   row.get("original_forecast_id"))):
            raise GameplanError("Account forecast lost its original producer or source identity")
    contract = probability_target_contract(frame)
    if "gameplan_variant" in frame and not frame.gameplan_variant.eq(probability_target_metadata(contract)["gameplan_variant"]).all():
        raise GameplanError("Account Gameplan variant disagrees with its frozen target")
    status, note = _projection_metadata(ledger, report, frame)
    available = status == "COMPLETE"
    # Validate the entire account first, including the other producer's events.
    # Only presentation is filtered; the shared cash ledger remains untouched.
    forecasts = tuple(sorted((_forecast(row, projection_available=available) for row in frame.to_dict("records")),
                            key=lambda row: (row.start, HORIZONS.index(row.horizon), row.symbol, row.route)))
    all_actions = _actions(ledger, forecasts, session) if available else ()
    if available:
        hourly = {_timestamp(item["timestamp"]): tuple(_number(item[f"cash_{key}"]) for key in ("low", "base", "high"))
                  for item in ledger["hourly"]}
        expected_clocks = {_timestamp(pd.Timestamp(f"{session} {hour:02d}:00", tz=PACIFIC))
                           for hour in range(4, 18)}
        if len(ledger["hourly"]) != 14 or set(hourly) != expected_clocks:
            raise GameplanError("Account hourly cash coverage is incomplete or duplicated")
        for row in forecasts:
            if row.eligible and (row.cash_after_low, row.cash_after_base, row.cash_after_high) != hourly.get(row.start):
                raise GameplanError("Producer forecast cash differs from the shared account post-clock balance")
    symbols = set(registry[producer]["symbols"])
    forecasts = tuple(row for row in forecasts if row.symbol in symbols)
    actions = tuple(row for row in all_actions if row.symbol in symbols)
    receipt = _json(plan.path / "receipt.json")
    view_name = f"Gameplan-{producer}.md"
    if view_name not in _json(plan.path / "manifest.json").get("output_files", {}):
        raise GameplanError("Account Gameplan is missing its bound producer report")
    planning_note = ("This producer view uses the combined account's post-clock cash after all companies. "
                     "Projected fills and sale proceeds are conditional; actual cash follows broker evidence.")
    return Gameplan(session, _timestamp(report["observed_at"]), _timestamp(receipt["completed_at"]),
                    plan.path, plan.path / view_name, forecasts, actions, status, note,
                    planning_note, _execution_quotes(root, forecasts), ledger.get("holding_policy", "fixed_target_expiry"),
                    contract, producer, tuple(sources), ledger)


def load_gameplan(datastore_root: Path | None = None, session: str | None = None, *,
                  run_directory: Path | None = None,
                  expected_receipt_sha256: str | None = None) -> Gameplan:
    """Read the selected display or an explicitly pinned local publication.

    Explicit immutable publications are used by preparation before joint
    synthesis exists. They never change or consult combined display pointers.
    """
    root = resolve_datastore_dir(root_dir=datastore_root).resolve()
    try:
        requested = _session(session) if session else None
        if run_directory is not None:
            run = _run_path(root, Path(run_directory).resolve())
            if (not isinstance(expected_receipt_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", expected_receipt_sha256) is None):
                raise GameplanError("Frozen Gameplan requires its exact receipt hash")
            saved = _json(run / "receipt.json")
            selected = _session(saved.get("action_date"))
            if requested is not None and selected != requested:
                raise GameplanError(f"No verified local Gameplan is saved for {requested}")
            pointer = {"receipt_sha256": expected_receipt_sha256,
                       "source_receipt_sha256": saved.get("source_receipt_sha256")}
        else:
            if expected_receipt_sha256 is not None:
                raise GameplanError("A frozen receipt hash requires an explicit saved run")
            current = _latest(root)
            from ml.joint_capital_adoption import accepted_sessions, read_accepted_joint_plan
            combined_dates = accepted_sessions(root)
            combined_date = requested or (combined_dates[0] if combined_dates else None)
            accepted = read_accepted_joint_plan(root, combined_date) if combined_date else None
            joint_selected = bool(accepted and (requested or not current or combined_date >= current["action_date"]))
            account_pointer = _account_pointer(root, accepted_joint=joint_selected)
            # A damaged account publication still fails closed. Once verified, its
            # old session cannot shadow a newer local or accepted joint publication.
            account = _load_account_view(root, account_pointer) if account_pointer is not None else None
            if (joint_selected and account is not None and not requested
                    and combined_date < account.session):
                joint_selected = False
            if joint_selected:
                combined, binding, run = accepted
                forecasts = tuple(sorted((_forecast(row) for row in combined["forecasts"]),
                    key=lambda row: (row.start, HORIZONS.index(row.horizon), row.symbol, row.route)))
                return Gameplan(combined_date, _timestamp(combined["as_of"]), _timestamp(binding["accepted_at"]),
                    run, run / "joint-plan.json", forecasts, _actions(combined["ledger"], forecasts, combined_date),
                    "COMPLETE", "", "Combined plan: one account-wide budget; quantities remain subject to actual fills and cash.",
                    _execution_quotes(root, forecasts), combined["holding_policy"],
                    probability_target_contract(pd.DataFrame(combined["forecasts"])))
            if account is not None:
                if requested and requested <= account.session:
                    return _load_account_view(root, account_pointer, requested)
                if not requested and (not current or account.session >= current["action_date"]):
                    return account
            pointer = current if current and (requested is None or requested == current["action_date"]) else None
            if pointer:
                selected, run = pointer["action_date"], _run_path(root, pointer["run_path"])
            elif requested:
                candidates = [item for item in _history(root) if item[0] == requested]
                if not candidates:
                    raise GameplanError(f"No completed direction-ledger plan is saved for {requested}")
                selected, _, run = max(candidates, key=lambda item: (item[1], item[2].name))
            else:
                raise GameplanError("No saved Gameplan is available yet. Refresh after nightly planning finishes.")
        receipt_path = run / "receipt.json"
        receipt_hash = file_checksum(receipt_path)
        if pointer and receipt_hash != pointer.get("receipt_sha256"):
            raise GameplanError("Gameplan receipt does not match its latest pointer")
        receipt = _json(receipt_path)
        if (receipt.get("schema_version") != VERSION or receipt.get("status") != "COMPLETE"
                or receipt.get("action_date") != selected or _run_path(root, receipt.get("run_path")) != run
                or receipt.get("manifest_sha256") != file_checksum(run / "manifest.json")):
            raise GameplanError("Saved Gameplan receipt and manifest disagree")
        manifest = verify_manifest(run)
        required = {"trade-plan.parquet", "direction-ledger.json", "report.json", "Gameplan.md"}
        config = manifest.get("configuration", {})
        if (not required.issubset(manifest["output_files"]) or config.get("schema_version") != VERSION
                or config.get("action_date") != selected
                or config.get("source_receipt_sha256") != receipt.get("source_receipt_sha256")
                or (pointer and pointer.get("source_receipt_sha256") != receipt.get("source_receipt_sha256"))):
            raise GameplanError("Saved Gameplan manifest has an invalid plan contract")
        report = _json(run / "report.json")
        if (report.get("schema_version") != VERSION or report.get("status") != "COMPLETE"
                or report.get("action_date") != selected
                or report.get("source_receipt_sha256") != receipt.get("source_receipt_sha256")):
            raise GameplanError("Saved Gameplan report does not match its publication")
        frame = pd.read_parquet(run / "trade-plan.parquet")
        contract = probability_target_contract(frame)
        for metadata in (report, config, receipt):
            if "probability_target_contract" in metadata and probability_target_contract(metadata) != contract:
                raise GameplanError("Saved Gameplan probability targets disagree")
        if "gameplan_variant" in frame and not frame.gameplan_variant.eq(probability_target_metadata(contract)["gameplan_variant"]).all():
            raise GameplanError("Saved Gameplan variant disagrees with its probability target")
        if (len(frame) != receipt.get("forecast_rows") or len(frame) != report.get("forecast_rows")
                or frame.empty or frame.id.isna().any() or frame.id.duplicated().any()
                or frame.symbol.isna().any() or frame.duplicated(["symbol", "route"]).any()
                or not frame.action_date.astype(str).eq(selected).all()):
            raise GameplanError("Invalid saved forecast identities, counts or session")
        ledger = _json(run / "direction-ledger.json")
        if ledger.get("status") == SHARED_PROJECTION_UNAVAILABLE:
            binding = report.get("account_config_sha256")
            if report.get("publication_mode") == "RESEARCH_PRODUCER_SOURCE":
                if (any(item.get("publication_mode") != "RESEARCH_PRODUCER_SOURCE"
                        or item.get("producer_id") != "scout" or item.get("account_config_sha256") is not None
                        for item in (report, config, receipt)) or "account-snapshot.json" in manifest["output_files"]):
                    raise GameplanError("Research-only preparation has inconsistent producer bindings")
            elif (not isinstance(binding, str) or re.fullmatch(r"[a-f0-9]{64}", binding) is None
                    or any(item.get("publication_mode") != "ACCOUNT_PRODUCER_SOURCE" or item.get("account_config_sha256") != binding
                           for item in (config, receipt)) or "account-snapshot.json" in manifest["output_files"]):
                raise GameplanError("Producer-only preparation is missing its source-bound account configuration")
        from ml.stock_trader.cross_horizon_fallback import validate_fallback_policy
        fallback_policy = validate_fallback_policy(config.get("cross_horizon_fallback_policy"), selected)
        if any(metadata.get("cross_horizon_fallback_policy") != fallback_policy for metadata in (ledger, report, receipt)):
            raise GameplanError("Saved fallback policy bindings disagree")
        if fallback_policy is not None:
            from ml.stock_trader.cross_horizon_fallback import read_gameplan_fallback_policy
            source_ref = config.get("source_gameplan_run")
            if not isinstance(source_ref, str) or not source_ref:
                raise GameplanError("Saved fallback policy is missing its pinned source")
            source = (root / source_ref).resolve()
            if (source.parent != (root / "ml/nightly-gameplan-runs").resolve()
                    or report.get("source_gameplan_run") != source_ref
                    or receipt.get("source_gameplan_run") != source_ref
                    or file_checksum(source / "receipt.json") != receipt.get("source_receipt_sha256")
                    or read_gameplan_fallback_policy(source, selected) != fallback_policy):
                raise GameplanError("Saved fallback policy differs from its pinned source")
        projection_status, projection_note = _projection_metadata(ledger, report, frame)
        available = projection_status == "COMPLETE"
        forecasts = tuple(sorted((_forecast(row, projection_available=available,
                                           unavailable_reason=SHARED_PROJECTION_UNAVAILABLE if projection_status == SHARED_PROJECTION_UNAVAILABLE else "PRICE_REFERENCES_UNAVAILABLE")
                                  for row in frame.to_dict("records")),
                                 key=lambda row: (row.start, HORIZONS.index(row.horizon), row.symbol, row.route)))
        actions = _actions(ledger, forecasts, selected) if available else ()
        if file_checksum(receipt_path) != receipt_hash:
            raise GameplanError("Saved Gameplan changed while it was being read. Refresh again.")
        return Gameplan(selected, _timestamp(report["observed_at"]), _timestamp(receipt["completed_at"]),
                        run, run / "Gameplan.md", forecasts, actions, projection_status, projection_note,
                        _planning_note(report, set(frame.symbol)), _execution_quotes(root, forecasts),
                        ledger.get("holding_policy", "fixed_target_expiry"), contract)
    except GameplanError:
        raise
    except FileNotFoundError as exc:
        raise GameplanError("Saved Gameplan files are unavailable. Refresh after nightly planning finishes.") from exc
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        raise GameplanError(f"Could not verify the saved Gameplan: {exc}") from exc
