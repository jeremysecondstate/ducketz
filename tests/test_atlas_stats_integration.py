"""Offline checks that Atlas data cannot change Gameplan horizon populations."""
from __future__ import annotations

import gc
import time
import tkinter as tk
import weakref
from tkinter import ttk
from types import SimpleNamespace

import pytest

import app.ui.gameplan_stats as stats_ui
from app.ui.gameplan_stats_data import load_gameplan_stats
from app.ui.atlas_stats_data import AtlasStatsSnapshot
from gameplan_stats_fixture import write_review


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
def view(tmp_path, tk_root):
    root = tk_root
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    tab = stats_ui.GameplanStatsTab(root, frame, datastore_root=tmp_path, auto_load=False)
    write_review(tmp_path)
    tab.set_review(load_gameplan_stats(tmp_path))
    reference = weakref.ref(tab)
    try:
        yield weakref.proxy(tab)
    finally:
        frame.destroy()
        del tab, frame, root
        gc.collect()
        assert reference() is None, "Stats integration fixture retained its destroyed Tk view"


def finish(view):
    deadline = time.monotonic() + 5
    while view._loading and time.monotonic() < deadline:
        view.root.update()
        time.sleep(.01)
    assert not view._loading


def snapshot(session="2026-09-15"):
    return AtlasStatsSnapshot(session=session, status="available", message="Saved Atlas fixture",
        prediction_status="read", execution_status="read", forecasts=({
            "forecast_id": "atlas-fixture", "symbol": "TEST", "supported_target": True,
            "issued_at": f"{session}T16:15:00Z", "target_open": f"{session}T16:30:00Z",
            "target_close": f"{session}T16:45:00Z", "probability_not_down": .8,
            "outcome_status": "scored", "target_not_down": True,
        },))


def test_atlas_only_session_remains_selectable_without_gameplan_review(view, monkeypatch):
    saved = snapshot()
    monkeypatch.setattr(stats_ui, "atlas_stats_sessions", lambda *_: (saved.session,))
    monkeypatch.setattr(stats_ui, "load_atlas_stats", lambda *_: saved)
    view.refresh()
    finish(view)
    assert view.session.get() == saved.session
    assert view.atlas.snapshot is saved
    assert view.atlas.accuracy_label.cget("text") == "100.0%"
    assert view.review is None
    assert all(value.get() == "—" for value in view.values.values())
    assert "2026-09-11" in view.date_box.cget("values")
    assert saved.session in view.date_box.cget("values")


def test_horizon_filter_does_not_recompute_atlas_population(view):
    saved = snapshot(view.session.get())
    view.atlas.set_snapshot(saved)
    caption = view.atlas.accuracy_caption.cget("text")
    view.horizon.set("4 hours")
    view.render()
    assert view.values["accuracy"].get() == "0.0%"
    assert view.atlas.snapshot is saved
    assert view.atlas.accuracy_caption.cget("text") == caption
    assert view.atlas.accuracy_label.cget("text") == "100.0%"


def test_atlas_reader_failure_preserves_gameplan_metrics(view, monkeypatch):
    def fail(*_):
        raise OSError("Offline unreadable fixture")
    monkeypatch.setattr(stats_ui, "load_atlas_stats", fail)
    view.refresh()
    finish(view)
    assert view.values["accuracy"].get() == "50.0%"
    assert view.atlas.snapshot.prediction_status == "unreadable"
    assert view.atlas.accuracy_label.cget("text") == "—"
    assert "could not be read" in view.atlas.state_label.cget("text")


def test_session_change_clears_old_atlas_and_ignores_obsolete_reply(view, monkeypatch):
    view.atlas.set_snapshot(snapshot(view.session.get()))
    monkeypatch.setattr(stats_ui.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    view.session.set("2026-09-15")
    view._date_changed()
    stale_request = view._request_id
    assert view.atlas.snapshot is None
    view.session.set("2026-09-16")
    view._date_changed()
    saved = snapshot("2026-09-16")
    view._messages.put((view._request_id, (saved.session,), None, ValueError("No Gameplan"), None, None, saved))
    view._messages.put((stale_request, ("2026-09-15",), None, ValueError("No Gameplan"), None, None, snapshot()))
    finish(view)
    assert view.session.get() == saved.session
    assert view.atlas.snapshot is saved


def test_pinned_session_is_passed_to_atlas_reader(view, monkeypatch):
    requested = []
    monkeypatch.setattr(stats_ui, "atlas_stats_sessions", lambda *_: ("2026-09-15", "2026-09-11"))
    def load(_root, session):
        requested.append(session)
        return snapshot(session)
    monkeypatch.setattr(stats_ui, "load_atlas_stats", load)
    view.session.set("2026-09-11")
    view._date_changed()
    finish(view)
    assert requested == ["2026-09-11"]
    assert view.session.get() == view.atlas.snapshot.session == "2026-09-11"


def test_scout_and_atlas_panels_keep_distinct_populations(view):
    from app.ui.scout_stats_data import ScoutStatsSnapshot
    atlas = snapshot(view.session.get())
    scout = ScoutStatsSnapshot(session=view.session.get(), status="read", prediction_status="read",
        forecasts=({**atlas.forecasts[0], "forecast_id": "scout-fixture", "target_not_down": False},))
    view.atlas.set_snapshot(atlas)
    view.scout.set_snapshot(scout)
    assert view.atlas.accuracy_label.cget("text") == "100.0%"
    assert view.scout.accuracy_label.cget("text") == "0.0%"
    assert view.values["accuracy"].get() == "50.0%"


def test_session_switch_clears_atlas_even_without_gameplan_review(view, monkeypatch):
    view.review = None
    view.atlas.set_snapshot(snapshot("2026-09-15"))
    monkeypatch.setattr(stats_ui.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    view.session.set("2026-09-16")
    view._date_changed()
    assert view.atlas.snapshot is None
    assert view.atlas.order_tree.get_children() == ()
    assert view.atlas.accuracy_label.cget("text") == "—"


@pytest.mark.parametrize("width", (1280, 700))
def test_actual_layout_keeps_atlas_columns_and_scrollbars_inside_canvas(view, width):
    root = view.root
    root.geometry(f"{width}x1000")
    root.deiconify()
    try:
        root.update()
        canvas_right = view.canvas.winfo_rootx() + view.canvas.winfo_width()
        for widget in (view.atlas.frame, view.atlas.prediction_panel, view.atlas.order_panel,
                       view.atlas.company_tree, view.atlas.order_tree):
            assert widget.winfo_rootx() + widget.winfo_width() <= canvas_right + 2
        for tree in (view.atlas.company_tree, view.atlas.order_tree):
            scrollbars = [child for child in tree.master.winfo_children() if isinstance(child, ttk.Scrollbar)]
            assert any(str(child.cget("orient")) == "horizontal" and child.winfo_ismapped() for child in scrollbars)
        assert view.atlas.order_panel.grid_info()["row"] == (1 if width < 900 else 0)
    finally:
        root.withdraw()
