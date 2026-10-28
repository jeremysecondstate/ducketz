"""Scout-only contention regressions; every operating boundary is offline."""
from datetime import timedelta
import json
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from ml.intraday import live_model_recipe, live_session, scout_services
from ml.intraday.prediction import instant
from ml.intraday.service_handoff import ServiceStore
from tests.test_intraday_live_session import scout_manual_fixture


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Network in offline busy fixture")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)


def test_native_busy_write_preserves_absolute_provider_floor_until_durable(tmp_path):
    store = ServiceStore(tmp_path / "science.sqlite3", object_root=tmp_path / "objects")
    queue = scout_services.ServiceExecutionQueue(store, "manual-scout-15m:fixture", pc="Scout")
    now = instant("2026-10-28T17:00:00Z")
    locker = sqlite3.connect(store.path, timeout=0)
    try:
        locker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as failure:
            queue.broker_wait(now=now, seconds=120)
        assert failure.value.sqlite_errorcode & 0xff == sqlite3.SQLITE_BUSY
        assert queue._pending_broker_wait == (now + timedelta(seconds=120)).isoformat()
        with pytest.raises(sqlite3.OperationalError):
            queue.broker_due(now + timedelta(seconds=30))
        # Even after the local clock floor passes, an unpersisted wait must not
        # permit an effect or a planned handoff while the database stays busy.
        with pytest.raises(sqlite3.OperationalError):
            queue.broker_due(now + timedelta(seconds=150))
        # Read-only recovery does not shorten or extend the original floor.
        assert queue._pending_broker_wait == (now + timedelta(seconds=120)).isoformat()
    finally:
        locker.rollback()
        locker.close()
    assert queue.broker_due(now + timedelta(seconds=30)) is False
    assert queue._pending_broker_wait is None
    with store.connect() as db:
        assert db.execute("SELECT next_attempt_at FROM intraday_live_retry").fetchone() == (
            (now + timedelta(seconds=120)).isoformat(),)
    reopened = scout_services.ServiceExecutionQueue(store, queue.session_id, pc="Scout")
    assert reopened.broker_due(now + timedelta(seconds=119)) is False
    assert reopened.broker_due(now + timedelta(seconds=120)) is True


def test_defer_retains_floor_before_a_busy_forecast_write(tmp_path):
    store = ServiceStore(tmp_path / "science.sqlite3", object_root=tmp_path / "objects")
    queue = scout_services.ServiceExecutionQueue(store, "manual-scout-15m:fixture", pc="Scout")
    now = instant("2026-10-28T17:00:00Z")
    locker = sqlite3.connect(store.path, timeout=0)
    try:
        locker.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError):
            queue.defer("preserved-original", now=now, seconds=90, broker_wait=True)
        assert queue._pending_broker_wait == (now + timedelta(seconds=90)).isoformat()
    finally:
        locker.rollback()
        locker.close()
    assert queue.broker_due(now + timedelta(seconds=1)) is False
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM intraday_live_queue").fetchone() == (0,)


def independent_case(tmp_path, monkeypatch, *, stop_reason="manual_stop"):
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(live_model_recipe, "prepare_manual_models", lambda config, **kw: (config, {}))
    now, stopped, sleeps = [instant("2026-10-28T17:17:00Z")], [None], []
    lifecycle = SimpleNamespace(role="trading", session_id="manual-scout-15m:busy-fixture",
        generation=1, drain_timeout_seconds=1, started_at=now[0], wake_at=now[0], deadline=instant("2026-10-29T00:00:00Z"),
        release={"manifest_sha256":"fixture-only"}, stop_reason=lambda:stopped[0], ready=lambda:None)
    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += timedelta(seconds=seconds)
        if now[0] >= instant("2026-10-28T17:17:05Z"):
            stopped[0] = stop_reason
    kwargs.update(lifecycle=lifecycle, independent_services=True, clock=lambda:now[0], sleep=sleep)
    return kwargs, calls, executors, now, stopped, sleeps


