"""Bounded original data-worker failures with fake services and no live work."""
from argparse import Namespace
import errno
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
from types import SimpleNamespace

import pytest

from tools import intraday_data_host as host
from tools import intraday_data_renewal as protocol


def test_custom_sqlite_attributes_cannot_replace_diagnostics():
    class UnreadableName(str):
        def startswith(self, *args):
            raise RuntimeError("PRIVATE ATTRIBUTE")
    error = OSError("PRIVATE ORIGINAL")
    error.sqlite_errorname = UnreadableName("SQLITE_FULL")
    error.sqlite_errorcode = True
    value = protocol.failure_diagnostics(error, {"operation": "service_run"})
    assert value["error_type"] == "OSError"
    assert "sqlite_errorname" not in value["exceptions"][0]
    assert "sqlite_errorcode" not in value["exceptions"][0]
    assert "PRIVATE" not in json.dumps(value)


@pytest.fixture(autouse=True)
def no_external_operations(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Offline worker diagnostics attempted an external operation")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(host.subprocess, "Popen", denied)
    monkeypatch.setattr(host.subprocess, "run", denied)


@pytest.fixture
def worker(tmp_path, monkeypatch):
    request_path = tmp_path / "candles-1363-exact-turn.json"
    request = {"token": "exact-token", "turn": "exact-turn", "role": "candles", "generation": 1363}
    original_request = (json.dumps(request, sort_keys=True) + "\n").encode()
    request_path.write_bytes(original_request)
    selected = {"generation": "a" * 64, "runtime_id": "b" * 64}
    calls, finished = [], []
    injection = {"phase": None, "error": None, "stopped": False, "planned": False}
    controls = tmp_path / "manual-session.json"
    original_controls = b'{"status":"STOPPED","original":"manual-stop"}\n'
    controls.write_bytes(original_controls)

    def fail(phase):
        calls.append(phase)
        if injection["phase"] == phase:
            raise injection["error"]

    class Renewal:
        def __init__(self):
            self.request_path = request_path
            self.request = request
            self.session = {"session_id": "data-atlas-1a:exact-session"}
            self.envelope = {"session": self.session}

        def wait_due(self):
            fail("wait_due")

        def stopped(self):
            fail("stopped_check")
            return injection["stopped"]

        def finish(self, value):
            fail("receipt_publish")
            finished.append(value)

    renewal = Renewal()

    def load(name):
        assert name == "ml.intraday.data_renewal"
        fail("service_import")

        def run(subject):
            assert subject is renewal
            fail("service_run")
            if injection["planned"]:
                raise protocol.PlannedRenewal()
            return {"status": "COMPLETED", "saved": 3}
        return SimpleNamespace(run=run)

    monkeypatch.setattr(host, "bootstrap", lambda *args, **kwargs: selected)
    monkeypatch.setattr(protocol, "Renewal", lambda path: renewal if Path(path) == request_path else pytest.fail("Wrong request"))
    monkeypatch.setattr(protocol, "birth", lambda pid: "exact-worker-birth")
    monkeypatch.setattr(host, "_record_loaded", lambda *args, **kwargs: calls.append("loaded"))
    monkeypatch.setattr(host.importlib, "import_module", load)
    options = Namespace(request=request_path, full_verify=False, verify_only=False)
    return SimpleNamespace(root=tmp_path, request_path=request_path, request=request, raw=original_request,
        selected=selected, renewal=renewal, options=options, calls=calls, finished=finished,
        injection=injection, controls=controls, controls_raw=original_controls,
        failure=request_path.with_suffix(".failure.json"))


def invoke(worker):
    return host.run_generation(worker.root / worker.selected["generation"], worker.options)


def read_failure(worker):
    return json.loads(worker.failure.read_bytes())


def assert_originals(worker):
    assert worker.request_path.read_bytes() == worker.raw
    assert worker.controls.read_bytes() == worker.controls_raw
    assert not worker.request_path.with_suffix(".response.json").exists()


def test_original_cause_has_exact_identity_and_no_messages_paths_or_locals(worker):
    cause = OSError(errno.ENOSPC, "PRIVATE MESSAGE", str(worker.root / "PRIVATE FILE"))
    cause.sqlite_errorcode = sqlite3.SQLITE_FULL
    cause.sqlite_errorname = "SQLITE_FULL"
    try:
        try:
            raise cause
        except OSError as exc:
            raise RuntimeError("PRIVATE WRAPPER") from exc
    except RuntimeError as error:
        host._record_worker_failure(worker.renewal, worker.selected, error, "service_run")
    value = read_failure(worker)
    assert value["schema_version"] == "intraday-data-worker-failure-v1"
    assert value["diagnostic_only"] is True and value["trading_authority"] is False
    assert value["protocol"] == protocol.PROTOCOL
    assert value["operation"] == "service_run"
    assert value["token"] == worker.request["token"]
    assert value["turn"] == worker.request["turn"] and value["role"] == "candles"
    assert value["generation"] == 1363
    assert value["worker_source_generation"] == worker.selected["generation"]
    assert value["session_id"] == "data-atlas-1a:exact-session"
    assert value["worker_pid"] == os.getpid() and value["worker_birth"] == "exact-worker-birth"
    assert value["request_sha256"] == hashlib.sha256(worker.raw).hexdigest()
    assert isinstance(value["at"], str) and value["at"]
    assert [item["relation"] for item in value["exceptions"]] == ["raised", "cause"]
    native = value["exceptions"][1]
    assert native["error_type"] == "OSError" and native["errno"] == errno.ENOSPC
    assert native["sqlite_errorcode"] == sqlite3.SQLITE_FULL and native["sqlite_errorname"] == "SQLITE_FULL"
    assert native["frames"]
    encoded = json.dumps(value)
    assert "PRIVATE" not in encoded and str(worker.root) not in encoded
    assert all("locals" not in item and "message" not in item for item in value["exceptions"])
    assert all(set(frame) <= {"source", "function", "line"}
        for item in value["exceptions"] for frame in item["frames"])
    assert_originals(worker)


@pytest.mark.parametrize("phase", ["wait_due", "stopped_check", "service_import", "service_run", "receipt_publish"])
def test_original_failure_propagates_with_exact_phase_without_finishing(worker, phase):
    error = ValueError("PRIVATE ORIGINAL ERROR")
    worker.injection.update(phase=phase, error=error)
    with pytest.raises(ValueError) as captured:
        invoke(worker)
    assert captured.value is error
    value = read_failure(worker)
    assert value["operation"] == phase
    assert value["error_type"] == "ValueError"
    assert worker.finished == []
    assert worker.calls.count(phase) == 1
    assert "PRIVATE" not in json.dumps(value)
    assert_originals(worker)


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit, InterruptedError])
def test_non_planned_base_exceptions_keep_original_raise(worker, kind):
    error = kind("PRIVATE ORIGINAL ERROR")
    worker.injection.update(phase="service_run", error=error)
    with pytest.raises(kind) as captured:
        invoke(worker)
    assert captured.value is error
    assert read_failure(worker)["error_type"] == kind.__name__
    assert worker.finished == []
    assert_originals(worker)


