"""Read-only Atlas order projections from independent and preserved native ledgers.

The installed executor binds new activity to intraday/execution/holdings.sqlite3.
The older mixed ledger retains historical Atlas attempts. Explicit reservation
and allocation stream ownership, request fields and forecast proofs identify each
attempt; a symbol or 15m horizon never establishes ownership. No writer classes
are imported, and private account evidence is never returned to the UI.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3

from app.ui.scout_stats_data import _connect, _instant, _number, _session

LEDGER_PATHS = (Path("intraday/execution/holdings.sqlite3"),
                Path("state/independent-stock-trader/holdings.sqlite3"))
_STREAM = "atlas-15m"
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _object(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Expected saved object")
    return value


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _whole(value, *, positive=False):
    return type(value) is int and value >= (1 if positive else 0)


def _original(row, allocation, owners, account):
    request = _object(row["request"])
    reservation_owner = owners.get(("reservation", row["id"]))
    if request.get("stream") != _STREAM and (reservation_owner or {}).get("stream") != _STREAM:
        return None
    owner = owners.get(("allocation", row["allocation"]))
    if (not allocation or not reservation_owner or not owner
            or reservation_owner["stream"] != _STREAM or owner["stream"] != _STREAM
            or request.get("kind") != "intraday-account-intention" or request.get("stream") != _STREAM
            or request.get("horizon") != "15m" or allocation["horizon"] != "15m"
            or allocation["account"] != account or not _HASH.fullmatch(str(account))
            or not _text(row["id"]) or not _whole(row["quantity"], positive=True)
            or row["side"] not in {"BUY", "SELL"}
            or _number(row["price"]) in (None, 0)
            or any(request.get(key) != value for key, value in (
                ("symbol", allocation["symbol"]), ("side", row["side"]),
                ("quantity", row["quantity"]), ("forecast_sha256", request.get("forecast"))))
            or not _HASH.fullmatch(str(request.get("forecast")))
            or not _HASH.fullmatch(str(request.get("forecast_snapshot_sha256")))
            or _number(request.get("price")) != _number(row["price"])):
        raise ValueError("Conflicting native order ownership or request")
    proof = _object(reservation_owner["proof"])
    if any(proof.get(key) != request.get(field) for key, field in (
            ("forecast_id", "forecast"), ("forecast_sha256", "forecast_sha256"),
            ("forecast_snapshot_sha256", "forecast_snapshot_sha256"),
            ("reference_at", "start"), ("target_close_at", "end"))):
        raise ValueError("Native reservation forecast proof differs")
    start, end = _instant(request.get("start")), _instant(request.get("end"))
    if start is None or end is None or (end - start).total_seconds() != 45 * 60:
        raise ValueError("Invalid saved native target")
    # An allocation can own several later forecasts; its opening forecast/proof
    # must not be substituted for this reservation's exact request and proof.
    return dict(request_id=row["id"], forecast_id=request["forecast"],
                fill_identity_scope=row["id"],
                symbol=request["symbol"], side=row["side"], reference_at=request["start"],
                issued_at=None, quantity=row["quantity"], status="UNKNOWN", submission_state="UNKNOWN",
                filled_quantity=None, fills=(), observed_at=row["last_evidence_at"],
                broker_order_id=row["broker_order"], submitted_at=None)


def _fills(values, quantity, observed):
    if not isinstance(values, list):
        raise ValueError("Invalid saved fill list")
    unique = {}
    for value in values:
        if (not isinstance(value, dict) or not _text(value.get("fill_id"))
                or not _whole(value.get("quantity"), positive=True)
                or _number(value.get("price")) in (None, 0)
                or _instant(value.get("executed_at")) is None
                or _instant(value["executed_at"]) > observed):
            raise ValueError("Invalid saved fill")
        item = {key: value[key] for key in ("fill_id", "quantity", "price", "executed_at")}
        if item["fill_id"] in unique and unique[item["fill_id"]] != item:
            raise ValueError("Conflicting fill identity")
        unique[item["fill_id"]] = item
    if sum(value["quantity"] for value in unique.values()) != quantity:
        raise ValueError("Incomplete cumulative fill evidence")
    return tuple(unique.values())


def _lifecycle(order, row, account, submissions, observations, saved_fills, submitted_at):
    broker = row["broker_order"]
    receipts = submissions.get(row["id"], ())
    valid_receipts = []
    for item in receipts:
        if (item.get("status") not in {"SUBMITTED", "UNKNOWN", "REJECTED"}
                or _instant(item.get("observed_at")) is None
                or item.get("broker_order_id") not in (None, broker)):
            raise ValueError("Invalid native submission evidence")
        if item["status"] == "SUBMITTED" and not _text(item.get("broker_order_id")):
            raise ValueError("Accepted submission lacks order identity")
        valid_receipts.append(item)
    attempts = [item["observed_at"] for item in valid_receipts if item["status"] in {"SUBMITTED", "UNKNOWN"}]
    if submitted_at is not None:
        if _instant(submitted_at) is None:
            raise ValueError("Invalid saved submission timestamp")
        attempts.append(submitted_at)
    order["submitted_at"] = min(attempts, key=_instant) if attempts else None
    if _text(broker) and (row["status"] == "SUBMITTED" or any(
            item["status"] == "SUBMITTED" and item.get("broker_order_id") == broker for item in valid_receipts)):
        order["submission_state"] = "ACCEPTED"
    elif not _text(broker) and row["status"] == "REJECTED" and any(item["status"] == "REJECTED" for item in valid_receipts):
        if attempts or row["filled"] != 0 or observations.get(row["id"]):
            raise ValueError("No-send evidence conflicts with submission or fills")
        order.update(status="NOT_SENT", submission_state="NOT_SENT", filled_quantity=0,
                     note="Saved native submission was rejected before broker acceptance; no order was sent.")
        return order
    latest = observations.get(row["id"])
    if not latest:
        return order
    # Latest evidence is authoritative. A malformed latest record must never
    # fall back to an earlier successful observation.
    evidence = latest[1]
    status, filled = evidence.get("status"), evidence.get("cumulative_filled_quantity")
    observed = _instant(evidence.get("observed_at"))
    if (observed is None or not _text(broker)
            or row["last_evidence_at"] is not None and _instant(row["last_evidence_at"]) != observed
            or evidence.get("broker_order_id") != broker
            or evidence.get("account_fingerprint") != account
            or evidence.get("order_quantity") != row["quantity"]
            or status not in {"UNKNOWN", "WORKING", "PARTIAL", "FILLED", "CANCELLED", "REJECTED"}
            or status != row["status"] or not _whole(filled) or not 0 <= filled <= row["quantity"]
            or filled != row["filled"]
            or status == "FILLED" and filled != row["quantity"]
            or status == "PARTIAL" and not 0 < filled < row["quantity"]
            or status in {"WORKING", "REJECTED"} and filled != 0):
        raise ValueError("Native lifecycle differs from saved order")
    fills = _fills(evidence.get("fills"), filled, observed)
    if saved_fills is not None:
        canonical = {item["fill_id"]: item for item in saved_fills.get(row["id"], ())}
        if canonical != {item["fill_id"]: item for item in fills}:
            raise ValueError("Identified fills differ from native cumulative evidence")
    order.update(status=status, submission_state="ACCEPTED", filled_quantity=filled,
                 fills=fills, observed_at=evidence["observed_at"])
    return order


def _read_ledger(path, selected):
    orders, warnings, identities = [], [], set()
    with closing(_connect(path)) as db:
        db.row_factory = sqlite3.Row
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"reservations", "allocations", "intraday_streams", "metadata", "evidence"} <= tables:
            raise ValueError("Native ledger ownership schema is unavailable")
        row = db.execute("SELECT value FROM metadata WHERE key='account'").fetchone()
        account = row[0] if row else None
        if not _HASH.fullmatch(str(account)):
            raise ValueError("Native account ownership metadata is unavailable")
        allocations = {row["id"]: dict(row) for row in db.execute("SELECT id,account,symbol,horizon FROM allocations")}
        owners = {(row["kind"], row["id"]): dict(row) for row in db.execute("SELECT kind,id,stream,proof FROM intraday_streams")}
        submissions, observations, undecodable_order_at = {}, {}, 0
        for position, kind, raw in db.execute("SELECT rowid,kind,payload FROM evidence WHERE kind IN ('submission','order') ORDER BY rowid"):
            try:
                item = _object(raw)
                identity = item.get("reservation_id")
                if not _text(identity):
                    raise ValueError("Missing evidence reservation")
                if kind == "submission":
                    submissions.setdefault(identity, []).append(item)
                else:
                    # Native insertion order rejects stale observations during
                    # execution, and also ensures malformed newest times cannot
                    # silently disappear behind an older timestamp.
                    observations[identity] = (position, item)
            except (TypeError, ValueError, KeyError):
                if kind == "order":
                    undecodable_order_at = position
                warnings.append("A native evidence record is malformed; order/fill coverage is incomplete.")
        times = dict(db.execute("SELECT reservation,submitted_at FROM order_submission_times")) if "order_submission_times" in tables else {}
        fills = {} if "fills" in tables else None
        if fills is not None:
            for row in db.execute("SELECT id,reservation,quantity,price,executed_at FROM fills"):
                fills.setdefault(row["reservation"], []).append(dict(fill_id=row["id"], quantity=row["quantity"],
                    price=row["price"], executed_at=row["executed_at"]))
        else:
            warnings.append("Native identified-fill table is absent; coverage relies on readable saved order evidence.")
        for row in db.execute("SELECT id,allocation,side,quantity,price,filled,status,broker_order,request,last_evidence_at FROM reservations ORDER BY rowid"):
            identities.add(row["id"])
            try:
                order = _original(row, allocations.get(row["allocation"]), owners, account)
                if order is None or selected is not None and _session(order["reference_at"]) != selected:
                    continue
            except (TypeError, ValueError, KeyError):
                warnings.append("An Atlas native request failed ownership/forecast validation; order coverage is incomplete.")
                continue
            try:
                if undecodable_order_at and observations.get(row["id"], (0, None))[0] <= undecodable_order_at:
                    raise ValueError("Unattributed newer lifecycle evidence prevents an older result")
                order = _lifecycle(order, row, account, submissions, observations, fills, times.get(row["id"]))
            except (TypeError, ValueError, KeyError):
                order.update(status="UNKNOWN", filled_quantity=None, fills=())
                warnings.append("An Atlas order's saved lifecycle failed validation; its fill result remains unknown.")
            orders.append(order)
    return orders, warnings, identities, account


def atlas_orders(root: Path, selected: str | None):
    """Count each attributed reservation once; active independent bytes win."""
    root, orders, warnings, readable, present = Path(root), {}, [], 0, 0
    preferred_identities = set()
    broker_orders, fill_orders = {}, {}
    for relative in LEDGER_PATHS:
        path = root / relative
        if not path.is_file():
            continue
        present += 1
        try:
            rows, messages, identities, account = _read_ledger(path, selected)
            readable += 1
            warnings.extend(messages)
            for order in rows:
                if order["request_id"] in preferred_identities:
                    if orders.get(order["request_id"]) != order:
                        warnings.append("Duplicate native attempt conflicts across history; the independent ledger identity takes precedence.")
                    continue
                broker = order.get("broker_order_id")
                broker_key = (account, broker)
                if _text(broker) and broker_key in broker_orders:
                    warnings.append("A broker order identity appears under differing native requests; the preferred saved order is counted once.")
                    continue
                if _text(broker):
                    broker_orders[broker_key] = order["request_id"]
                for fill in tuple(order.get("fills", ())):
                    fill_key = (account, fill["fill_id"])
                    previous = fill_orders.get(fill_key)
                    if previous is not None and previous != order["request_id"]:
                        for affected in (orders[previous], order):
                            affected.update(status="UNKNOWN", filled_quantity=None, fills=(),
                                note="An identified fill conflicts with a different saved order; its fill result remains unknown.")
                        warnings.append("An identified fill appears on different broker orders; affected fill results remain unknown.")
                    else:
                        fill_orders[fill_key] = order["request_id"]
                orders[order["request_id"]] = order
            # Reserve every trustworthy original identity from the preferred
            # source, including damaged or foreign originals. A historical copy
            # cannot restore evidence contradicted by the independent ledger.
            preferred_identities.update(identities)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            warnings.append("An Atlas native order ledger could not be read; order/fill coverage is incomplete.")
    if present and not (root / LEDGER_PATHS[0]).is_file():
        warnings.append("The current independent Atlas ledger is missing; only preserved native history is available.")
    status = "missing" if not present else "unreadable" if not readable else "partial" if warnings else "read"
    return tuple(sorted(orders.values(), key=lambda row: (row["reference_at"], row["request_id"]))), status, tuple(dict.fromkeys(warnings))


def atlas_execution_sessions(root: Path):
    orders, _, _ = atlas_orders(root, None)
    return tuple(sorted({_session(row["reference_at"]) for row in orders}, reverse=True))
