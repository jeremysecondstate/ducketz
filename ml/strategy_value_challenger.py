from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from ml.artifacts import file_checksum, input_inventory, utc_timestamp, verify_manifest
from ml.current_publication import read_publication_receipt
from ml.strategy_publication import read_current_strategy_publication
from ml.strategy_selection.candidates import _terminal_profit
from ml.strategy_selection.contracts import StrategyModel, StrategySelectionPolicy
from ml.strategy_selection.market_state import (
    MarketState,
    _HALF_NORMAL_MAGNITUDES,
    _SCENARIO_COUNT,
    _candidate_prior,
)
from ml.strategy_selection.model import (
    _bounded_expected_return,
    _matrix,
    _prior_return,
    partition_strategy_outcomes,
)
from ml.strategy_selection.slow_model import (
    CANONICAL_PROFIT_HORIZONS,
    canonical_profit_horizon,
    load_promoted_strategy_model,
)
from ml.system_monitor import summarize_strategy_candidate_values


STRATEGY_VALUE_CHALLENGER_VERSION = "strategy-value-shadow-challenger-v1"
STRATEGY_VALUE_CHALLENGER_RECEIPT_VERSION = (
    "strategy-value-shadow-challenger-receipt-v1"
)
STRATEGY_VALUE_CHALLENGER_METHOD = "training-support-clipped-prior-plus-residual-v1"
_PRIOR_FEATURE = "strategy_prior__expected_return_on_risk"
_SCENARIO_FEATURE = "strategy_prior__scenario_coverage_score"
_KEY_COLUMNS = (
    "symbol",
    "horizon",
    "decision_timestamp",
    "target_window_start",
    "target_window_end",
)
_GREEKS = ("net_delta", "net_gamma", "net_theta", "net_vega")
_PARITY_TOLERANCE = 1e-10
_ASSESSMENT_METRIC_TOLERANCE = 1e-12


@dataclass(frozen=True)
class StrategyValueChallengerResult:
    directory: Path
    report_path: Path
    shadow_candidates_path: Path
    assessment_path: Path
    manifest_path: Path
    receipt_path: Path
    report: Mapping[str, object]


