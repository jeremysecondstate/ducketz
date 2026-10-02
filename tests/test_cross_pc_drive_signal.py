"""Synthetic local signal recovery fixtures; no connectors, mounts, or network."""
import json

import pytest

from tools.cross_pc import drive_signal as ds
from tools.cross_pc.core import digest, encoded


def binding(actor="Scout"):
    machine = "pc-new" if actor == "Scout" else "pc-original"
    return {"actor": actor, "machine": machine,
            "recipient": "pc-original" if actor == "Scout" else "pc-new",
            "repository": "jeremysecondstate/ducketz", "link_id": "synthetic-link-" + actor,
            "profile_id": "synthetic-profile-" + actor, "drive_id": "synthetic-drive",
            "parent_id": "synthetic-own-outbox-" + actor}


def notice(number=1):
    return {"id": f"20261001T210000Z-{number:032x}", "manifest_sha256": str(number) * 64,
            "summary": "Synthetic reviewed notice", "source_commit": "a" * 40}


def frozen(root, b):
    return ds.Path(ds.plan(root, b)["local_file"]).read_bytes()


def metadata(b, raw, identity="synthetic-signal"):
    # These are precisely the required fields; connector checksum/version/trash
    # omission does not become invented false or an invented content digest.
    return {"id": identity, "name": ds.signal_name(b), "mimeType": "application/json",
            "parents": [b["parent_id"]], "driveId": b["drive_id"], "size": str(len(raw))}


def publish_first(root, b):
    ds.prepare(root, b, git_head="b" * 40, notices=[notice()], reviewed=True)
    raw = frozen(root, b)
    action = ds.begin_write(root, b)
    assert action["tool"] == "google_drive_upload_file"
    m = metadata(b, raw)
    ds.bind_created(root, b, m)
    result = ds.reconcile(root, b, m, raw)
    assert result["stage"] == "published"
    return raw


@pytest.mark.parametrize("actor", ["Scout", "Atlas"])
def test_bootstrap_writeahead_readback_and_exact_private_scope(tmp_path, actor):
    b = binding(actor)
    ds.prepare(tmp_path, b, git_head="b" * 40, notices=[notice()], reviewed=True)
    raw = frozen(tmp_path, b)
    action = ds.begin_write(tmp_path, b)
    assert json.loads((tmp_path / "state.json").read_text())["pending"]["stage"] == "write_pending"
    assert action["arguments"]["parent_folder_id"] == b["parent_id"]
    assert action["arguments"]["link_id"] == b["link_id"]
    assert ds.Path(action["arguments"]["file_uri"]).is_absolute()
    for private in (b["link_id"], b["profile_id"], b["parent_id"], b["drive_id"], str(tmp_path)):
        assert private.encode() not in raw
    assert "unresolved" in ds.plan(tmp_path, b)["next_step"]
    ds.bind_created(tmp_path, b, metadata(b, raw))
    assert ds.reconcile(tmp_path, b, metadata(b, raw), raw)["stage"] == "published"
    assert ds.prepare(tmp_path, b, git_head="b" * 40, notices=[notice()], reviewed=True)["stage"] == "published"


def test_ambiguous_create_never_retries_or_replaces_frozen_bytes(tmp_path):
    b = binding()
    ds.prepare(tmp_path, b, git_head="b" * 40, notices=[notice()], reviewed=True)
    raw = frozen(tmp_path, b)
    ds.begin_write(tmp_path, b)
    before = (tmp_path / "state.json").read_bytes()
    with pytest.raises(ValueError, match="reconcile first"):
        ds.begin_write(tmp_path, b)
    with pytest.raises(ValueError, match="unresolved"):
        ds.prepare(tmp_path, b, git_head="c" * 40, notices=[notice(2)], reviewed=True)
    with pytest.raises(ValueError, match="unknown bootstrap"):
        ds.reconcile(tmp_path, b, metadata(b, raw), raw)
    assert (tmp_path / "state.json").read_bytes() == before
    assert frozen(tmp_path, b) == raw


