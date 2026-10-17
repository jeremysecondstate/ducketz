"""Offline integration of the explicitly selected native interval contract."""
from datetime import timedelta
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.intraday import data, live_session, native_interval
from ml.intraday.minute_feed import QuarterHourMinuteFeed
from ml.intraday.prediction import ContractError, STEP, instant
from ml.intraday.store import ShadowStore
from tests.test_intraday_minute_feed import setup
from tests.test_intraday_store import forecast, candle


def sparse_rows(reference):
    return tuple(dict(opened_at=(reference + timedelta(minutes=n)).isoformat(),
        open=10 + n / 100, high=11, low=9, close=10 + n / 100, volume=100)
        for n in (2, 7, 13))


def selected_feed(tmp_path, monkeypatch, **kwargs):
    _, args, clock, calls = setup(tmp_path, monkeypatch, **kwargs)
    args.update(session_id="native-test", native_interval_symbols=("ABCL",))
    return QuarterHourMinuteFeed(**args), args, clock, calls


def complete(feed, clock, *, symbol="ABCL", reference=None):
    reference = instant(reference) if reference else feed.first
    for _ in range(4):
        feed.step()
        if (reference, symbol) in feed.outcomes:
            return feed.outcomes[reference, symbol]
        clock.advance(4)
    pytest.fail("Covered exact interval did not resolve within bounded fake attempts")


def test_selected_sparse_interval_uses_observed_prices_without_previous_fill(tmp_path, monkeypatch):
    feed, _, clock, calls = selected_feed(tmp_path, monkeypatch,
        behavior=lambda reference, *_: {"rows": sparse_rows(reference)})
    previous_symbols = []
    original = feed._previous
    def previous(symbol, reference):
        previous_symbols.append(symbol)
        return original(symbol, reference)
    monkeypatch.setattr(feed, "_previous", previous)
    outcome = complete(feed, clock)
    native = feed.store.observed_candles("ABCL")[0]
    assert outcome["status"] == "observed" and len(calls) == 2
    assert native["native_interval_contract"] == native_interval.CONTRACT
    assert (native["open"], native["close"], native["volume"]) == (10.02, 10.13, 300)
    assert native["observed_constituents"] == 3 and native["source_empty_constituents"] == 12
    assert native["synthetic_constituents"] == 0 and native["synthetic"] is False
    assert "ABCL" not in previous_symbols
    assert not feed.store.observed_candles("DBX")
    assert feed.outcomes[feed.first, "DBX"]["reason"] == "no_prior_known_price"
    history = feed.history_source(feed.first).get("ABCL")
    assert len(history.frame) == 1 and history.manifest["native_interval_contract"] == native_interval.CONTRACT


def test_source_empty_native_interval_never_invents_price_and_restores_without_refetch(tmp_path, monkeypatch):
    feed, args, clock, calls = selected_feed(tmp_path, monkeypatch,
        now="2026-10-15T00:00Z", start="2026-10-14T23:45Z", behavior=lambda *_: {"rows": ()})
    outcome = complete(feed, clock)
    assert outcome["status"] == "resolved_no_usable_15m"
    assert outcome["reason"] == "source_empty_interval"
    assert outcome["native_interval_contract"] == native_interval.CONTRACT
    assert len(calls) == 2 and not feed.store.observed_candles()
    with pytest.raises(ContractError, match="no usable 15m"):
        feed.history_source(feed.first).get("ABCL")
    restored = QuarterHourMinuteFeed(**args)
    assert restored.step()["status"] == "closed" and len(calls) == 2
    assert restored.outcomes[feed.first, "ABCL"] == outcome


def test_unavailable_native_coverage_keeps_retrying_without_synthetic_candle(tmp_path, monkeypatch):
    feed, _, clock, calls = selected_feed(tmp_path, monkeypatch,
        behavior=lambda *_: {"status": "unavailable_coverage"})
    for _ in range(3):
        feed.step()
        clock.advance(4)
    assert len(calls) == 3 and not feed.outcomes and not feed.store.observed_candles()
    with pytest.raises(data.AcquisitionPending):
        feed.history_source(feed.first).get("ABCL")


