from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from ml.intraday import live_session
from ml.intraday.prediction import ContractError, instant, STEP
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast


@pytest.mark.parametrize("now,expected", [
    ("2026-10-12T17:15:29Z","2026-10-12T16:45:00Z"),
    ("2026-10-12T17:15:30Z","2026-10-12T17:00:00Z"),
    ("2026-10-12T17:30:30Z","2026-10-12T17:15:00Z"),
    ("2026-10-12T17:46:00Z","2026-10-12T17:30:00Z")])
def test_start_latest_completed_on_quarter_grid(now, expected):
    assert live_session.latest_completed_reference(now, publication_offset_seconds=30) == instant(expected)


def test_live_queue_retains_retry_wait_across_restart_and_never_consumes_old_session(tmp_path):
    store = ShadowStore(tmp_path/"forecast.sqlite3")
    f = store.save_forecast(forecast(mode="live"))
    queue = live_session.ExecutionQueue(store,"manual-one")
    queue.enqueue(f)
    queue.enqueue(f)
    assert queue.due("2026-10-08T17:17:00Z") == [f]
    queue.defer(f["forecast_id"],now="2026-10-08T17:17:00Z",seconds=120,broker_wait=True)
    restored = live_session.ExecutionQueue(store,"manual-one")
    assert restored.due("2026-10-08T17:18:59Z") == []
    assert not restored.broker_due("2026-10-08T17:18:59Z")
    assert restored.due("2026-10-08T17:19:00Z") == [f]
    fresh = live_session.ExecutionQueue(store,"manual-two")
    assert fresh.due("2026-10-08T17:20:00Z") == []
    with pytest.raises(ContractError,match="another manual session"):
        fresh.enqueue(f)


def test_scout_queue_reuses_engine_but_rejects_foreign_participant(tmp_path):
    store = ShadowStore(tmp_path / "forecast.sqlite3")
    scout = store.save_forecast(forecast(mode="live", pc="Scout", symbol="DOCU"))
    atlas = store.save_forecast(forecast(mode="live", pc="Atlas", symbol="AAPL"))
    queue = live_session.ExecutionQueue(store, "scout-manual", pc="Scout")
    queue.enqueue(scout)
    with pytest.raises(ContractError, match="another participant"):
        queue.enqueue(atlas)
    assert queue.due("2026-10-08T17:17:00Z") == [scout]
    queue.defer(scout["forecast_id"], now="2026-10-08T17:17:00Z", seconds=30)
    restored = live_session.ExecutionQueue(store, "scout-manual", pc="Scout")
    assert restored.due("2026-10-08T17:17:29Z") == []
    assert restored.due("2026-10-08T17:17:30Z") == [scout]
    foreign = live_session.ExecutionQueue(store, "scout-manual", pc="Atlas")
    assert foreign.due("2026-10-08T17:18:00Z") == []
    foreign.defer(scout["forecast_id"], now="2026-10-08T17:18:00Z", seconds=900, broker_wait=True)
    foreign.complete(scout["forecast_id"], "foreign-completed")
    assert restored.due("2026-10-08T17:18:00Z") == [scout]
    with pytest.raises(ContractError):
        live_session.ExecutionQueue(store, "manual", pc="unbound")


def test_newest_symbol_forecast_supersedes_queued_intention(tmp_path):
    store = ShadowStore(tmp_path/"forecast.sqlite3")
    first = store.save_forecast(forecast(mode="live"))
    next_value = forecast(mode="live",cycle_id="next",issued_at="2026-10-08T17:31:00Z",
        reference_at="2026-10-08T17:15:00Z",target_open="2026-10-08T17:45:00Z",
        target_close="2026-10-08T18:00:00Z",data_through="2026-10-08T17:30:00Z")
    second = store.save_forecast(next_value)
    queue = live_session.ExecutionQueue(store,"manual")
    queue.enqueue(first); queue.enqueue(second)
    assert queue.due("2026-10-08T17:32:00Z") == [second]
    queue.complete(second["forecast_id"],"no_purchasing_capacity")
    assert queue.due("2026-10-08T17:33:00Z") == []
    assert len(store.forecasts()) == 2


def test_default_cli_never_starts_worker(monkeypatch, capsys):
    from ml.intraday import readiness
    monkeypatch.setattr(readiness,"inspect_readiness",lambda **kw:{"manual_live_start_ready":False})
    monkeypatch.setattr(live_session,"run_manual",lambda **kw:pytest.fail("manual worker called"))
    assert live_session.main([]) == 2
    assert json.loads(capsys.readouterr().out)["manual_live_start_ready"] is False


