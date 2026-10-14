"""Offline continuation of the same dated night, without providers or trading."""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from ml import nightly_workflow as workflow, nightly_dispatch as dispatch
from ml import overnight_runtime as runtime
from ml.artifacts import utc_timestamp
from ml.nightly_continuation import ENVIRONMENT_KEY, POLICY, continuing, deadline_expired
from test_nightly_dispatch import config, run, identity
from test_late_gameplan_recovery import sources
from test_overnight_supervision import _terminal_attempt, _evidence_snapshot


def test_missing_night_starts_after_target_without_new_expiry(config):
    config.pop("continue_until_complete")
    result = run(config, "datastore", now="2026-10-12T22:00Z")
    assert result["action_date"] == "2026-10-12"
    assert result["deadline_at"] == "2026-10-12T11:00:00+00:00"
    assert result["continuation_policy"] == POLICY
    assert "scheduled_recovery" not in result and "recovery_deadline_at" not in result


def test_unfinished_night_retains_identity_across_next_kickoff(config):
    config.pop("continue_until_complete")
    first = run(config, "datastore")
    # At the next scheduled kickoff, finish the existing dated night first.
    resumed = run(config, "stats", now="2026-10-13T04:15Z")
    assert (resumed["run_id"], resumed["action_date"], resumed["deadline_at"]) == (
        first["run_id"], first["action_date"], first["deadline_at"])
    assert resumed["steps"]["datastore_catchup"] == first["steps"]["datastore_catchup"]


def test_completed_night_and_stale_archive_attempt_never_replay(config):
    config.pop("continue_until_complete")
    for role in dispatch.RESPONSIBILITIES:
        complete = run(config, role)
    old = Path(config["state_root"]) / "runs/2026-10-01/state.json"
    workflow._write(old, {"status": "FAILED", "action_date": "2026-10-01"})
    path = Path(config["state_root"]) / "runs/2026-10-12/state.json"
    before = path.read_bytes()
    result = run(config, "datastore", lambda *a: pytest.fail("Completed logical night replayed"), now="2026-10-13T02:00Z")
    assert result["run_id"] == complete["run_id"] and path.read_bytes() == before
    decision = workflow.dispatch_status(config, now="2026-10-13T04:06Z")
    assert decision["dispatch"] and decision["action_date"] == "2026-10-13"


def test_transient_failure_remains_retryable_after_three_attempts(config):
    config.pop("continue_until_complete")
    def failed(*_):
        raise ConnectionError("fixture transient provider exception")
    for minute in (5, 11, 17):
        with pytest.raises(ConnectionError):
            run(config, "datastore", failed, now=f"2026-10-10T04:{minute}:00Z")
    assert workflow.dispatch_status(config, now="2026-10-10T04:23Z")["dispatch"]


def test_dated_child_context_does_not_change_parent_environment(tmp_path, monkeypatch):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)
    updates = []
    runtime._run_stage([sys.executable, "-c", f"import os; print(os.environ['{ENVIRONMENT_KEY}'])"],
        repository=tmp_path, log_path=tmp_path / "child.log", deadline=utc_timestamp()-pd.Timedelta(days=1),
        stop_request=tmp_path / "stop.json", progress=updates.append, poll_seconds=.02,
        continuation_action_date="2026-10-12")
    assert (tmp_path / "child.log").read_text().strip() == "2026-10-12"
    assert not continuing()
    assert updates[-1]["publication_target_missed"]


def test_late_resume_preserves_receipts_and_action_date(tmp_path, monkeypatch):
    prior = _terminal_attempt(tmp_path, monkeypatch)
    before = _evidence_snapshot(prior)
    report = json.loads((prior / "stage-report.json").read_text())
    target = pd.Timestamp(report["deadline_at"])
    day = target.tz_convert("America/Los_Angeles").date().isoformat()
    monkeypatch.setattr(runtime, "utc_timestamp", lambda value=None: pd.Timestamp(value) if value else target+pd.Timedelta(days=3))
    saved = runtime._resume_configuration(tmp_path, prior, continuation_action_date=day)
    assert saved["deadline_at"] == report["deadline_at"]
    assert _evidence_snapshot(prior) == before
    with pytest.raises(ValueError, match="cannot change"):
        runtime._resume_configuration(tmp_path, prior, continuation_action_date="2020-01-01")


