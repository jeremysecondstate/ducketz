from __future__ import annotations

import copy
import random
from dataclasses import dataclass
from typing import Mapping

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

from ml.calibration import ProbabilityCalibrator, fit_probability_calibrator
from ml.sequence_encoder.contracts import SequenceEncoderConfig
from ml.sequence_encoder.dataset import WindowedExamples
from ml.sequence_encoder.model import (
    EnsemblePrediction,
    PooledCausalSequenceEncoder,
    ensemble_predict,
)


@dataclass(frozen=True)
class HorizonCalibration:
    probability: ProbabilityCalibrator
    standard_deviation_scale: float
    nominal_interval_coverage: float = 0.80


@dataclass(frozen=True)
class TrainedSequenceEnsemble:
    models: tuple[PooledCausalSequenceEncoder, ...]
    calibrations: Mapping[str, HorizonCalibration]
    training_history: tuple[Mapping[str, object], ...]


def train_sequence_ensemble(
    *,
    pretrain_windows: np.ndarray,
    pretrain_targets: np.ndarray,
    train: WindowedExamples,
    calibration: WindowedExamples,
    config: SequenceEncoderConfig,
    symbol_count: int,
    horizon_vocabulary: Mapping[str, int],
) -> TrainedSequenceEnsemble:
    if len(train) < 1 or len(calibration) < 1:
        raise ValueError("Sequence training requires train and calibration rows")
    if len(pretrain_windows) < config.window_length:
        raise ValueError("Sequence pretraining has insufficient causal windows")
    models: list[PooledCausalSequenceEncoder] = []
    history: list[Mapping[str, object]] = []
    for member in range(config.ensemble_size):
        seed = config.random_state + member
        _seed_everything(seed)
        model = PooledCausalSequenceEncoder(
            input_width=train.windows.shape[2],
            symbol_count=symbol_count,
            horizon_count=len(horizon_vocabulary),
            config=config,
        )
        pretrain_loss = _pretrain(
            model,
            pretrain_windows,
            pretrain_targets,
            config=config,
            seed=seed,
        )
        selected = _cluster_bootstrap_indices(train, seed=seed)
        supervised_loss = _supervised_train(
            model,
            _subset(train, selected),
            config=config,
            seed=seed,
        )
        models.append(model.cpu())
        history.append(
            {
                "member": member,
                "seed": seed,
                "pretrain_final_loss": pretrain_loss,
                "supervised_final_loss": supervised_loss,
                "bootstrap_rows": int(len(selected)),
            }
        )
    calibration_prediction = ensemble_predict(
        models,
        windows=calibration.windows,
        symbol_ids=calibration.symbol_ids,
        horizon_ids=calibration.horizon_ids,
        batch_size=config.batch_size,
    )
    calibrations = _fit_horizon_calibrations(
        calibration,
        calibration_prediction,
        horizon_vocabulary=horizon_vocabulary,
    )
    return TrainedSequenceEnsemble(
        models=tuple(models),
        calibrations=calibrations,
        training_history=tuple(history),
    )


def calibrated_prediction(
    ensemble: TrainedSequenceEnsemble,
    examples: WindowedExamples,
    *,
    horizon_vocabulary: Mapping[str, int],
    batch_size: int = 512,
) -> dict[str, np.ndarray]:
    prediction = ensemble_predict(
        list(ensemble.models),
        windows=examples.windows,
        symbol_ids=examples.symbol_ids,
        horizon_ids=examples.horizon_ids,
        batch_size=batch_size,
    )
    calibrated_probability = np.empty(len(examples), dtype=float)
    adjusted_standard_deviation = np.empty(len(examples), dtype=float)
    inverse_horizons = {value: key for key, value in horizon_vocabulary.items()}
    raw_standard_deviation = np.sqrt(np.maximum(prediction.total_variance, 1.0e-12))
    for horizon_id in np.unique(examples.horizon_ids):
        horizon = inverse_horizons[int(horizon_id)]
        mask = examples.horizon_ids == horizon_id
        calibration = ensemble.calibrations[horizon]
        calibrated_probability[mask] = calibration.probability.predict(
            prediction.raw_probability_up[mask]
        )
        adjusted_standard_deviation[mask] = (
            raw_standard_deviation[mask] * calibration.standard_deviation_scale
        )
    z_80 = 1.2815515655446004
    return {
        "raw_probability_up": prediction.raw_probability_up,
        "calibrated_probability_up": calibrated_probability,
        "expected_return": prediction.expected_return,
        "return_standard_deviation": adjusted_standard_deviation,
        "return_quantile_10": prediction.expected_return
        - z_80 * adjusted_standard_deviation,
        "return_quantile_50": prediction.expected_return,
        "return_quantile_90": prediction.expected_return
        + z_80 * adjusted_standard_deviation,
        "aleatoric_uncertainty": np.sqrt(
            np.maximum(prediction.aleatoric_variance, 0.0)
        ),
        "epistemic_uncertainty": np.sqrt(
            np.maximum(prediction.epistemic_variance, 0.0)
        ),
        "total_uncertainty": adjusted_standard_deviation,
        "embedding": prediction.embedding,
    }


