from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from ml.contracts import (
    ACTIVE,
    BLOCKED,
    IMPLEMENTED_BUT_QUARANTINED,
    FeatureSpec,
    MLContractError,
    READINESS_STATES,
)
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY
from ml.horizons import INTERNAL_HORIZON_ORDER


@dataclass(frozen=True)
class FeatureReadiness:
    """Deterministic operational evidence for one feature and horizon."""

    feature_name: str
    horizon: str
    state: str
    source_files: tuple[str, ...]
    observed_schema_versions: tuple[str, ...]
    observed_calculation_versions: tuple[str, ...]
    symbol_coverage: float
    first_safe_available_at: pd.Timestamp | None
    last_safe_available_at: pd.Timestamp | None
    eligible_decision_count: int
    null_rate: float
    stale_row_rate: float
    duplicate_key_failures: int
    minimum_coverage_passes: bool
    blocking_reason: str

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        for name in ("first_safe_available_at", "last_safe_available_at"):
            value = payload[name]
            payload[name] = value.isoformat() if value is not None else None
        return payload


@dataclass(frozen=True)
class ReadinessReport:
    """Feature-level report that never requires targets or lockbox rows."""

    feature_set_name: str
    horizon: str
    features: tuple[FeatureReadiness, ...]

    @property
    def state(self) -> str:
        states = {feature.state for feature in self.features}
        if BLOCKED in states:
            return BLOCKED
        if IMPLEMENTED_BUT_QUARANTINED in states:
            return IMPLEMENTED_BUT_QUARANTINED
        return ACTIVE

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        return tuple(
            f"{feature.feature_name}: {feature.blocking_reason}"
            for feature in self.features
            if feature.state != ACTIVE
        )

    def ensure_model_ready(self) -> None:
        if self.state != ACTIVE:
            raise MLContractError(
                f"Feature set {self.feature_set_name!r} is not ready for "
                f"{self.horizon}: "
                + "; ".join(self.blocking_reasons)
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "feature_set_name": self.feature_set_name,
            "horizon": self.horizon,
            "state": self.state,
            "features": [feature.as_dict() for feature in self.features],
        }


