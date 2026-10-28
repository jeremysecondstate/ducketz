"""Offline native UI assertions for saved Scout horizon prediction history."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import gc
from pathlib import Path
from types import SimpleNamespace
import tkinter as tk
import weakref

import pytest

from app.ui.gameplan_stats_data import GameplanStatsReview, PredictionOutcome
from app.ui.scout_horizon_stats_widgets import ScoutHorizonStatsPanel
from ml.gameplan_probability_target import LEGACY_COST_TARGET, RAW_DIRECTION_TARGET


def review():
    start = datetime(2026, 10, 23, 16, tzinfo=timezone.utc)
    def row(identity, horizon, *, correct=True, brier=.04, direction="BULLISH", status="EVALUATED"):
        end = start + timedelta(hours={"1h": 1, "4h": 4, "1d": 24, "1w": 168}[horizon])
        observed = status == "EVALUATED"
        return PredictionOutcome(identity, "TEST", horizon, horizon + "@09:00", "EXECUTION", direction,
            status, .8, correct if observed else None, brier if observed else None,
            .01 if observed else None, .001, start, end, start if observed else None,
            end if observed else None, direction_policy_version="fixture-saved-policy")
    rows = (row("one-hour", "1h"), row("four-hour", "4h", correct=False, brier=.64, direction="BEARISH"),
        row("one-day", "1d", status="PENDING_MATURITY"),
        row("one-week", "1w", status="MATURE_AWAITING_DATA"),
        row("neutral", "1h", correct=None, brier=.25, direction="NO_EDGE"))
    return GameplanStatsReview("2026-10-23", start + timedelta(days=2), start + timedelta(days=1),
        Path("invented-fixture"), Path("invented-fixture/report.html"), rows)


def snapshot(saved=None, **changes):
    values = dict(session="2026-10-23", review=saved if saved is not None else review(),
        exported_at="2026-10-28T01:32:47Z", completion_id="invented-scout-history-export",
        package_sha256="a" * 64, revision_count=3, coverage_status=None, status="read",
        message="Saved Scout Gameplan forecast snapshot.")
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture(scope="session")
def tk_root():
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def view(tk_root):
    frame = tk.Frame(tk_root)
    frame.pack(fill="both", expand=True)
    widget = ScoutHorizonStatsPanel(frame, tk_root)
    widget.frame.pack(fill="both", expand=True)
    reference = weakref.ref(widget)
    try:
        yield weakref.proxy(widget)
    finally:
        frame.destroy()
        del widget, frame
        gc.collect()
        assert reference() is None, "Scout horizon fixture retained destroyed Tk view"


def test_saved_metrics_source_and_revisions_are_visible(view):
    saved = snapshot(replace(review(), excluded_forecasts=1))
    view.set_snapshot(saved)
    assert view.heading.cget("text") == "Scout Gameplan forecast Stats"
    assert view.accuracy_label.cget("text") == "50.0%"
    assert view.accuracy_caption.cget("text") == "1 / 2 scored"
    assert view.brier_label.cget("text") == "0.310"
    assert view.bullish_label.cget("text") == "100.0%"
    assert view.bearish_label.cget("text") == "0.0%"
    assert "5 promoted forecasts · 3 evaluated · 1 pending maturity · 1 missing outcomes" in view.coverage_label.cget("text")
    assert "Excluded non-promoted research forecasts: 1" in view.coverage_label.cget("text")
    assert "Scout saved Gameplan history" in view.source_label.cget("text")
    assert "Session 2026-10-23" in view.source_label.cget("text")
    assert "Exported 2026-10-27 18:32:47 PDT" in view.source_label.cget("text")
    assert saved.completion_id in view.source_label.cget("text")
    assert "Reviewed" in view.source_label.cget("text") and "Outcomes through" in view.source_label.cget("text")
    assert "Saved revisions: 3" in view.revision_label.cget("text")
    assert saved.package_sha256 in view.revision_label.cget("text")
    assert len(view.symbol_tree.get_children()) == 1
    assert len(view.forecast_tree.get_children()) == 5
    assert view.profit_label.cget("text") == "Forecast scores do not measure trading profit. Realized P&L: Not available."


def test_horizon_filter_preserves_saved_probabilities_and_scoring(view):
    saved = snapshot()
    view.set_snapshot(saved)
    view.horizon.set("4 hours")
    view.render()
    assert view.snapshot is saved
    assert view.selected_horizon == "4h"
    assert view.accuracy_label.cget("text") == "0.0%"
    assert view.brier_label.cget("text") == "0.640"
    assert view.accuracy_caption.cget("text") == "0 / 1 scored"
    values = view.forecast_tree.item(view.forecast_tree.get_children()[0], "values")
    assert values[2:4] == ("80.0000%", "BEARISH")
    assert len(view.forecast_tree.get_children()) == 1
    view.horizon.set("1 day")
    view.render()
    assert view.accuracy_label.cget("text") == view.brier_label.cget("text") == "—"
    assert view.forecast_tree.item(view.forecast_tree.get_children()[0], "values")[6] == "Pending maturity"


def test_saved_research_only_population_retains_exclusions_without_a_score(view):
    saved = replace(review(), outcomes=(), excluded_forecasts=2, coverage_status="SAVED_FORECASTS")
    view.set_snapshot(snapshot(saved, coverage_status=saved.coverage_status))
    assert "No promoted Gameplan forecasts" in view.state_label.cget("text")
    assert "No saved Gameplan forecasts" not in view.state_label.cget("text")
    assert "Excluded non-promoted research forecasts: 2" in view.coverage_label.cget("text")
    assert view.accuracy_label.cget("text") == view.brier_label.cget("text") == "—"
    assert view.symbol_tree.get_children() == view.forecast_tree.get_children() == ()


def test_detail_preserves_original_cost_target_and_observation_times(view):
    view.set_snapshot(snapshot())
    view.forecast_tree.selection_set("forecast-0")
    view._select_forecast()
    text = view.detail_label.cget("text")
    assert "Saved probability: 0.8 (80.0000%)" in text
    assert LEGACY_COST_TARGET in text
    assert "Saved cost threshold: 0.1000% · Positive-class return threshold: 0.1000%" in text
    assert "Actual start observed: 2026-10-23 09:00:00 PDT" in text
    assert "Actual end observed: 2026-10-23 10:00:00 PDT" in text
    assert "Outcome cutoff:" in text and "Reviewed:" in text
    assert "fixture-saved-policy" in text


def test_raw_price_contract_uses_original_zero_target_without_rescoring(view):
    saved = review()
    original = replace(saved.outcomes[0], probability_target_contract=RAW_DIRECTION_TARGET,
        target_cost=.003, brier=.123)
    saved = replace(saved, probability_target_contract=RAW_DIRECTION_TARGET, outcomes=(original,))
    view.set_snapshot(snapshot(saved))
    assert view.brier_label.cget("text") == "0.123"
    assert RAW_DIRECTION_TARGET in view.contract_label.cget("text")
    view.forecast_tree.selection_set("forecast-0")
    view._select_forecast()
    assert "raw price return strictly above 0%" in view.detail_label.cget("text")
    assert "Saved cost threshold: 0.3000% · Positive-class return threshold: 0.0000%" in view.detail_label.cget("text")


def test_no_saved_forecasts_and_missing_source_never_display_zero_scores(view):
    no_saved = replace(review(), outcomes=(), coverage_status="NO_SAVED_INDEPENDENT_GAMEPLAN")
    view.set_snapshot(snapshot(no_saved, coverage_status=no_saved.coverage_status))
    assert "No saved Gameplan forecasts" in view.state_label.cget("text")
    assert view.accuracy_label.cget("text") == view.brier_label.cget("text") == "—"
    assert view.symbol_tree.get_children() == view.forecast_tree.get_children() == ()
    assert view.accuracy_caption.cget("text") == "No scored forecast population"
    view.set_snapshot(None)
    assert "unavailable" in view.source_label.cget("text")
    assert view.accuracy_label.cget("text") == "—"
    error = snapshot(review=None, status="unreadable", message="Saved Scout source could not be verified.")
    view.set_snapshot(error)
    assert "could not be verified" in view.state_label.cget("text")
    assert view.accuracy_label.cget("text") == view.brier_label.cget("text") == "—"


def test_replacement_clears_previous_forecasts_and_narrow_layout_reflows(view):
    view.set_snapshot(snapshot())
    view.forecast_tree.selection_set("forecast-0")
    view._select_forecast()
    view.set_snapshot(None)
    assert view._forecasts == {}
    assert "Select a saved forecast" in view.detail_label.cget("text")
    view._resize(SimpleNamespace(width=600))
    assert view.bullish_label.grid_info()["row"] == 3
    assert view.bullish_label.grid_info()["column"] == 0
    assert view.source_label.cget("wraplength") == 555
    view._resize(SimpleNamespace(width=1200))
    assert view.bullish_label.grid_info()["row"] == 0
    assert view.bullish_label.grid_info()["column"] == 2


def test_details_retain_saved_policy_description_and_version(view):
    original = review()
    row = replace(original.outcomes[0], direction_policy_version="stock-direction-5001-4999-v3",
                  direction_up_threshold=.61, direction_down_threshold=.39)
    view.set_snapshot(snapshot(replace(original, outcomes=(row,))))
    item = view.forecast_tree.get_children()[0]
    view.forecast_tree.selection_set(item)
    view._select_forecast()
    details = view.detail_label.cget("text")
    assert row.direction_policy_description in details
    assert "stock-direction-5001-4999-v3" in details
    assert "61.00%" in details and "39.00%" in details
    assert repr(row.probability) in details
