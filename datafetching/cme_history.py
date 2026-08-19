from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pandas as pd

from app.services.databento_cme_context import DatabentoCmeContextSpec
from datafetching.ids import adaptive_unique_key, add_readable_id
from datafetching.layout import safe_token
from ml.artifacts import file_checksum


CME_CURSOR_VERSION = "cme-successful-query-cursor-v1"
CME_EVENT_HISTORY_VERSION = "cme-partitioned-event-history-v1"
CME_L2_SNAPSHOT_VERSION = "cme-l2-snapshot-v1"
CME_L2_POINTER_VERSION = "cme-l2-pointer-v1"
CME_WRITER_LOCK_NAME = ".ducketz-cme-writer.lock"
_EVENT_TIME_COLUMNS = (
    "timestamp",
    "ts_event",
    "databento_ts_event",
    "ts_recv",
    "databento_ts_recv",
)
_VOLATILE_EVENT_COLUMNS = {
    "id",
    "fetched_at",
    "range_start",
    "range_end",
    "initial_range_start",
    "initial_range_end",
    "effective_range_start",
    "effective_range_end",
    "latest_event_timestamp",
    "limit",
    "request_limit_saturated",
    "latest_window_shrink_count",
    "empty_window_expansion_count",
    "cme_schema_status",
    "row_index",
}


@dataclass(frozen=True)
class CmeCursor:
    group_key: str
    schema: str
    queried_through: pd.Timestamp
    successful_at: pd.Timestamp
    last_event_at: pd.Timestamp | None
    row_count: int


@dataclass(frozen=True)
class CmePartitionWrite:
    paths: tuple[Path, ...]
    rows: int
    written: int
    reused: int


@dataclass(frozen=True)
class CmeL2Snapshot:
    snapshot_for: pd.Timestamp
    directory: Path
    snapshot_path: Path
    receipt_path: Path
    rows: int
    reused: bool


def cme_writer_lock_path(datastore_root: Path) -> Path:
    return Path(datastore_root) / CME_WRITER_LOCK_NAME


def cme_cursor_path(
    datastore_root: Path,
    *,
    group_key: str,
    schema: str,
) -> Path:
    return (
        Path(datastore_root)
        / "pools"
        / "cme"
        / "runtime"
        / "cursors"
        / f"{safe_token(group_key)}__{safe_token(schema)}.json"
    )


def read_cme_cursor(
    datastore_root: Path,
    *,
    group_key: str,
    schema: str,
) -> CmeCursor | None:
    path = cme_cursor_path(
        datastore_root,
        group_key=group_key,
        schema=schema,
    )
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"CME cursor is unreadable: {path}") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != CME_CURSOR_VERSION:
        raise RuntimeError(f"CME cursor contract is invalid: {path}")
    if (
        payload.get("group_key") != group_key
        or payload.get("provider_schema") != schema
    ):
        raise RuntimeError(f"CME cursor identity does not match its path: {path}")
    return CmeCursor(
        group_key=group_key,
        schema=schema,
        queried_through=_utc(payload.get("queried_through"), "queried_through"),
        successful_at=_utc(payload.get("successful_at"), "successful_at"),
        last_event_at=_optional_utc(payload.get("last_event_at")),
        row_count=int(payload.get("row_count", 0)),
    )


def publish_cme_cursor(
    datastore_root: Path,
    *,
    spec: DatabentoCmeContextSpec,
    queried_through: object,
    successful_at: object,
    last_event_at: object | None,
    row_count: int,
) -> CmeCursor:
    through = _utc(queried_through, "queried_through")
    completed = _utc(successful_at, "successful_at")
    event = _optional_utc(last_event_at)
    path = cme_cursor_path(
        datastore_root,
        group_key=spec.group_key,
        schema=spec.schema,
    )
    payload = {
        "schema_version": CME_CURSOR_VERSION,
        "provider": "databento",
        "dataset": spec.dataset,
        "group_key": spec.group_key,
        "output_symbol": spec.output_symbol,
        "provider_schema": spec.schema,
        "provider_stype_in": spec.stype_in,
        "symbols": list(spec.symbols),
        "queried_through": through.isoformat(),
        "successful_at": completed.isoformat(),
        "last_event_at": event.isoformat() if event is not None else None,
        "row_count": int(row_count),
    }
    _write_json_atomic(path, payload)
    return CmeCursor(
        spec.group_key,
        spec.schema,
        through,
        completed,
        event,
        int(row_count),
    )


