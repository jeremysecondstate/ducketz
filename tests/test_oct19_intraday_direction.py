"""Both independent PCs use the cutoff and retain their original order facts."""
from dataclasses import replace
import json

import pytest

from ml.intraday import intentions as intentions_module
from ml.intraday import starter_sizing, store as store_module, technical_sizing
from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest
from ml.intraday.starter_sizing import size_forecast
from ml.intraday.store import ShadowStore
from ml.stock_direction_policy import stock_direction
from ml.stock_trader.horizon_ledger import ReservationState
from test_intraday_starter_sizing import fixture as sizing_fixture
from tests.test_intraday_store import forecast as forecast_fixture


def prepare(tmp_path, *, pc, probability, intent=None):
    original, snapshot, equity, quote = sizing_fixture(probability=probability)
    store = ShadowStore(tmp_path / (pc + ".sqlite3"))
    cycle = pc.lower() + "-direction-fixture"
    frozen_id, frozen = store.freeze_inputs(cycle, original["symbol"], snapshot)
    payload = forecast_fixture(pc=pc, cycle_id=cycle, mode="live")
    payload.update({key: value for key, value in original.items() if key != "forecast_id"})
    payload.update(snapshot_hash=frozen_id, information_available_at=original["issued_at"],
                   models=[dict(name=name, status="success", probability_not_down=probability) for name in ("a", "b")])
    issued = store.save_forecast(payload, intent=intent)
    sizing = size_forecast(issued, forecast_snapshot=frozen, account_equity=equity, quote=quote,
        calculated_at=issued["issued_at"], maximum_quote_age_seconds=60.)
    return store, issued, sizing


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
@pytest.mark.parametrize("probability,side", [(.5001, "BUY"), (.4999, "SELL"), (.5, "HOLD"),
                                             (.50005, "HOLD"), (.49995, "HOLD")])
def test_both_pcs_forecast_sizing_and_intention_use_the_same_probability_cutoffs(tmp_path, pc, probability, side):
    store, issued, sized = prepare(tmp_path, pc=pc, probability=probability)
    assert issued["pc"] == pc and issued["probability_not_down"] == probability
    assert sized.payload()["side"] == side
    with store.connect() as db:
        initial_intent = json.loads(db.execute("SELECT payload FROM intents ORDER BY rowid LIMIT 1").fetchone()[0])
    assert initial_intent["side"] == side
    intents = IntentionStore(store)
    saved = intents.save_sizing(issued, sized)
    if side == "HOLD":
        assert saved["side"] == "HOLD" and saved["desired_quantity"] is None
        assert saved["submitted_quantity"] == saved["filled_quantity"] == 0
        assert saved["disposition"] == "neutral_probability_no_direction"
        assert intents.get(issued["forecast_id"]) is None
    else:
        assert saved["side"] == side and saved["desired_quantity"] > 0
        assert intents.get(issued["forecast_id"]) == saved


@pytest.mark.parametrize("pc", ["Atlas", "Scout"])
@pytest.mark.parametrize("probability,side", [(.50005, "BUY"), (.49995, "SELL"), (.5, "BUY")])
def test_policy_update_preserves_issued_forecast_original_sizing_and_order_recovery(tmp_path, monkeypatch, pc, probability, side):
    def legacy_direction(value):
        return "BULLISH" if float(value) >= .5 else "BEARISH"

    with monkeypatch.context() as old_policy:
        for module in (store_module, intentions_module, starter_sizing, technical_sizing):
            old_policy.setattr(module, "stock_direction", legacy_direction)
        store, issued, sized = prepare(tmp_path, pc=pc, probability=probability, intent={"side": side})
        original_intention = IntentionStore(store).save_sizing(issued, sized)
    assert stock_direction(probability) == "NO_EDGE"
    original_hash = digest({key: value for key, value in issued.items() if key != "forecast_id"})
    reopened = ShadowStore(store.path)
    assert reopened.forecast(issued["cycle_id"], issued["symbol"]) == issued
    assert original_hash == issued["forecast_id"]
    assert reopened.save_forecast(issued, intent={"side": side}) == issued
    intents = IntentionStore(reopened)
    assert intents.get(issued["forecast_id"]) == original_intention
    assert intents.save_sizing(issued, sized) == original_intention
    native = ReservationState("native-order", "native-allocation", issued["symbol"], "15m", issued["forecast_id"],
        side, original_intention["desired_quantity"], "100", 1, "PARTIAL", "actual-broker-id-fixture",
        "idempotency-fixture", issued["cycle_id"], issued["issued_at"])
    result = intents.record(issued, event_key="recovery:actual-order", recorded_at=issued["issued_at"],
        disposition="order_status_reconciled", evidence={"native_reservation_id": native.reservation_id},
        kind="recovery", order=native)
    assert result["side"] == side and result["filled_quantity"] == 1
    assert result["broker_order_id"] == native.broker_order_id
    assert result["desired_quantity"] == original_intention["desired_quantity"]
    assert intents.get(issued["forecast_id"]) == original_intention
    with pytest.raises(ContractError, match="differs from intended"):
        intents.record(issued, event_key="recovery:wrong-side", recorded_at=issued["issued_at"],
            disposition="order_status_reconciled", evidence={"fixture": True}, kind="recovery",
            order=replace(native, side="SELL" if side == "BUY" else "BUY"))
