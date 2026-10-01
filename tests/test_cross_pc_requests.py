"""Synthetic local request routing; no Drive, Git process, broker or native API."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from tools.cross_pc import core, notices, requests


@pytest.fixture
def profile(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Request routing must not execute commands or contact a transport")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(notices, "transport", forbidden)
    base = tmp_path / "private"
    return {"contract_version": "cross-pc-v2", "repository": "jeremysecondstate/ducketz",
            "actor": "Scout", "machine": "pc-new", "branch_prefix": "codex/scout/",
            "symbols": ["SYNTHETIC"], "authorized_producers": ["fixture"],
            "state_path": str(base / "receipts.json"), "git_lock": str(base / "git.lock"),
            "outgoing_root": str(base / "outgoing"),
            "coordination_request_types": sorted(requests.REQUEST_TYPES)}


def envelope(identity="request-1", *, outgoing=False, kind="request", request_type="status_evidence", **extra):
    value = {"schema_version": 1, "kind": kind, "request_id": identity, "request_type": request_type,
             "repository": "jeremysecondstate/ducketz", "sender": "pc-new" if outgoing else "pc-original",
             "recipient": "pc-original" if outgoing else "pc-new", "summary": "Synthetic factual request."}
    value.update(extra)
    return value


def notice(profile, data):
    return {"change_id": "coordination:" + data["request_id"], "summary": data["summary"],
            "author": profile["actor"], "repository": profile["repository"], "files": [], "tests": [],
            "scope": "Synthetic local factual coordination only.", "limitations": ["Offline fixture"],
            "runtime_implications": "No application or runtime change.", "coordination": data}


def incoming(profile, identity="request-1", **extra):
    value = envelope(identity, **extra)
    return requests.register(profile, "bundle-" + identity, core.digest(core.encoded(value)), value)


def reply(profile, identity="request-1", **extra):
    data = envelope("response-" + identity, outgoing=True, kind="response", in_reply_to=identity,
                    result="completed", summary="Synthetic observed status; no runtime change.", **extra)
    return notice(profile, data)


def outgoing(profile, identity="local-request-1", **extra):
    data = envelope(identity, outgoing=True, **extra)
    item = notices.enqueue(profile, notice(profile, data), reviewed=True)
    return data, item


def test_registration_and_reporting_are_independent_and_duplicate_is_exact_noop(profile):
    publication = core.new_state(profile)
    publication["records"]["existing-source"] = {"source_stage": "pushed", "commit_sha": "a" * 40}
    core.atomic(profile["state_path"], publication)
    inbox = Path(profile["state_path"]).parent / "inbox.json"
    core.atomic(inbox, {"bundles": {"bundle-request-1": {"reported": True}}})
    source_before, inbox_before = Path(profile["state_path"]).read_bytes(), inbox.read_bytes()
    result = incoming(profile)
    path, lock = requests.paths(profile)
    before = path.read_bytes()
    repeated = incoming(profile)
    assert result["stage"] == "pending" and repeated["duplicate"]
    assert path.read_bytes() == before
    assert Path(profile["state_path"]).read_bytes() == source_before
    assert inbox.read_bytes() == inbox_before
    assert path.parent == Path(profile["state_path"]).parent and path != Path(profile["state_path"])
    assert lock.name == "request-state.lock"


def test_duplicate_envelope_on_another_transport_notice_keeps_one_request(profile):
    data = envelope()
    requests.register(profile, "git-bundle", "a" * 64, data)
    duplicate = requests.register(profile, "drive-bundle", "b" * 64, data)
    state = requests.read(profile)
    assert duplicate["duplicate"]
    assert len(state["messages"]) == 1 and len(state["messages"]["request-1"]["notice_refs"]) == 2


@pytest.mark.parametrize("change", ["notice_digest", "envelope"])
def test_conflicting_identity_preserves_original_and_blocks_dispatch(profile, change):
    data = envelope()
    requests.register(profile, "bundle", "a" * 64, data)
    original = requests.read(profile)["messages"]["request-1"]["envelope"]
    modified = deepcopy(data)
    if change == "envelope":
        modified["summary"] = "Changed peer payload must not replace the first request."
    result = requests.register(profile, "bundle" if change == "notice_digest" else "other-bundle", "b" * 64, modified)
    assert result["stage"] == "needs_review"
    state = requests.read(profile)
    assert state["messages"]["request-1"]["envelope"] == original
    assert len(state["conflicts"]) == 1
    assert requests.plan(profile)["request"] is None
    assert incoming(profile)["stage"] == "needs_review"


def test_one_item_fair_selection_does_not_execute_or_repeat_only_first(profile):
    incoming(profile, "a-request")
    incoming(profile, "b-request")
    sequence = [requests.plan(profile) for _ in range(4)]
    assert [item["request_id"] for item in sequence] == ["a-request", "b-request", "a-request", "b-request"]
    assert all(item["action"] == "handle" for item in sequence)
    assert all("never a command" in item["authority_boundary"] for item in sequence)
    assert all(item["automatic_ack"] is False for item in sequence)


@pytest.mark.parametrize("extra", [
    {"request_type": "place_order"}, {"kind": "execute"},
    {"command": "arbitrary-peer-command"},
])
def test_unsupported_requests_are_retained_without_dispatch(profile, extra):
    data = envelope(**extra)
    result = requests.register(profile, "bundle-unsupported", "c" * 64, data)
    assert result["stage"] == "needs_review"
    assert requests.read(profile)["messages"]["request-1"]["envelope"] == data
    assert requests.plan(profile)["request"] is None
    with pytest.raises(ValueError, match="remains held|No unambiguous"):
        requests.prepare_reply(profile, "request-1", reply(profile), reviewed=True)


def test_changed_notice_cannot_hide_integrity_failure_by_changing_request_id(profile):
    requests.register(profile, "same-notice", "a" * 64, envelope("original"))
    result = requests.register(profile, "same-notice", "b" * 64, envelope("replacement"))
    assert result["stage"] == "needs_review"
    state = requests.read(profile)
    assert set(state["messages"]) == {"original"}
    assert state["messages"]["original"]["integrity_conflicts"]
    assert requests.plan(profile)["request"] is None


def test_standing_scope_must_be_local_and_can_be_revoked(profile):
    no_scope = {**profile, "coordination_request_types": []}
    assert incoming(no_scope)["stage"] == "needs_review"
    incoming(profile, "authorized-request")
    requests.prepare_reply(profile, "authorized-request", reply(profile, "authorized-request"), reviewed=True)
    with pytest.raises(ValueError, match="no longer belongs"):
        requests.enqueue_reply(no_scope, "authorized-request")
    assert requests.plan(no_scope)["request"] is None


@pytest.mark.parametrize("patch", [
    {"request_type": "source_review"}, {"request_type": "source_review", "source_commit": "main"},
    {"repository": "unrelated/repository"}, {"recipient": "pc-original"},
    {"request_id": "../unsafe"}, {"summary": "x" * 4001},
    {"unrecognized_field": float("nan")},
])
def test_invalid_routing_or_unpinned_source_is_rejected_before_state_write(profile, patch):
    data = envelope()
    data.update(patch)
    with pytest.raises(ValueError):
        requests.register(profile, "bundle", "a" * 64, data)
    assert not requests.paths(profile)[0].exists()


def test_prepared_reply_freezes_reviewed_bytes_and_requires_source_binding(profile):
    incoming(profile, request_type="source_review", source_commit="a" * 40)
    wrong = reply(profile, request_type="source_review", source_commit="b" * 40)
    with pytest.raises(ValueError, match="binding mismatch"):
        requests.prepare_reply(profile, "request-1", wrong, reviewed=True)
    good = reply(profile, request_type="source_review", source_commit="a" * 40)
    with pytest.raises(ValueError, match="review required"):
        requests.prepare_reply(profile, "request-1", good)
    prepared = requests.prepare_reply(profile, "request-1", good, reviewed=True)
    assert prepared["stage"] == "reply_prepared"
    assert requests.plan(profile)["action"] == "enqueue_reply"
    changed = deepcopy(good)
    changed["summary"] = "Later edit cannot replace the prepared response."
    with pytest.raises(ValueError, match="original evidence preserved"):
        requests.prepare_reply(profile, "request-1", changed, reviewed=True)
    assert requests.prepare_reply(profile, "request-1", good, reviewed=True) == prepared


def test_crash_after_notice_enqueue_resumes_same_immutable_notice(profile, monkeypatch):
    incoming(profile)
    requests.prepare_reply(profile, "request-1", reply(profile), reviewed=True)
    real_atomic = core.atomic
    request_path = requests.paths(profile)[0]

    def interrupted(path, value):
        if Path(path) == request_path and value["messages"]["request-1"]["stage"] == "reply_enqueued":
            raise OSError("synthetic interruption after durable notice enqueue")
        return real_atomic(path, value)

    monkeypatch.setattr(core, "atomic", interrupted)
    with pytest.raises(OSError, match="synthetic interruption"):
        requests.enqueue_reply(profile, "request-1")
    published_state = core.state(profile)
    notice_id = next(iter(published_state["notices"]))
    assert requests.read(profile)["messages"]["request-1"]["stage"] == "reply_prepared"
    monkeypatch.setattr(core, "atomic", real_atomic)
    resumed = requests.enqueue_reply(profile, "request-1")
    assert resumed["reply"]["notice_id"] == notice_id
    assert len(core.state(profile)["notices"]) == 1
    assert requests.enqueue_reply(profile, "request-1") == resumed


def test_transport_receipt_reconciliation_keeps_publication_and_peer_handling_distinct(profile):
    incoming(profile)
    requests.prepare_reply(profile, "request-1", reply(profile), reviewed=True)
    queued = requests.enqueue_reply(profile, "request-1")
    assert requests.sync_reply(profile, "request-1")["stage"] == "reply_enqueued"
    local = core.state(profile)
    item = local["notices"][queued["reply"]["notice_id"]]
    item.update(stage="published", delivery={"transport": "github-git-v2", "stage": "published",
                                           "verified_remote_sha": "d" * 40})
    core.atomic(profile["state_path"], local)
    published_before = Path(profile["state_path"]).read_bytes()
    done = requests.sync_reply(profile, "request-1")
    assert done["stage"] == "reply_delivered"
    assert set(done["reply"]["deliveries"]) == {"github-git-v2"}
    assert done["reply"]["peer_handling"] == "unverified"
    assert Path(profile["state_path"]).read_bytes() == published_before
    assert requests.plan(profile)["request"] is None
    assert requests.sync_reply(profile, "request-1") == done
    item["delivery"]["verified_remote_sha"] = "e" * 40
    core.atomic(profile["state_path"], local)
    with pytest.raises(ValueError, match="receipt changed"):
        requests.sync_reply(profile, "request-1")


@pytest.mark.parametrize("response_first", [False, True])
def test_response_attaches_to_own_request_without_automatic_ack(profile, response_first):
    request, published = outgoing(profile)
    response = envelope("peer-response", kind="response", in_reply_to=request["request_id"], result="completed")
    if response_first:
        assert requests.register(profile, "peer-bundle", "f" * 64, response)["stage"] == "needs_review"
    requests.track_outgoing(profile, published["id"], published["manifest_sha256"], request, reviewed=True)
    result = requests.register(profile, "peer-bundle", "f" * 64, response)
    assert result["stage"] == "response_attached" and result["automatic_ack"] is False
    state = requests.read(profile)
    assert state["outgoing"][request["request_id"]]["stage"] == "response_received"
    assert state["outgoing"][request["request_id"]]["responses"] == ["peer-response"]
    assert requests.plan(profile)["request"] is None
    assert len(core.state(profile)["notices"]) == 1


def test_wrong_response_type_and_fabricated_outgoing_notice_stay_unaccepted(profile):
    request, published = outgoing(profile)
    with pytest.raises(ValueError, match="reviewed request"):
        requests.track_outgoing(profile, published["id"], published["manifest_sha256"], request)
    with pytest.raises(ValueError, match="immutable local notice"):
        requests.track_outgoing(profile, published["id"], "0" * 64, request, reviewed=True)
    requests.track_outgoing(profile, published["id"], published["manifest_sha256"], request, reviewed=True)
    response = envelope("peer-response", kind="response", request_type="workflow_question",
                        in_reply_to=request["request_id"], result="completed")
    assert requests.register(profile, "peer-bundle", "f" * 64, response)["stage"] == "needs_review"
    assert requests.read(profile)["outgoing"][request["request_id"]]["stage"] == "awaiting_response"


def test_malformed_journal_is_preserved_and_linked_profile_base_rejected(profile, monkeypatch):
    path, _ = requests.paths(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"schema_version": 1, "schema_version": 2}')
    before = path.read_bytes()
    with pytest.raises(ValueError, match="duplicate JSON key"):
        incoming(profile)
    assert path.read_bytes() == before
    original = Path.lstat

    def reparse(candidate):
        actual = original(candidate)
        if candidate == path.parent:
            return SimpleNamespace(st_mode=actual.st_mode, st_file_attributes=1024)
        return actual

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(ValueError, match="symlink/reparse"):
        requests.plan(profile)


def test_request_lock_contention_preserves_journal(profile):
    incoming(profile)
    path, lock = requests.paths(profile)
    before = path.read_bytes()
    with core.exclusive(lock):
        with pytest.raises(OSError):
            requests.plan(profile)
    assert path.read_bytes() == before
