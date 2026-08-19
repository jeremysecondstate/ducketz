from __future__ import annotations

import json
import importlib.metadata
import platform
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Sequence

import joblib
import numpy as np
import pandas as pd

from ml.artifacts import (
    create_timestamp_directory,
    file_checksum,
    input_inventory,
    utc_timestamp,
)
from ml.calibration import (
    IdentityCalibrator,
    ProbabilityCalibrator,
    fit_probability_calibrator,
)
from ml.horizons import (
    WEEKLY_HORIZON_ORDER,
    HorizonSpecification,
    horizon_specification,
    is_weekly_horizon,
)
from ml.model_features import (
    ModelFeatureSet,
    model_matrix_for_feature_set,
    resolve_model_feature_set,
)
from ml.models.registry import (
    DEFAULT_MODEL_PARAMETERS,
    ModelFamily,
    ModelSpec,
    build_estimator,
)
from ml.preprocessing import preprocessing_policy

TARGET_COLUMN = "target_cost_adjusted_positive"
_RUNTIME_PACKAGES = (
    "numpy",
    "pandas",
    "pyarrow",
    "scikit-learn",
    "joblib",
    "exchange-calendars",
    "lightgbm",
    "xgboost",
)


@dataclass(frozen=True)
class ModelPartitionConfig:
    minimum_train_clusters: int = 252
    calibration_clusters: int = 63
    assessment_clusters: int = 63
    lockbox_clusters: int = 126

    def __post_init__(self) -> None:
        if self.minimum_train_clusters < 1:
            raise ValueError("minimum_train_clusters must be positive")
        if self.calibration_clusters < 1:
            raise ValueError("calibration_clusters must be positive")
        if self.assessment_clusters < 1:
            raise ValueError("assessment_clusters must be positive")
        if self.lockbox_clusters < 1:
            raise ValueError("lockbox_clusters must be positive")


DEFAULT_PARTITION_CONFIGS: Mapping[str, ModelPartitionConfig] = {
    # Intraday decisions are sourced from the deliberately bounded 100-calendar-day
    # native one-minute history.  A full regular session contributes roughly five
    # target-start clusters, so the daily 504-cluster policy can never be satisfied
    # by the production input contract.  Keep the same 4:1:1:2 partition proportions
    # while leaving enough room for holiday/half-day and overlap-boundary purging.
    "1h": ModelPartitionConfig(160, 40, 40, 80),
    "4h": ModelPartitionConfig(160, 40, 40, 80),
    "1d": ModelPartitionConfig(252, 63, 63, 126),
    **{
        horizon: ModelPartitionConfig(252, 63, 63, 126)
        for horizon in WEEKLY_HORIZON_ORDER
    },
}


_AGGREGATE_WEEKLY_LOGISTIC_PARAMETERS: Mapping[str, object] = {
    "C": 0.3,
    "l1_ratio": 1.0,
    "solver": "liblinear",
    "max_iter": 5_000,
    "tol": 1e-5,
}
_AGGREGATE_WEEKLY_PLATT_PARAMETERS: Mapping[str, object] = {
    "platt_regularization_c": 0.1,
    "clip_to_observed_probability_range": True,
}


@dataclass(frozen=True)
class ModelPartitions:
    train: pd.DataFrame
    calibration: pd.DataFrame
    assessment: pd.DataFrame
    minimum_train_clusters: int
    calibration_clusters: int
    assessment_clusters: int
    lockbox_clusters: int
    lockbox_row_count: int
    lockbox_cluster_count: int
    lockbox_start: pd.Timestamp
    lockbox_end: pd.Timestamp
    lockbox_cluster_values: tuple[pd.Timestamp, ...]


