"""Offline scoring continuation under native local SQLite contention."""
from contextlib import contextmanager
import json
import sqlite3
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday import live_session
from ml.intraday.prediction import ContractError, instant
from ml.intraday.scoring_storage import (
    record_scoring_worker_failure, retry_scoring_storage, scoring_failure_evidence,
)
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import candle, forecast


SESSION = "manual-atlas-15m:" + "a" * 32
NOW = "2026-10-08T18:00:00Z"


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


class FastStore(ShadowStore):
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()


def saved_fixture(tmp_path, *, save_candle=True):
    store = FastStore(tmp_path / "shadow.sqlite3")
    value = forecast(mode="live")
    store.begin_cycle(value["cycle_id"], dict(pc="Atlas", mode="live", experiment=SESSION,
        symbols=[value["symbol"]], reference_at=value["reference_at"], configuration_hash="fixture",
        started_at=value["issued_at"]))
    original = store.save_forecast(value)
    target = candle()
    if save_candle:
        store.record_candle(target)
    with store.connect() as db:
        frozen = db.execute("SELECT id,payload FROM forecasts").fetchall()
    return store, original, target, frozen


def run_one_pass(store, *, monkeypatch, pause, session_id="unrelated-manual-session", histories=None):
    stopped = [False]

    def sleep(seconds):
        if seconds == 3:
            stopped[0] = True
        else:
            pause(seconds)

    live_session.scoring_loop(store, histories=histories or {}, provider=object(), journal=object(),
        session_id=session_id, stopped=lambda: stopped[0], clock=lambda: NOW, pause=sleep)


def target_history(target):
    frame = pd.DataFrame([target])
    for name in ("opened_at", "available_at"):
        frame[name] = pd.to_datetime(frame[name], utc=True)
    return SimpleNamespace(frame=frame)


@pytest.mark.parametrize("method", [
    "retained_session_scoring_targets", "observed_candles", "scoring_forecasts", "revisions",
])
def test_storage_iterables_are_fully_consumed_inside_retry_before_scoring(tmp_path, monkeypatch, method):
    store, original, target, frozen = saved_fixture(tmp_path)
    holder = sqlite3.connect(store.path, timeout=0)
    original_method = getattr(store, method)
    attempts = []
    if method == "revisions":
        # Reach the explicit outcome-revision read without the inventory's own
        # internal revision lookup consuming the injected failure first.
        monkeypatch.setattr(store, "scoring_forecasts", lambda **kw: [original])

    def contended(*args, **kwargs):
        attempts.append(True)
        rows = list(original_method(*args, **kwargs))
        if len(attempts) == 1:
            holder.execute("BEGIN IMMEDIATE")
            if rows:
                yield rows[0]
            # Raise an actual native contention error after yielding a row.
            # The caller must not act on the partial iterable.
            with store.connect() as db:
                db.execute("INSERT INTO events(kind) VALUES('must-not-commit')")
        yield from rows

    monkeypatch.setattr(store, method, contended)
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kw: pytest.fail("Saved targets require no provider call"))
    try:
        run_one_pass(store, monkeypatch=monkeypatch, pause=lambda _: holder.rollback(),
            session_id=SESSION if method == "retained_session_scoring_targets" else "other")
    finally:
        holder.rollback()
        holder.close()
    assert len(attempts) >= 2
    assert list(store.revisions(original["forecast_id"]))[-1]["status"] == "scored"
    with store.connect() as db:
        assert db.execute("SELECT id,payload FROM forecasts").fetchall() == frozen
        assert db.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events WHERE kind='must-not-commit'").fetchone()[0] == 0


@pytest.mark.parametrize("acquisition", ["saved", "acquired", "source_empty", "failure"])
def test_outcome_or_event_contention_does_not_repeat_provider_acquisition(tmp_path, monkeypatch, acquisition):
    store, original, target, frozen = saved_fixture(tmp_path, save_candle=acquisition == "saved")
    holder = sqlite3.connect(store.path, timeout=0)
    calls, attempts = [], []
    original_observe, original_event = store.observe, store.event
    event_kind = {"source_empty": "target_source_empty", "failure": "target_scoring_retry"}.get(acquisition)

    class Incoming:
        def __init__(self, **kwargs):
            pass
        def bind_cycle(self, identity):
            assert identity == "score:" + original["forecast_id"]
        def get(self, symbol):
            calls.append(symbol)
            if acquisition == "source_empty":
                raise live_session.SourceIntervalEmpty({"receipt_id": "fixture-source-empty"})
            if acquisition == "failure":
                raise ContractError("Fixture input remains unavailable")
            return target_history(target)

    def hold_once():
        attempts.append(True)
        if len(attempts) == 1:
            holder.execute("BEGIN IMMEDIATE")

    def observe(*args, **kwargs):
        if event_kind is None:
            hold_once()
        return original_observe(*args, **kwargs)

    def event(session, kind, payload, **kwargs):
        if kind == event_kind:
            hold_once()
        return original_event(session, kind, payload, **kwargs)

    monkeypatch.setattr(live_session, "LiveHistories", Incoming)
    monkeypatch.setattr(store, "observe", observe)
    monkeypatch.setattr(store, "event", event)
    try:
        run_one_pass(store, monkeypatch=monkeypatch, pause=lambda _: holder.rollback(), histories={"AAPL": object()})
    finally:
        holder.rollback()
        holder.close()
    assert calls == ([] if acquisition == "saved" else ["AAPL"])
    assert len(attempts) == 2
    revisions = store.revisions(original["forecast_id"])
    assert len(revisions) == (0 if acquisition == "failure" else 1)
    if revisions:
        assert revisions[0]["status"] == ("unscorable" if acquisition == "source_empty" else "scored")
    with store.connect() as db:
        assert db.execute("SELECT id,payload FROM forecasts").fetchall() == frozen
        if event_kind:
            assert db.execute("SELECT count(*) FROM events WHERE kind=?", (event_kind,)).fetchone()[0] == 1


