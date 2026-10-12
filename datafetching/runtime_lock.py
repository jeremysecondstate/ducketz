from __future__ import annotations

import errno
import math
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from filelock import FileLock


_MAINTENANCE_LOCK_NAME = ".ducketz-runtime-lock-maintenance.lock"


@contextmanager
def interprocess_file_lock(
    path: Path,
    *,
    poll_seconds: float = 0.25,
    reporter: Callable[[str], None] | None = None,
    waiting_message: str = "Waiting for the other datastore operation to finish.",
) -> Iterator[None]:
    """Hold the first byte of a persistent file as an OS-released mutex."""

    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = target.open("a+b")
    if target.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    waiting_reported = False
    acquired = False
    try:
        while not _try_os_file_lock(handle):
            if reporter is not None and not waiting_reported:
                reporter(waiting_message)
                waiting_reported = True
            time.sleep(float(poll_seconds))
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _unlock_os_file(handle)
        finally:
            handle.close()


@contextmanager
def runtime_lock_maintenance_gate(
    lock_parent: Path,
    *,
    timeout: float = -1,
) -> Iterator[None]:
    """Serialize the short create/replace/remove phases of runtime lock handling."""

    parent = Path(lock_parent)
    parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(parent / _MAINTENANCE_LOCK_NAME), timeout=timeout):
        yield


@contextmanager
def exclusive_runtime_lock(path: Path, *, process_name: str) -> Iterator[None]:
    """Hold an inter-process ownership lock for one artifact-writing runtime."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    owned_payload: bytes | None = None
    owner_created_at = _process_created_at(os.getpid())
    payload = (
        f"process={process_name}\n"
        f"pid={os.getpid()}\n"
        f"owner_created_at={owner_created_at if owner_created_at is not None else 'unknown'}\n"
        f"started_at={datetime.now(timezone.utc).isoformat()}\n"
        f"token={secrets.token_hex(16)}\n"
    ).encode("utf-8")
    with runtime_lock_maintenance_gate(target.parent):
        for attempt in range(2):
            try:
                _publish_lock(target, payload)
                owned_payload = payload
                break
            except FileExistsError as exc:
                original = target.read_bytes() if target.is_file() else b""
                detail = original.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
                owner = _lock_pid(detail)
                if attempt == 0 and owner is not None and _original_owner_exited(owner, detail):
                    stale = target.with_name(
                        f"{target.name}.stale-{owner}-{os.getpid()}"
                    )
                    try:
                        if target.read_bytes() != original:
                            raise RuntimeError("Runtime lock changed during owner verification; preserve the new owner")
                        target.replace(stale)
                    except FileNotFoundError:
                        continue
                    else:
                        stale.unlink(missing_ok=True)
                        continue
                raise RuntimeError(
                    f"Another {process_name} owns these artifacts. Lock: {target}\n{detail}"
                ) from exc
        else:  # pragma: no cover - the loop either acquires or raises
            raise AssertionError("Runtime lock acquisition exited unexpectedly")

    try:
        yield
    finally:
        with runtime_lock_maintenance_gate(target.parent):
            if owned_payload is not None:
                try:
                    unchanged = target.read_bytes() == owned_payload
                except FileNotFoundError:
                    unchanged = False
                if unchanged:
                    target.unlink()


def _publish_lock(target: Path, payload: bytes) -> None:
    """Publish complete fsynced bytes atomically, never overwrite another lock."""
    temporary = target.with_name(target.name + ".pending-" + secrets.token_hex(16))
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # Both paths share a directory/filesystem. A crash before link leaves
        # only an inert temporary; after link the PID/birth/token are complete.
        os.link(temporary, target)
    except FileExistsError:
        raise
    except BaseException:
        # An operation may succeed before its caller observes an I/O error.
        # Remove only this exact unique token, never a concurrent replacement.
        try:
            if target.read_bytes() == payload:
                target.unlink()
        except FileNotFoundError:
            pass
        raise
    finally:
        temporary.unlink(missing_ok=True)


def _lock_pid(payload: str) -> int | None:
    matches = re.findall(r"(?m)^pid=(.*)$", payload)
    if len(matches) != 1 or re.fullmatch(r"\d+", matches[0]) is None:
        return None
    owner = int(matches[0])
    return owner if owner > 0 else None


def _process_created_at(pid: int) -> float | None:
    """Read process birth without signals; unknown evidence never proves reuse."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        created = psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None
    return created if type(created) in (int, float) and math.isfinite(created) and created > 0 else None


def _original_owner_exited(pid: int, payload: str) -> bool:
    if not _pid_is_running(pid):
        return True
    current_birth = _process_created_at(pid)
    if current_birth is None:
        return False
    recorded = re.findall(r"(?m)^owner_created_at=(.*)$", payload)
    if recorded:
        if len(recorded) != 1:
            return False
        try:
            original_birth = float(recorded[0])
        except ValueError:
            return False
        return math.isfinite(original_birth) and original_birth > 0 and current_birth != original_birth
    # Legacy locks have no process birth. A process born strictly after the
    # recorded lock creation cannot be its original owner. Require an explicit
    # zoned timestamp; malformed, future, or inaccessible evidence stays locked.
    timestamps = re.findall(r"(?m)^started_at=(.*)$", payload)
    if len(timestamps) != 1:
        return False
    try:
        started = datetime.fromisoformat(timestamps[0].replace("Z", "+00:00"))
        if started.tzinfo is None or started.utcoffset() is None:
            return False
        original_start = started.timestamp()
    except (ValueError, OverflowError, OSError):
        return False
    return math.isfinite(original_start) and original_start > 0 and current_birth > original_start


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        )
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        )
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            # ERROR_INVALID_PARAMETER is the documented result for a PID that
            # does not exist. Access-denied and other query failures are not
            # proof of death, so preserve the lock.
            return ctypes.get_last_error() != 87
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return int(exit_code.value) == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _try_os_file_lock(handle: object) -> bool:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN} or getattr(
                exc, "winerror", None
            ) in {32, 33}:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_os_file(handle: object) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "exclusive_runtime_lock",
    "interprocess_file_lock",
    "runtime_lock_maintenance_gate",
]
