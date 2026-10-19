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
    fresh = [(symbol, reference) for symbol, reference, _ in provider.calls[1:]
             if reference == instant("2026-07-10T22:00:00Z")]
    assert {symbol for symbol, _ in fresh} == {"AAPL", "TWST"}
    assert provider.calls[1][1] == instant("2026-07-10T22:00:00Z")
    assert any(reference == old for _, reference, _ in provider.calls[1:]), "Retained older work must receive its bounded share"
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
        fairness_before = db.execute("SELECT * FROM intraday_candle_fairness").fetchall()
    manual_stop[0] = True
    collector.pass_once()
    assert len(provider.calls) == 1
    with store.connect() as db:
        assert db.execute("SELECT * FROM intraday_candle_collection ORDER BY symbol,reference_at").fetchall() == before
        assert db.execute("SELECT * FROM intraday_candle_fairness").fetchall() == fairness_before


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



def test_saturated_quarters_preserve_due_retries_and_symbol_progress_after_recreation(monkeypatch, tmp_path):
    """Every bounded request crosses a quarter; no restart may reset fairness."""
    clock = Clock("2026-07-10T15:00:30Z")
    symbols = ("AAPL", "MU", "TWST")
    old = instant("2026-07-10T14:00:00Z")
    calls, attempts = [], {(symbol, old): 1 for symbol in symbols}
    store = ShadowStore(tmp_path / "saturated.sqlite3")

    class Incoming:
        def __init__(self, *, store, reference_at, stopped, **kwargs):
            self.store, self.reference, self.stopped = store, reference_at, stopped

        def bind_cycle(self, identity):
            assert identity == "saturated-fixture"

        def get(self, symbol):
            assert not self.stopped()
            calls.append((symbol, self.reference, clock()))
            key = (symbol, self.reference)
            attempts[key] = attempts.get(key, 0) + 1
            clock.advance(901)
            if attempts[key] == 1:
                raise AcquisitionPending("fixture_sparse_confirmation_pending", 4)
            self.store.record_candle(observed(symbol, self.reference, clock()))

    monkeypatch.setattr(candle_collection, "LiveHistories", Incoming)
    def recreate():
        return candle_collection.CandleCollector(store, histories={s: object() for s in symbols},
            provider=type("Provider", (), {"dataset": "EQUS.MINI"})(), journal=object(),
            symbols=symbols, session_id="saturated-fixture", started_at=clock(), clock=clock)

    recreate()
    with store.connect() as db:
        db.executemany("INSERT INTO intraday_candle_collection VALUES(?,?,?,'pending',?,?)",
            [("saturated-fixture", s, old.isoformat(), old.isoformat(), "original pending receipt")
             for s in symbols])
    for _ in range(9):
        before = len(calls)
        recreate().pass_once()
        assert len(calls) == before + 1, "A new quarter must be scheduled after the current bounded step"
    with store.connect() as db:
        old_status = db.execute("SELECT symbol,status FROM intraday_candle_collection WHERE session_id=? AND reference_at=? ORDER BY symbol",
            ("saturated-fixture", old.isoformat())).fetchall()
    assert old_status == [(symbol, "saved") for symbol in symbols], "Existing retries and the final symbol must receive bounded service"
    fresh = [(symbol, reference, at) for symbol, reference, at in calls if reference != old]
    assert {symbol for symbol, _, _ in fresh} == set(symbols), "Fresh opportunities must rotate across symbols after object recreation"
    assert all(at - reference < 2 * STEP for _, reference, at in fresh)
    assert [reference for _, reference, _ in calls[2::3]] == [old] * 3



def test_future_retry_does_not_consume_turn_or_displace_due_fresh_work(monkeypatch, tmp_path):
    clock = Clock("2026-07-10T22:00:30Z")
    collector, store, provider = fixture(monkeypatch, tmp_path, clock)
    old = instant("2026-07-10T21:00:00Z")
    future = clock() + timedelta(minutes=20)
    collector._schedule(clock())
    with store.connect() as db:
        db.execute("UPDATE intraday_candle_fairness SET fresh_streak=2 WHERE session_id='cadence-fixture'")
        db.execute("INSERT INTO intraday_candle_collection VALUES(?,?,?,'pending',?,?)",
            ("cadence-fixture", "TWST", old.isoformat(), future.isoformat(), "original deferred error"))
        before = db.execute("SELECT * FROM intraday_candle_collection WHERE reference_at=?", (old.isoformat(),)).fetchall()
    collector.pass_once()
    assert len(provider.calls) == 2 and all(reference != old for _, reference, _ in provider.calls)
    with store.connect() as db:
        state = db.execute("SELECT * FROM intraday_candle_fairness").fetchall()
        assert state == [("cadence-fixture", 2, "TWST")]
        assert db.execute("SELECT * FROM intraday_candle_collection WHERE reference_at=?", (old.isoformat(),)).fetchall() == before
    collector.pass_once()
    assert len(provider.calls) == 2
    with store.connect() as db:
        assert db.execute("SELECT * FROM intraday_candle_fairness").fetchall() == state


def test_final_source_empty_preserves_old_pending_and_does_not_invent_candles(monkeypatch, tmp_path):
    clock = Clock("2026-07-10T23:59:59Z")
    collector, store, provider = fixture(monkeypatch, tmp_path, clock)
    old = instant("2026-07-10T22:00:00Z")
    with store.connect() as db:
        db.execute("INSERT INTO intraday_candle_collection VALUES(?,?,?,'pending',?,?)",
            ("cadence-fixture", "TWST", old.isoformat(), old.isoformat(), "old pending evidence"))
        old_row = db.execute("SELECT * FROM intraday_candle_collection").fetchall()
    clock.advance(1)
    calls = []

    class EmptyIncoming:
        def __init__(self, *, reference_at, **kwargs):
            self.reference = reference_at

        def bind_cycle(self, identity):
            assert identity == "cadence-fixture"

        def get(self, symbol):
            calls.append((symbol, self.reference))
            raise candle_collection.SourceIntervalEmpty({"status": "unavailable", "reason": "source_empty",
                "final": True, "candle": None, "evidence": []})

    monkeypatch.setattr(candle_collection, "LiveHistories", EmptyIncoming)
    assert collector.pass_once()["final_complete"]
    assert {symbol for symbol, _ in calls} == {"AAPL", "TWST"}
    assert all(reference == collector.final_reference for _, reference in calls)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM candles").fetchone()[0] == 0
        assert db.execute("SELECT * FROM intraday_candle_collection WHERE reference_at=?", (old.isoformat(),)).fetchall() == old_row
        assert db.execute("SELECT status FROM intraday_candle_collection WHERE reference_at=?",
            (collector.final_reference.isoformat(),)).fetchall() == [("source_empty",), ("source_empty",)]
        assert db.execute("SELECT * FROM intraday_candle_fairness").fetchall() == [("cadence-fixture", 0, None)]
    assert collector.pass_once()["final_complete"] and len(calls) == 2
