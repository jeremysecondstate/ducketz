"""Stats workspace for each trader's independently saved 15-minute evidence."""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from app.ui.atlas_stats_data import AtlasStatsSnapshot, atlas_stats_sessions, load_atlas_stats
from app.ui.gameplan_widgets import label
from app.ui.scout_stats_data import ScoutStatsSnapshot, load_scout_stats, scout_stats_sessions
from app.ui.scout_stats_widgets import AtlasStatsPanel, ScoutStatsPanel
from app.ui.theme import BACKGROUND, MUTED_TEXT, TEXT

AUTO_REFRESH_MS = 5 * 60 * 1000


class StatsTab:
    def __init__(self, root, parent, *, datastore_root=None, auto_load=True):
        self.root, self.parent, self.datastore_root = root, parent, datastore_root
        self.session = tk.StringVar(master=root)
        self.actor = tk.StringVar(master=root, value="Atlas")
        self.status = tk.StringVar(master=root, value="Loading saved results…" if auto_load else "No results loaded.")
        self._messages = queue.Queue()
        self._request_id = 0
        self._closed = self._loading = False
        self._follow_latest = True
        self._auto_enabled = auto_load
        self._last_load = 0.0
        self._pending = set()
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
        self.latest_button = ttk.Button(self.controls, text="Latest session", command=self.follow_latest)
        self.latest_button.pack(side="left", padx=(0, 8))
        self.refresh_button = ttk.Button(self.controls, text="Refresh", command=self.refresh)
        self.refresh_button.pack(side="left")
        self.subtitle = label(self.page, "Independent 15-minute results · All times Pacific · Saved evidence only",
                              color=MUTED_TEXT, background=BACKGROUND, justify="left")
        self.subtitle.pack(fill="x", pady=(4, 10))
        self.traders = tk.Frame(self.page, background=BACKGROUND)
        self.traders.pack(fill="x", pady=(0, 6))
        style = ttk.Style(self.root)
        style.configure("Stats.TRadiobutton", background=BACKGROUND, foreground=TEXT,
                        font=("Segoe UI", 10), padding=(2, 4))
        style.map("Stats.TRadiobutton", background=[("active", BACKGROUND)])
        for actor in ("Atlas", "Scout"):
            ttk.Radiobutton(self.traders, text=f"{actor} 15-Minute", variable=self.actor,
                            value=actor, command=self._actor_changed,
                            style="Stats.TRadiobutton").pack(side="left", padx=(0, 16))
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
        self.atlas = AtlasStatsPanel(self.body, self.root)
        self.scout = ScoutStatsPanel(self.body, self.root)
        self._actor_changed()
        self._bind_wheel(self.body)

    def _actor_changed(self):
        self.atlas.frame.pack_forget()
        self.scout.frame.pack_forget()
        panel = self.scout if self.actor.get() == "Scout" else self.atlas
        panel.frame.pack(fill="x")
        self.canvas.yview_moveto(0)

    def _resize(self, event):
        self.canvas.itemconfigure(self._body_window, width=max(1, event.width))
        self.controls.grid(row=1 if event.width < 800 else 0,
                           column=0 if event.width < 800 else 1,
                           columnspan=2 if event.width < 800 else 1,
                           sticky="w" if event.width < 800 else "e",
                           pady=(6, 0) if event.width < 800 else 0)
        for widget in (self.subtitle, self.status_label):
            widget.configure(wraplength=max(200, event.width - 8))

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
        bounds = self.canvas.bbox("all")
        if event.delta and bounds and bounds[3] > self.canvas.winfo_height():
            self.canvas.yview_scroll(-int(event.delta / 120) or (-1 if event.delta > 0 else 1), "units")
        return "break"

    def _date_changed(self, _event=None):
        self._follow_latest = False
        self.canvas.yview_moveto(0)
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
        self._pending = {"Atlas", "Scout"}
        self._loading = True
        self.status.set("Reading saved 15-minute results…")
        self.refresh_button.configure(state="disabled")
        self.atlas.set_snapshot(None)
        self.scout.set_snapshot(None)
        # Readers run outside Tk and retain no widgets. Each completed projection
        # can display immediately while the other trader's reader is still busy.
        messages, datastore = self._messages, self.datastore_root
        readers = (("Atlas", atlas_stats_sessions, load_atlas_stats, AtlasStatsSnapshot),
                   ("Scout", scout_stats_sessions, load_scout_stats, ScoutStatsSnapshot))

        def work():
            dates, errors = set(), set()
            for actor, sessions, _, _ in readers:
                try:
                    dates.update(sessions(datastore))
                except Exception:
                    errors.add(actor)
            dates = tuple(sorted(dates, reverse=True))
            session = selected or (dates[0] if dates else None)

            def read(actor, loader, snapshot_type):
                try:
                    if session is None:
                        if actor in errors:
                            raise OSError("Session inventory unavailable")
                        snapshot = snapshot_type(message=f"No saved {actor} 15-Minute sessions are available.")
                    else:
                        snapshot = loader(datastore, session)
                except Exception:
                    snapshot = snapshot_type(session=session, status="unreadable",
                        message=f"{actor} saved evidence could not be read. Refresh to retry.",
                        prediction_status="unreadable", execution_status="unreadable")
                messages.put((request, actor, dates, session, snapshot))

            for actor, _, loader, snapshot_type in readers:
                threading.Thread(target=read, args=(actor, loader, snapshot_type),
                                 name=f"stats-{actor.lower()}-read", daemon=True).start()

        threading.Thread(target=work, name="stats-session-read", daemon=True).start()

    def _poll(self):
        if self._closed:
            return
        try:
            while True:
                request, actor, dates, session, snapshot = self._messages.get_nowait()
                if request != self._request_id:
                    continue
                self.date_box.configure(values=dates or ((session,) if session else ()))
                self.session.set(session or "")
                panel = self.atlas if actor == "Atlas" else self.scout
                panel.set_snapshot(snapshot)
                self._pending.discard(actor)
                self._loading = bool(self._pending)
                if not self._loading:
                    self._last_load = time.monotonic()
                    self.refresh_button.configure(state="normal")
                self.status.set((f"{session} · " if session else "") +
                    ("Reading " + ", ".join(sorted(self._pending)) + " saved results…" if self._pending else
                     "Saved 15-minute evidence · Choose a trader to review its results."))
        except queue.Empty:
            pass
        self._poll_job = self.parent.after(80, self._poll)

    def _mapped(self, event):
        if (event.widget is self.parent and self._auto_enabled and not self._loading
                and time.monotonic() - self._last_load > 30):
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
