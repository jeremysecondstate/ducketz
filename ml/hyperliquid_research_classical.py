"""Bounded classical estimators for offline Hyperliquid research only.

This factory does not load legacy code, data, model publications, or credentials.
The caller owns chronological splitting, fit-only preprocessing, calibration,
assessment, and an outer native-thread limit. ``scaled`` requests standardized
features; every estimator still needs finite input or suitable imputation.
"""
from __future__ import annotations

import importlib
from typing import Any

from sklearn.ensemble import (
    AdaBoostClassifier,
    ExtraTreesClassifier,
    GradientBoostingClassifier,
    HistGradientBoostingClassifier,
    RandomForestClassifier,
)
from sklearn.linear_model import LogisticRegression
from sklearn.neural_network import MLPClassifier
from sklearn.tree import DecisionTreeClassifier


def make_classical_estimators(
    seed: int = 42, threads: int = 2,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Return fresh raw estimators and explicit optional-dependency skips.

    ``threads`` validates the caller's intended outer thread budget. Estimators
    exposing their own worker setting use one worker to avoid nested parallelism.
    Missing optional top-level packages are skipped; broken installed packages
    and missing transitive dependencies raise instead of hiding an environment
    problem. No fit, search, installation, publication, or file write occurs.

    The four existing families retain the production factory's parameters so
    research can compare model additions without silently changing the baseline.
    """
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("seed must be an integer in [0, 2**32 - 1].")
    if type(threads) is not int or threads < 1:
        raise ValueError("threads must be a positive integer for the outer thread limit.")

    estimators: dict[str, dict[str, Any]] = {
        "logistic": {
            "estimator": LogisticRegression(C=1.0, max_iter=500, random_state=seed),
            "scaled": True,
        },
        "extra_trees": {
            "estimator": ExtraTreesClassifier(
                n_estimators=128, max_depth=10, min_samples_leaf=12,
                max_features=0.7, n_jobs=1, random_state=seed,
            ),
            "scaled": False,
        },
        "hist_gradient_boosting": {
            "estimator": HistGradientBoostingClassifier(
                max_iter=100, max_leaf_nodes=15, learning_rate=0.06,
                min_samples_leaf=20, l2_regularization=1.0,
                early_stopping=False, random_state=seed,
            ),
            "scaled": False,
        },
        "mlp": {
            "estimator": MLPClassifier(
                hidden_layer_sizes=(32, 16), alpha=0.01, batch_size=128,
                max_iter=100, learning_rate_init=0.001, early_stopping=False,
                shuffle=False, random_state=seed,
            ),
            "scaled": True,
        },
        "random_forest": {
            "estimator": RandomForestClassifier(
                n_estimators=128, max_depth=10, min_samples_leaf=12,
                max_features=0.7, n_jobs=1, random_state=seed,
            ),
            "scaled": False,
        },
        "adaboost": {
            "estimator": AdaBoostClassifier(
                estimator=DecisionTreeClassifier(
                    max_depth=2, min_samples_leaf=12, random_state=seed,
                ),
                n_estimators=100, learning_rate=0.1, random_state=seed,
            ),
            "scaled": False,
        },
        "gradient_boosting": {
            "estimator": GradientBoostingClassifier(
                n_estimators=100, learning_rate=0.06, max_depth=3,
                min_samples_leaf=20, subsample=0.8, max_features=0.7,
                random_state=seed,
            ),
            "scaled": False,
        },
    }
    skipped: dict[str, str] = {}
    optional = {
        "lightgbm": ("LGBMClassifier", dict(
            n_estimators=100, learning_rate=0.06, max_depth=6,
            num_leaves=15, min_child_samples=20, colsample_bytree=0.7,
            reg_lambda=1.0, objective="binary", n_jobs=1,
            random_state=seed, verbosity=-1,
        )),
        "xgboost": ("XGBClassifier", dict(
            n_estimators=100, learning_rate=0.06, max_depth=6,
            min_child_weight=3, subsample=0.8, colsample_bytree=0.7,
            reg_lambda=1.0, objective="binary:logistic", eval_metric="logloss",
            tree_method="hist", n_jobs=1, random_state=seed,
        )),
        "catboost": ("CatBoostClassifier", dict(
            iterations=100, depth=6, learning_rate=0.06, l2_leaf_reg=3.0,
            loss_function="Logloss", thread_count=1, random_seed=seed,
            verbose=False, allow_writing_files=False,
        )),
    }
    for name, (class_name, params) in optional.items():
        try:
            module = importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise
            skipped[name] = f"Optional dependency '{name}' is not installed."
            continue
        estimators[name] = {
            "estimator": getattr(module, class_name)(**params), "scaled": False,
        }
    return estimators, skipped
