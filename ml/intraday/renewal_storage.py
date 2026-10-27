"""Cold local storage continuation inside one already admitted worker.

This helper starts no process and knows no broker, trader, account or science.
The consumer supplies the original manual-stop and lifetime predicates.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import traceback


def _failure_evidence(exc):
    frames = [{"file": Path(frame.filename).name[-120:],
               "function": frame.name[:120], "line": frame.lineno}
              for frame in traceback.extract_tb(exc.__traceback__, limit=-8)]
    evidence = {"error_type": type(exc).__name__[:80], "frames": frames}
    code, name = getattr(exc, "sqlite_errorcode", None), getattr(exc, "sqlite_errorname", None)
    if (type(code) is int and type(name) is str and name.startswith("SQLITE_")
            and getattr(sqlite3, name, None) == code):
        evidence.update(sqlite_errorcode=code, sqlite_errorname=name)
    return evidence


def retry_renewal_storage_setup(action, *, role, stopped, permitted, pause):
    """Repeat exact cold setup only after native SQLite contention.

    Each action must own and close its own transactions. In particular, an
    interrupted schema setup is repeated on fresh connections, rather than on
    a stale recovery snapshot. Other errors keep their original failure path.
    This is continuation inside the same worker, never restart-on-error.
    """
    if role not in {"execution", "forecast", "scoring", "candles", "market_inputs"}:
        raise ValueError("Unknown admitted storage role")
    retries = 0
    while not stopped() and permitted():
        try:
            result = action()
            if stopped() or not permitted():
                raise InterruptedError("Manual session stopped or original lane cutoff reached during storage setup")
            return result
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", None)
            if type(code) is not int or code & 255 not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise
            retries += 1
            print(json.dumps({"event": "renewal_storage_setup_contention", "role": role,
                              "retry_count": retries, "diagnostics": _failure_evidence(exc)},
                             sort_keys=True, separators=(",", ":")), flush=True)
            pause(1)
    raise InterruptedError("Manual session stopped or original lane cutoff reached during storage setup")
