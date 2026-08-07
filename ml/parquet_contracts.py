from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from datafetching.ids import (
    ID_COLUMN,
    PARQUET_CONTROL_PLANE_COLUMNS,
    add_readable_id,
    is_opaque_identifier,
)

TEXT = pa.string()
FLOAT = pa.float64()
INTEGER = pa.int64()
SMALL_INTEGER = pa.int8()
BOOLEAN = pa.bool_()
UTC_TIMESTAMP = pa.timestamp("ns", tz="UTC")

_IDENTITY_TERM = re.compile(
    r"(?:^|_)(?:hash|digest|fingerprint|checksum|sha(?:1|224|256|384|512)|"
    r"receipt|lineage|identity|content_address|uuid|guid)(?:_|$)",
    re.IGNORECASE,
)

# Loop coordination belongs in the small atomic JSON control file, never in a
# row-oriented data artifact. Keep this list narrow enough that ordinary data
# columns such as ``status`` remain available to predictions and evaluations.
CONTROL_PLANE_COLUMN_NAMES = PARQUET_CONTROL_PLANE_COLUMNS

# These timestamps are useful while a sample is assembled and validated, but
# would only duplicate persisted decision/run metadata afterward.
NON_PERSISTED_SAMPLE_WORKFLOW_COLUMNS = frozenset(
    {
        "feature_available_at",
        "feature_computed_at",
        "materialized_at",
    }
)


def _identity_column_name(name: str) -> bool:
    normalized = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])",
        "_",
        str(name).strip(),
    ).lower()
    return normalized.endswith(("_id", "_ids")) or bool(
        _IDENTITY_TERM.search(normalized)
    )


def _schema(fields: Iterable[tuple[str, pa.DataType]]) -> pa.Schema:
    schema = pa.schema(
        [pa.field(name, data_type, nullable=True) for name, data_type in fields]
    )
    if schema.names.count(ID_COLUMN) != 1 or schema.names[0] != ID_COLUMN:
        raise ValueError(
            "Every persisted Parquet schema must start with exactly one id"
        )
    if schema.field(ID_COLUMN).type != TEXT:
        raise TypeError("The Duckets id column must be readable text")
    forbidden = [
        name
        for name in schema.names
        if name != ID_COLUMN
        and _identity_column_name(name)
    ]
    if forbidden:
        raise ValueError(
            "Persisted ML schemas contain forbidden identity columns: "
            + ", ".join(sorted(forbidden))
        )
    control_plane = sorted(
        set(schema.names).intersection(CONTROL_PLANE_COLUMN_NAMES)
    )
    if control_plane:
        raise ValueError(
            "Persisted ML schemas contain control-plane columns: "
            + ", ".join(control_plane)
        )
    workflow_only = sorted(
        set(schema.names).intersection(NON_PERSISTED_SAMPLE_WORKFLOW_COLUMNS)
    )
    if workflow_only:
        raise ValueError(
            "Persisted ML schemas contain workflow-only columns: "
            + ", ".join(workflow_only)
        )
    return schema


SAMPLE_BASE_SCHEMA = _schema(
    (
        ("id", TEXT),
        ("symbol", TEXT),
        ("venue", TEXT),
        ("currency", TEXT),
        ("provider", TEXT),
        ("timeframe", TEXT),
        ("exchange_calendar", TEXT),
        ("exchange_session", UTC_TIMESTAMP),
        ("horizon", TEXT),
        ("bar_timestamp", UTC_TIMESTAMP),
        ("bar_end_timestamp", UTC_TIMESTAMP),
        ("decision_timestamp", UTC_TIMESTAMP),
        ("information_available_at", UTC_TIMESTAMP),
        ("target_window_start", UTC_TIMESTAMP),
        ("target_window_end", UTC_TIMESTAMP),
        ("actionable_until", UTC_TIMESTAMP),
        ("label_available_at", UTC_TIMESTAMP),
        ("target_definition_version", TEXT),
        ("target_specification", TEXT),
        ("target_open", FLOAT),
        ("target_close", FLOAT),
        ("forward_raw_return", FLOAT),
        ("forward_cost_adjusted_return", FLOAT),
        ("target_cost_adjusted_positive", SMALL_INTEGER),
        ("label_status", TEXT),
        ("label_exclusion_reason", TEXT),
        ("previous_period_direction", FLOAT),
        ("assumed_round_trip_cost", FLOAT),
    )
)