def test_update_recovers_lost_response_without_duplicate_write(tmp_path):
    b = binding()
    old = publish_first(tmp_path, b)
    ds.prepare(tmp_path, b, git_head="c" * 40, notices=[notice(2)], reviewed=True)
    new = frozen(tmp_path, b)
    action = ds.begin_write(tmp_path, b, metadata=metadata(b, old), raw=old)
    assert action["tool"] == "google_drive_update_file"
    assert action["arguments"]["fileId"] == "synthetic-signal"
    assert "parent_folder_id" not in action["arguments"]
    assert ds.plan(tmp_path, b)["next_step"] == "reconcile raw readback"
    result = ds.reconcile(tmp_path, b, metadata(b, new), new)
    assert result["stage"] == "published" and result["attempts"] == 1
    with pytest.raises(ValueError, match="already published"):
        ds.begin_write(tmp_path, b, metadata=metadata(b, old), raw=old)


def test_update_unknown_outcome_readback_then_same_bytes_retry(tmp_path):
    b = binding()
    old = publish_first(tmp_path, b)
    ds.prepare(tmp_path, b, git_head="c" * 40, notices=[notice(2)], reviewed=True)
    new = frozen(tmp_path, b)
    first = ds.begin_write(tmp_path, b, metadata=metadata(b, old), raw=old)
    with pytest.raises(ValueError, match="reconcile first"):
        ds.begin_write(tmp_path, b, metadata=metadata(b, old), raw=old)
    assert ds.reconcile(tmp_path, b, metadata(b, old), old)["stage"] == "retry_ready"
    second = ds.begin_write(tmp_path, b, metadata=metadata(b, old), raw=old)
    assert first["arguments"] == second["arguments"]
    assert first["expected_sha256"] == second["expected_sha256"] == digest(new)
    result = ds.reconcile(tmp_path, b, metadata(b, new), new)
    assert result["attempts"] == 2 and result["stage"] == "published"


def test_unknown_remote_mutation_preserves_pending(tmp_path):
    b = binding()
    old = publish_first(tmp_path, b)
    ds.prepare(tmp_path, b, git_head="c" * 40, notices=[notice(2)], reviewed=True)
    ds.begin_write(tmp_path, b, metadata=metadata(b, old), raw=old)
    before = (tmp_path / "state.json").read_bytes()
    unknown = old + b" "
    with pytest.raises(ValueError, match="unexpected remote"):
        ds.reconcile(tmp_path, b, metadata(b, unknown), unknown)
    assert (tmp_path / "state.json").read_bytes() == before


@pytest.mark.parametrize("field,value", [
    ("id", "foreign-file"), ("name", "atlas-coordination-signal-v1.json"),
    ("parents", ["peer-outbox"]), ("driveId", "foreign-drive"),
    ("mimeType", "application/vnd.google-apps.document"), ("size", "65537"),
    ("size", True), ("trashed", True), ("size", None),
])
def test_exact_metadata_binding_required(field, value):
    b = binding()
    b["file_id"] = "synthetic-signal"
    m = metadata(b, b"{}")
    m[field] = value
    with pytest.raises(ValueError):
        ds.validate_metadata(m, b, raw=b"{}")


def test_normalized_metadata_and_absent_optional_provider_fields():
    b = binding()
    m = metadata(b, b"{}")
    for raw, normal in [("name", "title"), ("mimeType", "mime_type"), ("driveId", "drive_id"), ("parents", "parent_ids")]:
        m[normal] = m.pop(raw)
    assert ds.validate_metadata(m, b, raw=b"{}") == "synthetic-signal"
    m["name"] = "conflicting.json"
    with pytest.raises(ValueError, match="conflicting"):
        ds.validate_metadata(m, b)


def test_cannot_repin_id_or_rebind_account(tmp_path):
    b = binding()
    ds.prepare(tmp_path, b, git_head="b" * 40, notices=[notice()], reviewed=True)
    raw = frozen(tmp_path, b)
    ds.begin_write(tmp_path, b)
    ds.bind_created(tmp_path, b, metadata(b, raw))
    before = (tmp_path / "state.json").read_bytes()
    with pytest.raises(ValueError, match="pinned"):
        ds.bind_created(tmp_path, b, metadata(b, raw, "different-file"))
    other = {**b, "link_id": "other-account"}
    with pytest.raises(ValueError, match="different signal state"):
        ds.plan(tmp_path, other)
    assert (tmp_path / "state.json").read_bytes() == before


def test_dedup_git_notice_and_reject_digest_conflict(tmp_path):
    b = binding()
    raw = publish_first(tmp_path, b)
    assert len(ds.inspect_signal(raw, b)["notices"]) == 1
    known = {notice()["id"]: notice()["manifest_sha256"]}
    result = ds.inspect_signal(raw, b, known_digests=known)
    assert result["notices"] == [] and result["requires_git_validation"]
    assert result["complete_history"] == "Git coordination branch"
    with pytest.raises(ValueError, match="cross-transport"):
        ds.inspect_signal(raw, b, known_digests={notice()["id"]: "f" * 64})


