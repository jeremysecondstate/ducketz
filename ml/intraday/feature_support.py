"""Training-only admission of variation in optional market feature columns."""
from __future__ import annotations

import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin

from .prediction import ContractError

VERSION = "intraday-market-variation-support-v1"


class MarketVariationSupport(TransformerMixin, BaseEstimator):
    """Keep unlearnable market coordinates at their base-fit constant.

    Applied after training-only imputation and before scaling/model fitting.
    Raw values and availability masks remain in the frozen input snapshot;
    this is model preprocessing, never a replacement market observation.
    Calibration, validation and inference cannot teach this transformer.
    """
    def __init__(self, feature_names):
        self.feature_names = feature_names

    def fit(self, X, y=None):
        values = np.asarray(X, dtype=float)
        if values.ndim != 2 or not len(values) or values.shape[1] != len(self.feature_names):
            raise ContractError("Market support requires matching named training columns")
        if not np.isfinite(values).all():
            raise ContractError("Market support follows finite training-only imputation")
        self.n_features_in_ = values.shape[1]
        self.training_rows_ = len(values)
        self.indices_ = np.asarray([i for i, name in enumerate(self.feature_names)
            if str(name).startswith("market_") and np.all(values[:, i] == values[0, i])], dtype=int)
        self.constants_ = values[0, self.indices_].copy()
        return self

    def transform(self, X):
        values = np.asarray(X, dtype=float).copy()
        if values.ndim != 2 or values.shape[1] != self.n_features_in_:
            raise ContractError("Market support inference column shape changed")
        values[:, self.indices_] = self.constants_
        return values

    def evidence(self):
        return {"version": VERSION, "base_training_rows": self.training_rows_,
                "constant_columns": [self.feature_names[i] for i in self.indices_],
                "constant_values": self.constants_.tolist(),
                "fit_scope": "base estimator training partition after imputation",
                "policy": "no extrapolation in market coordinates with no training variation",
                "raw_snapshot_preserved": True}


def attach_market_support(pipeline, feature_names):
    if feature_names is None or not any(str(name).startswith("market_") for name in feature_names):
        return pipeline
    names = tuple(feature_names)
    steps = list(pipeline.steps)
    if not steps or steps[0][0] != "impute":
        raise ContractError("Market support requires the established imputation pipeline")
    steps.insert(1, ("market_support", MarketVariationSupport(names)))
    pipeline.steps = steps
    return pipeline
