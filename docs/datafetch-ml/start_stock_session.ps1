param(
    [switch]$WaitForOpen,
    [ValidateSet('fixed-horizon-budget-v1', 'gameplan-direction-current-market-v1')]
    [string]$SizingPolicy = 'fixed-horizon-budget-v1',
    [switch]$ActivateForManualStart
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-ValidatedStockSessionOwner {
    param(
        [object[]]$Owners,
        [string]$PythonPath,
        [string]$BasePythonPath,
        [string]$LockText,
        [DateTimeOffset]$ObservedAt = [DateTimeOffset]::UtcNow
    )

    # Match the complete deployed command. Substring checks also accepted
    # conflicting repeated arguments or a different executable containing it.
    $executable = '(?i:' + [regex]::Escape($PythonPath) + ')'
    $executablePattern = '"' + $executable + '"'
    if ($PythonPath -notmatch '\s') { $executablePattern = '(?:' + $executablePattern + '|' + $executable + ')' }
    $commandPattern = '\A' + $executablePattern + '[ \t]+-u[ \t]+-m[ \t]+ml\.gameplan_stock_trader[ \t]+--datastore-target[ \t]+pc[ \t]+--execute[ \t]+--target-horizon[ \t]+all[ \t]+--sizing-policy[ \t]+(?<policy>fixed-horizon-budget-v1|gameplan-direction-current-market-v1)[ \t]+--run-session(?<wait>[ \t]+--wait-for-open)?(?:[ \t]+--late-opening-date[ \t]+(?<late>\d{4}-\d{2}-\d{2}))?(?:[ \t]+--resume-quote-run[ \t]+(?<resume>\d{8}T\d{6}\.\d{6}Z)[ \t]+--resume-quote-symbol[ \t]+(?<symbol>[A-Z][A-Z0-9.\-]{0,9}))?[ \t]*\z'
    if ($Owners.Count -ne 2 -or @($Owners | Where-Object {
        $_.Name -ine 'python.exe' -or -not [regex]::IsMatch([string]$_.CommandLine, $commandPattern)
    }).Count -ne 0) {
        throw 'Existing stock session commands do not match the deployed bounded worker.'
    }
    $commandIdentities = @($Owners | ForEach-Object {
        $matchedCommand = [regex]::Match([string]$_.CommandLine, $commandPattern)
        $matchedCommand.Groups['policy'].Value + '/' + $matchedCommand.Groups['wait'].Success + '/' + $matchedCommand.Groups['late'].Value + '/' + $matchedCommand.Groups['resume'].Value + '/' + $matchedCommand.Groups['symbol'].Value
    } | Select-Object -Unique)
    if ($commandIdentities.Count -ne 1) {
        throw 'Existing stock session launcher and child commands disagree on their policy or wait mode.'
    }
    $ownerIds = @($Owners | ForEach-Object { [int]$_.ProcessId })
    $workers = @($Owners | Where-Object { [int]$_.ParentProcessId -in $ownerIds })
    if (@($ownerIds | Select-Object -Unique).Count -ne 2 -or @($ownerIds | Where-Object { $_ -le 0 }).Count -ne 0 -or $workers.Count -ne 1) {
        throw 'Existing stock session launcher and child ownership is ambiguous.'
    }
    $worker = $workers[0]
    $launcher = @($Owners | Where-Object { [int]$_.ProcessId -eq [int]$worker.ParentProcessId })[0]
    if ([int]$launcher.ProcessId -eq [int]$worker.ProcessId -or
        [string]$launcher.ExecutablePath -ine $PythonPath -or
        [string]$worker.ExecutablePath -ine $BasePythonPath) {
        throw 'Existing stock session executable identities do not match the virtualenv pair.'
    }

    $lockFields = @{}
    foreach ($line in ($LockText -split '\r?\n')) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch '\A([a-z_]+)=(.+)\z' -or $lockFields.ContainsKey($Matches[1])) {
            throw 'Existing stock session lock is malformed or has duplicate fields.'
        }
        $lockFields[$Matches[1]] = $Matches[2]
    }
    $lockPid = 0
    $lockStartedAt = [DateTimeOffset]::MinValue
    if ($lockFields.Count -ne 4 -or $lockFields['process'] -cne 'independent-stock-session' -or
        -not [int]::TryParse($lockFields['pid'], [ref]$lockPid) -or $lockPid -ne [int]$worker.ProcessId -or
        $lockFields['token'] -cnotmatch '\A[0-9a-f]{32}\z' -or
        $lockFields['started_at'] -notmatch '(?:Z|[+-]\d{2}:\d{2})\z' -or
        -not [DateTimeOffset]::TryParse($lockFields['started_at'], [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$lockStartedAt)) {
        throw 'Existing stock session lock does not identify its living worker.'
    }
    if ($null -eq $launcher.CreationDate -or $null -eq $worker.CreationDate) {
        throw 'Existing stock session process creation times are unavailable.'
    }
    $launcherCreatedAt = [DateTimeOffset]$launcher.CreationDate
    $workerCreatedAt = [DateTimeOffset]$worker.CreationDate
    $workerArguments = [regex]::Match([string]$worker.CommandLine, $commandPattern)
    if ($workerArguments.Groups['resume'].Success) {
        $pacificStart = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId($workerCreatedAt, 'Pacific Standard Time')
        if ($workerArguments.Groups['policy'].Value -cne 'gameplan-direction-current-market-v1' -or
            $workerArguments.Groups['late'].Success -or
            -not $workerArguments.Groups['resume'].Value.StartsWith($pacificStart.ToString('yyyyMMdd'))) {
            throw 'Quote recovery does not match this Gameplan worker and action date.'
        }
    }
    if ($workerArguments.Groups['late'].Success) {
        $pacificStart = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId($workerCreatedAt, 'Pacific Standard Time')
        if ($workerArguments.Groups['policy'].Value -cne 'gameplan-direction-current-market-v1' -or
            $workerArguments.Groups['late'].Value -cne $pacificStart.ToString('yyyy-MM-dd') -or $pacificStart.Hour -ne 4) {
            throw 'Late-opening exception does not match this Gameplan worker and opening date.'
        }
    }
    # Acquisition follows interpreter startup; do not require equal timestamps.
    # A lock predating this process belongs to an older instance of the PID.
    if ($launcherCreatedAt -gt $workerCreatedAt -or $workerCreatedAt -gt $lockStartedAt -or $lockStartedAt -gt $ObservedAt) {
        throw 'Existing stock session creation times do not match its lock lifetime.'
    }
    [pscustomobject]@{
        launcher_pid = [int]$launcher.ProcessId
        worker_pid = [int]$worker.ProcessId
        launcher_created_at = $launcherCreatedAt.ToUniversalTime().ToString('o')
        worker_created_at = $workerCreatedAt.ToUniversalTime().ToString('o')
        lock_started_at = $lockStartedAt.ToUniversalTime().ToString('o')
        sizing_policy = [regex]::Match([string]$worker.CommandLine, $commandPattern).Groups['policy'].Value
        wait_for_open = [regex]::Match([string]$worker.CommandLine, $commandPattern).Groups['wait'].Success
        late_opening_date = $workerArguments.Groups['late'].Value
        resume_quote_run = $workerArguments.Groups['resume'].Value
        resume_quote_symbol = $workerArguments.Groups['symbol'].Value
    }
}

