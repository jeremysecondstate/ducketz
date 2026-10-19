"""Own authority observations must not starve the same operation's lease."""
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from queue import Queue
import socket
import sqlite3
import threading

import pytest

from ml.account_gameplan import authority as module
from ml.account_gameplan.authority import AccountAuthority, AuthorityBinding


AT = "2026-10-19T18:00:00+00:00"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Authority read regressions use only synthetic local state")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def insert_reservation(db, identity, *, created=AT, broker=None, filled=0):
    request = {"quantity": 9007199254740993, "limit_price": "000.001000",
               "literal": "null", "nested": {"text": "quote\" snowman \u2603", "optional": None}}
    db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?)", (
        identity, "key-" + identity, "native-" + identity, "forecast-" + identity,
        json.dumps(request), "PARTIAL" if filled else "RESERVED", filled, broker, created,
        AT if filled else None))


@pytest.fixture
def authority(tmp_path):
    authority = AccountAuthority(tmp_path / "authority.sqlite3",
        AuthorityBinding("a" * 64, "pc-original", {"pc-original": ("AAPL",)}),
        clock=lambda: datetime.fromisoformat(AT).astimezone(timezone.utc))
    with authority._transaction() as db:
        insert_reservation(db, "b", broker="broker-b", filled=9007199254740993)
        insert_reservation(db, "a")
        db.execute("INSERT INTO snapshots VALUES('snapshot',?,'{}')", (AT,))
        db.execute("INSERT INTO events(observed,kind,payload) VALUES(?,'fixture','{}')", (AT,))
    return authority


def read_legacy_state(authority):
    with closing(sqlite3.connect(authority.path)) as db:
        db.row_factory = sqlite3.Row
        return {"version": module.VERSION, "binding": json.loads(module._json(authority.binding)),
            "reservations": [asdict(authority._reservation(row)) for row in
                             db.execute("SELECT * FROM reservations ORDER BY created,id")],
            "snapshot_count": db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0],
            "event_count": db.execute("SELECT COUNT(*) FROM events").fetchone()[0]}


def test_read_state_preserves_exact_values_order_and_empty_state(authority, tmp_path):
    expected = read_legacy_state(authority)
    actual = authority.read_state()
    assert actual == expected
    assert [row["reservation_id"] for row in actual["reservations"]] == ["a", "b"]
    assert actual["reservations"][1]["filled_quantity"] == 9007199254740993
    actual["binding"]["participants"]["pc-original"].append("OTHER")
    assert authority.binding["participants"]["pc-original"] == ["AAPL"]
    empty = AccountAuthority(tmp_path / "empty.sqlite3",
        AuthorityBinding("a" * 64, "pc-original", {"pc-original": ("AAPL",)}))
    assert empty.read_state() == read_legacy_state(empty)


@pytest.mark.parametrize("pause_at", ["json_decode", "reservation_conversion"])
def test_own_renewal_can_commit_while_snapshot_python_conversion_is_blocked(authority, monkeypatch, pause_at):
    lease = authority.acquire_lease(coordinator_id="pc-original", holder_id="fixture")
    expected = read_legacy_state(authority)
    original_connect = sqlite3.connect
    with closing(original_connect(authority.path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    # Keep the real SQLite busy failure, but bound the synthetic reproduction.
    def connect(*args, **kwargs):
        kwargs["timeout"] = .1
        return original_connect(*args, **kwargs)
    monkeypatch.setattr(module.sqlite3, "connect", connect)
    entered, release = threading.Event(), threading.Event()
    reader_id = []
    original_reservation, original_loads = authority._reservation, json.loads
    def pause():
        if threading.get_ident() == reader_id[0] and not entered.is_set():
            entered.set()
            assert release.wait(5), "fixture reader was not released"
    def reservation(row):
        if pause_at == "reservation_conversion":
            pause()
        return original_reservation(row)
    def loads(value, *args, **kwargs):
        if pause_at == "json_decode":
            pause()
        return original_loads(value, *args, **kwargs)
    monkeypatch.setattr(authority, "_reservation", reservation)
    monkeypatch.setattr(module.json, "loads", loads)
    result, renewed = Queue(), Queue()
    def read():
        reader_id.append(threading.get_ident())
        try:
            result.put(authority.read_state())
        except BaseException as error:
            result.put(error)
    def renew_and_append():
        try:
            current = authority.renew_lease(lease.token)
            with authority._transaction() as db:
                insert_reservation(db, "later")
                db.execute("INSERT INTO snapshots VALUES('later',?,'{}')", (AT,))
                db.execute("INSERT INTO events(observed,kind,payload) VALUES(?,'later','{}')", (AT,))
            renewed.put(current)
        except BaseException as error:
            renewed.put(error)
    reader = threading.Thread(target=read)
    writer = threading.Thread(target=renew_and_append)
    reader.start()
    try:
        assert entered.wait(5)
        writer.start()
        current = renewed.get(timeout=5)
        assert not isinstance(current, BaseException), repr(current)
        assert (current.token, current.epoch, current.holder_id) == (lease.token, lease.epoch, lease.holder_id)
    finally:
        release.set()
        reader.join(5)
        if writer.ident is not None:
            writer.join(5)
    assert not reader.is_alive() and not writer.is_alive()
    assert result.get(timeout=1) == expected
    after = authority.read_state()
    assert len(after["reservations"]) == len(expected["reservations"]) + 1
    assert after["snapshot_count"] == expected["snapshot_count"] + 1
    assert after["event_count"] == expected["event_count"] + 1


def test_observation_uses_one_read_only_statement_and_preserves_database_bytes(authority, monkeypatch):
    original_connect = sqlite3.connect
    queries, connections = [], []
    class Connection(sqlite3.Connection):
        closed = False
        def close(self):
            self.closed = True
            return super().close()
    def connect(*args, **kwargs):
        assert kwargs.get("uri") is True and str(args[0]).endswith("?mode=ro")
        db = original_connect(*args, **kwargs, factory=Connection)
        db.set_trace_callback(queries.append)
        connections.append(db)
        return db
    before = hashlib.sha256(authority.path.read_bytes()).hexdigest()
    monkeypatch.setattr(module.sqlite3, "connect", connect)
    authority.read_state()
    assert len(queries) == 1 and queries[0].lstrip().upper().startswith("SELECT")
    assert len(connections) == 1 and connections[0].closed
    assert hashlib.sha256(authority.path.read_bytes()).hexdigest() == before
    assert not authority.path.with_name(authority.path.name + "-wal").exists()


@pytest.mark.parametrize("statement", ["PRAGMA synchronous=EXTRA", "BEGIN IMMEDIATE"])
def test_transaction_initialization_failure_closes_connection(authority, monkeypatch, statement):
    original_connect = sqlite3.connect
    opened = []
    error = sqlite3.OperationalError("fixture initialization failure")
    class Connection(sqlite3.Connection):
        closed = False
        def execute(self, sql, *args):
            if sql == statement:
                raise error
            return super().execute(sql, *args)
        def close(self):
            self.closed = True
            return super().close()
    def connect(*args, **kwargs):
        db = original_connect(*args, **kwargs, factory=Connection)
        opened.append(db)
        return db
    monkeypatch.setattr(module.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError) as raised:
        with authority._transaction():
            pytest.fail("failed transaction must never yield")
    assert raised.value is error
    assert len(opened) == 1 and opened[0].closed
