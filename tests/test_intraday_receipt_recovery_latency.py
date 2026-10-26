"""Bounded retained-receipt workloads; all evidence and ledgers are synthetic."""
from datetime import datetime, timezone
import json
import socket
import sqlite3

import pytest

from ml.account_gameplan.authority import (
    AccountAuthority, AuthorityBinding, AuthorityError, IntradayCapacitySnapshot,
    IntradayReservationRequest, SUBMISSION_RECEIPT_VERSION, _json, _sha,
)


NOW = datetime(2026, 10, 26, 18, 30, tzinfo=timezone.utc)


@pytest.fixture
def authority_env(tmp_path):
    def clock():
        return datetime(2026, 10, 12, 17, 15, 3, tzinfo=timezone.utc)
    authority = AccountAuthority(tmp_path / "synthetic-submission.sqlite3",
        AuthorityBinding("a" * 64, "pc-original", {"pc-original": ("AAPL",)}), clock=clock)
    lease = authority.acquire_lease(coordinator_id="pc-original", holder_id="synthetic-owner")
    authority.publish_intraday_snapshot(lease.token, IntradayCapacitySnapshot(
        "actual-1", "a" * 64, "2026-10-12", clock().isoformat(), "100", {"AAPL": 20},
        evidence_fingerprint="b" * 64, identity_before="a" * 64, identity_after="a" * 64,
        complete=True))
    return authority, lease.token, clock


def request(*, quantity):
    return IntradayReservationRequest(
        "synthetic-forecast", "actual-1", "a" * 64, "2026-10-12", "pc-original", "AAPL",
        "c" * 64, "BUY", quantity, "10", "10", "synthetic-native", "synthetic-allocation",
        "c" * 64, "d" * 64, "2026-10-12T17:15:02+00:00", "2026-10-12T17:00:00+00:00",
        "2026-10-12T17:45:00+00:00", "intraday-15m-open-to-third-close-not-down-v1", ("AAPL",))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("Receipt workload must not call a broker or provider")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def history(tmp_path):
    db = sqlite3.connect(tmp_path / "synthetic-authority.sqlite3")
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
        db.rollback()
        db.close()


def add(db, index, *, pending=False):
    key = f"synthetic-{index}"
    broker = str(100000 + index)
    request = _json({"symbol": "AAPL", "quantity": 1})
    db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?)", (
        key, "key-" + key, "native-" + key, "forecast-" + key, request,
        "UNKNOWN" if pending else "FILLED", 0 if pending else 1,
        None if pending else broker, "2026-10-26T15:00:00+00:00", None))
    receipt = dict(version=SUBMISSION_RECEIPT_VERSION, reservation_id=key,
        account_fingerprint="a" * 64, request_sha256=_sha(json.loads(request)),
        broker_order_id=broker, outcome="ACCEPTED", gate_used=True, no_send_error_type=None,
        submission_epoch=1, submission_holder_id="synthetic-holder",
        submission_started_at="2026-10-26T15:01:00+00:00", gate_epoch=1,
        gate_holder_id="synthetic-holder", gate_at="2026-10-26T15:01:01+00:00",
        received_at="2026-10-26T15:01:02+00:00")
    db.execute("INSERT INTO submission_receipts VALUES(?,?,?,?,?)",
        (key, broker, _sha(receipt), receipt["received_at"], _json(receipt)))
    return key


def state(db):
    return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]
            for table in ("reservations", "submission_receipts", "fills", "events")}


class ReceiptTransfer:
    """Count result rows crossing sqlite's Python boundary, not elapsed time."""
    def __init__(self, db):
        self.db, self.transferred, self.receipt_queries = db, 0, []

    def execute(self, sql, *parameters):
        cursor = self.db.execute(sql, *parameters)
        if not sql.startswith("SELECT") or "FROM submission_receipts" not in sql:
            return cursor
        self.receipt_queries.append(sql)
        transfer = self
        class Cursor:
            def fetchone(self):
                row = cursor.fetchone()
                transfer.transferred += row is not None
                return row

            def fetchall(self):
                rows = cursor.fetchall()
                transfer.transferred += len(rows)
                return rows
        return Cursor()

    def __getattr__(self, name):
        return getattr(self.db, name)


def recover(owner, db):
    db.commit()
    db.execute("BEGIN IMMEDIATE")
    try:
        result = owner._recover_submission_receipts(db, NOW, 2)
        db.commit()
        return result
    except BaseException:
        db.rollback()
        raise


def test_495_retained_receipts_transfer_one_result_row_and_validate_every_receipt(history, monkeypatch):
    owner, db = history
    for index in range(495):
        add(db, index)
    # An unrelated unresolved commitment has no exact response to project.
    db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?)", (
        "unresolved-SNDK", "unknown-key", "unknown-native", "unknown-forecast",
        _json({"symbol": "SNDK", "quantity": 1}), "UNKNOWN", 0, None,
        "2026-10-26T15:00:00+00:00", None))
    db.commit()
    before = state(db)
    validated = []
    validate = owner._submission_receipt_value
    def counted(raw):
        validated.append(raw["reservation"])
        return validate(raw)
    monkeypatch.setattr(owner, "_submission_receipt_value", counted)
    transfer = ReceiptTransfer(db)
    assert recover(owner, transfer) == []
    assert transfer.transferred == 1
    assert len(transfer.receipt_queries) == 1
    assert validated == [f"synthetic-{index}" for index in range(495)]
    assert state(db) == before


