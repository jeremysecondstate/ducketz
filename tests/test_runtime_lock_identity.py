"""PID reuse recovery never signals processes or reclaims uncertain owners."""
import json
import os
from pathlib import Path

import pytest

from datafetching import runtime_lock as locks
from ml import nightly_workflow as workflow, overnight_runtime as native
from ml.artifacts import file_checksum
from test_nightly_workflow import setup, _identity


def existing(tmp_path, *, birth="100.0", started="2026-10-08T21:05:00Z"):
    path = tmp_path / ".writer.lock"
    record = "process=original\npid=424242\n"
    if birth is not None:
        record += f"owner_created_at={birth}\n"
    if started is not None:
        record += f"started_at={started}\n"
    path.write_text(record + "token=original\n", encoding="utf-8")
    return path


def test_new_lock_records_actual_process_birth(tmp_path):
    import psutil
    path = tmp_path / ".writer.lock"
    with locks.exclusive_runtime_lock(path, process_name="fixture"):
        saved = dict(line.split("=", 1) for line in path.read_text().splitlines())
        assert float(saved["owner_created_at"]) == psutil.Process(os.getpid()).create_time()
        assert int(saved["pid"]) == os.getpid()
    assert not path.exists()


def test_proven_reused_birth_reclaims_lock_without_signaling(tmp_path, monkeypatch):
    path = existing(tmp_path)
    monkeypatch.setattr(locks, "_pid_is_running", lambda pid: True)
    monkeypatch.setattr(locks, "_process_created_at", lambda pid: 200.0)
    monkeypatch.setattr(locks.os, "kill", lambda *args: pytest.fail("No process signals"))
    with locks.exclusive_runtime_lock(path, process_name="restarted"):
        assert "token=original" not in path.read_text()
    assert not path.exists()


@pytest.mark.parametrize("birth,current", [("100.0", 100.0), ("unknown", 200.0),
    ("nan", 200.0), ("-1", 200.0), ("", 200.0), ("100.0", None)])
def test_live_and_unverifiable_births_are_preserved(tmp_path, monkeypatch, birth, current):
    path = existing(tmp_path, birth=birth)
    before = path.read_bytes()
    monkeypatch.setattr(locks, "_pid_is_running", lambda pid: True)
    monkeypatch.setattr(locks, "_process_created_at", lambda pid: current)
    with pytest.raises(RuntimeError, match="owns these artifacts"):
        with locks.exclusive_runtime_lock(path, process_name="fixture"):
            pytest.fail("No ownership proof")
    assert path.read_bytes() == before


@pytest.mark.parametrize("started,current,can_reclaim", [
    ("2020-01-01T00:00:00Z", 1760000000.0, True),
    ("2030-01-01T00:00:00+00:00", 1760000000.0, False),
    ("2020-01-01T00:00:00", 1760000000.0, False),
    ("unverifiable", 1760000000.0, False), (None, 1760000000.0, False),
    ("2020-01-01T00:00:00Z", None, False),
])
def test_legacy_timestamp_reclaims_only_process_born_after_lock(tmp_path, monkeypatch, started, current, can_reclaim):
    path = existing(tmp_path, birth=None, started=started)
    before = path.read_bytes()
    monkeypatch.setattr(locks, "_pid_is_running", lambda pid: True)
    monkeypatch.setattr(locks, "_process_created_at", lambda pid: current)
    if can_reclaim:
        with locks.exclusive_runtime_lock(path, process_name="fixture"):
            assert path.read_bytes() != before
    else:
        with pytest.raises(RuntimeError, match="owns these artifacts"):
            with locks.exclusive_runtime_lock(path, process_name="fixture"):
                pytest.fail("Legacy evidence cannot establish PID reuse")
        assert path.read_bytes() == before


def test_process_birth_access_denied_is_unknown(monkeypatch):
    import psutil
    class Denied:
        def __init__(self, pid):
            raise psutil.AccessDenied(pid)
    monkeypatch.setattr(psutil, "Process", Denied)
    assert locks._process_created_at(424242) is None


def test_changed_payload_during_verification_is_not_removed(tmp_path, monkeypatch):
    path = existing(tmp_path)
    replacement = "process=new-owner\npid=434343\nowner_created_at=200\ntoken=new\n"
    monkeypatch.setattr(locks, "_pid_is_running", lambda pid: True)
    def observe(pid):
        if pid == 424242:
            path.write_text(replacement)
        return 200.0
    monkeypatch.setattr(locks, "_process_created_at", observe)
    with pytest.raises(RuntimeError, match="changed during owner verification"):
        with locks.exclusive_runtime_lock(path, process_name="fixture"):
            pytest.fail("Changed lock must stay intact")
    assert path.read_text() == replacement


