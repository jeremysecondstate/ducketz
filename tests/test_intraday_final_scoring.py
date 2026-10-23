"""Final local scoring and worker interrupts use only inert offline fixtures."""
from datetime import timedelta
import json
from types import ModuleType, SimpleNamespace
import sys

import pytest

from ml.intraday import live_session
from ml.intraday.data import IntervalResponse, SessionBounds, resolve_interval
from ml.intraday.prediction import STEP, instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast


CLOSE = instant("2026-10-15T00:00:00Z")  # October 14, 17:00 Pacific.
SOURCE = "databento:EQUS.MINI:ohlcv-1m"
SID = "manual-current-fixture"


def save_forecast(store, *, cycle="own-cycle", session=SID, actor="Atlas", symbol="AAPL",
                  reference=None, mode="live", source=SOURCE):
    ref = instant(reference) if reference is not None else CLOSE - 3 * STEP
    store.begin_cycle(cycle, {"pc": actor, "mode": mode, "experiment": session, "symbols": [symbol],
        "reference_at": ref.isoformat(), "configuration_hash": "fixture", "started_at": ref.isoformat()})
    return store.save_forecast(forecast(pc=actor, symbol=symbol, cycle_id=cycle, mode=mode,
        reference_at=ref.isoformat(), data_through=(ref + STEP).isoformat(),
        issued_at=(ref + STEP + timedelta(seconds=20)).isoformat(),
        information_available_at=(ref + STEP + timedelta(seconds=10)).isoformat(),
        target_open=(ref + 2 * STEP).isoformat(), target_close=(ref + 3 * STEP).isoformat(),
        source_id=source, source_dataset="EQUS.MINI"))


def final_candle(*, symbol="AAPL", available=None, source=SOURCE):
    return {"symbol": symbol, "opened_at": (CLOSE - STEP).isoformat(),
        "available_at": instant(available or CLOSE + timedelta(seconds=4)).isoformat(),
        "source_id": source, "source_dataset": "EQUS.MINI", "availability_kind": "actual",
        "synthetic": False, "no_trade": False, "open": 100., "high": 101., "low": 99.,
        "close": 100.5, "volume": 100., "evidence_json": "{}", "interval_seconds": 900}


def score(store, **changes):
    options = {"session_id": SID, "actor": "Atlas", "symbols": ("AAPL",),
               "as_of": CLOSE + timedelta(seconds=5)}
    options.update(changes)
    return live_session.score_saved_session_outcomes(store, **options)


def test_final_1700_target_scores_from_saved_candle_idempotently_without_acquisition(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "saved.sqlite3")
    original = save_forecast(store)
    store.record_candle(final_candle())
    monkeypatch.setattr(live_session, "LiveHistories", lambda **kwargs: pytest.fail("Final scoring fetched data"))
    first = score(store)
    assert first == {"session_id": SID, "scored": 1, "synthetic": 0, "unavailable": 0}
    assert store.revisions(original["forecast_id"])[-1]["status"] == "scored"
    assert score(store, as_of=CLOSE + timedelta(minutes=1)) == first
    assert len(store.revisions(original["forecast_id"])) == 1
    assert store.forecast(original["cycle_id"], "AAPL") == original


def test_native_one_minute_row_cannot_score_the_final_15m_target(tmp_path):
    store = ShadowStore(tmp_path / "one-minute.sqlite3")
    original = save_forecast(store)
    value = final_candle()
    value["interval_seconds"] = 60
    store.record_candle(value)
    assert score(store) == {"session_id": SID, "scored": 0, "synthetic": 0, "unavailable": 1}
    assert store.revisions(original["forecast_id"]) == []


def test_final_pass_never_scores_other_session_actor_or_symbol(tmp_path):
    store = ShadowStore(tmp_path / "scope.sqlite3")
    own = save_forecast(store)
    excluded = [
        save_forecast(store, cycle="old-session", session="manual-previous"),
        save_forecast(store, cycle="other-actor", actor="Scout"),
        save_forecast(store, cycle="other-symbol", symbol="AMZN"),
        save_forecast(store, cycle="replay", mode="historical_replay"),
    ]
    store.record_candle(final_candle())
    store.record_candle(final_candle(symbol="AMZN"))
    assert score(store)["scored"] == 1
    assert store.revisions(own["forecast_id"])
    assert all(store.revisions(row["forecast_id"]) == [] for row in excluded)


