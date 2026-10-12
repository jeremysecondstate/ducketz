"""Require proof from loaded Gameplan code before a second execution stream.

Disk fingerprints alone do not identify an already running Python worker. The
updated runtime emits fingerprints of its actual function code objects into its
existing cycle/status result. The reader binds those to a living process birth,
the existing session lock, a fresh heartbeat and the reviewed source functions.
This check never starts/stops workers or opens providers, brokers or databases.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from types import CodeType

from .prediction import instant

VERSION = "gameplan-intraday-loaded-compatibility-v1"
CAPABILITY = "gameplan-excludes-explicit-intraday-cancellation-and-expiry-v1"
REQUIRED_CODE = {
    "ml/stock_trader/independent_runtime.py": (
        "run_independent_stock_trader_once", "_cancel_expired_entries", "_exit_decisions",
        "_StreamCompatibleStockTraderRunResult.to_dict"),
    "ml/stock_trader/horizon_ledger.py": (
        "HorizonLedger._reservation", "HorizonLedger.snapshot", "HorizonLedger.due_exits",
        "HorizonLedger._snapshot", "HorizonLedger._stream", "HorizonLedger._intraday_share_accounting",
        "HorizonLedger._apply_order", "HorizonLedger._attribute_intraday_fills", "HorizonLedger._close_empty_allocations",
        "HorizonLedger.reservation_stream", "HorizonLedger.allocation_stream", "HorizonLedger.reserve_stream_cancellation"),
    "ml/intraday/cancellation.py": (
        "reservation_stream", "allocation_stream", "gameplan_reservation", "gameplan_allocation"),
    "ml/account_gameplan/authority.py": (
        "AccountAuthority.__init__", "AccountAuthority.acquire_lease", "AccountAuthority._lease",
        "AccountAuthority._remaining", "AccountAuthority._intraday_remaining",
        "AccountAuthority._submission_snapshot", "AccountAuthority.submit", "AccountAuthority.reconcile",
        "AccountAuthority._submission_receipt_value", "AccountAuthority.submission_receipt",
        "AccountAuthority._record_submission_receipt", "AccountAuthority._recover_submission_receipts",
        "AccountAuthority.recover_submission_receipts"),
}


def _constant(value):
    if isinstance(value, CodeType):
        return _code(value)
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, tuple):
        return {"tuple": [_constant(item) for item in value]}
    if isinstance(value, frozenset):
        items = [_constant(item) for item in value]
        return {"frozenset": sorted(items, key=lambda item: json.dumps(item, sort_keys=True))}
    if isinstance(value, float):
        return {"float": value.hex()}
    if isinstance(value, complex):
        return {"complex": [value.real.hex(), value.imag.hex()]}
    if value is Ellipsis:
        return {"ellipsis": True}
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValueError("Unsupported compiled constant")


def _code(value):
    return {"name": value.co_name, "qualname": value.co_qualname,
            "arguments": [value.co_argcount, value.co_posonlyargcount, value.co_kwonlyargcount],
            "locals": value.co_nlocals, "stack": value.co_stacksize, "flags": value.co_flags,
            "instructions": value.co_code.hex(), "exceptions": value.co_exceptiontable.hex(),
            "names": list(value.co_names), "variables": list(value.co_varnames),
            "freevars": list(value.co_freevars), "cellvars": list(value.co_cellvars),
            "constants": [_constant(item) for item in value.co_consts]}


def code_fingerprint(function):
    """Fingerprint actual semantics without pathname or source-line metadata."""
    function = getattr(function, "__func__", function)
    code = function if isinstance(function, CodeType) else function.__code__
    return hashlib.sha256(json.dumps(_code(code), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _source_function(module_code, qualified_name):
    code = module_code
    for name in qualified_name.split("."):
        found = [item for item in code.co_consts if isinstance(item, CodeType) and item.co_name == name]
        if len(found) != 1:
            raise ValueError("Required source capability absent")
        code = found[0]
    return code


def expected_fingerprints(repository):
    """Compile reviewed source without executing its imports or module body."""
    result = {}
    for relative, names in REQUIRED_CODE.items():
        code = compile((Path(repository)/relative).read_bytes(), relative, "exec", dont_inherit=True, optimize=sys.flags.optimize)
        for name in names:
            result[relative+":"+name] = code_fingerprint(_source_function(code, name))
    return result


def _actual_functions(runtime_namespace, ledger_class):
    from . import cancellation
    from ml.account_gameplan.authority import AccountAuthority
    output = {}
    for relative, names in REQUIRED_CODE.items():
        for qualified in names:
            parts = qualified.split(".")
            if relative.endswith("independent_runtime.py"):
                value = runtime_namespace[parts[0]]
            elif relative.endswith("horizon_ledger.py"):
                value = ledger_class
            elif relative.endswith("authority.py"):
                value = AccountAuthority
            else:
                value = getattr(cancellation, parts[0])
            for name in parts[1:]:
                value = getattr(value, name)
            output[relative+":"+qualified] = code_fingerprint(value)
    return output


def emit_loaded_attestation(runtime_namespace, ledger_class, *, observed_at, session_managed,
                            pid=None, process_created_at=None):
    """Called by the updated running runtime; never infer loaded code from disk."""
    pid = os.getpid() if pid is None else pid
    if process_created_at is None:
        try:
            import psutil
            process_created_at = psutil.Process(pid).create_time()
        except Exception:
            process_created_at = None
    proof = {"schema_version": VERSION, "capability_version": CAPABILITY,
             "python_version": list(sys.version_info[:2]), "optimization": sys.flags.optimize,
             "pid": pid, "process_created_at": process_created_at,
             "observed_at": instant(observed_at).isoformat(), "session_managed": bool(session_managed),
             "compatible": False, "fingerprints": {}}
    try:
        proof["fingerprints"] = _actual_functions(runtime_namespace, ledger_class)
        proof["compatible"] = True
    except (KeyError, AttributeError, TypeError, ValueError):
        proof["reason_code"] = "LOADED_NATIVE_STREAM_INTERFACES_UNAVAILABLE"
    return proof


def _process(pid):
    import psutil
    try:
        process = psutil.Process(pid)
        return {"pid": process.pid, "created_at": process.create_time(), "parent_pid": process.ppid(),
                "executable": process.exe(), "command_line": process.cmdline()}
    except (psutil.Error, OSError) as exc:
        raise ValueError("Process identity unavailable") from exc


def _inventory():
    """Read all Python process commands; inaccessible candidates block absence."""
    import psutil
    output = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            name = process.info["name"]
            if not isinstance(name, str):
                raise ValueError("Process name unavailable")
            if "python" not in name.lower():
                continue
            output.append(_process(process.pid))
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError) as exc:
            raise ValueError("Process inventory incomplete") from exc
    return output


def _gameplan_commands(inventory):
    result = []
    if not isinstance(inventory, list):
        raise ValueError("Complete process inventory required")
    for process in inventory:
        if not isinstance(process, dict) or not isinstance(process.get("command_line"), list):
            raise ValueError("Complete process command required")
        if any(isinstance(item, str) and ("ml.gameplan_stock_trader" in item or "ml.stock_trader" in item
                                         or "gameplan_stock_trader.py" in item) for item in process["command_line"]):
            result.append(process)
    return result


def parse_session_lock(text):
    pairs = [line.split("=", 1) for line in text.splitlines() if line]
    if any(len(pair) != 2 for pair in pairs):
        raise ValueError("Invalid session lock")
    result = dict(pairs)
    if len(result) != len(pairs) or set(result) != {"process", "pid", "owner_created_at", "started_at", "token"}:
        raise ValueError("Exact birth-aware session lock required")
    return result


def _worker_command(process, repository):
    argv = process.get("command_line")
    if not isinstance(argv, list) or len(argv) < 4 or argv[1:4] != ["-u", "-m", "ml.gameplan_stock_trader"]:
        return False
    allowed = {os.path.normcase(str(Path(item).resolve())) for item in (
        Path(repository)/".venv/Scripts/python.exe", sys.executable, getattr(sys, "_base_executable", sys.executable))}
    if (os.path.normcase(str(Path(argv[0]).resolve())) not in allowed
            or os.path.normcase(str(Path(process.get("executable", "")).resolve())) not in allowed):
        return False
    options = {}
    flags = {"--execute", "--run-session", "--wait-for-open"}
    arguments = {"--datastore-target", "--target-horizon", "--sizing-policy", "--late-opening-date", "--resume-quote-run", "--resume-quote-symbol"}
    rest = iter(argv[4:])
    for option in rest:
        if option in options:
            return False
        if option in flags:
            options[option] = True
        elif option in arguments:
            options[option] = next(rest, None)
            if options[option] is None:
                return False
        else:
            return False
    return (options.get("--execute") is True and options.get("--run-session") is True
            and options.get("--datastore-target") == "pc" and options.get("--target-horizon") == "all"
            and options.get("--sizing-policy") == "gameplan-direction-current-market-v1")


def verify_runtime_compatibility(*, repository, status, lock, as_of,
                                 maximum_heartbeat_age_seconds, process_reader=_process):
    """Return bounded evidence. Fresh disk bytes do not replace loaded proof.

    process_reader is an injectable read-only process-identity adapter for tests
    or a caller's existing verified process snapshot, never a worker launcher.
    Heartbeat age is explicit configuration; it is not an order/forecast timeout.
    """
    result = {"ready": False, "schema_version": VERSION, "reason_codes": [],
              "loaded_code_proven": False, "orders_placed": 0, "workers_changed": False}
    def reject(reason):
        result["reason_codes"].append(reason)
        return result
    try:
        now = instant(as_of)
        age_limit = float(maximum_heartbeat_age_seconds)
        if not math.isfinite(age_limit) or age_limit <= 0:
            return reject("EXPLICIT_POSITIVE_HEARTBEAT_AGE_REQUIRED")
        if (not isinstance(status, dict) or not isinstance(lock, dict)
                or status.get("schema_version") != "independent-stock-session-status-v1"
                or status.get("status") != "RUNNING" or status.get("execute") is not True
                or status.get("sizing_policy") != "gameplan-direction-current-market-v1"):
            return reject("RUNNING_GAMEPLAN_SESSION_REQUIRED")
        if lock.get("process") != "independent-stock-session":
            return reject("GAMEPLAN_SESSION_LOCK_UNVERIFIED")
        if (set(lock) != {"process", "pid", "owner_created_at", "started_at", "token"}
                or not isinstance(lock.get("token"), str) or not re.fullmatch("[a-f0-9]{32}", lock["token"])):
            return reject("GAMEPLAN_SESSION_LOCK_UNVERIFIED")
        pid = status.get("pid")
        if type(pid) is not int or pid <= 0 or str(pid) != lock.get("pid"):
            return reject("GAMEPLAN_PROCESS_IDENTITY_UNVERIFIED")
        heartbeat = instant(status["heartbeat_at"])
        if not 0 <= (now-heartbeat).total_seconds() <= age_limit:
            return reject("GAMEPLAN_HEARTBEAT_STALE_OR_FUTURE")
        proof = (status.get("last_cycle") or {}).get("intraday_runtime_compatibility")
        if (not isinstance(proof, dict) or proof.get("schema_version") != VERSION
                or proof.get("capability_version") != CAPABILITY or proof.get("compatible") is not True
                or proof.get("session_managed") is not True):
            return reject("LOADED_GAMEPLAN_COMPATIBILITY_PROOF_REQUIRED")
        if proof.get("python_version") != list(sys.version_info[:2]) or proof.get("optimization") != sys.flags.optimize:
            return reject("LOADED_RUNTIME_COMPILER_DIFFERS")
        if proof.get("pid") != pid or not instant(status["started_at"]) <= instant(proof["observed_at"]) <= heartbeat:
            return reject("LOADED_PROOF_SESSION_OR_TIME_MISMATCH")
        birth = float(lock["owner_created_at"])
        if not math.isfinite(birth) or birth <= 0 or not math.isclose(float(proof["process_created_at"]), birth, rel_tol=0, abs_tol=1e-6):
            return reject("LOADED_PROOF_PROCESS_BIRTH_MISMATCH")
        process = process_reader(pid)
        if (process.get("pid") != pid or not math.isclose(float(process["created_at"]), birth, rel_tol=0, abs_tol=1e-6)
                or not _worker_command(process, repository)):
            return reject("LIVING_GAMEPLAN_COMMAND_OR_BIRTH_UNVERIFIED")
        if not datetime.fromtimestamp(birth, timezone.utc) <= instant(lock["started_at"]) <= now:
            return reject("GAMEPLAN_LOCK_LIFETIME_UNVERIFIED")
        if proof.get("fingerprints") != expected_fingerprints(repository):
            return reject("LOADED_GAMEPLAN_CODE_DIFFERS_FROM_REVIEWED_SOURCE")
        result.update(ready=True, loaded_code_proven=True, worker_pid=pid,
                      capability_version=CAPABILITY, heartbeat_at=heartbeat.isoformat())
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError, SyntaxError):
        return reject("GAMEPLAN_COMPATIBILITY_EVIDENCE_UNAVAILABLE")


def read_gameplan_runtime_compatibility(datastore, repository, *, as_of,
                                      maximum_heartbeat_age_seconds, process_reader=_process,
                                      process_inventory=_inventory, allow_verified_absence=True):
    """Prove a loaded compatible worker or complete read-only worker absence.

    Stale/missing status is never absence evidence. A complete live process
    inventory must find no supported manual or scheduled Gameplan command, and
    the installed source must contain every compatible interface. The caller
    serializes this check and each broker operation under the common cycle lock.
    """
    blocked = {"ready": False, "schema_version": VERSION, "loaded_code_proven": False,
               "orders_placed": 0, "workers_changed": False}
    try:
        now = instant(as_of)
        age_limit = float(maximum_heartbeat_age_seconds)
        if not math.isfinite(age_limit) or age_limit <= 0:
            raise ValueError("Explicit positive heartbeat age required")
        candidates = _gameplan_commands(process_inventory())
        expected = expected_fingerprints(repository)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError, SyntaxError):
        return {**blocked, "reason_codes": ["COMPLETE_GAMEPLAN_INVENTORY_OR_INSTALLED_CAPABILITY_UNAVAILABLE"]}
    if not candidates and allow_verified_absence:
        try:
            if _gameplan_commands(process_inventory()):
                return {**blocked, "reason_codes": ["GAMEPLAN_INVENTORY_CHANGED_DURING_CHECK"]}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError):
            return {**blocked, "reason_codes": ["COMPLETE_GAMEPLAN_INVENTORY_UNAVAILABLE"]}
        return {**blocked, "ready": True, "reason_codes": [], "mode": "verified_gameplan_absent",
                "observed_at": now.isoformat(), "reviewed_installed_code_checked": True,
                "installed_code_fingerprints": expected}
    try:
        root = Path(datastore)
        status = json.loads((root/"state/independent-stock-trader/session-status.json").read_text(encoding="utf-8"))
        lock = parse_session_lock((root/"locks/independent-stock-session.lock").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {**blocked, "reason_codes": ["GAMEPLAN_STATUS_OR_LOCK_UNAVAILABLE"]}
    result = verify_runtime_compatibility(repository=repository, status=status, lock=lock, as_of=as_of,
        maximum_heartbeat_age_seconds=maximum_heartbeat_age_seconds, process_reader=process_reader)
    if result["ready"]:
        try:
            worker = process_reader(status["pid"])
            if not any(candidate.get("pid") == worker["pid"] for candidate in candidates):
                return {**blocked, "reason_codes": ["GAMEPLAN_INVENTORY_CHANGED_DURING_CHECK"]}
            for candidate in candidates:
                if candidate.get("pid") == worker["pid"]:
                    continue
                if (candidate.get("pid") != worker.get("parent_pid") or not _worker_command(candidate, repository)
                        or candidate.get("command_line") != worker.get("command_line")
                        or not candidate.get("created_at", float("inf")) <= worker["created_at"]):
                    return {**blocked, "reason_codes": ["OTHER_GAMEPLAN_WORKER_UNVERIFIED"]}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return {**blocked, "reason_codes": ["LIVING_GAMEPLAN_IDENTITY_CHANGED_DURING_CHECK"]}
        result["mode"] = "loaded_gameplan_verified"
    return result
