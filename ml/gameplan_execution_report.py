"""Private, offline execution observations for the Gameplan Stats screen.

Only the dedicated Gameplan holdings ledger supplies order membership. Legacy
event files may enrich an exact ledger decision identity; they never supply new
orders. No ledger classes, broker adapters or prediction scorers are imported.
The JSON produced here is local account activity, not a forecast export.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "gameplan-execution-summary-v1"
REPORT_RELATIVE_PATH = Path("state/gameplan-execution-reports")
LEDGER_RELATIVE_PATH = Path("state/gameplan-trader/holdings.sqlite3")
STATUS_KEYS = ("fully_filled", "partially_filled", "working", "cancelled", "rejected", "unknown")
_PACIFIC = ZoneInfo("America/Los_Angeles")
_OPEN = {"RESERVED", "SUBMITTED", "WORKING", "PARTIAL", "UNKNOWN"}
_HORIZONS = {"1h", "4h", "1d", "1w"}
_STALE_SECONDS = 30 * 60


def _timestamp(value):
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Saved timestamps must include their timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat() if value is not None else None


def _ref(kind, value):
    if value is None:
        return None
    return hashlib.sha256(json.dumps([kind, str(value)], separators=(",", ":")).encode()).hexdigest()


def _json_object(value):
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("Expected a saved object")
    return result


def _quantity(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0 or result != result.to_integral_value():
        raise ValueError("Invalid recorded share quantity")
    return int(result)


def _price(value):
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("Invalid recorded fill price")
    return result


def _exact_event(root, identity):
    """Read only the event directory named by the ledger's idempotency key."""
    if not isinstance(identity, str) or not identity or Path(identity).name != identity or any(
        character in identity for character in ("/", "\\", ":")
    ):
        return {}, "Execution-result identity is unavailable."
    directory = root / "ml/stock-trader-execution-events" / identity
    if not directory.resolve().is_relative_to((root / "ml/stock-trader-execution-events").resolve()):
        return {}, "Execution-result location is outside its private event directory."
    result_path = directory / "result.json"
    if not result_path.is_file():
        return {}, None  # Older ledgers can have complete independent evidence.
    try:
        intent = _json_object((directory / "intent.json").read_text(encoding="utf-8"))
        if intent.get("decision_id") != identity:
            return {}, "Execution-result decision identity does not match."
        return _json_object(result_path.read_text(encoding="utf-8")), None
    except (OSError, ValueError, UnicodeError):
        return {}, "An exact execution-result record is unreadable."


def execution_summary_path(root, session):
    day = date.fromisoformat(str(session))
    if day.isoformat() != session:
        raise ValueError("Session must be an exact ISO Pacific date")
    return Path(root).resolve() / REPORT_RELATIVE_PATH / session / "summary.json"


def _base(session, now):
    day = date.fromisoformat(session)
    if day.isoformat() != session:
        raise ValueError("Session must be an exact ISO Pacific date")
    start = datetime.combine(day, time(), _PACIFIC).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time(), _PACIFIC).astimezone(timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION, "session_date": session,
        "timezone": "America/Los_Angeles", "generated_at": _iso(now),
        "state": "missing", "message": "Saved Gameplan execution ledger is unavailable.",
        "last_broker_observation_at": None, "counts": None, "orders": [],
        "coverage": {
            "start": _iso(start), "end": _iso(end), "reasons": [],
            "stale": False, "stale_after_seconds": _STALE_SECONDS,
            "scope": "Gameplan's dedicated ledger only; saved evidence, not a live broker check.",
            "population": "Order attempts submitted on this Pacific date; retries are separate attempts.",
            "carryover": "Earlier submissions are listed separately if filled on this date or still open; excluded from session attempt totals.",
            "outcomes": "Latest saved outcome, including observations and fills recorded after the selected date.",
            "distribution": "Exclusive lifecycle categories. Cancelled orders with fills count as cancelled, with filled shares retained.",
            "completeness": "Recorded ledger coverage does not establish that all broker activity was captured.",
            "attribution": "Exact dedicated-ledger reservation, allocation, broker and decision identities; no symbol/time/quantity matching.",
            "logical_intentions": "Only explicit retry links group attempts. Forecasts and catch-up components are not counted as individual orders.",
        },
        "provenance": {"source": LEDGER_RELATIVE_PATH.as_posix(),
            "reader": SCHEMA_VERSION, "private": True, "broker_requests": False,
            "query_snapshot": "SQLite mode=ro, query_only, one consistent BEGIN read transaction; optional exact decision-result files read separately.",
            "read_started_at": _iso(now), "read_completed_at": None},
    }, start, end


