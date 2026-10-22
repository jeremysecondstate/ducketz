"""Final production scoring callback under real private SQLite writer contention."""
import ast
from contextlib import contextmanager
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from ml.intraday import candle_collection, live_session
from ml.intraday.store import ShadowStore
from tests.test_intraday_candle_close import history
from tests.test_intraday_final_scoring import CLOSE, SID, final_candle, save_forecast


class CompletionStore(ShadowStore):
    """Real transactions with zero fixture lock timeout; production default stays intact."""
    contention = None
    observes = 0
    completion_events = 0

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0)
        try:
            with db:
                yield db
        finally:
            db.close()

    def observe(self, forecast, **kwargs):
        self.observes += 1
        if self.contention is not None:
            self.contention("scoring", self.observes)
        return super().observe(forecast, **kwargs)

    def event(self, session_id, kind, payload, **kwargs):
        if kind == "final_saved_outcomes_scored":
            self.completion_events += 1
            if self.contention is not None:
                self.contention("completion_event", self.completion_events)
        return super().event(session_id, kind, payload, **kwargs)


def production_callback(store, stopped, complete, clock):
    """Compile the exact nested production callback; never invoke run_manual."""
    source = Path(live_session.__file__).read_text(encoding="utf-8-sig")
    node = next(node for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.FunctionDef) and node.name == "collection_completed")
    namespace = dict(store=store, stopped=stopped, final_collection_complete=complete,
        score_saved_session_outcomes=live_session.score_saved_session_outcomes,
        session_id=SID, pc="Atlas", symbols=("AAPL", "AMZN"), clock=clock)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 str(live_session.__file__), "exec"), namespace)
    return namespace["collection_completed"]


def setup_completion(tmp_path, monkeypatch):
    store = CompletionStore(tmp_path / "private.sqlite3")
    originals = [save_forecast(store, cycle="own-" + symbol, symbol=symbol)
                 for symbol in ("AAPL", "AMZN")]
    candles = [final_candle(symbol=symbol) for symbol in ("AAPL", "AMZN")]
    for candle in candles:
        store.record_candle(candle)
    histories = {row["symbol"]: history([row]) for row in candles}
    stopped, complete = threading.Event(), threading.Event()
    clock = lambda: CLOSE.replace(second=5)
    callback = production_callback(store, stopped, complete, clock)
    calls = {"passes": 0, "callback": 0, "pauses": 0}
    actual_pass = candle_collection.CandleCollector.pass_once

    def counted_pass(self):
        calls["passes"] += 1
        return actual_pass(self)

    monkeypatch.setattr(candle_collection.CandleCollector, "pass_once", counted_pass)
    def completed(result):
        calls["callback"] += 1
        callback(result)
    def forbidden(*args, **kwargs):
        pytest.fail("Saved final collection must not acquire, fit, or execute")
    provider = SimpleNamespace(dataset="EQUS.MINI", completed_candle=forbidden,
        native_specs=forbidden, dataset_range=forbidden, fetch_native_bars_range=forbidden)
    journal = SimpleNamespace(responses=forbidden)
    def run(pause):
        return candle_collection.collection_loop(store, histories=histories, provider=provider,
            journal=journal, symbols=("AAPL", "AMZN"), session_id=SID,
            started_at=CLOSE.replace(hour=22) - timedelta(days=1),
            clock=clock, stopped=stopped.is_set, pause=pause, on_completed=completed)
    return SimpleNamespace(store=store, originals=originals, candles=candles,
        stopped=stopped, complete=complete, calls=calls, run=run)


class WriterContention:
    def __init__(self, state, target):
        self.state, self.target, self.holder = state, target, None
        self.began = False
        state.store.contention = self.contend

    def contend(self, kind, attempt):
        # The second scoring write proves recovery after a prior outcome commits.
        if not self.began and kind == self.target and attempt == (2 if kind == "scoring" else 1):
            self.holder = sqlite3.connect(self.state.store.path, timeout=0)
            self.holder.execute("BEGIN IMMEDIATE")
            self.began = True

    def release(self):
        if self.holder is not None:
            self.holder.rollback()
            self.holder.close()
            self.holder = None