def test_later_issue_of_older_reference_never_supersedes_newer_reference(tmp_path):
    store = ShadowStore(tmp_path/"forecast.sqlite3")
    newer = store.save_forecast(forecast(mode="live",cycle_id="newer",issued_at="2026-10-08T17:31:00Z",
        reference_at="2026-10-08T17:15:00Z",target_open="2026-10-08T17:45:00Z",
        target_close="2026-10-08T18:00:00Z",data_through="2026-10-08T17:30:00Z"))
    older = store.save_forecast(forecast(mode="live",issued_at="2026-10-08T17:32:00Z"))
    queue = live_session.ExecutionQueue(store,"manual")
    queue.enqueue(newer); queue.enqueue(older)
    assert queue.due("2026-10-08T17:33:00Z") == [newer]


def test_forecasting_start_and_overrun_do_not_trade_a_backlog(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path/"forecast.sqlite3")
    times = [instant("2026-10-12T17:16:00Z"),instant("2026-10-12T18:01:00Z")]
    calls = []
    queue = SimpleNamespace(session_id="manual",enqueue=lambda f:None)
    def run(histories, **kwargs):
        calls.append(kwargs)
    runner = SimpleNamespace(store=store,config=SimpleNamespace(publication_offset_seconds=30),
                             run_cycle=run,score_available=lambda *a,**k:None)
    monkeypatch.setattr(live_session,"LiveHistories",lambda **kw:object())
    monkeypatch.setattr(live_session,"eligible_reference",lambda ref:True)
    live_session.forecasting_loop(runner,histories={},provider=object(),journal=object(),queue=queue,
        stopped=lambda:len(calls)==2,clock=lambda:times[min(len(calls),1)],pause=lambda n:None)
    assert [c["reference_at"] for c in calls] == [instant("2026-10-12T17:00:00Z"),instant("2026-10-12T17:45:00Z")]
    assert all(c["mode"] == "live" and c["on_published"] == queue.enqueue for c in calls)
    assert all(c["allow_execution"] and c["retry_symbols"] is None for c in calls)
    with store.connect() as db:
        missed = json.loads(db.execute("SELECT payload FROM events WHERE kind='missed_cycle_boundaries'").fetchone()[0])
    assert missed["missed_count"] == 2


def saved_pending_cycle(store,session,reference,*,retryable=True):
    reference=instant(reference)
    identity=f"{session}:{reference.isoformat()}"
    begun=reference+STEP+timedelta(seconds=30)
    store.begin_cycle(identity,{"pc":"Atlas","symbols":["AAPL"],"reference_at":reference.isoformat(),
        "mode":"live","experiment":session,"configuration_hash":"fixture","started_at":begun.isoformat(),
        "training_cutoff":begun.isoformat(),"status":"running"})
    store.update_cycle(identity,{"status":"partial_failure","completed_at":begun.isoformat(),
        "failures":{"AAPL":{"type":"AcquisitionPending" if retryable else "ContractError",
            "retryable":retryable,"retry_at":begun.isoformat(),"reason":"no_prior_known_price"}}})
    return identity


def test_pending_reconstruction_is_same_session_due_data_only(tmp_path):
    store=ShadowStore(tmp_path/"pending.sqlite3")
    wanted=saved_pending_cycle(store,"manual","2026-10-12T17:00:00Z")
    saved_pending_cycle(store,"other-human","2026-10-12T17:00:00Z")
    saved_pending_cycle(store,"manual","2026-10-12T16:45:00Z",retryable=False)
    assert [item[0] for item in live_session._pending_cycle_passes(store,"manual","2026-10-12T17:16:00Z")]==[wanted]
    assert live_session._pending_cycle_passes(store,"manual","2026-10-12T17:15:29Z")==[]


def test_pending_scout_cycle_recovery_preserves_participant_and_session(tmp_path):
    store = ShadowStore(tmp_path / "pending.sqlite3")
    atlas = saved_pending_cycle(store, "manual", "2026-10-12T17:00:00Z")
    for key, session in (("scout", "manual"), ("older-scout", "older-manual")):
        store.begin_cycle(key, {"pc": "Scout", "symbols": ["DOCU"],
            "reference_at": "2026-10-12T17:00:00Z", "mode": "live", "experiment": session,
            "configuration_hash": "fixture", "started_at": "2026-10-12T17:15:30Z", "status": "running"})
    due = live_session._pending_cycle_passes(store, "manual", "2026-10-12T17:16:00Z", pc="Scout")
    assert [(key, symbols) for key, _, symbols in due] == [("scout", ["DOCU"])]
    assert [key for key, _, _ in live_session._pending_cycle_passes(store, "manual", "2026-10-12T17:16:00Z")] == [atlas]
    foreign = store.save_forecast(forecast(mode="live", pc="Scout", symbol="DOCU", cycle_id=atlas))
    with pytest.raises(ContractError, match="Cycle belongs to another participant"):
        live_session.ExecutionQueue(store, "manual", pc="Scout").enqueue(foreign)


