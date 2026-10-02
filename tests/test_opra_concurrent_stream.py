from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor as RealExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from datafetching import opra_concurrent_stream as concurrent_stream


class OpraSyncError(RuntimeError):
    pass


class OpraNoDataError(OpraSyncError):
    pass


@dataclass
class Progress:
    completed_partitions: int = 0
    skipped_partitions: int = 0
    completed_rows: int = 0
    completed_bytes: int = 0
    errors: dict[str, str] = field(default_factory=dict)


class FakeNative:
    _SyncProgress = Progress
    OpraSyncError = OpraSyncError
    OpraNoDataError = OpraNoDataError

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []
        self.downloads = []
        self.planner_threads = []
        self.manifests = {}
        self.action = lambda day, ordinal: None
        self.plan_action = lambda day: None

    def client_factory(self):
        client = SimpleNamespace(owner=threading.get_ident())
        with self.lock:
            self.clients.append(client)
        return client

    def configure_client(self, client):
        assert client.owner == threading.get_ident()

    def _partition_time_segments(self, client, *, schema, day, symbols):
        self.planner_threads.append(threading.get_ident())
        self.plan_action(day)
        end = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
        return [(day, end, None)]

    def partition_directory(self, datastore_root, *, schema, day, symbols, segment):
        return datastore_root / schema / symbols[0] / day / (segment or "full-day")

    def verify_partition(self, destination, *, datastore_root):
        return {"manifest": self.manifests[destination]}

    def _download_partition(self, client, **kwargs):
        assert client.owner == threading.get_ident()
        with self.lock:
            ordinal = len(self.downloads)
            self.downloads.append((kwargs["day"], client, threading.get_ident()))
        self.action(kwargs["day"], ordinal)
        destination = self.partition_directory(
            kwargs["datastore_root"],
            schema=kwargs["schema"],
            day=kwargs["day"],
            symbols=kwargs["symbols"],
            segment=kwargs["segment"],
        )
        manifest = {"normalized": {"row_count": 7, "size_bytes": 91}}
        with self.lock:
            self.manifests[destination] = manifest
        destination.mkdir(parents=True)
        return manifest


def days(count):
    start = date(2026, 1, 1)
    return [("definition", (start + timedelta(days=index)).isoformat()) for index in range(count)]


def execute(native, tmp_path, **kwargs):
    return concurrent_stream.execute_concurrent_stream_plan(
        native=native,
        client_factory=native.client_factory,
        planning_client=kwargs.pop("planning_client", SimpleNamespace(owner=threading.get_ident())),
        datastore_root=tmp_path,
        entitlement={},
        symbols=("SPY.OPT",),
        plan=kwargs.pop("plan", days(5)),
        reporter=kwargs.pop("reporter", None),
        fail_fast=kwargs.pop("fail_fast", True),
        **kwargs,
    )


def test_twenty_workers_overlap_and_reuse_thread_local_clients(tmp_path):
    native = FakeNative()
    barrier = threading.Barrier(20)
    caller = threading.get_ident()
    reports, events = [], []

    def action(day, ordinal):
        if ordinal < 20:
            barrier.wait(timeout=10)
        time.sleep(0.005)

    def report(line):
        assert threading.get_ident() == caller
        reports.append(line)

    def metric(event):
        assert threading.get_ident() == caller
        events.append(event)

    native.action = action
    result = execute(native, tmp_path, workers=20, plan=days(80), reporter=report, metrics_callback=metric)

    assert result.completed_partitions == 80
    assert result.completed_rows == 560
    assert result.completed_bytes == 7280
    assert len(native.clients) == 20
    assert len({thread_id for _, _, thread_id in native.downloads}) == 20
    for thread_id in {thread_id for _, _, thread_id in native.downloads}:
        assert len({id(client) for _, client, owner in native.downloads if owner == thread_id}) == 1
    assert set(native.planner_threads) == {caller}
    assert len(reports) == 80
    summary = events[-1]
    assert summary["event"] == "summary"
    assert summary["peak_active_tasks"] == 20
    assert summary["peak_pending_tasks"] == 20
    assert summary["active_tasks"] == 0
    assert summary["worker_clients_created"] == 20
    assert summary["download_publish_seconds"] > 0
    assert summary["elapsed_seconds"] > 0
    assert max(event.get("pending_tasks", 0) for event in events) <= 20