def test_acquired_outcome_non_contention_failure_is_not_hidden_as_acquisition_retry(tmp_path, monkeypatch):
    store, original, target, _ = saved_fixture(tmp_path, save_candle=False)
    calls = []
    error = native_error(sqlite3.SQLITE_IOERR)
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kw: SimpleNamespace(
        bind_cycle=lambda _: None, get=lambda symbol: (calls.append(symbol) or target_history(target))))
    monkeypatch.setattr(store, "observe", lambda *a, **kw: (_ for _ in ()).throw(error))
    with pytest.raises(sqlite3.OperationalError) as caught:
        run_one_pass(store, monkeypatch=monkeypatch, pause=lambda _: pytest.fail("Fatal storage cannot pause"),
            histories={"AAPL": object()})
    assert caught.value is error and calls == ["AAPL"]
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='target_scoring_retry'").fetchone()[0] == 0


@pytest.mark.parametrize("interruption", ["manual_stop", "market_close"])
def test_scoring_contention_honors_stop_and_close_without_another_attempt(tmp_path, monkeypatch, interruption):
    store, _, _, _ = saved_fixture(tmp_path)
    stopped, clock, calls = [False], [instant(NOW)], []
    close = instant("2026-10-08T18:00:01Z")

    def blocked(**kwargs):
        calls.append(True)
        raise native_error()

    def pause(seconds):
        assert seconds == 1
        if interruption == "manual_stop":
            stopped[0] = True
        else:
            clock[0] = close

    monkeypatch.setattr(store, "retained_session_scoring_targets", blocked)
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kw: pytest.fail("Interrupted scoring cannot acquire data"))
    with pytest.raises(InterruptedError):
        live_session.scoring_loop(store, histories={}, provider=object(), journal=object(), session_id=SESSION,
            stopped=lambda: stopped[0], clock=lambda: clock[0], pause=pause, close_at=close)
    assert calls == [True]


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

@pytest.mark.parametrize("acquisition", ["saved", "acquired", "source_empty"])
@pytest.mark.parametrize("contended", [False, True])
def test_scoring_retains_concurrent_newer_outcome_without_reacquisition(
        tmp_path, monkeypatch, capsys, acquisition, contended):
    store, original, target, frozen = saved_fixture(tmp_path, save_candle=acquisition == "saved")
    holder = sqlite3.connect(store.path, timeout=0)
    original_observe = store.observe
    calls, attempts, committed, pauses = [], [], [], []
    newer_target = dict(target, close="99", available_at="2026-10-08T17:59:00Z")

    class Incoming:
        def __init__(self, **kwargs):
            assert acquisition != "saved", "Persisted target must not request acquisition"
        def bind_cycle(self, identity):
            assert identity == "score:" + original["forecast_id"]
        def get(self, symbol):
            calls.append(symbol)
            if acquisition == "source_empty":
                raise live_session.SourceIntervalEmpty({"receipt_id": "fixture-source-empty"})
            return target_history(target)

    def commit_newer():
        assert not committed
        committed.append(original_observe(original, as_of="2026-10-08T18:00:01Z", candle=newer_target))

    def observe(forecast, **kwargs):
        attempts.append(dict(kwargs))
        if len(attempts) == 1:
            if contended:
                holder.execute("BEGIN IMMEDIATE")
            else:
                # The original scorer already had this race even without a
                # contention retry: a valid newer revision can win after as_of.
                commit_newer()
        return original_observe(forecast, **kwargs)

    def pause(seconds):
        assert contended and seconds == 1 and not pauses
        pauses.append(seconds)
        holder.rollback()
        # A separate writer releases contention and commits a corrected exact
        # target before the scoring caller retries its captured older as_of.
        commit_newer()

    monkeypatch.setattr(live_session, "LiveHistories", Incoming)
    monkeypatch.setattr(store, "observe", observe)
    try:
        run_one_pass(store, monkeypatch=monkeypatch, pause=pause,
            session_id=SESSION, histories={"AAPL": object()})
    finally:
        holder.rollback()
        holder.close()

    assert calls == ([] if acquisition == "saved" else ["AAPL"])
    assert pauses == ([1] if contended else [])
    assert attempts[0]["as_of"] == NOW
    if contended:
        assert attempts[1]["as_of"] == NOW
        assert '"sqlite_errorname":"SQLITE_BUSY"' in capsys.readouterr().out
    if acquisition == "source_empty":
        assert "unavailable_reason" in attempts[0]
    else:
        assert attempts[0]["candle"] == target
    assert len(committed) == 1 and committed[0]["status"] == "scored"
    assert committed[0]["target_not_down"] is False
    assert store.revisions(original["forecast_id"]) == committed
    with store.connect() as db:
        assert db.execute("SELECT id,payload FROM forecasts").fetchall() == frozen
        assert db.execute("SELECT count(*) FROM outcomes").fetchone()[0] == 1
    # Only the live scoring caller opts in. Other callers retain the original
    # strict no-backdating contract, even if the candle was already recorded.
    with pytest.raises(ContractError, match="backdated"):
        original_observe(original, as_of=NOW, candle=target)
    assert store.revisions(original["forecast_id"]) == committed
