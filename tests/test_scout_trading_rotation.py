"""Offline native-store and injected-worker trading rotation regressions."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import socket
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from ml.intraday import live_model_recipe, live_session, scout_services, trading_rotation as rotation
from ml.intraday.prediction import ContractError, instant
from ml.intraday.scout_session_books import ScoutBooks
from ml.intraday.service_handoff import ServiceStore
from tests.test_intraday_live_session import scout_manual_fixture
from tests.test_intraday_store import forecast
from tests.test_intraday_scout_journal import request as order_request, grant as order_grant


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*a, **kw):
        pytest.fail("Network is forbidden in trading rotation fixtures")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def lifecycle():
    value = SimpleNamespace(role="trading", session_id="manual-scout-15m:rotation-fixture",
        generation=1, token="old-token", started_at="2026-10-08T17:16:00Z",
        wake_at="2026-10-08T11:00:00Z", deadline="2026-10-09T00:00:00Z",
        drain_timeout_seconds=1, previous_checkpoint=None,
        request={"trading_rotation_version":rotation.VERSION},
        release={"manifest_sha256":"a"*64, "recipe_file_sha256":"b"*64},
        stop_reason=lambda:None, ready=lambda:None, phase=lambda *a,**kw:None)
    value.header = lambda: {"role":"trading", "session_id":value.session_id,
        "generation":value.generation, "token":value.token, "release":value.release}
    return value


def request(value, *, sequence=1):
    return {**value.header(), "schema":rotation.VERSION, "iteration_sequence":sequence,
        "boundary_id":f"model-{sequence}:cycle-{sequence}", "boundary_cycle_id":f"cycle-{sequence}",
        "model_generation":sequence, "checkpoint_sha256":"c"*64}


def prepare(tmp_path):
    life = lifecycle()
    store = ServiceStore(tmp_path/"intraday/shadow.sqlite3", object_root=tmp_path/"intraday/services")
    queue = scout_services.ServiceExecutionQueue(store, life.session_id, pc="Scout")
    books = ScoutBooks(tmp_path/"intraday/scout", account_fingerprint="a"*64)
    journal = books.create_session(life.session_id)
    req = replace(order_request(), session_id=life.session_id)
    journal.prepare(req); journal.grant(order_grant(req))
    token = journal.claim_send(req.request_id)
    journal.outcome(req.request_id, token, None)
    journal.close()
    store.begin_cycle("first", {"pc":"Scout", "experiment":life.session_id, "started_at":life.started_at})
    f = store.save_forecast(forecast(pc="Scout", symbol="DOCU", mode="live", cycle_id="first"))
    queue.enqueue(f)
    queue.broker_wait(now="2026-10-08T17:17:00Z", seconds=60)
    cp = rotation.capture_checkpoint(store, lifecycle=life, symbols=["DOCU"],
        rotation_request=request(life), datastore=tmp_path)
    life.previous_checkpoint=cp; life.generation=2; life.token="new-token"
    life.release={**life.release, "manifest_sha256":"d"*64}
    return life, store, queue, books, f


def test_resume_allows_new_model_work_and_monotonic_retirement_keeps_unknown(tmp_path):
    life, store, queue, books, original = prepare(tmp_path)
    store.begin_cycle("second", {"pc":"Scout", "experiment":life.session_id, "started_at":life.started_at})
    newer = store.save_forecast(forecast(pc="Scout", symbol="DOCU", mode="live", cycle_id="second",
        issued_at="2026-10-08T17:17:03Z"))
    queue.enqueue(newer)  # Retires original as superseded while old trader is drained.
    queue.broker_wait(now="2026-10-08T17:17:00Z", seconds=120)
    rotation.validate_resume(store, lifecycle=life, symbols=["DOCU"], datastore=tmp_path)
    with books.open_session(life.session_id) as journal:
        assert journal.get("request-1")["state"]=="UNKNOWN"
        with pytest.raises(Exception, match="once|UNKNOWN|state"):
            journal.claim_send("request-1")
    assert store.forecast("first","DOCU")==original
    assert not queue.broker_due("2026-10-08T17:18:30Z")


@pytest.mark.parametrize("mutation", ["missing_queue", "identity", "forecast", "retry_floor", "broker_floor", "revive", "journal", "generation", "recipe", "deadline", "request"])
def test_resume_rejects_lost_or_changed_identity_before_effects(tmp_path, mutation):
    life, store, queue, books, original = prepare(tmp_path)
    cp=life.previous_checkpoint
    with store.connect() as db:
        if mutation=="missing_queue":db.execute("DELETE FROM intraday_live_queue")
        if mutation=="identity":db.execute("UPDATE intraday_live_queue SET symbol='OTHER'")
        if mutation=="forecast":
            db.execute("UPDATE forecasts SET issued_at='2026-10-08T17:16:03Z'")
        if mutation=="retry_floor":db.execute("UPDATE intraday_live_queue SET next_attempt_at='2026-10-08T17:00:00Z'")
        if mutation=="broker_floor":db.execute("DELETE FROM intraday_live_retry")
        if mutation=="revive":
            cp["queue_witnesses"][0]["status"]="expired"
    if mutation=="journal":
        with books.open_session(life.session_id) as journal:
            journal.prepare(replace(order_request("new-order"), session_id=life.session_id))
    if mutation=="generation":cp["generation"]=99
    if mutation=="recipe":life.release["recipe_file_sha256"]="e"*64
    if mutation=="deadline":life.deadline="2026-10-09T01:00:00Z"
    if mutation=="request":cp["rotation_request"]["iteration_sequence"]=2
    with pytest.raises(ContractError):
        rotation.validate_resume(store, lifecycle=life, symbols=["DOCU"], datastore=tmp_path)


def test_initial_generation_cannot_adopt_previous_checkpoint(tmp_path):
    life=lifecycle();life.previous_checkpoint={}
    with pytest.raises(ContractError, match="Initial"):
        rotation.validate_resume(None,lifecycle=life,symbols=["DOCU"],datastore=tmp_path)


@pytest.mark.parametrize("field,value", [("token","other"),("generation",9),("session_id","other"),("iteration_sequence",0),("model_generation",0),("boundary_id","wrong"),("checkpoint_sha256","x"*64)])
def test_rotation_request_is_fenced(field,value):
    life=lifecycle();req=request(life);req[field]=value
    with pytest.raises(ContractError):rotation.validate_request(req,lifecycle=life)


def test_drained_capture_retries_only_native_busy_and_stop_wins(tmp_path,monkeypatch):
    life=lifecycle();attempts=[];ticks=[0.0];stopped=[None]
    life.stop_reason=lambda:stopped[0]
    def capture(*a,**kw):
        attempts.append(1)
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(rotation,"capture_checkpoint",capture)
    def pause(seconds):ticks[0]+=seconds;stopped[0]="manual_stop"
    with pytest.raises(InterruptedError):
        rotation.capture_with_retry(None,lifecycle=life,symbols=["DOCU"],rotation_request=request(life),
            datastore=tmp_path,sleep=pause,monotonic=lambda:ticks[0])
    assert len(attempts)==1 and ticks[0]<=.2
    stopped[0]=None
    def wrong(*a,**kw):raise sqlite3.OperationalError("no such table: scientific evidence")
    monkeypatch.setattr(rotation,"capture_checkpoint",wrong)
    with pytest.raises(sqlite3.OperationalError,match="no such table"):
        rotation.capture_with_retry(None,lifecycle=life,symbols=["DOCU"],rotation_request=request(life),
            datastore=tmp_path,sleep=lambda _:pytest.fail("Non-busy error retried"))


def run_case(tmp_path, monkeypatch, *, recovery_busy=False, stop_in_drain=False):
    kwargs,calls,executors=scout_manual_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(live_model_recipe,"prepare_manual_models",lambda config,**kw:(config,{}))
    life=lifecycle();now=[instant("2026-10-08T17:17:00Z")];checkpoints=[];phases=[];stop=[None]
    life.stop_reason=lambda:stop[0]
    life.trading_rotation_request=lambda:request(life)
    life.phase=lambda name,**kw:phases.append(name)
    life.checkpoint=lambda cp:checkpoints.append(cp)
    base=sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor
    books=ScoutBooks(tmp_path/"intraday/scout",account_fingerprint="a"*64)
    class Executor(base):
        def preflight(self):
            self.journal=books.open_session(life.session_id) if self.kwargs.get("resume_session") else books.create_session(life.session_id)
            return super().preflight()
        def recover(self):
            calls.append("recover")
            if recovery_busy:raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(sys.modules["ml.intraday.scout_local_execution"],"ScoutLocalAccountExecutor",Executor)
    if stop_in_drain:
        def phase(name,**kw):
            phases.append(name)
            if name=="draining":stop[0]="manual_stop"
        life.phase=phase
    def sleep(seconds):
        now[0]+=timedelta(seconds=seconds)
        assert now[0]<instant("2026-10-08T17:17:10Z"),"Trading did not complete bounded rotation"
    kwargs.update(lifecycle=life,independent_services=True,clock=lambda:now[0],sleep=sleep)
    return kwargs,calls,executors,checkpoints,phases


@pytest.mark.parametrize("recovery_busy",[False,True])
def test_real_trading_loop_drains_and_resumes_same_journal(tmp_path,monkeypatch,recovery_busy):
    kwargs,calls,executors,checkpoints,phases=run_case(tmp_path,monkeypatch,recovery_busy=recovery_busy)
    result=live_session.run_manual(**kwargs)
    assert result["status"]=="PLANNED_RESTART" and len(checkpoints)==1
    assert phases==["iteration_execution","draining"]
    cp=checkpoints[0];assert cp["owned_workers_drained"] and cp["pending_order_identity_preserved"]
    assert cp["broker_retry_floor"] is not None if recovery_busy else cp["broker_retry_floor"] is None
    life=kwargs["lifecycle"];life.previous_checkpoint=cp;life.generation=2;life.token="next-token"
    life.release={**life.release,"manifest_sha256":"f"*64}
    result=live_session.run_manual(**kwargs)
    assert result["status"]=="PLANNED_RESTART" and executors[-1].kwargs["resume_session"] is True
    assert checkpoints[-1]["journal_witness"]==cp["journal_witness"]
    assert not any(c in calls for c in ("provider","history","forecast","scoring","step"))


def test_manual_stop_during_drain_never_publishes_planned_handoff(tmp_path,monkeypatch):
    kwargs,calls,executors,checkpoints,phases=run_case(tmp_path,monkeypatch,stop_in_drain=True)
    result=live_session.run_manual(**kwargs)
    assert result["status"]=="STOPPED" and result["stop_reason"]=="manual_stop" and not checkpoints


def test_invalid_resume_rejected_before_broker_constructor(tmp_path,monkeypatch):
    kwargs,calls,executors,checkpoints,phases=run_case(tmp_path,monkeypatch)
    life=kwargs["lifecycle"];life.generation=2;life.previous_checkpoint={"schema":"wrong"}
    from app.services import schwab
    monkeypatch.setattr(schwab,"SchwabSession",lambda:pytest.fail("Broker constructed before checkpoint validation"))
    with pytest.raises(ContractError,match="checkpoint"):
        live_session.run_manual(**kwargs)
    assert not executors
