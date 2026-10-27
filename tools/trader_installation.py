"""Seal reviewed trader code and its Python runtime without changing live workers.

This supplements reviewed per-file local adoption; the coordination installer does
not install application code. A generation is selected only by one atomic pointer
after complete source/runtime verification. Never update a published generation in
place. Private repository bindings and scientific/execution state stay outside it.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
import uuid


SCHEMA = "trader-installation-v1"
PROTOCOL = 1
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_PRIVATE_PARTS = {".git", ".env", ".venv", "scratch", "__pycache__", "data", "artifacts"}
_CODE_SUFFIXES = {".py", ".pyi", ".json", ".toml", ".txt", ".cmd", ".ps1", ".md"}
_REQUIRED = {"tools/trader_worker.py", "tools/trader_installation.py",
             "tools/trader_session_host.py", "ml/trader_renewal.py"}


class InstallationError(ValueError):
    """The prior installed generation remains selected when sealing fails."""


class InstallationBusy(InstallationError):
    pass


def _encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _digest(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _safe_path(path):
    path = Path(os.path.abspath(path))
    for item in (*reversed(path.parents), path):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise InstallationError("Symlink/reparse installation input is not supported: " + str(item))
    return path


def _relative(value, *, source=False):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise InstallationError("Inventory paths must be portable relative paths")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise InstallationError("Invalid inventory path: " + value)
    if source:
        if any(part.lower() in _PRIVATE_PARTS or part.lower().startswith(".env")
               or ".local." in part.lower() for part in path.parts):
            raise InstallationError("Private state/bindings are not generation source: " + value)
        if path.suffix.lower() not in _CODE_SUFFIXES:
            raise InstallationError("Only reviewed source/resource files may be sealed: " + value)
    return value


def _inventory(value, *, source=False):
    if not isinstance(value, dict) or not value:
        raise InstallationError("An explicit nonempty reviewed inventory is required")
    result, folded = {}, set()
    for name, digest in value.items():
        _relative(name, source=source)
        if not isinstance(digest, str) or not _SHA.fullmatch(digest):
            raise InstallationError("Invalid SHA-256 in inventory: " + name)
        if name.casefold() in folded:
            raise InstallationError("Case-colliding generation paths")
        folded.add(name.casefold())
        result[name] = digest
    return dict(sorted(result.items()))


def _load(path):
    try:
        value = json.loads(_safe_path(path).read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallationError("Unreadable installation record: " + str(path)) from exc
    if not isinstance(value, dict):
        raise InstallationError("Installation record must be an object")
    return value


def _atomic(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("xb") as handle:
        handle.write(_encoded(value))
        handle.flush()
        os.fsync(handle.fileno())
    for attempt in range(6):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.02 * (attempt + 1))


@contextmanager
def _exclusive(root):
    path = _safe_path(root / "installation.lock")
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise InstallationBusy("Another generation installer owns the installation lock") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _regular(path):
    path = _safe_path(path)
    try:
        regular = stat.S_ISREG(path.stat().st_mode)
    except OSError as exc:
        raise InstallationError("Missing or unreadable generation file: " + str(path)) from exc
    if not regular:
        raise InstallationError("Expected regular generation file: " + str(path))
    return path


def _readonly(path):
    path.chmod(path.stat().st_mode & ~0o222)


def _walk_entry(path, *, directory=False):
    """Validate one entry whose ancestors were already checked by the walker."""
    try:
        info = path.lstat()
    except OSError as exc:
        raise InstallationError("Missing or unreadable generation entry: " + str(path)) from exc
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
        raise InstallationError("Symlink/reparse generation entry: " + str(path))
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode):
        raise InstallationError("Unexpected generation entry type: " + str(path))
    return path


def _check_files(root, inventory, *, full=True, exact=True):
    if exact:
        root = _safe_path(root)
        actual = set()
        for directory, folders, filenames in os.walk(root, followlinks=False):
            for name in folders:
                _walk_entry(Path(directory) / name, directory=True)
            for name in filenames:
                path = _walk_entry(Path(directory) / name)
                relative = path.relative_to(root).as_posix()
                if relative not in inventory:
                    raise InstallationError("Generation contains unexpected files: " + relative)
                if full and _digest(path) != inventory[relative]:
                    raise InstallationError("Generation file hash mismatch: " + relative)
                actual.add(relative)
        if actual != set(inventory):
            raise InstallationError("Missing generation files")
    else:
        for name, expected in inventory.items():
            path = _regular(root / name)
            if full and _digest(path) != expected:
                raise InstallationError("Generation file hash mismatch: " + name)


def _runtime_files(python_root, site_packages):
    result = {}
    for label, root in (("python", python_root), ("site-packages", site_packages)):
        root = _safe_path(root)
        for directory, folders, filenames in os.walk(root, followlinks=False):
            current = Path(directory)
            for name in list(folders):
                child = current / name
                relative = child.relative_to(root).as_posix()
                if name == "__pycache__" or (label == "python" and relative.casefold() == "lib/site-packages"):
                    folders.remove(name)
                    continue
                _walk_entry(child, directory=True)
            for name in sorted(filenames):
                if name.startswith("__editable__"):
                    continue
                if name.endswith((".pyc", ".pyo")) and (current / name).with_suffix(".py").is_file():
                    continue  # Keep deliberately bytecode-only distribution files.
                path = _walk_entry(current / name)
                relative = path.relative_to(root).as_posix()
                if name == "pyvenv.cfg":
                    raise InstallationError("python_root must be the complete base interpreter, not a virtualenv")
                result[label + "/" + relative] = path
    return dict(sorted(result.items()))


def _copy_checked(source, target, expected):
    target.parent.mkdir(parents=True, exist_ok=True)
    with _regular(source).open("rb") as incoming, target.open("xb") as outgoing:
        digest = hashlib.sha256()
        for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
            digest.update(chunk)
            outgoing.write(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if digest.hexdigest() != expected:
        raise InstallationError("Input changed while copying: " + str(source))
    shutil.copystat(source, target)
    _readonly(target)


def _runtime_manifest(root, identity, manifest_sha256, *, full):
    path = _regular(root / "runtimes" / identity / "manifest.json")
    if _digest(path) != manifest_sha256:
        raise InstallationError("Runtime manifest hash mismatch")
    manifest = _load(path)
    if set(manifest) != {"schema", "protocol", "files", "python_relative"} or manifest.get("schema") != SCHEMA or manifest.get("protocol") != PROTOCOL:
        raise InstallationError("Unsupported runtime manifest")
    if hashlib.sha256(_encoded(manifest)).hexdigest() != identity:
        raise InstallationError("Runtime content identity mismatch")
    files = _inventory(manifest["files"])
    python_relative = _relative(manifest["python_relative"])
    if python_relative not in files or not python_relative.startswith("python/"):
        raise InstallationError("Runtime does not contain its interpreter")
    runtime_root = path.parent
    if full:
        _check_files(runtime_root, {**files, "manifest.json": manifest_sha256})
    else:
        # Full verification is mandatory at seal and manual session activation.
        # A handoff rechecks the immutable manifests and entry executables; it
        # never scans or selects mutable source/dependency inputs.
        if _digest(_regular(runtime_root / python_relative)) != files[python_relative]:
            raise InstallationError("Frozen interpreter changed")
    return manifest, runtime_root


def verify(root, generation, manifest_sha256, *, full=True):
    """Verify a pinned complete generation; never fall back to mutable code."""
    root = _safe_path(root)
    if not isinstance(generation, str) or not _SHA.fullmatch(generation) or not isinstance(manifest_sha256, str) or not _SHA.fullmatch(manifest_sha256):
        raise InstallationError("Invalid pinned generation identity")
    manifest_path = _regular(root / "generations" / generation / "manifest.json")
    if _digest(manifest_path) != manifest_sha256:
        raise InstallationError("Generation manifest hash mismatch")
    manifest = _load(manifest_path)
    if set(manifest) != {"schema", "protocol", "source_files", "runtime", "metadata"} or manifest.get("schema") != SCHEMA or manifest.get("protocol") != PROTOCOL:
        raise InstallationError("Unsupported source/dependency generation protocol")
    if hashlib.sha256(_encoded(manifest)).hexdigest() != generation:
        raise InstallationError("Generation content identity mismatch")
    source_files = _inventory(manifest["source_files"], source=True)
    if not _REQUIRED <= source_files.keys():
        raise InstallationError("Incomplete trader entry-point inventory")
    runtime = manifest["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != {"id", "manifest_sha256"} or not all(isinstance(v, str) and _SHA.fullmatch(v) for v in runtime.values()):
        raise InstallationError("Invalid pinned runtime identity")
    source_root = manifest_path.parent / "source"
    # Source is small enough to rehash at every handoff. Only the large sealed
    # dependency tree uses the lighter between-generation verification mode.
    _check_files(source_root, source_files, full=True, exact=True)
    runtime_manifest, runtime_root = _runtime_manifest(root, runtime["id"], runtime["manifest_sha256"], full=full)
    standard_archives = [str(runtime_root / name) for name in runtime_manifest["files"]
                         if re.fullmatch(r"python/python[0-9]+\.zip", name)]
    return {"schema": SCHEMA, "protocol": PROTOCOL, "generation": generation,
            "manifest_sha256": manifest_sha256, "source_root": str(source_root),
            "runtime_id": runtime["id"], "runtime_root": str(runtime_root),
            "python": str(runtime_root / runtime_manifest["python_relative"]),
            "site_packages": str(runtime_root / "site-packages"),
            "stdlib_paths": [*standard_archives, str(runtime_root / "python/Lib"),
                             str(runtime_root / "python/DLLs"), str(runtime_root / "python")],
            "manifest": manifest, "runtime_manifest": runtime_manifest}


def read_current(root, *, full=True):
    """Read one atomic pointer, then verify exactly that immutable generation."""
    root = _safe_path(root)
    active = _load(root / "active.json")
    if set(active) != {"schema", "protocol", "generation", "manifest_sha256"} or active.get("schema") != SCHEMA or active.get("protocol") != PROTOCOL:
        raise InstallationError("Invalid active generation pointer")
    return verify(root, active["generation"], active["manifest_sha256"], full=full)


def seal(root, *, source_root, source_inventory, python_root, site_packages,
         reviewed=False, python_relative="python.exe", metadata=None):
    """Stage a reviewed exact source inventory plus a complete dependency snapshot.

    This does not install/rewrite the mutable application, execute Python, or touch
    a trader. Both dependency and source inputs must remain byte stable throughout
    sealing. Failed staging is retained, and active.json remains unchanged.
    """
    if reviewed is not True:
        raise InstallationError("Generation source requires exact-byte local review")
    root, source_root = _safe_path(root), _safe_path(source_root)
    python_root, site_packages = _safe_path(python_root), _safe_path(site_packages)
    if not all(path.is_dir() for path in (source_root, python_root, site_packages)):
        raise InstallationError("Source, interpreter and dependency input roots must all exist")
    if root == source_root or root.is_relative_to(python_root) or root.is_relative_to(site_packages):
        raise InstallationError("Generation destination overlaps copied input roots")
    source_inventory = _inventory(source_inventory, source=True)
    if not _REQUIRED <= source_inventory.keys():
        raise InstallationError("Reviewed inventory must include worker and installer")
    python_relative = "python/" + _relative(python_relative)
    metadata = {} if metadata is None else metadata
    if not isinstance(metadata, dict):
        raise InstallationError("Generation metadata must be an object")
    _encoded(metadata)  # Reject unserializable review metadata before copying.
    root.mkdir(parents=True, exist_ok=True)
    with _exclusive(root):
        _check_files(source_root, source_inventory, exact=False)
        runtime_sources = _runtime_files(python_root, site_packages)
        runtime_files = {name: _digest(path) for name, path in runtime_sources.items()}
        _inventory(runtime_files)
        if python_relative not in runtime_files or "python/Lib/encodings/__init__.py" not in runtime_files:
            raise InstallationError("Incomplete base Python runtime")
        runtime_manifest = {"schema": SCHEMA, "protocol": PROTOCOL,
                            "files": runtime_files, "python_relative": python_relative}
        runtime_id = hashlib.sha256(_encoded(runtime_manifest)).hexdigest()
        runtime_root = root / "runtimes" / runtime_id
        (root / "runtimes").mkdir(exist_ok=True)
        if runtime_root.exists():
            _runtime_manifest(root, runtime_id, runtime_id, full=True)
        else:
            staging = root / "runtimes" / ("." + runtime_id + "." + uuid.uuid4().hex + ".staging")
            staging.mkdir()
            for name, source in runtime_sources.items():
                _copy_checked(source, staging / name, runtime_files[name])
            _atomic(staging / "manifest.json", runtime_manifest)
            _readonly(staging / "manifest.json")
            _check_files(staging, {**runtime_files, "manifest.json": runtime_id})
            os.rename(staging, runtime_root)
        # Inventories as well as content are compared: a newly installed package
        # appearing during the snapshot cannot silently escape the completeness
        # fence. No .pth or editable finder is executed by this installer.
        current_sources = _runtime_files(python_root, site_packages)
        if set(current_sources) != set(runtime_sources) or any(_digest(path) != runtime_files[name] for name, path in current_sources.items()):
            raise InstallationError("Dependency installation changed while sealing; prior pointer retained")
        manifest = {"schema": SCHEMA, "protocol": PROTOCOL,
                    "source_files": source_inventory,
                    "runtime": {"id": runtime_id, "manifest_sha256": runtime_id},
                    "metadata": metadata}
        generation = hashlib.sha256(_encoded(manifest)).hexdigest()
        generation_root = root / "generations" / generation
        (root / "generations").mkdir(exist_ok=True)
        if not generation_root.exists():
            staging = root / "generations" / ("." + generation + "." + uuid.uuid4().hex + ".staging")
            staging.mkdir()
            for name, expected in source_inventory.items():
                _copy_checked(source_root / name, staging / "source" / name, expected)
            _atomic(staging / "manifest.json", manifest)
            _readonly(staging / "manifest.json")
            _check_files(staging / "source", source_inventory)
            os.rename(staging, generation_root)
        _check_files(source_root, source_inventory, exact=False)
        selected = verify(root, generation, generation, full=True)
        _atomic(root / "active.json", {"schema": SCHEMA, "protocol": PROTOCOL,
                                      "generation": generation, "manifest_sha256": generation})
        return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("seal")
    install.add_argument("--root", type=Path, required=True)
    install.add_argument("--spec", type=Path, required=True)
    check = sub.add_parser("verify")
    check.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "seal":
        value = seal(args.root, **_load(args.spec))
    else:
        value = read_current(args.root)
    print(json.dumps({key: value[key] for key in ("schema", "protocol", "generation", "runtime_id", "source_root", "python", "site_packages")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
