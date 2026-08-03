from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from ml.contracts import MLContractError


@dataclass(frozen=True)
class PointInTimeAuditColumns:
    """Stable audit-column names emitted by every point-in-time join."""

    family: str

    @property
    def available_at(self) -> str:
        return f"{self.family}__available_at"

    @property
    def age_seconds(self) -> str:
        return f"{self.family}__age_seconds"

    @property
    def is_stale(self) -> str:
        return f"{self.family}__is_stale"

    @property
    def join_status(self) -> str:
        return f"{self.family}__join_status"


def exact_feature_join(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    family: str,
    value_columns: Mapping[str, str],
    left_keys: Sequence[str] = (
        "symbol",
        "provider",
        "timeframe",
        "bar_timestamp",
    ),
    right_keys: Sequence[str] | None = None,
    decision_column: str = "decision_timestamp",
    available_column: str = "available_at",
    freshness: pd.Timedelta | str | None = pd.Timedelta(0),
    audit_columns: Mapping[str, str] | None = None,
    quality_columns: Sequence[str] = (),
) -> pd.DataFrame:
    """Attach one exact-grain family without allowing a non-exact availability.

    A matching natural key whose source availability is before or after the
    decision remains visible in audit context, but its model values are null.
    Duplicate source natural keys fail closed.
    """

    left_key_names = tuple(left_keys)
    right_key_names = tuple(right_keys or left_keys)
    if len(left_key_names) != len(right_key_names):
        raise ValueError("left_keys and right_keys must have the same length")
    _validate_join_arguments(family=family, value_columns=value_columns)
    _require_columns(
        decisions,
        (*left_key_names, decision_column),
        label="decision frame",
    )
    _require_columns(
        source,
        (
            *right_key_names,
            available_column,
            *value_columns.values(),
            *(audit_columns or {}).values(),
            *quality_columns,
        ),
        label=f"{family} source",
    )
    _reject_duplicate_columns(decisions, label="decision frame")
    _reject_duplicate_columns(source, label=f"{family} source")

    left = decisions.copy()
    _assert_output_columns_available(
        left,
        family=family,
        model_columns=tuple(value_columns),
        audit_columns=tuple((audit_columns or {}).keys()),
    )
    left["_pit_row_order"] = np.arange(len(left), dtype=np.int64)
    left["_pit_decision_at"] = _utc_series(
        left[decision_column],
        label=f"decision {decision_column}",
    )

    right = source.copy()
    for left_name, right_name in zip(
        left_key_names,
        right_key_names,
        strict=True,
    ):
        if left_name != right_name:
            right[left_name] = right[right_name]
    _normalize_exact_keys(left, left_key_names)
    _normalize_exact_keys(right, left_key_names)
    if right.duplicated(list(left_key_names)).any():
        raise MLContractError(
            f"{family} source contains duplicate natural keys: "
            + ", ".join(left_key_names)
        )

    prepared_right, temporary_values, temporary_audit = _prepared_right_frame(
        right,
        keys=left_key_names,
        family=family,
        value_columns=value_columns,
        available_column=available_column,
        audit_columns=audit_columns or {},
        quality_columns=quality_columns,
    )
    merged = left.merge(
        prepared_right,
        on=list(left_key_names),
        how="left",
        validate="many_to_one",
        sort=False,
    )
    return _finalize_join(
        merged,
        family=family,
        value_columns=value_columns,
        temporary_values=temporary_values,
        temporary_audit=temporary_audit,
        freshness=_timedelta_or_none(freshness),
        exact_availability=True,
    )


