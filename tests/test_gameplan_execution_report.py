"""Offline execution reporting fixtures: no trader/broker imports or services."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from ml.gameplan_execution_report import (
    LEDGER_RELATIVE_PATH, STATUS_KEYS, build_execution_summary, publish_execution_summary,
)


ACCOUNT = "fixture-account-private"
SESSION = "2026-10-14"
SUBMITTED = "2026-10-14T16:30:00+00:00"
OBSERVED = "2026-10-14T23:11:00+00:00"
NOW = "2026-10-15T00:00:00+00:00"


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / LEDGER_RELATIVE_PATH
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE allocations (id TEXT PRIMARY KEY, account TEXT, symbol TEXT, horizon TEXT, forecast TEXT);
            CREATE TABLE reservations (id TEXT PRIMARY KEY, allocation TEXT, side TEXT, quantity INTEGER,
                filled INTEGER, status TEXT, broker_order TEXT, idempotency_key TEXT, request TEXT);
            CREATE TABLE fills (id TEXT PRIMARY KEY, reservation TEXT, quantity INTEGER, price TEXT, executed_at TEXT);
            CREATE TABLE evidence (id TEXT PRIMARY KEY, kind TEXT, payload TEXT);
            CREATE TABLE order_submission_times (reservation TEXT PRIMARY KEY, submitted_at TEXT);
            CREATE TABLE order_timeout_retries (child TEXT PRIMARY KEY, parent TEXT, linked_at TEXT, requested_quantity INTEGER);
        """)
        db.execute("INSERT INTO metadata VALUES ('account',?)", (ACCOUNT,))
    return tmp_path


def add_order(root, identity="one", *, status="FILLED", quantity=10, filled=None, submitted_at=SUBMITTED,
              observed_at=OBSERVED, symbol="TEST", side="BUY", broker=True, observation=True,
              fill_rows=None, account=ACCOUNT, horizon="1h", forecast=None, submission_status="SUBMITTED"):
    if filled is None:
        filled = quantity if status == "FILLED" else 0
    broker_id = "private-broker-" + identity if broker else None
    forecast = forecast or "forecast-" + identity
    payload = {"reservation_id": identity, "account_fingerprint": account, "observed_at": observed_at,
        "broker_order_id": broker_id, "status": status, "order_quantity": quantity,
        "cumulative_filled_quantity": filled, "remaining_quantity": quantity-filled}
    with sqlite3.connect(root / LEDGER_RELATIVE_PATH) as db:
        db.execute("INSERT INTO allocations VALUES (?,?,?,?,?)", ("allocation-"+identity, account, symbol, horizon, forecast))
        db.execute("INSERT INTO reservations VALUES (?,?,?,?,?,?,?,?,?)",
            (identity, "allocation-"+identity, side, quantity, filled, status, broker_id, identity, json.dumps({"forecast": forecast})))
        if submitted_at:
            db.execute("INSERT INTO order_submission_times VALUES (?,?)", (identity, submitted_at))
            db.execute("INSERT INTO evidence VALUES (?,?,?)", (identity+":submitted", "submission", json.dumps({
                "reservation_id": identity, "status": submission_status, "observed_at": submitted_at, "broker_order_id": broker_id})))
        if observation:
            db.execute("INSERT INTO evidence VALUES (?,?,?)", (identity+":observation", "order", json.dumps(payload)))
        if fill_rows is None:
            fill_rows = [(filled, "100.25", observed_at)] if filled else []
        for index, (shares, price, at) in enumerate(fill_rows):
            db.execute("INSERT INTO fills VALUES (?,?,?,?,?)", (f"{identity}:fill:{index}", identity, shares, price, at))
    return payload


def report(root, **kwargs):
    return build_execution_summary(root, kwargs.pop("session", SESSION), observed_at=kwargs.pop("now", NOW), **kwargs)


def write_result(root, identity, status, *, at=OBSERVED, broker_id=None, reason=None):
    directory = root / "ml/stock-trader-execution-events" / identity
    directory.mkdir(parents=True)
    (directory / "intent.json").write_text(json.dumps({"decision_id": identity}), encoding="utf-8")
    (directory / "result.json").write_text(json.dumps({"status": status, "completed_at": at,
        "broker_order_id": broker_id, "error": reason}), encoding="utf-8")


