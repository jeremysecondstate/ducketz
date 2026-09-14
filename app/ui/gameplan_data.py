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
from zoneinfo import ZoneInfo

import pandas as pd

from datafetching.parquet_store import resolve_datastore_dir
from ml.artifacts import file_checksum, verify_manifest

VERSION = "cash-aware-gameplan-trade-planning-v4"
LEDGER_VERSION = "direction-based-gameplan-cash-ledger-v1"
UNAVAILABLE_PROJECTION = "UNAVAILABLE_PRICE_REFERENCES"
PACIFIC = ZoneInfo("America/Los_Angeles")
HORIZONS = ("1h", "4h", "1d", "1w")
REASONS = {
    "BULLISH_BUY": "Bullish entry", "BEARISH_SELL": "Bearish sale",
    "HORIZON_EXIT": "Horizon exit", "LATER_EXPIRY": "Later horizon expiry",
    "UNRESOLVED_EXPIRY": "Unresolved horizon expiry", "NEUTRAL": "Neutral forecast",
    "NO_AVAILABLE_SHARES_FOR_THIS_HORIZON": "No eligible shares in this horizon",
    "NON_ENTRY_CONTEXT": "Forecast context only", "MODEL_NOT_PROMOTED": "Model not promoted",
    "SYMBOL_ALLOCATION_UNRESOLVED": "Allocation needs resolution",
    "HORIZON_BUY_ALREADY_PENDING": "Buy already pending in this horizon",
    "HORIZON_POSITION_ALREADY_HELD": "Position already held in this horizon",
    "INSUFFICIENT_CASH_OR_ALLOCATION_FOR_ONE_SHARE": "Insufficient cash or allocation",
    "PRICE_REFERENCES_UNAVAILABLE": "Cash projection unavailable",
}


class GameplanError(ValueError):
    """A saved plan is missing, incomplete, unsupported or inconsistent."""


def reason_text(reason: str) -> str:
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
    sessions = {item[0] for item in _history(root)}
    current = _latest(root)
    if current:
        sessions.add(current["action_date"])
    return tuple(sorted(sessions, reverse=True))


def _forecast(row: dict, *, projection_available: bool = True) -> PlanForecast:
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
        reason = "PRICE_REFERENCES_UNAVAILABLE" if eligible else "NON_ENTRY_CONTEXT"
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
    return PlanForecast(str(row["id"]), str(row["symbol"]), horizon, str(row["route"]),
                        str(row["target_role"]), eligible, str(row["model_status"]), probability,
                        direction, action, quantity, reason, start, end,
                        price)


def _projection_metadata(ledger: dict, report: dict, frame: pd.DataFrame) -> tuple[str, str]:
    status = ledger.get("status")
    if report.get("direction_projection_status", "COMPLETE") != status:
        raise GameplanError("Saved report and direction ledger projection statuses disagree")
    if status == "COMPLETE":
        return status, ""
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
    symbols = ", ".join(sorted({point["symbol"] for point in points}))
    return status, f"Cash projection unavailable: missing price references for {symbols}. Saved forecasts remain available."


def _actions(ledger: dict, forecasts: tuple[PlanForecast, ...], session: str) -> tuple[PlannedAction, ...]:
    if ledger.get("version") != LEDGER_VERSION or ledger.get("status") != "COMPLETE":
        raise GameplanError("The direction ledger is not complete or supported")
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
        if (action, reason) not in {("BUY", "BULLISH_BUY"), ("SELL", "BEARISH_SELL"), ("SELL", "HORIZON_EXIT")}:
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
        actions.append(PlannedAction(f"trade:{sequence}", sequence, when, symbol, horizon, action,
                                     quantity, price, reason, identifier))
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
    for index, lot in enumerate(ledger["ending_allocations"]):
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


def load_gameplan(datastore_root: Path | None = None, session: str | None = None) -> Gameplan:
    root = resolve_datastore_dir(root_dir=datastore_root).resolve()
    try:
        requested = _session(session) if session else None
        current = _latest(root)
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
        if (len(frame) != receipt.get("forecast_rows") or len(frame) != report.get("forecast_rows")
                or frame.empty or frame.id.isna().any() or frame.id.duplicated().any()
                or frame.symbol.isna().any() or frame.duplicated(["symbol", "route"]).any()
                or not frame.action_date.astype(str).eq(selected).all()):
            raise GameplanError("Invalid saved forecast identities, counts or session")
        ledger = _json(run / "direction-ledger.json")
        projection_status, projection_note = _projection_metadata(ledger, report, frame)
        available = projection_status == "COMPLETE"
        forecasts = tuple(sorted((_forecast(row, projection_available=available) for row in frame.to_dict("records")),
                                 key=lambda row: (row.start, HORIZONS.index(row.horizon), row.symbol, row.route)))
        actions = _actions(ledger, forecasts, selected) if available else ()
        if file_checksum(receipt_path) != receipt_hash:
            raise GameplanError("Saved Gameplan changed while it was being read. Refresh again.")
        return Gameplan(selected, _timestamp(report["observed_at"]), _timestamp(receipt["completed_at"]),
                        run, run / "Gameplan.md", forecasts, actions, projection_status, projection_note,
                        _planning_note(report, set(frame.symbol)))
    except GameplanError:
        raise
    except FileNotFoundError as exc:
        raise GameplanError("Saved Gameplan files are unavailable. Refresh after nightly planning finishes.") from exc
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError) as exc:
        raise GameplanError(f"Could not verify the saved Gameplan: {exc}") from exc
