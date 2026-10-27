"""Offline console feedback checks; all controller workers are in-memory fakes."""
from datetime import timedelta
import importlib.util
import io
import os
from pathlib import Path
import signal
import site
import subprocess
import sys
import time

import pytest

from tools.scout_console import ConsoleStatus
from tools import scout_iteration_controller as controller


class Stream(io.StringIO):
    def __init__(self, *, terminal=False):
        super().__init__()
        self.terminal = terminal
        self.flushes = 0

    def isatty(self):
        return self.terminal

    def flush(self):
        self.flushes += 1


def state(**changes):
    return {
        "status": "RUNNING", "generation": 1,
        "wake_at": "2026-10-27T13:30:00+00:00",
        "deadline": "2026-10-28T00:00:00+00:00",
        "worker": {"pid": 12345}, **changes,
    }


def harness(tmp_path, monkeypatch, **kwargs):
    # Reuse the existing deterministic controller fixture without importing a
    # third-party package also named tests or altering the application's path.
    path = Path(__file__).with_name("test_scout_iteration_controller.py")
    spec = importlib.util.spec_from_file_location("scout_console_controller_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Harness(tmp_path, monkeypatch, **kwargs)


def test_status_is_visible_immediately_and_unchanged_ticks_are_quiet(tmp_path):
    output = Stream()
    elapsed = [0.0]
    reporter = ConsoleStatus(stream=output, monotonic=lambda: elapsed[0])
    reporter.update(state(), tmp_path)
    first = output.getvalue()
    assert "running" in first.lower()
    assert "12345" in first
    assert output.flushes > 0
    for tick in range(1, 300):
        elapsed[0] = tick / 10
        reporter.update(state(), tmp_path)
    assert output.getvalue() == first
    elapsed[0] = 30
    reporter.update(state(), tmp_path)
    assert len(output.getvalue()) > len(first)
    heartbeat = output.getvalue()[len(first):].lower()
    assert "running" in heartbeat
    assert "healthy" not in heartbeat
    after_heartbeat = output.getvalue()
    elapsed[0] = 59.9
    reporter.update(state(), tmp_path)
    assert output.getvalue() == after_heartbeat


def test_phase_generation_and_worker_changes_are_reported_without_waiting(tmp_path):
    output = Stream()
    reporter = ConsoleStatus(stream=output, monotonic=lambda: 0)
    reporter.update(state(), tmp_path, phase="ready")
    before = output.getvalue()
    reporter.update(state(), tmp_path, phase="draining")
    assert "drain" in output.getvalue()[len(before):].lower()
    before = output.getvalue()
    reporter.update(state(generation=2, worker={"pid": 54321}), tmp_path)
    assert "54321" in output.getvalue()[len(before):]
    before = output.getvalue()
    reporter.update(state(generation=2, worker={"pid": 65432}), tmp_path)
    assert "65432" in output.getvalue()[len(before):]


@pytest.mark.parametrize("error", [BrokenPipeError("closed pipe"), OSError("console unavailable"),
                                  UnicodeError("unsupported console character"),
                                  RuntimeError("reentrant console write")])
def test_console_write_failure_cannot_interrupt_lifecycle(error, tmp_path):
    class BrokenStream:
        def write(self, _):
            raise error

        def flush(self):
            raise AssertionError("Cannot flush a failed write")

        def isatty(self):
            return False

    reporter = ConsoleStatus(stream=BrokenStream(), monotonic=lambda: 0)
    reporter.write("Scout running")
    reporter.update(state(), tmp_path)


@pytest.mark.parametrize("stdin_terminal,stdout_terminal", [(False, False), (False, True), (True, False)])
def test_exit_hold_never_blocks_a_redirected_or_unattended_run(monkeypatch, stdin_terminal, stdout_terminal):
    monkeypatch.setattr(sys, "stdin", Stream(terminal=stdin_terminal))
    output = Stream(terminal=stdout_terminal)
    reporter = ConsoleStatus(stream=output)

    def unexpected_input(*_):
        raise AssertionError("Noninteractive execution must never wait for input")

    monkeypatch.setattr("builtins.input", unexpected_input)
    reporter.hold()


@pytest.mark.parametrize("interruption", [None, EOFError, KeyboardInterrupt])
def test_interactive_exit_hold_can_be_dismissed_without_traceback(monkeypatch, interruption):
    monkeypatch.setattr(sys, "stdin", Stream(terminal=True))
    output = Stream(terminal=True)
    reporter = ConsoleStatus(stream=output)
    calls = []

    def dismissed(*args):
        calls.append(args)
        if interruption is not None:
            raise interruption()
        return ""

    monkeypatch.setattr("builtins.input", dismissed)
    reporter.hold()
    assert len(calls) == 1


def test_console_observes_real_controller_rotation_without_changing_it(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch, generations=2)
    output = Stream()
    h.owner.console = ConsoleStatus(stream=output, monotonic=lambda: h.clock.elapsed)
    assert h.run() == 0
    text = output.getvalue().lower()
    assert "starting" in text
    assert "running" in text
    assert "drain" in text
    assert "stop" in text
    assert str(h.workers[0].pid) in text
    assert str(h.workers[1].pid) in text
    assert len(h.workers) == 2
    assert all(worker.closed and not worker.terminated for worker in h.workers)
    assert h.owner.state["stop_reason"] == "manual_stop"


def test_reporter_formatting_failure_cannot_fail_or_interrupt_the_run(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch, generations=2)

    class BrokenReporter:
        def update(self, *_args, **_kwargs):
            raise ValueError("fixture console formatter unavailable")

    h.owner.console = BrokenReporter()
    assert h.run() == 0
    assert len(h.workers) == 2
    assert all(worker.closed and not worker.terminated for worker in h.workers)
    assert h.owner.state["status"] == "STOPPED"
    assert h.owner.state["stop_reason"] == "manual_stop"


@pytest.mark.parametrize("stage", ["waiting", "created", "running", "backoff"])
def test_manual_stop_remains_visible_and_never_spawns_a_successor(tmp_path, monkeypatch, stage):
    h = harness(tmp_path, monkeypatch, stop_stage=stage)
    if stage == "waiting":
        h.owner.state["wake_at"] = (h.clock() + timedelta(minutes=5)).isoformat()
    output = Stream()
    h.owner.console = ConsoleStatus(stream=output, monotonic=lambda: h.clock.elapsed)
    assert h.run() == 0
    assert "stop" in output.getvalue().lower()
    assert h.owner.stop_path.exists()
    assert len(h.workers) <= 1
    assert all(worker.closed for worker in h.workers)


def test_failed_worker_reports_failure_and_still_closes_tree(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch, mode="crash")
    output = Stream()
    h.owner.console = ConsoleStatus(stream=output, monotonic=lambda: h.clock.elapsed)
    with pytest.raises(RuntimeError, match="precise fake failure"):
        h.run()
    assert "fail" in output.getvalue().lower()
    assert len(h.workers) == 1
    assert h.workers[0].closed
    assert h.owner.state["automatic_retry"] is False


def main_arguments(tmp_path, mode="--live", *, hold=True):
    arguments = [mode, "--repository", str(tmp_path), "--datastore", str(tmp_path / "private"),
                 "--selector", str(tmp_path / "selector.json")]
    return arguments + (["--hold-console"] if hold else [])


def prepare_fake_main(h, monkeypatch, reporter):
    original_run = h.owner.run
    h.owner.run = lambda: original_run(lock_factory=h.locks)

    def controller_factory(**kwargs):
        assert kwargs["console"] is reporter
        h.owner.console = reporter
        return h.owner

    monkeypatch.setattr(controller, "ConsoleStatus", lambda: reporter)
    monkeypatch.setattr(controller, "resolve_selection", lambda _: h.current)
    monkeypatch.setattr(controller, "probe", lambda *_: {
        "wake_at": h.owner.state["wake_at"], "deadline": h.owner.state["deadline"]})
    monkeypatch.setattr(controller, "Controller", controller_factory)


@pytest.mark.parametrize("interruption", [False, True])
def test_main_explains_startup_failure_or_ctrl_c_without_ever_activating(tmp_path, monkeypatch, interruption):
    output = Stream()
    reporter = ConsoleStatus(stream=output)
    held, selections = [], []
    monkeypatch.setattr(reporter, "hold", lambda: held.append(output.getvalue()))
    monkeypatch.setattr(controller, "ConsoleStatus", lambda: reporter)

    def resolve(_):
        selections.append(True)
        assert "starting" in output.getvalue().lower()
        return {"fixture": True}

    def preflight(*_):
        if interruption:
            raise KeyboardInterrupt()
        raise ValueError("fixture manifest validation failed")

    def forbidden(**_):
        raise AssertionError("Failed or interrupted preflight must never activate a controller")

    monkeypatch.setattr(controller, "resolve_selection", resolve)
    monkeypatch.setattr(controller, "probe", preflight)
    monkeypatch.setattr(controller, "Controller", forbidden)
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    assert controller.main(main_arguments(tmp_path)) == (130 if interruption else 1)
    assert selections == [True]
    assert len(held) == 1
    assert ("interrupted" if interruption else "failed") in held[0].lower()
    assert "cleanup complete" not in output.getvalue().lower()
    assert all(signal.getsignal(sig) is previous for sig, previous in before.items())


def test_sigint_stop_and_final_confirmation_precede_hold_only_after_tree_cleanup(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch)
    output = Stream()
    reporter = ConsoleStatus(stream=output, monotonic=lambda: h.clock.elapsed)
    prepare_fake_main(h, monkeypatch, reporter)
    original_event = h.event
    held, delivered = [], []
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def event(stage, worker=None):
        original_event(stage, worker)
        if stage == "running" and not delivered:
            delivered.append(True)
            handler = signal.getsignal(signal.SIGINT)
            assert callable(handler) and handler is not before[signal.SIGINT]
            handler(signal.SIGINT, None)
        if stage == "closed":
            assert "cleanup complete" not in output.getvalue().lower()

    def hold():
        held.append(True)
        assert all(worker.closed for worker in h.workers)
        assert not h.locked
        assert "cleanup complete" in output.getvalue().lower()
        assert all(signal.getsignal(sig) is previous for sig, previous in before.items())

    h.event = event
    monkeypatch.setattr(reporter, "hold", hold)
    assert controller.main(main_arguments(tmp_path)) == 0
    assert held == delivered == [True]
    assert h.owner.stop_path.exists()
    assert len(h.workers) == 1
    assert "stopping" in output.getvalue().lower()


def test_main_failure_holds_only_after_cleanup_and_restores_signal_handlers(tmp_path, monkeypatch):
    h = harness(tmp_path, monkeypatch, mode="crash")
    output = Stream()
    reporter = ConsoleStatus(stream=output, monotonic=lambda: h.clock.elapsed)
    prepare_fake_main(h, monkeypatch, reporter)
    held = []
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

    def hold():
        held.append(True)
        assert h.workers and all(worker.closed for worker in h.workers)
        assert not h.locked
        assert "precise fake failure" in output.getvalue()
        assert "cleanup complete" not in output.getvalue().lower()
        assert all(signal.getsignal(sig) is previous for sig, previous in before.items())

    monkeypatch.setattr(reporter, "hold", hold)
    assert controller.main(main_arguments(tmp_path)) == 1
    assert held == [True]
    assert len(h.workers) == 1


def test_check_mode_preserves_machine_readable_output_and_never_holds(tmp_path, monkeypatch, capsys):
    prepared = {"wake_at": "fixture-wake", "deadline": "fixture-deadline"}
    monkeypatch.setattr(controller, "resolve_selection", lambda _: {})
    monkeypatch.setattr(controller, "probe", lambda *_: prepared)

    def forbidden():
        raise AssertionError("Check mode must not create an interactive console reporter")

    monkeypatch.setattr(controller, "ConsoleStatus", forbidden)
    assert controller.main(main_arguments(tmp_path, "--check")) == 0
    import json
    assert json.loads(capsys.readouterr().out) == prepared


@pytest.mark.parametrize("mode,function", [("--worker", "worker_main"), ("--probe", "offline_probe")])
def test_child_modes_never_create_or_hold_interactive_console(tmp_path, monkeypatch, mode, function):
    request = tmp_path / "fixture-request.json"
    calls = []
    monkeypatch.setattr(controller, function, lambda path: calls.append(path) or 17)

    def forbidden():
        raise AssertionError("Contained child modes must never use the interactive parent console")

    monkeypatch.setattr(controller, "ConsoleStatus", forbidden)
    assert controller.main([mode, str(request), "--hold-console"]) == 17
    assert calls == [request]


@pytest.mark.skipif(os.name != "nt", reason="Windows console control event contract")
def test_native_ctrl_c_stops_fake_run_and_keeps_its_own_hidden_console_open(tmp_path):
    """Send CTRL_C only to a newly created fixture console; never attach elsewhere."""
    source = r'''
import ctypes
import importlib.util
import os
from pathlib import Path
import sys
import time
import traceback
import pytest

root, repository = Path(sys.argv[1]), Path(sys.argv[2])
(root / "fixture-pid").write_text(str(os.getpid()), encoding="utf-8")
log = (root / "console.txt").open("w", encoding="utf-8", buffering=1)
native_sleep = time.sleep
try:
    sys.stdin = open("CONIN$", "r")
    native_output = open("CONOUT$", "w")
    class Tee:
        def write(self, message):
            log.write(message)
            return native_output.write(message)
        def flush(self):
            log.flush()
            native_output.flush()
        def isatty(self):
            return native_output.isatty()
    sys.stdout = Tee()
    assert sys.stdin.isatty() and sys.stdout.isatty()
    fixture_path = repository / "tests/test_scout_iteration_controller.py"
    spec = importlib.util.spec_from_file_location("native_console_fixtures", fixture_path)
    fixtures = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixtures)
    from tools import scout_iteration_controller as controller
    from tools.scout_console import ConsoleStatus
    patch = pytest.MonkeyPatch()
    h = fixtures.Harness(root / "fake-run", patch)
    original_run, original_event = h.owner.run, h.event
    h.owner.run = lambda: original_run(lock_factory=h.locks)
    sent = []
    def event(stage, worker=None):
        original_event(stage, worker)
        if stage == "running" and not sent:
            sent.append(True)
            # Group zero targets only processes attached to THIS new console.
            if not ctypes.windll.kernel32.GenerateConsoleCtrlEvent(0, 0):
                raise ctypes.WinError()
            native_sleep(0.1)
        if stage == "closed":
            assert "cleanup complete" not in (root / "console.txt").read_text(encoding="utf-8")
            (root / "cleaned").write_text("owned fake worker closed", encoding="utf-8")
    h.event = event
    reporter = ConsoleStatus(stream=sys.stdout)
    original_hold = reporter.hold
    def hold():
        assert len(h.workers) == 1 and all(worker.closed for worker in h.workers)
        assert not h.locked
        assert h.owner.state["stop_reason"] == "manual_stop"
        assert "cleanup complete" in (root / "console.txt").read_text(encoding="utf-8")
        original_hold()
    reporter.hold = hold
    def make_controller(**kwargs):
        h.owner.console = kwargs["console"]
        return h.owner
    patch.setattr(controller, "ConsoleStatus", lambda: reporter)
    patch.setattr(controller, "resolve_selection", lambda _: h.current)
    patch.setattr(controller, "probe", lambda *_: {
        "wake_at": h.owner.state["wake_at"], "deadline": h.owner.state["deadline"]})
    patch.setattr(controller, "Controller", make_controller)
    result = controller.main(["--live", "--hold-console", "--repository", str(root),
        "--datastore", str(root / "private"), "--selector", str(root / "selector.json")])
    (root / "returned").write_text(str(result), encoding="utf-8")
except BaseException:
    traceback.print_exc(file=log)
    log.flush()
    raise
'''
    script = tmp_path / "native-console-fixture.py"
    script.write_text(source, encoding="utf-8")
    repository = Path(__file__).resolve().parents[1]
    # Invoke the actual interpreter directly so terminating this fixture cannot
    # strand a child of Windows' virtualenv launcher at the input prompt.
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(repository), *site.getsitepackages()]),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    process = subprocess.Popen([sys._base_executable, "-B", str(script), str(tmp_path), str(repository)],
                               cwd=tmp_path, env=environment, startupinfo=startup,
                               creationflags=subprocess.CREATE_NEW_CONSOLE)
    output = ""
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            log = tmp_path / "console.txt"
            if log.exists():
                output = log.read_text(encoding="utf-8")
            if "Press Enter to close this window" in output or process.poll() is not None:
                break
            time.sleep(0.02)
        assert "cleanup complete" in output, output
        assert int((tmp_path / "fixture-pid").read_text()) == process.pid
        assert (tmp_path / "cleaned").exists(), output
        assert "Press Enter to close this window" in output, output
        assert "Traceback" not in output, output
        time.sleep(0.1)
        assert process.poll() is None, "The fixture must remain in its interactive exit prompt: " + output
        assert not (tmp_path / "returned").exists(), output
    finally:
        # Only this test-created subprocess is stopped; it has no real workers.
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
