"""Real local SQLite contention; no clients, training, orders or live state."""
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import candle_collection
from ml.intraday.prediction import instant
from ml.intraday.runtime import SourceIntervalEmpty
from ml.intraday.store import ShadowStore
from tests.test_intraday_candle_close import Clock, ProviderFixture, history


SESSION = "manual-collection-storage-fixture"


def fake_live_histories(monkeypatch):
    class Incoming:
        def __init__(self, *, histories, provider, store, journal, reference_at, clock,
                     bounded, stopped, reuse_saved, population_contract=None):
            assert bounded and reuse_saved and population_contract is None
            self.histories, self.provider, self.store = histories, provider, store
            self.reference, self.clock, self.stopped = instant(reference_at), clock, stopped

        def bind_cycle(self, identity):
            self.identity = identity

        def get(self, symbol):
            if self.stopped():
                raise InterruptedError("Fixture manual stop")
            saved = [row for row in self.store.observed_candles(symbol)
                     if instant(row["opened_at"]) == self.reference]
            row = saved[-1] if saved else self.provider.completed_candle(symbol, self.reference)
            if not saved:
                self.store.record_candle(row)
            self.histories[symbol] = history([row])
            return self.histories[symbol]

    monkeypatch.setattr(candle_collection, "LiveHistories", Incoming)


class FastStore(ShadowStore):
    """Exercise real SQLite failures immediately instead of its production 30s wait."""
    hook = None

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0)
        owner = self

        class Proxy:
            def execute(self, sql, arguments=()):
                if owner.hook:
                    owner.hook(sql)
                return db.execute(sql, arguments)

            def __getattr__(self, name):
                return getattr(db, name)

        try:
            with db:
                yield Proxy()
        finally:
            db.close()


class CompetingWriter:
    def __init__(self, store, predicate):
        self.store, self.predicate, self.holder = store, predicate, None
        self.starts, self.releases = 0, 0
        store.hook = self.before_sql

    def before_sql(self, sql):
        if not self.starts and self.predicate(sql):
            self.holder = sqlite3.connect(self.store.path, timeout=0)
            self.holder.execute("BEGIN IMMEDIATE")
            self.starts += 1

    def pause(self, seconds):
        assert seconds == 1 and self.holder is not None
        self.holder.rollback()
        self.holder.close()
        self.holder = None
        self.releases += 1

    def close(self):
        self.store.hook = None
        if self.holder is not None:
            self.holder.rollback()
            self.holder.close()
            self.holder = None


def fixture(monkeypatch, tmp_path, *, provider=None, stopped=lambda: False, pause=lambda _: None):
    fake_live_histories(monkeypatch)
    clock = Clock("2026-07-11T00:00:01Z")
    store = FastStore(tmp_path / "shadow.sqlite3")
    provider = provider or ProviderFixture(clock)
    collector = candle_collection.CandleCollector(store, histories={}, provider=provider,
        journal=object(), symbols=("AAPL",), session_id=SESSION,
        started_at="2026-07-10T22:00:00Z", clock=clock, stopped=stopped, pause=pause)
    return collector, store, provider


@pytest.mark.parametrize("kind", ["busy", "locked"])
def test_real_native_busy_and_locked_retry_exact_write(tmp_path, capsys, kind):
    path = tmp_path / "private.sqlite3"
    uri = path.as_uri() + "?cache=shared" if kind == "locked" else str(path)
    holder = sqlite3.connect(uri, uri=kind == "locked", timeout=0)
    holder.execute("CREATE TABLE work(value TEXT)")
    holder.commit()
    if kind == "locked":
        holder.execute("BEGIN")
        holder.execute("SELECT * FROM work").fetchall()
    else:
        holder.execute("BEGIN IMMEDIATE")
    attempts, events = [], []

    def action():
        attempts.append(True)
        with sqlite3.connect(uri, uri=kind == "locked", timeout=0) as db:
            db.execute("INSERT INTO work VALUES('done')")
        return "original-result"

    try:
        assert candle_collection._retry_collection_storage(action,
            store=SimpleNamespace(event=lambda *args: events.append(args)), session_id=SESSION,
            operation="fixture_write", stopped=lambda: False,
            pause=lambda seconds: holder.rollback()) == "original-result"
    finally:
        holder.rollback()
        holder.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM work").fetchall() == [("done",)]
    assert attempts == [True, True] and len(events) == 1
    diagnostic = events[0][2]["last_failure"]["root_cause"]
    assert diagnostic["sqlite_errorcode"] & 255 == (sqlite3.SQLITE_LOCKED if kind == "locked" else sqlite3.SQLITE_BUSY)
    assert diagnostic["frames"][-1]["function"] == "action"
    output = capsys.readouterr().out
    assert "collection_storage_contention" in output and str(path) not in output


