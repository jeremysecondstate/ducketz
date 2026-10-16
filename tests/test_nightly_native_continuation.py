"""Offline coverage for native preparation continuing its original action date."""

import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

from ml import overnight_runtime
from ml.artifacts import file_checksum, utc_timestamp
from ml.nightly_continuation import ENVIRONMENT_KEY
from ml.overnight_runtime import _run_stage as real_run_stage
from test_nightly_stats_segments import _feedback
from test_overnight_supervision import _evidence_snapshot, run


ACTION_DATE = "2026-09-10"
REVIEW_DATE = "2026-09-09"
DEADLINE = "2026-09-10T11:00:00+00:00"


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    clock = {"now": pd.Timestamp("2026-09-10T04:05:00Z")}
    monkeypatch.setattr(
        overnight_runtime, "utc_timestamp",
        lambda value=None: utc_timestamp(value) if value is not None else clock["now"],
    )
    monkeypatch.setattr(
        overnight_runtime, "_run_stage",
        lambda *args, **kwargs: pytest.fail("A test must provide a synthetic stage runner"),
    )

    def pinned(root, *, pinned=None, deadline_at, **kwargs):
        return pinned or {
            "run_path": "ml/nightly-gameplan-runs/synthetic-pinned-run",
            "receipt_sha256": "synthetic-receipt",
            "action_date": deadline_at.tz_convert("America/Los_Angeles").date().isoformat(),
        }

    monkeypatch.setattr(overnight_runtime, "_pin_stock_gameplan", pinned)
    return clock


def _stages(monkeypatch, *, fail_module=None):
    calls = []

    def stage(command, **kwargs):
        calls.append((command, kwargs))
        kwargs["log_path"].write_text("synthetic continuation evidence", encoding="utf-8")
        return 7 if command[3] == fail_module else 0

    monkeypatch.setattr(overnight_runtime, "_run_stage", stage)
    return calls


def _report(directory):
    return json.loads((directory / "stage-report.json").read_text(encoding="utf-8"))


def _prepare(root, **kwargs):
    arguments = {
        "stock_only": True,
        "independent_stock_horizons": True,
        "stats_first": True,
        "deadline": DEADLINE,
        "review_action_date": REVIEW_DATE,
        "continuation_action_date": ACTION_DATE,
    }
    arguments.update(kwargs)
    return run(root, **arguments)


def test_stats_segment_keeps_review_session_separate_from_continuation_date(tmp_path, monkeypatch):
    calls = _stages(monkeypatch)
    directory = _prepare(tmp_path, stop_after="gameplan_stats")
    report = _report(directory)
    assert report["stage_order"] == ["loop_a_close_fetch", "gameplan_stats"]
    assert report["review_action_date"] == REVIEW_DATE
    assert report["continuation_action_date"] == report["late_action_date"] == ACTION_DATE
    assert report["deadline_at"] == report["effective_deadline_at"] == DEADLINE
    assert all(kwargs["continuation_action_date"] == ACTION_DATE for _, kwargs in calls)
    stats_command = calls[-1][0]
    assert stats_command[stats_command.index("--action-date") + 1] == REVIEW_DATE
    assert all("--late-action-date" not in command for command, _ in calls)
    assert report["status"] == "COMPLETE"


def test_training_and_planning_receive_original_dated_continuation(tmp_path, monkeypatch, frozen_clock):
    feedback = _feedback(tmp_path)
    frozen_clock["now"] = pd.Timestamp("2026-09-11T04:05:00Z")
    calls = _stages(monkeypatch)
    directory = _prepare(
        tmp_path, start_at="loop_b_directional_generation", model_feedback=feedback,
    )
    report = _report(directory)
    assert report["stage_order"] == [
        "loop_b_directional_generation", "gameplan_evaluation", "gameplan_publication",
        "stock_enrichment_training", "gameplan_trade_planning",
    ]
    assert report["continuation_action_date"] == report["late_action_date"] == ACTION_DATE
    assert report["deadline_at"] == report["effective_deadline_at"] == DEADLINE
    assert report["model_feedback"] == {
        "path": str(feedback.resolve()), "sha256": file_checksum(feedback),
    }
    for command, kwargs in calls:
        assert kwargs["continuation_action_date"] == ACTION_DATE
        assert kwargs["deadline"].isoformat() == DEADLINE
        if command[3] in ("ml.nightly_gameplan", "ml.gameplan_trade_planning"):
            assert command[command.index("--late-action-date") + 1] == ACTION_DATE
        else:
            assert "--late-action-date" not in command
    planning = calls[-1][0]
    assert planning[planning.index("--deadline") + 1] == DEADLINE
    assert report["broker_orders_enabled"] is False and report["orders_placed"] == 0
    assert all("--execute" not in command for command, _ in calls)


