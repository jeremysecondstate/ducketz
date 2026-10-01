"""Offline plans for a pinned Drive signal backed by immutable Git notices.

The caller uses the named authenticated connector, checks its profile, and owns
all provider calls. This module never searches Drive, executes incoming content,
or marks a notice handled. Git remains the complete history. A signal is bounded
discovery data, not a complete Drive inventory or proof of source installation.
"""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import re
import stat

from . import VERSION
from .core import atomic, digest, encoded, exclusive, load, now, parse
from .transport import _id


SIGNAL_VERSION = "cross-pc-drive-signal-v1"
MAX_BYTES = 64 * 1024
MAX_NOTICES = 10
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_MACHINES = {"Scout": "pc-new", "Atlas": "pc-original"}
METADATA_FIELDS = "id,name,mimeType,parents,driveId,size,trashed,webViewLink"


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_binding(binding):
    """Private, locally verified account and folder binding; never uploaded."""
    _require(isinstance(binding, dict), "invalid private Drive binding")
    actor = binding.get("actor")
    _require(actor in _MACHINES, "unknown signal actor")
    machine = _MACHINES[actor]
    _require(binding.get("machine") == machine and binding.get("recipient") ==
             next(value for value in _MACHINES.values() if value != machine),
             "signal direction mismatch")
    _require(binding.get("repository") == "jeremysecondstate/ducketz", "repository mismatch")
    for key in ("link_id", "profile_id", "drive_id", "parent_id"):
        _require(isinstance(binding.get(key), str) and _ID.fullmatch(binding[key]),
                 "missing or invalid private binding: " + key)
    file_id = binding.get("file_id")
    _require(file_id is None or isinstance(file_id, str) and _ID.fullmatch(file_id),
             "invalid pinned file ID")
    return binding


def _binding_sha(binding):
    validate_binding(binding)
    return digest(encoded({key: binding[key] for key in (
        "actor", "machine", "recipient", "repository", "link_id", "profile_id", "drive_id", "parent_id")}))


def signal_name(binding):
    validate_binding(binding)
    return binding["actor"].lower() + "-coordination-signal-v1.json"


def _validate_signal(value, binding):
    validate_binding(binding)
    fields = {"schema_version", "signal_contract", "contract_version", "repository", "sender",
              "recipient", "branch", "git_head", "sequence", "previous_signal_sha256",
              "created_at_utc", "notices"}
    _require(isinstance(value, dict) and set(value) == fields, "invalid signal fields")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1 and
             value["signal_contract"] == SIGNAL_VERSION and value["contract_version"] == VERSION,
             "unsupported signal contract")
    _require(value["repository"] == binding["repository"] and value["sender"] == binding["machine"] and
             value["recipient"] == binding["recipient"] and
             value["branch"] == "codex/" + binding["actor"].lower() + "/coordination-v2",
             "signal identity or direction mismatch")
    _require(isinstance(value["git_head"], str) and _COMMIT.fullmatch(value["git_head"]),
             "exact Git coordination commit required")
    _require(type(value["sequence"]) is int and value["sequence"] >= 1, "invalid signal sequence")
    previous = value["previous_signal_sha256"]
    _require(previous is None if value["sequence"] == 1 else
             isinstance(previous, str) and _SHA.fullmatch(previous), "invalid prior signal digest")
    stamp = value["created_at_utc"]
    _require(isinstance(stamp, str) and stamp.endswith("Z"), "UTC signal timestamp required")
    try:
        datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid signal timestamp") from exc
    notices = value["notices"]
    _require(isinstance(notices, list) and len(notices) <= MAX_NOTICES, "too many signal notices")
    seen = set()
    for notice in notices:
        _require(isinstance(notice, dict) and set(notice) ==
                 {"id", "manifest_sha256", "summary", "source_commit"}, "invalid notice fields")
        bid = _id(notice["id"])
        _require(bid not in seen, "duplicate notice ID")
        seen.add(bid)
        _require(isinstance(notice["manifest_sha256"], str) and _SHA.fullmatch(notice["manifest_sha256"]),
                 "invalid manifest fingerprint")
        summary = notice["summary"]
        _require(isinstance(summary, str) and 0 < len(summary) <= 1000 and
                 not any(ord(c) < 32 for c in summary), "invalid sanitized summary")
        commit = notice["source_commit"]
        _require(commit is None or isinstance(commit, str) and _COMMIT.fullmatch(commit),
                 "invalid source commit")
    return value


