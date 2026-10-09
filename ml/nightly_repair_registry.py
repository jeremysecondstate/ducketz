"""One durable preparation/exchange source-repair owner per local repository.

Callers hold the existing workflow.lock for mutations. An applied installation
does not release ownership: the matching domain validates its audit before
resuming and releases the exact record only after verified recovery. No expiry
or process death authorizes takeover of an interrupted repair.
"""
import json
import os
from pathlib import Path
import re
import uuid
from hashlib import sha256

FIELDS = {"owner", "repair_id", "completion_record", "token", "action_date", "domain"}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Ambiguous duplicate repair-owner field")
        result[key] = value
    return result


def path(state_root):
    return Path(state_root) / "repair-owner.json"


def validate(record):
    if (not isinstance(record, dict) or set(record) != FIELDS
            or record["domain"] not in {"preparation", "exchange"}
            or not all(isinstance(record[key], str) and record[key] for key in FIELDS - {"completion_record"})
            or (record["completion_record"] is not None and
                (not isinstance(record["completion_record"], str) or not record["completion_record"]))
            or re.fullmatch(r"[0-9a-f]{64}", record["token"]) is None
            or re.fullmatch(r"\d{4}-\d{2}-\d{2}", record["action_date"]) is None
            or re.fullmatch(r"[A-Za-z0-9_-]{8,100}", record["repair_id"]) is None):
        raise ValueError("Invalid repository repair owner record")
    return record


def read(state_root):
    target = path(state_root)
    if target.is_symlink():
        raise ValueError("Repair owner cannot be a link")
    if not target.exists():
        return None
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return None  # Exact owner released between the existence check and read.
    return validate(json.loads(raw, object_pairs_hook=_unique))


def assert_owner(state_root, record):
    if read(state_root) != validate(record):
        raise ValueError("Another repair owner holds the repository")


def acquire(state_root, record):
    """Caller holds workflow.lock; exact retries retain the original owner."""
    validate(record)
    current = read(state_root)
    if current is not None:
        assert_owner(state_root, record)
        return current
    target = path(state_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".pending-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as output:
            output.write((json.dumps(record, sort_keys=True) + "\n").encode())
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            assert_owner(state_root, record)
    finally:
        temporary.unlink(missing_ok=True)
    return record


def bind_completion(state_root, record, completion_record):
    """Bind the actual queue identity once, after pre-edit ownership was taken."""
    validate(record)
    if not isinstance(completion_record, str) or not completion_record:
        raise ValueError("Actual published completion identity required")
    current = read(state_root)
    if current is None or any(current[key] != record[key] for key in FIELDS - {"completion_record"}):
        raise ValueError("Another repair owner holds the repository")
    if current["completion_record"] not in (None, completion_record):
        raise ValueError("Original completion identity cannot change")
    if current["completion_record"] == completion_record:
        return current
    updated = {**current, "completion_record": completion_record}
    target = path(state_root)
    temporary = target.with_name(target.name + ".pending-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as output:
            output.write((json.dumps(updated, sort_keys=True) + "\n").encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return updated


def release(state_root, record, *, verified=False):
    """Caller holds workflow.lock and supplies verified resolution evidence."""
    if verified is not True:
        raise ValueError("Verified repair disposition required")
    assert_owner(state_root, record)
    path(state_root).unlink()


def continue_owner(state_root, previous, replacement, *, evidence, verified=False):
    """Continue one verified unsuccessful repair, without a release/takeover gap.

    Domain helpers verify a newly failed resumed attempt and archive its exact
    evidence. The registry permits at most three repair attempts in that chain.
    Callers hold workflow.lock; there is no deadline or owner expiry bypass.
    """
    validate(previous)
    validate(replacement)
    if (verified is not True or any(previous[key] != replacement[key] for key in ("owner", "domain", "action_date"))
            or replacement["completion_record"] is not None or previous["completion_record"] is None
            or replacement["repair_id"] == previous["repair_id"] or replacement["token"] == previous["token"]):
        raise ValueError("Verified same-owner continuation with a new attempt identity required")
    if not isinstance(evidence, dict) or set(evidence) != {"path", "sha256"}:
        raise ValueError("Exact immutable continuation evidence required")
    proof = Path(evidence["path"])
    if (not proof.is_absolute() or proof.is_symlink() or not proof.is_file()
            or sha256(proof.read_bytes()).hexdigest() != evidence["sha256"]):
        raise ValueError("Continuation evidence changed")
    current = read(state_root)
    if current not in (previous, replacement):
        raise ValueError("Another repair owner holds the repository")
    directory = Path(state_root) / "repair-owner-history"
    ancestry = []
    cursor = previous
    while True:
        matches = []
        for item in directory.glob("*.json"):
            raw = item.read_bytes()
            if sha256(raw).hexdigest() != item.stem:
                raise ValueError("Repair-owner continuation history changed")
            value = json.loads(raw)
            if all(value["replacement"][key] == cursor[key] for key in FIELDS - {"completion_record"}):
                matches.append((item.stem, value))
        if not matches:
            break
        if len(matches) != 1 or matches[0][0] in ancestry:
            raise ValueError("Ambiguous repair-owner continuation history")
        digest, value = matches[0]
        ancestry.append(digest)
        cursor = value["previous"]
    if len(ancestry) >= 2:
        raise ValueError("Three repair attempts exhausted; retain the unresolved owner and evidence")
    record = {"schema_version": "nightly-repair-owner-continuation-v1", "previous": previous,
              "replacement": replacement, "evidence": evidence, "ancestry": ancestry,
              "root_token": cursor["token"], "attempt": len(ancestry) + 2}
    raw = (json.dumps(record, sort_keys=True) + "\n").encode()
    directory.mkdir(parents=True, exist_ok=True)
    history = directory / (sha256(raw).hexdigest() + ".json")
    temporary = history.with_name(history.name + ".pending-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, history)
        except FileExistsError:
            if history.read_bytes() != raw:
                raise ValueError("Immutable continuation history changed")
    finally:
        temporary.unlink(missing_ok=True)
    if current == replacement:
        return replacement
    target = path(state_root)
    temporary = target.with_name(target.name + ".pending-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as output:
            output.write((json.dumps(replacement, sort_keys=True) + "\n").encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return replacement
