"""Real Windows console cancellation, using only disposable synthetic children.

The production launcher body, datastore, controls and trader are never invoked.
Console signals come from a separate no-console helper attached to the fixture's
NEW console; the pytest/Codex console is never attached or signalled.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows manual launcher lifetime")
REPOSITORY = Path(__file__).resolve().parents[1]
POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


def _until(predicate, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Synthetic process fixture did not reach its expected state")


def _write_fixture(root):
    child = root / "synthetic.py"
    child.write_text('''import json,os,subprocess,sys,time
from pathlib import Path
root=Path(sys.argv[1]); kind=sys.argv[2]
if kind=="worker":
    child=subprocess.Popen([sys.executable,"-u",__file__,str(root),"grandchild"],creationflags=subprocess.CREATE_NO_WINDOW)
    (root/"worker.json").write_text(json.dumps({"pid":os.getpid(),"child":child.pid}))
else:
    (root/"grandchild.json").write_text(json.dumps({"pid":os.getpid()}))
time.sleep(90)
''', encoding="utf-8")
    runner = root / "manual-fixture.ps1"
    runner.write_text(r'''
param([string]$Repository,[string]$Root,[string]$PythonPath,[string]$Mode,[int]$WorkerPid=0,[int]$LauncherPid=0)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $Repository 'docs/datafetch-ml/start_stock_session.ps1'),[ref]$tokens,[ref]$errors)
if($errors.Count -ne 0){throw ($errors | Out-String)}
foreach($name in @('Enable-ManualStockSessionLifetime','Add-VerifiedWorkerToManualLifetime','Write-StockSessionLogOutput','Wait-StockSessionWithOutput')) {
    $definition=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$false)
    if($null -eq $definition){throw "Missing actual launcher function: $name"}
    Invoke-Expression $definition.Extent.Text
}
if($Mode -ne 'nonmanual'){ Enable-ManualStockSessionLifetime }
if($Mode -in @('adopt','wrongbirth','wronglauncherbirth','attachfailure')) {
    $worker=Get-Process -Id $WorkerPid
    $launcher=Get-Process -Id $LauncherPid
    $birth=$worker.StartTime.ToUniversalTime().ToString('o')
    $launcherBirth=$launcher.StartTime.ToUniversalTime().ToString('o')
    if($Mode -eq 'wrongbirth'){$birth=$worker.StartTime.ToUniversalTime().AddSeconds(-1).ToString('o')}
    if($Mode -eq 'wronglauncherbirth'){$launcherBirth=$launcher.StartTime.ToUniversalTime().AddSeconds(-1).ToString('o')}
    try {
        if($Mode -eq 'attachfailure') {
            # Actual native first attach succeeds, second invalid handle fails.
            # Its inert candidate job must not kill the attached worker.
            [Ducketz.ManualStockSessionLifetime]::Adopt($worker.Handle,[IntPtr]::Zero)
        } else {
            Add-VerifiedWorkerToManualLifetime -Process $worker -ExpectedCreatedAt $birth -LauncherProcess $launcher -LauncherCreatedAt $launcherBirth
        }
    } catch {
        if($Mode -notin @('wrongbirth','wronglauncherbirth','attachfailure')){throw}
        [IO.File]::WriteAllText((Join-Path $Root 'refused.txt'),'REFUSED')
        exit 9
    }
    if($Mode -in @('wrongbirth','wronglauncherbirth','attachfailure')){throw 'Invalid adoption unexpectedly succeeded'}
} else {
    $args=@('-u',('"'+(Join-Path $Root 'synthetic.py')+'"'),('"'+$Root+'"'),'worker')
    $stdout=Join-Path $Root 'stdout.log'; $stderr=Join-Path $Root 'stderr.log'
    $worker=Start-Process -FilePath $PythonPath -ArgumentList $args -RedirectStandardOutput $stdout -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    $null=$worker.Handle
}
[GC]::Collect(); [GC]::WaitForPendingFinalizers()
[IO.File]::WriteAllText((Join-Path $Root 'ready.txt'),'READY')
if($Mode -eq 'adopt') { $worker | Wait-Process }
else { Wait-StockSessionWithOutput -Process $worker -StandardOutput $stdout -StandardError $stderr }
exit 0
''', encoding="utf-8")
    sender = root / "send-console-event.py"
    sender.write_text('''import ctypes,sys,time
k=ctypes.WinDLL("kernel32",use_last_error=True)
u=ctypes.WinDLL("user32",use_last_error=True)
k.GetConsoleWindow.restype=ctypes.c_void_p
k.FreeConsole()  # The virtualenv shim may give its child a private console.
assert k.AttachConsole(int(sys.argv[1])),ctypes.get_last_error()
assert k.SetConsoleCtrlHandler(None,True),ctypes.get_last_error()
if sys.argv[2]=="close":
    window=k.GetConsoleWindow()
    assert window,"Fixture console window missing"
    u.PostMessageW.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_size_t,ctypes.c_ssize_t]
    assert u.PostMessageW(window,0x10,0,0),ctypes.get_last_error()
else:
    assert k.GenerateConsoleCtrlEvent(0,0),ctypes.get_last_error()
time.sleep(0.3)
k.FreeConsole()
''', encoding="utf-8")
    return runner, sender, child


def _launch(root, runner, mode, *, worker_pid=0, launcher_pid=0):
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    # Only the fixture owns this new console. Never use our caller's console.
    return subprocess.Popen(
        [str(POWERSHELL), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner),
         "-Repository", str(REPOSITORY), "-Root", str(root), "-PythonPath", sys.executable, "-Mode", mode,
         "-WorkerPid", str(worker_pid), "-LauncherPid", str(launcher_pid)],
        creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _fixture_processes(root):
    import psutil
    _until(lambda: (root / "grandchild.json").exists() and (root / "worker.json").exists())
    return [psutil.Process(json.loads((root / name).read_text())["pid"])
            for name in ("worker.json", "grandchild.json")]


def _cleanup(supervisor, processes):
    # psutil retains creation identity and checks PID reuse before killing.
    for process in processes:
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception as exc:
            import psutil
            if not isinstance(exc, (psutil.NoSuchProcess, psutil.ZombieProcess)):
                raise
    if supervisor.poll() is None:
        supervisor.kill()
    supervisor.communicate(timeout=10)


@pytest.mark.parametrize(("mode", "signal"), [("manual", "ctrl_c"), ("manual", "close"), ("nonmanual", "ctrl_c")])
def test_real_console_cancel_stops_only_manual_children(tmp_path, mode, signal):
    runner, sender, _ = _write_fixture(tmp_path)
    supervisor = _launch(tmp_path, runner, mode)
    processes = []
    try:
        _until(lambda: (tmp_path / "ready.txt").exists() or supervisor.poll() is not None)
        assert supervisor.poll() is None, supervisor.communicate()
        processes = _fixture_processes(tmp_path)
        sent = subprocess.run([sys.executable, str(sender), str(supervisor.pid), signal],
                              creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, text=True, timeout=10)
        assert sent.returncode in ({0, 0xC000013A} if signal == 'close' else {0}), sent.stdout + sent.stderr
        supervisor.wait(timeout=12)
        if mode == "manual":
            for process in processes:
                process.wait(timeout=8)
        else:
            assert all(process.is_running() for process in processes)
    finally:
        _cleanup(supervisor, processes)


@pytest.mark.parametrize("mode", ["adopt", "wrongbirth", "wronglauncherbirth", "attachfailure"])
def test_adopted_exact_handles_stop_and_invalid_adoption_leaves_them_alive(tmp_path, mode):
    import psutil
    runner, sender, child = _write_fixture(tmp_path)
    original = subprocess.Popen([sys.executable, "-u", str(child), str(tmp_path), "worker"],
                                creationflags=subprocess.CREATE_NO_WINDOW)
    processes = _fixture_processes(tmp_path)
    # The original executable can be a virtualenv shim. Adopt its captured
    # launcher plus actual worker, as the production validator requires.
    launcher = psutil.Process(original.pid)
    supervisor = _launch(tmp_path, runner, mode, worker_pid=processes[0].pid, launcher_pid=launcher.pid)
    try:
        if mode == "adopt":
            _until(lambda: (tmp_path / "ready.txt").exists() or supervisor.poll() is not None)
            assert supervisor.poll() is None, supervisor.communicate()
            assert all(process.is_running() for process in processes)  # GC did not close retained job.
            sent = subprocess.run([sys.executable, str(sender), str(supervisor.pid), "ctrl_c"],
                                  creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, text=True, timeout=10)
            assert sent.returncode == 0, sent.stdout + sent.stderr
            supervisor.wait(timeout=12)
            processes[0].wait(timeout=8)
            launcher.wait(timeout=8)
            # Existing descendants are not enumerated or blindly adopted. The
            # real worker's validated pair, not arbitrary names, is authoritative.
        else:
            output = supervisor.communicate(timeout=15)
            assert supervisor.returncode == 9, output
            assert (tmp_path / "refused.txt").read_text() == "REFUSED"
            assert all(process.is_running() for process in processes)
            assert launcher.is_running()
    finally:
        _cleanup(supervisor, [*processes, launcher])
        original.wait(timeout=5)