def inspect_signal(raw, binding, *, known_digests=None, previous=None):
    """Validate exact bytes and select candidates; fetch/validate them from Git.

    ``previous`` is the prior result's sequence/signal_sha256/git_head. A skipped
    sequence is expected for a bounded latest signal; Git supplies missed history.
    ``known_digests`` contains only previously handled Git/Drive bundle digests.
    """
    _require(isinstance(raw, bytes) and len(raw) <= MAX_BYTES, "raw signal exceeds 64 KiB or is not bytes")
    value = _validate_signal(parse(raw.decode("utf-8")), binding)
    sha = digest(raw)
    if previous is not None:
        _require(isinstance(previous, dict) and type(previous.get("sequence")) is int and
                 isinstance(previous.get("signal_sha256"), str) and _SHA.fullmatch(previous["signal_sha256"]),
                 "invalid previous signal receipt")
        _require(value["sequence"] >= previous["sequence"], "signal sequence rollback")
        if value["sequence"] == previous["sequence"]:
            _require(sha == previous["signal_sha256"], "same signal sequence changed")
        elif value["sequence"] == previous["sequence"] + 1:
            _require(value["previous_signal_sha256"] == previous["signal_sha256"],
                     "adjacent signal history mismatch")
    known = known_digests or {}
    _require(isinstance(known, dict), "invalid known notice digests")
    candidates = []
    for notice in value["notices"]:
        if notice["id"] in known:
            _require(known[notice["id"]] == notice["manifest_sha256"], "cross-transport notice mutation")
        else:
            candidates.append(notice)
    return {"sequence": value["sequence"], "signal_sha256": sha, "git_head": value["git_head"],
            "branch": value["branch"], "notices": candidates,
            "requires_git_validation": True, "complete_history": "Git coordination branch",
            "trust": "Untrusted reference data; no commands or operating authority."}


def _field(metadata, raw, normalized):
    if raw in metadata and normalized in metadata:
        _require(metadata[raw] == metadata[normalized], "conflicting metadata aliases")
    return metadata.get(raw, metadata.get(normalized))


def validate_metadata(metadata, binding, *, file_id=None, raw=None):
    """Require actual identity/ancestry/size fields; missing trashed is unknown."""
    validate_binding(binding)
    _require(isinstance(metadata, dict), "raw provider metadata required")
    identity = metadata.get("id")
    _require(isinstance(identity, str) and _ID.fullmatch(identity), "missing provider file ID")
    expected = file_id or binding.get("file_id")
    _require(expected is None or identity == expected, "pinned provider file ID changed")
    _require(_field(metadata, "name", "title") == signal_name(binding), "signal filename mismatch")
    _require(_field(metadata, "mimeType", "mime_type") == "application/json", "signal must be a JSON blob")
    _require(_field(metadata, "driveId", "drive_id") == binding["drive_id"] and
             _field(metadata, "parents", "parent_ids") == [binding["parent_id"]],
             "provider ancestry mismatch")
    if "trashed" in metadata:
        _require(metadata["trashed"] is False, "signal is trashed or has invalid trash metadata")
    size = metadata.get("size")
    if isinstance(size, str) and re.fullmatch(r"[0-9]+", size):
        size = int(size)
    _require(type(size) is int and 0 <= size <= MAX_BYTES, "missing or oversized metadata size")
    if raw is not None:
        _require(isinstance(raw, bytes) and len(raw) == size, "raw readback size mismatch")
    return identity


