"""Execution cutoffs remain independent of immutable forecast/model evidence."""
from importlib import import_module

import pytest

from ml.intraday.intentions import IntentionStore
from ml.intraday.prediction import ContractError, digest, encoded
from ml.intraday.store import ShadowStore
from ml.intraday.technical_sizing import SizingResult
from ml.stock_direction_policy import stock_action
from tests.test_intraday_store import forecast


def saved_forecast(store, *, probability, pc="Atlas", symbol="AAPL", cycle="cycle"):
    return store.save_forecast(forecast(pc=pc, symbol=symbol, cycle_id=cycle,
        probability_not_down=probability, models=[
            dict(name=name, status="success", probability_not_down=probability) for name in ("a", "b")]))


def sized(forecast_value, side, *, status="sized"):
    value = dict(forecast_id=forecast_value["forecast_id"], symbol=forecast_value["symbol"], side=side,
        status=status, desired_quantity=4 if status == "sized" else None,
        calculated_at="2026-10-08T17:16:03Z", reason="probability_between_action_cutoffs")
    return SizingResult(digest(value), encoded(value))


@pytest.mark.parametrize("pc,symbol", [("Atlas", "AAPL"), ("Scout", "DOCU")])
@pytest.mark.parametrize("probability,side", [(.4999, "SELL"), (.5, "HOLD"), (.5001, "BUY")])
def test_initial_and_sizing_intentions_agree_on_exact_execution_cutoffs(tmp_path, pc, symbol, probability, side):
    store = ShadowStore(tmp_path / "evidence.sqlite3")
    original = saved_forecast(store, probability=probability, pc=pc, symbol=symbol)
    with store.connect() as db:
        import json
        initial = json.loads(db.execute("SELECT payload FROM intents").fetchone()[0])
    assert initial["side"] == side
    intentions = IntentionStore(store)
    result = intentions.save_sizing(original, sized(original, side, status="unavailable" if side == "HOLD" else "sized"))
    assert result["side"] == side
    if side == "HOLD":
        assert initial["disposition"] == result["disposition"] == "probability_between_action_cutoffs"
        assert intentions.get(original["forecast_id"]) is None
        assert result["desired_quantity"] is None and result["submitted_quantity"] == 0
        with pytest.raises(ContractError, match="Neutral forecast"):
            intentions.save_sizing(original, sized(original, "HOLD"))
    else:
        assert result["desired_quantity"] == 4
        assert intentions.get(original["forecast_id"]) == result


def test_existing_intention_and_forecast_recovery_keep_original_saved_side(tmp_path, monkeypatch):
    intention_module = import_module("ml.intraday.intentions")
    store_module = import_module("ml.intraday.store")
    # Reconstruct a saved intention created under the previous 50.00%=BUY rule.
    monkeypatch.setattr(intention_module, "stock_action", lambda probability: "BUY")
    monkeypatch.setattr(store_module, "stock_action", lambda probability: "BUY")
    store = ShadowStore(tmp_path / "evidence.sqlite3")
    original = saved_forecast(store, probability=.5)
    intentions = IntentionStore(store)
    sizing = sized(original, "BUY")
    previous = intentions.save_sizing(original, sizing)
    history = intentions.history(original["forecast_id"])
    monkeypatch.setattr(intention_module, "stock_action", stock_action)
    monkeypatch.setattr(store_module, "stock_action", stock_action)
    assert saved_forecast(store, probability=.5) == original
    assert store.save_forecast(forecast(probability_not_down=.5, models=[
        dict(name=name, status="success", probability_not_down=.5) for name in ("a", "b")]),
        intent={"side": "BUY", "desired_quantity": 4, "adjusted_quantity": 0,
                "disposition": "old_saved_intention"}) == original
    assert intentions.save_sizing(original, sizing) == previous
    assert intentions.history(original["forecast_id"]) == history
    recovered = intentions.record(original, event_key="recover", recorded_at="2026-10-08T17:16:04Z",
        disposition="recovering_saved_order", kind="recovery", evidence={"saved_order": True})
    assert recovered["side"] == "BUY" and recovered["desired_quantity"] == 4
    following = saved_forecast(store, probability=.5, cycle="next")
    held = intentions.save_sizing(following, sized(following, "HOLD", status="unavailable"))
    assert held["side"] == "HOLD" and held["desired_quantity"] is None