PREDICTION_SCHEMA = _schema(
    (
        ("id", TEXT),
        ("symbol", TEXT),
        ("provider", TEXT),
        ("horizon", TEXT),
        ("decision_timestamp", UTC_TIMESTAMP),
        ("information_available_at", UTC_TIMESTAMP),
        ("target_window_start", UTC_TIMESTAMP),
        ("target_window_end", UTC_TIMESTAMP),
        ("actionable_until", UTC_TIMESTAMP),
        ("target_definition_version", TEXT),
        ("target_specification", TEXT),
        ("prediction_created_at", UTC_TIMESTAMP),
        ("model_name", TEXT),
        ("model_version", TEXT),
        ("calibration_method", TEXT),
        ("prediction_mode", TEXT),
        ("prediction_status", TEXT),
        ("assumed_round_trip_cost", FLOAT),
        ("raw_probability", FLOAT),
        ("calibrated_probability", FLOAT),
    )
)

EVALUATION_SCHEMA = _schema(
    (
        ("id", TEXT),
        ("symbol", TEXT),
        ("provider", TEXT),
        ("horizon", TEXT),
        ("decision_timestamp", UTC_TIMESTAMP),
        ("target_window_start", UTC_TIMESTAMP),
        ("target_window_end", UTC_TIMESTAMP),
        ("prediction_created_at", UTC_TIMESTAMP),
        ("evaluated_at", UTC_TIMESTAMP),
        ("model_name", TEXT),
        ("model_version", TEXT),
        ("prediction_mode", TEXT),
        ("evaluation_status", TEXT),
        ("target_definition_version", TEXT),
        ("target_specification", TEXT),
        ("assumed_round_trip_cost", FLOAT),
        ("observed_target", SMALL_INTEGER),
        ("observed_forward_raw_return", FLOAT),
        ("observed_forward_cost_adjusted_return", FLOAT),
        ("raw_probability", FLOAT),
        ("calibrated_probability", FLOAT),
        ("raw_log_loss", FLOAT),
        ("log_loss", FLOAT),
        ("raw_brier_score", FLOAT),
        ("brier_score", FLOAT),
        ("prediction_correct_0_5", BOOLEAN),
    )
)

MONITORING_SCHEMA = _schema(
    (
        ("id", TEXT),
        ("monitored_at", UTC_TIMESTAMP),
        ("category", TEXT),
        ("metric_name", TEXT),
        ("scope_type", TEXT),
        ("scope_value", TEXT),
        ("status", TEXT),
        ("observed_value", FLOAT),
        ("reference_value", FLOAT),
        ("unit", TEXT),
        ("evidence_row_count", INTEGER),
        ("window_start", UTC_TIMESTAMP),
        ("window_end", UTC_TIMESTAMP),
        ("details", TEXT),
    )
)

INTELLIGENCE_SCHEMA = _schema(
    (
        ("id", TEXT),
        ("symbol", TEXT),
        ("horizon", TEXT),
        ("decision_timestamp", UTC_TIMESTAMP),
        ("forecast_created_at", UTC_TIMESTAMP),
        ("information_available_at", UTC_TIMESTAMP),
        ("target_window_start", UTC_TIMESTAMP),
        ("target_window_end", UTC_TIMESTAMP),
        ("actionable_until", UTC_TIMESTAMP),
        ("target_definition_version", TEXT),
        ("probability_up", FLOAT),
        ("probability_down", FLOAT),
        ("actionability_status", TEXT),
        ("operational_status", TEXT),
        ("model_evidence_status", TEXT),
        ("live_evidence_status", TEXT),
        ("intelligence_status", TEXT),
        ("model_name", TEXT),
        ("completed_decision_count", INTEGER),
        ("minimum_live_decision_count", INTEGER),
        ("automated_action_allowed", BOOLEAN),
        ("limitations", TEXT),
        ("schema_version", TEXT),
    )
)

