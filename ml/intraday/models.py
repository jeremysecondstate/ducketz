"""Independent, fresh-fit classifiers for the exact intraday not-down target.

This module has no provider, account or order dependencies. Callers supply a
causal numeric dataset and its per-label availability evidence. A failed model
has a failure record, never a stale-model or constant-probability substitute.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import os
import re
import tempfile
import time
from typing import Mapping
import warnings

import joblib
import numpy as np
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.discriminant_analysis import LinearDiscriminantAnalysis, QuadraticDiscriminantAnalysis
from sklearn.ensemble import (
    AdaBoostClassifier, ExtraTreesClassifier, GradientBoostingClassifier,
    HistGradientBoostingClassifier, RandomForestClassifier,
)
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression, PassiveAggressiveClassifier, RidgeClassifier
from sklearn.naive_bayes import GaussianNB
from sklearn.neighbors import KNeighborsClassifier
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier
from threadpoolctl import threadpool_limits

from ml.intraday.prediction import ContractError, TARGET_VERSION, instant

REGISTRY_VERSION = "intraday-independent-tabular-v2"
RUNTIME_CONTRACT_VERSION = "intraday-frozen-estimator-pipeline-v1"

# Ten independently fitted research candidates. These are explicit settings,
# not a claim that this composition or its probabilities outperform alternatives.
DEFAULT_MODELS = (
    "logistic", "gaussian_nb", "knn", "lda", "random_forest", "extra_trees",
    "hist_gradient_boosting", "gradient_boosting", "adaboost", "xgboost",
)

_DEPENDENCIES = {"xgboost": "xgboost", "lightgbm": "lightgbm", "catboost": "catboost"}
_TABULAR = set(DEFAULT_MODELS) | {"lightgbm", "catboost", "qda", "ridge", "passive_aggressive", "mlp"}
_NEURAL = (
    "cnn_1d", "lstm_attention", "bidirectional_gru_attention",
    "stacked_bidirectional_lstm_attention_rnn", "attn", "affn", "csann",
    "dc_lstm", "garn", "tcn", "tft", "transformer", "transformer_a", "transformer_b",
)

DEFAULT_PARAMETERS = {
    "logistic": {"C": 1.0, "max_iter": 1000, "solver": "lbfgs"},
    "gaussian_nb": {"var_smoothing": 1e-8},
    "knn": {"n_neighbors": 15, "weights": "distance", "n_jobs": 1},
    "lda": {"solver": "lsqr", "shrinkage": "auto"},
    "qda": {"reg_param": 0.1},
    "random_forest": {"n_estimators": 128, "max_depth": 6, "min_samples_leaf": 8, "n_jobs": 1},
    "extra_trees": {"n_estimators": 128, "max_depth": 6, "min_samples_leaf": 8, "n_jobs": 1},
    "hist_gradient_boosting": {"max_iter": 100, "max_leaf_nodes": 15, "learning_rate": 0.05,
                               "l2_regularization": 1.0, "early_stopping": False},
    "gradient_boosting": {"n_estimators": 100, "max_depth": 2, "min_samples_leaf": 8, "learning_rate": 0.05},
    "adaboost": {"n_estimators": 100, "learning_rate": 0.05},
    "xgboost": {"n_estimators": 150, "max_depth": 3, "learning_rate": 0.05,
                "min_child_weight": 8, "reg_lambda": 2.0, "tree_method": "hist",
                "objective": "binary:logistic", "eval_metric": "logloss", "n_jobs": 1, "verbosity": 0},
    "lightgbm": {"n_estimators": 150, "max_depth": 3, "num_leaves": 7, "learning_rate": 0.05,
                 "min_child_samples": 20, "reg_lambda": 2.0, "objective": "binary",
                 "n_jobs": 1, "verbosity": -1, "deterministic": True, "force_col_wise": True},
    "catboost": {"iterations": 150, "depth": 4, "learning_rate": 0.05, "loss_function": "Logloss",
                "thread_count": 1, "verbose": False, "allow_writing_files": False},
    "ridge": {"alpha": 1.0},
    "passive_aggressive": {"C": 0.5, "max_iter": 1000, "tol": 1e-3, "shuffle": False},
    "mlp": {"hidden_layer_sizes": (32, 16), "max_iter": 200, "shuffle": False,
            "early_stopping": False, "warm_start": False},
}


class UnsupportedModelError(ImportError):
    pass


@dataclass(frozen=True)
class ModelSpec:
    name: str
    family: str | None = None
    random_state: int = 20261012
    parameters: Mapping[str, object] = field(default_factory=dict)
    calibration_fraction: float = 0.2
    probability_calibration: str | None = "none"
    training_max_rows: int | None = None
    runtime_contract: Mapping[str, object] | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", self.name):
            raise ContractError("Model name must be a safe unique identifier")
        if self.family is None:
            object.__setattr__(self, "family", self.name)
        if not 0 < self.calibration_fraction < 1:
            raise ContractError("Calibration fraction must be between zero and one")
        if self.probability_calibration not in {None, "none", "sigmoid"}:
            raise ContractError("Probability calibration must be none or sigmoid (None is a legacy alias)")
        if self.training_max_rows is not None and (isinstance(self.training_max_rows, bool)
                or not isinstance(self.training_max_rows, int) or self.training_max_rows < 4):
            raise ContractError("Training row limit must be an integer of at least four or None")
        if not isinstance(self.parameters, Mapping):
            raise ContractError("Model parameters must be an object")
        if self.runtime_contract is not None and not isinstance(self.runtime_contract, Mapping):
            raise ContractError("Frozen model runtime contract must be an object")
        # A frozen dataclass must not keep caller-owned mutable policy dictionaries.
        object.__setattr__(self, "parameters", deepcopy(dict(self.parameters)))
        if self.runtime_contract is not None:
            object.__setattr__(self, "runtime_contract", deepcopy(dict(self.runtime_contract)))


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def configured_models(config=None) -> tuple[ModelSpec, ...]:
    if config is None:
        config = DEFAULT_MODELS
    if isinstance(config, Mapping):
        config = config["models"]
    specs = tuple(s if isinstance(s, ModelSpec) else ModelSpec(s) if isinstance(s, str)
                  else ModelSpec(**s) for s in config)
    if not specs or len({s.name for s in specs}) != len(specs):
        raise ContractError("Enabled ensemble must contain unique model names")
    return specs


def registry_status() -> list[dict]:
    """Report real adapters separately from installed architecture dependencies."""
    result = []
    for family in sorted(_TABULAR):
        dependency = _DEPENDENCIES.get(family, "sklearn")
        installed = importlib.util.find_spec(dependency) is not None
        result.append({"family": family, "status": "available" if installed else "dependency_unavailable",
                       "dependency": dependency,
                       "optional_probability_calibration": "purged_chronological_sigmoid",
                       "probability_interface": "purged_chronological_sigmoid" if family in {"ridge", "passive_aggressive"}
                       else "predict_proba"})
    for family in _NEURAL:
        result.append({"family": family, "status": "adapter_not_implemented",
                       "dependency": "architecture-specific sequence training adapter",
                       "note": "Installed torch or prior code does not establish a trained exact-target model."})
    return result


def _parameters(spec: ModelSpec) -> dict:
    if spec.family not in _TABULAR:
        raise UnsupportedModelError(f"No genuine training/probability adapter for {spec.family}")
    frozen = spec.runtime_contract is not None
    params = (_decode_parameters(dict(spec.parameters)) if frozen else
              {**DEFAULT_PARAMETERS[spec.family], **dict(spec.parameters)})
    if params.get("warm_start", False):
        raise ContractError("Warm-start is forbidden for the full-retraining benchmark")
    # These binary linear classifiers expose n_jobs=None as their legacy
    # constructor default; preserve that resolved single-fit behavior exactly.
    jobs = params.get("n_jobs", 1)
    if (jobs != 1 and not (frozen and spec.family in {"logistic", "passive_aggressive"} and jobs is None)
            or params.get("thread_count", 1) != 1):
        raise ContractError("This research adapter requires one internal worker per model")
    if spec.family == "knn":
        neighbors = params["n_neighbors"]
        if isinstance(neighbors, (bool, np.bool_)) or not isinstance(neighbors, (int, np.integer)) or neighbors < 1:
            raise ContractError("KNN n_neighbors must be a positive integer")
    if spec.family in {"random_forest", "extra_trees", "hist_gradient_boosting", "gradient_boosting",
                       "adaboost", "xgboost", "lightgbm", "logistic", "mlp", "passive_aggressive"}:
        if frozen and params.get("random_state") != spec.random_state:
            raise ContractError("Frozen estimator random state differs from its specification")
        params["random_state"] = spec.random_state
    elif spec.family == "catboost":
        if frozen and params.get("random_seed") != spec.random_state:
            raise ContractError("Frozen estimator random seed differs from its specification")
        params["random_seed"] = spec.random_state
    return params


def build_estimator(spec: ModelSpec) -> Pipeline:
    """Construct a new untrained pipeline; preprocessing fits only its training rows."""
    params = _parameters(spec)
    constructors = {
        "logistic": LogisticRegression, "gaussian_nb": GaussianNB, "knn": KNeighborsClassifier,
        "lda": LinearDiscriminantAnalysis, "qda": QuadraticDiscriminantAnalysis,
        "random_forest": RandomForestClassifier, "extra_trees": ExtraTreesClassifier,
        "hist_gradient_boosting": HistGradientBoostingClassifier, "gradient_boosting": GradientBoostingClassifier,
        "adaboost": AdaBoostClassifier, "ridge": RidgeClassifier,
        "passive_aggressive": PassiveAggressiveClassifier, "mlp": MLPClassifier,
    }
    if spec.family == "adaboost" and spec.runtime_contract is None:
        params.setdefault("estimator", DecisionTreeClassifier(max_depth=1, random_state=spec.random_state))
    if spec.family in _DEPENDENCIES:
        module = _DEPENDENCIES[spec.family]
        if importlib.util.find_spec(module) is None:
            raise UnsupportedModelError(f"{module} is not installed; no substitute was fitted")
        if spec.family == "xgboost":
            from xgboost import XGBClassifier
            estimator = XGBClassifier(**params)
        elif spec.family == "lightgbm":
            from lightgbm import LGBMClassifier
            estimator = LGBMClassifier(**params)
        else:
            from catboost import CatBoostClassifier
            estimator = CatBoostClassifier(**params)
    else:
        estimator = constructors[spec.family](**params)
    if spec.runtime_contract is not None:
        return _frozen_pipeline(spec, estimator)
    steps = [("impute", SimpleImputer(strategy="median", keep_empty_features=True))]
    if spec.family in {"logistic", "knn", "lda", "qda", "gaussian_nb", "ridge", "passive_aggressive", "mlp"}:
        steps.append(("scale", StandardScaler()))
    steps.append(("model", estimator))
    return Pipeline(steps)


def _encode_parameters(value):
    """JSON-only constructor values; never serialize/import arbitrary objects."""
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        if np.isnan(value):
            return {"__intraday_parameter__": "nan"}
        raise ContractError("Infinite constructor parameters cannot be frozen")
    if isinstance(value, DecisionTreeClassifier):
        return {"__intraday_parameter__": "decision_tree_classifier",
                "parameters": _encode_parameters(value.get_params(deep=False))}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContractError("Frozen constructor parameter keys must be strings")
        return {key: _encode_parameters(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode_parameters(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ContractError(f"Unsupported frozen constructor parameter type: {type(value).__name__}")


def _decode_parameters(value):
    if isinstance(value, dict):
        marker = value.get("__intraday_parameter__")
        if marker is not None:
            if marker == "nan" and set(value) == {"__intraday_parameter__"}:
                return np.nan
            if marker == "decision_tree_classifier" and set(value) == {"__intraday_parameter__", "parameters"}:
                params = _decode_parameters(value["parameters"])
                tree = DecisionTreeClassifier(**params)
                if set(tree.get_params(deep=False)) != set(params):
                    raise ContractError("Frozen base-tree constructor contract is incompatible")
                return tree
            raise ContractError("Unknown frozen constructor parameter encoding")
        return {key: _decode_parameters(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode_parameters(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ContractError("Invalid frozen constructor parameter value")


def _constructor_identity(estimator):
    return type(estimator).__module__ + "." + type(estimator).__qualname__


def _resolved_step(estimator):
    return {"class": _constructor_identity(estimator),
            "parameters": _encode_parameters(estimator.get_params(deep=False))}


def _construct_frozen_step(description, constructor):
    if not isinstance(description, dict) or set(description) != {"class", "parameters"}:
        raise ContractError("Frozen preprocessing/calibration contract is invalid")
    params = _decode_parameters(description["parameters"])
    estimator = constructor(**params)
    if (description["class"] != _constructor_identity(estimator)
            or set(estimator.get_params(deep=False)) != set(params)):
        raise ContractError("Frozen preprocessing/calibration constructor contract is incompatible")
    return estimator


def _frozen_pipeline(spec, estimator):
    contract = spec.runtime_contract
    required = {"version", "estimator_class", "preprocessing", "calibrator", "dependency_versions"}
    if (set(contract) != required or contract["version"] != RUNTIME_CONTRACT_VERSION
            or contract["estimator_class"] != _constructor_identity(estimator)
            or contract["dependency_versions"] != _versions(spec)
            or set(estimator.get_params(deep=False)) != set(spec.parameters)):
        raise ContractError("Frozen estimator runtime contract is incompatible; local review is required")
    preprocessing = contract["preprocessing"]
    if (not isinstance(preprocessing, dict) or set(preprocessing) != {"impute", "scale"}
            or preprocessing["impute"] is None):
        raise ContractError("Frozen preprocessing contract is incompatible")
    steps = [("impute", _construct_frozen_step(preprocessing["impute"], SimpleImputer))]
    if preprocessing["scale"] is not None:
        steps.append(("scale", _construct_frozen_step(preprocessing["scale"], StandardScaler)))
    calibrated = spec.probability_calibration == "sigmoid" or spec.family in {"ridge", "passive_aggressive"}
    if calibrated != (contract["calibrator"] is not None):
        raise ContractError("Frozen probability calibration contract differs from its specification")
    if calibrated:
        _construct_frozen_step(contract["calibrator"], LogisticRegression)
    return Pipeline([*steps, ("model", estimator)])


def freeze_model_spec(spec):
    """Resolve this local installation's exact untrained pipeline; write/fit nothing.

    Legacy specifications keep their prior interpretation until explicitly
    frozen. The returned specification never consults mutable tuning defaults.
    Dependency or constructor incompatibility fails explicitly at use time.
    """
    if not isinstance(spec, ModelSpec):
        spec = configured_models([spec])[0]
    pipeline = build_estimator(spec)
    if spec.runtime_contract is not None:
        return replace(spec)
    calibrated = spec.probability_calibration == "sigmoid" or spec.family in {"ridge", "passive_aggressive"}
    contract = {
        "version": RUNTIME_CONTRACT_VERSION,
        "estimator_class": _constructor_identity(pipeline.named_steps["model"]),
        "preprocessing": {"impute": _resolved_step(pipeline.named_steps["impute"]),
                          "scale": _resolved_step(pipeline.named_steps["scale"])
                                   if "scale" in pipeline.named_steps else None},
        "calibrator": _resolved_step(LogisticRegression(C=1.0, max_iter=1000,
                                      random_state=spec.random_state)) if calibrated else None,
        "dependency_versions": _versions(spec),
    }
    frozen = replace(spec, parameters=_encode_parameters(pipeline.named_steps["model"].get_params(deep=False)),
                     runtime_contract=contract)
    build_estimator(frozen)
    return frozen


def purged_chronological_split(reference_at, target_close_at, *, fraction=0.2,
                               label_available_at=None):
    """No shuffle: prior label windows end before the held-out reference begins."""
    references = tuple(instant(x) for x in reference_at)
    targets = tuple(instant(x) for x in target_close_at)
    if len(references) != len(targets) or len(references) < 4 or not 0 < fraction < 1:
        raise ContractError("Chronological split requires matching timestamp arrays and valid fraction")
    if any(b <= a for a, b in zip(references, references[1:])):
        raise ContractError("Training references must be strictly chronological")
    if any(end <= start for start, end in zip(references, targets)):
        raise ContractError("Each label target must follow its reference")
    split = min(len(references) - 1, max(1, int(len(references) * (1 - fraction))))
    available = tuple(instant(x) for x in label_available_at) if label_available_at is not None else targets
    if len(available) != len(targets) or any(end > observed for end, observed in zip(targets, available)):
        raise ContractError("Calibration label availability must follow each target close")
    train = np.array([i for i in range(split) if targets[i] <= references[split]
                      and available[i] <= references[split]], dtype=int)
    calibration = np.arange(split, len(references))
    if not len(train):
        raise ContractError("No label windows remain after chronological overlap purge")
    return train, calibration


class ChronologicalSigmoid(ClassifierMixin, BaseEstimator):
    """Fresh base fit plus disjoint, purged chronological probability calibration."""
    def __init__(self, base, random_state=20261012, calibrator=None):
        self.base = base
        self.random_state = random_state
        self.calibrator = calibrator

    def fit_chronological(self, X, y, train, calibration):
        if set(np.unique(y[train])) != {0, 1} or set(np.unique(y[calibration])) != {0, 1}:
            raise ContractError("Base and calibration partitions both require both target classes")
        self.base.fit(X[train], y[train])
        scores = self._scores(X[calibration])
        sigmoid = (_construct_frozen_step(self.calibrator, LogisticRegression) if self.calibrator is not None
                   else LogisticRegression(C=1.0, max_iter=1000, random_state=self.random_state))
        self.sigmoid_ = sigmoid.fit(scores, y[calibration])
        self.classes_ = np.array([0, 1])
        return self

    def predict_proba(self, X):
        return self.sigmoid_.predict_proba(self._scores(X))

    def _scores(self, X):
        classes = np.asarray(self.base.classes_)
        if classes.shape != (2,) or set(classes.tolist()) != {0, 1}:
            raise ContractError("Calibration requires explicit binary class identity")
        if hasattr(self.base, "decision_function"):
            scores = np.asarray(self.base.decision_function(X), dtype=float)
            if scores.shape != (len(X),) or not np.isfinite(scores).all():
                raise ContractError("Calibration received invalid binary decision scores")
            self.score_interface_ = "decision_function"
            return (scores if classes[1] == 1 else -scores).reshape(-1, 1)
        probabilities = np.asarray(self.base.predict_proba(X), dtype=float)
        if (probabilities.shape != (len(X), 2) or not np.isfinite(probabilities).all()
                or (probabilities < 0).any() or (probabilities > 1).any()
                or not np.allclose(probabilities.sum(axis=1), 1, atol=1e-6)):
            raise ContractError("Calibration received an invalid probability vector")
        probability = probabilities[:, np.flatnonzero(classes == 1)[0]]
        # The finite logit is an input to a fitted sigmoid, never a substitute
        # prediction or a display-only cap on confidence.
        probability = np.clip(probability, np.finfo(float).eps, 1 - np.finfo(float).eps)
        self.score_interface_ = "predict_proba_log_odds"
        return (np.log(probability) - np.log1p(-probability)).reshape(-1, 1)


def fit_calibrated_estimator(spec, X, y, *, reference_at=None, target_close_at=None,
                             label_available_at=None, training_cutoff=None, feature_names=None):
    """Fit a new pipeline and, when requested, a disjoint probability calibrator.

    The base estimator's preprocessing fits only its own earlier partition.
    Neither the base nor sigmoid sees later validation or inference rows. A
    missing timestamp or one-class partition is a failure, with no fallback.
    """
    if feature_names is None and hasattr(X, "columns"):
        feature_names = tuple(str(name) for name in X.columns)
    features, labels = np.asarray(X, dtype=float), np.asarray(y)
    if (features.ndim != 2 or not len(features) or not features.shape[1]
            or labels.shape != (len(features),) or not np.isin(labels, [0, 1]).all()
            or np.isinf(features).any()):
        raise ContractError("Fitting requires a numeric matrix and binary labels")
    if set(np.unique(labels)) != {0, 1}:
        raise ContractError("Training requires both target classes; no constant model substitution")
    if label_available_at is not None:
        available = tuple(instant(v) for v in label_available_at)
        if len(available) != len(labels) or training_cutoff is None:
            raise ContractError("Label availability requires matching rows and an explicit cutoff")
        if any(v > instant(training_cutoff) for v in available):
            raise ContractError("Every training label must be observable at the training cutoff")
    else:
        available = None
    calibrated = spec.probability_calibration == "sigmoid" or spec.family in {"ridge", "passive_aggressive"}
    evidence = {"effective_parameters": _encode_parameters(_parameters(spec)), "calibration": None}
    references = tuple(instant(v) for v in reference_at) if reference_at is not None else None
    targets = tuple(instant(v) for v in target_close_at) if target_close_at is not None else None
    for timestamps in (references, targets):
        if timestamps is not None and len(timestamps) != len(labels):
            raise ContractError("Fitting timestamps must match training rows")
    # Validate the complete supplied snapshot before a selected row window can
    # hide bad clocks in excluded rows. Arrival order itself may be nonmonotonic.
    if references is not None and any(b <= a for a, b in zip(references, references[1:])):
        raise ContractError("Training references must be strictly chronological")
    if targets is not None and available is not None and any(end > observed for end, observed in zip(targets, available)):
        raise ContractError("Each training target must have closed before its label was available")
    if references is not None and targets is not None and any(end != start + timedelta(minutes=45)
                                                             for start, end in zip(references, targets)):
        raise ContractError("Training label windows must match the exact 45-minute target")
    if spec.training_max_rows is not None:
        input_rows = len(features)
        features, labels = features[-spec.training_max_rows:], labels[-spec.training_max_rows:]
        references = references[-spec.training_max_rows:] if references is not None else None
        targets = targets[-spec.training_max_rows:] if targets is not None else None
        available = available[-spec.training_max_rows:] if available is not None else None
        evidence.update(input_training_rows=input_rows, training_rows=len(features),
            training_window={"policy": "newest_causal_rows", "max_rows": spec.training_max_rows,
                "excluded_rows": input_rows - len(features),
                "first_reference": references[0].isoformat() if references is not None else None,
                "last_reference": references[-1].isoformat() if references is not None else None},
            class_counts={str(i): int((labels == i).sum()) for i in (0, 1)})
        if set(np.unique(labels)) != {0, 1}:
            raise ContractError("Newest training row window requires both target classes")
    if calibrated:
        if references is None or targets is None or available is None:
            raise ContractError("Probability calibration requires label-window and availability timestamps")
        if len(references) != len(labels) or any(end != start + timedelta(minutes=45)
                                                for start, end in zip(references, targets)):
            raise ContractError("Training label windows must match the exact 45-minute target")
        train, calibration = purged_chronological_split(references, targets,
            fraction=spec.calibration_fraction, label_available_at=available)
        evidence["calibration"] = {"method": "purged_chronological_sigmoid", "base_rows": len(train),
            "calibration_rows": len(calibration), "purged_rows": len(labels) - len(train) - len(calibration),
            "first_calibration_reference": references[calibration[0]].isoformat(),
            "base_latest_target": max(targets[i] for i in train).isoformat(),
            "base_latest_label_available_at": max(available[i] for i in train).isoformat(),
            "base_latest_label_available": max(available[i] for i in train).isoformat()}
        base_rows = len(train)
    else:
        base_rows = len(features)
    estimator = build_estimator(spec)
    from .feature_support import attach_market_support
    estimator = attach_market_support(estimator, feature_names)
    if spec.family == "knn":
        requested = evidence["effective_parameters"]["n_neighbors"]
        effective = min(requested, base_rows)
        evidence.update(requested_n_neighbors=requested, neighbor_count_policy="cap_to_training_rows")
        evidence["effective_parameters"]["n_neighbors"] = effective
        estimator.set_params(model__n_neighbors=effective)
    if calibrated:
        calibrator = spec.runtime_contract["calibrator"] if spec.runtime_contract is not None else None
        estimator = ChronologicalSigmoid(estimator, spec.random_state, calibrator).fit_chronological(features, labels, train, calibration)
        evidence["calibration"]["score_interface"] = estimator.score_interface_
    else:
        estimator.fit(features, labels)
    base = estimator.base if calibrated else estimator
    if "market_support" in base.named_steps:
        evidence["market_feature_support"] = base.named_steps["market_support"].evidence()
    return estimator, evidence


def probability_not_down(estimator, inference_X) -> float:
    probabilities = np.asarray(estimator.predict_proba(inference_X), dtype=float)
    classes = np.asarray(estimator.classes_)
    if probabilities.shape != (1, 2) or set(classes.tolist()) != {0, 1}:
        raise ContractError("Binary predict_proba must identify down=0 and not-down=1")
    if (not np.isfinite(probabilities).all() or (probabilities < 0).any()
            or (probabilities > 1).any() or not np.allclose(probabilities.sum(axis=1), 1, atol=1e-6)):
        raise ContractError("Model emitted an invalid probability vector")
    return float(probabilities[0, np.flatnonzero(classes == 1)[0]])


def _versions(spec) -> dict:
    packages = ["scikit-learn", "numpy", "scipy", "joblib", "threadpoolctl"]
    if spec.family in _DEPENDENCIES:
        packages.append(_DEPENDENCIES[spec.family])
    result = {}
    for name in packages:
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def _export_artifact(artifact: Path, payload) -> str:
    """Publish complete bytes exclusively; a crash cannot expose a partial fit."""
    descriptor, temporary_name = tempfile.mkstemp(prefix=".fit-", suffix=".joblib", dir=artifact.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            joblib.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        # A same-volume hard link publishes complete bytes and fails if the
        # immutable final name already exists (unlike replace/rename on POSIX).
        os.link(temporary, artifact)
        return hashlib.sha256(artifact.read_bytes()).hexdigest()
    finally:
        temporary.unlink(missing_ok=True)


def fit_ensemble(X, y, inference_X, *, config=None, target_version=TARGET_VERSION,
                 training_cutoff, label_available_at, feature_names=None,
                 artifact_dir=None, run_id=None, reference_at=None, target_close_at=None,
                 dataset_metadata=None) -> dict:
    """Fully retrain every enabled classifier and record every success/failure.

    X/y rows must already be causally constructed for TARGET_VERSION. Availability
    is required, never inferred here from event time. An incomplete mean is marked
    explicitly for research observation; eventual execution eligibility is open.
    Artifacts, when requested, are written exclusively under a run-owned directory.
    """
    specs = configured_models(config)
    if target_version != TARGET_VERSION:
        raise ContractError("Training labels must use the exact not-down target version")
    cutoff = instant(training_cutoff)
    available = tuple(instant(v) for v in label_available_at)
    if len(available) != len(y) or any(v > cutoff for v in available):
        raise ContractError("Every training label must be observable at the training cutoff")
    # Late observations can make availability non-monotonic. Reference order,
    # rather than arrival order, determines chronological calibration.
    if reference_at is not None:
        references = tuple(instant(v) for v in reference_at)
        if len(references) != len(y) or any(b <= a for a, b in zip(references, references[1:])):
            raise ContractError("Training references must match rows and be strictly chronological")
    if target_close_at is not None:
        target_closes = tuple(instant(v) for v in target_close_at)
        if len(target_closes) != len(y) or any(end > observed for end, observed in zip(target_closes, available)):
            raise ContractError("Each training target must have closed before its label was available")
        if reference_at is not None and any(end != start + timedelta(minutes=45)
                                            for start, end in zip(references, target_closes)):
            raise ContractError("Training label windows must match the exact 45-minute target")
    if feature_names is None and hasattr(X, "columns"):
        feature_names = tuple(str(c) for c in X.columns)
    if hasattr(inference_X, "columns") and feature_names is not None:
        if tuple(str(c) for c in inference_X.columns) != tuple(feature_names):
            raise ContractError("Inference feature names/order differ from training")
    features = np.asarray(X, dtype=np.float64).copy()
    inference = np.asarray(inference_X, dtype=np.float64).copy()
    labels = np.asarray(y)
    if (features.ndim != 2 or inference.shape != (1, features.shape[1]) or labels.shape != (len(features),)
            or not len(features) or not features.shape[1]):
        raise ContractError("Expected training matrix, matching labels, and exactly one inference row")
    if not np.isin(labels, [0, 1]).all():
        raise ContractError("Labels must be down=0/not-down=1")
    labels = labels.astype(np.uint8)
    if np.isinf(features).any() or np.isinf(inference).any():
        raise ContractError("Infinite features are invalid; explicit missing values may be imputed")
    names = tuple(feature_names or [f"feature_{i}" for i in range(features.shape[1])])
    if len(names) != features.shape[1] or len(set(names)) != len(names):
        raise ContractError("Training feature names must match unique matrix columns")
    data_hash = hashlib.sha256(features.tobytes() + labels.tobytes() +
                               _json({"label_available_at": [v.isoformat() for v in available],
                                      "reference_at": [v.isoformat() for v in references] if reference_at is not None else None,
                                      "target_close_at": [v.isoformat() for v in target_closes] if target_close_at is not None else None}).encode()).hexdigest()
    outputs = []
    for spec in specs:
        start = time.perf_counter()
        output = {"name": spec.name, "family": spec.family, "status": "failed", "probability_not_down": None,
                  "version": None, "training_seconds": 0.0, "inference_seconds": 0.0, "error": None,
                  "artifact_path": None, "artifact_sha256": None, "warnings": [],
                  "training_started_at": _now(), "training_completed_at": None,
                  "inference_started_at": None, "inference_completed_at": None,
                  "artifact_persistence_started_at": None, "artifact_persisted_at": None,
                  "artifact_persistence_seconds": 0.0, "failed_at": None}
        metadata = {"registry_version": REGISTRY_VERSION, "adapter_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "spec": asdict(spec), "target_version": TARGET_VERSION,
                    "training_cutoff": cutoff.isoformat(), "latest_label_available_at": max(available).isoformat(),
                    "training_rows": len(features), "class_counts": {str(i): int((labels == i).sum()) for i in (0, 1)},
                    "feature_names": names, "training_data_sha256": data_hash, "full_retrain": True,
                    "internal_workers": 1, "dependency_versions": _versions(spec),
                    "dataset_metadata": dict(dataset_metadata or {}), "calibration": None}
        output["metadata"] = metadata
        phase = "training"
        try:
            if set(np.unique(labels)) != {0, 1}:
                raise ContractError("Training requires both target classes; no constant model substitution")
            metadata["effective_parameters"] = _encode_parameters(_parameters(spec))
            output["version"] = _sha(metadata)
            with threadpool_limits(limits=1), warnings.catch_warnings(record=True) as observed_warnings:
                warnings.simplefilter("always")
                estimator, fitting_evidence = fit_calibrated_estimator(spec, features, labels,
                    reference_at=reference_at, target_close_at=target_close_at,
                    label_available_at=available, training_cutoff=cutoff, feature_names=names)
                metadata.update(fitting_evidence)
                output["training_seconds"] = time.perf_counter() - start
                output["training_completed_at"] = _now()
                phase = "inference"
                started_inference = time.perf_counter()
                output["inference_started_at"] = _now()
                output["probability_not_down"] = probability_not_down(estimator, inference)
                output["inference_seconds"] = time.perf_counter() - started_inference
                output["inference_completed_at"] = _now()
                output["warnings"] = list(dict.fromkeys(str(w.message) for w in observed_warnings))
            output["version"] = _sha(metadata)
            phase = "artifact_persistence"
            if artifact_dir is not None:
                started_persistence = time.perf_counter()
                output["artifact_persistence_started_at"] = _now()
                if not run_id or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", str(run_id)):
                    raise ContractError("Artifact exports require a safe, explicit immutable run identity")
                directory = Path(artifact_dir) / str(run_id)
                directory.mkdir(parents=True, exist_ok=True)
                artifact = directory / f"{spec.name}.joblib"
                output["artifact_sha256"] = _export_artifact(
                    artifact, {"estimator": estimator, "metadata": metadata, "version": output["version"]})
                output["artifact_path"] = str(artifact.resolve())
                output["artifact_persistence_seconds"] = time.perf_counter() - started_persistence
                output["artifact_persisted_at"] = _now()
            output["status"] = "success"
        except Exception as exc:
            output["failed_at"] = _now()
            if phase == "training":
                output["training_seconds"] = time.perf_counter() - start
                output["training_completed_at"] = output["failed_at"]
            elif phase == "inference":
                output["inference_seconds"] = time.perf_counter() - started_inference
                output["inference_completed_at"] = output["failed_at"]
            elif output["artifact_persistence_started_at"] is not None:
                output["artifact_persistence_seconds"] = time.perf_counter() - started_persistence
            output["probability_not_down"] = None
            output["error"] = {"phase": phase, "type": type(exc).__name__, "message": str(exc)}
        outputs.append(output)
    successes = [out for out in outputs if out["status"] == "success"]
    probability = float(np.mean([out["probability_not_down"] for out in successes])) if successes else None
    return {"outputs": outputs, "configured_count": len(specs), "successful_count": len(successes),
            "complete": len(successes) == len(specs), "consensus_status": "full" if len(successes) == len(specs)
            else "incomplete" if successes else "failed", "probability_not_down": probability,
            "classification_not_down": probability >= 0.5 if probability is not None else None,
            "execution_eligibility": "unresolved_shadow_only", "training_seconds": sum(o["training_seconds"] for o in outputs),
            "inference_seconds": sum(o["inference_seconds"] for o in outputs),
            "target_version": TARGET_VERSION, "training_cutoff": cutoff.isoformat()}
