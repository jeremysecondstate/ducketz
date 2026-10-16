"""Offline regression coverage for the two independent Stats-first segments."""

import json
from pathlib import Path

import pandas as pd
import pytest

from ml import overnight_runtime
from ml.artifacts import file_checksum, utc_timestamp
from test_overnight_supervision import _evidence_snapshot, _synthetic_gameplan_pin, run


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
    monkeypatch.setattr(overnight_runtime, "_pin_stock_gameplan", _synthetic_gameplan_pin)
    return clock


def _stages(monkeypatch, *, fail_module=None):
    calls = []

    def stage(command, **kwargs):
        calls.append(command)
        kwargs["log_path"].write_text("synthetic stage evidence", encoding="utf-8")
        return 7 if command[3] == fail_module else 0

    monkeypatch.setattr(overnight_runtime, "_run_stage", stage)
    return calls


def _feedback(root):
    path = root / "reviewed-proposal.json"
    path.write_text('{"reviewed": true}', encoding="utf-8")
    return path


def _report(directory):
    return json.loads((directory / "stage-report.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("source,history", [
    ("canonical-equity-minute-v1", []),
    ("xnas-itch-archive-v1", ["stock_target_history"]),
])
def test_stats_first_fetch_and_history_precede_stats_without_training(tmp_path, monkeypatch, source, history):
    calls = _stages(monkeypatch)
    directory = run(tmp_path, stock_only=True, independent_stock_horizons=True, stats_first=True,
                    stock_price_source=source, stop_after="gameplan_stats")
    report = _report(directory)
    assert report["stage_order"] == ["loop_a_close_fetch", *history, "gameplan_stats"]
    expected_modules = ["datafetching.orchestrate", *(["ml.stock_target_history"] if history else []),
                        "ml.gameplan_actuals_review"]
    assert [command[3] for command in calls] == expected_modules
    assert "--completed-session" in calls[-1] and "--gameplan-run" not in calls[-1]
    assert calls[-1][calls[-1].index("--action-date") + 1] == report["review_action_date"] == "2026-09-09"
    assert report["broker_orders_enabled"] is False and report["orders_placed"] == 0


def test_stats_first_requires_separate_reviewed_training_segment(tmp_path):
    with pytest.raises(ValueError, match="Stop after gameplan_stats"):
        run(tmp_path, stock_only=True, independent_stock_horizons=True, stats_first=True)
    with pytest.raises(ValueError, match="requires reviewed model feedback"):
        run(tmp_path, stock_only=True, independent_stock_horizons=True, stats_first=True,
            start_at="loop_b_directional_generation")
    assert not (tmp_path / "ml/overnight-runs").exists()


def test_stats_first_training_pins_feedback_and_omits_post_training_stats(tmp_path, monkeypatch):
    feedback = _feedback(tmp_path)
    calls = _stages(monkeypatch)
    directory = run(tmp_path, stock_only=True, independent_stock_horizons=True, stats_first=True,
                    start_at="loop_b_directional_generation", model_feedback=feedback)
    report = _report(directory)
    assert report["model_feedback"] == {"path": str(feedback.resolve()), "sha256": file_checksum(feedback)}
    assert report["stage_order"] == ["loop_b_directional_generation", "gameplan_evaluation",
                                     "gameplan_publication", "stock_enrichment_training", "gameplan_trade_planning"]
    publication_command = next(command for command in calls if command[3] == "ml.nightly_gameplan")
    assert publication_command[-2:] == ("--model-feedback", str(feedback))
    assert all(command[3] != "ml.gameplan_actuals_review" for command in calls)


def test_stats_first_resume_preserves_order_date_and_feedback_bytes(tmp_path, monkeypatch, frozen_clock):
    feedback = _feedback(tmp_path)
    _stages(monkeypatch, fail_module="ml.prediction_runtime")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        run(tmp_path, stock_only=True, independent_stock_horizons=True, stats_first=True,
            start_at="loop_b_directional_generation", stop_after="gameplan_publication", model_feedback=feedback)
    failed = Path(overnight_runtime.overnight_status(tmp_path)["run_path"])
    original = _report(failed)
    before = _evidence_snapshot(failed)
    frozen_clock["now"] = pd.Timestamp("2026-09-11T04:05:00Z")
    calls = _stages(monkeypatch)
    resumed = run(tmp_path, resume_run=failed)
    report = _report(resumed)
    assert report["review_action_date"] == original["review_action_date"] == "2026-09-09"
    assert report["deadline_at"] == original["deadline_at"]
    assert report["stage_order"] == original["stage_order"]
    assert report["stats_first"] is True and report["model_feedback"] == original["model_feedback"]
    assert calls[-1][-2:] == ("--model-feedback", str(feedback))
    assert _evidence_snapshot(failed) == before
    feedback.write_text('{"reviewed": false}', encoding="utf-8")
    calls.clear()
    with pytest.raises(ValueError, match="feedback changed"):
        run(tmp_path, resume_run=failed)
    assert calls == []
    assert _evidence_snapshot(failed) == before


