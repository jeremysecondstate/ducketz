"""Offline quarter-hour fairness with a fake clock and acquisition adapter."""
from datetime import timedelta

import pytest

from ml.intraday import candle_collection
from ml.intraday.data import AcquisitionPending
from ml.intraday.prediction import STEP, instant
from ml.intraday.store import ShadowStore


class Clock:
    def __init__(self, value):
        self.value = instant(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def observed(symbol, reference, available):
    return {"symbol": symbol, "opened_at": reference.isoformat(), "available_at": available.isoformat(),
            "open": 100., "high": 101., "low": 99., "close": 100.5, "volume": 100.,
            "synthetic": False, "no_trade": False, "availability_kind": "actual", "evidence_json": "{}",
            "source_dataset": "EQUS.MINI", "source_id": "databento:EQUS.MINI:ohlcv-1m",
            "session_id": "cadence-fixture", "interval_seconds": 900.}


class ProviderFixture:
    dataset = "EQUS.MINI"

    def __init__(self, clock, first_step_seconds=0):
        self.clock, self.first_step_seconds, self.calls = clock, first_step_seconds, []

    def acquire(self, symbol, reference):
        assert reference + STEP <= self.clock()
        self.calls.append((symbol, reference, self.clock()))
        if len(self.calls) == 1 and self.first_step_seconds:
            self.clock.advance(self.first_step_seconds)
            raise AcquisitionPending("fixture_publication_pending", 4)
        return observed(symbol, reference, self.clock())


def fixture(monkeypatch, tmp_path, clock, *, first_step_seconds=0, offset=30, stopped=lambda: False):
    class Incoming:
        def __init__(self, *, provider, store, reference_at, bounded, reuse_saved, stopped, **kwargs):
            assert bounded and reuse_saved
            self.provider, self.store, self.reference, self.stopped = provider, store, reference_at, stopped

        def bind_cycle(self, identity):
            self.identity = identity

        def get(self, symbol):
            if self.stopped():
                raise InterruptedError("Fixture manual stop")
            candle = self.provider.acquire(symbol, self.reference)
            self.store.record_candle(candle)
            return candle

    monkeypatch.setattr(candle_collection, "LiveHistories", Incoming)
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    provider = ProviderFixture(clock, first_step_seconds)
    collector = candle_collection.CandleCollector(store, histories={"AAPL": object(), "TWST": object()},
        provider=provider, journal=object(), symbols=("AAPL", "TWST"), session_id="cadence-fixture",
        started_at="2026-07-10T22:00:00Z", publication_offset_seconds=offset, clock=clock, stopped=stopped)
    return collector, store, provider


@pytest.mark.parametrize("offset,first_step_seconds", [(0, 1), (30, 31), (30, 91)])
def test_new_quarter_preempts_backlog_after_inflight_step_and_old_work_resumes(monkeypatch, tmp_path, offset, first_step_seconds):
    clock = Clock("2026-07-10T22:14:59Z")
    collector, store, provider = fixture(monkeypatch, tmp_path, clock,
        first_step_seconds=first_step_seconds, offset=offset)
    old = instant("2026-07-10T21:00:00Z")
    prior = observed("AAPL", old - STEP, old)
    prior_id = store.record_candle(prior)
    with store.connect() as db:
        db.execute("INSERT INTO intraday_candle_collection VALUES(?,?,?,'pending',?,?)",
            ("cadence-fixture", "TWST", old.isoformat(), old.isoformat(), "original retry evidence"))
        old_row = db.execute("SELECT * FROM intraday_candle_collection WHERE reference_at=?", (old.isoformat(),)).fetchone()
        original_bytes = db.execute("SELECT payload FROM candles WHERE id=?", (prior_id,)).fetchone()[0]
    first = collector.pass_once()
    assert first["saved"] == 0 and not first["final_complete"]
    assert len(provider.calls) == 1, "New intervals must not wait for the stale pending snapshot to drain"
    assert provider.calls[0][1] == instant("2026-07-10T21:45:00Z")
    with store.connect() as db:
        assert db.execute("SELECT * FROM intraday_candle_collection WHERE reference_at=?", (old.isoformat(),)).fetchone() == old_row
        assert db.execute("SELECT payload FROM candles WHERE id=?", (prior_id,)).fetchone()[0] == original_bytes
        first_retry = db.execute("SELECT status,next_attempt_at,last_error FROM intraday_candle_collection WHERE symbol='AAPL' AND reference_at=?",
            (provider.calls[0][1].isoformat(),)).fetchone()
        assert first_retry[0] == "pending" and "fixture_publication_pending" in first_retry[2]
        assert instant(first_retry[1]) == clock() + timedelta(seconds=4)
    collector.pass_once()
    assert [(symbol, reference) for symbol, reference, _ in provider.calls[1:3]] == [
        ("AAPL", instant("2026-07-10T22:00:00Z")), ("TWST", instant("2026-07-10T22:00:00Z"))]
    assert any(reference == old for _, reference, _ in provider.calls[3:]), "Retained older work must resume after fresh slots"
    clock.advance(4)
    collector.pass_once()
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM intraday_candle_collection WHERE status='pending'").fetchone()[0] == 0
        assert db.execute("SELECT payload FROM candles WHERE id=?", (prior_id,)).fetchone()[0] == original_bytes
        assert db.execute("SELECT count(*) FROM events WHERE kind='candle_collection_retry'").fetchone()[0] == 1


def test_step_before_publication_offset_keeps_the_existing_pass_working(monkeypatch, tmp_path):
    clock = Clock("2026-07-10T22:14:59Z")
    collector, _, provider = fixture(monkeypatch, tmp_path, clock, first_step_seconds=30, offset=30)
    collector.pass_once()
    assert clock() == instant("2026-07-10T22:15:29Z")
    assert len(provider.calls) == 2
    assert {reference for _, reference, _ in provider.calls} == {instant("2026-07-10T21:45:00Z")}


def test_manual_stop_after_yield_does_not_acquire_or_clear_pending_work(monkeypatch, tmp_path):
    clock, manual_stop = Clock("2026-07-10T22:14:59Z"), [False]
    collector, store, provider = fixture(monkeypatch, tmp_path, clock,
        first_step_seconds=31, stopped=lambda: manual_stop[0])
    collector.pass_once()
    with store.connect() as db:
        before = db.execute("SELECT * FROM intraday_candle_collection ORDER BY symbol,reference_at").fetchall()
    manual_stop[0] = True
    collector.pass_once()
    assert len(provider.calls) == 1
    with store.connect() as db:
        assert db.execute("SELECT * FROM intraday_candle_collection ORDER BY symbol,reference_at").fetchall() == before


def test_reaching_fixed_close_yields_and_next_pass_collects_final_slots(monkeypatch, tmp_path):
    clock = Clock("2026-07-10T23:59:59Z")
    collector, store, provider = fixture(monkeypatch, tmp_path, clock, first_step_seconds=1)
    assert not collector.pass_once()["final_complete"]
    assert len(provider.calls) == 1
    assert collector.pass_once()["final_complete"]
    assert {reference for _, reference, _ in provider.calls[1:]} == {instant("2026-07-10T23:45:00Z")}
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM intraday_candle_collection WHERE status='pending'").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM events WHERE kind='final_candle_collection_completed'").fetchone()[0] == 1