def test_verified_synthetic_final_target_retains_synthetic_outcome(tmp_path):
    from tests.test_intraday_data import independent_absence
    store = ShadowStore(tmp_path / "synthetic.sqlite3")
    original = save_forecast(store)
    reference = CLOSE - STEP
    previous = final_candle(available=reference)
    previous["opened_at"] = (reference - STEP).isoformat()
    previous["close"] = 100.
    previous["session_id"] = "same-session"
    session = SessionBounds("same-session", (CLOSE - timedelta(hours=13)).isoformat(), CLOSE.isoformat())
    attempts = [IntervalResponse(str(index), "AAPL", SOURCE, "EQUS.MINI", reference.isoformat(), CLOSE.isoformat(),
        (CLOSE + timedelta(seconds=wait)).isoformat(), (CLOSE + timedelta(seconds=wait + 1)).isoformat(),
        "success", (), session.opened_at, session.closed_at, "independent complete scope fixture",
        no_trade_scope_evidence=independent_absence(symbol="AAPL",dataset="EQUS.MINI",opened_at=reference,
            closed_at=CLOSE,verified_at=CLOSE+timedelta(seconds=wait+1)))
        for index, wait in enumerate((1, 6))]
    resolved = resolve_interval(attempts, session=session, previous=previous)
    assert resolved["status"] == "synthetic_no_trade"
    store.record_candle(resolved["candle"])
    result = score(store, as_of=CLOSE + timedelta(seconds=8))
    assert result["synthetic"] == 1 and result["scored"] == 0
    assert store.revisions(original["forecast_id"])[-1]["status"] == "synthetic"


def test_future_unavailable_and_other_source_targets_are_not_invented(tmp_path):
    store = ShadowStore(tmp_path / "unavailable.sqlite3")
    due = save_forecast(store)
    future = save_forecast(store, cycle="future", reference=CLOSE - 2 * STEP)
    store.record_candle(final_candle(available=CLOSE + timedelta(minutes=1)))
    store.record_candle(final_candle(source="different-source"))
    result = score(store)
    assert result == {"session_id": SID, "scored": 0, "synthetic": 0, "unavailable": 1}
    assert store.revisions(due["forecast_id"]) == store.revisions(future["forecast_id"]) == []


def test_manual_stop_prevents_final_outcome_writes(tmp_path):
    store = ShadowStore(tmp_path / "manual-stop.sqlite3")
    original = save_forecast(store)
    store.record_candle(final_candle())
    assert score(store, stopped=lambda: True)["scored"] == 0
    assert store.revisions(original["forecast_id"]) == []


def test_legacy_unverified_synthetic_target_stays_unresolved(tmp_path):
    store = ShadowStore(tmp_path / "unverified.sqlite3")
    original = save_forecast(store)
    value = final_candle()
    value.update(open=100., high=100., low=100., close=100., volume=0., synthetic=True, no_trade=True,
                 evidence_json='{"legacy_confirmations":2}')
    store.record_candle(value)
    assert score(store)["unavailable"] == 1
    assert store.revisions(original["forecast_id"]) == []


def inert_manual(monkeypatch):
    """Replace every client and worker; no native trader or client is launched."""
    from ml.intraday import data
    def module(name, **members):
        value = ModuleType(name)
        value.__dict__.update(members)
        monkeypatch.setitem(sys.modules, name, value)
    module("ml.intraday.account_execution", IntradayAccountExecutor=lambda **kwargs: SimpleNamespace(
        step=lambda *args: pytest.fail("Unexpected broker step"), recover=lambda: pytest.fail("Unexpected recovery")),
        execution_failure_evidence=lambda *args: pytest.fail("Unexpected execution failure"))
    module("ml.intraday.local_execution", load_local_binding=lambda _: SimpleNamespace(actor="Atlas", symbols=("AAPL",)))
    module("ml.intraday.cancellation", retry_delay=lambda *args: pytest.fail("Unexpected broker retry"))
    module("app.services.schwab", SchwabSession=lambda: object())
    module("app.services.databento_market_data", DatabentoMarketDataProvider=lambda: SimpleNamespace(dataset="EQUS.MINI"))
    module("dotenv", load_dotenv=lambda *args: None)
    monkeypatch.setattr(live_session, "local_symbols", lambda *args: ("Atlas", ("AAPL",)))
    monkeypatch.setattr(data, "load_local_history", lambda *args, **kwargs: object())
    monkeypatch.setattr(live_session, "ShadowRunner", lambda **kwargs: SimpleNamespace(store=kwargs["store"]))
    for name in ("forecasting_loop", "scoring_loop", "collection_loop"):
        monkeypatch.setattr(live_session, name, lambda *args, **kwargs: None)
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


@pytest.mark.parametrize("worker", ["scoring", "collection"])
@pytest.mark.parametrize("manual_stop", [False, True])
def test_worker_interrupted_error_is_expected_only_for_actual_manual_stop(tmp_path, monkeypatch, worker, manual_stop):
    inert_manual(monkeypatch)
    def interrupt(*args, **kwargs):
        if manual_stop:
            kwargs["stopped"].__self__.set()
        raise InterruptedError("unexpected transport interrupt fixture")
    monkeypatch.setattr(live_session, worker + "_loop", interrupt)
    result = live_session.run_manual(repository=tmp_path, datastore=tmp_path, profile=tmp_path / "profile.json",
        watchlist=tmp_path / "watchlist.txt", config=live_session.ResearchConfig(),
        clock=lambda: CLOSE - timedelta(minutes=1), sleep=lambda _: pytest.fail("No running fixture loop expected"))
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    if manual_stop:
        assert result["status"] == "STOPPED" and status["worker_errors"] == []
        assert status["stop_reason"] == "manual_stop"
    else:
        assert result["status"] == "FAILED" and status["stop_reason"] == "worker_failure"
        assert status["worker_errors"][0]["worker"] == worker
        assert status["worker_errors"][0]["error_type"] == "InterruptedError"


