from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from datafetching.ids import add_readable_id
from ml.calendars import (
    attach_official_daily_sessions,
    attach_official_intraday_sessions,
)
from ml.contracts import FeatureSet, MLContractError
from ml.feature_registry import DEFAULT_FEATURE_REGISTRY, FeatureRegistry

_SOURCE_KEYS = ("symbol", "provider", "timeframe", "bar_timestamp")
_UNIVERSE_COLUMNS = (
    "symbol",
    "venue",
    "currency",
    "exchange_calendar",
    "effective_from",
    "effective_to",
)
_VALID_PRICE_ADJUSTMENT_STATUSES = {
    "NO_SPLIT_EVENTS_IN_RANGE",
    "SPLIT_ADJUSTED",
    "SPLITS_ALREADY_REFLECTED",
    "SPLIT_ADJUSTED_WITH_PREEXISTING_ADJUSTMENTS",
}


@dataclass(frozen=True)
class TechnicalDatasetConfig:
    feature_set: str = "technical-all"
    required_timeframe: str = "1d"
    processing_delay: pd.Timedelta = pd.Timedelta(minutes=5)
    strict_key_alignment: bool = True
    temporal_mode: str = "daily"
    include_extended_hours: bool = False

    def __post_init__(self) -> None:
        if self.processing_delay < pd.Timedelta(0):
            raise ValueError("processing_delay cannot be negative")
        if not self.required_timeframe:
            raise ValueError("required_timeframe is required")
        if self.temporal_mode not in {"daily", "intraday-hour"}:
            raise ValueError(f"Unsupported temporal_mode: {self.temporal_mode}")


def assemble_technical_feature_frame(
    market_regime: pd.DataFrame,
    breakout_pressure: pd.DataFrame,
    universe_membership: pd.DataFrame,
    *,
    config: TechnicalDatasetConfig | None = None,
    registry: FeatureRegistry = DEFAULT_FEATURE_REGISTRY,
) -> pd.DataFrame:
    """Build one strict, calendar-authoritative technical feature table.

    Operational daily ``bar_end_timestamp`` values remain timing context. The ML
    decision timestamp is derived from the row's versioned exchange calendar and
    the configured processing delay.
    """

    config = config or TechnicalDatasetConfig()
    feature_set = registry.feature_set(config.feature_set, require_active=True)

    mr = _prepare_family(
        market_regime,
        source_family="mr",
        feature_set=feature_set,
        registry=registry,
    )
    bp = _prepare_family(
        breakout_pressure,
        source_family="bp",
        feature_set=feature_set,
        registry=registry,
    )

    merged = mr.merge(
        bp,
        on=list(_SOURCE_KEYS),
        how="outer" if config.strict_key_alignment else "inner",
        validate="one_to_one",
        indicator=True,
    )
    if config.strict_key_alignment and not merged["_merge"].eq("both").all():
        counts = merged["_merge"].value_counts().to_dict()
        raise MLContractError(
            "Market-regime and breakout-pressure keys do not align exactly: "
            f"{counts}."
        )
    merged = merged.loc[merged["_merge"].eq("both")].drop(columns="_merge")
    if merged.empty:
        raise MLContractError("No aligned technical rows were produced.")

    _require_equal(
        merged,
        "mr__operational_bar_end_timestamp",
        "bp__operational_bar_end_timestamp",
    )
    _require_equal(
        merged,
        "mr__price_adjustment_status",
        "bp__price_adjustment_status",
    )
    _require_equal(merged, "mr__split_event_count", "bp__split_event_count")

    if not merged["timeframe"].eq(config.required_timeframe.lower()).all():
        observed = sorted(merged["timeframe"].dropna().astype(str).unique())
        raise MLContractError(
            f"Technical baseline requires timeframe {config.required_timeframe!r}; "
            f"observed {observed}."
        )

    merged["operational_bar_end_timestamp"] = merged.pop(
        "mr__operational_bar_end_timestamp"
    )
    merged = merged.drop(columns=["bp__operational_bar_end_timestamp"])
    merged["price_adjustment_status"] = merged.pop(
        "mr__price_adjustment_status"
    )
    merged = merged.drop(columns=["bp__price_adjustment_status"])
    merged["split_event_count"] = merged.pop("mr__split_event_count").astype(
        "int64"
    )
    merged = merged.drop(columns=["bp__split_event_count"])
    invalid_adjustments = sorted(
        set(merged["price_adjustment_status"].dropna().astype(str)).difference(
            _VALID_PRICE_ADJUSTMENT_STATUSES
        )
    )
    if merged["price_adjustment_status"].isna().any() or invalid_adjustments:
        rendered = invalid_adjustments or ["<missing>"]
        raise MLContractError(
            "Unsupported price adjustment status: " + ", ".join(rendered)
        )

    merged = _attach_point_in_time_universe(
        merged,
        universe_membership,
        processing_delay=config.processing_delay,
        temporal_mode=config.temporal_mode,
        include_extended_hours=config.include_extended_hours,
    )
    merged["mr__source_available_at"] = (
        merged["bar_end_timestamp"] + config.processing_delay
    )
    merged["bp__source_available_at"] = (
        merged["bar_end_timestamp"] + config.processing_delay
    )
    merged["feature_available_at"] = merged[
        ["mr__source_available_at", "bp__source_available_at"]
    ].max(axis=1)

    if not merged["feature_available_at"].eq(merged["decision_timestamp"]).all():
        raise MLContractError(
            "Calendar decision time disagrees with technical feature availability"
        )

    merged["feature_set"] = feature_set.name
    merged = add_readable_id(
        merged,
        key_columns=("symbol", "decision_timestamp"),
    )

    if (merged["bar_end_timestamp"] > merged["decision_timestamp"]).any():
        raise MLContractError("bar_end_timestamp exceeds decision_timestamp")
    if (merged["feature_available_at"] > merged["decision_timestamp"]).any():
        raise MLContractError("feature_available_at exceeds decision_timestamp")
    if merged.duplicated(["symbol", "decision_timestamp"]).any():
        raise MLContractError(
            "symbol + decision_timestamp must be unique in the feature frame"
        )
    if merged["id"].duplicated().any():
        raise MLContractError("Feature id must be unique")

    key_columns = [
        "id",
        "symbol",
        "venue",
        "currency",
        "exchange_calendar",
    ]
    temporal_columns = [
        "exchange_session",
        "bar_timestamp",
        "operational_bar_end_timestamp",
        "bar_end_timestamp",
        "decision_timestamp",
        "mr__source_available_at",
        "bp__source_available_at",
        "feature_available_at",
    ]
    temporal_columns.extend(
        column
        for column in (
            "session_open_timestamp",
            "session_close_timestamp",
        )
        if column in merged.columns
    )
    label_context_columns = ["mr__atr_14"]
    source_context_columns = [
        "provider",
        "timeframe",
        "feature_set",
        "price_adjustment_status",
        "split_event_count",
        "exchange_calendar_name",
        "exchange_calendar_version",
        "exchange_timezone",
        "mr__calculation_version",
        "mr__calculation_mode",
        "bp__calculation_version",
        "bp__calculation_mode",
    ]
    technical_feature_names = tuple(
        feature.name
        for feature in feature_set.features
        if feature.source_family in {"mr", "bp"}
    )
    ordered = [
        *key_columns,
        *temporal_columns,
        *technical_feature_names,
        *label_context_columns,
        *source_context_columns,
    ]
    return merged.loc[:, ordered].sort_values(
        ["decision_timestamp", "symbol"]
    ).reset_index(drop=True)


