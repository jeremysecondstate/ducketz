from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.metrics import brier_score_loss, log_loss

from datafetching.parquet_store import DATASTORE_TARGETS, resolve_datastore_dir
from datafetching.runtime_lock import exclusive_runtime_lock
from ml.artifacts import (
    create_timestamp_directory,
    file_checksum,
    utc_timestamp,
    write_manifest,
)
from ml.current_publication import read_current_publication
from ml.parquet_contracts import (
    empty_frame,
    frame_with_readable_id,
    write_parquet_with_schema,
)
from ml.sequence_encoder.contracts import (
    DISTRIBUTION_SCHEMA,
    EMBEDDING_COLUMNS,
    EMBEDDING_SCHEMA,
    SEQUENCE_DISTRIBUTION_SCHEMA_VERSION,
    SEQUENCE_EMBEDDING_SCHEMA_VERSION,
    SEQUENCE_ENCODER_POLICY_VERSION,
    STATE_SCHEMA,
    SequenceEncoderConfig,
)
from ml.sequence_encoder.dataset import (
    RobustSequenceScaler,
    WindowedExamples,
    build_windowed_examples,
    chronological_partitions,
    pretraining_windows,
)
from ml.sequence_encoder.model import model_contract
from ml.sequence_encoder.publication import publish_sequence_run
from ml.sequence_encoder.surface import (
    loop_b_supervised_labels,
    materialize_hourly_surface_states,
)
from ml.sequence_encoder.training import (
    TrainedSequenceEnsemble,
    calibrated_prediction,
    train_sequence_ensemble,
)


@dataclass(frozen=True)
class SequenceTrainingResult:
    run_directory: Path
    status: str
    state_rows: int
    pretraining_windows: int
    training_rows: int
    calibration_rows: int
    assessment_rows: int
    live_distribution_rows: int
    published: bool
    report: Mapping[str, object]