@pytest.mark.parametrize("scope", [("ABCL", "ABCL"), ("OTHER",), "ABCL", (None,)])
def test_feed_rejects_unbound_or_ambiguous_native_scope(tmp_path, monkeypatch, scope):
    _, args, _, _ = setup(tmp_path, monkeypatch)
    args.update(session_id="invalid-native", native_interval_symbols=scope)
    with pytest.raises(ContractError, match="explicit subset"):
        QuarterHourMinuteFeed(**args)


def test_selected_scope_is_frozen_for_a_saved_feed_session(tmp_path, monkeypatch):
    feed, args, clock, calls = selected_feed(tmp_path, monkeypatch)
    complete(feed, clock)
    assert len(calls) == 1
    args["histories"] = {}
    restored = QuarterHourMinuteFeed(**args)
    assert restored.step()["status"] == "waiting" and len(calls) == 1
    with pytest.raises(ContractError, match="identity or source differs"):
        QuarterHourMinuteFeed(**{**args, "native_interval_symbols": ()})
    with feed.store.connect() as db:
        value = json.loads(db.execute("SELECT payload FROM events WHERE cycle_id=? AND kind=?",
            (feed.identity, "minute_feed_initialized")).fetchone()[0])
    assert value["native_interval_symbols"] == ["ABCL"]
    assert value["native_interval_contract"] == native_interval.CONTRACT


def native_candle(symbol="AAPL", reference="2026-10-08T17:30Z"):
    opened = instant(reference)
    responses = tuple(data.IntervalResponse(str(n), symbol, "databento:EQUS.MINI:ohlcv-1m", "EQUS.MINI",
        opened.isoformat(), (opened + STEP).isoformat(), (opened + STEP + timedelta(seconds=n)).isoformat(),
        (opened + STEP + timedelta(seconds=n)).isoformat(), "success", rows=sparse_rows(opened),
        coverage_open=opened.isoformat(), coverage_close=(opened + STEP).isoformat()) for n in (4, 8))
    session = next(s for s in data.equity_sessions(opened, opened + STEP) if s.contains(opened))
    return native_interval.resolve_native_interval(responses, session=session)["candle"]


def test_restore_keeps_legacy_and_native_histories_separate_without_deleting_evidence(tmp_path, monkeypatch):
    feed, args, clock, _ = selected_feed(tmp_path, monkeypatch, symbols=("ABCL",))
    native = native_candle("ABCL", feed.first)
    legacy = candle(symbol="ABCL", opened_at=feed.first.isoformat(), available_at=clock().isoformat(),
        source_id=feed.source_id, source_dataset=feed.dataset, open=50, high=51, low=49, close=50)
    for value in (native, legacy):
        feed.store.record_candle(value)
    combined = pd.DataFrame([native, legacy])
    for field in ("opened_at", "available_at"):
        combined[field] = pd.to_datetime(combined[field], utc=True)
    def history():
        return {"ABCL": data.History(combined.copy(), {"source_dataset": "EQUS.MINI"})}
    selected = QuarterHourMinuteFeed(**{**args, "histories": history()})
    default = QuarterHourMinuteFeed(**{**args, "session_id": "legacy-only", "histories": history(),
                                      "native_interval_symbols": ()})
    assert selected.histories["ABCL"].frame.close.tolist() == [10.13]
    assert default.histories["ABCL"].frame.close.tolist() == [50]
    assert "native_interval_contract" not in default.histories["ABCL"].manifest
    assert len(feed.store.observed_candles("ABCL")) == 2


@pytest.mark.parametrize("change", [dict(native_interval_contract=None), dict(symbol="DBX"),
    dict(source_dataset="OTHER"), dict(source_id="other"), dict(opened_at="2026-10-14T16:45Z")])
