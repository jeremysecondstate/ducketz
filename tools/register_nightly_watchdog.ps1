param(
    [Parameter(Mandatory=$true)][string]$Repository,
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Config,
    [Parameter(Mandatory=$true)][string]$TaskName,
    [switch]$ReadOnly
)
$ErrorActionPreference = 'Stop'
foreach ($item in @($Repository, $Python, $Config)) {
    if (-not [IO.Path]::IsPathFullyQualified($item) -or -not (Test-Path -LiteralPath $item)) {
        throw "Expected an existing absolute local path: $item"
    }
}
if ((Get-TimeZone).Id -ne 'Pacific Standard Time') {
    throw 'This installation expects the Windows Pacific timezone; do not silently use another timezone.'
}
if (-not $ReadOnly) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing -and -not ($existing.Description -like 'Ducketz deterministic responsibility dispatch.*')) {
        throw 'The requested task name belongs to another task; preserve it and choose the owned watchdog name.'
    }
    $arguments = '-B -m tools.nightly_watchdog --config "' + $Config + '"'
    $action = New-ScheduledTaskAction -Execute $Python -Argument $arguments -WorkingDirectory $Repository
    $kickoff = New-ScheduledTaskTrigger -Daily -At '21:05'
    # A floating boundary follows the Windows Pacific timezone through DST.
    $kickoff.StartBoundary = (Get-Date).ToString('yyyy-MM-dd') + 'T21:05:00'
    # The timer is cheap and date-aware. AtLogOn resumes missed work after a reboot.
    $timer = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(5)) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $logon = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun
    $description = 'Ducketz deterministic responsibility dispatch. One action-date workflow; no trader control. Native Codex responsibility tasks own review, repairs, private handoff and notifications.'
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($kickoff, $timer, $logon) -Principal $principal -Settings $settings -Description $description -Force | Out-Null
}
$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
[pscustomobject]@{
    TaskName=$task.TaskName; State=[string]$task.State; Timezone=(Get-TimeZone).Id
    NextRunTime=$info.NextRunTime.ToString('o'); LastRunTime=$info.LastRunTime.ToString('o')
    LastTaskResult=$info.LastTaskResult; Actions=@($task.Actions | Select-Object Execute,Arguments,WorkingDirectory)
    Triggers=@($task.Triggers | Select-Object StartBoundary,Enabled,Repetition,CimClass)
    StartWhenAvailable=$task.Settings.StartWhenAvailable; MultipleInstances=[string]$task.Settings.MultipleInstances
    LogonType=[string]$task.Principal.LogonType
} | ConvertTo-Json -Depth 8
