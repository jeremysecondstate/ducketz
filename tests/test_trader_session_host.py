"""Actual subprocess/lifetime proofs; every path and action is an offline fixture."""
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from tools.trader_session_host import own_session, write_json

FIXTURE = Path(__file__).with_name("trader_renewal_subprocess_fixture.py")


def wait_file(path, timeout=15):
    deadline = time.monotonic()+timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"Fixture did not reach {path.name}")
        time.sleep(.025)
    return json.loads(path.read_text())


def launch(root, trader, **config):
    write_json(root/"config.json",dict(trader=trader,**config))
    write_json(root/"installed.json",{"version":"v1"})
    stream = (root/"process.log").open("wb")
    child = subprocess.Popen([sys.executable,"-I","-S","-B",str(FIXTURE),"anchor",str(root)],
         stdout=stream,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name=="nt" else 0)
    stream.close()
    return child


@pytest.mark.parametrize("trader",["gameplan","intraday"])
def test_consecutive_fresh_workers_one_session_new_installed_behavior(tmp_path,trader):
    child=launch(tmp_path,trader,install_phase="handoff",turns=4)
    assert child.wait(timeout=30)==0, (tmp_path/"process.log").read_text()
    actions=[json.loads((tmp_path/f"action-{n}.json").read_text()) for n in range(1,5)]
    assert len({a["pid"] for a in actions})==4
    assert {a["session_id"] for a in actions}=={"one-manual-session"}
    assert [a["version"] for a in actions]==["v1","v2","v2","v2"]
    assert [a["count"] for a in actions]==[1,2,3,4]
    assert all((datetime.fromisoformat(b["at"])-datetime.fromisoformat(a["at"])).total_seconds()>=.14
               for a,b in zip(actions,actions[1:]))
    outcome=json.loads((tmp_path/"session/outcome.json").read_text())
    assert outcome["disposition"]=="SESSION_COMPLETE"
    assert outcome["generations"]=={"execution":4}
    time.sleep(.2)
    assert not (tmp_path/"action-5.json").exists()


@pytest.mark.parametrize("trader",["gameplan","intraday"])
@pytest.mark.parametrize("phase",["before_start","started","exited","handoff"])
def test_stop_at_each_handoff_phase_never_admits_successor(tmp_path,trader,phase):
    child=launch(tmp_path,trader,stop_phase=phase)
    assert child.wait(timeout=30)==0, (tmp_path/"process.log").read_text()
    assert not (tmp_path/"action-2.json").exists()
    if phase in {"before_start","started"}:
        assert not (tmp_path/"action-1.json").exists()
    assert json.loads((tmp_path/"session/outcome.json").read_text())["disposition"]=="MANUAL_STOP"


@pytest.mark.parametrize("trader",["gameplan","intraday"])
def test_stop_during_successor_startup(tmp_path,trader):
    child=launch(tmp_path,trader,hold_start=True)
    wait_file(tmp_path/"worker-start-1.json")
    write_json(tmp_path/"session/stop.json",{"reason":"MANUAL_STOP"})
    assert child.wait(timeout=20)==0
    assert not list(tmp_path.glob("action-*.json"))


@pytest.mark.parametrize("trader",["gameplan","intraday"])
def test_unknown_crash_is_terminal_no_restart(tmp_path,trader):
    child=launch(tmp_path,trader,worker_crash=True)
    assert child.wait(timeout=20)==1
    outcome=json.loads((tmp_path/"session/outcome.json").read_text())
    assert outcome["generations"]=={"execution":1}
    assert outcome["disposition"]=="FAILURE"


@pytest.mark.parametrize("trader",["gameplan","intraday"])
def test_duplicate_launch_cannot_acquire_lifetime(tmp_path,trader):
    child=launch(tmp_path,trader,hold_start=True)
    try:
        wait_file(tmp_path/"worker-start-1.json")
        with pytest.raises((OSError,RuntimeError)):
            with own_session(tmp_path,trader):
                raise AssertionError("Duplicate owner acquired")
    finally:
        write_json(tmp_path/"session/stop.json",{"reason":"MANUAL_STOP"})
        child.wait(timeout=20)


@pytest.mark.skipif(os.name!="nt",reason="Windows job/console lifecycle")
@pytest.mark.parametrize("trader",["gameplan","intraday"])
@pytest.mark.parametrize("termination",["kill","ctrl_break"])
def test_controller_death_or_console_interrupt_removes_workers(tmp_path,trader,termination):
    import psutil
    child=launch(tmp_path,trader,hold_start=True)
    worker=wait_file(tmp_path/"worker-start-1.json")["pid"]
    process=psutil.Process(worker)
    original_birth=process.create_time()
    if termination=="kill":
        child.kill()
    else:
        child.send_signal(signal.CTRL_BREAK_EVENT)
    child.wait(timeout=20)
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        try:
            if not process.is_running() or process.create_time()!=original_birth:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(.05)
    else:
        raise AssertionError("Kernel-owned worker was orphaned")
    assert not list(tmp_path.glob("action-*.json"))


def test_stale_lock_is_recovered_without_signalling_any_pid(tmp_path):
    lock=tmp_path/"locks/independent-stock-session.lock"
    lock.parent.mkdir()
    lock.write_text("process=independent-stock-session\npid=2147483647\nowner_created_at=1\nstarted_at=2000-01-01T00:00:00+00:00\ntoken=old\n")
    with own_session(tmp_path,"gameplan"):
        assert f"pid={os.getpid()}" in lock.read_text()
    assert not lock.exists()
    assert list(lock.parent.glob("*.stale-*"))
