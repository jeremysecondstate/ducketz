from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

import pandas as pd

from datafetching.ids import (
    ID_COLUMN,
    add_readable_id,
    without_internal_identity_columns,
)

_TEMPORAL_SUFFIXES = (
    "_at",
    "_timestamp",
    "_date",
)


def write_immutable_feature_partition(
    path: Path,
    frame: pd.DataFrame,
    *,
    columns: Sequence[str],
    natural_key: Sequence[str],
) -> Path:
    target = Path(path)
    with _exclusive_partition_writer(target):
        return _write_immutable_feature_partition_unlocked(
            target,
            frame,
            columns=columns,
            natural_key=natural_key,
        )


def _write_immutable_feature_partition_unlocked(
    path: Path,
    frame: pd.DataFrame,
    *,
    columns: Sequence[str],
    natural_key: Sequence[str],
) -> Path:
    """Append immutable calculated rows to one atomically replaced partition.

    The physical partition can be replaced atomically, but an existing natural
    row can never be rewritten with different content. Every output has exactly
    one leading readable ``id``.
    """

    target = Path(path)
    ordered = tuple(columns)
    keys = tuple(natural_key)
    if not ordered or ordered[0] == ID_COLUMN:
        raise ValueError("columns must describe the payload after the leading id")
    if len(ordered) != len(set(ordered)):
        raise ValueError("Calculated feature columns must be unique")
    if not keys or not set(keys).issubset(ordered):
        raise ValueError("Natural key columns must be present in the output schema")

    incoming = _prepare(frame, columns=ordered)
    _reject_duplicate_keys(incoming, keys, label="incoming")
    existing = (
        _prepare(pd.read_parquet(target), columns=ordered)
        if target.is_file()
        else pd.DataFrame(columns=ordered)
    )
    _reject_duplicate_keys(existing, keys, label="existing")

    if existing.empty:
        output = incoming
    elif incoming.empty:
        output = existing
    else:
        common = existing.merge(
            incoming,
            on=list(keys),
            how="inner",
            suffixes=("__existing", "__incoming"),
        )
        conflicts: list[str] = []
        value_columns = [column for column in ordered if column not in keys]
        for column in value_columns:
            left = common[f"{column}__existing"]
            right = common[f"{column}__incoming"]
            equal = left.eq(right) | (left.isna() & right.isna())
            if not equal.all():
                conflicts.append(column)
        if conflicts:
            raise ValueError(
                "Immutable calculated feature rows conflict on natural key; "
                "append a new availability version instead. Columns: "
                + ", ".join(conflicts)
            )
        incoming_only = incoming.merge(
            existing.loc[:, list(keys)],
            on=list(keys),
            how="left",
            indicator=True,
        ).loc[lambda values: values["_merge"].eq("left_only"), ordered]
        output = pd.concat([existing, incoming_only], ignore_index=True, sort=False)

    output = output.sort_values(list(keys), kind="stable").reset_index(drop=True)
    output = add_readable_id(output, key_columns=keys)
    if list(output.columns)[0] != ID_COLUMN:
        raise ValueError("Calculated feature id must be the leading column")
    identity_columns = [
        column
        for column in output.columns
        if str(column).strip().lower() == ID_COLUMN
    ]
    if identity_columns != [ID_COLUMN]:
        raise ValueError("Calculated features require exactly one id column")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        output.to_parquet(temporary, index=False)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


@contextmanager
def _exclusive_partition_writer(target: Path) -> Iterator[None]:
    """Fail closed if another process is updating the same partition."""

    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(f".{target.name}.write.lock")
    descriptor: int | None = None
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(descriptor, f"pid={os.getpid()}\n".encode("utf-8"))
        os.close(descriptor)
        descriptor = None
    except FileExistsError as exc:
        raise RuntimeError(
            f"Another writer is updating calculated partition: {target}"
        ) from exc
    try:
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock.unlink(missing_ok=True)


def _prepare(frame: pd.DataFrame, *, columns: tuple[str, ...]) -> pd.DataFrame:
    raw_values = frame.drop(columns=[ID_COLUMN], errors="ignore").copy()
    values = without_internal_identity_columns(frame).drop(
        columns=[ID_COLUMN],
        errors="ignore",
    )
    # This is an explicit provider revision key in the declared ALFRED schema,
    # not an internal opaque identifier.  Preserve it when a caller names it
    # in the immutable payload contract.
    if "revision_identity" in columns and "revision_identity" in raw_values:
        values["revision_identity"] = raw_values["revision_identity"]
    missing = sorted(set(columns).difference(values.columns))
    if missing and not values.empty:
        raise ValueError(
            "Calculated feature frame is missing schema columns: "
            + ", ".join(missing)
        )
    values = values.reindex(columns=columns).copy()
    for column in columns:
        normalized = column.strip().lower()
        if normalized == "realtime_end":
            # ALFRED uses 9999-12-31 as its open-ended provider interval.
            # Preserve that exact identity instead of coercing it outside
            # pandas' nanosecond timestamp range and silently producing NaT.
            values[column] = values[column].astype("string")
        elif normalized.endswith(_TEMPORAL_SUFFIXES) or normalized in {
            "window_start",
            "window_end",
            "observation_date",
            "realtime_start",
            "period_end_date",
        }:
            values[column] = pd.to_datetime(
                values[column],
                utc=True,
                errors="coerce",
            )
    return values


def _reject_duplicate_keys(
    frame: pd.DataFrame,
    keys: tuple[str, ...],
    *,
    label: str,
) -> None:
    if frame.empty:
        return
    missing_key = frame.loc[:, list(keys)].isna().any(axis=1)
    if missing_key.any():
        raise ValueError(
            f"{label} calculated features contain missing natural-key values"
        )
    duplicates = frame.duplicated(list(keys), keep=False)
    if duplicates.any():
        raise ValueError(
            f"{label} calculated features contain duplicate natural keys"
        )
