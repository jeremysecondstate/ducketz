from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.stock_trader import independent_session as worker
from ml.stock_trader.session_reporting import format_session_cycle_report, session_cycle_report


def _cycle(**overrides):
    return {
        "status": "NO_ORDERS_SUBMITTED", "submitted_orders": 0,
        "activation_active": True, "execution_requested": True,
        "error": None, "stopped_after_error": False,
        "run_directory": "synthetic/stock-trader-decision-runs/cycle-13",
        "selected_orders": 0, "duplicate_suppressions": 0,
        "broker_state_capture": {
            "status": "CURRENT", "attempts": 1, "elapsed_seconds": 4.2,
            "quote_refresh": {"status": "NOT_NEEDED", "refreshes": 0},
        },
        **overrides,
    }


def _report(cycle, *, failures=0, consecutive=0, as_of="2026-10-12T11:06:00Z"):
    return session_cycle_report(
        cycle, as_of=as_of, session_orders_submitted=12,
        session_cycles=13, session_failed_cycles=failures,
        session_consecutive_failures=consecutive,
    )


def test_no_new_orders_preserves_prior_session_submissions_and_full_cycle():
    cycle = _cycle()
    original = deepcopy(cycle)
    report = _report(cycle)

    assert json.loads(json.dumps(report)) == report
    assert report["status"] == "NO_ORDERS_SUBMITTED"
    assert report["orders_submitted_this_cycle"] == 0
    assert report["session_orders_submitted"] == 12
    assert report["session_cycles"] == 13
    assert report["session_failed_cycles"] == report["session_consecutive_failures"] == 0
    assert report["session_status"] == "RUNNING"
    assert report["timestamp_pacific"] == "2026-10-12T04:06:00-07:00"
    assert report["broker_capture_status"] == "CURRENT"
    assert report["activation_active"] is True
    assert report["execution_requested"] is True
    assert report["run_directory"] == cycle["run_directory"]
    assert report["message"] == "No new orders this cycle; session continues monitoring."
    assert "broker_state_capture" not in report
    assert cycle == original


@pytest.mark.parametrize("failure", [
    {"status": "HORIZON_BROKER_RECONCILIATION_UNAVAILABLE", "error": "ReadTimeout",
     "broker_state_capture": {"status": "UNAVAILABLE"}},
    {"status": "SUBMISSION_STOPPED_SAFETY_CHECK", "stopped_after_error": True},
    {"status": "NO_ORDERS_SUBMITTED", "error": "ReadTimeout", "stopped_after_error": True},
])
def test_actual_errors_remain_visible_without_reassuring_monitoring_message(failure):
    cycle = _cycle(**failure)
    report = _report(cycle, failures=1, consecutive=1)

    assert report["status"] == cycle["status"]
    assert report["error"] == cycle["error"]
    assert report["stopped_after_error"] == cycle["stopped_after_error"]
    assert report["broker_capture_status"] == cycle["broker_state_capture"]["status"]
    assert report["session_status"] == "DEGRADED"
    assert report["session_failed_cycles"] == report["session_consecutive_failures"] == 1
    assert report["session_orders_submitted"] == 12
    line = format_session_cycle_report(report)
    assert report["activity"] == "ATTENTION"
    assert cycle["status"] in line
    assert "monitoring" not in line.lower()
    if cycle["error"]:
        assert cycle["error"] in line


def test_no_capture_cycle_does_not_claim_session_recovery():
    report = _report(_cycle(status="NO_DIRECTIONAL_STOCK_ENTRY_SIGNAL", broker_state_capture=None),
                     failures=1, consecutive=1)
    assert report["status"] == "NO_DIRECTIONAL_STOCK_ENTRY_SIGNAL"
    assert report["broker_capture_status"] is None
    assert report["session_status"] == "DEGRADED"
    assert report["session_consecutive_failures"] == 1
    line = format_session_cycle_report(report)
    assert report["activity"] == "ATTENTION"
    assert "NO_DIRECTIONAL_STOCK_ENTRY_SIGNAL" in line
    assert "monitoring" not in line.lower()


