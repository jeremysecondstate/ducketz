from __future__ import annotations

import importlib.metadata
import json
import math
import os
import platform
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
from sklearn.kernel_approximation import Nystroem
from sklearn.linear_model import BayesianRidge
from sklearn.preprocessing import RobustScaler

from ml.artifacts import file_checksum, semantic_metadata_fingerprint, utc_timestamp
from ml.option_pricing.model import (
    FiniteBasisGP,
    IntervalCalibration,
    derived_feature_matrix,
)
from ml.option_pricing.policies import (
    DERIVED_FEATURE_COLUMNS,
    FINITE_BASIS_RESIDUAL_MODEL_NAME,
    LOOP_NATIVE_CALL_PUTS,
    LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION,
    LOOP_NATIVE_MODEL_POLICY_VERSION,
    LOOP_NATIVE_SHADOW_SCHEMA_VERSION,
    LOOP_NATIVE_SURFACE_WEIGHTING_POLICY_VERSION,
    LOOP_NATIVE_SYMBOLS,
    LoopNativeModelPolicy,
    SEMANTIC_FEATURE_COLUMNS,
)
from ml.option_pricing.schwab_materialization import (
    OFFLINE_OPRA_BACKFILL,
    OFFLINE_SCHWAB_BOOTSTRAP,
    PROSPECTIVE_OPRA,
    PROSPECTIVE_SCHWAB,
    SCHWAB_MATERIALIZATION_SAMPLE_NAME,
    SchwabMaterialization,
    read_loop_native_schwab_materialization,
)


LOOP_NATIVE_MODEL_GENERATION_SCHEMA_VERSION = "loop-native-finite-basis-generation-v3"
LOOP_NATIVE_MODEL_RECEIPT_VERSION = "loop-native-finite-basis-generation-receipt-v3"
LOOP_NATIVE_MODEL_POINTER_VERSION = "loop-native-finite-basis-generation-pointer-v3"
LOOP_NATIVE_MODEL_FILE = "pooled-call-put-model.joblib"
LOOP_NATIVE_MODEL_MANIFEST = "manifest.json"
LOOP_NATIVE_MODEL_RECEIPT = "receipt.json"


class LoopNativeModelError(RuntimeError):
    """A causal pooled shadow-model generation failed closed."""


@dataclass(frozen=True)
class LoopNativePartitions:
    train: pd.DataFrame
    calibration: pd.DataFrame
    assessment: pd.DataFrame
    train_sessions: tuple[str, ...]
    calibration_sessions: tuple[str, ...]
    assessment_sessions: tuple[str, ...]
    duplicate_rows_collapsed: int
    input_rows: int


@dataclass(frozen=True)
class PooledSideModel:
    call_put: str
    bsgp: FiniteBasisGP
    standard_gp: FiniteBasisGP
    interval_calibration: IntervalCalibration
    constant_residual: float
    support_minimum: np.ndarray
    support_maximum: np.ndarray
    route_intercepts: Mapping[str, float]
    route_support_sessions: Mapping[str, int]
    calibration_session_count: int
    calibrated: bool
    assessment: Mapping[str, object]


@dataclass(frozen=True)
class LoopNativeModelGeneration:
    directory: Path
    published_at: pd.Timestamp
    effective_from: pd.Timestamp
    trained_through: pd.Timestamp
    expires_at: pd.Timestamp
    models: Mapping[str, PooledSideModel]
    manifest: Mapping[str, object]
    receipt: Mapping[str, object]
    generation_hash: str


@dataclass(frozen=True)
class LoopNativeModelLoad:
    generation: LoopNativeModelGeneration | None
    status: str
    reason: str


def surface_weights(frame: pd.DataFrame) -> np.ndarray:
    """Preserve one unit per surface while distributing it by causal liquidity."""

    from ml.option_pricing.weighting import liquidity_weights

    return liquidity_weights(frame)