STRATEGY_CANDIDATE_SCHEMA = _schema(
    (
        ("id", TEXT),
        ("symbol", TEXT),
        ("horizon", TEXT),
        ("decision_timestamp", UTC_TIMESTAMP),
        ("information_available_at", UTC_TIMESTAMP),
        ("target_window_start", UTC_TIMESTAMP),
        ("target_window_end", UTC_TIMESTAMP),
        ("entry_available_at", UTC_TIMESTAMP),
        ("strategy_name", TEXT),
        ("strategy_display_name", TEXT),
        ("strategy_family", TEXT),
        ("candidate_key", TEXT),
        ("account_approval", TEXT),
        ("authorization_status", TEXT),
        ("construction_status", TEXT),
        ("risk_form", TEXT),
        ("expiration_structure", TEXT),
        ("stock_requirement", TEXT),
        ("cash_requirement", TEXT),
        ("lifecycle", BOOLEAN),
        ("front_expiration", UTC_TIMESTAMP),
        ("back_expiration", UTC_TIMESTAMP),
        ("front_days_to_expiration", FLOAT),
        ("back_days_to_expiration", FLOAT),
        ("target_elapsed_hours", FLOAT),
        ("width_steps", INTEGER),
        ("leg_count", INTEGER),
        ("legs_json", TEXT),
        ("underlying_price", FLOAT),
        ("entry_cash_flow", FLOAT),
        ("entry_fees", FLOAT),
        ("entry_net_credit", FLOAT),
        ("entry_net_debit", FLOAT),
        ("max_profit", FLOAT),
        ("max_loss", FLOAT),
        ("capital_required", FLOAT),
        ("risk_calculation_status", TEXT),
        ("net_delta", FLOAT),
        ("net_gamma", FLOAT),
        ("net_theta", FLOAT),
        ("net_vega", FLOAT),
        ("mean_relative_spread", FLOAT),
        ("max_relative_spread", FLOAT),
        ("minimum_open_interest", FLOAT),
        ("total_volume", FLOAT),
        ("entry_debit_to_underlying", FLOAT),
        ("max_loss_to_underlying", FLOAT),
        ("net_delta_per_share", FLOAT),
        ("surface_quality_pass", BOOLEAN),
        ("all_option_quotes_valid", BOOLEAN),
        ("liquidity_policy_pass", BOOLEAN),
        ("stock_quote_quality_pass", BOOLEAN),
        ("maximum_quote_staleness_seconds", FLOAT),
        ("quality_observations_json", TEXT),
        ("market_expected_absolute_move", FLOAT),
        ("market_expected_realized_volatility", FLOAT),
        ("market_uncertainty", FLOAT),
        ("market_trend_persistence", FLOAT),
        ("market_mean_reversion_tendency", FLOAT),
        ("raw_profit_probability", FLOAT),
        ("calibrated_profit_probability", FLOAT),
        ("direction_probability_up", FLOAT),
        ("direction_alignment", FLOAT),
        ("expected_net_profit", FLOAT),
        ("expected_return_on_risk", FLOAT),
        ("decision_score", FLOAT),
        ("candidate_rank", INTEGER),
        ("pricing_mode", TEXT),
        ("pricing_status", TEXT),
        ("pricing_leg_coverage", FLOAT),
        ("pricing_missing_reason", TEXT),
        ("pricing_candidate_edge", FLOAT),
        ("pricing_edge_to_friction", FLOAT),
        ("pricing_uncertainty", FLOAT),
        ("pricing_edge_minus_scenario_expected_profit", FLOAT),
        ("model_version", TEXT),
        ("model_status", TEXT),
        ("registry_version", TEXT),
        ("candidate_policy_version", TEXT),
        ("model_policy_version", TEXT),
        ("ranking_policy_version", TEXT),
    )
)

STRATEGY_AUDIT_SCHEMA = _schema(
    (
        ("id", TEXT),
        ("symbol", TEXT),
        ("horizon", TEXT),
        ("decision_timestamp", UTC_TIMESTAMP),
        ("strategy_name", TEXT),
        ("strategy_display_name", TEXT),
        ("strategy_family", TEXT),
        ("account_approval", TEXT),
        ("authorization_status", TEXT),
        ("construction_status", TEXT),
        ("candidate_count", INTEGER),
        ("reason", TEXT),
        ("registry_version", TEXT),
        ("candidate_policy_version", TEXT),
    )
)

