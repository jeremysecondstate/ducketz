"""Offline recovery of Atlas's exact interrupted forecast-storage operation."""
from contextlib import contextmanager
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import live_session, runtime
from ml.intraday.prediction import instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast


SESSION = "manual-atlas-15m:" + "a" * 32


def sqlite_error(code=sqlite3.SQLITE_BUSY, *, error_type=sqlite3.OperationalError):
    error = error_type("private-token=do-not-log; C:/private/account.sqlite3")
    error.sqlite_errorcode = code
    names = {sqlite3.SQLITE_BUSY: "SQLITE_BUSY", sqlite3.SQLITE_LOCKED: "SQLITE_LOCKED",
             sqlite3.SQLITE_ERROR: "SQLITE_ERROR", sqlite3.SQLITE_IOERR: "SQLITE_IOERR",
             sqlite3.SQLITE_FULL: "SQLITE_FULL", 517: "SQLITE_BUSY_SNAPSHOT"}
    error.sqlite_errorname = names.get(code, "private-untrusted-sqlite-name")
    return error


def event_store():
    events = []
    return SimpleNamespace(event=lambda *args, **kwargs: events.append((args, kwargs))), events


def retry(action, store, *, stopped=lambda: False, pause=lambda _: None, operation="run_cycle"):
    return live_session._retry_forecast_storage(action, store=store, session_id=SESSION,
        operation=operation, stopped=stopped, pause=pause)


def test_real_sqlite_busy_retries_exact_write_after_competing_transaction_rolls_back(tmp_path, capsys):
    path = tmp_path / "private-storage.sqlite3"
    holder = sqlite3.connect(path, timeout=0)
    holder.execute("CREATE TABLE work(value TEXT)")
    holder.commit()
    holder.execute("BEGIN IMMEDIATE")
    attempts, pauses = [], []
    store, events = event_store()
    expected = object()

    def exact_write():
        attempts.append("same-write")
        with sqlite3.connect(path, timeout=0) as db:
            db.execute("INSERT INTO work VALUES('finished')")
        return expected

    def release(seconds):
        pauses.append(seconds)
        assert len(attempts) == 1 and events == []
        holder.rollback()

    try:
        assert retry(exact_write, store, pause=release) is expected
    finally:
        holder.rollback()
        holder.close()
    with sqlite3.connect(path) as verified:
        assert verified.execute("SELECT value FROM work").fetchall() == [("finished",)]
    assert attempts == ["same-write", "same-write"] and pauses == [1]
    assert len(events) == 1
    args, kwargs = events[0]
    assert args[:2] == (SESSION, "forecast_storage_retry_completed") and kwargs == {}
    assert args[2]["operation"] == "run_cycle" and args[2]["retry_count"] == 1
    assert args[2]["operation_returned_normally"] is True
    assert args[2]["last_failure"]["root_cause"]["sqlite_errorcode"] & 255 == sqlite3.SQLITE_BUSY
    output = capsys.readouterr().out
    assert str(path) not in output and "forecast_storage_contention" in output


@pytest.mark.parametrize("error", [
    sqlite_error(sqlite3.SQLITE_ERROR), sqlite_error(sqlite3.SQLITE_LOCKED),
    sqlite_error(sqlite3.SQLITE_IOERR), sqlite_error(sqlite3.SQLITE_FULL),
    sqlite_error(None), sqlite_error("5"), sqlite_error(True),
    sqlite3.OperationalError("database is locked"),
    sqlite_error(sqlite3.SQLITE_BUSY, error_type=sqlite3.DatabaseError),
    sqlite_error(sqlite3.SQLITE_BUSY, error_type=RuntimeError),
])
def test_only_integer_busy_operational_errors_are_retried(error):
    store, events = event_store()
    attempts = []

    def fail():
        attempts.append(True)
        raise error

    with pytest.raises(type(error)) as caught:
        retry(fail, store, pause=lambda _: pytest.fail("Non-BUSY error must not pause"))
    assert caught.value is error and attempts == [True] and events == []