@pytest.mark.parametrize("target", ["scoring", "completion_event"])
def test_real_final_callback_contention_resumes_without_recollection_or_duplicate_outcomes(tmp_path, monkeypatch, target):
    state = setup_completion(tmp_path, monkeypatch)
    lock = WriterContention(state, target)
    original_rows = []
    def release(seconds):
        assert seconds == 1 and lock.began
        state.calls["pauses"] += 1
        with state.store.connect() as db:
            original_rows.extend(db.execute("SELECT * FROM outcomes ORDER BY id").fetchall())
        assert len(original_rows) == (1 if target == "scoring" else 2)
        assert not state.stopped.is_set() and not state.complete.is_set()
        lock.release()
    try:
        result = state.run(release)
    finally:
        lock.release()
    assert result["final_complete"] and result["saved"] == 2
    assert state.calls == {"passes": 1, "callback": 2, "pauses": 1}
    assert state.stopped.is_set() and state.complete.is_set()
    with state.store.connect() as db:
        outcomes = db.execute("SELECT * FROM outcomes ORDER BY id").fetchall()
        assert len(outcomes) == 2 and all(row in outcomes for row in original_rows)
        assert db.execute("SELECT count(*) FROM events WHERE kind='final_saved_outcomes_scored'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events WHERE kind='final_candle_collection_completed'").fetchone()[0] == 1
        retries = [json.loads(row[0]) for row in db.execute("SELECT payload FROM events WHERE kind='collection_storage_retry_completed'")]
    assert len(retries) == 1 and retries[0]["operation"] == "on_completed"
    assert retries[0]["last_failure"]["root_cause"]["sqlite_errorcode"] == sqlite3.SQLITE_BUSY
    assert len(state.store.observed_candles()) == 2
    for original in state.originals:
        assert state.store.forecast(original["cycle_id"], original["symbol"]) == original
        assert len(state.store.revisions(original["forecast_id"])) == 1


@pytest.mark.parametrize("target", ["scoring", "completion_event"])
def test_manual_stop_during_final_callback_contention_preserves_unfinished_scoring(tmp_path, monkeypatch, target):
    state = setup_completion(tmp_path, monkeypatch)
    lock = WriterContention(state, target)
    def stop(seconds):
        state.calls["pauses"] += 1
        state.stopped.set()
        lock.release()
    try:
        result = state.run(stop)
    finally:
        lock.release()
    assert result == {"final_complete": False, "manual_stop_preserved": True}
    assert state.calls == {"passes": 1, "callback": 1, "pauses": 1}
    assert not state.complete.is_set() and state.stopped.is_set()
    with state.store.connect() as db:
        assert db.execute("SELECT count(*) FROM outcomes").fetchone()[0] == (1 if target == "scoring" else 2)
        assert db.execute("SELECT count(*) FROM events WHERE kind='final_saved_outcomes_scored'").fetchone()[0] == 0


def test_keyboard_interrupt_during_contention_is_not_swallowed(tmp_path, monkeypatch):
    state = setup_completion(tmp_path, monkeypatch)
    lock = WriterContention(state, "completion_event")
    def interrupt(seconds):
        state.calls["pauses"] += 1
        raise KeyboardInterrupt()
    try:
        with pytest.raises(KeyboardInterrupt):
            state.run(interrupt)
    finally:
        lock.release()
    assert state.calls == {"passes": 1, "callback": 1, "pauses": 1}
    assert not state.complete.is_set() and not state.stopped.is_set()


def test_non_contention_callback_error_retains_failure_without_retry(tmp_path, monkeypatch):
    state = setup_completion(tmp_path, monkeypatch)
    original_event = state.store.event
    def bad_event(session, kind, payload, **kwargs):
        if kind == "final_saved_outcomes_scored":
            with state.store.connect() as db:
                db.execute("INSERT INTO nonexistent_callback_table VALUES(1)")
        return original_event(session, kind, payload, **kwargs)
    monkeypatch.setattr(state.store, "event", bad_event)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        state.run(lambda _: pytest.fail("Non-contention errors must not pause"))
    assert state.calls == {"passes": 1, "callback": 1, "pauses": 0}
    assert not state.complete.is_set() and not state.stopped.is_set()


def test_already_stopped_session_never_collects_or_calls_completion(tmp_path, monkeypatch):
    state = setup_completion(tmp_path, monkeypatch)
    state.stopped.set()
    assert state.run(lambda _: pytest.fail("Stopped session must not pause")) == {
        "final_complete": False, "manual_stop_preserved": True}
    assert state.calls == {"passes": 0, "callback": 0, "pauses": 0}
    assert not state.complete.is_set()
