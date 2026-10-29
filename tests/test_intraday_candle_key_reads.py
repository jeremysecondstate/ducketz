"""Offline regressions for bounded candle reads and the published store hunk."""
from __future__ import annotations

import ast
from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "candle_key_reads_fixture", ROOT / "ml/intraday/candle_reads.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
ordered_candle_rows = MODULE.ordered_candle_rows

SCHEMA = """CREATE TABLE candles(
    id TEXT PRIMARY KEY, symbol TEXT, opened_at TEXT,
    available_at TEXT, payload TEXT NOT NULL)"""


def database(path=":memory:"):
    db = sqlite3.connect(path)
    db.execute(SCHEMA)
    db.commit()
    return db


def insert(db, rowid, key, symbol, available_at, payload):
    db.execute("INSERT INTO candles(rowid,id,symbol,opened_at,available_at,payload) "
               "VALUES(?,?,?,NULL,?,?)", (rowid, key, symbol, available_at, payload))


def original_rows(db, symbol=None):
    return db.execute("SELECT id,payload FROM candles"
                      + (" WHERE symbol=?" if symbol else "")
                      + " ORDER BY available_at,rowid",
                      (symbol,) if symbol else ()).fetchall()


class RecordingConnection:
    def __init__(self, db, before_payload=None):
        self.db = db
        self.statements = []
        self.before_payload = before_payload

    @property
    def in_transaction(self):
        return self.db.in_transaction

    def execute(self, sql, parameters=()):
        self.statements.append((sql, tuple(parameters)))
        if "payload" in sql.lower() and self.before_payload is not None:
            callback, self.before_payload = self.before_payload, None
            callback()
        return self.db.execute(sql, parameters)


@pytest.mark.parametrize("symbol", [None, "", "AAPL", "NVDA", "ABSENT"])
def test_original_availability_and_rowid_order(symbol):
    db = database()
    try:
        for row in [
            (-2, "negative", "AAPL", "same", "negative payload"),
            (1, "one", "AAPL", "same", "one payload"),
            (4, "four", "NVDA", "before", "four payload"),
            (7, "seven", "AAPL", None, "null availability payload"),
            (9, "nine", "NVDA", "same", "nine payload"),
            (12, "twelve", "AAPL", "zz", "twelve payload"),
        ]:
            insert(db, *row)
        db.commit()
        expected = original_rows(db, symbol)
        assert ordered_candle_rows(db, symbol) == expected
        assert db.in_transaction
    finally:
        db.close()


@pytest.mark.parametrize("count", [0, 1, 128, 129, 257])
def test_payload_queries_have_bounded_key_batches(count):
    db = database()
    try:
        for index in range(count):
            insert(db, index + 1, str(index), "AAPL", str(index % 7), "x" * 256)
        db.commit()
        expected = original_rows(db)
        recorded = RecordingConnection(db)
        assert ordered_candle_rows(recorded) == expected
        payload_queries = [(sql, args) for sql, args in recorded.statements
                           if "payload" in sql.lower()]
        assert len(payload_queries) == (count + 127) // 128
        assert all(1 <= len(args) <= 128 for _, args in payload_queries)
        assert sum(len(args) for _, args in payload_queries) == count
        assert all("rowid" in sql.lower() and "where" in sql.lower()
                   and "order by" not in sql.lower() for sql, _ in payload_queries)
    finally:
        db.close()


def test_one_snapshot_survives_wal_replacement_deletion_and_new_rows(tmp_path):
    path = tmp_path / "candles.sqlite3"
    db = database(path)
    writer = None
    try:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        for index in range(257):
            insert(db, index + 1, str(index), "AAPL", str(index % 5), "old" + str(index))
        db.commit()
        expected = original_rows(db, "AAPL")
        writer = sqlite3.connect(path, timeout=1)

        def concurrent_change():
            assert db.in_transaction
            writer.execute("UPDATE candles SET payload='replacement' WHERE rowid=1")
            writer.execute("DELETE FROM candles WHERE rowid=129")
            insert(writer, 999, "new", "AAPL", "earlier", "new payload")
            writer.commit()

        recorded = RecordingConnection(db, concurrent_change)
        assert ordered_candle_rows(recorded, "AAPL") == expected
        assert recorded.before_payload is None
        db.rollback()
        assert original_rows(db, "AAPL") != expected
    finally:
        if writer is not None:
            writer.close()
        db.close()


def test_existing_transaction_and_caller_lifetime_are_preserved():
    db = database()
    try:
        insert(db, 1, "one", "AAPL", "same", "payload")
        db.commit()
        db.execute("BEGIN")
        recorded = RecordingConnection(db)
        assert ordered_candle_rows(recorded) == [("one", "payload")]
        assert not any(sql.strip().upper() == "BEGIN" for sql, _ in recorded.statements)
        assert db.in_transaction
        db.rollback()
        with db:
            assert ordered_candle_rows(db) == [("one", "payload")]
            assert db.in_transaction
        assert not db.in_transaction
        assert db.execute("SELECT 1").fetchone() == (1,)
    finally:
        db.close()