def test_wrapped_busy_error_keeps_original_fatal_exception():
    busy = sqlite_error()
    outer = RuntimeError("private outer message")
    outer.__cause__ = busy
    store, events = event_store()
    with pytest.raises(RuntimeError) as caught:
        retry(lambda: (_ for _ in ()).throw(outer), store,
              pause=lambda _: pytest.fail("A wrapped failure is not the exact SQLite operation"))
    assert caught.value is outer and caught.value.__cause__ is busy and events == []


def test_extended_busy_code_retries_without_changing_action_result():
    store, events = event_store()
    calls = []
    busy = sqlite_error(getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517))

    def operation():
        calls.append(True)
        if len(calls) == 1:
            raise busy
        return {"original": "result"}

    assert retry(operation, store) == {"original": "result"}
    assert len(calls) == 2 and events[0][0][2]["retry_count"] == 1


@pytest.mark.parametrize("already_stopped", [False, True])
def test_manual_stop_prevents_first_or_next_attempt(already_stopped):
    stopped = [already_stopped]
    attempts, pauses = [], []
    store, events = event_store()

    def action():
        attempts.append(True)
        raise sqlite_error()

    def stop(seconds):
        pauses.append(seconds)
        stopped[0] = True

    with pytest.raises(InterruptedError):
        retry(action, store, stopped=lambda: stopped[0], pause=stop)
    assert len(attempts) == (0 if already_stopped else 1)
    assert pauses == ([] if already_stopped else [1]) and events == []


@pytest.mark.parametrize("sink_error", [sqlite_error(), RuntimeError("private-token=event-sink-secret")])
def test_recovery_event_failure_does_not_repeat_successful_action(sink_error, capsys):
    attempts, events = [], []
    expected = object()

    def action():
        attempts.append(True)
        if len(attempts) == 1:
            raise sqlite_error()
        return expected

    def failed_event(*args):
        events.append(args)
        raise sink_error

    assert retry(action, SimpleNamespace(event=failed_event)) is expected
    assert len(attempts) == 2 and len(events) == 1
    output = capsys.readouterr().out
    assert "forecast_storage_retry_diagnostic_unavailable" in output
    assert "private-token" not in output and "event-sink-secret" not in output
    assert "private-untrusted-sqlite-name" not in output and "account.sqlite3" not in output


def test_failure_evidence_retains_trusted_code_and_chained_locations_without_private_values():
    private = "private-token=fixture-secret-never-print"

    def deepest(depth):
        if depth:
            return deepest(depth - 1)
        raise sqlite_error()

    try:
        try:
            deepest(20)
        except sqlite3.OperationalError as cause:
            raise RuntimeError(private) from cause
    except RuntimeError as error:
        evidence = live_session._forecast_failure_evidence(error)
    assert [item["error_type"] for item in evidence["exception_chain"]] == ["RuntimeError", "OperationalError"]
    assert evidence["root_cause"]["sqlite_errorcode"] == sqlite3.SQLITE_BUSY
    assert evidence["root_cause"]["sqlite_errorname"] == "SQLITE_BUSY"
    assert evidence["exception_chain_truncated"] is False
    assert 1 <= len(evidence["root_cause"]["frames"]) <= 8
    for item in evidence["exception_chain"]:
        assert len(item["error_type"]) <= 80 and len(item["frames"]) <= 8
        for frame in item["frames"]:
            assert len(frame["file"]) <= 120 and "/" not in frame["file"] and "\\" not in frame["file"]
            assert len(frame["function"]) <= 120 and type(frame["line"]) is int
    saved = json.dumps(evidence)
    assert all(secret not in saved for secret in (private, "private-token", "account.sqlite3", "private-untrusted-sqlite-name"))