def backward_asof_by_symbol(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    family: str,
    value_columns: Mapping[str, str],
    freshness: pd.Timedelta | str | None,
    symbol_column: str = "symbol",
    source_symbol_column: str = "symbol",
    decision_column: str = "decision_timestamp",
    available_column: str = "available_at",
    natural_key_columns: Sequence[str] | None = None,
    valid_until_column: str | None = None,
    audit_columns: Mapping[str, str] | None = None,
    quality_columns: Sequence[str] = (),
) -> pd.DataFrame:
    """Backward-as-of join by symbol with fail-closed freshness and quality.

    Exact availability is allowed. Source rows later than the decision cannot
    be selected. No row is supplied before a symbol's first publication, and a
    stale or quality-failed latest row is not replaced by an older row.
    """

    _validate_join_arguments(family=family, value_columns=value_columns)
    _require_columns(
        decisions,
        (symbol_column, decision_column),
        label="decision frame",
    )
    required_source = [
        source_symbol_column,
        available_column,
        *value_columns.values(),
        *(audit_columns or {}).values(),
        *quality_columns,
    ]
    if valid_until_column:
        required_source.append(valid_until_column)
    _require_columns(source, required_source, label=f"{family} source")
    _reject_duplicate_columns(decisions, label="decision frame")
    _reject_duplicate_columns(source, label=f"{family} source")

    left = decisions.copy()
    _assert_output_columns_available(
        left,
        family=family,
        model_columns=tuple(value_columns),
        audit_columns=tuple((audit_columns or {}).keys()),
    )
    left["_pit_row_order"] = np.arange(len(left), dtype=np.int64)
    left["_pit_decision_at"] = _utc_series(
        left[decision_column],
        label=f"decision {decision_column}",
    )
    left["_pit_symbol"] = _normalized_symbol(
        left[symbol_column],
        label=f"decision {symbol_column}",
    )

    right = source.copy()
    right["_pit_symbol"] = _normalized_symbol(
        right[source_symbol_column],
        label=f"{family} {source_symbol_column}",
    )
    right["_pit_available_at"] = _utc_series(
        right[available_column],
        label=f"{family} {available_column}",
    )
    if natural_key_columns:
        _require_columns(
            right,
            natural_key_columns,
            label=f"{family} source natural key",
        )
        normalized_natural_key = _normalized_natural_key_frame(
            right,
            natural_key_columns,
            label=f"{family} source natural key",
        )
        if normalized_natural_key.duplicated(
            list(natural_key_columns)
        ).any():
            raise MLContractError(
                f"{family} source contains duplicate natural keys: "
                + ", ".join(natural_key_columns)
            )
    if right.duplicated(["_pit_symbol", "_pit_available_at"]).any():
        raise MLContractError(
            f"{family} source availability must be unique per symbol"
        )

    prepared_right, temporary_values, temporary_audit = _prepared_right_frame(
        right,
        keys=("_pit_symbol",),
        family=family,
        value_columns=value_columns,
        available_column="_pit_available_at",
        audit_columns=audit_columns or {},
        quality_columns=quality_columns,
        valid_until_column=valid_until_column,
    )

    aligned_parts: list[pd.DataFrame] = []
    prepared_columns = [
        column
        for column in prepared_right.columns
        if column != "_pit_symbol"
    ]
    for symbol, left_part in left.groupby("_pit_symbol", sort=False):
        right_part = prepared_right.loc[
            prepared_right["_pit_symbol"].eq(symbol)
        ].copy()
        ordered_left = left_part.sort_values(
            ["_pit_decision_at", "_pit_row_order"],
            kind="mergesort",
        )
        if right_part.empty:
            aligned = ordered_left.copy()
            for column in prepared_columns:
                aligned[column] = pd.NA
        else:
            ordered_right = right_part.sort_values(
                "_pit_available_at",
                kind="mergesort",
            ).drop(columns="_pit_symbol")
            aligned = pd.merge_asof(
                ordered_left,
                ordered_right,
                left_on="_pit_decision_at",
                right_on="_pit_available_at",
                direction="backward",
                allow_exact_matches=True,
            )
        aligned_parts.append(aligned)

    if not aligned_parts:
        merged = left.copy()
        for column in prepared_columns:
            merged[column] = pd.NA
    else:
        merged = pd.concat(aligned_parts, ignore_index=True, sort=False)
    return _finalize_join(
        merged,
        family=family,
        value_columns=value_columns,
        temporary_values=temporary_values,
        temporary_audit=temporary_audit,
        freshness=_timedelta_or_none(freshness),
        exact_availability=False,
    )


