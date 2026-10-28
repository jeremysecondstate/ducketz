"""Regression: exact original model failure evidence survives SQLite busy."""
from datetime import timedelta
import json
import socket
import sqlite3
from types import SimpleNamespace
import pytest
from ml.intraday import runtime
from ml.intraday.data import AcquisitionPending
from ml.intraday.prediction import instant
from ml.intraday.runtime import ResearchConfig, ShadowRunner

from ml.intraday import scout_services as candidate

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Model persistence fixture reached network")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "sendto", forbidden)

def make_store(tmp_path, *, pause, stop_reason=lambda:None):
    store=candidate._ModelServiceStore(tmp_path/"science.sqlite3",object_root=tmp_path/"objects",
        persistence_stop_reason=stop_reason,persistence_pause=pause)
    store.run_binding={"logical_run_id":"manual-scout-15m:fixture"}
    store.begin_cycle("cycle",{"pc":"Scout","status":"running","symbols":["DOCU"],"started_at":"2026-10-28T17:15:35+00:00"})
    return store

def locked(store):
    locker=sqlite3.connect(store.path,timeout=0,check_same_thread=False)
    locker.execute("BEGIN IMMEDIATE")
    return locker

def payload():
    return {"type":"AcquisitionPending","reason":"system_1a_handoff_busy",
        "recorded_at":"2026-10-28T17:15:35+00:00","retryable":True,
        "retry_at":"2026-10-28T17:15:36+00:00","diagnostics":{"original":"immutable"}}

def test_native_failure_event_retries_exact_original_payload_and_clock(tmp_path,monkeypatch):
    waits=[]; box=[]
    original=payload()
    def pause(seconds):
        waits.append(seconds)
        original["diagnostics"]["original"]="caller-mutation-must-not-change-write"
        box[0].rollback()
    store=make_store(tmp_path,pause=pause)
    box.append(locked(store))
    stamp=instant("2026-10-28T17:15:37Z")
    monkeypatch.setattr(candidate,"utcnow",lambda:stamp)
    try:
        store.event("cycle","symbol_failure",original,symbol="DOCU")
    finally:
        box[0].rollback();box[0].close()
    with store.connect() as db:
        rows=db.execute("SELECT symbol,recorded_at,payload FROM events WHERE kind='symbol_failure'").fetchall()
    assert waits==[.2]
    assert rows==[("DOCU",stamp.isoformat(),candidate.encoded(payload()))]
    assert not (tmp_path/"objects/model-metrics.jsonl").exists()

@pytest.mark.parametrize("reason",["manual_stop","session_end"])
def test_busy_stop_preserves_exact_pending_evidence_and_cause_without_retry(tmp_path,reason):
    waits=[]
    store=make_store(tmp_path,pause=waits.append,stop_reason=lambda:reason)
    locker=locked(store)
    original=payload()
    try:
        with pytest.raises(candidate._ModelFailurePersistenceStopped) as caught:
            store.event("cycle","symbol_failure",original,symbol="DOCU",recorded_at=original["recorded_at"])
        assert caught.value.reason==reason
        assert isinstance(caught.value.__cause__,sqlite3.OperationalError)
        assert caught.value.__cause__.sqlite_errorcode & 0xff==sqlite3.SQLITE_BUSY
    finally:
        locker.rollback();locker.close()
    assert waits==[]
    rows=[json.loads(line) for line in (tmp_path/"objects/model-metrics.jsonl").read_text().splitlines()]
    assert len(rows)==1 and rows[0]["logical_run_id"]=="manual-scout-15m:fixture"
    assert rows[0]["cycle_id"]=="cycle" and rows[0]["symbol"]=="DOCU"
    assert rows[0]["phase"]=="failure_persistence_stopped" and rows[0]["stop_reason"]==reason
    assert rows[0]["pending_write"]["payload"]==original
    assert rows[0]["pending_write"]["recorded_at"]==original["recorded_at"]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='symbol_failure'").fetchone()==(0,)

def test_stop_after_pause_prevents_retry_but_first_write_is_allowed(tmp_path):
    reasons=[None]; waits=[];box=[]
    def pause(seconds):
        waits.append(seconds);box[0].rollback();reasons[0]="session_end"
    store=make_store(tmp_path,pause=pause,stop_reason=lambda:reasons[0])
    box.append(locked(store))
    try:
        with pytest.raises(candidate._ModelFailurePersistenceStopped):
            store.event("cycle","symbol_failure",payload(),symbol="DOCU")
    finally:
        box[0].rollback();box[0].close()
    assert waits==[.2]
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='symbol_failure'").fetchone()==(0,)
    # Ordinary final evidence may still be committed on the first attempt.
    store.event("cycle","symbol_failure",payload(),symbol="DOCU")
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='symbol_failure'").fetchone()==(1,)

def test_nonbusy_database_failure_propagates_without_sleep_or_conversion(tmp_path):
    waits=[]
    store=make_store(tmp_path,pause=waits.append)
    with store.connect() as db:
        db.execute("DROP TABLE events")
    with pytest.raises(sqlite3.OperationalError,match="no such table: events"):
        store.event("cycle","symbol_failure",payload(),symbol="DOCU")
    assert waits==[] and not (tmp_path/"objects/model-metrics.jsonl").exists()

