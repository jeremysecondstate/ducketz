from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

from ml.artifacts import semantic_metadata_fingerprint
from ml.parquet_contracts import (
    SEQUENCE_DISTRIBUTION_SCHEMA as DISTRIBUTION_SCHEMA,
    SEQUENCE_EMBEDDING_SCHEMA as EMBEDDING_SCHEMA,
    SEQUENCE_EMBEDDING_VALUE_COLUMNS as EMBEDDING_COLUMNS,
    SEQUENCE_STATE_FEATURE_COLUMNS as SEQUENCE_FEATURE_COLUMNS,
    SEQUENCE_STATE_SCHEMA as STATE_SCHEMA,
)


SEQUENCE_ENCODER_POLICY_VERSION = "pooled-causal-hourly-surface-lstm-v1"
SEQUENCE_STATE_SCHEMA_VERSION = "pooled-causal-sequence-state-v1"
SEQUENCE_DISTRIBUTION_SCHEMA_VERSION = "pooled-causal-distribution-v1"
SEQUENCE_EMBEDDING_SCHEMA_VERSION = "pooled-causal-embedding-v1"
SEQUENCE_PUBLICATION_SCHEMA_VERSION = "pooled-causal-sequence-publication-v1"
SEQUENCE_POINTER_SCHEMA_VERSION = "pooled-causal-sequence-pointer-v1"
SEQUENCE_MODEL_RECEIPT_VERSION = "pooled-causal-sequence-model-receipt-v1"

SUPPORTED_HORIZONS = ("1h", "4h", "1d", "1w")


@dataclass(frozen=True)
class SequenceEncoderConfig:
    """Versioned training and inference policy for the pooled encoder."""

    window_length: int = 32
    hidden_size: int = 32
    num_layers: int = 2
    dropout: float = 0.10
    ensemble_size: int = 3
    pretrain_epochs: int = 4
    supervised_epochs: int = 8
    batch_size: int = 128
    learning_rate: float = 1.0e-3
    weight_decay: float = 1.0e-4
    minimum_train_clusters: int = 160
    calibration_clusters: int = 48
    assessment_clusters: int = 48
    embargo_hours: int = 168
    random_state: int = 20260829
    horizons: tuple[str, ...] = SUPPORTED_HORIZONS
    feature_columns: tuple[str, ...] = SEQUENCE_FEATURE_COLUMNS
    policy_version: str = SEQUENCE_ENCODER_POLICY_VERSION

    def __post_init__(self) -> None:
        for name in (
            "window_length",
            "hidden_size",
            "num_layers",
            "ensemble_size",
            "pretrain_epochs",
            "supervised_epochs",
            "batch_size",
            "minimum_train_clusters",
            "calibration_clusters",
            "assessment_clusters",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")
        if self.hidden_size != len(EMBEDDING_COLUMNS):
            raise ValueError(
                "hidden_size must match the persisted embedding contract"
            )
        if not 0.0 <= self.dropout < 1.0:
            raise ValueError("dropout must be in [0, 1)")
        if self.learning_rate <= 0.0 or self.weight_decay < 0.0:
            raise ValueError("optimizer parameters are invalid")
        if self.embargo_hours < 0:
            raise ValueError("embargo_hours cannot be negative")
        if tuple(self.feature_columns) != SEQUENCE_FEATURE_COLUMNS:
            raise ValueError("feature_columns must use the frozen v1 order")
        invalid = set(self.horizons).difference(SUPPORTED_HORIZONS)
        if invalid:
            raise ValueError(f"Unsupported sequence horizons: {sorted(invalid)}")
        if self.policy_version != SEQUENCE_ENCODER_POLICY_VERSION:
            raise ValueError("Unsupported sequence encoder policy_version")

    def semantic_contract(self) -> Mapping[str, object]:
        payload = asdict(self)
        payload["horizons"] = list(self.horizons)
        payload["feature_columns"] = list(self.feature_columns)
        return payload

    @property
    def semantic_fingerprint(self) -> str:
        return semantic_metadata_fingerprint(self.semantic_contract())


__all__ = [
    "DISTRIBUTION_SCHEMA",
    "EMBEDDING_COLUMNS",
    "EMBEDDING_SCHEMA",
    "SEQUENCE_DISTRIBUTION_SCHEMA_VERSION",
    "SEQUENCE_EMBEDDING_SCHEMA_VERSION",
    "SEQUENCE_ENCODER_POLICY_VERSION",
    "SEQUENCE_FEATURE_COLUMNS",
    "SEQUENCE_MODEL_RECEIPT_VERSION",
    "SEQUENCE_POINTER_SCHEMA_VERSION",
    "SEQUENCE_PUBLICATION_SCHEMA_VERSION",
    "SEQUENCE_STATE_SCHEMA_VERSION",
    "STATE_SCHEMA",
    "SUPPORTED_HORIZONS",
    "SequenceEncoderConfig",
]
