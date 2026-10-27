"""Private-store/fake-provider proofs for Scout's complete iteration handoff."""
from dataclasses import asdict, replace
from datetime import timedelta
import json
import sys
from types import SimpleNamespace

import pytest

from ml.intraday import live_session
from ml.intraday.iteration_boundary import capture_checkpoint, inspect_cycle, validate_resume
from ml.intraday.prediction import ContractError, digest, instant
from ml.intraday.runtime import _jsonable
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast
from tests.test_intraday_live_session import module_fixture

SYMBOLS = tuple("TEST" + str(i) for i in range(11))
NOW = "2026-10-08T17:17:00Z"


class Lifecycle:
    session_id = "manual-scout-15m:iteration-fixture"
    started_at = NOW
    wake_at = "2026-10-08T11:00:00Z"
    deadline = "2026-10-09T00:00:00Z"
    drain_timeout_seconds = 0.1
    def __init__(self, generation=1, previous_checkpoint=None):
        self.generation, self.previous_checkpoint = generation, previous_checkpoint
        self.release = {"manifest_sha256": str(generation) * 64}
        self.reason, self.phases, self.checkpoints = None, [], []
        self.ready_count = 0
    def stop_reason(self):
        return self.reason
    def ready(self):
        self.ready_count += 1
    def phase(self, name, **detail):
        self.phases.append((name, detail))
    def checkpoint(self, value):
        self.checkpoints.append(value)


def seed_cycle(store, *, lifecycle, config, reference="2026-10-08T17:00:00Z", failure=None,
               missing=None, queue=None, status=None, attempt_count=1):
    reference = instant(reference)
    identity = "cycle-" + str(lifecycle.generation)
    failures = {} if failure is None else {SYMBOLS[-1]: failure}
    value = dict(pc="Scout", symbols=list(SYMBOLS), reference_at=reference.isoformat(), mode="live",
                 experiment=lifecycle.session_id, status=status or ("partial_failure" if failures else "completed"),
                 started_at=NOW, completed_at=NOW, attempt_count=attempt_count, configuration_hash=digest(_jsonable(asdict(config))), failures=failures)
    store.begin_cycle(identity, value)
    for symbol in SYMBOLS:
        if symbol in failures or symbol == missing:
            continue
        f = store.save_forecast(forecast(pc="Scout", symbol=symbol, mode="live", cycle_id=identity,
            reference_at=reference.isoformat(), data_through=(reference + timedelta(minutes=15)).isoformat(),
            information_available_at=(reference + timedelta(minutes=15, seconds=3)).isoformat(),
            issued_at=(reference + timedelta(minutes=16, seconds=2)).isoformat(),
            target_open=(reference + timedelta(minutes=30)).isoformat(),
            target_close=(reference + timedelta(minutes=45)).isoformat()))
        if queue:
            queue.enqueue(f)
    return identity


