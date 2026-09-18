import json

import joblib
import numpy as np
import pandas as pd
import pytest

from ml.gameplan_probability_target import (
    LEGACY_COST_TARGET, RAW_DIRECTION_TARGET, observed_probability_target,
    probability_target_contract, probability_target_metadata, resolve_probability_target,
)
from ml.gameplan_development_selection import logistic_regularization_candidates
from ml.nightly_gameplan import MODEL_GROUPS, _verify_probability_target_metadata


@pytest.mark.parametrize("change,raw,cost", [(.0005, 1, 0), (-.0005, 0, 0), (0, 0, 0), (.002, 1, 1)])
def test_raw_direction_and_cost_adjusted_targets_have_distinct_frozen_meanings(change, raw, cost):
    assert observed_probability_target(change, .001, RAW_DIRECTION_TARGET) == raw
    assert observed_probability_target(change, .001, LEGACY_COST_TARGET) == cost
    assert observed_probability_target(change, .001) == cost


def test_absent_historical_contract_stays_cost_adjusted_and_unknown_contract_rejected():
    assert probability_target_contract({}) == LEGACY_COST_TARGET
    assert resolve_probability_target(np.nan) == LEGACY_COST_TARGET
    assert probability_target_metadata(RAW_DIRECTION_TARGET)["gameplan_variant"] == "YG"
    with pytest.raises(ValueError, match="Unknown"):
        resolve_probability_target("unversioned-direction")
    with pytest.raises(ValueError, match="share one"):
        probability_target_contract(pd.DataFrame({"probability_target_contract": [RAW_DIRECTION_TARGET, np.nan]}))


@pytest.mark.parametrize("group", ["1h", "4h", "1d", "1w"])
def test_preregistered_regularization_grid_expands_only_new_intraday_targets(group):
    grid = (.001, .01, .1, 1.)
    assert logistic_regularization_candidates(group, probability_target=RAW_DIRECTION_TARGET) == grid
    assert logistic_regularization_candidates(group) == (grid if group == "1d" else (1.,))


def _probability_publication(tmp_path):
    metadata = probability_target_metadata(RAW_DIRECTION_TARGET)
    config = dict(metadata)
    cohort = pd.DataFrame({"observed_return": [.0005, -.0005], "assumed_round_trip_cost": [.001, .001],
                          "target": [1, 0], "target_raw_price_direction": [1, 0],
                          "target_cost_adjusted_positive": [0, 0], **metadata})
    cohort.to_parquet(tmp_path / "forecasts.parquet", index=False)
    (tmp_path / "gameplan.json").write_text(json.dumps(metadata))
    reports = {}
    outputs = {name: {} for name in ("gameplan.json", "forecasts.parquet", "model-reports.json")}
    for group in MODEL_GROUPS:
        model = f"model-{group}.joblib"
        joblib.dump(dict(metadata), tmp_path / model)
        reports[group] = {**metadata, "model_file": {"path": model}}
        name = f"training-cohort-{group}.parquet"
        cohort.to_parquet(tmp_path / name, index=False)
        outputs.update({model: {}, name: {}})
    (tmp_path / "model-reports.json").write_text(json.dumps(reports))
    return {"configuration": config, "output_files": outputs, "target_column": "target_raw_price_direction"}, metadata