def test_pacific_timestamp_tracks_standard_time_and_omits_absent_error_fields():
    report = _report({"status": "NO_ENTRY_SIGNAL"}, as_of="2026-12-01T12:06:00Z")
    assert report["timestamp_pacific"] == "2026-12-01T04:06:00-08:00"
    assert "error" not in report
    assert "stopped_after_error" not in report


def test_normal_idle_line_distinguishes_no_new_submissions_from_working_orders():
    report = _report(_cycle())
    original = deepcopy(report)
    line = format_session_cycle_report(report)
    assert "No new orders this cycle" in line
    assert "monitoring" in line.lower()
    assert "Open orders may still fill" in line
    assert "submitted cycle=0 session=12" in line
    assert "broker=CURRENT" in line
    assert "NO_ORDERS_SUBMITTED" not in line
    assert not line.startswith("{") and len(line.splitlines()) == 1
    assert report == original


@pytest.mark.parametrize("as_of,pacific", [
    ("2026-10-12T11:06:00Z", "[04:06:00 PDT]"),
    ("2026-12-01T12:06:00Z", "[04:06:00 PST]"),
])
def test_activity_line_uses_local_pacific_time_through_daylight_saving(as_of, pacific):
    assert format_session_cycle_report(_report(_cycle(), as_of=as_of)).startswith(pacific)


@pytest.mark.parametrize("cycle,activity,meaning", [
    (_cycle(status="ORDERS_SUBMITTED", submitted_orders=2), "ORDERS SUBMITTED", "fills are not confirmed"),
    (_cycle(status="ENTRY_CANCELLATION_AWAITING_RECONCILIATION"), "CANCELLATION PENDING",
     "Waiting for broker cancellation confirmation"),
    (_cycle(status="NO_ORDERS_SUBMITTED_RETRY_EXHAUSTED"), "MONITORING",
     "unfilled remainder abandoned"),
    (_cycle(status="TRADER_INACTIVE", activation_active=False), "INACTIVE", "inactive"),
    (_cycle(status="DRY_RUN_INDEPENDENT_STOCK_DECISIONS", execution_requested=False), "DRY RUN", "dry run"),
])
def test_activity_line_explains_order_and_operating_states(cycle, activity, meaning):
    report = _report(cycle)
    assert report["activity"] == activity
    line = format_session_cycle_report(report)
    assert meaning.lower() in line.lower()
    assert f"submitted cycle={cycle['submitted_orders']} session=12" in line
    assert len(line.splitlines()) == 1
    if activity in {"INACTIVE", "DRY RUN", "CANCELLATION PENDING"}:
        assert "continues monitoring" not in line
    if activity == "CANCELLATION PENDING":
        assert "orders cancelled" not in line.lower()


def test_partial_submission_failure_retains_counts_exact_status_and_single_line_error():
    cycle = _cycle(status="SUBMISSION_STOPPED_AFTER_ERROR", submitted_orders=2,
                   stopped_after_error=True, error="ReadTimeout\r\nresponse lost\tplease reconcile")
    report = _report(cycle, failures=1, consecutive=1)
    line = format_session_cycle_report(report)
    assert report["error"] == cycle["error"]  # Sanitizing display must preserve evidence.
    assert "ATTENTION" in line and "SUBMISSION_STOPPED_AFTER_ERROR" in line
    assert "submitted cycle=2 session=12" in line
    assert "ReadTimeout response lost please reconcile" in line
    assert "stopped" in line.lower()
    assert "monitoring" not in line.lower()
    assert len(line.splitlines()) == 1 and "\t" not in line


