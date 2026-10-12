from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json

import pytest

from ml.intraday.prediction import (
    Candle, ContractError, PredictionHistory, TARGET_VERSION, UnresolvedPolicy,
    issue_prediction, sizing_features,
)
from ml.intraday.prototype import AtlasPrototype, AtlasScope
from ml.intraday.sizing import size

DAY = date(2026, 10, 8)


def candle(opened="2026-10-08T10:00:00-07:00", *, symbol="AAPL", price="100", available=None):
    start = datetime.fromisoformat(opened)
    return Candle(symbol, opened, available or (start + timedelta(minutes=15, seconds=1)).isoformat(),
                  price, "110", "90", price, "1000", "offline-fixture")


def predict(rows=None, **kwargs):
    defaults = dict(symbol="AAPL", issued_at="2026-10-08T10:15:02-07:00", eligible_session=DAY,
                    model_version="mock-not-down-v1", model_target_version=TARGET_VERSION,
                    infer=lambda inputs: "0.7")
    return issue_prediction(rows or [candle()], **(defaults | kwargs))


def test_exact_target_and_equality_label_with_revision_history(tmp_path):
    prediction = predict()
    assert prediction.reference_at == "2026-10-08T17:00:00+00:00"
    assert prediction.data_through == "2026-10-08T17:15:00+00:00"
    assert prediction.target_open == "2026-10-08T17:30:00+00:00"
    assert prediction.target_close == "2026-10-08T17:45:00+00:00"
    history = PredictionHistory(tmp_path / "issues.sqlite3")
    history.save(prediction)
    history.save(prediction)
    assert history.observe(prediction.prediction_id, as_of="2026-10-08T10:44:00-07:00", candle=None,
                           observation_id="pending")["status"] == "pending"
    assert history.observe(prediction.prediction_id, as_of="2026-10-08T10:45:00-07:00", candle=None,
                           observation_id="missing")["status"] == "missing"
    equality = candle("2026-10-08T10:30:00-07:00")
    first = history.observe(prediction.prediction_id, as_of="2026-10-08T10:45:02-07:00", candle=equality,
                            observation_id="target-original")
    assert first["status"] == "completed" and first["target_not_down"] is True
    corrected = history.observe(prediction.prediction_id, as_of="2026-10-08T11:00:00-07:00",
                                candle=replace(equality, close="99"), observation_id="target-correction")
    assert corrected["target_not_down"] is False
    assert len(history.revisions(prediction.prediction_id)) == 4
    assert history.get(prediction.prediction_id) == prediction
    with pytest.raises(ContractError, match="immutable"):
        history.save(predict(infer=lambda _: "0.2"))
    with pytest.raises(ContractError, match="identity"):
        history.observe(prediction.prediction_id, as_of="2026-10-08T11:00:00-07:00",
                        candle=equality, observation_id="target-correction")


def test_available_information_and_model_target_are_explicit():
    future = candle("2026-10-08T10:15:00-07:00")
    result = predict([candle(), future])
    assert len(json.loads(result.inputs_json)["candles"]) == 1
    with pytest.raises(ContractError, match="Fresh"):
        predict([replace(candle(), available_at="2026-10-08T10:15:03-07:00")])
    with pytest.raises(ContractError, match="not-down"):
        predict(model_target_version="strictly-positive-legacy")
    with pytest.raises(ContractError, match="unavailable"):
        predict(additional_inputs={"holding": (5, "2026-10-08T10:16:00-07:00")})
    with pytest.raises(ContractError, match="Conflicting"):
        predict([candle(), replace(candle(), close="99")])
    with pytest.raises(ContractError, match="quarter-hour"):
        predict([candle("2026-10-08T10:01:00-07:00")])
    with pytest.raises(ContractError, match="Timezone"):
        predict(issued_at="2026-10-08T10:15:02")
    with pytest.raises(ContractError, match="Probability"):
        predict(infer=lambda _: "1.1")


@pytest.mark.parametrize("issued", ["2026-10-08T04:00:02-07:00", "2026-10-08T16:45:02-07:00"])
def test_unresolved_session_boundaries_are_not_fabricated(issued):
    with pytest.raises(UnresolvedPolicy):
        predict(issued_at=issued)


def test_future_rows_cannot_change_features_and_gaps_are_visible():
    first = datetime(2026, 10, 8, 4, 30, tzinfo=timezone(timedelta(hours=-7)))
    rows = [candle((first + timedelta(minutes=15 * i)).isoformat(), price=str(98 + i / 10)) for i in range(23)]
    now = "2026-10-08T10:15:02-07:00"
    kwargs = dict(symbol="AAPL", issued_at=now, expected_volume=("1000", now), holdings=(7, now), recent_executed=(2, now))
    features = sizing_features(rows, **kwargs)
    assert features == sizing_features(rows + [candle("2026-10-08T10:15:00-07:00", price="90")], **kwargs)
    assert {"momentum_rate", "momentum_acceleration", "rsi_rate", "relative_volume_acceleration",
            "distance_vwap", "distance_sma20", "atr_relative_price"} <= features.keys()
    assert features["holdings"] == 7 and features["recent_executed_quantity"] == 2
    with pytest.raises(ContractError, match="contiguous"):
        sizing_features(rows[:10] + rows[11:], **kwargs)
    with pytest.raises(ContractError, match="unavailable"):
        sizing_features(rows, **(kwargs | {"holdings": (7, "2026-10-08T10:30:00-07:00")}))


