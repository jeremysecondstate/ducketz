"""Private, loaded Atlas writer proof. No broker IO, worker start, or lock cleanup.

The host reads this under the existing account cycle lock. A missing heartbeat
never proves absence: complete process observations and the manual lock/status
must agree. An older running Atlas writer remains blocked until human reload.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import uuid

from .prediction import instant
from . import runtime_compatibility as runtime

VERSION = "atlas-intraday-loaded-writer-v1"
CAPABILITY = "atlas-shared-account-writer-v1"
HEARTBEAT_PATH = Path("intraday/atlas-writer-heartbeat.json")
STATUS_PATH = Path("intraday/manual-session.json")
LOCK_PATH = Path("locks/intraday-manual-session.lock")


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _read(path):
    raw = path.read_bytes()
    if len(raw) > 262144:
        raise ValueError("Oversized writer evidence")
    def pairs(items):
        value = dict(items)
        if len(value) != len(items):
            raise ValueError("Duplicate writer evidence")
        return value
    def invalid(_value):
        raise ValueError("Nonfinite writer evidence")
    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(result, dict):
        raise ValueError("Writer object required")
    return result


def _path(value):
    return os.path.normcase(str(Path(value).resolve()))


def _birth(value):
    if type(value) not in (int, float, str):
        raise ValueError("Exact process birth required")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Exact process birth required")
    return value


def _same_birth(left, right):
    return math.isclose(_birth(left), _birth(right), rel_tol=0, abs_tol=1e-6)


def _candidates(inventory):
    if not isinstance(inventory, list):
        raise ValueError("Complete process inventory required")
    result = []
    for process in inventory:
        if (not isinstance(process, dict) or type(process.get("pid")) is not int
                or process["pid"] <= 0 or not isinstance(process.get("command_line"), list)
                or not process["command_line"]
                or not all(isinstance(arg, str) for arg in process["command_line"])):
            raise ValueError("Complete process identity required")
        text = " ".join(process["command_line"]).replace("\\", "/")
        if any(marker in text for marker in ("ml.intraday.live_session", "intraday/live_session.py",
                                             "ml.intraday.account_execution", "IntradayAccountExecutor")):
            result.append(process)
    if len({item["pid"] for item in result}) != len(result):
        raise ValueError("Duplicate process inventory")
    return result


def _command(process, repository, datastore):
    argv = process["command_line"]
    if len(argv) < 4:
        return False
    allowed = {_path(item) for item in (Path(repository)/".venv/Scripts/python.exe",
               sys.executable, getattr(sys, "_base_executable", sys.executable))}
    if _path(argv[0]) not in allowed or _path(process.get("executable", "")) not in allowed:
        return False
    index = 1
    while index < len(argv) and argv[index] in {"-u", "-B"}:
        index += 1
    if argv[index:index+2] != ["-m", "ml.intraday.live_session"]:
        return False
    options = {}
    rest = iter(argv[index+2:])
    for option in rest:
        if option in options:
            return False
        if option == "--live":
            options[option] = True
        elif option in {"--repository", "--datastore", "--profile", "--watchlist",
                         "--publication-offset-seconds", "--symbol-workers", "--lookback-days"}:
            options[option] = next(rest, None)
            if options[option] is None or options[option].startswith("--"):
                return False
        else:
            return False
    return (options.get("--live") is True
        and _path(options.get("--repository", repository)) == _path(repository)
        and _path(options.get("--datastore", "C:/DATASTORE")) == _path(datastore))


def _session(status, lock, *, now, pid, birth, session_started_at=None):
    if (status.get("schema_version") != "manual-atlas-15m-session-v1" or status.get("pc") != "Atlas"
            or status.get("status") != "RUNNING" or status.get("manual_start") is not True
            or type(status.get("pid")) is not int or status["pid"] != pid
            or not _same_birth(status.get("process_create_time"), birth)
            or not re.fullmatch(r"manual-atlas-15m:[a-f0-9]{32}", status.get("session_id", ""))):
        raise ValueError("Running manual Atlas session required")
    if (lock.get("process") != "intraday-manual-session" or lock.get("pid") != str(pid)
            or not re.fullmatch(r"[a-f0-9]{32}", lock.get("token", ""))
            or not _same_birth(lock.get("owner_created_at"), birth)):
        raise ValueError("Exact manual session lock required")
    start = instant(status["started_at"])
    if (not datetime.fromtimestamp(_birth(birth), timezone.utc) <= start <= instant(lock["started_at"]) <= now
            or not start <= instant(status["heartbeat_at"]) <= now
            or session_started_at is not None and instant(session_started_at) != start):
        raise ValueError("Manual session lifetime differs")


def _callbacks(executor, repository, datastore, status, birth):
    code = compile((Path(repository)/"ml/intraday/live_session.py").read_bytes(),
                   "ml/intraday/live_session.py", "exec", dont_inherit=True, optimize=sys.flags.optimize)
    actual = {}
    for name in ("manual_authority", "compatibility"):
        function = getattr(executor, name)
        expected = runtime.code_fingerprint(runtime._source_function(code, "run_manual."+name))
        actual[name] = runtime.code_fingerprint(function)
        if actual[name] != expected:
            raise ValueError("Loaded manual callback differs")
    function = executor.manual_authority
    cells = dict(zip(function.__code__.co_freevars, (cell.cell_contents for cell in function.__closure__ or ())))
    stopped = cells["stopped"]
    if (cells["session_id"] != status["session_id"] or cells["pc"] != "Atlas"
            or _path(cells["status_path"]) != _path(Path(datastore)/STATUS_PATH)
            or not _same_birth(cells["process_create_time"], birth)
            or type(stopped) is not threading.Event
            or getattr(executor.stopped, "__self__", None) is not stopped
            or getattr(executor.stopped, "__func__", None) is not threading.Event.is_set):
        raise ValueError("Manual callback binding differs")
    cells = dict(zip(executor.compatibility.__code__.co_freevars,
                     (cell.cell_contents for cell in executor.compatibility.__closure__ or ())))
    if (_path(cells["root"]) != _path(datastore) or _path(cells["repository"]) != _path(repository)
            or cells["clock"] is not executor.clock
            or cells["read_gameplan_runtime_compatibility"] is not runtime.read_gameplan_runtime_compatibility):
        raise ValueError("Compatibility callback binding differs")
    reader_code = compile((Path(repository)/"ml/intraday/runtime_compatibility.py").read_bytes(),
                          "runtime_compatibility", "exec", dont_inherit=True, optimize=sys.flags.optimize)
    actual["compatibility_reader"] = runtime.code_fingerprint(cells["read_gameplan_runtime_compatibility"])
    if actual["compatibility_reader"] != runtime.code_fingerprint(
            runtime._source_function(reader_code, "read_gameplan_runtime_compatibility")):
        raise ValueError("Loaded compatibility reader differs")
    if executor.stopped() or executor.manual_authority() is not True:
        raise ValueError("Manual authority stopped")
    return actual


def emit_atlas_writer_heartbeat(executor, *, datastore, repository, now,
                                pid=None, process_reader=runtime._process):
    """Emit actual bound code from the writer; never mark disk bytes as loaded.

    Invalid callbacks/old code produce a blocked proof, replacing prior success.
    Emission cannot enable orders or change existing Atlas stop/start controls.
    """
    pid = os.getpid() if pid is None else pid
    root = Path(datastore)
    proof = {"schema_version": VERSION, "capability_version": CAPABILITY,
             "compatible": False, "pid": pid, "observed_at": instant(now).isoformat(),
             "python_version": list(sys.version_info[:2]), "optimization": sys.flags.optimize,
             "repository": _path(repository), "datastore": _path(root)}
    try:
        status = _read(root/STATUS_PATH)
        raw_lock = (root/LOCK_PATH).read_text(encoding="utf-8")
        lock = runtime.parse_session_lock(raw_lock)
        process = process_reader(pid)
        birth = process["created_at"]
        if process.get("pid") != pid or not _command(process, repository, root):
            raise ValueError("Actual writer process differs")
        _session(status, lock, now=instant(now), pid=pid, birth=birth,
                 session_started_at=executor.session_started_at)
        callbacks = _callbacks(executor, repository, root, status, birth)
        fingerprints = runtime.actual_intraday_executor_fingerprints(executor)
        if fingerprints != runtime.expected_intraday_executor_fingerprints(repository):
            raise ValueError("Loaded writer differs from reviewed code")
        if (root/LOCK_PATH).read_text(encoding="utf-8") != raw_lock or _read(root/STATUS_PATH) != status:
            raise ValueError("Manual session changed during proof")
        proof.update(compatible=True, process_created_at=birth, session_id=status["session_id"],
            session_started_at=status["started_at"], lock_token=lock["token"],
            fingerprints=fingerprints, callback_fingerprints=callbacks)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError, SyntaxError):
        proof["reason_code"] = "ATLAS_LOADED_WRITER_PROOF_UNAVAILABLE"
    try:
        target = root/HEARTBEAT_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name+"."+uuid.uuid4().hex+".tmp")
        try:
            with temporary.open("xb") as output:
                output.write(_encoded(proof))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        return {"compatible": False, "reason_code": "ATLAS_WRITER_HEARTBEAT_WRITE_FAILED"}
    return proof


def read_atlas_runtime_compatibility(datastore, repository, *, as_of=None, now=None,
        maximum_heartbeat_age_seconds=120, process_reader=runtime._process,
        process_inventory=runtime._inventory):
    """Read under the common account cycle lock; absence also needs that lock."""
    result = {"ready": False, "compatibility_state": "blocked", "reason_codes": [],
              "loaded_code_proven": False, "schema_version": VERSION,
              "workers_changed": False, "orders_placed": 0}
    def reject(reason):
        return {**result, "reason_codes": [reason]}
    try:
        current = instant(as_of if as_of is not None else now)
        if as_of is not None and now is not None and instant(as_of) != instant(now):
            raise ValueError("Conflicting observation times")
        age = float(maximum_heartbeat_age_seconds)
        if not math.isfinite(age) or age <= 0:
            raise ValueError("Positive heartbeat bound required")
        root = Path(datastore)
        first = _candidates(process_inventory())
        expected = runtime.expected_intraday_executor_fingerprints(repository)
        if not first:
            # Preserve stale locks for their owner/manual recovery; never clear
            # or silently treat an unreadable/reused-PID lock as absence.
            if (root/LOCK_PATH).exists():
                return reject("ATLAS_MANUAL_LOCK_WITHOUT_VERIFIED_WRITER")
            status = _read(root/STATUS_PATH) if (root/STATUS_PATH).exists() else None
            if status is not None and (status.get("schema_version") != "manual-atlas-15m-session-v1"
                    or status.get("pc") != "Atlas" or status.get("status") not in {"STOPPED", "FAILED"}):
                return reject("ATLAS_SESSION_ABSENCE_UNVERIFIED")
            if _candidates(process_inventory()) or (root/LOCK_PATH).exists():
                return reject("ATLAS_INVENTORY_CHANGED_DURING_CHECK")
            evidence = {"observed_at": current.isoformat(), "inventory": [],
                        "installed_fingerprints": expected, "manual_status": status}
            return {**result, "ready": True, "compatibility_state": "absent", "reason_codes": [],
                    "observed_at": current.isoformat(), "checked_at": current.isoformat(),
                    "expires_at": (current+timedelta(seconds=age)).isoformat(), "proof_sha256": _hash(evidence)}
        lock_text = (root/LOCK_PATH).read_text(encoding="utf-8")
        lock = runtime.parse_session_lock(lock_text)
        status = _read(root/STATUS_PATH)
        proof = _read(root/HEARTBEAT_PATH)
        pid = status["pid"]
        process = process_reader(pid)
        birth = process["created_at"]
        if process.get("pid") != pid or not _command(process, repository, root):
            return reject("ATLAS_LIVING_PROCESS_IDENTITY_UNVERIFIED")
        _session(status, lock, now=current, pid=pid, birth=birth)
        if not 0 <= (current-instant(status["heartbeat_at"])).total_seconds() <= age:
            return reject("ATLAS_MANUAL_HEARTBEAT_STALE_OR_FUTURE")
        if (proof.get("schema_version") != VERSION or proof.get("capability_version") != CAPABILITY
                or proof.get("compatible") is not True or proof.get("pid") != pid
                or not _same_birth(proof.get("process_created_at"), birth)
                or proof.get("session_id") != status["session_id"]
                or proof.get("session_started_at") != status["started_at"]
                or proof.get("lock_token") != lock["token"]
                or proof.get("python_version") != list(sys.version_info[:2])
                or proof.get("optimization") != sys.flags.optimize
                or proof.get("repository") != _path(repository) or proof.get("datastore") != _path(root)):
            return reject("ATLAS_LOADED_PROOF_IDENTITY_UNVERIFIED")
        if not instant(status["started_at"]) <= instant(proof["observed_at"]) <= current or not 0 <= (
                current-instant(proof["observed_at"])).total_seconds() <= age:
            return reject("ATLAS_LOADED_HEARTBEAT_STALE_OR_FUTURE")
        module = compile((Path(repository)/"ml/intraday/live_session.py").read_bytes(),
                         "ml/intraday/live_session.py", "exec", dont_inherit=True, optimize=sys.flags.optimize)
        callbacks = {name: runtime.code_fingerprint(runtime._source_function(module, "run_manual."+name))
                     for name in ("manual_authority", "compatibility")}
        reader_code = compile((Path(repository)/"ml/intraday/runtime_compatibility.py").read_bytes(),
                              "runtime_compatibility", "exec", dont_inherit=True, optimize=sys.flags.optimize)
        callbacks["compatibility_reader"] = runtime.code_fingerprint(
            runtime._source_function(reader_code, "read_gameplan_runtime_compatibility"))
        if proof.get("fingerprints") != expected or proof.get("callback_fingerprints") != callbacks:
            return reject("ATLAS_LOADED_CODE_DIFFERS_FROM_REVIEWED_SOURCE")
        # Windows virtualenv launcher may be a matching parent, never a second
        # independent writer. Every inventory snapshot must retain this identity.
        for candidates in (first, _candidates(process_inventory())):
            if not any(item == process for item in candidates):
                return reject("ATLAS_INVENTORY_CHANGED_DURING_CHECK")
            for other in candidates:
                if other["pid"] == pid:
                    continue
                if (other["pid"] != process.get("parent_pid") or not _command(other, repository, root)
                        or other["command_line"] != process["command_line"]
                        or not _birth(other["created_at"]) <= _birth(birth)):
                    return reject("OTHER_ATLAS_WRITER_UNVERIFIED")
        again = process_reader(pid)
        if again != process or (root/LOCK_PATH).read_text(encoding="utf-8") != lock_text:
            return reject("ATLAS_PROCESS_OR_LOCK_CHANGED_DURING_CHECK")
        latest = _read(root/STATUS_PATH)
        if any(latest.get(key) != status.get(key) for key in status if key != "heartbeat_at"):
            return reject("ATLAS_MANUAL_SESSION_CHANGED_DURING_CHECK")
        latest_heartbeat = instant(latest["heartbeat_at"])
        if not 0 <= (current-latest_heartbeat).total_seconds() <= age:
            return reject("ATLAS_MANUAL_HEARTBEAT_STALE_OR_FUTURE")
        proof_time = instant(proof["observed_at"])
        expires = min(proof_time, latest_heartbeat)+timedelta(seconds=age)
        return {**result, "ready": True, "compatibility_state": "compatible", "reason_codes": [],
                "loaded_code_proven": True, "observed_at": proof_time.isoformat(),
                "heartbeat_at": proof_time.isoformat(), "checked_at": current.isoformat(),
                "expires_at": expires.isoformat(),
                "proof_sha256": _hash(proof), "worker_pid": pid}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError, SyntaxError):
        return reject("ATLAS_RUNTIME_EVIDENCE_UNAVAILABLE")
