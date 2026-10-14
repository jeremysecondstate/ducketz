[CmdletBinding()]
param(
    [string]$Repository,
    [string]$Python,
    [string]$Profile,
    [string]$Watchlist,
    [string]$Datastore,
    [string]$Config,
    [string]$LogDirectory,
    [string]$TaskName,
    [string]$TaskPath = '\',
    [string]$XmlPath,
    [switch]$Plan,
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
$descriptionPrefix = 'Ducketz independent 15-minute model supervision v1.'
$taskNamespace = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
$times = @('01:00:00', '01:30:00', '02:00:00', '02:30:00', '03:00:00', '03:30:00')
$days = @('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday')

function Assert-SupervisorXml([string]$Source) {
    $document = New-Object System.Xml.XmlDocument
    # Task XML is local evidence; never permit external entities or DTD access.
    $document.XmlResolver = $null
    $document.LoadXml($Source)
    $namespace = New-Object System.Xml.XmlNamespaceManager($document.NameTable)
    $namespace.AddNamespace('t', $taskNamespace)
    $triggers = @($document.SelectNodes('/t:Task/t:Triggers/*', $namespace))
    if ($triggers.Count -ne 6) { throw 'Expected exactly six weekly supervisor triggers.' }
    for ($index = 0; $index -lt $times.Count; $index++) {
        $trigger = $triggers[$index]
        if ($trigger.LocalName -ne 'CalendarTrigger') { throw 'Only weekly calendar triggers are permitted.' }
        $boundary = $trigger.SelectSingleNode('t:StartBoundary', $namespace).InnerText
        if ($boundary -notmatch ('^\d{4}-\d{2}-\d{2}T' + [regex]::Escape($times[$index]) + '$')) {
            throw 'Supervisor times must use floating Pacific boundaries, without UTC offsets.'
        }
        $actualDays = @($trigger.SelectNodes('t:ScheduleByWeek/t:DaysOfWeek/*', $namespace) | ForEach-Object { $_.LocalName })
        if (($actualDays -join ',') -ne ($days -join ',')) { throw 'Supervisor cadence must be Monday through Friday.' }
        if ($trigger.SelectSingleNode('t:ScheduleByWeek/t:WeeksInterval', $namespace).InnerText -ne '1') {
            throw 'Supervisor cadence must repeat every week.'
        }
        if ($trigger.SelectSingleNode('t:Enabled', $namespace).InnerText -ne 'true' -or
            $trigger.SelectSingleNode('t:EndBoundary', $namespace) -or
            $trigger.SelectSingleNode('t:Repetition', $namespace) -or
            $trigger.SelectSingleNode('t:ExecutionTimeLimit', $namespace)) {
            throw 'Supervisor triggers must remain enabled and have no repetition, end boundary or execution deadline.'
        }
    }
    $expectedSettings = @{
        MultipleInstancesPolicy = 'IgnoreNew'; ExecutionTimeLimit = 'PT0S';
        StartWhenAvailable = 'true'; DisallowStartIfOnBatteries = 'false';
        StopIfGoingOnBatteries = 'false'; RunOnlyIfNetworkAvailable = 'false'; AllowHardTerminate = 'false';
        RunOnlyIfIdle = 'false'; Enabled = 'true'; WakeToRun = 'true'
    }
    foreach ($setting in $expectedSettings.GetEnumerator()) {
        $node = $document.SelectSingleNode('/t:Task/t:Settings/t:' + $setting.Key, $namespace)
        if (-not $node -or $node.InnerText -ne $setting.Value) { throw ('Invalid supervisor setting: ' + $setting.Key) }
    }
    if ($document.SelectSingleNode('/t:Task/t:Settings/t:IdleSettings/t:StopOnIdleEnd', $namespace).InnerText -ne 'false') {
        throw 'Idleness must not stop a supervisor run.'
    }
    $actions = @($document.SelectNodes('/t:Task/t:Actions/*', $namespace))
    if ($actions.Count -ne 1 -or $actions[0].LocalName -ne 'Exec') { throw 'Expected one direct Python supervisor action.' }
    $arguments = $actions[0].SelectSingleNode('t:Arguments', $namespace).InnerText
    if ($arguments -notmatch '^\-B \-m ml\.intraday\.supervisor --profile "[^"]+" --watchlist "[^"]+" --datastore "[^"]+" --config "[^"]+" --log-dir "[^"]+"$') {
        throw 'Task action must only invoke model preparation, with explicit local bindings.'
    }
    $command = $actions[0].SelectSingleNode('t:Command', $namespace).InnerText
    if ([IO.Path]::GetFileName($command) -notmatch '^pythonw?\.exe$') { throw 'Task action must execute a Python interpreter.' }
    if ($document.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:LogonType', $namespace).InnerText -ne 'InteractiveToken' -or
        $document.SelectSingleNode('/t:Task/t:Principals/t:Principal/t:RunLevel', $namespace).InnerText -ne 'LeastPrivilege') {
        throw 'Use the existing interactive local account at limited privilege.'
    }
    return $document
}

if ($ValidateOnly) {
    if ($Plan -or -not $XmlPath -or -not (Test-Path -LiteralPath $XmlPath -PathType Leaf)) {
        throw 'ValidateOnly requires one existing -XmlPath and cannot be combined with -Plan.'
    }
    $null = Assert-SupervisorXml ([IO.File]::ReadAllText($XmlPath))
    [pscustomobject]@{ Status = 'VALID'; XmlPath = [IO.Path]::GetFullPath($XmlPath); Timezone = 'America/Los_Angeles'; Times = $times; Days = $days; StartsTrader = $false } | ConvertTo-Json -Depth 4
    return
}

foreach ($binding in @{
    Repository = $Repository; Python = $Python; Profile = $Profile; Watchlist = $Watchlist;
    Datastore = $Datastore; Config = $Config; LogDirectory = $LogDirectory
}.GetEnumerator()) {
    if (-not $binding.Value -or -not [IO.Path]::IsPathRooted($binding.Value) -or
        $binding.Value -match '["\r\n]' -or $binding.Value -notmatch '^(?:[A-Za-z]:[\\/]|\\\\)') {
        throw ('Expected an absolute local path for ' + $binding.Key)
    }
    if ($binding.Key -ne 'LogDirectory' -and -not (Test-Path -LiteralPath $binding.Value)) {
        throw ('Local binding does not exist: ' + $binding.Key + ' (' + $binding.Value + ')')
    }
}
if (-not (Test-Path -LiteralPath $Repository -PathType Container) -or
    -not (Test-Path -LiteralPath $Datastore -PathType Container)) { throw 'Repository and Datastore must be directories.' }
foreach ($file in @($Python, $Profile, $Watchlist, $Config)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw ('Expected an existing local file: ' + $file) }
}
if (-not $TaskName -or $TaskName -match '[\\/\r\n]') { throw "Supply this PC's owned native task name." }
if ($TaskPath -notmatch '^\\.*\\$' -and $TaskPath -ne '\') { throw 'TaskPath must be a rooted Task Scheduler folder.' }
if ((Get-TimeZone).Id -ne 'Pacific Standard Time') {
    throw 'The Windows host must use Pacific Standard Time (America/Los_Angeles); no timezone change is performed.'
}
if ($Plan -and -not $XmlPath) { throw '-Plan requires an absolute -XmlPath for review.' }
if ($XmlPath -and -not [IO.Path]::IsPathRooted($XmlPath)) { throw 'XmlPath must be absolute.' }

# Inspect native identity before mutation. Reuse the existing owned name instead of
# silently registering a second task with the same purpose.
$inventory = @(Get-ScheduledTask)
$existing = @($inventory | Where-Object { $_.TaskName -eq $TaskName -and $_.TaskPath -eq $TaskPath })
if ($existing.Count -gt 1) { throw 'Native task identity is ambiguous.' }
if ($existing.Count -eq 1 -and $existing[0].Description -notlike ($descriptionPrefix + '*')) {
    throw 'The requested native task identity belongs to another purpose; preserve it.'
}
$equivalent = @($inventory | Where-Object {
    ($_.Description -like ($descriptionPrefix + '*') -or @($_.Actions | Where-Object { $_.Arguments -match '(?:^|\s)-m\s+ml\.intraday\.supervisor(?:\s|$)' }).Count -gt 0) -and
    -not ($_.TaskName -eq $TaskName -and $_.TaskPath -eq $TaskPath)
})
if ($equivalent.Count -gt 0) { throw ('An equivalent supervisor already exists; reuse its owned identity: ' + (($equivalent | ForEach-Object { $_.TaskPath + $_.TaskName }) -join ', ')) }

function Escape-Xml([string]$Value) { return [Security.SecurityElement]::Escape($Value) }
function Quote-Argument([string]$Value) {
    # Windows argv escaping doubles a trailing slash before the closing quote.
    return '"' + [regex]::Replace($Value, '(\\+)$', '$1$1') + '"'
}
$arguments = '-B -m ml.intraday.supervisor --profile ' + (Quote-Argument $Profile) +
    ' --watchlist ' + (Quote-Argument $Watchlist) + ' --datastore ' + (Quote-Argument $Datastore) +
    ' --config ' + (Quote-Argument $Config) + ' --log-dir ' + (Quote-Argument $LogDirectory)
$today = (Get-Date).ToString('yyyy-MM-dd')
$triggerXml = ($times | ForEach-Object {
    '<CalendarTrigger><StartBoundary>' + $today + 'T' + $_ + '</StartBoundary><Enabled>true</Enabled>' +
    '<ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek><Monday/><Tuesday/><Wednesday/><Thursday/><Friday/></DaysOfWeek></ScheduleByWeek></CalendarTrigger>'
}) -join "`n"
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$description = $descriptionPrefix + ' Mon-Fri 01:00-03:30 America/Los_Angeles every 30 minutes; local data refresh and walk-forward model selection. Preparation only; each trader remains manually started. Running work continues without an execution deadline.'
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="$taskNamespace">
  <RegistrationInfo><Description>$(Escape-Xml $description)</Description></RegistrationInfo>
  <Triggers>$triggerXml</Triggers>
  <Principals><Principal id="Author"><UserId>$(Escape-Xml $sid)</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>false</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable><RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden><RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>true</WakeToRun><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>7</Priority>
  </Settings>
  <Actions Context="Author"><Exec><Command>$(Escape-Xml $Python)</Command><Arguments>$(Escape-Xml $arguments)</Arguments><WorkingDirectory>$(Escape-Xml $Repository)</WorkingDirectory></Exec></Actions>
</Task>
"@
$null = Assert-SupervisorXml $xml
if ($XmlPath) {
    $parentDirectory = Split-Path -Parent $XmlPath
    $null = New-Item -ItemType Directory -Path $parentDirectory -Force
    [IO.File]::WriteAllText($XmlPath, $xml, [Text.Encoding]::Unicode)
}
if ($Plan) {
    [pscustomobject]@{ Status = 'PLANNED'; TaskName = $TaskName; TaskPath = $TaskPath; XmlPath = $XmlPath; Timezone = 'America/Los_Angeles'; Times = $times; Days = $days; Arguments = $arguments; ExistingOwnedTask = ($existing.Count -eq 1); StartsTrader = $false } | ConvertTo-Json -Depth 4
    return
}

$null = New-Item -ItemType Directory -Path $LogDirectory -Force
$rollbackPath = $null
if ($existing.Count -eq 1) {
    $rollbackDirectory = Join-Path $LogDirectory 'task-rollback'
    $null = New-Item -ItemType Directory -Path $rollbackDirectory -Force
    $rollbackPath = Join-Path $rollbackDirectory (([DateTimeOffset]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ')) + '-' + [guid]::NewGuid().ToString('N') + '.xml')
    [IO.File]::WriteAllText($rollbackPath, (Export-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath), [Text.Encoding]::Unicode)
}
# Registering does not start or stop a task, and never changes another task.
$null = Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath -Xml $xml -Force
$savedXml = Export-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
$null = Assert-SupervisorXml $savedXml
$task = Get-ScheduledTask -TaskName $TaskName -TaskPath $TaskPath
$information = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath $TaskPath
[pscustomobject]@{
    Status = 'REGISTERED'; TaskName = $task.TaskName; TaskPath = $task.TaskPath; State = [string]$task.State;
    Timezone = 'America/Los_Angeles'; Times = $times; Days = $days; XmlPath = $XmlPath; RollbackXml = $rollbackPath;
    NextRunTime = $information.NextRunTime.ToString('o'); LastRunTime = $information.LastRunTime.ToString('o'); LastTaskResult = $information.LastTaskResult;
    Arguments = $arguments; StartsTrader = $false; StartedForVerification = $false
} | ConvertTo-Json -Depth 4
