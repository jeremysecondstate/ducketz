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
    if (($lockFields.Count -ne 4 -and $lockFields.Count -ne 5) -or
        ($lockFields.Count -eq 5 -and -not $lockFields.ContainsKey('owner_created_at')) -or
        $lockFields['process'] -cne 'independent-stock-session' -or
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
    if ($lockFields.ContainsKey('owner_created_at')) {
        $lockBirth = 0.0
        $epochTicks = ([DateTimeOffset]'1970-01-01T00:00:00Z').UtcDateTime.Ticks
        $workerBirth = ($workerCreatedAt.UtcDateTime.Ticks - $epochTicks) / 10000000.0
        # Current runtime locks bind the owner's Unix process-birth timestamp.
        # CIM has microsecond precision, whereas psutil's float can retain a
        # fractional microsecond. Permit only that representation tolerance.
        if ($lockFields['owner_created_at'] -cnotmatch '\A[0-9]+(?:\.[0-9]+)?\z' -or
            -not [double]::TryParse($lockFields['owner_created_at'], [Globalization.NumberStyles]::Float,
                [Globalization.CultureInfo]::InvariantCulture, [ref]$lockBirth) -or
            [double]::IsNaN($lockBirth) -or [double]::IsInfinity($lockBirth) -or $lockBirth -le 0 -or
            [Math]::Abs($workerBirth - $lockBirth) -gt 0.000001) {
            throw 'Existing stock session lock birth does not match its worker.'
        }
    }
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

function Enable-ManualStockSessionLifetime {
    # A PowerShell finally block is not a reliable console-close handler. The
    # kernel owns this boundary: only the explicit manual launcher joins this
    # non-inheritable, kill-on-close job, before it can create a worker. Its
    # children inherit membership, including the virtualenv's second Python.
    # Closing/Ctrl-C'ing this launcher therefore cannot orphan its trader.
    # No activation control, order, lock, or worker receipt is rewritten here.
    if ($null -eq ('Ducketz.ManualStockSessionLifetime' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
namespace Ducketz {
    public static class ManualStockSessionLifetime {
        [StructLayout(LayoutKind.Sequential)]
        private struct BasicLimits {
            public long ProcessTime, JobTime;
            public uint Flags;
            public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
            public uint ActiveProcesses;
            public UIntPtr Affinity;
            public uint Priority, Scheduling;
        }
        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters { public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes; }
        [StructLayout(LayoutKind.Sequential)]
        private struct ExtendedLimits {
            public BasicLimits Basic;
            public IoCounters Io;
            public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool SetInformationJobObject(IntPtr job, int type, ref ExtendedLimits limits, uint size);
        [DllImport("kernel32.dll", SetLastError=true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll")]
        private static extern IntPtr GetCurrentProcess();
        [DllImport("kernel32.dll")]
        private static extern bool CloseHandle(IntPtr handle);
        // Raw, non-inheritable handles deliberately retained until OS process
        // exit. A SafeHandle finalizer can close earlier during CLR shutdown,
        // killing this launcher before its genuine exit code is committed.
        private static IntPtr lifetime = IntPtr.Zero;
        private static IntPtr adoptedLifetime = IntPtr.Zero;
        public static void Enable() {
            if (lifetime != IntPtr.Zero) return;
            IntPtr candidate = CreateJobObject(IntPtr.Zero, null);
            if (candidate == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            ExtendedLimits limits = new ExtendedLimits();
            limits.Basic.Flags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if (!SetInformationJobObject(candidate, 9, ref limits, (uint)Marshal.SizeOf(limits))) {
                int error = Marshal.GetLastWin32Error(); CloseHandle(candidate); throw new Win32Exception(error);
            }
            if (!AssignProcessToJobObject(candidate, GetCurrentProcess())) {
                int error = Marshal.GetLastWin32Error(); CloseHandle(candidate); throw new Win32Exception(error);
            }
            lifetime = candidate;
        }
        public static void Adopt(IntPtr workerHandle, IntPtr launcherHandle) {
            if (lifetime == IntPtr.Zero) throw new InvalidOperationException("Manual lifetime was not enabled.");
            if (adoptedLifetime != IntPtr.Zero) throw new InvalidOperationException("A manual worker is already adopted.");
            IntPtr candidate = CreateJobObject(IntPtr.Zero, null);
            if (candidate == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
            // No kill limit until BOTH exact handles are attached. If either
            // assignment fails, closing this inert job cannot kill an already
            // running worker merely because its adoption was refused.
            if (!AssignProcessToJobObject(candidate, workerHandle) ||
                !AssignProcessToJobObject(candidate, launcherHandle)) {
                int error = Marshal.GetLastWin32Error(); CloseHandle(candidate); throw new Win32Exception(error);
            }
            ExtendedLimits limits = new ExtendedLimits();
            limits.Basic.Flags = 0x2000;
            if (!SetInformationJobObject(candidate, 9, ref limits, (uint)Marshal.SizeOf(limits))) {
                int error = Marshal.GetLastWin32Error(); CloseHandle(candidate); throw new Win32Exception(error);
            }
            adoptedLifetime = candidate;
        }
    }
}
'@
    }
    [Ducketz.ManualStockSessionLifetime]::Enable()
}

function Add-VerifiedWorkerToManualLifetime {
    param([Diagnostics.Process]$Process, [string]$ExpectedCreatedAt,
          [Diagnostics.Process]$LauncherProcess, [string]$LauncherCreatedAt)
    # Open and retain the actual kernel handle before checking birth/liveness.
    # Never reopen by PID when assigning: a recycled PID is not this worker.
    $handle = $Process.Handle
    $launcherHandle = $LauncherProcess.Handle
    foreach ($pair in @(@($Process, $ExpectedCreatedAt), @($LauncherProcess, $LauncherCreatedAt))) {
        if ($pair[0].HasExited -or
            [Math]::Abs(($pair[0].StartTime.ToUniversalTime() - ([DateTimeOffset]$pair[1]).UtcDateTime).Ticks) -gt 10) {
            throw 'Existing worker identity changed before manual lifetime adoption.'
        }
    }
    [Ducketz.ManualStockSessionLifetime]::Adopt($handle, $launcherHandle)
}

function Assert-StockSessionPreflight {
    param([string]$PythonPath, [string]$DatastoreRoot, [string]$Policy)
    # Scheduled starts and adoption only read these gates. A new explicit
    # manual start completes account setup before reaching this verification.
    @'
import sys
from pathlib import Path
from ml.account_gameplan.config import assert_coordinator, load_account_config, verify_cutover
root = Path(sys.argv[1]).resolve()
try:
    config = load_account_config(root)
    assert_coordinator(config, sys.argv[2])
    if config is not None:
        verify_cutover(root, config)
except Exception as exc:
    print(f"TRADER START BLOCKED: {exc}", flush=True)
    if str(exc) == "COMBINED_ACCOUNT_CUTOVER_NOT_ACTIVE":
        print("The account's native ownership setup has not completed.", flush=True)
        print("Start-Gameplan-Trader.cmd will finish account setup automatically when the ownership data is ready.", flush=True)
    else:
        print("Resolve the account configuration or startup receipt reported above before starting.", flush=True)
    sys.exit(2)
print("Account startup checks passed. Worker readiness has not yet been confirmed.", flush=True)
'@ | & $PythonPath -B - $DatastoreRoot $Policy
    if ($LASTEXITCODE -ne 0) {
        throw 'Trader startup blocked before changing manual controls or launching a worker. Resolve the issue above, then run Start-Gameplan-Trader.cmd again.'
    }
}

function Initialize-ManualGameplanAccount {
    param([string]$PythonPath, [string]$DatastoreRoot, [string]$Policy)
    Write-Host 'Checking account setup for this manual start.'
    @'
import re
import sys
from pathlib import Path
try:
    from tools.native_ownership_cutover import ensure_gameplan_account_ready
    result = ensure_gameplan_account_ready(Path(sys.argv[1]).resolve(), sizing_policy=sys.argv[2])
    if not isinstance(result, dict) or result.get("ready") is not True or result.get("status") != "ACCOUNT_READY":
        reason = result.get("reason", "ACCOUNT_SETUP_NOT_READY") if isinstance(result, dict) else "ACCOUNT_SETUP_NOT_READY"
        if not isinstance(reason, str) or re.fullmatch(r"[A-Z][A-Z0-9_]{0,159}", reason) is None:
            reason = "ACCOUNT_SETUP_NOT_READY"
        print(f"TRADER START BLOCKED: {reason}", flush=True)
        print("Account setup is waiting for its required ownership data or reconciliation. Run the same start command after that condition is resolved.", flush=True)
        sys.exit(2)
except Exception as exc:
    # Broker exceptions can contain private response data. The setup helper
    # retains detailed local diagnostics; console output stays classified.
    print(f"TRADER START BLOCKED: ACCOUNT_SETUP_ERROR ({type(exc).__name__})", flush=True)
    sys.exit(2)
print("Account setup ready.", flush=True)
'@ | & $PythonPath -B - $DatastoreRoot $Policy
    if ($LASTEXITCODE -ne 0) {
        throw 'Account setup blocked this start before trading controls were enabled or a worker was launched.'
    }
}

function Write-StockSessionLogOutput {
    param([IO.StreamReader]$Reader)
    # Forward appended bytes without inventing line breaks in a partial write.
    $appended = $Reader.ReadToEnd()
    if ($appended.Length -gt 0) {
        Write-Host -NoNewline $appended
    }
}

function Wait-StockSessionWithOutput {
    param([object]$Process, [string]$StandardOutput, [string]$StandardError)
    $outputReader = $null
    $errorReader = $null
    try {
        $outputReader = [IO.StreamReader]::new([IO.File]::Open($StandardOutput, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite))
        $errorReader = [IO.StreamReader]::new([IO.File]::Open($StandardError, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite))
        do {
            $finished = $Process.WaitForExit(500)
            Write-StockSessionLogOutput -Reader $outputReader
            Write-StockSessionLogOutput -Reader $errorReader
        } while (-not $finished)
        # Wait for redirected streams to flush, then forward their final bytes.
        $Process.WaitForExit()
        Write-StockSessionLogOutput -Reader $outputReader
        Write-StockSessionLogOutput -Reader $errorReader
    } finally {
        if ($null -ne $outputReader) { $outputReader.Dispose() }
        if ($null -ne $errorReader) { $errorReader.Dispose() }
    }
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
print("Manual trading intent saved. Waiting for the worker to confirm startup.")
'@ | & $PythonPath - $DatastoreRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not enable the manual Gameplan trader.' }
}

if ($ActivateForManualStart -and (-not $WaitForOpen -or $SizingPolicy -cne 'gameplan-direction-current-market-v1')) {
    throw 'Manual activation requires the Gameplan policy and -WaitForOpen.'
}
if ($ActivateForManualStart) { Enable-ManualStockSessionLifetime }

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

# The native runtime retains all exchange-calendar, activation, forecast,
# entry-slot and broker safeguards. Only explicit manual activation binds
# the worker lifetime to this user's launcher console.
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
    # Existing ownership setup is only verified; never migrate beneath a worker.
    Assert-StockSessionPreflight -PythonPath $pythonPath -DatastoreRoot $datastoreRoot -Policy $identity.sizing_policy
    if ($ActivateForManualStart) {
        Add-VerifiedWorkerToManualLifetime -Process $workerProcess -ExpectedCreatedAt $identity.worker_created_at `
            -LauncherProcess $launcherProcess -LauncherCreatedAt $identity.launcher_created_at
        Write-Host 'Manual supervision owns this verified worker. Ctrl-C or closing this launcher stops it.'
    }
    if ($ActivateForManualStart) { Enable-ManualGameplanTrading -PythonPath $pythonPath -DatastoreRoot $datastoreRoot }
    Write-Host "Supervising existing verified trader worker $workerPid. No second worker was launched."
    Write-Host "Worker status: $sessionStatus"
    [pscustomobject]@{ status='SUPERVISING_EXISTING_WORKER'; worker_pid=$workerPid; launcher_pid=$identity.launcher_pid; worker_created_at=$identity.worker_created_at; launcher_created_at=$identity.launcher_created_at; lock_started_at=$identity.lock_started_at; observed_at=[DateTime]::UtcNow.ToString('o') } |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logDirectory 'launcher.json') -Encoding utf8
    $workerProcess | Wait-Process
    if (Test-Path -LiteralPath $sessionStatus) {
        $terminal = Get-Content -LiteralPath $sessionStatus -Raw | ConvertFrom-Json
        if ($terminal.pid -eq $workerPid -and $terminal.status -in @('FINISHED', 'STOPPED_TRADER_INACTIVE', 'STOPPED_INTERRUPTED')) {
            Write-Host "Existing trader worker ended: $($terminal.status)."
            exit 0
        }
    }
    # A disappeared owner without a verified normal termination is a failure.
    # This launcher does not independently restart the worker.
    Write-Host "Existing trader worker stopped without a verified normal termination. Inspect worker status: $sessionStatus"
    exit 1
}

$stdout = Join-Path $logDirectory 'stock-session.stdout.log'
$stderr = Join-Path $logDirectory 'stock-session.stderr.log'
$arguments = @(Get-StockSessionArguments -Policy $SizingPolicy -Wait $WaitForOpen.IsPresent)
if ($ActivateForManualStart) {
    Initialize-ManualGameplanAccount -PythonPath $pythonPath -DatastoreRoot $datastoreRoot -Policy $SizingPolicy
}
Assert-StockSessionPreflight -PythonPath $pythonPath -DatastoreRoot $datastoreRoot -Policy $SizingPolicy
if ($ActivateForManualStart) { Enable-ManualGameplanTrading -PythonPath $pythonPath -DatastoreRoot $datastoreRoot }
$process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $repoRoot `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
# Windows PowerShell can lose a redirected child's exit code after it exits
# unless its process handle was retained first. A null code becomes exit 0.
$null = $process.Handle
[pscustomobject]@{ status='LAUNCHED_AWAITING_WORKER_STATUS'; launcher_pid=$process.Id; started_at=[DateTime]::UtcNow.ToString('o'); stdout=$stdout; stderr=$stderr } |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logDirectory 'launcher.json') -Encoding utf8
Write-Host 'Trader process launched; waiting for its readiness report below.'
if ($ActivateForManualStart) { Write-Host 'Ctrl-C or closing this manual launcher stops its trader process.' }
Write-Host 'SLEEPING_UNTIL_OPEN means the worker is waiting; SESSION_STARTED means its session started.'
Write-Host "Output log: $stdout"
Write-Host "Error log: $stderr"
Wait-StockSessionWithOutput -Process $process -StandardOutput $stdout -StandardError $stderr
Write-Host ''
$process.Refresh()
if ($null -eq $process.ExitCode) {
    Write-Host "Trader process ended without a verified exit code. Check $stdout and $stderr."
    exit 1
}
if ($process.ExitCode -ne 0) {
    Write-Host "Trader process stopped with exit code $($process.ExitCode). Check the failure above and logs: $stdout ; $stderr"
} else {
    Write-Host 'Trader process ended with exit code 0. Its final status above explains whether the session completed or stopped.'
}
exit $process.ExitCode