def test_resume_verifies_existing_without_creating_worker_clients(tmp_path):
    native = FakeNative()
    execute(native, tmp_path, workers=2)
    native.clients.clear()
    native.downloads.clear()
    reports = []

    result = execute(native, tmp_path, workers=20, reporter=reports.append)

    assert result.completed_partitions == 0
    assert result.skipped_partitions == 5
    assert result.completed_rows == 35
    assert result.completed_bytes == 455
    assert native.clients == []
    assert native.downloads == []
    assert all(line.startswith("VERIFIED_EXISTING ") for line in reports)


def test_duplicate_destination_is_rejected_without_duplicate_download(tmp_path):
    native = FakeNative()
    plan = days(1) * 2
    with pytest.raises(OpraSyncError, match="Duplicate OPRA partition destination"):
        execute(native, tmp_path, workers=2, plan=plan)
    assert len(native.downloads) <= 1


def test_fail_fast_drains_and_reports_running_publications(tmp_path):
    native = FakeNative()
    barrier = threading.Barrier(3)
    failure = OpraSyncError("download broke")
    reports, events = [], []

    def action(day, ordinal):
        barrier.wait(timeout=10)
        if day == days(1)[0][1]:
            raise failure
        time.sleep(0.05)

    native.action = action
    with pytest.raises(OpraSyncError, match="download broke") as error:
        execute(native, tmp_path, workers=3, plan=days(8), reporter=reports.append, metrics_callback=events.append)

    assert error.value is failure
    assert len(native.downloads) == 3
    assert len(native.manifests) == 2
    assert sum(line.startswith("PUBLISHED ") for line in reports) == 2
    assert events[-1]["published_tasks"] == 2
    assert events[-1]["failed_tasks"] == 1
    assert events[-1]["active_tasks"] == 0


def test_fail_fast_cancels_queued_work_and_stops_submission(tmp_path, monkeypatch):
    native = FakeNative()
    submitted = threading.Event()
    events = []

    def factory(*, max_workers, thread_name_prefix):
        # Keep tasks queued deterministically to exercise cancellation.
        return RealExecutor(max_workers=1, thread_name_prefix=thread_name_prefix)

    def action(day, ordinal):
        assert submitted.wait(timeout=10)
        raise OpraSyncError("stop queued work")

    def metric(event):
        events.append(event)
        if event["event"] == "submitted" and event["submitted_tasks"] == 3:
            submitted.set()

    monkeypatch.setattr(concurrent_stream, "ThreadPoolExecutor", factory)
    native.action = action
    with pytest.raises(OpraSyncError, match="stop queued work"):
        execute(native, tmp_path, workers=3, plan=days(8), metrics_callback=metric)

    assert events[-1]["submitted_tasks"] == 3
    assert events[-1]["cancelled_tasks"] >= 1
    assert len(native.downloads) < 3
    assert events[-1]["active_tasks"] == 0


def test_planner_failure_drains_running_publications(tmp_path):
    native = FakeNative()
    started = threading.Barrier(3)
    events, reports = [], []
    bad_day = days(3)[2][1]

    def action(day, ordinal):
        started.wait(timeout=10)
        time.sleep(0.03)

    def plan_action(day):
        if day == bad_day:
            started.wait(timeout=10)
            raise OpraSyncError("planner broke")

    native.action = action
    native.plan_action = plan_action
    with pytest.raises(OpraSyncError, match="planner broke"):
        execute(native, tmp_path, workers=3, plan=days(8), reporter=reports.append, metrics_callback=events.append)

    assert len(native.downloads) == 2
    assert sum(line.startswith("PUBLISHED ") for line in reports) == 2
    assert events[-1]["published_tasks"] == 2
    assert events[-1]["active_tasks"] == 0


def test_max_partitions_limits_planning_and_dispatch(tmp_path):
    native = FakeNative()
    result = execute(native, tmp_path, workers=20, plan=days(8), max_partitions=3)
    assert result.completed_partitions == 3
    assert len(native.downloads) == 3
    assert len(native.planner_threads) == 3