def _prepare_family(
    frame: pd.DataFrame,
    *,
    source_family: str,
    feature_set: FeatureSet,
    registry: FeatureRegistry,
) -> pd.DataFrame:
    registry.validate_source(
        frame,
        source_family=source_family,
        feature_set=feature_set,
    )
    calculation = registry.calculation(source_family)
    family_features = feature_set.for_family(source_family)

    prepared = frame.copy()
    prepared["symbol"] = prepared["symbol"].astype(str).str.strip().str.upper()
    prepared["provider"] = prepared["provider"].astype(str).str.strip().str.lower()
    prepared["timeframe"] = prepared["timeframe"].astype(str).str.strip().str.lower()
    prepared["bar_timestamp"] = pd.to_datetime(
        prepared["timestamp"], utc=True, errors="coerce"
    )
    prepared["bar_end_timestamp"] = pd.to_datetime(
        prepared["bar_end_timestamp"], utc=True, errors="coerce"
    )
    if prepared[["bar_timestamp", "bar_end_timestamp"]].isna().any().any():
        raise MLContractError(f"{source_family} contains invalid timestamps")
    if prepared.duplicated(list(_SOURCE_KEYS)).any():
        raise MLContractError(f"{source_family} contains duplicate technical keys")

    selected = prepared.loc[
        :, ["symbol", "provider", "timeframe", "bar_timestamp"]
    ].copy()
    selected[f"{source_family}__operational_bar_end_timestamp"] = prepared[
        "bar_end_timestamp"
    ]
    selected[f"{source_family}__price_adjustment_status"] = prepared[
        "price_adjustment_status"
    ].astype("string")
    selected[f"{source_family}__split_event_count"] = pd.to_numeric(
        prepared["split_event_count"], errors="raise"
    )
    selected[f"{source_family}__calculation_version"] = prepared[
        "calculation_version"
    ].astype("string")
    selected[f"{source_family}__calculation_mode"] = prepared[
        calculation.mode_column
    ].astype("string")
    for feature in family_features:
        selected[feature.name] = pd.to_numeric(
            prepared[feature.source_column], errors="coerce"
        ).astype("float64")

    if source_family == "mr":
        if "atr_14" not in prepared.columns:
            raise MLContractError("market-regime source is missing required column: atr_14")
        selected["mr__atr_14"] = pd.to_numeric(
            prepared["atr_14"], errors="coerce"
        ).astype("float64")
        invalid_atr = ~selected["mr__atr_14"].gt(0) | selected["mr__atr_14"].isna()
        if invalid_atr.any():
            raise MLContractError(
                f"market-regime contains {int(invalid_atr.sum())} invalid ATR values"
            )
    return selected


