from __future__ import annotations

import hashlib
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
from sklearn.neural_network import MLPClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import FunctionTransformer, OneHotEncoder, RobustScaler

from ml.artifacts import (
    create_timestamp_directory,
    file_checksum,
    input_inventory,
    utc_timestamp,
)
from ml.training_progress import fit_with_progress
from ml.calibration import fit_probability_calibrator
from ml.preprocessing import (
    PREPROCESSING_POLICY_VERSION,
    TRAINING_CLIP_LOWER_QUANTILE,
    TRAINING_CLIP_UPPER_QUANTILE,
    QuantileClipper,
)
from ml.strategy_selection.contracts import (
    BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
    BSGP_CALIBRATED_MODEL_SCORE_BASIS,
    MARKET_STATE_POLICY_VERSION,
    OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
    STRATEGY_CANDIDATE_POLICY_VERSION,
    STRATEGY_CANDIDATE_SCHEMA_VERSION,
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
    "direction_probability_up",
    "direction_alignment",
    "strategy_prior__scenario_coverage_score",
    "strategy_prior__expected_return_on_risk",
    "pricing_leg_coverage",
    "pricing_candidate_edge",
    "pricing_conservative_edge",
    "pricing_edge_to_friction",
    "pricing_uncertainty",
    "pricing_probability_favorable",
    "pricing_relative_edge",
    "pricing_model_age_seconds",
    "pricing_residual_shrinkage",
)

_NEURAL_CHALLENGER_POLICY_VERSION = (
    "chronological-hgb-mlp-log-loss-challenger-v1"
)
_NEURAL_CHALLENGER_MINIMUM_DECISIONS = 64
_NEURAL_CHALLENGER_MAXIMUM_VALIDATION_DECISIONS = 63
_NEURAL_CHALLENGER_VALIDATION_FRACTION = 0.20
_NEURAL_CHALLENGER_REQUIRED_RELATIVE_IMPROVEMENT = 0.005
_NEURAL_BLEND_WEIGHTS = (0.25, 0.50, 0.75, 1.0)


class _ProbabilityBlend:
    """A fitted, joblib-safe convex blend of two probability estimators."""

    def __init__(
        self,
        hist_gradient: object,
        neural_network: object,
        *,
        neural_weight: float,
    ) -> None:
        self.hist_gradient = hist_gradient
        self.neural_network = neural_network
        self.neural_weight = float(neural_weight)

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        tree = np.asarray(self.hist_gradient.predict_proba(frame), dtype=float)
        neural = np.asarray(self.neural_network.predict_proba(frame), dtype=float)
        return (1.0 - self.neural_weight) * tree + self.neural_weight * neural