def test_telemetry_error_waits_for_useful_child_without_termination(tmp_path, monkeypatch):
    marker = tmp_path / "completed.txt"
    command = [sys.executable, "-c", "import pathlib,time; time.sleep(.15); pathlib.Path('completed.txt').write_text('complete')"]
    monkeypatch.setattr(runtime, "_terminate_owned_process", lambda _: pytest.fail("No human stop"))
    def telemetry(_):
        raise OSError("fixture failed telemetry write")
    with pytest.raises(OSError, match="telemetry"):
        runtime._run_stage(command, repository=tmp_path, log_path=tmp_path / "child.log",
            deadline=utc_timestamp()-pd.Timedelta(days=1), stop_request=tmp_path / "stop.json",
            progress=telemetry, poll_seconds=.02)
    assert marker.read_text() == "complete"


def test_asserted_stop_cannot_interrupt_child_during_failed_telemetry(tmp_path):
    stop = tmp_path / "stop.json"
    def telemetry(_):
        stop.write_text(json.dumps({"human_authorized": True,
            "authorization_source": "fixture explicit user stop", "reason": "stop now"}))
        raise OSError("telemetry unavailable")
    with pytest.raises(OSError, match="telemetry"):
        runtime._run_stage([sys.executable, "-c", "import pathlib,time; time.sleep(.15); pathlib.Path('completed.txt').write_text('complete')"],
            repository=tmp_path, log_path=tmp_path / "child.log", deadline=utc_timestamp(),
            stop_request=stop, progress=telemetry, poll_seconds=.02)
    assert (tmp_path / "completed.txt").read_text() == "complete"
    assert stop.is_file()


def test_older_human_resume_does_not_authorize_later_stop(config, tmp_path, monkeypatch):
    prior = tmp_path / "attempt"
    prior.mkdir()
    stop = prior / "stop-request.json"
    stop.write_text("new human stop")
    state = {"deadline_at": "2026-10-12T11:00Z", "action_date": "2026-10-12", "source_session": "2026-10-09",
        "human_resume": {"native_run": str(prior), "source": "old resume", "stop_request_sha256": "0"*64},
        "steps": {"datastore_catchup": {"native_run": str(prior)}}}
    (prior / "receipt.json").write_text('{"status":"CANCELLED"}')
    calls = []
    monkeypatch.setattr(runtime, "run_overnight_pipeline", lambda *a, **kw: calls.append(kw) or prior)
    monkeypatch.setattr(workflow, "_native_outputs", lambda *_: {"files": {}})
    workflow._run_native(config, state, "datastore_catchup", lambda: None)
    assert "human_resume_source" not in calls[0]


def test_saved_human_stop_assertion_cannot_block_automatic_continuation(tmp_path, monkeypatch):
    prior = _terminal_attempt(tmp_path, monkeypatch, status="CANCELLED")
    (prior / "stop-request.json").write_text(json.dumps({"human_authorized": True,
        "authorization_source": "fixture explicit human request", "reason": "stop"}))
    before = _evidence_snapshot(prior)
    target = json.loads((prior / "stage-report.json").read_text())["deadline_at"]
    day = pd.Timestamp(target).tz_convert("America/Los_Angeles").date().isoformat()
    runtime._resume_configuration(tmp_path, prior, continuation_action_date=day)
    assert _evidence_snapshot(prior) == before