def sized(**kwargs):
    defaults = dict(symbol="AAPL", side="BUY", hourly_reference=10, reference_version="fixture-reference",
                    score="0.36", score_version="fixture-score", inputs={"momentum": 0.01})
    return size(**(defaults | kwargs))


def test_nearest_share_is_not_flooring_and_no_minimum_veto():
    assert sized().desired_quantity == 4
    assert Decimal(sized().raw_quantity) == Decimal("3.60")
    assert sized(score="0.04").desired_quantity == 0
    with pytest.raises(UnresolvedPolicy, match="Half-share"):
        sized(score="0.25")
    assert sized(score="0.25", half_share_ties="half_even").desired_quantity == 2
    assert sized(score="0.25", half_share_ties="half_up").desired_quantity == 3
    with pytest.raises(UnresolvedPolicy, match="equals hourly"):
        sized(hourly_reference=1, score="0.6")
    assert sized(hourly_reference=1, score="0.6", rounded_equality="allow").desired_quantity == 1
    with pytest.raises(ContractError):
        sized(hourly_reference=0)
    with pytest.raises(ContractError):
        sized(score="1")


def test_atlas_full_cycle_retry_reuses_issues_and_reports_missing(tmp_path):
    symbols = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST", "CROX", "PATH", "TWST", "IONQ")
    scope = AtlasScope(symbols, symbols + ("ABCL",))
    prototype = AtlasPrototype(scope=scope, history=PredictionHistory(tmp_path / "issues.sqlite3"))
    calls = []
    def infer(inputs):
        calls.append(inputs)
        return "0.6"
    kwargs = dict(issued_at="2026-10-08T10:15:02-07:00", eligible_session=DAY, model_version="mock-v1",
                  model_target_version=TARGET_VERSION, infer_by_symbol={s: infer for s in symbols})
    first = prototype.issue_cycle([candle(symbol=s) for s in symbols[:-1]], **kwargs)
    assert len(first["predictions"]) == 10 and set(first["failures"]) == {"IONQ"}
    second = prototype.issue_cycle([candle(symbol=s) for s in symbols], **kwargs)
    assert len(second["predictions"]) == 11 and not second["failures"]
    assert len(calls) == 11
    assert second["predictions"]["AAPL"] == first["predictions"]["AAPL"]
    refreshed = prototype.issue_cycle([candle("2026-10-08T10:15:00-07:00", symbol=s) for s in symbols],
                                      **(kwargs | {"issued_at": "2026-10-08T10:30:02-07:00"}))
    assert len(refreshed["predictions"]) == 11
    assert refreshed["predictions"]["AAPL"].prediction_id != first["predictions"]["AAPL"].prediction_id


def test_scope_reads_bindings_without_changing_them(tmp_path):
    files = {"profile.json": {"actor": "Atlas", "machine": "pc-original", "symbols": ["AAPL"]},
             "ownership.json": {"machine_id": "pc-original", "coordinator_id": "pc-original",
                                "participants": {"pc-original": ["AAPL"], "pc-new": ["ABCL"]}}}
    for name, value in files.items():
        (tmp_path / name).write_text(json.dumps(value))
    (tmp_path / "watch.txt").write_text("# local\nAAPL\n")
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    scope = AtlasScope.from_local_files(profile=tmp_path / "profile.json", watchlist=tmp_path / "watch.txt",
                                       ownership=tmp_path / "ownership.json")
    assert scope.atlas_symbols == ("AAPL",) and scope.gameplan_symbols == ("AAPL", "ABCL")
    assert all(p.read_bytes() == content for p, content in before.items())


def test_history_rejects_modified_prediction_bytes_and_preserves_final_information(tmp_path):
    history = PredictionHistory(tmp_path / "issues.sqlite3")
    prediction = predict()
    with pytest.raises(ContractError, match="identity"):
        history.save(replace(prediction, reference_open="101"))
    result = history.save_final_information(checkpoint_id="session-final", candles=[candle("2026-10-08T16:30:00-07:00")],
                                            observed_at="2026-10-08T16:45:02-07:00", eligible_session=DAY)
    assert result["next_open_target"] is None
    with pytest.raises(ContractError, match="immutable"):
        history.save_final_information(checkpoint_id="session-final", candles=[candle("2026-10-08T16:30:00-07:00", price="99")],
                                       observed_at="2026-10-08T16:45:02-07:00", eligible_session=DAY)