OPTION_PRICING_SAMPLE_SCHEMA = _schema(
    (
        ("id", TEXT),
        ("symbol", TEXT),
        ("source_provider", TEXT),
        ("prediction_mode", TEXT),
        ("call_put", TEXT),
        ("contract_symbol", TEXT),
        ("expiration_date", UTC_TIMESTAMP),
        ("target_snapshot_for", UTC_TIMESTAMP),
        ("source_snapshot_for", UTC_TIMESTAMP),
        ("source_available_at", UTC_TIMESTAMP),
        ("source_quote_timestamp", UTC_TIMESTAMP),
        ("source_quote_staleness_seconds", FLOAT),
        ("observed_quote_timestamp", UTC_TIMESTAMP),
        ("observed_available_at", UTC_TIMESTAMP),
        ("underlying_price", FLOAT),
        ("strike", FLOAT),
        ("multiplier", FLOAT),
        ("risk_free_rate", FLOAT),
        ("rate_source_at", UTC_TIMESTAMP),
        ("lagged_implied_volatility", FLOAT),
        ("volatility_source_at", UTC_TIMESTAMP),
        ("target_years_to_expiration", FLOAT),
        ("dividend_yield", FLOAT),
        ("dividend_source_at", UTC_TIMESTAMP),
        ("source_mid", FLOAT),
        ("observed_bid", FLOAT),
        ("observed_ask", FLOAT),
        ("observed_mid", FLOAT),
        ("bid_ask_spread", FLOAT),
        ("black_scholes_price", FLOAT),
        ("normalized_residual", FLOAT),
        ("sample_status", TEXT),
        ("exclusion_reason", TEXT),
        ("expiration_policy_version", TEXT),
        ("timing_policy_version", TEXT),
        ("rate_policy_version", TEXT),
        ("dividend_policy_version", TEXT),
        ("volatility_policy_version", TEXT),
        ("contract_policy_version", TEXT),
        ("schema_version", TEXT),
    )
)

OPTION_PRICING_PREDICTION_SCHEMA = _schema(
    (
        ("id", TEXT),
        ("symbol", TEXT),
        ("source_provider", TEXT),
        ("prediction_mode", TEXT),
        ("call_put", TEXT),
        ("contract_symbol", TEXT),
        ("expiration_date", UTC_TIMESTAMP),
        ("target_snapshot_for", UTC_TIMESTAMP),
        ("source_snapshot_for", UTC_TIMESTAMP),
        ("source_available_at", UTC_TIMESTAMP),
        ("source_quote_staleness_seconds", FLOAT),
        ("prediction_created_at", UTC_TIMESTAMP),
        ("prediction_available_at", UTC_TIMESTAMP),
        ("model_name", TEXT),
        ("model_version", TEXT),
        ("model_status", TEXT),
        ("underlying_price", FLOAT),
        ("strike", FLOAT),
        ("multiplier", FLOAT),
        ("risk_free_rate", FLOAT),
        ("lagged_implied_volatility", FLOAT),
        ("target_years_to_expiration", FLOAT),
        ("dividend_yield", FLOAT),
        ("black_scholes_price", FLOAT),
        ("predicted_normalized_residual", FLOAT),
        ("raw_fair_value", FLOAT),
        ("point_lower_bound", FLOAT),
        ("point_upper_bound", FLOAT),
        ("predictive_standard_deviation", FLOAT),
        ("raw_interval_80_lower", FLOAT),
        ("raw_interval_80_upper", FLOAT),
        ("raw_interval_95_lower", FLOAT),
        ("raw_interval_95_upper", FLOAT),
        ("constrained_fair_value", FLOAT),
        ("constrained_interval_80_lower", FLOAT),
        ("constrained_interval_80_upper", FLOAT),
        ("constrained_interval_95_lower", FLOAT),
        ("constrained_interval_95_upper", FLOAT),
        ("raw_bound_violation", BOOLEAN),
        ("raw_monotonicity_violation", BOOLEAN),
        ("raw_convexity_violation", BOOLEAN),
        ("constrained_bound_violation", BOOLEAN),
        ("constrained_monotonicity_violation", BOOLEAN),
        ("constrained_convexity_violation", BOOLEAN),
        ("projection_correction", FLOAT),
        ("projection_status", TEXT),
        ("prediction_status", TEXT),
        ("pricing_policy_version", TEXT),
        ("timing_policy_version", TEXT),
        ("schema_version", TEXT),
        ("automated_action_allowed", BOOLEAN),
    )
)

