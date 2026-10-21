"""Per-build timing evidence, kept outside immutable dataset/model inputs.

Captures own their mutable state. Context tokens isolate concurrent threads and
nested builds; no collector is installed outside an explicit capture. Observer
failures never replace the dataset operation's result or original exception.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import re
import sys
import time


_current = ContextVar("intraday_dataset_diagnostics", default=None)
_MAX_PHASES = 32
_MAX_CACHES = 4
_MAX_OBSERVER_ERRORS = 8
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
    return time.perf_counter(), time.thread_time()


@contextmanager
def capture_dataset_build():
    """Yield one bounded diagnostic record and restore the caller's capture."""
    profile = {"schema_version": "intraday-dataset-build-profile-v1",
               "cpu_scope": "current_thread", "status": "running",
               "phases": [], "context_caches": [], "observer_errors": []}
    token = _current.set(profile)
    try:
        yield profile
    except BaseException as error:
        profile.update(status="error", error_type=_error_type(error))
        raise
    else:
        profile["status"] = "success"
    finally:
        _current.reset(token)


@contextmanager
def dataset_phase(name):
    """Record inclusive wall/current-thread CPU time, including failed phases."""
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
                         "thread_cpu_seconds": max(0.0, finished[1] - started[1])}
                if error_type is not None:
                    phase["error_type"] = error_type
                if len(profile["phases"]) < _MAX_PHASES:
                    profile["phases"].append(phase)
            except BaseException as error:
                if not isinstance(error, Exception) and error_type is None:
                    raise
                _observer_error(profile, error)


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
