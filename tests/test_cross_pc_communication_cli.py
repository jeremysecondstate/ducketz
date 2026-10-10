"""Local CLI workflows: synthetic Drive bytes, durable queues and temporary Git."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess

import pytest

from tools.cross_pc import cli, core, drive_signal, incoming, notices, requests


@pytest.fixture
def local(tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("Communication CLI fixtures must not contact external services")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(notices, "transport", forbidden)
    private = tmp_path / "private"
    profile = {"contract_version": "cross-pc-v2", "repository": "jeremysecondstate/ducketz",
               "actor": "Scout", "machine": "pc-new", "branch_prefix": "codex/scout/",
               "symbols": ["SYNTHETIC"], "authorized_producers": ["fixture"],
               "state_path": str(private / "receipts.json"), "git_lock": str(private / "git.lock"),
               "outgoing_root": str(private / "outgoing"),
               "coordination_request_types": sorted(requests.REQUEST_TYPES)}
    profile_path = tmp_path / "profile.json"

    def save(name, value):
        path = tmp_path / name
        core.atomic(path, value)
        return path

    def invoke(action, *args):
        core.atomic(profile_path, profile)
        cli.main(["--profile", str(profile_path), action, *(str(arg) for arg in args)])
        return json.loads(capsys.readouterr().out)

    return profile, private, save, invoke


def binding(actor="Scout", *, file_id=None):
    value = {"actor": actor, "machine": "pc-new" if actor == "Scout" else "pc-original",
             "recipient": "pc-original" if actor == "Scout" else "pc-new",
             "repository": "jeremysecondstate/ducketz", "link_id": "synthetic-own-connection",
             "profile_id": "synthetic-own-profile", "drive_id": "synthetic-drive",
             "parent_id": "synthetic-outbox-" + actor}
    if file_id:
        value["file_id"] = file_id
    return value


def signal_notice():
    return {"id": "20261001T210000Z-" + "1" * 32, "manifest_sha256": "a" * 64,
            "summary": "Synthetic reviewed source notice", "source_commit": "b" * 40}


def metadata(b, raw, file_id="synthetic-own-file"):
    return {"id": file_id, "name": drive_signal.signal_name(b), "mimeType": "application/json",
            "parents": [b["parent_id"]], "driveId": b["drive_id"], "size": str(len(raw))}


@pytest.mark.parametrize("policy", [None, "git_and_drive"])
def test_drive_cli_bootstrap_and_update_recovery_preserve_one_identity(local, policy):
    profile, private, save, invoke = local
    if policy is not None:
        profile["coordination_notification_policy"] = policy
    b = binding()
    profile["drive_own_binding"] = str(save("own-binding.json", b))
    spec = save("signal-spec.json", {"git_head": "c" * 40, "notices": [signal_notice()]})
    assert invoke("drive-prepare", "--spec", spec, "--reviewed")["stage"] == "prepared"
    plan = invoke("drive-plan")
    original_path = Path(plan["local_file"])
    original = original_path.read_bytes()
    action = invoke("drive-begin-write")
    assert action["tool"] == "google_drive_upload_file"
    assert action["arguments"]["parent_folder_id"] == b["parent_id"]
    state = core.load(private / "drive-signal" / "state.json")
    assert state["pending"]["stage"] == "write_pending"
    with pytest.raises(ValueError, match="reconcile first"):
        invoke("drive-begin-write")
    m = save("readback-metadata.json", metadata(b, original))
    assert invoke("drive-bind-created", "--metadata", m)["file_id"] == "synthetic-own-file"
    assert invoke("drive-reconcile", "--metadata", m, "--raw", original_path)["stage"] == "published"
    # A fresh head updates the same file and preserves the old immutable bytes.
    spec = save("signal-spec.json", {"git_head": "d" * 40, "notices": [signal_notice()]})
    invoke("drive-prepare", "--spec", spec, "--reviewed")
    new_plan = invoke("drive-plan")
    new_path = Path(new_plan["local_file"])
    first = invoke("drive-begin-write", "--metadata", m, "--raw", original_path)
    assert first["tool"] == "google_drive_update_file"
    assert first["arguments"]["fileId"] == "synthetic-own-file"
    assert invoke("drive-plan")["next_step"] == "reconcile raw readback"
    assert invoke("drive-reconcile", "--metadata", m, "--raw", original_path)["stage"] == "retry_ready"
    repeated = invoke("drive-begin-write", "--metadata", m, "--raw", original_path)
    assert repeated["arguments"] == first["arguments"]
    m = save("readback-metadata.json", metadata(b, new_path.read_bytes()))
    assert invoke("drive-reconcile", "--metadata", m, "--raw", new_path)["stage"] == "published"
    assert original_path.read_bytes() == original
    assert not Path(profile["state_path"]).exists()


@pytest.mark.parametrize("action,args", [
    ("drive-prepare", ["--spec", "unused", "--reviewed"]), ("drive-plan", []),
    ("drive-begin-write", []), ("drive-bind-created", ["--metadata", "unused"]),
    ("drive-reconcile", ["--metadata", "unused", "--raw", "unused"]),
    ("drive-inspect", ["--metadata", "unused", "--raw", "unused", "--previous", "unused"]),
])
def test_github_only_drive_routes_preserve_history_without_reading_inputs(local, monkeypatch, action, args):
    profile, private, save, invoke = local
    profile["coordination_notification_policy"] = "github_only"
    profile["drive_own_binding"] = "missing-own-binding"
    profile["drive_peer_binding"] = "missing-peer-binding"
    save("private/drive-signal/state.json", {"pending": {"stage": "write_pending", "attempts": 1}})
    save("private/drive-signal/bytes/frozen.json", {"retained": "exact historical bytes"})
    save("private/request-state.json", {"pending": "independent request"})
    save("private/incoming-source-receipts.json", {"pending": "independent source review"})
    before = {str(path.relative_to(private)): path.read_bytes() for path in private.rglob("*") if path.is_file()}

    def forbidden(*args, **kwargs):
        raise AssertionError("Retired Drive input/state must not be read")

    monkeypatch.setattr(cli, "_local_bytes", forbidden)
    monkeypatch.setattr(cli, "_drive_binding", forbidden)
    monkeypatch.setattr(cli, "_drive_root", forbidden)
    result = invoke(action, "--binding", "missing-explicit-binding", *args)
    assert result == {"status": "DISABLED_BY_POLICY", "action": action,
                      "coordination_notification_policy": "github_only",
                      "drive_operation_performed": False, "drive_delivery": "not_attempted",
                      "historical_state_preserved": True}
    assert before == {str(path.relative_to(private)): path.read_bytes() for path in private.rglob("*") if path.is_file()}


@pytest.mark.parametrize("policy", [None, "", "github", True, [], {}])
@pytest.mark.parametrize("action", ["plan", "drive-plan", "request-plan"])
def test_invalid_notification_policy_rejects_before_any_state_mutation(local, policy, action):
    profile, private, _, invoke = local
    profile["coordination_notification_policy"] = policy
    with pytest.raises(ValueError, match="coordination_notification_policy"):
        invoke(action)
    assert not private.exists()


@pytest.mark.parametrize("policy", [None, "git_and_drive", "github_only"])
def test_plan_reports_effective_policy_without_inventing_signal_delivery(local, policy):
    profile, private, _, invoke = local
    profile["ready_root"] = str(private / "ready")
    if policy is not None:
        profile["coordination_notification_policy"] = policy
    result = invoke("plan")
    assert result["coordination_notification_policy"] == (policy or "git_and_drive")
    assert result["drive_signal_required"] == (policy != "github_only")
    assert result["source"] is None and result["delivery"] is None
    assert not (private / "drive-signal").exists()
    assert ("coordination_notification_policy" in profile) == (policy is not None)


@pytest.mark.parametrize("action,args", [
    ("drive-prepare", ["--spec", "unused", "--reviewed"]), ("drive-plan", []),
    ("drive-begin-write", []), ("drive-bind-created", ["--metadata", "unused"]),
    ("drive-reconcile", ["--metadata", "unused", "--raw", "unused"]),
])
def test_drive_writer_rejects_peer_before_state_or_evidence_reads(local, action, args):
    profile, private, save, invoke = local
    b = save("peer-binding.json", binding("Atlas", file_id="synthetic-peer-file"))
    with pytest.raises(ValueError, match="writer must match"):
        invoke(action, "--binding", b, *args)
    assert not private.exists()


def test_drive_inspection_requires_pinned_peer_metadata_and_deduplicates(local, tmp_path):
    profile, private, save, invoke = local
    b = binding("Atlas", file_id="synthetic-peer-file")
    profile["drive_peer_binding"] = str(save("peer-binding.json", b))
    peer_root = tmp_path / "synthetic-peer-fixture"
    drive_signal.prepare(peer_root, b, git_head="c" * 40, notices=[signal_notice()], reviewed=True)
    raw_path = Path(drive_signal.plan(peer_root, b)["local_file"])
    m = save("peer-metadata.json", metadata(b, raw_path.read_bytes(), "synthetic-peer-file"))
    known = save("known-digests.json", {signal_notice()["id"]: signal_notice()["manifest_sha256"]})
    result = invoke("drive-inspect", "--metadata", m, "--raw", raw_path, "--known-digests", known)
    assert result["notices"] == [] and result["requires_git_validation"] is True
    assert not private.exists()
    bad = save("peer-metadata.json", {**metadata(b, raw_path.read_bytes(), "synthetic-peer-file"), "parents": ["wrong-folder"]})
    with pytest.raises(ValueError, match="ancestry"):
        invoke("drive-inspect", "--metadata", bad, "--raw", raw_path)


@pytest.mark.parametrize("b", [binding(), binding(file_id="own-file"), binding("Atlas")])
def test_drive_inspection_rejects_self_or_unpinned_peer(local, b):
    _, private, save, invoke = local
    path = save("bad-binding.json", b)
    with pytest.raises(ValueError, match="pinned peer"):
        invoke("drive-inspect", "--binding", path, "--metadata", "unused", "--raw", "unused")
    assert not private.exists()


def test_drive_private_root_override_and_unsafe_root_rejection(local, tmp_path):
    profile, private, save, invoke = local
    profile["drive_own_binding"] = str(save("own-binding.json", binding()))
    spec = save("signal-spec.json", {"git_head": "c" * 40, "notices": []})
    profile["drive_signal_root"] = str(tmp_path / "outside-private")
    with pytest.raises(ValueError, match="inside the private"):
        invoke("drive-prepare", "--spec", spec, "--reviewed")
    assert not (tmp_path / "outside-private").exists()
    profile["drive_signal_root"] = str(private / "signal-custom")
    invoke("drive-prepare", "--spec", spec, "--reviewed")
    assert (private / "signal-custom" / "state.json").is_file()
    assert not (private / "drive-signal").exists()
    with pytest.raises(ValueError, match="together"):
        invoke("drive-begin-write", "--metadata", spec)


def test_drive_cli_readback_does_not_publish_without_write_intent(local):
    profile, private, save, invoke = local
    b = binding(file_id="synthetic-own-file")
    profile["drive_own_binding"] = str(save("own-binding.json", b))
    spec = save("signal-spec.json", {"git_head": "c" * 40, "notices": []})
    invoke("drive-prepare", "--spec", spec, "--reviewed")
    raw_path = Path(invoke("drive-plan")["local_file"])
    m = save("readback-metadata.json", metadata(b, raw_path.read_bytes()))
    state_path = private / "drive-signal" / "state.json"
    before = state_path.read_bytes()
    with pytest.raises(ValueError, match="durable prior write intent"):
        invoke("drive-reconcile", "--metadata", m, "--raw", raw_path)
    assert state_path.read_bytes() == before
    assert invoke("drive-plan")["stage"] == "prepared"


def request_envelope(identity="request-1", *, outgoing=False, response_to=None):
    result = {"schema_version": 1, "kind": "response" if response_to else "request",
              "request_id": identity, "request_type": "status_evidence",
              "repository": "jeremysecondstate/ducketz", "sender": "pc-new" if outgoing else "pc-original",
              "recipient": "pc-original" if outgoing else "pc-new", "summary": "Synthetic factual status."}
    if response_to:
        result.update(in_reply_to=response_to, result="completed")
    return result


def notice_spec(envelope):
    return {"change_id": "coordination:" + envelope["request_id"], "summary": envelope["summary"],
            "author": "Scout", "repository": "jeremysecondstate/ducketz", "files": [], "tests": [],
            "scope": "Synthetic reviewed local evidence", "limitations": ["Offline fixture"],
            "runtime_implications": "No runtime change", "coordination": envelope}


@pytest.mark.parametrize("policy", ["git_and_drive", "github_only"])
def test_request_cli_register_prepare_enqueue_sync_aliases_and_independent_delivery(local, policy):
    profile, _, save, invoke = local
    profile["coordination_notification_policy"] = policy
    request = save("incoming-envelope.json", request_envelope())
    registered = invoke("request-register", "--notice-id", "validated-peer-notice", "--digest", "a" * 64, "--spec", request)
    assert registered["stage"] == "pending"
    assert invoke("request-plan")["action"] == "handle"
    response = notice_spec(request_envelope("response-1", outgoing=True, response_to="request-1"))
    spec = save("reply-spec.json", response)
    assert invoke("request-prepare", "--id", "request-1", "--spec", spec, "--reviewed")["stage"] == "reply_prepared"
    queued = invoke("request-enqueue", "--id", "request-1")
    again = invoke("request-enqueue-reply", "--id", "request-1")
    assert queued["reply"]["notice_id"] == again["reply"]["notice_id"]
    assert invoke("request-sync", "--id", "request-1")["stage"] == "reply_enqueued"
    state = core.state(profile)
    item = state["notices"][queued["reply"]["notice_id"]]
    item.update(stage="published", delivery={"transport": "github-git-v2", "commit_sha": "b" * 40})
    core.atomic(profile["state_path"], state)
    delivered = invoke("request-sync-reply", "--id", "request-1")
    assert delivered["stage"] == "reply_delivered"
    assert set(delivered["reply"]["deliveries"]) == {"github-git-v2"}
    assert delivered["reply"]["peer_handling"] == "unverified"
    assert invoke("request-plan")["request"] is None
    assert invoke("request-status")["messages"]["request-1"]["stage"] == "reply_delivered"


def test_github_only_notice_delivery_keeps_frozen_drive_transaction(local, monkeypatch):
    profile, private, save, invoke = local
    profile["coordination_notification_policy"] = "github_only"
    profile["transport_cache"] = str(private / "transport.git")
    historical = save("private/drive-signal/state.json", {"pending": {"stage": "write_pending"}})
    original = historical.read_bytes()
    queued = invoke("notice", "--spec", save("notice.json", notice_spec(request_envelope(outgoing=True))), "--reviewed")
    calls = []

    class LocalTransportFixture:
        def prepare(self, directory, **kwargs):
            calls.append(("prepare", str(directory)))
            return {"fixture": "immutable prepared Git transaction"}

        def publish_prepared(self, transaction):
            calls.append(("publish", transaction))
            return {"transport": "github-git-v2", "commit_sha": "b" * 40}

    monkeypatch.setattr(notices, "transport", lambda profile: LocalTransportFixture())
    delivered = invoke("deliver", "--id", queued["id"])
    assert delivered["stage"] == "published"
    assert delivered["delivery"]["transport"] == "github-git-v2"
    assert invoke("deliver", "--id", queued["id"]) == delivered
    assert [call[0] for call in calls] == ["prepare", "publish"]
    assert historical.read_bytes() == original


def test_request_cli_tracks_immutable_notice_then_attaches_response_without_ack(local):
    _, _, save, invoke = local
    envelope = request_envelope("outgoing-1", outgoing=True)
    notice = invoke("notice", "--spec", save("outgoing-notice.json", notice_spec(envelope)), "--reviewed")
    outgoing = save("outgoing-envelope.json", envelope)
    tracked = invoke("request-track", "--id", notice["id"], "--digest", notice["manifest_sha256"], "--spec", outgoing, "--reviewed")
    assert tracked["stage"] == "awaiting_response"
    reply = save("response-envelope.json", request_envelope("peer-response", response_to="outgoing-1"))
    attached = invoke("request-register", "--id", "peer-response-notice", "--digest", "b" * 64, "--spec", reply)
    assert attached["stage"] == "response_attached" and attached["automatic_ack"] is False
    assert invoke("request-status")["outgoing"]["outgoing-1"]["stage"] == "response_received"
    assert invoke("request-plan")["request"] is None


def test_request_cli_rejects_wrong_direction_and_unreviewed_reply(local):
    _, private, save, invoke = local
    spec = save("wrong-direction.json", request_envelope(outgoing=True))
    with pytest.raises(ValueError, match="routing mismatch"):
        invoke("request-register", "--id", "peer-notice", "--digest", "a" * 64, "--spec", spec)
    assert not private.exists()
    with pytest.raises(ValueError, match="review required"):
        invoke("request-prepare-reply", "--id", "request-1", "--spec", spec)


def incoming_spec():
    return {"repository": "jeremysecondstate/ducketz", "peer": "Atlas", "branch": "codex/atlas/fixture-source",
            "commit_sha": "a" * 40, "base_commit": "b" * 40,
            "files": [{"path": "shared.py", "operation": "modify", "sha256": "c" * 64}],
            "dependency_fingerprints": {}, "notice_id": "validated-source-notice", "notice_manifest_sha256": "d" * 64}


def test_incoming_cli_review_role_and_local_remote_escape_guards(local, tmp_path):
    profile, _, save, invoke = local
    remote = tmp_path / "local-remote"
    remote.mkdir()
    profile["remote"] = str(remote)
    spec = incoming_spec()
    path = save("incoming-source.json", spec)
    with pytest.raises(ValueError, match="reviewed local"):
        invoke("incoming-enqueue", "--spec", path)
    wrong = save("wrong-peer-source.json", {**spec, "peer": "Scout"})
    with pytest.raises(ValueError, match="peer identity"):
        invoke("incoming-enqueue", "--spec", wrong, "--reviewed")
    assert invoke("incoming-enqueue", "--spec", path, "--reviewed")["stage"] == "source_fetch_pending"
    result = invoke("incoming-process")
    assert result["stage"] == "blocked" and "not the fixed repository" in result["reason"]
    with pytest.raises(SystemExit):
        invoke("incoming-process", "--allow-local-remote")


def test_incoming_cli_end_to_end_temporary_git_is_retained_for_review_only(local, tmp_path, monkeypatch):
    profile, _, save, invoke = local
    checkout, remote = tmp_path / "author", tmp_path / "remote.git"
    checkout.mkdir()

    def git(cwd, *args):
        return subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "commit.gpgsign=false",
                               "-c", "core.hooksPath=" + str(tmp_path / "no-hooks"), *args],
                              cwd=cwd, capture_output=True, check=True).stdout.decode().strip()

    git(tmp_path, "init", "--bare", str(remote))
    git(checkout, "init")
    git(checkout, "config", "user.name", "Offline fixture")
    git(checkout, "config", "user.email", "offline@example.invalid")
    (checkout / "shared.py").write_bytes(b"VALUE = 1\n")
    git(checkout, "add", "--", "shared.py")
    git(checkout, "commit", "-m", "fixture base")
    base = git(checkout, "rev-parse", "HEAD")
    raw = b"raise RuntimeError('PEER SOURCE MUST NEVER EXECUTE')\n"
    (checkout / "shared.py").write_bytes(raw)
    git(checkout, "add", "--", "shared.py")
    git(checkout, "commit", "-m", "fixture source")
    commit = git(checkout, "rev-parse", "HEAD")
    git(checkout, "push", str(remote), "HEAD:refs/heads/codex/atlas/fixture-source")
    profile["remote"] = str(remote)
    spec = incoming_spec()
    spec.update(commit_sha=commit, base_commit=base)
    spec["files"][0]["sha256"] = core.digest(raw)
    invoke("incoming-enqueue", "--spec", save("source-spec.json", spec), "--reviewed")
    # Enable the internal seam only in this Python fixture; no CLI/profile field
    # or peer specification can opt production processing into a local remote.
    process = incoming.process_one
    monkeypatch.setattr(incoming, "process_one", lambda p: process(p, allow_local_remote=True))
    result = invoke("incoming-process")
    assert result["stage"] == "review_pending" and result["last_completed_stage"] == "tree_verified"
    assert result["evidence"]["owned_bytes_exact"] is True
    assert result["evidence"]["local_tests_run"] is False and result["evidence"]["installed"] is False
    assert git(checkout, "rev-parse", "HEAD") == commit
    assert not (incoming.paths(profile)["cache"] / "shared.py").exists()
    assert invoke("incoming-process") == {"stage": "quiet", "attempted": 0}
