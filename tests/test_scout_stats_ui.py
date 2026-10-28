"""Offline UI checks for the independently counted Scout Stats section."""
from __future__ import annotations

import gc
import tkinter as tk
from types import SimpleNamespace
import weakref

import pytest

from app.ui.scout_stats_widgets import ScoutStatsPanel


@pytest.fixture(scope="module")
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
    widget = ScoutStatsPanel(frame, tk_root)
    widget.frame.pack(fill="both", expand=True)
    reference = weakref.ref(widget)
    try:
        yield weakref.proxy(widget)
    finally:
        frame.destroy()
        del widget, frame
        gc.collect()
        assert reference() is None, "Scout Stats fixture retained its destroyed Tk view"


def forecast(identity="f1", **changes):
    row = dict(forecast_id=identity, pc="Scout", symbol="TEST", mode="live",
               reference_at="2026-10-23T16:00:00Z", issued_at="2026-10-23T16:15:10Z",
               target_open="2026-10-23T16:30:00Z", target_close="2026-10-23T16:45:00Z",
               supported_target=True, probability_not_down=.8, outcome_status="scored",
               target_not_down=True, target_close_price=101, outcome_observed_at="2026-10-23T16:46:00Z")
    row.update(changes)
    return row


def order(identity="r1", **changes):
    row = dict(request_id=identity, forecast_id="f1", symbol="TEST", side="BUY", status="FILLED",
               submission_state="ACCEPTED", quantity=3, filled_quantity=3,
               issued_at="2026-10-23T16:16:00Z", reference_at="2026-10-23T16:00:00Z",
               observed_at="2026-10-23T16:17:00Z", fills=(dict(fill_id="fill-1", quantity=3,
                   price=100.25, executed_at="2026-10-23T16:16:01Z"),))
    row.update(changes)
    return row


def snapshot(*, forecasts=(), orders=(), **changes):
    values = dict(session="2026-10-23", status="read", message="Saved Scout session loaded.", forecasts=tuple(forecasts),
                  orders=tuple(orders), warnings=(), prediction_status="read", execution_status="read")
    values.update(changes)
    return SimpleNamespace(**values)


def test_prediction_accuracy_and_execution_counts_have_independent_populations(view):
    view.set_snapshot(snapshot(forecasts=(forecast(), forecast("missing", outcome_status="missing", target_not_down=None),
        forecast("synthetic", outcome_status="synthetic"),
        forecast("during", issued_at="2026-10-23T16:31:00Z"),
        forecast("late", issued_at="2026-10-23T16:45:00Z")), orders=(order(),)))
    assert view.accuracy_label.cget("text") == "100.0%"
    assert view.accuracy_caption.cget("text") == "1 / 1 scored"
    assert view.brier_label.cget("text") == "0.040"
    assert "5 live forecasts · 3 before target open" in view.coverage_label.cget("text")
    assert "1 missing" in view.coverage_label.cget("text")
    assert "1 synthetic" in view.coverage_label.cget("text")
    assert "1 during target · 1 after close" in view.exclusions_label.cget("text")
    assert "1 saved requests" in view.order_total.cget("text")
    assert "1 filled" in view.order_counts.cget("text")
    assert len(view.company_tree.get_children()) == len(view.order_tree.get_children()) == 1
    assert "Realized P&L: Not available" in view.profit_label.cget("text")


def test_missing_prediction_data_does_not_hide_available_order_results(view):
    view.set_snapshot(snapshot(orders=(order(),), prediction_status="missing",
                               message="Prediction store is missing; journal evidence remains available."))
    assert view.accuracy_label.cget("text") == view.brier_label.cget("text") == "—"
    assert view.coverage_label.cget("text") == "Outcome coverage: unavailable"
    assert "Prediction data missing or unreadable" in view.prediction_state.cget("text")
    assert "1 filled" in view.order_counts.cget("text")
    assert len(view.order_tree.get_children()) == 1


def test_unknown_order_quantity_is_not_rendered_as_zero_and_fill_detail_is_saved(view):
    unknown = order("unknown", status="UNKNOWN", submission_state="UNKNOWN", filled_quantity=None, fills=())
    view.set_snapshot(snapshot(forecasts=(forecast(),), orders=(order(), unknown),
                               execution_status="partial", warnings=("One journal could not be read.",)))
    rows = view.order_tree.get_children()
    assert view.order_tree.item(rows[1], "values")[3] == "— / 3"
    assert "1 unknown" in view.order_counts.cget("text")
    assert "Counts cover readable records only" in view.execution_state.cget("text")
    assert "One journal could not be read" in view.warning_label.cget("text")
    view.order_tree.selection_set(rows[0])
    view._select_order()
    assert "3 @ $100.2500" in view.detail_label.cget("text")
    assert "Oct 23, 09:16:01 PDT" in view.detail_label.cget("text")
    assert "fill-1" not in view.detail_label.cget("text")
    view.order_tree.selection_set(rows[1])
    view._select_order()
    assert "No identified fill records saved" in view.detail_label.cget("text")


def test_replacement_clears_old_session_rows_and_missing_execution_is_unavailable(view):
    view.set_snapshot(snapshot(forecasts=(forecast(),), orders=(order(),)))
    view.set_snapshot(snapshot(session="2026-10-22", execution_status="missing", message="No saved execution journal."))
    assert view.company_tree.get_children() == view.order_tree.get_children() == ()
    assert view.accuracy_label.cget("text") == "—"
    assert view.accuracy_caption.cget("text") == "0 / 0 scored"
    assert view.order_total.cget("text") == "— saved requests"
    assert "Order/fill data missing or unreadable" in view.execution_state.cget("text")
    assert "2026-10-22" in view.session_label.cget("text")


def test_panel_reflows_to_keep_prediction_and_execution_readable_at_narrow_width(view):
    view._resize(SimpleNamespace(width=650))
    assert view.order_panel.grid_info()["row"] == 1
    assert view.order_panel.grid_info()["column"] == 0
    view._resize(SimpleNamespace(width=1250))
    assert view.order_panel.grid_info()["row"] == 0
    assert view.order_panel.grid_info()["column"] == 1


def test_source_identity_refreshes_and_local_scaffold_needs_no_actor(view):
    view.set_snapshot(snapshot())
    assert view.source_label.cget("text") == "Source: Local saved Scout evidence"
    view.set_snapshot(snapshot(source="Scout saved export", exported_at="2026-10-28T01:32:47Z",
                               completion_id="synthetic-first-export"))
    assert "Scout saved export" in view.source_label.cget("text")
    assert "Oct 27, 18:32:47 PDT" in view.source_label.cget("text")
    assert "synthetic-first-export" in view.source_label.cget("text")
    view.set_snapshot(snapshot(source="Scout saved export", exported_at="2026-10-28T02:00:00Z",
                               completion_id="synthetic-second-export"))
    assert "synthetic-second-export" in view.source_label.cget("text")
    assert "synthetic-first-export" not in view.source_label.cget("text")
    view.set_snapshot(snapshot())
    assert view.source_label.cget("text") == "Source: Local saved Scout evidence"
