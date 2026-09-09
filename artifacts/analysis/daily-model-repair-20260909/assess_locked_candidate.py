"""Score exactly the already-locked candidate once; do not select another."""
import json
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from ml.artifacts import file_checksum
from ml.nightly_gameplan import _calibration_signal_diagnostics, _chronological_partitions, _model_frame, _proper_scores

output = Path(__file__).resolve().parent
destination = output / "locked-candidate-assessment.json"
if destination.exists():
    raise RuntimeError("This locked candidate was already assessed")
locked = json.loads((output / "development-selection-result.json").read_text())
source = Path(locked["source_run"])
if file_checksum(source / "training-cohort-1d.parquet") != locked["cohort_sha256"]:
    raise RuntimeError("Pinned cohort changed")
payload = joblib.load(output / "locked-development-candidate.joblib")
if payload["selection_lock"] != locked:
    raise RuntimeError("Candidate differs from its preregistered lock")
partitions = _chronological_partitions(pd.read_parquet(source / "training-cohort-1d.parquet"), group="1d")
assessment, calibration = partitions["assessment"], partitions["calibration"]
estimator, calibrator = payload["estimator"], payload["calibrator"]
numeric, categorical = payload["feature_columns"], payload["categorical_columns"]
raw = estimator.predict_proba(_model_frame(assessment, numeric, categorical))[:, 1]
prediction = calibrator.predict(raw)
calibration_raw = estimator.predict_proba(_model_frame(calibration, numeric, categorical))[:, 1]
diagnostics = _calibration_signal_diagnostics(calibrator, calibration_raw, calibration.target.to_numpy(), prediction)
scores = _proper_scores(assessment.target.to_numpy(), prediction)
baseline = _proper_scores(assessment.target.to_numpy(), np.full(len(assessment), locked["fitted_positive_rate"]))
checks = {"calibration_retains_directional_information": diagnostics["information_available"],
    "assessment_has_at_least_10_decision_clusters": assessment.decision_timestamp.nunique() >= 10,
    "brier_beats_training_base_rate": scores["brier_score"] < baseline["brier_score"],
    "log_loss_beats_training_base_rate": scores["log_loss"] < baseline["log_loss"],
    "expected_calibration_error_at_most_0_15": scores["expected_calibration_error_10_bin"] <= .15}
report = {"evaluated_at": datetime.now(timezone.utc).isoformat(),
    "selection_lock_sha256": file_checksum(output / "development-selection-result.json"),
    "candidate_sha256": file_checksum(output / "locked-development-candidate.joblib"),
    "cohort_sha256": locked["cohort_sha256"], "selected_logistic_c": locked["selected_logistic_c"],
    "assessment_used_for_selection": False, "candidate_locked_at": locked["locked_at"],
    "assessment": scores, "training_base_rate_assessment": baseline,
    "calibration_diagnostics": diagnostics, "assessment_decision_clusters": int(assessment.decision_timestamp.nunique()),
    "promotion_gate": {"status": "PROMOTED" if all(checks.values()) else "RESEARCH_NOT_PROMOTED", "checks": checks},
    "no_further_candidates_or_parameter_search_on_this_assessment": True}
destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2))
