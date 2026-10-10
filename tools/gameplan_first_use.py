"""Manual account setup: retain Atlas's history; normal worker reconciles it.

Scout produces research, never trading ownership. This module never reads native
inventory or calls a broker, and does not assert the existing history is empty.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import uuid

from filelock import FileLock

from datafetching.runtime_lock import exclusive_runtime_lock
from ml.account_gameplan.config import (CONFIG, FIRST_USE_VERSION, load_account_config,
                                       validate_first_use_declaration, verify_cutover)
from ml.account_gameplan.migration import _plain

MARKER = Path("scratch/nightly-workflow/first-use.json")
LOCK = Path("state/account-gameplan/first-use.lock")


def _repository():
    return Path(__file__).resolve().parents[1]


def _raw(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path):
    raw = _plain(path).read_bytes()
    if len(raw) > 128 * 1024:
        raise ValueError("ATLAS_SOLE_EXECUTOR_EVIDENCE_TOO_LARGE")
    return raw


def _write(path, raw, *, expected=None):
    path = _plain(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if expected is None and path.exists():
        if _read(path) != raw:
            raise ValueError("ATLAS_SOLE_EXECUTOR_ORIGINAL_EVIDENCE_CHANGED")
        return
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if expected is not None:
            if _read(path) != expected:
                raise ValueError("ATLAS_SOLE_EXECUTOR_CONFIG_CHANGED")
            temporary.replace(path)
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _declaration(root):
    original = _read(Path(root) / CONFIG)
    account = load_account_config(root)
    if account is None:
        raise ValueError("ATLAS_SOLE_EXECUTOR_ACCOUNT_MISSING")
    raw = _read(_repository() / MARKER)
    declaration = json.loads(raw)
    validate_first_use_declaration(declaration, account)
    if _read(Path(root) / CONFIG) != original:
        raise ValueError("ATLAS_SOLE_EXECUTOR_CONFIG_CHANGED")
    return account, declaration, raw, original


def inspect_first_use(root):
    """Return None when unselected; never inspect or modify account inventory."""
    if not (_repository() / MARKER).exists():
        return None
    try:
        _declaration(Path(root))
        return {"ready": True, "status": "READY_FOR_MANUAL_START", "startup_mode": "ATLAS_SOLE_EXECUTOR",
                "broker_reconciliation_pending": True, "activation_changed": False}
    except (ValueError, OSError, KeyError, TypeError):
        return {"ready": False, "status": "INVENTORY_SETUP_PENDING",
                "reason": "ATLAS_SOLE_EXECUTOR_DECLARATION_INVALID", "startup_mode": "ATLAS_SOLE_EXECUTOR",
                "broker_reconciliation_pending": True, "activation_changed": False}


def initialize_first_use(root, *, checkpoint=None):
    """Called only by explicit manual Start; worker guards remain unchanged."""
    root = _plain(Path(root)).resolve()
    checkpoint = checkpoint or (lambda _: None)
    lock = root / LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        stack.enter_context(FileLock(str(lock), timeout=0))
        for name in ("independent-stock-session.lock", "stock-trader-hourly.lock"):
            stack.enter_context(exclusive_runtime_lock(root / "locks" / name, process_name="gameplan-first-use"))
        stack.enter_context(FileLock(str(root / "state/account-gameplan/preparation.lock"), timeout=0))
        account = load_account_config(root)
        if account is not None and (account.machine_id != "pc-original" or account.role != "coordinator"):
            raise ValueError("ATLAS_SOLE_EXECUTOR_ACCOUNT_REQUIRED")
        if account is not None and account.activation["status"] == "ACTIVE":
            verify_cutover(root, account)
            return {"status": "ACCOUNT_READY", "ready": True, "activation_changed": False, "broker_read_performed": False}
        account, declaration, declaration_raw, original = _declaration(root)
        folder = root / "state/account-gameplan/first-use" / declaration["operation_id"]
        _write(folder / "original-config.json", original)
        _write(folder / "declaration.json", declaration_raw)
        invocation_path = folder / "manual-invocation.json"
        if not invocation_path.exists():
            _write(invocation_path, _raw({"operation_id": declaration["operation_id"],
                "trigger": "EXPLICIT_MANUAL_START", "requested_at": datetime.now(timezone.utc).isoformat()}))
        invocation_raw = _read(invocation_path)
        receipt = {"schema_version": FIRST_USE_VERSION, "status": "VERIFIED", "binding_sha256": account.fingerprint,
            "account_fingerprint": account.account_fingerprint, "machine_id": account.machine_id,
            "coordinator_id": account.coordinator_id, "operation_id": declaration["operation_id"],
            "declaration_sha256": _sha(declaration_raw), "manual_invocation_sha256": _sha(invocation_raw),
            "native_ledger_policy": "PRESERVE_EXISTING_ATLAS_ACCOUNT_WIDE_HISTORY",
            "runtime_reconciliation_required": True, "broker_calls": 0, "orders_placed": 0}
        receipt_path = root / "state/account-gameplan/cutovers" / (declaration["operation_id"] + "-first-use.json")
        receipt_raw = _raw(receipt)
        _write(receipt_path, receipt_raw)
        active = json.loads(original)
        active["activation"] = {"status": "ACTIVE", "binding_sha256": account.fingerprint,
            "receipt_path": receipt_path.relative_to(root).as_posix(), "receipt_sha256": _sha(receipt_raw)}
        # Validate the exact proposed evidence before changing the account file.
        from dataclasses import replace
        verify_cutover(root, replace(account, activation=active["activation"]))
        checkpoint("before_activation")
        if _read(_repository() / MARKER) != declaration_raw or _read(receipt_path) != receipt_raw:
            raise ValueError("ATLAS_SOLE_EXECUTOR_EVIDENCE_CHANGED")
        verify_cutover(root, replace(account, activation=active["activation"]))
        _write(root / CONFIG, _raw(active), expected=original)
        checkpoint("after_activation")
        verify_cutover(root, load_account_config(root))
        return {"status": "ACCOUNT_READY", "ready": True, "startup_mode": "ATLAS_SOLE_EXECUTOR",
                "activation_changed": True, "broker_read_performed": False, "trader_started": False, "orders_placed": 0}