@pytest.mark.parametrize("symbol", [None, "AAPL"])
def test_sorter_never_reads_payload_column(symbol):
    db = database()
    try:
        insert(db, 1, "one", "AAPL", "same", "x" * 1024)
        db.commit()
        recorded = RecordingConnection(db)
        ordered_candle_rows(recorded, symbol)
        sort_sql, args = next((sql, args) for sql, args in recorded.statements
                              if "order by" in sql.lower())
        original_sql = "SELECT id,payload FROM candles" + (
            " WHERE symbol=?" if symbol else "") + " ORDER BY available_at,rowid"
        old_vm = db.execute("EXPLAIN " + original_sql, args).fetchall()
        new_vm = db.execute("EXPLAIN " + sort_sql, args).fetchall()
        assert any(row[1] == "SorterInsert" for row in old_vm)
        assert any(row[1] == "Column" and row[3] == 4 for row in old_vm)
        assert any(row[1] == "SorterInsert" for row in new_vm)
        assert not any(row[1] == "Column" and row[3] == 4 for row in new_vm)
        for sql, parameters in recorded.statements:
            if "payload" in sql.lower():
                plan = db.execute("EXPLAIN QUERY PLAN " + sql, parameters).fetchall()
                assert any("INTEGER PRIMARY KEY" in row[3] for row in plan)
                assert not any("TEMP B-TREE" in row[3] for row in plan)
    finally:
        db.close()


def test_reads_do_not_mutate_schema_or_data():
    db = database()
    try:
        insert(db, 1, "one", "AAPL", "same", "payload")
        db.commit()
        schema = db.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY name").fetchall()
        rows = db.execute("SELECT rowid,* FROM candles ORDER BY rowid").fetchall()
        changes = db.total_changes
        ordered_candle_rows(db)
        db.commit()
        assert db.total_changes == changes
        assert db.execute("SELECT type,name,sql FROM sqlite_schema ORDER BY name").fetchall() == schema
        assert db.execute("SELECT rowid,* FROM candles ORDER BY rowid").fetchall() == rows
    finally:
        db.close()


class FixtureContractError(ValueError):
    pass


def fixture_digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def published_observed_method():
    """Compile the complete final method carried by the portable review patch.

    This exercises the actual published integration context without importing
    application startup or requiring a private installed consumer. The package
    checker separately binds this method to the local complete consumer.
    """
    patch = (ROOT / "docs/development/patches/candle-key-reads.patch").read_text()
    lines = [line[1:] for line in patch.splitlines()
             if line.startswith((" ", "+")) and not line.startswith("+++")]
    start = next(index for index, line in enumerate(lines)
                 if line.startswith("    def observed_candles("))
    end = next((index for index in range(start + 1, len(lines))
                if lines[index].startswith("    def ")), len(lines))
    tree = ast.parse(textwrap.dedent("\n".join(lines[start:end])))
    function = tree.body[0]
    assert isinstance(function, ast.FunctionDef)
    imports = [node for node in ast.walk(function)
               if isinstance(node, ast.ImportFrom) and node.module == "candle_reads"]
    assert len(imports) == 1
    function.body.remove(imports[0])
    namespace = {"ordered_candle_rows": ordered_candle_rows, "json": json,
                 "digest": fixture_digest, "ContractError": FixtureContractError}
    exec(compile(ast.fix_missing_locations(tree), "published-candle-read-method", "exec"), namespace)
    return namespace["observed_candles"]


class MethodStore:
    def __init__(self, db):
        self.db = db

    @contextmanager
    def connect(self):
        with self.db:
            yield self.db


def insert_value(db, index, value, key=None):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    insert(db, index, key if key is not None else fixture_digest(value),
           value.get("symbol", "AAPL"), value.get("available_at"), raw)


def test_published_method_preserves_original_filter_and_future_unrelated_inputs():
    db = database()
    try:
        actual = {"symbol": "AAPL", "availability_kind": "actual", "available_at": "2099"}
        unrelated = {"symbol": "NVDA", "availability_kind": "actual", "available_at": "2000"}
        synthetic = {"symbol": "AAPL", "availability_kind": "synthetic", "available_at": "1999"}
        for index, value in enumerate((actual, unrelated, synthetic), 1):
            insert_value(db, index, value)
        db.commit()
        method = published_observed_method()
        assert method(MethodStore(db)) == [unrelated, actual]
        assert method(MethodStore(db), "AAPL") == [actual]
    finally:
        db.close()


@pytest.mark.parametrize("kind,symbol,available_at", [
    ("actual", "AAPL", "2000"),
    ("synthetic", "AAPL", "2000"),
    ("unrecorded", "NVDA", "2000"),
    ("actual", "AAPL", "2099"),
])
def test_published_method_validates_every_original_digest(kind, symbol, available_at):
    db = database()
    try:
        insert_value(db, 1, {"symbol": symbol, "availability_kind": kind,
                             "available_at": available_at}, key="corrupt digest")
        db.commit()
        with pytest.raises(FixtureContractError, match="Persisted candle evidence hash changed"):
            published_observed_method()(MethodStore(db))
    finally:
        db.close()


def test_published_method_preserves_json_decode_failure():
    db = database()
    try:
        insert(db, 1, "bad", "AAPL", "same", "{malformed")
        db.commit()
        with pytest.raises(json.JSONDecodeError):
            published_observed_method()(MethodStore(db))
    finally:
        db.close()