@pytest.mark.parametrize("worker", ["scoring", "collection"])
def test_market_close_suppresses_score_interrupt_but_not_unfinished_collection_failure(tmp_path, monkeypatch, worker):
    inert_manual(monkeypatch)
    def interrupt(*args, **kwargs):
        raise InterruptedError("unexpected transport interrupt at close fixture")
    monkeypatch.setattr(live_session, worker + "_loop", interrupt)
    def manual_stop(_):
        raise KeyboardInterrupt()
    result = live_session.run_manual(repository=tmp_path, datastore=tmp_path, profile=tmp_path / "profile.json",
        watchlist=tmp_path / "watchlist.txt", config=live_session.ResearchConfig(),
        clock=lambda: CLOSE + timedelta(seconds=5), sleep=manual_stop)
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    if worker == "scoring":
        assert result["status"] == "STOPPED" and status["worker_errors"] == []
    else:
        assert result["status"] == "FAILED" and status["worker_errors"][0]["worker"] == "collection"


@pytest.mark.parametrize("diagnostic_unavailable", [False, True])
def test_collection_failure_latches_original_before_diagnostic_persistence(
        tmp_path, monkeypatch, capsys, diagnostic_unavailable):
    inert_manual(monkeypatch)
    original = RuntimeError("original collection failure fixture")
    stopped, diagnostics = [], []
    event = live_session.ShadowStore.event

    def collect(*args, **kwargs):
        stopped.append(kwargs["stopped"].__self__)
        raise original

    def record(store, cycle_id, kind, payload, **kwargs):
        if kind == "collection_worker_stopped":
            assert stopped[0].is_set()
            diagnostics.append(payload)
            if diagnostic_unavailable:
                raise RuntimeError("private-token=diagnostic-sink-secret")
        return event(store, cycle_id, kind, payload, **kwargs)

    monkeypatch.setattr(live_session, "collection_loop", collect)
    monkeypatch.setattr(live_session.ShadowStore, "event", record)
    result = live_session.run_manual(repository=tmp_path, datastore=tmp_path,
        profile=tmp_path / "profile.json", watchlist=tmp_path / "watchlist.txt",
        config=live_session.ResearchConfig(), clock=lambda: CLOSE - timedelta(minutes=1),
        sleep=lambda _: pytest.fail("Failed worker must not enter an operating loop"))
    status = json.loads((tmp_path / "intraday/manual-session.json").read_text())
    assert result["status"] == status["status"] == "FAILED"
    assert status["stop_reason"] == "worker_failure" and stopped[0].is_set()
    assert len(status["worker_errors"]) == len(diagnostics) == 1
    failure = status["worker_errors"][0]
    assert failure["worker"] == "collection" and failure["error_type"] == "RuntimeError"
    assert failure["message"] == str(original)
    assert failure["diagnostics"] == diagnostics[0]["diagnostics"]
    assert failure["diagnostics"]["root_cause"]["frames"][-1]["function"] == "collect"
    output = capsys.readouterr().out
    if diagnostic_unavailable:
        assert "collection_worker_stopped" in output and "diagnostic_failure" in output
    assert "private-token" not in output and "diagnostic-sink-secret" not in output


def test_collection_completion_scores_before_marking_market_close_stop(tmp_path, monkeypatch):
    inert_manual(monkeypatch)
    clock = [CLOSE - timedelta(seconds=1)]
    saved = []
    def publish(runner, **kwargs):
        saved.append(save_forecast(runner.store, session=kwargs["queue"].session_id))
        runner.store.record_candle(final_candle())
    def collect(store, **kwargs):
        clock[0] = CLOSE + timedelta(seconds=5)
        assert not kwargs["stopped"]()
        kwargs["on_completed"]({"final_complete": True})
        assert kwargs["stopped"]()
        assert store.revisions(saved[0]["forecast_id"])[-1]["status"] == "scored"
    monkeypatch.setattr(live_session, "forecasting_loop", publish)
    monkeypatch.setattr(live_session, "collection_loop", collect)
    result = live_session.run_manual(repository=tmp_path, datastore=tmp_path, profile=tmp_path / "profile.json",
        watchlist=tmp_path / "watchlist.txt", config=live_session.ResearchConfig(),
        clock=lambda: clock[0], sleep=lambda _: pytest.fail("Completed collection must stop"))
    assert result["status"] == "STOPPED" and result["stop_reason"] == "market_close"
    store = ShadowStore(tmp_path / "intraday/shadow.sqlite3")
    with store.connect() as db:
        record = db.execute("SELECT payload FROM events WHERE kind='final_saved_outcomes_scored'").fetchone()
    assert record is not None and json.loads(record[0])["scored"] == 1

