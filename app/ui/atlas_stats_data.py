"""Read-only Atlas 15-minute scientific results, separate from trade decisions.

The installed forecast contract is p(third target candle close >= reference
open), with target open at reference + 30 minutes and close at + 45 minutes.
The saved consensus is the equal mean of successful model probabilities. Binary
accuracy uses p >= .5; BUY/SELL/HOLD uses the separately saved execution policy.
No model settings, original forecasts or actual-outcome revisions are changed.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3

from app.ui.scout_stats_data import (
    PACIFIC, TARGET_VERSION, _connect, _digest, _instant, _root, _session,
    execution_summary, prediction_summary,
)
from app.ui.atlas_stats_execution import atlas_execution_sessions, atlas_orders

_FIELDS = ("pc", "symbol", "forecast_id", "mode", "target_version", "target_open",
           "target_close", "probability_not_down", "reference_at", "issued_at",
           "reference_open", "data_through", "training_cutoff", "model_selection_id",
           "ensemble_status", "configured_count", "successful_count", "source_dataset",
           "direction", "direction_policy_version", "direction_up_threshold", "direction_down_threshold")
_STATUSES = {"scored", "pending", "missing", "unscorable", "synthetic"}


@dataclass(frozen=True)
class AtlasStatsSnapshot:
    session: str | None = None
    status: str = "missing"
    message: str = "No saved Atlas 15-Minute results loaded."
    forecasts: tuple[dict, ...] = ()
    orders: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()
    prediction_status: str = "missing"
    execution_status: str = "missing"


def _headers(db):
    # The production database contains very large scientific payloads. Session
    # discovery must stay on indexed headers and never inspect those payloads.
    return db.execute("SELECT id,reference_at,issued_at,symbol FROM forecasts WHERE pc='Atlas'")


def _prediction_sessions(root):
    path = root / "intraday/shadow.sqlite3"
    if not path.is_file():
        return ()
    try:
        with closing(_connect(path)) as db:
            return tuple({day for _, reference, _, _ in _headers(db) if (day := _session(reference))})
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return ()


def _latest_outcome(db, forecast, now):
    row = db.execute("SELECT id,forecast_id,observed_at,payload FROM outcomes WHERE forecast_id=? "
                     "ORDER BY rowid DESC LIMIT 1", (forecast["forecast_id"],)).fetchone()
    if not row:
        return None
    key, fid, observed, raw = row
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Invalid outcome")
    evidence = dict(value)
    revision, observation = evidence.pop("revision_id", None), evidence.pop("observed_at", None)
    stamp, closed = _instant(observed), _instant(forecast.get("target_close"))
    if (revision != key or observation != observed or fid != forecast["forecast_id"]
            or value.get("forecast_id") != fid or _digest(evidence, ensure_ascii=True) != key
            or stamp is None or stamp > now):
        raise ValueError("Outcome identity or observation differs")
    if value.get("status") in {"scored", "synthetic"}:
        if closed is None or stamp < closed:
            raise ValueError("Outcome predates maturity")
        # Live outcomes must actually have been observed. Historical assumed
        # availability or absent availability proof cannot enter this score.
        if value.get("status") == "scored" and value.get("availability_basis") != "actual":
            value = dict(value, status="unscorable", unavailable_reason="Observed availability is unrecorded")
    return value


def _predictions(root, selected, now):
    path = root / "intraday/shadow.sqlite3"
    if not path.is_file():
        return (), "missing", ("Atlas prediction database is missing.",)
    rows, warnings = [], []
    paths = ",".join("'$." + field + "'" for field in _FIELDS)
    query = "SELECT CASE WHEN json_valid(payload) THEN json_extract(payload," + paths + ") END FROM forecasts WHERE id=? AND pc='Atlas'"
    try:
        with closing(_connect(path)) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            headers = list(_headers(db))
            if "outcomes" not in tables:
                warnings.append("Atlas outcome table is missing; mature forecasts have no saved actual results.")
            for fid, reference, issued, symbol in headers:
                day = _session(reference)
                if day is None:
                    warnings.append("An Atlas forecast has no usable reference date; session coverage is incomplete.")
                    continue
                if selected is None or day != selected:
                    continue
                try:
                    projected = db.execute(query, (fid,)).fetchone()
                    value = dict(zip(_FIELDS, json.loads(projected[0])))
                    if value.get("mode") != "live":
                        continue
                    if any(value.get(field) != indexed for field, indexed in
                           (("forecast_id", fid), ("pc", "Atlas"), ("symbol", symbol),
                            ("reference_at", reference), ("issued_at", issued))):
                        raise ValueError("Forecast identity differs")
                    ref, opened, closed = (_instant(value.get(key)) for key in ("reference_at", "target_open", "target_close"))
                    value["supported_target"] = (value.get("target_version") == TARGET_VERSION
                        and (opened is None or closed is None or
                             opened == ref + timedelta(minutes=30) and closed == ref + timedelta(minutes=45)))
                except (ValueError, TypeError, KeyError, IndexError):
                    warnings.append("A saved Atlas forecast is malformed; prediction coverage is incomplete.")
                    continue
                outcome = None
                if "outcomes" in tables:
                    try:
                        outcome = _latest_outcome(db, value, now)
                    except (ValueError, TypeError, KeyError):
                        warnings.append("A latest saved Atlas outcome is unreadable; its forecast remains missing.")
                status = (outcome or {}).get("status", "missing")
                if closed is not None and now < closed:
                    status = "pending"
                elif status == "pending":
                    status = "missing"
                if status not in _STATUSES:
                    status = "missing"
                    warnings.append("An Atlas outcome has an unknown status; coverage is incomplete.")
                if outcome and outcome.get("unavailable_reason") == "Observed availability is unrecorded":
                    warnings.append("An Atlas outcome lacks observed availability evidence; it is unscorable.")
                value.update(outcome_status=status, target_not_down=(outcome or {}).get("target_not_down"),
                    outcome_observed_at=(outcome or {}).get("observed_at"),
                    outcome_revision_id=(outcome or {}).get("revision_id"),
                    target_close_price=(outcome or {}).get("target_close_price"),
                    unavailable_reason=(outcome or {}).get("unavailable_reason"))
                rows.append(value)
        return tuple(sorted(rows, key=lambda row: (row.get("issued_at") or "", row["forecast_id"]))), \
            "partial" if warnings else "read", tuple(dict.fromkeys(warnings))
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return tuple(rows), "partial" if rows else "unreadable", tuple(dict.fromkeys((*warnings,
            "Atlas prediction database could not be completely read; prediction coverage is incomplete.")))


def atlas_stats_sessions(datastore_root=None):
    """Discover Atlas reference dates from scalar headers and own order evidence.

    Indexed date discovery may include a historical shadow/replay-only date;
    loading it still scores live forecasts only. No scientific payloads are read
    merely to populate the session selector.
    """
    root = _root(datastore_root)
    predictions = _prediction_sessions(root)
    try:
        execution = atlas_execution_sessions(root)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        execution = ()
    return tuple(sorted(set(predictions) | set(execution), reverse=True))


def load_atlas_stats(datastore_root=None, session=None, *, now=None):
    root = _root(datastore_root)
    now = _instant(now or datetime.now(timezone.utc))
    if now is None:
        raise ValueError("A timezone-aware review time is required")
    if session is not None and date.fromisoformat(session).isoformat() != session:
        raise ValueError("Invalid session date")
    if session is None:
        sessions = atlas_stats_sessions(root)
        session = sessions[0] if sessions else None
    forecasts, prediction_status, prediction_warnings = _predictions(root, session, now)
    try:
        orders, execution_status, execution_warnings = atlas_orders(root, session)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        orders, execution_status = (), "unreadable"
        execution_warnings = ("Atlas order/fill evidence could not be read; execution results are unavailable.",)
    statuses = {prediction_status, execution_status}
    status = "read" if statuses == {"read"} else "missing" if statuses == {"missing"} else "partial"
    warnings = tuple(dict.fromkeys((*prediction_warnings, *execution_warnings)))
    message = (f"Atlas live 15-Minute · {session} · Saved evidence only" if session else
               "No saved Atlas 15-Minute reference sessions are available.")
    return AtlasStatsSnapshot(session, status, message, forecasts, orders, warnings, prediction_status, execution_status)
