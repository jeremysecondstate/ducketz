from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import (
    HistGradientBoostingClassifier,
    HistGradientBoostingRegressor,
)
from sklearn.impute import MissingIndicator, SimpleImputer
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, RobustScaler

from ml.artifacts import (
    create_timestamp_directory,
    file_checksum,
    input_inventory,
    utc_timestamp,
)
from ml.calibration import IdentityCalibrator, fit_probability_calibrator
from ml.preprocessing import (
    PREPROCESSING_POLICY_VERSION,
    TRAINING_CLIP_LOWER_QUANTILE,
    TRAINING_CLIP_UPPER_QUANTILE,
    QuantileClipper,
)
from ml.strategy_selection.contracts import (
    MARKET_STATE_POLICY_VERSION,
    STRATEGY_CANDIDATE_POLICY_VERSION,
    STRATEGY_MODEL_POLICY_VERSION,
    STRATEGY_OUTCOME_POLICY_VERSION,
    STRATEGY_PRIOR_POLICY_VERSION,
    STRATEGY_RANKING_POLICY_VERSION,
    STRATEGY_REGISTRY_VERSION,
    StrategyModel,
    StrategyPartitions,
    StrategySelectionPolicy,
)
from ml.strategy_selection.research_trace import strategy_research_trace


CANDIDATE_NUMERIC_FEATURES = (
    "underlying_price",
    "front_days_to_expiration",
    "back_days_to_expiration",
    "target_elapsed_hours",
    "width_steps",
    "leg_count",
    "entry_net_credit",
    "entry_net_debit",
    "max_profit",
    "max_loss",
    "capital_required",
    "net_delta",
    "net_gamma",
    "net_theta",
    "net_vega",
    "mean_relative_spread",
    "max_relative_spread",
    "minimum_open_interest",
    "total_volume",
    "entry_debit_to_underlying",
    "max_loss_to_underlying",
    "net_delta_per_share",
    "surface_quality_pass",
    "all_option_quotes_valid",
    "liquidity_policy_pass",
    "stock_quote_quality_pass",
    "maximum_quote_staleness_seconds",
    "market_expected_absolute_move",
    "market_expected_realized_volatility",
    "market_uncertainty",
    "market_trend_persistence",
    "market_mean_reversion_tendency",
    "strategy_prior__profit_probability",
    "strategy_prior__expected_return_on_risk",
)
CANDIDATE_CATEGORICAL_FEATURES = (
    "strategy_name",
    "strategy_family",
    "risk_form",
    "expiration_structure",
    "stock_requirement",
    "cash_requirement",
)
_CONTEXT_PREFIXES = (
    "technical__",
    "bar__",
    "weekly__",
    "life__",
    "fdir__",
    "fund__",
    "ftlife__",
    "quote__",
    "opt__",
    "energy__",
    "macro__",
    "sec__",
    "cme__",
    "mr__",
    "bp__",
)


