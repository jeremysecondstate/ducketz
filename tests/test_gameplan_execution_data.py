import copy
import json
import sqlite3

import pytest

from app.ui.gameplan_execution_data import execution_sessions, load_gameplan_execution


def saved_execution_report(*, session="2026-10-14", state="available"):
    order = {"order_ref": "attempt-a", "symbol": "EXAMPLE", "side": "BUY",
             "requested_quantity": 10.0, "filled_quantity": 4.0, "average_fill_price": "8.25",
             "status": "cancelled", "lifecycle_status": "CANCELLED", "submitted_at": f"{session}T09:00:00-07:00",
             "last_observed_at": f"{session}T09:05:00-07:00", "fills": [
                 {"quantity": 4.0, "price": "8.25", "executed_at": f"{session}T09:01:00-07:00"}],
             "reasons": ["Recorded cancellation after partial fill"], "retry_parent_ref": None,
             "retry_child_refs": [], "session_scope": "submitted", "submission_state": "submitted"}
    return {"schema_version": "gameplan-execution-summary-v1", "session_date": session,
            "timezone": "America/Los_Angeles", "generated_at": f"{session}T17:00:00-07:00",
            "last_broker_observation_at": f"{session}T09:05:00-07:00", "state": state,
            "message": "Saved own-order evidence only.", "coverage": {"status": "saved_ledger"},
            "provenance": {"source": "gameplan-own-order-ledger"},
            "counts": {"submitted_attempts": 1, "fully_filled": 0, "partially_filled": 0, "working": 0,
                       "cancelled": 1, "rejected": 0, "unknown": 0, "not_submitted": 0,
                       "unknown_submission": 0, "carryover_attempts": 0}, "orders": [order]}


def write_execution(root, report):
    path = root / "state/gameplan-execution-reports" / report["session_date"] / "summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def test_load_reads_only_saved_summary_and_preserves_partial_cancel_evidence(tmp_path, monkeypatch):
    report = saved_execution_report()
    path = write_execution(tmp_path, report)
    before = path.read_bytes()
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: pytest.fail("UI must not open an execution database"))
    snapshot = load_gameplan_execution(tmp_path, "2026-10-14", now="2026-10-15T12:00:00Z")
    assert snapshot.state == "available" and snapshot.counts["submitted_attempts"] == 1
    assert snapshot.counts["cancelled"] == 1 and snapshot.counts["partially_filled"] == 0
    assert snapshot.symbols == ("EXAMPLE",)
    assert snapshot.orders[0]["filled_quantity"] == 4
    assert snapshot.orders[0]["average_fill_price"] == "8.25"
    assert snapshot.orders[0]["fills"][0]["quantity"] == 4
    assert snapshot.observed_at.isoformat() == "2026-10-14T09:05:00-07:00"
    assert path.read_bytes() == before


def test_missing_snapshot_is_unknown_coverage_not_zero(tmp_path):
    snapshot = load_gameplan_execution(tmp_path, "2026-10-14")
    assert snapshot.state == "missing" and snapshot.counts is None and snapshot.report is None
    assert snapshot.orders == () and "do not mean zero orders" in snapshot.message
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("state", ["missing", "unreadable", "incomplete"])
def test_saved_unavailable_state_does_not_manufacture_totals(tmp_path, state):
    report = saved_execution_report(state=state)
    report.update(counts=None, orders=[], last_broker_observation_at=None)
    write_execution(tmp_path, report)
    snapshot = load_gameplan_execution(tmp_path, "2026-10-14")
    assert snapshot.state == state and snapshot.counts is None and snapshot.observed_at is None


@pytest.mark.parametrize("state", ["stale", "incomplete"])
def test_saved_partial_state_keeps_observed_orders(tmp_path, state):
    report = saved_execution_report(state=state)
    write_execution(tmp_path, report)
    snapshot = load_gameplan_execution(tmp_path, "2026-10-14")
    assert snapshot.state == state and snapshot.counts["cancelled"] == 1 and len(snapshot.orders) == 1


@pytest.mark.parametrize("damage", ["json", "session", "timezone", "counts", "duplicate", "number", "overfill", "zero_request", "price", "timestamp", "unknown_status"])
def test_unreadable_or_inconsistent_snapshot_has_explicit_unavailable_state(tmp_path, damage):
    report = saved_execution_report()
    path = write_execution(tmp_path, report)
    if damage == "json":
        path.write_text("{bad json")
    else:
        if damage == "session":
            report["session_date"] = "2026-10-13"
        elif damage == "timezone":
            report["timezone"] = "UTC"
        elif damage == "counts":
            report["counts"]["fully_filled"] = 1
        elif damage == "duplicate":
            report["orders"].append(copy.deepcopy(report["orders"][0]))
        elif damage == "number":
            report["orders"][0]["filled_quantity"] = float("nan")
        elif damage == "overfill":
            report["orders"][0]["filled_quantity"] = 11
        elif damage == "zero_request":
            report["orders"][0]["requested_quantity"] = 0
        elif damage == "price":
            report["orders"][0]["average_fill_price"] = "NaN"
        elif damage == "timestamp":
            report["orders"][0]["last_observed_at"] = "2026-10-14T09:05:00"
        else:
            report["orders"][0]["status"] = "successful"
        path.write_text(json.dumps(report))
    snapshot = load_gameplan_execution(tmp_path, "2026-10-14")
    assert snapshot.state == "unreadable" and snapshot.counts is None and snapshot.orders == ()


