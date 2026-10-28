"""Offline queue corruption, supersession and bounded-read regression fixtures."""
from contextlib import contextmanager
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday.prediction import ContractError, TARGET_VERSION, instant
from ml.intraday.store import ShadowStore
from ml.intraday.split_queue import due_forecasts, due_for_pass, broker_due_for_pass, install_indexes

NOW = "2026-10-08T17:32:00+00:00"


def raw_forecast(symbol="AAPL", offset=0):
    value = dict(pc="Atlas", symbol=symbol, cycle_id="cycle-" + str(offset),
        reference_at="2026-10-08T17:00:00+00:00", reference_open="100",
        target_open="2026-10-08T17:30:00+00:00", target_close="2026-10-08T17:45:00+00:00",
        issued_at="2026-10-08T17:16:02+00:00", information_available_at="2026-10-08T17:15:03+00:00",
        data_through="2026-10-08T17:15:00+00:00", target_version=TARGET_VERSION,
        mode="live", availability_basis="observed", probability_not_down=.72,
        configured_count=2, successful_count=2, ensemble_status="full",
        models=[dict(name="a",status="success",probability_not_down=.7),
                dict(name="b",status="success",probability_not_down=.74)])
    for field in ("reference_at", "target_open", "target_close", "issued_at", "information_available_at", "data_through"):
        value[field] = (instant(value[field]) + timedelta(minutes=offset)).isoformat()
    return value


@pytest.fixture
def queue(tmp_path):
    store = ShadowStore(tmp_path / "fixture.sqlite3")
    with store.connect() as db:
        db.execute("CREATE TABLE intraday_live_queue(forecast_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,"
                   "symbol TEXT NOT NULL,issued_at TEXT NOT NULL,next_attempt_at TEXT NOT NULL,status TEXT NOT NULL)")
        install_indexes(db)
    return SimpleNamespace(store=store, session_id="manual-fixture", actor="Atlas", broker_due=lambda now: True)


def enqueue(queue, symbol="AAPL", offset=0):
    value = queue.store.save_forecast(raw_forecast(symbol, offset))
    with queue.store.connect() as db:
        db.execute("INSERT INTO intraday_live_queue VALUES(?,?,?,?,?,'pending')",
                   (value["forecast_id"], queue.session_id, symbol, value["issued_at"], value["issued_at"]))
    return value


@pytest.mark.parametrize("damage", ["json", "hash", "index", "missing", "queue_symbol", "actor"])
def test_bad_original_is_retained_and_does_not_block_unrelated_work(queue, damage):
    bad = enqueue(queue)
    good = enqueue(queue, "AMZN")
    key = bad["forecast_id"]
    with queue.store.connect() as db:
        if damage == "json":
            db.execute("UPDATE forecasts SET payload='{' WHERE id=?", (key,))
        elif damage == "hash":
            db.execute("UPDATE forecasts SET payload=? WHERE id=?", (json.dumps({**bad,"probability_not_down":.99}), key))
        elif damage == "index":
            db.execute("UPDATE forecasts SET issued_at='changed' WHERE id=?", (key,))
        elif damage == "missing":
            db.execute("DELETE FROM forecasts WHERE id=?", (key,))
        elif damage == "queue_symbol":
            db.execute("UPDATE intraday_live_queue SET symbol='GOOG' WHERE forecast_id=?", (key,))
        else:
            db.execute("UPDATE forecasts SET pc='Scout' WHERE id=?", (key,))
        before = db.execute("SELECT * FROM forecasts WHERE id=?", (key,)).fetchall()
    assert list(due_forecasts(queue, NOW)) == [good]
    assert list(due_forecasts(queue, NOW)) == [good]
    with queue.store.connect() as db:
        assert db.execute("SELECT * FROM forecasts WHERE id=?", (key,)).fetchall() == before
        assert db.execute("SELECT status FROM intraday_live_queue WHERE forecast_id=?", (key,)).fetchone()[0] == "invalid_original"
        evidence = db.execute("SELECT payload FROM events WHERE kind='execution_queue_original_rejected'").fetchall()
    assert len(evidence) == 1
    assert json.loads(evidence[0][0])["order_state_changed"] is False


