"""Offline forward-reference integration; all sources and fits are fixtures."""
from copy import deepcopy
from dataclasses import replace
import json

import pandas as pd
import pytest

from ml.intraday import data, native_interval
from ml.intraday.forward_fill import CONTRACT, make_reference, policy_contract, verified_reference
from ml.intraday.minute_feed import QuarterHourMinuteFeed
from ml.intraday.prediction import ContractError, STEP, digest, instant
from ml.intraday.runtime import ResearchConfig, ShadowRunner
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import indicator_snapshot
from ml.intraday.starter_sizing import starter_policy
from tests.test_intraday_minute_feed import setup
from tests.test_intraday_native_feed import native_candle, complete
from tests.test_intraday_runtime import dataset, trained
from tests.test_intraday_store import forecast


def fixture_history(symbol="ABCL", reference="2026-10-14T16:00Z", count=48):
    ref = instant(reference)
    sessions = data.equity_sessions(ref - 5 * pd.Timedelta(days=1), ref)
    times = [t for t in pd.date_range(ref - 5 * pd.Timedelta(days=1), ref, freq="15min", inclusive="left")
             if any(s.contains(t) for s in sessions)][-count:]
    rows = [native_candle(symbol, t) for t in times]
    frame = pd.DataFrame(rows)
    for k in ("opened_at", "available_at"):
        frame[k] = pd.to_datetime(frame[k], utc=True)
    manifest = {"source_dataset": "EQUS.MINI", "native_interval_contract": native_interval.CONTRACT}
    frame.attrs["intraday_history"] = manifest
    return data.History(frame, manifest)


def fill_feed(tmp_path, monkeypatch):
    _, args, clock, calls = setup(tmp_path, monkeypatch, symbols=("ABCL",),
        now="2026-10-14T16:15:30.123456Z", start="2026-10-14T16:00Z", behavior=lambda *_: {"rows": ()})
    args.update(session_id="filled-reference", native_interval_symbols=("ABCL",),
                reference_fill_policy=policy_contract(), histories={"ABCL": fixture_history()})
    return QuarterHourMinuteFeed(**args), args, clock, calls


def test_empty_window_supplies_projection_without_changing_raw_history_or_ledger(tmp_path, monkeypatch):
    feed, args, clock, calls = fill_feed(tmp_path, monkeypatch)
    original = feed.histories["ABCL"].frame.copy(deep=True)
    outcome = complete(feed, clock)
    assert outcome["reason"] == "source_empty_interval"
    projected = feed.history_at("ABCL", feed.first)
    row = projected.frame.iloc[-1].to_dict()
    assert verified_reference(row) and row["close"] == original.iloc[-1].close
    assert row["synthetic"] and not row["no_trade"] and row["volume"] == 0
    pd.testing.assert_frame_equal(feed.histories["ABCL"].frame, original)
    assert not feed.store.observed_candles("ABCL")
    assert len(calls) == 2
    # A process recreation can project saved evidence without fetching again.
    restored = QuarterHourMinuteFeed(**args)
    assert verified_reference(restored.history_at("ABCL", feed.first).frame.iloc[-1].to_dict())
    assert len(calls) == 2
    with pytest.raises(ContractError, match="identity or source"):
        QuarterHourMinuteFeed(**{**args, "reference_fill_policy": None})


def test_actual_native_reference_takes_priority_over_projection(tmp_path, monkeypatch):
    feed, _, clock, _ = fill_feed(tmp_path, monkeypatch)
    complete(feed, clock)
    actual = native_candle("ABCL", feed.first)
    feed._merge("ABCL", [actual])
    frame = feed.history_at("ABCL", feed.first).frame
    assert not frame.iloc[-1].synthetic and frame.iloc[-1].close == actual["close"]


