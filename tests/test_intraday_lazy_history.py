"""Live callers defer restoration to the requested symbol's validated get step."""
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday import data, live_session, runtime
from ml.intraday.prediction import ContractError, STEP, digest, encoded, instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import candle, forecast


OPEN = instant("2026-10-08T17:30:00Z")
NOW = instant("2026-10-08T17:50:00Z")


def observation(**changes):
    value = dict(symbol="AAPL", opened_at=OPEN.isoformat(),
        available_at="2026-10-08T17:45:04Z", availability_kind="actual", interval_seconds=900,
        open=100, high=110, low=90, close=101, volume=100, synthetic=False, no_trade=False,
        source_id="databento:EQUS.MINI:ohlcv-1m", source_dataset="EQUS.MINI", evidence_json="{}")
    value.update(changes)
    return value


def frame(rows):
    result = pd.DataFrame(rows)
    for column in ("opened_at", "available_at"):
        result[column] = pd.to_datetime(result[column], utc=True, format="mixed")
    return result


def revisions():
    return [observation(),
        observation(interval_seconds=60, close=99, available_at="2026-10-08T17:46:00Z"),
        observation(observed_constituents=14, synthetic_constituents=0,
                    close=98, available_at="2026-10-08T17:47:00Z"),
        observation(close=97, available_at="2026-10-08T18:00:00Z")]


def raw_rows(store):
    with store.connect() as connection:
        return connection.execute("SELECT id,payload FROM candles ORDER BY rowid").fetchall()


def save_raw(store, rows):
    with store.connect() as connection:
        for row in rows:
            connection.execute("INSERT INTO candles VALUES(?,?,?,?,?)",
                (digest(row), row["symbol"], row["opened_at"], row["available_at"], encoded(row)))


def test_constructor_defaults_to_eager_and_lazy_constructor_does_no_symbol_reads(monkeypatch):
    seen = []
    monkeypatch.setattr(runtime.LiveHistories, "_refresh_symbol", lambda self, symbol: seen.append(symbol))
    histories = {symbol: SimpleNamespace(manifest={}) for symbol in ("AAPL", "MU")}
    arguments = dict(histories=histories, provider=SimpleNamespace(dataset="EQUS.MINI"),
                     store=object(), journal=object(), reference_at=OPEN)
    runtime.LiveHistories(**arguments, restore_eagerly=False)
    assert seen == []
    runtime.LiveHistories(**arguments)
    assert seen == ["AAPL", "MU"]


def test_lazy_get_matches_eager_sanitization_and_reuses_exact_durable_observation(tmp_path, monkeypatch):
    original_refresh = runtime.LiveHistories._refresh_symbol
    refreshed = []
    def refresh(self, symbol):
        refreshed.append(symbol)
        return original_refresh(self, symbol)
    monkeypatch.setattr(runtime.LiveHistories, "_refresh_symbol", refresh)
    results = []
    for eager in (True, False):
        store = ShadowStore(tmp_path / str(eager) / "observations.sqlite3")
        saved = revisions() + [observation(opened_at=(OPEN - STEP).isoformat(),
            available_at=(OPEN + STEP).isoformat(), synthetic=True,
            evidence_json='{"legacy_confirmations":2}')]
        save_raw(store, saved)
        before = raw_rows(store)
        empty = frame([observation()]).iloc[:0].copy()
        histories = {symbol: data.History(empty.copy(), {"source_dataset": "EQUS.MINI"})
                     for symbol in ("AAPL", "MU")}
        other = histories["MU"]
        journal = SimpleNamespace(responses=lambda **kw: pytest.fail("Durable reference was reacquired"))
        refreshed.clear()
        incoming = runtime.LiveHistories(histories=histories,
            provider=SimpleNamespace(dataset="EQUS.MINI"), store=store, journal=journal,
            reference_at=OPEN, clock=lambda: NOW, bounded=True, reuse_saved=True,
            restore_eagerly=eager)
        if not eager:
            assert refreshed == [] and histories["MU"] is other
        result = incoming.get("AAPL")
        assert refreshed == (["AAPL", "MU", "AAPL"] if eager else ["AAPL"])
        if not eager:
            assert histories["MU"] is other
        assert len(result.frame) == 1 and result.frame.iloc[0].close == 101
        assert result.frame.iloc[0].opened_at == pd.Timestamp(OPEN)
        assert result.frame.iloc[0].available_at == pd.Timestamp(saved[0]["available_at"])
        assert raw_rows(store) == before
        results.append(result.frame)
    pd.testing.assert_frame_equal(results[0], results[1])


