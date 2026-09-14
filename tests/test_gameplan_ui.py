from __future__ import annotations

import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

import pytest

import app.ui.gameplan as plan_ui
from app.ui.gameplan_data import GameplanError, load_gameplan
from gameplan_fixture import unavailable_payload, write_plan


@pytest.fixture
def tab(tmp_path):
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk unavailable: {exc}")
    root.withdraw()
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)
    view = plan_ui.GameplanTab(root, frame, datastore_root=tmp_path, auto_load=False)
    write_plan(tmp_path)
    view.set_plan(load_gameplan(tmp_path))
    yield view
    root.destroy()


def finish_refresh(tab):
    deadline = time.monotonic() + 3
    while tab._loading and time.monotonic() < deadline:
        tab.root.update()
        time.sleep(.01)
    assert not tab._loading


def test_planned_and_execution_mid_prices_are_adjacent_in_both_views(tab):
    from dataclasses import replace
    from app.ui.gameplan_data import ExecutionQuote
    forecast=tab.plan.forecast(tab.visible_rows[0].forecast_id)
    quote=ExecutionQuote(forecast.forecast_id,127.22,forecast.start,127.24,15.)
    tab.set_plan(replace(tab.plan,execution_quotes=(quote,)))
    for view in ('trades','forecasts'):
        tab.view.set(view)
        tab.render()
        headers=[tab.table_header.itemcget(i,'text') for i in tab.table_header.find_all() if tab.table_header.type(i)=='text']
        assert headers[headers.index('Planning price')+1] == 'Execution mid'
        cells=[tab.table.itemcget(i,'text') for i in tab.table.find_all() if tab.table.type(i)=='text']
        assert '$127.22' in cells


def test_filtered_cards_table_and_forecasts_have_consistent_scope(tab):
    assert tab.values["entries"].get() == "2 buys"
    assert tab.values["exits"].get() == "1 sell"
    assert tab.captions["exits"].get() == "2 remaining horizon expiries"
    assert len(tab.visible_rows) == 5
    tab.horizon.set("1 hour")
    tab.horizon_box.event_generate("<<ComboboxSelected>>")
    assert tab.view.get() == "forecasts"
    assert len(tab.visible_rows) == 2
    assert tab.selected_row is not None
    assert tab.values["entries"].get() == "0 buys"
    assert "No projected trades for these filters" in tab.table_note.cget("text")
    assert {row.action for row in tab.visible_rows} == {"HOLD", "CONTEXT"}
    tab.company.set("NVDA")
    tab.render()
    assert not tab.visible_rows
    assert tab.values["coverage"].get() == "0 horizons"


def test_company_dropdown_shows_saved_forecasts_when_only_other_symbols_have_trades(tab):
    tab.company.set("AAPL")
    tab.horizon.set("1 hour")
    tab.company_box.event_generate("<<ComboboxSelected>>")
    assert tab.view.get() == "forecasts"
    assert tab.visible_rows == tab.plan.rows("1h", "AAPL")
    assert tab.selected_row.symbol == "AAPL"
    assert tab.selected_row.action == "CONTEXT"
    assert tab.trade_button.cget("text") == "Trades 0"
    assert tab.forecast_button.cget("text") == "All forecasts 2"
    assert "disabled" not in tab.details_button.state()
    assert tab.values["entries"].get() == "0 buys"
    # An explicit request for the empty trade view still shows the explanation.
    tab.trade_button.invoke()
    assert tab.view.get() == "trades" and not tab.visible_rows
    assert any("No projected actions" in tab.table.itemcget(item, "text")
               for item in tab.table.find_all() if tab.table.type(item) == "text")
    # A different company with a real trade keeps the requested trade view.
    tab.company.set("NVDA")
    tab.horizon.set("1 week")
    tab.company_box.event_generate("<<ComboboxSelected>>")
    assert tab.view.get() == "trades"
    assert tab.visible_rows == tab.plan.trades("1w", "NVDA")
    assert tab.selected_row.action == "BUY"


def test_horizon_card_falls_back_to_forecasts_without_changing_trade_counts(tab):
    tab._choose_horizon("1h")
    assert tab.view.get() == "forecasts"
    assert tab.visible_rows == tab.plan.rows("1h")
    assert tab.values["entries"].get() == "0 buys"
    assert tab.values["exits"].get() == "0 sells"