def backward_asof_shared(
    decisions: pd.DataFrame,
    source: pd.DataFrame,
    *,
    family: str,
    value_columns: Mapping[str, str],
    freshness: pd.Timedelta | str | None,
    decision_column: str = "decision_timestamp",
    available_column: str = "available_at",
    natural_key_columns: Sequence[str] | None = None,
    valid_until_column: str | None = None,
    audit_columns: Mapping[str, str] | None = None,
    quality_columns: Sequence[str] = (),
) -> pd.DataFrame:
    """Backward-as-of join a shared context without using a stock key."""

    _require_columns(decisions, (decision_column,), label="decision frame")
    left = decisions.copy()
    right = source.copy()
    scope_column = "_pit_shared_scope"
    if scope_column in left.columns or scope_column in right.columns:
        raise MLContractError(f"Reserved column already exists: {scope_column}")
    left[scope_column] = "shared"
    right[scope_column] = "shared"
    result = backward_asof_by_symbol(
        left,
        right,
        family=family,
        value_columns=value_columns,
        freshness=freshness,
        symbol_column=scope_column,
        source_symbol_column=scope_column,
        decision_column=decision_column,
        available_column=available_column,
        natural_key_columns=natural_key_columns,
        valid_until_column=valid_until_column,
        audit_columns=audit_columns,
        quality_columns=quality_columns,
    )
    return result.drop(columns=scope_column)


def pivot_shared_context(
    source: pd.DataFrame,
    *,
    context_to_model: Mapping[str, str],
    context_column: str = "context_name",
    value_column: str = "value",
    window_start_column: str = "window_start",
    window_end_column: str = "window_end",
    available_column: str = "available_at",
    quality_columns: Sequence[str] = (),
) -> pd.DataFrame:
    """Pivot synchronized long-form shared context without mixing windows.

    Each output row is one exact ``window_start``/``window_end`` pair. Missing
    required contexts are retained as an incomplete row, never filled from a
    different window. Availability is the maximum across present constituents.
    """

    _require_columns(
        source,
        (
            context_column,
            value_column,
            window_start_column,
            window_end_column,
            available_column,
            *quality_columns,
        ),
        label="shared context source",
    )
    if not context_to_model:
        raise ValueError("context_to_model cannot be empty")
    frame = source.copy()
    frame[window_start_column] = _utc_series(
        frame[window_start_column],
        label=window_start_column,
    )
    frame[window_end_column] = _utc_series(
        frame[window_end_column],
        label=window_end_column,
    )
    frame[available_column] = _utc_series(
        frame[available_column],
        label=available_column,
    )
    frame[context_column] = frame[context_column].astype("string").str.strip()
    if frame[context_column].isna().any() or frame[context_column].eq("").any():
        raise MLContractError("Shared context names must be non-empty")
    required_contexts = tuple(context_to_model)
    selected = frame.loc[frame[context_column].isin(required_contexts)].copy()
    duplicate_key = [window_start_column, window_end_column, context_column]
    if selected.duplicated(duplicate_key).any():
        raise MLContractError(
            "Shared context contains duplicate constituent window keys"
        )

    rows: list[dict[str, object]] = []
    group_columns = [window_start_column, window_end_column]
    for window, group in selected.groupby(group_columns, sort=True, dropna=False):
        by_context = group.set_index(context_column)
        observed = set(str(value) for value in by_context.index)
        complete = set(required_contexts).issubset(observed)
        quality_pass = complete
        for column in quality_columns:
            quality_pass = quality_pass and _all_true(group[column])
        row: dict[str, object] = {
            window_start_column: window[0],
            window_end_column: window[1],
            available_column: group[available_column].max(),
            "constituent_complete": bool(complete),
            "quality_pass": bool(quality_pass),
        }
        for context, model_name in context_to_model.items():
            row[model_name] = (
                by_context.at[context, value_column]
                if context in by_context.index
                else np.nan
            )
        rows.append(row)
    return pd.DataFrame(rows)


