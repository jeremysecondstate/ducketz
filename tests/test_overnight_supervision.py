import json
import sys

import pandas as pd
import pytest

from ml.artifacts import file_checksum, utc_timestamp
from ml.overnight_runtime import (
    STAGE_ORDER, _run_stage, next_action_deadline, overnight_status,
    claim_supervision, recover_interrupted_run, request_stage_stop, run_overnight_pipeline,
)


def run(root, **kwargs):
    return run_overnight_pipeline(root, datastore_argument=("--datastore", str(root)),
                                  repository_root=root, reporter=None, **kwargs)


def test_weekend_and_holiday_preserve_long_training_window():
    assert next_action_deadline("2026-09-05T00:05:00Z") == pd.Timestamp("2026-09-08T11:00:00Z")
    assert next_action_deadline("2026-09-09T00:05:00Z") == pd.Timestamp("2026-09-09T11:00:00Z")
    assert next_action_deadline("2026-09-09T09:00:00Z") == pd.Timestamp("2026-09-09T11:00:00Z")


def test_failed_stage_has_receipt_and_resume_skips_completed_stages(tmp_path, monkeypatch):
    calls = []
    def fail_training(command, **kwargs):
        calls.append(command[3])
        kwargs["log_path"].write_text("test output")
        return 7 if command[3] == "ml.strategy_profit_training_runtime" else 0
    monkeypatch.setattr("ml.overnight_runtime._run_stage", fail_training)
    with pytest.raises(RuntimeError, match="exited with code 7"):
        run(tmp_path)
    state = overnight_status(tmp_path)
    failed = tmp_path / "ml/overnight-runs" / pd.Timestamp(state["run_timestamp"]).strftime("%Y%m%dT%H%M%S.%fZ")
    receipt = json.loads((failed / "receipt.json").read_text())
    assert state["status"] == receipt["status"] == "FAILED"
    assert state["failed_stage"] == "strategy_profit_training"
    assert len(calls) == 4
    assert receipt["stage_report_checksum_sha256"] == file_checksum(failed / "stage-report.json")
    assert receipt["logs"]["strategy_profit_training.log"]["checksum_sha256"] == file_checksum(failed / "strategy_profit_training.log")
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda command, **kw: calls.append(command[3]) or 0)
    resumed = run(tmp_path, resume_run=failed)
    report = json.loads((resumed / "stage-report.json").read_text())
    assert report["status"] == "COMPLETE"
    assert report["stage_order"] == list(STAGE_ORDER[3:])
    assert report["completed_stages_from_previous_attempt"] == list(STAGE_ORDER[:3])
    assert report["deadline_at"] == state["deadline_at"]
    assert len(calls) == 7
    with pytest.raises(RuntimeError, match="verified failed or stopped"):
        run(tmp_path, resume_run=resumed)


