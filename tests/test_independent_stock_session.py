from __future__ import annotations

from types import SimpleNamespace
import json

import pandas as pd
import pytest

from ml.stock_trader import independent_session as worker


class Clock:
    def __init__(self, value):
        self.now = pd.Timestamp(value)
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += pd.Timedelta(seconds=seconds)


def _run(tmp_path, monkeypatch, *, started="2026-09-08T10:55:00Z", inventory=False, active=True, qualified_fixture=True):
    clock = Clock(started)
    calls = []
    monkeypatch.setattr(worker, "_has_inventory", lambda path: inventory)
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent", lambda root: SimpleNamespace(active=active))
    if qualified_fixture:
        monkeypatch.setattr(worker, "_independent_enrichment_preflight", lambda root, **kwargs: {"status": "READY"})

    def runner(root, *, execute, entries, runtime_clock, session_managed):
        assert root == tmp_path.resolve()
        assert execute is False
        assert session_managed is True
        calls.append((runtime_clock(), entries))
        return SimpleNamespace(submitted_orders=0, to_dict=lambda: {"status": "SYNTHETIC_NO_ORDERS", "submitted_orders": 0})

    result = worker.run_independent_stock_session(
        tmp_path, execute=False, clock=clock, sleep=clock.sleep, runner=runner, reporter=lambda message: None,
    )
    return result, calls, clock


def test_empty_inventory_only_runs_one_combined_entry_batch_per_hour(tmp_path, monkeypatch):
    result, calls, clock = _run(tmp_path, monkeypatch)
    assert result["status"] == "SESSION_FINISHED"
    assert result["orders_submitted"] == 0
    assert len(calls) == 13
    assert all(entry for _, entry in calls)
    local = [timestamp.tz_convert("America/Los_Angeles") for timestamp, _ in calls]
    assert [(value.hour, value.minute) for value in local] == [(hour, 6 if hour == 13 else 1) for hour in range(4, 17)]
    assert clock.now == pd.Timestamp("2026-09-09T00:00:00Z")
    assert all(0 < delay <= 30 for delay in clock.sleeps)


def test_fixed_policy_worker_runs_clock_even_with_no_bullish_forecasts(tmp_path, monkeypatch):
    clock = Clock("2026-09-08T10:55:00Z")
    calls = []
    monkeypatch.setattr(worker, "_has_inventory", lambda path: False)
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent", lambda root: SimpleNamespace(active=True))
    monkeypatch.setattr(worker, "_independent_forecast_preflight", lambda root, **kwargs:
                        {"status": "READY", "bullish_entry_windows": [], "all_execution_windows_qualified": True})
    monkeypatch.setattr(worker, "_independent_enrichment_preflight", lambda *args, **kwargs: pytest.fail("Wrong sizing strategy preflight"))
    def runner(root, **kwargs):
        assert kwargs["sizing_policy"] == "fixed-horizon-budget-v1"
        assert kwargs["execute"] is False and kwargs["session_managed"] is True
        calls.append(kwargs["runtime_clock"]())
        return SimpleNamespace(submitted_orders=0, to_dict=lambda: {"status": "NO_ENTRY_SIGNAL"})
    result = worker.run_independent_stock_session(tmp_path, execute=False, clock=clock, sleep=clock.sleep,
        runner=runner, reporter=lambda message: None, sizing_policy="fixed-horizon-budget-v1")
    assert result["status"] == "SESSION_FINISHED"
    assert result["orders_submitted"] == 0
    assert len(calls) == 13


def test_inventory_checks_never_create_extra_entries_and_manage_close_lead(tmp_path, monkeypatch):
    result, calls, clock = _run(tmp_path, monkeypatch, inventory=True)
    entries = [(timestamp, entry) for timestamp, entry in calls if entry]
    assert len(entries) == 13
    assert len(calls) > 13
    assert all(timestamp < pd.Timestamp("2026-09-09T00:00:00Z") for timestamp, _ in calls)
    close_checks = [(timestamp, entry) for timestamp, entry in calls if timestamp >= pd.Timestamp("2026-09-08T23:59:00Z")]
    assert len(close_checks) == 12
    assert all(not entry for _, entry in close_checks)
    assert [timestamp.second for timestamp, _ in close_checks] == list(range(0, 60, 5))
    assert result["unclosed_allocations"] is True
    assert result["orders_submitted"] == 0


