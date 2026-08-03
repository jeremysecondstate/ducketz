from __future__ import annotations

import errno
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence


LOOP_A_CYCLE_SCHEMA_VERSION = "loop-a-cycle-v1"
LOOP_A_CYCLE_FILENAME = ".ducketz-loop-a-cycle.json"
LOOP_A_CYCLE_LOCK_FILENAME = ".ducketz-loop-a-cycle.lock"
_VALID_STATUSES = {"WRITING", "COMPLETE", "FAILED"}


class LoopACycleError(RuntimeError):
    """Loop A has not published a complete datastore cycle."""


@dataclass(frozen=True)
class LoopACycle:
    generation: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    symbols: tuple[str, ...]
    providers: tuple[str, ...]
    failure_count: int | None = None

    def __post_init__(self) -> None:
        if self.status not in _VALID_STATUSES:
            raise ValueError(f"Unsupported Loop A cycle status: {self.status}")
        if not self.generation.strip():
            raise ValueError("Loop A cycle generation is required")
        if not self.symbols:
            raise ValueError("Loop A cycle requires at least one symbol")
        if self.status == "WRITING":
            if self.finished_at is not None or self.failure_count is not None:
                raise ValueError("WRITING cycle cannot have terminal metadata")
        else:
            if self.finished_at is None or self.failure_count is None:
                raise ValueError("Terminal Loop A cycle requires terminal metadata")
            if self.finished_at < self.started_at:
                raise ValueError("Loop A cycle cannot finish before it starts")
            if self.failure_count < 0:
                raise ValueError("Loop A failure count cannot be negative")
            if self.status == "COMPLETE" and self.failure_count != 0:
                raise ValueError("COMPLETE Loop A cycle cannot contain failures")
            if self.status == "FAILED" and self.failure_count == 0:
                raise ValueError("FAILED Loop A cycle requires at least one failure")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": LOOP_A_CYCLE_SCHEMA_VERSION,
            "generation": self.generation,
            "status": self.status,
            "started_at": _isoformat(self.started_at),
            "finished_at": (
                _isoformat(self.finished_at)
                if self.finished_at is not None
                else None
            ),
            "symbols": list(self.symbols),
            "providers": list(self.providers),
            "failure_count": self.failure_count,
        }


def begin_loop_a_cycle(
    datastore_root: Path,
    *,
    symbols: Sequence[str],
    providers: Sequence[str],
    now: object | None = None,
) -> LoopACycle:
    started_at = _utc_datetime(
        now if now is not None else datetime.now(timezone.utc)
    )
    cycle = LoopACycle(
        generation=(
            f"{started_at.strftime('%Y%m%dT%H%M%S.%fZ')}-pid{os.getpid()}"
        ),
        status="WRITING",
        started_at=started_at,
        finished_at=None,
        symbols=_normalize_symbols(symbols),
        providers=_normalize_providers(providers),
    )
    _write_cycle(datastore_root, cycle)
    return cycle


def finish_loop_a_cycle(
    datastore_root: Path,
    cycle: LoopACycle,
    *,
    failure_count: int,
    now: object | None = None,
) -> LoopACycle:
    failures = int(failure_count)
    if failures < 0:
        raise ValueError("failure_count cannot be negative")
    current = read_loop_a_cycle(datastore_root)
    if (
        current is None
        or current.generation != cycle.generation
        or current.status != "WRITING"
    ):
        raise LoopACycleError(
            "Loop A cycle changed before its terminal state could be published"
        )
    terminal = replace(
        current,
        status="COMPLETE" if failures == 0 else "FAILED",
        finished_at=_utc_datetime(
            now if now is not None else datetime.now(timezone.utc)
        ),
        failure_count=failures,
    )
    _write_cycle(datastore_root, terminal)
    return terminal


def require_complete_loop_a_cycle(datastore_root: Path) -> LoopACycle:
    cycle = read_loop_a_cycle(datastore_root)
    if cycle is None:
        raise LoopACycleError(
            "Loop A has not completed a datastore cycle yet"
        )
    if cycle.status != "COMPLETE":
        raise LoopACycleError(
            f"Latest Loop A datastore cycle is {cycle.status}, not COMPLETE"
        )
    return cycle


def read_loop_a_cycle(datastore_root: Path) -> LoopACycle | None:
    path = Path(datastore_root) / LOOP_A_CYCLE_FILENAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("Loop A cycle payload must be an object")
        if payload.get("schema_version") != LOOP_A_CYCLE_SCHEMA_VERSION:
            raise ValueError("Unsupported Loop A cycle schema")
        finished_at = payload.get("finished_at")
        failure_count = payload.get("failure_count")
        return LoopACycle(
            generation=str(payload["generation"]),
            status=str(payload["status"]),
            started_at=_utc_datetime(payload["started_at"]),
            finished_at=(
                _utc_datetime(finished_at) if finished_at is not None else None
            ),
            symbols=_normalize_symbols(payload.get("symbols", ())),
            providers=_normalize_providers(payload.get("providers", ())),
            failure_count=(
                int(failure_count) if failure_count is not None else None
            ),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise LoopACycleError(f"Loop A cycle state is unreadable: {path}") from exc


@contextmanager
def datastore_cycle_lock(
    datastore_root: Path,
    *,
    poll_seconds: float = 0.25,
    reporter: Callable[[str], None] | None = None,
) -> Iterator[None]:
    """Serialize Loop A mutations with complete Loop B reads.

    This is an operating-system file lock, so the lock is released if a process
    exits unexpectedly. The small lock file itself intentionally persists.
    """

    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    path = Path(datastore_root) / LOOP_A_CYCLE_LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    if path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()
    waiting_reported = False
    acquired = False
    try:
        while not _try_lock(handle):
            if reporter is not None and not waiting_reported:
                reporter("Waiting for the other datastore loop to finish its cycle.")
                waiting_reported = True
            time.sleep(float(poll_seconds))
        acquired = True
        yield
    finally:
        try:
            if acquired:
                _unlock(handle)
        finally:
            handle.close()


def _try_lock(handle: object) -> bool:
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


def _unlock(handle: object) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_cycle(datastore_root: Path, cycle: LoopACycle) -> None:
    path = Path(datastore_root) / LOOP_A_CYCLE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(cycle.as_dict(), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _normalize_symbols(values: Sequence[object]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("Loop A cycle symbols must be an array")
    symbols = tuple(
        dict.fromkeys(
            str(value).strip().upper()
            for value in values
            if str(value).strip()
        )
    )
    if not symbols:
        raise ValueError("At least one Loop A cycle symbol is required")
    return symbols


def _normalize_providers(values: Sequence[object]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("Loop A cycle providers must be an array")
    return tuple(
        dict.fromkeys(
            str(value).strip().lower()
            for value in values
            if str(value).strip()
        )
    )


def _utc_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        timestamp = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        timestamp = datetime.fromisoformat(text)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _isoformat(value: datetime) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")
