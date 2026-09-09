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
