from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import joblib
import numpy as np
import pandas as pd
import torch

from ml.artifacts import utc_timestamp
from ml.parquet_contracts import frame_with_readable_id
from ml.sequence_encoder.contracts import (
    SEQUENCE_DISTRIBUTION_SCHEMA_VERSION,
    SEQUENCE_EMBEDDING_SCHEMA_VERSION,
    EMBEDDING_COLUMNS,
    SequenceEncoderConfig,
    frame_with_sequence_distribution_id,
)
from ml.sequence_encoder.dataset import (
    RobustSequenceScaler,
    build_windowed_examples,
)
from ml.sequence_encoder.model import PooledCausalSequenceEncoder
from ml.sequence_encoder.publication import (
    SequencePublication,
    read_current_sequence_publication,
)
from ml.sequence_encoder.surface import (
    attach_sequence_sample_windows,
    materialize_hourly_surface_states,
)
from ml.sequence_encoder.training import (
    TrainedSequenceEnsemble,
    calibrated_prediction,
)


@dataclass(frozen=True)
class SequenceInferenceResult:
    status: str
    distributions: pd.DataFrame
    embeddings: pd.DataFrame
    source_files: tuple[Path, ...]
    details: Mapping[str, object]


def infer_loop_b_sequence_shadow(
    datastore_root: Path,
    *,
    samples: pd.DataFrame,
    predictions: pd.DataFrame,
    information_cutoff: object,
    prediction_created_at: object,
) -> SequenceInferenceResult:
    """Infer the current Loop B routes from one verified shared model generation."""

    root = Path(datastore_root).resolve()
    publication = read_current_sequence_publication(root)
    ensemble, scaler, config, symbol_vocabulary, horizon_vocabulary = (
        _load_sequence_model(publication)
    )
    routes = _inference_labels(samples, predictions, horizons=config.horizons)
    if routes.empty:
        return SequenceInferenceResult(
            status="NO_LIVE_ROUTES",
            distributions=pd.DataFrame(),
            embeddings=pd.DataFrame(),
            source_files=_publication_files(publication),
            details={"authority": "SHADOW_ONLY", "matched_routes": 0},
        )
    selected_symbols = tuple(sorted(routes["symbol"].astype(str).str.upper().unique()))
    maximum_sessions = max(8, int(np.ceil(config.window_length / 6.0)) + 5)
    states, state_inputs = materialize_hourly_surface_states(
        root,
        symbols=selected_symbols,
        information_cutoff=information_cutoff,
        start=routes["bar_end_timestamp"].min()
        - pd.Timedelta(hours=config.window_length * 8),
        maximum_sessions_per_symbol=maximum_sessions,
    )
    examples = build_windowed_examples(
        states,
        routes,
        scaler=scaler,
        config=config,
        symbol_vocabulary=symbol_vocabulary,
        horizon_vocabulary=horizon_vocabulary,
    )
    if len(examples) == 0:
        return SequenceInferenceResult(
            status="INSUFFICIENT_CAUSAL_WINDOWS",
            distributions=pd.DataFrame(),
            embeddings=pd.DataFrame(),
            source_files=(*_publication_files(publication), *state_inputs),
            details={
                "authority": "SHADOW_ONLY",
                "requested_routes": len(routes),
                "matched_routes": 0,
                "state_rows": len(states),
            },
        )
    prediction = calibrated_prediction(
        ensemble,
        examples,
        horizon_vocabulary=horizon_vocabulary,
        batch_size=config.batch_size,
    )
    created = utc_timestamp(prediction_created_at)
    model_version = _model_origin_version(publication)
    distributions = _distribution_frame(
        examples.metadata,
        prediction,
        created=created,
        model_version=model_version,
        ensemble_size=len(ensemble.models),
    )
    embeddings = _embedding_frame(
        examples.metadata,
        prediction,
        model_version=model_version,
    )
    return SequenceInferenceResult(
        status=(
            "READY_SHADOW"
            if len(distributions) == len(routes)
            else "PARTIAL_SHADOW"
        ),
        distributions=distributions,
        embeddings=embeddings,
        source_files=tuple(
            dict.fromkeys((*_publication_files(publication), *state_inputs))
        ),
        details={
            "authority": "SHADOW_ONLY",
            "model_generation": publication.run_directory.name,
            "requested_routes": len(routes),
            "matched_routes": len(distributions),
            "state_rows": len(states),
            "automated_action_allowed": False,
        },
    )


