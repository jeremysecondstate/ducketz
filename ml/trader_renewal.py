"""Private continuation protocol for an already manually started trader.

This module uses only the standard library. A saved envelope is not authority:
the kernel owner must still be the same living process, and stops are latched.
"""
from __future__ import annotations

import json
import hashlib
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PLANNED_EXIT = 75
PROTOCOL = "manual-trader-renewal-v1"


class PlannedRenewal(BaseException):
    """A committed, nonterminal handoff; never handled as a trading error."""


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def process_birth(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
        k.OpenProcess.restype = w.HANDLE
        k.GetProcessTimes.argtypes = (w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4))
        k.GetProcessTimes.restype = w.BOOL
        k.CloseHandle.argtypes = (w.HANDLE,)
        h = k.OpenProcess(0x1000, False, int(pid))
        if not h:
            return None
        try:
            values = [w.FILETIME() for _ in range(4)]
            if not k.GetProcessTimes(h, *(ctypes.byref(v) for v in values)):
                return None
            k.GetExitCodeProcess.argtypes = (w.HANDLE, ctypes.POINTER(w.DWORD))
            code = w.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:
                return None
            return str((values[0].dwHighDateTime << 32) | values[0].dwLowDateTime)
        finally:
            k.CloseHandle(h)
    try:
        # Linux test hosts: proc start ticks distinguish PID reuse.
        return Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return None


GAMEPLAN_CONTROLS = {
    "controls/stock-trader/operator-intent.txt": "CONFIRM_ACTIVE_TRADING",
    "controls/gameplan-stock-trader/operator-intent.txt": "CONFIRM_GAMEPLAN_STOCK_TRADING",
}


def control_signatures(envelope):
    """Identify one exact control revision, including a same-value stop write."""
    root = Path(envelope["datastore"])
    paths = GAMEPLAN_CONTROLS if envelope["trader"] == "gameplan" else {"intraday/manual-session.json": None}
    result = {}
    for relative, key in paths.items():
        try:
            with (root / relative).open("rb") as stream:
                raw, info = stream.read(), os.fstat(stream.fileno())
            if key is not None:
                rows = [line.strip() for line in raw.decode("utf-8").splitlines() if line.strip()]
                parts = rows[0].split("=", 1) if len(rows) == 1 else []
                active = len(parts) == 2 and [part.strip() for part in parts] == [key, "TRUE"]
            else:
                value = json.loads(raw)
                active = isinstance(value, dict) and value.get("status") == "RUNNING"
            result[relative] = {"sha256": hashlib.sha256(raw).hexdigest(), "mtime_ns": info.st_mtime_ns,
                                "file_id": info.st_ino, "active": active}
        except FileNotFoundError:
            result[relative] = {"missing": True, "active": False}
        except (OSError, ValueError, UnicodeError):
            result[relative] = {"unreadable": True, "active": False}
    return result


def initialization_stop_changed(envelope):
    original = envelope.get("initial_controls", {})
    return any(not current["active"] and current != original.get(path)
               for path, current in control_signatures(envelope).items())


def _latch_envelope_stop(envelope):
    atomic_json(envelope["stop_path"], {"reason": "MANUAL_STOP", "token": envelope["token"],
                "at": datetime.now(timezone.utc).isoformat()})


def _owner_alive(envelope):
    pid, recorded = envelope.get("owner_pid"), envelope.get("owner_birth")
    return (type(pid) is int and pid > 0 and isinstance(recorded, str) and bool(recorded)
            and process_birth(pid) == recorded)


def latch_manual_stop(datastore, trader="gameplan"):
    """Latch an existing session before an official manual control turns off.

    A stale pointer or PID reuse never gives a prior session authority. This
    writes no activation and cannot start a process or clear a stop record.
    """
    root = Path(datastore).resolve()
    base = root / "state/trader-renewal" / trader
    try:
        active = json.loads((base / "active.json").read_text("utf-8"))
        if (active.get("protocol") != PROTOCOL or active.get("status") != "RUNNING"
                or active.get("trader") != trader
                or not _owner_alive(active)):
            return False
        stop = Path(active["stop_path"]).resolve()
        if stop.name != "stop.json" or stop.parent.parent != base or not active.get("token"):
            return False
        _latch_envelope_stop(active)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        # The persistent FALSE write remains the normal stop even if a stale
        # or unreadable optional renewal pointer cannot be used.
        return False


def manual_controls_active(envelope):
    root = Path(envelope["datastore"])
    if envelope["trader"] == "gameplan":
        for relative, key in GAMEPLAN_CONTROLS.items():
            try:
                rows = [line.strip() for line in (root / relative).read_text().splitlines() if line.strip()]
                parts = rows[0].split("=", 1) if len(rows) == 1 else []
                if len(parts) != 2 or [part.strip() for part in parts] != [key, "TRUE"]:
                    return False
            except OSError:
                return False
    elif envelope["trader"] == "intraday":
        try:
            status = json.loads((root / "intraday/manual-session.json").read_text())
            if (not isinstance(status, dict) or status.get("session_id") != envelope["session"]["session_id"]
                    or status.get("status") != "RUNNING"):
                return False
        except (OSError, ValueError):
            return False
    return True


