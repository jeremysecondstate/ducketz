"""Read-only diagnosis of the first 4H model; reports go to workspace artifacts."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPOSITORY))

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score
from threadpoolctl import threadpool_limits

from ml.calibration import fit_probability_calibrator
from ml.nightly_gameplan import (
    _ProbabilityBlend,
    _build_training_groups,
    _chronological_partitions,
    _load_equity_minute_bars,
    _model_frame,
    _overnight_sources,
    _proper_scores,
    read_gameplan_run,
)

ROOT = Path("C:/DATASTORE")
RUN = ROOT / "ml/nightly-gameplan-runs/20260904T105944.876700Z"
OUTPUT = REPOSITORY / "artifacts/validation/four-hour-root-cause"


def scores(y, p):
    return {
        **_proper_scores(y, p),
        "auc": float(roc_auc_score(y, p)) if np.unique(y).size == 2 else None,
        "probability_min": float(np.min(p)), "probability_max": float(np.max(p)),
    }


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    read_gameplan_run(ROOT, RUN)
    plan = json.loads((RUN / "gameplan.json").read_text())
    old_report = json.loads((RUN / "model-reports.json").read_text())["4h"]
    model = joblib.load(RUN / "models/4h/model.joblib")
    samples = pd.read_parquet(ROOT / plan["source_authorities"]["loop_b_run"] / "samples.parquet")
    frozen = pd.Timestamp(plan["frozen_at"])
    sources = _overnight_sources(samples, symbols=plan["symbols"], available_at=frozen)
    bars, paths = _load_equity_minute_bars(ROOT, symbols=plan["symbols"])
    bars = bars.loc[bars.timestamp.lt(frozen)].sort_values(["symbol", "timestamp"])
    group = _build_training_groups(
        samples, sources=sources, feature_columns=model["feature_columns"], minute_bars=bars,
        enforce_boundary_alignment=False,  # Reconstruct the September 4 training cohort.
    )["4h"]
    partitions = _chronological_partitions(group, group="4h")
    assert {name + "_rows": len(part) for name, part in partitions.items()} == old_report["partitions"]
    estimator = model["estimator"]
    base_rate = pd.concat([partitions["train"], partitions["selection"]]).target.mean()
    summary = {"run": str(RUN), "feature_columns": model["feature_columns"],
               "partition_metrics": {}, "calibration_comparisons": {}, "bar_coverage": {}}
    for name, frame in partitions.items():
        matrix = _model_frame(frame, model["feature_columns"], model["categorical_columns"])
        frame["raw"] = estimator.predict_proba(matrix)[:, 1]
        frame["tree"] = estimator.tree.predict_proba(matrix)[:, 1]
        frame["neural"] = estimator.neural.predict_proba(matrix)[:, 1]
        y = frame.target.to_numpy(dtype=int)
        summary["partition_metrics"][name] = {
            "decision_start": str(frame.decision_timestamp.min()),
            "decision_end": str(frame.decision_timestamp.max()),
            "target_start": str(frame.target_window_start.min()),
            "target_end": str(frame.target_window_end.max()),
            "unique_decisions": frame.decision_timestamp.nunique(),
            "unique_sessions": frame.action_date.nunique(),
            "feature_null_fraction": float(matrix.loc[:, model["feature_columns"]].isna().mean().mean()),
            **{key: scores(y, frame[key].to_numpy()) for key in ("raw", "tree", "neural")},
            "saved_calibrated": scores(y, model["calibrator"].predict(frame.raw)),
            "training_base_rate": scores(y, np.full(len(frame), base_rate)),
        }
        frame.to_parquet(OUTPUT / (name + ".parquet"), index=False)
    cal, assessment = partitions["calibration"], partitions["assessment"]
    for method in ("none", "platt", "isotonic"):
        for constrained in ((False, True) if method == "platt" else (True,)):
            calibrator = fit_probability_calibrator(
                method, cal.raw, cal.target, platt_regularization_c=0.1,
                require_nondecreasing=constrained, clip_to_observed_probability_range=True,
            )
            key = method + ("_constrained" if constrained else "_unconstrained")
            summary["calibration_comparisons"][key] = {
                "assessment": scores(assessment.target.to_numpy(), calibrator.predict(assessment.raw)),
                "slope": float(calibrator.model.coef_[0, 0]) if method == "platt" else None,
            }
    summary["by_route"] = {}
    summary["by_symbol"] = {}
    for column in ("route", "symbol"):
        for key, cohort in cal.groupby(column):
            later = assessment.loc[assessment[column].eq(key)]
            fit = fit_probability_calibrator("platt", cohort.raw, cohort.target, platt_regularization_c=0.1)
            summary["by_" + column][str(key)] = {
                "calibration_raw": scores(cohort.target.to_numpy(), cohort.raw),
                "assessment_raw": scores(later.target.to_numpy(), later.raw),
                "unconstrained_calibration_slope": float(fit.model.coef_[0, 0]),
            }
    for name, component in (("tree", estimator.tree), ("neural", estimator.neural)):
        classifier = component.named_steps["classifier"]
        summary[name + "_fit"] = {
            "classes": classifier.classes_.tolist(),
            "iterations": int(classifier.n_iter_),
            "loss": getattr(classifier, "loss_", None),
            "best_validation_score": getattr(classifier, "best_validation_score_", None),
        }
    bars["local_date"] = bars.timestamp.dt.tz_convert("America/Los_Angeles").dt.date
    bars["local_hour"] = bars.timestamp.dt.tz_convert("America/Los_Angeles").dt.hour
    extended = bars.loc[bars.local_hour.between(4, 16)].copy()
    source_lookup = sources.set_index(["symbol", "action_date"])
    duplicates = bars.duplicated(["symbol", "timestamp"], keep=False)
    summary["duplicate_bar_rows"] = int(duplicates.sum())
    duplicate_prices = bars.loc[duplicates].groupby(["symbol", "timestamp"])[["open", "close"]].nunique()
    summary["conflicting_duplicate_bar_timestamps"] = int(duplicate_prices.gt(1).any(axis=1).sum())
    label_mismatches = []
    coverage = []
    for row in group.to_dict("records"):
        same_symbol = extended.loc[extended.symbol.eq(row["symbol"])]
        start, end = row["target_window_start"], row["target_window_end"]
        if row["route"] == "4h@04:00":
            current = same_symbol.loc[same_symbol.local_date.eq(row["action_date"])]
            prior = same_symbol.loc[same_symbol.local_date.lt(row["action_date"])]
            first, last = current.iloc[0], prior.iloc[-1]
            observed = first.open / last.close - 1.0
        else:
            window = same_symbol.loc[same_symbol.timestamp.ge(start) & same_symbol.timestamp.lt(end)]
            first, last = window.iloc[0], window.iloc[-1]
            observed = last.close / first.open - 1.0
            coverage.append({
                "symbol": row["symbol"], "route": row["route"], "date": str(row["action_date"]),
                "entry_lag_minutes": (first.timestamp - start).total_seconds() / 60,
                "exit_gap_minutes": (end - last.timestamp).total_seconds() / 60 - 1,
                "bars": len(window),
            })
        source = source_lookup.loc[(row["symbol"], row["action_date"])]
        cost = source.get("assumed_round_trip_cost", 0.001)
        cost = 0.001 if pd.isna(cost) else cost
        if abs(observed - row["observed_return"]) > 1e-12 or int(observed > cost) != row["target"]:
            label_mismatches.append((row["symbol"], row["route"], str(row["action_date"])))
    coverage = pd.DataFrame(coverage)
    coverage.to_csv(OUTPUT / "window-coverage.csv", index=False)
    summary["label_reconstruction_mismatches"] = label_mismatches
    summary["natural_key_duplicates"] = int(group.duplicated(["symbol", "route", "action_date"]).sum())
    summary["bar_coverage"] = coverage.groupby("route")[["entry_lag_minutes", "exit_gap_minutes", "bars"]].agg(["min", "median", "max"]).to_json()
    summary["rows_with_boundary_gap_over_5_minutes"] = int(coverage[["entry_lag_minutes", "exit_gap_minutes"]].gt(5).any(axis=1).sum())
    summary["original_promotion_gate"] = old_report["promotion_gate"]
    (OUTPUT / "diagnosis.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, default=str), flush=True)


if __name__ == "__main__":
    with threadpool_limits(limits=4):
        main()
