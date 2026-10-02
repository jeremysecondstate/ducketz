"""Publish explicitly reviewed, completed artifacts to a machine namespace on main.

This is separate from the shared-source queue. It reads an exact local spec,
copies stable bytes into private snapshots, and only stages those snapshots in
an isolated Git worktree. It never stages the live output paths or ``.env``.
"""
from __future__ import annotations

from datetime import datetime
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

from . import core


class ArtifactPublishError(ValueError):
    """A candidate needs review or a publication attempt needs a safe retry."""


_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-z0-9][a-z0-9-]{7,63}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_SENSITIVE_NAME = re.compile(r"KEY|TOKEN|SECRET|PASS|CREDENTIAL|AUTH|PRIVATE", re.I)
_CREDENTIAL_PATH = re.compile(r"(?:^|[._-])(?:secrets?|credentials?|passwords?|api[_-]?keys?|private[_-]?keys?)(?:$|[._-])", re.I)
_CHUNK = 1024 * 1024
_COMPRESSION_THRESHOLD = 50 * 1024 * 1024
_MAX_FILES = 200
_MAX_BYTES = 2 * 1024 * 1024 * 1024
_MAX_ENV_BYTES = 1024 * 1024
_MAX_ENV_ENTRIES = 4096
_MAX_ENV_VALUE_BYTES = 64 * 1024
_MAX_ENV_ALIAS_DEPTH = 16
_ENV_ALIAS = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")


def _fail(message: str):
    raise ArtifactPublishError(message)


def _git(root: Path, *args: str, data: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-c", "core.longpaths=true", "-c", "commit.gpgsign=false", *args], cwd=root, input=data,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
    )
    if check and result.returncode:
        # Git stderr can contain remote credentials or local paths.
        _fail("git " + args[0] + " failed; receipt remains available for review")
    return result


def _text(root: Path, *args: str) -> str:
    return _git(root, *args).stdout.decode("utf-8", "strict").strip()


def _hash_object(worktree: Path, source: Path) -> str:
    # On Windows a full snapshot path can exceed Git's argv path limit. Feed
    # the already reviewed bytes over stdin without loading large files into RAM.
    with source.open("rb") as reader:
        result = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", "hash-object", "-w", "--stdin"],
            cwd=worktree, stdin=reader, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=180,
        )
    if result.returncode:
        _fail("git hash-object failed; receipt remains available for review")
    return result.stdout.decode("ascii").strip()


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _utc(value: str) -> float:
    if not isinstance(value, str) or not value.endswith("Z"):
        _fail("completed_at_utc must be a UTC timestamp")
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00").timestamp()
    except ValueError:
        _fail("invalid completed_at_utc")


def _path(value: str) -> str:
    try:
        core.relative(value)
    except ValueError:
        _fail("unsafe repository-relative artifact source path")
    parts = value.split("/")
    if len(parts) >= 2 and parts[0].casefold() == "artifacts" and parts[1].casefold() in {"pc-original", "pc-new"}:
        _fail("already-published machine namespace cannot be a source")
    if any(part.lower() in {".git", "scratch", "secrets", "credentials"} or _CREDENTIAL_PATH.search(part) or
           part.lower().startswith(".env") or part.lower().endswith((".key", ".pem", ".p12", ".pfx", ".p8", ".jks"))
           for part in parts):
        _fail("credential or private path cannot be published")
    return value


def _destination(profile: dict, completion_id: str, source: str, compression: str) -> str:
    parts = source.split("/")
    relative = "/".join(parts[1:]) if len(parts) > 1 and parts[0].casefold() == "artifacts" else source
    result = f'artifacts/{profile["machine"]}/{completion_id}/' + relative
    return result + (".gz" if compression == "gzip" else "")


def _source_file(root: Path, name: str) -> Path:
    path = core.safe_path(root, _path(name))
    if not path.is_file() or not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
        _fail("artifact source is not a regular file")
    return path