def run_strategy_value_challenger(
    datastore_root: Path,
    *,
    created_at: object | None = None,
    policy: StrategySelectionPolicy | None = None,
) -> StrategyValueChallengerResult:
    """Evaluate a value-only shadow correction without touching production.

    The designated challenger clips the scenario prior used in the final
    expected-return addition to the train-fitted support already embedded in
    the promoted return-estimator pipeline.  It does not refit a model, alter
    probability, rerank the production publication, publish authority, or
    enable an order path.
    """

    root = Path(datastore_root).resolve()
    created = utc_timestamp(created_at)
    strategy_publication = read_current_strategy_publication(root)
    strategy_run = strategy_publication.run_directory
    candidates_path = strategy_run / "strategy-candidates.parquet"
    candidates = pd.read_parquet(candidates_path)
    configuration = strategy_publication.manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ValueError("Strategy publication has no configuration mapping")
    source_loop_b_run = _safe_datastore_path(
        root,
        configuration.get("source_loop_b_run"),
        label="source Loop B run",
    )
    source_manifest = verify_manifest(source_loop_b_run)
    read_publication_receipt(
        source_loop_b_run,
        source_manifest,
        datastore_root=root,
    )
    samples_path = source_loop_b_run / "samples.parquet"
    samples = pd.read_parquet(samples_path)

    training_run, training_manifest, training_receipt, training_pointer = (
        _verified_training_authority(root)
    )
    authority_generation = training_run.name
    source_files: list[Path] = [
        candidates_path,
        strategy_run / "manifest.json",
        strategy_run / "publication.json",
        samples_path,
        source_loop_b_run / "manifest.json",
        source_loop_b_run / "publication.json",
        training_pointer,
        training_run / "manifest.json",
        training_run / "receipt.json",
    ]
    shadow_frames: list[pd.DataFrame] = []
    assessment_frames: list[pd.DataFrame] = []
    horizon_reports: dict[str, object] = {}

    for horizon in CANONICAL_PROFIT_HORIZONS:
        promoted = load_promoted_strategy_model(root, horizon=horizon)
        if promoted is None:
            raise ValueError(f"No verified promoted Strategy model for {horizon}")
        if int(promoted.report.get("orders_placed", -1)) != 0:
            raise ValueError(f"Promoted Strategy {horizon} report is not order-safe")
        model = promoted.model
        effective_policy = policy or _artifact_policy(model)
        outcome_path = training_run / f"{horizon}-modeled-outcomes.parquet"
        _verify_training_output(training_manifest, outcome_path)
        outcomes = pd.read_parquet(outcome_path)
        partitions = partition_strategy_outcomes(
            outcomes,
            policy=effective_policy,
        )
        live = _live_model_frame(
            candidates,
            samples,
            canonical_horizon=horizon,
            model=model,
            policy=effective_policy,
        )
        live_shadow, live_report = _live_shadow_evaluation(
            live,
            model=model,
            canonical_horizon=horizon,
        )
        assessment_shadow, assessment_report = _assessment_shadow_evaluation(
            partitions.assessment,
            model=model,
            canonical_horizon=horizon,
        )
        training_coverage = _partition_evidence(
            partitions.train,
            model=model,
        )
        calibration_coverage = _partition_evidence(
            partitions.calibration,
            model=model,
        )
        assessment_coverage = _partition_evidence(
            partitions.assessment,
            model=model,
        )
        acceptance = _acceptance_gates(
            live_report=live_report,
            assessment_report=assessment_report,
            training_coverage=training_coverage,
            calibration_coverage=calibration_coverage,
            assessment_coverage=assessment_coverage,
        )
        horizon_reports[horizon] = {
            "model_data_feature_health": (
                "WATCH"
                if not acceptance["promotion_eligible"]
                else "RETRAIN_DUE"
            ),
            "production_authority_status": "HEALTHY_UNCHANGED",
            "challenger_promotion_status": acceptance["status"],
            "authority_generation": authority_generation,
            "model_artifact": model.artifact_directory.relative_to(root).as_posix(),
            "model_policy_version": training_manifest.get("model_policy_version"),
            "strategy_selection_policy": _policy_payload(effective_policy),
            "challenger_method": STRATEGY_VALUE_CHALLENGER_METHOD,
            "live": live_report,
            "chronological_evidence": {
                "training": training_coverage,
                "calibration": calibration_coverage,
                "assessment": assessment_coverage,
                "assessment_comparison": assessment_report,
                "assessment_used_for_training": False,
                "assessment_used_for_calibration": False,
                "assessment_used_for_challenger_design": False,
                "assessment_used_for_predeclared_acceptance_gate_only": True,
                "real_lockbox_used": False,
            },
            "acceptance": acceptance,
        }
        shadow_frames.append(live_shadow)
        assessment_frames.append(assessment_shadow)
        source_files.extend((*promoted.authority_files, outcome_path))

    profit_route = candidates["horizon"].map(
        lambda value: canonical_profit_horizon(str(value)) is not None
    )
    heuristic = candidates.loc[
        profit_route
        & candidates["model_status"].astype("string").eq("HEURISTIC_ONLY")
    ].copy()
    heuristic_reasons = _heuristic_reason_counts(
        heuristic.get("pricing_missing_reason", pd.Series(dtype="string"))
    )
    shadow_candidates = pd.concat(shadow_frames, ignore_index=True, sort=False)
    assessment_comparison = pd.concat(
        assessment_frames,
        ignore_index=True,
        sort=False,
    )
    promotion_eligible = all(
        bool(report["acceptance"]["promotion_eligible"])
        for report in horizon_reports.values()
        if isinstance(report, Mapping)
    )
    report = {
        "schema_version": STRATEGY_VALUE_CHALLENGER_VERSION,
        "created_at": created.isoformat(),
        "status": "COMPLETE_SHADOW_ONLY",
        "decision": (
            "ELIGIBLE_FOR_SEPARATE_REVIEW_NOT_PROMOTED"
            if promotion_eligible
            else "BLOCKED_KEEP_CURRENT_AUTHORITY"
        ),
        "promotion_eligible": promotion_eligible,
        "promotion_performed": False,
        "production_candidate_mutation": False,
        "production_ranking_mutation": False,
        "production_model_authority_mutation": False,
        "probability_model_mutation": False,
        "orders_enabled": False,
        "orders_placed": 0,
        "challenger_method": STRATEGY_VALUE_CHALLENGER_METHOD,
        "method_summary": (
            "Keep the promoted return residual fixed and replace only the raw "
            "post-model scenario-prior addition with that prior clipped to the "
            "return pipeline's immutable train-fitted support."
        ),
        "probability_semantics": (
            "ML Profit Probability is copied and parity-checked; Scenario Coverage "
            "and exact-expiration profitable-scenario fractions are nonprobabilistic."
        ),
        "strategy_source_run": strategy_run.relative_to(root).as_posix(),
        "strategy_candidate_checksum_sha256": file_checksum(candidates_path),
        "source_loop_b_run": source_loop_b_run.relative_to(root).as_posix(),
        "authority_generation": authority_generation,
        "authority_pointer_checksum_sha256": file_checksum(training_pointer),
        "authority_manifest_checksum_sha256": file_checksum(
            training_run / "manifest.json"
        ),
        "authority_receipt_checksum_sha256": file_checksum(
            training_run / "receipt.json"
        ),
        "training_authority_orders_enabled": bool(
            training_manifest.get("orders_enabled", True)
        ),
        "training_authority_orders_placed": int(
            training_receipt.get("orders_placed", -1)
        ),
        "live_candidate_rows": len(candidates),
        "shadow_candidate_rows": len(shadow_candidates),
        "heuristic_only_rows_excluded": len(heuristic),
        "heuristic_only_missing_reason_counts": heuristic_reasons,
        "horizons": horizon_reports,
        "safety": {
            "shadow_artifact_only": True,
            "production_fields_preserved": [
                "calibrated_profit_probability",
                "expected_return_on_risk",
                "expected_net_profit",
                "candidate_rank",
            ],
            "runtime_restart_required": False,
            "options_prediction_loop_changed": False,
            "authority_pointer_write_allowed": False,
            "manual_review_required_before_any_future_code_or_authority_change": True,
        },
    }
    return _publish(
        root,
        created=created,
        report=report,
        shadow_candidates=shadow_candidates,
        assessment=assessment_comparison,
        source_files=tuple(dict.fromkeys(source_files)),
    )


def _live_model_frame(
    candidates: pd.DataFrame,
    samples: pd.DataFrame,
    *,
    canonical_horizon: str,
    model: StrategyModel,
    policy: StrategySelectionPolicy,
) -> pd.DataFrame:
    required_candidates = {
        *_KEY_COLUMNS,
        "candidate_key",
        "model_status",
        "calibrated_profit_probability",
        "expected_return_on_risk",
        "expected_net_profit",
        "capital_required",
    }
    missing = sorted(required_candidates.difference(candidates.columns))
    if missing:
        raise ValueError("Strategy candidates are missing: " + ", ".join(missing))
    routed = candidates["horizon"].map(
        lambda value: canonical_profit_horizon(str(value)) == canonical_horizon
    )
    frame = candidates.loc[
        routed & candidates["model_status"].astype("string").eq("MODEL_FIT")
    ].copy()
    if frame.empty:
        raise ValueError(f"No fitted live candidates for {canonical_horizon}")
    for source in (frame, samples):
        for column in _KEY_COLUMNS[2:]:
            source[column] = pd.to_datetime(source[column], utc=True, errors="coerce")
    context_columns = [
        column
        for column in model.numeric_features
        if column in samples.columns and column not in frame.columns
    ]
    sample_columns = [*_KEY_COLUMNS, *context_columns]
    context = samples.loc[:, sample_columns].copy()
    if context.duplicated(list(_KEY_COLUMNS)).any():
        raise ValueError("Loop B samples contain duplicate Strategy context keys")
    frame = frame.merge(
        context,
        on=list(_KEY_COLUMNS),
        how="left",
        validate="many_to_one",
    )
    prior_rows = [
        _candidate_prior(row, state=_market_state(row), policy=policy)
        for row in frame.to_dict("records")
    ]
    prior = pd.DataFrame(prior_rows, index=frame.index)
    frame[_SCENARIO_FEATURE] = prior["scenario_coverage"].to_numpy(dtype=float)
    frame[_PRIOR_FEATURE] = prior["expected_return_on_risk"].to_numpy(dtype=float)
    for column in model.numeric_features:
        frame[column] = pd.Series(
            pd.to_numeric(frame[column], errors="coerce").to_numpy(
                dtype=float,
                na_value=np.nan,
            ),
            index=frame.index,
        )
    missing_model = sorted(
        set((*model.numeric_features, *model.categorical_features)).difference(
            frame.columns
        )
    )
    if missing_model:
        raise ValueError(
            f"Live {canonical_horizon} challenger frame is missing model features: "
            + ", ".join(missing_model)
        )
    return frame


