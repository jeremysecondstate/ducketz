"""Exact scoring-write recovery from the observed Scout 1A native BUSY paths."""
from copy import deepcopy
import json
import sqlite3
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday import data, live_session
from ml.intraday.prediction import ContractError, encoded, instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import candle, forecast

NOW = "2026-10-09T00:01:00+00:00"
AUDIT = "unverified_synthetic_outcome_requires_audit"


def fixture_store(tmp_path, monkeypatch, operation):
    store = ShadowStore(tmp_path / "score.sqlite3")
    store.connection_timeout = 0
    original = store.save_forecast(forecast(mode="live", pc="Scout"))
    if operation in {"audit", "invalidate"}:
        target = candle(synthetic=True, evidence_json="{}")
        if operation == "invalidate":
            with monkeypatch.context() as m:
                m.setattr(data, "synthetic_continuity_verified", lambda value: True)
                store.observe(original, as_of=NOW, candle=target)
        else:
            store.record_candle(target)
    elif operation == "observe":
        store.record_candle(candle())
    return store, original


def busy_at(store, monkeypatch, method, *, kind=None):
    real = getattr(store, method)
    calls, writers = [], []
    def action(*args, **kwargs):
        if kind is not None and args[1] != kind:
            return real(*args, **kwargs)
        calls.append(deepcopy((args, kwargs)))
        if len(calls) == 1:
            writer = sqlite3.connect(store.path, timeout=0)
            writer.execute("BEGIN IMMEDIATE")
            writers.append(writer)
        return real(*args, **kwargs)
    monkeypatch.setattr(store, method, action)
    return calls, writers


def run(store, *, pause, stopped, histories=None, source=None, **kwargs):
    live_session.scoring_loop(store, histories=histories or {}, provider=None, journal=None,
        pc=kwargs.pop("pc", "Scout"), session_id="scout-scoring-write-fixture",
        clock=kwargs.pop("clock", lambda: NOW), pause=pause, stopped=stopped,
        history_source=source, **kwargs)


def unlock(writers):
    while writers:
        writer = writers.pop()
        writer.rollback()
        writer.close()


@pytest.mark.parametrize("operation", ["audit", "invalidate", "observe"])
def test_native_busy_retries_exact_write_without_replaying_completed_work(tmp_path, monkeypatch, operation):
    store, original = fixture_store(tmp_path, monkeypatch, operation)
    method = {"audit": "event", "invalidate": "invalidate_unverified_outcome", "observe": "observe"}[operation]
    calls, writers = busy_at(store, monkeypatch, method, kind=AUDIT if operation == "audit" else None)
    polls, stamps = [], []
    def clock():
        stamps.append(len(stamps))
        return NOW if len(stamps) < 10 else "2026-10-09T00:02:00+00:00"
    def pause(seconds):
        polls.append(seconds)
        if seconds == .2:
            assert len(calls) == 1
            unlock(writers)
    try:
        run(store, pause=pause, stopped=lambda: 3 in polls, clock=clock)
    finally:
        unlock(writers)
    assert polls == [.2, 3]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert store.forecast_by_id(original["forecast_id"]) == original
    if operation == "audit":
        with store.connect() as db:
            rows = db.execute("SELECT payload,recorded_at FROM events WHERE kind=?", (AUDIT,)).fetchall()
        assert len(rows) == 1 and rows[0][1] == calls[0][1]["recorded_at"]
        assert not store.revisions(original["forecast_id"])
    else:
        revisions = store.revisions(original["forecast_id"])
        assert len(revisions) == (2 if operation == "invalidate" else 1)
        assert revisions[-1]["status"] == ("unscorable" if operation == "invalidate" else "scored")
        assert revisions[-1]["observed_at"] == instant(NOW).isoformat()


def test_busy_retry_diagnostic_keeps_original_acquisition_error_and_one_get(tmp_path, monkeypatch):
    store, original = fixture_store(tmp_path, monkeypatch, "retry")
    calls, writers = busy_at(store, monkeypatch, "event", kind="target_scoring_retry")
    gets, polls = [], []
    class Incoming:
        def bind_cycle(self, identity):
            assert identity == "score:" + original["forecast_id"]
        def get(self, symbol):
            gets.append(symbol)
            raise ContractError("original exact target missing")
    def pause(seconds):
        polls.append(seconds)
        if seconds == .2:
            assert gets == ["AAPL"]
            unlock(writers)
    try:
        run(store, histories={"AAPL": object()}, source=lambda reference: Incoming(),
            pause=pause, stopped=lambda: 3 in polls)
    finally:
        unlock(writers)
    assert polls == [.2, 3] and gets == ["AAPL"]
    assert len(calls) == 2 and calls[0] == calls[1]
    with store.connect() as db:
        rows = db.execute("SELECT payload FROM events WHERE kind='target_scoring_retry'").fetchall()
    assert len(rows) == 1 and json.loads(rows[0][0]) == {
        "forecast_id": original["forecast_id"], "error_type": "ContractError", "message": "original exact target missing"}