def cme_event_root(
    datastore_root: Path,
    *,
    group_key: str,
    schema: str,
    scope: str,
) -> Path:
    return (
        Path(datastore_root)
        / "pools"
        / "cme"
        / "events"
        / "databento"
        / safe_token(group_key)
        / safe_token(schema)
        / safe_token(scope)
    )


def cme_normalized_event_paths(
    datastore_root: Path,
    *,
    group_key: str,
    schema: str,
) -> tuple[Path, ...]:
    root = cme_event_root(
        datastore_root,
        group_key=group_key,
        schema=schema,
        scope="normalized",
    )
    return tuple(sorted(root.rglob("events.parquet"))) if root.is_dir() else ()


def persist_cme_event_history(
    datastore_root: Path,
    *,
    spec: DatabentoCmeContextSpec,
    normalized_rows: Sequence[Mapping[str, object]],
    raw_frame: pd.DataFrame | None,
) -> CmePartitionWrite:
    """Upsert exact events into bounded daily/hourly Parquet partitions."""

    frames: list[tuple[str, pd.DataFrame]] = []
    normalized = pd.DataFrame(list(normalized_rows))
    if not normalized.empty and "cme_row_kind" in normalized.columns:
        normalized = normalized.loc[
            ~normalized["cme_row_kind"].astype("string").eq("schema_status")
        ].copy()
    if not normalized.empty:
        frames.append(("normalized", normalized))
    if raw_frame is not None and not raw_frame.empty:
        raw = raw_frame.reset_index(drop=True).copy()
        fetched_at = (
            pd.to_datetime(normalized["fetched_at"], utc=True, errors="coerce").max()
            if not normalized.empty and "fetched_at" in normalized.columns
            else pd.Timestamp.now(tz="UTC")
        )
        raw["fetched_at"] = fetched_at
        raw["provider_dataset"] = spec.dataset
        raw["provider_schema"] = spec.schema
        raw["provider_stype_in"] = spec.stype_in
        raw["cme_context_group"] = spec.group_key
        frames.append(("raw", raw))

    paths: list[Path] = []
    written = 0
    reused = 0
    row_count = (
        len(normalized)
        if not normalized.empty
        else len(raw_frame)
        if raw_frame is not None
        else 0
    )
    for scope, frame in frames:
        prepared = _prepare_event_frame(frame)
        event_time = _event_times(prepared)
        if event_time.isna().any():
            raise ValueError(
                f"CME {scope} history contains rows without an event timestamp"
            )
        prepared["__event_partition_time"] = event_time
        granularity = "day" if spec.schema.startswith("ohlcv-") else "hour"
        grouping = (
            prepared["__event_partition_time"].dt.strftime("%Y-%m-%d")
            if granularity == "day"
            else prepared["__event_partition_time"].dt.strftime("%Y-%m-%d/%H")
        )
        for partition, group in prepared.groupby(grouping, sort=True):
            output = group.drop(columns="__event_partition_time").reset_index(drop=True)
            target = _event_partition_path(
                datastore_root,
                group_key=spec.group_key,
                schema=spec.schema,
                scope=scope,
                partition=str(partition),
                hourly=granularity == "hour",
            )
            changed = _upsert_event_partition(target, output)
            paths.append(target)
            written += int(changed)
            reused += int(not changed)
    return CmePartitionWrite(
        tuple(dict.fromkeys(paths)),
        row_count,
        written,
        reused,
    )


def five_minute_boundary(value: object) -> pd.Timestamp:
    return _utc(value, "snapshot boundary").floor("5min")


