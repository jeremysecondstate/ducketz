"""Offline data-only iteration boundaries; every acquisition is a fake."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
import json
import socket
import sqlite3
import sys
import threading

import pytest

from ml.intraday import data_renewal as subject


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Offline renewal fixture attempted network access")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)


class Store:
    def __init__(self, path):
        self.path = path
        self.events = []

    @contextmanager
    def connect(self):
        with sqlite3.connect(self.path) as db:
            yield db

    def event(self, *args, **kwargs):
        self.events.append((args, kwargs))


class Planned(BaseException):
    pass


class Renewal:
    def __init__(self, session, state=None, role="candles"):
        self.session = session
        self.state = state or {}
        self.request = {"role": role}
        self.checkpoints = []

    def stopped(self):
        return False

    def checkpoint(self, state, due):
        self.checkpoints.append((state, due))
        raise Planned


def module(monkeypatch, name, **values):
    value = ModuleType(name)
    value.__dict__.update(values)
    monkeypatch.setitem(sys.modules, name, value)
    return value


def session():
    return dict(actor="Atlas", session_id="data-atlas-1a:fixture", symbols=["AAPL", "IONQ"],
                started_at="2026-10-28T14:16:00+00:00", close_at="2026-10-29T00:00:00+00:00",
                config=dict(models=["fixture"], source_dataset="EQUS.MINI", population_contract="original",
                            publication_offset_seconds=30))


@pytest.mark.parametrize("workers,expected", [(1, [("A", "B", "C")]),
    (2, [("A", "C"), ("B",)]), (3, [("A",), ("B",), ("C",)]),
    (11, [("A",), ("B",), ("C",)])])
def test_frozen_allocation_has_no_extra_concurrency_or_hidden_backlog(workers, expected):
    assert [subject.lane_symbols(("A", "B", "C"), workers, index) for index in range(len(expected))] == expected


@pytest.mark.parametrize("workers,index", [(0, 0), (True, 0), (1, 1), (2, -1), (1, "0")])
def test_invalid_or_changed_worker_ownership_rejects(workers, index):
    with pytest.raises(ValueError):
        subject.lane_symbols(("A", "B"), workers, index)


@pytest.mark.parametrize("last,ready,expected", [(None, set(), "A"), ("A", set(), "B"),
    ("B", {"A"}, "C"), ("C", set(), "A"), ("A", {"A", "B", "C"}, None)])
def test_symbol_fairness_survives_fresh_worker(last, ready, expected):
    assert subject.next_symbol(("A", "B", "C"), last, ready) == expected


def test_market_failed_original_references_and_alternating_turn_survive():
    state = dict(known_references=["old"], pending_references=["old"], collect_turn=False)
    known, pending, turn = subject.market_state(state, "new")
    assert known == {"old", "new"} and list(pending) == ["old", "new"] and turn is False
    again = subject.market_state(dict(known_references=sorted(known), pending_references=list(pending), collect_turn=turn), "new")
    assert again[0] == known and list(again[1]) == list(pending) and again[2] is False
    assert state == dict(known_references=["old"], pending_references=["old"], collect_turn=False)


def test_checkpoint_retains_original_lifetime_and_exact_absolute_due():
    from ml.intraday.prediction import instant
    original = session(); renewal = Renewal(original)
    with pytest.raises(Planned):
        subject._checkpoint(renewal, {"last_symbol": "IONQ"}, instant(original["close_at"]),
                            lambda: "2026-10-28T21:00:00Z", 3)
    assert renewal.checkpoints == [({"last_symbol": "IONQ", "data_close_at": original["close_at"]}, "2026-10-28T21:00:03+00:00")]
    assert renewal.session == original


def test_data_day_recovery_keeps_original_frozen_configuration(tmp_path):
    store = Store(tmp_path / "collection.sqlite3"); original = session()
    subject._collection_schema(store)
    day = subject._admit_collection_day(store, original, original["close_at"], original["started_at"])
    assert subject._pending_collection(store) == 1
    subject._admit_collection_day(store, original, original["close_at"], "2026-10-28T22:00:00Z")
    with store.connect() as db:
        row = db.execute("SELECT close_at,configuration,symbols,last_attempt_at FROM system_1a_collection_days WHERE identity=?", (day,)).fetchone()
    assert row == (original["close_at"], json.dumps(original["config"], sort_keys=True, separators=(",", ":")),
                   json.dumps(original["symbols"], sort_keys=True, separators=(",", ":")), original["started_at"])


@pytest.mark.parametrize("field,value", [("symbols", ["AAPL"]), ("config", {"models": ["different"]})])
def test_existing_collection_day_cannot_be_relabelled_or_gate_new_worker(tmp_path, field, value):
    store = Store(tmp_path / "collection.sqlite3"); original = session()
    subject._collection_schema(store)
    subject._admit_collection_day(store, original, original["close_at"], original["started_at"])
    subject._admit_collection_day(store, {**original, field: value}, original["close_at"], original["started_at"])
    with store.connect() as db:
        row = db.execute("SELECT configuration,symbols FROM system_1a_collection_days").fetchone()
    assert json.loads(row[0]) == original["config"] and json.loads(row[1]) == original["symbols"]


def candle_environment(tmp_path, monkeypatch, *, state=None, old_day=False, pass_once=None):
    from ml.intraday.prediction import instant
    original = session(); renewal = Renewal(original, state)
    store = Store(tmp_path / "collection.sqlite3"); subject._collection_schema(store)
    subject._admit_collection_day(store, original, original["close_at"], original["started_at"])
    if old_day:
        subject._admit_collection_day(store, original, "2026-10-28T00:00:00Z", original["started_at"])
    with store.connect() as db:
        db.execute("CREATE TABLE intraday_candle_collection(session_id TEXT,status TEXT)")
    calls = []
    class Config(SimpleNamespace):
        def validate(self): return self
    class Collector:
        def __init__(self, *args, **kwargs): calls.append(kwargs)
        def pass_once(self):
            if pass_once: pass_once()
            return {"final_complete": False, "saved": 1}
    module(monkeypatch, "ml.intraday.runtime", ResearchConfig=lambda **fields: Config(**fields))
    module(monkeypatch, "ml.intraday.candle_collection", CandleCollector=Collector)
    module(monkeypatch, "ml.intraday.data", AcquisitionJournal=lambda path: ("journal", path))
    module(monkeypatch, "app.services.databento_market_data", DatabentoMarketDataProvider=lambda: SimpleNamespace(dataset="EQUS.MINI"))
    monkeypatch.setattr(subject, "_history", lambda *args: {"original": "history"})
    args = dict(root=tmp_path, base=tmp_path, session=original,
                config=Config(**original["config"]), store=store, close=instant(original["close_at"]),
                clock=lambda: "2026-10-28T21:00:00Z", pause=lambda _: None)
    return renewal, calls, args


@pytest.mark.parametrize("turn,old_selected", [(0, False), (1, False), (2, True), (3, False), (5, True)])
def test_candle_old_day_fairness_counter_survives_worker_generation(tmp_path, monkeypatch, turn, old_selected):
    renewal, calls, args = candle_environment(tmp_path, monkeypatch, state={"turn": turn}, old_day=True)
    with pytest.raises(Planned): subject._candles(renewal, **args)
    assert (calls[0]["close_at"].isoformat() == "2026-10-28T00:00:00+00:00") is old_selected
    assert calls[0]["continuous"] is True and calls[0]["max_steps"] == 1
    assert renewal.checkpoints[0][0]["turn"] == turn + 1


def test_inflight_candle_finishes_before_checkpoint_without_cancellation(tmp_path, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def blocked():
        entered.set(); assert release.wait(5); finished.set()
    renewal, calls, args = candle_environment(tmp_path, monkeypatch, pass_once=blocked)
    observed = []
    def run():
        try: subject._candles(renewal, **args)
        except Planned: observed.append("checkpoint")
    thread = threading.Thread(target=run)
    try:
        thread.start(); assert entered.wait(5)
        assert renewal.checkpoints == [] and not finished.is_set()
        release.set(); thread.join(5)
        assert not thread.is_alive() and finished.is_set() and observed == ["checkpoint"]
    finally:
        release.set(); thread.join(5)


def test_market_streams_close_before_quarter_checkpoint_and_keep_failed_queue(tmp_path, monkeypatch):
    from ml.intraday.prediction import instant
    original = session(); renewal = Renewal(original)
    store = Store(tmp_path / "collection.sqlite3"); subject._collection_schema(store)
    subject._admit_collection_day(store, original, original["close_at"], original["started_at"])
    now = [instant("2026-10-28T21:00:30Z")]; calls = []
    class Collector:
        def __init__(self, *args, **kwargs): pass
        def start(self): calls.append("start"); return {}
        def collect(self, reference): calls.append(("collect", reference.isoformat())); raise ValueError("fake missing input")
        def recover_pending(self): calls.append("recover"); return None
        def close(self): calls.append("close")
    module(monkeypatch, "ml.intraday.market_inputs", MarketInputCollector=Collector)
    module(monkeypatch, "ml.intraday.market_input_worker", _failure_details=lambda exc, at, **kwargs: {"error_type": type(exc).__name__})
    module(monkeypatch, "ml.intraday.live_session", latest_completed_reference=lambda at, **kwargs: instant("2026-10-28T20:45:00Z") if instant(at) < instant("2026-10-28T21:15:30Z") else instant("2026-10-28T21:00:00Z"))
    def pause(seconds):
        from datetime import timedelta
        now[0] += timedelta(seconds=450)
    with pytest.raises(Planned):
        subject._market(renewal, root=tmp_path, base=tmp_path, session=original,
            config=SimpleNamespace(publication_offset_seconds=30), store=store,
            close=instant(original["close_at"]), clock=lambda: now[0], pause=pause)
    assert calls == ["start", ("collect", "2026-10-28T20:45:00+00:00"), "recover", "close"]
    state = renewal.checkpoints[0][0]
    assert state["pending_references"] == ["2026-10-28T20:45:00+00:00"]
    assert state["known_references"] == ["2026-10-28T20:45:00+00:00"] and state["collect_turn"] is True


def test_unknown_candle_failure_does_not_create_renewal_receipt(tmp_path, monkeypatch):
    def failure(): raise RuntimeError("fake unexpected worker error")
    renewal, calls, args = candle_environment(tmp_path, monkeypatch, pass_once=failure)
    with pytest.raises(RuntimeError, match="unexpected"):
        subject._candles(renewal, **args)
    assert renewal.checkpoints == []


def test_concurrent_history_reads_reconstruct_exactly_once_with_original_cutoff(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls, results, errors = [], [], []
    original = object()
    def load(root, symbol, **kwargs):
        calls.append((symbol, kwargs)); entered.set(); assert release.wait(5); return original
    module(monkeypatch, "ml.intraday.data", AcquisitionPending=RuntimeError, load_local_history=load)
    module(monkeypatch, "ml.intraday.coverage", RECOVERY_POPULATION_CONTRACT="original")
    histories = subject._history(tmp_path, Store(tmp_path / "history.sqlite3"), session(),
        SimpleNamespace(source_dataset="EQUS.MINI", population_contract="original"))
    def read():
        try: results.append(histories.get("AAPL"))
        except BaseException as exc: errors.append(exc)
    threads = [threading.Thread(target=read) for _ in range(6)]
    try:
        for thread in threads: thread.start()
        assert entered.wait(5)
        assert len(calls) == 1
        release.set()
        for thread in threads: thread.join(5)
        assert not errors and results == [original] * 6 and len(calls) == 1
        assert calls[0][1]["recovery_cutoff"].isoformat() == session()["started_at"]
    finally:
        release.set()
        for thread in threads: thread.join(5)


def test_candle_rollover_freezes_new_window_history_cutoff_once(tmp_path, monkeypatch):
    from ml.intraday.prediction import instant
    renewal, calls, args = candle_environment(tmp_path, monkeypatch)
    now = ["2026-10-29T11:16:00Z"]
    args["clock"] = lambda: now[0]
    args["close"] = instant("2026-10-30T00:00:00Z")
    subject._admit_collection_day(args["store"], renewal.session, args["close"], now[0])
    cutoffs = []
    monkeypatch.setattr(subject, "_history", lambda root, store, session, config: cutoffs.append(session["started_at"]) or {})
    with pytest.raises(Planned): subject._candles(renewal, **args)
    renewal.state = renewal.checkpoints[-1][0]
    now[0] = "2026-10-29T11:16:05Z"
    with pytest.raises(Planned): subject._candles(renewal, **args)
    assert cutoffs == ["2026-10-29T11:16:00+00:00"] * 2
    assert renewal.session["started_at"] == "2026-10-28T14:16:00+00:00"


def test_retained_collection_uses_original_symbols_and_recipe_after_local_change(tmp_path, monkeypatch):
    renewal, calls, args = candle_environment(tmp_path, monkeypatch)
    original = dict(renewal.session)
    args["session"] = {**original, "symbols": ["AAPL"], "config": {**original["config"], "population_contract": "changed"}}
    captured = []
    monkeypatch.setattr(subject, "_history", lambda root, store, session, config:
        captured.append((list(session["symbols"]), config.population_contract, session["started_at"])) or {})
    with pytest.raises(Planned): subject._candles(renewal, **args)
    assert captured == [(["AAPL", "IONQ"], "original", original["started_at"])]
    assert calls[0]["symbols"] == ("AAPL", "IONQ") and calls[0]["population_contract"] == "original"