def test_worker_does_not_backfill_missed_entry_boundary(tmp_path, monkeypatch):
    _, calls, _ = _run(tmp_path, monkeypatch, started="2026-09-08T23:02:00Z")
    assert calls == []


@pytest.mark.parametrize("recover", [False, True])
def test_worker_persists_degraded_health_until_a_successful_cycle(tmp_path, monkeypatch, recover):
    clock = Clock("2026-09-08T22:01:00Z")
    monkeypatch.setattr(worker, "_has_inventory", lambda path: False)
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent", lambda root: SimpleNamespace(active=True))
    monkeypatch.setattr(worker, "_independent_enrichment_preflight", lambda *args, **kwargs: {"status": "READY"})
    observed = []
    status_path = tmp_path / "state/independent-stock-trader/session-status.json"
    attempts = []
    def runner(root, **kwargs):
        attempts.append(clock())
        failed = not recover or len(attempts) == 1
        payload = {"status": "HORIZON_BROKER_RECONCILIATION_UNAVAILABLE" if failed else "NO_ORDERS_SUBMITTED",
                   "error": "ReadTimeout" if failed else None, "submitted_orders": 0,
                   "broker_state_capture": {"status": "UNAVAILABLE" if failed else "CURRENT"}}
        return SimpleNamespace(submitted_orders=0, to_dict=lambda: payload)
    def sleep(seconds):
        observed.append(json.loads(status_path.read_text()))
        clock.sleep(seconds)
    result = worker.run_independent_stock_session(tmp_path, clock=clock, sleep=sleep, runner=runner,
                                                  reporter=lambda message: None)
    assert len(attempts) == 2
    assert observed[0]["status"] == "DEGRADED"
    assert observed[0]["consecutive_failures"] == 1
    assert result["failed_cycles"] == (1 if recover else 2)
    assert result["status"] == ("SESSION_FINISHED" if recover else "SESSION_FINISHED_WITH_ERRORS")
    final = json.loads(status_path.read_text())
    assert final["status"] == ("FINISHED" if recover else "FINISHED_WITH_ERRORS")
    assert final["consecutive_failures"] == (0 if recover else 2)


@pytest.mark.parametrize("recovery_capture", [None, "CURRENT", "CURRENT_AFTER_RETRY"])
def test_no_capture_cycles_preserve_failure_until_verified_broker_recovery(tmp_path, monkeypatch, recovery_capture):
    clock = Clock("2026-09-08T21:01:00Z")
    monkeypatch.setattr(worker, "_has_inventory", lambda path: False)
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent", lambda root: SimpleNamespace(active=True))
    monkeypatch.setattr(worker, "_independent_enrichment_preflight", lambda *args, **kwargs: {"status": "READY"})
    payloads = [
        {"status": "HORIZON_BROKER_RECONCILIATION_UNAVAILABLE", "error": "ReadTimeout",
         "broker_state_capture": {"status": "UNAVAILABLE"}},
        {"status": "NO_DIRECTIONAL_STOCK_ENTRY_SIGNAL", "broker_state_capture": None},
        {"status": "NO_QUALIFIED_INDEPENDENT_STOCK_ENTRIES" if recovery_capture is None else "NO_ORDERS_SUBMITTED",
         "broker_state_capture": None if recovery_capture is None else {"status": recovery_capture}},
    ]
    attempts = []
    observed = {}
    status_path = tmp_path / "state/independent-stock-trader/session-status.json"
    def runner(root, **kwargs):
        payload = payloads[len(attempts)]
        attempts.append(clock())
        return SimpleNamespace(submitted_orders=0, to_dict=lambda: payload)
    def sleep(seconds):
        status = json.loads(status_path.read_text())
        observed[status["calls"]] = status
        clock.sleep(seconds)
    result = worker.run_independent_stock_session(tmp_path, clock=clock, sleep=sleep, runner=runner,
                                                  reporter=lambda message: None)
    assert len(attempts) == 3
    assert observed[1]["status"] == observed[2]["status"] == "DEGRADED"
    assert observed[1]["consecutive_failures"] == observed[2]["consecutive_failures"] == 1
    assert result["failed_cycles"] == 1  # No-signal results remain normal cycles.
    recovered = recovery_capture is not None
    assert result["status"] == ("SESSION_FINISHED" if recovered else "SESSION_FINISHED_WITH_ERRORS")
    assert result["consecutive_failures"] == (0 if recovered else 1)
    assert observed[3]["status"] == ("RUNNING" if recovered else "DEGRADED")


