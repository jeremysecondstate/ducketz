"""Best-effort terminal feedback for the Scout controller, never effect authority."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo


class ConsoleStatus:
    def __init__(self, stream=None, monotonic=None):
        self.stream = sys.stdout if stream is None else stream
        self.monotonic = time.monotonic if monotonic is None else monotonic
        self._last_key = None
        self._last_at = None
        self._generation = None
        self._phase = None

    def write(self, message):
        # Closing/redirecting a terminal must never change execution or cleanup.
        try:
            stamp = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%H:%M:%S %Z")
            print(f"[{stamp}] Scout 15m | {message}", file=self.stream, flush=True)
        except Exception:
            pass

    @staticmethod
    def local_time(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
                ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M:%S %Z")
        except Exception:
            return str(value)

    def update(self, state, root, phase=None):
        generation = state["generation"]
        if generation != self._generation:
            self._generation, self._phase = generation, None
        if phase is not None:
            self._phase = phase
        status = state["status"]
        worker = state.get("worker") or {}
        key = (status, generation, worker.get("pid"), self._phase, state.get("stop_reason"))
        now = self.monotonic()
        changed = key != self._last_key
        if not changed and self._last_at is not None and now - self._last_at < 30:
            return
        self._last_key, self._last_at = key, now
        detail = f"{status} | generation {generation}"
        if worker.get("pid"):
            detail += f" | worker PID {worker['pid']}"
        if self._phase:
            detail += f" | last worker phase: {self._phase}"
        if not changed:
            self.write("Heartbeat: controller active | " + detail)
            return
        descriptions = {
            "WAITING": "Waiting for the selected session start at " + self.local_time(state["wake_at"]),
            "STARTING": "Starting worker; waiting for readiness",
            "RUNNING": "Worker ready; trading details remain in the worker log",
            "BETWEEN_ITERATIONS": "Full iteration checkpoint saved; preparing the next worker",
            "STOPPING": "Stop requested; waiting for the owned worker tree to exit",
            "STOPPED": "Stop recorded; finishing controller cleanup",
            "FAILED": "Failure recorded; cleaning up without automatic retry",
        }
        self.write(detail + " | " + descriptions.get(status, status))
        if status in {"STOPPING", "STOPPED"}:
            self.write("Reason: " + str(state.get("stop_reason", "unknown")))
        if status == "STARTING" and not worker:
            self.write("Worker log: " + str(Path(root) / str(generation) / "worker.log"))

    def hold(self):
        # Opt-in from the manual launcher only. NUL/redirected task IO never waits.
        try:
            if sys.stdin is None or not sys.stdin.isatty() or not self.stream.isatty():
                return
            self.write("Run ended. Press Enter to close this window.")
            input()
        except (Exception, KeyboardInterrupt):
            pass