def _pretrain(
    model: PooledCausalSequenceEncoder,
    windows: np.ndarray,
    targets: np.ndarray,
    *,
    config: SequenceEncoderConfig,
    seed: int,
) -> float:
    dataset = TensorDataset(torch.from_numpy(windows), torch.from_numpy(targets))
    generator = torch.Generator().manual_seed(seed)
    loader = DataLoader(
        dataset,
        batch_size=config.batch_size,
        shuffle=True,
        generator=generator,
    )
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=config.learning_rate,
        weight_decay=config.weight_decay,
    )
    final_loss = float("nan")
    model.train()
    for _ in range(config.pretrain_epochs):
        total = 0.0
        count = 0
        for window, target in loader:
            optimizer.zero_grad(set_to_none=True)
            prediction = model.reconstruct_next(window)
            loss = nn.functional.smooth_l1_loss(prediction, target)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            total += float(loss.detach()) * len(window)
            count += len(window)
        final_loss = total / max(count, 1)
    return float(final_loss)


def _supervised_train(
    model: PooledCausalSequenceEncoder,
    examples: WindowedExamples,
    *,
    config: SequenceEncoderConfig,
    seed: int,
) -> float:
    dataset = TensorDataset(
        torch.from_numpy(examples.windows),
        torch.from_numpy(examples.symbol_ids),
        torch.from_numpy(examples.horizon_ids),
        torch.from_numpy(examples.direction_targets),
        torch.from_numpy(examples.return_targets),
        torch.from_numpy(examples.sample_weights),
    )
    loader = DataLoader(
        dataset,
        batch_size=config.batch_size,
        shuffle=True,
        generator=torch.Generator().manual_seed(seed + 10_000),
    )
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=config.learning_rate,
        weight_decay=config.weight_decay,
    )
    final_loss = float("nan")
    model.train()
    for _ in range(config.supervised_epochs):
        total = 0.0
        total_weight = 0.0
        for window, symbols, horizons, direction, returns, weights in loader:
            optimizer.zero_grad(set_to_none=True)
            output = model(window, symbols, horizons)
            direction_loss = nn.functional.binary_cross_entropy_with_logits(
                output["direction_logit"], direction, reduction="none"
            )
            inverse_variance = torch.exp(-output["return_log_variance"])
            return_loss = 0.5 * (
                output["return_log_variance"]
                + (returns - output["return_mean"]) ** 2 * inverse_variance
            )
            combined = direction_loss + return_loss
            loss = (combined * weights).sum() / weights.sum().clamp_min(1.0e-8)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), max_norm=1.0)
            optimizer.step()
            total += float((combined.detach() * weights).sum())
            total_weight += float(weights.sum())
        final_loss = total / max(total_weight, 1.0e-8)
    return float(final_loss)


def _fit_horizon_calibrations(
    examples: WindowedExamples,
    prediction: EnsemblePrediction,
    *,
    horizon_vocabulary: Mapping[str, int],
) -> dict[str, HorizonCalibration]:
    output: dict[str, HorizonCalibration] = {}
    for horizon, horizon_id in horizon_vocabulary.items():
        mask = examples.horizon_ids == horizon_id
        if not mask.any():
            continue
        probability = fit_probability_calibrator(
            "platt",
            prediction.raw_probability_up[mask],
            examples.direction_targets[mask],
            sample_weight=examples.sample_weights[mask],
            clip_to_observed_probability_range=True,
            require_nondecreasing=True,
        )
        raw_std = np.sqrt(np.maximum(prediction.total_variance[mask], 1.0e-12))
        ratio = np.abs(
            examples.return_targets[mask] - prediction.expected_return[mask]
        ) / raw_std
        scale = float(np.quantile(ratio, 0.80) / 1.2815515655446004)
        output[horizon] = HorizonCalibration(
            probability=probability,
            standard_deviation_scale=float(np.clip(scale, 0.25, 4.0)),
        )
    return output


def _cluster_bootstrap_indices(
    examples: WindowedExamples,
    *,
    seed: int,
) -> np.ndarray:
    if examples.metadata.empty:
        return np.arange(len(examples), dtype=int)
    clusters = examples.metadata[
        ["horizon", "target_window_start", "target_window_end"]
    ].astype("string").agg("|".join, axis=1)
    unique = clusters.drop_duplicates().to_numpy()
    rng = np.random.default_rng(seed)
    sampled = rng.choice(unique, size=len(unique), replace=True)
    positions: list[int] = []
    for cluster in sampled:
        positions.extend(np.flatnonzero(clusters.to_numpy() == cluster).tolist())
    return np.asarray(positions, dtype=int)


def _subset(examples: WindowedExamples, indices: np.ndarray) -> WindowedExamples:
    return WindowedExamples(
        windows=examples.windows[indices],
        symbol_ids=examples.symbol_ids[indices],
        horizon_ids=examples.horizon_ids[indices],
        direction_targets=examples.direction_targets[indices],
        return_targets=examples.return_targets[indices],
        sample_weights=examples.sample_weights[indices],
        metadata=examples.metadata.iloc[indices].reset_index(drop=True),
    )


def clone_model(model: PooledCausalSequenceEncoder) -> PooledCausalSequenceEncoder:
    return copy.deepcopy(model)


def _seed_everything(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True, warn_only=True)


__all__ = [
    "HorizonCalibration",
    "TrainedSequenceEnsemble",
    "calibrated_prediction",
    "train_sequence_ensemble",
]