def partition_loop_native_samples(
    samples: pd.DataFrame,
    *,
    trainer_cutoff: object,
    policy: LoopNativeModelPolicy | None = None,
) -> LoopNativePartitions:
    """Create purged chronological partitions by independent XNYS session."""

    effective = policy or LoopNativeModelPolicy()
    cutoff = utc_timestamp(trainer_cutoff)
    required = {
        "symbol",
        "call_put",
        "contract_symbol",
        "target_snapshot_for",
        "source_snapshot_for",
        "source_available_at",
        "source_quote_timestamp",
        "rate_source_at",
        "volatility_source_at",
        "dividend_source_at",
        "observed_quote_timestamp",
        "observed_available_at",
        "offline_emulated_prediction_at",
        "prediction_created_at",
        "prediction_available_at",
        "underlying_readiness_ready_at",
        "underlying_readiness_path",
        "underlying_readiness_receipt_path",
        "sample_status",
        "normalized_residual",
        "dollar_residual",
        "observed_mid",
        "black_scholes_price",
        "underlying_price",
        "evidence_lane",
        "prospective_eligible",
        *SEMANTIC_FEATURE_COLUMNS,
    }
    if missing := sorted(required.difference(samples.columns)):
        raise LoopNativeModelError(
            "Loop-native samples are missing: " + ", ".join(missing)
        )
    eligible = samples.loc[
        samples["sample_status"].astype("string").eq("AVAILABLE")
    ].copy()
    if eligible.empty:
        raise LoopNativeModelError("No available Loop-native samples can train")
    eligible["symbol"] = eligible["symbol"].astype("string").str.strip().str.upper()
    eligible["call_put"] = (
        eligible["call_put"].astype("string").str.strip().str.upper()
    )
    if not set(eligible["symbol"]).issubset(LOOP_NATIVE_SYMBOLS):
        raise LoopNativeModelError(
            f"Training samples escape the {len(LOOP_NATIVE_SYMBOLS)}-symbol production scope"
        )
    if not set(eligible["call_put"]).issubset(LOOP_NATIVE_CALL_PUTS):
        raise LoopNativeModelError("Training samples contain an invalid option side")
    for column in (
        "target_snapshot_for",
        "source_snapshot_for",
        "source_available_at",
        "source_quote_timestamp",
        "rate_source_at",
        "volatility_source_at",
        "dividend_source_at",
        "observed_quote_timestamp",
        "observed_available_at",
        "offline_emulated_prediction_at",
        "prediction_created_at",
        "prediction_available_at",
        "underlying_readiness_ready_at",
    ):
        eligible[column] = pd.to_datetime(eligible[column], utc=True, errors="coerce")
    clocks = eligible[
        [
            "target_snapshot_for",
            "source_snapshot_for",
            "source_available_at",
            "source_quote_timestamp",
            "rate_source_at",
            "volatility_source_at",
            "dividend_source_at",
            "observed_quote_timestamp",
            "observed_available_at",
            "underlying_readiness_ready_at",
        ]
    ]
    if clocks.isna().any(axis=None):
        raise LoopNativeModelError("Training samples contain invalid causal clocks")
    if not eligible["source_snapshot_for"].lt(eligible["target_snapshot_for"]).all():
        raise LoopNativeModelError("Training samples contain same-target leakage")
    if not eligible["source_available_at"].lt(eligible["observed_available_at"]).all():
        raise LoopNativeModelError("Training label receipts do not follow inputs")
    if not eligible["observed_available_at"].lt(cutoff).all():
        raise LoopNativeModelError(
            "Only outcomes whose receipts strictly predate the trainer cutoff may train"
        )
    lanes = set(eligible["evidence_lane"].astype("string"))
    if not lanes.issubset(
        {
            OFFLINE_OPRA_BACKFILL,
            OFFLINE_SCHWAB_BOOTSTRAP,
            PROSPECTIVE_OPRA,
            PROSPECTIVE_SCHWAB,
        }
    ):
        raise LoopNativeModelError("Training samples contain an unauthorized evidence lane")
    offline = eligible["evidence_lane"].astype("string").isin(
        {OFFLINE_OPRA_BACKFILL, OFFLINE_SCHWAB_BOOTSTRAP}
    )
    prediction_cutoff = eligible["prediction_created_at"].where(
        ~offline,
        eligible["offline_emulated_prediction_at"],
    )
    publication_cutoff = eligible["prediction_available_at"].where(
        ~offline,
        eligible["offline_emulated_prediction_at"],
    )
    feature_clocks = (
        eligible["source_available_at"].lt(prediction_cutoff)
        & eligible["source_quote_timestamp"].lt(prediction_cutoff)
        & eligible["rate_source_at"].lt(prediction_cutoff)
        & eligible["volatility_source_at"].lt(prediction_cutoff)
        & eligible["dividend_source_at"].lt(prediction_cutoff)
        & eligible["underlying_readiness_ready_at"].lt(prediction_cutoff)
        & publication_cutoff.ge(prediction_cutoff)
        & eligible["observed_quote_timestamp"].gt(publication_cutoff)
        & eligible["observed_available_at"].gt(publication_cutoff)
    )
    if prediction_cutoff.isna().any() or not feature_clocks.all():
        raise LoopNativeModelError(
            "Training samples contain target-time or later feature leakage"
        )
    prospective = eligible["prospective_eligible"].fillna(False).astype(bool)
    if prospective.loc[offline].any() or not prospective.loc[~offline].all():
        raise LoopNativeModelError("Offline/prospective evidence labels are inconsistent")

    natural = [
        "evidence_lane",
        "symbol",
        "target_snapshot_for",
        "call_put",
        "contract_symbol",
    ]
    eligible = eligible.sort_values(
        [*natural, "source_available_at", "observed_available_at"], kind="stable"
    )
    duplicate_mask = eligible.duplicated(natural, keep=False)
    if duplicate_mask.any():
        comparison = [
            *SEMANTIC_FEATURE_COLUMNS,
            "normalized_residual",
            "observed_mid",
            "black_scholes_price",
            "source_snapshot_for",
            "source_available_at",
            "source_quote_timestamp",
            "rate_source_at",
            "volatility_source_at",
            "dividend_source_at",
            "observed_quote_timestamp",
            "observed_available_at",
            "underlying_readiness_ready_at",
            "underlying_readiness_path",
            "underlying_readiness_receipt_path",
        ]
        conflicts = (
            eligible.loc[duplicate_mask]
            .groupby(natural, dropna=False, sort=False)[comparison]
            .nunique(dropna=False)
            .gt(1)
            .any(axis=1)
        )
        if conflicts.any():
            raise LoopNativeModelError(
                "Duplicate materialized samples disagree on causal features or labels"
            )
    input_rows = len(eligible)
    eligible = eligible.drop_duplicates(natural, keep="first").reset_index(drop=True)
    duplicate_rows_collapsed = input_rows - len(eligible)
    numeric = [*SEMANTIC_FEATURE_COLUMNS, "normalized_residual"]
    coerced = eligible[numeric].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(coerced.to_numpy(dtype=float)).all():
        raise LoopNativeModelError("Training features or residual targets are non-finite")
    eligible[numeric] = coerced
    dollar_residual = pd.to_numeric(
        eligible["dollar_residual"], errors="coerce"
    ).to_numpy(dtype=float)
    reconstructed_dollar = (
        eligible["normalized_residual"].to_numpy(dtype=float)
        * eligible["underlying_price"].to_numpy(dtype=float)
    )
    if not np.isfinite(dollar_residual).all() or not np.allclose(
        dollar_residual,
        reconstructed_dollar,
        rtol=0.0,
        atol=1e-10,
    ):
        raise LoopNativeModelError(
            "Normalized and dollar residual targets disagree"
        )
    eligible["_session"] = (
        eligible["target_snapshot_for"]
        .dt.tz_convert("America/New_York")
        .dt.strftime("%Y-%m-%d")
    )
    sessions = tuple(sorted(eligible["_session"].dropna().unique()))
    minimum = (
        effective.minimum_fit_sessions
        + effective.minimum_calibration_sessions
        + effective.minimum_assessment_sessions
    )
    if len(sessions) < minimum:
        raise LoopNativeModelError(
            "Insufficient independent sessions for chronological fitting: "
            f"required {minimum}, observed {len(sessions)}"
        )
    assessment_count = max(
        effective.minimum_assessment_sessions,
        int(math.floor(len(sessions) * 0.20)),
    )
    calibration_count = max(
        effective.minimum_calibration_sessions,
        int(math.floor(len(sessions) * 0.20)),
    )
    while (
        len(sessions) - assessment_count - calibration_count
        < effective.minimum_fit_sessions
    ):
        if calibration_count > effective.minimum_calibration_sessions:
            calibration_count -= 1
        elif assessment_count > effective.minimum_assessment_sessions:
            assessment_count -= 1
        else:  # protected by the minimum-session check above
            raise LoopNativeModelError("Chronological partition allocation failed")
    train_end = len(sessions) - calibration_count - assessment_count
    calibration_end = len(sessions) - assessment_count
    train_sessions = sessions[:train_end]
    calibration_sessions = sessions[train_end:calibration_end]
    assessment_sessions = sessions[calibration_end:]
    train = eligible.loc[eligible["_session"].isin(train_sessions)].copy()
    calibration = eligible.loc[
        eligible["_session"].isin(calibration_sessions)
        & eligible["observed_available_at"].lt(
            eligible.loc[eligible["_session"].isin(assessment_sessions), "target_snapshot_for"].min()
        )
    ].copy()
    assessment = eligible.loc[
        eligible["_session"].isin(assessment_sessions)
    ].copy()
    if train.empty or calibration.empty or assessment.empty:
        raise LoopNativeModelError("Causal boundary purging emptied a model partition")
    first_calibration = calibration["target_snapshot_for"].min()
    first_assessment = assessment["target_snapshot_for"].min()
    train = train.loc[train["observed_available_at"].lt(first_calibration)].copy()
    calibration = calibration.loc[
        calibration["observed_available_at"].lt(first_assessment)
    ].copy()
    if train.empty or calibration.empty:
        raise LoopNativeModelError("Receipt boundary purging emptied fit or calibration")

    def clean(frame: pd.DataFrame) -> pd.DataFrame:
        return (
            frame.drop(columns="_session")
            .sort_values(
                ["target_snapshot_for", "symbol", "call_put", "contract_symbol"],
                kind="stable",
            )
            .reset_index(drop=True)
        )

    return LoopNativePartitions(
        train=clean(train),
        calibration=clean(calibration),
        assessment=clean(assessment),
        train_sessions=tuple(train_sessions),
        calibration_sessions=tuple(calibration_sessions),
        assessment_sessions=tuple(assessment_sessions),
        duplicate_rows_collapsed=duplicate_rows_collapsed,
        input_rows=input_rows,
    )


