from __future__ import annotations

import gc
from datetime import datetime, timezone
import time
import tkinter as tk
import weakref
from tkinter import ttk
from types import SimpleNamespace

import pytest

import app.ui.gameplan_stats as stats_ui
from app.ui.gameplan_stats_data import GameplanStatsError, load_gameplan_stats
from app.ui.gameplan_execution_data import GameplanExecutionSnapshot
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
    reference = weakref.ref(view)
    try:
        # Pytest caches the yielded value until after fixture finalization. Keep
        # its cache weak so all Tk resources can be reclaimed on this thread.
        yield weakref.proxy(view)
    finally:
        root.destroy()
        del view, frame, root
        gc.collect()
        assert reference() is None, "Stats fixture retained its destroyed Tk view"


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
    assert tab.captions["bullish"].get() == "0 / 0 scored"
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


def test_yg_stats_identifies_raw_probability_target(tab, tmp_path):
    from ml.gameplan_probability_target import RAW_DIRECTION_TARGET
    row = forecast(change=.0005, probability=.9, direction="BULLISH")
    row.update(probability_target_contract=RAW_DIRECTION_TARGET, gameplan_variant="YG",
               model_observed_target=1, model_brier_score=.01)
    write_review(tmp_path, [row], version="yg")
    tab.set_review(load_gameplan_stats(tmp_path))
    assert tab.heading.cget("text") == "Yung Gameplan (YG) Stats"
    assert "return above 0.00%" in tab._probability_definition()
    assert tab.values["brier"].get() == "0.010"


def test_stats_details_show_frozen_policy_and_definitions_cover_every_trader(tab, tmp_path, monkeypatch):
    from ml.stock_direction_policy import STOCK_DIRECTION_POLICY_VERSION
    row = forecast(probability=.4872)
    row.update(direction_policy_version=STOCK_DIRECTION_POLICY_VERSION,
               direction_up_threshold=.5001, direction_down_threshold=.4999)
    write_review(tmp_path, [row], version="exact-policy")
    tab.set_review(load_gameplan_stats(tmp_path))
    shown = []
    monkeypatch.setattr(tab, "_text_dialog", lambda title, text: shown.append(text))
    tab._show_outcome("AAPL", 0, tab.review.outcomes[0])
    assert "BUY at 50.01% or higher; SELL at 49.99% or lower; HOLD between" in shown[-1]
    assert STOCK_DIRECTION_POLICY_VERSION in shown[-1]
    tab.show_definitions()
    assert "both 15-minute traders" in shown[-1]
    assert "original saved calls without relabeling" in shown[-1]


def test_stats_detail_explains_unrounded_probability_inside_exact_hold_gap(tab, tmp_path, monkeypatch):
    from ml.stock_direction_policy import STOCK_DIRECTION_POLICY_VERSION
    row = forecast(probability=.500099, direction="NO_EDGE")
    row.update(direction_policy_version=STOCK_DIRECTION_POLICY_VERSION,
               direction_up_threshold=.5001, direction_down_threshold=.4999)
    write_review(tmp_path, [row], version="unrounded-policy")
    tab.set_review(load_gameplan_stats(tmp_path))
    shown = []
    monkeypatch.setattr(tab, "_text_dialog", lambda title, text: shown.append(text))
    tab._show_outcome("AAPL", 0, tab.review.outcomes[0])
    assert "Saved call: Neutral" in shown[-1]
    assert "Saved model probability: 50.01%" in shown[-1]
    assert "Original unrounded P(up): 50.0099% (the exact policy uses this value)" in shown[-1]


def test_date_change_clears_old_values_and_loads_requested_publication(tab, tmp_path):
    write_review(tmp_path, [forecast(session="2026-09-10")], session="2026-09-10", latest=False)
    tab.session.set("2026-09-10")
    tab._date_changed()
    assert tab.review is None
    assert tab.values["accuracy"].get() == "—"
    _finish_refresh(tab)
    assert tab.review.session == "2026-09-10"
    assert tab.values["accuracy"].get() == "100.0%"


def test_default_refresh_follows_newly_published_session(tab, tmp_path):
    assert tab.review.session == "2026-09-11"
    newer = write_review(tmp_path, [forecast(session="2026-09-14", change=.01)], session="2026-09-14")
    tab.refresh()
    _finish_refresh(tab)
    assert tab.review.session == tab.session.get() == "2026-09-14"
    assert tab.review.run_directory == newer
    assert tab.values["accuracy"].get() == "0.0%"


