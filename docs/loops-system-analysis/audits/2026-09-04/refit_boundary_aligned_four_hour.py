"""Controlled 4H label repair experiment; no publication or DATASTORE writes.

Keep the first Gameplan's train/selection/calibration/assessment dates fixed.
Filter inaccurate window labels within each original cohort, then refit using
unchanged features, model families, hyperparameters, and calibration policy.
The assessment cohort is used only for final scoring and the promotion gate.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPOSITORY))

import joblib
import pandas as pd
from threadpoolctl import threadpool_limits

from ml.artifacts import file_checksum
from ml.nightly_gameplan import (
    _ProbabilityBlend,  # The September 4 model uses __main__._ProbabilityBlend.
    _build_current_groups,
    _build_training_groups,
    _chronological_partitions,
    _current_overnight_sources,
    _fit_group_model,
    _load_equity_minute_bars,
    _model_frame,
    _overnight_sources,
    _proper_scores,
    read_gameplan_run,
)

ROOT = Path("C:/DATASTORE")
RUN = ROOT / "ml/nightly-gameplan-runs/20260904T105944.876700Z"
OUTPUT = REPOSITORY / "artifacts/validation/four-hour-root-cause/boundary-corrected-v2"


def main():
    read_gameplan_run(ROOT, RUN)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    plan = json.loads((RUN / "gameplan.json").read_text())
    original_report = json.loads((RUN / "model-reports.json").read_text())["4h"]
    model = joblib.load(RUN / "models/4h/model.joblib")
    before_hashes = {name: file_checksum(RUN / name) for name in (
        "gameplan.json", "forecasts.parquet", "models/4h/model.joblib", "model-reports.json",
        "manifest.json", "receipt.json",
    )}
    samples_path = ROOT / plan["source_authorities"]["loop_b_run"] / "samples.parquet"
    samples = pd.read_parquet(samples_path)
    frozen = pd.Timestamp(plan["frozen_at"])
    sources = _overnight_sources(samples, symbols=plan["symbols"], available_at=frozen)
    bars, paths = _load_equity_minute_bars(ROOT, symbols=plan["symbols"])
    bars = bars.loc[bars.timestamp.lt(frozen)]
    features = model["feature_columns"]
    original = _build_training_groups(
        samples, sources=sources, feature_columns=features, minute_bars=bars,
        enforce_boundary_alignment=False,
    )["4h"]
    corrected = _build_training_groups(
        samples, sources=sources, feature_columns=features, minute_bars=bars,
    )["4h"]
    partitions = _chronological_partitions(original, group="4h")
    assert {name + "_rows": len(frame) for name, frame in partitions.items()} == original_report["partitions"]
    original_counts = {name: len(frame) for name, frame in partitions.items()}
    for name, frame in partitions.items():
        # Preserve time boundaries instead of repartitioning the smaller cohort.
        partitions[name] = frame.loc[frame.target_boundary_aligned].copy()
        partitions[name].attrs = corrected.attrs.copy()
        partitions[name].to_parquet(OUTPUT / (name + ".parquet"), index=False)
    current_sources, action_date = _current_overnight_sources(sources, symbols=plan["symbols"], as_of=frozen)
    current = _build_current_groups(
        samples, current_sources=current_sources, action_date=action_date,
        as_of=frozen, symbols=plan["symbols"], feature_columns=features,
    )["4h"]
    with patch("ml.nightly_gameplan._chronological_partitions", return_value=partitions):
        result = _fit_group_model(
            corrected, current=current, feature_columns=features, group="4h",
            model_directory=OUTPUT / "models/4h", trained_at=frozen,
        )
    assessment = partitions["assessment"]
    original_raw = model["estimator"].predict_proba(
        _model_frame(assessment, features, model["categorical_columns"])
    )[:, 1]
    result["forecasts"].to_parquet(OUTPUT / "forecasts.parquet", index=False)
    assert before_hashes == {name: file_checksum(RUN / name) for name in before_hashes}
    evidence = {
        "experiment": "Boundary-aligned labels with fixed original time partitions; no publication",
        "corrected": result["report"],
        "original_on_same_valid_assessment": {
            "raw": _proper_scores(assessment.target.to_numpy(), original_raw),
            "calibrated": _proper_scores(assessment.target.to_numpy(), model["calibrator"].predict(original_raw)),
        },
        "original_partition_rows": original_counts,
        "corrected_current_calibrated_range": [
            float(result["forecasts"].calibrated_probability.min()),
            float(result["forecasts"].calibrated_probability.max()),
        ],
        "source_files_sha256": {str(path): file_checksum(path) for path in (samples_path, *paths)},
        "original_gameplan_files_sha256": before_hashes,
        "original_gameplan_unchanged": True,
    }
    (OUTPUT / "comparison.json").write_text(json.dumps(evidence, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "comparison": str(OUTPUT / "comparison.json"),
        "corrected": result["report"],
        "original_on_same_valid_assessment": evidence["original_on_same_valid_assessment"],
    }, indent=2), flush=True)


if __name__ == "__main__":
    with threadpool_limits(limits=4):
        main()