def test_last_corrupt_receipt_is_not_skipped_and_rolls_back_early_projection(history):
    owner, db = history
    for index in range(495):
        add(db, index, pending=index == 0)
    db.execute("UPDATE submission_receipts SET payload='{}' WHERE reservation='synthetic-494'")
    db.commit()
    before = state(db)
    with pytest.raises(AuthorityError, match="IMMUTABLE_SUBMISSION_RECEIPT_CHANGED"):
        recover(owner, db)
    assert state(db) == before


def test_receipt_aggregate_fallback_keeps_original_blob_semantics(history):
    owner, db = history
    add(db, 0)
    payload = db.execute("SELECT payload FROM submission_receipts").fetchone()[0]
    db.execute("UPDATE submission_receipts SET payload=?", (payload.encode(),))
    db.commit()
    before = state(db)
    assert recover(owner, db) == []
    assert state(db) == before


def test_receipt_inventory_stays_frozen_across_projection_trigger(history):
    owner, db = history
    add(db, 0, pending=True)
    add(db, 1)
    db.execute("CREATE TRIGGER remove_receipt AFTER UPDATE ON reservations WHEN NEW.id='synthetic-0' "
               "BEGIN DELETE FROM submission_receipts WHERE reservation='synthetic-1'; END")
    db.commit()
    validated = []
    validate = owner._submission_receipt_value
    owner._submission_receipt_value = lambda raw: (validated.append(raw["reservation"]), validate(raw))[1]
    result = recover(owner, db)
    assert [item.reservation_id for item in result] == ["synthetic-0"]
    assert validated == ["synthetic-0", "synthetic-1"]
    assert db.execute("SELECT status,filled,broker_id FROM reservations WHERE id='synthetic-1'").fetchone()[:] == (
        "FILLED", 1, "100001")


@pytest.mark.parametrize("failure", ["unsupported", "malformed", "shape", "missing_field", "lost_transaction"])
def test_receipt_optional_read_failure_preserves_first_error_and_fence(history, failure):
    owner, db = history
    add(db, 0)
    add(db, 1)
    db.execute("UPDATE reservations SET request='{}' WHERE id='synthetic-0'")
    db.execute("UPDATE submission_receipts SET payload='{}' WHERE reservation='synthetic-1'")
    db.commit()
    before = state(db)
    class Cursor:
        def fetchone(self):
            return ({"malformed": "not json", "shape": "[1]", "missing_field": '[{"reservation":"synthetic-0"}]'}[failure],)
    class Connection:
        def execute(self, sql, *parameters):
            if "json_group_array" in sql and "FROM submission_receipts" in sql:
                if failure == "lost_transaction":
                    db.rollback()
                    raise sqlite3.OperationalError("synthetic fence transaction lost")
                if failure == "unsupported":
                    raise sqlite3.OperationalError("synthetic aggregate unsupported")
                return Cursor()
            return db.execute(sql, *parameters)
        def __getattr__(self, name):
            return getattr(db, name)
    error = sqlite3.OperationalError if failure == "lost_transaction" else AuthorityError
    message = "synthetic fence transaction lost" if failure == "lost_transaction" else "SUBMISSION_RECEIPT_SCOPE_OR_TIME_MISMATCH"
    with pytest.raises(error, match=message):
        recover(owner, Connection())
    assert state(db) == before


@pytest.mark.parametrize("failure", ["before_gate", "after_gate", "lost_response", "invalid_id", "receipt_write", "irregular_type"])
def test_unknown_submission_records_type_and_gate_without_releasing_commitment(authority_env, monkeypatch, failure):
    authority, token, _ = authority_env
    reserved = authority.reserve_intraday(token, request(quantity=1))
    private_message = "synthetic-private-exception-content"
    error = {
        "before_gate": KeyboardInterrupt(private_message),
        "after_gate": KeyboardInterrupt(private_message),
        "lost_response": TimeoutError(private_message),
        "receipt_write": OSError(private_message),
        "irregular_type": type("Invalid type " + private_message, (RuntimeError,), {})(private_message),
    }.get(failure)
    sends = []
    def callback(gate):
        if failure in {"before_gate", "irregular_type"}:
            raise error
        gate()
        sends.append("synthetic-send")
        if failure in {"after_gate", "lost_response"}:
            raise error
        return None if failure == "invalid_id" else "101"
    if failure == "receipt_write":
        def failed_receipt(*args, **kwargs):
            raise error
        monkeypatch.setattr(authority, "_record_submission_receipt", failed_receipt)
    with pytest.raises(AuthorityError if failure == "invalid_id" else type(error)) as raised:
        authority.submit(token, reserved.reservation_id, callback)
    if error is not None:
        assert raised.value is error
    assert len(sends) == (0 if failure in {"before_gate", "irregular_type"} else 1)
    with sqlite3.connect(authority.path) as db:
        payload, = db.execute("SELECT payload FROM events WHERE kind='submission-unknown'").fetchone()
    evidence = json.loads(payload)
    assert evidence == {
        "reservation_id": reserved.reservation_id,
        "error_type": "BaseException" if failure == "irregular_type" else type(raised.value).__name__,
        "gate_called": bool(sends),
    }
    assert private_message not in payload
    unknown = authority.reservation(reserved.reservation_id)
    assert unknown.status == "UNKNOWN" and unknown.broker_order_id is None and unknown.filled_quantity == 0
    assert authority.submission_receipt(reserved.reservation_id) is None
    assert authority.available_intraday_capacity(token, "actual-1")["cash_available"] == "90"
    assert authority.submit(token, reserved.reservation_id,
        lambda gate: pytest.fail("Diagnostic gate/type fields cannot authorize another send")) == unknown
