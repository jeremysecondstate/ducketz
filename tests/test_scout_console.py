"""Offline console fixtures: log evidence and lifecycle stay independent of display."""
from pathlib import Path
import io
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from tools import scout_service_controller as candidate
from test_scout_service_controller import FakeWorker, Harness, selection


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Console fixture reached network")
    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class RecordingConsole:
    def __init__(self):
        self.lines, self.closed = [], False
    def emit(self, text):
        self.lines.append(text)
    def close(self):
        self.closed = True


def test_combined_stdout_stderr_partial_utf8_and_final_fragment_have_one_cursor(tmp_path):
    path = tmp_path / "trading.log"
    console = RecordingConsole()
    tail = candidate.LogTail(path, "[trading g1]")
    tail.drain(console)  # The suspended worker has not created its file yet.
    raw = 'stdout JSON {"event":"fixture"}\n\u03b1'.encode()
    path.write_bytes(raw[:-1])
    tail.drain(console)
    assert console.lines == ['[trading g1] stdout JSON {"event":"fixture"}']
    with path.open("ab") as stream:
        stream.write(raw[-1:] + b" stderr traceback\nlast without newline")
    tail.drain(console)
    assert console.lines[-1] == "[trading g1] \u03b1 stderr traceback"
    tail.drain(console, final=True)
    assert console.lines[-1] == "[trading g1] last without newline"
    tail.drain(console, final=True)
    assert len(console.lines) == 3
    assert path.read_bytes() == raw + b" stderr traceback\nlast without newline"


def test_tail_bounds_backlog_fragments_and_retains_original_bytes(tmp_path):
    path = tmp_path / "model.log"
    original = b"x" * 500000 + b"\nfinal traceback"
    path.write_bytes(original)
    console = RecordingConsole()
    tail = candidate.LogTail(path, "[model g7]")
    tail.drain(console)
    assert tail.offset == 65536 and len(tail.pending) < 16384
    tail.drain(console, final=True)
    assert any("backlog omitted" in line and str(path) in line for line in console.lines)
    assert console.lines[-1] == "[model g7] final traceback"
    assert max(map(len, console.lines)) < 16500
    assert path.read_bytes() == original


def test_external_truncation_and_console_error_are_only_diagnostics(tmp_path):
    path = tmp_path / "model.log"
    path.write_bytes(b"first line\n")
    console = RecordingConsole()
    tail = candidate.LogTail(path, "[model g1]")
    tail.drain(console)
    path.write_bytes(b"new\n")
    tail.drain(console)
    assert "truncated externally" in console.lines[-2]
    assert console.lines[-1] == "[model g1] new"
    class BrokenConsole:
        def emit(self, _):
            raise OSError("closed display")
    with path.open("ab") as stream:
        stream.write(b"preserved\n")
    tail.drain(BrokenConsole(), final=True)
    assert path.read_bytes() == b"new\npreserved\n"


