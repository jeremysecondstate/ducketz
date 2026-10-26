"""Bounded in-memory observations of existing collector queue locks.

This module performs no I/O, acquires no extra lock and does not infer an
external holder. Diagnostic failure cannot replace native lock/body errors.
"""
from collections import deque
from contextlib import contextmanager
import itertools
import math
import time


PHASES = frozenset({"initialize", "enqueue", "step_due", "task_recheck",
                    "orphan_commit", "source_block", "receipt_commit",
                    "capability_manifest"})


def _elapsed(start, end):
    if (type(start) not in {int, float} or type(end) not in {int, float}
            or not math.isfinite(start) or not math.isfinite(end) or end < start):
        return None
    return end - start


class QueueLockTiming:
    """Observe the original lock context; retain at most 32 finished/active rows."""
    def __init__(self, *, monotonic=time.monotonic, thread_cpu=time.thread_time):
        self._monotonic = monotonic
        self._thread_cpu = thread_cpu
        self._ids = itertools.count()
        self._events = deque(maxlen=32)
        self._active = {}
        self._observer_error = False

    def _stamp(self):
        try:
            return self._monotonic(), self._thread_cpu()
        except BaseException:
            self._observer_error = True
            return None, None

    def _begin(self, phase, stamp):
        try:
            token = next(self._ids)
            if len(self._active) < 32:
                self._active[token] = (phase, "waiting", stamp[0])
            return token
        except BaseException:
            self._observer_error = True
            return None

    def _acquired(self, token, phase, stamp):
        try:
            if token in self._active:
                self._active[token] = (phase, "holding", stamp[0])
        except BaseException:
            self._observer_error = True

    def _finish(self, token, phase, outcome, started, acquired, ended):
        try:
            self._active.pop(token, None)
            self._events.append({"phase": phase, "outcome": outcome,
                "wait_seconds": _elapsed(started[0], acquired[0] if acquired else ended[0]),
                "hold_seconds": _elapsed(acquired[0], ended[0]) if acquired else None,
                "thread_cpu_seconds": _elapsed(started[1], ended[1])})
        except BaseException:
            self._observer_error = True

    @contextmanager
    def observe(self, lock, phase):
        # The phase is a fixed source label. Never retain paths, keys or errors.
        phase = phase if type(phase) is str and phase in PHASES else "other"
        started = self._stamp()
        token = self._begin(phase, started)
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
            self._finish(token, phase, outcome, started, acquired, self._stamp())

    def snapshot(self):
        try:
            now = self._stamp()[0]
            return {"schema_version": "intraday-market-queue-lock-timing-v1",
                "recent": [dict(row) for row in tuple(self._events)],
                "active_local": [{"phase": phase, "state": state,
                    "elapsed_seconds": _elapsed(started, now)}
                    for phase, state, started in list(self._active.values())],
                "observer_error": self._observer_error,
                "holder_scope": "This collector instance only; external holder unknown"}
        except BaseException:
            self._observer_error = True
            return {"schema_version": "intraday-market-queue-lock-timing-v1",
                    "recent": [], "active_local": [], "observer_error": True,
                    "holder_scope": "This collector instance only; external holder unknown"}


def queue_lock_timing_snapshot(collector):
    """Return safe bounded observations even if a collector lacks the observer."""
    try:
        return collector.queue_lock_timing.snapshot()
    except BaseException:
        return {"schema_version": "intraday-market-queue-lock-timing-v1",
                "recent": [], "active_local": [], "observer_error": True,
                "holder_scope": "This collector instance only; external holder unknown"}


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