def test_max_partitions_counts_segments_without_planning_the_next_day(tmp_path):
    native = FakeNative()
    planned = []

    def segments(client, *, schema, day, symbols):
        planned.append(day)
        return [(day, day, f"segment-{index}") for index in range(3)]

    native._partition_time_segments = segments
    result = execute(native, tmp_path, workers=2, plan=days(2), max_partitions=2)

    assert result.completed_partitions == 2
    assert planned == [days(1)[0][1]]
    assert len(native.downloads) == 2


def test_one_worker_uses_a_separate_lazy_planning_client(tmp_path):
    native = FakeNative()
    events = []
    result = execute(native, tmp_path, workers=1, planning_client=None, metrics_callback=events.append)
    assert result.completed_partitions == 5
    assert len(native.clients) == 2
    assert native.clients[0].owner == threading.get_ident()
    assert native.clients[1].owner != threading.get_ident()
    assert events[-1]["worker_clients_created"] == 1
    assert events[-1]["peak_active_tasks"] == 1


def test_no_data_preserves_skip_semantics(tmp_path):
    native = FakeNative()
    reports = []

    def action(day, ordinal):
        if day == days(1)[0][1]:
            raise OpraNoDataError("clean empty provider response")

    native.action = action
    result = execute(native, tmp_path, workers=2, plan=days(2), reporter=reports.append)
    assert result.completed_partitions == 1
    assert result.skipped_partitions == 1
    assert result.errors == {}
    assert sum(line.startswith("NO_DATA ") for line in reports) == 1


def test_non_fail_fast_records_failure_and_finishes_remaining_tasks(tmp_path):
    native = FakeNative()

    def action(day, ordinal):
        if day == days(1)[0][1]:
            raise OpraSyncError("one failure")

    native.action = action
    result = execute(native, tmp_path, workers=2, fail_fast=False)
    assert result.completed_partitions == 4
    assert result.errors == {"definition/2026-01-01": "OpraSyncError: one failure"}
    assert len(native.downloads) == 5


@pytest.mark.parametrize("workers", [0, 41, True, 20.0])
def test_invalid_worker_bounds_fail_before_planning(tmp_path, workers):
    native = FakeNative()
    with pytest.raises(ValueError, match="1 to 40"):
        execute(native, tmp_path, workers=workers)
    assert native.planner_threads == []
    assert native.clients == []


@pytest.mark.parametrize("maximum", [0, -1, True, 3.0])
def test_invalid_partition_bounds_fail_before_planning(tmp_path, maximum):
    native = FakeNative()
    with pytest.raises(ValueError, match="positive integer"):
        execute(native, tmp_path, max_partitions=maximum)
    assert native.planner_threads == []


@pytest.fixture
def native_hook(tmp_path, monkeypatch):
    from datafetching import databento_opra_history as native

    fake = FakeNative()
    caller = threading.get_ident()
    timeline = []
    events = []
    for name in (
        "configure_client", "_partition_time_segments", "partition_directory",
        "verify_partition", "_download_partition",
    ):
        monkeypatch.setattr(native, name, getattr(fake, name))
    monkeypatch.setattr(native, "_validate_storage_preflight_receipt", lambda *args, **kwargs: {"capacity_pass": True})
    monkeypatch.setattr(native, "_partition_plan", lambda *args, **kwargs: days(5))

    def cursor(datastore_root, *, schema):
        assert threading.get_ident() == caller
        assert len(fake.manifests) == 5
        timeline.append("cursor")

    def health(datastore_root):
        assert threading.get_ident() == caller
        assert len(fake.manifests) == 5
        timeline.append("health")
        return tmp_path / "health.json"

    def metric(event):
        assert threading.get_ident() == caller
        events.append(event)
        if event["event"] == "summary":
            assert event["active_tasks"] == 0
            timeline.append("summary")

    monkeypatch.setattr(native, "_publish_cursor", cursor)
    monkeypatch.setattr(native, "publish_health", health)

    def invoke(**kwargs):
        return native.synchronize(
            SimpleNamespace(owner=caller),
            datastore_root=tmp_path,
            entitlement={},
            scope=native.SyncScope(schemas=("definition",), symbols=("SPY.OPT",)),
            storage_preflight_receipt={},
            reporter=None,
            **kwargs,
        )

    return SimpleNamespace(native=native, fake=fake, invoke=invoke, timeline=timeline, events=events, metric=metric)


