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
    for name in repair.ALLOWED | {"tools/nightly_account_snapshot.py", "tools/nightly_ownership.py", "ml/immutable_policy.py"}:
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


def test_symlink_check_log_is_rejected_before_completion_binding(env, monkeypatch):
    path = claimed(env)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda value: value == env["log"] or original(value))
    with pytest.raises(ValueError, match="regular passing check log"):
        prepared(env, path)
    assert registry.read(env["work"])["completion_record"] is None
