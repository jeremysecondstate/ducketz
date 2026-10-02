"""Offline concurrency coverage using the real immutable partition publisher."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

from datafetching import databento_opra_history as native
from datafetching.opra_concurrent_stream import execute_concurrent_stream_plan
from ml.artifacts import file_checksum


class _SyntheticDBNStore:
    """Replace the provider's SDK conversion while writing genuine Parquet."""

    def __init__(self, day: str):
        self.day = day

    def to_parquet(self, path: Path, *, map_symbols: bool) -> None:
        assert map_symbols is True
        start = pd.Timestamp(self.day, tz="UTC")
        pd.DataFrame(
            {
                "ts_event": [start + pd.Timedelta(hours=13), start + pd.Timedelta(hours=14)],
                "publisher_id": [1, 1],
                "instrument_id": [12345, 12345],
                "symbol": ["SPY   260320C00600000", "SPY   260320C00600000"],
                "open": [1.0, 1.25],
                "high": [1.5, 1.75],
                "low": [0.75, 1.0],
                "close": [1.25, 1.5],
                "volume": [10, 12],
            }
        ).to_parquet(path, index=False)


class _SyntheticProvider:
    def __init__(self, workers: int):
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(workers)
        self.requests: list[dict[str, object]] = []
        self.client_owners: list[int] = []
        self.active = 0
        self.peak_active = 0

    def client_factory(self):
        owner = threading.get_ident()
        with self.lock:
            self.client_owners.append(owner)
        return self.make_client(owner)

    def make_client(self, owner: int):
        provider = self

        class TimeSeries:
            TIMEOUT = 0

            def get_range(self, **kwargs):
                assert threading.get_ident() == owner
                assert kwargs["dataset"] == "OPRA.PILLAR"
                assert kwargs["schema"] == "ohlcv-1h"
                assert kwargs["symbols"] == ["SPY.OPT"]
                assert kwargs["stype_in"] == "parent"
                with provider.lock:
                    provider.requests.append(dict(kwargs))
                    provider.active += 1
                    provider.peak_active = max(provider.peak_active, provider.active)
                try:
                    provider.barrier.wait(timeout=10)
                    day = pd.Timestamp(kwargs["start"]).date().isoformat()
                    # The fake SDK returns the store directly, so publication
                    # can checksum these opaque synthetic bytes without DBN
                    # parsing. Every subsequent writer/validator is native.
                    Path(kwargs["path"]).write_bytes(f"synthetic-dbn-fixture:{day}".encode())
                    return _SyntheticDBNStore(day)
                finally:
                    with provider.lock:
                        provider.active -= 1

        return SimpleNamespace(metadata=SimpleNamespace(TIMEOUT=0), timeseries=TimeSeries())


@pytest.mark.parametrize("workers", [20, 40])
def test_native_publications_verify_and_resume_without_provider_requests(tmp_path: Path, workers: int):
    provider = _SyntheticProvider(workers)
    start = date(2026, 1, 1)
    dates = [(start + timedelta(days=index)).isoformat() for index in range(workers)]
    plan = [("ohlcv-1h", day) for day in dates]
    symbols = ("SPY.OPT",)
    entitlement = {
        "entitlements": {
            "ohlcv-1h": {
                "entitled_start": dates[0],
                "entitled_end": (start + timedelta(days=workers)).isoformat(),
            }
        }
    }
    reports, metrics = [], []
    arguments = {
        "native": native,
        "client_factory": provider.client_factory,
        "planning_client": provider.make_client(threading.get_ident()),
        "datastore_root": tmp_path,
        "entitlement": entitlement,
        "symbols": symbols,
        "plan": plan,
        "reporter": reports.append,
        "fail_fast": True,
        "workers": workers,
        "metrics_callback": metrics.append,
    }

    first = execute_concurrent_stream_plan(**arguments)

    assert first.completed_partitions == workers
    assert first.skipped_partitions == 0
    assert first.completed_rows == workers * 2
    assert first.errors == {}
    assert len(provider.requests) == workers
    assert len(set(provider.client_owners)) == workers
    assert provider.peak_active == workers
    assert metrics[-1]["peak_active_tasks"] == workers
    assert sum(line.startswith("PUBLISHED ") for line in reports) == workers

    destinations = [
        native.partition_directory(tmp_path, schema="ohlcv-1h", day=day, symbols=symbols)
        for day in dates
    ]
    assert len(set(destinations)) == workers
    inventories = {}
    total_bytes = 0
    for day, destination in zip(dates, destinations, strict=True):
        verified = native.verify_partition(destination, datastore_root=tmp_path)
        manifest, receipt = verified["manifest"], verified["receipt"]
        assert manifest["partition_date"] == day
        assert manifest["request"]["symbols"] == ["SPY.OPT"]
        assert manifest["normalized"]["row_count"] == 2
        assert manifest["normalized"]["duplicate_natural_key_rows"] == 0
        assert receipt["raw_checksum_sha256"] == manifest["raw"]["checksum_sha256"]
        assert receipt["normalized_checksum_sha256"] == manifest["normalized"]["checksum_sha256"]
        assert (destination / "provider.dbn.zst").read_bytes() == f"synthetic-dbn-fixture:{day}".encode()
        total_bytes += manifest["normalized"]["size_bytes"]
        inventories[destination] = {
            name: file_checksum(destination / name)
            for name in ("provider.dbn.zst", "normalized.parquet", "manifest.json", "receipt.json")
        }
    assert first.completed_bytes == total_bytes
    assert not list(native.canonical_root(tmp_path).glob(".staging/**/provider.dbn.zst"))

    reports.clear()
    second = execute_concurrent_stream_plan(**arguments)

    assert second.completed_partitions == 0
    assert second.skipped_partitions == workers
    assert second.completed_rows == workers * 2
    assert second.completed_bytes == total_bytes
    assert second.errors == {}
    assert len(provider.requests) == workers
    assert len(provider.client_owners) == workers
    assert metrics[-1]["worker_clients_created"] == 0
    assert sum(line.startswith("VERIFIED_EXISTING ") for line in reports) == workers
    for destination, inventory in inventories.items():
        assert native.verify_partition(destination, datastore_root=tmp_path)["manifest"]["normalized"]["duplicate_natural_key_rows"] == 0
        assert {name: file_checksum(destination / name) for name in inventory} == inventory


