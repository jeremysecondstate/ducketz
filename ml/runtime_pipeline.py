from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from datafetching.observability import timed_stage
from ml.artifacts import (
    create_timestamp_directory,
    file_checksum,
    utc_timestamp,
    verify_manifest,
    write_manifest,
)
from ml.calendars import ExchangeSessionCalendar
from ml.current_publication import (
    PUBLICATION_CONTRACT_VERSION,
    PUBLICATION_RECEIPT_NAME,
    CurrentPublicationError,
    authoritative_pointer_payload,
    authoritative_receipt_runs,
    expected_run_path,
    publication_contract_kind,
    publication_record,
    read_current_publication,
    read_publication_receipt,
    resolve_current_output,
    verify_publication_receipt,
)
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY
from ml.horizons import (
    DEFAULT_FEATURE_PROFILE,
    FEATURE_PROFILES,
    INTERNAL_HORIZON_ORDER,
    WEEKLY_HORIZON_ORDER,
    HorizonSpecification,
    feature_contract_horizon,
    horizon_specifications_for_profile,
    is_weekly_horizon,
)
from ml.live_evidence import (
    MINIMUM_LIVE_DECISIONS,
    live_evidence_status,
    minimum_live_decisions,
)
from ml.model_runtime import (
    DEFAULT_PARTITION_CONFIGS,
    ModelPartitionConfig,
    ModelPartitions,
    RuntimeModel,
    fit_or_reuse_model,
    partition_model_rows,
)
from ml.parquet_contracts import (
    EVALUATION_SCHEMA,
    INTELLIGENCE_SCHEMA,
    MONITORING_SCHEMA,
    PREDICTION_SCHEMA,
    empty_frame,
    frame_with_readable_id,
    sample_schema,
    write_parquet_with_schema,
)
from ml.rolling_materialization import RollingMaterialization, materialize_rolling_samples
from ml.strategy_selection import (
    STRATEGY_SELECTION_OPRA_FIRST_SPREADS_V2,
)
from ml.strategy_selection.research_trace import strategy_research_trace

_RUN_OUTPUT_NAMES = frozenset(
    {
        "samples.parquet",
        "predictions.parquet",
        "evaluations.parquet",
        "monitoring.parquet",
        "intelligence.parquet",
    }
)
_PUBLICATION_RECEIPT_VERSION = PUBLICATION_CONTRACT_VERSION
_PUBLICATION_RECEIPT_NAME = PUBLICATION_RECEIPT_NAME
_LEGACY_TARGET_DEFINITION_VERSIONS: Mapping[str, str] = {
    "1h": "next-full-exchange-hour-open-close-v1",
    "4h": "next-four-eligible-exchange-hours-open-close-v1",
    "1d": "next-session-open-close-v1",
    "1w": "weekly-context-next-session-open-close-v2",
}
OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION = (
    "option-pricing-loop-b-family-coverage-freshness-gate-v1"
)
OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE = 0.80
OPTION_PRICING_LOOP_B_MINIMUM_DISTINCT_TARGETS = 20
_OPTION_PRICING_FEATURE_GROUPS: Mapping[str, tuple[str, ...]] = {
    "fair_value": (
        "opx__causal_coverage",
        "opx__median_normalized_residual",
    ),
    "uncertainty": ("opx__median_predictive_standard_deviation",),
    "edge": (
        "opx__median_model_edge_in_half_spreads",
        "opx__positive_edge_fraction",
        "opx__negative_edge_fraction",
    ),
    "constraints": (
        "opx__raw_arbitrage_violation_rate",
        "opx__constrained_arbitrage_violation_rate",
    ),
    "interval_calibration": (
        "opx__interval_80_coverage",
        "opx__interval_95_coverage",
    ),
    "liquidity": ("opx__median_relative_bid_ask_spread",),
}
_OPTION_PRICING_BASELINE_FEATURE_SETS: Mapping[str, str] = {
    "loop-a-all-bsgp-shadow-v1-1h": "loop-a-all-v1-1h",
    "loop-a-all-bsgp-shadow-v1-4h": "loop-a-all-v1-4h",
    "loop-a-all-bsgp-shadow-v1-1d": "loop-a-all-v1-1d",
    "loop-a-all-bsgp-shadow-v1-1w": "loop-a-all-v1-1w",
    "loop-a-all-bsgp-active-v2-1h": "loop-a-all-v1-1h",
    "loop-a-all-bsgp-active-v2-4h": "loop-a-all-v1-4h",
    "loop-a-all-bsgp-active-v2-1d": "loop-a-all-v1-1d",
    "loop-a-all-bsgp-active-v2-1w": "loop-a-all-v1-1w",
    "loop-a-all-bsgp-active-v3-1d": "loop-a-all-v3-1d",
    "loop-a-all-bsgp-active-v3-1w": "loop-a-all-v3-1w",
}


@dataclass(frozen=True)
class RuntimeConfig:
    provider: str = "databento"
    model_family: str = "logistic"
    calibration_method: str = "platt"
    class_weight: str | None = None
    assumed_round_trip_cost: float = 0.001
    minimum_train_clusters: int | None = None
    calibration_clusters: int | None = None
    assessment_clusters: int | None = None
    lockbox_clusters: int | None = None
    latest_per_symbol: bool = True
    require_all_routes: bool = False
    feature_profile: str = DEFAULT_FEATURE_PROFILE

    def __post_init__(self) -> None:
        if self.model_family not in {"logistic", "lightgbm", "xgboost"}:
            raise ValueError("Unsupported model family")
        if self.calibration_method not in {"none", "platt", "isotonic"}:
            raise ValueError("Unsupported calibration method")
        if self.class_weight not in {None, "balanced"}:
            raise ValueError("class_weight must be None or balanced")
        if not 0.0 <= self.assumed_round_trip_cost < 1.0:
            raise ValueError("assumed_round_trip_cost must satisfy 0 <= cost < 1")
        if self.feature_profile not in FEATURE_PROFILES:
            raise ValueError(
                "Unsupported feature_profile; expected one of "
                + ", ".join(FEATURE_PROFILES)
            )
        for name in (
            "minimum_train_clusters",
            "calibration_clusters",
            "assessment_clusters",
            "lockbox_clusters",
        ):
            value = getattr(self, name)
            if value is not None and value < 1:
                raise ValueError(f"{name} must be positive when provided")

    def partition_for(self, horizon: str) -> ModelPartitionConfig:
        try:
            defaults = DEFAULT_PARTITION_CONFIGS[horizon]
        except KeyError as exc:
            raise ValueError(f"No partition defaults exist for {horizon}") from exc
        return ModelPartitionConfig(
            minimum_train_clusters=(
                self.minimum_train_clusters
                if self.minimum_train_clusters is not None
                else defaults.minimum_train_clusters
            ),
            calibration_clusters=(
                self.calibration_clusters
                if self.calibration_clusters is not None
                else defaults.calibration_clusters
            ),
            assessment_clusters=(
                self.assessment_clusters
                if self.assessment_clusters is not None
                else defaults.assessment_clusters
            ),
            lockbox_clusters=(
                self.lockbox_clusters
                if self.lockbox_clusters is not None
                else defaults.lockbox_clusters
            ),
        )


@dataclass(frozen=True)
class LoopBResult:
    run_directory: Path
    sample_rows: int
    prediction_rows: int
    evaluation_rows: int
    monitoring_rows: int
    intelligence_rows: int
    models_trained: int
    models_reused: int
    route_errors: Mapping[str, str]
    latest_intelligence_path: Path
    backtest_prediction_rows: int = 0
    fresh_live_prediction_rows: int = 0
    carried_active_live_prediction_rows: int = 0
    retained_weekly_live_prediction_rows: int = 0
    actionable_ordinary_routes: int = 0
    in_progress_ordinary_routes: int = 0
    strategy_candidate_rows: int = 0
    strategy_audit_rows: int = 0
    strategy_models_trained: int = 0
    strategy_models_reused: int = 0

    @property
    def status(self) -> str:
        return "COMPLETED_WITH_LIMITATIONS" if self.route_errors else "COMPLETED"


@dataclass(frozen=True)
class VerifiedWeeklyPredictionRun:
    """One receipt-chain run that can authoritatively prove an issuance."""

    run_directory: Path
    promoted_at: pd.Timestamp
    predictions: pd.DataFrame


def discover_symbols(datastore_root: Path) -> tuple[str, ...]:
    stocks = Path(datastore_root) / "stocks"
    if not stocks.is_dir():
        return ()
    return tuple(
        path.name.strip().upper()
        for path in sorted(stocks.iterdir())
        if path.is_dir() and path.name.strip()
    )


def run_loop_b_once(
    datastore_root: Path,
    *,
    symbols: Sequence[str] | None = None,
    config: RuntimeConfig | None = None,
    specifications: Mapping[str, HorizonSpecification] | None = None,
    input_available_at: object,
    run_timestamp: object | None = None,
    runtime_clock: Callable[[], object] | None = None,
    enforce_publication_deadline: bool = True,
    reporter: Callable[[str], None] | None = print,
) -> LoopBResult:
    selected = tuple(symbols or ())
    with timed_stage(
        "loop-b.directional-publication",
        provider=(config or RuntimeConfig()).provider,
        reporter=reporter,
        extra={"symbol_count": len(selected) if symbols is not None else None},
    ) as timing:
        result = _run_loop_b_once(
            datastore_root,
            symbols=symbols,
            config=config,
            specifications=specifications,
            input_available_at=input_available_at,
            run_timestamp=run_timestamp,
            runtime_clock=runtime_clock,
            enforce_publication_deadline=enforce_publication_deadline,
            reporter=reporter,
        )
        timing.annotate(
            row_count=result.prediction_rows,
            operation="wrote",
            sample_rows=result.sample_rows,
            run_directory=str(result.run_directory),
        )
        return result


