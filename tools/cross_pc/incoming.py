"""Bounded incoming source intake; peer code is stored and inspected, never run.

Callers derive an explicit local specification from a validated notice under
standing local authority. Notice reporting and source review are separate queues.
Only the existing courier's single-commit publication shape is supported. A
verified tree is pending local review, not installed, tested locally or merged.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .core import atomic, digest, encoded, exclusive, load, now, shareable, validate_profile


PROTOCOL = "cross-pc-incoming-v1"
REPOSITORY = "jeremysecondstate/ducketz"
MAX_PATHS = 1000
MAX_BLOB_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_STATE_BYTES = 16 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class IncomingError(ValueError):
    """Source/evidence validation failed; the durable record stays visible."""


class FetchPending(IncomingError):
    """A Git operation could not be proved; retain state and retry later."""


def _location(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise IncomingError("incoming paths must be absolute without parent traversal")
    for item in reversed((path, *path.parents)):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
                raise IncomingError("incoming path contains a symlink or reparse point")
    return path


def paths(profile):
    """Local ignored storage defaults; overrides must remain under state_path's parent."""
    validate_profile(profile)
    root = _location(Path(profile["state_path"]).parent)
    result = {
        "state": _location(profile.get("incoming_state", root / "incoming-source-receipts.json")),
        "lock": _location(profile.get("incoming_lock", root / "locks" / "incoming-source.lock")),
        "cache": _location(profile.get("source_cache", root / "incoming-source.git")),
    }
    if any(not p.is_relative_to(root) or p == root for p in result.values()):
        raise IncomingError("incoming storage must stay inside the local receipt directory")
    if len(set(result.values())) != 3 or any(result[k].is_relative_to(result["cache"]) for k in ("state", "lock")):
        raise IncomingError("incoming storage paths overlap")
    return result


def _hash(value, pattern, label):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise IncomingError("invalid " + label)
    return value


def _path(value):
    if not isinstance(value, str) or len(value) > 1024 or any(c in value for c in '*?[]<>"|'):
        raise IncomingError("invalid literal source path")
    try:
        return shareable(value)
    except ValueError as exc:
        raise IncomingError("unsafe or machine-local source path") from exc


