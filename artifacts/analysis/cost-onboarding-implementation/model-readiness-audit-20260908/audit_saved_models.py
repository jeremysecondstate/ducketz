"""Read-only reconstruction of one immutable Gameplan; never fits or calls providers."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
import joblib
import numpy as np
import pandas as pd
from ml.independent_stock_targets import build_stock_training_groups
from ml.nightly_gameplan import (
    _ProbabilityBlend, _chronological_partitions, _model_frame,
    _overnight_sources, _proper_scores,
)

DATA = Path(r"C:\DATASTORE")
RUN = DATA / "ml/nightly-gameplan-runs/20260908T065643.819336Z"
OUT = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def records_by(frame, key):
    return {str(k): {"rows": len(v), "positive_rate": float(v.target.mean())}
            for k, v in frame.groupby(key, sort=True)}


def main():
    manifest = json.loads((RUN / "manifest.json").read_text())
    reports = json.loads((RUN / "model-reports.json").read_text())
    symbols = manifest["configuration"]["symbols"]
    selected = [item for item in manifest["input_files"] if item["path"].endswith("samples.parquet")
                or "\\bars\\1m\\databento\\normalized\\" in item["path"]]
    verified = []
    frames = []
    for item in selected:
        path = DATA / item["path"]
        actual = digest(path)
        assert actual == item["checksum_sha256"], f"Immutable input changed: {path}"
        verified.append({"path": str(path), "sha256": actual})
        if path.name == "samples.parquet":
            samples = pd.read_parquet(path)
        else:
            bars = pd.read_parquet(path, columns=["timestamp", "open", "close"])
            bars["symbol"] = path.name.split("_", 1)[0]
            frames.append(bars)
    minute_bars = pd.concat(frames, ignore_index=True)
    features = manifest["feature_columns"]
    available_at = pd.Timestamp(manifest["run_timestamp"])
    sources = _overnight_sources(samples, symbols=symbols, available_at=available_at)
    groups = build_stock_training_groups(sources, feature_columns=features,
                                         minute_bars=minute_bars, available_at=available_at)
    result = {
        "audited_at": datetime.now(timezone.utc).isoformat(),
        "immutable_run": str(RUN), "run_manifest_sha256": digest(RUN / "manifest.json"),
        "operations": {"fits": 0, "provider_calls": 0, "broker_calls": 0, "model_changes": 0},
        "source_inputs_verified": verified,
        "audit_source_sha256": {name: digest(ROOT / name) for name in (
            "ml/nightly_gameplan.py", "ml/independent_stock_targets.py", "ml/calibration.py")},
        "source_rows": len(sources), "manifest_feature_count": len(features),
        "groups": {},
    }
    for group, frame in groups.items():
        report = reports[group]
        partitions = _chronological_partitions(frame, group=group)
        model_relative = report["model_file"]["path"]
        model_path = RUN / model_relative
        assert digest(model_path) == manifest["output_files"][model_relative]["checksum_sha256"]
        # The original CLI pickle names its blend class __main__._ProbabilityBlend.
        # The explicit imported alias above is inspection compatibility only.
        bundle = joblib.load(model_path)
        partition_reports = {}
        for name, part in partitions.items():
            assert len(part) == report["partitions"][f"{name}_rows"], (group, name)
            partition_reports[name] = {
                "rows": len(part), "decision_clusters": int(part.decision_timestamp.nunique()),
                "positive_rate": float(part.target.mean()),
                "earliest_decision": part.decision_timestamp.min().isoformat(),
                "latest_decision": part.decision_timestamp.max().isoformat(),
                "earliest_target_start": part.target_window_start.min().isoformat(),
                "latest_target_end": part.target_window_end.max().isoformat(),
                "unique_symbol_target_windows": len(part.drop_duplicates(["symbol", "target_window_start", "target_window_end"])),
                "by_symbol": records_by(part, "symbol"), "by_route": records_by(part, "route"),
            }
        boundaries = []
        for left_name, right_name in zip(("train", "selection", "calibration"), ("selection", "calibration", "assessment")):
            left, right = partitions[left_name], partitions[right_name]
            right_decision = right.decision_timestamp.min()
            right_target = right.target_window_start.min()
            ends = pd.to_datetime(left.observed_close_timestamp, utc=True)
            violating = left.loc[ends.ge(right_decision)]
            boundaries.append({
                "left": left_name, "right": right_name,
                "right_first_decision": right_decision.isoformat(),
                "right_first_target_start": right_target.isoformat(),
                "left_labels_not_strictly_before_next_first_decision": len(violating),
                "left_labels_after_next_first_decision": int(ends.gt(right_decision).sum()),
                "left_labels_not_before_next_first_target": int(ends.ge(right_target).sum()),
                "examples": violating.loc[:, ["symbol", "route", "decision_timestamp", "observed_close_timestamp"]].head(8).astype(str).to_dict("records"),
            })
        train = partitions["train"]
        feature_support = []
        for feature in features:
            values = pd.to_numeric(train[feature], errors="coerce")
            feature_support.append({"feature": feature, "nonnull_rows": int(values.notna().sum()),
                                    "distinct_values": int(values.nunique()),
                                    "admitted": feature in bundle["feature_columns"]})
        part = partitions["assessment"]
        matrix = _model_frame(part, bundle["feature_columns"], bundle["categorical_columns"])
        raw = bundle["estimator"].predict_proba(matrix)[:, 1]
        calibrated = bundle["calibrator"].predict(raw)
        recalculated = _proper_scores(part.target.astype(int).to_numpy(), calibrated)
        raw_scores = _proper_scores(part.target.astype(int).to_numpy(), raw)
        assert all(np.isclose(recalculated[k], v, rtol=1e-10, atol=1e-12)
                   for k, v in report["assessment"].items()), group
        assert all(np.isclose(raw_scores[k], v, rtol=1e-10, atol=1e-12)
                   for k, v in report["assessment_raw_scores"].items()), group
        result["groups"][group] = {
            "reconstruction_matches_saved_row_counts_and_scores": True,
            "target_boundary_quality": frame.attrs["target_boundary_quality"],
            "selected_family": report["selected_family"],
            "partitions": partition_reports, "partition_boundary_audit": boundaries,
            "target_support_by_symbol": report["target_support_by_symbol"],
            "feature_support": feature_support,
            "admitted_feature_count": len(bundle["feature_columns"]),
            "all_null_training_features": [r["feature"] for r in feature_support if r["nonnull_rows"] == 0],
            "nonvarying_training_features": [r["feature"] for r in feature_support if r["nonnull_rows"] and r["distinct_values"] <= 1],
            "expected_routes_without_fitted_symbol_history": {
                symbol: [route for route in sorted(frame.route.unique())
                         if report["target_support_by_symbol"][symbol]["fitted_rows_by_route"].get(route, 0) == 0]
                for symbol in symbols
            },
            "assessment": recalculated, "assessment_raw_scores": raw_scores,
            "training_base_rate_assessment": report["training_base_rate_assessment"],
            "calibration_diagnostics": report["calibration_diagnostics"],
            "promotion_gate": report["promotion_gate"],
        }
    out = OUT / "evidence.json"
    out.write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(json.dumps({"output": str(out), "groups": {group: {
        "matches_saved": data["reconstruction_matches_saved_row_counts_and_scores"],
        "all_null_features": len(data["all_null_training_features"]),
        "nonvarying_features": len(data["nonvarying_training_features"]),
        "boundary_audit": data["partition_boundary_audit"],
    } for group, data in result["groups"].items()}}, indent=2))


if __name__ == "__main__":
    main()