def _live_shadow_evaluation(
    frame: pd.DataFrame,
    *,
    model: StrategyModel,
    canonical_horizon: str,
) -> tuple[pd.DataFrame, dict[str, object]]:
    matrix = _matrix(frame, model.numeric_features, model.categorical_features)
    raw_probability = np.asarray(
        model.estimator.predict_proba(matrix)[:, 1],
        dtype=float,
    )
    calibrated_probability = np.asarray(
        model.calibrator.predict(raw_probability),
        dtype=float,
    )
    residual = np.asarray(model.return_estimator.predict(matrix), dtype=float)
    raw_prior = _prior_return(frame)
    lower, upper = _prior_support(model)
    clipped_prior = np.clip(raw_prior, lower, upper)
    current_return, current_profit = _bounded_expected_return(
        frame,
        raw_prior + residual,
    )
    shadow_return, shadow_profit = _bounded_expected_return(
        frame,
        clipped_prior + residual,
    )
    published_probability = pd.to_numeric(
        frame["calibrated_profit_probability"], errors="coerce"
    ).to_numpy(dtype=float)
    published_return = pd.to_numeric(
        frame["expected_return_on_risk"], errors="coerce"
    ).to_numpy(dtype=float)
    published_profit = pd.to_numeric(
        frame["expected_net_profit"], errors="coerce"
    ).to_numpy(dtype=float)
    probability_parity = _maximum_absolute_error(
        calibrated_probability,
        published_probability,
    )
    return_parity = _maximum_absolute_error(current_return, published_return)
    profit_parity = _maximum_absolute_error(current_profit, published_profit)
    if max(probability_parity, return_parity, profit_parity) > _PARITY_TOLERANCE:
        raise ValueError(
            f"Live {canonical_horizon} challenger failed production parity: "
            f"probability={probability_parity}, return={return_parity}, "
            f"profit={profit_parity}"
        )

    current_audit = summarize_strategy_candidate_values(frame)
    shadow_audit_frame = frame.copy()
    shadow_audit_frame["expected_return_on_risk"] = shadow_return
    shadow_audit_frame["expected_net_profit"] = shadow_profit
    shadow_audit = summarize_strategy_candidate_values(shadow_audit_frame)
    if current_audit["integrity_failure_rows"] or shadow_audit[
        "integrity_failure_rows"
    ]:
        raise ValueError(
            f"Live {canonical_horizon} value integrity failed during shadow audit"
        )

    exact_rows = [
        _exact_expiration_stress(row)
        for row in frame.to_dict("records")
    ]
    exact = pd.DataFrame(exact_rows, index=frame.index)
    output = pd.DataFrame(
        {
            "symbol": frame["symbol"].astype("string"),
            "horizon": frame["horizon"].astype("string"),
            "canonical_profit_horizon": canonical_horizon,
            "decision_timestamp": frame["decision_timestamp"],
            "target_window_start": frame["target_window_start"],
            "target_window_end": frame["target_window_end"],
            "candidate_key": frame["candidate_key"].astype("string"),
            "strategy_name": frame["strategy_name"].astype("string"),
            "production_candidate_rank": pd.to_numeric(
                frame["candidate_rank"], errors="coerce"
            ).astype("Int64"),
            "production_calibrated_profit_probability": published_probability,
            "production_expected_return_on_risk": published_return,
            "production_expected_net_profit": published_profit,
            "scenario_prior_return_on_risk": raw_prior,
            "return_model_training_support_lower": lower,
            "return_model_training_support_upper": upper,
            "scenario_prior_outside_training_support": (
                (raw_prior < lower) | (raw_prior > upper)
            ),
            "support_clipped_scenario_prior_return_on_risk": clipped_prior,
            "return_model_residual": residual,
            "shadow_expected_return_on_risk": shadow_return,
            "shadow_expected_net_profit": shadow_profit,
            "shadow_minus_production_return_on_risk": (
                shadow_return - published_return
            ),
            "probability_changed": False,
            "production_rank_changed": False,
            "shadow_only": True,
        }
    )
    output = pd.concat((output, exact.reset_index(drop=True)), axis=1)
    eligible_exact = exact["exact_expiration_stress_status"].eq("AVAILABLE")
    exact_summary = {
        "policy": "same-market-state-grid-exact-expiration-payoff-diagnostic-v1",
        "interpretation": (
            "Diagnostic only. The profitable-scenario fraction is not a calibrated "
            "probability and the stress value is not used as a live predictor."
        ),
        "eligible_rows": int(eligible_exact.sum()),
        "unavailable_rows": int((~eligible_exact).sum()),
        "unavailable_reason_counts": {
            str(key): int(value)
            for key, value in exact.loc[
                ~eligible_exact, "exact_expiration_stress_status"
            ].value_counts().sort_index().items()
        },
        "expected_return_distribution": _distribution(
            pd.to_numeric(
                exact.loc[
                    eligible_exact,
                    "exact_expiration_stress_return_on_risk",
                ],
                errors="coerce",
            ).to_numpy(dtype=float)
        ),
    }
    report = {
        "rows": len(frame),
        "route_rows": {
            str(key): int(value)
            for key, value in frame["horizon"].value_counts().sort_index().items()
        },
        "prior_training_support": {"lower": lower, "upper": upper},
        "scenario_prior_distribution": _distribution(raw_prior),
        "scenario_prior_above_training_support_rows": int((raw_prior > upper).sum()),
        "scenario_prior_below_training_support_rows": int((raw_prior < lower).sum()),
        "scenario_prior_outside_training_support_fraction": float(
            ((raw_prior < lower) | (raw_prior > upper)).mean()
        ),
        "probability_parity_max_absolute_error": probability_parity,
        "production_return_parity_max_absolute_error": return_parity,
        "production_profit_parity_max_absolute_error": profit_parity,
        "probability_values_changed": False,
        "production_candidate_fields_changed": False,
        "production_candidate_ranks_changed": False,
        "production_value_audit": _compact_value_audit(current_audit),
        "shadow_value_audit": _compact_value_audit(shadow_audit),
        "shadow_expected_return_distribution": _distribution(shadow_return),
        "exact_expiration_stress": exact_summary,
    }
    return output, report


