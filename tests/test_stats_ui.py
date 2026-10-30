"""Offline behavior of the 15-minute-only Stats workspace.

Every reader is replaced with saved-evidence fixtures. No operational datastore,
provider, broker, model producer or retired Gameplan producer is invoked.
"""
from __future__ import annotations

import gc
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import weakref

import pytest

import app.ui.stats as stats_ui
from app.ui.atlas_stats_data import AtlasStatsSnapshot
from app.ui.scout_stats_data import ScoutStatsSnapshot
from app.ui.scout_stats_report import ScoutStatsReport
from test_scout_stats_report_ui import saved_report


LATEST = "2026-10-29"
EARLIER = "2026-10-28"


def saved(actor="Atlas", session=LATEST, *, correct=True, identity="original"):
    snapshot_type = AtlasStatsSnapshot if actor == "Atlas" else ScoutStatsSnapshot
    return snapshot_type(session=session, status="read", message=f"Saved {actor} fixture",
        prediction_status="read", execution_status="read", forecasts=({
            "forecast_id": f"{actor}-{identity}", "pc": actor, "symbol": "TEST", "mode": "live",
            "reference_at": f"{session}T16:00:00Z", "issued_at": f"{session}T16:16:00Z",
            "target_open": f"{session}T16:30:00Z", "target_close": f"{session}T16:45:00Z",
            "supported_target": True, "probability_not_down": .8, "outcome_status": "scored",
            "target_not_down": correct, "direction": "BULLISH",
            "direction_policy_version": "stock-direction-50-01-v3",
            "direction_up_threshold": .5001, "direction_down_threshold": .4999,
        },), orders=({
            "request_id": f"{actor}-request", "forecast_id": f"{actor}-{identity}",
            "reference_at": f"{session}T16:00:00Z", "symbol": "TEST", "side": "BUY",
            "quantity": 3, "status": "UNKNOWN", "submission_state": "UNKNOWN",
            "filled_quantity": None, "fills": (), "submitted_at": None,
        },))


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
def view(tmp_path, tk_root, monkeypatch):
    monkeypatch.setattr(stats_ui, "atlas_stats_sessions", lambda *_: (LATEST, EARLIER))
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: (EARLIER,))
    monkeypatch.setattr(stats_ui, "scout_report_sessions", lambda *_: ())
    monkeypatch.setattr(stats_ui, "load_atlas_stats", lambda _root, session: saved("Atlas", session))
    monkeypatch.setattr(stats_ui, "load_scout_stats", lambda _root, session: saved("Scout", session, correct=False))
    monkeypatch.setattr(stats_ui, "load_scout_report", lambda _root, session: ScoutStatsReport(session=session))
    frame = ttk.Frame(tk_root)
    frame.pack(fill="both", expand=True)
    widget = stats_ui.StatsTab(tk_root, frame, datastore_root=tmp_path, auto_load=False)
    reference = weakref.ref(widget)
    try:
        yield weakref.proxy(widget)
    finally:
        frame.destroy()
        tk_root.withdraw()
        del widget, frame
        gc.collect()
        assert reference() is None, "Stats workspace retained its destroyed Tk view"


def wait_for(view, predicate, *, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view.root.update()
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), "Offline Stats response did not arrive"


def finish(view):
    wait_for(view, lambda: not view._loading)


def texts(widget):
    result = []
    try:
        result.append(str(widget.cget("text")))
    except tk.TclError:
        pass
    for child in widget.winfo_children():
        result.extend(texts(child))
    return result


def test_stats_import_has_no_retired_or_operational_producer_dependencies():
    # A fresh interpreter catches accidental eager production imports even when
    # another test has already imported the historical UI for archival checks.
    script = """
import sys
import app.ui.stats
forbidden = {
    'app.ui.gameplan_stats', 'app.ui.gameplan_stats_data',
    'app.ui.gameplan_execution_data', 'app.ui.scout_horizon_stats_widgets',
    'app.services.rolling_price_change', 'ml.nightly_workflow',
    'ml.intraday.runtime', 'ml.intraday.live_session', 'ml.intraday.supervisor',
    'datafetching.orchestrate',
}
assert not (forbidden & sys.modules.keys()), sorted(forbidden & sys.modules.keys())
"""
    result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_stats_shows_atlas_then_scout_without_retired_sections(view):
    view.root.geometry("1280x900")
    view.root.deiconify()
    view.root.update()
    assert view.heading.cget("text") == "Stats"
    assert view.atlas.frame.winfo_ismapped()
    assert view.scout.frame.winfo_ismapped()
    assert not view.scout_report.frame.winfo_ismapped()
    assert view.atlas.frame.winfo_y() <= 2
    assert view.scout.frame.winfo_y() >= view.atlas.frame.winfo_height()
    content = "\n".join(texts(view.parent))
    assert "Atlas 15-Minute Trader" in content
    assert "Scout 15-Minute Trader" in content
    for retired in ("Gameplan", "All horizons", "Open full report", "1-hour prediction outcomes", "Current prices"):
        assert retired not in content


