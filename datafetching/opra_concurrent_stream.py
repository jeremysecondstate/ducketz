"""Bounded concurrency for the native immutable OPRA partition writer.

The caller retains the ordinary scope ownership lock and publishes aggregate
cursors and health after this helper returns. Planning, reporting, and progress
aggregation stay on the calling thread; each worker owns its provider client.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class _Task:
    ordinal: int
    schema: str
    day: str
    start: str
    end: str
    segment: str | None
    destination: Path

    @property
    def key(self) -> str:
        return f"{self.schema}/{self.day}" + (
            f"/{self.segment}" if self.segment else ""
        )


@dataclass(frozen=True)
class _Outcome:
    task: _Task
    status: str
    duration_seconds: float
    rows: int = 0
    size_bytes: int = 0
    error: BaseException | None = None


def execute_concurrent_stream_plan(
    *,
    native: Any,
    client_factory: Callable[[], Any],
    datastore_root: Path,
    entitlement: Mapping[str, object],
    symbols: Sequence[str],
    plan: Iterable[tuple[str, str]],
    reporter: Callable[[str], None] | None,
    fail_fast: bool,
    max_partitions: int | None = None,
    workers: int = 20,
    metrics_callback: Callable[[Mapping[str, object]], None] | None = None,
    planning_client: Any | None = None,
) -> Any:
    """Return ``native._SyncProgress`` using at most ``workers`` pending tasks.

    ``planning_client`` should be the existing scope client. When omitted, one
    is lazily created for serial segment planning. Worker clients are created
    independently and reused only on their owning thread. Metrics callbacks
    receive ``submitted``, outcome, and final ``summary`` events on the caller's
    thread. A fatal failure cancels queued tasks and drains running publications
    before re-raising the original exception.
    """

    if type(workers) is not int or not 1 <= workers <= 40:
        raise ValueError("OPRA concurrent workers must be an integer from 1 to 40")
    if max_partitions is not None and (
        type(max_partitions) is not int or max_partitions < 1
    ):
        raise ValueError("max_partitions must be a positive integer")

    progress = native._SyncProgress()
    local = threading.local()
    counter_lock = threading.Lock()
    stop_requested = threading.Event()
    started_at = time.perf_counter()
    counters: dict[str, int | float] = {
        "submitted_tasks": 0,
        "started_tasks": 0,
        "active_tasks": 0,
        "peak_active_tasks": 0,
        "peak_pending_tasks": 0,
        "worker_clients_created": 0,
        "published_tasks": 0,
        "existing_tasks": 0,
        "no_data_tasks": 0,
        "failed_tasks": 0,
        "cancelled_tasks": 0,
        "download_publish_seconds": 0.0,
        "existing_verify_seconds": 0.0,
        "failed_task_seconds": 0.0,
    }

    def emit(event: str, **fields: object) -> None:
        if metrics_callback is not None:
            with counter_lock:
                snapshot = dict(counters)
            metrics_callback({"event": event, "workers": workers, **snapshot, **fields})

    def tasks() -> Iterable[_Task]:
        nonlocal planning_client
        ordinal = 0
        for schema, day in plan:
            if max_partitions is not None and ordinal >= max_partitions:
                return
            if planning_client is None:
                planning_client = client_factory()
                native.configure_client(planning_client)
            segments = native._partition_time_segments(
                planning_client, schema=schema, day=day, symbols=symbols
            )
            for start, end, segment in segments:
                if max_partitions is not None and ordinal >= max_partitions:
                    return
                destination = native.partition_directory(
                    datastore_root,
                    schema=schema,
                    day=day,
                    symbols=symbols,
                    segment=segment,
                )
                yield _Task(ordinal, schema, day, start, end, segment, destination)
                ordinal += 1

    def run(task: _Task) -> _Outcome:
        if stop_requested.is_set():
            return _Outcome(task, "cancelled", 0.0)
        began = time.perf_counter()
        with counter_lock:
            counters["started_tasks"] += 1
            counters["active_tasks"] += 1
            counters["peak_active_tasks"] = max(
                counters["peak_active_tasks"], counters["active_tasks"]
            )
        try:
            if task.destination.is_dir():
                manifest = native.verify_partition(
                    task.destination, datastore_root=datastore_root
                )["manifest"]
                status = "existing"
            else:
                if not hasattr(local, "client"):
                    local.client = client_factory()
                    native.configure_client(local.client)
                    with counter_lock:
                        counters["worker_clients_created"] += 1
                manifest = native._download_partition(
                    local.client,
                    datastore_root=datastore_root,
                    entitlement=entitlement,
                    schema=task.schema,
                    day=task.day,
                    symbols=symbols,
                    request_start=task.start,
                    request_end=task.end,
                    segment=task.segment,
                )
                status = "published"
            return _Outcome(
                task,
                status,
                time.perf_counter() - began,
                rows=int(manifest["normalized"]["row_count"]),
                size_bytes=int(manifest["normalized"]["size_bytes"]),
            )
        except native.OpraNoDataError as exc:
            return _Outcome(task, "no_data", time.perf_counter() - began, error=exc)
        except BaseException as exc:
            if fail_fast or not isinstance(exc, Exception):
                stop_requested.set()
            return _Outcome(task, "failed", time.perf_counter() - began, error=exc)
        finally:
            with counter_lock:
                counters["active_tasks"] -= 1

    def aggregate(outcome: _Outcome) -> None:
        status, task = outcome.status, outcome.task
        with counter_lock:
            counters[f"{status}_tasks"] += 1
            if status == "published":
                counters["download_publish_seconds"] += outcome.duration_seconds
            elif status == "existing":
                counters["existing_verify_seconds"] += outcome.duration_seconds
            elif status == "failed":
                counters["failed_task_seconds"] += outcome.duration_seconds
        if status in {"published", "existing"}:
            if status == "published":
                progress.completed_partitions += 1
            else:
                progress.skipped_partitions += 1
            progress.completed_rows += outcome.rows
            progress.completed_bytes += outcome.size_bytes
        elif status == "no_data":
            progress.skipped_partitions += 1
        elif status == "failed":
            exc = outcome.error
            progress.errors[task.key] = f"{type(exc).__name__}: {exc}"

        # Aggregate first so reporter/metrics failures cannot discard a native
        # publication that has already completed successfully.
        if reporter:
            if status == "published":
                reporter(f"PUBLISHED {task.key} rows={outcome.rows} bytes={outcome.size_bytes}")
            elif status == "existing":
                reporter(f"VERIFIED_EXISTING {task.key}")
            elif status == "no_data":
                reporter(f"NO_DATA {task.key} {outcome.error}")
            elif status == "failed":
                reporter(f"FAILED {task.key} {progress.errors[task.key]}")
        emit(
            status,
            key=task.key,
            duration_seconds=outcome.duration_seconds,
            rows=outcome.rows,
            size_bytes=outcome.size_bytes,
        )

    iterator = iter(tasks())
    pending: dict[Future[_Outcome], _Task] = {}
    destinations: set[Path] = set()
    exhausted = False
    fatal_error: BaseException | None = None

    def remember_failure(exc: BaseException) -> None:
        nonlocal fatal_error, exhausted
        if fatal_error is None:
            fatal_error = exc
        stop_requested.set()
        exhausted = True

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="opra-partition") as pool:
        while pending or not exhausted:
            if fatal_error is None:
                try:
                    while len(pending) < workers and not exhausted and not stop_requested.is_set():
                        try:
                            task = next(iterator)
                        except StopIteration:
                            exhausted = True
                            break
                        identity = task.destination.resolve()
                        if identity in destinations:
                            raise native.OpraSyncError(
                                f"Duplicate OPRA partition destination: {task.key}"
                            )
                        destinations.add(identity)
                        future = pool.submit(run, task)
                        pending[future] = task
                        with counter_lock:
                            counters["submitted_tasks"] += 1
                            counters["peak_pending_tasks"] = max(
                                counters["peak_pending_tasks"], len(pending)
                            )
                        emit("submitted", key=task.key, pending_tasks=len(pending))
                except BaseException as exc:
                    remember_failure(exc)

            if fatal_error is not None:
                for future in pending:
                    future.cancel()
            if not pending:
                break
            try:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
            except BaseException as exc:
                remember_failure(exc)
                continue
            for future in sorted(done, key=lambda item: pending[item].ordinal):
                task = pending.pop(future)
                if future.cancelled():
                    with counter_lock:
                        counters["cancelled_tasks"] += 1
                    try:
                        emit("cancelled", key=task.key)
                    except BaseException as exc:
                        remember_failure(exc)
                    continue
                try:
                    outcome = future.result()
                except BaseException as exc:
                    outcome = _Outcome(task, "failed", 0.0, error=exc)
                if outcome.status == "failed" and (
                    fail_fast or not isinstance(outcome.error, Exception)
                ):
                    assert outcome.error is not None
                    remember_failure(outcome.error)
                try:
                    aggregate(outcome)
                except BaseException as exc:
                    remember_failure(exc)

    try:
        emit(
            "summary",
            elapsed_seconds=time.perf_counter() - started_at,
            completed_partitions=progress.completed_partitions,
            skipped_partitions=progress.skipped_partitions,
            completed_rows=progress.completed_rows,
            completed_bytes=progress.completed_bytes,
            error_count=len(progress.errors),
            fatal_error_type=type(fatal_error).__name__ if fatal_error else None,
        )
    except BaseException as exc:
        remember_failure(exc)
    if fatal_error is not None:
        raise fatal_error
    return progress
