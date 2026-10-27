"""Exercise registration with mocked Windows cmdlets; never register a real task."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "tools/register_nightly_watchdog.ps1"


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("already_installed", [True, False])
def test_existing_registration_preserves_everything_except_stats_trigger(tmp_path, enabled, already_installed):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        pytest.skip("PowerShell required for mocked registration integration")
    harness = tmp_path / "registration.ps1"
    # All Windows mutation cmdlets are shadowed before invoking the real helper.
    harness.write_text(r'''
$ErrorActionPreference='Stop'
function Get-TimeZone { [pscustomobject]@{Id='Pacific Standard Time'} }
function New-ScheduledTaskTrigger {
 param([switch]$Daily,[string]$At)
 [pscustomobject]@{StartBoundary='';Enabled=$true;DaysInterval=1;CimClass=[pscustomobject]@{CimClassName='MSFT_TaskDailyTrigger'}}
}
$old=@(
 [pscustomobject]@{StartBoundary='2026-10-09T21:05:00';Enabled=$true;DaysInterval=1;CimClass=[pscustomobject]@{CimClassName='MSFT_TaskDailyTrigger'}},
 [pscustomobject]@{StartBoundary='2026-10-09T15:13:24-07:00';Enabled=$true;Repetition=[pscustomobject]@{Interval='PT5M'};CimClass=[pscustomobject]@{CimClassName='MSFT_TaskTimeTrigger'}},
 [pscustomobject]@{UserId='retained-user';Enabled=$true;CimClass=[pscustomobject]@{CimClassName='MSFT_TaskLogonTrigger'}}
)
if (ALREADY_INSTALLED) { $t=New-ScheduledTaskTrigger -Daily -At '17:30';$t.StartBoundary='2026-10-26T17:30:00';$old += $t }
$global:mockTask=[pscustomobject]@{
 TaskName='Owned dispatch';TaskPath='\';Description='Ducketz deterministic responsibility dispatch. old';State='Ready'
 Triggers=$old;Actions=@([pscustomobject]@{Execute=PYTHON_PATH;Arguments=ARGUMENTS;WorkingDirectory=REPO_PATH})
 Settings=[pscustomobject]@{Enabled=TASK_ENABLED;StartWhenAvailable=$true;MultipleInstances='IgnoreNew';PrivateSetting='retain'}
 Principal=[pscustomobject]@{UserId='original';LogonType='InteractiveToken';RunLevel='Highest'}
}
$before=$global:mockTask | ConvertTo-Json -Depth 20
function Get-ScheduledTask { param($TaskName,$TaskPath,$ErrorAction) $global:mockTask }
function Get-ScheduledTaskInfo {
 param($TaskName)
 [pscustomobject]@{NextRunTime=[datetime]'2026-10-27T17:30:00';LastRunTime=[datetime]'2026-10-26T17:03:24';LastTaskResult=0}
}
function Register-ScheduledTask { throw 'Existing registration must not be recreated' }
function New-ScheduledTaskAction { throw 'Action must be preserved' }
function New-ScheduledTaskPrincipal { throw 'Principal must be preserved' }
function New-ScheduledTaskSettingsSet { throw 'Settings must be preserved' }
function Set-ScheduledTask {
 param($InputObject)
 $global:mockTask=$InputObject
 $global:mockTask
}
& HELPER_PATH -Repository REPO_PATH -Python PYTHON_PATH -Config CONFIG_PATH -TaskName 'Owned dispatch' | Out-Null
$first=$global:mockTask | ConvertTo-Json -Depth 20
& HELPER_PATH -Repository REPO_PATH -Python PYTHON_PATH -Config CONFIG_PATH -TaskName 'Owned dispatch' | Out-Null
[pscustomobject]@{Before=($before|ConvertFrom-Json);First=($first|ConvertFrom-Json);After=$global:mockTask} | ConvertTo-Json -Depth 20
'''.replace("ALREADY_INSTALLED", "$true" if already_installed else "$false")
        .replace("TASK_ENABLED", "$true" if enabled else "$false")
        .replace("HELPER_PATH", _ps(SCRIPT))
        .replace("PYTHON_PATH", _ps(Path(shell)))
        .replace("REPO_PATH", _ps(tmp_path))
        .replace("CONFIG_PATH", _ps(harness))
        .replace("ARGUMENTS", _ps(f'-B -m tools.nightly_watchdog --config "{harness}"')),
        encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", str(harness)],
                            capture_output=True, text=True, timeout=30, check=True)
    result = json.loads(result.stdout)
    before, first, after = result["Before"], result["First"], result["After"]
    assert first == after, "Repeated installation must not add another trigger"
    assert len(after["Triggers"]) == 4
    assert after["Triggers"][:3] == before["Triggers"][:3]
    assert after["Triggers"][3]["StartBoundary"].endswith("T17:30:00")
    assert after["Triggers"][3]["DaysInterval"] == 1
    assert after["Triggers"][3]["Enabled"] is True
    for key in ("Actions", "Settings", "Principal", "State", "TaskName", "TaskPath"):
        assert after[key] == before[key]


def _ps(value):
    return "'" + str(value).replace("'", "''") + "'"
