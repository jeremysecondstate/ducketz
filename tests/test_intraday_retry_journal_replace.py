"""Offline retry-journal publication; all files and provider callbacks are fixtures."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import ctypes
import errno
import json
import os
import threading
from types import SimpleNamespace

import pytest

from ml.intraday import provider_recovery as recovery


NOW = datetime(2026, 10, 26, 12, 18, 45, tzinfo=timezone.utc)


def _windows_failure(code):
    failure = PermissionError(errno.EACCES, "fixture Windows replacement denial")
    failure.winerror = code
    return failure


@pytest.mark.parametrize("code", [5, 32, 33])
@pytest.mark.parametrize("failures", [1, 9])
def test_transient_windows_replace_reuses_exact_temp_without_repeating_request(tmp_path, monkeypatch, code, failures):
    path = tmp_path / "retry.json"
    path.write_text(json.dumps({"unrelated": {"status": "request_rejected", "attempt_count": 4}}))
    ledger = recovery.RetryLedger(path, clock=lambda: NOW)
    real_replace = recovery.os.replace
    attempts, calls, events, sleeps = [], [], [], []

    def replace(source, destination):
        attempts.append((str(source), source.read_bytes()))
        assert destination == path
        assert json.loads(path.read_text())["unrelated"]["attempt_count"] == 4
        if len(attempts) <= failures:
            raise _windows_failure(code)
        real_replace(source, destination)

    monkeypatch.setattr(recovery.os, "replace", replace)
    monkeypatch.setattr(recovery, "_replace_pause", sleeps.append, raising=False)
    result = ledger.run("job", lambda: calls.append("request") or {"native": "original"}, events.append, max_attempts=1)
    assert result["status"] == "ok" and result["attempt_number"] == 1
    assert calls == ["request"] and len(events) == 1
    assert events[0]["payload"] == {"native": "original"}
    assert len(attempts) == failures + 1
    assert len(set(attempts)) == 1
    assert sleeps == [.02] * failures
    saved = json.loads(path.read_text())
    assert saved["job"]["attempt_count"] == 1 and saved["unrelated"]["attempt_count"] == 4
    assert not list(tmp_path.glob("*.tmp"))


def test_exhausted_windows_denial_preserves_original_and_attempt_evidence(tmp_path, monkeypatch):
    path = tmp_path / "retry.json"
    original = b'{"existing":{"status":"ok","attempt_count":7}}'
    path.write_bytes(original)
    ledger = recovery.RetryLedger(path, clock=lambda: NOW)
    calls, events, attempts, sleeps = [], [], [], []
    failure = _windows_failure(5)

    def denied(source, destination):
        attempts.append(source.read_bytes())
        raise failure

    monkeypatch.setattr(recovery.os, "replace", denied)
    monkeypatch.setattr(recovery, "_replace_pause", sleeps.append, raising=False)
    with pytest.raises(PermissionError) as caught:
        ledger.run("job", lambda: calls.append("request") or "original", events.append, max_attempts=2)
    assert caught.value is failure
    assert calls == ["request"] and len(events) == 1
    assert len(attempts) == 10 and len(set(attempts)) == 1
    assert sleeps == [.02] * 9 and sum(sleeps) < .181
    assert path.read_bytes() == original and not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("code", [None, 2, 16, 112])
def test_nonretryable_filesystem_error_is_not_retried(tmp_path, monkeypatch, code):
    ledger = recovery.RetryLedger(tmp_path / "retry.json", clock=lambda: NOW)
    calls, sleeps = [], []
    failure = OSError(errno.EACCES if code is None else errno.ENOSPC, "fixture permanent filesystem error")
    if code is not None:
        failure.winerror = code

    def denied(source, destination):
        calls.append(True)
        raise failure

    monkeypatch.setattr(recovery.os, "replace", denied)
    monkeypatch.setattr(recovery, "_replace_pause", sleeps.append, raising=False)
    with pytest.raises(OSError) as caught:
        ledger.defer("job")
    assert caught.value is failure and calls == [True] and sleeps == []
    assert not list(tmp_path.glob("*.tmp"))


def test_retry_does_not_release_merge_lock_or_lose_another_writer(tmp_path, monkeypatch):
    path = tmp_path / "retry.json"
    first, second = (recovery.RetryLedger(path, clock=lambda: NOW) for _ in range(2))
    real_replace = recovery.os.replace
    denied, contender_started = threading.Event(), threading.Event()
    replacements, sleeps = [], []

    def replace(source, destination):
        replacements.append(json.loads(source.read_text()))
        if len(replacements) == 1:
            denied.set()
            assert contender_started.wait(3)
            raise _windows_failure(32)
        real_replace(source, destination)

    def pause(seconds):
        sleeps.append(seconds)
        # The second writer has started, but cannot merge until this exact
        # temporary has replaced the old journal under the original path lock.
        assert not path.exists()

    def contender():
        assert denied.wait(3)
        contender_started.set()
        second.defer("second")

    monkeypatch.setattr(recovery.os, "replace", replace)
    monkeypatch.setattr(recovery, "_replace_pause", pause, raising=False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first.defer, "first")
        b = pool.submit(contender)
        a.result(timeout=5)
        b.result(timeout=5)
    assert set(json.loads(path.read_text())) == {"first", "second"}
    assert replacements[0] == replacements[1]
    assert sleeps == [.02] and not list(tmp_path.glob("*.tmp"))


def test_provider_error_callback_and_retry_after_survive_replace_retry(tmp_path, monkeypatch):
    ledger = recovery.RetryLedger(tmp_path / "retry.json", clock=lambda: NOW)
    real_replace = recovery.os.replace
    calls, events, replacements = [], [], []

    def request():
        calls.append(True)
        raise recovery.ProviderRequestError(status=429, retry_after="30", cause="fixture rate limit")

    def replace(source, destination):
        replacements.append(source.read_bytes())
        if len(replacements) == 1:
            raise _windows_failure(33)
        real_replace(source, destination)

    monkeypatch.setattr(recovery.os, "replace", replace)
    monkeypatch.setattr(recovery, "_replace_pause", lambda seconds: None, raising=False)
    result = ledger.run("job", request, events.append, max_attempts=1)
    assert result["status"] == "retry_pending" and result["attempts"] == 1
    assert calls == [True] and len(events) == 1
    assert ledger.state("job")["next_retry_at"] == "2026-10-26T12:19:15+00:00"
    assert len(replacements) == 2 and replacements[0] == replacements[1]


@pytest.mark.skipif(os.name != "nt", reason="Real Windows deny-delete sharing fixture")
def test_real_windows_open_reader_denies_original_then_exact_retry_succeeds(tmp_path, monkeypatch):
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    path = tmp_path / "retry.json"
    original = b'{"older":{"status":"ok","attempt_count":2}}'
    path.write_bytes(original)
    # Generic read, share read/write but deliberately omit FILE_SHARE_DELETE.
    handle = kernel.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
    assert handle != ctypes.c_void_p(-1).value
    real_replace = recovery.os.replace
    denied = []

    def replace(source, destination):
        try:
            real_replace(source, destination)
        except OSError as error:
            denied.append(error.winerror)
            assert error.winerror in {5, 32, 33}
            assert path.read_bytes() == original and source.is_file()
            raise

    def release(seconds):
        nonlocal handle
        assert seconds == .02 and denied
        assert kernel.CloseHandle(handle)
        handle = None

    monkeypatch.setattr(recovery.os, "replace", replace)
    monkeypatch.setattr(recovery, "_replace_pause", release, raising=False)
    try:
        recovery.RetryLedger(path, clock=lambda: NOW).defer("new")
    finally:
        if handle is not None:
            assert kernel.CloseHandle(handle)
    assert len(denied) == 1
    assert set(json.loads(path.read_text())) == {"older", "new"}
    assert not list(tmp_path.glob("*.tmp"))


def test_cleanup_error_does_not_replace_original_publication_failure(tmp_path, monkeypatch):
    path = tmp_path / "retry.json"
    failure = _windows_failure(5)
    real_unlink = type(path).unlink

    def denied(source, destination):
        raise failure

    def cleanup_denied(candidate, *args, **kwargs):
        if candidate.suffix == ".tmp":
            raise OSError(errno.EACCES, "fixture cleanup denied")
        return real_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr(recovery.os, "replace", denied)
    monkeypatch.setattr(recovery, "_replace_pause", lambda seconds: None, raising=False)
    monkeypatch.setattr(type(path), "unlink", cleanup_denied)
    with pytest.raises(PermissionError) as caught:
        recovery.RetryLedger(path, clock=lambda: NOW).defer("job")
    assert caught.value is failure
    assert any("temporary cleanup" in note for note in failure.__notes__)
    assert len(list(tmp_path.glob("*.tmp"))) == 1


# These standalone API cases retain the original retry ledger's provider and
# manual-stop contracts while its previously published module joins main.
@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_only_explicit_transient_http_retries(status):
    assert recovery.safe_failure(recovery.ProviderRequestError(status=status), NOW)["transient"]


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 405, 422, 501])
def test_rejected_requests_keep_original_classification(status):
    error = recovery.safe_failure(recovery.ProviderRequestError(status=status), NOW)
    assert error["category"] == "request_rejected" and not error["transient"]
    assert error["http_status"] == status


def test_retry_after_and_redaction_preserved():
    assert recovery.retry_after_seconds("Mon, 26 Oct 2026 12:19:45 GMT", NOW) == 60
    error = recovery.ProviderRequestError(status=429,
        cause="apikey=fixtureprivate Bearer fixturetoken https://example.test/?apikey=fixtureprivate", retry_after="7")
    detail = recovery.safe_failure(error, NOW, secrets=("fixtureprivate",))
    assert detail["retry_after_seconds"] == 7
    assert all(value not in detail["cause"] for value in ["fixtureprivate", "example.test", "fixturetoken"])


def test_pending_job_survives_restart_and_keeps_attempt_numbers(tmp_path):
    now, attempts, waits, events = [NOW], [], [], []

    def wait(seconds):
        waits.append(seconds)
        now[0] += timedelta(seconds=seconds)

    def failed():
        attempts.append(now[0])
        raise TimeoutError("fixture temporary request")

    path = tmp_path / "state.json"
    first = recovery.RetryLedger(path, clock=lambda: now[0], wait=wait)
    assert first.run("job", failed, events.append)["status"] == "retry_pending"
    assert len(attempts) == 2 and waits == [2]
    second = recovery.RetryLedger(path, clock=lambda: now[0], wait=wait)
    assert second.run("job", lambda: [], events.append)["payload"] == []
    assert waits == [2, 2] and events[-1]["attempt_number"] == 3
    assert second.state("job")["status"] == "ok"


def test_deadline_defers_without_waiting_past_provider_budget(tmp_path):
    events, waits = [], []
    ledger = recovery.RetryLedger(tmp_path / "state.json", clock=lambda: NOW, wait=waits.append)

    def failed():
        raise recovery.ProviderRequestError(status=429, retry_after="300")

    result = ledger.run("job", failed, events.append, deadline=NOW + timedelta(seconds=20))
    assert result["status"] == "deferred" and len(events) == 1 and not waits
    assert ledger.state("job")["next_retry_at"] == (NOW + timedelta(seconds=300)).isoformat()


def test_manual_stop_interrupts_provider_wait_and_preserves_pending(tmp_path):
    stop, events = threading.Event(), []

    def wait(seconds):
        assert seconds == 2
        stop.set()
        return True

    ledger = recovery.RetryLedger(tmp_path / "state.json", clock=lambda: NOW, wait=wait)

    def failed():
        raise TimeoutError("fixture temporary")

    result = ledger.run("job", failed, events.append, stop_event=stop)
    assert result["status"] == "stopped" and len(events) == 1
    assert ledger.state("job")["status"] == "retry_pending"
    assert ledger.run("other", lambda: pytest.fail("manual stop ignored"), events.append, stop_event=stop)["status"] == "stopped"


def test_permanent_rejection_is_not_replayed_and_explicit_reset_preserves_count(tmp_path):
    events = []
    ledger = recovery.RetryLedger(tmp_path / "state.json", clock=lambda: NOW)

    def failed():
        raise recovery.ProviderRequestError(status=404)

    assert ledger.run("job", failed, events.append)["status"] == "request_rejected"
    assert ledger.run("job", lambda: pytest.fail("permanent rejection retried"), events.append)["attempts"] == 0
    ledger.reset("job")
    assert ledger.run("job", lambda: "changed scope", events.append)["attempt_number"] == 2
    assert len(events) == 2


def test_same_job_coalesces_without_blocking_other_jobs(tmp_path):
    path = tmp_path / "state.json"
    first, second = (recovery.RetryLedger(path, clock=lambda: NOW) for _ in range(2))
    entered, release = threading.Event(), threading.Event()
    events = []

    def request():
        entered.set()
        assert release.wait(3)
        return "original"

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(first.run, "same", request, events.append, max_attempts=1)
        assert entered.wait(3)
        try:
            assert second.run("same", lambda: pytest.fail("duplicate request"), events.append, max_attempts=1)["reason"] == "request_in_flight"
            assert second.run("different", lambda: "independent", events.append, max_attempts=1)["status"] == "ok"
        finally:
            release.set()
        assert running.result(timeout=3)["payload"] == "original"
    assert set(json.loads(path.read_text())) == {"same", "different"} and len(events) == 2


def test_fmp_injected_transport_preserves_safe_error_and_never_uses_network():
    provider = SimpleNamespace(_validate_config=lambda: None, base_url="https://example.test", api_key="fixtureprivate", timeout_seconds=10)

    def get(url, **kwargs):
        assert url == "https://example.test/economic-indicators"
        assert kwargs["params"] == {"name": "GDP", "apikey": "fixtureprivate"}
        return SimpleNamespace(status_code=503, headers={"Retry-After": "4"}, json=lambda: {"error": "apikey=fixtureprivate"})

    with pytest.raises(recovery.ProviderRequestError) as caught:
        recovery.fmp_request(provider, "economic-indicators", {"name": "GDP"}, http_get=get)
    detail = recovery.safe_failure(caught.value, NOW)
    assert detail["transient"] and detail["retry_after_seconds"] == 4
    assert "fixtureprivate" not in str(caught.value) and "fixtureprivate" not in json.dumps(detail)
