from __future__ import annotations

import re

import pandas as pd

from ml.contracts import FeatureSet, MLContractError
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY, FeatureRegistry
from ml.horizons import feature_contract_horizon

# Compatibility import name; FeatureSet is the sole semantic/model set abstraction.
ModelFeatureSet = FeatureSet

_FORBIDDEN_EXACT_SOURCE_COLUMNS = {
    "id",
    "provider",
    "source",
    "path",
    "url",
    "accession",
    "accession_number",
    "cik",
    "row_index",
    "sequence",
    "event_sequence",
    "timestamp",
    "available_at",
    "fetched_at",
    "observed_at",
    "calculated_at",
    "generated_at",
    "freshness_age",
    "freshness_status",
    "coverage",
    "coverage_rate",
    "quality",
    "quality_status",
    "surface_quality_pass",
    "constituent_complete",
    "source_stale",
    "target",
    "target_price",
    "future_return",
    "label",
    "prediction",
    "evaluation",
}
_FORBIDDEN_SOURCE_MARKERS = (
    "target_",
    "future_",
    "label_",
    "prediction_",
    "evaluation_",
    "accession",
    "provider_id",
    "contract_id",
    "request_",
    "source_file",
    "file_path",
    "receipt_age",
    "freshness_age",
    "coverage_rate",
)
_IDENTITY_COLUMN = re.compile(
    r"(^|_)(?:id|uuid|hash|row_number|sequence_number)(?:$|_)",
    flags=re.IGNORECASE,
)


def resolve_model_feature_set(
    feature_set: str,
    *,
    horizon: str | None = None,
    registry: FeatureRegistry = DEFAULT_FEATURE_REGISTRY,
) -> ModelFeatureSet:
    """Resolve one active registry-backed semantic feature set."""

    normalized = str(feature_set).strip()
    resolved = registry.feature_set(
        normalized,
        require_active=True,
        horizon=(
            feature_contract_horizon(horizon)
            if horizon is not None
            else None
        ),
    )
    _validate_model_specs(resolved)
    return resolved


def model_matrix_for_feature_set(
    frame: pd.DataFrame,
    feature_set: ModelFeatureSet,
    *,
    include_symbol: bool = False,
) -> pd.DataFrame:
    """Project only the ordered semantic allowlist after a leakage safety check."""

    feature_set.ensure_model_eligible()
    _validate_model_specs(feature_set)
    required = list(feature_set.names)
    if include_symbol:
        required.append("symbol")
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise MLContractError("Model frame is missing columns: " + ", ".join(missing))
    return frame.loc[:, required].copy()


def model_categorical_features(
    feature_set: ModelFeatureSet,
    *,
    include_symbol: bool,
) -> tuple[str, ...]:
    return (
        *feature_set.categorical_features,
        *(("symbol",) if include_symbol else ()),
    )


def _validate_model_specs(feature_set: ModelFeatureSet) -> None:
    unsafe: list[str] = []
    for feature in feature_set.features:
        source = feature.source_column.strip().lower()
        if not feature.is_model_value:
            unsafe.append(feature.name)
            continue
        if (
            source in _FORBIDDEN_EXACT_SOURCE_COLUMNS
            or any(marker in source for marker in _FORBIDDEN_SOURCE_MARKERS)
            or _IDENTITY_COLUMN.search(source)
        ):
            unsafe.append(feature.name)
    if unsafe:
        raise MLContractError(
            "Audit, identity, timing, quality, or post-decision fields cannot enter "
            "the model matrix: "
            + ", ".join(unsafe)
        )
