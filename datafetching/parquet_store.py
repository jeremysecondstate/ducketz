from __future__ import annotations

import json
import math
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from numbers import Real
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from datafetching.bar_schema import (
    NORMALIZED_BAR_COLUMNS,
    legacy_bar_completion_mask,
    normalized_bar_canonical_path,
    normalized_bar_file_sort_key,
    normalized_bar_schema_is_canonical,
    project_normalized_bar_frame,
    read_normalized_bar_parquet,
    write_normalized_bar_parquet,
)
from datafetching.bar_timing import annotate_bar_timing, completed_market_bars
from datafetching.ids import (
    ID_COLUMN,
    add_readable_id,
    minimum_unique_key,
    preserve_provider_native_id,
    validate_raw_provider_id_columns,
    without_internal_identity_columns,
)
from datafetching.layout import (
    DEFAULT_POOL,
    canonical_timeframe,
    pool_data_folder,
    safe_token,
    stock_data_folder,
)

# PC_DATASTORE_DIR = Path(r"C:\DATASTORE")
PC_DATASTORE_DIR = Path(r"C:\My Drive\DATASTORE")

DEFAULT_DATASTORE_DIR = Path(__file__).resolve().parent / "datastore"
DATASTORE_TARGETS = {"pc": PC_DATASTORE_DIR, "local": DEFAULT_DATASTORE_DIR}
TEMPORAL_KEY_COLUMNS = {
    "timestamp",
    "ts_event",
    "ts_recv",
    "datetime",
    "time",
    "fetched_at",
    "available_at",
}
TEMPORAL_METADATA_COLUMNS = {
    "range_start",
    "range_end",
    "initial_range_start",
    "initial_range_end",
    "effective_range_start",
    "effective_range_end",
    "latest_event_timestamp",
}
NULLABLE_BLANK_TEMPORAL_COLUMNS = {"latest_event_timestamp"}
VOLATILE_COLUMNS = {
    ID_COLUMN,
    "fetched_at",
    "row_index",
    "freshness_age_days",
    "range_start",
    "range_end",
    "initial_range_start",
    "initial_range_end",
    "effective_range_start",
    "effective_range_end",
    "empty_window_expansion_count",
    "latest_event_timestamp",
    "fetch_profile",
}
DATABENTO_MBP_NATURAL_KEY = (
    "symbol",
    "ts_event",
    "sequence",
    "action",
    "side",
    "depth",
    "price",
)
DATABENTO_BBO_NATURAL_KEY = (
    "symbol",
    "ts_event",
    "sequence",
    "side",
    "price",
)