def publish_cme_l2_snapshot(
    datastore_root: Path,
    *,
    snapshot_for: object,
    available_not_after: object | None = None,
    require_all_fresh: bool = False,
    expected_stream_symbols: Mapping[
        tuple[str, str], Sequence[str]
    ] | None = None,
) -> CmeL2Snapshot | None:
    """Publish latest L2 state using only events available by the boundary."""

    boundary = five_minute_boundary(snapshot_for)
    availability_cutoff = _utc(
        boundary if available_not_after is None else available_not_after,
        "availability cutoff",
    )
    root = (
        Path(datastore_root)
        / "pools"
        / "cme"
        / "snapshots"
        / "l2"
        / "databento"
        / "5m"
    )
    destination = root / str(boundary.value)
    if (destination / "receipt.json").is_file():
        stored = pd.read_parquet(destination / "snapshot.parquet")
        if require_all_fresh and not _strict_l2_snapshot_ready(
            stored,
            datastore_root=datastore_root,
            expected_stream_symbols=expected_stream_symbols,
        ):
            return None
        _publish_l2_pointer(
            datastore_root,
            root=root,
            destination=destination,
            snapshot_for=boundary,
        )
        return CmeL2Snapshot(
            boundary,
            destination,
            destination / "snapshot.parquet",
            destination / "receipt.json",
            len(stored),
            True,
        )

    selected: list[pd.DataFrame] = []
    cursor_lineage: list[dict[str, object]] = []
    strict_streams_ready = True
    events_root = Path(datastore_root) / "pools" / "cme" / "events" / "databento"
    if not events_root.is_dir():
        return None
    for group_root in sorted(path for path in events_root.iterdir() if path.is_dir()):
        for schema_root in sorted(path for path in group_root.iterdir() if path.is_dir()):
            schema = schema_root.name
            if not (schema.startswith("bbo-") or schema.startswith("mbp-") or schema.startswith("mbo")):
                continue
            paths = tuple(sorted((schema_root / "normalized").rglob("events.parquet")))
            if not paths:
                continue
            expected_symbols = _expected_stream_symbols(
                datastore_root,
                group_key=group_root.name,
                schema=schema,
                configured=expected_stream_symbols,
            )
            eligible = _causal_latest_events(
                paths,
                boundary=boundary,
                available_not_after=availability_cutoff,
                expected_symbols=expected_symbols,
            )
            if eligible.empty:
                strict_streams_ready = False
                continue
            symbol_column = next(
                (
                    column
                    for column in ("provider_symbol", "symbol", "raw_symbol")
                    if column in eligible.columns
                ),
                None,
            )
            if symbol_column is None:
                strict_streams_ready = False
                continue
            observed_symbols = frozenset(
                eligible[symbol_column].astype("string").dropna().astype(str)
            )
            if expected_symbols and not expected_symbols.issubset(observed_symbols):
                strict_streams_ready = False
            latest = eligible.copy()
            latest["snapshot_for"] = boundary
            latest["event_age_seconds"] = (
                boundary - latest["__event_at"]
            ).dt.total_seconds()
            latest["receipt_age_seconds"] = (
                boundary
                - pd.to_datetime(latest["fetched_at"], utc=True, errors="coerce")
            ).dt.total_seconds()
            maximum_age = 60.0 if schema.startswith(("mbp-", "mbo")) else 300.0
            latest["quality_status"] = latest["event_age_seconds"].le(maximum_age).map(
                {True: "FRESH", False: "STALE"}
            )
            latest["causally_available"] = True
            latest["calculation"] = "latest-l2-state"
            latest["schema_version"] = CME_L2_SNAPSHOT_VERSION
            latest["provider_schema"] = schema
            latest["cme_context_group"] = group_root.name
            latest = latest.drop(columns="__event_at")
            selected.append(latest)
            cursor = read_cme_cursor(
                datastore_root,
                group_key=group_root.name,
                schema=schema,
            )
            if cursor is not None:
                cursor_lineage.append(
                    {
                        "group_key": cursor.group_key,
                        "provider_schema": cursor.schema,
                        "queried_through": cursor.queried_through.isoformat(),
                    }
                )
    if not selected:
        return None

    snapshot = pd.concat(selected, ignore_index=True, sort=False)
    if require_all_fresh and (
        not strict_streams_ready
        or not snapshot["quality_status"].astype("string").eq("FRESH").all()
    ):
        return None
    snapshot = _add_event_ids(
        snapshot,
        preferred=(
            "snapshot_for",
            "cme_context_group",
            "provider_schema",
            "provider_symbol",
            "timestamp",
            "sequence",
            "action",
            "side",
            "depth",
        ),
    )
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{boundary.value}.tmp-{os.getpid()}-",
            dir=root,
        )
    )
    try:
        snapshot_path = staging / "snapshot.parquet"
        snapshot.to_parquet(snapshot_path, index=False)
        manifest = {
            "schema_version": CME_L2_SNAPSHOT_VERSION,
            "snapshot_for": boundary.isoformat(),
            "available_at": pd.Timestamp.now(tz="UTC").isoformat(),
            "row_count": len(snapshot),
            "snapshot_checksum_sha256": file_checksum(snapshot_path),
            "cursor_lineage": cursor_lineage,
        }
        _write_json(staging / "manifest.json", manifest)
        receipt = {
            **manifest,
            "run_path": destination.relative_to(Path(datastore_root)).as_posix(),
            "manifest_checksum_sha256": file_checksum(staging / "manifest.json"),
        }
        _write_json(staging / "receipt.json", receipt)
        staging.replace(destination)
    except BaseException:
        _remove_staging(staging)
        raise
    _publish_l2_pointer(
        datastore_root,
        root=root,
        destination=destination,
        snapshot_for=boundary,
    )
    return CmeL2Snapshot(
        boundary,
        destination,
        destination / "snapshot.parquet",
        destination / "receipt.json",
        len(snapshot),
        False,
    )