def run_sequence_training_once(
    datastore_root: Path,
    *,
    symbols: Sequence[str] | None = None,
    information_cutoff: object,
    run_timestamp: object | None = None,
    config: SequenceEncoderConfig | None = None,
    maximum_sessions_per_symbol: int | None = None,
    publish_shadow: bool = False,
) -> SequenceTrainingResult:
    root = Path(datastore_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Datastore does not exist: {root}")
    runtime = config or SequenceEncoderConfig()
    created = utc_timestamp(run_timestamp)
    cutoff = utc_timestamp(information_cutoff)
    if created < cutoff:
        raise ValueError("run_timestamp cannot precede information_cutoff")
    loop_b = read_current_publication(root)
    source_record = _current_record(loop_b.pointer)
    samples_path = loop_b.run_directory / "samples.parquet"
    predictions_path = loop_b.run_directory / "predictions.parquet"
    if not samples_path.is_file() or not predictions_path.is_file():
        raise FileNotFoundError("Current Loop B samples or predictions are missing")
    samples = pd.read_parquet(samples_path)
    labels = loop_b_supervised_labels(samples, horizons=runtime.horizons)
    partitions = chronological_partitions(labels, config=runtime)
    missing_horizons = sorted(set(runtime.horizons).difference(partitions))
    if missing_horizons:
        raise ValueError(
            "Sequence encoder has insufficient chronological partitions for: "
            + ", ".join(missing_horizons)
        )
    selected_symbols = tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in (symbols or sorted(labels["symbol"].astype(str).unique()))
            if str(value).strip()
        )
    )
    earliest_train = min(
        partition.train["bar_end_timestamp"].min() for partition in partitions.values()
    )
    start = utc_timestamp(earliest_train) - pd.Timedelta(
        hours=runtime.window_length * 4
    )
    states, state_inputs = materialize_hourly_surface_states(
        root,
        symbols=selected_symbols,
        information_cutoff=cutoff,
        start=start,
        maximum_sessions_per_symbol=maximum_sessions_per_symbol,
    )
    if states.empty:
        raise ValueError("Sequence state materialization produced no rows")
    calibration_fit_cutoff = min(
        partition.calibration["decision_timestamp"].min()
        for partition in partitions.values()
    )
    scaler_states = states.loc[
        pd.to_datetime(states["information_available_at"], utc=True).lt(
            calibration_fit_cutoff
        )
    ]
    scaler = RobustSequenceScaler.fit(
        scaler_states,
        feature_columns=runtime.feature_columns,
    )
    pretrain_x, pretrain_y = pretraining_windows(
        states,
        scaler=scaler,
        config=runtime,
        through=calibration_fit_cutoff - pd.Timedelta(nanoseconds=1),
    )
    symbol_vocabulary = {
        symbol: index + 1 for index, symbol in enumerate(sorted(selected_symbols))
    }
    horizon_vocabulary = {
        horizon: index for index, horizon in enumerate(runtime.horizons)
    }
    train_labels = pd.concat(
        [partition.train for partition in partitions.values()],
        ignore_index=True,
        sort=False,
    )
    calibration_labels = pd.concat(
        [partition.calibration for partition in partitions.values()],
        ignore_index=True,
        sort=False,
    )
    assessment_labels = pd.concat(
        [partition.assessment for partition in partitions.values()],
        ignore_index=True,
        sort=False,
    )
    train_examples = _examples(
        states,
        train_labels,
        scaler=scaler,
        config=runtime,
        symbol_vocabulary=symbol_vocabulary,
        horizon_vocabulary=horizon_vocabulary,
    )
    calibration_examples = _examples(
        states,
        calibration_labels,
        scaler=scaler,
        config=runtime,
        symbol_vocabulary=symbol_vocabulary,
        horizon_vocabulary=horizon_vocabulary,
    )
    assessment_examples = _examples(
        states,
        assessment_labels,
        scaler=scaler,
        config=runtime,
        symbol_vocabulary=symbol_vocabulary,
        horizon_vocabulary=horizon_vocabulary,
    )
    _require_example_horizons(train_examples, horizon_vocabulary, "training")
    _require_example_horizons(
        calibration_examples,
        horizon_vocabulary,
        "calibration",
    )
    _require_example_horizons(
        assessment_examples,
        horizon_vocabulary,
        "assessment",
    )
    ensemble = train_sequence_ensemble(
        pretrain_windows=pretrain_x,
        pretrain_targets=pretrain_y,
        train=train_examples,
        calibration=calibration_examples,
        config=runtime,
        symbol_count=len(symbol_vocabulary),
        horizon_vocabulary=horizon_vocabulary,
    )
    assessment_prediction = calibrated_prediction(
        ensemble,
        assessment_examples,
        horizon_vocabulary=horizon_vocabulary,
        batch_size=runtime.batch_size,
    )
    evaluation = _assessment_report(
        train_examples,
        assessment_examples,
        assessment_prediction,
        horizon_vocabulary=horizon_vocabulary,
    )
    live_labels = _live_route_labels(
        samples,
        pd.read_parquet(predictions_path),
        horizons=runtime.horizons,
    )
    live_examples = build_windowed_examples(
        states,
        live_labels,
        scaler=scaler,
        config=runtime,
        symbol_vocabulary=symbol_vocabulary,
        horizon_vocabulary=horizon_vocabulary,
    )
    run_directory = create_timestamp_directory(
        root / "ml" / "sequence-encoder-runs",
        timestamp=created,
    )
    if len(live_examples):
        live_prediction = calibrated_prediction(
            ensemble,
            live_examples,
            horizon_vocabulary=horizon_vocabulary,
            batch_size=runtime.batch_size,
        )
        distributions = _distribution_frame(
            live_examples,
            live_prediction,
            created=created,
            model_version=run_directory.name,
            ensemble_size=runtime.ensemble_size,
        )
        embeddings = _embedding_frame(
            live_examples,
            live_prediction,
            model_version=run_directory.name,
        )
        live_inference_status = "READY_SHADOW"
    else:
        distributions = empty_frame(DISTRIBUTION_SCHEMA)
        embeddings = empty_frame(EMBEDDING_SCHEMA)
        live_inference_status = "NO_CAUSAL_LIVE_WINDOWS"
    write_parquet_with_schema(states, run_directory / "states.parquet", STATE_SCHEMA)
    write_parquet_with_schema(
        distributions,
        run_directory / "distributions.parquet",
        DISTRIBUTION_SCHEMA,
    )
    write_parquet_with_schema(
        embeddings,
        run_directory / "embeddings.parquet",
        EMBEDDING_SCHEMA,
    )
    contract = model_contract(
        input_width=train_examples.windows.shape[2],
        symbol_vocabulary=symbol_vocabulary,
        horizon_vocabulary=horizon_vocabulary,
        config=runtime,
    )
    _write_model(run_directory / "model.pt", ensemble, contract=contract)
    _write_joblib_atomic(run_directory / "calibration.joblib", ensemble.calibrations)
    _write_json_atomic(
        run_directory / "preprocessor.json",
        scaler.semantic_contract(),
    )
    report = {
        "schema_version": "pooled-causal-sequence-report-v1",
        "status": "COMPLETE_SHADOW_ONLY",
        "authority": "SHADOW_ONLY",
        "policy_version": SEQUENCE_ENCODER_POLICY_VERSION,
        "run_timestamp": created.isoformat(),
        "information_cutoff": cutoff.isoformat(),
        "calibration_fit_cutoff": utc_timestamp(
            calibration_fit_cutoff
        ).isoformat(),
        "live_inference_status": live_inference_status,
        "source_loop_b": source_record,
        "counts": {
            "states": len(states),
            "pretraining_windows": len(pretrain_x),
            "train": len(train_examples),
            "calibration": len(calibration_examples),
            "assessment": len(assessment_examples),
            "live_distributions": len(distributions),
        },
        "partitions": {
            horizon: {
                "train_clusters": partition.train_clusters,
                "calibration_clusters": partition.calibration_clusters,
                "assessment_clusters": partition.assessment_clusters,
                "purged_rows": partition.purged_rows,
            }
            for horizon, partition in partitions.items()
        },
        "assessment": evaluation,
        "training_history": [dict(value) for value in ensemble.training_history],
        "activation": {
            "eligible": False,
            "reason": "SHADOW_AND_PROSPECTIVE_EVIDENCE_REQUIRED",
            "production_model_replaced": False,
            "loop_b_ranking_changed": False,
            "options_strategy_ranking_changed": False,
        },
        "safety": {
            "orders_enabled": False,
            "orders_placed": 0,
            "automated_action_allowed": False,
            "deterministic_loop_c_authority_required": True,
        },
    }
    _write_json_atomic(run_directory / "report.json", report)
    output_names = (
        "states.parquet",
        "distributions.parquet",
        "embeddings.parquet",
        "model.pt",
        "calibration.joblib",
        "preprocessor.json",
        "report.json",
    )
    write_manifest(
        run_directory,
        run_timestamp=created,
        input_files=(samples_path, predictions_path, *state_inputs),
        output_files=output_names,
        model_name="pooled-causal-sequence-encoder",
        feature_columns=runtime.feature_columns,
        target_column="multi_horizon_direction_and_cost_adjusted_return",
        configuration={
            "policy_version": SEQUENCE_ENCODER_POLICY_VERSION,
            "authority": "SHADOW_ONLY",
            "orders_enabled": False,
            "orders_placed": 0,
            "source_loop_b": source_record,
            "model_contract": contract,
            "configuration": runtime.semantic_contract(),
            "consumers": ["LOOP_B", "OPTIONS_STRATEGY", "LOOP_C_OBSERVE"],
        },
        datastore_root=root,
    )
    if publish_shadow:
        publish_sequence_run(
            root,
            run_directory=run_directory,
            published_at=created,
            source_loop_b=source_record,
        )
    return SequenceTrainingResult(
        run_directory=run_directory,
        status="COMPLETE_SHADOW_ONLY",
        state_rows=len(states),
        pretraining_windows=len(pretrain_x),
        training_rows=len(train_examples),
        calibration_rows=len(calibration_examples),
        assessment_rows=len(assessment_examples),
        live_distribution_rows=len(distributions),
        published=publish_shadow,
        report=report,
    )


