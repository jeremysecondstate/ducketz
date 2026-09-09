"""One preregistered selection experiment; never score the assessment cohort."""
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import pandas as pd

from ml.artifacts import file_checksum
from ml.gameplan_development_selection import select_development_calibrator
from ml.nightly_gameplan import _chronological_partitions, _estimator, _model_frame, _proper_scores, read_gameplan_run


root = Path("C:/DATASTORE")
source = root / "ml/nightly-gameplan-runs/20260909T040455.436642Z"
output = Path(__file__).resolve().parent
preregistration = {
    "registered_at": datetime.now(timezone.utc).isoformat(),
    "source_run": str(source), "cohort_sha256": file_checksum(source / "training-cohort-1d.parquet"),
    "purpose": "Reduce unsupported model complexity observed on original development selection",
    "candidate_logistic_c": [0.001, 0.01, 0.1, 1.0],
    "selection": "Minimum original selection log loss; lower C wins exact ties",
    "reference": "Original best candidate selection score from verified source report",
    "calibration": "Unchanged purged chronological identity-versus-constrained-Platt development selection",
    "assessment_scored": False, "assessment_used_for_selection": False,
    "training_features": "Exactly original admitted train-only numeric columns and symbol/route categories",
    "no_additional_grid_or_assessment_search": True,
}
registration = output / "preregistration.json"
if registration.exists():
    raise RuntimeError("This preregistered experiment already ran; do not overwrite")
registration.write_text(json.dumps(preregistration, indent=2) + "\n", encoding="utf-8")
publication = read_gameplan_run(root, source)
report = json.loads((source / "model-reports.json").read_text(encoding="utf-8"))["1d"]
numeric, categorical = tuple(report["features"]["admitted"]), ("symbol", "route")
cohort = pd.read_parquet(source / "training-cohort-1d.parquet")
partitions = _chronological_partitions(cohort, group="1d")
del partitions["assessment"]
del cohort
train, selection, calibration = [partitions[name] for name in ("train", "selection", "calibration")]
matrix_train, matrix_selection = [_model_frame(frame, numeric, categorical) for frame in (train, selection)]
metrics = {}
for value in preregistration["candidate_logistic_c"]:
    estimator = _estimator("logistic", numeric, categorical)
    estimator.set_params(classifier__C=value)
    estimator.fit(matrix_train, train.target.astype(int).to_numpy())
    scores = _proper_scores(selection.target.astype(int).to_numpy(), estimator.predict_proba(matrix_selection)[:, 1])
    metrics[str(value)] = scores
    print(json.dumps({"event": "DEVELOPMENT_CANDIDATE", "C": value, "selection_scores": scores}), flush=True)
selected = min(preregistration["candidate_logistic_c"], key=lambda value: metrics[str(value)]["log_loss"])
fit = pd.concat([train, selection], ignore_index=True)
estimator = _estimator("logistic", numeric, categorical)
estimator.set_params(classifier__C=selected)
estimator.fit(_model_frame(fit, numeric, categorical), fit.target.astype(int).to_numpy())
raw = estimator.predict_proba(_model_frame(calibration, numeric, categorical))[:, 1]
calibrator, calibration_report = select_development_calibrator(calibration, raw)
locked = {
    **preregistration, "selected_logistic_c": selected, "candidate_selection_metrics": metrics,
    "original_selected_family": report["selected_family"],
    "original_selected_selection_scores": report["selection_metrics"][report["selected_family"]],
    "calibration_selection": calibration_report,
    "selected_calibration_raw_scores": _proper_scores(calibration.target.astype(int).to_numpy(), raw),
    "locked_at": datetime.now(timezone.utc).isoformat(),
    "partition_rows_used": {name: len(frame) for name, frame in partitions.items()},
    "fitted_positive_rate": float(fit.target.mean()),
}
joblib.dump({"estimator": estimator, "calibrator": calibrator, "feature_columns": numeric,
             "categorical_columns": categorical, "selected_logistic_c": selected,
             "selection_lock": locked}, output / "locked-development-candidate.joblib")
(output / "development-selection-result.json").write_text(json.dumps(locked, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"event": "CANDIDATE_LOCKED", "C": selected,
                  "calibration_family": calibration_report["selected_family"],
                  "assessment_scored": False, "evidence": str(output / "development-selection-result.json")}), flush=True)
