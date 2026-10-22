"""Offline closing-clock and final-candle contracts; no real clients or training."""
from datetime import timedelta
import json
import sys
from types import ModuleType, SimpleNamespace

import pandas as pd
import pytest

from ml.intraday.data import (
    AcquisitionJournal, AcquisitionPending, History, SessionBounds, exact_target,
    fetch_databento_interval_attempt, recover_databento_candle_step,
)
from ml.intraday.prediction import ContractError, STEP, instant
from ml.intraday.runtime import LiveHistories
from ml.intraday.store import ShadowStore


SYMBOLS = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST", "CROX", "PATH", "TWST", "IONQ")


class Clock:
    def __init__(self, value):
        self.value = instant(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def candle(symbol, reference, available=None):
    reference = instant(reference)
    return dict(symbol=symbol, opened_at=reference.isoformat(),
        available_at=instant(available or reference+STEP).isoformat(),
        open=100., high=101., low=99., close=100.5, volume=100.,
        synthetic=False, no_trade=False, availability_kind="actual", evidence_json="{}",
        source_dataset="EQUS.MINI", source_id="databento:EQUS.MINI:ohlcv-1m",
        session_id="fixture-session", interval_seconds=900.)


def history(rows):
    frame = pd.DataFrame(rows)
    for name in ("opened_at", "available_at"):
        frame[name] = pd.to_datetime(frame[name], utc=True)
    return History(frame, {"source_dataset": "EQUS.MINI"})


@pytest.mark.parametrize("started,expected", [
    ("2026-07-10T22:00:00Z", "2026-07-11T00:00:00Z"),  # PDT: 17:00 = 00:00 UTC.
    ("2026-12-10T23:00:00Z", "2026-12-11T01:00:00Z"),  # PST: 17:00 = 01:00 UTC.
    ("2026-07-11T06:59:59Z", "2026-07-11T00:00:00Z"),  # Manual date stays July 10 Pacific.
])
def test_close_is_17_pacific_on_the_fixed_manual_date(started, expected):
    from ml.intraday.candle_collection import market_close_at

    assert market_close_at(started) == instant(expected)


def test_saved_final_candle_is_restored_without_a_provider_or_journal_request(tmp_path):
    store = ShadowStore(tmp_path/"shadow.sqlite3")
    final = candle("AAPL", "2026-07-10T23:45:00Z")
    store.record_candle(final)
    histories = {"AAPL": history([candle("AAPL", "2026-07-10T23:30:00Z")])}
    journal = SimpleNamespace(responses=lambda **kwargs: pytest.fail("Saved final candle must not be reacquired"))
    incoming = LiveHistories(histories=histories, provider=SimpleNamespace(dataset="EQUS.MINI"),
        store=store, journal=journal, reference_at=final["opened_at"], clock=Clock("2026-07-11T01:00:00Z"),
        bounded=True, stopped=lambda: False, reuse_saved=True)
    recovered = incoming.get("AAPL")
    assert recovered.frame.opened_at.max() == pd.Timestamp(final["opened_at"])
    assert len(store.observed_candles("AAPL")) == 1


def test_manual_stop_does_not_fetch_a_final_candle(tmp_path):
    store = ShadowStore(tmp_path/"shadow.sqlite3")
    histories = {"AAPL": history([candle("AAPL", "2026-07-10T23:30:00Z")])}
    journal = SimpleNamespace(responses=lambda **kwargs: pytest.fail("Manual stop must prevent provider preparation"))
    with pytest.raises(InterruptedError):
        incoming = LiveHistories(histories=histories, provider=SimpleNamespace(dataset="EQUS.MINI"),
            store=store, journal=journal, reference_at="2026-07-10T23:45:00Z",
            clock=Clock("2026-07-11T00:01:00Z"), bounded=True, stopped=lambda: True, reuse_saved=True)
        incoming.get("AAPL")
    assert store.observed_candles("AAPL") == []


def test_final_collection_does_not_redefine_the_forecast_45_minute_target():
    reference = instant("2026-07-10T23:15:00Z")  # 16:15 Pacific.
    target_open, target_close = exact_target(reference)
    assert target_open.to_pydatetime() == reference+2*STEP
    assert target_close.to_pydatetime() == reference+3*STEP == instant("2026-07-11T00:00:00Z")


class ProviderFixture:
    dataset = "EQUS.MINI"

    def __init__(self, clock, *, pending=None, events=None):
        self.clock, self.pending = clock, dict(pending or {})
        self.calls, self.events = [], events if events is not None else []

    def completed_candle(self, symbol, reference):
        assert reference+STEP <= self.clock(), "A forming interval reached the provider"
        self.calls.append((symbol, reference, self.clock()))
        key = (symbol, reference)
        if self.pending.get(key, 0):
            self.pending[key] -= 1
            raise AcquisitionPending("fixture_delayed_publication", 4)
        return candle(symbol, reference, self.clock())


def fake_live_histories(monkeypatch, *, persist=True):
    from ml.intraday import candle_collection

    class Incoming:
        def __init__(self, *, histories, provider, store, journal, reference_at, clock,
                     bounded, stopped, reuse_saved, population_contract=None):
            assert population_contract is None
            assert bounded and reuse_saved
            self.histories, self.provider, self.store = histories, provider, store
            self.reference, self.clock, self.stopped = instant(reference_at), clock, stopped

        def bind_cycle(self, identity):
            self.identity = identity

        def get(self, symbol):
            if self.stopped():
                raise InterruptedError("Fixture manual stop")
            saved = [row for row in self.store.observed_candles(symbol)
                     if instant(row["opened_at"]) == self.reference]
            row = saved[-1] if saved else self.provider.completed_candle(symbol, self.reference)
            if not saved and persist:
                self.store.record_candle(row)
                self.provider.events.append(("persist", symbol, self.reference))
            self.histories[symbol] = history([row])
            return self.histories[symbol]

    monkeypatch.setattr(candle_collection, "LiveHistories", Incoming)


def collector_fixture(monkeypatch, tmp_path, clock, *, store=None, provider=None,
                      stopped=lambda: False, persist=True, started_at="2026-07-10T22:00:00Z"):
    from ml.intraday.candle_collection import CandleCollector

    fake_live_histories(monkeypatch, persist=persist)
    store = store or ShadowStore(tmp_path/"shadow.sqlite3")
    provider = provider or ProviderFixture(clock)
    collector = CandleCollector(store, histories={}, provider=provider, journal=object(),
        symbols=SYMBOLS, session_id="manual-close-fixture", started_at=started_at,
        clock=clock, stopped=stopped)
    return collector, store, provider


def test_forming_final_interval_is_excluded_but_completed_1630_bar_is_collected(monkeypatch, tmp_path):
    clock = Clock("2026-07-10T23:59:59Z")
    collector, store, provider = collector_fixture(monkeypatch, tmp_path, clock)
    result = collector.pass_once()
    assert not result["closing"] and not result["final_complete"]
    assert {reference for _, reference, _ in provider.calls} == {instant("2026-07-10T23:30:00Z")}
    assert len(provider.calls) == len(SYMBOLS)
    assert all(store.observed_candles(symbol)[0]["opened_at"] == "2026-07-10T23:30:00+00:00" for symbol in SYMBOLS)


def test_partial_closing_collection_retries_then_completes_only_after_all_eleven_persist(monkeypatch, tmp_path):
    clock = Clock("2026-07-11T00:00:01Z")
    final = instant("2026-07-10T23:45:00Z")
    provider = ProviderFixture(clock, pending={("TWST", final): 1})
    collector, store, provider = collector_fixture(monkeypatch, tmp_path, clock, provider=provider)
    first = collector.pass_once()
    assert first["closing"] and not first["final_complete"]
    assert first["pending_final_symbols"] == ["TWST"] and first["saved"] == 10
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='final_candle_collection_completed'").fetchone()[0] == 0
    calls = len(provider.calls)
    collector.pass_once()  # The durable retry timestamp excludes an early extra request.
    assert len(provider.calls) == calls
    clock.advance(4)
    final_result = collector.pass_once()
    assert final_result["final_complete"] and final_result["pending_final_symbols"] == []
    assert len(provider.calls) == len(SYMBOLS)+1
    assert all(store.observed_candles(symbol) for symbol in SYMBOLS)
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM events WHERE kind='final_candle_collection_completed'").fetchone()[0] == 1
    collector.pass_once()
    assert len(provider.calls) == len(SYMBOLS)+1


def test_restart_skips_durable_final_symbols_and_preserves_older_pending_slots(monkeypatch, tmp_path):
    clock = Clock("2026-07-11T00:00:01Z")
    final = instant("2026-07-10T23:45:00Z")
    provider = ProviderFixture(clock, pending={("TWST", final): 1})
    first, store, provider = collector_fixture(monkeypatch, tmp_path, clock, provider=provider)
    first.pass_once()
    old = instant("2026-07-10T23:30:00Z")
    with store.connect() as db:
        db.execute("INSERT INTO intraday_candle_collection VALUES(?,?,?,'pending',?,NULL)",
                   ("manual-close-fixture", "AAPL", old.isoformat(), clock().isoformat()))
    clock.advance(4)
    before = len(provider.calls)
    resumed, _, _ = collector_fixture(monkeypatch, tmp_path, clock, store=store, provider=provider)
    result = resumed.pass_once()
    assert result["final_complete"]
    assert provider.calls[before:] == [("TWST", final, clock())]
    with store.connect() as db:
        assert db.execute("SELECT status FROM intraday_candle_collection WHERE symbol='AAPL' AND reference_at=?",
                          (old.isoformat(),)).fetchone()[0] == "pending"


def test_volatile_candle_without_durable_save_cannot_signal_close_completion(monkeypatch, tmp_path):
    clock = Clock("2026-07-11T00:00:01Z")
    collector, store, provider = collector_fixture(monkeypatch, tmp_path, clock, persist=False)
    result = collector.pass_once()
    assert not result["final_complete"] and result["pending_final_symbols"] == list(SYMBOLS)
    assert all(store.observed_candles(symbol) == [] for symbol in SYMBOLS)


@pytest.mark.parametrize("late", ["2026-07-11T01:00:00Z", "2026-07-11T07:05:00Z"])
def test_late_collection_queries_only_fixed_final_interval_without_18_or_next_day_fetch(monkeypatch, tmp_path, late):
    clock = Clock(late)
    collector, _, provider = collector_fixture(monkeypatch, tmp_path, clock)
    assert collector.pass_once()["final_complete"]
    assert {reference for _, reference, _ in provider.calls} == {instant("2026-07-10T23:45:00Z")}
    assert all(reference+STEP == collector.close_at for _, reference, _ in provider.calls)


def test_collection_loop_persists_every_final_symbol_before_stop_callback(monkeypatch, tmp_path):
    from ml.intraday.candle_collection import collection_loop

    clock = Clock("2026-07-11T00:00:01Z")
    stopped = [False]
    events = []
    provider = ProviderFixture(clock, events=events)
    store = ShadowStore(tmp_path/"shadow.sqlite3")
    fake_live_histories(monkeypatch)

    def completed(result):
        assert result["final_complete"]
        assert all(store.observed_candles(symbol) for symbol in SYMBOLS)
        events.append(("stop", None, clock()))
        stopped[0] = True

    result = collection_loop(store, histories={}, provider=provider, journal=object(), symbols=SYMBOLS,
        session_id="manual-close-fixture", started_at="2026-07-10T22:00:00Z", clock=clock,
        stopped=lambda: stopped[0], pause=lambda _: pytest.fail("Complete final collection must finish"),
        on_completed=completed)
    assert result["final_complete"] and stopped[0]
    assert len(events) == len(SYMBOLS)+1 and events[-1][0] == "stop"
    assert all(event[0] == "persist" for event in events[:-1])


def test_manual_stop_leaves_missing_final_slots_resumable_and_never_reenables_collection(monkeypatch, tmp_path):
    from ml.intraday.candle_collection import collection_loop

    clock = Clock("2026-07-11T00:00:01Z")
    provider = ProviderFixture(clock)
    store = ShadowStore(tmp_path/"shadow.sqlite3")
    fake_live_histories(monkeypatch)
    result = collection_loop(store, histories={}, provider=provider, journal=object(), symbols=SYMBOLS,
        session_id="manual-close-fixture", started_at="2026-07-10T22:00:00Z", clock=clock,
        stopped=lambda: True, pause=lambda _: pytest.fail("Manual stop must not wait"),
        on_completed=lambda _: pytest.fail("Manual stop must not become market-close completion"))
    assert result == {"final_complete": False, "manual_stop_preserved": True}
    assert provider.calls == []


@pytest.mark.parametrize("cutoff", ["2026-07-11T00:00:00Z", "2026-12-11T01:00:00Z"])
@pytest.mark.parametrize("late_seconds", [0, 3600, 8*3600])
def test_forecasting_and_scoring_make_no_requests_at_or_after_17(monkeypatch, tmp_path, cutoff, late_seconds):
    from ml.intraday import live_session

    close = instant(cutoff)
    clock = Clock(close+timedelta(seconds=late_seconds))
    store = ShadowStore(tmp_path/"shadow.sqlite3")
    runner = SimpleNamespace(store=store, config=SimpleNamespace(publication_offset_seconds=30),
        run_cycle=lambda *args, **kwargs: pytest.fail("Closed session must not fit or publish another forecast"))
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kwargs: pytest.fail("Closed workers must not acquire candles"))
    live_session.forecasting_loop(runner, histories={}, provider=object(), journal=object(),
        queue=SimpleNamespace(session_id="manual-close-fixture", enqueue=lambda _: pytest.fail("Closed enqueue")),
        stopped=lambda: False, clock=clock, close_at=close, pause=lambda _: pytest.fail("Closed worker must finish"))
    scoring_store = SimpleNamespace(observed_candles=lambda: pytest.fail("Closed scoring must return before its acquisition pass"))
    live_session.scoring_loop(scoring_store, histories={}, provider=object(), journal=object(),
        session_id="manual-close-fixture", stopped=lambda: False, clock=clock, close_at=close,
        pause=lambda _: pytest.fail("Closed scoring must finish"))


