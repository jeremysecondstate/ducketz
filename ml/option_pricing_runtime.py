from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
import tracemalloc
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

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
    OPTION_PRICING_POLICY_VERSION,
    PricingPartitionConfig,
    ProjectionPolicy,
)
from ml.option_pricing.prediction import create_prediction_rows
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
    run_directory: Path
    sample_rows: int
    prediction_rows: int
    evaluation_rows: int
    surface_rows: int
    monitoring_rows: int
    models_trained: int
    models_reused: int
    published_at: pd.Timestamp
    route_errors: Mapping[str, str]
    live_routes: Mapping[str, Mapping[str, object]]
    eligibility_report_directory: Path
    gate_status: str
    health_path: Path
    health_status: str
    health_exit_code: int


def run_option_pricing_once(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    run_timestamp: object | None = None,
    runtime_clock: Callable[[], object] | None = None,
    partition_config: PricingPartitionConfig | None = None,
    model_policy: BSGPModelPolicy | None = None,
    contract_policy: ContractSelectionPolicy | None = None,
    projection_policy: ProjectionPolicy | None = None,
    rate_observations: pd.DataFrame | None = None,
    runtime_limits: RuntimeLimits | None = None,
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
            contract_policy=contract_policy,
            projection_policy=projection_policy,
            rate_observations=rate_observations,
            runtime_limits=runtime_limits,
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
    contract_policy: ContractSelectionPolicy | None = None,
    projection_policy: ProjectionPolicy | None = None,
    rate_observations: pd.DataFrame | None = None,
    runtime_limits: RuntimeLimits | None = None,
) -> OptionPricingRuntimeResult:
    root = Path(datastore_root).resolve()
    clean_symbols = normalize_symbols(symbols)
    if not clean_symbols:
        raise ValueError("At least one Pricing symbol is required")
    created = utc_timestamp(run_timestamp)
    clock = runtime_clock or (lambda: utc_timestamp())
    cycle_started = time.perf_counter()
    effective_partitions = partition_config or PricingPartitionConfig()
    effective_model = model_policy or BSGPModelPolicy()
    effective_contract = contract_policy or ContractSelectionPolicy()
    effective_projection = projection_policy or ProjectionPolicy()
    limits = runtime_limits or RuntimeLimits()
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
    initial_capacity = capacity_report(root, limits=limits)
    if initial_capacity.get("status") != "PASS":
        raise RuntimeError(
            "Pricing preflight failed closed for disk capacity: "
            + json.dumps(initial_capacity, sort_keys=True)
        )

    prior_samples, prior_predictions, prior_lineage_files = _recover_prior_generation(root)
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
    prior_proven_live = (
        receipt_proven_prediction_rows(root)
        if not prior_predictions.empty
        else pd.DataFrame()
    )
    source_files = [
        *prior_lineage_files,
        policy_artifact.directory / "policy.json",
        policy_artifact.directory / "receipt.json",
    ]
    model_input_files: list[Path] = []
    if rate_observations is None:
        rate_observations, rate_files = load_point_in_time_rate_observations(root)
        source_files.extend(rate_files)
        model_input_files.extend(rate_files)
    route_errors: dict[str, str] = {}
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

    combined_samples = _canonical_samples(opra_history, prior_samples)

    live_samples: list[pd.DataFrame] = []
    live_source_files: list[Path] = []
    live_status: dict[str, Mapping[str, object]] = {}
    for symbol in clean_symbols:
        try:
            batch = build_live_prediction_inputs(
                root,
                symbol=symbol,
                prediction_created_at=created,
                contract_policy=effective_contract,
                rate_observations=rate_observations,
            )
            live_status[symbol] = {
                "status": batch.status,
                "reason": batch.reason,
                "target_snapshot_for": batch.target_snapshot_for,
            }
            if not batch.samples.empty:
                live_samples.append(batch.samples)
            live_source_files.extend(batch.source_files)
        except Exception as exc:
            route_errors[f"{symbol}/live"] = f"{type(exc).__name__}: {exc}"
            live_status[symbol] = {
                "status": "TARGET_BAR_NOT_READY",
                "reason": route_errors[f"{symbol}/live"],
            }
    source_files.extend(live_source_files)
    new_live_samples = (
        pd.concat(live_samples, ignore_index=True, sort=False)
        if live_samples
        else pd.DataFrame()
    )

    # The timestamp written into every new prediction is the same receipt time
    # passed to the publisher. Existing predictions retain their first receipt.
    published_at = max(utc_timestamp(clock()), created)
    new_live_predictions = (
        create_prediction_rows(
            new_live_samples,
            prediction_created_at=created,
            prediction_available_at=published_at,
            models=models,
            projection_policy=effective_projection,
        )
        if not new_live_samples.empty
        else pd.DataFrame()
    )
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
        _concat_frames(prior_predictions, offline_predictions, new_live_predictions)
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
    samples_for_publication = _concat_frames(combined_samples, new_live_samples)
    samples_for_publication = _redact_closed_lockbox(
        samples_for_publication,
        global_partitions,
    )
    surfaces = build_pricing_surfaces(
        predictions,
        evaluations,
        available_at=published_at,
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
        monitored_at=published_at,
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
            "new_live_sample_rows": len(new_live_samples),
            "new_live_prediction_rows": new_live_prediction_count,
            "route_statuses": live_route_states,
        },
        "black_scholes_baseline": {
            "status": "READY_WHEN_CAUSAL_INPUTS_AVAILABLE",
            "requires_fitted_residual_model": False,
            "new_predictions_created": new_live_prediction_count,
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

    run_directory = _write_and_publish_generation(
        root,
        created=created,
        published_at=published_at,
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
    lineage = verify_completed_option_pricing_lineage(
        root,
        run_directory=run_directory,
        policy_artifact=policy_artifact,
    )
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
        generated_at=published_at,
    )
    final_report["closed_lockbox_inventory"] = closed_lockbox_report
    eligibility_artifact = publish_eligibility_report(
        root,
        report=final_report,
        pricing_run=run_directory,
        published_at=published_at,
    )
    previous_health = _read_optional_evidence(
        lambda: read_current_runtime_health(root),
        label="prior runtime health",
        route_errors=route_errors,
    )
    health = build_runtime_health(
        pricing_run=run_directory,
        eligibility_report=final_report,
        lineage_report=lineage,
        route_errors=route_errors,
        live_routes=live_status,
        live_symbols=clean_symbols,
        elapsed_seconds=time.perf_counter() - cycle_started,
        peak_memory_bytes=peak_memory_bytes,
        capacity=capacity_report(root, limits=limits),
        checked_at=published_at,
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
    )
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
    args = parser.parse_args(argv)
    if args.interval_minutes < 1:
        parser.error("--interval-minutes must be at least 1")
    if not 0 <= args.phase_offset_minutes < args.interval_minutes:
        parser.error("--phase-offset-minutes must satisfy 0 <= phase < interval-minutes")
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
        try:
            while True:
                if not args.once:
                    boundary = next_boundary(
                        datetime.now(timezone.utc),
                        interval_minutes=args.interval_minutes,
                        phase_offset_minutes=args.phase_offset_minutes,
                    )
                    print(f"Next Pricing cycle: {boundary.isoformat()}")
                    time.sleep(max(0.0, (boundary - datetime.now(timezone.utc)).total_seconds()))
                try:
                    result = run_option_pricing_once(root, symbols=configured_symbols)
                    print(
                        "Pricing published: "
                        f"samples={result.sample_rows}; predictions={result.prediction_rows}; "
                        f"evaluations={result.evaluation_rows}; surfaces={result.surface_rows}; "
                        f"models_trained={result.models_trained}; models_reused={result.models_reused}; "
                        f"gate_status={result.gate_status}; health={result.health_status}; "
                        f"run={result.run_directory}"
                    )
                    for route, error in result.route_errors.items():
                        print(f"Route unavailable {route}: {error}")
                    for symbol, route in result.live_routes.items():
                        target = route.get("target_snapshot_for")
                        target_text = f"; target={target}" if target is not None else ""
                        reason = str(route.get("reason", "")).strip()
                        reason_text = f"; reason={reason}" if reason else ""
                        print(
                            f"Live route {symbol}: {route.get('status', 'UNKNOWN')}"
                            f"{target_text}{reason_text}"
                        )
                    exit_code = result.health_exit_code
                except Exception as exc:
                    print(f"Pricing failed: {type(exc).__name__}: {exc}")
                    exit_code = 1
                if args.once:
                    return exit_code
        except KeyboardInterrupt:
            print("Option Pricing runtime stopped.")
            return 0


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


def _recover_prior_generation(root: Path) -> tuple[pd.DataFrame, pd.DataFrame, list[Path]]:
    try:
        publication = read_current_option_pricing_publication(root)
    except Exception:
        if (root / "ml" / "option-pricing-latest" / "run.json").exists():
            raise
        return pd.DataFrame(), pd.DataFrame(), []
    samples = pd.read_parquet(publication.run_directory / "pricing-samples.parquet")
    predictions = pd.read_parquet(publication.run_directory / "pricing-predictions.parquet")
    return samples.drop(columns="id"), predictions.drop(columns="id"), [
        publication.run_directory / "publication.json",
        publication.run_directory / "manifest.json",
    ]


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
    published_at: pd.Timestamp,
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
) -> Path:
    runs_root = root / "ml" / "option-pricing-runs"
    runs_root.mkdir(parents=True, exist_ok=True)
    base = created.strftime("%Y%m%dT%H%M%S.%fZ")
    destination = runs_root / base
    suffix = 2
    while destination.exists():
        destination = runs_root / f"{base}-{suffix}"
        suffix += 1
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.tmp-{os.getpid()}-", dir=runs_root))
    names_and_frames = (
        ("pricing-samples.parquet", samples, OPTION_PRICING_SAMPLE_SCHEMA, ("symbol", "target_snapshot_for", "contract_symbol")),
        ("pricing-predictions.parquet", predictions, OPTION_PRICING_PREDICTION_SCHEMA, ("symbol", "target_snapshot_for", "contract_symbol", "prediction_created_at")),
        ("pricing-evaluations.parquet", evaluations, OPTION_PRICING_EVALUATION_SCHEMA, ("symbol", "target_snapshot_for", "contract_symbol", "prediction_created_at")),
        ("pricing-surfaces.parquet", surfaces, OPTION_PRICING_SURFACE_SCHEMA, ("symbol", "target_snapshot_for", "call_put", "expiration_bucket", "moneyness_bucket")),
        ("pricing-monitoring.parquet", monitoring, OPTION_PRICING_MONITORING_SCHEMA, ("metric_name", "scope_type", "scope_value", "monitored_at")),
    )
    output_names: list[str] = []
    try:
        for name, frame, schema, keys in names_and_frames:
            output = _output_frame(frame, schema=schema, key_columns=keys)
            write_parquet_with_schema(output, staging / name, schema)
            output_names.append(name)
        (staging / OPTION_PRICING_REPORT_NAME).write_text(
            json.dumps(dict(reports), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        output_names.append(OPTION_PRICING_REPORT_NAME)
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
                "prediction_available_at": published_at.isoformat(),
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
        published_at=published_at,
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
    return publication.run_directory


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
    clean = frame.drop(columns="id", errors="ignore")
    return frame_with_readable_id(clean, key_columns=key_columns)


def _concat_frames(*frames: pd.DataFrame) -> pd.DataFrame:
    available = [frame.drop(columns="id", errors="ignore") for frame in frames if not frame.empty]
    return pd.concat(available, ignore_index=True, sort=False) if available else pd.DataFrame()


if __name__ == "__main__":
    raise SystemExit(main())
