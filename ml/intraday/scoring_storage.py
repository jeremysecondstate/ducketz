"""Exact scoring-storage continuation; no market-data or execution dependencies."""
from collections import deque
import json
import sqlite3


def scoring_failure_evidence(exc):
    """Bound code locations and native SQLite codes without private values."""
    chain, seen, current = [], set(), exc
    while current is not None and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        frames = deque(maxlen=8)
        trace = current.__traceback__
        while trace is not None:
            code = trace.tb_frame.f_code
            frames.append({"file": code.co_filename.replace("\\", "/").rsplit("/", 1)[-1][:120],
                           "function": code.co_name[:120], "line": trace.tb_lineno})
            trace = trace.tb_next
        item = {"error_type": type(current).__name__[:80], "frames": list(frames)}
        code, name = getattr(current, "sqlite_errorcode", None), getattr(current, "sqlite_errorname", None)
        if (isinstance(current, sqlite3.Error) and type(code) is int and type(name) is str
                and name.startswith("SQLITE_") and getattr(sqlite3, name, None) == code):
            item.update(sqlite_errorcode=code, sqlite_errorname=name)
        chain.append(item)
        current = (current.__cause__ if current.__cause__ is not None else
                   None if current.__suppress_context__ else current.__context__)
    return {"exception_chain": chain, "root_cause": chain[-1],
            "exception_chain_truncated": current is not None}


def _print_evidence(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")), flush=True)


def retry_scoring_storage(action, *, store, session_id, operation, stopped, pause):
    """Retry native contention in one storage action using fresh transactions.

    Callers fully consume storage iterables inside the action. Provider requests
    and model work must stay outside this boundary. A successful action is never
    repeated merely because its recovery diagnostic could not be persisted.
    """
    retries, failure = 0, None
    while not stopped():
        try:
            result = action()
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            if type(code) is not int or code & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            retries += 1
            failure = scoring_failure_evidence(exc)
            _print_evidence({"event": "scoring_storage_contention", "operation": operation,
                             "retry_count": retries, "diagnostics": failure})
            pause(1)
            continue
        if retries:
            try:
                store.event(session_id, "scoring_storage_retry_completed",
                    {"operation": operation, "retry_count": retries,
                     "last_failure": failure, "operation_returned_normally": True})
            except Exception as exc:
                _print_evidence({"event": "scoring_storage_retry_diagnostic_unavailable",
                                 "operation": operation, "diagnostics": scoring_failure_evidence(exc)})
        return result
    raise InterruptedError("Manual session stopped; scoring storage continuation retained")


def record_scoring_worker_failure(exc, *, store, session_id, worker_errors, stopped):
    """Latch the actual failure even when its diagnostic database is unavailable."""
    diagnostics = scoring_failure_evidence(exc)
    failure = {"error_type": type(exc).__name__, "message": str(exc), "diagnostics": diagnostics}
    worker_errors.append({"worker": "scoring", **failure})
    stopped.set()
    try:
        store.event(session_id, "scoring_worker_stopped", failure)
    except Exception as diagnostic_exc:
        _print_evidence({"event": "scoring_worker_stopped", "diagnostics": diagnostics,
                         "diagnostic_failure": scoring_failure_evidence(diagnostic_exc)})
