"""Select a transported combined artifact for Scout's UI only.

No transport, broker calls, controls, activation, or dated execution pointer is
provided here. The caller supplies exact artifact, configuration and prior-view
pins. Both PREPARING and ACTIVE producer bindings may inspect verified reports.
"""
from __future__ import annotations

from datetime import date
import os
from pathlib import Path
import stat
import uuid

from filelock import FileLock

from ml.account_gameplan.config import CONFIG, VERSION, _hash, load_account_config
from ml.account_gameplan.planner import encoded, read_account_plan, sha
from ml.account_gameplan.sources import _json


POINTER = Path("ml/account-gameplan-latest/run.json")
LOCK = Path("state/account-gameplan/view-selection.lock")


def _plain(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("View paths must not contain symlinks or reparse points")
    return path


def _bytes(path):
    _plain(path)
    return path.read_bytes() if path.exists() else None


def _config(root, expected):
    _plain(root / CONFIG)
    config = load_account_config(root)
    if config is None or config.fingerprint != _hash(expected):
        raise ValueError("Exact private producer configuration is required")
    if config.role != "producer":
        raise ValueError("View import is restricted to the forecasting producer")
    return config


def _run(root, value):
    path = _plain(root / Path(value)).resolve()
    if path.parent != root / "ml/account-gameplan-runs":
        raise ValueError("View artifact must be inside the local immutable account run directory")
    return path


def _plan(root, run, pin, config):
    plan = read_account_plan(_run(root, run), expected_manifest_sha256=_hash(pin))
    sources = plan.report["sources"]
    membership = {row["producer_id"]: tuple(sorted(row["symbols"])) for row in sources}
    if (len(sources) != 2 or len(membership) != 2 or membership != config.participants
            or plan.report.get("account_fingerprint") != config.account_fingerprint):
        raise ValueError("View account or exact producer membership differs from its private binding")
    return plan


def _previous(root, raw, config):
    pointer = _json(raw)
    if (set(pointer) != {"schema_version", "status", "config_sha256", "producer_id", "action_date", "current"}
            or pointer["schema_version"] != VERSION or pointer["status"] != "SELECTED"
            or pointer["config_sha256"] != config.fingerprint or pointer["producer_id"] != config.machine_id):
        raise ValueError("Previous view has an incompatible producer binding")
    current = pointer["current"]
    if not isinstance(current, dict) or set(current) != {"run_path", "manifest_sha256", "action_date"}:
        raise ValueError("Previous view must retain its exact source and date")
    plan = _plan(root, current["run_path"], current["manifest_sha256"], config)
    if pointer["action_date"] != plan.report["action_date"] or current["action_date"] != pointer["action_date"]:
        raise ValueError("Previous view action date differs from its verified report")
    return pointer


def _write_new(path, raw):
    _plain(path)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _rollback(path, previous, installed):
    current = _bytes(path)
    if current == previous:
        return
    if current != installed:
        raise RuntimeError("VIEW_ROLLBACK_INCOMPLETE: independently changed pointer preserved")
    if previous is None:
        path.unlink()
        return
    temporary = path.with_name(".view-restore-" + uuid.uuid4().hex + ".tmp")
    try:
        _write_new(temporary, previous)
        if _bytes(path) != installed:
            raise RuntimeError("VIEW_ROLLBACK_INCOMPLETE: pointer changed during restoration")
        os.replace(temporary, path)
    finally:
        if _bytes(temporary) == previous:
            temporary.unlink()


def select_view(root, *, run, manifest_sha256, expected_config, expected_previous_pointer_sha256):
    """CAS-select one verified local report; ``None`` requires no prior pointer."""
    root = _plain(root).resolve()
    if not root.is_dir():
        raise ValueError("Existing local datastore directory is required")
    config = _config(root, expected_config)
    pin = _hash(manifest_sha256)
    previous_pin = None if expected_previous_pointer_sha256 is None else _hash(expected_previous_pointer_sha256)
    run = _run(root, run)
    lock = _plain(root / LOCK)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(lock), timeout=0):
        path = _plain(root / POINTER)
        previous = _bytes(path)
        if (sha(previous) if previous is not None else None) != previous_pin:
            raise ValueError("Previous view pointer differs from its explicit expected SHA256")
        plan = _plan(root, run, pin, config)
        day = date.fromisoformat(plan.report["action_date"]).isoformat()
        selected = {"schema_version": VERSION, "status": "SELECTED", "config_sha256": config.fingerprint,
            "producer_id": config.machine_id, "action_date": day,
            "current": {"run_path": run.relative_to(root).as_posix(), "manifest_sha256": pin, "action_date": day}}
        old = _previous(root, previous, config) if previous is not None else None
        if old is not None and (old["action_date"] > day or (old["action_date"] == day and old != selected)):
            raise ValueError("View cannot downgrade its date or replace a different same-date generation")
        installed = encoded(selected)
        def guard(expected_bytes):
            if _config(root, expected_config) != config:
                raise ValueError("Producer configuration changed during view selection")
            _plan(root, run, pin, config)
            if _bytes(path) != expected_bytes:
                raise ValueError("View pointer changed independently during selection")
        changed = old != selected
        if not changed:
            guard(previous)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(".view-" + uuid.uuid4().hex + ".tmp")
            attempted = False
            try:
                _write_new(temporary, installed)
                guard(previous)
                attempted = True
                os.replace(temporary, path)
                guard(installed)
            except BaseException as exc:
                if attempted:
                    try:
                        _rollback(path, previous, installed)
                    except (OSError, ValueError, RuntimeError) as failure:
                        raise RuntimeError("View selection failed; exact-owned rollback incomplete: " + str(failure)) from exc
                raise
            finally:
                if _bytes(temporary) == installed:
                    temporary.unlink()
        return {"status": "SELECTED", "action_date": day, "producer_id": config.machine_id,
            "current": selected["current"], "pointer_sha256": sha(installed if changed else previous),
            "selection_changed": changed, "orders_placed": 0, "broker_orders_enabled": False,
            "activation_changed": False, "execution_selection_changed": False}
