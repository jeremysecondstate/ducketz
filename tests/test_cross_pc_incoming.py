"""Offline source intake acceptance: every source remote is a temporary bare repo."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from tools.cross_pc import incoming
from tools.cross_pc.core import atomic, load


def git(root, *args, data=None):
    result = subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.hooksPath=" + str(root / "no-hooks"),
                             *args], cwd=root, input=data, capture_output=True, check=True)
    return result.stdout.decode().strip()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


@pytest.fixture
def source(tmp_path):
    remote, checkout = tmp_path / "remote.git", tmp_path / "author"
    checkout.mkdir()
    git(tmp_path, "init", "--bare", str(remote))
    git(checkout, "init")
    git(checkout, "config", "user.email", "offline@example.invalid")
    git(checkout, "config", "user.name", "offline fixture")
    for name, raw in {"shared.py": b"VALUE = 1\n", "dependency.py": b"POLICY = 7\n", "obsolete.py": b"OLD = 1\n"}.items():
        (checkout / name).write_bytes(raw)
    git(checkout, "add", "--", "shared.py", "dependency.py", "obsolete.py")
    git(checkout, "commit", "-m", "offline base")
    base = git(checkout, "rev-parse", "HEAD")
    (checkout / "shared.py").write_bytes(b"VALUE = 2\n")
    # A malicious received script is inspected as bytes. No import/test execution
    # is authorized by source intake, even when such a file is advertised.
    (checkout / "new.py").write_bytes(b"raise RuntimeError('MUST NEVER EXECUTE PEER CODE')\n")
    (checkout / "obsolete.py").unlink()
    git(checkout, "add", "--", "shared.py", "new.py", "obsolete.py")
    git(checkout, "commit", "-m", "offline source")
    commit = git(checkout, "rev-parse", "HEAD")
    branch = "codex/atlas/offline-source"
    git(checkout, "push", str(remote), "HEAD:refs/heads/" + branch)
    root = tmp_path / "scratch" / "cross-pc"
    profile = {"contract_version": "cross-pc-v2", "repository": incoming.REPOSITORY,
               "actor": "Scout", "machine": "pc-new", "branch_prefix": "codex/scout/",
               "symbols": ["COST", "IONQ"], "authorized_producers": [],
               "state_path": str(root / "receipts.json"), "remote": str(remote)}
    spec = {"repository": incoming.REPOSITORY, "peer": "Atlas", "branch": branch,
            "commit_sha": commit, "base_commit": base,
            "files": [{"path": "new.py", "operation": "add", "sha256": sha((checkout / "new.py").read_bytes())},
                      {"path": "shared.py", "operation": "modify", "sha256": sha((checkout / "shared.py").read_bytes())},
                      {"path": "obsolete.py", "operation": "delete", "sha256": None}],
            "dependency_fingerprints": {"dependency.py": sha((checkout / "dependency.py").read_bytes())},
            "notice_id": "offline-notice-1", "notice_manifest_sha256": "a" * 64}
    return profile, spec, checkout, remote


def receive(source):
    profile, spec, *_ = source
    incoming.enqueue(profile, spec, reviewed=True)
    return incoming.process_one(profile, allow_local_remote=True)


def test_exact_source_has_pinned_ref_verified_tree_and_durable_pending_review(source):
    profile, spec, checkout, _ = source
    before_head = git(checkout, "rev-parse", "HEAD")
    profile_before = copy.deepcopy(profile)
    result = receive(source)
    assert result["stage"] == "review_pending", result
    assert result["last_completed_stage"] == "tree_verified"
    assert result["evidence"]["owned_bytes_exact"] is True
    assert result["evidence"]["dependency_tested_bytes_exact"] is True
    assert result["evidence"]["local_tests_run"] is False
    assert result["evidence"]["installed"] is False
    assert set(result["evidence"]["files"]) == {"shared.py", "new.py", "obsolete.py"}
    cache = incoming.paths(profile)["cache"]
    assert git(cache, "rev-parse", result["received_ref"]) == spec["commit_sha"]
    assert git(cache, "rev-parse", "--is-bare-repository") == "true"
    assert not (cache / "new.py").exists()
    assert git(checkout, "rev-parse", "HEAD") == before_head
    assert profile == profile_before
    assert load(incoming.paths(profile)["state"])["records"][result["id"]] == result
    assert incoming.process_one(profile, allow_local_remote=True) == {"stage": "quiet", "attempted": 0}


def test_duplicate_notice_and_cross_transport_provenance_do_not_refetch(source, monkeypatch):
    profile, spec, *_ = source
    result = receive(source)
    repeated = incoming.enqueue(profile, spec, reviewed=True)
    assert repeated == result
    other = copy.deepcopy(spec)
    other["notice_id"], other["notice_manifest_sha256"] = "drive-notice-2", "b" * 64
    repeated = incoming.enqueue(profile, other, reviewed=True)
    assert len(repeated["provenance"]) == 2
    monkeypatch.setattr(incoming.SourceCache, "fetch", lambda *_: pytest.fail("duplicate refetched"))
    assert incoming.process_one(profile, allow_local_remote=True)["stage"] == "quiet"
    assert repeated["attempts"] == 1


def test_changed_evidence_for_same_source_is_retained_as_visible_conflict(source):
    profile, spec, *_ = source
    result = receive(source)
    conflicting = copy.deepcopy(spec)
    conflicting["files"][0]["sha256"] = "b" * 64
    blocked = incoming.enqueue(profile, conflicting, reviewed=True)
    assert blocked["stage"] == "blocked"
    assert blocked["retryable"] is False
    assert blocked["last_completed_stage"] == "tree_verified"
    assert blocked["specification"] == result["specification"]
    assert blocked["conflicts"][0]["specification"]["files"][0]["sha256"] == "b" * 64
    assert incoming.enqueue(profile, conflicting, reviewed=True)["conflicts"] == blocked["conflicts"]


def test_mutated_notice_identity_is_a_conflict_even_when_source_evidence_matches(source):
    profile, spec, *_ = source
    incoming.enqueue(profile, spec, reviewed=True)
    spec["notice_manifest_sha256"] = "b" * 64
    result = incoming.enqueue(profile, spec, reviewed=True)
    assert result["stage"] == "blocked"
    assert result["provenance"][0]["notice_manifest_sha256"] == "a" * 64


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(repository="attacker/foreign"),
    lambda s: s.update(peer="Scout"),
    lambda s: s.update(branch="codex/scout/wrong-owner"),
    lambda s: s.update(branch="codex/atlas/coordination-v2"),
    lambda s: s.update(branch="codex/atlas/../../evil"),
    lambda s: s.update(commit_sha="HEAD"),
    lambda s: s.update(test_command="python peer.py"),
    lambda s: s["files"][0].update(path="../outside.py"),
    lambda s: s["files"][0].update(path="configs/strategy.local.json"),
    lambda s: s["files"][0].update(path="datafetching/watchlist.local.txt"),
    lambda s: s["files"][0].update(path=":(glob)*"),
    lambda s: s["files"][0].update(path="folder/*"),
    lambda s: s["dependency_fingerprints"].update({"SHARED.py": "a" * 64}),
])
def test_untrusted_identity_paths_and_commands_rejected_before_fetch(source, mutate):
    profile, spec, *_ = source
    mutate(spec)
    with pytest.raises(incoming.IncomingError):
        incoming.enqueue(profile, spec, reviewed=True)
    assert not incoming.paths(profile)["cache"].exists()


def test_requires_local_review_and_explicit_local_remote_test_seam(source):
    profile, spec, *_ = source
    with pytest.raises(incoming.IncomingError, match="reviewed"):
        incoming.enqueue(profile, spec)
    incoming.enqueue(profile, spec, reviewed=True)
    profile["allow_local_remote"] = True
    result = incoming.process_one(profile)
    assert result["stage"] == "blocked"
    assert "fixed repository" in result["reason"]
    assert not incoming.paths(profile)["cache"].exists()


def test_foreign_repository_remote_rejected_without_contact(source):
    profile, spec, *_ = source
    profile["remote"] = "https://github.com/attacker/foreign.git"
    incoming.enqueue(profile, spec, reviewed=True)
    result = incoming.process_one(profile)
    assert result["stage"] == "blocked"
    assert "fixed repository" in result["reason"]
    assert not incoming.paths(profile)["cache"].exists()


def test_branch_readback_cannot_substitute_another_branch_or_newer_tip(source):
    profile, spec, checkout, remote = source
    spec["branch"] = "codex/atlas/unadvertised-source"
    missing = receive(source)
    assert missing["stage"] == "blocked"
    assert "branch does not resolve" in missing["reason"]
    assert missing["last_completed_stage"] == "notice_validated"
    # A same-actor branch containing only the old base is still not the exact
    # advertised source. This verifies remote branch identity independently of
    # the source commit being available elsewhere in the repository.
    git(checkout, "push", str(remote), spec["base_commit"] + ":refs/heads/" + spec["branch"])
    storage = incoming.paths(profile)
    receipts = load(storage["state"])
    receipts["records"][missing["id"]].update(retryable=True)
    atomic(storage["state"], receipts)
    wrong = incoming.process_one(profile, allow_local_remote=True)
    assert wrong["stage"] == "blocked"
    assert "branch does not resolve" in wrong["reason"]


@pytest.mark.parametrize("kind", ["undeclared_path", "wrong_operation", "raw_hash", "wrong_base"])
def test_exact_full_diff_and_raw_owned_hashes_are_required(source, kind):
    _, spec, *_ = source
    if kind == "undeclared_path":
        spec["files"] = spec["files"][:-1]
    elif kind == "wrong_operation":
        spec["files"][0]["operation"] = "modify"
    elif kind == "raw_hash":
        spec["files"][0]["sha256"] = "b" * 64
    else:
        spec["base_commit"] = "b" * 40
    result = receive(source)
    assert result["stage"] == "blocked"
    assert result["last_completed_stage"] == "fetched"
    assert result["retryable"] is False
    assert result["received_ref"].endswith(spec["commit_sha"])


def test_crlf_working_byte_evidence_is_explicitly_different_from_exact_blob(source):
    _, spec, *_ = source
    spec["dependency_fingerprints"]["dependency.py"] = sha(b"POLICY = 7\r\n")
    result = receive(source)
    assert result["stage"] == "review_pending", result
    dependency = result["evidence"]["dependencies"]["dependency.py"]
    assert dependency["comparison"] == "line_endings_only"
    assert dependency["committed_sha256"] == sha(b"POLICY = 7\n")
    assert dependency["claimed_tested_working_sha256"] == sha(b"POLICY = 7\r\n")
    assert result["evidence"]["dependency_tested_bytes_exact"] is False
    assert result["evidence"]["local_tests_run"] is False


def test_semantic_dependency_mismatch_blocks_and_preserves_both_hashes(source):
    _, spec, *_ = source
    spec["dependency_fingerprints"]["dependency.py"] = sha(b"POLICY = 8\n")
    result = receive(source)
    assert result["stage"] == "blocked"
    dependency = result["evidence"]["dependencies"]["dependency.py"]
    assert dependency["comparison"] == "mismatch"
    assert dependency["committed_sha256"] != dependency["claimed_tested_working_sha256"]


def test_ambiguous_fetch_is_retained_then_exact_ref_readback_recovers(source, monkeypatch):
    profile, spec, *_ = source
    original = incoming.SourceCache.fetch

    def interrupted(self, value):
        original(self, value)
        raise incoming.FetchPending("simulated interruption after durable ref")

    monkeypatch.setattr(incoming.SourceCache, "fetch", interrupted)
    result = receive(source)
    assert result["stage"] == "blocked" and result["retryable"] is True
    assert result["specification"]["commit_sha"] == spec["commit_sha"]
    monkeypatch.setattr(incoming.SourceCache, "fetch", original)
    result = incoming.process_one(profile, allow_local_remote=True)
    assert result["stage"] == "review_pending", result
    assert result["attempts"] == 2


def test_a_wake_advances_only_one_source_and_rotates_blocked_backlog(source, monkeypatch):
    profile, spec, *_ = source
    first = incoming.enqueue(profile, spec, reviewed=True)
    another = copy.deepcopy(spec)
    another["commit_sha"] = "b" * 40
    second = incoming.enqueue(profile, another, reviewed=True)
    calls = []

    def unavailable(self, value):
        calls.append(value["commit_sha"])
        raise incoming.FetchPending("simulated unavailable source")

    monkeypatch.setattr(incoming.SourceCache, "fetch", unavailable)
    a = incoming.process_one(profile, allow_local_remote=True)
    assert len(calls) == 1
    b = incoming.process_one(profile, allow_local_remote=True)
    assert len(calls) == 2 and a["id"] != b["id"]
    assert {a["id"], b["id"]} == {first["id"], second["id"]}
    assert a["stage"] == b["stage"] == "blocked"


def test_foreign_cache_remote_or_received_ref_mutation_is_rejected(source):
    profile, spec, *_ = source
    result = receive(source)
    cache, state = incoming.paths(profile)["cache"], incoming.paths(profile)["state"]
    git(cache, "update-ref", result["received_ref"], spec["base_commit"])
    receipts = load(state)
    receipts["records"][result["id"]].update(stage="fetched", retryable=True)
    atomic(state, receipts)
    blocked = incoming.process_one(profile, allow_local_remote=True)
    assert blocked["stage"] == "blocked"
    assert "ref identity changed" in blocked["reason"]
    git(cache, "config", "remote.origin.url", "https://github.com/attacker/foreign.git")
    receipts = load(state)
    receipts["records"][result["id"]].update(retryable=True)
    atomic(state, receipts)
    blocked = incoming.process_one(profile, allow_local_remote=True)
    assert "remote identity changed" in blocked["reason"]


def test_foreign_receipt_identity_cannot_be_reused(source):
    profile, spec, *_ = source
    incoming.enqueue(profile, spec, reviewed=True)
    storage = incoming.paths(profile)
    receipts = load(storage["state"])
    receipts["actor"] = "Atlas"
    atomic(storage["state"], receipts)
    with pytest.raises(incoming.IncomingError, match="receipt identity changed"):
        incoming.process_one(profile, allow_local_remote=True)


def test_git_symlink_blob_is_rejected_without_creating_a_filesystem_link(source):
    profile, spec, checkout, remote = source
    # Forge an index symlink using Git plumbing; Windows symlink privilege is
    # unnecessary, so this safety test runs rather than skipping on Windows.
    identity = git(checkout, "hash-object", "-w", "--stdin", data=b"../private-file")
    git(checkout, "update-index", "--add", "--cacheinfo", "120000," + identity + ",new.py")
    git(checkout, "commit", "--amend", "--no-edit")
    spec["commit_sha"] = git(checkout, "rev-parse", "HEAD")
    spec["files"][0]["sha256"] = sha(b"../private-file")
    git(checkout, "push", "--force", str(remote), "HEAD:refs/heads/" + spec["branch"])
    result = receive(source)
    assert result["stage"] == "blocked"
    assert "symlinks/submodules" in result["reason"]
    assert not (incoming.paths(profile)["cache"] / "new.py").exists()


def test_private_storage_cannot_escape_receipt_root_or_reuse_nonempty_directory(source):
    profile, spec, checkout, _ = source
    profile["source_cache"] = str(checkout)
    with pytest.raises(incoming.IncomingError, match="local receipt directory"):
        incoming.enqueue(profile, spec, reviewed=True)
    del profile["source_cache"]
    cache = incoming.paths(profile)["cache"]
    cache.mkdir(parents=True)
    (cache / "unrelated.txt").write_text("preserve this")
    result = receive(source)
    assert result["stage"] == "blocked"
    assert "unrecognized nonempty" in result["reason"]
    assert (cache / "unrelated.txt").read_text() == "preserve this"


def test_blob_byte_bounds_leave_source_pinned_for_review(source, monkeypatch):
    monkeypatch.setattr(incoming, "MAX_BLOB_BYTES", 2)
    result = receive(source)
    assert result["stage"] == "blocked"
    assert "blob size" in result["reason"]
    assert result["last_completed_stage"] == "fetched"
