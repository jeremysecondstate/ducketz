"""Database telemetry never retains SQLite writer ownership; offline fixtures only."""
import sqlite3
import threading

import pytest

from ml.intraday import scout_services
from ml.intraday.service_handoff import ServiceStore


@pytest.fixture
def store(tmp_path):
    return ServiceStore(tmp_path / "shadow.sqlite3", object_root=tmp_path)


def insert(db, key="fixture"):
    db.execute("INSERT INTO events(cycle_id,kind,payload) VALUES(?,?,?)", (key, "fixture", "{}"))


def competing_writer(path):
    db = sqlite3.connect(path, timeout=.05)
    try:
        db.execute("BEGIN IMMEDIATE")
    finally:
        db.close()


def test_blocked_metric_does_not_block_another_database_writer(store, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    failures = []

    def metric(*args, **values):
        entered.set()
        if not release.wait(3):
            raise RuntimeError("fixture telemetry timeout")

    monkeypatch.setattr(scout_services, "metric", metric)

    def writer():
        try:
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                insert(db)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=writer)
    worker.start()
    try:
        assert entered.wait(2), "fixture never reached telemetry"
        competing_writer(store.path)
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert failures == []
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM events WHERE cycle_id='fixture'").fetchone() == (1,)


def test_metrics_are_deferred_until_connection_exit(store, monkeypatch):
    rows = []
    monkeypatch.setattr(scout_services, "metric", lambda *args, **values: rows.append((args, values)))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        insert(db)
        assert rows == []
    assert [row[0][2] for row in rows] == ["writer_admission", "write_statement"]
    assert all(row[1]["busy"] is False and row[1]["seconds"] >= 0 for row in rows)


def test_metric_failure_preserves_committed_effects_and_release(store, monkeypatch):
    attempts = []

    def metric(*args, **values):
        attempts.append(args[2])
        raise OSError("fixture metric unavailable")

    monkeypatch.setattr(scout_services, "metric", metric)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        insert(db)
    competing_writer(store.path)
    assert attempts == ["writer_admission", "write_statement"]
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM events WHERE cycle_id='fixture'").fetchone() == (1,)


def test_rollback_releases_writer_before_metrics_and_preserves_original_error(store, monkeypatch):
    outcomes = []

    def metric(*args, **values):
        try:
            competing_writer(store.path)
        except sqlite3.OperationalError as exc:
            outcomes.append(exc.sqlite_errorname)
        else:
            outcomes.append("released")

    monkeypatch.setattr(scout_services, "metric", metric)
    original = ValueError("fixture original body error")
    with pytest.raises(ValueError) as caught:
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            insert(db)
            raise original
    assert caught.value is original
    assert outcomes == ["released", "released"]
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM events WHERE cycle_id='fixture'").fetchone() == (0,)


def test_native_busy_remains_native_and_keeps_busy_measurement(store, monkeypatch):
    rows = []
    monkeypatch.setattr(scout_services, "metric", lambda *args, **values: rows.append((args, values)))
    owner = sqlite3.connect(store.path)
    try:
        owner.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            with store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
        assert caught.value.sqlite_errorname == "SQLITE_BUSY"
    finally:
        owner.close()
    assert len(rows) == 1
    assert rows[0][0][2] == "writer_admission" and rows[0][1]["busy"] is True
    competing_writer(store.path)


def test_metric_buffer_is_bounded_without_losing_database_effects(store, monkeypatch):
    rows = []
    monkeypatch.setattr(scout_services, "metric", lambda *args, **values: rows.append((args, values)))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for index in range(140):
            insert(db, str(index))
        assert rows == []
    measured = [row for row in rows if row[0][2] != "measurement_overflow"]
    overflow = [row for row in rows if row[0][2] == "measurement_overflow"]
    assert len(measured) == 128
    assert len(overflow) == 1 and overflow[0][1] == {"dropped_count": 13}
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone() == (140,)


def test_second_close_does_not_repeat_metrics(store, monkeypatch):
    rows = []
    monkeypatch.setattr(scout_services, "metric", lambda *args, **values: rows.append((args, values)))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        insert(db)
    assert len(rows) == 2
    db.close()
    assert len(rows) == 2


def test_sql_failure_releases_writer_before_metrics_and_is_not_masked(store, monkeypatch):
    outcomes = []

    def metric(*args, **values):
        try:
            competing_writer(store.path)
        except sqlite3.OperationalError as exc:
            outcomes.append(exc.sqlite_errorname)
        else:
            outcomes.append("released")
        raise OSError("fixture diagnostic error")

    monkeypatch.setattr(scout_services, "metric", metric)
    with pytest.raises(sqlite3.OperationalError, match="no such table") as caught:
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            insert(db)
            db.execute("INSERT INTO nonexistent_table VALUES(1)")
    assert caught.value.sqlite_errorname == "SQLITE_ERROR"
    assert outcomes == ["released", "released", "released"]
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM events WHERE cycle_id='fixture'").fetchone() == (0,)


def test_failed_native_close_does_not_emit_until_owner_releases_writer(store, monkeypatch):
    outcomes, failures = [], []

    def metric(*args, **values):
        competing_writer(store.path)
        outcomes.append("released")

    monkeypatch.setattr(scout_services, "metric", metric)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        insert(db)

        def wrong_thread_close():
            try:
                db.close()
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=wrong_thread_close)
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], sqlite3.ProgrammingError)
        assert outcomes == []
        with pytest.raises(sqlite3.OperationalError) as caught:
            competing_writer(store.path)
        assert caught.value.sqlite_errorname == "SQLITE_BUSY"
        db.rollback()
        assert outcomes == []
    assert outcomes == ["released", "released"]
    db.close()
    assert outcomes == ["released", "released"]
    with sqlite3.connect(store.path) as reader:
        assert reader.execute("SELECT count(*) FROM events WHERE cycle_id='fixture'").fetchone() == (0,)
