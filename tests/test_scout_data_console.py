"""Offline 1A start/display tests. All tasks and service files are fixtures."""
from pathlib import Path
import hashlib
import json
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest

from tools import scout_data_console as candidate

REAL_SET_TITLE = candidate.set_console_title


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Data console fixture reached network")
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(candidate, "set_console_title", lambda: None)


def actions(arguments='-B "fixture-data-launch.py" --data-live'):
    return [{"execute": "C:/fixture/pythonw.exe", "arguments": arguments}]


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_default_once_is_read_only_and_does_not_replay_historical_error(tmp_path, monkeypatch, capsys):
    save(tmp_path / "data-status.json", {"status": "RUNNING", "pid": 123,
        "heartbeat_at": "2026-10-28T17:00:00Z", "release": {"manifest_sha256": "fixture"}})
    (tmp_path / "data-metrics.jsonl").write_text(json.dumps({"recorded_at": "2026-10-28T16:59:00Z", "phase": "fixture"}) + "\n")
    (tmp_path / "data.stderr.log").write_text("historical traceback must not look current\n")
    original = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    def forbidden(*args, **kwargs):
        raise AssertionError("Display-only capture reached task control")
    monkeypatch.setattr(candidate, "task_operation", forbidden)
    monkeypatch.setattr(candidate, "start_existing_task", forbidden)
    assert candidate.main(["--root", str(tmp_path), "--once"]) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0]["mode"] == "display only" and rows[0]["closing_viewer_stops_service"] is False
    status = next(row for row in rows if row["event"] == "system_1a_status")
    assert status["pid"] == 123 and status["service_heartbeat_at"] == "2026-10-28T17:00:00Z"
    assert status["recorded_loaded_release"]["manifest_sha256"] == "fixture"
    metric = next(row for row in rows if row["event"] == "system_1a_metric")
    assert metric["historical"] is True and metric["recorded_at"] == "2026-10-28T16:59:00Z"
    assert not any(row["event"] == "system_1a_output" for row in rows)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == original


@pytest.mark.parametrize("state,expected_calls,status", [("Running", [False], "already_running"),
    ("Ready", [False, True], "start_dispatched")])
def test_explicit_start_inspects_then_dispatches_at_most_once(state, expected_calls, status):
    calls = []
    def native(task, *, start):
        calls.append(start)
        return {"task_name": task, "enabled": True, "state": state, "multiple_instances": 2, "actions": actions(),
                "status": "start_dispatched" if start else "inspected"}
    result = candidate.start_existing_task("fixture data task", operation=native)
    assert calls == expected_calls and result["status"] == status


@pytest.mark.parametrize("patch", [{"enabled": False}, {"state": "Disabled"}, {"multiple_instances": 0}])
def test_disabled_or_duplicate_policy_task_never_dispatches(patch):
    calls = []
    def native(task, *, start):
        calls.append(start)
        return {"task_name": task, "enabled": True, "state": "Ready", "multiple_instances": 2, "actions": actions(), **patch}
    with pytest.raises(ValueError):
        candidate.start_existing_task("fixture", operation=native)
    assert calls == [False]


def test_task_changed_to_running_during_dispatch_is_not_duplicated():
    calls = []
    def native(task, *, start):
        calls.append(start)
        return {"task_name": task, "enabled": True, "state": "Running" if start else "Ready",
                "multiple_instances": 2, "actions": actions(), "status": "already_running" if start else "inspected"}
    assert candidate.start_existing_task("fixture", operation=native)["status"] == "already_running"
    assert calls == [False, True]


