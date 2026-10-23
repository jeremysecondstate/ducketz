"""Offline clock separation proofs; fabricated fixtures are not market evidence."""

import numpy as np
import pandas as pd
import pytest

from ml.intraday.data import build_dataset
from ml.intraday.models import (ModelSpec, fit_calibrated_estimator,
    purged_chronological_split, with_market_feature_projection)
from ml.intraday.prediction import ContractError
from ml.intraday.training_timing import (HISTORICAL_RESEARCH, OPERATIONAL_REPLAY,
    LEGACY, timing_contract, timing_mode_from_metadata)
from tests.test_intraday_data import candles


def test_present_research_admits_late_history_without_backdating_receipts():
    frame = candles(42)
    frame["available_at"] = pd.Timestamp("2026-05-12T12:00Z")
    frame.loc[10, "available_at"] = pd.Timestamp("2026-05-12T12:01Z")
    before = frame.copy(deep=True)
    data = build_dataset(frame, reference_at=frame.opened_at.iloc[-1],
        cutoff="2026-05-12T12:02Z", synthetic_policy="exclude", timing_mode=HISTORICAL_RESEARCH)
    assert frame.equals(before)
    assert data.manifest["training_timing_contract"] == timing_contract(HISTORICAL_RESEARCH)
    assert frame.opened_at.iloc[25] in data.reference_at
    assert data.label_available_at.min() >= pd.Timestamp("2026-05-12T12:00Z")
    train, calibration = purged_chronological_split(data.reference_at, data.target_close_at,
        label_available_at=data.label_available_at, fraction=.25, timing_mode=HISTORICAL_RESEARCH)
    assert len(train) and max(data.target_close_at[train]) <= data.reference_at[calibration[0]]
    assert min(data.label_available_at[train]) > data.reference_at[calibration[0]]
    with pytest.raises(ContractError, match="No label windows"):
        purged_chronological_split(data.reference_at, data.target_close_at,
            label_available_at=data.label_available_at, fraction=.25, timing_mode=OPERATIONAL_REPLAY)
    with pytest.raises(ContractError, match="Exact completed reference"):
        build_dataset(frame, reference_at=frame.opened_at.iloc[-1], cutoff="2026-05-12T11:59Z",
            synthetic_policy="exclude", timing_mode=HISTORICAL_RESEARCH)


def test_missing_original_receipt_is_research_only_and_not_exact_replay():
    frame = candles(42)
    frame["availability_kind"] = "assumed_historical_close"
    data = build_dataset(frame, reference_at=frame.opened_at.iloc[-1], cutoff=frame.available_at.max(),
        synthetic_policy="exclude", timing_mode=HISTORICAL_RESEARCH)
    assert len(data.y)
    assert "not proof of local possession" in data.manifest["historical_receipt_limitation"]
    with pytest.raises(ContractError, match="actual possession"):
        build_dataset(frame, reference_at=frame.opened_at.iloc[-1], cutoff=frame.available_at.max(),
            synthetic_policy="exclude", timing_mode=OPERATIONAL_REPLAY)


def test_research_fit_uses_full_purged_base_and_base_only_preprocessing():
    refs = pd.date_range("2026-05-01", periods=200, freq="15min", tz="UTC")
    targets = refs + pd.Timedelta(minutes=45)
    receipts = pd.DatetimeIndex([pd.Timestamp("2026-06-01T00:00Z")] * len(refs))
    X = np.column_stack([np.arange(200, dtype=float), np.r_[np.zeros(150), np.ones(50)]])
    y = np.arange(200) % 2
    spec = with_market_feature_projection(ModelSpec("test", family="logistic", probability_calibration="sigmoid", calibration_fraction=.25))
    estimator, evidence = fit_calibrated_estimator(spec, X, y, reference_at=refs,
        target_close_at=targets, label_available_at=receipts, training_cutoff=receipts.max(),
        feature_names=("price", "ctx_new"), timing_mode=HISTORICAL_RESEARCH)
    assert evidence["calibration"]["base_rows"] == 148
    assert evidence["calibration"]["purged_rows"] == 2
    assert estimator.base.named_steps["model"].n_features_in_ == 1
    assert estimator.base.named_steps["scale"].mean_[0] == pytest.approx(np.arange(148).mean())
    with pytest.raises(ContractError, match="observable at the training cutoff"):
        fit_calibrated_estimator(spec, X, y, reference_at=refs, target_close_at=targets,
            label_available_at=receipts, training_cutoff="2026-05-31T23:59Z", timing_mode=HISTORICAL_RESEARCH)




