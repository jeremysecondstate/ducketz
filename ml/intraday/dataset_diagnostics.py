"""Per-build timing evidence, kept outside immutable dataset/model inputs.

Captures own their mutable state. Context tokens isolate concurrent threads and
nested builds; no collector is installed outside an explicit capture. Observer
failures never replace the dataset operation's result or original exception.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import math
import re
import sys
import threading
import time


_current = ContextVar("intraday_dataset_diagnostics", default=None)
_recovery_journal = ContextVar("intraday_recovery_diagnostic_journal", default=None)
_MAX_PHASES = 32
_MAX_CACHES = 4
_MAX_OBSERVER_ERRORS = 8
_MAX_RECOVERY_GROUPS = 16
_MAX_RECOVERY_SAMPLES = 3
_CACHE_COUNTERS = ("hits", "misses", "invalid_entries", "read_skips", "write_skips")


def _error_type(error):
    return re.sub(r"[^A-Za-z0-9_]", "_", type(error).__name__)[:96]


def _observer_error(profile, error):
    try:
        errors = profile["observer_errors"]
        if len(errors) < _MAX_OBSERVER_ERRORS:
            errors.append(_error_type(error))
    except BaseException:
        pass


def _clocks():
    return time.perf_counter(), time.thread_time(), time.process_time()


def worker_identity():
    """Bounded local identity for correlating native read-only CPU samples."""
    native_id = threading.get_native_id()
    if type(native_id) is not int or native_id < 1:
        raise ValueError("Invalid diagnostic native thread identity")
    return {"thread_name": re.sub(r"[^A-Za-z0-9_.-]", "_",
                                threading.current_thread().name)[:96],
            "native_thread_id": native_id}


def worker_threads(registry):
    """Project only the host's four known worker identities; no worker control."""
    result = []
    pending_error = sys.exception()
    for role, name, thread in registry[:4]:
        row = {"role": role, "thread_name": name}
        try:
            native_id = getattr(thread, "native_id", None)
            if native_id is not None:
                if type(native_id) is not int or native_id < 1:
                    raise ValueError("Invalid diagnostic native thread identity")
                row["native_thread_id"] = native_id
        except BaseException as error:
            if not isinstance(error, Exception) and pending_error is None:
                raise
            row["observer_error"] = _error_type(error)
        result.append(row)
    return result


@contextmanager
def capture_dataset_build():
    """Yield one bounded diagnostic record and restore the caller's capture."""
    profile = {"schema_version": "intraday-dataset-build-profile-v1",
               "cpu_scope": "current_thread", "process_cpu_scope": "all_process_threads",
               "status": "running",
               "phases": [], "context_caches": [], "observer_errors": [],
               "recovery_subphases": {"totals_are_inclusive": True,
                                      "groups": [], "overflow_count": 0}}
    token = _current.set(profile)
    journal_token = _recovery_journal.set(None)
    try:
        try:
            profile["worker"] = worker_identity()
        except Exception as error:
            _observer_error(profile, error)
        yield profile
    except BaseException as error:
        profile.update(status="error", error_type=_error_type(error))
        raise
    else:
        profile["status"] = "success"
    finally:
        _recovery_journal.reset(journal_token)
        _current.reset(token)


@contextmanager
def dataset_phase(name):
    """Record inclusive wall, own-thread CPU and all-process CPU, including failures."""
    profile = _current.get()
    started = None
    if profile is not None:
        try:
            if not isinstance(name, str) or not re.fullmatch(r"[a-z_]{1,64}", name):
                raise ValueError("Invalid dataset diagnostic phase name")
            started = _clocks()
        except Exception as error:
            _observer_error(profile, error)
    error_type = None
    try:
        yield
    except BaseException as error:
        error_type = _error_type(error)
        raise
    finally:
        if started is not None:
            try:
                finished = _clocks()
                phase = {"phase": name, "status": "error" if error_type else "success",
                         "wall_seconds": max(0.0, finished[0] - started[0]),
                         "thread_cpu_seconds": max(0.0, finished[1] - started[1]),
                         "process_cpu_seconds": max(0.0, finished[2] - started[2])}
                if error_type is not None:
                    phase["error_type"] = error_type
                if len(profile["phases"]) < _MAX_PHASES:
                    profile["phases"].append(phase)
            except BaseException as error:
                if not isinstance(error, Exception) and error_type is None:
                    raise
                _observer_error(profile, error)