def setup_session(tmp_path, monkeypatch, *, lifecycle=None, failure=None, missing=None):
    import dotenv
    from app.services import schwab
    from ml.intraday import data, minute_feed
    lifecycle = lifecycle or Lifecycle()
    calls, executors, threads = [], [], []
    config = live_session.ResearchConfig()
    now = [instant(NOW) + timedelta(minutes=15 * (lifecycle.generation - 1))]
    monkeypatch.setattr(live_session, "local_symbols", lambda *a: ("Scout", SYMBOLS))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a: None)
    monkeypatch.setattr(data, "load_local_history", lambda *a, **k: object())
    monkeypatch.setattr(live_session, "prepare_minute_provider", lambda *a, **k: object())
    monkeypatch.setattr(live_session, "prepare_market_collection", lambda *a, **k: None)
    monkeypatch.setattr(schwab, "SchwabSession", lambda: SimpleNamespace(offline=True))
    class Executor:
        evidence_paths = {}
        def __init__(self, **kw):
            assert kw["broker"].offline and kw["local_symbols"] == SYMBOLS
            self.kw = kw
            executors.append(self)
        def preflight(self):
            assert not self.kw["manual_authority"]()
            calls.append("fresh_broker_preflight")
            return dict(ready=True, mode="scout-local-broker-account", session_id=self.kw["session_id"])
        def step(self, value):
            assert self.kw["manual_authority"]() and lifecycle.ready_count == 1
            calls.append(("step", value["forecast_id"]))
            return dict(kind="execution_disposition", disposition="submitted", evidence={})
        def recover(self):
            calls.append("recover_own_orders")
    module_fixture(monkeypatch, "ml.intraday.scout_local_execution", ScoutLocalAccountExecutor=Executor)
    class Feed:
        history_source = scoring_history_source = None
        def __init__(self, **kw):
            assert kw["session_id"] == lifecycle.session_id
        def run(self, **kw):
            pass
    monkeypatch.setattr(minute_feed, "QuarterHourMinuteFeed", Feed)
    class InlineThread:
        def __init__(self, *, target, name, **kw):
            self.target, self.name = target, name
            threads.append(self)
        def start(self):
            self.target()
        def is_alive(self):
            return False
        def join(self, **kw):
            pass
    monkeypatch.setattr(live_session.threading, "Thread", InlineThread)
    def publish(runner, **kw):
        identity = seed_cycle(runner.store, lifecycle=lifecycle, config=config,
            reference=live_session.latest_completed_reference(now[0], publication_offset_seconds=30),
            failure=failure, missing=missing, queue=kw["queue"])
        kw["on_iteration"]({"cycle_id": identity})
    monkeypatch.setattr(live_session, "forecasting_loop", publish)
    monkeypatch.setattr(live_session, "scoring_loop", lambda *a, **k: None)
    kwargs = dict(repository=tmp_path, datastore=tmp_path, profile=tmp_path / "profile.json",
                  watchlist=tmp_path / "watchlist.txt", config=config, lifecycle=lifecycle,
                  clock=lambda: now[0], sleep=lambda seconds: pytest.fail("Fixture should reach boundary without polling"))
    return kwargs, lifecycle, calls, executors, threads


def test_all_eleven_symbols_execute_then_same_run_rotates_and_keeps_originals(tmp_path, monkeypatch):
    kwargs, first, calls, _, _ = setup_session(tmp_path, monkeypatch)
    assert live_session.run_manual(**kwargs)["status"] == "PLANNED_RESTART"
    assert len([c for c in calls if isinstance(c, tuple)]) == 11
    checkpoint = first.checkpoints[0]
    assert checkpoint["owned_workers_drained"] and not checkpoint["pending_queue"]
    original = ShadowStore(tmp_path / "intraday/shadow.sqlite3").forecasts()
    next_lifecycle = Lifecycle(2, checkpoint)
    kwargs, second, calls, _, _ = setup_session(tmp_path, monkeypatch, lifecycle=next_lifecycle)
    result = live_session.run_manual(**kwargs)
    assert result == {"session_id": first.session_id, "status": "PLANNED_RESTART",
                      "stop_reason": "planned_iteration_restart", "generation": 2}
    assert len([c for c in calls if isinstance(c, tuple)]) == 11
    assert calls.count("fresh_broker_preflight") == 1
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    assert store.forecasts()[:11] == original
    assert len(store.forecasts()) == 22
    assert len(second.checkpoints[0]["completed_queue"]) == 22
    session = tmp_path / "intraday/sessions" / first.session_id.replace(":", "-")
    assert json.loads((session / "generation-000001.json").read_text())["loaded_release"] == first.release
    assert json.loads((session / "generation-000002.json").read_text())["started_at"] == instant(first.started_at).isoformat()


@pytest.mark.parametrize("retryable", [False, True])
def test_partial_attempt_keeps_truthful_failure_and_durable_retry(tmp_path, monkeypatch, retryable):
    failure = dict(type="AcquisitionPending" if retryable else "ContractError", recorded_at=NOW, message="saved fixture cause")
    if retryable:
        failure.update(retryable=True, reason="real_input_missing", retry_at="2026-10-08T17:20:00Z")
    kwargs, lifecycle, calls, _, _ = setup_session(tmp_path, monkeypatch, failure=failure)
    assert live_session.run_manual(**kwargs)["status"] == "PLANNED_RESTART"
    checkpoint = lifecycle.checkpoints[0]
    assert checkpoint["cycles"][0]["status"] == "partial_failure"
    assert checkpoint["cycles"][0]["failures"] == {SYMBOLS[-1]: failure}
    assert len([c for c in calls if isinstance(c, tuple)]) == 10
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    with store.connect() as db:
        assert json.loads(db.execute("SELECT payload FROM cycles").fetchone()[0])["status"] == "partial_failure"