def test_terminal_categories_and_partial_cancellation_are_exclusive(ledger):
    add_order(ledger)
    add_order(ledger, "cancel", status="CANCELLED", filled=3)
    add_order(ledger, "reject", status="REJECTED")
    add_order(ledger, "partial", status="PARTIAL", filled=4)
    add_order(ledger, "working", status="WORKING")
    add_order(ledger, "unrecognized", status="NEW_BROKER_STATUS")
    result = report(ledger, now=OBSERVED)
    assert result["counts"]["submitted_attempts"] == 6
    assert {key: result["counts"][key] for key in STATUS_KEYS} == dict.fromkeys(STATUS_KEYS, 1)
    cancelled = next(order for order in result["orders"] if order["status"] == "cancelled")
    assert cancelled["filled_quantity"] == 3
    assert cancelled["average_fill_price"] == "100.25"
    assert sum(result["counts"][key] for key in STATUS_KEYS) == result["counts"]["submitted_attempts"]


def test_replayed_cumulative_evidence_does_not_duplicate_fills_or_orders(ledger):
    payload = add_order(ledger, fill_rows=[(4, "100", OBSERVED), (6, "110", OBSERVED)])
    with sqlite3.connect(ledger / LEDGER_RELATIVE_PATH) as db:
        db.execute("INSERT INTO evidence VALUES ('repeated', 'order', ?)", (json.dumps(payload),))
    result = report(ledger)
    assert result["counts"]["fully_filled"] == 1
    assert result["orders"][0]["filled_quantity"] == 10
    assert result["orders"][0]["average_fill_price"] == "106"
    assert len(result["orders"][0]["fills"]) == 2


def test_retry_is_another_attempt_with_exact_parent_and_child(ledger):
    add_order(ledger, "parent", status="CANCELLED", filled=3)
    add_order(ledger, "child", quantity=7)
    with sqlite3.connect(ledger / LEDGER_RELATIVE_PATH) as db:
        db.execute("INSERT INTO order_timeout_retries VALUES ('child','parent',?,7)", (SUBMITTED,))
    result = report(ledger)
    assert result["counts"]["submitted_attempts"] == 2
    child = next(order for order in result["orders"] if order["retry_parent_ref"])
    parent = next(order for order in result["orders"] if order["retry_child_refs"])
    assert child["retry_parent_ref"] == parent["order_ref"]
    assert parent["retry_child_refs"] == [child["order_ref"]]


def test_forecast_is_not_order_identity_and_catchup_is_not_split(ledger):
    add_order(ledger, "first", forecast="catchup:fixture:combined")
    add_order(ledger, "second", forecast="catchup:fixture:combined")
    result = report(ledger)
    assert result["counts"]["submitted_attempts"] == 2
    assert len({order["forecast_ref"] for order in result["orders"]}) == 1
    assert all(order["is_catchup"] for order in result["orders"])


def test_local_pre_submission_stop_is_not_a_broker_rejection(ledger):
    add_order(ledger, "stopped", status="REJECTED", broker=False, observation=False, submitted_at=None)
    write_result(ledger, "stopped", "NOT_SUBMITTED_SAFETY_CHECK", reason="local safety reason")
    result = report(ledger)
    assert result["counts"]["submitted_attempts"] == 0
    assert result["counts"]["rejected"] == 0
    assert result["counts"]["not_submitted"] == 1
    assert result["orders"][0]["status"] == "not_submitted"
    assert "local safety reason" in result["orders"][0]["reasons"]


def test_local_stop_immutable_ledger_evidence_without_event(ledger):
    add_order(ledger, "stopped", status="REJECTED", broker=False, observation=False, submitted_at=None)
    with sqlite3.connect(ledger / LEDGER_RELATIVE_PATH) as db:
        db.execute("INSERT INTO evidence VALUES ('stopped:not-submitted','submission',?)", (json.dumps({
            "reservation_id": "stopped", "status": "REJECTED", "observed_at": OBSERVED}),))
    result = report(ledger)
    assert result["counts"]["not_submitted"] == 1
    assert result["counts"]["rejected"] == 0


def test_unknown_post_attempt_is_not_reported_as_broker_rejected(ledger):
    add_order(ledger, status="UNKNOWN", broker=False, observation=False, submission_status="UNKNOWN")
    result = report(ledger)
    assert result["counts"]["submitted_attempts"] == 1
    assert result["counts"]["unknown"] == result["counts"]["unknown_submission"] == 1
    assert result["orders"][0]["submission_state"] == "attempted_unknown"
    assert result["state"] == "stale"