@pytest.mark.parametrize("corruption", ["cost-label", "fit-label", "model", "forecast", "receipt", "stripped-config"])
def test_raw_publication_rejects_target_provenance_mismatch(tmp_path, corruption):
    manifest, receipt = _probability_publication(tmp_path)
    _verify_probability_target_metadata(tmp_path, manifest, receipt)
    if corruption in {"cost-label", "fit-label"}:
        path = tmp_path / "training-cohort-1h.parquet"
        frame = pd.read_parquet(path)
        frame.loc[0, "target_cost_adjusted_positive" if corruption == "cost-label" else "target"] ^= 1
        frame.to_parquet(path, index=False)
    elif corruption == "model":
        joblib.dump(probability_target_metadata(LEGACY_COST_TARGET), tmp_path / "model-1h.joblib")
    elif corruption == "forecast":
        path = tmp_path / "forecasts.parquet"
        frame = pd.read_parquet(path).assign(probability_target_contract=LEGACY_COST_TARGET)
        frame.to_parquet(path, index=False)
    elif corruption == "stripped-config":
        manifest["configuration"] = {}
    else:
        receipt = probability_target_metadata(LEGACY_COST_TARGET)
    with pytest.raises(RuntimeError):
        _verify_probability_target_metadata(tmp_path, manifest, receipt)


def test_historical_publication_verification_does_not_invent_new_labels(tmp_path):
    _verify_probability_target_metadata(tmp_path, {"configuration": {}})


@pytest.mark.parametrize("contract", [LEGACY_COST_TARGET, RAW_DIRECTION_TARGET])
def test_cli_pins_explicit_probability_contract_without_fitting(tmp_path, monkeypatch, contract):
    from ml.nightly_gameplan import main
    captured = {}
    monkeypatch.setattr("ml.nightly_gameplan.run_nightly_gameplan_once",
                        lambda root, **kwargs: captured.update(kwargs))
    assert main(["--datastore", str(tmp_path), "--stock-only", "--independent-stock-horizons",
                 "--probability-target-contract", contract]) == 0
    assert captured["probability_target_contract"] == contract
    assert logistic_regularization_candidates("1h", probability_target=LEGACY_COST_TARGET) == (1.,)


def test_raw_direction_fit_uses_new_labels_and_saves_exact_target_and_grid(tmp_path, monkeypatch):
    from sklearn.dummy import DummyClassifier
    from ml.nightly_gameplan import _estimator, _fit_group_model

    monkeypatch.setattr("ml.nightly_gameplan._estimator",
                        lambda family, *args: _estimator(family, *args) if family == "logistic"
                        else DummyClassifier(strategy="prior"))
    clocks = pd.date_range("2026-01-01", periods=120, freq="D", tz="UTC")
    labels = np.arange(120) % 2
    samples = pd.DataFrame({"decision_timestamp": clocks, "information_available_at": clocks,
        "target_window_start": clocks + pd.Timedelta(hours=1),
        "target_window_end": clocks + pd.Timedelta(hours=2),
        "symbol": "AAPL", "model_group": "1h", "route": "1h@04:00", "forecast_anchor_local": "04:00",
        "target_semantics": "independent_test", "target_contract_version": "independent-stock-targets-v1",
        "trading_hours": 1., "mr__x": labels, "observed_return": np.where(labels, .0005, -.0005),
        "assumed_round_trip_cost": .001, "target": labels, "target_raw_price_direction": labels,
        "target_cost_adjusted_positive": 0, **probability_target_metadata(RAW_DIRECTION_TARGET)})
    result = _fit_group_model(samples, current=samples.tail(2), feature_columns=("mr__x",),
        group="1h", model_directory=tmp_path / "models/1h", trained_at=pd.Timestamp("2026-06-01T00:00Z"))
    report = result["report"]
    assert report["probability_target_contract"] == RAW_DIRECTION_TARGET
    assert report["logistic_regularization_candidates"] == [.001, .01, .1, 1.]
    assert len([name for name in report["selection_metrics"] if name.startswith("regularized-logistic")]) == 4
    assert report["assessment"]["positive_rate"] > 0
    assert result["forecasts"].gameplan_variant.eq("YG").all()
    assert result["forecasts"].assumed_round_trip_cost.eq(.001).all()
    payload = joblib.load(tmp_path / "models/1h/model.joblib")
    assert payload["probability_target_contract"] == RAW_DIRECTION_TARGET
    assert payload["gameplan_variant"] == "YG"
