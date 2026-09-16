"""The Gameplan session run sheet, backed by immutable saved planning artifacts."""
from __future__ import annotations

import math
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from datetime import date, datetime
from pathlib import Path
from tkinter import messagebox, ttk

from app.ui.gameplan_data import (
    HORIZONS, PACIFIC, Gameplan, PlanForecast, PlannedAction, load_gameplan,
    plan_sessions, reason_text, SIGNAL_DRIVEN_HOLDING_POLICY,
)
from app.ui.gameplan_widgets import Tooltip, label, panel
from app.ui.theme import BACKGROUND, BORDER, DANGER, MUTED_TEXT, SUCCESS, SURFACE, SURFACE_ALT, TEXT, WARNING

CYAN = "#32d4ec"
TABLE = "#0c1929"
HORIZON_LABELS = {"All horizons": "all", "1 hour": "1h", "4 hours": "4h", "1 day": "1d", "1 week": "1w"}
HORIZON_NAMES = dict(zip(HORIZONS, ("1 hour", "4 hours", "1 day", "1 week")))
AUTO_REFRESH_MS = 5 * 60 * 1000
ACTION_COLORS = {"BUY": SUCCESS, "SELL": DANGER, "EXPIRY": WARNING, "HOLD": MUTED_TEXT,
                 "CONTEXT": "#87bafa", "UNAVAILABLE": WARNING}


def money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def shares(value: float | None) -> str:
    return "—" if value is None else f"{abs(value):,.6f}".rstrip("0").rstrip(".")


def counted(count: int, noun: str, plural: str | None = None) -> str:
    return f"{count} {noun if count == 1 else plural or noun + 's'}"


def clock_text(value: datetime, session: str, *, full=False) -> str:
    return value.strftime("%a, %b %d · %H:%M" if full or value.date().isoformat() != session else "%H:%M")


def window_text(row: PlanForecast, session: str) -> str:
    if row.start.date().isoformat() == session and row.end.date().isoformat() == session:
        return f"{row.start:%H:%M}–{row.end:%H:%M}"
    return f"{row.start:%b %d %H:%M} →\n{row.end:%b %d %H:%M}"