def test_foreign_allocation_and_legacy_event_cannot_enter_gameplan_totals(ledger):
    add_order(ledger)
    add_order(ledger, "foreign", account="other-private-account")
    add_order(ledger, "intraday", horizon="15m")
    write_result(ledger, "legacy-unrelated", "SUBMITTED", broker_id="999")
    result = report(ledger)
    assert result["counts"]["submitted_attempts"] == 1
    assert len(result["orders"]) == 1
    assert result["coverage"]["excluded_unattributed_reservations"] == 2
    assert result["state"] == "incomplete"


def test_no_heuristic_order_matching_when_submission_identity_is_absent(ledger):
    add_order(ledger, submitted_at=None)
    write_result(ledger, "other-order-same-symbol", "SUBMITTED", broker_id="private-broker-one")
    result = report(ledger)
    assert result["counts"]["submitted_attempts"] == 0
    assert result["orders"][0]["session_scope"] == "unattributed"
    assert result["orders"][0]["submitted_at"] is None


def test_pacific_midnight_and_carryover_fill_scope(ledger):
    add_order(ledger, "prior", submitted_at="2026-10-14T06:59:59+00:00")
    add_order(ledger, "start", submitted_at="2026-10-14T07:00:00+00:00")
    add_order(ledger, "end-minus", submitted_at="2026-10-15T06:59:59+00:00")
    add_order(ledger, "next", submitted_at="2026-10-15T07:00:00+00:00", observed_at="2026-10-15T07:01:00+00:00")
    result = report(ledger)
    assert result["counts"]["submitted_attempts"] == 2
    assert result["counts"]["carryover_attempts"] == 1
    assert len(result["orders"]) == 3
    carry = next(order for order in result["orders"] if order["session_scope"] == "carryover")
    assert carry["filled_quantity_in_session"] == 10
    assert result["coverage"]["start"] == "2026-10-14T07:00:00+00:00"
    assert result["coverage"]["end"] == "2026-10-15T07:00:00+00:00"


def test_dst_transition_is_a_25_hour_pacific_date(ledger):
    add_order(ledger, submitted_at="2026-11-01T07:00:00+00:00", observed_at="2026-11-02T07:59:00+00:00")
    result = report(ledger, session="2026-11-01", now="2026-11-02T08:00:00+00:00")
    start, end = (datetime.fromisoformat(result["coverage"][key]) for key in ("start", "end"))
    assert (end-start).total_seconds() == 25 * 3600
    assert result["counts"]["submitted_attempts"] == 1


def test_historical_terminal_evidence_is_not_age_stale(ledger):
    add_order(ledger)
    assert report(ledger, now="2027-01-01T00:00:00+00:00")["state"] == "available"


def test_stale_open_order_remains_visible_and_has_saved_observation(ledger):
    add_order(ledger, status="WORKING")
    result = report(ledger)
    assert result["state"] == "stale"
    assert result["last_broker_observation_at"] == OBSERVED
    assert result["orders"][0]["status"] == "working"
    assert result["coverage"]["stale"] is True


@pytest.mark.parametrize("source", ["absent", "empty", "invalid", "schema"])
def test_missing_or_unreadable_records_never_fabricate_zeros(tmp_path, source):
    path = tmp_path / LEDGER_RELATIVE_PATH
    if source != "absent":
        path.parent.mkdir(parents=True)
        if source == "invalid":
            path.write_bytes(b"not sqlite")
        elif source == "empty":
            path.touch()
        else:
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE reservations (id TEXT)")
    result = report(tmp_path)
    assert result["state"] in {"missing", "incomplete", "unreadable"}
    assert result["counts"] is None and result["orders"] == []
    if source == "absent":
        assert not path.exists()


def test_missing_fill_details_do_not_invent_average_price_or_full_fill(ledger):
    add_order(ledger, fill_rows=[(3, "100", OBSERVED)])
    result = report(ledger)
    order = result["orders"][0]
    assert order["filled_quantity"] == 10
    assert order["average_fill_price"] is None
    assert order["status"] == "unknown"
    assert result["state"] == "incomplete"


@pytest.mark.parametrize("field,value", [("status", "WORKING"), ("order_quantity", 20),
    ("cumulative_filled_quantity", 2), ("account_fingerprint", "foreign"), ("broker_order_id", "foreign")])