def _assessment_shadow_evaluation(
    assessment: pd.DataFrame,
    *,
    model: StrategyModel,
    canonical_horizon: str,
) -> tuple[pd.DataFrame, dict[str, object]]:
    frame = assessment.copy()
    for column in model.numeric_features:
        frame[column] = pd.Series(
            pd.to_numeric(frame[column], errors="coerce").to_numpy(
                dtype=float,
                na_value=np.nan,
            ),
            index=frame.index,
        )
    matrix = _matrix(frame, model.numeric_features, model.categorical_features)
    raw_probability = np.asarray(
        model.estimator.predict_proba(matrix)[:, 1],
        dtype=float,
    )
    probability = np.asarray(model.calibrator.predict(raw_probability), dtype=float)
    residual = np.asarray(model.return_estimator.predict(matrix), dtype=float)
    raw_prior = _prior_return(frame)
    lower, upper = _prior_support(model)
    clipped_prior = np.clip(raw_prior, lower, upper)
    production_return, _ = _bounded_expected_return(
        frame,
        raw_prior + residual,
    )
    shadow_return, _ = _bounded_expected_return(
        frame,
        clipped_prior + residual,
    )
    observed_return = pd.to_numeric(
        frame["return_on_risk"], errors="coerce"
    ).to_numpy(dtype=float)
    weights = _decision_weights(frame)
    production_metrics = _return_metrics(
        observed_return,
        production_return,
        weights=weights,
    )
    shadow_metrics = _return_metrics(
        observed_return,
        shadow_return,
        weights=weights,
    )
    production_candidate_metrics = _return_metrics(
        observed_return,
        production_return,
    )
    shadow_candidate_metrics = _return_metrics(
        observed_return,
        shadow_return,
    )
    production_ranking = _ranking_evidence(
        frame,
        probability=probability,
        expected_return=production_return,
    )
    shadow_ranking = _ranking_evidence(
        frame,
        probability=probability,
        expected_return=shadow_return,
    )
    report = {
        "rows": len(frame),
        "decisions": int(frame["target_window_start"].nunique()),
        "prior_training_support": {"lower": lower, "upper": upper},
        "prior_above_training_support_rows": int((raw_prior > upper).sum()),
        "prior_below_training_support_rows": int((raw_prior < lower).sum()),
        "production_expected_return": production_metrics,
        "challenger_expected_return": shadow_metrics,
        "challenger_minus_production": {
            key: float(shadow_metrics[key] - production_metrics[key])
            for key in (
                "mean_predicted_return_on_risk",
                "mean_error",
                "mean_absolute_error",
                "root_mean_squared_error",
            )
        },
        "candidate_level_production_expected_return": production_candidate_metrics,
        "candidate_level_challenger_expected_return": shadow_candidate_metrics,
        "production_probability_first_ranking": production_ranking,
        "challenger_probability_first_ranking": shadow_ranking,
        "probability_first_selected_candidates_unchanged": (
            production_ranking["selected_candidate_keys_sha256"]
            == shadow_ranking["selected_candidate_keys_sha256"]
        ),
        "probability_model_or_calibration_changed": False,
        "assessment_used_for_challenger_selection": False,
        "assessment_used_for_acceptance_only": True,
    }
    output = pd.DataFrame(
        {
            "canonical_profit_horizon": canonical_horizon,
            "symbol": frame["symbol"].astype("string"),
            "decision_timestamp": frame["decision_timestamp"],
            "target_window_start": frame["target_window_start"],
            "target_window_end": frame["target_window_end"],
            "candidate_key": frame["candidate_key"].astype("string"),
            "strategy_name": frame["strategy_name"].astype("string"),
            "observed_return_on_risk": observed_return,
            "profitable": pd.to_numeric(frame["profitable"], errors="coerce").astype(
                "Int8"
            ),
            "calibrated_profit_probability": probability,
            "scenario_prior_return_on_risk": raw_prior,
            "support_clipped_scenario_prior_return_on_risk": clipped_prior,
            "return_model_residual": residual,
            "production_expected_return_on_risk": production_return,
            "shadow_expected_return_on_risk": shadow_return,
            "assessment_used_for_training": False,
            "assessment_used_for_calibration": False,
            "real_lockbox": False,
        }
    )
    return output, report