def test_selection_does_not_filter_global_agenda_and_details_use_selected_forecast(tab, monkeypatch):
    shown = []
    monkeypatch.setattr(tab, "_text_dialog", lambda title, text: shown.append((title, text)))
    before = tab.visible_rows
    tab._step_row(1)
    assert tab.selected_row.symbol == "NVDA"
    assert tab.company.get() == "All companies"
    assert tab.visible_rows == before
    assert tab.probability.cget("text") == "70.00%"
    tab.show_details()
    assert "Sep 18, 2026 17:00 PDT" in shown[-1][1]
    assert "Saved action: BUY" in shown[-1][1]
    assert "9999" not in shown[-1][1]
    top, bottom, row = tab._row_bounds[0]
    tab._select_row(SimpleNamespace(y=(top+bottom)/2))
    assert tab.selected_row.symbol == "AAPL"
    assert tab.probability.cget("text") == "54.36%"


def test_later_existing_expiry_never_borrows_a_current_forecast_or_price(tab, monkeypatch):
    tab.company.set("GOOG")
    tab.render()
    assert tab.selected_row.action == "EXPIRY"
    assert tab.probability.cget("text") == "—"
    assert "2 shares already reserved" in tab.selection_note.cget("text")
    shown = []
    monkeypatch.setattr(tab, "_text_dialog", lambda title, text: shown.append(text))
    tab.show_details()
    assert "Sep 15, 2026 07:00 PDT" in shown[-1]
    assert "Unreserved allocation: 3 shares" in shown[-1]
    assert "Planning price: —" in shown[-1]
    assert "Published P(up)" not in shown[-1]


def test_clicking_horizon_filters_schedule_and_keeps_overview_across_horizons(tab):
    tab._choose_horizon("1w")
    assert tab.horizon.get() == "1 week"
    assert {row.horizon for row in tab.visible_rows} == {"1w"}
    assert tab.horizon_widgets[0][1].cget("text") == "1 entry window"
    assert tab.horizon_widgets[3][0].cget("highlightbackground") == plan_ui.CYAN


def test_failed_refresh_clears_old_data_and_disables_report_and_details(tab, monkeypatch):
    def fail(*_args):
        raise GameplanError("Fixture integrity mismatch")
    monkeypatch.setattr(plan_ui, "load_gameplan", fail)
    tab.refresh()
    finish_refresh(tab)
    assert tab.plan is None and not tab.visible_rows
    assert all(value.get() == "—" for value in tab.values.values())
    assert "integrity mismatch" in tab.status.get()
    assert "disabled" in tab.report_button.state()
    assert "disabled" in tab.details_button.state()


def test_history_selection_stays_pinned_until_latest_plan_is_requested(tab, tmp_path):
    write_plan(tmp_path, session="2026-09-11", latest=False)
    tab.session.set("2026-09-11")
    tab._date_changed()
    assert tab.plan is None and not tab.visible_rows
    finish_refresh(tab)
    assert tab.plan.session == "2026-09-11"
    tab.refresh()
    finish_refresh(tab)
    assert tab.plan.session == "2026-09-11"
    tab.follow_latest()
    finish_refresh(tab)
    assert tab.plan.session == "2026-09-14"


def test_stale_background_result_cannot_replace_newer_choice(tab, tmp_path):
    latest = tab.plan
    write_plan(tmp_path, session="2026-09-11", latest=False)
    historical = load_gameplan(tmp_path, "2026-09-11")
    tab._request_id = 2
    tab._loading = True
    tab._messages.put((2, ("2026-09-14", "2026-09-11"), historical, None))
    tab._messages.put((1, ("2026-09-14",), latest, None))
    finish_refresh(tab)
    assert tab.plan.session == "2026-09-11"


def test_refresh_preserves_selection_when_saved_session_is_unchanged(tab):
    tab._step_row(1)
    selected = tab.selected_key
    tab.refresh()
    finish_refresh(tab)
    assert tab.selected_key == selected
    assert tab.selected_row.symbol == "NVDA"