def test_no_preopen_forecasts_explains_empty_review_and_preserves_history(tab, tmp_path):
    write_review(tmp_path, [], session="2026-09-14", coverage_status="NO_SAVED_INDEPENDENT_GAMEPLAN")
    tab.refresh()
    _finish_refresh(tab)
    assert tab.review.session == "2026-09-14" and tab.review.outcomes == ()
    assert "no eligible forecasts were saved before the 04:00 Pacific opening" in tab.status.get()
    assert all(value.get() == "—" for value in tab.values.values())
    assert "2026-09-11" in tab.date_box.cget("values")
    tab.session.set("2026-09-11")
    tab._date_changed()
    _finish_refresh(tab)
    assert tab.review.session == "2026-09-11"
    assert tab.values["accuracy"].get() == "50.0%"
    tab.follow_latest()
    _finish_refresh(tab)
    assert tab.review.session == "2026-09-14" and tab.review.outcomes == ()
    assert "Later forecasts are not counted as pre-open predictions" in tab.status.get()


def test_historical_selection_stays_pinned_until_latest_session_clicked(tab, tmp_path):
    write_review(tmp_path, [forecast(session="2026-09-10")], session="2026-09-10", latest=False)
    tab.session.set("2026-09-10")
    tab.date_box.event_generate("<<ComboboxSelected>>")
    _finish_refresh(tab)
    write_review(tmp_path, [forecast(session="2026-09-14", change=.01)], session="2026-09-14")
    tab.refresh_button.invoke()
    _finish_refresh(tab)
    assert tab.review.session == tab.session.get() == "2026-09-10"
    assert tab.values["accuracy"].get() == "100.0%"
    tab.latest_button.invoke()
    _finish_refresh(tab)
    assert tab.review.session == tab.session.get() == "2026-09-14"
    assert tab.values["accuracy"].get() == "0.0%"
    write_review(tmp_path, [forecast(session="2026-09-15")], session="2026-09-15")
    tab.refresh()
    _finish_refresh(tab)
    assert tab.review.session == tab.session.get() == "2026-09-15"
    assert tab.values["accuracy"].get() == "100.0%"


def test_latest_session_ignores_late_reply_from_historical_selection(tab, tmp_path, monkeypatch):
    old = tab.review
    write_review(tmp_path, [forecast(session="2026-09-14", change=.01)], session="2026-09-14")
    newest = load_gameplan_stats(tmp_path)
    # Control reply arrival without starting another thread; the real button
    # still creates the request that must invalidate the historical response.
    monkeypatch.setattr(stats_ui.threading, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: None))
    tab.session.set("2026-09-11")
    tab._date_changed()
    historical_request = tab._request_id
    tab.latest_button.invoke()
    tab._messages.put((tab._request_id, ("2026-09-14", "2026-09-11"), newest, None))
    tab._messages.put((historical_request, ("2026-09-11",), old, None))
    _finish_refresh(tab)
    assert tab.review.session == tab.session.get() == "2026-09-14"
    assert tab.values["accuracy"].get() == "0.0%"


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
    photo = tk.PhotoImage(master=tab.root, width=1, height=1)
    name, reference = str(photo), weakref.ref(photo)
    tab._photos["cleanup-probe"] = photo
    del photo
    tab.parent.destroy()
    assert tab._closed
    assert not tab._photos and reference() is None
    assert name not in tab.root.tk.call("image", "names")
    jobs = tab.root.tk.call("after", "info")
    assert tab._poll_job not in jobs and tab._auto_job not in jobs