@pytest.mark.parametrize("capture", [None, {"status": "UNAVAILABLE"}])
def test_idle_status_with_unresolved_failure_never_claims_healthy_monitoring(capture):
    report = _report(_cycle(broker_state_capture=capture), failures=2, consecutive=2)
    line = format_session_cycle_report(report)
    assert report["session_status"] == "DEGRADED"
    assert report["activity"] == "ATTENTION"
    assert "NO_ORDERS_SUBMITTED" in line and "monitoring" not in line.lower()


def test_unrecognized_status_is_visible_without_inventing_recovery():
    line = format_session_cycle_report(_report(_cycle(status="FUTURE_OPERATIONAL_STATUS")))
    assert "FUTURE_OPERATIONAL_STATUS" in line
    assert "WAITING" in line
    assert "monitoring" not in line.lower()


class Clock:
    def __init__(self, value):
        self.now = pd.Timestamp(value)

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += pd.Timedelta(seconds=seconds)


def test_session_reports_cumulative_orders_and_retains_full_saved_cycle(tmp_path, monkeypatch):
    clock = Clock("2026-10-12T23:58:30Z")
    monkeypatch.setattr(worker, "_has_inventory", lambda path: True)
    payloads = [
        _cycle(status="ORDERS_SUBMITTED", submitted_orders=12),
        _cycle(status="HORIZON_BROKER_RECONCILIATION_UNAVAILABLE", error="ReadTimeout",
               broker_state_capture={"status": "UNAVAILABLE", "attempts": 3}),
        _cycle(status="NO_DIRECTIONAL_STOCK_ENTRY_SIGNAL", broker_state_capture=None),
        _cycle(),
    ]
    attempts = []
    emitted = []
    saved = []
    status_path = tmp_path / "state/independent-stock-trader/session-status.json"
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent",
                        lambda root: SimpleNamespace(active=len(attempts) < len(payloads)))

    def runner(root, *, execute, entries, runtime_clock, session_managed):
        assert execute is False and entries is False and session_managed is True
        attempts.append(runtime_clock())
        payload = payloads[len(attempts) - 1]
        return SimpleNamespace(submitted_orders=payload["submitted_orders"], to_dict=lambda: payload)

    def sleep(seconds):
        saved.append(json.loads(status_path.read_text()))
        clock.sleep(seconds)

    result = worker.run_independent_stock_session(
        tmp_path, execute=False, clock=clock, sleep=sleep, runner=runner,
        reporter=emitted.append,
    )
    assert json.loads(emitted[0])["status"] == "SESSION_STARTED"
    lines = emitted[1:]
    assert len(lines) == len(payloads) == len(saved)
    assert all(not line.startswith("{") and len(line.splitlines()) == 1 for line in lines)
    assert "submitted cycle=12 session=12" in lines[0]
    assert "fills are not confirmed" in lines[0]
    assert all("submitted cycle=0 session=12" in line for line in lines[1:])
    assert "HORIZON_BROKER_RECONCILIATION_UNAVAILABLE" in lines[1] and "ReadTimeout" in lines[1]
    assert "ATTENTION" in lines[1] and "ATTENTION" in lines[2]
    assert "monitoring" not in lines[1].lower() and "monitoring" not in lines[2].lower()
    assert "No new orders this cycle; session continues monitoring." in lines[-1]
    assert [status["calls"] for status in saved] == [1, 2, 3, 4]
    assert [status["failed_cycles"] for status in saved] == [0, 1, 1, 1]
    assert [status["consecutive_failures"] for status in saved] == [0, 1, 1, 0]
    assert [status["last_cycle"] for status in saved] == payloads
    assert [status["status"] for status in saved] == ["RUNNING", "DEGRADED", "DEGRADED", "RUNNING"]
    final = json.loads(status_path.read_text())
    assert final["last_cycle"] == payloads[-1]
    assert final["orders_submitted"] == result["orders_submitted"] == 12
    assert final["calls"] == result["calls"] == 4
    assert result["status"] == "SESSION_STOPPED_TRADER_INACTIVE"
