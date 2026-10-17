"""Training-only admission of variation in optional market feature columns."""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin

from .prediction import ContractError

VERSION = "intraday-market-variation-support-v2"
MARKET_PREFIXES = ("market_", "ctx_")
PROJECTION_POLICY = "drop_base_training_constant_context_v1"


class MarketVariationSupport(TransformerMixin, BaseEstimator):
    """Keep unlearnable market coordinates at their base-fit constant.

    Applied after training-only imputation and before scaling/model fitting.
    Both source values and missing/availability indicators are covered. Raw
    values remain in the frozen input snapshot; this is model preprocessing,
    never a replacement market observation. Calibration, validation and
    inference cannot teach this transformer.
    """
    def __init__(self, feature_names):
        self.feature_names = feature_names

    def fit(self, X, y=None):
        values = np.asarray(X, dtype=float)
        if (values.ndim != 2 or not len(values) or values.shape[1] != len(self.feature_names)
                or len(set(self.feature_names)) != len(self.feature_names)
                or any(not isinstance(name, str) or not name for name in self.feature_names)):
            raise ContractError("Market support requires matching unique named training columns")
        if not np.isfinite(values).all():
            raise ContractError("Market support follows finite training-only imputation")
        self.n_features_in_ = values.shape[1]
        self.training_rows_ = len(values)
        self.indices_ = np.asarray([i for i, name in enumerate(self.feature_names)
            if name.startswith(MARKET_PREFIXES) and np.all(values[:, i] == values[0, i])], dtype=int)
        self.constants_ = values[0, self.indices_].copy()
        self.source_file_sha256_ = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
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
                "constant_values": self.constants_.tolist(), "feature_prefixes": list(MARKET_PREFIXES),
                "source_file_sha256": self.source_file_sha256_,
                "fit_scope": "base estimator training partition after imputation",
                "policy": "no extrapolation in market coordinates with no training variation",
                "raw_snapshot_preserved": True}


class MarketVariationProjection(MarketVariationSupport):
    """Opt-in removal of unsupported coordinates before dimension-sensitive fits.

    Freezing values alone leaves a forest's ``max_features='sqrt'`` dependent
    on thousands of constant optional columns. This projection learns its
    retained coordinates solely on the base training partition. The original
    transformer remains unchanged for existing recipes and serialized fits.
    """
    def fit(self, X, y=None):
        super().fit(X, y)
        self.retained_indices_ = np.setdiff1d(np.arange(self.n_features_in_), self.indices_)
        if not len(self.retained_indices_):
            raise ContractError("Market projection requires at least one supported feature")
        return self

    def transform(self, X):
        return super().transform(X)[:, self.retained_indices_]

    def evidence(self):
        return {**super().evidence(), "version": "intraday-market-variation-projection-v1",
            "projection_policy": PROJECTION_POLICY, "input_features": self.n_features_in_,
            "retained_features": len(self.retained_indices_),
            "retained_columns": [self.feature_names[i] for i in self.retained_indices_],
            "policy": "drop only optional coordinates constant in base training; later partitions cannot activate them"}


def attach_market_support(pipeline, feature_names, *, projection=None):
    if projection not in {None, PROJECTION_POLICY}:
        raise ContractError("Unsupported frozen market feature projection")
    if feature_names is None or not any(str(name).startswith(MARKET_PREFIXES) for name in feature_names):
        return pipeline
    names = tuple(feature_names)
    steps = list(pipeline.steps)
    if not steps or steps[0][0] != "impute" or any(name == "market_support" for name, _ in steps):
        raise ContractError("Market support requires the established imputation pipeline")
    transformer = MarketVariationProjection if projection == PROJECTION_POLICY else MarketVariationSupport
    steps.insert(1, ("market_support", transformer(names)))
    pipeline.steps = steps
    return pipeline