def test_conflicting_projection_or_identity_cannot_claim_confirmed_fill(ledger, field, value):
    payload = add_order(ledger)
    payload[field] = value
    with sqlite3.connect(ledger / LEDGER_RELATIVE_PATH) as db:
        db.execute("UPDATE evidence SET payload=? WHERE kind='order'", (json.dumps(payload),))
    result = report(ledger)
    assert result["counts"]["fully_filled"] == 0
    assert result["orders"][0]["status"] == "unknown"
    assert result["state"] == "incomplete"


def test_private_atomic_summary_does_not_change_ledger_or_forecast_exports(ledger):
    from app.ui.gameplan_execution_data import load_gameplan_execution

    add_order(ledger)
    path = ledger / LEDGER_RELATIVE_PATH
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    output = publish_execution_summary(ledger, SESSION, observed_at=NOW)
    result = json.loads(output.read_text(encoding="utf-8"))
    assert output == ledger / "state/gameplan-execution-reports" / SESSION / "summary.json"
    assert result["provenance"]["private"] is True
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert ACCOUNT not in output.read_text(encoding="utf-8")
    assert "private-broker-one" not in output.read_text(encoding="utf-8")
    assert not (ledger / "ml").exists()
    assert list(output.parent.iterdir()) == [output]
    displayed = load_gameplan_execution(ledger, SESSION, now=NOW)
    assert displayed.state == "available"
    assert displayed.counts["fully_filled"] == 1
    assert displayed.orders[0]["average_fill_price"] == "100.25"


def test_multiple_broker_identities_do_not_become_one_confirmed_order(ledger):
    payload = add_order(ledger)
    payload["broker_order_id"] = "different-order"
    with sqlite3.connect(ledger / LEDGER_RELATIVE_PATH) as db:
        db.execute("UPDATE reservations SET broker_order=NULL")
        db.execute("INSERT INTO evidence VALUES ('conflict','order',?)", (json.dumps(payload),))
    result = report(ledger)
    assert result["counts"]["fully_filled"] == 0
    assert result["orders"][0]["status"] == "unknown"
    assert result["state"] == "incomplete"


def test_order_observation_requires_exact_broker_identity(ledger):
    add_order(ledger, broker=False)
    result = report(ledger)
    assert result["counts"]["fully_filled"] == 0
    assert result["orders"][0]["status"] == "unknown"


def test_conflicting_submission_identity_cannot_supply_session_date(ledger):
    add_order(ledger)
    with sqlite3.connect(ledger / LEDGER_RELATIVE_PATH) as db:
        db.execute("DELETE FROM order_submission_times")
        db.execute("UPDATE evidence SET payload=? WHERE kind='submission'", (json.dumps({
            "reservation_id": "one", "status": "SUBMITTED", "observed_at": SUBMITTED,
            "broker_order_id": "different-broker-order"}),))
    result = report(ledger)
    assert result["state"] == "incomplete"
    assert result["counts"]["submitted_attempts"] == 0
    assert result["orders"][0]["status"] == "unknown"
    assert result["orders"][0]["submitted_at"] is None
    assert result["orders"][0]["session_scope"] == "unattributed"


def test_terminal_order_without_observation_time_is_not_confirmed(ledger):
    payload = add_order(ledger)
    payload["observed_at"] = None
    with sqlite3.connect(ledger / LEDGER_RELATIVE_PATH) as db:
        db.execute("UPDATE evidence SET payload=? WHERE kind='order'", (json.dumps(payload),))
    result = report(ledger)
    assert result["state"] == "incomplete"
    assert result["counts"]["fully_filled"] == 0
    assert result["orders"][0]["status"] == "unknown"
    assert result["last_broker_observation_at"] is None
    assert any("no observation timestamp" in reason for reason in result["orders"][0]["reasons"])


def test_reasons_are_recorded_and_private_ids_are_omitted(ledger):
    payload = add_order(ledger, status="REJECTED")
    payload["broker_status_description"] = "Broker reason for private-broker-one " + ACCOUNT
    with sqlite3.connect(ledger / LEDGER_RELATIVE_PATH) as db:
        db.execute("UPDATE evidence SET payload=? WHERE kind='order'", (json.dumps(payload),))
    result = report(ledger)
    assert result["counts"]["rejected"] == 1
    assert "Broker reason for [private identity] [private identity]" in result["orders"][0]["reasons"]


def test_unknown_submission_timestamp_does_not_reuse_broker_observation_time(ledger):
    add_order(ledger, submitted_at=None)
    result = report(ledger)
    assert result["counts"]["submitted_attempts"] == 0
    assert result["coverage"]["orders_without_submission_date"] == 1