def _examples(
    states: pd.DataFrame,
    labels: pd.DataFrame,
    *,
    scaler: RobustSequenceScaler,
    config: SequenceEncoderConfig,
    symbol_vocabulary: Mapping[str, int],
    horizon_vocabulary: Mapping[str, int],
) -> WindowedExamples:
    examples = build_windowed_examples(
        states,
        labels,
        scaler=scaler,
        config=config,
        symbol_vocabulary=symbol_vocabulary,
        horizon_vocabulary=horizon_vocabulary,
    )
    if len(examples) == 0:
        raise ValueError("No causal sequence windows matched the requested labels")
    return examples


def _live_route_labels(
    samples: pd.DataFrame,
    predictions: pd.DataFrame,
    *,
    horizons: Sequence[str],
) -> pd.DataFrame:
    live = predictions.loc[
        predictions["horizon"].astype("string").isin(horizons)
        & predictions["prediction_mode"].astype("string").eq("LIVE")
        & predictions["prediction_status"].astype("string").isin(
            ["COMPLETE", "PREDICTED", "ACTIVE"]
        )
    ].copy()
    if live.empty:
        # Some runs use LIVE as the status and a horizon-specific prediction mode.
        live = predictions.loc[
            predictions["horizon"].astype("string").isin(horizons)
            & predictions["prediction_mode"].astype("string").eq("LIVE")
        ].copy()
    keys = ["symbol", "horizon", "decision_timestamp"]
    sample_columns = [
        *keys,
        "information_available_at",
        "bar_end_timestamp",
        "target_window_start",
        "target_window_end",
    ]
    source = samples.loc[:, sample_columns].drop_duplicates(keys)
    joined = live.loc[:, keys].drop_duplicates().merge(
        source,
        on=keys,
        how="inner",
        validate="one_to_one",
    )
    if len(joined) != len(live.loc[:, keys].drop_duplicates()):
        raise ValueError("Current Loop B LIVE routes do not map to exact samples")
    joined["target_cost_adjusted_positive"] = 0.0
    joined["forward_cost_adjusted_return"] = 0.0
    joined["decision_weight"] = 1.0
    joined["label_available_at"] = joined["target_window_end"]
    joined["label_status"] = "INFERENCE_ONLY"
    return joined


