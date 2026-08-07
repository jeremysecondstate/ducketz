from __future__ import annotations

import importlib.metadata
import json
import math
import platform
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
from sklearn.kernel_approximation import Nystroem
from sklearn.linear_model import BayesianRidge
from sklearn.preprocessing import RobustScaler

from ml.artifacts import create_timestamp_directory, file_checksum, utc_timestamp
from ml.option_pricing.causal import model_feature_frame
from ml.option_pricing.policies import (
    BSGPModelPolicy,
    DERIVED_FEATURE_COLUMNS,
    OPTION_PRICING_CONTRACT_POLICY_VERSION,
    OPTION_PRICING_DIVIDEND_POLICY_VERSION,
    OPTION_PRICING_EXPIRATION_POLICY_VERSION,
    OPTION_PRICING_FEATURE_CONTRACT_VERSION,
    OPTION_PRICING_POLICY_VERSION,
    OPTION_PRICING_PROJECTION_POLICY_VERSION,
    OPTION_PRICING_RATE_POLICY_VERSION,
    OPTION_PRICING_TIMING_POLICY_VERSION,
    OPTION_PRICING_UNCERTAINTY_POLICY_VERSION,
    OPTION_PRICING_VOLATILITY_POLICY_VERSION,
    OPTION_PRICING_WEIGHTING_POLICY_VERSION,
    PricingPartitionConfig,
    ProjectionPolicy,
    SEMANTIC_FEATURE_COLUMNS,
)


@dataclass(frozen=True)
class PricingPartitions:
    train: pd.DataFrame
    calibration: pd.DataFrame
    assessment: pd.DataFrame
    train_clusters: int
    calibration_clusters: int
    assessment_clusters: int
    boundary_purged_rows: int
    lockbox_rows: int
    lockbox_clusters: int
    lockbox_start: pd.Timestamp
    lockbox_end: pd.Timestamp
    first_training_cluster: pd.Timestamp
    lockbox_route_cluster_counts: Mapping[tuple[str, str], int]
    lockbox_route_row_counts: Mapping[tuple[str, str], int]


@dataclass(frozen=True)
class IntervalCalibration:
    standard_deviation_scale: float
    quantile_80: float
    quantile_95: float