def conservative_date_only_availability(
    source: pd.DataFrame,
    decisions: pd.DataFrame,
    *,
    date_column: str,
    available_column: str = "available_at",
    symbol_column: str | None = "symbol",
    decision_column: str = "decision_timestamp",
) -> pd.DataFrame:
    """Fill missing availability with the first decision after a full date.

    Exact publication timestamps are preserved. A date-only publication becomes
    available at the first eligible model decision on or after the following
    UTC midnight; it is never assigned to a decision inside the reported date.
    """

    _require_columns(source, (date_column,), label="date-only source")
    _require_columns(decisions, (decision_column,), label="decision frame")
    if symbol_column is not None:
        _require_columns(source, (symbol_column,), label="date-only source")
        _require_columns(decisions, (symbol_column,), label="decision frame")

    result = source.copy()
    if available_column in result.columns:
        supplied_availability = result[available_column].notna()
        availability = pd.to_datetime(
            result[available_column],
            utc=True,
            errors="coerce",
        )
        if (supplied_availability & availability.isna()).any():
            raise MLContractError(
                f"Date-only source contains invalid {available_column}"
            )
    else:
        availability = pd.Series(
            pd.NaT,
            index=result.index,
            dtype="datetime64[ns, UTC]",
        )
    dates = pd.to_datetime(result[date_column], utc=True, errors="coerce")
    if (availability.isna() & dates.isna()).any():
        raise MLContractError(
            f"Date-only source contains invalid {date_column}"
        )
    missing = availability.isna() & dates.notna()
    decision_times = pd.to_datetime(
        decisions[decision_column],
        utc=True,
        errors="coerce",
    )
    if decision_times.isna().any():
        raise MLContractError("Decision frame contains invalid timestamps")

    for index in result.index[missing]:
        not_before = dates.loc[index].normalize() + pd.Timedelta(days=1)
        eligible = decision_times.ge(not_before)
        if symbol_column is not None:
            symbol = str(result.at[index, symbol_column]).strip().upper()
            decision_symbols = (
                decisions[symbol_column].astype(str).str.strip().str.upper()
            )
            eligible &= decision_symbols.eq(symbol)
        candidates = decision_times.loc[eligible]
        if not candidates.empty:
            availability.loc[index] = candidates.min()
    result[available_column] = availability
    return result


def model_value_projection(
    frame: pd.DataFrame,
    model_columns: Sequence[str],
    *,
    include_keys: Sequence[str] = (),
) -> pd.DataFrame:
    """Project only explicit model values and optional readable decision keys."""

    allowed_keys = {"symbol", "horizon", "decision_timestamp"}
    invalid_keys = sorted(set(include_keys).difference(allowed_keys))
    if invalid_keys:
        raise MLContractError(
            "Model projection include_keys may contain only decision identity; "
            "invalid: "
            + ", ".join(invalid_keys)
        )
    ordered = [*include_keys, *model_columns]
    if len(ordered) != len(set(ordered)):
        raise ValueError("Projection columns contain duplicates")
    _require_columns(frame, ordered, label="joined feature frame")
    return frame.loc[:, ordered].copy()


def _prepared_right_frame(
    frame: pd.DataFrame,
    *,
    keys: Sequence[str],
    family: str,
    value_columns: Mapping[str, str],
    available_column: str,
    audit_columns: Mapping[str, str],
    quality_columns: Sequence[str],
    valid_until_column: str | None = None,
) -> tuple[pd.DataFrame, dict[str, str], dict[str, str]]:
    selected = frame.loc[
        :,
        list(
            dict.fromkeys(
                (
                    *keys,
                    available_column,
                    *value_columns.values(),
                    *audit_columns.values(),
                    *quality_columns,
                    *((valid_until_column,) if valid_until_column else ()),
                )
            )
        ),
    ].copy()
    selected["_pit_available_at"] = _utc_series(
        selected[available_column],
        label=f"{family} {available_column}",
    )
    if available_column != "_pit_available_at":
        selected = selected.drop(columns=available_column)

    temporary_values: dict[str, str] = {}
    for index, (model_name, source_name) in enumerate(value_columns.items()):
        temporary = f"_pit_value_{index}"
        selected[temporary] = pd.to_numeric(
            selected[source_name],
            errors="coerce",
        ).astype("float64")
        temporary_values[model_name] = temporary
    temporary_audit: dict[str, str] = {}
    for index, (output_name, source_name) in enumerate(audit_columns.items()):
        temporary = f"_pit_audit_{index}"
        selected[temporary] = selected[source_name]
        temporary_audit[output_name] = temporary

    quality = pd.Series(True, index=selected.index, dtype="boolean")
    for column in quality_columns:
        quality &= _explicit_true_mask(selected[column])
    selected["_pit_quality_pass"] = _explicit_true_mask(quality)
    if valid_until_column:
        selected["_pit_valid_until"] = _utc_series(
            selected[valid_until_column],
            label=f"{family} {valid_until_column}",
        )

    keep = [
        *keys,
        "_pit_available_at",
        *temporary_values.values(),
        *temporary_audit.values(),
        "_pit_quality_pass",
    ]
    if valid_until_column:
        keep.append("_pit_valid_until")
    return selected.loc[:, keep], temporary_values, temporary_audit


