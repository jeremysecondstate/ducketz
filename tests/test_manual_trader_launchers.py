"""Copy-only Windows launcher tests using disposable synthetic runtimes.

No real launcher PowerShell body, trading module or datastore is executed.
This file intentionally needs only pytest and the standard library so the
sealed publication candidate can run it with --noconftest.
"""
from __future__ import annotations

import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows command launchers")
REPOSITORY = Path(__file__).resolve().parents[1]
COMSPEC = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/cmd.exe"


@pytest.fixture(scope="module")
def synthetic_python(tmp_path_factory):
    root = tmp_path_factory.mktemp("synthetic-launcher-runtime")
    source = root / "runtime.cs"
    source.write_text(r'''
using System;
class SyntheticRuntime {
    static int Main(string[] args) {
        Console.WriteLine("SYNTHETIC_RUNTIME_ONLY");
        Console.WriteLine("CWD=" + Environment.CurrentDirectory);
        for (int i=0; i<args.Length; i++) Console.WriteLine("ARG["+i+"]="+args[i]);
        return Int32.Parse(Environment.GetEnvironmentVariable("DUCKETZ_FIXTURE_EXIT") ?? "0");
    }
}
''', encoding="utf-8")
    framework = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Microsoft.NET"
    compilers = [framework / name / "v4.0.30319/csc.exe" for name in ("Framework64", "Framework")]
    compiler = next((path for path in compilers if path.is_file()), None)
    assert compiler is not None, "Windows .NET Framework compiler unavailable for synthetic fixture"
    executable = root / "python.exe"
    result = subprocess.run(
        [str(compiler), "/nologo", "/target:exe", f"/out:{executable}", str(source)],
        capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def _copy_launcher(tmp_path, name, synthetic_python=None, *, gameplan_exit=7):
    checkout = tmp_path / "checkout with spaces & punctuation"
    unrelated = tmp_path / "unrelated working directory"
    checkout.mkdir()
    unrelated.mkdir()
    shutil.copyfile(REPOSITORY / name, checkout / name)
    if synthetic_python is not None:
        runtime = checkout / ".venv/Scripts/python.exe"
        runtime.parent.mkdir(parents=True)
        shutil.copyfile(synthetic_python, runtime)
    if name == "Start-Gameplan-Trader.cmd":
        # The real PowerShell source is never copied or evaluated. Only the
        # wrapper's quoting, flags, visible errors and exit code are exercised.
        script = checkout / "docs/datafetch-ml/start_stock_session.ps1"
        script.parent.mkdir(parents=True)
        script.write_text(r'''
param([switch]$WaitForOpen,[string]$SizingPolicy,[switch]$ActivateForManualStart)
Write-Host 'SYNTHETIC_GAMEPLAN_ONLY'
Write-Host "SCRIPT_ROOT=$PSScriptRoot"
Write-Host "WAIT=$WaitForOpen;POLICY=$SizingPolicy;MANUAL=$ActivateForManualStart"
exit EXIT_CODE
'''.replace("EXIT_CODE", str(gameplan_exit)), encoding="utf-8")
    return checkout, unrelated


def _command(launcher, arguments=""):
    # cmd /s removes the outer quote pair; the remaining pair protects the
    # complete copied path, including its deliberately embedded ampersand.
    return f'"{COMSPEC}" /d /s /c ""{launcher}" {arguments}"'


def _run(launcher, unrelated, *, exit_code=0, arguments=""):
    return subprocess.run(
        _command(launcher, arguments), cwd=unrelated,
        env=dict(os.environ, DUCKETZ_FIXTURE_EXIT=str(exit_code)),
        input="\n", capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _paused_failure(launcher, unrelated, marker, expected_code, *, exit_code=7):
    """Prove failure remains open until input, without creating a real window."""
    process = subprocess.Popen(
        _command(launcher), cwd=unrelated,
        env=dict(os.environ, DUCKETZ_FIXTURE_EXIT=str(exit_code)),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    output = []
    lines = queue.Queue()

    def collect():
        for line in process.stdout:
            output.append(line)
            lines.put(line)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 20
        while marker not in "".join(output):
            remaining = deadline - time.monotonic()
            assert remaining > 0, "Copied launcher did not report failure: " + "".join(output)
            try:
                lines.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                assert process.poll() is None, "Copied failure console closed early: " + "".join(output)
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=0.25)
        process.stdin.write("\n")
        process.stdin.flush()
        process.stdin.close()
        assert process.wait(timeout=15) == expected_code
        reader.join(timeout=5)
        assert not reader.is_alive()
        return "".join(output)
    finally:
        if process.poll() is None:
            process.kill()  # Only the disposable copied cmd fixture.
            process.wait(timeout=5)
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def test_15m_double_click_route_uses_own_environment_and_arguments(tmp_path, synthetic_python):
    checkout, unrelated = _copy_launcher(tmp_path, "Start-15m-Trader.cmd", synthetic_python)
    result = _run(checkout / "Start-15m-Trader.cmd", unrelated,
                  arguments='--fixture-token "value with spaces"')
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SYNTHETIC_RUNTIME_ONLY" in result.stdout
    assert "CWD=" + str(checkout) in result.stdout
    expected = ["-B", "-m", "ml.intraday.live_session", "--live", "--fixture-token", "value with spaces"]
    assert all(f"ARG[{i}]={value}" in result.stdout for i, value in enumerate(expected))
    assert "15-minute trader - manual start" in result.stdout
    assert "If console text is selected, press Esc first" in result.stdout
    assert "stopped with exit code" not in result.stdout


@pytest.mark.parametrize("exit_code", [7, 23])
def test_15m_worker_failure_stays_visible_and_retains_exit_code(tmp_path, synthetic_python, exit_code):
    checkout, unrelated = _copy_launcher(tmp_path, "Start-15m-Trader.cmd", synthetic_python)
    output = _paused_failure(checkout / "Start-15m-Trader.cmd", unrelated,
                             "Review the reason above before starting it again.", exit_code, exit_code=exit_code)
    assert f"The 15-minute trader stopped with exit code {exit_code}." in output
    assert "SYNTHETIC_RUNTIME_ONLY" in output


def test_15m_missing_environment_stays_visible_with_exit_two(tmp_path):
    checkout, unrelated = _copy_launcher(tmp_path, "Start-15m-Trader.cmd")
    output = _paused_failure(checkout / "Start-15m-Trader.cmd", unrelated,
                             "Ducketz Python environment is missing.", 2)
    assert "SYNTHETIC_RUNTIME_ONLY" not in output




