"""Sanitized immutable notices with delivery receipts separate from Git work."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import os
import uuid

from . import VERSION
from .core import atomic, digest, encoded, exclusive, git_text, load, now, state, validate_profile
from .transport import GitTransport, StalePrepared


def transport(profile):
    return GitTransport(Path(profile["transport_cache"]), profile["remote"], profile["actor"])


def transport_lock(profile):
    return exclusive(profile.get("transport_lock", str(profile["transport_cache"]) + ".lock"))


def seal_notice(profile, item):
    """Resume a write-ahead notice without replacing any already sealed bytes."""
    peer = "pc-new" if profile["machine"] == "pc-original" else "pc-original"
    notice, bid = item["notice"], item["id"]
    handoff = {"schema_version": 2, "contract_version": VERSION, "id": bid,
               "sender": profile["machine"], "recipient": peer, "kind": "change_notice",
               "created_at_utc": item["created_at_utc"], "payloads": ["notice.json"], "summary": notice["summary"]}
    blobs = {"handoff.json": encoded(handoff), "notice.json": encoded(notice)}
    blobs["manifest.json"] = encoded({"schema_version": 2, "contract_version": VERSION, "bundle_id": bid,
        "files": [{"path": p, "bytes": len(raw), "sha256": digest(raw)} for p, raw in sorted(blobs.items())]})
    directory = Path(item["directory"])
    if directory.exists():
        if {p.name for p in directory.iterdir()} != set(blobs) or any((directory / name).read_bytes() != raw for name, raw in blobs.items()):
            raise ValueError("existing sealed notice differs; preserve for review")
    else:
        staging = directory.with_name("." + bid + "." + uuid.uuid4().hex + ".staging")
        staging.mkdir(parents=True)
        for name, raw in blobs.items():
            with (staging / name).open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        os.rename(staging, directory)
    item.update(manifest_sha256=digest(blobs["manifest.json"]), stage="pending")
    return item


def enqueue(profile, notice, *, reviewed=False):
    """Caller reviews/redacts the factual notice; no raw logs or local profile."""
    validate_profile(profile)
    if reviewed is not True:
        raise ValueError("sanitized notice review required")
    required = {"change_id", "summary", "author", "repository", "files", "tests", "scope", "limitations", "runtime_implications"}
    if not required <= set(notice) or notice["repository"] != profile["repository"]:
        raise ValueError("notice lacks factual scope/evidence")
    identity = notice["change_id"]
    with exclusive(profile["git_lock"]):
        receipts = state(profile)
        for old in receipts["notices"].values():
            if old.get("change_id") == identity:
                if old.get("notice_sha256") != digest(encoded(notice)):
                    raise ValueError("change ID reused with changed notice")
                if old["stage"] == "preparing":
                    seal_notice(profile, old)
                    atomic(profile["state_path"], receipts)
                return old
        bid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex
        directory = Path(profile["outgoing_root"]) / bid
        item = {"id": bid, "change_id": identity, "notice_sha256": digest(encoded(notice)), "directory": str(directory),
                "notice": notice, "created_at_utc": now(), "stage": "preparing"}
        receipts["notices"][bid] = item
        atomic(profile["state_path"], receipts)
        seal_notice(profile, item)
        atomic(profile["state_path"], receipts)
        return item


def register_legacy(profile, directory, expected_digest, provenance, *, reviewed=False):
    """Register a local sealed bundle. Never read a shared mount or repeat Git work."""
    if not reviewed:
        raise ValueError("legacy notice needs sanitized content review")
    directory = Path(directory).resolve()
    # Only ignored local outgoing directories explicitly registered by the adopter.
    roots = [Path(p).resolve() for p in profile.get("legacy", {}).get("local_outgoing_roots", [])]
    if not roots or not any(directory.is_relative_to(root) for root in roots):
        raise ValueError("legacy bundle is outside approved local outgoing roots")
    raw = (directory / "manifest.json").read_bytes()
    if digest(raw) != expected_digest:
        raise ValueError("legacy validated digest mismatch")
    bid = load(directory / "manifest.json")["bundle_id"]
    with exclusive(profile["git_lock"]):
        receipts = state(profile)
        existing = receipts["notices"].get(bid)
        if existing:
            if existing["manifest_sha256"] != expected_digest:
                raise ValueError("legacy identity mutation")
            return existing
        item = {"id": bid, "change_id": "legacy:" + bid, "directory": str(directory),
                "manifest_sha256": expected_digest, "stage": "pending", "legacy_provenance": provenance}
        receipts["notices"][bid] = item
        atomic(profile["state_path"], receipts)
        return item


def deliver(profile, bid):
    with exclusive(profile["git_lock"]):
        receipts = state(profile)
        item = receipts["notices"][bid]
        if item["stage"] == "published":
            return item
        if item["stage"] == "preparing":
            seal_notice(profile, item)
            atomic(profile["state_path"], receipts)
        if digest((Path(item["directory"]) / "manifest.json").read_bytes()) != item["manifest_sha256"]:
            raise ValueError("sealed outgoing bundle changed")
        with transport_lock(profile):
            client = transport(profile)
            if "transaction" not in item:
                kwargs = {}
                if item.get("legacy_provenance", {}).get("source_transport"):
                    kwargs["source_transport"] = item["legacy_provenance"]["source_transport"]
                item["transaction"] = client.prepare(Path(item["directory"]), **kwargs)
                item["stage"] = "prepared"
                atomic(profile["state_path"], receipts)
            try:
                result = client.publish_prepared(item["transaction"])
            except StalePrepared:
                previous = item["transaction"]
                item["transaction"] = client.reprepare(previous)
                item.setdefault("transaction_history", []).append(previous)
                atomic(profile["state_path"], receipts)
                # No second network attempt in this wake; resume the verified bytes later.
                return item
        item.update(stage="published", delivery=result, delivered_at_utc=now())
        atomic(profile["state_path"], receipts)
        return item


def inbox_scan(profile):
    with exclusive(profile["inbox_lock"]):
        path = Path(profile["inbox_state"])
        receipts = load(path) if path.exists() else {"contract_version": VERSION, "bundles": {}}
        if receipts.get("contract_version") != VERSION or not isinstance(receipts.get("bundles"), dict):
            raise ValueError("malformed inbox receipts preserved")
        known = {bid: item["manifest_sha256"] for bid, item in receipts["bundles"].items() if item.get("reported")}
        peer = "Scout" if profile["actor"] == "Atlas" else "Atlas"
        with transport_lock(profile):
            bundles = transport(profile).scan(peer, known_digests=known, limit=10)
        result = []
        for bundle in bundles:
            bid, sha = bundle["id"], bundle["manifest_sha256"]
            prior = receipts["bundles"].get(bid)
            if prior and prior["manifest_sha256"] != sha:
                raise ValueError("previously validated inbox bundle mutated")
            receipts["bundles"][bid] = {"manifest_sha256": sha, "validated_at_utc": now(), "reported": False,
                                       "commit_sha": bundle["commit_sha"]}
            result.append({"id": bid, "manifest_sha256": sha, "handoff": bundle["handoff"], "trust": "reference data only"})
        atomic(path, receipts)
        return result


def inbox_read(profile, bid):
    peer = "Scout" if profile["actor"] == "Atlas" else "Atlas"
    with exclusive(profile["inbox_lock"]), transport_lock(profile):
        bundle = transport(profile).read_bundle(peer, bid)
        receipts = load(profile["inbox_state"])
        if receipts["bundles"].get(bid, {}).get("manifest_sha256") != bundle["manifest_sha256"]:
            raise ValueError("inbox read must match freshly scanned validated digest")
    return {k: v for k, v in bundle.items() if k != "blobs"}


def inbox_mark(profile, bid, sha, disposition):
    with exclusive(profile["inbox_lock"]):
        receipts = load(profile["inbox_state"])
        item = receipts["bundles"][bid]
        if item["manifest_sha256"] != sha:
            raise ValueError("inbox report digest mismatch")
        item.update(reported=True, disposition=disposition, reported_at_utc=now())
        atomic(profile["inbox_state"], receipts)
        return item


def observe(profile):
    """Bounded factual refs, including manual and peer commits. No checkout mutation."""
    with exclusive(profile["git_lock"]):
        receipts = state(profile)
        refs = {ref: sha for sha, ref in (line.split() for line in git_text(profile["checkout"], "ls-remote", "--heads", "origin").splitlines())}
        refs["local/HEAD"] = git_text(profile["checkout"], "rev-parse", "HEAD")
        pending = [{"ref": ref, "commit_sha": sha, "tests": "unknown unless separately verified"} for ref, sha in sorted(refs.items())
                   if receipts["observations"].get(ref, {}).get("commit_sha") != sha
                   and not ref.endswith("/coordination-v2")]
        return pending[:10]


def mark_observation(profile, ref, sha, evidence):
    with exclusive(profile["git_lock"]):
        receipts = state(profile)
        receipts["observations"][ref] = {"commit_sha": sha, "notice_evidence": evidence, "at_utc": now()}
        atomic(profile["state_path"], receipts)