def test_real_initialization_contention_resumes_final_collection(monkeypatch, tmp_path):
    fake_live_histories(monkeypatch)
    clock = Clock("2026-07-11T00:00:01Z")
    store = FastStore(tmp_path / "shadow.sqlite3")
    provider = ProviderFixture(clock)
    lock = CompetingWriter(store, lambda sql: "CREATE TABLE IF NOT EXISTS intraday_candle_collection" in sql)
    try:
        result = candle_collection.collection_loop(store, histories={}, provider=provider,
            journal=object(), symbols=("AAPL",), session_id=SESSION,
            started_at="2026-07-10T22:00:00Z", clock=clock, pause=lock.pause)
    finally:
        lock.close()
    assert result["final_complete"] and len(provider.calls) == 1
    assert lock.starts == lock.releases == 1
    with store.connect() as db:
        event = json.loads(db.execute("SELECT payload FROM events WHERE kind='collection_storage_retry_completed'").fetchone()[0])
    assert event["operation"] == "initialize" and event["operation_returned_normally"]


def test_real_extended_busy_snapshot_retries_after_releasing_stale_read(tmp_path):
    path = tmp_path / "snapshot.sqlite3"
    reader = sqlite3.connect(path, timeout=0)
    reader.execute("PRAGMA journal_mode=WAL")
    reader.execute("CREATE TABLE work(value INTEGER)")
    reader.execute("INSERT INTO work VALUES(1)")
    reader.commit()
    reader.execute("BEGIN")
    reader.execute("SELECT value FROM work").fetchone()
    with sqlite3.connect(path, timeout=0) as writer:
        writer.execute("UPDATE work SET value=2")
    events, attempts = [], []

    def update():
        attempts.append(True)
        reader.execute("UPDATE work SET value=3")
        reader.commit()

    try:
        candle_collection._retry_collection_storage(update,
            store=SimpleNamespace(event=lambda *args: events.append(args)),
            session_id=SESSION, operation="stale_snapshot", stopped=lambda: False,
            pause=lambda seconds: reader.rollback())
        assert reader.execute("SELECT value FROM work").fetchone()[0] == 3
    finally:
        reader.close()
    assert attempts == [True, True]
    assert events[0][2]["last_failure"]["root_cause"]["sqlite_errorcode"] == sqlite3.SQLITE_BUSY_SNAPSHOT


