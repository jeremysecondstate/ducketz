"""Offline retirement checks: synthetic launchers and mocked scheduler reads."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def ps(value):
    return "'" + str(value).replace("'", "''") + "'"


class GameplanRetirementTests(unittest.TestCase):
    def test_retired_launcher_exits_before_missing_runtime_or_forwarded_arguments(self):
        shell = shutil.which("cmd.exe")
        if not shell:
            self.skipTest("Windows cmd.exe required for batch integration")
        with tempfile.TemporaryDirectory() as temporary:
            launcher = Path(temporary) / "Start-Gameplan-Trader.cmd"
            launcher.write_bytes((ROOT / launcher.name).read_bytes())
            result = subprocess.run(
                [shell, "/d", "/c", str(launcher), "--trader", "gameplan"],
                cwd=temporary, text=True, capture_output=True, timeout=10,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("Gameplan Trader is retired", result.stdout)
            self.assertNotIn("Python environment is missing", result.stdout)
            self.assertEqual(list(Path(temporary).iterdir()), [launcher])

    def test_registration_rejected_before_paths_or_native_cmdlets(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell required for mocked registration integration")
        script = ROOT / "tools/register_nightly_watchdog.ps1"
        command = (
            "$ErrorActionPreference='Stop'; "
            "function Get-ScheduledTask { throw 'NATIVE_ACCESS_ATTEMPTED' }; "
            "function Register-ScheduledTask { throw 'NATIVE_MUTATION_ATTEMPTED' }; "
            "function Set-ScheduledTask { throw 'NATIVE_MUTATION_ATTEMPTED' }; "
            "try { & " + ps(script)
            + " -Repository 'missing' -Python 'missing' -Config 'missing' -TaskName 'historical'; "
            "throw 'REGISTRATION_UNEXPECTEDLY_ALLOWED' } catch { Write-Output $_.Exception.Message }"
        )
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                text=True, capture_output=True, timeout=15, check=True)
        self.assertIn("Gameplan nightly dispatch is retired", result.stdout)
        self.assertNotIn("ATTEMPTED", result.stdout)
        self.assertNotIn("UNEXPECTEDLY_ALLOWED", result.stdout)

    def test_historical_readonly_inspection_remains_available(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell required for mocked read-only integration")
        script = ROOT / "tools/register_nightly_watchdog.ps1"
        command = r"""
$ErrorActionPreference='Stop'
function Get-TimeZone { [pscustomobject]@{Id='Pacific Standard Time'} }
function Register-ScheduledTask { throw 'NATIVE_MUTATION_ATTEMPTED' }
function Set-ScheduledTask { throw 'NATIVE_MUTATION_ATTEMPTED' }
function Get-ScheduledTask {
 param($TaskName)
 [pscustomobject]@{TaskName=$TaskName;State='Disabled';Actions=@();Triggers=@();
 Settings=[pscustomobject]@{StartWhenAvailable=$true;MultipleInstances='IgnoreNew'};
 Principal=[pscustomobject]@{LogonType='Interactive'}}
}
function Get-ScheduledTaskInfo {
 param($TaskName)
 [pscustomobject]@{NextRunTime=[datetime]'2026-10-30';LastRunTime=[datetime]'2026-10-29';LastTaskResult=0}
}
& SCRIPT -Repository REPO -Python SHELL -Config SCRIPT -TaskName 'historical' -ReadOnly
""".replace("SCRIPT", ps(script)).replace("REPO", ps(ROOT)).replace("SHELL", ps(shell))
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command],
                                text=True, capture_output=True, timeout=15, check=True)
        observation = json.loads(result.stdout)
        self.assertEqual(observation["TaskName"], "historical")
        self.assertEqual(observation["State"], "Disabled")


if __name__ == "__main__":
    unittest.main()
