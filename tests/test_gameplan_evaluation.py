from datetime import date
import json

import pandas as pd
import pytest

from ml.artifacts import write_manifest
from ml.gameplan_evaluation import evaluate_saved_gameplans, read_evaluation_history
from ml.nightly_gameplan import _intraday_outcomes, _publish_gameplan


def saved_plan(root, name, action_date, *, end="2026-09-10T20:00:00Z"):
    run = root / "ml/nightly-gameplan-runs" / name
    run.mkdir(parents=True)
    frame = pd.DataFrame([{
        "id": f"{action_date}:AAPL:1d@D+5", "symbol": "AAPL", "route": "1d@D+5",
        "target_window_start": pd.Timestamp("2026-09-10T13:30:00Z"),
        "target_window_end": pd.Timestamp(end), "calibrated_probability": 0.7,
        "model_group": "1d", "model_status": "PROMOTED",
    }])
    frame.to_parquet(run / "forecasts.parquet", index=False)
    write_manifest(run, run_timestamp="2026-09-04T10:00:00Z", input_files=(),
                   output_files=("forecasts.parquet",), configuration={"action_date": action_date}, datastore_root=root)
    _publish_gameplan(root, run=run, action_date=date.fromisoformat(action_date),
                      published_at=pd.Timestamp("2026-09-04T10:01:00Z"), source_loop_b="test", source_strategy="test")
    return run


def outcomes():
    return {"1d": pd.DataFrame([{
        "symbol": "AAPL", "route": "1d@D+5", "target_window_start": pd.Timestamp("2026-09-10T13:30:00Z"),
        "target_window_end": pd.Timestamp("2026-09-10T20:00:00Z"), "target": 1, "observed_return": 0.02,
    }])}


def test_pending_forecast_survives_pointer_advance_and_scores_once(tmp_path):
    first = saved_plan(tmp_path, "first", "2026-09-04")
    initial = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-05T16:00:00Z")
    assert initial.summary["review_week"]["forecasts"] == 1
    assert initial.evaluations.iloc[0].evaluation_status == "PENDING_MATURITY"
    saved_plan(tmp_path, "second", "2026-09-08")
    # A new current plan must not hide the older plan when its D+5 forecast matures.
    result = evaluate_saved_gameplans(tmp_path, observed_groups=outcomes(), evaluated_at="2026-09-11T01:00:00Z")
    assert len(result.evaluations) == 2
    old = result.evaluations.loc[result.evaluations.source_gameplan_run.eq(first.relative_to(tmp_path).as_posix())].iloc[0]
    assert old.evaluation_status == "EVALUATED"
    assert old.brier_score == pytest.approx(0.09)
    again = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-12T16:00:00Z")
    pd.testing.assert_frame_equal(result.evaluations, again.evaluations)


def test_mature_missing_data_is_visible_and_later_retried(tmp_path):
    saved_plan(tmp_path, "first", "2026-09-04")
    missing = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-11T01:00:00Z")
    assert missing.evaluations.iloc[0].evaluation_status == "MATURE_AWAITING_DATA"
    assert missing.summary["all_saved_gameplans"]["mature_awaiting_data"] == 1
    result = evaluate_saved_gameplans(tmp_path, observed_groups=outcomes(), evaluated_at="2026-09-12T16:00:00Z")
    assert result.evaluations.iloc[0].evaluation_status == "EVALUATED"


def test_only_september_four_onward_and_only_receipted_plans(tmp_path):
    saved_plan(tmp_path, "old", "2026-09-03")
    saved_plan(tmp_path, "first", "2026-09-04")
    incomplete = tmp_path / "ml/nightly-gameplan-runs/incomplete"
    incomplete.mkdir()
    (incomplete / "forecasts.parquet").write_text("failed training output")
    result = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-05T16:00:00Z")
    assert result.evaluations.action_date.tolist() == ["2026-09-04"]