def test_native_hook_publishes_cursor_and_health_after_concurrent_drain(native_hook):
    hook = native_hook
    barrier = threading.Barrier(2)

    def action(day, ordinal):
        if ordinal < 2:
            barrier.wait(timeout=10)
        time.sleep(0.005)

    hook.fake.action = action
    result = hook.invoke(
        partition_workers=2,
        partition_client_factory=hook.fake.client_factory,
        partition_metrics_callback=hook.metric,
    )
    assert result.status == "COMPLETE"
    assert result.completed_partitions == 5
    assert result.completed_rows == 35
    assert result.completed_bytes == 455
    assert len(hook.fake.clients) == 2
    assert hook.timeline == ["summary", "cursor", "health"]
    assert hook.events[-1]["peak_active_tasks"] == 2


def test_native_hook_defaults_to_serial_existing_writer(native_hook, monkeypatch):
    hook = native_hook

    def unexpected_pool(**kwargs):
        raise AssertionError("Default native writer should stay serial")

    monkeypatch.setattr(concurrent_stream, "execute_concurrent_stream_plan", unexpected_pool)
    result = hook.invoke()
    assert result.status == "COMPLETE"
    assert result.completed_partitions == 5
    assert hook.fake.clients == []
    assert {owner for _, _, owner in hook.fake.downloads} == {threading.get_ident()}
    assert hook.timeline == ["cursor", "health"]


def test_native_hook_fatal_failure_drains_without_publishing_cursor_or_health(native_hook):
    hook = native_hook
    barrier = threading.Barrier(3)

    def action(day, ordinal):
        barrier.wait(timeout=10)
        if day == days(1)[0][1]:
            raise hook.native.OpraSyncError("native integration failure")
        time.sleep(0.02)

    hook.fake.action = action
    with pytest.raises(hook.native.OpraSyncError, match="native integration failure"):
        hook.invoke(
            fail_fast=True,
            partition_workers=3,
            partition_client_factory=hook.fake.client_factory,
            partition_metrics_callback=hook.metric,
        )
    assert len(hook.fake.manifests) == 2
    assert hook.timeline == ["summary"]
    assert hook.events[-1]["published_tasks"] == 2
    assert hook.events[-1]["active_tasks"] == 0


@pytest.mark.parametrize("workers", [0, 41, True, 20.0])
def test_native_hook_rejects_invalid_workers_before_configuration(native_hook, monkeypatch, workers):
    hook = native_hook

    def unexpected_configuration(client):
        raise AssertionError("Invalid settings must fail before client configuration")

    monkeypatch.setattr(hook.native, "configure_client", unexpected_configuration)
    with pytest.raises(ValueError, match="1 to 40"):
        hook.invoke(partition_workers=workers)
    assert hook.fake.clients == []
    assert hook.fake.planner_threads == []


@pytest.mark.parametrize("factory", [None, object()])
def test_native_hook_rejects_missing_or_noncallable_factory_before_configuration(native_hook, monkeypatch, factory):
    hook = native_hook

    def unexpected_configuration(client):
        raise AssertionError("Invalid settings must fail before client configuration")

    monkeypatch.setattr(hook.native, "configure_client", unexpected_configuration)
    with pytest.raises(ValueError, match="independent clients"):
        hook.invoke(partition_workers=2, partition_client_factory=factory)
    assert hook.fake.clients == []


def test_native_hook_rejects_concurrent_batch_before_configuration(native_hook, monkeypatch):
    hook = native_hook

    def unexpected_configuration(client):
        raise AssertionError("Invalid settings must fail before client configuration")

    monkeypatch.setattr(hook.native, "configure_client", unexpected_configuration)
    with pytest.raises(ValueError, match="batch_download=False"):
        hook.invoke(partition_workers=2, partition_client_factory=hook.fake.client_factory, batch_download=True)
    assert hook.fake.clients == []
