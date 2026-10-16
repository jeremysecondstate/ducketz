"""Execution reads only relevant forecasts without weakening their validation."""
import json

import pytest

from ml.intraday.prediction import ContractError
from ml.intraday.store import ShadowStore
from test_intraday_store import forecast


def test_symbol_scope_preserves_default_order_and_validates_only_selected_rows(tmp_path, monkeypatch):
    store = ShadowStore(tmp_path / "scope.sqlite3")
    first = store.save_forecast(forecast(cycle_id="first"))
    other = store.save_forecast(forecast(cycle_id="other", symbol="AMZN"))
    second = store.save_forecast(forecast(cycle_id="second"))
    peer = store.save_forecast(forecast(cycle_id="peer", pc="Scout"))
    validate = ShadowStore._forecast_value
    visited = []

    def observed(row):
        visited.append(row[0])
        return validate(row)

    monkeypatch.setattr(ShadowStore, "_forecast_value", staticmethod(observed))
    assert store.forecasts(pc="Atlas", symbol="AAPL") == [first, second]
    assert visited == [first["forecast_id"], second["forecast_id"]]
    assert store.forecasts(symbol="AAPL") == [first, second, peer]
    assert store.forecasts(pc="Atlas") == [first, other, second]
    assert store.forecasts() == [first, other, second, peer]
    assert store.forecasts(pc="Atlas", symbol="AAPL' OR 1=1 --") == []


@pytest.mark.parametrize("corruption", ["payload", "index"])
def test_scoped_forecast_still_rejects_selected_corruption(tmp_path, corruption):
    store = ShadowStore(tmp_path / "scope.sqlite3")
    original = store.save_forecast(forecast())
    with store.connect() as db:
        if corruption == "payload":
            db.execute("UPDATE forecasts SET payload=?", (json.dumps(dict(original, probability_not_down=.1)),))
        else:
            db.execute("UPDATE forecasts SET issued_at='2026-10-08T17:16:04+00:00'")
    with pytest.raises(ContractError, match="forecast evidence hash or indexed identity"):
        store.forecasts(pc="Atlas", symbol="AAPL")