def test_native_operation_has_bounded_timeout_exact_identity_and_guarded_dispatch(monkeypatch):
    monkeypatch.setattr(candidate, "os", SimpleNamespace(name="nt", environ={"SystemRoot": "C:/Windows"}))
    calls = []
    task_name = 'fixture "quotes" $literal'
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"task_name": task_name,
            "state": "Ready", "enabled": True, "multiple_instances": 2, "actions": actions(), "status": "start_dispatched"}), stderr="")
    assert candidate.task_operation(task_name, start=True, runner=runner)["status"] == "start_dispatched"
    command, kwargs = calls[0]
    assert command[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert task_name not in command[-1] and kwargs["env"]["SCOUT_DATA_TASK_NAME"] == task_name
    assert kwargs["env"]["SCOUT_DATA_TASK_START"] == "1" and kwargs["timeout"] == 15
    assert "Start-ScheduledTask -InputObject $t" in command[-1]
    assert "if($state -eq 'Running')" in command[-1] and "if($policy -ne 2)" in command[-1]
    assert "Enable-ScheduledTask" not in command[-1] and "Stop-ScheduledTask" not in command[-1]
    assert len(calls) == 1


@pytest.mark.parametrize("output", ["invalid JSON", json.dumps({"task_name": "wrong identity"}), "x" * 70000],
                         ids=["malformed", "changed-identity", "oversize"])
def test_invalid_native_response_cannot_look_like_success(monkeypatch, output):
    monkeypatch.setattr(candidate, "os", SimpleNamespace(name="nt", environ={}))
    with pytest.raises(ValueError):
        candidate.task_operation("fixture", runner=lambda *args, **kwargs:
            SimpleNamespace(returncode=0, stdout=output, stderr=""))


def test_native_failure_is_reported_once_without_retry(monkeypatch, tmp_path, capsys):
    calls = []
    def native(*args, **kwargs):
        calls.append(True)
        raise RuntimeError("fixture Access denied")
    monkeypatch.setattr(candidate, "task_operation", native)
    assert candidate.main(["--root", str(tmp_path), "--start", "--task-name", "fixture", "--once"]) == 1
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[-1]["event"] == "system_1a_start_failed" and rows[-1]["automatic_retry"] is False
    assert len(calls) == 1


def test_new_output_retains_utf8_across_reads_and_detects_truncation(tmp_path):
    path = tmp_path / "data.stderr.log"
    path.write_bytes(b"old error\n")
    tail = candidate.FileTail(path)
    assert tail.read()[0] == []
    raw = "new \u03b1 event\n".encode()
    with path.open("ab") as stream:
        stream.write(raw[:5])  # Split inside the two-byte alpha character.
    assert tail.read()[0] == []
    with path.open("ab") as stream:
        stream.write(raw[5:])
    assert tail.read()[0] == [("new \u03b1 event", False)]
    path.write_bytes(b"fresh\n")
    lines, notices = tail.read()
    assert lines == [("fresh", False)] and "replaced/truncated" in notices[0]


def test_metric_initial_tail_and_each_read_are_bounded(tmp_path):
    path = tmp_path / "data-metrics.jsonl"
    original = b"ignored prefix " * 10000 + b"\n" + json.dumps({"recorded_at": "saved time", "phase": "recent"}).encode() + b"\n"
    path.write_bytes(original)
    tail = candidate.FileTail(path, recent=True)
    assert tail.offset == len(original) - candidate.INITIAL_METRICS
    lines, _ = tail.read()
    assert len(lines) == 1 and json.loads(lines[0][0])["recorded_at"] == "saved time" and lines[0][1] is True
    before = tail.offset
    with path.open("ab") as stream:
        stream.write(b"x" * 100000)
    _, notices = tail.read()
    assert tail.offset - before == candidate.MAX_READ and len(tail.pending) <= candidate.MAX_LINE
    assert "oversize line" in notices[0] and path.read_bytes().startswith(original)


def test_metric_arrival_displays_immediately_with_original_recorded_time(tmp_path):
    save(tmp_path / "data-status.json", {"status": "RUNNING", "heartbeat_at": "old time"})
    rows, elapsed = [], [0.0]
    viewer = candidate.DataConsole(tmp_path, sink=rows.append, monotonic=lambda: elapsed[0])
    viewer.poll()
    rows.clear()
    (tmp_path / "data-metrics.jsonl").write_text(json.dumps({"recorded_at": "actual time", "phase": "preparation"}) + "\n")
    viewer.poll()
    metric = next(row for row in rows if row["event"] == "system_1a_metric")
    assert metric["recorded_at"] == "actual time" and metric["historical"] is False
    assert not any(row["event"] == "system_1a_status" for row in rows)


def test_status_missing_transient_invalid_and_recovery_are_truthful(tmp_path):
    rows, elapsed = [], [0.0]
    viewer = candidate.DataConsole(tmp_path, sink=rows.append, monotonic=lambda: elapsed[0])
    viewer.poll()
    assert rows[0]["event"] == "system_1a_status_unavailable"
    (tmp_path / "data-status.json").write_text('{"status":')
    viewer.poll()
    assert rows[-1]["event"] == "system_1a_status_unavailable" and rows[-1]["reason"] == "JSONDecodeError"
    save(tmp_path / "data-status.json", {"status": "RUNNING", "heartbeat_at": "saved", "pid": 42})
    viewer.poll()
    assert rows[-1]["status"] == "RUNNING" and rows[-1]["service_heartbeat_at"] == "saved"
    assert "liveness not inferred" in rows[-1]["evidence"]
    count = len(rows)
    viewer.poll()
    assert len(rows) == count
    elapsed[0] = 30
    viewer.poll()
    assert len(rows) == count + 1 and rows[-1]["service_heartbeat_at"] == "saved"
    (tmp_path / "data-status.json").write_bytes(b" " * 65537)
    viewer.poll()
    assert rows[-1]["event"] == "system_1a_status_unavailable" and rows[-1]["reason"] == "ValueError"


def test_oversize_and_invalid_metric_lines_do_not_change_originals(tmp_path):
    path = tmp_path / "data-metrics.jsonl"
    path.write_bytes(b"")
    rows = []
    viewer = candidate.DataConsole(tmp_path, sink=rows.append)
    original = b"x" * 20000 + b"\nnot json\n" + json.dumps({"recorded_at": "real", "phase": "fixture"}).encode() + b"\n"
    path.write_bytes(original)
    viewer.poll()
    assert any(row["event"] == "system_1a_metric_unreadable" for row in rows)
    assert any(row["event"] == "system_1a_display_notice" and "oversize line" in str(row) for row in rows)
    assert any(row["event"] == "system_1a_metric" for row in rows)
    assert path.read_bytes() == original


def test_viewer_ctrl_c_never_stops_service_or_contacts_task_api(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Viewer closure reached task controls")
    def close_viewer(_): raise KeyboardInterrupt()
    monkeypatch.setattr(candidate, "task_operation", forbidden)
    monkeypatch.setattr(candidate, "start_existing_task", forbidden)
    monkeypatch.setattr(candidate.time, "sleep", close_viewer)
    assert candidate.main(["--root", str(tmp_path)]) == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("arguments", ["--live", "--data-live --live", "--check"])
def test_wrong_or_trading_task_action_is_never_dispatched(arguments):
    calls = []
    def native(task, *, start):
        calls.append(start)
        return {"task_name": task, "enabled": True, "state": "Ready", "multiple_instances": 2,
                "actions": actions(arguments)}
    with pytest.raises(ValueError, match="not data-only"):
        candidate.start_existing_task("fixture", operation=native)
    assert calls == [False]


def test_transient_log_open_error_is_a_notice_and_recovers(tmp_path, monkeypatch):
    path = tmp_path / "data.stdout.log"
    original = Path.open
    fail = [True]
    def opening(self, *args, **kwargs):
        if self == path and fail[0]:
            raise PermissionError("fixture transient access")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opening)
    tail = candidate.FileTail(path)
    assert tail.read()[1] == ["read unavailable: PermissionError"]
    fail[0] = False
    path.write_bytes(b"new after temporary failure\n")
    assert tail.read()[0] == []  # Unknown earlier bytes cannot be relabeled current.
    with path.open("ab") as stream:
        stream.write(b"new after attachment\n")
    assert tail.read()[0] == [("new after attachment", False)]


def test_status_heartbeat_alone_does_not_replay_saved_error_every_second(tmp_path):
    path = tmp_path / "data-status.json"
    status = {"status": "RUNNING", "heartbeat_at": "first", "errors": [{"error_type": "saved failure"}]}
    save(path, status)
    rows, elapsed = [], [0.0]
    viewer = candidate.DataConsole(tmp_path, sink=rows.append, monotonic=lambda: elapsed[0])
    viewer.poll()
    first = next(row for row in rows if row["event"] == "system_1a_status")
    assert first["error_count"] == 1 and first["latest_saved_error"] == status["errors"][0]
    rows.clear()
    status["heartbeat_at"] = "new service heartbeat"
    save(path, status)
    elapsed[0] = 1
    viewer.poll()
    assert rows == []
    elapsed[0] = 30
    viewer.poll()
    assert rows[-1]["service_heartbeat_at"] == "new service heartbeat"


def test_start_requires_explicit_local_task_binding(tmp_path):
    with pytest.raises(SystemExit) as caught:
        candidate.main(["--root", str(tmp_path), "--start", "--once"])
    assert caught.value.code == 2



def test_delayed_attachment_never_relabels_preexisting_stderr_as_new(tmp_path, monkeypatch):
    path = tmp_path / "data.stderr.log"
    path.write_bytes(b"old historical exception\n")
    original, denied = Path.open, [True]
    def opening(self, *args, **kwargs):
        if self == path and denied[0]:
            raise PermissionError("fixture initial access")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", opening)
    tail = candidate.FileTail(path)
    denied[0] = False
    lines, notices = tail.read()
    assert lines == [] and "delayed attachment" in notices[0]
    with path.open("ab") as stream:
        stream.write(b"actually new exception\n")
    assert tail.read()[0] == [("actually new exception", False)]


@pytest.mark.parametrize("available", [True, False])
def test_windows_console_title_is_explicit_and_best_effort(monkeypatch, available):
    calls = []
    class Setter:
        def __call__(self, title):
            calls.append(title)
            if not available:
                raise OSError("No attached console")
    setter = Setter()
    fake = SimpleNamespace(windll=SimpleNamespace(kernel32=SimpleNamespace(SetConsoleTitleW=setter)),
                           c_wchar_p=object(), c_int=object())
    monkeypatch.setattr(candidate, "os", SimpleNamespace(name="nt"))
    monkeypatch.setitem(sys.modules, "ctypes", fake)
    REAL_SET_TITLE()
    assert calls == ["Scout 15m 1A - Data and Preparation"]
    assert setter.argtypes == [fake.c_wchar_p] and setter.restype == fake.c_int
