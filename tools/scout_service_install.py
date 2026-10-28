"""Compose an immutable Scout independent-services release without activation.

The caller supplies an exact reviewed operation list, including installed before
hashes and final source hashes. Private recipe bytes and all unowned predecessor
source are preserved. A distinct selector/bootstrap keeps a running v1 controller
valid; binding the new bootstrap affects only the next normal authorized launch.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import time
import uuid

if __package__:
    from .scout_release import (atomic_json, read_bytes, select_release, sha256,
                               validate_release, _sharing_denial, PROTOCOL as LEGACY_PROTOCOL)
    from .scout_service_controller import PROTOCOL
else:
    from scout_release import (atomic_json, read_bytes, select_release, sha256,
                               validate_release, _sharing_denial, PROTOCOL as LEGACY_PROTOCOL)
    from scout_service_controller import PROTOCOL


def relative_path(value):
    path = PurePosixPath(value)
    if (not isinstance(value, str) or not value or "\\" in value or ":" in value
            or path.is_absolute() or str(path) != value or any(p in {".", "..", ".git", ".env"} for p in path.parts)
            or any(p.lower().startswith(".env") for p in path.parts)):
        raise ValueError("Unsafe reviewed source path")
    return path


def publish_directory(staging, destination):
    """Bounded Windows sharing retry without exposing a partial installation."""
    deadline = time.monotonic() + 1.0
    while True:
        try:
            os.replace(staging, destination)
            return
        except PermissionError as exc:
            if not _sharing_denial(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(min(.01, max(0, deadline - time.monotonic())))


def compose_release(*, predecessor_root, predecessor_manifest_sha256, reviewed_source,
                    operations, destination, completion_record, checks,
                    predecessor_protocol=LEGACY_PROTOCOL, trading_rotation_version=None):
    """Build verified bytes beside the predecessor; never change any selector."""
    previous = validate_release(predecessor_root, predecessor_manifest_sha256, protocol=predecessor_protocol)
    previous_rotation = previous["manifest"].get("trading_rotation_version")
    rotation_version = previous_rotation if trading_rotation_version is None else trading_rotation_version
    if rotation_version not in {None, "scout-trading-rotation-v1"}:
        raise ValueError("Unsupported trading rotation contract")
    if not completion_record or not checks or not isinstance(operations, list) or not operations:
        raise ValueError("Release composition needs review identity, passing checks and explicit operations")
    if any(not isinstance(check, dict) or check.get("exit_code") != 0 or not check.get("output_sha256")
           or not check.get("command") or not check.get("completed_at") for check in checks):
        raise ValueError("Offline checks must retain real completed passing evidence")
    source = Path(reviewed_source).resolve(strict=True)
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError("Immutable release destination already exists")
    if destination == Path(previous["release_root"]) or destination.is_relative_to(Path(previous["release_root"])):
        raise ValueError("Never compose inside an immutable predecessor")
    reviewed = []
    seen = set()
    for operation in operations:
        path = relative_path(operation["path"]).as_posix()
        if path in seen:
            raise ValueError("Duplicate reviewed source operation")
        seen.add(path)
        kind = operation.get("operation")
        old_hash = previous["manifest"]["files"].get(path)
        if kind not in {"add", "modify", "delete"}:
            raise ValueError("Explicit add/modify/delete operation required")
        if (kind == "add") != (old_hash is None) or operation.get("before_sha256") != old_hash:
            raise ValueError("Installed source changed since hunk review: " + path)
        raw = None
        if kind != "delete":
            candidate = (source / path).resolve(strict=True)
            if not candidate.is_relative_to(source):
                raise ValueError("Reviewed source escaped its root")
            raw = read_bytes(candidate)
            if sha256(raw) != operation.get("sha256"):
                raise ValueError("Reviewed final source hash differs: " + path)
        elif operation.get("sha256") is not None:
            raise ValueError("Deleted source cannot have final bytes")
        reviewed.append((path, kind, raw))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".pending-" + uuid.uuid4().hex)
    staging.mkdir()
    inventory = dict(previous["manifest"]["files"])
    try:
        for path, expected in inventory.items():
            relative_path(path)
            original = Path(previous["source_root"]) / path
            raw = read_bytes(original)
            if sha256(raw) != expected:
                raise ValueError("Predecessor changed during composition: " + path)
            output = staging / "source" / path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(raw)
        for path, kind, raw in reviewed:
            output = staging / "source" / path
            if kind == "delete":
                output.unlink()
                del inventory[path]
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(raw)
                inventory[path] = sha256(raw)
        rotation_sources = {"ml/intraday/trading_rotation.py", "ml/intraday/live_session.py",
                            "tools/scout_service_controller.py"}
        if rotation_version and not rotation_sources.issubset(inventory):
            raise ValueError("Trading rotation capability requires the complete worker/controller source")
        if rotation_version and previous_rotation is None:
            # Existing filenames can still contain the old model-only lifecycle.
            # First promotion must review changed bytes for every integration
            # component; later compatible releases retain the proven capability.
            changed = {path for path, kind, raw in reviewed if kind != "delete"
                       and sha256(raw) != previous["manifest"]["files"].get(path)}
            if not rotation_sources.issubset(changed):
                raise ValueError("Initial trading rotation requires reviewed changed worker/controller source")
        recipe_raw = read_bytes(previous["recipe_path"])
        if sha256(recipe_raw) != previous["recipe_file_sha256"]:
            raise ValueError("Private recipe changed during composition")
        (staging / "private").mkdir()
        (staging / "private/recipe.json").write_bytes(recipe_raw)
        manifest = {**previous["manifest"], "source_root": str(destination / "source"),
            "recipe_path": str(destination / "private/recipe.json"), "recipe_file_sha256": sha256(recipe_raw),
            "files": inventory, "lifecycle_protocol": PROTOCOL, "release_id": destination.name,
            "prepared_only": True, "manual_restart_required": True,
            "independent_services_installation": {"completion_record": completion_record,
                "predecessor_root": previous["release_root"], "predecessor_manifest_sha256": predecessor_manifest_sha256,
                "operations": operations, "checks": checks, "private_recipe_unchanged": True,
                "system_1a_owned_by_1b": False, "trader_started": False,
                "activation_boundary": "next_normal_authorized_launch_new_independent_controller"}}
        if rotation_version:
            manifest["trading_rotation_version"] = rotation_version
        atomic_json(staging / "manifest.json", manifest)
        # Same-volume rename publishes the complete immutable directory. A crash
        # before this point leaves no selectable partial release.
        publish_directory(staging, destination)
    except BaseException:
        # Retain partial evidence for diagnosis/recovery. No pointer was changed.
        raise
    manifest_hash = sha256(read_bytes(destination / "manifest.json"))
    return validate_release(destination, manifest_hash, protocol=PROTOCOL)


BOOTSTRAP_FILES = ("scout_service_controller.py", "scout_iteration_controller.py", "scout_process.py", "scout_release.py")


def manual_launcher_source(*, python_executable, bootstrap):
    """Bind a local manual entrypoint to the same pinned controller as its task.

    No arguments means the existing manual activation. Explicit --check and
    --stop are passed without an implicit --live, so checking cannot start a
    trader and stopping cannot create a fresh logical run. The controller owns
    argument validation; unsupported legacy recipe overrides fail explicitly.
    """
    values = [str(python_executable), str(bootstrap)]
    if any(not value or any(c in value for c in '\r\n"%!') for value in values):
        raise ValueError("Unsafe Windows launcher binding")
    command = '"' + values[0] + '" -B -u "' + values[1] + '"'
    return ("@echo off\r\nsetlocal DisableDelayedExpansion\r\n"
            "title Scout 15m Trader\r\nset \"PYTHONDONTWRITEBYTECODE=1\"\r\n"
            "if \"%~1\"==\"\" (\r\n  " + command + " --live --hold-console\r\n"
            ") else (\r\n  " + command + " %*\r\n)\r\nexit /b %errorlevel%\r\n").encode("utf-8")


LAUNCH_SOURCE = '''"""Verified independent Scout bootstrap. --live is initial trading activation."""
import hashlib
import json
from pathlib import Path
import sys

def main():
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "bootstrap-manifest.json").read_bytes())
    importable = {p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and p.suffix.lower() in {".py", ".pyc", ".pyd"}}
    if importable != set(manifest["files"]):
        raise RuntimeError("Independent Scout bootstrap source inventory changed")
    for name, expected in manifest["files"].items():
        target = (root / name).resolve()
        if target.parent != root or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Installed Scout bootstrap differs: " + name)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    from scout_service_controller import main as run
    return run(["--repository", manifest["repository"], "--datastore", manifest["datastore"],
                "--selector", manifest["selector"], *sys.argv[1:]])

if __name__ == "__main__":
    raise SystemExit(main())
'''


def install_bootstrap(*, selected, destination, repository, datastore, selector, completion_record):
    """Create a new protocol bootstrap. Does not alter the cured launcher/task."""
    selected = validate_release(selected["release_root"], selected["manifest_sha256"], protocol=PROTOCOL)
    root = Path(destination).resolve()
    if root.exists():
        raise FileExistsError("Immutable bootstrap destination already exists")
    root.mkdir(parents=True)
    inventory = {}
    for name in BOOTSTRAP_FILES:
        raw = read_bytes(Path(selected["source_root"]) / "tools" / name)
        if sha256(raw) != selected["manifest"]["files"].get("tools/" + name):
            raise ValueError("Bootstrap source missing from reviewed installation")
        (root / name).write_bytes(raw)
        inventory[name] = sha256(raw)
    raw = LAUNCH_SOURCE.encode("utf-8")
    (root / "launch.py").write_bytes(raw)
    inventory["launch.py"] = sha256(raw)
    manifest = {"schema": "scout-independent-bootstrap-v1", "protocol": PROTOCOL,
                "files": inventory, "repository": str(Path(repository)), "datastore": str(Path(datastore)),
                "selector": str(Path(selector)), "source_completion_record": completion_record,
                "system_1a_started": False, "trader_started": False}
    atomic_json(root / "bootstrap-manifest.json", manifest)
    return {"bootstrap_root": str(root), "launch_path": str(root / "launch.py"),
            "manifest_sha256": sha256(read_bytes(root / "bootstrap-manifest.json")), **manifest}


DATA_LAUNCH_SOURCE = '''"""Verified Scout 1A data bootstrap. No trading activation entrypoint exists."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import traceback

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--data-live", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "bootstrap-manifest.json").read_bytes())
    if manifest.get("schema") != "scout-independent-data-bootstrap-v1":
        raise RuntimeError("Independent Scout data bootstrap schema changed")
    importable = {p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() and p.suffix.lower() in {".py", ".pyc", ".pyd"}}
    if importable != set(manifest["files"]):
        raise RuntimeError("Independent Scout data bootstrap source inventory changed")
    for name, expected in manifest["files"].items():
        target = (root / name).resolve()
        if target.parent != root or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Independent Scout data bootstrap changed: " + name)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(root))
    from scout_release import resolve_selection
    selected = resolve_selection(manifest["selector"], protocol=manifest["protocol"])
    if not args.data_live:
        print(json.dumps({"status": "verified_offline", "release": selected["manifest_sha256"],
            "recipe_file_sha256": selected["recipe_file_sha256"], "system_1a_started": False,
            "trading_started": False, "provider_calls": 0}, sort_keys=True))
        return 0
    source = Path(selected["source_root"])
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PYTHONPATH"] = str(source)
    sys.path.insert(0, str(source))
    os.chdir(source)
    token_store = importlib.import_module("app.services.schwab_token_store")
    services = importlib.import_module("ml.intraday.scout_services")
    for module in (token_store, services):
        if not Path(module.__file__).resolve().is_relative_to(source):
            raise RuntimeError("Independent Scout data imported outside pinned release")
    repository = Path(manifest["repository"])
    token_store.PROJECT_ROOT = repository
    token_store.DEFAULT_TOKEN_CACHE_PATH = repository / "data/schwab_tokens.json"
    token_store.TOKEN_CACHE_PATH = token_store.DEFAULT_TOKEN_CACHE_PATH
    datastore = Path(manifest["datastore"])
    log_root = datastore / "intraday/services"
    log_root.mkdir(parents=True, exist_ok=True)
    with (log_root / "data.stdout.log").open("a", encoding="utf-8", buffering=1) as stdout, \\
         (log_root / "data.stderr.log").open("a", encoding="utf-8", buffering=1) as stderr, \\
         redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            result = services.run_data_service(repository=repository, datastore=datastore, release=selected)
        except BaseException:
            traceback.print_exc()
            raise
    print(json.dumps(result, sort_keys=True))
    return 1 if result["status"] == "FAILED" else 0

