"""Read saved private Gameplan execution summaries without touching a ledger.

Refresh reloads a JSON snapshot only: it never observes a broker, scores a
forecast, or changes execution state. An absent snapshot does not mean zero
orders. Historical terminal evidence does not become stale simply with age.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from datafetching.parquet_store import resolve_datastore_dir


VERSION = "gameplan-execution-summary-v1"
PACIFIC = ZoneInfo("America/Los_Angeles")
STATES = {"available", "missing", "stale", "incomplete", "unreadable"}
STATUSES = ("fully_filled", "partially_filled", "working", "cancelled", "rejected", "unknown")
COUNT_FIELDS = (*STATUSES, "submitted_attempts", "not_submitted", "unknown_submission", "carryover_attempts")
CURRENT_SESSION_MAX_AGE = timedelta(minutes=30)
OPEN_LIFECYCLES = {"RESERVED", "SUBMITTED", "WORKING", "PARTIAL", "UNKNOWN"}


@dataclass(frozen=True)
class GameplanExecutionSnapshot:
    session: str
    state: str
    message: str
    report_path: Path | None = None
    generated_at: datetime | None = None
    observed_at: datetime | None = None
    report: dict | None = None

    @property
    def orders(self) -> tuple[dict, ...]:
        return tuple(self.report.get("orders", ())) if self.report else ()

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted({row["symbol"] for row in self.orders}))

    @property
    def counts(self) -> dict | None:
        return self.report.get("counts") if self.report else None

    @property
    def coverage(self) -> dict:
        return self.report.get("coverage", {}) if self.report else {}


def _timestamp(value, *, optional=False) -> datetime | None:
    if value is None and optional:
        return None
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Execution timestamp has no timezone")
    return result


def _quantity(value, *, optional=False) -> None:
    if value is None and optional:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid saved execution quantity")


def _price(value) -> None:
    if value is None:
        return
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Invalid saved execution price") from exc
    if isinstance(value, bool) or not number.is_finite() or number <= 0:
        raise ValueError("Invalid saved execution price")


def _validate(report: object, session: str) -> tuple[datetime, datetime | None]:
    if (not isinstance(report, dict) or report.get("schema_version") != VERSION
            or report.get("session_date") != session or report.get("timezone") != "America/Los_Angeles"
            or report.get("state") not in STATES or not isinstance(report.get("message"), str)
            or not isinstance(report.get("coverage"), dict) or not isinstance(report.get("provenance"), dict)):
        raise ValueError("Invalid saved execution summary")
    generated = _timestamp(report.get("generated_at"))
    observed = _timestamp(report.get("last_broker_observation_at"), optional=True)
    orders, counts = report.get("orders"), report.get("counts")
    if not isinstance(orders, list):
        raise ValueError("Invalid saved order list")
    if counts is None:
        if orders or report["state"] not in {"missing", "unreadable", "incomplete"}:
            raise ValueError("Execution totals are missing without a coverage explanation")
    elif (not isinstance(counts, dict) or any(
            type(counts.get(key)) is not int or counts[key] < 0 for key in COUNT_FIELDS)):
        raise ValueError("Invalid saved execution totals")
    refs = set()
    for order in orders:
        if (not isinstance(order, dict) or not isinstance(order.get("order_ref"), str)
                or not order["order_ref"] or order["order_ref"] in refs
                or not isinstance(order.get("symbol"), str) or not order["symbol"]
                or order.get("side") not in {"BUY", "SELL"}
                or order.get("status") not in {*STATUSES, "not_submitted"}
                or not isinstance(order.get("lifecycle_status"), str)
                or order.get("session_scope") not in {"submitted", "carryover", "unattributed", "not_submitted"}
                or not isinstance(order.get("submission_state"), str)):
            raise ValueError("Invalid or duplicate saved order")
        refs.add(order["order_ref"])
        _quantity(order.get("requested_quantity"))
        _quantity(order.get("filled_quantity"))
        if order["requested_quantity"] <= 0 or order["filled_quantity"] > order["requested_quantity"]:
            raise ValueError("Saved filled shares exceed the requested shares")
        _price(order.get("average_fill_price"))
        for field in ("submitted_at", "last_observed_at"):
            _timestamp(order.get(field), optional=True)
        if (not isinstance(order.get("fills"), list) or not isinstance(order.get("reasons"), list)
                or any(not isinstance(reason, str) for reason in order["reasons"])
                or not isinstance(order.get("retry_child_refs"), list)
                or any(not isinstance(ref, str) for ref in order["retry_child_refs"])
                or (order.get("retry_parent_ref") is not None and not isinstance(order["retry_parent_ref"], str))):
            raise ValueError("Invalid saved order details")
        for fill in order["fills"]:
            if not isinstance(fill, dict):
                raise ValueError("Invalid saved fill")
            _quantity(fill.get("quantity"))
            _price(fill.get("price"))
            _timestamp(fill.get("executed_at"), optional=True)
    if counts is not None:
        submitted = [order for order in orders if order["session_scope"] == "submitted"]
        if (counts["submitted_attempts"] != len(submitted)
                or sum(counts[key] for key in STATUSES) != counts["submitted_attempts"]
                or any(counts[key] != sum(order["status"] == key for order in submitted) for key in STATUSES)):
            raise ValueError("Saved execution totals disagree with order evidence")
    return generated, observed


def execution_sessions(datastore_root: Path | None = None) -> tuple[str, ...]:
    """List only dated private summaries, without requiring prediction results.

    Invalid names and inaccessible entries cannot hide other saved sessions.
    JSON validation belongs to the selected-session loader so an unreadable
    report remains selectable with its explicit unavailable state.
    """
    try:
        directory = (resolve_datastore_dir(root_dir=datastore_root) / "state/gameplan-execution-reports").resolve()
        entries = tuple(directory.iterdir())
    except (OSError, ValueError, TypeError):
        return ()
    sessions = set()
    for entry in entries:
        try:
            session = entry.name
            summary = entry / "summary.json"
            if (date.fromisoformat(session).isoformat() == session and entry.resolve().parent == directory
                    and summary.resolve().parent == entry.resolve() and summary.is_file()):
                sessions.add(session)
        except (OSError, ValueError, TypeError):
            continue
    return tuple(sorted(sessions, reverse=True))


def load_gameplan_execution(datastore_root: Path | None, session: str, *, now=None) -> GameplanExecutionSnapshot:
    """Load one Pacific session's private JSON, returning explicit coverage state.

    Unresolved attempts age stale after thirty minutes without an observation,
    even when their submission session is historical. Current-session snapshots
    also age stale; past-session age alone never stales terminal orders.
    """
    report_path = None
    try:
        if date.fromisoformat(session).isoformat() != session:
            raise ValueError("Invalid Pacific session date")
        root = resolve_datastore_dir(root_dir=datastore_root)
        report_path = root / "state/gameplan-execution-reports" / session / "summary.json"
        try:
            contents = report_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return GameplanExecutionSnapshot(session, "missing",
                "No saved execution summary for this Pacific session. Missing records do not mean zero orders.", report_path)
        report = json.loads(contents)
        generated, observed = _validate(report, session)
        state, message = report["state"], report["message"]
        current = datetime.now(timezone.utc) if now is None else _timestamp(now)
        unresolved = [order for order in report["orders"]
                      if order["lifecycle_status"] in OPEN_LIFECYCLES
                      and order["submission_state"] != "not_submitted"]
        if (state in {"available", "incomplete"} and any(
                order["last_observed_at"] is None
                or current - _timestamp(order["last_observed_at"]) > CURRENT_SESSION_MAX_AGE
                for order in unresolved)):
            state = "stale"
            message = ("Some unresolved attempts have saved broker observations older than 30 minutes or none. "
                       "No live broker check was made. " + message)
        elif (state == "available" and current.astimezone(PACIFIC).date().isoformat() == session
                and current - generated > CURRENT_SESSION_MAX_AGE):
            state = "stale"
            message = "Saved execution summary is over 30 minutes old for this session. No live broker check was made."
        return GameplanExecutionSnapshot(session, state, message, report_path, generated, observed, report)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return GameplanExecutionSnapshot(session, "unreadable",
            "Saved execution summary could not be read or validated. Execution totals are unavailable.", report_path)
