"""Gameplan Stats: the prediction scorecard, backed only by saved reviews."""
from __future__ import annotations

import math
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from tkinter import messagebox, ttk

from app.ui.gameplan_widgets import label as _label, panel as _panel, Tooltip as _Tooltip
from app.ui.gameplan_stats_data import (
    GameplanStatsReview, PredictionOutcome, load_gameplan_stats, prediction_metrics,
    review_sessions,
)
from app.ui.theme import BACKGROUND, BORDER, DANGER, MUTED_TEXT, SUCCESS, SURFACE, SURFACE_ALT, TEXT, WARNING

CYAN = "#32d4ec"
TABLE = "#0c1929"
HORIZON_LABELS = {"All horizons": "all", "1 hour": "1h", "4 hours": "4h", "1 day": "1d", "Weekly": "1w"}
CELL_STYLES = {
    "correct": ("✓", SUCCESS, "#103b35", "Correct"),
    "incorrect": ("×", DANGER, "#3b2732", "Incorrect"),
    "neutral": ("—", MUTED_TEXT, "#243548", "Neutral"),
    "awaiting_data": ("?", "#ffda5b", "#40391e", "Awaiting data"),
    "pending": ("◷", "#75bfff", "#19334e", "Awaiting maturity"),
    "not_saved": ("·", MUTED_TEXT, TABLE, "No saved forecast"),
}
AUTO_REFRESH_MS = 5 * 60 * 1000


def percent(value: float | None) -> str:
    return "—" if value is None else f"{Decimal(str(value * 100)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def brier_text(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"


