"""Compact, session-independent display of current rolling market-price evidence."""
from __future__ import annotations

import math
import tkinter as tk
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.ui.gameplan_widgets import Tooltip, label, panel
from app.ui.theme import BORDER, DANGER, MUTED_TEXT, SUCCESS, SURFACE, TEXT, WARNING

PACIFIC = ZoneInfo("America/Los_Angeles")


def change_text(value: float | None) -> str:
    """Round only the displayed percentage; never turn absent evidence into zero."""
    if value is None or not math.isfinite(value):
        return "Unavailable"
    return f"{value:+.2f}%"


def change_color(value: float | None) -> str:
    if value is None or not math.isfinite(value) or value == 0:
        return MUTED_TEXT
    return SUCCESS if value > 0 else DANGER


def pacific_time(value: datetime | None, *, seconds: bool = False) -> str:
    if value is None:
        return "Not available"
    return value.astimezone(PACIFIC).strftime("%b %d, %H:%M:%S %Z" if seconds else "%b %d, %H:%M %Z")


class RollingPricePanel:
    """Presentation only: callers provide independent background-loaded snapshots."""

    def __init__(self, parent):
        self.frame = panel(parent)
        self.snapshot = None
        self.selected_session = None
        self.loading = False
        self.tiles = []
        self.symbol_labels = {}
        self.value_labels = {}
        self.state_labels = {}
        self._columns = 0

        self.title_label = label(self.frame, "Rolling 24-Hour Price Change", size=13, bold=True)
        self.title_label.pack(anchor="w", padx=12, pady=(9, 3))
        self.summary = tk.Frame(self.frame, background=SURFACE)
        self.summary.pack(fill="x", padx=12)
        self.summary.columnconfigure(0, weight=1)
        self.average_title = label(self.summary, "Symbol average", size=10, bold=True)
        self.average_title.grid(row=0, column=0, sticky="w")
        self.average_value = label(self.summary, "Unavailable", size=18, bold=True, color=MUTED_TEXT)
        self.average_value.grid(row=0, column=1, sticky="e")
        self.coverage_label = label(self.summary, "Waiting for local symbol profile", size=8, color=MUTED_TEXT)
        self.coverage_label.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 4))
        Tooltip(self.average_value, self._average_detail)

        self.grid_frame = tk.Frame(self.frame, background=SURFACE)
        self.grid_frame.pack(fill="x", padx=9, pady=(1, 3))
        self.status_label = label(self.frame, "Current market prices · Not loaded", size=8, color=MUTED_TEXT,
                                  justify="left", wraplength=450)
        self.status_label.pack(fill="x", padx=12, pady=(2, 1))
        self.context_label = label(self.frame, "Current prices · Independent of Stats session and order filters",
                                   size=8, color=MUTED_TEXT, justify="left", wraplength=450)
        self.context_label.pack(fill="x", padx=12, pady=(0, 8))
        Tooltip(self.title_label, self._policy_detail)
        Tooltip(self.status_label, self._policy_detail)
        self.frame.bind("<Configure>", self._resize, add="+")

    def set_loading(self):
        self.loading = True
        if self.snapshot is None:
            self.status_label.configure(text="Current market prices · Loading…")
        else:
            self.status_label.configure(text=f"As of {pacific_time(self.snapshot.as_of)} · Refreshing…")

    def set_session_context(self, session):
        self.selected_session = session or None
        self._context()

    def set_snapshot(self, snapshot):
        self.snapshot = snapshot
        self.loading = False
        symbols = tuple(row.symbol for row in snapshot.rows)
        if tuple(self.symbol_labels) != symbols:
            for child in self.grid_frame.winfo_children():
                child.destroy()
            self.tiles.clear()
            self.symbol_labels.clear()
            self.value_labels.clear()
            self.state_labels.clear()
            for symbol in symbols:
                tile = tk.Frame(self.grid_frame, background=SURFACE, highlightbackground=BORDER, highlightthickness=1)
                tile.columnconfigure(1, weight=1)
                symbol_label = label(tile, symbol, size=10, bold=True)
                symbol_label.grid(row=0, column=0, sticky="w", padx=(7, 4), pady=(4, 0))
                value_label = label(tile, "Unavailable", size=11, bold=True, color=MUTED_TEXT)
                value_label.grid(row=0, column=1, sticky="e", padx=(0, 7), pady=(4, 0))
                state_label = label(tile, "", size=8, color=MUTED_TEXT)
                state_label.grid(row=1, column=0, columnspan=2, sticky="w", padx=7, pady=(0, 4))
                self.tiles.append(tile)
                self.symbol_labels[symbol] = symbol_label
                self.value_labels[symbol] = value_label
                self.state_labels[symbol] = state_label
                for widget in (tile, symbol_label, value_label, state_label):
                    Tooltip(widget, lambda symbol=symbol: self._symbol_detail(symbol))
            self._columns = 0

        for row in snapshot.rows:
            eligible = row.status in {"available", "closed"}
            value = row.change_pct if eligible else None
            self.value_labels[row.symbol].configure(text=change_text(value), foreground=change_color(value))
            state = {"available": "", "closed": "Closed · as-of prices",
                     "stale": "Stale evidence", "unavailable": "Data unavailable"}.get(row.status, "Data unavailable")
            self.state_labels[row.symbol].configure(text=state, foreground=WARNING if row.status == "stale" else MUTED_TEXT)
            if state:
                self.state_labels[row.symbol].grid()
            else:
                self.state_labels[row.symbol].grid_remove()
            for widget in (self.symbol_labels[row.symbol], self.value_labels[row.symbol]):
                widget.grid_configure(pady=(5, 0 if state else 5))
        self.average_title.configure(text=f"{snapshot.total_count}-symbol average")
        self.average_value.configure(text=change_text(snapshot.average_pct), foreground=change_color(snapshot.average_pct))
        self.coverage_label.configure(text=f"{snapshot.available_count}/{snapshot.total_count} available · Equal weighting")
        closed = sum(row.status == "closed" for row in snapshot.rows)
        stale = sum(row.status == "stale" for row in snapshot.rows)
        status = "All prices available" if snapshot.average_pct is not None else "Incomplete price coverage"
        if closed:
            status = f"Market closed · {closed} as-of pairs" if closed == snapshot.total_count else status + f" · {closed} closed"
        if stale:
            status += f" · {stale} stale"
        if snapshot.error:
            status = "Price data unavailable"
        self.status_label.configure(text=f"As of {pacific_time(snapshot.as_of)} · {status}")
        self._context()
        self._resize()

    def _context(self):
        prefix = "Current prices"
        if self.selected_session:
            prefix += f" · Stats session {self.selected_session}"
        self.context_label.configure(text=prefix + " · Independent of session and order filters")

    def _resize(self, event=None):
        width = event.width if event is not None else self.frame.winfo_width()
        columns = max(1, min(4, (max(width, 160) - 20) // 170))
        if columns != self._columns:
            for index in range(max(self._columns, columns)):
                self.grid_frame.columnconfigure(index, weight=1 if index < columns else 0,
                                                uniform="rolling-symbol" if index < columns else "")
            for index, tile in enumerate(self.tiles):
                tile.grid(row=index // columns, column=index % columns, sticky="nsew", padx=3, pady=3)
            self._columns = columns
        wraplength = max(120, width - 28)
        self.status_label.configure(wraplength=wraplength)
        self.context_label.configure(wraplength=wraplength)
        self.title_label.configure(wraplength=wraplength)

    def _average_detail(self):
        if self.snapshot is None:
            return "Equal-weight average of every symbol in this PC's production watchlist. Missing prices are never zero."
        return (f"Equal-weight average of all {self.snapshot.total_count} unrounded percentage changes.\n"
                f"Coverage: {self.snapshot.available_count}/{self.snapshot.total_count}.\n"
                "The full average is unavailable whenever any symbol is missing or stale.")

    def _policy_detail(self):
        if self.snapshot is None:
            return "True rolling 24 elapsed hours. Current market prices refresh independently of saved prediction/order evidence."
        return (f"Source: {self.snapshot.source}\nAs of (UTC): {self.snapshot.as_of.isoformat()}\n"
                f"Earlier cutoff (UTC): {(self.snapshot.as_of - timedelta(hours=24)).isoformat()}\n"
                f"{self.snapshot.policy}" + (f"\n{self.snapshot.error}" if self.snapshot.error else ""))

    def _symbol_detail(self, symbol):
        if self.snapshot is None:
            return "No price evidence loaded."
        row = next(row for row in self.snapshot.rows if row.symbol == symbol)
        lines = [f"{symbol} · {row.status}", row.reason]
        for title, observation in (("Current cutoff", row.current), ("24h earlier cutoff", row.previous)):
            if observation is None:
                lines.append(f"{title}: price unavailable")
                continue
            lines.append(f"{title}: ${observation.price:.6g} · {pacific_time(observation.observed_at, seconds=True)}")
            lines.append(f"Eligible observation bound (UTC): {observation.observed_at.isoformat()}")
            if observation.interval_start is not None:
                lines.append(f"Candle start (UTC): {observation.interval_start.isoformat()}")
            lines.append(f"Source: {observation.source} · Basis: {observation.basis}")
        return "\n".join(lines)