def test_open_gameplan_opens_the_displayed_immutable_report(tab, monkeypatch):
    opened = []
    if plan_ui.os.name == "nt":
        monkeypatch.setattr(plan_ui.os, "startfile", lambda path: opened.append(path))
    else:
        monkeypatch.setattr(plan_ui.webbrowser, "open", lambda path: opened.append(path))
    tab.open_report()
    assert opened == [str(tab.plan.report_path) if plan_ui.os.name == "nt" else tab.plan.report_path.as_uri()]
    assert "gameplan-trade-plan-runs" in opened[0]


def test_destroy_cancels_callbacks_and_ignores_completions(tab):
    tab.parent.destroy()
    assert tab._closed
    jobs = tab.root.tk.call("after", "info")
    assert tab._poll_job not in jobs and tab._auto_job not in jobs


def test_cross_session_clock_and_fractional_share_formatting():
    from datetime import datetime
    assert plan_ui.shares(0) == "0"
    assert plan_ui.shares(-17) == "17"
    assert plan_ui.shares(.5) == "0.5"
    assert plan_ui.money(None) == "—"
    stamp = datetime.fromisoformat("2026-09-15T07:00:00-07:00")
    assert "Sep 15" in plan_ui.clock_text(stamp, "2026-09-14")


def test_available_projection_keeps_carried_price_notice_visible(tab, tmp_path):
    write_plan(tmp_path, report_updates={"reference_completion": {"references": {
        "AAPL|2026-09-14": {"status": "AVAILABLE_SYNTHETIC", "symbol": "AAPL", "gap_minutes": 143,
                              "observed_at": "2026-09-11T14:37:00-07:00", "effective_at": "2026-09-11T17:00:00-07:00"}}}})
    tab.refresh()
    finish_refresh(tab)
    assert "AAPL (143 min)" in tab.status.get()
    assert tab.trade_button.cget("text") == "Trades 5"
    assert tab.values["entries"].get() == "2 buys"


def test_unavailable_projection_opens_forecasts_and_keeps_filters_and_details(tab, tmp_path, monkeypatch):
    rows, ledger, report = unavailable_payload()
    write_plan(tmp_path, rows=rows, ledger=ledger, report_updates=report)
    tab.refresh()
    finish_refresh(tab)
    assert tab.view.get() == "forecasts"
    assert len(tab.visible_rows) == 7
    assert "Cash projection unavailable" in tab.status.get()
    assert tab.values["coverage"].get() == "4 horizons"
    assert tab.captions["coverage"].get() == "7 forecasts · 3 companies"
    for key in ("first", "entries", "exits"):
        assert tab.values[key].get() == "—"
        assert tab.captions[key].get() == "Cash projection unavailable"
    assert tab.trade_button.cget("text") == "Trades unavailable"
    assert "disabled" not in tab.report_button.state()
    tab.company.set("NVDA")
    tab.horizon.set("1 week")
    tab.render()
    assert len(tab.visible_rows) == 1
    assert tab.selected_row.action == "UNAVAILABLE"
    assert tab.probability.cget("text") == "70.00%"
    shown = []
    monkeypatch.setattr(tab, "_text_dialog", lambda title, text: shown.append(text))
    tab.show_details()
    assert "Direction-based shares: —" in shown[-1]
    assert "Projected action: Unavailable" in shown[-1]
    assert "Sep 18, 2026 17:00 PDT" in shown[-1]
    assert "9999" not in shown[-1]
    assert all(widgets[3].cget("text") == "Cash projection unavailable" for widgets in tab.horizon_widgets)
    tab.refresh()
    finish_refresh(tab)
    assert tab.selected_row.symbol == "NVDA"
    assert tab.horizon.get() == "1 week"
    tab.view.set("trades")
    tab.render()
    assert not tab.visible_rows
    assert "Cash projection unavailable" in tab.table_note.cget("text")
    assert "No saved plan" not in tab.table_note.cget("text")
    write_plan(tmp_path)
    tab.refresh()
    finish_refresh(tab)
    assert tab.trade_button.cget("text") == "Trades 2"
    assert tab.values["entries"].get() == "1 buy"
    assert "Cash projection unavailable" not in tab.status.get()
