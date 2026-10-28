"""Nested scientific stores must retain the actual Atlas data root."""
from types import SimpleNamespace
from datetime import timedelta

import pandas as pd
import pytest

from ml.intraday import runtime, candle_collection, data
from ml.intraday.prediction import STEP, instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_recovered_population import primary, wrap_primary, START, END, NOW, POPULATION_CONTRACT
from tests.test_intraday_collection_cadence import Clock, ProviderFixture, observed


def test_nested_collection_store_uses_explicit_original_gap_and_recovery_roots(tmp_path, monkeypatch):
    roots, saved = [], []
    original_root = tmp_path / "original-datastore"
    store = SimpleNamespace(path=original_root / "intraday/system-1a/collection.sqlite3",
        observed_candles=lambda symbol: [], event=lambda *a, **k: None, record_candle=saved.append)
    previous = primary()
    previous.update(opened_at=START - pd.Timedelta(minutes=15), available_at=START)
    frame = pd.DataFrame([previous])
    histories = {"TEST": data.History(frame, {"source_dataset": "EQUS.MINI"})}
    monkeypatch.setattr(runtime.LiveHistories, "_refresh_symbol", lambda *a: None)
    monkeypatch.setattr(data, "recover_databento_candle_step",
        lambda *a, **k: {"final": True, "reason": "source_empty", "status": "source_empty"})

    class Gap:
        def __init__(self, root, **kwargs):
            roots.append(("gap", root))

        def recover(self, *args):
            return {"assessment": {}, "retry": False}

    monkeypatch.setattr("ml.intraday.gap_recovery.GapRecovery", Gap)
    monkeypatch.setattr("ml.intraday.recovered_population.recovery_candle", lambda *a: wrap_primary(primary()))
    monkeypatch.setattr("ml.intraday.recovered_population.save_recovery_candle",
        lambda root, candle: roots.append(("save", root)))
    incoming = runtime.LiveHistories(histories=histories, provider=SimpleNamespace(dataset="EQUS.MINI"),
        store=store, journal=SimpleNamespace(responses=lambda **kw: []), reference_at=START,
        population_contract=POPULATION_CONTRACT, restore_eagerly=False, clock=lambda: NOW,
        bounded=True, datastore=original_root)
    result = incoming.get("TEST")
    assert roots == [("gap", original_root), ("save", original_root)]
    assert len(saved) == 1
    assert result.manifest["source_dataset"] == "EQUS.MINI"
    assert not (original_root / "intraday/intraday").exists()


def test_continuous_collector_resumes_old_pending_after_close_and_respects_step_bound(tmp_path, monkeypatch):
    clock = Clock("2026-07-11T00:01:00Z")  # Prior session closed17:00 Pacific.
    captured = []

    class Incoming:
        def __init__(self, **kwargs):
            assert kwargs["datastore"] == tmp_path / "actual-data"
            assert not kwargs["stopped"]()
            captured.append(kwargs)
            self.store, self.reference = kwargs["store"], kwargs["reference_at"]

        def bind_cycle(self, identity):
            pass

        def get(self, symbol):
            self.store.record_candle(observed(symbol, self.reference, clock()))

    monkeypatch.setattr(candle_collection, "LiveHistories", Incoming)
    store = ShadowStore(tmp_path / "system-1a/collection.sqlite3")
    collector = candle_collection.CandleCollector(store, histories={"AAPL": object()},
        provider=SimpleNamespace(dataset="EQUS.MINI"), journal=object(), symbols=("AAPL",),
        session_id="durable-day", started_at="2026-07-10T22:00:00Z",
        close_at="2026-07-11T00:00:00Z", clock=clock, continuous=True,
        datastore=tmp_path / "actual-data", max_steps=1)
    old = instant("2026-07-10T21:00:00Z")
    with store.connect() as db:
        db.execute("INSERT INTO intraday_candle_collection VALUES(?,?,?,'pending',?,NULL)",
            ("durable-day", "AAPL", old.isoformat(), old.isoformat()))
    first = collector.pass_once()
    assert len(captured) == 1 and not first["final_complete"]
    for _ in range(20):
        before = len(captured)
        result = collector.pass_once()
        assert len(captured) - before <= 1
        if result["final_complete"]:
            break
    assert result["final_complete"]
    references = {row["reference_at"] for row in captured}
    assert old in references and instant("2026-07-10T23:45:00Z") in references
    assert all(old <= value <= instant("2026-07-10T23:45:00Z") for value in references)