def _run_loop_b_once(
    datastore_root: Path,
    *,
    symbols: Sequence[str] | None = None,
    config: RuntimeConfig | None = None,
    specifications: Mapping[str, HorizonSpecification] | None = None,
    input_available_at: object,
    run_timestamp: object | None = None,
    runtime_clock: Callable[[], object] | None = None,
    enforce_publication_deadline: bool = True,
    reporter: Callable[[str], None] | None = print,
) -> LoopBResult:
    root = Path(datastore_root)
    if not root.is_dir():
        raise FileNotFoundError(f"Datastore does not exist: {root}")
    runtime = config or RuntimeConfig()
    effective_specifications = (
        dict(specifications)
        if specifications is not None
        else horizon_specifications_for_profile(runtime.feature_profile)
    )
    created = utc_timestamp(run_timestamp)
    input_cutoff = utc_timestamp(input_available_at)
    clock = (
        runtime_clock
        if runtime_clock is not None
        else (
            (lambda: utc_timestamp())
            if run_timestamp is None
            else (lambda: created)
        )
    )
    selected_symbols = tuple(
        dict.fromkeys(
            str(symbol).strip().upper()
            for symbol in (symbols or discover_symbols(root))
            if str(symbol).strip()
        )
    )
    if not selected_symbols:
        raise ValueError("Loop B requires at least one symbol")
    materialization = materialize_rolling_samples(
        root,
        symbols=selected_symbols,
        provider=runtime.provider,
        specifications=effective_specifications,
        assumed_round_trip_cost=runtime.assumed_round_trip_cost,
        materialized_at=created,
        input_available_at=input_cutoff,
        reporter=reporter,
    )
    failed_routes = [
        route
        for route in materialization.routes
        if route.status != "READY" or route.error
    ]
    if failed_routes and runtime.require_all_routes:
        rendered = ", ".join(
            f"{route.symbol}/{route.horizon}" for route in failed_routes
        )
        details = "; ".join(
            f"{route.symbol}|{route.horizon}: "
            f"{route.error or route.status.replace('_', ' ').lower()}"
            for route in failed_routes
        )
        raise RuntimeError(
            "Loop B produced no predictions for required routes: "
            f"{rendered} ({details})"
        )
    feature_columns = _feature_columns(effective_specifications)
    samples_contract = sample_schema(feature_columns)
    samples = _project_samples(
        materialization.samples,
        schema_names=samples_contract.names,
    )

    runs_root = root / "ml" / "runs"
    run_directory = create_timestamp_directory(runs_root, timestamp=created)
    samples_path = run_directory / "samples.parquet"
    prior_predictions = _load_prior_live_predictions(
        runs_root,
        run_directory,
        as_of=created,
        specifications=effective_specifications,
    )
    verified_weekly_runs = (
        _load_verified_weekly_prediction_runs(
            root,
            current_run=run_directory,
            as_of=created,
            specifications=effective_specifications,
            assumed_round_trip_cost=runtime.assumed_round_trip_cost,
        )
        if any(is_weekly_horizon(value) for value in effective_specifications)
        else ()
    )
    if verified_weekly_runs:
        verified_weekly_predictions = _verified_weekly_prediction_rows(
            verified_weekly_runs,
            samples=samples,
            specifications=effective_specifications,
            assumed_round_trip_cost=runtime.assumed_round_trip_cost,
        )
        prior_predictions = pd.concat(
            [prior_predictions, verified_weekly_predictions],
            ignore_index=True,
            sort=False,
        ).drop_duplicates(
            [
                "symbol",
                "horizon",
                "decision_timestamp",
                "prediction_created_at",
            ],
            keep="last",
        ).reset_index(drop=True)

    prediction_frames: list[pd.DataFrame] = []
    fresh_live_frames: list[pd.DataFrame] = []
    models: dict[str, RuntimeModel] = {}
    partitions_by_horizon: dict[str, ModelPartitions] = {}
    pricing_model_admission: dict[str, dict[str, object]] = {}

    route_errors: dict[str, str] = {
        f"{route.symbol}|{route.horizon}": (
            route.error or route.status.replace("_", " ").lower()
        )
        for route in materialization.routes
        if route.status != "READY" or route.error
    }
    for horizon, specification in effective_specifications.items():
        route_samples = samples.loc[samples["horizon"].eq(horizon)].copy()
        if route_samples.empty:
            route_errors[f"model|{horizon}"] = "No materialized samples"
            continue
        try:
            requested_feature_set = DEFAULT_FEATURE_REGISTRY.feature_set(
                specification.feature_set,
                require_active=True,
                horizon=feature_contract_horizon(horizon),
            )
            pricing_evidence_rows = materialization.samples.loc[
                materialization.samples["horizon"].eq(horizon)
            ].copy()
            pricing_gate = _pricing_family_gate(
                pricing_evidence_rows,
                feature_columns=requested_feature_set.names,
            )
            model_specification = _specification_for_pricing_gate(
                specification,
                gate=pricing_gate,
            )
            pricing_admitted = (
                model_specification.feature_set == specification.feature_set
            )
            pricing_model_admission[horizon] = {
                "policy_version": OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION,
                "pricing_family_selected": bool(pricing_gate["enabled"]),
                "pricing_family_admitted": bool(
                    pricing_gate["enabled"] and pricing_admitted
                ),
                "requested_feature_set": specification.feature_set,
                "effective_model_feature_set": (
                    model_specification.feature_set
                ),
                "failed_routes": list(pricing_gate["failed_routes"]),
            }
            if pricing_gate["enabled"] and not pricing_admitted:
                failed = ", ".join(pricing_gate["failed_routes"])
                _report(
                    reporter,
                    f"[Loop B/{horizon}] Option Pricing family quarantined; "
                    f"fitting {model_specification.feature_set} until the "
                    "coverage/freshness gate passes"
                    + (f" ({failed})" if failed else ""),
                )
            partitions = partition_model_rows(
                route_samples,
                config=runtime.partition_for(horizon),
                excluded_target_starts=tuple(
                    _compatible_prospective_live_predictions(
                        prior_predictions,
                        specification=specification,
                        assumed_round_trip_cost=(
                            runtime.assumed_round_trip_cost
                        ),
                    ).loc[
                        lambda frame: frame["horizon"].eq(horizon),
                        "target_window_start",
                    ]
                ),
            )
            model = fit_or_reuse_model(
                root,
                horizon=horizon,
                feature_set_name=model_specification.feature_set,
                family=runtime.model_family,
                calibration_method=runtime.calibration_method,
                class_weight=runtime.class_weight,
                partitions=partitions,
                input_files=_horizon_source_files(materialization, horizon),
                specification=model_specification,
                assumed_round_trip_cost=runtime.assumed_round_trip_cost,
                trained_at=created,
            )
            prediction_created_at = utc_timestamp(clock())
            horizon_prediction_frames = [
                _prediction_frame(
                    model,
                    partitions.assessment,
                    prediction_created_at=prediction_created_at,
                    mode="BACKTEST",
                )
            ]
            if not is_weekly_horizon(horizon):
                live = _live_candidates(
                    route_samples,
                    as_of=prediction_created_at,
                    latest_per_symbol=runtime.latest_per_symbol,
                )
                if not live.empty:
                    live_predictions = _prediction_frame(
                        model,
                        live,
                        prediction_created_at=prediction_created_at,
                        mode="LIVE",
                    )
                    horizon_prediction_frames.append(live_predictions)
                    fresh_live_frames.append(live_predictions)
            models[horizon] = model
            partitions_by_horizon[horizon] = partitions
            prediction_frames.extend(horizon_prediction_frames)
        except Exception as exc:
            error_key = f"model|{horizon}"
            route_errors[error_key] = f"{type(exc).__name__}: {exc}"
            _report(reporter, f"[Loop B/{horizon}] {route_errors[error_key]}")

    weekly_horizons = tuple(
        horizon
        for horizon in WEEKLY_HORIZON_ORDER
        if horizon in effective_specifications
    )
    ready_route_pairs = {
        (route.symbol, route.horizon)
        for route in materialization.routes
        if route.status == "READY" and not route.error
    }
    if weekly_horizons:
        weekly_created_at = utc_timestamp(clock())
        for symbol in selected_symbols:
            try:
                weekly_live, newly_issued = _weekly_live_predictions(
                    samples,
                    models=models,
                    verified_runs=verified_weekly_runs,
                    specifications=effective_specifications,
                    symbols=(symbol,),
                    assumed_round_trip_cost=runtime.assumed_round_trip_cost,
                    prediction_created_at=weekly_created_at,
                )
                if weekly_live.empty:
                    error_key = f"{symbol}|weekly-snapshot"
                    route_errors[error_key] = (
                        "No usable per-symbol decision, remaining-session "
                        "route set, and fitted models; weekly LIVE rows skipped"
                    )
                    _report(
                        reporter,
                        f"[Loop B/{symbol} weekly snapshot] SKIPPED: "
                        f"{route_errors[error_key]}",
                    )
                else:
                    prediction_frames.append(weekly_live)
                if not newly_issued.empty:
                    fresh_live_frames.append(newly_issued)
            except Exception as exc:
                error_key = f"{symbol}|weekly-snapshot"
                route_errors[error_key] = f"{type(exc).__name__}: {exc}"
                _report(
                    reporter,
                    f"[Loop B/{symbol} weekly snapshot] {route_errors[error_key]}",
                )

    current_run_predictions = (
        pd.concat(prediction_frames, ignore_index=True, sort=False)
        if prediction_frames
        else empty_frame(PREDICTION_SCHEMA)
    )
    current_run_predictions = _project(
        current_run_predictions,
        PREDICTION_SCHEMA.names,
    )
    fresh_live_predictions = (
        pd.concat(fresh_live_frames, ignore_index=True, sort=False)
        if fresh_live_frames
        else empty_frame(PREDICTION_SCHEMA)
    )
    fresh_live_predictions = _project(
        fresh_live_predictions,
        PREDICTION_SCHEMA.names,
    )

    # Keep evaluation time and the actual pre-promotion publication check
    # distinct. Carry eligibility is decided at the latter so a forecast is
    # never re-published at or beyond its target-window end.
    evaluated_at = utc_timestamp(clock())
    publication_checked_at = utc_timestamp(clock())
    live_deadlines = pd.to_datetime(
        fresh_live_predictions["actionable_until"],
        utc=True,
        errors="coerce",
    )
    enforce_live_target_deadline = bool(
        enforce_publication_deadline and not live_deadlines.empty
    )
    if enforce_live_target_deadline and (
        live_deadlines.isna().any()
        or publication_checked_at >= live_deadlines.min()
    ):
        raise RuntimeError(
            "Loop B publication deadline passed before atomic promotion; "
            "the prior current files remain unchanged."
        )

    carried_active_live_predictions = (
        _load_verified_active_prior_ordinary_forecasts(
            root,
            current_run=run_directory,
            publication_time=publication_checked_at,
            samples=samples,
            current_predictions=current_run_predictions,
            specifications=effective_specifications,
            assumed_round_trip_cost=runtime.assumed_round_trip_cost,
        )
    )
    predictions = pd.concat(
        [current_run_predictions, carried_active_live_predictions],
        ignore_index=True,
        sort=False,
    )
    predictions = _project(predictions, PREDICTION_SCHEMA.names)
    if not predictions.empty:
        predictions = predictions.drop_duplicates("id", keep="first").reset_index(
            drop=True
        )
    expected_routes = (
        {
            (symbol, horizon)
            for symbol in selected_symbols
            for horizon in effective_specifications
        }
        if runtime.require_all_routes
        else ready_route_pairs
    )
    observed_routes = set(
        predictions.loc[:, ["symbol", "horizon"]]
        .drop_duplicates()
        .itertuples(index=False, name=None)
    )
    missing_routes = sorted(expected_routes.difference(observed_routes))
    for symbol, horizon in missing_routes:
        route_errors.setdefault(
            f"{symbol}|{horizon}",
            "No prediction rows were produced for a materialized route",
        )

    if predictions.empty:
        rendered = ", ".join(
            f"{symbol}/{horizon}"
            for symbol in selected_symbols
            for horizon in effective_specifications
        )
        details = "; ".join(
            f"{key}: {value}" for key, value in sorted(route_errors.items())
        )
        suffix = f" ({details})" if details else ""
        raise RuntimeError(
            "Loop B produced no predictions for required routes: "
            f"{rendered}{suffix}"
        )
    if missing_routes and runtime.require_all_routes:
        rendered = ", ".join(
            f"{symbol}/{horizon}" for symbol, horizon in missing_routes
        )
        details = "; ".join(
            f"{key}: {value}" for key, value in sorted(route_errors.items())
        )
        suffix = f" ({details})" if details else ""
        raise RuntimeError(
            "Loop B produced no predictions for required routes: "
            f"{rendered}{suffix}"
        )
    if route_errors and runtime.require_all_routes:
        details = "; ".join(
            f"{key}: {value}" for key, value in sorted(route_errors.items())
        )
        raise RuntimeError(
            "Loop B route failures prevent fail-closed publication: "
            + details
        )
    if weekly_horizons and runtime.require_all_routes:
        _require_weekly_live_predictions(
            predictions,
            symbols=selected_symbols,
            specifications=effective_specifications,
        )

    published_samples = _closed_lockbox_view(
        samples,
        partitions_by_horizon=partitions_by_horizon,
    )
    write_parquet_with_schema(
        published_samples,
        samples_path,
        samples_contract,
    )
    predictions_path = run_directory / "predictions.parquet"
    write_parquet_with_schema(predictions, predictions_path, PREDICTION_SCHEMA)

    evaluation_predictions = pd.concat(
        [predictions, prior_predictions],
        ignore_index=True,
        sort=False,
    )
    if not evaluation_predictions.empty:
        evaluation_predictions = evaluation_predictions.drop_duplicates(
            ["symbol", "horizon", "decision_timestamp", "prediction_created_at"],
            keep="last",
        )
    evaluations = _evaluation_frame(
        evaluation_predictions,
        # Verified prior-LIVE target starts were excluded from offline
        # partitioning, so their matured labels remain in this redacted view.
        # Passing only the published view prevents a closed-lockbox outcome
        # from entering the evaluation join at all.
        published_samples,
        evaluated_at=evaluated_at,
    )
    evaluations_path = run_directory / "evaluations.parquet"
    write_parquet_with_schema(evaluations, evaluations_path, EVALUATION_SCHEMA)

    monitoring = _monitoring_frame(
        current_run_predictions,
        evaluations,
        models=models,
        monitored_at=evaluated_at,
    )
    monitoring_path = run_directory / "monitoring.parquet"
    write_parquet_with_schema(monitoring, monitoring_path, MONITORING_SCHEMA)

    intelligence = _intelligence_frame(
        materialization,
        published_samples,
        predictions,
        evaluations,
        models=models,
        created_at=publication_checked_at,
        carried_predictions=carried_active_live_predictions,
    )
    intelligence_path = run_directory / "intelligence.parquet"
    write_parquet_with_schema(
        intelligence,
        intelligence_path,
        INTELLIGENCE_SCHEMA,
    )

    fresh_live_ids = set(
        fresh_live_predictions["id"].dropna().astype(str)
    )
    backtest_prediction_rows = int(
        current_run_predictions["prediction_mode"].eq("BACKTEST").sum()
    )
    fresh_live_prediction_rows = len(fresh_live_predictions)
    carried_active_live_prediction_rows = len(
        carried_active_live_predictions
    )
    retained_weekly_live_prediction_rows = int(
        (
            current_run_predictions["prediction_mode"].eq("LIVE")
            & current_run_predictions["horizon"].isin(WEEKLY_HORIZON_ORDER)
            & ~current_run_predictions["id"].astype(str).isin(fresh_live_ids)
        ).sum()
    )
    ordinary_intelligence = intelligence.loc[
        ~intelligence["horizon"].isin(WEEKLY_HORIZON_ORDER)
    ]
    actionable_ordinary_routes = int(
        ordinary_intelligence["actionability_status"].eq("ACTIONABLE").sum()
    )
    in_progress_ordinary_routes = int(
        ordinary_intelligence["intelligence_status"]
        .eq("FORECAST_IN_PROGRESS")
        .sum()
    )
    publication_counts = {
        "total_prediction_rows": len(predictions),
        "backtest_prediction_rows": backtest_prediction_rows,
        "fresh_live_rows": fresh_live_prediction_rows,
        "carried_active_live_rows": carried_active_live_prediction_rows,
        "retained_frozen_weekly_live_rows": (
            retained_weekly_live_prediction_rows
        ),
        "actionable_ordinary_routes": actionable_ordinary_routes,
        "in_progress_ordinary_routes": in_progress_ordinary_routes,
    }

    output_names = (
        "samples.parquet",
        "predictions.parquet",
        "evaluations.parquet",
        "monitoring.parquet",
        "intelligence.parquet",
    )
    write_manifest(
        run_directory,
        run_timestamp=created,
        input_files=tuple(
            dict.fromkeys(
                materialization.source_files
            )
        ),
        output_files=output_names,
        feature_columns=feature_columns,
        target_column="target_cost_adjusted_positive",
        configuration={
            **asdict(runtime),
            "symbols": list(selected_symbols),
            "horizons": list(effective_specifications),
            "horizon_specifications": {
                horizon: specification.as_dict()
                for horizon, specification in effective_specifications.items()
            },
            "models": {
                horizon: model.model_name for horizon, model in models.items()
            },
            "model_feature_sets": {
                horizon: model.feature_set.name
                for horizon, model in models.items()
            },
            "partition_configuration_by_horizon": {
                horizon: asdict(runtime.partition_for(horizon))
                for horizon in effective_specifications
            },
            "route_errors": route_errors,
            "pricing_evidence": _pricing_evidence_manifest(
                materialization,
                feature_columns=feature_columns,
                model_admission_by_horizon=pricing_model_admission,
            ),
            "publication_counts": publication_counts,
            "strategy_selection": {
                "policy": STRATEGY_SELECTION_OPRA_FIRST_SPREADS_V2,
                "account_authorization": "SPREADS",
                "real_lockbox_used": False,
                "mode": "independent-runtime",
                "authority": "ml/strategy-latest/run.json",
                "research_trace": strategy_research_trace(),
            },
            "causal_input_cutoff": input_cutoff.isoformat(),
            "runtime_timing": {
                "run_started_at": created.isoformat(),
                "evaluated_at": evaluated_at.isoformat(),
                "publication_checked_at": publication_checked_at.isoformat(),
                "publication_deadline_enforced": bool(
                    enforce_publication_deadline
                ),
                "rule": (
                    "actual_scoring_and_publication_times_must_be_strictly_"
                    "before_live_actionable_until"
                ),
            },
            "publication_contract": {
                "version": _PUBLICATION_RECEIPT_VERSION,
                "receipt": _PUBLICATION_RECEIPT_NAME,
                "required_for_live_evidence": True,
                "authority": "ml/latest/run.json",
                "rule": (
                    "immutable_run_and_valid_receipt_selected_by_one_atomic_"
                    "authoritative_pointer"
                ),
            },
        },
        datastore_root=root,
    )
    latest_root = root / "ml" / "latest"
    latest_intelligence_path = (
        root / "ml-intelligence" / "latest" / "rolling-predictions.parquet"
    )
    _promote_current_outputs(
        run_directory=run_directory,
        datastore_root=root,
        output_names=output_names,
        latest_root=latest_root,
        latest_intelligence_path=latest_intelligence_path,
        clock=clock,
        enforce_target_deadline=enforce_live_target_deadline,
        target_deadline=(
            live_deadlines.min()
            if enforce_live_target_deadline
            else None
        ),
        carried_target_window_end=(
            pd.to_datetime(
                carried_active_live_predictions["target_window_end"],
                utc=True,
                errors="coerce",
            ).min()
            if (
                enforce_publication_deadline
                and not carried_active_live_predictions.empty
            )
            else None
        ),
    )
    authoritative_intelligence_path = resolve_current_output(
        root,
        "intelligence.parquet",
    )

    return LoopBResult(
        run_directory=run_directory,
        sample_rows=len(published_samples),
        prediction_rows=len(predictions),
        backtest_prediction_rows=backtest_prediction_rows,
        fresh_live_prediction_rows=fresh_live_prediction_rows,
        carried_active_live_prediction_rows=(
            carried_active_live_prediction_rows
        ),
        retained_weekly_live_prediction_rows=(
            retained_weekly_live_prediction_rows
        ),
        actionable_ordinary_routes=actionable_ordinary_routes,
        in_progress_ordinary_routes=in_progress_ordinary_routes,
        evaluation_rows=len(evaluations),
        monitoring_rows=len(monitoring),
        intelligence_rows=len(intelligence),
        models_trained=sum(not model.reused for model in models.values()),
        models_reused=sum(model.reused for model in models.values()),
        route_errors=route_errors,
        latest_intelligence_path=authoritative_intelligence_path,
    )


