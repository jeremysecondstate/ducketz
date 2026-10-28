"""Offline integration of synthetic Scout horizon history with the Stats tab."""
from __future__ import annotations

import gc
import threading
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import weakref

import pytest

import app.ui.gameplan_stats as stats_ui
from app.ui.atlas_stats_data import AtlasStatsSnapshot
from app.ui.scout_stats_history import load_horizon_history
from gameplan_stats_fixture import write_review
from test_scout_stats_history import history_fixture


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
def view(tmp_path, tk_root):
    frame = ttk.Frame(tk_root)
    frame.pack(fill="both", expand=True)
    widget = stats_ui.GameplanStatsTab(tk_root, frame, datastore_root=tmp_path, auto_load=False)
    reference = weakref.ref(widget)
    try:
        yield weakref.proxy(widget)
    finally:
        frame.destroy()
        del widget, frame
        gc.collect()
        assert reference() is None, "History integration fixture retained destroyed Tk view"


def finish(view):
    deadline = time.monotonic() + 10
    while view._loading and time.monotonic() < deadline:
        view.root.update()
        time.sleep(.01)
    assert not view._loading


def choose(view, session):
    view.session.set(session)
    view._date_changed()
    finish(view)


def test_history_dates_join_selector_and_no_saved_date_is_selectable(view, tmp_path):
    history_fixture(tmp_path)
    write_review(tmp_path)
    view.refresh()
    finish(view)
    assert set(view.date_box.cget("values")) == {"2026-10-13", "2026-10-01", "2026-09-11"}
    assert view.session.get() == "2026-10-13"
    assert view.scout_horizons.snapshot.session == "2026-10-13"
    assert view.scout_horizons.accuracy_label.cget("text") == "100.0%"
    assert view.scout_horizons.brier_label.cget("text") == "0.090"
    assert view.review is None
    choose(view, "2026-10-01")
    assert view.session.get() == view.scout_horizons.snapshot.session == "2026-10-01"
    assert view.scout_horizons.snapshot.coverage_status == "NO_SAVED_INDEPENDENT_GAMEPLAN"
    assert "No saved Gameplan forecasts" in view.scout_horizons.state_label.cget("text")
    assert view.scout_horizons.accuracy_label.cget("text") == view.scout_horizons.brier_label.cget("text") == "—"
    assert view.scout_horizons.forecast_tree.get_children() == ()
    assert view.review is None
    assert all(value.get() == "—" for value in view.values.values())


def test_history_scores_and_filters_are_independent_of_local_review_and_15m(view, tmp_path, monkeypatch):
    history_fixture(tmp_path)
    write_review(tmp_path, session="2026-10-13")
    atlas = AtlasStatsSnapshot(session="2026-10-13", status="read", message="Invented Atlas snapshot",
        prediction_status="read", execution_status="read")
    monkeypatch.setattr(stats_ui, "load_atlas_stats", lambda *_: atlas)
    choose(view, "2026-10-13")
    local_values = tuple(value.get() for value in view.values.values())
    fifteen = view.scout.snapshot
    assert view.values["accuracy"].get() == "50.0%"
    assert view.scout.accuracy_label.cget("text") == view.scout_horizons.accuracy_label.cget("text") == "100.0%"
    assert view.scout.brier_label.cget("text") == "0.040"
    assert view.scout_horizons.brier_label.cget("text") == "0.090"
    view.scout_horizons.horizon.set("4 hours")
    view.scout_horizons.render()
    assert view.scout_horizons.accuracy_label.cget("text") == "—"
    assert view.scout.snapshot is fifteen
    assert view.scout.accuracy_label.cget("text") == "100.0%"
    assert tuple(value.get() for value in view.values.values()) == local_values
    assert view.atlas.snapshot is atlas
    view.scout_horizons.horizon.set("All horizons")
    view.scout_horizons.render()
    view.horizon.set("4 hours")
    view.render()
    assert view.values["accuracy"].get() == "0.0%"
    assert view.scout_horizons.accuracy_label.cget("text") == "100.0%"
    assert view.scout.brier_label.cget("text") == "0.040"