def evaluate_feature_readiness(
    decisions: pd.DataFrame,
    joined_features: pd.DataFrame,
    *,
    feature_set_name: str,
    horizon: str,
    feature_names: Sequence[str],
    source_frames: Mapping[str, pd.DataFrame | None],
    source_files: Mapping[str, Sequence[Path | str]] | None = None,
    natural_keys: Mapping[str, Sequence[str]] | None = None,
    activation_status: Mapping[str, str] | None = None,
    required_schema_versions: Mapping[str, Sequence[str]] | None = None,
    required_calculation_versions: Mapping[str, Sequence[str]] | None = None,
    minimum_eligible_decisions: int | Mapping[str, int] = 1,
    minimum_symbol_coverage: float | Mapping[str, float] = 1.0,
) -> ReadinessReport:
    """Evaluate source and joined coverage without reading any target column.

    ``source_frames`` and related mappings may be keyed by feature name or by
    source-family prefix (the text before ``__``). Feature order is preserved.
    """

    if not str(feature_set_name).strip():
        raise ValueError("feature_set_name is required")
    normalized_horizon = str(horizon).strip().lower()
    if normalized_horizon not in INTERNAL_HORIZON_ORDER:
        raise ValueError(
            "horizon must be " + ", ".join(INTERNAL_HORIZON_ORDER[:-1])
            + f", or {INTERNAL_HORIZON_ORDER[-1]}"
        )
    ordered_features = tuple(str(name) for name in feature_names)
    if not ordered_features or len(ordered_features) != len(set(ordered_features)):
        raise ValueError("feature_names must be non-empty and unique")
    _require_columns(
        decisions,
        ("decision_timestamp",),
        label="decision frame",
    )
    if len(decisions) != len(joined_features):
        raise MLContractError(
            "Readiness requires joined features at the same decision-row grain"
        )
    _validate_decision_alignment(decisions, joined_features)
    decision_times = pd.to_datetime(
        decisions["decision_timestamp"],
        utc=True,
        errors="coerce",
    )
    if decision_times.isna().any():
        raise MLContractError("Readiness decision timestamps are invalid")
    requested_symbols = _requested_symbols(decisions)

    registered_set_features: dict[str, FeatureSpec] = {}
    try:
        registered_set = DEFAULT_FEATURE_REGISTRY.feature_set(
            str(feature_set_name)
        )
    except MLContractError:
        registered_set = None
    # The 4h specs are horizon-scoped clones and intentionally do not replace
    # the same-named global feature specs used by the three existing horizons.
    if (
        registered_set is not None
        and normalized_horizon == "4h"
        and normalized_horizon in registered_set.applicable_horizons
    ):
        registered_set_features = {
            feature.name: feature for feature in registered_set.features
        }

    reports: list[FeatureReadiness] = []
    for feature_name in ordered_features:
        family = _family(feature_name)
        registered = registered_set_features.get(feature_name)
        if registered is None:
            try:
                registered = DEFAULT_FEATURE_REGISTRY.feature(feature_name)
            except MLContractError:
                registered = None
        policy_reasons: list[str] = []
        source = _lookup(source_frames, feature_name, family)
        paths = _render_source_files(
            _lookup(source_files or {}, feature_name, family) or ()
        )
        configured_activation_value = _lookup(
            activation_status or {},
            feature_name,
            family,
        )
        if configured_activation_value is None:
            if registered is None:
                configured_activation = BLOCKED
                policy_reasons.append("activation-status policy is absent")
            else:
                configured_activation = registered.activation_status
        else:
            configured_activation = str(configured_activation_value)
        if configured_activation not in READINESS_STATES:
            policy_reasons.append(
                f"activation-status policy is invalid: {configured_activation}"
            )
            configured_activation = BLOCKED

        required_schema_value = _lookup(
            required_schema_versions or {},
            feature_name,
            family,
        )
        if required_schema_value is None and registered is not None:
            required_schema_value = registered.required_schema_versions
        elif required_schema_value is None:
            policy_reasons.append("schema-version policy is absent")
        required_schemas = tuple(
            str(value)
            for value in (required_schema_value or ())
        )

        required_calculation_value = _lookup(
            required_calculation_versions or {},
            feature_name,
            family,
        )
        if required_calculation_value is None and registered is not None:
            required_calculation_value = (
                registered.required_calculation_versions
            )
        elif required_calculation_value is None:
            policy_reasons.append("calculation-version policy is absent")
        required_calculations = tuple(
            str(value)
            for value in (required_calculation_value or ())
        )
        if not required_calculations:
            policy_reasons.append("calculation-version policy is empty")

        natural_key_value = _lookup(
            natural_keys or {},
            feature_name,
            family,
        )
        feature_keys = tuple(
            str(value) for value in (natural_key_value or ())
        )
        if not feature_keys:
            policy_reasons.append("natural-key policy is absent")
        minimum_count = int(
            _threshold(
                minimum_eligible_decisions,
                feature_name=feature_name,
                family=family,
            )
        )
        minimum_symbols = float(
            _threshold(
                minimum_symbol_coverage,
                feature_name=feature_name,
                family=family,
            )
        )
        if minimum_count < 0:
            raise ValueError("minimum_eligible_decisions cannot be negative")
        if not 0.0 <= minimum_symbols <= 1.0:
            raise ValueError("minimum_symbol_coverage must be between 0 and 1")

        (
            observed_schema_versions,
            observed_calculation_versions,
            duplicate_count,
            structural_reasons,
        ) = _source_evidence(
            source,
            natural_key=feature_keys,
            required_schema_versions=required_schemas,
            required_calculation_versions=required_calculations,
            source_files=paths,
        )
        structural_reasons.extend(policy_reasons)
        family_availability_column = f"{family}__available_at"
        feature_availability_column = f"{feature_name}__available_at"
        availability_column = (
            feature_availability_column
            if feature_availability_column in joined_features.columns
            else family_availability_column
        )
        family_stale_column = f"{family}__is_stale"
        feature_stale_column = f"{feature_name}__is_stale"
        stale_column = (
            feature_stale_column
            if feature_stale_column in joined_features.columns
            else family_stale_column
        )
        if feature_name not in joined_features.columns:
            structural_reasons.append("joined model value is absent")
            values = pd.Series(
                pd.NA,
                index=joined_features.index,
                dtype="Float64",
            )
        else:
            values = pd.to_numeric(
                joined_features[feature_name],
                errors="coerce",
            )
        if availability_column not in joined_features.columns:
            structural_reasons.append(
                f"joined audit availability {availability_column} is absent"
            )
            availability = pd.Series(
                pd.NaT,
                index=joined_features.index,
                dtype="datetime64[ns, UTC]",
            )
        else:
            availability = pd.to_datetime(
                joined_features[availability_column],
                utc=True,
                errors="coerce",
            )
        if stale_column in joined_features.columns:
            invalid_stale = _invalid_boolean_mask(
                joined_features[stale_column]
            )
            if invalid_stale.any():
                structural_reasons.append(
                    f"joined audit staleness {stale_column} contains "
                    f"invalid values: {int(invalid_stale.sum())}"
                )
            stale = _explicit_true_mask(joined_features[stale_column])
        else:
            structural_reasons.append(
                f"joined audit staleness {stale_column} is absent"
            )
            stale = pd.Series(False, index=joined_features.index)

        decision_clock = pd.Series(
            decision_times.to_numpy(),
            index=joined_features.index,
            dtype="datetime64[ns, UTC]",
        )
        value_without_availability = values.notna() & availability.isna()
        if value_without_availability.any():
            structural_reasons.append(
                "joined model values lack audit availability: "
                f"{int(value_without_availability.sum())}"
            )
        future_availability = (
            availability.notna() & availability.gt(decision_clock)
        )
        if future_availability.any():
            structural_reasons.append(
                "joined audit availability exceeds its decision: "
                f"{int(future_availability.sum())}"
            )
        eligible = (
            values.notna()
            & availability.notna()
            & ~future_availability
            & ~stale
        )
        symbol_coverage = _symbol_coverage(
            decisions,
            eligible=eligible,
            requested_symbols=requested_symbols,
        )
        eligible_count = int(eligible.sum())
        null_rate = float(values.isna().mean()) if len(values) else 1.0
        stale_rate = float(stale.mean()) if len(stale) else 0.0
        safe_availability = availability.loc[
            eligible & availability.notna()
        ]
        first_safe = (
            pd.Timestamp(safe_availability.min())
            if not safe_availability.empty
            else None
        )
        last_safe = (
            pd.Timestamp(safe_availability.max())
            if not safe_availability.empty
            else None
        )
        source_has_rows = source is not None and not source.empty
        coverage_passes = (
            source_has_rows
            and eligible_count >= minimum_count
            and symbol_coverage >= minimum_symbols
        )
        coverage_reasons: list[str] = []
        if source is not None and source.empty:
            coverage_reasons.append("source frame contains no rows")
        if eligible_count < minimum_count:
            coverage_reasons.append(
                f"eligible decisions {eligible_count} < {minimum_count}"
            )
        if symbol_coverage < minimum_symbols:
            coverage_reasons.append(
                "symbol coverage "
                f"{symbol_coverage:.6f} < {minimum_symbols:.6f}"
            )

        if structural_reasons:
            state = BLOCKED
            reasons = structural_reasons
        elif configured_activation == BLOCKED:
            state = BLOCKED
            reasons = ["registry activation is BLOCKED"]
        elif (
            configured_activation != ACTIVE
            or not coverage_passes
        ):
            state = IMPLEMENTED_BUT_QUARANTINED
            reasons = []
            if configured_activation != ACTIVE:
                reasons.append(
                    f"registry activation is {configured_activation}"
                )
            reasons.extend(coverage_reasons)
            if not reasons:
                reasons.append("coverage/readiness policy is not qualified")
        else:
            state = ACTIVE
            reasons = []

        reports.append(
            FeatureReadiness(
                feature_name=feature_name,
                horizon=normalized_horizon,
                state=state,
                source_files=paths,
                observed_schema_versions=observed_schema_versions,
                observed_calculation_versions=(
                    observed_calculation_versions
                ),
                symbol_coverage=symbol_coverage,
                first_safe_available_at=first_safe,
                last_safe_available_at=last_safe,
                eligible_decision_count=eligible_count,
                null_rate=null_rate,
                stale_row_rate=stale_rate,
                duplicate_key_failures=duplicate_count,
                minimum_coverage_passes=coverage_passes,
                blocking_reason="; ".join(reasons),
            )
        )
    return ReadinessReport(
        feature_set_name=str(feature_set_name),
        horizon=normalized_horizon,
        features=tuple(reports),
    )


