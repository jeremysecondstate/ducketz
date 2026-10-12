"""Compact console reports without changing the full persisted cycle record."""
from __future__ import annotations

from collections.abc import Mapping

from ml.stock_trader.contracts import utc


def session_cycle_report(
    cycle: Mapping[str, object], *, as_of: object,
    session_orders_submitted: int, session_cycles: int,
    session_failed_cycles: int, session_consecutive_failures: int,
) -> dict[str, object]:
    """Keep cycle outcomes distinct from cumulative session activity and health."""
    capture = cycle.get("broker_state_capture") or {}
    report = {
        "timestamp_pacific": utc(as_of).tz_convert("America/Los_Angeles").isoformat(),
        "status": cycle.get("status"),
        "orders_submitted_this_cycle": cycle.get("submitted_orders", 0),
        "session_orders_submitted": session_orders_submitted,
        "session_cycles": session_cycles,
        "session_failed_cycles": session_failed_cycles,
        "session_consecutive_failures": session_consecutive_failures,
        "session_status": "DEGRADED" if session_consecutive_failures else "RUNNING",
        "broker_capture_status": capture.get("status"),
        "activation_active": cycle.get("activation_active"),
        "execution_requested": cycle.get("execution_requested"),
        "run_directory": cycle.get("run_directory"),
    }
    for field in ("error", "stopped_after_error"):
        if field in cycle:
            report[field] = cycle[field]
    if (report["status"] == "NO_ORDERS_SUBMITTED"
            and report["orders_submitted_this_cycle"] == 0
            and not session_consecutive_failures
            and not cycle.get("error") and not cycle.get("stopped_after_error")):
        report["message"] = "No new orders this cycle; session continues monitoring."
    return report


__all__ = ["session_cycle_report"]
