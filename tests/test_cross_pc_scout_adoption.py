"""Scout migration fixtures: local bare Git only; no live profiles or providers."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tools.cross_pc import core, notices
from tools.cross_pc.transport import GitTransport


def profile(root, actor, remote):
    local = root / actor.lower() / "cross-pc"
    return {
        "contract_version": "cross-pc-v2",
        "repository": "jeremysecondstate/ducketz",
        "actor": actor,
        "machine": "pc-new" if actor == "Scout" else "pc-original",
        "branch_prefix": "codex/" + actor.lower() + "/",
        "checkout": str(root / actor.lower() / "application"),
        "symbols": ["SCOUT_FIXTURE" if actor == "Scout" else "ATLAS_FIXTURE"],
        "authorized_producers": ["fixture-development"],
        "ready_root": str(local / "ready"),
        "snapshot_root": str(local / "snapshots"),
        "evidence_root": str(local / "evidence"),
        "state_path": str(local / "receipts.json"),
        "inbox_state": str(local / "inbox-receipts.json"),
        "outgoing_root": str(local / "outgoing"),
        "transport_cache": str(local / "transport.git"),
        "git_lock": str(local / "locks" / "git.lock"),
        "inbox_lock": str(local / "locks" / "inbox.lock"),
        "transport_lock": str(local / "locks" / "transport.lock"),
        "remote": str(remote),
    }


def adoption_notice(change_id="fixture-scout-adoption"):
    return {
        "change_id": change_id,
        "summary": "Synthetic Scout coordination adoption; legacy deliveries retained.",
        "author": "Scout",
        "repository": "jeremysecondstate/ducketz",
        "files": [],
        "tests": "Synthetic offline migration fixture",
        "scope": "coordination adoption",
        "limitations": ["Temporary repositories; no native task or runtime claims"],
        "runtime_implications": "No application deployment or ownership change.",
    }


def legacy_files(root):
    """Five already delivered v1 records in Scout's actual nested layout."""
    git_root = root / "codexstore-git"
    inbox_root = root / "codexstore-monitor"
    messages = {}
    for number in range(1, 6):
        bid = f"20260930T10000{number}Z-{number:032x}"
        ready = git_root / "scout-outbound-ready" / bid
        bundle = ready / "bundle"
        bundle.mkdir(parents=True)
        payloads = {
            "summary.md": b"Historical synthetic Scout status.\r\n",
            "handoff.json": core.encoded({
                "schema_version": 1, "id": bid,
                "sender": "pc-new", "recipient": "pc-original",
                "created_at_utc": "2026-09-30T10:00:00Z",
                "message_type": "status", "purpose": "Synthetic historical fixture",
                "payloads": ["summary.md"],
            }),
        }
        manifest = core.encoded({
            "schema_version": 1, "bundle_id": bid,
            "files": [{"path": name, "bytes": len(raw), "sha256": core.digest(raw)}
                      for name, raw in sorted(payloads.items())],
        })
        for name, raw in {**payloads, "manifest.json": manifest}.items():
            (bundle / name).write_bytes(raw)
        core.atomic(ready / "approval.json", {
            "schema_version": 1, "id": bid, "assistant": "Scout",
            "origin": "local-scout-reviewed", "approved_for_atlas": True,
            "reviewed_at_utc": "2026-09-30T10:00:00Z",
            "manifest_sha256": core.digest(manifest),
        })
        messages[bid] = {
            "stage": "published", "report_pending": False,
            "manifest_sha256": core.digest(manifest),
            "published_at_utc": "2026-09-30T10:01:00Z",
            "reported_at_utc": "2026-09-30T10:02:00Z",
            "temporary_paths": {}, "failure_count": 0,
        }
    core.atomic(git_root / "scout-outbound-receipts.json", {
        "schema_version": 1, "assistant": "Scout", "messages": messages,
    })
    core.atomic(inbox_root / "scout-receipts.json", {
        "schema_version": 1, "machine_id": "pc-new", "peer_machine_id": "pc-original",
        "messages": {}, "consecutive_access_failures": 0,
    })
    return (git_root / "scout-outbound.lock", inbox_root / "scout-receipts.lock")