def execution_snapshot(*, state="available", session="2026-09-11"):
    stamp = f"{session}T16:10:00-07:00"
    parent = {"order_ref": "parent", "symbol": "AAPL", "side": "BUY", "requested_quantity": 5,
              "filled_quantity": 2, "average_fill_price": "18.25", "status": "cancelled",
              "lifecycle_status": "CANCELLED", "submitted_at": stamp, "last_observed_at": stamp,
              "cancel_requested_at": stamp, "fill_details_complete": True,
              "fills": [{"quantity": 2, "price": "18.25", "executed_at": stamp}],
              "reasons": ["Recorded timeout cancellation"], "retry_parent_ref": None,
              "retry_child_refs": ["replacement"], "session_scope": "submitted", "submission_state": "submitted"}
    child = {**parent, "order_ref": "replacement", "filled_quantity": 3, "requested_quantity": 3,
             "average_fill_price": "18.50", "status": "fully_filled", "lifecycle_status": "FILLED",
             "retry_parent_ref": "parent", "retry_child_refs": [], "reasons": [],
             "fills": [{"quantity": 3, "price": "18.50", "executed_at": stamp}]}
    working = {**parent, "order_ref": "working", "symbol": "NVDA", "status": "working",
               "lifecycle_status": "WORKING", "filled_quantity": 0, "average_fill_price": None,
               "fills": [], "retry_child_refs": [], "reasons": []}
    stopped = {**working, "order_ref": "stopped", "status": "not_submitted", "lifecycle_status": "REJECTED",
               "submission_state": "not_submitted", "session_scope": "not_submitted", "submitted_at": None,
               "reasons": ["Local buying-power stop"]}
    carry = {**child, "order_ref": "carry", "session_scope": "carryover", "retry_parent_ref": None}
    counts = {"submitted_attempts": 3, "fully_filled": 1, "partially_filled": 0, "working": 1,
              "cancelled": 1, "rejected": 0, "unknown": 0, "not_submitted": 1,
              "unknown_submission": 0, "carryover_attempts": 1}
    return GameplanExecutionSnapshot(session, state, "Saved evidence" if state == "available" else f"Execution records are {state}",
        generated_at=datetime.now(timezone.utc), observed_at=datetime.fromisoformat(stamp),
        report={"counts": counts, "orders": [parent, child, working, stopped, carry],
                "coverage": {"reasons": ["Only this Gameplan ledger is represented."]}})


def test_approved_card_order_and_direction_counts_reconcile(tab):
    assert tab.card_names == ("accuracy", "bullish", "bearish", "brier")
    metrics = tab.review.metrics("all")
    assert metrics.correct == metrics.bullish_correct + metrics.bearish_correct
    assert metrics.scored == metrics.bullish_scored + metrics.bearish_scored
    for key, correct, scored in (("accuracy", metrics.correct, metrics.scored),
                                ("bullish", metrics.bullish_correct, metrics.bullish_scored),
                                ("bearish", metrics.bearish_correct, metrics.bearish_scored)):
        assert tab.values[key].get() == stats_ui.percent(correct / scored)
        assert tab.captions[key].get() == f"{correct} / {scored} scored"
    assert tab.captions["brier"].get() == "Lower is better"
    assert metrics.evaluated > metrics.scored  # Evaluated neutral still contributes to Brier.


def test_execution_filters_preserve_distribution_and_cancelled_partial_details(tab):
    snapshot = execution_snapshot()
    tab.set_execution(snapshot)
    view = tab.execution
    assert len(view.tree.get_children()) == 3
    assert view.total_label.cget("text").startswith("3 recorded submitted")
    assert view.count_labels["cancelled"].cget("text") == "1  Cancelled"
    assert view.count_labels["partially_filled"].cget("text") == "0  Partially filled"
    view.tree.selection_set("0")
    view._select_order()
    detail = view.detail_text.get("1.0", "end")
    assert "2 / 5 shares" in detail and "Actual average fill price: $18.25" in detail
    assert "Cancel requested:" in detail and "Recorded timeout cancellation" in detail
    assert "Retry / replacement children: AAPL 16:10 · Fully filled · 3 / 3 shares [replacemen]" in detail
    view.status_filter.set("Cancelled")
    view.render()
    assert len(view.tree.get_children()) == 1
    assert view.total_label.cget("text").startswith("3 recorded submitted")
    assert snapshot.orders[0]["filled_quantity"] == 2
    view.company.set("NVDA")
    view.status_filter.set("All statuses")
    view.render()
    assert view.scope_label.cget("text") == "Company scope · NVDA · selected session"
    assert view.total_label.cget("text").startswith("1 recorded submitted")
    assert view.count_labels["rejected"].cget("text") == "0  Rejected"
    view.scope_filter.set("Not submitted")
    view.render()
    assert len(view.tree.get_children()) == 1
    view.tree.selection_set("0")
    view._select_order()
    assert "Local buying-power stop" in view.detail_text.get("1.0", "end")
    assert "Submitted: Not recorded" in view.detail_text.get("1.0", "end")
    assert "1 carryover records" not in view.coverage_label.cget("text")  # This company has none.


@pytest.mark.parametrize("state", ["missing", "unreadable"])
def test_execution_unavailable_has_no_fabricated_zero_totals(tab, state):
    tab.set_execution(GameplanExecutionSnapshot(tab.review.session, state, "Execution evidence unavailable"))
    view = tab.execution
    assert view.total_label.cget("text").startswith("— submitted")
    assert all(widget.cget("text").startswith("—") for widget in view.count_labels.values())
    assert "does not establish that no orders occurred" in view.detail_text.get("1.0", "end")
    assert tab.values["accuracy"].get() == "50.0%"


