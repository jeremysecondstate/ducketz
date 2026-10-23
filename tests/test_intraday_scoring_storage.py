"""Standalone scoring-storage retries use only offline temporary SQLite fixtures."""
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from ml.intraday.scoring_storage import (
    record_scoring_worker_failure, retry_scoring_storage, scoring_failure_evidence,
)

SESSION = "offline-main-scoring-session"


def native_error(code=sqlite3.SQLITE_BUSY, *, cls=sqlite3.OperationalError):
    error = cls("private-token=fixture-secret; C:/private/account.sqlite3")
    error.sqlite_errorcode = code
    names = {sqlite3.SQLITE_BUSY: "SQLITE_BUSY", sqlite3.SQLITE_LOCKED: "SQLITE_LOCKED",
             sqlite3.SQLITE_ERROR: "SQLITE_ERROR", sqlite3.SQLITE_IOERR: "SQLITE_IOERR",
             sqlite3.SQLITE_FULL: "SQLITE_FULL", 517: "SQLITE_BUSY_SNAPSHOT"}
    error.sqlite_errorname = names.get(code, "private-untrusted-name")
    return error


def retry(action, *, store=None, stopped=lambda: False, pause=lambda _: None):
    return retry_scoring_storage(action, store=store or SimpleNamespace(event=lambda *a: None),
        session_id=SESSION, operation="fixture_storage", stopped=stopped, pause=pause)


@pytest.mark.parametrize("shared_cache", [False, True])
def test_real_busy_and_locked_retry_exact_storage_after_competing_writer_releases(tmp_path, capsys, shared_cache):
    path = tmp_path / "contention.sqlite3"
    uri = path.as_uri() + "?cache=shared" if shared_cache else str(path)
    holder = sqlite3.connect(uri, uri=shared_cache, timeout=0)
    holder.execute("CREATE TABLE work(value TEXT)")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO work VALUES('uncommitted')")
    calls, events = [], []
    expected = object()

    def action():
        calls.append(True)
        with sqlite3.connect(uri, uri=shared_cache, timeout=0) as db:
            db.execute("INSERT INTO work VALUES('actual-result')")
        return expected

    try:
        assert retry(action, store=SimpleNamespace(event=lambda *a: events.append(a)),
                     pause=lambda _: holder.rollback()) is expected
    finally:
        holder.rollback()
        holder.close()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM work").fetchall() == [("actual-result",)]
    assert calls == [True, True] and len(events) == 1
    assert events[0][:2] == (SESSION, "scoring_storage_retry_completed")
    code = events[0][2]["last_failure"]["root_cause"]["sqlite_errorcode"]
    assert code & 255 == (sqlite3.SQLITE_LOCKED if shared_cache else sqlite3.SQLITE_BUSY)
    assert str(path) not in capsys.readouterr().out


def test_extended_busy_retries_but_successful_operation_is_not_repeated_for_diagnostic_failure(capsys):
    calls = []
    expected = object()

    def action():
        calls.append(True)
        if len(calls) == 1:
            raise native_error(sqlite3.SQLITE_BUSY_SNAPSHOT)
        return expected

    def unavailable(*args):
        raise RuntimeError("private-token=event-sink-secret")

    assert retry(action, store=SimpleNamespace(event=unavailable)) is expected
    assert calls == [True, True]
    output = capsys.readouterr().out
    assert "scoring_storage_retry_diagnostic_unavailable" in output
    assert all(value not in output for value in ("private-token", "event-sink-secret", "account.sqlite3"))


@pytest.mark.parametrize("error", [
    native_error(sqlite3.SQLITE_ERROR), native_error(sqlite3.SQLITE_IOERR), native_error(sqlite3.SQLITE_FULL),
    native_error(None), native_error("5"), native_error(True),
    sqlite3.OperationalError("database is locked"),
    native_error(cls=sqlite3.DatabaseError), RuntimeError("fixture-fatal"),
])
def test_non_contention_or_untyped_errors_keep_original_failure(error):
    calls = []

    def action():
        calls.append(True)
        raise error

    with pytest.raises(type(error)) as caught:
        retry(action, pause=lambda _: pytest.fail("Only native contention may retry"))
    assert caught.value is error and calls == [True]


def test_diagnostic_write_failure_cannot_bypass_worker_stop_latch(capsys):
    stopped, failures = threading.Event(), []

    def unavailable(*args):
        assert stopped.is_set() and failures[0]["worker"] == "scoring"
        raise RuntimeError("private-token=diagnostic-sink-secret")

    try:
        raise native_error(sqlite3.SQLITE_FULL)
    except sqlite3.OperationalError as exc:
        record_scoring_worker_failure(exc, store=SimpleNamespace(event=unavailable),
            session_id=SESSION, worker_errors=failures, stopped=stopped)
    assert stopped.is_set() and failures[0]["diagnostics"]["root_cause"]["sqlite_errorcode"] == sqlite3.SQLITE_FULL
    output = capsys.readouterr().out
    assert "scoring_worker_stopped" in output
    assert all(value not in output for value in ("private-token", "diagnostic-sink-secret", "account.sqlite3"))


def test_failure_evidence_bounds_chains_and_frames_without_private_values():
    errors = [RuntimeError("private-token=chain-secret") for _ in range(12)]
    for outer, inner in zip(errors, errors[1:]):
        outer.__cause__ = inner
    errors[-1].__cause__ = errors[0]
    evidence = scoring_failure_evidence(errors[0])
    assert len(evidence["exception_chain"]) == 8 and evidence["exception_chain_truncated"] is True
    assert "private-token" not in json.dumps(evidence)
    spoofed = native_error()
    spoofed.sqlite_errorname = "private-untrusted-name"
    assert "sqlite_errorcode" not in scoring_failure_evidence(spoofed)["root_cause"]
