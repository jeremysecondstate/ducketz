"""Bounded, causal model research for independent 15-minute fresh fits.

Candidates compete on purged expanding-window validation, with a separate
chronological calibration partition inside each fit. The latest holdout is
opened only after candidate membership is fixed and never changes that choice.
No provider, broker, order, fitted-model reuse, or filesystem writes occur here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import timedelta
import hashlib
import json
import math
import time
import warnings

import numpy as np
from sklearn.metrics import balanced_accuracy_score, brier_score_loss, log_loss
from threadpoolctl import threadpool_limits

from .models import (
    ModelSpec, configured_models, fit_calibrated_estimator, purged_chronological_split, registry_status,
)
from .prediction import ContractError, TARGET_VERSION, instant

SELECTION_VERSION = "intraday-purged-calibrated-selection-v1"


@dataclass(frozen=True)
class SelectionConfig:
    max_rows: int = 3000
    min_training_rows: int = 100
    min_calibration_rows: int = 30
    min_validation_rows: int = 30
    min_holdout_rows: int = 60
    folds: int = 2
    holdout_fraction: float = 0.2
    validation_fraction: float = 0.15
    calibration_fraction: float = 0.2
    max_selected: int = 16
    max_brier_gap: float = 0.06
    min_brier_improvement: float = 0.001
    max_fold_brier_std: float = 0.04
    max_fold_baseline_regression: float = 0.01

    def validate(self):
        for name in ("max_rows", "min_training_rows", "min_calibration_rows", "min_validation_rows",
                     "min_holdout_rows", "folds", "max_selected"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ContractError(f"{name} requires a positive integer")
        if self.folds < 2:
            raise ContractError("Model selection requires at least two chronological validation folds")
        for name in ("holdout_fraction", "validation_fraction", "calibration_fraction"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0 < value < 1:
                raise ContractError(f"{name} requires a finite fraction between zero and one")
        for name in ("max_brier_gap", "min_brier_improvement", "max_fold_brier_std", "max_fold_baseline_regression"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ContractError(f"{name} requires a finite nonnegative value")
        return self


@dataclass(frozen=True)
class SelectionResult:
    selected_models: tuple[ModelSpec, ...]
    report: dict
    holdout_start: str | None


def default_candidates() -> tuple[ModelSpec, ...]:
    """Research every genuine tabular family with modest regularized CPU fits.

    Optional dependencies stay in the grid: an unavailable library is recorded
    as a real failed candidate, rather than silently removing its family.
    """
    definitions = (
        ("logistic_strong", "logistic", {"C": 0.05}),
        ("logistic_moderate", "logistic", {"C": 0.3}),
        ("gaussian_nb_smooth", "gaussian_nb", {"var_smoothing": 0.01}),
        ("gaussian_nb_regularized", "gaussian_nb", {"var_smoothing": 0.1}),
        ("knn_uniform", "knn", {"n_neighbors": 41, "weights": "uniform"}),
        ("lda_shrunk", "lda", {"solver": "lsqr", "shrinkage": 0.5}),
        ("random_forest_shallow", "random_forest", {"n_estimators": 64, "max_depth": 3, "min_samples_leaf": 24}),
        ("random_forest_regularized", "random_forest", {"n_estimators": 64, "max_depth": 5, "min_samples_leaf": 40}),
        ("extra_trees_shallow", "extra_trees", {"n_estimators": 64, "max_depth": 3, "min_samples_leaf": 24}),
        ("hist_boost_shallow", "hist_gradient_boosting", {"max_iter": 60, "max_leaf_nodes": 7,
                                                          "min_samples_leaf": 30, "l2_regularization": 10.0}),
        ("gradient_boost_shallow", "gradient_boosting", {"n_estimators": 50, "max_depth": 1,
                                                        "min_samples_leaf": 30}),
        ("adaboost_regularized", "adaboost", {"n_estimators": 50, "learning_rate": 0.05}),
        ("xgboost_shallow", "xgboost", {"n_estimators": 64, "max_depth": 2,
                                         "min_child_weight": 24, "reg_lambda": 10.0}),
        ("lightgbm_shallow", "lightgbm", {"n_estimators": 64, "max_depth": 3,
                                           "num_leaves": 7, "min_child_samples": 30,
                                           "reg_lambda": 10.0}),
        ("catboost_shallow", "catboost", {"iterations": 64, "depth": 3, "l2_leaf_reg": 10.0}),
        ("qda_regularized", "qda", {"reg_param": 0.5}),
        ("ridge_regularized", "ridge", {"alpha": 10.0}),
        ("passive_aggressive_regularized", "passive_aggressive", {"C": 0.05, "max_iter": 500}),
        ("mlp_single_layer", "mlp", {"hidden_layer_sizes": (16,), "alpha": 1.0, "max_iter": 120}),
        ("mlp_two_layers", "mlp", {"hidden_layer_sizes": (24, 8), "alpha": 3.0, "max_iter": 120}),
        ("mlp_compact", "mlp", {"hidden_layer_sizes": (8, 4), "alpha": 10.0, "max_iter": 120}),
    )
    return tuple(ModelSpec(name, family=family, parameters=params,
                           probability_calibration="sigmoid") for name, family, params in definitions)


def _catalog_report(specs):
    """Expose adapter availability separately from configured/attempted fits."""
    catalog = registry_status()
    known = {item["family"] for item in catalog}
    for family in sorted({spec.family for spec in specs} - known):
        catalog.append({"family": family, "status": "adapter_not_implemented",
                        "dependency": "No registered exact-target training adapter"})
    for item in catalog:
        item["candidate_names"] = [spec.name for spec in specs if spec.family == item["family"]]
        item["configured"] = bool(item["candidate_names"])
        if not item["configured"]:
            item["nonselection_reason"] = ("adapter_not_implemented" if item["status"] == "adapter_not_implemented"
                                            else "not_configured")
    implemented = [item for item in catalog if item["status"] != "adapter_not_implemented"]
    coverage = {"configured_variant_count": len(specs), "attempted_variant_count": 0,
                "configured_family_count": len({spec.family for spec in specs}),
                "attempted_family_count": 0, "implemented_family_count": len(implemented),
                "available_family_count": sum(item["status"] == "available" for item in implemented),
                "unconfigured_implemented_families": [item["family"] for item in implemented if not item["configured"]],
                "dependency_unavailable_families": [item["family"] for item in implemented
                                                    if item["status"] == "dependency_unavailable"],
                "adapter_not_implemented_families": [item["family"] for item in catalog
                                                     if item["status"] == "adapter_not_implemented"]}
    return catalog, coverage


def _probabilities(estimator, X):
    probabilities = np.asarray(estimator.predict_proba(X), dtype=float)
    classes = np.asarray(estimator.classes_)
    if (probabilities.shape != (len(X), 2) or set(classes.tolist()) != {0, 1}
            or not np.isfinite(probabilities).all() or (probabilities < 0).any()
            or (probabilities > 1).any() or not np.allclose(probabilities.sum(axis=1), 1, atol=1e-6)):
        raise ContractError("Candidate emitted invalid binary probabilities")
    return probabilities[:, np.flatnonzero(classes == 1)[0]]


def probability_metrics(y, probability) -> dict:
    """Scores keep probability confidence separate from classification success."""
    y, p = np.asarray(y), np.asarray(probability, dtype=float)
    if y.ndim != 1 or p.shape != y.shape or not len(y) or not np.isin(y, [0, 1]).all():
        raise ContractError("Metrics require nonempty matching binary labels and probabilities")
    if not np.isfinite(p).all() or (p < 0).any() or (p > 1).any():
        raise ContractError("Metrics require finite probabilities between zero and one")
    both_classes = set(np.unique(y)) == {0, 1}
    return {"rows": len(y), "brier": float(brier_score_loss(y, p)),
            "log_loss": float(log_loss(y, np.column_stack((1-p, p)), labels=[0, 1])),
            "balanced_accuracy": float(balanced_accuracy_score(y, p >= .5)) if both_classes else None,
            "positive_label_fraction": float(y.mean()), "mean_probability": float(p.mean()),
            "saturated_probability_fraction": float(np.mean((p <= .001) | (p >= .999))),
            "both_classes": both_classes}


def _indices_report(indices, references, targets, available):
    return {"rows": len(indices), "first_reference": references[indices[0]].isoformat(),
            "last_reference": references[indices[-1]].isoformat(),
            "latest_target_close": max(targets[i] for i in indices).isoformat(),
            "latest_label_available": max(available[i] for i in indices).isoformat()}


def _fit_partitions(pool, references, targets, available, config, training_max_rows=None):
    # Live fitting caps the complete causal pool before its calibration split.
    # Preserve those same rows and purge boundaries when scoring candidates.
    pool = pool[-training_max_rows:] if training_max_rows is not None else pool
    train, calibration = purged_chronological_split(
        [references[i] for i in pool], [targets[i] for i in pool],
        fraction=config.calibration_fraction, label_available_at=[available[i] for i in pool])
    train, calibration = pool[train], pool[calibration]
    if len(calibration) < config.min_calibration_rows:
        raise ContractError("Insufficient chronological calibration history")
    if len(train) < config.min_training_rows:
        raise ContractError("Insufficient base-fit rows after label-window and receipt purge")
    return train, calibration


def _fit(spec, X, y, train, calibration, pool, references, targets, available, cutoff, feature_names=None):
    if set(np.unique(y[train])) != {0, 1} or set(np.unique(y[calibration])) != {0, 1}:
        raise ContractError("Base fit and calibration both require both target classes")
    with threadpool_limits(limits=1), warnings.catch_warnings(record=True) as observed:
        warnings.simplefilter("always")
        model, _ = fit_calibrated_estimator(spec, X[pool], y[pool],
            reference_at=[references[i] for i in pool], target_close_at=[targets[i] for i in pool],
            label_available_at=[available[i] for i in pool], training_cutoff=cutoff, feature_names=feature_names)
    return model, list(dict.fromkeys(str(w.message) for w in observed))


def _validated_dataset(dataset):
    X, y = np.asarray(dataset.X, dtype=float), np.asarray(dataset.y)
    references = tuple(instant(v) for v in dataset.reference_at)
    targets = tuple(instant(v) for v in dataset.target_close_at)
    available = tuple(instant(v) for v in dataset.label_available_at)
    if (X.ndim != 2 or not X.shape[1] or y.shape != (len(X),)
            or len(references) != len(X) or len(targets) != len(X) or len(available) != len(X)):
        raise ContractError("Selection needs matching matrix, labels and all causal timestamps")
    if not np.isin(y, [0, 1]).all() or np.isinf(X).any():
        raise ContractError("Selection needs binary labels and features without infinite values")
    if any(b <= a for a, b in zip(references, references[1:])):
        raise ContractError("Selection references must be strictly chronological")
    if any(end != ref + timedelta(minutes=45) for ref, end in zip(references, targets)):
        raise ContractError("Selection requires the exact 45-minute target")
    if any(receipt < end for receipt, end in zip(available, targets)):
        raise ContractError("Selection label availability cannot precede target close")
    if dataset.manifest.get("target_version") != TARGET_VERSION:
        raise ContractError("Selection requires exact-target dataset provenance")
    cutoff = instant(dataset.manifest["training_cutoff"])
    if any(receipt > cutoff for receipt in available):
        raise ContractError("Selection cannot consume labels unavailable at the snapshot cutoff")
    return X, y.astype(np.uint8), references, targets, available


def select_models(dataset, *, candidates=None, config=None, previous_holdout_start=None) -> SelectionResult:
    """Select using search folds, then report final holdout without reconsidering.

    Repeated supervisors should retain ``previous_holdout_start`` for a given
    nightly search, so an inspected holdout cannot slide into its search folds.
    Empty selections record honest insufficient/no-advantage results; they do
    not authorize stopping another nightly operation or a manual trader.
    """
    config = (config or SelectionConfig()).validate()
    specs = configured_models(default_candidates() if candidates is None else candidates)
    # Deployment must use the same disjoint calibration as candidate research.
    specs = tuple(replace(spec, probability_calibration="sigmoid",
                          calibration_fraction=config.calibration_fraction) for spec in specs)
    catalog, coverage = _catalog_report(specs)
    catalog_status = {item["family"]: item["status"] for item in catalog}
    X, y, references, targets, available = _validated_dataset(dataset)
    admitted = np.arange(max(0, len(X)-config.max_rows), len(X), dtype=int)
    report = {"selection_version": SELECTION_VERSION, "status": "insufficient_history",
              "configuration": asdict(config), "input_rows": len(X), "admitted_rows": len(admitted),
              "candidate_count": len(specs), "candidates": [], "selected_models": [], "holdout": None,
              "model_catalog": catalog, "candidate_coverage": coverage,
              "selection_policy": "purged_expanding_validation_only_then_report_only_final_holdout",
              "deployment_policy": "portable_specs_fresh_fit_with_disjoint_chronological_sigmoid",
              "source": dict(dataset.manifest), "limitations": [
                  "A 100% forecast probability is confidence, not observed accuracy or proof of overfitting.",
                  "Validation scores and training gaps diagnose overfit risk; they cannot guarantee trading profit.",
                  "Overlapping labels and repeated searches create dependent evidence; fold spread is not a significance test.",
                  "Final holdout is report-only and cannot choose, remove, or reorder models.",
                  "Only supplied exact-target causal features are researched; no unrelated external training inputs are invented."]}
    holdout_start = instant(previous_holdout_start).isoformat() if previous_holdout_start else None
    try:
        if not len(admitted):
            raise ContractError("No matured training observations")
        if holdout_start:
            boundary = instant(holdout_start)
            if boundary <= references[admitted[0]]:
                raise ContractError("Retained holdout boundary leaves no admitted search history")
            holdout = admitted[[references[i] >= boundary for i in admitted]]
        else:
            count = max(config.min_holdout_rows, int(len(admitted)*config.holdout_fraction))
            if count >= len(admitted):
                raise ContractError("Insufficient final holdout and search history")
            holdout = admitted[-count:]
            boundary = references[holdout[0]]
            holdout_start = boundary.isoformat()
        if len(holdout) < config.min_holdout_rows:
            raise ContractError("Insufficient final holdout observations")
        search = np.asarray([i for i in admitted if references[i] < boundary
                             and targets[i] <= boundary and available[i] <= boundary], dtype=int)
        validation_rows = max(config.min_validation_rows, int(len(search)*config.validation_fraction))
        first_validation = len(search)-config.folds*validation_rows
        if first_validation < config.min_training_rows+config.min_calibration_rows:
            raise ContractError("Insufficient expanding-window search history")
        folds = []
        for fold in range(config.folds):
            start = first_validation+fold*validation_rows
            validation = search[start:start+validation_rows]
            cutoff = references[validation[0]]
            pool = np.asarray([i for i in search[:start] if targets[i] <= cutoff and available[i] <= cutoff], dtype=int)
            train, calibration = _fit_partitions(pool, references, targets, available, config)
            if set(np.unique(y[validation])) != {0, 1}:
                raise ContractError("Search validation folds require both target classes")
            folds.append((pool, train, calibration, validation))
        final_train, final_calibration = _fit_partitions(search, references, targets, available, config)
    except ContractError as exc:
        report["reason"] = str(exc)
        report["candidates"] = [{"spec": asdict(spec), "status": "not_run", "selected": False,
                                 "selection_reason": "insufficient_history", "catalog_status": catalog_status[spec.family],
                                 "reason": str(exc), "folds": []} for spec in specs]
        return SelectionResult((), report, holdout_start)

    report["search"] = _indices_report(search, references, targets, available)
    report["holdout_boundary"] = holdout_start
    report["holdout_rows"] = len(holdout)
    report["folds"] = [{"fold": number,
                        "training": _indices_report(train, references, targets, available),
                        "calibration": _indices_report(calibration, references, targets, available),
                        "validation": _indices_report(validation, references, targets, available)}
                       for number, (_, train, calibration, validation) in enumerate(folds)]
    # This fingerprint intentionally excludes holdout feature/label values.
    fingerprint = hashlib.sha256(X[search].tobytes()+y[search].tobytes())
    fingerprint.update(json.dumps({"specs": [asdict(s) for s in specs], "config": asdict(config),
                                   "feature_names": list(getattr(dataset, "feature_names", ())),
                                   "population_contract": getattr(dataset, "manifest", {}).get("population_contract"),
                                   "feature_contract": getattr(dataset, "manifest", {}).get("feature_contract"),
                                   "references": [references[i].isoformat() for i in search],
                                   "target_closes": [targets[i].isoformat() for i in search],
                                   "label_availability": [available[i].isoformat() for i in search]},
                                  sort_keys=True, default=list).encode())
    report["search_sha256"] = fingerprint.hexdigest()
    supported = []
    for spec in specs:
        started = time.perf_counter()
        record = {"spec": asdict(spec), "status": "failed", "folds": [], "warnings": [],
                  "rejection_reasons": [], "seconds": 0.0, "selected": False,
                  "selection_reason": "fit_failed", "catalog_status": catalog_status[spec.family]}
        try:
            for number, (pool, _, _, validation) in enumerate(folds):
                train, calibration = _fit_partitions(pool, references, targets, available, config,
                                                      spec.training_max_rows)
                model, observed = _fit(spec, X, y, train, calibration, pool, references, targets,
                                       available, references[validation[0]], getattr(dataset, "feature_names", None))
                record["warnings"].extend(w for w in observed if w not in record["warnings"])
                p = _probabilities(model, X[validation])
                metrics = probability_metrics(y[validation], p)
                train_metrics = probability_metrics(y[train], _probabilities(model, X[train]))
                base_probability = float(y[np.concatenate((train, calibration))].mean())
                baseline = probability_metrics(y[validation], np.full(len(validation), base_probability))
                raw_train = raw_validation = None
                if hasattr(model.base, "predict_proba"):
                    raw_train = probability_metrics(y[train], _probabilities(model.base, X[train]))
                    raw_validation = probability_metrics(y[validation], _probabilities(model.base, X[validation]))
                gap = metrics["brier"]-train_metrics["brier"]
                raw_gap = raw_validation["brier"]-raw_train["brier"] if raw_train else None
                record["folds"].append({"fold": number, "validation": metrics, "training": train_metrics,
                                        "training_partition": _indices_report(train, references, targets, available),
                                        "calibration_partition": _indices_report(calibration, references, targets, available),
                                        "raw_training": raw_train, "raw_validation": raw_validation,
                                        "baseline": baseline, "brier_improvement": baseline["brier"]-metrics["brier"],
                                        "brier_generalization_gap": gap, "raw_brier_generalization_gap": raw_gap})
            values = record["folds"]
            briers = np.asarray([v["validation"]["brier"] for v in values])
            improvements = np.asarray([v["brier_improvement"] for v in values])
            gaps = [max(v["brier_generalization_gap"], v["raw_brier_generalization_gap"]
                        if v["raw_brier_generalization_gap"] is not None else v["brier_generalization_gap"])
                    for v in values]
            summary = {"mean_brier": float(briers.mean()), "fold_brier_std": float(briers.std(ddof=0)),
                       "mean_log_loss": float(np.mean([v["validation"]["log_loss"] for v in values])),
                       "mean_balanced_accuracy": float(np.mean([v["validation"]["balanced_accuracy"] for v in values])),
                       "mean_baseline_brier_improvement": float(improvements.mean()),
                       "worst_baseline_brier_improvement": float(improvements.min()),
                       "best_baseline_brier_improvement": float(improvements.max()),
                       "maximum_brier_generalization_gap": max(gaps)}
            record["summary"] = summary
            if summary["mean_baseline_brier_improvement"] < config.min_brier_improvement:
                record["rejection_reasons"].append("insufficient_validation_advantage_over_causal_frequency_baseline")
            if summary["worst_baseline_brier_improvement"] < -config.max_fold_baseline_regression:
                record["rejection_reasons"].append("validation_regression_in_a_chronological_fold")
            if summary["maximum_brier_generalization_gap"] > config.max_brier_gap:
                record["rejection_reasons"].append("training_validation_brier_gap")
            if summary["fold_brier_std"] > config.max_fold_brier_std:
                record["rejection_reasons"].append("unstable_chronological_fold_brier")
            record["status"] = "rejected" if record["rejection_reasons"] else "supported"
            record["selection_reason"] = "validation_rejected" if record["rejection_reasons"] else "qualified"
            if record["status"] == "supported":
                supported.append((spec, record))
        except Exception as exc:
            record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            if catalog_status[spec.family] in {"dependency_unavailable", "adapter_not_implemented"}:
                record["selection_reason"] = catalog_status[spec.family]
        record["seconds"] = time.perf_counter()-started
        report["candidates"].append(record)
    coverage["attempted_variant_count"] = len(report["candidates"])
    coverage["attempted_family_count"] = len({item["spec"]["family"] for item in report["candidates"]})

    ranked = sorted(supported, key=lambda item: (item[1]["summary"]["mean_brier"],
                                                item[1]["summary"]["mean_log_loss"], item[0].name))
    # Keep independent families in the ensemble; architectures still compete
    # within their genuine family and every candidate's results remain logged.
    winners, families = [], set()
    for spec, record in ranked:
        if spec.family in families:
            record["selection_reason"] = "duplicate_family"
            continue
        if len(winners) >= config.max_selected:
            record["selection_reason"] = "configured_cap"
            continue
        families.add(spec.family)
        winners.append(spec)
        record["selected"] = True
        record["selection_reason"] = "selected_validation_rank"
    selected = tuple(winners)  # Membership is now fixed before touching holdout values.
    report["selected_models"] = [asdict(s) for s in selected]
    report["status"] = "selected" if selected else "no_supported_candidate"
    report["holdout"] = {"purpose": "report_only_no_selection_or_activation_gate", "models": [],
                         "partition": _indices_report(holdout, references, targets, available)}
    if selected:
        baseline_p = float(y[np.concatenate((final_train, final_calibration))].mean())
        report["holdout"]["baseline"] = probability_metrics(y[holdout], np.full(len(holdout), baseline_p))
        predictions = []
        for spec in selected:
            record = {"name": spec.name, "status": "failed"}
            try:
                train, calibration = _fit_partitions(search, references, targets, available, config,
                                                      spec.training_max_rows)
                model, observed = _fit(spec, X, y, train, calibration, search, references, targets,
                                       available, boundary, getattr(dataset, "feature_names", None))
                p = _probabilities(model, X[holdout])
                predictions.append(p)
                record.update(status="evaluated", metrics=probability_metrics(y[holdout], p), warnings=observed,
                    training_partition=_indices_report(train, references, targets, available),
                    calibration_partition=_indices_report(calibration, references, targets, available))
            except Exception as exc:
                record["error"] = {"type": type(exc).__name__, "message": str(exc)}
            report["holdout"]["models"].append(record)
        if len(predictions) == len(selected):
            report["holdout"]["ensemble"] = probability_metrics(y[holdout], np.mean(predictions, axis=0))
        else:
            report["holdout"]["ensemble"] = None
            report["limitations"].append("A selected model failed the report-only holdout fit; no partial ensemble score is substituted.")
    return SelectionResult(selected, report, holdout_start)
