"""Offline native-ledger projections; all evidence is synthetic temporary data."""
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.ui.atlas_stats_execution import LEDGER_PATHS, atlas_orders, atlas_execution_sessions
from app.ui.scout_stats_data import execution_summary

ACCOUNT = "a" * 64
FORECAST = "f" * 64
REFERENCE = "2026-10-24T00:00:00+00:00"  # October 23 Pacific
END = "2026-10-24T00:45:00+00:00"
OBSERVED = "2026-10-24T00:30:00+00:00"


def ledger(root, *, legacy=False):
    path = root / LEDGER_PATHS[int(legacy)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE allocations(id TEXT PRIMARY KEY,account TEXT,symbol TEXT,horizon TEXT,forecast TEXT);
            CREATE TABLE reservations(id TEXT PRIMARY KEY,allocation TEXT,side TEXT,quantity INTEGER,price TEXT,
                filled INTEGER,status TEXT,broker_order TEXT,request TEXT,last_evidence_at TEXT);
            CREATE TABLE intraday_streams(kind TEXT,id TEXT,stream TEXT,proof TEXT,PRIMARY KEY(kind,id));
            CREATE TABLE evidence(id TEXT PRIMARY KEY,kind TEXT,payload TEXT);
            CREATE TABLE fills(id TEXT PRIMARY KEY,reservation TEXT,quantity INTEGER,price TEXT,executed_at TEXT);
            CREATE TABLE order_submission_times(reservation TEXT PRIMARY KEY,submitted_at TEXT);
        """)
        db.execute("INSERT INTO metadata VALUES('account',?)", (ACCOUNT,))
    return path


def add(path, key="one", *, status="FILLED", filled=None, stream="atlas-15m", broker=True,
        ownership=None, submission=True, quantity=10, reference=REFERENCE, end=END):
    if filled is None:
        filled = quantity if status == "FILLED" else 4 if status == "PARTIAL" else 0
    owner = ownership or stream
    request = dict(kind="intraday-account-intention", stream=stream, symbol="TEST", horizon="15m",
                   forecast=FORECAST, forecast_sha256=FORECAST, forecast_snapshot_sha256="e" * 64,
                   side="BUY", quantity=quantity, price="10", start=reference, end=end)
    proof = dict(forecast_id=FORECAST, forecast_sha256=FORECAST, forecast_snapshot_sha256="e" * 64,
                 reference_at=reference, target_close_at=end)
    order_id = "broker-" + key if broker else None
    with sqlite3.connect(path) as db:
        # Opening allocation forecast legitimately differs from current request.
        db.execute("INSERT INTO allocations VALUES(?,?,?,?,?)", (key, ACCOUNT, "TEST", "15m", "old-opening-forecast"))
        db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (key, key, "BUY", quantity, "10", filled, status, order_id, json.dumps(request), OBSERVED))
        for kind in ("allocation", "reservation"):
            db.execute("INSERT INTO intraday_streams VALUES(?,?,?,?)", (kind, key, owner, json.dumps(proof)))
        if submission:
            payload = dict(reservation_id=key, status="SUBMITTED" if broker else "REJECTED" if status == "REJECTED" else "UNKNOWN",
                           broker_order_id=order_id, observed_at=OBSERVED)
            db.execute("INSERT INTO evidence VALUES(?,?,?)", (key + "-submit", "submission", json.dumps(payload)))
        if status in {"WORKING", "PARTIAL", "FILLED", "CANCELLED", "REJECTED"} and broker:
            fills = [dict(fill_id=key + "-fill", quantity=filled, price="10.25", executed_at=OBSERVED)] if filled else []
            evidence = dict(reservation_id=key, account_fingerprint=ACCOUNT, broker_order_id=order_id,
                            status=status, order_quantity=quantity, cumulative_filled_quantity=filled,
                            remaining_quantity=0 if status in {"FILLED", "CANCELLED", "REJECTED"} else quantity - filled,
                            fills=fills, observed_at=OBSERVED)
            db.execute("INSERT INTO evidence VALUES(?,?,?)", (key + "-order", "order", json.dumps(evidence)))
            for fill in fills:
                db.execute("INSERT INTO fills VALUES(?,?,?,?,?)", (fill["fill_id"], key, fill["quantity"], fill["price"], fill["executed_at"]))
    return key


def test_native_attribution_excludes_foreign_and_horizon_only_records(tmp_path):
    path = ledger(tmp_path)
    add(path, "atlas")
    add(path, "scout", stream="scout-15m")
    add(path, "gameplan", stream="gameplan")
    add(path, "conflict", ownership="scout-15m")
    rows, status, warnings = atlas_orders(tmp_path, "2026-10-23")
    assert [row["request_id"] for row in rows] == ["atlas"]
    assert status == "partial" and warnings
    assert rows[0]["forecast_id"] == FORECAST
    assert "account_fingerprint" not in rows[0] and "request" not in rows[0]


def test_all_statuses_have_independent_submission_and_fill_evidence(tmp_path):
    path = ledger(tmp_path)
    for status in ("FILLED", "PARTIAL", "WORKING", "CANCELLED", "REJECTED"):
        add(path, status, status=status)
    add(path, "not-sent", status="REJECTED", broker=False)
    add(path, "unknown", status="RESERVED", broker=False, submission=False)
    add(path, "submitted", status="SUBMITTED")
    rows, status, warnings = atlas_orders(tmp_path, "2026-10-23")
    result = execution_summary(rows)
    assert status == "read" and not warnings
    assert result == dict(attempts=8, submitted=6, filled=1, partial=1, working=1, cancelled=1,
                         rejected=1, unknown=2, not_sent=1, filled_quantity=14, fill_records=2,
                         missing_fill_evidence=2, realized_pnl=None)
    unknown = next(row for row in rows if row["request_id"] == "unknown")
    assert unknown["filled_quantity"] is None and unknown["submitted_at"] is None
    assert next(row for row in rows if row["request_id"] == "not-sent")["note"]


def test_repeated_cumulative_observations_and_fill_ids_count_once(tmp_path):
    path = ledger(tmp_path)
    add(path, status="PARTIAL")
    with sqlite3.connect(path) as db:
        raw = db.execute("SELECT payload FROM evidence WHERE kind='order'").fetchone()[0]
        db.execute("INSERT INTO evidence VALUES('repeated','order',?)", (raw,))
        evidence = json.loads(raw)
        evidence["fills"] *= 2
        db.execute("INSERT INTO evidence VALUES('repeated-fill-id','order',?)", (json.dumps(evidence),))
    rows, status, warnings = atlas_orders(tmp_path, None)
    result = execution_summary(rows + rows)
    assert status == "read" and not warnings
    assert result["partial"] == 1 and result["filled_quantity"] == 4 and result["fill_records"] == 1


@pytest.mark.parametrize("damage", ["quantity", "fill", "latest-time", "account", "shape"])
def test_malformed_latest_lifecycle_retains_original_attempt_as_unknown(tmp_path, damage):
    path = ledger(tmp_path)
    add(path, "bad")
    add(path, "good")
    with sqlite3.connect(path) as db:
        evidence = json.loads(db.execute("SELECT payload FROM evidence WHERE id='bad-order'").fetchone()[0])
        if damage == "quantity": evidence["cumulative_filled_quantity"] = 30
        elif damage == "fill": evidence["fills"][0]["quantity"] = 4
        elif damage == "latest-time": evidence["observed_at"] = "malformed"
        elif damage == "account": evidence["account_fingerprint"] = "b" * 64
        else: evidence["fills"] = {"invalid": "shape"}
        db.execute("INSERT INTO evidence VALUES('latest-bad','order',?)", (json.dumps(evidence),))
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert status == "partial" and warnings and len(rows) == 2
    result = execution_summary(rows)
    assert result["filled"] == 1 and result["unknown"] == 1 and result["filled_quantity"] == 10


@pytest.mark.parametrize("damage", ["request-json", "proof", "owner", "foreign-account", "request-quantity", "target-time"])
def test_invalid_original_attribution_excludes_only_damaged_attempt(tmp_path, damage):
    path = ledger(tmp_path)
    add(path, "bad")
    add(path, "good")
    with sqlite3.connect(path) as db:
        request = json.loads(db.execute("SELECT request FROM reservations WHERE id='bad'").fetchone()[0])
        if damage == "proof":
            db.execute("UPDATE intraday_streams SET proof='{}' WHERE id='bad' AND kind='reservation'")
        elif damage == "owner":
            db.execute("DELETE FROM intraday_streams WHERE id='bad' AND kind='allocation'")
        elif damage == "foreign-account":
            db.execute("UPDATE allocations SET account=? WHERE id='bad'", ("b" * 64,))
        else:
            if damage == "request-quantity": request["quantity"] = 99
            elif damage == "target-time": request["end"] = "bad"
            db.execute("UPDATE reservations SET request=? WHERE id='bad'", ("{" if damage == "request-json" else json.dumps(request),))
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert status == "partial" and warnings and [row["request_id"] for row in rows] == ["good"]


def test_missing_and_unreadable_sources_stay_unavailable(tmp_path):
    assert atlas_orders(tmp_path, None) == ((), "missing", ())
    path = tmp_path / LEDGER_PATHS[0]
    path.parent.mkdir(parents=True)
    path.write_bytes(b"unreadable")
    before = path.read_bytes()
    assert atlas_orders(tmp_path, None)[1] == "unreadable"
    assert path.read_bytes() == before
    old = ledger(tmp_path, legacy=True)
    add(old)
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert status == "partial" and warnings and len(rows) == 1


def test_existing_empty_source_is_readable_zero(tmp_path):
    ledger(tmp_path)
    assert atlas_orders(tmp_path, None) == ((), "read", ())


def test_legacy_mixed_ledger_and_current_attempts_merge_without_duplicate(tmp_path):
    current = ledger(tmp_path)
    legacy = ledger(tmp_path, legacy=True)
    add(current, "repeated")
    add(legacy, "repeated")
    add(legacy, "legacy-only", reference="2026-10-12T17:00:00+00:00", end="2026-10-12T17:45:00+00:00")
    add(legacy, "foreign", stream="gameplan")
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert len(rows) == 2 and status == "read" and not warnings
    assert atlas_execution_sessions(tmp_path) == ("2026-10-23", "2026-10-12")
    assert len(atlas_orders(tmp_path, "2026-10-12")[0]) == 1
    assert atlas_orders(tmp_path, "2026-10-24")[0] == ()


def test_read_only_queries_never_backfill_submission_times_or_alter_sources(tmp_path):
    path = ledger(tmp_path)
    add(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    rows, _, _ = atlas_orders(tmp_path, None)
    assert rows[0]["submitted_at"] == OBSERVED and rows[0]["issued_at"] is None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM order_submission_times").fetchone()[0] == 0


def test_missing_unique_fill_row_marks_known_attempt_unknown(tmp_path):
    path = ledger(tmp_path)
    add(path)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM fills")
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert status == "partial" and warnings
    assert rows[0]["status"] == "UNKNOWN" and rows[0]["filled_quantity"] is None


def test_unknown_submission_receipt_never_becomes_not_sent(tmp_path):
    path = ledger(tmp_path)
    add(path, status="UNKNOWN", broker=False)
    rows, status, _ = atlas_orders(tmp_path, None)
    assert status == "read" and rows[0]["submission_state"] == "UNKNOWN"
    assert rows[0]["status"] == "UNKNOWN" and rows[0]["filled_quantity"] is None


def test_unknown_submission_with_unverified_identity_stays_unknown(tmp_path):
    path = ledger(tmp_path)
    add(path, status="UNKNOWN")
    with sqlite3.connect(path) as db:
        raw = json.loads(db.execute("SELECT payload FROM evidence WHERE kind='submission'").fetchone()[0])
        raw["status"] = "UNKNOWN"
        db.execute("UPDATE evidence SET payload=? WHERE kind='submission'", (json.dumps(raw),))
    rows, status, _ = atlas_orders(tmp_path, None)
    assert status == "read" and execution_summary(rows)["submitted"] == 0
    assert rows[0]["submission_state"] == rows[0]["status"] == "UNKNOWN"


def test_missing_current_ledger_preserves_explicit_historical_coverage(tmp_path):
    path = ledger(tmp_path, legacy=True)
    add(path)
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert len(rows) == 1 and status == "partial"
    assert any("current independent" in warning for warning in warnings)


@pytest.mark.parametrize("raw", ["{", "[]", '{}', '{"status":"FILLED"}'])
def test_unattributable_newer_lifecycle_cannot_restore_older_success(tmp_path, raw):
    path = ledger(tmp_path)
    add(path, "older")
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO evidence VALUES('malformed','order',?)", (raw,))
    add(path, "later")
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert status == "partial" and warnings
    rows = {row["request_id"]: row for row in rows}
    assert rows["older"]["status"] == "UNKNOWN" and rows["older"]["filled_quantity"] is None
    assert rows["later"]["status"] == "FILLED" and rows["later"]["filled_quantity"] == 10


def test_cached_lifecycle_time_must_match_native_latest_observation(tmp_path):
    path = ledger(tmp_path)
    add(path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE reservations SET last_evidence_at='2026-10-24T00:35:00+00:00'")
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert status == "partial" and warnings
    assert rows[0]["status"] == "UNKNOWN" and rows[0]["filled_quantity"] is None


def test_invalid_current_ownership_cannot_be_resurrected_from_historical_copy(tmp_path):
    current = ledger(tmp_path)
    legacy = ledger(tmp_path, legacy=True)
    add(current, "same-attempt", ownership="scout-15m")
    add(legacy, "same-attempt")
    add(legacy, "valid-history")
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert status == "partial" and warnings
    assert [row["request_id"] for row in rows] == ["valid-history"]


def test_foreign_current_identity_also_prevents_historical_reclassification(tmp_path):
    current = ledger(tmp_path)
    legacy = ledger(tmp_path, legacy=True)
    add(current, "same-attempt", stream="scout-15m")
    add(legacy, "same-attempt")
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert rows == () and status == "partial" and warnings


def copy_broker_identity(path, key, *, broker_id=None, fill_id=None):
    with sqlite3.connect(path) as db:
        if broker_id is not None:
            db.execute("UPDATE reservations SET broker_order=? WHERE id=?", (broker_id, key))
        for identity, kind, raw in db.execute("SELECT id,kind,payload FROM evidence").fetchall():
            value = json.loads(raw)
            if value.get("reservation_id") != key:
                continue
            if broker_id is not None:
                value["broker_order_id"] = broker_id
            if kind == "order" and fill_id is not None:
                for fill in value["fills"]:
                    fill["fill_id"] = fill_id
            db.execute("UPDATE evidence SET payload=? WHERE id=?", (json.dumps(value), identity))
        if fill_id is not None:
            db.execute("UPDATE fills SET id=? WHERE reservation=?", (fill_id, key))


@pytest.mark.parametrize("same_ledger", [False, True])
def test_different_requests_cannot_double_count_one_broker_order(tmp_path, same_ledger):
    current = ledger(tmp_path)
    other = current if same_ledger else ledger(tmp_path, legacy=True)
    add(current, "first")
    add(other, "different-request")
    copy_broker_identity(other, "different-request", broker_id="broker-first",
                         fill_id=None if same_ledger else "first-fill")
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert status == "partial" and warnings
    assert [row["request_id"] for row in rows] == ["first"]
    result = execution_summary(rows)
    assert result["attempts"] == result["submitted"] == result["fill_records"] == 1
    assert result["filled_quantity"] == 10


def test_duplicate_fill_identity_on_distinct_brokers_makes_both_unknown(tmp_path):
    current = ledger(tmp_path)
    legacy = ledger(tmp_path, legacy=True)
    add(current, "first")
    add(legacy, "second")
    copy_broker_identity(legacy, "second", fill_id="first-fill")
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert status == "partial" and warnings and len(rows) == 2
    result = execution_summary(rows)
    assert result["attempts"] == result["submitted"] == result["unknown"] == 2
    assert result["filled_quantity"] == result["fill_records"] == 0
    assert result["missing_fill_evidence"] == 2


def test_unknown_current_order_cannot_gain_legacy_fill_through_broker_duplicate(tmp_path):
    current = ledger(tmp_path)
    legacy = ledger(tmp_path, legacy=True)
    add(current, "first", status="SUBMITTED")
    add(legacy, "different-request")
    copy_broker_identity(legacy, "different-request", broker_id="broker-first")
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert len(rows) == 1 and status == "partial" and warnings
    assert rows[0]["request_id"] == "first" and rows[0]["status"] == "UNKNOWN"
    assert rows[0]["filled_quantity"] is None


def test_distinct_saved_accounts_retain_separate_broker_and_fill_identities(tmp_path):
    current = ledger(tmp_path)
    legacy = ledger(tmp_path, legacy=True)
    add(current, "first")
    add(legacy, "second")
    copy_broker_identity(legacy, "second", broker_id="broker-first", fill_id="first-fill")
    with sqlite3.connect(legacy) as db:
        db.execute("UPDATE metadata SET value=? WHERE key='account'", ("b" * 64,))
        db.execute("UPDATE allocations SET account=?", ("b" * 64,))
        raw = json.loads(db.execute("SELECT payload FROM evidence WHERE kind='order'").fetchone()[0])
        raw["account_fingerprint"] = "b" * 64
        db.execute("UPDATE evidence SET payload=? WHERE kind='order'", (json.dumps(raw),))
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert len(rows) == 2 and status == "read" and not warnings
    result = execution_summary(rows)
    assert result["attempts"] == result["fill_records"] == result["submitted"] == 2
    assert result["filled_quantity"] == 20
    assert all("account_fingerprint" not in row for row in rows)


def test_missing_native_account_metadata_is_unavailable_even_when_empty(tmp_path):
    path = ledger(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM metadata WHERE key='account'")
    rows, status, warnings = atlas_orders(tmp_path, None)
    assert rows == () and status == "unreadable" and warnings


def test_rejected_receipt_with_order_identity_cannot_prove_not_sent(tmp_path):
    path = ledger(tmp_path)
    add(path, status="REJECTED", broker=False)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE reservations SET broker_order='unverified-order'")
        raw = json.loads(db.execute("SELECT payload FROM evidence WHERE kind='submission'").fetchone()[0])
        raw["broker_order_id"] = "unverified-order"
        db.execute("UPDATE evidence SET payload=? WHERE kind='submission'", (json.dumps(raw),))
    rows, _, _ = atlas_orders(tmp_path, None)
    assert rows[0]["status"] == rows[0]["submission_state"] == "UNKNOWN"
    assert rows[0]["filled_quantity"] is None