def specification(profile, value):
    """Validate literal data only; arbitrary commands and additional fields are rejected."""
    validate_profile(profile)
    required = {"repository", "peer", "branch", "commit_sha", "base_commit", "files", "dependency_fingerprints"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"notice_id", "notice_manifest_sha256"}:
        raise IncomingError("unexpected or missing incoming specification fields")
    peer = "Atlas" if profile["actor"] == "Scout" else "Scout"
    if value["repository"] != REPOSITORY or value["peer"] != peer:
        raise IncomingError("incoming repository or peer identity mismatch")
    branch = value["branch"]
    prefix = "codex/" + peer.lower() + "/"
    if (not isinstance(branch, str) or not branch.startswith(prefix)
            or not re.fullmatch(r"[A-Za-z0-9_/-][A-Za-z0-9_./-]{0,199}", branch)
            or ".." in branch or "//" in branch or branch.endswith(("/", ".", ".lock"))
            or any(p.startswith(".") or p.endswith(".lock") for p in branch.split("/"))
            or branch == prefix + "coordination-v2"):
        raise IncomingError("unexpected peer source branch")
    commit = _hash(value["commit_sha"], _SHA, "exact source commit")
    base = _hash(value["base_commit"], _SHA, "exact source base")
    if commit == base:
        raise IncomingError("source commit equals its base")
    files, deps = value["files"], value["dependency_fingerprints"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_PATHS or not isinstance(deps, dict) or len(files) + len(deps) > MAX_PATHS:
        raise IncomingError("incoming path count exceeds bound")
    seen, normalized = set(), []
    for item in files:
        if not isinstance(item, dict) or set(item) != {"path", "operation", "sha256"}:
            raise IncomingError("invalid owned file specification")
        name, operation = _path(item["path"]), item["operation"]
        if name.casefold() in seen or operation not in {"add", "modify", "delete"}:
            raise IncomingError("duplicate path or invalid file operation")
        seen.add(name.casefold())
        sha = None if operation == "delete" else _hash(item["sha256"], _DIGEST, "owned file SHA256")
        if operation == "delete" and item["sha256"] is not None:
            raise IncomingError("deleted path must have a null fingerprint")
        normalized.append({"path": name, "operation": operation, "sha256": sha})
    dependencies = {}
    for name, claimed in deps.items():
        name = _path(name)
        if name.casefold() in seen:
            raise IncomingError("overlapping or case-aliased dependency path")
        seen.add(name.casefold())
        dependencies[name] = _hash(claimed, _DIGEST, "dependency working-byte SHA256")
    result = {"repository": REPOSITORY, "peer": peer, "branch": branch, "commit_sha": commit,
              "base_commit": base, "files": sorted(normalized, key=lambda p: p["path"]),
              "dependency_fingerprints": dict(sorted(dependencies.items()))}
    if ("notice_id" in value) != ("notice_manifest_sha256" in value):
        raise IncomingError("notice identity and digest must be supplied together")
    if "notice_id" in value:
        if not isinstance(value["notice_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:_-]{0,199}", value["notice_id"]):
            raise IncomingError("invalid notice identity")
        result["notice_id"] = value["notice_id"]
        result["notice_manifest_sha256"] = _hash(value["notice_manifest_sha256"], _DIGEST, "notice manifest SHA256")
    return result


def _state(profile, storage):
    path = _location(storage["state"])
    if path.exists() and (not path.is_file() or path.stat().st_size > MAX_STATE_BYTES):
        raise IncomingError("incoming receipt size or type invalid; preserve file")
    value = load(path) if path.exists() else {"protocol": PROTOCOL, "repository": REPOSITORY,
        "actor": profile["actor"], "records": {}, "cursor": None}
    if (value.get("protocol"), value.get("repository"), value.get("actor")) != (PROTOCOL, REPOSITORY, profile["actor"]) or not isinstance(value.get("records"), dict):
        raise IncomingError("incoming receipt identity changed; preserve file")
    return value


def _save(storage, receipts):
    if len(encoded(receipts)) > MAX_STATE_BYTES:
        raise IncomingError("incoming receipt size bound reached; preserve prior state")
    atomic(_location(storage["state"]), receipts)


def enqueue(profile, value, *, reviewed=False):
    """Queue one reviewed local specification; duplicate source identities do not refetch."""
    if reviewed is not True:
        raise IncomingError("reviewed local incoming specification required")
    spec, storage = specification(profile, value), paths(profile)
    evidence = {k: v for k, v in spec.items() if not k.startswith("notice_")}
    identity = REPOSITORY + ":" + spec["peer"] + ":" + spec["commit_sha"]
    evidence_hash = digest(encoded(evidence))
    provenance = {k: v for k, v in spec.items() if k.startswith("notice_")}
    with exclusive(storage["lock"]):
        receipts = _state(profile, storage)
        item = receipts["records"].get(identity)
        if item is None:
            item = {"id": identity, "specification": evidence, "specification_sha256": evidence_hash,
                    "stage": "source_fetch_pending", "last_completed_stage": "notice_validated",
                    "queued_at_utc": now(), "attempts": 0, "retryable": True, "provenance": [], "conflicts": []}
            receipts["records"][identity] = item
        changed_provenance = any(p.get("notice_id") == provenance.get("notice_id") and p != provenance for p in item["provenance"]) if provenance else False
        if item["specification_sha256"] != evidence_hash or changed_provenance:
            conflict = {"specification_sha256": evidence_hash, "specification": evidence, "provenance": provenance}
            if conflict not in item["conflicts"]:
                item["conflicts"].append(conflict)
            item.update(stage="blocked", retryable=False, reason="conflicting evidence for the same source identity")
        elif provenance and provenance not in item["provenance"]:
            item["provenance"].append(provenance)
        _save(storage, receipts)
        return item


class SourceCache:
    """Private bare source cache with fixed identity and no checkout or execution API."""

    def __init__(self, profile, storage, *, allow_local_remote=False):
        self.path = _location(storage["cache"])
        remote = profile.get("remote")
        allowed = {"https://github.com/" + REPOSITORY + ".git", "https://github.com/" + REPOSITORY,
                   "git@github.com:" + REPOSITORY + ".git", "ssh://git@github.com/" + REPOSITORY + ".git"}
        local = isinstance(remote, str) and Path(remote).is_absolute() and Path(remote).is_dir()
        if remote not in allowed and not (allow_local_remote is True and local):
            raise IncomingError("incoming source remote is not the fixed repository")
        if local:
            _location(remote)
        expected = {"protocol": PROTOCOL, "repository": REPOSITORY, "actor": profile["actor"], "remote": remote}
        marker = _location(self.path / "incoming-identity.json")
        if self.path.exists() and not self.path.is_dir():
            raise IncomingError("source cache is not a directory")
        if marker.exists():
            if load(marker) != expected:
                raise IncomingError("source cache identity changed; preserve existing cache")
        else:
            if self.path.exists() and any(self.path.iterdir()):
                raise IncomingError("unrecognized nonempty source cache; preserve it")
            self.path.mkdir(parents=True, exist_ok=True)
            atomic(marker, expected)
        for name in ("config", "objects", "objects/info/alternates", "refs", "FETCH_HEAD", "shallow", "HEAD"):
            _location(self.path / name)
        if not (self.path / "HEAD").exists():
            self.run("init", "--bare", str(self.path), outside=True)
        if self.run("rev-parse", "--is-bare-repository").strip() != b"true":
            raise IncomingError("source cache must remain bare")
        prior = self.run("config", "--get-all", "remote.origin.url", check=False).strip()
        if prior and prior != remote.encode():
            raise IncomingError("source cache remote identity changed")
        if not prior:
            self.run("config", "remote.origin.url", remote)

    def run(self, *args, check=True, outside=False, limit=4 * 1024 * 1024):
        # Never inherit Git repository/index/object overrides from the app shell.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_NO_REPLACE_OBJECTS="1")
        command = ["git", "-c", "core.hooksPath=" + str(self.path / "disabled-hooks"),
                   "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-c", "fetch.fsckObjects=true",
                   "-c", "protocol.ext.allow=never", "-c", "core.attributesFile=" + os.devnull]
        if not outside:
            command += ["--git-dir", str(self.path)]
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            try:
                result = subprocess.run([*command, *args], env=env, stdout=output, stderr=errors, timeout=60)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise FetchPending("Git operation interrupted; exact source receipt remains pending") from exc
            output.seek(0, os.SEEK_END)
            if output.tell() > limit:
                raise IncomingError("Git inspection output exceeded bound")
            output.seek(0)
            raw = output.read(limit)
            if check and result.returncode:
                raise FetchPending("Git operation failed; exact source receipt remains pending")
            return raw

    def fetch(self, spec):
        commit, peer = spec["commit_sha"], spec["peer"].lower()
        received = "refs/received/" + peer + "/" + commit
        _location(self.path / received)
        prior = self.run("show-ref", "--verify", "--hash", received, check=False).strip()
        if prior and prior != commit.encode():
            raise IncomingError("immutable received ref identity changed")
        # Always fetch the advertised object itself. A moving branch tip is never
        # a substitute for the notice's immutable source identity.
        if not prior:
            branch = "refs/heads/" + spec["branch"]
            head = self.run("ls-remote", "--heads", "origin", branch, limit=1024).strip()
            if head != commit.encode() + b"\t" + branch.encode():
                raise IncomingError("advertised source branch does not resolve to the exact source commit")
            self.run("fetch", "--depth=2", "--no-tags", "--no-recurse-submodules", "--refmap=", "origin", commit)
        actual = self.run("rev-parse", "--verify", commit + "^{commit}").decode().strip()
        if actual != commit:
            raise IncomingError("fetched source commit identity mismatch")
        if not prior:
            self.run("update-ref", received, commit, "0" * 40)
        if self.run("show-ref", "--verify", "--hash", received).strip() != commit.encode():
            raise IncomingError("received ref readback mismatch")
        return received

    def blob(self, commit, name):
        output = self.run("ls-tree", "-z", "--full-tree", commit, "--", ":(literal)" + name)
        rows = output.rstrip(b"\0").split(b"\0") if output else []
        if len(rows) != 1:
            raise IncomingError("missing or ambiguous source blob")
        metadata, actual_name = rows[0].split(b"\t", 1)
        mode, kind, identity = metadata.split(b" ")
        if actual_name.decode("utf-8") != name or mode not in {b"100644", b"100755"} or kind != b"blob":
            raise IncomingError("source path is not a regular blob; symlinks/submodules are forbidden")
        size = int(self.run("cat-file", "-s", identity.decode()).strip())
        if not 0 <= size <= MAX_BLOB_BYTES:
            raise IncomingError("source blob size exceeds bound")
        raw = self.run("cat-file", "blob", identity.decode(), limit=MAX_BLOB_BYTES)
        if len(raw) != size or hashlib.sha1(b"blob " + str(size).encode() + b"\0" + raw).hexdigest() != identity.decode():
            raise IncomingError("source blob Git identity mismatch")
        return raw, identity.decode(), mode.decode()

    def verify(self, spec):
        commit, base = spec["commit_sha"], spec["base_commit"]
        raw_commit = self.run("cat-file", "commit", commit)
        if hashlib.sha1(b"commit " + str(len(raw_commit)).encode() + b"\0" + raw_commit).hexdigest() != commit:
            raise IncomingError("source commit raw object identity mismatch")
        header = raw_commit.split(b"\n\n", 1)[0]
        parents = [line[7:].decode("ascii") for line in header.splitlines() if line.startswith(b"parent ")]
        if parents != [base]:
            raise IncomingError("source must have exactly the advertised base as its single parent")
        if self.run("rev-parse", "--verify", base + "^{commit}").strip() != base.encode():
            raise IncomingError("advertised base is not available as an exact commit")
        raw = self.run("diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-status", "-z", base, commit, "--")
        fields = raw.rstrip(b"\0").split(b"\0") if raw else []
        if len(fields) % 2 or len(fields) // 2 > MAX_PATHS:
            raise IncomingError("changed path inventory exceeds bound or is malformed")
        actual = {}
        for status, name in zip(fields[::2], fields[1::2]):
            path = _path(name.decode("utf-8"))
            if status not in (b"A", b"M", b"D") or path.casefold() in {p.casefold() for p in actual}:
                raise IncomingError("unsupported changed file type or case alias")
            actual[path] = {b"A": "add", b"M": "modify", b"D": "delete"}[status]
        expected = {p["path"]: p["operation"] for p in spec["files"]}
        if actual != expected:
            raise IncomingError("full changed path and operation set differs from notice evidence")
        evidence, dependencies, total = {}, {}, 0
        for item in spec["files"]:
            path, operation = item["path"], item["operation"]
            raw, identity, mode = self.blob(base if operation == "delete" else commit, path)
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                raise IncomingError("source inspection bytes exceeded bound")
            if operation != "delete" and digest(raw) != item["sha256"]:
                raise IncomingError("raw committed owned file hash differs from claimed hash")
            evidence[path] = {"operation": operation, "git_blob": identity, "mode": mode,
                              "committed_sha256": digest(raw), "claimed_sha256": item["sha256"],
                              "comparison": "deleted_base_blob" if operation == "delete" else "exact"}
        for path, claimed in spec["dependency_fingerprints"].items():
            raw, identity, mode = self.blob(commit, path)
            total += len(raw)
            if total > MAX_TOTAL_BYTES:
                raise IncomingError("source inspection bytes exceeded bound")
            actual_hash = digest(raw)
            comparison = "exact" if actual_hash == claimed else "mismatch"
            if comparison == "mismatch" and b"\0" not in raw:
                try:
                    raw.decode("utf-8")
                    lf = raw.replace(b"\r\n", b"\n")
                    if claimed in {digest(lf), digest(lf.replace(b"\n", b"\r\n"))}:
                        comparison = "line_endings_only"
                except UnicodeError:
                    pass
            dependencies[path] = {"git_blob": identity, "mode": mode, "committed_sha256": actual_hash,
                                  "claimed_tested_working_sha256": claimed, "comparison": comparison}
        return {"commit_sha": commit, "base_commit": base, "files": evidence, "dependencies": dependencies,
                "inspection_bytes": total, "owned_bytes_exact": True,
                "dependency_tested_bytes_exact": all(d["comparison"] == "exact" for d in dependencies.values()),
                "local_tests_run": False, "installed": False}


def process_one(profile, *, allow_local_remote=False):
    """Advance at most one source per wake, retaining ambiguous failures for retry.

    ``allow_local_remote`` is solely the offline-test seam. Peers cannot set it
    in a notice, specification or profile. Review-pending sources are never run.
    """
    storage = paths(profile)
    with exclusive(storage["lock"]):
        receipts = _state(profile, storage)
        eligible = sorted(k for k, v in receipts["records"].items() if v.get("retryable") and v.get("stage") != "review_pending")
        if not eligible:
            return {"stage": "quiet", "attempted": 0}
        cursor = receipts.get("cursor") or ""
        identity = next((k for k in eligible if k > cursor), eligible[0])
        item = receipts["records"][identity]
        receipts["cursor"] = identity
        item.update(attempts=item["attempts"] + 1, attempted_at_utc=now())
        _save(storage, receipts)
        try:
            spec = specification(profile, item["specification"])
            if identity != REPOSITORY + ":" + spec["peer"] + ":" + spec["commit_sha"] or digest(encoded(spec)) != item["specification_sha256"]:
                raise IncomingError("stored source specification identity changed")
            cache = SourceCache(profile, storage, allow_local_remote=allow_local_remote)
            item["received_ref"] = cache.fetch(spec)
            item.update(stage="fetched", last_completed_stage="fetched", fetched_at_utc=now())
            _save(storage, receipts)
            item["evidence"] = cache.verify(spec)
            if any(d["comparison"] == "mismatch" for d in item["evidence"]["dependencies"].values()):
                raise IncomingError("dependency committed bytes differ from claimed tested working bytes")
            item.update(stage="tree_verified", last_completed_stage="tree_verified", tree_verified_at_utc=now())
            _save(storage, receipts)
            item.update(stage="review_pending", retryable=False, review_pending_at_utc=now(),
                        reason="exact source retained; local review and any local tests remain pending")
        except (IncomingError, OSError, UnicodeError, KeyError, TypeError, ValueError) as exc:
            # Raw Git stderr, arbitrary exception text and notice prose are never
            # copied into the state or caller's logs.
            retryable = isinstance(exc, (FetchPending, OSError))
            reason = str(exc) if isinstance(exc, IncomingError) else "local source intake validation failed; preserve evidence"
            item.update(stage="blocked", retryable=retryable, reason=reason, blocked_at_utc=now())
        _save(storage, receipts)
        return item