class Renewal:
    def __init__(self, request_path):
        self.request_path = Path(request_path)
        self.request = json.loads(self.request_path.read_text(encoding="utf-8"))
        self.envelope_path = Path(self.request["envelope"])
        self.envelope = json.loads(self.envelope_path.read_text(encoding="utf-8"))
        if self.envelope["protocol"] != PROTOCOL or self.request["token"] != self.envelope["token"]:
            raise RuntimeError("Invalid session continuation")
        self.session = self.envelope["session"]
        self.session_id = self.session["session_id"]
        self.generation = self.request["generation"]
        self.state = self.request.get("state", {})
        self.next_poll_at = self.request.get("next_poll_at")

    def claim(self):
        """Consume a dispatch once, including after a crash or normal exit."""
        marker = self.request_path.with_suffix(".claim.json")
        value = {"turn": self.request["turn"], "pid": os.getpid(),
                 "birth": process_birth(os.getpid()), "token": self.request["token"]}
        if value["birth"] is None:
            raise RuntimeError("Cannot identify worker lifetime")
        # Never reclaim this marker: failure needs a new explicit session, or a
        # new acknowledged handoff. An old request cannot grant another worker.
        with marker.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())

    def require_initial_session_lock(self, datastore):
        """Verify the initializer inherits this exact controller's outer lock."""
        root = Path(datastore).resolve()
        if (self.request.get("role") != "initialize" or self.envelope.get("trader") != "gameplan"
                or root != Path(self.envelope["datastore"]).resolve() or self.stopped()):
            raise RuntimeError("Manual initializer session authority is unavailable")
        marker = json.loads(self.request_path.with_suffix(".claim.json").read_text("utf-8"))
        if (marker.get("pid"), marker.get("birth"), marker.get("turn"), marker.get("token")) != (
                os.getpid(), process_birth(os.getpid()), self.request["turn"], self.request["token"]):
            raise RuntimeError("Manual initializer dispatch is not owned by this worker")
        active = json.loads((root / "state/trader-renewal/gameplan/active.json").read_text("utf-8"))
        if (active.get("status") != "RUNNING" or any(active.get(key) != self.envelope.get(key)
                for key in ("protocol", "token", "owner_pid", "owner_birth", "stop_path"))):
            raise RuntimeError("Manual initializer controller identity changed")
        raw = (root / "locks/independent-stock-session.lock").read_text("utf-8")
        pairs = [line.split("=", 1) for line in raw.splitlines()]
        if any(len(pair) != 2 for pair in pairs) or len(dict(pairs)) != len(pairs):
            raise RuntimeError("Manual initializer outer lock is malformed")
        owner = dict(pairs)
        if (owner.get("process") != "independent-stock-session"
                or owner.get("pid") != str(self.envelope["owner_pid"]) or not owner.get("token")
                or not _owner_alive(self.envelope)):
            raise RuntimeError("Manual initializer does not inherit the controller's session lock")
        return True

    def stopped(self):
        if Path(self.envelope["stop_path"]).exists():
            return True
        if not _owner_alive(self.envelope):
            return True
        marker = self.request_path.with_suffix(".claim.json")
        if marker.exists():
            try:
                claim = json.loads(marker.read_text("utf-8"))
                if (claim.get("pid") != os.getpid() or claim.get("birth") != process_birth(os.getpid())
                        or claim.get("turn") != self.request["turn"] or claim.get("token") != self.request["token"]):
                    return True
            except (OSError, ValueError):
                return True
        if not self.envelope.get("check_controls", True):
            return False
        if self.request.get("role") == "initialize":
            changed = initialization_stop_changed(self.envelope)
        else:
            changed = not manual_controls_active(self.envelope)
        if changed:
            _latch_envelope_stop(self.envelope)
        return changed

    def wait_due(self, *, clock=lambda: datetime.now(timezone.utc), sleep=time.sleep):
        if not self.next_poll_at:
            return
        due = datetime.fromisoformat(str(self.next_poll_at).replace("Z", "+00:00"))
        while not self.stopped():
            now = clock()
            if isinstance(now, str):
                now = datetime.fromisoformat(now.replace("Z", "+00:00"))
            seconds = (due - now).total_seconds()
            if seconds <= 0:
                return
            sleep(min(seconds, 0.1))

    def checkpoint(self, state, next_poll_at):
        if self.stopped():
            raise InterruptedError("Manual session authority ended before handoff")
        value = {"protocol": PROTOCOL, "token": self.request["token"],
                 "turn": self.request["turn"], "role": self.request["role"],
                 "disposition": "PLANNED_RENEWAL", "state": state,
                 "next_poll_at": str(next_poll_at) if next_poll_at is not None else None,
                 "worker_pid": os.getpid()}
        atomic_json(self.request["response"], value)
        raise PlannedRenewal()

    def finish(self, result):
        atomic_json(self.request["response"], {
            "protocol": PROTOCOL, "token": self.request["token"], "turn": self.request["turn"],
            "role": self.request["role"], "disposition": "COMPLETED", "result": result,
            "worker_pid": os.getpid()})


def managed_execution_allowed():
    """Called by the existing final Gameplan safety check before submission."""
    request = os.environ.get("DUCKETZ_TRADER_RENEWAL_REQUEST")
    if not request:
        return True  # Existing manually started legacy worker behavior.
    try:
        return not Renewal(request).stopped()
    except (OSError, ValueError, KeyError, TypeError):
        return False