def _assessment_report(
    train: WindowedExamples,
    assessment: WindowedExamples,
    prediction: Mapping[str, np.ndarray],
    *,
    horizon_vocabulary: Mapping[str, int],
) -> dict[str, object]:
    report: dict[str, object] = {}
    for horizon, horizon_id in horizon_vocabulary.items():
        train_mask = train.horizon_ids == horizon_id
        mask = assessment.horizon_ids == horizon_id
        if not train_mask.any() or not mask.any():
            continue
        labels = assessment.direction_targets[mask].astype(int)
        probabilities = prediction["calibrated_probability_up"][mask]
        weights = assessment.sample_weights[mask]
        train_base_rate = float(
            np.average(train.direction_targets[train_mask], weights=train.sample_weights[train_mask])
        )
        base = np.full(len(labels), np.clip(train_base_rate, 1.0e-6, 1.0 - 1.0e-6))
        lower = prediction["return_quantile_10"][mask]
        upper = prediction["return_quantile_90"][mask]
        returns = assessment.return_targets[mask]
        observed_log_loss = float(
            log_loss(
                labels,
                probabilities,
                sample_weight=weights,
                labels=[0, 1],
            )
        )
        base_log_loss = float(
            log_loss(labels, base, sample_weight=weights, labels=[0, 1])
        )
        report[horizon] = {
            "rows": int(mask.sum()),
            "decision_weight_sum": float(weights.sum()),
            "log_loss": observed_log_loss,
            "training_base_rate_log_loss": base_log_loss,
            "beats_training_base_rate_log_loss": observed_log_loss < base_log_loss,
            "brier_score": float(
                brier_score_loss(labels, probabilities, sample_weight=weights)
            ),
            "mean_absolute_return_error": float(
                np.average(
                    np.abs(returns - prediction["expected_return"][mask]),
                    weights=weights,
                )
            ),
            "interval_80_coverage": float(
                np.average((returns >= lower) & (returns <= upper), weights=weights)
            ),
        }
    return report


def _require_example_horizons(
    examples: WindowedExamples,
    horizon_vocabulary: Mapping[str, int],
    partition_name: str,
) -> None:
    observed = set(int(value) for value in np.unique(examples.horizon_ids))
    missing = sorted(
        horizon
        for horizon, horizon_id in horizon_vocabulary.items()
        if int(horizon_id) not in observed
    )
    if missing:
        raise ValueError(
            f"Sequence {partition_name} windows are missing horizons: "
            + ", ".join(missing)
        )