def test_real_schedule_contention_resumes_same_collector(monkeypatch, tmp_path):
    collector, store, provider = fixture(monkeypatch, tmp_path)
    # Scheduling uses executemany, so contend before its read/write transaction.
    holder = sqlite3.connect(store.path, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    collector.pause = lambda seconds: holder.rollback()
    try:
        result = collector._storage("pass_once", collector.pass_once)
    finally:
        holder.rollback()
        holder.close()
    assert result["final_complete"] and len(provider.calls) == 1
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM candles").fetchone()[0] == 1
        assert db.execute("SELECT status FROM intraday_candle_collection").fetchone()[0] == "saved"


@pytest.mark.parametrize("target", ["status_update", "saved_event", "final_event"])
def test_post_candle_bookkeeping_retries_without_reacquisition(monkeypatch, tmp_path, target):
    collector, store, provider = fixture(monkeypatch, tmp_path)
    predicate = {
        "status_update": lambda sql: "SET status='saved'" in sql,
        "saved_event": lambda sql: False,
        "final_event": lambda sql: False,
    }[target]
    lock = CompetingWriter(store, predicate)
    original_event = store.event

    def event(cycle, kind, payload, **kwargs):
        wanted = "candle_collection_saved" if target == "saved_event" else "final_candle_collection_completed"
        if target != "status_update" and kind == wanted:
            lock.before_sql("target_event")
        return original_event(cycle, kind, payload, **kwargs)

    if target != "status_update":
        lock.predicate = lambda sql: sql == "target_event"
        monkeypatch.setattr(store, "event", event)
    collector.pause = lock.pause
    try:
        result = collector._storage("pass_once", collector.pass_once)
    finally:
        lock.close()
    assert result["final_complete"] and len(provider.calls) == 1
    assert lock.starts == lock.releases == 1
    with store.connect() as db:
        original = db.execute("SELECT id,payload FROM candles").fetchall()
        assert len(original) == 1
        assert db.execute("SELECT count(*) FROM events WHERE kind='candle_collection_saved'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events WHERE kind='final_candle_collection_completed'").fetchone()[0] == 1
    assert collector.pass_once()["final_complete"] and len(provider.calls) == 1
    with store.connect() as db:
        assert db.execute("SELECT id,payload FROM candles").fetchall() == original


@pytest.mark.parametrize("source_empty", [False, True])
def test_original_failure_bookkeeping_retries_without_repeating_acquisition(monkeypatch, tmp_path, source_empty):
    collector, store, provider = fixture(monkeypatch, tmp_path)
    final = instant("2026-07-10T23:45:00Z")
    if source_empty:
        def empty(symbol, reference):
            provider.calls.append((symbol, reference, provider.clock()))
            raise SourceIntervalEmpty({"receipt_id": "original-exact-empty-receipt"})
        provider.completed_candle = empty
        predicate = lambda sql: "SET status='source_empty'" in sql
    else:
        provider.pending[("AAPL", final)] = 1
        predicate = lambda sql: "SET next_attempt_at=" in sql
    lock = CompetingWriter(store, predicate)
    collector.pause = lock.pause
    try:
        result = collector._storage("pass_once", collector.pass_once)
    finally:
        lock.close()
    assert len(provider.calls) == 1 and lock.starts == lock.releases == 1
    assert result["final_complete"] is source_empty
    with store.connect() as db:
        status, last_error = db.execute("SELECT status,last_error FROM intraday_candle_collection").fetchone()
        assert status == ("source_empty" if source_empty else "pending")
        assert ("original-exact-empty-receipt" if source_empty else "fixture_delayed_publication") in last_error
        assert db.execute("SELECT count(*) FROM candles").fetchone()[0] == 0


def test_manual_stop_during_contention_retains_pending_work(monkeypatch, tmp_path):
    stopped = [False]
    collector, store, provider = fixture(monkeypatch, tmp_path, stopped=lambda: stopped[0])
    holder = sqlite3.connect(store.path, timeout=0)
    holder.execute("BEGIN IMMEDIATE")

    def stop(seconds):
        stopped[0] = True
        holder.rollback()

    collector.pause = stop
    try:
        with pytest.raises(InterruptedError):
            collector._storage("pass_once", collector.pass_once)
    finally:
        holder.rollback()
        holder.close()
    assert provider.calls == []
    assert collector.pass_once()["saved"] == 0


def test_non_lock_operational_error_propagates_without_retry(tmp_path):
    attempts = []

    def action():
        attempts.append(True)
        with sqlite3.connect(tmp_path / "fixture.sqlite3") as db:
            db.execute("SELECT * FROM missing_table")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        candle_collection._retry_collection_storage(action,
            store=SimpleNamespace(event=lambda *args: pytest.fail("No recovery event")),
            session_id=SESSION, operation="fixture", stopped=lambda: False,
            pause=lambda _: pytest.fail("Non-lock failure must not pause"))
    assert attempts == [True]


def test_successful_operation_is_not_repeated_when_retry_event_fails(capsys):
    calls = []
    busy = sqlite3.OperationalError("private-token-never-print")
    busy.sqlite_errorcode, busy.sqlite_errorname = sqlite3.SQLITE_BUSY, "SQLITE_BUSY"

    def action():
        calls.append(True)
        if len(calls) == 1:
            raise busy
        return "original-result"

    def failed_event(*args):
        raise RuntimeError("private-event-value-never-print")

    assert candle_collection._retry_collection_storage(action,
        store=SimpleNamespace(event=failed_event), session_id=SESSION, operation="fixture",
        stopped=lambda: False, pause=lambda _: None) == "original-result"
    assert calls == [True, True]
    output = capsys.readouterr().out
    assert "collection_storage_retry_diagnostic_unavailable" in output
    assert "private-token" not in output and "private-event" not in output
