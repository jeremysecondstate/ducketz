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
def test_scheduled_preflight_is_read_only(tmp_path, state, accepted, reason):
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
$ActivateForManualStart = $false
Set-Location -LiteralPath $Repository
Invoke-Expression $sequence
''', encoding="utf-8")
    result = _powershell(runner, "-Repository", REPOSITORY, "-PythonPath", sys.executable,
                         "-FixtureRoot", root, "-Policy", POLICY)
    assert (result.returncode == 0) is accepted, result.stdout + result.stderr
    assert reason in result.stdout
    assert "MANUAL_CONTROL_STEP_REACHED" not in result.stdout
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    if state == "preparing":
        assert "will finish account setup automatically" in result.stdout
        assert "before changing manual controls or launching a worker" in result.stderr


@pytest.mark.parametrize(("case", "accepted", "reason"), [
    ("ready", True, "Account setup ready"),
    ("ready_preparing", False, "COMBINED_ACCOUNT_CUTOVER_NOT_ACTIVE"),
    ("missing", False, "NATIVE_SCOUT_LEDGER_EXPORT"),
    ("unready_success_status", False, "ACCOUNT_SETUP_NOT_READY"),
    ("ready_wrong_status", False, "ACCOUNT_SETUP_NOT_READY"),
    ("private_reason", False, "ACCOUNT_SETUP_NOT_READY"),
    ("exception", False, "ACCOUNT_SETUP_ERROR (ValueError)"),
])
def test_explicit_manual_start_requires_setup_before_controls(tmp_path, case, accepted, reason):
    root = tmp_path / "synthetic-datastore"
    root.mkdir()
    _account_fixture(root, "preparing" if case == "ready_preparing" else "active")
    original = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    calls = tmp_path / "setup-calls.jsonl"
    harness = tmp_path / "python-fixture.py"
    harness.write_text(f'''
import json,sys,types
from pathlib import Path
sys.path.insert(0, {str(REPOSITORY)!r})
module=types.ModuleType("tools.native_ownership_cutover")
def ensure_gameplan_account_ready(root, *, sizing_policy):
    with Path({str(calls)!r}).open("a") as stream:
        stream.write(json.dumps({{"root":str(root),"policy":sizing_policy}})+"\\n")
    case={case!r}
    if case=="exception":
        raise ValueError("PRIVATE_RAW_BROKER_REPLY_123456789")
    return {{
        "ready": {{"ready":True,"status":"ACCOUNT_READY"}},
        "ready_preparing": {{"ready":True,"status":"ACCOUNT_READY"}},
        "missing": {{"ready":False,"status":"ACCOUNT_NOT_READY","reason":"NATIVE_SCOUT_LEDGER_EXPORT"}},
        "unready_success_status": {{"ready":False,"status":"ACCOUNT_READY"}},
        "ready_wrong_status": {{"ready":True,"status":"PENDING"}},
        "private_reason": {{"ready":False,"status":"ACCOUNT_NOT_READY","reason":"PRIVATE_RAW_BROKER_REPLY_123456789 = sensitive"}},
    }}[case]
module.ensure_gameplan_account_ready=ensure_gameplan_account_ready
sys.modules["tools.native_ownership_cutover"]=module
source=sys.stdin.read()
sys.argv=["-",*sys.argv[3:]]
exec(compile(source,"<actual-launcher-inline-python>","exec"))
''', encoding="utf-8")
    python_wrapper = tmp_path / "python-fixture.cmd"
    python_wrapper.write_text(f'@echo off\n"{sys.executable}" -B "{harness}" %*\nexit /b %errorlevel%\n', encoding="utf-8")
    runner = tmp_path / "manual-preflight-only.ps1"
    runner.write_text(r'''
param([string]$Repository,[string]$PythonPath,[string]$FixtureRoot,[string]$Policy)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null
$errors=$null
$launcher=Join-Path $Repository 'docs/datafetch-ml/start_stock_session.ps1'
$ast=[Management.Automation.Language.Parser]::ParseFile($launcher,[ref]$tokens,[ref]$errors)
if ($errors.Count -ne 0) { throw ($errors | Out-String) }
foreach ($name in @('Initialize-ManualGameplanAccount','Assert-StockSessionPreflight','Get-StockSessionArguments')) {
    $definition=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$false)
    if ($null -eq $definition) { throw "Missing function $name" }
    Invoke-Expression $definition.Extent.Text
}
function Enable-ManualGameplanTrading {
    param([string]$PythonPath,[string]$DatastoreRoot)
    Write-Output 'MANUAL_CONTROL_STEP_REACHED'
}
$start=$ast.Find({param($node) $node -is [Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -eq '$stdout'},$false)
$end=$ast.Find({param($node) $node -is [Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -eq '$process' -and $node.Right.Extent.Text -match '^Start-Process\s'},$false)
if ($null -eq $start -or $null -eq $end) { throw 'Bounded startup sequence missing' }
$sequence=(Get-Content -LiteralPath $launcher -Raw).Substring($start.Extent.StartOffset,$end.Extent.StartOffset-$start.Extent.StartOffset)
$logDirectory=$FixtureRoot
$datastoreRoot=$FixtureRoot
$SizingPolicy=$Policy
$WaitForOpen=[switch]$true
$ActivateForManualStart=$true
Set-Location -LiteralPath $Repository
Invoke-Expression $sequence
Write-Output 'WORKER_LAUNCH_BOUNDARY_REACHED'
''', encoding="utf-8")
    result = _powershell(runner, "-Repository", REPOSITORY, "-PythonPath", python_wrapper,
                         "-FixtureRoot", root, "-Policy", POLICY)
    output = result.stdout + result.stderr
    assert (result.returncode == 0) is accepted, output
    assert reason in output
    assert "PRIVATE_RAW_BROKER_REPLY" not in output
    assert ("MANUAL_CONTROL_STEP_REACHED" in output) is accepted
    assert ("WORKER_LAUNCH_BOUNDARY_REACHED" in output) is accepted
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [{"root": str(root), "policy": POLICY}]
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == original
    if accepted:
        assert output.index("Account setup ready") < output.index("Account startup checks passed") < output.index("MANUAL_CONTROL_STEP_REACHED")


def test_adopted_worker_path_cannot_invoke_account_setup(tmp_path):
    """Inspect the real PowerShell branch AST, rather than matching comments."""
    runner = tmp_path / "adoption-boundary.ps1"
    runner.write_text(r'''
param([string]$Repository)
$ErrorActionPreference='Stop'
$tokens=$null
$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $Repository 'docs/datafetch-ml/start_stock_session.ps1'),[ref]$tokens,[ref]$errors)
$branch=$ast.Find({param($node) $node -is [Management.Automation.Language.IfStatementAst] -and $node.Clauses[0].Item1.Extent.Text -eq '$owners.Count -gt 0'},$false)
if ($null -eq $branch) { throw 'Existing-owner branch missing' }
$commands=@($branch.FindAll({param($node) $node -is [Management.Automation.Language.CommandAst]},$true) | ForEach-Object { $_.GetCommandName() })
if ($commands -contains 'Initialize-ManualGameplanAccount') { throw 'Existing worker can migrate account ownership' }
if ($commands -notcontains 'Assert-StockSessionPreflight') { throw 'Existing worker lacks read-only account verification' }
Write-Output 'EXISTING_WORKER_USES_READ_ONLY_PREFLIGHT'
''', encoding="utf-8")
    result = _powershell(runner, "-Repository", REPOSITORY)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EXISTING_WORKER_USES_READ_ONLY_PREFLIGHT" in result.stdout