def _secret_values(env_path: Path) -> list[bytes]:
    """Scan literals and bounded whole-value aliases defined in this file only."""
    if not env_path.is_file() or env_path.is_symlink():
        _fail("local .env is required for private-value scanning")
    try:
        with env_path.open("rb") as reader:
            encoded = reader.read(_MAX_ENV_BYTES + 1)
        if len(encoded) > _MAX_ENV_BYTES:
            _fail("local .env exceeds the private-scan size limit")
        raw = encoded.decode("utf-8-sig")
    except (OSError, UnicodeError):
        _fail("local .env cannot be scanned")
    entries: dict[str, str] = {}
    keys: set[str] = set()
    aliases: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            _fail("local .env contains a malformed entry")
        key, value = stripped.split("=", 1)
        key, value = key.strip(), value.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            _fail("local .env contains a malformed key")
        if key.casefold() in keys:
            _fail("local .env contains ambiguous duplicate keys")
        if len(entries) >= _MAX_ENV_ENTRIES:
            _fail("local .env exceeds the private-scan entry limit")
        if len(value) >= 2 and value[:1] in {'"', "'"} and value[-1:] == value[:1]:
            value = value[1:-1]
        elif value[:1] in {'"', "'"} or value[-1:] in {'"', "'"}:
            _fail("local .env contains an unterminated quoted value")
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        if len(value.encode("utf-8")) > _MAX_ENV_VALUE_BYTES:
            _fail("local .env exceeds the private-scan value limit")
        alias = _ENV_ALIAS.fullmatch(value)
        if "${" in value and alias is None:
            _fail("local .env contains an unsupported credential reference")
        keys.add(key.casefold())
        entries[key] = value
        if alias:
            aliases[key] = alias.group(1)
    values: set[bytes] = set()
    for key in entries:
        cursor = key
        visited: set[str] = set()
        while cursor in aliases:
            if cursor in visited:
                _fail("local .env contains a cyclic credential reference")
            if len(visited) >= _MAX_ENV_ALIAS_DEPTH:
                _fail("local .env credential reference exceeds the depth limit")
            visited.add(cursor)
            cursor = aliases[cursor]
            if cursor not in entries:
                _fail("local .env contains an unresolved credential reference")
        value = entries[cursor]
        if visited and not value:
            _fail("local .env contains an empty credential reference")
        if _SENSITIVE_NAME.search(key) and (visited or len(value) >= 4):
            values.add(value.encode("utf-8"))
    return sorted(values, key=len, reverse=True)


def _scan_bytes(raw: bytes, secrets: list[bytes]):
    if any(value in raw for value in secrets):
        _fail("candidate contains an exact local .env value")


def _scan_file(path: Path, secrets: list[bytes]):
    overlap = b""
    longest = max((len(value) for value in secrets), default=1)
    with path.open("rb") as reader:
        for chunk in iter(lambda: reader.read(_CHUNK), b""):
            _scan_bytes(overlap + chunk, secrets)
            overlap = (overlap + chunk)[-longest + 1:] if longest > 1 else b""