def test_refresh_selects_newer_saved_revision_without_summing_history(view, tmp_path):
    original = history_fixture(tmp_path)
    before = {path.relative_to(original): path.read_bytes() for path in original.rglob("*") if path.is_file()}
    choose(view, "2026-10-13")
    first = view.scout_horizons.snapshot
    assert first.revision_count == 2
    assert view.scout_horizons.brier_label.cget("text") == "0.090"
    history_fixture(tmp_path, completion_id="20261028T014000Z-synthetic-scout-history-new", revisions=3)
    view.refresh()
    finish(view)
    selected = view.scout_horizons.snapshot
    assert selected.revision_count == 3
    assert selected.package_sha256 != first.package_sha256
    assert selected.review.metrics().total == 1
    assert view.scout_horizons.brier_label.cget("text") == "0.160"
    assert "Saved revisions: 3" in view.scout_horizons.revision_label.cget("text")
    assert all((original / path).read_bytes() == raw for path, raw in before.items())


def test_date_change_clears_history_and_stale_progressive_reply_is_ignored(view, tmp_path, monkeypatch):
    history_fixture(tmp_path)
    current = load_horizon_history(tmp_path, "2026-10-13")
    obsolete = load_horizon_history(tmp_path, "2026-10-01")
    view.scout_horizons.set_snapshot(obsolete)
    monkeypatch.setattr(stats_ui.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    view.session.set("2026-10-13")
    view._date_changed()
    request = view._request_id
    assert view.scout_horizons.snapshot is None
    view._messages.put(("horizons", request, (current.session,), current))
    view._messages.put(("horizons", request - 1, (obsolete.session,), obsolete))
    view._messages.put((request, (current.session,), None, ValueError("No saved local review"), None, None, None))
    finish(view)
    assert view.scout_horizons.snapshot is current
    assert view.scout_horizons.accuracy_label.cget("text") == "100.0%"
    assert view.session.get() == current.session


def test_corrupt_history_is_unavailable_without_altering_local_gameplan_or_atlas(view, tmp_path, monkeypatch):
    directory = history_fixture(tmp_path)
    write_review(tmp_path, session="2026-10-13")
    atlas = AtlasStatsSnapshot(session="2026-10-13", status="read", message="Invented Atlas snapshot")
    monkeypatch.setattr(stats_ui, "load_atlas_stats", lambda *_: atlas)
    with (directory / "report.html").open("ab") as handle:
        handle.write(b"changed after verification")
    choose(view, "2026-10-13")
    assert view.scout_horizons.snapshot.status == "unreadable"
    assert "could not be verified" in view.scout_horizons.state_label.cget("text")
    assert view.scout_horizons.accuracy_label.cget("text") == view.scout_horizons.brier_label.cget("text") == "—"
    assert view.values["accuracy"].get() == "50.0%"
    assert view.atlas.snapshot is atlas


def test_horizon_history_appears_before_slow_atlas_reader_finishes(view, tmp_path, monkeypatch):
    history_fixture(tmp_path)
    release = threading.Event()
    def slow_atlas(*_):
        assert release.wait(10)
        return AtlasStatsSnapshot(session="2026-10-13")
    monkeypatch.setattr(stats_ui, "load_atlas_stats", slow_atlas)
    view.session.set("2026-10-13")
    view._date_changed()
    try:
        deadline = time.monotonic() + 5
        while view.scout_horizons.snapshot is None and time.monotonic() < deadline:
            view.root.update()
            time.sleep(.01)
        assert view._loading
        assert view.scout_horizons.accuracy_label.cget("text") == "100.0%"
        assert view.scout.accuracy_label.cget("text") == "100.0%"
        assert view.scout_horizons.snapshot.review.metrics().total == 1
    finally:
        release.set()
        finish(view)
