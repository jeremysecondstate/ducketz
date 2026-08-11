from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
import tracemalloc
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd
import pyarrow.parquet as pq

from datafetching.bar_readiness import (
    wait_for_bar_readiness,
)
from datafetching.decision_time import (
    CycleTargetDecision,
    CycleTargetState,
    cycle_target_decision,
    expected_quarter_hour_target,
)
from datafetching.orchestrate import DEFAULT_WATCHLIST, normalize_symbols, read_watchlist
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import file_checksum, utc_timestamp, write_manifest
from ml.option_pricing.causal import (
    build_live_prediction_inputs,
    canonicalize_predictions,
    evaluate_offline_predictions,
    reconcile_predictions,
)
from ml.option_pricing.model import (
    PricingPartitions,
    fit_or_reuse_pricing_model,
    partition_pricing_samples_v2,
    route_partitions,
)
from ml.option_pricing.candidate import load_candidate_models, read_current_candidate
from ml.option_pricing.eligibility import (
    EligibilityPolicy,
    EligibilityPolicyArtifact,
    REQUIRED_SYMBOLS,
    build_eligibility_report,
    publish_eligibility_policy,
    publish_eligibility_report,
)
from ml.option_pricing.lineage import (
    verify_completed_option_pricing_lineage,
    verify_staged_option_pricing_run,
)
from ml.option_pricing.loop_native_eligibility import (
    build_loop_native_eligibility_report,
    publish_loop_native_eligibility_policy,
    publish_loop_native_eligibility_report,
    verify_loop_native_capture_lineage,
)
from ml.option_pricing.lockbox import read_lockbox_result
from ml.option_pricing.operations import (
    EXIT_EVIDENCE,
    RuntimeLimits,
    build_runtime_health,
    capacity_report,
    enforce_runtime_limits,
    publish_runtime_health,
    read_current_operational_readiness,
    read_current_runtime_health,
)
from ml.option_pricing.opra_materialization import (
    ClosedOpraLockboxInventory,
    materialize_committed_opra_history_v2,
)
from ml.option_pricing.policies import (
    BSGPModelPolicy,
    ContractSelectionPolicy,
    LOOP_NATIVE_SYMBOLS,
    LoopNativeModelPolicy,
    OPTION_PRICING_POLICY_VERSION,
    PricingPartitionConfig,
    ProjectionPolicy,
)
from ml.option_pricing.prediction import create_bsgp_shadow_rows, create_prediction_rows
from ml.option_pricing.schwab_materialization import (
    read_current_loop_native_schwab_materialization,
)
from ml.option_pricing.shadow_model import (
    LOOP_NATIVE_MODEL_FILE,
    LOOP_NATIVE_MODEL_MANIFEST,
    LOOP_NATIVE_MODEL_RECEIPT,
    LoopNativeModelLoad,
    load_prior_loop_native_model,
)
from ml.option_pricing_loop_native_worker import launch_loop_native_worker
from ml.option_pricing.publication import (
    OPTION_PRICING_POINTER_VERSION,
    OPTION_PRICING_PUBLICATION_VERSION,
    OPTION_PRICING_REPORT_NAME,
    pricing_pointer_path,
    publish_option_pricing_run,
    receipt_proven_prediction_rows,
    read_current_option_pricing_publication,
)
from ml.option_pricing.rates import load_point_in_time_rate_observations
from ml.option_pricing.reporting import (
    build_gate_report,
    build_monitoring_rows,
    build_pricing_surfaces,
)
from ml.option_pricing.strategy_outcomes import (
    read_current_strategy_outcome_evidence,
)
from ml.option_pricing.target_outcome import (
    TargetOutcomePublication,
    TargetOutcomeError,
    authoritative_target_outcomes,
    publish_target_outcome,
    read_target_outcome,
)
from ml.parquet_contracts import (
    OPTION_PRICING_EVALUATION_SCHEMA,
    OPTION_PRICING_MONITORING_SCHEMA,
    OPTION_PRICING_PREDICTION_SCHEMA,
    OPTION_PRICING_SAMPLE_SCHEMA,
    OPTION_PRICING_SURFACE_SCHEMA,
    empty_frame,
    frame_with_readable_id,
    write_parquet_with_schema,
)
from options.publication import committed_option_snapshots


@dataclass(frozen=True)
class OptionPricingRuntimeResult:
    run_directory: Path | None
    sample_rows: int
    prediction_rows: int
    evaluation_rows: int
    surface_rows: int
    monitoring_rows: int
    models_trained: int
    models_reused: int
    published_at: pd.Timestamp | None
    route_errors: Mapping[str, str]
    live_routes: Mapping[str, Mapping[str, object]]
    eligibility_report_directory: Path | None
    gate_status: str
    health_path: Path | None
    health_status: str
    health_exit_code: int
    target_snapshot_for: pd.Timestamp | None
    target_outcome_directory: Path | None
    target_outcome_status: str
    target_published_at: pd.Timestamp | None
    stage_timings: Mapping[str, float]
    cycle_mode: str = "ACTIONABLE"
    target_state: str = CycleTargetState.ACTIONABLE_EXACT_TARGET.value
    reason: str = ""
    next_eligible_cycle: pd.Timestamp | None = None
    current_target_sample_rows: int = 0
    current_target_prediction_rows: int = 0
    current_target_evaluation_rows: int = 0
    new_prospective_prediction_rows: int = 0
    new_prospective_evaluation_rows: int = 0


def run_option_pricing_once(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    run_timestamp: object | None = None,
    runtime_clock: Callable[[], object] | None = None,
    partition_config: PricingPartitionConfig | None = None,
    model_policy: BSGPModelPolicy | None = None,
    loop_native_model_policy: LoopNativeModelPolicy | None = None,
    contract_policy: ContractSelectionPolicy | None = None,
    projection_policy: ProjectionPolicy | None = None,
    rate_observations: pd.DataFrame | None = None,
    runtime_limits: RuntimeLimits | None = None,
    target_snapshot_for: object | None = None,
    bar_readiness_mode: str = "exact",
    bar_readiness_timeout_seconds: float = 45.0,
    readiness_sleeper: Callable[[float], None] = time.sleep,
    monotonic_clock: Callable[[], float] = time.monotonic,
    phase_offset_minutes: int = 1,
) -> OptionPricingRuntimeResult:
    """Publish one independent, shadow-only Pricing generation."""

    tracing_started_here = not tracemalloc.is_tracing()
    if tracing_started_here:
        tracemalloc.start()
    try:
        return _run_option_pricing_once_impl(
            datastore_root,
            symbols=symbols,
            run_timestamp=run_timestamp,
            runtime_clock=runtime_clock,
            partition_config=partition_config,
            model_policy=model_policy,
            loop_native_model_policy=loop_native_model_policy,
            contract_policy=contract_policy,
            projection_policy=projection_policy,
            rate_observations=rate_observations,
            runtime_limits=runtime_limits,
            target_snapshot_for=target_snapshot_for,
            bar_readiness_mode=bar_readiness_mode,
            bar_readiness_timeout_seconds=bar_readiness_timeout_seconds,
            readiness_sleeper=readiness_sleeper,
            monotonic_clock=monotonic_clock,
            phase_offset_minutes=phase_offset_minutes,
        )
    finally:
        if tracing_started_here:
            tracemalloc.stop()


