param(
    [Parameter(Mandatory=$true)][string]$Pythonw,
    [Parameter(Mandatory=$true)][string]$Script,
    [Parameter(Mandatory=$true)][string]$Config,
    [string]$TaskName = 'Ducketz Completed Scheduled Chat Cleanup',
    [switch]$ReadOnly
)
$ErrorActionPreference = 'Stop'
$description = 'Ducketz completed scheduled chat cleanup v1. Archive allowlisted completed five-minute Codex chats after one hour; never delete or interrupt an active writer.'
foreach ($item in @($Pythonw, $Script, $Config)) {
    if ($item -notmatch '^[A-Za-z]:[\\/]' -or -not (Test-Path -LiteralPath $item -PathType Leaf) -or $item.Contains('"')) {
        throw "Expected an existing absolute local file without quotes: $item"
    }
    if ($item.StartsWith('\\')) { throw 'Cleanup bindings must use local files.' }
}
if ([IO.Path]::GetFileName($Pythonw) -ne 'pythonw.exe') {
    throw 'Use pythonw.exe to keep this interactive-user maintenance task hidden.'
}
$binding = Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json
if (-not $binding.helper_sha256 -or (Get-FileHash -LiteralPath $Script -Algorithm SHA256).Hash.ToLowerInvariant() -ne $binding.helper_sha256) {
    throw 'The installed helper must match its reviewed private binding hash.'
}
if (-not $binding.codex_sha256 -or (Get-FileHash -LiteralPath $binding.codex_exe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $binding.codex_sha256) {
    throw 'The Codex executable must match the locally tested writer-lock guard.'
}
if ($binding.minimum_age_seconds -lt 3600) { throw 'The completion grace period must be at least one hour.' }
if (-not $ReadOnly) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing -and $existing.Description -ne $description) {
        throw 'The requested task name belongs to another task; preserve it.'
    }
    $arguments = '-I -B "' + $Script + '" --config "' + $Config + '" --apply'
    $action = New-ScheduledTaskAction -Execute $Pythonw -Argument $arguments -WorkingDirectory (Split-Path -Parent $Script)
    $timer = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(5)) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($timer, $logon) -Principal $principal -Settings $settings -Description $description -Force | Out-Null
}
$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
[pscustomobject]@{
    TaskName=$task.TaskName; State=[string]$task.State; Description=$task.Description
    NextRunTime=$info.NextRunTime.ToString('o'); LastRunTime=$info.LastRunTime.ToString('o'); LastTaskResult=$info.LastTaskResult
    Actions=@($task.Actions | Select-Object Execute,Arguments,WorkingDirectory)
    Triggers=@($task.Triggers | Select-Object StartBoundary,Enabled,Repetition,CimClass)
    StartWhenAvailable=$task.Settings.StartWhenAvailable; MultipleInstances=[string]$task.Settings.MultipleInstances
    ExecutionTimeLimit=$task.Settings.ExecutionTimeLimit; LogonType=[string]$task.Principal.LogonType
    HelperSHA256=$binding.helper_sha256; CodexSHA256=$binding.codex_sha256
} | ConvertTo-Json -Depth 8
