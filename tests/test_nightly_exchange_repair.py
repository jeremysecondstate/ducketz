"""Private offline repair fixtures; no exchange providers or trader processes."""
import json
from pathlib import Path
import shutil

import pytest
from filelock import FileLock, Timeout

from ml import nightly_exchange_repair as repair
from ml import nightly_repair_registry as registry
from ml.artifacts import file_checksum

DAY = "2026-10-09"
NOW = "2026-10-09T21:00:00Z"


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(repair._bytes(value))
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo, candidate, data, work, exchange = (tmp_path / name for name in ("repo", "candidate", "data", "work", "exchange"))
    for path in (repo, data, work, exchange):
        path.mkdir()
    for name in repair.ALLOWED | repair.PLANNING_READERS | {"tools/nightly_account_snapshot.py", "tools/nightly_ownership.py", "ml/immutable_policy.py"}:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# original " + name + "\n")
    native = {"actor": "Scout", "repository": str(repo), "datastore": str(data), "state_root": str(work)}
    config = {"actor": "Scout", "state_root": str(exchange), "workflow_config": str(write(repo / "workflow.json", native)),
              "private_exchange_authorized": True, "local_profile": str(write(repo / "profile.json", {"actor": "Scout"})),
              "coordination_active": str(write(repo / "active.json", {"release": "original"}))}
    monkeypatch.setattr(repair, "_native", lambda config: native)
    monkeypatch.setattr(repair.workflow, "source_identity", lambda root: repair._source_from_files("a" * 40, repair._inventory(root)))
    monkeypatch.setattr(repair.workflow, "_verify_configuration_binding", lambda *args: None)
    monkeypatch.setattr(repair.workflow, "_verify_symbol_binding", lambda *args: None)
    monkeypatch.setattr(repair.workflow, "_verify_local_preparation", lambda *args: None)
    def binding(config):
        return {"config": config, "profile_sha256": file_checksum(Path(config["local_profile"])),
                "adapter_sources": {name: file_checksum(repo / "tools" / name)
                                    for name in ("nightly_exchange.py", "nightly_account_snapshot.py", "nightly_ownership.py")}}
    monkeypatch.setattr(repair, "_binding", binding)
    output = write(data / "completed-output.json", {"actual_original_cutoff": "2026-10-08T23:59:00Z"})
    state = {"schema_version": repair.workflow.VERSION, "actor": "Scout", "action_date": DAY,
             "status": "LOCAL_COMPLETE_PEER_SETUP_PENDING", "source_session": "2026-10-08",
             "source_identity": repair.workflow.source_identity(repo), "owner_pid": None,
             "deadline_at": "2026-10-09T11:00:00Z", "run_id": "immutable-preparation", "steps": {}}
    for step in repair.workflow.workflow_steps(state):
        state["steps"][step] = {"status": "COMPLETE", "output": {"files": {str(output): file_checksum(output)}}}
    prep = write(work / "runs" / DAY / "state.json", state)
    session = exchange / "sessions" / DAY
    status = write(session / "status.json", {"actor": "Scout", "action_date": DAY, "review_session": "2026-10-08",
                                              "status": "FAILED", "error_type": "ValueError", "failure_epoch": 1})
    original_binding = write(session / "binding.json", binding(config))
    selection = write(session / "synthesis-selection.json", {"completion_id": "original-id", "snapshot": "original-budget",
                                                              "as_of": "2026-10-09T12:00:00Z"})
    partial = write(session / "synthesis/original-id/state.json", {"status": "PARTIAL_ADOPTION", "steps": {"plan_adopted": "original-plan"}})
    budget = write(session / "cache/snapshot.json", {"observed_at": "2026-10-09T11:59:00Z", "budget": 100})
    shutil.copytree(repo, candidate)
    log = tmp_path / "checks.log"
    log.write_text("5 passed (synthetic fixture runner evidence)")
    return locals()


def claimed(env, repair_id="exchange-repair-first", **kwargs):
    return repair.claim(env["config"], action_date=DAY, owner="Scout source reconciliation", repair_id=repair_id,
                        reason="Reproduced deterministic adoption defect", reviewed=True, now=NOW, **kwargs)


def prepared(env, claim_path=None, *, path="ml/nightly_synthesis.py", risk="source", completion="actual-queue-id"):
    claim_path = claimed(env) if claim_path is None else claim_path
    changes = {}
    if risk == "source":
        (env["candidate"] / path).write_text("# fixed " + completion + "\n")
        changes[path] = "modify"
    return repair.prepare(env["config"], claim_path=claim_path, owner="Scout source reconciliation",
        candidate=env["candidate"], changes=changes, completion_record=completion,
        checks=[{"command": ["python", "-B", "-m", "pytest", "fixture"], "log": str(env["log"]), "exit_code": 0,
                 "started_at": NOW, "completed_at": NOW, "source_files": repair._inventory(env["candidate"])}],
        rationale="Reproduced and fixed exact defect", runtime_implications="Independent entrypoint; no trader mutation",
        risk=risk, reviewed=True, now=NOW)


def applied(env, spec):
    return repair.apply(env["config"], spec, owner="Scout source reconciliation", reviewed=True, now=NOW)


