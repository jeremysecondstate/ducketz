from __future__ import annotations

from datetime import date, datetime
import re
from typing import Sequence

import pandas as pd

ID_COLUMN = "id"
PARQUET_CONTROL_PLANE_COLUMNS = frozenset(
    {
        "acknowledged_at",
        "coordination_generation",
        "coordination_status",
        "cycle_failure_count",
        "cycle_finished_at",
        "cycle_generation",
        "cycle_status",
        "forecast_coordination_status",
        "forecast_generation",
        "lease_expires_at",
        "loop_a_cycle_generation",
        "loop_a_cycle_status",
        "loop_a_generation",
        "publication_not_after",
        "readiness_state",
        "rejected_at",
        "rejection_reason",
        "required_provider_lanes",
        "route_contract",
        "writing_started_at",
    }
)
_HASH_LIKE = re.compile(
    r"^(?:[a-z][a-z0-9_-]*[_:-])?[0-9a-f]{32,}$",
    re.IGNORECASE,
)
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_OPAQUE_CANDIDATE = re.compile(
    r"[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_IDENTITY_COLUMN_TERM = re.compile(
    r"(?:^|_)(?:hash|digest|fingerprint|checksum|sha(?:1|224|256|384|512)|"
    r"receipt|lineage|identity|content_address|uuid|guid)(?:_|$)",
    re.IGNORECASE,
)


def minimum_unique_key(
    frame: pd.DataFrame,
    candidates: Sequence[Sequence[str]],
    *,
    reject_opaque: bool = False,
) -> tuple[str, ...]:
    """Return the first complete, unique natural-key candidate in ``frame``."""

    for candidate in candidates:
        columns = tuple(dict.fromkeys(str(column) for column in candidate))
        if not columns or any(column not in frame.columns for column in columns):
            continue
        if frame.empty:
            return columns
        values = frame.loc[:, list(columns)].apply(
            lambda column: column.map(
                lambda value: _readable_value(value, column=str(column.name))
            )
        )
        if values.eq("").any(axis=None) or values.duplicated(keep=False).any():
            continue
        if reject_opaque and values.map(is_opaque_identifier).any(axis=None):
            continue
        return columns
    return ()


def adaptive_unique_key(
    frame: pd.DataFrame,
    candidates: Sequence[Sequence[str]],
    *,
    reject_opaque: bool = False,
    max_component_length: int = 240,
    excluded_columns: Sequence[str] = (),
) -> tuple[str, ...]:
    """Return a compact readable key, extending declared candidates as needed.

    Provider schemas occasionally contain two legitimate events that collide on
    the provider's usual natural grain.  A provider-ingestion boundary should
    not reject the whole batch in that case.  Prefer the declared recipes, then
    greedily add complete readable value columns until the rows are unique.

    Explicit calculated and ML writers can continue to call
    :func:`minimum_unique_key` or :func:`add_readable_id` directly when their
    declared grain is itself a data contract.
    """

    preferred = minimum_unique_key(
        frame,
        candidates,
        reject_opaque=reject_opaque,
    )
    if preferred:
        return preferred

    normalized_candidates = tuple(
        tuple(dict.fromkeys(str(column) for column in candidate))
        for candidate in candidates
        if candidate
    )
    excluded = {str(column) for column in excluded_columns}
    readable_columns: list[str] = []
    rendered: dict[str, pd.Series] = {}
    for raw_column in frame.columns:
        column = str(raw_column)
        if column == ID_COLUMN or column in excluded:
            continue
        values = frame[column].map(
            lambda value, column=column: _readable_value(value, column=column)
        )
        if values.eq("").any():
            continue
        if reject_opaque and values.map(is_opaque_identifier).any():
            continue
        if not values.empty and int(values.str.len().max()) > max_component_length:
            continue
        readable_columns.append(column)
        rendered[column] = values

    if not readable_columns:
        return ()
    if frame.empty:
        for candidate in normalized_candidates:
            if candidate and set(candidate).issubset(readable_columns):
                return candidate
        return (readable_columns[0],)

    seeds = [
        candidate
        for candidate in normalized_candidates
        if candidate and set(candidate).issubset(readable_columns)
    ]
    seeds.extend((column,) for column in readable_columns)
    seeds = list(dict.fromkeys(seeds))
    selected = min(
        seeds,
        key=lambda columns: (
            _duplicate_row_count(rendered, columns),
            len(columns),
            tuple(readable_columns.index(column) for column in columns),
        ),
    )
    if _duplicate_row_count(rendered, selected) == 0:
        return selected

    remaining = [column for column in readable_columns if column not in selected]
    while remaining:
        column = min(
            remaining,
            key=lambda value: (
                _duplicate_row_count(rendered, (*selected, value)),
                readable_columns.index(value),
            ),
        )
        selected = (*selected, column)
        remaining.remove(column)
        if _duplicate_row_count(rendered, selected) == 0:
            return selected
    return ()