def _causal_latest_events(
    paths: Sequence[Path],
    *,
    boundary: pd.Timestamp,
    available_not_after: pd.Timestamp | None = None,
    expected_symbols: frozenset[str],
) -> pd.DataFrame:
    """Read newest partitions first and stop once every expected state is found."""

    availability_cutoff = available_not_after or boundary
    latest_by_symbol: dict[str, pd.DataFrame] = {}
    for path in sorted(paths, reverse=True):
        frame = pd.read_parquet(path)
        event_at = _event_times(frame)
        fetched_at = pd.to_datetime(
            frame.get("fetched_at"), utc=True, errors="coerce"
        )
        eligible = frame.loc[
            event_at.le(boundary) & fetched_at.le(availability_cutoff)
        ].copy()
        if eligible.empty:
            continue
        eligible["__event_at"] = event_at.loc[eligible.index]
        symbol_column = next(
            (
                column
                for column in ("provider_symbol", "symbol", "raw_symbol")
                if column in eligible.columns
            ),
            None,
        )
        if symbol_column is None:
            continue
        for symbol, rows in eligible.groupby(symbol_column, sort=False):
            clean_symbol = str(symbol)
            if clean_symbol in latest_by_symbol:
                continue
            latest_at = rows["__event_at"].max()
            latest_by_symbol[clean_symbol] = rows.loc[
                rows["__event_at"].eq(latest_at)
            ].copy()
        if expected_symbols and expected_symbols.issubset(latest_by_symbol):
            break
    if not latest_by_symbol:
        return pd.DataFrame()
    return pd.concat(latest_by_symbol.values(), ignore_index=True, sort=False)


def _strict_l2_snapshot_ready(
    frame: pd.DataFrame,
    *,
    datastore_root: Path,
    expected_stream_symbols: Mapping[
        tuple[str, str], Sequence[str]
    ] | None = None,
) -> bool:
    if frame.empty or "quality_status" not in frame.columns:
        return False
    if not frame["quality_status"].astype("string").eq("FRESH").all():
        return False
    required = {
        "cme_context_group",
        "provider_schema",
        "provider_symbol",
    }
    if not required.issubset(frame.columns):
        return False
    events_root = Path(datastore_root) / "pools" / "cme" / "events" / "databento"
    for group_root in sorted(path for path in events_root.iterdir() if path.is_dir()):
        for schema_root in sorted(path for path in group_root.iterdir() if path.is_dir()):
            schema = schema_root.name
            if not schema.startswith(("bbo-", "mbp-", "mbo")):
                continue
            if not tuple((schema_root / "normalized").rglob("events.parquet")):
                continue
            expected = _expected_stream_symbols(
                datastore_root,
                group_key=group_root.name,
                schema=schema,
                configured=expected_stream_symbols,
            )
            rows = frame.loc[
                frame["cme_context_group"].astype("string").eq(group_root.name)
                & frame["provider_schema"].astype("string").eq(schema)
            ]
            observed = frozenset(
                rows["provider_symbol"].astype("string").dropna().astype(str)
            )
            if rows.empty or (expected and not expected.issubset(observed)):
                return False
    return True


