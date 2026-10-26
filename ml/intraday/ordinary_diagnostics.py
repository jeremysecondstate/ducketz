"""Call-local clocks for ordinary expired-intention chronology, without IO.

Elapsed time includes descheduling and waits; low thread CPU cannot distinguish
IO, lock contention and GIL waits. Nested phase totals overlap. These clocks
attribute an observed operation and never supply an execution prerequisite.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import sys
import time


_CURRENT = ContextVar("ordinary_intention_diagnostics", default=None)
_PHASES = frozenset({
    "chronology_total", "newer_forecast_scan", "native_filter", "processed_scan",
    "processed_id_read", "processed_header_read", "fence_renewal", "record_total",
    "record_identity", "record_cache_lock", "record_cache_unlock",
    "preflight_connection_open", "preflight_connection_close", "preflight_begin",
    "preflight_marker", "preflight_payload_read", "preflight_payload_validate",
    "preflight_caller_compare", "writer_connection_open", "writer_commit_close",
    "writer_lock", "writer_forecast_recheck", "writer_marker", "writer_payload_read",
    "writer_payload_validate", "writer_caller_compare", "sizing_read", "projection_append",
})
_COUNTS = frozenset({"native_rows", "newer_forecasts", "processed_forecasts",
                     "forecast_payload_reads", "forecast_text_characters", "forecast_blob_bytes"})


class OrdinaryTimings:
    def __init__(self):
        self.phases, self.workload = {}, {}
        self.elapsed_unavailable = self.thread_cpu_unavailable = False

    def _clock(self, name):
        try:
            value = getattr(time, name)()
            if type(value) in (int, float) and math.isfinite(value):
                return value
        except Exception:
            pass
        setattr(self, "thread_cpu_unavailable" if name == "thread_time" else "elapsed_unavailable", True)
        return None

    @contextmanager
    def phase(self, name):
        wall, cpu = self._clock("monotonic"), self._clock("thread_time")
        try:
            yield
        finally:
            end, cpu_end = self._clock("monotonic"), self._clock("thread_time")
            try:
                if name not in _PHASES:
                    self.elapsed_unavailable = True
                else:
                    for start, stop, prefix in ((wall, end, ""), (cpu, cpu_end, "thread_cpu_")):
                        if start is None or stop is None or stop < start or not math.isfinite(stop-start):
                            setattr(self, "thread_cpu_unavailable" if prefix else "elapsed_unavailable", True)
                            continue
                        row = self.phases.setdefault(name, {})
                        count, total, maximum = prefix+"count", prefix+"total_seconds", prefix+"maximum_seconds"
                        elapsed = stop-start
                        summed = row.get(total, 0.)+elapsed
                        if not math.isfinite(summed):
                            self.elapsed_unavailable = True
                            continue
                        row[count] = row.get(count, 0)+1
                        row[total] = summed
                        row[maximum] = max(row.get(maximum, 0.), elapsed)
            except Exception:
                self.elapsed_unavailable = True

    def snapshot(self):
        return dict(schema_version="intraday-ordinary-intention-details-v1",
            phases={name: {key: round(value, 6) if type(value) is float else value
                           for key, value in row.items()} for name, row in self.phases.items()},
            workload=dict(self.workload), elapsed_unavailable=self.elapsed_unavailable,
            thread_cpu_unavailable=self.thread_cpu_unavailable, totals_are_inclusive=True)


@contextmanager
def capture(timings):
    token = _CURRENT.set(timings)
    try:
        yield
    finally:
        _CURRENT.reset(token)


@contextmanager
def phase(name):
    current = _CURRENT.get()
    if current is None:
        yield
    else:
        with current.phase(name):
            yield


def count(name, amount):
    current = _CURRENT.get()
    if current is not None:
        try:
            if name not in _COUNTS or type(amount) is not int or amount < 0:
                current.elapsed_unavailable = True
            else:
                current.workload[name] = current.workload.get(name, 0)+amount
        except Exception:
            current.elapsed_unavailable = True


def payload(raw):
    count("forecast_payload_reads", 1)
    if isinstance(raw, str):
        count("forecast_text_characters", len(raw))
    elif isinstance(raw, bytes):
        count("forecast_blob_bytes", len(raw))


@contextmanager
def context(manager, entering, leaving):
    """Measure context entry/exit separately, preserving exception suppression."""
    with phase(entering):
        value = manager.__enter__()
    try:
        yield value
    except BaseException:
        with phase(leaving):
            suppressed = manager.__exit__(*sys.exc_info())
        if not suppressed:
            raise
    else:
        with phase(leaving):
            manager.__exit__(None, None, None)
