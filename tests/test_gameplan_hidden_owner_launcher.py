"""Offline owner discovery; never launch a trader or access a real datastore."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@pytest.mark.parametrize(("case", "blocked", "count"), [
    ("hidden-command", True, 0),
    ("hidden-path", True, 0),
    ("hidden-with-visible-parent", True, 0),
    ("visible-pair", False, 2),
    ("exited-owner", False, 0),
    ("reused-pid", False, 0),
    ("no-lock", False, 0),
])
def test_discovery_uses_exact_live_lock_owner(tmp_path, case, blocked, count):
    runner = tmp_path / "discovery.ps1"
    runner.write_text(r'''
param([string]$Repository, [string]$Case)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $Repository 'docs/datafetch-ml/start_stock_session.ps1'),[ref]$tokens,[ref]$errors)
if($errors.Count -ne 0){throw ($errors | Out-String)}
$definition=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-StockSessionOwnerCandidates'},$false)
Invoke-Expression $definition.Extent.Text
$birth=[DateTimeOffset]'2026-10-20T08:00:02.858726Z'
$command='"C:\fixture\python.exe" -u -m ml.gameplan_stock_trader --run-session'
$worker=[pscustomobject]@{Name='python.exe';ProcessId=120;ParentProcessId=119;CreationDate=$birth;CommandLine=$null;ExecutablePath=$null}
$parent=[pscustomobject]@{Name='python.exe';ProcessId=119;ParentProcessId=118;CreationDate=$birth.AddMilliseconds(-10);CommandLine=$command;ExecutablePath='C:\fixture\python.exe'}
$lock="process=independent-stock-session`r`npid=120`r`nowner_created_at=1792483202.8587265`r`n"
$processes=@($worker)
switch($Case){
 'hidden-path' {$worker.CommandLine=$command}
 'hidden-with-visible-parent' {$processes=@($parent,$worker)}
 'visible-pair' {$worker.CommandLine=$command;$worker.ExecutablePath='C:\fixture\python.exe';$processes=@($parent,$worker)}
 'exited-owner' {$processes=@()}
 'reused-pid' {$worker.CreationDate=$birth.AddMinutes(1)}
 'no-lock' {$lock=''}
}
try {
 $owners=@(Get-StockSessionOwnerCandidates -Processes $processes -LockText $lock)
 Write-Output "DISCOVERED=$($owners.Count)"
} catch {
 Write-Output $_.Exception.Message
 exit 7
}
''', encoding="utf-8")
    result = subprocess.run([str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner),
                             "-Repository", str(ROOT), "-Case", case],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == (7 if blocked else 0), result.stdout + result.stderr
    if blocked:
        assert "PID 120 is still running" in result.stdout
        assert "No new worker was started" in result.stdout
        assert "PowerShell as administrator" in result.stdout
    else:
        assert f"DISCOVERED={count}" in result.stdout


def test_hidden_owner_check_precedes_control_or_start():
    source = (ROOT / "docs/datafetch-ml/start_stock_session.ps1").read_text()
    body = source[source.index("$processInventory ="):]
    guard = body.index("Get-StockSessionOwnerCandidates")
    assert guard < body.index("Enable-ManualGameplanTrading") < body.index("$process = Start-Process")
    command = (ROOT / "Start-Gameplan-Trader.cmd").read_text()
    assert "A pre-existing trader may still be running" in command
    assert "does not leave its trader running after it closes" not in command
