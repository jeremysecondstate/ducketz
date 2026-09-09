"""Deterministic retention of verified same-day independent-stock champions."""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from ml.artifacts import file_checksum
from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION
from ml.stock_target_prices import stock_price_dataset


CHAMPION_RETENTION_POLICY = "latest-compatible-promoted-same-action-date-v1"
_GATES = {
    "calibration_retains_directional_information", "assessment_has_at_least_10_decision_clusters",
    "brier_beats_training_base_rate", "log_loss_beats_training_base_rate",
    "expected_calibration_error_at_most_0_15",
}


def latest_promoted_champion(root: Path, *, group: str, action_date, symbols,
                             price_source: str, before) -> dict | None:
    """Choose latest eligible publication, never compare held-out candidate scores."""
    from ml.nightly_gameplan import GAMEPLAN_VERSION, read_gameplan_run
    root = Path(root).resolve()
    candidates = []
    for run in (root / "ml/nightly-gameplan-runs").glob("*"):
        if not (run / "receipt.json").is_file():
            continue
        receipt = json.loads((run / "receipt.json").read_text(encoding="utf-8"))
        manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
        config = manifest.get("configuration", {})
        if (config.get("schema_version") != GAMEPLAN_VERSION
                or config.get("target_contract_version") != STOCK_TARGET_CONTRACT_VERSION
                or config.get("action_date") != str(action_date)
                or tuple(config.get("symbols", ())) != tuple(symbols)
                or config.get("target_price_source_contract") != price_source
                or config.get("target_price_dataset") != stock_price_dataset(price_source)):
            continue
        published = pd.Timestamp(receipt["published_at"])
        if published.tzinfo is None or published >= pd.Timestamp(before):
            continue
        candidates.append((published, run.name, run))
    for _, _, run in sorted(candidates, reverse=True):
        publication = read_gameplan_run(root, run)
        outputs = publication.manifest["output_files"]
        if "model-reports.json" not in outputs:
            raise RuntimeError("Champion publication does not bind its model reports")
        reports = json.loads((run / "model-reports.json").read_text(encoding="utf-8"))
        report = reports.get(group, {})
        gate = report.get("promotion_gate", {})
        if gate.get("status") != "PROMOTED":
            continue
        checks = gate.get("checks", {})
        if (set(checks) != _GATES or any(value is not True for value in checks.values())
                or report.get("schema_version") != GAMEPLAN_VERSION or report.get("group") != group
                or report.get("target_contract_version") != STOCK_TARGET_CONTRACT_VERSION
                or report.get("target_price_source_contract") != price_source
                or report.get("target_price_dataset") != stock_price_dataset(price_source)):
            raise RuntimeError("Champion promotion or model contract is invalid")
        assessment, baseline = report["assessment"], report["training_base_rate_assessment"]
        if not (report["calibration_diagnostics"]["information_available"] is True
                and report["partition_decision_clusters"]["assessment"] >= 10
                and assessment["brier_score"] < baseline["brier_score"]
                and assessment["log_loss"] < baseline["log_loss"]
                and assessment["expected_calibration_error_10_bin"] <= .15):
            raise RuntimeError("Champion assessment does not satisfy its promotion gates")
        model_name = str(report["model_file"]["path"])
        cohort_name = report.get("deployment", {}).get("retained_cohort_output", f"training-cohort-{group}.parquet")
        for name in (model_name, cohort_name):
            if name not in outputs or not (run / name).resolve().is_relative_to(run.resolve()):
                raise RuntimeError("Champion model/cohort path is unbound or escapes its publication")
        model_path = run / model_name
        if (file_checksum(model_path) != report["model_file"]["checksum_sha256"]
                or model_path.stat().st_size != report["model_file"]["size"]):
            raise RuntimeError("Champion model differs from its report")
        payload = joblib.load(model_path)
        if (payload.get("schema_version") != GAMEPLAN_VERSION or payload.get("group") != group
                or tuple(payload.get("feature_columns", ())) != tuple(report["features"]["admitted"])
                or tuple(payload.get("categorical_columns", ())) != ("symbol", "route")
                or payload.get("selected_family") != report["selected_family"]
                or pd.Timestamp(payload["trained_at"]) > pd.Timestamp(publication.receipt["published_at"])):
            raise RuntimeError("Champion fitted payload differs from its verified report")
        files = (run / "manifest.json", run / "receipt.json", run / "model-reports.json", model_path, run / cohort_name)
        return {"run": run, "payload": payload, "report": report, "files": files,
                "model_path": model_path, "cohort_path": run / cohort_name,
                "evidence": {"policy": CHAMPION_RETENTION_POLICY,
                    "source_run": run.relative_to(root).as_posix(),
                    "source_published_at": publication.receipt["published_at"],
                    "files": [{"path": str(path), "size": path.stat().st_size,
                               "checksum_sha256": file_checksum(path)} for path in files]}}
    return None


