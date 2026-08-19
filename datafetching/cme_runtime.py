from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pandas as pd
from dotenv import load_dotenv

from app.services.databento_cme_context import (
    DatabentoCmeContextProvider,
    DatabentoCmeContextSpec,
)
from app.services.databento_retry import call_with_persistent_databento_retry
from datafetching.cme_cross_asset_context import (
    CmeCrossAssetNotReady,
    CmeCrossAssetQualityError,
    cme_archive_replay_pending,
    materialize_cme_cross_asset_context,
)
from datafetching.databento_archive import cme_archive_cursor_for_spec
from datafetching.cme_history import (
    CmeCursor,
    cme_writer_lock_path,
    five_minute_boundary,
    persist_cme_event_history,
    publish_cme_cursor,
    publish_cme_l2_snapshot,
    read_cme_cursor,
)
from datafetching.observability import timed_stage
from datafetching.parquet_store import DATASTORE_TARGETS, ParquetStore
from datafetching.runtime_lock import exclusive_runtime_lock


DEFAULT_CADENCE_SECONDS = {
    "ohlcv-1m": 60,
    "bbo-1m": 15,
    "mbp-10": 5,
}
DEFAULT_PHASE_OFFSETS_SECONDS = {
    "ohlcv-1m": 1,
    "bbo-1m": 2,
    "mbp-10": 0,
}
DEFAULT_OVERLAP_SECONDS = {
    "ohlcv-1m": 120,
    "bbo-1m": 15,
    "mbp-10": 2,
}
DEFAULT_CHUNK_MINUTES = {
    "ohlcv-1m": 24 * 60,
    "bbo-1m": 60,
    "mbp-10": 5,
}
DEFAULT_RECORD_LIMITS = {
    # MBP-10 records are 368 uncompressed bytes. This keeps one streaming
    # response below 100 MB and saturated ranges are split before publication.
    "mbp-10": 250_000,
}
CME_RECOVERY_CHUNK_BUDGET = 1
CME_OPERATIONAL_MBP_WINDOW_SECONDS = 5
CME_OPERATIONAL_BBO_WINDOW_SECONDS = 5 * 60
REPOSITORY_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_repository_environment(env_file: Path | None = None) -> bool:
    """Load CLI credentials from the repository .env without overriding the shell."""

    return load_dotenv(
        dotenv_path=env_file or REPOSITORY_ENV_FILE,
        override=False,
    )


@dataclass(frozen=True)
class CmeSchemaResult:
    group_key: str
    schema: str
    queried_from: pd.Timestamp
    queried_through: pd.Timestamp
    rows: int
    partitions_written: int
    partitions_reused: int
    request_count: int
    cursor: CmeCursor


@dataclass(frozen=True)
class CmeCycleResult:
    schemas_succeeded: int
    schemas_failed: int
    event_rows: int
    partitions_written: int
    partitions_reused: int
    l2_snapshot_rows: int
    hourly_context_written: bool


