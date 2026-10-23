"""Main scoring API keeps exact work through real temporary WAL contention."""
from contextlib import contextmanager
from datetime import timedelta
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday import live_session
from ml.intraday.prediction import instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import candle, forecast


class QuickContentionStore(ShadowStore):
    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=0.01)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()


def setup_target(tmp_path, pc, saved_target):
    store = QuickContentionStore(tmp_path / "temporary-shadow.sqlite3")
    original = store.save_forecast(forecast(mode="live", pc=pc))
    target = candle(close="101")
    if saved_target:
        store.record_candle(target)
    source_calls, acquired = [], []

    class Incoming:
        def bind_cycle(self, identity):
            assert identity == "score:" + original["forecast_id"]

        def get(self, symbol):
            acquired.append(symbol)
            row = dict(target, opened_at=pd.Timestamp(target["opened_at"]))
            return SimpleNamespace(frame=pd.DataFrame([row]))

    def source(reference):
        source_calls.append(reference)
        return Incoming()

    return store, original, source, source_calls, acquired


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
@pytest.mark.parametrize("saved_target", [False, True], ids=["acquired", "saved"])
def test_main_scoring_retries_exact_observation_after_real_writer_contention(
        tmp_path, monkeypatch, capsys, pc, saved_target):
    store, original, source, source_calls, acquired = setup_target(tmp_path, pc, saved_target)
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kwargs: pytest.fail("unexpected data client"))
    blocker = sqlite3.connect(store.path)
    blocker.execute("BEGIN IMMEDIATE")
    stopped, attempts, pauses = [], [], []
    observe = store.observe

    def observed(value, **kwargs):
        attempts.append((value, dict(kwargs)))
        return observe(value, **kwargs)

    monkeypatch.setattr(store, "observe", observed)
    clock_calls = []

    def clock():
        clock_calls.append(None)
        return instant("2026-10-08T17:46:00Z") + timedelta(seconds=len(clock_calls))

    def pause(seconds):
        pauses.append(seconds)
        if seconds == 1:
            blocker.rollback()
        else:
            stopped.append(True)

    try:
        live_session.scoring_loop(store, histories={"AAPL": object()}, provider=object(), journal=object(),
            session_id="temporary-session", stopped=lambda: bool(stopped), clock=clock, pause=pause,
            pc=pc, history_source=source)
    finally:
        blocker.close()
    assert pauses == [1, 3]
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert source_calls == ([] if saved_target else [original["target_open"]])
    assert acquired == ([] if saved_target else ["AAPL"])
    outcomes = store.revisions(original["forecast_id"])
    assert len(outcomes) == 1 and outcomes[0]["status"] == "scored"
    assert store.forecast("cycle", "AAPL") == original
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM forecasts").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events WHERE kind='scoring_storage_retry_completed'").fetchone()[0] == 1
    assert '"sqlite_errorname":"SQLITE_BUSY"' in capsys.readouterr().out


@pytest.mark.parametrize("saved_target", [False, True], ids=["acquired", "saved"])
def test_main_storage_retry_obeys_manual_stop_without_losing_original(tmp_path, monkeypatch, saved_target):
    store, original, source, _, acquired = setup_target(tmp_path, "Scout", saved_target)
    blocker = sqlite3.connect(store.path)
    blocker.execute("BEGIN IMMEDIATE")
    stopped = []

    def pause(seconds):
        assert seconds == 1
        stopped.append(True)
        blocker.rollback()

    try:
        with pytest.raises(InterruptedError, match="Manual session stopped"):
            live_session.scoring_loop(store, histories={"AAPL": object()}, provider=object(), journal=object(),
                session_id="temporary-session", stopped=lambda: bool(stopped),
                clock=lambda: "2026-10-08T17:46:00Z", pause=pause, pc="Scout", history_source=source)
    finally:
        blocker.close()
    assert store.forecast("cycle", "AAPL") == original
    assert store.revisions(original["forecast_id"]) == []
    assert acquired == ([] if saved_target else ["AAPL"])


def test_main_acquired_outcome_non_contention_sqlite_error_is_not_acquisition_retry(tmp_path, monkeypatch):
    store, original, source, _, acquired = setup_target(tmp_path, "Scout", False)

    def broken_storage(*args, **kwargs):
        with store.connect() as db:
            db.execute("SELECT missing_storage_function()")

    monkeypatch.setattr(store, "observe", broken_storage)
    with pytest.raises(sqlite3.OperationalError, match="no such function") as caught:
        live_session.scoring_loop(store, histories={"AAPL": object()}, provider=object(), journal=object(),
            session_id="temporary-session", stopped=lambda: False, clock=lambda: "2026-10-08T17:46:00Z",
            pause=lambda seconds: pytest.fail("fatal storage error retried"), pc="Scout", history_source=source)
    assert caught.value.sqlite_errorcode == sqlite3.SQLITE_ERROR
    assert acquired == ["AAPL"]
    assert store.forecast("cycle", "AAPL") == original
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='target_scoring_retry'").fetchone()[0] == 0
