"""Active Stats workspace for saved independent 15-minute evidence."""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from app.ui.gameplan_widgets import label, Tooltip
from app.ui.scout_stats_data import (
    ScoutStatsSnapshot, execution_summary, load_scout_stats,
    prediction_summary, scout_stats_sessions,
)
from app.ui.scout_stats_widgets import ScoutStatsPanel
from app.ui.theme import BACKGROUND, MUTED_TEXT, SURFACE, TEXT

AUTO_REFRESH_MS = 5 * 60 * 1000


class StatsTab:
    """Local or verified peer 15m results, with no retired Gameplan loaders."""

    def __init__(self, root, parent, *, datastore_root: Path | None = None, auto_load=True):
        self.root, self.parent, self.datastore_root = root, parent, datastore_root
        self.session = tk.StringVar(master=root)
        self.status = tk.StringVar(master=root, value="Loading saved 15-Minute results…" if auto_load else "No results loaded.")
        self._request_id = 0
        self._closed = self._loading = False
        self._follow_latest = True
        self._auto_enabled = auto_load
        self._last_load = 0.0
        self._messages = queue.Queue()
        self._build()
        self._poll_job = parent.after(80, self._poll)
        self._auto_job = parent.after(AUTO_REFRESH_MS, self._auto_refresh)
        parent.bind("<Destroy>", self._destroy, add="+")
        parent.bind("<Map>", self._mapped, add="+")
        if auto_load:
            parent.after_idle(lambda: self.refresh() if not self._closed and not self._loading else None)

    def _build(self):
        self.page = tk.Frame(self.parent, background=BACKGROUND)
        self.page.pack(fill="both", expand=True, padx=18, pady=12)
        self.header = tk.Frame(self.page, background=BACKGROUND)
        self.header.pack(fill="x")
        self.header.columnconfigure(0, weight=1)
        self.heading = label(self.header, "Stats", size=21, bold=True, background=BACKGROUND)
        self.heading.grid(row=0, column=0, sticky="w")
        self.controls = tk.Frame(self.header, background=BACKGROUND)
        self.controls.grid(row=0, column=1, sticky="e")
        self.date_box = ttk.Combobox(self.controls, textvariable=self.session, state="readonly", width=13)
        self.date_box.pack(side="left", padx=(0, 8))
        self.date_box.bind("<<ComboboxSelected>>", self._date_changed)
        Tooltip(self.date_box, "Select the Pacific session date of saved 15-Minute predictions and orders.")
        self.latest_button = ttk.Button(self.controls, text="Latest session", command=self.follow_latest)
        self.latest_button.pack(side="left", padx=(0, 8))
        self.report_button = ttk.Button(self.controls, text="Session summary", command=self.open_report, state="disabled")
        self.report_button.pack(side="left", padx=(0, 8))
        self.refresh_button = ttk.Button(self.controls, text="Refresh", command=self.refresh)
        self.refresh_button.pack(side="left")
        label(self.page, "15-Minute Trader · Predictions, orders and fills · All times Pacific",
              color=MUTED_TEXT, background=BACKGROUND).pack(anchor="w", pady=(3, 2))
        self.status_label = label(self.page, textvariable=self.status, size=9, color=MUTED_TEXT,
                                  background=BACKGROUND, justify="left", wraplength=1000)
        self.status_label.pack(fill="x", pady=(0, 10))
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
        self.scout = ScoutStatsPanel(self.body, self.root)
        self.scout.frame.pack(fill="both", expand=True)
        self._bind_wheel(self.body)

    def _resize(self, event):
        self.canvas.itemconfigure(self._body_window, width=max(1, event.width))
        if event.width < 760:
            self.controls.grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        else:
            self.controls.grid(row=0, column=1, columnspan=1, sticky="e", pady=0)
        self.status_label.configure(wraplength=max(240, event.width - 20))

    def _scroll_changed(self, first, last):
        self.scrollbar.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.scrollbar.pack_forget()
        else:
            self.scrollbar.pack(side="right", fill="y")

    def _bind_wheel(self, widget):
        if isinstance(widget, (ttk.Treeview, tk.Text, ttk.Combobox, ttk.Scrollbar)):
            return
        widget.bind("<MouseWheel>", self._wheel, add="+")
        for child in widget.winfo_children():
            self._bind_wheel(child)

    def _wheel(self, event):
        if event.delta and self.canvas.bbox("all"):
            self.canvas.yview_scroll(-int(event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
        return "break"

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
        self.status.set("Reading saved 15-Minute results…")
        self.refresh_button.configure(state="disabled")
        self.report_button.configure(state="disabled")
        self.scout.set_snapshot(None)
        # Workers capture values and a queue; they never retain or call Tk widgets.
        datastore, messages = self.datastore_root, self._messages

        def work():
            try:
                dates = tuple(sorted(set(scout_stats_sessions(datastore)), reverse=True))
            except Exception:
                dates = ()
            requested = selected or (dates[0] if dates else None)
            try:
                snapshot = load_scout_stats(datastore, requested)
                if requested and snapshot.session != requested:
                    raise ValueError("Saved snapshot differs from the selected session")
            except Exception:
                snapshot = ScoutStatsSnapshot(session=requested, status="unreadable",
                    prediction_status="unreadable", execution_status="unreadable",
                    message="Saved 15-Minute evidence could not be read. Refresh to retry.")
            messages.put((request, dates, snapshot))

        threading.Thread(target=work, name="15m-stats-read", daemon=True).start()

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                request, dates, snapshot = self._messages.get_nowait()
                if request != self._request_id:
                    continue
                self._loading = False
                self._last_load = time.monotonic()
                self.refresh_button.configure(state="normal")
                displayed = snapshot.session if snapshot else None
                self.date_box.configure(values=tuple(sorted(set(dates) | ({displayed} if displayed else set()), reverse=True)))
                self.session.set(displayed or "")
                self.scout.set_snapshot(snapshot)
                self.status.set(snapshot.message if snapshot else "No saved 15-Minute results available.")
                self.report_button.configure(state="normal" if snapshot and displayed else "disabled")
        except queue.Empty:
            pass
        self._poll_job = self.parent.after(80, self._poll)

    def open_report(self):
        """Summarize the displayed 15m snapshot, never a retired report path."""
        saved = self.scout.snapshot
        if saved is None or not saved.session:
            return
        prediction = prediction_summary(saved.forecasts)
        execution = execution_summary(saved.orders)
        accuracy = "Unavailable" if prediction["accuracy"] is None else f"{prediction['accuracy']:.1%}"
        brier = "Unavailable" if prediction["brier"] is None else f"{prediction['brier']:.6f}"
        lines = [f"15-Minute Trader · {saved.session}", saved.message, "",
            f"Source: {saved.source}", f"Predictions: {saved.prediction_status}; execution: {saved.execution_status}", ""]
        if ScoutStatsPanel._available(saved.prediction_status):
            lines.extend([
            f"Live forecasts: {prediction['issued']} · Before target open: {prediction['eligible']}",
            f"Observed scored: {prediction['scored']} · Correct: {prediction['correct']}",
            f"Accuracy: {accuracy} · Brier: {brier}",
            f"Missing: {prediction['missing']} · Pending: {prediction['pending']}",
            f"Synthetic: {prediction['synthetic']} · Unscorable: {prediction['unscorable']}", ""])
        else:
            lines.extend(["Prediction counts and scores: unavailable.", ""])
        if ScoutStatsPanel._available(saved.execution_status):
            lines.extend([
            f"Saved order requests: {execution['attempts']} · Recorded submitted: {execution['submitted']}",
            f"Filled: {execution['filled']} · Partial: {execution['partial']} · Working: {execution['working']}",
            f"Cancelled: {execution['cancelled']} · Rejected: {execution['rejected']} · Unknown: {execution['unknown']}",
            f"Known filled quantity: {execution['filled_quantity']} · Identified fills: {execution['fill_records']}", ""])
        else:
            lines.extend(["Order counts and filled quantity: unavailable.", ""])
        lines.extend([
            "Realized P&L: unavailable. Forecast correctness and filled quantities are not trading profit.",
            "Only forecasts issued before target open with observed, scorable outcomes enter accuracy and Brier.",
            *saved.warnings])
        dialog = tk.Toplevel(self.root)
        dialog.title(f"15-Minute Trader · {saved.session} · Session summary")
        dialog.geometry("780x560")
        text = tk.Text(dialog, wrap="word", background=SURFACE, foreground=TEXT, padx=18, pady=14)
        scroll = ttk.Scrollbar(dialog, command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.pack(fill="both", expand=True)
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")

    def _mapped(self, event):
        if self._auto_enabled and event.widget is self.parent and not self._loading and time.monotonic() - self._last_load > 30:
            self.refresh()

    def _auto_refresh(self):
        if not self._closed:
            if self._auto_enabled and self.parent.winfo_ismapped() and not self._loading:
                self.refresh()
            self._auto_job = self.parent.after(AUTO_REFRESH_MS, self._auto_refresh)

    def _destroy(self, event):
        if event.widget is not self.parent or self._closed:
            return
        self._closed = True
        self._request_id += 1
        for job in (self._poll_job, self._auto_job):
            self.parent.after_cancel(job)