@pytest.mark.parametrize("failure", [
    {"status": "SUBMISSION_STOPPED_SAFETY_CHECK"},
    {"status": "SUBMISSION_STOPPED_AFTER_ERROR"},
    {"status": "NO_ORDERS_SUBMITTED", "stopped_after_error": True},
    {"status": "HORIZON_BROKER_RECONCILIATION_UNAVAILABLE"},
])
def test_successful_capture_does_not_hide_later_cycle_failure(tmp_path, monkeypatch, failure):
    clock = Clock("2026-09-08T23:01:00Z")
    monkeypatch.setattr(worker, "_has_inventory", lambda path: False)
    monkeypatch.setattr(worker, "read_gameplan_stock_activation_intent", lambda root: SimpleNamespace(active=True))
    monkeypatch.setattr(worker, "_independent_enrichment_preflight", lambda *args, **kwargs: {"status": "READY"})
    payload = {**failure, "broker_state_capture": {"status": "CURRENT"}}
    result = worker.run_independent_stock_session(tmp_path, clock=clock, sleep=clock.sleep,
        runner=lambda *args, **kwargs: SimpleNamespace(submitted_orders=0, to_dict=lambda: payload),
        reporter=lambda message: None)
    assert result["status"] == "SESSION_FINISHED_WITH_ERRORS"
    assert result["failed_cycles"] == result["consecutive_failures"] == 1


@pytest.mark.parametrize(("status", "exit_code"), [
    ("SESSION_FINISHED_WITH_ERRORS", 1),
    ("NOOP_STOCK_FORECASTS_NOT_QUALIFIED", 1),
    ("NOOP_ENRICHMENT_NOT_QUALIFIED", 1),
    ("SESSION_FINISHED", 0),
    ("NOOP_UNSUPPORTED_OR_CLOSED_SESSION", 0),
])
def test_session_cli_reports_operational_failure_to_os_scheduler(tmp_path, monkeypatch, status, exit_code):
    from ml import gameplan_stock_trader as cli
    monkeypatch.setattr(cli, "resolve_datastore_dir", lambda **kwargs: tmp_path)
    monkeypatch.setattr(worker, "run_independent_stock_session", lambda *args, **kwargs: {"status": status})
    assert cli.main(["--datastore-target", "pc", "--target-horizon", "all", "--run-session"]) == exit_code


@pytest.mark.parametrize(("started", "status"), [
    ("2026-09-07T10:55:00Z", "NOOP_UNSUPPORTED_OR_CLOSED_SESSION"),
    ("2026-11-27T11:55:00Z", "NOOP_UNSUPPORTED_OR_CLOSED_SESSION"),
    ("2026-09-09T00:00:00Z", "NOOP_SESSION_FINISHED"),
])
def test_holiday_early_close_and_finished_session_never_wait_for_next_day(tmp_path, monkeypatch, started, status):
    result, calls, clock = _run(tmp_path, monkeypatch, started=started)
    assert result["status"] == status
    assert calls == []
    assert clock.sleeps == []


def test_worker_rejects_overnight_start_before_bounded_window(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="03:55 Pacific"):
        _run(tmp_path, monkeypatch, started="2026-09-08T10:54:59Z")


def test_worker_stops_immediately_when_controls_are_inactive(tmp_path, monkeypatch):
    result, calls, clock = _run(tmp_path, monkeypatch, active=False, inventory=True)
    assert result["status"] == "SESSION_STOPPED_TRADER_INACTIVE"
    assert calls == []
    assert clock.sleeps == []


