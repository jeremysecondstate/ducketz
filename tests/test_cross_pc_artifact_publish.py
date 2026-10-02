"""Offline artifact-publication tests; only temporary local Git remotes are used."""
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tools.cross_pc import artifact_publish as publisher


def git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)
    return result.stdout.decode("utf-8").strip()


def remote_blob(remote, name):
    return subprocess.run(["git", "--git-dir", str(remote), "show", "main:" + name],
                          capture_output=True, check=True).stdout


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "application"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Offline Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    (root / "README.md").write_text("fixture\n")
    (root / ".env").write_text("API_KEY=VERY_PRIVATE_FIXTURE_TOKEN_12345\nMODE=paper\n")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "fixture main")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "origin", "main")
    worktree = tmp_path / "isolated"
    git(root, "worktree", "add", "--detach", str(worktree), "HEAD")
    local = tmp_path / "local-state"
    profile = {
        "contract_version": "cross-pc-v2", "repository": "jeremysecondstate/ducketz",
        "actor": "Atlas", "machine": "pc-original", "branch_prefix": "codex/atlas/",
        "symbols": ["AAPL", "NVDA"], "authorized_producers": ["overnight-gameplan"],
        "checkout": str(root), "evidence_root": str(local / "evidence"),
        "git_lock": str(local / "git.lock"), "test_remote": True,
    }
    return root, worktree, remote, profile


def output(root, name, raw):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    old = time.time() - 120
    os.utime(target, (old, old))
    return target


def spec(root, name, *, id="20261002T080000Z-run00001", operation="add", compression="none", supersedes=None):
    item = {"path": name, "operation": operation, "compression": compression, "owned": True}
    if operation != "delete":
        item["expected_sha256"] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    if supersedes is not None:
        item["supersedes"] = supersedes
    return {
        "producer": "overnight-gameplan", "reviewed": True, "completed": True,
        "completion_id": id, "scope": "symbol-specific", "symbols": ["AAPL"],
        "summary": "Capture the completed Atlas Gameplan evidence",
        "details": ["Preserved the reviewed AAPL Gameplan result for cross-PC inspection."],
        "completed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "paths": [item],
    }


