"""Bounded chronological model research for the exact 45-minute intraday target.

The most recent session is a final gate for a proposal frozen using earlier
sessions. It never ranks candidates or supplies replacement candidates. Reports
are local research evidence, not profitability estimates or order instructions.
Callers must persist/reuse the report for that holdout session instead of tuning
again against the same holdout during subsequent overnight supervision wakes.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import timedelta
import hashlib
import json
import math
import time
import warnings
from zoneinfo import ZoneInfo

import numpy as np
from threadpoolctl import threadpool_limits

from ml.intraday.models import (
    ModelSpec, configured_models, fit_calibrated_estimator, freeze_model_spec,
    purged_chronological_split, registry_status,
)
from ml.intraday.prediction import ContractError, TARGET_VERSION, instant
from .training_timing import (LEGACY, HISTORICAL_RESEARCH, timing_contract,
                              timing_mode_from_metadata, validate_timing_mode)

SELECTION_VERSION = "intraday-purged-session-selection-v2"


class InsufficientEvidenceError(ContractError):
    """A terminal sample/class gate for this session, rather than a failed fit."""


class _SearchDeadlineReached(TimeoutError):
    """The search budget expired before a candidate fit began."""


@dataclass(frozen=True)
class SelectionConfig:
    cv_folds: int = 3
    min_training_sessions: int = 5
    min_training_rows: int = 160
    min_validation_rows: int = 24
    min_holdout_rows: int = 24
    min_class_count: int = 5
    min_calibration_rows: int = 32
    max_selected: int = 16
    max_candidates: int = 21
    max_training_rows: int = 4096
    max_search_seconds: float = 180.0
    min_brier_improvement: float = 0.002
    min_log_loss_improvement: float = 0.0
    min_winning_fold_fraction: float = 2 / 3
    max_ece: float = 0.15
    extreme_probability: float = 0.95
    min_extreme_samples: int = 8
    max_extreme_error_rate: float = 0.25
    max_incumbent_degradation: float = 0.0

    def __post_init__(self):
        positive_ints = ("cv_folds", "min_training_sessions", "min_training_rows", "min_validation_rows",
                         "min_holdout_rows", "min_class_count", "min_calibration_rows", "max_selected",
                         "max_candidates", "max_training_rows", "min_extreme_samples")
        for field in positive_ints:
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractError(f"{field} must be a positive integer")
        if self.cv_folds > 5 or self.max_candidates > 24 or self.max_selected > 16:
            raise ContractError("Research budget permits at most 5 folds, 24 candidates and 16 selected models")
        if self.max_training_rows < self.min_training_rows:
            raise ContractError("Training row budget is below the training minimum")
        for field in ("max_search_seconds", "min_brier_improvement", "min_log_loss_improvement",
                      "min_winning_fold_fraction", "max_ece", "extreme_probability",
                      "max_extreme_error_rate", "max_incumbent_degradation"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ContractError(f"{field} must be a finite nonnegative number")
        if (self.max_search_seconds <= 0 or not 0 < self.min_winning_fold_fraction <= 1
                or not 0 <= self.max_ece <= 1 or not 0.5 < self.extreme_probability < 1
                or not 0 <= self.max_extreme_error_rate <= 1):
            raise ContractError("Invalid probability gate or search deadline")


def default_candidates() -> tuple[ModelSpec, ...]:
    """Research every genuine tabular family; unavailable libraries stay explicit."""
    definitions = (
        ("logistic_strong", "logistic", {"C": 0.05}),
        ("logistic_moderate", "logistic", {"C": 0.5}),
        ("nb_smooth", "gaussian_nb", {"var_smoothing": 1e-5}),
        ("gaussian_nb_regularized", "gaussian_nb", {"var_smoothing": 0.1}),
        ("lda_shrunk", "lda", {"shrinkage": 0.8}),
        ("knn_uniform", "knn", {"n_neighbors": 31, "weights": "uniform"}),
        ("forest_shallow", "random_forest", {"n_estimators": 64, "max_depth": 3, "min_samples_leaf": 16}),
        ("forest_regularized", "random_forest", {"n_estimators": 64, "max_depth": 5, "min_samples_leaf": 32}),
        ("extra_shallow", "extra_trees", {"n_estimators": 64, "max_depth": 4, "min_samples_leaf": 16}),
        ("hist_regularized", "hist_gradient_boosting",
         {"max_iter": 80, "max_leaf_nodes": 7, "l2_regularization": 5.0}),
        ("boost_shallow", "gradient_boosting",
         {"n_estimators": 64, "max_depth": 1, "min_samples_leaf": 16}),
        ("adaboost_regularized", "adaboost", {"n_estimators": 50, "learning_rate": 0.05}),
        ("catboost_shallow", "catboost", {"iterations": 64, "depth": 3, "l2_leaf_reg": 10.0}),
        ("qda_regularized", "qda", {"reg_param": 0.5}),
        ("ridge_regularized", "ridge", {"alpha": 10.0}),
        ("passive_aggressive_regularized", "passive_aggressive", {"C": 0.05, "max_iter": 500}),
        ("mlp_small", "mlp", {"hidden_layer_sizes": (16,), "alpha": 1.0, "max_iter": 120}),
        ("mlp_two_layer", "mlp", {"hidden_layer_sizes": (32, 8), "alpha": 2.0, "max_iter": 120}),
        ("mlp_compact", "mlp", {"hidden_layer_sizes": (8, 4), "alpha": 10.0, "max_iter": 120}),
        ("xgb_regularized", "xgboost", {"n_estimators": 80, "max_depth": 2, "reg_lambda": 5.0}),
        ("lightgbm_regularized", "lightgbm", {"n_estimators": 80, "num_leaves": 7, "reg_lambda": 5.0}),
    )
    return tuple(ModelSpec(name, family=family, parameters=parameters, calibration_fraction=0.25,
                           probability_calibration="sigmoid")
                 for name, family, parameters in definitions)


def _catalog_report(specs):
    """Separate adapter discovery from configured and actually attempted research."""
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


def _finish_report(report, specs):
    """Account for every configured candidate without changing promotion policy."""
    statuses = {item["family"]: item["status"] for item in report["model_catalog"]}
    recorded = {item["spec"]["name"] for item in report["candidates"]}
    for spec in specs:
        if spec.name not in recorded:
            report["candidates"].append({"spec": asdict(spec), "status": "not_run", "folds": [],
                "metrics": None, "reasons": list(report["reasons"]), "selected": False,
                "shortlisted": False, "catalog_status": statuses[spec.family],
                "selection_reason": "search_budget_exhausted" if report["status"] == "budget_exhausted"
                                    else "insufficient_data"})
    selected = {item["name"] for item in report["selected_specs"]}
    for row in report["candidates"]:
        row["selected"] = row["spec"]["name"] in selected
        if row["selected"]:
            row["selection_reason"] = "selected_validation_rank"
        elif row.get("shortlisted"):
            row["selection_reason"] = "proposal_not_promoted"
    attempted = [item for item in report["candidates"] if item["status"] != "not_run"]
    report["candidate_coverage"]["attempted_variant_count"] = len(attempted)
    report["candidate_coverage"]["attempted_family_count"] = len({item["spec"]["family"] for item in attempted})
    return report


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def probability_metrics(y, probabilities, *, extreme_probability=0.95) -> dict:
    """Proper probability scores plus auditable reliability/overconfidence bins."""
    labels = np.asarray(y)
    p = np.asarray(probabilities, dtype=float)
    if (labels.ndim != 1 or p.shape != labels.shape or not len(labels)
            or not np.isin(labels, [0, 1]).all() or not np.isfinite(p).all()
            or (p < 0).any() or (p > 1).any() or not 0.5 < extreme_probability < 1):
        raise ContractError("Probability metrics require matching nonempty binary labels and valid probabilities")
    labels = labels.astype(int)
    correct = (p >= 0.5) == labels
    clipped = np.clip(p, 1e-15, 1 - 1e-15)
    bins = []
    ece = 0.0
    assignments = np.minimum((p * 10).astype(int), 9)
    for index in range(10):
        mask = assignments == index
        count = int(mask.sum())
        predicted = float(p[mask].mean()) if count else None
        observed = float(labels[mask].mean()) if count else None
        if count:
            ece += count / len(labels) * abs(predicted - observed)
        bins.append({"lower": index / 10, "upper": (index + 1) / 10,
                     "count": count, "mean_probability": predicted, "observed_not_down_rate": observed})
    extreme = (p >= extreme_probability) | (p <= 1 - extreme_probability)
    extreme_count = int(extreme.sum())
    extreme_errors = int((extreme & ~correct).sum())
    recalls = [float(correct[labels == label].mean()) for label in (0, 1) if (labels == label).any()]
    return {"scored": len(labels), "class_counts": {str(label): int((labels == label).sum()) for label in (0, 1)},
            "accuracy": float(correct.mean()), "balanced_accuracy": float(np.mean(recalls)) if len(recalls) == 2 else None,
            "brier": float(np.mean((p - labels) ** 2)),
            "log_loss": float(np.mean(-labels * np.log(clipped) - (1 - labels) * np.log(1 - clipped))),
            "mean_probability": float(p.mean()), "observed_not_down_rate": float(labels.mean()),
            "expected_calibration_error": float(ece), "calibration_bins": bins,
            "extreme_probability_threshold": extreme_probability, "extreme_count": extreme_count,
            "extreme_error_count": extreme_errors,
            "extreme_error_rate": extreme_errors / extreme_count if extreme_count else None}


def _class_gate(labels, minimum, name):
    return [f"{name}_class_{label}_below_{minimum}" for label in (0, 1) if int((labels == label).sum()) < minimum]


def _metric_gates(metrics, baselines, config):
    reasons = []
    for baseline_name, baseline in baselines.items():
        if metrics["brier"] > baseline["brier"] - config.min_brier_improvement:
            reasons.append(f"brier_does_not_improve_{baseline_name}")
        if metrics["log_loss"] > baseline["log_loss"] - config.min_log_loss_improvement:
            reasons.append(f"log_loss_does_not_improve_{baseline_name}")
    if metrics["expected_calibration_error"] > config.max_ece:
        reasons.append("calibration_error_above_limit")
    if (metrics["extreme_count"] >= config.min_extreme_samples
            and metrics["extreme_error_rate"] > config.max_extreme_error_rate):
        reasons.append("high_confidence_error_rate_above_limit")
    return reasons


def _arrays(dataset, as_of, session_ids, session_timezone):
    X, y = np.asarray(dataset.X, dtype=float), np.asarray(dataset.y)
    references = tuple(instant(value) for value in dataset.reference_at)
    targets = tuple(instant(value) for value in dataset.target_close_at)
    availability = tuple(instant(value) for value in dataset.label_available_at)
    if (X.ndim != 2 or not X.shape[1] or y.shape != (len(X),) or not len(X)
            or not len(references) == len(targets) == len(availability) == len(X)
            or not np.isin(y, [0, 1]).all() or np.isinf(X).any()):
        raise ContractError("Selection dataset requires matching numeric features, binary labels and timestamp arrays")
    names = tuple(str(name) for name in dataset.feature_names)
    if len(names) != X.shape[1] or len(set(names)) != len(names):
        raise ContractError("Selection feature names must match unique matrix columns")
    if dataset.manifest.get("target_version") != TARGET_VERSION:
        raise ContractError("Model selection requires the exact intraday target version")
    if any(b <= a for a, b in zip(references, references[1:])):
        raise ContractError("Selection references must be strictly chronological")
    if any(target != reference + timedelta(minutes=45) or available < target
           for reference, target, available in zip(references, targets, availability)):
        raise ContractError("Selection requires exact 45-minute target windows and truthful label availability")
    cutoff = instant(as_of if as_of is not None else dataset.manifest["training_cutoff"])
    if instant(dataset.manifest["training_cutoff"]) > cutoff:
        raise ContractError("Dataset was constructed after selection as_of")
    zone = ZoneInfo(session_timezone)
    sessions = tuple(str(value) for value in session_ids) if session_ids is not None else tuple(
        reference.astimezone(zone).date().isoformat() for reference in references)
    if len(sessions) != len(X) or any(not value for value in sessions):
        raise ContractError("Session identities must match every row")
    ordered_sessions = list(dict.fromkeys(sessions))
    if list(dict.fromkeys(sessions)) != [sessions[i] for i in range(len(sessions))
                                        if i == 0 or sessions[i] != sessions[i - 1]]:
        raise ContractError("Each session must occupy one chronological contiguous block")
    eligible = np.array([i for i, available in enumerate(availability) if available <= cutoff], dtype=int)
    if not len(eligible):
        raise ContractError("No selection labels are observable as of the research cutoff")
    # An unavailable tail must not silently redefine the latest session.
    if sessions[eligible[-1]] != ordered_sessions[-1]:
        raise ContractError("Most recent session has no observable labels at the selection cutoff")
    return X, y.astype(np.uint8), references, targets, availability, sessions, eligible, cutoff


def _partition(train, validation, *, X, y, references, targets, availability, sessions, config,
               holdout=False, timing_mode=LEGACY, research_cutoff=None):
    validate_timing_mode(timing_mode)
    market_boundary = references[validation[0]]
    if timing_mode == HISTORICAL_RESEARCH and research_cutoff is None:
        raise ContractError("Historical research requires its actual construction cutoff")
    fit_cutoff = instant(research_cutoff) if timing_mode == HISTORICAL_RESEARCH else market_boundary
    available_train = np.array([i for i in train if targets[i] <= market_boundary
                                and availability[i] <= fit_cutoff], dtype=int)
    capped = available_train[-config.max_training_rows:]
    reasons = []
    if len(capped) < config.min_training_rows:
        reasons.append("training_rows_below_minimum")
    if len({sessions[i] for i in capped}) < config.min_training_sessions:
        reasons.append("training_sessions_below_minimum")
    if len(validation) < (config.min_holdout_rows if holdout else config.min_validation_rows):
        reasons.append("holdout_rows_below_minimum" if holdout else "validation_rows_below_minimum")
    reasons.extend(_class_gate(y[capped], config.min_class_count, "training"))
    reasons.extend(_class_gate(y[validation], config.min_class_count, "holdout" if holdout else "validation"))
    evidence = {"session": sessions[validation[0]], "training_cutoff": fit_cutoff.isoformat(),
                "training_timing_contract": timing_contract(timing_mode),
                "market_partition_boundary": market_boundary.isoformat(),
                "training_rows": len(capped), "validation_rows": len(validation),
                "training_class_counts": {str(label): int((y[capped] == label).sum()) for label in (0, 1)},
                "validation_class_counts": {str(label): int((y[validation] == label).sum()) for label in (0, 1)},
                "overlap_or_unavailable_rows_purged": len(train) - len(available_train),
                "row_budget_excluded": len(available_train) - len(capped),
                "training_first_reference": references[capped[0]].isoformat() if len(capped) else None,
                "training_last_reference": references[capped[-1]].isoformat() if len(capped) else None,
                "training_latest_label_available_at": max(availability[i] for i in capped).isoformat() if len(capped) else None,
                "training_latest_target_close_at": max(targets[i] for i in capped).isoformat() if len(capped) else None,
                "validation_first_reference": references[validation[0]].isoformat(),
                "validation_last_reference": references[validation[-1]].isoformat(),
                "training_indices_sha256": _digest(capped.tolist()), "validation_indices_sha256": _digest(validation.tolist()),
                "gate_reasons": reasons}
    return capped, validation, evidence


def _fit_predict(spec, partition, data, config, deadline, *, feature_names):
    if time.monotonic() >= deadline:
        raise _SearchDeadlineReached("Bounded candidate search deadline reached before next fit")
    X, y, references, targets, availability, sessions, _, _ = data
    train, validation, evidence = partition
    timing_mode = timing_mode_from_metadata(evidence)
    if spec.training_max_rows is not None:
        train = train[-spec.training_max_rows:]
    if len(train) < config.min_training_rows or len({sessions[i] for i in train}) < config.min_training_sessions:
        raise InsufficientEvidenceError("Actual model history cap is below training row/session minimum")
    if _class_gate(y[train], config.min_class_count, "training"):
        raise InsufficientEvidenceError("Actual model history cap has insufficient examples of a target class")
    if getattr(spec, "probability_calibration", "none") == "sigmoid" or spec.family in {"ridge", "passive_aggressive"}:
        base, calibration = purged_chronological_split([references[i] for i in train], [targets[i] for i in train],
            fraction=spec.calibration_fraction, label_available_at=[availability[i] for i in train],
            timing_mode=timing_mode)
        if len(calibration) < config.min_calibration_rows:
            raise InsufficientEvidenceError("Chronological calibration rows below minimum")
        reasons = (_class_gate(y[train[base]], config.min_class_count, "calibration_base")
                   + _class_gate(y[train[calibration]], config.min_class_count, "calibration"))
        if reasons:
            raise InsufficientEvidenceError(",".join(reasons))
    started = time.monotonic()
    with threadpool_limits(limits=1), warnings.catch_warnings(record=True) as observed_warnings:
        warnings.simplefilter("always")
        estimator, fit_evidence = fit_calibrated_estimator(
            spec, X[train], y[train], reference_at=[references[i] for i in train],
            target_close_at=[targets[i] for i in train], label_available_at=[availability[i] for i in train],
            training_cutoff=evidence["training_cutoff"], feature_names=feature_names, timing_mode=timing_mode)
        matrix = np.asarray(estimator.predict_proba(X[validation]), dtype=float)
    classes = np.asarray(estimator.classes_)
    if (matrix.shape != (len(validation), 2) or set(classes.tolist()) != {0, 1}
            or not np.isfinite(matrix).all() or (matrix < 0).any() or (matrix > 1).any()
            or not np.allclose(matrix.sum(axis=1), 1, atol=1e-6)):
        raise ContractError("Candidate returned invalid binary probabilities")
    return matrix[:, int(np.flatnonzero(classes == 1)[0])], {
        "fit_seconds": time.monotonic() - started, "fit_evidence": fit_evidence,
        "warnings": list(dict.fromkeys(str(warning.message) for warning in observed_warnings))}


def _baseline_predictions(partitions, y):
    priors, neutral = [], []
    for train, validation, _ in partitions:
        # Fit the prevalence baseline solely on the fold's causal training rows.
        prevalence = (float(y[train].sum()) + 1) / (len(train) + 2)
        priors.append(np.full(len(validation), prevalence))
        neutral.append(np.full(len(validation), 0.5))
    return {"training_prevalence": np.concatenate(priors), "neutral_0_5": np.concatenate(neutral)}


def assess_frozen_ensemble(dataset, *, models, as_of=None, config=None, session_ids=None,
                           session_timezone="America/Los_Angeles", holdout_already_examined=True):
    """Assess every member of an existing recipe without selecting replacements.

    Unlike candidate selection, this path measures the exact installed manual
    ensemble even when one member would not make a supervised shortlist. Sparse
    validation partitions retain descriptive scores and their failed support
    gates. Training/calibration gates still prevent unsupported fits. A spent
    holdout can diagnose a repaired pipeline but cannot qualify its recipe; this
    result is never an installable selection report.
    """
    if type(holdout_already_examined) is not bool:
        raise ContractError("Holdout examination history must be explicit boolean")
    config = config if isinstance(config, SelectionConfig) else SelectionConfig(**(config or {}))
    specs = tuple(freeze_model_spec(spec) for spec in configured_models(models))
    if len(specs) > config.max_candidates or any(spec.probability_calibration != "sigmoid" for spec in specs):
        raise ContractError("Frozen assessment requires bounded explicitly calibrated members")
    # Do not silently impose a different history cap on the installed recipe.
    if any(spec.training_max_rows is None or spec.training_max_rows > config.max_training_rows for spec in specs):
        raise ContractError("Frozen assessment budget must cover each recipe's explicit history cap")
    data = _arrays(dataset, as_of, session_ids, session_timezone)
    X, y, references, targets, availability, sessions, eligible, cutoff = data
    timing_mode = timing_mode_from_metadata(dataset.manifest)
    ordered = list(dict.fromkeys(sessions[i] for i in eligible))
    recipe = dataset.manifest.get("manual_model_recipe", {})
    if recipe.get("models") is not None and [asdict(spec) for spec in specs] != [
            asdict(freeze_model_spec(spec)) for spec in configured_models(recipe["models"])]:
        raise ContractError("Exact manual assessment must use the dataset's frozen recipe members")
    binding = {"symbol": dataset.inference.get("symbol"), "recipe_sha256": recipe.get("sha256"),
        "manifest_sha256": _digest(dataset.manifest),
        "source_history_sha256": _digest(dataset.manifest.get("source_history", {})),
        "feature_contract_sha256": _digest({key: dataset.manifest.get(key) for key in
            ("feature_version", "feature_policy", "label_provenance", "native_interval_contract", "market_context")}),
        "feature_names_sha256": _digest(list(dataset.feature_names)), "feature_count": len(dataset.feature_names)}
    data_hash = hashlib.sha256(X.tobytes() + y.tobytes() + json.dumps({
        "binding": binding, "references": [value.isoformat() for value in references],
        "targets": [value.isoformat() for value in targets],
        "availability": [value.isoformat() for value in availability], "sessions": sessions}, sort_keys=True).encode()).hexdigest()
    report = {"assessment_version": "intraday-frozen-ensemble-assessment-v1",
        "training_timing_contract": timing_contract(timing_mode),
        "assessment_kind": "exact_frozen_manual_ensemble_no_selection", "status": "insufficient_data",
        "qualification": "not_established", "source_binding": binding, "dataset_sha256": data_hash,
        "as_of": cutoff.isoformat(), "target_version": TARGET_VERSION, "config": asdict(config),
        "models": [asdict(spec) for spec in specs], "partitions": [], "reasons": [],
        "holdout_session": ordered[-1], "holdout_already_examined": holdout_already_examined,
        "holdout_use": "diagnostic_only" if holdout_already_examined else "independent_fixed_recipe_gate",
        "selected_specs": [], "policy_installation_authorized": False,
        "availability_kinds": dataset.manifest.get("availability_kinds", []),
        "historical_availability_limitation": ("Historical close availability is assumed where explicitly declared; no receipt was backdated."
            if any(kind != "actual" for kind in dataset.manifest.get("availability_kinds", [])) else None)}
    report["assessment_id"] = _digest({"data": data_hash, "models": report["models"],
        "config": asdict(config), "holdout_already_examined": holdout_already_examined})
    if len(ordered) < config.min_training_sessions + config.cv_folds + 1:
        report["reasons"] = ["insufficient_chronological_sessions"]
        return report
    deadline = time.monotonic() + config.max_search_seconds
    metric = lambda labels, p: probability_metrics(labels, p, extreme_probability=config.extreme_probability)
    cv_parts, cv_probabilities, cv_labels = [], [], []
    cv_member_probabilities = {spec.name: [] for spec in specs}
    for session in ordered[-config.cv_folds - 1:]:
        validation = np.asarray([i for i in eligible if sessions[i] == session], dtype=int)
        train = np.asarray([i for i in eligible if references[i] < references[validation[0]]], dtype=int)
        held = session == ordered[-1]
        partition = _partition(train, validation, X=X, y=y, references=references, targets=targets,
            availability=availability, sessions=sessions, config=config, holdout=held,
            timing_mode=timing_mode, research_cutoff=cutoff)
        if (recipe and dataset.manifest.get("input_training_rows", len(X)) > len(X)
                and len(partition[0]) < max(spec.training_max_rows for spec in specs)):
            raise ContractError("Exact manual assessment requires enough uncapped history for every fold's recipe window")
        entry = {**partition[2], "role": "holdout" if held else "development", "models": [],
            "ensemble_metrics": None, "metric_reasons": [], "complete": False,
            "scores_are_descriptive_only": held and holdout_already_examined or bool(partition[2]["gate_reasons"])}
        entry["baselines"] = {name: metric(y[validation], p) for name, p in _baseline_predictions([partition], y).items()}
        train_reasons = [reason for reason in entry["gate_reasons"] if reason.startswith("training")]
        pieces = []
        for spec in specs:
            member = {"name": spec.name, "status": "not_fit", "metrics": None, "reasons": list(train_reasons)}
            if not train_reasons:
                try:
                    p, fitted = _fit_predict(spec, partition, data, config, deadline, feature_names=dataset.feature_names)
                    member.update(status="fitted", metrics=metric(y[validation], p), **fitted)
                    member["reasons"] = _metric_gates(member["metrics"], entry["baselines"], config)
                    pieces.append(p)
                    if not held:
                        cv_member_probabilities[spec.name].append(p)
                except Exception as exc:
                    member.update(status="insufficient_data" if isinstance(exc, InsufficientEvidenceError) else "failed",
                        error={"type": type(exc).__name__, "message": str(exc)})
            entry["models"].append(member)
        entry["complete"] = len(pieces) == len(specs)
        if entry["complete"]:
            probability = np.mean(pieces, axis=0)
            entry["ensemble_metrics"] = metric(y[validation], probability)
            entry["metric_reasons"] = _metric_gates(entry["ensemble_metrics"], entry["baselines"], config)
            if not held:
                cv_parts.append(partition)
                cv_probabilities.append(probability)
                cv_labels.append(y[validation])
        report["partitions"].append(entry)
    reasons = [f"{part['session']}:{reason}" for part in report["partitions"] for reason in part["gate_reasons"]]
    if not all(part["complete"] for part in report["partitions"]):
        reasons.append("incomplete_frozen_ensemble")
        reasons.extend(f"{part['session']}:{member['name']}:{member['error']['type']}:{member['error']['message']}"
            for part in report["partitions"] for member in part["models"] if "error" in member)
    if len(cv_parts) == config.cv_folds:
        labels = np.concatenate(cv_labels)
        report["development_metrics"] = metric(labels, np.concatenate(cv_probabilities))
        report["development_baselines"] = {name: metric(labels, p) for name, p in _baseline_predictions(cv_parts, y).items()}
        reasons.extend("development:" + reason for reason in _metric_gates(
            report["development_metrics"], report["development_baselines"], config))
        wins = sum(all(part["ensemble_metrics"]["brier"] < base["brier"] for base in part["baselines"].values())
            for part in report["partitions"] if part["role"] == "development")
        report["development_winning_folds"] = wins
        if wins < math.ceil(config.min_winning_fold_fraction * config.cv_folds):
            reasons.append("development:too_few_folds_improve_baseline")
        report["development_members"] = []
        for spec in specs:
            member_metrics = metric(labels, np.concatenate(cv_member_probabilities[spec.name]))
            member_reasons = _metric_gates(member_metrics, report["development_baselines"], config)
            member_wins = sum(all(next(member for member in part["models"] if member["name"] == spec.name)
                ["metrics"]["brier"] < base["brier"] for base in part["baselines"].values())
                for part in report["partitions"] if part["role"] == "development")
            if member_wins < math.ceil(config.min_winning_fold_fraction * config.cv_folds):
                member_reasons.append("too_few_folds_improve_baseline")
            report["development_members"].append({"name": spec.name, "metrics": member_metrics,
                "winning_folds": member_wins, "reasons": member_reasons})
            reasons.extend("development:" + spec.name + ":" + reason for reason in member_reasons)
    held = report["partitions"][-1]
    reasons.extend("holdout:" + reason for reason in held["metric_reasons"])
    for member in held["models"]:
        reasons.extend("holdout:" + member["name"] + ":" + reason for reason in member["reasons"])
    report["reasons"] = sorted(set(reasons))
    report["status"] = ("insufficient_data" if any(part["gate_reasons"] or any(
        member["status"] == "insufficient_data" for member in part["models"]) for part in report["partitions"])
        else "failed" if not all(part["complete"] for part in report["partitions"]) else "assessed")
    report["qualification"] = ("not_qualified" if reasons else
        "requires_new_independent_holdout" if holdout_already_examined else "fixed_recipe_gates_passed")
    return report


def evaluate_candidates(dataset, *, as_of=None, candidates=None, config=None, session_ids=None,
                        session_timezone="America/Los_Angeles", incumbent_specs=None) -> dict:
    """Research once, then return a reviewable calibrated configuration proposal.

    ``dataset`` is the existing causal ``data.Dataset``. No model/provider/broker
    connections or files are opened by this function. Expanding folds precede
    the latest session; all training windows are purged by exact label close AND
    joint target/feature information availability at each fitting cutoff. The
    dataset builder conservatively stores that joint receipt in its existing
    ``label_available_at`` field. The bounded row cap is explicit.
    A failed or inconclusive gate returns no selected specs; callers retain the
    incumbent configuration. Fit/runtime installation remains the caller's job.
    """
    config = config if isinstance(config, SelectionConfig) else SelectionConfig(**(config or {}))
    specs = configured_models(candidates if candidates is not None else default_candidates())
    # The selected config must keep the same history policy when the live engine
    # later refits it. A validation-only cap would evaluate a different procedure.
    specs = tuple(replace(spec, training_max_rows=min(spec.training_max_rows or config.max_training_rows,
                                                     config.max_training_rows)) for spec in specs)
    if len(specs) > config.max_candidates:
        raise ContractError("Candidate count exceeds the explicit research budget")
    if any(getattr(spec, "probability_calibration", "none") != "sigmoid" for spec in specs):
        raise ContractError("Selection candidates must explicitly use chronological sigmoid calibration")
    # Resolve before the manifest, any validation fit, or the final holdout.
    # An unavailable optional adapter remains an explicit candidate failure.
    resolution_errors = {}
    resolved = []
    for spec in specs:
        try:
            resolved.append(freeze_model_spec(spec))
        except Exception as exc:
            resolution_errors[spec.name] = exc
            resolved.append(spec)
    specs = tuple(resolved)
    catalog, coverage = _catalog_report(specs)
    catalog_status = {item["family"]: item["status"] for item in catalog}
    incumbents = configured_models(incumbent_specs) if incumbent_specs else ()
    if len(incumbents) > 24:
        raise ContractError("Incumbent comparison exceeds the 24-model budget")
    incumbent_error = None
    try:
        incumbents = tuple(freeze_model_spec(spec) for spec in incumbents)
    except Exception as exc:
        incumbent_error = exc
    data = _arrays(dataset, as_of, session_ids, session_timezone)
    X, y, references, targets, availability, sessions, eligible, cutoff = data
    timing_mode = timing_mode_from_metadata(dataset.manifest)
    ordered_sessions = list(dict.fromkeys(sessions[i] for i in eligible))
    holdout_session = ordered_sessions[-1]
    data_hash = hashlib.sha256(X.tobytes() + y.tobytes() + json.dumps({
        "references": [value.isoformat() for value in references],
        "availability": [value.isoformat() for value in availability], "sessions": sessions,
        "feature_names": list(dataset.feature_names), "feature_version": dataset.manifest.get("feature_version"),
        "target_version": TARGET_VERSION, "synthetic_policy": dataset.manifest.get("synthetic_policy"),
        "training_timing_contract": timing_contract(timing_mode)}, sort_keys=True).encode()).hexdigest()
    spec_dicts = [asdict(spec) for spec in specs]
    report = {"selection_version": SELECTION_VERSION, "target_version": TARGET_VERSION,
              "training_timing_contract": timing_contract(timing_mode),
              "status": "insufficient_data", "as_of": cutoff.isoformat(), "config": asdict(config),
              "dataset_sha256": data_hash, "candidate_manifest_sha256": _digest(spec_dicts),
              "holdout_session": holdout_session, "holdout_policy": "one_final_gate_after_frozen_cv_shortlist_no_replacements",
              "holdout_reuse_policy": "persist_and_reuse_this_report_for_session_do_not_retune_on_same_holdout",
              "session_timezone": session_timezone, "session_identity_source": "explicit" if session_ids is not None else "local_reference_date",
              "row_count": len(X), "observable_rows": len(eligible), "unavailable_rows_excluded": len(X) - len(eligible),
              "feature_names": list(dataset.feature_names), "availability_kinds": dataset.manifest.get("availability_kinds", []),
              "information_availability_policy": dataset.manifest.get("label_availability_policy", "supplied_dataset_receipt_gate"),
              "historical_availability_limitation": ("Historical receipt timing includes explicit assumptions; results are conditional on them."
                  if any(kind != "actual" for kind in dataset.manifest.get("availability_kinds", [])) else None),
              "synthetic_policy": dataset.manifest.get("synthetic_policy"), "candidates": [], "folds": [],
              "model_catalog": catalog, "candidate_coverage": coverage,
              "frozen_shortlist": [], "selected_specs": [], "holdout": None, "incumbent": None,
              "reasons": [], "limitation": "Probability validation does not establish trading profitability or future performance."}
    report["selection_id"] = _digest({"data": data_hash, "candidates": spec_dicts,
                                      "config": asdict(config), "incumbent": [asdict(spec) for spec in incumbents]})
    required_sessions = config.min_training_sessions + config.cv_folds + 1
    if len(ordered_sessions) < required_sessions:
        report["reasons"] = [f"need_at_least_{required_sessions}_sessions"]
        return _finish_report(report, specs)
    folds = []
    incumbent_folds = []
    incumbent_config = replace(config, max_training_rows=max(len(X), config.min_training_rows))
    for session in ordered_sessions[-config.cv_folds - 1:-1]:
        validation = np.array([i for i in eligible if sessions[i] == session], dtype=int)
        train = np.array([i for i in eligible if references[i] < references[validation[0]]], dtype=int)
        folds.append(_partition(train, validation, X=X, y=y, references=references, targets=targets,
                                availability=availability, sessions=sessions, config=config,
                                timing_mode=timing_mode, research_cutoff=cutoff))
        if incumbents:
            incumbent_folds.append(_partition(train, validation, X=X, y=y, references=references, targets=targets,
                                              availability=availability, sessions=sessions, config=incumbent_config,
                                              timing_mode=timing_mode, research_cutoff=cutoff))
    holdout_rows = np.array([i for i in eligible if sessions[i] == holdout_session], dtype=int)
    pre_holdout = np.array([i for i in eligible if references[i] < references[holdout_rows[0]]], dtype=int)
    holdout_partition = _partition(pre_holdout, holdout_rows, X=X, y=y, references=references, targets=targets,
                                   availability=availability, sessions=sessions, config=config, holdout=True,
                                   timing_mode=timing_mode, research_cutoff=cutoff)
    incumbent_holdout_partition = _partition(pre_holdout, holdout_rows, X=X, y=y, references=references, targets=targets,
                                             availability=availability, sessions=sessions, config=incumbent_config, holdout=True,
                                             timing_mode=timing_mode, research_cutoff=cutoff)
    report["folds"] = [partition[2] for partition in folds]
    report["holdout_partition"] = holdout_partition[2]
    partition_reasons = [reason for partition in [*folds, holdout_partition] for reason in partition[2]["gate_reasons"]]
    if partition_reasons:
        report["reasons"] = sorted(set(partition_reasons))
        return _finish_report(report, specs)
    deadline = time.monotonic() + config.max_search_seconds
    cv_y = np.concatenate([y[validation] for _, validation, _ in folds])
    baseline_p = _baseline_predictions(folds, y)
    metric = lambda labels, probabilities: probability_metrics(labels, probabilities, extreme_probability=config.extreme_probability)
    baselines = {name: metric(cv_y, probabilities) for name, probabilities in baseline_p.items()}
    report["cv_baselines"] = baselines
    predictions = {}
    for spec in specs:
        row = {"spec": asdict(spec), "status": "failed", "folds": [], "metrics": None, "reasons": [],
               "selected": False, "shortlisted": False, "selection_reason": "fit_failed",
               "catalog_status": catalog_status[spec.family]}
        pieces = []
        try:
            if spec.name in resolution_errors:
                raise resolution_errors[spec.name]
            for fold in folds:
                p, fitted = _fit_predict(spec, fold, data, config, deadline, feature_names=dataset.feature_names)
                fold_metrics = metric(y[fold[1]], p)
                fold_baseline = {name: metric(y[fold[1]], value) for name, value in _baseline_predictions([fold], y).items()}
                row["folds"].append({"session": fold[2]["session"], "metrics": fold_metrics,
                                     "baselines": fold_baseline, **fitted})
                pieces.append(p)
            p = np.concatenate(pieces)
            row["metrics"] = metric(cv_y, p)
            row["reasons"] = _metric_gates(row["metrics"], baselines, config)
            wins = sum(all(fold["metrics"]["brier"] < baseline["brier"] for baseline in fold["baselines"].values())
                       for fold in row["folds"])
            row["winning_folds"] = wins
            if wins < math.ceil(config.min_winning_fold_fraction * len(folds)):
                row["reasons"].append("too_few_folds_improve_baseline")
            row["status"] = "eligible_cv" if not row["reasons"] else "rejected_cv"
            row["selection_reason"] = "qualified" if not row["reasons"] else "validation_rejected"
            predictions[spec.name] = p
        except Exception as exc:
            if isinstance(exc, InsufficientEvidenceError):
                row["status"] = "insufficient_data"
                row["selection_reason"] = "insufficient_data"
            elif isinstance(exc, TimeoutError):
                row["selection_reason"] = "search_budget_exhausted"
                if isinstance(exc, _SearchDeadlineReached) and not row["folds"]:
                    row["status"] = "not_run"
            elif catalog_status[spec.family] in {"dependency_unavailable", "adapter_not_implemented"}:
                row["selection_reason"] = catalog_status[spec.family]
            row["error"] = {"type": type(exc).__name__, "message": str(exc)}
        report["candidates"].append(row)
        if time.monotonic() >= deadline:
            report["status"] = "budget_exhausted"
            report["reasons"] = ["search_deadline_reached_no_promotion"]
            return _finish_report(report, specs)
    eligible_results = sorted((row for row in report["candidates"] if row["status"] == "eligible_cv"),
                              key=lambda row: (row["metrics"]["brier"], row["metrics"]["log_loss"], row["spec"]["name"]))
    shortlist, families = [], set()
    for row in eligible_results:
        if row["spec"]["family"] in families:
            row["selection_reason"] = "duplicate_family"
            continue
        if len(shortlist) >= config.max_selected:
            row["selection_reason"] = "configured_cap"
            continue
        shortlist.append(next(spec for spec in specs if spec.name == row["spec"]["name"]))
        families.add(row["spec"]["family"])
        row["shortlisted"] = True
        row["selection_reason"] = "frozen_cv_shortlist"
    report["frozen_shortlist"] = [asdict(spec) for spec in shortlist]
    report["frozen_shortlist_sha256"] = _digest(report["frozen_shortlist"])
    report["status"] = "retained"
    if not shortlist:
        if all(row["status"] == "insufficient_data" for row in report["candidates"]):
            report["status"] = "insufficient_data"
            report["reasons"] = ["all_candidates_have_insufficient_calibration_or_training_samples"]
        elif all(row["status"] in {"failed", "insufficient_data"} for row in report["candidates"]):
            report["status"] = "failed"
            report["reasons"] = ["all_candidate_fits_failed"]
        else:
            report["reasons"] = ["no_candidate_passed_development_gates"]
        return _finish_report(report, specs)
    proposal_cv = metric(cv_y, np.mean([predictions[spec.name] for spec in shortlist], axis=0))
    report["proposal_cv"] = proposal_cv
    proposal_reasons = _metric_gates(proposal_cv, baselines, config)
    incumbent_cv = None
    try:
        if incumbent_error is not None:
            raise incumbent_error
        if incumbents:
            incumbent_predictions = [np.concatenate([_fit_predict(spec, fold, data, config, deadline, feature_names=dataset.feature_names)[0] for fold in incumbent_folds])
                                     for spec in incumbents]
            incumbent_cv = metric(cv_y, np.mean(incumbent_predictions, axis=0))
            report["incumbent"] = {"specs": [asdict(spec) for spec in incumbents], "cv_metrics": incumbent_cv,
                                   "history_policy": "actual_incumbent_spec_cap_or_all_causal_prior_rows",
                                   "folds": [fold[2] for fold in incumbent_folds]}
            for score in ("brier", "log_loss"):
                if proposal_cv[score] > incumbent_cv[score] + config.max_incumbent_degradation:
                    proposal_reasons.append(f"cv_{score}_worse_than_incumbent")
        if proposal_reasons:
            report["reasons"] = proposal_reasons
            return _finish_report(report, specs)
        # No holdout labels/scores have influenced fitting, ranking or this frozen proposal.
        held_y = y[holdout_rows]
        held_baselines = {name: metric(held_y, probabilities)
                          for name, probabilities in _baseline_predictions([holdout_partition], y).items()}
        held_predictions, held_models, held_reasons = [], [], []
        for spec in shortlist:
            p, evidence = _fit_predict(spec, holdout_partition, data, config, deadline, feature_names=dataset.feature_names)
            scores = metric(held_y, p)
            reasons = _metric_gates(scores, held_baselines, config)
            held_predictions.append(p)
            held_models.append({"name": spec.name, "metrics": scores, "reasons": reasons, **evidence})
            held_reasons.extend(f"{spec.name}:{reason}" for reason in reasons)
        proposal_holdout = metric(held_y, np.mean(held_predictions, axis=0))
        held_reasons.extend(_metric_gates(proposal_holdout, held_baselines, config))
        report["holdout"] = {"session": holdout_session, "baselines": held_baselines,
                             "models": held_models, "proposal_metrics": proposal_holdout}
        if incumbents:
            incumbent_p = [_fit_predict(spec, incumbent_holdout_partition, data, config, deadline, feature_names=dataset.feature_names)[0] for spec in incumbents]
            incumbent_holdout = metric(held_y, np.mean(incumbent_p, axis=0))
            report["incumbent"]["holdout_metrics"] = incumbent_holdout
            for score in ("brier", "log_loss"):
                if proposal_holdout[score] > incumbent_holdout[score] + config.max_incumbent_degradation:
                    held_reasons.append(f"holdout_{score}_worse_than_incumbent")
        if time.monotonic() >= deadline:
            held_reasons.append("search_deadline_reached_no_promotion")
            report["status"] = "budget_exhausted"
        report["reasons"] = held_reasons
        if not held_reasons:
            report["status"] = "promoted"
            report["selected_specs"] = report["frozen_shortlist"]
    except Exception as exc:
        report["status"] = ("budget_exhausted" if isinstance(exc, TimeoutError)
                            else "insufficient_data" if isinstance(exc, InsufficientEvidenceError) else "failed")
        report["reasons"] = ["proposal_or_incumbent_fit_failed_no_promotion"]
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
    return _finish_report(report, specs)
