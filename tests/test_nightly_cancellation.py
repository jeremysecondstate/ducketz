"""Persisted assertions never replace a direct human stop of the owned run."""
import json
import subprocess
import sys

import pandas as pd
import pytest

from ml.artifacts import utc_timestamp
from ml.overnight_runtime import HumanRequestedStop, _run_stage
from tests.test_gameplan_deployment_handoff import handoff


def asserted_human_record(run):
    """Reproduce the retired schema without treating any field as authority."""
    return {
        "schema_version": "jeremy-explicit-nightly-cancellation-v1",
        "authority": "jeremy-explicit-nightly-cancellation",
        "requested_by": "Jeremy", "explicit_human_request": True, "action": "cancel",
        "human_instruction": "Cancel tonight's nightly operations.",
        "human_request_ref": "test://software-asserted-human-cancellation",
        "requested_at": utc_timestamp().isoformat(),
        "run_id": run.name, "run_path": str(run.resolve()),
    }


@pytest.mark.parametrize("contents", [
    "", "stop", "{broken", '{"status": "CANCELLED"}',
    '{"reason": "supervisor stop request"}', "asserted-human-record",
])
def test_persisted_stop_records_do_not_block_launch(tmp_path, contents):
    request = tmp_path / "stop-request.json"
    raw = (json.dumps(asserted_human_record(tmp_path))
           if contents == "asserted-human-record" else contents).encode("utf-8")
    request.write_bytes(raw)
    result = _run_stage([sys.executable, "-c", "print('completed')"], repository=tmp_path,
        log_path=tmp_path / "child.log", deadline=utc_timestamp() + pd.Timedelta(minutes=1),
        stop_request=request, progress=lambda _: None, poll_seconds=0.03)
    assert result == 0
    assert "completed" in (tmp_path / "child.log").read_text()
    assert request.read_bytes() == raw


@pytest.mark.parametrize("asserts_human", [False, True])
def test_running_child_survives_persisted_stop_record(tmp_path, asserts_human):
    request = tmp_path / "stop-request.json"
    raw = json.dumps(asserted_human_record(tmp_path) if asserts_human else
        {"reason": "supervisor cancelled after a model interruption"}).encode("utf-8")
    wrote = False
    def progress(_):
        nonlocal wrote
        if not wrote:
            request.write_bytes(raw)
            wrote = True
    result = _run_stage([sys.executable, "-u", "-c",
        "import time; time.sleep(0.25); print('completed')"], repository=tmp_path,
        log_path=tmp_path / "child.log", deadline=utc_timestamp() + pd.Timedelta(minutes=1),
        stop_request=request, progress=progress, poll_seconds=0.03)
    assert wrote and result == 0
    assert "completed" in (tmp_path / "child.log").read_text()
    assert request.read_bytes() == raw


def test_immediate_human_stop_terminates_only_owned_child(tmp_path, monkeypatch):
    popen = subprocess.Popen
    owned = []
    def record_owned(*args, **kwargs):
        process = popen(*args, **kwargs)
        if kwargs.get("cwd") == tmp_path:
            owned.append(process)
        return process
    unrelated = popen([sys.executable, "-c", "import time; time.sleep(10)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    monkeypatch.setattr("ml.overnight_runtime.subprocess.Popen", record_owned)
    def direct_human_action(payload):
        assert payload["child_pid"] == owned[0].pid
        raise HumanRequestedStop("fixture immediate direct human action")
    try:
        with pytest.raises(HumanRequestedStop, match="Explicit human stop"):
            _run_stage([sys.executable, "-c", "import time; time.sleep(10)"],
                repository=tmp_path, log_path=tmp_path / "child.log",
                deadline=utc_timestamp() + pd.Timedelta(minutes=1),
                stop_request=tmp_path / "stop-request.json",
                progress=direct_human_action, poll_seconds=0.03)
        assert len(owned) == 1 and owned[0].poll() is not None
        assert unrelated.poll() is None
        assert not (tmp_path / "stop-request.json").exists()
    finally:
        for process in [*owned, unrelated]:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)


def test_interrupted_native_ancestor_remains_usable_after_completed_resume(handoff):
    from tests.test_gameplan_deployment_handoff import activate, resumed_tail, DAY
    from ml import gameplan_deployment as deployment
    original = resumed_tail(handoff)
    handoff["report"]["status"] = "INTERRUPTED"
    handoff["bind_native"]()
    before = {path.name: path.read_bytes() for path in original.iterdir() if path.is_file()}
    activate(handoff)
    assert deployment.read_deployment(handoff["root"], DAY)["status"] == "ACTIVE"
    assert {path.name: path.read_bytes() for path in original.iterdir() if path.is_file()} == before