def test_unqualified_current_hourly_model_returns_immediate_noop_without_runner_or_sleep(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "load_current_enrichment_model", lambda root: SimpleNamespace(
        qualified_target_contracts=(), supported_horizons=("1h",), model_fingerprint="verified-hourly-model",
    ))
    result, calls, clock = _run(tmp_path, monkeypatch, qualified_fixture=False)
    assert result["status"] == "NOOP_ENRICHMENT_NOT_QUALIFIED"
    assert result["calls"] == result["orders_submitted"] == 0
    assert result["enrichment_readiness"]["reason"] == "ENRICHMENT_INDEPENDENT_TARGET_CONTRACT_NOT_QUALIFIED"
    assert calls == []
    assert clock.sleeps == []


def test_missing_model_is_immediate_noop_but_does_not_block_inventory_management(tmp_path, monkeypatch):
    def missing_model(root):
        raise ValueError("Model receipt is unavailable")

    monkeypatch.setattr(worker, "load_current_enrichment_model", missing_model)
    result, calls, clock = _run(tmp_path, monkeypatch, qualified_fixture=False)
    assert result["status"] == "NOOP_ENRICHMENT_NOT_QUALIFIED"
    assert result["enrichment_readiness"]["reason"] == "ENRICHMENT_ARTIFACT_UNAVAILABLE"
    assert calls == clock.sleeps == []
    result, calls, _ = _run(tmp_path, monkeypatch, qualified_fixture=False, inventory=True, started="2026-09-08T23:59:00Z")
    assert result["status"] == "SESSION_FINISHED"
    assert len(calls) == 12
    assert all(not entries for _, entries in calls)


def test_claimed_contract_metadata_alone_cannot_qualify_longer_current_targets(tmp_path, monkeypatch):
    from datetime import date
    from test_independent_stock_signals import _frame, _publish
    from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION

    _publish(tmp_path, _frame())
    monkeypatch.setattr(worker, "load_current_enrichment_model", lambda root: SimpleNamespace(
        qualified_target_contracts=(STOCK_TARGET_CONTRACT_VERSION,),
        supported_horizons=("1h", "4h", "1d", "1w"), model_fingerprint="synthetic-claimed-scope",
    ))
    readiness = worker._independent_enrichment_preflight(tmp_path, action_date=date(2026, 9, 8))
    assert readiness["status"] == "NOT_READY"
    assert readiness["reason"] == "NO_QUALIFIED_ENTRY_WINDOWS"
    assert all(check["status"] != "READY" for check in readiness["checks"].values())
    assert readiness["qualified_entry_windows"] == []


def test_qualified_hourly_entries_are_not_blocked_by_research_weekly_models(tmp_path, monkeypatch):
    from datetime import date
    from test_independent_stock_signals import _frame, _publish
    from ml.independent_stock_targets import STOCK_TARGET_CONTRACT_VERSION

    frame = _frame()
    frame.loc[frame.model_group.ne("1h"), "model_status"] = "RESEARCH_NOT_PROMOTED"
    _publish(tmp_path, frame)
    monkeypatch.setattr(worker, "load_current_enrichment_model", lambda root: SimpleNamespace(
        qualified_target_contracts=(STOCK_TARGET_CONTRACT_VERSION,),
        supported_horizons=("1h",), model_fingerprint="synthetic-partial-scope",
    ))
    observed_sources = []
    def readiness(model, signal):
        observed_sources.append(signal.target_price_source_contract)
        return {"status": "READY" if signal.primary_horizon == "1h" else "NOT_READY"}
    monkeypatch.setattr(worker, "enrichment_signal_readiness", readiness)
    result = worker._independent_enrichment_preflight(tmp_path, action_date=date(2026, 9, 8))
    assert result["status"] == "READY"
    assert result["qualified_entry_windows"]
    assert all("/1h@" in key for key in result["qualified_entry_windows"])
    assert set(observed_sources) == {"canonical-equity-minute-v1"}