def test_feedback_mutation_between_stages_stops_publication(tmp_path, monkeypatch):
    feedback = _feedback(tmp_path)
    calls = []

    def mutate(command, **kwargs):
        calls.append(command[3])
        feedback.write_text('{"reviewed": false}', encoding="utf-8")
        return 0

    monkeypatch.setattr(overnight_runtime, "_run_stage", mutate)
    with pytest.raises(ValueError, match="feedback changed before training"):
        run(tmp_path, stock_only=True, independent_stock_horizons=True, stats_first=True,
            start_at="loop_b_directional_generation", stop_after="gameplan_publication", model_feedback=feedback)
    assert "ml.nightly_gameplan" not in calls
    status = overnight_runtime.overnight_status(tmp_path)
    assert status["status"] == "FAILED" and status["failed_stage"] == "gameplan_publication"
    assert status["receipt_present"] is True


def test_failed_stats_resume_does_not_replay_completed_fetch_or_history(tmp_path, monkeypatch, frozen_clock):
    first_calls = _stages(monkeypatch, fail_module="ml.gameplan_actuals_review")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        run(tmp_path, stock_only=True, independent_stock_horizons=True, stats_first=True,
            stock_price_source="xnas-itch-archive-v1", stop_after="gameplan_stats")
    assert [command[3] for command in first_calls] == [
        "datafetching.orchestrate", "ml.stock_target_history", "ml.gameplan_actuals_review",
    ]
    failed = Path(overnight_runtime.overnight_status(tmp_path)["run_path"])
    original = _report(failed)
    before = _evidence_snapshot(failed)
    frozen_clock["now"] = pd.Timestamp("2026-09-11T04:05:00Z")
    calls = _stages(monkeypatch)
    resumed = run(tmp_path, resume_run=failed)
    report = _report(resumed)
    assert report["stage_order"] == ["gameplan_stats"]
    assert report["completed_stages_from_previous_attempt"] == ["loop_a_close_fetch", "stock_target_history"]
    assert report["review_action_date"] == original["review_action_date"] == "2026-09-09"
    assert report["deadline_at"] == original["deadline_at"]
    assert [command[3] for command in calls] == ["ml.gameplan_actuals_review"]
    assert calls[0][calls[0].index("--action-date") + 1] == "2026-09-09"
    assert report["model_feedback"] is None and report["status"] == "COMPLETE"
    assert _evidence_snapshot(failed) == before


@pytest.mark.parametrize("changed,message", [
    ("order", "Stats ordering"),
    ("date", "reviewed action date"),
    ("feedback_path", "reviewed model feedback"),
])
def test_stats_first_resume_rejects_changed_review_identity(tmp_path, monkeypatch, changed, message):
    feedback = _feedback(tmp_path)
    _stages(monkeypatch, fail_module="ml.prediction_runtime")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        run(tmp_path, stock_only=True, independent_stock_horizons=True, stats_first=True,
            start_at="loop_b_directional_generation", stop_after="gameplan_publication", model_feedback=feedback)
    failed = Path(overnight_runtime.overnight_status(tmp_path)["run_path"])
    before = _evidence_snapshot(failed)
    alternate = tmp_path / "same-bytes-different-proposal.json"
    alternate.write_bytes(feedback.read_bytes())
    arguments = {"order": {"stats_first": False}, "date": {"review_action_date": "2026-09-08"},
                 "feedback_path": {"model_feedback": alternate}}[changed]
    calls = _stages(monkeypatch)
    with pytest.raises(ValueError, match=message):
        run(tmp_path, resume_run=failed, **arguments)
    assert calls == []
    assert _evidence_snapshot(failed) == before


def test_legacy_resume_without_stats_fields_keeps_saved_publication_boundary(tmp_path, monkeypatch):
    _stages(monkeypatch, fail_module="ml.nightly_gameplan")
    with pytest.raises(RuntimeError, match="exited with code 7"):
        run(tmp_path, stock_only=True, independent_stock_horizons=True,
            start_at="gameplan_publication", stop_after="gameplan_publication")
    failed = Path(overnight_runtime.overnight_status(tmp_path)["run_path"])
    report = _report(failed)
    for key in ("stats_first", "review_action_date", "model_feedback"):
        report.pop(key, None)
    report_path = failed / "stage-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    receipt_path = failed / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["stage_report_checksum_sha256"] = file_checksum(report_path)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    before = _evidence_snapshot(failed)
    calls = _stages(monkeypatch)
    resumed = run(tmp_path, resume_run=failed)
    resumed_report = _report(resumed)
    assert resumed_report["stage_order"] == ["gameplan_publication"]
    assert resumed_report["stats_first"] is False
    assert resumed_report["review_action_date"] is None and resumed_report["model_feedback"] is None
    assert len(calls) == 1 and calls[0][3] == "ml.nightly_gameplan"
    assert "--model-feedback" not in calls[0]
    assert _evidence_snapshot(failed) == before


def test_stats_first_cli_passes_review_parameters(tmp_path, monkeypatch):
    feedback = _feedback(tmp_path)
    calls = []
    monkeypatch.setattr(overnight_runtime, "run_overnight_pipeline",
                        lambda root, **kwargs: calls.append(kwargs) or tmp_path)
    assert overnight_runtime.main([
        "--datastore", str(tmp_path), "--stock-only", "--independent-stock-horizons", "--stats-first",
        "--start-at", "loop_b_directional_generation", "--review-action-date", "2026-09-09",
        "--model-feedback", str(feedback),
    ]) == 0
    assert len(calls) == 1
    assert calls[0]["stats_first"] is True and calls[0]["review_action_date"] == "2026-09-09"
    assert calls[0]["model_feedback"] == feedback
