param(
    [switch]$AuditOnly,
    [switch]$RequireAllMissing
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# This launcher owns the eight recurring production runtimes. It consumes
# the guardian's closed allowlist, refuses ambiguous/partial ownership, starts
# missing owners sequentially, and verifies the worker-owned singleton lock.
# Empty-store imports and provider-history maintenance remain separate commands.

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$python = (Resolve-Path -LiteralPath (Join-Path $repoRoot '.venv\Scripts\python.exe')).Path
$metadataScript = @'
import json
from datafetching.parquet_store import resolve_datastore_dir
from ml.system_guardian import GUARDIAN_LAUNCHES
from ml.system_monitor import RUNTIMES

runtimes = {spec.name: spec for spec in RUNTIMES}
payload = {
    "datastore": str(resolve_datastore_dir(target="pc").resolve()),
    "owners": [
        {
            "runtime": launch.runtime,
            "module": launch.module,
            "log_stem": launch.log_stem,
            "lock_name": runtimes[launch.runtime].lock_name,
            "required_arguments": list(runtimes[launch.runtime].required_arguments),
            "arguments": list(launch.arguments),
        }
        for launch in GUARDIAN_LAUNCHES
    ],
}
print(json.dumps(payload, separators=(",", ":")))
'@
$metadataText = $metadataScript | & $python -
if ($LASTEXITCODE -ne 0 -or -not $metadataText) {
    throw 'Could not load the checked-in guardian launch allowlist.'
}
$metadata = $metadataText | ConvertFrom-Json
$datastoreRoot = (Resolve-Path -LiteralPath ([string]$metadata.datastore)).Path
$primaryLogRoot = Join-Path $datastoreRoot 'logs\ducketz\background-launch'

Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class DucketzNativeCommandLine {
    [DllImport("shell32.dll", SetLastError = true)]
    private static extern IntPtr CommandLineToArgvW(
        [MarshalAs(UnmanagedType.LPWStr)] string commandLine,
        out int argumentCount
    );

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);

    public static string[] Split(string commandLine) {
        int count;
        IntPtr values = CommandLineToArgvW(commandLine, out count);
        if (values == IntPtr.Zero) {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        try {
            var result = new List<string>(count);
            for (int index = 0; index < count; index++) {
                IntPtr value = Marshal.ReadIntPtr(values, index * IntPtr.Size);
                result.Add(Marshal.PtrToStringUni(value));
            }
            return result.ToArray();
        } finally {
            LocalFree(values);
        }
    }
}
'@

function ConvertTo-NormalizedCommand {
    param([AllowNull()][string]$CommandLine)
    if (-not $CommandLine) { return '' }
    return ([regex]::Replace($CommandLine, '\s+', ' ')).Trim().ToLowerInvariant()
}

function Test-ExactOwnerCommand {
    param(
        [AllowNull()][string]$CommandLine,
        [Parameter(Mandatory)]$Owner
    )
    if (-not $CommandLine) { return $false }
    $actual = @([DucketzNativeCommandLine]::Split($CommandLine))
    $expected = @($Owner.arguments | ForEach-Object { [string]$_ })
    if ($actual.Count -ne $expected.Count + 1) { return $false }
    $executable = [System.IO.Path]::GetFullPath([string]$actual[0])
    if (-not [string]::Equals(
        $executable,
        $python,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        return $false
    }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if (-not [string]::Equals(
            [string]$actual[$index + 1],
            [string]$expected[$index],
            [System.StringComparison]::Ordinal
        )) {
            return $false
        }
    }
    return $true
}

function Get-OwnerProcesses {
    param([Parameter(Mandatory)]$Owner)
    $modulePattern = '(?i)(?:^|\s)-m\s+' + [regex]::Escape([string]$Owner.module) + '(?:\s|$)'
    return @(
        Get-CimInstance Win32_Process |
            Where-Object { ([string]$_.CommandLine) -match $modulePattern } |
            Select-Object ProcessId, ParentProcessId, CreationDate, CommandLine
    )
}

function Get-LockOwnerState {
    param([AllowNull()]$OwnerPid)
    if ($null -eq $OwnerPid) { return 'UNVERIFIABLE' }
    $numericPid = [int]$OwnerPid
    if ($numericPid -le 0) { return 'UNVERIFIABLE' }
    try {
        $matches = @(
            Get-CimInstance Win32_Process `
                -Filter "ProcessId = $numericPid" `
                -ErrorAction Stop
        )
    } catch {
        return 'UNVERIFIABLE'
    }
    if ($matches.Count -eq 0) { return 'DEAD' }
    return 'LIVE'
}

function Get-OwnerState {
    param([Parameter(Mandatory)]$Owner)
    $processes = @(Get-OwnerProcesses -Owner $Owner)
    $ids = @($processes | ForEach-Object { [int]$_.ProcessId })
    $workers = @(
        $processes | Where-Object {
            $ids -contains [int]$_.ParentProcessId -and
            [int]$_.ParentProcessId -ne [int]$_.ProcessId
        }
    )
    $missingArguments = @()
    foreach ($required in @($Owner.required_arguments)) {
        $fragment = ConvertTo-NormalizedCommand -CommandLine ([string]$required)
        foreach ($process in $processes) {
            if ((ConvertTo-NormalizedCommand -CommandLine ([string]$process.CommandLine)) -notlike "*$fragment*") {
                $missingArguments += [string]$required
                break
            }
        }
    }
    $canonical = $processes.Count -eq 2 -and @(
        $processes | Where-Object {
            Test-ExactOwnerCommand -CommandLine ([string]$_.CommandLine) -Owner $Owner
        }
    ).Count -eq 2
    $lockPath = Join-Path $datastoreRoot ([string]$Owner.lock_name)
    $lockPid = $null
    $lockOwnerState = 'MISSING'
    if (Test-Path -LiteralPath $lockPath -PathType Leaf) {
        $lockMatch = [regex]::Match(
            (Get-Content -LiteralPath $lockPath -Raw -ErrorAction Stop),
            '(?m)^pid=(\d+)\s*$'
        )
        if ($lockMatch.Success) {
            try {
                $lockPid = [int]$lockMatch.Groups[1].Value
            } catch {
                $lockPid = $null
            }
        }
        $lockOwnerState = Get-LockOwnerState -OwnerPid $lockPid
    }
    $validPair = (
        $processes.Count -eq 2 -and
        $workers.Count -eq 1 -and
        $missingArguments.Count -eq 0 -and
        $null -ne $lockPid -and
        $lockPid -eq [int]$workers[0].ProcessId
    )
    return [pscustomobject]@{
        Runtime = [string]$Owner.runtime
        ProcessCount = $processes.Count
        ProcessIds = @($ids | Sort-Object)
        ValidPairAndLock = $validPair
        CanonicalCommand = $canonical
        LauncherPid = if ($workers.Count -eq 1) { [int]$workers[0].ParentProcessId } else { $null }
        WorkerPid = if ($workers.Count -eq 1) { [int]$workers[0].ProcessId } else { $null }
        LockPath = $lockPath
        LockPid = $lockPid
        LockOwnerState = $lockOwnerState
        MissingRequiredArguments = @($missingArguments | Sort-Object -Unique)
    }
}

$launcherMutex = [System.Threading.Mutex]::new(
    $false,
    'Global\DucketzAllLoopsCanonicalLauncherV1'
)
$launcherMutexAcquired = $false
try {
    $launcherMutexAcquired = $launcherMutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $launcherMutexAcquired = $true
}
if (-not $launcherMutexAcquired) {
    [pscustomobject]@{
        schema_version = 'ducketz-background-launch-v1'
        audit_only = [bool]$AuditOnly
        require_all_missing = [bool]$RequireAllMissing
        repository = $repoRoot
        datastore = $datastoreRoot
        canonical_log_root = $primaryLogRoot
        issues = @('launcher-mutex-busy')
        owners = @()
    } | ConvertTo-Json -Depth 8
    $launcherMutex.Dispose()
    exit 2
}

$preflightResults = [System.Collections.Generic.List[object]]::new()
$preflightIssues = [System.Collections.Generic.List[string]]::new()
if ($RequireAllMissing) {
    foreach ($owner in @($metadata.owners)) {
        $state = Get-OwnerState -Owner $owner
        $lockExists = Test-Path -LiteralPath $state.LockPath
        if ($state.ProcessCount -ne 0 -or $lockExists) {
            $preflightIssues.Add("$($owner.runtime):not-completely-missing")
        }
        $preflightResults.Add([pscustomobject]@{
            runtime = [string]$owner.runtime
            status = if ($state.ProcessCount -eq 0 -and -not $lockExists) { 'MISSING_VERIFIED' } else { 'BLOCKED_NOT_MISSING' }
            process_ids = $state.ProcessIds
            lock_path = $state.LockPath
            lock_exists = [bool]$lockExists
            lock_pid = $state.LockPid
            lock_owner_state = $state.LockOwnerState
        })
    }
    if ($preflightIssues.Count -gt 0) {
        [pscustomobject]@{
            schema_version = 'ducketz-background-launch-v1'
            audit_only = [bool]$AuditOnly
            require_all_missing = $true
            repository = $repoRoot
            datastore = $datastoreRoot
            canonical_log_root = $primaryLogRoot
            issues = @($preflightIssues)
            owners = @($preflightResults)
        } | ConvertTo-Json -Depth 8
        $launcherMutex.ReleaseMutex()
        $launcherMutex.Dispose()
        exit 2
    }
}

$results = [System.Collections.Generic.List[object]]::new()
$issues = [System.Collections.Generic.List[string]]::new()
$launchDirectory = $null

foreach ($owner in @($metadata.owners)) {
    $before = Get-OwnerState -Owner $owner
    if ($before.ValidPairAndLock) {
        $status = if ($before.CanonicalCommand) { 'ALREADY_RUNNING' } else { 'RUNNING_COMMAND_DRIFT' }
        if (-not $before.CanonicalCommand) {
            $issues.Add("$($owner.runtime):command-drift")
        }
        $results.Add([pscustomobject]@{
            runtime = [string]$owner.runtime
            status = $status
            launcher_pid = $before.LauncherPid
            worker_pid = $before.WorkerPid
            lock_path = $before.LockPath
            log_directory = $null
        })
        if (-not $before.CanonicalCommand) { break }
        continue
    }

    if ($before.ProcessCount -ne 0) {
        $issues.Add("$($owner.runtime):ambiguous-or-partial-owner")
        $results.Add([pscustomobject]@{
            runtime = [string]$owner.runtime
            status = 'BLOCKED_EXISTING_OWNER_STATE'
            process_ids = $before.ProcessIds
            lock_path = $before.LockPath
            lock_pid = $before.LockPid
            missing_required_arguments = $before.MissingRequiredArguments
        })
        break
    }
    if (
        (Test-Path -LiteralPath $before.LockPath) -and
        $before.LockOwnerState -ne 'DEAD'
    ) {
        $lockIssue = if ($before.LockOwnerState -eq 'LIVE') {
            'lock-owned-by-live-pid'
        } else {
            'lock-needs-guardian-review'
        }
        $issues.Add("$($owner.runtime):$lockIssue")
        $results.Add([pscustomobject]@{
            runtime = [string]$owner.runtime
            status = 'BLOCKED_EXISTING_LOCK'
            process_ids = @()
            lock_path = $before.LockPath
            lock_pid = $before.LockPid
            lock_owner_state = $before.LockOwnerState
        })
        break
    }
    if ($AuditOnly) {
        $auditStatus = if ($before.LockOwnerState -eq 'DEAD') {
            'MISSING_WITH_DEAD_LOCK_AUDIT_ONLY'
        } else {
            'MISSING_AUDIT_ONLY'
        }
        $issues.Add("$($owner.runtime):missing")
        $results.Add([pscustomobject]@{
            runtime = [string]$owner.runtime
            status = $auditStatus
            process_ids = @()
            lock_path = $before.LockPath
            lock_pid = $before.LockPid
            lock_owner_state = $before.LockOwnerState
        })
        continue
    }

    if ($null -eq $launchDirectory) {
        $launchDirectory = Join-Path $primaryLogRoot ([DateTime]::UtcNow.ToString('yyyyMMddTHHmmss.fffffffZ'))
        New-Item -ItemType Directory -Path $launchDirectory -Force | Out-Null
        $launchDirectory = (Resolve-Path -LiteralPath $launchDirectory).Path
    }
    $stem = [string]$owner.log_stem
    $stdout = Join-Path $launchDirectory "$stem.stdout.log"
    $stderr = Join-Path $launchDirectory "$stem.stderr.log"
    $arguments = @($owner.arguments | ForEach-Object { [string]$_ })
    $started = Start-Process -FilePath $python `
        -ArgumentList $arguments `
        -WorkingDirectory $repoRoot `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden `
        -PassThru

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $after = Get-OwnerState -Owner $owner
    } while (-not $after.ValidPairAndLock -and [DateTime]::UtcNow -lt $deadline)
    if (
        -not $after.ValidPairAndLock -or
        -not $after.CanonicalCommand -or
        [int]$after.LauncherPid -ne [int]$started.Id
    ) {
        $issues.Add("$($owner.runtime):launch-verification-failed")
        $results.Add([pscustomobject]@{
            runtime = [string]$owner.runtime
            status = 'STARTED_NOT_VERIFIED'
            start_process_pid = [int]$started.Id
            process_ids = $after.ProcessIds
            lock_path = $after.LockPath
            lock_pid = $after.LockPid
            stdout = $stdout
            stderr = $stderr
        })
        break
    }
    $results.Add([pscustomobject]@{
        runtime = [string]$owner.runtime
        status = 'STARTED_VERIFIED'
        launcher_pid = $after.LauncherPid
        worker_pid = $after.WorkerPid
        lock_path = $after.LockPath
        stdout = $stdout
        stderr = $stderr
        dead_lock_observed_before_start = ($before.LockOwnerState -eq 'DEAD')
        prior_lock_pid = $before.LockPid
    })
}

$payload = [pscustomobject]@{
    schema_version = 'ducketz-background-launch-v1'
    audit_only = [bool]$AuditOnly
    require_all_missing = [bool]$RequireAllMissing
    repository = $repoRoot
    datastore = $datastoreRoot
    canonical_log_root = $primaryLogRoot
    issues = @($issues)
    owners = @($results)
}
$launcherMutex.ReleaseMutex()
$launcherMutex.Dispose()
$payload | ConvertTo-Json -Depth 8

if ($issues.Count -gt 0) { exit 2 }