PRICING_NUMERIC_FEATURES = (
    "pricing_leg_coverage",
    "pricing_candidate_edge",
    "pricing_conservative_edge",
    "pricing_edge_to_friction",
    "pricing_uncertainty",
    "pricing_probability_favorable",
    "pricing_relative_edge",
    "pricing_model_age_seconds",
    "pricing_residual_shrinkage",
)
CANDIDATE_CATEGORICAL_FEATURES = (
    "strategy_name",
    "strategy_family",
    "risk_form",
    "expiration_structure",
    "stock_requirement",
    "cash_requirement",
    "pricing_source",
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
        "symbol",
        "horizon",
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
    natural_key = pd.DataFrame(
        {
            "symbol": eligible["symbol"]
            .astype("string")
            .str.strip()
            .str.upper(),
            "horizon": eligible["horizon"]
            .astype("string")
            .str.strip()
            .str.lower(),
            "decision_timestamp": eligible["decision_timestamp"],
            "target_window_start": eligible["target_window_start"],
            "target_window_end": eligible["target_window_end"],
            "candidate_key": eligible["candidate_key"]
            .astype("string")
            .str.strip(),
        },
        index=eligible.index,
    )
    if (
        natural_key.isna().any(axis=None)
        or natural_key["symbol"].eq("").any()
        or natural_key["horizon"].eq("").any()
        or natural_key["candidate_key"].eq("").any()
    ):
        raise ValueError("Strategy outcomes contain incomplete natural keys")
    if natural_key.duplicated().any():
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
    publish_latest: bool = True,
) -> StrategyModel:
    created = utc_timestamp(trained_at)
    missing_pricing = sorted(
        set(PRICING_NUMERIC_FEATURES).difference(partitions.train.columns)
    )
    if missing_pricing or "pricing_source" not in partitions.train:
        raise ValueError(
            "Strategy model training requires pricing evidence features: "
            + ", ".join((*missing_pricing, *(() if "pricing_source" in partitions.train else ("pricing_source",))))
        )
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
            probability_model_family=str(
                existing.get("probability_model_family") or "hist-gradient"
            ),
            reused=True,
        )

    train_target = partitions.train["profitable"].astype(int)
    if train_target.nunique() != 2:
        raise ValueError("Strategy model training requires both outcome classes")
    weights = _decision_weights(partitions.train)
    estimator, probability_model_family, model_selection = (
        _fit_probability_model(
            partitions.train,
            numeric=numeric,
            categorical=categorical,
            policy=policy,
        )
    )
    return_estimator = _return_estimator(numeric, categorical, policy=policy)
    observed_return = pd.to_numeric(
        partitions.train["return_on_risk"], errors="coerce"
    ).to_numpy(dtype=float)
    return_residual = observed_return - _prior_return(partitions.train)
    if not np.isfinite(return_residual).all():
        raise ValueError("Strategy expected-return target must be finite")
    fit_with_progress(
        return_estimator, _matrix(partitions.train, numeric, categorical),
        return_residual, label="strategy/expected-return",
        model__sample_weight=weights,
    )
    calibration_raw = _validated_probability_array(
        estimator.predict_proba(
            _matrix(partitions.calibration, numeric, categorical)
        )[:, 1],
        label="Calibration raw model",
    )
    calibration_target = partitions.calibration["profitable"].astype(int)
    if calibration_target.nunique() != 2:
        raise ValueError(
            "Strategy calibration unavailable: calibration partition requires "
            "both observed outcome classes"
        )
    calibrator = fit_probability_calibrator(
        "platt",
        calibration_raw,
        calibration_target,
        clip_to_observed_probability_range=True,
        sample_weight=_decision_weights(partitions.calibration),
    )
    effective_calibration = calibrator.method
    offline_evaluation = _offline_evaluation(
        partitions,
        estimator=estimator,
        return_estimator=return_estimator,
        calibrator=calibrator,
        numeric=numeric,
        categorical=categorical,
        calibration_raw=calibration_raw,
        effective_calibration=effective_calibration,
        model_selection=model_selection,
        probability_model_family=probability_model_family,
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
        "selected_probability_model_family": probability_model_family,
        "offline_evaluation": offline_evaluation,
        "model_file": {
            "path": model_path.name,
            "size": model_path.stat().st_size,
            "checksum_sha256": file_checksum(model_path),
        },
    }
    _write_json(directory / "manifest.json", manifest)
    if publish_latest:
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
        probability_model_family=probability_model_family,
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
    raw = _validated_probability_array(raw, label="Raw model")
    calibrated = _validated_probability_array(
        model.calibrator.predict(raw),
        label="Calibrated model",
    )
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
    output["decision_score"] = calibrated
    pricing_source = output.get(
        "pricing_source", pd.Series("BLACK_SCHOLES", index=output.index)
    ).astype("string").str.upper()
    output["score_basis"] = np.select(
        (
            pricing_source.eq("BSGP"),
            pricing_source.eq("BLACK_SCHOLES"),
        ),
        (
            BSGP_CALIBRATED_MODEL_SCORE_BASIS,
            BLACK_SCHOLES_CALIBRATED_MODEL_SCORE_BASIS,
        ),
        default=OPRA_EXECUTION_CALIBRATED_MODEL_SCORE_BASIS,
    )
    output["schema_version"] = STRATEGY_CANDIDATE_SCHEMA_VERSION
    output["model_version"] = model.artifact_directory.name
    output["model_policy_version"] = STRATEGY_MODEL_POLICY_VERSION
    output["ranking_policy_version"] = STRATEGY_RANKING_POLICY_VERSION
    output["model_status"] = "MODEL_FIT"
    output = output.sort_values(
        ["decision_score", "expected_return_on_risk", "candidate_key"],
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


def _neural_probability_estimator(
    numeric: tuple[str, ...],
    categorical: tuple[str, ...],
    *,
    policy: StrategySelectionPolicy,
) -> Pipeline:
    return _model_pipeline(
        numeric,
        categorical,
        model=MLPClassifier(
            hidden_layer_sizes=(64, 32),
            activation="relu",
            solver="adam",
            alpha=1e-3,
            batch_size=128,
            learning_rate_init=1e-3,
            max_iter=150,
            early_stopping=True,
            validation_fraction=0.15,
            n_iter_no_change=15,
            tol=1e-4,
            shuffle=False,
            random_state=policy.random_state,
        ),
    )


def _fit_probability_model(
    train: pd.DataFrame,
    *,
    numeric: tuple[str, ...],
    categorical: tuple[str, ...],
    policy: StrategySelectionPolicy,
) -> tuple[object, str, dict[str, object]]:
    """Fit HGB and admit neural influence only on an earlier training holdout.

    Calibration and assessment stay completely outside this choice.  The
    neural network therefore cannot reach production merely because it fits
    the calibration or assessment cohort well.
    """

    split = _neural_challenger_split(train)
    if split is None:
        estimator = _probability_estimator(numeric, categorical, policy=policy)
        _fit_probability_pipeline(
            estimator,
            train,
            numeric=numeric,
            categorical=categorical,
        )
        return estimator, "hist-gradient", {
            "policy_version": _NEURAL_CHALLENGER_POLICY_VERSION,
            "status": "NOT_EVALUATED_INSUFFICIENT_TRAINING_DECISIONS",
            "selected_family": "hist-gradient",
            "training_decisions": int(train["target_window_start"].nunique()),
            "minimum_required_decisions": _NEURAL_CHALLENGER_MINIMUM_DECISIONS,
            "calibration_used_for_selection": False,
            "assessment_used_for_selection": False,
        }

    inner_train, validation, purged_rows = split
    try:
        hist_validation_model = _probability_estimator(
            numeric, categorical, policy=policy
        )
        neural_validation_model = _neural_probability_estimator(
            numeric, categorical, policy=policy
        )
        _fit_probability_pipeline(
            hist_validation_model,
            inner_train,
            numeric=numeric,
            categorical=categorical,
        )
        _fit_probability_pipeline(
            neural_validation_model,
            inner_train,
            numeric=numeric,
            categorical=categorical,
        )
        validation_matrix = _matrix(validation, numeric, categorical)
        target = validation["profitable"].astype(int).to_numpy()
        weights = _decision_weights(validation)
        hist_probability = _validated_probability_array(
            hist_validation_model.predict_proba(validation_matrix)[:, 1],
            label="Neural challenger HGB validation",
        )
        neural_probability = _validated_probability_array(
            neural_validation_model.predict_proba(validation_matrix)[:, 1],
            label="Neural challenger MLP validation",
        )
        candidate_metrics: dict[str, dict[str, object]] = {
            "hist-gradient": {
                "neural_weight": 0.0,
                **_probability_metrics(target, hist_probability, sample_weight=weights),
            }
        }
        for neural_weight in _NEURAL_BLEND_WEIGHTS:
            probability = (
                (1.0 - neural_weight) * hist_probability
                + neural_weight * neural_probability
            )
            label = (
                "mlp-neural-network"
                if neural_weight == 1.0
                else f"hist-gradient-mlp-{neural_weight:.2f}"
            )
            candidate_metrics[label] = {
                "neural_weight": neural_weight,
                **_probability_metrics(target, probability, sample_weight=weights),
            }
        baseline_loss = float(candidate_metrics["hist-gradient"]["log_loss"])
        required_improvement = max(
            1e-4,
            baseline_loss * _NEURAL_CHALLENGER_REQUIRED_RELATIVE_IMPROVEMENT,
        )
        eligible = [
            (name, metrics)
            for name, metrics in candidate_metrics.items()
            if name != "hist-gradient"
            and float(metrics["log_loss"]) <= baseline_loss - required_improvement
        ]
        if eligible:
            selected_name, selected_metrics = min(
                eligible,
                key=lambda item: (
                    float(item[1]["log_loss"]),
                    float(item[1]["brier_score"]),
                    float(item[1]["neural_weight"]),
                ),
            )
            neural_weight = float(selected_metrics["neural_weight"])
        else:
            selected_name = "hist-gradient"
            neural_weight = 0.0
        report: dict[str, object] = {
            "policy_version": _NEURAL_CHALLENGER_POLICY_VERSION,
            "status": "EVALUATED",
            "selected_family": selected_name,
            "selected_neural_weight": neural_weight,
            "required_relative_log_loss_improvement": (
                _NEURAL_CHALLENGER_REQUIRED_RELATIVE_IMPROVEMENT
            ),
            "required_absolute_log_loss_improvement": required_improvement,
            "inner_training_rows": len(inner_train),
            "inner_training_decisions": int(
                inner_train["target_window_start"].nunique()
            ),
            "validation_rows": len(validation),
            "validation_decisions": int(
                validation["target_window_start"].nunique()
            ),
            "boundary_purged_rows": purged_rows,
            "validation_target_start_min": pd.Timestamp(
                validation["target_window_start"].min()
            ).isoformat(),
            "validation_target_start_max": pd.Timestamp(
                validation["target_window_start"].max()
            ).isoformat(),
            "candidate_metrics": candidate_metrics,
            "calibration_used_for_selection": False,
            "assessment_used_for_selection": False,
        }
    except Exception as exc:
        selected_name = "hist-gradient"
        neural_weight = 0.0
        report = {
            "policy_version": _NEURAL_CHALLENGER_POLICY_VERSION,
            "status": "CHALLENGER_FAILED_SAFE_TO_HIST_GRADIENT",
            "selected_family": selected_name,
            "error": f"{type(exc).__name__}: {exc}",
            "calibration_used_for_selection": False,
            "assessment_used_for_selection": False,
        }

    hist_gradient = _probability_estimator(numeric, categorical, policy=policy)
    _fit_probability_pipeline(
        hist_gradient,
        train,
        numeric=numeric,
        categorical=categorical,
    )
    if neural_weight <= 0.0:
        return hist_gradient, "hist-gradient", report

    try:
        neural_network = _neural_probability_estimator(
            numeric, categorical, policy=policy
        )
        _fit_probability_pipeline(
            neural_network,
            train,
            numeric=numeric,
            categorical=categorical,
        )
    except Exception as exc:
        report = {
            **report,
            "status": "SELECTED_CHALLENGER_REFIT_FAILED_SAFE_TO_HIST_GRADIENT",
            "selected_family": "hist-gradient",
            "selected_neural_weight": 0.0,
            "refit_error": f"{type(exc).__name__}: {exc}",
        }
        return hist_gradient, "hist-gradient", report
    if neural_weight >= 1.0:
        return neural_network, "mlp-neural-network", report
    return (
        _ProbabilityBlend(
            hist_gradient,
            neural_network,
            neural_weight=neural_weight,
        ),
        "hist-gradient-mlp-ensemble",
        report,
    )


def _fit_probability_pipeline(
    estimator: Pipeline,
    frame: pd.DataFrame,
    *,
    numeric: tuple[str, ...],
    categorical: tuple[str, ...],
) -> None:
    target = frame["profitable"].astype(int)
    if target.nunique() != 2:
        raise ValueError("Strategy probability fitting requires both outcome classes")
    fit_with_progress(
        estimator, _matrix(frame, numeric, categorical), target,
        label=f"strategy/{type(estimator.steps[-1][1]).__name__}",
        model__sample_weight=_decision_weights(frame),
    )


def _neural_challenger_split(
    train: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, int] | None:
    clusters = pd.Index(
        pd.to_datetime(
            train["target_window_start"], utc=True, errors="coerce"
        ).dropna().drop_duplicates().sort_values()
    )
    if len(clusters) < _NEURAL_CHALLENGER_MINIMUM_DECISIONS:
        return None
    validation_count = min(
        _NEURAL_CHALLENGER_MAXIMUM_VALIDATION_DECISIONS,
        max(
            1,
            int(math.ceil(len(clusters) * _NEURAL_CHALLENGER_VALIDATION_FRACTION)),
        ),
    )
    validation_clusters = clusters[-validation_count:]
    boundary = pd.Timestamp(validation_clusters[0])
    starts = pd.to_datetime(train["target_window_start"], utc=True, errors="coerce")
    ends = pd.to_datetime(train["target_window_end"], utc=True, errors="coerce")
    inner = train.loc[starts.lt(boundary) & ends.lt(boundary)].copy()
    validation = train.loc[starts.isin(validation_clusters)].copy()
    if (
        inner.empty
        or validation.empty
        or inner["profitable"].nunique() != 2
        or inner["target_window_start"].nunique() < 2
    ):
        return None
    inner = inner.sort_values(
        ["target_window_start", "strategy_name", "candidate_key"],
        kind="mergesort",
    ).reset_index(drop=True)
    validation = validation.sort_values(
        ["target_window_start", "strategy_name", "candidate_key"],
        kind="mergesort",
    ).reset_index(drop=True)
    purged = int(len(train) - len(inner) - len(validation))
    return inner, validation, purged


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
    predicted = np.asarray(expected_return, dtype=float)
    if len(predicted) != len(frame) or not np.isfinite(predicted).all():
        raise ValueError("Strategy expected-return predictions must be finite")
    capital = pd.to_numeric(frame["capital_required"], errors="coerce").to_numpy(
        dtype=float
    )
    if not np.isfinite(capital).all() or np.any(capital <= 0.0):
        raise ValueError("Strategy scoring requires finite positive capital")
    profit = predicted * capital
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
    model_selection: Mapping[str, object],
    probability_model_family: str,
) -> dict[str, object]:
    assessment_target = partitions.assessment["profitable"].astype(int).to_numpy()
    training_target = partitions.train["profitable"].astype(int).to_numpy()
    training_weights = _decision_weights(partitions.train)
    training_base_rate = float(
        np.average(training_target, weights=training_weights)
    )
    assessment_matrix = _matrix(partitions.assessment, numeric, categorical)
    raw = _validated_probability_array(
        estimator.predict_proba(assessment_matrix)[:, 1],
        label="Assessment raw model",
    )
    calibrated = _validated_probability_array(
        calibrator.predict(raw),
        label="Assessment calibrated model",
    )
    expected_return, _expected_profit = _bounded_expected_return(
        partitions.assessment,
        _prior_return(partitions.assessment)
        + np.asarray(return_estimator.predict(assessment_matrix), dtype=float),
    )
    support_min = float(np.min(calibration_raw))
    support_max = float(np.max(calibration_raw))
    assessment = partitions.assessment.copy()
    assessment["probability"] = calibrated
    assessment["expected_return"] = expected_return
    probability_first = _ranking_policy_evidence(
        assessment,
        primary="probability",
        secondary="expected_return",
        ranking_rule=(
            "highest_calibrated_probability_then_expected_return_on_risk_"
            "then_candidate_key_per_decision"
        ),
        role="ACTIVE",
    )
    expected_return_first = _ranking_policy_evidence(
        assessment,
        primary="expected_return",
        secondary="probability",
        ranking_rule=(
            "highest_expected_return_on_risk_then_calibrated_probability_"
            "then_candidate_key_per_decision"
        ),
        role="BENCHMARK",
    )
    return {
        "status": "OFFLINE_ASSESSMENT_COMPLETE",
        "assessment_used_for_training": False,
        "assessment_used_for_calibration": False,
        "assessment_used_for_ranking_policy_selection": False,
        "real_lockbox_used": False,
        "fit_partition": "training",
        "calibration_partition": "calibration",
        "ranking_rule": (
            "highest_calibrated_probability_then_expected_return_on_risk_"
            "then_candidate_key_per_decision"
        ),
        "benchmark_ranking_rule": (
            "highest_expected_return_on_risk_then_calibrated_probability_"
            "then_candidate_key_per_decision"
        ),
        "training_rows": len(partitions.train),
        "training_decisions": partitions.train_decisions,
        "calibration_rows": len(partitions.calibration),
        "calibration_decisions": partitions.calibration_decisions,
        "assessment_rows": len(partitions.assessment),
        "assessment_decisions": partitions.assessment_decisions,
        "boundary_purged_rows": partitions.purged_rows,
        "probability_model_family": probability_model_family,
        "probability_model_selection": dict(model_selection),
        "effective_calibration_method": effective_calibration,
        "calibration_support": {
            "raw_probability_min": support_min,
            "raw_probability_max": support_max,
            "assessment_below": int((raw < support_min).sum()),
            "assessment_above": int((raw > support_max).sum()),
        },
        "metric_weighting": "equal_weight_per_target_window_start",
        "base_rate_model": {
            "probability_source": "equal-decision-weighted-training_base_rate",
            "training_base_rate": training_base_rate,
            **_probability_metrics(
                assessment_target,
                np.full(len(assessment_target), training_base_rate, dtype=float),
                sample_weight=_decision_weights(partitions.assessment),
            ),
        },
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
        "ranking_policy_assessment": {
            "probability_first": probability_first,
            "expected_return_first_benchmark": expected_return_first,
        },
    }


