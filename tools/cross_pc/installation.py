"""Pinned immutable coordination releases; application code is never installed."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import uuid

from . import VERSION
from .core import atomic, digest, encoded, exclusive, git, git_text, load, relative, safe_path


def _safe_location(path):
    """Check lexical ancestors before resolving so a linked root cannot hide."""
    path = Path(path)
    if ".." in path.parts:
        raise ValueError("installation path cannot contain parent traversal")
    path = Path(os.path.abspath(path))
    for item in [*reversed(path.parents), path]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise ValueError("symlink/reparse installation path forbidden: " + str(item))
    return path


def _regular_file(path):
    path = _safe_location(path)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("installation entry must be a regular file: " + str(path))
    return path


def release_paths(repository, commit):
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("an exact immutable commit is required")
    names = git(repository, "ls-tree", "-r", "-z", "--name-only", commit).stdout.decode("utf-8").split("\0")
    paths = [p for p in names if p == "AGENTS.md" or p.startswith(("tools/cross_pc/", "coordination/"))
             or p == "tools/__init__.py" or (p.startswith("docs/development/cross-pc") and p.endswith(".md"))]
    if not {"coordination/contract.json", "coordination/task-catalog.json", "tools/cross_pc/cli.py"} <= set(paths):
        raise ValueError("incomplete infrastructure release")
    for name in paths:
        relative(name)
    return paths


def _active(destination, *, verify_release=True):
    path = _safe_location(destination / "active.json")
    if not path.exists():
        return None
    value = load(_regular_file(path))
    _verify_pointer(destination, value, verify_release=verify_release)
    return value


def _verify_pointer(destination, value, *, verify_release=True):
    if not isinstance(value, dict):
        raise ValueError("invalid active installation pointer; preserve for inspection")
    if value.get("contract_version") != VERSION or not isinstance(value.get("commit"), str) or not re.fullmatch(r"[0-9a-f]{40}", value["commit"]):
        raise ValueError("invalid active installation pointer; preserve for inspection")
    expected = destination / "releases" / value["commit"]
    if _safe_location(value.get("release_root", "")) != expected:
        raise ValueError("active release path differs from pinned local destination")
    if not isinstance(value.get("manifest_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", value["manifest_sha256"]):
        raise ValueError("active installation has no valid manifest fingerprint")
    if verify_release:
        verify(expected, value["manifest_sha256"])


def install(repository, commit, destination):
    destination = _safe_location(destination)
    contents = {}
    for name in release_paths(repository, commit):
        mode = git_text(repository, "ls-tree", commit, "--", name).split()[0]
        if mode not in {"100644", "100755"}:
            raise ValueError("unsupported release entry")
        contents[name] = git(repository, "show", commit + ":" + name).stdout
    manifest = {"contract_version": VERSION, "commit": commit,
                "files": {p: digest(raw) for p, raw in contents.items()}}
    manifest_sha = digest(encoded(manifest))
    destination.mkdir(parents=True, exist_ok=True)
    destination = _safe_location(destination)
    lock = _safe_location(destination / "installation.lock")
    if lock.exists():
        _regular_file(lock)
    with exclusive(lock):
        releases = _safe_location(destination / "releases")
        releases.mkdir(exist_ok=True)
        release = _safe_location(releases / commit)
        previous = _active(destination)
        if release.exists():
            try:
                verify(release, manifest_sha)
            except (ValueError, OSError, KeyError, TypeError) as error:
                raise ValueError(f"Existing release is incomplete or altered: {release}. "
                                 "No files were overwritten. Preserve it for inspection; "
                                 "explicitly move the damaged directory aside within this "
                                 "installation before retrying, or use a fresh destination. "
                                 f"Verification: {error}") from error
        else:
            staging = _safe_location(releases / ("." + commit + "." + uuid.uuid4().hex + ".staging"))
            staging.mkdir()
            try:
                for name, raw in contents.items():
                    target = safe_path(staging, name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as handle:
                        handle.write(raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                atomic(staging / "installation.json", manifest)
                verify(staging, manifest_sha)
                # Both paths share a filesystem; the native lock serializes installers.
                _safe_location(releases)
                if release.exists() or release.is_symlink():
                    raise ValueError("release appeared while staging; preserve both for inspection")
                os.rename(staging, release)
            except Exception as error:
                raise ValueError(f"Installation did not publish a new active pointer. "
                                 f"Staging evidence is retained at {staging}; retry builds a "
                                 f"fresh sibling without overwriting it. Cause: {error}") from error
        verify(release, manifest_sha)
        if previous is not None and previous["commit"] == commit:
            # Do not wrap the same active release repeatedly into its own history.
            return previous
        active = {"contract_version": VERSION, "commit": commit, "release_root": str(release),
                  "manifest_sha256": manifest_sha, "previous": previous}
        atomic(destination / "active.json", active)
        return active


def verify(release, manifest_sha256=None):
    release = _safe_location(release)
    if not release.is_dir():
        raise ValueError("installed release directory missing: " + str(release))
    manifest_path = _regular_file(release / "installation.json")
    raw = manifest_path.read_bytes()
    if manifest_sha256 and digest(raw) != manifest_sha256:
        raise ValueError("installation manifest changed")
    manifest = load(manifest_path)
    if set(manifest) != {"contract_version", "commit", "files"} or manifest["contract_version"] != VERSION:
        raise ValueError("installation version/manifest mismatch")
    if not re.fullmatch(r"[0-9a-f]{40}", manifest["commit"]) or not isinstance(manifest["files"], dict):
        raise ValueError("invalid installation manifest")
    required = {"coordination/contract.json", "coordination/task-catalog.json", "tools/cross_pc/cli.py"}
    if not required <= set(manifest["files"]):
        raise ValueError("incomplete installation manifest")
    expected_files = set(manifest["files"]) | {"installation.json"}
    expected_dirs = {parent.as_posix() for name in expected_files
                     for parent in Path(relative(name)).parents if parent != Path(".")}
    actual_files, actual_dirs = set(), set()
    # os.walk never follows a directory link; inspect links before descending.
    for directory, directories, filenames in os.walk(release, followlinks=False):
        for name in directories:
            path = _safe_location(Path(directory) / name)
            actual_dirs.add(path.relative_to(release).as_posix())
        for name in filenames:
            path = _regular_file(Path(directory) / name)
            actual_files.add(path.relative_to(release).as_posix())
    extra = sorted((actual_files - expected_files) | (actual_dirs - expected_dirs))
    if extra:
        raise ValueError("unexpected installed release files/directories: " + ", ".join(extra))
    for name, sha in manifest["files"].items():
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise ValueError("invalid installed file fingerprint")
        if digest(_regular_file(safe_path(release, name)).read_bytes()) != sha:
            raise ValueError("installed release file changed: " + name)
    return {"verified": True, "commit": manifest["commit"], "files": len(manifest["files"])}


def rollback(destination):
    destination = _safe_location(destination)
    lock = _safe_location(destination / "installation.lock")
    if lock.exists():
        _regular_file(lock)
    with exclusive(lock):
        # A damaged current release is itself a reason to roll back. Validate
        # its pointer structure, then require the previous release to verify.
        active = _active(destination, verify_release=False)
        previous = active.get("previous") if active else None
        if not previous:
            raise ValueError("no previous release; preserve installation for inspection")
        _verify_pointer(destination, previous)
        atomic(destination / "active.json", previous)
        return previous