class ParquetStore:
    """Store one canonical, idempotently upserted Parquet per provider request."""

    def __init__(self, root_dir: Path | str | None = None, *, target: str | None = None) -> None:
        self.root_dir = resolve_datastore_dir(root_dir=root_dir, target=target)
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def has_bar_history(self, symbol: str) -> bool:
        root = self.root_dir / "stocks" / safe_token(symbol.strip().upper()) / "bars"
        if not root.is_dir():
            return False
        return any(
            not re.search(r"_\d{8}T\d{6}\.\d{6}Z$", path.stem)
            for path in root.glob("*/*/normalized/*.parquet")
        )

    def save_quote(self, quote: Any) -> Path | None:
        row = _object_row(quote)
        frame = _frame([row])
        return self._write(
            scope="normalized",
            source=str(row.get("source") or getattr(quote, "source", "unknown")),
            category="quotes",
            symbol=str(row.get("symbol") or getattr(quote, "symbol", "UNKNOWN")),
            frame=frame,
            keys=("fetched_at",),
            mode="append_if_changed",
        )

    def save_bars(
        self,
        source: str,
        symbol: str,
        timeframe: str,
        bars: list[Any],
        *,
        request_key: str,
        metadata: dict[str, object] | None = None,
        as_of: datetime | pd.Timestamp | None = None,
    ) -> Path | None:
        if not bars:
            return None
        canonical = canonical_timeframe(source, timeframe, metadata)
        rows = []
        for bar in completed_market_bars(
            bars,
            timeframe=canonical,
            as_of=as_of,
        ):
            row = _object_row(bar)
            rows.append(
                {
                    column: row.get(column)
                    for column in NORMALIZED_BAR_COLUMNS
                    if column in row
                }
            )
        frame = pd.DataFrame(rows, columns=NORMALIZED_BAR_COLUMNS)
        return self._write(
            scope="normalized",
            source=source,
            category="bars",
            symbol=symbol,
            suffix=request_key,
            timeframe=canonical,
            frame=project_normalized_bar_frame(frame),
            keys=("timestamp",),
            storage_columns=NORMALIZED_BAR_COLUMNS,
            bar_timeframe=canonical,
            bar_as_of=as_of,
        )

    def save_corporate_rows(
        self,
        source: str,
        symbol: str,
        request_key: str,
        rows: list[dict[str, Any]],
        *,
        metadata: dict[str, object] | None = None,
        keys: Sequence[str] | None = None,
        mode: str = "upsert",
    ) -> Path | None:
        return self._save_rows(
            source,
            "corporate",
            symbol,
            request_key,
            rows,
            metadata,
            keys=keys,
            mode=mode,
        )

    def save_macro_rows(
        self,
        source: str,
        symbol: str,
        request_key: str,
        rows: list[dict[str, Any]],
        *,
        metadata: dict[str, object] | None = None,
        pool: str = DEFAULT_POOL,
        mode: str = "upsert",
    ) -> Path | None:
        return self._save_rows(
            source,
            "macro",
            symbol,
            request_key,
            rows,
            metadata,
            pool=pool,
            mode=mode,
        )

    def save_raw_payload(
        self,
        *,
        source: str,
        category: str,
        symbol: str,
        endpoint: str,
        payload: Any,
        timeframe: str | None = None,
        dataset_key: str | None = None,
        pool: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Path | None:
        extra = dict(metadata or {})
        canonical = canonical_timeframe(source, timeframe, extra) if category == "bars" else ""
        row = {
            "symbol": symbol,
            "source": source,
            "endpoint": endpoint,
            "canonical_timeframe": canonical,
            "fetched_at": _now(),
            "payload_json": json.dumps(payload, default=str, sort_keys=True),
            **extra,
        }
        frame, provider_id_column = preserve_provider_native_id(_frame([row]))
        return self._write(
            scope="raw",
            source=source,
            category=category,
            symbol=symbol,
            suffix=endpoint,
            timeframe=canonical,
            dataset_key=dataset_key or endpoint,
            pool=pool,
            frame=frame,
            keys=(
                ("fetched_at",)
                if "fetched_at" in frame.columns
                else (provider_id_column,)
                if provider_id_column is not None
                else ()
            ),
            mode="snapshot",
        )

    def save_raw_frame(
        self,
        *,
        source: str,
        category: str,
        symbol: str,
        endpoint: str,
        frame: pd.DataFrame,
        timeframe: str | None = None,
        dataset_key: str | None = None,
        pool: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> Path | None:
        if frame.empty:
            return None
        extra = dict(metadata or {})
        canonical = canonical_timeframe(source, timeframe, extra) if category == "bars" else ""
        output = frame.reset_index(drop=True).copy()
        if canonical:
            output["canonical_timeframe"] = canonical
        for key, value in extra.items():
            if key not in output.columns:
                output[key] = _value(value)
        output, provider_id_column = preserve_provider_native_id(output)
        natural_keys = _infer_keys(output, category)
        if provider_id_column is not None:
            natural_keys = (provider_id_column,)
        return self._write(
            scope="raw",
            source=source,
            category=category,
            symbol=symbol,
            suffix=endpoint,
            timeframe=canonical,
            dataset_key=dataset_key or endpoint,
            pool=pool,
            frame=output,
            keys=natural_keys,
        )

    def save_error(
        self,
        *,
        source: str,
        category: str,
        symbol: str,
        request_key: str,
        error_type: str,
        error_message: str,
        metadata: dict[str, object] | None = None,
        pool: str | None = None,
    ) -> Path | None:
        row = {
            "symbol": symbol,
            "source": source,
            "category": category,
            "request_key": request_key,
            "fetched_at": _now(),
            "error_type": error_type,
            "error_message": error_message,
            **dict(metadata or {}),
        }
        return self._write(
            scope="errors",
            source=source,
            category=category,
            symbol=symbol,
            suffix=request_key,
            dataset_key=request_key,
            pool=pool,
            frame=_frame([row]),
            keys=("source", "category", "request_key", "error_type", "error_message"),
        )

    def _save_rows(
        self,
        source: str,
        category: str,
        symbol: str,
        request_key: str,
        rows: list[dict[str, Any]],
        metadata: dict[str, object] | None,
        *,
        pool: str | None = None,
        mode: str = "upsert",
        keys: Sequence[str] | None = None,
    ) -> Path | None:
        if not rows:
            return None
        extra = dict(metadata or {})
        output = _frame([{**row, "request_key": request_key, **extra} for row in rows])
        return self._write(
            scope="normalized",
            source=source,
            category=category,
            symbol=symbol,
            suffix=request_key,
            dataset_key=request_key,
            pool=pool,
            frame=output,
            keys=tuple(keys) if keys is not None else _infer_keys(output, category),
            mode=mode,
        )

    def _write(
        self,
        *,
        scope: str,
        source: str,
        category: str,
        symbol: str,
        frame: pd.DataFrame,
        suffix: str = "",
        timeframe: str = "",
        dataset_key: str = "",
        pool: str | None = None,
        keys: Sequence[str] = (),
        mode: str = "upsert",
        storage_columns: Sequence[str] = (),
        bar_timeframe: str = "",
        bar_as_of: datetime | pd.Timestamp | None = None,
    ) -> Path | None:
        path = self._path(scope, source, category, symbol, suffix, timeframe, dataset_key, pool)
        if scope == "raw":
            validate_raw_provider_id_columns(frame, source=source)
        else:
            frame = without_internal_identity_columns(frame)
        expected_columns = tuple(storage_columns)
        if expected_columns:
            frame = frame.reindex(columns=expected_columns)
        frame = frame.drop(columns=[ID_COLUMN], errors="ignore")
        existing, legacy, schema_mismatch = self._load(
            path,
            storage_columns=expected_columns,
            bar_timeframe=bar_timeframe,
            bar_as_of=bar_as_of,
        )
        if scope == "raw":
            validate_raw_provider_id_columns(existing, source=source)
        else:
            existing = without_internal_identity_columns(existing)
        existing = existing.drop(columns=[ID_COLUMN], errors="ignore")
        existing, frame, temporal_schema_mismatch = _normalize_temporal_columns(
            existing,
            frame,
        )
        temporal_schema_mismatch = temporal_schema_mismatch and (
            path.is_file() or bool(legacy)
        )
        merge_keys = tuple(keys)
        availability_migrated = False
        if "available_at" in merge_keys:
            existing, availability_migrated = _backfill_legacy_availability(
                existing
            )
        if scope == "raw" and not any(_provider_id_key(key) for key in merge_keys):
            merge_keys = _minimum_upsert_key(existing, frame, category) or merge_keys
        if mode == "snapshot":
            output, changed = frame.reset_index(drop=True), not _same(existing, frame)
        elif mode == "append_if_changed":
            existing, frame = _normalize_key_columns(
                existing,
                frame,
                merge_keys,
            )
            output, changed = _append_if_changed(existing, frame)
        elif mode == "append_if_revised":
            revision_keys = tuple(
                key for key in merge_keys if key != "available_at"
            )
            revised = _revised_rows(
                existing,
                frame,
                identity_keys=revision_keys,
            )
            output, changed = _upsert(existing, revised, merge_keys)
        else:
            output, changed = _upsert(existing, frame, merge_keys)
        changed = changed or availability_migrated or temporal_schema_mismatch
        if not changed and not legacy and not schema_mismatch:
            return None
        natural_keys = _infer_keys(output, category, readable_only=True)
        if not natural_keys:
            requested = tuple(key for key in keys if key in output.columns)
            natural_keys = minimum_unique_key(
                output,
                (requested,),
                reject_opaque=True,
            )
        output = add_readable_id(
            output.reset_index(drop=True),
            key_columns=natural_keys,
        )
        if expected_columns:
            output = output.reindex(columns=expected_columns)
        temporary = path.with_suffix(".tmp.parquet")
        try:
            if expected_columns == NORMALIZED_BAR_COLUMNS:
                write_normalized_bar_parquet(output.reset_index(drop=True), temporary)
            else:
                output.reset_index(drop=True).to_parquet(temporary, index=False)
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        for old in legacy:
            old.unlink(missing_ok=True)
        return path

    def _load(
        self,
        path: Path,
        *,
        storage_columns: tuple[str, ...] = (),
        bar_timeframe: str = "",
        bar_as_of: datetime | pd.Timestamp | None = None,
    ) -> tuple[pd.DataFrame, tuple[Path, ...], bool]:
        legacy = tuple(
            sorted(
                (
                    candidate
                    for candidate in path.parent.glob(f"{path.stem}_*.parquet")
                    if candidate != path
                    and normalized_bar_canonical_path(candidate) == path
                ),
                key=normalized_bar_file_sort_key,
            )
        )
        paths = list(legacy) + ([path] if path.is_file() else [])
        if not paths:
            return pd.DataFrame(columns=storage_columns or None), legacy, False

        frames: list[pd.DataFrame] = []
        schema_mismatch = False
        for existing_path in paths:
            if storage_columns:
                frame, physical_schema = read_normalized_bar_parquet(
                    existing_path,
                    include_legacy_completion=True,
                )
                schema_mismatch = (
                    schema_mismatch
                    or not normalized_bar_schema_is_canonical(physical_schema)
                )
            else:
                frame = pd.read_parquet(existing_path)
            frames.append(frame)
        combined = pd.concat(frames, ignore_index=True, sort=False)
        if storage_columns:
            stored_row_count = len(combined)
            combined = combined.drop_duplicates("timestamp", keep="last")
            combined = combined.loc[
                legacy_bar_completion_mask(combined)
            ].reset_index(drop=True)
            if bar_timeframe:
                timing = annotate_bar_timing(
                    combined,
                    timeframe=bar_timeframe,
                    as_of=bar_as_of,
                )
                combined = combined.loc[
                    timing["bar_complete"].fillna(False).astype(bool)
                ].reset_index(drop=True)
            schema_mismatch = (
                schema_mismatch or len(combined) != stored_row_count
            )
            combined = project_normalized_bar_frame(combined).reindex(
                columns=storage_columns
            )
        return combined, legacy, schema_mismatch

    def _path(
        self,
        scope: str,
        source: str,
        category: str,
        symbol: str,
        suffix: str,
        timeframe: str,
        dataset_key: str,
        pool: str | None,
    ) -> Path:
        path = self.target_path(
            scope=scope,
            source=source,
            category=category,
            symbol=symbol,
            suffix=suffix,
            timeframe=timeframe,
            dataset_key=dataset_key,
            pool=pool,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def target_path(
        self,
        *,
        scope: str,
        source: str,
        category: str,
        symbol: str,
        suffix: str = "",
        timeframe: str = "",
        dataset_key: str = "",
        pool: str | None = None,
    ) -> Path:
        """Return a deterministic persistence target without creating it."""

        if category == "macro" or pool is not None:
            folder = pool_data_folder(
                self.root_dir,
                pool=pool or DEFAULT_POOL,
                symbol=symbol,
                category=category,
                source=source,
                scope=scope,
                dataset_key=dataset_key,
            )
        else:
            folder = stock_data_folder(
                self.root_dir,
                symbol=symbol,
                category=category,
                source=source,
                scope=scope,
                dataset_key=dataset_key,
                timeframe=timeframe,
            )
        stem = safe_token(symbol.strip().upper().replace("/", "-"))
        if suffix:
            stem += f"_{safe_token(suffix)}"
        return folder / f"{stem}.parquet"


def resolve_datastore_dir(*, root_dir: Path | str | None = None, target: str | None = None) -> Path:
    if root_dir is not None:
        return Path(root_dir).expanduser()
    if target is not None:
        try:
            return DATASTORE_TARGETS[target.strip().lower()]
        except KeyError as exc:
            choices = ", ".join(sorted(DATASTORE_TARGETS))
            raise ValueError(f"Unknown datastore target {target!r}. Use one of: {choices}.") from exc
    configured = os.getenv("DUCKETS_DATASTORE_DIR", "").strip() or os.getenv(
        "DUCKETS_OHLCV_PARQUET_DIR", ""
    ).strip()
    return Path(configured).expanduser() if configured else PC_DATASTORE_DIR


def _upsert(existing: pd.DataFrame, incoming: pd.DataFrame, keys: tuple[str, ...]) -> tuple[pd.DataFrame, bool]:
    existing, incoming = _normalize_key_columns(existing, incoming, keys)
    if incoming.empty:
        return existing.reset_index(drop=True), False
    if existing.empty:
        usable = tuple(key for key in keys if key in incoming.columns)
        rows = incoming.to_dict("records")
        if usable:
            helper = incoming.copy()
            helper["__key"] = [_row_key(row, usable) or (f"missing:{i}",) for i, row in enumerate(rows)]
            output = helper.drop_duplicates("__key", keep="last").drop(columns="__key")
            return _sort(output, usable), not output.empty
        unique_rows = []
        seen: set[str] = set()
        for row in rows:
            canonical = _canonical_row(row)
            if canonical in seen:
                continue
            seen.add(canonical)
            unique_rows.append(row)
        return pd.DataFrame(unique_rows, columns=incoming.columns), bool(unique_rows)
    existing, incoming = _align(existing, incoming)
    usable = tuple(key for key in keys if key in existing.columns and key in incoming.columns)
    if not usable:
        rows = []
        known: set[str] = set()
        for row in [*existing.to_dict("records"), *incoming.to_dict("records")]:
            canonical = _canonical_row(row)
            if canonical in known:
                continue
            known.add(canonical)
            rows.append(row)
        output = pd.DataFrame(rows, columns=existing.columns)
        return output.reset_index(drop=True), len(output) != len(existing)
    rows = existing.to_dict("records")
    positions = {_row_key(row, usable): i for i, row in enumerate(rows) if _row_key(row, usable)}
    changed = False
    for row in incoming.to_dict("records"):
        key = _row_key(row, usable)
        if not key:
            if _canonical_row(row) not in {_canonical_row(item) for item in rows}:
                rows.append(row)
                changed = True
            continue
        position = positions.get(key)
        if position is None:
            positions[key] = len(rows)
            rows.append(row)
            changed = True
        elif _canonical_row(rows[position]) != _canonical_row(row):
            rows[position] = row
            changed = True
    output = pd.DataFrame(rows, columns=existing.columns)
    helper = output.copy()
    helper["__key"] = [_row_key(row, usable) or (f"missing:{i}",) for i, row in enumerate(rows)]
    output = helper.drop_duplicates("__key", keep="last").drop(columns="__key")
    changed = changed or len(output) != len(rows)
    return _sort(output, usable), changed


def _append_if_changed(existing: pd.DataFrame, incoming: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    if existing.empty:
        return incoming.reset_index(drop=True), True
    existing, incoming = _align(existing, incoming)
    compacted_rows = []
    seen: set[str] = set()
    for row in existing.to_dict("records"):
        canonical = _canonical_row(row)
        if canonical in seen:
            continue
        seen.add(canonical)
        compacted_rows.append(row)
    compacted = pd.DataFrame(compacted_rows, columns=existing.columns)
    changed = len(compacted) != len(existing)
    if _canonical_row(compacted.iloc[-1].to_dict()) == _canonical_row(
        incoming.iloc[-1].to_dict()
    ):
        return compacted.reset_index(drop=True), changed
    return pd.concat([compacted, incoming.tail(1)], ignore_index=True), True


def _same(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    if left.empty and right.empty:
        return True
    left, right = _align(left.reset_index(drop=True), right.reset_index(drop=True))
    return len(left) == len(right) and [
        _canonical_row(row) for row in left.to_dict("records")
    ] == [_canonical_row(row) for row in right.to_dict("records")]


def _infer_keys(
    frame: pd.DataFrame,
    category: str,
    *,
    readable_only: bool = False,
) -> tuple[str, ...]:
    return minimum_unique_key(
        frame,
        _key_candidates(frame, category),
        reject_opaque=readable_only,
    )


def _key_candidates(
    frame: pd.DataFrame,
    category: str,
) -> tuple[tuple[str, ...], ...]:
    temporal = (
        "timestamp",
        "ts_event",
        "ts_recv",
        "datetime",
        "time",
        "fetched_at",
    )
    provider_ids = tuple(
        str(column)
        for column in frame.columns
        if str(column) != ID_COLUMN
        and (
            _provider_id_key(str(column))
            or str(column) in {"sequence", "event_sequence"}
        )
    )
    candidates: list[tuple[str, ...]] = []
    if all(column in frame.columns for column in DATABENTO_MBP_NATURAL_KEY):
        candidates.append(DATABENTO_MBP_NATURAL_KEY)
    if all(column in frame.columns for column in DATABENTO_BBO_NATURAL_KEY):
        candidates.append(DATABENTO_BBO_NATURAL_KEY)
    candidates.extend((column,) for column in temporal)
    candidates.extend(("symbol", column) for column in temporal)
    candidates.extend((column, "contract_symbol") for column in temporal)
    candidates.extend((column, "horizon") for column in temporal)
    candidates.extend(
        (
            ("accession_number",),
            ("accessionNumber",),
            ("document_url",),
            ("url",),
            ("date",),
            ("period_end_date",),
            ("date", "period"),
            ("date", "period", "calendar_year"),
            ("date", "period", "calendarYear"),
            ("period_end_date", "fiscal_period"),
            ("filing_date", "form_type"),
            ("filingDate", "type"),
        )
    )
    candidates.extend((column,) for column in provider_ids)
    candidates.extend(
        (temporal_column, provider_column)
        for temporal_column in temporal
        for provider_column in provider_ids
    )
    candidates.append(("symbol",))
    if category == "bars":
        candidates = [
            candidate
            for candidate in candidates
            if any(column in temporal for column in candidate)
        ]
    return tuple(dict.fromkeys(candidates))


def _minimum_upsert_key(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    category: str,
) -> tuple[str, ...]:
    existing, incoming = _normalize_key_columns(
        existing,
        incoming,
        tuple(TEMPORAL_KEY_COLUMNS),
    )
    candidates = _key_candidates(
        pd.concat([existing, incoming], ignore_index=True, sort=False),
        category,
    )
    candidate_columns = list(
        dict.fromkeys(
            column
            for candidate in candidates
            for column in candidate
            if column in existing.columns or column in incoming.columns
        )
    )
    combined = pd.concat([existing, incoming], ignore_index=True, sort=False)
    signatures = combined.reindex(columns=candidate_columns).drop_duplicates()
    for candidate in candidates:
        if existing.empty:
            existing_key = candidate
        else:
            existing_key = minimum_unique_key(existing, (candidate,))
        incoming_key = minimum_unique_key(incoming, (candidate,))
        signature_key = minimum_unique_key(signatures, (candidate,))
        if existing_key and incoming_key and signature_key:
            return candidate
    return ()


def _provider_id_key(column: str) -> bool:
    normalized = column.strip().lower()
    return normalized.startswith("provider_") and (
        normalized.endswith("_id")
        or "_native_id" in normalized
        or "native_identifier" in normalized
    ) or normalized.endswith(("_id", "_ids"))


def _sort(frame: pd.DataFrame, keys: Sequence[str]) -> pd.DataFrame:
    columns = [key for key in keys if key in frame.columns]
    if not columns:
        return frame.reset_index(drop=True)
    try:
        return frame.sort_values(columns, kind="stable").reset_index(drop=True)
    except (TypeError, ValueError):
        return frame.reset_index(drop=True)


def _align(left: pd.DataFrame, right: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    columns = list(dict.fromkeys([*left.columns, *right.columns]))
    return left.reindex(columns=columns), right.reindex(columns=columns)


def _normalize_key_columns(
    left: pd.DataFrame,
    right: pd.DataFrame,
    keys: Sequence[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Canonicalize temporal upsert keys before rows are compared."""
    normalized_left = left.copy()
    normalized_right = right.copy()
    for key in keys:
        if not _is_temporal_column(key):
            continue
        if key in normalized_left.columns:
            normalized_left[key] = _normalize_temporal_series(
                normalized_left[key],
                column=key,
                frame_name="left",
            )
        if key in normalized_right.columns:
            normalized_right[key] = _normalize_temporal_series(
                normalized_right[key],
                column=key,
                frame_name="right",
            )
    return normalized_left, normalized_right


def _normalize_temporal_columns(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, bool]:
    """Canonicalize every known temporal column at the persistence boundary.

    Upsert keys are an identity concern, not a storage-schema contract. A
    temporal value therefore receives the same UTC nanosecond representation
    whether or not key inference selected that column for the current batch.
    """

    normalized_existing = existing.copy()
    normalized_incoming = incoming.copy()
    columns = tuple(
        dict.fromkeys([*normalized_existing.columns, *normalized_incoming.columns])
    )
    schema_mismatch = False
    for column in columns:
        if not _is_temporal_column(str(column)):
            continue
        if column in normalized_existing.columns:
            values = normalized_existing[column]
            schema_mismatch = schema_mismatch or not _canonical_temporal_dtype(
                values.dtype
            )
            normalized_existing[column] = _normalize_temporal_series(
                values,
                column=str(column),
                frame_name="stored",
            )
        if column in normalized_incoming.columns:
            normalized_incoming[column] = _normalize_temporal_series(
                normalized_incoming[column],
                column=str(column),
                frame_name="incoming",
            )
    return normalized_existing, normalized_incoming, schema_mismatch


def _is_temporal_column(column: str) -> bool:
    normalized = str(column).strip().lower()
    return (
        normalized in TEMPORAL_KEY_COLUMNS
        or normalized in TEMPORAL_METADATA_COLUMNS
        or normalized.endswith("_timestamp")
        or normalized.endswith("_at")
    )


def _canonical_temporal_dtype(dtype: object) -> bool:
    return (
        isinstance(dtype, pd.DatetimeTZDtype)
        and str(dtype.tz).upper() == "UTC"
        and getattr(dtype, "unit", "ns") == "ns"
    )


def _normalize_temporal_series(
    values: pd.Series,
    *,
    column: str,
    frame_name: str,
) -> pd.Series:
    normalized: list[object] = []
    invalid: list[tuple[object, object]] = []
    nullable_blank = column.strip().lower() in NULLABLE_BLANK_TEMPORAL_COLUMNS
    for index, value in values.items():
        if _missing_temporal_value(value) or (
            nullable_blank and isinstance(value, str) and not value.strip()
        ):
            normalized.append(pd.NaT)
            continue
        parsed = _normalize_temporal_value(value)
        if pd.isna(parsed):
            invalid.append((index, value))
            normalized.append(pd.NaT)
        else:
            normalized.append(parsed)
    if invalid:
        samples = ", ".join(
            f"row {index!r}={value!r}" for index, value in invalid[:5]
        )
        raise ValueError(
            f"{frame_name} temporal column {column!r} contains "
            f"{len(invalid)} invalid non-null value(s): {samples}"
        )
    return pd.Series(
        normalized,
        index=values.index,
        dtype="datetime64[ns, UTC]",
    )


def _missing_temporal_value(value: object) -> bool:
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _normalize_temporal_value(value: object) -> pd.Timestamp | pd.NaT:
    epoch = _likely_epoch_timestamp(value)
    if epoch is not None:
        return epoch

    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return pd.NaT
    timestamp = pd.Timestamp(parsed)
    # The old generic conversion treated Unix seconds as nanoseconds, leaving
    # contemporary provider timestamps one to four seconds after the epoch.
    # Its nanosecond value is the original Unix-seconds value, so the inverse
    # is exact and deliberately bounded to plausible 2001-2096 source dates.
    if (
        timestamp.year == 1970
        and 1_000_000_000 <= timestamp.value <= 4_000_000_000
    ):
        return pd.Timestamp(
            pd.to_datetime(timestamp.value, unit="s", utc=True)
        )
    return timestamp


def _likely_epoch_timestamp(value: object) -> pd.Timestamp | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Real):
        number: int | float = value
    elif isinstance(value, str):
        text = value.strip()
        try:
            number = int(text)
        except ValueError:
            try:
                number = float(text)
            except ValueError:
                return None
    else:
        return None

    try:
        magnitude = abs(float(number))
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(magnitude):
        return None
    for unit, scale in (
        ("s", 1),
        ("ms", 1_000),
        ("us", 1_000_000),
        ("ns", 1_000_000_000),
    ):
        if 1_000_000_000 * scale <= magnitude <= 4_000_000_000 * scale:
            parsed = pd.to_datetime(number, unit=unit, utc=True, errors="coerce")
            return None if pd.isna(parsed) else pd.Timestamp(parsed)
    return None


def _backfill_legacy_availability(
    frame: pd.DataFrame,
) -> tuple[pd.DataFrame, bool]:
    """Give pre-availability rows their conservative local receipt clock."""

    if frame.empty or "fetched_at" not in frame.columns:
        return frame, False
    output = frame.copy()
    receipt = pd.to_datetime(output["fetched_at"], utc=True, errors="coerce")
    had_availability = "available_at" in output.columns
    if "available_at" in output.columns:
        availability = pd.to_datetime(
            output["available_at"], utc=True, errors="coerce"
        )
    else:
        availability = pd.Series(
            pd.NaT,
            index=output.index,
            dtype="datetime64[ns, UTC]",
        )
    output["available_at"] = availability.fillna(receipt)
    migrated = not had_availability or not output["available_at"].equals(
        availability
    )
    return output, migrated


def _revised_rows(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    *,
    identity_keys: Sequence[str],
) -> pd.DataFrame:
    """Keep only new identities or content revisions of existing identities."""

    keys = tuple(
        key
        for key in identity_keys
        if key in existing.columns and key in incoming.columns
    )
    if existing.empty or incoming.empty or not keys:
        return incoming.reset_index(drop=True)

    latest_content: dict[tuple[str, ...], str] = {}
    ordered_existing = existing.copy()
    if "available_at" in ordered_existing.columns:
        ordered_existing["__revision_clock"] = pd.to_datetime(
            ordered_existing["available_at"],
            utc=True,
            errors="coerce",
        )
        ordered_existing = ordered_existing.sort_values(
            "__revision_clock",
            kind="stable",
            na_position="first",
        ).drop(columns="__revision_clock")
    for row in ordered_existing.to_dict("records"):
        identity = _row_key(row, keys)
        if identity:
            latest_content[identity] = _revision_content(row)

    revised: list[dict[str, Any]] = []
    for row in incoming.to_dict("records"):
        identity = _row_key(row, keys)
        content = _revision_content(row)
        if identity and latest_content.get(identity) == content:
            continue
        revised.append(row)
        if identity:
            latest_content[identity] = content
    return pd.DataFrame(revised, columns=incoming.columns)


def _revision_content(row: Mapping[str, Any]) -> str:
    stable = {
        str(key): _canonical_value(value)
        for key, value in sorted(row.items(), key=lambda item: str(item[0]))
        if str(key) not in VOLATILE_COLUMNS and str(key) != "available_at"
    }
    return json.dumps(stable, default=str, sort_keys=True, separators=(",", ":"))


def _row_key(row: Mapping[str, Any], keys: Sequence[str]) -> tuple[str, ...] | None:
    values = tuple(_key_value(row.get(key)) for key in keys)
    return None if any(not value for value in values) else values


def _key_value(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _canonical_row(row: Mapping[str, Any]) -> str:
    stable = {
        str(key): _canonical_value(value)
        for key, value in sorted(row.items(), key=lambda item: str(item[0]))
        if str(key) not in VOLATILE_COLUMNS
    }
    return json.dumps(stable, default=str, sort_keys=True, separators=(",", ":"))


def _canonical_value(value: Any) -> Any:
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _frame(rows: list[Mapping[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame([{str(key): _value(value) for key, value in row.items()} for row in rows])


def _object_row(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict):
        return dict(attributes)
    raise TypeError(f"Cannot convert {type(value).__name__} to a Parquet row.")


def _value(value: Any) -> Any:
    if isinstance(value, datetime):
        normalized = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return normalized.isoformat()
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, default=str, sort_keys=True)
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