def test_failed_retry_inherits_date_deadline_feedback_and_remaining_boundary(tmp_path, monkeypatch, frozen_clock):
    feedback = _feedback(tmp_path)
    _stages(monkeypatch, fail_module="ml.nightly_gameplan")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        _prepare(tmp_path, start_at="loop_b_directional_generation",
                 stop_after="gameplan_publication", model_feedback=feedback)
    failed = Path(overnight_runtime.overnight_status(tmp_path)["run_path"])
    original = _report(failed)
    before = _evidence_snapshot(failed)
    frozen_clock["now"] = pd.Timestamp("2026-09-12T04:05:00Z")
    calls = _stages(monkeypatch)
    resumed = run(tmp_path, resume_run=failed, deadline="2026-09-14T11:00:00Z")
    report = _report(resumed)
    assert report["stage_order"] == ["gameplan_publication"]
    assert report["completed_stages_from_previous_attempt"] == [
        "loop_b_directional_generation", "gameplan_evaluation",
    ]
    for key in ("deadline_at", "effective_deadline_at", "stats_first", "review_action_date",
                "model_feedback", "continuation_action_date", "late_action_date"):
        assert report[key] == original[key]
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command[3] == "ml.nightly_gameplan"
    assert command[command.index("--late-action-date") + 1] == ACTION_DATE
    assert command[command.index("--model-feedback") + 1] == str(feedback)
    assert kwargs["continuation_action_date"] == ACTION_DATE
    assert kwargs["deadline"].isoformat() == DEADLINE
    assert _evidence_snapshot(failed) == before


def test_resume_rejects_another_action_date_before_children_or_new_attempt(tmp_path, monkeypatch):
    _stages(monkeypatch, fail_module="ml.gameplan_actuals_review")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        _prepare(tmp_path, stop_after="gameplan_stats")
    failed = Path(overnight_runtime.overnight_status(tmp_path)["run_path"])
    before = _evidence_snapshot(failed)
    attempts = set(failed.parent.iterdir())
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError):
        run(tmp_path, resume_run=failed, continuation_action_date="2026-09-11")
    assert calls == []
    assert set(failed.parent.iterdir()) == attempts
    assert _evidence_snapshot(failed) == before


@pytest.mark.parametrize("field", ["late_action_date", "continuation_action_date"])
def test_resume_rejects_saved_date_contradicting_original_context(tmp_path, monkeypatch, field):
    _stages(monkeypatch, fail_module="ml.gameplan_actuals_review")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        _prepare(tmp_path, stop_after="gameplan_stats")
    failed = Path(overnight_runtime.overnight_status(tmp_path)["run_path"])
    report = _report(failed)
    report[field] = "2026-09-11"
    report_path = failed / "stage-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    receipt_path = failed / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["stage_report_checksum_sha256"] = file_checksum(report_path)
    receipt["stage_report_size"] = report_path.stat().st_size
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    before = _evidence_snapshot(failed)
    attempts = set(failed.parent.iterdir())
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError):
        run(tmp_path, resume_run=failed)
    assert calls == []
    assert set(failed.parent.iterdir()) == attempts
    assert _evidence_snapshot(failed) == before


@pytest.mark.parametrize("continuation", [None, ACTION_DATE])
def test_legacy_stats_retry_uses_only_explicit_continuation(tmp_path, monkeypatch, continuation):
    _stages(monkeypatch, fail_module="ml.gameplan_actuals_review")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        _prepare(tmp_path, stop_after="gameplan_stats", continuation_action_date=None)
    failed = Path(overnight_runtime.overnight_status(tmp_path)["run_path"])
    report = _report(failed)
    for key in ("continuation_action_date", "late_action_date"):
        report.pop(key, None)
    report_path = failed / "stage-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    receipt_path = failed / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["stage_report_checksum_sha256"] = file_checksum(report_path)
    receipt["stage_report_size"] = report_path.stat().st_size
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    before = _evidence_snapshot(failed)
    calls = _stages(monkeypatch)
    resumed = run(tmp_path, resume_run=failed, continuation_action_date=continuation)
    result = _report(resumed)
    assert result.get("continuation_action_date") == continuation
    assert result.get("late_action_date") == continuation
    assert result["stage_order"] == ["gameplan_stats"]
    assert result["completed_stages_from_previous_attempt"] == ["loop_a_close_fetch"]
    assert result["review_action_date"] == REVIEW_DATE
    assert result["deadline_at"] == result["effective_deadline_at"] == DEADLINE
    assert len(calls) == 1 and calls[0][1].get("continuation_action_date") == continuation
    assert _evidence_snapshot(failed) == before


