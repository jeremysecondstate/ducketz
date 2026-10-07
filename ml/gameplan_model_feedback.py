"""Bounded model review of the same immutable outcomes displayed by Gameplan Stats.

Preparation only diagnoses; an explicit reviewer response is required before
training can consume candidates. Reviewers cannot change splits or promotion
gates through this interface. No data acquisition or fitting occurs here.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
from typing import Mapping
import uuid

import pandas as pd

from app.ui.gameplan_stats_data import HORIZONS, load_gameplan_stats
from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp
from ml.gameplan_probability_target import LEGACY_COST_TARGET, RAW_DIRECTION_TARGET, resolve_probability_target

VERSION = "nightly-gameplan-model-feedback-v1"
REVIEW_VERSION = "nightly-gameplan-model-review-v1"
_POLICY_FILES = ("ml/gameplan_model_feedback.py", "ml/nightly_gameplan.py",
                 "ml/gameplan_development_selection.py", "ml/gameplan_promotion.py",
                 "ml/gameplan_champions.py", "ml/gameplan_probability_target.py",
                 "ml/calibration.py", "ml/independent_stock_targets.py",
                 "app/ui/gameplan_stats_data.py")
_BOUNDS = {
    "tree": {"learning_rate": (0.005, 0.2, False), "max_iter": (30, 300, True),
             "max_leaf_nodes": (3, 63, True), "l2_regularization": (0.0, 100.0, False)},
    "neural": {"alpha": (0.00001, 1.0, False), "learning_rate_init": (0.0001, 0.01, False),
               "max_iter": (30, 400, True)},
    "logistic": {"C": (0.0001, 10.0, False)},
}


def _code_binding() -> dict:
    checkout = Path(__file__).resolve().parents[1]
    return {name: file_checksum(checkout / name) for name in _POLICY_FILES}


def _write(path: Path, value: Mapping) -> None:
    text = json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Published review inputs cannot silently change beneath a resumed fit.
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError(f"Immutable feedback output already exists: {path.name}")
        return
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            if path.read_text(encoding="utf-8") != text:
                raise ValueError(f"Immutable feedback output already exists: {path.name}")
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _stats(root: Path, review_run: Path | None = None):
    review = load_gameplan_stats(root)
    if review_run is not None and Path(review_run).resolve() != review.run_directory.resolve():
        raise ValueError("Model feedback requires the exact newest completed Gameplan Stats run")
    run = review.run_directory
    report = json.loads((run / "report.json").read_text(encoding="utf-8"))
    baseline = report.get("coverage_status") == "NO_SAVED_INDEPENDENT_GAMEPLAN"
    if baseline and (report.get("source_gameplan_run") or report.get("source_gameplan_path")
                     or not pd.read_parquet(run / "forecast-results.parquet").empty
                     or review.outcomes or review.excluded_forecasts):
        raise ValueError("A no-history baseline must have no saved source or forecast rows")
    binding = {"run_path": run.relative_to(root.resolve()).as_posix(), "session": review.session,
               "reviewed_at": review.reviewed_at.isoformat(),
               "outcomes_through": review.outcomes_through.isoformat(),
               "probability_target_contract": None if baseline else review.probability_target_contract,
               "baseline_no_saved_predictions": baseline,
               "symbols": list(review.symbols),
               "receipt_sha256": file_checksum(run / "receipt.json"),
               "manifest_sha256": file_checksum(run / "manifest.json"),
               "results_sha256": file_checksum(run / "forecast-results.parquet"),
               "report_sha256": file_checksum(run / "report.json")}
    return review, binding


def feedback_review_schema() -> dict:
    """JSON schema for a strong reviewer, also independently enforced in Python."""
    alternatives = []
    for family, bounds in _BOUNDS.items():
        properties = {key: {"type": "integer" if integer else "number", "minimum": low, "maximum": high}
                      for key, (low, high, integer) in bounds.items()}
        if family == "neural":
            properties["hidden_layer_sizes"] = {"type": "array", "minItems": 1, "maxItems": 3,
                "items": {"type": "integer", "minimum": 8, "maximum": 256}}
        alternatives.append({"type": "object", "additionalProperties": False,
            "required": ["family", "parameters"], "properties": {
                "family": {"type": "string", "enum": [family]},
                "parameters": {"type": "object", "additionalProperties": False,
                               "required": list(properties), "properties": properties}}})
    group = {"type": "object", "additionalProperties": False,
             "required": ["decision", "rationale", "candidates"], "properties": {
                 "decision": {"type": "string", "enum": ["KEEP_CURRENT", "EVALUATE_CANDIDATES"]},
                 "rationale": {"type": "string", "minLength": 1, "maxLength": 4000},
                 "candidates": {"type": "array", "maxItems": 2, "items": {"anyOf": alternatives}}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["schema_version", "stats_receipt_sha256", "reviewed_by", "groups"],
            "properties": {"schema_version": {"type": "string", "enum": [REVIEW_VERSION]},
                "stats_receipt_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "reviewed_by": {"type": "string", "minLength": 1, "maxLength": 200},
                "groups": {"type": "object", "additionalProperties": False,
                           "required": list(HORIZONS), "properties": {name: group for name in HORIZONS}}}}


def validate_candidates(candidates: object) -> list[dict]:
    if not isinstance(candidates, list) or len(candidates) > 2:
        raise ValueError("At most two feedback candidates are permitted per horizon")
    result = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) != {"family", "parameters"}:
            raise ValueError("Candidate requires exactly family and parameters")
        family, parameters = candidate["family"], candidate["parameters"]
        if family not in _BOUNDS or not isinstance(parameters, dict) or not parameters:
            raise ValueError("Unsupported or empty model candidate")
        valid = set(_BOUNDS[family]) | ({"hidden_layer_sizes"} if family == "neural" else set())
        if not set(parameters).issubset(valid):
            raise ValueError("Model review may not alter features, splits, targets, random seeds or promotion gates")
        for key, value in parameters.items():
            if key == "hidden_layer_sizes":
                if (not isinstance(value, list) or not 1 <= len(value) <= 3
                        or any(type(width) is not int or not 8 <= width <= 256 for width in value)
                        or sum(value) > 512):
                    raise ValueError("Neural architecture exceeds the bounded candidate interface")
            else:
                low, high, integer = _BOUNDS[family][key]
                if (type(value) not in (int, float) or not math.isfinite(value)
                        or not low <= value <= high or (integer and type(value) is not int)):
                    raise ValueError(f"Invalid bounded candidate parameter: {key}")
        if candidate in result:
            raise ValueError("Duplicate feedback candidates are not allowed")
        result.append(candidate)
    return result


def _validate_proposal(proposal: Mapping, diagnostics: Mapping) -> None:
    if (not isinstance(proposal, dict) or set(proposal) != {
            "schema_version", "stats_receipt_sha256", "reviewed_by", "groups"}
            or proposal["schema_version"] != REVIEW_VERSION
            or proposal["stats_receipt_sha256"] != diagnostics["stats"]["receipt_sha256"]):
        raise ValueError("Reviewer response is not bound to this completed Stats receipt")
    if not isinstance(proposal["reviewed_by"], str) or not 1 <= len(proposal["reviewed_by"].strip()) <= 200:
        raise ValueError("Model review requires a named reviewer")
    groups = proposal["groups"]
    if not isinstance(groups, dict) or set(groups) != set(HORIZONS):
        raise ValueError("Model review requires a decision for every horizon")
    for horizon, decision in groups.items():
        if not isinstance(decision, dict) or set(decision) != {"decision", "rationale", "candidates"}:
            raise ValueError("Unsupported horizon review fields")
        if not isinstance(decision["rationale"], str) or not 1 <= len(decision["rationale"].strip()) <= 4000:
            raise ValueError("Every horizon decision requires a bounded rationale")
        candidates = validate_candidates(decision["candidates"])
        if candidates and not diagnostics["groups"][horizon]["evaluated"]:
            raise ValueError("A horizon with no completed observations requires KEEP_CURRENT")
        expected = "EVALUATE_CANDIDATES" if candidates else "KEEP_CURRENT"
        if decision["decision"] != expected:
            raise ValueError("Horizon decision and candidate list disagree")


def _diagnostic_groups(review) -> dict:
    groups = {}
    for horizon in HORIZONS:
        metrics = review.metrics(horizon)
        alerts = []
        if metrics.evaluated < 30:
            alerts.append("LIMITED_COMPLETED_OUTCOME_SUPPORT")
        if metrics.awaiting_data:
            alerts.append("MATURE_OUTCOMES_MISSING_DATA")
        if metrics.pending:
            alerts.append("LONGER_OUTCOMES_STILL_PENDING")
        groups[horizon] = {**asdict(metrics), "direction_accuracy": metrics.accuracy,
                           "bullish_accuracy": metrics.bullish_accuracy,
                           "bearish_accuracy": metrics.bearish_accuracy, "alerts": alerts}
    return groups


def prepare_feedback(root: Path, *, review_run: Path | None = None,
                     output: Path | None = None, now=None, probability_target: str | None = None) -> Path:
    root = Path(root).resolve()
    review, binding = _stats(root, review_run)
    created = utc_timestamp(now)
    if created < pd.Timestamp(review.reviewed_at) or created < pd.Timestamp(review.outcomes_through):
        raise ValueError("Model review cannot consume future Stats")
    intended_target = _intended_target(binding, probability_target)
    source_report = json.loads((review.run_directory / "report.json").read_text(encoding="utf-8"))
    saved_models = {}
    if source_report.get("source_gameplan_run"):
        from ml.nightly_gameplan import read_gameplan_run
        publication = read_gameplan_run(root, root / source_report["source_gameplan_run"])
        reports = json.loads((publication.run_directory / "model-reports.json").read_text(encoding="utf-8"))
        for horizon in HORIZONS:
            model = reports.get(horizon, {})
            saved_models[horizon] = {key: model.get(key) for key in (
                "selected_family", "model_file", "model_feedback", "selection_metrics",
                "assessment", "promotion_gate", "partitions", "deployment")}
    diagnostics = {"schema_version": VERSION, "status": "AWAITING_MODEL_REVIEW",
        "created_at": created.isoformat(), "stats": binding, "training_code": _code_binding(),
        "intended_probability_target_contract": intended_target,
        "groups": _diagnostic_groups(review), "excluded_unpromoted_forecasts": review.excluded_forecasts,
        "models_that_produced_scored_predictions": saved_models,
        "metric_semantics": "Exact Gameplan Stats promoted forecast outcomes; direction uses raw return sign; Brier uses each saved probability target. Pending/missing outcomes are not losses.",
        "review_instruction": "Review every horizon. Propose at most two justified parameter/MLP-architecture candidates or KEEP_CURRENT. Horizons with zero completed observations must KEEP_CURRENT with no candidates. A nightly review does not require a change. Use only completed outcomes. Neural layers must total at most 512 units. Candidates are added to default specifications and selected on purged chronological development data; unchanged assessment gates alone permit promotion. No fitting or promotion has occurred.",
        "selection_policy": "existing_chronological_selection_log_loss_defaults_win_ties",
        "assessment_used_for_candidate_selection": False,
        "orders_placed": 0, "broker_orders_enabled": False}
    run = Path(output).resolve() if output else create_timestamp_directory(root / "ml/gameplan-model-feedback-runs", timestamp=created)
    _write(run / "diagnostics.json", diagnostics)
    _write(run / "review-schema.json", feedback_review_schema())
    return run


def save_feedback_review(root: Path, feedback_run: Path, proposal: Mapping, *, now=None) -> Path:
    run = Path(feedback_run).resolve()
    diagnostics = json.loads((run / "diagnostics.json").read_text(encoding="utf-8"))
    _verify_diagnostics(Path(root).resolve(), diagnostics)
    _validate_proposal(proposal, diagnostics)
    output = run / "reviewed-proposal.json"
    if output.exists():
        saved = load_feedback_review(root, output)
        if saved["proposal"] != proposal:
            raise ValueError("A reviewed proposal cannot be replaced; create a new feedback run")
        return output
    reviewed_at = utc_timestamp(now)
    if reviewed_at < pd.Timestamp(diagnostics["created_at"]):
        raise ValueError("Review predates its diagnostics")
    _write(output, {"schema_version": VERSION, "status": "REVIEWED", "reviewed_at": reviewed_at.isoformat(),
                    "diagnostics_sha256": file_checksum(run / "diagnostics.json"), "proposal": proposal})
    return output


def _verify_diagnostics(root: Path, diagnostics: Mapping) -> None:
    review, actual = _stats(root)
    if (diagnostics.get("schema_version") != VERSION or diagnostics.get("stats") != actual
            or diagnostics.get("training_code") != _code_binding()):
        raise ValueError("Model review Stats or training implementation changed; review fresh evidence")
    if diagnostics.get("groups") != _diagnostic_groups(review):
        raise ValueError("Model diagnostics disagree with the verified UI Stats metrics")
    _intended_target(actual, diagnostics.get("intended_probability_target_contract"))


def _intended_target(stats: Mapping, requested: str | None) -> str:
    if stats["baseline_no_saved_predictions"]:
        if requested is None:
            raise ValueError("No-history feedback requires an explicit intended training probability target")
        return resolve_probability_target(requested)
    intended = resolve_probability_target(requested or stats["probability_target_contract"])
    if intended != stats["probability_target_contract"]:
        raise ValueError("Model review and training probability targets differ")
    return intended


def load_feedback_review(root: Path, path: Path, *, as_of=None, probability_target=None) -> dict:
    path = Path(path).resolve()
    if path.stat().st_size > 131072:
        raise ValueError("Model review exceeds the bounded input size")
    payload = json.loads(path.read_text(encoding="utf-8"))
    diagnostic_path = path.parent / "diagnostics.json"
    diagnostics = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    if (payload.get("schema_version") != VERSION or payload.get("status") != "REVIEWED"
            or payload.get("diagnostics_sha256") != file_checksum(diagnostic_path)):
        raise ValueError("Model review is incomplete or its diagnostics changed")
    _verify_diagnostics(Path(root).resolve(), diagnostics)
    _validate_proposal(payload["proposal"], diagnostics)
    reviewed = pd.Timestamp(payload["reviewed_at"])
    if reviewed.tzinfo is None or reviewed < pd.Timestamp(diagnostics["created_at"]):
        raise ValueError("Model review has an invalid completion time")
    if as_of is not None and reviewed > utc_timestamp(as_of):
        raise ValueError("Model review was not completed before training")
    intended = _intended_target(diagnostics["stats"], diagnostics.get("intended_probability_target_contract"))
    if probability_target is not None and intended != resolve_probability_target(probability_target):
        raise ValueError("Model review and training probability targets differ")
    return {**payload, "stats": diagnostics["stats"], "path": str(path),
            "intended_probability_target_contract": intended,
            "checksum_sha256": file_checksum(path)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore", type=Path, required=True)
    parser.add_argument("--review-run", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--feedback-run", type=Path)
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--probability-target-contract", choices=(RAW_DIRECTION_TARGET, LEGACY_COST_TARGET),
                        help="Explicit intended training target; required for no-history baseline review")
    args = parser.parse_args(argv)
    if args.proposal:
        if not args.feedback_run:
            parser.error("--proposal requires --feedback-run")
        result = save_feedback_review(args.datastore, args.feedback_run,
                                      json.loads(args.proposal.read_text(encoding="utf-8")))
    else:
        result = prepare_feedback(args.datastore, review_run=args.review_run, output=args.output,
                                  probability_target=args.probability_target_contract)
    print(json.dumps({"status": "REVIEWED" if args.proposal else "AWAITING_MODEL_REVIEW", "path": str(result)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