OPTION_PRICING_EVALUATION_SCHEMA = _schema(
    (
        ("id", TEXT),
        ("symbol", TEXT),
        ("source_provider", TEXT),
        ("prediction_mode", TEXT),
        ("call_put", TEXT),
        ("contract_symbol", TEXT),
        ("expiration_date", UTC_TIMESTAMP),
        ("target_snapshot_for", UTC_TIMESTAMP),
        ("prediction_created_at", UTC_TIMESTAMP),
        ("prediction_available_at", UTC_TIMESTAMP),
        ("observed_quote_timestamp", UTC_TIMESTAMP),
        ("observed_available_at", UTC_TIMESTAMP),
        ("observed_quote_staleness_seconds", FLOAT),
        ("evaluated_at", UTC_TIMESTAMP),
        ("model_name", TEXT),
        ("model_version", TEXT),
        ("underlying_price", FLOAT),
        ("strike", FLOAT),
        ("multiplier", FLOAT),
        ("lagged_implied_volatility", FLOAT),
        ("target_years_to_expiration", FLOAT),
        ("observed_bid", FLOAT),
        ("observed_ask", FLOAT),
        ("observed_mid", FLOAT),
        ("bid_ask_spread", FLOAT),
        ("black_scholes_price", FLOAT),
        ("predicted_normalized_residual", FLOAT),
        ("observed_normalized_residual", FLOAT),
        ("raw_fair_value", FLOAT),
        ("constrained_fair_value", FLOAT),
        ("predictive_standard_deviation", FLOAT),
        ("constrained_interval_80_lower", FLOAT),
        ("constrained_interval_80_upper", FLOAT),
        ("constrained_interval_95_lower", FLOAT),
        ("constrained_interval_95_upper", FLOAT),
        ("dollar_error", FLOAT),
        ("normalized_absolute_error", FLOAT),
        ("normalized_squared_error", FLOAT),
        ("error_in_half_spreads", FLOAT),
        ("model_edge_in_half_spreads", FLOAT),
        ("interval_80_covered", BOOLEAN),
        ("interval_95_covered", BOOLEAN),
        ("prospective_eligible", BOOLEAN),
        ("evaluation_status", TEXT),
        ("pricing_policy_version", TEXT),
        ("timing_policy_version", TEXT),
        ("schema_version", TEXT),
    )
)

OPTION_PRICING_SURFACE_SCHEMA = _schema(
    (
        ("id", TEXT),
        ("symbol", TEXT),
        ("target_snapshot_for", UTC_TIMESTAMP),
        ("available_at", UTC_TIMESTAMP),
        ("call_put", TEXT),
        ("expiration_bucket", TEXT),
        ("moneyness_bucket", TEXT),
        ("source_provider", TEXT),
        ("prediction_mode", TEXT),
        ("contract_count", INTEGER),
        ("causal_coverage", FLOAT),
        ("median_normalized_residual", FLOAT),
        ("median_predictive_standard_deviation", FLOAT),
        ("median_model_edge_in_half_spreads", FLOAT),
        ("positive_edge_fraction", FLOAT),
        ("negative_edge_fraction", FLOAT),
        ("raw_arbitrage_violation_rate", FLOAT),
        ("constrained_arbitrage_violation_rate", FLOAT),
        ("interval_80_coverage", FLOAT),
        ("interval_95_coverage", FLOAT),
        ("median_bid_ask_spread", FLOAT),
        ("median_relative_bid_ask_spread", FLOAT),
        ("median_quote_staleness_seconds", FLOAT),
        ("surface_quality_pass", BOOLEAN),
        ("surface_status", TEXT),
        ("pricing_policy_version", TEXT),
        ("schema_version", TEXT),
        ("automated_action_allowed", BOOLEAN),
    )
)

OPTION_PRICING_MONITORING_SCHEMA = _schema(
    (
        ("id", TEXT),
        ("monitored_at", UTC_TIMESTAMP),
        ("category", TEXT),
        ("metric_name", TEXT),
        ("scope_type", TEXT),
        ("scope_value", TEXT),
        ("status", TEXT),
        ("observed_value", FLOAT),
        ("reference_value", FLOAT),
        ("unit", TEXT),
        ("evidence_row_count", INTEGER),
        ("window_start", UTC_TIMESTAMP),
        ("window_end", UTC_TIMESTAMP),
        ("details", TEXT),
    )
)


def sample_schema(feature_columns: Sequence[str]) -> pa.Schema:
    feature_names = tuple(dict.fromkeys(str(name) for name in feature_columns))
    overlap = sorted(set(feature_names).intersection(SAMPLE_BASE_SCHEMA.names))
    if overlap:
        raise ValueError(
            "Feature columns overlap the sample contract: " + ", ".join(overlap)
        )
    return _schema(
        (
            *((field.name, field.type) for field in SAMPLE_BASE_SCHEMA),
            *((name, FLOAT) for name in feature_names),
        )
    )