def test_crash_after_permanent_failure_event_before_summary_does_not_retry_model(tmp_path):
    store=ShadowStore(tmp_path/"crash.sqlite3")
    identity="crash-before-summary"
    store.begin_cycle(identity,{"pc":"Atlas","symbols":["AAPL","IONQ"],"reference_at":"2026-10-12T17:00:00+00:00",
        "mode":"live","experiment":"manual","configuration_hash":"fixture",
        "started_at":"2026-10-12T17:15:30+00:00","status":"running"})
    store.event(identity,"symbol_failure",{"type":"ContractError","message":"All enabled models failed"},symbol="IONQ")
    pending=live_session._pending_cycle_passes(store,"manual","2026-10-12T17:16:00Z")
    assert len(pending)==1 and pending[0][2]==["AAPL"]
    store.event(identity,"symbol_failure",{"type":"AcquisitionPending","retryable":True,
        "retry_at":"2026-10-12T17:17:00Z","reason":"unavailable_coverage"},symbol="AAPL")
    assert live_session._pending_cycle_passes(store,"manual","2026-10-12T17:16:00Z")==[]
    assert live_session._pending_cycle_passes(store,"manual","2026-10-12T17:17:00Z")[0][2]==["AAPL"]


def test_new_quarter_first_then_bounded_old_recovery_survives_lost_cursor_and_never_enqueues(tmp_path,monkeypatch):
    store=ShadowStore(tmp_path/"fair.sqlite3")
    old=saved_pending_cycle(store,"manual","2026-10-12T17:00:00Z")
    queue=live_session.ExecutionQueue(store,"manual")
    now=[instant("2026-10-12T17:31:00Z")]
    calls=[]
    def run(inputs,**kwargs):
        reference=kwargs["reference_at"]
        calls.append((reference,kwargs["retry_symbols"],kwargs["allow_execution"]))
        if reference==instant("2026-10-12T17:00:00Z"):
            value=forecast(mode="live",cycle_id=old,issued_at=now[0].isoformat(),
                           execution_blocked_reason="recovered_older_cycle")
            issued=store.save_forecast(value)
            kwargs["on_published"](issued)
            store.update_cycle(old,{"status":"completed","failures":{}})
            return {"cycle_id":old}
        identity=saved_pending_cycle(store,"manual",reference)
        return {"cycle_id":identity}
    runner=SimpleNamespace(store=store,config=SimpleNamespace(publication_offset_seconds=30),run_cycle=run)
    monkeypatch.setattr(live_session,"LiveHistories",lambda **kwargs:SimpleNamespace(bounded=kwargs["bounded"]))
    def pause(seconds):
        assert seconds==1
        now[0]+=timedelta(seconds=seconds)
        if len(calls)==2: now[0]=instant("2026-10-12T17:46:00Z")
    live_session.forecasting_loop(runner,histories={},provider=object(),journal=object(),queue=queue,
        stopped=lambda:len(calls)==3,clock=lambda:now[0],pause=pause)
    assert [row[0] for row in calls]==[instant("2026-10-12T17:15:00Z"),instant("2026-10-12T17:00:00Z"),instant("2026-10-12T17:30:00Z")]
    assert calls[1][1:]==(["AAPL"],False)
    assert queue.due(now[0])==[] and len(store.forecasts())==1
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='forecast_execution_blocked'").fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM intraday_live_queue").fetchone()[0]==0


def test_forecaster_restarts_same_pending_reference_without_lost_memory(tmp_path,monkeypatch):
    store=ShadowStore(tmp_path/"restart.sqlite3")
    saved_pending_cycle(store,"manual","2026-10-12T17:00:00Z")
    calls=[]
    runner=SimpleNamespace(store=store,config=SimpleNamespace(publication_offset_seconds=30),
        run_cycle=lambda inputs,**kwargs:calls.append(kwargs))
    monkeypatch.setattr(live_session,"LiveHistories",lambda **kwargs:object())
    live_session.forecasting_loop(runner,histories={},provider=object(),journal=object(),
        queue=SimpleNamespace(session_id="manual",enqueue=lambda _:pytest.fail("No fabricated forecast")),
        stopped=lambda:bool(calls),clock=lambda:"2026-10-12T17:16:00Z",pause=lambda _:None)
    assert calls[0]["reference_at"]==instant("2026-10-12T17:00:00Z")
    assert calls[0]["retry_symbols"]==["AAPL"] and calls[0]["allow_execution"]