def test_sequence_change_rollback_and_skipped_signals(tmp_path):
    b = binding()
    first = publish_first(tmp_path, b)
    previous = ds.inspect_signal(first, b)
    assert ds.inspect_signal(first, b, previous=previous)["sequence"] == 1
    value = json.loads(first)
    value["git_head"] = "c" * 40
    with pytest.raises(ValueError, match="same signal sequence"):
        ds.inspect_signal(encoded(value), b, previous=previous)
    value.update(sequence=2, previous_signal_sha256="0" * 64)
    with pytest.raises(ValueError, match="history mismatch"):
        ds.inspect_signal(encoded(value), b, previous=previous)
    value.update(sequence=3, previous_signal_sha256="d" * 64)
    later = ds.inspect_signal(encoded(value), b, previous=previous)
    assert later["sequence"] == 3
    with pytest.raises(ValueError, match="rollback"):
        ds.inspect_signal(first, b, previous=later)


def test_signal_size_json_and_arbitrary_command_fields_rejected(tmp_path):
    b = binding()
    raw = publish_first(tmp_path, b)
    with pytest.raises(ValueError, match="64 KiB"):
        ds.inspect_signal(b" " * 65537, b)
    with pytest.raises(ValueError, match="duplicate JSON"):
        ds.inspect_signal(b'{"schema_version":1,"schema_version":1}', b)
    value = json.loads(raw)
    value["command"] = "anything"
    with pytest.raises(ValueError, match="signal fields"):
        ds.inspect_signal(encoded(value), b)
    value.pop("command")
    value["notices"] = [notice()] * 11
    with pytest.raises(ValueError, match="too many"):
        ds.inspect_signal(encoded(value), b)


def test_review_direction_and_frozen_mutation_gates(tmp_path):
    b = binding()
    with pytest.raises(ValueError, match="review required"):
        ds.prepare(tmp_path, b, git_head="b" * 40, notices=[notice()])
    wrong = {**b, "recipient": b["machine"]}
    with pytest.raises(ValueError, match="direction"):
        ds.prepare(tmp_path, wrong, git_head="b" * 40, notices=[notice()], reviewed=True)
    ds.prepare(tmp_path, b, git_head="b" * 40, notices=[notice()], reviewed=True)
    path = ds.Path(ds.plan(tmp_path, b)["local_file"])
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="frozen signal bytes changed"):
        ds.begin_write(tmp_path, b)


def test_malformed_state_is_preserved(tmp_path):
    raw = b'{"broken":true}'
    (tmp_path / "state.json").write_bytes(raw)
    with pytest.raises(ValueError, match="malformed"):
        ds.prepare(tmp_path, binding(), git_head="b" * 40, notices=[notice()], reviewed=True)
    assert (tmp_path / "state.json").read_bytes() == raw


@pytest.mark.parametrize("mutation", ["file_id", "attempts", "history"])
def test_malformed_receipt_details_block_before_any_plan(tmp_path, mutation):
    b = binding()
    publish_first(tmp_path, b)
    state = json.loads((tmp_path / "state.json").read_text())
    if mutation == "file_id":
        state["file_id"] = "../foreign/file"
    elif mutation == "attempts":
        state["pending"]["attempts"] = True
    else:
        state["last_published"]["sha256"] = "f" * 64
    raw = encoded(state)
    (tmp_path / "state.json").write_bytes(raw)
    with pytest.raises(ValueError):
        ds.plan(tmp_path, b)
    assert (tmp_path / "state.json").read_bytes() == raw


@pytest.mark.parametrize("attempts", [0, 1])
def test_matching_raw_without_pending_write_cannot_publish_or_poison_state(tmp_path, attempts):
    b = {**binding(), "file_id": "synthetic-signal"}
    ds.prepare(tmp_path, b, git_head="b" * 40, notices=[notice()], reviewed=True)
    raw = frozen(tmp_path, b)
    path = tmp_path / "state.json"
    state = json.loads(path.read_text())
    state["pending"]["attempts"] = attempts
    before = encoded(state)
    path.write_bytes(before)
    with pytest.raises(ValueError, match="durable prior write intent"):
        ds.reconcile(tmp_path, b, metadata(b, raw), raw)
    assert path.read_bytes() == before
    assert ds.plan(tmp_path, b)["stage"] == "prepared"
