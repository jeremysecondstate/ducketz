"""Portable estimators shared by imported and CLI Gameplan publishers."""
from __future__ import annotations

import numpy as np
import pandas as pd


class ProbabilityBlend:
    """Convex probability blend with a stable module name for persisted models.

    The weight is selected by the caller's chronological development cohort.
    This module deliberately contains no fitting or model-selection policy.
    """

    def __init__(
        self,
        tree: object,
        neural: object,
        *,
        neural_weight: float,
    ) -> None:
        self.tree = tree
        self.neural = neural
        self.neural_weight = float(neural_weight)

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        tree = np.asarray(self.tree.predict_proba(frame), dtype=float)
        neural = np.asarray(self.neural.predict_proba(frame), dtype=float)
        return (1.0 - self.neural_weight) * tree + self.neural_weight * neural


class PriorProbabilityShrinkage:
    """A fixed, nonzero model contribution toward a fitted training-only prior."""

    def __init__(self, estimator: object, *, prior_probability: float, weight: float) -> None:
        prior, selected_weight = float(prior_probability), float(weight)
        if not np.isfinite(prior) or not 0 < prior < 1:
            raise ValueError("Probability shrinkage prior must be strictly between zero and one")
        if selected_weight not in (0.25, 0.5, 0.75, 1.0):
            raise ValueError("Probability shrinkage requires a preregistered nonzero weight")
        self.estimator = estimator
        self.prior_probability = prior
        self.weight = selected_weight

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        probability = np.asarray(self.estimator.predict_proba(frame), dtype=float)
        if (probability.shape != (len(frame), 2) or not np.isfinite(probability).all()
                or ((probability < 0) | (probability > 1)).any()
                or not np.allclose(probability.sum(axis=1), 1.0, atol=1e-12, rtol=0)):
            raise ValueError("Probability shrinkage requires valid binary model probabilities")
        positive = self.weight * probability[:, 1] + (1.0 - self.weight) * self.prior_probability
        return np.column_stack((1.0 - positive, positive))