def _expected_stream_symbols(
    datastore_root: Path,
    *,
    group_key: str,
    schema: str,
    configured: Mapping[tuple[str, str], Sequence[str]] | None,
) -> frozenset[str]:
    key = (group_key, schema)
    if configured is not None and key in configured:
        return frozenset(str(value) for value in configured[key] if str(value))
    return _cursor_symbols(
        datastore_root,
        group_key=group_key,
        schema=schema,
    )


def _cursor_symbols(
    datastore_root: Path,
    *,
    group_key: str,
    schema: str,
) -> frozenset[str]:
    path = cme_cursor_path(
        datastore_root,
        group_key=group_key,
        schema=schema,
    )
    if not path.is_file():
        return frozenset()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return frozenset()
    values = payload.get("symbols") if isinstance(payload, Mapping) else None
    if not isinstance(values, list):
        return frozenset()
    return frozenset(str(value) for value in values if str(value))


def _publish_l2_pointer(
    datastore_root: Path,
    *,
    root: Path,
    destination: Path,
    snapshot_for: pd.Timestamp,
) -> None:
    pointer = {
        "schema_version": CME_L2_POINTER_VERSION,
        "snapshot_for": snapshot_for.isoformat(),
        "run_path": destination.relative_to(Path(datastore_root)).as_posix(),
        "receipt_checksum_sha256": file_checksum(destination / "receipt.json"),
    }
    _write_json_atomic(root / "latest.json", pointer)


def _event_partition_path(
    datastore_root: Path,
    *,
    group_key: str,
    schema: str,
    scope: str,
    partition: str,
    hourly: bool,
) -> Path:
    pieces = partition.split("/")
    path = cme_event_root(
        datastore_root,
        group_key=group_key,
        schema=schema,
        scope=scope,
    ) / f"date={pieces[0]}"
    if hourly:
        path /= f"hour={pieces[1]}"
    return path / "events.parquet"


def _upsert_event_partition(path: Path, incoming: pd.DataFrame) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    incoming = _prepare_event_frame(incoming)
    existing = (
        _prepare_event_frame(pd.read_parquet(path))
        if path.is_file()
        else pd.DataFrame()
    )
    key_columns = _event_natural_key(incoming)
    if existing.empty:
        output = incoming.drop_duplicates(list(key_columns), keep="last")
        changed = not output.empty
    elif key_columns and all(column in existing.columns for column in key_columns):
        existing_no_id = existing.drop(columns="id", errors="ignore").drop_duplicates(
            list(key_columns), keep="last"
        )
        incoming_no_id = incoming.drop(columns="id", errors="ignore")
        columns = list(dict.fromkeys([*existing_no_id.columns, *incoming_no_id.columns]))
        existing_no_id = existing_no_id.reindex(columns=columns)
        incoming_no_id = incoming_no_id.reindex(columns=columns).drop_duplicates(
            list(key_columns), keep="last"
        )
        existing_index = pd.MultiIndex.from_frame(existing_no_id.loc[:, list(key_columns)])
        incoming_index = pd.MultiIndex.from_frame(incoming_no_id.loc[:, list(key_columns)])
        common = incoming_index.intersection(existing_index)
        compare = [
            column for column in columns
            if column not in key_columns and column not in _VOLATILE_EVENT_COLUMNS
        ]
        identical_keys = common[:0]
        if len(common):
            left = existing_no_id.set_index(list(key_columns)).reindex(common).loc[:, compare]
            right = incoming_no_id.set_index(list(key_columns)).reindex(common).loc[:, compare]
            equal_rows = (
                (left.eq(right) | (left.isna() & right.isna()))
                .fillna(False)
                .all(axis=1)
                if compare
                else pd.Series(True, index=common)
            )
            identical_keys = common[equal_rows.to_numpy()]
        replacement_mask = ~incoming_index.isin(identical_keys)
        if not bool(replacement_mask.any()):
            return False
        replacements = incoming_no_id.loc[replacement_mask]
        replacement_index = incoming_index[replacement_mask]
        retained = existing_no_id.loc[~existing_index.isin(replacement_index)]
        output = pd.concat([retained, replacements], ignore_index=True, sort=False)
        changed = True
    else:
        columns = list(dict.fromkeys([*existing.columns, *incoming.columns]))
        output = pd.concat(
            [existing.reindex(columns=columns), incoming.reindex(columns=columns)],
            ignore_index=True,
            sort=False,
        ).drop_duplicates(keep="first")
        changed = len(output) != len(existing)
    if not changed:
        return False
    output = _add_event_ids(output.drop(columns="id", errors="ignore"), preferred=key_columns)
    if key_columns:
        output = output.sort_values(list(key_columns), kind="mergesort")
    temporary = path.with_suffix(".tmp.parquet")
    try:
        output.reset_index(drop=True).to_parquet(temporary, index=False)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _prepare_event_frame(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    for column in output.columns:
        normalized = str(column).strip().lower()
        if (
            normalized in _EVENT_TIME_COLUMNS
            or normalized == "fetched_at"
            or normalized.endswith("_timestamp")
            or normalized.endswith("_at")
        ):
            output[column] = pd.to_datetime(
                output[column], utc=True, errors="coerce"
            ).astype("datetime64[ns, UTC]")
    return output


def _event_times(frame: pd.DataFrame) -> pd.Series:
    output = pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns, UTC]")
    for column in _EVENT_TIME_COLUMNS:
        if column not in frame.columns:
            continue
        parsed = pd.to_datetime(frame[column], utc=True, errors="coerce")
        output = output.fillna(parsed)
    return output