def frame_with_readable_id(
    frame: pd.DataFrame,
    *,
    key_columns: Sequence[str],
) -> pd.DataFrame:
    output = add_readable_id(frame, key_columns=key_columns)
    validate_readable_ids(output)
    return output


def empty_frame(schema: pa.Schema) -> pd.DataFrame:
    assert_one_id_schema(schema)
    return schema.empty_table().to_pandas()


def write_parquet_with_schema(
    frame: pd.DataFrame,
    path: Path,
    schema: pa.Schema,
) -> None:
    assert_one_id_schema(schema)
    output = _coerce_frame(frame, schema)
    validate_readable_ids(output)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(
        output,
        schema=schema,
        preserve_index=False,
        safe=True,
    )
    pq.write_table(table, target)


def verify_parquet_schema(path: Path, expected: pa.Schema) -> None:
    assert_one_id_schema(expected)
    observed = pq.read_schema(Path(path))
    if not observed.equals(expected, check_metadata=False):
        raise RuntimeError(
            "Parquet physical schema mismatch: "
            f"expected={expected}, observed={observed}"
        )
    validate_readable_ids(pd.read_parquet(path, columns=[ID_COLUMN]))


def assert_one_id_schema(schema: pa.Schema) -> None:
    names = schema.names
    if names.count(ID_COLUMN) != 1:
        raise ValueError("Every persisted Parquet schema must contain exactly one id")
    if names[0] != ID_COLUMN:
        raise ValueError("The Duckets id column must be first")
    if schema.field(ID_COLUMN).type != TEXT:
        raise TypeError("The Duckets id column must be readable text")
    forbidden = forbidden_identity_columns(names)
    if forbidden:
        raise ValueError(
            "Persisted ML schemas contain forbidden identity columns: "
            + ", ".join(forbidden)
        )


def forbidden_identity_columns(
    columns: Iterable[str],
    *,
    allowed_provider_native: Iterable[str] = (),
) -> list[str]:
    allowed = set(allowed_provider_native)
    forbidden: list[str] = []
    for raw_name in columns:
        name = str(raw_name)
        if name == ID_COLUMN or name in allowed:
            continue
        if _identity_column_name(name):
            forbidden.append(name)
    return sorted(forbidden)


def validate_readable_ids(frame: pd.DataFrame) -> None:
    if frame.columns.tolist().count(ID_COLUMN) != 1:
        raise ValueError("Persisted frames must contain exactly one id column")
    forbidden = forbidden_identity_columns(frame.columns)
    if forbidden:
        raise ValueError(
            "Persisted frame contains forbidden identity columns: "
            + ", ".join(forbidden)
        )
    if frame.empty:
        return
    values = frame[ID_COLUMN].astype("string")
    if values.isna().any() or values.str.strip().eq("").any():
        raise ValueError("Persisted id values must be non-null and non-empty")
    if values.duplicated().any():
        raise ValueError("Persisted id values must be unique within each Parquet")
    unreadable = values.map(is_opaque_identifier)
    if unreadable.any():
        raise ValueError("Persisted id values must be readable natural keys")


def _coerce_frame(frame: pd.DataFrame, schema: pa.Schema) -> pd.DataFrame:
    unexpected = sorted(set(frame.columns).difference(schema.names))
    if unexpected:
        raise ValueError(
            "Frame contains columns outside the explicit Parquet contract: "
            + ", ".join(unexpected)
        )
    output = frame.copy()
    for field in schema:
        if field.name not in output:
            output[field.name] = pd.NA
        series = output[field.name]
        if pa.types.is_timestamp(field.type):
            output[field.name] = pd.to_datetime(series, utc=True, errors="coerce")
        elif pa.types.is_string(field.type):
            output[field.name] = series.astype("string")
        elif pa.types.is_float64(field.type):
            output[field.name] = pd.to_numeric(series, errors="coerce").astype(
                "float64"
            )
        elif pa.types.is_int8(field.type):
            output[field.name] = pd.to_numeric(series, errors="coerce").astype(
                "Int8"
            )
        elif pa.types.is_int64(field.type):
            output[field.name] = pd.to_numeric(series, errors="coerce").astype(
                "Int64"
            )
        elif pa.types.is_boolean(field.type):
            output[field.name] = series.astype("boolean")
        else:
            raise TypeError(f"Unsupported Parquet contract type: {field.type}")
    return output.loc[:, schema.names]
