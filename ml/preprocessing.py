from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin


TRAINING_CLIP_LOWER_QUANTILE = 0.0025
TRAINING_CLIP_UPPER_QUANTILE = 0.9975
LOG1P_CAPPED_TRAINING_QUANTILES = {
    "log1p-capped-training-v1": 0.995,
    "log1p-capped-training-v2": 0.9975,
}
PREPROCESSING_POLICY_VERSION = "training-quantiles-0.25-99.75-v1"


def preprocessing_policy(model_family: str) -> dict[str, object]:
    """Return model-reuse metadata for the fitted preprocessing policy."""

    if model_family not in {"logistic", "lightgbm", "xgboost"}:
        raise ValueError(f"Unsupported model family: {model_family}")
    return {
        "version": PREPROCESSING_POLICY_VERSION,
        "fit_partition": "training",
        "numeric_quantile_clipping": (
            {
                "lower_quantile": TRAINING_CLIP_LOWER_QUANTILE,
                "upper_quantile": TRAINING_CLIP_UPPER_QUANTILE,
            }
            if model_family == "logistic"
            else None
        ),
        "semantic_log1p_training_cap_quantiles": dict(
            LOG1P_CAPPED_TRAINING_QUANTILES
        ),
    }


class QuantileClipper(TransformerMixin, BaseEstimator):
    """Clip each numeric column to train-fitted quantile bounds."""

    def __init__(
        self,
        lower_quantile: float = TRAINING_CLIP_LOWER_QUANTILE,
        upper_quantile: float = TRAINING_CLIP_UPPER_QUANTILE,
    ):
        self.lower_quantile = lower_quantile
        self.upper_quantile = upper_quantile

    def fit(self, X: object, y: object = None) -> "QuantileClipper":
        del y
        if not 0.0 <= self.lower_quantile < self.upper_quantile <= 1.0:
            raise ValueError("Quantile bounds must satisfy 0 <= lower < upper <= 1")
        array = self._validate_array(X)
        self.n_features_in_ = array.shape[1]
        self.lower_bounds_ = np.quantile(array, self.lower_quantile, axis=0)
        self.upper_bounds_ = np.quantile(array, self.upper_quantile, axis=0)
        return self

    def transform(self, X: object) -> np.ndarray:
        array = self._validate_array(X)
        if array.shape[1] != self.n_features_in_:
            raise ValueError(
                f"Expected {self.n_features_in_} features, received {array.shape[1]}."
            )
        return np.clip(array, self.lower_bounds_, self.upper_bounds_)

    def get_feature_names_out(self, input_features: object = None) -> np.ndarray:
        if input_features is None:
            return np.asarray(
                [f"x{i}" for i in range(self.n_features_in_)], dtype=object
            )
        return np.asarray(input_features, dtype=object)

    @staticmethod
    def _validate_array(X: object) -> np.ndarray:
        array = np.asarray(X, dtype=float)
        if array.ndim != 2:
            raise ValueError("QuantileClipper expects a two-dimensional array")
        if array.shape[0] == 0:
            raise ValueError("QuantileClipper cannot fit or transform an empty array")
        if not np.isfinite(array).all():
            raise ValueError("QuantileClipper expects finite values after imputation")
        return array


class SemanticFeatureTransformer(TransformerMixin, BaseEstimator):
    """Apply named contract transforms inside the fitted sklearn pipeline."""

    _IDENTITY_VERSIONS = frozenset(
        {
            "identity-v1",
            # These values are already calculated in signed-return form or use
            # the train-fitted QuantileClipper/RobustScaler downstream.
            "signed-log-return-robust-v1",
            "signed-log-return-v1",
            "training-cap-robust-v1",
            "training-winsor-bounded-ratio-v1",
        }
    )
    _LOG_VERSIONS = frozenset(
        {"log1p-nonnegative-v1", *LOG1P_CAPPED_TRAINING_QUANTILES}
    )

    def __init__(self, transform_versions: Mapping[str, str]):
        self.transform_versions = transform_versions

    def fit(self, X: object, y: object = None) -> "SemanticFeatureTransformer":
        del y
        frame = self._frame(X)
        self.feature_names_in_ = np.asarray(frame.columns, dtype=object)
        configured = dict(self.transform_versions)
        missing = sorted(set(configured).difference(frame.columns))
        if missing:
            raise ValueError(
                "Semantic transforms reference missing columns: "
                + ", ".join(missing)
            )
        supported = self._IDENTITY_VERSIONS | self._LOG_VERSIONS
        unsupported = sorted(
            {
                version
                for version in configured.values()
                if version not in supported
            }
        )
        if unsupported:
            raise ValueError(
                "Unsupported active semantic transform versions: "
                + ", ".join(unsupported)
            )
        self.training_caps_ = {}
        for column, version in configured.items():
            cap_quantile = LOG1P_CAPPED_TRAINING_QUANTILES.get(version)
            if cap_quantile is None:
                continue
            values = pd.to_numeric(frame[column], errors="coerce")
            eligible = values.where(values >= 0.0).dropna()
            self.training_caps_[column] = (
                float(eligible.quantile(cap_quantile))
                if not eligible.empty
                else None
            )
        return self

    def transform(self, X: object) -> pd.DataFrame:
        frame = self._frame(X)
        expected = list(self.feature_names_in_)
        if list(frame.columns) != expected:
            raise ValueError("Semantic transform input columns changed after fitting")
        result = frame.copy()
        for column, version in dict(self.transform_versions).items():
            if version in self._IDENTITY_VERSIONS:
                continue
            values = pd.to_numeric(result[column], errors="coerce")
            nonnegative = values.where(values >= 0.0)
            if version in LOG1P_CAPPED_TRAINING_QUANTILES:
                cap = self.training_caps_.get(column)
                if cap is not None:
                    nonnegative = nonnegative.clip(upper=cap)
            result[column] = np.log1p(nonnegative)
        return result

    def get_feature_names_out(self, input_features: object = None) -> np.ndarray:
        if input_features is None:
            return np.asarray(self.feature_names_in_, dtype=object)
        return np.asarray(input_features, dtype=object)

    @staticmethod
    def _frame(X: object) -> pd.DataFrame:
        if not isinstance(X, pd.DataFrame):
            raise ValueError("SemanticFeatureTransformer expects a pandas DataFrame")
        if X.columns.has_duplicates:
            raise ValueError("SemanticFeatureTransformer rejects duplicate columns")
        return X
