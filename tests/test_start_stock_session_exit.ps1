param([Parameter(Mandatory=$true)][string]$OutputDirectory)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
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
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$outputRoot = (Resolve-Path -LiteralPath $OutputDirectory).Path
$child = Join-Path $outputRoot 'synthetic-exit.py'
@'
import sys, time
print("SYNTHETIC_EXIT_ONLY", flush=True)
time.sleep(0.2)
sys.exit(int(sys.argv[1]))
'@ | Set-Content -LiteralPath $child -Encoding utf8
$runner = Join-Path $outputRoot 'launcher-tail.ps1'
$prelude = @'
param([string]$Repository,[string]$OutputRoot,[string]$Child,[int]$Code)
$ErrorActionPreference='Stop'
$pythonPath=Join-Path $Repository '.venv\Scripts\python.exe'
$arguments=@('-u', ('"'+$Child+'"'), [string]$Code)
$repoRoot=$Repository
$logDirectory=Join-Path $OutputRoot ('case-'+$Code)
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$stdout=Join-Path $logDirectory 'stdout.log'
$stderr=Join-Path $logDirectory 'stderr.log'
'@
($prelude + [Environment]::NewLine + $tail) | Set-Content -LiteralPath $runner -Encoding utf8
$windowsPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
foreach ($code in @(7, 0, 1)) {
    & $windowsPowerShell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $runner -Repository $repository -OutputRoot $outputRoot -Child $child -Code $code
    $actual = $LASTEXITCODE
    if ($actual -ne $code) { throw "Child exit $code became launcher exit $actual." }
    $caseDirectory = Join-Path $outputRoot ('case-'+$code)
    if ((Get-Content -LiteralPath (Join-Path $caseDirectory 'stdout.log') -Raw).Trim() -ne 'SYNTHETIC_EXIT_ONLY') { throw 'Synthetic child output was not captured.' }
    if ((Get-Item -LiteralPath (Join-Path $caseDirectory 'stderr.log')).Length -ne 0) { throw 'Synthetic child wrote stderr.' }
    Write-Output "Redirected virtualenv child exit $code correctly propagated."
}
Write-Output 'Three real Windows launcher-tail regressions passed; no trader, controls or broker calls.'