def _finalize_join(
    frame: pd.DataFrame,
    *,
    family: str,
    value_columns: Mapping[str, str],
    temporary_values: Mapping[str, str],
    temporary_audit: Mapping[str, str],
    freshness: pd.Timedelta | None,
    exact_availability: bool,
) -> pd.DataFrame:
    result = frame.copy()
    available = pd.to_datetime(
        result["_pit_available_at"],
        utc=True,
        errors="coerce",
    )
    decision = pd.to_datetime(
        result["_pit_decision_at"],
        utc=True,
        errors="coerce",
    )
    age = decision - available
    has_source = available.notna()
    future = has_source & age.lt(pd.Timedelta(0))
    stale = pd.Series(False, index=result.index)
    if freshness is not None:
        stale |= has_source & age.gt(freshness)
    if "_pit_valid_until" in result.columns:
        valid_until = pd.to_datetime(
            result["_pit_valid_until"],
            utc=True,
            errors="coerce",
        )
        stale |= has_source & (
            valid_until.isna() | decision.gt(valid_until)
        )
    exact = age.eq(pd.Timedelta(0))
    quality = _explicit_true_mask(result["_pit_quality_pass"])
    eligible = has_source & ~future & ~stale & quality
    if exact_availability:
        eligible &= exact

    for model_name in value_columns:
        result[model_name] = pd.to_numeric(
            result[temporary_values[model_name]],
            errors="coerce",
        ).where(eligible)
    for output_name, temporary in temporary_audit.items():
        result[output_name] = result[temporary]

    audit = PointInTimeAuditColumns(family)
    result[audit.available_at] = available
    result[audit.age_seconds] = age.dt.total_seconds()
    result[audit.is_stale] = stale.astype("boolean")
    statuses = np.select(
        (
            ~has_source,
            future,
            exact_availability & has_source & ~exact,
            has_source & ~quality,
            stale,
        ),
        (
            "NO_PRIOR_PUBLICATION",
            "FUTURE_REJECTED",
            "AVAILABILITY_NOT_EXACT",
            "QUALITY_REJECTED",
            "STALE",
        ),
        default="JOINED",
    )
    result[audit.join_status] = pd.Series(
        statuses,
        index=result.index,
        dtype="string",
    )
    if (eligible & available.gt(decision)).any():
        raise MLContractError(f"{family} join selected future evidence")

    drop = [
        "_pit_decision_at",
        "_pit_symbol",
        "_pit_available_at",
        "_pit_quality_pass",
        "_pit_valid_until",
        *temporary_values.values(),
        *temporary_audit.values(),
    ]
    result = result.drop(
        columns=[column for column in drop if column in result.columns]
    )
    return result.sort_values("_pit_row_order", kind="mergesort").drop(
        columns="_pit_row_order"
    ).reset_index(drop=True)


def _validate_join_arguments(
    *,
    family: str,
    value_columns: Mapping[str, str],
) -> None:
    if not str(family).strip():
        raise ValueError("family is required")
    if not value_columns:
        raise ValueError("value_columns cannot be empty")
    if len(value_columns) != len(set(value_columns)):
        raise ValueError("Model feature names must be unique")
    if len(value_columns.values()) != len(set(value_columns.values())):
        raise ValueError("Source value columns must be unique")
    expected = f"{family}__"
    invalid = [name for name in value_columns if not name.startswith(expected)]
    if invalid:
        raise ValueError(
            f"Model values must use the {expected!r} namespace: {invalid}"
        )


