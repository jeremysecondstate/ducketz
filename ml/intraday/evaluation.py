"""Point-in-time walk-forward experiments using common evaluation timestamps.

Research schedule comparisons may reuse a fitted object between their specified
retraining times. The initial 15m benchmark always fits at each quarter-hour
point. This module does not schedule workers or acquire provider/account data.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
import time
from typing import Callable, Sequence

import numpy as np

from ml.intraday.prediction import ContractError, STEP, TARGET_VERSION, instant

RETRAIN_INTERVALS = {"15m": timedelta(minutes=15), "30m": timedelta(minutes=30),
                    "1h": timedelta(hours=1), "2h": timedelta(hours=2),
                    "4h": timedelta(hours=4), "daily": timedelta(days=1),
                    "weekly": timedelta(days=7)}


@dataclass(frozen=True)
class EvaluationPoint:
    """A common nominal decision point; issue_at is its frozen data cutoff.

    Actual simulated forecast issuance is computed by run_walk_forward after
    measured work, rather than treating this nominal timestamp as publication.
    """
    reference_at: str | datetime
    issue_at: str | datetime
    target_available_at: str | datetime | None = None
    target_not_down: bool | int | None = None
    synthetic_target: bool = False
    availability_kind: str = "assumed_historical_close"

    @property
    def target_close_at(self):
        return instant(self.reference_at) + 3 * STEP

    def validate(self):
        reference = instant(self.reference_at)
        issued = instant(self.issue_at)
        if reference.minute % 15 or reference.second or reference.microsecond:
            raise ContractError("Evaluation references must be quarter-hour candle openings")
        if not reference + STEP <= issued < self.target_close_at:
            raise ContractError("Out-of-sample issue must follow the reference close and precede its fixed target")
        if self.availability_kind not in {"actual", "assumed_historical_close"}:
            raise ContractError("Historical availability must be explicitly actual or assumed")
        if self.target_not_down is not None and self.target_not_down not in (False, True, 0, 1):
            raise ContractError("Outcome must be the exact down/not-down classification")
        if self.target_available_at is not None and instant(self.target_available_at) < self.target_close_at:
            raise ContractError("Outcome cannot be available before the fixed target closes")
        if (self.target_not_down is None) != (self.target_available_at is None):
            raise ContractError("Outcome value and its availability evidence must be supplied together")
        return self


def eligible_training_rows(reference_at, target_close_at, label_available_at, *, training_cutoff,
                           window_start=None, synthetic_targets=None, synthetic_policy=None):
    """Select genuinely matured labels; no prior window or source is invented."""
    references = tuple(instant(v) for v in reference_at)
    targets = tuple(instant(v) for v in target_close_at)
    availability = tuple(instant(v) for v in label_available_at)
    if not len(references) == len(targets) == len(availability):
        raise ContractError("Training timestamp arrays must have identical lengths")
    if any(b <= a for a, b in zip(references, references[1:])):
        raise ContractError("Training references must be strictly chronological")
    if any(target != ref + 3 * STEP or available < target
           for ref, target, available in zip(references, targets, availability)):
        raise ContractError("Training timestamps must describe the exact available 45-minute target")
    synthetic = np.asarray(synthetic_targets if synthetic_targets is not None else [False] * len(references), dtype=bool)
    if synthetic.shape != (len(references),):
        raise ContractError("Synthetic-target flags must match training rows")
    if synthetic.any() and synthetic_policy not in {"include", "exclude"}:
        raise ContractError("Synthetic-label inclusion requires an explicit policy")
    if synthetic_policy not in {None, "include", "exclude"}:
        raise ContractError("Unknown synthetic-label policy")
    cutoff = instant(training_cutoff)
    start = instant(window_start) if window_start is not None else None
    return np.array([i for i, (reference, target, available) in enumerate(zip(references, targets, availability))
                     if target <= cutoff and available <= cutoff and (start is None or reference >= start)
                     and (synthetic_policy != "exclude" or not synthetic[i])], dtype=int)


def purged_validation_rows(reference_at, target_close_at, *, validation_start, validation_end):
    """Chronological holdout with overlapping label windows removed from fit."""
    references = tuple(instant(v) for v in reference_at)
    targets = tuple(instant(v) for v in target_close_at)
    start, end = instant(validation_start), instant(validation_end)
    if len(references) != len(targets) or start >= end:
        raise ContractError("Invalid chronological validation interval")
    if any(b <= a for a, b in zip(references, references[1:])):
        raise ContractError("Validation references must be strictly chronological")
    if any(target != reference + 3 * STEP for reference, target in zip(references, targets)):
        raise ContractError("Validation label windows must use the exact target")
    train = np.array([i for i, (reference, target) in enumerate(zip(references, targets))
                      if reference < start and target <= start], dtype=int)
    validation = np.array([i for i, reference in enumerate(references) if start <= reference < end], dtype=int)
    return train, validation


def build_walk_forward_plan(points: Sequence[EvaluationPoint], *, schedule="15m", lookback_days=None):
    """Elapsed UTC cadences anchored to the first reference opening, on one grid.

    Daily/weekly mean 24h/7d elapsed time, with a due fit at the next supplied
    evaluation point after a session gap. Actual issue delays do not change the
    reference grid. They are research definitions, not new
    live schedules. None lookback uses every compatible matured historical row.
    """
    if schedule not in RETRAIN_INTERVALS:
        raise ContractError(f"Unsupported research retraining schedule: {schedule}")
    if lookback_days is not None and (not math.isfinite(lookback_days) or lookback_days <= 0):
        raise ContractError("Lookback days must be positive or None for all compatible history")
    previous_issue = None
    previous_reference = None
    last_fit = None
    last_fit_reference = None
    plan = []
    for point in points:
        point.validate()
        issue = instant(point.issue_at)
        reference = instant(point.reference_at)
        if previous_issue is not None and issue <= previous_issue:
            raise ContractError("Evaluation issue timestamps must be strictly chronological")
        if previous_reference is not None and reference <= previous_reference:
            raise ContractError("Each immutable reference candle may be evaluated only once, chronologically")
        due = last_fit is None or schedule == "15m" or reference - last_fit_reference >= RETRAIN_INTERVALS[schedule]
        if due:
            last_fit = issue
            last_fit_reference = reference
        plan.append({"point": point, "retrain": due, "training_cutoff": last_fit.isoformat(),
                     "window_start": (last_fit - timedelta(days=lookback_days)).isoformat() if lookback_days is not None else None,
                     "model_age_seconds": (issue - last_fit).total_seconds(), "schedule": schedule,
                     "target_version": TARGET_VERSION})
        previous_issue = issue
        previous_reference = reference
    return plan


def _metrics(rows):
    scored = [r for r in rows if r["outcome_status"] == "scored"]
    if not scored:
        return {"scored": 0, "accuracy": None, "balanced_accuracy": None, "brier": None, "log_loss": None}
    y = np.array([r["target_not_down"] for r in scored], dtype=int)
    p = np.array([r["probability_not_down"] for r in scored], dtype=float)
    correct = (p >= 0.5) == y
    class_scores = [float(correct[y == label].mean()) for label in (0, 1) if (y == label).any()]
    clipped = np.clip(p, 1e-15, 1 - 1e-15)
    return {"scored": len(scored), "accuracy": float(correct.mean()),
            "balanced_accuracy": float(np.mean(class_scores)) if len(class_scores) == 2 else None,
            "brier": float(np.mean((p - y) ** 2)),
            "log_loss": float(np.mean(-y * np.log(clipped) - (1 - y) * np.log(1 - clipped)))}


def run_walk_forward(points, *, fit: Callable, predict: Callable, schedule="15m", lookback_days=None,
                     evaluation_as_of, synthetic_policy, run_metadata=None, clock=None, perf_counter=None):
    """Run real supplied fit/inference callbacks and measure each actual call.

    fit receives training_cutoff/window_start and returns (fitted, evidence).
    evidence must prove target_version and latest_label_available_at. predict
    receives fitted/point and returns a persisted-model output mapping with its
    probability, completeness and model details. Callbacks retain artifact and
    dataset provenance; failures append records, and fit failures invalidate the
    previous model rather than becoming an undocumented fallback. Callback point
    issue_at remains the nominal decision/data cutoff so experiments use common
    causal snapshots. Returned issued_at includes measured processing and any
    serialized queue delay. Actual computation timestamps are recorded separately
    from that historical simulation clock. A failed callback never issues a
    forecast. Late successful forecasts retain their exact targets and explicit
    timely/late score breakdowns.
    """
    if synthetic_policy not in {"include", "exclude"}:
        raise ContractError("Evaluation requires an explicit synthetic headline policy")
    as_of = instant(evaluation_as_of)
    clock = clock or (lambda: datetime.now(timezone.utc))
    perf_counter = perf_counter or time.perf_counter
    fitted = None
    evidence = None
    rows = []
    previous_completion = None
    for planned in build_walk_forward_plan(points, schedule=schedule, lookback_days=lookback_days):
        point = planned["point"]
        nominal = instant(point.issue_at)
        if nominal > as_of:
            raise ContractError("Historical decision cutoff is after the evaluation-as-of time")
        simulated_start = max(nominal, previous_completion) if previous_completion is not None else nominal
        point_started = perf_counter()
        row = {k: v for k, v in planned.items() if k != "point"}
        row.update({"reference_at": instant(point.reference_at).isoformat(), "issued_at": None,
                    "scheduled_issue_at": nominal.isoformat(), "decision_cutoff_at": nominal.isoformat(),
                    "simulated_started_at": simulated_start.isoformat(),
                    "queue_delay_seconds": (simulated_start - nominal).total_seconds(),
                    "compute_started_at": instant(clock()).isoformat(), "compute_completed_at": None,
                    "recorded_at": None, "compute_seconds": 0.0, "issue_after_target": None,
                    "target_close_at": point.target_close_at.isoformat(), "availability_kind": point.availability_kind,
                    "synthetic_target": None, "target_not_down": None,
                    "target_available_at": instant(point.target_available_at).isoformat() if point.target_available_at is not None else None,
                    "probability_not_down": None, "training_seconds": 0.0, "inference_seconds": 0.0,
                    "training_succeeded": False, "outcome_status": "pending", "error": None, "outputs": []})
        phase = "training"
        try:
            if planned["retrain"]:
                fitted = None
                evidence = None
                started = perf_counter()
                fitted, evidence = fit(training_cutoff=planned["training_cutoff"], window_start=planned["window_start"])
                row["training_seconds"] = perf_counter() - started
                if (evidence.get("target_version") != TARGET_VERSION
                        or instant(evidence["latest_label_available_at"]) > instant(planned["training_cutoff"])):
                    fitted = None
                    raise ContractError("Fit callback did not prove labels matured at the exact-target training cutoff")
                if ("latest_feature_available_at" in evidence
                        and instant(evidence["latest_feature_available_at"]) > instant(planned["training_cutoff"])):
                    fitted = None
                    raise ContractError("Fit callback used features unavailable at the training cutoff")
                if "training_cutoff" in evidence and instant(evidence["training_cutoff"]) != instant(planned["training_cutoff"]):
                    fitted = None
                    raise ContractError("Fit callback reported a different training cutoff")
                row["training_succeeded"] = True
            if fitted is None:
                raise ContractError("No valid fitted research model; prior failed fit is not a fallback")
            row["training_evidence"] = dict(evidence)
            phase = "inference"
            started = perf_counter()
            # Neither realized outcomes nor retrospective synthetic-target flags
            # are inference inputs. The callback receives only causal timing.
            causal_point = EvaluationPoint(reference_at=point.reference_at, issue_at=point.issue_at,
                                            availability_kind=point.availability_kind)
            prediction = dict(predict(fitted=fitted, point=causal_point))
            row["inference_seconds"] = perf_counter() - started
            probability = float(prediction["probability_not_down"])
            if not math.isfinite(probability) or not 0 <= probability <= 1:
                raise ContractError("Inference callback emitted an invalid probability")
            row.update({"probability_not_down": probability, "outputs": prediction.get("outputs", []),
                        "complete": bool(prediction.get("complete", False)),
                        "configured_count": prediction.get("configured_count"),
                        "successful_count": prediction.get("successful_count")})
        except Exception as exc:
            row["outcome_status"] = "unscorable"
            row["probability_not_down"] = None
            row["error"] = {"phase": phase, "type": type(exc).__name__, "message": str(exc)}
            if phase == "training" and planned["retrain"]:
                fitted = None
                evidence = None
                row["training_seconds"] = perf_counter() - started
            elif phase == "inference":
                row["inference_seconds"] = perf_counter() - started
        elapsed = perf_counter() - point_started
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ContractError("Evaluation processing clock must be finite and monotonic")
        row["compute_seconds"] = elapsed
        row["compute_completed_at"] = row["recorded_at"] = instant(clock()).isoformat()
        if instant(row["compute_completed_at"]) < instant(row["compute_started_at"]):
            raise ContractError("Actual evaluation computation clock regressed")
        previous_completion = simulated_start + timedelta(seconds=elapsed)
        row["simulated_completed_at"] = previous_completion.isoformat()
        if row["error"] is None:
            row["issued_at"] = previous_completion.isoformat()
            row["issue_after_target"] = previous_completion >= point.target_close_at
            row["model_age_at_issue_seconds"] = (previous_completion - instant(planned["training_cutoff"])).total_seconds()
            if previous_completion > as_of:
                row["outcome_status"] = "pending_issue"
            elif point.target_available_at is not None and instant(point.target_available_at) <= as_of:
                row["target_not_down"] = bool(point.target_not_down)
                row["synthetic_target"] = point.synthetic_target
                row["outcome_status"] = "excluded_synthetic" if point.synthetic_target and synthetic_policy == "exclude" else "scored"
            elif as_of >= point.target_close_at:
                row["outcome_status"] = "missing" if point.target_available_at is None else "pending_availability"
        rows.append(row)
    return {"schedule": schedule, "lookback_days": lookback_days, "synthetic_policy": synthetic_policy,
            "evaluation_as_of": as_of.isoformat(), "target_version": TARGET_VERSION, "rows": rows,
            "denominator": "all successfully issued callback forecasts with available targets, subject to explicit synthetic policy",
            "headline": _metrics(rows), "observed": _metrics([r for r in rows if not r["synthetic_target"]]),
            "synthetic": _metrics([r for r in rows if r["synthetic_target"]]),
            "full_ensemble": _metrics([r for r in rows if r.get("complete")]),
            "incomplete_ensemble": _metrics([r for r in rows if not r.get("complete")]),
            "timely": _metrics([r for r in rows if r["issue_after_target"] is False]),
            "late": _metrics([r for r in rows if r["issue_after_target"] is True]),
            "issued_count": sum(r["issued_at"] is not None for r in rows),
            "late_issue_count": sum(r["issue_after_target"] is True for r in rows),
            "issuance_clock": "nominal_decision_plus_measured_processing_and_serialized_queue_delay",
            "status_counts": {status: sum(r["outcome_status"] == status for r in rows)
                              for status in {r["outcome_status"] for r in rows}},
            "training_attempt_count": sum(r["retrain"] for r in rows),
            "full_training_count": sum(r["retrain"] and r["training_succeeded"] for r in rows),
            "run_metadata": dict(run_metadata or {})}


def compare_schedules(points, *, fit, predict, evaluation_as_of, synthetic_policy,
                      schedules=tuple(RETRAIN_INTERVALS), lookbacks=(None,), run_metadata=None,
                      clock=None, perf_counter=None):
    """Run each explicitly defined experiment on identical prediction timestamps."""
    points = tuple(points)
    results = []
    for lookback in lookbacks:
        for schedule in schedules:
            results.append(run_walk_forward(points, fit=fit, predict=predict, schedule=schedule,
                                            lookback_days=lookback, evaluation_as_of=evaluation_as_of,
                                            synthetic_policy=synthetic_policy, run_metadata=run_metadata,
                                            clock=clock, perf_counter=perf_counter))
    return results
