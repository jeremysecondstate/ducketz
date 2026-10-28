"""Local offline integration: cache reports never alter original order evidence."""
import json
from contextlib import contextmanager
import socket
import sqlite3

import pytest

from ml.intraday import intentions as module
from ml.intraday.intentions import IntentionStore, _RecoveryTimings
from ml.intraday.sizing_header_cache import DurableSizingHeaders
from ml.intraday.store import ShadowStore
from tests.test_intraday_batched_sizing_headers import original_rows
from tests.test_intraday_durable_sizing_headers import cache_path, decoded, projection_rows
from tests.test_intraday_recovery_batch_preflight import fixture, run


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Sizing diagnostics integration must remain offline")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def test_authenticated_report_reaches_existing_recovery_diagnostics(tmp_path, monkeypatch):
    source, _, _, groups, expected = fixture(tmp_path, 5)
    store, timings = ShadowStore(source.path), _RecoveryTimings()
    cold = IntentionStore(store)
    before = original_rows(store), projection_rows(store)
    decoded_rows = decoded(cold, monkeypatch)
    assert run(cold, groups, timings=timings) == expected
    report = timings.snapshot()["read_details"]["sizing_cache"]
    assert report["attempts"] == 1 and report["outcomes"] == {"complete":1}
    assert report["requested_count"] == report["expected_count"] == 5
    assert report["validated_count"] == report["returned_count"] == 5
    assert report["discarded_count"] == report["rejected_count"] == 0
    assert decoded_rows == []
    assert (original_rows(store), projection_rows(store)) == before
    # Snapshot callers cannot mutate accumulated evidence.
    report["outcomes"]["complete"] = -1
    assert timings.snapshot()["read_details"]["sizing_cache"]["outcomes"] == {"complete":1}
    raw = json.dumps(timings.snapshot(), allow_nan=False)
    assert str(source.path) not in raw
    assert all(key not in raw for key in groups)


def test_absent_optional_cache_keeps_exact_original_fallback(tmp_path, monkeypatch):
    source, _, _, groups, expected = fixture(tmp_path, 5)
    cache_path(source.path).unlink()
    store, timings = ShadowStore(source.path), _RecoveryTimings()
    cold = IntentionStore(store)
    before = original_rows(store), projection_rows(store)
    decoded_rows = decoded(cold, monkeypatch)
    assert run(cold, groups, timings=timings) == expected
    report = timings.snapshot()["read_details"]["sizing_cache"]
    assert report["attempts"] == 1 and report["outcomes"] == {"missing_file":1}
    assert report["requested_count"] == 5 and report["returned_count"] == 0
    assert len(decoded_rows) == 5
    assert (original_rows(store), projection_rows(store)) == before


def test_raising_custom_diagnostic_consumer_does_not_change_recovery(tmp_path, monkeypatch):
    source, _, _, groups, expected = fixture(tmp_path, 3)
    store, timings = ShadowStore(source.path), _RecoveryTimings()
    cold = IntentionStore(store)
    def failure(report):
        raise RuntimeError("private diagnostic consumer failure")
    monkeypatch.setattr(timings, "cache_read", failure)
    assert run(cold, groups, timings=timings) == expected


@pytest.mark.parametrize("cause", ["identity_unavailable", "source_unpinned"])
def test_batch_cache_bypass_records_reason_and_retains_original_fallback(tmp_path, monkeypatch, cause):
    source, _, _, groups, expected = fixture(tmp_path, 3)
    store, timings = ShadowStore(source.path), _RecoveryTimings()
    cold = IntentionStore(store)
    before = original_rows(store), projection_rows(store)
    decoded_rows = decoded(cold, monkeypatch)
    if cause == "identity_unavailable":
        path_type = type(store.path)
        resolve = path_type.resolve
        def unavailable(self, *args, **kwargs):
            if self == store.path:
                raise OSError("synthetic source identity unavailable")
            return resolve(self, *args, **kwargs)
        monkeypatch.setattr(path_type, "resolve", unavailable)
    else:
        connect = store.connect
        @contextmanager
        def unpinned():
            with connect() as db:
                store._connection_source_identities[id(db)] = None
                yield db
        monkeypatch.setattr(store, "connect", unpinned)
    monkeypatch.setattr(DurableSizingHeaders, "load_many",
        lambda *args, **kwargs: pytest.fail("Uncertain source opened optional sizing cache"))
    assert run(cold, groups, timings=timings) == expected
    report = timings.snapshot()["read_details"]["sizing_cache"]
    assert report["attempts"] == 1 and report["outcomes"] == {cause:1}
    assert report["requested_count"] == 3
    assert report["expected_count"] == report["returned_count"] == 0
    assert len(decoded_rows) == 3
    assert (original_rows(store), projection_rows(store)) == before


def report(reason="complete", code=None):
    return dict(schema_version="sizing-cache-load-outcome-v1", reason=reason,
        requested_count=5, expected_count=5, validated_count=3, returned_count=3,
        discarded_count=0, rejected_count=2, sqlite_errorcode=code)


@pytest.mark.parametrize("damage", ["unknown_reason", "extra", "bad_count", "bad_code", "object"])
def test_bad_reports_are_unavailable_without_unbounded_material(damage):
    timings, item = _RecoveryTimings(), report()
    if damage == "unknown_reason": item["reason"] = "PRIVATE"
    elif damage == "extra": item["PRIVATE"] = "payload"
    elif damage == "bad_count": item["returned_count"] = -1
    elif damage == "bad_code": item["sqlite_errorcode"] = "PRIVATE"
    else: item = object()
    timings.cache_read(item)
    snapshot = timings.snapshot()["read_details"]
    assert snapshot["diagnostics_unavailable"]
    assert snapshot["sizing_cache"]["attempts"] == 0
    assert "PRIVATE" not in json.dumps(snapshot)


def test_aggregation_is_bounded_fixed_schema_and_buckets_extended_codes():
    timings = _RecoveryTimings()
    for code in (sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY | 256,
                 sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_CORRUPT):
        timings.cache_read(report("sqlite_error", code))
    stats = timings.snapshot()["read_details"]["sizing_cache"]
    assert stats["attempts"] == 4 and stats["outcomes"] == {"sqlite_error":4}
    assert all(stats[key] == 1 for key in ("sqlite_interrupt_count", "sqlite_busy_count",
                                          "sqlite_locked_count", "sqlite_other_count"))
    assert stats["requested_count"] == 20 and stats["validated_count"] == 12
    timings.sizing_cache["attempts"] = 2147483647
    timings.sizing_cache["requested_count"] = 2147483646
    timings.cache_read(report())
    stats = timings.snapshot()["read_details"]["sizing_cache"]
    assert stats["attempts"] == stats["requested_count"] == 2147483647
