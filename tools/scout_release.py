"""Exact-byte Scout generation selection. No application imports or live I/O."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

PROTOCOL = "scout-iteration-v1"
SELECTOR_SCHEMA = "scout-release-selection-v1"


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _sharing_denial(exc):
    # Windows CRT opens can discard GetLastError and expose only EACCES.
    # Keep this retry Windows-specific and bounded; permanent denials still fail.
    code = getattr(exc, "winerror", None)
    return code in {5, 32, 33} or (os.name == "nt" and code is None and exc.errno == errno.EACCES)


def read_bytes(path):
    """Bounded Windows sharing retry; no caller may enable effects while waiting."""
    deadline = time.monotonic() + 1.0
    while True:
        try:
            return Path(path).read_bytes()
        except PermissionError as exc:
            if not _sharing_denial(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(json.dumps(value, sort_keys=True, allow_nan=False).encode())
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers/indexers may briefly hold a destination without delete
        # sharing. Retry only that class of transient error, with a real deadline.
        # Never remove the previous complete value or publish partial bytes.
        deadline = time.monotonic() + 1.0
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                if not _sharing_denial(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    finally:
        temporary.unlink(missing_ok=True)


def validate_release(release_root, manifest_sha256, *, protocol=PROTOCOL):
    release = Path(release_root).resolve(strict=True)
    raw = read_bytes(release / "manifest.json")
    if sha256(raw) != manifest_sha256:
        raise ValueError("Selected Scout manifest hash differs")
    manifest = json.loads(raw)
    source = (release / "source").resolve(strict=True)
    if Path(manifest["source_root"]).resolve() != source:
        raise ValueError("Scout source root differs from immutable installation")
    if manifest.get("lifecycle_protocol") != protocol:
        raise ValueError("Scout release requires a different controller protocol")
    files = manifest["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("Scout manifest has no complete source inventory")
    for relative, expected in files.items():
        path = (source / relative).resolve(strict=True)
        if not path.is_relative_to(source) or sha256(read_bytes(path)) != expected:
            raise ValueError("Scout installed source hash differs: " + relative)
    # -B prevents new bytecode but does not prevent reading old pyc files.
    # Also reject unlisted importable source: a manifest must cover the release.
    for path in source.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".py", ".pyc", ".pyd"}:
            if path.suffix.lower() == ".pyc" or path.relative_to(source).as_posix() not in files:
                raise ValueError("Unmanifested importable Scout source: " + str(path))
    recipe_path = Path(manifest["recipe_path"]).resolve(strict=True)
    recipe_raw = read_bytes(recipe_path)
    if sha256(recipe_raw) != manifest["recipe_file_sha256"]:
        raise ValueError("Scout saved recipe changed after release review")
    recipe = json.loads(recipe_raw)
    if len(recipe["symbols"]) != 11 or len(set(recipe["symbols"])) != 11:
        raise ValueError("Scout release must retain eleven unique recipe symbols")
    # The application validates the complete frozen recipe before broker startup.
    return {"release_root": str(release), "manifest_sha256": manifest_sha256,
            "source_root": str(source), "recipe_path": str(recipe_path),
            "recipe_file_sha256": manifest["recipe_file_sha256"],
            "symbols": recipe["symbols"], "manifest": manifest,
            "lifecycle_protocol": protocol}


def resolve_selection(selector, *, protocol=PROTOCOL):
    selector = Path(selector)
    original = read_bytes(selector)
    selected = json.loads(original)
    if selected.get("schema") != SELECTOR_SCHEMA:
        raise ValueError("Invalid Scout release selector schema")
    result = validate_release(selected["release_root"], selected["manifest_sha256"], protocol=protocol)
    if read_bytes(selector) != original:
        raise ValueError("Scout release selection changed during validation; no fallback")
    result["selection_sha256"] = sha256(original)
    return result


def select_release(selector, release_root, manifest_sha256, *, expected_selection_sha256=None,
                   protocol=PROTOCOL):
    """Installer commit point, called only after complete review and offline tests.

    Compare the predecessor under the installer's exclusive ownership; a caller
    must never repoint a selector while another installation owns that scope.
    Invalid selections fail closed, never silently fall back to old source.
    """
    selector = Path(selector)
    from filelock import FileLock
    selector.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(selector) + ".install.lock", timeout=0):
        validated = validate_release(release_root, manifest_sha256, protocol=protocol)
        previous = read_bytes(selector) if selector.exists() else None
        if (sha256(previous) if previous is not None else None) != expected_selection_sha256:
            raise ValueError("Scout selector changed since installer review")
        atomic_json(selector, {"schema": SELECTOR_SCHEMA, "release_root": validated["release_root"],
                              "manifest_sha256": manifest_sha256,
                              "previous": json.loads(previous) if previous is not None else None})
        return resolve_selection(selector, protocol=protocol)