def test_acquired_outcome_busy_does_not_refetch_or_shift_observation_clock(tmp_path, monkeypatch):
    store, original = fixture_store(tmp_path, monkeypatch, "acquire")
    calls, writers = busy_at(store, monkeypatch, "observe")
    gets, polls = [], []
    class Incoming:
        def bind_cycle(self, identity):
            pass
        def get(self, symbol):
            gets.append(symbol)
            frame = pd.DataFrame([candle()])
            for field in ("opened_at", "available_at"):
                frame[field] = pd.to_datetime(frame[field], utc=True)
            return SimpleNamespace(frame=frame)
    def pause(seconds):
        polls.append(seconds)
        if seconds == .2:
            assert gets == ["AAPL"]
            unlock(writers)
    try:
        run(store, histories={"AAPL": object()}, source=lambda reference: Incoming(),
            pause=pause, stopped=lambda: 3 in polls)
    finally:
        unlock(writers)
    assert polls == [.2, 3] and gets == ["AAPL"]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert store.revisions(original["forecast_id"])[-1]["observed_at"] == NOW


@pytest.mark.parametrize("operation", ["audit", "invalidate", "observe", "retry"])
def test_stop_during_native_busy_keeps_originals_and_pending_write_unaffected(tmp_path, monkeypatch, operation):
    store, original = fixture_store(tmp_path, monkeypatch, operation)
    method = {"audit": "event", "retry": "event", "invalidate": "invalidate_unverified_outcome", "observe": "observe"}[operation]
    kind = AUDIT if operation == "audit" else "target_scoring_retry" if operation == "retry" else None
    before = store.revisions(original["forecast_id"])
    calls, writers = busy_at(store, monkeypatch, method, kind=kind)
    stopped = []
    class Incoming:
        def bind_cycle(self, identity):
            pass
        def get(self, symbol):
            raise ContractError("original exact target missing")
    def pause(seconds):
        assert seconds == .2
        stopped.append(True)
        unlock(writers)
    try:
        run(store, histories={"AAPL": object()} if operation == "retry" else {},
            source=lambda reference: Incoming(), pause=pause, stopped=lambda: bool(stopped))
    finally:
        unlock(writers)
    assert len(calls) == 1 and store.revisions(original["forecast_id"]) == before
    with store.connect() as db:
        kinds = [row[0] for row in db.execute("SELECT kind FROM events")]
        assert kinds == ([AUDIT] if operation == "invalidate" else [])
    assert store.forecast_by_id(original["forecast_id"]) == original


def test_nonbusy_sqlite_error_propagates_without_retry(tmp_path, monkeypatch):
    store, _ = fixture_store(tmp_path, monkeypatch, "audit")
    calls = []
    def event(*args, **kwargs):
        calls.append(1)
        raise sqlite3.OperationalError("no such table: original")
    monkeypatch.setattr(store, "event", event)
    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        run(store, pause=lambda _: pytest.fail("nonbusy retry"), stopped=lambda: False)
    assert calls == [1]


def test_optional_retry_receipts_cannot_mask_success(tmp_path, monkeypatch):
    store, _ = fixture_store(tmp_path, monkeypatch, "audit")
    calls, writers = busy_at(store, monkeypatch, "event", kind=AUDIT)
    def bad_diagnostics(error):
        raise OSError("optional diagnostics unavailable")
    monkeypatch.setattr(live_session, "_symbol_exception_diagnostics", bad_diagnostics)
    polls, receipts = [], []
    def pause(seconds):
        polls.append(seconds)
        unlock(writers)
    def receipt(phase, detail):
        receipts.append((phase, deepcopy(detail)))
        raise OSError("optional receipt unavailable")
    try:
        run(store, pause=pause, stopped=lambda: 3 in polls, on_persistence_retry=receipt)
    finally:
        unlock(writers)
    assert [phase for phase, _ in receipts] == ["started", "recovered"]
    assert receipts[0][1]["episode_id"] == receipts[1][1]["episode_id"]
    assert receipts[1][1]["busy_count"] == 1 and len(calls) == 2


def test_atlas_scoring_contention_keeps_existing_behavior(tmp_path, monkeypatch):
    store, _ = fixture_store(tmp_path, monkeypatch, "audit")
    calls, writers = busy_at(store, monkeypatch, "event", kind=AUDIT)
    try:
        with pytest.raises(sqlite3.OperationalError):
            run(store, pc="Atlas", pause=lambda _: pytest.fail("Atlas retry changed"), stopped=lambda: False)
    finally:
        unlock(writers)
    assert len(calls) == 1


def test_main_retained_target_busy_retries_before_historical_scan(tmp_path, monkeypatch):
    from ml.intraday.store import ScoringForecastReader
    store, original = fixture_store(tmp_path, monkeypatch, "observe")
    store.begin_cycle(original["cycle_id"], dict(pc="Scout", mode="live",
        experiment="scout-scoring-write-fixture", symbols=[original["symbol"]],
        reference_at=original["reference_at"], configuration_hash="fixture",
        started_at=original["issued_at"]))
    reads, polls = [], []
    real_iter = ScoringForecastReader.__iter__
    def read(self):
        reads.append(self.session_id)
        return real_iter(self)
    monkeypatch.setattr(ScoringForecastReader, "__iter__", read)
    calls, writers = busy_at(store, monkeypatch, "observe")
    def pause(seconds):
        polls.append(seconds)
        if seconds == .2:
            assert reads == ["scout-scoring-write-fixture"]
            unlock(writers)
    try:
        run(store, pause=pause, stopped=lambda: 3 in polls)
    finally:
        unlock(writers)
    assert polls == [.2, 3]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[0][1]["skip_newer"] is True
    assert store.revisions(original["forecast_id"])[-1]["observed_at"] == NOW
    assert None in reads