def _load_sequence_model(
    publication: SequencePublication,
) -> tuple[
    TrainedSequenceEnsemble,
    RobustSequenceScaler,
    SequenceEncoderConfig,
    Mapping[str, int],
    Mapping[str, int],
]:
    configuration = publication.manifest.get("configuration")
    model_value = (
        configuration.get("model_contract")
        if isinstance(configuration, Mapping)
        else None
    )
    if not isinstance(model_value, Mapping):
        raise ValueError("Sequence manifest model contract is missing")
    raw_configuration = model_value.get("configuration")
    if not isinstance(raw_configuration, Mapping):
        raise ValueError("Sequence model configuration is missing")
    config_kwargs = dict(raw_configuration)
    config_kwargs["horizons"] = tuple(config_kwargs["horizons"])
    config_kwargs["feature_columns"] = tuple(config_kwargs["feature_columns"])
    config = SequenceEncoderConfig(**config_kwargs)
    symbol_vocabulary = {
        str(key): int(value)
        for key, value in dict(model_value.get("symbol_vocabulary", {})).items()
    }
    horizon_vocabulary = {
        str(key): int(value)
        for key, value in dict(model_value.get("horizon_vocabulary", {})).items()
    }
    if not symbol_vocabulary or set(horizon_vocabulary) != set(config.horizons):
        raise ValueError("Sequence model vocabularies are incompatible")
    payload = torch.load(
        publication.run_directory / "model.pt",
        map_location="cpu",
        weights_only=True,
    )
    if not isinstance(payload, Mapping) or not isinstance(payload.get("state_dicts"), list):
        raise ValueError("Sequence model artifact is malformed")
    input_width = int(model_value.get("input_width", -1))
    models: list[PooledCausalSequenceEncoder] = []
    for state in payload["state_dicts"]:
        model = PooledCausalSequenceEncoder(
            input_width=input_width,
            symbol_count=len(symbol_vocabulary),
            horizon_count=len(horizon_vocabulary),
            config=config,
        )
        model.load_state_dict(state, strict=True)
        models.append(model.eval())
    calibrations = joblib.load(publication.run_directory / "calibration.joblib")
    if not isinstance(calibrations, Mapping) or set(calibrations) != set(config.horizons):
        raise ValueError("Sequence calibration artifact is incompatible")
    scaler_value = json.loads(
        (publication.run_directory / "preprocessor.json").read_text(encoding="utf-8")
    )
    scaler = RobustSequenceScaler.from_contract(scaler_value)
    return (
        TrainedSequenceEnsemble(
            models=tuple(models),
            calibrations=dict(calibrations),
            training_history=(),
        ),
        scaler,
        config,
        symbol_vocabulary,
        horizon_vocabulary,
    )


def _inference_labels(
    samples: pd.DataFrame,
    predictions: pd.DataFrame,
    *,
    horizons: tuple[str, ...],
) -> pd.DataFrame:
    live = predictions.loc[
        predictions["horizon"].astype("string").isin(horizons)
        & predictions["prediction_mode"].astype("string").eq("LIVE"),
    ].copy()
    output = attach_sequence_sample_windows(samples, live)
    output["target_cost_adjusted_positive"] = 0.0
    output["forward_cost_adjusted_return"] = 0.0
    output["decision_weight"] = 1.0
    return output


def _distribution_frame(
    metadata: pd.DataFrame,
    prediction: Mapping[str, np.ndarray],
    *,
    created: pd.Timestamp,
    model_version: str,
    ensemble_size: int,
) -> pd.DataFrame:
    output = metadata.copy()
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
        "Shared shadow representation; existing Loop B and Strategy authorities remain unchanged."
    )
    output["schema_version"] = SEQUENCE_DISTRIBUTION_SCHEMA_VERSION
    return frame_with_sequence_distribution_id(output)


def _embedding_frame(
    metadata: pd.DataFrame,
    prediction: Mapping[str, np.ndarray],
    *,
    model_version: str,
) -> pd.DataFrame:
    unique = metadata.sort_values(
        ["symbol", "decision_timestamp", "information_available_at"]
    ).drop_duplicates(["symbol", "decision_timestamp"], keep="last")
    positions = unique.index.to_numpy(dtype=int)
    output = unique.loc[
        :, [
            "symbol",
            "decision_timestamp",
            "information_available_at",
            "sequence_window_start",
            "sequence_window_end",
        ]
    ].reset_index(drop=True)
    values = prediction["embedding"][positions]
    for index, name in enumerate(EMBEDDING_COLUMNS):
        output[name] = values[:, index]
    output["model_version"] = model_version
    output["embedding_status"] = "SHADOW_READY"
    output["schema_version"] = SEQUENCE_EMBEDDING_SCHEMA_VERSION
    return frame_with_readable_id(
        output,
        key_columns=("symbol", "decision_timestamp"),
    )


def _publication_files(publication: SequencePublication) -> tuple[Path, ...]:
    return tuple(
        publication.run_directory / name
        for name in (
            "manifest.json",
            "publication.json",
            "model.pt",
            "calibration.joblib",
            "preprocessor.json",
        )
    )


def _model_origin_version(publication: SequencePublication) -> str:
    configuration = publication.manifest.get("configuration")
    origin = (
        configuration.get("model_origin")
        if isinstance(configuration, Mapping)
        else None
    )
    raw_path = origin.get("run_path") if isinstance(origin, Mapping) else None
    if isinstance(raw_path, str) and raw_path.strip():
        return Path(raw_path).name
    return publication.run_directory.name


__all__ = ["SequenceInferenceResult", "infer_loop_b_sequence_shadow"]
