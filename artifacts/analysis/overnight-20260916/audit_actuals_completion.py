"""Bounded saved-artifact actuals audit; no archive reload or outcome recomputation."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

root = Path("C:/DATASTORE")
out = Path(__file__).resolve().parent
actuals = root / "ml/gameplan-actuals-review-runs/20260916T061453.637702Z"
original = root / "ml/nightly-gameplan-runs/20260915T054345.154531Z"
old_plan = root / "ml/gameplan-trade-plan-runs/20260915T054710.644968Z"
successor = root / "ml/nightly-gameplan-runs/20260916T060149.932707Z"
new_plan = root / "ml/gameplan-trade-plan-runs/20260916T060421.076865Z"
resume = root / "ml/overnight-runs/20260916T061451.489725Z"
read = lambda p: json.loads(p.read_text(encoding="utf-8-sig"))
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
evidence = read(out / "actuals-planning-close-diagnosis.json")
weekly = read(out / "weekly-model-diagnosis.json")
before = evidence["saved_source_bindings"]
unchanged = {
    "prior_planning_path": {"path":str(old_plan/"planning-price-path.json"), "before":before["planning_path_sha256"], "after":sha(old_plan/"planning-price-path.json")},
    "prior_trade_plan_manifest": {"path":str(old_plan/"manifest.json"), "before":before["manifest_sha256"], "after":sha(old_plan/"manifest.json")},
    "prior_gameplan_receipt": {"path":str(original/"receipt.json"), "before":before["source_receipt_sha256"], "after":sha(original/"receipt.json")},
}
for run in (original,successor):
    recorded = weekly["runs"][run.name]["artifact_checksums"]["forecasts.parquet"]
    unchanged[run.name+"_forecasts"] = {"path":str(run/"forecasts.parquet"), "before":recorded["sha256"], "after":sha(run/"forecasts.parquet")}
assert all(value["before"] == value["after"] for value in unchanged.values())

receipt, manifest, report = (read(actuals / name) for name in ("receipt.json","manifest.json","report.json"))
assert receipt["status"] == report["status"] == "COMPLETE"
assert receipt["manifest_sha256"] == sha(actuals/"manifest.json")
assert receipt["action_date"] == report["action_date"] == "2026-09-15"
assert report["successor_action_date"] == "2026-09-16"
assert report["source_gameplan_run"] == original.relative_to(root).as_posix()
assert Path(report["source_trade_plan_path"]).resolve() == old_plan
assert report["successor_gameplan_run"] == successor.relative_to(root).as_posix()
assert report["target_price_source_contract"] == "xnas-itch-archive-v1"
assert pd.Timestamp(report["outcomes_through"]) == pd.Timestamp("2026-09-16T00:00Z")
assert pd.Timestamp(report["deadline_at"]) == pd.Timestamp("2026-09-16T11:00Z")
assert report["deadline_exception"] is None
assert all(p["orders_placed"] == 0 and p["broker_orders_enabled"] is False for p in (receipt,report))
outputs = {}
for name, record in manifest["output_files"].items():
    path = actuals / name
    assert path.is_file() and path.stat().st_size == record["size"] and sha(path) == record["checksum_sha256"]
    outputs[name] = {"sha256":sha(path), "size":path.stat().st_size}
bound = {}
for path in [successor/"receipt.json",new_plan/"receipt.json",original/"receipt.json",original/"manifest.json",
             original/"forecasts.parquet",old_plan/"receipt.json",old_plan/"manifest.json",old_plan/"trade-plan.parquet",old_plan/"planning-price-path.json"]:
    records = [item for item in manifest["input_files"] if (root/item["path"]).resolve() == path]
    assert len(records) == 1
    recorded = records[0]
    assert recorded["status"] == "present" and sha(path) == recorded["checksum_sha256"] and path.stat().st_size == recorded["size"]
    bound[str(path)] = sha(path)
latest = read(root/"ml/gameplan-actuals-review-latest/run.json")
dated = root/"ml/gameplan-actuals-review-by-date/2026-09-15"
assert latest == read(dated/"run.json")
assert latest["current"]["run_path"] == actuals.relative_to(root).as_posix()
assert latest["current"]["receipt_sha256"] == sha(actuals/"receipt.json")
assert sha(actuals/"Gameplan-results.md") == sha(dated/"Gameplan-results.md")
assert Path(read(new_plan/"report.json")["previous_session_results_path"]).resolve() == dated/"Gameplan-results.md"
runtime_report, runtime_receipt = read(resume/"stage-report.json"),read(resume/"receipt.json")
assert runtime_report["stage_order"] == ["gameplan_actuals_review"]
assert runtime_report["status"] == runtime_receipt["status"] == "COMPLETE"
assert runtime_receipt["stage_report_checksum_sha256"] == sha(resume/"stage-report.json")
assert runtime_report["enrichment_gameplan"]["receipt_sha256"] == sha(successor/"receipt.json")
assert runtime_report["stages"][0]["exit_code"] == 0

forecasts = pd.read_parquet(original/"forecasts.parquet")
results = pd.read_parquet(actuals/"forecast-results.parquet")
prices = pd.read_parquet(actuals/"price-results.parquet")
identity = ["id","symbol","route","model_group","model_status","target_role","target_window_start","target_window_end","calibrated_probability","direction","target_price_source_contract"]
pd.testing.assert_frame_equal(forecasts[identity].sort_values("id").reset_index(drop=True), results[identity].sort_values("id").reset_index(drop=True), check_dtype=False,check_exact=True)
assert len(results) == 264 and len(prices) == 154
counts = results.actuals_status.value_counts().to_dict()
assert report["forecasts"] == {"total":len(results),"evaluated":counts.get("EVALUATED",0),"mature_awaiting_data":counts.get("MATURE_AWAITING_DATA",0),"pending_maturity":counts.get("PENDING_MATURITY",0)}
assert report["prices"] == prices.comparison_status.value_counts().to_dict()
assert results.loc[~results.actuals_status.eq("EVALUATED") | ~results.direction.isin(["BULLISH","BEARISH"]),"direction_correct"].isna().all()
scored = results.loc[results.direction_correct.notna(),"direction_correct"]
observed = prices.loc[prices.actual_price.notna()].copy()
gap = (pd.to_datetime(observed.timestamp,utc=True)-pd.to_datetime(observed.actual_observed_at,utc=True)).dt.total_seconds()
assert gap.loc[observed.clock_local.eq("17:00")].between(0,300).all()
assert gap.loc[~observed.clock_local.eq("17:00")].between(-300,0).all()
saved_path = read(old_plan/"planning-price-path.json")
for row in prices.itertuples():
    point=saved_path["points"][f"{row.symbol}|2026-09-15|{row.clock_local}"]
    for suffix in ("low","mid","high"):
        expected=point[f"planned_price_{suffix}"]
        actual=getattr(row,f"planned_price_{suffix}")
        assert pd.isna(actual) if expected is None else actual == expected
    if row.comparison_status != "COMPARED":
        assert pd.isna(row.price_error) and pd.isna(row.price_error_fraction) and pd.isna(row.in_planned_range)

summary = {"checked_at":datetime.now(timezone.utc).isoformat(),"status":"VERIFIED_BOUNDED_SAVED_ARTIFACT_AUDIT",
           "archive_loaded_or_outcomes_recomputed":False,"orders_placed":0,
           "actuals_run":str(actuals),"readable_results":str(actuals/"Gameplan-results.md"),
           "resume_run":str(resume),"native_completed_at":runtime_report["completed_at"],
           "prior_action_date":"2026-09-15","successor_action_date":"2026-09-16",
           "unchanged_pre_repair_artifacts":unchanged,"receipt_sha256":sha(actuals/"receipt.json"),
           "manifest_sha256":sha(actuals/"manifest.json"),"outputs":outputs,"bound_saved_inputs":bound,
           "forecast_rows":len(results),"forecast_counts":counts,"price_rows":len(prices),
           "price_counts":prices.comparison_status.value_counts().to_dict(),
           "directional_calls_scored":len(scored),"directional_calls_correct":int(scored.sum()),
           "direction_accuracy":float(scored.mean()),
           "price_comparisons_in_range":int(prices.in_planned_range.fillna(False).sum()),
           "same_clock_actual_boundary_within_300_seconds":True,
           "saved_estimates_preserved_exactly":True,"latest_and_dated_readers_match":True,
           "17_close_results":json.loads(prices.loc[prices.clock_local.eq("17:00"),["symbol","comparison_status","planned_price_mid","actual_price","actual_observed_at","actual_gap_seconds","actual_source_coverage"]].to_json(orient="records",date_format="iso")),
           "missing_price_boundary_statuses":prices.loc[prices.comparison_status.eq("MATURE_AWAITING_DATA"),"actual_status"].value_counts().to_dict(),
           "missing_price_source_coverage":prices.loc[prices.comparison_status.eq("MATURE_AWAITING_DATA"),"actual_source_coverage"].value_counts().to_dict()}
(out/"actuals-completion.json").write_text(json.dumps(summary,indent=2,sort_keys=True)+"\n",encoding="utf-8")
md = f'''# September 15 actuals: completed after scoped recovery

The native actuals-only resume completed at **{runtime_report["completed_at"]}**, exit 0, with the original September 16 04:00 Pacific deadline and pinned successor preserved. The new [readable results](C:/DATASTORE/ml/gameplan-actuals-review-runs/20260916T061453.637702Z/Gameplan-results.md) review September 15 through its 17:00 Pacific close. Overnight orders remain zero.

The prior saved planning path, trade-plan manifest and source receipt match the pre-repair diagnosis hashes. Both prior and successor forecast files match the earlier weekly-diagnosis hashes. No saved estimate, direction or forecast identity changed. New receipt/manifest/output hashes, exact prior/successor input bindings, latest pointer, dated reader and successor results link verified.

- Forecasts: **{len(results)}**, with **{counts.get("EVALUATED",0)} evaluated**, **{counts.get("MATURE_AWAITING_DATA",0)} mature awaiting data**, and **{counts.get("PENDING_MATURITY",0)} pending**.
- Direction: **{int(scored.sum())}/{len(scored)} correct ({float(scored.mean()):.2%})** among mature, observed bullish/bearish calls. Neutral, pending and missing outcomes remain excluded.
- Same-clock prices: **{len(prices)}**, status counts **{prices.comparison_status.value_counts().to_dict()}**; **{summary["price_comparisons_in_range"]}** compared prices inside their original saved range.
- Saved low/mid/high estimates match every original point exactly. All present same-clock actuals retain the correct observation side and native five-minute bound. Unavailable estimates and missing actuals retain null error/range comparisons. The v3 planning-close compatibility repair supplies no actual prices.

The boundary diagnostics classify missing same-clock observations as {summary["missing_price_boundary_statuses"]}; source-window coverage is {summary["missing_price_source_coverage"]}. Complete requested coverage does not establish a trade occurred or permit a synthetic actual. Detailed 17:00 per-symbol coverage and checksums are in [the bounded audit JSON](C:/dev/ducketz/artifacts/analysis/overnight-20260916/actuals-completion.json).

This audit reads only saved small artifacts and checksums; it does not reload the native price archive or duplicate the supervisor's full actuals recomputation. Market price direction accuracy is not broker-fill evidence or realized P/L. No production code, process, claims or publications were changed by this audit.
'''
(out/"actuals-completion.md").write_text(md,encoding="utf-8")
print(json.dumps({k:summary[k] for k in ("status","forecast_counts","price_counts","directional_calls_scored","directional_calls_correct","direction_accuracy","price_comparisons_in_range","missing_price_boundary_statuses","missing_price_source_coverage")},indent=2))
