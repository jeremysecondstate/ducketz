"""Offline two-PC queue/publication/drift acceptance tests; no application imports."""
from copy import deepcopy
from pathlib import Path
import subprocess
import sys
import os
import py_compile

import pytest

from tools.cross_pc import core, drift, installation, notices


def run(root, *args):
    return core.git_text(root, *args)


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "application"
    root.mkdir()
    run(root, "init", "-b", "main")
    run(root, "config", "user.name", "Offline Fixture")
    run(root, "config", "user.email", "fixture@example.invalid")
    run(root, "config", "core.autocrlf", "false")
    (root / "app").mkdir()
    (root / "app/gameplan.py").write_text("def plan(symbols):\n    return {symbol: 2 for symbol in symbols}\n")
    (root / "app/dependency.py").write_text("FACTOR = 2\n")
    (root / "configs").mkdir()
    core.atomic(root / "configs/shared.json", {"threshold": 0.5, "symbols": ["AAA"]})
    (root / "tests").mkdir()
    (root / "tests/check.py").write_text("from pathlib import Path\nassert 'return' in Path('app/gameplan.py').read_text()\nprint('Gameplan check passed')\n")
    run(root, "add", ".")
    run(root, "commit", "-m", "fixture base")
    base = run(root, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    run(tmp_path, "init", "--bare", str(remote))
    run(root, "remote", "add", "origin", str(remote))
    run(root, "push", "origin", "main")
    local = tmp_path / "local-state"
    profile = {"contract_version": "cross-pc-v2", "repository": "jeremysecondstate/ducketz", "actor": "Atlas", "machine": "pc-original",
               "branch_prefix": "codex/atlas/", "symbols": ["AAA"], "authorized_producers": ["dev", "other"], "checkout": str(root),
               "ready_root": str(local / "ready"), "snapshot_root": str(local / "objects"), "evidence_root": str(local / "evidence"),
               "state_path": str(local / "receipts.json"), "git_lock": str(local / "git.lock"), "test_remote": True,
               "inbox_lock": str(local / "inbox.lock"), "inbox_state": str(local / "inbox.json"),
               "outgoing_root": str(local / "outgoing"), "transport_cache": str(local / "transport.git"), "remote": str(remote)}
    spec = {"producer": "dev", "reviewed": True, "base_commit": base, "summary": "Share Gameplan behavior",
            "files": [{"path": "app/gameplan.py", "operation": "modify", "owned": True}],
            "dependencies": ["app/dependency.py", "tests/check.py"], "checks": [[sys.executable, "tests/check.py"]],
            "limitations": ["Offline fixture"], "runtime_implications": "Source availability only; no runtime change."}
    (root / "app/gameplan.py").write_text("def plan(symbols):\n    return {symbol: 3 for symbol in symbols}\n")
    return root, profile, spec


def candidate(tmp_path, root, base):
    tree = tmp_path / "managed"
    run(root, "worktree", "add", "--detach", str(tree), base)
    return tree


def test_immutable_snapshot_and_later_source_mutation(setup):
    root, profile, spec = setup
    record = core.queue(profile, root, spec)
    sha = record["fingerprints"]["app/gameplan.py"]
    raw = core.blob_path(profile, sha).read_bytes()
    (root / "app/gameplan.py").write_text("later unrelated writer\n")
    with pytest.raises(ValueError, match="changed after tests"):
        core.validate_record(profile, record["id"])
    assert core.blob_path(profile, sha).read_bytes() == raw
    assert core.plan(profile)["source_blocker"]


def test_dependency_mutation_and_mutation_during_test(setup):
    root, profile, spec = setup
    record = core.queue(profile, root, spec)
    (root / "app/dependency.py").write_text("FACTOR = 4\n")
    with pytest.raises(ValueError, match="changed after tests"):
        core.validate_record(profile, record["id"])
    (root / "tests/check.py").write_text("from pathlib import Path\nPath('app/dependency.py').write_text('changed')\n")
    with pytest.raises(ValueError, match="during verification"):
        core.queue(profile, root, spec)


def test_unowned_dirty_dependency_blocks_isolated_candidate(setup, tmp_path):
    root, profile, spec = setup
    (root / "app/dependency.py").write_text("FACTOR = 4\n")
    record = core.queue(profile, root, spec)
    tree = candidate(tmp_path, root, spec["base_commit"])
    with pytest.raises(ValueError, match="dependency closure"):
        core.prepare_candidate(profile, record["id"], tree)
    assert run(tree, "status", "--porcelain") == ""


def test_overlapping_writers_require_explicit_supersession(setup):
    root, profile, spec = setup
    first = core.queue(profile, root, spec)
    spec["producer"] = "other"
    with pytest.raises(ValueError, match="overlapping producer"):
        core.queue(profile, root, spec)
    spec["supersedes"] = [first["id"]]
    with pytest.raises(ValueError, match="overlapping producer"):
        core.queue(profile, root, spec)
    profile["ownership_resolutions"] = {first["id"]: {"from": "dev", "to": "other", "approved": True}}
    second = core.queue(profile, root, spec)
    assert core.state(profile)["records"][first["id"]]["source_stage"] == "superseded"
    assert core.plan(profile)["source"] == second["id"]


def test_queue_fairness_is_independent_of_delivery_outage(setup):
    root, profile, spec = setup
    first = core.queue(profile, root, spec)
    (root / "app/gameplan.py").write_text("def plan(symbols):\n    return dict.fromkeys(symbols, 4)\n")
    second = core.queue(profile, root, spec)
    receipts = core.state(profile)
    receipts["notices"]["undeliverable"] = {"stage": "pending"}
    core.atomic(profile["state_path"], receipts)
    checks = [core.plan(profile) for _ in range(2)]
    assert {x["source"] for x in checks} == {first["id"], second["id"]}
    assert all(x["delivery"] == "undeliverable" for x in checks)
    assert any("source_blocker" in x for x in checks)


def test_exact_publication_preserves_application_and_never_duplicates(setup, tmp_path):
    root, profile, spec = setup
    spec["scope"] = "shared"
    spec["change_details"] = ["Keep Gameplan output consistent for both PCs"]
    # CRLF bytes must survive Git staging without conversion.
    (root / "app/gameplan.py").write_bytes(b"def plan(symbols):\r\n    return {symbol: 3 for symbol in symbols}\r\n")
    before = (run(root, "rev-parse", "HEAD"), run(root, "status", "--porcelain"), (root / ".git/index").read_bytes())
    record = core.queue(profile, root, spec)
    tree = candidate(tmp_path, root, spec["base_commit"])
    core.prepare_candidate(profile, record["id"], tree)
    with pytest.raises(ValueError, match="review"):
        core.verify_candidate(profile, record["id"], tree)
    core.verify_candidate(profile, record["id"], tree, reviewed=True)
    result = core.publish_source(profile, record["id"])
    assert result["source_stage"] == "pushed"
    message = run(tree, "log", "-1", "--format=%B")
    assert "Producer: dev" in message
    assert "Scope: Shared source for Atlas and Scout" in message
    assert "- Keep Gameplan output consistent for both PCs" in message
    assert "- modify app/gameplan.py" in message
    assert "repository test script tests/check.py passed " in message
    assert record["fingerprints"]["app/gameplan.py"] not in message
    assert "output SHA-256 " + core.state(profile)["records"][record["id"]]["isolated_tests"][0]["output_sha256"] in message
    assert "- Offline fixture" in message
    assert "Runtime: Source availability only; no runtime change." in message
    assert str(sys.executable) not in message
    assert core.git(tree, "show", result["commit_sha"] + ":app/gameplan.py").stdout == (root / "app/gameplan.py").read_bytes()
    assert core.publish_source(profile, record["id"])["commit_sha"] == result["commit_sha"]
    assert before == (run(root, "rev-parse", "HEAD"), run(root, "status", "--porcelain"), (root / ".git/index").read_bytes())


def test_candidate_change_after_tests_blocks_publication(setup, tmp_path):
    root, profile, spec = setup
    record = core.queue(profile, root, spec)
    tree = candidate(tmp_path, root, spec["base_commit"])
    core.prepare_candidate(profile, record["id"], tree)
    core.verify_candidate(profile, record["id"], tree, reviewed=True)
    (tree / "app/dependency.py").write_text("FACTOR = 9\n")
    with pytest.raises(ValueError, match="changed after isolated"):
        core.publish_source(profile, record["id"])


def test_interrupted_commit_recovers_without_duplicate_commit(setup, tmp_path, monkeypatch):
    root, profile, spec = setup
    record = core.queue(profile, root, spec)
    tree = candidate(tmp_path, root, spec["base_commit"])
    core.prepare_candidate(profile, record["id"], tree)
    core.verify_candidate(profile, record["id"], tree, reviewed=True)
    real_atomic = core.atomic
    def interrupted(path, value):
        if str(path) == profile["state_path"] and value.get("records", {}).get(record["id"], {}).get("source_stage") == "committed":
            raise OSError("simulated crash after commit")
        return real_atomic(path, value)
    monkeypatch.setattr(core, "atomic", interrupted)
    with pytest.raises(OSError, match="simulated"):
        core.publish_source(profile, record["id"])
    completed = run(tree, "rev-parse", "HEAD")
    assert core.state(profile)["records"][record["id"]]["source_stage"] == "committing"
    monkeypatch.setattr(core, "atomic", real_atomic)
    result = core.publish_source(profile, record["id"])
    assert result["commit_sha"] == completed and result["recovered_commit"]


def test_commit_hooks_cannot_change_tested_bytes(setup, tmp_path):
    root, profile, spec = setup
    hook = root / ".git/hooks/pre-commit"
    hook.write_text("#!/bin/sh\necho unsafe > unexpected.txt\ngit add unexpected.txt\n")
    record = core.queue(profile, root, spec)
    tree = candidate(tmp_path, root, spec["base_commit"])
    core.prepare_candidate(profile, record["id"], tree)
    core.verify_candidate(profile, record["id"], tree, reviewed=True)
    result = core.publish_source(profile, record["id"])
    assert "unexpected.txt" not in run(tree, "ls-tree", "-r", "--name-only", result["commit_sha"])


def test_absolute_test_path_and_unbound_import_rejected(setup):
    root, profile, spec = setup
    spec["checks"] = [[sys.executable, str(root / "tests/check.py")]]
    with pytest.raises(ValueError, match="repository-relative"):
        core.queue(profile, root, spec)
    spec["checks"] = [[sys.executable, "tests/check.py"]]
    (root / "tests/check.py").write_text("import app.dependency\n")
    spec["dependencies"] = ["tests/check.py"]
    with pytest.raises(ValueError, match="verification failed"):
        core.queue(profile, root, spec)


def test_direct_script_must_be_fingerprinted(setup):
    root, profile, spec = setup
    spec["dependencies"] = ["app/dependency.py"]
    with pytest.raises(ValueError, match="direct test script"):
        core.queue(profile, root, spec)


def test_stale_bytecode_cannot_substitute_for_attested_source(setup):
    root, profile, spec = setup
    dependency = root / "app/dependency.py"
    prior = dependency.stat()
    py_compile.compile(str(dependency), doraise=True)
    dependency.write_text("FACTOR = 4\n")
    os.utime(dependency, ns=(prior.st_atime_ns, prior.st_mtime_ns))
    (root / "tests/check.py").write_text("from app.dependency import FACTOR\nassert FACTOR == 4\n")
    assert core.queue(profile, root, spec)["ready"]


def test_orphan_sealed_record_recovery(setup):
    root, profile, spec = setup
    record = core.queue(profile, root, spec)
    receipts = core.state(profile)
    receipts["records"].clear()
    core.atomic(profile["state_path"], receipts)
    assert core.plan(profile)["source"] == record["id"]
    assert core.state(profile)["records"][record["id"]]["recovered_orphan"]


def test_failed_check_does_not_queue_ready_record(setup):
    root, profile, spec = setup
    (root / "tests/check.py").write_text("raise SystemExit(7)\n")
    with pytest.raises(ValueError, match="verification failed"):
        core.queue(profile, root, spec)
    assert not core.state(profile)["records"]


@pytest.mark.parametrize("name", [
    "../x", "a/../../x", "C:/secret", "a\\b", ".git/config", ".env",
    "data/run.json", "a/CON.txt", "artifacts/analysis/overnight-20261001/verification/audit_environment.py",
    "artifacts/analysis/overnight-20261001/provider-warning/latest.json",
    "tmp/analysis.py", "ml/runs/20261001/report.json", "app/runtime-state/session.py",
    "configs/account-state.json", "ml/_paper/weights.py",
])
def test_private_and_escaping_paths_rejected(name):
    with pytest.raises(ValueError):
        core.shareable(name)


@pytest.mark.parametrize("field,value", [
    ("scope", "Atlas-only"),
    ("summary", "Updated C:\\Users\\Operator\\private.json"),
    ("runtime_implications", "account_id=123456789"),
    ("change_details", ["Read token=private-value from local state"]),
    ("limitations", ["See /Users/operator/private.log"]),
])
def test_queue_rejects_private_or_ambiguous_public_commit_metadata(setup, field, value):
    root, profile, spec = setup
    spec[field] = value
    with pytest.raises(ValueError):
        core.queue(profile, root, spec)
    assert not core.state(profile)["records"]


def test_symbol_specific_source_message_identifies_peer_boundary(setup):
    root, profile, spec = setup
    spec["scope"] = "symbol-specific"
    spec["change_details"] = ["Document Atlas-specific overlay without changing shared behavior"]
    record = core.queue(profile, root, spec)
    message = core.source_commit_message(record, record["tests"])
    assert "Scope: Atlas symbol-specific material; peer keeps its own symbol settings" in message
    assert "Document Atlas-specific overlay" in message
    assert "Completion-Record: " + record["id"] in message


@pytest.mark.parametrize("name", ["app/ui/gameplan.py", "ml/nightly_gameplan.py",
                                      "configs/hyperliquid-paper.json", "tests/test_stock_only_gameplan.py",
                                      "docs/loops-system-analysis/NIGHTLY_GAMEPLAN.md", ".gitignore"])
def test_shared_source_paths_remain_queueable(name):
    assert core.shareable(name) == name
    assert drift.shared_source_path(name)


def test_queue_rejects_overnight_artifact_before_sealing(setup):
    root, profile, spec = setup
    name = "artifacts/analysis/overnight-20261001/verification/audit_environment.py"
    path = root / name
    path.parent.mkdir(parents=True)
    path.write_text("print('local evidence')\n")
    spec["files"].append({"path": name, "operation": "add", "owned": True})
    with pytest.raises(ValueError, match="local/private path"):
        core.queue(profile, root, spec)
    assert not core.state(profile)["records"]
    assert not Path(profile["snapshot_root"]).exists()


def test_explicit_deletion_intent(setup):
    root, profile, spec = setup
    spec["files"][0]["operation"] = "delete"
    with pytest.raises(ValueError, match="current bytes"):
        core.queue(profile, root, spec)


def test_receipt_corruption_preserved(setup):
    _, profile, _ = setup
    path = Path(profile["state_path"])
    path.parent.mkdir(parents=True)
    path.write_text('{"actor":"Atlas","actor":"Scout"}')
    raw = path.read_bytes()
    with pytest.raises(ValueError, match="duplicate"):
        core.plan(profile)
    assert path.read_bytes() == raw


def test_two_symbol_profiles_share_gameplan_behavior(setup):
    root, atlas, spec = setup
    scout = deepcopy(atlas)
    scout.update(actor="Scout", machine="pc-new", branch_prefix="codex/scout/", symbols=["BBB", "CCC"])
    assert core.validate_profile(scout)
    # Exercise the same exact Gameplan fixture bytes with two distinct universes.
    namespace = {}
    exec((root / "app/gameplan.py").read_text(), namespace)
    assert namespace["plan"](atlas["symbols"]) == {"AAA": 3}
    assert namespace["plan"](scout["symbols"]) == {"BBB": 3, "CCC": 3}
    assert atlas["symbols"] == ["AAA"] and scout["symbols"] == ["BBB", "CCC"]
    first = drift.inspect(atlas, spec["base_commit"])
    second = drift.inspect(scout, spec["base_commit"])
    assert drift.compare(first, second)["same_actual_bytes"]
    assert any(x["path"] == "app/gameplan.py" for x in first["deviations"])
    assert first["runtime_version"] == "unverified"


def test_mixed_config_only_explicit_local_fields_excluded():
    left = {"symbols": ["AAA"], "threshold": 0.5, "account": {"owner": "atlas"}, "weights": [1, 2]}
    right = {"symbols": ["BBB"], "threshold": 0.5, "account": {"owner": "scout"}, "weights": [1, 2]}
    pointers = ["/symbols", "/account/owner"]
    assert drift.project_config(left, pointers) == drift.project_config(right, pointers)
    right["threshold"] = 0.6
    assert drift.project_config(left, pointers) != drift.project_config(right, pointers)
    assert left["symbols"] == ["AAA"]


def test_meaningful_run_reporting_is_deduplicated(setup):
    _, profile, spec = setup
    first = core.record_run(profile, "observer", "blocked", "Peer evidence pending", spec["base_commit"], "peer unavailable")
    second = core.record_run(profile, "observer", "blocked", "Peer evidence pending", spec["base_commit"], "peer unavailable")
    assert first["meaningful_change"] and not second["meaningful_change"]


def test_notice_delivery_failure_keeps_source_receipt_and_prepared_transaction(setup, monkeypatch):
    root, profile, spec = setup
    record = core.queue(profile, root, spec)
    receipts = core.state(profile)
    receipts["records"][record["id"]]["source_stage"] = "pushed"
    receipts["records"][record["id"]]["commit_sha"] = "a" * 40
    core.atomic(profile["state_path"], receipts)
    notice = {"change_id": "fixture", "summary": "Offline test", "author": "Atlas", "repository": profile["repository"],
              "files": ["app/gameplan.py"], "tests": "offline passed", "scope": "source", "limitations": [], "runtime_implications": "none"}
    item = notices.enqueue(profile, notice, reviewed=True)
    assert notices.enqueue(profile, notice, reviewed=True)["id"] == item["id"]
    class Failure:
        def prepare(self, directory):
            return {"commit_sha": "b" * 40}
        def publish_prepared(self, transaction):
            raise RuntimeError("network unavailable")
    monkeypatch.setattr(notices, "transport", lambda _: Failure())
    with pytest.raises(RuntimeError, match="network unavailable"):
        notices.deliver(profile, item["id"])
    saved = core.state(profile)
    assert saved["records"][record["id"]]["source_stage"] == "pushed"
    assert saved["notices"][item["id"]]["transaction"]["commit_sha"] == "b" * 40


def test_notice_write_ahead_recovers_interrupted_sealing(setup, monkeypatch):
    _, profile, _ = setup
    notice = {"change_id": "task-finding", "summary": "Fixture", "author": "Atlas", "repository": profile["repository"],
              "files": [], "tests": "not applicable", "scope": "observation", "limitations": [], "runtime_implications": "none"}
    real = notices.seal_notice
    def failure(*args):
        raise OSError("interrupted sealing")
    monkeypatch.setattr(notices, "seal_notice", failure)
    with pytest.raises(OSError):
        notices.enqueue(profile, notice, reviewed=True)
    pending = core.state(profile)["notices"]
    assert len(pending) == 1 and next(iter(pending.values()))["stage"] == "preparing"
    monkeypatch.setattr(notices, "seal_notice", real)
    result = notices.enqueue(profile, notice, reviewed=True)
    assert result["id"] == next(iter(pending)) and result["stage"] == "pending"


def test_installed_release_is_pinned_and_rollback_keeps_profile(setup, tmp_path):
    root, _, _ = setup
    for name in ["coordination/contract.json", "coordination/task-catalog.json", "tools/cross_pc/cli.py"]:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    run(root, "add", ".")
    run(root, "commit", "-m", "release one")
    one = run(root, "rev-parse", "HEAD")
    target = tmp_path / "installed"
    first = installation.install(root, one, target)
    core.atomic(target / "local-profile.json", {"symbols": ["LOCAL"]})
    (root / "tools/cross_pc/cli.py").write_text("# new release\n")
    run(root, "add", ".")
    run(root, "commit", "-m", "release two")
    installation.install(root, run(root, "rev-parse", "HEAD"), target)
    assert installation.rollback(target)["commit"] == one
    assert core.load(target / "local-profile.json") == {"symbols": ["LOCAL"]}
    (Path(first["release_root"]) / "tools/cross_pc/cli.py").write_text("tampered")
    with pytest.raises(ValueError, match="changed"):
        installation.verify(first["release_root"], first["manifest_sha256"])