def _partition_evidence(
    frame: pd.DataFrame,
    *,
    model: StrategyModel,
) -> dict[str, object]:
    prior = _prior_return(frame)
    lower, upper = _prior_support(model)
    greek_counts = {
        greek: int(pd.to_numeric(frame.get(greek), errors="coerce").notna().sum())
        for greek in _GREEKS
    }
    return {
        "rows": len(frame),
        "decisions": int(frame["target_window_start"].nunique()),
        "prior_distribution": _distribution(prior),
        "positive_prior_rows": int((prior > 0.0).sum()),
        "prior_above_return_pipeline_support_rows": int((prior > upper).sum()),
        "prior_below_return_pipeline_support_rows": int((prior < lower).sum()),
        "greek_finite_rows": greek_counts,
        "all_greeks_finite_rows": int(
            pd.concat(
                [pd.to_numeric(frame.get(greek), errors="coerce") for greek in _GREEKS],
                axis=1,
            ).notna().all(axis=1).sum()
        ),
    }


def _acceptance_gates(
    *,
    live_report: Mapping[str, object],
    assessment_report: Mapping[str, object],
    training_coverage: Mapping[str, object],
    calibration_coverage: Mapping[str, object],
    assessment_coverage: Mapping[str, object],
) -> dict[str, object]:
    production = assessment_report["production_expected_return"]
    challenger = assessment_report["challenger_expected_return"]
    production_ranking = assessment_report[
        "production_probability_first_ranking"
    ]
    challenger_ranking = assessment_report[
        "challenger_probability_first_ranking"
    ]
    production_audit = live_report["production_value_audit"]
    shadow_audit = live_report["shadow_value_audit"]
    checks = {
        "production_probability_parity": (
            float(live_report["probability_parity_max_absolute_error"])
            <= _PARITY_TOLERANCE
        ),
        "production_expected_return_parity": (
            float(live_report["production_return_parity_max_absolute_error"])
            <= _PARITY_TOLERANCE
        ),
        "production_fields_and_ranks_unchanged": (
            not bool(live_report["production_candidate_fields_changed"])
            and not bool(live_report["production_candidate_ranks_changed"])
        ),
        "live_value_alerts_not_increased": (
            int(shadow_audit["alert_rows"])
            <= int(production_audit["alert_rows"])
        ),
        "assessment_mae_not_worse": (
            float(challenger["mean_absolute_error"])
            <= float(production["mean_absolute_error"])
            + _ASSESSMENT_METRIC_TOLERANCE
        ),
        "assessment_rmse_not_worse": (
            float(challenger["root_mean_squared_error"])
            <= float(production["root_mean_squared_error"])
            + _ASSESSMENT_METRIC_TOLERANCE
        ),
        "assessment_probability_first_realized_return_not_worse": (
            float(challenger_ranking["mean_realized_return_on_risk"])
            >= float(production_ranking["mean_realized_return_on_risk"])
            - _ASSESSMENT_METRIC_TOLERANCE
        ),
        "assessment_probability_first_total_net_profit_not_worse": (
            float(challenger_ranking["total_net_profit"])
            >= float(production_ranking["total_net_profit"])
            - _ASSESSMENT_METRIC_TOLERANCE
        ),
        "training_contains_positive_scenario_prior_support": (
            int(training_coverage["positive_prior_rows"]) > 0
        ),
        "calibration_contains_positive_scenario_prior_support": (
            int(calibration_coverage["positive_prior_rows"]) > 0
        ),
        "assessment_contains_positive_scenario_prior_support": (
            int(assessment_coverage["positive_prior_rows"]) > 0
        ),
        "training_contains_finite_greek_evidence": (
            int(training_coverage["all_greeks_finite_rows"]) > 0
        ),
        "calibration_contains_finite_greek_evidence": (
            int(calibration_coverage["all_greeks_finite_rows"]) > 0
        ),
        "assessment_contains_finite_greek_evidence": (
            int(assessment_coverage["all_greeks_finite_rows"]) > 0
        ),
    }
    evidence_gate_names = (
        "training_contains_positive_scenario_prior_support",
        "calibration_contains_positive_scenario_prior_support",
        "assessment_contains_positive_scenario_prior_support",
        "training_contains_finite_greek_evidence",
        "calibration_contains_finite_greek_evidence",
        "assessment_contains_finite_greek_evidence",
    )
    missing_evidence = [name for name in evidence_gate_names if not checks[name]]
    failed_checks = [name for name, passed in checks.items() if not passed]
    return {
        "promotion_eligible": all(checks.values()),
        "status": (
            "ELIGIBLE_FOR_SEPARATE_REVIEW"
            if all(checks.values())
            else "BLOCKED_KEEP_CURRENT_AUTHORITY"
        ),
        "checks": checks,
        "blocking_reasons": [
            *(
                ["INSUFFICIENT_CAUSAL_GREEK_PRIOR_SUPPORT"]
                if missing_evidence
                else []
            ),
            *failed_checks,
        ],
        "automatic_promotion_allowed": False,
        "authority_pointer_rewrite_allowed": False,
        "required_next_evidence": (
            "Build causally clocked historical Greek features for the modeled OPRA "
            "cohorts, retrain through the sole checked-in slow owner, and require a "
            "new untouched chronological assessment to pass all existing gates."
        ),
    }


def _prior_support(model: StrategyModel) -> tuple[float, float]:
    try:
        feature_index = model.numeric_features.index(_PRIOR_FEATURE)
        preprocess = model.return_estimator.named_steps["preprocess"]
        numeric = preprocess.named_transformers_["numeric"]
        clipper = numeric.named_steps["clip"]
        lower = float(clipper.lower_bounds_[feature_index])
        upper = float(clipper.upper_bounds_[feature_index])
    except (AttributeError, KeyError, TypeError, ValueError, IndexError) as exc:
        raise ValueError(
            "Promoted return estimator does not expose immutable prior support"
        ) from exc
    if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
        raise ValueError("Promoted return-estimator prior support is invalid")
    return lower, upper


