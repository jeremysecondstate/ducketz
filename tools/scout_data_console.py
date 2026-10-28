"""Standalone Scout System 1A start/display. Default operation reads files only.

--start dispatches the existing enabled IgnoreNew Windows task once if idle.
Closing this viewer never stops data preparation or System 1B trading.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time

MAX_JSON = 65536
MAX_READ = 65536
MAX_LINE = 16384
INITIAL_METRICS = 16384


def emit(value):
    print(json.dumps(value, ensure_ascii=True, sort_keys=True), flush=True)


def task_operation(task_name, *, start=False, runner=None):
    """Bounded native inspection/one dispatch; names are data passed in the environment."""
    if os.name != "nt":
        raise RuntimeError("Explicit data-task start requires Windows")
    if not task_name or len(task_name) > 240 or any(c in task_name for c in "*?[]\r\n\0"):
        raise ValueError("An exact existing local task name is required")
    script = r'''$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$found=@(Get-ScheduledTask -TaskName $env:SCOUT_DATA_TASK_NAME -TaskPath '\' -ErrorAction Stop | Where-Object {$_.TaskName -ceq $env:SCOUT_DATA_TASK_NAME})
if($found.Count -ne 1){throw 'Exact data task not found'}
$t=$found[0]
$state=[string]$t.State
$enabled=[bool]$t.Settings.Enabled
$policy=[int]$t.Settings.MultipleInstances
$actions=@($t.Actions | ForEach-Object {@{execute=[string]$_.Execute;arguments=[string]$_.Arguments}})
$dataOnly=($actions.Count -eq 1 -and (Split-Path -Leaf $actions[0].execute) -match '^pythonw?\.exe$' -and $actions[0].arguments -match '(^|\s)--data-live(\s|$)' -and $actions[0].arguments -notmatch '(^|\s)--live(\s|$)')
$result=@{task_name=$t.TaskName;state=$state;enabled=$enabled;multiple_instances=$policy;actions=$actions;status='inspected'}
if($env:SCOUT_DATA_TASK_START -eq '1'){
 if((-not $enabled) -or $state -eq 'Disabled'){throw 'Data task is disabled; no start dispatched'}
 if($policy -ne 2){throw 'Data task must preserve IgnoreNew; no start dispatched'}
 if(-not $dataOnly){throw 'Task action is not data-only; no start dispatched'}
 if($state -eq 'Running'){$result.status='already_running'}
 else{Start-ScheduledTask -InputObject $t -ErrorAction Stop;$result.status='start_dispatched'}
}
$result | ConvertTo-Json -Compress
'''
    command = [str(Path(os.environ.get("SystemRoot", "C:/Windows")) /
                   "System32/WindowsPowerShell/v1.0/powershell.exe"),
               "-NoProfile", "-NonInteractive", "-Command", script]
    result = (runner or subprocess.run)(command, env=dict(os.environ,
        SCOUT_DATA_TASK_NAME=task_name, SCOUT_DATA_TASK_START="1" if start else "0"),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError("Native data task operation failed: " + (result.stderr or "no diagnostic")[:2048])
    if len(result.stdout.encode("utf-8")) > MAX_JSON:
        raise ValueError("Native task response exceeds display bound")
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or value.get("task_name") != task_name:
        raise ValueError("Native task response changed its exact identity")
    return value


def data_only_action(info):
    actions = info.get("actions")
    if not isinstance(actions, list) or len(actions) != 1 or not isinstance(actions[0], dict):
        return False
    executable = str(actions[0].get("execute", "")).replace("\\", "/").rsplit("/", 1)[-1].lower()
    arguments = str(actions[0].get("arguments", ""))
    return (executable in {"python.exe", "pythonw.exe"}
            and re.search(r"(?:^|\s)--data-live(?:\s|$)", arguments) is not None
            and re.search(r"(?:^|\s)--live(?:\s|$)", arguments) is None)


def start_existing_task(task_name, *, operation=None):
    native = operation or task_operation
    info = native(task_name, start=False)
    if info.get("enabled") is not True or info.get("state") == "Disabled":
        raise ValueError("Data task is disabled; no start dispatched")
    if info.get("multiple_instances") != 2:
        raise ValueError("Data task must preserve IgnoreNew; no start dispatched")
    if not data_only_action(info):
        raise ValueError("Task action is not data-only; no start dispatched")
    if info.get("state") == "Running":
        return {**info, "status": "already_running"}
    # The native dispatch rechecks enabled/running/IgnoreNew immediately before
    # its single Start-ScheduledTask. A concurrent scheduler start stays IgnoreNew.
    result = native(task_name, start=True)
    if not data_only_action(result):
        raise ValueError("Native dispatch changed its data-only task action")
    if result.get("status") not in {"already_running", "start_dispatched"}:
        raise ValueError("Native task dispatch returned no supported outcome")
    return result


class FileTail:
    """Read at most one bounded chunk per poll, with bounded incomplete lines."""
    def __init__(self, path, *, recent=False):
        self.path, self.recent = Path(path), recent
        self.offset, self.identity, self.pending = 0, None, b""
        self.discard = False
        self.historical, self.attach_pending = False, False
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                self.identity = (stat.st_dev, stat.st_ino)
                self.offset = max(0, stat.st_size - INITIAL_METRICS) if recent else stat.st_size
                self.discard = recent and self.offset > 0
                self.historical = recent and stat.st_size > 0
        except FileNotFoundError:
            pass
        except OSError:
            self.attach_pending = True  # Existing/new bytes cannot yet be distinguished.

    def read(self):
        lines, notices = [], []
        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                if self.attach_pending:
                    self.offset = max(0, stat.st_size - INITIAL_METRICS) if self.recent else stat.st_size
                    self.discard = self.recent and self.offset > 0
                    self.historical = self.recent and stat.st_size > 0
                    self.attach_pending = False
                    notices.append("delayed attachment; existing bytes are historical/skipped")
                if ((self.identity is not None and self.identity != identity) or stat.st_size < self.offset):
                    self.offset, self.pending, self.discard = 0, b"", False
                    self.historical = False
                    notices.append("file replaced/truncated; following new bytes")
                self.identity = identity
                stream.seek(self.offset)
                raw = stream.read(MAX_READ)
                self.offset += len(raw)
            old = self.historical
            self.historical = False
            for part in raw.splitlines(keepends=True):
                ended = part.endswith(b"\n")
                if self.discard:
                    if ended:
                        self.discard = False
                    continue
                self.pending += part
                if len(self.pending) > MAX_LINE:
                    self.pending = b""
                    self.discard = not ended
                    notices.append("oversize line omitted; original retained")
                elif ended:
                    lines.append((self.pending.rstrip(b"\r\n").decode("utf-8", "replace"), old))
                    self.pending = b""
            return lines, notices
        except FileNotFoundError:
            return [], ["file not present yet"]
        except OSError as exc:
            return [], ["read unavailable: " + type(exc).__name__]


def read_status(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("Status JSON exceeds display bound")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Status JSON is not an object")
    return value


class DataConsole:
    def __init__(self, root, *, sink=emit, monotonic=time.monotonic, heartbeat_seconds=30):
        self.root, self.sink, self.monotonic = Path(root), sink, monotonic
        self.heartbeat_seconds = heartbeat_seconds
        self.tails = {name: FileTail(self.root / name, recent=name == "data-metrics.jsonl")
                      for name in ("data-metrics.jsonl", "data.stdout.log", "data.stderr.log")}
        self.status_signature, self.status_at, self.notices = None, float("-inf"), {}

    def poll(self):
        now = self.monotonic()
        try:
            saved = read_status(self.root / "data-status.json")
            errors = saved.get("errors", [])
            if not isinstance(errors, list):
                raise ValueError("Status errors are not a list")
            value = {"event": "system_1a_status", "evidence": "saved service record; liveness not inferred",
                "status": saved.get("status"), "pid": saved.get("pid"),
                "service_heartbeat_at": saved.get("heartbeat_at"), "source_session": saved.get("source_session"),
                "recorded_loaded_release": saved.get("release"), "data_day": saved.get("data_day"),
                "wake_at": saved.get("wake_at"), "deadline": saved.get("deadline"),
                "startup_progress": saved.get("startup_progress"), "error_count": len(errors),
                "latest_saved_error": errors[-1] if errors else None,
                "source": str(self.root / "data-status.json")}
        except (OSError, ValueError, UnicodeError) as exc:
            value = {"event": "system_1a_status_unavailable", "reason": type(exc).__name__,
                     "source": str(self.root / "data-status.json")}
        signature = json.dumps({k:v for k,v in value.items() if k != "service_heartbeat_at"}, sort_keys=True)
        if signature != self.status_signature or now - self.status_at >= self.heartbeat_seconds:
            self.sink(value)
            self.status_signature, self.status_at = signature, now
        for name, tail in self.tails.items():
            lines, notices = tail.read()
            notice = tuple(notices)
            if notice and notice != self.notices.get(name):
                self.sink({"event": "system_1a_display_notice", "source": str(tail.path), "notes": notices})
            self.notices[name] = notice
            for text, historical in lines:
                if name == "data-metrics.jsonl":
                    try:
                        metric = json.loads(text)
                        if not isinstance(metric, dict):
                            raise ValueError("Metric is not an object")
                    except ValueError:
                        self.sink({"event": "system_1a_metric_unreadable", "source": str(tail.path),
                                   "historical": historical})
                        continue
                    self.sink({"event": "system_1a_metric", "historical": historical,
                               "recorded_at": metric.get("recorded_at"), "metric": metric, "source": str(tail.path)})
                else:
                    self.sink({"event": "system_1a_output", "stream": name, "text": text,
                               "source": str(tail.path), "observed_at": datetime.now(timezone.utc).isoformat()})


def set_console_title():
    try:
        if os.name == "nt":
            import ctypes
            setter = ctypes.windll.kernel32.SetConsoleTitleW
            setter.argtypes, setter.restype = [ctypes.c_wchar_p], ctypes.c_int
            setter("Scout 15m 1A - Data and Preparation")
    except Exception:
        pass  # A missing/detached console cannot affect the independent service.


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--task-name", help="Exact local native data task; required only with --start")
    parser.add_argument("--start", action="store_true", help="One explicit start of existing enabled data task")
    parser.add_argument("--once", action="store_true", help="One bounded read-only display capture")
    args = parser.parse_args(argv)
    if args.start and not args.task_name:
        parser.error("--task-name is required with --start")
    set_console_title()
    emit({"event": "system_1a_console", "title": "Scout 15m 1A - Data and Preparation",
          "mode": "explicit data start and display" if args.start else "display only",
          "closing_viewer_stops_service": False, "root": str(args.root)})
    if args.start:
        try:
            emit({"event": "system_1a_start", **start_existing_task(args.task_name)})
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            emit({"event": "system_1a_start_failed", "error_type": type(exc).__name__, "error": str(exc)[:2048],
                  "automatic_retry": False, "task_name": args.task_name})
            return 1
    viewer = DataConsole(args.root)
    try:
        while True:
            viewer.poll()
            if args.once:
                return 0
            time.sleep(1)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0  # Viewer closure has no process/task stop action.


if __name__ == "__main__":
    raise SystemExit(main())