def test_unknown_recorded_status_remains_unknown(tmp_path):
    report = saved_execution_report()
    report["orders"][0].update(status="unknown", lifecycle_status="NEW_UNRECOGNIZED_STATUS")
    report["counts"].update(cancelled=0, unknown=1)
    write_execution(tmp_path, report)
    snapshot = load_gameplan_execution(tmp_path, "2026-10-14")
    assert snapshot.state == "available" and snapshot.counts["unknown"] == 1


def test_current_session_report_ages_stale_using_pacific_date(tmp_path):
    write_execution(tmp_path, saved_execution_report())
    # 00:20 UTC is still Oct 14 Pacific, twenty minutes after generation.
    fresh = load_gameplan_execution(tmp_path, "2026-10-14", now="2026-10-15T00:20:00Z")
    assert fresh.state == "available"
    stale = load_gameplan_execution(tmp_path, "2026-10-14", now="2026-10-15T00:31:00Z")
    assert stale.state == "stale" and stale.counts["cancelled"] == 1
    # Historical terminal evidence is not stale merely because it is old.
    historical = load_gameplan_execution(tmp_path, "2026-10-14", now="2026-11-15T00:31:00Z")
    assert historical.state == "available"


@pytest.mark.parametrize("state", ["available", "incomplete"])
def test_historical_unresolved_orders_age_stale_without_discarding_saved_evidence(tmp_path, state):
    report = saved_execution_report(state=state)
    report["orders"][0].update(status="partially_filled", lifecycle_status="PARTIAL")
    report["counts"].update(cancelled=0, partially_filled=1)
    report["generated_at"] = report["orders"][0]["last_observed_at"]
    write_execution(tmp_path, report)
    fresh = load_gameplan_execution(tmp_path, "2026-10-14", now="2026-10-14T16:10:00Z")
    assert fresh.state == state
    historical = load_gameplan_execution(tmp_path, "2026-10-14", now="2026-10-16T16:10:00Z")
    assert historical.state == "stale"
    assert historical.counts["partially_filled"] == 1 and historical.orders[0]["filled_quantity"] == 4
    assert historical.observed_at == fresh.observed_at
    assert "unresolved" in historical.message and report["message"] in historical.message


def test_historical_unresolved_order_with_no_observation_is_stale(tmp_path):
    report = saved_execution_report(state="incomplete")
    report["orders"][0].update(status="unknown", lifecycle_status="UNKNOWN", last_observed_at=None)
    report["counts"].update(cancelled=0, unknown=1)
    report["last_broker_observation_at"] = None
    write_execution(tmp_path, report)
    snapshot = load_gameplan_execution(tmp_path, "2026-10-14", now="2026-10-16T16:10:00Z")
    assert snapshot.state == "stale" and snapshot.observed_at is None
    assert snapshot.counts["unknown"] == 1


@pytest.mark.parametrize("session", ["../2026-10-14", "2026-1-1", "2026-10-99"])
def test_invalid_session_cannot_redirect_reader(tmp_path, session):
    snapshot = load_gameplan_execution(tmp_path, session)
    assert snapshot.state == "unreadable" and snapshot.report_path is None


def test_execution_session_listing_is_independent_of_predictions_and_bad_entries(tmp_path):
    assert execution_sessions(tmp_path) == ()
    write_execution(tmp_path, saved_execution_report(session="2026-10-13"))
    broken = write_execution(tmp_path, saved_execution_report())
    broken.write_text("unreadable JSON remains selectable")
    parent = tmp_path / "state/gameplan-execution-reports"
    (parent / "2026-10-15").mkdir()  # A directory alone is not a saved report.
    invalid = parent / "not-a-date"
    invalid.mkdir()
    (invalid / "summary.json").write_text("{}")
    unrelated = tmp_path / "ml/gameplan-actuals-review-by-date/2026-10-16"
    unrelated.mkdir(parents=True)
    (unrelated / "run.json").write_text("{}")
    assert execution_sessions(tmp_path) == ("2026-10-14", "2026-10-13")