def retain_champion(challenger: dict, *, champion: dict, current: pd.DataFrame,
                    run: Path, group: str, frozen_at) -> tuple[dict, tuple[str, ...]]:
    """Fresh predictions from the retained model; preserve both sets of evidence."""
    from ml.nightly_gameplan import _model_frame, _write_json_atomic
    payload, original_report = champion["payload"], champion["report"]
    numeric, categorical = tuple(payload["feature_columns"]), tuple(payload["categorical_columns"])
    if not set((*numeric, *categorical)).issubset(current.columns):
        raise RuntimeError("Current causal inputs cannot support the retained champion")
    for column in ("decision_timestamp", "information_available_at"):
        times = pd.to_datetime(current[column], utc=True, errors="coerce")
        if times.isna().any() or not times.le(pd.Timestamp(frozen_at)).all():
            raise RuntimeError("Champion current features contain future information")
    if not pd.to_datetime(current.information_available_at, utc=True).le(
        pd.to_datetime(current.decision_timestamp, utc=True)
    ).all():
        raise RuntimeError("Champion current information postdates its causal decision")
    matrix = _model_frame(current, numeric, categorical)
    raw = np.asarray(payload["estimator"].predict_proba(matrix)[:, 1], dtype=float)
    probability = np.asarray(payload["calibrator"].predict(raw), dtype=float)
    if (len(raw) != len(current) or not np.isfinite(raw).all() or not np.isfinite(probability).all()
            or ((raw < 0) | (raw > 1) | (probability < 0) | (probability > 1)).any()):
        raise RuntimeError("Retained champion produced invalid current probabilities")
    frames = challenger["forecasts"].copy()
    if list(zip(frames.symbol, frames.route)) != list(zip(current.symbol, current.route)):
        raise RuntimeError("Champion current rows differ from the challenger grid")
    model_name = f"models/{group}/champion.joblib"
    cohort_name = f"models/{group}/champion-training-cohort.parquet"
    report_name = f"models/{group}/champion-source-reports.json"
    challenger_report_name = f"models/{group}/challenger-report.json"
    for source, name in ((champion["model_path"], model_name), (champion["cohort_path"], cohort_name),
                         (champion["run"] / "model-reports.json", report_name)):
        destination = run / name
        if destination.exists():
            raise RuntimeError("Champion output already exists")
        shutil.copyfile(source, destination)
        bound = next(item for item in champion["evidence"]["files"] if item["path"] == str(source))
        if destination.stat().st_size != bound["size"] or file_checksum(destination) != bound["checksum_sha256"]:
            raise RuntimeError("Copied champion evidence differs from the verified source")
    _write_json_atomic(run / challenger_report_name, challenger["report"])
    report = copy.deepcopy(original_report)
    report["model_file"] = {"path": model_name, "size": (run / model_name).stat().st_size,
                            "checksum_sha256": file_checksum(run / model_name)}
    report["deployment"] = {**champion["evidence"], "status": "RETAINED_VERIFIED_CHAMPION",
                             "retained_cohort_output": cohort_name,
                             "challenger_cohort_output": f"training-cohort-{group}.parquet",
                             "challenger_report_output": challenger_report_name,
                             "challenger_status": challenger["report"]["promotion_gate"]["status"],
                             "predictions_recomputed_at": pd.Timestamp(frozen_at).isoformat()}
    frames["raw_probability"], frames["calibrated_probability"] = raw, probability
    for column, value in {
        "model_family": report["selected_family"], "neural_weight": report["selected_neural_weight"],
        "calibration_method": report["calibration_method"],
        "calibration_status": report["calibration_diagnostics"]["status"],
        "model_status": "PROMOTED", "model_artifact": model_name,
        "model_deployment_status": "RETAINED_VERIFIED_CHAMPION",
        "model_source_run": champion["evidence"]["source_run"],
        "option_feature_count": sum(name.startswith(("opt__", "opx__")) for name in numeric),
    }.items():
        frames[column] = value
    support = report["target_support_by_symbol"]
    for column, key, route in (
        ("symbol_target_history_rows", "admitted_rows", False),
        ("symbol_fitted_target_rows", "fitted_rows", False),
        ("symbol_assessment_rows", "assessment_rows", False),
        ("symbol_route_target_history_rows", "admitted_rows_by_route", True),
        ("symbol_route_fitted_target_rows", "fitted_rows_by_route", True),
        ("symbol_route_assessment_rows", "assessment_rows_by_route", True),
    ):
        frames[column] = [int(support.get(str(s), {}).get(key, {}).get(str(r), 0) if route
                              else support.get(str(s), {}).get(key, 0)) for s, r in zip(frames.symbol, frames.route)]
    frames.loc[frames.symbol_fitted_target_rows.eq(0) | frames.symbol_route_fitted_target_rows.eq(0),
               "model_status"] = "RESEARCH_NO_TARGET_HISTORY"
    return {"forecasts": frames, "report": report}, (model_name, cohort_name, report_name, challenger_report_name)