def test_saved_forecast_corruption_fails_without_losing_history(tmp_path):
    first = saved_plan(tmp_path, "first", "2026-09-04")
    result = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-05T16:00:00Z")
    saved_plan(tmp_path, "second", "2026-09-08")
    with (first / "forecasts.parquet").open("ab") as output:
        output.write(b"corrupt")
    with pytest.raises((ValueError, RuntimeError)):
        evaluate_saved_gameplans(tmp_path, observed_groups=outcomes(), evaluated_at="2026-09-11T01:00:00Z")
    assert read_evaluation_history(tmp_path).run_directory == result.run_directory


def test_evaluation_history_is_checksum_verified(tmp_path):
    saved_plan(tmp_path, "first", "2026-09-04")
    result = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-05T16:00:00Z")
    (result.run_directory / "summary.json").write_text(json.dumps({"evaluated": 10000}))
    with pytest.raises((ValueError, RuntimeError)):
        read_evaluation_history(tmp_path)


def test_repeated_editions_have_separate_forecast_identities(tmp_path):
    saved_plan(tmp_path, "first", "2026-09-04")
    saved_plan(tmp_path, "second", "2026-09-04")
    result = evaluate_saved_gameplans(tmp_path, observed_groups={}, evaluated_at="2026-09-05T16:00:00Z")
    assert result.evaluations.id.nunique() == 2


def test_future_outcomes_are_not_scored_early(tmp_path):
    saved_plan(tmp_path, "first", "2026-09-04")
    result = evaluate_saved_gameplans(tmp_path, observed_groups=outcomes(), evaluated_at="2026-09-05T16:00:00Z")
    assert result.evaluations.iloc[0].evaluation_status == "PENDING_MATURITY"


def test_four_hour_evaluation_waits_for_an_actual_window_end_price(tmp_path):
    run = tmp_path / "ml/nightly-gameplan-runs/four-hour"
    run.mkdir(parents=True)
    forecasts = pd.DataFrame([{
        "id": "2026-09-04:AAPL:4h@16:00", "symbol": "AAPL", "route": "4h@16:00",
        "target_window_start": pd.Timestamp("2026-09-04T19:00:00Z"),
        "target_window_end": pd.Timestamp("2026-09-04T23:00:00Z"),
        "calibrated_probability": 0.5, "model_group": "4h", "model_status": "PROMOTED",
    }])
    forecasts.to_parquet(run / "forecasts.parquet", index=False)
    write_manifest(run, run_timestamp="2026-09-04T10:00:00Z", input_files=(),
                   output_files=("forecasts.parquet",), configuration={"action_date": "2026-09-04"}, datastore_root=tmp_path)
    _publish_gameplan(tmp_path, run=run, action_date=date(2026, 9, 4),
                      published_at=pd.Timestamp("2026-09-04T10:01:00Z"), source_loop_b="test", source_strategy="test")
    sources = pd.DataFrame([{
        "symbol": "AAPL", "action_date": date(2026, 9, 4),
        "decision_timestamp": pd.Timestamp("2026-09-04T00:05:00Z"),
    }])
    bars = pd.DataFrame({
        "symbol": "AAPL", "open": 100.0, "close": 101.0,
        "timestamp": pd.to_datetime(["2026-09-04T19:00:00Z", "2026-09-04T19:59:00Z"]),
    })
    _, incomplete = _intraday_outcomes(sources=sources, feature_columns=(), minute_bars=bars)
    waiting = evaluate_saved_gameplans(tmp_path, observed_groups={"4h": incomplete}, evaluated_at="2026-09-05T01:00:00Z")
    assert waiting.evaluations.iloc[0].evaluation_status == "MATURE_AWAITING_DATA"
    bars.loc[1, "timestamp"] = pd.Timestamp("2026-09-04T22:59:00Z")
    _, complete = _intraday_outcomes(sources=sources, feature_columns=(), minute_bars=bars)
    scored = evaluate_saved_gameplans(tmp_path, observed_groups={"4h": complete}, evaluated_at="2026-09-05T02:00:00Z")
    assert scored.evaluations.iloc[0].evaluation_status == "EVALUATED"
    assert scored.evaluations.iloc[0].brier_score == 0.25