def train_loop_native_shadow_generation(
    datastore_root: Path,
    *,
    materialization: SchwabMaterialization,
    trainer_cutoff: object,
    published_at: object,
    policy: LoopNativeModelPolicy | None = None,
) -> LoopNativeModelGeneration:
    """Fit two bounded pooled models and atomically publish one future generation."""

    root = Path(datastore_root).resolve()
    effective = policy or LoopNativeModelPolicy()
    cutoff = utc_timestamp(trainer_cutoff)
    published = utc_timestamp(published_at)
    if published <= cutoff:
        raise LoopNativeModelError("Model publication must strictly follow its trainer cutoff")
    if (
        materialization.dry_run
        or materialization.directory is None
        or materialization.receipt is None
    ):
        raise LoopNativeModelError("A model generation requires immutable materialization")
    verified_materialization = read_loop_native_schwab_materialization(
        materialization.directory,
        datastore_root=root,
    )
    materialization_published_at = utc_timestamp(
        verified_materialization.receipt.get("published_at")
        if verified_materialization.receipt is not None
        else None
    )
    if published <= materialization_published_at:
        raise LoopNativeModelError(
            "Model publication must strictly follow immutable materialization publication"
        )
    materialization_cutoff = utc_timestamp(
        verified_materialization.manifest.get("trainer_cutoff")
    )
    if materialization_cutoff > cutoff:
        raise LoopNativeModelError(
            "Materialization includes evidence beyond the trainer cutoff"
        )
    partitions = partition_loop_native_samples(
        verified_materialization.samples,
        trainer_cutoff=cutoff,
        policy=effective,
    )
    train = _bounded_surface_rows(
        partitions.train,
        maximum_rows=effective.maximum_training_rows,
    )
    calibration = _bounded_surface_rows(
        partitions.calibration,
        maximum_rows=effective.maximum_training_rows,
    )
    assessment = _bounded_surface_rows(
        partitions.assessment,
        maximum_rows=effective.maximum_training_rows,
    )
    side_models: dict[str, PooledSideModel] = {}
    side_reports: dict[str, object] = {}
    for call_put in LOOP_NATIVE_CALL_PUTS:
        side_train = _side(train, call_put)
        side_calibration = _side(calibration, call_put)
        side_assessment = _side(assessment, call_put)
        if side_train.empty or side_calibration.empty or side_assessment.empty:
            raise LoopNativeModelError(
                f"Pooled {call_put} model lacks a complete chronological partition"
            )
        model, report = _fit_pooled_side(
            call_put,
            train=side_train,
            calibration=side_calibration,
            assessment=side_assessment,
            calibration_session_count=len(partitions.calibration_sessions),
            policy=effective,
        )
        side_models[call_put] = model
        side_reports[call_put] = report
    used = pd.concat((train, calibration, assessment), ignore_index=True, sort=False)
    trained_through = pd.Timestamp(used["observed_available_at"].max())
    if not trained_through < cutoff:
        raise LoopNativeModelError("Model inputs do not strictly predate the trainer cutoff")
    route_statistics = _route_statistics(used)
    surface_report = _surface_weight_report(
        input_rows=partitions.input_rows,
        duplicate_rows_collapsed=partitions.duplicate_rows_collapsed,
        train=train,
        calibration=calibration,
        assessment=assessment,
    )
    materialization_manifest_path = (
        verified_materialization.directory / "manifest.json"
    )
    materialization_receipt_path = (
        verified_materialization.directory / "receipt.json"
    )
    sample_path = (
        verified_materialization.directory / SCHWAB_MATERIALIZATION_SAMPLE_NAME
    )
    manifest_base: dict[str, object] = {
        "schema_version": LOOP_NATIVE_MODEL_GENERATION_SCHEMA_VERSION,
        "model_name": FINITE_BASIS_RESIDUAL_MODEL_NAME,
        "model_kind": (
            "finite-nystroem-rbf-basis-with-bayesian-ridge-posterior"
        ),
        "exact_gaussian_process": False,
        "legacy_serialization_aliases": {
            "bsgp": "finite_basis_residual",
            "standard_gp": "finite_basis_price_comparator",
        },
        "policy_version": LOOP_NATIVE_MODEL_POLICY_VERSION,
        "shadow_schema_version": LOOP_NATIVE_SHADOW_SCHEMA_VERSION,
        "materialization_policy_version": LOOP_NATIVE_MATERIALIZATION_POLICY_VERSION,
        "surface_weighting_policy_version": LOOP_NATIVE_SURFACE_WEIGHTING_POLICY_VERSION,
        "scope": {
            "pooling": "one-pooled-model-per-call-put",
            "symbols": list(LOOP_NATIVE_SYMBOLS),
            "call_puts": list(LOOP_NATIVE_CALL_PUTS),
            "routes": [
                {"symbol": symbol, "call_put": call_put}
                for symbol in LOOP_NATIVE_SYMBOLS
                for call_put in LOOP_NATIVE_CALL_PUTS
            ],
        },
        "trainer_cutoff": cutoff.isoformat(),
        "trained_through": trained_through.isoformat(),
        "published_at": published.isoformat(),
        "effective_from": published.isoformat(),
        "expires_at": (
            published + pd.Timedelta(hours=effective.maximum_age_hours)
        ).isoformat(),
        "maximum_age_hours": effective.maximum_age_hours,
        "chronological_session_partitions": {
            "timezone": "America/New_York",
            "unit": "distinct-regular-session-date",
            "training": list(partitions.train_sessions),
            "calibration": list(partitions.calibration_sessions),
            "assessment": list(partitions.assessment_sessions),
            "assessment_used_for_training": False,
            "assessment_used_for_calibration": False,
        },
        "materialization": {
            "run_path": verified_materialization.directory.relative_to(root).as_posix(),
            "published_at": verified_materialization.receipt.get("published_at"),
            "manifest_checksum_sha256": file_checksum(materialization_manifest_path),
            "receipt_checksum_sha256": file_checksum(materialization_receipt_path),
            "sample_checksum_sha256": file_checksum(sample_path),
            "sample_rows": len(verified_materialization.samples),
            "selected_input_receipts": verified_materialization.manifest.get(
                "selected_input_receipts", []
            ),
        },
        "surface_weight_report": surface_report,
        "route_support_statistics": route_statistics,
        "features": {
            "semantic": list(SEMANTIC_FEATURE_COLUMNS),
            "derived": list(DERIVED_FEATURE_COLUMNS),
            "target": "normalized_residual",
            "dollar_residual_recorded": True,
            "target_time_implied_volatility_is_a_feature": False,
        },
        "finite_basis_policy": asdict(effective),
        "side_models": side_reports,
        "evidence_counts": _evidence_counts(used),
        "library_versions": _library_versions(),
        "historical_opra_used": bool(
            verified_materialization.report.get("historical_opra_used", False)
        ),
        "external_provider_requests": 0,
        "automated_action_allowed": False,
    }
    return _publish_generation(
        root,
        models=side_models,
        manifest_base=manifest_base,
        published_at=published,
    )


