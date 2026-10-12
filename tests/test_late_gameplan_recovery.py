"""Offline checks for explicit late publication; never launch real stages."""
from datetime import date
import json
from pathlib import Path

import pandas as pd
import pytest

from ml.nightly_gameplan import _current_overnight_sources


def sources():
    return pd.DataFrame([
        {"symbol": symbol, "action_date": date(2026, 10, 9),
         "source_action_start": pd.Timestamp("2026-10-09T11:00Z"),
         "decision_timestamp": pd.Timestamp("2026-10-09T00:05Z"),
         "information_available_at": pd.Timestamp("2026-10-09T00:05Z")}
        for symbol in ("AAPL", "MSFT")])


def test_late_route_selects_original_causal_rows_without_backdating():
    original = sources()
    now = pd.Timestamp("2026-10-09T15:00Z")
    with pytest.raises(RuntimeError, match="No future"):
        _current_overnight_sources(original, symbols=("AAPL", "MSFT"), as_of=now)
    rows, day = _current_overnight_sources(original, symbols=("AAPL", "MSFT"),
        as_of=now, late_action_date="2026-10-09")
    assert day == date(2026, 10, 9)
    pd.testing.assert_frame_equal(rows, original)
    assert now == pd.Timestamp("2026-10-09T15:00Z")


@pytest.mark.parametrize("day, now", [
    ("2026-10-08", "2026-10-09T15:00Z"),
    ("2026-10-12", "2026-10-09T15:00Z"),
    ("2026-10-09", "2026-10-09T10:59Z"),
    ("2026-10-09", "2026-10-10T00:00Z"),
])
def test_late_route_rejects_wrong_date_and_closed_window(day, now):
    with pytest.raises(ValueError, match="today's open action session"):
        _current_overnight_sources(sources(), symbols=("AAPL", "MSFT"),
            as_of=pd.Timestamp(now), late_action_date=day)


@pytest.mark.parametrize("field", ["decision_timestamp", "information_available_at"])
def test_late_route_rejects_same_day_features(field):
    rows = sources()
    rows.loc[0, field] = pd.Timestamp("2026-10-09T12:00Z")
    with pytest.raises(ValueError, match="predate"):
        _current_overnight_sources(rows, symbols=("AAPL", "MSFT"),
            as_of=pd.Timestamp("2026-10-09T15:00Z"), late_action_date="2026-10-09")


def test_late_route_still_requires_complete_symbol_coverage():
    with pytest.raises(RuntimeError, match="coverage is incomplete"):
        _current_overnight_sources(sources().iloc[:1], symbols=("AAPL", "MSFT"),
            as_of=pd.Timestamp("2026-10-09T15:00Z"), late_action_date="2026-10-09")


def test_runtime_forwards_and_records_late_date(tmp_path, monkeypatch):
    from ml import overnight_runtime as runtime
    calls = []
    monkeypatch.setattr(runtime, "_run_stage", lambda command, **kwargs: calls.append(command) or 0)
    run = runtime.run_overnight_pipeline(tmp_path, datastore_argument=("--datastore", str(tmp_path)),
        repository_root=tmp_path, start_at="gameplan_publication", stop_after="gameplan_publication",
        stock_only=True, independent_stock_horizons=True, late_action_date="2026-10-09", reporter=None)
    command = calls[0]
    assert command[command.index("--late-action-date") + 1] == "2026-10-09"
    report = json.loads((run / "stage-report.json").read_text())
    assert report["late_action_date"] == "2026-10-09"
    assert report["orders_placed"] == 0


def test_workflow_passes_late_mode_only_for_recovery(tmp_path, monkeypatch):
    from ml import nightly_workflow as workflow
    from ml import overnight_runtime as runtime
    calls = []
    monkeypatch.setattr(runtime, "run_overnight_pipeline", lambda *args, **kwargs: calls.append(kwargs) or tmp_path)
    monkeypatch.setattr(workflow, "_native_outputs", lambda *args: {"files": {}})
    monkeypatch.setattr(workflow, "_intended_probability_target", lambda *args: "raw-price-direction-v1")
    config = {"datastore": str(tmp_path), "repository": str(tmp_path)}
    state = {"deadline_at": "2026-10-09T11:00Z", "recovery_deadline_at": "2026-10-09T19:00Z",
        "action_date": "2026-10-09", "source_session": "2026-10-08", "steps": {
            "train_and_plan": {}, "model_review": {"output": {"proposal": str(tmp_path / "proposal.json")}}}}
    workflow._run_native(config, state, "train_and_plan", lambda: None)
    assert calls[0]["late_action_date"] == "2026-10-09"
    assert calls[0]["deadline"] == state["recovery_deadline_at"]
    del state["recovery_deadline_at"]
    state["steps"]["train_and_plan"] = {}
    workflow._run_native(config, state, "train_and_plan", lambda: None)
    assert "late_action_date" not in calls[1]


@pytest.mark.parametrize('late', [None, '2026-10-09'])
def test_runtime_passes_frozen_recovery_date_to_trade_planning(tmp_path, monkeypatch, late):
    from ml import overnight_runtime as runtime
    calls = []
    monkeypatch.setattr(runtime, '_run_stage', lambda command, **kwargs: calls.append((command, kwargs['deadline'])) or 0)
    monkeypatch.setattr(runtime, '_pin_stock_gameplan', lambda *a, **k: {'run_path': 'ml/nightly-gameplan-runs/frozen'})
    deadline = pd.Timestamp('2026-10-09T19:00Z' if late else '2026-10-09T11:00Z')
    runtime.run_overnight_pipeline(tmp_path, datastore_argument=('--datastore', str(tmp_path)),
        repository_root=tmp_path, start_at='gameplan_trade_planning', stop_after='gameplan_trade_planning',
        stock_only=True, independent_stock_horizons=True, late_action_date=late,
        deadline=deadline, reporter=None)
    command, process_deadline = calls[0]
    assert command[command.index('--deadline') + 1] == deadline.isoformat()
    assert process_deadline == deadline
    if late:
        assert command[command.index('--late-action-date') + 1] == late
    else:
        assert '--late-action-date' not in command
