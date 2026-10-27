"""Kernel lifetime anchor. Run ONLY from an explicit manual trader launcher.

No broker, strategy, dependency imports, recovery decisions, or restart-on-error
policy live here. Every lane turn loads its controller adapter in a fresh process.
The lifetime protocol is deliberately fixed; unknown protocol versions fail closed.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

PROTOCOL = "manual-trader-renewal-v1"
PLANNED_EXIT = 75
HOST_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _control_revision(raw, info, key):
    if key is not None:
        rows = [line.strip() for line in raw.decode("utf-8").splitlines() if line.strip()]
        parts = rows[0].split("=", 1) if len(rows) == 1 else []
        active = len(parts) == 2 and [part.strip() for part in parts] == [key, "TRUE"]
    else:
        value = json.loads(raw)
        active = isinstance(value, dict) and value.get("status") == "RUNNING"
    return {"sha256": hashlib.sha256(raw).hexdigest(), "mtime_ns": info.st_mtime_ns,
            "file_id": info.st_ino, "active": active}


def control_signatures(envelope):
    """Fixed protocol evidence: detect a new stop even if it repeats FALSE."""
    root = Path(envelope["datastore"])
    paths = ({"controls/stock-trader/operator-intent.txt": "CONFIRM_ACTIVE_TRADING",
              "controls/gameplan-stock-trader/operator-intent.txt": "CONFIRM_GAMEPLAN_STOCK_TRADING"}
             if envelope["trader"] == "gameplan" else {"intraday/manual-session.json": None})
    result = {}
    for relative, key in paths.items():
        try:
            with (root / relative).open("rb") as stream:
                raw, info = stream.read(), os.fstat(stream.fileno())
            result[relative] = _control_revision(raw, info, key)
        except FileNotFoundError:
            result[relative] = {"missing": True, "active": False}
        except (OSError, ValueError, UnicodeError):
            result[relative] = {"unreadable": True, "active": False}
    return result


def initialization_stop_changed(envelope):
    original = envelope.get("initial_controls", {})
    return any(not current["active"] and current != original.get(path)
               for path, current in control_signatures(envelope).items())


@contextlib.contextmanager
def _exclusive_initial_file(path, *, missing):
    """Exclude successful competing writes/replacements for initial activation."""
    if os.name != "nt":
        raise RuntimeError("Guarded manual activation requires Windows file sharing semantics")
    import ctypes as c
    from ctypes import wintypes as w
    import msvcrt
    k = c.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = (w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE)
    k.CreateFileW.restype = w.HANDLE
    k.CloseHandle.argtypes = (w.HANDLE,)
    handle = k.CreateFileW(str(path), 0xC0000000, 1, None, 1 if missing else 3, 0x80, None)
    if handle == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        k.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        yield stream


def guarded_initial_control(envelope, relative, content):
    """Activate once without overwriting a successful concurrent manual stop.

    The initial inactive revision is permitted only while unchanged. Compare
    and write use one handle denying other writers and replacement. Official
    stop APIs additionally latch before attempting their control-file write.
    """
    keys = ({"controls/stock-trader/operator-intent.txt": "CONFIRM_ACTIVE_TRADING",
             "controls/gameplan-stock-trader/operator-intent.txt": "CONFIRM_GAMEPLAN_STOCK_TRADING"}
            if envelope["trader"] == "gameplan" else {"intraday/manual-session.json": None})
    key = keys[relative]
    expected = envelope["initial_controls"][relative]
    target = Path(envelope["datastore"]) / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    stop_path = Path(envelope["stop_path"])

    def changed_stop():
        write_json(stop_path, {"reason": "MANUAL_STOP", "token": envelope["token"],
                             "at": datetime.now(timezone.utc).isoformat()})
        return False

    try:
        with _exclusive_initial_file(target, missing=bool(expected.get("missing"))) as stream:
            raw, info = stream.read(), os.fstat(stream.fileno())
            try:
                current = ({"missing": True, "active": False} if expected.get("missing")
                           else _control_revision(raw, info, key))
            except (ValueError, UnicodeError):
                return changed_stop()
            if not current["active"] and current != expected:
                return changed_stop()
            if stop_path.exists():
                return False
            # Retain the exact displaced control as private session evidence.
            write_json(stop_path.parent / ("initial-control-" + uuid.uuid4().hex + ".json"),
                       {"relative": relative, "revision": current, "bytes_hex": raw.hex()})
            stream.seek(0)
            if stream.write(content) != len(content):
                raise OSError("Incomplete initial control write")
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        # CREATE_NEW may lose to a successful stop write. Other errors remain
        # explicit failures; no fallback may overwrite an unobserved revision.
        current = control_signatures(envelope)[relative]
        if not current["active"] and current != expected:
            return changed_stop()
        raise
    return not stop_path.exists()


def read_stop_reason(path):
    """Preserve a controller failure instead of relabelling it as user input."""
    try:
        value = json.loads(Path(path).read_text("utf-8"))
        return "FAILURE" if isinstance(value, dict) and value.get("reason") == "FAILURE" else "MANUAL_STOP"
    except (OSError, ValueError):
        return "MANUAL_STOP"


def intraday_status(envelope, status, *, done=(), **extra):
    session = envelope["session"]
    return dict(session, status=status, pid=envelope["owner_pid"],
                schema_version="manual-local-15m-session-v2", manual_start=True,
                heartbeat_at=datetime.now(timezone.utc).isoformat(),
                market_close_at=session.get("close_at"), final_collection_complete="candles" in done,
                renewal_protocol=PROTOCOL, **extra)


def birth(pid):
    # Kept within the small stdlib anchor so editable application imports cannot
    # enter the parent. Representation matches runtime_lock's psutil timestamp.
    if os.name != "nt":
        try:
            return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        except OSError:
            return None
    import ctypes
    from ctypes import wintypes as w
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
    k.OpenProcess.restype = w.HANDLE
    k.GetProcessTimes.argtypes = (w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4))
    k.GetExitCodeProcess.argtypes = (w.HANDLE, ctypes.POINTER(w.DWORD))
    k.CloseHandle.argtypes = (w.HANDLE,)
    handle = k.OpenProcess(0x1000, False, int(pid))
    if not handle:
        if ctypes.get_last_error() == 87:
            return None
        raise RuntimeError("Cannot inspect existing session owner")
    try:
        values = [w.FILETIME() for _ in range(4)]
        code = w.DWORD()
        if not k.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)) or not k.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise RuntimeError("Cannot verify existing session owner")
        return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime) if code.value == 259 else None
    finally:
        k.CloseHandle(handle)


_JOB = None


def bind_kernel_lifetime():
    """Assign this anchor before spawning anything; all descendants inherit it.

    The non-inheritable job handle exists only here. Abrupt anchor termination
    or console closure kills every descendant, including virtualenv launchers.
    """
    global _JOB
    if os.name != "nt" or _JOB is not None:
        return
    import ctypes as c
    from ctypes import wintypes as w
    class Basic(c.Structure):
        _fields_ = [("ProcessTime", c.c_longlong), ("JobTime", c.c_longlong),
                    ("Flags", w.DWORD), ("Minimum", c.c_size_t), ("Maximum", c.c_size_t),
                    ("Active", w.DWORD), ("Affinity", c.c_size_t), ("Priority", w.DWORD), ("Scheduling", w.DWORD)]
    class IO(c.Structure):
        _fields_ = [(name, c.c_ulonglong) for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]
    class Limits(c.Structure):
        _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessMemory", c.c_size_t),
                    ("JobMemory", c.c_size_t), ("PeakProcess", c.c_size_t), ("PeakJob", c.c_size_t)]
    k = c.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes = (c.c_void_p, w.LPCWSTR)
    k.CreateJobObjectW.restype = w.HANDLE
    k.SetInformationJobObject.argtypes = (w.HANDLE, c.c_int, c.c_void_p, w.DWORD)
    k.AssignProcessToJobObject.argtypes = (w.HANDLE, w.HANDLE)
    k.GetCurrentProcess.restype = w.HANDLE
    k.CloseHandle.argtypes = (w.HANDLE,)
    handle = k.CreateJobObjectW(None, None)
    limits = Limits()
    limits.Basic.Flags = 0x2000
    if not handle or not k.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)) or not k.AssignProcessToJobObject(handle, k.GetCurrentProcess()):
        error = c.get_last_error()
        if handle:
            k.CloseHandle(handle)
        raise OSError(error, "Cannot establish kernel trader lifetime; no worker started")
    _JOB = handle  # Do not close during normal interpreter teardown before exit.


@contextlib.contextmanager
def byte_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()


@contextlib.contextmanager
def own_session(root, trader):
    """Own both new session namespace and the legacy worker exclusion path."""
    lock_name = "independent-stock-session" if trader == "gameplan" else "intraday-manual-session"
    target = root / "locks" / (lock_name + ".lock")
    own_birth = birth(os.getpid())
    with byte_lock(root / "locks" / (lock_name + ".renewal-owner")):
        with byte_lock(root / "locks/.ducketz-runtime-lock-maintenance.lock"):
            if target.exists():
                raw = target.read_text()
                fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
                owner = birth(int(fields["pid"]))
                old_birth = fields.get("owner_created_at")
                if owner is not None:
                    if os.name != "nt":
                        raise RuntimeError("A live legacy or renewed trader already owns this session")
                    seconds = (int(owner) - 116444736000000000) / 10000000
                    if old_birth is None:
                        started = datetime.fromisoformat(fields["started_at"])
                        if started.tzinfo is None or seconds <= started.timestamp():
                            raise RuntimeError("A live legacy or renewed trader already owns this session")
                    elif not math.isfinite(float(old_birth)) or abs(seconds - float(old_birth)) < 0.000005:
                        raise RuntimeError("A live legacy or renewed trader already owns this session")
                # Stale lock retained as evidence; no PID is ever killed.
                target.replace(target.with_name(target.name + ".stale-" + uuid.uuid4().hex))
            # The supported legacy four-field form proves process birth is before
            # acquisition without differing FILETIME/psutil float roundoff.
            payload = f"process={lock_name}\npid={os.getpid()}\nstarted_at={datetime.now(timezone.utc).isoformat()}\ntoken={uuid.uuid4().hex}\n"
            with target.open("x", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        try:
            yield
        finally:
            with byte_lock(root / "locks/.ducketz-runtime-lock-maintenance.lock"):
                if target.exists() and target.read_text() == payload:
                    target.unlink()


def parent_process_id():
    """Read the real parent without importing mutable process libraries."""
    if os.name != "nt":
        return os.getppid()
    import ctypes as c
    from ctypes import wintypes as w
    class Entry(c.Structure):
        _fields_ = [("size", w.DWORD), ("usage", w.DWORD), ("pid", w.DWORD),
                    ("heap", c.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                    ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                    ("exe", w.WCHAR * 260)]
    k = c.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = (w.DWORD, w.DWORD)
    k.CreateToolhelp32Snapshot.restype = w.HANDLE
    k.Process32FirstW.argtypes = (w.HANDLE, c.POINTER(Entry))
    k.Process32NextW.argtypes = (w.HANDLE, c.POINTER(Entry))
    k.CloseHandle.argtypes = (w.HANDLE,)
    handle = k.CreateToolhelp32Snapshot(2, 0)
    if handle == c.c_void_p(-1).value:
        raise OSError(c.get_last_error(), "Cannot verify controller parent")
    try:
        value = Entry()
        value.size = c.sizeof(value)
        present = k.Process32FirstW(handle, c.byref(value))
        while present:
            if value.pid == os.getpid():
                return int(value.parent)
            present = k.Process32NextW(handle, c.byref(value))
    finally:
        k.CloseHandle(handle)
    raise RuntimeError("Controller parent identity unavailable")


def _living(pid, recorded):
    return type(pid) is int and pid > 0 and isinstance(recorded, str) and bool(recorded) and birth(pid) == recorded


def wait_for_controller_relay(path, payload, command, *, stop, stopping, phase):
    """After quiescence the old controller is only a kernel-lifetime relay."""
    stop_path = Path(json.loads(Path(payload["envelope"]).read_text())["stop_path"])
    child = subprocess.Popen([*command, "--continue", str(path)], close_fds=True)
    deadline = None
    try:
        phase("controller_started", "controller")
        while child.poll() is None:
            if stop_path.exists() and not stopping.is_set():
                stop(read_stop_reason(stop_path))
            if stopping.is_set():
                deadline = deadline or time.monotonic() + 10
                if time.monotonic() >= deadline:
                    child.terminate()
                    child.wait()
                    break
            time.sleep(.05)
        phase("controller_exited", "controller")
        result = json.loads(path.with_suffix(".result.json").read_text())
        if (result.get("protocol"), result.get("token"), result.get("handoff_id")) != (
                PROTOCOL, payload["token"], payload["handoff_id"]):
            raise ValueError("Controller result identity mismatch")
        outcome = result["outcome"]
        if outcome["disposition"] not in {"SESSION_COMPLETE", "MANUAL_STOP", "FAILURE"}:
            raise ValueError("Unknown controller outcome")
        if child.returncode != (1 if outcome["disposition"] == "FAILURE" else 0):
            raise ValueError("Controller exit differs from its final receipt")
        return outcome
    except BaseException:
        disposition = stop("FAILURE")
        if child.poll() is None:
            child.terminate()
            child.wait()
        if disposition == "MANUAL_STOP":
            return {"disposition": "MANUAL_STOP", "generations": payload["generations"],
                    "completed_roles": payload["done"], "session_id": json.loads(
                        Path(payload["envelope"]).read_text())["session"]["session_id"]}
        raise


def resume_controller(path, *, commands, phase=lambda *a: None):
    """Consume one parent-bound handoff; never an unattended initial start.

    ``commands(envelope)`` supplies the fresh installed worker and relay selectors.
    No session lock, job, original admission, stop record or clock is recreated.
    """
    path = Path(path).resolve()
    payload = json.loads(path.read_text())
    if (payload.get("protocol") != PROTOCOL or payload.get("host_sha256") != HOST_SHA256
            or parent_process_id() != payload.get("parent_pid")
            or not _living(payload.get("parent_pid"), payload.get("parent_birth"))):
        raise ValueError("Controller handoff has no living exact parent")
    directory = Path(payload["directory"]).resolve()
    envelope_path = Path(payload["envelope"]).resolve()
    if path.parent != directory or envelope_path != directory / "session.json":
        raise ValueError("Controller handoff directory mismatch")
    envelope = json.loads(envelope_path.read_text())
    if (envelope.get("protocol") != PROTOCOL or envelope.get("token") != payload.get("token")
            or not _living(envelope.get("owner_pid"), envelope.get("owner_birth"))):
        raise ValueError("Original manual session lifetime has ended")
    with byte_lock(path.with_suffix(".claim.lock")):
        # The persistent claim prevents replay after a successor exits while
        # an ancestor is still unwinding. A PID or stale request is not authority.
        with path.with_suffix(".consumed.json").open("x", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "birth": birth(os.getpid()), "handoff_id": payload["handoff_id"]}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        command, relay_selector = commands(envelope)
        code = run_protocol(directory=directory, envelope=envelope, command=command,
                            initialize=False, continuation=payload, relay_selector=relay_selector, phase=phase)
        outcome = json.loads((directory / "outcome.json").read_text())
        write_json(path.with_suffix(".result.json"), {"protocol": PROTOCOL, "token": payload["token"],
                   "handoff_id": payload["handoff_id"], "outcome": outcome})
        return code


def run_protocol(*, directory, envelope, command, initialize=True, phase=lambda *a: None,
                 continuation=None, relay_selector=None):
    """Run exact, acknowledged handoffs. Used unchanged by subprocess fixtures.

    command(role, request) selects a complete installed generation and returns
    argv. It executes no broker actions. Only exit 75 + matching durable receipt
    can renew. Unknown exceptions/exits terminate the session, never retry.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    envelope_path = directory / "session.json"
    stop_path = directory / "stop.json"
    if continuation is None:
        envelope = dict(envelope, protocol=PROTOCOL, owner_pid=os.getpid(), owner_birth=birth(os.getpid()),
                        token=uuid.uuid4().hex, stop_path=str(stop_path))
        if envelope.get("check_controls", True):
            envelope["initial_controls"] = control_signatures(envelope)
        write_json(envelope_path, envelope)
    else:
        # A relay inherits the ORIGINAL manual admission and kernel owner. It
        # never resets session time, activation controls or the stop latch.
        if envelope.get("protocol") != PROTOCOL or Path(envelope["stop_path"]) != stop_path:
            raise ValueError("Incompatible controller continuation")
        initialize = False
    active_path = Path(envelope["datastore"]) / "state/trader-renewal" / envelope["trader"] / "active.json"
    active = {key: envelope[key] for key in ("protocol", "trader", "owner_pid", "owner_birth", "token", "stop_path")}
    active.update(status="RUNNING", session_id=envelope["session"]["session_id"],
                  controller_pid=os.getpid(), heartbeat_at=datetime.now(timezone.utc).isoformat())
    if continuation is None:
        write_json(active_path, active)
    stopping = threading.Event()
    reason = None
    children = {}
    states = {} if continuation is None else dict(continuation["states"])
    done = set() if continuation is None else set(continuation["done"])
    generations = {} if continuation is None else dict(continuation["generations"])
    relay_target = None
    final_execution_started = False
    previous = {}

    def stop(why):
        nonlocal reason
        if not stopping.is_set():
            reason = why
            stopping.set()
            write_json(stop_path, {"reason": why, "at": datetime.now(timezone.utc).isoformat()})
        return reason

    def signal_stop(*args):
        stop("MANUAL_STOP")

    for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", signal.SIGTERM), signal.SIGTERM):
        previous[sig] = signal.signal(sig, signal_stop)
    roles = ["initialize"] if initialize else list(envelope["roles"])
    try:
        while True:
            if birth(envelope["owner_pid"]) != envelope["owner_birth"]:
                stop("FAILURE")
            if stop_path.exists() and not stopping.is_set():
                stop(read_stop_reason(stop_path))
            if (not stopping.is_set() and roles == ["initialize"]
                    and envelope.get("check_controls", True) and initialization_stop_changed(envelope)):
                stop("MANUAL_STOP")
            if not stopping.is_set() and roles != ["initialize"] and envelope.get("check_controls", True):
                # Check without importing any application code. Child guards also
                # check controls immediately before submitting an order.
                if envelope["trader"] == "gameplan":
                    for relative in ("controls/stock-trader/operator-intent.txt", "controls/gameplan-stock-trader/operator-intent.txt"):
                        try:
                            text = (Path(envelope["datastore"]) / relative).read_text().strip()
                            if text.split("=", 1)[-1].strip() != "TRUE":
                                stop("MANUAL_STOP")
                        except OSError:
                            stop("MANUAL_STOP")
                else:
                    try:
                        status = json.loads((Path(envelope["datastore"]) / "intraday/manual-session.json").read_text())
                        if (not isinstance(status, dict) or status.get("session_id") != envelope["session"]["session_id"]
                                or status.get("status") != "RUNNING"):
                            stop("MANUAL_STOP")
                    except (OSError, ValueError):
                        stop("MANUAL_STOP")
            # Check for controller changes only when a new lane could be
            # admitted. Scientific work already running keeps its full unit;
            # execution continues while those lanes drain to durable cursors.
            if (relay_selector is not None and relay_target is None and not stopping.is_set()
                    and roles != ["initialize"]
                    and any(role not in done and role not in children for role in roles)):
                candidate = relay_selector()
                if candidate is not None and (candidate["host_sha256"] != HOST_SHA256 or candidate.get("runtime_changed")):
                    relay_target = candidate
                    phase("controller_draining", "controller")
            for role in roles:
                if stopping.is_set() or role in done or role in children:
                    continue
                if relay_target is not None:
                    if role != "execution" or final_execution_started:
                        continue
                    if not any(name != "execution" for name in children):
                        final_execution_started = True
                phase("before_start", role)
                if stop_path.exists() or stopping.is_set():
                    stop(read_stop_reason(stop_path))
                    break
                turn = uuid.uuid4().hex
                generation = generations.get(role, 0) + 1
                request = directory / f"{role}-{generation}-{turn}.json"
                response = request.with_suffix(".response.json")
                state = states.get(role, {})
                write_json(request, {"envelope": str(envelope_path), "token": envelope["token"],
                                     "role": role, "turn": turn, "generation": generation,
                                     "response": str(response), **state})
                argv = command(role, request)
                if stop_path.exists() or stopping.is_set():
                    stop(read_stop_reason(stop_path))
                    break
                child = subprocess.Popen(argv, close_fds=True)
                children[role] = (child, response, turn)
                generations[role] = generation
                phase("started", role)
            for role, (child, response, turn) in list(children.items()):
                code = child.poll()
                if code is None:
                    continue
                del children[role]
                phase("exited", role)
                if stopping.is_set() or stop_path.exists():
                    stop(read_stop_reason(stop_path))
                    continue
                try:
                    result = json.loads(response.read_text())
                    if (result["protocol"], result["token"], result["turn"], result["role"]) != (PROTOCOL, envelope["token"], turn, role):
                        raise ValueError("Handoff receipt identity mismatch")
                    if code == PLANNED_EXIT and result["disposition"] == "PLANNED_RENEWAL":
                        states[role] = {"state": result["state"], "next_poll_at": result["next_poll_at"]}
                        phase("handoff", role)
                    elif code == 0 and result["disposition"] == "COMPLETED":
                        done.add(role)
                        if role == "initialize":
                            if (stop_path.exists() or (envelope.get("check_controls", True)
                                                       and initialization_stop_changed(envelope))):
                                stop("MANUAL_STOP")
                                continue
                            envelope["session"] = result["result"]["session"]
                            envelope["roles"] = result["result"]["roles"]
                            write_json(envelope_path, envelope)
                            write_json(active_path, dict(active, session_id=envelope["session"]["session_id"]))
                            roles = list(envelope["roles"])
                            if envelope["trader"] == "intraday":
                                if not guarded_initial_control(envelope, "intraday/manual-session.json",
                                    json.dumps(intraday_status(envelope, "RUNNING"), sort_keys=True,
                                               allow_nan=False).encode("utf-8")):
                                    stop("MANUAL_STOP")
                        elif result.get("result", {}).get("status") in {"FAILED", "SESSION_FINISHED_WITH_ERRORS", "NOOP_STOCK_FORECASTS_NOT_QUALIFIED", "NOOP_ENRICHMENT_NOT_QUALIFIED"}:
                            stop("FAILURE")
                    else:
                        raise RuntimeError(f"Unacknowledged worker exit {code}")
                    if role == "execution" and not stopping.is_set():
                        # The manual-session document is also a stop input;
                        # writing RUNNING heartbeats there could overwrite a
                        # concurrent human stop. Liveness has its own record.
                        write_json(active_path, dict(active, session_id=envelope["session"]["session_id"],
                                   controller_pid=os.getpid(), heartbeat_at=datetime.now(timezone.utc).isoformat(),
                                   execution_generation=generations[role]))
                except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
                    print(f"{role}: {exc}; session will not restart.", flush=True)
                    stop("FAILURE")
            if stopping.is_set():
                deadline = time.monotonic() + 10
                while children and time.monotonic() < deadline:
                    for role, (child, _, _) in list(children.items()):
                        if child.poll() is not None:
                            del children[role]
                    if children:
                        time.sleep(0.05)
                # The anchor owns these exact process handles, never a PID lookup.
                # On Windows kernel job teardown also removes grandchildren.
                for child, _, _ in children.values():
                    child.terminate()
                for child, _, _ in children.values():
                    child.wait()
                break
            if all(role in done for role in roles):
                reason = "SESSION_COMPLETE"
                break
            if (relay_target is not None and not children
                    and ("execution" in done or "execution" not in roles or final_execution_started)):
                # Stop admitting all work before transferring scheduling. The
                # old process retains its exact kernel/lock ownership and only
                # waits for its successor. It can never schedule again.
                latest = relay_selector()
                if latest is None:
                    # A completed local rollback selected this controller again.
                    # Resume from the same checkpoints, never launch stale code.
                    relay_target = None
                    final_execution_started = False
                    continue
                relay_target = latest
                handoff = directory / ("controller-handoff-" + uuid.uuid4().hex + ".json")
                payload = {"protocol": PROTOCOL, "envelope": str(envelope_path),
                           "directory": str(directory), "token": envelope["token"],
                           "handoff_id": uuid.uuid4().hex, "parent_pid": os.getpid(),
                           "parent_birth": birth(os.getpid()), "host_sha256": relay_target["host_sha256"],
                           "states": states, "done": sorted(done), "generations": generations}
                write_json(handoff, payload)
                phase("controller_handoff", "controller")
                if stopping.is_set() or stop_path.exists():
                    stop(read_stop_reason(stop_path))
                    continue
                outcome = wait_for_controller_relay(handoff, payload, relay_target["command"],
                                                    stop=stop, stopping=stopping, phase=phase)
                reason = outcome["disposition"]
                generations, done = outcome["generations"], set(outcome["completed_roles"])
                break
            time.sleep(0.05)
    except BaseException:
        stop("FAILURE")
        for child, _, _ in children.values():
            if child.poll() is None:
                child.terminate()
                child.wait()
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        write_json(directory / "outcome.json", {"disposition": reason or "FAILURE", "generations": generations,
                   "session_id": envelope["session"]["session_id"], "completed_roles": sorted(done)})
        try:
            current = json.loads(active_path.read_text("utf-8"))
            if current.get("token") == envelope["token"]:
                write_json(active_path, dict(current, status="ENDED", disposition=reason or "FAILURE"))
        except (OSError, ValueError):
            pass
        if envelope["trader"] == "intraday" and envelope.get("check_controls", True):
            write_json(Path(envelope["datastore"]) / "intraday/manual-session.json",
                intraday_status(envelope, "FAILED" if reason == "FAILURE" else "STOPPED", done=done,
                                stop_reason=reason, broker_orders_automatically_cancelled=False))
    return 1 if reason == "FAILURE" else 0