def _ranking_policy_evidence(
    assessment: pd.DataFrame,
    *,
    primary: str,
    secondary: str,
    ranking_rule: str,
    role: str,
) -> dict[str, object]:
    top_ranked = (
        assessment.sort_values(
            ["target_window_start", primary, secondary, "candidate_key"],
            ascending=[True, False, False, True],
            kind="mergesort",
        )
        .groupby("target_window_start", sort=False)
        .head(1)
    )
    profitable = pd.to_numeric(
        top_ranked["profitable"], errors="coerce"
    ).to_numpy(dtype=float)
    realized_return = pd.to_numeric(
        top_ranked["return_on_risk"], errors="coerce"
    ).to_numpy(dtype=float)
    net_profit = pd.to_numeric(
        top_ranked["net_profit"], errors="coerce"
    ).to_numpy(dtype=float)
    probability = pd.to_numeric(
        top_ranked["probability"], errors="coerce"
    ).to_numpy(dtype=float)
    if not all(
        np.isfinite(values).all()
        for values in (profitable, realized_return, net_profit, probability)
    ):
        raise ValueError("Top-candidate assessment values must be finite")
    return {
        "role": role,
        "ranking_rule": ranking_rule,
        "decision_count": len(top_ranked),
        "top_candidate_profitable_rate": (
            float(np.mean(profitable)) if len(top_ranked) else None
        ),
        "mean_realized_return_on_risk": (
            float(np.mean(realized_return)) if len(top_ranked) else None
        ),
        "total_net_profit": (
            float(np.sum(net_profit)) if len(top_ranked) else None
        ),
        "probability_calibration": (
            _probability_metrics(
                profitable.astype(int),
                probability,
            )
            if len(top_ranked)
            else None
        ),
    }