def add_readable_id(
    frame: pd.DataFrame,
    *,
    key_columns: Sequence[str],
    fallback_prefix: str | None = None,
) -> pd.DataFrame:
    """Return ``frame`` with one readable, first-position Duckets ``id`` column.

    Callers provide the minimum natural columns that identify a row inside the
    persisted Parquet. Strict calculated-data callers fail when no complete,
    unique natural key is available. Provider-boundary callers may opt into a
    deterministic file-local fallback by supplying ``fallback_prefix``.
    """

    if frame.columns.has_duplicates:
        raise ValueError("Cannot create readable IDs for duplicate column names")

    output = frame.drop(columns=[ID_COLUMN], errors="ignore").copy()
    columns = tuple(dict.fromkeys(str(column) for column in key_columns))
    if not columns:
        if fallback_prefix is None:
            raise ValueError(
                "Readable ID requires at least one natural column"
            )
        output.insert(
            0,
            ID_COLUMN,
            _fallback_identifiers(output, prefix=fallback_prefix),
        )
        return output
    missing = [column for column in columns if column not in output.columns]
    if missing:
        if fallback_prefix is not None:
            output.insert(
                0,
                ID_COLUMN,
                _fallback_identifiers(output, prefix=fallback_prefix),
            )
            return output
        raise ValueError(
            "Readable ID columns are missing: " + ", ".join(missing)
        )

    if output.empty:
        output.insert(0, ID_COLUMN, pd.Series(dtype="string"))
        return output

    if columns:
        identifiers = output.loc[:, list(columns)].apply(
            lambda row: "|".join(
                _readable_value(row[column], column=column)
                for column in columns
            ),
            axis=1,
        ).astype("string")
    else:
        identifiers = pd.Series("", index=output.index, dtype="string")

    invalid = identifiers.isna() | identifiers.str.strip().eq("") | identifiers.str.contains(
        r"(?:^|\|)(?=\||$)",
        regex=True,
        na=True,
    )
    if invalid.any():
        if fallback_prefix is not None:
            output.insert(
                0,
                ID_COLUMN,
                _fallback_identifiers(output, prefix=fallback_prefix),
            )
            return output
        raise ValueError(
            "Readable ID natural columns contain missing or blank values"
        )

    duplicates = identifiers.duplicated(keep=False)
    if duplicates.any():
        if fallback_prefix is not None:
            output.insert(
                0,
                ID_COLUMN,
                _fallback_identifiers(output, prefix=fallback_prefix),
            )
            return output
        duplicate_values = identifiers.loc[duplicates].drop_duplicates().tolist()
        raise ValueError(
            "Readable ID natural columns are not unique: "
            + ", ".join(str(value) for value in duplicate_values[:5])
        )
    if contains_opaque_identifier(identifiers):
        if fallback_prefix is not None:
            output.insert(
                0,
                ID_COLUMN,
                _fallback_identifiers(output, prefix=fallback_prefix),
            )
            return output
        raise ValueError("Readable ID natural columns cannot contain a hash or UUID")

    output.insert(0, ID_COLUMN, identifiers)
    return output


