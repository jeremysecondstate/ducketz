"""Offline native task XML checks; scheduler APIs are fixtures, never real writes."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest


SHELL = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(os.name != "nt" or not SHELL, reason="Windows PowerShell task XML fixture checks")
SCRIPT = Path(__file__).resolve().parents[1] / "tools/register_intraday_model_supervisor.ps1"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


def psquote(value):
    return "'" + str(value).replace("'", "''") + "'"


def invoke(arguments, *, fixture="", expect_success=True):
    # All native scheduler calls are replaced before entering the portable script.
    # Unmocked mutations fail loudly even when an assertion under test regresses.
    source = """
$ErrorActionPreference = 'Stop'
function Get-TimeZone { [pscustomobject]@{Id='Pacific Standard Time'} }
function Get-ScheduledTask { param($TaskName,$TaskPath) return @() }
function Register-ScheduledTask { throw 'Fixture forbids native registration' }
function Export-ScheduledTask { throw 'Fixture forbids native export' }
function Get-ScheduledTaskInfo { throw 'Fixture forbids native task reads' }
function Start-ScheduledTask { throw 'Fixture forbids starting any task' }
function Stop-ScheduledTask { throw 'Fixture forbids stopping any task' }
function Unregister-ScheduledTask { throw 'Fixture forbids deleting any task' }
""" + fixture
    parameters = []
    iterator = iter(arguments)
    for name in iterator:
        value = "$true" if name in {"-Plan", "-ValidateOnly"} else psquote(next(iterator))
        parameters.append(name.lstrip("-") + "=" + value)
    source += "\n$fixtureBindings = @{" + ";".join(parameters) + "}\n& " + psquote(SCRIPT) + " @fixtureBindings\n"
    encoded = base64.b64encode(source.encode("utf-16le")).decode("ascii")
    result = subprocess.run([SHELL, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, text=True, encoding="utf-8", timeout=30)
    if expect_success:
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout.lstrip("\ufeff"))
    assert result.returncode != 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def bindings(tmp_path):
    root = tmp_path / "bindings & local"
    root.mkdir()
    datastore = root / "data"
    datastore.mkdir()
    names = {"Python": "pythonw.exe", "Profile": "profile.json", "Watchlist": "watchlist.txt", "Config": "config.json"}
    args = ["-Repository", str(root), "-Datastore", str(datastore), "-LogDirectory", str(root / "logs"),
            "-TaskName", "Ducketz 15m Model Supervisor Fixture"]
    for name, filename in names.items():
        path = root / filename
        path.write_text("{}" if filename.endswith(".json") else "FIXTURE", encoding="utf-8")
        args += ["-" + name, str(path)]
    xml_path = root / "task.xml"
    return args, xml_path


def plan(tmp_path):
    args, xml_path = bindings(tmp_path)
    result = invoke(args + ["-Plan", "-XmlPath", str(xml_path)])
    return result, xml_path, ET.parse(xml_path).getroot()


def test_plan_exact_weekday_cadence_follows_pacific_dst_and_never_starts(tmp_path):
    result, xml_path, root = plan(tmp_path)
    assert result["Status"] == "PLANNED" and not result["StartsTrader"]
    assert result["Timezone"] == "America/Los_Angeles"
    triggers = root.findall("t:Triggers/t:CalendarTrigger", NS)
    assert len(triggers) == 6
    assert [trigger.findtext("t:StartBoundary", namespaces=NS)[11:] for trigger in triggers] == [
        "01:00:00", "01:30:00", "02:00:00", "02:30:00", "03:00:00", "03:30:00"]
    for trigger in triggers:
        assert [node.tag.rsplit("}", 1)[-1] for node in trigger.find("t:ScheduleByWeek/t:DaysOfWeek", NS)] == [
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
        assert trigger.findtext("t:ScheduleByWeek/t:WeeksInterval", namespaces=NS) == "1"
        assert trigger.find("t:EndBoundary", NS) is None
    action = root.find("t:Actions/t:Exec", NS)
    assert action.findtext("t:Command", namespaces=NS).endswith("pythonw.exe")
    assert action.findtext("t:Arguments", namespaces=NS).startswith("-B -m ml.intraday.supervisor --profile ")
    assert "bindings & local" in action.findtext("t:Arguments", namespaces=NS)
    assert invoke(["-ValidateOnly", "-XmlPath", str(xml_path)])["Status"] == "VALID"


def test_plan_preserves_running_work_and_manual_trading(tmp_path):
    _, _, root = plan(tmp_path)
    expected = {"MultipleInstancesPolicy": "IgnoreNew", "ExecutionTimeLimit": "PT0S", "AllowHardTerminate": "false",
                "StartWhenAvailable": "true", "StopIfGoingOnBatteries": "false", "RunOnlyIfNetworkAvailable": "false"}
    for key, value in expected.items():
        assert root.findtext("t:Settings/t:" + key, namespaces=NS) == value
    assert root.findtext("t:Settings/t:IdleSettings/t:StopOnIdleEnd", namespaces=NS) == "false"
    assert len(root.find("t:Actions", NS)) == 1
    arguments = root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=NS)
    assert "live_session" not in arguments and "start_stock_session" not in arguments
    assert not (tmp_path / "bindings & local/logs").exists()


@pytest.mark.parametrize("mutation, expected", [
    (lambda source: source.replace("<Friday/>", "<Saturday/>"), "Monday through Friday"),
    (lambda source: source.replace("T01:00:00</StartBoundary>", "T01:00:00-07:00</StartBoundary>"), "floating Pacific"),
    (lambda source: source.replace("<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", "<ExecutionTimeLimit>PT3M</ExecutionTimeLimit>"), "ExecutionTimeLimit"),
    (lambda source: source.replace("ml.intraday.supervisor", "ml.intraday.live_session"), "model preparation"),
    (lambda source: source.replace("<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>", "<MultipleInstancesPolicy>StopExisting</MultipleInstancesPolicy>"), "MultipleInstancesPolicy"),
])
def test_validate_rejects_schedule_cancellation_or_trading_regressions(tmp_path, mutation, expected):
    _, xml_path, _ = plan(tmp_path)
    xml_path.write_text(mutation(xml_path.read_text(encoding="utf-16")), encoding="utf-16")
    failure = invoke(["-ValidateOnly", "-XmlPath", str(xml_path)], expect_success=False)
    assert expected in failure


def test_registration_refuses_wrong_host_timezone_without_mutation(tmp_path):
    args, xml_path = bindings(tmp_path)
    failure = invoke(args + ["-Plan", "-XmlPath", str(xml_path)], fixture="\nfunction Get-TimeZone { [pscustomobject]@{Id='Eastern Standard Time'} }\n", expect_success=False)
    assert "Pacific Standard Time" in failure and not xml_path.exists()


@pytest.mark.parametrize("description, name, expected", [
    ("Some other purpose", "Ducketz 15m Model Supervisor Fixture", "belongs to another purpose"),
    ("Ducketz independent 15-minute model supervision v1. Existing purpose", "Existing owned supervisor", "equivalent supervisor already exists"),
])
def test_existing_identity_and_equivalent_purpose_never_get_overwritten_or_duplicated(tmp_path, description, name, expected):
    args, xml_path = bindings(tmp_path)
    fixture = "\nfunction Get-ScheduledTask { param($TaskName,$TaskPath) [pscustomobject]@{TaskName=" + psquote(name) + ";TaskPath='\\';Description=" + psquote(description) + ";Actions=@()} }\n"
    failure = invoke(args + ["-Plan", "-XmlPath", str(xml_path)], fixture=fixture, expect_success=False)
    assert expected in failure and not xml_path.exists()


def test_mock_owned_update_saves_exact_rollback_and_verifies_saved_xml(tmp_path):
    args, xml_path = bindings(tmp_path)
    old_xml = '<existing-task unchanged="true"/>'
    fixture = r"""
$script:registeredXml = $null
function Get-ScheduledTask { param($TaskName,$TaskPath)
    [pscustomobject]@{TaskName='Ducketz 15m Model Supervisor Fixture';TaskPath='\';State='Ready';Description='Ducketz independent 15-minute model supervision v1. Existing';Actions=@()}
}
function Register-ScheduledTask { param($TaskName,$TaskPath,$Xml,[switch]$Force) $script:registeredXml = $Xml }
function Export-ScheduledTask { param($TaskName,$TaskPath)
    if ($script:registeredXml) { return $script:registeredXml }
    return '<existing-task unchanged="true"/>'
}
function Get-ScheduledTaskInfo { param($TaskName,$TaskPath)
    [pscustomobject]@{NextRunTime=[datetime]'2026-10-15T01:00:00';LastRunTime=[datetime]'2026-10-14T01:00:00';LastTaskResult=0}
}
"""
    result = invoke(args + ["-XmlPath", str(xml_path)], fixture=fixture)
    assert result["Status"] == "REGISTERED" and not result["StartedForVerification"]
    assert Path(result["RollbackXml"]).read_text(encoding="utf-16") == old_xml
    assert invoke(["-ValidateOnly", "-XmlPath", str(xml_path)])["Status"] == "VALID"