def _run_option_pricing_once_impl(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    run_timestamp: object | None = None,
    runtime_clock: Callable[[], object] | None = None,
    partition_config: PricingPartitionConfig | None = None,
    model_policy: BSGPModelPolicy | None = None,
    loop_native_model_policy: LoopNativeModelPolicy | None = None,
    contract_policy: ContractSelectionPolicy | None = None,
    projection_policy: ProjectionPolicy | None = None,
    rate_observations: pd.DataFrame | None = None,
    runtime_limits: RuntimeLimits | None = None,
    target_snapshot_for: object | None = None,
    bar_readiness_mode: str = "exact",
    bar_readiness_timeout_seconds: float = 45.0,
    readiness_sleeper: Callable[[float], None] = time.sleep,
    monotonic_clock: Callable[[], float] = time.monotonic,
    phase_offset_minutes: int = 1,
) -> OptionPricingRuntimeResult:
    root = Path(datastore_root).resolve()
    clean_symbols = normalize_symbols(symbols)
    if not clean_symbols:
        raise ValueError("At least one Pricing symbol is required")
    created = utc_timestamp(run_timestamp)
    decision = cycle_target_decision(created)
    supplied_target = (
        utc_timestamp(target_snapshot_for) if target_snapshot_for is not None else None
    )
    if not decision.actionable:
        return _idle_pricing_result(
            root,
            decision=decision,
            phase_offset_minutes=phase_offset_minutes,
        )
    target = decision.target_snapshot_for
    assert target is not None
    if supplied_target is not None and supplied_target != target:
        raise ValueError(
            "Pricing target must match the calendar-owned target for run start; "
            "older targets cannot be replayed"
        )
    readiness_mode = str(bar_readiness_mode).strip().lower()
    if readiness_mode not in {"required", "exact"}:
        raise ValueError("bar_readiness_mode must be required or exact")
    if bar_readiness_timeout_seconds < 0:
        raise ValueError("bar_readiness_timeout_seconds cannot be negative")
    clock = runtime_clock or (lambda: utc_timestamp())
    cycle_started = time.perf_counter()
    stage_started = cycle_started
    stage_timings: dict[str, float] = {}
    effective_partitions = partition_config or PricingPartitionConfig()
    effective_model = model_policy or BSGPModelPolicy()
    effective_loop_native_model = loop_native_model_policy or LoopNativeModelPolicy()
    effective_contract = contract_policy or ContractSelectionPolicy()
    effective_projection = projection_policy or ProjectionPolicy()
    limits = runtime_limits or RuntimeLimits()
    loop_native_scope = (
        len(clean_symbols) == len(LOOP_NATIVE_SYMBOLS)
        and frozenset(clean_symbols) == frozenset(LOOP_NATIVE_SYMBOLS)
    )
    eligibility_policy = EligibilityPolicy()
    policy_artifact = publish_eligibility_policy(
        root,
        policy=eligibility_policy,
        partition_config=effective_partitions,
        model_policy=effective_model,
        contract_policy=effective_contract,
        projection_policy=effective_projection,
        published_at=created,
    )
    loop_native_policy_artifact = (
        publish_loop_native_eligibility_policy(
            root,
            model_policy=effective_loop_native_model,
            contract_policy=effective_contract,
            published_at=created,
        )
        if loop_native_scope
        else None
    )
    initial_capacity = capacity_report(root, limits=limits)
    if initial_capacity.get("status") != "PASS":
        raise RuntimeError(
            "Pricing preflight failed closed for disk capacity: "
            + json.dumps(initial_capacity, sort_keys=True)
        )
    stage_timings["preflight_seconds"] = time.perf_counter() - stage_started
    stage_started = time.perf_counter()

    source_files = [
        policy_artifact.directory / "policy.json",
        policy_artifact.directory / "receipt.json",
    ]
    if loop_native_policy_artifact is not None:
        source_files.extend(
            (
                loop_native_policy_artifact.directory / "policy.json",
                loop_native_policy_artifact.directory / "receipt.json",
            )
        )
    model_input_files: list[Path] = []
    if rate_observations is None:
        rate_observations, rate_files = load_point_in_time_rate_observations(root)
        source_files.extend(rate_files)
        model_input_files.extend(rate_files)
    earlier_shadow_model = (
        load_prior_loop_native_model(root, prediction_created_at=created)
        if loop_native_scope
        else None
    )
    (
        target_publication,
        target_live_samples,
        target_live_predictions,
        live_status,
        target_inputs,
        target_published_now,
        final_target_decision,
    ) = (
        _publish_fast_target_outcome(
            root,
            symbols=clean_symbols,
            target_snapshot_for=target,
            created_at=created,
            runtime_clock=clock,
            bar_readiness_mode=readiness_mode,
            contract_policy=effective_contract,
            projection_policy=effective_projection,
            rate_observations=rate_observations,
            cycle_decision=decision,
            bar_readiness_timeout_seconds=bar_readiness_timeout_seconds,
            readiness_sleeper=readiness_sleeper,
            monotonic_clock=monotonic_clock,
            loop_native_model_load=earlier_shadow_model,
            loop_native_model_policy=effective_loop_native_model,
        )
    )
    new_live_samples = target_live_samples if target_published_now else pd.DataFrame()
    new_live_predictions = (
        target_live_predictions if target_published_now else pd.DataFrame()
    )
    source_files.extend(target_inputs)
    source_files.extend(
        (
            target_publication.manifest_path,
            target_publication.receipt_path,
            target_publication.outcome_path,
        )
    )
    stage_timings["target_authority_seconds"] = time.perf_counter() - stage_started
    stage_started = time.perf_counter()

    loop_native_materialization = None
    loop_native_generation = (
        earlier_shadow_model.generation
        if earlier_shadow_model is not None
        else None
    )
    loop_native_stage_error = ""
    loop_native_worker: Mapping[str, object] | None = None
    if loop_native_scope:
        try:
            loop_native_materialization = (
                read_current_loop_native_schwab_materialization(
                    root,
                    load_samples=False,
                )
            )
        except Exception as exc:
            latest_pointer = (
                root
                / "ml"
                / "option-pricing-loop-native-materialization-latest"
                / "run.json"
            )
            if latest_pointer.exists():
                loop_native_stage_error = (
                    "Existing Loop-native materialization failed verification: "
                    f"{type(exc).__name__}: {exc}"
                )
        try:
            loop_native_worker = launch_loop_native_worker(
                root,
                trainer_cutoff=target_publication.published_at,
            )
        except Exception as exc:
            loop_native_stage_error = (
                loop_native_stage_error + "; " if loop_native_stage_error else ""
            ) + f"Worker launch failed: {type(exc).__name__}: {exc}"

    (
        prior_samples,
        prior_predictions,
        prior_evaluations,
        prior_lineage_files,
        prior_generation_published_at,
    ) = _recover_prior_generation(root)
    prior_samples = _evidence_lane(
        prior_samples,
        provider="schwab",
        prediction_mode="LIVE",
    )
    prior_predictions = _evidence_lane(
        prior_predictions,
        provider="schwab",
        prediction_mode="LIVE",
    )
    prior_proven_live = receipt_proven_prediction_rows(root)
    target_history = authoritative_target_outcomes(
        root,
        published_after=prior_generation_published_at,
    )
    target_history_samples = _concat_frames(
        *(publication.samples() for publication in target_history)
    )
    for publication in target_history:
        source_files.extend((publication.manifest_path, publication.receipt_path))
    source_files.extend(prior_lineage_files)
    route_errors: dict[str, str] = {}
    if loop_native_stage_error:
        route_errors["loop-native/model-update"] = loop_native_stage_error
    if loop_native_materialization is not None:
        if loop_native_materialization.directory is not None:
            source_files.extend(
                (
                    loop_native_materialization.directory / "manifest.json",
                    loop_native_materialization.directory / "receipt.json",
                    loop_native_materialization.directory
                    / "materialization-report.json",
                )
            )
    if loop_native_generation is not None:
        source_files.extend(
            (
                loop_native_generation.directory / "manifest.json",
                loop_native_generation.directory / "receipt.json",
            )
        )
    models: dict[tuple[str, str], object] = {}
    model_reports: dict[str, dict[str, object]] = {}
    models_trained = models_reused = 0
    global_partitions: PricingPartitions | None = None
    sealed_lockbox_inventory: dict[str, object] = {}
    candidate = read_current_candidate(root)
    frozen_offline_predictions = pd.DataFrame()
    frozen_offline_evaluations = pd.DataFrame()
    if candidate is not None:
        models = load_candidate_models(
            root,
            candidate=candidate,
            required_routes=eligibility_policy.required_routes,
        )
        models_reused = len(models)
        candidate_run = (root / str(candidate["pricing_run_path"])).resolve()
        opra_history = _evidence_lane(
            pd.read_parquet(candidate_run / "pricing-samples.parquet"),
            provider="databento-opra",
            prediction_mode="OFFLINE",
        )
        frozen_offline_predictions = _evidence_lane(
            pd.read_parquet(candidate_run / "pricing-predictions.parquet"),
            provider="databento-opra",
            prediction_mode="OFFLINE",
        )
        frozen_offline_evaluations = _evidence_lane(
            pd.read_parquet(candidate_run / "pricing-evaluations.parquet"),
            provider="databento-opra",
            prediction_mode="OFFLINE",
        )
        candidate_report = json.loads(
            (candidate_run / OPTION_PRICING_REPORT_NAME).read_text(encoding="utf-8")
        )
        raw_reports = candidate_report.get("model_reports", {})
        model_reports = {
            str(name): dict(value)
            for name, value in raw_reports.items()
            if isinstance(value, Mapping)
        } if isinstance(raw_reports, Mapping) else {}
        sealed_lockbox_inventory = dict(candidate.get("closed_lockbox", {}))
        closed_lockbox_report = _redacted_lockbox_inventory(
            sealed_lockbox_inventory
        )
        candidate_directory = root / "ml" / "option-pricing-candidates" / str(
            candidate["candidate_id"]
        )
        source_files.extend(
            (
                candidate_directory / "candidate.json",
                candidate_directory / "receipt.json",
                candidate_run / "manifest.json",
                candidate_run / "publication.json",
            )
        )
    else:
        opra = materialize_committed_opra_history_v2(
            root,
            symbols=eligibility_policy.required_symbols,
            rate_observations=rate_observations,
            closed_lockbox_clusters=effective_partitions.lockbox_clusters,
            eligibility_policy_hash=policy_artifact.policy_hash,
            contract_policy=effective_contract,
        )
        opra_history = opra.samples
        sealed_lockbox_inventory = _closed_lockbox_inventory_report(
            opra.closed_lockbox
        )
        closed_lockbox_report = _redacted_lockbox_inventory(
            sealed_lockbox_inventory
        )
        route_errors.update(
            {f"opra/{route}": error for route, error in opra.errors.items()}
        )
        source_files.extend(opra.source_files)
        model_input_files.extend(opra.source_files)
        if not opra_history.empty:
            try:
                global_partitions = partition_pricing_samples_v2(
                    opra_history,
                    closed_lockbox=opra.closed_lockbox,
                    config=effective_partitions,
                )
            except Exception as exc:
                route_errors["global-partitions"] = f"{type(exc).__name__}: {exc}"
    if global_partitions is not None:
        for symbol, call_put in eligibility_policy.required_routes:
            route_name = f"{symbol}/{call_put.lower()}"
            try:
                partitions = route_partitions(
                    global_partitions,
                    symbol=symbol,
                    call_put=call_put,
                    config=effective_partitions,
                )
                if len(partitions.train) > limits.maximum_model_rows_per_route:
                    raise RuntimeError(
                        "route training rows exceed the predeclared runtime bound: "
                        f"{len(partitions.train)} > {limits.maximum_model_rows_per_route}"
                    )
                model = fit_or_reuse_pricing_model(
                    root,
                    symbol=symbol,
                    call_put=call_put,
                    partitions=partitions,
                    input_files=tuple(dict.fromkeys(model_input_files)),
                    trained_at=created,
                    model_policy=effective_model,
                    partition_config=effective_partitions,
                    projection_policy=effective_projection,
                )
                models[(symbol, call_put)] = model
                models_trained += int(not model.reused)
                models_reused += int(model.reused)
                model_reports[route_name] = _model_report(model, partitions)
            except Exception as exc:
                route_errors[route_name] = f"{type(exc).__name__}: {exc}"
                model_reports[route_name] = {
                    "status": "MODEL_NOT_FIT",
                    "reason": route_errors[route_name],
                    "black_scholes_baseline_available_when_inputs_valid": True,
                    "automated_action_allowed": False,
                }
    for symbol, call_put in eligibility_policy.required_routes:
        route_name = f"{symbol}/{call_put.lower()}"
        if route_name not in model_reports:
            reason = "required route has no frozen or fitted real OPRA model"
            model_reports[route_name] = {
                "status": "MODEL_NOT_FIT",
                "reason": reason,
                "source_provider": "databento-opra",
                "evidence_kind": "REAL_RECEIPT_PROVEN",
                "black_scholes_baseline_available_when_inputs_valid": True,
                "automated_action_allowed": False,
            }

    combined_samples = _canonical_samples(
        opra_history,
        prior_samples,
        target_history_samples,
    )

    for symbol, status in live_status.items():
        if status.get("status") == "TARGET_BAR_NOT_READY":
            route_errors[f"{symbol}/live"] = str(status.get("reason", ""))
    offline_predictions = (
        frozen_offline_predictions
        if candidate is not None
        else _assessment_predictions(
            global_partitions,
            models=models,
            projection_policy=effective_projection,
        )
    )
    predictions = canonicalize_predictions(
        _concat_frames(
            prior_predictions,
            prior_proven_live,
            offline_predictions,
            new_live_predictions,
        )
    )

    snapshots_by_symbol = {
        symbol: committed_option_snapshots(root, symbol=symbol)
        for symbol in clean_symbols
    }
    live_for_reconciliation = canonicalize_predictions(
        _concat_frames(prior_proven_live, new_live_predictions)
    )
    live_evaluations = (
        reconcile_predictions(
            live_for_reconciliation,
            snapshots_by_symbol=snapshots_by_symbol,
            evaluated_at=created,
        )
        if not live_for_reconciliation.empty
        else pd.DataFrame()
    )
    offline_evaluations = (
        frozen_offline_evaluations
        if candidate is not None
        else evaluate_offline_predictions(
            predictions,
            opra_history,
            evaluated_at=created,
        )
        if not predictions.empty and not opra_history.empty
        else pd.DataFrame()
    )
    evaluations = _concat_frames(offline_evaluations, live_evaluations)
    current_target_evaluation_rows = _target_row_count(evaluations, target=target)
    new_prospective_evaluation_rows = _new_prospective_evaluation_count(
        prior_evaluations,
        evaluations,
    )
    samples_for_publication = _canonical_samples(combined_samples, new_live_samples)
    samples_for_publication = _redact_closed_lockbox(
        samples_for_publication,
        global_partitions,
    )
    generation_prepared_at = max(utc_timestamp(clock()), target_publication.published_at)
    surfaces = build_pricing_surfaces(
        predictions,
        evaluations,
        available_at=generation_prepared_at,
    )
    preliminary_report = {
        "policy": OPTION_PRICING_POLICY_VERSION,
        "models_trained": models_trained,
        "models_reused": models_reused,
        "model_reports": model_reports,
        "route_errors": route_errors,
        "live_routes": live_status,
    }
    gate = build_gate_report(
        evaluations=evaluations,
        predictions=predictions,
        model_reports=preliminary_report,
        lineage_verified=False,
    )
    monitoring = build_monitoring_rows(
        report=gate,
        predictions=predictions,
        evaluations=evaluations,
        monitored_at=generation_prepared_at,
        live_routes=live_status,
        live_samples=new_live_samples,
    )
    new_live_prediction_count = len(new_live_predictions)
    live_route_states = {
        symbol: str(status.get("status", "UNKNOWN"))
        for symbol, status in live_status.items()
    }
    if new_live_prediction_count:
        cycle_status = "PREDICTIONS_CREATED"
    elif live_route_states and all(
        status == "TARGET_ALREADY_OBSERVED" for status in live_route_states.values()
    ):
        cycle_status = "WAITING_FOR_UNOBSERVED_TARGET"
    elif any(status == "NO_ELIGIBLE_CONTRACTS" for status in live_route_states.values()):
        cycle_status = "CAUSAL_INPUTS_EXCLUDED"
    else:
        cycle_status = "TARGET_INPUT_UNAVAILABLE"
    reports_payload = {
        **preliminary_report,
        "runtime_scope": {
            "live_symbols": list(clean_symbols),
            "live_symbol_count": len(clean_symbols),
            "source": "configured-watchlist-or-explicit-symbols",
            "black_scholes_baseline_symbols": list(clean_symbols),
            "bsgp_eligibility_pilot_symbols": list(
                eligibility_policy.required_symbols
            ),
        },
        "cycle": {
            "status": cycle_status,
            "cycle_mode": final_target_decision.cycle_mode,
            "target_state": final_target_decision.target_state.value,
            "reason": final_target_decision.reason,
            "target_snapshot_for": target.isoformat(),
            "bar_ready_at": (
                _bar_ready_at(target_publication)
            ),
            "pricing_started_at": created.isoformat(),
            "target_authority_published_at": target_publication.published_at.isoformat(),
            "target_outcome_status": target_publication.terminal_status,
            "target_outcome_run_path": target_publication.directory.relative_to(root).as_posix(),
            "target_outcome_receipt_checksum_sha256": (
                target_publication.receipt_checksum_sha256
            ),
            "new_live_sample_rows": len(new_live_samples),
            "new_live_prediction_rows": new_live_prediction_count,
            "current_target_sample_rows": len(target_live_samples),
            "current_target_prediction_rows": len(target_live_predictions),
            "current_target_evaluation_rows": current_target_evaluation_rows,
            "new_prospective_prediction_rows": new_live_prediction_count,
            "new_prospective_evaluation_rows": new_prospective_evaluation_rows,
            "cumulative_sample_rows": len(samples_for_publication),
            "cumulative_prediction_rows": len(predictions),
            "cumulative_evaluation_rows": len(evaluations),
            "route_statuses": live_route_states,
        },
        "black_scholes_baseline": {
            "status": "READY_WHEN_CAUSAL_INPUTS_AVAILABLE",
            "requires_fitted_residual_model": False,
            "new_predictions_created": new_live_prediction_count,
        },
        "loop_native_bsgp_shadow": {
            "scope_active": loop_native_scope,
            "loaded_before_fast_publication_status": (
                earlier_shadow_model.status
                if earlier_shadow_model is not None
                else "OUTSIDE_TEN_SYMBOL_SCOPE"
            ),
            "loaded_generation": (
                earlier_shadow_model.generation.receipt.get("run_path")
                if earlier_shadow_model is not None
                and earlier_shadow_model.generation is not None
                else None
            ),
            "target_sidecar_rows": len(target_publication.shadow_predictions()),
            "post_publication_materialization": (
                loop_native_materialization.directory.relative_to(root).as_posix()
                if loop_native_materialization is not None
                and loop_native_materialization.directory is not None
                else None
            ),
            "current_verified_generation": (
                loop_native_generation.receipt.get("run_path")
                if loop_native_generation is not None
                else None
            ),
            "post_publication_worker": (
                dict(loop_native_worker)
                if loop_native_worker is not None
                else None
            ),
            "update_error": loop_native_stage_error or None,
            "paid_opra_required": False,
            "external_provider_requests": 0,
            "automated_action_allowed": False,
            "eligibility_policy": (
                {
                    "path": loop_native_policy_artifact.receipt.get("run_path"),
                    "policy_hash_sha256": loop_native_policy_artifact.policy_hash,
                }
                if loop_native_policy_artifact is not None
                else None
            ),
        },
        "gate": gate,
        "closed_lockbox": _closed_lockbox_report(global_partitions),
        "closed_lockbox_inventory": closed_lockbox_report,
        "eligibility_policy": {
            "policy_hash": policy_artifact.policy_hash,
            "path": policy_artifact.directory.relative_to(root).as_posix(),
        },
        "frozen_candidate_id": candidate.get("candidate_id") if candidate else None,
        "paid_opra_download_performed_by_runtime": False,
        "automated_action_allowed": False,
    }

    elapsed_before_publication = time.perf_counter() - cycle_started
    peak_memory_bytes = tracemalloc.get_traced_memory()[1]
    enforce_runtime_limits(
        samples=samples_for_publication,
        predictions=predictions,
        evaluations=evaluations,
        surfaces=surfaces,
        elapsed_seconds=elapsed_before_publication,
        peak_memory_bytes=peak_memory_bytes,
        limits=limits,
    )
    benchmark = {
        "elapsed_seconds": elapsed_before_publication,
        "peak_memory_bytes": peak_memory_bytes,
        "sample_rows": len(samples_for_publication),
        "prediction_rows": len(predictions),
        "evaluation_rows": len(evaluations),
        "surface_rows": len(surfaces),
        "limits": asdict(limits),
    }

    stage_timings["research_and_generation_prepare_seconds"] = (
        time.perf_counter() - stage_started
    )
    stage_started = time.perf_counter()
    run_directory, published_at, lineage = _write_and_publish_generation(
        root,
        created=created,
        runtime_clock=clock,
        samples=samples_for_publication,
        predictions=predictions,
        evaluations=evaluations,
        surfaces=surfaces,
        monitoring=monitoring,
        reports=reports_payload,
        models=models,
        input_files=tuple(dict.fromkeys(source_files)),
        partition_config=effective_partitions,
        model_policy=effective_model,
        contract_policy=effective_contract,
        projection_policy=effective_projection,
        policy_artifact=policy_artifact,
        runtime_benchmark=benchmark,
        sealed_lockbox_inventory=sealed_lockbox_inventory,
        runtime_symbols=clean_symbols,
    )
    stage_timings["generation_publication_and_lineage_seconds"] = (
        time.perf_counter() - stage_started
    )
    stage_started = time.perf_counter()
    strategy_report = _read_optional_evidence(
        lambda: read_current_strategy_outcome_evidence(root),
        label="Strategy outcome evidence",
        route_errors=route_errors,
    )
    operational_report = _read_optional_evidence(
        lambda: read_current_operational_readiness(root),
        label="operational readiness",
        route_errors=route_errors,
    )
    lockbox_result = None
    if candidate is not None:
        lockbox_result = _read_optional_evidence(
            lambda: read_lockbox_result(
                root, candidate_id=str(candidate["candidate_id"])
            ),
            label="closed lockbox result",
            route_errors=route_errors,
        )
    final_report = build_eligibility_report(
        policy_artifact=policy_artifact,
        policy=eligibility_policy,
        evaluations=evaluations,
        predictions=predictions,
        model_reports=preliminary_report,
        lineage_report=lineage,
        strategy_report=strategy_report,
        frozen_candidate=candidate,
        lockbox_result=lockbox_result,
        operational_report=operational_report,
        generated_at=utc_timestamp(clock()),
    )
    final_report["closed_lockbox_inventory"] = closed_lockbox_report
    eligibility_artifact = publish_eligibility_report(
        root,
        report=final_report,
        pricing_run=run_directory,
        published_at=utc_timestamp(clock()),
    )
    if loop_native_policy_artifact is not None:
        capture_lineage = verify_loop_native_capture_lineage(
            policy_artifact=loop_native_policy_artifact,
            target_publication=target_publication,
            materialization=loop_native_materialization,
            model_load=earlier_shadow_model,
        )
        capture_lineage_verified = capture_lineage.get("status") == "PASS"
        loop_native_report = build_loop_native_eligibility_report(
            policy_artifact=loop_native_policy_artifact,
            materialization_report=(
                loop_native_materialization.report
                if loop_native_materialization is not None
                else None
            ),
            model_manifest=(
                loop_native_generation.manifest
                if loop_native_generation is not None
                else None
            ),
            operational_report=operational_report,
            strategy_report=strategy_report,
            generated_at=utc_timestamp(clock()),
            capture_lineage_verified=capture_lineage_verified,
        )
        loop_native_report["capture_lineage"] = dict(capture_lineage)
        publish_loop_native_eligibility_report(
            root,
            report=loop_native_report,
            published_at=utc_timestamp(clock()),
        )
    previous_health = _read_optional_evidence(
        lambda: read_current_runtime_health(root),
        label="prior runtime health",
        route_errors=route_errors,
    )
    final_capacity = capacity_report(root, limits=limits)
    stage_timings["post_publication_tail_seconds"] = time.perf_counter() - stage_started
    health = dict(build_runtime_health(
        pricing_run=run_directory,
        eligibility_report=final_report,
        lineage_report=lineage,
        route_errors=route_errors,
        live_routes=live_status,
        live_symbols=clean_symbols,
        elapsed_seconds=time.perf_counter() - cycle_started,
        peak_memory_bytes=peak_memory_bytes,
        capacity=final_capacity,
        checked_at=utc_timestamp(clock()),
        previous_prospective_count=(
            int(previous_health.get("prospective_completed_count", 0))
            if isinstance(previous_health, Mapping)
            else None
        ),
        previous_prospective_checked_at=(
            previous_health.get("prospective_last_increase_at")
            if isinstance(previous_health, Mapping)
            else None
        ),
        limits=limits,
    ))
    health["stage_timings"] = dict(stage_timings)
    health_path = publish_runtime_health(root, health=health)
    return OptionPricingRuntimeResult(
        run_directory=run_directory,
        sample_rows=len(samples_for_publication),
        prediction_rows=len(predictions),
        evaluation_rows=len(evaluations),
        surface_rows=len(surfaces),
        monitoring_rows=len(monitoring),
        models_trained=models_trained,
        models_reused=models_reused,
        published_at=published_at,
        route_errors=route_errors,
        live_routes=live_status,
        eligibility_report_directory=eligibility_artifact.directory,
        gate_status=str(final_report["gate_status"]),
        health_path=health_path,
        health_status=str(health["status"]),
        health_exit_code=int(health["actionable_exit_code"]),
        target_snapshot_for=target,
        target_outcome_directory=target_publication.directory,
        target_outcome_status=target_publication.terminal_status,
        target_published_at=target_publication.published_at,
        stage_timings=dict(stage_timings),
        cycle_mode=final_target_decision.cycle_mode,
        target_state=final_target_decision.target_state.value,
        reason=final_target_decision.reason,
        next_eligible_cycle=final_target_decision.next_eligible_cycle(
            phase_offset_minutes=phase_offset_minutes
        ),
        current_target_sample_rows=len(target_live_samples),
        current_target_prediction_rows=len(target_live_predictions),
        current_target_evaluation_rows=current_target_evaluation_rows,
        new_prospective_prediction_rows=new_live_prediction_count,
        new_prospective_evaluation_rows=new_prospective_evaluation_rows,
    )


