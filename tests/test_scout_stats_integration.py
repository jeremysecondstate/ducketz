"""Offline checks that Scout data cannot change Gameplan horizon populations."""
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
from app.ui.scout_stats_data import ScoutStatsSnapshot
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
    return ScoutStatsSnapshot(session=session, status="available", message="Saved Scout fixture",
        prediction_status="read", execution_status="read", forecasts=({
            "forecast_id": "scout-fixture", "symbol": "TEST", "supported_target": True,
            "issued_at": f"{session}T16:15:00Z", "target_open": f"{session}T16:30:00Z",
            "target_close": f"{session}T16:45:00Z", "probability_not_down": .8,
            "outcome_status": "scored", "target_not_down": True,
        },))


def test_scout_only_session_remains_selectable_without_gameplan_review(view, monkeypatch):
    saved = snapshot()
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: (saved.session,))
    monkeypatch.setattr(stats_ui, "load_scout_stats", lambda *_: saved)
    view.refresh()
    finish(view)
    assert view.session.get() == saved.session
    assert view.scout.snapshot is saved
    assert view.scout.accuracy_label.cget("text") == "100.0%"
    assert view.review is None
    assert all(value.get() == "—" for value in view.values.values())
    assert "2026-09-11" in view.date_box.cget("values")
    assert saved.session in view.date_box.cget("values")


def test_horizon_filter_does_not_recompute_scout_population(view):
    saved = snapshot(view.session.get())
    view.scout.set_snapshot(saved)
    caption = view.scout.accuracy_caption.cget("text")
    view.horizon.set("4 hours")
    view.render()
    assert view.values["accuracy"].get() == "0.0%"
    assert view.scout.snapshot is saved
    assert view.scout.accuracy_caption.cget("text") == caption
    assert view.scout.accuracy_label.cget("text") == "100.0%"


def test_scout_reader_failure_preserves_gameplan_metrics(view, monkeypatch):
    def fail(*_):
        raise OSError("Offline unreadable fixture")
    monkeypatch.setattr(stats_ui, "load_scout_stats", fail)
    view.refresh()
    finish(view)
    assert view.values["accuracy"].get() == "50.0%"
    assert view.scout.snapshot.prediction_status == "unreadable"
    assert view.scout.accuracy_label.cget("text") == "—"
    assert "could not be read" in view.scout.state_label.cget("text")


def test_session_change_clears_old_scout_and_ignores_obsolete_reply(view, monkeypatch):
    view.scout.set_snapshot(snapshot(view.session.get()))
    monkeypatch.setattr(stats_ui.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    view.session.set("2026-09-15")
    view._date_changed()
    stale_request = view._request_id
    assert view.scout.snapshot is None
    view.session.set("2026-09-16")
    view._date_changed()
    saved = snapshot("2026-09-16")
    view._messages.put((view._request_id, (saved.session,), None, ValueError("No Gameplan"), None, saved))
    view._messages.put((stale_request, ("2026-09-15",), None, ValueError("No Gameplan"), None, snapshot()))
    finish(view)
    assert view.session.get() == saved.session
    assert view.scout.snapshot is saved


def test_pinned_session_is_passed_to_scout_reader(view, monkeypatch):
    requested = []
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: ("2026-09-15", "2026-09-11"))
    def load(_root, session):
        requested.append(session)
        return snapshot(session)
    monkeypatch.setattr(stats_ui, "load_scout_stats", load)
    view.session.set("2026-09-11")
    view._date_changed()
    finish(view)
    assert requested == ["2026-09-11"]
    assert view.session.get() == view.scout.snapshot.session == "2026-09-11"