def test_lazy_get_checks_stop_before_any_restoration(monkeypatch):
    monkeypatch.setattr(runtime.LiveHistories, "_refresh_symbol",
                        lambda *args: pytest.fail("Stopped get restored history"))
    incoming = runtime.LiveHistories(histories={"AAPL": SimpleNamespace(manifest={})},
        provider=SimpleNamespace(dataset="EQUS.MINI"), store=object(), journal=object(),
        reference_at=OPEN, stopped=lambda: True, restore_eagerly=False)
    with pytest.raises(InterruptedError, match="before the next attempt"):
        incoming.get("AAPL")


def test_lazy_requested_symbol_still_rejects_a_foreign_source(tmp_path):
    history = data.History(frame([observation()]), {"source_dataset": "XNAS.ITCH"})
    incoming = runtime.LiveHistories(histories={"AAPL": history},
        provider=SimpleNamespace(dataset="EQUS.MINI"), store=ShadowStore(tmp_path / "shadow.sqlite3"),
        journal=object(), reference_at=OPEN, restore_eagerly=False)
    with pytest.raises(ContractError, match="single historical source"):
        incoming.get("AAPL")


def test_forecasting_constructs_lazy_history_without_changing_scope(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    histories = {symbol: object() for symbol in ("AAPL", "MU")}
    captured, runs = [], []
    def incoming(**kwargs):
        captured.append(kwargs)
        return object()
    monkeypatch.setattr(live_session, "LiveHistories", incoming)
    monkeypatch.setattr(live_session, "eligible_reference", lambda reference: True)
    runner = SimpleNamespace(store=store,
        config=SimpleNamespace(publication_offset_seconds=30, population_contract="fixture"),
        run_cycle=lambda history, **kwargs: runs.append(kwargs))
    live_session.forecasting_loop(runner, histories=histories, provider=object(), journal=object(),
        queue=SimpleNamespace(session_id="manual", enqueue=lambda value: None),
        stopped=lambda: bool(runs), clock=lambda: "2026-10-12T17:16:00Z", pause=lambda _: None)
    assert len(captured) == 1 and captured[0]["restore_eagerly"] is False
    assert captured[0]["histories"] is histories
    assert captured[0]["population_contract"] == "fixture"
    assert runs[0]["reference_at"] == instant("2026-10-12T17:00:00Z")
    assert runs[0]["allow_execution"] is True


def test_scoring_constructs_only_requested_symbol_history_and_opts_into_lazy(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    saved = store.save_forecast(forecast(mode="live"))
    histories = {symbol: object() for symbol in ("AAPL", "MU")}
    captured, done = [], []
    class Incoming:
        def __init__(self, **kwargs):
            captured.append(kwargs)
        def bind_cycle(self, identity):
            assert identity == "score:" + saved["forecast_id"]
        def get(self, symbol):
            assert symbol == "AAPL"
            value = candle(close="101")
            value["opened_at"] = pd.Timestamp(value["opened_at"])
            return SimpleNamespace(frame=pd.DataFrame([value]))
    monkeypatch.setattr(live_session, "LiveHistories", Incoming)
    live_session.scoring_loop(store, histories=histories, provider=object(), journal=object(),
        session_id="manual", stopped=lambda: bool(done), clock=lambda: "2026-10-09T00:01:00Z",
        pause=lambda _: done.append(True))
    assert len(captured) == 1 and captured[0]["restore_eagerly"] is False
    assert captured[0]["histories"] == {"AAPL": histories["AAPL"]}
    assert captured[0]["reference_at"] == saved["target_open"]
    assert store.revisions(saved["forecast_id"])[-1]["status"] == "scored"