def _source_evidence(
    source: pd.DataFrame | None,
    *,
    natural_key: Sequence[str],
    required_schema_versions: Sequence[str],
    required_calculation_versions: Sequence[str],
    source_files: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...], int, list[str]]:
    reasons: list[str] = []
    if source is None:
        reasons.append("required source frame is absent")
        return (), (), 0, reasons
    if source.columns.has_duplicates:
        reasons.append("source contains duplicate columns")
    duplicate_count = 0
    if natural_key:
        missing_key = sorted(set(natural_key).difference(source.columns))
        if missing_key:
            reasons.append(
                "source natural-key columns are absent: "
                + ", ".join(missing_key)
            )
        else:
            normalized_key, key_reasons = _normalized_natural_key(
                source,
                natural_key,
            )
            reasons.extend(key_reasons)
            duplicate_count = int(
                normalized_key.duplicated(list(natural_key)).sum()
            )
            if duplicate_count:
                reasons.append(
                    f"duplicate natural keys: {duplicate_count}"
                )
    observed_schemas = _observed_versions(
        source,
        ("schema_version", "source_schema_version"),
    )
    observed_calculations = _observed_versions(
        source,
        ("calculation_version",),
    )
    if required_schema_versions and not observed_schemas:
        reasons.append("required schema version evidence is absent")
    elif required_schema_versions:
        invalid = sorted(
            set(observed_schemas).difference(required_schema_versions)
        )
        if invalid:
            reasons.append(
                "unsupported schema versions: " + ", ".join(invalid)
            )
    if required_calculation_versions and not observed_calculations:
        reasons.append("required calculation version evidence is absent")
    elif required_calculation_versions:
        invalid = sorted(
            set(observed_calculations).difference(
                required_calculation_versions
            )
        )
        if invalid:
            reasons.append(
                "unsupported calculation versions: "
                + ", ".join(invalid)
            )
    missing_paths = [
        path
        for path in source_files
        if not Path(path).is_file()
    ]
    if missing_paths:
        reasons.append(
            "source files are missing: " + ", ".join(missing_paths)
        )
    return (
        observed_schemas,
        observed_calculations,
        duplicate_count,
        reasons,
    )