def test_latest_uses_only_independent_session_union_and_keeps_populations_separate(view, monkeypatch):
    monkeypatch.setattr(stats_ui, "atlas_stats_sessions", lambda *_: (EARLIER,))
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: (LATEST,))
    view.refresh()
    finish(view)
    assert tuple(view.date_box.cget("values")) == (LATEST, EARLIER)
    assert view.session.get() == LATEST
    assert view.atlas.snapshot.session == view.scout.snapshot.session == LATEST
    assert view.atlas.accuracy_label.cget("text") == "100.0%"
    assert view.scout.accuracy_label.cget("text") == "0.0%"
    assert "1 unknown" in view.atlas.order_counts.cget("text")
    assert "Missing fill evidence: 1" in view.atlas.fill_coverage.cget("text")


def test_saved_scout_report_is_shown_below_atlas_without_mixing_row_populations(view, monkeypatch):
    report = saved_report()
    monkeypatch.setattr(stats_ui, "scout_report_sessions", lambda *_: (LATEST,))
    monkeypatch.setattr(stats_ui, "load_scout_report", lambda *_: report)
    view.root.deiconify()
    view.refresh()
    finish(view)
    view.root.update()
    assert view.atlas.frame.winfo_ismapped() and view.scout_report.frame.winfo_ismapped()
    assert not view.scout.frame.winfo_ismapped()
    assert view.atlas.frame.winfo_y() <= 2
    assert view.scout_report.frame.winfo_y() >= view.atlas.frame.winfo_height()
    assert view.scout_report.snapshot is report
    assert view.atlas.accuracy_label.cget("text") == "100.0%"
    assert view.scout_report.accuracy_label.cget("text") == "62.5%"
    assert view.scout.snapshot.forecasts[0]["forecast_id"] == "Scout-original"
    assert len(view.scout_report.order_tree.get_children()) == 2


def test_report_only_session_remains_selectable(view, monkeypatch):
    monkeypatch.setattr(stats_ui, "atlas_stats_sessions", lambda *_: ())
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: ())
    monkeypatch.setattr(stats_ui, "scout_report_sessions", lambda *_: (LATEST, EARLIER))
    monkeypatch.setattr(stats_ui, "load_scout_report", lambda _root, session: saved_report(session))
    view.refresh()
    finish(view)
    assert tuple(view.date_box.cget("values")) == (LATEST, EARLIER)
    assert view.session.get() == LATEST
    assert view.scout_report.snapshot.session == LATEST


def test_unreadable_report_stays_visible_without_using_other_scout_rows_as_replacement(view, monkeypatch):
    report = ScoutStatsReport(session=LATEST, status="unreadable", message="Saved report integrity failed.")
    monkeypatch.setattr(stats_ui, "load_scout_report", lambda *_: report)
    view.root.deiconify()
    view.refresh()
    finish(view)
    view.root.update()
    assert view.scout_report.frame.winfo_ismapped() and not view.scout.frame.winfo_ismapped()
    assert view.scout_report.accuracy_label.cget("text") == "—"
    assert "integrity failed" in view.scout_report.state_label.cget("text")
    assert view.scout.snapshot.prediction_status == "read"
    assert view.atlas.accuracy_label.cget("text") == "100.0%"


def test_missing_report_preserves_row_level_scout_fallback(view):
    view.root.deiconify()
    view.refresh()
    finish(view)
    view.root.update()
    assert view.scout_report.snapshot.status == "missing"
    assert view.scout.frame.winfo_ismapped() and not view.scout_report.frame.winfo_ismapped()
    assert view.scout.accuracy_label.cget("text") == "0.0%"
    assert "1 unknown" in view.scout.order_counts.cget("text")


def test_report_displays_before_slow_row_reader_and_is_not_hidden_by_its_later_reply(view, monkeypatch):
    release = threading.Event()
    returned = threading.Event()
    report = saved_report()
    def slow_rows(_root, session):
        try:
            assert release.wait(10), "Offline row reader fixture was not released"
            return saved("Scout", session, correct=False)
        finally:
            returned.set()
    monkeypatch.setattr(stats_ui, "load_scout_stats", slow_rows)
    monkeypatch.setattr(stats_ui, "load_scout_report", lambda *_: report)
    view.root.deiconify()
    view.refresh()
    try:
        wait_for(view, lambda: view.scout_report.snapshot is report)
        view.root.update()
        assert view._loading and view.scout.snapshot is None
        assert view.scout_report.frame.winfo_ismapped() and not view.scout.frame.winfo_ismapped()
    finally:
        release.set()
        assert returned.wait(5)
        finish(view)
    view.root.update()
    assert view.scout_report.frame.winfo_ismapped() and not view.scout.frame.winfo_ismapped()
    assert view.scout_report.snapshot is report