def _fit_pooled_side(
    call_put: str,
    *,
    train: pd.DataFrame,
    calibration: pd.DataFrame,
    assessment: pd.DataFrame,
    calibration_session_count: int,
    policy: LoopNativeModelPolicy,
) -> tuple[PooledSideModel, Mapping[str, object]]:
    train_target = _target(train, "normalized_residual")
    calibration_target = _target(calibration, "normalized_residual")
    bsgp, bsgp_scores = _select_gamma(
        train,
        train_target,
        calibration,
        calibration_target,
        policy=policy,
    )
    standard_gp, standard_scores = _select_gamma(
        train,
        _normalized_price(train),
        calibration,
        _normalized_price(calibration),
        policy=policy,
    )
    constant = float(np.average(train_target, weights=surface_weights(train)))
    train_mean, _train_std = bsgp.predict(train)
    route_intercepts: dict[str, float] = {}
    route_sessions: dict[str, int] = {}
    train_with_prediction = train.copy()
    train_with_prediction["_pooled_prediction"] = train_mean
    train_with_prediction["_route_error"] = (
        train_target - train_mean
    )
    for symbol in LOOP_NATIVE_SYMBOLS:
        route = train_with_prediction.loc[
            train_with_prediction["symbol"].astype("string").eq(symbol)
        ]
        count = _distinct_sessions(route)
        route_sessions[symbol] = count
        if count >= policy.minimum_route_support_sessions and not route.empty:
            route_intercepts[symbol] = float(
                np.average(
                    _target(route, "_route_error"),
                    weights=surface_weights(route),
                )
            )
    calibration_mean, calibration_std = bsgp.predict(calibration)
    calibration_symbols = (
        calibration["symbol"].astype("string").str.strip().str.upper()
    )
    calibration_mean = calibration_mean + calibration_symbols.map(
        route_intercepts
    ).fillna(0.0).to_numpy(dtype=float)
    interval = _calibrate_intervals(
        calibration_target - calibration_mean,
        calibration_std,
        surface_weights(calibration),
        minimum_standard_deviation=policy.minimum_predictive_standard_deviation,
    )
    matrix = derived_feature_matrix(train)
    support_minimum = matrix.min(axis=0)
    support_maximum = matrix.max(axis=0)
    calibrated = calibration_session_count >= policy.minimum_calibrated_sessions
    assessment_report = _compare_models(
        assessment,
        bsgp=bsgp,
        standard_gp=standard_gp,
        constant_residual=constant,
        interval=interval,
        route_intercepts=route_intercepts,
    )
    model = PooledSideModel(
        call_put=call_put,
        bsgp=bsgp,
        standard_gp=standard_gp,
        interval_calibration=interval,
        constant_residual=constant,
        support_minimum=support_minimum,
        support_maximum=support_maximum,
        route_intercepts=route_intercepts,
        route_support_sessions=route_sessions,
        calibration_session_count=calibration_session_count,
        calibrated=calibrated,
        assessment=assessment_report,
    )
    report = {
        "call_put": call_put,
        "training_rows": len(train),
        "calibration_rows": len(calibration),
        "assessment_rows": len(assessment),
        "training_sessions": _distinct_sessions(train),
        "calibration_sessions": _distinct_sessions(calibration),
        "assessment_sessions": _distinct_sessions(assessment),
        "component_count": bsgp.component_count,
        "basis_seed": policy.random_state,
        "basis_kernel": "rbf-nystroem",
        "feature_transform": {
            "formula": [
                "log(underlying_price)",
                "log(strike/underlying_price)",
                "risk_free_rate",
                "log(lagged_implied_volatility)",
                "sqrt(target_years_to_expiration)",
                "dividend_yield",
            ],
            "robust_scaler_center": bsgp.scaler.center_.tolist(),
            "robust_scaler_scale": bsgp.scaler.scale_.tolist(),
        },
        "selected_gamma": {
            "bsgp": bsgp.gamma,
            "standard_gp": standard_gp.gamma,
        },
        "gamma_calibration_scores": {
            "bsgp": bsgp_scores,
            "standard_gp": standard_scores,
        },
        "regression": {
            "kind": "bayesian-ridge",
            "bsgp": _regression_manifest(bsgp.regression),
            "standard_gp": _regression_manifest(standard_gp.regression),
        },
        "constant_residual": constant,
        "interval_calibration": asdict(interval),
        "interval_calibration_mean": (
            "pooled-bsgp-plus-eligible-train-derived-route-intercept"
        ),
        "interval_calibration_status": (
            "CALIBRATED" if calibrated else "IMMATURE_CONSERVATIVE_FALLBACK"
        ),
        "minimum_calibrated_sessions": policy.minimum_calibrated_sessions,
        "support_minimum": support_minimum.tolist(),
        "support_maximum": support_maximum.tolist(),
        "route_intercepts": route_intercepts,
        "route_support_sessions": route_sessions,
        "route_adaptation_policy": (
            "pooled-plus-route-intercept-only-after-minimum-independent-sessions"
        ),
        "assessment": assessment_report,
        "automated_action_allowed": False,
    }
    return model, report


def _select_gamma(
    train: pd.DataFrame,
    train_target: np.ndarray,
    calibration: pd.DataFrame,
    calibration_target: np.ndarray,
    *,
    policy: LoopNativeModelPolicy,
) -> tuple[FiniteBasisGP, Mapping[str, float]]:
    scores: dict[str, float] = {}
    selected: FiniteBasisGP | None = None
    selected_score = math.inf
    weights = surface_weights(calibration)
    for gamma in policy.gamma_grid:
        fitted = _fit_finite_basis_gp(
            train,
            train_target,
            gamma=gamma,
            policy=policy,
        )
        predicted, _std = fitted.predict(calibration)
        score = float(
            math.sqrt(
                np.average(
                    np.square(predicted - calibration_target), weights=weights
                )
            )
        )
        scores[str(gamma)] = score
        if score < selected_score - 1e-15:
            selected = fitted
            selected_score = score
    if selected is None:
        raise LoopNativeModelError("Finite-basis gamma selection failed")
    return selected, scores


def _fit_finite_basis_gp(
    frame: pd.DataFrame,
    target: np.ndarray,
    *,
    gamma: float,
    policy: LoopNativeModelPolicy,
) -> FiniteBasisGP:
    matrix = derived_feature_matrix(frame)
    scaler = RobustScaler()
    scaled = scaler.fit_transform(matrix)
    components = min(policy.component_count, len(frame))
    basis = Nystroem(
        kernel="rbf",
        gamma=float(gamma),
        n_components=components,
        random_state=policy.random_state,
    )
    expanded = basis.fit_transform(scaled)
    regression = BayesianRidge()
    regression.fit(
        expanded,
        np.asarray(target, dtype=float),
        sample_weight=surface_weights(frame),
    )
    return FiniteBasisGP(scaler, basis, regression, float(gamma), components)


