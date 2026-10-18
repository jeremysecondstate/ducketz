"""Pure, bounded offline comparison of a fixed observed-candle model proposal.

Five consecutive dates with observed labels form each evaluation block. Three
development blocks precede a final held-out block. Neither scores nor holdout
labels select models: the same two sigmoid-calibrated models are fixed before
the first fit. This module writes no files and never installs a configuration.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib
import json
import math
import time
import warnings
from zoneinfo import ZoneInfo

import numpy as np
from threadpoolctl import threadpool_limits

from ml.intraday.model_selection import probability_metrics
from ml.intraday.models import (
    ModelSpec, configured_models, fit_calibrated_estimator,
    purged_chronological_split,
)
from ml.intraday.observed_data import FEATURE_POLICY, FEATURE_VERSION, LABEL_PROVENANCE
from ml.intraday.prediction import ContractError, TARGET_VERSION, instant

EXPERIMENT_VERSION = "intraday-fixed-observed-block-research-v1"
BLOCK_DATES = 5
DEVELOPMENT_BLOCKS = 3
PROPOSAL = "fixed_calibrated_mean"
INCUMBENT = "incumbent_uncalibrated_mean"


@dataclass(frozen=True)
class ObservedResearchConfig:
    min_training_rows: int = 160
    min_training_dates: int = 5
    min_validation_rows: int = 24
    min_holdout_rows: int = 24
    min_class_count: int = 5
    min_calibration_rows: int = 32
    max_training_rows: int = 4096
    max_seconds: float = 120.0
    session_timezone: str = "America/Los_Angeles"

    def __post_init__(self):
        for name in ("min_training_rows", "min_training_dates", "min_validation_rows",
                     "min_holdout_rows", "min_class_count", "min_calibration_rows", "max_training_rows"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ContractError(f"{name} must be a positive integer")
        if self.max_training_rows < self.min_training_rows or self.max_training_rows > 4096:
            raise ContractError("Training row budget must cover the training minimum")
        if (isinstance(self.max_seconds, bool) or not isinstance(self.max_seconds, (int, float))
                or not math.isfinite(self.max_seconds) or not 0 < self.max_seconds <= 120):
            raise ContractError("Offline experiment budget must be at most 120 seconds")
        try:
            ZoneInfo(self.session_timezone)
        except (ValueError, TypeError, KeyError) as exc:
            raise ContractError("Research requires a valid session timezone") from exc


def fixed_specs() -> tuple[ModelSpec, ...]:
    """The fixed proposal; no caller or observed score can change its members."""
    return (
        ModelSpec("logistic_strong", family="logistic", parameters={"C": 0.05},
                  calibration_fraction=0.25, probability_calibration="sigmoid"),
        ModelSpec("forest_shallow", family="random_forest",
                  parameters={"n_estimators": 64, "max_depth": 3, "min_samples_leaf": 16},
                  calibration_fraction=0.25, probability_calibration="sigmoid"),
    )


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _data(dataset, config, *, candidate):
    try:
        X, y = np.asarray(dataset.X, dtype=float), np.asarray(dataset.y)
        inference = np.asarray(dataset.inference_X, dtype=float)
        names = tuple(dataset.feature_names)
        references = tuple(instant(value) for value in dataset.reference_at)
        targets = tuple(instant(value) for value in dataset.target_close_at)
        available = tuple(instant(value) for value in dataset.label_available_at)
        manifest = dict(dataset.manifest)
        cutoff = instant(manifest["training_cutoff"])
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        raise ContractError("Research requires a complete numeric Dataset with timezone-aware timestamps") from exc
    if (X.ndim != 2 or not X.shape[1] or y.shape != (len(X),)
            or inference.shape != (1, X.shape[1]) or not np.isin(y, [0, 1]).all()
            or np.isinf(X).any() or np.isinf(inference).any()
            or not len(references) == len(targets) == len(available) == len(X)):
        raise ContractError("Research features, binary labels, inference and timestamps must match")
    if (len(names) != X.shape[1] or any(not isinstance(name, str) or not name for name in names)
            or len(set(names)) != len(names)):
        raise ContractError("Research feature names must match unique matrix columns")
    if manifest.get("target_version") != TARGET_VERSION:
        raise ContractError("Research requires the exact 45-minute not-down target")
    if any(b <= a for a, b in zip(references, references[1:])):
        raise ContractError("Research reference timestamps must be strictly chronological")
    if any(target != reference + timedelta(minutes=45) or receipt < target
           for reference, target, receipt in zip(references, targets, available)):
        raise ContractError("Research targets and label receipts must match exact causal 45-minute windows")
    if candidate and ("body_return" not in names or not np.isfinite(X[:, names.index("body_return")]).all()):
        raise ContractError("Observed research requires a finite completed reference body_return feature")
    if candidate and (manifest.get("feature_version") != FEATURE_VERSION
                      or manifest.get("feature_policy") != FEATURE_POLICY
                      or manifest.get("label_provenance") != LABEL_PROVENANCE
                      or manifest.get("synthetic_policy") != "exclude"
                      or manifest.get("synthetic_labels_included") != 0):
        raise ContractError("Candidate must carry the observed-only feature and label provenance contract")
    if candidate and "synthetic_input_fraction20" in names:
        index = names.index("synthetic_input_fraction20")
        if not np.equal(X[:, index], 0).all() or not np.equal(inference[:, index], 0).all():
            raise ContractError("Observed-only candidate cannot contain synthetic feature history")
    identity = {name: dataset.inference.get(name) for name in ("symbol", "source", "source_dataset")}
    if any(not isinstance(value, str) or not value.strip() for value in identity.values()):
        raise ContractError("Research Dataset requires explicit symbol, source and source_dataset identity")
    dates = tuple(reference.astimezone(ZoneInfo(config.session_timezone)).date().isoformat()
                  for reference in references)
    return dict(X=X, y=y.astype(np.uint8), references=references, targets=targets,
                available=available, names=names, cutoff=cutoff, dates=dates, manifest=manifest, identity=identity)


def _class_reasons(y, minimum, name):
    return [f"{name}_class_{label}_below_{minimum}" for label in (0, 1)
            if int((y == label).sum()) < minimum]


def _bounds(data, rows):
    dates = list(dict.fromkeys(data["dates"][i] for i in rows))
    return {
        "rows": len(rows), "actual_date_count": len({data["dates"][i] for i in rows}),
        "rows_by_actual_date": {date: sum(data["dates"][i] == date for i in rows) for date in dates},
        "first_reference": data["references"][rows[0]].isoformat() if len(rows) else None,
        "last_reference": data["references"][rows[-1]].isoformat() if len(rows) else None,
        "latest_target_close": max(data["targets"][i] for i in rows).isoformat() if len(rows) else None,
        "latest_label_available": max(data["available"][i] for i in rows).isoformat() if len(rows) else None,
        "class_counts": {str(label): int((data["y"][rows] == label).sum()) for label in (0, 1)},
        "indices_sha256": _digest(rows.tolist()),
    }


def _partition(data, evaluation, cutoff, config, *, incumbent=False, holdout=False):
    prior = np.array([i for i, reference in enumerate(data["references"]) if reference < cutoff], dtype=int)
    overlap = np.array([i for i in prior if data["targets"][i] > cutoff], dtype=int)
    unavailable = np.array([i for i in prior if data["available"][i] > cutoff], dtype=int)
    allowed = np.array([i for i in prior if data["targets"][i] <= cutoff
                        and data["available"][i] <= cutoff], dtype=int)
    train = allowed if incumbent else allowed[-config.max_training_rows:]
    reasons = []
    if len(train) < config.min_training_rows:
        reasons.append("training_rows_below_minimum")
    if len({data["dates"][i] for i in train}) < config.min_training_dates:
        reasons.append("actual_training_dates_below_minimum")
    minimum = config.min_holdout_rows if holdout else config.min_validation_rows
    if len(evaluation) < minimum:
        reasons.append("holdout_rows_below_minimum" if holdout else "validation_rows_below_minimum")
    reasons.extend(_class_reasons(data["y"][train], config.min_class_count, "training"))
    reasons.extend(_class_reasons(data["y"][evaluation], config.min_class_count, "evaluation"))
    evidence = {"training_cutoff": cutoff.isoformat(), "training": _bounds(data, train),
                "evaluation": _bounds(data, evaluation), "prior_rows": len(prior),
                "target_overlap_rows": len(overlap), "unavailable_prior_rows": len(unavailable),
                "overlap_or_unavailable_rows_purged": len(prior) - len(allowed),
                "row_budget_excluded": len(allowed) - len(train), "gate_reasons": reasons,
                "history_policy": "all_original_causal_rows" if incumbent else "newest_causal_observed_rows",
                "proof": {"every_training_reference_before_cutoff": all(data["references"][i] < cutoff for i in train),
                          "every_training_target_closed_by_cutoff": all(data["targets"][i] <= cutoff for i in train),
                          "every_training_label_available_by_cutoff": all(data["available"][i] <= cutoff for i in train)}}
    return train, evidence


def _calibration(data, train, spec, config):
    evidence = {"method": "purged_chronological_sigmoid", "fraction": spec.calibration_fraction,
                "base": None, "calibration": None, "gate_reasons": []}
    try:
        base, calibration = purged_chronological_split(
            [data["references"][i] for i in train], [data["targets"][i] for i in train],
            fraction=spec.calibration_fraction, label_available_at=[data["available"][i] for i in train])
    except ContractError as exc:
        evidence["gate_reasons"] = ["calibration_split_unavailable"]
        evidence["error"] = str(exc)
        return evidence
    base, calibration = train[base], train[calibration]
    first = data["references"][calibration[0]]
    evidence.update(base=_bounds(data, base), calibration=_bounds(data, calibration),
                    first_calibration_reference=first.isoformat(),
                    purged_rows=len(train) - len(base) - len(calibration),
                    proof={"base_targets_closed_by_calibration": all(data["targets"][i] <= first for i in base),
                           "base_labels_available_by_calibration": all(data["available"][i] <= first for i in base)})
    if len(calibration) < config.min_calibration_rows:
        evidence["gate_reasons"].append("calibration_rows_below_minimum")
    evidence["gate_reasons"].extend(_class_reasons(data["y"][base], config.min_class_count, "calibration_base"))
    evidence["gate_reasons"].extend(_class_reasons(data["y"][calibration], config.min_class_count, "calibration"))
    return evidence


def _fit(data, train, evaluation, cutoff, spec, deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("Offline experiment deadline reached before next fit")
    started = time.monotonic()
    with threadpool_limits(limits=1), warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        estimator, evidence = fit_calibrated_estimator(
            spec, data["X"][train], data["y"][train],
            reference_at=[data["references"][i] for i in train],
            target_close_at=[data["targets"][i] for i in train],
            label_available_at=[data["available"][i] for i in train], training_cutoff=cutoff)
        matrix = np.asarray(estimator.predict_proba(data["X"][evaluation]), dtype=float)
    classes = np.asarray(estimator.classes_)
    if (matrix.shape != (len(evaluation), 2) or classes.shape != (2,) or set(classes.tolist()) != {0, 1}
            or not np.isfinite(matrix).all() or (matrix < 0).any() or (matrix > 1).any()
            or not np.allclose(matrix.sum(axis=1), 1, atol=1e-6)):
        raise ContractError("Research model emitted invalid binary probabilities")
    return matrix[:, int(np.flatnonzero(classes == 1)[0])], {
        "fit_seconds": time.monotonic() - started, "fitting": evidence,
        "warnings": list(dict.fromkeys(str(item.message) for item in seen))}


def _result(y, p=None, *, status="unavailable", reasons=None, **extra):
    return {"status": "scored" if p is not None else status,
            "metrics": probability_metrics(y, p) if p is not None else None,
            "reasons": list(reasons or []), **extra}


def _error_result(y, error):
    return _result(y, status="budget_exhausted" if isinstance(error, TimeoutError) else "failed",
                   error={"type": type(error).__name__, "message": str(error)})


def _baselines(data, train, evaluation):
    labels = data["y"][train]
    prior = (float(labels.sum()) + 1) / (len(train) + 2)
    body = data["X"][:, data["names"].index("body_return")] >= 0
    conditional, counts = {}, {}
    for direction in (False, True):
        group = labels[body[train] == direction]
        # A bin with no history is explicitly the Laplace-smoothed 1/2 prior.
        conditional[direction] = (float(group.sum()) + 1) / (len(group) + 2)
        counts[str(direction).lower()] = {"rows": len(group), "not_down": int(group.sum()),
                                        "probability_not_down": conditional[direction]}
    predictions = {"neutral_0_5": np.full(len(evaluation), 0.5),
                   "training_prevalence": np.full(len(evaluation), prior),
                   "reference_direction": np.array([conditional[bool(value)] for value in body[evaluation]])}
    evidence = {"training_rows": len(train), "laplace_alpha": 1,
                "conditional_training_counts": counts,
                "hard_direction_accuracy": float((body[evaluation] == data["y"][evaluation]).mean())}
    return predictions, evidence


def evaluate_observed_experiment(dataset, *, incumbent_dataset=None, config=None) -> dict:
    """Return scores and predictions without changing data, runtime or policy.

    Candidate and incumbent score exactly the same observable reference/label
    keys. The incumbent refits its ten original uncalibrated members on its own
    original causal feature/label population, including synthetic training labels.
    Missing members never produce a survivor-only ensemble. All dates and cutoff
    proofs are explicit; synthetic historical receipt assumptions remain limits.
    """
    config = config if isinstance(config, ObservedResearchConfig) else ObservedResearchConfig(**(config or {}))
    started = time.monotonic()
    deadline = started + config.max_seconds
    specs = fixed_specs()  # Frozen before parsing scores or fitting any model.
    incumbent_specs = configured_models() if incumbent_dataset is not None else ()
    data = _data(dataset, config, candidate=True)
    incumbent = _data(incumbent_dataset, config, candidate=False) if incumbent_dataset is not None else None
    if incumbent and data["identity"] != incumbent["identity"]:
        raise ContractError("Candidate and incumbent symbol/source identities must match")
    cutoff = min(data["cutoff"], incumbent["cutoff"]) if incumbent else data["cutoff"]
    incumbent_lookup = {reference: i for i, reference in enumerate(incumbent["references"])} if incumbent else {}
    # Validate shared target identity even if a malformed row would later be purged.
    if incumbent:
        for i, reference in enumerate(data["references"]):
            j = incumbent_lookup.get(reference)
            if j is not None and (data["targets"][i] != incumbent["targets"][j] or data["y"][i] != incumbent["y"][j]):
                raise ContractError("Candidate and incumbent shared reference targets/labels disagree")
    eligible = [i for i, receipt in enumerate(data["available"]) if receipt <= cutoff]
    dates = list(dict.fromkeys(data["dates"][i] for i in eligible))
    common = [i for i in eligible if not incumbent or (
        data["references"][i] in incumbent_lookup
        and incumbent["available"][incumbent_lookup[data["references"][i]]] <= cutoff)]
    candidate_references = set(data["references"])
    names = [spec.name for spec in specs] + [PROPOSAL, "neutral_0_5", "training_prevalence", "reference_direction"]
    if incumbent:
        names.append(INCUMBENT)
    manifests = [data["manifest"]] + ([incumbent["manifest"]] if incumbent else [])
    kinds = sorted({kind for manifest in manifests for kind in manifest.get("availability_kinds", [])})
    report = {
        "experiment_version": EXPERIMENT_VERSION, "target_version": TARGET_VERSION,
        "status": "insufficient_data", "as_of": cutoff.isoformat(), "config": asdict(config),
        "dataset_identity": data["identity"],
        "frozen_specs": [asdict(spec) for spec in specs],
        "frozen_specs_sha256": _digest([asdict(spec) for spec in specs]),
        "incumbent_specs": [asdict(spec) for spec in incumbent_specs],
        "holdout_policy": "fixed_specs_before_all_scores_no_tuning_or_promotion",
        "block_policy": {"observed_dates_per_block": BLOCK_DATES, "development_blocks": DEVELOPMENT_BLOCKS,
                         "final_holdout_blocks": 1, "alignment": "latest_observed_label_date_right_aligned"},
        "row_count": len(data["X"]), "observable_rows": len(eligible),
        "unavailable_rows_excluded": len(data["X"]) - len(eligible),
        "observed_label_dates": len(dates), "common_evaluation_rows": len(common),
        "comparison_rows_excluded": len(eligible) - len(common),
        "comparison_coverage": {
            "candidate_observable_by_date": {date: sum(data["dates"][i] == date for i in eligible) for date in dates},
            "common_observable_by_date": {date: sum(data["dates"][i] == date for i in common) for date in dates},
            "candidate_rows_missing_incumbent_reference": sum(data["references"][i] not in incumbent_lookup for i in eligible) if incumbent else 0,
            "candidate_rows_with_unavailable_incumbent_label": sum(data["references"][i] in incumbent_lookup and
                incumbent["available"][incumbent_lookup[data["references"][i]]] > cutoff for i in eligible) if incumbent else 0,
            "incumbent_observable_rows_absent_from_candidate": sum(reference not in candidate_references and receipt <= cutoff
                for reference, receipt in zip(incumbent["references"], incumbent["available"])) if incumbent else 0,
        },
        "feature_names": list(data["names"]), "candidate_manifest": data["manifest"],
        "incumbent_manifest": incumbent["manifest"] if incumbent else None,
        "availability_kinds": kinds, "blocks": [], "development": None, "holdout": None,
        "promotion_allowed": False, "production_writes": False, "selected_specs": [], "reasons": [],
        "limitations": ["Offline probability scores do not establish trading profitability or future performance.",
                        "Four temporal blocks are correlated market observations, not independent trade trials.",
                        "Existing receipt assumptions and missing source coverage are retained, not reconstructed."],
    }
    if any(kind != "actual" for kind in kinds):
        report["limitations"].append("Historical receipt timing includes explicit assumptions; causal results are conditional on them.")
    if len(dates) < BLOCK_DATES * (DEVELOPMENT_BLOCKS + 1):
        report["reasons"] = ["need_twenty_observed_label_dates_for_four_fixed_blocks"]
        report["elapsed_seconds"] = time.monotonic() - started
        return report
    last_dates = dates[-BLOCK_DATES * (DEVELOPMENT_BLOCKS + 1):]
    all_predictions = {}
    for block_number in range(DEVELOPMENT_BLOCKS + 1):
        block_dates = last_dates[block_number * BLOCK_DATES:(block_number + 1) * BLOCK_DATES]
        evaluation = np.array([i for i in common if data["dates"][i] in block_dates], dtype=int)
        holdout = block_number == DEVELOPMENT_BLOCKS
        block = {"role": "holdout" if holdout else "development", "block_number": block_number + 1,
                 "dates": block_dates, "date_start": block_dates[0], "date_end": block_dates[-1],
                 "status": "insufficient_data", "partition": None, "calibration": None,
                 "incumbent_partition": None, "comparators": {}, "predictions": []}
        report["blocks"].append(block)
        if holdout:
            report["holdout"] = block
        y = data["y"][evaluation]
        probabilities = {name: None for name in names}
        if not len(evaluation):
            block["reasons"] = ["no_common_observable_evaluation_rows"]
            block["comparators"] = {name: _result(y, reasons=block["reasons"]) for name in names}
            continue
        # Freeze before common-key filtering: an unmatched early held-out row
        # must never become training data for a later matched row in this block.
        block_rows = [i for i in eligible if data["dates"][i] in block_dates]
        fit_cutoff = data["references"][block_rows[0]]
        train, partition = _partition(data, evaluation, fit_cutoff, config, holdout=holdout)
        block["partition"] = partition
        calibration = _calibration(data, train, specs[0], config)
        block["calibration"] = calibration
        reasons = partition["gate_reasons"] + calibration["gate_reasons"]
        block["reasons"] = reasons
        if partition["gate_reasons"]:
            for name in ("neutral_0_5", "training_prevalence", "reference_direction"):
                block["comparators"][name] = _result(y, reasons=partition["gate_reasons"])
        else:
            baseline_p, baseline_evidence = _baselines(data, train, evaluation)
            for name, p in baseline_p.items():
                probabilities[name] = p
                block["comparators"][name] = _result(y, p, fitting=baseline_evidence if name == "reference_direction" else None)
        for spec in specs:
            if reasons:
                block["comparators"][spec.name] = _result(y, status="insufficient_data", reasons=reasons)
                continue
            try:
                p, fitted = _fit(data, train, evaluation, fit_cutoff, spec, deadline)
                probabilities[spec.name] = p
                block["comparators"][spec.name] = _result(y, p, **fitted)
            except Exception as exc:
                block["comparators"][spec.name] = _error_result(y, exc)
        if all(probabilities[spec.name] is not None for spec in specs):
            probabilities[PROPOSAL] = np.mean([probabilities[spec.name] for spec in specs], axis=0)
            block["comparators"][PROPOSAL] = _result(y, probabilities[PROPOSAL])
        else:
            block["comparators"][PROPOSAL] = _result(y, reasons=["both_fixed_members_required_no_partial_ensemble"])
        if incumbent:
            inc_evaluation = np.array([incumbent_lookup[data["references"][i]] for i in evaluation], dtype=int)
            inc_train, inc_partition = _partition(incumbent, inc_evaluation, fit_cutoff, config,
                                                incumbent=True, holdout=holdout)
            block["incumbent_partition"] = inc_partition
            members, member_predictions = [], []
            for spec in incumbent_specs:
                if inc_partition["gate_reasons"]:
                    members.append({"name": spec.name, **_result(y, status="insufficient_data", reasons=inc_partition["gate_reasons"])})
                    continue
                try:
                    p, fitted = _fit(incumbent, inc_train, inc_evaluation, fit_cutoff, spec, deadline)
                    member_predictions.append(p)
                    members.append({"name": spec.name, **_result(y, p, **fitted)})
                except Exception as exc:
                    members.append({"name": spec.name, **_error_result(y, exc)})
            if len(member_predictions) == len(incumbent_specs):
                probabilities[INCUMBENT] = np.mean(member_predictions, axis=0)
            block["comparators"][INCUMBENT] = _result(y, probabilities[INCUMBENT], members=members,
                reasons=[] if probabilities[INCUMBENT] is not None else ["all_ten_incumbent_members_required_no_partial_ensemble"])
        for position, i in enumerate(evaluation):
            key = {**data["identity"], "target_version": TARGET_VERSION,
                   "reference_at": data["references"][i].isoformat(),
                   "target_close_at": data["targets"][i].isoformat(), "label": int(data["y"][i])}
            key["evaluation_key"] = _digest(key)
            key["label_available_at"] = data["available"][i].isoformat()
            key["probabilities"] = {name: float(p[position]) if p is not None else None for name, p in probabilities.items()}
            key["hard_reference_direction_not_down"] = bool(data["X"][i, data["names"].index("body_return")] >= 0)
            block["predictions"].append(key)
        all_predictions[block_number] = probabilities
        if probabilities[PROPOSAL] is not None:
            block["status"] = "scored"
        elif any(row["status"] == "budget_exhausted" for row in block["comparators"].values()):
            block["status"] = "budget_exhausted"
        elif any(row["status"] == "failed" for row in block["comparators"].values()):
            block["status"] = "failed"
    development = report["blocks"][:DEVELOPMENT_BLOCKS]
    dev_y = np.array([row["label"] for block in development for row in block["predictions"]], dtype=np.uint8)
    dev_comparators = {}
    for name in names:
        complete = all(block["comparators"][name]["status"] == "scored" for block in development)
        p = np.concatenate([all_predictions[index][name] for index in range(DEVELOPMENT_BLOCKS)]) if complete else None
        dev_comparators[name] = _result(dev_y, p, reasons=[] if complete else ["all_three_development_blocks_required"])
    report["development"] = {"comparators": dev_comparators, "scored_blocks": sum(block["status"] == "scored" for block in development)}
    report["status"] = "scored" if all(block["status"] == "scored" for block in report["blocks"]) else "incomplete"
    report["reasons"] = [] if report["status"] == "scored" else sorted({reason for block in report["blocks"] for reason in block.get("reasons", [])})
    report["elapsed_seconds"] = time.monotonic() - started
    return report