def build_execution_summary(root, session, observed_at=None):
    """Observe saved Gameplan execution without changing source databases.

    ``observed_at`` is the report-generation clock, never a claimed broker check.
    Missing/unreadable sources return explicit panel states with unknown counts.
    Staleness concerns still-open orders: terminal historical evidence does not
    become stale solely because the user views an old date.
    """
    now = _timestamp(observed_at) if observed_at is not None else datetime.now(timezone.utc)
    if now is None:
        raise ValueError("A report-generation timestamp is required")
    root = Path(root).resolve()
    report, start, end = _base(session, now)
    path = root / LEDGER_RELATIVE_PATH
    if not path.is_file():
        report["provenance"]["read_completed_at"] = _iso(datetime.now(timezone.utc))
        return report
    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"metadata", "allocations", "reservations", "fills", "evidence"}
        if not required.issubset(tables):
            report.update(state="incomplete", message="Saved ledger schema is incomplete; order totals are unavailable.")
            report["coverage"]["reasons"] = ["Missing required ledger tables."]
            return report
        raw = {name: [dict(row) for row in connection.execute(f'SELECT * FROM "{name}"')]
               for name in sorted(required | (tables & {"order_submission_times", "order_timeout_retries", "cancellations", "order_timeouts"}))}
        report["provenance"]["source_content_sha256"] = hashlib.sha256(
            json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        _populate(report, raw, root, start, end, now)
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, InvalidOperation, UnicodeError):
        # Source errors never claim no orders and never leak database/account text.
        report.update(state="unreadable", message="Saved Gameplan execution records could not be read consistently.", counts=None, orders=[])
        report["coverage"]["reasons"] = ["A saved ledger record is unreadable or has an unsupported schema."]
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()
        report["provenance"]["read_completed_at"] = _iso(datetime.now(timezone.utc))
    return report


