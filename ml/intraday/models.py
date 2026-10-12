"""Independent, fresh-fit classifiers for the exact intraday not-down target.

This module has no provider, account or order dependencies. Callers supply a
causal numeric dataset and its per-label availability evidence. A failed model
has a failure record, never a stale-model or constant-probability substitute.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
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

REGISTRY_VERSION = "intraday-independent-tabular-v1"

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

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", self.name):
            raise ContractError("Model name must be a safe unique identifier")
        if self.family is None:
            object.__setattr__(self, "family", self.name)
        if not 0 < self.calibration_fraction < 1:
            raise ContractError("Calibration fraction must be between zero and one")


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
    params = {**DEFAULT_PARAMETERS[spec.family], **dict(spec.parameters)}
    if params.get("warm_start", False):
        raise ContractError("Warm-start is forbidden for the full-retraining benchmark")
    if params.get("n_jobs", 1) != 1 or params.get("thread_count", 1) != 1:
        raise ContractError("This research adapter requires one internal worker per model")
    if spec.family in {"random_forest", "extra_trees", "hist_gradient_boosting", "gradient_boosting",
                       "adaboost", "xgboost", "lightgbm", "logistic", "mlp", "passive_aggressive"}:
        params["random_state"] = spec.random_state
    elif spec.family == "catboost":
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
    if spec.family == "adaboost":
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
    steps = [("impute", SimpleImputer(strategy="median", keep_empty_features=True))]
    if spec.family in {"logistic", "knn", "lda", "qda", "gaussian_nb", "ridge", "passive_aggressive", "mlp"}:
        steps.append(("scale", StandardScaler()))
    steps.append(("model", estimator))
    return Pipeline(steps)


def purged_chronological_split(reference_at, target_close_at, *, fraction=0.2):
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
    train = np.array([i for i in range(split) if targets[i] <= references[split]], dtype=int)
    calibration = np.arange(split, len(references))
    if not len(train):
        raise ContractError("No label windows remain after chronological overlap purge")
    return train, calibration


class ChronologicalSigmoid(ClassifierMixin, BaseEstimator):
    """Fresh base fit plus disjoint, purged chronological probability calibration."""
    def __init__(self, base, random_state=20261012):
        self.base = base
        self.random_state = random_state

    def fit_chronological(self, X, y, train, calibration):
        if set(np.unique(y[train])) != {0, 1} or set(np.unique(y[calibration])) != {0, 1}:
            raise ContractError("Base and calibration partitions both require both target classes")
        self.base.fit(X[train], y[train])
        scores = np.asarray(self.base.decision_function(X[calibration])).reshape(-1, 1)
        self.sigmoid_ = LogisticRegression(C=1.0, max_iter=1000, random_state=self.random_state).fit(scores, y[calibration])
        self.classes_ = np.array([0, 1])
        return self

    def predict_proba(self, X):
        return self.sigmoid_.predict_proba(np.asarray(self.base.decision_function(X)).reshape(-1, 1))


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
    packages = ["scikit-learn", "numpy", "joblib", "threadpoolctl"]
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
            metadata["effective_parameters"] = _parameters(spec)
            output["version"] = _sha(metadata)
            estimator = build_estimator(spec)
            with threadpool_limits(limits=1), warnings.catch_warnings(record=True) as observed_warnings:
                warnings.simplefilter("always")
                if spec.family in {"ridge", "passive_aggressive"}:
                    if reference_at is None or target_close_at is None:
                        raise ContractError("Non-probability models require label-window timestamps for chronological calibration")
                    train, calibration = purged_chronological_split(reference_at, target_close_at,
                                                                    fraction=spec.calibration_fraction)
                    if len(reference_at) != len(labels):
                        raise ContractError("Calibration timestamps differ from training rows")
                    metadata["calibration"] = {"method": "purged_chronological_sigmoid", "base_rows": len(train),
                                               "calibration_rows": len(calibration),
                                               "purged_rows": len(labels) - len(train) - len(calibration),
                                               "first_calibration_reference": instant(reference_at[calibration[0]]).isoformat(),
                                               "base_latest_target": instant(target_close_at[train[-1]]).isoformat()}
                    estimator = ChronologicalSigmoid(estimator, spec.random_state).fit_chronological(features, labels, train, calibration)
                else:
                    estimator.fit(features, labels)
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
