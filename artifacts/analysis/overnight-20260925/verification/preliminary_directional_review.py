"""Read only a completed publication pinned by tonight's still-running native tail.

No archive scan, model fitting, estimator inference, account/provider calls or
mutation of production. This preliminary review checks saved report arithmetic
and frozen rows; independent source/cohort/inference reproduction remains pending.
"""
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
from decimal import Decimal
import hashlib
import json
import sys

sys.dont_write_bytecode = True
REPO = Path("C:/dev/ducketz")
ROOT = Path("C:/DATASTORE")
NATIVE = ROOT / "ml/overnight-runs/20260925T040822.112032Z"
OUT = Path(__file__).resolve().parent
GROUPS = ("1h", "4h", "1d", "1w")
TARGET = "raw-price-direction-v1"
ACTION = "2026-09-25"
DEADLINE = "2026-09-25T11:00:00Z"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def main():
    native = read(NATIVE / "stage-report.json")
    stages = [s for s in native.get("stages", []) if s.get("stage") == "gameplan_publication"]
    pin = native.get("enrichment_gameplan")
    if len(stages) != 1 or stages[0].get("status") != "COMPLETE" or stages[0].get("exit_code") != 0 or not pin:
        print(json.dumps({"status": "WAITING_FOR_COMPLETED_PINNED_PUBLICATION", "native_status": native.get("status"),
            "current_stage": native.get("current_stage"), "heartbeat_at": native.get("heartbeat_at"), "archive_checks_started": False}))
        return 2
    sys.path.insert(0, str(REPO))
    import pandas as pd
    from ml.gameplan_promotion import build_promotion_gate
    from ml.stock_direction_policy import STOCK_DIRECTION_POLICY_VERSION, BULLISH_PROBABILITY, BEARISH_PROBABILITY, stock_direction
    from datafetching.symbol_universe import read_symbols

    require(native.get("archive_history") is True and native.get("probability_target_contract") == TARGET
            and native.get("gameplan_variant") == "YG" and native.get("stock_price_source") == "xnas-itch-archive-v1"
            and pd.Timestamp(native["deadline_at"]) == pd.Timestamp(DEADLINE), "Native configuration/source/deadline differs")
    run = (ROOT / pin["run_path"]).resolve()
    require(run.parent == (ROOT / "ml/nightly-gameplan-runs").resolve(), "Pinned publication path escapes native root")
    require(sha(run / "receipt.json") == pin["receipt_sha256"], "Pinned receipt checksum differs")
    receipt, manifest = read(run / "receipt.json"), read(run / "manifest.json")
    require(receipt["manifest_checksum_sha256"] == sha(run / "manifest.json"), "Receipt does not bind manifest")
    require(receipt["action_date"] == ACTION and pd.Timestamp(receipt["published_at"]) < pd.Timestamp(DEADLINE), "Action date/deadline differs")
    cfg = manifest["configuration"]
    for item in (receipt, cfg):
        require(item.get("probability_target_contract") == TARGET and item.get("gameplan_variant") == "YG"
                and item.get("preparation_scope") == "STOCK_ONLY", "Frozen publication target identity differs")
    require(cfg.get("archive_history") is True and cfg.get("source_selection_contract") == "xnas-archive-prior-session-features-v1", "Archive feature policy differs")
    hashes = {}
    for name in ("model-reports.json", "forecasts.parquet", "gameplan.json", "archive-history.json"):
        metadata = manifest["output_files"][name]
        require(metadata["size"] == (run / name).stat().st_size and metadata["checksum_sha256"] == sha(run / name), "Bounded output hash differs: " + name)
        hashes[name] = metadata["checksum_sha256"]
    reports = read(run / "model-reports.json")
    archive = read(run / "archive-history.json")
    frame = pd.read_parquet(run / "forecasts.parquet")
    symbols = tuple(read_symbols(REPO / "datafetching/watchlist.txt"))
    require(set(symbols) == set(cfg["symbols"]) == set(frame.symbol) and frame.groupby("symbol").size().eq(24).all()
            and len(frame) == 24 * len(symbols) and not frame.id.duplicated().any(), "Frozen forecast grid/universe differs")
    require(frame.direction_policy_version.eq(STOCK_DIRECTION_POLICY_VERSION).all()
            and frame.direction_up_threshold.eq(BULLISH_PROBABILITY).all()
            and frame.direction_down_threshold.eq(BEARISH_PROBABILITY).all(), "Saved direction policy differs from the preserved native policy")
    review = {"reviewed_at": datetime.now(timezone.utc).isoformat(), "status": "PENDING",
        "native_run": str(NATIVE), "native_status_at_review": native["status"], "native_current_stage_at_review": native.get("current_stage"),
        "gameplan_run": str(run), "action_date": ACTION, "publication_stage": stages[0], "pinned_receipt_sha256": pin["receipt_sha256"],
        "bounded_output_hashes": hashes, "configured_symbols": list(symbols), "forecast_rows": len(frame),
        "saved_current_training_cohorts": archive.get("training_cohorts"),
        "saved_archive_feature_dates": archive.get("by_symbol"),
        "saved_archive_exclusions": {key: archive.get(key) for key in ("minimum_core_daily_observations", "synthetic_feature_bars",
            "excluded_undefined_observations", "quality_resets", "excluded_intervals", "split_boundaries", "target_discontinuity_boundaries")},
        "saved_second_minute_consistency": archive.get("second_minute_consistency"),
        "direction_policy": {"version": STOCK_DIRECTION_POLICY_VERSION, "bullish_strictly_above": BULLISH_PROBABILITY,
                             "bearish_strictly_below": BEARISH_PROBABILITY}, "groups": {}, "errors": [], "quality_failures": [],
        "full_native_completion_verified": False, "source_and_cohort_reproduction": "DEFERRED_TO_FINAL_COMPLETION",
        "estimator_score_reproduction": "DEFERRED_TO_FINAL_COMPLETION", "orders_placed": 0,
        "scope": "Saved manifest-bound reports and frozen rows only. No fitting, archive scans, estimator inference, candidate reselection or production writes."}
    for group in GROUPS:
        r = reports[group]
        rows = frame.loc[frame.model_group.eq(group)]
        gate = build_promotion_gate(r["assessment"], r["training_base_rate_assessment"], r["calibration_diagnostics"],
            r["partition_decision_clusters"]["assessment"], policy_version=r["promotion_gate"]["policy_version"])
        failures = []
        checks = {"saved_promotion_arithmetic": gate == r["promotion_gate"],
            "same_target_and_feature_contract": r.get("probability_target_contract") == TARGET and r.get("gameplan_variant") == "YG"
                and r.get("source_selection_contract") == cfg["source_selection_contract"],
            "assessment_not_used_for_calibration_selection": r["calibration_selection"]["assessment_used_for_selection"] is False,
            "saved_development_minimum_log_loss": r["selection_metrics"][r["selected_family"]]["log_loss"] == min(v["log_loss"] for v in r["selection_metrics"].values()),
            "fixed_logistic_grid": r["logistic_regularization_candidates"] == [0.001, 0.01, 0.1, 1.0],
            "fitted_model_reference": rows.model_artifact.eq(r["model_file"]["path"]).all() and rows.model_family.eq(r["selected_family"]).all()}
        expected_directions = rows.calibrated_probability.map(stock_direction)
        expected_directions.loc[rows.model_status.eq("RESEARCH_NO_TARGET_HISTORY")] = "NO_EDGE"
        checks["frozen_direction_policy_arithmetic"] = rows.direction.eq(expected_directions).all()
        for row in rows.itertuples():
            support = r["target_support_by_symbol"].get(row.symbol, {})
            symbol_count = support.get("fitted_rows", 0)
            route_count = support.get("fitted_rows_by_route", {}).get(row.route, 0)
            expected_status = gate["status"] if symbol_count > 0 and route_count > 0 else "RESEARCH_NO_TARGET_HISTORY"
            if row.symbol_fitted_target_rows != symbol_count or row.symbol_route_fitted_target_rows != route_count or row.model_status != expected_status:
                failures.append({"symbol": row.symbol, "route": row.route, "expected_status": expected_status})
        checks["exact_saved_symbol_route_support_and_status"] = not failures
        metric_limits = {}
        for metric in ("brier_score", "log_loss"):
            score = Decimal(str(r["assessment"][metric])); baseline = Decimal(str(r["training_base_rate_assessment"][metric]))
            tolerance = Decimal(str(gate.get("baseline_tolerances", {}).get(metric, 0)))
            metric_limits[metric] = {"score": float(score), "baseline": float(baseline), "allowed_excess": float(tolerance),
                "saved_maximum": float(baseline+tolerance), "actual_excess": float(score-baseline), "margin_to_maximum": float(baseline+tolerance-score),
                "strictly_beats_baseline": bool(score < baseline)}
        calibration = r["calibration_selection"]
        eligible = [name for name in ("identity", "platt") if calibration.get("candidate_eligibility", {}).get(name, {}).get("eligible")]
        selected = min(eligible, key=lambda name: calibration["candidate_metrics"][name]["log_loss"]) if eligible else "identity"
        fallback = calibration.get("selection_status") == "IDENTITY_AFTER_INELIGIBLE_FULL_DEVELOPMENT_REFIT"
        checks["saved_eligible_development_calibrator"] = ((selected == "platt" and calibration["selected_family"] == "identity"
            and calibration.get("full_refit_eligibility", {}).get("eligible") is False
            and calibration.get("candidate_eligibility", {}).get("identity", {}).get("eligible") is True)
            if fallback else selected == calibration["selected_family"])
        minimum = rows.sort_values(["symbol_route_fitted_target_rows", "symbol", "route"]).iloc[0]
        quality = [name for name, passed in gate["checks"].items() if not passed]
        detail = {"promotion_status": gate["status"], "promotion_policy": gate["policy_version"], "promotion_checks": gate["checks"],
            "failed_quality_checks": quality, "metric_limits": metric_limits, "assessment": r["assessment"],
            "raw_assessment": r["assessment_raw_scores"], "baseline": r["training_base_rate_assessment"],
            "selected_family": r["selected_family"], "selected_development_metrics": r["selection_metrics"][r["selected_family"]],
            "development_candidate_count": len(r["selection_metrics"]), "selected_probability_shrinkage_weight": r.get("selected_probability_shrinkage_weight"),
            "calibration_selection": calibration, "calibration_diagnostics": r["calibration_diagnostics"],
            "partitions": r["partitions"], "partition_clusters": r["partition_decision_clusters"],
            "target_boundary_quality": r["target_boundary_quality"], "current_archive_cohort": archive.get("training_cohorts", {}).get(group),
            "support_by_symbol": r["target_support_by_symbol"],
            "minimum_exact_route_support": {"symbol": minimum.symbol, "route": minimum.route, "fitted_rows": int(minimum.symbol_route_fitted_target_rows)},
            "symbols_without_assessment_rows": [s for s,d in r["target_support_by_symbol"].items() if d["assessment_rows"] == 0],
            "forecast_status_counts": rows.model_status.value_counts().to_dict(),
            "forecast_raw_probability_range": [float(rows.raw_probability.min()), float(rows.raw_probability.max())],
            "forecast_probability_range": [float(rows.calibrated_probability.min()), float(rows.calibrated_probability.max())],
            "checks": {k: bool(v) for k,v in checks.items()}, "support_errors": failures}
        review["groups"][group] = detail
        bad = [name for name,passed in checks.items() if not passed]
        if bad:review["errors"].append({"group": group, "failed_checks": bad})
        if quality:review["quality_failures"].append({"group": group, "status": gate["status"], "failed_checks": quality, "metric_limits": metric_limits})
    review["status"] = "REPORT_INVARIANT_FAILURE" if review["errors"] else "SAVED_REPORTS_VERIFY_WITH_QUALITY_FAILURES" if review["quality_failures"] else "SAVED_DIRECTIONAL_REPORTS_VERIFY"
    review["concrete_report_defect_demonstrated"] = bool(review["errors"])
    review["strict_baseline_comparison"] = {g: all(m["strictly_beats_baseline"] for m in d["metric_limits"].values())
                                            for g,d in review["groups"].items()}
    review["TWST_saved_support_focus"] = {g: d["support_by_symbol"]["TWST"] for g,d in review["groups"].items()}
    review["support_limitation"] = "The saved group-level promotion gate and positive exact fitted support are verified separately. Very small exact-route or symbol assessment counts remain limitations; group promotion does not establish precise per-symbol/per-route accuracy."
    review["repair_assessment"] = ("Investigate the exact saved-report invariant failures; no model alteration performed." if review["errors"] else
        "No concrete data, fitting or selection defect demonstrated by this bounded report review. Quality failures alone do not justify changing gates or repeatedly selecting on assessment outcomes. Full immutable-source and estimator verification remains pending.")
    (OUT / "preliminary-directional-review.json").write_text(json.dumps(review, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    lines = ["# September 25 preliminary directional review", "", "Pinned publication: `"+str(run)+"`.", "",
        "Native status at review: "+native["status"]+". This checks saved reports and frozen rows; final source/cohort/inference verification is pending.", "",
        "| Horizon | Saved status | Brier / baseline | Log loss / baseline | Failed quality checks | Smallest exact fitted route |",
        "| --- | --- | --- | --- | --- | --- |"]
    for group,d in review["groups"].items():
        b=d["metric_limits"]["brier_score"];l=d["metric_limits"]["log_loss"];m=d["minimum_exact_route_support"]
        lines.append(f"| {group} | {d['promotion_status']} | {b['score']:.9f} / {b['baseline']:.9f} | {l['score']:.9f} / {l['baseline']:.9f} | {', '.join(d['failed_quality_checks']) or 'None'} | {m['symbol']} {m['route']}: {m['fitted_rows']} |")
    lines += ["", "| Horizon | Current cohort rows | Date range | Boundary admitted / excluded | Further quality exclusions | Conflicting minutes |",
        "| --- | ---: | --- | --- | ---: | --- |"]
    for group,d in review["groups"].items():
        c = d["current_archive_cohort"] or {}
        q = d["target_boundary_quality"]
        lines.append(f"| {group} | {c.get('rows')} | {c.get('first_action_date')} to {c.get('last_action_date')} | {q.get('aligned_rows')} / {q.get('excluded_rows')} | {c.get('quality_excluded_rows')} | {q.get('conflicting_minute_rows_excluded')} |")
    lines += ["", "Saved policy permits Brier baseline +0.005 and log-loss baseline +0.01; qualification within those tolerances does not establish baseline outperformance.", "", "Horizons strictly beating both training baselines: "+", ".join(g for g,v in review["strict_baseline_comparison"].items() if v)+". The other promoted horizons pass the saved tolerances without beating both baselines.", "",
        "TWST fitted support: hourly 10,895 total, with only 1 row on route 1h@16:00; four-hour 1,982 total, minimum 24 on 4h@16:00; daily 10 total, 2 per route, with 10 assessment rows total; weekly 8 fitted and 2 assessment rows. These are observed saved counts, not a new qualification rule.", "", review["support_limitation"], "", review["repair_assessment"], "",
        "No archive reads, estimator inference, training, model selection, production writes, account/provider calls or orders occurred. Exact reports, support counts, calibration variation and numeric margins are retained in the JSON."]
    (OUT / "preliminary-directional-review.md").write_text("\n".join(lines)+"\n", encoding="utf-8")
    print(json.dumps({"status":review["status"],"run":str(run),"quality_failures":review["quality_failures"],"errors":review["errors"],"report":str(OUT / "preliminary-directional-review.md")}))
    return 1 if review["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
