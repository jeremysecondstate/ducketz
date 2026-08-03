from __future__ import annotations

import importlib.util
import inspect
from dataclasses import dataclass, field
from typing import Literal, Mapping

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.impute import MissingIndicator, SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, RobustScaler

from ml.calibration import CalibrationMethod
from ml.model_features import ModelFeatureSet, model_categorical_features
from ml.preprocessing import (
    TRAINING_CLIP_LOWER_QUANTILE,
    TRAINING_CLIP_UPPER_QUANTILE,
    QuantileClipper,
    SemanticFeatureTransformer,
)

ModelFamily = Literal["logistic", "lightgbm", "xgboost"]


class OptionalModelDependencyError(ImportError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    """One readable, auditable Loop B model configuration."""

    model_name: str
    family: ModelFamily
    feature_set: str
    calibration_method: CalibrationMethod = "none"
    include_symbol: bool = False
    class_weight: str | None = None
    random_state: int = 20260725
    parameters: Mapping[str, object] = field(default_factory=dict)
    calibration_parameters: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.model_name:
            raise ValueError("model_name is required")
        if not self.feature_set:
            raise ValueError("feature_set is required")
        if self.family not in {"logistic", "lightgbm", "xgboost"}:
            raise ValueError(f"Unsupported model family: {self.family}")
        if self.calibration_method not in {"none", "platt", "isotonic"}:
            raise ValueError(f"Unsupported calibration method: {self.calibration_method}")
        if self.class_weight not in {None, "balanced"}:
            raise ValueError("class_weight must be None or 'balanced'")
        if self.family != "logistic" and self.class_weight is not None:
            raise ValueError("class_weight is currently supported only for logistic models")


DEFAULT_MODEL_PARAMETERS: dict[ModelFamily, dict[str, object]] = {
    "logistic": {
        "C": 1.0,
        "max_iter": 2_000,
    },
    "lightgbm": {
        "n_estimators": 300,
        "learning_rate": 0.03,
        "max_depth": 3,
        "num_leaves": 7,
        "min_child_samples": 100,
        "min_child_weight": 10.0,
        "subsample": 0.80,
        "subsample_freq": 1,
        "colsample_bytree": 0.80,
        "reg_alpha": 0.10,
        "reg_lambda": 2.0,
    },
    "xgboost": {
        "n_estimators": 300,
        "learning_rate": 0.03,
        "max_depth": 3,
        "min_child_weight": 50.0,
        "subsample": 0.80,
        "colsample_bytree": 0.80,
        "reg_alpha": 0.10,
        "reg_lambda": 2.0,
        "gamma": 0.0,
    },
}


def build_estimator(spec: ModelSpec, feature_set: ModelFeatureSet) -> Pipeline:
    parameters = {**DEFAULT_MODEL_PARAMETERS[spec.family], **dict(spec.parameters)}
    categorical = model_categorical_features(
        feature_set,
        include_symbol=spec.include_symbol,
    )
    if spec.family == "logistic":
        preprocessor = _linear_preprocessor(feature_set.numeric_features, categorical)
        l1_ratio = float(parameters.get("l1_ratio", 0.0))
        penalty_parameters: dict[str, object] = {}
        penalty_default = inspect.signature(LogisticRegression).parameters[
            "penalty"
        ].default
        if penalty_default != "deprecated":
            penalty_parameters["penalty"] = (
                "l2"
                if l1_ratio == 0.0
                else "l1"
                if l1_ratio == 1.0
                else "elasticnet"
            )
        estimator = LogisticRegression(
            l1_ratio=l1_ratio,
            C=float(parameters["C"]),
            class_weight=spec.class_weight,
            solver=str(parameters.get("solver", "lbfgs")),
            max_iter=int(parameters["max_iter"]),
            tol=float(parameters.get("tol", 1e-4)),
            random_state=spec.random_state,
            **penalty_parameters,
        )
    elif spec.family == "lightgbm":
        if importlib.util.find_spec("lightgbm") is None:
            raise OptionalModelDependencyError(
                "LightGBM is not installed. Install duckets[ml-tree]."
            )
        from lightgbm import LGBMClassifier

        preprocessor = _tree_preprocessor(feature_set.numeric_features, categorical)
        estimator = LGBMClassifier(
            objective="binary",
            random_state=spec.random_state,
            n_jobs=1,
            verbosity=-1,
            deterministic=True,
            force_col_wise=True,
            **parameters,
        )
    else:
        if importlib.util.find_spec("xgboost") is None:
            raise OptionalModelDependencyError(
                "XGBoost is not installed. Install duckets[ml-tree]."
            )
        from xgboost import XGBClassifier

        preprocessor = _tree_preprocessor(feature_set.numeric_features, categorical)
        estimator = XGBClassifier(
            objective="binary:logistic",
            eval_metric="logloss",
            tree_method="hist",
            random_state=spec.random_state,
            n_jobs=1,
            verbosity=0,
            **parameters,
        )
    semantic = SemanticFeatureTransformer(feature_set.transform_versions)
    return Pipeline(
        steps=[
            ("semantic_feature_transforms", semantic),
            ("preprocess", preprocessor),
            ("model", estimator),
        ]
    )


def _replace_non_finite(values: object) -> np.ndarray:
    array = np.asarray(values, dtype=float).copy()
    array[~np.isfinite(array)] = np.nan
    return array


def _linear_preprocessor(
    numeric_features: tuple[str, ...],
    categorical_features: tuple[str, ...],
) -> ColumnTransformer:
    finite = FunctionTransformer(
        _replace_non_finite,
        validate=False,
        feature_names_out="one-to-one",
    )
    numeric = Pipeline(
        steps=[
            ("finite", finite),
            (
                "impute",
                SimpleImputer(strategy="median", keep_empty_features=True),
            ),
            (
                "clip",
                QuantileClipper(
                    lower_quantile=TRAINING_CLIP_LOWER_QUANTILE,
                    upper_quantile=TRAINING_CLIP_UPPER_QUANTILE,
                ),
            ),
            ("scale", RobustScaler()),
        ]
    )
    missing = Pipeline(
        steps=[
            (
                "finite",
                FunctionTransformer(
                    _replace_non_finite,
                    validate=False,
                    feature_names_out="one-to-one",
                ),
            ),
            ("indicator", MissingIndicator(features="all")),
        ]
    )
    transformers: list[tuple[str, object, tuple[str, ...]]] = [
        ("numeric", numeric, numeric_features),
        ("missing", missing, numeric_features),
    ]
    if categorical_features:
        transformers.append(
            (
                "categorical",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                categorical_features,
            )
        )
    return ColumnTransformer(
        transformers=transformers,
        remainder="drop",
        sparse_threshold=0.0,
        verbose_feature_names_out=True,
    )


def _tree_preprocessor(
    numeric_features: tuple[str, ...],
    categorical_features: tuple[str, ...],
) -> ColumnTransformer:
    numeric = Pipeline(
        steps=[
            (
                "finite",
                FunctionTransformer(
                    _replace_non_finite,
                    validate=False,
                    feature_names_out="one-to-one",
                ),
            ),
            (
                "impute",
                SimpleImputer(
                    strategy="median",
                    add_indicator=True,
                    keep_empty_features=True,
                ),
            ),
        ]
    )
    transformers: list[tuple[str, object, tuple[str, ...]]] = [
        ("numeric", numeric, numeric_features),
    ]
    if categorical_features:
        transformers.append(
            (
                "categorical",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                categorical_features,
            )
        )
    return ColumnTransformer(
        transformers=transformers,
        remainder="drop",
        sparse_threshold=0.0,
        verbose_feature_names_out=True,
    )