def _attach_point_in_time_universe(
    frame: pd.DataFrame,
    membership: pd.DataFrame,
    *,
    processing_delay: pd.Timedelta,
    temporal_mode: str,
    include_extended_hours: bool,
) -> pd.DataFrame:
    if membership.columns.has_duplicates:
        raise MLContractError("Universe membership contains duplicate columns")
    missing = [column for column in _UNIVERSE_COLUMNS if column not in membership.columns]
    if missing:
        raise MLContractError(
            "Universe membership is missing columns: " + ", ".join(missing)
        )

    normalized = membership.loc[:, _UNIVERSE_COLUMNS].copy()
    normalized["symbol"] = normalized["symbol"].astype(str).str.strip().str.upper()
    normalized["exchange_calendar"] = (
        normalized["exchange_calendar"].astype("string").str.strip().str.upper()
    )
    normalized["effective_from"] = pd.to_datetime(
        normalized["effective_from"], utc=True, errors="coerce"
    )
    normalized["effective_to"] = pd.to_datetime(
        normalized["effective_to"], utc=True, errors="coerce"
    )
    if normalized["effective_from"].isna().any():
        raise MLContractError("Universe effective_from must be a valid UTC timestamp")

    base = frame.reset_index(drop=True).reset_index(names="_row_number")
    candidates = base.loc[:, ["_row_number", "symbol", "bar_timestamp"]].merge(
        normalized,
        on="symbol",
        how="left",
    )
    candidates = candidates.loc[candidates["exchange_calendar"].notna()].copy()
    if not candidates.empty:
        if temporal_mode == "intraday-hour":
            bar_ends = base.loc[
                :, ["_row_number", "operational_bar_end_timestamp"]
            ]
            candidates = candidates.merge(
                bar_ends,
                on="_row_number",
                how="left",
                validate="many_to_one",
            )
            candidates = attach_official_intraday_sessions(
                candidates,
                calendar_column="exchange_calendar",
                bar_timestamp_column="bar_timestamp",
                bar_end_column="operational_bar_end_timestamp",
                processing_delay=processing_delay,
                include_extended_hours=include_extended_hours,
            )
            candidates = candidates.loc[
                candidates["intraday_interval_eligible"].fillna(False)
            ].copy()
            eligible_rows = set(candidates["_row_number"])
            base = base.loc[base["_row_number"].isin(eligible_rows)].copy()
        else:
            candidates = attach_official_daily_sessions(
                candidates,
                calendar_column="exchange_calendar",
                bar_timestamp_column="bar_timestamp",
                processing_delay=processing_delay,
            )
        candidates = candidates.loc[
            candidates["effective_from"].le(candidates["decision_timestamp"])
            & (
                candidates["effective_to"].isna()
                | candidates["decision_timestamp"].lt(candidates["effective_to"])
            )
        ].copy()

    counts = (
        candidates.groupby("_row_number").size()
        if not candidates.empty
        else pd.Series(dtype="int64")
    )
    missing_rows = sorted(set(base["_row_number"]).difference(counts.index))
    ambiguous_rows = counts[counts.ne(1)].index.tolist()
    if missing_rows or ambiguous_rows:
        raise MLContractError(
            "Universe membership must resolve exactly once per decision row; "
            f"missing={missing_rows[:10]}, ambiguous={ambiguous_rows[:10]}."
        )

    calendar_columns = [
        "exchange_session",
        "bar_end_timestamp",
        "decision_timestamp",
        "exchange_calendar_name",
        "exchange_calendar_version",
        "exchange_timezone",
    ]
    calendar_columns.extend(
        column
        for column in (
            "session_open_timestamp",
            "session_close_timestamp",
            "intraday_interval_eligible",
        )
        if column in candidates.columns
    )
    resolved = candidates.drop_duplicates("_row_number", keep=False).loc[
        :,
        [
            "_row_number",
            "venue",
            "currency",
            "exchange_calendar",
            *calendar_columns,
        ],
    ]
    result = base.merge(
        resolved,
        on="_row_number",
        how="left",
        validate="one_to_one",
    )
    result = result.drop(columns=["_row_number"])
    return result


def _require_equal(frame: pd.DataFrame, left: str, right: str) -> None:
    unequal = ~(
        frame[left].eq(frame[right]) | (frame[left].isna() & frame[right].isna())
    )
    if unequal.any():
        raise MLContractError(f"Source families disagree on {left} versus {right}")
