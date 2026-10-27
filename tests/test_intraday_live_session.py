import builtins
from datetime import timedelta
import importlib.util
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from ml.intraday import live_session
from ml.intraday.prediction import ContractError, instant, STEP
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast


def module_fixture(monkeypatch, name, **attributes):
    """Supply a caller boundary without requiring the historical implementation."""
    module = ModuleType(name)
    module.__dict__.update(attributes)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def atlas_manual_modules(monkeypatch):
    """Atlas caller cases test orchestration with explicitly injected adapters."""
    module_fixture(monkeypatch, "ml.intraday.runtime_compatibility",
        read_gameplan_runtime_compatibility=lambda *args, **kwargs: {"ready": True})
    return module_fixture(monkeypatch, "ml.intraday.account_execution",
        IntradayAccountExecutor=lambda **kwargs: pytest.fail("unexpected Atlas executor construction"))


def block_remote_caller_imports(monkeypatch):
    """Scout's local branch must not even resolve a historical authority module."""
    legacy = frozenset({
        "ml.intraday.account_execution", "ml.intraday.cancellation",
        "ml.intraday.readiness", "ml.intraday.runtime_compatibility",
        "ml.intraday.scout_execution", "ml.intraday.scout_runtime_gate",
        "ml.intraday.scout_authority", "ml.intraday.scout_books",
        "ml.intraday.scout_accounting", "ml.intraday.admission_service",
        "ml.intraday.admission_transport", "ml.intraday.admission_enrollment",
        "ml.intraday.scout_enrollment",
    })
    original = builtins.__import__
    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        resolved = importlib.util.resolve_name("." * level + name, globals["__package__"]) if level else name
        requested = {resolved, *(resolved + "." + item for item in (fromlist or ()))}
        assert not requested.intersection(legacy), "Local caller imported a historical authority dependency"
        assert not any(item == "ml.account_gameplan" or item.startswith("ml.account_gameplan.")
                       for item in requested), "Local caller imported Gameplan account authority"
        return original(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", guarded)


@pytest.fixture(autouse=True)
def isolate_minute_acquisition_in_execution_fixtures(monkeypatch):
    """Execution fixtures own no market-data transport; feed behavior has its own tests."""
    from ml.intraday import minute_feed
    class FixtureFeed:
        history_source = None
        scoring_history_source = None
        def __init__(self, **kwargs):
            pass
        def run(self, **kwargs):
            pass
    monkeypatch.setattr(minute_feed, "QuarterHourMinuteFeed", FixtureFeed)


@pytest.mark.parametrize("now,expected", [
    ("2026-10-12T17:15:29Z","2026-10-12T16:45:00Z"),
    ("2026-10-12T17:15:30Z","2026-10-12T17:00:00Z"),
    ("2026-10-12T17:30:30Z","2026-10-12T17:15:00Z"),
    ("2026-10-12T17:46:00Z","2026-10-12T17:30:00Z")])
def test_start_latest_completed_on_quarter_grid(now, expected):
    assert live_session.latest_completed_reference(now, publication_offset_seconds=30) == instant(expected)


def test_forecast_uses_independent_saved_history_without_a_provider_call(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "forecast.sqlite3")
    references, calls = [], []
    incoming = object()
    def source(reference):
        references.append(reference)
        return incoming
    def run(history, **kwargs):
        assert history is incoming
        calls.append(kwargs)
    runner = SimpleNamespace(store=store, config=live_session.ResearchConfig(), run_cycle=run)
    queue = live_session.ExecutionQueue(store, "saved-minute-session", pc="Scout")
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kw: pytest.fail("forecast fetched data"))
    live_session.forecasting_loop(runner, histories={}, provider=object(), journal=object(), queue=queue,
        stopped=lambda: bool(calls), clock=lambda: "2026-10-14T17:15:30Z", pause=lambda _: None,
        history_source=source)
    assert references == [instant("2026-10-14T17:00:00Z")]
    assert calls[0]["allow_execution"] is True


def test_scoring_consumes_saved_feed_candle_without_fetching_after_close(tmp_path, monkeypatch):
    import pandas as pd
    from tests.test_intraday_store import candle
    store = ShadowStore(tmp_path / "forecast.sqlite3")
    saved = store.save_forecast(forecast(mode="live", pc="Scout"))
    calls, done = [], []
    class Incoming:
        def bind_cycle(self, identity):
            assert identity == "score:" + saved["forecast_id"]
        def get(self, symbol):
            value = candle(close="101")
            value["opened_at"] = pd.Timestamp(value["opened_at"])
            return SimpleNamespace(frame=pd.DataFrame([value]))
    def source(reference):
        calls.append(reference)
        return Incoming()
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kw: pytest.fail("scorer fetched after close"))
    live_session.scoring_loop(store, histories={"AAPL": object()}, provider=object(), journal=object(),
        session_id="saved-minute-session", stopped=lambda: bool(done), clock=lambda: "2026-10-09T00:01:00Z",
        pause=lambda _: done.append(True), pc="Scout", history_source=source)
    assert calls == [saved["target_open"]]
    assert store.revisions(saved["forecast_id"])[-1]["status"] == "scored"