function Get-StockSessionArguments {
    param([string]$Policy, [bool]$Wait)
    @('-u', '-m', 'ml.gameplan_stock_trader', '--datastore-target', 'pc', '--execute', '--target-horizon', 'all', '--sizing-policy', $Policy, '--run-session')
    if ($Wait) { '--wait-for-open' }
}

function Enable-ManualGameplanTrading {
    param([string]$PythonPath, [string]$DatastoreRoot)
    # This is called only by the user's explicit manual-start option. There is
    # no activation prompt or second interaction when the worker wakes.
    @'
import sys
from pathlib import Path
from ml.stock_trader.control import write_activation_intent
from ml.stock_trader.gameplan import write_gameplan_stock_activation_intent
root = Path(sys.argv[1]).resolve()
write_activation_intent(root, active=True)
write_gameplan_stock_activation_intent(root, active=True)
print("Gameplan trader enabled by manual start. It will wait for its session if needed.")
'@ | & $PythonPath - $DatastoreRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not enable the manual Gameplan trader.' }
}

if ($ActivateForManualStart -and (-not $WaitForOpen -or $SizingPolicy -cne 'gameplan-direction-current-market-v1')) {
    throw 'Manual activation requires the Gameplan policy and -WaitForOpen.'
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$pythonPath = (Resolve-Path -LiteralPath (Join-Path $repoRoot '.venv\Scripts\python.exe')).Path
Set-Location -LiteralPath $repoRoot
$runtimeText = @'
import json
import sys
from datafetching.parquet_store import resolve_datastore_dir
print(json.dumps({"datastore": str(resolve_datastore_dir(target="pc").resolve()), "base_python": sys._base_executable}))
'@ | & $pythonPath -
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the production datastore.' }
$runtimePaths = ([string]$runtimeText) | ConvertFrom-Json
$datastoreRoot = (Resolve-Path -LiteralPath $runtimePaths.datastore).Path
$basePythonPath = (Resolve-Path -LiteralPath $runtimePaths.base_python).Path
$sessionLock = Join-Path $datastoreRoot 'locks\independent-stock-session.lock'
$sessionStatus = Join-Path $datastoreRoot 'state\independent-stock-trader\session-status.json'
$logDirectory = Join-Path $datastoreRoot ('logs\daytime-operations\' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmss.fffffffZ'))
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