def test_bad_iso_date_cannot_escape_private_output_root(ledger):
    with pytest.raises(ValueError):
        publish_execution_summary(ledger, "../other", observed_at=NOW)


def test_missing_local_ledger_preserves_imported_execution_summary_bytes(ledger):
    from app.ui.gameplan_execution_data import load_gameplan_execution

    add_order(ledger)
    imported = (json.dumps(report(ledger), sort_keys=True, indent=1) + "\r\n").encode("utf-8")
    recipient = ledger / "recipient"
    destination = recipient / "state/gameplan-execution-reports" / SESSION / "summary.json"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(imported)
    assert not (recipient / LEDGER_RELATIVE_PATH).exists()

    output = publish_execution_summary(recipient, SESSION, observed_at=NOW)

    assert output == destination
    assert destination.read_bytes() == imported
    assert list(destination.parent.iterdir()) == [destination]
    snapshot = load_gameplan_execution(recipient, SESSION, now=NOW)
    assert snapshot.state == "available"
    assert snapshot.counts["fully_filled"] == 1


@pytest.mark.parametrize("saved_bytes", [b'{"unfinished":', b'{"unsupported": true}\n'])
def test_missing_local_ledger_preserves_unreadable_saved_execution(tmp_path, saved_bytes):
    from app.ui.gameplan_execution_data import load_gameplan_execution

    destination = tmp_path / "state/gameplan-execution-reports" / SESSION / "summary.json"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(saved_bytes)

    output = publish_execution_summary(tmp_path, SESSION, observed_at=NOW)

    assert output == destination
    assert destination.read_bytes() == saved_bytes
    assert list(destination.parent.iterdir()) == [destination]
    snapshot = load_gameplan_execution(tmp_path, SESSION, now=NOW)
    assert snapshot.state == "unreadable"
    assert snapshot.counts is None


def test_missing_local_ledger_creates_initial_missing_summary(tmp_path):
    from app.ui.gameplan_execution_data import load_gameplan_execution

    output = publish_execution_summary(tmp_path, SESSION, observed_at=NOW)

    assert output == tmp_path / "state/gameplan-execution-reports" / SESSION / "summary.json"
    snapshot = load_gameplan_execution(tmp_path, SESSION, now=NOW)
    assert snapshot.state == "missing"
    assert snapshot.counts is None
    assert snapshot.orders == ()
    assert snapshot.report["session_date"] == SESSION
    assert list(output.parent.iterdir()) == [output]


def test_imported_summary_arriving_during_missing_publication_is_preserved(ledger, monkeypatch):
    from app.ui.gameplan_execution_data import load_gameplan_execution
    import ml.gameplan_execution_report as execution_report

    add_order(ledger)
    imported = (json.dumps(report(ledger), sort_keys=True, indent=1) + "\r\n").encode("utf-8")
    recipient = ledger / "recipient"
    destination = recipient / "state/gameplan-execution-reports" / SESSION / "summary.json"
    link = execution_report.os.link
    arrivals = []

    def arrive_before_link(source, target):
        assert Path(target) == destination
        assert not destination.exists()
        destination.write_bytes(imported)
        arrivals.append(destination)
        return link(source, target)

    monkeypatch.setattr(execution_report.os, "link", arrive_before_link)
    output = publish_execution_summary(recipient, SESSION, observed_at=NOW)

    assert arrivals == [destination]
    assert output == destination
    assert destination.read_bytes() == imported
    assert list(destination.parent.iterdir()) == [destination]
    snapshot = load_gameplan_execution(recipient, SESSION, now=NOW)
    assert snapshot.state == "available"
    assert snapshot.counts["fully_filled"] == 1


def test_existing_local_ledger_refreshes_saved_execution_summary(ledger):
    from app.ui.gameplan_execution_data import load_gameplan_execution

    add_order(ledger)
    destination = publish_execution_summary(ledger, SESSION, observed_at=NOW)
    earlier = destination.read_bytes()
    add_order(ledger, "second")
    later = "2026-10-15T00:01:00+00:00"

    output = publish_execution_summary(ledger, SESSION, observed_at=later)

    assert output == destination
    assert destination.read_bytes() != earlier
    assert list(destination.parent.iterdir()) == [destination]
    snapshot = load_gameplan_execution(ledger, SESSION, now=later)
    assert snapshot.state == "available"
    assert snapshot.counts["fully_filled"] == 2
    assert snapshot.report["generated_at"] == later
