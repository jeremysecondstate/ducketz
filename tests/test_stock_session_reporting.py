from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from ml.stock_trader import independent_session as worker
from ml.stock_trader.session_reporting import session_cycle_report


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
    assert "message" not in report


def test_no_capture_cycle_does_not_claim_session_recovery():
    report = _report(_cycle(status="NO_DIRECTIONAL_STOCK_ENTRY_SIGNAL", broker_state_capture=None),
                     failures=1, consecutive=1)
    assert report["status"] == "NO_DIRECTIONAL_STOCK_ENTRY_SIGNAL"
    assert report["broker_capture_status"] is None
    assert report["session_status"] == "DEGRADED"
    assert report["session_consecutive_failures"] == 1
    assert "message" not in report


def test_pacific_timestamp_tracks_standard_time_and_omits_absent_error_fields():
    report = _report({"status": "NO_ENTRY_SIGNAL"}, as_of="2026-12-01T12:06:00Z")
    assert report["timestamp_pacific"] == "2026-12-01T04:06:00-08:00"
    assert "error" not in report
    assert "stopped_after_error" not in report


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
        reporter=lambda message: emitted.append(json.loads(message)),
    )
    assert emitted[0]["status"] == "SESSION_STARTED"
    reports = emitted[1:]
    assert len(reports) == len(payloads) == len(saved)
    assert [report["orders_submitted_this_cycle"] for report in reports] == [12, 0, 0, 0]
    assert [report["session_orders_submitted"] for report in reports] == [12, 12, 12, 12]
    assert [report["session_cycles"] for report in reports] == [1, 2, 3, 4]
    assert [report["session_failed_cycles"] for report in reports] == [0, 1, 1, 1]
    assert [report["session_consecutive_failures"] for report in reports] == [0, 1, 1, 0]
    assert [report["session_status"] for report in reports] == ["RUNNING", "DEGRADED", "DEGRADED", "RUNNING"]
    assert [report["status"] for report in reports] == [payload["status"] for payload in payloads]
    assert all("message" not in report for report in reports[:-1])
    assert reports[-1]["message"] == "No new orders this cycle; session continues monitoring."
    assert [status["last_cycle"] for status in saved] == payloads
    assert [status["status"] for status in saved] == [report["session_status"] for report in reports]
    final = json.loads(status_path.read_text())
    assert final["last_cycle"] == payloads[-1]
    assert final["orders_submitted"] == result["orders_submitted"] == 12
    assert final["calls"] == result["calls"] == 4
    assert result["status"] == "SESSION_STOPPED_TRADER_INACTIVE"
