"""Offline fault injection for the reviewed stage-repair transaction."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil

from filelock import FileLock, Timeout
import pytest

from ml import nightly_stage_repair as repair
from ml.artifacts import file_checksum

NOW = "2026-10-09T20:00:00Z"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo, candidate, root, state_root = (tmp_path / name for name in ("repo", "candidate", "data", "state"))
    root.mkdir()
    (repo / "ml").mkdir(parents=True)
    (repo / "ml/nightly_workflow.py").write_text("original workflow\n")
    (repo / "ml/nightly_recovery.py").write_text("original recovery\n")
    (repo / "ml/nightly_gameplan.py").write_text("original model policy\n")
    shutil.copytree(repo, candidate)
    (candidate / "ml/nightly_workflow.py").write_text("fixed workflow\n")
    config = {"actor": "Scout", "repository": str(repo), "datastore": str(root), "state_root": str(state_root)}
    def source_identity(path):
        digest = hashlib.sha256(json.dumps(repair._inventory(path), sort_keys=True).encode()).hexdigest()
        return {"commit": "a" * 40, "source_sha256": digest}
    monkeypatch.setattr(repair.workflow, "source_identity", source_identity)
    monkeypatch.setattr(repair.workflow, "_verify_configuration_binding", lambda *args: None)
    monkeypatch.setattr(repair.workflow, "_verify_symbol_binding", lambda *args: None)
    monkeypatch.setattr(repair, "_pid_is_running", lambda pid: False)
    monkeypatch.setattr(repair, "_trader_running", lambda repository: False)
    output = root / "completed-stats.json"
    output.write_text("immutable previous Stats")
    recovery = root / "original-recovery.json"
    recovery.write_text('{"expires_at":"2026-10-09T19:00:00Z","original":true}')
    state = {"schema_version": repair.workflow.VERSION, "actor": "Scout", "action_date": "2026-10-09",
        "source_session": "2026-10-08", "run_id": "original-run", "status": "FAILED", "current_step": "model_review",
        "source_identity": source_identity(repo), "owner_pid": None,
        "deadline_at": "2026-10-09T11:00:00Z", "effective_deadline_at": "2026-10-09T19:00:00Z",
        "recovery": {"path": str(recovery), "sha256": file_checksum(recovery)},
        "failure": {"fingerprint": "original-fingerprint", "attempts": 3, "owner": "Scout model task"},
        "error": "original failure", "failed_at": "2026-10-09T18:00:00Z",
        "steps": {"prepare_stats": {"status": "COMPLETE", "output": {"files": {str(output): file_checksum(output)}}},
                  "model_review": {"status": "FAILED", "error": "original failure"}}}
    state_path = state_root / "runs/2026-10-09/state.json"
    write(state_path, state)
    log = tmp_path / "offline-check.log"
    log.write_text("5 passed in 0.2s (synthetic fixture)")
    request = {"action_date": "2026-10-09", "repair_id": "repair-20261009-example", "candidate": candidate,
        "changes": {"ml/nightly_workflow.py": "modify"}, "completion_record": "fixture-completion-record",
        "owner": "Scout/model-repair", "risk": "orchestration", "rationale": "Fix deterministic dispatch defect",
        "runtime_implications": "No running process changes; existing deadline remains expired",
        "checks": [{"command": ["python", "-B", "-m", "pytest", "tests/test_fixture.py", "-q"],
            "log": str(log), "exit_code": 0, "started_at": "2026-10-09T18:30:00Z",
            "completed_at": "2026-10-09T18:30:01Z", "source_files": repair._inventory(candidate)}],
        "reviewed": True, "now": NOW}
    return {"repo": repo, "candidate": candidate, "root": root, "state_path": state_path,
            "config": config, "state": state, "request": request, "log": log}


def prepare(env, **changes):
    return repair.prepare(env["config"], **{**env["request"], **changes})


def apply(env, spec):
    return repair.apply(env["config"], spec, owner=env["request"]["owner"], reviewed=True, now=NOW)


def test_expired_failed_review_repair_preserves_original_identity_outputs_and_deadline(env):
    original = env["state_path"].read_bytes()
    spec = prepare(env)
    result = apply(env, spec)
    assert result["status"] == "SOURCE_REPAIR_APPLIED"
    saved = repair.workflow._json(env["state_path"])
    assert saved["status"] == "READY" and saved["failure"]["disposition"] == "RESOLVED"
    assert saved["failure_history"] == [env["state"]["failure"]]
    assert "repair_claim" not in saved
    for name in ("run_id", "action_date", "source_session", "deadline_at", "effective_deadline_at", "recovery"):
        assert saved[name] == env["state"][name]
    assert saved["steps"]["prepare_stats"] == env["state"]["steps"]["prepare_stats"]
    assert saved["steps"]["model_review"]["retry_epoch_attempt_start"] == 0
    assert (spec.parent / "before-state.json").read_bytes() == original
    assert saved["source_repairs"][0]["original_failure"] == env["state"]["failure"]
    after = env["state_path"].read_bytes()
    assert apply(env, spec)["status"] == "SOURCE_REPAIR_ALREADY_APPLIED"
    assert env["state_path"].read_bytes() == after


@pytest.mark.parametrize("step", repair.SPLIT)
def test_each_split_responsibility_can_repair_only_its_failed_stage(env, step):
    state = env["state"]
    state["workflow_layout"] = "split"
    state["current_step"] = step
    state["steps"] = {"datastore_catchup": {"status": "FAILED"}}
    write(env["state_path"], state)
    spec = prepare(env)
    apply(env, spec)
    assert repair.workflow._json(env["state_path"])["current_step"] == step


@pytest.mark.parametrize("status", ["RUNNING", "READY", "LOCAL_COMPLETE_PEER_SETUP_PENDING", "COMPLETE"])
def test_terminal_running_and_not_failed_states_cannot_be_repaired(env, status):
    state = {**env["state"], "status": status}
    write(env["state_path"], state)
    before = env["state_path"].read_bytes()
    with pytest.raises(ValueError, match="failed or stopped"):
        prepare(env)
    assert env["state_path"].read_bytes() == before


@pytest.mark.parametrize("field", ["owner_pid", "child_pid", "worker_pid"])
def test_live_or_reused_pids_are_rejected_without_signals(env, monkeypatch, field):
    write(env["state_path"], {**env["state"], field: os.getpid()})
    monkeypatch.setattr(repair, "_pid_is_running", lambda pid: True)
    with pytest.raises(ValueError, match="Live or unverifiable"):
        prepare(env)


def native(env, *, child=None):
    run = env["root"] / "ml/overnight-runs/native-failure"
    report = {"status": "FAILED", "owner_pid": 10001, "child_pid": child}
    write(run / "stage-report.json", report)
    write(run / "receipt.json", {"status": "FAILED", "stage_report_checksum_sha256": file_checksum(run / "stage-report.json"), "logs": {}})
    state = repair.workflow._json(env["state_path"])
    state["steps"]["model_review"]["native_run"] = str(run)
    write(env["state_path"], state)
    return run


def test_native_child_must_be_dead_and_failure_receipts_unchanged(env, monkeypatch):
    run = native(env, child=10002)
    monkeypatch.setattr(repair, "_pid_is_running", lambda pid: pid == 10002)
    with pytest.raises(ValueError, match="child_pid"):
        prepare(env)
    monkeypatch.setattr(repair, "_pid_is_running", lambda pid: False)
    spec = prepare(env)
    before = (run / "receipt.json").read_bytes()
    apply(env, spec)
    assert (run / "receipt.json").read_bytes() == before


def test_native_receipt_mutation_after_review_blocks_before_install(env):
    run = native(env)
    spec = prepare(env)
    (run / "receipt.json").write_text("{}")
    with pytest.raises(ValueError, match="report and receipt"):
        apply(env, spec)
    assert (env["repo"] / "ml/nightly_workflow.py").read_text() == "original workflow\n"


@pytest.mark.parametrize("lock", ["workflow.lock", "stage-repair.lock"])
def test_held_coordinator_or_repair_lock_prevents_install(env, lock):
    with FileLock(str(Path(env["config"]["state_root"]) / lock)):
        with pytest.raises(Timeout):
            prepare(env)


def test_held_native_runtime_lock_prevents_install(env):
    with repair.exclusive_runtime_lock(env["root"] / ".ducketz-overnight-runtime.lock", process_name="fixture"):
        with pytest.raises(RuntimeError, match="Another"):
            prepare(env)


def test_one_repair_owner_and_frozen_identity_across_retries(env):
    spec = prepare(env)
    assert repair.workflow._json(env["state_path"])["repair_claim"] == {
        "owner": env["request"]["owner"], "token": env["request"]["repair_id"],
        "failure_fingerprint": "original-fingerprint"}
    assert prepare(env) == spec
    with pytest.raises(ValueError, match="Another repair owner"):
        prepare(env, owner="Atlas/competing-task", repair_id="different-repair-id")
    with pytest.raises(ValueError, match="owner/configuration"):
        repair.apply(env["config"], spec, owner="Atlas/competing-task", reviewed=True)


@pytest.mark.parametrize("damage", ["source", "candidate", "check", "state"])
def test_changed_reviewed_bytes_block_install(env, damage):
    spec = prepare(env)
    path = {"source": env["repo"] / "ml/nightly_workflow.py", "candidate": spec.parent / "candidate/ml/nightly_workflow.py",
            "check": spec.parent / "checks/0.log", "state": env["state_path"]}[damage]
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        apply(env, spec)


def test_unrelated_source_and_candidate_dependency_changes_are_rejected(env):
    (env["candidate"] / "ml/nightly_gameplan.py").write_text("unreviewed model change")
    with pytest.raises(ValueError, match="dependency"):
        prepare(env)


def test_review_policy_requires_explicit_invalidation_and_archives_completed_stages(env):
    (env["candidate"] / "ml/nightly_workflow.py").write_text("original workflow\n")
    (env["candidate"] / "ml/nightly_gameplan.py").write_text("fixed model policy\n")
    changes = {"ml/nightly_gameplan.py": "modify"}
    checks = copy.deepcopy(env["request"]["checks"])
    checks[0]["source_files"] = repair._inventory(env["candidate"])
    with pytest.raises(ValueError, match="review-binding"):
        prepare(env, changes=changes, risk="model", checks=checks)
    with pytest.raises(ValueError, match="downstream invalidation"):
        prepare(env, changes=changes, risk="model", checks=checks, review_binding="invalidated")
    state = env["state"]
    state["current_step"] = "train_and_plan"
    state["steps"]["model_review"] = {"status": "COMPLETE", "output": {"files": {}}}
    state["steps"]["train_and_plan"] = {"status": "FAILED", "native_run": None}
    write(env["state_path"], state)
    spec = prepare(env, changes=changes, risk="model", checks=checks,
                   review_binding="invalidated", invalidate_from="model_review")
    apply(env, spec)
    saved = repair.workflow._json(env["state_path"])
    assert saved["steps"]["prepare_stats"] == state["steps"]["prepare_stats"]
    assert saved["steps"]["model_review"]["status"] == "READY"
    assert "output" not in saved["steps"]["model_review"]
    assert saved["current_step"] == "model_review"
    assert "model_review" in repair.workflow._json(spec.parent / "before-state.json")["steps"]


def test_active_trader_allows_only_isolated_orchestration_entrypoints(env, monkeypatch):
    monkeypatch.setattr(repair, "_trader_running", lambda _: True)
    spec = prepare(env)
    apply(env, spec)
    # A separate fixture after completion remains terminal/READY; prove the
    # guard on the original failed bytes and another reviewed unsafe module.
    write(env["state_path"], env["state"])
    (env["repo"] / "ml/nightly_workflow.py").write_text("original workflow\n")
    (env["candidate"] / "ml/nightly_workflow.py").write_text("original workflow\n")
    (env["candidate"] / "ml/nightly_recovery.py").write_text("fixed recovery\n")
    checks = copy.deepcopy(env["request"]["checks"])
    checks[0]["source_files"] = repair._inventory(env["candidate"])
    with pytest.raises(ValueError, match="Active trader"):
        prepare(env, repair_id="second-reviewed-repair", changes={"ml/nightly_recovery.py": "modify"}, checks=checks)


@pytest.mark.parametrize("name", ["../escape.py", "/absolute.py", "tools/cross_pc/cli.py", "ml/.env", "ml\\mixed.py"])
def test_private_or_pinned_or_escaping_paths_rejected(env, name):
    with pytest.raises(ValueError):
        prepare(env, changes={name: "modify"})


@pytest.mark.parametrize("phase", ["intent.json", "before-state.json", "nightly_workflow.py", "0.log", "spec.json"])
@pytest.mark.parametrize("after", [False, True])
def test_prepare_is_resumable_at_each_frozen_write(env, monkeypatch, phase, after):
    original = repair._immutable
    tripped = False
    def interrupted(path, raw):
        nonlocal tripped
        if path.name == phase and not tripped:
            tripped = True
            if after:
                original(path, raw)
            raise OSError("simulated prepare interruption")
        return original(path, raw)
    monkeypatch.setattr(repair, "_immutable", interrupted)
    with pytest.raises(OSError, match="interruption"):
        prepare(env)
    monkeypatch.setattr(repair, "_immutable", original)
    spec = prepare(env)
    apply(env, spec)
    assert repair.workflow._json(env["state_path"])["run_id"] == "original-run"


@pytest.mark.parametrize("phase", ["transition", "install", "state", "applied"])
@pytest.mark.parametrize("after", [False, True])
def test_apply_is_resumable_before_and_after_each_commit_boundary(env, monkeypatch, phase, after):
    spec = prepare(env)
    tripped = False
    attribute, module = (("_install_file", repair) if phase == "install" else
                         ("_write", repair.workflow) if phase == "state" else ("_immutable", repair))
    original = getattr(module, attribute)
    def interrupted(path, value):
        nonlocal tripped
        selected = (phase in ("install", "state") or path.name == phase + ".json")
        if selected and not tripped:
            tripped = True
            if after:
                original(path, value)
            raise OSError("simulated apply interruption")
        return original(path, value)
    monkeypatch.setattr(module, attribute, interrupted)
    with pytest.raises(OSError, match="interruption"):
        apply(env, spec)
    monkeypatch.setattr(module, attribute, original)
    apply(env, spec)
    saved = repair.workflow._json(env["state_path"])
    assert len(saved["source_repairs"]) == 1
    assert saved["steps"]["prepare_stats"] == env["state"]["steps"]["prepare_stats"]
    assert (spec.parent / "applied.json").is_file()


def test_changed_spec_is_rejected_against_frozen_intent(env):
    spec = prepare(env)
    payload = repair.workflow._json(spec)
    payload["risk"] = "data"
    write(spec, payload)
    with pytest.raises(ValueError, match="original intent"):
        apply(env, spec)


def test_receipt_recovery_after_install_is_read_only_and_does_not_relaunch(env, monkeypatch):
    spec = prepare(env)
    apply(env, spec)
    state_before = env["state_path"].read_bytes()
    (spec.parent / "applied.json").unlink()
    monkeypatch.setattr(repair, "_install_file", lambda *args: pytest.fail("must not reinstall"))
    assert apply(env, spec)["status"] == "SOURCE_REPAIR_ALREADY_APPLIED"
    assert env["state_path"].read_bytes() == state_before


def test_same_source_external_dependency_resolution_retains_every_output(env):
    state = env["state"]
    state["failure"]["kind"] = "EXTERNAL_DEPENDENCY"
    write(env["state_path"], state)
    (env["candidate"] / "ml/nightly_workflow.py").write_text("original workflow\n")
    checks = copy.deepcopy(env["request"]["checks"])
    checks[0]["source_files"] = repair._inventory(env["candidate"])
    spec = prepare(env, changes={}, risk="external_dependency", checks=checks,
                   rationale="Existing authenticated dependency is available; offline resume fixture passed")
    result = apply(env, spec)
    saved = repair.workflow._json(env["state_path"])
    assert result["original_source_identity"] == result["reviewed_source_identity"]
    assert saved["source_identity"] == state["source_identity"]
    assert saved["steps"]["prepare_stats"] == state["steps"]["prepare_stats"]
    assert saved["failure"]["disposition"] == "RESOLVED"


def test_other_saved_state_claim_prevents_competing_repair(env):
    write(env["state_path"], {**env["state"], "repair_claim": {
        "owner": "Atlas/another-owner", "token": "unexpired-claim", "failure_fingerprint": "original-fingerprint"}})
    with pytest.raises(ValueError, match="Another repair owner"):
        prepare(env)


@pytest.mark.parametrize("after", [False, True])
def test_prepare_claim_state_write_is_resumable(env, monkeypatch, after):
    original = repair.workflow._write
    def interrupted(path, value):
        if path == env["state_path"]:
            if after:
                original(path, value)
            raise OSError("interrupted claim")
        original(path, value)
    monkeypatch.setattr(repair.workflow, "_write", interrupted)
    with pytest.raises(OSError, match="claim"):
        prepare(env)
    monkeypatch.setattr(repair.workflow, "_write", original)
    spec = prepare(env)
    apply(env, spec)
    assert len(repair.workflow._json(env["state_path"])["source_repairs"]) == 1


def test_add_delete_and_partial_multi_file_install_retain_exact_originals(env, monkeypatch):
    (env["candidate"] / "ml/nightly_workflow.py").write_text("original workflow\n")
    (env["candidate"] / "ml/nightly_recovery.py").unlink()
    (env["candidate"] / "ml/nightly_stage_repair.py").write_text("new audited helper\n")
    changes = {"ml/nightly_recovery.py": "delete", "ml/nightly_stage_repair.py": "add"}
    checks = copy.deepcopy(env["request"]["checks"])
    checks[0]["source_files"] = repair._inventory(env["candidate"])
    spec = prepare(env, changes=changes, checks=checks)
    original = repair._install_file
    calls = 0
    def interrupt_after_first(path, raw):
        nonlocal calls
        calls += 1
        original(path, raw)
        if calls == 1:
            raise OSError("first operation installed")
    monkeypatch.setattr(repair, "_install_file", interrupt_after_first)
    with pytest.raises(OSError):
        apply(env, spec)
    monkeypatch.setattr(repair, "_install_file", original)
    apply(env, spec)
    assert not (env["repo"] / "ml/nightly_recovery.py").exists()
    assert (env["repo"] / "ml/nightly_stage_repair.py").read_text() == "new audited helper\n"
    assert (spec.parent / "originals/ml/nightly_recovery.py").read_text() == "original recovery\n"


@pytest.mark.parametrize("field,value", [("exit_code", 1), ("exit_code", False),
                                         ("completed_at", "2026-10-10T20:00:00Z"), ("source_files", {})])
def test_checks_require_real_passing_exact_source_evidence(env, field, value):
    checks = copy.deepcopy(env["request"]["checks"])
    checks[0][field] = value
    with pytest.raises(ValueError):
        prepare(env, checks=checks)


def test_recovery_evidence_cannot_change_during_install(env):
    spec = prepare(env)
    Path(env["state"]["recovery"]["path"]).write_text('{"expires_at":"2026-10-10T19:00:00Z"}')
    with pytest.raises(ValueError, match="recovery evidence"):
        apply(env, spec)


def test_terminal_other_session_is_never_modified(env):
    other = env["state_path"].parent.parent / "2026-10-08/state.json"
    write(other, {**env["state"], "action_date": "2026-10-08", "status": "LOCAL_COMPLETE_PEER_SETUP_PENDING",
                  "owner_pid": os.getpid()})
    before = other.read_bytes()
    apply(env, prepare(env))
    assert other.read_bytes() == before


@pytest.mark.parametrize("latest", [False, True])
def test_unreferenced_native_child_prevents_repair_even_without_outer_binding(env, monkeypatch, latest):
    run = env["root"] / "ml/overnight-runs/unreferenced-child"
    write(run / "stage-report.json", {"status": "RUNNING", "owner_pid": 10001, "child_pid": 10002})
    if latest:
        write(env["root"] / "ml/overnight-latest/run.json", {"run_path": "ml/overnight-runs/unreferenced-child"})
    monkeypatch.setattr(repair, "_pid_is_running", lambda pid: pid == 10002)
    with pytest.raises(ValueError, match="child_pid"):
        prepare(env)


def test_prepared_repair_rechecks_new_unreferenced_child_before_install(env, monkeypatch):
    spec = prepare(env)
    run = env["root"] / "ml/overnight-runs/new-orphan"
    write(run / "stage-report.json", {"status": "RUNNING", "child_pid": 10002})
    monkeypatch.setattr(repair, "_pid_is_running", lambda pid: pid == 10002)
    with pytest.raises(ValueError, match="child_pid"):
        apply(env, spec)
    assert (env["repo"] / "ml/nightly_workflow.py").read_text() == "original workflow\n"


def test_repaired_legacy_native_planning_tail_retains_review_and_resume_identity(env, monkeypatch):
    run = native(env)
    state = repair.workflow._json(env["state_path"])
    proposal = env["root"] / "reviewed-proposal.json"
    proposal.write_text("immutable model review")
    state["current_step"] = "train_and_plan"
    state["steps"]["model_review"] = {"status": "COMPLETE", "output": {
        "proposal": str(proposal), "files": {str(proposal): file_checksum(proposal)}}}
    state["steps"]["train_and_plan"] = {"status": "FAILED", "native_run": str(run)}
    write(env["state_path"], state)
    apply(env, prepare(env))
    saved = repair.workflow._json(env["state_path"])
    import ml.overnight_runtime as runtime
    calls = []
    def resume(root, **kwargs):
        calls.append(kwargs)
        completed = root / "ml/overnight-runs/repaired-tail"
        write(completed / "stage-report.json", {"status": "COMPLETE", "enrichment_gameplan": {"run_path": "original-pinned-forecast"}})
        write(completed / "receipt.json", {"status": "COMPLETE", "stage_report_checksum_sha256": file_checksum(completed / "stage-report.json"), "logs": {}})
        return completed
    monkeypatch.setattr(runtime, "run_overnight_pipeline", resume)
    monkeypatch.setattr(runtime, "overnight_status", lambda _: {"owner_pid": -1})
    output = repair.workflow._run_native(env["config"], saved, "train_and_plan", lambda: None)
    assert calls[0]["resume_run"] == run
    assert calls[0]["model_feedback"] == proposal
    assert calls[0]["deadline"] == state["deadline_at"]
    # Both runtime variants retain the original evidence in outer state. The
    # Scout-compatible runtime additionally forwards recovery_spec to native.
    assert saved["recovery"] == state["recovery"]
    if "recovery_spec" in calls[0]:
        assert calls[0]["recovery_spec"] == Path(state["recovery"]["path"])
    assert "original-pinned-forecast" in Path(output["native_run"]).joinpath("stage-report.json").read_text()


@pytest.mark.parametrize("field", ["owner_pid", "child_pid", "worker_pid"])
def test_reused_pid_is_not_mistaken_for_the_original_live_worker(env, monkeypatch, field):
    state = {**env["state"], field: 10001, field.removesuffix("pid") + "created_at": 123.0}
    write(env["state_path"], state)
    monkeypatch.setattr(repair, "_pid_is_running", lambda _: True)
    monkeypatch.setattr(repair, "_process_created_at", lambda _: 456.0)
    apply(env, prepare(env))
    assert repair.workflow._json(env["state_path"])[field.removesuffix("pid") + "created_at"] == 123.0


def test_exact_live_native_child_birth_prevents_install(env, monkeypatch):
    run = native(env, child=10002)
    report = repair.workflow._json(run / "stage-report.json")
    report["child_created_at"] = 123.0
    write(run / "stage-report.json", report)
    write(run / "receipt.json", {"status": "FAILED", "stage_report_checksum_sha256": file_checksum(run / "stage-report.json"), "logs": {}})
    monkeypatch.setattr(repair, "_pid_is_running", lambda pid: pid == 10002)
    monkeypatch.setattr(repair, "_process_created_at", lambda _: 123.0)
    with pytest.raises(ValueError, match="child_pid"):
        prepare(env)


def test_safe_orchestration_or_same_source_resolution_needs_no_trader_process_access(env, monkeypatch):
    monkeypatch.setattr(repair, "_trader_running", lambda _: pytest.fail("safe entrypoints do not inspect unrelated Python"))
    apply(env, prepare(env))


def test_repaired_failure_gets_new_bounded_epoch_without_erasing_attempt_history(env):
    state = env["state"]
    state["steps"]["model_review"].update(attempts=7, retry_epoch_attempt_start=4, retry_epoch_id="previous-repair")
    write(env["state_path"], state)
    spec = prepare(env)
    result = apply(env, spec)
    entry = repair.workflow._json(env["state_path"])["steps"]["model_review"]
    assert entry["attempts"] == 7
    assert entry["retry_epoch_attempt_start"] == 7
    assert entry["retry_epoch_id"] == file_checksum(spec)
    assert entry["attempts"] - entry["retry_epoch_attempt_start"] == 0
    assert result["prior_retry_epochs"]["model_review"] == {
        "attempts": 7, "retry_epoch_attempt_start": 4, "retry_epoch_id": "previous-repair"}


@pytest.mark.parametrize("attempts,start", [(3, 4), (True, 0), (3, -1), (3, 1.5)])
def test_malformed_retry_counters_cannot_create_a_fresh_repair_epoch(env, attempts, start):
    state = env["state"]
    state["steps"]["model_review"].update(attempts=attempts, retry_epoch_attempt_start=start)
    write(env["state_path"], state)
    with pytest.raises(ValueError, match="epoch counters"):
        prepare(env)