def _calibrate_intervals(
    errors: np.ndarray,
    raw_standard_deviation: np.ndarray,
    weights: np.ndarray,
    *,
    minimum_standard_deviation: float,
) -> IntervalCalibration:
    standard_deviation = np.maximum(
        np.asarray(raw_standard_deviation, dtype=float),
        minimum_standard_deviation,
    )
    residual = np.asarray(errors, dtype=float)
    scale = max(
        float(
            math.sqrt(
                np.average(
                    np.square(residual / standard_deviation), weights=weights
                )
            )
        ),
        minimum_standard_deviation,
    )
    standardized = np.abs(residual) / (standard_deviation * scale)
    return IntervalCalibration(
        standard_deviation_scale=scale,
        quantile_80=_weighted_quantile(standardized, weights, 0.80),
        quantile_95=_weighted_quantile(standardized, weights, 0.95),
    )


def _weighted_quantile(
    values: np.ndarray,
    weights: np.ndarray,
    probability: float,
) -> float:
    observed = np.asarray(values, dtype=float)
    observed_weights = np.asarray(weights, dtype=float)
    if (
        observed.shape != observed_weights.shape
        or not np.isfinite(observed).all()
        or not np.isfinite(observed_weights).all()
        or np.any(observed_weights <= 0.0)
        or not 0.0 < probability < 1.0
    ):
        raise LoopNativeModelError("Interval calibration inputs are invalid")
    order = np.argsort(observed, kind="stable")
    cumulative = np.cumsum(observed_weights[order])
    threshold = probability * cumulative[-1]
    position = min(
        int(np.searchsorted(cumulative, threshold, side="left")),
        len(order) - 1,
    )
    return float(observed[order[position]])


def _compare_models(
    assessment: pd.DataFrame,
    *,
    bsgp: FiniteBasisGP,
    standard_gp: FiniteBasisGP,
    constant_residual: float,
    interval: IntervalCalibration,
    route_intercepts: Mapping[str, float],
) -> Mapping[str, object]:
    observed = _normalized_price(assessment)
    black_scholes = _target(assessment, "black_scholes_price") / _target(
        assessment, "underlying_price"
    )
    residual, raw_std = bsgp.predict(assessment)
    symbols = assessment["symbol"].astype("string").str.strip().str.upper()
    residual = residual + symbols.map(route_intercepts).fillna(0.0).to_numpy(dtype=float)
    standard, _standard_std = standard_gp.predict(assessment)
    predictions = {
        "bsgp": black_scholes + residual,
        "black_scholes": black_scholes,
        "constant_residual": black_scholes + constant_residual,
        "standard_gp": standard,
    }
    weights = surface_weights(assessment)
    metrics: dict[str, object] = {}
    for name, predicted in predictions.items():
        error = np.asarray(predicted) - observed
        metrics[name] = {
            "normalized_rmse": float(
                math.sqrt(np.average(np.square(error), weights=weights))
            ),
            "normalized_mae": float(
                np.average(np.abs(error), weights=weights)
            ),
        }
    calibrated_std = np.maximum(
        raw_std * interval.standard_deviation_scale,
        1e-12,
    )
    standardized = np.abs(observed - predictions["bsgp"]) / calibrated_std
    metrics["bsgp"] = {
        **dict(metrics["bsgp"]),
        "interval_80_coverage": float(
            np.average(standardized <= interval.quantile_80, weights=weights)
        ),
        "interval_95_coverage": float(
            np.average(standardized <= interval.quantile_95, weights=weights)
        ),
    }
    return {
        "status": "SESSION_BLOCKED_OFFLINE_ASSESSMENT_COMPLETE",
        "rows": len(assessment),
        "surfaces": _surface_count(assessment),
        "sessions": _distinct_sessions(assessment),
        "models": metrics,
        "beats_black_scholes_normalized_rmse": bool(
            metrics["bsgp"]["normalized_rmse"]
            < metrics["black_scholes"]["normalized_rmse"]
        ),
        "beats_constant_residual_normalized_rmse": bool(
            metrics["bsgp"]["normalized_rmse"]
            < metrics["constant_residual"]["normalized_rmse"]
        ),
        "beats_standard_gp_normalized_rmse": bool(
            metrics["bsgp"]["normalized_rmse"]
            < metrics["standard_gp"]["normalized_rmse"]
        ),
        "assessment_used_for_training": False,
        "assessment_used_for_calibration": False,
    }


def _publish_generation(
    root: Path,
    *,
    models: Mapping[str, PooledSideModel],
    manifest_base: Mapping[str, object],
    published_at: pd.Timestamp,
) -> LoopNativeModelGeneration:
    parent = root / "ml" / "option-pricing-loop-native-models" / "generations"
    parent.mkdir(parents=True, exist_ok=True)
    base = published_at.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = parent / base
    suffix = 2
    while destination.exists():
        destination = parent / f"{base}-{suffix}"
        suffix += 1
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.tmp-{os.getpid()}-", dir=parent
        )
    )
    try:
        model_path = staging / LOOP_NATIVE_MODEL_FILE
        temporary_model = model_path.with_suffix(".joblib.tmp")
        joblib.dump(dict(models), temporary_model)
        temporary_model.replace(model_path)
        model_inventory = {
            "path": LOOP_NATIVE_MODEL_FILE,
            "size": model_path.stat().st_size,
            "checksum_sha256": file_checksum(model_path),
        }
        manifest = {
            **dict(manifest_base),
            "model_file": model_inventory,
        }
        _write_json(staging / LOOP_NATIVE_MODEL_MANIFEST, manifest)
        manifest_checksum = file_checksum(staging / LOOP_NATIVE_MODEL_MANIFEST)
        generation_hash = semantic_metadata_fingerprint(
            {
                "run_path": destination.relative_to(root).as_posix(),
                "manifest_checksum_sha256": manifest_checksum,
                "model_checksum_sha256": model_inventory["checksum_sha256"],
            }
        )
        receipt = {
            "schema_version": LOOP_NATIVE_MODEL_RECEIPT_VERSION,
            "run_path": destination.relative_to(root).as_posix(),
            "published_at": manifest["published_at"],
            "effective_from": manifest["effective_from"],
            "trained_through": manifest["trained_through"],
            "expires_at": manifest["expires_at"],
            "manifest_checksum_sha256": manifest_checksum,
            "model_checksum_sha256": model_inventory["checksum_sha256"],
            "generation_hash_sha256": generation_hash,
            "automated_action_allowed": False,
        }
        _write_json(staging / LOOP_NATIVE_MODEL_RECEIPT, receipt)
        staging.replace(destination)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    generation = read_loop_native_model_generation(
        destination,
        datastore_root=root,
    )
    pointer_record = {
        "run_path": destination.relative_to(root).as_posix(),
        "published_at": generation.published_at.isoformat(),
        "effective_from": generation.effective_from.isoformat(),
        "trained_through": generation.trained_through.isoformat(),
        "expires_at": generation.expires_at.isoformat(),
        "generation_hash_sha256": generation.generation_hash,
        "manifest_checksum_sha256": file_checksum(
            destination / LOOP_NATIVE_MODEL_MANIFEST
        ),
        "receipt_checksum_sha256": file_checksum(
            destination / LOOP_NATIVE_MODEL_RECEIPT
        ),
    }
    _write_json_atomic(
        loop_native_model_pointer_path(root),
        {
            "schema_version": LOOP_NATIVE_MODEL_POINTER_VERSION,
            "current": pointer_record,
        },
    )
    observed = read_current_loop_native_model_generation(root)
    if observed.directory != destination:
        raise LoopNativeModelError("Model pointer disagrees after publication")
    return observed


