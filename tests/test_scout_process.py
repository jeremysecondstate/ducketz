"""Real Windows process fixtures; no trader, broker, provider, or live store."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from tools.scout_process import ContainmentError, ManagedWorker, inspect_job, process_in_job


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows kernel Job Object contract")


FIXTURE_JSON_WRITER = """
def publish_json(path, payload):
    import json, os, pathlib
    path = pathlib.Path(path)
    temporary = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    temporary.write_text(json.dumps(payload), encoding='utf-8')
    os.replace(temporary, path)
"""


def eventually(predicate, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Fixture did not reach expected bounded state")


def dead(identity):
    try:
        p = psutil.Process(identity["pid"])
        return not p.is_running() or abs(p.create_time() - identity["created"]) > 0.001
    except psutil.NoSuchProcess:
        return True


def fixture_script(tmp_path, name, source):
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return script


def child_tree(tmp_path):
    return fixture_script(tmp_path, "tree.py", FIXTURE_JSON_WRITER + """
import json, os, pathlib, subprocess, sys, time
import psutil
root=pathlib.Path(sys.argv[1])
if len(sys.argv)>2:
    publish_json(root/'grandchild.json', {'pid':os.getpid(),'created':psutil.Process().create_time()})
    while True: time.sleep(.05)
publish_json(root/'child.json', {'pid':os.getpid(),'created':psutil.Process().create_time()})
child=subprocess.Popen([sys.executable,'-B',__file__,str(root),'grandchild'], creationflags=subprocess.CREATE_NO_WINDOW)
if (root/'exit-root').exists():
    while not (root/'grandchild.json').exists(): time.sleep(.01)
    sys.exit(42)
while True: time.sleep(.05)
""")


def test_suspended_child_has_no_effect_until_resume_and_job_is_visible(tmp_path):
    effect = tmp_path / "effect"
    script = fixture_script(tmp_path, "effect.py", "from pathlib import Path\nPath('effect').write_text('done')\n")
    with ManagedWorker([sys.executable, "-B", str(script)], cwd=tmp_path, start_suspended=True) as worker:
        assert worker.poll() is None
        assert worker.pid > 0 and worker.create_time > 0
        assert worker.create_time == psutil.Process(worker.pid).create_time()
        assert inspect_job(worker.job_name) == 1
        assert process_in_job(worker.pid, worker.job_name)
        assert not process_in_job(os.getpid(), worker.job_name)
        time.sleep(0.1)
        assert not effect.exists()
        worker.resume()
        assert worker.wait(timeout=10) == 0
        assert worker.await_empty(timeout=10)
        assert effect.read_text() == "done"
        with pytest.raises(ContainmentError, match="only be resumed once"):
            worker.resume()
    assert worker.job_empty()
    assert inspect_job(worker.job_name) is None


def test_stop_before_resume_reaps_suspended_worker_without_effect(tmp_path):
    script = fixture_script(tmp_path, "effect.py", "from pathlib import Path\nPath('effect').write_text('bad')\n")
    worker = ManagedWorker([sys.executable, "-B", str(script)], cwd=tmp_path, start_suspended=True)
    identity = {"pid":worker.pid, "created":worker.create_time}
    assert worker.terminate(timeout=10) == 70
    assert worker.job_empty()
    worker.close()
    assert dead(identity)
    assert not (tmp_path / "effect").exists()


def test_root_exit_does_not_falsely_report_grandchild_drained(tmp_path):
    script = child_tree(tmp_path)
    (tmp_path / "exit-root").touch()
    with ManagedWorker([sys.executable, "-B", str(script), str(tmp_path)], cwd=tmp_path) as worker:
        assert worker.wait(timeout=10) == 42
        identity = json.loads((tmp_path / "grandchild.json").read_text())
        assert not worker.job_empty()
        assert inspect_job(worker.job_name) >= 1
        assert process_in_job(identity["pid"], worker.job_name)
        with pytest.raises(ContainmentError, match="remain after bounded drain"):
            worker.await_empty(timeout=0)
        assert not dead(identity)
        worker.terminate(timeout=10)
        assert worker.job_empty()
        eventually(lambda: dead(identity))
    assert not process_in_job(identity["pid"], worker.job_name)


def test_close_terminates_only_owned_tree_and_retains_output(tmp_path):
    script = child_tree(tmp_path)
    log = tmp_path / "worker.log"
    log.write_text("predecessor\n")
    unrelated = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(30)"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        worker = ManagedWorker([sys.executable, "-B", str(script), str(tmp_path)], cwd=tmp_path, log_path=log)
        eventually(lambda: (tmp_path / "grandchild.json").exists())
        identities = [json.loads((tmp_path / name).read_text()) for name in ("child.json", "grandchild.json")]
        worker.close(timeout=10)
        assert worker.job_empty()
        eventually(lambda: all(dead(identity) for identity in identities))
        assert unrelated.poll() is None
        assert log.read_text().startswith("predecessor\n")
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=10)


@pytest.mark.parametrize("suspended", [False, True])
def test_controller_hard_failure_kills_owned_children(tmp_path, suspended):
    tree = child_tree(tmp_path)
    controller = fixture_script(tmp_path, "controller.py", FIXTURE_JSON_WRITER + """