def test_historical_selection_stays_pinned_until_latest_clicked(view, monkeypatch):
    calls = []
    for actor in ("Atlas", "Scout", "Scout report"):
        def load(_root, session, actor=actor):
            calls.append((actor, session))
            return ScoutStatsReport(session=session) if actor == "Scout report" else saved(actor, session)
        name = "load_scout_report" if actor == "Scout report" else f"load_{actor.lower()}_stats"
        monkeypatch.setattr(stats_ui, name, load)
    view.session.set(EARLIER)
    view._date_changed()
    finish(view)
    assert view.session.get() == EARLIER
    assert set(calls) == {("Atlas", EARLIER), ("Scout", EARLIER), ("Scout report", EARLIER)}
    calls.clear()
    view.refresh()
    finish(view)
    assert set(calls) == {("Atlas", EARLIER), ("Scout", EARLIER), ("Scout report", EARLIER)}
    calls.clear()
    view.follow_latest()
    finish(view)
    assert view.session.get() == LATEST
    assert set(calls) == {("Atlas", LATEST), ("Scout", LATEST), ("Scout report", LATEST)}


def test_date_change_clears_all_panels_and_rejects_obsolete_replies(view, monkeypatch):
    view.atlas.set_snapshot(saved())
    view.scout.set_snapshot(saved("Scout"))
    view.scout_report.set_snapshot(saved_report())
    monkeypatch.setattr(stats_ui.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    view.session.set(EARLIER)
    view._date_changed()
    obsolete = view._request_id
    assert view.atlas.snapshot is None and view.scout.snapshot is None
    assert view.scout_report.snapshot is None
    assert view.atlas.company_tree.get_children() == view.scout.company_tree.get_children() == ()
    view.session.set(LATEST)
    view._date_changed()
    current = view._request_id
    atlas, scout = saved(), saved("Scout", correct=False)
    view._messages.put((current, "Atlas", (LATEST, EARLIER), LATEST, atlas))
    view._messages.put((current, "Scout", (LATEST, EARLIER), LATEST, scout))
    report = saved_report(session=LATEST)
    view._messages.put((current, "Scout report", (LATEST, EARLIER), LATEST, report))
    view._messages.put((obsolete, "Atlas", (EARLIER,), EARLIER, saved(session=EARLIER)))
    view._messages.put((obsolete, "Scout", (EARLIER,), EARLIER, saved("Scout", EARLIER)))
    view._messages.put((obsolete, "Scout report", (EARLIER,), EARLIER, saved_report(session=EARLIER)))
    finish(view)
    assert view.session.get() == LATEST
    assert view.atlas.snapshot is atlas and view.scout.snapshot is scout
    assert view.scout_report.snapshot is report


@pytest.mark.parametrize("failed_actor", ("Atlas", "Scout"))
def test_reader_failure_preserves_other_actor_and_missing_is_unavailable(view, monkeypatch, failed_actor):
    def unreadable(*_):
        raise OSError("Offline unreadable saved evidence")
    monkeypatch.setattr(stats_ui, f"load_{failed_actor.lower()}_stats", unreadable)
    view.refresh()
    finish(view)
    failed = getattr(view, failed_actor.lower())
    other = view.scout if failed_actor == "Atlas" else view.atlas
    assert failed.snapshot.prediction_status == failed.snapshot.execution_status == "unreadable"
    assert failed.accuracy_label.cget("text") == "—"
    assert failed.order_total.cget("text") == "— saved requests"
    assert other.snapshot.prediction_status == "read"
    assert other.accuracy_label.cget("text") in {"100.0%", "0.0%"}
    assert "disabled" not in view.refresh_button.state()


@pytest.mark.parametrize("slow_actor", ("Atlas", "Scout", "Scout report"))
def test_fast_actor_appears_without_waiting_for_other_reader(view, monkeypatch, slow_actor):
    release = threading.Event()
    returned = threading.Event()
    def slow(_root, session):
        try:
            assert release.wait(10), "Offline reader fixture was not released"
            return ScoutStatsReport(session=session) if slow_actor == "Scout report" else saved(slow_actor, session)
        finally:
            returned.set()
    name = "load_scout_report" if slow_actor == "Scout report" else f"load_{slow_actor.lower()}_stats"
    monkeypatch.setattr(stats_ui, name, slow)
    fast = view.scout if slow_actor == "Atlas" else view.atlas
    slow_panel = view.scout_report if slow_actor == "Scout report" else getattr(view, slow_actor.lower())
    view.refresh()
    try:
        wait_for(view, lambda: fast.snapshot is not None)
        assert slow_panel.snapshot is None
        assert view._loading
        assert fast.snapshot.prediction_status == "read"
    finally:
        release.set()
        assert returned.wait(5)
        finish(view)
    if slow_actor == "Scout report":
        assert slow_panel.snapshot.status == "missing"
    else:
        assert slow_panel.snapshot.prediction_status == "read"


def test_discovery_error_does_not_hide_other_actors_saved_session(view, monkeypatch):
    def unavailable(*_):
        raise OSError("Offline Atlas date index unavailable")
    monkeypatch.setattr(stats_ui, "atlas_stats_sessions", unavailable)
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: (EARLIER,))
    view.refresh()
    finish(view)
    assert view.session.get() == EARLIER
    assert tuple(view.date_box.cget("values")) == (EARLIER,)
    assert view.scout.snapshot.session == EARLIER