def _event_natural_key(frame: pd.DataFrame) -> tuple[str, ...]:
    candidates = (
        (
            "provider_symbol",
            "ts_event",
            "sequence",
            "action",
            "side",
            "depth",
            "price",
        ),
        ("symbol", "ts_event", "sequence", "action", "side", "depth", "price"),
        ("provider_symbol", "timestamp", "sequence", "side", "depth"),
        ("symbol", "timestamp", "sequence", "side", "depth"),
        ("provider_symbol", "timestamp"),
        ("symbol", "timestamp"),
        ("instrument_id", "ts_event", "sequence", "action", "side", "depth"),
        ("instrument_id", "ts_event", "sequence"),
        ("ts_event", "sequence", "action", "side", "depth"),
        ("timestamp", "sequence"),
    )
    return adaptive_unique_key(
        frame.drop(columns="id", errors="ignore"),
        candidates,
        excluded_columns=tuple(_VOLATILE_EVENT_COLUMNS),
    )


def _add_event_ids(
    frame: pd.DataFrame,
    *,
    preferred: Sequence[str],
) -> pd.DataFrame:
    key = tuple(column for column in preferred if column in frame.columns)
    if not key or frame.loc[:, list(key)].isna().any(axis=None) or frame.duplicated(list(key)).any():
        key = _event_natural_key(frame)
    if not key:
        raise ValueError("CME event rows have no complete readable natural key")
    return add_readable_id(frame.reset_index(drop=True), key_columns=key)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(dict(payload), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    try:
        _write_json(temporary, payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_staging(path: Path) -> None:
    if not path.is_dir() or ".tmp-" not in path.name:
        return
    for child in path.iterdir():
        if child.is_file():
            child.unlink(missing_ok=True)
    path.rmdir()


def _utc(value: object, label: str) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError(f"CME {label} is invalid")
    return pd.Timestamp(timestamp)


def _optional_utc(value: object | None) -> pd.Timestamp | None:
    if value in (None, ""):
        return None
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(timestamp) else pd.Timestamp(timestamp)


__all__ = [
    "CME_EVENT_HISTORY_VERSION",
    "CME_L2_SNAPSHOT_VERSION",
    "CmeCursor",
    "CmeL2Snapshot",
    "CmePartitionWrite",
    "cme_normalized_event_paths",
    "cme_writer_lock_path",
    "five_minute_boundary",
    "persist_cme_event_history",
    "publish_cme_cursor",
    "publish_cme_l2_snapshot",
    "read_cme_cursor",
]
