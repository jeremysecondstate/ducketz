from __future__ import annotations

import threading
import time
import tkinter as tk
from dataclasses import replace
from tkinter import ttk

import pytest

from app.ui.hyper_workspace import HyperWorkspace, MINT
from app.services.hyperliquid_paper_view import PaperViewSnapshot, SourceState
from visual_hyper_workspace_fixture import FixtureService, _snapshot


@pytest.fixture(scope="module")
def root():
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk is unavailable: {exc}")
    window.withdraw()
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass


@pytest.fixture
def workspace(root, monkeypatch):
    # H.Y.P.E.R. is an observer. No view action may initialize a ledger, reach
    # the exchange or start a process, including merely switching to Powder.
    import subprocess
    import requests
    from ml.hyperliquid_paper_ledger import PaperLedger

    def forbidden(*args, **kwargs):
        pytest.fail("H.Y.P.E.R. attempted network, execution or a runtime mutation")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(PaperLedger, "__init__", forbidden)
    window = tk.Toplevel(root)
    window.geometry("1706x1000")
    parent = ttk.Frame(window)
    parent.pack(fill=tk.BOTH, expand=True)
    view = HyperWorkspace(window, parent, service=FixtureService(), auto_load=False)
    view.show_snapshot(_snapshot())
    window.update()
    yield view, window, parent
    try:
        window.destroy()
    except tk.TclError:
        pass


def _visible_rows(view):
    return [view._rows_by_id[item] for item in view.tree.get_children()]


