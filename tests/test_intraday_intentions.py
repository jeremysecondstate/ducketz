from dataclasses import replace
import json

import pytest

from ml.intraday.capacity import clip_to_capacity
from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import SizingResult
from tests.test_intraday_store import forecast, candle


def setup(tmp_path):
    store = ShadowStore(tmp_path / "evidence.sqlite3")
    f = store.save_forecast(forecast())
    intents = IntentionStore(store)
    value = dict(forecast_id=f["forecast_id"], symbol="AAPL", side="BUY", status="sized",
                 desired_quantity=4, calculated_at="2026-10-08T17:16:03Z")
    sized = SizingResult(digest(value), encoded(value))
    return store, f, intents, sized


def test_smaller_possible_and_zero_intents_remain_scoreable(tmp_path):
    store, f, intents, sized = setup(tmp_path)
    saved = intents.save_sizing(f, sized)
    for index, cash in enumerate(("100", "0")):
        cap = clip_to_capacity(symbol="AAPL", side="BUY", desired_quantity=4, limit_price="50",
            available_cash=cash, available_shares=3, capacity_evidence_id=f"snapshot-{index}",
            observed_at="2026-10-08T17:16:04Z", calculated_at="2026-10-08T17:16:05Z")
        result = intents.record(f, event_key="capacity:" + cap.decision_id,
            recorded_at="2026-10-08T17:16:05Z", disposition=cap.disposition, kind="capacity",
            capacity=cap, evidence={"snapshot": cap.capacity_evidence_id})
        assert result["desired_quantity"] == 4
        assert result["adjusted_quantity"] == (2 if index == 0 else 0)
        assert result["submitted_quantity"] == result["filled_quantity"] == 0
        assert result["realized_pnl"] is None
    assert IntentionStore(store).get(f["forecast_id"]) == saved
    assert intents.save_sizing(f, sized) == saved
    assert len(intents.history(f["forecast_id"])) == 3
    outcome = store.observe(f, as_of="2026-10-08T17:46:00Z", candle=candle(close="101"))
    assert outcome["reference_to_target_return"] == "0.01"
    assert store.forecast("cycle", "AAPL") == f


def test_first_sizing_immutable_and_no_fabricated_zero(tmp_path):
    store, f, intents, sized = setup(tmp_path)
    unavailable = sized.payload() | {"status":"unavailable", "desired_quantity":None, "reason":"quote_unavailable"}
    result = intents.save_sizing(f, SizingResult(digest(unavailable), encoded(unavailable)))
    assert result["desired_quantity"] is result["adjusted_quantity"] is None
    assert intents.get(f["forecast_id"]) is None
    intents.save_sizing(f, sized)
    changed = sized.payload() | {"desired_quantity":8}
    with pytest.raises(ContractError, match="immutable"):
        intents.save_sizing(f, SizingResult(digest(changed), encoded(changed)))


def test_event_recovery_rejects_reused_identity_changed_evidence(tmp_path):
    store, f, intents, sized = setup(tmp_path)
    intents.save_sizing(f, sized)
    kw = dict(event_key="capacity-check-1", recorded_at="2026-10-08T17:17:00Z",
              disposition="awaiting_capacity", evidence={"error":"temporary"}, kind="capacity_retry")
    first = intents.record(f, **kw)
    assert intents.record(f, **(kw | {"recorded_at":"2026-10-08T17:18:00Z"})) == first
    with pytest.raises(ContractError, match="identity reused"):
        intents.record(f, **(kw | {"evidence":{"error":"different"}}))
    with pytest.raises(ContractError, match="backdated"):
        intents.record(f, **(kw | {"event_key":"other", "recorded_at":"2026-10-08T17:16:30Z"}))


def test_tampered_sizing_and_forecast_rejected(tmp_path):
    store, f, intents, sized = setup(tmp_path)
    intents.save_sizing(f, sized)
    with store.connect() as db:
        saved = json.loads(db.execute("SELECT payload FROM intraday_intentions").fetchone()[0])
        db.execute("UPDATE intraday_intentions SET payload=?", (encoded(saved | {"desired_quantity":8}),))
    with pytest.raises(ContractError, match="intention changed"):
        intents.get(f["forecast_id"])
    with pytest.raises(ContractError, match="original saved forecast"):
        intents.record(f | {"symbol":"IONQ"}, event_key="changed", recorded_at="2026-10-08T17:17:00Z",
                       disposition="waiting", evidence={"test":True}, kind="test")


def test_distinct_events_with_same_facts_retain_identity_and_time(tmp_path):
    store, f, intents, sized = setup(tmp_path)
    intents.save_sizing(f, sized)
    kw = dict(disposition="waiting", evidence={"test":True}, kind="test")
    first = intents.record(f, event_key="attempt-1", recorded_at="2026-10-08T17:17:00Z", **kw)
    second = intents.record(f, event_key="attempt-2", recorded_at="2026-10-08T17:18:00Z", **kw)
    assert first["event_identity"] != second["event_identity"]
    assert len(intents.history(f["forecast_id"])) == 3
    assert intents.record(f,event_key="attempt-1",recorded_at="2026-10-08T17:19:00Z",**kw) == first
    with store.connect() as db:
        db.execute("UPDATE intents SET recorded_at=? WHERE id=?",("2026-10-08T17:16:00+00:00",digest(second)))
    with pytest.raises(ContractError,match="event changed"):
        intents.history(f["forecast_id"])
