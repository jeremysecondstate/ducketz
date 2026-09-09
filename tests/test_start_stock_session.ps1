$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Load only the pure validator AST. Never execute the launcher's top-level
# datastore lookup, process supervision, broker runtime or Start-Process.
$launcherPath = Join-Path $PSScriptRoot '..\docs\datafetch-ml\start_stock_session.ps1'
$parseErrors = $null
$tokens = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($launcherPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw ($parseErrors | Out-String) }
$validator = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-ValidatedStockSessionOwner' }, $false)
if ($null -eq $validator) { throw 'Session identity validator was not found.' }
Invoke-Expression $validator.Extent.Text

$script:passed = 0
$pythonPath = 'C:\test repo\.venv\Scripts\python.exe'
$basePythonPath = 'C:\Python313\python.exe'
$commandLine = '"' + $pythonPath + '" -u -m ml.gameplan_stock_trader --datastore-target pc --execute --target-horizon all --sizing-policy fixed-horizon-budget-v1 --run-session'
$lockText = "process=independent-stock-session`npid=202`nstarted_at=2026-09-08T21:36:07.195720+00:00`ntoken=0123456789abcdef0123456789abcdef`n"
$observedAt = [DateTimeOffset]'2026-09-08T22:00:00Z'

function New-Owners {
    @(
        [pscustomobject]@{ Name='python.exe'; ProcessId=101; ParentProcessId=99; ExecutablePath=$pythonPath; CommandLine=$commandLine; CreationDate=[DateTimeOffset]'2026-09-08T21:36:05.662035Z' },
        [pscustomobject]@{ Name='python.exe'; ProcessId=202; ParentProcessId=101; ExecutablePath=$basePythonPath; CommandLine=$commandLine; CreationDate=[DateTimeOffset]'2026-09-08T21:36:05.689795Z' }
    )
}

function Assert-Accepted {
    param([string]$Name, [object[]]$Owners = (New-Owners), [string]$Lock = $lockText, [string]$Python = $pythonPath)
    $identity = Get-ValidatedStockSessionOwner -Owners $Owners -PythonPath $Python -BasePythonPath $basePythonPath -LockText $Lock -ObservedAt $observedAt
    if ($identity.worker_pid -ne 202 -or $identity.launcher_pid -ne 101) { throw "$Name returned the wrong process pair." }
    $script:passed++
}

function Assert-Rejected {
    param([string]$Name, [object[]]$Owners = (New-Owners), [string]$Lock = $lockText)
    $rejected = $false
    try {
        $null = Get-ValidatedStockSessionOwner -Owners $Owners -PythonPath $pythonPath -BasePythonPath $basePythonPath -LockText $Lock -ObservedAt $observedAt
    } catch { $rejected = $true }
    if (-not $rejected) { throw "$Name was incorrectly accepted." }
    $script:passed++
}

Assert-Accepted 'Exact launcher/child pair with normal lock acquisition lag'
$owners = New-Owners
$owners[0].CommandLine = $commandLine.Replace($pythonPath, $pythonPath.ToUpperInvariant())
Assert-Accepted 'Windows executable path case is insensitive' -Owners $owners
$owners = New-Owners
$owners[1].CreationDate = [DateTimeOffset]'2026-09-08T14:36:05.689795-07:00'
Assert-Accepted 'Process timestamp UTC conversion' -Owners $owners
$owners = New-Owners
$unspacedPythonPath = 'C:\repo\.venv\Scripts\python.exe'
foreach ($owner in $owners) { $owner.CommandLine = $commandLine.Replace(('"' + $pythonPath + '"'), $unspacedPythonPath) }
$owners[0].ExecutablePath = $unspacedPythonPath
Assert-Accepted 'Unquoted executable path without spaces' -Owners $owners -Python $unspacedPythonPath

foreach ($suffix in @(' --execute', ' --datastore-target other', ' --target-horizon 1h', ' --sizing-policy another-policy', ' --unknown', ' # comment')) {
    $owners = New-Owners
    $owners[1].CommandLine += $suffix
    Assert-Rejected "Trailing argument $suffix" -Owners $owners
}
foreach ($replacement in @(
    @('--execute', '--execution'),
    @('-u -m', '-m'),
    @('ml.gameplan_stock_trader', 'ML.gameplan_stock_trader'),
    @(('"' + $pythonPath + '"'), $pythonPath),
    @(('"' + $pythonPath + '"'), ('"' + $pythonPath + '.other.exe"')),
    @('--run-session', "--run-session`n")
)) {
    $owners = New-Owners
    $owners[0].CommandLine = $commandLine.Replace($replacement[0], $replacement[1])
    Assert-Rejected 'Nonmatching complete command' -Owners $owners
}
Assert-Rejected 'Single orphan process' -Owners @((New-Owners)[1])
Assert-Rejected 'Extra session process' -Owners @((New-Owners) + (New-Owners)[0])
$owners = New-Owners
$owners[1].ParentProcessId = 303
Assert-Rejected 'Child belongs to unrelated launcher' -Owners $owners
$owners = New-Owners
$owners[0].ParentProcessId = 202
Assert-Rejected 'Cyclic parent relationship' -Owners $owners
$owners = New-Owners
$owners[1].ProcessId = 101
Assert-Rejected 'Duplicate process IDs' -Owners $owners
$owners = New-Owners
$owners[0].ExecutablePath = $basePythonPath
Assert-Rejected 'Launcher executable differs from command' -Owners $owners
$owners = New-Owners
$owners[1].ExecutablePath = 'C:\unrelated\python.exe'
Assert-Rejected 'Child executable differs from configured base interpreter' -Owners $owners
$owners = New-Owners
$owners[1].Name = 'pythonw.exe'
Assert-Rejected 'Unexpected interpreter image' -Owners $owners
$owners = New-Owners
$owners[0].CreationDate = [DateTimeOffset]'2026-09-08T21:36:06Z'
Assert-Rejected 'Reused parent PID newer than child' -Owners $owners
$owners = New-Owners
$owners[1].CreationDate = [DateTimeOffset]'2026-09-08T21:36:08Z'
Assert-Rejected 'Reused child PID newer than lock' -Owners $owners
$owners = New-Owners
$owners[1].CreationDate = $null
Assert-Rejected 'Missing creation timestamp' -Owners $owners
foreach ($replacement in @(
    @('pid=202', 'pid=101'),
    @('process=independent-stock-session', 'process=overnight-runtime'),
    @('2026-09-08T21:36:07.195720+00:00', '2026-09-08T21:36:05Z'),
    @('2026-09-08T21:36:07.195720+00:00', '2026-09-09T00:00:00Z'),
    @('2026-09-08T21:36:07.195720+00:00', '2026-09-08T21:36:07'),
    @('2026-09-08T21:36:07.195720+00:00', 'invalid'),
    @('token=0123456789abcdef0123456789abcdef', 'token=invalid')
)) {
    Assert-Rejected 'Invalid lock identity field' -Lock $lockText.Replace($replacement[0], $replacement[1])
}
Assert-Rejected 'Duplicate lock PID' -Lock ($lockText + "pid=202`n")
Assert-Rejected 'Malformed lock line' -Lock ($lockText + "malformed`n")
Assert-Rejected 'Missing lock' -Lock ''
Write-Output "$script:passed synthetic stock session identity checks passed; no processes were launched or stopped."
