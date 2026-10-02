"""Offline crash/retry installation and complete shared-byte drift regressions."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.cross_pc import core, drift, installation


def write(root, name, content="fixture\n"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def commit(root, message):
    core.git(root, "add", ".")
    core.git(root, "commit", "-m", message)
    return core.git_text(root, "rev-parse", "HEAD")


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for args in [("init", "-b", "main"), ("config", "user.name", "Offline Fixture"),
                 ("config", "user.email", "fixture@example.invalid"),
                 ("config", "core.autocrlf", "false")]:
        core.git(root, *args)
    write(root, "app/gameplan.py", "POLICY = 1\n")
    write(root, "AGENTS.md", "Shared completion contract.\n")
    write(root, "README.md", "Shared application.\n")
    write(root, "docs/development/cross-pc-bootstrap.md", "Install pinned release.\n")
    write(root, "coordination/contract.json", "{}\n")
    write(root, "coordination/task-catalog.json", "{}\n")
    write(root, "tools/cross_pc/cli.py", "# Shared coordination CLI\n")
    write(root, "pyproject.toml", '[project]\nname = "fixture"\n')
    write(root, "requirements-ml-runtime.lock", "dependency==1\n")
    return root, commit(root, "initial release")


def profile(root, actor="Atlas", symbols=None):
    return {"contract_version": "cross-pc-v2", "repository": "jeremysecondstate/ducketz",
            "actor": actor, "machine": "pc-original" if actor == "Atlas" else "pc-new",
            "branch_prefix": "codex/" + actor.lower() + "/", "checkout": str(root),
            "symbols": symbols or ["AAA"], "authorized_producers": ["fixture"]}


def test_install_stages_verified_release_then_same_commit_is_exact_noop(repository, tmp_path):
    root, sha = repository
    destination = tmp_path / "installed"
    first = installation.install(root, sha, destination)
    active_bytes = (destination / "active.json").read_bytes()
    assert first["previous"] is None
    assert installation.verify(first["release_root"], first["manifest_sha256"])["verified"]
    second = installation.install(root, sha, destination)
    assert second == first
    assert (destination / "active.json").read_bytes() == active_bytes
    assert not list((destination / "releases").glob("*.staging"))


def test_crash_before_rename_preserves_stage_and_retry_publishes_fresh_sibling(repository, tmp_path, monkeypatch):
    root, sha = repository
    destination = tmp_path / "installed"
    rename = installation.os.rename
    monkeypatch.setattr(installation.os, "rename", lambda *_: (_ for _ in ()).throw(OSError("simulated crash")))
    with pytest.raises(ValueError, match="Staging evidence is retained"):
        installation.install(root, sha, destination)
    stages = list((destination / "releases").glob("*.staging"))
    assert len(stages) == 1
    retained = (stages[0] / "installation.json").read_bytes()
    assert not (destination / "releases" / sha).exists()
    assert not (destination / "active.json").exists()
    monkeypatch.setattr(installation.os, "rename", rename)
    active = installation.install(root, sha, destination)
    assert installation.verify(active["release_root"], active["manifest_sha256"])["verified"]
    assert (stages[0] / "installation.json").read_bytes() == retained


def test_crash_after_release_publish_retries_only_active_pointer(repository, tmp_path, monkeypatch):
    root, sha = repository
    destination = tmp_path / "installed"
    original = installation.atomic

    def crash_active(path, value):
        if Path(path).name == "active.json":
            raise OSError("simulated pointer interruption")
        return original(path, value)

    monkeypatch.setattr(installation, "atomic", crash_active)
    with pytest.raises(OSError, match="pointer interruption"):
        installation.install(root, sha, destination)
    release = destination / "releases" / sha
    before = (release / "installation.json").read_bytes()
    assert installation.verify(release)["verified"]
    monkeypatch.setattr(installation, "atomic", original)
    active = installation.install(root, sha, destination)
    assert active["previous"] is None
    assert (release / "installation.json").read_bytes() == before


def test_partial_existing_release_is_never_overwritten(repository, tmp_path):
    root, sha = repository
    destination = tmp_path / "installed"
    damaged = write(destination, "releases/" + sha + "/partial.txt", "preserve interrupted evidence")
    with pytest.raises(ValueError, match="No files were overwritten") as error:
        installation.install(root, sha, destination)
    assert str(damaged.parent) in str(error.value)
    assert "move the damaged directory aside" in str(error.value)
    assert damaged.read_text() == "preserve interrupted evidence"
    assert not (destination / "active.json").exists()


def test_install_history_and_rollback_preserve_private_profile(repository, tmp_path):
    root, first_sha = repository
    destination = tmp_path / "installed"
    first = installation.install(root, first_sha, destination)
    private = write(destination, "local-profile.json", '{"symbols":["LOCAL"]}\n')
    write(root, "tools/cross_pc/cli.py", "# next version\n")
    second_sha = commit(root, "second release")
    second = installation.install(root, second_sha, destination)
    again = installation.install(root, second_sha, destination)
    assert again == second and again["previous"] == first
    assert installation.rollback(destination) == first
    assert private.read_text() == '{"symbols":["LOCAL"]}\n'


def test_rollback_can_restore_verified_previous_release_when_current_bytes_are_damaged(repository, tmp_path):
    root, first_sha = repository
    destination = tmp_path / "installed"
    first = installation.install(root, first_sha, destination)
    write(root, "tools/cross_pc/cli.py", "# next version\n")
    second = installation.install(root, commit(root, "second release"), destination)
    damaged = write(Path(second["release_root"]), "tools/cross_pc/cli.py", "damaged bytes\n")
    assert installation.rollback(destination) == first
    assert damaged.read_text() == "damaged bytes\n"


@pytest.mark.parametrize("extra", ["injected.py", "tools/cross_pc/__pycache__/cached.pyc", "extra/empty"])
def test_verify_rejects_every_unlisted_file_or_directory(repository, tmp_path, extra):
    root, sha = repository
    active = installation.install(root, sha, tmp_path / "installed")
    release = Path(active["release_root"])
    if extra.endswith("empty"):
        (release / extra).mkdir(parents=True)
    else:
        write(release, extra)
    with pytest.raises(ValueError, match="unexpected installed release"):
        installation.verify(release, active["manifest_sha256"])


def test_destination_reparse_attribute_is_rejected_before_any_write(repository, tmp_path, monkeypatch):
    root, sha = repository
    destination = tmp_path / "linked-destination"
    destination.mkdir()
    original = Path.lstat

    def reparse(path):
        actual = original(path)
        if path == destination:
            return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=1024)
        return actual

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(ValueError, match="symlink/reparse"):
        installation.install(root, sha, destination)
    assert list(destination.iterdir()) == []


def test_linked_destination_ancestor_is_rejected(repository, tmp_path):
    root, sha = repository
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError as error:
        pytest.skip("Windows symlink creation unavailable: " + str(error))
    with pytest.raises(ValueError, match="symlink/reparse"):
        installation.install(root, sha, link / "installed")
    assert list(real.iterdir()) == []


def test_drift_includes_reference_tracked_additions_docs_manifests_and_untracked_source(repository):
    root, reference = repository
    write(root, "docs/new-shared-guide.md")
    write(root, "app/new_engine.py")
    commit(root, "local committed shared additions")
    write(root, "AGENTS.md", "changed coordination obligations\n")
    write(root, "README.md", "changed shared usage\n")
    write(root, "pyproject.toml", "# changed dependency manifest\n")
    write(root, "requirements-ml-runtime.lock", "dependency==2\n")
    write(root, "docs/untracked-guide.md")
    write(root, "configs/untracked-shared-default.json", '{"threshold":0.5}\n')
    write(root, "requirements-new.txt", "new-dependency==1\n")
    result = drift.inspect(profile(root), reference)
    deviations = {item["path"]: item for item in result["deviations"]}
    expected = {"docs/new-shared-guide.md", "app/new_engine.py", "AGENTS.md", "README.md",
                "pyproject.toml", "requirements-ml-runtime.lock", "docs/untracked-guide.md",
                "configs/untracked-shared-default.json", "requirements-new.txt"}
    assert expected <= set(deviations)
    assert deviations["app/new_engine.py"]["classification"] == "local_shared_addition"
    assert deviations["docs/untracked-guide.md"]["classification"] == "untracked_shared_source"
    assert result["reference_shared_bytes"]["app/new_engine.py"] is None


def test_drift_detects_deleted_source_and_excludes_private_runtime_even_when_tracked(repository):
    root, reference = repository
    private = ["scratch/private.py", "ml/runs/run/model.json", "ml/runs/run/model.joblib",
               "datafetching/datastore/raw.json", "configs/account-state.json", "ml/_operations/receipt.json",
               "app/state/ledger.json", "docs/secrets/private.json", ".env", "configs/private.key"]
    for name in private:
        write(root, name, "private runtime fixture")
    commit(root, "local private fixtures")
    (root / "app/gameplan.py").unlink()
    result = drift.inspect(profile(root), reference)
    assert set(private).isdisjoint(result["actual_shared_bytes"])
    row = next(item for item in result["deviations"] if item["path"] == "app/gameplan.py")
    assert row["actual_sha256"] is None and row["classification"] == "missing_shared_source"


def test_drift_compare_requires_explicit_per_machine_pre_post_symbol_proof(repository):
    root, reference = repository
    atlas = drift.inspect(profile(root), reference)
    scout = drift.inspect(profile(root, "Scout", ["BBB", "CCC"]), reference)
    default = drift.compare(atlas, scout)
    assert default["same_actual_bytes"] is True
    assert default["symbol_profiles_preserved"] == "unverified"
    proof = {item["machine"]: {"before_sha256": item["symbol_profile_digest"],
                              "after_sha256": item["symbol_profile_digest"]} for item in (atlas, scout)}
    assert drift.compare(atlas, scout, symbol_profile_evidence=proof)["symbol_profiles_preserved"] is True
    incomplete = deepcopy(proof)
    del incomplete["pc-new"]
    assert drift.compare(atlas, scout, symbol_profile_evidence=incomplete)["symbol_profiles_preserved"] == "unverified"
    changed = deepcopy(proof)
    changed["pc-new"]["before_sha256"] = "f" * 64
    assert drift.compare(atlas, scout, symbol_profile_evidence=changed)["symbol_profiles_preserved"] is False


def test_drift_compare_detects_a_peer_committed_addition_absent_from_reference(repository):
    root, reference = repository
    before = drift.inspect(profile(root), reference)
    write(root, "docs/peer-only.md")
    commit(root, "peer addition")
    after = drift.inspect(profile(root, "Scout", ["BBB"]), reference)
    result = drift.compare(before, after)
    assert result["same_actual_bytes"] is False
    assert "docs/peer-only.md" in result["different_paths"]
