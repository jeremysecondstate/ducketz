"""Nightly children and immutable retry evidence survive software interruptions."""
import json
import subprocess
import sys

import pandas as pd
import pytest

from tests.test_gameplan_deployment_handoff import handoff

from ml import overnight_runtime as runtime
from ml.artifacts import file_checksum, utc_timestamp
from ml.overnight_runtime import HumanRequestedStop, _run_stage


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


def test_telemetry_error_waits_for_useful_child_without_termination(tmp_path, monkeypatch):
    marker = tmp_path / "completed.txt"
    command = [sys.executable, "-c", "import pathlib,time; time.sleep(.15); pathlib.Path('completed.txt').write_text('complete')"]
    monkeypatch.setattr(runtime, "_terminate_owned_process", lambda _: pytest.fail("No human stop"))
    def telemetry(_):
        raise OSError("fixture failed telemetry write")
    with pytest.raises(OSError, match="telemetry"):
        runtime._run_stage(command, repository=tmp_path, log_path=tmp_path / "child.log",
            deadline=utc_timestamp()-pd.Timedelta(days=1), stop_request=tmp_path / "stop.json",
            progress=telemetry, poll_seconds=.02)
    assert marker.read_text() == "complete"


def test_asserted_stop_cannot_interrupt_child_during_failed_telemetry(tmp_path):
    stop = tmp_path / "stop.json"
    def telemetry(_):
        stop.write_text(json.dumps({"human_authorized": True,
            "authorization_source": "fixture explicit user stop", "reason": "stop now"}))
        raise OSError("telemetry unavailable")
    with pytest.raises(OSError, match="telemetry"):
        runtime._run_stage([sys.executable, "-c", "import pathlib,time; time.sleep(.15); pathlib.Path('completed.txt').write_text('complete')"],
            repository=tmp_path, log_path=tmp_path / "child.log", deadline=utc_timestamp(),
            stop_request=stop, progress=telemetry, poll_seconds=.02)
    assert (tmp_path / "completed.txt").read_text() == "complete"
    assert stop.is_file()


def evidence_snapshot(run):
    return {path.name: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in run.iterdir() if path.is_file()}


@pytest.mark.parametrize("error_type, expected_status", [
    (InterruptedError, "INTERRUPTED"), (TimeoutError, "TIMED_OUT"),
])
@pytest.mark.parametrize("resume_after_deadline", [False, True])
def test_producer_interruption_receipt_recovers_and_resumes_same_work(
        tmp_path, monkeypatch, error_type, expected_status, resume_after_deadline):
    observed = pd.Timestamp("2026-10-14T07:00:00Z")
    deadline = observed + pd.Timedelta(hours=1)
    monkeypatch.setattr(runtime, "utc_timestamp",
                        lambda value=None: pd.Timestamp(value) if value is not None else observed)
    calls = []
    def interrupted(command, **kwargs):
        calls.append(command[3])
        kwargs["log_path"].write_text("preserved producer evidence")
        if len(calls) == 2:
            raise error_type("fixture producer interruption")
        return 0
    monkeypatch.setattr(runtime, "_run_stage", interrupted)
    with pytest.raises(error_type, match="fixture producer interruption"):
        runtime.run_overnight_pipeline(tmp_path, datastore_argument=("--datastore", str(tmp_path)),
            repository_root=tmp_path, reporter=None, deadline=deadline,
            stop_after=runtime.STAGE_ORDER[2])
    state = runtime.overnight_status(tmp_path)
    failed = tmp_path / "ml" / "overnight-runs" / pd.Timestamp(state["run_timestamp"]).strftime("%Y%m%dT%H%M%S.%fZ")
    report = json.loads((failed / "stage-report.json").read_text())
    receipt = json.loads((failed / "receipt.json").read_text())
    assert report["status"] == receipt["status"] == expected_status
    assert report["stages"][0]["status"] == "COMPLETE"
    assert report["stages"][1]["status"] == expected_status
    assert report["failed_stage"] == runtime.STAGE_ORDER[1]
    assert receipt["stage_report_checksum_sha256"] == file_checksum(failed / "stage-report.json")
    for name, record in receipt["logs"].items():
        assert record["checksum_sha256"] == file_checksum(failed / name)
    (failed / "stop-request.json").write_text(json.dumps(asserted_human_record(failed)))
    before = evidence_snapshot(failed)
    observed = deadline + pd.Timedelta(days=2) if resume_after_deadline else observed + pd.Timedelta(minutes=1)
    monkeypatch.setattr(runtime, "_process_created_at", lambda _: None)
    assert runtime.recover_interrupted_run(tmp_path, failed, "Fixture producer exited") == failed
    assert runtime.recover_interrupted_run(tmp_path, failed, "Same immutable recovery") == failed
    assert evidence_snapshot(failed) == before
    resumed_calls = []
    def complete(command, **kwargs):
        resumed_calls.append(command[3])
        assert kwargs["deadline"] == deadline
        kwargs["log_path"].write_text("preserved resumed completion")
        return 0
    monkeypatch.setattr(runtime, "_run_stage", complete)
    resumed = runtime.run_overnight_pipeline(tmp_path, datastore_argument=("--datastore", str(tmp_path)),
        repository_root=tmp_path, reporter=None, resume_run=failed)
    result = json.loads((resumed / "stage-report.json").read_text())
    resumed_receipt = json.loads((resumed / "receipt.json").read_text())
    assert result["status"] == resumed_receipt["status"] == "COMPLETE"
    assert result["resumed_from"] == str(failed.resolve())
    assert result["deadline_at"] == report["deadline_at"]
    assert result["stage_order"] == list(runtime.STAGE_ORDER[1:3])
    assert result["completed_stages_from_previous_attempt"] == [runtime.STAGE_ORDER[0]]
    assert resumed_calls == ["ml.prediction_runtime", "ml.gameplan_evaluation"]
    assert resumed_receipt["stage_report_checksum_sha256"] == file_checksum(resumed / "stage-report.json")
    assert evidence_snapshot(failed) == before


@pytest.mark.parametrize("authorization", [{}, {"human_authorized": True},
    {"authorization_source": "A software assertion is not a human instruction"}])
def test_software_stop_note_cannot_supply_missing_human_authority(tmp_path, authorization):
    with pytest.raises(PermissionError, match="explicit stop instruction"):
        runtime.request_stage_stop(tmp_path, tmp_path, "Software requested cancellation", **authorization)
    assert not (tmp_path / "stop-request.json").exists()


@pytest.mark.parametrize("ancestor_status", ["INTERRUPTED", "TIMED_OUT"])
def test_interrupted_native_ancestor_remains_usable_after_completed_resume(handoff, ancestor_status):
    from tests.test_gameplan_deployment_handoff import activate, resumed_tail, DAY
    from ml import gameplan_deployment as deployment
    original = resumed_tail(handoff)
    handoff["report"]["status"] = ancestor_status
    handoff["bind_native"]()
    before = {path.name: path.read_bytes() for path in original.iterdir() if path.is_file()}
    activate(handoff)
    assert deployment.read_deployment(handoff["root"], DAY)["status"] == "ACTIVE"
    assert {path.name: path.read_bytes() for path in original.iterdir() if path.is_file()} == before
