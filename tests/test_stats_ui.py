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
    monkeypatch.setattr(stats_ui, "load_atlas_stats", lambda _root, session: saved("Atlas", session))
    monkeypatch.setattr(stats_ui, "load_scout_stats", lambda _root, session: saved("Scout", session, correct=False))
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


def test_stats_opens_directly_on_atlas_without_retired_sections(view):
    view.root.geometry("1280x900")
    view.root.deiconify()
    view.root.update()
    assert view.heading.cget("text") == "Stats"
    assert view.actor.get() == "Atlas"
    assert view.atlas.frame.winfo_ismapped()
    assert not view.scout.frame.winfo_ismapped()
    assert view.atlas.frame.winfo_y() <= 2
    content = "\n".join(texts(view.parent))
    assert "Atlas 15-Minute Trader" in content
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


def test_historical_selection_stays_pinned_until_latest_clicked(view, monkeypatch):
    calls = []
    for actor in ("Atlas", "Scout"):
        def load(_root, session, actor=actor):
            calls.append((actor, session))
            return saved(actor, session)
        monkeypatch.setattr(stats_ui, f"load_{actor.lower()}_stats", load)
    view.session.set(EARLIER)
    view._date_changed()
    finish(view)
    assert view.session.get() == EARLIER
    assert set(calls) == {("Atlas", EARLIER), ("Scout", EARLIER)}
    calls.clear()
    view.refresh()
    finish(view)
    assert set(calls) == {("Atlas", EARLIER), ("Scout", EARLIER)}
    calls.clear()
    view.follow_latest()
    finish(view)
    assert view.session.get() == LATEST
    assert set(calls) == {("Atlas", LATEST), ("Scout", LATEST)}


def test_date_change_clears_both_panels_and_rejects_obsolete_replies(view, monkeypatch):
    view.atlas.set_snapshot(saved())
    view.scout.set_snapshot(saved("Scout"))
    monkeypatch.setattr(stats_ui.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    view.session.set(EARLIER)
    view._date_changed()
    obsolete = view._request_id
    assert view.atlas.snapshot is None and view.scout.snapshot is None
    assert view.atlas.company_tree.get_children() == view.scout.company_tree.get_children() == ()
    view.session.set(LATEST)
    view._date_changed()
    current = view._request_id
    atlas, scout = saved(), saved("Scout", correct=False)
    view._messages.put((current, "Atlas", (LATEST, EARLIER), LATEST, atlas))
    view._messages.put((current, "Scout", (LATEST, EARLIER), LATEST, scout))
    view._messages.put((obsolete, "Atlas", (EARLIER,), EARLIER, saved(session=EARLIER)))
    view._messages.put((obsolete, "Scout", (EARLIER,), EARLIER, saved("Scout", EARLIER)))
    finish(view)
    assert view.session.get() == LATEST
    assert view.atlas.snapshot is atlas and view.scout.snapshot is scout


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


@pytest.mark.parametrize("slow_actor", ("Atlas", "Scout"))
def test_fast_actor_appears_without_waiting_for_other_reader(view, monkeypatch, slow_actor):
    release = threading.Event()
    returned = threading.Event()
    def slow(_root, session):
        try:
            assert release.wait(10), "Offline reader fixture was not released"
            return saved(slow_actor, session)
        finally:
            returned.set()
    monkeypatch.setattr(stats_ui, f"load_{slow_actor.lower()}_stats", slow)
    fast = view.scout if slow_actor == "Atlas" else view.atlas
    slow_panel = getattr(view, slow_actor.lower())
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
def test_selected_actor_starts_at_top_and_tables_fit_viewport(view, width):
    view.root.geometry(f"{width}x700")
    view.root.deiconify()
    view.root.update()
    view.canvas.yview_moveto(1)
    view.root.update()
    view.actor.set("Scout")
    view._actor_changed()
    view.root.update()
    assert not view.atlas.frame.winfo_ismapped()
    assert view.scout.frame.winfo_ismapped()
    assert view.scout.frame.winfo_y() <= 2
    assert view.canvas.yview()[0] == 0
    canvas_right = view.canvas.winfo_rootx() + view.canvas.winfo_width()
    for widget in (view.scout.frame, view.scout.prediction_panel, view.scout.order_panel,
                   view.scout.company_tree, view.scout.order_tree):
        assert widget.winfo_rootx() + widget.winfo_width() <= canvas_right + 2
    for tree in (view.scout.company_tree, view.scout.order_tree):
        bars = [child for child in tree.master.winfo_children() if isinstance(child, ttk.Scrollbar)]
        assert any(str(bar.cget("orient")) == "horizontal" and bar.winfo_ismapped() for bar in bars)
    assert view.scout.order_panel.grid_info()["row"] == (1 if width < 900 else 0)


def test_actor_switch_uses_existing_evidence_without_reloading(view, monkeypatch):
    view.refresh()
    finish(view)
    atlas, scout = view.atlas.snapshot, view.scout.snapshot
    def unexpected(*_):
        pytest.fail("Switching the visible trader must not reload saved evidence")
    monkeypatch.setattr(stats_ui, "load_atlas_stats", unexpected)
    monkeypatch.setattr(stats_ui, "load_scout_stats", unexpected)
    view.actor.set("Scout")
    view._actor_changed()
    view.actor.set("Atlas")
    view._actor_changed()
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