if __name__ == "__main__":
    raise SystemExit(main())
'''


def install_data_bootstrap(*, selected, destination, repository, datastore, selector, completion_record):
    """Pin a separate data executable; create no task, provider or trader process."""
    selected = validate_release(selected["release_root"], selected["manifest_sha256"], protocol=PROTOCOL)
    root = Path(destination).resolve()
    if root.exists():
        raise FileExistsError("Immutable data bootstrap destination already exists")
    root.mkdir(parents=True)
    raw = read_bytes(Path(selected["source_root"]) / "tools/scout_release.py")
    if sha256(raw) != selected["manifest"]["files"].get("tools/scout_release.py"):
        raise ValueError("Data bootstrap source missing from reviewed installation")
    (root / "scout_release.py").write_bytes(raw)
    launch = DATA_LAUNCH_SOURCE.encode("utf-8")
    (root / "launch.py").write_bytes(launch)
    manifest = {"schema": "scout-independent-data-bootstrap-v1", "protocol": PROTOCOL,
        "files": {"scout_release.py": sha256(raw), "launch.py": sha256(launch)},
        "repository": str(Path(repository)), "datastore": str(Path(datastore)),
        "selector": str(Path(selector)), "source_completion_record": completion_record,
        "system_1a_started": False, "trader_started": False, "trading_activation_supported": False}
    atomic_json(root / "bootstrap-manifest.json", manifest)
    return {"bootstrap_root": str(root), "launch_path": str(root / "launch.py"),
            "manifest_sha256": sha256(read_bytes(root / "bootstrap-manifest.json")), **manifest}


def select_independent_release(selector, selected, *, expected_selection_sha256=None):
    """New independent selector only; reject changing a legacy v1 pointer."""
    selector = Path(selector)
    if selector.exists():
        current = json.loads(read_bytes(selector))
        current_manifest = json.loads(read_bytes(Path(current["release_root"]) / "manifest.json"))
        if current_manifest.get("lifecycle_protocol") != PROTOCOL:
            raise ValueError("Independent installation must preserve the legacy controller selector")
    return select_release(selector, selected["release_root"], selected["manifest_sha256"],
                          protocol=PROTOCOL, expected_selection_sha256=expected_selection_sha256)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args(argv)
    spec = json.loads(read_bytes(args.spec))
    result = compose_release(**spec["compose"])
    if spec.get("bootstrap"):
        result["bootstrap"] = install_bootstrap(selected=result, **spec["bootstrap"])
    if spec.get("data_bootstrap"):
        result["data_bootstrap"] = install_data_bootstrap(selected=result, **spec["data_bootstrap"])
    if spec.get("selector"):
        result["selection"] = select_independent_release(spec["selector"], result,
            expected_selection_sha256=spec.get("expected_selection_sha256"))
    print(json.dumps({k: v for k, v in result.items() if k != "manifest"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