def _promote_current_outputs(
    *,
    run_directory: Path,
    datastore_root: Path,
    output_names: Sequence[str],
    latest_root: Path,
    latest_intelligence_path: Path,
    clock: Callable[[], object],
    enforce_target_deadline: bool,
    target_deadline: object | None,
    carried_target_window_end: object | None = None,
) -> None:
    """Commit an immutable run through one authoritative atomic pointer.

    Predictable latest files remain compatibility mirrors. They are staged and
    rollback-safe for ordinary failures, but no official reader treats them as
    the generation boundary. The receipt is durable and verified before the
    sole authoritative ``run.json`` pointer is atomically replaced.
    """

    token = run_directory.name
    promotions: list[tuple[Path, Path]] = [
        (run_directory / name, latest_root / name)
        for name in output_names
    ]
    promotions.append(
        (
            run_directory / "intelligence.parquet",
            latest_intelligence_path,
        )
    )
    pointer_destination = latest_root / "run.json"
    pointer_destination.parent.mkdir(parents=True, exist_ok=True)
    pointer_stage = pointer_destination.parent / (
        f".{pointer_destination.name}.{token}.next"
    )
    pointer_stage.unlink(missing_ok=True)
    pointer_before = (
        pointer_destination.read_bytes()
        if pointer_destination.is_file()
        else None
    )
    previous_publication: Mapping[str, object] | None = None
    if pointer_before is not None:
        previous = read_current_publication(datastore_root)
        if previous.receipt is not None:
            previous_publication = dict(previous.pointer["current"])

    staged: list[tuple[Path, Path, Path | None]] = []
    replaced: list[tuple[Path, Path | None]] = []
    publication_receipt = run_directory / _PUBLICATION_RECEIPT_NAME
    publication_receipt.unlink(missing_ok=True)
    pointer_committed = False
    try:
        for source, destination in promotions:
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage = destination.parent / (
                f".{destination.name}.{token}.next"
            )
            backup = (
                destination.parent
                / f".{destination.name}.{token}.previous"
                if destination.exists()
                else None
            )
            stage.unlink(missing_ok=True)
            if backup is not None:
                backup.unlink(missing_ok=True)
            shutil.copyfile(source, stage)
            if backup is not None:
                shutil.copyfile(destination, backup)
            staged.append((stage, destination, backup))

        _enforce_promotion_deadlines(
            utc_timestamp(clock()),
            enforce_target_deadline=enforce_target_deadline,
            target_deadline=target_deadline,
            carried_target_window_end=carried_target_window_end,
        )
        for stage, destination, backup in staged:
            _replace_staged_current_file(stage, destination)
            replaced.append((destination, backup))
            _enforce_promotion_deadlines(
                utc_timestamp(clock()),
                enforce_target_deadline=enforce_target_deadline,
                target_deadline=target_deadline,
                carried_target_window_end=carried_target_window_end,
            )
        promoted_at = utc_timestamp(clock())
        _enforce_promotion_deadlines(
            promoted_at,
            enforce_target_deadline=enforce_target_deadline,
            target_deadline=target_deadline,
            carried_target_window_end=carried_target_window_end,
        )
        _write_publication_receipt(
            publication_receipt,
            run_directory=run_directory,
            datastore_root=datastore_root,
            promoted_at=promoted_at,
            previous_publication=previous_publication,
        )
        manifest = verify_manifest(run_directory)
        receipt = read_publication_receipt(
            run_directory,
            manifest,
            datastore_root=datastore_root,
        )
        record = publication_record(
            run_directory,
            manifest,
            receipt,
            datastore_root=datastore_root,
        )
        _write_json_durable(
            pointer_stage,
            authoritative_pointer_payload(record),
        )
        _enforce_promotion_deadlines(
            utc_timestamp(clock()),
            enforce_target_deadline=enforce_target_deadline,
            target_deadline=target_deadline,
            carried_target_window_end=carried_target_window_end,
        )
        current_pointer = (
            pointer_destination.read_bytes()
            if pointer_destination.is_file()
            else None
        )
        if current_pointer != pointer_before:
            raise RuntimeError(
                "Authoritative current pointer changed during promotion; "
                "the new run was not published."
            )
        _replace_authoritative_pointer(pointer_stage, pointer_destination)
        pointer_committed = True
    except Exception as exc:
        if not pointer_committed:
            publication_receipt.unlink(missing_ok=True)
        rollback_errors: list[str] = []
        if not pointer_committed:
            for destination, backup in reversed(replaced):
                try:
                    if backup is None:
                        destination.unlink(missing_ok=True)
                    else:
                        os.replace(backup, destination)
                except OSError as rollback_exc:  # pragma: no cover
                    rollback_errors.append(
                        f"{destination}: {type(rollback_exc).__name__}: "
                        f"{rollback_exc}"
                    )
        if rollback_errors:  # pragma: no cover - catastrophic disk failure
            raise RuntimeError(
                "Compatibility-mirror promotion failed and rollback was "
                "incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        raise
    finally:
        pointer_stage.unlink(missing_ok=True)
        for stage, _destination, backup in staged:
            stage.unlink(missing_ok=True)
            if backup is not None:
                backup.unlink(missing_ok=True)


def _replace_staged_current_file(stage: Path, destination: Path) -> None:
    os.replace(stage, destination)


def _replace_authoritative_pointer(stage: Path, destination: Path) -> None:
    os.replace(stage, destination)


def _write_publication_receipt(
    path: Path,
    *,
    run_directory: Path,
    datastore_root: Path,
    promoted_at: pd.Timestamp,
    previous_publication: Mapping[str, object] | None,
) -> None:
    manifest_path = run_directory / "manifest.json"
    manifest = verify_manifest(run_directory)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload: dict[str, object] = {
        "schema_version": _PUBLICATION_RECEIPT_VERSION,
        "run_timestamp": utc_timestamp(
            manifest["run_timestamp"]
        ).isoformat(),
        "run_path": expected_run_path(
            datastore_root,
            run_directory,
        ),
        "promoted_at": promoted_at.isoformat(),
        "manifest_checksum_sha256": file_checksum(manifest_path),
        "previous_publication": (
            dict(previous_publication)
            if previous_publication is not None
            else None
        ),
    }
    try:
        _write_json_durable(temporary, payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_durable(
    path: Path,
    payload: Mapping[str, object],
) -> None:
    encoded = (
        json.dumps(
            dict(payload),
            indent=2,
            sort_keys=True,
            default=str,
        )
        + "\n"
    ).encode("utf-8")
    with Path(path).open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


def _verify_publication_receipt(
    run_directory: Path,
    manifest: Mapping[str, object],
) -> bool:
    kind = publication_contract_kind(manifest)
    if kind == "legacy":
        return True
    if kind != "receipt":
        return False
    try:
        datastore_root = Path(run_directory).resolve().parents[2]
    except IndexError:
        return False
    return verify_publication_receipt(
        run_directory,
        manifest,
        datastore_root=datastore_root,
    )


def _enforce_promotion_deadlines(
    checked_at: pd.Timestamp,
    *,
    enforce_target_deadline: bool,
    target_deadline: object | None,
    carried_target_window_end: object | None = None,
) -> None:
    if enforce_target_deadline:
        target_start = (
            utc_timestamp(target_deadline)
            if target_deadline is not None and pd.notna(target_deadline)
            else None
        )
        if target_start is None or checked_at >= target_start:
            raise RuntimeError(
                "Loop B publication deadline passed during atomic promotion; "
                "the prior current files remain unchanged."
            )
    if carried_target_window_end is not None:
        target_end = (
            utc_timestamp(carried_target_window_end)
            if pd.notna(carried_target_window_end)
            else None
        )
        if target_end is None or checked_at >= target_end:
            raise RuntimeError(
                "Loop B carried forecast target window ended during atomic "
                "promotion; the prior current files remain unchanged."
            )


def _weekly_specifications(
    specifications: Mapping[str, HorizonSpecification],
) -> dict[str, HorizonSpecification]:
    """Return configured weekly routes in their public display order."""

    return {
        horizon: specifications[horizon]
        for horizon in WEEKLY_HORIZON_ORDER
        if horizon in specifications
    }


def _verified_weekly_prediction_rows(
    runs: Sequence[VerifiedWeeklyPredictionRun],
    *,
    samples: pd.DataFrame,
    specifications: Mapping[str, HorizonSpecification],
    assumed_round_trip_cost: float,
) -> pd.DataFrame:
    """Return calendar-proven receipt-chain remaining-week origins."""

    weekly_specifications = _weekly_specifications(specifications)
    if not weekly_specifications:
        return empty_frame(PREDICTION_SCHEMA)
    bundles = _weekly_prediction_bundles(
        runs,
        samples=samples,
        specifications=weekly_specifications,
        assumed_round_trip_cost=assumed_round_trip_cost,
    )
    frames = [
        bundle
        for symbol_bundles in bundles.values()
        for _promoted_at, bundle in symbol_bundles
    ]
    if not frames:
        return empty_frame(PREDICTION_SCHEMA)
    return pd.concat(frames, ignore_index=True, sort=False).drop_duplicates(
        [
            "symbol",
            "horizon",
            "decision_timestamp",
            "prediction_created_at",
        ],
        keep="last",
    ).reset_index(drop=True)


def _weekly_live_predictions(
    samples: pd.DataFrame,
    *,
    models: Mapping[str, RuntimeModel],
    verified_runs: Sequence[VerifiedWeeklyPredictionRun],
    specifications: Mapping[str, HorizonSpecification],
    symbols: Sequence[str],
    assumed_round_trip_cost: float,
    prediction_created_at: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Issue the latest remaining-week outlook or reuse that exact decision."""

    weekly_specifications = _weekly_specifications(specifications)
    if not weekly_specifications:
        empty = empty_frame(PREDICTION_SCHEMA)
        return empty, empty.copy()
    candidates = _weekly_issuance_candidates(
        samples,
        specifications=weekly_specifications,
        symbols=symbols,
        as_of=prediction_created_at,
        assumed_round_trip_cost=assumed_round_trip_cost,
    )
    verified = _weekly_prediction_bundles(
        verified_runs,
        samples=samples,
        specifications=weekly_specifications,
        assumed_round_trip_cost=assumed_round_trip_cost,
    )
    selected: list[pd.DataFrame] = []
    newly_issued: list[pd.DataFrame] = []
    for symbol in symbols:
        symbol_name = str(symbol).strip().upper()
        candidate = candidates.get(symbol_name)
        prior_bundles = verified.get(symbol_name, ())
        if candidate is not None:
            matching = next(
                (
                    bundle
                    for _promoted_at, bundle in prior_bundles
                    if _weekly_prediction_matches_samples(bundle, candidate)
                ),
                None,
            )
            if matching is not None:
                selected.append(matching.copy())
                continue
            candidate_horizons = tuple(
                horizon
                for horizon in WEEKLY_HORIZON_ORDER
                if horizon in set(candidate["horizon"].astype(str))
            )
            if all(horizon in models for horizon in candidate_horizons):
                issued_frames = [
                    _prediction_frame(
                        models[horizon],
                        candidate.loc[
                            candidate["horizon"].eq(horizon)
                        ].copy(),
                        prediction_created_at=prediction_created_at,
                        mode="LIVE",
                    )
                    for horizon in candidate_horizons
                ]
                issued = pd.concat(
                    issued_frames,
                    ignore_index=True,
                    sort=False,
                )
                _validate_weekly_prediction_bundle(
                    issued,
                    specifications=weekly_specifications,
                    assumed_round_trip_cost=assumed_round_trip_cost,
                )
                selected.append(issued)
                newly_issued.append(issued)
                continue

        active = next(
            (
                bundle
                for _promoted_at, bundle in prior_bundles
                if _weekly_prediction_bundle_is_active(
                    bundle,
                    samples=samples,
                    as_of=prediction_created_at,
                    specifications=weekly_specifications,
                    assumed_round_trip_cost=assumed_round_trip_cost,
                )
            ),
            None,
        )
        if active is not None:
            selected.append(active.copy())

    output = (
        pd.concat(selected, ignore_index=True, sort=False)
        if selected
        else empty_frame(PREDICTION_SCHEMA)
    )
    fresh = (
        pd.concat(newly_issued, ignore_index=True, sort=False)
        if newly_issued
        else empty_frame(PREDICTION_SCHEMA)
    )
    return (
        _project(output, PREDICTION_SCHEMA.names),
        _project(fresh, PREDICTION_SCHEMA.names),
    )


def _weekly_issuance_candidates(
    samples: pd.DataFrame,
    *,
    specifications: Mapping[str, HorizonSpecification],
    symbols: Sequence[str],
    as_of: pd.Timestamp,
    assumed_round_trip_cost: float,
) -> dict[str, pd.DataFrame]:
    weekly_specifications = _weekly_specifications(specifications)
    if "1w" not in weekly_specifications:
        return {}
    weekly = samples.loc[
        samples["horizon"].isin(weekly_specifications)
    ].copy()
    if weekly.empty:
        return {}
    for column in (
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "actionable_until",
    ):
        weekly[column] = pd.to_datetime(
            weekly[column], utc=True, errors="coerce"
        )
    weekly["exchange_session"] = pd.to_datetime(
        weekly["exchange_session"], errors="coerce"
    ).dt.tz_localize(None).dt.normalize()
    timestamp_columns = [
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "actionable_until",
        "exchange_session",
    ]
    weekly = weekly.dropna(subset=timestamp_columns)
    if weekly.empty:
        return {}

    result: dict[str, pd.DataFrame] = {}
    for raw_symbol in symbols:
        symbol = str(raw_symbol).strip().upper()
        symbol_rows = weekly.loc[weekly["symbol"].eq(symbol)].copy()
        decisions = (
            symbol_rows.loc[
                symbol_rows["information_available_at"].le(as_of),
                "decision_timestamp",
            ]
            .drop_duplicates()
            .sort_values(ascending=False)
        )
        for decision in decisions:
            bundle = symbol_rows.loc[
                symbol_rows["decision_timestamp"].eq(decision)
            ].copy()
            bundle_horizons = set(bundle["horizon"].astype(str))
            if (
                "1w" not in bundle_horizons
                or bundle["horizon"].duplicated().any()
            ):
                continue
            try:
                calendar_name = _one_text_value(
                    bundle["exchange_calendar"],
                    label=f"{symbol} weekly exchange calendar",
                )
                session_values = pd.to_datetime(
                    bundle["exchange_session"], errors="coerce"
                ).dt.tz_localize(None).dt.normalize()
                if session_values.isna().any() or session_values.nunique() != 1:
                    continue
                session = pd.Timestamp(session_values.iloc[0])
                calendar = ExchangeSessionCalendar(
                    calendar_name,
                    start=session - pd.Timedelta(days=14),
                    end=session + pd.Timedelta(days=45),
                )

                remaining = _remaining_week_candidate(
                    bundle,
                    calendar=calendar,
                    as_of=as_of,
                    symbol=symbol,
                )
                if remaining.empty:
                    continue
                _validate_weekly_sample_bundle(
                    remaining,
                    specifications=weekly_specifications,
                    assumed_round_trip_cost=assumed_round_trip_cost,
                )
            except (KeyError, RuntimeError, ValueError):
                # A malformed or obsolete decision is not a global runtime
                # failure. Try the symbol's next-newest usable decision.
                continue
            result[symbol] = _sort_weekly_rows(remaining)
            break
    return result


def _remaining_week_candidate(
    bundle: pd.DataFrame,
    *,
    calendar: ExchangeSessionCalendar,
    as_of: pd.Timestamp,
    symbol: str,
) -> pd.DataFrame:
    components = bundle.loc[
        bundle["horizon"].isin(WEEKLY_HORIZON_ORDER[1:])
        & ~bundle["label_status"].eq("COMPLETE")
        & bundle["information_available_at"].le(as_of)
        & bundle["actionable_until"].gt(as_of)
    ].copy()
    if components.empty:
        return components

    week_keys: list[pd.Timestamp] = []
    for row in components.itertuples(index=False):
        session = _weekly_target_session(
            calendar,
            start=row.target_window_start,
            end=row.target_window_end,
            label=f"{symbol} {row.horizon}",
        )
        week_keys.append(_exchange_week_key(session))
    components["__exchange_week"] = week_keys
    first_week = components.sort_values("target_window_start").iloc[0][
        "__exchange_week"
    ]
    components = components.loc[
        components["__exchange_week"].eq(first_week)
    ].drop(columns="__exchange_week")
    aggregate = bundle.loc[
        bundle["horizon"].eq("1w")
        & ~bundle["label_status"].eq("COMPLETE")
        & bundle["information_available_at"].le(as_of)
        & bundle["actionable_until"].gt(as_of)
    ].copy()
    if len(aggregate) != 1:
        return bundle.iloc[0:0].copy()
    return pd.concat([aggregate, components], ignore_index=True, sort=False)


def _weekly_prediction_bundles(
    runs: Sequence[VerifiedWeeklyPredictionRun],
    *,
    samples: pd.DataFrame,
    specifications: Mapping[str, HorizonSpecification],
    assumed_round_trip_cost: float,
) -> dict[str, tuple[tuple[pd.Timestamp, pd.DataFrame], ...]]:
    grouped: dict[str, list[tuple[pd.Timestamp, pd.DataFrame]]] = {}
    for run in sorted(runs, key=lambda item: item.promoted_at, reverse=True):
        frame = run.predictions.copy()
        if frame.empty:
            continue
        for (_symbol, _decision, _created), candidate in frame.groupby(
            ["symbol", "decision_timestamp", "prediction_created_at"],
            sort=False,
            dropna=False,
        ):
            try:
                horizons = _weekly_bundle_horizons(candidate)
                if any(horizon not in specifications for horizon in horizons):
                    continue
                _validate_weekly_prediction_bundle(
                    candidate,
                    specifications=specifications,
                    assumed_round_trip_cost=assumed_round_trip_cost,
                )
            except (RuntimeError, ValueError):
                continue
            matching_samples = _matching_weekly_sample_bundle(
                candidate,
                samples,
                specifications=specifications,
                assumed_round_trip_cost=assumed_round_trip_cost,
            )
            if matching_samples is None:
                continue
            deadline = pd.to_datetime(
                candidate["actionable_until"], utc=True, errors="coerce"
            ).min()
            if pd.isna(deadline) or run.promoted_at >= deadline:
                continue
            symbol = str(candidate["symbol"].iloc[0]).strip().upper()
            grouped.setdefault(symbol, []).append(
                (run.promoted_at, _sort_weekly_rows(candidate))
            )
    return _deduplicate_weekly_prediction_bundles(grouped)


def _matching_weekly_sample_bundle(
    predictions: pd.DataFrame,
    samples: pd.DataFrame,
    *,
    specifications: Mapping[str, HorizonSpecification],
    assumed_round_trip_cost: float,
) -> pd.DataFrame | None:
    required = {
        "symbol",
        "exchange_calendar",
        "exchange_session",
        "horizon",
        "decision_timestamp",
    }
    if predictions.empty or not required.issubset(samples.columns):
        return None
    symbols = predictions["symbol"].astype("string").str.strip().str.upper()
    decisions = pd.to_datetime(
        predictions["decision_timestamp"], utc=True, errors="coerce"
    )
    if symbols.isna().any() or symbols.nunique() != 1:
        return None
    if decisions.isna().any() or decisions.nunique() != 1:
        return None
    sample_decisions = pd.to_datetime(
        samples["decision_timestamp"], utc=True, errors="coerce"
    )
    try:
        horizons = _weekly_bundle_horizons(predictions)
    except RuntimeError:
        return None
    candidate = samples.loc[
        samples["horizon"].isin(horizons)
        & samples["symbol"].astype("string").str.strip().str.upper().eq(
            str(symbols.iloc[0])
        )
        & sample_decisions.eq(decisions.iloc[0])
    ].copy()
    if len(candidate) != len(horizons):
        return None
    try:
        _validate_weekly_sample_bundle(
            candidate,
            specifications=specifications,
            assumed_round_trip_cost=assumed_round_trip_cost,
        )
    except (RuntimeError, ValueError):
        return None
    if not _weekly_prediction_matches_samples(predictions, candidate):
        return None
    return _sort_weekly_rows(candidate)


def _deduplicate_weekly_prediction_bundles(
    grouped: Mapping[str, Sequence[tuple[pd.Timestamp, pd.DataFrame]]],
) -> dict[str, tuple[tuple[pd.Timestamp, pd.DataFrame], ...]]:
    output: dict[str, tuple[tuple[pd.Timestamp, pd.DataFrame], ...]] = {}
    for symbol, items in grouped.items():
        by_decision: dict[
            pd.Timestamp,
            tuple[pd.Timestamp, pd.DataFrame],
        ] = {}
        for promoted_at, bundle in items:
            decision = pd.to_datetime(
                bundle["decision_timestamp"], utc=True, errors="coerce"
            ).iloc[0]
            existing = by_decision.get(decision)
            if existing is None or promoted_at < existing[0]:
                by_decision[decision] = (
                    promoted_at,
                    _sort_weekly_rows(bundle),
                )
        output[symbol] = tuple(
            (promoted_at, bundle)
            for promoted_at, bundle in sorted(
                by_decision.values(),
                key=lambda item: item[0],
                reverse=True,
            )
        )
    return output


def _weekly_bundle_horizons(frame: pd.DataFrame) -> tuple[str, ...]:
    if frame.empty or "horizon" not in frame.columns:
        raise RuntimeError("Weekly outlook contains no routes")
    horizons = tuple(frame["horizon"].astype(str))
    if len(set(horizons)) != len(horizons):
        raise RuntimeError("Weekly outlook contains duplicate routes")
    if "1w" not in horizons:
        raise RuntimeError("Remaining-week outlook requires its aggregate route")
    component_order = WEEKLY_HORIZON_ORDER[1:]
    ordered = tuple(horizon for horizon in component_order if horizon in horizons)
    if not ordered or len(ordered) + 1 != len(horizons):
        raise RuntimeError("Weekly outlook contains unsupported component routes")
    positions = tuple(component_order.index(horizon) for horizon in ordered)
    if positions != tuple(range(0, positions[-1] + 1)):
        raise RuntimeError(
            "Remaining-week component routes must be a Day 1 prefix"
        )
    return ("1w", *ordered)


def _exchange_week_key(session: object) -> pd.Timestamp:
    label = pd.Timestamp(session)
    if label.tzinfo is not None:
        label = label.tz_convert("UTC").tz_localize(None)
    label = label.normalize()
    return (label - pd.Timedelta(days=int(label.weekday()))).normalize()


def _weekly_target_session(
    calendar: ExchangeSessionCalendar,
    *,
    start: object,
    end: object,
    label: str,
) -> pd.Timestamp:
    target_start = utc_timestamp(start)
    target_end = utc_timestamp(end)
    matches = [
        pd.Timestamp(session)
        for session in calendar.sessions
        if calendar.session_open(session) == target_start
        and calendar.session_close(session) == target_end
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"{label} target window does not resolve to one exchange session"
        )
    return matches[0]


def _validate_weekly_sample_bundle(
    frame: pd.DataFrame,
    *,
    specifications: Mapping[str, HorizonSpecification],
    assumed_round_trip_cost: float,
    validate_calendar: bool = True,
) -> None:
    _validate_weekly_bundle_geometry(frame)
    if validate_calendar:
        _validate_weekly_sample_calendar(
            frame,
            specifications=specifications,
        )
    expected_specifications = {
        horizon: _canonical_target_specification(specification)
        for horizon, specification in specifications.items()
    }
    cost = pd.to_numeric(frame["assumed_round_trip_cost"], errors="coerce")
    if not np.isclose(
        cost,
        float(assumed_round_trip_cost),
        rtol=0.0,
        atol=1e-12,
        equal_nan=False,
    ).all():
        raise RuntimeError("Weekly bundle round-trip cost is incompatible")
    for row in frame.itertuples(index=False):
        specification = specifications[str(row.horizon)]
        if row.target_definition_version != specification.target_definition_version:
            raise RuntimeError("Weekly bundle target version is incompatible")
        if row.target_specification != expected_specifications[str(row.horizon)]:
            raise RuntimeError("Weekly bundle target specification is incompatible")


def _validate_weekly_sample_calendar(
    frame: pd.DataFrame,
    *,
    specifications: Mapping[str, HorizonSpecification],
) -> None:
    required = {"exchange_calendar", "exchange_session"}
    if not required.issubset(frame.columns):
        raise RuntimeError(
            "Weekly bundle cannot prove its exchange calendar and decision session"
        )
    calendar_name = _one_text_value(
        frame["exchange_calendar"],
        label="weekly exchange calendar",
    )
    decision_sessions = pd.to_datetime(
        frame["exchange_session"], utc=True, errors="coerce"
    ).dt.tz_localize(None).dt.normalize()
    if decision_sessions.isna().any() or decision_sessions.nunique() != 1:
        raise RuntimeError("Weekly bundle must share one exchange session")
    decision_session = pd.Timestamp(decision_sessions.iloc[0])
    calendar = ExchangeSessionCalendar(
        calendar_name,
        start=decision_session - pd.Timedelta(days=14),
        end=decision_session + pd.Timedelta(days=45),
    )
    horizons = _weekly_bundle_horizons(frame)
    first_specification = specifications[horizons[0]]
    expected_information = (
        calendar.session_close(decision_session)
        + first_specification.processing_delay
    )
    information = pd.to_datetime(
        frame["information_available_at"], utc=True, errors="coerce"
    )
    decisions = pd.to_datetime(
        frame["decision_timestamp"], utc=True, errors="coerce"
    )
    if not information.eq(expected_information).all():
        raise RuntimeError(
            "Weekly bundle information time is not the decision session close "
            "plus processing delay"
        )
    if not decisions.eq(expected_information).all():
        raise RuntimeError(
            "Weekly bundle decision time is not the decision session close plus "
            "processing delay"
        )
    resolved = calendar.horizon(
        decision_session=decision_session,
        decision_timestamp=expected_information,
        future_session_count=5,
    )
    ordered = frame.set_index("horizon").loc[list(horizons)]
    starts = pd.to_datetime(ordered["target_window_start"], utc=True)
    ends = pd.to_datetime(ordered["target_window_end"], utc=True)
    expected_starts = tuple(
        calendar.session_open(session) for session in resolved.future_sessions
    )
    expected_ends = tuple(
        calendar.session_close(session) for session in resolved.future_sessions
    )
    component_sessions: list[pd.Timestamp] = []
    for horizon in horizons[1:]:
        lead = int(horizon.removeprefix("1w-d"))
        if (
            starts.loc[horizon] != expected_starts[lead - 1]
            or ends.loc[horizon] != expected_ends[lead - 1]
        ):
            raise RuntimeError(
                f"{horizon} is not the official open-to-close window for "
                "the required consecutive eligible session"
            )
        component_sessions.append(pd.Timestamp(resolved.future_sessions[lead - 1]))
    if len(
        {_exchange_week_key(session) for session in component_sessions}
    ) != 1:
        raise RuntimeError(
            "Remaining-week components must belong to one exchange week"
        )
    final_lead = int(horizons[-1].removeprefix("1w-d"))
    if (
        starts.loc["1w"] != expected_starts[0]
        or ends.loc["1w"] != expected_ends[final_lead - 1]
    ):
        raise RuntimeError(
            "Weekly aggregate does not span the remaining exchange-week sessions"
        )


def _validate_weekly_prediction_bundle(
    frame: pd.DataFrame,
    *,
    specifications: Mapping[str, HorizonSpecification],
    assumed_round_trip_cost: float,
) -> None:
    _validate_weekly_sample_bundle(
        frame,
        specifications=specifications,
        assumed_round_trip_cost=assumed_round_trip_cost,
        validate_calendar=False,
    )
    if not (
        frame["prediction_mode"].eq("LIVE").all()
        and frame["prediction_status"].eq("CREATED").all()
    ):
        raise RuntimeError("Weekly bundle is not a created LIVE prediction")
    created = pd.to_datetime(
        frame["prediction_created_at"], utc=True, errors="coerce"
    )
    deadlines = pd.to_datetime(
        frame["actionable_until"], utc=True, errors="coerce"
    )
    if created.isna().any() or created.nunique() != 1:
        raise RuntimeError("Weekly bundle must share one issuance timestamp")
    if not created.lt(deadlines).all():
        raise RuntimeError(
            "Weekly outlook was created at or after one of its route deadlines"
        )
    if frame[["model_name", "model_version"]].isna().any().any():
        raise RuntimeError("Weekly bundle must preserve every model version")
    probability = pd.to_numeric(
        frame["calibrated_probability"], errors="coerce"
    )
    if not probability.between(0.0, 1.0, inclusive="both").all():
        raise RuntimeError("Weekly bundle contains an invalid probability")


def _validate_weekly_bundle_geometry(frame: pd.DataFrame) -> None:
    horizons = _weekly_bundle_horizons(frame)
    for column in (
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "actionable_until",
    ):
        values = pd.to_datetime(frame[column], utc=True, errors="coerce")
        if values.isna().any():
            raise RuntimeError(f"Weekly bundle contains invalid {column}")
        if (
            column in {"decision_timestamp", "information_available_at"}
            and values.nunique() != 1
        ):
            raise RuntimeError(f"Weekly bundle must share one {column}")
    ordered = frame.set_index("horizon").loc[list(horizons)]
    starts = pd.to_datetime(ordered["target_window_start"], utc=True)
    ends = pd.to_datetime(ordered["target_window_end"], utc=True)
    deadlines = pd.to_datetime(ordered["actionable_until"], utc=True)
    if not starts.lt(ends).all():
        raise RuntimeError("Weekly target windows must have positive duration")
    component_horizons = horizons[1:]
    component_starts = starts.loc[list(component_horizons)]
    component_ends = ends.loc[list(component_horizons)]
    component_deadlines = deadlines.loc[list(component_horizons)]
    if starts.loc["1w"] != component_starts.iloc[0]:
        raise RuntimeError("Weekly aggregate must start at Day 1 open")
    if ends.loc["1w"] != component_ends.iloc[-1]:
        raise RuntimeError(
            "Weekly aggregate must end at the final remaining session close"
        )
    if deadlines.loc["1w"] != component_ends.iloc[0]:
        raise RuntimeError(
            "Weekly aggregate must expire at the first remaining session close"
        )
    if not component_deadlines.eq(component_ends).all():
        raise RuntimeError(
            "Every weekly component must use its own session close as its deadline"
        )
    if (
        not component_starts.is_monotonic_increasing
        or component_starts.duplicated().any()
    ):
        raise RuntimeError(
            "Day 1 through Day 5 target sessions must be strictly ordered"
        )
    if any(
        previous >= current
        for previous, current in zip(
            component_ends.iloc[:-1], component_starts.iloc[1:], strict=True
        )
    ):
        raise RuntimeError("Weekly component target sessions overlap")


def _weekly_prediction_matches_samples(
    predictions: pd.DataFrame,
    samples: pd.DataFrame,
) -> bool:
    prediction_rows = predictions.set_index("horizon")
    sample_rows = samples.set_index("horizon")
    if set(prediction_rows.index) != set(sample_rows.index):
        return False
    try:
        horizons = _weekly_bundle_horizons(predictions)
    except RuntimeError:
        return False
    for column in (
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "actionable_until",
        "target_definition_version",
        "target_specification",
        "assumed_round_trip_cost",
    ):
        left = prediction_rows.loc[list(horizons), column]
        right = sample_rows.loc[list(horizons), column]
        if column.endswith("timestamp") or column in {
            "target_window_start",
            "target_window_end",
            "actionable_until",
        }:
            left = pd.to_datetime(left, utc=True, errors="coerce")
            right = pd.to_datetime(right, utc=True, errors="coerce")
        if column == "assumed_round_trip_cost":
            if not np.isclose(
                pd.to_numeric(left, errors="coerce"),
                pd.to_numeric(right, errors="coerce"),
                rtol=0.0,
                atol=1e-12,
                equal_nan=False,
            ).all():
                return False
        else:
            left_values = left.reset_index(drop=True)
            right_values = right.reset_index(drop=True)
            if left_values.isna().any() or right_values.isna().any():
                return False
            if not left_values.eq(right_values).all():
                return False
    return True


def _weekly_prediction_bundle_is_active(
    bundle: pd.DataFrame,
    *,
    samples: pd.DataFrame,
    as_of: pd.Timestamp,
    specifications: Mapping[str, HorizonSpecification],
    assumed_round_trip_cost: float,
) -> bool:
    matching_samples = _matching_weekly_sample_bundle(
        bundle,
        samples,
        specifications=specifications,
        assumed_round_trip_cost=assumed_round_trip_cost,
    )
    if matching_samples is None:
        return False
    try:
        horizons = _weekly_bundle_horizons(bundle)
    except RuntimeError:
        return False
    final_horizon = horizons[-1]
    final_rows = bundle.loc[bundle["horizon"].eq(final_horizon)]
    if len(final_rows) != 1:
        return False
    final_row = final_rows.iloc[0]
    final_start = pd.Timestamp(final_row["target_window_start"])
    final_end = pd.Timestamp(final_row["target_window_end"])
    symbol = str(bundle["symbol"].iloc[0]).strip().upper()
    calendars = matching_samples["exchange_calendar"]
    calendar_name = _one_text_value(
        calendars,
        label=f"{symbol} weekly exchange calendar",
    )
    calendar = ExchangeSessionCalendar(
        calendar_name,
        start=final_start - pd.Timedelta(days=14),
        end=final_end + pd.Timedelta(days=45),
    )
    final_session = _weekly_target_session(
        calendar,
        start=final_start,
        end=final_end,
        label=f"{symbol} final weekly component",
    )
    location = calendar.sessions.get_loc(final_session)
    if not isinstance(location, (int, np.integer)) or location + 1 >= len(
        calendar.sessions
    ):
        raise RuntimeError(
            f"{symbol} final weekly component has no following exchange session"
        )
    next_open = calendar.session_open(calendar.sessions[int(location) + 1])
    return utc_timestamp(as_of) < next_open


def _require_weekly_live_predictions(
    predictions: pd.DataFrame,
    *,
    symbols: Sequence[str],
    specifications: Mapping[str, HorizonSpecification],
) -> None:
    weekly_specifications = _weekly_specifications(specifications)
    live = predictions.loc[
        predictions["prediction_mode"].eq("LIVE")
        & predictions["horizon"].isin(WEEKLY_HORIZON_ORDER)
    ].copy()
    expected_symbols = {str(symbol).strip().upper() for symbol in symbols}
    if set(live["symbol"].astype(str).str.upper()) != expected_symbols:
        raise RuntimeError(
            "Weekly publication requires one LIVE snapshot for every symbol"
        )
    for symbol in sorted(expected_symbols):
        bundle = live.loc[live["symbol"].astype(str).str.upper().eq(symbol)]
        _validate_weekly_prediction_bundle(
            bundle,
            specifications=weekly_specifications,
            assumed_round_trip_cost=float(
                pd.to_numeric(
                    bundle["assumed_round_trip_cost"], errors="coerce"
                ).iloc[0]
            ),
        )


def _sort_weekly_rows(frame: pd.DataFrame) -> pd.DataFrame:
    order = {horizon: index for index, horizon in enumerate(WEEKLY_HORIZON_ORDER)}
    output = frame.copy()
    output["__weekly_order"] = output["horizon"].map(order)
    return output.sort_values("__weekly_order", kind="mergesort").drop(
        columns="__weekly_order"
    ).reset_index(drop=True)


def _one_text_value(values: pd.Series, *, label: str) -> str:
    normalized = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in values.dropna()
            if str(value).strip()
        )
    )
    if len(normalized) != 1:
        raise RuntimeError(f"{label} must contain exactly one value")
    return normalized[0]


def _feature_columns(
    specifications: Mapping[str, HorizonSpecification],
) -> tuple[str, ...]:
    columns: list[str] = []
    for horizon, specification in specifications.items():
        for name in DEFAULT_FEATURE_REGISTRY.feature_set(
            specification.feature_set,
            require_active=True,
            horizon=feature_contract_horizon(horizon),
        ).names:
            if name not in columns:
                columns.append(name)
    return tuple(columns)


def _horizon_source_files(
    materialization: RollingMaterialization,
    horizon: str,
) -> tuple[Path, ...]:
    """Inventory only files that can alter this horizon's fitted model."""

    return tuple(
        dict.fromkeys(
            path
            for route in materialization.routes
            if route.horizon == horizon
            for path in route.source_files
        )
    )


def _pricing_evidence_manifest(
    materialization: RollingMaterialization,
    *,
    feature_columns: Sequence[str],
    model_admission_by_horizon: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Summarize Pricing missingness and provenance in the control plane."""

    model_columns = tuple(
        column for column in feature_columns if str(column).startswith("opx__")
    )
    if not model_columns:
        return {
            "enabled": False,
            "policy_version": OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION,
            "downstream_training_eligible": False,
            "model_admission_by_horizon": dict(
                model_admission_by_horizon or {}
            ),
            "routes": {},
        }
    routes: dict[str, object] = {}
    audit_names = (
        "opx__source_status",
        "opx__source_detail",
        "opx__source_publication_version",
        "opx__source_surface_version",
        "opx__source_policy_version",
        "opx__source_target_snapshot_for",
        "opx__source_original_available_at",
        "opx__normalization_policy",
        "opx__legacy_normalized",
        "opx__authority_published_at",
        "opx__authority_run_path",
    )
    for route in materialization.routes:
        frame = route.samples
        route_key = f"{route.symbol}|{route.horizon}"
        joined = (
            frame["opx__join_status"].astype("string").value_counts(dropna=False)
            if "opx__join_status" in frame
            else pd.Series(dtype="int64")
        )
        available_model_columns = [
            column for column in model_columns if column in frame.columns
        ]
        missing_rows = (
            int(frame.loc[:, available_model_columns].isna().all(axis=1).sum())
            if available_model_columns
            else len(frame)
        )
        audit: dict[str, object] = {}
        for name in audit_names:
            if name not in frame:
                continue
            values = tuple(
                dict.fromkeys(
                    str(value)
                    for value in frame[name].dropna()
                    if str(value).strip()
                )
            )
            audit[name] = list(values)
        routes[route_key] = {
            "route_status": route.status,
            "sample_rows": len(frame),
            "all_pricing_values_missing_rows": missing_rows,
            "join_status_counts": {
                str(key): int(value) for key, value in joined.items()
            },
            "audit": audit,
            "family_gate": _pricing_route_family_gate(
                frame,
                feature_columns=model_columns,
            ),
        }
    gate = _pricing_family_gate(
        materialization.samples,
        feature_columns=model_columns,
    )
    return {
        **gate,
        "model_admission_by_horizon": dict(
            model_admission_by_horizon or {}
        ),
        "routes": routes,
    }


def _specification_for_pricing_gate(
    specification: HorizonSpecification,
    *,
    gate: Mapping[str, object],
) -> HorizonSpecification:
    """Select the enriched contract only after its Pricing gate passes."""

    if not bool(gate.get("enabled")) or bool(
        gate.get("downstream_training_eligible")
    ):
        return specification
    try:
        baseline_name = _OPTION_PRICING_BASELINE_FEATURE_SETS[
            specification.feature_set
        ]
    except KeyError as exc:
        raise RuntimeError(
            "No baseline feature contract is registered for gated Option "
            f"Pricing feature set {specification.feature_set!r}"
        ) from exc
    horizon = feature_contract_horizon(specification.horizon)
    requested = DEFAULT_FEATURE_REGISTRY.feature_set(
        specification.feature_set,
        require_active=True,
        horizon=horizon,
    )
    baseline = DEFAULT_FEATURE_REGISTRY.feature_set(
        baseline_name,
        require_active=True,
        horizon=horizon,
    )
    expected = tuple(
        feature.name
        for feature in requested.features
        if feature.source_family != "opx"
    )
    if baseline.names != expected:
        raise RuntimeError(
            "Option Pricing baseline contract does not exactly preserve the "
            f"non-Pricing features for {specification.feature_set!r}"
        )
    return replace(specification, feature_set=baseline.name)


def _pricing_family_gate(
    frame: pd.DataFrame,
    *,
    feature_columns: Sequence[str],
) -> dict[str, object]:
    """Fail closed until every selected Pricing subfamily is fresh and covered."""

    selected = tuple(
        str(column)
        for column in feature_columns
        if str(column).startswith("opx__")
    )
    if not selected:
        return {
            "enabled": False,
            "policy_version": OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION,
            "downstream_training_eligible": False,
            "failed_routes": [],
            "thresholds": {
                "minimum_complete_row_fraction": (
                    OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE
                ),
                "minimum_fresh_joined_row_fraction": (
                    OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE
                ),
                "minimum_distinct_surface_targets": (
                    OPTION_PRICING_LOOP_B_MINIMUM_DISTINCT_TARGETS
                ),
            },
            "route_gates": {},
        }
    route_gates: dict[str, object] = {}
    if frame.empty or not {"symbol", "horizon"}.issubset(frame.columns):
        route_gates["<no-route>"] = _pricing_route_family_gate(
            frame,
            feature_columns=selected,
        )
    else:
        for (symbol, horizon), route in frame.groupby(
            ["symbol", "horizon"], sort=True, dropna=False
        ):
            route_gates[f"{symbol}|{horizon}"] = _pricing_route_family_gate(
                route,
                feature_columns=selected,
            )
    failed = [
        route
        for route, report in route_gates.items()
        if not bool(report.get("pass"))
    ]
    return {
        "enabled": True,
        "policy_version": OPTION_PRICING_LOOP_B_GATE_POLICY_VERSION,
        "downstream_training_eligible": bool(route_gates) and not failed,
        "failed_routes": failed,
        "thresholds": {
            "minimum_complete_row_fraction": (
                OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE
            ),
            "minimum_fresh_joined_row_fraction": (
                OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE
            ),
            "minimum_distinct_surface_targets": (
                OPTION_PRICING_LOOP_B_MINIMUM_DISTINCT_TARGETS
            ),
        },
        "route_gates": route_gates,
    }


def _pricing_route_family_gate(
    frame: pd.DataFrame,
    *,
    feature_columns: Sequence[str],
) -> dict[str, object]:
    row_count = len(frame)
    joined = (
        frame["opx__join_status"].astype("string").eq("JOINED")
        if "opx__join_status" in frame
        else pd.Series(False, index=frame.index, dtype=bool)
    )
    target_column = (
        "opx__source_target_snapshot_for"
        if "opx__source_target_snapshot_for" in frame
        else "decision_timestamp"
        if "decision_timestamp" in frame
        else None
    )
    targets = (
        pd.to_datetime(frame[target_column], utc=True, errors="coerce")
        if target_column is not None
        else pd.Series(pd.NaT, index=frame.index)
    )
    groups: dict[str, object] = {}
    for name, declared in _OPTION_PRICING_FEATURE_GROUPS.items():
        columns = tuple(
            column
            for column in declared
            if column in feature_columns and column in frame.columns
        )
        if not columns:
            continue
        complete = frame.loc[:, columns].notna().all(axis=1)
        fresh_complete = complete & joined
        distinct_targets = int(targets.loc[fresh_complete].nunique())
        complete_fraction = float(complete.mean()) if row_count else 0.0
        fresh_fraction = float(fresh_complete.mean()) if row_count else 0.0
        passed = bool(
            complete_fraction >= OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE
            and fresh_fraction >= OPTION_PRICING_LOOP_B_MINIMUM_COVERAGE
            and distinct_targets
            >= OPTION_PRICING_LOOP_B_MINIMUM_DISTINCT_TARGETS
        )
        groups[name] = {
            "columns": list(columns),
            "complete_rows": int(complete.sum()),
            "fresh_joined_rows": int(fresh_complete.sum()),
            "sample_rows": row_count,
            "complete_row_fraction": complete_fraction,
            "fresh_joined_row_fraction": fresh_fraction,
            "distinct_surface_targets": distinct_targets,
            "pass": passed,
        }
    return {
        "pass": bool(groups) and all(
            bool(report.get("pass")) for report in groups.values()
        ),
        "groups": groups,
    }


def _project_samples(
    frame: pd.DataFrame,
    *,
    schema_names: Sequence[str],
) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=schema_names)
    return frame.loc[
        :, [name for name in schema_names if name in frame.columns]
    ].copy()


def _closed_lockbox_view(
    samples: pd.DataFrame,
    *,
    partitions_by_horizon: Mapping[str, ModelPartitions],
) -> pd.DataFrame:
    """Remove exact closed-lockbox clusters from published sample artifacts."""

    output = samples.copy()
    if output.empty:
        return output
    target_starts = pd.to_datetime(
        output["target_window_start"],
        utc=True,
        errors="coerce",
    )
    closed_rows = pd.Series(False, index=output.index, dtype=bool)
    for horizon, partitions in partitions_by_horizon.items():
        closed_rows |= (
            output["horizon"].eq(horizon)
            & target_starts.isin(partitions.lockbox_cluster_values)
        )
    return output.loc[~closed_rows].reset_index(drop=True)


def _project(frame: pd.DataFrame, names: Sequence[str]) -> pd.DataFrame:
    if frame.empty:
        return frame.reindex(columns=names)
    return frame.loc[:, [name for name in names if name in frame.columns]].copy()


def _strategy_output_frame(
    frame: pd.DataFrame,
    *,
    schema: object,
    key_columns: Sequence[str],
) -> pd.DataFrame:
    names = tuple(getattr(schema, "names"))
    if frame.empty:
        return empty_frame(schema)  # type: ignore[arg-type]
    identified = frame_with_readable_id(frame, key_columns=key_columns)
    return _project(identified, names)


def _live_candidates(
    samples: pd.DataFrame,
    *,
    as_of: pd.Timestamp,
    latest_per_symbol: bool,
) -> pd.DataFrame:
    decision = pd.to_datetime(
        samples["information_available_at"], utc=True, errors="coerce"
    )
    deadline = pd.to_datetime(
        samples["target_window_start"], utc=True, errors="coerce"
    )
    candidates = samples.loc[
        ~samples["label_status"].eq("COMPLETE")
        & decision.le(as_of)
        & deadline.gt(as_of)
    ].copy()
    if candidates.empty or not latest_per_symbol:
        return candidates
    return (
        candidates.sort_values(
            ["symbol", "information_available_at", "decision_timestamp"],
            kind="mergesort",
        )
        .groupby("symbol", sort=False, as_index=False)
        .tail(1)
        .reset_index(drop=True)
    )


def _prediction_frame(
    model: RuntimeModel,
    rows: pd.DataFrame,
    *,
    prediction_created_at: pd.Timestamp,
    mode: str,
) -> pd.DataFrame:
    if rows.empty:
        return empty_frame(PREDICTION_SCHEMA)
    rows = rows.copy()
    for column in ("target_definition_version", "target_specification"):
        if column not in rows:
            rows[column] = pd.NA
    raw, calibrated = model.probabilities(rows)
    output = rows.loc[
        :,
        [
            "symbol",
            "provider",
            "horizon",
            "decision_timestamp",
            "information_available_at",
            "target_window_start",
            "target_window_end",
            "actionable_until",
            "target_definition_version",
            "target_specification",
            "assumed_round_trip_cost",
        ],
    ].copy()
    output["prediction_created_at"] = prediction_created_at
    output["model_name"] = model.model_name
    output["model_version"] = model.artifact_directory.name
    output["calibration_method"] = model.calibration_method
    output["prediction_mode"] = mode
    output["prediction_status"] = "CREATED"
    output["raw_probability"] = raw
    output["calibrated_probability"] = calibrated
    return frame_with_readable_id(
        output,
        key_columns=(
            "symbol",
            "horizon",
            "decision_timestamp",
            "prediction_created_at",
        ),
    )


def _load_verified_weekly_prediction_runs(
    datastore_root: Path,
    *,
    current_run: Path,
    as_of: pd.Timestamp,
    specifications: Mapping[str, HorizonSpecification],
    assumed_round_trip_cost: float,
) -> tuple[VerifiedWeeklyPredictionRun, ...]:
    """Read receipt-chain runs that prove a coherent remaining-week issuance."""

    weekly_specifications = _weekly_specifications(specifications)
    if not weekly_specifications:
        return ()
    promoted_runs = authoritative_receipt_runs(datastore_root)
    results: list[VerifiedWeeklyPredictionRun] = []
    for run_directory, promoted_at in sorted(
        promoted_runs.items(),
        key=lambda item: item[1],
        reverse=True,
    ):
        if run_directory == current_run.resolve() or promoted_at > as_of:
            continue
        manifest = verify_manifest(run_directory)
        outputs = manifest.get("output_files", {})
        if (
            publication_contract_kind(manifest) != "receipt"
            or not isinstance(outputs, Mapping)
            or not _RUN_OUTPUT_NAMES.issubset(outputs)
        ):
            continue
        prediction_path = run_directory / "predictions.parquet"
        frame = pd.read_parquet(prediction_path)
        if not set(PREDICTION_SCHEMA.names).issubset(frame.columns):
            # Weekly snapshots have no compatibility adapter. Old next-session
            # rows are not authoritative under the new target definitions.
            continue
        manifest_timestamp = pd.to_datetime(
            manifest.get("run_timestamp"), utc=True, errors="coerce"
        )
        if pd.isna(manifest_timestamp) or manifest_timestamp > as_of:
            continue
        valid = _valid_archived_live_rows(
            _project(frame, PREDICTION_SCHEMA.names),
            as_of=as_of,
            supported_horizons=frozenset(weekly_specifications),
            manifest_run=manifest_timestamp,
            promoted_at=promoted_at,
        )
        compatible = [
            _compatible_prospective_live_predictions(
                valid,
                specification=specification,
                assumed_round_trip_cost=assumed_round_trip_cost,
            )
            for specification in weekly_specifications.values()
        ]
        rows = pd.concat(compatible, ignore_index=True, sort=False)
        if rows.empty:
            continue
        rows = rows.drop_duplicates("id", keep="last").reset_index(drop=True)
        results.append(
            VerifiedWeeklyPredictionRun(
                run_directory=run_directory,
                promoted_at=promoted_at,
                predictions=rows,
            )
        )
    return tuple(results)


def _load_prior_live_predictions(
    runs_root: Path,
    current_run: Path,
    *,
    as_of: pd.Timestamp,
    specifications: Mapping[str, HorizonSpecification],
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    generic_supported_horizons = frozenset(
        horizon
        for horizon in specifications
        if not is_weekly_horizon(horizon)
    )
    datastore_root = runs_root.resolve().parents[1]
    try:
        promoted_receipt_runs = authoritative_receipt_runs(datastore_root)
    except CurrentPublicationError as exc:
        raise RuntimeError(
            "Authoritative current publication is invalid; archived LIVE "
            "evidence cannot be reconciled safely."
        ) from exc
    legacy_required = set(PREDICTION_SCHEMA.names).difference(
        {
            "model_version",
            "target_definition_version",
            "target_specification",
        }
    )
    if runs_root.is_dir():
        for path in sorted(runs_root.glob("*/predictions.parquet")):
            if path.parent == current_run:
                continue
            try:
                manifest = verify_manifest(path.parent)
                outputs = manifest.get("output_files", {})
                if (
                    not isinstance(outputs, Mapping)
                    or not _RUN_OUTPUT_NAMES.issubset(outputs)
                ):
                    continue
                contract_kind = publication_contract_kind(manifest)
                if contract_kind == "invalid":
                    continue
                receipt_promoted_at: pd.Timestamp | None = None
                manifest_timestamp = pd.to_datetime(
                    manifest.get("run_timestamp"),
                    utc=True,
                    errors="coerce",
                )
                if contract_kind == "receipt":
                    receipt_promoted_at = promoted_receipt_runs.get(
                        path.parent.resolve()
                    )
                    if receipt_promoted_at is None:
                        # A prepared receipt is not proof of publication. Only
                        # runs reachable from the atomic pointer are evidence.
                        continue
                if pd.isna(manifest_timestamp) or manifest_timestamp > as_of:
                    continue
                frame = pd.read_parquet(path)
            except Exception:
                continue
            if legacy_required.issubset(frame.columns):
                if "model_version" not in frame.columns:
                    frame["model_version"] = path.parent.name
                target_metadata = _manifest_target_metadata(
                    manifest,
                    specifications=specifications,
                )
                frame["target_definition_version"] = frame["horizon"].map(
                    {
                        horizon: metadata[0]
                        for horizon, metadata in target_metadata.items()
                    }
                )
                frame["target_specification"] = frame["horizon"].map(
                    {
                        horizon: metadata[1]
                        for horizon, metadata in target_metadata.items()
                    }
                )
                frame = _valid_archived_live_rows(
                    frame,
                    as_of=as_of,
                    supported_horizons=generic_supported_horizons,
                    manifest_run=(
                        manifest_timestamp
                        if contract_kind == "receipt"
                        else None
                    ),
                    promoted_at=receipt_promoted_at,
                )
                frames.append(
                    _project(
                        frame,
                        PREDICTION_SCHEMA.names,
                    )
                )
    if not frames:
        return empty_frame(PREDICTION_SCHEMA)
    combined = pd.concat(frames, ignore_index=True, sort=False)
    return combined.drop_duplicates(
        ["symbol", "horizon", "decision_timestamp", "prediction_created_at"],
        keep="last",
    ).reset_index(drop=True)


def _load_verified_active_prior_ordinary_forecasts(
    datastore_root: Path,
    *,
    current_run: Path,
    publication_time: pd.Timestamp,
    samples: pd.DataFrame,
    current_predictions: pd.DataFrame,
    specifications: Mapping[str, HorizonSpecification],
    assumed_round_trip_cost: float,
) -> pd.DataFrame:
    """Select receipt-proven ordinary forecasts whose targets are in progress.

    Only the run that originally issued a row can contribute it. A copy in a
    later publication falls before that later manifest timestamp and is
    rejected by ``_valid_archived_live_rows``. This preserves original
    issuance lineage and prevents repeated publications from creating new
    LIVE evidence.
    """

    ordinary_specifications = {
        horizon: specification
        for horizon, specification in specifications.items()
        if not is_weekly_horizon(horizon)
    }
    if not ordinary_specifications or samples.empty:
        return empty_frame(PREDICTION_SCHEMA)

    published_at = utc_timestamp(publication_time)
    try:
        promoted_runs = authoritative_receipt_runs(datastore_root)
    except CurrentPublicationError as exc:
        raise RuntimeError(
            "Authoritative current publication is invalid; active prior "
            "forecasts cannot be carried safely."
        ) from exc

    frames: list[pd.DataFrame] = []
    supported_horizons = frozenset(ordinary_specifications)
    for run_directory, promoted_at in sorted(
        promoted_runs.items(),
        key=lambda item: (item[1], str(item[0])),
    ):
        if (
            run_directory == current_run.resolve()
            or promoted_at > published_at
        ):
            continue
        try:
            manifest = verify_manifest(run_directory)
            outputs = manifest.get("output_files", {})
            if (
                publication_contract_kind(manifest) != "receipt"
                or not isinstance(outputs, Mapping)
                or not _RUN_OUTPUT_NAMES.issubset(outputs)
            ):
                continue
            manifest_timestamp = pd.to_datetime(
                manifest.get("run_timestamp"),
                utc=True,
                errors="coerce",
            )
            if (
                pd.isna(manifest_timestamp)
                or manifest_timestamp > published_at
            ):
                continue
            archived = pd.read_parquet(
                run_directory / "predictions.parquet"
            )
        except Exception:
            # The authoritative-chain verifier has already rejected corrupt
            # manifests and receipts. An incompatible prediction artifact is
            # simply ineligible for carry-forward.
            continue
        if not set(PREDICTION_SCHEMA.names).issubset(archived.columns):
            continue
        valid = _valid_archived_live_rows(
            _project(archived, PREDICTION_SCHEMA.names),
            as_of=published_at,
            supported_horizons=supported_horizons,
            manifest_run=manifest_timestamp,
            promoted_at=promoted_at,
        )
        compatible = [
            _compatible_prospective_live_predictions(
                valid,
                specification=specification,
                assumed_round_trip_cost=assumed_round_trip_cost,
            )
            for specification in ordinary_specifications.values()
        ]
        eligible = pd.concat(compatible, ignore_index=True, sort=False)
        if eligible.empty:
            continue
        eligible["_source_promoted_at"] = promoted_at
        eligible["_source_run"] = str(run_directory)
        frames.append(eligible)

    if not frames:
        return empty_frame(PREDICTION_SCHEMA)

    candidates = pd.concat(frames, ignore_index=True, sort=False)
    target_start = pd.to_datetime(
        candidates["target_window_start"], utc=True, errors="coerce"
    )
    target_end = pd.to_datetime(
        candidates["target_window_end"], utc=True, errors="coerce"
    )
    candidates = candidates.loc[
        target_start.le(published_at) & target_end.gt(published_at)
    ].copy()
    if candidates.empty:
        return empty_frame(PREDICTION_SCHEMA)

    current_window_keys = _current_ordinary_target_window_keys(
        samples,
        specifications=ordinary_specifications,
        assumed_round_trip_cost=assumed_round_trip_cost,
    )
    candidates = candidates.loc[
        [
            _ordinary_target_window_key(row) in current_window_keys
            for _, row in candidates.iterrows()
        ]
    ].copy()
    if candidates.empty:
        return empty_frame(PREDICTION_SCHEMA)

    candidates["prediction_created_at"] = pd.to_datetime(
        candidates["prediction_created_at"], utc=True, errors="coerce"
    )
    candidates["decision_timestamp"] = pd.to_datetime(
        candidates["decision_timestamp"], utc=True, errors="coerce"
    )
    candidates = (
        candidates.sort_values(
            [
                "symbol",
                "horizon",
                "prediction_created_at",
                "_source_promoted_at",
                "decision_timestamp",
                "id",
                "_source_run",
            ],
            kind="mergesort",
        )
        .groupby(["symbol", "horizon"], sort=False, as_index=False)
        .tail(1)
        .reset_index(drop=True)
    )

    current_valid = _valid_archived_live_rows(
        current_predictions,
        as_of=published_at,
        supported_horizons=supported_horizons,
    )
    current_compatible_frames = [
        _compatible_prospective_live_predictions(
            current_valid,
            specification=specification,
            assumed_round_trip_cost=assumed_round_trip_cost,
        )
        for specification in ordinary_specifications.values()
    ]
    current_compatible = pd.concat(
        current_compatible_frames,
        ignore_index=True,
        sort=False,
    )
    if not current_compatible.empty:
        current_compatible["prediction_created_at"] = pd.to_datetime(
            current_compatible["prediction_created_at"],
            utc=True,
            errors="coerce",
        )
        latest_current = (
            current_compatible.groupby(["symbol", "horizon"])[
                "prediction_created_at"
            ]
            .max()
            .to_dict()
        )

        def current_is_not_newer(row: pd.Series) -> bool:
            latest = latest_current.get(
                (row["symbol"], row["horizon"]),
                pd.NaT,
            )
            return pd.isna(latest) or latest <= row["prediction_created_at"]

        candidates = candidates.loc[
            candidates.apply(current_is_not_newer, axis=1)
        ].copy()
    if candidates.empty:
        return empty_frame(PREDICTION_SCHEMA)
    return _project(candidates, PREDICTION_SCHEMA.names).reset_index(drop=True)


def _current_ordinary_target_window_keys(
    samples: pd.DataFrame,
    *,
    specifications: Mapping[str, HorizonSpecification],
    assumed_round_trip_cost: float,
) -> set[tuple[object, ...]]:
    required = {
        "symbol",
        "horizon",
        "target_window_start",
        "target_window_end",
        "actionable_until",
        "target_definition_version",
        "target_specification",
        "assumed_round_trip_cost",
    }
    if samples.empty or not required.issubset(samples.columns):
        return set()
    working = samples.copy()
    costs = pd.to_numeric(
        working["assumed_round_trip_cost"], errors="coerce"
    )
    cost_matches = np.isclose(
        costs,
        float(assumed_round_trip_cost),
        rtol=0.0,
        atol=1e-12,
        equal_nan=False,
    )
    contract_matches = pd.Series(False, index=working.index)
    for horizon, specification in specifications.items():
        contract_matches |= (
            working["horizon"].eq(horizon)
            & working["target_definition_version"].eq(
                specification.target_definition_version
            )
            & working["target_specification"].eq(
                _canonical_target_specification(specification)
            )
        )
    working = working.loc[cost_matches & contract_matches].copy()
    return {
        _ordinary_target_window_key(row)
        for _, row in working.iterrows()
    }


def _ordinary_target_window_key(row: pd.Series) -> tuple[object, ...]:
    return (
        str(row["symbol"]).strip().upper(),
        str(row["horizon"]).strip().lower(),
        pd.to_datetime(row["target_window_start"], utc=True, errors="coerce"),
        pd.to_datetime(row["target_window_end"], utc=True, errors="coerce"),
        pd.to_datetime(row["actionable_until"], utc=True, errors="coerce"),
        str(row["target_definition_version"]),
        str(row["target_specification"]),
    )


def _manifest_target_metadata(
    manifest: Mapping[str, object],
    *,
    specifications: Mapping[str, HorizonSpecification],
) -> dict[str, tuple[str, str]]:
    configuration = manifest.get("configuration")
    configuration = (
        configuration if isinstance(configuration, Mapping) else {}
    )
    raw_specifications = configuration.get("horizon_specifications")
    raw_specifications = (
        raw_specifications
        if isinstance(raw_specifications, Mapping)
        else {}
    )
    metadata: dict[str, tuple[str, str]] = {}
    for horizon, current in specifications.items():
        raw = raw_specifications.get(horizon)
        if isinstance(raw, Mapping):
            payload = dict(raw)
            version = str(
                payload.get("target_definition_version")
                or _LEGACY_TARGET_DEFINITION_VERSIONS.get(horizon)
                or ""
            )
        elif horizon == "1d":
            # Early one-id-v1 manifests predate per-horizon serialization.
            # The 1d target policy is unchanged; exact window and cost checks
            # remain mandatory at reconciliation. The retired next-session 1w
            # policy must never be inferred as the dynamic remaining-week aggregate.
            payload = current.as_dict()
            version = current.target_definition_version
        else:
            version = _LEGACY_TARGET_DEFINITION_VERSIONS.get(horizon, "")
            payload = {
                "horizon": horizon,
                "target_definition_version": version,
                "legacy_manifest_contract": "not_serialized",
            }
        metadata[horizon] = (
            version,
            _canonical_target_specification(payload),
        )
    return metadata


def _canonical_target_specification(
    specification: Mapping[str, object] | HorizonSpecification,
) -> str:
    payload = (
        specification.as_dict()
        if isinstance(specification, HorizonSpecification)
        else dict(specification)
    )
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _valid_archived_live_rows(
    frame: pd.DataFrame,
    *,
    as_of: pd.Timestamp,
    supported_horizons: frozenset[str],
    manifest_run: object | None = None,
    promoted_at: object | None = None,
) -> pd.DataFrame:
    if frame.empty:
        return frame
    output = frame.copy()
    timestamp_columns = (
        "decision_timestamp",
        "information_available_at",
        "target_window_start",
        "target_window_end",
        "actionable_until",
        "prediction_created_at",
    )
    for column in timestamp_columns:
        output[column] = pd.to_datetime(
            output[column],
            utc=True,
            errors="coerce",
        )
    probability = pd.to_numeric(
        output["calibrated_probability"],
        errors="coerce",
    )
    cost = pd.to_numeric(
        output["assumed_round_trip_cost"],
        errors="coerce",
    )
    symbol = output["symbol"].astype("string").str.strip()
    weekly = output["horizon"].astype("string").isin(WEEKLY_HORIZON_ORDER)
    valid_deadline = (
        ~weekly
        & output["actionable_until"].le(output["target_window_start"])
    ) | (
        weekly
        & output["actionable_until"].le(output["target_window_end"])
    )
    valid = (
        output["prediction_mode"].eq("LIVE")
        & output["prediction_status"].eq("CREATED")
        & output["horizon"].isin(supported_horizons)
        & symbol.notna()
        & symbol.ne("")
        & output.loc[:, timestamp_columns].notna().all(axis=1)
        & output["decision_timestamp"].le(output["information_available_at"])
        & output["information_available_at"].le(
            output["prediction_created_at"]
        )
        & output["prediction_created_at"].lt(output["actionable_until"])
        & valid_deadline
        & output["prediction_created_at"].le(as_of)
        & probability.between(0.0, 1.0, inclusive="both")
        & np.isfinite(cost)
        & cost.ge(0.0)
        & cost.lt(1.0)
        & output["target_definition_version"].notna()
        & output["target_specification"].notna()
    )
    if manifest_run is not None or promoted_at is not None:
        manifest_timestamp = pd.to_datetime(
            manifest_run,
            utc=True,
            errors="coerce",
        )
        promotion_timestamp = pd.to_datetime(
            promoted_at,
            utc=True,
            errors="coerce",
        )
        if pd.isna(manifest_timestamp) or pd.isna(promotion_timestamp):
            return output.iloc[0:0].copy()
        valid &= (
            output["prediction_created_at"].ge(manifest_timestamp)
            & output["prediction_created_at"].le(promotion_timestamp)
            & output["actionable_until"].gt(promotion_timestamp)
            & pd.Series(
                promotion_timestamp <= utc_timestamp(as_of),
                index=output.index,
            )
        )
    return output.loc[valid].reset_index(drop=True)


def _compatible_prospective_live_predictions(
    predictions: pd.DataFrame,
    *,
    specification: HorizonSpecification,
    assumed_round_trip_cost: float,
) -> pd.DataFrame:
    if predictions.empty:
        return predictions.iloc[0:0].copy()
    created = pd.to_datetime(
        predictions["prediction_created_at"],
        utc=True,
        errors="coerce",
    )
    information = pd.to_datetime(
        predictions["information_available_at"],
        utc=True,
        errors="coerce",
    )
    target_start = pd.to_datetime(
        predictions["target_window_start"],
        utc=True,
        errors="coerce",
    )
    target_end = pd.to_datetime(
        predictions["target_window_end"],
        utc=True,
        errors="coerce",
    )
    actionable_until = pd.to_datetime(
        predictions["actionable_until"],
        utc=True,
        errors="coerce",
    )
    cost = pd.to_numeric(
        predictions["assumed_round_trip_cost"],
        errors="coerce",
    )
    cost_matches = pd.Series(
        np.isclose(
            cost,
            float(assumed_round_trip_cost),
            rtol=0.0,
            atol=1e-12,
            equal_nan=False,
        ),
        index=predictions.index,
    )
    deadline_matches = (
        actionable_until.le(target_end)
        if is_weekly_horizon(specification.horizon)
        else actionable_until.le(target_start)
    )
    compatible = (
        predictions["prediction_mode"].eq("LIVE")
        & predictions["prediction_status"].eq("CREATED")
        & predictions["horizon"].eq(specification.horizon)
        & predictions["target_definition_version"].eq(
            specification.target_definition_version
        )
        & predictions["target_specification"].eq(
            _canonical_target_specification(specification)
        )
        & information.notna()
        & created.notna()
        & target_start.notna()
        & target_end.notna()
        & actionable_until.notna()
        & information.le(created)
        & created.lt(actionable_until)
        & deadline_matches
        & cost_matches
    )
    return predictions.loc[compatible].copy()


def _evaluation_frame(
    predictions: pd.DataFrame,
    samples: pd.DataFrame,
    *,
    evaluated_at: pd.Timestamp,
) -> pd.DataFrame:
    if predictions.empty:
        return empty_frame(EVALUATION_SCHEMA)
    samples = samples.copy()
    predictions = predictions.copy()
    for frame in (samples, predictions):
        for column in ("target_definition_version", "target_specification"):
            if column not in frame:
                frame[column] = pd.NA
    if "model_version" not in predictions:
        predictions["model_version"] = pd.NA
    natural = ["symbol", "horizon", "decision_timestamp"]
    label_columns = [
        *natural,
        "label_status",
        "target_window_start",
        "target_window_end",
        "assumed_round_trip_cost",
        "target_definition_version",
        "target_specification",
        "target_cost_adjusted_positive",
        "forward_raw_return",
        "forward_cost_adjusted_return",
    ]
    labels = (
        samples.loc[:, label_columns]
        .drop_duplicates(natural, keep="last")
        .rename(
            columns={
                "target_window_start": "observed_target_window_start",
                "target_window_end": "observed_target_window_end",
                "assumed_round_trip_cost": "observed_round_trip_cost",
                "target_definition_version": (
                    "observed_target_definition_version"
                ),
                "target_specification": "observed_target_specification",
            }
        )
    )
    prediction_rows = predictions.drop(columns=["id"]).copy()
    prediction_rows["decision_timestamp"] = pd.to_datetime(
        prediction_rows["decision_timestamp"],
        utc=True,
        errors="coerce",
    )
    labels["decision_timestamp"] = pd.to_datetime(
        labels["decision_timestamp"],
        utc=True,
        errors="coerce",
    )
    if (
        prediction_rows["decision_timestamp"].isna().any()
        or labels["decision_timestamp"].isna().any()
    ):
        raise ValueError(
            "Predictions and labels require valid UTC decision timestamps"
        )
    merged = prediction_rows.merge(
        labels,
        on=natural,
        how="left",
        validate="many_to_one",
    )
    observed = pd.to_numeric(
        merged["target_cost_adjusted_positive"], errors="coerce"
    )
    matured = merged["label_status"].eq("COMPLETE") & observed.notna()
    predicted_start = pd.to_datetime(
        merged["target_window_start"],
        utc=True,
        errors="coerce",
    )
    predicted_end = pd.to_datetime(
        merged["target_window_end"],
        utc=True,
        errors="coerce",
    )
    predicted_deadline = pd.to_datetime(
        merged["actionable_until"],
        utc=True,
        errors="coerce",
    )
    observed_start = pd.to_datetime(
        merged["observed_target_window_start"],
        utc=True,
        errors="coerce",
    )
    observed_end = pd.to_datetime(
        merged["observed_target_window_end"],
        utc=True,
        errors="coerce",
    )
    window_matches = (
        predicted_start.notna()
        & predicted_end.notna()
        & predicted_start.eq(observed_start)
        & predicted_end.eq(observed_end)
    )
    predicted_cost = pd.to_numeric(
        merged["assumed_round_trip_cost"],
        errors="coerce",
    )
    observed_cost = pd.to_numeric(
        merged["observed_round_trip_cost"],
        errors="coerce",
    )
    cost_matches = pd.Series(
        np.isclose(
            predicted_cost,
            observed_cost,
            rtol=0.0,
            atol=1e-12,
            equal_nan=False,
        ),
        index=merged.index,
    )
    both_contracts_legacy_missing = (
        merged["target_definition_version"].isna()
        & merged["observed_target_definition_version"].isna()
        & merged["target_specification"].isna()
        & merged["observed_target_specification"].isna()
    )
    predicted_version = merged["target_definition_version"].astype("string")
    observed_version = merged[
        "observed_target_definition_version"
    ].astype("string")
    predicted_specification = merged["target_specification"].astype("string")
    observed_specification = merged[
        "observed_target_specification"
    ].astype("string")
    legacy_backtest_contract = (
        both_contracts_legacy_missing
        & merged["prediction_mode"].eq("BACKTEST")
    )
    contract_matches = legacy_backtest_contract | (
        merged["target_definition_version"].notna()
        & merged["observed_target_definition_version"].notna()
        & predicted_version.fillna("").eq(observed_version.fillna(""))
        & merged["target_specification"].notna()
        & merged["observed_target_specification"].notna()
        & predicted_specification.fillna("").eq(
            observed_specification.fillna("")
        )
    )
    prediction_created = pd.to_datetime(
        merged["prediction_created_at"],
        utc=True,
        errors="coerce",
    )
    information_available = pd.to_datetime(
        merged["information_available_at"],
        utc=True,
        errors="coerce",
    )
    prediction_valid = (
        merged["prediction_status"].eq("CREATED")
        & merged["prediction_mode"].isin(("LIVE", "BACKTEST"))
        & prediction_created.notna()
        & information_available.notna()
        & information_available.le(prediction_created)
    )
    weekly_prediction = merged["horizon"].astype("string").isin(
        WEEKLY_HORIZON_ORDER
    )
    deadline_geometry = (
        ~weekly_prediction & predicted_deadline.le(predicted_start)
    ) | (
        weekly_prediction & predicted_deadline.le(predicted_end)
    )
    pre_deadline = (
        ~merged["prediction_mode"].eq("LIVE")
        | (
            prediction_created.notna()
            & predicted_deadline.notna()
            & predicted_start.notna()
            & predicted_end.notna()
            & deadline_geometry
            & prediction_created.lt(predicted_deadline)
        )
    )
    scoreable = (
        prediction_valid
        & matured
        & contract_matches
        & window_matches
        & cost_matches
        & pre_deadline
    )
    raw = pd.to_numeric(merged["raw_probability"], errors="coerce")
    calibrated = pd.to_numeric(
        merged["calibrated_probability"], errors="coerce"
    )
    output = merged.loc[
        :,
        [
            "symbol",
            "provider",
            "horizon",
            "decision_timestamp",
            "target_window_start",
            "target_window_end",
            "prediction_created_at",
            "model_name",
            "model_version",
            "prediction_mode",
            "target_definition_version",
            "target_specification",
            "assumed_round_trip_cost",
            "raw_probability",
            "calibrated_probability",
        ],
    ].copy()
    output["evaluated_at"] = evaluated_at
    output["evaluation_status"] = np.select(
        (
            ~prediction_valid,
            ~matured,
            matured & ~contract_matches,
            matured & contract_matches & ~window_matches,
            matured & contract_matches & window_matches & ~cost_matches,
            (
                matured
                & contract_matches
                & window_matches
                & cost_matches
                & ~pre_deadline
            ),
        ),
        (
            "INVALID_PREDICTION",
            "PENDING",
            "TARGET_CONTRACT_MISMATCH",
            "TARGET_WINDOW_MISMATCH",
            "CONFIGURATION_MISMATCH",
            "POST_ENTRY_PREDICTION",
        ),
        default="EVALUATED",
    )
    output["observed_target"] = observed.where(scoreable)
    output["observed_forward_raw_return"] = pd.to_numeric(
        merged["forward_raw_return"], errors="coerce"
    ).where(scoreable)
    output["observed_forward_cost_adjusted_return"] = pd.to_numeric(
        merged["forward_cost_adjusted_return"], errors="coerce"
    ).where(scoreable)
    output["raw_log_loss"] = _log_loss_rows(observed, raw).where(scoreable)
    output["log_loss"] = _log_loss_rows(observed, calibrated).where(scoreable)
    output["raw_brier_score"] = ((raw - observed) ** 2).where(scoreable)
    output["brier_score"] = ((calibrated - observed) ** 2).where(scoreable)
    output["prediction_correct_0_5"] = (
        calibrated.ge(0.5).eq(observed.eq(1))
    ).where(scoreable)
    output = frame_with_readable_id(
        output,
        key_columns=(
            "symbol",
            "horizon",
            "decision_timestamp",
            "prediction_created_at",
        ),
    )
    return _project(output, EVALUATION_SCHEMA.names)


def _log_loss_rows(target: pd.Series, probability: pd.Series) -> pd.Series:
    clipped = probability.clip(1e-12, 1.0 - 1e-12)
    return -(target * np.log(clipped) + (1.0 - target) * np.log(1.0 - clipped))


def _monitoring_frame(
    predictions: pd.DataFrame,
    evaluations: pd.DataFrame,
    *,
    models: Mapping[str, RuntimeModel],
    monitored_at: pd.Timestamp,
) -> pd.DataFrame:
    evaluated = evaluations.loc[evaluations["evaluation_status"].eq("EVALUATED")]
    live_evaluated = _canonical_live_evaluations(evaluations)
    rows = [
        _metric_row(
            monitored_at,
            category="coverage",
            name="prediction_rows",
            value=float(len(predictions)),
            unit="rows",
            evidence=len(predictions),
        ),
        _metric_row(
            monitored_at,
            category="coverage",
            name="evaluated_predictions",
            value=float(len(evaluated)),
            unit="rows",
            evidence=len(evaluated),
        ),
        _metric_row(
            monitored_at,
            category="model",
            name="model_reuse_rate",
            value=(
                float(np.mean([model.reused for model in models.values()]))
                if models
                else 0.0
            ),
            unit="ratio",
            evidence=len(models),
        ),
    ]
    rows.extend(
        _performance_metric_rows(
            evaluated,
            monitored_at=monitored_at,
            scope_type="global",
            scope_value="all",
            details="All evaluated predictions, including offline backtests.",
        )
    )
    route_pairs = set(
        predictions.loc[:, ["symbol", "horizon"]]
        .dropna()
        .astype(str)
        .itertuples(index=False, name=None)
    ) | set(
        evaluations.loc[:, ["symbol", "horizon"]]
        .dropna()
        .astype(str)
        .itertuples(index=False, name=None)
    )
    observed_horizons = (
        set(evaluations["horizon"].dropna().astype(str))
        | set(models)
        | {horizon for _, horizon in route_pairs}
    )
    horizon_order = {
        horizon: index
        for index, horizon in enumerate(INTERNAL_HORIZON_ORDER)
    }
    for horizon in sorted(
        observed_horizons,
        key=lambda value: (horizon_order.get(value, len(horizon_order)), value),
    ):
        horizon_evaluated = evaluated.loc[evaluated["horizon"].eq(horizon)]
        horizon_live = live_evaluated.loc[
            live_evaluated["horizon"].eq(horizon)
        ]
        for symbol in sorted(
            symbol
            for symbol, route_horizon in route_pairs
            if route_horizon == horizon
        ):
            route_live = horizon_live.loc[
                horizon_live["symbol"].eq(symbol)
            ]
            completed_count = len(route_live)
            threshold = minimum_live_decisions(horizon)
            rows.append(
                _metric_row(
                    monitored_at,
                    category="live_evidence",
                    name="completed_live_forecasts",
                    value=float(completed_count),
                    unit="forecasts",
                    evidence=completed_count,
                    scope_type="symbol_horizon",
                    scope_value=f"{symbol}|{horizon}",
                    reference=float(threshold),
                    status=live_evidence_status(
                        horizon=horizon,
                        completed_decisions=completed_count,
                    ),
                    details=(
                        "Unique compatible matured prospective LIVE forecasts "
                        "for this symbol and horizon; repeated publications of "
                        "one decision count once."
                    ),
                )
            )
        rows.extend(
            _performance_metric_rows(
                horizon_evaluated,
                monitored_at=monitored_at,
                scope_type="horizon",
                scope_value=horizon,
                details=(
                    "Evaluated predictions for this horizon, including "
                    "offline backtests."
                ),
            )
        )
        if not horizon_live.empty:
            rows.extend(
                _performance_metric_rows(
                    horizon_live,
                    monitored_at=monitored_at,
                    scope_type="live_horizon",
                    scope_value=horizon,
                    details=(
                        "Pooled compatible matured LIVE predictions across "
                        "symbols for this horizon; not the route-card count."
                    ),
                )
            )
    output = pd.DataFrame(rows)
    output = frame_with_readable_id(
        output,
        key_columns=(
            "metric_name",
            "scope_type",
            "scope_value",
            "monitored_at",
        ),
    )
    return _project(output, MONITORING_SCHEMA.names)


def _metric_row(
    monitored_at: pd.Timestamp,
    *,
    category: str,
    name: str,
    value: float | None,
    unit: str,
    evidence: int,
    scope_type: str = "global",
    scope_value: str = "all",
    reference: float | None = None,
    status: str | None = None,
    details: str = "",
    window_start: object | None = None,
) -> dict[str, object]:
    return {
        "monitored_at": monitored_at,
        "category": category,
        "metric_name": name,
        "scope_type": scope_type,
        "scope_value": scope_value,
        "status": status or (
            "OK" if value is not None else "INSUFFICIENT_EVIDENCE"
        ),
        "observed_value": value,
        "reference_value": reference,
        "unit": unit,
        "evidence_row_count": evidence,
        "window_start": window_start,
        "window_end": monitored_at,
        "details": details,
    }


def _performance_metric_rows(
    evaluated: pd.DataFrame,
    *,
    monitored_at: pd.Timestamp,
    scope_type: str,
    scope_value: str,
    details: str,
) -> list[dict[str, object]]:
    evidence = len(evaluated)
    window_start = (
        pd.to_datetime(
            evaluated["decision_timestamp"],
            utc=True,
            errors="coerce",
        ).min()
        if evidence
        else None
    )
    calibration_gap = _calibration_gap_or_none(evaluated)
    roc_auc = _roc_auc_or_none(evaluated)
    metrics = (
        ("mean_raw_log_loss", _mean_or_none(evaluated["raw_log_loss"]), "loss"),
        ("mean_log_loss", _mean_or_none(evaluated["log_loss"]), "loss"),
        (
            "mean_raw_brier_score",
            _mean_or_none(evaluated["raw_brier_score"]),
            "score",
        ),
        (
            "mean_brier_score",
            _mean_or_none(evaluated["brier_score"]),
            "score",
        ),
        (
            "accuracy_at_0_5",
            _mean_or_none(evaluated["prediction_correct_0_5"]),
            "ratio",
        ),
        (
            "observed_positive_rate",
            _mean_or_none(evaluated["observed_target"]),
            "ratio",
        ),
        (
            "mean_calibrated_probability",
            _mean_or_none(evaluated["calibrated_probability"]),
            "probability",
        ),
        ("calibration_gap", calibration_gap, "absolute_probability"),
        ("roc_auc", roc_auc, "score"),
    )
    rows: list[dict[str, object]] = []
    for name, value, unit in metrics:
        reference = None
        status = None
        metric_details = details
        if name == "calibration_gap":
            reference = 0.05
            if value is not None:
                status = "OK" if value <= reference else "WARNING"
            metric_details += " Reference is a 0.05 absolute calibration gap."
        elif name == "roc_auc":
            reference = 0.5
            if value is not None:
                status = "OK" if value >= reference else "WARNING"
            metric_details += " Reference is chance-level discrimination."
        rows.append(
            _metric_row(
                monitored_at,
                category="performance",
                name=name,
                value=value,
                unit=unit,
                evidence=evidence,
                scope_type=scope_type,
                scope_value=scope_value,
                reference=reference,
                status=status,
                details=metric_details,
                window_start=window_start,
            )
        )
    return rows


def _mean_or_none(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return None if numeric.empty else float(numeric.mean())


def _canonical_live_evaluations(evaluations: pd.DataFrame) -> pd.DataFrame:
    if evaluations.empty:
        return evaluations.iloc[0:0].copy()
    live = evaluations.loc[
        evaluations["prediction_mode"].eq("LIVE")
        & evaluations["evaluation_status"].eq("EVALUATED")
    ].copy()
    if live.empty:
        return live
    live["prediction_created_at"] = pd.to_datetime(
        live["prediction_created_at"],
        utc=True,
        errors="coerce",
    )
    return (
        live.sort_values("prediction_created_at", kind="mergesort")
        .drop_duplicates(
            ["symbol", "horizon", "decision_timestamp"],
            keep="first",
        )
        .reset_index(drop=True)
    )


def _calibration_gap_or_none(evaluated: pd.DataFrame) -> float | None:
    paired = evaluated.loc[
        :,
        ["observed_target", "calibrated_probability"],
    ].apply(pd.to_numeric, errors="coerce").dropna()
    if paired.empty:
        return None
    return float(
        abs(
            paired["calibrated_probability"].mean()
            - paired["observed_target"].mean()
        )
    )


def _roc_auc_or_none(evaluated: pd.DataFrame) -> float | None:
    paired = evaluated.loc[
        :,
        ["observed_target", "calibrated_probability"],
    ].apply(pd.to_numeric, errors="coerce").dropna()
    if paired.empty:
        return None
    labels = paired["observed_target"].astype(int)
    positive_count = int(labels.eq(1).sum())
    negative_count = int(labels.eq(0).sum())
    if positive_count == 0 or negative_count == 0:
        return None
    ranks = paired["calibrated_probability"].rank(method="average")
    positive_rank_sum = float(ranks.loc[labels.eq(1)].sum())
    return (
        positive_rank_sum
        - positive_count * (positive_count + 1) / 2.0
    ) / (positive_count * negative_count)


def _intelligence_frame(
    materialization: RollingMaterialization,
    samples: pd.DataFrame,
    predictions: pd.DataFrame,
    evaluations: pd.DataFrame,
    *,
    models: Mapping[str, RuntimeModel],
    created_at: pd.Timestamp,
    carried_predictions: pd.DataFrame | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    completed_live = _canonical_live_evaluations(evaluations)
    weekly_bundle_horizons = _coherent_weekly_live_bundle_horizons(predictions)
    carried_frame = (
        carried_predictions
        if carried_predictions is not None
        else empty_frame(PREDICTION_SCHEMA)
    )
    carried_ids = set(
        carried_frame["id"].dropna().astype(str)
    )
    for route in materialization.routes:
        symbol = route.symbol
        horizon = route.horizon
        route_samples = samples.loc[
            samples["symbol"].eq(symbol) & samples["horizon"].eq(horizon)
        ].sort_values("decision_timestamp")
        live = predictions.loc[
            predictions["symbol"].eq(symbol)
            & predictions["horizon"].eq(horizon)
            & predictions["prediction_mode"].eq("LIVE")
        ].sort_values("prediction_created_at")
        prediction = live.iloc[-1] if not live.empty else None
        sample = route_samples.iloc[-1] if not route_samples.empty else None
        decision_timestamp = (
            prediction["decision_timestamp"]
            if prediction is not None
            else (
                sample["decision_timestamp"]
                if sample is not None
                else created_at
            )
        )
        target_start = (
            prediction["target_window_start"]
            if prediction is not None
                else (sample["target_window_start"] if sample is not None else pd.NaT)
        )
        target_end = (
            prediction["target_window_end"]
            if prediction is not None
            else (sample["target_window_end"] if sample is not None else pd.NaT)
        )
        weekly_snapshot = is_weekly_horizon(horizon) and prediction is not None
        symbol_weekly_horizons = weekly_bundle_horizons.get(
            str(symbol).strip().upper(), ()
        )
        weekly_component_not_applicable = (
            horizon in WEEKLY_HORIZON_ORDER[1:]
            and prediction is None
            and bool(symbol_weekly_horizons)
            and horizon not in symbol_weekly_horizons
        )
        carried = (
            prediction is not None
            and str(prediction.get("id")) in carried_ids
        )
        in_progress = (
            carried
            and pd.notna(target_start)
            and pd.notna(target_end)
            and pd.Timestamp(target_start) <= created_at
            and created_at < pd.Timestamp(target_end)
        )
        actionable = (
            prediction is not None
            and pd.notna(target_start)
            and created_at < pd.Timestamp(target_start)
        )
        route_evaluations = completed_live.loc[
            completed_live["symbol"].eq(symbol)
            & completed_live["horizon"].eq(horizon)
        ]
        model = models.get(horizon)
        completed_decisions = len(route_evaluations)
        minimum_decisions = minimum_live_decisions(horizon)
        route_live_evidence_status = live_evidence_status(
            horizon=horizon,
            completed_decisions=completed_decisions,
        )
        current_evidence_status = (
            _weekly_prediction_evidence_status(prediction, evaluations)
            if weekly_snapshot
            else None
        )
        limitation_parts = [
            value
            for value in (
                route.error,
                None if model is not None else "model unavailable",
                (
                    None
                    if prediction is not None
                    else (
                        "not part of current remaining-week snapshot"
                        if weekly_component_not_applicable
                        else (
                            "no remaining-week snapshot"
                            if is_weekly_horizon(horizon)
                            else "no current forecast"
                        )
                    )
                ),
                "remaining-week snapshot" if weekly_snapshot else None,
                (
                    "forecast in progress; entry window passed; not actionable"
                    if in_progress
                    else None
                ),
                "research support only; automated action is disabled",
            )
            if value
        ]
        probability = (
            float(prediction["calibrated_probability"])
            if prediction is not None
            and (weekly_snapshot or actionable or in_progress)
            else None
        )
        rows.append(
            {
                "symbol": symbol,
                "horizon": horizon,
                "decision_timestamp": decision_timestamp,
                "forecast_created_at": (
                    prediction["prediction_created_at"]
                    if prediction is not None
                    else created_at
                ),
                "information_available_at": (
                    prediction["information_available_at"]
                    if prediction is not None
                    else (
                        sample["information_available_at"]
                        if sample is not None
                        else pd.NaT
                    )
                ),
                "target_window_start": target_start,
                "target_window_end": (
                    target_end
                ),
                "actionable_until": (
                    prediction["actionable_until"]
                    if prediction is not None
                    else (
                        sample["actionable_until"]
                        if sample is not None
                        else pd.NaT
                    )
                ),
                "target_definition_version": (
                    prediction.get("target_definition_version")
                    if prediction is not None
                    else (
                        sample.get("target_definition_version")
                        if sample is not None
                        else None
                    )
                ),
                "probability_up": probability,
                "probability_down": (
                    None if probability is None else 1.0 - probability
                ),
                "actionability_status": (
                    "FROZEN_WEEKLY_SNAPSHOT"
                    if weekly_snapshot
                    else (
                        "TARGET_WINDOW_STARTED"
                        if in_progress
                        else ("ACTIONABLE" if actionable else "NOT_ACTIONABLE")
                    )
                ),
                "operational_status": (
                    "OPERATIONALLY_CURRENT"
                    if route.status == "READY"
                    and (
                        actionable
                        or in_progress
                        or weekly_snapshot
                        or weekly_component_not_applicable
                    )
                    else (
                        "OPERATIONALLY_STALE"
                        if route.status == "READY"
                        else route.status
                    )
                ),
                "model_evidence_status": (
                    "OFFLINE_EVALUATED_CANDIDATE"
                    if model is not None
                    else "MODEL_UNAVAILABLE"
                ),
                "live_evidence_status": route_live_evidence_status,
                "intelligence_status": (
                    current_evidence_status
                    if weekly_snapshot
                    else (
                        "NOT_APPLICABLE_TO_REMAINING_WEEK"
                        if weekly_component_not_applicable
                        else (
                            "FORECAST_IN_PROGRESS"
                            if in_progress
                            else (
                                "RISK_ANALYSIS_SUPPORT"
                                if actionable
                                else "NO_CURRENT_FORECAST"
                            )
                        )
                    )
                ),
                "model_name": (
                    prediction["model_name"]
                    if prediction is not None
                    else (model.model_name if model is not None else None)
                ),
                "completed_decision_count": completed_decisions,
                "minimum_live_decision_count": minimum_decisions,
                "automated_action_allowed": False,
                "limitations": "; ".join(limitation_parts),
                "schema_version": "one-id-v2",
            }
        )
    output = pd.DataFrame(rows)
    if output.empty:
        return empty_frame(INTELLIGENCE_SCHEMA)
    output = frame_with_readable_id(
        output,
        key_columns=("symbol", "horizon", "decision_timestamp"),
    )
    return _project(output, INTELLIGENCE_SCHEMA.names)


def _coherent_weekly_live_bundle_horizons(
    predictions: pd.DataFrame,
) -> dict[str, tuple[str, ...]]:
    """Return the one coherent published weekly LIVE bundle per symbol.

    The runtime verifies receipt-chain and calendar validity before building the
    intelligence frame.  This local check deliberately repeats the structural
    invariants needed to distinguish a dynamic Day 1 prefix from a missing
    weekly publication.  Ambiguous or malformed input remains fail-closed.
    """

    required = {
        "symbol",
        "horizon",
        "decision_timestamp",
        "prediction_created_at",
        "prediction_mode",
        "prediction_status",
        "target_window_start",
        "target_window_end",
        "actionable_until",
        "model_name",
        "model_version",
        "calibrated_probability",
    }
    if predictions.empty or not required.issubset(predictions.columns):
        return {}
    weekly_live = predictions.loc[
        predictions["prediction_mode"].eq("LIVE")
        & predictions["horizon"].isin(WEEKLY_HORIZON_ORDER)
    ].copy()
    if weekly_live.empty:
        return {}

    candidates: dict[str, list[tuple[str, ...]]] = {}
    invalid_symbols: set[str] = set()
    for (symbol, _decision, _created), bundle in weekly_live.groupby(
        ["symbol", "decision_timestamp", "prediction_created_at"],
        sort=False,
        dropna=False,
    ):
        normalized_symbol = str(symbol or "").strip().upper()
        if not normalized_symbol:
            continue
        try:
            horizons = _weekly_bundle_horizons(bundle)
            _validate_weekly_bundle_geometry(bundle)
            created = pd.to_datetime(
                bundle["prediction_created_at"], utc=True, errors="coerce"
            )
            deadlines = pd.to_datetime(
                bundle["actionable_until"], utc=True, errors="coerce"
            )
            probability = pd.to_numeric(
                bundle["calibrated_probability"], errors="coerce"
            )
            if not (
                bundle["prediction_mode"].eq("LIVE").all()
                and bundle["prediction_status"].eq("CREATED").all()
                and created.notna().all()
                and created.nunique() == 1
                and deadlines.notna().all()
                and created.lt(deadlines).all()
                and bundle[["model_name", "model_version"]]
                .notna()
                .all()
                .all()
                and probability.between(0.0, 1.0, inclusive="both").all()
            ):
                raise RuntimeError("Weekly LIVE bundle is not publication-valid")
        except (KeyError, RuntimeError, TypeError, ValueError):
            invalid_symbols.add(normalized_symbol)
            continue
        candidates.setdefault(normalized_symbol, []).append(horizons)

    return {
        symbol: bundles[0]
        for symbol, bundles in candidates.items()
        if symbol not in invalid_symbols and len(bundles) == 1
    }


def _weekly_prediction_evidence_status(
    prediction: pd.Series,
    evaluations: pd.DataFrame,
) -> str:
    if evaluations.empty:
        return "PENDING_EVIDENCE"
    decision = pd.to_datetime(
        evaluations["decision_timestamp"], utc=True, errors="coerce"
    )
    created = pd.to_datetime(
        evaluations["prediction_created_at"], utc=True, errors="coerce"
    )
    matches = evaluations.loc[
        evaluations["symbol"].eq(prediction["symbol"])
        & evaluations["horizon"].eq(prediction["horizon"])
        & decision.eq(pd.Timestamp(prediction["decision_timestamp"]))
        & created.eq(pd.Timestamp(prediction["prediction_created_at"]))
        & evaluations["prediction_mode"].eq("LIVE")
    ].sort_values("evaluated_at")
    if matches.empty:
        return "PENDING_EVIDENCE"
    status = str(matches.iloc[-1]["evaluation_status"])
    if status == "EVALUATED":
        return "COMPLETED_EVIDENCE"
    if status == "PENDING":
        return "PENDING_EVIDENCE"
    return f"EVIDENCE_{status}"


def _report(
    reporter: Callable[[str], None] | None,
    message: str,
) -> None:
    if reporter is not None:
        reporter(message)