@pytest.mark.parametrize("damage", ["json", "missing"])
def test_invalid_newer_forecast_never_replays_superseded_older_work(queue, damage):
    enqueue(queue)
    newer = enqueue(queue, offset=15)
    with queue.store.connect() as db:
        db.execute("DELETE FROM forecasts WHERE id=?" if damage == "missing" else
                   "UPDATE forecasts SET payload='{' WHERE id=?", (newer["forecast_id"],))
    assert list(due_forecasts(queue, NOW)) == []
    assert list(due_forecasts(queue, NOW)) == []


def test_stop_between_symbols_avoids_reading_next_original(queue, monkeypatch):
    first = enqueue(queue)
    enqueue(queue, "AMZN")
    stop = [False]
    original, reads = queue.store.forecasts, []
    def observed(**kwargs):
        reads.extend(kwargs["forecast_ids"])
        return original(**kwargs)
    monkeypatch.setattr(queue.store, "forecasts", observed)
    work = due_forecasts(queue, NOW, stopped=lambda: stop[0])
    assert next(work) == first
    stop[0] = True
    assert list(work) == [] and reads == [first["forecast_id"]]


def test_current_session_due_and_symbol_queries_use_covering_indexes(queue):
    with queue.store.connect() as db:
        install_indexes(db)  # Idempotent next-process setup.
        due = db.execute("EXPLAIN QUERY PLAN SELECT forecast_id FROM intraday_live_queue "
                         "WHERE session_id=? AND status='pending' AND next_attempt_at<=?", (queue.session_id,NOW)).fetchall()
        symbol = db.execute("EXPLAIN QUERY PLAN SELECT issued_at FROM intraday_live_queue "
                            "WHERE session_id=? AND symbol=?", (queue.session_id,"AAPL")).fetchall()
    assert any("intraday_queue_due" in row[3] and "SEARCH" in row[3] for row in due)
    assert any("intraday_queue_symbol" in row[3] and "SEARCH" in row[3] for row in symbol)


def test_storage_failure_is_not_misclassified_as_bad_science(queue, monkeypatch):
    enqueue(queue)
    def failed(**kwargs):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(queue.store, "forecasts", failed)
    with pytest.raises(sqlite3.OperationalError):
        list(due_forecasts(queue, NOW))
    with queue.store.connect() as db:
        assert db.execute("SELECT status FROM intraday_live_queue").fetchone()[0] == "pending"


def test_renewal_recovery_continues_after_corrupt_queued_original(queue):
    from ml.intraday import live_session
    if not hasattr(live_session, "_renewal_execution_pass"):
        pytest.skip("Atlas local renewal adapter required; portable queue tests still run")
    bad = enqueue(queue)
    good = enqueue(queue, "AMZN")
    with queue.store.connect() as db:
        db.execute("UPDATE forecasts SET payload='{' WHERE id=?", (bad["forecast_id"],))
    calls = []
    queue.iter_due = lambda now, **kwargs: due_forecasts(queue, now, **kwargs)
    class Checkpoint(BaseException): pass
    def checkpoint(*args): raise Checkpoint()
    queue.complete = lambda *args: None
    renewal = SimpleNamespace(session=dict(session_id=queue.session_id, architecture="independent-1a1b-v1",
        started_at=NOW, close_at="2026-10-09T00:00:00+00:00"), state={}, stopped=lambda:False, checkpoint=checkpoint)
    executor = SimpleNamespace(step=lambda value: calls.append(value["forecast_id"]) or dict(
        disposition="no_purchasing_capacity", evidence={}, kind="execution_disposition"),
        recover=lambda: calls.append("recover"))
    with pytest.raises(Checkpoint):
        live_session._renewal_execution_pass(store=queue.store, queue=queue, executor=executor, renewal=renewal, clock=lambda: NOW)
    assert calls == [good["forecast_id"], "recover"]