def test_exact_main_publication_is_namespaced_and_idempotent(site):
    root, worktree, remote, profile = site
    name = "artifacts/analysis/overnight-20261001/gameplan.json"
    raw = b'{"symbol":"AAPL","result":"reviewed"}\r\n'
    output(root, name, raw)
    before = (git(root, "rev-parse", "HEAD"), git(root, "status", "--porcelain"), (root / ".git/index").read_bytes())
    reviewed_spec = spec(root, name)
    record = publisher.snapshot(profile, root, reviewed_spec)
    result = publisher.publish(profile, record["id"], worktree)
    destination = f'artifacts/pc-original/{record["id"]}/analysis/overnight-20261001/gameplan.json'
    manifest = f'artifacts/pc-original/{record["id"]}/artifact-manifest.json'
    assert result["stage"] == "pushed"
    assert result["remote_sha"] == git(worktree, "ls-remote", "origin", "refs/heads/main").split()[0]
    assert remote_blob(remote, destination) == raw
    assert json.loads(remote_blob(remote, manifest))["scope"] == "symbol-specific"
    assert set(git(worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", result["commit_sha"]).splitlines()) == {destination, manifest}
    message = git(worktree, "log", "-1", "--format=%B")
    assert "Machine: pc-original" in message and "Symbols: AAPL" in message
    assert "VERY_PRIVATE_FIXTURE_TOKEN_12345" not in message
    assert before == (git(root, "rev-parse", "HEAD"), git(root, "status", "--porcelain"), (root / ".git/index").read_bytes())
    assert publisher.snapshot(profile, root, reviewed_spec)["id"] == record["id"]
    assert publisher.publish(profile, record["id"], worktree)["commit_sha"] == result["commit_sha"]
    output(root, name, b'{"symbol":"AAPL","result":"next run"}\n')
    assert publisher.publish(profile, record["id"], worktree)["commit_sha"] == result["commit_sha"]


@pytest.mark.parametrize("raw", [b"token=VERY_PRIVATE_FIXTURE_TOKEN_12345\n",
                                    b"\x00\xffVERY_PRIVATE_FIXTURE_TOKEN_12345\x00"])
def test_exact_env_values_block_text_and_binary_without_echo(site, raw):
    root, _, _, profile = site
    name = "artifacts/analysis/overnight-20261001/private.bin"
    output(root, name, raw)
    with pytest.raises(publisher.ArtifactPublishError) as error:
        publisher.snapshot(profile, root, spec(root, name))
    assert "VERY_PRIVATE_FIXTURE_TOKEN_12345" not in str(error.value)
    assert "exact local .env value" in str(error.value)


def test_malformed_env_and_active_writes_fail_closed(site):
    root, worktree, _, profile = site
    name = "artifacts/analysis/overnight-20261001/report.json"
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("stable report\n")
    with pytest.raises(publisher.ArtifactPublishError, match="active-write guard"):
        publisher.snapshot(profile, root, spec(root, name))
    old = time.time() - 120
    os.utime(target, (old, old))
    (root / ".env").write_text("API_KEY=unterminated\nnot-an-assignment\n")
    with pytest.raises(publisher.ArtifactPublishError, match="malformed"):
        publisher.snapshot(profile, root, spec(root, name))
    (root / ".env").write_text("API_KEY=VERY_PRIVATE_FIXTURE_TOKEN_12345\n")
    receipt = publisher.snapshot(profile, root, spec(root, name))
    target.write_text("later writer\n")
    with pytest.raises(publisher.ArtifactPublishError, match="active-write guard|changed after snapshot"):
        publisher.publish(profile, receipt["id"], worktree)


def test_reviewed_inventory_hash_blocks_stable_but_different_bytes(site):
    root, _, _, profile = site
    name = "artifacts/analysis/overnight-20261001/inventory-bound.json"
    output(root, name, b'{"reviewed":1}\n')
    sealed_spec = spec(root, name)
    output(root, name, b'{"reviewed":2}\n')
    with pytest.raises(publisher.ArtifactPublishError, match="reviewed inventory hash"):
        publisher.snapshot(profile, root, sealed_spec)


def test_credential_named_artifact_is_never_staged(site):
    root, _, _, profile = site
    name = "artifacts/analysis/overnight-20261001/supervision-token.txt"
    output(root, name, b"synthetic marker\n")
    with pytest.raises(publisher.ArtifactPublishError, match="credential or private path"):
        publisher.snapshot(profile, root, spec(root, name))


def test_deterministic_gzip_round_trip_and_original_hash(site):
    root, worktree, remote, profile = site
    name = "artifacts/analysis/overnight-20261001/holdings.sqlite3"
    raw = b"SQLite format 3\x00" + (b"synthetic account-free fixture\x00" * 10000)
    output(root, name, raw)
    record = publisher.snapshot(profile, root, spec(root, name, compression="gzip"))
    item = record["files"][0]
    compressed = Path(record["snapshot_root"]) / "compressed" / (name + ".gz")
    assert gzip.decompress(compressed.read_bytes()) == raw
    assert item["sha256"] == hashlib.sha256(raw).hexdigest()
    assert item["published_sha256"] == hashlib.sha256(compressed.read_bytes()).hexdigest()
    again = publisher.snapshot(profile, root, spec(root, name, id="20261002T081000Z-run00002", compression="gzip"))
    again_compressed = Path(again["snapshot_root"]) / "compressed" / (name + ".gz")
    assert compressed.read_bytes() == again_compressed.read_bytes()
    assert publisher.publish(profile, record["id"], worktree)["stage"] == "pushed"
    assert gzip.decompress(remote_blob(remote, item["destination"])) == raw
    assert Path(record["snapshot_root"]).joinpath("artifact-manifest.json").is_file()


def test_main_advance_is_fetched_before_artifact_commit(site):
    root, worktree, remote, profile = site
    name = "artifacts/analysis/overnight-20261001/plan.txt"
    output(root, name, b"Atlas AAPL plan\n")
    receipt = publisher.snapshot(profile, root, spec(root, name))
    (root / "README.md").write_text("peer change\n")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "Peer shared change")
    git(root, "push", "origin", "main")
    peer_sha = git(root, "rev-parse", "HEAD")
    published = publisher.publish(profile, receipt["id"], worktree)
    assert git(worktree, "rev-parse", published["commit_sha"] + "^") == peer_sha
    assert published["remote_sha"] == git(worktree, "ls-remote", "origin", "refs/heads/main").split()[0]


def test_failed_push_keeps_exact_committed_receipt_for_retry(site, monkeypatch):
    root, worktree, remote, profile = site
    name = "artifacts/analysis/overnight-20261001/retry.json"
    output(root, name, b'{"retry":"same bytes"}\n')
    record = publisher.snapshot(profile, root, spec(root, name))
    original_git = publisher._git

    def fail_push(worktree_arg, *args, **kwargs):
        if args and args[0] == "push":
            return subprocess.CompletedProcess(["git", *args], 1, b"", b"fixture outage")
        return original_git(worktree_arg, *args, **kwargs)

    monkeypatch.setattr(publisher, "_git", fail_push)
    with pytest.raises(publisher.ArtifactPublishError, match="committed receipt remains pending"):
        publisher.publish(profile, record["id"], worktree)
    receipt_path = publisher._receipt_path(profile, record["id"])
    pending = json.loads(receipt_path.read_text())
    assert pending["stage"] == "committed"
    monkeypatch.setattr(publisher, "_git", original_git)
    result = publisher.publish(profile, record["id"], worktree)
    assert result["stage"] == "pushed" and result["commit_sha"] == pending["commit_sha"]
    assert remote_blob(remote, result["files"][0]["destination"]) == b'{"retry":"same bytes"}\n'


def test_revision_requires_exact_prior_receipt_and_deduplicates_same_bytes(site):
    root, worktree, remote, profile = site
    name = "artifacts/analysis/overnight-20261001/correctable.json"
    output(root, name, b'{"value":1}\n')
    first = publisher.snapshot(profile, root, spec(root, name))
    first_published = publisher.publish(profile, first["id"], worktree)
    duplicate = publisher.snapshot(profile, root, spec(root, name, id="20261002T081000Z-run00002"))
    no_op = publisher.publish(profile, duplicate["id"], worktree)
    assert no_op["stage"] == "deduplicated"
    assert git(worktree, "ls-remote", "origin", "refs/heads/main").split()[0] == first_published["commit_sha"]
    output(root, name, b'{"value":2}\n')
    unreviewed = publisher.snapshot(profile, root, spec(root, name, id="20261002T082000Z-run00003"))
    with pytest.raises(publisher.ArtifactPublishError, match="use exact revision"):
        publisher.publish(profile, unreviewed["id"], worktree)
    prior = first["files"][0]
    link = {"receipt_id": first["id"], "destination": prior["destination"],
            "published_sha256": prior["published_sha256"]}
    revised = publisher.snapshot(profile, root, spec(root, name, id="20261002T083000Z-run00004",
                                                   operation="revision", supersedes=link))
    result = publisher.publish(profile, revised["id"], worktree)
    assert result["stage"] == "pushed"
    assert remote_blob(remote, prior["destination"]) == b'{"value":1}\n'
    assert remote_blob(remote, revised["files"][0]["destination"]) == b'{"value":2}\n'
    bad_link = {**link, "destination": prior["destination"].replace("pc-original", "pc-new")}
    with pytest.raises(publisher.ArtifactPublishError, match="peer artifact namespace"):
        publisher.snapshot(profile, root, spec(root, name, id="20261002T084000Z-run00005",
                                               operation="revision", supersedes=bad_link))


def test_absolute_installed_style_cli_invocation(site, tmp_path):
    root, _, _, profile = site
    name = "artifacts/analysis/overnight-20261001/cli-result.json"
    output(root, name, b'{"result":"complete"}\n')
    private_profile = tmp_path / "profile.json"
    private_spec = tmp_path / "spec.json"
    private_profile.write_text(json.dumps(profile))
    private_spec.write_text(json.dumps(spec(root, name)))
    cli = Path(__file__).resolve().parents[1] / "tools" / "cross_pc" / "cli.py"
    result = subprocess.run([sys.executable, "-B", str(cli), "--profile", str(private_profile),
                             "artifact-snapshot", "--source", str(root), "--spec", str(private_spec)],
                            cwd=tmp_path, capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"id": "20261002T080000Z-run00001", "stage": "snapshotted", "files": 1}