@dataclass(frozen=True)
class FiniteBasisGP:
    scaler: RobustScaler
    basis: Nystroem
    regression: BayesianRidge
    gamma: float
    component_count: int

    def predict(self, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        matrix = derived_feature_matrix(rows)
        transformed = self.basis.transform(self.scaler.transform(matrix))
        mean, standard_deviation = self.regression.predict(
            transformed,
            return_std=True,
        )
        mean = np.asarray(mean, dtype=float)
        standard_deviation = np.asarray(standard_deviation, dtype=float)
        if not np.isfinite(mean).all() or not np.isfinite(standard_deviation).all():
            raise ValueError("Finite-basis GP produced non-finite predictions")
        return mean, standard_deviation


@dataclass(frozen=True)
class PricingRouteModel:
    symbol: str
    call_put: str
    bsgp: FiniteBasisGP
    standard_gp: FiniteBasisGP
    interval_calibration: IntervalCalibration
    constant_residual: float
    artifact_directory: Path
    offline_evaluation: Mapping[str, object]
    reused: bool

    @property
    def model_version(self) -> str:
        return self.artifact_directory.name

    def predict_residual(
        self,
        rows: pd.DataFrame,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        mean, raw_standard_deviation = self.bsgp.predict(rows)
        calibrated_standard_deviation = np.maximum(
            raw_standard_deviation
            * self.interval_calibration.standard_deviation_scale,
            0.0,
        )
        width_80 = calibrated_standard_deviation * self.interval_calibration.quantile_80
        width_95 = calibrated_standard_deviation * self.interval_calibration.quantile_95
        return mean, calibrated_standard_deviation, width_80, width_95


def partition_pricing_samples(
    samples: pd.DataFrame,
    *,
    config: PricingPartitionConfig | None = None,
) -> PricingPartitions:
    """Create purged chronological partitions by complete target snapshot."""

    policy = config or PricingPartitionConfig()
    required = {
        "symbol",
        "call_put",
        "target_snapshot_for",
        "source_snapshot_for",
        "contract_symbol",
        "observed_available_at",
        "sample_status",
        "normalized_residual",
        "observed_mid",
        "black_scholes_price",
        "underlying_price",
    }
    if missing := sorted(required.difference(samples.columns)):
        raise ValueError("Pricing samples are missing columns: " + ", ".join(missing))
    eligible = samples.loc[samples["sample_status"].eq("AVAILABLE")].copy()
    if eligible.empty:
        raise ValueError("No complete causal pricing samples are available")
    for column in (
        "target_snapshot_for",
        "source_snapshot_for",
        "observed_available_at",
    ):
        eligible[column] = pd.to_datetime(eligible[column], utc=True, errors="coerce")
    if eligible[["target_snapshot_for", "source_snapshot_for", "observed_available_at"]].isna().any(axis=None):
        raise ValueError("Pricing samples contain invalid causal timestamps")
    if not eligible["source_snapshot_for"].lt(eligible["target_snapshot_for"]).all():
        raise ValueError("Pricing samples contain a non-lagged option surface")
    normalized_key = pd.DataFrame(
        {
            "symbol": eligible["symbol"].astype("string").str.strip().str.upper(),
            "target_snapshot_for": eligible["target_snapshot_for"],
            "contract_symbol": eligible["contract_symbol"].astype("string").str.strip(),
        },
        index=eligible.index,
    )
    if normalized_key.isna().any(axis=None) or normalized_key[["symbol", "contract_symbol"]].eq("").any(axis=None):
        raise ValueError("Pricing samples contain incomplete natural keys")
    if normalized_key.duplicated().any():
        raise ValueError("Pricing samples contain duplicate natural contract targets")

    clusters = pd.Index(
        eligible["target_snapshot_for"].drop_duplicates().sort_values()
    )
    total_required = (
        policy.minimum_train_clusters
        + policy.calibration_clusters
        + policy.assessment_clusters
        + policy.lockbox_clusters
    )
    if len(clusters) < total_required:
        raise ValueError(
            "Insufficient pricing target-snapshot clusters: "
            f"required {total_required}, observed {len(clusters)}"
        )
    lockbox_values = clusters[-policy.lockbox_clusters :]
    lockbox_start = pd.Timestamp(lockbox_values[0])
    assessment_candidates = eligible.loc[
        eligible["target_snapshot_for"].lt(lockbox_start)
        & eligible["observed_available_at"].lt(lockbox_start)
    ]
    assessment_values = pd.Index(
        assessment_candidates["target_snapshot_for"].drop_duplicates().sort_values()
    )[-policy.assessment_clusters :]
    if len(assessment_values) < policy.assessment_clusters:
        raise ValueError("Boundary purging left too few assessment clusters")
    assessment_start = pd.Timestamp(assessment_values[0])
    calibration_candidates = eligible.loc[
        eligible["target_snapshot_for"].lt(assessment_start)
        & eligible["observed_available_at"].lt(assessment_start)
    ]
    calibration_values = pd.Index(
        calibration_candidates["target_snapshot_for"].drop_duplicates().sort_values()
    )[-policy.calibration_clusters :]
    if len(calibration_values) < policy.calibration_clusters:
        raise ValueError("Boundary purging left too few calibration clusters")
    calibration_start = pd.Timestamp(calibration_values[0])
    train = eligible.loc[
        eligible["target_snapshot_for"].lt(calibration_start)
        & eligible["observed_available_at"].lt(calibration_start)
    ].copy()
    calibration = eligible.loc[
        eligible["target_snapshot_for"].isin(calibration_values)
        & eligible["observed_available_at"].lt(assessment_start)
    ].copy()
    assessment = eligible.loc[
        eligible["target_snapshot_for"].isin(assessment_values)
        & eligible["observed_available_at"].lt(lockbox_start)
    ].copy()
    train_count = int(train["target_snapshot_for"].nunique())
    calibration_count = int(calibration["target_snapshot_for"].nunique())
    assessment_count = int(assessment["target_snapshot_for"].nunique())
    if train_count < policy.minimum_train_clusters:
        raise ValueError("Boundary purging left too few training clusters")
    if calibration_count < policy.calibration_clusters:
        raise ValueError("Boundary purging left too few calibration clusters")
    if assessment_count < policy.assessment_clusters:
        raise ValueError("Boundary purging left too few assessment clusters")
    first_training = pd.Timestamp(train["target_snapshot_for"].min())
    if (
        policy.minimum_calendar_months
        and pd.Timestamp(lockbox_values[-1])
        < first_training + pd.DateOffset(months=policy.minimum_calendar_months)
    ):
        raise ValueError("Pricing evidence does not span the required calendar months")

    used_indices = set(train.index) | set(calibration.index) | set(assessment.index)
    purged = int(
        eligible["target_snapshot_for"].lt(lockbox_start).sum()
        - len(used_indices)
    )
    for name, frame in (
        ("training", train),
        ("calibration", calibration),
        ("assessment", assessment),
    ):
        _validate_model_values(frame, label=name)

    lockbox_metadata = eligible.loc[
        eligible["target_snapshot_for"].isin(lockbox_values),
        ["symbol", "call_put", "target_snapshot_for"],
    ].copy()
    lockbox_metadata["symbol"] = (
        lockbox_metadata["symbol"].astype("string").str.strip().str.upper()
    )
    lockbox_metadata["call_put"] = (
        lockbox_metadata["call_put"].astype("string").str.strip().str.upper()
    )
    lockbox_route_cluster_counts = {
        (str(symbol), str(call_put)): int(group["target_snapshot_for"].nunique())
        for (symbol, call_put), group in lockbox_metadata.groupby(
            ["symbol", "call_put"], sort=True
        )
    }
    lockbox_route_row_counts = {
        (str(symbol), str(call_put)): len(group)
        for (symbol, call_put), group in lockbox_metadata.groupby(
            ["symbol", "call_put"], sort=True
        )
    }

    def clean(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.sort_values(
            ["target_snapshot_for", "symbol", "call_put", "contract_symbol"],
            kind="mergesort",
        ).reset_index(drop=True)

    return PricingPartitions(
        train=clean(train),
        calibration=clean(calibration),
        assessment=clean(assessment),
        train_clusters=train_count,
        calibration_clusters=calibration_count,
        assessment_clusters=assessment_count,
        boundary_purged_rows=max(purged, 0),
        lockbox_rows=int(eligible["target_snapshot_for"].isin(lockbox_values).sum()),
        lockbox_clusters=len(lockbox_values),
        lockbox_start=lockbox_start,
        lockbox_end=pd.Timestamp(lockbox_values[-1]),
        first_training_cluster=first_training,
        lockbox_route_cluster_counts=lockbox_route_cluster_counts,
        lockbox_route_row_counts=lockbox_route_row_counts,
    )


def route_partitions(
    partitions: PricingPartitions,
    *,
    symbol: str,
    call_put: str,
    config: PricingPartitionConfig | None = None,
) -> PricingPartitions:
    """Select one independent symbol/call-put route without moving boundaries."""

    policy = config or PricingPartitionConfig()
    clean_symbol = str(symbol).strip().upper()
    clean_call_put = _call_put(call_put)

    def select(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.loc[
            frame["symbol"].astype("string").str.strip().str.upper().eq(clean_symbol)
            & frame["call_put"].astype("string").str.strip().str.upper().eq(clean_call_put)
        ].reset_index(drop=True)

    train = select(partitions.train)
    calibration = select(partitions.calibration)
    assessment = select(partitions.assessment)
    counts = tuple(
        int(frame["target_snapshot_for"].nunique())
        for frame in (train, calibration, assessment)
    )
    required = (
        policy.minimum_train_clusters,
        policy.calibration_clusters,
        policy.assessment_clusters,
    )
    if any(observed < needed for observed, needed in zip(counts, required, strict=True)):
        raise ValueError(
            f"Route {clean_symbol}/{clean_call_put.lower()} lacks required clusters: "
            f"observed={counts}, required={required}"
        )
    route_key = (clean_symbol, clean_call_put)
    route_lockbox_clusters = partitions.lockbox_route_cluster_counts.get(route_key, 0)
    if route_lockbox_clusters < policy.lockbox_clusters:
        raise ValueError(
            f"Route {clean_symbol}/{clean_call_put.lower()} lacks closed-lockbox "
            f"clusters: observed={route_lockbox_clusters}, "
            f"required={policy.lockbox_clusters}"
        )
    return PricingPartitions(
        train=train,
        calibration=calibration,
        assessment=assessment,
        train_clusters=counts[0],
        calibration_clusters=counts[1],
        assessment_clusters=counts[2],
        boundary_purged_rows=partitions.boundary_purged_rows,
        lockbox_rows=partitions.lockbox_route_row_counts.get(route_key, 0),
        lockbox_clusters=route_lockbox_clusters,
        lockbox_start=partitions.lockbox_start,
        lockbox_end=partitions.lockbox_end,
        first_training_cluster=partitions.first_training_cluster,
        lockbox_route_cluster_counts={route_key: route_lockbox_clusters},
        lockbox_route_row_counts={
            route_key: partitions.lockbox_route_row_counts.get(route_key, 0)
        },
    )


def snapshot_weights(frame: pd.DataFrame) -> np.ndarray:
    """Give every complete target snapshot total weight one."""

    if "target_snapshot_for" not in frame:
        raise ValueError("Snapshot weights require target_snapshot_for")
    clusters = pd.to_datetime(frame["target_snapshot_for"], utc=True, errors="coerce")
    if clusters.isna().any():
        raise ValueError("Snapshot weights received invalid target timestamps")
    counts = clusters.groupby(clusters).transform("count")
    weights = 1.0 / counts.to_numpy(dtype=float)
    if not np.isfinite(weights).all() or np.any(weights <= 0.0):
        raise ValueError("Snapshot weights are invalid")
    return weights


def derived_feature_matrix(rows: pd.DataFrame) -> np.ndarray:
    semantic = model_feature_frame(rows)
    s = semantic["underlying_price"].to_numpy(dtype=float)
    k = semantic["strike"].to_numpy(dtype=float)
    r = semantic["risk_free_rate"].to_numpy(dtype=float)
    sigma = semantic["lagged_implied_volatility"].to_numpy(dtype=float)
    years = semantic["target_years_to_expiration"].to_numpy(dtype=float)
    q = semantic["dividend_yield"].to_numpy(dtype=float)
    if np.any(s <= 0.0) or np.any(k <= 0.0) or np.any(sigma <= 0.0) or np.any(years <= 0.0):
        raise ValueError("Pricing model inputs violate positive-domain transforms")
    matrix = np.column_stack(
        (np.log(s), np.log(k / s), r, np.log(sigma), np.sqrt(years), q)
    )
    if not np.isfinite(matrix).all():
        raise ValueError("Derived pricing features must be finite")
    return matrix


def fit_or_reuse_pricing_model(
    datastore_root: Path,
    *,
    symbol: str,
    call_put: str,
    partitions: PricingPartitions,
    input_files: Sequence[Path],
    trained_at: object,
    model_policy: BSGPModelPolicy | None = None,
    partition_config: PricingPartitionConfig | None = None,
    projection_policy: ProjectionPolicy | None = None,
) -> PricingRouteModel:
    effective_model_policy = model_policy or BSGPModelPolicy()
    effective_partition_config = partition_config or PricingPartitionConfig()
    effective_projection = projection_policy or ProjectionPolicy()
    clean_symbol = str(symbol).strip().upper()
    clean_call_put = _call_put(call_put)
    expected = _model_configuration(
        symbol=clean_symbol,
        call_put=clean_call_put,
        partitions=partitions,
        input_files=input_files,
        datastore_root=Path(datastore_root),
        model_policy=effective_model_policy,
        partition_config=effective_partition_config,
        projection_policy=effective_projection,
    )
    model_root = (
        Path(datastore_root)
        / "ml"
        / "option-pricing-models"
        / clean_symbol
        / clean_call_put.lower()
        / "black-scholes-rbf-residual"
    )
    existing = _load_compatible_model(model_root, expected=expected)
    if existing is not None:
        return PricingRouteModel(
            symbol=clean_symbol,
            call_put=clean_call_put,
            bsgp=existing["bsgp"],
            standard_gp=existing["standard_gp"],
            interval_calibration=existing["interval_calibration"],
            constant_residual=float(existing["constant_residual"]),
            artifact_directory=existing["artifact_directory"],
            offline_evaluation=existing["offline_evaluation"],
            reused=True,
        )

    train_residual = _target(partitions.train, "normalized_residual")
    calibration_residual = _target(partitions.calibration, "normalized_residual")
    train_normalized_price = _normalized_price(partitions.train)
    calibration_normalized_price = _normalized_price(partitions.calibration)
    bsgp, bsgp_scores = _select_gamma(
        partitions.train,
        train_residual,
        partitions.calibration,
        calibration_residual,
        policy=effective_model_policy,
    )
    standard_gp, standard_scores = _select_gamma(
        partitions.train,
        train_normalized_price,
        partitions.calibration,
        calibration_normalized_price,
        policy=effective_model_policy,
    )
    calibration_mean, calibration_standard_deviation = bsgp.predict(
        partitions.calibration
    )
    interval_calibration = _calibrate_intervals(
        calibration_residual - calibration_mean,
        calibration_standard_deviation,
        snapshot_weights(partitions.calibration),
        minimum_standard_deviation=effective_model_policy.minimum_predictive_standard_deviation,
    )
    constant_residual = float(
        np.average(train_residual, weights=snapshot_weights(partitions.train))
    )
    evaluation = compare_pricing_models(
        partitions.assessment,
        bsgp=bsgp,
        standard_gp=standard_gp,
        constant_residual=constant_residual,
        interval_calibration=interval_calibration,
    )
    created = utc_timestamp(trained_at)
    directory = create_timestamp_directory(model_root, timestamp=created)
    model_path = directory / "model.joblib"
    temporary = model_path.with_suffix(".joblib.tmp")
    joblib.dump(
        {
            "bsgp": bsgp,
            "standard_gp": standard_gp,
            "interval_calibration": interval_calibration,
            "constant_residual": constant_residual,
        },
        temporary,
    )
    temporary.replace(model_path)
    manifest = {
        **expected,
        "trained_at": created.isoformat(),
        "selected_gamma": {
            "bsgp": bsgp.gamma,
            "standard_gp": standard_gp.gamma,
        },
        "gamma_calibration_scores": {
            "bsgp": bsgp_scores,
            "standard_gp": standard_scores,
        },
        "uncertainty_calibration": asdict(interval_calibration),
        "constant_residual": constant_residual,
        "offline_evaluation": evaluation,
        "model_file": {
            "path": model_path.name,
            "size": model_path.stat().st_size,
            "checksum_sha256": file_checksum(model_path),
        },
    }
    _write_json(directory / "manifest.json", manifest)
    _write_json(model_root / "latest.json", {"path": directory.name})
    return PricingRouteModel(
        symbol=clean_symbol,
        call_put=clean_call_put,
        bsgp=bsgp,
        standard_gp=standard_gp,
        interval_calibration=interval_calibration,
        constant_residual=constant_residual,
        artifact_directory=directory,
        offline_evaluation=evaluation,
        reused=False,
    )


def compare_pricing_models(
    assessment: pd.DataFrame,
    *,
    bsgp: FiniteBasisGP,
    standard_gp: FiniteBasisGP,
    constant_residual: float,
    interval_calibration: IntervalCalibration,
) -> dict[str, object]:
    """Compare all four contract-price models under snapshot weighting."""

    observed = _normalized_price(assessment)
    black_scholes = _target(assessment, "black_scholes_price") / _target(
        assessment, "underlying_price"
    )
    residual_mean, residual_standard_deviation = bsgp.predict(assessment)
    standard_mean, _standard_deviation = standard_gp.predict(assessment)
    calibrated_standard_deviation = (
        residual_standard_deviation
        * interval_calibration.standard_deviation_scale
    )
    weights = snapshot_weights(assessment)
    predictions = {
        "bsgp": black_scholes + residual_mean,
        "black_scholes": black_scholes,
        "constant_residual": black_scholes + constant_residual,
        "standard_gp": standard_mean,
    }
    metrics = {
        name: _pricing_metrics(
            assessment,
            observed=observed,
            predicted=prediction,
            weights=weights,
        )
        for name, prediction in predictions.items()
    }
    absolute_standardized = np.abs(observed - predictions["bsgp"]) / np.maximum(
        calibrated_standard_deviation,
        1e-12,
    )
    covered80 = absolute_standardized <= interval_calibration.quantile_80
    covered95 = absolute_standardized <= interval_calibration.quantile_95
    metrics["bsgp"]["interval_80_coverage"] = float(
        np.average(covered80, weights=weights)
    )
    metrics["bsgp"]["interval_95_coverage"] = float(
        np.average(covered95, weights=weights)
    )
    metrics["bsgp"]["average_interval_80_width_normalized"] = float(
        np.average(
            2.0
            * calibrated_standard_deviation
            * interval_calibration.quantile_80,
            weights=weights,
        )
    )
    metrics["bsgp"]["average_interval_95_width_normalized"] = float(
        np.average(
            2.0
            * calibrated_standard_deviation
            * interval_calibration.quantile_95,
            weights=weights,
        )
    )
    return {
        "status": "OFFLINE_ASSESSMENT_COMPLETE",
        "assessment_used_for_training": False,
        "assessment_used_for_calibration": False,
        "metric_weighting": OPTION_PRICING_WEIGHTING_POLICY_VERSION,
        "assessment_rows": len(assessment),
        "assessment_clusters": int(assessment["target_snapshot_for"].nunique()),
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
    }


def _select_gamma(
    train: pd.DataFrame,
    train_target: np.ndarray,
    calibration: pd.DataFrame,
    calibration_target: np.ndarray,
    *,
    policy: BSGPModelPolicy,
) -> tuple[FiniteBasisGP, dict[str, float]]:
    scores: dict[str, float] = {}
    selected: FiniteBasisGP | None = None
    selected_score = math.inf
    weights = snapshot_weights(calibration)
    for gamma in policy.gamma_grid:
        model = _fit_finite_basis_gp(
            train,
            train_target,
            gamma=gamma,
            policy=policy,
        )
        prediction, _standard_deviation = model.predict(calibration)
        score = float(
            math.sqrt(
                np.average(
                    np.square(prediction - calibration_target),
                    weights=weights,
                )
            )
        )
        scores[str(gamma)] = score
        if score < selected_score - 1e-15:
            selected = model
            selected_score = score
    if selected is None:
        raise RuntimeError("Gamma selection did not fit a finite-basis GP")
    return selected, scores


def _fit_finite_basis_gp(
    train: pd.DataFrame,
    target: np.ndarray,
    *,
    gamma: float,
    policy: BSGPModelPolicy,
) -> FiniteBasisGP:
    matrix = derived_feature_matrix(train)
    scaler = RobustScaler()
    scaled = scaler.fit_transform(matrix)
    components = min(policy.component_count, len(train))
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
        sample_weight=snapshot_weights(train),
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
    scale = float(
        math.sqrt(
            np.average(
                np.square(residual / standard_deviation),
                weights=weights,
            )
        )
    )
    scale = max(scale, minimum_standard_deviation)
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
        raise ValueError("Weighted quantile inputs are invalid")
    order = np.argsort(observed, kind="mergesort")
    sorted_values = observed[order]
    cumulative = np.cumsum(observed_weights[order])
    threshold = probability * cumulative[-1]
    index = min(int(np.searchsorted(cumulative, threshold, side="left")), len(sorted_values) - 1)
    return float(sorted_values[index])


def _pricing_metrics(
    frame: pd.DataFrame,
    *,
    observed: np.ndarray,
    predicted: np.ndarray,
    weights: np.ndarray,
) -> dict[str, float | int | None]:
    error = np.asarray(predicted, dtype=float) - np.asarray(observed, dtype=float)
    underlying = _target(frame, "underlying_price")
    dollar_error = error * underlying
    spread = pd.to_numeric(frame.get("bid_ask_spread"), errors="coerce").to_numpy(dtype=float)
    half_spread = spread / 2.0
    usable_spread = np.isfinite(half_spread) & (half_spread > 0.0)
    spread_error = None
    if usable_spread.any():
        spread_error = float(
            np.average(
                np.abs(dollar_error[usable_spread]) / half_spread[usable_spread],
                weights=weights[usable_spread],
            )
        )
    return {
        "rows": len(frame),
        "target_snapshot_clusters": int(frame["target_snapshot_for"].nunique()),
        "normalized_mae": float(np.average(np.abs(error), weights=weights)),
        "normalized_rmse": float(math.sqrt(np.average(np.square(error), weights=weights))),
        "dollar_mae": float(np.average(np.abs(dollar_error), weights=weights)),
        "mean_absolute_error_in_half_spreads": spread_error,
    }


def _model_configuration(
    *,
    symbol: str,
    call_put: str,
    partitions: PricingPartitions,
    input_files: Sequence[Path],
    datastore_root: Path,
    model_policy: BSGPModelPolicy,
    partition_config: PricingPartitionConfig,
    projection_policy: ProjectionPolicy,
) -> dict[str, object]:
    return {
        "model_name": "black-scholes-rbf-residual",
        "model_kind": "rbf-finite-feature-gp-approximation",
        "route": {"symbol": symbol, "call_put": call_put},
        "model_policy_version": OPTION_PRICING_POLICY_VERSION,
        "semantic_feature_contract_version": OPTION_PRICING_FEATURE_CONTRACT_VERSION,
        "semantic_feature_columns": list(SEMANTIC_FEATURE_COLUMNS),
        "derived_feature_columns": list(DERIVED_FEATURE_COLUMNS),
        "target_definition": "observed_mid_minus_black_scholes_divided_by_underlying",
        "price_scale": "underlying_price",
        "timing_policy_version": OPTION_PRICING_TIMING_POLICY_VERSION,
        "rate_policy_version": OPTION_PRICING_RATE_POLICY_VERSION,
        "dividend_policy_version": OPTION_PRICING_DIVIDEND_POLICY_VERSION,
        "volatility_policy_version": OPTION_PRICING_VOLATILITY_POLICY_VERSION,
        "expiration_policy_version": OPTION_PRICING_EXPIRATION_POLICY_VERSION,
        "contract_policy_version": OPTION_PRICING_CONTRACT_POLICY_VERSION,
        "weighting_policy_version": OPTION_PRICING_WEIGHTING_POLICY_VERSION,
        "uncertainty_policy_version": OPTION_PRICING_UNCERTAINTY_POLICY_VERSION,
        "projection_policy_version": OPTION_PRICING_PROJECTION_POLICY_VERSION,
        "kernel_policy": {
            "approximation": "sklearn-Nystroem-rbf",
            "regression": "sklearn-BayesianRidge",
            "component_count": model_policy.component_count,
            "gamma_grid": list(model_policy.gamma_grid),
            "random_state": model_policy.random_state,
            "minimum_predictive_standard_deviation": (
                model_policy.minimum_predictive_standard_deviation
            ),
        },
        "projection_policy": asdict(projection_policy),
        "partition_configuration": asdict(partition_config),
        "partition_counts": {
            "training_rows": len(partitions.train),
            "training_clusters": partitions.train_clusters,
            "calibration_rows": len(partitions.calibration),
            "calibration_clusters": partitions.calibration_clusters,
            "assessment_rows": len(partitions.assessment),
            "assessment_clusters": partitions.assessment_clusters,
            "boundary_purged_rows": partitions.boundary_purged_rows,
        },
        "partition_boundaries": {
            "training_start": pd.Timestamp(
                partitions.train["target_snapshot_for"].min()
            ).isoformat(),
            "training_end": pd.Timestamp(
                partitions.train["target_snapshot_for"].max()
            ).isoformat(),
            "calibration_start": pd.Timestamp(
                partitions.calibration["target_snapshot_for"].min()
            ).isoformat(),
            "calibration_end": pd.Timestamp(
                partitions.calibration["target_snapshot_for"].max()
            ).isoformat(),
            "assessment_start": pd.Timestamp(
                partitions.assessment["target_snapshot_for"].min()
            ).isoformat(),
            "assessment_end": pd.Timestamp(
                partitions.assessment["target_snapshot_for"].max()
            ).isoformat(),
            "closed_lockbox_start": partitions.lockbox_start.isoformat(),
            "closed_lockbox_end": partitions.lockbox_end.isoformat(),
        },
        "training_through": pd.Timestamp(partitions.train["target_snapshot_for"].max()).isoformat(),
        "closed_lockbox": {
            "status": "CLOSED_UNTOUCHED_UNSCORED",
            "rows": partitions.lockbox_rows,
            "target_snapshot_clusters": partitions.lockbox_clusters,
            "start": partitions.lockbox_start.isoformat(),
            "end": partitions.lockbox_end.isoformat(),
        },
        "input_files": _immutable_input_inventory(input_files, root=datastore_root),
        "runtime_compatibility": _runtime_compatibility(),
        "implementation_files": _implementation_inventory(),
    }


def _load_compatible_model(
    model_root: Path,
    *,
    expected: Mapping[str, object],
) -> dict[str, object] | None:
    pointer_path = model_root / "latest.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        if set(pointer) != {"path"}:
            return None
        relative = Path(str(pointer["path"]))
        if relative.is_absolute() or len(relative.parts) != 1:
            return None
        directory = (model_root / relative).resolve()
        if directory.parent != model_root.resolve():
            return None
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if any(manifest.get(key) != value for key, value in expected.items()):
            return None
        metadata = manifest.get("model_file")
        if not isinstance(metadata, Mapping):
            return None
        model_path = directory / str(metadata.get("path", ""))
        if (
            model_path.parent.resolve() != directory
            or not model_path.is_file()
            or int(metadata.get("size", -1)) != model_path.stat().st_size
            or metadata.get("checksum_sha256") != file_checksum(model_path)
        ):
            return None
        bundle = joblib.load(model_path)
        if not isinstance(bundle, Mapping):
            return None
        return {
            **bundle,
            "artifact_directory": directory,
            "offline_evaluation": manifest.get("offline_evaluation", {}),
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _immutable_input_inventory(
    paths: Sequence[Path],
    *,
    root: Path,
) -> list[dict[str, object]]:
    base = Path(root).resolve()
    records: list[dict[str, object]] = []
    for path in dict.fromkeys(Path(value) for value in paths):
        resolved = path.resolve()
        try:
            rendered = resolved.relative_to(base).as_posix()
        except ValueError:
            rendered = str(resolved)
        if not resolved.is_file():
            records.append({"path": rendered, "status": "missing"})
        else:
            records.append(
                {
                    "path": rendered,
                    "status": "present",
                    "size": resolved.stat().st_size,
                    "checksum_sha256": file_checksum(resolved),
                }
            )
    return records


def _implementation_inventory() -> list[dict[str, object]]:
    directory = Path(__file__).resolve().parent
    names = (
        "black_scholes.py",
        "causal.py",
        "constraints.py",
        "model.py",
        "policies.py",
    )
    return [
        {"path": name, "checksum_sha256": file_checksum(directory / name)}
        for name in names
    ]


def _runtime_compatibility() -> dict[str, object]:
    packages: dict[str, str | None] = {}
    for name in (
        "numpy",
        "pandas",
        "pyarrow",
        "scipy",
        "scikit-learn",
        "joblib",
    ):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "packages": packages,
    }


def _validate_model_values(frame: pd.DataFrame, *, label: str) -> None:
    model_feature_frame(frame)
    for column in (
        "normalized_residual",
        "observed_mid",
        "black_scholes_price",
        "underlying_price",
    ):
        values = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
        if not np.isfinite(values).all():
            raise ValueError(f"{label} pricing samples contain invalid {column}")


def _normalized_price(frame: pd.DataFrame) -> np.ndarray:
    return _target(frame, "observed_mid") / _target(frame, "underlying_price")


def _target(frame: pd.DataFrame, column: str) -> np.ndarray:
    values = pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError(f"Pricing target {column} must be finite")
    return values


def _call_put(value: object) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"CALL", "C"}:
        return "CALL"
    if normalized in {"PUT", "P"}:
        return "PUT"
    raise ValueError("call_put must be CALL or PUT")


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


__all__ = [
    "FiniteBasisGP",
    "IntervalCalibration",
    "PricingPartitions",
    "PricingRouteModel",
    "compare_pricing_models",
    "derived_feature_matrix",
    "fit_or_reuse_pricing_model",
    "partition_pricing_samples",
    "route_partitions",
    "snapshot_weights",
]