def test_close_crossing_during_history_setup_prevents_a_new_forecast_fit(monkeypatch, tmp_path):
    from ml.intraday import live_session

    close = instant("2026-07-11T00:00:00Z")
    clock = Clock(close-timedelta(seconds=1))
    store = ShadowStore(tmp_path/"shadow.sqlite3")
    runner = SimpleNamespace(store=store, config=SimpleNamespace(publication_offset_seconds=30),
        run_cycle=lambda *args, **kwargs: pytest.fail("Clock crossed close before the fit"))
    monkeypatch.setattr(live_session, "eligible_reference", lambda _: True)

    def incoming(**kwargs):
        assert not kwargs["stopped"]()
        clock.advance(2)
        assert kwargs["stopped"]()
        return object()

    monkeypatch.setattr(live_session, "LiveHistories", incoming)
    live_session.forecasting_loop(runner, histories={}, provider=object(), journal=object(),
        queue=SimpleNamespace(session_id="manual-close-fixture", enqueue=lambda _: pytest.fail("Closed enqueue")),
        stopped=lambda: False, clock=clock, close_at=close, pause=lambda _: pytest.fail("No further closed pass"))


@pytest.mark.parametrize("cross_during", ["dataset_range", "schema_range"])
def test_metadata_request_crossing_close_never_starts_price_fetch_or_records_a_provider_failure(tmp_path, cross_during):
    close = instant("2026-07-11T00:00:00Z")
    clock = Clock(close-timedelta(seconds=1))
    reference = close-2*STEP
    calls = []

    class MetadataProvider:
        dataset = "EQUS.MINI"

        def native_specs(self):
            calls.append("specs")
            return [SimpleNamespace(schema="ohlcv-1m", frequency="1m")]

        def dataset_range(self):
            calls.append("dataset_range")
            if cross_during == "dataset_range":
                clock.advance(2)
            return object()

        def available_range_for_schema(self, schema, **kwargs):
            calls.append("schema_range")
            if cross_during == "schema_range":
                clock.advance(2)
            return SimpleNamespace(start=reference-STEP, end=close)

        def fetch_native_bars_range(self, *args, **kwargs):
            pytest.fail("A price request began after the 17:00 cutoff")

    journal = AcquisitionJournal(tmp_path/"acquisition.sqlite3")
    with pytest.raises(InterruptedError, match="stopped before"):
        recover_databento_candle_step(MetadataProvider(), symbol="AAPL", opened_at=reference,
            journal=journal, session=SessionBounds("fixture", (close-timedelta(hours=16)).isoformat(), close.isoformat()),
            previous=None, clock=clock, stopped=lambda: clock() >= close)
    assert calls == (["specs", "dataset_range"] if cross_during == "dataset_range"
                     else ["specs", "dataset_range", "schema_range"])
    assert journal.responses(symbol="AAPL", source_id="databento:EQUS.MINI:ohlcv-1m",
        interval_open=reference, interval_close=reference+STEP) == ()