def test_resolver_cannot_persist_candle_from_wrong_frozen_scope(tmp_path, monkeypatch, change):
    feed, _, _, _ = selected_feed(tmp_path, monkeypatch, symbols=("ABCL",))
    value = {**native_candle("ABCL", feed.first), **change}
    monkeypatch.setattr(native_interval, "resolve_native_interval",
        lambda *args, **kwargs: {"status": "observed", "candle": value})
    with pytest.raises(ContractError, match="frozen interval contract"):
        feed.step()
    assert not feed.store.observed_candles()


@pytest.mark.parametrize("incoming", [False, True])
@pytest.mark.parametrize("native_forecast", [False, True])
def test_scoring_requires_forecast_interval_contract_on_saved_and_incoming_targets(tmp_path, incoming, native_forecast):
    store = ShadowStore(tmp_path / "score.sqlite3")
    saved = store.save_forecast(forecast(mode="live", pc="Scout", source_dataset="EQUS.MINI",
        source_id="databento:EQUS.MINI:ohlcv-1m",
        **({"feature_version": native_interval.FEATURE_VERSION} if native_forecast else {})))
    native = native_candle()
    legacy = candle(source_id=saved["source_id"], source_dataset=saved["source_dataset"])
    correct, wrong = (native, legacy) if native_forecast else (legacy, native)
    values = [wrong]
    class Incoming:
        def bind_cycle(self, identity):
            assert identity == "score:" + saved["forecast_id"]
        def get(self, symbol):
            frame = pd.DataFrame(values)
            frame["opened_at"] = pd.to_datetime(frame.opened_at, utc=True)
            return SimpleNamespace(frame=frame)
    def run():
        done = []
        live_session.scoring_loop(store, histories={"AAPL": object()} if incoming else {}, provider=object(),
            journal=object(), session_id="score-contract", stopped=lambda: bool(done),
            clock=lambda: "2026-10-09T00:01Z", pause=lambda _: done.append(True), pc="Scout",
            history_source=lambda _: Incoming())
    if not incoming:
        store.record_candle(wrong)
    run()
    assert not any(r["status"] == "scored" for r in store.revisions(saved["forecast_id"]))
    if incoming:
        values.append(correct)
    else:
        store.record_candle(correct)
    run()
    assert store.revisions(saved["forecast_id"])[-1]["status"] == "scored"


@pytest.mark.parametrize("selected", [False, True])
def test_manual_start_uses_latched_symbol_recipe_for_history_and_feed(tmp_path, monkeypatch, selected):
    from ml.intraday import live_model_recipe, minute_feed
    from tests.test_intraday_live_session import scout_manual_fixture
    args, _, _ = scout_manual_fixture(tmp_path, monkeypatch)
    loads, feeds, selections = [], [], []
    monkeypatch.setattr(data, "load_local_history", lambda root, symbol, **kwargs: loads.append(("legacy", symbol, kwargs)) or object())
    monkeypatch.setattr(native_interval, "load_native_interval_history",
        lambda root, symbol, **kwargs: loads.append(("native", symbol, kwargs)) or object())
    monkeypatch.setattr(live_model_recipe, "prepare_manual_models", lambda config, **kwargs: (config, {}))
    def scope(runtime):
        selections.append(runtime)
        return ("DOCU",) if selected else ()
    monkeypatch.setattr(live_model_recipe, "native_interval_symbols_for_runtime", scope)
    class Feed:
        history_source = None
        def __init__(self, **kwargs):
            feeds.append(kwargs)
        def run(self, **kwargs):
            pass
    monkeypatch.setattr(minute_feed, "QuarterHourMinuteFeed", Feed)
    result = live_session.run_manual(**args)
    assert result["status"] == "STOPPED"
    assert selections == [{}]
    assert loads == [("native" if selected else "legacy", "DOCU", {"source_dataset": "EQUS.MINI"})]
    assert feeds[0]["native_interval_symbols"] == (("DOCU",) if selected else ())
