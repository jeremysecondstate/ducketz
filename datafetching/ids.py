from __future__ import annotations

from datetime import date, datetime
import re
from typing import Sequence

import pandas as pd

ID_COLUMN = "id"
RAW_PROVIDER_ID_COLUMNS = {
    "databento": frozenset({"instrument_id", "publisher_id"}),
}
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


def add_readable_id(
    frame: pd.DataFrame,
    *,
    key_columns: Sequence[str],
) -> pd.DataFrame:
    """Return ``frame`` with one readable, first-position Duckets ``id`` column.

    Callers provide the minimum natural columns that identify a row inside the
    persisted Parquet. Every writer fails when no complete, unique natural key
    is available.
    """

    if frame.columns.has_duplicates:
        raise ValueError("Cannot create readable IDs for duplicate column names")

    output = frame.drop(columns=[ID_COLUMN], errors="ignore").copy()
    columns = tuple(dict.fromkeys(str(column) for column in key_columns))
    if not columns:
        raise ValueError(
            "Readable ID requires at least one natural column"
        )
    missing = [column for column in columns if column not in output.columns]
    if missing:
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
        raise ValueError(
            "Readable ID natural columns contain missing or blank values"
        )

    duplicates = identifiers.duplicated(keep=False)
    if duplicates.any():
        duplicate_values = identifiers.loc[duplicates].drop_duplicates().tolist()
        raise ValueError(
            "Readable ID natural columns are not unique: "
            + ", ".join(str(value) for value in duplicate_values[:5])
        )
    if identifiers.map(is_opaque_identifier).any():
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
    """Reject internal identity fields unless the provider contract allows one."""

    allowed = RAW_PROVIDER_ID_COLUMNS.get(str(source).strip().lower(), frozenset())
    unexpected = sorted(
        str(column)
        for column in frame.columns
        if str(column) != ID_COLUMN
        and str(column) not in allowed
        and _internal_identity_column(str(column))
    )
    if unexpected:
        raise ValueError(
            "Raw provider identity columns are not registered for "
            f"{source}: {', '.join(unexpected)}"
        )


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
