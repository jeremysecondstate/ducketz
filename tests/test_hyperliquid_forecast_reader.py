"""Read local forecast artifacts without loading or constructing runtimes."""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.hyperliquid_forecast_reader import read_forecast


NOW = 1790395230.0
DATA_ID = "20260926T040005Z-1234abcd"
MODEL_ID = "20260926T035000Z-abcdef12"


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


@pytest.fixture
def artifacts(tmp_path):
    policy = SimpleNamespace(data_root=tmp_path, max_forecast_age_seconds=900, max_model_age_seconds=86400)
    model = tmp_path / "_models" / "BTC" / "15m" / "h4"
    run = model / "runs" / MODEL_ID
    run.mkdir(parents=True)
    prediction = {"prediction_id": "test-forecast", "coin": "BTC", "interval": "15m", "horizon_bars": 4,
        "p_not_down": .6, "p_down": .4, "created_at_utc": utc(NOW-20), "decision_close_utc": utc(NOW-30),
        "target_close_utc": utc(NOW-30+3600), "qualified": False, "model_id": MODEL_ID, "data_run_id": DATA_ID}
    (model / "latest_prediction.json").write_text(json.dumps(prediction), encoding="utf-8")
    (run / "record.json").write_text(json.dumps({"coin": "BTC", "interval": "15m", "horizon_bars": 4,
        "model_id": MODEL_ID, "trained_at_utc": utc(NOW-600)}), encoding="utf-8")
    data = tmp_path / "BTC" / "15m" / "runs" / DATA_ID
    data.mkdir(parents=True)
    pd.DataFrame({"close_time": [pd.Timestamp(utc(NOW-30))], "close": [100.0],
                  "volatility_log_return_20": [.01]}).to_parquet(data / "features.parquet", index=False)
    return policy, model, prediction


def test_reads_pinned_forecast_without_a_runtime(artifacts):
    policy, _, expected = artifacts
    prediction, sigma = read_forecast("BTC", NOW, policy, "15m", 4)
    assert prediction == {**expected, "_valid_until_epoch": NOW + 870}
    assert sigma == pytest.approx(.02)


@pytest.mark.parametrize("change", [{"p_down": .7}, {"qualified": None}, {"target_close_utc": utc(NOW)},
    {"decision_close_utc": utc(NOW+10)}, {"model_id": "../outside"}, {"data_run_id": "../outside"}])
def test_rejects_invalid_or_stale_provenance(artifacts, change):
    policy, model, prediction = artifacts
    (model / "latest_prediction.json").write_text(json.dumps({**prediction, **change}), encoding="utf-8")
    with pytest.raises(ValueError):
        read_forecast("BTC", NOW, policy, "15m", 4)


def test_optional_feature_loader_preserves_existing_paper_cache_contract(artifacts):
    policy, _, _ = artifacts
    calls = []
    def load(coin, run_id):
        calls.append((coin, run_id))
        return pd.DataFrame({"close_time": [pd.Timestamp(utc(NOW-30))], "close": [100],
                             "volatility_log_return_20": [.02]})
    _, sigma = read_forecast("BTC", NOW, policy, "15m", 4, feature_loader=load)
    assert calls == [("BTC", DATA_ID)]
    assert sigma == pytest.approx(.04)


@pytest.mark.parametrize("forecast_age,model_age,remaining", [
    (900, 86400, 870), (4500, 86400, 3570), (900, 610, 10),
])
def test_expiry_is_earliest_authoritative_constraint(artifacts, forecast_age, model_age, remaining):
    policy, model, prediction = artifacts
    policy.max_forecast_age_seconds = forecast_age
    policy.max_model_age_seconds = model_age
    # A publication cannot supply its own later expiry or overwrite model age.
    (model / "latest_prediction.json").write_text(json.dumps(
        {**prediction, "_valid_until_epoch": NOW + 1_000_000}), encoding="utf-8")
    actual, _ = read_forecast("BTC", NOW, policy, "15m", 4)
    assert actual["_valid_until_epoch"] == pytest.approx(NOW + remaining)