@pytest.mark.parametrize("failure_mode", ["existing", "directory", "fsync"])
def test_diagnostic_storage_failure_never_replaces_original_exception(worker, monkeypatch, failure_mode):
    original = b"original failure evidence\x00"
    if failure_mode == "existing":
        worker.failure.write_bytes(original)
    elif failure_mode == "directory":
        worker.failure.mkdir()
    else:
        def denied(*args):
            raise OSError(errno.ENOSPC, "PRIVATE DIAGNOSTIC STORAGE ERROR")
        monkeypatch.setattr(host.os, "fsync", denied)
    error = RuntimeError("PRIVATE ORIGINAL ERROR")
    worker.injection.update(phase="service_run", error=error)
    with pytest.raises(RuntimeError) as captured:
        invoke(worker)
    assert captured.value is error
    if failure_mode == "existing":
        assert worker.failure.read_bytes() == original
    elif failure_mode == "directory":
        assert worker.failure.is_dir()
    assert worker.finished == []
    assert_originals(worker)


def test_planned_completion_and_existing_stop_keep_original_paths(worker):
    worker.injection["planned"] = True
    assert invoke(worker) == protocol.PLANNED_EXIT
    assert worker.finished == [] and not worker.failure.exists()
    worker.injection["planned"] = False
    assert invoke(worker) == 0
    assert worker.finished == [{"status": "COMPLETED", "saved": 3}]
    assert not worker.failure.exists()
    worker.finished.clear()
    worker.calls.clear()
    worker.injection["stopped"] = True
    assert invoke(worker) == 1
    assert worker.calls == ["loaded", "wait_due", "stopped_check"]
    assert worker.finished == [] and not worker.failure.exists()
    assert_originals(worker)


def test_optional_loaded_diagnostic_failure_does_not_become_worker_failure(worker, monkeypatch):
    def unavailable(*args, **kwargs):
        raise PermissionError(errno.EACCES, "PRIVATE DIAGNOSTIC ERROR")
    monkeypatch.setattr(host, "_record_loaded", unavailable)
    assert invoke(worker) == 0
    assert worker.finished == [{"status": "COMPLETED", "saved": 3}]
    assert not worker.failure.exists()
    assert_originals(worker)