def _validated_probability_array(
    values: object,
    *,
    label: str,
) -> np.ndarray:
    probability = np.asarray(values, dtype=float)
    if (
        probability.ndim != 1
        or not np.isfinite(probability).all()
        or np.any(probability < 0.0)
        or np.any(probability > 1.0)
    ):
        raise ValueError(f"{label} probabilities must be a finite one-dimensional array in [0, 1]")
    return probability


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
    calibration_bins: list[dict[str, object]] = []
    expected_calibration_error = 0.0
    boundaries = np.linspace(0.0, 1.0, 11)
    total_weight = float(np.sum(weights))
    for index in range(10):
        lower = float(boundaries[index])
        upper = float(boundaries[index + 1])
        selected = (
            (probability >= lower)
            & (
                probability <= upper
                if index == 9
                else probability < upper
            )
        )
        count = int(selected.sum())
        selected_weight = float(np.sum(weights[selected]))
        if count:
            mean_probability = float(
                np.average(probability[selected], weights=weights[selected])
            )
            observed_rate = float(
                np.average(target[selected], weights=weights[selected])
            )
            if total_weight > 0.0:
                expected_calibration_error += (
                    selected_weight
                    / total_weight
                    * abs(mean_probability - observed_rate)
                )
        else:
            mean_probability = None
            observed_rate = None
        calibration_bins.append(
            {
                "lower": lower,
                "upper": upper,
                "rows": count,
                "weight": selected_weight,
                "mean_probability": mean_probability,
                "observed_rate": observed_rate,
            }
        )
    return {
        "rows": len(target),
        "target_base_rate": float(np.average(target, weights=weights)),
        "log_loss": float(log_loss),
        "brier_score": float(brier),
        "roc_auc": auc,
        "accuracy_0_5": float(accuracy),
        "expected_calibration_error_10_bin": float(
            expected_calibration_error
        ),
        "reliability_bins": calibration_bins,
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
    configuration = {
        "model_name": "market-state-strategy-outcome",
        "model_family": "hgb-mlp-challenger-classifier-hgb-regressor",
        "probability_model_policy": {
            "version": _NEURAL_CHALLENGER_POLICY_VERSION,
            "baseline": "hist-gradient",
            "challenger": "mlp-neural-network",
            "selection_partition": "chronological_tail_of_training_only",
            "selection_metric": "equal-decision-weighted-log-loss",
            "minimum_training_decisions": (
                _NEURAL_CHALLENGER_MINIMUM_DECISIONS
            ),
            "maximum_validation_decisions": (
                _NEURAL_CHALLENGER_MAXIMUM_VALIDATION_DECISIONS
            ),
            "required_relative_improvement": (
                _NEURAL_CHALLENGER_REQUIRED_RELATIVE_IMPROVEMENT
            ),
            "calibration_used_for_selection": False,
            "assessment_used_for_selection": False,
        },
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
        "decision_score_definition": (
            "calibrated_probability_of_strictly_positive_net_profit"
        ),
        "fallback_score_definition": (
            "nonprobabilistic_pricing_informed_scenario_grid_coverage"
        ),
        "assessment_policy_selection": "fixed_before_assessment",
        "calibration_method": "platt",
        "policy": asdict(policy),
        "training_rows": len(partitions.train),
        "training_decisions": partitions.train_decisions,
        "calibration_rows": len(partitions.calibration),
        "calibration_decisions": partitions.calibration_decisions,
        "assessment_rows": len(partitions.assessment),
        "assessment_decisions": partitions.assessment_decisions,
        "training_data_fingerprint_sha256": _partition_fingerprint(partitions),
        "training_through": pd.to_datetime(
            partitions.train["target_window_end"], utc=True
        ).max().isoformat(),
        "input_files": input_inventory(input_files, relative_to=datastore_root),
    }
    # Manifests pass through JSON, which turns policy tuples into lists. Return
    # the same canonical representation before comparing a live configuration
    # with an existing generation; otherwise every run needlessly refits.
    return json.loads(json.dumps(configuration, sort_keys=True, default=str))


def _partition_fingerprint(partitions: StrategyPartitions) -> str:
    """Hash the actual train/calibration/assessment cohorts, not broad inputs."""

    digest = hashlib.sha256()
    for label, frame in (
        ("train", partitions.train),
        ("calibration", partitions.calibration),
        ("assessment", partitions.assessment),
    ):
        digest.update(label.encode("utf-8"))
        normalized = frame.reindex(sorted(frame.columns), axis=1).copy()
        digest.update("\x1f".join(normalized.columns).encode("utf-8"))
        for column in normalized.columns:
            normalized[column] = normalized[column].map(_fingerprint_value)
        row_hashes = pd.util.hash_pandas_object(
            normalized,
            index=False,
            categorize=True,
        ).to_numpy(dtype="uint64", copy=True)
        row_hashes.sort()
        digest.update(len(normalized).to_bytes(8, byteorder="big", signed=False))
        digest.update(row_hashes.tobytes())
    return digest.hexdigest()


def _fingerprint_value(value: object) -> str:
    if value is None or value is pd.NA or value is pd.NaT:
        return "<null>"
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    try:
        if pd.isna(value):
            return "<null>"
    except (TypeError, ValueError):
        pass
    return str(value)


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
        compatibility = {
            key: value for key, value in expected.items() if key != "input_files"
        }
        if any(manifest.get(key) != value for key, value in compatibility.items()):
            return None
        if manifest.get("effective_calibration_method") != "platt":
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
            "probability_model_family": manifest.get(
                "selected_probability_model_family", "hist-gradient"
            ),
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
    "PRICING_NUMERIC_FEATURES",
    "fit_or_reuse_strategy_model",
    "partition_strategy_outcomes",
    "score_strategy_candidates",
]
