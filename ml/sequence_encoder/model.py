from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import torch
from torch import nn

from ml.sequence_encoder.contracts import SequenceEncoderConfig


class PooledCausalSequenceEncoder(nn.Module):
    """Compact pooled LSTM with probabilistic multi-horizon heads."""

    def __init__(
        self,
        *,
        input_width: int,
        symbol_count: int,
        horizon_count: int,
        config: SequenceEncoderConfig,
    ) -> None:
        super().__init__()
        self.input_width = int(input_width)
        self.symbol_count = int(symbol_count)
        self.horizon_count = int(horizon_count)
        self.config = config
        self.input_projection = nn.Linear(self.input_width, config.hidden_size)
        self.input_norm = nn.LayerNorm(config.hidden_size)
        self.encoder = nn.LSTM(
            input_size=config.hidden_size,
            hidden_size=config.hidden_size,
            num_layers=config.num_layers,
            dropout=config.dropout if config.num_layers > 1 else 0.0,
            batch_first=True,
        )
        symbol_width = 8
        horizon_width = 4
        self.symbol_embedding = nn.Embedding(symbol_count + 1, symbol_width)
        self.horizon_embedding = nn.Embedding(horizon_count, horizon_width)
        fused_width = config.hidden_size + symbol_width + horizon_width
        self.fusion = nn.Sequential(
            nn.Linear(fused_width, config.hidden_size),
            nn.GELU(),
            nn.Dropout(config.dropout),
            nn.LayerNorm(config.hidden_size),
        )
        self.direction_head = nn.Linear(config.hidden_size, 1)
        self.return_head = nn.Linear(config.hidden_size, 2)
        self.reconstruction_head = nn.Linear(
            config.hidden_size,
            len(config.feature_columns),
        )

    def encode(self, windows: torch.Tensor) -> torch.Tensor:
        projected = self.input_norm(self.input_projection(windows))
        encoded, _ = self.encoder(projected)
        return encoded[:, -1, :]

    def reconstruct_next(self, windows: torch.Tensor) -> torch.Tensor:
        return self.reconstruction_head(self.encode(windows))

    def forward(
        self,
        windows: torch.Tensor,
        symbol_ids: torch.Tensor,
        horizon_ids: torch.Tensor,
    ) -> dict[str, torch.Tensor]:
        embedding = self.encode(windows)
        fused = torch.cat(
            [
                embedding,
                self.symbol_embedding(symbol_ids),
                self.horizon_embedding(horizon_ids),
            ],
            dim=1,
        )
        state = self.fusion(fused)
        direction_logit = self.direction_head(state).squeeze(1)
        return_parameters = self.return_head(state)
        return {
            "embedding": embedding,
            "direction_logit": direction_logit,
            "return_mean": return_parameters[:, 0],
            "return_log_variance": return_parameters[:, 1].clamp(-16.0, 4.0),
        }


@dataclass(frozen=True)
class EnsemblePrediction:
    raw_probability_up: np.ndarray
    expected_return: np.ndarray
    aleatoric_variance: np.ndarray
    epistemic_variance: np.ndarray
    total_variance: np.ndarray
    embedding: np.ndarray


def ensemble_predict(
    models: list[PooledCausalSequenceEncoder],
    *,
    windows: np.ndarray,
    symbol_ids: np.ndarray,
    horizon_ids: np.ndarray,
    batch_size: int = 512,
) -> EnsemblePrediction:
    if not models:
        raise ValueError("At least one sequence model is required")
    if len(windows) != len(symbol_ids) or len(windows) != len(horizon_ids):
        raise ValueError("Sequence inference arrays have inconsistent lengths")
    member_probability: list[np.ndarray] = []
    member_mean: list[np.ndarray] = []
    member_variance: list[np.ndarray] = []
    member_embedding: list[np.ndarray] = []
    for model in models:
        model.eval()
        probability_parts: list[np.ndarray] = []
        mean_parts: list[np.ndarray] = []
        variance_parts: list[np.ndarray] = []
        embedding_parts: list[np.ndarray] = []
        with torch.inference_mode():
            for start in range(0, len(windows), batch_size):
                stop = min(start + batch_size, len(windows))
                output = model(
                    torch.from_numpy(windows[start:stop]),
                    torch.from_numpy(symbol_ids[start:stop]),
                    torch.from_numpy(horizon_ids[start:stop]),
                )
                probability_parts.append(
                    torch.sigmoid(output["direction_logit"]).cpu().numpy()
                )
                mean_parts.append(output["return_mean"].cpu().numpy())
                variance_parts.append(
                    torch.exp(output["return_log_variance"]).cpu().numpy()
                )
                embedding_parts.append(output["embedding"].cpu().numpy())
        member_probability.append(np.concatenate(probability_parts))
        member_mean.append(np.concatenate(mean_parts))
        member_variance.append(np.concatenate(variance_parts))
        member_embedding.append(np.concatenate(embedding_parts))
    probabilities = np.stack(member_probability)
    means = np.stack(member_mean)
    variances = np.stack(member_variance)
    embeddings = np.stack(member_embedding)
    aleatoric = variances.mean(axis=0)
    epistemic = means.var(axis=0)
    return EnsemblePrediction(
        raw_probability_up=probabilities.mean(axis=0),
        expected_return=means.mean(axis=0),
        aleatoric_variance=aleatoric,
        epistemic_variance=epistemic,
        total_variance=aleatoric + epistemic,
        embedding=embeddings.mean(axis=0),
    )


def model_contract(
    *,
    input_width: int,
    symbol_vocabulary: Mapping[str, int],
    horizon_vocabulary: Mapping[str, int],
    config: SequenceEncoderConfig,
) -> dict[str, object]:
    return {
        "architecture": "input-projection-layernorm-lstm-pooled-heads-v1",
        "input_width": int(input_width),
        "symbol_vocabulary": dict(sorted(symbol_vocabulary.items())),
        "horizon_vocabulary": dict(sorted(horizon_vocabulary.items())),
        "configuration": dict(config.semantic_contract()),
        "uncertainty": {
            "aleatoric": "ensemble-mean-gaussian-head-variance",
            "epistemic": "between-member-return-mean-variance",
            "total": "aleatoric-plus-epistemic",
        },
    }


__all__ = [
    "EnsemblePrediction",
    "PooledCausalSequenceEncoder",
    "ensemble_predict",
    "model_contract",
]