@pytest.fixture
def application(env, monkeypatch):
    """Actual files and immutable publication refs; no providers or processes."""
    from ml import nightly_stage_repair
    original_spec = prepared(env)
    applied(env, original_spec)
    write(env["status"], {"actor": "Scout", "action_date": DAY, "review_session": "2026-10-08",
                          "status": "PENDING", "reason": "FRESH_ACCOUNT_SNAPSHOT"})
    shutil.copytree(env["repo"], env["candidate"], dirs_exist_ok=True)
    operations = {}
    for name in ("ml/intraday/scout_accounting.py", "ml/intraday/live_session.py", "pyproject.toml",
                 "docs/development/scout-15m-manual-start.md", "tests/test_intraday_scout_accounting.py"):
        path = env["candidate"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# reviewed client " + name + "\n")
        operations[name] = {"operation": "add", "before_sha256": None, "after_sha256": file_checksum(path)}
    (env["candidate"] / repair.APPLICATION_HELPER).write_text("# new reviewed application continuation helper\n")
    fingerprints = {name: row["after_sha256"] for name, row in operations.items()}
    root = env["repo"].parent / "publications"
    def publication(identifier, files, commit):
        record = write(root / (identifier + "-record.json"), {"id": identifier, "actor": "Scout", "ready": True,
            "reviewed": True, "base_commit": "a" * 40, "fingerprints": files,
            "tests": [{"exit_code": 0, "tested_fingerprints": files}]})
        published = write(root / (identifier + "-publication.json"), {"id": identifier,
            "record_sha256": file_checksum(record), "commit_sha": commit, "remote_sha": commit,
            "source_stage": "pushed", "branch": "codex/scout/" + identifier})
        return {"record": {"path": str(record), "sha256": file_checksum(record)},
                "publication": {"path": str(published), "sha256": file_checksum(published)}}
    client = publication("reviewed-client", fingerprints, "b" * 40)
    framework = publication("reviewed-framework", {repair.APPLICATION_HELPER:
        file_checksum(env["candidate"] / repair.APPLICATION_HELPER)}, "c" * 40)
    install_input = write(root / "client-input.json", {"version": "scout-final-install-input-v1",
        "owner_chat_id": "existing-client-owner", "reviewed_base_commit": "a" * 40,
        "completion_record": client["record"], "publication": client["publication"], "operations": operations})
    # Publication proof is tested separately from source transition mechanics.
    blobs = {("b" * 40, name): digest for name, digest in fingerprints.items()}
    blobs[("c" * 40, repair.APPLICATION_HELPER)] = file_checksum(env["candidate"] / repair.APPLICATION_HELPER)
    monkeypatch.setattr(nightly_stage_repair, "_published_blob", lambda root, commit, name: blobs.get((commit, name)))
    monkeypatch.setattr(nightly_stage_repair, "_trader_running", lambda root: False)
    request = dict(claim_path=original_spec.with_name("claim.json"), owner="Scout source reconciliation",
        continuation_id="reviewed-client-install", candidate=env["candidate"], installation_input=str(install_input),
        installation_input_sha256=file_checksum(install_input), framework=framework,
        checks=[{"command": ["python", "-B", "-m", "pytest", "fixture"], "log": str(env["log"]), "exit_code": 0,
                 "started_at": NOW, "completed_at": NOW, "source_files": repair._inventory(env["candidate"])}],
        rationale="Reviewed client handoff under retained exchange owner", runtime_implications="Source installation only",
        reviewed=True, now=NOW)
    return {**env, "original_spec": original_spec, "request": request, "operations": operations,
            "input": install_input, "blobs": blobs}


def application_prepare(application, **updates):
    return repair.prepare_application_transition(application["config"], **{**application["request"], **updates})


def application_apply(application, spec):
    return repair.apply_application_transition(application["config"], spec,
        owner="Scout source reconciliation", reviewed=True, now=NOW)


def test_application_transition_preserves_original_owner_receipts_and_pending_state(application, monkeypatch):
    original = application["original_spec"].parent
    preserved = {p: p.read_bytes() for p in (original / "claim.json", original / "spec.json", original / "applied.json",
        application["session"] / "repair-claim.json", application["work"] / "repair-owner.json",
        application["prep"], application["status"], application["budget"], application["selection"], application["partial"])}
    entries = repair._transition_entries(application["session"])
    before = repair._inventory(application["repo"])
    spec = application_prepare(application)
    assert repair._inventory(application["repo"]) == before  # SEALED/UNARMED has no source effects.
    repair.verify_transition(application["config"], application["state"], repair._read(application["original_binding"]))
    assert application_prepare(application) == spec
    result = application_apply(application, spec)
    assert result["status"] == "APPLICATION_TRANSITION_APPLIED"
    assert result["parent_repair_id"] == "exchange-repair-first"
    assert result["framework_completion_record"] == "reviewed-framework"
    assert result["client_completion_record"] == "reviewed-client"
    assert result["repository_claim"]["completion_record"] == "actual-queue-id"
    assert all(p.read_bytes() == value for p, value in preserved.items())
    assert repair._transition_entries(application["session"])[:-1] == entries
    assert repair.pending_claim(application["config"], DAY) is None
    assert application_apply(application, spec)["status"] == "APPLICATION_TRANSITION_ALREADY_APPLIED"
    write(application["status"], {"actor": "Scout", "action_date": DAY, "status": "PENDING", "reason": "OTHER_INPUT"})
    repair.verify_transition(application["config"], application["state"], repair._read(application["original_binding"]))
    from tools import nightly_exchange
    monkeypatch.setattr(nightly_exchange, "_completed_result", lambda *args: {"status": "PENDING"})
    with pytest.raises(ValueError, match="completed exchange"):
        repair.resolve(application["config"], action_date=DAY, owner="Scout source reconciliation", verified_result={"status": "PENDING"})
    completed = {"status": "COMPLETE", "action_date": DAY}
    monkeypatch.setattr(nightly_exchange, "_completed_result", lambda *args: completed)
    repair.resolve(application["config"], action_date=DAY, owner="Scout source reconciliation", verified_result=completed, now=NOW)
    assert registry.read(application["work"]) is None
    assert all(p.read_bytes() == value for p, value in preserved.items()
               if p not in {application["work"] / "repair-owner.json", application["status"]})


@pytest.mark.parametrize("phase", ["before_helper", "after_helper", "after_client", "before_receipt", "after_receipt", "after_append"])
def test_application_bootstrap_and_interrupted_install_resume_exactly(application, monkeypatch, phase):
    spec = application_prepare(application)
    install, atomic = repair._install_file, repair._atomic
    before = repair._inventory(application["repo"])
    preserved_status = application["status"].read_bytes()
    seen = []
    def faulty_install(target, raw):
        relative = target.relative_to(application["repo"]).as_posix()
        seen.append(relative)
        if phase == "before_helper":
            raise OSError("injected before bootstrap")
        install(target, raw)
        if (phase == "after_helper" and relative == repair.APPLICATION_HELPER
                or phase == "after_client" and relative != repair.APPLICATION_HELPER):
            raise OSError("injected source interruption")
    def faulty_atomic(path, raw, **kwargs):
        if Path(path).parent == spec.parent and Path(path).name == "applied.json":
            if phase == "before_receipt":
                raise OSError("injected before receipt")
            if phase == "after_receipt":
                atomic(path, raw, **kwargs)
                raise OSError("injected after receipt")
        if Path(path).name == "repair-transitions.json" and phase == "after_append":
            atomic(path, raw, **kwargs)
            raise OSError("injected after append")
        return atomic(path, raw, **kwargs)
    monkeypatch.setattr(repair, "_install_file", faulty_install)
    monkeypatch.setattr(repair, "_atomic", faulty_atomic)
    with pytest.raises(OSError, match="injected"):
        application_apply(application, spec)
    assert seen[0] == repair.APPLICATION_HELPER
    assert application["status"].read_bytes() == preserved_status
    if phase == "before_helper":
        assert repair._inventory(application["repo"]) == before
        repair.verify_transition(application["config"], application["state"], repair._read(application["original_binding"]))
    elif phase != "after_append":
        assert repair.dispatch_guard(application["config"], DAY)["reason"] == "SOURCE_REPAIR_OWNED"
    monkeypatch.setattr(repair, "_install_file", install)
    monkeypatch.setattr(repair, "_atomic", atomic)
    application_apply(application, spec)
    assert len(repair._transition_entries(application["session"])) == 2
    assert application["status"].read_bytes() == preserved_status


def test_application_unarmed_status_drift_can_be_retired_without_changing_owner(application):
    spec = application_prepare(application)
    owner = registry.read(application["work"])
    write(application["status"], {"actor": "Scout", "action_date": DAY, "status": "PENDING", "reason": "NEXT_INPUT"})
    changed_status = application["status"].read_bytes()
    with pytest.raises(ValueError, match="Protected"):
        application_apply(application, spec)
    assert file_checksum(application["repo"] / repair.APPLICATION_HELPER) == repair._read(spec)["before_files"][repair.APPLICATION_HELPER]
    repair.retire_unarmed_application_transition(application["config"], spec, owner=owner["owner"],
        reason="Ordinary exchange advanced before bootstrap", reviewed=True, now=NOW)
    assert repair.pending_claim(application["config"], DAY) is None
    with pytest.raises(ValueError, match="Retired"):
        application_apply(application, spec)
    next_spec = application_prepare(application, continuation_id="reviewed-client-install-recapture")
    application_apply(application, next_spec)
    assert registry.read(application["work"]) == owner
    assert application["status"].read_bytes() == changed_status


def test_application_seal_interruption_completes_original_prepared_marker(application, monkeypatch):
    atomic = repair._atomic
    def fail_marker(path, raw, **kwargs):
        if Path(path).name == "prepared.json":
            raise OSError("injected prepare interruption")
        return atomic(path, raw, **kwargs)
    monkeypatch.setattr(repair, "_atomic", fail_marker)
    with pytest.raises(OSError):
        application_prepare(application)
    monkeypatch.setattr(repair, "_atomic", atomic)
    spec = application_prepare(application)
    application_apply(application, spec)


@pytest.mark.parametrize("path", ["ml/nightly_synthesis.py", "ml/intraday/../nightly_workflow.py", ".env", "tools/nightly_account_snapshot.py", "tests/test_unrelated.py", "docs/unrelated.md"])
def test_application_scope_rejects_neighboring_sources(application, path):
    value = repair._read(application["input"])
    value["operations"][path] = next(iter(value["operations"].values()))
    write(application["input"], value)
    with pytest.raises(ValueError, match="scope|source path"):
        application_prepare(application, installation_input_sha256=file_checksum(application["input"]))


@pytest.mark.parametrize("phase", ["prepare", "apply"])
@pytest.mark.parametrize("name", repair.STOCK_SESSION_LOCKS)
def test_application_holds_each_session_lifetime_gate(application, phase, name):
    spec = application_prepare(application) if phase == "apply" else None
    with repair.exclusive_runtime_lock(application["data"] / "locks" / name, process_name="fixture live session"):
        with pytest.raises(Exception, match="[Ll]ock|already running"):
            application_apply(application, spec) if spec else application_prepare(application)


@pytest.mark.parametrize("phase", ["prepare", "apply"])
@pytest.mark.parametrize("mode", ["active", "unverifiable"])
def test_application_actual_process_guard_fails_closed(application, monkeypatch, phase, mode):
    from ml import nightly_stage_repair
    spec = application_prepare(application) if phase == "apply" else None
    def observed(root):
        if mode == "unverifiable":
            raise ValueError("Unverifiable process inventory")
        return True
    monkeypatch.setattr(nightly_stage_repair, "_trader_running", observed)
    with pytest.raises(ValueError, match="Active trader|Unverifiable"):
        application_apply(application, spec) if spec else application_prepare(application)


@pytest.mark.parametrize("mutate", ["candidate", "log", "input", "pyproject", "unrelated_source"])
def test_application_frozen_evidence_and_unrelated_source_reject_mutation(application, mutate):
    spec = application_prepare(application)
    value = repair._read(spec)
    target = {"candidate": spec.parent / "candidate" / "ml/intraday/scout_accounting.py",
              "log": Path(value["checks"][0]["log"]), "input": application["input"],
              "pyproject": application["repo"] / "pyproject.toml",
              "unrelated_source": application["repo"] / "ml/immutable_policy.py"}[mutate]
    target.write_text("unreviewed mutation")
    with pytest.raises(ValueError):
        application_apply(application, spec)


def test_application_non_python_endpoint_remains_bound_after_install(application):
    spec = application_prepare(application)
    application_apply(application, spec)
    (application["repo"] / "pyproject.toml").write_text("unreviewed environment change")
    with pytest.raises(ValueError, match="Protected"):
        repair.verify_transition(application["config"], application["state"], repair._read(application["original_binding"]))


def test_application_rejects_client_bytes_installed_before_bootstrap(application):
    spec = application_prepare(application)
    name = "ml/intraday/scout_accounting.py"
    target = application["repo"] / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes((spec.parent / "candidate" / name).read_bytes())
    with pytest.raises(ValueError, match="before application helper"):
        application_apply(application, spec)


def test_application_armed_plan_cannot_be_retired(application, monkeypatch):
    spec = application_prepare(application)
    install = repair._install_file
    def interrupt(target, raw):
        install(target, raw)
        raise OSError("armed")
    monkeypatch.setattr(repair, "_install_file", interrupt)
    with pytest.raises(OSError):
        application_apply(application, spec)
    with pytest.raises(ValueError, match="unchanged unarmed"):
        repair.retire_unarmed_application_transition(application["config"], spec,
            owner="Scout source reconciliation", reason="Cannot retire armed source", reviewed=True, now=NOW)


@pytest.mark.parametrize("phase", ["prepare", "apply"])
def test_application_other_source_compatible_exchange_is_not_stranded(application, phase):
    spec = application_prepare(application) if phase == "apply" else None
    other = {**application["state"], "action_date": "2026-10-10",
             "source_identity": repair.workflow.source_identity(application["repo"])}
    write(application["work"] / "runs/2026-10-10/state.json", other)
    write(application["exchange"] / "sessions/2026-10-10/status.json", {"status": "PENDING"})
    with pytest.raises(ValueError, match="Another unfinished source-compatible"):
        application_apply(application, spec) if spec else application_prepare(application)


@pytest.mark.parametrize("phase", ["prepare", "apply"])
def test_application_lifetime_locks_remain_held_after_process_snapshot(application, monkeypatch, phase):
    from ml import nightly_stage_repair
    spec = application_prepare(application) if phase == "apply" else None
    def racing_session(root):
        for name in repair.STOCK_SESSION_LOCKS:
            with pytest.raises(Exception, match="[Ll]ock|already running"):
                with repair.exclusive_runtime_lock(application["data"] / "locks" / name, process_name="racing session"):
                    pass
        return False
    monkeypatch.setattr(nightly_stage_repair, "_trader_running", racing_session)
    application_apply(application, spec) if spec else application_prepare(application)


def test_application_ordinary_adapter_locks_before_repair_import_and_blocks_without_status_write(application, monkeypatch):
    from tools import nightly_exchange
    spec = application_prepare(application)
    install = repair._install_file
    def after_helper(target, raw):
        install(target, raw)
        raise OSError("armed interruption")
    monkeypatch.setattr(repair, "_install_file", after_helper)
    with pytest.raises(OSError):
        application_apply(application, spec)
    status = application["status"].read_bytes()
    monkeypatch.setattr(nightly_exchange.workflow, "load_config", lambda path: application["native"])
    monkeypatch.setattr(nightly_exchange.workflow, "verify_installation", lambda config: None)
    monkeypatch.setattr(nightly_exchange, "_context", lambda now: (DAY, "2026-10-08"))
    monkeypatch.setattr(nightly_exchange, "_completed_result", lambda *args: None)
    original_guard = repair.dispatch_guard
    def guard(config, action_date, **kwargs):
        for path in (application["work"] / "workflow.lock", application["exchange"] / "exchange.lock"):
            with pytest.raises(Timeout):
                with FileLock(str(path), timeout=0):
                    pass
        return original_guard(config, action_date, **kwargs)
    monkeypatch.setattr(repair, "dispatch_guard", guard)
    result = nightly_exchange._run_once(application["config"], now=NOW)
    assert result["reason"] == "SOURCE_REPAIR_OWNED"
    assert application["status"].read_bytes() == status
    assert not (application["session"] / "failure.json").exists()


def test_claim_precedes_source_edits_and_completion_binding_is_one_way(env):
    path = claimed(env)
    saved = path.read_bytes()
    original = registry.read(env["work"])
    assert original["completion_record"] is None
    assert repair.pending_claim(env["config"], DAY)["repair_id"] == "exchange-repair-first"
    assert claimed(env) == path and path.read_bytes() == saved
    with pytest.raises(ValueError, match="owner holds"):
        claimed(env, "another-repair-identity")
    bound = registry.bind_completion(env["work"], original, "actual-queue-id")
    assert registry.bind_completion(env["work"], original, "actual-queue-id") == bound
    with pytest.raises(ValueError, match="cannot change"):
        registry.bind_completion(env["work"], original, "substitute-id")


def test_adapter_repair_before_first_export_keeps_original_binding_anchor(env, monkeypatch):
    from tools import nightly_exchange as exchange
    env["original_binding"].unlink()
    output = env["state"]["steps"]["local_handoff"]["output"]
    output.update(package=str(env["output"]), stats_package=str(env["output"]))
    write(env["prep"], env["state"])
    original = repair.original_binding(env["config"], DAY)
    spec = prepared(env, path="tools/nightly_exchange.py")
    applied(env, spec)
    assert repair.original_binding(env["config"], DAY) == original
    monkeypatch.setattr(exchange, "_binding", repair._binding)
    def pending_before_export(*args):
        raise exchange.Pending("fixture verified first binding before private export")
    monkeypatch.setattr(exchange, "_owners", pending_before_export)
    with pytest.raises(exchange.Pending, match="first binding"):
        exchange._run(env["config"], env["native"], DAY, "2026-10-08", lambda: NOW, False)
    assert repair._read(env["original_binding"]) == original
    assert original != repair._binding(env["config"])
    assert applied(env, spec)["status"] == "EXCHANGE_REPAIR_ALREADY_APPLIED"
    assert exchange._local_state(env["config"], env["native"], DAY, "2026-10-08") == env["state"]


@pytest.mark.parametrize("path", ["ml/nightly_synthesis.py", "ml/nightly_handoff.py", "tools/nightly_exchange.py"])
def test_exact_source_install_preserves_prep_budget_selections_failure_and_receipts(env, path):
    original = {p: p.read_bytes() for p in (env["prep"], env["budget"], env["selection"], env["status"], env["partial"], env["original_binding"])}
    spec = prepared(env, path=path)
    result = applied(env, spec)
    assert result["status"] == "EXCHANGE_REPAIR_APPLIED"
    assert all(p.read_bytes() == raw for p, raw in original.items())
    assert repair.pending_claim(env["config"], DAY) is None
    assert repair.verify_transition(env["config"], env["state"], repair._read(env["original_binding"])) == {"verified": True, "repairs": 1}
    duplicate = applied(env, spec)
    assert duplicate.pop("status") == "EXCHANGE_REPAIR_ALREADY_APPLIED"
    assert duplicate == {key: value for key, value in result.items() if key != "status"}
    assert len(repair._transition_entries(env["session"])) == 1
    assert registry.read(env["work"])["owner"] == "Scout source reconciliation"


@pytest.mark.parametrize("phase", ["before_install", "after_install", "before_applied", "after_applied"])
def test_interrupted_apply_resumes_same_bytes_without_releasing_owner(env, monkeypatch, phase):
    spec = prepared(env)
    install, atomic = repair._install_file, repair._atomic
    def faulty_install(target, raw):
        if phase == "after_install":
            install(target, raw)
        raise OSError("injected interrupted installation")
    def faulty_atomic(path, raw, **kwargs):
        if Path(path).name == "applied.json":
            if phase == "after_applied":
                atomic(path, raw, **kwargs)
            raise OSError("injected interrupted receipt")
        return atomic(path, raw, **kwargs)
    if phase.endswith("install"):
        monkeypatch.setattr(repair, "_install_file", faulty_install)
    else:
        monkeypatch.setattr(repair, "_atomic", faulty_atomic)
    with pytest.raises(OSError, match="injected"):
        applied(env, spec)
    assert registry.read(env["work"])
    assert repair.pending_claim(env["config"], DAY)
    monkeypatch.setattr(repair, "_install_file", install)
    monkeypatch.setattr(repair, "_atomic", atomic)
    applied(env, spec)
    assert repair.pending_claim(env["config"], DAY) is None
    repair.verify_transition(env["config"], env["state"], repair._read(env["original_binding"]))


@pytest.mark.parametrize("phase", ["before_guard", "after_guard", "before_local_claim"])
def test_interrupted_claim_recovers_original_identity(env, monkeypatch, phase):
    acquire, atomic = registry.acquire, repair._atomic
    def fail_acquire(root, record):
        if phase == "after_guard":
            acquire(root, record)
        raise OSError("injected claim interruption")
    def fail_atomic(path, raw, **kwargs):
        if Path(path).name == "repair-claim.json":
            raise OSError("injected local claim interruption")
        return atomic(path, raw, **kwargs)
    if phase == "before_local_claim":
        monkeypatch.setattr(repair, "_atomic", fail_atomic)
    else:
        monkeypatch.setattr(registry, "acquire", fail_acquire)
    with pytest.raises(OSError, match="injected"):
        claimed(env)
    original = (env["session"] / "source-repairs/exchange-repair-first/claim.json").read_bytes()
    monkeypatch.setattr(registry, "acquire", acquire)
    monkeypatch.setattr(repair, "_atomic", atomic)
    path = claimed(env)
    assert path.read_bytes() == original
    assert registry.read(env["work"])


def test_repeated_repairs_and_no_source_restoration_form_ordered_chain(env):
    first = prepared(env, risk="external_dependency")
    first_source = repair.workflow.source_identity(env["repo"])
    applied(env, first)
    assert repair.workflow.source_identity(env["repo"]) == first_source
    for number in (2, 3):
        failure = repair._read(env["status"])
        write(env["status"], {**failure, "failure_epoch": number})
        shutil.rmtree(env["candidate"])
        shutil.copytree(env["repo"], env["candidate"])
        path = claimed(env, f"exchange-repair-{number}", supersede_applied=True)
        spec = prepared(env, path, completion=f"actual-queue-id-{number}")
        applied(env, spec)
    assert repair.verify_transition(env["config"], env["state"], repair._read(env["original_binding"]))["repairs"] == 3
    assert repair._read(env["prep"])["source_identity"] == first_source
    assert len(repair._transition_entries(env["session"])) == 3


@pytest.mark.parametrize("target", ["budget", "selection", "prep", "partial", "original_binding"])
def test_changed_frozen_evidence_refuses_installation(env, target):
    spec = prepared(env)
    env[target].write_text("changed after review")
    before = (env["repo"] / "ml/nightly_synthesis.py").read_bytes()
    with pytest.raises(ValueError):
        applied(env, spec)
    assert (env["repo"] / "ml/nightly_synthesis.py").read_bytes() == before


@pytest.mark.parametrize("target", ["source", "spec", "log"])
def test_changed_reviewed_source_spec_or_test_evidence_refuses_install(env, target):
    spec = prepared(env)
    paths = {"source": spec.parent / "candidate/ml/nightly_synthesis.py", "spec": spec, "log": spec.parent / "checks/0"}
    if target == "spec":
        value = repair._read(spec)
        value["rationale"] = "unreviewed replacement"
        write(spec, value)
    else:
        paths[target].write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        applied(env, spec)


def test_new_source_dependency_after_claim_is_rejected(env):
    path = claimed(env)
    (env["repo"] / "ml/immutable_policy.py").write_text("other writer")
    with pytest.raises(ValueError, match="changed after repair claim"):
        prepared(env, path)


def test_shared_trader_numerical_source_is_not_a_live_safe_repair(env):
    path = claimed(env)
    with pytest.raises(ValueError, match="supported independent"):
        repair.prepare(env["config"], claim_path=path, owner="Scout source reconciliation", candidate=env["candidate"],
            changes={"ml/joint_capital_adoption.py": "modify"}, completion_record="id", checks=[{}],
            rationale="not authorized safe scope", runtime_implications="trader library", reviewed=True)


def test_completed_exchange_cannot_be_claimed_or_replayed(env):
    status = repair._read(env["status"])
    write(env["status"], {**status, "status": "COMPLETE"})
    with pytest.raises(ValueError, match="unfinished"):
        claimed(env)
    assert registry.read(env["work"]) is None


def test_other_domain_owner_and_native_orphan_prevent_source_edits(env, monkeypatch):
    record = {"owner": "model owner", "repair_id": "model-repair-first", "completion_record": None,
              "token": "a" * 64, "action_date": DAY, "domain": "preparation"}
    registry.acquire(env["work"], record)
    with pytest.raises(ValueError, match="owns the repository"):
        claimed(env)
    registry.release(env["work"], record, verified=True)
    write(env["data"] / "ml/overnight-runs/orphan/stage-report.json", {"status": "RUNNING", "owner_pid": None})
    with pytest.raises(ValueError, match="interrupted native"):
        claimed(env)


def test_existing_global_workflow_lock_prevents_repair(env):
    with FileLock(str(env["work"] / "workflow.lock")):
        with pytest.raises(Timeout):
            claimed(env)


def test_changed_source_after_applied_transition_is_rejected(env):
    applied(env, prepared(env))
    (env["repo"] / "ml/nightly_synthesis.py").write_text("unknown source")
    with pytest.raises(ValueError, match="Unreviewed"):
        repair.verify_transition(env["config"], env["state"], repair._read(env["original_binding"]))


def test_partial_adoption_status_and_pointers_may_advance_after_review(env):
    applied(env, prepared(env))
    write(env["partial"], {"status": "JOINT_READY_LOCAL"})
    write(env["status"], {"status": "COMPLETE"})
    repair.verify_transition(env["config"], env["state"], repair._read(env["original_binding"]))


def test_registry_requires_exact_owner_and_verified_disposition(env):
    claimed(env)
    record = registry.read(env["work"])
    with pytest.raises(ValueError, match="Verified"):
        registry.release(env["work"], record)
    with pytest.raises(ValueError, match="Another"):
        registry.release(env["work"], {**record, "owner": "other"}, verified=True)
    assert registry.read(env["work"]) == record


def test_direct_synthesis_owner_prevents_source_repair(env):
    with repair.exclusive_runtime_lock(env["session"] / "synthesis/synthesis.lock", process_name="healthy synthesis"):
        with pytest.raises(RuntimeError, match="owns these artifacts"):
            claimed(env)
    assert registry.read(env["work"]) is None


def test_deterministic_failure_is_not_retried_without_reviewed_epoch(env):
    first = repair.record_failure(env["config"], DAY, ValueError("deterministic defect"), now=NOW)
    before = (env["session"] / "failure.json").read_bytes()
    for _ in range(3):
        assert repair.dispatch_guard(env["config"], DAY, now="2026-10-09T22:00:00Z")["reason"] == "REPAIR_REQUIRED"
    assert (env["session"] / "failure.json").read_bytes() == before
    assert first["attempts"] == 1
    applied(env, prepared(env))
    assert repair.dispatch_guard(env["config"], DAY, now=NOW) is None


def test_transient_retry_budget_cooldown_and_restored_dependency_epoch(env):
    for number in range(3):
        timestamp = f"2026-10-09T21:{number * 5:02}:00Z"
        failure = repair.record_failure(env["config"], DAY, OSError("temporary transport failure"), now=timestamp)
        assert failure["attempts"] == number + 1
        assert repair.dispatch_guard(env["config"], DAY, now=timestamp) is not None
        if number < 2:
            assert repair.dispatch_guard(env["config"], DAY, now=f"2026-10-09T21:{(number + 1) * 5:02}:00Z") is None
    assert repair.dispatch_guard(env["config"], DAY, now="2026-10-09T22:00:00Z")["reason"] == "DEPENDENCY_RESTORATION_REQUIRED"
    original = list((env["session"] / "failures").rglob("attempt-*.json"))
    applied(env, prepared(env, risk="external_dependency"))
    assert repair.dispatch_guard(env["config"], DAY, now=NOW) is None
    assert all(path.is_file() for path in original)
    assert repair.record_failure(env["config"], DAY, TimeoutError("new transient"), now=NOW)["attempts"] == 1


def test_original_error_survives_unavailable_source_fingerprint(env, monkeypatch):
    def unavailable(*args):
        raise OSError("source inventory unavailable")
    monkeypatch.setattr(repair.workflow, "source_identity", unavailable)
    saved = repair.record_failure(env["config"], DAY, ValueError("original source defect"), now=NOW)
    assert saved["error"] == "ValueError: original source defect"
    assert saved["kind"] == "SOURCE_DEFECT"


def test_interrupted_prepare_seals_original_spec_after_retry(env, monkeypatch):
    atomic = repair._atomic
    def interrupted(path, raw, **kwargs):
        if Path(path).name == "prepared.json":
            raise OSError("injected seal interruption")
        return atomic(path, raw, **kwargs)
    claim_path = claimed(env)
    monkeypatch.setattr(repair, "_atomic", interrupted)
    with pytest.raises(OSError, match="injected"):
        prepared(env, claim_path)
    spec = claim_path.with_name("spec.json")
    before = spec.read_bytes()
    monkeypatch.setattr(repair, "_atomic", atomic)
    assert prepared(env, claim_path) == spec
    assert spec.read_bytes() == before
    applied(env, spec)


@pytest.mark.parametrize("phase", ["before_replace", "after_replace", "before_local_claim"])
def test_interrupted_same_owner_continuation_has_no_takeover_gap(env, monkeypatch, phase):
    applied(env, prepared(env))
    failure = repair._read(env["status"])
    write(env["status"], {**failure, "failure_epoch": 2})
    original_replace, original_atomic = registry.os.replace, repair._atomic
    def interrupted_replace(source, target):
        if Path(target) == registry.path(env["work"]):
            if phase == "after_replace":
                original_replace(source, target)
            raise OSError("injected continuation replacement")
        return original_replace(source, target)
    def interrupted_atomic(path, raw, **kwargs):
        if Path(path).name == "repair-claim.json":
            raise OSError("injected local continuation claim")
        return original_atomic(path, raw, **kwargs)
    if phase == "before_local_claim":
        monkeypatch.setattr(repair, "_atomic", interrupted_atomic)
    else:
        monkeypatch.setattr(registry.os, "replace", interrupted_replace)
    with pytest.raises(OSError, match="injected"):
        claimed(env, "exchange-repair-second", supersede_applied=True)
    owner = registry.read(env["work"])
    assert owner is not None and owner["owner"] == "Scout source reconciliation"
    with pytest.raises(ValueError):
        registry.acquire(env["work"], {**owner, "owner": "another writer"})
    monkeypatch.setattr(registry.os, "replace", original_replace)
    monkeypatch.setattr(repair, "_atomic", original_atomic)
    claim_path = claimed(env, "exchange-repair-second", supersede_applied=True)
    assert claimed(env, "exchange-repair-second", supersede_applied=True) == claim_path
    assert registry.read(env["work"])["repair_id"] == "exchange-repair-second"
    assert len(list((env["work"] / "repair-owner-history").glob("*.json"))) == 1


def test_unchanged_failure_and_fourth_automatic_source_repair_are_refused(env):
    applied(env, prepared(env))
    with pytest.raises(ValueError, match="Unchanged failure"):
        claimed(env, "exchange-repair-second", supersede_applied=True)
    for number in (2, 3):
        write(env["status"], {**repair._read(env["status"]), "failure_epoch": number})
        shutil.rmtree(env["candidate"])
        shutil.copytree(env["repo"], env["candidate"])
        claim_path = claimed(env, f"exchange-repair-{number}", supersede_applied=True)
        applied(env, prepared(env, claim_path, completion=f"queue-id-{number}"))
    owner = registry.read(env["work"])
    write(env["status"], {**repair._read(env["status"]), "failure_epoch": 4})
    with pytest.raises(ValueError, match="Three repair attempts exhausted"):
        claimed(env, "exchange-repair-fourth", supersede_applied=True)
    assert registry.read(env["work"]) == owner


@pytest.mark.parametrize("root", ["fundamentals", "options", "signals", "technicals"])
def test_unreviewed_public_dependency_additions_are_rejected_before_resume(env, root):
    applied(env, prepared(env))
    path = env["repo"] / root / "unreviewed.py"
    path.parent.mkdir()
    path.write_text("# unexpected shared source")
    with pytest.raises(ValueError, match="dependency source change"):
        repair.verify_transition(env["config"], env["state"], repair._read(env["original_binding"]))


def test_normal_adapter_verifies_full_dependency_chain_after_source_free_restoration(env):
    from tools import nightly_exchange
    applied(env, prepared(env, risk="external_dependency"))
    path = env["repo"] / "signals/unknown.py"
    path.parent.mkdir()
    path.write_text("# unreviewed dependency")
    with pytest.raises(ValueError, match="dependency source change"):
        nightly_exchange._local_state(env["config"], env["native"], DAY, "2026-10-08")


def test_blocked_exchange_cli_reports_nonzero_without_replaying_work(env, monkeypatch, capsys):
    from tools import nightly_exchange
    monkeypatch.setattr(nightly_exchange, "load_config", lambda path: env["config"])
    monkeypatch.setattr(nightly_exchange, "run_once", lambda *args, **kwargs: {
        "status": "FAILED", "reason": "REPAIR_REQUIRED", "owner": "Scout source reconciliation"})
    assert nightly_exchange.main(["--config", str(env["repo"] / "exchange.json")]) == 1
    assert "REPAIR_REQUIRED" in capsys.readouterr().out


@pytest.mark.parametrize("damage", ["bool_exit", "float_exit", "string_command", "blank_argument", "relative_log", "directory_log",
                                     "future_start", "future_end", "naive_time", "before_claim"])
def test_prepare_rejects_unbound_check_evidence_without_binding_completion(env, damage):
    path = claimed(env)
    (env["candidate"] / "ml/nightly_synthesis.py").write_text("# reviewed fixture")
    check = {"command": ["pytest", "fixture"], "exit_code": 0, "log": str(env["log"]),
             "started_at": NOW, "completed_at": NOW, "source_files": repair._inventory(env["candidate"])}
    changes = {"bool_exit": ("exit_code", False), "float_exit": ("exit_code", 0.0), "string_command": ("command", "pytest fixture"),
               "blank_argument": ("command", ["pytest", " "]), "relative_log": ("log", "checks.log"),
               "directory_log": ("log", str(env["repo"])), "future_start": ("started_at", "2026-10-09T22:00:00Z"),
               "future_end": ("completed_at", "2026-10-09T22:00:00Z"), "naive_time": ("started_at", "2026-10-09T21:00:00"),
               "before_claim": ("started_at", "2026-10-09T20:00:00Z")}
    key, value = changes[damage]
    check[key] = value
    with pytest.raises(ValueError):
        repair.prepare(env["config"], claim_path=path, owner="Scout source reconciliation", candidate=env["candidate"],
            changes={"ml/nightly_synthesis.py": "modify"}, completion_record="must-not-bind", checks=[check],
            rationale="review", runtime_implications="safe entrypoint", reviewed=True, now=NOW)
    assert registry.read(env["work"])["completion_record"] is None
    assert not path.with_name("spec.json").exists()


def test_duplicate_apply_after_nonterminal_exchange_progress_is_read_only(env, monkeypatch):
    spec = prepared(env)
    applied(env, spec)
    write(env["status"], {"status": "PENDING", "reason": "ACCEPTED_ATLAS"})
    write(env["partial"], {"status": "JOINT_READY_LOCAL"})
    protected = {path: path.read_bytes() for path in (env["status"], env["partial"], spec.parent / "applied.json", env["prep"])}
    monkeypatch.setattr(repair, "_install_file", lambda *args: pytest.fail("Duplicate apply cannot write source"))
    assert applied(env, spec)["status"] == "EXCHANGE_REPAIR_ALREADY_APPLIED"
    assert all(path.read_bytes() == raw for path, raw in protected.items())


@pytest.mark.parametrize("raw", [b"", b"{", b"{}", b'{"owner":"one","owner":"two"}'])
def test_registry_never_treats_malformed_claim_as_unowned(env, raw):
    target = registry.path(env["work"])
    target.write_bytes(raw)
    with pytest.raises(ValueError):
        registry.read(env["work"])
    assert target.read_bytes() == raw


@pytest.mark.parametrize("mutation", ["installed", "candidate"])
def test_multi_file_apply_rechecks_exact_bytes_before_each_write(env, monkeypatch, mutation):
    claim_path = claimed(env)
    names = ("ml/nightly_synthesis.py", "ml/nightly_handoff.py")
    original = {name: (env["repo"] / name).read_bytes() for name in names}
    reviewed = b"# reviewed two-file repair\n"
    for name in names:
        (env["candidate"] / name).write_bytes(reviewed)
    spec = repair.prepare(env["config"], claim_path=claim_path, owner="Scout source reconciliation",
        candidate=env["candidate"], changes=dict.fromkeys(names, "modify"), completion_record="queue-two-file-repair",
        checks=[{"command": ["python", "-B", "-m", "pytest", "fixture"], "log": str(env["log"]),
                 "exit_code": 0, "started_at": NOW, "completed_at": NOW,
                 "source_files": repair._inventory(env["candidate"])}],
        rationale="Reproduced concurrent edit between two writes", runtime_implications="Independent entrypoints only",
        reviewed=True, now=NOW)
    installed = env["repo"] / names[1]
    payload = spec.parent / "candidate" / names[1]
    changed = installed if mutation == "installed" else payload
    unexpected = b"# concurrent edit must survive\n"
    install = repair._install_file

    def interleave(target, raw):
        install(target, raw)
        if target == env["repo"] / names[0]:
            changed.write_bytes(unexpected)

    monkeypatch.setattr(repair, "_install_file", interleave)
    with pytest.raises(ValueError, match="unreviewed bytes|candidate source changed"):
        applied(env, spec)
    assert changed.read_bytes() == unexpected
    assert (env["repo"] / names[0]).read_bytes() == reviewed
    assert installed.read_bytes() == (unexpected if mutation == "installed" else original[names[1]])
    assert registry.read(env["work"]) is not None
    assert not (spec.parent / "applied.json").exists()
    # Restore the concurrent edit explicitly in this fixture, then prove the
    # first already-installed file resumes under the same owner and receipt.
    monkeypatch.setattr(repair, "_install_file", install)
    changed.write_bytes(original[names[1]] if mutation == "installed" else reviewed)
    assert applied(env, spec)["status"] == "EXCHANGE_REPAIR_APPLIED"
    assert all((env["repo"] / name).read_bytes() == reviewed for name in names)
    assert len(repair._transition_entries(env["session"])) == 1


@pytest.fixture
def planning_env(env, monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda *args: iter(()))
    return env


@pytest.mark.parametrize("reader", sorted(repair.PLANNING_READERS))
def test_pending_exchange_planning_reader_preserves_completed_preparation(planning_env, reader):
    env = planning_env
    write(env["status"], {"actor": "Scout", "action_date": DAY, "review_session": "2026-10-08",
                          "status": "PENDING", "reason": "PREPARATION_ATLAS"})
    originals = {path: path.read_bytes() for path in (env["prep"], env["budget"], env["selection"],
                                                     env["status"], env["partial"], env["original_binding"])}
    with pytest.raises(ValueError, match="Unreviewed exchange source"):
        target = env["repo"] / reader
        original = target.read_bytes()
        try:
            target.write_bytes(b"# unreviewed overlay\n")
            repair.verify_transition(env["config"], env["state"], repair._read(env["original_binding"]))
        finally:
            target.write_bytes(original)
    spec = prepared(env, path=reader)
    frozen = repair._read(spec)
    if reader == "tools/nightly_account_snapshot.py":
        assert frozen["before_source"] == frozen["after_source"]
        expected_binding = json.loads(json.dumps(frozen["before_binding"]))
        expected_binding["adapter_sources"]["nightly_account_snapshot.py"] = file_checksum(env["candidate"] / reader)
        assert frozen["after_binding"] == expected_binding
        assert frozen["before_binding"] != frozen["after_binding"]
    assert applied(env, spec)["status"] == "EXCHANGE_REPAIR_APPLIED"
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert repair.verify_transition(env["config"], env["state"], repair._read(env["original_binding"])) == {
        "verified": True, "repairs": 1}
    assert applied(env, spec)["status"] == "EXCHANGE_REPAIR_ALREADY_APPLIED"
    assert registry.read(env["work"])["completion_record"] == "actual-queue-id"


@pytest.mark.parametrize("phase", ["after_first_reader", "after_adapter", "before_receipt", "after_receipt"])
def test_four_reader_and_adapter_binding_transition_resumes_exact_mixed_install(planning_env, monkeypatch, phase):
    from tools import nightly_exchange
    env = planning_env
    claim_path = claimed(env)
    names = sorted(repair.PLANNING_READERS)
    originals = {path: path.read_bytes() for path in (env["prep"], env["budget"], env["selection"],
                                                     env["status"], env["partial"], env["original_binding"])}
    for name in names:
        (env["candidate"] / name).write_bytes(("# corrected fixture " + name + "\n").encode())
    spec = repair.prepare(env["config"], claim_path=claim_path, owner="Scout source reconciliation",
        candidate=env["candidate"], changes=dict.fromkeys(names, "modify"), completion_record="exact-four-reader-queue",
        checks=[{"command": ["python", "-B", "-m", "pytest", "fixture"], "log": str(env["log"]), "exit_code": 0,
                 "started_at": NOW, "completed_at": NOW, "source_files": repair._inventory(env["candidate"])}],
        rationale="Four exact planning/export fixtures", runtime_implications="Quiescent installation without capture",
        reviewed=True, now=NOW)
    saved = repair._read(spec)
    expected_binding = json.loads(json.dumps(saved["before_binding"]))
    expected_binding["adapter_sources"]["nightly_account_snapshot.py"] = file_checksum(
        env["candidate"] / "tools/nightly_account_snapshot.py")
    assert saved["after_binding"] == expected_binding
    assert saved["before_source"] != saved["after_source"]
    owner = registry.read(env["work"])
    install, atomic = repair._install_file, repair._atomic

    def interrupted_install(path, raw):
        install(path, raw)
        if ((phase == "after_first_reader" and path == env["repo"] / names[0])
                or (phase == "after_adapter" and path == env["repo"] / "tools/nightly_account_snapshot.py")):
            raise OSError("interrupted mixed source and adapter install")

    def interrupted_receipt(path, raw, **kwargs):
        if Path(path).name == "applied.json" and phase in {"before_receipt", "after_receipt"}:
            if phase == "after_receipt":
                atomic(path, raw, **kwargs)
            raise OSError("interrupted mixed transition receipt")
        return atomic(path, raw, **kwargs)

    monkeypatch.setattr(repair, "_install_file", interrupted_install)
    monkeypatch.setattr(repair, "_atomic", interrupted_receipt)
    with pytest.raises(OSError, match="interrupted mixed"):
        applied(env, spec)
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    assert registry.read(env["work"]) == owner
    assert repair.pending_claim(env["config"], DAY) is not None
    monkeypatch.setattr(repair, "_install_file", install)
    monkeypatch.setattr(repair, "_atomic", atomic)
    assert applied(env, spec)["status"] == "EXCHANGE_REPAIR_APPLIED"
    assert repair._binding(env["config"]) == expected_binding
    assert all((env["repo"] / name).read_bytes() == (env["candidate"] / name).read_bytes() for name in names)
    assert all(path.read_bytes() == raw for path, raw in originals.items())
    monkeypatch.setattr(nightly_exchange, "_binding", repair._binding)
    assert nightly_exchange._local_state(env["config"], env["native"], DAY, "2026-10-08") == env["state"]
    assert applied(env, spec)["status"] == "EXCHANGE_REPAIR_ALREADY_APPLIED"
    assert len(repair._transition_entries(env["session"])) == 1
    assert registry.read(env["work"]) == owner


@pytest.mark.parametrize("reader", sorted(repair.PLANNING_READERS))
def test_planning_export_scope_cannot_delete_required_sources(planning_env, reader):
    env = planning_env
    claim_path = claimed(env)
    with pytest.raises(ValueError, match="cannot be deleted"):
        repair.prepare(env["config"], claim_path=claim_path, owner="Scout source reconciliation",
            candidate=env["candidate"], changes={reader: "delete"}, completion_record="must-not-bind",
            checks=[{"synthetic": "scope fails before checks"}], rationale="review",
            runtime_implications="deletion prohibited", reviewed=True, now=NOW)
    assert registry.read(env["work"])["completion_record"] is None


@pytest.mark.parametrize("operation", ["prepare", "apply"])
@pytest.mark.parametrize("name", repair.STOCK_SESSION_LOCKS)
@pytest.mark.parametrize("reader", sorted(repair.PLANNING_READERS))
def test_each_stock_lifetime_gate_blocks_planning_reader(planning_env, operation, name, reader):
    env = planning_env
    claim_path = claimed(env)
    spec = prepared(env, claim_path, path=reader) if operation == "apply" else None
    source = repair._inventory(env["repo"])
    original_claim = claim_path.read_bytes()
    original_prep = env["prep"].read_bytes()
    with repair.exclusive_runtime_lock(env["data"] / "locks" / name, process_name="healthy stock session"):
        with pytest.raises(RuntimeError, match="owns these artifacts"):
            if operation == "prepare":
                prepared(env, claim_path, path=reader)
            else:
                applied(env, spec)
    assert repair._inventory(env["repo"]) == source
    assert claim_path.read_bytes() == original_claim and env["prep"].read_bytes() == original_prep
    assert not claim_path.with_name("applied.json").exists()
    if operation == "prepare":
        assert not claim_path.with_name("spec.json").exists()
        assert registry.read(env["work"])["completion_record"] is None


@pytest.mark.parametrize("name", repair.STOCK_SESSION_LOCKS)
def test_legacy_independent_repair_preserves_live_safe_behavior(env, monkeypatch, name):
    from ml import nightly_stage_repair
    monkeypatch.setattr(nightly_stage_repair, "_trader_running",
                        lambda *args: pytest.fail("Legacy independent repair must retain its boundary"))
    claim_path = claimed(env)
    with repair.exclusive_runtime_lock(env["data"] / "locks" / name, process_name="healthy stock session"):
        spec = prepared(env, claim_path)
        assert applied(env, spec)["status"] == "EXCHANGE_REPAIR_APPLIED"


@pytest.mark.parametrize("operation", ["prepare", "apply"])
@pytest.mark.parametrize("command", [["python", "-m", "ml.stock_trader"],
                                      ["python", "-m", "ml.intraday.live_session"], []])
def test_actual_process_inventory_refuses_active_or_unverifiable_trader(planning_env, monkeypatch, operation, command):
    from types import SimpleNamespace
    import psutil
    env = planning_env
    claim_path = claimed(env)
    spec = prepared(env, claim_path, path="ml/gameplan_cash_ledger.py") if operation == "apply" else None
    process = SimpleNamespace(pid=123, info={"pid": 123, "name": "python.exe", "cmdline": command})
    monkeypatch.setattr(psutil, "process_iter", lambda *args: iter((process,)))
    source = repair._inventory(env["repo"])
    with pytest.raises(ValueError, match="Active trader|quiescence is unverified"):
        prepared(env, claim_path, path="ml/gameplan_cash_ledger.py") if operation == "prepare" else applied(env, spec)
    assert repair._inventory(env["repo"]) == source
    assert not claim_path.with_name("applied.json").exists()


@pytest.mark.parametrize("operation", ["prepare", "apply"])
def test_manual_start_after_process_snapshot_cannot_enter_any_stock_gate(planning_env, monkeypatch, operation):
    import psutil
    env = planning_env
    claim_path = claimed(env)
    spec = prepared(env, claim_path, path="ml/gameplan_cash_ledger.py") if operation == "apply" else None
    refused = []

    def snapshot(*args):
        for name in repair.STOCK_SESSION_LOCKS:
            with pytest.raises(RuntimeError, match="owns these artifacts"):
                with repair.exclusive_runtime_lock(env["data"] / "locks" / name, process_name="racing manual start"):
                    pytest.fail("Session started between process inspection and installation")
            refused.append(name)
        return iter(())

    monkeypatch.setattr(psutil, "process_iter", snapshot)
    if operation == "prepare":
        prepared(env, claim_path, path="ml/gameplan_cash_ledger.py")
    else:
        assert applied(env, spec)["status"] == "EXCHANGE_REPAIR_APPLIED"
    assert refused == list(repair.STOCK_SESSION_LOCKS)


@pytest.mark.parametrize("neighbor", ["ml/planning_reservations.py", "tools/nightly_ownership.py",
                                     "ml/joint_capital_plan.py", "ml/stock_trader/runtime.py"])
def test_planning_reader_scope_does_not_admit_neighboring_libraries(planning_env, neighbor):
    env = planning_env
    claim_path = claimed(env)
    with pytest.raises(ValueError, match="supported independent entrypoint scope"):
        repair.prepare(env["config"], claim_path=claim_path, owner="Scout source reconciliation",
            candidate=env["candidate"], changes={neighbor: "add"}, completion_record="must-not-bind",
            checks=[{"synthetic": "scope fails before check validation"}], rationale="review",
            runtime_implications="neighbor not supported", reviewed=True, now=NOW)
    assert registry.read(env["work"])["completion_record"] is None


def test_planning_reader_preserves_old_failed_exchange_but_fences_unfinished_preparation(planning_env):
    env = planning_env
    old = write(env["exchange"] / "sessions/2026-10-08/status.json", {"status": "FAILED", "action_date": "2026-10-08"})
    original = old.read_bytes()
    claim_path = claimed(env)
    other = write(env["work"] / "runs/2026-10-10/state.json", {"status": "FAILED"})
    with pytest.raises(ValueError, match="Another unfinished source-bound"):
        prepared(env, claim_path, path="ml/gameplan_cash_ledger.py")
    other.unlink()  # Synthetic competing run is explicitly withdrawn by its fixture owner.
    spec = prepared(env, claim_path, path="ml/gameplan_cash_ledger.py")
    applied(env, spec)
    assert old.read_bytes() == original


def test_unverifiable_exchange_owner_refuses_planning_reader(planning_env):
    import os
    env = planning_env
    claim_path = claimed(env)
    status = repair._read(env["status"])
    write(env["status"], {**status, "owner_pid": os.getpid()})
    with pytest.raises(ValueError, match="Live or unverifiable"):
        prepared(env, claim_path, path="ml/gameplan_cash_ledger.py")
    assert registry.read(env["work"])["completion_record"] is None


def other_completed_preparation(env, *, status="PENDING", historical=False):
    day = "2026-10-10"
    state = json.loads(json.dumps(env["state"]))
    state.update(action_date=day, run_id="other-completed-preparation")
    if historical:
        state["source_identity"]["source_sha256"] = "b" * 64
    path = write(env["work"] / "runs" / day / "state.json", state)
    session = env["exchange"] / "sessions" / day
    binding = write(session / "binding.json", repair._read(env["original_binding"]))
    if status is not None:
        write(session / "status.json", {"actor": "Scout", "action_date": day, "status": status})
    return path, session, binding


@pytest.mark.parametrize("operation", ["prepare", "apply"])
@pytest.mark.parametrize("status", ["PENDING", None])
def test_planning_reader_cannot_strand_another_source_valid_exchange(planning_env, operation, status):
    env = planning_env
    claim_path = claimed(env)
    spec = prepared(env, claim_path, path="ml/gameplan_cash_ledger.py") if operation == "apply" else None
    path, session, binding = other_completed_preparation(env, status=status)
    state = repair._read(path)
    assert repair.verify_transition(env["config"], state, repair._read(binding))["verified"]
    source = repair._inventory(env["repo"])
    original = path.read_bytes()
    with pytest.raises(ValueError, match="Another unfinished source-compatible exchange"):
        prepared(env, claim_path, path="ml/gameplan_cash_ledger.py") if operation == "prepare" else applied(env, spec)
    assert repair._inventory(env["repo"]) == source and path.read_bytes() == original
    assert repair.verify_transition(env["config"], state, repair._read(binding))["verified"]
    assert not claim_path.with_name("applied.json").exists()


def test_partly_installed_retry_checks_other_exchange_against_original_source(planning_env, monkeypatch):
    env = planning_env
    spec = prepared(env, path="ml/gameplan_cash_ledger.py")
    install = repair._install_file

    def interrupted(path, raw):
        install(path, raw)
        raise OSError("interrupted after source write")

    monkeypatch.setattr(repair, "_install_file", interrupted)
    with pytest.raises(OSError, match="interrupted after"):
        applied(env, spec)
    monkeypatch.setattr(repair, "_install_file", install)
    other_completed_preparation(env)
    with pytest.raises(ValueError, match="Another unfinished source-compatible exchange"):
        applied(env, spec)
    assert not spec.with_name("applied.json").exists()
    assert repair._transition_entries(env["session"]) == []


def test_already_incompatible_historical_exchange_remains_fenced_and_unchanged(planning_env):
    env = planning_env
    path, session, binding = other_completed_preparation(env, status="FAILED", historical=True)
    originals = {item: item.read_bytes() for item in (path, binding, session / "status.json")}
    with pytest.raises(ValueError, match="Unreviewed exchange source"):
        repair.verify_transition(env["config"], repair._read(path), repair._read(binding))
    spec = prepared(env, path="ml/gameplan_cash_ledger.py")
    applied(env, spec)
    assert all(item.read_bytes() == raw for item, raw in originals.items())
    with pytest.raises(ValueError, match="Unreviewed exchange source"):
        repair.verify_transition(env["config"], repair._read(path), repair._read(binding))


def test_historical_source_mismatch_does_not_hide_malformed_transition_evidence(planning_env):
    env = planning_env
    path, session, binding = other_completed_preparation(env, status="FAILED", historical=True)
    write(session / "repair-transitions.json", [{"spec_path": str(session / "outside-spec.json"),
                                               "repair_id": "historical-repair"}])
    claim_path = claimed(env)
    with pytest.raises(ValueError, match="Repair transition identity changed"):
        prepared(env, claim_path, path="ml/gameplan_cash_ledger.py")
    assert registry.read(env["work"])["completion_record"] is None


@pytest.mark.parametrize("damage", ["candidate", "original", "protected", "log", "frozen_candidate", "frozen_original",
                                    "frozen_log", "original_evidence", "dependency"])
def test_late_prepare_mutation_does_not_bind_completion(planning_env, monkeypatch, damage):
    env = planning_env
    claim_path = claimed(env)
    directory = claim_path.parent
    reader = "ml/gameplan_cash_ledger.py"
    paths = {"candidate": env["candidate"] / reader, "original": env["repo"] / reader,
             "protected": env["selection"], "log": env["log"], "frozen_candidate": directory / "candidate" / reader,
             "frozen_original": directory / "original-source" / reader, "frozen_log": directory / "checks/0",
             "original_evidence": next((directory / "original-evidence").iterdir()),
             "dependency": env["candidate"] / "ml/unreviewed_dependency.py"}
    atomic = repair._atomic

    def interleave(path, raw, **kwargs):
        atomic(path, raw, **kwargs)
        if Path(path) == directory / "checks/0":
            paths[damage].write_bytes(b"concurrent mutation must be refused\n")

    monkeypatch.setattr(repair, "_atomic", interleave)
    source = repair._inventory(env["repo"])
    with pytest.raises(ValueError, match="evidence changed|source or binding changed"):
        prepared(env, claim_path, path=reader)
    assert registry.read(env["work"])["completion_record"] is None
    assert not directory.joinpath("spec.json").exists()
    if damage != "original":
        assert repair._inventory(env["repo"]) == source


@pytest.mark.parametrize("damage", ["candidate", "original", "log", "original_evidence"])
def test_frozen_evidence_mutation_during_apply_cannot_produce_receipt(planning_env, monkeypatch, damage):
    env = planning_env
    reader = "ml/gameplan_cash_ledger.py"
    spec = prepared(env, path=reader)
    paths = {"candidate": spec.parent / "candidate" / reader, "original": spec.parent / "original-source" / reader,
             "log": spec.parent / "checks/0", "original_evidence": next((spec.parent / "original-evidence").iterdir())}
    target = paths[damage]
    original = target.read_bytes()
    install = repair._install_file

    def interleave(path, raw):
        install(path, raw)
        target.write_bytes(b"concurrent mutation must be refused\n")

    monkeypatch.setattr(repair, "_install_file", interleave)
    with pytest.raises(ValueError, match="evidence changed"):
        applied(env, spec)
    assert not spec.with_name("applied.json").exists()
    assert repair._transition_entries(env["session"]) == []
    original_owner = registry.read(env["work"])
    target.write_bytes(original)
    monkeypatch.setattr(repair, "_install_file", install)
    assert applied(env, spec)["status"] == "EXCHANGE_REPAIR_APPLIED"
    assert registry.read(env["work"]) == original_owner
    assert len(repair._transition_entries(env["session"])) == 1


def test_symlink_check_log_is_rejected_before_completion_binding(env, monkeypatch):
    path = claimed(env)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda value: value == env["log"] or original(value))
    with pytest.raises(ValueError, match="regular passing check log"):
        prepared(env, path)
    assert registry.read(env["work"])["completion_record"] is None