def contention(code=sqlite3.SQLITE_BUSY):
    error = sqlite3.OperationalError("database contention")
    error.sqlite_errorcode = code
    error.sqlite_errorname = {sqlite3.SQLITE_BUSY:"SQLITE_BUSY", sqlite3.SQLITE_LOCKED:"SQLITE_LOCKED"}[code]
    return error


@pytest.mark.parametrize("code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_native_contention_is_one_bounded_queue_attempt_without_quarantine(queue, monkeypatch, code):
    saved = enqueue(queue)
    original = queue.store.forecasts
    calls=[]
    def busy(**kwargs):
        calls.append(kwargs)
        raise contention(code)
    monkeypatch.setattr(queue.store, "forecasts", busy)
    queue.iter_due=lambda now,**kwargs:due_forecasts(queue,now,**kwargs)
    retries=[]
    assert list(due_for_pass(queue,NOW,stopped=lambda:False,retries=retries))==[]
    assert len(calls)==len(retries)==1 and retries[0]["broker_wait_changed"] is False
    with queue.store.connect() as db:
        assert db.execute("SELECT status FROM intraday_live_queue").fetchone()[0]=="pending"
        assert db.execute("SELECT count(*) FROM events WHERE kind='execution_queue_original_rejected'").fetchone()[0]==0
    monkeypatch.setattr(queue.store, "forecasts", original)
    assert list(due_for_pass(queue,NOW,stopped=lambda:False,retries=[]))==[saved]


def test_non_contention_iteration_failure_surfaces(queue, monkeypatch):
    enqueue(queue)
    queue.iter_due=lambda now,**kwargs:due_forecasts(queue,now,**kwargs)
    def failed(**kwargs):
        raise sqlite3.OperationalError("unclassified disk error")
    monkeypatch.setattr(queue.store,"forecasts",failed)
    retries=[]
    with pytest.raises(sqlite3.OperationalError,match="disk"):
        list(due_for_pass(queue,NOW,stopped=lambda:False,retries=retries))
    assert retries==[]


@pytest.mark.parametrize("stop",[False,True])
def test_queue_storage_contention_keeps_recovery_and_bounded_checkpoint_without_broker_backoff(queue,monkeypatch,stop):
    from ml.intraday import live_session
    if not hasattr(live_session,"_renewal_execution_pass"):
        pytest.skip("Atlas local renewal adapter required; portable queue tests still run")
    enqueue(queue)
    halted=[False]
    def busy(**kwargs):
        halted[0]=stop
        raise contention()
    monkeypatch.setattr(queue.store,"forecasts",busy)
    queue.iter_due=lambda now,**kwargs:due_forecasts(queue,now,**kwargs)
    queue.broker_wait=lambda **kwargs:pytest.fail("Storage contention altered broker backoff")
    calls,saved=[],[]
    class Checkpoint(BaseException):pass
    def checkpoint(state,due):
        saved.append((state,due))
        raise Checkpoint
    renewal=SimpleNamespace(session=dict(session_id=queue.session_id,architecture="independent-1a1b-v1",
        started_at=NOW,close_at="2026-10-09T00:00:00+00:00"),state={},stopped=lambda:halted[0],checkpoint=checkpoint)
    executor=SimpleNamespace(step=lambda _:pytest.fail("Unread forecast executed"),recover=lambda:calls.append("recover"))
    if stop:
        result=live_session._renewal_execution_pass(store=queue.store,queue=queue,executor=executor,renewal=renewal,clock=lambda:NOW)
        assert result["status"]=="STOPPED" and calls==saved==[]
    else:
        with pytest.raises(Checkpoint):
            live_session._renewal_execution_pass(store=queue.store,queue=queue,executor=executor,renewal=renewal,clock=lambda:NOW)
        assert calls==["recover"]
        assert saved[-1][0]["queue_storage_retries"][0]["operation"]=="execution_queue_read"
        assert saved[-1][1]==(instant(NOW)+timedelta(seconds=3)).isoformat()


def test_unreadable_broker_retry_state_never_authorizes_recovery(queue):
    def busy(now):raise contention()
    queue.broker_due=busy
    retries=[]
    assert broker_due_for_pass(queue,NOW,retries=retries) is False
    assert retries[0]["operation"]=="broker_retry_read"