def loop_native_model_pointer_path(datastore_root: Path) -> Path:
    return (
        Path(datastore_root)
        / "ml"
        / "option-pricing-loop-native-models"
        / "latest.json"
    )


def read_current_loop_native_model_generation(
    datastore_root: Path,
) -> LoopNativeModelGeneration:
    root = Path(datastore_root).resolve()
    pointer_path = loop_native_model_pointer_path(root)
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LoopNativeModelError("Loop-native model pointer is unreadable") from exc
    if (
        not isinstance(pointer, Mapping)
        or pointer.get("schema_version") != LOOP_NATIVE_MODEL_POINTER_VERSION
        or not isinstance(pointer.get("current"), Mapping)
    ):
        raise LoopNativeModelError("Loop-native model pointer is malformed")
    record = pointer["current"]
    expected = {
        "run_path",
        "published_at",
        "effective_from",
        "trained_through",
        "expires_at",
        "generation_hash_sha256",
        "manifest_checksum_sha256",
        "receipt_checksum_sha256",
    }
    if set(record) != expected:
        raise LoopNativeModelError("Loop-native model pointer fields changed")
    directory = _model_directory(root, record.get("run_path"))
    generation = read_loop_native_model_generation(directory, datastore_root=root)
    observed = {
        "run_path": directory.relative_to(root).as_posix(),
        "published_at": generation.published_at.isoformat(),
        "effective_from": generation.effective_from.isoformat(),
        "trained_through": generation.trained_through.isoformat(),
        "expires_at": generation.expires_at.isoformat(),
        "generation_hash_sha256": generation.generation_hash,
        "manifest_checksum_sha256": file_checksum(
            directory / LOOP_NATIVE_MODEL_MANIFEST
        ),
        "receipt_checksum_sha256": file_checksum(
            directory / LOOP_NATIVE_MODEL_RECEIPT
        ),
    }
    if dict(record) != observed:
        raise LoopNativeModelError("Loop-native model pointer disagrees with generation")
    return generation


def read_loop_native_model_generation(
    directory: Path,
    *,
    datastore_root: Path,
) -> LoopNativeModelGeneration:
    root = Path(datastore_root).resolve()
    run = _model_directory(root, Path(directory).resolve().relative_to(root))
    manifest_path = run / LOOP_NATIVE_MODEL_MANIFEST
    receipt_path = run / LOOP_NATIVE_MODEL_RECEIPT
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LoopNativeModelError("Loop-native model metadata is unreadable") from exc
    if not isinstance(manifest, Mapping) or not isinstance(receipt, Mapping):
        raise LoopNativeModelError("Loop-native model metadata is malformed")
    model_metadata = manifest.get("model_file")
    model_path = run / LOOP_NATIVE_MODEL_FILE
    scope = manifest.get("scope")
    expected_scope = {
        "pooling": "one-pooled-model-per-call-put",
        "symbols": list(LOOP_NATIVE_SYMBOLS),
        "call_puts": list(LOOP_NATIVE_CALL_PUTS),
        "routes": [
            {"symbol": symbol, "call_put": call_put}
            for symbol in LOOP_NATIVE_SYMBOLS
            for call_put in LOOP_NATIVE_CALL_PUTS
        ],
    }
    if (
        manifest.get("schema_version") != LOOP_NATIVE_MODEL_GENERATION_SCHEMA_VERSION
        or manifest.get("policy_version") != LOOP_NATIVE_MODEL_POLICY_VERSION
        or manifest.get("shadow_schema_version") != LOOP_NATIVE_SHADOW_SCHEMA_VERSION
        or manifest.get("scope") != expected_scope
        or manifest.get("automated_action_allowed") is not False
        or receipt.get("schema_version") != LOOP_NATIVE_MODEL_RECEIPT_VERSION
        or receipt.get("run_path") != run.relative_to(root).as_posix()
        or receipt.get("automated_action_allowed") is not False
        or receipt.get("manifest_checksum_sha256") != file_checksum(manifest_path)
        or not isinstance(model_metadata, Mapping)
        or model_metadata.get("path") != LOOP_NATIVE_MODEL_FILE
        or not model_path.is_file()
        or int(model_metadata.get("size", -1)) != model_path.stat().st_size
        or model_metadata.get("checksum_sha256") != file_checksum(model_path)
        or receipt.get("model_checksum_sha256") != file_checksum(model_path)
    ):
        raise LoopNativeModelError("Loop-native model generation verification failed")
    generation_hash = semantic_metadata_fingerprint(
        {
            "run_path": run.relative_to(root).as_posix(),
            "manifest_checksum_sha256": file_checksum(manifest_path),
            "model_checksum_sha256": file_checksum(model_path),
        }
    )
    if receipt.get("generation_hash_sha256") != generation_hash:
        raise LoopNativeModelError("Loop-native generation hash mismatch")
    for name in ("published_at", "effective_from", "trained_through", "expires_at"):
        if receipt.get(name) != manifest.get(name):
            raise LoopNativeModelError(f"Loop-native model {name} metadata disagrees")
    published = utc_timestamp(manifest.get("published_at"))
    effective_from = utc_timestamp(manifest.get("effective_from"))
    trained_through = utc_timestamp(manifest.get("trained_through"))
    expires_at = utc_timestamp(manifest.get("expires_at"))
    cutoff = utc_timestamp(manifest.get("trainer_cutoff"))
    if not trained_through < cutoff < published or effective_from < published:
        raise LoopNativeModelError("Loop-native model causal chronology is invalid")
    materialization = manifest.get("materialization")
    if not isinstance(materialization, Mapping):
        raise LoopNativeModelError("Model materialization lineage is missing")
    materialization_run = _materialization_directory(
        root, materialization.get("run_path")
    )
    verified_materialization = read_loop_native_schwab_materialization(
        materialization_run,
        datastore_root=root,
        load_samples=False,
    )
    materialization_published_at = utc_timestamp(
        verified_materialization.receipt.get("published_at")
        if verified_materialization.receipt is not None
        else None
    )
    if materialization_published_at >= published:
        raise LoopNativeModelError(
            "Model generation does not strictly follow its immutable materialization"
        )
    if (
        materialization.get("published_at")
        != materialization_published_at.isoformat()
        or materialization.get("manifest_checksum_sha256")
        != file_checksum(materialization_run / "manifest.json")
        or materialization.get("receipt_checksum_sha256")
        != file_checksum(materialization_run / "receipt.json")
        or materialization.get("sample_checksum_sha256")
        != file_checksum(materialization_run / SCHWAB_MATERIALIZATION_SAMPLE_NAME)
        or materialization.get("sample_rows")
        != int(verified_materialization.manifest.get("sample_rows", -1))
        or materialization.get("selected_input_receipts")
        != verified_materialization.manifest.get("selected_input_receipts")
    ):
        raise LoopNativeModelError("Model materialization lineage changed")
    try:
        loaded = joblib.load(model_path)
    except Exception as exc:
        raise LoopNativeModelError("Loop-native model payload is unreadable") from exc
    if not isinstance(loaded, Mapping) or set(loaded) != set(LOOP_NATIVE_CALL_PUTS):
        raise LoopNativeModelError("Loop-native model payload scope changed")
    models: dict[str, PooledSideModel] = {}
    for call_put in LOOP_NATIVE_CALL_PUTS:
        model = loaded.get(call_put)
        if not isinstance(model, PooledSideModel) or model.call_put != call_put:
            raise LoopNativeModelError("Loop-native pooled side model is malformed")
        if set(model.route_support_sessions) != set(LOOP_NATIVE_SYMBOLS):
            raise LoopNativeModelError("Loop-native route support scope is incomplete")
        models[call_put] = model
    return LoopNativeModelGeneration(
        directory=run,
        published_at=published,
        effective_from=effective_from,
        trained_through=trained_through,
        expires_at=expires_at,
        models=models,
        manifest=manifest,
        receipt=receipt,
        generation_hash=generation_hash,
    )