@pytest.mark.parametrize("arguments", [
    {"continuation_action_date": "2026-09-11"},
    {"deadline": "2026-09-10T06:00:00Z"},  # Still September 9 in Pacific time.
    {"stats_first": False, "review_action_date": None, "stop_after": "gameplan_publication"},
    {"stock_only": False},
    {"independent_stock_horizons": False},
])
def test_continuation_requires_original_pacific_date_and_stats_stock_scope(tmp_path, monkeypatch, arguments):
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError):
        _prepare(tmp_path, **{"stop_after": "gameplan_stats", **arguments})
    assert calls == []
    assert not (tmp_path / "ml/overnight-runs").exists()


@pytest.mark.parametrize("date", [
    "", "2026-9-10", "2026-09-10T00:00:00", "2026-09-10T00:00:00Z", "2026-02-30", "NaT",
])
def test_native_continuation_rejects_nonexact_dates_before_children(tmp_path, monkeypatch, date):
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError):
        _prepare(tmp_path, stop_after="gameplan_stats", continuation_action_date=date)
    assert calls == []
    assert not (tmp_path / "ml/overnight-runs").exists()


def test_ambient_continuation_does_not_enable_native_run(tmp_path, monkeypatch):
    monkeypatch.setenv(ENVIRONMENT_KEY, ACTION_DATE)
    calls = _stages(monkeypatch)
    directory = _prepare(tmp_path, stop_after="gameplan_stats", continuation_action_date=None)
    report = _report(directory)
    assert report.get("continuation_action_date") is None
    assert report.get("late_action_date") is None
    assert all(kwargs.get("continuation_action_date") is None for _, kwargs in calls)
    assert all("--late-action-date" not in command for command, _ in calls)
    assert os.environ[ENVIRONMENT_KEY] == ACTION_DATE


@pytest.mark.parametrize("explicit", [None, ACTION_DATE])
def test_stage_child_environment_is_explicit_and_parent_is_unchanged(tmp_path, monkeypatch, explicit):
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-09-11")
    parent_before = dict(os.environ)
    command = [sys.executable, "-B", "-c",
               f"import json, os; print(json.dumps(os.environ.get({ENVIRONMENT_KEY!r})))"]
    log = tmp_path / "child-environment.log"
    kwargs = {"continuation_action_date": explicit} if explicit is not None else {}
    result = real_run_stage(
        command, repository=tmp_path, log_path=log, deadline=pd.Timestamp(DEADLINE),
        stop_request=tmp_path / "stop.json", progress=lambda _: None,
        poll_seconds=0.03, **kwargs,
    )
    assert result == 0
    assert json.loads(log.read_text(encoding="utf-8")) == explicit
    assert dict(os.environ) == parent_before


@pytest.mark.parametrize("date", ["", "2026-9-10", "2026-09-10T00:00:00Z", "NaT"])
def test_stage_rejects_invalid_date_without_launch_or_environment_mutation(tmp_path, monkeypatch, date):
    monkeypatch.setenv(ENVIRONMENT_KEY, "2026-09-11")
    parent_before = dict(os.environ)
    monkeypatch.setattr(overnight_runtime.subprocess, "Popen",
                        lambda *args, **kwargs: pytest.fail("Invalid date must not launch a child"))
    with pytest.raises(ValueError):
        real_run_stage(
            [sys.executable, "-B", "-c", "pass"], repository=tmp_path,
            log_path=tmp_path / "invalid-date.log", deadline=pd.Timestamp(DEADLINE),
            stop_request=tmp_path / "stop.json", progress=lambda _: None,
            poll_seconds=0.03, continuation_action_date=date,
        )
    assert dict(os.environ) == parent_before


def test_cli_forwards_explicit_continuation_date(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(overnight_runtime, "run_overnight_pipeline",
                        lambda root, **kwargs: calls.append(kwargs) or tmp_path)
    assert overnight_runtime.main([
        "--datastore", str(tmp_path), "--stock-only", "--independent-stock-horizons",
        "--stats-first", "--stop-after", "gameplan_stats",
        "--continuation-action-date", ACTION_DATE,
    ]) == 0
    assert len(calls) == 1
    assert calls[0]["continuation_action_date"] == ACTION_DATE