def _publish_fast_target_outcome(
    root: Path,
    *,
    symbols: Sequence[str],
    target_snapshot_for: pd.Timestamp,
    created_at: pd.Timestamp,
    runtime_clock: Callable[[], object],
    bar_readiness_mode: str,
    contract_policy: ContractSelectionPolicy,
    projection_policy: ProjectionPolicy,
    rate_observations: pd.DataFrame | None,
    cycle_decision: CycleTargetDecision,
    bar_readiness_timeout_seconds: float,
    readiness_sleeper: Callable[[float], None],
    monotonic_clock: Callable[[], float],
    loop_native_model_load: LoopNativeModelLoad | None = None,
    loop_native_model_policy: LoopNativeModelPolicy | None = None,
) -> tuple[
    TargetOutcomePublication,
    pd.DataFrame,
    pd.DataFrame,
    dict[str, Mapping[str, object]],
    tuple[Path, ...],
    bool,
    CycleTargetDecision,
]:
    existing = None
    try:
        existing = read_target_outcome(
            root,
            target_snapshot_for=target_snapshot_for,
        )
    except TargetOutcomeError as exc:
        if "No authoritative Pricing outcome exists" not in str(exc) and (
            root / "ml" / "option-pricing-target-latest" / "run.json"
        ).exists():
            raise
    if existing is not None:
        states = {
            str(value.get("status", "UNKNOWN"))
            for value in existing.symbol_outcomes.values()
        }
        reason = _grouped_live_reason(existing.symbol_outcomes)
        if existing.terminal_status == "TARGET_BAR_NOT_READY":
            final_decision = replace(
                cycle_decision,
                observed_at=max(cycle_decision.observed_at, existing.published_at),
            ).with_runtime_state(
                readiness_available=False,
                deadline_at=existing.published_at,
                reason=reason,
            )
        elif states == {"TARGET_ALREADY_OBSERVED"}:
            final_decision = cycle_decision.with_runtime_state(
                target_observed=True,
                reason=reason,
            )
        else:
            final_decision = cycle_decision.with_runtime_state(
                readiness_available=True,
                reason=reason or "Existing immutable Pricing target authority verified.",
            )
        return (
            existing,
            existing.samples(),
            existing.predictions(),
            {
                symbol: dict(value)
                for symbol, value in existing.symbol_outcomes.items()
            },
            (existing.manifest_path, existing.receipt_path, existing.outcome_path),
            False,
            final_decision,
        )

    readiness = None
    readiness_error = ""
    prediction_created_at = created_at
    if bar_readiness_mode == "required":
        wait = wait_for_bar_readiness(
            root,
            target_snapshot_for=target_snapshot_for,
            required_symbols=symbols,
            timeout_seconds=bar_readiness_timeout_seconds,
            clock=runtime_clock,
            sleeper=readiness_sleeper,
            monotonic_clock=monotonic_clock,
        )
        prediction_created_at = max(created_at, wait.observed_at)
        readiness = wait.readiness
        readiness_error = wait.detail

    effective_loop_native_model_load = loop_native_model_load
    if (
        loop_native_model_load is not None
        and loop_native_model_load.generation is not None
        and loop_native_model_load.generation.expires_at <= prediction_created_at
    ):
        effective_loop_native_model_load = LoopNativeModelLoad(
            None,
            "BASELINE_FALLBACK_STALE_MODEL",
            "The verified model expired while Pricing waited for exact Loop A readiness.",
        )

    live_samples: list[pd.DataFrame] = []
    live_status: dict[str, Mapping[str, object]] = {}
    source_files: list[Path] = []
    if readiness is not None:
        source_files.extend(readiness.evidence_files)
    if (
        effective_loop_native_model_load is not None
        and effective_loop_native_model_load.generation is not None
    ):
        generation_directory = effective_loop_native_model_load.generation.directory
        source_files.extend(
            (
                generation_directory / LOOP_NATIVE_MODEL_FILE,
                generation_directory / LOOP_NATIVE_MODEL_MANIFEST,
                generation_directory / LOOP_NATIVE_MODEL_RECEIPT,
            )
        )
    for symbol in symbols:
        if bar_readiness_mode == "required" and readiness is None:
            live_status[symbol] = {
                "status": "TARGET_BAR_NOT_READY",
                "reason": readiness_error,
                "target_snapshot_for": target_snapshot_for,
            }
            continue
        try:
            batch = build_live_prediction_inputs(
                root,
                symbol=symbol,
                prediction_created_at=prediction_created_at,
                target_snapshot_for=target_snapshot_for,
                decision_clock=(readiness.decision_clock(symbol) if readiness else None),
                target_underlying_price=(readiness.close(symbol) if readiness else None),
                target_source_files=(readiness.evidence_files if readiness else None),
                contract_policy=contract_policy,
                rate_observations=rate_observations,
                allow_source_chain_carry_fallback=(
                    loop_native_model_load is None
                ),
            )
            live_status[symbol] = {
                "status": batch.status,
                "reason": batch.reason,
                "target_snapshot_for": target_snapshot_for,
            }
            if not batch.samples.empty:
                live_samples.append(batch.samples)
            source_files.extend(batch.source_files)
        except FileNotFoundError as exc:
            live_status[symbol] = {
                "status": "TARGET_BAR_NOT_READY",
                "reason": f"{type(exc).__name__}: {exc}",
                "target_snapshot_for": target_snapshot_for,
            }
        except Exception as exc:
            live_status[symbol] = {
                "status": "PRICING_FAILED",
                "reason": f"{type(exc).__name__}: {exc}",
                "target_snapshot_for": target_snapshot_for,
            }
    samples = (
        pd.concat(live_samples, ignore_index=True, sort=False)
        if live_samples
        else pd.DataFrame()
    )
    predictions = (
        create_prediction_rows(
            samples,
            prediction_created_at=prediction_created_at,
            # Replaced by publish_target_outcome inside the publication path.
            prediction_available_at=prediction_created_at,
            models={},
            projection_policy=projection_policy,
        )
        if not samples.empty
        else pd.DataFrame()
    )
    shadow_predictions: pd.DataFrame | None = None
    if effective_loop_native_model_load is not None:
        try:
            shadow_predictions = create_bsgp_shadow_rows(
                samples,
                predictions,
                prediction_created_at=prediction_created_at,
                prediction_available_at=prediction_created_at,
                model_load=effective_loop_native_model_load,
                model_policy=loop_native_model_policy,
                projection_policy=projection_policy,
            )
        except Exception as exc:
            shadow_predictions = create_bsgp_shadow_rows(
                samples,
                predictions,
                prediction_created_at=prediction_created_at,
                prediction_available_at=prediction_created_at,
                model_load=LoopNativeModelLoad(
                    None,
                    "BASELINE_FALLBACK_NO_MODEL",
                    f"Shadow inference failed closed: {type(exc).__name__}: {exc}",
                ),
                model_policy=loop_native_model_policy,
                projection_policy=projection_policy,
            )
    states = {str(value.get("status", "UNKNOWN")) for value in live_status.values()}
    if not predictions.empty and states == {"READY"}:
        terminal_status = "PREDICTIONS_PUBLISHED"
    elif not predictions.empty:
        terminal_status = "MIXED_TERMINAL"
    elif len(states) == 1:
        terminal_status = next(iter(states))
    else:
        terminal_status = "MIXED_TERMINAL"
    readiness_reference = (
        {
            "run_path": readiness.directory.relative_to(root).as_posix(),
            "receipt_checksum_sha256": readiness.receipt_checksum_sha256,
            "ready_at": readiness.ready_at.isoformat(),
            "loop_a_generation": readiness.loop_a_generation,
        }
        if readiness is not None
        else None
    )
    grouped_reason = _grouped_live_reason(live_status)
    if terminal_status == "TARGET_BAR_NOT_READY":
        final_decision = replace(
            cycle_decision,
            observed_at=prediction_created_at,
        ).with_runtime_state(
            readiness_available=False,
            deadline_at=prediction_created_at,
            reason=grouped_reason or readiness_error,
        )
    elif states == {"TARGET_ALREADY_OBSERVED"}:
        final_decision = cycle_decision.with_runtime_state(
            target_observed=True,
            reason=grouped_reason,
        )
    else:
        final_decision = cycle_decision.with_runtime_state(
            readiness_available=readiness is not None,
            reason=grouped_reason or cycle_decision.reason,
        ) if bar_readiness_mode == "required" else cycle_decision
    publication = publish_target_outcome(
        root,
        target_snapshot_for=target_snapshot_for,
        created_at=prediction_created_at,
        symbols=symbols,
        symbol_outcomes=live_status,
        terminal_status=terminal_status,
        samples=samples,
        predictions=predictions,
        shadow_predictions=shadow_predictions,
        bar_readiness=readiness_reference,
        input_files=tuple(dict.fromkeys(source_files)),
        clock=runtime_clock,
    )
    # The immutable publication wins over any recomputation after a restart.
    authoritative_samples = publication.samples()
    authoritative_predictions = publication.predictions()
    authoritative_status = {
        symbol: dict(value)
        for symbol, value in publication.symbol_outcomes.items()
    }
    return (
        publication,
        authoritative_samples,
        authoritative_predictions,
        authoritative_status,
        tuple(dict.fromkeys(source_files)),
        True,
        final_decision,
    )