def load_prior_loop_native_model(
    datastore_root: Path,
    *,
    prediction_created_at: object,
) -> LoopNativeModelLoad:
    """Load only a fully verified generation published strictly before prediction."""

    created = utc_timestamp(prediction_created_at)
    pointer = loop_native_model_pointer_path(datastore_root)
    if not pointer.is_file():
        return LoopNativeModelLoad(
            None,
            "BASELINE_FALLBACK_NO_MODEL",
            "No immutable Loop-native model generation is published.",
        )
    try:
        generation = read_current_loop_native_model_generation(datastore_root)
    except Exception as exc:
        return LoopNativeModelLoad(
            None,
            "BASELINE_FALLBACK_NO_MODEL",
            f"Model verification failed closed: {type(exc).__name__}: {exc}",
        )
    if generation.published_at >= created:
        return LoopNativeModelLoad(
            None,
            "BASELINE_FALLBACK_NO_MODEL",
            "The current model was not published strictly before prediction creation.",
        )
    if generation.effective_from > created:
        return LoopNativeModelLoad(
            None,
            "BASELINE_FALLBACK_NO_MODEL",
            "The current model is not yet effective for this prediction.",
        )
    if generation.expires_at <= created:
        return LoopNativeModelLoad(
            None,
            "BASELINE_FALLBACK_STALE_MODEL",
            "The earlier model generation exceeded its immutable maximum age.",
        )
    return LoopNativeModelLoad(generation, "MODEL_VERIFIED", "")


def predict_loop_native_residuals(
    generation: LoopNativeModelGeneration,
    rows: pd.DataFrame,
    *,
    policy: LoopNativeModelPolicy | None = None,
) -> pd.DataFrame:
    """Return causal pooled residual diagnostics aligned one-for-one with rows."""

    effective = policy or LoopNativeModelPolicy()
    if rows.empty:
        return pd.DataFrame(index=rows.index)
    required = {"symbol", "call_put", *SEMANTIC_FEATURE_COLUMNS}
    if missing := sorted(required.difference(rows.columns)):
        raise LoopNativeModelError(
            "Shadow inference rows are missing: " + ", ".join(missing)
        )
    output = pd.DataFrame(index=rows.index)
    fallback_std = (
        effective.black_scholes_fallback_standard_deviation_normalized
    )
    output["normalized_residual"] = 0.0
    output["predictive_standard_deviation_normalized"] = fallback_std
    output["width_80_normalized"] = fallback_std * 1.2815515655446004
    output["width_95_normalized"] = fallback_std * 1.959963984540054
    output["status"] = "BASELINE_FALLBACK_INPUT_UNAVAILABLE"
    output["reason"] = "Causal BSGP features are unavailable."
    output["support_status"] = "INPUT_UNAVAILABLE"
    output["support_distance"] = np.nan
    output["shrinkage"] = 0.0
    output["route_support_sessions"] = 0
    for call_put in LOOP_NATIVE_CALL_PUTS:
        mask = (
            rows["call_put"].astype("string").str.strip().str.upper().eq(call_put)
        )
        if not mask.any():
            continue
        side_rows = rows.loc[mask]
        model = generation.models[call_put]
        try:
            matrix = derived_feature_matrix(side_rows)
            raw_mean, raw_std = model.bsgp.predict(side_rows)
        except Exception as exc:
            output.loc[mask, "reason"] = (
                f"Causal BSGP input validation failed: {type(exc).__name__}: {exc}"
            )
            continue
        span = np.maximum(model.support_maximum - model.support_minimum, 1e-12)
        below = np.maximum(model.support_minimum - matrix, 0.0) / span
        above = np.maximum(matrix - model.support_maximum, 0.0) / span
        distances = np.maximum(below, above).max(axis=1)
        within_support = distances <= effective.support_margin_fraction
        symbols = (
            side_rows["symbol"].astype("string").str.strip().str.upper()
        )
        route_sessions = symbols.map(model.route_support_sessions).fillna(0).astype(int)
        route_intercepts = symbols.map(model.route_intercepts).fillna(0.0).to_numpy(
            dtype=float
        )
        route_shrinkage = np.minimum(
            1.0,
            route_sessions.to_numpy(dtype=float)
            / float(effective.minimum_route_support_sessions),
        )
        calibrated_std = np.maximum(
            raw_std * model.interval_calibration.standard_deviation_scale,
            effective.minimum_predictive_standard_deviation,
        )
        uncertainty_ok = (
            calibrated_std
            <= effective.maximum_predictive_standard_deviation_normalized
        )
        ready = within_support & uncertainty_ok & model.calibrated & (route_sessions > 0)
        shrinkage = np.where(ready, route_shrinkage, 0.0)
        residual = (raw_mean + route_intercepts * route_shrinkage) * shrinkage
        uncertainty_floor = fallback_std * (1.0 - shrinkage)
        effective_std = np.maximum(calibrated_std, uncertainty_floor)
        effective_width_80 = np.maximum(
            calibrated_std * model.interval_calibration.quantile_80,
            uncertainty_floor * 1.2815515655446004,
        )
        effective_width_95 = np.maximum(
            calibrated_std * model.interval_calibration.quantile_95,
            uncertainty_floor * 1.959963984540054,
        )
        side_index = side_rows.index
        output.loc[side_index, "normalized_residual"] = residual
        output.loc[
            side_index, "predictive_standard_deviation_normalized"
        ] = effective_std
        output.loc[side_index, "width_80_normalized"] = effective_width_80
        output.loc[side_index, "width_95_normalized"] = effective_width_95
        output.loc[side_index, "support_distance"] = distances
        output.loc[side_index, "shrinkage"] = shrinkage
        output.loc[side_index, "route_support_sessions"] = route_sessions.to_numpy()
        output.loc[side_index, "support_status"] = np.where(
            within_support, "WITHIN_TRAINING_SUPPORT", "OUT_OF_TRAINING_SUPPORT"
        )
        statuses = np.full(len(side_rows), "BSGP_SHADOW_READY", dtype=object)
        reasons = np.full(len(side_rows), "", dtype=object)
        statuses[~within_support] = "BASELINE_FALLBACK_OUT_OF_SUPPORT"
        reasons[~within_support] = "Input lies outside immutable training support bounds."
        uncertain = within_support & ~uncertainty_ok
        statuses[uncertain] = "BASELINE_FALLBACK_OUT_OF_SUPPORT"
        reasons[uncertain] = "Posterior uncertainty exceeds the configured support limit."
        uncalibrated = within_support & uncertainty_ok & (not model.calibrated)
        statuses[uncalibrated] = "BASELINE_FALLBACK_UNCALIBRATED"
        reasons[uncalibrated] = (
            "Independent interval-calibration sessions remain below policy."
        )
        unsupported_route = (
            within_support & uncertainty_ok & model.calibrated & (route_sessions <= 0)
        )
        statuses[unsupported_route] = "BASELINE_FALLBACK_OUT_OF_SUPPORT"
        reasons[unsupported_route] = "This symbol/side has no independent training session."
        output.loc[side_index, "status"] = statuses
        output.loc[side_index, "reason"] = reasons
    return output