import json, pathlib, sys, time
from tools.scout_process import ManagedWorker
root=pathlib.Path(sys.argv[1])
worker=ManagedWorker([sys.executable,'-B',sys.argv[2],str(root)],cwd=root,start_suspended=sys.argv[3]=='True')
publish_json(root/'controller-ready.json', {'pid':worker.pid,'created':worker.create_time,'job':worker.job_name})
while True: time.sleep(.05)
""")
    env = {**os.environ, "PYTHONPATH":str(Path(__file__).resolve().parents[1]), "PYTHONDONTWRITEBYTECODE":"1"}
    owner = subprocess.Popen([sys.executable, "-B", str(controller), str(tmp_path), str(tree), str(suspended)],
                             env=env, cwd=tmp_path, creationflags=subprocess.CREATE_NO_WINDOW)
    identities = []
    try:
        eventually(lambda: (tmp_path / "controller-ready.json").exists())
        identity = json.loads((tmp_path / "controller-ready.json").read_text())
        identities.append(identity)
        if not suspended:
            eventually(lambda: (tmp_path / "grandchild.json").exists())
            identities.append(json.loads((tmp_path / "grandchild.json").read_text()))
        owner.kill()  # Only this private fixture controller, never a real application.
        owner.wait(timeout=10)
        eventually(lambda: all(dead(item) for item in identities))
        eventually(lambda: inspect_job(identity["job"]) in (None, 0))
        if suspended:
            assert not (tmp_path / "child.json").exists()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=10)


def test_failed_create_does_not_leave_job_or_run_code(tmp_path, monkeypatch):
    import tools.scout_process as processes
    real_api = processes._api
    names = []

    class FailingAPI:
        def __init__(self):
            self.kernel = real_api()

        def __getattr__(self, name):
            return getattr(self.kernel, name)

        def CreateJobObjectW(self, security, name):
            names.append(name)
            return self.kernel.CreateJobObjectW(security, name)

        def CreateProcessW(self, *args):
            import ctypes
            ctypes.set_last_error(5)
            return 0

    monkeypatch.setattr(processes, "_api", FailingAPI)
    with pytest.raises(ContainmentError, match="Create contained process failed"):
        ManagedWorker([sys.executable, "-B", "-c", "raise Exception('must not run')"], cwd=tmp_path)
    assert len(names) == 1
    assert inspect_job(names[0]) is None


def test_log_environment_and_command_quoting(tmp_path):
    script = fixture_script(tmp_path, "with spaces.py", "import os,sys\nprint(os.environ['SCOUT_FIXTURE'])\nprint(sys.argv[1])\n")
    log = tmp_path / "worker.log"
    with ManagedWorker([sys.executable, "-B", str(script), 'spaces and "quotes"'], cwd=tmp_path,
                       env={**os.environ, "SCOUT_FIXTURE":"private fixture only"}, log_path=log) as worker:
        assert worker.wait(timeout=10) == 0
    assert log.read_text().splitlines() == ["private fixture only", 'spaces and "quotes"']


def test_failed_setup_after_atomic_creation_reaps_without_resuming(tmp_path, monkeypatch):
    import tools.scout_process as processes
    real_api = processes._api
    created = []

    class FailingSetupAPI:
        def __init__(self):
            self.kernel = real_api()

        def __getattr__(self, name):
            return getattr(self.kernel, name)

        def CreateProcessW(self, *args):
            result = self.kernel.CreateProcessW(*args)
            if result:
                pid = args[-1]._obj.dwProcessId
                created.append({"pid": pid, "created": psutil.Process(pid).create_time()})
            return result

        def GetProcessTimes(self, *args):
            import ctypes
            ctypes.set_last_error(5)
            return 0

    monkeypatch.setattr(processes, "_api", FailingSetupAPI)
    with pytest.raises(ContainmentError, match="GetProcessTimes failed"):
        ManagedWorker([sys.executable, "-B", "-c", "from pathlib import Path;Path('effect').touch()"], cwd=tmp_path)
    assert len(created) == 1
    eventually(lambda: dead(created[0]))
    assert not (tmp_path / "effect").exists()


def test_unbounded_or_invalid_timeout_rejected_before_shutdown(tmp_path):
    with ManagedWorker([sys.executable, "-B", "-c", "import time;time.sleep(30)"], cwd=tmp_path) as worker:
        for bad in (-1, float("inf"), float("nan"), 301):
            with pytest.raises(ValueError, match="finite process timeout"):
                worker.terminate(timeout=bad)
        with pytest.raises(subprocess.TimeoutExpired):
            worker.wait(timeout=0)


def test_owned_child_cannot_escape_with_breakaway_flag(tmp_path):
    script = fixture_script(tmp_path, "breakaway.py", FIXTURE_JSON_WRITER + """
import json, pathlib, subprocess, sys, time
import psutil
try:
    child=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(30)'],
                           creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB|subprocess.CREATE_NO_WINDOW)
except OSError:
    pathlib.Path('blocked').touch()
else:
    publish_json('created.json', {'pid':child.pid,'created':psutil.Process(child.pid).create_time()})
while True: time.sleep(.05)
""")
    with ManagedWorker([sys.executable, "-B", str(script)], cwd=tmp_path, log_path=tmp_path / "breakaway.log") as worker:
        eventually(lambda: (tmp_path / "blocked").exists() or (tmp_path / "created.json").exists())
        if (tmp_path / "created.json").exists():
            # Nested Windows jobs may accept the flag without removing this
            # non-breakaway job. Native membership, not API rejection, is proof.
            identity = json.loads((tmp_path / "created.json").read_text())
            assert process_in_job(identity["pid"], worker.job_name)
            worker.terminate(timeout=10)
            eventually(lambda: dead(identity))