def _assert_output_columns_available(
    frame: pd.DataFrame,
    *,
    family: str,
    model_columns: Sequence[str],
    audit_columns: Sequence[str],
) -> None:
    generated = {
        *model_columns,
        *audit_columns,
        PointInTimeAuditColumns(family).available_at,
        PointInTimeAuditColumns(family).age_seconds,
        PointInTimeAuditColumns(family).is_stale,
        PointInTimeAuditColumns(family).join_status,
    }
    overlap = sorted(generated.intersection(frame.columns))
    if overlap:
        raise MLContractError(
            "Point-in-time join would overwrite existing columns: "
            + ", ".join(overlap)
        )


def _normalize_exact_keys(frame: pd.DataFrame, keys: Sequence[str]) -> None:
    for key in keys:
        lowered = key.lower()
        if lowered in {
            "bar_timestamp",
            "bar_end_timestamp",
            "available_at",
            "decision_timestamp",
            "timestamp",
        } or lowered.endswith(("_at", "_timestamp")):
            frame[key] = _utc_series(frame[key], label=key)
        elif lowered == "symbol":
            frame[key] = _normalized_symbol(frame[key], label=key)
        elif lowered in {"provider", "timeframe", "horizon"}:
            frame[key] = frame[key].astype("string").str.strip().str.lower()


def _normalized_natural_key_frame(
    frame: pd.DataFrame,
    keys: Sequence[str],
    *,
    label: str,
) -> pd.DataFrame:
    normalized = frame.loc[:, list(keys)].copy()
    for key in keys:
        lowered = key.strip().lower()
        if lowered in {
            "bar_timestamp",
            "bar_end_timestamp",
            "available_at",
            "decision_timestamp",
            "timestamp",
            "window_start",
            "window_end",
            "observation_date",
            "realtime_start",
            "realtime_end",
            "period_end_date",
        } or lowered.endswith(("_at", "_timestamp", "_date")):
            normalized[key] = _utc_series(
                normalized[key],
                label=f"{label} {key}",
            )
        elif lowered == "symbol":
            normalized[key] = _normalized_symbol(
                normalized[key],
                label=f"{label} {key}",
            )
        elif lowered in {"provider", "timeframe", "horizon"}:
            normalized[key] = (
                normalized[key].astype("string").str.strip().str.lower()
            )
        elif (
            pd.api.types.is_string_dtype(normalized[key].dtype)
            or normalized[key].dtype == object
        ):
            normalized[key] = normalized[key].astype("string").str.strip()
    if normalized.isna().any(axis=None):
        raise MLContractError(f"{label} contains missing values")
    return normalized


def _normalized_symbol(values: pd.Series, *, label: str) -> pd.Series:
    normalized = values.astype("string").str.strip().str.upper()
    if normalized.isna().any() or normalized.eq("").any():
        raise MLContractError(f"{label} contains missing symbols")
    return normalized


def _utc_series(values: pd.Series, *, label: str) -> pd.Series:
    converted = pd.to_datetime(
        values,
        utc=True,
        errors="coerce",
    ).astype("datetime64[ns, UTC]")
    if converted.isna().any():
        raise MLContractError(f"{label} contains invalid timestamps")
    return converted


def _timedelta_or_none(
    value: pd.Timedelta | str | None,
) -> pd.Timedelta | None:
    if value is None:
        return None
    parsed = pd.Timedelta(value)
    if parsed < pd.Timedelta(0):
        raise ValueError("freshness cannot be negative")
    return parsed


def _require_columns(
    frame: pd.DataFrame,
    columns: Sequence[str],
    *,
    label: str,
) -> None:
    missing = sorted(set(columns).difference(frame.columns))
    if missing:
        raise MLContractError(
            f"{label} is missing required columns: " + ", ".join(missing)
        )


def _reject_duplicate_columns(frame: pd.DataFrame, *, label: str) -> None:
    if frame.columns.has_duplicates:
        duplicated = frame.columns[frame.columns.duplicated()].tolist()
        raise MLContractError(f"{label} contains duplicate columns: {duplicated}")


def _all_true(values: pd.Series) -> bool:
    return bool(_explicit_true_mask(values).all())


def _explicit_true_mask(values: pd.Series) -> pd.Series:
    """Accept only explicit true-like values; ambiguous metadata fails closed."""

    normalized = values.astype("string").str.strip().str.lower()
    return normalized.isin({"true", "1", "1.0", "yes", "y"}).fillna(False)