def test_missing_symbol_is_failed_not_a_restart_boundary(tmp_path, monkeypatch):
    kwargs, lifecycle, _, _, _ = setup_session(tmp_path, monkeypatch, missing=SYMBOLS[-1])
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "FAILED" and not lifecycle.checkpoints
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert "without a durable disposition" in status["worker_errors"][0]["message"]


@pytest.mark.parametrize("reason", ["manual_stop", "session_end", "controller_failure"])
@pytest.mark.parametrize("stage", ["ready", "execution", "draining", "checkpoint"])
def test_stop_wins_every_effect_and_handoff_stage(tmp_path, monkeypatch, reason, stage):
    kwargs, lifecycle, calls, executors, _ = setup_session(tmp_path, monkeypatch)
    if stage == "ready":
        def ready():
            lifecycle.ready_count += 1
            lifecycle.reason = reason
        lifecycle.ready = ready
    elif stage == "execution":
        original = sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor.step
        def step(self, value):
            result = original(self, value)
            lifecycle.reason = reason
            return result
        monkeypatch.setattr(sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor, "step", step)
    elif stage == "draining":
        original = lifecycle.phase
        def phase(name, **detail):
            original(name, **detail)
            if name == "draining":
                lifecycle.reason = reason
        lifecycle.phase = phase
    else:
        def checkpoint(value):
            lifecycle.reason = reason
            lifecycle.checkpoints.append(value)
        lifecycle.checkpoint = checkpoint
    result = live_session.run_manual(**kwargs)
    assert result["status"] != "PLANNED_RESTART" and result["stop_reason"] == reason
    assert not executors[0].kw["manual_authority"]()
    if stage == "ready":
        assert not any(isinstance(c, tuple) for c in calls)
    if stage == "execution":
        assert len([c for c in calls if isinstance(c, tuple)]) == 1
    if stage != "checkpoint":
        assert not lifecycle.checkpoints


def test_drain_timeout_is_failure_preserving_pending_work(tmp_path, monkeypatch):
    kwargs, lifecycle, calls, _, threads = setup_session(tmp_path, monkeypatch)
    lifecycle.drain_timeout_seconds = 0
    original = live_session.threading.Thread.is_alive
    monkeypatch.setattr(live_session.threading.Thread, "is_alive", lambda self: self.name.endswith("scoring"))
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "FAILED" and not lifecycle.checkpoints
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert status["worker_errors"][-1]["worker"] == "drain"
    assert len(ShadowStore(tmp_path / "intraday/shadow.sqlite3").forecasts()) == 11


def test_unknown_send_is_completed_for_submission_but_other_queue_work_stays_deferred(tmp_path, monkeypatch):
    kwargs, lifecycle, _, _, _ = setup_session(tmp_path, monkeypatch)
    def unknown(self, value):
        assert self.kw["manual_authority"]()
        return dict(kind="execution_disposition", disposition="submission_recovery_required", evidence={"retry_after_seconds": 30})
    monkeypatch.setattr(sys.modules["ml.intraday.scout_local_execution"].ScoutLocalAccountExecutor, "step", unknown)
    assert live_session.run_manual(**kwargs)["status"] == "PLANNED_RESTART"
    checkpoint = lifecycle.checkpoints[0]
    assert len(checkpoint["completed_queue"]) == 1 and len(checkpoint["pending_queue"]) == 10
    assert checkpoint["completed_queue"][0]["status"] == "submission_recovery_required"
    assert checkpoint["broker_retry"]
    next_lifecycle = Lifecycle(2, checkpoint)
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    validate_resume(store, lifecycle=next_lifecycle, symbols=SYMBOLS, config=kwargs["config"])
    queue = live_session.ExecutionQueue(store, lifecycle.session_id, pc="Scout")
    assert not queue.due(NOW)
    due = queue.due("2026-10-08T17:18:00Z")
    assert len(due) == 10
    assert checkpoint["completed_queue"][0]["forecast_id"] not in {f["forecast_id"] for f in due}