def test_same_session_refresh_replaces_saved_revision_without_summing(view, monkeypatch):
    view.refresh()
    finish(view)
    assert view.atlas.accuracy_label.cget("text") == "100.0%"
    revised = saved(correct=False, identity="revised")
    monkeypatch.setattr(stats_ui, "load_atlas_stats", lambda *_: revised)
    view.refresh()
    finish(view)
    assert view.atlas.snapshot is revised
    assert view.atlas.accuracy_label.cget("text") == "0.0%"
    assert view.atlas.accuracy_caption.cget("text") == "0 / 1 scored"
    assert view.scout.snapshot.forecasts[0]["forecast_id"] == "Scout-original"


@pytest.mark.parametrize("width", (1280, 700))
@pytest.mark.parametrize("report_available", (False, True))
def test_atlas_and_scout_stack_without_empty_area_and_tables_fit_viewport(view, width, monkeypatch, report_available):
    if report_available:
        monkeypatch.setattr(stats_ui, "load_scout_report", lambda *_: saved_report())
    view.refresh()
    finish(view)
    view.root.geometry(f"{width}x700")
    view.root.deiconify()
    view.root.update()
    scout = view.scout_report if report_available else view.scout
    assert view.atlas.frame.winfo_ismapped()
    assert scout.frame.winfo_ismapped()
    assert view.atlas.frame.winfo_y() <= 2
    gap = scout.frame.winfo_y() - view.atlas.frame.winfo_height()
    assert 0 <= gap <= 24
    assert view.canvas.yview()[0] == 0
    canvas_right = view.canvas.winfo_rootx() + view.canvas.winfo_width()
    for panel in (view.atlas, scout):
        for widget in (panel.frame, panel.prediction_panel, panel.order_panel,
                       panel.company_tree, panel.order_tree):
            assert widget.winfo_rootx() + widget.winfo_width() <= canvas_right + 2
        for tree in (panel.company_tree, panel.order_tree):
            bars = [child for child in tree.master.winfo_children() if isinstance(child, ttk.Scrollbar)]
            assert any(str(bar.cget("orient")) == "horizontal" and bar.winfo_ismapped() for bar in bars)
        assert panel.order_panel.grid_info()["row"] == (1 if width < 900 else 0)


def test_scrolling_uses_existing_evidence_without_reloading(view, monkeypatch):
    view.refresh()
    finish(view)
    atlas, scout = view.atlas.snapshot, view.scout.snapshot
    def unexpected(*_):
        pytest.fail("Scrolling between traders must not reload saved evidence")
    monkeypatch.setattr(stats_ui, "load_atlas_stats", unexpected)
    monkeypatch.setattr(stats_ui, "load_scout_stats", unexpected)
    monkeypatch.setattr(stats_ui, "load_scout_report", unexpected)
    view.canvas.yview_moveto(1)
    view.root.update()
    view.canvas.yview_moveto(0)
    view.root.update()
    assert view.atlas.snapshot is atlas and view.scout.snapshot is scout
    assert view.atlas.accuracy_label.cget("text") == "100.0%"


def test_destroy_cancels_callbacks_and_ignores_late_completion(view, monkeypatch):
    monkeypatch.setattr(stats_ui.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    view.refresh()
    request = view._request_id
    jobs = (view._poll_job, view._auto_job)
    errors = []
    monkeypatch.setattr(view.root, "report_callback_exception", lambda *args: errors.append(args))
    view.parent.destroy()
    view._messages.put((request, "Atlas", (LATEST,), LATEST, saved()))
    view.root.update()
    pending = view.root.tk.call("after", "info")
    assert all(job not in pending for job in jobs)
    assert view._closed
    assert not errors