def preserve_provider_native_id(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, str | None]:
    """Move a provider's literal ``id`` aside so Duckets can own ``id``.

    Other provider-native identifier columns are left untouched. The returned
    column name can be used as a natural key when the provider supplied no
    timestamp, accession number, URL, or other recognized key.
    """

    if frame.columns.has_duplicates:
        raise ValueError("Provider data contains duplicate column names")
    if ID_COLUMN not in frame.columns:
        return frame.copy(), None

    output = frame.copy()
    candidate = "provider_native_identifier"
    suffix = 2
    while candidate in output.columns:
        candidate = f"provider_native_identifier_{suffix}"
        suffix += 1
    output = output.rename(columns={ID_COLUMN: candidate})
    return output, candidate


def validate_raw_provider_id_columns(
    frame: pd.DataFrame,
    *,
    source: str,
) -> None:
    """Accept provider-native identity fields at the raw-data boundary.

    Raw payloads are provider-shaped evidence.  New provider ``*_id``, UUID, or
    hash fields are values to preserve, not reasons to reject an otherwise
    successful fetch.  Duckets still owns the single leading ``id`` column and
    still strips identity-shaped fields from normalized/calculated outputs.
    """

    del source
    if frame.columns.has_duplicates:
        raise ValueError("Raw provider data contains duplicate column names")


def without_internal_identity_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove internal identity and control fields from calculated data."""

    forbidden = [
        str(column)
        for column in frame.columns
        if str(column) != ID_COLUMN
        and (
            _internal_identity_column(str(column))
            or str(column).strip().lower() in PARQUET_CONTROL_PLANE_COLUMNS
        )
    ]
    return frame.drop(columns=forbidden, errors="ignore").copy()


def _internal_identity_column(column: str) -> bool:
    normalized = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])",
        "_",
        str(column).strip(),
    ).lower()
    return normalized.endswith(("_id", "_ids")) or bool(
        _IDENTITY_COLUMN_TERM.search(normalized)
    )


def _readable_value(value: object, *, column: str) -> str:
    if _missing(value):
        return ""
    if isinstance(value, pd.Timestamp):
        return _timestamp_text(value)
    if isinstance(value, datetime):
        return _timestamp_text(pd.Timestamp(value))
    if isinstance(value, date):
        return value.isoformat()
    if _timestamp_column(column):
        timestamp = pd.to_datetime(value, utc=True, errors="coerce")
        if not pd.isna(timestamp):
            return _timestamp_text(pd.Timestamp(timestamp))
    return str(value).strip().replace("\\", "\\\\").replace("|", "\\|")


def _timestamp_text(value: pd.Timestamp) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat().replace("+00:00", "Z")


def _missing(value: object) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def is_opaque_identifier(value: object) -> bool:
    text = str(value).strip()
    components = re.split(r"(?<!\\)\|", text)
    return any(
        _HASH_LIKE.fullmatch(component.strip())
        or _UUID.fullmatch(component.strip())
        for component in components
    )


def _timestamp_column(column: str) -> bool:
    normalized = column.strip().lower()
    return (
        normalized in {"timestamp", "ts_event", "ts_recv", "datetime"}
        or normalized.endswith("_timestamp")
        or normalized.endswith("_at")
    )


def contains_opaque_identifier(values: pd.Series) -> bool:
    """Check exact opaque-ID rules only for strings that can possibly match."""

    strings = values.astype("string")
    candidates = strings.str.contains(_OPAQUE_CANDIDATE, regex=True, na=True)
    return bool(
        candidates.any()
        and strings.loc[candidates].map(is_opaque_identifier).any()
    )


def _duplicate_row_count(
    rendered: dict[str, pd.Series],
    columns: Sequence[str],
) -> int:
    values = pd.DataFrame({column: rendered[column] for column in columns})
    return int(values.duplicated(keep=False).sum())


def _fallback_identifiers(frame: pd.DataFrame, *, prefix: str) -> pd.Series:
    readable_prefix = str(prefix).strip().replace("\\", "\\\\").replace("|", "\\|")
    readable_prefix = readable_prefix or "row"
    width = max(6, len(str(max(1, len(frame)))))
    return pd.Series(
        [
            f"{readable_prefix}|row-{position:0{width}d}"
            for position in range(1, len(frame) + 1)
        ],
        index=frame.index,
        dtype="string",
    )