def _normalized_natural_key(
    source: pd.DataFrame,
    columns: Sequence[str],
) -> tuple[pd.DataFrame, list[str]]:
    """Normalize semantic key values before checking duplicate identities."""

    normalized = source.loc[:, list(columns)].copy()
    reasons: list[str] = []
    for column in columns:
        lowered = column.strip().lower()
        values = normalized[column]
        temporal = (
            lowered.endswith(("_at", "_timestamp", "_date"))
            or lowered
            in {
                "window_start",
                "window_end",
                "observation_date",
                "realtime_start",
                "realtime_end",
                "period_end_date",
            }
        )
        if temporal:
            converted = pd.to_datetime(values, utc=True, errors="coerce")
            invalid = values.notna() & converted.isna()
            if invalid.any():
                reasons.append(
                    f"source natural key {column} contains invalid timestamps: "
                    f"{int(invalid.sum())}"
                )
            normalized[column] = converted
        elif pd.api.types.is_string_dtype(values.dtype) or values.dtype == object:
            normalized[column] = (
                values.astype("string").str.strip().str.upper()
            )
    missing = normalized.isna().any(axis=1)
    if missing.any():
        reasons.append(
            "source natural keys contain missing values: "
            f"{int(missing.sum())}"
        )
    return normalized, reasons


