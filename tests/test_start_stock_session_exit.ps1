param(
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [string]$PythonPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($PythonPath)) { $PythonPath = Join-Path $repository '.venv\Scripts\python.exe' }
$launcherPath = Join-Path $repository 'docs\datafetch-ml\start_stock_session.ps1'
$parseErrors = $null
$tokens = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($launcherPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw ($parseErrors | Out-String) }
# Execute only the real launch/wait tail, supplying a harmless synthetic child.
# The native launcher's datastore, controls, ownership and trader are never run.
$start = $ast.Find({ param($node)
    $node -is [Management.Automation.Language.AssignmentStatementAst] -and
    $node.Left.Extent.Text -eq '$process' -and
    $node.Right.Extent.Text -match '^Start-Process\s'
}, $false)
if ($null -eq $start) { throw 'The process launch assignment was not found.' }
$tail = (Get-Content -LiteralPath $launcherPath -Raw).Substring($start.Extent.StartOffset)
$outputFunctions = foreach ($name in @('Write-StockSessionLogOutput', 'Wait-StockSessionWithOutput', 'Enable-ManualStockSessionLifetime')) {
    $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $false)
    if ($null -eq $definition) { throw "Missing launcher output helper $name." }
    $definition.Extent.Text
}
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$outputRoot = (Resolve-Path -LiteralPath $OutputDirectory).Path
$child = Join-Path $outputRoot 'synthetic-exit.py'
@'
import sys, time
print('{"status":"SLEEPING_UNTIL_OPEN","fixture":true}', flush=True)
time.sleep(0.7)
print("SYNTHETIC_STDERR_ONLY", file=sys.stderr, flush=True)
print("SYNTHETIC_FINAL_OUTPUT", end="", flush=True)
sys.exit(int(sys.argv[1]))
'@ | Set-Content -LiteralPath $child -Encoding utf8
$runner = Join-Path $outputRoot 'launcher-tail.ps1'
$prelude = @'
param([string]$Repository,[string]$OutputRoot,[string]$Child,[int]$Code,[string]$PythonPath,[switch]$Manual)
$ErrorActionPreference='Stop'
$ActivateForManualStart=$Manual.IsPresent
$arguments=@('-u', ('"'+$Child+'"'), [string]$Code)
$repoRoot=$Repository
$logDirectory=Join-Path $OutputRoot ('case-'+$Code+'-manual-'+$Manual.IsPresent)
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stdout=Join-Path $logDirectory 'stdout.log'
$stderr=Join-Path $logDirectory 'stderr.log'
'@
($prelude + [Environment]::NewLine + ($outputFunctions -join [Environment]::NewLine) + [Environment]::NewLine + 'if($Manual){Enable-ManualStockSessionLifetime}' + [Environment]::NewLine + $tail) | Set-Content -LiteralPath $runner -Encoding utf8
$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
foreach ($manual in @($false,$true)) {
foreach ($code in @(7, 0, 1)) {
    $manualArguments=@()
    if($manual){$manualArguments=@('-Manual')}
    $visible = @(& $windowsPowerShell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner -Repository $repository -OutputRoot $outputRoot -Child $child -Code $code -PythonPath $PythonPath @manualArguments) -join "`n"
    $actual = $LASTEXITCODE
    if ($actual -ne $code) { throw "Child exit $code became launcher exit $actual." }
    $caseDirectory = Join-Path $outputRoot ('case-'+$code+'-manual-'+$manual)
    foreach ($text in @('SLEEPING_UNTIL_OPEN', 'SYNTHETIC_STDERR_ONLY', 'SYNTHETIC_FINAL_OUTPUT', 'Output log:', 'Error log:', "exit code $code")) {
        if (-not $visible.Contains($text)) { throw "The launcher hid expected output: $text" }
    }
    if (@([regex]::Matches($visible, 'SYNTHETIC_FINAL_OUTPUT')).Count -ne 1) { throw 'Final worker output was lost or duplicated.' }
    if ((Get-Content -LiteralPath (Join-Path $caseDirectory 'stdout.log') -Raw) -notmatch 'SYNTHETIC_FINAL_OUTPUT$') { throw 'Synthetic final output was not captured.' }
    if ((Get-Content -LiteralPath (Join-Path $caseDirectory 'stderr.log') -Raw).Trim() -ne 'SYNTHETIC_STDERR_ONLY') { throw 'Synthetic stderr was not captured.' }
    Write-Output "Redirected virtualenv child exit $code correctly propagated."
}
}
Write-Output 'Six real Windows launcher-tail regressions passed; no trader, controls or broker calls.'