def synthetic_controller(tmp_path, monkeypatch, *, code, mode="missing", next_command_error=False):
    calls = []
    directory = tmp_path / "state/intraday-data-renewal/synthetic"
    envelope = {"lifetime_id": "synthetic", "datastore": str(tmp_path),
        "session": {"session_id": "data-atlas-1a:synthetic"}}
    controls = tmp_path / "manual-session.json"
    controls.write_bytes(b"original independent 1B manual stop")

    class Child:
        def __init__(self, argv, **kwargs):
            role, path = argv
            request = json.loads(Path(path).read_bytes())
            self.pid = 70000 + len(calls)
            calls.append((role, request["generation"]))
            self.returncode = 0 if role == "initialize" else code
            value = {"protocol": protocol.PROTOCOL, "token": request["token"],
                "turn": request["turn"], "role": role, "worker_pid": self.pid}
            if role == "initialize":
                session = {**envelope["session"], "actor": "Atlas", "symbols": ["SYNTH"],
                    "config": {"symbol_workers": 1}, "started_at": "2026-10-29T10:55:00+00:00",
                    "close_at": "2026-10-30T00:00:00+00:00"}
                value.update(disposition="COMPLETED", result={"session": session, "roles": ["candles"]})
                protocol.atomic_json(request["response"], value)
            elif mode == "corrupt":
                Path(request["response"]).write_bytes(b"{corrupt response")
            elif mode == "renew":
                value.update(disposition="PLANNED_RENEWAL", state={"count": 1}, next_poll_at=None)
                protocol.atomic_json(request["response"], value)

        def poll(self):
            return self.returncode

        def wait(self):
            return self.returncode

        def terminate(self):
            raise AssertionError("Completed fake child must never be terminated")

    def command(role, request):
        if next_command_error and role == "candles" and calls == [("initialize", 1), ("candles", 1)]:
            raise ValueError("PRIVATE NEXT TURN COMMAND ERROR")
        return [role, str(request)]

    monkeypatch.setattr(protocol.subprocess, "Popen", Child)
    monkeypatch.setattr(protocol, "birth", lambda pid: "synthetic-birth")
    return directory, envelope, command, calls, controls


def controller_failures(tmp_path, directory):
    records = [json.loads(path.read_bytes()) for path in (
        directory / "outcome.json", tmp_path / "state/intraday-data-renewal/active.json",
        tmp_path / "intraday/system-1a/status.json")]
    assert records[0]["disposition"] == records[1]["disposition"] == "FAILED"
    assert records[2]["status"] == "FAILED"
    failures = [value["failure"] for value in records]
    assert failures[0] == failures[1] == failures[2]
    return failures[0]


@pytest.mark.parametrize("code", [-9, 0, 1, 3221225477])
def test_missing_receipt_retains_exact_child_exit_code_without_successor(tmp_path, monkeypatch, code):
    directory, envelope, command, calls, controls = synthetic_controller(tmp_path, monkeypatch, code=code)
    assert protocol.run_protocol(directory=directory, envelope=envelope, command=command, poll_seconds=0) == 1
    failure = controller_failures(tmp_path, directory)
    assert failure["worker_exit_code"] == code and type(failure["worker_exit_code"]) is int
    assert failure["role"] == "candles" and failure["generation"] == 1
    assert failure["operation"] == "worker_receipt_read"
    assert failure["error_type"] == "FileNotFoundError"
    assert calls == [("initialize", 1), ("candles", 1)]
    assert controls.read_bytes() == b"original independent 1B manual stop"


def test_corrupt_response_retains_exact_exit_code_without_successor(tmp_path, monkeypatch):
    directory, envelope, command, calls, controls = synthetic_controller(tmp_path, monkeypatch, code=75, mode="corrupt")
    assert protocol.run_protocol(directory=directory, envelope=envelope, command=command, poll_seconds=0) == 1
    failure = controller_failures(tmp_path, directory)
    assert failure["worker_exit_code"] == 75 and failure["operation"] == "worker_receipt_decode"
    assert failure["error_type"] == "JSONDecodeError"
    assert calls == [("initialize", 1), ("candles", 1)]
    assert controls.read_bytes() == b"original independent 1B manual stop"


@pytest.mark.parametrize("event,operation", [("exited", "worker_exit_callback"), ("handoff", "worker_handoff_callback")])
def test_worker_callback_failure_retains_that_turn_exit_code(tmp_path, monkeypatch, event, operation):
    directory, envelope, command, calls, controls = synthetic_controller(tmp_path, monkeypatch, code=75, mode="renew")
    def phase(kind, role):
        if (kind, role) == (event, "candles"):
            raise RuntimeError("PRIVATE CALLBACK ERROR")
    assert protocol.run_protocol(directory=directory, envelope=envelope, command=command, poll_seconds=0, phase=phase) == 1
    failure = controller_failures(tmp_path, directory)
    assert failure["worker_exit_code"] == 75 and failure["operation"] == operation
    assert failure["error_type"] == "RuntimeError" and failure["role"] == "candles"
    assert calls == [("initialize", 1), ("candles", 1)]
    assert "PRIVATE" not in json.dumps(failure)
    assert controls.read_bytes() == b"original independent 1B manual stop"


def test_successful_turn_does_not_leak_exit_code_into_next_turn_failure(tmp_path, monkeypatch):
    directory, envelope, command, calls, controls = synthetic_controller(tmp_path, monkeypatch,
        code=75, mode="renew", next_command_error=True)
    assert protocol.run_protocol(directory=directory, envelope=envelope, command=command, poll_seconds=0) == 1
    failure = controller_failures(tmp_path, directory)
    assert failure["operation"] == "worker_command_select" and failure["generation"] == 2
    assert "worker_exit_code" not in failure
    assert calls == [("initialize", 1), ("candles", 1)]
    assert controls.read_bytes() == b"original independent 1B manual stop"
