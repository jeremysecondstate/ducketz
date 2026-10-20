"""Exercise the launcher argument builder without running the launcher."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest


def test_manual_gameplan_launcher_uses_current_policy_without_starting_processes():
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for the pure launcher AST checks")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         "tests/test_start_stock_session.ps1"],
        cwd=root, capture_output=True, text=True, timeout=45, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    checks = re.search(r"(\d+) synthetic stock session identity checks passed", result.stdout)
    assert checks is not None, result.stdout
    assert int(checks.group(1)) >= 59
    assert "no processes were launched or stopped" in result.stdout