def test_forecast_with_foreign_cycle_session_cannot_enter_new_human_queue(tmp_path):
    store=ShadowStore(tmp_path/"foreign.sqlite3")
    identity=saved_pending_cycle(store,"old-human","2026-10-08T17:00:00Z")
    f=store.save_forecast(forecast(mode="live",cycle_id=identity))
    queue=live_session.ExecutionQueue(store,"new-human")
    with pytest.raises(ContractError,match="another manual session"): queue.enqueue(f)
    assert queue.due("2026-10-08T17:17:00Z")==[]


def test_no_closed_or_unspecified_opening_target():
    assert not live_session.eligible_reference("2026-10-10T17:00:00Z")  # Saturday
    assert not live_session.eligible_reference("2026-10-12T10:45:00Z")  # 04:00 issue
    assert live_session.eligible_reference("2026-10-12T11:00:00Z")
    assert not live_session.eligible_reference("2026-10-12T23:30:00Z")  # 16:45 issue


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
def test_scoring_acquires_exact_outcome_after_cutoff_without_execution(tmp_path, monkeypatch, pc):
    import pandas as pd
    from tests.test_intraday_store import candle
    store = ShadowStore(tmp_path/"forecast.sqlite3")
    f = store.save_forecast(forecast(mode="live", pc=pc))
    foreign = store.save_forecast(forecast(mode="live", cycle_id="foreign",
        pc="Scout" if pc == "Atlas" else "Atlas"))
    calls, done = [], []
    class Incoming:
        def __init__(self, **kw):
            calls.append(kw)
        def bind_cycle(self, key):
            assert key == "score:" + f["forecast_id"]
        def get(self, symbol):
            assert symbol == "AAPL"
            value = candle(close="101")
            value["opened_at"] = pd.Timestamp(value["opened_at"])
            return SimpleNamespace(frame=pd.DataFrame([value]))
    monkeypatch.setattr(live_session,"LiveHistories",Incoming)
    live_session.scoring_loop(store,histories={"AAPL":object()},provider=object(),journal=object(),
        session_id="manual",stopped=lambda:bool(done),clock=lambda:"2026-10-09T00:01:00Z",
        pause=lambda seconds:done.append(True), pc=pc)
    assert calls[0]["reference_at"] == f["target_open"]
    result = store.revisions(f["forecast_id"])[-1]
    assert result["status"] == "scored" and result["reference_to_target_return"] == "0.01"
    assert store.revisions(foreign["forecast_id"]) == []