@pytest.mark.parametrize("stop_reason", ["manual_stop", "session_end"])
def test_full_trading_loop_retains_run_after_busy_recovery_and_handler_write(tmp_path, monkeypatch, stop_reason):
    kwargs, calls, executors, now, stopped, sleeps = independent_case(tmp_path, monkeypatch,
        stop_reason=stop_reason)
    recoveries, persist_attempts = [], []
    original_persist = scout_services.ServiceExecutionQueue._persist_broker_wait
    def persist(self):
        if self._pending_broker_wait is not None:
            persist_attempts.append((now[0], self._pending_broker_wait))
            if len(persist_attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
        return original_persist(self)
    monkeypatch.setattr(scout_services.ServiceExecutionQueue, "_persist_broker_wait", persist)
    # Executor constructor remains the existing fully injected fixture boundary.
    import sys
    base = sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor
    class Executor(base):
        def recover(self):
            recoveries.append(now[0])
            if len(recoveries) == 1:
                raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(sys.modules["ml.intraday.scout_local_execution"], "ScoutLocalAccountExecutor", Executor)
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED" and result["session_id"] == kwargs["lifecycle"].session_id
    assert result["stop_reason"] == stop_reason
    assert len(recoveries) == 2 and recoveries[1] >= recoveries[0] + timedelta(seconds=3)
    assert len(persist_attempts) == 2 and persist_attempts[0][1] == persist_attempts[1][1]
    assert "step" not in calls and "forecast" not in calls and "provider" not in calls
    status = json.loads((tmp_path / "intraday/manual-session.json").read_bytes())
    assert status["worker_errors"] == [] and status["generation"] == 1
    assert status["automatic_stop_at"] == "2026-10-29T00:00:00+00:00"
    events = [json.loads(line) for line in (tmp_path / "intraday/sessions/manual-scout-15m-busy-fixture/events.jsonl").read_text().splitlines()]
    assert sum(row["event"] == "execution_store_busy" for row in events) == 1
    assert sleeps and all(s <= 1 for s in sleeps)


def test_nonbusy_handler_failure_stays_fatal_and_shutdown_busy_cannot_mask_it(tmp_path, monkeypatch):
    kwargs, calls, executors, now, stopped, sleeps = independent_case(tmp_path, monkeypatch)
    import sys
    base = sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor
    class Executor(base):
        def recover(self):
            raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(sys.modules["ml.intraday.scout_local_execution"], "ScoutLocalAccountExecutor", Executor)
    monkeypatch.setattr(scout_services.ServiceExecutionQueue, "broker_wait",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("no such table: original")))
    original_event = ServiceStore.event
    def event(self, cycle, kind, payload, **kw):
        if kind == "manual_session_stopped":
            raise sqlite3.OperationalError("database is locked")
        return original_event(self, cycle, kind, payload, **kw)
    monkeypatch.setattr(ServiceStore, "event", event)
    with pytest.raises(sqlite3.OperationalError, match="no such table: original"):
        live_session.run_manual(**kwargs)
    status = json.loads((tmp_path / "intraday/manual-session.json").read_bytes())
    assert status["status"] == "FAILED" and status["worker_errors"][0]["message"] == "no such table: original"
    events = [json.loads(line) for line in (tmp_path / "intraday/sessions/manual-scout-15m-busy-fixture/events.jsonl").read_text().splitlines()]
    fallback = [row for row in events if row["event"] == "manual_session_stopped_store_busy"]
    assert len(fallback) == 1 and fallback[0]["worker_errors"] == status["worker_errors"]
    assert "step" not in calls and not sleeps


@pytest.mark.parametrize("code,expected", [(5, True), (261, True), (6, True), (262, True), (1, False), (11, False)])
def test_busy_classifier_uses_native_primary_code(code, expected):
    exc = sqlite3.OperationalError("message deliberately irrelevant")
    exc.sqlite_errorcode = code
    assert live_session._execution_store_busy(exc) is expected


def test_busy_words_in_non_sqlite_error_do_not_convert_failure():
    assert live_session._execution_store_busy(RuntimeError("database is locked")) is False
    assert live_session._execution_store_busy(sqlite3.OperationalError("missing busy table")) is False