def test_midstream_retries_retain_open_partial_attempts_before_native_publication(
    tmp_path: Path, monkeypatch, offline_databento_sdk,
):
    from requests.exceptions import ReadTimeout
    from databento.common.error import BentoError
    requests = []
    retained_handles = []
    pauses = []
    day = "2026-01-01"
    successful_raw = b"synthetic-complete-dbn-fixture:2026-01-01"
    monkeypatch.setattr(native.time, "sleep", pauses.append)

    class TimeSeries:
        def get_range(self, **kwargs):
            requests.append(dict(kwargs))
            attempt = len(requests)
            path = Path(kwargs["path"])
            if attempt <= 3:
                # Match the SDK's exclusive open. Retain strong references to
                # open handles across every retry and final publication so a
                # Windows directory move/delete would fail instead of hiding
                # an unsafe overwrite of the previous partial attempt.
                handle = path.open("x+b")
                handle.write(f"synthetic-partial-attempt-{attempt}".encode())
                handle.flush()
                retained_handles.append(handle)
                try:
                    raise ReadTimeout("synthetic stream timeout")
                except ReadTimeout as exc:
                    raise BentoError("Error streaming response: read timed out") from exc
            assert attempt == 4
            assert len(retained_handles) == 3
            assert all(not handle.closed for handle in retained_handles)
            with path.open("x+b") as handle:
                handle.write(successful_raw)
            return _SyntheticDBNStore(day)

    client = SimpleNamespace(timeseries=TimeSeries())
    entitlement = {"entitlements": {"ohlcv-1h": {"entitled_start": day, "entitled_end": "2026-01-02"}}}

    try:
        manifest = native._download_partition(
            client,
            datastore_root=tmp_path,
            entitlement=entitlement,
            schema="ohlcv-1h",
            day=day,
            symbols=("SPY.OPT",),
        )

        assert len(requests) == 4
        assert pauses == [4.0, 4.0, 4.0]
        scoped_requests = [{name: value for name, value in request.items() if name != "path"} for request in requests]
        assert all(request == manifest["request"] for request in scoped_requests)
        assert manifest["request"]["start"] == day
        assert manifest["request"]["end"] == "2026-01-02"
        assert manifest["request"]["symbols"] == ["SPY.OPT"]
        attempted_paths = [Path(request["path"]) for request in requests]
        assert len(set(attempted_paths)) == 4
        assert len({path.parent.parent for path in attempted_paths}) == 1
        assert [path.parent.name for path in attempted_paths] == [f"attempt-{index:03d}" for index in range(1, 5)]

        destination = native.partition_directory(tmp_path, schema="ohlcv-1h", day=day, symbols=("SPY.OPT",))
        verified = native.verify_partition(destination, datastore_root=tmp_path)
        assert verified["manifest"] == manifest
        assert manifest["normalized"]["row_count"] == 2
        assert manifest["normalized"]["duplicate_natural_key_rows"] == 0
        assert (destination / "provider.dbn.zst").read_bytes() == successful_raw
        assert verified["receipt"]["raw_checksum_sha256"] == file_checksum(destination / "provider.dbn.zst")
        assert verified["receipt"]["normalized_checksum_sha256"] == file_checksum(destination / "normalized.parquet")
        assert not attempted_paths[-1].parent.exists()
        assert all(not handle.closed for handle in retained_handles)

        staging_root = native.canonical_root(tmp_path) / ".staging"
        retained_paths = sorted(staging_root.glob("**/provider.dbn.zst"))
        assert retained_paths == attempted_paths[:3]
        for attempt, path in enumerate(retained_paths, start=1):
            assert path.is_relative_to(staging_root)
            assert not path.is_relative_to(destination)
            assert path.read_bytes() == f"synthetic-partial-attempt-{attempt}".encode()
            assert not (path.parent / "manifest.json").exists()
            assert not (path.parent / "receipt.json").exists()
    finally:
        for handle in retained_handles:
            handle.close()
