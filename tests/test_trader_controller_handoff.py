"""Real subprocess controller upgrades preserve the original manual lifetime."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from tools import trader_session_host as host

FIXTURE = Path(__file__).with_name("trader_controller_handoff_fixture.py")


def wait_file(path, timeout=15):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError("Fixture did not reach " + str(path))
        time.sleep(.02)
    return json.loads(path.read_text())


def launch(root, **config):
    host.write_json(root / "config.json", config)
    source = Path(host.__file__).read_text()
    for version in ("v1", "v2", "v3"):
        (root / f"host-{version}.py").write_text(source + f'\nFIXTURE_REVISION = "{version}"\n', encoding="utf-8")
    host.write_json(root / "installed.json", {"path": str(root / "host-v1.py")})
    stream = (root / "controller.log").open("wb")
    process = subprocess.Popen([sys._base_executable, "-I", "-S", "-B", str(FIXTURE), "controller",
                                str(root), str(root / "host-v1.py")], stdout=stream, stderr=subprocess.STDOUT,
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
    stream.close()
    return process


@pytest.mark.parametrize("trader", ["gameplan", "intraday"])
def test_real_controller_relay_keeps_one_session_and_complete_scientific_cursors(tmp_path, trader):
    process = launch(tmp_path, trader=trader)
    assert process.wait(timeout=30) == 0, (tmp_path / "controller.log").read_text()
    actions = [json.loads((tmp_path / f"action-{count}.json").read_text()) for count in range(1, 9)]
    assert len({row["pid"] for row in actions}) == 8
    assert len({row["controller"] for row in actions}) == 2
    assert {row["version"] for row in actions} == {"v1", "v2"}
    assert {row["session"] for row in actions} == {"one-manually-started-session"}
    assert {row["owner"] for row in actions} == {process.pid}
    assert [row["count"] for row in actions] == list(range(1, 9))
    assert sorted(path.name for path in tmp_path.glob("science-*.json")) == ["science-1.json", "science-2.json"]
    handoff, = list((tmp_path / "session").glob("controller-handoff-*.consumed.json"))
    assert json.loads(handoff.read_text())["pid"] == actions[-1]["controller"]
    outcome = json.loads((tmp_path / "session/outcome.json").read_text())
    assert outcome["disposition"] == "SESSION_COMPLETE"
    assert outcome["generations"] == {"forecast": 2, "execution": 8}
    assert not (tmp_path / "action-9.json").exists()
    active = json.loads((tmp_path / "state/trader-renewal" / trader / "active.json").read_text())
    assert active["status"] == "ENDED"
    assert active["owner_pid"] == process.pid
    assert active["controller_pid"] == actions[-1]["controller"]
    assert active["execution_generation"] == 8
    assert active["heartbeat_at"]


def test_updated_controller_can_relay_again_without_losing_parent_lifetime(tmp_path):
    process = launch(tmp_path, turns=12, second_update=True)
    assert process.wait(timeout=30) == 0, (tmp_path / "controller.log").read_text()
    actions = [json.loads((tmp_path / f"action-{count}.json").read_text()) for count in range(1, 13)]
    assert {row["version"] for row in actions} == {"v1", "v2", "v3"}
    assert len({row["controller"] for row in actions}) == 3
    assert {row["owner"] for row in actions} == {process.pid}
    assert len(list((tmp_path / "session").glob("controller-handoff-*.consumed.json"))) == 2


def test_runtime_only_update_renews_controller_with_identical_host_source(tmp_path):
    process = launch(tmp_path, runtime_only=True)
    assert process.wait(timeout=30) == 0, (tmp_path / "controller.log").read_text()
    actions = [json.loads((tmp_path / f"action-{count}.json").read_text()) for count in range(1, 9)]
    assert {row["version"] for row in actions} == {"v1"}
    assert {row["runtime"] for row in actions} == {"v1", "v2"}
    assert len({row["controller"] for row in actions}) == 2
    assert {row["owner"] for row in actions} == {process.pid}
    assert {row["session"] for row in actions} == {"one-manually-started-session"}


def test_rollback_while_draining_cancels_obsolete_controller_handoff(tmp_path):
    process = launch(tmp_path, rollback=True)
    assert process.wait(timeout=30) == 0, (tmp_path / "controller.log").read_text()
    actions = [json.loads((tmp_path / f"action-{count}.json").read_text()) for count in range(1, 9)]
    assert {row["version"] for row in actions} == {"v1"}
    assert {row["controller"] for row in actions} == {process.pid}
    assert not list((tmp_path / "session").glob("controller-handoff-*.consumed.json"))
    assert sorted(path.name for path in tmp_path.glob("science-*.json")) == ["science-1.json", "science-2.json"]


def test_failure_latch_remains_failure_through_waiting_ancestor(tmp_path):
    process = launch(tmp_path, crash_worker_after_relay=True)
    assert process.wait(timeout=30) == 1, (tmp_path / "controller.log").read_text()
    assert json.loads((tmp_path / "session/outcome.json").read_text())["disposition"] == "FAILURE"
    assert json.loads((tmp_path / "session/stop.json").read_text())["reason"] == "FAILURE"


@pytest.mark.parametrize("phase", ["controller_draining", "controller_handoff", "controller_started"])
def test_manual_stop_in_each_controller_handoff_phase_prevents_new_execution(tmp_path, phase):
    process = launch(tmp_path, stop_phase=phase)
    assert process.wait(timeout=30) == 0, (tmp_path / "controller.log").read_text()
    actions = [json.loads(path.read_text()) for path in tmp_path.glob("action-*.json")]
    assert actions and all(row["version"] == "v1" for row in actions)
    assert json.loads((tmp_path / "session/outcome.json").read_text())["disposition"] == "MANUAL_STOP"


def test_unknown_controller_failure_is_terminal_without_restart(tmp_path):
    process = launch(tmp_path, crash_successor=True)
    assert process.wait(timeout=30) == 1
    assert json.loads((tmp_path / "session/outcome.json").read_text())["disposition"] == "FAILURE"
    assert not list((tmp_path / "session").glob("*.consumed.json"))
    assert all(json.loads(path.read_text())["version"] == "v1" for path in tmp_path.glob("action-*.json"))


@pytest.mark.skipif(os.name != "nt", reason="Windows job-object process tree")
@pytest.mark.parametrize("mode", ["kill", "ctrl_break"])
def test_original_kernel_owner_termination_removes_relay_and_descendant_workers(tmp_path, mode):
    import psutil
    process = launch(tmp_path, hold_after_relay=True)
    relay = wait_file(tmp_path / "controller_started-v1.json")
    deadline = time.monotonic() + 15
    descendants = []
    while time.monotonic() < deadline:
        descendants = psutil.Process(process.pid).children(recursive=True)
        if len(descendants) >= 2 and any(path.exists() for path in tmp_path.glob("execution-enter-*.json")
                                         if json.loads(path.read_text())["controller"] != process.pid):
            break
        time.sleep(.025)
    assert len(descendants) >= 2
    if mode == "kill":
        process.kill()
    else:
        process.send_signal(signal.CTRL_BREAK_EVENT)
    process.wait(timeout=20)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(child.is_running() for child in descendants):
        time.sleep(.025)
    assert not any(child.is_running() for child in descendants)


def test_a_saved_handoff_without_its_exact_parent_cannot_start(tmp_path):
    process = launch(tmp_path)
    assert process.wait(timeout=30) == 0
    handoff, = list((tmp_path / "session").glob("controller-handoff-*.consumed.json"))
    request = handoff.with_name(handoff.name.replace(".consumed.json", ".json"))
    result = subprocess.run([sys._base_executable, "-I", "-S", "-B", str(FIXTURE), "controller", str(tmp_path),
                             str(tmp_path / "host-v2.py"), "--continue", str(request)], capture_output=True, timeout=15)
    assert result.returncode != 0
    assert b"living exact parent" in result.stderr
    assert not (tmp_path / "action-9.json").exists()