def _finish_recovery(profile, name, journal_id, started, error_type):
    finished = _clocks()
    if (len(started) != 3 or len(finished) != 3
            or any(not math.isfinite(value) for value in (*started, *finished))):
        raise ValueError("Invalid recovery diagnostic clocks")
    elapsed = [max(0.0, end - start) for start, end in zip(started, finished)]
    summary = profile["recovery_subphases"]
    groups = summary["groups"]
    group = next((row for row in groups if row["group"] == name), None)
    if group is None:
        if len(groups) >= _MAX_RECOVERY_GROUPS:
            summary["overflow_count"] += 1
            return
        group = {"group": name, "count": 0, "error_count": 0,
                 "wall_seconds": 0.0, "thread_cpu_seconds": 0.0,
                 "process_cpu_seconds": 0.0, "maximum_wall_seconds": 0.0,
                 "slow_samples": []}
        groups.append(group)
    origin = profile["recovery_start_monotonic_for_offsets_only"]
    sample = {"original_journal_id": journal_id,
              "status": "error" if error_type else "success",
              "wall_seconds": elapsed[0], "thread_cpu_seconds": elapsed[1],
              "process_cpu_seconds": elapsed[2],
              "start_offset_seconds": max(0.0, started[0] - origin),
              "end_offset_seconds": max(0.0, finished[0] - origin)}
    if error_type:
        sample["error_type"] = error_type
    group["count"] += 1
    group["error_count"] += int(error_type is not None)
    for field, value in zip(("wall_seconds", "thread_cpu_seconds", "process_cpu_seconds"), elapsed):
        group[field] += value
    group["maximum_wall_seconds"] = max(group["maximum_wall_seconds"], elapsed[0])
    group["slow_samples"].append(sample)
    group["slow_samples"].sort(key=lambda row: row["wall_seconds"], reverse=True)
    del group["slow_samples"][_MAX_RECOVERY_SAMPLES:]


@contextmanager
def recovery_phase(name, journal_id=None, *, journal=None):
    """Bounded inclusive recovery timing inside an existing explicit capture.

    Expected original journal identity flows into nested constructor/audit
    phases. No paths, account values, requests, tokens or exception text enter
    this diagnostic record. Nested totals must not be added together.
    """
    profile = _current.get()
    started = journal_token = None
    error_type = None
    # A close/finally phase may begin while its caller is already unwinding.
    # Observer cancellation must not replace that original business exception.
    pending_error = sys.exception()
    if profile is not None:
        try:
            if not isinstance(name, str) or not re.fullmatch(r"[a-z_]{1,64}", name):
                raise ValueError("Invalid recovery diagnostic group")
            if journal is not None:
                if journal_id is not None:
                    raise ValueError("Ambiguous diagnostic journal identity")
                journal_id = getattr(journal, "journal_id", None)
            if journal_id is None:
                journal_id = _recovery_journal.get()
            if journal_id is not None and (type(journal_id) is not str
                    or re.fullmatch(r"[a-f0-9]{32}", journal_id) is None):
                raise ValueError("Invalid diagnostic original journal identity")
            started = _clocks()
            if len(started) != 3 or any(not math.isfinite(value) for value in started):
                raise ValueError("Invalid recovery diagnostic clocks")
            profile.setdefault("recovery_start_monotonic_for_offsets_only", started[0])
            journal_token = _recovery_journal.set(journal_id)
        except BaseException as error:
            if not isinstance(error, Exception) and pending_error is None:
                raise
            started = None
            _observer_error(profile, error)
    try:
        yield
    except BaseException as error:
        error_type = _error_type(error)
        raise
    finally:
        try:
            if started is not None:
                try:
                    _finish_recovery(profile, name, journal_id, started, error_type)
                except BaseException as error:
                    if (not isinstance(error, Exception) and error_type is None
                            and pending_error is None):
                        raise
                    _observer_error(profile, error)
        finally:
            if journal_token is not None:
                _recovery_journal.reset(journal_token)


def record_context_cache(cache):
    """Copy counters after cache exit, so optional flush skips are included."""
    profile = _current.get()
    if profile is None:
        return
    pending_error = sys.exception()
    try:
        counters = {"enabled": cache is not None}
        if cache is not None:
            for name in _CACHE_COUNTERS:
                value = getattr(cache, name)
                if type(value) is not int or value < 0:
                    raise ValueError("Invalid context cache diagnostic counter")
                counters[name] = value
        if len(profile["context_caches"]) < _MAX_CACHES:
            profile["context_caches"].append(counters)
    except BaseException as error:
        if not isinstance(error, Exception) and pending_error is None:
            raise
        _observer_error(profile, error)
