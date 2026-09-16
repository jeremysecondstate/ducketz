"""Read saved weekly cohorts/reports; no fitting, providers or publication writes."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, "C:/dev/ducketz")
import pandas as pd
from ml.nightly_gameplan import _chronological_partitions

base = Path("C:/DATASTORE/ml/nightly-gameplan-runs")
run_names = ["20260915T054345.154531Z", "20260916T060149.932707Z"]
out = Path(__file__).resolve().parent
identity = ["symbol", "route", "target_window_start", "target_window_end"]
def hashfile(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
def records(frame):
    fields = [*identity, "decision_timestamp", "target", "observed_return"]
    return json.loads(frame[fields].to_json(orient="records", date_format="iso"))
def describe(frame):
    return {"rows": len(frame), "decision_clusters": int(frame.decision_timestamp.nunique()),
            "decision_first": str(frame.decision_timestamp.min()), "decision_last": str(frame.decision_timestamp.max()),
            "target_first_start": str(frame.target_window_start.min()), "target_last_end": str(frame.target_window_end.max()),
            "classes": frame.target.value_counts().sort_index().to_dict(),
            "symbols": frame.symbol.value_counts().sort_index().to_dict()}
def subset_delta(left, right):
    l, r = left.set_index(identity), right.set_index(identity)
    added, removed = r.loc[r.index.difference(l.index)].reset_index(), l.loc[l.index.difference(r.index)].reset_index()
    return {"added_count": len(added), "removed_count": len(removed),
            "added": records(added), "removed": records(removed)}

reports, frames, partitions, calibrations = {}, {}, {}, {}
result = {"observed_at": datetime.now(timezone.utc).isoformat(), "read_only": True,
          "training_performed": False, "assessment_used_to_select_or_modify_candidate": False, "runs": {}}
for name in run_names:
    run = base / name
    report = json.loads((run / "model-reports.json").read_text())
    reports[name] = report
    manifest = json.loads((run / "manifest.json").read_text())
    receipt = json.loads((run / "receipt.json").read_text())
    evidence = {}
    for file in ["model-reports.json", "training-cohort-1w.parquet", "forecasts.parquet", "models/1w/model.joblib"]:
        path = run / file
        record = manifest["output_files"][file]
        digest = hashfile(path)
        assert digest == record["checksum_sha256"] and path.stat().st_size == record["size"]
        evidence[file] = {"path": str(path), "sha256": digest, "size": path.stat().st_size}
    digest = hashfile(run / "manifest.json")
    assert digest == receipt.get("manifest_sha256", receipt.get("manifest_checksum_sha256"))
    frame = pd.read_parquet(run / "training-cohort-1w.parquet")
    assert not frame.duplicated(identity).any()
    assert frame.target_boundary_aligned.all()
    assert frame.target_start_gap_seconds.between(0, 300).all() and frame.target_end_gap_seconds.between(0, 300).all()
    assert frame.target_price_source_contract.eq("xnas-itch-archive-v1").all()
    assert frame.target_price_dataset.eq("XNAS.ITCH").all()
    assert frame.target.eq(frame.observed_return.gt(frame.assumed_round_trip_cost).astype(int)).all()
    parts = _chronological_partitions(frame, group="1w")
    assert {key+"_rows":len(value) for key,value in parts.items()} == report["1w"]["partitions"]
    boundaries = {}
    for left, right in zip(("train", "selection", "calibration"), ("selection", "calibration", "assessment")):
        bad = parts[left].loc[parts[left].target_window_end.ge(parts[right].decision_timestamp.min())]
        boundaries[left+"_to_"+right] = {"labels_not_complete_at_first_right_decision": len(bad), "rows": records(bad)}
    calibration = parts["calibration"]
    clusters = pd.Index(calibration.decision_timestamp.unique()).sort_values()
    midpoint = len(clusters)//2
    fit = calibration.loc[calibration.decision_timestamp.isin(clusters[:midpoint])].copy()
    validation = calibration.loc[calibration.decision_timestamp.isin(clusters[midpoint:])].copy()
    fit = fit.loc[fit.target_window_end.lt(validation.decision_timestamp.min())]
    assert len(fit) == report["1w"]["calibration_selection"]["fit_rows"]
    assert len(validation) == report["1w"]["calibration_selection"]["validation_rows"]
    forecasts = pd.read_parquet(run / "forecasts.parquet")
    weekly = forecasts.loc[forecasts.model_group.eq("1w")]
    result["runs"][name] = {
        "artifact_checksums": evidence, "manifest_sha256": digest,
        "groups": {group:{"family": value["selected_family"], "gate": value["promotion_gate"],
                          "assessment": value["assessment"], "baseline": value["training_base_rate_assessment"]}
                   for group,value in report.items()},
        "weekly": {key:report["1w"].get(key) for key in ("selected_family", "partitions", "partition_decision_clusters", "selection_metrics", "calibration_selection", "calibration_diagnostics", "assessment", "assessment_raw_scores", "training_base_rate_assessment", "promotion_gate", "target_boundary_quality", "target_support_by_symbol")},
        "cohort": describe(frame), "partition_details": {key:describe(value) for key,value in parts.items()},
        "partition_information_timing": boundaries,
        "calibrator_fit": describe(fit), "calibrator_validation": describe(validation),
        "weekly_forecasts": json.loads(weekly[["symbol","route","model_status","direction","execution_eligible","calibrated_probability"]].to_json(orient="records")),
        "forecast_status_counts": {"/".join(map(str,key)):int(value) for key,value in forecasts.groupby(["model_group","model_status","execution_eligible","direction"]).size().items()},
    }
    frames[name], partitions[name], calibrations[name] = frame, parts, {"fit":fit,"validation":validation}
left, right = run_names
common_left, common_right = frames[left].set_index(identity), frames[right].set_index(identity)
common = common_left.index.intersection(common_right.index)
a,b = common_left.loc[common].sort_index(), common_right.loc[common].sort_index()
changes = ~(a.eq(b) | (a.isna() & b.isna()))
result["cohort_change"] = {**subset_delta(frames[left],frames[right]),
    "common_rows":len(common), "changed_common_cells":int(changes.sum().sum()),
    "changed_common_fields":{key:int(value) for key,value in changes.sum().items() if value}}
result["partition_changes"] = {key:subset_delta(partitions[left][key],partitions[right][key]) for key in partitions[left]}
result["calibrator_changes"] = {key:subset_delta(calibrations[left][key],calibrations[right][key]) for key in ("fit","validation")}
from ml.stock_trader.gameplan_execution import execution_frame, load_execution_signals
execution_rows, execution_run = execution_frame(Path("C:/DATASTORE"), action_date="2026-09-16")
assert execution_run == base / right
signals, _ = load_execution_signals(Path("C:/DATASTORE"), as_of="2026-09-16T11:01:00Z")
result["manual_instruction_read"] = {
    "inspection_only_as_of": "2026-09-16T11:01:00Z", "run": str(execution_run),
    "saved_execution_rows": len(execution_rows), "opening_signal_count": len(signals),
    "weekly_opening_signals": {symbol: {"probability": signal.calibrated_probability,
                                        "target_end": signal.target_window_end}
                               for (symbol,horizon),signal in signals.items() if horizon == "1w"},
    "broker_or_runtime_invoked": False, "orders_placed": 0,
    "interpretation": "Manual selected policy consumes saved research-status weekly instructions; actual account, quote, ownership, activation and deduplication checks still govern any order.",
}
enrichment_path = Path("C:/DATASTORE/ml/stock-trader-model-runs/20260916T060336.201575Z/training-report.json")
enrichment = json.loads(enrichment_path.read_text())
result["enrichment"] = {"report":str(enrichment_path), "sha256":hashfile(enrichment_path),
    "horizons":{key:{field:value.get(field) for field in ("status","fitted_scope_count","qualified_scope_count","diagnostic_scope_count","symbols_without_targets")}
                for key,value in enrichment["horizons"].items()}}
result["conclusion"] = {
    "concrete_training_or_data_defect_found": False,
    "immediate_retrain_justified": False,
    "classification": "Expected rolling development sample sensitivity and valid held-out rejection",
    "calibration_change": "Six positive fit rows leave the rolling calibration partition; identity then wins the unchanged later development set.",
    "champion_retention": "latest-compatible-promoted-same-action-date-v1 excludes the September 15 action-date model from September 16.",
    "approved_action": "Preserve immutable reports, model status and current execution policy; no unchanged retry or assessment-driven calibration substitution.",
    "potential_future_research": "Prospectively preregister calibration stability work on development-only data with an untouched future assessment; current evidence does not identify a concrete corrective patch.",
}
path = out / "weekly-model-diagnosis.json"
path.write_text(json.dumps(result, indent=2, sort_keys=True, default=str)+"\n",encoding="utf-8")
print(json.dumps({"evidence":str(path),"cohort_change":result["cohort_change"],
                 "calibrator_changes":result["calibrator_changes"],
                 "timing":{name:result["runs"][name]["partition_information_timing"] for name in run_names}},indent=2))
