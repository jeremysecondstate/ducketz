"""Offline acceptance tests; every remote is a temporary local bare repository."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from tools.cross_pc.transport import (
    CONTRACT_VERSION, DeliveryPending, GitTransport, StalePrepared, TransportError,
    read_directory, validate_bundle, validate_legacy_bundle,
)


BID = "20261001T120000Z-" + "a" * 32


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def bundle(bid=BID, sender="pc-original", recipient="pc-new", version=2, text=b"Reviewed shared infrastructure.\r\n"):
    handoff = {"schema_version": version, "id": bid, "sender": sender, "recipient": recipient,
               "type": "change_notice", "payloads": ["summary.md"]}
    if version == 2:
        handoff["contract_version"] = CONTRACT_VERSION
    blobs = {"handoff.json": encoded(handoff), "summary.md": text}
    manifest = {"schema_version": version, "bundle_id": bid,
                "files": [{"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                          for name, raw in blobs.items()]}
    if version == 2:
        manifest["contract_version"] = CONTRACT_VERSION
    blobs["manifest.json"] = encoded(manifest)
    return blobs


def remanifest(blobs):
    manifest = json.loads(blobs["manifest.json"])
    manifest["files"] = [{"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                         for name, raw in blobs.items() if name != "manifest.json"]
    blobs["manifest.json"] = encoded(manifest)
    return blobs


@pytest.fixture
def pair(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], capture_output=True, check=True)
    atlas = GitTransport(tmp_path / "atlas.git", str(remote), "Atlas", allow_local_remote=True)
    scout = GitTransport(tmp_path / "scout.git", str(remote), "Scout", allow_local_remote=True)
    return atlas, scout


def publish(atlas, bid=BID, blobs=None):
    transaction = atlas.prepare_bytes(bid, blobs or bundle(bid))
    return atlas.publish_prepared(transaction)


def raw_commit(transport, paths, parent=None, branch=None):
    """Deliberately forge malformed peer trees to test the trust boundary."""
    index = transport.cache / "test-index"
    env = {"GIT_INDEX_FILE": str(index), "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
    transport._git("read-tree", parent or "--empty", env_extra=env)
    rows = []
    for name, value in paths.items():
        if value is None:
            transport._git("update-index", "--force-remove", name, env_extra=env)
            continue
        identity = transport._git("hash-object", "-w", "--stdin", data=value).strip()
        rows.append(b"100644 " + identity + b"\t" + name.encode() + b"\0")
    transport._git("update-index", "-z", "--index-info", data=b"".join(rows), env_extra=env)
    tree = transport._git("write-tree", env_extra=env).decode().strip()
    args = ["commit-tree", tree] + (["-p", parent] if parent else [])
    commit = transport._git(*args, data=b"malformed fixture\n", env_extra=env).decode().strip()
    transport._git("push", "origin", f"{commit}:refs/heads/{branch or transport.branch}")
    index.unlink(missing_ok=True)
    return commit


def test_two_pc_exchange_exact_original_bytes_and_legacy_digests(pair):
    atlas, scout = pair
    blobs = bundle(version=1, text=b"Original BOM-free CRLF bytes.\r\n")
    receipt = publish(atlas, blobs=blobs)
    incoming = scout.scan("Atlas")
    assert len(incoming) == 1
    assert incoming[0]["blobs"] == blobs
    assert incoming[0]["manifest_sha256"] == hashlib.sha256(blobs["manifest.json"]).hexdigest()
    assert incoming[0]["provenance"]["source_transport"] == "legacy-drive-api"
    assert "does not cryptographically prove" in incoming[0]["trust_context"]
    assert receipt["stage"] == "published"
    assert receipt["verified_remote_sha"] == receipt["commit_sha"]
    assert scout.scan("Atlas", {BID: receipt["manifest_sha256"]}) == []
    reply_id = BID.replace("a" * 32, "b" * 32)
    scout.publish_prepared(scout.prepare_bytes(reply_id, bundle(reply_id, "pc-new", "pc-original")))
    assert atlas.scan("Scout")[0]["handoff"]["sender"] == "pc-new"


def test_idempotent_retry_and_same_id_mutation_rejected(pair):
    atlas, scout = pair
    first = publish(atlas)
    second = publish(atlas)
    assert first["commit_sha"] == second["commit_sha"]
    assert second["recovered"] is True
    with pytest.raises(TransportError, match="different bytes"):
        publish(atlas, blobs=bundle(text=b"mutated"))
    assert len(scout.scan("Atlas")) == 1


def test_wrong_actor_cannot_publish_peer_transaction(pair):
    atlas, scout = pair
    transaction = atlas.prepare_bytes(BID, bundle())
    with pytest.raises(TransportError, match="ownership"):
        scout.publish_prepared(transaction)
    assert scout._head(atlas.branch) is None


def test_offline_or_missing_peer_returns_no_placeholder(pair):
    atlas, scout = pair
    assert scout.scan("Atlas") == []
    assert atlas.scan("Scout") == []


@pytest.mark.parametrize("mutation", ["sender", "recipient", "id", "boolean_schema", "contract", "missing", "extra", "size", "digest", "self", "duplicate", "payload", "json_duplicate", "nan", "non_utf8"])
def test_malformed_bundles_rejected(mutation):
    blobs = bundle()
    handoff = json.loads(blobs["handoff.json"])
    manifest = json.loads(blobs["manifest.json"])
    if mutation in {"sender", "recipient", "id"}:
        handoff[mutation] = "other"
        blobs["handoff.json"] = encoded(handoff)
        remanifest(blobs)
    elif mutation == "boolean_schema":
        manifest["schema_version"] = True
        blobs["manifest.json"] = encoded(manifest)
    elif mutation == "contract":
        manifest["contract_version"] = "future"
        blobs["manifest.json"] = encoded(manifest)
    elif mutation == "missing":
        del blobs["summary.md"]
    elif mutation == "extra":
        blobs["extra.md"] = b"unlisted"
    elif mutation in {"size", "digest"}:
        manifest["files"][0]["bytes" if mutation == "size" else "sha256"] = -1 if mutation == "size" else "0" * 64
        blobs["manifest.json"] = encoded(manifest)
    elif mutation == "self":
        manifest["files"].append({"path": "manifest.json", "bytes": 1, "sha256": "0" * 64})
        blobs["manifest.json"] = encoded(manifest)
    elif mutation == "duplicate":
        manifest["files"].append(manifest["files"][0])
        blobs["manifest.json"] = encoded(manifest)
    elif mutation == "payload":
        handoff["payloads"] = ["summary.md", "summary.md"]
        blobs["handoff.json"] = encoded(handoff)
        remanifest(blobs)
    elif mutation == "json_duplicate":
        blobs["handoff.json"] = blobs["handoff.json"].replace(b'"schema_version": 2', b'"schema_version": 2, "schema_version": 2')
        remanifest(blobs)
    elif mutation == "nan":
        handoff["number"] = float("nan")
        blobs["handoff.json"] = encoded(handoff)
        remanifest(blobs)
    else:
        blobs["summary.md"] = b"\xff"
        remanifest(blobs)
    with pytest.raises(TransportError):
        validate_bundle(BID, blobs, "pc-original", "pc-new")


@pytest.mark.parametrize("path", ["../bad.md", "/abs.md", "C:/bad.md", "bad\\file.md", "a//b.md", "CON.md", "x./file.md", "bad\x00.md", "bad.exe", "SUMMARY.md"])
def test_unsafe_paths_rejected(path):
    blobs = bundle()
    blobs[path] = b"unexpected"
    with pytest.raises(TransportError):
        validate_bundle(BID, blobs, "pc-original", "pc-new")


def test_legacy_validator_preserves_schema_and_digest():
    value = bundle(version=1)
    assert validate_legacy_bundle(BID, value, "pc-original", "pc-new")["blobs"] == value
    with pytest.raises(TransportError, match="original v1"):
        validate_legacy_bundle(BID, bundle(), "pc-original", "pc-new")


def test_snapshot_publication_and_unsupported_local_remote(tmp_path, pair):
    atlas, scout = pair
    directory = tmp_path / BID
    directory.mkdir()
    original = bundle()
    for name, raw in original.items():
        (directory / name).write_bytes(raw)
    transaction = atlas.prepare(directory)
    (directory / "summary.md").write_bytes(b"changed after prepare")
    atlas.publish_prepared(transaction)
    assert scout.scan("Atlas")[0]["blobs"] == original
    with pytest.raises(TransportError, match="GitHub"):
        GitTransport(tmp_path / "other.git", atlas.remote, "Atlas")


def test_ambiguous_push_recovers_without_repeat(pair, monkeypatch):
    atlas, scout = pair
    transaction = atlas.prepare_bytes(BID, bundle())
    original = atlas._git
    pushes = []
    def disconnected(*args, **kwargs):
        if args[0] == "push":
            pushes.append(args)
            original(*args, **kwargs)
            raise TransportError("simulated connection dropped after accepted push")
        return original(*args, **kwargs)
    monkeypatch.setattr(atlas, "_git", disconnected)
    receipt = atlas.publish_prepared(transaction)
    assert receipt["stage"] == "published"
    assert receipt["recovered"] is True
    assert len(pushes) == 1
    assert atlas.publish_prepared(transaction)["recovered"] is True
    assert len(pushes) == 1
    assert scout.scan("Atlas")[0]["manifest_sha256"] == receipt["manifest_sha256"]


def test_failed_push_stays_pending_and_can_retry(pair, monkeypatch):
    atlas, scout = pair
    transaction = atlas.prepare_bytes(BID, bundle())
    original = atlas._git
    def failed(*args, **kwargs):
        if args[0] == "push":
            raise TransportError("offline")
        return original(*args, **kwargs)
    monkeypatch.setattr(atlas, "_git", failed)
    with pytest.raises(DeliveryPending):
        atlas.publish_prepared(transaction)
    assert scout.scan("Atlas") == []
    monkeypatch.setattr(atlas, "_git", original)
    assert atlas.publish_prepared(transaction)["stage"] == "published"


def test_remote_verification_failure_does_not_claim_publication(pair, monkeypatch):
    atlas, _ = pair
    transaction = atlas.prepare_bytes(BID, bundle())
    original = atlas._git
    pushed = False
    def uncertain(*args, **kwargs):
        nonlocal pushed
        if pushed and args[0] == "ls-remote":
            raise TransportError("offline verification")
        result = original(*args, **kwargs)
        if args[0] == "push":
            pushed = True
        return result
    monkeypatch.setattr(atlas, "_git", uncertain)
    with pytest.raises(DeliveryPending, match="verification"):
        atlas.publish_prepared(transaction)
    monkeypatch.setattr(atlas, "_git", original)
    assert atlas.publish_prepared(transaction)["recovered"] is True


def test_receipt_digest_mismatch_rejected(pair):
    atlas, scout = pair
    publish(atlas)
    with pytest.raises(TransportError, match="digest changed"):
        scout.scan("Atlas", {BID: "0" * 64})


def test_partial_tree_and_application_source_rejected(pair):
    atlas, scout = pair
    raw_commit(atlas, {f"bundles/{BID}/manifest.json": b"{}", "app/runtime.py": b"unwanted source"})
    with pytest.raises(TransportError, match="unexpected source|provenance"):
        scout.scan("Atlas")


def test_missing_provenance_is_not_complete_inventory(pair):
    atlas, scout = pair
    raw_commit(atlas, {f"bundles/{BID}/{name}": raw for name, raw in bundle().items()})
    with pytest.raises(TransportError, match="provenance"):
        scout.scan("Atlas")


def test_immutable_existing_bundle_edit_rejected(pair):
    atlas, scout = pair
    receipt = publish(atlas)
    scout.scan("Atlas")
    raw_commit(atlas, {f"bundles/{BID}/summary.md": b"changed after publication"}, parent=receipt["commit_sha"])
    with pytest.raises(TransportError, match="immutable bundle"):
        scout.scan("Atlas")


def test_new_reader_rejects_wrong_manifest_hash_in_remote_blob(pair):
    atlas, scout = pair
    receipt = publish(atlas)
    raw_commit(atlas, {f"bundles/{BID}/summary.md": b"changed after publication"}, parent=receipt["commit_sha"])
    with pytest.raises(TransportError, match="SHA-256"):
        scout.scan("Atlas")


def test_scan_bounded_and_continues_from_dedup_receipts(pair):
    atlas, scout = pair
    for index in range(11):
        bid = "20261001T120000Z-" + f"{index:032x}"
        publish(atlas, bid)
    first = scout.scan("Atlas")
    assert len(first) == 10
    second = scout.scan("Atlas", {item["id"]: item["manifest_sha256"] for item in first})
    assert len(second) == 1
    assert second[0]["id"].endswith("a")
    for limit in (0, 11, True):
        with pytest.raises(TransportError, match="limit"):
            scout.scan("Atlas", limit=limit)


def test_complete_inventory_output_bound_fails_closed(pair, monkeypatch):
    atlas, scout = pair
    publish(atlas)
    original = scout._git
    def truncated(*args, **kwargs):
        value = original(*args, **kwargs)
        if args[0] == "ls-tree":
            return value[:-1]
        return value
    monkeypatch.setattr(scout, "_git", truncated)
    with pytest.raises(TransportError, match="incomplete"):
        scout.scan("Atlas")


def test_concurrent_prepare_never_force_pushes_or_overwrites(pair):
    atlas, scout = pair
    first = atlas.prepare_bytes(BID, bundle())
    second_id = "20261001T120001Z-" + "b" * 32
    second = atlas.prepare_bytes(second_id, bundle(second_id))
    atlas.publish_prepared(first)
    with pytest.raises(StalePrepared):
        atlas.publish_prepared(second)
    assert len(scout.scan("Atlas")) == 1
    # Caller can prepare a fresh immutable commit atop the newer remote tree.
    replacement = atlas.reprepare(second)
    assert replacement["parent_sha"] == first["commit_sha"]
    assert replacement["supersedes_commit_sha"] == second["commit_sha"]
    atlas.publish_prepared(replacement)
    assert len(scout.scan("Atlas")) == 2


def test_bare_cache_identity_and_remote_validation(pair, tmp_path):
    atlas, _ = pair
    with pytest.raises(TransportError, match="identity mismatch"):
        GitTransport(atlas.cache, "https://github.com/other/repo.git", "Atlas", repository="other/repo")
    for remote in ("https://token@github.com/jeremysecondstate/ducketz.git", "https://example.com/repo", "--upload-pack=bad"):
        with pytest.raises(TransportError, match="GitHub"):
            GitTransport(tmp_path / "rejected", remote, "Atlas")


def test_prepared_transaction_cannot_modify_older_published_bytes(pair):
    atlas, scout = pair
    first = publish(atlas)
    scout.scan("Atlas")
    next_id = "20261001T120001Z-" + "b" * 32
    transaction = atlas.prepare_bytes(next_id, bundle(next_id))
    # Construct a local malicious child without publishing it. A regular push
    # would accept its ancestry, so the adapter must compare complete trees.
    index = atlas.cache / "tamper-index"
    env = {"GIT_INDEX_FILE": str(index), "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
           "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "fixture@example.invalid"}
    atlas._git("read-tree", transaction["commit_sha"], env_extra=env)
    oid = atlas._git("hash-object", "-w", "--stdin", data=b"replace existing notice").strip()
    row = b"100644 " + oid + b"\t" + f"bundles/{BID}/summary.md".encode() + b"\0"
    atlas._git("update-index", "-z", "--index-info", data=row, env_extra=env)
    tree = atlas._git("write-tree", env_extra=env).decode().strip()
    transaction["commit_sha"] = atlas._git("commit-tree", tree, "-p", first["commit_sha"], data=b"malformed\n", env_extra=env).decode().strip()
    with pytest.raises(TransportError, match="changes published immutable bytes"):
        atlas.publish_prepared(transaction)
    assert scout.read_bundle("Atlas", BID)["blobs"] == bundle()
    index.unlink(missing_ok=True)


def test_reprepare_uses_frozen_bytes_and_preserves_legacy_provenance(pair, tmp_path):
    atlas, scout = pair
    first = atlas.prepare_bytes(BID, bundle())
    next_id = "20261001T120001Z-" + "b" * 32
    directory = tmp_path / next_id
    directory.mkdir()
    original = bundle(next_id, version=1)
    for name, raw in original.items():
        (directory / name).write_bytes(raw)
    stale = atlas.prepare(directory, source_transport="legacy-filesystem")
    (directory / "summary.md").write_bytes(b"changed after original review")
    atlas.publish_prepared(first)
    with pytest.raises(StalePrepared):
        atlas.publish_prepared(stale)
    replacement = atlas.reprepare(stale)
    atlas.publish_prepared(replacement)
    fetched = scout.read_bundle("Atlas", next_id)
    assert fetched["blobs"] == original
    assert fetched["provenance"]["source_transport"] == "legacy-filesystem"
    assert fetched["manifest_sha256"] == stale["manifest_sha256"]


def test_recovered_receipt_reports_actual_remote_commit_after_branch_advances(pair):
    atlas, _ = pair
    first = atlas.prepare_bytes(BID, bundle())
    atlas.publish_prepared(first)
    next_id = "20261001T120001Z-" + "b" * 32
    second = publish(atlas, next_id)
    recovered = atlas.publish_prepared(first)
    assert recovered["commit_sha"] == second["commit_sha"]
    assert recovered["verified_remote_sha"] == second["commit_sha"]
    assert recovered["prepared_commit_sha"] == first["commit_sha"]
    assert recovered["recovered"] is True


def test_symlink_outgoing_rejected_when_supported(tmp_path):
    directory = tmp_path / BID
    directory.mkdir()
    target = tmp_path / "outside.md"
    target.write_bytes(b"outside")
    try:
        (directory / "summary.md").symlink_to(target)
    except OSError:
        pytest.skip("Windows symlink creation is unavailable")
    with pytest.raises(TransportError, match="symlink"):
        read_directory(directory)
