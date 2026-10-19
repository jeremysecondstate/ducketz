"""Execution reads only the immutable forecasts selected by its own queue."""
import json

import pytest

from ml.intraday.live_session import ExecutionQueue
from ml.intraday.prediction import ContractError
from ml.intraday.store import ShadowStore
from tests.test_intraday_store import forecast


def test_exact_forecast_selection_retains_actor_order_and_hash_checks(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "forecasts.sqlite3")
    first = store.save_forecast(forecast(cycle_id="first"))
    second = store.save_forecast(forecast(cycle_id="second", symbol="MSFT"))
    foreign = store.save_forecast(forecast(cycle_id="foreign", pc="Scout"))
    checked = []
    original = store._forecast_value

    def check(row):
        checked.append(row[0])
        return original(row)

    monkeypatch.setattr(store, "_forecast_value", check)
    assert store.forecasts(pc="Atlas", forecast_ids=[second["forecast_id"], first["forecast_id"],
        second["forecast_id"], foreign["forecast_id"]]) == [first, second]
    assert checked == [first["forecast_id"], second["forecast_id"]]
    checked.clear()
    assert store.forecasts(pc="Atlas", symbol="MSFT", forecast_ids=[first["forecast_id"], second["forecast_id"]]) == [second]
    assert checked == [second["forecast_id"]]
    with store.connect() as db:
        payload = dict(first, reference_open="1")
        db.execute("UPDATE forecasts SET payload=? WHERE id=?", (json.dumps(payload), first["forecast_id"]))
    with pytest.raises(ContractError, match="hash or indexed identity"):
        store.forecasts(pc="Atlas", forecast_ids=[first["forecast_id"]])


def test_empty_execution_queue_never_decodes_history(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "empty.sqlite3")
    store.save_forecast(forecast(mode="live", models=[
        dict(name="a", status="success", probability_not_down=.7, retained_evidence="a" * 200000),
        dict(name="b", status="success", probability_not_down=.74)]))
    queue = ExecutionQueue(store, "manual")
    monkeypatch.setattr(store, "_forecast_value", lambda row: pytest.fail("Empty queue decoded old model history"))
    assert queue.due("2026-10-08T17:17:00Z") == []


def test_execution_queue_decodes_only_its_selected_forecasts(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "selected.sqlite3")
    old = store.save_forecast(forecast(mode="live", cycle_id="old"))
    selected = store.save_forecast(forecast(mode="live", cycle_id="selected", symbol="MSFT"))
    queue = ExecutionQueue(store, "manual")
    queue.enqueue(selected)
    checked = []
    original = store._forecast_value
    monkeypatch.setattr(store, "_forecast_value", lambda row: (checked.append(row[0]), original(row))[1])
    assert queue.due("2026-10-08T17:17:00Z") == [selected]
    assert checked == [selected["forecast_id"]]
    assert old["forecast_id"] not in checked


def test_large_exact_selection_does_not_exceed_sqlite_parameter_limit(tmp_path):
    store = ShadowStore(tmp_path / "large.sqlite3")
    selected = store.save_forecast(forecast())
    ids = [str(index).zfill(64) for index in range(2000)] + [selected["forecast_id"]]
    assert store.forecasts(pc="Atlas", forecast_ids=ids) == [selected]
