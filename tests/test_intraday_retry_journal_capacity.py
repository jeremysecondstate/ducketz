"""Offline overflow regression: old retry identities and attempt evidence survive."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import socket
import subprocess
import threading

import pytest

from ml.intraday import provider_recovery as recovery


NOW = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_live_operations(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Offline fixtures cannot start a process or use a network")
    monkeypatch.setattr(socket, "socket", denied)
    monkeypatch.setattr(subprocess, "Popen", denied)


@pytest.fixture
def full_ledger(tmp_path):
    path = tmp_path / "retry.json"
    state = {f"old-{i}": {"status": "ok", "attempt_count": 3,
                         "last_received_at": NOW.isoformat()} for i in range(1999)}
    state["rejected"] = {"status": "request_rejected", "attempt_count": 2,
                         "error": {"http_status": 400}}
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return recovery.RetryLedger(path, clock=lambda: NOW), path.read_bytes()


def test_new_scope_at_capacity_preserves_all_legacy_bytes(full_ledger):
    ledger, original = full_ledger
    events = []
    assert ledger.run("new-quarter", lambda: {"native": "original"}, events.append)["status"] == "ok"
    assert len(events) == 1 and events[0]["attempt_number"] == 1
    assert ledger.path.read_bytes() == original
    assert len(json.loads(original)) == 2000
    assert ledger.state("new-quarter")["attempt_count"] == 1
    assert ledger.state("old-1998")["attempt_count"] == 3
    assert recovery.RetryLedger(ledger.path).state("new-quarter") == ledger.state("new-quarter")
    assert ledger.due_keys() == ()


def test_rejected_overflow_stays_rejected_after_restart_then_explicit_reset(full_ledger):
    ledger, original = full_ledger
    events, calls = [], []
    def rejected():
        calls.append(1)
        raise recovery.ProviderRequestError(status=400, cause="invalid fixture scope")
    assert ledger.run("overflow-rejected", rejected, events.append)["status"] == "request_rejected"
    fresh = recovery.RetryLedger(ledger.path, clock=lambda: NOW)
    assert fresh.run("overflow-rejected", rejected, events.append)["attempts"] == 0
    assert fresh.run("rejected", rejected, events.append)["attempts"] == 0
    assert calls == [1]
    fresh.reset("overflow-rejected")
    assert fresh.run("overflow-rejected", lambda: {}, events.append)["attempt_number"] == 2
    assert fresh.state("overflow-rejected")["attempt_count"] == 2
    assert ledger.path.read_bytes() == original


def test_due_order_merges_pending_jobs_without_loss(full_ledger):
    ledger, _ = full_ledger
    state = json.loads(ledger.path.read_bytes())
    state["old-0"] = {"status": "retry_pending", "attempt_count": 8,
                      "next_retry_at": (NOW - timedelta(seconds=3)).isoformat()}
    ledger.path.write_text(json.dumps(state))
    original = ledger.path.read_bytes()
    ledger.defer("late", seconds=2)
    ledger._save("earlier", {"status": "retry_pending", "attempt_count": 9,
                             "next_retry_at": (NOW - timedelta(seconds=4)).isoformat()})
    ledger._save("tie-b", {"status": "retry_pending", "next_retry_at": NOW.isoformat()})
    ledger._save("tie-a", {"status": "retry_pending", "next_retry_at": NOW.isoformat()})
    assert ledger.due_keys() == ("earlier", "old-0", "tie-a", "tie-b")
    assert ledger.state("earlier")["attempt_count"] == 9
    assert ledger.state("old-0")["attempt_count"] == 8
    assert ledger.path.read_bytes() == original


def test_existing_legacy_key_updates_in_place(full_ledger):
    ledger, _ = full_ledger
    assert ledger.run("old-0", lambda: {}, lambda event: None)["attempt_number"] == 4
    assert ledger.state("old-0")["attempt_count"] == 4
    assert not ledger._job_path("old-0").exists()


def test_overflow_stays_authoritative_if_legacy_has_room(full_ledger):
    ledger, _ = full_ledger
    ledger.run("overflow", lambda: {}, lambda event: None)
    state = json.loads(ledger.path.read_bytes()); del state["old-0"]
    ledger.path.write_text(json.dumps(state)); original = ledger.path.read_bytes()
    assert ledger.run("overflow", lambda: {}, lambda event: None)["attempt_number"] == 2
    assert ledger.path.read_bytes() == original


def test_independent_instances_and_concurrent_keys_preserve_all_jobs(full_ledger):
    ledger, original = full_ledger
    def work(i):
        own = recovery.RetryLedger(ledger.path, clock=lambda: NOW)
        return own.run(f"new-{i}", lambda: {"i": i}, lambda event: None)["status"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(work, range(12))) == ["ok"] * 12
    assert all(ledger.state(f"new-{i}")["attempt_count"] == 1 for i in range(12))
    assert ledger.path.read_bytes() == original


def test_same_key_coalesces_while_original_request_is_in_flight(full_ledger):
    ledger, _ = full_ledger
    entered, release = threading.Event(), threading.Event()
    calls = []
    def request():
        calls.append(1); entered.set()
        assert release.wait(5)
        return {"native": True}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ledger.run, "same", request, lambda event: None)
        assert entered.wait(5)
        other = recovery.RetryLedger(ledger.path, clock=lambda: NOW)
        try:
            assert other.run("same", request, lambda event: None)["reason"] == "request_in_flight"
        finally:
            release.set()
        assert first.result()["status"] == "ok"
    assert calls == [1]


@pytest.mark.parametrize("raw", [b'{"schema": "bad"}', b'{}', b'{', b'x' * 65537],
                         ids=["bad-schema", "empty-envelope", "partial-json", "oversized"])
def test_damaged_finalized_job_fails_visibly(full_ledger, raw):
    ledger, _ = full_ledger
    path = ledger._job_path("bad"); path.parent.mkdir(parents=True); path.write_bytes(raw)
    with pytest.raises(ValueError):
        ledger.state("bad")
    with pytest.raises(ValueError):
        ledger.due_keys()


def test_wrong_identity_nonfinite_and_duplicate_records_fail(full_ledger):
    ledger, _ = full_ledger
    path = ledger._job_path("new"); path.parent.mkdir(parents=True)
    def write(key, state):
        path.write_text(json.dumps({"schema": "provider-retry-job-v1", "key": key, "state": state}))
    write("another", {})
    with pytest.raises(ValueError): ledger.state("new")
    write("new", {"bad": float("nan")})
    with pytest.raises(ValueError): ledger.state("new")
    path.write_text('{"schema":"provider-retry-job-v1","key":"new","state":{"bad":1e999}}')
    with pytest.raises(ValueError): ledger.state("new")
    path.unlink()
    duplicate = ledger._job_path("old-0"); duplicate.parent.mkdir(parents=True, exist_ok=True)
    duplicate.write_text(json.dumps({"schema":"provider-retry-job-v1", "key":"old-0", "state":{}}))
    with pytest.raises(ValueError): ledger.state("old-0")
    with pytest.raises(ValueError): ledger.due_keys()
    with pytest.raises(ValueError): ledger._save("old-0", {})


def test_crash_temporary_ignored_but_unknown_finalized_file_is_error(full_ledger):
    ledger, _ = full_ledger
    root = ledger.path.with_name(ledger.path.name + ".jobs"); root.mkdir()
    (root / "crash.json.tmp").write_bytes(b"partial")
    assert ledger.due_keys() == ()
    (root / "unknown.json").write_text(json.dumps({"schema":"provider-retry-job-v1", "key":"new", "state":{}}))
    with pytest.raises(ValueError): ledger.due_keys()


def test_oversized_write_and_unbounded_legacy_remain_errors(full_ledger):
    ledger, original = full_ledger
    with pytest.raises(ValueError): ledger._save("huge", {"value": "x" * 65536})
    with pytest.raises(ValueError): ledger._save("nan", {"value": float("nan")})
    assert ledger.path.read_bytes() == original
    state = json.loads(original); state["illegal-extra"] = {}
    ledger.path.write_text(json.dumps(state))
    with pytest.raises(ValueError): ledger.state("new")


def test_replace_contention_retries_sealed_bytes_without_repeating_callback(full_ledger, monkeypatch):
    ledger, original = full_ledger
    replace, replacements, callbacks = recovery.os.replace, [], []
    monkeypatch.setattr(recovery, "_replace_pause", lambda seconds: None)
    def contended(source, target):
        replacements.append(Path(source).read_bytes())
        if len(replacements) < 4:
            error = OSError("fixture sharing contention"); error.winerror = 32; raise error
        return replace(source, target)
    monkeypatch.setattr(recovery.os, "replace", contended)
    assert ledger.run("one", lambda: {}, callbacks.append)["status"] == "ok"
    assert len(callbacks) == 1 and len(replacements) == 4
    assert all(raw == replacements[0] for raw in replacements)
    assert ledger.path.read_bytes() == original


def test_terminal_replace_failure_retains_original_and_single_callback(full_ledger, monkeypatch):
    ledger, original = full_ledger
    callbacks, tries = [], []
    monkeypatch.setattr(recovery, "_replace_pause", lambda seconds: None)
    def failure(source, target):
        tries.append(1); error = OSError("fixture access denied"); error.winerror = 5; raise error
    monkeypatch.setattr(recovery.os, "replace", failure)
    with pytest.raises(OSError): ledger.run("one", lambda: {}, callbacks.append)
    assert len(callbacks) == 1 and len(tries) == 10
    assert ledger.path.read_bytes() == original
    assert ledger.state("one") == {}
    assert not list(ledger.path.parent.rglob("*.tmp"))
