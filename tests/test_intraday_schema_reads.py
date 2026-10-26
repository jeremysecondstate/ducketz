"""Exact schema guards retain canonical failures with fewer SQLite handoffs."""
from contextlib import contextmanager
import json
import socket
import sqlite3

import pytest

from ml.intraday.execution_intentions import IntentionStore, _RECOVERY_ROW_SCHEMA
from ml.intraday.prediction import ContractError
from ml.intraday.store import ShadowStore, _EXECUTION_FORECAST_SCHEMA


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("Schema read fixtures prohibit provider and broker calls")
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture(params=["forecast", "sizing"])
def schema(tmp_path, request):
    store = ShadowStore(tmp_path / "own.sqlite3"); IntentionStore(store)
    method, expected = ((ShadowStore._execution_forecast_schema, _EXECUTION_FORECAST_SCHEMA)
        if request.param == "forecast" else (IntentionStore._row_schema, _RECOVERY_ROW_SCHEMA))
    with store.connect() as db:
        yield db, method, expected


def test_known_schema_reads_one_sqlite_result_row_without_changing_definitions(schema):
    db, method, expected = schema
    observed = []
    class Cursor:
        def __init__(self, cursor): self.cursor = cursor
        def fetchone(self): observed.append("row"); return self.cursor.fetchone()
        def __iter__(self):
            for row in self.cursor: observed.append("row"); yield row
    class Connection:
        def execute(self, sql, *args): return Cursor(db.execute(sql, *args))
        def __getattr__(self, name): return getattr(db, name)
    before = dict(db.execute("SELECT name,sql FROM sqlite_master"))
    assert method(Connection()) == expected
    assert observed == ["row"]
    assert dict(db.execute("SELECT name,sql FROM sqlite_master")) == before


@pytest.mark.parametrize("failure", ["unsupported", "malformed", "shape", "wrong_map"])
@pytest.mark.parametrize("pinned", [False, True])
def test_optional_failure_retains_original_reader_without_json_prerequisite(schema, failure, pinned):
    db, method, expected = schema
    if pinned: db.execute("BEGIN")
    aggregate = []; canonical = []
    class Cursor:
        def fetchone(self):
            return ({"malformed": "not json", "shape": "[]", "wrong_map": '{"missing":"wrong"}'}[failure],)
    class Connection:
        def execute(self, sql, *args):
            if "json_group_object" in sql:
                aggregate.append(sql)
                if failure == "unsupported": raise sqlite3.OperationalError("synthetic absent JSON")
                return Cursor()
            canonical.append(sql)
            return db.execute(sql, *args)
        def __getattr__(self, name): return getattr(db, name)
    assert method(Connection()) == expected
    assert len(aggregate) == len(canonical) == 1
    assert db.in_transaction is pinned


def test_failed_optional_read_cannot_leave_pinned_source_transaction(schema):
    db, method, _ = schema
    db.execute("BEGIN"); canonical = []
    class Connection:
        def execute(self, sql, *args):
            if "json_group_object" in sql:
                db.rollback()
                raise sqlite3.OperationalError("synthetic lost schema transaction")
            canonical.append(sql)
            return db.execute(sql, *args)
        def __getattr__(self, name): return getattr(db, name)
    with pytest.raises(sqlite3.OperationalError, match="lost schema transaction"):
        method(Connection())
    assert canonical == []


@pytest.mark.parametrize("damage", ["missing", "changed", "blob"])
def test_changed_protected_schema_retains_full_canonical_rejection(schema, damage):
    db, method, expected = schema
    trigger = next(name for name, sql in expected.items() if "CREATE TRIGGER" in sql)
    if damage == "missing": db.execute("DROP TRIGGER " + trigger)
    elif damage == "changed":
        table = "forecasts" if expected is _EXECUTION_FORECAST_SCHEMA else "intraday_intentions"
        db.execute("DROP TRIGGER " + trigger)
        db.execute("CREATE TRIGGER " + trigger + " AFTER INSERT ON " + table + " BEGIN SELECT 1; END")
    else:
        db.execute("PRAGMA writable_schema=ON")
        db.execute("UPDATE sqlite_master SET sql=CAST(sql AS BLOB) WHERE name=?", (trigger,))
        db.execute("PRAGMA writable_schema=OFF")
    canonical = dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name IN (" +
        ",".join("?" for _ in expected) + ")", tuple(expected)))
    assert canonical != expected
    with pytest.raises(ContractError, match="schema changed"):
        method(db)


def test_irregular_late_schema_text_preserves_canonical_decode_failure(schema):
    db, method, expected = schema
    trigger = next(name for name, sql in expected.items() if "CREATE TRIGGER" in sql)
    db.execute("PRAGMA writable_schema=ON")
    db.execute("UPDATE sqlite_master SET sql=CAST(x'80' AS TEXT) WHERE name=?", (trigger,))
    db.execute("PRAGMA writable_schema=OFF")
    with pytest.raises(sqlite3.OperationalError) as canonical:
        dict(db.execute("SELECT name,sql FROM sqlite_master WHERE name IN (" +
            ",".join("?" for _ in expected) + ")", tuple(expected)))
    with pytest.raises(type(canonical.value)):
        method(db)
