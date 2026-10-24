"""Small, read-only Scout 15-minute projections for Gameplan Stats.

Only saved live forecasts and Scout's registered private order journals are read.
This reader deliberately imports no acquisition, model, execution or store writer.
It projects scalar forecast fields in SQL; fitted inputs and model arrays never
leave SQLite. Prediction outcome revisions and cumulative order evidence each
contribute once. Forecast returns are never treated as trading profit.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
TARGET_VERSION = "intraday-15m-open-to-third-close-not-down-v1"
_FORECAST_FIELDS = ("pc", "symbol", "forecast_id", "mode", "target_version", "target_open",
                    "target_close", "probability_not_down", "reference_at", "issued_at")
_STATUSES = {"scored", "pending", "missing", "unscorable", "synthetic"}


@dataclass(frozen=True)
class ScoutStatsSnapshot:
    session: str | None = None
    status: str = "missing"
    message: str = "No saved Scout 15-Minute results loaded."
    forecasts: tuple[dict, ...] = ()
    orders: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()
    prediction_status: str = "missing"
    execution_status: str = "missing"


def _root(value):
    configured = os.environ.get("DUCKETS_DATASTORE_DIR", "").strip() or os.environ.get("DUCKETS_OHLCV_PARQUET_DIR", "").strip()
    return Path(value if value is not None else configured or "C:/DATASTORE").expanduser().resolve()


def _instant(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _session(value):
    stamp = _instant(value)
    return stamp.astimezone(PACIFIC).date().isoformat() if stamp else None


def _number(value, *, probability=False):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number and (not probability or number <= 1) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _digest(value, *, ensure_ascii=False):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=ensure_ascii, allow_nan=False).encode()).hexdigest()


def _checked(raw, digest):
    value = json.loads(raw)
    if not isinstance(value, dict) or _digest(value) != digest:
        raise ValueError("Saved evidence digest differs")
    return value


def _connect(path):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def _forecast_projection(db):
    paths = ",".join("'$." + field + "'" for field in _FORECAST_FIELDS)
    # json_valid keeps one damaged payload from hiding all other saved rows.
    query = ("SELECT id,pc,symbol,reference_at,issued_at, CASE WHEN json_valid(payload) "
             "THEN json_extract(payload," + paths + ") END FROM forecasts WHERE pc='Scout' ORDER BY issued_at,id")
    for fid, pc, symbol, reference, issued, projected in db.execute(query):
        if projected is None:
            yield None
            continue
        value = dict(zip(_FORECAST_FIELDS, json.loads(projected)))
        if value.get("mode") != "live":
            continue
        if any(value.get(key) != indexed for key, indexed in
               (("forecast_id", fid), ("pc", pc), ("symbol", symbol), ("reference_at", reference), ("issued_at", issued))):
            yield None
            continue
        value["supported_target"] = value.get("target_version") == TARGET_VERSION
        yield value


def _predictions(root, selected, now, *, discovery=False):
    path = root / "intraday/shadow.sqlite3"
    if not path.is_file():
        return (), "missing", ("Scout prediction database is missing.",)
    rows, warnings = [], []
    try:
        with closing(_connect(path)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for forecast in _forecast_projection(db):
                if forecast is None:
                    warnings.append("A saved Scout forecast is malformed; prediction coverage is incomplete.")
                    continue
                if discovery:
                    rows.append(forecast)
                    continue
                reference_session = _session(forecast.get("reference_at"))
                if reference_session is None:
                    warnings.append("A saved Scout forecast has no usable reference date; session coverage is incomplete.")
                    continue
                if selected is not None and reference_session != selected:
                    continue
                outcome = None
                if "outcomes" in tables:
                    saved = db.execute("SELECT id,forecast_id,observed_at,payload FROM outcomes WHERE forecast_id=? ORDER BY rowid DESC LIMIT 1",
                                       (forecast["forecast_id"],)).fetchone()
                    if saved:
                        try:
                            key, fid, observed, raw = saved
                            value = json.loads(raw)
                            evidence = dict(value)
                            revision = evidence.pop("revision_id", None)
                            observation = evidence.pop("observed_at", None)
                            if (revision != key or observation != observed or fid != forecast["forecast_id"]
                                    or value.get("forecast_id") != fid or _digest(evidence, ensure_ascii=True) != key):
                                raise ValueError("Outcome identity differs")
                            outcome = value
                        except (TypeError, ValueError, KeyError):
                            warnings.append("A latest saved outcome is unreadable; its forecast remains missing.")
                status = (outcome or {}).get("status", "missing")
                end = _instant(forecast.get("target_close"))
                if end is not None and now < end:
                    status = "pending"
                elif status == "pending":
                    status = "missing"
                if status not in _STATUSES:
                    status = "missing"
                forecast.update(outcome_status=status, target_not_down=(outcome or {}).get("target_not_down"),
                                outcome_observed_at=(outcome or {}).get("observed_at"),
                                target_close_price=(outcome or {}).get("target_close_price"),
                                unavailable_reason=(outcome or {}).get("unavailable_reason"))
                rows.append(forecast)
            if not discovery and "outcomes" not in tables:
                warnings.append("Scout outcome table is missing; mature forecasts have no saved actual results.")
        return tuple(rows), "partial" if warnings else "read", tuple(dict.fromkeys(warnings))
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return (), "unreadable", ("Scout prediction database could not be read; prediction results are unavailable.",)


def _journal_items(root):
    scout = root / "intraday/scout"
    path = scout / "inventory.json"
    if not path.is_file():
        return (), "missing", ("Scout order-journal inventory is missing; order/fill results are unavailable.",)
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        inventory = envelope["inventory"]
        if (not isinstance(inventory, dict) or inventory.get("version") != "scout-session-books-v1"
                or _digest(inventory) != envelope["sha256"]
                or inventory.get("stream") != "scout-15m"):
            raise ValueError("Inventory identity differs")
        sessions = inventory["sessions"]
        if not isinstance(sessions, dict):
            raise ValueError("Invalid sessions")
        items = []
        for session, item in sessions.items():
            if not isinstance(item, dict):
                raise ValueError("Invalid journal inventory entry")
            filename = item["filename"]
            binding = item["binding"]
            if (not isinstance(binding, dict) or filename != _digest({"session_id": session}) + ".sqlite3"
                    or binding.get("session_id") != session
                    or any(binding.get(field) != inventory.get(field)
                           for field in ("account_fingerprint", "participant_id", "stream"))):
                raise ValueError("Journal binding differs")
            items.append((scout / "journals" / filename, item))
        registered = {path.name for path, _ in items}
        actual = {path.name for path in (scout / "journals").glob("*.sqlite3")}
        warnings = ("Unregistered Scout order journals exist; order/fill coverage is incomplete.",) if actual - registered else ()
        return tuple(items), "partial" if warnings else "read", warnings
    except (OSError, ValueError, TypeError, KeyError):
        return (), "unreadable", ("Scout order-journal inventory failed validation; order/fill results are unavailable.",)


def _request_session(request):
    if not isinstance(request, dict):
        raise ValueError("Invalid saved order request")
    reference, issued = (_instant(request.get(key)) for key in ("reference_at", "issued_at"))
    if reference is None or issued is None:
        raise ValueError("Saved order request timestamps are invalid")
    return reference.astimezone(PACIFIC).date().isoformat()


def _project_order(rid, record, lifecycle):
    request = record["request"]
    _request_session(request)
    if request["request_id"] != rid or record["request_sha256"] != _digest(request):
        raise ValueError("Request identity differs")
    state = record["state"]
    if state not in {"PREPARED", "GRANTED", "NOT_SENT", "SENDING", "UNKNOWN", "ACCEPTED"}:
        raise ValueError("Invalid saved submission state")
    grant = record.get("grant")
    if grant is not None and (not isinstance(grant, dict) or record.get("grant_sha256") != _digest(grant)):
        raise ValueError("Grant identity differs")
    quantity = (grant or {}).get("quantity", request.get("quantity"))
    if type(quantity) is not int or quantity < 0:
        raise ValueError("Order quantity is invalid")
    status = "NOT_SENT" if state in {"PREPARED", "GRANTED", "NOT_SENT"} else "UNKNOWN"
    evidence = (lifecycle or {}).get("evidence")
    filled, fills, observed = (0 if status == "NOT_SENT" else None), (), None
    if state == "ACCEPTED" and evidence:
        if not isinstance(evidence, dict):
            raise ValueError("Invalid saved lifecycle evidence")
        if (evidence.get("broker_order_id") != record.get("broker_order_id")
                or evidence.get("account_fingerprint") != request.get("account_fingerprint")
                or evidence.get("order_quantity") != quantity
                or evidence.get("reservation_id") != (grant or {}).get("reservation_id")):
            raise ValueError("Order evidence binding differs")
        status = evidence.get("status")
        if status not in {"FILLED", "PARTIAL", "WORKING", "CANCELLED", "REJECTED"}:
            raise ValueError("Unknown order lifecycle status")
        filled = evidence.get("cumulative_filled_quantity")
        if (type(filled) is not int or not 0 <= filled <= quantity
                or status == "FILLED" and filled != quantity
                or status == "PARTIAL" and not 0 < filled < quantity
                or status in {"WORKING", "REJECTED"} and filled != 0):
            raise ValueError("Fill quantity is invalid")
        unique = {}
        saved_fills = evidence.get("fills", [])
        if not isinstance(saved_fills, list):
            raise ValueError("Invalid saved fill list")
        for fill in saved_fills:
            if not isinstance(fill, dict):
                raise ValueError("Invalid saved fill")
            key = fill.get("fill_id")
            if (not isinstance(key, str) or not key or type(fill.get("quantity")) is not int or fill["quantity"] <= 0
                    or _number(fill.get("price")) in (None, 0) or _instant(fill.get("executed_at")) is None):
                raise ValueError("Fill evidence is invalid")
            if key in unique and unique[key] != fill:
                raise ValueError("Fill identity differs")
            unique[key] = {name: fill[name] for name in ("fill_id", "quantity", "price", "executed_at")}
        if sum(fill["quantity"] for fill in unique.values()) != filled:
            raise ValueError("Fill quantity differs from identified fills")
        fills, observed = tuple(unique.values()), evidence.get("observed_at")
        if _instant(observed) is None:
            raise ValueError("Observation time is invalid")
    return dict(request_id=rid, forecast_id=request.get("forecast_id"), symbol=request.get("symbol"),
                side=request.get("side"), reference_at=request.get("reference_at"), issued_at=request.get("issued_at"),
                quantity=quantity, status=status, submission_state=state, filled_quantity=filled, fills=fills,
                observed_at=observed, broker_order_id=record.get("broker_order_id"))


def _orders(root, selected):
    items, status, original_warnings = _journal_items(root)
    if status in {"missing", "unreadable"}:
        return (), status, original_warnings
    warnings, orders = list(original_warnings), {}
    for path, item in items:
        if not path.is_file():
            warnings.append("A registered Scout order journal is missing; order/fill coverage is incomplete.")
            continue
        try:
            with closing(_connect(path)) as db:
                control_row = db.execute("SELECT payload,sha256 FROM control WHERE id=1").fetchone()
                control = _checked(*control_row) if control_row else {}
                if control.get("binding") != item["binding"] or control.get("journal_id") != item.get("journal_id"):
                    raise ValueError("Original journal identity differs")
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for rid, raw, digest in db.execute("SELECT request_id,payload,sha256 FROM records ORDER BY request_id"):
                    try:
                        record = _checked(raw, digest)
                        request = record.get("request", {})
                        request_session = _request_session(request)
                        if any(request.get(key) != item["binding"].get(key) for key in
                               ("session_id", "account_fingerprint", "participant_id", "stream")):
                            raise ValueError("Wrong order binding")
                        if selected is not None and request_session != selected:
                            continue
                        lifecycle = None
                        if "lifecycle_records" in tables:
                            row = db.execute("SELECT payload,sha256 FROM lifecycle_records WHERE request_id=?", (rid,)).fetchone()
                            if row:
                                try:
                                    lifecycle = _checked(*row)
                                except (TypeError, ValueError):
                                    warnings.append("A saved order's fill/status evidence failed validation; its result remains unknown.")
                        try:
                            order = _project_order(rid, record, lifecycle)
                        except (TypeError, ValueError, KeyError):
                            # Keep a valid original request visible even when its latest
                            # lifecycle is damaged; do not substitute an older fill state.
                            if lifecycle is None:
                                raise
                            order = _project_order(rid, record, None)
                            warnings.append("A saved order's fill/status evidence failed validation; its result remains unknown.")
                        if rid in orders and orders[rid] != order:
                            raise ValueError("Order request appears with differing evidence")
                        orders[rid] = order
                    except (ValueError, TypeError, KeyError):
                        warnings.append("A Scout order record failed validation; order/fill coverage is incomplete.")
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            warnings.append("A registered Scout order journal could not be read; order/fill coverage is incomplete.")
    return tuple(sorted(orders.values(), key=lambda row: (row.get("issued_at") or "", row["request_id"]))), \
        "unreadable" if warnings and not orders else "partial" if warnings else "read", tuple(dict.fromkeys(warnings))


def scout_stats_sessions(datastore_root=None):
    """Discover only saved Scout live reference sessions, including orders alone."""
    root = _root(datastore_root)
    forecasts, _, _ = _predictions(root, None, datetime.now(timezone.utc), discovery=True)
    orders, _, _ = _orders(root, None)
    return tuple(sorted({day for row in (*forecasts, *orders) if (day := _session(row.get("reference_at")))}, reverse=True))


def load_scout_stats(datastore_root=None, session=None, *, now=None):
    root = _root(datastore_root)
    now = _instant(now or datetime.now(timezone.utc))
    if now is None:
        raise ValueError("A timezone-aware review time is required")
    if session is not None and date.fromisoformat(session).isoformat() != session:
        raise ValueError("Invalid session date")
    if session is None:
        sessions = scout_stats_sessions(root)
        session = sessions[0] if sessions else None
    forecasts, prediction_status, prediction_warnings = _predictions(root, session, now)
    orders, execution_status, execution_warnings = _orders(root, session)
    warnings = tuple(dict.fromkeys((*prediction_warnings, *execution_warnings)))
    statuses = {prediction_status, execution_status}
    status = "read" if statuses == {"read"} else "missing" if statuses == {"missing"} else "partial"
    message = (f"Scout live 15-Minute · {session} · Saved evidence only" if session else
               "No saved Scout live 15-Minute sessions are available.")
    return ScoutStatsSnapshot(session, status, message, forecasts, orders, warnings, prediction_status, execution_status)


def prediction_summary(rows):
    """Observed, before-target-open accuracy; report every excluded population."""
    counts = Counter()
    probabilities, labels = [], []
    for row in rows:
        counts["issued"] += 1
        if row.get("supported_target", row.get("target_version") == TARGET_VERSION) is not True:
            counts["unsupported_target"] += 1
            continue
        issued, opened, closed = (_instant(row.get(key)) for key in ("issued_at", "target_open", "target_close"))
        if issued is None or opened is None or closed is None or opened >= closed:
            counts["timing_unknown"] += 1
            continue
        if issued >= closed:
            counts["after_target"] += 1
            continue
        if issued >= opened:
            counts["during_target"] += 1
            continue
        counts["eligible"] += 1
        status = row.get("outcome_status", "missing")
        if status != "scored":
            counts[status if status in _STATUSES else "missing"] += 1
            continue
        probability = _number(row.get("probability_not_down"), probability=True)
        label = row.get("target_not_down")
        if probability is None or type(label) is not bool:
            counts["invalid_probability"] += 1
            continue
        probabilities.append(probability)
        labels.append(int(label))
    counts["scored"] = len(labels)
    counts["correct"] = sum((p >= .5) == y for p, y in zip(probabilities, labels))
    result = {key: counts[key] for key in ("issued", "eligible", "scored", "correct", "pending", "missing",
              "unscorable", "synthetic", "during_target", "after_target", "timing_unknown", "invalid_probability", "unsupported_target")}
    result.update(accuracy=counts["correct"] / len(labels) if labels else None,
                  brier=sum((p - y) ** 2 for p, y in zip(probabilities, labels)) / len(labels) if labels else None)
    return result


def execution_summary(orders):
    """One row per request, cumulative fills once; missing evidence stays unknown."""
    unique = {row["request_id"]: row for row in orders}
    counts = Counter(row.get("status", "UNKNOWN") for row in unique.values())
    # Native Atlas broker IDs are account-scoped. Its adapter deduplicates
    # orders internally and supplies a request scope without exposing accounts.
    # Scout's journal identity and existing default behavior remain unchanged.
    fills = {(row.get("fill_identity_scope") or row.get("broker_order_id") or row["request_id"], fill["fill_id"]): fill
             for row in unique.values() for fill in row.get("fills", ())}
    return dict(attempts=len(unique), submitted=sum(row.get("submission_state") == "ACCEPTED" for row in unique.values()),
                filled=counts["FILLED"], partial=counts["PARTIAL"], working=counts["WORKING"],
                cancelled=counts["CANCELLED"], rejected=counts["REJECTED"], unknown=counts["UNKNOWN"], not_sent=counts["NOT_SENT"],
                filled_quantity=sum(row["filled_quantity"] for row in unique.values() if row.get("filled_quantity") is not None),
                fill_records=len(fills), missing_fill_evidence=sum(row.get("filled_quantity") is None for row in unique.values()),
                realized_pnl=None)