def _bar_ready_at(publication: TargetOutcomePublication) -> object | None:
    try:
        payload = json.loads(publication.outcome_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    readiness = payload.get("bar_readiness") if isinstance(payload, Mapping) else None
    return readiness.get("ready_at") if isinstance(readiness, Mapping) else None


def _idle_pricing_result(
    root: Path,
    *,
    decision: CycleTargetDecision,
    phase_offset_minutes: int,
) -> OptionPricingRuntimeResult:
    """Return a write-free monitor-only heartbeat using carried inventory only."""

    run_directory = None
    published_at = None
    counts = {
        "samples": 0,
        "predictions": 0,
        "evaluations": 0,
        "surfaces": 0,
        "monitoring": 0,
    }
    gate_status = "NOT_PRODUCTION_ELIGIBLE"
    try:
        publication = read_current_option_pricing_publication(root)
    except Exception:
        if pricing_pointer_path(root).exists():
            raise
    else:
        run_directory = publication.run_directory
        published_at = utc_timestamp(publication.receipt.get("published_at"))
        names = {
            "samples": "pricing-samples.parquet",
            "predictions": "pricing-predictions.parquet",
            "evaluations": "pricing-evaluations.parquet",
            "surfaces": "pricing-surfaces.parquet",
            "monitoring": "pricing-monitoring.parquet",
        }
        counts = {
            key: int(pq.ParquetFile(publication.run_directory / name).metadata.num_rows)
            for key, name in names.items()
        }
        try:
            report = json.loads(
                (publication.run_directory / OPTION_PRICING_REPORT_NAME).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            report = {}
        gate = report.get("gate") if isinstance(report, Mapping) else None
        if isinstance(gate, Mapping):
            gate_status = str(gate.get("gate_status", gate_status))

    health_path = root / "ml" / "option-pricing-health" / "latest.json"
    previous_health = read_current_runtime_health(root)
    health_status = (
        str(previous_health.get("status", "NOT_EVALUATED"))
        if isinstance(previous_health, Mapping)
        else "NOT_EVALUATED"
    )
    health_exit_code = (
        int(previous_health.get("actionable_exit_code", 0))
        if isinstance(previous_health, Mapping)
        else 0
    )
    idle_route_errors: dict[str, str] = {}
    idle_capacity = capacity_report(root)
    if idle_capacity.get("status") != "PASS":
        health_status = "FAIL"
        health_exit_code = EXIT_EVIDENCE
        idle_route_errors["capacity"] = json.dumps(idle_capacity, sort_keys=True)
    return OptionPricingRuntimeResult(
        run_directory=run_directory,
        sample_rows=counts["samples"],
        prediction_rows=counts["predictions"],
        evaluation_rows=counts["evaluations"],
        surface_rows=counts["surfaces"],
        monitoring_rows=counts["monitoring"],
        models_trained=0,
        models_reused=0,
        published_at=published_at,
        route_errors=idle_route_errors,
        live_routes={},
        eligibility_report_directory=None,
        gate_status=gate_status,
        health_path=health_path if health_path.is_file() else None,
        health_status=health_status,
        health_exit_code=health_exit_code,
        target_snapshot_for=None,
        target_outcome_directory=None,
        target_outcome_status="NOT_APPLICABLE",
        target_published_at=None,
        stage_timings={},
        cycle_mode=decision.cycle_mode,
        target_state=decision.target_state.value,
        reason=decision.reason,
        next_eligible_cycle=decision.next_eligible_cycle(
            phase_offset_minutes=phase_offset_minutes
        ),
    )


def _grouped_live_reason(statuses: Mapping[str, Mapping[str, object]]) -> str:
    grouped: dict[tuple[str, str], list[str]] = {}
    for symbol, value in statuses.items():
        key = (
            str(value.get("status", "UNKNOWN")),
            str(value.get("reason", "")).strip(),
        )
        grouped.setdefault(key, []).append(symbol)
    return " | ".join(
        f"{status}: {reason or 'no detail'} (count={len(symbols)})"
        for (status, reason), symbols in grouped.items()
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run independent shadow-only Black-Scholes/RBF finite-feature GP "
            "option pricing before the Options snapshot phase."
        )
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default="pc",
    )
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST)
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=None,
        help="Explicit live Pricing symbols; overrides --watchlist.",
    )
    parser.add_argument("--interval-minutes", type=int, default=15)
    parser.add_argument("--phase-offset-minutes", type=int, default=1)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--bar-readiness-mode",
        choices=("required", "exact"),
        default="required",
        help=(
            "Required consumes Loop A's atomic all-symbol receipt; exact is the "
            "standalone compatibility mode and still rejects stale targets."
        ),
    )
    parser.add_argument(
        "--bar-readiness-timeout-seconds",
        type=float,
        default=45.0,
        help=(
            "Monotonic bounded wait for exact Loop A readiness before publishing "
            "the immutable noncreditable terminal outcome."
        ),
    )
    parser.add_argument(
        "--per-symbol-detail",
        action="store_true",
        help="Print per-symbol live-route detail in addition to grouped diagnostics.",
    )
    args = parser.parse_args(argv)
    if args.interval_minutes < 1:
        parser.error("--interval-minutes must be at least 1")
    if not 0 <= args.phase_offset_minutes < args.interval_minutes:
        parser.error("--phase-offset-minutes must satisfy 0 <= phase < interval-minutes")
    if args.bar_readiness_timeout_seconds < 0:
        parser.error("--bar-readiness-timeout-seconds cannot be negative")
    try:
        configured_symbols = resolve_pricing_symbols(
            symbols=args.symbols,
            watchlist=args.watchlist,
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    print("DUCKETS OPTION PRICING RUNTIME")
    print("==============================")
    print(f"DATASTORE: {root}")
    print(f"Live symbols: {', '.join(configured_symbols)}")
    print(
        "Scope: Black-Scholes uses every live symbol; BSGP eligibility remains "
        f"the {', '.join(REQUIRED_SYMBOLS)} pilot"
    )
    print("Authority: ml/option-pricing-latest/run.json")
    print("Mode: shadow only; automated_action_allowed=false")
    print("Timing: completed quarter-hour bar -> Pricing receipt -> independent Options fetch")
    print("A missing completed target bar is skipped; predictions are never backdated.")
    print("Stop: Ctrl+C")
    print()
    lock = root / ".ducketz-option-pricing-runtime.lock"
    with exclusive_runtime_lock(lock, process_name="Duckets Option Pricing runtime"):
        previous_boundary: datetime | None = None
        try:
            while True:
                cycle_anchor = datetime.now(timezone.utc)
                if not args.once:
                    boundary = next_boundary(
                        cycle_anchor,
                        interval_minutes=args.interval_minutes,
                        phase_offset_minutes=args.phase_offset_minutes,
                    )
                    if previous_boundary is not None:
                        for missed_boundary in _missed_boundaries(
                            previous_boundary,
                            boundary,
                            interval_minutes=args.interval_minutes,
                        ):
                            missed_decision = cycle_target_decision(missed_boundary)
                            if not missed_decision.actionable:
                                continue
                            missed = _publish_missed_target_outcome(
                                root,
                                symbols=configured_symbols,
                                target_snapshot_for=missed_decision.target_snapshot_for,
                                detected_at=cycle_anchor,
                            )
                            if missed is None:
                                continue
                            print(
                                "Pricing boundary missed: "
                                f"scheduled_at={missed_boundary.isoformat()}; "
                                f"target={missed.target_snapshot_for.isoformat()}; "
                                f"outcome={missed.terminal_status}; "
                                f"published_at={missed.published_at.isoformat()}"
                            )
                    print(f"Next Pricing cycle: {boundary.isoformat()}")
                    time.sleep(max(0.0, (boundary - datetime.now(timezone.utc)).total_seconds()))
                    cycle_anchor = boundary
                try:
                    starting_decision = cycle_target_decision(cycle_anchor)
                    if (
                        starting_decision.actionable
                        and args.bar_readiness_mode == "required"
                    ):
                        print(
                            "Pricing target coordination: "
                            "cycle_mode=ACTIONABLE; "
                            "target_state=WAITING_FOR_LOOP_A_READINESS; "
                            f"target={starting_decision.target_snapshot_for.isoformat()}; "
                            f"deadline_seconds={args.bar_readiness_timeout_seconds:g}"
                        )
                    result = run_option_pricing_once(
                        root,
                        symbols=configured_symbols,
                        target_snapshot_for=expected_quarter_hour_target(cycle_anchor),
                        bar_readiness_mode=args.bar_readiness_mode,
                        bar_readiness_timeout_seconds=(
                            args.bar_readiness_timeout_seconds
                        ),
                        phase_offset_minutes=args.phase_offset_minutes,
                    )
                    report_pricing_result(
                        result,
                        reporter=print,
                        per_symbol_detail=args.per_symbol_detail,
                    )
                    exit_code = result.health_exit_code
                except Exception as exc:
                    print(f"Pricing failed: {type(exc).__name__}: {exc}")
                    exit_code = 1
                previous_boundary = cycle_anchor
                if args.once:
                    return exit_code
        except KeyboardInterrupt:
            print("Option Pricing runtime stopped.")
            return 0


def report_pricing_result(
    result: OptionPricingRuntimeResult,
    *,
    reporter: Callable[[str], None] = print,
    per_symbol_detail: bool = False,
) -> None:
    target = (
        result.target_snapshot_for.isoformat()
        if result.target_snapshot_for is not None
        else "NONE"
    )
    next_cycle = (
        result.next_eligible_cycle.isoformat()
        if result.next_eligible_cycle is not None
        else "UNKNOWN"
    )
    reporter(
        "Pricing cycle: "
        f"cycle_mode={result.cycle_mode}; target_state={result.target_state}; "
        f"target={target}; reason={result.reason}; "
        f"next_eligible_cycle={next_cycle}"
    )
    reporter(
        "Pricing authority: "
        f"terminal_outcome={result.target_outcome_status}; "
        f"published_at={result.target_published_at.isoformat() if result.target_published_at is not None else 'NONE'}; "
        f"run={result.target_outcome_directory or 'NONE'}"
    )
    reporter(
        "Pricing rows: "
        f"current_target_samples={result.current_target_sample_rows}; "
        f"current_target_predictions={result.current_target_prediction_rows}; "
        f"current_target_evaluations={result.current_target_evaluation_rows}; "
        f"new_prospective_predictions={result.new_prospective_prediction_rows}; "
        f"new_prospective_evaluations={result.new_prospective_evaluation_rows}; "
        f"cumulative_samples={result.sample_rows}; "
        f"cumulative_predictions={result.prediction_rows}; "
        f"cumulative_evaluations={result.evaluation_rows}; "
        f"carried_samples={max(0, result.sample_rows - result.current_target_sample_rows)}; "
        f"carried_predictions={max(0, result.prediction_rows - result.current_target_prediction_rows)}"
    )
    reporter(
        "Pricing research state: "
        f"gate_status={result.gate_status}; health={result.health_status}; "
        f"health_scope={'LAST_ACTIONABLE_GENERATION' if result.cycle_mode == 'MONITOR_ONLY' else 'CURRENT_ACTIONABLE_GENERATION'}; "
        "automated_action_allowed=false; "
        f"generation_run={result.run_directory or 'UNCHANGED'}"
    )

    grouped: dict[tuple[str, str], list[str]] = {}
    for symbol, route in result.live_routes.items():
        key = (
            str(route.get("status", "UNKNOWN")),
            str(route.get("reason", "")).strip(),
        )
        grouped.setdefault(key, []).append(symbol)
    for (status, reason), symbols in grouped.items():
        reporter(
            "Pricing live routes: "
            f"status={status}; count={len(symbols)}; "
            f"reason={reason or 'NONE'}"
        )
        if per_symbol_detail:
            reporter(f"Pricing live symbols: {', '.join(symbols)}")

    non_live_errors = {
        route: error
        for route, error in result.route_errors.items()
        if not route.endswith("/live")
    }
    errors_grouped: dict[str, list[str]] = {}
    for route, error in non_live_errors.items():
        errors_grouped.setdefault(error, []).append(route)
    for error, routes in errors_grouped.items():
        reporter(
            "Pricing research routes unavailable: "
            f"count={len(routes)}; reason={error}"
        )
        if per_symbol_detail:
            reporter(f"Pricing unavailable routes: {', '.join(routes)}")


def next_boundary(
    now: datetime,
    *,
    interval_minutes: int,
    phase_offset_minutes: int,
) -> datetime:
    if interval_minutes < 1 or not 0 <= phase_offset_minutes < interval_minutes:
        raise ValueError("Invalid Pricing interval/phase")
    current = now.astimezone(timezone.utc)
    midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
    anchor = midnight + timedelta(minutes=phase_offset_minutes)
    if current < anchor:
        return anchor
    count = int((current - anchor).total_seconds() // (interval_minutes * 60))
    return anchor + timedelta(minutes=(count + 1) * interval_minutes)


def _missed_boundaries(
    previous_boundary: datetime,
    next_scheduled_boundary: datetime,
    *,
    interval_minutes: int,
) -> tuple[datetime, ...]:
    interval = timedelta(minutes=interval_minutes)
    candidate = previous_boundary.astimezone(timezone.utc) + interval
    stop = next_scheduled_boundary.astimezone(timezone.utc)
    output: list[datetime] = []
    while candidate < stop:
        output.append(candidate)
        candidate += interval
    return tuple(output)


def _publish_missed_target_outcome(
    root: Path,
    *,
    symbols: Sequence[str],
    target_snapshot_for: object,
    detected_at: object,
) -> TargetOutcomePublication | None:
    target = utc_timestamp(target_snapshot_for)
    target_decision = cycle_target_decision(target)
    if not target_decision.actionable or target_decision.target_snapshot_for != target:
        return None
    detected = max(utc_timestamp(detected_at), target)
    outcomes = {
        symbol: {
            "status": "PRICING_TIMED_OUT",
            "reason": "The prior Pricing cycle was still running at this scheduled boundary.",
            "target_snapshot_for": target,
        }
        for symbol in symbols
    }
    return publish_target_outcome(
        root,
        target_snapshot_for=target,
        created_at=detected,
        symbols=symbols,
        symbol_outcomes=outcomes,
        terminal_status="PRICING_TIMED_OUT",
        samples=pd.DataFrame(),
        predictions=pd.DataFrame(),
        bar_readiness=None,
        clock=lambda: detected,
    )


def resolve_pricing_symbols(
    *,
    symbols: Sequence[str] | None,
    watchlist: Path,
) -> tuple[str, ...]:
    configured = normalize_symbols(
        symbols if symbols is not None else read_watchlist(Path(watchlist))
    )
    if not configured:
        raise ValueError("No Pricing symbols were configured")
    missing_pilot = [symbol for symbol in REQUIRED_SYMBOLS if symbol not in configured]
    if missing_pilot:
        raise ValueError(
            "Pricing live scope must include the BSGP eligibility pilot symbols: "
            + ", ".join(missing_pilot)
        )
    return configured


def _assessment_predictions(
    partitions: PricingPartitions | None,
    *,
    models: Mapping[tuple[str, str], object],
    projection_policy: ProjectionPolicy,
) -> pd.DataFrame:
    if partitions is None or partitions.assessment.empty:
        return pd.DataFrame()
    frames: list[pd.DataFrame] = []
    for target, cluster in partitions.assessment.groupby("target_snapshot_for", sort=True):
        timestamp = utc_timestamp(target)
        frames.append(
            create_prediction_rows(
                cluster,
                prediction_created_at=timestamp,
                prediction_available_at=timestamp,
                models=models,
                projection_policy=projection_policy,
            )
        )
    return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()


def _recover_prior_generation(
    root: Path,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    list[Path],
    pd.Timestamp | None,
]:
    try:
        publication = read_current_option_pricing_publication(root)
    except Exception:
        if (root / "ml" / "option-pricing-latest" / "run.json").exists():
            raise
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), [], None
    samples = pd.read_parquet(publication.run_directory / "pricing-samples.parquet")
    predictions = pd.read_parquet(publication.run_directory / "pricing-predictions.parquet")
    evaluations = pd.read_parquet(publication.run_directory / "pricing-evaluations.parquet")
    return (
        samples.drop(columns="id"),
        predictions.drop(columns="id"),
        evaluations.drop(columns="id"),
        [
            publication.run_directory / "publication.json",
            publication.run_directory / "manifest.json",
        ],
        utc_timestamp(publication.receipt.get("published_at")),
    )


def _target_row_count(frame: pd.DataFrame, *, target: pd.Timestamp) -> int:
    if frame.empty or "target_snapshot_for" not in frame:
        return 0
    values = pd.to_datetime(frame["target_snapshot_for"], utc=True, errors="coerce")
    return int(values.eq(target).sum())


def _new_prospective_evaluation_count(
    previous: pd.DataFrame,
    current: pd.DataFrame,
) -> int:
    keys = (
        "symbol",
        "target_snapshot_for",
        "contract_symbol",
        "prediction_created_at",
    )

    def proven(frame: pd.DataFrame) -> set[tuple[str, ...]]:
        if frame.empty or not set(keys).issubset(frame.columns):
            return set()
        eligible = frame.get(
            "prospective_eligible",
            pd.Series(False, index=frame.index),
        ).fillna(False).astype(bool)
        return {
            tuple(str(row[key]) for key in keys)
            for row in frame.loc[eligible, list(keys)].to_dict("records")
        }

    return len(proven(current).difference(proven(previous)))


def _canonical_samples(*frames: pd.DataFrame) -> pd.DataFrame:
    combined = _concat_frames(*frames)
    if combined.empty:
        return combined
    combined["target_snapshot_for"] = pd.to_datetime(
        combined["target_snapshot_for"], utc=True, errors="coerce"
    )
    combined["source_available_at"] = pd.to_datetime(
        combined["source_available_at"], utc=True, errors="coerce"
    )
    combined["_matured_target"] = combined.get(
        "observed_mid", pd.Series(index=combined.index, dtype=float)
    ).notna()
    return (
        combined.sort_values(
            ["_matured_target", "source_available_at"],
            ascending=(False, True),
            kind="stable",
        )
        .drop_duplicates(["symbol", "target_snapshot_for", "contract_symbol"], keep="first")
        .drop(columns="_matured_target")
        .reset_index(drop=True)
    )


def _evidence_lane(
    frame: pd.DataFrame,
    *,
    provider: str,
    prediction_mode: str,
) -> pd.DataFrame:
    if frame.empty:
        return frame.drop(columns="id", errors="ignore").copy()
    required = {"source_provider", "prediction_mode"}
    if not required.issubset(frame.columns):
        return pd.DataFrame()
    selected = frame.loc[
        frame["source_provider"]
        .astype("string")
        .str.strip()
        .str.lower()
        .eq(provider.lower())
        & frame["prediction_mode"]
        .astype("string")
        .str.strip()
        .str.upper()
        .eq(prediction_mode.upper())
    ]
    return selected.drop(columns="id", errors="ignore").reset_index(drop=True)


def _closed_lockbox_inventory_report(
    inventory: ClosedOpraLockboxInventory,
) -> dict[str, object]:
    return {
        "status": (
            "CLOSED_UNTOUCHED_UNSCORED"
            if inventory.cluster_count
            else "CLOSED_UNTOUCHED_UNSCORED_NOT_YET_AVAILABLE"
        ),
        "target_values_read": inventory.target_values_read,
        "cluster_count": inventory.cluster_count,
        "start": inventory.start.isoformat() if inventory.start is not None else None,
        "end": inventory.end.isoformat() if inventory.end is not None else None,
        "target_snapshot_fors": [
            value.isoformat() for value in inventory.target_snapshot_fors
        ],
        "route_cluster_counts": {
            f"{symbol}/{call_put.lower()}": count
            for (symbol, call_put), count in inventory.route_cluster_counts.items()
        },
        "route_request_symbol_counts": {
            f"{symbol}/{call_put.lower()}": count
            for (symbol, call_put), count in inventory.route_request_symbol_counts.items()
        },
        "output_count": inventory.output_count,
        "outputs": [dict(value) for value in inventory.outputs],
        "automated_action_allowed": False,
    }


def _redacted_lockbox_inventory(
    inventory: Mapping[str, object],
) -> dict[str, object]:
    return {
        "status": inventory.get(
            "status", "CLOSED_UNTOUCHED_UNSCORED_NOT_YET_AVAILABLE"
        ),
        "target_values_read": inventory.get("target_values_read", False),
        "cluster_count": int(inventory.get("cluster_count", 0)),
        "start": inventory.get("start"),
        "end": inventory.get("end"),
        "route_cluster_counts": dict(inventory.get("route_cluster_counts", {})),
        "route_request_symbol_counts": dict(
            inventory.get("route_request_symbol_counts", {})
        ),
        "output_count": int(inventory.get("output_count", 0)),
        "target_snapshot_fors_redacted": True,
        "target_output_paths_redacted": True,
        "automated_action_allowed": False,
    }


def _read_optional_evidence(
    reader: Callable[[], Mapping[str, object] | None],
    *,
    label: str,
    route_errors: dict[str, str],
) -> Mapping[str, object] | None:
    try:
        return reader()
    except Exception as exc:
        route_errors[f"evidence/{label}"] = f"{type(exc).__name__}: {exc}"
        return None


def _redact_closed_lockbox(
    samples: pd.DataFrame,
    partitions: PricingPartitions | None,
) -> pd.DataFrame:
    if samples.empty or partitions is None:
        return samples
    targets = pd.to_datetime(samples["target_snapshot_for"], utc=True, errors="coerce")
    closed = targets.between(partitions.lockbox_start, partitions.lockbox_end)
    return samples.loc[~closed].reset_index(drop=True)


def _closed_lockbox_report(partitions: PricingPartitions | None) -> Mapping[str, object]:
    if partitions is None:
        return {
            "status": "CLOSED_UNTOUCHED_UNSCORED_NOT_YET_AVAILABLE",
            "target_values_in_report": False,
        }
    span_months = (
        (partitions.lockbox_end.year - partitions.first_training_cluster.year) * 12
        + partitions.lockbox_end.month
        - partitions.first_training_cluster.month
    )
    return {
        "status": "CLOSED_UNTOUCHED_UNSCORED",
        "cluster_count": partitions.lockbox_clusters,
        "row_count": partitions.lockbox_rows,
        "start": partitions.lockbox_start.isoformat(),
        "end": partitions.lockbox_end.isoformat(),
        "calendar_span_months": span_months,
        "target_values_in_report": False,
    }


def _model_report(model: object, partitions: PricingPartitions) -> dict[str, object]:
    span_months = (
        (partitions.lockbox_end.year - partitions.first_training_cluster.year) * 12
        + partitions.lockbox_end.month
        - partitions.first_training_cluster.month
    )
    evaluation = dict(model.offline_evaluation)
    return {
        "status": "MODEL_FIT",
        "source_provider": "databento-opra",
        "evidence_kind": "REAL_RECEIPT_PROVEN",
        "evidence_lanes": {
            "train_calibration": "OFFLINE_TRAIN_CALIBRATION",
            "assessment": "UNTOUCHED_OFFLINE_ASSESSMENT",
            "lockbox": "CLOSED_LOCKBOX_METADATA_ONLY",
        },
        "artifact_directory": str(model.artifact_directory),
        "reused": bool(model.reused),
        "assessment_metrics": evaluation,
        "partition_contract": {
            "cluster_counts": {
                "train": partitions.train_clusters,
                "calibration": partitions.calibration_clusters,
                "assessment": partitions.assessment_clusters,
                "lockbox": partitions.lockbox_clusters,
            },
            "lockbox_status": "CLOSED_UNTOUCHED_UNSCORED",
            "lockbox_start": partitions.lockbox_start.isoformat(),
            "lockbox_end": partitions.lockbox_end.isoformat(),
            "calendar_span_months": span_months,
            "lockbox_target_values_reported": False,
        },
        "automated_action_allowed": False,
    }


def _write_and_publish_generation(
    root: Path,
    *,
    created: pd.Timestamp,
    runtime_clock: Callable[[], object],
    samples: pd.DataFrame,
    predictions: pd.DataFrame,
    evaluations: pd.DataFrame,
    surfaces: pd.DataFrame,
    monitoring: pd.DataFrame,
    reports: Mapping[str, object],
    models: Mapping[tuple[str, str], object],
    input_files: Sequence[Path],
    partition_config: PricingPartitionConfig,
    model_policy: BSGPModelPolicy,
    contract_policy: ContractSelectionPolicy,
    projection_policy: ProjectionPolicy,
    policy_artifact: EligibilityPolicyArtifact,
    runtime_benchmark: Mapping[str, object],
    sealed_lockbox_inventory: Mapping[str, object],
    runtime_symbols: Sequence[str],
) -> tuple[Path, pd.Timestamp, Mapping[str, object]]:
    runs_root = root / "ml" / "option-pricing-runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    base = created.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = runs_root / base
    suffix = 2
    while destination.exists():
        destination = runs_root / f"{base}-{suffix}"
        suffix += 1
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-{os.getpid()}-", dir=runs_root))
    timeless_frames = (
        ("pricing-samples.parquet", samples, OPTION_PRICING_SAMPLE_SCHEMA, ("symbol", "target_snapshot_for", "contract_symbol")),
        ("pricing-predictions.parquet", predictions, OPTION_PRICING_PREDICTION_SCHEMA, ("symbol", "target_snapshot_for", "contract_symbol", "prediction_created_at")),
        ("pricing-evaluations.parquet", evaluations, OPTION_PRICING_EVALUATION_SCHEMA, ("symbol", "target_snapshot_for", "contract_symbol", "prediction_created_at")),
    )
    output_names: list[str] = []
    try:
        for name, frame, schema, keys in timeless_frames:
            output = _output_frame(frame, schema=schema, key_columns=keys)
            write_parquet_with_schema(output, staging / name, schema)
            output_names.append(name)
        sealed_inventory_name = "closed-lockbox-inventory.json"
        (staging / sealed_inventory_name).write_text(
            json.dumps(
                dict(sealed_lockbox_inventory),
                indent=2,
                sort_keys=True,
                default=str,
            )
            + "\n",
            encoding="utf-8",
        )
        output_names.append(sealed_inventory_name)
        copied = _copy_model_artifacts(root, staging, models)
        output_names.extend(copied)

        # Expensive immutable inputs are complete before this clock is sampled.
        # Only the compact publication-bound outputs and manifest remain.
        files_completed_at = max(utc_timestamp(runtime_clock()), created)
        published_surfaces = surfaces.copy()
        if not published_surfaces.empty:
            published_surfaces["available_at"] = files_completed_at
        published_monitoring = monitoring.copy()
        if not published_monitoring.empty:
            published_monitoring["monitored_at"] = files_completed_at
        for name, frame, schema, keys in (
            ("pricing-surfaces.parquet", published_surfaces, OPTION_PRICING_SURFACE_SCHEMA, ("symbol", "target_snapshot_for", "call_put", "expiration_bucket", "moneyness_bucket")),
            ("pricing-monitoring.parquet", published_monitoring, OPTION_PRICING_MONITORING_SCHEMA, ("metric_name", "scope_type", "scope_value", "monitored_at")),
        ):
            output = _output_frame(frame, schema=schema, key_columns=keys)
            write_parquet_with_schema(output, staging / name, schema)
            output_names.append(name)
        published_reports = dict(reports)
        published_cycle = published_reports.get("cycle")
        if isinstance(published_cycle, Mapping):
            published_reports["cycle"] = {
                **dict(published_cycle),
                "immutable_files_completed_at": files_completed_at.isoformat(),
            }
        (staging / OPTION_PRICING_REPORT_NAME).write_text(
            json.dumps(published_reports, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        output_names.append(OPTION_PRICING_REPORT_NAME)
        write_manifest(
            staging,
            run_timestamp=created,
            input_files=input_files,
            output_files=tuple(output_names),
            feature_columns=(
                "underlying_price",
                "strike",
                "risk_free_rate",
                "lagged_implied_volatility",
                "target_years_to_expiration",
                "dividend_yield",
            ),
            target_column="normalized_residual",
            configuration={
                "pricing_policy_version": OPTION_PRICING_POLICY_VERSION,
                "partition_config": asdict(partition_config),
                "model_policy": asdict(model_policy),
                "contract_policy": asdict(contract_policy),
                "projection_policy": asdict(projection_policy),
                "eligibility_policy": {
                    "policy_hash": policy_artifact.policy_hash,
                    "path": policy_artifact.directory.relative_to(root).as_posix(),
                    "receipt_checksum_sha256": file_checksum(
                        policy_artifact.directory / "receipt.json"
                    ),
                },
                "runtime_benchmark": dict(runtime_benchmark),
                "runtime_scope": {
                    "live_symbols": list(runtime_symbols),
                    "bsgp_eligibility_pilot_symbols": list(
                        policy_artifact.policy.get("required_symbols", REQUIRED_SYMBOLS)
                    ),
                },
                "immutable_files_completed_at": files_completed_at.isoformat(),
                "publication_contract": {
                    "version": OPTION_PRICING_PUBLICATION_VERSION,
                    "authority": "ml/option-pricing-latest/run.json",
                    "schema_validation": True,
                    "prior_receipt_chain_required": True,
                    "automated_action_allowed": False,
                },
                "runtime_isolation": {
                    "owns_lock": ".ducketz-option-pricing-runtime.lock",
                    "starts_other_runtime": False,
                    "writes_other_authority": False,
                },
            },
            datastore_root=root,
        )
        staging.replace(destination)
    except BaseException:
        # An interrupted staging directory is intentionally left unreachable;
        # it has no receipt and can never become authoritative by discovery.
        raise
    staged_verification = verify_staged_option_pricing_run(
        root,
        run_directory=destination,
        policy_artifact=policy_artifact,
    )
    if not staged_verification.get("verified"):
        raise RuntimeError(
            "Completed Pricing staging verification failed: "
            + json.dumps(staged_verification, sort_keys=True, default=str)
        )
    publication = publish_option_pricing_run(
        root,
        run_directory=destination,
        clock=runtime_clock,
    )
    completed_verification = verify_completed_option_pricing_lineage(
        root,
        run_directory=destination,
        policy_artifact=policy_artifact,
    )
    if not completed_verification.get("verified"):
        previous = publication.receipt.get("previous_publication")
        pointer = pricing_pointer_path(root)
        if isinstance(previous, Mapping):
            _write_runtime_json_atomic(
                pointer,
                {
                    "schema_version": OPTION_PRICING_POINTER_VERSION,
                    "current": dict(previous),
                },
            )
            restored = read_current_option_pricing_publication(root)
            if restored.run_directory == destination:
                raise RuntimeError("Failed to restore prior Pricing authority")
        else:
            pointer.unlink(missing_ok=True)
        raise RuntimeError(
            "Final Pricing publication verification failed; authority was restored: "
            + json.dumps(completed_verification, sort_keys=True, default=str)
        )
    return publication.run_directory, utc_timestamp(
        publication.receipt["published_at"]
    ), completed_verification


def _write_runtime_json_atomic(
    path: Path, payload: Mapping[str, object]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _copy_model_artifacts(
    datastore_root: Path,
    staging: Path,
    models: Mapping[tuple[str, str], object],
) -> list[str]:
    output: list[str] = []
    allowed = (Path(datastore_root) / "ml" / "option-pricing-models").resolve()
    for (symbol, call_put), model in models.items():
        source = Path(model.artifact_directory).resolve()
        if allowed not in source.parents or not source.is_dir():
            raise RuntimeError(
                f"Pricing model artifact escapes its model registry: {source}"
            )
        destination = staging / "model-artifacts" / symbol / call_put.lower() / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        output.extend(
            path.relative_to(staging).as_posix()
            for path in sorted(destination.rglob("*"))
            if path.is_file()
        )
    return output


def _output_frame(
    frame: pd.DataFrame,
    *,
    schema: object,
    key_columns: Sequence[str],
) -> pd.DataFrame:
    if frame.empty:
        return empty_frame(schema)  # type: ignore[arg-type]
    internal_proof = [
        column for column in frame.columns if str(column).startswith("_pricing_")
    ]
    clean = frame.drop(columns=["id", *internal_proof], errors="ignore")
    return frame_with_readable_id(clean, key_columns=key_columns)


def _concat_frames(*frames: pd.DataFrame) -> pd.DataFrame:
    available = [frame.drop(columns="id", errors="ignore") for frame in frames if not frame.empty]
    return pd.concat(available, ignore_index=True, sort=False) if available else pd.DataFrame()


if __name__ == "__main__":
    raise SystemExit(main())