def test_failure_evidence_bounds_cyclic_chains_and_drops_untrusted_sqlite_codes():
    errors = [RuntimeError("secret-" + str(index)) for index in range(12)]
    for outer, inner in zip(errors, errors[1:]):
        outer.__cause__ = inner
    errors[-1].__cause__ = errors[0]
    errors[0].sqlite_errorcode = sqlite3.SQLITE_BUSY
    errors[0].sqlite_errorname = "private-name"
    evidence = live_session._forecast_failure_evidence(errors[0])
    assert len(evidence["exception_chain"]) <= 8 and evidence["exception_chain_truncated"] is True
    assert all("sqlite_errorcode" not in item and "sqlite_errorname" not in item for item in evidence["exception_chain"])
    assert "secret-" not in json.dumps(evidence)
    for invalid in (True, "5", 123456789):
        item = live_session._forecast_failure_evidence(sqlite_error(invalid))["root_cause"]
        assert "sqlite_errorcode" not in item and "sqlite_errorname" not in item
    spoofed = sqlite_error()
    spoofed.sqlite_errorname = "private-untrusted-sqlite-name"
    assert "sqlite_errorcode" not in live_session._forecast_failure_evidence(spoofed)["root_cause"]


def test_suppressed_context_is_not_exposed_as_failure_cause():
    try:
        try:
            raise sqlite_error()
        except sqlite3.OperationalError:
            raise ValueError("private outer") from None
    except ValueError as error:
        evidence = live_session._forecast_failure_evidence(error)
    assert len(evidence["exception_chain"]) == 1
    assert evidence["root_cause"]["error_type"] == "ValueError"


@contextmanager
def immediate_store_connections(store):
    """Real local SQLite fixture writes fail promptly under a held writer."""
    db = sqlite3.connect(store.path, timeout=0)
    try:
        with db:
            yield db
    finally:
        db.close()


@pytest.mark.parametrize("blocked_at", ["begin", "summary", "finalization"])
def test_forecast_loop_recovers_same_cycle_and_preserves_original_forecast_queue(tmp_path, monkeypatch, blocked_at):
    store = ShadowStore(tmp_path / "forecast.sqlite3")
    queue = live_session.ExecutionQueue(store, SESSION)
    now = instant("2026-10-08T17:16:02Z")
    monkeypatch.setattr(store, "connect", lambda: immediate_store_connections(store))
    monkeypatch.setattr(live_session, "eligible_reference", lambda _: True)
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kwargs: SimpleNamespace(
        bind_cycle=lambda _: None, get=lambda _: pytest.fail("Fixture must not fetch data")))
    # A small configured symbol lane avoids selected-model manifests. The real
    # runner still owns logical cycle identity, persistence, reuse and callbacks.
    runner = runtime.ShadowRunner(store=store, pc="Atlas", symbols=("AAPL",),
        config=runtime.ResearchConfig(models=("fixture",)), clock=lambda: now,
        artifact_root=tmp_path / "models")
    original_begin, original_update = store.begin_cycle, store.update_cycle
    lock = sqlite3.connect(store.path, timeout=0, check_same_thread=False)
    held = [False]
    stopped = [False]
    begin_calls, run_calls, fits, pauses, published = [], [], [], [], []
    frozen = []
    update_attempts = []

    def hold():
        assert not held[0]
        lock.execute("BEGIN IMMEDIATE")
        held[0] = True

    def begin(identity, payload):
        begin_calls.append(identity)
        if blocked_at == "begin" and len(begin_calls) == 1:
            hold()
        return original_begin(identity, payload)

    def update(identity, changes):
        update_attempts.append((identity, dict(changes)))
        is_target = ((blocked_at == "summary" and changes.get("status") == "completed") or
                     (blocked_at == "finalization" and "last_recovery_attempt_at" in changes))
        if is_target and not any(item[0] == blocked_at for item in fits):
            fits.append((blocked_at, "injected-contention"))
            hold()
        result = original_update(identity, changes)
        if "last_recovery_attempt_at" in changes:
            stopped[0] = True
        return result

    def symbol(cycle_id, symbol, history, reference, cutoff, mode, intent, on_published, *args):
        existing = store.forecast(cycle_id, symbol)
        if existing is None:
            fits.append(("model", cycle_id))
            existing = store.save_forecast(forecast(mode=mode, cycle_id=cycle_id,
                issued_at=now.isoformat(), reference_at=reference.isoformat()))
            with store.connect() as db:
                frozen.append(db.execute("SELECT id,payload FROM forecasts WHERE id=?", (existing["forecast_id"],)).fetchone())
        published.append(existing["forecast_id"])
        on_published(existing)
        return existing

    original_run = runner.run_cycle

    def run(*args, **kwargs):
        run_calls.append(dict(kwargs))
        return original_run(*args, **kwargs)

    monkeypatch.setattr(store, "begin_cycle", begin)
    monkeypatch.setattr(store, "update_cycle", update)
    monkeypatch.setattr(runner, "_symbol", symbol)
    monkeypatch.setattr(runner, "run_cycle", run)

    def pause(seconds):
        pauses.append(seconds)
        if held[0]:
            lock.rollback()
            held[0] = False

    try:
        live_session.forecasting_loop(runner, histories={}, provider=object(), journal=object(), queue=queue,
            stopped=lambda: stopped[0], clock=lambda: now, pause=pause)
    finally:
        lock.rollback()
        lock.close()
    assert len([item for item in fits if item[0] == "model"]) == 1
    expected_run_attempts = 1 if blocked_at == "finalization" else 2
    assert len(run_calls) == expected_run_attempts
    assert len(set(begin_calls)) == 1
    assert all(call["reference_at"] == instant("2026-10-08T17:00:00Z") and
               call["experiment"] == SESSION and call["allow_execution"] is True and
               call["retry_symbols"] is None for call in run_calls)
    assert len(frozen) == 1 and set(published) == {frozen[0][0]}
    with store.connect() as db:
        assert db.execute("SELECT id,payload FROM forecasts").fetchall() == frozen
        queued = db.execute("SELECT forecast_id,session_id,status FROM intraday_live_queue").fetchall()
        events = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM events WHERE kind='forecast_storage_retry_completed'")]
        cycle = json.loads(db.execute("SELECT payload FROM cycles").fetchone()[0])
    assert queued == [(frozen[0][0], SESSION, "pending")]
    assert len(events) == 1 and events[0]["operation"] == (
        "cycle_finalization" if blocked_at == "finalization" else "run_cycle")
    assert events[0]["retry_count"] == 1 and events[0]["operation_returned_normally"] is True
    assert cycle["status"] == "completed" and cycle["last_recovery_attempt_at"] == now.isoformat()
    assert all(seconds == 1 for seconds in pauses)


