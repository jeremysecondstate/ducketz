from __future__ import annotations

import ctypes
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from filelock import FileLock


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Operator contract: the UI, PyCharm workflows, and production runtimes share
# this project-local cache. ``data/`` is gitignored; do not relocate or reject
# this path without an explicit operator request.
DEFAULT_TOKEN_CACHE_PATH = PROJECT_ROOT / "data" / "schwab_tokens.json"
TOKEN_CACHE_PATH = DEFAULT_TOKEN_CACHE_PATH
ACCESS_TOKEN_EXPIRY_SAFETY_SECONDS = 60
TOKEN_CACHE_LOCK_TIMEOUT_SECONDS = 30.0
OAUTH_REAUTHORIZATION_ERROR_CODES = frozenset(
    {"invalid_client", "invalid_grant", "http_400", "http_401"}
)
_TOKEN_CACHE_THREAD_LOCK = threading.RLock()
_TOKEN_CACHE_LOCK_STATE = threading.local()


@contextmanager
def locked_token_cache() -> Iterator[None]:
    """Serialize a complete token refresh transaction across sessions/processes."""

    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache_path = TOKEN_CACHE_PATH.resolve()
    lock_path = TOKEN_CACHE_PATH.with_name(f"{TOKEN_CACHE_PATH.name}.lock")
    with _TOKEN_CACHE_THREAD_LOCK:
        depth = int(getattr(_TOKEN_CACHE_LOCK_STATE, "depth", 0))
        active_path = getattr(_TOKEN_CACHE_LOCK_STATE, "cache_path", None)
        if depth:
            if active_path != cache_path:
                raise RuntimeError("Nested Schwab token-cache locks used different paths")
            _TOKEN_CACHE_LOCK_STATE.depth = depth + 1
            try:
                yield
            finally:
                _TOKEN_CACHE_LOCK_STATE.depth = depth
            return

        with _interprocess_token_lock(
            lock_path,
            timeout=TOKEN_CACHE_LOCK_TIMEOUT_SECONDS,
        ):
            _TOKEN_CACHE_LOCK_STATE.depth = 1
            _TOKEN_CACHE_LOCK_STATE.cache_path = cache_path
            try:
                _remove_stale_token_temp_files_unlocked()
                if TOKEN_CACHE_PATH.exists():
                    _protect_token_path(TOKEN_CACHE_PATH)
                yield
            finally:
                _TOKEN_CACHE_LOCK_STATE.depth = 0
                _TOKEN_CACHE_LOCK_STATE.cache_path = None


@contextmanager
def _interprocess_token_lock(path: Path, *, timeout: float) -> Iterator[None]:
    """Hold one stable OS lock for the entire credential transaction."""

    # filelock 3.30+ can retain the native lock file after release, so every
    # process contends on one stable pathname/inode instead of racing through
    # unlink-and-recreate cycles. Never fall back to an existence-only lock.
    with FileLock(
        str(path),
        timeout=timeout,
        preserve_lock_file=True,
        fallback_to_soft=False,
    ):
        _protect_token_path(path)
        yield

def load_token_payload(*, strict: bool = False) -> dict[str, Any] | None:
    with locked_token_cache():
        return _load_token_payload_unlocked(strict=strict)


def _load_token_payload_unlocked(*, strict: bool = False) -> dict[str, Any] | None:
    """Read the cache while the caller owns ``locked_token_cache``."""

    if not TOKEN_CACHE_PATH.exists():
        return None

    try:
        with TOKEN_CACHE_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        if strict:
            raise
        return None

    if isinstance(payload, dict):
        return payload
    if strict:
        raise ValueError("Schwab token cache must contain a JSON object")
    return None


