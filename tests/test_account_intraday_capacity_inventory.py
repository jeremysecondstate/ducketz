"""Exact synthetic capacity publication; no broker, provider, or shared books."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import socket
import sqlite3

import pytest

from ml.account_gameplan.authority import (
    AccountAuthority, AuthorityBinding, AuthorityError, BrokerReservation,
    IntradayCapacitySnapshot,
)


NOW = datetime(2026, 10, 26, 22, 0, tzinfo=timezone.utc)
ACCOUNT = "a" * 64
COLS = ("id", "key", "native_id", "forecast_key", "request", "status", "filled",
        "broker_id", "created", "last_evidence")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Capacity inventory fixtures cannot access a network")
    for target in ("connect", "connect_ex", "bind"):
        monkeypatch.setattr(socket.socket, target, denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def request(**changes):
    return {"symbol": "AAPL", "side": "BUY", "quantity": 2,
            "limit_price": "10", "exposure_price": "10", **changes}


def fixture_authority(tmp_path):
    authority = AccountAuthority(tmp_path / "authority.sqlite3",
        AuthorityBinding(ACCOUNT, "pc-original", {"pc-original": ("AAPL",)}),
        clock=lambda: NOW)
    lease = authority.acquire_lease(coordinator_id="pc-original", holder_id="test")
    older = (NOW - timedelta(seconds=10)).isoformat()
    rows = []
    for i in range(417):
        status, filled, last = "CANCELLED", 0, None
        if i == 0:
            status, filled = "PARTIAL", 1
            last = older
        elif i == 1:
            status, filled, last = "FILLED", 2, older
        rows.append((f"r{i}", f"key{i}", f"native{i}", f"forecast{i}",
            json.dumps(request()), status, filled, f"broker{i}", older, last))
    rows.append(("unknown", "unknown-key", "unknown-native", "unknown-forecast",
                 json.dumps(request(quantity=3)), "UNKNOWN", 0, None, older, None))
    with authority._transaction() as db:
        db.executemany("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?,?)", rows)
    snapshot = IntradayCapacitySnapshot("snapshot", ACCOUNT, "2026-10-26",
        NOW.isoformat(), "1000", {"AAPL": 20},
        broker_pending=(BrokerReservation("broker0", "AAPL", "BUY", 1, 1, "10", "10"),),
        accounted_fills={"broker0": 1, "broker1": 2},
        evidence_fingerprint="b" * 64, identity_before=ACCOUNT,
        identity_after=ACCOUNT, complete=True)
    return authority, lease, snapshot


class CursorSpy:
    def __init__(self, cursor, stats, count):
        self.cursor, self.stats, self.count = cursor, stats, count

    def _count(self, rows):
        if self.count:
            self.stats["handoffs"] += len(rows)
        return rows

    def __iter__(self):
        for row in self.cursor:
            self._count([row])
            yield row

    def fetchone(self):
        row = self.cursor.fetchone()
        self._count([] if row is None else [row])
        return row

    def fetchall(self):
        return self._count(self.cursor.fetchall())

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class FakeCursor:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,)

    def __iter__(self):
        yield (self.value,)


class ConnectionSpy:
    def __init__(self, db, stats, fault=None):
        self.db, self.stats, self.fault = db, stats, fault

    def execute(self, sql, *args):
        compact = " ".join(sql.lower().split())
        inventory = "from reservations" in compact and "broker_id is not null" in compact
        aggregate = inventory and "json_group_array" in compact
        if aggregate:
            self.stats["aggregate"] += 1
            if self.fault == "unsupported":
                raise sqlite3.OperationalError("no such function: json_group_array")
            if self.fault == "transaction_lost":
                self.db.rollback()
                raise sqlite3.OperationalError("transaction interrupted")
            if self.fault == "unexpected":
                raise RuntimeError("unexpected inventory failure")
            fake = {"malformed": "[", "invalid_utf8": b"\xff", "wrong_shape": "null"}
            if self.fault in fake:
                return CursorSpy(FakeCursor(fake[self.fault]), self.stats, True)
        elif inventory:
            self.stats["fallback"] += 1
        return CursorSpy(self.db.execute(sql, *args), self.stats, inventory)

    def __getattr__(self, name):
        return getattr(self.db, name)


def spy_transactions(authority, monkeypatch, fault=None):
    original = authority._transaction
    stats = {"handoffs": 0, "aggregate": 0, "fallback": 0}

    @contextmanager
    def transaction():
        with original() as db:
            yield ConnectionSpy(db, stats, fault)

    monkeypatch.setattr(authority, "_transaction", transaction)
    return stats


def stored(path):
    with sqlite3.connect(path) as db:
        return {"reservations": db.execute("SELECT * FROM reservations ORDER BY id").fetchall(),
                "snapshots": db.execute("SELECT name FROM sqlite_master WHERE name='intraday_capacity_snapshots'").fetchall(),
                "events": db.execute("SELECT * FROM events ORDER BY sequence").fetchall()}


def published_payload(path):
    with sqlite3.connect(path) as db:
        return json.loads(db.execute("SELECT payload FROM intraday_capacity_snapshots").fetchone()[0])


def test_publish_inventory_transfers_one_aggregate_row(tmp_path, monkeypatch):
    authority, lease, snapshot = fixture_authority(tmp_path)
    stats = spy_transactions(authority, monkeypatch)
    authority.publish_intraday_snapshot(lease.token, snapshot)
    assert stats["handoffs"] == 1
    assert stats == {"handoffs": 1, "aggregate": 1, "fallback": 0}
    payload = published_payload(authority.path)
    assert payload["accounted_fills"] == {"broker0": 1, "broker1": 2}
    assert payload["broker_pending"][0]["remaining_quantity"] == 1
    before = stored(authority.path)["reservations"]
    assert before[-1][0] == "unknown" and before[-1][5] == "UNKNOWN" and before[-1][7] is None
    # Unknown commitment retains all three shares; represented working order
    # and already-accounted fills are not charged a second time.
    assert authority.available_intraday_capacity(lease.token, snapshot.snapshot_id) == {
        "cash_available": "970", "eligible_shares": {"AAPL": 20}}
    assert stored(authority.path)["reservations"] == before


@pytest.mark.parametrize("fault", [None, "unsupported", "malformed", "invalid_utf8", "wrong_shape"])
def test_inventory_preserves_all_ten_columns_and_publish_equivalence(tmp_path, monkeypatch, fault):
    authority, lease, snapshot = fixture_authority(tmp_path)
    with authority._transaction() as db:
        expected = [dict(row) for row in db.execute("SELECT * FROM reservations WHERE broker_id IS NOT NULL")]
    stats = spy_transactions(authority, monkeypatch, fault)
    with authority._transaction() as db:
        actual = authority._intraday_broker_order_rows(db)
    assert [dict(row) for row in actual] == expected
    assert all(tuple(dict(row)) == COLS for row in actual)
    authority.publish_intraday_snapshot(lease.token, snapshot)
    assert published_payload(authority.path)["accounted_fills"] == {"broker0": 1, "broker1": 2}
    assert stats["aggregate"] == 2
    assert stats["fallback"] == (0 if fault is None else 2)


def test_blob_opaque_requests_use_fallback_without_parsing_or_mutation(tmp_path, monkeypatch):
    authority, lease, snapshot = fixture_authority(tmp_path)
    opaque = b'\xff\x00opaque historical request'
    with authority._transaction() as db:
        db.execute("UPDATE reservations SET request=? WHERE id='r416'", (opaque,))
        expected = [dict(row) for row in db.execute("SELECT * FROM reservations WHERE broker_id IS NOT NULL")]
    before = stored(authority.path)["reservations"]
    stats = spy_transactions(authority, monkeypatch)
    with authority._transaction() as db:
        actual = authority._intraday_broker_order_rows(db)
    assert [dict(row) for row in actual] == expected
    assert actual[-1]["request"] == opaque
    authority.publish_intraday_snapshot(lease.token, snapshot)
    assert stats["fallback"] == 2
    assert stored(authority.path)["reservations"] == before


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("change,error", [
    ({"broker_pending": (BrokerReservation("broker0", "AAPL", "BUY", 2, 1, "20", "20"),)},
     "BROKER_RESERVATION_DOES_NOT_MATCH_LOCAL_ORDER"),
    ({"accounted_fills": {"broker0": 2}}, "FILL_NOT_PROVEN_IN_NEWER_ACCOUNT_SNAPSHOT"),
    ({"accounted_fills": {"missing": 1}}, "FILL_NOT_PROVEN_IN_NEWER_ACCOUNT_SNAPSHOT"),
    ({"observed_at": (NOW - timedelta(seconds=10)).isoformat()},
     "FILL_NOT_PROVEN_IN_NEWER_ACCOUNT_SNAPSHOT"),
])
def test_publish_validation_and_rollback_match_fallback(tmp_path, monkeypatch, fallback, change, error):
    authority, lease, snapshot = fixture_authority(tmp_path)
    before = stored(authority.path)
    stats = spy_transactions(authority, monkeypatch, "unsupported" if fallback else None)
    with pytest.raises(AuthorityError, match=error):
        authority.publish_intraday_snapshot(lease.token, replace(snapshot, **change))
    assert stored(authority.path) == before
    assert stats["aggregate"] == 1
    assert stats["fallback"] == int(fallback)


@pytest.mark.parametrize("fault,exception,message", [
    ("transaction_lost", sqlite3.OperationalError, "transaction interrupted"),
    ("unexpected", RuntimeError, "unexpected inventory failure"),
])
def test_inventory_failure_never_uses_fallback_after_transaction_loss_or_unexpected_error(
        tmp_path, monkeypatch, fault, exception, message):
    authority, lease, snapshot = fixture_authority(tmp_path)
    before = stored(authority.path)
    stats = spy_transactions(authority, monkeypatch, fault)
    with pytest.raises(exception, match=message):
        authority.publish_intraday_snapshot(lease.token, snapshot)
    assert stats == {"handoffs": 0, "aggregate": 1, "fallback": 0}
    assert stored(authority.path) == before


@pytest.mark.parametrize("fallback", [False, True])
def test_real_fill_precision_never_changes_publication_rejection(tmp_path, monkeypatch, fallback):
    authority, lease, snapshot = fixture_authority(tmp_path)
    with authority._transaction() as db:
        db.execute("UPDATE reservations SET filled=? WHERE id='r1'", (1.0000000000000002,))
    snapshot = replace(snapshot, accounted_fills={"broker0": 1, "broker1": 1})
    before = stored(authority.path)
    stats = spy_transactions(authority, monkeypatch, "unsupported" if fallback else None)
    # SQLite's JSON renderer rounds this REAL to 1.0. Canonical comparison
    # must continue seeing the stored REAL, which differs from observed 1.
    with pytest.raises(AuthorityError, match="FILL_NOT_PROVEN_IN_NEWER_ACCOUNT_SNAPSHOT"):
        authority.publish_intraday_snapshot(lease.token, snapshot)
    assert stats["aggregate"] == 1
    assert stats["fallback"] == 1
    assert stored(authority.path) == before


@pytest.mark.parametrize("column,value", [
    ("filled", 1.0000000000000002),
    ("filled", 1.5),
    ("filled", "opaque stored quantity"),
    ("id", None),
])
def test_irregular_storage_types_keep_exact_canonical_values(tmp_path, monkeypatch, column, value):
    authority, _, _ = fixture_authority(tmp_path)
    with authority._transaction() as db:
        db.execute("UPDATE reservations SET " + column + "=? WHERE id='r416'", (value,))
        expected = [dict(row) for row in db.execute("SELECT * FROM reservations WHERE broker_id IS NOT NULL")]
    stats = spy_transactions(authority, monkeypatch)
    with authority._transaction() as db:
        actual = authority._intraday_broker_order_rows(db)
    assert stats["aggregate"] == 1 and stats["fallback"] == 1
    assert [dict(row) for row in actual] == expected
    assert type(actual[-1][column]) is type(value)
    assert actual[-1][column] == value


def test_custom_text_factory_preserves_canonical_bytes(tmp_path):
    authority, _, _ = fixture_authority(tmp_path)
    stats = {"handoffs": 0, "aggregate": 0, "fallback": 0}
    with authority._transaction() as db:
        db.text_factory = bytes
        expected = [dict(row) for row in db.execute("SELECT * FROM reservations WHERE broker_id IS NOT NULL")]
        actual = authority._intraday_broker_order_rows(ConnectionSpy(db, stats))
    assert [dict(row) for row in actual] == expected
    assert type(actual[0]["request"]) is bytes
    assert stats == {"handoffs": 417, "aggregate": 0, "fallback": 1}