def test_runtime_persists_reference_provenance_and_keeps_outcomes_separate(tmp_path, monkeypatch):
    feed, _, clock, _ = fill_feed(tmp_path, monkeypatch)
    complete(feed, clock)
    history = feed.history_at("ABCL", feed.first)
    current = history.frame.iloc[-1].to_dict()
    def builder(frame, *, cutoff, reference_at, **kwargs):
        value = dataset(frame, cutoff=cutoff, reference_at=reference_at)
        value.inference.update(reference_open=current["open"], information_available_at=current["available_at"].isoformat(),
            source=feed.source_id,source_dataset=feed.dataset, reference_fill_policy=policy_contract(),
            reference_price_basis="forward_filled_last_genuine_close")
        value.manifest["reference_fill_policy"] = policy_contract()
        return value
    store = ShadowStore(tmp_path/"runtime.sqlite3")
    runner = ShadowRunner(store=store,pc="Scout",symbols=("ABCL",),config=ResearchConfig(models=("logistic",)),
        dataset_builder=builder,trainer=trained,clock=clock)
    result = runner.run_cycle({"ABCL": history},reference_at=feed.first, mode="live", experiment="fixture")
    # Inspect durable data rather than depending on the runner summary shape.
    with store.connect() as db:
        saved = store._forecast_value(db.execute('SELECT id,pc,symbol,cycle_id,reference_at,issued_at,payload FROM forecasts').fetchone())
    provenance = saved["reference_provenance"]
    assert provenance["forward_fill_contract"] == CONTRACT and provenance["synthetic"]
    assert provenance["price_basis"] == "forward_filled_last_genuine_close"
    assert provenance["evidence"]["responses"][-1]["request_ended_at"].endswith(".123456+00:00")
    # Separate simple saved fixture exercises target classification.
    other = store.save_forecast(forecast(mode="live", pc="Scout",source_dataset="EQUS.MINI",
        source_id=feed.source_id,native_interval_contract=native_interval.CONTRACT,
        reference_provenance=provenance))
    target = native_candle("AAPL", other["target_open"])
    observed = store.observe(other,as_of=target["available_at"],candle=target)
    assert observed["status"] == "synthetic"


def test_sizing_accepts_only_frozen_valid_current_carry(tmp_path, monkeypatch):
    feed, _, clock, _ = fill_feed(tmp_path, monkeypatch)
    complete(feed, clock)
    h = feed.history_at("ABCL", feed.first)
    from ml.intraday.forward_fill import _clean
    rows = _clean(h.frame.to_dict(orient="records"))
    cutoff = instant(clock()).isoformat()
    snapshot = {"cutoff":cutoff,"candles":rows,"dataset_scope":{"synthetic_policy":"exclude"},
        "manifest":{"synthetic_policy":"exclude","reference_fill_policy":policy_contract(),
                    "manual_model_recipe":{"reference_fill_policy":policy_contract()}},
        "inference":{"symbol":"ABCL","reference_at":feed.first.isoformat(),"reference_open":rows[-1]["open"],
                     "source":feed.source_id,"source_dataset":feed.dataset}}
    policy = starter_policy("ABCL")
    def compute(s):
        return indicator_snapshot(s["candles"],symbol="ABCL",reference_at=feed.first,data_cutoff=cutoff,
            indicator_specs=policy.indicators,window_policy=policy.window,forecast_snapshot=s,forecast_snapshot_hash=digest(s)).payload()
    ready=compute(snapshot)
    assert ready["status"]=="ready" and ready["provenance"]["synthetic_rows"]==1
    assert ready["provenance"]["missing_policy"]=="latest_genuine_close_current_reference_only"
    broken=deepcopy(snapshot);broken["candles"][-1]["close"]+=1
    assert compute(broken)["status"]=="unavailable"
    forged=deepcopy(snapshot);forged["candles"][-1]["synthetic"]=False
    assert compute(forged)["reason"]=="invalid_forward_filled_reference"
    undeclared=deepcopy(snapshot);undeclared["manifest"].pop("reference_fill_policy")
    assert compute(undeclared)["status"]=="unavailable"
