"""Offline exact-byte installation, contention and interrupted-journal checks."""
import copy
import json
from pathlib import Path
import subprocess
import sys

from filelock import FileLock
import pytest

from tools import nightly_source_adoption as adoption
from tools.cross_pc import core


RECORD = "20261014T044350Z-" + "a" * 32


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(core.encoded(value))


@pytest.fixture
def local(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    for args in (("init",), ("config", "user.name", "Offline Fixture"),
                 ("config", "user.email", "fixture@local.invalid")):
        subprocess.run(["git", *args], cwd=repository, capture_output=True, check=True)
    source = repository / "ml/example.py"
    source.parent.mkdir()
    source.write_text("VALUE = 'old'\n")
    subprocess.run(["git", "add", "ml/example.py"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-m", "Offline baseline"],
                   cwd=repository, check=True, capture_output=True)
    root = tmp_path / "workflow"
    state_path = root / "runs/2026-10-14/state.json"
    identity = adoption.source_identity(repository)
    state = {"run_id": "same-run", "actor": "Scout", "action_date": "2026-10-14",
             "source_session": "2026-10-13", "status": "WAITING_PREREQUISITE", "owner_pid": None,
             "source_identity": identity, "current_step": "prepare_stats",
             "deadline_at": "2026-10-14T11:00:00Z", "private_config": {"preserve": "local"},
             "steps": {"datastore_catchup": {"status": "COMPLETE", "attempts": 1,
                                              "output": {"files": {"original-native-receipt": "unchanged"}}}}}
    write(state_path, state)
    write(root / "latest.json", {"run_id": state["run_id"], "state_path": str(state_path)})
    profile = {"contract_version": "cross-pc-v2", "repository": "jeremysecondstate/ducketz",
               "actor": "Scout", "machine": "pc-new", "branch_prefix": "codex/scout/",
               "symbols": [], "authorized_producers": ["human-authorized-development"],
               "checkout": str(repository), "test_remote": True,
               "state_path": str(tmp_path / "receipts.json"), "ready_root": str(tmp_path / "ready"),
               "snapshot_root": str(tmp_path / "snapshots")}
    profile_path = tmp_path / "profile.json"
    write(profile_path, profile)
    config = {"repository": str(repository), "state_root": str(root), "actor": "Scout",
              "local_profile": str(profile_path)}
    monkeypatch.setattr(adoption, "_installed_core", lambda config: core)
    payloads = {"ml/example.py": b"VALUE = 'reviewed'\n", "ml/new_dependency.py": b"NEW = True\n"}
    fingerprints = {path: core.store_blob(profile, raw) for path, raw in payloads.items()}
    log, imports = tmp_path / "check.log", tmp_path / "check-imports.json"
    log.write_text("Offline fixture check passed\n")
    write(imports, {"violations": []})
    test = {"argv": [sys.executable, "-B", "-m", "pytest", "tests/test_nightly_source_adoption.py"],
            "exit_code": 0, "tested_fingerprints": fingerprints,
            "evidence_path": str(log), "output_sha256": core.digest(log.read_bytes()),
            "import_evidence_path": str(imports), "import_evidence_sha256": core.digest(imports.read_bytes())}
    files = [{"path": name, "operation": "modify" if name == "ml/example.py" else "add", "owned": True}
             for name in payloads]
    record = {"id": RECORD, "contract_version": "cross-pc-v2", "actor": "Scout",
              "producer": "human-authorized-development", "ready": True, "reviewed": True,
              "tests": [test], "files": files, "fingerprints": fingerprints, "scope": "shared",
              "completed_at_utc": "2026-10-14T05:00:00Z"}
    record_path = Path(profile["ready_root"]) / (RECORD + ".json")
    write(record_path, record)
    receipts = core.new_state(profile)
    receipts["records"][RECORD] = {"id": RECORD, "record_sha256": core.digest(record_path.read_bytes())}
    write(Path(profile["state_path"]), receipts)
    authority_path = tmp_path / "human-installation.json"
    authority = {"issuer": "Jeremy", "completion_record": RECORD, "authorized_paths": list(payloads),
                 "instruction": "Install this exact reviewed nightly persistence repair between responsibilities.",
                 "recorded_at": "2026-10-14T05:00:00Z"}
    write(authority_path, authority)
    spec = {"schema_version": adoption.VERSION, "completion_record": RECORD,
            "record_sha256": receipts["records"][RECORD]["record_sha256"], "repository": str(repository),
            "baseline_source_identity": identity, "baseline_source_inventory": adoption.source_inventory(repository),
            "run_binding": {"run_id": state["run_id"], "action_date": state["action_date"],
                            "source_session": state["source_session"], "state_path": str(state_path)},
            "installation_authority": {"path": str(authority_path), "sha256": core.digest(authority_path.read_bytes())},
            "files": [{"path": name, "expected_sha256": core.digest(source.read_bytes()) if name == "ml/example.py" else None,
                       "payload_path": str(core.blob_path(profile, digest)), "payload_sha256": digest}
                      for name, digest in fingerprints.items()]}
    pending = root / "pending-source-adoption.json"
    write(pending, spec)
    return {"config": config, "spec": spec, "pending": pending, "state": state, "state_path": state_path,
            "source": source, "repository": repository, "root": root, "log": log,
            "record_path": record_path, "record": record, "profile": profile,
            "new_source": repository / "ml/new_dependency.py", "payloads": payloads}


def test_busy_workflow_defers_without_changing_source(local):
    original = local["source"].read_bytes()
    with FileLock(str(local["root"] / "workflow.lock"), timeout=0):
        result = adoption.attempt_pending(local["config"])
    assert result["status"] == "PENDING"
    assert local["source"].read_bytes() == original
    assert not local["new_source"].exists()


def test_exact_install_adopts_same_state_and_preserves_outputs(local):
    spec_bytes = local["pending"].read_bytes()
    result = adoption.attempt_pending(local["config"])
    assert result["status"] == "INSTALLED"
    state = json.loads(local["state_path"].read_text())
    assert state["source_identity"] == adoption.source_identity(local["repository"])
    for key in set(local["state"]) - {"source_identity"}:
        assert state[key] == local["state"][key]
    assert len(state["source_adoptions"]) == 1
    audit = json.loads(Path(result["audit"]).read_text())
    assert audit["before_source"] == local["state"]["source_identity"]
    assert audit["after_source"] == state["source_identity"]
    assert audit["before_inventory"] == local["spec"]["baseline_source_inventory"]
    assert audit["after_inventory"] == adoption.source_inventory(local["repository"])
    assert local["source"].read_bytes() == local["payloads"]["ml/example.py"]
    assert local["new_source"].read_bytes() == local["payloads"]["ml/new_dependency.py"]
    preserved = local["state_path"].read_bytes()
    assert result["selector_retired"] and not local["pending"].exists()
    archived = local["root"] / "source-adoptions" / RECORD / "applied-spec.json"
    assert archived.read_bytes() == spec_bytes
    assert adoption.attempt_pending(local["config"])["status"] == "NO_PENDING"
    assert local["state_path"].read_bytes() == preserved


@pytest.mark.parametrize("owned", [True, False])
def test_unknown_live_edits_are_not_overwritten(local, owned):
    target = local["source"] if owned else local["repository"] / "ml/other_writer.py"
    target.write_text("OTHER_WRITER = True\n")
    original_state = local["state_path"].read_bytes()
    result = adoption.attempt_pending(local["config"])
    assert result["status"] == "REFUSED"
    assert "baseline" in result["error"]
    assert target.read_text() == "OTHER_WRITER = True\n"
    assert local["state_path"].read_bytes() == original_state
    assert not local["new_source"].exists()


class InjectedCrash(BaseException):
    pass


def crash_after_new_module(monkeypatch, local):
    original = adoption._atomic_bytes
    def crashing(path, raw):
        original(path, raw)
        if Path(path) == local["new_source"]:
            raise InjectedCrash()
    monkeypatch.setattr(adoption, "_atomic_bytes", crashing)
    with pytest.raises(InjectedCrash):
        adoption.attempt_pending(local["config"])
    monkeypatch.setattr(adoption, "_atomic_bytes", original)


def test_partial_crash_resumes_exact_journal_without_rewriting_state_history(monkeypatch, local):
    crash_after_new_module(monkeypatch, local)
    assert local["new_source"].exists()
    assert local["source"].read_text() == "VALUE = 'old'\n"
    result = adoption.attempt_pending(local["config"])
    assert result["status"] == "INSTALLED"
    state = json.loads(local["state_path"].read_text())
    assert state["steps"] == local["state"]["steps"]
    assert len(state["source_adoptions"]) == 1


def test_crash_after_identity_save_does_not_duplicate_adoption_history(monkeypatch, local):
    original = adoption._atomic_bytes
    def crashing(path, raw):
        original(path, raw)
        if Path(path) == local["state_path"]:
            raise InjectedCrash()
    monkeypatch.setattr(adoption, "_atomic_bytes", crashing)
    with pytest.raises(InjectedCrash):
        adoption.attempt_pending(local["config"])
    saved = local["state_path"].read_bytes()
    monkeypatch.setattr(adoption, "_atomic_bytes", original)
    assert adoption.attempt_pending(local["config"])["status"] == "INSTALLED"
    assert local["state_path"].read_bytes() == saved
    assert len(json.loads(saved)["source_adoptions"]) == 1


def test_unknown_edit_after_partial_crash_is_preserved(monkeypatch, local):
    crash_after_new_module(monkeypatch, local)
    local["new_source"].write_text("ANOTHER_WRITER = True\n")
    assert adoption.attempt_pending(local["config"])["status"] == "REFUSED"
    assert local["new_source"].read_text() == "ANOTHER_WRITER = True\n"
    assert local["source"].read_text() == "VALUE = 'old'\n"
    assert json.loads(local["state_path"].read_text()) == local["state"]


@pytest.mark.parametrize("after_crash", [False, True])
def test_tampered_passing_evidence_is_refused_and_partial_bytes_rolled_back(monkeypatch, local, after_crash):
    if after_crash:
        crash_after_new_module(monkeypatch, local)
    local["log"].write_text("tampered evidence\n")
    result = adoption.attempt_pending(local["config"])
    assert result["status"] == "REFUSED"
    assert "test evidence" in result["error"]
    assert local["source"].read_text() == "VALUE = 'old'\n"
    assert not local["new_source"].exists()
    assert json.loads(local["state_path"].read_text()) == local["state"]


@pytest.mark.parametrize("change", ["run", "selector", "state_identity"])
def test_current_run_binding_is_required(local, change):
    if change == "selector":
        write(local["root"] / "latest.json", {"run_id": "another-run", "state_path": str(local["state_path"])})
    else:
        state = copy.deepcopy(local["state"])
        if change == "run":
            state["run_id"] = "another-run"
        else:
            state["source_identity"]["source_sha256"] = "0" * 64
        write(local["state_path"], state)
    before = local["state_path"].read_bytes()
    assert adoption.attempt_pending(local["config"])["status"] == "REFUSED"
    assert local["state_path"].read_bytes() == before
    assert not local["new_source"].exists()


@pytest.mark.parametrize("field", ["recovery", "scheduled_recovery", "planning_tail_continuation", "repair_claim"])
def test_frozen_recovery_and_genuine_claims_remain_untouched(local, field):
    state = {**local["state"], field: {"original": "preserve"}}
    write(local["state_path"], state)
    assert adoption.attempt_pending(local["config"])["status"] == "REFUSED"
    assert json.loads(local["state_path"].read_text()) == state
    assert not local["new_source"].exists()


def test_running_state_without_lock_is_deferred(local):
    state = {**local["state"], "status": "RUNNING", "owner_pid": 12345}
    write(local["state_path"], state)
    assert adoption.attempt_pending(local["config"])["status"] == "PENDING"
    assert json.loads(local["state_path"].read_text()) == state


def test_payload_tamper_cannot_be_authorized_by_a_boolean(local):
    payload = Path(local["spec"]["files"][0]["payload_path"])
    payload.write_bytes(b"UNREVIEWED = True\n")
    local["spec"]["reviewed"] = True
    write(local["pending"], local["spec"])
    assert adoption.attempt_pending(local["config"])["status"] == "REFUSED"
    assert not local["new_source"].exists()


def test_terminal_state_and_its_history_are_not_rewritten(local):
    state = {**local["state"], "status": "COMPLETE", "completed_at": "original"}
    write(local["state_path"], state)
    before = local["state_path"].read_bytes()
    assert adoption.attempt_pending(local["config"])["status"] == "INSTALLED"
    assert local["state_path"].read_bytes() == before


def test_pinned_helper_manifest_tamper_is_detected_before_loading_code(monkeypatch, tmp_path):
    release = tmp_path / "release"
    manifest = release / "installation.json"
    write(manifest, {"commit": "a" * 40, "files": {}})
    active = tmp_path / "active.json"
    write(active, {"release_root": str(release), "commit": "a" * 40,
                   "manifest_sha256": "0" * 64})
    with pytest.raises(ValueError, match="manifest changed"):
        # Restore the actual verifier instead of the queue fixture's pinned-core binding.
        adoption._installed_core({"coordination_active": str(active)})


def reseal(local, record):
    write(local["record_path"], record)
    receipts_path = Path(local["profile"]["state_path"])
    receipts = json.loads(receipts_path.read_text())
    receipts["records"][RECORD]["record_sha256"] = core.digest(local["record_path"].read_bytes())
    write(receipts_path, receipts)
    local["spec"]["record_sha256"] = receipts["records"][RECORD]["record_sha256"]
    write(local["pending"], local["spec"])


def test_passing_checks_must_match_the_effective_installed_dependencies(local):
    dependency = local["repository"] / "ml/dependency.py"
    dependency.write_text("UNREVIEWED_DEPENDENCY = True\n")
    reviewed = core.store_blob(local["profile"], b"REVIEWED_DEPENDENCY = True\n")
    record = copy.deepcopy(local["record"])
    record["fingerprints"]["ml/dependency.py"] = reviewed
    record["tests"][0]["tested_fingerprints"] = record["fingerprints"]
    reseal(local, record)
    local["spec"]["baseline_source_inventory"] = adoption.source_inventory(local["repository"])
    local["spec"]["baseline_source_identity"] = adoption.source_identity(local["repository"])
    write(local["pending"], local["spec"])
    result = adoption.attempt_pending(local["config"])
    assert result["status"] == "REFUSED" and "Installed dependency" in result["error"]
    assert dependency.read_text() == "UNREVIEWED_DEPENDENCY = True\n"
    assert not local["new_source"].exists()


def prior_review(local):
    prior_id = "20261014T044351Z-" + "b" * 32
    path = local["repository"] / "ml/prior_reviewed.py"
    path.write_text("PRIOR_REVIEWED = True\n")
    digest = core.store_blob(local["profile"], path.read_bytes())
    prior = copy.deepcopy(local["record"])
    prior.update(id=prior_id, files=[{"path": "ml/prior_reviewed.py", "operation": "add", "owned": True}],
                 fingerprints={"ml/prior_reviewed.py": digest})
    prior["tests"][0]["tested_fingerprints"] = prior["fingerprints"]
    record_path = Path(local["profile"]["ready_root"]) / (prior_id + ".json")
    write(record_path, prior)
    receipts_path = Path(local["profile"]["state_path"])
    receipts = json.loads(receipts_path.read_text())
    receipts["records"][prior_id] = {"id": prior_id, "record_sha256": core.digest(record_path.read_bytes())}
    write(receipts_path, receipts)
    spec = local["spec"]
    spec["run_source_inventory"] = copy.deepcopy(spec["baseline_source_inventory"])
    spec["run_source_identity"] = copy.deepcopy(spec["baseline_source_identity"])
    spec["baseline_source_inventory"] = adoption.source_inventory(local["repository"])
    spec["baseline_source_identity"] = adoption.source_identity(local["repository"])
    spec["reviewed_prior_source_records"] = [prior_id]
    authority_path = Path(spec["installation_authority"]["path"])
    grant = json.loads(authority_path.read_text())
    grant.update(reviewed_prior_source_records=[prior_id], run_source_identity=spec["run_source_identity"],
                 baseline_source_identity=spec["baseline_source_identity"])
    write(authority_path, grant)
    spec["installation_authority"]["sha256"] = core.digest(authority_path.read_bytes())
    write(local["pending"], spec)
    return path


def test_exact_reviewed_prior_source_delta_is_adopted_and_source_drift_resolved(local):
    path = prior_review(local)
    state = {**local["state"], "status": "FAILED", "failure": {
        "kind": "SOURCE_DEFECT", "disposition": "OPEN", "fingerprint": "original-failure",
        "source_identity": local["state"]["source_identity"],
        "error": "ValueError: Application source changed during the nightly run"}}
    write(local["state_path"], state)
    assert adoption.attempt_pending(local["config"])["status"] == "INSTALLED"
    saved = json.loads(local["state_path"].read_text())
    assert saved["status"] == "READY" and saved["failure"]["disposition"] == "RESOLVED"
    assert saved["failure_history"] == [state["failure"]]
    assert saved["steps"] == state["steps"]
    assert path.read_text() == "PRIOR_REVIEWED = True\n"
    audit = json.loads(Path(saved["source_adoptions"][0]["path"]).read_text())
    assert audit["before_source"] == local["spec"]["run_source_identity"]
    assert audit["baseline_source"] == local["spec"]["baseline_source_identity"]


@pytest.mark.parametrize("mismatch", ["unreviewed", "grant", "inventory"])
def test_prior_source_delta_requires_exact_records_inventory_and_local_instruction(local, mismatch):
    path = prior_review(local)
    if mismatch == "unreviewed":
        local["spec"]["reviewed_prior_source_records"] = []
    elif mismatch == "grant":
        authority_path = Path(local["spec"]["installation_authority"]["path"])
        grant = json.loads(authority_path.read_text())
        grant.pop("reviewed_prior_source_records")
        write(authority_path, grant)
        local["spec"]["installation_authority"]["sha256"] = core.digest(authority_path.read_bytes())
    else:
        local["spec"]["run_source_inventory"]["ml/example.py"] = "0" * 64
    write(local["pending"], local["spec"])
    before = local["state_path"].read_bytes()
    assert adoption.attempt_pending(local["config"])["status"] == "REFUSED"
    assert local["state_path"].read_bytes() == before
    assert path.read_text() == "PRIOR_REVIEWED = True\n"
    assert not local["new_source"].exists()


def test_reviewed_prior_delta_does_not_resolve_an_unrelated_genuine_failure(local):
    prior_review(local)
    state = {**local["state"], "status": "FAILED", "failure": {
        "kind": "SOURCE_DEFECT", "error": "ValueError: Genuine broken input contract", "disposition": "OPEN"}}
    write(local["state_path"], state)
    assert adoption.attempt_pending(local["config"])["status"] == "INSTALLED"
    saved = json.loads(local["state_path"].read_text())
    assert saved["failure"] == state["failure"] and saved["status"] == "FAILED"


def supersede_installed_source(local):
    local["source"].write_text("NEWER_LEGITIMATE_SOURCE = True\n")
    receipts_path = Path(local["profile"]["state_path"])
    receipts = json.loads(receipts_path.read_text())
    receipts["path_frontier"]["ml/example.py"] = {
        "id": "later-record", "completed_at_utc": "2026-10-15T05:00:00Z"}
    write(receipts_path, receipts)


def test_retired_selector_never_reopens_after_later_source_or_frontier_changes(monkeypatch, local):
    assert adoption.attempt_pending(local["config"])["status"] == "INSTALLED"
    supersede_installed_source(local)
    monkeypatch.setattr(adoption, "_installed_core", lambda config: pytest.fail("Retired work must not be revalidated"))
    assert adoption.attempt_pending(local["config"])["status"] == "NO_PENDING"
    assert local["source"].read_text() == "NEWER_LEGITIMATE_SOURCE = True\n"


def test_crash_after_install_retires_exact_selector_despite_newer_dependencies(monkeypatch, local):
    spec_bytes = local["pending"].read_bytes()
    original = adoption._retire
    monkeypatch.setattr(adoption, "_retire", lambda *args: (_ for _ in ()).throw(InjectedCrash()))
    with pytest.raises(InjectedCrash):
        adoption.attempt_pending(local["config"])
    assert local["pending"].exists()
    supersede_installed_source(local)
    monkeypatch.setattr(adoption, "_retire", original)
    monkeypatch.setattr(adoption, "_installed_core", lambda config: pytest.fail("Installed history must not reopen"))
    result = adoption.attempt_pending(local["config"])
    assert result["status"] == "INSTALLED" and result["recovered_retirement"]
    assert result["selector_retired"] and not local["pending"].exists()
    assert (local["root"] / "source-adoptions" / RECORD / "applied-spec.json").read_bytes() == spec_bytes
    assert local["source"].read_text() == "NEWER_LEGITIMATE_SOURCE = True\n"
    assert adoption.attempt_pending(local["config"])["status"] == "NO_PENDING"


def test_retirement_preserves_a_newer_pending_selector(monkeypatch, local):
    original = adoption._retire
    newer = {**local["spec"], "completion_record": "20261015T044350Z-" + "c" * 32}
    def switched(config, directory, journal, pending):
        write(pending, newer)
        return original(config, directory, journal, pending)
    monkeypatch.setattr(adoption, "_retire", switched)
    result = adoption.attempt_pending(local["config"])
    assert result["status"] == "INSTALLED" and not result["selector_retired"]
    assert json.loads(local["pending"].read_text()) == newer
    assert (local["root"] / "source-adoptions" / RECORD / "applied-spec.json").exists()