def test_retry_after_quarter_rollover_does_not_queue_saved_eligible_older_forecast(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "rollover.sqlite3")
    queue = live_session.ExecutionQueue(store, SESSION)
    now = [instant("2026-10-08T17:16:02Z")]
    calls, frozen, pauses = [], [], []
    stopped = [False]
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kwargs: object())
    monkeypatch.setattr(live_session, "eligible_reference", lambda _: True)

    def run(incoming, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            # Publication was committed before a later storage failure. Its
            # original bytes remain eligible and must never be rewritten.
            saved = store.save_forecast(forecast(mode="live", issued_at=now[0].isoformat()))
            with store.connect() as db:
                frozen.append(db.execute("SELECT id,payload FROM forecasts").fetchone())
            raise sqlite_error()
        saved = store.forecast("cycle", "AAPL")
        kwargs["on_published"](saved)
        stopped[0] = True
        return {"cycle_id": "cycle"}

    def pause(seconds):
        pauses.append(seconds)
        now[0] = instant("2026-10-08T17:31:00Z")

    runner = SimpleNamespace(store=store, config=SimpleNamespace(publication_offset_seconds=30), run_cycle=run)
    live_session.forecasting_loop(runner, histories={}, provider=object(), journal=object(), queue=queue,
        stopped=lambda: stopped[0], clock=lambda: now[0], pause=pause)
    assert len(calls) == 2 and pauses == [1]
    assert calls[0]["allow_execution"] is True and calls[1]["allow_execution"] is False
    assert calls[0]["reference_at"] == calls[1]["reference_at"] == instant("2026-10-08T17:00:00Z")
    assert calls[0]["on_published"] == queue.enqueue
    with store.connect() as db:
        assert db.execute("SELECT id,payload FROM forecasts").fetchall() == frozen
        assert db.execute("SELECT COUNT(*) FROM intraday_live_queue").fetchone()[0] == 0
        blocked = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM events WHERE kind='forecast_execution_blocked'")]
    assert blocked == [{"forecast_id": frozen[0][0], "reason": "recovered_older_cycle", "forecast_preserved": True}]


def test_market_close_interrupts_busy_retry_without_new_attempt(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "close.sqlite3")
    now = [instant("2026-10-08T17:16:02Z")]
    close = instant("2026-10-08T17:16:03Z")
    calls = []
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kwargs: object())
    monkeypatch.setattr(live_session, "eligible_reference", lambda _: True)

    def run(*args, **kwargs):
        calls.append(kwargs)
        raise sqlite_error()

    def pause(seconds):
        assert seconds == 1
        now[0] = close

    runner = SimpleNamespace(store=store, config=SimpleNamespace(publication_offset_seconds=30), run_cycle=run)
    with pytest.raises(InterruptedError):
        live_session.forecasting_loop(runner, histories={}, provider=object(), journal=object(),
            queue=SimpleNamespace(session_id=SESSION, enqueue=lambda _: pytest.fail("No execution")),
            stopped=lambda: False, clock=lambda: now[0], pause=pause, close_at=close)
    assert len(calls) == 1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM forecasts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='forecast_storage_retry_completed'").fetchone()[0] == 0


