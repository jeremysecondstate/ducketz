import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from ml.artifacts import utc_timestamp
from ml.nightly_cancellation import (
    CANCELLATION_AUTHORITY, CANCELLATION_VERSION, ExplicitNightlyCancellation,
    read_cancellation_request, validate_cancellation,
)
from ml.overnight_runtime import _run_stage
from tests.test_gameplan_deployment_handoff import handoff


def human_request(run):
    return {
        "schema_version": CANCELLATION_VERSION, "authority": CANCELLATION_AUTHORITY,
        "requested_by": "Jeremy", "explicit_human_request": True, "action": "cancel",
        "human_instruction": "Cancel tonight's nightly operations.",
        "human_request_ref": "test://jeremy/direct-cancellation",
        "requested_at": utc_timestamp().isoformat(),
        "run_id": run.name, "run_path": str(run.resolve()),
    }


@pytest.mark.parametrize("record", [None, {}, {"status": "CANCELLED"},
    {"reason": "supervisor requested stop", "requested_at": "2026-10-14T03:00:00Z"}])
def test_software_records_and_statuses_have_no_authority(record):
    assert validate_cancellation(record) is False
    with pytest.raises(ValueError, match="Jeremy's explicit instruction"):
        ExplicitNightlyCancellation(record)


@pytest.mark.parametrize("field,replacement", [
    ("schema_version", "legacy"), ("authority", "supervisor"), ("requested_by", "Codex"),
    ("explicit_human_request", False), ("explicit_human_request", 1), ("action", "pause"),
    ("human_instruction", ""), ("human_request_ref", " "), ("requested_at", "2026-10-14"),
    ("run_id", "other-run"), ("run_path", "relative/run"),
])
def test_missing_or_automatic_human_evidence_is_inert(tmp_path, field, replacement):
    record = human_request(tmp_path)
    record[field] = replacement
    assert validate_cancellation(record) is False


def test_human_request_is_bound_to_exact_local_run(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    path = tmp_path / "stop-request.json"
    record = human_request(tmp_path)
    path.write_text(json.dumps(record))
    assert validate_cancellation(record, run_id=tmp_path.name)
    assert not validate_cancellation(record, run_id=other.name)
    assert read_cancellation_request(path, run_path=tmp_path) == record
    assert read_cancellation_request(path, run_path=other) is None
    with pytest.raises(ExplicitNightlyCancellation) as raised:
        raise ExplicitNightlyCancellation(record)
    assert raised.value.request == record


@pytest.mark.parametrize("contents", ["", "stop", "{broken", '{"status": "CANCELLED"}',
    '{"reason": "supervisor stop request"}'])
def test_bare_and_legacy_stop_files_do_not_block_launch(tmp_path, contents):
    request = tmp_path / "stop-request.json"
    request.write_text(contents)
    assert read_cancellation_request(request) is None
    result = _run_stage([sys.executable, "-c", "print('completed')"], repository=tmp_path,
        log_path=tmp_path / "child.log", deadline=utc_timestamp() + pd.Timedelta(minutes=1),
        stop_request=request, progress=lambda _: None, poll_seconds=0.03)
    assert result == 0
    assert "completed" in (tmp_path / "child.log").read_text()
    assert request.read_text() == contents


def test_running_child_survives_automatic_stop_request(tmp_path):
    request = tmp_path / "stop-request.json"
    wrote = False
    def progress(_):
        nonlocal wrote
        if not wrote:
            request.write_text(json.dumps({"reason": "supervisor cancelled after a model interruption"}))
            wrote = True
    result = _run_stage([sys.executable, "-u", "-c", "import time; time.sleep(0.25); print('completed')"],
        repository=tmp_path, log_path=tmp_path / "child.log",
        deadline=utc_timestamp() + pd.Timedelta(minutes=1), stop_request=request,
        progress=progress, poll_seconds=0.03)
    assert wrote and result == 0
    assert "completed" in (tmp_path / "child.log").read_text()


def test_explicit_human_request_before_launch_stops_bound_run(tmp_path, monkeypatch):
    request = tmp_path / "stop-request.json"
    request.write_text(json.dumps(human_request(tmp_path)))
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("human-cancelled run cannot launch"))
    with pytest.raises(ExplicitNightlyCancellation):
        _run_stage([sys.executable, "-c", "pass"], repository=tmp_path,
            log_path=tmp_path / "child.log", deadline=utc_timestamp() + pd.Timedelta(minutes=1),
            stop_request=request, progress=lambda _: None, poll_seconds=0.03)


def test_explicit_human_request_stops_only_owned_running_child(tmp_path):
    import psutil
    request = tmp_path / "stop-request.json"
    child_pid = None
    def progress(payload):
        nonlocal child_pid
        child_pid = payload["child_pid"]
        request.write_text(json.dumps(human_request(tmp_path)))
    with pytest.raises(ExplicitNightlyCancellation):
        _run_stage([sys.executable, "-c", "import time; time.sleep(30)"], repository=tmp_path,
            log_path=tmp_path / "child.log", deadline=utc_timestamp() + pd.Timedelta(minutes=1),
            stop_request=request, progress=progress, poll_seconds=0.03)
    assert child_pid is not None
    assert not psutil.pid_exists(child_pid)


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
