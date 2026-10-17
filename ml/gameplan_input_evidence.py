"""Prospective Gameplan input receipts and descriptive local support evidence."""
from __future__ import annotations

import json
from pathlib import Path

from ml.artifacts import file_checksum
from ml.market_provenance import capture_runtime


FEATURE_EVIDENCE_COLUMNS = ("feature_raw_input_hashes_json", "feature_availability_basis",
    "observed_technical_contract", "observed_technical_bar_timestamp",
    "observed_technical_bar_end_timestamp", "observed_technical_available_at",
    "observed_technical_segment_start", "observed_technical_history_rows",
    "observed_technical_raw_input_hashes_json", "observed_technical_availability_basis")


def annotate_local_support(forecasts, support):
    """Describe pooled qualification without silently inventing a local gate."""
    result = forecasts.copy()
    result["model_qualification_scope"] = "POOLED_SYMBOLS_AND_ROUTES"
    result["symbol_route_separately_qualified"] = False
    states, fit_classes, assessment_classes, clusters = [], [], [], []
    for row in result.itertuples():
        counts = support.get(str(row.symbol), {})
        route = str(row.route)
        fit = counts.get("fitted_rows_by_route", {}).get(route, 0)
        assessed = counts.get("assessment_rows_by_route", {}).get(route, 0)
        fc = counts.get("fitted_class_counts_by_route", {}).get(route)
        ac = counts.get("assessment_class_counts_by_route", {}).get(route)
        cluster = counts.get("assessment_decision_clusters_by_route", {}).get(route)
        state = ("NO_LOCAL_FITTED_TARGETS" if not fit else "NO_LOCAL_ASSESSMENT" if not assessed
                 else "LEGACY_LOCAL_CLASS_SUPPORT_UNRECORDED" if ac is None or cluster is None
                 else "LOCAL_ASSESSMENT_ONE_CLASS" if len(ac) < 2
                 else "LOCAL_ASSESSMENT_FEWER_THAN_TEN_DECISIONS" if cluster < 10
                 else "LOCAL_ASSESSMENT_PRESENT_NOT_SEPARATELY_QUALIFIED")
        states.append(state); fit_classes.append(json.dumps(fc, sort_keys=True))
        assessment_classes.append(json.dumps(ac, sort_keys=True)); clusters.append(cluster)
    result["symbol_route_support_status"] = states
    result["symbol_route_fitted_class_counts_json"] = fit_classes
    result["symbol_route_assessment_class_counts_json"] = assessment_classes
    result["symbol_route_assessment_decision_clusters"] = clusters
    return result


def freeze_inference_inputs(current, *, numeric, categorical, directory, report, prefix="", matrix=None):
    """Save the exact selected inference values and current loaded-code receipt.

    These are new receipts. An old champion's missing original receipt remains
    missing; this records its new inference and copied source model identity.
    """
    directory = Path(directory)
    stem = prefix + "inference-inputs"
    columns = list(dict.fromkeys((*numeric, *categorical,
        *(name for name in current if name not in numeric and name not in categorical))))
    path = directory / (stem + ".parquet")
    current.loc[:, columns].to_parquet(path, index=False)
    matrix_path = directory / (prefix + "inference-matrix.parquet")
    (current.loc[:, [*numeric, *categorical]] if matrix is None else matrix).to_parquet(matrix_path, index=False)
    import sys
    observed_modules = tuple(name for name in ("ml.gameplan_observed_technicals",
        "technicals.calculations.market_regime", "technicals.calculations.breakout_pressure",
        "technicals.calculations.session_aware_breakout",
        "technicals.calculations.bar_shape") if name in sys.modules)
    runtime = capture_runtime(("ml.nightly_gameplan", "ml.gameplan_archive_features",
        "ml.gameplan_archive_integration", "ml.independent_stock_targets", "ml.stock_target_prices",
        "ml.stock_observation_sources", "ml.gameplan_champions", "ml.gameplan_input_evidence", *observed_modules),
        object_root=directory / (prefix + "source-objects"))
    evidence = {
        "schema_version": "gameplan-inference-input-receipt-v1",
        "receipt_scope": "new_inference_only; historical_original_inputs_not_recovered",
        "input_file": {"path": path.name, "sha256": file_checksum(path), "rows": len(current)},
        "model_input_matrix": {"path": matrix_path.name, "sha256": file_checksum(matrix_path),
                               "stage": "before_fitted_pipeline_imputation_and_encoding"},
        "numeric_features": list(numeric), "categorical_features": list(categorical),
        "model_file": report["model_file"],
        "recipe": {name: report.get(name) for name in (
            "group", "selected_family", "selected_neural_weight", "selected_logistic_regularization_c",
            "calibration_method", "probability_target_contract", "target_contract_version",
            "target_price_source_contract", "target_price_dataset", "source_selection_contract",
            "directional_promotion_policy", "calibration_selection", "promotion_gate", "deployment")},
        "runtime": runtime,
        "source_evidence_note": "input row raw hashes refer to native files bound by the publication manifest; legacy operational features without raw-row IDs remain explicitly unlinked",
    }
    receipt = directory / (stem + ".json")
    receipt.write_text(json.dumps(evidence, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8")
    run = directory.parent.parent
    paths = (path, matrix_path, receipt, *sorted((directory / (prefix + "source-objects")).rglob("*.py")))
    return {"path": path.relative_to(run).as_posix(), "sha256": file_checksum(path),
            "receipt": receipt.relative_to(run).as_posix(), "receipt_sha256": file_checksum(receipt)}, tuple(
                p.relative_to(run).as_posix() for p in paths)
