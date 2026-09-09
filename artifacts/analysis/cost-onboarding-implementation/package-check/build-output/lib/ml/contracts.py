from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Final

from ml.artifacts import canonical_metadata_json, semantic_metadata_fingerprint


class MLContractError(ValueError):
    """Raised when model input violates a versioned ML contract."""


ACTIVE: Final = "ACTIVE"
IMPLEMENTED_BUT_QUARANTINED: Final = "IMPLEMENTED_BUT_QUARANTINED"
BLOCKED: Final = "BLOCKED"
READINESS_STATES: Final = (ACTIVE, IMPLEMENTED_BUT_QUARANTINED, BLOCKED)

USABLE_NOW: Final = "USABLE_NOW"
NEEDS_NORMALIZATION: Final = "NEEDS_NORMALIZATION"
INSUFFICIENT_COVERAGE: Final = "INSUFFICIENT_COVERAGE"
NEEDS_POINT_IN_TIME_HISTORY: Final = "NEEDS_POINT_IN_TIME_HISTORY"

MODEL_VALUE: Final = "MODEL_VALUE"
AUDIT_CONTROL: Final = "AUDIT_CONTROL"
ALLOWED_HORIZONS: Final = ("1h", "4h", "1d", "1w")


@dataclass(frozen=True)
class CalculationSpec:
    """Versioned contract for one upstream calculation family."""

    source_family: str
    calculation_name: str
    allowed_versions: tuple[str, ...]
    mode_column: str = ""
    allowed_modes: tuple[str, ...] = ()
    allowed_schema_versions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_family:
            raise ValueError("source_family is required")
        if not self.calculation_name:
            raise ValueError("calculation_name is required")
        if not self.allowed_versions:
            raise ValueError("allowed_versions cannot be empty")
        if bool(self.mode_column) != bool(self.allowed_modes):
            raise ValueError("mode_column and allowed_modes must be declared together")


@dataclass(frozen=True)
class FeatureSpec:
    """One explicit semantic definition for a persisted model value or control."""

    name: str
    source_family: str
    source_column: str
    dtype: str = "float64"
    applicable_horizons: tuple[str, ...] = ("1h", "1d", "1w")
    provider_policy: str = "canonical-provider-v1"
    source_timeframe: str = "horizon-source-timeframe"
    source_grain: str = "completed-bar"
    required_calculation_versions: tuple[str, ...] = ()
    required_schema_versions: tuple[str, ...] = ()
    availability_rule: str = "persisted-available-at-no-later-than-decision"
    availability_rule_version: str = "available-at-v1"
    processing_delay_seconds: int = 0
    freshness_by_horizon: tuple[tuple[str, str], ...] = ()
    missing_policy: str = "missing-no-backfill-training-median-indicator-v1"
    transform_version: str = "identity-v1"
    coverage_policy: str = "explicit-readiness-v1"
    readiness_policy_version: str = "readiness-v1"
    audit_classification: str = USABLE_NOW
    activation_status: str = ACTIVE
    value_role: str = MODEL_VALUE
    recommended_preprocessing: str = "median-clip-robust-scale"
    is_aggregate_score: bool = False
    is_confidence_field: bool = False

    def __post_init__(self) -> None:
        expected_prefix = f"{self.source_family}__"
        if not self.name.startswith(expected_prefix):
            raise ValueError(
                f"Feature {self.name!r} must use namespace prefix {expected_prefix!r}."
            )
        if not self.source_column:
            raise ValueError("source_column is required")
        invalid_horizons = set(self.applicable_horizons).difference(
            ALLOWED_HORIZONS
        )
        if invalid_horizons:
            raise ValueError(f"Invalid feature horizons: {sorted(invalid_horizons)}")
        if self.processing_delay_seconds < 0:
            raise ValueError("processing_delay_seconds cannot be negative")
        if self.activation_status not in READINESS_STATES:
            raise ValueError(
                f"activation_status must be one of {', '.join(READINESS_STATES)}"
            )
        if self.value_role not in {MODEL_VALUE, AUDIT_CONTROL}:
            raise ValueError("value_role must be MODEL_VALUE or AUDIT_CONTROL")
        freshness_horizons = tuple(horizon for horizon, _ in self.freshness_by_horizon)
        if len(freshness_horizons) != len(set(freshness_horizons)):
            raise ValueError("freshness_by_horizon contains duplicate horizons")
        invalid_freshness_horizons = set(freshness_horizons).difference(
            ALLOWED_HORIZONS
        )
        if invalid_freshness_horizons:
            raise ValueError(
                "Invalid freshness horizons: "
                f"{sorted(invalid_freshness_horizons)}"
            )

    @property
    def is_model_value(self) -> bool:
        return self.value_role == MODEL_VALUE

    def freshness_limit(self, horizon: str) -> str:
        return dict(self.freshness_by_horizon).get(horizon, "not-applicable")

    def semantic_dict(self) -> dict[str, object]:
        """Return stable manifest metadata; tuple order is part of the contract."""

        payload = asdict(self)
        payload["is_model_value"] = self.is_model_value
        return json.loads(canonical_metadata_json(payload))