def partition_strategy_outcomes(
    outcomes: pd.DataFrame,
    *,
    policy: StrategySelectionPolicy,
) -> StrategyPartitions:
    required = {
        "candidate_key",
        "decision_timestamp",
        "target_window_start",
        "target_window_end",
        "outcome_status",
        "profitable",
    }
    missing = sorted(required.difference(outcomes.columns))
    if missing:
        raise ValueError("Strategy outcomes are missing columns: " + ", ".join(missing))
    eligible = outcomes.loc[outcomes["outcome_status"].eq("COMPLETE")].copy()
    for column in ("decision_timestamp", "target_window_start", "target_window_end"):
        eligible[column] = pd.to_datetime(eligible[column], utc=True, errors="coerce")
    if eligible.empty:
        raise ValueError("No complete causal strategy outcomes are available")
    if eligible[["decision_timestamp", "target_window_start", "target_window_end"]].isna().any().any():
        raise ValueError("Complete strategy outcomes contain invalid timestamps")
    eligible["profitable"] = pd.to_numeric(
        eligible["profitable"], errors="coerce"
    ).astype("Int8")
    if not eligible["profitable"].isin([0, 1]).all():
        raise ValueError("Strategy profitable target must contain only 0/1")
    row_key = eligible["decision_timestamp"].astype("string") + "|" + eligible[
        "candidate_key"
    ].astype("string")
    if row_key.duplicated().any():
        raise ValueError("Strategy outcomes contain duplicate decision candidates")

    clusters = pd.Index(
        eligible["target_window_start"].drop_duplicates().sort_values()
    )
    required_clusters = (
        policy.minimum_train_decisions
        + policy.calibration_decisions
        + policy.assessment_decisions
    )
    if len(clusters) < required_clusters:
        raise ValueError(
            "Insufficient pre-lockbox strategy decision clusters: "
            f"required {required_clusters}, observed {len(clusters)}"
        )

    assessment_clusters = clusters[-policy.assessment_decisions :]
    assessment_boundary = pd.Timestamp(assessment_clusters[0])
    calibration_candidates = eligible.loc[
        eligible["target_window_start"].lt(assessment_boundary)
        & eligible["target_window_end"].lt(assessment_boundary)
    ]
    calibration_clusters = pd.Index(
        calibration_candidates["target_window_start"].drop_duplicates().sort_values()
    )
    if len(calibration_clusters) < policy.calibration_decisions:
        raise ValueError(
            "Boundary purging left too few strategy calibration clusters: "
            f"required {policy.calibration_decisions}, observed {len(calibration_clusters)}"
        )
    calibration_clusters = calibration_clusters[-policy.calibration_decisions :]
    calibration_boundary = pd.Timestamp(calibration_clusters[0])

    train = eligible.loc[
        eligible["target_window_start"].lt(calibration_boundary)
        & eligible["target_window_end"].lt(calibration_boundary)
    ].copy()
    calibration = eligible.loc[
        eligible["target_window_start"].isin(calibration_clusters)
        & eligible["target_window_end"].lt(assessment_boundary)
    ].copy()
    assessment = eligible.loc[
        eligible["target_window_start"].isin(assessment_clusters)
    ].copy()
    train_decisions = int(train["target_window_start"].nunique())
    calibration_decisions = int(calibration["target_window_start"].nunique())
    assessment_decisions = int(assessment["target_window_start"].nunique())
    if train_decisions < policy.minimum_train_decisions:
        raise ValueError(
            "Boundary purging left too few strategy training clusters: "
            f"required {policy.minimum_train_decisions}, observed {train_decisions}"
        )
    if calibration_decisions < policy.calibration_decisions:
        raise ValueError("Boundary purging left too few strategy calibration clusters")
    if assessment_decisions < policy.assessment_decisions:
        raise ValueError("Boundary purging left too few strategy assessment clusters")

    membership = pd.concat([train, calibration, assessment]).index
    purged_rows = int(len(eligible) - len(set(membership)))
    for left, right, label in (
        (train, calibration, "training/calibration"),
        (train, assessment, "training/assessment"),
        (calibration, assessment, "calibration/assessment"),
    ):
        if set(left.index).intersection(right.index):
            raise ValueError(f"Strategy {label} rows overlap")

    def clean(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.sort_values(
            ["target_window_start", "strategy_name", "candidate_key"],
            kind="mergesort",
        ).reset_index(drop=True)

    return StrategyPartitions(
        train=clean(train),
        calibration=clean(calibration),
        assessment=clean(assessment),
        train_decisions=train_decisions,
        calibration_decisions=calibration_decisions,
        assessment_decisions=assessment_decisions,
        purged_rows=purged_rows,
    )


def fit_or_reuse_strategy_model(
    datastore_root: Path,
    *,
    horizon: str,
    partitions: StrategyPartitions,
    policy: StrategySelectionPolicy,
    input_files: Sequence[Path],
    trained_at: object,
) -> StrategyModel:
    created = utc_timestamp(trained_at)
    numeric = _numeric_features(partitions.train)
    categorical = tuple(
        column
        for column in CANDIDATE_CATEGORICAL_FEATURES
        if column in partitions.train.columns
    )
    expected = _model_configuration(
        horizon=horizon,
        partitions=partitions,
        policy=policy,
        numeric_features=numeric,
        categorical_features=categorical,
        input_files=input_files,
        datastore_root=Path(datastore_root),
    )
    model_root = (
        Path(datastore_root)
        / "ml"
        / "strategy-models"
        / horizon
        / "market-state-strategy-outcome"
    )
    existing = _load_compatible_model(model_root, expected=expected)
    if existing is not None:
        return StrategyModel(
            horizon=horizon,
            estimator=existing["estimator"],
            return_estimator=existing["return_estimator"],
            calibrator=existing["calibrator"],
            numeric_features=numeric,
            categorical_features=categorical,
            artifact_directory=existing["artifact_directory"],
            offline_evaluation=existing["offline_evaluation"],
            reused=True,
        )

    train_target = partitions.train["profitable"].astype(int)
    if train_target.nunique() != 2:
        raise ValueError("Strategy model training requires both outcome classes")
    estimator = _probability_estimator(numeric, categorical, policy=policy)
    weights = _decision_weights(partitions.train)
    estimator.fit(
        _matrix(partitions.train, numeric, categorical),
        train_target,
        model__sample_weight=weights,
    )
    return_estimator = _return_estimator(numeric, categorical, policy=policy)
    observed_return = pd.to_numeric(
        partitions.train["return_on_risk"], errors="coerce"
    ).to_numpy(dtype=float)
    return_residual = observed_return - _prior_return(partitions.train)
    if not np.isfinite(return_residual).all():
        raise ValueError("Strategy expected-return target must be finite")
    return_estimator.fit(
        _matrix(partitions.train, numeric, categorical),
        return_residual,
        model__sample_weight=weights,
    )
    calibration_raw = estimator.predict_proba(
        _matrix(partitions.calibration, numeric, categorical)
    )[:, 1]
    calibration_target = partitions.calibration["profitable"].astype(int)
    calibrator: object
    if calibration_target.nunique() != 2:
        calibrator = IdentityCalibrator()
        effective_calibration = "none"
    else:
        calibrator = fit_probability_calibrator(
            "platt",
            calibration_raw,
            calibration_target,
            clip_to_observed_probability_range=True,
            sample_weight=_decision_weights(partitions.calibration),
        )
        effective_calibration = "platt"
    offline_evaluation = _offline_evaluation(
        partitions,
        estimator=estimator,
        return_estimator=return_estimator,
        calibrator=calibrator,
        numeric=numeric,
        categorical=categorical,
        calibration_raw=calibration_raw,
        effective_calibration=effective_calibration,
    )

    directory = create_timestamp_directory(model_root, timestamp=created)
    model_path = directory / "model.joblib"
    temporary = model_path.with_suffix(".joblib.tmp")
    joblib.dump(
        {
            "estimator": estimator,
            "return_estimator": return_estimator,
            "calibrator": calibrator,
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
    return StrategyModel(
        horizon=horizon,
        estimator=estimator,
        return_estimator=return_estimator,
        calibrator=calibrator,
        numeric_features=numeric,
        categorical_features=categorical,
        artifact_directory=directory,
        offline_evaluation=offline_evaluation,
        reused=False,
    )


def score_strategy_candidates(
    model: StrategyModel,
    candidates: pd.DataFrame,
) -> pd.DataFrame:
    if candidates.empty:
        return candidates.copy()
    matrix = _matrix(
        candidates,
        model.numeric_features,
        model.categorical_features,
    )
    raw = np.asarray(model.estimator.predict_proba(matrix)[:, 1], dtype=float)
    calibrated = np.asarray(model.calibrator.predict(raw), dtype=float)
    output = candidates.copy()
    output["raw_profit_probability"] = raw
    output["calibrated_profit_probability"] = calibrated
    residual = np.asarray(model.return_estimator.predict(matrix), dtype=float)
    expected_return = _prior_return(output) + residual
    expected_return, expected_profit = _bounded_expected_return(
        output,
        expected_return,
    )
    output["expected_net_profit"] = expected_profit
    output["expected_return_on_risk"] = expected_return
    output["decision_score"] = expected_return
    output["model_version"] = model.artifact_directory.name
    output["model_policy_version"] = STRATEGY_MODEL_POLICY_VERSION
    output["ranking_policy_version"] = STRATEGY_RANKING_POLICY_VERSION
    output = output.sort_values(
        ["decision_score", "calibrated_profit_probability", "candidate_key"],
        ascending=[False, False, True],
        kind="mergesort",
    ).reset_index(drop=True)
    output["candidate_rank"] = np.arange(1, len(output) + 1, dtype=int)
    return output


def _numeric_features(frame: pd.DataFrame) -> tuple[str, ...]:
    context = tuple(
        column
        for column in frame.columns
        if column.startswith(_CONTEXT_PREFIXES)
        and pd.api.types.is_numeric_dtype(frame[column])
    )
    base = tuple(column for column in CANDIDATE_NUMERIC_FEATURES if column in frame)
    previous = ("previous_period_direction",) if "previous_period_direction" in frame else ()
    return tuple(dict.fromkeys((*base, *previous, *context)))


def _matrix(
    frame: pd.DataFrame,
    numeric: Sequence[str],
    categorical: Sequence[str],
) -> pd.DataFrame:
    required = tuple((*numeric, *categorical))
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError("Strategy model frame is missing columns: " + ", ".join(missing))
    return frame.loc[:, list(required)].copy()


def _probability_estimator(
    numeric: tuple[str, ...],
    categorical: tuple[str, ...],
    *,
    policy: StrategySelectionPolicy,
) -> Pipeline:
    return _model_pipeline(
        numeric,
        categorical,
        model=HistGradientBoostingClassifier(
            learning_rate=0.05,
            max_iter=200,
            max_leaf_nodes=31,
            l2_regularization=1.0,
            early_stopping=False,
            random_state=policy.random_state,
        ),
    )


def _return_estimator(
    numeric: tuple[str, ...],
    categorical: tuple[str, ...],
    *,
    policy: StrategySelectionPolicy,
) -> Pipeline:
    return _model_pipeline(
        numeric,
        categorical,
        model=HistGradientBoostingRegressor(
            learning_rate=0.05,
            max_iter=200,
            max_leaf_nodes=31,
            l2_regularization=1.0,
            early_stopping=False,
            random_state=policy.random_state,
        ),
    )


def _model_pipeline(
    numeric: tuple[str, ...],
    categorical: tuple[str, ...],
    *,
    model: object,
) -> Pipeline:
    finite = FunctionTransformer(
        _replace_non_finite,
        validate=False,
        feature_names_out="one-to-one",
    )
    numeric_pipeline = Pipeline(
        steps=[
            ("finite", finite),
            ("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
            (
                "clip",
                QuantileClipper(
                    lower_quantile=TRAINING_CLIP_LOWER_QUANTILE,
                    upper_quantile=TRAINING_CLIP_UPPER_QUANTILE,
                ),
            ),
            ("scale", RobustScaler()),
        ]
    )
    missing_pipeline = Pipeline(
        steps=[
            (
                "finite",
                FunctionTransformer(
                    _replace_non_finite,
                    validate=False,
                    feature_names_out="one-to-one",
                ),
            ),
            ("indicator", MissingIndicator(features="all")),
        ]
    )
    transformers: list[tuple[str, object, Sequence[str]]] = [
        ("numeric", numeric_pipeline, numeric),
        ("missing", missing_pipeline, numeric),
    ]
    if categorical:
        transformers.append(
            (
                "categorical",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                categorical,
            )
        )
    preprocessor = ColumnTransformer(
        transformers=transformers,
        remainder="drop",
        sparse_threshold=0.0,
    )
    return Pipeline(
        steps=[
            ("preprocess", preprocessor),
            ("model", model),
        ]
    )


def _replace_non_finite(values: object) -> np.ndarray:
    array = np.asarray(values, dtype=float).copy()
    array[~np.isfinite(array)] = np.nan
    return array


def _decision_weights(frame: pd.DataFrame) -> np.ndarray:
    counts = frame.groupby("target_window_start")["candidate_key"].transform("count")
    return (1.0 / pd.to_numeric(counts, errors="coerce")).to_numpy(dtype=float)


def _prior_return(frame: pd.DataFrame) -> np.ndarray:
    if "strategy_prior__expected_return_on_risk" not in frame:
        return np.zeros(len(frame), dtype=float)
    values = pd.to_numeric(
        frame["strategy_prior__expected_return_on_risk"], errors="coerce"
    ).to_numpy(dtype=float, copy=True)
    values[~np.isfinite(values)] = 0.0
    return values


def _bounded_expected_return(
    frame: pd.DataFrame,
    expected_return: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    capital = pd.to_numeric(frame["capital_required"], errors="coerce").to_numpy(
        dtype=float
    )
    if not np.isfinite(capital).all() or np.any(capital <= 0.0):
        raise ValueError("Strategy scoring requires finite positive capital")
    profit = np.asarray(expected_return, dtype=float) * capital
    maximum_loss = pd.to_numeric(frame["max_loss"], errors="coerce").to_numpy(
        dtype=float
    )
    if not np.isfinite(maximum_loss).all():
        raise ValueError("Strategy scoring requires finite maximum loss")
    profit = np.maximum(profit, -maximum_loss)
    maximum_profit = pd.to_numeric(
        frame["max_profit"], errors="coerce"
    ).to_numpy(dtype=float)
    finite_profit = np.isfinite(maximum_profit)
    profit[finite_profit] = np.minimum(
        profit[finite_profit], maximum_profit[finite_profit]
    )
    return profit / capital, profit


def _offline_evaluation(
    partitions: StrategyPartitions,
    *,
    estimator: object,
    return_estimator: object,
    calibrator: object,
    numeric: tuple[str, ...],
    categorical: tuple[str, ...],
    calibration_raw: np.ndarray,
    effective_calibration: str,
) -> dict[str, object]:
    assessment_target = partitions.assessment["profitable"].astype(int).to_numpy()
    assessment_matrix = _matrix(partitions.assessment, numeric, categorical)
    raw = np.asarray(
        estimator.predict_proba(assessment_matrix)[:, 1],
        dtype=float,
    )
    calibrated = np.asarray(calibrator.predict(raw), dtype=float)
    expected_return, _expected_profit = _bounded_expected_return(
        partitions.assessment,
        _prior_return(partitions.assessment)
        + np.asarray(return_estimator.predict(assessment_matrix), dtype=float),
    )
    support_min = float(np.min(calibration_raw))
    support_max = float(np.max(calibration_raw))
    top_ranked = partitions.assessment.copy()
    top_ranked["probability"] = calibrated
    top_ranked["expected_return"] = expected_return
    top_ranked = (
        top_ranked.sort_values(
            ["target_window_start", "expected_return", "probability"],
            ascending=[True, False, False],
            kind="mergesort",
        )
        .groupby("target_window_start", sort=False)
        .head(1)
    )
    return {
        "status": "OFFLINE_ASSESSMENT_COMPLETE",
        "assessment_used_for_training": False,
        "assessment_used_for_calibration": False,
        "real_lockbox_used": False,
        "fit_partition": "training",
        "calibration_partition": "calibration",
        "ranking_rule": (
            "highest_expected_return_on_risk_then_calibrated_probability_per_decision"
        ),
        "training_rows": len(partitions.train),
        "training_decisions": partitions.train_decisions,
        "calibration_rows": len(partitions.calibration),
        "calibration_decisions": partitions.calibration_decisions,
        "assessment_rows": len(partitions.assessment),
        "assessment_decisions": partitions.assessment_decisions,
        "boundary_purged_rows": partitions.purged_rows,
        "effective_calibration_method": effective_calibration,
        "calibration_support": {
            "raw_probability_min": support_min,
            "raw_probability_max": support_max,
            "assessment_below": int((raw < support_min).sum()),
            "assessment_above": int((raw > support_max).sum()),
        },
        "metric_weighting": "equal_weight_per_target_window_start",
        "raw_model": _probability_metrics(
            assessment_target,
            raw,
            sample_weight=_decision_weights(partitions.assessment),
        ),
        "calibrated_model": _probability_metrics(
            assessment_target,
            calibrated,
            sample_weight=_decision_weights(partitions.assessment),
        ),
        "candidate_level_raw_model": _probability_metrics(
            assessment_target,
            raw,
        ),
        "candidate_level_calibrated_model": _probability_metrics(
            assessment_target,
            calibrated,
        ),
        "expected_return_model": _return_metrics(
            pd.to_numeric(
                partitions.assessment["return_on_risk"], errors="coerce"
            ).to_numpy(dtype=float),
            expected_return,
            sample_weight=_decision_weights(partitions.assessment),
        ),
        "candidate_level_expected_return_model": _return_metrics(
            pd.to_numeric(
                partitions.assessment["return_on_risk"], errors="coerce"
            ).to_numpy(dtype=float),
            expected_return,
        ),
        "top_ranked_assessment_decisions": len(top_ranked),
        "top_ranked_profitable_rate": (
            float(pd.to_numeric(top_ranked["profitable"], errors="coerce").mean())
            if not top_ranked.empty
            else None
        ),
        "top_ranked_mean_return_on_risk": (
            float(pd.to_numeric(top_ranked["return_on_risk"], errors="coerce").mean())
            if not top_ranked.empty
            else None
        ),
        "top_ranked_total_net_profit": (
            float(pd.to_numeric(top_ranked["net_profit"], errors="coerce").sum())
            if not top_ranked.empty
            else None
        ),
    }


def _probability_metrics(
    target: np.ndarray,
    probability: np.ndarray,
    *,
    sample_weight: np.ndarray | None = None,
) -> dict[str, object]:
    clipped = np.clip(probability, 1e-12, 1.0 - 1e-12)
    weights = (
        np.ones(len(target), dtype=float)
        if sample_weight is None
        else np.asarray(sample_weight, dtype=float)
    )
    log_loss = -np.average(
        target * np.log(clipped) + (1 - target) * np.log(1 - clipped),
        weights=weights,
    )
    brier = np.average((probability - target) ** 2, weights=weights)
    accuracy = np.average(
        (probability >= 0.5).astype(int) == target,
        weights=weights,
    )
    auc = (
        float(roc_auc_score(target, probability, sample_weight=weights))
        if np.unique(target).size == 2
        else None
    )
    return {
        "rows": len(target),
        "target_base_rate": float(np.average(target, weights=weights)),
        "log_loss": float(log_loss),
        "brier_score": float(brier),
        "roc_auc": auc,
        "accuracy_0_5": float(accuracy),
    }


def _return_metrics(
    target: np.ndarray,
    prediction: np.ndarray,
    *,
    sample_weight: np.ndarray | None = None,
) -> dict[str, object]:
    observed = np.asarray(target, dtype=float)
    predicted = np.asarray(prediction, dtype=float)
    if not np.isfinite(observed).all() or not np.isfinite(predicted).all():
        raise ValueError("Expected-return assessment values must be finite")
    weights = (
        np.ones(len(observed), dtype=float)
        if sample_weight is None
        else np.asarray(sample_weight, dtype=float)
    )
    error = predicted - observed
    return {
        "rows": len(observed),
        "mean_observed_return_on_risk": float(np.average(observed, weights=weights)),
        "mean_predicted_return_on_risk": float(
            np.average(predicted, weights=weights)
        ),
        "mean_absolute_error": float(np.average(np.abs(error), weights=weights)),
        "root_mean_squared_error": float(
            math.sqrt(np.average(np.square(error), weights=weights))
        ),
    }


def _model_configuration(
    *,
    horizon: str,
    partitions: StrategyPartitions,
    policy: StrategySelectionPolicy,
    numeric_features: tuple[str, ...],
    categorical_features: tuple[str, ...],
    input_files: Sequence[Path],
    datastore_root: Path,
) -> dict[str, object]:
    return {
        "model_name": "market-state-strategy-outcome",
        "model_family": "hist-gradient-classifier-regressor",
        "horizon": horizon,
        "model_policy_version": STRATEGY_MODEL_POLICY_VERSION,
        "registry_version": STRATEGY_REGISTRY_VERSION,
        "candidate_policy_version": STRATEGY_CANDIDATE_POLICY_VERSION,
        "outcome_policy_version": STRATEGY_OUTCOME_POLICY_VERSION,
        "ranking_policy_version": STRATEGY_RANKING_POLICY_VERSION,
        "market_state_policy_version": MARKET_STATE_POLICY_VERSION,
        "strategy_prior_policy_version": STRATEGY_PRIOR_POLICY_VERSION,
        "research_trace": strategy_research_trace(),
        "preprocessing_policy_version": PREPROCESSING_POLICY_VERSION,
        "numeric_features": list(numeric_features),
        "categorical_features": list(categorical_features),
        "probability_target_column": "profitable",
        "expected_return_target_column": "return_on_risk_residual_to_prior",
        "calibration_method": "platt",
        "policy": asdict(policy),
        "training_rows": len(partitions.train),
        "training_decisions": partitions.train_decisions,
        "calibration_rows": len(partitions.calibration),
        "calibration_decisions": partitions.calibration_decisions,
        "assessment_rows": len(partitions.assessment),
        "assessment_decisions": partitions.assessment_decisions,
        "training_through": pd.to_datetime(
            partitions.train["target_window_end"], utc=True
        ).max().isoformat(),
        "input_files": input_inventory(input_files, relative_to=datastore_root),
    }


def _load_compatible_model(
    model_root: Path,
    *,
    expected: Mapping[str, object],
) -> dict[str, object] | None:
    pointer = model_root / "latest.json"
    if not pointer.is_file():
        return None
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        directory = model_root / str(payload["path"])
        manifest = json.loads(
            (directory / "manifest.json").read_text(encoding="utf-8")
        )
        if any(manifest.get(key) != value for key, value in expected.items()):
            return None
        metadata = manifest["model_file"]
        model_path = directory / str(metadata["path"])
        if not model_path.is_file():
            return None
        if int(metadata["size"]) != model_path.stat().st_size:
            return None
        if str(metadata["checksum_sha256"]) != file_checksum(model_path):
            return None
        bundle = joblib.load(model_path)
        if not isinstance(bundle, dict):
            return None
        return {
            **bundle,
            "artifact_directory": directory,
            "offline_evaluation": manifest.get("offline_evaluation", {}),
        }
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


__all__ = [
    "CANDIDATE_CATEGORICAL_FEATURES",
    "CANDIDATE_NUMERIC_FEATURES",
    "fit_or_reuse_strategy_model",
    "partition_strategy_outcomes",
    "score_strategy_candidates",
]
