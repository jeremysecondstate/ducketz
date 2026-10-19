"""Exact saved-evidence checks recover only bounded native SQLite contention."""
import json
import sqlite3
import threading

import pytest

from ml.intraday import local_forecast as reader
from ml.intraday.prediction import ContractError
from test_intraday_scout_local_execution import env, forecast


def native_error(code):
    exc = sqlite3.OperationalError("database is locked" if code != sqlite3.SQLITE_IOERR else "disk I/O error")
    exc.sqlite_errorcode = code
    return exc


def read(env):
    return reader._saved_forecast(env.store.path, env.forecast,
        session_id=env.executor.session_id, session_started=env.executor.session_started_at)


def test_recovery_error_on_first_select_closes_failed_connection(env, monkeypatch):
    connect = sqlite3.connect
    connections, closes, failures = [], [], []
    class RecoveringConnection:
        def __init__(self, db):
            self.db = db
            connections.append(self)
        def execute(self, sql, *args):
            if sql.startswith("SELECT " + reader._FORECAST_COLUMNS) and not failures:
                failures.append(1)
                raise native_error(261)
            return self.db.execute(sql, *args)
        def close(self):
            self.db.close()
            closes.append(self)
    def recovering_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        return RecoveringConnection(db)
    monkeypatch.setattr(reader.sqlite3, "connect", recovering_connect)
    monkeypatch.setattr(reader.time, "sleep", lambda _: None)
    assert read(env) is None
    assert len(connections) == 2 and failures == [1] and closes == connections
    assert env.broker.posts == []


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED, 261, 517, 262])
def test_native_contention_reopens_and_preserves_exact_evidence(env, monkeypatch, code):
    original = reader._saved_forecast_once
    attempts, pauses = [], []
    def once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise native_error(code)
        return original(*args, **kwargs)
    monkeypatch.setattr(reader, "_saved_forecast_once", once)
    monkeypatch.setattr(reader.time, "sleep", pauses.append)
    assert read(env) is None
    assert len(attempts) == 2 and pauses == [reader._READ_RETRY_PAUSE_SECONDS]
    assert env.store.forecast(env.forecast["cycle_id"], env.forecast["symbol"]) == env.forecast
    assert env.broker.posts == []


def test_real_locked_connection_is_closed_before_fresh_retry(env, monkeypatch):
    # A real exclusive lock exercises SQLite's reader failure; the separately
    # controlled connection releases only after the failed read reaches retry.
    locker = sqlite3.connect(env.store.path, check_same_thread=False)
    locker.execute("PRAGMA journal_mode=DELETE")
    locker.execute("BEGIN EXCLUSIVE")
    release = threading.Event()
    released = threading.Event()
    def unlock():
        try:
            assert release.wait(2)
            locker.rollback()
        finally:
            locker.close()
            released.set()
    worker = threading.Thread(target=unlock)
    worker.start()
    calls = []
    def pause(seconds):
        calls.append(seconds)
        release.set()
        assert released.wait(2)
    monkeypatch.setattr(reader.time, "sleep", pause)
    try:
        assert read(env) is None
        assert calls == [reader._READ_RETRY_PAUSE_SECONDS]
        assert env.store.forecast(env.forecast["cycle_id"], env.forecast["symbol"]) == env.forecast
        assert env.broker.posts == []
    finally:
        release.set()
        worker.join(2)
        assert not worker.is_alive()


@pytest.mark.parametrize("deadline_expires", [False, True])
def test_persistent_native_lock_has_attempt_and_elapsed_bounds(env, monkeypatch, deadline_expires):
    elapsed, attempts, pauses = [0.0], [], []
    error = native_error(261)
    def once(*args, **kwargs):
        attempts.append(1)
        if deadline_expires:
            elapsed[0] += reader._READ_RETRY_SECONDS
        raise error
    def pause(seconds):
        pauses.append(seconds)
        elapsed[0] += seconds
    monkeypatch.setattr(reader, "_saved_forecast_once", once)
    monkeypatch.setattr(reader.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(reader.time, "sleep", pause)
    with pytest.raises(sqlite3.OperationalError) as caught:
        read(env)
    assert caught.value is error
    assert len(attempts) == (1 if deadline_expires else reader._READ_MAX_ATTEMPTS)
    assert len(pauses) == len(attempts) - 1
    assert sum(pauses) < reader._READ_RETRY_SECONDS
    assert env.broker.posts == []


@pytest.mark.parametrize("code", [sqlite3.SQLITE_IOERR, sqlite3.SQLITE_CORRUPT, None])
def test_non_contention_errors_are_not_retried(env, monkeypatch, code):
    attempts, pauses = [], []
    error = native_error(code)
    def once(*args, **kwargs):
        attempts.append(1)
        raise error
    monkeypatch.setattr(reader, "_saved_forecast_once", once)
    monkeypatch.setattr(reader.time, "sleep", pauses.append)
    with pytest.raises(sqlite3.OperationalError) as caught:
        read(env)
    assert caught.value is error and len(attempts) == 1 and pauses == []


@pytest.mark.parametrize("changed", ["missing", "forecast_hash", "snapshot_hash", "session", "superseded"])
def test_retry_revalidates_changed_saved_evidence(env, monkeypatch, changed):
    original = reader._saved_forecast_once
    attempts = []
    def once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            if changed == "superseded":
                forecast(env, advance=True)
            else:
                with env.store.connect() as db:
                    if changed == "missing":
                        db.execute("DELETE FROM forecasts WHERE id=?", (env.forecast["forecast_id"],))
                    elif changed == "forecast_hash":
                        db.execute("UPDATE forecasts SET payload='{}' WHERE id=?", (env.forecast["forecast_id"],))
                    elif changed == "snapshot_hash":
                        db.execute("UPDATE snapshots SET payload='{}' WHERE id=?", (env.forecast["snapshot_hash"],))
                    else:
                        payload = json.loads(db.execute("SELECT payload FROM cycles WHERE id=?",
                            (env.forecast["cycle_id"],)).fetchone()[0])
                        payload["experiment"] = "another-manual-session"
                        db.execute("UPDATE cycles SET payload=? WHERE id=?",
                            (json.dumps(payload), env.forecast["cycle_id"]))
            raise native_error(261)
        return original(*args, **kwargs)
    monkeypatch.setattr(reader, "_saved_forecast_once", once)
    monkeypatch.setattr(reader.time, "sleep", lambda _: None)
    with pytest.raises(ContractError):
        read(env)
    assert len(attempts) == 2 and env.broker.posts == []
