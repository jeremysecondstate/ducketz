"""Offline session and rendering checks for the independent 15-minute Stats tab."""
from __future__ import annotations

import gc
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

import pytest

import app.ui.stats as stats_ui
from app.ui.scout_stats_data import ScoutStatsSnapshot


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
    frame = ttk.Frame(tk_root)
    frame.pack(fill="both", expand=True)
    tab = stats_ui.StatsTab(tk_root, frame, datastore_root=tmp_path, auto_load=False)
    yield tab
    for child in tk_root.winfo_children():
        if isinstance(child, tk.Toplevel):
            child.destroy()
    frame.destroy()
    gc.collect()


def finish(view):
    deadline = time.monotonic() + 5
    while view._loading and time.monotonic() < deadline:
        view.root.update()
        time.sleep(.01)
    assert not view._loading, "Saved Stats reader did not complete"


def snapshot(session="2026-10-29", **changes):
    values = dict(session=session, status="available", message="Saved 15-minute fixture loaded.",
        prediction_status="read", execution_status="read", forecasts=({
            "forecast_id": "fixture-forecast", "symbol": "TEST", "supported_target": True,
            "reference_at": f"{session}T16:00:00Z", "issued_at": f"{session}T16:15:00Z",
            "target_open": f"{session}T16:30:00Z", "target_close": f"{session}T16:45:00Z",
            "probability_not_down": .8, "outcome_status": "scored", "target_not_down": True,
        },), orders=({
            "request_id": "fixture-request", "forecast_id": "fixture-forecast", "symbol": "TEST",
            "side": "BUY", "submission_state": "ACCEPTED", "status": "FILLED",
            "quantity": 3, "filled_quantity": 3, "issued_at": f"{session}T16:16:00Z",
            "observed_at": f"{session}T16:17:00Z", "fills": ({"fill_id": "fixture-fill",
                "quantity": 3, "price": 10.25, "executed_at": f"{session}T16:16:01Z"},),
        },))
    values.update(changes)
    return ScoutStatsSnapshot(**values)


def all_widgets(widget):
    yield widget
    for child in widget.winfo_children():
        yield from all_widgets(child)


def widget_text(widget):
    if isinstance(widget, tk.Text):
        return widget.get("1.0", "end")
    try:
        return str(widget.cget("text"))
    except tk.TclError:
        return ""


def test_latest_session_uses_only_15m_evidence_and_has_no_retired_sections(view, monkeypatch):
    calls = []
    saved = snapshot()
    # Poison retired readers if they accidentally become a dependency again.
    def retired(*_args, **_kwargs):
        pytest.fail("Retired Gameplan evidence was read by the Stats tab")
    for name in ("review_sessions", "execution_sessions", "horizon_history_sessions",
                 "load_gameplan_stats", "load_gameplan_execution", "load_horizon_history"):
        monkeypatch.setattr(stats_ui, name, retired, raising=False)
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: ("2026-10-29", "2026-10-28"))
    def load(root, session):
        calls.append((root, session))
        return saved
    monkeypatch.setattr(stats_ui, "load_scout_stats", load)
    view.refresh()
    finish(view)
    assert calls == [(view.datastore_root, "2026-10-29")]
    assert view.session.get() == saved.session
    assert view.scout.snapshot is saved
    assert tuple(view.date_box.cget("values")) == ("2026-10-29", "2026-10-28")
    assert view.heading.cget("text") == "Stats"
    assert view.scout.accuracy_caption.cget("text") == "1 / 1 scored"
    assert "Gameplan" not in "\n".join(widget_text(w) for w in all_widgets(view.parent))


def test_pinned_session_and_latest_button_choose_matching_population(view, monkeypatch):
    calls = []
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: ("2026-10-29", "2026-10-28"))
    def load(_root, session):
        calls.append(session)
        return snapshot(session)
    monkeypatch.setattr(stats_ui, "load_scout_stats", load)
    view.session.set("2026-10-28")
    view._date_changed()
    finish(view)
    assert view.session.get() == view.scout.snapshot.session == "2026-10-28"
    view.follow_latest()
    finish(view)
    assert view.session.get() == view.scout.snapshot.session == "2026-10-29"
    assert calls == ["2026-10-28", "2026-10-29"]


