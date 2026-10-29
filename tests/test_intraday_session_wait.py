"""Offline launch-window checks using fake clocks and disposable Windows hosts.

Run directly with unittest; no application conftest, credentials, data store,
provider, broker, or production host is imported or executed by these tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools/intraday_session_wait.py"
UTC = timezone.utc


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_helper(path=HELPER):
    spec = importlib.util.spec_from_file_location("session_wait_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeClock:
    def __init__(self, start, *, first_sleep_jump=None, interrupt=False):
        self.current = instant(start)
        self.sleeps = []
        self.first_sleep_jump = first_sleep_jump
        self.interrupt = interrupt

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.interrupt:
            raise KeyboardInterrupt
        if self.first_sleep_jump is not None:
            self.current = self.first_sleep_jump
            self.first_sleep_jump = None
        else:
            self.current += timedelta(seconds=seconds)
        if len(self.sleeps) > 1000:
            raise AssertionError("Fake-clock wait did not finish")


class SessionWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = load_helper()

    def messages(self):
        output = []

        def emit(*items, **kwargs):
            output.append(" ".join(str(item) for item in items))

        return output, emit

    def test_pacific_target_uses_date_and_daylight_saving_rules(self):
        examples = (
            ("2026-10-29T07:30:00Z", "2026-10-29T11:00:00Z"),
            ("2026-12-03T08:30:00Z", "2026-12-03T12:00:00Z"),
            # The cutoff is after the spring jump and fall repeated hour.
            ("2026-03-08T08:30:00Z", "2026-03-08T11:00:00Z"),
            ("2026-11-01T07:30:00Z", "2026-11-01T12:00:00Z"),
            ("2026-11-01T08:30:00Z", "2026-11-01T12:00:00Z"),
            ("2026-11-01T09:30:00Z", "2026-11-01T12:00:00Z"),
            # UTC date differs from the Pacific session date; after the
            # cutoff means start now, never defer to the next morning.
            ("2026-10-29T02:00:00Z", "2026-10-28T11:00:00Z"),
        )
        for current, expected in examples:
            with self.subTest(current=current):
                target = self.helper.pacific_session_start(instant(current))
                self.assertIsNotNone(target.tzinfo)
                self.assertEqual(target.astimezone(UTC), instant(expected))

    def test_naive_session_timestamp_is_rejected(self):
        with self.assertRaises(ValueError):
            self.helper.pacific_session_start(datetime(2026, 10, 29, 3, 55))

    def test_wait_before_four_never_releases_early(self):
        clock = FakeClock("2026-10-29T10:59:57.500000Z")
        messages, emit = self.messages()
        self.helper.wait_until_start("15-minute data", now=clock.now,
                                     sleep=clock.sleep, emit=emit)
        self.assertEqual(clock.current, instant("2026-10-29T11:00:00Z"))
        self.assertTrue(clock.sleeps)
        self.assertTrue(all(0 < delay <= 1 for delay in clock.sleeps))
        waiting = [line.lower() for line in messages if "waiting" in line.lower()]
        self.assertTrue(waiting, messages)
        self.assertIn("4:00", waiting[0])
        self.assertIn("session start", waiting[0])

    def test_at_or_after_four_does_not_sleep(self):
        for start in ("2026-10-29T11:00:00Z", "2026-10-29T19:00:00Z"):
            with self.subTest(start=start):
                clock = FakeClock(start)
                _, emit = self.messages()
                self.helper.wait_until_start("15-minute trader", now=clock.now,
                                             sleep=clock.sleep, emit=emit)
                self.assertEqual(clock.sleeps, [])

    def test_wait_reports_minute_heartbeats(self):
        clock = FakeClock("2026-10-29T10:57:55Z")
        messages, emit = self.messages()
        self.helper.wait_until_start("15-minute data", now=clock.now,
                                     sleep=clock.sleep, emit=emit)
        waiting = [line for line in messages if "waiting" in line.lower()]
        self.assertGreaterEqual(len(waiting), 3)
        self.assertLessEqual(len(waiting), 4, "Waiting output should not flood the console")

    def test_forward_clock_jump_releases_without_original_duration_sleep(self):
        clock = FakeClock("2026-10-29T10:59:40Z",
                          first_sleep_jump=instant("2026-10-29T11:00:04Z"))
        _, emit = self.messages()
        self.helper.wait_until_start("15-minute data", now=clock.now,
                                     sleep=clock.sleep, emit=emit)
        self.assertEqual(len(clock.sleeps), 1)
        self.assertLessEqual(clock.sleeps[0], 1)

    def test_backward_clock_jump_does_not_release_before_fixed_cutoff(self):
        clock = FakeClock("2026-10-29T10:59:58Z",
                          first_sleep_jump=instant("2026-10-29T10:59:55Z"))
        _, emit = self.messages()
        self.helper.wait_until_start("15-minute trader", now=clock.now,
                                     sleep=clock.sleep, emit=emit)
        self.assertEqual(clock.current, instant("2026-10-29T11:00:00Z"))
        self.assertGreaterEqual(len(clock.sleeps), 6)

    def test_suspension_to_next_day_before_four_waits_for_new_session_start(self):
        clock = FakeClock("2026-10-29T10:59:58Z",
                          first_sleep_jump=instant("2026-10-30T10:59:58Z"))
        _, emit = self.messages()
        self.helper.wait_until_start("15-minute data", now=clock.now,
                                     sleep=clock.sleep, emit=emit)
        self.assertEqual(clock.current, instant("2026-10-30T11:00:00Z"))
        self.assertEqual(len(clock.sleeps), 3)

    def test_wait_keyboard_interrupt_propagates(self):
        clock = FakeClock("2026-10-29T10:59:58Z", interrupt=True)
        _, emit = self.messages()
        with self.assertRaises(KeyboardInterrupt):
            self.helper.wait_until_start("15-minute trader", now=clock.now,
                                         sleep=clock.sleep, emit=emit)

    def test_cancelled_main_never_dispatches_either_host(self):
        for role in ("data", "trader"):
            with self.subTest(role=role):
                clock = FakeClock("2026-10-29T10:59:58Z", interrupt=True)
                _, emit = self.messages()
                dispatched = []
                result = self.helper.main(
                    [role, "--fixture", "literal!value (with spaces)"],
                    now=clock.now, sleep=clock.sleep, emit=emit,
                    dispatch=lambda *args: dispatched.append(args),
                )
                self.assertEqual(result, 130)
                self.assertEqual(dispatched, [])

    def test_timezone_resolution_failure_never_dispatches_host(self):
        clock = FakeClock("2026-10-29T10:59:58Z")
        messages, emit = self.messages()
        dispatched = []
        with patch.object(self.helper, "pacific_session_start",
                          side_effect=OSError("Synthetic timezone lookup failure")):
            result = self.helper.main(
                ["data"], now=clock.now, sleep=clock.sleep, emit=emit,
                dispatch=lambda *args: dispatched.append(args))
        self.assertEqual(result, 2)
        self.assertEqual(dispatched, [])
        self.assertEqual(clock.sleeps, [])
        self.assertTrue(any("no session was started" in line.lower() for line in messages))

    def test_owner_ending_during_wait_cancels_without_dispatch(self):
        for role in ("data", "trader"):
            with self.subTest(role=role):
                clock = FakeClock("2026-10-29T10:59:58Z")
                messages, emit = self.messages()
                dispatched = []
                result = self.helper.main(
                    [role], now=clock.now, sleep=clock.sleep, emit=emit,
                    dispatch=lambda *args: dispatched.append(args),
                    owner_alive=lambda: not clock.sleeps)
                self.assertEqual(result, 130)
                self.assertEqual(len(clock.sleeps), 1)
                self.assertEqual(dispatched, [])
                self.assertTrue(any("no session was started" in line.lower() for line in messages))

    def test_dispatch_is_once_after_release_and_forwards_exact_arguments_and_code(self):
        for role in ("data", "trader"):
            with self.subTest(role=role):
                clock = FakeClock("2026-10-29T10:59:58Z")
                _, emit = self.messages()
                dispatched = []
                forwarded = ["--fixture", "literal!value (with spaces)", "--datastore", "C:/DATASTORE"]

                def dispatch(selected, arguments):
                    self.assertGreaterEqual(clock.current, instant("2026-10-29T11:00:00Z"))
                    dispatched.append((selected, list(arguments)))
                    return 23

                result = self.helper.main([role, *forwarded], now=clock.now,
                                          sleep=clock.sleep, emit=emit, dispatch=dispatch)
                self.assertEqual(result, 23)
                self.assertEqual(dispatched, [(role, forwarded)])

    def test_default_dispatch_uses_only_copied_hosts_with_original_bindings(self):
        # This exercises the real runpy dispatch against empty disposable host
        # scripts. It cannot import either production data/trading entrypoint.
        for role, filename in (("data", "intraday_data_host.py"),
                               ("trader", "trader_session_host.py")):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary:
                repository = Path(temporary) / "copy with spaces & punctuation"
                tools = repository / "tools"
                tools.mkdir(parents=True)
                copied = tools / HELPER.name
                shutil.copyfile(HELPER, copied)
                receipt = repository / "arguments.json"
                (tools / filename).write_text(
                    "import json, sys\nfrom pathlib import Path\n"
                    f"Path({str(receipt)!r}).write_text(json.dumps(sys.argv))\n"
                    "raise SystemExit(7)\n", encoding="utf-8")
                helper = load_helper(copied)
                original_argv = list(sys.argv)
                clock = FakeClock("2026-10-29T12:00:00Z")
                _, emit = self.messages()
                forwarded = ["--fixture", "literal!value (with spaces)"]
                result = helper.main([role, *forwarded], now=clock.now,
                                     sleep=clock.sleep, emit=emit)
                self.assertEqual(result, 7)
                self.assertEqual(sys.argv, original_argv)
                args = json.loads(receipt.read_text())
                self.assertEqual(Path(args[0]), tools / filename)
                if role == "data":
                    self.assertEqual(args[1], "--repository")
                    self.assertEqual(Path(args[2]), repository)
                    self.assertEqual(args[3], "--datastore")
                    self.assertEqual(args[4].replace("\\", "/"), "C:/DATASTORE")
                    self.assertEqual(args[5:], forwarded)
                else:
                    self.assertEqual(args[1:], ["--trader", "intraday", *forwarded])


LEGACY_DATA = r'''@echo off
setlocal DisableDelayedExpansion
echo 15-minute System 1A - data collection and preparation only.
pushd "%~dp0"
if errorlevel 1 exit /b 2
if not exist ".venv\Scripts\python.exe" (
  echo Ducketz Python environment is missing.
  popd
  exit /b 2
)
".venv\Scripts\python.exe" -I -S -B "tools\intraday_data_host.py" --repository "%CD%" --datastore "C:\DATASTORE" %*
set "DUCKETZ_DATA_EXIT=%ERRORLEVEL%"
popd
exit /b %DUCKETZ_DATA_EXIT%
'''.encode("ascii")


@unittest.skipUnless(os.name == "nt", "Windows batch wrapper tests")
class WindowsLauncherTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        base = Path(cls.temporary.name)
        source = base / "fixture.cs"
        source.write_text(r'''
using System;
using System.IO;
using System.Threading;
class Fixture {
  static int Main(string[] args) {
    Console.WriteLine("SYNTHETIC_HOST_ONLY");
    Console.WriteLine("CWD=" + Environment.CurrentDirectory);
    for (int i=0; i<args.Length; i++) Console.WriteLine("ARG[" + i + "]=" + args[i]);
    string log=Environment.GetEnvironmentVariable("WAIT_FIXTURE_LOG");
    if (log != null) File.AppendAllText(log,"invoked\n");
    string ready=Environment.GetEnvironmentVariable("WAIT_FIXTURE_READY");
    string release=Environment.GetEnvironmentVariable("WAIT_FIXTURE_RELEASE");
    if (ready != null && release != null) {
      File.WriteAllText(ready,"waiting");
      DateTime deadline=DateTime.UtcNow.AddSeconds(30);
      while (!File.Exists(release) && DateTime.UtcNow<deadline) Thread.Sleep(20);
      if (!File.Exists(release)) return 90;
    }
    return Int32.Parse(Environment.GetEnvironmentVariable("WAIT_FIXTURE_EXIT") ?? "0");
  }
}
''', encoding="utf-8")
        windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
        cls.comspec = windows / "System32/cmd.exe"
        compilers = [windows / "Microsoft.NET" / arch / "v4.0.30319/csc.exe"
                     for arch in ("Framework64", "Framework")]
        compiler = next((candidate for candidate in compilers if candidate.is_file()), None)
        if compiler is None:
            raise AssertionError("Windows .NET compiler required for disposable launcher fixture")
        cls.runtime = base / "python.exe"
        result = subprocess.run([str(compiler), "/nologo", "/target:exe",
                                 f"/out:{cls.runtime}", str(source)],
                                capture_output=True, text=True, timeout=30,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def copy_launcher(self, temporary, name):
        checkout = Path(temporary) / "checkout with spaces & punctuation"
        unrelated = Path(temporary) / "unrelated cwd"
        runtime = checkout / ".venv/Scripts/python.exe"
        runtime.parent.mkdir(parents=True)
        unrelated.mkdir()
        shutil.copyfile(self.runtime, runtime)
        launcher = checkout / name
        shutil.copyfile(ROOT / name, launcher)
        return checkout, unrelated, launcher

    def command(self, launcher, arguments=""):
        return f'"{self.comspec}" /d /s /c ""{launcher}" {arguments}"'

    def test_copied_wrappers_route_through_wait_helper_and_preserve_exit_codes(self):
        for role, filename in (("data", "Start-15m-Data.cmd"),
                               ("trader", "Start-15m-Trader.cmd")):
            for exit_code in (0, 23, 130):
                with self.subTest(role=role, exit_code=exit_code), tempfile.TemporaryDirectory() as temporary:
                    checkout, unrelated, launcher = self.copy_launcher(temporary, filename)
                    result = subprocess.run(
                        self.command(launcher, '--fixture "literal!value (with spaces)"'),
                        cwd=unrelated, env=dict(os.environ, WAIT_FIXTURE_EXIT=str(exit_code)),
                        input="\n", capture_output=True, text=True, timeout=15,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
                    self.assertEqual(result.stdout.count("SYNTHETIC_HOST_ONLY"), 1)
                    self.assertIn("CWD=" + str(checkout), result.stdout)
                    expected = ["-I", "-S", "-B", "tools\\intraday_session_wait.py", role,
                                "--fixture", "literal!value (with spaces)"]
                    for index, value in enumerate(expected):
                        self.assertIn(f"ARG[{index}]={value}", result.stdout)

    def test_real_waiting_child_exits_when_its_exact_synthetic_parent_ends(self):
        """A canceled task must not leave an orphan that starts the real host later."""
        import ctypes as c
        from ctypes import wintypes as w

        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
        kernel.OpenProcess.restype = w.HANDLE
        kernel.WaitForSingleObject.argtypes = (w.HANDLE, w.DWORD)
        kernel.WaitForSingleObject.restype = w.DWORD
        kernel.CloseHandle.argtypes = (w.HANDLE,)
        kernel.CloseHandle.restype = w.BOOL
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            copied = base / "intraday_session_wait.py"
            shutil.copyfile(HELPER, copied)
            ready, outcome, dispatched, identity = (
                base / name for name in ("ready.json", "outcome.json", "dispatched.json", "parent.json"))
            child = base / "child.py"
            child.write_text(
                "import importlib.util, json, os, time\n"
                "from datetime import datetime, timezone\nfrom pathlib import Path\n"
                f"spec=importlib.util.spec_from_file_location('copied_wait', {str(copied)!r})\n"
                "helper=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(helper)\n"
                "def emit(message):\n"
                "    print(message,flush=True)\n"
                "    if 'Waiting until' in message:\n"
                f"        Path({str(ready)!r}).write_text(json.dumps({{'pid':os.getpid()}}))\n"
                "def dispatch(role, arguments):\n"
                f"    Path({str(dispatched)!r}).write_text(json.dumps({{'role':role,'arguments':arguments}}))\n"
                "    return 0\n"
                "result=helper.main(['data'],now=lambda:datetime(2026,10,29,10,55,tzinfo=timezone.utc),"
                "sleep=time.sleep,emit=emit,dispatch=dispatch)\n"
                f"Path({str(outcome)!r}).write_text(json.dumps({{'exit_code':result}}))\n"
                "raise SystemExit(result)\n", encoding="utf-8")
            parent_script = base / "parent.py"
            parent_script.write_text(
                "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
                f"child=subprocess.Popen([sys.executable,'-I','-S','-B',{str(child)!r}],"
                "creationflags=subprocess.CREATE_NO_WINDOW)\n"
                f"Path({str(identity)!r}).write_text(json.dumps({{'script_pid':os.getpid(),'child_wrapper_pid':child.pid}}))\n"
                "time.sleep(45)\n", encoding="utf-8")
            log = base / "console.txt"
            child_handle = None
            parent = None
            parent_identity = None
            with log.open("w", encoding="utf-8") as output:
                try:
                    parent = subprocess.Popen(
                        [sys.executable, "-I", "-S", "-B", str(parent_script)],
                        stdout=output, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    deadline = time.monotonic() + 15
                    child_identity = None
                    while child_identity is None:
                        self.assertIsNone(parent.poll(), "Disposable parent exited before ready")
                        self.assertLess(time.monotonic(), deadline,
                                        "Copied helper never reached wait: " + log.read_text())
                        try:
                            child_identity = json.loads(ready.read_text())
                            parent_identity = json.loads(identity.read_text())
                        except (FileNotFoundError, ValueError):
                            child_identity = None
                            time.sleep(0.02)
                    child_handle = kernel.OpenProcess(0x100000, False, child_identity["pid"])
                    self.assertTrue(child_handle, "Cannot pin disposable child lifetime")
                    self.assertFalse(dispatched.exists())
                    parent.kill()  # Only the exact Popen fixture process, possibly its venv redirector.
                    parent.wait(timeout=3)
                    self.assertEqual(kernel.WaitForSingleObject(child_handle, 3000), 0,
                                     "Waiting child survived its synthetic parent: " + log.read_text())
                    self.assertEqual(json.loads(outcome.read_text())["exit_code"], 130)
                    self.assertFalse(dispatched.exists(), "Canceled pending launch reached its host")
                finally:
                    if child_handle:
                        kernel.CloseHandle(child_handle)
                    # A venv redirector can exit while its synthetic script is
                    # still alive. End only this fixture's recorded tree, not
                    # another Python process or any actual trader/data owner.
                    cleanup_pid = (parent_identity["script_pid"] if parent_identity
                                   else parent.pid if parent is not None else None)
                    if cleanup_pid is not None:
                        subprocess.run(["taskkill.exe", "/PID", str(cleanup_pid), "/T", "/F"],
                                       capture_output=True, timeout=10,
                                       creationflags=subprocess.CREATE_NO_WINDOW, check=False)
                    if parent is not None and parent.poll() is None:
                        parent.kill()
                        parent.wait(timeout=3)

    def test_active_legacy_data_wrapper_replacement_preserves_tail_and_never_relaunches(self):
        self.assertEqual(len(LEGACY_DATA), 443)
        invocation = LEGACY_DATA.index(b'".venv\\Scripts\\python.exe" -I')
        resume = LEGACY_DATA.index(b"\n", invocation) + 1
        self.assertEqual(resume, 373)
        replacement = (ROOT / "Start-15m-Data.cmd").read_bytes()
        self.assertEqual(replacement[resume:], LEGACY_DATA[resume:],
                         "Existing cmd must resume at its unchanged exit tail")
        self.assertEqual(len(replacement), len(LEGACY_DATA))
        for exit_code in (0, 23):
            with self.subTest(exit_code=exit_code), tempfile.TemporaryDirectory() as temporary:
                _, unrelated, launcher = self.copy_launcher(temporary, "Start-15m-Data.cmd")
                launcher.write_bytes(LEGACY_DATA)
                ready, release, log = (Path(temporary) / item for item in ("ready", "release", "log"))
                process = subprocess.Popen(
                    self.command(launcher), cwd=unrelated,
                    env=dict(os.environ, WAIT_FIXTURE_EXIT=str(exit_code),
                             WAIT_FIXTURE_READY=str(ready), WAIT_FIXTURE_RELEASE=str(release),
                             WAIT_FIXTURE_LOG=str(log)),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    deadline = time.monotonic() + 15
                    while not ready.exists():
                        self.assertIsNone(process.poll(), "Disposable legacy child exited unexpectedly")
                        self.assertLess(time.monotonic(), deadline, "Disposable child never reached wait")
                        time.sleep(0.02)
                    candidate = launcher.with_name("replacement.cmd")
                    candidate.write_bytes(replacement)
                    os.replace(candidate, launcher)
                    release.write_text("test completed", encoding="ascii")
                    output, _ = process.communicate(input="\n", timeout=15)
                    self.assertEqual(process.returncode, exit_code, output)
                    self.assertEqual(log.read_text().splitlines(), ["invoked"], output)
                finally:
                    release.touch(exist_ok=True)
                    if process.poll() is None:
                        process.kill()  # This exact disposable synthetic fixture only.
                        process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