def _pump_until(window, condition, *, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        window.update()
        if condition():
            return
        time.sleep(.01)
    assert condition(), "Tk did not receive the background read before timeout"


def test_headline_uses_opening_baseline_pnl_and_preserves_inherited_position(workspace):
    view, _, _ = workspace
    assert "42,112.86" in view.metric_values["equity"].cget("text")
    assert "34.58" in view.metric_values["pnl"].cget("text")
    assert "795.93" not in view.metric_values["pnl"].cget("text")
    assert not any(row.get("passive") for row in _visible_rows(view))
    view.include_passive.set(True)
    rows = _visible_rows(view)
    inherited = next(row for row in rows if row.get("passive"))
    assert inherited["account"] == "alex"
    assert inherited["kind"] == "spot"


def test_refresh_retains_filters_and_stable_row_selection(workspace):
    view, window, _ = workspace
    view.account_filter.set("Alex")
    view.asset_filter.set("HYPE")
    view.qualification_filter.set("All models")
    view.include_passive.set(True)
    view.chart_metric.set("Drawdown")
    view.chart_range.set("24H")
    view._render()
    rows = view.tree.get_children()
    assert len(rows) == 2
    selected = next(item for item in rows if view._rows_by_id[item]["kind"] == "perp")
    view.tree.selection_set(selected)
    view.tree.focus(selected)
    before = view._rows_by_id[selected]

    snapshot = _snapshot()
    updated = [{**row, "mark_price": row["mark_price"] + .01} for row in snapshot.positions]
    view.show_snapshot(replace(snapshot, positions=updated))
    window.update()

    assert view.account_filter.get() == "Alex"
    assert view.asset_filter.get() == "HYPE"
    assert view.qualification_filter.get() == "All models"
    assert view.include_passive.get()
    assert view.chart_metric.get() == "Drawdown"
    assert view.chart_range.get() == "24H"
    assert view.tree.selection() == (selected,)
    assert view._rows_by_id[selected]["account"] == before["account"]
    assert view._rows_by_id[selected]["mark_price"] == pytest.approx(before["mark_price"] + .01)


@pytest.mark.parametrize("journal", ["Decisions", "Fills"])
def test_account_asset_and_qualification_filters_apply_to_each_journal(workspace, journal):
    view, _, _ = workspace
    view.view.set(journal)
    view.account_filter.set("Clear Pond")
    view.asset_filter.set("ETH")
    view.qualification_filter.set("Research")
    view._render()
    rows = _visible_rows(view)
    assert len(rows) == 1
    assert rows[0]["account"] == "clearpond"
    assert rows[0]["coin"] == "ETH"
    assert rows[0]["qualified"] is False


def test_position_filters_do_not_invent_qualification_from_current_forecasts(workspace):
    view, _, _ = workspace
    view.account_filter.set("Jeremy")
    view.asset_filter.set("BTC")
    view._render()
    assert len(_visible_rows(view)) == 1
    assert _visible_rows(view)[0]["account"] == "jeremy"
    assert _visible_rows(view)[0]["coin"] == "BTC"
    view.qualification_filter.set("Qualified")
    view._render()
    # A current qualified BTC forecast does not establish the provenance of
    # inventory accumulated across earlier fills or inherited opening positions.
    assert not _visible_rows(view)


@pytest.mark.parametrize("journal", ["Decisions", "Fills"])
def test_partial_fill_inspector_distinguishes_requested_executed_and_unfilled(workspace, journal):
    view, window, _ = workspace
    view.view.set(journal)
    view.account_filter.set("Alex")
    view.asset_filter.set("HYPE")
    view._render()
    item = view.tree.get_children()[0]
    view.tree.selection_set(item)
    view.tree.event_generate("<<TreeviewSelect>>")
    window.update()
    # Inspect the user-facing explanation, rather than the raw JSON record.
    description = view.detail.get("1.0", "end").split("SAVED RECORD")[0]
    assert "SIMULATED EXECUTION" in description
    assert "Requested -4" in description
    assert "Executed -3.407" in description
    assert "Unfilled remainder -0.593" in description
    assert "Partial" in description


def test_hold_reason_is_explained_without_an_execution_claim(workspace):
    view, window, _ = workspace
    view.view.set("Decisions")
    view.account_filter.set("Clear Pond")
    view.asset_filter.set("ETH")
    view._render()
    item = view.tree.get_children()[0]
    view.tree.selection_set(item)
    view.tree.event_generate("<<TreeviewSelect>>")
    window.update()
    description = view.detail.get("1.0", "end").split("SAVED RECORD")[0]
    assert "Below entry threshold" in description
    assert "SIMULATED EXECUTION" not in description


@pytest.mark.parametrize("account,key,expected_count", [("Alex", "alex", 2), ("Jeremy", "jeremy", 1)])
def test_transfer_account_filter_includes_both_sides(workspace, account, key, expected_count):
    view, _, _ = workspace
    view.view.set("Transfers")
    view.account_filter.set(account)
    view._render()
    rows = _visible_rows(view)
    assert len(rows) == expected_count
    assert all(key in (row["from_account"], row["to_account"]) for row in rows)


def test_powder_has_no_paper_balances_or_journal_and_labels_forecast_preview(workspace):
    view, window, _ = workspace
    view.include_passive.set(True)
    selected = view.tree.get_children()[0]
    view.tree.selection_set(selected)
    view.tree.event_generate("<<TreeviewSelect>>")
    window.update()
    assert "Alex" in view.detail.get("1.0", "end")
    view.mode.set("Powder")
    view._choose_mode()
    window.update()
    for journal in ("Positions", "Decisions", "Fills", "Transfers"):
        view.view.set(journal)
        view._render()
        assert not view.tree.get_children()
    for label in view.metric_values.values():
        assert not any(character.isdigit() for character in label.cget("text"))
    for widgets in view.account_values.values():
        for key in ("equity", "pnl", "free_cash", "gross_exposure"):
            assert not any(character.isdigit() for character in widgets[key].cget("text"))
    assert view.badge.cget("text") == "REAL MONEY"
    assert view.mode_status.cget("text") == "Not connected"
    assert not view._chart_points
    assert "Not connected" in view.detail.get("1.0", "end")
    assert "Alex" not in view.detail.get("1.0", "end")
    caption = view.forecast_caption.cget("text").casefold()
    assert "shared" in caption
    assert "preview" in caption
    view.mode.set("Paper")
    view._choose_mode()
    view.view.set("Positions")
    view._render()
    assert len(view.tree.get_children()) == len(_snapshot().positions)


def test_powder_observations_and_fills_are_separate_from_paper(workspace):
    from app.services.hyperliquid_powder_view import project_powder
    view, window, _ = workspace
    paper = _snapshot()
    paper.powder = project_powder({"latest_observation": {"observed_at": 1000,
        "accounts": {"clearpond": {"equity": 800, "available_cash": 600, "gross": 200,
            "positions": {"HYPE": {"quantity": 5, "mark": 40, "kind": "spot"}}}}},
        "fills": [{"account": "clearpond", "symbol": "HYPE", "tid": 7, "quantity": 1,
                   "price": 40, "fee": .001, "fee_token": "HYPE", "time": 1000000}]},
        paper=paper, now=1001)
    view.show_snapshot(paper)
    view.mode.set("Powder")
    window.update()
    assert view.metric_values["equity"].cget("text") == "$800.00"
    assert view.metric_values["pnl"].cget("text") == "—"
    assert len(view.tree.get_children()) == 1
    view.view.set("Fills")
    view._render()
    assert "0.001 HYPE" in view.tree.item(view.tree.get_children()[0], "values")
    assert "Not connected" not in view.detail.get("1.0", "end")
    view.mode.set("Paper")
    assert "42,112.86" in view.metric_values["equity"].cget("text")


def test_missing_and_degraded_sources_remain_visible_without_fabricated_balances(workspace):
    view, _, _ = workspace
    view.show_snapshot(PaperViewSnapshot(
        observed_at_utc="2026-09-26T03:30:12+00:00",
        sources={"ledger": SourceState("Ledger", "missing"),
                 "paper": SourceState("Paper", "stale", "2026-09-26T00:00:00+00:00", 12612, 30),
                 "models": SourceState("Models", "stopped", "2026-09-26T02:00:00+00:00", 5412, 5),
                 "forecast:ETH": SourceState("ETH forecast", "partial")},
        warnings=("Paper ledger is missing; no balances have been inferred",),
    ))
    assert "missing" in view.alert.cget("text").lower()
    assert "stale" in view.source_labels["paper"][0].cget("text").lower()
    assert "stopped" in view.source_labels["models"][0].cget("text").lower()
    assert "partial" in view.source_labels["forecasts"][0].cget("text").lower()
    assert all(label.cget("text") == "—" for label in view.metric_values.values())
    assert not view.tree.get_children()


def test_compact_layout_keeps_panels_reachable_without_horizontal_clipping(workspace):
    view, window, _ = workspace
    assert view._layout_mode == "wide"
    window.geometry("1180x760")
    window.update()
    assert view._layout_mode == "compact"
    assert view.body.winfo_height() > view.canvas.winfo_height()
    for panel in (view.accounts_rail, view.center, view.right):
        assert panel.winfo_x() >= 0
        assert panel.winfo_x() + panel.winfo_width() <= view.body.winfo_width() + 2
    view.canvas.yview_moveto(1)
    window.update_idletasks()
    assert view.canvas.yview()[1] == pytest.approx(1)


def test_drawdown_chart_uses_transfer_adjusted_presampling_drawdown(workspace):
    view, _, _ = workspace
    snapshot = _snapshot()
    # The outgoing transfer reduces marked account equity by $100. A prior
    # performance peak omitted by downsampling still makes the true drawdown
    # 2.5%; neither raw equity nor the two visible P/L points recover that.
    history = [{"account": "alex", "timestamp_utc": "2026-09-26T02:30:00+00:00",
                "equity": 1000, "total_pnl": 0, "drawdown_fraction": 0},
               {"account": "alex", "timestamp_utc": "2026-09-26T03:30:00+00:00",
                "equity": 900, "total_pnl": 0, "drawdown_fraction": -.025}]
    view.show_snapshot(replace(snapshot, equity_history=history, history_sampled=True))
    view.account_filter.set("Alex")
    view.chart_metric.set("Drawdown")
    assert [value for _, value, _ in view._chart_series()] == [0, -2.5]
    view.chart_metric.set("P/L")
    assert [value for _, value, _ in view._chart_series()] == [0, 0]


class BlockingService:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.thread_ids = []

    def load_snapshot(self):
        self.calls += 1
        self.thread_ids.append(threading.get_ident())
        self.started.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("Test did not release the read worker")
        return _snapshot()


def test_refresh_is_bounded_and_applies_results_on_tk_thread(root):
    window = tk.Toplevel(root)
    parent = ttk.Frame(window)
    parent.pack(fill=tk.BOTH, expand=True)
    service = BlockingService()
    view = HyperWorkspace(window, parent, service=service, auto_load=False, refresh_ms=60_000)
    rendered_on = []
    original_show = view.show_snapshot

    def record_show(snapshot):
        rendered_on.append(threading.get_ident())
        original_show(snapshot)

    view.show_snapshot = record_show
    try:
        view.refresh()
        assert service.started.wait(timeout=1)
        assert view._loading
        for _ in range(8):
            view.refresh()
        window.update()
        assert service.calls == 1
        assert not rendered_on
        service.release.set()
        _pump_until(window, lambda: not view._loading)
        assert rendered_on == [threading.get_ident()]
        assert service.thread_ids[0] != threading.get_ident()
        view.refresh()
        _pump_until(window, lambda: service.calls == 2 and not view._loading)
        assert len(rendered_on) == 2
    finally:
        service.release.set()
        window.destroy()


def test_failed_refresh_preserves_last_observation_and_clears_loading(workspace):
    view, window, _ = workspace

    class UnavailableService:
        def load_snapshot(self):
            raise OSError("Sample ledger unavailable during refresh")

    # Use a dedicated view rather than replacing its private service field.
    frame = ttk.Frame(window)
    frame.pack(fill=tk.BOTH, expand=True)
    failing = HyperWorkspace(window, frame, service=UnavailableService(), auto_load=False)
    failing.show_snapshot(_snapshot())
    previous_rows = failing.tree.get_children()
    failing.refresh()
    _pump_until(window, lambda: not failing._loading)
    assert "42,112.86" in failing.metric_values["equity"].cget("text")
    assert failing.tree.get_children() == previous_rows
    assert "displaying previous snapshot" in failing.alert.cget("text")
    for title, _, _, _ in failing.source_labels.values():
        assert "Read failed" in title.cget("text")
        assert title.cget("fg") != MINT
    frame.destroy()


def test_destroy_during_read_discards_results_and_prevents_later_refresh(root):
    window = tk.Toplevel(root)
    parent = ttk.Frame(window)
    parent.pack(fill=tk.BOTH, expand=True)
    service = BlockingService()
    view = HyperWorkspace(window, parent, service=service, auto_load=False, refresh_ms=10)
    applied = []
    view.show_snapshot = applied.append
    try:
        view.refresh()
        assert service.started.wait(timeout=1)
        parent.destroy()
        assert view._closed
        assert not view._jobs
        service.release.set()
        view.refresh()
        # Process several would-be refresh intervals after destruction.
        deadline = time.monotonic() + .15
        while time.monotonic() < deadline:
            window.update()
            time.sleep(.01)
        assert service.calls == 1
        assert not applied
    finally:
        service.release.set()
        window.destroy()
