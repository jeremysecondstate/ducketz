from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

import pytest

import app.ui.gameplan_stats as stats_ui
from app.ui.gameplan_stats_data import GameplanStatsError, load_gameplan_stats
from gameplan_stats_fixture import forecast, write_review


@pytest.fixture
def tab(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    view = stats_ui.GameplanStatsTab(root, frame, datastore_root=tmp_path, auto_load=False)
    write_review(tmp_path)
    view.set_review(load_gameplan_stats(tmp_path))
    yield view
    root.destroy()


def _finish_refresh(tab):
    deadline = time.monotonic() + 3
    while tab._loading and time.monotonic() < deadline:
        tab.root.update()
        time.sleep(.01)
    assert not tab._loading, "Background review load did not finish"


def test_filter_updates_summary_without_hiding_or_relabelling_hourly_grid(tab):
    assert tab.values["accuracy"].get() == "50.0%"
    assert tab.values["brier"].get() == "0.316"
    before = [(cell[4:6], cell[6].state if cell[6] else None) for cell in tab._cell_bounds]
    tab.horizon.set("4 hours")
    tab.horizon_box.event_generate("<<ComboboxSelected>>")
    assert tab.values["accuracy"].get() == "0.0%"
    assert tab.values["bullish"].get() == "—"
    assert tab.captions["bullish"].get() == "0 scored calls"
    assert before == [(cell[4:6], cell[6].state if cell[6] else None) for cell in tab._cell_bounds]
    tab.horizon.set("Weekly")
    tab.render()
    assert all(value.get() == "—" for value in tab.values.values())
    assert "weekly" in tab.grid_scope.cget("text")


def test_selection_and_grid_click_show_frozen_observations(tab, monkeypatch):
    tab._step_symbol(1)
    assert tab.selected_symbol == "NVDA"
    assert tab.selected_boxes[0][0].cget("text") == "1 / 1"
    shown = []
    monkeypatch.setattr(tab, "_text_dialog", lambda title, text: shown.append((title, text)))
    left, top, right, bottom, symbol, col, outcome = tab._cell_bounds[0]
    tab._grid_click(SimpleNamespace(x=(left+right)/2, y=(top+bottom)/2))
    assert tab.selected_symbol == "AAPL"
    assert "04:00–05:00" in shown[0][0]
    assert "Saved model probability: 20.00%" in shown[0][1]
    assert "Observed return: -1.000%" in shown[0][1]
    assert "Start observation:" in shown[0][1]
    assert "return above 0.10%" in shown[0][1]


def test_date_change_clears_old_values_and_loads_requested_publication(tab, tmp_path):
    write_review(tmp_path, [forecast(session="2026-09-10")], session="2026-09-10", latest=False)
    tab.session.set("2026-09-10")
    tab._date_changed()
    assert tab.review is None
    assert tab.values["accuracy"].get() == "—"
    _finish_refresh(tab)
    assert tab.review.session == "2026-09-10"
    assert tab.values["accuracy"].get() == "100.0%"


def test_refresh_failure_clears_unverifiable_metrics_and_report_action(tab, monkeypatch):
    def fail(*_args):
        raise GameplanStatsError("Fixture review integrity mismatch")
    monkeypatch.setattr(stats_ui, "load_gameplan_stats", fail)
    tab.refresh()
    _finish_refresh(tab)
    assert tab.review is None
    assert all(value.get() == "—" for value in tab.values.values())
    assert "integrity mismatch" in tab.status.get()
    assert "disabled" in tab.report_button.state()


def test_older_background_response_cannot_replace_new_selected_date(tab, tmp_path):
    old = tab.review
    write_review(tmp_path, [forecast(session="2026-09-10")], session="2026-09-10", latest=False)
    selected = load_gameplan_stats(tmp_path, "2026-09-10")
    tab._request_id = 2
    tab._loading = True
    tab._messages.put((2, ("2026-09-11", "2026-09-10"), selected, None))
    tab._messages.put((1, ("2026-09-11",), old, None))
    _finish_refresh(tab)
    assert tab.review.session == "2026-09-10"
    assert tab.values["accuracy"].get() == "100.0%"


def test_open_report_uses_the_displayed_immutable_report(tab, monkeypatch):
    opened = []
    if stats_ui.os.name == "nt":
        monkeypatch.setattr(stats_ui.os, "startfile", lambda path: opened.append(path))
    else:
        monkeypatch.setattr(stats_ui.webbrowser, "open", lambda path: opened.append(path))
    tab.open_report()
    assert len(opened) == 1
    assert "gameplan-actuals-review-runs" in opened[0]
    assert opened[0].endswith("Gameplan-results.md")
    tab.show_definitions()
    dialogs = [child for child in tab.root.winfo_children() if isinstance(child, tk.Toplevel)]
    assert dialogs


def test_destroy_cancels_polling_and_ignores_completion(tab):
    tab.parent.destroy()
    assert tab._closed
    jobs = tab.root.tk.call("after", "info")
    assert tab._poll_job not in jobs and tab._auto_job not in jobs