@pytest.mark.parametrize("mutation", ["cycle", "queue", "scope", "deadline", "generation", "schema", "recipe"])
def test_incompatible_checkpoint_fails_before_broker_start(tmp_path, monkeypatch, mutation):
    failure = dict(type="AcquisitionPending", recorded_at=NOW, message="saved", retryable=True,
                   reason="real_input_missing", retry_at="2026-10-08T17:20:00Z") if mutation == "recipe" else None
    kwargs, first, _, _, _ = setup_session(tmp_path, monkeypatch, failure=failure)
    live_session.run_manual(**kwargs)
    next_lifecycle = Lifecycle(2, first.checkpoints[0])
    if mutation in {"scope", "deadline", "generation", "schema"}:
        keys = {"scope": "symbols", "deadline": "deadline", "generation": "generation", "schema": "schema"}
        next_lifecycle.previous_checkpoint[keys[mutation]] = [] if mutation == "scope" else "bad"
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    if mutation == "cycle":
        store.update_cycle("cycle-1", {"extra_unreviewed": True})
    elif mutation == "queue":
        live_session.ExecutionQueue(store, first.session_id, pc="Scout").complete(first.checkpoints[0]["completed_queue"][0]["forecast_id"], "changed")
    kwargs, _, calls, executors, _ = setup_session(tmp_path, monkeypatch, lifecycle=next_lifecycle)
    if mutation == "recipe":
        kwargs["config"] = replace(kwargs["config"], symbol_workers=2)
    with pytest.raises((ContractError, ValueError)):
        live_session.run_manual(**kwargs)
    assert not executors and "fresh_broker_preflight" not in calls


def test_real_forecasting_loop_returns_only_after_full_fresh_cycle_and_does_not_retrain_saved_reference(tmp_path, monkeypatch):
    lifecycle = Lifecycle()
    store = ShadowStore(tmp_path / "cycles.sqlite3")
    queue = live_session.ExecutionQueue(store, lifecycle.session_id, pc="Scout")
    config = live_session.ResearchConfig()
    calls, boundaries = [], []
    def run(incoming, **kw):
        calls.append(kw)
        return {"cycle_id": seed_cycle(store, lifecycle=lifecycle, config=config, reference=kw["reference_at"])}
    runner = SimpleNamespace(store=store, config=config, run_cycle=run)
    live_session.forecasting_loop(runner, histories={}, provider=None, journal=None, queue=queue,
        stopped=lambda: False, clock=lambda: NOW, pause=lambda _: pytest.fail("No tick required to form completed boundary"),
        history_source=lambda _: object(), on_iteration=boundaries.append)
    assert len(calls) == len(boundaries) == 1
    assert inspect_cycle(store, boundaries[0]["cycle_id"], session_id=lifecycle.session_id, symbols=SYMBOLS)["status"] == "completed"
    ticks = []
    live_session.forecasting_loop(runner, histories={}, provider=None, journal=None, queue=queue,
        stopped=lambda: bool(ticks), clock=lambda: NOW, pause=ticks.append,
        history_source=lambda _: pytest.fail("Completed reference cannot rerun"), on_iteration=boundaries.append, recovery_first=True)
    assert len(calls) == len(boundaries) == 1 and ticks == [1]

