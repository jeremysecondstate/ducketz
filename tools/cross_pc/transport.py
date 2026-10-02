"""Explicit GitHub transport for immutable, sanitized cross-PC notice bundles.

This adapter never executes notice content. Authentication is delegated to Git's
existing native connection; no credential files are read. The caller holds its
native exclusive lock and atomically records ``prepare`` before invoking
``publish_prepared``. No Drive or shared-filesystem fallback exists.

Only the actor's dedicated coordination ref can be pushed. These orphan trees
contain bundles and their provenance wrappers, never application source. V1
bundles retain their original bytes and manifest digest during migration.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Mapping
import uuid


CONTRACT_VERSION = "cross-pc-v2"
TRANSPORT = "github-git-v2"
MAX_BUNDLE_BYTES = 20 * 1024 * 1024
MAX_FILES = 102
MAX_INVENTORY_FILES = 20_000
MAX_INVENTORY_BYTES = 4 * 1024 * 1024
MAX_NEW_NOTICES = 10
MAX_TRANSPORT_BYTES = 512 * 1024 * 1024
_ID = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{32}\Z")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_ROLES = {"atlas": "pc-original", "scout": "pc-new"}


class TransportError(ValueError):
    """A validation/transport failure; its stage remains pending."""


class DeliveryPending(TransportError):
    """Publication could not be proved; retry the same prepared transaction."""


class StalePrepared(DeliveryPending):
    """Remote advanced; safely reprepare cached exact bytes and persist history."""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(raw: bytes) -> object:
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise TransportError("duplicate JSON key")
            value[key] = item
        return value

    def constant(_):
        raise TransportError("non-finite JSON value")

    try:
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise TransportError("invalid UTF-8 JSON") from exc


def _encode(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _actor(value: str) -> str:
    if not isinstance(value, str):
        raise TransportError("invalid actor")
    name = {v: k for k, v in _ROLES.items()}.get(value.lower(), value.lower())
    if name not in _ROLES:
        raise TransportError("unknown actor")
    return name


def _id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise TransportError("invalid UTC/UUID bundle ID")
    try:
        dt.datetime.strptime(value[:16], "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise TransportError("invalid bundle timestamp") from exc
    return value


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise TransportError("invalid bundle path")
    if any(c in value for c in '\\:<>"|?*') or value.startswith("/"):
        raise TransportError("unsafe bundle path")
    for part in value.split("/"):
        if part in {"", ".", ".."} or part.endswith((".", " ")) or any(ord(c) < 32 or ord(c) == 127 for c in part):
            raise TransportError("unsafe bundle path")
        if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
            raise TransportError("reserved bundle path")
    return value


def validate_bundle(bid: str, blobs: Mapping[str, bytes], sender: str, recipient: str) -> dict:
    """Validate exact original bytes; returned content is still untrusted data."""
    _id(bid)
    if sender == recipient or set((sender, recipient)) != set(_ROLES.values()):
        raise TransportError("invalid sender/recipient pair")
    if not isinstance(blobs, Mapping) or not 2 <= len(blobs) <= MAX_FILES:
        raise TransportError("unsupported bundle file count")
    seen = set()
    for name, raw in blobs.items():
        _path(name)
        if name.casefold() in seen:
            raise TransportError("duplicate case-insensitive file path")
        seen.add(name.casefold())
        if not name.endswith((".json", ".md")) or not isinstance(raw, bytes):
            raise TransportError("unsupported non-text/executable bundle payload")
    if sum(map(len, blobs.values())) > MAX_BUNDLE_BYTES:
        raise TransportError("oversized bundle")
    if not {"manifest.json", "handoff.json"} <= set(blobs):
        raise TransportError("missing manifest or handoff")
    manifest = _json(blobs["manifest.json"])
    if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest.get("schema_version") not in (1, 2) or manifest.get("bundle_id") != bid:
        raise TransportError("manifest schema or ID mismatch")
    version = manifest["schema_version"]
    if version == 2 and manifest.get("contract_version") != CONTRACT_VERSION:
        raise TransportError("unsupported contract version")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not 1 <= len(entries) < MAX_FILES:
        raise TransportError("unsupported manifest file count")
    declared = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TransportError("invalid manifest entry")
        name = _path(entry.get("path"))
        if name.casefold() in declared or name.casefold() == "manifest.json":
            raise TransportError("duplicate manifest path or self-coverage")
        declared.add(name.casefold())
        size, digest = entry.get("bytes"), entry.get("sha256")
        if type(size) is not int or size < 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            raise TransportError("invalid payload size or digest")
        if name not in blobs or size != len(blobs[name]) or digest.lower() != _sha(blobs[name]):
            raise TransportError("payload size or SHA-256 mismatch")
    if set(blobs) != {"manifest.json", *(entry["path"] for entry in entries)}:
        raise TransportError("manifest coverage mismatch")
    handoff = _json(blobs["handoff.json"])
    if not isinstance(handoff, dict) or type(handoff.get("schema_version")) is not int or handoff.get("schema_version") != version or handoff.get("id") != bid or handoff.get("sender") != sender or handoff.get("recipient") != recipient:
        raise TransportError("handoff schema, ID or sender/recipient mismatch")
    if version == 2 and handoff.get("contract_version") != CONTRACT_VERSION:
        raise TransportError("unsupported handoff contract version")
    payloads = handoff.get("payloads")
    if not isinstance(payloads, list) or len(payloads) > MAX_FILES - 2 or any(not isinstance(p, str) for p in payloads) or len({p.casefold() for p in payloads}) != len(payloads):
        raise TransportError("invalid handoff payload declarations")
    if set(blobs) != {"manifest.json", "handoff.json", *payloads}:
        raise TransportError("handoff payload coverage mismatch")
    content = {}
    for name, raw in blobs.items():
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError as exc:
            raise TransportError("non-UTF-8 payload") from exc
        if name.endswith(".json"):
            _json(raw)
        if name != "manifest.json":
            content[name] = text
    return {"id": bid, "manifest_sha256": _sha(blobs["manifest.json"]), "schema_version": version,
            "handoff": handoff, "file_count": len(blobs) - 1, "total_bytes": sum(map(len, blobs.values())),
            "content": content, "blobs": dict(blobs)}


def validate_legacy_bundle(bid: str, blobs: Mapping[str, bytes], sender: str, recipient: str) -> dict:
    result = validate_bundle(bid, blobs, sender, recipient)
    if result["schema_version"] != 1:
        raise TransportError("legacy migration requires original v1 bundle")
    return result


def read_directory(directory: Path) -> dict[str, bytes]:
    """Snapshot an outgoing directory without following symlinks/reparse points."""
    directory = Path(directory)
    _id(directory.name)
    result = {}
    total = 0
    count = 0
    pending = [directory]
    while pending:
        item = pending.pop()
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise TransportError("symlink or reparse point in outgoing bundle")
        if stat.S_ISDIR(info.st_mode):
            children = list(item.iterdir())
            count += len(children)
            if count > MAX_FILES * 3:
                raise TransportError("oversized outgoing directory inventory")
            pending.extend(children)
        elif stat.S_ISREG(info.st_mode):
            name = _path(item.relative_to(directory).as_posix())
            total += info.st_size
            if total > MAX_BUNDLE_BYTES or len(result) >= MAX_FILES:
                raise TransportError("oversized outgoing bundle")
            raw = item.read_bytes()
            after = item.lstat()
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or len(raw) != info.st_size:
                raise TransportError("outgoing file changed while snapshotting")
            result[name] = raw
        else:
            raise TransportError("non-regular outgoing bundle entry")
    return result


class GitTransport:
    """A private bare cache using native Git authentication and owned refs only.

    Hold ONE common exclusive native transport-cache lock across construction
    and each whole operation. Git-courier and inbox locks are insufficient when
    they differ: both roles share FETCH_HEAD, refs and the object database. The
    caller orders its role lock before this common cache lock consistently.
    Limit
    calls to ``publish``/``publish_prepared`` to ten per scheduler wake. ``scan``
    enforces that bound itself. Git data is read at a pinned immutable commit.
    """

    def __init__(self, cache: Path, remote: str, actor: str,
                 repository: str = "jeremysecondstate/ducketz", *, allow_local_remote: bool = False):
        self.cache = Path(cache).absolute()
        self.actor = _actor(actor)
        self.sender = _ROLES[self.actor]
        self.recipient = next(v for v in _ROLES.values() if v != self.sender)
        self.repository = repository
        self.branch = self._branch(self.actor)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise TransportError("invalid repository identity")
        allowed = {f"https://github.com/{repository}", f"https://github.com/{repository}.git",
                   f"git@github.com:{repository}", f"git@github.com:{repository}.git",
                   f"ssh://git@github.com/{repository}", f"ssh://git@github.com/{repository}.git"}
        if remote not in allowed:
            if not allow_local_remote or not Path(remote).is_absolute() or not Path(remote).is_dir():
                raise TransportError("remote must be the configured GitHub repository using native Git authentication")
        self.remote = remote
        self.cache.mkdir(parents=True, exist_ok=True)
        if not (self.cache / "HEAD").exists():
            if any(self.cache.iterdir()):
                raise TransportError("transport cache must be a private bare repository")
            self._git("init", "--bare", str(self.cache), outside=True)
        if self._git("rev-parse", "--is-bare-repository").strip() != b"true":
            raise TransportError("transport cache must be bare")
        current = self._git("remote", "get-url", "origin", check=False)
        if not current:
            self._git("remote", "add", "origin", remote)
        elif current.decode("utf-8").strip() != remote:
            raise TransportError("transport cache remote identity mismatch")

    @staticmethod
    def _branch(actor: str) -> str:
        return f"codex/{_actor(actor)}/coordination-v2"

    def _git(self, *args: str, data: bytes | None = None, check: bool = True,
             outside: bool = False, max_output: int = MAX_INVENTORY_BYTES,
             env_extra: dict | None = None) -> bytes:
        env = os.environ.copy()
        env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"})
        env.update(env_extra or {})
        command = ["git", "-c", f"core.hooksPath={self.cache / 'disabled-hooks'}", "-c", "commit.gpgSign=false"]
        if not outside:
            command += ["--git-dir", str(self.cache)]
        command.extend(args)
        try:
            # Files avoid buffering unbounded remote inventories/errors in RAM.
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                result = subprocess.run(command, input=data, stdout=stdout, stderr=stderr,
                                        env=env, timeout=60, check=False)
                if stdout.tell() > max_output:
                    raise TransportError("complete Git output exceeds bounded inventory; nothing accepted")
                stdout.seek(0)
                output = stdout.read(max_output + 1)
                if result.returncode:
                    if check:
                        # Do not echo Git stderr: helpers/remotes may emit account information.
                        raise TransportError(f"Git {args[0]} failed (exit {result.returncode}); stage remains pending")
                    return b""
                return output
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TransportError("Git operation unavailable or timed out; stage remains pending") from exc

    def _head(self, branch: str) -> str | None:
        output = self._git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
        if not output:
            return None
        lines = output.decode("ascii").splitlines()
        if len(lines) != 1:
            raise TransportError("ambiguous remote ref inventory")
        fields = lines[0].split("\t")
        if len(fields) != 2 or not _SHA.fullmatch(fields[0]) or fields[1] != f"refs/heads/{branch}":
            raise TransportError("remote branch identity mismatch")
        return fields[0]

    def _inventory(self, commit: str | None) -> dict[str, tuple[str, int]]:
        if commit is None:
            return {}
        if not _SHA.fullmatch(commit):
            raise TransportError("invalid commit identity")
        raw = self._git("ls-tree", "-r", "-l", "-z", "--full-tree", commit)
        if raw and not raw.endswith(b"\0"):
            raise TransportError("incomplete tree inventory")
        entries = raw.split(b"\0")[:-1]
        if len(entries) > MAX_INVENTORY_FILES:
            raise TransportError("transport tree inventory exceeds configured bound")
        result = {}
        folded = set()
        total = 0
        bundle_ids, wrappers = set(), set()
        for entry in entries:
            try:
                metadata, raw_path = entry.split(b"\t", 1)
                mode, kind, oid, size = metadata.split()
                path = _path(raw_path.decode("utf-8"))
                number = int(size)
                identity = oid.decode("ascii")
            except (ValueError, UnicodeError) as exc:
                raise TransportError("malformed tree inventory") from exc
            if mode != b"100644" or kind != b"blob" or not _SHA.fullmatch(identity) or number < 0 or number > MAX_BUNDLE_BYTES:
                raise TransportError("unsupported tree object, mode or size")
            if path.casefold() in folded:
                raise TransportError("duplicate tree path")
            folded.add(path.casefold())
            parts = path.split("/")
            if len(parts) >= 3 and parts[0] == "bundles":
                bundle_ids.add(_id(parts[1]))
            elif len(parts) == 2 and parts[0] == "provenance" and parts[1].endswith(".json"):
                wrappers.add(_id(parts[1][:-5]))
            else:
                raise TransportError("unexpected source/non-notice path in transport branch")
            result[path] = (identity, number)
            total += number
        if bundle_ids != wrappers:
            raise TransportError("missing or extra bundle provenance wrapper")
        if total > MAX_TRANSPORT_BYTES:
            raise TransportError("transport tree exceeds configured total byte bound")
        return result

    def _fetch(self, actor: str) -> tuple[str | None, dict]:
        branch = self._branch(actor)
        local_ref = f"refs/remotes/origin/{branch}"
        before_raw = self._git("rev-parse", "--verify", local_ref, check=False).strip()
        before = before_raw.decode("ascii") if before_raw else None
        remote = self._head(branch)
        if remote is None:
            if before is not None:
                raise TransportError("previously observed immutable coordination branch disappeared")
            return None, {}
        # Fetch to FETCH_HEAD, then validate before advancing the trusted cache ref.
        self._git("fetch", "--no-tags", "--refmap=", "origin", f"refs/heads/{branch}")
        commit = self._git("rev-parse", "--verify", "FETCH_HEAD").decode("ascii").strip()
        inventory = self._inventory(commit)
        if before:
            if self._git("merge-base", before, commit).decode("ascii").strip() != before:
                raise TransportError("coordination history rewrite detected")
            old = self._inventory(before)
            if any(inventory.get(path) != row for path, row in old.items()):
                raise TransportError("previously observed immutable bundle changed or disappeared")
        self._git("update-ref", local_ref, commit, before or "0" * len(commit))
        return commit, inventory

    def _blob(self, identity: str, size: int) -> bytes:
        value = self._git("cat-file", "blob", identity, max_output=MAX_BUNDLE_BYTES)
        if len(value) != size:
            raise TransportError("original Git blob length mismatch")
        algorithm = hashlib.sha1 if len(identity) == 40 else hashlib.sha256
        if algorithm(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest() != identity:
            raise TransportError("original Git blob identity mismatch")
        return value

    def _read(self, actor: str, bid: str, commit: str, inventory: dict) -> dict:
        actor = _actor(actor)
        _id(bid)
        prefix = f"bundles/{bid}/"
        selected = {path[len(prefix):]: row for path, row in inventory.items() if path.startswith(prefix)}
        if not 2 <= len(selected) <= MAX_FILES or sum(row[1] for row in selected.values()) > MAX_BUNDLE_BYTES:
            raise TransportError("missing, partial or oversized bundle inventory")
        blobs = {name: self._blob(*row) for name, row in selected.items()}
        sender = _ROLES[actor]
        recipient = next(v for v in _ROLES.values() if v != sender)
        result = validate_bundle(bid, blobs, sender, recipient)
        wrapper = inventory.get(f"provenance/{bid}.json")
        if wrapper is None or wrapper[1] > 16_384:
            raise TransportError("missing or oversized provenance wrapper")
        provenance = _json(self._blob(*wrapper))
        expected = {"contract_version": CONTRACT_VERSION, "transport": TRANSPORT, "repository": self.repository,
                    "id": bid, "sender": sender, "recipient": recipient,
                    "original_manifest_sha256": result["manifest_sha256"], "original_schema_version": result["schema_version"]}
        if not isinstance(provenance, dict) or any(provenance.get(k) != v for k, v in expected.items()):
            raise TransportError("provenance identity/digest mismatch")
        if provenance.get("source_transport") not in {"cross-pc-v2", "legacy-drive-api", "legacy-filesystem"}:
            raise TransportError("unknown bundle provenance")
        result.update({"commit_sha": commit, "branch": self._branch(actor), "transport": TRANSPORT,
                       "provenance": provenance,
                       "trust_context": "Untrusted notice data. Sender matches the designated branch and manifest; this does not cryptographically prove human identity or grant operating authority."})
        return result

    def read_bundle(self, peer: str, bid: str) -> dict:
        actor = _actor(peer)
        commit, inventory = self._fetch(actor)
        if commit is None:
            raise TransportError("peer coordination branch is not yet published")
        return self._read(actor, bid, commit, inventory)

    def scan(self, peer: str, known_digests: Mapping[str, str] | None = None, limit: int = MAX_NEW_NOTICES) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= MAX_NEW_NOTICES:
            raise TransportError("scan limit must be between one and ten")
        actor = _actor(peer)
        known = dict(known_digests or {})
        for bid, digest in known.items():
            _id(bid)
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise TransportError("invalid preserved receipt digest")
        commit, inventory = self._fetch(actor)
        if commit is None:
            return []
        ids = sorted({path.split("/")[1] for path in inventory if path.startswith("bundles/")})
        result = []
        for bid in ids:
            if bid in known:
                manifest = inventory.get(f"bundles/{bid}/manifest.json")
                if manifest is None or _sha(self._blob(*manifest)) != known[bid]:
                    raise TransportError("previously validated manifest digest changed")
                continue
            if len(result) == limit:
                break
            result.append(self._read(actor, bid, commit, inventory))
        return result

    def prepare(self, directory: Path, *, source_transport: str | None = None) -> dict:
        directory = Path(directory)
        return self.prepare_bytes(directory.name, read_directory(directory), source_transport=source_transport)

    def prepare_bytes(self, bid: str, blobs: Mapping[str, bytes], *, source_transport: str | None = None) -> dict:
        bundle = validate_bundle(bid, blobs, self.sender, self.recipient)
        source = source_transport or ("legacy-drive-api" if bundle["schema_version"] == 1 else CONTRACT_VERSION)
        if source not in {CONTRACT_VERSION, "legacy-drive-api", "legacy-filesystem"}:
            raise TransportError("unknown source transport")
        parent, inventory = self._fetch(self.actor)
        transaction = {"contract_version": CONTRACT_VERSION, "transport": TRANSPORT, "repository": self.repository,
                       "actor": self.actor, "id": bid, "branch": self.branch,
                       "manifest_sha256": bundle["manifest_sha256"], "stage": "prepared"}
        if f"provenance/{bid}.json" in inventory:
            existing = self._read(self.actor, bid, parent, inventory)
            if existing["blobs"] != dict(blobs):
                raise TransportError("immutable bundle ID already exists with different bytes")
            transaction.update({"commit_sha": parent, "already_present": True})
            return transaction
        provenance = {"contract_version": CONTRACT_VERSION, "transport": TRANSPORT, "repository": self.repository,
                      "id": bid, "sender": self.sender, "recipient": self.recipient,
                      "original_manifest_sha256": bundle["manifest_sha256"], "original_schema_version": bundle["schema_version"],
                      "source_transport": source}
        additions = {f"bundles/{bid}/{name}": raw for name, raw in blobs.items()}
        additions[f"provenance/{bid}.json"] = _encode(provenance)
        index_path = self.cache / ("index-" + uuid.uuid4().hex)
        environment = {"GIT_INDEX_FILE": str(index_path), "GIT_AUTHOR_NAME": self.actor.title(),
                       "GIT_AUTHOR_EMAIL": f"{self.actor}@cross-pc.invalid", "GIT_COMMITTER_NAME": "Ducketz coordination transport",
                       "GIT_COMMITTER_EMAIL": "coordination@cross-pc.invalid"}
        try:
            self._git("read-tree", parent or "--empty", env_extra=environment)
            records = []
            for name, raw in sorted(additions.items()):
                identity = self._git("hash-object", "-w", "--stdin", data=raw).strip()
                records.append(b"100644 " + identity + b"\t" + name.encode("utf-8") + b"\0")
            self._git("update-index", "-z", "--index-info", data=b"".join(records), env_extra=environment)
            tree = self._git("write-tree", env_extra=environment).decode("ascii").strip()
            args = ["commit-tree", tree]
            if parent:
                args += ["-p", parent]
            message = f"{self.actor.title()}: Publish coordination bundle {bid}\n\nContract: {CONTRACT_VERSION}\nManifest-SHA256: {bundle['manifest_sha256']}\nSource-Transport: {source}\n"
            commit = self._git(*args, data=message.encode(), env_extra=environment).decode("ascii").strip()
            new_inventory = self._inventory(commit)
            self._read(self.actor, bid, commit, new_inventory)
            self._git("update-ref", f"refs/prepared/{bid}", commit)
            transaction.update({"commit_sha": commit, "parent_sha": parent, "already_present": False})
            return transaction
        finally:
            # Only an exact local temporary index created by this operation.
            index_path.unlink(missing_ok=True)
            index_path.with_name(index_path.name + ".lock").unlink(missing_ok=True)

    def _verified(self, transaction: dict) -> dict | None:
        commit, inventory = self._fetch(self.actor)
        if commit is None or f"provenance/{transaction['id']}.json" not in inventory:
            return None
        result = self._read(self.actor, transaction["id"], commit, inventory)
        if result["manifest_sha256"] != transaction["manifest_sha256"]:
            raise TransportError("remote immutable bundle differs from prepared transaction")
        return {**transaction, "stage": "published", "prepared_commit_sha": transaction["commit_sha"],
                "commit_sha": commit, "verified_remote_sha": commit,
                "commit_url": f"https://github.com/{self.repository}/commit/{commit}"}

    def _prepared_bundle(self, transaction: dict) -> tuple[dict, dict]:
        if not isinstance(transaction, dict) or transaction.get("contract_version") != CONTRACT_VERSION or transaction.get("transport") != TRANSPORT or transaction.get("repository") != self.repository or transaction.get("actor") != self.actor or transaction.get("branch") != self.branch:
            raise TransportError("prepared transaction ownership mismatch")
        _id(transaction.get("id"))
        commit = transaction.get("commit_sha")
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            raise TransportError("invalid prepared commit identity")
        prepared_inventory = self._inventory(commit)
        local = self._read(self.actor, transaction["id"], commit, prepared_inventory)
        if local["manifest_sha256"] != transaction.get("manifest_sha256"):
            raise TransportError("prepared manifest digest mismatch")
        return local, prepared_inventory

    def reprepare(self, transaction: dict) -> dict:
        """Rebase a stale notice onto current remote using only frozen Git bytes.

        Invoke only after StalePrepared. Atomically save the previous transaction
        in receipt history and replace it with this result BEFORE publishing it.
        Uncertain push outcomes are recovered by prepare_bytes without rewriting
        an already published bundle. No live outgoing files are reread.
        """
        local, _ = self._prepared_bundle(transaction)
        replacement = self.prepare_bytes(transaction["id"], local["blobs"],
                                         source_transport=local["provenance"]["source_transport"])
        return {**replacement, "supersedes_commit_sha": transaction["commit_sha"], "reprepared": True}

    def publish_prepared(self, transaction: dict) -> dict:
        local, prepared_inventory = self._prepared_bundle(transaction)
        commit = transaction["commit_sha"]
        recovered = self._verified(transaction)
        if recovered:
            return {**recovered, "recovered": True}
        remote_commit, remote_inventory = self._fetch(self.actor)
        if any(path in prepared_inventory and prepared_inventory[path] != row for path, row in remote_inventory.items()):
            raise TransportError("prepared transaction changes published immutable bytes")
        if any(path not in prepared_inventory for path in remote_inventory):
            raise StalePrepared("remote advanced; reprepare exact cached bytes on the current remote tree")
        if remote_commit and self._git("merge-base", remote_commit, commit, check=False).decode("ascii").strip() != remote_commit:
            raise StalePrepared("remote ancestry advanced; reprepare exact cached bytes")
        expected_prefix = f"bundles/{transaction['id']}/"
        expected_wrapper = f"provenance/{transaction['id']}.json"
        if any(path not in remote_inventory and not path.startswith(expected_prefix) and path != expected_wrapper
               for path in prepared_inventory):
            raise TransportError("prepared transaction includes unrelated notice additions")
        try:
            self._git("push", "origin", f"{commit}:refs/heads/{self.branch}")
        except TransportError:
            # A disconnected push can have succeeded. Prove exact bytes remotely
            # before retrying; never force, invent success, or repeat Git work.
            try:
                recovered = self._verified(transaction)
            except TransportError as exc:
                raise DeliveryPending("push outcome unknown; retain prepared transaction") from exc
            if recovered:
                return {**recovered, "recovered": True}
            raise DeliveryPending("push unconfirmed; retain prepared transaction")
        try:
            receipt = self._verified(transaction)
        except TransportError as exc:
            raise DeliveryPending("push returned but remote verification is pending") from exc
        if receipt is None:
            raise DeliveryPending("pushed bundle is not visible; retain prepared transaction")
        return {**receipt, "recovered": False}

    def publish(self, directory: Path, *, source_transport: str | None = None) -> dict:
        """Convenience API; durable callers persist prepare() before pushing."""
        return self.publish_prepared(self.prepare(directory, source_transport=source_transport))