@contextmanager
def legacy_locks(paths):
    """Exercise existing create-exclusive sentinels without a v1 invocation."""
    descriptors = []
    try:
        for path in paths:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            descriptors.append(descriptor)
            os.write(descriptor, b"synthetic legacy owner; preserve this sentinel\n")
        yield
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def tree_bytes(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def local_transport(monkeypatch, root):
    remote = root / "remote.git"
    core.git(root, "init", "--bare", str(remote))
    calls = []

    def factory(bound_profile):
        calls.append(bound_profile["actor"])
        return GitTransport(Path(bound_profile["transport_cache"]),
                            bound_profile["remote"], bound_profile["actor"],
                            allow_local_remote=True)

    monkeypatch.setattr(notices, "transport", factory)
    return remote, calls


@contextmanager
def lock_in_another_process(path, ready):
    """Use the candidate lock implementation in a separate local Python process."""
    program = (
        "from pathlib import Path\n"
        "import sys\n"
        "from tools.cross_pc.core import exclusive\n"
        "with exclusive(sys.argv[1]):\n"
        "    Path(sys.argv[2]).write_bytes(b'locked')\n"
        "    sys.stdin.readline()\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-B", "-c", program, str(path), str(ready)],
        cwd=Path(__file__).resolve().parents[1],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "offline child did not acquire the fixture lock"
        yield
    finally:
        try:
            _, errors = process.communicate("release\n", timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
        assert process.returncode == 0, errors


def test_scout_v2_adoption_preserves_completed_v1_bundles_and_create_exclusive_locks(tmp_path, monkeypatch):
    remote, _ = local_transport(monkeypatch, tmp_path)
    scout = profile(tmp_path, "Scout", remote)
    atlas = profile(tmp_path, "Atlas", remote)
    legacy_root = tmp_path / "scout" / "legacy"
    old_locks = legacy_files(legacy_root)

    with legacy_locks(old_locks):
        before = tree_bytes(legacy_root)
        for key in ("git_lock", "inbox_lock", "transport_lock"):
            assert Path(scout[key]) not in old_locks
            assert not Path(scout[key]).is_relative_to(legacy_root)

        notice = adoption_notice()
        queued = notices.enqueue(scout, notice, reviewed=True)
        delivered = notices.deliver(scout, queued["id"])
        assert delivered["stage"] == "published"
        assert delivered["delivery"]["branch"] == "codex/scout/coordination-v2"
        assert len(core.state(scout)["notices"]) == 1
        assert core.state(scout)["records"] == {}
        assert not any(item["change_id"].startswith("legacy:")
                       for item in core.state(scout)["notices"].values())

        incoming = notices.inbox_scan(atlas)
        assert len(incoming) == 1
        assert incoming[0]["handoff"]["sender"] == "pc-new"
        assert incoming[0]["handoff"]["recipient"] == "pc-original"
        read = notices.inbox_read(atlas, incoming[0]["id"])
        assert json.loads(read["content"]["notice.json"]) == notice
        notices.inbox_mark(atlas, incoming[0]["id"], incoming[0]["manifest_sha256"],
                           "Synthetic factual adoption handled")
        assert notices.inbox_scan(atlas) == []
        assert core.git_text(remote, "for-each-ref", "--format=%(refname)") == (
            "refs/heads/codex/scout/coordination-v2")
        assert tree_bytes(legacy_root) == before


def test_scout_inbox_and_courier_share_cache_lock_before_transport_construction(tmp_path, monkeypatch):
    remote, transport_calls = local_transport(monkeypatch, tmp_path)
    scout = profile(tmp_path, "Scout", remote)
    atlas = profile(tmp_path, "Atlas", remote)
    queued = notices.enqueue(scout, adoption_notice("fixture-cache-lock"), reviewed=True)
    receipts_before = Path(scout["state_path"]).read_bytes()

    with lock_in_another_process(scout["transport_lock"], tmp_path / "lock-ready"):
        with pytest.raises(OSError):
            notices.inbox_scan(scout)
        with pytest.raises(OSError):
            notices.deliver(scout, queued["id"])
        assert transport_calls == []
        assert not Path(scout["transport_cache"]).exists()
        assert not Path(scout["inbox_state"]).exists()
        assert Path(scout["state_path"]).read_bytes() == receipts_before

    assert notices.deliver(scout, queued["id"])["stage"] == "published"
    assert len(notices.inbox_scan(atlas)) == 1
    assert notices.inbox_scan(scout) == []
    assert transport_calls == ["Scout", "Atlas", "Scout"]