def test_fatal_forecast_event_failure_still_latches_failed_worker_without_broker(tmp_path, monkeypatch,
                                                                                capsys, offline_databento_sdk):
    from ml.intraday import local_execution, data, account_execution
    from app.services import schwab, databento_market_data
    import dotenv
    monkeypatch.setattr(live_session, "local_symbols", lambda *args: ("Atlas", ("AAPL",)))
    monkeypatch.setattr(local_execution, "load_local_binding",
                        lambda *args: SimpleNamespace(actor="Atlas", symbols=("AAPL",)))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(data, "load_local_history", lambda *args, **kwargs: object())
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider",
                        lambda: SimpleNamespace(dataset="EQUS.MINI"))
    monkeypatch.setattr(schwab, "SchwabSession", lambda: object())
    monkeypatch.setattr(account_execution, "IntradayAccountExecutor", lambda **kwargs: SimpleNamespace(
        step=lambda *args: pytest.fail("No trader orders permitted"),
        recover=lambda: pytest.fail("Failed worker must prevent broker recovery")))

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
        def is_alive(self):
            return False
        def join(self, **kwargs):
            pass

    monkeypatch.setattr(live_session.threading, "Thread", InlineThread)
    monkeypatch.setattr(live_session, "scoring_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(live_session, "collection_loop", lambda *args, **kwargs: None)

    def fail_worker(*args, **kwargs):
        raise sqlite_error(sqlite3.SQLITE_FULL)

    monkeypatch.setattr(live_session, "forecasting_loop", fail_worker)
    original_event = ShadowStore.event

    def event(store, cycle_id, kind, payload, **kwargs):
        if kind == "forecast_worker_stopped":
            raise RuntimeError("private-token=fatal-event-sink-secret")
        return original_event(store, cycle_id, kind, payload, **kwargs)

    monkeypatch.setattr(ShadowStore, "event", event)
    result = live_session.run_manual(repository=tmp_path, datastore=tmp_path,
        profile=tmp_path / "profile.json", watchlist=tmp_path / "watchlist.txt",
        config=live_session.ResearchConfig(), clock=lambda: instant("2026-10-08T17:16:02Z"))
    assert result["status"] == "FAILED" and result["stop_reason"] == "worker_failure"
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["worker_errors"][0]["worker"] == "forecast"
    assert status["worker_errors"][0]["error_type"] == "OperationalError"
    output = capsys.readouterr().out
    assert "private-token" not in output and "fatal-event-sink-secret" not in output
    assert "account.sqlite3" not in output