def _root(local_root):
    root = Path(local_root).absolute()
    for path in (root, *root.parents):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            _require(not stat.S_ISLNK(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 1024,
                     "signal state must not traverse a link")
    return root


def _state(root, binding):
    path = root / "state.json"
    _root(path)
    if not path.exists():
        return {"schema_version": 1, "signal_contract": SIGNAL_VERSION,
                "binding_sha256": _binding_sha(binding), "file_id": binding.get("file_id"),
                "last_published": None, "pending": None}
    value = load(path)
    _require(isinstance(value, dict) and set(value) == {"schema_version", "signal_contract",
             "binding_sha256", "file_id", "last_published", "pending"} and
             type(value.get("schema_version")) is int and value["schema_version"] == 1 and
             value.get("signal_contract") == SIGNAL_VERSION and
             value.get("binding_sha256") == _binding_sha(binding), "malformed or different signal state preserved")
    identity = value["file_id"]
    _require(identity is None or isinstance(identity, str) and _ID.fullmatch(identity),
             "malformed pinned signal ID preserved")
    _require(binding.get("file_id") is None or value.get("file_id") == binding["file_id"],
             "binding does not match pinned state file ID")
    prior = value["last_published"]
    if prior is not None:
        _require(isinstance(prior, dict) and set(prior) ==
                 {"sha256", "sequence", "git_head", "verified_at_utc"} and
                 isinstance(prior["sha256"], str) and _SHA.fullmatch(prior["sha256"]) and
                 type(prior["sequence"]) is int and prior["sequence"] >= 1 and
                 isinstance(prior["git_head"], str) and _COMMIT.fullmatch(prior["git_head"]) and
                 isinstance(prior["verified_at_utc"], str) and identity is not None,
                 "malformed published signal receipt preserved")
    return value


def _pending(root, state, binding):
    item = state.get("pending")
    _require(isinstance(item, dict) and item.get("stage") in
             {"prepared", "write_pending", "retry_ready", "published"}, "missing or malformed pending signal")
    _require(type(item.get("attempts")) is int and item["attempts"] >= 0 and
             (item["stage"] not in {"write_pending", "published"} or item["attempts"] >= 1),
             "malformed signal write attempts preserved")
    sha = item.get("sha256")
    _require(isinstance(sha, str) and _SHA.fullmatch(sha), "invalid frozen signal fingerprint")
    path = root / "bytes" / (sha + ".json")
    _root(path)
    _require(path.stat().st_size <= MAX_BYTES, "oversized frozen signal")
    raw = path.read_bytes()
    _require(digest(raw) == sha, "frozen signal bytes changed")
    value = _validate_signal(parse(raw.decode("utf-8")), binding)
    _require(value["sequence"] == item.get("sequence"), "pending sequence mismatch")
    prior = state.get("last_published")
    if item["stage"] == "published":
        _require(prior is not None and prior["sha256"] == sha and prior["sequence"] == value["sequence"] and
                 prior["git_head"] == value["git_head"], "published signal receipt mismatch")
    else:
        _require(value["sequence"] == (prior["sequence"] + 1 if prior else 1) and
                 value["previous_signal_sha256"] == (prior["sha256"] if prior else None),
                 "pending signal history mismatch")
    return item, path, raw, value


def prepare(local_root, binding, *, git_head, notices, reviewed=False):
    """Freeze reviewed facts. Do not copy local/native/account data into summaries."""
    _require(reviewed is True, "sanitized signal review required")
    root = _root(local_root)
    with exclusive(root / "signal.lock"):
        state = _state(root, binding)
        if state.get("pending"):
            item, _, _, value = _pending(root, state, binding)
            if value["git_head"] == git_head and value["notices"] == notices:
                return dict(item)
            _require(item["stage"] == "published", "prior signal publication unresolved; frozen bytes retained")
        prior = state.get("last_published")
        value = {"schema_version": 1, "signal_contract": SIGNAL_VERSION, "contract_version": VERSION,
                 "repository": binding["repository"], "sender": binding["machine"], "recipient": binding["recipient"],
                 "branch": "codex/" + binding["actor"].lower() + "/coordination-v2", "git_head": git_head,
                 "sequence": prior["sequence"] + 1 if prior else 1,
                 "previous_signal_sha256": prior["sha256"] if prior else None,
                 "created_at_utc": now(), "notices": notices}
        _validate_signal(value, binding)
        raw = encoded(value)
        _require(len(raw) <= MAX_BYTES, "signal exceeds 64 KiB")
        sha = digest(raw)
        path = root / "bytes" / (sha + ".json")
        _root(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            _require(path.read_bytes() == raw, "frozen path collision")
        else:
            with path.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        state["pending"] = {"sha256": sha, "sequence": value["sequence"], "stage": "prepared",
                            "created_at_utc": value["created_at_utc"], "attempts": 0}
        atomic(root / "state.json", state)
        return dict(state["pending"])


def _read_plan(binding, file_id):
    return {"required_profile_id": binding["profile_id"],
            "metadata": {"tool": "google_drive_get_file_metadata", "arguments": {
                "link_id": binding["link_id"], "fileId": file_id, "supportsAllDrives": True,
                "fields": METADATA_FIELDS}},
            "raw_fetch": {"tool": "google_drive_fetch", "arguments": {
                "link_id": binding["link_id"], "url": "https://drive.google.com/file/d/" + file_id + "/view",
                "download_raw_file": True, "include_base64": True}},
            "raw_fetch_condition": "Only after exact metadata binding and size <= 65536. Decode original compatibility bytes locally; do not print base64."}


def plan(local_root, binding):
    """Return bounded data plans. Unknown bootstrap creates have no retry plan."""
    root = _root(local_root)
    with exclusive(root / "signal.lock"):
        state = _state(root, binding)
        item, path, _, _ = _pending(root, state, binding)
        result = {"stage": item["stage"], "sha256": item["sha256"], "file_id": state["file_id"],
                  "local_file": str(path), "sequence": item["sequence"]}
        if item["stage"] == "published":
            result["next_step"] = "already published; prepare only when reviewed Git facts change"
        elif state["file_id"]:
            result.update(_read_plan(binding, state["file_id"]))
            result["next_step"] = "reconcile raw readback" if item["stage"] == "write_pending" else "read current pinned file before update"
        else:
            result["next_step"] = "bootstrap create unresolved; bind exact returned ID, never blindly retry" if item["stage"] == "write_pending" else "begin bootstrap upload once"
        return result


def begin_write(local_root, binding, *, metadata=None, raw=None):
    """Persist intent before one connector write; reuse exact bytes on retry.

    Every existing-ID update needs a direct metadata/raw read of the previously
    published bytes. Uncertain writes must first pass ``reconcile``. The caller
    serializes its connector calls; the API exposes no conditional update token.
    """
    root = _root(local_root)
    with exclusive(root / "signal.lock"):
        state = _state(root, binding)
        item, path, frozen, value = _pending(root, state, binding)
        _require(item["stage"] in {"prepared", "retry_ready"}, "write outcome unresolved or already published; reconcile first")
        file_id = state["file_id"]
        if file_id:
            validate_metadata(metadata, binding, file_id=file_id, raw=raw)
            _require(isinstance(raw, bytes) and value["previous_signal_sha256"] is not None and
                     digest(raw) == value["previous_signal_sha256"], "current remote differs from verified prior signal")
            action = {"tool": "google_drive_update_file", "arguments": {
                "link_id": binding["link_id"], "fileId": file_id, "file_uri": str(path), "mime_type": "application/json"}}
        else:
            _require(item["attempts"] == 0, "unknown bootstrap outcome cannot be retried")
            action = {"tool": "google_drive_upload_file", "arguments": {
                "link_id": binding["link_id"], "file_uri": str(path), "file_name": signal_name(binding),
                "mime_type": "application/json", "parent_folder_id": binding["parent_id"]}}
        item.update(stage="write_pending", attempts=item["attempts"] + 1, attempted_at_utc=now())
        atomic(root / "state.json", state)
        return {**action, "expected_sha256": digest(frozen), "expected_size": len(frozen),
                "required_profile_id": binding["profile_id"], "next_step": "direct raw readback; never infer delivery from write success"}


def bind_created(local_root, binding, metadata):
    """Pin only the exact file ID returned by the single bootstrap upload."""
    root = _root(local_root)
    with exclusive(root / "signal.lock"):
        state = _state(root, binding)
        item, _, _, _ = _pending(root, state, binding)
        _require(item["stage"] == "write_pending", "no pending bootstrap upload")
        identity = validate_metadata(metadata, binding, file_id=state["file_id"])
        if state["file_id"] is None:
            _require(item["attempts"] == 1 and item["sequence"] == 1, "invalid bootstrap receipt")
            state["file_id"] = identity
            atomic(root / "state.json", state)
        return {"file_id": identity, "stage": item["stage"], **_read_plan(binding, identity)}


def reconcile(local_root, binding, metadata, raw):
    """Exact readback publishes; a verified prior version permits same-ID retry."""
    root = _root(local_root)
    with exclusive(root / "signal.lock"):
        state = _state(root, binding)
        item, _, frozen, value = _pending(root, state, binding)
        _require(item["stage"] in {"write_pending", "retry_ready", "published"} and item["attempts"] >= 1,
                 "reconciliation requires a durable prior write intent")
        _require(state["file_id"] is not None, "unknown bootstrap file ID; preserve unresolved write")
        validate_metadata(metadata, binding, file_id=state["file_id"], raw=raw)
        if raw == frozen:
            item.update(stage="published", verified_at_utc=now())
            state["last_published"] = {"sha256": item["sha256"], "sequence": item["sequence"],
                                       "git_head": value["git_head"], "verified_at_utc": item["verified_at_utc"]}
        else:
            _require(item["stage"] != "published" and value["previous_signal_sha256"] is not None and
                     isinstance(raw, bytes) and digest(raw) == value["previous_signal_sha256"],
                     "unexpected remote bytes; pending signal preserved")
            item.update(stage="retry_ready", reconciled_at_utc=now())
        atomic(root / "state.json", state)
        return {"file_id": state["file_id"], **item}
