from __future__ import annotations

import os
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from filelock import FileLock


_MAINTENANCE_LOCK_NAME = ".ducketz-runtime-lock-maintenance.lock"


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
    descriptor: int | None = None
    owned_payload: bytes | None = None
    with runtime_lock_maintenance_gate(target.parent):
        for attempt in range(2):
            try:
                descriptor = os.open(
                    target,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                )
                payload = (
                    f"process={process_name}\n"
                    f"pid={os.getpid()}\n"
                    f"started_at={datetime.now(timezone.utc).isoformat()}\n"
                    f"token={secrets.token_hex(16)}\n"
                ).encode("utf-8")
                os.write(descriptor, payload)
                os.close(descriptor)
                descriptor = None
                owned_payload = payload
                break
            except FileExistsError as exc:
                detail = (
                    target.read_text(encoding="utf-8", errors="replace")
                    if target.is_file()
                    else ""
                )
                owner = _lock_pid(detail)
                if attempt == 0 and owner is not None and not _pid_is_running(owner):
                    stale = target.with_name(
                        f"{target.name}.stale-{owner}-{os.getpid()}"
                    )
                    try:
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
        if descriptor is not None:
            os.close(descriptor)
        with runtime_lock_maintenance_gate(target.parent):
            if owned_payload is not None:
                try:
                    unchanged = target.read_bytes() == owned_payload
                except FileNotFoundError:
                    unchanged = False
                if unchanged:
                    target.unlink()


def _lock_pid(payload: str) -> int | None:
    match = re.search(r"(?m)^pid=(\d+)$", payload)
    if match is None:
        return None
    owner = int(match.group(1))
    return owner if owner > 0 else None


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


__all__ = ["exclusive_runtime_lock", "runtime_lock_maintenance_gate"]
