"""Initial controller selection never makes science await a bootstrap execution."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

host = pytest.importorskip("tools.trader_session_host", reason="Complete local renewal consumer required")

FIXTURE = Path(__file__).with_name("trader_initial_controller_handoff_fixture.py")


def wait_file(path, timeout=15):
    until = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > until:
            raise AssertionError("Fixture did not reach " + str(path))
        time.sleep(.02)
    return json.loads(path.read_text())


def launch(root, **config):
    host.write_json(root / "config.json", config)
    stream = (root / "controller.log").open("wb")
    child = subprocess.Popen([sys._base_executable, "-I", "-S", "-B", str(FIXTURE),
        "controller", str(root), str(Path(host.__file__).resolve()), "v1"],
        stdout=stream, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
    stream.close()
    return child


def release_and_finish(root, child):
    (root / "release").write_text("complete exact fixture iteration")
    assert child.wait(timeout=20) == 0, (root / "controller.log").read_text()


def test_initial_runtime_relay_admits_all_successor_lanes_before_execution_completes(tmp_path):
    child = launch(tmp_path)
    try:
        rows = [wait_file(tmp_path / ("entered-v2-" + role + ".json"))
                for role in ("execution", "forecast-0", "scoring")]
        envelope = json.loads((tmp_path / "session/session.json").read_text())
        assert child.poll() is None  # Execution is still held; science already ran.
        assert sorted(path.name for path in tmp_path.glob("entered-v1-*.json")) == ["entered-v1-initialize.json"]
        assert not (tmp_path / "entered-v2-initialize.json").exists()
        assert all(row["controller"] != child.pid for row in rows)
        assert {row["controller"] for row in rows} == {rows[0]["controller"]}
        assert all(row["token"] == envelope["token"] and row["owner_pid"] == child.pid
                   and row["owner_birth"] == envelope["owner_birth"] for row in rows)
        assert all(row["session"] == envelope["session"] for row in rows)
        assert envelope["session"] == {"session_id": "offline-initial-admission",
            "started_at": "2026-10-28T10:55:22+00:00", "close_at": "2026-10-29T00:00:00+00:00",
            "symbols": ["FIXTURE"], "original_order_id": "unchanged-order"}
    finally:
        release_and_finish(tmp_path, child)
    outcome = json.loads((tmp_path / "session/outcome.json").read_text())
    assert outcome["disposition"] == "SESSION_COMPLETE"
    assert outcome["generations"] == {"initialize": 1, "execution": 1, "forecast-0": 1, "scoring": 1}
    request, = [path for path in (tmp_path / "session").glob("controller-handoff-*.json")
                if path.name.count(".") == 1]
    handoff = json.loads(request.read_text())
    assert handoff["generations"] == {"initialize": 1}
    assert handoff["done"] == ["initialize"]


@pytest.mark.parametrize("phase", ["controller_draining", "controller_handoff", "controller_started"])
def test_stop_during_initial_controller_transfer_prevents_operating_lanes(tmp_path, phase):
    child = launch(tmp_path, stop_phase=phase)
    assert child.wait(timeout=20) == 0, (tmp_path / "controller.log").read_text()
    assert sorted(path.name for path in tmp_path.glob("entered-*.json")) == ["entered-v1-initialize.json"]
    outcome = json.loads((tmp_path / "session/outcome.json").read_text())
    assert outcome["disposition"] == "MANUAL_STOP"
    assert json.loads((tmp_path / "session/stop.json").read_text())["reason"] == "MANUAL_STOP"


def test_initial_rollback_uses_original_controller_without_phantom_execution_drain(tmp_path):
    child = launch(tmp_path, rollback=True)
    try:
        for role in ("execution", "forecast-0", "scoring"):
            wait_file(tmp_path / ("entered-v1-" + role + ".json"))
        assert child.poll() is None
        assert not list(tmp_path.glob("entered-v2-*.json"))
        assert not list((tmp_path / "session").glob("controller-handoff-*.consumed.json"))
    finally:
        release_and_finish(tmp_path, child)
    outcome = json.loads((tmp_path / "session/outcome.json").read_text())
    assert outcome["generations"] == {"initialize": 1, "execution": 1, "forecast-0": 1, "scoring": 1}


def test_stop_after_initial_worker_exit_never_activates_session(tmp_path):
    child = launch(tmp_path, stop_phase="exited")
    assert child.wait(timeout=20) == 0, (tmp_path / "controller.log").read_text()
    assert sorted(path.name for path in tmp_path.glob("entered-*.json")) == ["entered-v1-initialize.json"]
    assert not list((tmp_path / "session").glob("controller-handoff-*.json"))
    assert json.loads((tmp_path / "session/outcome.json").read_text())["disposition"] == "MANUAL_STOP"