def test_changed_date_clears_old_numbers_and_discards_obsolete_reply(view, monkeypatch):
    view.scout.set_snapshot(snapshot("2026-10-28"))
    monkeypatch.setattr(stats_ui.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    view.session.set("2026-10-28")
    view._date_changed()
    stale_request = view._request_id
    view.session.set("2026-10-29")
    view._date_changed()
    assert view.scout.snapshot is None
    assert view.scout.accuracy_label.cget("text") == "—"
    current = snapshot()
    view._messages.put((view._request_id, (current.session,), current))
    view._messages.put((stale_request, ("2026-10-28",), snapshot("2026-10-28")))
    finish(view)
    assert view.scout.snapshot is current
    assert view.session.get() == current.session
    assert "2026-10-29" in view.scout.session_label.cget("text")


def test_empty_store_finishes_without_gameplan_error_or_invented_zero_accuracy(view, monkeypatch):
    missing = ScoutStatsSnapshot(message="No saved 15-minute results are available.")
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: ())
    monkeypatch.setattr(stats_ui, "load_scout_stats", lambda *_: missing)
    view.refresh()
    finish(view)
    assert view.scout.accuracy_label.cget("text") == "—"
    assert tuple(view.date_box.cget("values")) == ()
    assert view.session.get() == ""
    assert "Gameplan" not in view.status.get()
    assert "normal" in str(view.refresh_button.cget("state"))


def test_unreadable_current_session_clears_old_results_and_can_be_retried(view, monkeypatch):
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: ("2026-10-29",))
    def fail(*_):
        raise OSError("Synthetic unreadable fixture")
    monkeypatch.setattr(stats_ui, "load_scout_stats", fail)
    view.scout.set_snapshot(snapshot("2026-10-28"))
    view.refresh()
    finish(view)
    assert view.scout.accuracy_label.cget("text") == "—"
    assert "Gameplan" not in view.status.get()
    assert view.scout.snapshot.status == "unreadable"
    saved = snapshot()
    monkeypatch.setattr(stats_ui, "load_scout_stats", lambda *_: saved)
    view.refresh()
    finish(view)
    assert view.scout.snapshot is saved
    assert view.scout.accuracy_label.cget("text") == "100.0%"


def test_session_summary_reports_displayed_15m_population_and_unknown_profit(view, monkeypatch):
    saved = snapshot()
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: (saved.session,))
    monkeypatch.setattr(stats_ui, "load_scout_stats", lambda *_: saved)
    view.refresh()
    finish(view)
    view.open_report()
    dialogs = [child for child in view.root.winfo_children() if isinstance(child, tk.Toplevel)]
    assert dialogs
    text = "\n".join(widget_text(w) for dialog in dialogs for w in all_widgets(dialog))
    assert saved.session in text
    assert "Observed scored: 1" in text
    assert "Correct: 1" in text
    assert "Known filled quantity: 3" in text
    assert "Gameplan" not in text
    assert "profit" in text.lower() or "p&l" in text.lower()
    assert "unknown" in text.lower() or "unavailable" in text.lower() or "not available" in text.lower()


def test_summary_preserves_unavailable_populations_instead_of_inventing_zero(view, monkeypatch):
    missing = ScoutStatsSnapshot(session="2026-10-29", status="unreadable",
        prediction_status="unreadable", execution_status="missing", message="Saved evidence is unavailable.")
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: (missing.session,))
    monkeypatch.setattr(stats_ui, "load_scout_stats", lambda *_: missing)
    view.refresh()
    finish(view)
    view.open_report()
    dialogs = [child for child in view.root.winfo_children() if isinstance(child, tk.Toplevel)]
    assert dialogs
    text = "\n".join(widget_text(w) for dialog in dialogs for w in all_widgets(dialog))
    assert "unavailable" in text.lower()
    assert "Live forecasts: 0" not in text
    assert "Saved order requests: 0" not in text
    assert "Known filled quantity: 0" not in text


def test_reader_cannot_put_another_sessions_metrics_under_selected_date(view, monkeypatch):
    monkeypatch.setattr(stats_ui, "scout_stats_sessions", lambda *_: ("2026-10-29", "2026-10-28"))
    monkeypatch.setattr(stats_ui, "load_scout_stats", lambda *_: snapshot("2026-10-28"))
    view.session.set("2026-10-29")
    view._date_changed()
    finish(view)
    assert view.session.get() == "2026-10-29"
    assert view.scout.snapshot.status == "unreadable"
    assert view.scout.accuracy_label.cget("text") == "—"


def test_destroy_invalidates_pending_read_and_cancels_refresh_jobs(view, monkeypatch):
    monkeypatch.setattr(stats_ui.threading, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
    view.refresh()
    request = view._request_id
    jobs = (view._poll_job, view._auto_job)
    view.parent.destroy()
    assert view._closed
    assert view._request_id > request
    remaining = view.root.tk.call("after", "info")
    assert all(job not in remaining for job in jobs)
    view._messages.put((request, ("2026-10-29",), snapshot()))
    view._poll()