class GameplanTab:
    def __init__(self, root: tk.Tk, parent: ttk.Frame, *, datastore_root: Path | None = None,
                 auto_load: bool = True):
        self.root, self.parent, self.datastore_root = root, parent, datastore_root
        self.plan: Gameplan | None = None
        self.session = tk.StringVar(master=root)
        self.horizon = tk.StringVar(master=root, value="All horizons")
        self.company = tk.StringVar(master=root, value="All companies")
        self.view = tk.StringVar(master=root, value="forecasts")
        self.status = tk.StringVar(master=root, value="Loading saved Gameplan…" if auto_load else "No plan loaded.")
        self.values = {key: tk.StringVar(master=root, value="—") for key in ("first", "entries", "exits", "coverage")}
        self.captions = {key: tk.StringVar(master=root, value="No saved plan") for key in self.values}
        self.selected_key: str | None = None
        self.visible_rows: tuple[PlannedAction | PlanForecast, ...] = ()
        self._row_bounds = []
        self._photos = {}
        self._request_id = 0
        self._closed = self._loading = False
        self._follow_latest = True
        self._last_load = 0.0
        self._auto_enabled = auto_load
        self._messages = queue.Queue()
        self._build()
        self._poll_job = parent.after(80, self._poll)
        self._auto_job = parent.after(AUTO_REFRESH_MS, self._auto_refresh)
        parent.bind("<Destroy>", self._destroy, add="+")
        parent.bind("<Map>", self._mapped, add="+")
        if auto_load:
            parent.after_idle(lambda: self.refresh() if not self._loading and not self._closed else None)

    def _build(self):
        self.page = tk.Frame(self.parent, background=BACKGROUND)
        self.page.pack(fill="both", expand=True, padx=18, pady=12)
        self.header = tk.Frame(self.page, background=BACKGROUND)
        self.header.pack(fill="x")
        label(self.header, "Gameplan", size=21, bold=True, background=BACKGROUND).grid(row=0, column=0, sticky="w")
        self.header.columnconfigure(0, weight=1)
        self.controls = tk.Frame(self.header, background=BACKGROUND)
        self.controls.grid(row=0, column=1, sticky="e")
        self.date_box = ttk.Combobox(self.controls, textvariable=self.session, state="readonly", width=13)
        self.date_box.pack(side="left", padx=(0, 8))
        self.date_box.bind("<<ComboboxSelected>>", self._date_changed)
        Tooltip(self.date_box, "The action session of the saved Gameplan. Historical plans remain snapshots. Completed plans can include forecasts with an unavailable cash projection.")
        ttk.Button(self.controls, text="Latest plan", command=self.follow_latest).pack(side="left", padx=(0, 8))
        self.report_button = ttk.Button(self.controls, text="Open Gameplan", command=self.open_report, state="disabled")
        self.report_button.pack(side="left", padx=(0, 8))
        self.refresh_button = ttk.Button(self.controls, text="Refresh", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.subtitle = label(self.page, "Saved plan · All times Pacific", color=MUTED_TEXT, background=BACKGROUND)
        self.subtitle.pack(anchor="w", pady=(3, 2))
        self.status_label = label(self.page, textvariable=self.status, size=9, color=MUTED_TEXT,
                                  background=BACKGROUND, wraplength=1100, justify="left")
        self.status_label.pack(fill="x", pady=(0, 9))

        viewport = tk.Frame(self.page, background=BACKGROUND)
        viewport.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(viewport, background=BACKGROUND, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=self.canvas.yview)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=self._scroll_changed)
        self.body = tk.Frame(self.canvas, background=BACKGROUND)
        self._body_window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._resize)

        self.cards = tk.Frame(self.body, background=BACKGROUND)
        self.cards.pack(fill="x", pady=(0, 12))
        self.card_widgets = []
        for key, title, icon, tip in (
            ("first", "First planned batch", "◷", "Earliest saved action in the current filters; this is not a live countdown."),
            ("entries", "Projected entries", "↗", "Saved direction-ledger buys, not standalone affordable quantities."),
            ("exits", "Projected sells", "↘", "Sales from saved directions. Historical fixed-duration plans may also show horizon exits."),
            ("coverage", "Plan coverage", "▦", "Summary follows the company and horizon filters. Includes hold and outlook forecasts."),
        ):
            card = panel(self.cards)
            card.columnconfigure(1, weight=1)
            label(card, icon, size=30, color=CYAN).grid(row=0, column=0, rowspan=3, padx=(15, 12), pady=10)
            label(card, title, color=MUTED_TEXT).grid(row=0, column=1, sticky="w", pady=(10, 0))
            label(card, textvariable=self.values[key], size=22, bold=True).grid(row=1, column=1, sticky="w")
            caption = label(card, textvariable=self.captions[key], size=9, color=MUTED_TEXT, wraplength=290, justify="left")
            caption.grid(row=2, column=1, sticky="w", padx=(0, 9), pady=(1, 9))
            info = label(card, "ⓘ", color=MUTED_TEXT)
            info.grid(row=0, column=2, sticky="ne", padx=(2, 9), pady=12)
            Tooltip(info, tip)
            self.card_widgets.append(card)

        self.middle = tk.Frame(self.body, background=BACKGROUND)
        self.middle.pack(fill="x", pady=(0, 12))
        self.schedule = panel(self.middle)
        self.schedule.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.table_top = tk.Frame(self.schedule, background=SURFACE)
        self.table_top.pack(fill="x", padx=12, pady=(10, 8))
        self.table_title = label(self.table_top, "Scheduled trade plan", size=13, bold=True)
        self.table_title.grid(row=0, column=0, sticky="w")
        self.table_top.columnconfigure(0, weight=1)
        self.filters = tk.Frame(self.table_top, background=SURFACE)
        self.filters.grid(row=0, column=1, sticky="e")
        self.horizon_box = ttk.Combobox(self.filters, textvariable=self.horizon, state="readonly",
                                      values=tuple(HORIZON_LABELS), width=12)
        self.horizon_box.pack(side="left", padx=(0, 6))
        self.company_box = ttk.Combobox(self.filters, textvariable=self.company, state="readonly",
                                      values=("All companies",), width=13)
        self.company_box.pack(side="left", padx=(0, 6))
        self.horizon_box.bind("<<ComboboxSelected>>", self._filters_changed)
        self.company_box.bind("<<ComboboxSelected>>", self._company_changed)
        style = ttk.Style(self.root)
        style.configure("Gameplan.Toolbutton", background=SURFACE_ALT, foreground=TEXT, padding=(8, 5), font=("Segoe UI", 9))
        style.map("Gameplan.Toolbutton", background=[("selected", "#10394b")],
                  foreground=[("selected", CYAN)], bordercolor=[("selected", CYAN)])
        self.trade_button = ttk.Radiobutton(self.filters, text="Trades", variable=self.view, value="trades",
                                            command=lambda: self.render(reset_scroll=True), style="Gameplan.Toolbutton")
        self.trade_button.pack(side="left", padx=(0, 4))
        Tooltip(self.trade_button, "Projected orders only. Companies with hold or context forecasts appear in All forecasts. EXIT closes a position at its scheduled horizon end.")
        self.forecast_button = ttk.Radiobutton(self.filters, text="All forecasts", variable=self.view, value="forecasts",
                                               command=lambda: self.render(reset_scroll=True), style="Gameplan.Toolbutton")
        self.forecast_button.pack(side="left")
        Tooltip(self.forecast_button, "Includes all saved entry windows, holds, opening-gap research and later daily outlook.")
        self.table_top.bind("<Configure>", self._resize_filters)

        table_frame = tk.Frame(self.schedule, background=TABLE)
        table_frame.pack(fill="both", expand=True, padx=10)
        table_frame.columnconfigure(0, weight=1)
        self.table_header = tk.Canvas(table_frame, height=31, background=SURFACE_ALT, highlightthickness=0)
        self.table_header.grid(row=0, column=0, sticky="ew")
        self.table = tk.Canvas(table_frame, height=294, background=TABLE, highlightthickness=0, takefocus=True)
        self.table.grid(row=1, column=0, sticky="nsew")
        self.table_scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table_scroll.grid(row=1, column=1, sticky="ns")
        self.table_xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        self.table_xscroll.grid(row=2, column=0, sticky="ew")
        self.table.configure(yscrollcommand=self.table_scroll.set, xscrollcommand=self._table_xchanged)
        self.table.bind("<Configure>", lambda _e: self._draw_table())
        self.table.bind("<Button-1>", self._select_row)
        self.table.bind("<Double-Button-1>", lambda _e: self.show_details())
        self.table.bind("<Up>", lambda _e: self._step_row(-1))
        self.table.bind("<Down>", lambda _e: self._step_row(1))
        self.table.bind("<Home>", lambda _e: self._step_row(-len(self.visible_rows)))
        self.table.bind("<End>", lambda _e: self._step_row(len(self.visible_rows)))
        self.table.bind("<Return>", lambda _e: self.show_details())
        self.table_note = label(self.schedule, "No saved actions loaded.", size=9, color=MUTED_TEXT, justify="left", wraplength=800)
        self.table_note.pack(fill="x", padx=12, pady=10)

        self.inspector = panel(self.middle)
        self.inspector.grid(row=0, column=1, sticky="nsew")
        top = tk.Frame(self.inspector, background=SURFACE)
        top.pack(fill="x", padx=14, pady=(12, 10))
        self.selected_title = label(top, "Select a planned action", size=13, bold=True)
        self.selected_title.pack(side="left")
        self.selected_mark = label(top)
        self.selected_mark.pack(side="right")
        self.probability = label(self.inspector, "—", size=27, bold=True)
        self.probability.pack(anchor="w", padx=18)
        self.direction = label(self.inspector, "Published P(up)", color=MUTED_TEXT)
        self.direction.pack(anchor="w", padx=18, pady=(0, 12))
        Tooltip(self.direction, "The saved published probability, with its saved direction. A bearish forecast can still produce a hold.")
        self.journey = tk.Canvas(self.inspector, height=160, background=SURFACE, highlightthickness=0)
        self.journey.pack(fill="x", padx=12)
        self.journey.bind("<Configure>", lambda _e: self._draw_selection())
        self.selection_note = label(self.inspector, "", size=9, color=WARNING, wraplength=340, justify="left")
        self.selection_note.pack(fill="x", padx=16, pady=(8, 12))
        self.details_button = ttk.Button(self.inspector, text="View forecast details", command=self.show_details, state="disabled")
        self.details_button.pack(fill="x", padx=14, pady=(0, 14))

        self.horizon_panel = panel(self.body)
        self.horizon_panel.pack(fill="x")
        self.horizon_title = label(self.horizon_panel, "Across the horizons", size=13, bold=True)
        self.horizon_title.pack(anchor="w", padx=12, pady=(10, 8))
        self.horizon_cards = tk.Frame(self.horizon_panel, background=SURFACE)
        self.horizon_cards.pack(fill="x", padx=12, pady=(0, 12))
        self.horizon_widgets = []
        for horizon in HORIZONS:
            box = panel(self.horizon_cards)
            title = label(box, HORIZON_NAMES[horizon], size=12, bold=True)
            title.pack(anchor="w", padx=12, pady=(9, 2))
            count = label(box, "— entry windows", size=9, color=MUTED_TEXT)
            count.pack(anchor="w", padx=12)
            timeline = tk.Canvas(box, height=38, background=SURFACE, highlightthickness=0)
            timeline.pack(fill="x", padx=10, pady=(2, 0))
            caption = label(box, "No plan loaded", size=9, justify="left")
            caption.pack(anchor="w", padx=12)
            end = label(box, "", size=9, color=MUTED_TEXT)
            end.pack(anchor="w", padx=12, pady=(3, 7))
            for widget in (box, title, count, timeline, caption, end):
                widget.configure(cursor="hand2")
                widget.bind("<Button-1>", lambda _e, h=horizon: self._choose_horizon(h))
            timeline.bind("<Configure>", lambda _e: self._draw_horizons())
            self.horizon_widgets.append((box, count, timeline, caption, end))
        self.footer = label(self.body, "Projected trades; quantities and exits depend on actual fills.", size=9,
                             color=MUTED_TEXT, background=BACKGROUND, wraplength=1100, justify="left")
        self.footer.pack(fill="x", pady=(9, 3))
        self._bind_wheel(self.body)
        self.render()

    @property
    def selected_horizon(self):
        return HORIZON_LABELS.get(self.horizon.get(), "all")

    @property
    def selected_company(self):
        return None if self.company.get() == "All companies" else self.company.get()

    @staticmethod
    def _key(row):
        return row.key if isinstance(row, PlannedAction) else f"forecast:{row.forecast_id}"

    @property
    def selected_row(self):
        return next((row for row in self.visible_rows if self._key(row) == self.selected_key), None)

    def _resize(self, event):
        width = max(event.width, 620)
        self.canvas.itemconfigure(self._body_window, width=width)
        columns = 2 if width < 1050 else 4
        for container in (self.cards, self.horizon_cards):
            for i in range(4):
                container.columnconfigure(i, weight=1 if i < columns else 0, uniform="cards" if i < columns else "")
        for widgets in (self.card_widgets, [item[0] for item in self.horizon_widgets]):
            for i, widget in enumerate(widgets):
                widget.grid(row=i // columns, column=i % columns, sticky="nsew",
                            padx=(0, 10 if i % columns < columns-1 else 0), pady=(0, 10 if columns == 2 and i < 2 else 0))
        narrow = width < 1250
        self.middle.columnconfigure(0, weight=1 if narrow else 7, minsize=0 if narrow else 840)
        self.middle.columnconfigure(1, weight=0 if narrow else 3, minsize=0 if narrow else 345)
        self.schedule.grid_configure(padx=(0, 0 if narrow else 12))
        self.inspector.grid(row=1 if narrow else 0, column=0 if narrow else 1, sticky="nsew", pady=(12 if narrow else 0, 0))
        self.controls.grid(row=1 if width < 850 else 0, column=0 if width < 850 else 1,
                           columnspan=2 if width < 850 else 1, sticky="w" if width < 850 else "e", pady=(5 if width < 850 else 0, 0))
        self.status_label.configure(wraplength=width-12)
        self.footer.configure(wraplength=width-12)

    def _resize_filters(self, event):
        narrow = event.width < 960
        self.filters.grid(row=1 if narrow else 0, column=0 if narrow else 1,
                          columnspan=2 if narrow else 1, sticky="w" if narrow else "e", pady=(7 if narrow else 0, 0))
        self.table_note.configure(wraplength=max(580, event.width-20))

    def _bind_wheel(self, widget):
        if isinstance(widget, ttk.Combobox):
            # Let Tk's dropdown handle its wheel rather than scrolling the page.
            return
        widget.bind("<MouseWheel>", self._wheel, add="+")
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def _wheel(self, event):
        if not event.delta:
            return "break"
        step = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        if event.widget is self.table:
            first, last = self.table.yview()
            if (step < 0 and first > 0) or (step > 0 and last < 1):
                self.table.yview_scroll(step, "units")
                return "break"
        self.canvas.yview_scroll(step, "units")
        return "break"

    def _scroll_changed(self, first, last):
        self.scrollbar.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.scrollbar.pack_forget()
        else:
            self.scrollbar.pack(side="right", fill="y")

    def _table_xchanged(self, first, last):
        self.table_xscroll.set(first, last)
        self.table_header.xview_moveto(float(first))

    def _date_changed(self, _event=None):
        self._follow_latest = False
        self.refresh()

    def follow_latest(self):
        self._follow_latest = True
        self.refresh()

    def refresh(self):
        if self._closed:
            return
        self._request_id += 1
        request = self._request_id
        selected = None if self._follow_latest else self.session.get() or None
        self._loading = True
        self.refresh_button.configure(state="disabled")
        self.report_button.configure(state="disabled")
        self.details_button.configure(state="disabled")
        self.status.set("Verifying saved Gameplan…")
        self.status_label.configure(foreground=MUTED_TEXT)
        if self.plan and selected is not None and selected != self.plan.session:
            self.plan = None
            self.render(reset_scroll=True)

        def work():
            try:
                loaded = load_gameplan(self.datastore_root, selected)
                dates = plan_sessions(self.datastore_root)
                self._messages.put((request, dates, loaded, None))
            except Exception as exc:
                self._messages.put((request, (), None, exc))
        threading.Thread(target=work, name="gameplan-read", daemon=True).start()

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                request, dates, plan, error = self._messages.get_nowait()
                if request != self._request_id:
                    continue
                self._loading = False
                self.refresh_button.configure(state="normal")
                if error is not None:
                    self.plan = None
                    self.status.set(str(error))
                    self.status_label.configure(foreground=WARNING)
                    self.report_button.configure(state="disabled")
                    self.render(reset_scroll=True)
                else:
                    self.date_box.configure(values=tuple(sorted(set(dates) | {plan.session}, reverse=True)))
                    self.set_plan(plan)
        except queue.Empty:
            pass
        self._poll_job = self.parent.after(80, self._poll)

    def set_plan(self, plan: Gameplan):
        changed = self.plan is None or self.plan.session != plan.session
        if not plan.projection_available and (changed or self.plan.projection_available):
            self.view.set("forecasts")
        if changed:
            self.selected_key = None
        self.plan = plan
        self._last_load = time.monotonic()
        self.session.set(plan.session)
        self.company_box.configure(values=("All companies", *plan.symbols))
        if self.selected_company not in (None, *plan.symbols):
            self.company.set("All companies")
        if changed:
            self._show_forecasts_if_no_trades()
        self.report_button.configure(state="normal")
        self.status_label.configure(foreground=MUTED_TEXT if plan.projection_available else WARNING)
        self.status.set(f"Saved {plan.saved_at:%b %d, %H:%M %Z} · {len(plan.forecasts)} forecasts · "
                        + ((plan.planning_note or "Summary follows filters") if plan.projection_available else plan.projection_note))
        self.render(reset_scroll=changed)

    def _choose_horizon(self, horizon):
        self.horizon.set(HORIZON_NAMES[horizon])
        self._filters_changed()

    def _show_forecasts_if_no_trades(self):
        if (self.plan is not None and self.view.get() == "trades"
                and self.plan.rows(self.selected_horizon, self.selected_company)
                and not self.plan.trades(self.selected_horizon, self.selected_company)):
            self.view.set("forecasts")

    def _filters_changed(self, _event=None):
        self._show_forecasts_if_no_trades()
        self.render(reset_scroll=True)

    def _company_changed(self, _event=None):
        if self.selected_company is None:
            self.view.set("forecasts")
        self._filters_changed()

    def render(self, *, reset_scroll=False):
        forecasts = self.plan.rows(self.selected_horizon, self.selected_company) if self.plan else ()
        actions = self.plan.trades(self.selected_horizon, self.selected_company) if self.plan else ()
        self.visible_rows = actions if self.view.get() == "trades" else forecasts
        if self.selected_key not in {self._key(row) for row in self.visible_rows}:
            self.selected_key = self._key(self.visible_rows[0]) if self.visible_rows else None
        unavailable = self.plan is not None and not self.plan.projection_available
        self.trade_button.configure(text="Trades unavailable" if unavailable else f"Trades {len(actions)}")
        self.forecast_button.configure(text=f"All forecasts {len(forecasts)}")
        self.table_title.configure(text="Scheduled trade plan" if self.view.get() == "trades" else "Saved forecast windows")
        buys, sells = [row for row in actions if row.action == "BUY"], [row for row in actions if row.action == "SELL"]
        expiries = [row for row in actions if row.action == "EXPIRY"]
        if self.plan:
            when = actions[0].when if actions else None
            self.values["first"].set(when.strftime("%H:%M") if when else "—")
            self.captions["first"].set(when.strftime("%A, %b %d") if when else "No projected actions")
            self.values["entries"].set(counted(len(buys), "buy"))
            self.captions["entries"].set(" · ".join(HORIZON_NAMES[h] for h in HORIZONS if any(row.horizon == h for row in buys)) or "No projected entries")
            self.values["exits"].set(counted(len(sells), "sell"))
            scheduled_exits = sum(row.reason == "HORIZON_EXIT" for row in sells)
            bearish_sales = sum(row.reason == "BEARISH_SELL" for row in sells)
            self.captions["exits"].set(f"{len(expiries)} remaining horizon expiries" if expiries else
                                      (f"{counted(scheduled_exits, 'scheduled exit')} · {counted(bearish_sales, 'bearish sale')}" if sells else "No projected sells"))
            if unavailable:
                for key in ("first", "entries", "exits"):
                    self.values[key].set("—")
                    self.captions[key].set("Cash projection unavailable")
            count = len({row.horizon for row in forecasts} | {row.horizon for row in actions})
            self.values["coverage"].set(f"{count} {'horizon' if count == 1 else 'horizons'}")
            self.captions["coverage"].set(f"{counted(len(forecasts), 'forecast')} · "
                                        f"{counted(len({row.symbol for row in forecasts}), 'company', 'companies')}")
            kind = "Upcoming session" if self.plan.session > datetime.now(PACIFIC).date().isoformat() else "Saved session"
            self.subtitle.configure(text=f"{kind} · {date.fromisoformat(self.plan.session):%A, %b %d, %Y} · All times Pacific")
            self.footer.configure(text=("Bullish adds shares · Bearish sells own-horizon shares · No automatic expiry sales · Quantities depend on actual fills."
                if self.plan.holding_policy == SIGNAL_DRIVEN_HOLDING_POLICY else
                f"Saved {self.plan.saved_at:%b %d, %H:%M %Z} · Projected trades; quantities and exits depend on actual fills."))
            if unavailable:
                self.footer.configure(text="Saved forecasts remain available. Projected trades, quantities and exits require the missing price references.")
            if self.view.get() == "trades":
                company_count = len({row.symbol for row in actions})
                forecast_company_count = len({row.symbol for row in forecasts})
                self.table_note.configure(text=f"{len(actions)} of {len(self.plan.actions)} saved actions shown. "
                    + f"{company_count} of {forecast_company_count} companies have projected actions. "
                    + ("All forecasts includes companies with no trades. " if company_count < forecast_company_count else "")
                    + ("Bullish adds shares; bearish sells within its horizon. No automatic expiry sales. " if self.plan.holding_policy == SIGNAL_DRIVEN_HOLDING_POLICY else "EXIT means a scheduled horizon close. ")
                    + ("Expiries include remaining allocations; reserved shares are disclosed in details." if expiries else
                       "Quantities follow the direction ledger; planning prices are estimates."))
                if unavailable:
                    self.table_note.configure(text=self.plan.projection_note + " Select All forecasts to review the saved windows.")
            else:
                self.table_note.configure(text=("No projected trades for these filters. " if forecasts and not actions and not unavailable else "")
                    + f"{sum(row.eligible for row in forecasts)} entry windows · "
                    f"{sum(not row.eligible for row in forecasts)} context forecasts. "
                    + ("Trade actions and quantities are unavailable." if unavailable else "Hold and outlook rows are not scheduled orders."))
        else:
            for key in self.values:
                self.values[key].set("—")
                self.captions[key].set("No saved plan")
            self.subtitle.configure(text="Saved plan · All times Pacific")
            self.footer.configure(text="Projected trades; quantities and exits depend on actual fills.")
            self.table_note.configure(text="Refresh to load a completed saved Gameplan.")
        if reset_scroll:
            self.table.yview_moveto(0)
            self.table.xview_moveto(0)
        self._draw_table()
        self._draw_selection()
        self._draw_horizons()

    def _photo(self, symbol, size=24):
        key = symbol, size
        if key not in self._photos:
            asset = "goog" if symbol == "GOOGL" else symbol.lower()
            path = Path(__file__).with_name("assets") / "security_marks" / f"{asset}.png"
            try:
                source = tk.PhotoImage(master=self.root, file=str(path))
                factor = max(1, math.ceil(max(source.width(), source.height()) / size))
                self._photos[key] = source.subsample(factor, factor)
            except tk.TclError:
                self._photos[key] = None
        return self._photos[key]

    def _draw_table(self):
        canvas, header = self.table, self.table_header
        canvas.delete("all")
        header.delete("all")
        self._row_bounds = []
        forecast_view = self.view.get() == "forecasts"
        weights = [175, 125, 105, 72, 85, 105, 105, 90, 190] if forecast_view else [95, 135, 90, 72, 75, 120, 120, 210]
        width = max(sum(weights), canvas.winfo_width())
        edges = [0]
        for value in weights:
            edges.append(edges[-1] + value * width / sum(weights))
        headers = ("Window (Pacific)", "Company", "Direction", "Horizon", "P(up)", "Planning price", "Execution mid", "Action", "Reason") if forecast_view else (
            "Time", "Company", "Action", "Horizon", "Shares", "Planning price", "Execution mid", "Reason")
        for i, title in enumerate(headers):
            header.create_rectangle(edges[i], 0, edges[i+1], 31, fill=SURFACE_ALT, outline=BORDER)
            header.create_text((edges[i]+edges[i+1])/2, 15, text=title, fill=MUTED_TEXT, font=("Segoe UI", 10))
        header.configure(scrollregion=(0, 0, width, 31))
        y, previous = 0, None
        for row in self.visible_rows:
            if isinstance(row, PlannedAction) and row.when != previous:
                count = sum(item.when == row.when for item in self.visible_rows)
                canvas.create_rectangle(0, y, width, y+27, fill=SURFACE_ALT, outline=BORDER)
                canvas.create_text(12, y+13, anchor="w", text=f"{row.when:%a, %b %d · %H:%M}  ({count} {'action' if count == 1 else 'actions'})",
                                   fill=TEXT, font=("Segoe UI", 9, "bold"))
                y += 27
                previous = row.when
            top, center = y, y+20
            selected = self._key(row) == self.selected_key
            canvas.create_rectangle(0, top, width-1, top+40, fill="#102d3e" if selected else TABLE,
                                    outline=CYAN if selected else BORDER)
            if isinstance(row, PlannedAction):
                execution = self.plan.execution_quote(row.forecast_id, is_exit=row.reason == "HORIZON_EXIT" or row.action == "EXPIRY")
                texts = [clock_text(row.when, self.plan.session), row.symbol,
                         "EXIT" if row.reason == "HORIZON_EXIT" else row.action, row.horizon,
                         shares(row.quantity), money(row.price), money(execution.midpoint) if execution else "—", reason_text(row.reason)]
                # Dates are already in the batch header; keep individual time cells compact.
                texts[0] = row.when.strftime("%H:%M")
            else:
                execution = self.plan.execution_quote(row.forecast_id)
                texts = [window_text(row, self.plan.session), row.symbol, row.direction.replace("NO_EDGE", "Neutral").title(),
                         row.horizon, "—" if row.probability is None else f"{row.probability:.2%}", money(row.price),
                         money(execution.midpoint) if execution else "—", row.action, reason_text(row.reason)]
            for i, value in enumerate(texts):
                x = (edges[i]+edges[i+1])/2
                if i == 1:
                    photo = self._photo(row.symbol)
                    if photo:
                        canvas.create_image(edges[i]+22, center, image=photo)
                    canvas.create_text(edges[i]+45, center, anchor="w", text=value, fill=TEXT, font=("Segoe UI", 10, "bold"))
                elif (not forecast_view and i == 2) or (forecast_view and i == 7):
                    color = CYAN if row.reason == "HORIZON_EXIT" else ACTION_COLORS[row.action]
                    half_width = 42 if row.action == "UNAVAILABLE" else 34
                    canvas.create_rectangle(x-half_width, center-11, x+half_width, center+11, fill="#183044", outline=color)
                    canvas.create_text(x, center, text="Unavailable" if row.action == "UNAVAILABLE" else value,
                                       fill=color, font=("Segoe UI", 9, "bold"))
                else:
                    color = TEXT
                    if forecast_view and i == 2:
                        color = SUCCESS if row.direction == "BULLISH" else DANGER if row.direction == "BEARISH" else MUTED_TEXT
                    canvas.create_text(x, center, text=value, fill=color, width=edges[i+1]-edges[i]-12,
                                       font=("Segoe UI", 10), justify="center")
            self._row_bounds.append((top, top+40, row))
            y += 40
        if not self.visible_rows:
            text = "No saved plan loaded." if not self.plan else (
                "No projected actions in these filters.\nAll forecasts shows the saved hold and context windows." if not forecast_view
                else "No forecasts match these filters.")
            if self.plan and not self.plan.projection_available and not forecast_view:
                text = "Cash projection unavailable.\nSelect All forecasts to review the saved forecast windows."
            canvas.create_text(width/2, 100, text=text, fill=MUTED_TEXT, font=("Segoe UI", 11), justify="center")
        canvas.configure(scrollregion=(0, 0, width, max(y, 230)))
        if canvas.winfo_width() < width:
            self.table_xscroll.grid()
        else:
            self.table_xscroll.grid_remove()
        if y > int(canvas.cget("height")):
            self.table_scroll.grid()
        else:
            self.table_scroll.grid_remove()

    def _select_row(self, event):
        self.table.focus_set()
        y = self.table.canvasy(event.y)
        for top, bottom, row in self._row_bounds:
            if top <= y < bottom:
                self.selected_key = self._key(row)
                self._draw_table()
                self._draw_selection()
                break

    def _step_row(self, step):
        if self.visible_rows:
            keys = [self._key(row) for row in self.visible_rows]
            index = keys.index(self.selected_key) if self.selected_key in keys else 0
            self.selected_key = keys[max(0, min(len(keys)-1, index+step))]
            self._draw_table()
            self._draw_selection()
            top, bottom, _ = next(bounds for bounds in self._row_bounds if self._key(bounds[2]) == self.selected_key)
            visible_top = self.table.canvasy(0)
            height = self.table.winfo_height()
            total = float(self.table.cget("scrollregion").split()[-1])
            if top < visible_top:
                self.table.yview_moveto(top/total)
            elif bottom > visible_top+height:
                self.table.yview_moveto((bottom-height)/total)
        return "break"

    def _selection_content(self):
        row = self.selected_row
        forecast = row if isinstance(row, PlanForecast) else self.plan.forecast(row.forecast_id) if row and self.plan else None
        if row is None:
            return None, None, [], "Select a row to inspect its saved forecast and action."
        related = [item for item in self.plan.actions if item.forecast_id == row.forecast_id]
        journey = []
        if forecast:
            entry = next((item for item in related if item.action in {"BUY", "SELL"} and item.reason != "HORIZON_EXIT"), None)
            closing = next((item for item in related if item.reason == "HORIZON_EXIT" or item.action == "EXPIRY"), None)
            if entry:
                journey.append((entry.when, f"Planned {entry.action.lower()}", f"{shares(entry.quantity)} shares · {money(entry.price)} estimate", ACTION_COLORS[entry.action]))
            else:
                journey.append((forecast.start, forecast.action.title(), reason_text(forecast.reason), ACTION_COLORS[forecast.action]))
            if closing:
                journey.append((closing.when, "Horizon expiry" if closing.action == "EXPIRY" else "Scheduled horizon exit",
                                f"{shares(closing.quantity)} shares" + (f" · {money(closing.price)} estimate" if closing.price is not None else " · Price not projected"),
                                CYAN if closing.reason == "HORIZON_EXIT" else ACTION_COLORS[closing.action]))
            else:
                journey.append((forecast.end, "Forecast window ends",
                    "Measurement boundary only; holdings continue until a bearish instruction"
                    if self.plan.holding_policy == SIGNAL_DRIVEN_HOLDING_POLICY else
                    "No separate exit saved for this forecast", MUTED_TEXT))
            note = ("Bullish buys add to this horizon; bearish instructions sell its held shares." if self.plan.holding_policy == SIGNAL_DRIVEN_HOLDING_POLICY else "Exit quantity follows actual filled shares.") if entry and entry.action == "BUY" else reason_text(forecast.reason)
            if closing and closing.reason == "HORIZON_EXIT":
                note += (f" The {forecast.start:%H:%M} position closes at its scheduled {forecast.end:%H:%M} end."
                         " Consecutive bullish windows can schedule a new buy at the same time.")
            if not self.plan.projection_available:
                note = self.plan.projection_note
            if not forecast.eligible:
                note = "Outlook / research context. This window is not a scheduled entry."
            execution = self.plan.execution_quote(forecast.forecast_id,
                is_exit=isinstance(row, PlannedAction) and (row.reason == "HORIZON_EXIT" or row.action == "EXPIRY"))
            if execution:
                note += (f" Recorded midpoint {money(execution.midpoint)} at {execution.observed_at:%H:%M:%S %Z};"
                         f" planned price {money(row.price)}. This comparison does not affect execution.")
        else:
            journey.append((row.when, reason_text(row.reason), f"{shares(row.quantity)} shares · {money(row.price)}", ACTION_COLORS[row.action]))
            note = "Allocation from an earlier plan. Its forecast is not part of this saved session."
        if isinstance(row, PlannedAction) and row.action == "EXPIRY":
            note = f"Remaining allocation; {shares(row.reserved)} shares already reserved for sale. Recheck actual holdings at expiry."
        return row, forecast, journey, note

    def _draw_selection(self):
        row, forecast, journey, note = self._selection_content()
        self.journey.delete("all")
        self.selected_title.configure(text=f"{row.symbol} · {HORIZON_NAMES[row.horizon]} plan" if row else "Select a planned action")
        photo = self._photo(row.symbol, 28) if row else None
        self.selected_mark.configure(image=photo if photo else "")
        self.probability.configure(text=f"{forecast.probability:.2%}" if forecast and forecast.probability is not None else "—")
        direction = forecast.direction.replace("NO_EDGE", "NEUTRAL").title() if forecast else "Forecast unavailable"
        probability_label = "Entry forecast P(up)" if isinstance(row, PlannedAction) and row.reason == "HORIZON_EXIT" else "Published P(up)"
        self.direction.configure(text=f"{probability_label} · {direction}", foreground=SUCCESS if forecast and forecast.direction == "BULLISH" else
                                 DANGER if forecast and forecast.direction == "BEARISH" else MUTED_TEXT)
        width = max(320, self.journey.winfo_width())
        if len(journey) > 1:
            self.journey.create_line(16, 23, 16, 103, fill=BORDER, width=2)
        for index, (when, title, detail, color) in enumerate(journey):
            y = 22 + index*80
            self.journey.create_oval(9, y-7, 23, y+7, fill=color, outline="")
            self.journey.create_text(38, y, text=f"{clock_text(when, self.plan.session)} · {title}", anchor="w",
                                     width=width-45, fill=TEXT, font=("Segoe UI", 10, "bold"))
            self.journey.create_text(38, y+33, text=detail, anchor="w", width=width-45,
                                     fill=MUTED_TEXT, font=("Segoe UI", 9))
        self.selection_note.configure(text=note, wraplength=width-12)
        self.details_button.configure(state="normal" if row and not self._loading else "disabled")

    def _draw_horizons(self):
        self.horizon_title.configure(text=f"Across the horizons · {self.selected_company or 'All companies'}")
        for horizon, (box, count, timeline, caption, end_label) in zip(HORIZONS, self.horizon_widgets):
            forecasts = self.plan.rows(horizon, self.selected_company) if self.plan else ()
            entries = [row for row in forecasts if row.eligible]
            actions = self.plan.trades(horizon, self.selected_company) if self.plan else ()
            count.configure(text=counted(len(entries), "entry window") if self.plan else "— entry windows")
            buys = sum(row.action == "BUY" for row in actions)
            sells = sum(row.action == "SELL" for row in actions)
            expiries = sum(row.action == "EXPIRY" for row in actions)
            text = f"{counted(buys, 'buy')} · {counted(sells, 'sell')}" if actions else "No projected trades"
            if self.plan and not self.plan.projection_available:
                text = "Cash projection unavailable"
            if expiries:
                text += f" · {counted(expiries, 'expiry', 'expiries')}"
            caption.configure(text=text if self.plan else "No plan loaded")
            end = max((row.end for row in entries), default=None)
            end_label.configure(text=f"Last window ends {clock_text(end, self.plan.session, full=True)}" if end else "")
            box.configure(highlightbackground=CYAN if self.selected_horizon == horizon else BORDER)
            timeline.delete("all")
            width = max(timeline.winfo_width(), 240)
            left, right = 12, width-12
            timeline.create_line(left, 15, right, 15, fill=CYAN if actions else BORDER, width=2)
            starts = sorted({row.start for row in entries})
            if entries:
                first, last = min(starts), max(row.end for row in entries)
                seconds = max(1, (last-first).total_seconds())
                # Mark forecast-window starts to scale, never invented price points.
                for start in starts:
                    x = left + (right-left)*(start-first).total_seconds()/seconds
                    timeline.create_oval(x-3, 12, x+3, 18, fill=MUTED_TEXT, outline="")
                timeline.create_text(left, 32, anchor="w", text=first.strftime("%H:%M"), fill=MUTED_TEXT, font=("Segoe UI", 8))
                timeline.create_text(right, 32, anchor="e", text=last.strftime("%a %H:%M"), fill=MUTED_TEXT, font=("Segoe UI", 8))

    def show_details(self):
        if not self.plan or self._loading or self.selected_row is None:
            return
        row, forecast, _, note = self._selection_content()
        lines = [f"Saved session: {self.plan.session} · All times Pacific", f"Company: {row.symbol} · Horizon: {row.horizon}", ""]
        if not self.plan.projection_available:
            lines += [self.plan.projection_note, "Projected actions and quantities were not calculated.", ""]
        if forecast:
            action_text = f"Saved action: {forecast.action}" if self.plan.projection_available else (
                "Projected action: Unavailable" if forecast.eligible else "Forecast context only")
            lines += [f"Published P(up): {forecast.probability:.2%}" if forecast.probability is not None else "Published probability: unavailable",
                      f"Saved direction: {forecast.direction.replace('NO_EDGE', 'NEUTRAL')}",
                      f"{action_text} · Direction-based shares: {shares(forecast.quantity)}",
                      f"Reason: {reason_text(forecast.reason)}", f"Model status: {forecast.model_status}",
                      f"Window start: {forecast.start:%a, %b %d, %Y %H:%M %Z}",
                      f"Window end: {forecast.end:%a, %b %d, %Y %H:%M %Z}",
                      f"Role: {forecast.role} · {'Entry window' if forecast.eligible else 'Context only'}", ""]
        if isinstance(row, PlannedAction):
            lines += [f"Selected action: {row.action} · {shares(row.quantity)} shares",
                      f"Scheduled time: {row.when:%a, %b %d, %Y %H:%M %Z}",
                      f"Planning price: {money(row.price)}", f"Action reason: {reason_text(row.reason)}",
                      f"Source: {'Saved direction ledger' if row.source == 'ledger' else 'Remaining horizon allocation'}", ""]
            if row.source != "ledger":
                lines += [f"Already reserved for sale: {shares(row.reserved)} shares",
                          f"Unreserved allocation: {shares(row.quantity-row.reserved)} shares",
                          "This expiry is not an additional simulated order or a projected future sale price.", ""]
        lines += [note, "", "Saved planning estimates are not current quotes or fills. Standalone affordable quantities are not added as orders.",
                  "Later daily outlook and opening-gap research are forecast context only.", "",
                  f"Saved at: {self.plan.saved_at:%b %d, %Y %H:%M %Z}", f"Forecast: {row.forecast_id}"]
        self._text_dialog(f"{row.symbol} · Gameplan details", "\n".join(lines))

    def _text_dialog(self, title, text):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.configure(background=SURFACE)
        dialog.transient(self.root)
        dialog.geometry("700x560")
        box = tk.Text(dialog, wrap="word", background=SURFACE, foreground=TEXT, font=("Segoe UI", 11),
                      padx=18, pady=16, relief="flat")
        scrollbar = ttk.Scrollbar(dialog, command=box.yview)
        scrollbar.pack(side="right", fill="y")
        box.configure(yscrollcommand=scrollbar.set)
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        box.configure(state="disabled")
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=10)
        dialog.bind("<Escape>", lambda _e: dialog.destroy())

    def open_report(self):
        if self.plan is None or self._loading:
            return
        try:
            if os.name == "nt":
                os.startfile(str(self.plan.report_path))
            else:
                webbrowser.open(self.plan.report_path.as_uri())
        except OSError as exc:
            messagebox.showerror("Could not open Gameplan", str(exc), parent=self.root)

    def _mapped(self, event):
        if self._auto_enabled and event.widget is self.parent and not self._loading and time.monotonic()-self._last_load > 30:
            self.refresh()

    def _auto_refresh(self):
        if not self._closed:
            if self._auto_enabled and self.parent.winfo_ismapped() and not self._loading:
                self.refresh()
            self._auto_job = self.parent.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _destroy(self, event):
        if event.widget is not self.parent:
            return
        self._closed = True
        self._request_id += 1
        for job in (self._poll_job, self._auto_job):
            try:
                self.parent.after_cancel(job)
            except tk.TclError:
                pass