def test_scoring_audits_legacy_synthetic_and_continues_after_unavailable_target(tmp_path,monkeypatch):
    import pandas as pd
    from ml.intraday.prediction import digest
    from tests.test_intraday_store import candle
    store=ShadowStore(tmp_path/"synthetic-scoring-audit.sqlite3")
    source="databento:EQUS.MINI:ohlcv-1m"
    forecasts={symbol:store.save_forecast(forecast(mode="live",symbol=symbol,
        source_dataset="EQUS.MINI",source_id=source)) for symbol in ("AAPL","IONQ","MSFT")}
    def target(symbol, *, synthetic=False):
        return candle(symbol=symbol,source_dataset="EQUS.MINI",source_id=source,
            high="100" if synthetic else "101",low="100" if synthetic else "99",
            close="100" if synthetic else "101",volume=0 if synthetic else 100,
            synthetic=synthetic,no_trade=synthetic,
            evidence_json='{"legacy_confirmations":2}' if synthetic else "{}")
    first_missing=store.observe(forecasts["AAPL"],as_of="2026-10-08T17:46:00Z")
    first_legacy=target("AAPL",synthetic=True)
    first_hash=store.record_candle(first_legacy)
    previous_synthetic=store.observe(forecasts["MSFT"],as_of="2026-10-08T17:46:00Z",
                                    candle=target("MSFT",synthetic=True))
    preserved={symbol:store.revisions(forecasts[symbol]["forecast_id"]) for symbol in ("AAPL","MSFT")}
    original_candles=store.observed_candles()
    calls,passes=[],[]
    class Incoming:
        def __init__(self,**kwargs):
            assert kwargs["bounded"] is True
            self.reference=kwargs["reference_at"]
        def bind_cycle(self,key):
            assert key.startswith("score:")
        def get(self,symbol):
            calls.append(symbol)
            assert self.reference==forecasts[symbol]["target_open"]
            if symbol=="AAPL":
                raise ContractError("Exact interval remains unavailable; do not fill from legacy price")
            assert symbol=="IONQ"  # Earlier scored legacy revision remains immutable.
            value=target(symbol)
            value["opened_at"]=pd.Timestamp(value["opened_at"])
            return SimpleNamespace(frame=pd.DataFrame([value]))
    monkeypatch.setattr(live_session,"LiveHistories",Incoming)
    live_session.scoring_loop(store,histories={s:object() for s in forecasts},provider=object(),journal=object(),
        session_id="manual-audit",stopped=lambda:len(passes)==2,clock=lambda:"2026-10-08T18:00:00Z",
        pause=lambda seconds:passes.append(seconds))
    assert calls==["AAPL","IONQ","AAPL"] and passes==[3,3]
    assert store.revisions(forecasts["AAPL"]["forecast_id"])==preserved["AAPL"]==[first_missing]
    assert store.revisions(forecasts["MSFT"]["forecast_id"])==preserved["MSFT"]==[previous_synthetic]
    scored=store.revisions(forecasts["IONQ"]["forecast_id"])
    assert len(scored)==1 and scored[0]["status"]=="scored" and scored[0]["target_not_down"] is True
    assert store.forecasts()==list(forecasts.values())
    assert [c for c in store.observed_candles() if c["synthetic"]]==original_candles
    with store.connect() as db:
        audits=[json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM events WHERE kind='unverified_synthetic_outcome_requires_audit'")]
        retries=[json.loads(row[0]) for row in db.execute("SELECT payload FROM events WHERE kind='target_scoring_retry'")]
    assert {a["candle_hash"] for a in audits}=={first_hash,previous_synthetic["candle_id"]}
    assert len(audits)==2 and all(a["immutable_original_retained"] is True for a in audits)
    assert len(retries)==2 and all(r["forecast_id"]==forecasts["AAPL"]["forecast_id"] for r in retries)
    assert digest(first_legacy)==first_hash


def test_manual_startup_failure_has_failed_status_and_never_constructs_broker(tmp_path,monkeypatch):
    from ml.intraday import runtime_compatibility,data
    from app.services import schwab,databento_market_data
    import dotenv
    monkeypatch.setattr(live_session,"local_symbols",lambda *args:("Atlas",("AAPL",)))
    monkeypatch.setattr(runtime_compatibility,"read_gameplan_runtime_compatibility",lambda *a,**k:{"ready":True})
    monkeypatch.setattr(dotenv,"load_dotenv",lambda *args:None)
    monkeypatch.setattr(data,"load_local_history",lambda *a,**k:object())
    def fail():
        raise RuntimeError("provider configuration fixture")
    monkeypatch.setattr(databento_market_data,"DatabentoMarketDataProvider",fail)
    monkeypatch.setattr(schwab,"SchwabSession",lambda:pytest.fail("broker constructed after failed setup"))
    with pytest.raises(RuntimeError,match="configuration fixture"):
        live_session.run_manual(repository=tmp_path,datastore=tmp_path,profile=tmp_path/"profile.json",
            watchlist=tmp_path/"watchlist.txt",config=live_session.ResearchConfig())
    status=json.loads((tmp_path/"intraday/manual-session.json").read_text())
    assert status["status"] == "FAILED" and status["stop_reason"] == "startup_failure"


def test_forecast_worker_failure_is_not_a_successful_manual_stop(tmp_path,monkeypatch):
    from ml.intraday import runtime_compatibility,data,account_execution
    from app.services import schwab,databento_market_data
    import dotenv
    monkeypatch.setattr(live_session,"local_symbols",lambda *args:("Atlas",("AAPL",)))
    monkeypatch.setattr(runtime_compatibility,"read_gameplan_runtime_compatibility",lambda *a,**k:{"ready":True})
    monkeypatch.setattr(dotenv,"load_dotenv",lambda *args:None)
    monkeypatch.setattr(data,"load_local_history",lambda *a,**k:object())
    monkeypatch.setattr(databento_market_data,"DatabentoMarketDataProvider",lambda:SimpleNamespace(dataset="EQUS.MINI"))
    monkeypatch.setattr(schwab,"SchwabSession",lambda:object())
    monkeypatch.setattr(account_execution,"IntradayAccountExecutor",lambda **kw:SimpleNamespace(
        step=lambda *a:pytest.fail("order step called"),recover=lambda:None))
    def fail(*a,**kw):
        raise RuntimeError("forecast worker fixture failure")
    monkeypatch.setattr(live_session,"forecasting_loop",fail)
    monkeypatch.setattr(live_session,"scoring_loop",lambda *a,**kw:None)
    result=live_session.run_manual(repository=tmp_path,datastore=tmp_path,profile=tmp_path/"profile.json",
        watchlist=tmp_path/"watchlist.txt",config=live_session.ResearchConfig())
    status=json.loads((tmp_path/"intraday/manual-session.json").read_text())
    assert result["status"] == status["status"] == "FAILED"
    assert status["stop_reason"] == "worker_failure" and status["worker_errors"][0]["worker"] == "forecast"



@pytest.mark.parametrize("first_disposition", [
    "previous_intention_reconciliation", "proven_unsent", "submission_recovery_required", "exception"])
def test_broker_retry_after_fences_next_symbol_and_recovery_in_same_batch(tmp_path,monkeypatch,first_disposition):
    """A genuine parsed429 header controls one batch, persisted retries and recovery.

    Manual orchestration uses two immutable fixture forecasts and injected
    transports only. This does not start a provider, train a model or POST.
    """
    import requests
    from ml.intraday import runtime_compatibility,data,account_execution,cancellation
    from app.services import schwab,databento_market_data
    import dotenv
    now=[instant("2026-10-08T17:17:00Z")]
    started=now[0]
    events=[]
    steps={"AAPL":0,"AMZN":0}
    response=requests.Response()
    response.status_code=429
    response.headers["Retry-After"]="120"
    error=requests.HTTPError("429 transport fixture",response=response)
    delay=cancellation.retry_delay(error,now[0])
    assert delay==120
    monkeypatch.setattr(live_session,"local_symbols",lambda *args:("Atlas",("AAPL","AMZN")))
    monkeypatch.setattr(runtime_compatibility,"read_gameplan_runtime_compatibility",lambda *a,**k:{"ready":True})
    monkeypatch.setattr(dotenv,"load_dotenv",lambda *args:None)
    monkeypatch.setattr(data,"load_local_history",lambda *a,**k:object())
    monkeypatch.setattr(databento_market_data,"DatabentoMarketDataProvider",lambda:SimpleNamespace(dataset="EQUS.MINI"))
    monkeypatch.setattr(schwab,"SchwabSession",lambda:object())
    class InlineThread:
        def __init__(self,*,target,**kwargs):
            self.target=target
        def start(self):
            self.target()
        def is_alive(self):
            return False
        def join(self,**kwargs):
            pass
    monkeypatch.setattr(live_session.threading,"Thread",InlineThread)
    def publish(runner,**kwargs):
        for symbol in ("AAPL","AMZN"):
            saved=runner.store.save_forecast(forecast(mode="live",symbol=symbol,issued_at=now[0].isoformat()))
            kwargs["queue"].enqueue(saved)
    monkeypatch.setattr(live_session,"forecasting_loop",publish)
    monkeypatch.setattr(live_session,"scoring_loop",lambda *a,**k:None)
    class Executor:
        def step(self,value):
            symbol=value["symbol"]
            steps[symbol]+=1
            events.append(("step",symbol,(now[0]-started).total_seconds()))
            if symbol=="AAPL" and steps[symbol]==1:
                if first_disposition=="exception":
                    raise error
                disposition="not_submitted" if first_disposition=="proven_unsent" else first_disposition
                return dict(disposition=disposition,kind="execution_disposition",
                    evidence={"retry_after_seconds":delay,"proven_unsent_retry_eligible":first_disposition=="proven_unsent"})
            return dict(disposition="no_purchasing_capacity",kind="execution_disposition",evidence={"fixture":"completed"})
        def recover(self):
            events.append(("recover",None,(now[0]-started).total_seconds()))
    monkeypatch.setattr(account_execution,"IntradayAccountExecutor",lambda **kwargs:Executor())
    def sleep(seconds):
        if (now[0]-started).total_seconds()>=121:
            raise KeyboardInterrupt()
        now[0]+=timedelta(seconds=seconds)
    result=live_session.run_manual(repository=tmp_path,datastore=tmp_path,profile=tmp_path/"profile.json",
        watchlist=tmp_path/"watchlist.txt",config=live_session.ResearchConfig(),clock=lambda:now[0],sleep=sleep)
    assert result["status"]=="STOPPED"
    assert events[0]==("step","AAPL",0)
    assert not [event for event in events[1:] if event[2]<120],events
    assert any(event[:2]==("step","AMZN") and event[2]>=120 for event in events)
    assert any(event[0]=="recover" and event[2]>=120 for event in events)
    assert steps["AAPL"]==(1 if first_disposition=="submission_recovery_required" else 2)
    store=ShadowStore(tmp_path/"intraday/shadow.sqlite3")
    with store.connect() as db:
        wait=instant(db.execute("SELECT next_attempt_at FROM intraday_live_retry WHERE scope='broker'").fetchone()[0])
        assert wait==started+timedelta(seconds=120)
        status=db.execute("SELECT status FROM intraday_live_queue WHERE symbol='AAPL'").fetchone()[0]
        assert status==("submission_recovery_required" if first_disposition=="submission_recovery_required" else "no_purchasing_capacity")


def scout_manual_fixture(tmp_path, monkeypatch, *, proof_changes=None):
    """All execution, provider and model boundaries are offline fixtures."""
    import sys
    import dotenv
    from ml.intraday import runtime_compatibility, data, account_execution
    from app.services import schwab, databento_market_data
    calls, executors = [], []
    monkeypatch.setattr(live_session,"local_symbols",lambda *args:("Scout",("DOCU",)))
    monkeypatch.setattr(dotenv,"load_dotenv",lambda *args:None)
    monkeypatch.setattr(runtime_compatibility,"read_gameplan_runtime_compatibility",
        lambda *a,**k:pytest.fail("Scout must use authenticated remote compatibility"))
    monkeypatch.setattr(account_execution,"IntradayAccountExecutor",
        lambda **kw:pytest.fail("Scout must not instantiate Atlas execution stores"))
    monkeypatch.setattr(schwab,"SchwabSession",lambda:SimpleNamespace(offline_broker_fixture=True))
    def history(*args,**kwargs):
        calls.append("history")
        return object()
    def provider():
        calls.append("provider")
        return SimpleNamespace(dataset="EQUS.MINI")
    monkeypatch.setattr(data,"load_local_history",history)
    monkeypatch.setattr(databento_market_data,"DatabentoMarketDataProvider",provider)
    class Executor:
        def __init__(self, **kwargs):
            self.kwargs=kwargs
            self.evidence_paths={"journal":str(tmp_path/"scout-journal.sqlite3")}
            assert kwargs["broker"].offline_broker_fixture
            assert kwargs["local_symbols"] == ("DOCU",)
            executors.append(self)
        def preflight(self):
            from test_intraday_scout_runtime_gate import saved_readiness
            calls.append("preflight")
            assert not self.kwargs["manual_authority"]()
            changes = dict(proof_changes or {})
            session = changes.pop("session_id", self.kwargs["session_id"])
            binding, receipt = saved_readiness(tmp_path / "authority-proof", self.kwargs["clock"](), changes=changes)
            return {"readiness_receipt":receipt,"authority_binding":binding,"session_id":session}
        def step(self, value):
            calls.append("step")
            assert value["pc"] == "Scout"
            assert self.kwargs["manual_authority"]()
            return {"kind":"execution_disposition","disposition":"no_purchasing_capacity","evidence":{}}
        def recover(self):
            calls.append("recover")
    monkeypatch.setitem(sys.modules,"ml.intraday.scout_execution",SimpleNamespace(ScoutAccountExecutor=Executor))
    class InlineThread:
        def __init__(self,*,target,**kwargs):
            self.target=target
        def start(self):
            self.target()
        def is_alive(self):
            return False
        def join(self,**kwargs):
            pass
    monkeypatch.setattr(live_session.threading,"Thread",InlineThread)
    def publish(runner,**kwargs):
        calls.append("forecast")
        queue=kwargs["queue"]
        assert queue.pc == "Scout"
        saved=runner.store.save_forecast(forecast(mode="live",pc="Scout",symbol="DOCU",cycle_id=queue.session_id))
        queue.enqueue(saved)
    def score(*args,**kwargs):
        calls.append("scoring")
        assert kwargs["pc"] == "Scout"
    monkeypatch.setattr(live_session,"forecasting_loop",publish)
    monkeypatch.setattr(live_session,"scoring_loop",score)
    def stop(seconds):
        raise KeyboardInterrupt()
    return dict(repository=tmp_path,datastore=tmp_path,profile=tmp_path/"profile.json",
        watchlist=tmp_path/"watchlist.txt",config=live_session.ResearchConfig(),
        clock=lambda:"2026-10-08T17:17:00Z",sleep=stop,scout_admission="atlas-service"),calls,executors


def test_scout_manual_start_preflight_and_history_never_use_atlas_owner(tmp_path,monkeypatch,capsys):
    kwargs,calls,executors=scout_manual_fixture(tmp_path,monkeypatch)
    result=live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED" and result["session_id"].startswith("manual-scout-15m:")
    assert calls[:3] == ["preflight","history","provider"]
    assert calls[3:] == ["forecast","scoring","step","recover"]
    assert not executors[0].kwargs["manual_authority"]()
    status=json.loads((tmp_path/"intraday/manual-session.json").read_text())
    assert status["pc"] == "Scout" and status["process_create_time"] > 0
    assert status["broker_orders_automatically_cancelled"] is False
    assert status["stop_reason"] == "manual_stop"
    assert status["evidence_paths"]["executor_journal"] == str(tmp_path/"scout-journal.sqlite3")
    session_root=tmp_path/"intraday/sessions"/result["session_id"].replace(":","-")
    assert json.loads((session_root/"status.json").read_text()) == status
    logs=[json.loads(line) for line in (session_root/"events.jsonl").read_text().splitlines()]
    assert [row["status"] for row in logs if row["event"] == "session_status"] == ["STARTING","RUNNING","STOPPING","STOPPED"]
    assert {row["event"] for row in logs} >= {"authority_ready","forecast_published","execution_disposition"}
    proof = next(row for row in logs if row["event"] == "authority_ready")
    assert proof["writers"]["atlas"]["ready"] is True and proof["writers"]["gameplan"]["ready"] is True
    assert not {"authority_binding", "account_fingerprint", "readiness_receipt"} & set(proof)
    assert all(row["session_id"] == result["session_id"] and row["pc"] == "Scout" for row in logs)
    assert "Scout 15m manual session running" in capsys.readouterr().out
    prior_bytes=(session_root/"status.json").read_bytes()
    second=live_session.run_manual(**kwargs)
    assert second["session_id"] != result["session_id"]
    assert (session_root/"status.json").read_bytes() == prior_bytes


@pytest.mark.parametrize("changes", [{"ready":False},{"authenticated":False},{"protocol_compatible":False},
    {"participant_id":"pc-original"},{"stream":"gameplan"},{"session_id":"another-session"},
    {"protocol_version":""},{"ready":1},{"writers":{}},{"expires_at":"2026-10-08T17:17:00Z"}])
def test_scout_rejects_unbound_preflight_before_history_provider_or_training(tmp_path,monkeypatch,changes):
    from ml.intraday.scout_authority import AuthorityProtocolError
    kwargs,calls,_=scout_manual_fixture(tmp_path,monkeypatch,proof_changes=changes)
    with pytest.raises((ContractError, AuthorityProtocolError)):
        live_session.run_manual(**kwargs)
    assert calls == ["preflight"]
    status=json.loads((tmp_path/"intraday/manual-session.json").read_text())
    assert status["status"] == "FAILED" and status["stop_reason"] == "startup_failure"
    assert status["broker_orders_automatically_cancelled"] is False
    assert status["schema_version"] == live_session.SCOUT_VERSION


def test_scout_positive_dictionary_does_not_substitute_for_saved_readiness(tmp_path, monkeypatch):
    kwargs,calls,_=scout_manual_fixture(tmp_path,monkeypatch)
    import sys
    executor_type=sys.modules["ml.intraday.scout_execution"].ScoutAccountExecutor
    def unbound(self):
        return dict(ready=True, authenticated=True, protocol_compatible=True, participant_id="pc-new",
                    stream="scout-15m", session_id=self.kwargs["session_id"], protocol="intraday-shared-admission-v1")
    monkeypatch.setattr(executor_type,"preflight",unbound)
    with pytest.raises(ContractError,match="authenticated, protocol-compatible"):
        live_session.run_manual(**kwargs)
    assert not calls
    assert json.loads((tmp_path/"intraday/manual-session.json").read_text())["status"] == "FAILED"


def test_scout_protocol_exception_stays_startup_failure_without_fallback(tmp_path,monkeypatch):
    kwargs,calls,executors=scout_manual_fixture(tmp_path,monkeypatch)
    import sys
    executor_type=sys.modules["ml.intraday.scout_execution"].ScoutAccountExecutor
    def fail(self):
        raise ConnectionError("authority unavailable fixture")
    monkeypatch.setattr(executor_type,"preflight",fail)
    with pytest.raises(ConnectionError,match="authority unavailable fixture"):
        live_session.run_manual(**kwargs)
    assert calls == []
    status=json.loads((tmp_path/"intraday/manual-session.json").read_text())
    assert status["status"] == "FAILED"
    assert status["error_type"] == "ConnectionError"
    assert not executors[0].kwargs["manual_authority"]()
