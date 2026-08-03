from __future__ import annotations

import joblib
import numpy as np
import pandas as pd

from ml.contracts import FeatureSet, FeatureSpec
from ml.models.registry import ModelSpec, _tree_preprocessor, build_estimator
from ml.preprocessing import (
    TRAINING_CLIP_LOWER_QUANTILE,
    TRAINING_CLIP_UPPER_QUANTILE,
)


def test_tree_preprocessor_retains_all_missing_required_numeric_feature() -> None:
    rows = pd.DataFrame(
        {
            "test__signal": [-1.0, 0.0, 1.0],
            "test__all_missing": [np.nan, np.nan, np.nan],
        }
    )
    preprocessor = _tree_preprocessor(tuple(rows.columns), ())

    transformed = preprocessor.fit_transform(rows)

    output_names = tuple(preprocessor.get_feature_names_out())
    assert transformed.shape[1] == 3
    assert "numeric__test__signal" in output_names
    assert "numeric__test__all_missing" in output_names
    assert "numeric__missingindicator_test__all_missing" in output_names


def test_logistic_retains_all_missing_required_numeric_feature_after_reload(
    tmp_path,
) -> None:
    feature_set = FeatureSet(
        "required-numeric-test",
        (
            FeatureSpec(
                name="test__signal",
                source_family="test",
                source_column="signal",
            ),
            FeatureSpec(
                name="test__all_missing",
                source_family="test",
                source_column="all_missing",
            ),
        ),
    )
    rows = pd.DataFrame(
        {
            "test__signal": [-2.0, -1.0, -0.5, 0.5, 1.0, 2.0],
            "test__all_missing": [np.nan] * 6,
        }
    )
    target = pd.Series([0, 0, 0, 1, 1, 1])
    estimator = build_estimator(
        ModelSpec(
            model_name="logistic-required-numeric-test",
            family="logistic",
            feature_set=feature_set.name,
        ),
        feature_set,
    )

    estimator.fit(rows, target)

    preprocessor = estimator.named_steps["preprocess"]
    clipper = preprocessor.named_transformers_["numeric"].named_steps["clip"]
    assert clipper.lower_quantile == TRAINING_CLIP_LOWER_QUANTILE
    assert clipper.upper_quantile == TRAINING_CLIP_UPPER_QUANTILE
    transformed = preprocessor.transform(
        estimator.named_steps["semantic_feature_transforms"].transform(rows)
    )
    output_names = tuple(preprocessor.get_feature_names_out())
    assert transformed.shape[1] == 4
    assert "numeric__test__signal" in output_names
    assert "numeric__test__all_missing" in output_names
    assert "missing__missingindicator_test__signal" in output_names
    assert "missing__missingindicator_test__all_missing" in output_names

    probability = estimator.predict_proba(rows)
    artifact = tmp_path / "model.joblib"
    joblib.dump(estimator, artifact)
    reloaded = joblib.load(artifact)

    assert tuple(
        reloaded.named_steps["preprocess"].get_feature_names_out()
    ) == output_names
    np.testing.assert_allclose(reloaded.predict_proba(rows), probability)


def test_logistic_accepts_sparse_regularized_configuration() -> None:
    feature_set = FeatureSet(
        "sparse-logistic-test",
        (
            FeatureSpec(
                name="test__signal",
                source_family="test",
                source_column="signal",
            ),
        ),
    )
    estimator = build_estimator(
        ModelSpec(
            model_name="sparse-logistic-test",
            family="logistic",
            feature_set=feature_set.name,
            parameters={
                "C": 0.3,
                "l1_ratio": 1.0,
                "solver": "liblinear",
                "max_iter": 5_000,
                "tol": 1e-5,
            },
        ),
        feature_set,
    )

    model = estimator.named_steps["model"]
    assert model.C == 0.3
    assert model.l1_ratio == 1.0
    assert model.solver == "liblinear"
    assert model.max_iter == 5_000
    assert model.tol == 1e-5
