"""Malformed saved evidence and real SQLite contention stay isolated offline."""
from contextlib import closing
from datetime import timedelta
import sqlite3
from types import SimpleNamespace

import pytest

from tests.intraday_split_package_support import require_split_integration
require_split_integration(__file__, files=(
    "ml/intraday/split_scoring.py", "ml/intraday/scoring_storage.py",
    "tests/test_intraday_split_scoring.py"),
    interfaces=(("ml/intraday/runtime.py", "ShadowRunner", "prepare_cycle", ()),
                ("ml/intraday/runtime.py", "ShadowRunner", "__init__",
                 ("prepared_source", "forecast_preparer", "datastore"))))

from ml.intraday import split_scoring
from ml.intraday.runtime import ResearchConfig
from tests.test_intraday_split_scoring import setup, run
from tests.test_intraday_final_scoring import CLOSE, final_candle


class Checkpoint(Exception):
    pass


def renewal_for(session, stopped=lambda: False):
    saved = []
    def checkpoint(state, due):
        saved.append((state, due))
        raise Checkpoint
    return SimpleNamespace(session=session, stopped=stopped, checkpoint=checkpoint), saved


@pytest.mark.parametrize("table", ["forecasts", "candles", "outcomes"])
def test_malformed_saved_symbol_does_not_stop_unrelated_scoring(tmp_path, table):
    source, target, session, forecasts = setup(tmp_path, symbols=("AAPL", "AMZN"))
    source.record_candle(final_candle())
    run(tmp_path, target, session, CLOSE + timedelta(seconds=5))
    source.record_candle(final_candle(symbol="AMZN"))
    with target.connect() as db:
        predicate, key = (("forecast_id", forecasts["AAPL"]["forecast_id"])
                          if table == "outcomes" else ("symbol", "AAPL"))
        db.execute(f"UPDATE {table} SET payload=? WHERE {predicate}=?", ("invalid original", key))
    result = run(tmp_path, target, session, CLOSE + timedelta(seconds=6))
    assert result["status"] == "WAITING_FOR_DATA"
    assert result["final_symbols_ready"] == ["AMZN"]
    assert result["outcomes"]["scored"] == 1
    assert any(row["symbol"] == "AAPL" and row["error_type"] == "JSONDecodeError"
               for row in result["rejected_candles"])
    assert target.revisions(forecasts["AMZN"]["forecast_id"])[-1]["status"] == "scored"
    with target.connect() as db:
        assert db.execute(f"SELECT payload FROM {table} WHERE {predicate}=?", (key,)).fetchone()[0] == "invalid original"


def test_native_target_writer_contention_checkpoints_and_retries_exact_original(tmp_path, monkeypatch):
    source, target, session, forecasts = setup(tmp_path)
    source.record_candle(final_candle())
    renewal, saved = renewal_for(session)
    original_record = target.record_candle
    def busy_record(value):
        # Use a separate short-timeout native writer to create a real BUSY
        # code, rather than relying on an exception-message classifier.
        with closing(sqlite3.connect(target.path, timeout=.01)) as writer:
            writer.execute("INSERT INTO candles VALUES(?,?,?,?,?)", ("unused", "AAPL", "unused", "unused", "unused"))
    with target.connect() as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        monkeypatch.setattr(target, "record_candle", busy_record)
        with pytest.raises(Checkpoint):
            split_scoring.renewal_scoring_pass(root=tmp_path, store=target, renewal=renewal,
                config=ResearchConfig(), clock=lambda: CLOSE + timedelta(seconds=5))
    result = saved[-1][0]["last_scoring"]
    assert result["status"] == "WAITING_FOR_STORAGE" and result["partial_progress_retained"]
    assert result["diagnostics"]["root_cause"]["sqlite_errorname"] == "SQLITE_BUSY"
    assert saved[-1][1] == (CLOSE + timedelta(seconds=8)).isoformat()
    monkeypatch.setattr(target, "record_candle", original_record)
    completed = split_scoring.renewal_scoring_pass(root=tmp_path, store=target, renewal=renewal,
        config=ResearchConfig(), clock=lambda: CLOSE + timedelta(seconds=8))
    assert completed["status"] == "COMPLETED"
    assert len(target.revisions(forecasts["AAPL"]["forecast_id"])) == 1
    assert target.forecast(forecasts["AAPL"]["cycle_id"], "AAPL") == forecasts["AAPL"]


def test_non_contention_storage_fault_is_not_retried(tmp_path, monkeypatch):
    _, target, session, _ = setup(tmp_path)
    renewal, saved = renewal_for(session)
    def broken(**kwargs):
        raise sqlite3.OperationalError("unclassified failure")
    monkeypatch.setattr(split_scoring, "scoring_pass", broken)
    with pytest.raises(sqlite3.OperationalError, match="unclassified"):
        split_scoring.renewal_scoring_pass(root=tmp_path, store=target, renewal=renewal,
            config=ResearchConfig(), clock=lambda: CLOSE + timedelta(seconds=5))
    assert saved == []


def test_completion_receipt_contention_retains_scored_outcome(tmp_path, monkeypatch):
    source, target, session, forecasts = setup(tmp_path)
    source.record_candle(final_candle())
    renewal, saved = renewal_for(session)
    original_event = target.event
    def busy(*args, **kwargs):
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode, error.sqlite_errorname = sqlite3.SQLITE_BUSY, "SQLITE_BUSY"
        raise error
    monkeypatch.setattr(target, "event", busy)
    with pytest.raises(Checkpoint):
        split_scoring.renewal_scoring_pass(root=tmp_path, store=target, renewal=renewal,
            config=ResearchConfig(), clock=lambda: CLOSE + timedelta(seconds=5))
    retained = target.revisions(forecasts["AAPL"]["forecast_id"])
    assert len(retained) == 1 and saved[-1][0]["last_scoring"]["status"] == "WAITING_FOR_STORAGE"
    monkeypatch.setattr(target, "event", original_event)
    result = split_scoring.renewal_scoring_pass(root=tmp_path, store=target, renewal=renewal,
        config=ResearchConfig(), clock=lambda: CLOSE + timedelta(seconds=8))
    assert result["status"] == "COMPLETED"
    assert target.revisions(forecasts["AAPL"]["forecast_id"]) == retained
    with target.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='final_saved_outcomes_scored'").fetchone()[0] == 1


def test_stop_during_storage_contention_prevents_successor(tmp_path, monkeypatch):
    _, target, session, _ = setup(tmp_path)
    stopped = [False]
    renewal, saved = renewal_for(session, stopped=lambda: stopped[0])
    def busy(**kwargs):
        stopped[0] = True
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode, error.sqlite_errorname = sqlite3.SQLITE_BUSY, "SQLITE_BUSY"
        raise error
    monkeypatch.setattr(split_scoring, "scoring_pass", busy)
    result = split_scoring.renewal_scoring_pass(root=tmp_path, store=target, renewal=renewal,
        config=ResearchConfig(), clock=lambda: CLOSE + timedelta(seconds=5))
    assert result["status"] == "STOPPED" and saved == []