def test_manual_scout_wires_persisted_minute_feed_into_both_consumers_without_new_orders(tmp_path, monkeypatch):
    import dotenv
    from app.services import databento_market_data, schwab
    from ml.intraday import data, minute_feed, minute_persistence, scout_local_execution

    calls, feeds, threads, executors = [], [], [], []
    native_provider = SimpleNamespace(dataset="EQUS.MINI")
    monkeypatch.setattr(live_session, "local_symbols", lambda *args: ("Scout", ("DOCU",)))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(data, "load_local_history", lambda *args, **kwargs: object())
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider", lambda: native_provider)
    monkeypatch.setattr(schwab, "SchwabSession", lambda: SimpleNamespace(offline_fixture=True))

    class Executor:
        evidence_paths = {}
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            executors.append(self)
            assert kwargs["broker"].offline_fixture
        def preflight(self):
            return {"session_id": self.kwargs["session_id"], "mode": "scout-local-broker-account", "ready": True}
        def step(self, forecast):
            pytest.fail("Minute acquisition must not create an executable forecast or order")
        def recover(self):
            calls.append("existing_order_recovery")

    class Feed:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.history_source = object()
            self.scoring_history_source = object()
            feeds.append(self)
            assert isinstance(kwargs["provider"], minute_persistence.PersistedMinuteProvider)
            assert kwargs["provider"]._provider is native_provider
            assert kwargs["pc"] == "Scout" and kwargs["symbols"] == ("DOCU",)
        def run(self, **kwargs):
            assert not kwargs["stopped"]()
            calls.append("independent_minute_data")

    class InlineThread:
        def __init__(self, *, target, name, **kwargs):
            self.target = target
            threads.append(name)
        def start(self):
            self.target()
        def is_alive(self):
            return False
        def join(self, **kwargs):
            pass

    def consume(kind, **kwargs):
        expected = feeds[0].history_source if kind == "forecast" else feeds[0].scoring_history_source
        assert kwargs["history_source"] is expected
        assert kwargs["provider"] is feeds[0].kwargs["provider"]
        assert kwargs["histories"] is feeds[0].kwargs["histories"]
        calls.append(kind)

    def stop(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(scout_local_execution, "ScoutLocalAccountExecutor", Executor)
    monkeypatch.setattr(minute_feed, "QuarterHourMinuteFeed", Feed)
    monkeypatch.setattr(live_session.threading, "Thread", InlineThread)
    monkeypatch.setattr(live_session, "forecasting_loop", lambda runner, **kwargs: consume("forecast", **kwargs))
    monkeypatch.setattr(live_session, "scoring_loop", lambda store, **kwargs: consume("scoring", **kwargs))
    block_remote_caller_imports(monkeypatch)
    result = live_session.run_manual(repository=tmp_path, datastore=tmp_path, profile=tmp_path / "profile.json",
        watchlist=tmp_path / "watchlist.txt", config=live_session.ResearchConfig(),
        clock=lambda: "2026-10-14T22:31:00Z", sleep=stop)
    assert result["status"] == "STOPPED"
    assert len(feeds) == len(executors) == 1
    assert calls == ["forecast", "scoring", "independent_minute_data", "existing_order_recovery"]
    assert threads == ["intraday-manual-forecast", "intraday-manual-scoring", "intraday-manual-minute-data"]
    assert feeds[0].kwargs["session_id"] == result["session_id"]
    assert feeds[0].kwargs["publication_offset_seconds"] == 30
    assert not executors[0].kwargs["manual_authority"]()
    with feeds[0].kwargs["store"].connect() as db:
        assert db.execute("SELECT COUNT(*) FROM intraday_live_queue").fetchone()[0] == 0


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


def test_default_cli_never_starts_worker(monkeypatch, capsys, tmp_path):
    module_fixture(monkeypatch, "ml.intraday.readiness",
        inspect_readiness=lambda **kw:{"manual_live_start_ready":False})
    monkeypatch.setattr(live_session,"run_manual",lambda **kw:pytest.fail("manual worker called"))
    assert live_session.main(["--repository", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().out)["manual_live_start_ready"] is False


def test_manual_live_entrypoint_enables_supervised_model_policies(monkeypatch, tmp_path):
    captured = {}
    def manual(**kwargs):
        captured.update(kwargs)
        return {"status": "STOPPED"}
    monkeypatch.setattr(live_session, "run_manual", manual)
    assert live_session.main(["--repository", str(tmp_path), "--live"]) == 0
    assert captured["config"].use_supervised_models is True


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


def saved_pending_cycle(store,session,reference,*,retryable=True,pc="Atlas"):
    reference=instant(reference)
    identity=f"{session}:{reference.isoformat()}"
    begun=reference+STEP+timedelta(seconds=30)
    store.begin_cycle(identity,{"pc":pc,"symbols":["AAPL"],"reference_at":reference.isoformat(),
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


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
def test_sustained_overruns_share_turns_with_due_recovery_without_replaying_orders(tmp_path, monkeypatch, pc):
    store = ShadowStore(tmp_path / "overrun.sqlite3")
    first = saved_pending_cycle(store, "manual", "2026-10-12T16:45:00Z", pc=pc)
    second = saved_pending_cycle(store, "manual", "2026-10-12T17:00:00Z", pc=pc)
    saved_pending_cycle(store, "manual", "2026-10-12T16:30:00Z", retryable=False, pc=pc)
    saved_pending_cycle(store, "older-human", "2026-10-12T17:30:00Z", pc=pc)
    saved_pending_cycle(store, "manual", "2026-10-12T19:45:00Z",
                        pc="Scout" if pc == "Atlas" else "Atlas")
    queue = live_session.ExecutionQueue(store, "manual", pc=pc)
    now = [instant("2026-10-12T17:31:00Z")]
    calls, recovered = [], []

    def run(inputs, **kwargs):
        reference = kwargs["reference_at"]
        calls.append({"at": now[0], **kwargs})
        if kwargs["retry_symbols"] is None:
            identity = "fresh:" + reference.isoformat()
            store.begin_cycle(identity, {"pc": pc, "symbols": ["AAPL"], "reference_at": reference.isoformat(),
                "mode": "live", "experiment": "manual", "configuration_hash": "fixture",
                "started_at": now[0].isoformat(), "status": "running"})
            # Genuine research may run longer than a quarter; do not shorten
            # it, change workers, or substitute an older fitted model.
            now[0] += timedelta(seconds=2218)
            store.update_cycle(identity, {"status": "completed"})
        else:
            identity = first if reference == instant("2026-10-12T16:45:00Z") else second
            assert kwargs["retry_symbols"] == ["AAPL"]
            assert not kwargs["allow_execution"]
            if identity == second:
                value = forecast(mode="live", pc=pc, cycle_id=identity, reference_at=reference.isoformat(),
                    issued_at=now[0].isoformat(), information_available_at=now[0].isoformat(),
                    data_through=(reference + STEP).isoformat(), target_open=(reference + 2 * STEP).isoformat(),
                    target_close=(reference + 3 * STEP).isoformat(), execution_blocked_reason="recovered_older_cycle")
                issued = store.save_forecast(value)
                recovered.append(issued)
                kwargs["on_published"](issued)
                store.update_cycle(identity, {"status": "completed", "failures": {}})
            # The first retry remains unavailable. Updating its attempt time
            # must let the other due cycle recover on the following turn.
            now[0] += timedelta(seconds=5)
        return {"cycle_id": identity}

    runner = SimpleNamespace(store=store, config=SimpleNamespace(publication_offset_seconds=30), run_cycle=run)
    monkeypatch.setattr(live_session, "eligible_reference", lambda reference: True)
    live_session.forecasting_loop(runner, histories={}, provider=None, journal=None, queue=queue,
        stopped=lambda: len(calls) == 5, clock=lambda: now[0], pause=lambda seconds: None,
        history_source=lambda reference: object())

    assert [call["retry_symbols"] for call in calls] == [None, ["AAPL"], None, ["AAPL"], None]
    assert [calls[index]["reference_at"] for index in (1, 3)] == [
        instant("2026-10-12T16:45:00Z"), instant("2026-10-12T17:00:00Z")]
    for call in calls[::2]:
        assert call["reference_at"] == live_session.latest_completed_reference(
            call["at"], publication_offset_seconds=30)
        assert call["allow_execution"]
    assert [key for key, _, _ in live_session._pending_cycle_passes(store, "manual", now[0], pc=pc)] == [first]
    assert store.forecast(second, "AAPL") == recovered[0]
    assert len(store.forecasts(pc=pc)) == 1 and queue.due(now[0]) == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM intraday_live_queue").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='forecast_execution_blocked'").fetchone()[0] == 1


@pytest.mark.parametrize("stop_after", [1, 2])
def test_manual_stop_between_overrun_and_recovery_turns_does_not_start_more_work(tmp_path, monkeypatch, stop_after):
    store = ShadowStore(tmp_path / "stop.sqlite3")
    old = saved_pending_cycle(store, "manual", "2026-10-12T17:00:00Z", pc="Scout")
    now = [instant("2026-10-12T17:31:00Z")]
    calls, histories = [], []

    def run(inputs, **kwargs):
        calls.append(kwargs)
        now[0] += timedelta(minutes=37)
        return {"cycle_id": old} if kwargs["retry_symbols"] else None

    runner = SimpleNamespace(store=store, config=SimpleNamespace(publication_offset_seconds=30), run_cycle=run)
    monkeypatch.setattr(live_session, "eligible_reference", lambda reference: True)
    live_session.forecasting_loop(runner, histories={}, provider=None, journal=None,
        queue=SimpleNamespace(session_id="manual", pc="Scout", enqueue=lambda value: pytest.fail("No forecast")),
        stopped=lambda: len(calls) == stop_after, clock=lambda: now[0], pause=lambda seconds: None,
        history_source=lambda reference: histories.append(reference))

    assert len(calls) == len(histories) == stop_after
    assert calls[0]["retry_symbols"] is None
    if stop_after == 2:
        assert calls[1]["retry_symbols"] == ["AAPL"] and not calls[1]["allow_execution"]
    assert [key for key, _, _ in live_session._pending_cycle_passes(store, "manual", now[0], pc="Scout")] == [old]


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
    # Seed a historical outcome using the legacy admission behavior. Production
    # observe now rejects this proof; the scoring loop must audit saved evidence.
    from ml.intraday import data
    with monkeypatch.context() as old_writer:
        old_writer.setattr(data, "synthetic_continuity_verified", lambda candle: True)
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
    revised = store.revisions(forecasts["MSFT"]["forecast_id"])
    assert revised[:1] == preserved["MSFT"] == [previous_synthetic]
    assert len(revised) == 2 and revised[-1]["status"] == "unscorable"
    assert revised[-1]["invalidates_revision_id"] == previous_synthetic["revision_id"]
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
    from ml.intraday import data
    from app.services import schwab,databento_market_data
    import dotenv
    monkeypatch.setattr(live_session,"local_symbols",lambda *args:("Atlas",("AAPL",)))
    atlas_manual_modules(monkeypatch)
    monkeypatch.setattr(dotenv,"load_dotenv",lambda *args:None)
    monkeypatch.setattr(data,"load_local_history",lambda *a,**k:object())
    def fail():
        raise RuntimeError("provider configuration fixture")
    monkeypatch.setattr(databento_market_data,"DatabentoMarketDataProvider",fail)
    monkeypatch.setattr(schwab,"SchwabSession",lambda:pytest.fail("broker constructed after failed setup"))
    with pytest.raises(RuntimeError,match="configuration fixture"):
        live_session.run_manual(repository=tmp_path,datastore=tmp_path,profile=tmp_path/"profile.json",
            watchlist=tmp_path/"watchlist.txt",config=live_session.ResearchConfig(),
            clock=lambda: "2026-10-15T18:00:00Z")
    status=json.loads((tmp_path/"intraday/manual-session.json").read_text())
    assert status["status"] == "FAILED" and status["stop_reason"] == "startup_failure"


def test_forecast_worker_failure_is_not_a_successful_manual_stop(tmp_path,monkeypatch):
    from ml.intraday import data
    from app.services import schwab,databento_market_data
    import dotenv
    monkeypatch.setattr(live_session,"local_symbols",lambda *args:("Atlas",("AAPL",)))
    account_execution = atlas_manual_modules(monkeypatch)
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
        watchlist=tmp_path/"watchlist.txt",config=live_session.ResearchConfig(),
        clock=lambda: "2026-10-15T18:00:00Z")
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
    from ml.intraday import data, exact_status
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
    delay=exact_status.retry_delay(error,now[0])
    assert delay==120
    monkeypatch.setattr(live_session,"local_symbols",lambda *args:("Atlas",("AAPL","AMZN")))
    account_execution = atlas_manual_modules(monkeypatch)
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
    """Default independent Scout execution; every operating boundary is injected."""
    import dotenv
    from ml.intraday import data
    from app.services import schwab, databento_market_data
    calls, executors = [], []
    monkeypatch.setattr(live_session,"local_symbols",lambda *args:("Scout",("DOCU",)))
    monkeypatch.setattr(dotenv,"load_dotenv",lambda *args:None)
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
            calls.append("preflight")
            assert not self.kwargs["manual_authority"]()
            return {"ready": True, "mode": "scout-local-broker-account",
                    "session_id": self.kwargs["session_id"], **(proof_changes or {})}
        def step(self, value):
            calls.append("step")
            assert value["pc"] == "Scout"
            assert self.kwargs["manual_authority"]()
            return {"kind":"execution_disposition","disposition":"no_purchasing_capacity","evidence":{}}
        def recover(self):
            calls.append("recover")
    module_fixture(monkeypatch, "ml.intraday.scout_local_execution", ScoutLocalAccountExecutor=Executor)
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
    block_remote_caller_imports(monkeypatch)
    return dict(repository=tmp_path,datastore=tmp_path,profile=tmp_path/"profile.json",
        watchlist=tmp_path/"watchlist.txt",config=live_session.ResearchConfig(),
        clock=lambda:"2026-10-08T17:17:00Z",sleep=stop),calls,executors


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
    assert proof["ready"] is True and proof["mode"] == "scout-local-broker-account"
    assert status["scout_admission"] == "broker-local"
    assert not {"writers", "authority_binding", "account_fingerprint", "readiness_receipt"} & set(proof)
    assert all(row["session_id"] == result["session_id"] and row["pc"] == "Scout" for row in logs)
    assert "Scout 15m manual session running" in capsys.readouterr().out
    prior_bytes=(session_root/"status.json").read_bytes()
    second=live_session.run_manual(**kwargs)
    assert second["session_id"] != result["session_id"]
    assert (session_root/"status.json").read_bytes() == prior_bytes


@pytest.mark.parametrize("wrapped", [False, True])
def test_market_context_failure_records_safe_os_cause_and_keeps_manual_control(tmp_path, monkeypatch, wrapped):
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)

    class Collector:
        def run(self, **unused):
            try:
                error = PermissionError(13, "https://provider.invalid/?token=PRIVATE_TOKEN", "PRIVATE_FILENAME")
                error.winerror = 5
                raise error
            except PermissionError as exc:
                if wrapped:
                    raise RuntimeError("PRIVATE_WRAPPER") from exc
                raise

    monkeypatch.setattr(live_session, "prepare_market_collection", lambda *a, **k: Collector())
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "FAILED"
    assert "step" not in calls and "recover" not in calls
    assert not executors[0].kwargs["manual_authority"]()
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["stop_reason"] == "worker_failure"
    assert status["broker_orders_automatically_cancelled"] is False
    failure = status["worker_errors"][0]
    assert failure["worker"] == "market_context"
    assert failure["error_type"] == ("RuntimeError" if wrapped else "PermissionError")
    chain = failure["diagnostics"]["exception_chain"]
    assert [entry["relation"] for entry in chain] == (["raised", "cause"] if wrapped else ["raised"])
    assert chain[-1]["type"] == "PermissionError"
    assert chain[-1]["errno"] == 13 and chain[-1]["winerror"] == 5
    assert chain[-1]["frames"][-1]["function"] == "run"
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    with store.connect() as db:
        saved = json.loads(db.execute("SELECT payload FROM events WHERE kind='market_context_worker_stopped'").fetchone()[0])
    assert saved["diagnostics"] == failure["diagnostics"]
    session_root = tmp_path / "intraday/sessions" / result["session_id"].replace(":", "-")
    serialized = json.dumps(saved) + json.dumps(status) + (session_root / "events.jsonl").read_text()
    assert all(secret not in serialized for secret in ["PRIVATE_TOKEN", "PRIVATE_FILENAME", "PRIVATE_WRAPPER"])


def test_market_context_queue_retry_is_own_session_evidence_not_worker_failure(tmp_path, monkeypatch):
    kwargs, calls, _ = scout_manual_fixture(tmp_path, monkeypatch)
    detail = {"reason": "queue_lock_contention", "error_type": "Timeout", "retry_after_seconds": 30.0}
    class Collector:
        def run(self, *, stopped, pause, on_retry, on_recovery=None):
            assert not stopped()
            on_retry(detail)
            assert not stopped()
    monkeypatch.setattr(live_session, "prepare_market_collection", lambda *a, **k: Collector())
    result = live_session.run_manual(**kwargs)
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert result["status"] == "STOPPED" and status["stop_reason"] == "manual_stop"
    assert status.get("worker_errors", []) == [] and "step" in calls
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    with store.connect() as db:
        rows = db.execute("SELECT cycle_id,payload FROM events WHERE kind='market_context_queue_retry'").fetchall()
        failures = db.execute("SELECT count(*) FROM events WHERE kind='market_context_worker_stopped'").fetchone()[0]
    assert rows == [(result["session_id"], json.dumps(detail, sort_keys=True, separators=(",", ":")))]
    assert failures == 0


@pytest.mark.parametrize("operation", ["execution", "recovery"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_scout_retry_records_real_sqlite_cause_without_changing_retry_or_forecast(
        tmp_path, monkeypatch, operation, wrapped):
    """Inject real private-database contention through both retry handlers."""
    import sqlite3
    from contextlib import closing

    kwargs, _, executors = scout_manual_fixture(tmp_path, monkeypatch)
    executor_type = sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor
    original_step, original_recover = executor_type.step, executor_type.recover
    now = [instant("2026-10-08T17:17:00Z")]
    started, attempts, supplied = now[0], [], []

    def fail_locked_write():
        private_diagnostic_value = "PRIVATE_DIAGNOSTIC_VALUE"
        path = tmp_path / "independent-contention.sqlite3"
        with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE fixture(value TEXT)")
            writer.execute("BEGIN IMMEDIATE")
            with closing(sqlite3.connect(path, timeout=0, isolation_level=None)) as blocked:
                try:
                    blocked.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if wrapped:
                        raise RuntimeError("fixture operation failed") from exc
                    raise
            pytest.fail("Expected real SQLite contention")

    def step(self, value):
        attempts.append(("execution", (now[0] - started).total_seconds()))
        supplied.append(json.loads(json.dumps(value)))
        if operation == "execution" and len(supplied) == 1:
            fail_locked_write()
        return original_step(self, value)

    def recover(self):
        previous = [item for item in attempts if item[0] == "recovery"]
        attempts.append(("recovery", (now[0] - started).total_seconds()))
        if operation == "recovery" and not previous:
            fail_locked_write()
        return original_recover(self)

    def sleep(seconds):
        if (now[0] - started).total_seconds() >= 4:
            raise KeyboardInterrupt()
        now[0] += timedelta(seconds=seconds)

    monkeypatch.setattr(executor_type, "step", step)
    monkeypatch.setattr(executor_type, "recover", recover)
    kwargs.update(clock=lambda: now[0], sleep=sleep)
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED" and len(executors) == 1
    if operation == "execution":
        assert attempts == [("execution", 0), ("execution", 3), ("recovery", 3)]
        assert supplied[0] == supplied[1]
    else:
        assert attempts == [("execution", 0), ("recovery", 0), ("recovery", 3)]
        assert len(supplied) == 1

    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    with store.connect() as db:
        rows = db.execute("SELECT kind,symbol,payload FROM events WHERE kind IN "
            "('execution_retry','order_recovery_retry')").fetchall()
        assert len(rows) == 1
        kind, symbol, raw = rows[0]
        payload = json.loads(raw)
        assert kind == ("execution_retry" if operation == "execution" else "order_recovery_retry")
        assert symbol == ("DOCU" if operation == "execution" else None)
        if operation == "execution":
            assert payload["forecast_id"] == supplied[0]["forecast_id"]
        assert payload["error_type"] == ("RuntimeError" if wrapped else "OperationalError")
        assert payload["message"] == ("fixture operation failed" if wrapped else "database is locked")
        assert payload["retry_after_seconds"] == 3
        chain = payload["diagnostics"]["exception_chain"]
        assert [entry["relation"] for entry in chain] == (["raised", "cause"] if wrapped else ["raised"])
        assert chain[-1]["type"] == "OperationalError"
        assert chain[-1]["sqlite_errorcode"] == sqlite3.SQLITE_BUSY
        assert chain[-1]["sqlite_errorname"] == "SQLITE_BUSY"
        assert chain[-1]["frames"][-1]["function"] == "fail_locked_write"
        assert chain[-1]["frames"][-1]["file"] == "test_intraday_live_session.py"
        assert chain[-1]["frames"][-1]["line"] > 0
        assert not payload["diagnostics"]["chain_truncated"]
        diagnostic_text = json.dumps(payload["diagnostics"])
        assert str(tmp_path) not in diagnostic_text and "PRIVATE_DIAGNOSTIC_VALUE" not in diagnostic_text
        wait = db.execute("SELECT next_attempt_at FROM intraday_live_retry WHERE scope='broker'").fetchone()[0]
        assert instant(wait) == started + timedelta(seconds=3)
        queued = db.execute("SELECT forecast_id,status FROM intraday_live_queue").fetchall()
        assert queued == [(supplied[0]["forecast_id"], "no_purchasing_capacity")]
    assert store.forecasts(pc="Scout") == [supplied[0]]


# Historical receipt authentication, writer compatibility and expiry checks belong
# to the retained remote API. The independent caller validates its executor's
# local mode, exact manual session and literal success before history or workers.
@pytest.mark.parametrize("changes", [
    {"ready":False}, {"ready":None}, {"ready":1}, {"ready":"true"},
    {"mode":"atlas-service"}, {"mode":"atlas-local-broker-account"},
    {"mode":""}, {"mode":None}, {"session_id":"another-session"}, {"session_id":None},
])
def test_scout_rejects_unbound_preflight_before_history_provider_or_training(tmp_path,monkeypatch,changes):
    kwargs,calls,_=scout_manual_fixture(tmp_path,monkeypatch,proof_changes=changes)
    with pytest.raises(ContractError, match="local broker-account preflight did not pass"):
        live_session.run_manual(**kwargs)
    assert calls == ["preflight"]
    status=json.loads((tmp_path/"intraday/manual-session.json").read_text())
    assert status["status"] == "FAILED" and status["stop_reason"] == "startup_failure"
    assert status["broker_orders_automatically_cancelled"] is False
    assert status["schema_version"] == live_session.SCOUT_VERSION


def test_scout_remote_positive_dictionary_does_not_substitute_for_local_preflight(tmp_path, monkeypatch):
    kwargs,calls,_=scout_manual_fixture(tmp_path,monkeypatch)
    import sys
    executor_type=sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor
    def unbound(self):
        return dict(ready=True, authenticated=True, protocol_compatible=True, participant_id="pc-new",
                    stream="scout-15m", session_id=self.kwargs["session_id"], protocol="intraday-shared-admission-v1")
    monkeypatch.setattr(executor_type,"preflight",unbound)
    with pytest.raises(ContractError,match="local broker-account preflight did not pass"):
        live_session.run_manual(**kwargs)
    assert not calls
    assert json.loads((tmp_path/"intraday/manual-session.json").read_text())["status"] == "FAILED"


@pytest.mark.parametrize("proof", [None, True, []])
def test_scout_nonmapping_preflight_fails_before_history_provider_or_workers(tmp_path, monkeypatch, proof):
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    executor_type = sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor
    monkeypatch.setattr(executor_type, "preflight", lambda self: proof)
    with pytest.raises(ContractError, match="local broker-account preflight did not pass"):
        live_session.run_manual(**kwargs)
    assert calls == [] and len(executors) == 1
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["status"] == "FAILED" and status["stop_reason"] == "startup_failure"
    assert status["broker_orders_automatically_cancelled"] is False
    assert not executors[0].kwargs["manual_authority"]()


def test_scout_local_preflight_exception_stays_startup_failure_without_fallback(tmp_path,monkeypatch):
    kwargs,calls,executors=scout_manual_fixture(tmp_path,monkeypatch)
    import sys
    executor_type=sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor
    def fail(self):
        raise ConnectionError("local broker unavailable fixture")
    monkeypatch.setattr(executor_type,"preflight",fail)
    with pytest.raises(ConnectionError,match="local broker unavailable fixture"):
        live_session.run_manual(**kwargs)
    assert calls == []
    status=json.loads((tmp_path/"intraday/manual-session.json").read_text())
    assert status["status"] == "FAILED"
    assert status["error_type"] == "ConnectionError"
    assert not executors[0].kwargs["manual_authority"]()


@pytest.mark.parametrize("stage", ["broker_session", "broker_preflight"])
@pytest.mark.parametrize("wrapped", [False, True])
def test_scout_startup_failure_retains_cause_without_diagnostic_secrets(tmp_path, monkeypatch, stage, wrapped):
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    from app.services import schwab

    def denied_operation():
        try:
            error = PermissionError(13, "PRIVATE_STARTUP_TOKEN", "PRIVATE_STARTUP_PATH")
            error.winerror = 5
            raise error
        except PermissionError as exc:
            if wrapped:
                raise RuntimeError("PRIVATE_STARTUP_WRAPPER") from exc
            raise

    if stage == "broker_session":
        monkeypatch.setattr(schwab, "SchwabSession", denied_operation)
    else:
        executor_type = sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor
        monkeypatch.setattr(executor_type, "preflight", lambda self: denied_operation())
    with pytest.raises(RuntimeError if wrapped else PermissionError):
        live_session.run_manual(**kwargs)
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["status"] == "FAILED" and status["stop_reason"] == "startup_failure"
    assert status["startup_stage"] == stage
    assert status["broker_orders_automatically_cancelled"] is False
    assert calls == []
    assert len(executors) == (0 if stage == "broker_session" else 1)
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    with store.connect() as db:
        rows = db.execute("SELECT payload FROM events WHERE kind='manual_startup_stopped'").fetchall()
    assert len(rows) == 1
    event = json.loads(rows[0][0])
    assert event["startup_stage"] == stage
    assert event["diagnostics"] == status["diagnostics"]
    chain = status["diagnostics"]["exception_chain"]
    assert [x["relation"] for x in chain] == (["raised", "cause"] if wrapped else ["raised"])
    assert chain[-1]["type"] == "PermissionError"
    assert chain[-1]["winerror"] == 5 and chain[-1]["errno"] == 13
    assert chain[-1]["frames"][-1]["function"] == "denied_operation"
    diagnostic_text = json.dumps(status["diagnostics"])
    assert "PRIVATE_STARTUP" not in diagnostic_text and str(tmp_path) not in diagnostic_text


@pytest.mark.parametrize("started,expected", [
    ("2026-10-14T22:31:00Z", "2026-10-15T00:00:00Z"),
    ("2026-12-14T23:31:00Z", "2026-12-15T01:00:00Z"),
    ("2026-10-15T06:59:59Z", "2026-10-15T00:00:00Z"),
    ("2026-10-15T07:00:00Z", "2026-10-16T00:00:00Z"),
    ("2026-11-01T08:30:00Z", "2026-11-02T01:00:00Z"),
    ("2026-03-08T09:30:00Z", "2026-03-09T00:00:00Z"),
])
def test_automatic_stop_deadline_uses_latched_pacific_date_and_dst(started, expected):
    assert live_session.automatic_stop_deadline(instant(started)) == instant(expected)


@pytest.mark.parametrize("now", ["2026-10-15T00:00:00Z", "2026-10-15T00:00:01Z"])
def test_scout_manual_launch_at_or_after_cutoff_never_opens_operating_boundaries(tmp_path, monkeypatch, now):
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    kwargs["clock"] = lambda: now
    monkeypatch.setattr(live_session.threading, "Thread",
        lambda **kwargs: pytest.fail("after-cutoff launch started a worker"))
    from app.services import schwab
    monkeypatch.setattr(schwab, "SchwabSession",
        lambda: pytest.fail("after-cutoff launch constructed a broker session"))
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED"
    assert calls == executors == []
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["automatic_stop_at"] == "2026-10-15T00:00:00+00:00"
    assert status["stop_reason"] == "automatic_17_00_pacific"
    assert status["broker_orders_automatically_cancelled"] is False


def test_scout_cutoff_during_execution_revokes_final_authority_and_fences_batch(tmp_path, monkeypatch):
    """Fake broker work crosses cutoff; neither another symbol nor recovery starts."""
    import sys
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    now = [instant("2026-10-14T23:59:59Z")]
    cutoff = instant("2026-10-15T00:00:00Z")
    kwargs["clock"] = lambda: now[0]
    kwargs["sleep"] = lambda seconds: pytest.fail("execution continued sleeping after cutoff")
    published, stepped = [], []

    def publish(runner, **options):
        queue = options["queue"]
        for symbol in ("DOCU", "DBX"):
            value = runner.store.save_forecast(forecast(mode="live", pc="Scout", symbol=symbol,
                cycle_id=queue.session_id, reference_at="2026-10-14T23:15:00Z",
                target_open="2026-10-14T23:45:00Z", target_close=cutoff.isoformat(),
                data_through="2026-10-14T23:30:00Z", information_available_at="2026-10-14T23:30:01Z",
                issued_at=now[0].isoformat()))
            queue.enqueue(value)
            published.append(value)

    def step(self, value):
        assert self.kwargs["manual_authority"]()
        assert self.kwargs["stopped"]() is False
        stepped.append(value["symbol"])
        now[0] = cutoff
        # This is the same callback the prepared broker's final gate receives.
        assert self.kwargs["manual_authority"]() is False
        assert self.kwargs["stopped"]() is True
        # The stop is sticky even if the wall clock subsequently moves back.
        now[0] -= timedelta(seconds=1)
        assert self.kwargs["manual_authority"]() is False
        assert self.kwargs["stopped"]() is True
        return {"kind": "execution_disposition", "disposition": "not_submitted", "evidence": {}}

    executor_type = sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor
    monkeypatch.setattr(executor_type, "step", step)
    monkeypatch.setattr(executor_type, "recover",
        lambda self: pytest.fail("order recovery began after cutoff"))
    monkeypatch.setattr(live_session, "forecasting_loop", publish)
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED"
    assert len(stepped) == 1 and len(executors) == 1
    assert {value["symbol"] for value in published} == {"DOCU", "DBX"}
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    assert store.forecasts(pc="Scout") == published
    with store.connect() as db:
        statuses = dict(db.execute("SELECT symbol,status FROM intraday_live_queue"))
    assert statuses[stepped[0]] == "not_submitted"
    assert statuses[next(symbol for symbol in statuses if symbol != stepped[0])] == "pending"
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["stop_reason"] == "automatic_17_00_pacific"
    assert status["broker_orders_automatically_cancelled"] is False


def test_scout_cutoff_drains_all_workers_and_keeps_original_deadline_after_midnight(tmp_path, monkeypatch):
    """Deterministic thread fixtures finish only when the shutdown path joins them."""
    from ml.intraday import minute_feed
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    now = [instant("2026-10-14T23:59:59Z")]
    kwargs["clock"] = lambda: now[0]
    threads, finished = [], []

    class DrainingThread:
        def __init__(self, *, target, name, **kwargs):
            self.target, self.name, self.alive = target, name, False
            threads.append(self)
        def start(self):
            self.alive = True
        def is_alive(self):
            return self.alive
        def join(self, **kwargs):
            if self.alive:
                status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
                assert status["status"] == "STOPPING"
                assert status["stop_reason"] == "automatic_17_00_pacific"
                self.target()
                self.alive = False
                finished.append(self.name)

    def finish_forecast(runner, **options):
        assert options["stopped"]() is True
        assert runner.stopped() is True

    def finish_scoring(store, **options):
        assert options["stopped"]() is True

    class DrainingFeed:
        history_source = None
        scoring_history_source = None
        def __init__(self, **kwargs):
            self.on_closed = kwargs["on_closed"]
        def run(self, **kwargs):
            # Closing order authority does not discard the feed's last native
            # interval; its existing acquisition finishes before final stop.
            assert kwargs["stopped"]() is False
            assert executors[0].kwargs["manual_authority"]() is False
            self.on_closed({"status": "closed", "close_at": "2026-10-15T00:00:00+00:00",
                "all_queued_native_windows_saved": True, "later_sessions_automatic": False})

    def cross_midnight(seconds):
        # The session began on October 14 Pacific; crossing midnight must not
        # advance the already latched deadline to the following evening.
        now[0] = instant("2026-10-15T07:00:00Z")

    kwargs["sleep"] = cross_midnight
    monkeypatch.setattr(live_session.threading, "Thread", DrainingThread)
    monkeypatch.setattr(live_session, "forecasting_loop", finish_forecast)
    monkeypatch.setattr(live_session, "scoring_loop", finish_scoring)
    monkeypatch.setattr(minute_feed, "QuarterHourMinuteFeed", DrainingFeed)
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED"
    assert len(threads) == 3 and all(not thread.alive for thread in threads)
    assert finished == ["intraday-manual-forecast", "intraday-manual-scoring", "intraday-manual-minute-data"]
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["automatic_stop_at"] == "2026-10-15T00:00:00+00:00"
    assert status["stop_reason"] == "automatic_17_00_pacific"
    assert status["worker_errors"] == []
    assert not executors[0].kwargs["manual_authority"]()
    log_path = tmp_path / "intraday/sessions" / result["session_id"].replace(":", "-") / "events.jsonl"
    logs = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [row["status"] for row in logs if row["event"] == "session_status"] == [
        "STARTING", "RUNNING", "STOPPING", "STOPPED"]
    completed = [row for row in logs if row["event"] == "minute_data_day_complete"]
    assert len(completed) == 1 and completed[0]["all_queued_native_windows_saved"] is True


def test_scout_preflight_crossing_cutoff_is_a_scheduled_stop_before_provider_or_workers(tmp_path, monkeypatch):
    import sys
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    now = [instant("2026-10-14T23:59:59Z")]
    kwargs["clock"] = lambda: now[0]

    def cross_cutoff(self):
        calls.append("preflight")
        now[0] = instant("2026-10-15T00:00:00Z")
        self.kwargs["stopped"]()
        pytest.fail("startup stop callback did not interrupt preflight")

    executor_type = sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor
    monkeypatch.setattr(executor_type, "preflight", cross_cutoff)
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED" and calls == ["preflight"]
    assert len(executors) == 1
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["stop_reason"] == "automatic_17_00_pacific"
    assert status["broker_orders_automatically_cancelled"] is False


def test_real_scout_preflight_failure_after_cutoff_remains_a_startup_failure(tmp_path, monkeypatch):
    import sys
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    now = [instant("2026-10-14T23:59:59Z")]
    kwargs["clock"] = lambda: now[0]

    def fail(self):
        calls.append("preflight")
        now[0] = instant("2026-10-15T00:00:00Z")
        raise RuntimeError("preflight fixture failure")

    executor_type = sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor
    monkeypatch.setattr(executor_type, "preflight", fail)
    with pytest.raises(RuntimeError, match="preflight fixture failure"):
        live_session.run_manual(**kwargs)
    assert calls == ["preflight"] and len(executors) == 1
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["status"] == "FAILED" and status["stop_reason"] == "startup_failure"
    assert status["error_type"] == "RuntimeError"


def test_scout_worker_failure_keeps_failure_status_when_it_crosses_cutoff(tmp_path, monkeypatch):
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    now = [instant("2026-10-14T23:59:59Z")]
    kwargs["clock"] = lambda: now[0]

    def fail(runner, **options):
        now[0] = instant("2026-10-15T00:00:00Z")
        assert options["stopped"]() is True
        raise RuntimeError("forecast fixture failure during shutdown")

    monkeypatch.setattr(live_session, "forecasting_loop", fail)
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "FAILED"
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["stop_reason"] == "worker_failure"
    assert status["worker_errors"][0]["worker"] == "forecast"
    assert status["broker_orders_automatically_cancelled"] is False
    assert "step" not in calls and "recover" not in calls


def test_scout_automatic_cutoff_does_not_change_atlas_manual_stop(tmp_path, monkeypatch):
    import dotenv
    from app.services import schwab, databento_market_data
    from ml.intraday import data
    monkeypatch.setattr(live_session, "local_symbols", lambda *args: ("Atlas", ("AAPL",)))
    account_execution = atlas_manual_modules(monkeypatch)
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(data, "load_local_history", lambda *args, **kwargs: object())
    monkeypatch.setattr(databento_market_data, "DatabentoMarketDataProvider",
        lambda: SimpleNamespace(dataset="EQUS.MINI"))
    monkeypatch.setattr(schwab, "SchwabSession", lambda: object())
    recoveries = []
    monkeypatch.setattr(account_execution, "IntradayAccountExecutor", lambda **kwargs: SimpleNamespace(
        step=lambda value: pytest.fail("fixture issued an order"), recover=lambda: recoveries.append(True)))
    monkeypatch.setattr(live_session, "forecasting_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(live_session, "scoring_loop", lambda *args, **kwargs: None)

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
        def is_alive(self):
            return False
        def join(self, **kwargs):
            pass

    def stop(seconds):
        raise KeyboardInterrupt()

    monkeypatch.setattr(live_session.threading, "Thread", InlineThread)
    result = live_session.run_manual(repository=tmp_path, datastore=tmp_path, profile=tmp_path / "profile.json",
        watchlist=tmp_path / "watchlist.txt", config=live_session.ResearchConfig(),
        clock=lambda: "2026-10-15T00:01:00Z", sleep=stop)
    assert result["status"] == "STOPPED" and recoveries == [True]
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["automatic_stop_at"] is None
    assert status["stop_reason"] == "manual_stop"


def test_ctrl_c_during_automatic_data_drain_still_joins_workers_and_records_manual_stop(tmp_path, monkeypatch):
    from ml.intraday import minute_feed
    kwargs, calls, executors = scout_manual_fixture(tmp_path, monkeypatch)
    now = [instant("2026-10-14T23:59:59Z")]
    kwargs["clock"] = lambda: now[0]
    interrupted, threads, finished = [], [], []

    class InterruptingThread:
        def __init__(self, *, target, name, **kwargs):
            self.target, self.name, self.alive = target, name, False
            threads.append(self)
        def start(self):
            self.alive = True
        def is_alive(self):
            return self.alive
        def join(self, **kwargs):
            if not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt()
            if self.alive:
                self.target()
                self.alive = False
                finished.append(self.name)

    class UnfinishedFeed:
        history_source = None
        scoring_history_source = None
        def __init__(self, **kwargs):
            pass
        def run(self, **kwargs):
            # The explicit Ctrl-C interrupts otherwise automatic late data
            # acquisition. It must not fabricate completion for missing work.
            assert kwargs["stopped"]() is True
            assert executors[0].kwargs["manual_authority"]() is False

    def finish_forecast(runner, **options):
        assert runner.stopped() is True and options["stopped"]() is True

    def finish_scoring(store, **options):
        assert options["stopped"]() is True

    def reach_cutoff(seconds):
        now[0] = instant("2026-10-15T00:00:00Z")

    kwargs["sleep"] = reach_cutoff
    monkeypatch.setattr(live_session.threading, "Thread", InterruptingThread)
    monkeypatch.setattr(live_session, "forecasting_loop", finish_forecast)
    monkeypatch.setattr(live_session, "scoring_loop", finish_scoring)
    monkeypatch.setattr(minute_feed, "QuarterHourMinuteFeed", UnfinishedFeed)
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED" and interrupted == [True]
    assert len(threads) == 3 and all(not thread.alive for thread in threads)
    assert finished == ["intraday-manual-forecast", "intraday-manual-scoring", "intraday-manual-minute-data"]
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["status"] == "STOPPED" and status["stop_reason"] == "manual_stop"
    assert status["broker_orders_automatically_cancelled"] is False and status["worker_errors"] == []
    log_path = tmp_path / "intraday/sessions" / result["session_id"].replace(":", "-") / "events.jsonl"
    logs = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert any(row["event"] == "automatic_stop_requested" for row in logs)
    assert any(row["event"] == "manual_stop_requested" for row in logs)
    assert not any(row["event"] == "minute_data_day_complete" for row in logs)


@pytest.mark.parametrize("scope,rotates", [
    ("all", True), ("reordered", True), ("subset", False),
    ("duplicate", False), ("foreign", False), ("reused", False),
])
def test_recovery_boundary_requires_exact_attempt_scope(tmp_path, monkeypatch, scope, rotates):
    symbols = tuple("TEST" + str(i) for i in range(11))
    store = ShadowStore(tmp_path / "boundary.sqlite3")
    reference = instant("2026-10-12T17:00:00Z")
    cycle = dict(pc="Scout", symbols=list(symbols), mode="live", experiment="manual",
                 reference_at=reference.isoformat(), started_at=reference.isoformat(), status="partial_failure")
    store.begin_cycle("old", cycle)
    retry = list(symbols)
    if scope == "reordered": retry.reverse()
    if scope == "subset": retry.pop()
    if scope == "duplicate": retry[-1] = retry[0]
    if scope == "foreign": retry[-1] = "FOREIGN"
    monkeypatch.setattr(live_session, "_pending_cycle_passes", lambda *a, **k: [("old", cycle, retry)])
    now, calls, boundaries, pauses = [instant("2026-10-12T17:31:00Z")], [], [], []
    def run(inputs, **kw):
        calls.append(kw)
        now[0] += timedelta(minutes=37)  # Another quarter is due on return.
        return {"cycle_id": "old", "recovered_existing": scope == "reused"}
    runner = SimpleNamespace(store=store, symbols=symbols,
        config=SimpleNamespace(publication_offset_seconds=30), run_cycle=run)
    live_session.forecasting_loop(runner, histories={}, provider=None, journal=None,
        queue=SimpleNamespace(session_id="manual", pc="Scout", enqueue=lambda _: None),
        stopped=lambda: bool(calls), clock=lambda: now[0], pause=pauses.append,
        history_source=lambda _: object(), on_iteration=boundaries.append, recovery_first=True)
    assert len(calls) == 1 and calls[0]["retry_symbols"] == retry
    assert not calls[0]["allow_execution"]
    assert len(boundaries) == int(rotates)
    assert pauses == ([] if rotates else [1])


def test_same_quarter_full_recovery_remains_due_with_fresh_priority(tmp_path):
    symbols = tuple("TEST" + str(i) for i in range(11))
    store = ShadowStore(tmp_path / "same-quarter.sqlite3")
    reference = instant("2026-10-12T17:00:00Z")
    failure = dict(type="AcquisitionPending", recorded_at="2026-10-12T17:15:30Z",
                   retryable=True, reason="real_input_missing", retry_at="2026-10-12T17:15:31Z")
    store.begin_cycle("old", dict(pc="Scout", symbols=list(symbols), mode="live", experiment="manual",
        reference_at=reference.isoformat(), started_at=reference.isoformat(), status="partial_failure",
        failures={s: dict(failure) for s in symbols}))
    calls, boundaries = [], []
    runner = SimpleNamespace(store=store, symbols=symbols, config=SimpleNamespace(publication_offset_seconds=30),
        run_cycle=lambda inputs, **kw: calls.append(kw) or {"cycle_id": "old"})
    live_session.forecasting_loop(runner, histories={}, provider=None, journal=None,
        queue=SimpleNamespace(session_id="manual", pc="Scout", enqueue=lambda _: None),
        stopped=lambda: bool(calls), clock=lambda: "2026-10-12T17:16:00Z",
        pause=lambda _: pytest.fail("Due recovery must not wait for the next quarter"),
        history_source=lambda _: object(), on_iteration=boundaries.append, recovery_first=False)
    assert len(calls) == len(boundaries) == 1
    assert calls[0]["retry_symbols"] == list(symbols) and calls[0]["allow_execution"]