# The OS task is the timed process owner. The native runtime retains all
# exchange-calendar, activation, forecast, entry-slot and broker safeguards.
$owners = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @('python.exe', 'pythonw.exe') -and
    $_.CommandLine -match '(?:^|\s)-m\s+"?ml\.gameplan_stock_trader"?(?:\s|$)'
})
if ($owners.Count -gt 0) {
    if (-not (Test-Path -LiteralPath $sessionLock)) {
        throw 'Existing stock session ownership is ambiguous; no second worker was launched.'
    }
    $identity = Get-ValidatedStockSessionOwner -Owners $owners -PythonPath $pythonPath -BasePythonPath $basePythonPath `
        -LockText (Get-Content -LiteralPath $sessionLock -Raw)
    if ($ActivateForManualStart -and $identity.sizing_policy -cne $SizingPolicy) {
        throw 'A stock worker is already running another strategy. Stop it before manually starting the Gameplan trader.'
    }
    $workerPid = $identity.worker_pid
    $workerProcess = Get-Process -Id $workerPid
    $launcherProcess = Get-Process -Id $identity.launcher_pid
    # Bind the wait to the same instances inspected above, including PID reuse
    # between the CIM snapshot and opening their process handles.
    foreach ($pair in @(@($workerProcess, $identity.worker_created_at), @($launcherProcess, $identity.launcher_created_at))) {
        if ([Math]::Abs(($pair[0].StartTime.ToUniversalTime() - ([DateTimeOffset]$pair[1]).UtcDateTime).Ticks) -gt 10) {
            throw 'Existing stock session process identity changed during adoption.'
        }
    }
    if ($ActivateForManualStart) { Enable-ManualGameplanTrading -PythonPath $pythonPath -DatastoreRoot $datastoreRoot }
    [pscustomobject]@{ status='SUPERVISING_EXISTING_WORKER'; worker_pid=$workerPid; launcher_pid=$identity.launcher_pid; worker_created_at=$identity.worker_created_at; launcher_created_at=$identity.launcher_created_at; lock_started_at=$identity.lock_started_at; observed_at=[DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logDirectory 'launcher.json') -Encoding utf8
    $workerProcess | Wait-Process
    if (Test-Path -LiteralPath $sessionStatus) {
        $terminal = Get-Content -LiteralPath $sessionStatus -Raw | ConvertFrom-Json
        if ($terminal.pid -eq $workerPid -and $terminal.status -in @('FINISHED', 'STOPPED_TRADER_INACTIVE', 'STOPPED_INTERRUPTED')) { exit 0 }
    }
    # A disappeared owner without a verified normal termination is a failed
    # OS task, allowing its configured bounded restart policy to take effect.
    exit 1
}

$stdout = Join-Path $logDirectory 'stock-session.stdout.log'
$stderr = Join-Path $logDirectory 'stock-session.stderr.log'
$arguments = @(Get-StockSessionArguments -Policy $SizingPolicy -Wait $WaitForOpen.IsPresent)
if ($ActivateForManualStart) { Enable-ManualGameplanTrading -PythonPath $pythonPath -DatastoreRoot $datastoreRoot }
$process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $repoRoot `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
# Windows PowerShell can lose a redirected child's exit code after it exits
# unless its process handle was retained first. A null code becomes exit 0.
$null = $process.Handle
[pscustomobject]@{ status='STARTED'; launcher_pid=$process.Id; started_at=[DateTime]::UtcNow.ToString('o'); stdout=$stdout; stderr=$stderr } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logDirectory 'launcher.json') -Encoding utf8
$process.WaitForExit()
$process.Refresh()
if ($null -eq $process.ExitCode) { exit 1 }
exit $process.ExitCode