def _observed_versions(
    frame: pd.DataFrame,
    candidates: Sequence[str],
) -> tuple[str, ...]:
    for column in candidates:
        if column in frame.columns:
            return tuple(
                sorted(
                    set(
                        frame[column]
                        .dropna()
                        .astype(str)
                        .str.strip()
                    )
                )
            )
    return ()


def _requested_symbols(frame: pd.DataFrame) -> tuple[str, ...]:
    if "symbol" not in frame.columns:
        return ()
    return tuple(
        sorted(
            set(
                frame["symbol"]
                .dropna()
                .astype(str)
                .str.strip()
                .str.upper()
            )
        )
    )


def _symbol_coverage(
    decisions: pd.DataFrame,
    *,
    eligible: pd.Series,
    requested_symbols: Sequence[str],
) -> float:
    if not requested_symbols:
        return 1.0 if bool(eligible.any()) else 0.0
    symbols = (
        decisions["symbol"].astype(str).str.strip().str.upper()
    )
    covered = set(symbols.loc[eligible.to_numpy(dtype=bool)])
    return float(len(covered) / len(requested_symbols))


def _lookup(
    values: Mapping[str, object],
    feature_name: str,
    family: str,
) -> object | None:
    if feature_name in values:
        return values[feature_name]
    return values.get(family)


def _threshold(
    value: int | float | Mapping[str, int | float],
    *,
    feature_name: str,
    family: str,
) -> int | float:
    if isinstance(value, Mapping):
        selected = _lookup(value, feature_name, family)
        if selected is None:
            raise ValueError(
                f"No readiness threshold for {feature_name}"
            )
        return selected
    return value


def _family(feature_name: str) -> str:
    if "__" not in feature_name:
        raise ValueError(
            f"Feature name lacks a registered family prefix: {feature_name}"
        )
    return feature_name.split("__", 1)[0]


def _render_source_files(
    paths: Sequence[Path | str],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            dict.fromkeys(
                str(Path(path).resolve()) for path in paths
            )
        )
    )


def _validate_decision_alignment(
    decisions: pd.DataFrame,
    joined_features: pd.DataFrame,
) -> None:
    identity_columns = [
        column
        for column in ("symbol", "horizon", "decision_timestamp")
        if column in decisions.columns
    ]
    missing = sorted(set(identity_columns).difference(joined_features.columns))
    if missing:
        raise MLContractError(
            "Joined feature frame is missing decision identity: "
            + ", ".join(missing)
        )
    for column in identity_columns:
        if column == "decision_timestamp":
            left = pd.to_datetime(
                decisions[column],
                utc=True,
                errors="coerce",
            ).reset_index(drop=True)
            right = pd.to_datetime(
                joined_features[column],
                utc=True,
                errors="coerce",
            ).reset_index(drop=True)
        else:
            left = (
                decisions[column]
                .astype("string")
                .str.strip()
                .str.upper()
                .reset_index(drop=True)
            )
            right = (
                joined_features[column]
                .astype("string")
                .str.strip()
                .str.upper()
                .reset_index(drop=True)
            )
        if left.isna().any() or right.isna().any() or not left.equals(right):
            raise MLContractError(
                f"Joined feature frame is misaligned on {column}"
            )


def _require_columns(
    frame: pd.DataFrame,
    columns: Sequence[str],
    *,
    label: str,
) -> None:
    missing = sorted(set(columns).difference(frame.columns))
    if missing:
        raise MLContractError(
            f"{label} is missing required columns: "
            + ", ".join(missing)
        )


def _explicit_true_mask(values: pd.Series) -> pd.Series:
    normalized = values.astype("string").str.strip().str.lower()
    return normalized.isin({"true", "1", "1.0", "yes", "y"}).fillna(False)


def _invalid_boolean_mask(values: pd.Series) -> pd.Series:
    normalized = values.astype("string").str.strip().str.lower()
    valid = {
        "true",
        "1",
        "1.0",
        "yes",
        "y",
        "false",
        "0",
        "0.0",
        "no",
        "n",
    }
    return values.notna() & ~normalized.isin(valid)