def test_continuation_keeps_causal_inputs_after_session_close(monkeypatch):
    from ml.nightly_gameplan import _current_overnight_sources
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-10-09")
    frame = sources()
    before = frame.copy(deep=True)
    result, day = _current_overnight_sources(frame, symbols=("AAPL", "MSFT"),
        as_of=pd.Timestamp("2026-10-12T15:00Z"), late_action_date="2026-10-09")
    assert day.isoformat() == "2026-10-09"
    pd.testing.assert_frame_equal(result, before)
    frame.loc[0, "information_available_at"] = pd.Timestamp("2026-10-09T12:00Z")
    with pytest.raises(ValueError, match="predate"):
        _current_overnight_sources(frame, symbols=("AAPL", "MSFT"),
            as_of=pd.Timestamp("2026-10-12T15:00Z"), late_action_date="2026-10-09")


def test_expired_planning_rows_never_consume_cash_or_generate_events():
    from test_gameplan_cash_ledger import forecast, snapshot, path, POLICY as SIZING
    from ml.gameplan_cash_ledger import project_direction_trades
    state = snapshot()
    state["observed_at"] = "2026-09-10T04:00:00Z"
    original = pd.DataFrame([forecast(hour=4), forecast(hour=10)])
    rows, ledger = project_direction_trades(original, state, path(), policy=SIZING, signal_driven=True)
    assert not ledger["events"]
    assert rows.direction_based_trade_quantity.eq(0).all()
    assert rows.direction_based_reason.eq("EXPIRED_BEFORE_PREPARATION").all()
    pd.testing.assert_series_equal(rows.target_window_start, original.target_window_start)


def test_expired_scheduled_rows_have_no_provisional_quantity():
    from test_gameplan_trade_planning import forecast, snapshot, bands
    from ml.gameplan_trade_planning import plan_trade_rows
    state = snapshot()
    state["observed_at"] = "2026-09-10T04:00:00Z"
    frame = pd.DataFrame([forecast()])
    result = plan_trade_rows(frame, state, bands(frame.to_dict("records")))
    assert result.trade_quantity.eq(0).all() and result.projected_trade_quantity.eq(0).all()
    assert result.trade_planning_reason.eq("EXPIRED_BEFORE_PREPARATION").all()


def test_expired_cash_rows_still_require_valid_target_clocks():
    from test_gameplan_cash_ledger import forecast, snapshot, path, POLICY as SIZING
    from ml.gameplan_cash_ledger import project_direction_trades
    state = snapshot()
    state["observed_at"] = "2026-09-10T04:00:00Z"
    row = forecast(hour=4)
    row["target_window_end"] = row["target_window_start"]
    with pytest.raises(ValueError, match="clocks"):
        project_direction_trades(pd.DataFrame([row]), state, path(), policy=SIZING, signal_driven=True)


def test_deadline_context_cannot_override_another_session(monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-10-12")
    assert not deadline_expired(pd.Timestamp("2026-10-13T04:00Z"), pd.Timestamp("2026-10-12T11:00Z"))
    assert deadline_expired(pd.Timestamp("2026-10-13T14:00Z"), pd.Timestamp("2026-10-13T11:00Z"))


def test_archived_late_forecasts_retain_real_times_after_session_close():
    from test_late_forecast_validation import validate, _frame
    frame = _frame()
    frame["frozen_at"] = pd.Timestamp("2026-09-08T16:00Z")
    result = validate(frame, published="2026-09-09T02:00Z")
    for column in ("frozen_at", "target_window_start", "target_window_end"):
        pd.testing.assert_series_equal(result[column], frame[column])


@pytest.mark.parametrize("kind", ["HUMAN_STOP", "TRANSIENT", "SOURCE_DEFECT", "EXTERNAL_DEPENDENCY"])
def test_saved_cancellation_or_delay_never_blocks_standing_continuation(config, kind):
    config.pop("continue_until_complete")
    state = {"failure": {"kind": kind, "step": "datastore_catchup",
        "retry_after": "2026-10-20T20:00Z", "disposition": "OPEN"},
        "steps": {"datastore_catchup": {"attempts": 99}}}
    assert dispatch.retry_disposition(config, state, "2026-10-14T04:00Z") is None