@dataclass(frozen=True)
class FeatureSet:
    """The single ordered, versioned model-input contract used by Loop B."""

    name: str
    features: tuple[FeatureSpec, ...]
    version: str = "1.0.0"
    applicable_horizons: tuple[str, ...] = ("1h", "1d", "1w")
    activation_status: str = ACTIVE
    blocking_reason: str = ""

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("name is required")
        if not self.version:
            raise ValueError("version is required")
        if not self.features:
            raise ValueError("features cannot be empty")
        names = self.names
        if len(names) != len(set(names)):
            raise ValueError(f"Feature set {self.name!r} contains duplicate names.")
        if self.activation_status not in READINESS_STATES:
            raise ValueError(
                f"activation_status must be one of {', '.join(READINESS_STATES)}"
            )
        if self.activation_status != ACTIVE and not self.blocking_reason:
            raise ValueError("Inactive feature sets require a blocking_reason")
        invalid_horizons = set(self.applicable_horizons).difference(
            ALLOWED_HORIZONS
        )
        if invalid_horizons:
            raise ValueError(f"Invalid feature-set horizons: {sorted(invalid_horizons)}")
        controls = [feature.name for feature in self.features if not feature.is_model_value]
        if controls:
            raise ValueError(
                "Audit-only controls cannot be members of a model feature set: "
                + ", ".join(controls)
            )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(feature.name for feature in self.features)

    @property
    def numeric_features(self) -> tuple[str, ...]:
        return tuple(
            feature.name
            for feature in self.features
            if feature.dtype not in {"category", "string", "bool"}
        )

    @property
    def categorical_features(self) -> tuple[str, ...]:
        return tuple(
            feature.name
            for feature in self.features
            if feature.dtype in {"category", "string", "bool"}
        )

    @property
    def transform_versions(self) -> dict[str, str]:
        return {
            feature.name: feature.transform_version
            for feature in self.features
        }

    def for_family(self, source_family: str) -> tuple[FeatureSpec, ...]:
        return tuple(
            feature for feature in self.features if feature.source_family == source_family
        )

    def ensure_model_eligible(self, *, horizon: str | None = None) -> None:
        if self.activation_status != ACTIVE:
            detail = self.blocking_reason or self.activation_status
            raise MLContractError(
                f"Feature set {self.name!r} is not active for modeling: {detail}"
            )
        if horizon is not None and horizon not in self.applicable_horizons:
            raise MLContractError(
                f"Feature set {self.name!r} is not applicable to horizon {horizon!r}."
            )
        blocked = [
            feature.name
            for feature in self.features
            if feature.activation_status != ACTIVE
            or (horizon is not None and horizon not in feature.applicable_horizons)
        ]
        if blocked:
            raise MLContractError(
                f"Feature set {self.name!r} contains unavailable features: "
                + ", ".join(blocked)
            )

    def semantic_contract(self) -> dict[str, object]:
        return {
            "feature_set_name": self.name,
            "feature_set_version": self.version,
            "applicable_horizons": list(self.applicable_horizons),
            "ordered_features": [
                feature.semantic_dict() for feature in self.features
            ],
        }

    @property
    def semantic_fingerprint(self) -> str:
        return semantic_metadata_fingerprint(self.semantic_contract())