def test_unversioned_recipe_metadata_remains_legacy_and_unknown_mode_fails():
    assert timing_mode_from_metadata({}) == LEGACY
    with pytest.raises(ContractError, match="Unknown training timing"):
        timing_mode_from_metadata({"training_timing_contract": {"version": "intraday-training-timing-v1", "mode": "invented"}})




def test_local_research_recipe_restores_late_reference_and_dependency_rows(tmp_path):
    from ml.intraday import live_model_recipe as recipes
    from ml.intraday.runtime import ResearchConfig
    from tests.test_intraday_market_recipe import candidate, frame, save
    original = candidate()
    revised = recipes.migrate_training_timing_recipe(original, pc="Scout", symbols=("TEST",),
        installed_at="2026-05-12T00:00Z", authority_reference="Explicit local research fixture")
    assert revised["models"] == original["models"]
    assert revised["training_support"] == original["training_support"]
    assert "training_timing_contract" not in original
    data = frame(late_reference=True)
    reference = pd.Timestamp(data.opened_at.iloc[-1])
    kwargs = dict(reference_at=reference, cutoff=reference + pd.Timedelta(minutes=16),
        lookback_days=None, synthetic_policy="exclude")
    save(tmp_path, original)
    _, old = recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path, pc="Scout", symbols=("TEST",))
    before = old["dataset_builder"](data, **kwargs)
    save(tmp_path, revised)
    _, new = recipes.prepare_manual_models(ResearchConfig(), datastore=tmp_path, pc="Scout", symbols=("TEST",))
    after = new["dataset_builder"](data, **kwargs)
    assert pd.Timestamp("2026-05-06T16:00Z") not in before.reference_at
    assert pd.Timestamp("2026-05-06T16:00Z") in after.reference_at
    assert len(after.y) > len(before.y)
    assert after.manifest["context_decision_rows_excluded"] == 0
    assert after.manifest["market_context"]["training_rows_with_source"] == 0


def test_local_calibration_fraction_changes_need_migration_and_preserve_minimum_support():
    from ml.intraday import live_model_recipe as recipes
    from tests.test_intraday_market_recipe import candidate
    original = candidate()
    for spec in original["models"]:
        spec["calibration_fraction"] = .1
    original.pop("sha256")
    original["sha256"] = recipes._sha(original)
    with pytest.raises(ContractError, match="explicit local timing migration"):
        recipes._validate_recipe(original, pc="Scout", symbols=("TEST",))
    original["training_timing_contract"] = timing_contract(HISTORICAL_RESEARCH)
    original.pop("sha256")
    original["sha256"] = recipes._sha(original)
    recipes._validate_recipe(original, pc="Scout", symbols=("TEST",))
    refs = pd.date_range("2026-05-01", periods=200, freq="1h", tz="UTC")
    with pytest.raises(recipes.ManualRecipeSupportError, match="calibration"):
        recipes._training_support(np.arange(200) % 2, refs, refs + pd.Timedelta(minutes=45),
            refs + pd.Timedelta(minutes=45), cutoff=refs[-1] + pd.Timedelta(hours=1),
            timing_mode=HISTORICAL_RESEARCH, calibration_fraction=.1)