def test_stalled_sink_queue_is_bounded_and_controller_keeps_its_deadline(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def blocked(_):
        entered.set()
        release.wait(5)
    console = candidate.ConsoleDisplay(blocked, capacity=4)
    console.emit("blocked sink")
    assert entered.wait(1)
    try:
        for _ in range(1000):
            console.emit("display only")
        assert console.queue.qsize() == 4 and console.dropped > 0
        h = Harness(tmp_path, "slow")
        h.owner.console.close()
        h.owner.console = console
        began = time.monotonic()
        assert h.run() == 0
        assert time.monotonic() - began < 1.5
        assert h.owner.state["stop_reason"] == "session_end"
        assert h.recovery_ticks > 0 and all(worker.closed for worker in h.workers)
        assert len([worker for worker in h.workers if worker.role == "model"]) == 1
    finally:
        release.set()
        console.close()


def test_broken_sink_does_not_kill_the_display_queue():
    attempted = threading.Event()
    count = []
    def broken(text):
        count.append(text)
        if len(count) == 2:
            attempted.set()
        raise BrokenPipeError("closed terminal")
    console = candidate.ConsoleDisplay(broken)
    try:
        console.emit("first"); console.emit("second")
        assert attempted.wait(1)
    finally:
        console.close()
    assert count == ["first", "second"]


def test_planned_rotations_forward_each_generation_final_output_and_phase(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.owner.console.close()
    console = h.owner.console = RecordingConsole()
    old_poll, old_close = FakeWorker.poll, FakeWorker.close
    def poll(worker):
        if worker.ticks == 0:
            (worker.root / (worker.role + ".log")).write_text("fixture stdout\n", encoding="utf-8")
        return old_poll(worker)
    def close(worker, **kwargs):
        if not worker.closed:
            with (worker.root / (worker.role + ".log")).open("a", encoding="utf-8") as stream:
                stream.write("fixture final stderr")
        return old_close(worker, **kwargs)
    monkeypatch.setattr(FakeWorker, "poll", poll)
    monkeypatch.setattr(FakeWorker, "close", close)
    assert h.run() == 0 and console.closed
    assert len([w for w in h.workers if w.role == "trading"]) == 1
    for worker in h.workers:
        prefix = f"[{worker.role} g{worker.request['generation']}] "
        assert console.lines.count(prefix + "fixture stdout") == 1
        assert console.lines.count(prefix + "fixture final stderr") == 1
    phases = [json.loads(line) for line in console.lines if line.startswith("{") and '"scout_worker_phase"' in line]
    assert {item["role"] for item in phases} == {"trading", "model"}
    assert any(item["generation"] > 1 for item in phases if item["role"] == "model")
    statuses = [json.loads(line) for line in console.lines if line.startswith("{") and '"scout_controller_status"' in line]
    assert statuses[-1]["status"] == "STOPPED" and statuses[-1]["stop_reason"] == "session_end"
    assert all(item["deadline"] == h.owner.state["deadline"] for item in statuses)


def test_missing_result_keeps_traceback_visible_and_other_role_running(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.owner.console.close()
    console = h.owner.console = RecordingConsole()
    old_poll = FakeWorker.poll
    def poll(worker):
        if worker.role == "model" and worker.ticks == 3:
            (worker.root / "model.log").write_text("Traceback: exact fixture failure", encoding="utf-8")
            worker.returncode = 9
            return 9
        return old_poll(worker)
    monkeypatch.setattr(FakeWorker, "poll", poll)
    assert h.run() == 0
    assert "[model g1] Traceback: exact fixture failure" in console.lines
    assert h.owner.state["roles"]["model"]["status"] == "FAILED"
    assert h.owner.state["roles"]["model"]["error_type"] == "FileNotFoundError"
    assert h.owner.state["roles"]["model"]["automatic_retry"] is False
    assert h.recovery_ticks > 10
    assert any('"status": "DEGRADED"' in line and 'result.json' in line for line in console.lines)


def test_quiet_heartbeat_reports_original_role_generation_and_deadline(tmp_path):
    h = Harness(tmp_path, "slow")
    h.owner.console.close()
    console = h.owner.console = RecordingConsole()
    h.owner.heartbeat_seconds = .3
    assert h.run() == 0
    statuses = [json.loads(line) for line in console.lines if line.startswith("{")]
    running = [item for item in statuses if item["event"] == "scout_controller_status" and item["status"] == "RUNNING"]
    assert len(running) >= 4
    assert all(item["roles"]["trading"]["generation"] == 1 for item in running)
    assert all(item["roles"]["model"]["generation"] == 1 for item in running)
    assert all(item["deadline"] == h.owner.state["deadline"] for item in running)


def test_probe_error_forwards_retained_log_and_uses_unbuffered_utf8(tmp_path, monkeypatch):
    console = RecordingConsole()
    closed = []
    class ProbeFixture:
        def __init__(self, command, **kwargs):
            assert "-u" in command
            assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
            assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8:replace"
            self.path = kwargs["log_path"]
            self.path.write_text("startup stdout\nfixture probe traceback", encoding="utf-8")
        def wait(self, **kwargs): return 1
        def close(self): closed.append(True)
    monkeypatch.setattr(candidate, "ManagedWorker", ProbeFixture)
    with pytest.raises(RuntimeError, match="recipe/calendar probe failed"):
        candidate.probe(selection(tmp_path), tmp_path, tmp_path / "probe", console=console)
    assert closed == [True]
    assert console.lines[0].startswith("Scout startup: validating saved recipe/calendar")
    assert console.lines[-1] == "[startup probe] fixture probe traceback"
    assert len(list((tmp_path / "probe").glob("*.log"))) == 1


def test_check_keeps_one_json_response_and_never_starts_console_or_trader(tmp_path, monkeypatch, capsys):
    selected = selection(tmp_path)
    prepared = {"wake_at": "fixture", "deadline": "fixture", "trader_started": False}
    monkeypatch.setattr(candidate, "selected_release", lambda _: selected)
    monkeypatch.setattr(candidate, "probe", lambda *args: prepared)
    def forbidden(*args, **kwargs):
        raise AssertionError("Check started display or trader")
    monkeypatch.setattr(candidate, "ConsoleDisplay", forbidden)
    monkeypatch.setattr(candidate, "ServiceController", forbidden)
    assert candidate.main(["--check", "--repository", str(tmp_path), "--datastore", str(tmp_path),
                           "--selector", str(tmp_path / "selected.json")]) == 0
    assert json.loads(capsys.readouterr().out) == prepared


def test_console_thread_resources_cannot_become_a_launch_prerequisite(monkeypatch):
    def unavailable(_):
        raise RuntimeError("fixture thread capacity")
    monkeypatch.setattr(threading.Thread, "start", unavailable)
    console = candidate.ConsoleDisplay()
    console.emit("display unavailable")
    console.close()
    assert console.thread is None


def test_close_failure_keeps_final_traceback_and_propagates_same_error(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    h.owner.console.close()
    console = h.owner.console = RecordingConsole()
    h.owner.root.mkdir(parents=True)
    h.owner._start_role("model", selection(tmp_path))
    worker = h.workers[0]
    original = RuntimeError("fixture containment failure")
    def close(**kwargs):
        (worker.root / "model.log").write_text("last traceback before close failure", encoding="utf-8")
        raise original
    monkeypatch.setattr(worker, "close", close)
    with pytest.raises(RuntimeError) as caught:
        h.owner._close_worker("model", .1)
    assert caught.value is original
    assert console.lines[-1] == "[model g1] last traceback before close failure"
    assert h.owner.workers["model"] is worker  # Original close failure is not hidden.


@pytest.mark.parametrize("native_available", [True, False])
def test_native_console_open_is_lazy_daemon_only_with_stdout_fallback(monkeypatch, native_available):
    attempted, delivered = threading.Event(), threading.Event()
    calls = []
    class Output(io.StringIO):
        def write(self, text):
            result = super().write(text)
            if "fixture visible event" in self.getvalue():
                delivered.set()
            return result
    native, redirected = Output(), Output()
    def open_console(path, mode, **kwargs):
        calls.append((path, mode, kwargs, threading.current_thread().name))
        attempted.set()
        if not native_available:
            raise OSError("No attached console")
        return native
    monkeypatch.setattr(candidate, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(candidate, "open", open_console, raising=False)
    monkeypatch.setattr(candidate.sys, "stdout", redirected)
    console = candidate.ConsoleDisplay()
    assert not calls  # Construction on the controller thread performs no console I/O.
    try:
        console.emit("fixture visible event")
        assert attempted.wait(1) and delivered.wait(1)
    finally:
        console.close()
    assert calls == [("CONOUT$", "w", {"encoding": "utf-8", "errors": "replace", "buffering": 1}, "scout-console")]
    if native_available:
        assert "fixture visible event" in native.getvalue() and redirected.getvalue() == ""
    else:
        assert native.getvalue() == "" and "fixture visible event" in redirected.getvalue()


def test_lost_native_console_falls_back_to_original_stdout(monkeypatch):
    delivered = threading.Event()
    class BrokenNative:
        def write(self, _): raise OSError("Console detached")
        def close(self): pass
    class Redirected(io.StringIO):
        def write(self, text):
            result = super().write(text)
            if "after detach" in self.getvalue(): delivered.set()
            return result
    redirected = Redirected()
    monkeypatch.setattr(candidate, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(candidate, "open", lambda *args, **kwargs: BrokenNative(), raising=False)
    monkeypatch.setattr(candidate.sys, "stdout", redirected)
    console = candidate.ConsoleDisplay()
    try:
        console.emit("after detach")
        assert delivered.wait(1)
    finally:
        console.close()
    assert redirected.getvalue() == "after detach\n"
