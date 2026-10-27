"""Bounded in-memory observations of existing collector queue locks.

This module performs no I/O, acquires no extra lock and does not infer an
external holder. Diagnostic failure cannot replace native lock/body errors.
"""
from collections import deque
from contextlib import contextmanager
import itertools
import math
import os
import threading
import time


PHASES = frozenset({"initialize", "enqueue", "step_due", "task_recheck",
                    "orphan_commit", "source_block", "receipt_commit",
                    "capability_manifest"})


def _point(value):
    if type(value) not in {int, float}:
        return None
    try:
        return value if math.isfinite(value) else None
    except BaseException:
        return None


def _identity(value, minimum=0):
    return value if type(value) is int and minimum <= value < 2 ** 64 else None


def _elapsed(start, end):
    if _point(start) is None or _point(end) is None or end < start:
        return None
    return _point(end - start)


def _copy_event(row):
    copied = dict(row)
    if "failure_active_local" in copied:
        copied["failure_active_local"] = [dict(active) for active in row["failure_active_local"]]
    return copied


def _snapshot_header(process_id=None, collector_id=None):
    return {"schema_version": "intraday-market-queue-lock-timing-v1",
            "evidence_version": "failure-time-correlation-v1",
            "process_id": process_id, "collector_id": collector_id,
            "clock_scope": "Monotonic clock within this process lifetime only",
            "identity_scope": "Process and collector instance only; identities may be reused after exit",
            "holder_scope": "This collector instance only; external holder unknown"}


class QueueLockTiming:
    """Observe original lock contexts; bound events and active rows to 32 each."""
    def __init__(self, *, monotonic=time.monotonic, thread_cpu=time.thread_time):
        self._monotonic = monotonic
        self._thread_cpu = thread_cpu
        self._ids = itertools.count()
        self._events = deque(maxlen=32)
        self._active = {}
        self._observer_error = False
        self._process_id = self._collector_id = None
        try:
            self._process_id = _identity(os.getpid(), 1)
            self._collector_id = _identity(id(self), 1)
            if self._process_id is None or self._collector_id is None:
                self._observer_error = True
        except BaseException:
            self._observer_error = True

    def _stamp(self):
        try:
            monotonic, cpu = _point(self._monotonic()), _point(self._thread_cpu())
            if monotonic is None or cpu is None:
                self._observer_error = True
            return monotonic, cpu
        except BaseException:
            self._observer_error = True
            return None, None

    def _begin(self, phase, stamp):
        try:
            token = _identity(next(self._ids))
            thread_id = _identity(threading.get_ident(), 1)
            if token is None or thread_id is None:
                self._observer_error = True
            if token is not None and len(self._active) < 32:
                self._active[token] = (phase, "waiting", stamp[0], None, thread_id)
            return token, thread_id
        except BaseException:
            self._observer_error = True
            return None, None

    def _acquired(self, token, phase, stamp):
        try:
            if token in self._active:
                previous = self._active[token]
                self._active[token] = (phase, "holding", previous[2], stamp[0], previous[4])
        except BaseException:
            self._observer_error = True

    def _active_rows(self, now):
        return [{"event_id": token, "thread_id": thread_id,
                 "phase": phase, "state": state,
                 "started_monotonic": started, "acquired_monotonic": acquired,
                 "elapsed_seconds": _elapsed(acquired if state == "holding" else started, now)}
                for token, (phase, state, started, acquired, thread_id)
                in list(self._active.items())[:32]]

    def _finish(self, token, thread_id, phase, outcome, started, acquired, ended):
        # Capture before removing the failed contender and before its caller can
        # drain a task pool. A waiting contender is never labeled a holder.
        failure_active = []
        if outcome == "acquire_error":
            try:
                failure_active = self._active_rows(ended[0])
            except BaseException:
                self._observer_error = True
        try:
            self._active.pop(token, None)
        except BaseException:
            self._observer_error = True
        try:
            row = {"event_id": token, "thread_id": thread_id,
                   "phase": phase, "outcome": outcome,
                   "started_monotonic": started[0],
                   "acquired_monotonic": acquired[0] if acquired else None,
                   "ended_monotonic": ended[0],
                   "wait_seconds": _elapsed(started[0], acquired[0] if acquired else ended[0]),
                   "hold_seconds": _elapsed(acquired[0], ended[0]) if acquired else None,
                   "thread_cpu_seconds": _elapsed(started[1], ended[1])}
            if outcome == "acquire_error":
                row.update(failure_capture_stage="native_acquisition_error_before_pool_drain",
                           failure_snapshot_monotonic=ended[0], failure_active_local=failure_active)
            self._events.append(row)
        except BaseException:
            self._observer_error = True

    @contextmanager
    def observe(self, lock, phase):
        # The phase is a fixed source label. Never retain paths, keys or errors.
        phase = phase if type(phase) is str and phase in PHASES else "other"
        started = self._stamp()
        token, thread_id = self._begin(phase, started)
        acquired = None
        outcome = "acquire_error"
        try:
            with lock as value:
                acquired = self._stamp()
                self._acquired(token, phase, acquired)
                try:
                    yield value
                except BaseException:
                    outcome = "body_error"
                    raise
                else:
                    outcome = "completed"
        except BaseException:
            if outcome == "completed":
                outcome = "release_error"
            raise
        finally:
            self._finish(token, thread_id, phase, outcome, started, acquired, self._stamp())

    def snapshot(self):
        try:
            now = self._stamp()[0]
            return {**_snapshot_header(self._process_id, self._collector_id),
                "recent": [_copy_event(row) for row in tuple(self._events)],
                "active_local": self._active_rows(now),
                "observer_error": self._observer_error}
        except BaseException:
            self._observer_error = True
            return {**_snapshot_header(self._process_id, self._collector_id),
                    "recent": [], "active_local": [], "observer_error": True}


def queue_lock_timing_snapshot(collector):
    """Return safe bounded observations even if a collector lacks the observer."""
    try:
        return collector.queue_lock_timing.snapshot()
    except BaseException:
        return {**_snapshot_header(), "recent": [], "active_local": [], "observer_error": True}


def observed_lock_acquire_trace(trace, lock):
    """Recognize only the exact contextlib/observer/native-enter chain.

    A body that later acquires another lock must not become a queue-acquire
    failure merely because an arbitrary deeper frame is a native lock enter.
    """
    try:
        from contextlib import _GeneratorContextManager
        expected = (_GeneratorContextManager.__enter__.__code__,
                    QueueLockTiming.observe.__wrapped__.__code__,
                    lock.__enter__.__func__.__code__)
        for code in expected:
            if trace is None or trace.tb_frame.f_code is not code:
                return False
            trace = trace.tb_next
        return True
    except BaseException:
        return False