def _populate(report, raw, root, start, end, now):
    issues = []
    metadata = {row["key"]: row["value"] for row in raw["metadata"]}
    account = metadata.get("account")
    if not account:
        report.update(state="incomplete", message="Dedicated ledger ownership metadata is missing; attribution is unavailable.")
        report["coverage"]["reasons"] = ["Missing dedicated ledger account binding."]
        return
    allocations = {row["id"]: row for row in raw["allocations"]}
    reservations = {row["id"]: row for row in raw["reservations"]}
    submissions = {row["reservation"]: _timestamp(row["submitted_at"]) for row in raw.get("order_submission_times", [])}
    retry_parents = {row["child"]: row["parent"] for row in raw.get("order_timeout_retries", [])}
    children = defaultdict(list)
    for child, parent in retry_parents.items():
        children[parent].append(child)
    cancellations = {row["reservation"]: row["requested_at"] for row in raw.get("cancellations", [])}
    timeouts = {row["reservation"]: row["requested_at"] for row in raw.get("order_timeouts", [])}
    evidence = defaultdict(list)
    for row in raw["evidence"]:
        if row["kind"] not in {"submission", "order"}:
            continue
        payload = _json_object(row["payload"])
        identity = payload.get("reservation_id")
        if identity not in reservations:
            issues.append("An order evidence record lacks an exact reservation match.")
            continue
        at = _timestamp(payload.get("observed_at"))
        evidence[identity].append((at, row["kind"], row["id"], payload))
    fills = defaultdict(list)
    seen_fills = {}
    for row in raw["fills"]:
        identity = row["id"]
        if identity in seen_fills:
            if row != seen_fills[identity]:
                issues.append("Conflicting fill identity records were excluded.")
            continue
        seen_fills[identity] = row
        if row["reservation"] not in reservations:
            issues.append("A fill lacks an exact reservation match.")
            continue
        fills[row["reservation"]].append(row)

    orders, excluded, without_date, stale_refs = [], 0, 0, []
    for identity, row in reservations.items():
        allocation = allocations.get(row["allocation"])
        if allocation is None or allocation["account"] != account or allocation["horizon"] not in _HORIZONS:
            excluded += 1
            continue
        request = _json_object(row["request"])
        quantity, cumulative = _quantity(row["quantity"]), _quantity(row["filled"])
        if cumulative > quantity or quantity <= 0 or str(row["side"]).upper() not in {"BUY", "SELL"}:
            raise ValueError("Invalid order quantities")
        lifecycle = str(row["status"]).upper()
        events = sorted(evidence[identity], key=lambda event: event[0] or datetime.min.replace(tzinfo=timezone.utc))
        own_events = []
        order_issues = []
        evidence_broker_ids = {str(event[3]["broker_order_id"]) for event in events
                              if event[3].get("broker_order_id")}
        if len(evidence_broker_ids) > 1:
            order_issues.append("Order evidence identity conflict across broker observations; outcome is unknown.")
        expected_broker_id = str(row["broker_order"]) if row["broker_order"] else (
            next(iter(evidence_broker_ids)) if len(evidence_broker_ids) == 1 else None)
        for at, kind, event_id, payload in events:
            if at is None:
                order_issues.append("Saved order/submission evidence has no observation timestamp; its outcome is unknown.")
                continue
            if kind == "order" and (payload.get("account_fingerprint") != account or
                not payload.get("broker_order_id") or
                str(payload.get("broker_order_id")) != expected_broker_id):
                order_issues.append("Order evidence identity conflict; outcome is unknown.")
                continue
            if kind == "submission" and payload.get("broker_order_id") and (
                expected_broker_id is None or str(payload["broker_order_id"]) != expected_broker_id
            ):
                order_issues.append("Submission evidence identity conflict; its submission date is unknown.")
                continue
            if kind == "submission" and payload.get("status") == "SUBMITTED" and not payload.get("broker_order_id"):
                order_issues.append("Submission evidence identity conflict: accepted submission lacks its broker identity.")
                continue
            own_events.append((at, kind, event_id, payload))
        latest_order = next((event for event in reversed(own_events) if event[1] == "order"), None)
        if latest_order and (str(latest_order[3].get("status")).upper() != lifecycle or
            _quantity(latest_order[3].get("order_quantity")) != quantity or
            _quantity(latest_order[3].get("cumulative_filled_quantity")) != cumulative):
            order_issues.append("Order evidence identity conflict or quantity/status mismatch; outcome is unknown.")
        submission_events = [event for event in own_events if event[1] == "submission"]
        submitted = submissions.get(identity)
        if submitted is None:
            submitted = next((event[0] for event in submission_events if event[3].get("status") in {"SUBMITTED", "UNKNOWN"}), None)
        result, result_issue = _exact_event(root, row["idempotency_key"])
        if result_issue:
            order_issues.append(result_issue)
        if result.get("broker_order_id") and expected_broker_id and str(result["broker_order_id"]) != expected_broker_id:
            order_issues.append("Execution-result broker identity conflicts with the ledger.")
            result = {}
        # Result timestamps are used only after exact decision identity joins.
        if submitted is None and result.get("status") in {"SUBMITTED", "SUBMISSION_FAILED_OR_UNKNOWN"}:
            submitted = _timestamp(result.get("completed_at"))
        not_submitted = result.get("status") == "NOT_SUBMITTED_SAFETY_CHECK" or any(
            event[2] == row["idempotency_key"] + ":not-submitted" and event[3].get("status") == "REJECTED"
            for event in submission_events)
        has_broker = bool(row["broker_order"] or (latest_order and latest_order[3].get("broker_order_id")))
        if not_submitted and (has_broker or submitted is not None or cumulative):
            order_issues.append("Conflicting submitted and not-submitted evidence; submission is unknown.")
            not_submitted = False
        submission_state = ("not_submitted" if not_submitted else "submitted" if has_broker else
                            "attempted_unknown" if submitted is not None else "unknown")
        parsed_fills = []
        for fill in fills[identity]:
            parsed_fills.append({"quantity": _quantity(fill["quantity"]), "price": str(_price(fill["price"])),
                                 "executed_at": _iso(_timestamp(fill["executed_at"]))})
        parsed_fills.sort(key=lambda fill: fill["executed_at"] or "")
        fill_quantity = sum(fill["quantity"] for fill in parsed_fills)
        fill_complete = fill_quantity == cumulative
        if not fill_complete:
            order_issues.append("Recorded cumulative fill quantity does not match the available fill details.")
        average = (str(sum(Decimal(fill["price"]) * fill["quantity"] for fill in parsed_fills) / fill_quantity)
                   if fill_quantity and fill_complete else None)
        last_at = latest_order[0] if latest_order else None
        activity_at = submitted or _timestamp(result.get("completed_at")) or next(
            (event[0] for event in reversed(own_events) if event[0]), None)
        fills_in_session = [fill for fill in parsed_fills if fill["executed_at"] and start <= _timestamp(fill["executed_at"]) < end]
        if submitted is not None and start <= submitted < end:
            scope = "submitted"
        elif not_submitted and activity_at is not None and start <= activity_at < end:
            scope = "not_submitted"
        elif submitted is not None and submitted < start and (fills_in_session or lifecycle in _OPEN):
            scope = "carryover"
        elif submitted is None and (fills_in_session or activity_at is not None and start <= activity_at < end):
            scope = "unattributed"
            without_date += 1
        elif submitted is None and has_broker:
            without_date += 1
            continue
        else:
            continue
        if not_submitted:
            status = "not_submitted"
        elif order_issues and any("identity conflict" in issue or "no observation timestamp" in issue for issue in order_issues):
            status = "unknown"
        elif lifecycle == "CANCELLED":
            status = "cancelled" if latest_order else "unknown"
        elif lifecycle == "REJECTED":
            status = "rejected" if latest_order and has_broker else "unknown"
        elif lifecycle == "FILLED" and cumulative == quantity and fill_complete and latest_order:
            status = "fully_filled"
        elif lifecycle in {"PARTIAL", "WORKING", "SUBMITTED"} and 0 < cumulative < quantity and latest_order:
            status = "partially_filled"
        elif lifecycle in {"WORKING", "SUBMITTED"} and not cumulative:
            status = "working"
        else:
            status = "unknown"
        if not not_submitted and latest_order is None:
            order_issues.append("No saved broker order observation is available for this attempt.")
        order_ref = _ref("reservation", identity)
        # Still-open records remain visibly stale; this is display information,
        # never a condition on any trader or nightly preparation.
        if lifecycle in _OPEN and not not_submitted and (last_at is None or (now-last_at).total_seconds() > _STALE_SECONDS):
            stale_refs.append(order_ref)
        reasons = []
        if latest_order:
            payload = latest_order[3]
            reasons += [str(payload[key]) for key in ("broker_status", "broker_status_description") if payload.get(key)]
        if result.get("error"):
            reasons.append(str(result["error"]))
        reasons += order_issues
        # The summary is private, but raw account/broker identifiers are still
        # unnecessary for UI inspection or its selected-order details.
        secrets = [str(value) for value in (account, row["broker_order"]) if value]
        for secret in secrets:
            reasons = [reason.replace(secret, "[private identity]") for reason in reasons]
        parent = retry_parents.get(identity)
        if parent is not None and parent not in reservations:
            order_issues.append("Retry parent record is unavailable.")
            reasons.append("Retry parent record is unavailable.")
        orders.append({"order_ref": order_ref, "allocation_ref": _ref("allocation", row["allocation"]),
            "forecast_ref": _ref("forecast", request.get("forecast", allocation["forecast"])),
            "symbol": allocation["symbol"], "side": str(row["side"]).upper(),
            "requested_quantity": quantity, "filled_quantity": cumulative,
            "filled_quantity_in_session": sum(fill["quantity"] for fill in fills_in_session),
            "average_fill_price": average, "fill_details_complete": fill_complete, "fills": parsed_fills,
            "status": status, "lifecycle_status": lifecycle, "submission_state": submission_state,
            "submitted_at": _iso(submitted), "last_observed_at": _iso(last_at),
            "recorded_at": _iso(activity_at), "session_scope": scope,
            "retry_parent_ref": _ref("reservation", parent),
            "retry_child_refs": [_ref("reservation", child) for child in sorted(children[identity])],
            "cancel_requested_at": cancellations.get(identity), "timeout_requested_at": timeouts.get(identity),
            "reasons": list(dict.fromkeys(reasons)), "is_catchup": str(request.get("forecast", allocation["forecast"])).startswith("catchup:")})
        issues.extend(order_issues)

    if excluded:
        issues.append(f"{excluded} reservation(s) lack reliable Gameplan allocation attribution and were excluded.")
    if without_date:
        issues.append(f"{without_date} order(s) lack a recorded submission date and are excluded from submitted-session totals.")
    orders.sort(key=lambda order: (order["submitted_at"] or order["recorded_at"] or "", order["order_ref"]), reverse=True)
    report["orders"] = orders
    # Last observation describes displayed orders; unrelated dates cannot make a
    # selected session appear freshly observed.
    displayed_times = [_timestamp(order["last_observed_at"]) for order in orders if order["last_observed_at"]]
    report["last_broker_observation_at"] = _iso(max(displayed_times)) if displayed_times else None
    report["coverage"].update(reasons=list(dict.fromkeys(issues)), stale=bool(stale_refs),
        stale_order_refs=stale_refs, excluded_unattributed_reservations=excluded,
        orders_without_submission_date=without_date, recorded_orders=len(orders))
    if not orders:
        report.update(state="incomplete", message="No attributable execution records cover this Pacific session; order totals are unknown.")
        return
    counts = {key: 0 for key in STATUS_KEYS}
    submitted_orders = [order for order in orders if order["session_scope"] == "submitted"]
    for order in submitted_orders:
        counts[order["status"] if order["status"] in STATUS_KEYS else "unknown"] += 1
    counts.update(submitted_attempts=len(submitted_orders),
        not_submitted=sum(order["submission_state"] == "not_submitted" for order in orders),
        unknown_submission=sum(order["submission_state"] in {"attempted_unknown", "unknown"} for order in orders),
        carryover_attempts=sum(order["session_scope"] == "carryover" for order in orders))
    report["counts"] = counts
    if stale_refs:
        report.update(state="stale", message="Some open attempts have no broker observation within 30 minutes of report generation; saved outcomes are shown.")
    elif issues:
        report.update(state="incomplete", message="Saved execution coverage is incomplete; only recorded attributed attempts are shown.")
    else:
        report.update(state="available", message="Saved Gameplan execution evidence; latest recorded outcomes, not a live broker check.")


def publish_execution_summary(root, session, observed_at=None):
    """Atomically save a private sidecar, outside all forecast-package paths."""
    report = build_execution_summary(root, session, observed_at=observed_at)
    destination = execution_summary_path(root, session)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".summary-", suffix=".json", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore-root", type=Path, required=True)
    parser.add_argument("--session", required=True, help="Pacific date, YYYY-MM-DD")
    args = parser.parse_args(argv)
    path = publish_execution_summary(args.datastore_root, args.session)
    print(json.dumps({"summary_path": str(path), "private": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