def test_same_session_minute_feed_reconstruction_does_not_fetch_settled_windows(tmp_path, monkeypatch):
    from ml.intraday.minute_feed import QuarterHourMinuteFeed
    from tests.test_intraday_minute_feed import setup
    feed, arguments, clock, calls = setup(tmp_path, monkeypatch, symbols=SYMBOLS)
    assert feed.step()["status"] == "acquired"
    original = feed.store.observed_candles()
    next_feed = QuarterHourMinuteFeed(**{**arguments, "histories": {}})
    assert next_feed.next_reference == feed.next_reference
    assert next_feed.step()["status"] == "waiting" and len(calls) == 1
    assert next_feed.store.observed_candles() == original
    clock.advance(900)
    assert next_feed.step()["status"] == "acquired" and len(calls) == 2
    assert calls[1][0] == calls[0][0] + timedelta(minutes=15)


def test_market_collector_reconstruction_keeps_durable_completed_task_without_refetch(tmp_path):
    from ml.intraday.market_collection import CollectionResult
    from tests.test_intraday_market_collection import collector, quote
    calls = []
    adapter = lambda *args: calls.append(args) or CollectionResult([quote()])
    first = collector(tmp_path, adapter)
    assert len(first.collect()) == len(calls) == 1
    saved = first.state_path.read_bytes()
    second = collector(tmp_path, adapter)
    assert second.collect() == [] and len(calls) == 1
    assert second.state_path.read_bytes() == saved


def test_session_end_drains_final_native_window_without_enabling_any_order(tmp_path, monkeypatch):
    from ml.intraday import minute_feed
    kwargs, lifecycle, calls, executors, _ = setup_session(tmp_path, monkeypatch)
    observed = []
    class Feed:
        history_source = scoring_history_source = None
        def __init__(self, **kw):
            pass
        def run(self, *, stopped, pause):
            assert lifecycle.reason == "session_end"
            assert not stopped()  # Preserve the latched final native publication.
            assert not executors[0].kw["manual_authority"]()
            pause(0)
            observed.append("final_native_window_drained")
    class DrainThread:
        def __init__(self, *, target, name, **kw):
            self.target, self.name, self.alive = target, name, False
        def start(self):
            if self.name.endswith("minute-data"):
                self.alive = True
            else:
                self.target()
        def is_alive(self):
            return self.alive
        def join(self, **kw):
            if self.alive:
                self.target()
                self.alive = False
    monkeypatch.setattr(minute_feed, "QuarterHourMinuteFeed", Feed)
    monkeypatch.setattr(live_session.threading, "Thread", DrainThread)
    monkeypatch.setattr(live_session, "forecasting_loop", lambda *a, **k: None)
    kwargs["sleep"] = lambda seconds: setattr(lifecycle, "reason", "session_end")
    result = live_session.run_manual(**kwargs)
    assert result["status"] == "STOPPED" and result["stop_reason"] == "session_end"
    assert observed == ["final_native_window_drained"] and not lifecycle.checkpoints
    assert not any(isinstance(c, tuple) for c in calls)

