"""Offline Windows launcher regressions; never invoke the real launcher body."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell launcher")
REPOSITORY = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
POLICY = "gameplan-direction-current-market-v1"


def _powershell(script, *arguments):
    return subprocess.run(
        [str(POWERSHELL), "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script), *map(str, arguments)],
        cwd=REPOSITORY, capture_output=True, text=True, timeout=40,
    )


def test_existing_owner_guard_and_worker_output(tmp_path):
    identity = _powershell(REPOSITORY / "tests/test_start_stock_session.ps1")
    assert identity.returncode == 0, identity.stdout + identity.stderr
    child = _powershell(REPOSITORY / "tests/test_start_stock_session_exit.ps1",
                        "-OutputDirectory", tmp_path / "synthetic-child", "-PythonPath", sys.executable)
    assert child.returncode == 0, child.stdout + child.stderr
    assert "Three real Windows launcher-tail regressions passed" in child.stdout


def _account_fixture(root, state):
    if state == "absent":
        return
    directory = root / "state/account-gameplan"
    directory.mkdir(parents=True)
    config = {
        "schema_version": "sole-coordinator-account-gameplan-v1",
        "machine_id": "pc-new" if state == "producer" else "pc-original",
        "coordinator_id": "pc-original",
        "participants": {"pc-original": ["AAA"], "pc-new": ["BBB"]},
        "account_fingerprint": "a" * 64,
    }
    binding = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    activation = {"status": "PREPARING" if state == "preparing" else "ACTIVE", "binding_sha256": binding}
    if state in {"active", "tampered"}:
        receipt = {
            "schema_version": config["schema_version"], "status": "VERIFIED", "binding_sha256": binding,
            "machine_id": config["machine_id"], "coordinator_id": config["coordinator_id"],
            "peer_execution_fenced": True, "peer_fence_receipt_sha256": "b" * 64,
            "migration_manifest_sha256": "c" * 64, "fresh_union_reconciliation_sha256": "d" * 64,
            "installed_source_commit": "e" * 40, "orders_placed": 0,
        }
        raw = json.dumps(receipt).encode()
        receipt_path = directory / "cutovers/fixture.json"
        receipt_path.parent.mkdir()
        receipt_path.write_bytes(raw + (b" " if state == "tampered" else b""))
        activation.update(receipt_path="state/account-gameplan/cutovers/fixture.json",
                          receipt_sha256=hashlib.sha256(raw).hexdigest())
    config["activation"] = activation
    (directory / "config.json").write_text(json.dumps(config), encoding="utf-8")


@pytest.mark.parametrize(("state", "accepted", "reason"), [
    ("absent", True, "Account startup checks passed"),
    ("active", True, "Account startup checks passed"),
    ("preparing", False, "COMBINED_ACCOUNT_CUTOVER_NOT_ACTIVE"),
    ("producer", False, "FORECAST_PRODUCER_HAS_NO_ORDER_AUTHORITY"),
    ("missing_receipt", False, "Account reference must be a relative immutable path"),
    ("tampered", False, "Account cutover receipt changed"),
])
def test_read_only_preflight_blocks_before_manual_controls(tmp_path, state, accepted, reason):
    root = tmp_path / "fixture-datastore"
    root.mkdir()
    _account_fixture(root, state)
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    runner = tmp_path / "preflight-only.ps1"
    # Exercise the actual startup sequence with a sentinel replacing control
    # writes. Never evaluate datastore resolution, CIM, or Start-Process.
    runner.write_text(r'''
param([string]$Repository, [string]$PythonPath, [string]$FixtureRoot, [string]$Policy)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$parseErrors = $null
$launcher = Join-Path $Repository 'docs/datafetch-ml/start_stock_session.ps1'
$ast = [Management.Automation.Language.Parser]::ParseFile($launcher, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw ($parseErrors | Out-String) }
foreach ($name in @('Assert-StockSessionPreflight', 'Get-StockSessionArguments')) {
    $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $false)
    if ($null -eq $definition) { throw "Missing launcher function $name." }
    Invoke-Expression $definition.Extent.Text
}
function Enable-ManualGameplanTrading {
    param([string]$PythonPath, [string]$DatastoreRoot)
    Write-Output 'MANUAL_CONTROL_STEP_REACHED'
}
$start = $ast.Find({ param($node) $node -is [Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -eq '$stdout' }, $false)
$end = $ast.Find({ param($node) $node -is [Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -eq '$process' -and $node.Right.Extent.Text -match '^Start-Process\s' }, $false)
if ($null -eq $start -or $null -eq $end) { throw 'Bounded startup sequence missing.' }
$sequence = (Get-Content -LiteralPath $launcher -Raw).Substring($start.Extent.StartOffset, $end.Extent.StartOffset - $start.Extent.StartOffset)
$logDirectory = $FixtureRoot
$datastoreRoot = $FixtureRoot
$SizingPolicy = $Policy
$WaitForOpen = [switch]$true
$ActivateForManualStart = $true
Set-Location -LiteralPath $Repository
Invoke-Expression $sequence
''', encoding="utf-8")
    result = _powershell(runner, "-Repository", REPOSITORY, "-PythonPath", sys.executable,
                         "-FixtureRoot", root, "-Policy", POLICY)
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    assert reason in result.stdout
    assert ("MANUAL_CONTROL_STEP_REACHED" in result.stdout) is accepted
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    if state == "preparing":
        assert "A completed nightly Gameplan handoff does not activate execution" in result.stdout
        assert "before changing manual controls or launching a worker" in result.stderr