def _bounded_surface_rows(frame: pd.DataFrame, *, maximum_rows: int) -> pd.DataFrame:
    if len(frame) <= maximum_rows:
        return frame.reset_index(drop=True)
    group_columns = ["symbol", "target_snapshot_for", "call_put"]
    groups = list(frame.groupby(group_columns, sort=True, dropna=False))
    per_surface = max(1, maximum_rows // len(groups))
    selected: list[pd.DataFrame] = []
    for _key, surface in groups:
        ordered = surface.sort_values(
            ["expiration_date", "strike", "contract_symbol"], kind="stable"
        )
        if len(ordered) <= per_surface:
            selected.append(ordered)
            continue
        positions = np.linspace(0, len(ordered) - 1, per_surface, dtype=int)
        selected.append(ordered.iloc[np.unique(positions)])
    output = pd.concat(selected, ignore_index=True, sort=False)
    if len(output) > maximum_rows:
        output = output.iloc[:maximum_rows].copy()
    return output.reset_index(drop=True)


def _side(frame: pd.DataFrame, call_put: str) -> pd.DataFrame:
    return frame.loc[
        frame["call_put"].astype("string").str.strip().str.upper().eq(call_put)
    ].reset_index(drop=True)


def _target(frame: pd.DataFrame, column: str) -> np.ndarray:
    values = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise LoopNativeModelError(f"Model target {column} contains non-finite values")
    return values


def _normalized_price(frame: pd.DataFrame) -> np.ndarray:
    return _target(frame, "observed_mid") / _target(frame, "underlying_price")


def _distinct_sessions(frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    targets = pd.to_datetime(
        frame["target_snapshot_for"], utc=True, errors="coerce"
    ).dropna()
    return int(targets.dt.tz_convert("America/New_York").dt.date.nunique())


def _surface_count(frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    return int(
        frame[["symbol", "target_snapshot_for", "call_put"]]
        .drop_duplicates()
        .shape[0]
    )


def _surface_weight_report(
    *,
    input_rows: int,
    duplicate_rows_collapsed: int,
    train: pd.DataFrame,
    calibration: pd.DataFrame,
    assessment: pd.DataFrame,
) -> Mapping[str, object]:
    partitions: dict[str, object] = {}
    for name, frame in (
        ("training", train),
        ("calibration", calibration),
        ("assessment", assessment),
    ):
        weights = surface_weights(frame)
        sums = (
            pd.DataFrame(
                {
                    "symbol": frame["symbol"].to_numpy(),
                    "target_snapshot_for": frame["target_snapshot_for"].to_numpy(),
                    "call_put": frame["call_put"].to_numpy(),
                    "weight": weights,
                }
            )
            .groupby(["symbol", "target_snapshot_for", "call_put"], sort=False)[
                "weight"
            ]
            .sum()
        )
        partitions[name] = {
            "rows": len(frame),
            "surfaces": len(sums),
            "sessions": _distinct_sessions(frame),
            "minimum_surface_total_weight": float(sums.min()),
            "maximum_surface_total_weight": float(sums.max()),
        }
    return {
        "policy": LOOP_NATIVE_SURFACE_WEIGHTING_POLICY_VERSION,
        "input_rows": input_rows,
        "duplicate_rows_collapsed": duplicate_rows_collapsed,
        "partitions": partitions,
    }


def _route_statistics(frame: pd.DataFrame) -> Mapping[str, object]:
    routes: dict[str, object] = {}
    for symbol in LOOP_NATIVE_SYMBOLS:
        for call_put in LOOP_NATIVE_CALL_PUTS:
            route = frame.loc[
                frame["symbol"].astype("string").eq(symbol)
                & frame["call_put"].astype("string").eq(call_put)
            ]
            routes[f"{symbol}/{call_put.lower()}"] = {
                "status": "PRESENT" if not route.empty else "MISSING_RETAINED",
                "rows": len(route),
                "surfaces": _surface_count(route),
                "sessions": _distinct_sessions(route),
            }
    return routes


def _evidence_counts(frame: pd.DataFrame) -> Mapping[str, object]:
    output: dict[str, object] = {}
    for lane in (
        OFFLINE_OPRA_BACKFILL,
        OFFLINE_SCHWAB_BOOTSTRAP,
        PROSPECTIVE_OPRA,
        PROSPECTIVE_SCHWAB,
    ):
        selected = frame.loc[frame["evidence_lane"].astype("string").eq(lane)]
        output[lane] = {
            "rows": len(selected),
            "surfaces": _surface_count(selected),
            "sessions": _distinct_sessions(selected),
            "increments_prospective_count": lane == PROSPECTIVE_OPRA,
        }
    return output


def _library_versions() -> Mapping[str, str]:
    packages = ("numpy", "pandas", "scikit-learn", "scipy", "joblib", "pyarrow")
    versions: dict[str, str] = {"python": platform.python_version()}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "NOT_INSTALLED"
    return versions


def _regression_manifest(regression: BayesianRidge) -> Mapping[str, object]:
    coefficients = np.asarray(regression.coef_, dtype=float)
    return {
        "alpha": float(regression.alpha_),
        "lambda": float(regression.lambda_),
        "intercept": float(regression.intercept_),
        "coefficient_count": len(coefficients),
        "coefficients": coefficients.tolist(),
    }


def _model_directory(root: Path, raw: object) -> Path:
    relative = Path(str(raw))
    directory = (root / relative).resolve()
    allowed = (
        root / "ml" / "option-pricing-loop-native-models" / "generations"
    ).resolve()
    if relative.is_absolute() or directory.parent != allowed:
        raise LoopNativeModelError("Loop-native model path escapes its immutable root")
    return directory


def _materialization_directory(root: Path, raw: object) -> Path:
    relative = Path(str(raw))
    directory = (root / relative).resolve()
    allowed = (root / "ml" / "option-pricing-loop-native-materializations").resolve()
    if relative.is_absolute() or directory.parent != allowed:
        raise LoopNativeModelError("Materialization lineage path escapes its root")
    return directory


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + f".tmp-{os.getpid()}")
    try:
        _write_json(temporary, payload)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "LOOP_NATIVE_MODEL_GENERATION_SCHEMA_VERSION",
    "LoopNativeModelError",
    "LoopNativeModelGeneration",
    "LoopNativeModelLoad",
    "LoopNativePartitions",
    "PooledSideModel",
    "load_prior_loop_native_model",
    "loop_native_model_pointer_path",
    "partition_loop_native_samples",
    "predict_loop_native_residuals",
    "read_current_loop_native_model_generation",
    "read_loop_native_model_generation",
    "surface_weights",
    "train_loop_native_shadow_generation",
]
