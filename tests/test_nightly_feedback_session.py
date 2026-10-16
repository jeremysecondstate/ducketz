"""Offline integration checks for native feedback and reported session identity."""

import json
from pathlib import Path

import pandas as pd
import pytest

from ml import overnight_runtime
from ml.gameplan_actuals_review import publish_completed_session_review
from ml.gameplan_model_feedback import load_feedback_review
from test_nightly_stats_segments import _feedback, _report, _stages, frozen_clock
from test_overnight_supervision import _evidence_snapshot, run


def _training(root, feedback, **kwargs):
    options = dict(stock_only=True, independent_stock_horizons=True, stats_first=True,
                   start_at="loop_b_directional_generation", stop_after="gameplan_publication",
                   model_feedback=feedback)
    options.update(kwargs)
    return run(root, **options)


@pytest.mark.parametrize("start_at", ["loop_b_directional_generation", "gameplan_publication"])
@pytest.mark.parametrize("review_date", [None, "2026-09-09"])
def test_wrong_stats_session_rejected_before_any_stage(tmp_path, monkeypatch, start_at, review_date):
    feedback = _feedback(tmp_path, action_date="2026-09-08")
    assert load_feedback_review(tmp_path, feedback)["stats"]["session"] == "2026-09-08"
    before = _evidence_snapshot(feedback.parent)
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError, match="feedback Stats session"):
        _training(tmp_path, feedback, start_at=start_at, review_action_date=review_date)
    assert calls == []
    assert not (tmp_path / "ml/overnight-runs").exists()
    assert _evidence_snapshot(feedback.parent) == before


@pytest.mark.parametrize("start_at", ["loop_b_directional_generation", "gameplan_publication"])
def test_matching_explicit_historical_session_is_preserved(tmp_path, monkeypatch, start_at):
    feedback = _feedback(tmp_path, action_date="2026-09-08")
    calls = _stages(monkeypatch)
    directory = _training(tmp_path, feedback, start_at=start_at, review_action_date="2026-09-08")
    report = _report(directory)
    assert report["status"] == "COMPLETE"
    assert report["review_action_date"] == load_feedback_review(tmp_path, feedback)["stats"]["session"]
    assert calls[-1][-2:] == ("--model-feedback", str(feedback))


@pytest.mark.parametrize("invalid", ["future", "target", "diagnostics"])
def test_unverified_feedback_rejected_before_any_stage(tmp_path, monkeypatch, invalid):
    feedback = _feedback(
        tmp_path, now="2026-09-10T04:06:00Z" if invalid == "future" else "2026-09-10T04:05:00Z",
        target="cost-adjusted-positive-return-v1" if invalid == "target" else "raw-price-direction-v1",
    )
    if invalid == "diagnostics":
        diagnostic = feedback.parent / "diagnostics.json"
        diagnostic.write_bytes(diagnostic.read_bytes() + b" ")
    calls = _stages(monkeypatch)
    message = {"future": "before training", "target": "probability targets",
               "diagnostics": "diagnostics changed"}[invalid]
    with pytest.raises(ValueError, match=message):
        _training(tmp_path, feedback)
    assert calls == []
    assert not (tmp_path / "ml/overnight-runs").exists()


def test_training_retry_rejects_replaced_stats_without_replaying(tmp_path, monkeypatch, frozen_clock):
    feedback = _feedback(tmp_path)
    _stages(monkeypatch, fail_module="ml.prediction_runtime")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        _training(tmp_path, feedback)
    failed = Path(overnight_runtime.overnight_status(tmp_path)["run_path"])
    before = _evidence_snapshot(failed)
    frozen_clock["now"] = pd.Timestamp("2026-09-11T04:05:00Z")
    _replacement_stats(tmp_path, frozen_clock["now"])
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError, match="implementation changed"):
        run(tmp_path, resume_run=failed)
    assert calls == []
    assert _evidence_snapshot(failed) == before


def _replacement_stats(root, now):
    return publish_completed_session_review(
        root, action_date="2026-09-10", clock=lambda: now,
        price_loader=lambda *args, **kwargs: pytest.fail("No forecast requires provider data"),
    )


def test_tail_retry_keeps_frozen_feedback_after_newer_display(tmp_path, monkeypatch, frozen_clock):
    feedback = _feedback(tmp_path)
    _stages(monkeypatch, fail_module="ml.gameplan_trade_planning")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        _training(tmp_path, feedback, stop_after="gameplan_trade_planning")
    failed = Path(overnight_runtime.overnight_status(tmp_path)["run_path"])
    original = _report(failed)
    before = _evidence_snapshot(failed)
    frozen_clock["now"] = pd.Timestamp("2026-09-11T04:05:00Z")
    _replacement_stats(tmp_path, frozen_clock["now"])
    with pytest.raises(ValueError, match="implementation changed"):
        load_feedback_review(tmp_path, feedback)
    calls = _stages(monkeypatch)
    resumed = run(tmp_path, resume_run=failed)
    report = _report(resumed)
    assert report["stage_order"] == ["gameplan_trade_planning"]
    assert [command[3] for command in calls] == ["ml.gameplan_trade_planning"]
    assert report["review_action_date"] == original["review_action_date"] == "2026-09-09"
    assert report["model_feedback"] == original["model_feedback"]
    assert report["deadline_at"] == original["deadline_at"]
    assert _evidence_snapshot(failed) == before
