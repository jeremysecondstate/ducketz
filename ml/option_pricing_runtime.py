from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd

from datafetching.decision_time import latest_completed_bar_clock
from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import utc_timestamp, write_manifest
from ml.option_pricing.causal import (
    build_causal_samples,
    build_live_prediction_inputs,
    canonicalize_predictions,
    completed_bar_close,
    evaluate_offline_predictions,
    reconcile_predictions,
)
from ml.option_pricing.model import (
    PricingPartitions,
    fit_or_reuse_pricing_model,
    partition_pricing_samples,
    route_partitions,
)
from ml.option_pricing.opra_materialization import materialize_committed_opra_history
from ml.option_pricing.policies import (
    BSGPModelPolicy,
    ContractSelectionPolicy,
    OPTION_PRICING_POLICY_VERSION,
    PricingPartitionConfig,
    ProjectionPolicy,
)
from ml.option_pricing.prediction import create_prediction_rows
from ml.option_pricing.publication import (
    OPTION_PRICING_PUBLICATION_VERSION,
    OPTION_PRICING_REPORT_NAME,
    publish_option_pricing_run,
    receipt_proven_prediction_rows,
    read_current_option_pricing_publication,
)
from ml.option_pricing.reporting import (
    build_gate_report,
    build_monitoring_rows,
    build_pricing_surfaces,
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
) -> OptionPricingRuntimeResult:
    """Publish one independent, shadow-only Pricing generation."""

    root = Path(datastore_root).resolve()
    clean_symbols = tuple(
        dict.fromkeys(str(value).strip().upper() for value in symbols if str(value).strip())
    )
    if not clean_symbols:
        raise ValueError("At least one Pricing symbol is required")
    created = utc_timestamp(run_timestamp)
    clock = runtime_clock or (lambda: utc_timestamp())
    effective_partitions = partition_config or PricingPartitionConfig()
    effective_model = model_policy or BSGPModelPolicy()
    effective_contract = contract_policy or ContractSelectionPolicy()
    effective_projection = projection_policy or ProjectionPolicy()

    prior_samples, prior_predictions, prior_lineage_files = _recover_prior_generation(root)
    prior_proven_live = (
        receipt_proven_prediction_rows(root)
        if not prior_predictions.empty
        else pd.DataFrame()
    )
    source_files = list(prior_lineage_files)
    model_input_files: list[Path] = []
    if rate_observations is None:
        rate_observations, rate_files = _load_point_in_time_rate_observations(root)
        source_files.extend(rate_files)
        model_input_files.extend(rate_files)
    route_errors: dict[str, str] = {}
    historical, historical_files, history_errors = _materialize_schwab_history(
        root,
        symbols=clean_symbols,
        contract_policy=effective_contract,
        rate_observations=rate_observations,
    )
    route_errors.update(history_errors)
    source_files.extend(historical_files)
    model_input_files.extend(historical_files)
    opra_history, opra_files, opra_errors = materialize_committed_opra_history(
        root,
        symbols=clean_symbols,
        rate_observations=rate_observations,
        contract_policy=effective_contract,
    )
    route_errors.update(
        {f"opra/{route}": error for route, error in opra_errors.items()}
    )
    source_files.extend(opra_files)
    model_input_files.extend(opra_files)
    combined_samples = _canonical_samples(prior_samples, historical, opra_history)

    models: dict[tuple[str, str], object] = {}
    model_reports: dict[str, dict[str, object]] = {}
    models_trained = models_reused = 0
    global_partitions: PricingPartitions | None = None
    if not combined_samples.empty:
        try:
            global_partitions = partition_pricing_samples(
                combined_samples,
                config=effective_partitions,
            )
        except Exception as exc:
            route_errors["global-partitions"] = f"{type(exc).__name__}: {exc}"
    if global_partitions is not None:
        routes = sorted(
            {
                (
                    str(symbol).strip().upper(),
                    str(call_put).strip().upper(),
                )
                for symbol, call_put in combined_samples[["symbol", "call_put"]].itertuples(
                    index=False, name=None
                )
            }
        )
        for symbol, call_put in routes:
            route_name = f"{symbol}/{call_put.lower()}"
            try:
                partitions = route_partitions(
                    global_partitions,
                    symbol=symbol,
                    call_put=call_put,
                    config=effective_partitions,
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
    offline_predictions = _assessment_predictions(
        global_partitions,
        models=models,
        projection_policy=effective_projection,
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
    offline_evaluations = evaluate_offline_predictions(
        predictions,
        combined_samples,
        evaluated_at=created,
    ) if not predictions.empty and not combined_samples.empty else pd.DataFrame()
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
        lineage_verified=True,
    )
    monitoring = build_monitoring_rows(
        report=gate,
        predictions=predictions,
        evaluations=evaluations,
        monitored_at=published_at,
    )
    reports_payload = {
        **preliminary_report,
        "gate": gate,
        "closed_lockbox": _closed_lockbox_report(global_partitions),
        "paid_opra_download_performed_by_runtime": False,
        "automated_action_allowed": False,
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
    )
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
    parser.add_argument("--symbols", nargs="+", default=("NVDA", "GOOG", "MU"))
    parser.add_argument("--interval-minutes", type=int, default=15)
    parser.add_argument("--phase-offset-minutes", type=int, default=1)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.interval_minutes < 1:
        parser.error("--interval-minutes must be at least 1")
    if not 0 <= args.phase_offset_minutes < args.interval_minutes:
        parser.error("--phase-offset-minutes must satisfy 0 <= phase < interval-minutes")
    root = resolve_datastore_dir(
        root_dir=args.datastore,
        target=None if args.datastore is not None else args.datastore_target,
    )
    print("DUCKETS OPTION PRICING RUNTIME")
    print("==============================")
    print(f"DATASTORE: {root}")
    print(f"Symbols: {', '.join(args.symbols)}")
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
                    result = run_option_pricing_once(root, symbols=args.symbols)
                    print(
                        "Pricing published: "
                        f"samples={result.sample_rows}; predictions={result.prediction_rows}; "
                        f"evaluations={result.evaluation_rows}; surfaces={result.surface_rows}; "
                        f"models_trained={result.models_trained}; models_reused={result.models_reused}; "
                        f"run={result.run_directory}"
                    )
                    for route, error in result.route_errors.items():
                        print(f"Route unavailable {route}: {error}")
                    exit_code = 0
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


def _materialize_schwab_history(
    root: Path,
    *,
    symbols: Sequence[str],
    contract_policy: ContractSelectionPolicy,
    rate_observations: pd.DataFrame | None,
) -> tuple[pd.DataFrame, list[Path], dict[str, str]]:
    frames: list[pd.DataFrame] = []
    source_files: list[Path] = []
    errors: dict[str, str] = {}
    for symbol in symbols:
        snapshots = committed_option_snapshots(root, symbol=symbol)
        for target in snapshots:
            candidates = [
                source
                for source in snapshots
                if source.snapshot_for < target.snapshot_for
                and source.available_at < target.available_at
            ]
            if not candidates:
                continue
            source = max(candidates, key=lambda value: (value.snapshot_for, value.available_at))
            route = f"{symbol}/history/{target.snapshot_for.isoformat()}"
            try:
                decision = latest_completed_bar_clock(
                    root,
                    symbol=symbol,
                    as_of=target.snapshot_for,
                )
                if pd.Timestamp(decision.decision_timestamp) != target.snapshot_for:
                    raise ValueError("No exact completed underlying bar at target snapshot")
                underlying = completed_bar_close(decision)
                frame = build_causal_samples(
                    pd.read_parquet(source.contracts_path),
                    target_contracts=pd.read_parquet(target.contracts_path),
                    target_underlying_price=underlying,
                    source_snapshot_for=source.snapshot_for,
                    source_available_at=source.available_at,
                    target_snapshot_for=target.snapshot_for,
                    source_provider="schwab",
                    prediction_mode="OFFLINE",
                    observed_available_at=target.available_at,
                    contract_policy=contract_policy,
                    rate_observations=rate_observations,
                )
                frames.append(frame)
                source_files.extend(
                    (
                        decision.source_file,
                        source.contracts_path,
                        source.receipt_path,
                        target.contracts_path,
                        target.receipt_path,
                    )
                )
            except Exception as exc:
                errors[route] = f"{type(exc).__name__}: {exc}"
    return (
        pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame(),
        source_files,
        errors,
    )


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


def _load_point_in_time_rate_observations(
    root: Path,
) -> tuple[pd.DataFrame | None, list[Path]]:
    paths = tuple(
        sorted(
            (
                root
                / "pools"
                / "macro"
                / "features"
                / "release-context"
                / "fred"
            ).glob("*.parquet")
        )
    )
    if not paths:
        return None, []
    frames = [pd.read_parquet(path) for path in paths]
    combined = pd.concat(frames, ignore_index=True, sort=False)
    required = {"fed_funds_available_at", "macro__fed_funds_level"}
    if not required.issubset(combined.columns):
        return None, list(paths)
    output = pd.DataFrame(
        {
            "available_at": pd.to_datetime(
                combined["fed_funds_available_at"], utc=True, errors="coerce"
            ),
            # FRED FEDFUNDS is quoted in percentage points.
            "risk_free_rate": pd.to_numeric(
                combined["macro__fed_funds_level"], errors="coerce"
            )
            / 100.0,
        }
    ).dropna()
    output = output.loc[output["risk_free_rate"].between(-0.20, 1.0)]
    output = output.sort_values("available_at").drop_duplicates(
        "available_at", keep="last"
    )
    return (output.reset_index(drop=True) if not output.empty else None), list(paths)


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
    publication = publish_option_pricing_run(
        root,
        run_directory=destination,
        published_at=published_at,
    )
    return publication.run_directory


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
