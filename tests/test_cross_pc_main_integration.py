"""Offline main-integration tests with temporary Git repositories only."""
from pathlib import Path
import os
import subprocess
import sys

import pytest

from tools.cross_pc import core, main_integration


def git(root, *args):
    return core.git_text(root, *args)


def test_absolute_script_entrypoint_from_other_cwd(tmp_path):
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run([sys.executable, "-B", str(Path(main_integration.__file__).resolve()), "--help"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True)
    assert result.returncode == 0
    assert "prepare" in result.stdout and "publish" in result.stdout


@pytest.fixture
def published(tmp_path):
    app = tmp_path / "application"
    app.mkdir()
    git(app, "init", "-b", "main")
    git(app, "config", "user.name", "Fixture")
    git(app, "config", "user.email", "fixture@example.invalid")
    git(app, "config", "core.autocrlf", "false")
    (app / "app").mkdir()
    (app / "app/gameplan.py").write_text("VALUE = 2\n")
    (app / "app/dependency.py").write_text("FACTOR = 2\n")
    (app / "tests").mkdir()
    (app / "tests/check.py").write_text("from pathlib import Path\nassert Path('app/gameplan.py').read_text() == 'VALUE = 3\\n'\n")
    git(app, "add", ".")
    git(app, "commit", "-m", "base")
    base = git(app, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(app, "remote", "add", "origin", str(remote))
    git(app, "push", "origin", "main")
    local = tmp_path / "local"
    profile = {"contract_version": "cross-pc-v2", "repository": "jeremysecondstate/ducketz",
               "actor": "Atlas", "machine": "pc-original", "branch_prefix": "codex/atlas/",
               "symbols": ["AAA"], "authorized_producers": ["dev"], "checkout": str(app),
               "ready_root": str(local / "ready"), "snapshot_root": str(local / "objects"),
               "evidence_root": str(local / "evidence"), "state_path": str(local / "receipts.json"),
               "git_lock": str(local / "git.lock"), "remote": str(remote), "test_remote": True}
    spec = {"producer": "dev", "reviewed": True, "base_commit": base,
            "summary": "Update common Gameplan behavior", "scope": "shared",
            "change_details": ["Gameplan returns the new common value for every symbol."],
            "files": [{"path": "app/gameplan.py", "operation": "modify", "owned": True}],
            "dependencies": ["app/dependency.py", "tests/check.py"],
            "checks": [[sys.executable, "tests/check.py"]], "limitations": ["Offline only"],
            "runtime_implications": "Separate runtime installation required."}
    (app / "app/gameplan.py").write_text("VALUE = 3\n")
    record = core.queue(profile, app, spec)
    # The initial pinned fixture predates optional scope metadata. Once that
    # queue schema lands, the producer records these fields directly.
    if "scope" not in record:
        record.update(scope="shared", change_details=spec["change_details"])
        ready = Path(profile["ready_root"]) / (record["id"] + ".json")
        core.atomic(ready, record)
        receipts = core.state(profile)
        receipts["records"][record["id"]]["record_sha256"] = core.digest(ready.read_bytes())
        core.atomic(profile["state_path"], receipts)
    source_tree = tmp_path / "source-candidate"
    git(app, "worktree", "add", "--detach", str(source_tree), base)
    core.prepare_candidate(profile, record["id"], source_tree)
    core.verify_candidate(profile, record["id"], source_tree, reviewed=True)
    source = core.publish_source(profile, record["id"])
    integration_tree = tmp_path / "main-candidate"
    git(app, "worktree", "add", "--detach", str(integration_tree), base)
    return app, profile, record, source, integration_tree, remote


def _advance_main(published, path, content):
    app, _, _, _, _, remote = published
    other = app.parent / ("other-" + path.replace("/", "-"))
    git(app.parent, "clone", str(remote), str(other))
    git(other, "switch", "main")
    git(other, "config", "user.name", "Other")
    git(other, "config", "user.email", "other@example.invalid")
    target = other / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(other, "add", "--", path)
    git(other, "commit", "-m", "other change")
    git(other, "push", "origin", "main")
    return git(other, "rev-parse", "HEAD")


def test_fast_forward_main_and_idempotent_receipt(published):
    app, profile, record, source, tree, _ = published
    before = (git(app, "rev-parse", "HEAD"), git(app, "status", "--porcelain"),
              (app / ".git/index").read_bytes())
    prepared = main_integration.prepare(profile, record["id"], tree)
    assert prepared["source_sha"] == source["commit_sha"]
    with pytest.raises(ValueError, match="review"):
        main_integration.verify(profile, record["id"], tree)
    verified = main_integration.verify(profile, record["id"], tree, reviewed=True)
    message = git(tree, "log", "-1", "--format=%B")
    assert "Scope: shared" in message and "modify app/gameplan.py" in message
    assert "Fresh offline checks:" in message and "Runtime: Separate runtime installation required." in message
    assert main_integration.verify(profile, record["id"], tree, reviewed=True)["candidate_sha"] == verified["candidate_sha"]
    assert main_integration.prepare(profile, record["id"], tree)["candidate_sha"] == verified["candidate_sha"]
    with pytest.raises(ValueError, match="approval"):
        main_integration.publish(profile, record["id"], tree)
    integrated = main_integration.publish(profile, record["id"], tree, approved=True)
    assert integrated["stage"] == "main_integrated"
    assert integrated["verified_remote_sha"] == verified["candidate_sha"]
    assert main_integration.publish(profile, record["id"], tree, approved=True)["candidate_sha"] == verified["candidate_sha"]
    assert main_integration.prepare(profile, record["id"], tree)["stage"] == "main_integrated"
    assert before == (git(app, "rev-parse", "HEAD"), git(app, "status", "--porcelain"),
                      (app / ".git/index").read_bytes())


def test_unrelated_main_advance_is_rebased_with_fresh_checks(published):
    app, profile, record, _, tree, _ = published
    advanced = _advance_main(published, "docs/new.md", "unrelated\n")
    assert main_integration.prepare(profile, record["id"], tree)["main_before"] == advanced
    verified = main_integration.verify(profile, record["id"], tree, reviewed=True)
    assert git(tree, "rev-parse", verified["candidate_sha"] + "^") == advanced
    assert main_integration.publish(profile, record["id"], tree, approved=True)["stage"] == "main_integrated"


@pytest.mark.parametrize("path", ["app/gameplan.py", "app/dependency.py"])
def test_main_collision_blocks_before_worktree_mutation(published, path):
    _, profile, record, _, tree, _ = published
    before = git(tree, "rev-parse", "HEAD")
    _advance_main(published, path, "OTHER = 9\n")
    with pytest.raises(ValueError, match="owned path or reviewed dependency"):
        main_integration.prepare(profile, record["id"], tree)
    assert git(tree, "rev-parse", "HEAD") == before


def test_main_race_blocks_push_and_retains_prepared_receipt(published):
    _, profile, record, _, tree, remote = published
    main_integration.prepare(profile, record["id"], tree)
    verified = main_integration.verify(profile, record["id"], tree, reviewed=True)
    advanced = _advance_main(published, "docs/new.md", "another change\n")
    with pytest.raises(ValueError, match="main advanced"):
        main_integration.publish(profile, record["id"], tree, approved=True)
    assert git(tree, "ls-remote", "--heads", "origin", "refs/heads/main").split()[0] == advanced
    receipt = main_integration._load_receipt(profile, record["id"], verified["main_before"])[1]
    assert receipt["stage"] == "prepared"


def test_unpushed_record_cannot_enter_main(published):
    _, profile, record, _, tree, _ = published
    receipts = core.state(profile)
    receipts["records"][record["id"]]["source_stage"] = "committed"
    core.atomic(profile["state_path"], receipts)
    with pytest.raises(ValueError, match="verified own-branch push"):
        main_integration.prepare(profile, record["id"], tree)


def test_symbol_specific_and_mixed_configuration_require_field_review(published):
    _, profile, record, _, tree, _ = published
    ready = Path(profile["ready_root"]) / (record["id"] + ".json")
    revised = core.load(ready)
    revised["scope"] = "symbol-specific"
    core.atomic(ready, revised)
    receipts = core.state(profile)
    receipts["records"][record["id"]]["record_sha256"] = core.digest(ready.read_bytes())
    core.atomic(profile["state_path"], receipts)
    with pytest.raises(ValueError, match="sealed shared scope"):
        main_integration.prepare(profile, record["id"], tree)
    revised["scope"] = "shared"
    core.atomic(ready, revised)
    receipts = core.state(profile)
    receipts["records"][record["id"]]["record_sha256"] = core.digest(ready.read_bytes())
    core.atomic(profile["state_path"], receipts)
    profile["local_config_fields"] = {"app/gameplan.py": ["/symbols"]}
    with pytest.raises(ValueError, match="field-level peer overlay review"):
        main_integration.prepare(profile, record["id"], tree)