def _copy_scanned(source: Path, destination: Path, secrets: list[bytes]) -> tuple[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    if temporary.exists():
        _fail("partial artifact snapshot exists; review before retry")
    before = source.stat()
    digest = hashlib.sha256()
    size = 0
    overlap = b""
    longest = max((len(value) for value in secrets), default=1)
    try:
        with source.open("rb") as reader, temporary.open("xb") as writer:
            for chunk in iter(lambda: reader.read(_CHUNK), b""):
                _scan_bytes(overlap + chunk, secrets)
                overlap = (overlap + chunk)[-longest + 1:] if longest > 1 else b""
                digest.update(chunk)
                size += len(chunk)
                if size > _MAX_BYTES:
                    _fail("artifact snapshot exceeds size limit")
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            _fail("artifact changed while snapshotting")
        second_hash, second_size = _hash_file(source)
        if (second_hash, second_size) != (digest.hexdigest(), size):
            _fail("artifact changed after snapshotting")
        os.replace(temporary, destination)
        return digest.hexdigest(), size
    finally:
        temporary.unlink(missing_ok=True)


def _compress_snapshot(source: Path, destination: Path, original_sha: str, original_size: int) -> tuple[str, int]:
    """Write a reproducible gzip member and prove it expands to exact source bytes."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", compresslevel=6, mtime=0, fileobj=raw) as writer:
            for chunk in iter(lambda: reader.read(_CHUNK), b""):
                writer.write(chunk)
        raw.flush()
        os.fsync(raw.fileno())
    unpacked = hashlib.sha256()
    size = 0
    with gzip.open(destination, "rb") as reader:
        for chunk in iter(lambda: reader.read(_CHUNK), b""):
            unpacked.update(chunk)
            size += len(chunk)
    if (unpacked.hexdigest(), size) != (original_sha, original_size):
        _fail("compressed artifact does not reproduce reviewed bytes")
    compressed_sha, compressed_size = _hash_file(destination)
    if compressed_size >= 100 * 1024 * 1024:
        _fail("compressed artifact exceeds GitHub's individual file limit")
    return compressed_sha, compressed_size


def _metadata(spec: dict, profile: dict, secrets: list[bytes]) -> dict:
    if spec.get("reviewed") is not True or spec.get("completed") is not True:
        _fail("artifact work must be completed and explicitly reviewed")
    completion_id = spec.get("completion_id")
    if not isinstance(completion_id, str) or not _ID.fullmatch(completion_id):
        _fail("invalid immutable completion_id")
    producer = spec.get("producer")
    if producer not in profile["authorized_producers"]:
        _fail("producer is outside this PC's existing authority")
    if spec.get("scope") not in {"shared-analysis", "symbol-specific", "machine-local"}:
        _fail("scope must identify sharing and symbol dependence")
    symbols = spec.get("symbols")
    if not isinstance(symbols, list) or any(not isinstance(s, str) or s not in profile["symbols"] for s in symbols) or len(set(symbols)) != len(symbols):
        _fail("symbols must be a unique subset of this PC's profile")
    if spec["scope"] == "symbol-specific" and not symbols:
        _fail("symbol-specific work must name its symbols")
    if spec["scope"] != "symbol-specific" and symbols:
        _fail("only symbol-specific work may name symbols")
    summary = spec.get("summary")
    details = spec.get("details")
    if not isinstance(summary, str) or not 10 <= len(summary) <= 140 or "\n" in summary:
        _fail("summary must be a concrete single line")
    if not isinstance(details, list) or not details or any(not isinstance(d, str) or not 8 <= len(d) <= 500 for d in details):
        _fail("details must describe the completed changes")
    completed_at = spec.get("completed_at_utc")
    finished = _utc(completed_at)
    if finished > time.time() + 2:
        _fail("completion time lies in the future")
    paths = spec.get("paths")
    if not isinstance(paths, list) or not 1 <= len(paths) <= _MAX_FILES:
        _fail("spec must list one to 200 exact artifact paths")
    normalized = []
    seen = set()
    destinations = set()
    for entry in paths:
        if not isinstance(entry, dict) or set(entry) - {"path", "operation", "compression", "supersedes", "owned", "expected_sha256"}:
            _fail("artifact entry has unsupported fields")
        if entry.get("owned") is not True:
            _fail("every artifact path needs explicit task ownership")
        name = _path(entry.get("path"))
        if name.casefold() in seen:
            _fail("duplicate artifact path")
        seen.add(name.casefold())
        operation = entry.get("operation")
        if operation not in {"add", "revision", "delete"}:
            _fail("artifact operation must be add, revision or delete")
        compression = entry.get("compression", "none")
        if compression not in {"none", "gzip"} or (operation == "delete" and compression != "none"):
            _fail("invalid artifact compression")
        destination = _destination(profile, completion_id, name, compression)
        if destination.casefold() in destinations:
            _fail("different source paths map to the same published destination")
        destinations.add(destination.casefold())
        expected_sha = entry.get("expected_sha256")
        if operation != "delete" and (not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)):
            _fail("each completed artifact needs an exact reviewed SHA-256")
        if operation == "delete" and expected_sha is not None:
            _fail("deleted artifact cannot declare present bytes")
        supersedes = entry.get("supersedes")
        if operation in {"revision", "delete"}:
            if not isinstance(supersedes, dict) or set(supersedes) != {"receipt_id", "destination", "published_sha256"}:
                _fail("revision/deletion requires an exact prior receipt and destination hash")
            if not isinstance(supersedes["receipt_id"], str) or not _ID.fullmatch(supersedes["receipt_id"]):
                _fail("invalid prior receipt ID")
            core.relative(supersedes["destination"])
            if not supersedes["destination"].startswith(f'artifacts/{profile["machine"]}/'):
                _fail("cannot supersede a peer artifact namespace")
            if not isinstance(supersedes["published_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", supersedes["published_sha256"]):
                _fail("invalid prior destination hash")
        elif supersedes is not None:
            _fail("new artifact entry cannot supersede a prior publication")
        normalized.append({"path": name, "operation": operation, "compression": compression,
                           "supersedes": supersedes, "owned": True, "expected_sha256": expected_sha})
    metadata = {
        "schema": "cross-pc-public-artifacts-v1", "completion_id": completion_id,
        "actor": profile["actor"], "machine": profile["machine"],
        "producer": producer, "scope": spec["scope"], "symbols": symbols,
        "summary": summary, "details": details, "completed_at_utc": completed_at,
        "paths": normalized,
    }
    _scan_bytes(core.encoded(metadata), secrets)
    return metadata


def _receipt_path(profile: dict, completion_id: str) -> Path:
    return Path(profile["evidence_root"]) / "artifact-publication" / "receipts" / (completion_id + ".json")


def _snapshot_root(profile: dict, completion_id: str) -> Path:
    return Path(profile["evidence_root"]) / "artifact-publication" / "snapshots" / completion_id


def _save_receipt(path: Path, receipt: dict):
    core.atomic(path, receipt)


def snapshot(profile: dict, source_root: str | Path, spec: dict, env_path: str | Path | None = None, *, quiet_seconds: int = 30) -> dict:
    """Copy only declared completed output bytes into a private immutable record."""
    core.validate_profile(profile)
    source_root = Path(source_root).resolve()
    if source_root != Path(profile["checkout"]).resolve():
        _fail("artifact source must be this PC's configured checkout")
    if not isinstance(quiet_seconds, int) or quiet_seconds < 30:
        _fail("invalid quiet period")
    secrets = _secret_values(Path(env_path) if env_path else source_root / ".env")
    metadata = _metadata(spec, profile, secrets)
    completion_id = metadata["completion_id"]
    path = _receipt_path(profile, completion_id)
    spec_sha = hashlib.sha256(core.encoded(metadata)).hexdigest()
    lock = path.with_suffix(".lock")
    with core.exclusive(lock):
        if path.exists():
            existing = core.load(path)
            if existing.get("spec_sha256") != spec_sha or existing.get("source_root") != str(source_root):
                _fail("completion ID already has different immutable content")
            _verify_snapshot(existing, profile)
            return existing
        root = _snapshot_root(profile, completion_id)
        if root.exists():
            _fail("snapshot directory exists without a receipt; review before retry")
        root.mkdir(parents=True)
        files = []
        total = 0
        try:
            for entry in metadata["paths"]:
                name = entry["path"]
                destination = _destination(profile, completion_id, name, entry["compression"])
                if entry["operation"] == "delete":
                    if core.safe_path(source_root, name).exists():
                        _fail("deleted artifact path still exists")
                    files.append({**entry, "destination": None, "sha256": None, "size": 0,
                                  "published_sha256": None, "published_size": 0})
                    continue
                original = _source_file(source_root, name)
                before = original.stat()
                if before.st_mtime > _utc(metadata["completed_at_utc"]) + 0.001 or time.time() - before.st_mtime < quiet_seconds:
                    _fail("artifact is still inside the active-write guard")
                target = core.safe_path(root, name)
                digest, size = _copy_scanned(original, target, secrets)
                if digest != entry["expected_sha256"]:
                    _fail("artifact differs from its reviewed inventory hash")
                total += size
                if total > _MAX_BYTES:
                    _fail("artifact snapshot exceeds size limit")
                if size >= _COMPRESSION_THRESHOLD and entry["compression"] != "gzip":
                    _fail("large artifact requires deterministic gzip compression")
                if entry["compression"] == "gzip":
                    compressed = core.safe_path(root, "compressed/" + name + ".gz")
                    published_sha, published_size = _compress_snapshot(target, compressed, digest, size)
                    _scan_file(compressed, secrets)
                else:
                    published_sha, published_size = digest, size
                files.append({**entry, "destination": destination, "sha256": digest, "size": size,
                              "published_sha256": published_sha, "published_size": published_size})
            manifest = {**metadata, "files": files, "source_head": _text(source_root, "rev-parse", "HEAD")}
            _scan_bytes(core.encoded(manifest), secrets)
            manifest_path = root / "artifact-manifest.json"
            manifest_path.write_bytes(core.encoded(manifest))
            receipt = {
                "id": completion_id, "stage": "snapshotted", "spec_sha256": spec_sha,
                "source_root": str(source_root), "snapshot_root": str(root),
                "manifest_sha256": _hash_file(manifest_path)[0], "metadata": metadata,
                "files": files, "created_at_utc": core.now(), "attempts": [],
            }
            _save_receipt(path, receipt)
            return receipt
        except BaseException:
            expected_parent = (Path(profile["evidence_root"]) / "artifact-publication" / "snapshots").resolve()
            if not path.exists() and root.resolve().parent == expected_parent:
                shutil.rmtree(root, ignore_errors=True)
            raise


def _verify_snapshot(receipt: dict, profile: dict):
    if receipt.get("metadata", {}).get("actor") != profile["actor"] or receipt.get("metadata", {}).get("machine") != profile["machine"]:
        _fail("snapshot identity differs from local profile")
    if Path(receipt["source_root"]).resolve() != Path(profile["checkout"]).resolve() or Path(receipt["snapshot_root"]).resolve() != _snapshot_root(profile, receipt["id"]).resolve():
        _fail("snapshot paths differ from this PC's private evidence root")
    root = Path(receipt["snapshot_root"])
    manifest = root / "artifact-manifest.json"
    if not manifest.is_file() or _hash_file(manifest)[0] != receipt["manifest_sha256"]:
        _fail("artifact manifest changed after snapshot")
    secrets = _secret_values(Path(receipt["source_root"]) / ".env")
    _scan_file(manifest, secrets)
    for item in receipt["files"]:
        if item["operation"] != "delete":
            file = core.safe_path(root, item["path"])
            if not file.is_file() or _hash_file(file) != (item["sha256"], item["size"]):
                _fail("artifact snapshot changed after review")
            _scan_file(file, secrets)
            if item["compression"] == "gzip":
                compressed = core.safe_path(root, "compressed/" + item["path"] + ".gz")
                if not compressed.is_file() or _hash_file(compressed) != (item["published_sha256"], item["published_size"]):
                    _fail("compressed artifact snapshot changed after review")
                _scan_file(compressed, secrets)


def _verify_live(receipt: dict, quiet_seconds: int):
    source_root = Path(receipt["source_root"])
    completed = _utc(receipt["metadata"]["completed_at_utc"])
    for item in receipt["files"]:
        source = core.safe_path(source_root, item["path"])
        if item["operation"] == "delete":
            if source.exists():
                _fail("deleted artifact was recreated after completion")
            continue
        if not source.is_file():
            _fail("artifact source disappeared after snapshot")
        before = source.stat()
        if before.st_mtime > completed + 0.001 or time.time() - before.st_mtime < quiet_seconds:
            _fail("artifact is still inside the active-write guard")
        if _hash_file(source) != (item["sha256"], item["size"]):
            _fail("artifact source changed after snapshot")
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            _fail("artifact source changed during verification")


def _review_history(profile: dict, receipt: dict) -> list[dict] | None:
    """Require explicit latest-receipt lineage; return prior records for a no-op."""
    directory = _receipt_path(profile, receipt["id"]).parent
    previous: dict[str, tuple[dict, dict]] = {}
    for path in directory.glob("*.json"):
        if path.name == receipt["id"] + ".json":
            continue
        candidate = core.load(path)
        if candidate.get("stage") != "pushed" or candidate.get("metadata", {}).get("machine") != profile["machine"]:
            continue
        for old in candidate.get("files", []):
            name = old.get("path")
            if name and (name not in previous or candidate.get("pushed_at_utc", "") > previous[name][0].get("pushed_at_utc", "")):
                previous[name] = (candidate, old)
    duplicates = []
    for item in receipt["files"]:
        prior = previous.get(item["path"])
        if prior is None:
            if item["operation"] != "add":
                _fail("revision/deletion has no published prior receipt")
            continue
        record, old = prior
        if item["operation"] == "add":
            if item["sha256"] != old.get("sha256") or old.get("operation") == "delete":
                _fail("artifact path changed since prior publication; use exact revision")
            duplicates.append({"receipt_id": record["id"], "destination": old["destination"],
                               "published_sha256": old["published_sha256"]})
            continue
        expected = {"receipt_id": record["id"], "destination": old.get("destination"),
                    "published_sha256": old.get("published_sha256")}
        if old.get("operation") == "delete" or item.get("supersedes") != expected:
            _fail("revision/deletion does not match latest published destination")
        if item["operation"] == "revision" and item["sha256"] == old.get("sha256"):
            duplicates.append(expected)
    return duplicates if len(duplicates) == len(receipt["files"]) else None


def _verify_superseded_base(worktree: Path, receipt: dict, base: str):
    for item in receipt["files"]:
        prior = item.get("supersedes")
        if prior is None:
            continue
        result = _git(worktree, "cat-file", "blob", f'{base}:{prior["destination"]}', check=False)
        if result.returncode or hashlib.sha256(result.stdout).hexdigest() != prior["published_sha256"]:
            _fail("prior published artifact differs from reviewed revision base")


def _verify_deduplicated_base(worktree: Path, receipt: dict, base: str):
    for prior in receipt.get("deduplicated_from", []):
        result = _git(worktree, "cat-file", "blob", f'{base}:{prior["destination"]}', check=False)
        if result.returncode or hashlib.sha256(result.stdout).hexdigest() != prior["published_sha256"]:
            _fail("previous identical artifact is no longer on main")


def _remote_main(worktree: Path) -> str:
    lines = _text(worktree, "ls-remote", "--heads", "origin", "refs/heads/main").splitlines()
    if len(lines) != 1:
        _fail("origin/main is missing or ambiguous")
    sha = lines[0].split()[0]
    if not _SHA.fullmatch(sha):
        _fail("origin/main SHA is invalid")
    return sha


def _commit_message(receipt: dict) -> bytes:
    metadata = receipt["metadata"]
    subject = f'{metadata["actor"]} artifacts: {metadata["summary"]}'
    lines = [subject, "", f'Completion-ID: {receipt["id"]}', f'Producer: {metadata["producer"]}',
             f'Machine: {metadata["machine"]}', f'Scope: {metadata["scope"]}',
             'Symbols: ' + (", ".join(metadata["symbols"]) or "none"),
             f'Completed-At-UTC: {metadata["completed_at_utc"]}', "", "Changes:"]
    lines += ["- " + detail for detail in metadata["details"]]
    lines += ["", "Published paths:"]
    for item in receipt["files"]:
        lines.append(f'- {item["operation"]}: {item["destination"] or item["path"]} '
                     f'(original_sha256={item["sha256"] or "deleted"}, original_bytes={item["size"]}, '
                     f'compression={item["compression"]})')
    return ("\n".join(lines) + "\n").encode("utf-8")


def _verify_commit(worktree: Path, receipt: dict, sha: str, base: str):
    if _text(worktree, "rev-parse", sha + "^") != base:
        _fail("artifact commit does not extend the saved main base")
    prefix = f'artifacts/{receipt["metadata"]["machine"]}/{receipt["id"]}'
    expected = {prefix + "/artifact-manifest.json"}
    expected.update(item["destination"] for item in receipt["files"] if item["destination"])
    changed = set(_git(worktree, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha).stdout.decode("utf-8").rstrip("\0").split("\0"))
    if changed != expected:
        _fail("artifact commit contains paths outside the reviewed snapshot")
    manifest = _git(worktree, "cat-file", "blob", sha + ":" + prefix + "/artifact-manifest.json").stdout
    if hashlib.sha256(manifest).hexdigest() != receipt["manifest_sha256"]:
        _fail("artifact commit manifest differs from reviewed snapshot")
    for item in receipt["files"]:
        if item["destination"]:
            raw = _git(worktree, "cat-file", "blob", sha + ":" + item["destination"]).stdout
            if hashlib.sha256(raw).hexdigest() != item["published_sha256"]:
                _fail("artifact commit bytes differ from reviewed snapshot")


def _prepare_commit(profile: dict, receipt: dict, worktree: Path, base: str) -> str:
    _git(worktree, "fetch", "--no-tags", "origin", "main")
    if _text(worktree, "rev-parse", "FETCH_HEAD") != base:
        _fail("origin/main advanced during candidate preparation")
    _verify_superseded_base(worktree, receipt, base)
    _git(worktree, "switch", "--detach", base)
    root = Path(receipt["snapshot_root"])
    target_root = f'artifacts/{profile["machine"]}/{receipt["id"]}'
    intended = set()
    for item in receipt["files"]:
        target = item["destination"]
        if item["operation"] == "delete":
            continue
        exists = _git(worktree, "cat-file", "-e", f"{base}:{target}", check=False).returncode == 0
        if exists:
            _fail("namespaced destination already exists on main")
        relative_source = "compressed/" + item["path"] + ".gz" if item["compression"] == "gzip" else item["path"]
        source = core.safe_path(root, relative_source)
        destination = core.safe_path(worktree, target)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        blob = _hash_object(worktree, source)
        _git(worktree, "update-index", "--add", "--cacheinfo", "100644", blob, target)
        intended.add(target)
    manifest_target = target_root + "/artifact-manifest.json"
    if _git(worktree, "cat-file", "-e", f"{base}:{manifest_target}", check=False).returncode == 0:
        _fail("namespaced manifest already exists on main")
    manifest_source = root / "artifact-manifest.json"
    manifest_dest = core.safe_path(worktree, manifest_target)
    manifest_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(manifest_source, manifest_dest)
    manifest_blob = _hash_object(worktree, manifest_source)
    _git(worktree, "update-index", "--add", "--cacheinfo", "100644", manifest_blob, manifest_target)
    intended.add(manifest_target)
    staged = set(_git(worktree, "diff", "--cached", "--name-only", "-z").stdout.decode("utf-8").rstrip("\0").split("\0"))
    if staged != intended:
        _fail("candidate stages unexpected files")
    message = _commit_message(receipt)
    secrets = _secret_values(Path(receipt["source_root"]) / ".env")
    _scan_bytes(message, secrets)
    _git(worktree, "-c", "core.hooksPath=NUL", "commit", "--no-verify", "-F", "-", data=message)
    sha = _text(worktree, "rev-parse", "HEAD")
    _verify_commit(worktree, receipt, sha, base)
    return sha


def publish(profile: dict, completion_id: str, worktree: str | Path, *, quiet_seconds: int = 30) -> dict:
    """Fast-forward the exact namespaced artifact commit to main, then read back SHA."""
    core.validate_profile(profile)
    if not isinstance(completion_id, str) or not _ID.fullmatch(completion_id):
        _fail("invalid completion ID")
    path = _receipt_path(profile, completion_id)
    if not path.is_file():
        _fail("artifact snapshot receipt is missing")
    worktree = Path(worktree).resolve()
    reported_root = Path(_text(worktree, "rev-parse", "--show-toplevel")).resolve()
    if worktree == Path(profile["checkout"]).resolve() or reported_root != worktree:
        _fail("publication requires a separate isolated Git worktree")
    if not (worktree / ".git").is_file():
        _fail("publication requires a linked Git worktree")
    origin = _text(worktree, "remote", "get-url", "origin")
    if origin not in {"https://github.com/jeremysecondstate/ducketz.git", "git@github.com:jeremysecondstate/ducketz.git"} and not profile.get("test_remote"):
        _fail("unexpected origin")
    with core.exclusive(profile["git_lock"]):
        with core.exclusive(path.with_suffix(".lock")):
            receipt = core.load(path)
            if receipt.get("id") != completion_id or receipt.get("metadata", {}).get("machine") != profile["machine"]:
                _fail("artifact receipt identity differs from this PC")
            remote = _remote_main(worktree)
            if receipt["stage"] == "pushed":
                _git(worktree, "fetch", "--no-tags", "origin", "main")
                if _git(worktree, "merge-base", "--is-ancestor", receipt["commit_sha"], remote, check=False).returncode:
                    _fail("published artifact commit is no longer on main")
                receipt["remote_sha"] = remote
                _save_receipt(path, receipt)
                return receipt
            if receipt["stage"] == "deduplicated":
                _git(worktree, "fetch", "--no-tags", "origin", "main")
                _verify_deduplicated_base(worktree, receipt, remote)
                return receipt
            _verify_snapshot(receipt, profile)
            _verify_live(receipt, quiet_seconds)
            if _text(worktree, "status", "--porcelain"):
                _fail("isolated worktree has unrelated changes")
            # A process can stop after creating the exact commit and before
            # recording its SHA. Reconcile that local stage instead of
            # creating a second commit from the same immutable snapshot.
            if receipt["stage"] == "snapshotted":
                head = _text(worktree, "rev-parse", "HEAD")
                parent = _git(worktree, "rev-parse", head + "^", check=False)
                if parent.returncode == 0:
                    recovered_base = parent.stdout.decode("ascii").strip()
                    try:
                        _verify_commit(worktree, receipt, head, recovered_base)
                    except ArtifactPublishError:
                        pass
                    else:
                        receipt.update(stage="committed", base_sha=recovered_base,
                                       commit_sha=head, committed_at_utc=core.now())
                        _save_receipt(path, receipt)
            for _ in range(3):
                remote = _remote_main(worktree)
                if receipt["stage"] == "committed" and receipt.get("commit_sha"):
                    _git(worktree, "fetch", "--no-tags", "origin", "main")
                    if _git(worktree, "merge-base", "--is-ancestor", receipt["commit_sha"], remote, check=False).returncode == 0:
                        receipt.update(stage="pushed", remote_sha=remote, pushed_at_utc=core.now())
                        _save_receipt(path, receipt)
                        return receipt
                    if remote == receipt.get("base_sha"):
                        sha = receipt["commit_sha"]
                        if _git(worktree, "cat-file", "-e", sha, check=False).returncode or _text(worktree, "rev-parse", sha + "^") != remote:
                            _fail("saved artifact commit is unavailable or has a different base")
                        _verify_commit(worktree, receipt, sha, remote)
                    else:
                        receipt["attempts"].append({"base_sha": receipt["base_sha"], "commit_sha": receipt["commit_sha"], "result": "main advanced before push"})
                        receipt["stage"] = "snapshotted"
                        _save_receipt(path, receipt)
                if receipt["stage"] == "snapshotted":
                    duplicates = _review_history(profile, receipt)
                    if duplicates is not None:
                        receipt.update(stage="deduplicated", deduplicated_from=duplicates,
                                       remote_sha=remote, deduplicated_at_utc=core.now())
                        _save_receipt(path, receipt)
                        return receipt
                    _verify_live(receipt, quiet_seconds)
                    sha = _prepare_commit(profile, receipt, worktree, remote)
                    receipt.update(stage="committed", base_sha=remote, commit_sha=sha, committed_at_utc=core.now())
                    _save_receipt(path, receipt)
                elif receipt["stage"] != "committed":
                    _fail("invalid artifact receipt stage")
                _verify_snapshot(receipt, profile)
                _verify_live(receipt, quiet_seconds)
                outcome = _git(worktree, "push", "origin", sha + ":refs/heads/main", check=False)
                observed = _remote_main(worktree)
                _git(worktree, "fetch", "--no-tags", "origin", "main")
                if _git(worktree, "merge-base", "--is-ancestor", sha, observed, check=False).returncode == 0:
                    receipt.update(stage="pushed", remote_sha=observed, pushed_at_utc=core.now())
                    _save_receipt(path, receipt)
                    return receipt
                if outcome.returncode == 0:
                    _fail("push returned success but remote SHA did not match")
                if observed == receipt["base_sha"]:
                    _fail("push failed; committed receipt remains pending for retry")
                # Another writer can win the race. The next attempt replays the
                # same reviewed snapshots onto the new fast-forward main base.
                receipt["attempts"].append({"base_sha": receipt["base_sha"], "commit_sha": sha, "result": "non-fast-forward"})
                receipt["stage"] = "snapshotted"
                _save_receipt(path, receipt)
                _git(worktree, "reset", "--hard", check=False)
            _fail("main advanced repeatedly; immutable receipt remains pending")