def _artifact_policy(model: StrategyModel) -> StrategySelectionPolicy:
    manifest = _read_json(model.artifact_directory / "manifest.json")
    raw = manifest.get("policy")
    if not isinstance(raw, Mapping):
        raise ValueError("Promoted Strategy artifact has no selection policy")
    try:
        return StrategySelectionPolicy(
            policy_id=str(raw["policy_id"]),
            account_approval=str(raw["account_approval"]),
            minimum_train_decisions=int(raw["minimum_train_decisions"]),
            calibration_decisions=int(raw["calibration_decisions"]),
            assessment_decisions=int(raw["assessment_decisions"]),
            candidate_width_steps=tuple(
                int(value) for value in raw["candidate_width_steps"]
            ),
            maximum_expiration_choices=int(raw["maximum_expiration_choices"]),
            maximum_relative_bid_ask_spread=float(
                raw["maximum_relative_bid_ask_spread"]
            ),
            minimum_open_interest=float(raw["minimum_open_interest"]),
            maximum_quote_staleness_seconds=float(
                raw["maximum_quote_staleness_seconds"]
            ),
            per_contract_fee=float(raw["per_contract_fee"]),
            fee_schedule=str(raw["fee_schedule"]),
            fee_schedule_verified_on=str(raw["fee_schedule_verified_on"]),
            buy_to_close_fee_waiver_applied=bool(
                raw["buy_to_close_fee_waiver_applied"]
            ),
            variable_exchange_regulatory_fees_included=bool(
                raw["variable_exchange_regulatory_fees_included"]
            ),
            random_state=int(raw["random_state"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Promoted Strategy artifact policy is invalid") from exc


def _policy_payload(policy: StrategySelectionPolicy) -> dict[str, object]:
    return {
        "policy_id": policy.policy_id,
        "account_approval": policy.account_approval,
        "minimum_train_decisions": policy.minimum_train_decisions,
        "calibration_decisions": policy.calibration_decisions,
        "assessment_decisions": policy.assessment_decisions,
        "candidate_width_steps": list(policy.candidate_width_steps),
        "maximum_expiration_choices": policy.maximum_expiration_choices,
        "maximum_relative_bid_ask_spread": policy.maximum_relative_bid_ask_spread,
        "minimum_open_interest": policy.minimum_open_interest,
        "maximum_quote_staleness_seconds": (
            policy.maximum_quote_staleness_seconds
        ),
        "per_contract_fee": policy.per_contract_fee,
        "fee_schedule": policy.fee_schedule,
        "fee_schedule_verified_on": policy.fee_schedule_verified_on,
        "buy_to_close_fee_waiver_applied": (
            policy.buy_to_close_fee_waiver_applied
        ),
        "variable_exchange_regulatory_fees_included": (
            policy.variable_exchange_regulatory_fees_included
        ),
        "random_state": policy.random_state,
    }


def _market_state(row: Mapping[str, object]) -> MarketState:
    decision = _utc(row.get("decision_timestamp"), "decision timestamp")
    target_end = _utc(row.get("target_window_end"), "target window end")
    return MarketState(
        direction_probability_up=_finite_or_none(row.get("direction_probability_up")),
        expected_absolute_move=_required_finite(
            row.get("market_expected_absolute_move"),
            "market expected absolute move",
        ),
        expected_realized_volatility=_finite_or_none(
            row.get("market_expected_realized_volatility")
        ),
        uncertainty=_required_finite(row.get("market_uncertainty"), "market uncertainty"),
        trend_persistence=_finite_or_none(row.get("market_trend_persistence")),
        mean_reversion_tendency=_finite_or_none(
            row.get("market_mean_reversion_tendency")
        ),
        holding_days=max(
            (target_end - decision).total_seconds() / 86_400.0,
            1.0 / 1_440.0,
        ),
    )


def _exact_expiration_stress(row: Mapping[str, object]) -> dict[str, object]:
    common: dict[str, object] = {
        "exact_expiration_stress_return_on_risk": np.nan,
        "exact_expiration_stress_net_profit": np.nan,
        "exact_expiration_profitable_scenario_fraction": np.nan,
    }
    if bool(row.get("lifecycle", False)):
        return {"exact_expiration_stress_status": "LIFECYCLE_PATH_REQUIRED", **common}
    if str(row.get("expiration_structure") or "").upper() != "SINGLE":
        return {"exact_expiration_stress_status": "MULTI_EXPIRATION_PATH_REQUIRED", **common}
    if str(row.get("risk_calculation_status") or "") != "EXPIRATION_PAYOFF_EXACT":
        return {"exact_expiration_stress_status": "EXACT_PAYOFF_CONTRACT_UNAVAILABLE", **common}
    target_end = _utc(row.get("target_window_end"), "target window end")
    expiration = _utc(row.get("front_expiration"), "front expiration")
    if target_end.date() != expiration.date():
        return {"exact_expiration_stress_status": "TARGET_NOT_EXPIRATION_SESSION", **common}
    try:
        legs = json.loads(str(row.get("legs_json") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"exact_expiration_stress_status": "INVALID_EXACT_LEGS", **common}
    if not isinstance(legs, list) or not legs:
        return {"exact_expiration_stress_status": "INVALID_EXACT_LEGS", **common}
    state = _market_state(row)
    positive = state.expected_absolute_move * _HALF_NORMAL_MAGNITUDES
    returns = np.concatenate((positive, -positive))
    weights = np.concatenate(
        (
            np.full(_SCENARIO_COUNT, state.effective_probability_up / _SCENARIO_COUNT),
            np.full(
                _SCENARIO_COUNT,
                (1.0 - state.effective_probability_up) / _SCENARIO_COUNT,
            ),
        )
    )
    underlying = _required_finite(row.get("underlying_price"), "underlying price")
    terminal_prices = np.maximum(underlying * (1.0 + returns), 0.0)
    entry_cash_flow = _required_finite(row.get("entry_cash_flow"), "entry cash flow")
    profits = np.asarray(
        [
            _terminal_profit(legs, entry_cash_flow, float(price))
            for price in terminal_prices
        ],
        dtype=float,
    )
    capital = _required_finite(row.get("capital_required"), "capital required")
    if capital <= 0.0 or not np.isfinite(profits).all():
        return {"exact_expiration_stress_status": "INVALID_EXACT_PAYOFF", **common}
    expected_profit = float(np.sum(weights * profits))
    profitable_fraction = float(np.sum(weights * (profits > 0.0)))
    return {
        "exact_expiration_stress_status": "AVAILABLE",
        "exact_expiration_stress_return_on_risk": expected_profit / capital,
        "exact_expiration_stress_net_profit": expected_profit,
        "exact_expiration_profitable_scenario_fraction": float(
            np.clip(profitable_fraction, 0.0, 1.0)
        ),
    }


def _ranking_evidence(
    frame: pd.DataFrame,
    *,
    probability: np.ndarray,
    expected_return: np.ndarray,
) -> dict[str, object]:
    ranked = frame.loc[
        :,
        [
            "target_window_start",
            "symbol",
            "candidate_key",
            "profitable",
            "return_on_risk",
            "net_profit",
        ],
    ].copy()
    ranked["probability"] = probability
    ranked["expected_return"] = expected_return
    top = (
        ranked.sort_values(
            ["target_window_start", "probability", "expected_return", "candidate_key"],
            ascending=[True, False, False, True],
            kind="mergesort",
        )
        .groupby("target_window_start", sort=False)
        .head(1)
    )
    return {
        "ranking_rule": (
            "highest_calibrated_probability_then_expected_return_on_risk_"
            "then_candidate_key_per_decision"
        ),
        "decision_count": len(top),
        "top_candidate_profitable_rate": float(
            pd.to_numeric(top["profitable"], errors="raise").mean()
        ),
        "mean_realized_return_on_risk": float(
            pd.to_numeric(top["return_on_risk"], errors="raise").mean()
        ),
        "total_net_profit": float(
            pd.to_numeric(top["net_profit"], errors="raise").sum()
        ),
        "selected_candidate_keys_sha256": _string_fingerprint(
            top["target_window_start"].astype("string")
            + "|"
            + top["symbol"].astype("string")
            + "|"
            + top["candidate_key"].astype("string")
        ),
    }


def _return_metrics(
    observed: np.ndarray,
    predicted: np.ndarray,
    *,
    weights: np.ndarray | None = None,
) -> dict[str, object]:
    target = np.asarray(observed, dtype=float)
    values = np.asarray(predicted, dtype=float)
    if not np.isfinite(target).all() or not np.isfinite(values).all():
        raise ValueError("Challenger expected-return evidence must be finite")
    effective_weights = (
        np.ones(len(target), dtype=float)
        if weights is None
        else np.asarray(weights, dtype=float)
    )
    error = values - target
    return {
        "rows": len(target),
        "mean_observed_return_on_risk": float(
            np.average(target, weights=effective_weights)
        ),
        "mean_predicted_return_on_risk": float(
            np.average(values, weights=effective_weights)
        ),
        "mean_error": float(np.average(error, weights=effective_weights)),
        "mean_absolute_error": float(
            np.average(np.abs(error), weights=effective_weights)
        ),
        "root_mean_squared_error": float(
            math.sqrt(np.average(np.square(error), weights=effective_weights))
        ),
    }


def _decision_weights(frame: pd.DataFrame) -> np.ndarray:
    counts = frame.groupby("target_window_start")["candidate_key"].transform("count")
    values = pd.to_numeric(counts, errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(values).all() or np.any(values <= 0.0):
        raise ValueError("Strategy challenger decision weights are invalid")
    return 1.0 / values


def _compact_value_audit(audit: Mapping[str, object]) -> dict[str, object]:
    return {
        "status": audit.get("status"),
        "summary": audit.get("summary"),
        "candidate_rows": audit.get("candidate_rows"),
        "integrity_failure_rows": audit.get("integrity_failure_rows"),
        "alert_rows": audit.get("alert_rows"),
        "alert_counts": audit.get("alert_counts"),
        "expected_return_distribution_percent": audit.get(
            "expected_return_distribution_percent"
        ),
        "by_horizon": audit.get("by_horizon"),
        "top_findings": audit.get("top_findings"),
    }


def _heuristic_reason_counts(values: pd.Series) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for value in values.astype("string").fillna("UNSPECIFIED"):
        text = str(value).strip()
        reasons = {
            part.rsplit(":", 1)[-1].strip() or "UNSPECIFIED"
            for part in text.split(";")
            if part.strip()
        }
        for reason in reasons or {"UNSPECIFIED"}:
            counts[reason] += 1
    return dict(sorted(counts.items()))


def _distribution(values: np.ndarray) -> dict[str, object]:
    array = np.asarray(values, dtype=float)
    finite = array[np.isfinite(array)]
    if not len(finite):
        return {
            "count": 0,
            "minimum": None,
            "median": None,
            "p95": None,
            "p99": None,
            "maximum": None,
        }
    return {
        "count": len(finite),
        "minimum": float(np.min(finite)),
        "median": float(np.median(finite)),
        "p95": float(np.quantile(finite, 0.95)),
        "p99": float(np.quantile(finite, 0.99)),
        "maximum": float(np.max(finite)),
    }


def _verified_training_authority(
    root: Path,
) -> tuple[Path, Mapping[str, object], Mapping[str, object], Path]:
    pointer_path = root / "ml" / "strategy-profit-training-latest" / "run.json"
    pointer = _read_json(pointer_path)
    current = pointer.get("current")
    if not isinstance(current, Mapping):
        raise ValueError("Strategy training pointer has no current record")
    run = _safe_datastore_path(root, current.get("run_path"), label="training run")
    manifest_path = run / "manifest.json"
    receipt_path = run / "receipt.json"
    manifest = _read_json(manifest_path)
    receipt = _read_json(receipt_path)
    if (
        current.get("receipt_checksum_sha256") != file_checksum(receipt_path)
        or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
        or bool(manifest.get("orders_enabled", True))
        or int(receipt.get("orders_placed", -1)) != 0
    ):
        raise ValueError("Strategy training authority failed receipt or order safety")
    return run, manifest, receipt, pointer_path


def _verify_training_output(
    manifest: Mapping[str, object],
    path: Path,
) -> None:
    outputs = manifest.get("outputs")
    if not isinstance(outputs, list):
        raise ValueError("Strategy training manifest outputs are invalid")
    matching = [
        item
        for item in outputs
        if isinstance(item, Mapping) and item.get("path") == path.name
    ]
    if len(matching) != 1:
        raise ValueError(f"Strategy training output is not manifested: {path.name}")
    record = matching[0]
    if (
        not path.is_file()
        or path.stat().st_size != int(record.get("size", -1))
        or file_checksum(path) != record.get("checksum_sha256")
    ):
        raise ValueError(f"Strategy training output failed checksum: {path.name}")


def _publish(
    root: Path,
    *,
    created: pd.Timestamp,
    report: Mapping[str, object],
    shadow_candidates: pd.DataFrame,
    assessment: pd.DataFrame,
    source_files: Sequence[Path],
) -> StrategyValueChallengerResult:
    parent = (root / "ml" / "strategy-value-challenger-runs").resolve()
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / created.strftime("%Y%m%dT%H%M%S.%fZ")
    suffix = 2
    while destination.exists():
        destination = parent / f"{created.strftime('%Y%m%dT%H%M%S.%fZ')}-{suffix}"
        suffix += 1
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-{os.getpid()}-",
            dir=parent,
        )
    )
    try:
        report_path = staging / "report.json"
        shadow_path = staging / "shadow-candidates.parquet"
        assessment_path = staging / "assessment-comparison.parquet"
        _write_json(report_path, report)
        shadow_candidates.to_parquet(shadow_path, index=False)
        assessment.to_parquet(assessment_path, index=False)
        manifest = {
            "schema_version": STRATEGY_VALUE_CHALLENGER_VERSION,
            "created_at": created.isoformat(),
            "status": "COMPLETE_SHADOW_ONLY",
            "run_path": destination.relative_to(root).as_posix(),
            "challenger_method": STRATEGY_VALUE_CHALLENGER_METHOD,
            "inputs": input_inventory(source_files, relative_to=root),
            "outputs": {
                path.name: {
                    "size": path.stat().st_size,
                    "checksum_sha256": file_checksum(path),
                }
                for path in (report_path, shadow_path, assessment_path)
            },
            "production_mutation": False,
            "production_authority_mutation": False,
            "orders_enabled": False,
            "orders_placed": 0,
        }
        manifest_path = staging / "manifest.json"
        _write_json(manifest_path, manifest)
        receipt = {
            "schema_version": STRATEGY_VALUE_CHALLENGER_RECEIPT_VERSION,
            "created_at": created.isoformat(),
            "status": "COMPLETE_SHADOW_ONLY",
            "run_path": destination.relative_to(root).as_posix(),
            "manifest_checksum_sha256": file_checksum(manifest_path),
            "report_checksum_sha256": file_checksum(report_path),
            "shadow_candidates_checksum_sha256": file_checksum(shadow_path),
            "assessment_checksum_sha256": file_checksum(assessment_path),
            "promotion_performed": False,
            "authority_pointer_written": False,
            "production_candidate_mutation": False,
            "orders_placed": 0,
        }
        receipt_path = staging / "receipt.json"
        _write_json(receipt_path, receipt)
        staging.replace(destination)
    except BaseException:
        if staging.exists() and staging.parent == parent:
            shutil.rmtree(staging)
        raise
    return StrategyValueChallengerResult(
        directory=destination,
        report_path=destination / "report.json",
        shadow_candidates_path=destination / "shadow-candidates.parquet",
        assessment_path=destination / "assessment-comparison.parquet",
        manifest_path=destination / "manifest.json",
        receipt_path=destination / "receipt.json",
        report=report,
    )


def _safe_datastore_path(root: Path, value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid {label} path")
    relative = Path(value)
    if relative.is_absolute():
        raise ValueError(f"{label} path must be relative")
    resolved = (root / relative).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError(f"{label} path escapes datastore")
    return resolved


def _read_json(path: Path) -> Mapping[str, object]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unreadable JSON evidence: {path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError(f"JSON evidence is not a mapping: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    Path(path).write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError(f"Invalid {label}")
    return pd.Timestamp(timestamp)


def _finite_or_none(value: object) -> float | None:
    number = pd.to_numeric(value, errors="coerce")
    return float(number) if pd.notna(number) and math.isfinite(float(number)) else None


def _required_finite(value: object, label: str) -> float:
    number = _finite_or_none(value)
    if number is None:
        raise ValueError(f"Strategy challenger requires finite {label}")
    return number


def _maximum_absolute_error(left: np.ndarray, right: np.ndarray) -> float:
    first = np.asarray(left, dtype=float)
    second = np.asarray(right, dtype=float)
    if (
        first.shape != second.shape
        or not np.isfinite(first).all()
        or not np.isfinite(second).all()
    ):
        return math.inf
    return float(np.max(np.abs(first - second))) if len(first) else 0.0


def _string_fingerprint(values: pd.Series) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(str(value).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run a receipt-first, value-only Strategy shadow challenger without "
            "mutating production candidates, model authority, loops, or orders."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    result = run_strategy_value_challenger(root)
    payload = dict(result.report)
    payload["run_path"] = result.directory.relative_to(root).as_posix()
    print(
        json.dumps(
            payload,
            indent=None if args.compact else 2,
            sort_keys=True,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "STRATEGY_VALUE_CHALLENGER_METHOD",
    "STRATEGY_VALUE_CHALLENGER_RECEIPT_VERSION",
    "STRATEGY_VALUE_CHALLENGER_VERSION",
    "StrategyValueChallengerResult",
    "run_strategy_value_challenger",
]