class GameplanStatsTab:
    def __init__(self, root: tk.Tk, parent: ttk.Frame, *, datastore_root: Path | None = None,
                 auto_load: bool = True):
        self.root, self.parent, self.datastore_root = root, parent, datastore_root
        self.review: GameplanStatsReview | None = None
        self.selected_symbol: str | None = None
        self.session = tk.StringVar(master=root)
        self.horizon = tk.StringVar(master=root, value="All horizons")
        self.status = tk.StringVar(master=root, value="Loading saved results…" if auto_load else "No results loaded.")
        self.values = {name: tk.StringVar(master=root, value="—") for name in ("accuracy", "brier", "bullish", "bearish")}
        self.captions = {name: tk.StringVar(master=root, value="No scored calls") for name in self.values}
        self._request_id = 0
        self._closed = False
        self._loading = False
        self._follow_latest = True
        self._last_load = 0.0
        self._auto_enabled = auto_load
        self._messages = queue.Queue()
        self._photos = {}
        self._cell_bounds = []
        self._build()
        self._poll_job = parent.after(80, self._poll)
        self._auto_job = parent.after(AUTO_REFRESH_MS, self._auto_refresh)
        parent.bind("<Destroy>", self._destroy, add="+")
        parent.bind("<Map>", self._mapped, add="+")
        if auto_load:
            parent.after_idle(lambda: self.refresh() if not self._loading else None)

    def _build(self):
        self.page = tk.Frame(self.parent, background=BACKGROUND)
        self.page.pack(fill="both", expand=True, padx=18, pady=12)
        self.header = tk.Frame(self.page, background=BACKGROUND)
        self.header.pack(fill="x")
        self.heading = _label(self.header, "Gameplan Stats", size=21, bold=True, background=BACKGROUND)
        self.heading.grid(row=0, column=0, sticky="w")
        self.controls = tk.Frame(self.header, background=BACKGROUND)
        self.controls.grid(row=0, column=1, sticky="e")
        self.header.columnconfigure(0, weight=1)
        self.date_box = ttk.Combobox(self.controls, textvariable=self.session, state="readonly", width=13)
        self.date_box.pack(side="left", padx=(0, 8))
        self.date_box.bind("<<ComboboxSelected>>", self._date_changed)
        _Tooltip(self.date_box, "The session being reviewed, not the next Gameplan's action date.")
        self.horizon_box = ttk.Combobox(self.controls, textvariable=self.horizon, state="readonly",
                                      values=tuple(HORIZON_LABELS), width=13)
        self.horizon_box.pack(side="left", padx=(0, 8))
        self.horizon_box.bind("<<ComboboxSelected>>", lambda _event: self.render())
        self.report_button = ttk.Button(self.controls, text="Open full report", command=self.open_report, state="disabled")
        self.report_button.pack(side="left", padx=(0, 8))
        self.refresh_button = ttk.Button(self.controls, text="Refresh", command=self.refresh)
        self.refresh_button.pack(side="left")
        _label(self.page, "Forecast windows measure prediction accuracy; they do not schedule sales. All times Pacific.",
               color=MUTED_TEXT, background=BACKGROUND).pack(anchor="w", pady=(3, 1))
        self.status_label = _label(self.page, textvariable=self.status, size=9, color=MUTED_TEXT,
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
        self.body.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._resize)
        self.cards = tk.Frame(self.body, background=BACKGROUND)
        self.cards.pack(fill="x", pady=(0, 12))
        self.card_widgets = []
        explanations = {
            "accuracy": "Correct saved bullish/bearish calls divided by scored calls. Neutral, pending and missing outcomes are excluded.",
            "brier": self._probability_definition,
            "bullish": "Accuracy of scored bullish calls only. A dash means there were no scored bullish calls; it does not mean 0% accuracy.",
            "bearish": "Accuracy of scored bearish calls only. Counts show how much evidence underlies the percentage.",
        }
        for index, (name, title, icon) in enumerate((("accuracy", "Direction accuracy", "◎"),
                ("brier", "Probability error", "▥"), ("bullish", "Bullish accuracy", "↗"),
                ("bearish", "Bearish accuracy", "↘"))):
            card = _panel(self.cards)
            card.grid(row=0, column=index, sticky="nsew", padx=(0, 10 if index < 3 else 0))
            card.columnconfigure(1, weight=1)
            _label(card, icon, size=28, color=CYAN, padx=14).grid(row=0, column=0, rowspan=3)
            title_label = _label(card, title + "  ⓘ", size=10)
            title_label.grid(row=0, column=1, sticky="w", pady=(10, 0), padx=(0, 12))
            _Tooltip(title_label, explanations[name])
            _label(card, textvariable=self.values[name], size=25, bold=True).grid(row=1, column=1, sticky="w")
            _label(card, textvariable=self.captions[name], size=9, color=MUTED_TEXT).grid(row=2, column=1, sticky="w", pady=(0, 10), padx=(0, 10))
            self.card_widgets.append(card)

        self.middle = tk.Frame(self.body, background=BACKGROUND)
        self.middle.pack(fill="x", pady=(0, 12))
        self.score_panel = _panel(self.middle)
        self.score_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        _label(self.score_panel, "Company scorecard", size=13, bold=True).pack(anchor="w", padx=12, pady=(8, 6))
        self.score_canvas, self.score_scroll = self._table_canvas(self.score_panel, 278)
        self.score_canvas.configure(takefocus=True)
        self.score_canvas.bind("<Configure>", lambda _event: self._draw_scorecard())
        self.score_canvas.bind("<Button-1>", self._select_row)
        self.score_canvas.bind("<Up>", lambda _event: self._step_symbol(-1))
        self.score_canvas.bind("<Down>", lambda _event: self._step_symbol(1))
        _Tooltip(self.score_canvas, "Click a company or use the up/down keys. Bull / Bear calls shows counts of scored predictions, not a success rate. Brier: lower is better.")
        self.side = tk.Frame(self.middle, background=BACKGROUND)
        self.side.grid(row=0, column=1, sticky="nsew")
        self.coverage = _panel(self.side)
        self.coverage.pack(fill="x", pady=(0, 10))
        coverage_header = tk.Frame(self.coverage, background=SURFACE)
        coverage_header.pack(fill="x", padx=12, pady=(8, 5))
        _label(coverage_header, "Outcome coverage", size=12, bold=True).pack(side="left")
        self.coverage_total = _label(coverage_header, "Total: —", size=10)
        self.coverage_total.pack(side="right")
        self.coverage_bar = tk.Canvas(self.coverage, height=16, background=SURFACE_ALT, highlightthickness=0)
        self.coverage_bar.pack(fill="x", padx=12, pady=5)
        self.coverage_bar.bind("<Configure>", lambda _event: self._draw_coverage())
        counts = tk.Frame(self.coverage, background=SURFACE)
        counts.pack(fill="x", padx=12)
        self.coverage_counts = []
        for index, (text, color) in enumerate((("evaluated", SUCCESS), ("awaiting maturity", WARNING), ("awaiting data", MUTED_TEXT))):
            counts.columnconfigure(index, weight=1, uniform="coverage")
            item = tk.Frame(counts, background=SURFACE)
            item.grid(row=0, column=index, sticky="ew")
            value = _label(item, "—", size=15, bold=True, color=color)
            value.pack(anchor="w")
            _label(item, text, size=8, color=MUTED_TEXT).pack(anchor="w")
            self.coverage_counts.append(value)
        self.coverage_caption = _label(self.coverage, "Brier includes evaluated neutral forecasts.", size=8, color=MUTED_TEXT)
        self.coverage_caption.pack(anchor="w", padx=12, pady=(5, 8))
        self.selection = _panel(self.side)
        self.selection.pack(fill="both", expand=True)
        self.selection_header = tk.Frame(self.selection, background=SURFACE)
        self.selection_header.pack(fill="x", padx=12, pady=(8, 6))
        self.selected_title = _label(self.selection_header, "Select a company", size=12, bold=True)
        self.selected_title.pack(side="left")
        self.selected_mark = tk.Label(self.selection_header, background=SURFACE)
        self.selected_mark.pack(side="right")
        selected_metrics = tk.Frame(self.selection, background=SURFACE)
        selected_metrics.pack(fill="x", padx=12)
        self.selected_boxes = []
        for index in range(2):
            selected_metrics.columnconfigure(index, weight=1, uniform="selected")
            box = _panel(selected_metrics)
            box.grid(row=0, column=index, sticky="nsew", padx=(0, 8 if index == 0 else 0))
            value = _label(box, "—", size=18, bold=True)
            value.pack(anchor="w", padx=10, pady=(6, 0))
            caption = _label(box, "correct / scored" if index == 0 else "Brier score", size=9, color=MUTED_TEXT)
            caption.pack(anchor="w", padx=10)
            detail = _label(box, "", size=9, color=MUTED_TEXT)
            detail.pack(anchor="w", padx=10, pady=(4, 7))
            self.selected_boxes.append((value, detail))
        self.selected_mix = _label(self.selection, "", size=9, color=MUTED_TEXT)
        self.selected_mix.pack(anchor="w", padx=12, pady=(8, 9))

        self.grid_panel = _panel(self.body)
        self.grid_panel.pack(fill="x")
        self.grid_header = tk.Frame(self.grid_panel, background=SURFACE)
        self.grid_header.pack(fill="x", padx=12, pady=(8, 5))
        self.grid_header.columnconfigure(0, weight=1)
        _label(self.grid_header, "1-hour prediction outcomes", size=13, bold=True).grid(row=0, column=0, sticky="w")
        self.grid_legend = _label(self.grid_header, "✓ Correct    × Incorrect    — Neutral    ? Awaiting data    ◷ Pending", size=9, color=MUTED_TEXT)
        self.grid_legend.grid(row=0, column=1, sticky="e")
        self.grid_canvas, self.grid_scroll = self._table_canvas(self.grid_panel, 224)
        self.grid_canvas.bind("<Configure>", lambda _event: self._draw_grid())
        self.grid_canvas.bind("<Button-1>", self._grid_click)
        _Tooltip(self.grid_canvas, "Each cell scores the saved prediction over the labelled 1h window. Click a cell for its saved probability, observations and outcome. Gray neutral calls have no directional score.")
        self.footer = tk.Frame(self.body, background=BACKGROUND)
        self.footer.pack(fill="x", pady=(8, 2))
        self.population_label = _label(self.footer, "", size=8, color=MUTED_TEXT, background=BACKGROUND, justify="left")
        self.population_label.pack(side="left", fill="x", expand=True)
        ttk.Button(self.footer, text="Metric definitions", command=self.show_definitions).pack(side="right", padx=(8, 0))
        self.grid_scope = _label(self.body, "Grid: 1h execution windows · Summary: all saved horizons", size=8, color=MUTED_TEXT, background=BACKGROUND)
        self.grid_scope.pack(anchor="w")
        self._bind_wheel(self.body)
        self.render()

    def _table_canvas(self, panel, height):
        canvas = tk.Canvas(panel, height=height, background=TABLE, highlightthickness=0)
        canvas.pack(fill="x", padx=6, pady=(0, 6))
        scroll = ttk.Scrollbar(panel, orient="horizontal", command=canvas.xview)
        canvas.configure(xscrollcommand=scroll.set)
        return canvas, scroll

    def _bind_wheel(self, widget):
        widget.bind("<MouseWheel>", self._wheel, add="+")
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def _wheel(self, event):
        # Bind only this tab's widgets, never another tab's global wheel handler.
        if event.delta and self.canvas.bbox("all"):
            self.canvas.yview_scroll(-int(event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
        return "break"

    def _scroll_changed(self, first, last):
        self.scrollbar.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.scrollbar.pack_forget()
        else:
            self.scrollbar.pack(side="right", fill="y")

    def _resize(self, event):
        self.canvas.itemconfigure(self._body_window, width=max(event.width, 580))
        narrow = event.width < 1080
        columns = 2 if narrow else 4
        for i in range(4):
            self.cards.columnconfigure(i, weight=1 if i < columns else 0, uniform="cards" if i < columns else "")
        for i, card in enumerate(self.card_widgets):
            card.grid(row=i // columns, column=i % columns, padx=(0, 10 if i % columns < columns - 1 else 0), pady=(0, 10 if narrow and i < 2 else 0))
        self.middle.columnconfigure(0, weight=2 if not narrow else 1, minsize=745 if not narrow else 0)
        self.middle.columnconfigure(1, weight=1 if not narrow else 0, minsize=320 if not narrow else 0)
        self.score_panel.grid_configure(padx=(0, 12 if not narrow else 0))
        self.side.grid(row=1 if narrow else 0, column=0 if narrow else 1, sticky="nsew", pady=(10 if narrow else 0, 0))
        if event.width < 940:
            self.controls.grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
            self.grid_legend.grid(row=1, column=0, columnspan=2, sticky="w", pady=(3, 0))
        else:
            self.controls.grid(row=0, column=1, columnspan=1, sticky="e", pady=0)
            self.grid_legend.grid(row=0, column=1, columnspan=1, sticky="e", pady=0)
        self.status_label.configure(wraplength=max(560, event.width - 20))
        self.population_label.configure(wraplength=max(400, event.width - 200))

    @property
    def selected_horizon(self):
        return HORIZON_LABELS.get(self.horizon.get(), "all")

    def _date_changed(self, _event=None):
        self._follow_latest = False
        self.refresh()

    def refresh(self):
        if self._closed:
            return
        self._request_id += 1
        request = self._request_id
        selected = None if self._follow_latest else self.session.get() or None
        self._loading = True
        self.status.set("Verifying saved results…")
        self.status_label.configure(foreground=MUTED_TEXT)
        self.refresh_button.configure(state="disabled")
        self.report_button.configure(state="disabled")
        # Clear another session immediately, rather than showing old numbers under a new date.
        if self.review is not None and selected is not None and selected != self.review.session:
            self.review = None
            self.render()

        def work():
            try:
                dates = review_sessions(self.datastore_root)
                review = load_gameplan_stats(self.datastore_root, selected or (dates[0] if dates else None))
                self._messages.put((request, dates, review, None))
            except Exception as exc:
                self._messages.put((request, (), None, exc))

        threading.Thread(target=work, name="gameplan-stats-read", daemon=True).start()

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                request, dates, review, error = self._messages.get_nowait()
                if request != self._request_id:
                    continue
                self._loading = False
                self.refresh_button.configure(state="normal")
                if error is not None:
                    self.review = None
                    self.status.set(str(error))
                    self.status_label.configure(foreground=WARNING)
                    self.report_button.configure(state="disabled")
                    self.render()
                else:
                    self.date_box.configure(values=dates or (review.session,))
                    self.session.set(review.session)
                    self.set_review(review)
                    self._last_load = time.monotonic()
        except queue.Empty:
            pass
        self._poll_job = self.parent.after(80, self._poll)

    def set_review(self, review: GameplanStatsReview):
        self.review = review
        self._last_load = time.monotonic()
        self.session.set(review.session)
        self.heading.configure(text=f"{review.display_name} Stats")
        self.report_button.configure(state="normal")
        self.status_label.configure(foreground=MUTED_TEXT)
        self.status.set(f"Verified session {review.session} · Outcomes through {review.outcomes_through:%b %d, %H:%M %Z} · Reviewed {review.reviewed_at:%b %d, %H:%M %Z}")
        if not review.outcomes:
            self.status.set(f"Session {review.session}: no saved approved forecasts are available to score.")
        self.render()

    def render(self):
        metrics = self.review.metrics(self.selected_horizon) if self.review else prediction_metrics(())
        self.values["accuracy"].set(percent(metrics.accuracy))
        self.values["brier"].set(brier_text(metrics.brier))
        self.values["bullish"].set(percent(metrics.bullish_accuracy))
        self.values["bearish"].set(percent(metrics.bearish_accuracy))
        for name, correct, scored in (("accuracy", metrics.correct, metrics.scored),
                                     ("bullish", metrics.bullish_correct, metrics.bullish_scored),
                                     ("bearish", metrics.bearish_correct, metrics.bearish_scored)):
            self.captions[name].set(f"{correct} / {scored} scored calls" if scored else "0 scored calls")
        self.captions["brier"].set("Brier score · lower is better" if metrics.brier is not None else "No evaluated forecasts")
        symbols = self.review.symbols if self.review else ()
        if self.selected_symbol not in symbols:
            self.selected_symbol = symbols[0] if symbols else None
        self.coverage_total.configure(text=f"Total: {metrics.total}")
        for widget, value in zip(self.coverage_counts, (metrics.evaluated, metrics.pending, metrics.awaiting_data)):
            widget.configure(text=str(value))
        self.coverage_caption.configure(text=f"Brier uses all {metrics.evaluated} evaluated forecasts.")
        self.population_label.configure(text=f"Direction: {metrics.scored} scored calls; {metrics.neutral} neutral excluded.  Brier: {metrics.evaluated} evaluated, including neutral.\nPending and missing outcomes excluded from performance.")
        excluded = f" · {self.review.excluded_forecasts} unpromoted forecasts excluded" if self.review and self.review.excluded_forecasts else ""
        self.grid_scope.configure(text=f"Grid: 1h execution windows · Summary: {self.horizon.get().lower()} (includes saved opening-gap research){excluded}")
        self._draw_scorecard()
        self._draw_coverage()
        self._draw_selection()
        self._draw_grid()

    def _table_width(self, canvas, scrollbar, minimum):
        visible = max(canvas.winfo_width(), 1)
        width = max(visible, minimum)
        canvas.configure(scrollregion=(0, 0, width, int(canvas.cget("height"))))
        if visible < minimum:
            scrollbar.pack(fill="x", padx=6, pady=(0, 4))
        else:
            scrollbar.pack_forget()
            canvas.xview_moveto(0)
        return width

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

    def _symbol(self, canvas, symbol, y):
        photo = self._photo(symbol)
        if photo:
            canvas.create_image(25, y, image=photo)
        else:
            canvas.create_text(25, y, text=symbol[:2], fill=CYAN, font=("Segoe UI", 8, "bold"))
        canvas.create_text(53, y, text=symbol, fill=TEXT, anchor="w", font=("Segoe UI", 10, "bold"))

    def _draw_scorecard(self):
        canvas = self.score_canvas
        canvas.delete("all")
        width = self._table_width(canvas, self.score_scroll, 700)
        edges = [0, width * .16, width * .47, width * .66, width * .82, width]
        canvas.create_rectangle(0, 0, width, 31, fill=SURFACE_ALT, outline=BORDER)
        for i, title in enumerate(("Symbol", "Direction accuracy", "Correct / Scored", "Brier score ↓", "Bull / Bear calls")):
            canvas.create_text((edges[i] + edges[i+1])/2, 15, text=title, fill=MUTED_TEXT, font=("Segoe UI", 9))
        if not self.review or not self.review.symbols:
            canvas.create_text(width/2, 115, text="No saved predictions to display", fill=MUTED_TEXT, font=("Segoe UI", 11))
            return
        canvas.configure(height=31+35*len(self.review.symbols))
        for index, symbol in enumerate(self.review.symbols):
            metrics = self.review.metrics(self.selected_horizon, symbol)
            top, y = 31+index*35, 48+index*35
            selected = symbol == self.selected_symbol
            canvas.create_rectangle(0, top, width-1, top+35, fill="#102a3c" if selected else TABLE,
                                    outline=CYAN if selected else BORDER, width=1)
            self._symbol(canvas, symbol, y)
            left, right = edges[1]+16, edges[2]-75
            canvas.create_rectangle(left, y-6, right, y+6, fill="#31465a", outline="")
            if metrics.accuracy is not None:
                canvas.create_rectangle(left, y-6, left+(right-left)*metrics.accuracy, y+6,
                                        fill=SUCCESS if metrics.accuracy >= .5 else DANGER, outline="")
            canvas.create_text(edges[2]-12, y, text=percent(metrics.accuracy), anchor="e", fill=TEXT, font=("Segoe UI", 10, "bold"))
            for column, value in ((2, f"{metrics.correct} / {metrics.scored}"), (3, brier_text(metrics.brier)),
                                  (4, f"{metrics.bullish_scored} / {metrics.bearish_scored}")):
                canvas.create_text((edges[column]+edges[column+1])/2, y, text=value, fill=TEXT, font=("Segoe UI", 10))

    def _select_row(self, event):
        self.score_canvas.focus_set()
        row = int((self.score_canvas.canvasy(event.y)-31)//35)
        if self.review and 0 <= row < len(self.review.symbols):
            self.selected_symbol = self.review.symbols[row]
            self._draw_scorecard()
            self._draw_selection()

    def _step_symbol(self, step):
        if self.review and self.review.symbols:
            symbols = self.review.symbols
            index = symbols.index(self.selected_symbol) if self.selected_symbol in symbols else 0
            self.selected_symbol = symbols[max(0, min(len(symbols)-1, index+step))]
            self._draw_scorecard()
            self._draw_selection()
        return "break"

    def _draw_selection(self):
        metrics = self.review.metrics(self.selected_horizon, self.selected_symbol) if self.review and self.selected_symbol else prediction_metrics(())
        self.selected_title.configure(text=f"{self.selected_symbol} · selected" if self.selected_symbol else "Select a company")
        photo = self._photo(self.selected_symbol, 27) if self.selected_symbol else None
        self.selected_mark.configure(image=photo if photo else "")
        self.selected_boxes[0][0].configure(text=f"{metrics.correct} / {metrics.scored}")
        self.selected_boxes[0][1].configure(text=f"{percent(metrics.accuracy)} direction accuracy")
        self.selected_boxes[1][0].configure(text=brier_text(metrics.brier))
        self.selected_boxes[1][1].configure(text=f"{metrics.evaluated} evaluated forecasts")
        self.selected_mix.configure(text=f"{metrics.bullish_scored} bullish · {metrics.bearish_scored} bearish · {metrics.neutral} neutral")

    def _draw_coverage(self):
        canvas = self.coverage_bar
        canvas.delete("all")
        metrics = self.review.metrics(self.selected_horizon) if self.review else prediction_metrics(())
        left, width = 0.0, canvas.winfo_width()
        for count, color in ((metrics.evaluated, SUCCESS), (metrics.pending, WARNING), (metrics.awaiting_data, MUTED_TEXT)):
            right = left + (width*count/metrics.total if metrics.total else 0)
            canvas.create_rectangle(left, 0, right, 16, fill=color, outline="")
            left = right

    def _draw_grid(self):
        canvas = self.grid_canvas
        canvas.delete("all")
        self._cell_bounds = []
        width = self._table_width(canvas, self.grid_scroll, 960)
        symbol_width, score_width = 124, 76
        cell_width = (width-symbol_width-score_width)/13
        canvas.create_rectangle(0, 0, width, 28, fill=SURFACE_ALT, outline=BORDER)
        for i in range(13):
            canvas.create_text(symbol_width+(i+.5)*cell_width, 14, text=f"{i+4:02d}–{i+5:02d}", fill=MUTED_TEXT, font=("Segoe UI", 9))
        canvas.create_text(width-score_width/2, 14, text="1h score", fill=MUTED_TEXT, font=("Segoe UI", 9))
        if not self.review or not self.review.symbols:
            canvas.create_text(width/2, 100, text="The hourly grid will appear when verified results are available.", fill=MUTED_TEXT, font=("Segoe UI", 10))
            return
        canvas.configure(height=28+28*len(self.review.symbols))
        for row_index, symbol in enumerate(self.review.symbols):
            top, y = 28+row_index*28, 42+row_index*28
            self._symbol(canvas, symbol, y)
            outcomes = self.review.hourly(symbol)
            for col, outcome in enumerate(outcomes):
                glyph, foreground, background, _ = CELL_STYLES[outcome.state if outcome else "not_saved"]
                left, right = symbol_width+col*cell_width, symbol_width+(col+1)*cell_width
                canvas.create_rectangle(left, top, right, top+28, fill=background, outline=BORDER)
                canvas.create_text((left+right)/2, y, text=glyph, fill=foreground, font=("Segoe UI", 14, "bold"))
                self._cell_bounds.append((left, top, right, top+28, symbol, col, outcome))
            metrics = prediction_metrics(tuple(row for row in outcomes if row is not None))
            canvas.create_text(width-score_width/2, y, text=f"{metrics.correct} / {metrics.scored}", fill=TEXT, font=("Segoe UI", 10, "bold"))

    def _grid_click(self, event):
        x, y = self.grid_canvas.canvasx(event.x), self.grid_canvas.canvasy(event.y)
        for left, top, right, bottom, symbol, col, outcome in self._cell_bounds:
            if left <= x < right and top <= y < bottom:
                self.selected_symbol = symbol
                self._draw_scorecard()
                self._draw_selection()
                self._show_outcome(symbol, col, outcome)
                break

    def _show_outcome(self, symbol, col, outcome):
        title = f"{symbol} · {col+4:02d}:00–{col+5:02d}:00 Pacific"
        if outcome is None:
            self._text_dialog(title, "No forecast was saved for this window.")
            return
        observed_start = outcome.observed_start.strftime("%b %d, %H:%M %Z") if outcome.observed_start else "Unavailable"
        observed_end = outcome.observed_end.strftime("%b %d, %H:%M %Z") if outcome.observed_end else "Unavailable"
        move = "Unavailable" if outcome.actual_return is None else f"{outcome.actual_return:+.3%}"
        text = (f"Saved call: {outcome.direction.replace('NO_EDGE', 'NEUTRAL').title()}\n"
                f"Outcome: {CELL_STYLES[outcome.state][3]}\n\n"
                f"Saved model probability: {outcome.probability:.2%}\n"
                f"Probability target: return above {outcome.probability_target_threshold:.2%}\n"
                f"Separate assumed round-trip cost: {outcome.target_cost:.2%}\n"
                f"Observed return: {move}\nBrier score: {brier_text(outcome.brier)}\n\n"
                f"Target start: {outcome.start:%b %d, %H:%M %Z}\nTarget end: {outcome.end:%b %d, %H:%M %Z}\n"
                f"Start observation: {observed_start}\nEnd observation: {observed_end}\n\n"
                "Direction checks the sign of the actual return. Probability scores retain this forecast's saved target.\n"
                "Observations follow the review's five-minute boundary tolerance; these are market outcomes, not broker fills.")
        self._text_dialog(title, text)

    def _probability_definition(self):
        costs = sorted({row.probability_target_threshold for row in self.review.rows(self.selected_horizon)}) if self.review else [.001]
        target = ", ".join(f"{cost:.2%}" for cost in costs) or "the saved cost threshold"
        return ("Brier score: average squared error of the saved model probability against its actual binary target. "
                f"Target: return above {target} over the saved window. Lower is better; zero is perfect. "
                "Includes evaluated neutral forecasts. This is a score, not a percent of incorrect calls.")

    def show_definitions(self):
        self._text_dialog("Gameplan Stats · metric definitions",
            "Direction accuracy\nCorrect saved bullish/bearish calls divided by scored calls. "
            "A bullish call is correct when the actual return is positive; a bearish call when it is negative. "
            "Neutral calls are excluded. Zero-return moves are incorrect for either direction.\n\n"
            "Bullish / bearish accuracy\nThe same check, split by saved call direction. "
            "A dash means no calls were scored. Always read accuracy alongside the sample count.\n\n"
            + self._probability_definition() + "\n\n"
            "Coverage and scope\nOnly promoted saved forecasts contribute. Summary metrics follow the horizon filter, "
            "including saved opening-gap research in the 1h group. Pending and missing outcomes do not count as failures. "
            "The bottom grid always shows the thirteen 1h execution windows for the selected session. "
            "Its score excludes neutral, pending and missing cells.\n\n"
            "Results are a saved review snapshot, not a live rescore. Refresh loads newly published reviews; "
            "it does not fetch prices or mature an old forecast. OG Brier retains its cost-adjusted target; YG Brier uses raw positive returns. "
            "Neutral calls are included only in Brier; flat moves fail either directional call. "
            "These are prediction outcomes, not realized trading profit.")

    def _text_dialog(self, title, text):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.configure(background=SURFACE)
        dialog.transient(self.root)
        dialog.geometry("660x510")
        box = tk.Text(dialog, wrap="word", background=SURFACE, foreground=TEXT, font=("Segoe UI", 11),
                      padx=18, pady=16, relief="flat", insertbackground=TEXT)
        scrollbar = ttk.Scrollbar(dialog, command=box.yview)
        scrollbar.pack(side="right", fill="y")
        box.configure(yscrollcommand=scrollbar.set)
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        box.configure(state="disabled")
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=10)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def open_report(self):
        if self.review is None or self._loading:
            return
        try:
            path = self.review.report_path
            if os.name == "nt":
                os.startfile(str(path))
            else:
                webbrowser.open(path.as_uri())
        except OSError as exc:
            messagebox.showerror("Could not open report", str(exc), parent=self.root)

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