def test_unfinished_minute_range_is_rejected_before_any_provider_metadata():
    reference = instant("2026-07-10T23:45:00Z")
    provider = SimpleNamespace(dataset="EQUS.MINI",
        native_specs=lambda: pytest.fail("A forming candle must not trigger metadata reads"))
    with pytest.raises(ContractError, match="unfinished reference"):
        fetch_databento_interval_attempt(provider, symbol="AAPL", opened_at=reference,
            attempt_id="fixture-forming", clock=Clock(reference+STEP-timedelta(seconds=1)))


@pytest.mark.parametrize("cutoff", ["2026-07-11T00:00:00Z", "2026-12-11T01:00:00Z"])
def test_execution_clock_crossing_17_rechecks_authority_before_any_order_or_recovery(monkeypatch, tmp_path, cutoff):
    """Exercise the real manual orchestration with inert local client bindings."""
    from ml.intraday import data, live_session

    close = instant(cutoff)
    clock = Clock(close-timedelta(seconds=1))
    authority = []
    due_calls, sleeps = [], []

    def module(name, **members):
        value = ModuleType(name)
        value.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, value)

    class Executor:
        def __init__(self, **kwargs):
            authority.append(kwargs["manual_authority"])

        def step(self, forecast):
            pytest.fail("An order began after the clock crossed 17:00")

        def recover(self):
            pytest.fail("A new recovery request began after the clock crossed 17:00")

    module("ml.intraday.account_execution", IntradayAccountExecutor=Executor,
           execution_failure_evidence=lambda exc: pytest.fail("No execution error expected"))
    module("ml.intraday.local_execution", load_local_binding=lambda _: SimpleNamespace(actor="Atlas", symbols=("AAPL",)))
    module("ml.intraday.cancellation", retry_delay=lambda *args: pytest.fail("No broker operation should fail"))
    module("app.services.schwab", SchwabSession=lambda: object())
    module("app.services.databento_market_data", DatabentoMarketDataProvider=lambda: SimpleNamespace(dataset="EQUS.MINI"))
    module("dotenv", load_dotenv=lambda *args: None)
    monkeypatch.setattr(live_session, "local_symbols", lambda *args: ("Atlas", ("AAPL",)))
    monkeypatch.setattr(data, "load_local_history", lambda *args, **kwargs: object())
    monkeypatch.setattr(live_session, "ShadowRunner", lambda **kwargs: object())
    monkeypatch.setattr(live_session, "forecasting_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(live_session, "scoring_loop", lambda *args, **kwargs: None)
    monkeypatch.setattr(live_session, "collection_loop", lambda *args, **kwargs: None)

    class InlineThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

        def is_alive(self):
            return False

        def join(self, **kwargs):
            pass

    monkeypatch.setattr(live_session.threading, "Thread", InlineThread)

    class Queue:
        def __init__(self, store, session_id, **kwargs):
            self.session_id = session_id

        def due(self, now):
            due_calls.append(instant(now))
            assert authority[0]()  # The own manual session is still RUNNING before close.
            clock.advance(2)
            assert not authority[0]()  # Clock alone revokes new execution before status is rewritten.
            return [{"symbol": "AAPL", "forecast_id": "unsent-fixture"}]

        def broker_due(self, now):
            return True

    monkeypatch.setattr(live_session, "ExecutionQueue", Queue)

    def sleep(seconds):
        assert not authority[0]()
        sleeps.append(clock())
        if len(sleeps) == 2:
            raise KeyboardInterrupt()  # Explicit manual stop while final collection remains unfinished.

    result = live_session.run_manual(repository=tmp_path, datastore=tmp_path,
        profile=tmp_path/"profile.json", watchlist=tmp_path/"watchlist.txt",
        config=live_session.ResearchConfig(), clock=clock, sleep=sleep)
    assert due_calls == [close-timedelta(seconds=1)]
    assert result["status"] == "STOPPED" and result["stop_reason"] == "manual_stop"
    status = json.loads((tmp_path/"intraday/manual-session.json").read_text())
    assert status["stop_reason"] == "manual_stop" and not status["final_collection_complete"]
    assert not status["broker_orders_automatically_cancelled"]


@pytest.mark.parametrize("started,late", [
    ("2026-12-25T23:00:00Z", "2026-12-26T01:01:00Z"),  # XNAS Christmas holiday.
    ("2026-11-27T23:00:00Z", "2026-11-28T01:01:00Z"),  # XNAS 13:00 Eastern short session.
])
def test_nonexistent_final_interval_on_holiday_or_short_day_finishes_without_fake_candles(monkeypatch, tmp_path, started, late):
    clock = Clock(late)
    collector, store, provider = collector_fixture(monkeypatch, tmp_path, clock, started_at=started)
    result = collector.pass_once()
    assert result["closing"] and result["final_complete"]
    assert collector.final_interval_eligible is False
    assert result["pending_final_symbols"] == [] and result["saved"] == 0
    assert provider.calls == []
    assert all(store.observed_candles(symbol) == [] for symbol in SYMBOLS)
    with store.connect() as db:
        row = db.execute("SELECT payload FROM events WHERE kind='final_candle_collection_completed'").fetchone()
    assert row is not None and json.loads(row[0])["final_interval_eligible"] is False