def select_generation(root):
    """Fixed on-disk protocol reader, with no installed Python imports.

    Mutable source and policy never run in the anchor. The selected fresh
    controller verifies its whole source tree before importing the application.
    """
    import re
    root = Path(root).resolve()
    active = json.loads((root / "active.json").read_text())
    identity = active.get("generation", "")
    if (active.get("schema") != "trader-installation-v1" or active.get("protocol") != 1
            or not re.fullmatch("[0-9a-f]{64}", identity) or active.get("manifest_sha256") != identity):
        raise ValueError("Unsupported/incomplete trader installation")
    generation = root / "generations" / identity
    raw = (generation / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != identity:
        raise ValueError("Trader generation manifest changed")
    manifest = json.loads(raw)
    runtime_id = manifest["runtime"]["id"]
    if not re.fullmatch("[0-9a-f]{64}", runtime_id) or manifest["runtime"]["manifest_sha256"] != runtime_id:
        raise ValueError("Invalid runtime identity")
    runtime = root / "runtimes" / runtime_id
    raw_runtime = (runtime / "manifest.json").read_bytes()
    if hashlib.sha256(raw_runtime).hexdigest() != runtime_id:
        raise ValueError("Runtime manifest changed")
    runtime_manifest = json.loads(raw_runtime)
    relative = runtime_manifest["python_relative"]
    if relative != "python/python.exe":
        raise ValueError("Unsupported Windows interpreter layout")
    python = runtime / relative
    if hashlib.sha256(python.read_bytes()).hexdigest() != runtime_manifest["files"][relative]:
        raise ValueError("Installed interpreter changed")
    for name in ("tools/trader_worker.py", "tools/trader_installation.py", "tools/trader_session_host.py"):
        path = generation / "source" / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["source_files"][name]:
            raise ValueError("Generation bootstrap changed")
    return {"python": python, "generation": generation,
            "host_sha256": manifest["source_files"]["tools/trader_session_host.py"]}


def installed_commands(envelope):
    """Fresh controller policy; all cached verification dies with this host."""
    installation = Path(envelope["installation"])
    verified_generations = set()

    def selected_verified():
        selected = select_generation(installation)
        identity = selected["generation"].name
        if identity not in verified_generations:
            checked = subprocess.run([str(selected["python"]), "-I", "-S", "-B",
                str(selected["generation"] / "source/tools/trader_worker.py"),
                "--generation", str(selected["generation"]), "--verify-only"], check=False)
            if checked.returncode != 0:
                raise ValueError("Installed generation failed validation; session will not restart")
            verified_generations.add(identity)
        return selected

    def command(role, request):
        selected = selected_verified()
        return [str(selected["python"]), "-I", "-S", "-B",
                str(selected["generation"] / "source/tools/trader_worker.py"),
                "--generation", str(selected["generation"]), "--request", str(request)]

    def relay_selector():
        selected = select_generation(installation)
        runtime_changed = Path(sys.executable).resolve() != selected["python"].resolve()
        if selected["host_sha256"] == HOST_SHA256 and not runtime_changed:
            return None
        selected = selected_verified()
        return {"host_sha256": selected["host_sha256"],
                "runtime_changed": Path(sys.executable).resolve() != selected["python"].resolve(),
                "command": [str(selected["python"]), "-I", "-S", "-B",
                str(selected["generation"] / "source/tools/trader_session_host.py")]}

    return command, relay_selector


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trader", choices=("gameplan", "intraday"))
    parser.add_argument("--continue", dest="continuation", type=Path)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--datastore", type=Path, default=Path("C:/DATASTORE"))
    parser.add_argument("--installation", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--watchlist", type=Path)
    parser.add_argument("--publication-offset-seconds", type=float, default=30)
    parser.add_argument("--symbol-workers", type=int)
    parser.add_argument("--lookback-days", type=int)
    args = parser.parse_args(argv)
    if args.continuation is not None:
        if args.trader is not None:
            parser.error("Controller continuation cannot request a new trader")
        return resume_controller(args.continuation, commands=installed_commands)
    if args.trader is None:
        parser.error("A manual initial start requires --trader")
    repository = args.repository.resolve()
    installation = args.installation or repository / "scratch/trader-installation"
    bind_kernel_lifetime()
    with own_session(args.datastore, args.trader):
        session_id = "manual-" + args.trader + ":" + uuid.uuid4().hex
        directory = args.datastore / "state/trader-renewal" / args.trader / session_id.replace(":", "-")
        envelope = {"trader": args.trader, "repository": str(repository), "datastore": str(args.datastore.resolve()),
                    "installation": str(Path(installation).resolve()),
                    "profile": str(args.profile or repository / "scratch/cross-pc/local-profile.json"),
                    "watchlist": str(args.watchlist or repository / "datafetching/watchlist.local.txt"),
                    "options": {"publication_offset_seconds":args.publication_offset_seconds,
                                "symbol_workers":args.symbol_workers,"lookback_days":args.lookback_days},
                    "session": {"session_id":session_id}, "roles": []}
        command, relay_selector = installed_commands(envelope)
        print("Manual session started. Ctrl+C or closing this window ends the entire session. Each completed iteration loads a fresh installed worker.",flush=True)
        return run_protocol(directory=directory,envelope=envelope,command=command,relay_selector=relay_selector)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"TRADER SESSION NOT STARTED/FAILED: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