@pytest.mark.parametrize("payload", ["", "process=partial\npid=", "pid=424242\npid=434343\n"])
def test_preexisting_partial_or_ambiguous_locks_are_preserved(tmp_path, payload):
    path = tmp_path / ".writer.lock"
    path.write_text(payload)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="owns these artifacts"):
        with locks.exclusive_runtime_lock(path, process_name="fixture"):
            pytest.fail("Unknown existing ownership must remain intact")
    assert path.read_bytes() == before


@pytest.mark.parametrize("phase", ["fsync", "before_link", "after_link"])
def test_interrupted_lock_publication_never_leaves_a_partial_blocking_target(tmp_path, monkeypatch, phase):
    path = tmp_path / ".writer.lock"
    original_fsync, original_link = locks.os.fsync, locks.os.link
    def fail_fsync(*args):
        raise OSError("injected failure before publication")
    def fail_link(source, target):
        if phase == "after_link":
            original_link(source, target)
        raise OSError("injected link outcome")
    if phase == "fsync":
        monkeypatch.setattr(locks.os, "fsync", fail_fsync)
    else:
        monkeypatch.setattr(locks.os, "link", fail_link)
    with pytest.raises(OSError, match="injected"):
        with locks.exclusive_runtime_lock(path, process_name="fixture"):
            pytest.fail("Interrupted acquisition cannot enter")
    assert not path.exists()
    assert not list(tmp_path.glob(".writer.lock.pending-*"))
    monkeypatch.setattr(locks.os, "fsync", original_fsync)
    monkeypatch.setattr(locks.os, "link", original_link)
    with locks.exclusive_runtime_lock(path, process_name="fixture retry"):
        assert locks._lock_pid(path.read_text()) == os.getpid()


def test_nightly_resume_after_native_birth_recovery_reclaims_reused_legacy_lock(setup, monkeypatch):
    root = Path(setup["datastore"])
    original_run = root / "ml/overnight-runs/interrupted-original"
    original_run.mkdir(parents=True)
    proposal = root / "reviewed-proposal.json"
    proposal.write_text("immutable fixture review")
    def original_stage(config, state, step, save):
        if step == "train_and_plan":
            state["steps"][step]["native_run"] = str(original_run)
            raise InterruptedError("Original supervisor interrupted")
        return {"files": {}, "proposal": str(proposal)}
    with pytest.raises(InterruptedError):
        workflow.run_workflow(setup, now="2026-10-06T04:05Z", identity=_identity,
            execute_step=original_stage, supervise=False)
    report = {"status": "RUNNING", "owner_pid": 424242, "owner_created_at": 1600000000.0,
        "stage_order": ["gameplan_trade_planning"], "stages": [], "deadline_at": "2026-10-06T11:00Z"}
    (original_run / "stage-report.json").write_text(json.dumps(report))
    lock = root / ".ducketz-overnight-runtime.lock"
    lock.write_text("process=old-supervisor\npid=424242\nstarted_at=2020-01-01T00:00:00Z\ntoken=old\n")
    monkeypatch.setattr(native, "_process_created_at", lambda pid: 1760000000.0)
    monkeypatch.setattr(locks, "_pid_is_running", lambda pid: pid == 424242)
    monkeypatch.setattr(locks, "_process_created_at", lambda pid: 1760000000.0)
    native.recover_interrupted_run(root, original_run, "Verified original supervisor exited, PID reused")
    before = (original_run / "receipt.json").read_bytes()
    calls = []
    def resume_native(datastore, **arguments):
        calls.append(arguments)
        completed = root / "ml/overnight-runs/resumed"
        completed.mkdir()
        (completed / "stage-report.json").write_text(json.dumps({"status": "COMPLETE"}))
        (completed / "receipt.json").write_text(json.dumps({"status": "COMPLETE", "logs": {},
            "stage_report_checksum_sha256": file_checksum(completed / "stage-report.json")}))
        return completed
    monkeypatch.setattr(native, "run_overnight_pipeline", resume_native)
    monkeypatch.setattr(native, "overnight_status", lambda root: {})
    def remaining(config, state, step, save):
        if step == "train_and_plan":
            return workflow._run_native(config, state, step, save)
        return {"files": {}}
    result = workflow.run_workflow(setup, resume_action_date="2026-10-06", now="2026-10-06T04:10Z",
        identity=_identity, execute_step=remaining, supervise=False)
    assert result["status"] == "LOCAL_COMPLETE_PEER_SETUP_PENDING"
    assert len(calls) == 1 and calls[0]["resume_run"] == original_run
    assert calls[0]["model_feedback"] == proposal
    assert calls[0]["deadline"] == "2026-10-06T11:00:00+00:00"
    assert (original_run / "receipt.json").read_bytes() == before
    assert not lock.exists()
