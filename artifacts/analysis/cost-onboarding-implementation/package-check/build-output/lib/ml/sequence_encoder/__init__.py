"""Shared pooled causal sequence encoder for Loop B and Strategy.

The package is deliberately shadow-first.  Its publication contract can be
consumed by production processes, but a sequence generation never authorizes
orders and cannot replace either loop's existing model authority.
"""

from ml.sequence_encoder.contracts import (
    DISTRIBUTION_SCHEMA,
    EMBEDDING_SCHEMA,
    SEQUENCE_FEATURE_COLUMNS,
    STATE_SCHEMA,
    SequenceEncoderConfig,
)

__all__ = [
    "DISTRIBUTION_SCHEMA",
    "EMBEDDING_SCHEMA",
    "SEQUENCE_FEATURE_COLUMNS",
    "STATE_SCHEMA",
    "SequenceEncoderConfig",
]