@pytest.mark.skipif(sys.platform != "win32", reason="Windows native file sharing proof")
def test_generation_status_survives_brief_native_observer_handle(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes
    import threading
    import time
    from tools import scout_release
    real_thread = threading.Thread
    kwargs, lifecycle, _, _, _ = setup_session(tmp_path, monkeypatch)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    target = tmp_path / "intraday/manual-session.json"
    held, releasers, denied = [], [], []
    publish, replace_file = scout_release.atomic_json, scout_release.os.replace
    def observed_replace(source, destination):
        try:
            return replace_file(source, destination)
        except PermissionError as error:
            if destination == target:
                denied.append(error.winerror)
            raise
    monkeypatch.setattr(scout_release.os, "replace", observed_replace)
    def hold_then_publish(path, value):
        if path == target and value["status"] == "RUNNING" and not held:
            # A genuine read-only Windows handle without FILE_SHARE_DELETE,
            # matching the intermittent observer/antivirus race seen offline.
            handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
            assert handle not in (None, ctypes.c_void_p(-1).value)
            held.append(handle)
            def release():
                time.sleep(0.15)
                assert kernel.CloseHandle(handle)
            thread = real_thread(target=release)
            releasers.append(thread)
            thread.start()
        return publish(path, value)
    monkeypatch.setattr(scout_release, "atomic_json", hold_then_publish)
    try:
        result = live_session.run_manual(**kwargs)
    finally:
        for thread in releasers:
            thread.join(timeout=2)
    assert result["status"] == "PLANNED_RESTART" and lifecycle.checkpoints
    assert denied and set(denied) <= {5, 32, 33}
    assert json.loads(target.read_text())["status"] == "PLANNED_RESTART"
    assert not list(target.parent.glob("manual-session.json.*.tmp"))


@pytest.mark.parametrize("attempt_count,recovery_first", [(1, True), (2, False), (7, False)])
def test_checkpoint_scheduler_turn_matches_durable_measured_attempt(tmp_path, attempt_count, recovery_first):
    store = ShadowStore(tmp_path / "turn.sqlite3")
    lifecycle = Lifecycle()
    config = live_session.ResearchConfig()
    live_session.ExecutionQueue(store, lifecycle.session_id, pc="Scout")
    identity = seed_cycle(store, lifecycle=lifecycle, config=config, attempt_count=attempt_count)
    checkpoint = capture_checkpoint(store, lifecycle=lifecycle, symbols=SYMBOLS, boundary_cycle_id=identity)
    assert checkpoint["recovery_first"] is recovery_first
    # This has the original v1 shape, including historical count1/True.
    successor = Lifecycle(2, checkpoint)
    validate_resume(store, lifecycle=successor, symbols=SYMBOLS, config=config)
    checkpoint["recovery_first"] = not recovery_first
    with pytest.raises(ContractError, match="scheduler turn"):
        validate_resume(store, lifecycle=successor, symbols=SYMBOLS, config=config)


@pytest.mark.parametrize("invalid", [None, 0, -1, True, "1", 1.0])
def test_boundary_requires_real_measured_attempt_count(tmp_path, invalid):
    store = ShadowStore(tmp_path / "unmeasured.sqlite3")
    lifecycle = Lifecycle()
    config = live_session.ResearchConfig()
    live_session.ExecutionQueue(store, lifecycle.session_id, pc="Scout")
    identity = seed_cycle(store, lifecycle=lifecycle, config=config, attempt_count=invalid)
    with pytest.raises(ContractError, match="measured attempt count"):
        capture_checkpoint(store, lifecycle=lifecycle, symbols=SYMBOLS, boundary_cycle_id=identity)


@pytest.mark.parametrize("invalid", [None, 0, 1, "true", "false", []])
def test_resume_rejects_non_boolean_scheduler_turn(tmp_path, invalid):
    store = ShadowStore(tmp_path / "typed-turn.sqlite3")
    lifecycle = Lifecycle()
    config = live_session.ResearchConfig()
    live_session.ExecutionQueue(store, lifecycle.session_id, pc="Scout")
    identity = seed_cycle(store, lifecycle=lifecycle, config=config)
    checkpoint = capture_checkpoint(store, lifecycle=lifecycle, symbols=SYMBOLS, boundary_cycle_id=identity)
    checkpoint["recovery_first"] = invalid
    with pytest.raises(ContractError, match="scheduler turn"):
        validate_resume(store, lifecycle=Lifecycle(2, checkpoint), symbols=SYMBOLS, config=config)


def test_four_generations_alternate_due_recovery_and_new_quarters(tmp_path):
    store = ShadowStore(tmp_path / "fair-generations.sqlite3")
    config = live_session.ResearchConfig()
    lifecycle = Lifecycle()
    queue = live_session.ExecutionQueue(store, lifecycle.session_id, pc="Scout")
    now, calls, checkpoint = [instant(NOW)], [], None
    def run(inputs, **kw):
        calls.append(kw)
        reference = kw["reference_at"].isoformat()
        identity = "cycle:" + reference
        failure = dict(type="AcquisitionPending", recorded_at=now[0].isoformat(), retryable=True,
                       reason="real_input_missing", retry_at=(now[0] + timedelta(seconds=1)).isoformat())
        saved = store.begin_cycle(identity, dict(pc="Scout", symbols=list(SYMBOLS), mode="live",
            experiment=lifecycle.session_id, reference_at=reference, started_at=now[0].isoformat(),
            status="running", configuration_hash=digest(_jsonable(asdict(config)))))
        store.update_cycle(identity, dict(status="partial_failure", completed_at=now[0].isoformat(),
            attempt_count=saved.get("attempt_count", 0) + 1,
            failures={symbol: dict(failure) for symbol in SYMBOLS}))
        now[0] += timedelta(minutes=17)
        return {"cycle_id": identity}
    runner = SimpleNamespace(store=store, config=config, symbols=SYMBOLS, run_cycle=run)
    for generation in range(1, 5):
        lifecycle = Lifecycle(generation, checkpoint)
        validate_resume(store, lifecycle=lifecycle, symbols=SYMBOLS, config=config)
        boundaries = []
        live_session.forecasting_loop(runner, histories={}, provider=None, journal=None, queue=queue,
            stopped=lambda: False, clock=lambda: now[0], pause=lambda _: pytest.fail("Expected a full returned attempt"),
            history_source=lambda _: object(), on_iteration=boundaries.append,
            recovery_first=checkpoint["recovery_first"] if checkpoint else False)
        assert len(boundaries) == 1
        checkpoint = capture_checkpoint(store, lifecycle=lifecycle, symbols=SYMBOLS,
            boundary_cycle_id=boundaries[0]["cycle_id"])
        assert checkpoint["recovery_first"] is (generation % 2 == 1)
        assert all(row["status"] == "partial_failure" and len(row["failures"]) == 11
                   for row in checkpoint["cycles"])
    assert [call["retry_symbols"] for call in calls] == [None, list(SYMBOLS), None, list(SYMBOLS)]
    assert calls[2]["reference_at"] > calls[0]["reference_at"]
    assert all(not call["allow_execution"] for call in (calls[1], calls[3]))
    assert not store.forecasts()  # Real deferrals never become invented observations.


@pytest.mark.parametrize("attempt_count,expected", [(1, True), (2, False)])
def test_manual_successor_consumes_validated_checkpoint_turn(tmp_path, monkeypatch, attempt_count, expected):
    kwargs, first, _, _, _ = setup_session(tmp_path, monkeypatch)
    assert live_session.run_manual(**kwargs)["status"] == "PLANNED_RESTART"
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    store.update_cycle("cycle-1", {"attempt_count": attempt_count})
    checkpoint = capture_checkpoint(store, lifecycle=first, symbols=SYMBOLS, boundary_cycle_id="cycle-1")
    kwargs, successor, _, _, _ = setup_session(tmp_path, monkeypatch, lifecycle=Lifecycle(2, checkpoint))
    publish = live_session.forecasting_loop
    seen = []
    def consume(runner, **kw):
        seen.append(kw["recovery_first"])
        assert kw["recovery_first"] is expected
        return publish(runner, **kw)
    monkeypatch.setattr(live_session, "forecasting_loop", consume)
    assert live_session.run_manual(**kwargs)["status"] == "PLANNED_RESTART"
    assert seen == [expected] and successor.checkpoints[0]["recovery_first"] is True


@pytest.mark.parametrize("mutation", ["turn", "typed_turn", "count", "kind", "typed_generation"])
def test_scheduler_checkpoint_corruption_fails_before_broker(tmp_path, monkeypatch, mutation):
    kwargs, first, _, _, _ = setup_session(tmp_path, monkeypatch)
    live_session.run_manual(**kwargs)
    checkpoint = first.checkpoints[0]
    if mutation == "turn": checkpoint["recovery_first"] = False
    if mutation == "typed_turn": checkpoint["recovery_first"] = 1
    if mutation == "kind": checkpoint["boundary_kind"] = "poll"
    if mutation == "typed_generation": checkpoint["generation"] = True
    if mutation == "count":
        ShadowStore(tmp_path / "intraday/shadow.sqlite3").update_cycle("cycle-1", {"attempt_count": None})
    kwargs, _, calls, executors, _ = setup_session(tmp_path, monkeypatch, lifecycle=Lifecycle(2, checkpoint))
    with pytest.raises(ContractError):
        live_session.run_manual(**kwargs)
    assert not executors and "fresh_broker_preflight" not in calls