@dataclass(frozen=True)
class RuntimeModel:
    model_name: str
    horizon: str
    feature_set: ModelFeatureSet
    estimator: object
    calibrator: ProbabilityCalibrator
    calibration_method: str
    artifact_directory: Path
    offline_evaluation: Mapping[str, object]
    reused: bool

    def probabilities(self, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        matrix = model_matrix_for_feature_set(rows, self.feature_set)
        raw = np.asarray(self.estimator.predict_proba(matrix)[:, 1], dtype=float)
        calibrated = np.asarray(self.calibrator.predict(raw), dtype=float)
        if (
            not np.isfinite(raw).all()
            or not np.isfinite(calibrated).all()
            or ((raw < 0.0) | (raw > 1.0)).any()
            or ((calibrated < 0.0) | (calibrated > 1.0)).any()
        ):
            raise ValueError("Model produced non-finite or out-of-range probabilities")
        return raw, calibrated


def partition_model_rows(
    samples: pd.DataFrame,
    *,
    config: ModelPartitionConfig,
    excluded_target_starts: Sequence[object] = (),
) -> ModelPartitions:
    required = {
        "id",
        "symbol",
        "decision_timestamp",
        "target_window_start",
        "target_window_end",
        "label_status",
        TARGET_COLUMN,
    }
    missing = sorted(required.difference(samples.columns))
    if missing:
        raise ValueError("Model samples are missing columns: " + ", ".join(missing))

    eligible = samples.loc[samples["label_status"].eq("COMPLETE")].copy()
    eligible["decision_timestamp"] = pd.to_datetime(
        eligible["decision_timestamp"], utc=True, errors="coerce"
    )
    eligible["target_window_start"] = pd.to_datetime(
        eligible["target_window_start"], utc=True, errors="coerce"
    )
    eligible["target_window_end"] = pd.to_datetime(
        eligible["target_window_end"], utc=True, errors="coerce"
    )
    if eligible[
        ["decision_timestamp", "target_window_start", "target_window_end"]
    ].isna().any().any():
        raise ValueError("Completed model samples contain invalid target windows")
    excluded = pd.to_datetime(
        pd.Index(tuple(excluded_target_starts)),
        utc=True,
        errors="coerce",
    )
    if excluded.isna().any():
        raise ValueError("Excluded target starts must be valid UTC timestamps")
    if len(excluded):
        eligible = eligible.loc[
            ~eligible["target_window_start"].isin(excluded)
        ].copy()
    if eligible.empty:
        raise ValueError("No completed target rows are available for training")
    if eligible["id"].duplicated().any():
        raise ValueError("Training sample id values must be unique")

    clusters = pd.Index(
        eligible["target_window_start"].drop_duplicates().sort_values()
    )
    cluster_ends = (
        eligible.groupby("target_window_start", sort=True)[
            "target_window_end"
        ]
        .max()
        .reindex(clusters)
    )
    overlap_safe_partitions = bool(
        len(clusters) > 1
        and (
            cluster_ends.iloc[:-1].reset_index(drop=True)
            >= pd.Series(clusters[1:]).reset_index(drop=True)
        ).any()
    )
    if overlap_safe_partitions:
        if len(clusters) < config.lockbox_clusters:
            raise ValueError(
                "Insufficient completed target clusters for the closed lockbox: "
                f"required {config.lockbox_clusters}, observed {len(clusters)}"
            )
        lockbox_cluster_values = clusters[-config.lockbox_clusters :]
        lockbox_boundary = pd.Timestamp(lockbox_cluster_values[0])
        assessment_candidates = eligible.loc[
            eligible["target_window_start"].lt(lockbox_boundary)
            & eligible["target_window_end"].lt(lockbox_boundary)
        ]
        assessment_candidate_clusters = pd.Index(
            assessment_candidates["target_window_start"]
            .drop_duplicates()
            .sort_values()
        )
        if len(assessment_candidate_clusters) < config.assessment_clusters:
            raise ValueError(
                "Target-window purging at the assessment-to-lockbox boundary "
                "left fewer assessment clusters than required: "
                f"required {config.assessment_clusters}, "
                f"observed {len(assessment_candidate_clusters)}"
            )
        assessment_cluster_values = assessment_candidate_clusters[
            -config.assessment_clusters :
        ]
        assessment_boundary = pd.Timestamp(assessment_cluster_values[0])
        calibration_candidates = eligible.loc[
            eligible["target_window_start"].lt(assessment_boundary)
            & eligible["target_window_end"].lt(assessment_boundary)
        ]
        calibration_candidate_clusters = pd.Index(
            calibration_candidates["target_window_start"]
            .drop_duplicates()
            .sort_values()
        )
        if len(calibration_candidate_clusters) < config.calibration_clusters:
            raise ValueError(
                "Target-window purging at the calibration-to-assessment boundary "
                "left fewer calibration clusters than required: "
                f"required {config.calibration_clusters}, "
                f"observed {len(calibration_candidate_clusters)}"
            )
        calibration_cluster_values = calibration_candidate_clusters[
            -config.calibration_clusters :
        ]
        calibration_boundary = pd.Timestamp(calibration_cluster_values[0])
        train = eligible.loc[
            eligible["target_window_start"].lt(calibration_boundary)
            & eligible["target_window_end"].lt(calibration_boundary)
        ].copy()
        calibration = eligible.loc[
            eligible["target_window_start"].isin(calibration_cluster_values)
            & eligible["target_window_end"].lt(assessment_boundary)
        ].copy()
        assessment = eligible.loc[
            eligible["target_window_start"].isin(assessment_cluster_values)
            & eligible["target_window_end"].lt(lockbox_boundary)
        ].copy()
    else:
        required_clusters = (
            config.minimum_train_clusters
            + config.calibration_clusters
            + config.assessment_clusters
            + config.lockbox_clusters
        )
        if len(clusters) < required_clusters:
            raise ValueError(
                "Insufficient completed target clusters: "
                f"required {required_clusters}, observed {len(clusters)}"
            )
        lockbox_cluster_values = clusters[-config.lockbox_clusters :]
        before_lockbox = clusters[: -config.lockbox_clusters]
        assessment_cluster_values = before_lockbox[
            -config.assessment_clusters :
        ]
        before_assessment = before_lockbox[: -config.assessment_clusters]
        calibration_cluster_values = before_assessment[
            -config.calibration_clusters :
        ]
        calibration_boundary = pd.Timestamp(calibration_cluster_values[0])
        assessment_boundary = pd.Timestamp(assessment_cluster_values[0])
        train = eligible.loc[
            eligible["target_window_start"].lt(calibration_boundary)
            & eligible["target_window_end"].lt(calibration_boundary)
        ].copy()
        calibration = eligible.loc[
            eligible["target_window_start"].isin(calibration_cluster_values)
            & eligible["target_window_end"].lt(assessment_boundary)
        ].copy()
        assessment = eligible.loc[
            eligible["target_window_start"].isin(assessment_cluster_values)
        ].copy()
    lockbox_mask = eligible["target_window_start"].isin(
        lockbox_cluster_values
    )
    for name, frame in (
        ("training", train),
        ("calibration", calibration),
        ("assessment", assessment),
    ):
        target = pd.to_numeric(frame[TARGET_COLUMN], errors="coerce")
        if target.isna().any():
            raise ValueError(f"{name} samples contain invalid targets")
        frame[TARGET_COLUMN] = target.astype("Int8")

    if (
        train["target_window_start"].nunique()
        < config.minimum_train_clusters
    ):
        raise ValueError(
            "Target-window purging left fewer training clusters than required"
        )
    if overlap_safe_partitions:
        if (
            calibration["target_window_start"].nunique()
            < config.calibration_clusters
        ):
            raise ValueError(
                "Target-window purging left fewer calibration clusters than required"
            )
        if (
            assessment["target_window_start"].nunique()
            < config.assessment_clusters
        ):
            raise ValueError(
                "Target-window purging left fewer assessment clusters than required"
            )
    else:
        if calibration.empty:
            raise ValueError("Target-window purging left no calibration rows")
        if assessment.empty:
            raise ValueError("No assessment rows remain")
    if set(train["id"]) & set(calibration["id"]):
        raise ValueError("Training and calibration rows overlap")
    if set(train["id"]) & set(assessment["id"]):
        raise ValueError("Training and assessment rows overlap")
    if set(calibration["id"]) & set(assessment["id"]):
        raise ValueError("Calibration and assessment rows overlap")

    def clean(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.sort_values(
            ["target_window_start", "symbol", "id"],
            kind="mergesort",
        ).reset_index(drop=True)

    return ModelPartitions(
        train=clean(train),
        calibration=clean(calibration),
        assessment=clean(assessment),
        minimum_train_clusters=config.minimum_train_clusters,
        calibration_clusters=config.calibration_clusters,
        assessment_clusters=config.assessment_clusters,
        lockbox_clusters=config.lockbox_clusters,
        lockbox_row_count=int(lockbox_mask.sum()),
        lockbox_cluster_count=len(lockbox_cluster_values),
        lockbox_start=pd.Timestamp(lockbox_cluster_values[0]),
        lockbox_end=pd.Timestamp(lockbox_cluster_values[-1]),
        lockbox_cluster_values=tuple(
            pd.Timestamp(value) for value in lockbox_cluster_values
        ),
    )


def fit_or_reuse_model(
    datastore_root: Path,
    *,
    horizon: str,
    feature_set_name: str,
    family: ModelFamily,
    calibration_method: str,
    class_weight: str | None,
    partitions: ModelPartitions,
    input_files: Sequence[Path],
    specification: HorizonSpecification | None = None,
    assumed_round_trip_cost: float = 0.001,
    trained_at: object,
) -> RuntimeModel:
    created = utc_timestamp(trained_at)
    feature_set = resolve_model_feature_set(feature_set_name, horizon=horizon)
    effective_specification = specification or replace(
        horizon_specification(horizon),
        feature_set=feature_set_name,
    )
    if effective_specification.horizon != horizon:
        raise ValueError("Model horizon and horizon specification disagree")
    if effective_specification.feature_set != feature_set_name:
        raise ValueError("Model feature set and horizon specification disagree")
    if not np.isfinite(assumed_round_trip_cost) or not (
        0.0 <= assumed_round_trip_cost < 1.0
    ):
        raise ValueError(
            "assumed_round_trip_cost must be finite and satisfy 0 <= cost < 1"
        )
    model_name = f"{family}-{horizon}"
    model_spec = ModelSpec(
        model_name=model_name,
        family=family,
        feature_set=feature_set.name,
        calibration_method=calibration_method,
        include_symbol=False,
        class_weight=class_weight,
        parameters=(
            _AGGREGATE_WEEKLY_LOGISTIC_PARAMETERS
            if family == "logistic" and horizon == "1w"
            else {}
        ),
        calibration_parameters=(
            _AGGREGATE_WEEKLY_PLATT_PARAMETERS
            if (
                family == "logistic"
                and calibration_method == "platt"
                and horizon == "1w"
            )
            else {}
        ),
    )
    root = Path(datastore_root)
    model_root = root / "ml" / "models" / horizon / model_name
    expected = _model_configuration(
        model_spec=model_spec,
        horizon=horizon,
        feature_set=feature_set,
        partitions=partitions,
        input_files=input_files,
        datastore_root=root,
        specification=effective_specification,
        assumed_round_trip_cost=assumed_round_trip_cost,
    )
    existing = _load_latest_compatible_model(model_root, expected=expected)
    if existing is not None:
        (
            estimator,
            calibrator,
            effective_calibration,
            offline_evaluation,
            directory,
        ) = existing
        return RuntimeModel(
            model_name=model_name,
            horizon=horizon,
            feature_set=feature_set,
            estimator=estimator,
            calibrator=calibrator,
            calibration_method=effective_calibration,
            artifact_directory=directory,
            offline_evaluation=offline_evaluation,
            reused=True,
        )

    train_target = partitions.train[TARGET_COLUMN].astype(int)
    if train_target.nunique() != 2:
        raise ValueError("Model training requires both target classes")
    estimator = build_estimator(model_spec, feature_set)
    estimator.fit(
        model_matrix_for_feature_set(partitions.train, feature_set),
        train_target,
    )
    calibration_raw = estimator.predict_proba(
        model_matrix_for_feature_set(partitions.calibration, feature_set)
    )[:, 1]
    calibration_target = partitions.calibration[TARGET_COLUMN].astype(int)
    calibrator: ProbabilityCalibrator
    effective_calibration = calibration_method
    if calibration_method == "none" or calibration_target.nunique() != 2:
        calibrator = IdentityCalibrator()
        effective_calibration = "none"
    else:
        calibrator = fit_probability_calibrator(
            calibration_method,
            calibration_raw,
            calibration_target,
            **dict(model_spec.calibration_parameters),
        )
    offline_evaluation = _offline_evaluation(
        partitions,
        estimator=estimator,
        calibrator=calibrator,
        feature_set=feature_set,
    )

    directory = create_timestamp_directory(model_root, timestamp=created)
    model_path = directory / "model.joblib"
    temporary = model_path.with_suffix(".joblib.tmp")
    joblib.dump(
        {
            "estimator": estimator,
            "calibrator": calibrator,
            "calibration_method": effective_calibration,
        },
        temporary,
    )
    temporary.replace(model_path)
    manifest = {
        **expected,
        "trained_at": created.isoformat(),
        "effective_calibration_method": effective_calibration,
        "offline_evaluation": offline_evaluation,
        "model_file": {
            "path": model_path.name,
            "size": model_path.stat().st_size,
            "checksum_sha256": file_checksum(model_path),
        },
    }
    _write_json(directory / "manifest.json", manifest)
    _write_json(
        model_root / "latest.json",
        {"path": directory.name, "trained_at": created.isoformat()},
    )
    return RuntimeModel(
        model_name=model_name,
        horizon=horizon,
        feature_set=feature_set,
        estimator=estimator,
        calibrator=calibrator,
        calibration_method=effective_calibration,
        artifact_directory=directory,
        offline_evaluation=offline_evaluation,
        reused=False,
    )


def _model_configuration(
    *,
    model_spec: ModelSpec,
    horizon: str,
    feature_set: ModelFeatureSet,
    partitions: ModelPartitions,
    input_files: Sequence[Path],
    datastore_root: Path,
    specification: HorizonSpecification,
    assumed_round_trip_cost: float,
) -> dict[str, object]:
    configuration = {
        "model_name": model_spec.model_name,
        "model_family": model_spec.family,
        "model_random_state": model_spec.random_state,
        "model_parameters": {
            **DEFAULT_MODEL_PARAMETERS[model_spec.family],
            **dict(model_spec.parameters),
        },
        "include_symbol": model_spec.include_symbol,
        "horizon": horizon,
        "feature_set_name": feature_set.name,
        "feature_set_version": feature_set.version,
        "feature_columns": list(feature_set.names),
        "semantic_feature_contract": feature_set.semantic_contract(),
        "semantic_feature_contract_fingerprint": (
            feature_set.semantic_fingerprint
        ),
        "preprocessing_policy": preprocessing_policy(model_spec.family),
        "target_column": TARGET_COLUMN,
        "runtime_compatibility": _runtime_compatibility(),
        "requested_calibration_method": model_spec.calibration_method,
        "class_weight": model_spec.class_weight,
        "training_rows": len(partitions.train),
        "calibration_rows": len(partitions.calibration),
        "assessment_rows": len(partitions.assessment),
        "partition_configuration": {
            "minimum_train_clusters": partitions.minimum_train_clusters,
            "calibration_clusters": partitions.calibration_clusters,
            "assessment_clusters": partitions.assessment_clusters,
            "lockbox_clusters": partitions.lockbox_clusters,
        },
        "lockbox": {
            "status": "CLOSED_UNTOUCHED_UNSCORED",
            "rows": partitions.lockbox_row_count,
            "target_clusters": partitions.lockbox_cluster_count,
            "start": partitions.lockbox_start.isoformat(),
            "end": partitions.lockbox_end.isoformat(),
        },
        "training_through": pd.to_datetime(
            partitions.train["decision_timestamp"], utc=True
        ).max().isoformat(),
        "input_files": input_inventory(
            input_files,
            relative_to=datastore_root,
        ),
    }
    if model_spec.calibration_parameters:
        configuration["calibration_parameters"] = dict(
            model_spec.calibration_parameters
        )
    if is_weekly_horizon(horizon):
        configuration["target_definition"] = {
            "version": specification.target_definition_version,
            "horizon_specification": specification.as_dict(),
            "assumed_round_trip_cost": float(assumed_round_trip_cost),
        }
    elif horizon in {"1h", "4h"}:
        configuration["target_definition"] = {
            "version": specification.target_definition_version,
            "horizon_specification": specification.as_dict(),
            "target_price_source": {
                "provider": specification.target_price_provider,
                "timeframe": specification.target_price_timeframe,
                "version": specification.target_price_source_version,
                "constituent_rule": specification.target_constituent_rule,
            },
            "calendar_policy": {
                "version": specification.target_calendar_policy_version,
                "definition": specification.exchange_calendar_rule,
            },
            "processing_delay": str(specification.processing_delay),
            "cost_convention": {
                "definition": specification.cost_convention,
                "assumed_round_trip_cost": float(assumed_round_trip_cost),
                "application": (
                    "subtract_exactly_once_from_first_target_minute_open_to_"
                    "final_target_minute_close_simple_return"
                ),
                "positive_class": "cost_adjusted_return_strictly_greater_than_zero",
            },
        }
    return configuration


def _load_latest_compatible_model(
    model_root: Path,
    *,
    expected: dict[str, object],
) -> tuple[
    object,
    ProbabilityCalibrator,
    str,
    Mapping[str, object],
    Path,
] | None:
    pointer_path = model_root / "latest.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        directory = model_root / str(pointer["path"])
        manifest = json.loads(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    for key, value in expected.items():
        if manifest.get(key) != value:
            return None
    raw_file = manifest.get("model_file")
    metadata = raw_file if isinstance(raw_file, dict) else {}
    model_path = directory / str(metadata.get("path", "model.joblib"))
    if not model_path.is_file():
        return None
    if int(metadata.get("size", -1)) != model_path.stat().st_size:
        return None
    if metadata.get("checksum_sha256") != file_checksum(model_path):
        return None
    bundle = joblib.load(model_path)
    if not isinstance(bundle, dict):
        return None
    raw_evaluation = manifest.get("offline_evaluation")
    offline_evaluation = (
        dict(raw_evaluation)
        if isinstance(raw_evaluation, Mapping)
        else {}
    )
    return (
        bundle["estimator"],
        bundle["calibrator"],
        str(bundle.get("calibration_method", "none")),
        offline_evaluation,
        directory,
    )


def _offline_evaluation(
    partitions: ModelPartitions,
    *,
    estimator: object,
    calibrator: ProbabilityCalibrator,
    feature_set: ModelFeatureSet,
) -> dict[str, object]:
    target = partitions.assessment[TARGET_COLUMN].astype(int).to_numpy()
    raw = np.asarray(
        estimator.predict_proba(
            model_matrix_for_feature_set(partitions.assessment, feature_set)
        )[:, 1],
        dtype=float,
    )
    calibrated = np.asarray(calibrator.predict(raw), dtype=float)
    calibration_raw = np.asarray(
        estimator.predict_proba(
            model_matrix_for_feature_set(partitions.calibration, feature_set)
        )[:, 1],
        dtype=float,
    )
    calibration_min = float(calibration_raw.min())
    calibration_max = float(calibration_raw.max())
    train_rate = float(partitions.train[TARGET_COLUMN].astype(int).mean())
    calibration_rate = float(
        partitions.calibration[TARGET_COLUMN].astype(int).mean()
    )
    model_metrics = _probability_metrics(target, calibrated)
    raw_metrics = _probability_metrics(target, raw)
    train_baseline = _probability_metrics(
        target,
        np.full(len(target), train_rate, dtype=float),
    )
    calibration_baseline = _probability_metrics(
        target,
        np.full(len(target), calibration_rate, dtype=float),
    )
    previous_direction = pd.to_numeric(
        partitions.assessment.get(
            "previous_period_direction",
            pd.Series(np.nan, index=partitions.assessment.index),
        ),
        errors="coerce",
    )
    previous_direction_accuracy = None
    if previous_direction.notna().any():
        usable = previous_direction.notna().to_numpy()
        previous_direction_accuracy = float(
            np.mean(
                previous_direction.loc[previous_direction.notna()]
                .astype(int)
                .to_numpy()
                == target[usable]
            )
        )
    return {
        "status": "OFFLINE_EVALUATED_CANDIDATE",
        "assessment_rows": int(len(target)),
        "assessment_start": pd.to_datetime(
            partitions.assessment["decision_timestamp"],
            utc=True,
        ).min().isoformat(),
        "assessment_end": pd.to_datetime(
            partitions.assessment["decision_timestamp"],
            utc=True,
        ).max().isoformat(),
        "assessment_used_for_training": False,
        "assessment_used_for_calibration": False,
        "lockbox": {
            "status": "CLOSED_UNTOUCHED_UNSCORED",
            "rows": partitions.lockbox_row_count,
            "target_clusters": partitions.lockbox_cluster_count,
            "start": partitions.lockbox_start.isoformat(),
            "end": partitions.lockbox_end.isoformat(),
        },
        "raw_model": raw_metrics,
        "calibrated_model": model_metrics,
        "calibration_support": {
            "raw_probability_min": calibration_min,
            "raw_probability_max": calibration_max,
            "assessment_below_support": int((raw < calibration_min).sum()),
            "assessment_above_support": int((raw > calibration_max).sum()),
            "assessment_outside_support": int(
                ((raw < calibration_min) | (raw > calibration_max)).sum()
            ),
            "clip_to_observed_probability_range": bool(
                getattr(calibrator, "raw_probability_min", None) is not None
                and getattr(calibrator, "raw_probability_max", None) is not None
            ),
        },
        "training_base_rate": {
            "probability": train_rate,
            **train_baseline,
        },
        "calibration_base_rate": {
            "probability": calibration_rate,
            **calibration_baseline,
        },
        "previous_period_direction_accuracy": previous_direction_accuracy,
        "beats_training_base_rate_log_loss": bool(
            model_metrics["log_loss"] < train_baseline["log_loss"]
        ),
        "beats_calibration_base_rate_log_loss": bool(
            model_metrics["log_loss"] < calibration_baseline["log_loss"]
        ),
    }


def _probability_metrics(
    target: np.ndarray,
    probability: np.ndarray,
) -> dict[str, float | None]:
    clipped = np.clip(np.asarray(probability, dtype=float), 1e-12, 1.0 - 1e-12)
    observed = np.asarray(target, dtype=int)
    log_loss = -np.mean(
        observed * np.log(clipped)
        + (1 - observed) * np.log(1.0 - clipped)
    )
    return {
        "log_loss": float(log_loss),
        "brier_score": float(np.mean((clipped - observed) ** 2)),
        "accuracy_at_0_5": float(
            np.mean((clipped >= 0.5).astype(int) == observed)
        ),
        "roc_auc": _roc_auc(observed, clipped),
    }


def _roc_auc(
    target: np.ndarray,
    probability: np.ndarray,
) -> float | None:
    labels = pd.Series(np.asarray(target, dtype=int))
    positive_count = int(labels.eq(1).sum())
    negative_count = int(labels.eq(0).sum())
    if positive_count == 0 or negative_count == 0:
        return None
    ranks = pd.Series(probability).rank(method="average")
    positive_rank_sum = float(ranks.loc[labels.eq(1)].sum())
    return float(
        (
            positive_rank_sum
            - positive_count * (positive_count + 1) / 2.0
        )
        / (positive_count * negative_count)
    )


def _runtime_compatibility() -> dict[str, object]:
    python_version = platform.python_version()
    packages: dict[str, str | None] = {}
    for distribution in _RUNTIME_PACKAGES:
        try:
            packages[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            packages[distribution] = None
    return {
        "python_implementation": platform.python_implementation(),
        "python_major_minor": ".".join(python_version.split(".")[:2]),
        "packages": packages,
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
