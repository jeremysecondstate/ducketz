"""Review explicit immutable September 25 models; no fitting or current pointers.

Uses saved cohorts/reports and native read-only validation. Qualification failures
are reported separately from evidence defects; assessment never selects a model.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode = True
REPO = Path("C:/dev/ducketz")
ROOT = Path("C:/DATASTORE")
OUT = Path(__file__).resolve().parent
BASELINE = ROOT / "ml/nightly-gameplan-runs/20260924T062615.050242Z"
NATIVE = ROOT / "ml/overnight-runs/20260925T040822.112032Z"
GROUPS = ("1h", "4h", "1d", "1w")
TARGET = "raw-price-direction-v1"
sys.path.insert(0, str(REPO))

import numpy as np
import pandas as pd
from ml.nightly_gameplan import read_gameplan_run, _chronological_partitions, _verify_probability_cohort
from ml.gameplan_promotion import build_promotion_gate, DIRECTIONAL_PROMOTION_POLICY


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def group_review(run, reports, forecasts, group, previous):
    r = reports[group]
    cohort_name = r.get("deployment", {}).get("retained_cohort_output", f"training-cohort-{group}.parquet")
    frame = pd.read_parquet(run / cohort_name)
    _verify_probability_cohort(frame, TARGET)
    parts = _chronological_partitions(frame, group=group)
    fitted = pd.concat([parts["train"], parts["selection"]], ignore_index=True)
    symbol_counts = fitted.groupby("symbol").size().to_dict()
    route_counts = fitted.groupby(["symbol", "route"]).size().to_dict()
    current = forecasts.loc[forecasts.model_group.eq(group)]
    gate = build_promotion_gate(r["assessment"], r["training_base_rate_assessment"],
        r["calibration_diagnostics"], r["partition_decision_clusters"]["assessment"],
        policy_version=r["promotion_gate"]["policy_version"])
    checks = {
        "archive_feature_contract": r.get("source_selection_contract") == "xnas-archive-prior-session-features-v1" and frame.source_selection_contract.eq("xnas-archive-prior-session-features-v1").all(),
        "raw_direction_target_preserved": r.get("probability_target_contract") == TARGET and r.get("gameplan_variant") == "YG",
        "saved_promotion_arithmetic": gate == r["promotion_gate"],
        "saved_v2_policy": gate["policy_version"] == DIRECTIONAL_PROMOTION_POLICY,
        "no_duplicate_exact_targets": not frame.duplicated(["symbol", "route", "decision_timestamp", "target_window_start", "target_window_end"]).any(),
        "observed_boundaries_within_five_minutes": frame[["target_start_gap_seconds", "target_end_gap_seconds"]].abs().le(300).all().all(),
        "unblended_xnas_source": frame.target_price_source_contract.eq("xnas-itch-archive-v1").all() and frame.target_price_dataset.eq("XNAS.ITCH").all(),
        "returns_match_observed_prices": np.allclose(frame.observed_return, frame.target_close / frame.target_open - 1, rtol=1e-10, atol=1e-12),
    }
    support_errors = []
    for row in current.itertuples():
        recorded = r["target_support_by_symbol"].get(row.symbol, {})
        symbol_count = int(symbol_counts.get(row.symbol, 0))
        route_count = int(route_counts.get((row.symbol, row.route), 0))
        expected_status = gate["status"] if symbol_count and route_count else "RESEARCH_NO_TARGET_HISTORY"
        if (row.symbol_fitted_target_rows != symbol_count or row.symbol_route_fitted_target_rows != route_count
                or recorded.get("fitted_rows", 0) != symbol_count
                or recorded.get("fitted_rows_by_route", {}).get(row.route, 0) != route_count
                or row.model_status != expected_status):
            support_errors.append({"symbol": row.symbol, "route": row.route, "expected_status": expected_status})
    checks["exact_fitted_symbol_route_support_and_status"] = not support_errors
    partitions = {}
    for name, part in parts.items():
        clusters = int(part.decision_timestamp.nunique())
        checks[name + "_counts"] = len(part) == r["partitions"][name + "_rows"] and clusters == r["partition_decision_clusters"][name]
        partitions[name] = {"rows": len(part), "decision_clusters": clusters, "positive_rate": float(part.target.mean()),
            "first_decision": pd.Timestamp(part.decision_timestamp.min()).isoformat(),
            "last_decision": pd.Timestamp(part.decision_timestamp.max()).isoformat(),
            "last_target_end": pd.Timestamp(part.target_window_end.max()).isoformat()}
    for left, right in zip(("train", "selection", "calibration"), ("selection", "calibration", "assessment")):
        label_end = pd.to_datetime(parts[left].target_window_end, utc=True).max()
        checks[left + "_before_" + right + "_target"] = label_end < pd.to_datetime(parts[right].target_window_start, utc=True).min()
        checks[left + "_before_" + right + "_source_cutoff"] = label_end < pd.to_datetime(parts[right].source_effective_cutoff, utc=True).min()
    selection = r["selection_metrics"]
    checks["selected_minimum_development_log_loss"] = selection[r["selected_family"]]["log_loss"] == min(v["log_loss"] for v in selection.values())
    checks["calibration_excludes_assessment_selection"] = r["calibration_selection"]["assessment_used_for_selection"] is False
    support = r["target_support_by_symbol"]
    minimum = current.sort_values(["symbol_route_fitted_target_rows", "symbol", "route"]).iloc[0]
    delta = {metric: float(r["assessment"][metric]) - float(previous["assessment"][metric]) for metric in ("brier_score", "log_loss")}
    excess = {metric: float(r["assessment"][metric]) - float(r["training_base_rate_assessment"][metric]) for metric in ("brier_score", "log_loss")}
    metric_limits = {}
    for metric in ("brier_score", "log_loss"):
        score = Decimal(str(r["assessment"][metric]))
        training_baseline = Decimal(str(r["training_base_rate_assessment"][metric]))
        tolerance = Decimal(str(gate.get("baseline_tolerances", {}).get(metric, 0)))
        maximum = training_baseline + tolerance
        metric_limits[metric] = {"assessment": float(score), "training_rate_baseline": float(training_baseline),
            "allowed_baseline_excess": float(tolerance), "maximum_under_saved_policy": float(maximum),
            "actual_baseline_excess": float(score - training_baseline), "margin_to_maximum": float(maximum - score),
            "strictly_beats_training_baseline": score < training_baseline,
            "inequality": "<=" if gate["policy_version"] == DIRECTIONAL_PROMOTION_POLICY else "<",
            "metric_passes_saved_policy": score <= maximum if gate["policy_version"] == DIRECTIONAL_PROMOTION_POLICY else score < maximum}
    comparison = {"baseline_run": str(BASELINE), "previous_gate": previous["promotion_gate"],
                  "previous_assessment": previous["assessment"], "assessment_delta": delta,
                  "previous_selected_family": previous["selected_family"],
                  "previous_assessment_rows": previous["partitions"]["assessment_rows"],
                  "previous_assessment_clusters": previous["partition_decision_clusters"]["assessment"],
                  "comparison_does_not_select_candidates": True,
                  "different_feature_contract": r.get("source_selection_contract") != previous.get("source_selection_contract"),
                  "cohorts_are_distinct_publications": True,
                  "comparability_note": "Previous publication is the frozen September 24 archive-history baseline; feature-contract identity is compared explicitly. This is a factual cross-publication score comparison, not a controlled same-cohort accuracy experiment."}
    return {"checks": {k: bool(v) for k, v in checks.items()}, "support_errors": support_errors,
        "promotion_gate": gate, "assessment": r["assessment"], "training_rate_baseline": r["training_base_rate_assessment"],
        "fitted_target_rows": len(fitted), "saved_policy_metric_limits": metric_limits,
        "strictly_beats_both_baselines": all(item["strictly_beats_training_baseline"] for item in metric_limits.values()),
        "failed_promotion_checks": [name for name, passed in gate["checks"].items() if not passed],
        "baseline_excess": excess, "selected_family": r["selected_family"],
        "selected_development_metrics": selection[r["selected_family"]], "selection_candidates": len(selection),
        "calibration_selection": r["calibration_selection"], "calibration_diagnostics": r["calibration_diagnostics"],
        "probability_shrinkage_policy": r.get("probability_shrinkage_policy"),
        "selected_probability_shrinkage_weight": r.get("selected_probability_shrinkage_weight"),
        "retained_champion": r.get("deployment"), "fitted_cohort": cohort_name, "partitions": partitions,
        "support_by_symbol": support, "minimum_exact_route_support": {"symbol": minimum.symbol, "route": minimum.route,
            "fitted_rows": int(minimum.symbol_route_fitted_target_rows)},
        "zero_assessment_symbols": [s for s, v in support.items() if not v["assessment_rows"]],
        "forecast_statuses": current.model_status.value_counts().to_dict(),
        "forecast_probability_range": [float(current.calibrated_probability.min()), float(current.calibrated_probability.max())],
        "previous_publication_comparison": comparison}


def enrichment_review(path, gameplan_run):
    from ml.stock_trader.independent_training import independent_model_from_payload, verify_independent_model_sources
    run = path.resolve()
    require(run.parent == (ROOT / "ml/stock-trader-model-runs").resolve(), "Unexpected enrichment path")
    receipt, report, payload, manifest = (read(run / name) for name in ("receipt.json", "training-report.json", "model.json", "manifest.json"))
    for filename, key in (("manifest.json", "manifest_sha256"), ("model.json", "model_sha256"), ("training-report.json", "training_report_sha256")):
        require(sha(run / filename) == receipt[key], "Enrichment receipt hash mismatch: " + filename)
    require(report["source_gameplan_run"] == payload["source_publication"]["source_gameplan_run"] == gameplan_run.relative_to(ROOT).as_posix(),
            "Enrichment belongs to a different publication")
    verify_independent_model_sources(ROOT, payload, manifest)
    model = independent_model_from_payload(payload)
    require(report["orders_placed"] == 0 and report["broker_orders_enabled"] is False, "Enrichment trading authority differs")
    require(report["supported_horizons"] == list(model.supported_horizons)
            and report["qualified_target_contracts"] == list(model.qualified_target_contracts), "Enrichment qualification summary differs")
    groups = {}
    for group in GROUPS:
        r = report["horizons"][group]
        readiness = r.get("scope_readiness", {})
        first = next(iter(readiness.values()), {})
        checks = {}
        if r["status"] == "FITTED":
            checks["calibration_selection_excludes_assessment"] = r["calibration_selection"]["assessment_used_for_selection"] is False
            development = r["head_development_selection"]
            checks["head_selection_excludes_assessment"] = development["assessment_used_for_selection"] is False
            checks["qualified_count_matches_scopes"] = r["qualified_scope_count"] == sum(v["status"] == "READY" for v in readiness.values())
            for head, objective in development["head_objectives"].items():
                candidates = development["candidate_penalties"]
                best = min(candidates, key=lambda penalty: development["candidate_metrics"][str(penalty)][objective])
                checks[head + "_minimum_development_objective"] = best == development["selected_penalties"][head]
        groups[group] = {"fit_status": r["status"], "fitted_scopes": r.get("fitted_scope_count", 0),
            "qualified_scopes": r.get("qualified_scope_count", 0), "reason_counts": dict(Counter(v.get("reason") for v in readiness.values())),
            "horizon_assessment_scores": first.get("horizon_assessment_scores"), "partition_evidence": r.get("partition_evidence"),
            "head_development_selection": r.get("head_development_selection"), "calibration_selection": r.get("calibration_selection"),
            "checks": {k: bool(v) for k, v in checks.items()}, "reason": r.get("reason")}
    return {"status": report["status"], "run": str(run), "source_verified": True,
            "supported_horizons": report["supported_horizons"], "qualified_target_contracts": report["qualified_target_contracts"],
            "feature_contract_version": payload["feature_contract_version"], "scope_qualification_policy": payload["scope_qualification_policy"],
            "groups": groups, "report": str(run / "training-report.json")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gameplan-run", required=True, type=Path)
    parser.add_argument("--enrichment-run", type=Path)
    parser.add_argument("--overnight-run", type=Path, default=NATIVE,
                        help="Current terminal/running native attempt; ancestry must retain this scheduled original run")
    args = parser.parse_args()
    run = args.gameplan_run.resolve()
    require(run.parent == (ROOT / "ml/nightly-gameplan-runs").resolve() and run != BASELINE.resolve(), "Explicit new publication required")
    native_run = args.overnight_run.resolve()
    require(native_run.parent == (ROOT / "ml/overnight-runs").resolve(), "Unexpected native attempt path")
    native = read(native_run / "stage-report.json")
    terminal_receipt_path = native_run / "receipt.json"
    terminal_receipt = read(terminal_receipt_path) if terminal_receipt_path.is_file() else {}
    if native.get("status") != "COMPLETE" or terminal_receipt.get("status") != "COMPLETE":
        print(json.dumps({"status": "NOT_READY_FOR_FINAL_VERIFICATION", "native_run": str(native_run),
            "native_report_status": native.get("status", "MISSING"),
            "native_receipt_status": terminal_receipt.get("status", "MISSING"),
            "heavy_checks_started": False, "orders_placed": 0}))
        return 2
    require(terminal_receipt.get("stage_report_checksum_sha256") == sha(native_run / "stage-report.json")
            and terminal_receipt.get("stage_report_size") == (native_run / "stage-report.json").stat().st_size,
            "Terminal receipt does not bind the completed native report")
    ancestor, seen, chain, completed = native_run, set(), [], set()
    while ancestor is not None:
        require(ancestor.parent == (ROOT / "ml/overnight-runs").resolve() and ancestor not in seen,
                "Invalid native ancestry")
        seen.add(ancestor)
        record = read(ancestor / "stage-report.json")
        require(pd.Timestamp(record["deadline_at"]) == pd.Timestamp("2026-09-25T11:00:00Z")
                and pd.Timestamp(record.get("effective_deadline_at", record["deadline_at"])) == pd.Timestamp("2026-09-25T11:00:00Z")
                and record.get("deadline_exception") is None, "Native original deadline changed")
        require(record.get("probability_target_contract") == TARGET and record.get("gameplan_variant") == "YG"
                and record.get("stock_only") is True and record.get("independent_stock_horizons") is True
                and record.get("stock_price_source") == "xnas-itch-archive-v1"
                and record.get("archive_history") is True, "Native source/target/archive mode changed")
        require(record.get("orders_placed") == 0 and record.get("broker_orders_enabled") is False,
                "Native report changed zero-order authority")
        completed.update(item["stage"] for item in record.get("stages", [])
                         if item.get("status") == "COMPLETE" and item.get("exit_code") == 0)
        chain.append({"run": str(ancestor), "status": record["status"]})
        ancestor = Path(record["resumed_from"]).resolve() if record.get("resumed_from") else None
    require(Path(chain[-1]["run"]) == NATIVE.resolve(), "Native root attempt differs")
    require("gameplan_publication" in completed, "Current publication stage is not yet COMPLETE")
    pin = native.get("enrichment_gameplan") or {}
    require(pin.get("run_path") == run.relative_to(ROOT).as_posix(), "Publication is not this native attempt's pinned Gameplan")
    require(sha(run / "receipt.json") == pin.get("receipt_sha256"), "Pinned receipt hash differs")
    publication = read_gameplan_run(ROOT, run)
    require(publication.receipt["action_date"] == "2026-09-25", "Wrong action date")
    require(pd.Timestamp(publication.receipt["published_at"]) < pd.Timestamp("2026-09-25T11:00:00Z"),
            "Publication missed the unchanged original deadline")
    for item in (publication.receipt, publication.manifest["configuration"]):
        require(item.get("probability_target_contract") == TARGET and item.get("gameplan_variant") == "YG", "YG raw-direction identity changed")
    baseline = read_gameplan_run(ROOT, BASELINE)
    require(baseline.receipt["action_date"] == "2026-09-24", "Baseline date changed")
    old = read(BASELINE / "model-reports.json")
    reports = read(run / "model-reports.json")
    forecasts = pd.read_parquet(run / "forecasts.parquet")
    symbols = publication.manifest["configuration"]["symbols"]
    require(len(forecasts) == len(symbols) * 24 and forecasts.groupby("symbol").size().eq(24).all(), "Forecast grid differs")
    review = {"reviewed_at": datetime.now(timezone.utc).isoformat(), "run": str(run), "action_date": "2026-09-25",
        "native_run": str(native_run), "native_ancestry": chain, "native_status_at_review": native["status"], "full_native_completion_verified": False,
        "scope": "Read-only immutable model reports/cohorts and enrichment validation; no fitting, candidate selection, archive/account/provider reads.",
        "assessment_inference_reproduction": "Directional estimator inference left to the separate final immutable-model verifier.",
        "forecast_rows": len(forecasts), "symbols": symbols, "groups": {}, "errors": [], "coverage_notes": [],
        "enrichment": {"status": "PENDING_EXPLICIT_NEW_ENRICHMENT_RUN"}, "orders_placed": 0}
    for group in GROUPS:
        detail = group_review(run, reports, forecasts, group, old[group])
        review["groups"][group] = detail
        failed = [name for name, passed in detail["checks"].items() if not passed]
        if failed:
            review["errors"].append({"group": group, "failed_checks": failed, "support_errors": detail["support_errors"]})
        if detail["promotion_gate"]["status"] != "PROMOTED":
            review["coverage_notes"].append({"group": group, "status": detail["promotion_gate"]["status"],
                "failed_gate_checks": [k for k, v in detail["promotion_gate"]["checks"].items() if not v]})
    if args.enrichment_run:
        review["enrichment"] = enrichment_review(args.enrichment_run, run)
        for group, detail in review["enrichment"]["groups"].items():
            failed = [name for name, passed in detail["checks"].items() if not passed]
            if failed:
                review["errors"].append({"enrichment_group": group, "failed_checks": failed})
            if not detail["qualified_scopes"]:
                review["coverage_notes"].append({"enrichment_group": group, "fit_status": detail["fit_status"], "qualified_scopes": 0,
                    "reason_counts": detail["reason_counts"]})
    review["status"] = "INVARIANT_FAILURE_REQUIRES_DIAGNOSIS" if review["errors"] else "VERIFIED_SAVED_MODELS_WITH_COVERAGE_NOTES" if review["coverage_notes"] else "VERIFIED_SAVED_DIRECTIONAL_MODELS"
    review["concrete_defect_demonstrated"] = bool(review["errors"])
    review["verifier_sha256"] = sha(Path(__file__))
    review["qualification_summary"] = {
        group: {"fitted_target_rows": detail["fitted_target_rows"],
            "evidence_checks_pass": all(detail["checks"].values()),
            "promotion_status": detail["promotion_gate"]["status"],
            "promotion_policy": detail["promotion_gate"]["policy_version"],
            "strictly_beats_both_baselines": detail["strictly_beats_both_baselines"],
            "metric_limits": detail["saved_policy_metric_limits"],
            "failed_promotion_checks": detail["failed_promotion_checks"],
            "forecast_statuses": detail["forecast_statuses"],
            "minimum_exact_route_support": detail["minimum_exact_route_support"],
            "zero_assessment_symbols": detail["zero_assessment_symbols"],
            "learned_sizing": ({field: review["enrichment"]["groups"][group][field]
                                for field in ("fit_status", "fitted_scopes", "qualified_scopes", "reason_counts")}
                               if args.enrichment_run else {"status": "PENDING_EXPLICIT_NEW_ENRICHMENT_RUN"})}
        for group, detail in review["groups"].items()}
    (OUT / "model-review.json").write_text(json.dumps(review, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    metric_summary = {"reviewed_at": review["reviewed_at"], "run": str(run), "native_run": str(native_run),
        "action_date": "2026-09-25", "status": review["status"], "full_native_completion_verified": False,
        "scope": "Reproduced saved promotion arithmetic; estimator-inference score reproduction is a separate audit.",
        "source_receipt_sha256": sha(run / "receipt.json"), "model_reports_sha256": sha(run / "model-reports.json"),
        "forecasts_sha256": sha(run / "forecasts.parquet"), "groups": review["qualification_summary"],
        "errors": review["errors"], "models_fitted_or_relabelled": False, "orders_placed": 0}
    (OUT / "model-metric-summary.json").write_text(json.dumps(metric_summary, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    lines = ["# September 25 Gameplan model review", "", f"Reviewed {review['reviewed_at']}; immutable publication `{run}`.", "",
        "Saved directional model qualification is separate from learned sizing qualification. This review never fits or selects candidates using assessment outcomes.", "",
        "| Group | Directional status | Brier / baseline | Log loss / baseline | Assessment rows / clusters | Smallest exact fitted route |",
        "| --- | --- | --- | --- | --- | --- |"]
    for group, item in review["groups"].items():
        a, b, p, m = item["assessment"], item["training_rate_baseline"], item["partitions"]["assessment"], item["minimum_exact_route_support"]
        lines.append(f"| {group} | {item['promotion_gate']['status']} | {a['brier_score']:.9f} / {b['brier_score']:.9f} | {a['log_loss']:.9f} / {b['log_loss']:.9f} | {p['rows']} / {p['decision_clusters']} | {m['symbol']} {m['route']}: {m['fitted_rows']} |")
    lines += ["", "Exact fitted symbol/route counts, saved score/gate arithmetic, development-only selection, observed price boundaries and chronological partitions are recorded in the JSON. Each group includes a factual comparison with the frozen September 24 publication; score changes do not select candidates. Qualification within the saved v2 tolerances does not establish baseline outperformance.", ""]
    lines += ["| Group | Brier excess / allowed | Log-loss excess / allowed | Strictly beats both baselines | Failed promotion checks |",
              "| --- | ---: | ---: | --- | --- |"]
    for group, item in review["groups"].items():
        b, l = item["saved_policy_metric_limits"]["brier_score"], item["saved_policy_metric_limits"]["log_loss"]
        failures = ", ".join(item["failed_promotion_checks"]) or "None"
        lines.append(f"| {group} | {b['actual_baseline_excess']:.9f} / {b['allowed_baseline_excess']:.3f} | {l['actual_baseline_excess']:.9f} / {l['allowed_baseline_excess']:.3f} | {item['strictly_beats_both_baselines']} | {failures} |")
    lines += ["", "The JSON records each saved-policy maximum and margin, separate from strict baseline wins. These are factual assessment results and never a candidate-selection rule.", ""]
    if args.enrichment_run:
        lines += ["| Group | Learned model fit | Fitted scopes | Qualified scopes |", "| --- | --- | ---: | ---: |"]
        for group, item in review["enrichment"]["groups"].items():
            lines.append(f"| {group} | {item['fit_status']} | {item['fitted_scopes']} | {item['qualified_scopes']} |")
        lines += ["", f"Enrichment source: `{review['enrichment']['run']}`. Source cohort binding and native model validity verified; research-only scopes retain their recorded reasons and all execution gates."]
    else:
        lines += ["Enrichment review is pending its explicit new immutable run path; no latest pointer was followed."]
    lines += ["", f"Review status: {review['status']}. Evidence errors: {len(review['errors'])}. Full overnight completion is outside this model review."]
    (OUT / "model-review.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"status": review["status"], "run": str(run), "directional": {g: r["promotion_gate"]["status"] for g, r in review["groups"].items()},
        "enrichment": review["enrichment"]["status"], "errors": review["errors"], "report": str(OUT / "model-review.md")}))
    return 1 if review["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