@pytest.mark.parametrize("state", ["stale", "incomplete"])
def test_execution_stale_or_incomplete_retains_observed_evidence(tab, state):
    tab.set_execution(execution_snapshot(state=state))
    assert state in tab.execution.state_label.cget("text")
    assert "16:10:00 PDT" in tab.execution.observation_label.cget("text")
    assert "Saved evidence only" in tab.execution.observation_label.cget("text")
    assert len(tab.execution.tree.get_children()) == 3


def test_execution_loader_failure_cannot_block_prediction_load(tab, monkeypatch):
    def failure(*_args):
        raise OSError("Fixture execution reader failed")
    monkeypatch.setattr(stats_ui, "load_gameplan_execution", failure)
    tab.refresh()
    _finish_refresh(tab)
    assert tab.values["accuracy"].get() == "50.0%"
    assert tab.execution.snapshot.state == "unreadable"
    assert "could not be read" in tab.execution.state_label.cget("text")


def test_execution_only_session_remains_selectable_when_prediction_review_missing(tab, monkeypatch):
    snapshot = execution_snapshot(session="2026-09-15")
    monkeypatch.setattr(stats_ui, "execution_sessions", lambda *_args: (snapshot.session,))
    monkeypatch.setattr(stats_ui, "load_gameplan_execution", lambda *_args: snapshot)
    tab.refresh()
    _finish_refresh(tab)
    assert tab.review is None
    assert tab.session.get() == snapshot.session
    assert snapshot.session in tab.date_box.cget("values")
    assert "2026-09-11" in tab.date_box.cget("values")
    assert len(tab.execution.tree.get_children()) == 3
    assert all(value.get() == "—" for value in tab.values.values())


def test_company_selection_connects_prediction_and_execution_exploration(tab):
    tab.set_execution(execution_snapshot())
    tab._step_symbol(1)
    assert tab.selected_symbol == tab.execution.company.get() == "NVDA"
    assert tab.execution.total_label.cget("text").startswith("1 recorded submitted")


def test_company_execution_coverage_uses_its_observation_and_unknown_records(tab):
    snapshot = execution_snapshot()
    nvda = snapshot.report["orders"][2]
    nvda["last_observed_at"] = "2026-09-11T09:00:00-07:00"
    stopped = snapshot.report["orders"][3]
    stopped["last_observed_at"] = None
    unknown = {**nvda, "order_ref": "unknown", "session_scope": "unattributed",
               "status": "unknown", "submission_state": "unknown", "last_observed_at": None}
    snapshot.report["orders"].append(unknown)
    snapshot.report["counts"]["unknown_submission"] = 1
    tab.set_execution(snapshot)
    tab.execution.set_company("NVDA")
    assert tab.execution._counts()["unknown_submission"] == 1
    assert "1 records have uncertain submission evidence" in tab.execution.coverage_label.cget("text")
    observed = tab.execution.observation_label.cget("text")
    assert "NVDA last recorded broker observation: Sep 11, 09:00:00 PDT" in observed
    assert "16:10" not in observed
    tab.execution.set_company("All companies")
    assert "16:10:00 PDT" in tab.execution.observation_label.cget("text")


@pytest.mark.parametrize("size", ["1180x760", "1586x990"])
def test_split_layout_keeps_all_companies_and_hour_cells_accessible(tab, tmp_path, size):
    symbols = [f"S{i:02}" for i in range(22)]
    write_review(tmp_path, [forecast(symbol=symbol) for symbol in symbols], version="many")
    tab.set_review(load_gameplan_stats(tmp_path))
    tab.set_execution(execution_snapshot())
    tab.root.geometry(size)
    tab.root.deiconify()
    tab.root.update()
    assert tab.execution.frame.grid_info()["column"] == 1
    assert abs(tab.prediction_panel.winfo_width() - tab.execution.frame.winfo_width()) <= 12
    assert tab.score_canvas._content_height > tab.score_canvas.winfo_height()
    assert tab.grid_canvas._content_height > tab.grid_canvas.winfo_height()
    assert len(tab._cell_bounds) == 22 * 13
    assert float(tab.grid_canvas.cget("scrollregion").split()[2]) <= tab.grid_canvas.winfo_width() + 1
    tab.score_canvas.yview_moveto(1)
    tab.grid_canvas.yview_moveto(1)
    tab.root.update()
    assert tab.grid_canvas.yview()[1] == 1.0
    assert tab.score_canvas.yview()[1] == 1.0