def save_token_payload(
    payload: dict[str, Any],
    previous_refresh_token: str | None = None,
    *,
    previous_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("Schwab token response did not include an access token")
    expires_in = _int_value(payload.get("expires_in"), default=1800)
    access_token_expires_at = now + timedelta(
        seconds=max(expires_in - ACCESS_TOKEN_EXPIRY_SAFETY_SECONDS, 1)
    )

    cached_payload = {
        "access_token": access_token,
        "refresh_token": str(
            payload.get("refresh_token") or previous_refresh_token or ""
        ).strip(),
        "token_type": payload.get("token_type"),
        "scope": payload.get("scope"),
        "access_token_expires_at": access_token_expires_at.isoformat(),
        "saved_at": now.isoformat(),
    }

    refresh_token_expires_in = _optional_int_value(
        payload.get("refresh_token_expires_in")
    )
    if refresh_token_expires_in is not None:
        cached_payload["refresh_token_expires_at"] = (
            now + timedelta(seconds=refresh_token_expires_in)
        ).isoformat()
    elif previous_payload and previous_payload.get("refresh_token_expires_at"):
        cached_payload["refresh_token_expires_at"] = previous_payload[
            "refresh_token_expires_at"
        ]

    try:
        write_token_payload_atomic(cached_payload)
    except Exception as exc:
        # The OAuth request may already have succeeded. Keep the exact token
        # generation available to the caller so it can persist a fail-closed
        # uncertainty marker without reverting to rotated credentials.
        setattr(exc, "schwab_candidate_token_payload", cached_payload)
        raise

    return cached_payload


def write_refresh_attempt(
    cached_payload: dict[str, Any],
    *,
    attempt_id: str | None = None,
    status: str,
    error_type: str | None = None,
    oauth_error_code: str | None = None,
) -> dict[str, Any]:
    """Persist non-secret refresh single-flight state while the cache lock is held."""

    identifier = str(attempt_id or uuid4())
    normalized_oauth_error = str(oauth_error_code or "").strip().lower()
    if (
        normalized_oauth_error
        and normalized_oauth_error not in OAUTH_REAUTHORIZATION_ERROR_CODES
    ):
        raise ValueError("Unsupported Schwab OAuth error classification")
    marked = dict(cached_payload)
    marked["refresh_attempt"] = {
        "id": identifier,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": str(status),
        "error_type": str(error_type) if error_type else None,
    }
    if normalized_oauth_error:
        marked["refresh_attempt"]["oauth_error_code"] = normalized_oauth_error
    write_token_payload_atomic(marked)
    return marked


def has_uncertain_refresh_attempt(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False
    attempt = payload.get("refresh_attempt")
    return bool(
        isinstance(attempt, dict)
        and str(attempt.get("status") or "").upper()
        in {"IN_PROGRESS", "FAILED_UNCERTAIN"}
    )


def refresh_reauthorization_error_code(
    payload: dict[str, Any] | None,
) -> str | None:
    """Return the allowlisted reason Schwab rejected this cached credential.

    A deterministic OAuth rejection cannot be repaired by repeating the same
    refresh token for every symbol or process.  Persisting this state turns a
    fan-out of identical token requests into one actionable operator event;
    a successful authorization-code exchange writes a new payload and clears
    the marker.
    """

    if not payload:
        return None
    attempt = payload.get("refresh_attempt")
    if not isinstance(attempt, dict):
        return None
    if str(attempt.get("status") or "").upper() != "FAILED_REAUTH_REQUIRED":
        return None
    error_code = str(attempt.get("oauth_error_code") or "").strip().lower()
    return error_code if error_code in OAUTH_REAUTHORIZATION_ERROR_CODES else None


def refresh_reauthorization_required(payload: dict[str, Any] | None) -> bool:
    return refresh_reauthorization_error_code(payload) is not None


def write_token_payload_atomic(payload: dict[str, Any]) -> None:
    """Atomically replace the credential cache without exposing partial JSON."""

    with locked_token_cache():
        _write_token_payload_atomic_unlocked(payload)


def _write_token_payload_atomic_unlocked(payload: dict[str, Any]) -> None:
    """Write and durably replace the cache while its transaction lock is held."""

    TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = TOKEN_CACHE_PATH.with_name(
        f".{TOKEN_CACHE_PATH.name}.{uuid4().hex}.tmp"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        _protect_token_path(temporary)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = None
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_file_durably(temporary, TOKEN_CACHE_PATH)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _remove_stale_token_temp_files_unlocked() -> None:
    """Remove credential-bearing temp files left by a terminated writer."""

    prefix = f".{TOKEN_CACHE_PATH.name}."
    for candidate in TOKEN_CACHE_PATH.parent.iterdir():
        if (
            candidate.is_file()
            and candidate.name.startswith(prefix)
            and candidate.name.endswith(".tmp")
        ):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


def _replace_file_durably(source: Path, destination: Path) -> None:
    """Replace a file and wait for the directory entry to reach stable storage."""

    if os.name == "nt":
        move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file_ex.restype = ctypes.c_int
        movefile_replace_existing = 0x1
        movefile_write_through = 0x8
        if not move_file_ex(
            str(source),
            str(destination),
            movefile_replace_existing | movefile_write_through,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return

    os.replace(source, destination)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_descriptor = os.open(destination.parent, directory_flags)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _protect_token_path(path: Path, *, directory: bool = False) -> None:
    """Restrict a credential file or directory to its owner."""

    if os.name != "nt":
        os.chmod(path, 0o700 if directory else 0o600)
        return

    security_descriptor = ctypes.c_void_p()
    descriptor_size = ctypes.c_ulong()
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert_descriptor = (
        advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    )
    convert_descriptor.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_ulong),
    ]
    convert_descriptor.restype = ctypes.c_int
    set_file_security = advapi32.SetFileSecurityW
    set_file_security.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    set_file_security.restype = ctypes.c_int
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    # OW is Windows' OWNER RIGHTS SID. P disables inherited ACEs.
    dacl = "D:P(A;OICI;FA;;;OW)" if directory else "D:P(A;;FA;;;OW)"
    if not convert_descriptor(
        dacl,
        1,
        ctypes.byref(security_descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        dacl_security_information = 0x00000004
        protected_dacl_security_information = 0x80000000
        if not set_file_security(
            str(path),
            dacl_security_information | protected_dacl_security_information,
            security_descriptor,
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        local_free(security_descriptor)


def access_token_is_fresh(payload: dict[str, Any] | None) -> bool:
    if not payload:
        return False

    access_token = payload.get("access_token")
    expires_at = _parse_datetime(payload.get("access_token_expires_at"))

    return bool(access_token and expires_at and expires_at > datetime.now(timezone.utc))


def refresh_token_is_available(payload: dict[str, Any] | None) -> bool:
    if not payload or not payload.get("refresh_token"):
        return False

    expires_at = _parse_datetime(payload.get("refresh_token_expires_at"))
    return expires_at is None or expires_at > datetime.now(timezone.utc)


def cached_access_token_expires_at(payload: dict[str, Any] | None) -> datetime | None:
    if not payload:
        return None

    return _parse_datetime(payload.get("access_token_expires_at"))


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc)


def _int_value(value: Any, default: int) -> int:
    parsed = _optional_int_value(value)
    return parsed if parsed is not None else default


def _optional_int_value(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