def _distribution_frame(
    examples: WindowedExamples,
    prediction: Mapping[str, np.ndarray],
    *,
    created: pd.Timestamp,
    model_version: str,
    ensemble_size: int,
) -> pd.DataFrame:
    output = examples.metadata.copy()
    for name in (
        "raw_probability_up",
        "calibrated_probability_up",
        "expected_return",
        "return_standard_deviation",
        "return_quantile_10",
        "return_quantile_50",
        "return_quantile_90",
        "aleatoric_uncertainty",
        "epistemic_uncertainty",
        "total_uncertainty",
    ):
        output[name] = prediction[name]
    output["prediction_created_at"] = created
    output["model_name"] = "pooled-causal-sequence-encoder"
    output["model_version"] = model_version
    output["prediction_mode"] = "SHADOW"
    output["prediction_status"] = "SHADOW_READY"
    output["calibration_method"] = "platt-plus-interval-scale"
    output["ensemble_size"] = ensemble_size
    output["automated_action_allowed"] = False
    output["limitations"] = (
        "Shadow-only; no Loop B, Strategy ranking, portfolio, broker, or order authority."
    )
    output["schema_version"] = SEQUENCE_DISTRIBUTION_SCHEMA_VERSION
    return frame_with_readable_id(
        output,
        key_columns=("symbol", "horizon", "decision_timestamp"),
    )


def _embedding_frame(
    examples: WindowedExamples,
    prediction: Mapping[str, np.ndarray],
    *,
    model_version: str,
) -> pd.DataFrame:
    metadata = examples.metadata.sort_values(
        ["symbol", "decision_timestamp", "information_available_at"]
    ).drop_duplicates(["symbol", "decision_timestamp"], keep="last")
    positions = metadata.index.to_numpy(dtype=int)
    output = metadata.loc[
        :, [
            "symbol",
            "decision_timestamp",
            "information_available_at",
            "sequence_window_start",
            "sequence_window_end",
        ]
    ].reset_index(drop=True)
    embeddings = prediction["embedding"][positions]
    for index, name in enumerate(EMBEDDING_COLUMNS):
        output[name] = embeddings[:, index]
    output["model_version"] = model_version
    output["embedding_status"] = "SHADOW_READY"
    output["schema_version"] = SEQUENCE_EMBEDDING_SCHEMA_VERSION
    return frame_with_readable_id(
        output,
        key_columns=("symbol", "decision_timestamp"),
    )


def _write_model(
    path: Path,
    ensemble: TrainedSequenceEnsemble,
    *,
    contract: Mapping[str, object],
) -> None:
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        torch.save(
            {
                "contract": dict(contract),
                "state_dicts": [model.state_dict() for model in ensemble.models],
            },
            temporary,
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_joblib_atomic(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        joblib.dump(value, temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(dict(value), indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _current_record(pointer: Mapping[str, object]) -> dict[str, object]:
    current = pointer.get("current")
    if isinstance(current, Mapping):
        return dict(current)
    return {
        "run_path": pointer.get("path"),
        "run_timestamp": pointer.get("run_timestamp"),
        "legacy": True,
    }


def _add_root_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--root-dir", type=Path)
    group.add_argument("--datastore-target", choices=sorted(DATASTORE_TARGETS))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Train the pooled causal sequence encoder as a shadow-only challenger."
    )
    _add_root_arguments(parser)
    parser.add_argument("--information-cutoff", required=True)
    parser.add_argument("--run-timestamp")
    parser.add_argument("--symbol", action="append", default=[])
    parser.add_argument("--maximum-sessions-per-symbol", type=int)
    parser.add_argument("--publish-shadow", action="store_true")
    parser.add_argument("--compact", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = resolve_datastore_dir(
            root_dir=args.root_dir,
            target=args.datastore_target,
        )
        with exclusive_runtime_lock(
            root / ".ducketz-sequence-encoder-runtime.lock",
            process_name="Duckets pooled sequence encoder",
        ):
            result = run_sequence_training_once(
                root,
                symbols=args.symbol or None,
                information_cutoff=args.information_cutoff,
                run_timestamp=args.run_timestamp,
                maximum_sessions_per_symbol=args.maximum_sessions_per_symbol,
                publish_shadow=args.publish_shadow,
            )
        payload = {
            **asdict(result),
            "run_directory": str(result.run_directory),
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 0
    except Exception as exc:
        payload = {
            "status": "ERROR",
            "error": str(exc),
            "orders_enabled": False,
            "orders_placed": 0,
        }
        exit_code = 2
    print(
        json.dumps(
            payload,
            separators=(",", ":") if args.compact else None,
            default=str,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SequenceTrainingResult", "main", "run_sequence_training_once"]
