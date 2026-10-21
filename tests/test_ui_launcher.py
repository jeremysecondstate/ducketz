"""Run copied UI launchers with a synthetic executable; never import the app."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows command launcher")
REPOSITORY = Path(__file__).resolve().parents[1]
SYSTEM_ROOT = Path(os.environ.get("SystemRoot", "C:/Windows"))
COMSPEC = SYSTEM_ROOT / "System32/cmd.exe"


@pytest.fixture(scope="module")
def synthetic_python(tmp_path_factory):
    root = tmp_path_factory.mktemp("ui-launcher-runtime")
    source = root / "runtime.cs"
    source.write_text(r'''
using System;
class SyntheticRuntime {
    static int Main(string[] args) {
        Console.WriteLine("SYNTHETIC_UI_RUNTIME_ONLY");
        Console.WriteLine("CWD=" + Environment.CurrentDirectory);
        Console.WriteLine("ARGS=" + String.Join("|", args));
        return Int32.Parse(Environment.GetEnvironmentVariable("DUCKETZ_FIXTURE_EXIT") ?? "0");
    }
}
''', encoding="utf-8")
    framework = SYSTEM_ROOT / "Microsoft.NET"
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


def _copy_launcher(tmp_path, synthetic_python=None):
    checkout = tmp_path / "checkout with spaces & punctuation"
    unrelated = tmp_path / "unrelated working directory"
    checkout.mkdir()
    unrelated.mkdir()
    launcher = checkout / "duckets-ui.cmd"
    shutil.copyfile(REPOSITORY / launcher.name, launcher)
    if synthetic_python is not None:
        runtime = checkout / ".venv/Scripts/python.exe"
        runtime.parent.mkdir(parents=True)
        shutil.copyfile(synthetic_python, runtime)
    return launcher, unrelated


def _run(launcher, unrelated, *, exit_code=0):
    # cmd /s strips the outer quote pair, preserving the copied path's quotes.
    command = f'"{COMSPEC}" /d /s /c ""{launcher}""'
    return subprocess.run(
        command, cwd=unrelated,
        env=dict(os.environ, DUCKETZ_FIXTURE_EXIT=str(exit_code)),
        input="\n", capture_output=True, text=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def test_ui_double_click_uses_own_environment_and_ui_route(tmp_path, synthetic_python):
    launcher, unrelated = _copy_launcher(tmp_path, synthetic_python)
    result = _run(launcher, unrelated)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SYNTHETIC_UI_RUNTIME_ONLY" in result.stdout
    assert "CWD=" + str(launcher.parent) in result.stdout
    assert "ARGS=-B|-m|app.main|ui\n" in result.stdout
    assert "stopped with exit code" not in result.stdout


def test_ui_failure_preserves_diagnostics_and_exit_code(tmp_path, synthetic_python):
    launcher, unrelated = _copy_launcher(tmp_path, synthetic_python)
    result = _run(launcher, unrelated, exit_code=23)
    assert result.returncode == 23, result.stdout + result.stderr
    assert "SYNTHETIC_UI_RUNTIME_ONLY" in result.stdout
    assert "Ducketz UI stopped with exit code 23." in result.stdout
    assert "Review the reason above before opening it again." in result.stdout
    assert "Press any key to continue" in result.stdout


def test_ui_missing_environment_reports_actionable_error(tmp_path):
    launcher, unrelated = _copy_launcher(tmp_path)
    result = _run(launcher, unrelated)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Ducketz Python environment is missing." in result.stdout
    assert "Press any key to continue" in result.stdout
    assert "SYNTHETIC_UI_RUNTIME_ONLY" not in result.stdout
