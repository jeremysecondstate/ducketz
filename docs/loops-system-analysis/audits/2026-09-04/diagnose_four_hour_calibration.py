"""Reproduce the first Gameplan's flat 4H calibration using saved local evidence.

Reads the DATASTORE and writes only four-hour-calibration-evidence.json beside
this script. It does not retrain the predictor, publish a plan, or use a broker.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPOSITORY))

import joblib
import pandas as pd

from app.ui.rolling_forecast_data import adapt_gameplan_forecasts
from ml.artifacts import file_checksum
from ml.calibration import fit_probability_calibrator
from ml.nightly_gameplan import (
    _ProbabilityBlend,  # The first saved model references __main__._ProbabilityBlend.
    _build_training_groups,
    _calibration_signal_diagnostics,
    _chronological_partitions,
    _load_equity_minute_bars,
    _model_frame,
    _overnight_sources,
    read_gameplan_run,
)


def main() -> None:
    root = Path("C:/DATASTORE")
    run = root / "ml/nightly-gameplan-runs/20260904T105944.876700Z"
    read_gameplan_run(root, run)
    plan = json.loads((run / "gameplan.json").read_text(encoding="utf-8"))
    report = json.loads((run / "model-reports.json").read_text(encoding="utf-8"))["4h"]
    model = joblib.load(run / "models/4h/model.joblib")
    forecasts = pd.read_parquet(run / "forecasts.parquet")
    four = forecasts.loc[forecasts.model_group.eq("4h")]
    samples_path = root / plan["source_authorities"]["loop_b_run"] / "samples.parquet"
    samples = pd.read_parquet(samples_path)
    frozen_at = pd.Timestamp(plan["frozen_at"])
    sources = _overnight_sources(samples, symbols=plan["symbols"], available_at=frozen_at)
    bars, _ = _load_equity_minute_bars(root, symbols=plan["symbols"])
    # Exclude any later append when rerunning this historical diagnosis.
    bars = bars.loc[bars.timestamp.lt(frozen_at)]
    group = _build_training_groups(
        samples, sources=sources, feature_columns=model["feature_columns"], minute_bars=bars,
    )["4h"]
    partitions = _chronological_partitions(group, group="4h")
    counts = {name + "_rows": len(frame) for name, frame in partitions.items()}
    assert counts == report["partitions"], (counts, report["partitions"])
    raw = model["estimator"].predict_proba(_model_frame(
        partitions["calibration"], model["feature_columns"], model["categorical_columns"],
    ))[:, 1]
    labels = partitions["calibration"].target.astype(int).to_numpy()
    assessment_raw = model["estimator"].predict_proba(_model_frame(
        partitions["assessment"], model["feature_columns"], model["categorical_columns"],
    ))[:, 1]
    diagnostics = _calibration_signal_diagnostics(
        model["calibrator"], raw, labels, model["calibrator"].predict(assessment_raw),
    )
    unconstrained = fit_probability_calibrator(
        "platt", raw, labels, platt_regularization_c=0.1,
    )
    view = adapt_gameplan_forecasts(
        forecasts, source_path=run / "forecasts.parquet", action_date=plan["action_date"],
        loaded_at=datetime(2026, 9, 4, 23, 0, tzinfo=timezone.utc),
    )
    assert len(four) == 24 and four.calibrated_probability.eq(0.5).all()
    assert diagnostics["status"] == "FLAT_CALIBRATION"
    assert diagnostics["information_available"] is False
    assert all(symbol.routes[1].probability_warning for symbol in view.symbols)
    payload = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "run": str(run), "partitions": counts, "four_hour_rows": len(four),
        "saved_raw_range": [float(four.raw_probability.min()), float(four.raw_probability.max())],
        "saved_calibrated_values": four.calibrated_probability.unique().tolist(),
        "unconstrained_platt_slope": float(unconstrained.model.coef_[0, 0]),
        "saved_platt_intercept": float(model["calibrator"].model.intercept_[0]),
        "diagnostics_under_new_guard": diagnostics,
        "original_promotion_gate": report["promotion_gate"],
        "original_assessment": report["assessment"],
        "original_training_base_rate_assessment": report["training_base_rate_assessment"],
        "ui_warnings": list(view.warnings),
        "saved_files_sha256": {name: file_checksum(run / name) for name in (
            "gameplan.json", "forecasts.parquet", "model-reports.json",
            "models/4h/model.joblib", "manifest.json", "receipt.json",
        )},
        "saved_outputs_modified": False,
    }
    output = Path(__file__).with_name("four-hour-calibration-evidence.json")
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
