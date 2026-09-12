from __future__ import annotations

import json

import pytest

from app.ui.gameplan_stats_data import GameplanStatsError, load_gameplan_stats, review_sessions
from gameplan_stats_fixture import forecast, write_review


def test_separate_direction_probability_and_incomplete_populations(tmp_path):
    write_review(tmp_path)
    review = load_gameplan_stats(tmp_path)
    metrics = review.metrics()
    assert (metrics.total, metrics.evaluated, metrics.pending, metrics.awaiting_data) == (7, 5, 1, 1)
    assert (metrics.correct, metrics.scored, metrics.neutral) == (2, 4, 1)
    assert metrics.accuracy == .5
    assert metrics.bullish_accuracy == 0
    assert metrics.bearish_accuracy == pytest.approx(2/3)
    assert metrics.brier == pytest.approx((.04 + .64 + .25 + .64 + .01)/5)
    # The frozen .10% probability target differs from positive/negative direction.
    rows = [forecast(change=.0005, direction="BULLISH", probability=.9)]
    write_review(tmp_path, rows, version="02")
    other = load_gameplan_stats(tmp_path).metrics()
    assert other.accuracy == 1 and other.brier == pytest.approx(.81)


def test_horizon_filter_does_not_relabel_the_hourly_grid(tmp_path):
    write_review(tmp_path)
    review = load_gameplan_stats(tmp_path)
    metrics = review.metrics("4h")
    assert (metrics.correct, metrics.scored, metrics.total) == (0, 1, 1)
    assert review.metrics("1w").brier is None
    cells = review.hourly("AAPL")
    assert len(cells) == 13
    assert [c.state if c else "not_saved" for c in cells[:6]] == [
        "correct", "incorrect", "neutral", "awaiting_data", "pending", "not_saved"]
    assert cells[0].route == "1h@04:00"


def test_no_bullish_calls_is_undefined_not_zero(tmp_path):
    write_review(tmp_path, [forecast()])
    metrics = load_gameplan_stats(tmp_path).metrics()
    assert metrics.bullish_scored == 0 and metrics.bullish_accuracy is None
    assert metrics.bearish_accuracy == 1


def test_partial_or_pending_evidence_is_not_scored_from_present_fields(tmp_path):
    pending = forecast(status="PENDING_MATURITY")
    pending.update(direction_correct=True, model_brier_score=0, actual_return=-.01, model_observed_target=0)
    missing = forecast(hour=5, status="MATURE_AWAITING_DATA")
    missing.update(direction_correct=False, model_brier_score=1, actual_return=.1, model_observed_target=1)
    write_review(tmp_path, [pending, missing])
    review = load_gameplan_stats(tmp_path)
    assert review.metrics().scored == 0 and review.metrics().brier is None
    assert [cell.state for cell in review.hourly("AAPL")[:2]] == ["pending", "awaiting_data"]


def test_dates_are_pinned_and_refresh_uses_new_publication(tmp_path):
    write_review(tmp_path, [forecast(session="2026-09-10")], session="2026-09-10")
    newer = write_review(tmp_path, [forecast(change=.01)])
    assert review_sessions(tmp_path) == ("2026-09-11", "2026-09-10")
    assert load_gameplan_stats(tmp_path).run_directory == newer
    assert load_gameplan_stats(tmp_path, "2026-09-10").metrics().accuracy == 1
    write_review(tmp_path, [forecast()], version="02")
    assert load_gameplan_stats(tmp_path, "2026-09-11").metrics().accuracy == 1
    with pytest.raises(GameplanStatsError, match="No verified results"):
        load_gameplan_stats(tmp_path, "2026-09-08")


@pytest.mark.parametrize("filename", ["forecast-results.parquet", "report.json", "Gameplan-results.md", "receipt.json"])
def test_corrupt_published_output_is_never_displayed(tmp_path, filename):
    run = write_review(tmp_path)
    path = run / filename
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(GameplanStatsError):
        load_gameplan_stats(tmp_path)


def test_invalid_pointer_does_not_escape_datastore(tmp_path):
    write_review(tmp_path)
    pointer = tmp_path / "ml/gameplan-actuals-review-latest/run.json"
    value = json.loads(pointer.read_text())
    value["current"]["run_path"] = "../../outside"
    pointer.write_text(json.dumps(value))
    with pytest.raises(GameplanStatsError, match="outside"):
        load_gameplan_stats(tmp_path)


def test_duplicate_forecast_and_inconsistent_saved_score_rejected(tmp_path):
    write_review(tmp_path, [forecast(), forecast()])
    with pytest.raises(GameplanStatsError, match="identities"):
        load_gameplan_stats(tmp_path)
    row = forecast()
    row["model_brier_score"] = .99
    write_review(tmp_path, [row], version="02")
    with pytest.raises(GameplanStatsError, match="probability error"):
        load_gameplan_stats(tmp_path)


def test_research_gap_is_in_summary_but_not_an_execution_grid_cell(tmp_path):
    rows = [forecast(role="OPENING_GAP_RESEARCH", direction="NO_EDGE", probability=.5), forecast()]
    unpromoted = forecast("MU")
    unpromoted["model_status"] = "RESEARCH_ONLY"
    write_review(tmp_path, [*rows, unpromoted])
    review = load_gameplan_stats(tmp_path)
    assert review.metrics().evaluated == 2
    assert review.metrics().neutral == 1
    assert review.hourly("AAPL")[0].role == "EXECUTION"
    assert review.excluded_forecasts == 1 and review.symbols == ("AAPL",)


def test_missing_and_empty_reports_have_no_invented_metrics(tmp_path):
    assert review_sessions(tmp_path) == ()
    with pytest.raises(GameplanStatsError, match="not available"):
        load_gameplan_stats(tmp_path)
    write_review(tmp_path, [])
    review = load_gameplan_stats(tmp_path)
    assert review.metrics().accuracy is None and review.metrics().total == 0