def test_launch_exception_still_writes_terminal_receipt(tmp_path, monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("test missing executable")
    monkeypatch.setattr("ml.overnight_runtime.subprocess.Popen", missing)
    with pytest.raises(FileNotFoundError):
        run(tmp_path)
    state = overnight_status(tmp_path)
    assert state["status"] == "FAILED"
    assert state["receipt_present"]


def test_live_output_reports_error_before_process_finishes(tmp_path):
    updates = []
    code = "import time; print('RuntimeError: test early diagnostic', flush=True); time.sleep(0.3); print('finished', flush=True)"
    result = _run_stage([sys.executable, "-u", "-c", code], repository=tmp_path,
        log_path=tmp_path / "child.log", deadline=utc_timestamp() + pd.Timedelta(minutes=1),
        stop_request=tmp_path / "stop.json", progress=lambda p: updates.append(dict(p)), poll_seconds=0.03)
    assert result == 0
    alerted = [p for p in updates if p["stage_health"] == "ATTENTION_REQUIRED"]
    assert len(alerted) >= 2
    assert any("test early diagnostic" in line for line in alerted[0]["recent_issues"])
    assert "finished" not in str(alerted[0]["recent_output"])


def test_deadline_stops_only_owned_child_and_writes_receipt(tmp_path, monkeypatch):
    original = _run_stage
    def slow(_command, **kwargs):
        return original([sys.executable, "-u", "-c", "import time; time.sleep(30)"], **kwargs)
    monkeypatch.setattr("ml.overnight_runtime._run_stage", slow)
    with pytest.raises(TimeoutError):
        run(tmp_path, deadline=utc_timestamp() + pd.Timedelta(seconds=0.5), poll_seconds=0.03)
    state = overnight_status(tmp_path)
    assert state["status"] == "TIMED_OUT"
    assert state["receipt_present"]


def test_controlled_stop_is_receipted_and_resumable(tmp_path, monkeypatch):
    original = _run_stage
    def stop(_command, **kwargs):
        request_stage_stop(tmp_path, kwargs["log_path"].parent, "test verified stalled stage")
        return original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
    monkeypatch.setattr("ml.overnight_runtime._run_stage", stop)
    with pytest.raises(InterruptedError):
        run(tmp_path, poll_seconds=0.03)
    state = overnight_status(tmp_path)
    assert state["status"] == "CANCELLED"
    assert state["receipt_present"]


def test_recovery_refuses_to_interrupt_a_living_owner(tmp_path, monkeypatch):
    def recover(_command, **kwargs):
        with pytest.raises(RuntimeError, match="owner is still alive"):
            recover_interrupted_run(tmp_path, kwargs["log_path"].parent, "test")
        return 0
    monkeypatch.setattr("ml.overnight_runtime._run_stage", recover)
    assert run(tmp_path).is_dir()


def test_dead_owner_recovery_does_not_replay_completed_stage(tmp_path, monkeypatch):
    def failed(_command, **kwargs):
        return 9
    monkeypatch.setattr("ml.overnight_runtime._run_stage", failed)
    with pytest.raises(RuntimeError):
        run(tmp_path)
    state = overnight_status(tmp_path)
    from pathlib import Path
    run_path = Path(state["run_path"])
    report_path = run_path / "stage-report.json"
    report = json.loads(report_path.read_text())
    # Simulate a parent exiting between completed first stage and the next launch.
    report["status"] = "RUNNING"
    report["stages"][0]["status"] = "COMPLETE"
    report["owner_pid"] = 99999999
    report_path.write_text(json.dumps(report))
    (run_path / "receipt.json").unlink()
    recover_interrupted_run(tmp_path, run_path, "verified supervisor exited")
    result = json.loads(report_path.read_text())
    assert result["failed_stage"] == STAGE_ORDER[1]
    assert result["status"] == "CANCELLED"


def test_recovery_refuses_reused_child_pid(tmp_path, monkeypatch):
    monkeypatch.setattr("ml.overnight_runtime._run_stage", lambda *a, **kw: 9)
    with pytest.raises(RuntimeError):
        run(tmp_path)
    from pathlib import Path
    import os
    state = overnight_status(tmp_path)
    run_path = Path(state["run_path"])
    report_path = run_path / "stage-report.json"
    report = json.loads(report_path.read_text())
    report.update(status="RUNNING", owner_pid=99999999, child_pid=os.getpid(), child_created_at=0)
    report_path.write_text(json.dumps(report))
    (run_path / "receipt.json").unlink()
    with pytest.raises(RuntimeError, match="PID was reused"):
        recover_interrupted_run(tmp_path, run_path, "test")


def test_only_one_scheduled_operator_can_claim_or_renew_supervision(tmp_path):
    import uuid
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    assert claim_supervision(tmp_path, first, observed_at="2026-09-05T01:00Z")["status"] == "ACQUIRED"
    assert claim_supervision(tmp_path, second, observed_at="2026-09-05T01:01Z")["status"] == "BUSY"
    assert claim_supervision(tmp_path, first, observed_at="2026-09-05T01:02Z")["status"] == "ACQUIRED"
    assert claim_supervision(tmp_path, second, observed_at="2026-09-05T01:04Z")["status"] == "BUSY"
    assert claim_supervision(tmp_path, second, observed_at="2026-09-05T01:05Z")["status"] == "ACQUIRED"
    assert claim_supervision(tmp_path, first, release=True, observed_at="2026-09-05T01:06Z")["status"] == "BUSY"
    assert claim_supervision(tmp_path, second, release=True, observed_at="2026-09-05T01:06Z")["status"] == "RELEASED"
