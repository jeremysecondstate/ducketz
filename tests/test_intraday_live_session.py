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
    with store.connect() as db:
        missed = json.loads(db.execute("SELECT payload FROM events WHERE kind='missed_cycle_boundaries'").fetchone()[0])
    assert missed["missed_count"] == 2


def test_no_closed_or_unspecified_opening_target():
    assert not live_session.eligible_reference("2026-10-10T17:00:00Z")  # Saturday
    assert not live_session.eligible_reference("2026-10-12T10:45:00Z")  # 04:00 issue
    assert live_session.eligible_reference("2026-10-12T11:00:00Z")
    assert not live_session.eligible_reference("2026-10-12T23:30:00Z")  # 16:45 issue


def test_scoring_acquires_exact_outcome_after_cutoff_without_execution(tmp_path, monkeypatch):
    import pandas as pd
    from tests.test_intraday_store import candle
    store = ShadowStore(tmp_path/"forecast.sqlite3")
    f = store.save_forecast(forecast(mode="live"))
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
        pause=lambda seconds:done.append(True))
    assert calls[0]["reference_at"] == f["target_open"]
    result = store.revisions(f["forecast_id"])[-1]
    assert result["status"] == "scored" and result["reference_to_target_return"] == "0.01"


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