def test_update_progress_busy_does_not_duplicate_original_symbol_event(tmp_path,monkeypatch):
    waits=[];box=[]
    def pause(seconds):
        waits.append(seconds);box[0].rollback()
    store=make_store(tmp_path,pause=pause)
    store.event("cycle","symbol_failure",payload(),symbol="DOCU")
    original_event=store.event
    def event(cycle_id,kind,value,**kwargs):
        if kind=="cycle_progress" and not box:
            box.append(locked(store))  # Parent update already committed its changes.
        return original_event(cycle_id,kind,value,**kwargs)
    monkeypatch.setattr(store,"event",event)
    changes={"failures":{"DOCU":payload()},"pending_recovery_symbols":["DOCU"]}
    try:
        value=store.update_cycle("cycle",changes)
    finally:
        for locker in box:locker.rollback();locker.close()
    assert waits==[.2] and value["failures"]==changes["failures"]
    with store.connect() as db:
        kinds=dict(db.execute("SELECT kind,COUNT(*) FROM events GROUP BY kind"))
        saved=json.loads(db.execute("SELECT payload FROM cycles WHERE id='cycle'").fetchone()[0])
    assert kinds["symbol_failure"]==1 and kinds["cycle_progress"]==1
    assert saved["failures"]==changes["failures"]

def test_actual_runner_keeps_original_failure_and_never_replays_symbol(tmp_path,monkeypatch):
    waits=[];box=[];calls=[]
    def pause(seconds):
        waits.append(seconds);box[0].rollback()
    store=make_store(tmp_path,pause=pause)
    now=instant("2026-10-28T17:15:35Z")
    runner=ShadowRunner(store=store,pc="Scout",symbols=("DOCU",),
        config=ResearchConfig(models=("fixture",),symbol_workers=1),clock=lambda:now)
    def symbol(*args,**kwargs):
        calls.append("DOCU")
        box.append(locked(store))
        try:
            store.attach("unused","DOCU",{"snapshot_hash":"fixture"})
        except sqlite3.OperationalError as exc:
            raise AcquisitionPending("system_1a_handoff_busy",retry_after_seconds=1) from exc
    monkeypatch.setattr(runner,"_symbol",symbol)
    try:
        result=runner.run_cycle(object(),reference_at=instant("2026-10-28T17:00:00Z"),
                                mode="live",experiment="manual-scout-15m:fixture")
    finally:
        for locker in box:locker.rollback();locker.close()
    assert waits==[.2] and calls==["DOCU"]
    assert result["status"]=="partial_failure" and result["forecasts"]=={}
    assert result["failures"]["DOCU"]["recorded_at"]==now.isoformat()
    assert result["failures"]["DOCU"]["retry_at"]==(now+timedelta(seconds=1)).isoformat()
    assert result["failures"]["DOCU"]["reason"]=="system_1a_handoff_busy"
    with store.connect() as db:
        rows=db.execute("SELECT payload FROM events WHERE kind='symbol_failure'").fetchall()
        assert db.execute("SELECT COUNT(*) FROM forecasts").fetchone()==(0,)
    assert len(rows)==1 and json.loads(rows[0][0])==result["failures"]["DOCU"]

def test_service_stop_during_failure_persistence_returns_no_checkpoint(tmp_path,monkeypatch):
    now=instant("2026-10-28T17:15:35Z");stop=[None];checkpoints=[];box=[]
    monkeypatch.setattr(candidate,"_configuration",lambda *a,**k:
        ("Scout",("DOCU",),ResearchConfig(models=("fixture",)),{},"fixture",None))
    class Runner:
        def __init__(self,store,**kwargs):self.store=store
        def run_cycle(self,*a,**kwargs):
            self.store.begin_cycle("cycle",{"pc":"Scout","status":"running","symbols":["DOCU"],"started_at":"2026-10-28T17:15:35+00:00"})
            box.append(locked(self.store))
            self.store.event("cycle","symbol_failure",payload(),symbol="DOCU")
            raise AssertionError("Must not return a cycle result")
    monkeypatch.setattr(runtime,"ShadowRunner",Runner)
    lifecycle=SimpleNamespace(previous_checkpoint=None,session_id="manual-scout-15m:fixture",
        started_at=now,wake_at=now,deadline=now+timedelta(hours=5),
        stop_reason=lambda:stop[0],ready=lambda:None,checkpoint=checkpoints.append)
    def pause(seconds):
        assert seconds==.2;stop[0]="manual_stop"
    try:
        result=candidate.run_model_service(repository=tmp_path,datastore=tmp_path,
            release={},lifecycle=lifecycle,clock=lambda:now,sleep=pause)
    finally:
        for locker in box:locker.rollback();locker.close()
    assert result=={"status":"STOPPED","stop_reason":"manual_stop","failure_persistence_pending":True}
    assert checkpoints==[]
    metrics=[json.loads(line) for line in (tmp_path/"intraday/services/model-metrics.jsonl").read_text().splitlines()]
    assert len(metrics)==1 and metrics[0]["pending_write"]["payload"]==payload()
    assert metrics[0]["logical_run_id"]==lifecycle.session_id
