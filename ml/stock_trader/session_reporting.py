"""Compact console reports without changing the full persisted cycle record."""
from __future__ import annotations

from collections.abc import Mapping

from ml.stock_trader.contracts import utc


def _activity_message(report: Mapping[str, object]) -> tuple[str, str]:
    status = str(report.get("status") or "")
    if (report.get("error") or report.get("stopped_after_error")
            or report.get("session_consecutive_failures")
            or any(word in status for word in ("UNAVAILABLE", "FAILED", "ERROR", "REVIEW_REQUIRED", "CONFLICT"))
            or status.startswith("SUBMISSION_STOPPED")):
        return "ATTENTION", "Cycle or session requires attention; see status and error."
    if status == "ENTRY_CANCELLATION_AWAITING_RECONCILIATION":
        return "CANCELLATION PENDING", "Waiting for broker cancellation confirmation."
    if report.get("activation_active") is False or "TRADER_INACTIVE" in status:
        return "INACTIVE", "Trader is inactive; no new orders are requested."
    if report.get("execution_requested") is False or status.startswith("DRY_RUN"):
        return "DRY RUN", "Execution is disabled for this cycle."
    if status == "PREDICTION_TARGET_ELAPSED":
        message = "The saved target expired before submission; the expired instruction is not replayed."
        if report.get("orders_submitted_this_cycle", 0):
            return "ORDERS SUBMITTED", "Orders submitted; fills are not confirmed. " + message
        return "TARGET ELAPSED", message
    if report.get("orders_submitted_this_cycle", 0):
        return "ORDERS SUBMITTED", "Orders submitted; fills are not confirmed."
    if status == "NO_ORDERS_SUBMITTED_RETRY_EXHAUSTED":
        return "MONITORING", "Retry limit reached; unfilled remainder abandoned. Monitoring continues."
    if status in {"NO_ORDERS_SUBMITTED", "NO_ENTRY_SIGNAL", "NO_DIRECTIONAL_STOCK_ENTRY_SIGNAL",
                  "NO_QUALIFIED_INDEPENDENT_STOCK_ENTRIES"}:
        return "MONITORING", "No new orders this cycle; session continues monitoring."
    return "WAITING", "Waiting for the next eligible action."


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
    for field in ("error", "stopped_after_error", "submission_stop"):
        if field in cycle:
            report[field] = cycle[field]
    report["activity"], report["message"] = _activity_message(report)
    return report


def format_session_cycle_report(report: Mapping[str, object]) -> str:
    """Render one readable line while the complete cycle stays in saved state."""
    def single_line(value):
        return " ".join(str(value).split())

    activity = str(report.get("activity") or "WAITING")
    message = str(report.get("message") or "Waiting for the next eligible action.")
    if activity == "MONITORING":
        message += " Open orders may still fill."
    display_time = utc(report["timestamp_pacific"]).tz_convert("America/Los_Angeles").strftime("[%H:%M:%S %Z]")
    parts = [
        display_time,
        single_line(activity), single_line(message),
        f"submitted cycle={report.get('orders_submitted_this_cycle', 0)} session={report.get('session_orders_submitted', 0)}",
        f"broker={single_line(report.get('broker_capture_status') or 'NOT CHECKED')}",
    ]
    if activity in {"ATTENTION", "WAITING"}:
        parts.append(f"status={single_line(report.get('status') or 'UNKNOWN')}")
    if activity == "ATTENTION":
        parts.append(f"session={single_line(report.get('session_status') or 'UNKNOWN')}"
                     f" failures={report.get('session_consecutive_failures', 0)} consecutive/{report.get('session_failed_cycles', 0)} total")
    if report.get("error"):
        parts.append(f"error={single_line(report['error'])}")
    if report.get("stopped_after_error"):
        parts.append("stopped_after_error=True")
    return " | ".join(parts)


__all__ = ["session_cycle_report", "format_session_cycle_report"]