def run_cme_cycle(
    store: ParquetStore,
    *,
    provider: DatabentoCmeContextProvider | None = None,
    schemas: Sequence[str] | None = None,
    max_concurrency: int = 1,
    overlap_seconds: Mapping[str, int] | None = None,
    chunk_minutes: Mapping[str, int] | None = None,
    record_limits: Mapping[str, int] | None = None,
    retry_attempts: int = 6,
    retry_delay_seconds: float = 4.0,
    now: Callable[[], datetime] | None = None,
    reporter: Callable[[str], None] | None = print,
) -> CmeCycleResult:
    """Collect complete CME ranges and publish derived artifacts independently."""

    if max_concurrency not in {1, 2}:
        raise ValueError("CME max_concurrency must be one or two")
    if retry_attempts < 1:
        raise ValueError("CME retry_attempts must be positive")
    provider = provider or DatabentoCmeContextProvider()
    clock = now or (lambda: datetime.now(timezone.utc))
    requested_schemas = set(schemas or ())
    with timed_stage(
        "cme.discover-endpoints",
        provider="databento",
        reporter=reporter,
    ) as timing:
        specs = tuple(
            spec
            for spec in provider.specs()
            if not requested_schemas or spec.schema in requested_schemas
        )
        timing.annotate(row_count=len(specs), operation="fetched")
    context_specs = tuple(value for value in specs if value.group_key == "context")
    archive_dataset = next(
        (value.dataset for value in context_specs),
        "",
    )
    archive_symbols = tuple(
        dict.fromkeys(
            symbol
            for value in context_specs
            for symbol in value.symbols
        )
    )
    overlaps = {**DEFAULT_OVERLAP_SECONDS, **dict(overlap_seconds or {})}
    chunks = {**DEFAULT_CHUNK_MINUTES, **dict(chunk_minutes or {})}
    limits = {**DEFAULT_RECORD_LIMITS, **dict(record_limits or {})}
    results: list[CmeSchemaResult] = []
    failures: list[tuple[DatabentoCmeContextSpec, Exception]] = []
    l2_rows = 0

    # A deep complete-history gap must not block the current operational L2
    # authority. Fetch one exact five-second lane for every L2 stream ending at
    # the latest completed common boundary, publish it only when every expected
    # stream is fresh, and then checkpoint at most one older recovery chunk per
    # schema in this cycle.
    recovery_ranges: dict[
        str, tuple[tuple[pd.Timestamp, pd.Timestamp], ...]
    ] = {}
    for spec in specs:
        if spec.schema != "mbp-10":
            continue
        cursor = read_cme_cursor(
            store.root_dir,
            group_key=spec.group_key,
            schema=spec.schema,
        )
        if cursor is None:
            continue
        ranges = query_chunks(
            spec,
            cursor=cursor,
            overlap=timedelta(seconds=overlaps.get(spec.schema, 30)),
            maximum_chunk=timedelta(minutes=chunks.get(spec.schema, 60)),
        )
        if len(ranges) > CME_RECOVERY_CHUNK_BUDGET:
            recovery_ranges[spec.key] = ranges

    operational_l2_specs = tuple(
        spec
        for spec in specs
        if spec.schema.startswith(("bbo-", "mbp-", "mbo"))
    )
    operational_expected_symbols = {
        (spec.group_key, spec.schema): spec.symbols
        for spec in operational_l2_specs
    }
    operational_target = (
        min(
            five_minute_boundary(_utc(spec.end))
            for spec in specs
            if spec.key in recovery_ranges
        )
        if recovery_ranges
        else None
    )
    latest_completed: list[DatabentoCmeContextSpec] = []
    for spec in operational_l2_specs if operational_target is not None else ():
        latest_end = operational_target
        window_seconds = (
            CME_OPERATIONAL_BBO_WINDOW_SECONDS
            if spec.schema.startswith("bbo-")
            else CME_OPERATIONAL_MBP_WINDOW_SECONDS
        )
        latest_start = latest_end - pd.Timedelta(
            seconds=window_seconds
        )
        latest_spec = replace(
            spec,
            start=latest_start.to_pydatetime(),
            end=latest_end.to_pydatetime(),
        )
        try:
            _collect_schema(
                store,
                provider=provider,
                spec=latest_spec,
                overlap=timedelta(0),
                chunk=timedelta(seconds=window_seconds),
                record_limit=limits.get(spec.schema),
                retry_attempts=retry_attempts,
                retry_delay_seconds=retry_delay_seconds,
                now=clock,
                reporter=reporter,
                ranges_override=((latest_start, latest_end),),
                publish_cursor_result=False,
                stage_name="cme.collect-latest-range",
            )
            latest_completed.append(spec)
        except Exception as exc:
            failures.append((spec, exc))

    if operational_target is not None and len(latest_completed) == len(
        operational_l2_specs
    ):
        publication_at = clock().astimezone(timezone.utc)
        try:
            with timed_stage(
                "cme.publish-operational-l2-snapshot",
                provider="databento",
                schema="l2-5m",
                request_end=operational_target,
                reporter=reporter,
            ) as timing:
                snapshot = publish_cme_l2_snapshot(
                    store.root_dir,
                    snapshot_for=operational_target,
                    available_not_after=publication_at,
                    require_all_fresh=True,
                    expected_stream_symbols=operational_expected_symbols,
                )
                if snapshot is not None:
                    l2_rows = snapshot.rows
                    timing.annotate(
                        row_count=snapshot.rows,
                        operation="reused" if snapshot.reused else "wrote",
                        snapshot_for=snapshot.snapshot_for.isoformat(),
                    )
                else:
                    timing.annotate(operation="skipped")
        except Exception as exc:
            _record_derived_failure(store, "operational-l2-5m", exc)

    hourly_written = False
    try:
        replay_pending = cme_archive_replay_pending(
            store.root_dir,
            archive_dataset=archive_dataset,
            archive_symbols=archive_symbols,
        )
        if replay_pending:
            with timed_stage(
                "cme.replay-archive-context",
                provider="databento",
                schema="cme-cross-asset-v1",
                reporter=reporter,
            ) as timing:
                output = materialize_cme_cross_asset_context(
                    store.root_dir,
                    calculated_at=clock().astimezone(timezone.utc),
                    archive_dataset=archive_dataset,
                    archive_symbols=archive_symbols,
                )
                hourly_written = output is not None
                timing.annotate(operation="wrote" if output else "skipped")
    except (CmeCrossAssetNotReady, CmeCrossAssetQualityError) as exc:
        if reporter is not None:
            reporter(f"[CME/archive-context] {type(exc).__name__}: {exc}")
    except Exception as exc:
        _record_derived_failure(store, "archive-cross-asset-context", exc)
    def collect(spec: DatabentoCmeContextSpec) -> CmeSchemaResult:
        return _collect_schema(
            store,
            provider=provider,
            spec=spec,
            overlap=timedelta(seconds=overlaps.get(spec.schema, 30)),
            chunk=timedelta(minutes=chunks.get(spec.schema, 60)),
            record_limit=limits.get(spec.schema),
            retry_attempts=retry_attempts,
            retry_delay_seconds=retry_delay_seconds,
            now=clock,
            reporter=reporter,
            maximum_chunks=(
                CME_RECOVERY_CHUNK_BUDGET
                if spec.key in recovery_ranges
                else None
            ),
        )

    if max_concurrency == 1:
        for spec in specs:
            try:
                results.append(collect(spec))
            except Exception as exc:
                failures.append((spec, exc))
    else:
        with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
            future_specs = {executor.submit(collect, spec): spec for spec in specs}
            for future in as_completed(future_specs):
                spec = future_specs[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    failures.append((spec, exc))

    for spec, exc in failures:
        _record_failure(store, spec, exc)

    completed_at = clock().astimezone(timezone.utc)
    try:
        with timed_stage(
            "cme.derive-hourly-context",
            provider="databento",
            schema="cme-cross-asset-v1",
            reporter=reporter,
        ) as timing:
            output = materialize_cme_cross_asset_context(
                store.root_dir,
                calculated_at=completed_at,
                archive_dataset=archive_dataset,
                archive_symbols=archive_symbols,
            )
            hourly_written = hourly_written or output is not None
            timing.annotate(operation="wrote" if output else "skipped")
    except (CmeCrossAssetNotReady, CmeCrossAssetQualityError) as exc:
        if reporter is not None:
            reporter(f"[CME/hourly-context] {type(exc).__name__}: {exc}")
    except Exception as exc:
        _record_derived_failure(store, "cross-asset-context", exc)

    mbp_specs = tuple(spec for spec in specs if spec.schema == "mbp-10")
    try:
        if not mbp_specs:
            raise CmeCrossAssetNotReady(
                "No MBP-10 schema is due; L2 publication cannot advance."
            )
        l2_target = min(_utc(spec.end) for spec in mbp_specs)
        with timed_stage(
            "cme.publish-l2-snapshot",
            provider="databento",
            schema="l2-5m",
            request_end=l2_target,
            reporter=reporter,
        ) as timing:
            snapshot = publish_cme_l2_snapshot(
                store.root_dir,
                snapshot_for=l2_target,
                available_not_after=completed_at,
                require_all_fresh=True,
                expected_stream_symbols=operational_expected_symbols,
            )
            if snapshot is not None:
                l2_rows = snapshot.rows
                timing.annotate(
                    row_count=snapshot.rows,
                    operation="reused" if snapshot.reused else "wrote",
                    snapshot_for=snapshot.snapshot_for.isoformat(),
                )
            else:
                timing.annotate(operation="skipped")
    except CmeCrossAssetNotReady:
        pass
    except Exception as exc:
        _record_derived_failure(store, "l2-5m", exc)

    return CmeCycleResult(
        schemas_succeeded=len(results),
        schemas_failed=len(failures),
        event_rows=sum(result.rows for result in results),
        partitions_written=sum(result.partitions_written for result in results),
        partitions_reused=sum(result.partitions_reused for result in results),
        l2_snapshot_rows=l2_rows,
        hourly_context_written=hourly_written,
    )


def query_chunks(
    spec: DatabentoCmeContextSpec,
    *,
    cursor: CmeCursor | None,
    overlap: timedelta,
    maximum_chunk: timedelta,
) -> tuple[tuple[pd.Timestamp, pd.Timestamp], ...]:
    """Continue from the last successful endpoint, including quiet periods."""

    if overlap < timedelta(0):
        raise ValueError("CME safety overlap cannot be negative")
    if maximum_chunk <= timedelta(0):
        raise ValueError("CME maximum chunk must be positive")
    endpoint = _utc(spec.end)
    initial = _utc(spec.start)
    start = (
        min(endpoint, cursor.queried_through) - overlap
        if cursor is not None
        else initial
    )
    start = min(start, endpoint)
    if start == endpoint:
        start = endpoint - overlap
    chunks: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    current = start
    while current < endpoint:
        end = min(current + maximum_chunk, endpoint)
        chunks.append((current, end))
        current = end
    return tuple(chunks)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the independent complete-history CME/L2 collector."
    )
    datastore = parser.add_mutually_exclusive_group()
    datastore.add_argument("--datastore", type=Path, default=None)
    datastore.add_argument(
        "--datastore-target",
        choices=tuple(DATASTORE_TARGETS),
        default=None,
    )
    parser.add_argument(
        "--cadence",
        action="append",
        default=[],
        metavar="SCHEMA=SECONDS",
        help="Per-schema cadence; repeat for multiple schemas.",
    )
    parser.add_argument(
        "--phase-offset",
        action="append",
        default=[],
        metavar="SCHEMA=SECONDS",
    )
    parser.add_argument(
        "--overlap-seconds",
        action="append",
        default=[],
        metavar="SCHEMA=SECONDS",
    )
    parser.add_argument(
        "--chunk-minutes",
        action="append",
        default=[],
        metavar="SCHEMA=MINUTES",
    )
    parser.add_argument(
        "--record-limit",
        action="append",
        default=[],
        metavar="SCHEMA=ROWS",
        help=(
            "Maximum records per exact request; saturated ranges are split and "
            "retried without advancing the cursor."
        ),
    )
    parser.add_argument("--max-concurrency", type=int, choices=(1, 2), default=1)
    parser.add_argument("--retry-attempts", type=int, default=6)
    parser.add_argument("--retry-delay-seconds", type=float, default=4.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    load_repository_environment()
    cadences = {**DEFAULT_CADENCE_SECONDS, **_parse_schema_values(args.cadence)}
    phases = {
        **DEFAULT_PHASE_OFFSETS_SECONDS,
        **_parse_schema_values(args.phase_offset),
    }
    overlaps = {
        **DEFAULT_OVERLAP_SECONDS,
        **_parse_schema_values(args.overlap_seconds),
    }
    chunk_values = {
        **DEFAULT_CHUNK_MINUTES,
        **_parse_schema_values(args.chunk_minutes),
    }
    record_limits = {
        **DEFAULT_RECORD_LIMITS,
        **_parse_schema_values(args.record_limit),
    }
    if any(value < 1 for value in cadences.values()):
        parser.error("Every CME cadence must be at least one second")
    if any(value < 0 for value in phases.values()):
        parser.error("CME phase offsets cannot be negative")
    if any(value < 1 for value in record_limits.values()):
        parser.error("Every CME record limit must be at least one row")
    if args.retry_attempts < 1 or args.retry_delay_seconds < 0:
        parser.error("Retry attempts must be positive and delay non-negative")
    store = ParquetStore(args.datastore, target=args.datastore_target)
    schedule = " | ".join(
        f"{schema} every {cadence}s at +{phases.get(schema, 0)}s"
        for schema, cadence in cadences.items()
    )
    limits_text = ", ".join(
        f"{schema}={limit:,}" for schema, limit in record_limits.items()
    )
    print("DUCKETS CME")
    print("===========")
    print(f"Datastore    {store.root_dir}")
    print("Owns         OHLCV, BBO, MBP-10, 5-minute L2, hourly context")
    print("Collection   complete history from successful queried_through cursors")
    print(f"Schedule     {schedule}")
    print(f"Requests     max {args.max_concurrency} concurrent; record caps {limits_text}")
    print("Stop         Ctrl+C")
    print()

    with exclusive_runtime_lock(
        cme_writer_lock_path(store.root_dir),
        process_name="Duckets CME runtime",
    ):
        try:
            if args.once:
                result = run_cme_cycle(
                    store,
                    max_concurrency=args.max_concurrency,
                    overlap_seconds=overlaps,
                    chunk_minutes=chunk_values,
                    record_limits=record_limits,
                    retry_attempts=args.retry_attempts,
                    retry_delay_seconds=args.retry_delay_seconds,
                )
                _print_result(result)
                return 1 if result.schemas_failed else 0

            next_due = {
                schema: _next_schema_due(
                    datetime.now(timezone.utc),
                    cadence_seconds=cadence,
                    phase_seconds=phases.get(schema, 0),
                    allow_now=True,
                )
                for schema, cadence in cadences.items()
            }
            while True:
                current = datetime.now(timezone.utc)
                due = tuple(
                    schema for schema, boundary in next_due.items()
                    if boundary <= current
                )
                if not due:
                    wait_until = min(next_due.values())
                    time.sleep(min(1.0, max(0.0, (wait_until - current).total_seconds())))
                    continue
                result = run_cme_cycle(
                    store,
                    schemas=due,
                    max_concurrency=args.max_concurrency,
                    overlap_seconds=overlaps,
                    chunk_minutes=chunk_values,
                    record_limits=record_limits,
                    retry_attempts=args.retry_attempts,
                    retry_delay_seconds=args.retry_delay_seconds,
                )
                _print_result(result)
                completed = datetime.now(timezone.utc)
                for schema in due:
                    next_due[schema] = _next_schema_due(
                        completed,
                        cadence_seconds=cadences[schema],
                        phase_seconds=phases.get(schema, 0),
                    )
        except KeyboardInterrupt:
            print("CME runtime stopped.")
            return 0


def _collect_schema(
    store: ParquetStore,
    *,
    provider: DatabentoCmeContextProvider,
    spec: DatabentoCmeContextSpec,
    overlap: timedelta,
    chunk: timedelta,
    record_limit: int | None,
    retry_attempts: int,
    retry_delay_seconds: float,
    now: Callable[[], datetime],
    reporter: Callable[[str], None] | None,
    ranges_override: Sequence[tuple[pd.Timestamp, pd.Timestamp]] | None = None,
    maximum_chunks: int | None = None,
    publish_cursor_result: bool = True,
    stage_name: str = "cme.collect-range",
) -> CmeSchemaResult:
    cursor = read_cme_cursor(
        store.root_dir,
        group_key=spec.group_key,
        schema=spec.schema,
    )
    if cursor is None:
        cursor = cme_archive_cursor_for_spec(store.root_dir, spec)
        if cursor is not None and reporter is not None:
            reporter(
                f"[CME/archive-bridge] continuing {spec.group_key}/{spec.schema} "
                f"from verified cold history through "
                f"{cursor.queried_through.isoformat()}"
            )
    ranges = (
        tuple(ranges_override)
        if ranges_override is not None
        else query_chunks(
            spec,
            cursor=cursor,
            overlap=overlap,
            maximum_chunk=chunk,
        )
    )
    if maximum_chunks is not None:
        if maximum_chunks < 1:
            raise ValueError("CME maximum chunks per cycle must be positive")
        ranges = ranges[:maximum_chunks]
    total_rows = 0
    partitions_written = 0
    partitions_reused = 0
    request_count = 0
    latest_event: pd.Timestamp | None = cursor.last_event_at if cursor else None
    for start, end in ranges:
        initial_request = replace(
            spec,
            start=start.to_pydatetime(),
            end=end.to_pydatetime(),
            limit=record_limit,
            initial_start=start.to_pydatetime(),
            initial_end=end.to_pydatetime(),
            limit_saturated=False,
            latest_window_shrink_count=0,
            empty_window_expansion_count=0,
        )
        pending = [initial_request]
        while pending:
            request_spec = pending.pop(0)
            request_start = pd.Timestamp(request_spec.start)
            request_end = pd.Timestamp(request_spec.end)
            with timed_stage(
                stage_name,
                symbol=spec.output_symbol,
                provider="databento",
                schema=spec.schema,
                request_start=request_start,
                request_end=request_end,
                reporter=reporter,
                extra={
                    "group_key": spec.group_key,
                    "symbol_count": len(request_spec.symbols),
                },
            ) as timing:
                fetch_exact = getattr(
                    provider,
                    "fetch_cme_context_exact",
                    provider.fetch_cme_context,
                )
                rows, raw_frame, effective = call_with_persistent_databento_retry(
                    lambda request_spec=request_spec: fetch_exact(request_spec),
                    operation_name=f"CME {spec.group_key}/{spec.schema}",
                    delay_seconds=retry_delay_seconds,
                    max_attempts=retry_attempts,
                    reporter=reporter,
                    symbol=spec.output_symbol,
                    schema=spec.schema,
                    request_start=request_start,
                    request_end=request_end,
                    timing_reporter=reporter,
                )
                request_count += 1
                if effective.limit_saturated:
                    children = _split_saturated_request(request_spec)
                    pending[0:0] = list(children)
                    timing.annotate(
                        row_count=len(raw_frame),
                        operation="fetched",
                        limit_saturated=True,
                        next_action="split",
                    )
                    if reporter is not None:
                        reporter(
                            f"[CME] {spec.group_key}/{spec.schema} reached its "
                            f"{request_spec.limit:,}-row cap; splitting into "
                            f"{len(children)} exact requests."
                        )
                    continue
                persisted = persist_cme_event_history(
                    store.root_dir,
                    spec=effective,
                    normalized_rows=rows,
                    raw_frame=raw_frame,
                )
                event = _latest_event(rows)
                if event is not None and (
                    latest_event is None or event > latest_event
                ):
                    latest_event = event
                total_rows += persisted.rows
                partitions_written += persisted.written
                partitions_reused += persisted.reused
                timing.annotate(
                    row_count=persisted.rows,
                    operation=(
                        "wrote"
                        if persisted.written
                        else "reused"
                        if persisted.reused
                        else "fetched"
                    ),
                    partitions_written=persisted.written,
                    partitions_reused=persisted.reused,
                    limit_saturated=False,
                )
    queried_through = ranges[-1][1] if ranges else _utc(spec.end)
    if publish_cursor_result:
        successful_at = now().astimezone(timezone.utc)
        published_cursor = publish_cme_cursor(
            store.root_dir,
            spec=spec,
            queried_through=queried_through,
            successful_at=successful_at,
            last_event_at=latest_event,
            row_count=total_rows,
        )
    else:
        if cursor is None:
            raise RuntimeError(
                "A cursorless CME latest lane cannot suppress cursor publication"
            )
        published_cursor = cursor
    queried_from = ranges[0][0] if ranges else _utc(spec.end)
    return CmeSchemaResult(
        spec.group_key,
        spec.schema,
        queried_from,
        queried_through,
        total_rows,
        partitions_written,
        partitions_reused,
        request_count,
        published_cursor,
    )


def _split_saturated_request(
    spec: DatabentoCmeContextSpec,
) -> tuple[DatabentoCmeContextSpec, DatabentoCmeContextSpec]:
    """Split a capped exact request without creating a history gap."""

    if len(spec.symbols) > 1:
        midpoint = len(spec.symbols) // 2
        return (
            replace(spec, symbols=spec.symbols[:midpoint], limit_saturated=False),
            replace(spec, symbols=spec.symbols[midpoint:], limit_saturated=False),
        )
    midpoint = spec.start + (spec.end - spec.start) / 2
    if midpoint <= spec.start or midpoint >= spec.end:
        raise RuntimeError(
            "A single-symbol CME request remained saturated at the minimum "
            f"time resolution for {spec.group_key}/{spec.schema}; the cursor "
            "was not advanced. Increase neither the cursor nor the record cap "
            "until the range can be collected completely."
        )
    return (
        replace(spec, end=midpoint, limit_saturated=False),
        replace(spec, start=midpoint, limit_saturated=False),
    )


def _latest_event(rows: Sequence[Mapping[str, object]]) -> pd.Timestamp | None:
    values: list[pd.Timestamp] = []
    for row in rows:
        if str(row.get("cme_row_kind") or "") == "schema_status":
            continue
        for column in ("timestamp", "ts_event", "databento_ts_event"):
            timestamp = pd.to_datetime(row.get(column), utc=True, errors="coerce")
            if not pd.isna(timestamp):
                values.append(pd.Timestamp(timestamp))
                break
    return max(values) if values else None


def _record_failure(
    store: ParquetStore,
    spec: DatabentoCmeContextSpec,
    exc: Exception,
) -> None:
    store.save_error(
        source="databento",
        category="macro",
        symbol=spec.output_symbol,
        request_key=f"cme_runtime_{spec.group_key}_{spec.schema}",
        error_type=type(exc).__name__,
        error_message=str(exc),
        metadata={
            "provider_schema": spec.schema,
            "cme_context_group": spec.group_key,
            "provider_dataset": spec.dataset,
            "queried_endpoint": spec.end.isoformat(),
        },
        pool="cme",
    )


def _record_derived_failure(store: ParquetStore, name: str, exc: Exception) -> None:
    store.save_error(
        source="databento",
        category="macro",
        symbol="CME_CONTEXT",
        request_key=f"cme_runtime_{name}",
        error_type=type(exc).__name__,
        error_message=str(exc),
        metadata={"calculation": name},
        pool="cme",
    )


def _parse_schema_values(values: Sequence[str]) -> dict[str, int]:
    output: dict[str, int] = {}
    for value in values:
        schema, separator, raw_number = str(value).partition("=")
        if not separator or not schema.strip():
            raise ValueError(f"Expected SCHEMA=NUMBER, observed {value!r}")
        output[schema.strip()] = int(raw_number)
    return output


def _next_schema_due(
    now: datetime,
    *,
    cadence_seconds: int,
    phase_seconds: int,
    allow_now: bool = False,
) -> datetime:
    current = now.astimezone(timezone.utc)
    if allow_now:
        return current
    epoch = current.timestamp()
    slot = int((epoch - phase_seconds) // cadence_seconds) + 1
    return datetime.fromtimestamp(
        slot * cadence_seconds + phase_seconds,
        tz=timezone.utc,
    )


def _print_result(result: CmeCycleResult) -> None:
    print(
        "CME cycle complete: "
        f"schemas={result.schemas_succeeded}; failed={result.schemas_failed}; "
        f"rows={result.event_rows}; partitions_written={result.partitions_written}; "
        f"partitions_reused={result.partitions_reused}; "
        f"l2_rows={result.l2_snapshot_rows}; "
        f"hourly_context_written={result.hourly_context_written}"
    )


def _utc(value: object) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(timestamp):
        raise ValueError("CME runtime timestamp is invalid")
    return pd.Timestamp(timestamp)


if __name__ == "__main__":
    raise SystemExit(main())
