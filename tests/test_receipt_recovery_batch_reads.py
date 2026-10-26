"""Batch retained receipt lookups without changing exact fenced transitions."""
from datetime import datetime, timezone
import json
import socket
import sqlite3

import pytest

from ml.account_gameplan.authority import (
    AccountAuthority, AuthorityError, SUBMISSION_RECEIPT_VERSION, _json, _sha,
)


NOW = datetime(2026, 10, 26, 16, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("Retained receipt reconstruction prohibits provider/broker calls")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def ledger(tmp_path):
    db = sqlite3.connect(tmp_path / "own-authority.sqlite3")
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE reservations(id TEXT PRIMARY KEY,key TEXT UNIQUE,native_id TEXT UNIQUE,
            forecast_key TEXT UNIQUE,request TEXT,status TEXT,filled INTEGER,broker_id TEXT UNIQUE,
            created TEXT,last_evidence TEXT);
        CREATE TABLE submission_receipts(reservation TEXT PRIMARY KEY,broker_id TEXT,
            receipt_id TEXT UNIQUE,received_at TEXT,payload TEXT);
        CREATE TABLE events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,observed TEXT,kind TEXT,payload TEXT);
        CREATE TABLE fills(reservation TEXT,fill_id TEXT,payload TEXT,PRIMARY KEY(reservation,fill_id));
    """)
    owner = AccountAuthority.__new__(AccountAuthority)
    owner.binding = {"account_fingerprint": "a" * 64}
    try:
        yield owner, db
    finally:
        db.rollback(); db.close()


def receipt(reservation, broker):
    accepted = broker is not None
    return dict(version=SUBMISSION_RECEIPT_VERSION, reservation_id=reservation,
        account_fingerprint="a" * 64, request_sha256=_sha({"symbol": "AAPL", "quantity": 1}),
        broker_order_id=broker, outcome="ACCEPTED" if accepted else "NOT_SUBMITTED",
        gate_used=accepted, no_send_error_type=None if accepted else "SubmissionNotStarted",
        submission_epoch=1, submission_holder_id="own-fixture-holder",
        submission_started_at="2026-10-26T15:01:00+00:00", gate_epoch=1 if accepted else None,
        gate_holder_id="own-fixture-holder" if accepted else None,
        gate_at="2026-10-26T15:01:01+00:00" if accepted else None,
        received_at="2026-10-26T15:01:02+00:00")


def add(db, index, *, pending=False, no_send=False):
    key = "own-" + str(index); broker = None if no_send else str(100000 + index)
    status = "UNKNOWN" if pending else "NOT_SUBMITTED" if no_send else "FILLED"
    db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?)", (
        key, "key-"+key, "native-"+key, "forecast-"+key,
        _json({"symbol": "AAPL", "quantity": 1}), status,
        0 if pending or no_send else 1, None if pending else broker,
        "2026-10-26T15:00:00+00:00", "2026-10-26T15:02:00+00:00"))
    value = receipt(key, broker)
    db.execute("INSERT INTO submission_receipts VALUES(?,?,?,?,?)",
        (key, broker, _sha(value), value["received_at"], _json(value)))
    return key


def state(db):
    return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            for table in ("reservations", "submission_receipts", "fills", "events")}


def recover(owner, db):
    db.commit(); db.execute("BEGIN IMMEDIATE")
    try:
        result = owner._recover_submission_receipts(db, NOW, 2)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def test_retained_487_receipts_need_one_snapshot_lookup_and_preserve_all_rows(ledger):
    owner, db = ledger
    for index in range(487): add(db, index)
    db.commit(); before = state(db); statements = []; db.set_trace_callback(statements.append)
    assert recover(owner, db) == []
    reads = [sql for sql in statements if sql.startswith("SELECT ") and
             ("FROM reservations" in sql or "FROM submission_receipts" in sql)]
    assert sum("FROM reservations" in sql for sql in reads) == 1
    assert len(reads) == 2
    assert state(db) == before


def test_two_pending_attempts_project_in_order_then_repeat_without_state_changes(ledger):
    owner, db = ledger
    accepted = add(db, 0, pending=True)
    no_send = add(db, 1, pending=True, no_send=True)
    db.commit(); original_receipts = state(db)["submission_receipts"]
    result = recover(owner, db)
    assert [(x.reservation_id, x.status, x.broker_order_id) for x in result] == [
        (accepted, "SUBMITTED", "100000"), (no_send, "NOT_SUBMITTED", None)]
    assert state(db)["submission_receipts"] == original_receipts
    assert [json.loads(row[0])["reservation_id"] for row in db.execute("SELECT payload FROM events ORDER BY sequence")] == [accepted, no_send]
    after = state(db)
    assert recover(owner, db) == []
    assert state(db) == after


@pytest.mark.parametrize("first", ["missing", "malformed", "scope", "state"])
def test_first_invalid_receipt_error_order_survives_later_malformed_input(ledger, first):
    owner, db = ledger
    add(db, 0); add(db, 1)
    if first == "missing":
        db.execute("DELETE FROM reservations WHERE id='own-0'"); expected = "RESERVATION_NOT_FOUND"
    elif first == "malformed":
        db.execute("UPDATE submission_receipts SET payload='{}' WHERE reservation='own-0'"); expected = "IMMUTABLE_SUBMISSION_RECEIPT_CHANGED"
    elif first == "scope":
        db.execute("UPDATE reservations SET request='{}' WHERE id='own-0'"); expected = "SUBMISSION_RECEIPT_SCOPE_OR_TIME_MISMATCH"
    else:
        db.execute("UPDATE reservations SET broker_id=NULL,status='RESERVED' WHERE id='own-0'"); expected = "SUBMISSION_RECEIPT_STATE_CONFLICT"
    db.execute("UPDATE submission_receipts SET payload='{}' WHERE reservation='own-1'")
    db.commit(); before = state(db)
    with pytest.raises(AuthorityError, match=expected): recover(owner, db)
    assert state(db) == before


def test_late_bad_receipt_rolls_back_earlier_projection_and_preserves_every_original(ledger):
    owner, db = ledger
    add(db, 0, pending=True); add(db, 1)
    db.execute("UPDATE submission_receipts SET payload='{}' WHERE reservation='own-1'")
    db.commit(); before = state(db)
    with pytest.raises(AuthorityError, match="IMMUTABLE_SUBMISSION_RECEIPT_CHANGED"):
        recover(owner, db)
    assert state(db) == before


def test_projection_side_effect_invalidates_prefetched_later_reservation(ledger):
    owner, db = ledger
    add(db, 0, pending=True); add(db, 1)
    db.execute("CREATE TRIGGER changed_later AFTER UPDATE ON reservations WHEN NEW.id='own-0' "
               "BEGIN UPDATE reservations SET request='{}' WHERE id='own-1'; END")
    db.commit(); before = state(db)
    with pytest.raises(AuthorityError, match="SUBMISSION_RECEIPT_SCOPE_OR_TIME_MISMATCH"):
        recover(owner, db)
    assert state(db) == before


def test_duplicate_reference_fault_cannot_overwrite_first_broker_identity(ledger):
    owner, db = ledger
    add(db, 0, pending=True)
    # Fault injection removes a normal PK solely to exercise duplicate input
    # handling. The real authority schema permits one receipt per reservation.
    db.executescript("ALTER TABLE submission_receipts RENAME TO originals;"
                     "CREATE TABLE submission_receipts AS SELECT * FROM originals;")
    other = receipt("own-0", "100001")
    db.execute("INSERT INTO submission_receipts VALUES(?,?,?,?,?)", (
        "own-0", "100001", _sha(other), other["received_at"], _json(other)))
    db.commit(); before = state(db)
    with pytest.raises(AuthorityError, match="SUBMISSION_RECEIPT_BROKER_ID_CONFLICT"):
        recover(owner, db)
    assert state(db) == before


@pytest.mark.parametrize("column", ["request", "native_id"])
def test_blob_values_fall_back_to_exact_canonical_row_semantics(ledger, column):
    owner, db = ledger
    add(db, 0); add(db, 1)
    raw = db.execute("SELECT " + column + " FROM reservations WHERE id='own-1'").fetchone()[0]
    db.execute("UPDATE reservations SET " + column + "=? WHERE id='own-1'", (raw.encode(),))
    db.commit(); before = state(db); statements = []; db.set_trace_callback(statements.append)
    assert recover(owner, db) == []
    assert sum(sql.startswith("SELECT * FROM reservations WHERE id=") for sql in statements) == 2
    assert state(db) == before


@pytest.mark.parametrize("failure", ["unsupported", "malformed", "shape", "missing_field"])
def test_optional_aggregate_failure_uses_canonical_reads_in_original_order(ledger, failure):
    owner, db = ledger
    add(db, 0); add(db, 1)
    db.execute("UPDATE submission_receipts SET payload='{}' WHERE reservation='own-1'")
    db.commit(); before = state(db); optional_calls = []; points = []
    class Cursor:
        def fetchone(self):
            return ({"malformed": "not json", "shape": "[1]", "missing_field": '[{"id":"own-0"}]'}[failure],)
    class Connection:
        def execute(self, sql, *parameters):
            if "json_group_array" in sql and "FROM reservations" in sql:
                optional_calls.append(sql)
                if failure == "unsupported": raise sqlite3.OperationalError("synthetic unsupported JSON aggregate")
                return Cursor()
            if sql.startswith("SELECT * FROM reservations WHERE id="): points.append(parameters)
            return db.execute(sql, *parameters)
        def __getattr__(self, name): return getattr(db, name)
    with pytest.raises(AuthorityError, match="IMMUTABLE_SUBMISSION_RECEIPT_CHANGED"):
        recover(owner, Connection())
    assert len(optional_calls) == 1 and len(points) == 1
    assert state(db) == before


def test_aggregate_error_that_loses_transaction_cannot_fall_back_without_fence(ledger):
    owner, db = ledger
    add(db, 0); db.commit(); before = state(db); points = []
    class Connection:
        def execute(self, sql, *parameters):
            if "json_group_array" in sql and "FROM reservations" in sql:
                db.rollback()
                raise sqlite3.OperationalError("synthetic transaction ended")
            if sql.startswith("SELECT * FROM reservations WHERE id="): points.append(parameters)
            return db.execute(sql, *parameters)
        def __getattr__(self, name): return getattr(db, name)
    with pytest.raises(sqlite3.OperationalError, match="synthetic transaction ended"):
        recover(owner, Connection())
    assert points == [] and state(db) == before


def test_opaque_request_text_is_not_reencoded_or_interpreted_by_batch(ledger):
    owner, db = ledger
    add(db, 0)
    opaque = ' { "symbol" : "AAPL", "quantity" : 1 }\n'
    db.execute("UPDATE reservations SET request=? WHERE id='own-0'", (opaque,))
    db.commit(); before = state(db)
    assert recover(owner, db) == []
    assert state(db) == before
    assert db.execute("SELECT request FROM reservations").fetchone()[0] == opaque


def test_later_invalid_utf8_request_cannot_preempt_first_scope_failure(ledger):
    owner, db = ledger
    add(db, 0); add(db, 1)
    db.execute("UPDATE reservations SET request='{}' WHERE id='own-0'")
    db.execute("UPDATE reservations SET request=CAST(x'80' AS TEXT) WHERE id='own-1'")
    db.commit()
    before = db.execute("SELECT id,hex(CAST(request AS BLOB)),status FROM reservations ORDER BY id").fetchall()
    with pytest.raises(AuthorityError, match="SUBMISSION_RECEIPT_SCOPE_OR_TIME_MISMATCH"):
        recover(owner, db)
    assert db.execute("SELECT id,hex(CAST(request AS BLOB)),status FROM reservations ORDER BY id").fetchall() == before


def test_projection_trigger_receipt_deletion_cannot_change_frozen_inventory(ledger):
    owner, db = ledger
    add(db, 0, pending=True); add(db, 1)
    db.execute("CREATE TRIGGER remove_later_receipt AFTER UPDATE ON reservations WHEN NEW.id='own-0' "
               "BEGIN DELETE FROM submission_receipts WHERE reservation='own-1'; END")
    db.commit()
    result = recover(owner, db)
    assert [(item.reservation_id, item.status, item.broker_order_id) for item in result] == [
        ("own-0", "SUBMITTED", "100000")]
    assert db.execute("SELECT status,broker_id FROM reservations WHERE id='own-1'").fetchone()[:] == ("FILLED", "100001")
    assert db.execute("SELECT count(*) FROM submission_receipts").fetchone()[0] == 1


def test_optional_map_miss_retains_canonical_primary_key_collation(ledger):
    owner, db = ledger
    add(db, 0)
    db.executescript("ALTER TABLE reservations RENAME TO old_rows;"
        "CREATE TABLE reservations(id TEXT COLLATE NOCASE PRIMARY KEY,key TEXT UNIQUE,native_id TEXT UNIQUE,"
        "forecast_key TEXT UNIQUE,request TEXT,status TEXT,filled INTEGER,broker_id TEXT UNIQUE,created TEXT,last_evidence TEXT);"
        "INSERT INTO reservations SELECT * FROM old_rows; DROP TABLE old_rows;")
    value = receipt("OWN-0", "100000")
    db.execute("UPDATE submission_receipts SET reservation=?,receipt_id=?,payload=?",
               ("OWN-0", _sha(value), _json(value)))
    db.commit(); before = state(db)
    assert recover(owner, db) == []
    assert state(db) == before
