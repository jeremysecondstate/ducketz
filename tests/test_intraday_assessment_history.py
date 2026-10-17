"""Chronological assessments must apply the runtime row cap at each fold."""
from dataclasses import asdict, replace

import numpy as np
import pandas as pd
import pytest

from ml.intraday import live_model_recipe as recipes
from ml.intraday.data import FEATURE_NAMES
from ml.intraday.model_selection import assess_frozen_ensemble
from ml.intraday.prediction import ContractError, STEP
from tests.test_intraday_live_model_recipe import selected, dataset_for, fit
from tests.test_intraday_model_selection import fixture_data, small_config, spec, fake_fitter


def test_assessment_history_is_distinct_from_runtime_fit_population(tmp_path, monkeypatch):
    _, _, _, config, runtime = selected(tmp_path)
    source = dataset_for(runtime)
    refs = pd.date_range("2025-01-01T00:00Z", periods=4200, freq="15min")
    expanded = replace(source, X=np.resize(source.X, (4200, len(FEATURE_NAMES))),
        y=np.arange(4200) % 2, training_reference_at=refs,
        target_close_at=refs + 3 * STEP, label_available_at=refs + 3 * STEP,
        manifest={**source.manifest, "training_rows": 4200})
    monkeypatch.setattr(recipes, "build_observed_dataset", lambda *a, **kw: expanded)
    kwargs = dict(cutoff=refs[-1] + 4 * STEP, reference_at=source.inference["reference_at"], synthetic_policy="exclude")
    normal = runtime["dataset_builder"](None, **kwargs)
    assessment = runtime["dataset_builder"].for_assessment(None, **kwargs)
    assert len(normal.y) == 4096 and len(assessment.y) == 4200
    assert assessment.manifest["assessment_only"] is True
    assert assessment.manifest["manual_recipe_training_support"]["training_rows"] == 4096
    assert assessment.manifest["training_window"]["max_rows"] == 8192
    with pytest.raises(ContractError, match="per-fold fits"):
        fit(runtime, config, assessment)


def test_capped_runtime_snapshot_cannot_be_misreported_as_exact_replay(monkeypatch):
    fake_fitter(monkeypatch)
    dataset, sessions = fixture_data()
    model = replace(spec(), training_max_rows=4096)
    dataset = replace(dataset, manifest={**dataset.manifest, "input_training_rows": 5000,
        "manual_model_recipe": {"sha256": "fixture", "models": [asdict(model)]}})
    with pytest.raises(ContractError, match="uncapped history"):
        assess_frozen_ensemble(dataset, models=[model], config=small_config(), session_ids=sessions)
