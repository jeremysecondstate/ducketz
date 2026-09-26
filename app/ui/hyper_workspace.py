"""Read-only H.Y.P.E.R. Paper/Powder workspace.

Only the loader thread touches local artifacts. It communicates with Tk through
a queue; neither mode owns an execution client or a runtime lifecycle control.
"""
from __future__ import annotations

import json
import math
import queue
import threading
import tkinter as tk
from datetime import datetime, timezone, timedelta
from tkinter import ttk
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.hyperliquid_paper_view import HyperliquidPaperViewService, filter_rows
from app.ui.theme import TEXT, MUTED_TEXT, DANGER, WARNING

PAGE = "#071522"
PANEL = "#0c2031"
INSET = "#0a1b2b"
LINE = "#233e52"
MINT = "#78edc1"
ACCOUNTS = {"alex": ("Alex", "Short perps", "AL"),
            "jeremy": ("Jeremy", "Long perps", "JE"),
            "clearpond": ("Clear Pond", "Spot", "CP")}
ACCOUNT_KEYS = {label: key for key, (label, _, _) in ACCOUNTS.items()}
REASONS = {"entry_deadband": "Below entry threshold", "hold_with_hysteresis": "Hold within exit band",
           "signal_rebalance": "Adjust to forecast target", "entry_based_stop": "Entry-based stop",
           "paper_target_collateral_allocation": "Fund target collateral", "no_change": "Target already met"}


def number(value):
    try:
        return float(value) if not isinstance(value, bool) and math.isfinite(float(value)) else None
    except (ValueError, TypeError, OverflowError):
        return None


def money(value, signed=False):
    value = number(value)
    if value is None:
        return "—"
    return ("−" if value < 0 else "+" if signed and value > 0 else "") + f"${abs(value):,.2f}"


def pct(value):
    value = number(value)
    return "—" if value is None else f"{value * 100:,.2f}%"


def quantity(value):
    value = number(value)
    return "—" if value is None else f"{value:,.8f}".rstrip("0").rstrip(".")


def timestamp(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, TypeError):
        return None


def local_time(value, *, date=False):
    stamp = timestamp(value)
    if stamp is None:
        return "Unknown time"
    try:
        return stamp.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%b %d · %H:%M:%S PT" if date else "%H:%M:%S PT")
    except ZoneInfoNotFoundError:
        return stamp.strftime("%b %d · %H:%M:%S UTC" if date else "%H:%M:%S UTC")


def age_text(seconds):
    value = number(seconds)
    if value is None:
        return "age unknown"
    if value < 0:
        return "clock ahead"
    if value < 60:
        return f"{int(value)}s ago"
    if value < 3600:
        return f"{int(value / 60)}m ago"
    return f"{value / 3600:.1f}h ago"


def label(parent, text="", *, size=10, color=TEXT, bold=False, **kwargs):
    return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=color, anchor="w",
                    font=("Segoe UI", size, "bold" if bold else "normal"), **kwargs)


def card(parent, **kwargs):
    return tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1, **kwargs)


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text, self.window = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")

    def show(self, _event=None):
        text = self.text() if callable(self.text) else self.text
        if not text or self.window:
            return
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{self.widget.winfo_rootx() + 12}+{self.widget.winfo_rooty() + self.widget.winfo_height() + 3}")
        tk.Label(self.window, text=text, bg=INSET, fg=TEXT, padx=10, pady=8,
                 justify="left", wraplength=520, relief="solid", borderwidth=1,
                 font=("Segoe UI", 9)).pack()

    def hide(self, _event=None):
        if self.window:
            self.window.destroy()
            self.window = None


class HyperWorkspace:
    def __init__(self, root, parent, *, service=None, auto_load=True, refresh_ms=5000):
        self.root, self.parent = root, parent
        self.service = service if service is not None else HyperliquidPaperViewService()
        self.snapshot = None
        self.refresh_ms = max(100, int(refresh_ms))
        self._closed = self._loading = False
        self._auto_enabled = auto_load
        self._messages = queue.Queue(maxsize=1)
        self._jobs = {}
        self._rows_by_id = {}
        self._selections = {}
        self._table_view = None
        self._layout_mode = None
        self._chart_points = []
        self._detail_content = None
        self._detail_key = None
        self._inspector_forecast = None
        self._error = ""
        self.mode = tk.StringVar(root, "Paper")
        self.account_filter = tk.StringVar(root, "All accounts")
        self.asset_filter = tk.StringVar(root, "All assets")
        self.qualification_filter = tk.StringVar(root, "All models")
        self.view = tk.StringVar(root, "Positions")
        self.chart_metric = tk.StringVar(root, "P/L")
        self.chart_range = tk.StringVar(root, "All")
        self.include_passive = tk.BooleanVar(root, False)
        self.show_record = tk.BooleanVar(root, False)
        self._styles()
        self._build()
        parent.bind("<Destroy>", self._destroy, add="+")
        self._wheel_binding = root.bind("<MouseWheel>", self._wheel, add="+")
        for var in (self.account_filter, self.asset_filter, self.qualification_filter, self.include_passive):
            var.trace_add("write", lambda *_: self._render())
        self.mode.trace_add("write", lambda *_: self._choose_mode())
        for var in (self.chart_metric, self.chart_range):
            var.trace_add("write", lambda *_: self._draw_chart())
        self.show_record.trace_add("write", lambda *_: self._render_table())
        self._schedule("poll", 100, self._poll)
        if auto_load:
            self._schedule("initial", 1, self.refresh)
            self._schedule("auto", self.refresh_ms, self._auto_refresh)
        self._render()

    def _styles(self):
        style = ttk.Style(self.root)
        style.configure("HYPER.TButton", background=PANEL, foreground=TEXT, bordercolor=LINE,
                        padding=(10, 5), font=("Segoe UI", 9))
        style.map("HYPER.TButton", background=[("active", "#183f49")], foreground=[("disabled", MUTED_TEXT)])
        style.configure("HYPER.TRadiobutton", background=PANEL, foreground=TEXT, padding=(9, 5),
                        font=("Segoe UI", 9), indicatoron=False)
        style.layout("HYPER.TRadiobutton", [("Radiobutton.padding", {"children": [("Radiobutton.label", {"sticky": "nswe"})]})])
        style.map("HYPER.TRadiobutton", background=[("selected", "#173e40"), ("active", "#163346")],
                  foreground=[("selected", MINT)])
        style.configure("HYPER.TCombobox", fieldbackground=INSET, background=PANEL, foreground=TEXT,
                        arrowcolor=MINT, bordercolor=LINE, padding=4)
        style.map("HYPER.TCombobox", fieldbackground=[("readonly", INSET)], foreground=[("readonly", TEXT)],
                  selectbackground=[("readonly", INSET)], selectforeground=[("readonly", TEXT)])
        style.configure("HYPER.Treeview", background=INSET, fieldbackground=INSET, foreground=TEXT,
                        rowheight=31, borderwidth=0, font=("Segoe UI", 9))
        style.configure("HYPER.Treeview.Heading", background=PANEL, foreground=MUTED_TEXT,
                        relief="flat", font=("Segoe UI", 9))
        style.map("HYPER.Treeview", background=[("selected", "#194849")], foreground=[("selected", TEXT)])
        style.configure("HYPER.TCheckbutton", background=PANEL, foreground=MUTED_TEXT, font=("Segoe UI", 8))

    def _build(self):
        self.page = tk.Frame(self.parent, bg=PAGE)
        self.page.pack(fill="both", expand=True)
        mode_row = tk.Frame(self.page, bg=PAGE)
        mode_row.pack(fill="x", padx=14, pady=(9, 7))
        for mode in ("Paper", "Powder"):
            ttk.Radiobutton(mode_row, text=f"  {mode}  ", value=mode, variable=self.mode,
                            style="HYPER.TRadiobutton").pack(side="left", padx=(0, 2))
        self.badge = label(mode_row, "SIMULATED", size=9, color=MINT, bold=True, padx=12)
        self.badge.pack(side="left", padx=(10, 2))
        self.mode_status = label(mode_row, "Loading local records…", size=9, color=MUTED_TEXT)
        self.mode_status.pack(side="left", padx=8)
        self.refresh_button = ttk.Button(mode_row, text="↻  Refresh view", command=self.refresh, style="HYPER.TButton")
        self.refresh_button.pack(side="right")
        ttk.Button(mode_row, text="Operations", command=self._show_operations, style="HYPER.TButton").pack(side="right", padx=6)
        self.ops = card(self.page)
        self.ops.pack(fill="x", padx=14, pady=(0, 6))
        self.source_labels, self.source_boxes = {}, []
        for i, (key, title, cadence) in enumerate((("data", "Data / features", "15m candles"),
                    ("forecasts", "Forecasts", "15m · 1h horizon"), ("models", "Models", "Hourly fit · 5s poll"),
                    ("paper", "Paper", "30s quote / risk cycle"))):
            self.ops.columnconfigure(i, weight=1, uniform="source")
            box = tk.Frame(self.ops, bg=PANEL)
            box.grid(row=0, column=i, sticky="nsew", padx=10, pady=6)
            self.source_boxes.append(box)
            top = label(box, title, size=9, bold=True)
            top.pack(anchor="w")
            state = label(box, "Awaiting local state", size=8, color=MUTED_TEXT)
            state.pack(anchor="w")
            self.source_labels[key] = (top, state, title, cadence)
            Tooltip(box, lambda key=key: self._source_tooltip(key))
            Tooltip(state, lambda key=key: self._source_tooltip(key))
        self.alert = label(self.page, "", size=9, color=WARNING)
        self.alert.pack(fill="x", padx=16, pady=(0, 4))
        viewport = tk.Frame(self.page, bg=PAGE)
        viewport.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(viewport, bg=PAGE, highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.body = tk.Frame(self.canvas, bg=PAGE)
        self._body_window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._resize)
        self.metrics = tk.Frame(self.body, bg=PAGE)
        self.metrics.pack(fill="x", padx=14, pady=(2, 9))
        self.metric_values, self.metric_captions, self.metric_titles, self.metric_cards = {}, {}, {}, []
        for i, (key, title) in enumerate((("equity", "Pool equity"), ("pnl", "Paper P/L since start"),
                      ("exposure", "Gross exposure"), ("fees", "Fees paid"), ("drawdown", "Worst drawdown"))):
            panel = card(self.metrics)
            panel.grid(row=0, column=i, sticky="nsew", padx=(0, 7 if i < 4 else 0))
            self.metrics.columnconfigure(i, weight=1, uniform="metric")
            self.metric_titles[key] = label(panel, title, size=9, color=MUTED_TEXT)
            self.metric_titles[key].pack(anchor="w", padx=12, pady=(8, 0))
            self.metric_values[key] = label(panel, "—", size=19, bold=True)
            self.metric_values[key].pack(anchor="w", padx=12)
            self.metric_captions[key] = label(panel, "", size=8, color=MUTED_TEXT)
            self.metric_captions[key].pack(anchor="w", padx=12, pady=(0, 8))
            self.metric_cards.append(panel)
        Tooltip(self.metric_cards[1], "Change from paper opening marked equity, including costs. Inherited gains/losses are excluded; account P/L is adjusted for internal transfers.")
        Tooltip(self.metric_cards[4], lambda: "Performance export as of: " + str((self.snapshot.performance if self.snapshot else {}).get("as_of_utc", "unavailable")))
        self.workspace = tk.Frame(self.body, bg=PAGE)
        self.workspace.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        self.accounts_rail = card(self.workspace)
        self.center = tk.Frame(self.workspace, bg=PAGE)
        self.right = tk.Frame(self.workspace, bg=PAGE)
        self._build_accounts()
        self._build_chart()
        self._build_table()
        self._build_right()
        self.footer = label(self.page, "Read-only local view · all times PT", size=8, color=MUTED_TEXT)
        self.footer.pack(fill="x", padx=16, pady=(4, 6))
        self._apply_layout(1706)

    def _build_accounts(self):
        label(self.accounts_rail, "Accounts", size=12, bold=True).pack(anchor="w", padx=12, pady=(10, 6))
        self.account_box = ttk.Combobox(self.accounts_rail, textvariable=self.account_filter, values=("All accounts", *ACCOUNT_KEYS),
                                        state="readonly", style="HYPER.TCombobox", width=16)
        self.account_box.pack(fill="x", padx=12, pady=(0, 8))
        self.account_values = {}
        for key, (name, role, initials) in ACCOUNTS.items():
            panel = card(self.accounts_rail)
            panel.pack(fill="x", padx=9, pady=(0, 8))
            head = tk.Frame(panel, bg=PANEL)
            head.pack(fill="x", padx=9, pady=(5, 1))
            mark = tk.Canvas(head, width=35, height=35, bg=PANEL, highlightthickness=0)
            mark.pack(side="left", padx=(0, 9))
            mark.create_oval(1, 1, 34, 34, fill="#175c54", outline="#267b68")
            mark.create_text(18, 18, text=initials, fill=TEXT, font=("Segoe UI", 11, "bold"))
            copy = tk.Frame(head, bg=PANEL)
            copy.pack(side="left", fill="x", expand=True)
            label(copy, name, size=11, bold=True).pack(anchor="w")
            label(copy, role.upper(), size=8, color=MUTED_TEXT).pack(anchor="w")
            equity = label(panel, "—", size=16, bold=True)
            equity.pack(anchor="w", padx=10)
            pnl = label(panel, "P/L since opening  —", size=8)
            pnl.pack(anchor="w", padx=10, pady=(0, 2))
            values = {"equity": equity, "pnl": pnl, "panel": panel}
            for field, title in (("free_cash", "Free cash / collateral"), ("gross_exposure", "Gross exposure")):
                row = tk.Frame(panel, bg=PANEL)
                row.pack(fill="x", padx=10, pady=1)
                label(row, title, size=8, color=MUTED_TEXT).pack(side="left")
                val = label(row, "—", size=8)
                val.pack(side="right")
                values[field] = val
            meter = tk.Canvas(panel, bg=INSET, height=4, highlightthickness=0)
            meter.pack(fill="x", padx=10, pady=(4, 2))
            values["meter"] = meter
            meter.bind("<Configure>", lambda _, account=key: self._draw_exposure(account))
            note = label(panel, "Awaiting local records", size=8, color=MUTED_TEXT)
            note.pack(anchor="w", padx=10, pady=(0, 4))
            values["note"] = note
            Tooltip(panel, "Account P/L excludes net internal transfers. Exposure / equity is a paper risk measure; no exchange liquidation or maintenance-margin simulation.")
            self.account_values[key] = values
        self.exposure_note = label(self.accounts_rail, "", size=9, color=MUTED_TEXT, justify="left", wraplength=270)
        self.exposure_note.pack(fill="x", padx=12, pady=(4, 10))

    def _radios(self, parent, variable, choices):
        for value in choices:
            ttk.Radiobutton(parent, text=value, value=value, variable=variable,
                            style="HYPER.TRadiobutton").pack(side="left", padx=1)

    def _build_chart(self):
        self.chart_card = card(self.center)
        self.chart_card.pack(fill="both", expand=True, pady=(0, 8))
        head = tk.Frame(self.chart_card, bg=PANEL)
        head.pack(fill="x", padx=12, pady=(10, 4))
        self.chart_title = label(head, "Performance since paper start", size=12, bold=True)
        self.chart_title.pack(side="left")
        options = tk.Frame(self.chart_card, bg=PANEL)
        options.pack(fill="x", padx=10, pady=(2, 5))
        self._radios(options, self.chart_metric, ("P/L", "Equity", "Drawdown"))
        ranges = tk.Frame(options, bg=PANEL)
        ranges.pack(side="right")
        self._radios(ranges, self.chart_range, ("1H", "24H", "7D", "All"))
        self.chart = tk.Canvas(self.chart_card, bg=PANEL, highlightthickness=0, height=240, width=100)
        self.chart.pack(fill="both", expand=True, padx=6)
        self.chart.bind("<Configure>", lambda _: self._draw_chart())
        self.chart.bind("<Motion>", self._chart_hover)
        self.chart.bind("<Leave>", lambda _: self.chart.delete("hover"))
        self.chart_note = label(self.chart_card, "", size=8, color=MUTED_TEXT)
        self.chart_note.pack(fill="x", padx=12, pady=(2, 6))
        self.costs = label(self.chart_card, "Fees  —    ·    Funding  — · estimated", size=9, color=MUTED_TEXT)
        self.costs.pack(fill="x", padx=12, pady=(2, 10))

    def _build_table(self):
        self.table_card = card(self.center)
        self.table_card.pack(fill="both", expand=True)
        head = tk.Frame(self.table_card, bg=PANEL)
        head.pack(fill="x", padx=12, pady=(10, 5))
        label(head, "Positions & activity", size=12, bold=True).pack(side="left")
        self.row_count = label(head, "", size=8, color=MUTED_TEXT)
        self.row_count.pack(side="right")
        tabs = tk.Frame(self.table_card, bg=PANEL)
        tabs.pack(fill="x", padx=9, pady=(0, 5))
        self._radios(tabs, self.view, ("Positions", "Decisions", "Fills", "Transfers"))
        self.view.trace_add("write", lambda *_: self._render_table())
        filters = tk.Frame(self.table_card, bg=PANEL)
        filters.pack(fill="x", padx=10, pady=(0, 5))
        ttk.Combobox(filters, textvariable=self.asset_filter, values=("All assets", "BTC", "ETH", "HYPE", "ZEC"),
                     state="readonly", width=10, style="HYPER.TCombobox").pack(side="left", padx=(0, 5))
        ttk.Combobox(filters, textvariable=self.qualification_filter, values=("All models", "Qualified", "Research"),
                     state="readonly", width=12, style="HYPER.TCombobox").pack(side="left", padx=(0, 5))
        ttk.Checkbutton(filters, text="Inherited dust", variable=self.include_passive,
                        style="HYPER.TCheckbutton").pack(side="right")
        table_box = tk.Frame(self.table_card, bg=INSET)
        table_box.pack(fill="both", expand=True, padx=10)
        table_box.columnconfigure(0, weight=1)
        table_box.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table_box, show="headings", selectmode="browse", height=4, style="HYPER.Treeview")
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar = ttk.Scrollbar(table_box, orient="vertical", command=self.tree.yview)
        ybar.grid(row=0, column=1, sticky="ns")
        xbar = ttk.Scrollbar(table_box, orient="horizontal", command=self.tree.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.tag_configure("negative", foreground="#ff898d")
        self.tree.tag_configure("passive", foreground=MUTED_TEXT)
        self.empty_table = label(table_box, "", size=10, color=MUTED_TEXT, justify="center")
        self.table_note = label(self.table_card, "", size=8, color=MUTED_TEXT, wraplength=650, justify="left")
        self.table_note.pack(fill="x", padx=12, pady=(5, 8))

    def _build_right(self):
        self.forecast_card = card(self.right)
        self.forecast_card.pack(fill="x", pady=(0, 8))
        label(self.forecast_card, "Latest forecasts", size=12, bold=True).pack(anchor="w", padx=12, pady=(10, 1))
        self.forecast_caption = label(self.forecast_card, "P(not-down) · 4 × 15m = 1 hour", size=8, color=MUTED_TEXT)
        self.forecast_caption.pack(anchor="w", padx=12, pady=(0, 4))
        self.forecast_canvas = tk.Canvas(self.forecast_card, bg=PANEL, highlightthickness=0, width=100, height=165, cursor="hand2")
        self.forecast_canvas.pack(fill="x", padx=10)
        self.forecast_canvas.bind("<Configure>", lambda _: self._render_forecasts())
        self.forecast_canvas.bind("<Button-1>", self._select_forecast)
        self.forecast_note = label(self.forecast_card, "Qualification measures model evaluation, not profitability.",
                                   size=8, color=MUTED_TEXT, wraplength=380, justify="left")
        self.forecast_note.pack(fill="x", padx=12, pady=(1, 9))
        self.detail_card = card(self.right)
        self.detail_card.pack(fill="both", expand=True, pady=(0, 8))
        self.detail_heading = label(self.detail_card, "Decision detail", size=12, bold=True)
        self.detail_heading.pack(anchor="w", padx=12, pady=(10, 6))
        ttk.Checkbutton(self.detail_card, text="Show saved record", variable=self.show_record,
                        style="HYPER.TCheckbutton").pack(anchor="w", padx=12, pady=(0, 3))
        content = tk.Frame(self.detail_card, bg=PANEL)
        content.pack(fill="both", expand=True, padx=10, pady=(0, 9))
        self.detail = tk.Text(content, bg=PANEL, fg=TEXT, relief="flat", highlightthickness=0,
                              wrap="word", width=20, height=18, font=("Segoe UI", 10),
                              padx=2, pady=3, cursor="arrow", selectbackground="#194849")
        self.detail.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(content, orient="vertical", command=self.detail.yview)
        scrollbar.pack(side="right", fill="y")
        self.detail.configure(yscrollcommand=scrollbar.set, state="disabled")
        self.detail.tag_configure("title", foreground=MINT, font=("Segoe UI", 11, "bold"), spacing3=6)
        self.detail.tag_configure("section", foreground=MUTED_TEXT, font=("Segoe UI", 9, "bold"), spacing1=10, spacing3=4)
        self.detail.tag_configure("body", spacing3=4)
        self.policy_label = label(self.right, "", size=8, color=MUTED_TEXT, wraplength=400, justify="left")
        self.policy_label.pack(fill="x", padx=2)

    def _resize(self, event):
        self.alert.configure(wraplength=max(200, event.width-32), justify="left")
        self.canvas.itemconfigure(self._body_window, width=event.width)
        self._apply_layout(event.width)
        self.body.update_idletasks()
        desired = max(self.body.winfo_reqheight(), event.height)
        self.canvas.itemconfigure(self._body_window, height=desired)
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _apply_layout(self, width):
        mode = "wide" if width >= 1450 else "compact" if width >= 950 else "narrow"
        if mode == self._layout_mode:
            return
        self._layout_mode = mode
        for i in range(5):
            self.metrics.columnconfigure(i, weight=1 if i < (3 if mode == "narrow" else 5) else 0,
                                         uniform="metric" if i < (3 if mode == "narrow" else 5) else "")
        for i, box in enumerate(self.source_boxes):
            box.grid(row=i // 2 if mode == "narrow" else 0, column=i % 2 if mode == "narrow" else i,
                     sticky="nsew", padx=10, pady=6)
            self.ops.columnconfigure(i, weight=1 if i < (2 if mode == "narrow" else 4) else 0,
                                     uniform="source" if i < (2 if mode == "narrow" else 4) else "")
        for panel in (self.accounts_rail, self.center, self.right):
            panel.grid_forget()
        for i in range(3):
            self.workspace.columnconfigure(i, weight=0, minsize=0, uniform="")
            self.workspace.rowconfigure(i, weight=0)
        if mode == "wide":
            for i, weight in enumerate((21, 51, 28)):
                self.workspace.columnconfigure(i, weight=weight, uniform="workspace")
            self.accounts_rail.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
            self.center.grid(row=0, column=1, sticky="nsew", padx=(0, 8))
            self.right.grid(row=0, column=2, sticky="nsew")
            self.workspace.rowconfigure(0, weight=1)
        elif mode == "compact":
            self.workspace.columnconfigure(0, weight=26, uniform="workspace")
            self.workspace.columnconfigure(1, weight=74, uniform="workspace")
            self.accounts_rail.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
            self.center.grid(row=0, column=1, sticky="nsew")
            self.right.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        else:
            self.workspace.columnconfigure(0, weight=1)
            self.accounts_rail.grid(row=1, column=0, sticky="ew", pady=(8, 0))
            self.center.grid(row=0, column=0, sticky="nsew")
            self.right.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        for child in (self.forecast_card, self.detail_card, self.policy_label):
            child.pack_forget()
            child.grid_forget()
        self.right.columnconfigure(0, weight=42, uniform="right")
        self.right.columnconfigure(1, weight=58, uniform="right")
        if mode == "compact":
            self.forecast_card.grid(row=0, column=0, sticky="new", padx=(0, 8))
            self.detail_card.grid(row=0, column=1, rowspan=2, sticky="nsew")
            self.policy_label.grid(row=1, column=0, sticky="new", padx=(0, 8), pady=8)
            self.detail.configure(height=19)
        else:
            self.forecast_card.pack(fill="x", pady=(0, 8))
            self.detail_card.pack(fill="both", expand=True, pady=(0, 8))
            self.policy_label.pack(fill="x", padx=2)
            self.detail.configure(height=12)
        for i, panel in enumerate(self.metric_cards):
            panel.grid_forget()
            panel.grid(row=i // 3 if mode == "narrow" else 0, column=i % 3 if mode == "narrow" else i,
                       sticky="nsew", padx=(0, 7), pady=(0, 5 if mode == "narrow" else 0))
        self.chart.configure(height=190 if mode == "wide" else 220)

    def _wheel(self, event):
        if self._closed or not str(event.widget).startswith(str(self.parent)):
            return
        if isinstance(event.widget, (tk.Text, ttk.Treeview)):
            return
        self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def _schedule(self, name, delay, callback):
        if self._closed:
            return
        def run():
            self._jobs.pop(name, None)
            if not self._closed:
                callback()
        self._jobs[name] = self.parent.after(delay, run)

    def refresh(self):
        if self._closed or self._loading:
            return
        self._loading = True
        self.refresh_button.configure(state="disabled")
        def load():
            try:
                result = (self.service.load_snapshot(), None)
            except Exception as exc:
                result = (None, f"Local read failed: {exc}")
            self._messages.put(result)
        threading.Thread(target=load, name="hyper-view-read", daemon=True).start()

    def _poll(self):
        try:
            snapshot, error = self._messages.get_nowait()
        except queue.Empty:
            pass
        else:
            self._loading = False
            self.refresh_button.configure(state="normal")
            self._error = error or ""
            if snapshot is not None:
                self.show_snapshot(snapshot)
            else:
                self._render()
        self._schedule("poll", 100, self._poll)

    def _auto_refresh(self):
        self.refresh()
        self._schedule("auto", self.refresh_ms, self._auto_refresh)

    def _destroy(self, event):
        if event.widget != self.parent or self._closed:
            return
        self._closed = True
        for job in self._jobs.values():
            self.parent.after_cancel(job)
        self._jobs.clear()
        if self._wheel_binding:
            self.root.unbind("<MouseWheel>", self._wheel_binding)

    def show_snapshot(self, snapshot):
        if self._closed:
            return
        self.snapshot = snapshot
        self._error = ""
        self._render()

    def _choose_mode(self):
        self._inspector_forecast = None
        self._render()

    def _source_group(self, key):
        if not self.snapshot:
            return []
        sources = self.snapshot.sources
        aliases = {"data": ("data", "feature", "candle", "coordinator"), "forecasts": ("forecast", "prediction"),
                   "models": ("model", "training"), "paper": ("paper", "ledger")}[key]
        return [source for name, source in sources.items() if any(word in name.casefold() for word in aliases)]

    def _source_tooltip(self, key):
        lines = [self.source_labels[key][3]]
        if self._error:
            lines.append("The latest read failed. These are observations from the last successful read.")
        for source in self._source_group(key):
            lines.append(f"{source.name}: {source.state}\n{source.observed_at_utc or 'No timestamp'}\n{source.detail or ''}")
        return "\n\n".join(lines)

    def _render(self):
        if self._closed:
            return
        paper = self.mode.get() == "Paper"
        snap = self.snapshot
        pooled = snap.pooled if snap and paper else {}
        self.badge.configure(text="SIMULATED" if paper else "REAL MONEY · PLANNED", fg=MINT if paper else WARNING)
        self.mode_status.configure(text=("Portfolio " + local_time(snap.portfolio_observed_at_utc) if snap and paper else
                                         "Awaiting local records" if paper else "Not connected"), fg=MUTED_TEXT if paper else WARNING)
        for key, (title, state, text, cadence) in self.source_labels.items():
            group = self._source_group(key)
            if key == "paper" and not paper:
                title.configure(text="Execution · Not connected", fg=WARNING)
                state.configure(text="No execution adapter", fg=MUTED_TEXT)
                continue
            rank = {"fresh": 0, "missing": 2, "partial": 3, "stale": 4, "stopped": 5, "error": 6}
            worst = max(group, key=lambda s: rank.get(s.state, 3), default=None)
            status = worst.state.capitalize() if worst else "Unavailable"
            if key == "paper" and worst and worst.state == "fresh" and snap:
                status = str(snap.runtime.get("status", "fresh")).capitalize()
            ages = [s.age_seconds for s in group if s.age_seconds is not None]
            if self._error:
                ages = [(datetime.now(timezone.utc) - timestamp(s.observed_at_utc)).total_seconds()
                        for s in group if timestamp(s.observed_at_utc)]
                status = "Read failed"
            age = age_text(max(ages)) if ages else "age unknown"
            title.configure(text=f"{text} · {status}", fg=MINT if worst and worst.state == "fresh" and not self._error else WARNING)
            state.configure(text=f"{cadence} · {age}")
            if key == "models" and snap and not self._error:
                poll = snap.sources.get("models")
                fits = [source.age_seconds for name, source in snap.sources.items() if name.startswith("training:") and source.age_seconds is not None]
                state.configure(text=f"Poll {age_text(poll.age_seconds if poll else None)} · fit {age_text(max(fits) if fits else None)}")
        warnings = list(snap.warnings if snap else ())
        if snap and not warnings:
            warnings.extend(f"{source.name} · {source.state}: {source.detail or age_text(source.age_seconds)}"
                            for source in snap.sources.values() if source.state != "fresh")
        if self._error:
            warnings.insert(0, self._error + (" — displaying previous snapshot" if snap else ""))
        self.alert.configure(text=(warnings[0] + (f"  (+{len(warnings)-1} in Operations)" if len(warnings) > 1 else "")) if warnings else
                             ("Paper ledger · simulated fills and virtual transfers" if paper else "Not connected · shared forecasts are a preview; execution history is empty"),
                             fg=WARNING if warnings or not paper else MUTED_TEXT)
        self.metric_titles["pnl"].configure(text="Paper P/L since start" if paper else "P/L since activation")
        values = {"equity": money(pooled.get("equity")), "pnl": money(pooled.get("total_pnl"), True),
                  "exposure": money(pooled.get("gross_exposure")), "fees": money(pooled.get("fees")),
                  "drawdown": pct(abs(number(snap.performance.get("max_drawdown_fraction"))))
                  if snap and paper and number(snap.performance.get("max_drawdown_fraction")) is not None else "—"}
        for key, value in values.items():
            self.metric_values[key].configure(text=value)
        pnl, opening, equity, exposure = (number(pooled.get(k)) for k in ("total_pnl", "initial_equity", "equity", "gross_exposure"))
        self.metric_values["pnl"].configure(fg=DANGER if pnl is not None and pnl < 0 else MINT if pnl is not None else TEXT)
        self.metric_captions["equity"].configure(text="Three paper accounts" if paper else "Awaiting reconciled accounts")
        self.metric_captions["pnl"].configure(text=f"{pct(pnl / opening)} vs opening" if pnl is not None and opening else "Opening baseline unavailable")
        self.metric_captions["exposure"].configure(text=f"{pct(exposure / equity)} of equity" if exposure is not None and equity else "No exposure observation")
        self.metric_captions["fees"].configure(text="Cumulative simulated fill fees" if paper else "Awaiting actual exchange fees")
        perf_stamp = snap.performance.get("as_of_utc") if snap and paper else None
        self.metric_captions["drawdown"].configure(text="Export " + local_time(perf_stamp) if perf_stamp else "No performance export")
        for key, widgets in self.account_values.items():
            account = snap.accounts.get(key, {}) if snap and paper else {}
            widgets["equity"].configure(text=money(account.get("equity")))
            apnl = number(account.get("total_pnl"))
            widgets["pnl"].configure(text="P/L since opening  " + money(apnl, True), fg=DANGER if apnl is not None and apnl < 0 else MINT)
            for field in ("free_cash", "gross_exposure"):
                widgets[field].configure(text=money(account.get(field)))
            amount, balance = number(account.get("gross_exposure")), number(account.get("equity"))
            ratio = amount / balance if amount is not None and balance and balance > 0 else None
            self._draw_exposure(key)
            widgets["note"].configure(text=(pct(ratio) + " exposure / equity") if ratio is not None else "Awaiting valuation" if paper else "Awaiting account sync")
            widgets["panel"].configure(highlightbackground=MINT if ACCOUNT_KEYS.get(self.account_filter.get()) == key else LINE)
        self.exposure_note.configure(text="Accounts are isolated.\nPool exposure does not imply shared exchange margin.")
        self.chart_title.configure(text="Performance since paper start" if paper else "Performance since activation")
        self.costs.configure(text=f"Fees  {money(pooled.get('fees'))}    ·    Funding  {money(pooled.get('funding'), True)} · estimated" if paper else "Fees  —    ·    Funding  — · awaiting execution")
        if snap and paper and snap.runtime.get("funding_errors"):
            self.costs.configure(text=self.costs.cget("text") + " · settlements pending", fg=WARNING)
        else:
            self.costs.configure(fg=MUTED_TEXT)
        policy = getattr(snap, "policy", {}) if snap else {}
        config = policy.get("config", policy) if isinstance(policy, dict) else {}
        qualification_policy = ("Only qualified forecasts participate." if config.get("require_qualified_forecasts") is True else
                                "Qualified and research models may participate." if config.get("require_qualified_forecasts") is False else "Forecast eligibility policy unavailable.")
        self.policy_label.configure(text=(f"Paper policy · {pct(config.get('per_symbol_gross_fraction'))} / symbol · {pct(config.get('pool_gross_fraction'))} pool cap\n{qualification_policy}" if paper else
                                         "Execution unavailable. Switching modes does not connect or enable real-money trading."))
        self.footer.configure(text=("H.Y.P.E.R. / " + self.mode.get() + "   ·   " + ("View refreshed " + local_time(snap.observed_at_utc, date=True) if snap else "No local snapshot") + "   ·   Read-only · times PT"))
        self._draw_chart()
        self._render_table()
        self._render_forecasts()

    def _draw_exposure(self, key):
        if key not in self.account_values:
            return
        meter = self.account_values[key]["meter"]
        meter.delete("all")
        account = self.snapshot.accounts.get(key, {}) if self.snapshot and self.mode.get() == "Paper" else {}
        amount, balance = number(account.get("gross_exposure")), number(account.get("equity"))
        if amount is not None and balance and balance > 0:
            ratio = min(1, max(0, amount / balance))
            meter.create_rectangle(0, 0, meter.winfo_width() * ratio, 4, fill=MINT, outline="")

    def _chart_series(self):
        if not self.snapshot or self.mode.get() != "Paper":
            return []
        account = ACCOUNT_KEYS.get(self.account_filter.get(), "pooled")
        rows = [r for r in self.snapshot.equity_history if r.get("account") == account and timestamp(r.get("timestamp_utc"))]
        rows.sort(key=lambda r: timestamp(r["timestamp_utc"]))
        metric = self.chart_metric.get()
        series = []
        for row in rows:
            # The adapter calculates drawdown from transfer-adjusted equity
            # before sampling. Raw account equity would create false losses
            # whenever the account sends an internal transfer.
            value = number(row.get("drawdown_fraction" if metric == "Drawdown" else "total_pnl" if metric == "P/L" else "equity"))
            if value is not None and metric == "Drawdown":
                value *= 100
            if value is not None:
                series.append((timestamp(row["timestamp_utc"]), value, row))
        hours = {"1H": 1, "24H": 24, "7D": 168}.get(self.chart_range.get())
        if hours and series:
            end = timestamp(self.snapshot.portfolio_observed_at_utc) or series[-1][0]
            series = [point for point in series if point[0] >= end - timedelta(hours=hours)]
        if len(series) > 350:
            # Last observation per time bucket: valuation snapshots are never summed.
            start, end = series[0][0].timestamp(), series[-1][0].timestamp()
            buckets = {}
            for point in series:
                buckets[min(349, int((point[0].timestamp() - start) / max(1, end - start) * 350))] = point
            series = list(buckets.values())
        return series

    def _draw_chart(self):
        if not hasattr(self, "chart") or self._closed:
            return
        canvas = self.chart
        canvas.delete("all")
        self._chart_points = []
        width, height = max(100, canvas.winfo_width()), max(100, canvas.winfo_height())
        series = self._chart_series()
        if not series:
            powder = self.mode.get() == "Powder"
            canvas.create_text(width/2, height/2-12, text="Real performance will appear here" if powder else "No equity observations available", fill=TEXT, font=("Segoe UI", 12))
            canvas.create_text(width/2, height/2+16, text="Not connected · awaiting execution and reconciliation" if powder else "The local ledger supplies the chart", fill=MUTED_TEXT, font=("Segoe UI", 9))
            self.chart_note.configure(text="No real-money opening baseline" if powder else "Account filter selects the chart; asset/model filters select activity")
            return
        left, top, right, bottom = 78, 18, width-20, height-32
        values = [point[1] for point in series]
        low, high = min(values), max(values)
        if self.chart_metric.get() in ("P/L", "Drawdown"):
            low, high = min(0, low), max(0, high)
        padding = max((high-low)*.12, .001 if self.chart_metric.get() == "Drawdown" else .05)
        low, high = low-padding, high+padding
        x0, x1 = series[0][0].timestamp(), series[-1][0].timestamp()
        for i in range(5):
            value = low+(high-low)*i/4
            y = bottom-(bottom-top)*i/4
            canvas.create_line(left, y, right, y, fill=LINE)
            tick = f"{value:.2f}%" if self.chart_metric.get() == "Drawdown" else money(value)
            canvas.create_text(left-8, y, text=tick, anchor="e", fill=MUTED_TEXT, font=("Segoe UI", 8))
        if low <= 0 <= high:
            y = bottom-(0-low)/(high-low)*(bottom-top)
            canvas.create_line(left, y, right, y, fill="#849bb3", dash=(4, 4))
        coords = []
        for stamp, value, row in series:
            x = left+(stamp.timestamp()-x0)/max(1, x1-x0)*(right-left)
            y = bottom-(value-low)/(high-low)*(bottom-top)
            self._chart_points.append((x, y, stamp, value))
            coords.extend((x, y))
        if len(coords) >= 4:
            canvas.create_line(*coords, fill=MINT, width=2)
        x, y = coords[-2:]
        canvas.create_oval(x-3, y-3, x+3, y+3, fill=TEXT, outline=MINT)
        for index in sorted({0, len(series)//2, len(series)-1}):
            point = self._chart_points[index]
            canvas.create_text(point[0], bottom+18, text=local_time(point[2].isoformat()).replace(":00 PT", " PT"), fill=MUTED_TEXT, font=("Segoe UI", 8),
                               anchor="w" if index == 0 else "e" if index == len(series)-1 else "center")
        sampled = " · sampled history" if self.snapshot.history_sampled else ""
        self.chart_note.configure(text=f"{self.account_filter.get()} · {len(series)} observations{sampled} · hover for exact time/value")

    def _chart_hover(self, event):
        self.chart.delete("hover")
        if not self._chart_points:
            return
        x, y, stamp, value = min(self._chart_points, key=lambda p: abs(p[0]-event.x))
        text = (f"{value:.4f}%" if self.chart_metric.get() == "Drawdown" else money(value)) + "  ·  " + local_time(stamp.isoformat()) + "\n" + stamp.isoformat()
        self.chart.create_line(x, 12, x, self.chart.winfo_height()-30, fill=MUTED_TEXT, dash=(2, 3), tags="hover")
        anchor_x = min(max(8, event.x-150), max(8, self.chart.winfo_width()-325))
        self.chart.create_rectangle(anchor_x, 6, anchor_x+320, 47, fill=INSET, outline=LINE, tags="hover")
        self.chart.create_text(anchor_x+8, 12, text=text, fill=TEXT, anchor="nw", font=("Segoe UI", 8), tags="hover")

    def _filters(self):
        return dict(account=ACCOUNT_KEYS.get(self.account_filter.get(), "all"), asset=self.asset_filter.get() if self.asset_filter.get() != "All assets" else "all",
                    qualification=self.qualification_filter.get().lower() if self.qualification_filter.get() != "All models" else "all", include_passive=self.include_passive.get())

    def _render_table(self):
        if not hasattr(self, "tree") or self._closed:
            return
        view = self.view.get()
        same_view = self._table_view == (self.mode.get(), view)
        if not same_view:
            self._inspector_forecast = None
        previous = self.tree.selection()
        if self._table_view:
            self._selections[self._table_view] = previous
        selected = previous if same_view else self._selections.get((self.mode.get(), view), ())
        scroll = self.tree.yview()
        columns = {
            "Positions": (("account", "Account", 93), ("market", "Market", 90), ("side", "Side", 55), ("quantity", "Quantity", 94), ("avg_entry", "Avg entry", 88), ("mark_price", "Mark", 88), ("notional", "Gross", 90), ("unrealized_pnl", "Unrealized P/L", 105)),
            "Decisions": (("time", "Time PT", 83), ("account", "Account", 90), ("market", "Market", 85), ("action", "Action", 60), ("current_notional", "Current", 83), ("target_notional", "Target", 83), ("p_not_down", "P(not-down)", 94), ("qualification", "Model", 87), ("reason", "Reason", 190)),
            "Fills": (("time", "Time PT", 83), ("account", "Account", 90), ("market", "Market", 85), ("side", "Side", 52), ("quantity", "Executed qty", 99), ("price", "Fill price", 88), ("notional", "Notional", 88), ("fee", "Fee", 65), ("qualification", "Model", 87)),
            "Transfers": (("time", "Time PT", 90), ("from_account", "From", 95), ("to_account", "To", 95), ("amount", "USDC", 100), ("reason", "Reason", 210), ("status", "Status", 95)),
        }[view]
        if not same_view:
            self.tree.delete(*self.tree.get_children())
            self.tree.configure(columns=[c[0] for c in columns])
            for key, title, width in columns:
                self.tree.heading(key, text=title)
                self.tree.column(key, width=width, minwidth=width, stretch=key == "reason", anchor="w")
        self._table_view = (self.mode.get(), view)
        source = getattr(self.snapshot, view.lower(), []) if self.snapshot and self.mode.get() == "Paper" else []
        rows = filter_rows(source, **self._filters())
        self._rows_by_id = {}
        for index, row in enumerate(rows):
            row = {**row.get("details", {}), **row}
            iid = str(row.get({"Positions": "position_id", "Decisions": "decision_id", "Fills": "fill_id", "Transfers": "transfer_id"}[view]) or
                      (f"{row.get('account')}:{row.get('kind')}:{row.get('coin')}" if view == "Positions" else f"{view}:{index}"))
            self._rows_by_id[iid] = row
            values = [self._cell(key, row, view) for key, _, _ in columns]
            tags = ("passive",) if row.get("passive") else ("negative",) if view == "Positions" and (number(row.get("unrealized_pnl")) or 0) < 0 else ()
            if self.tree.exists(iid):
                if self.tree.item(iid, "values") != tuple(str(value) for value in values):
                    self.tree.item(iid, values=values, tags=tags)
                self.tree.move(iid, "", index)
            else:
                self.tree.insert("", index, iid=iid, values=values, tags=tags)
        for iid in self.tree.get_children():
            if iid not in self._rows_by_id:
                self.tree.delete(iid)
        selected = [iid for iid in selected if iid in self._rows_by_id]
        if selected and tuple(selected) != self.tree.selection():
            self.tree.selection_set(selected)
        elif not selected and rows:
            self.tree.selection_set(next(iter(self._rows_by_id)))
        if same_view and scroll:
            self.tree.yview_moveto(scroll[0])
        self.row_count.configure(text=f"{len(rows)} records")
        self.empty_table.place_forget()
        if not rows:
            self.empty_table.configure(text="Not connected\nNo exchange activity yet" if self.mode.get() == "Powder" else "No records match these filters" if self.snapshot else "Awaiting local ledger")
            self.empty_table.place(relx=.5, rely=.5, anchor="center")
        self.table_note.configure(text=("Inherited positions retain their entry cost basis; unrealized P/L can predate paper opening." if view == "Positions" else
                                       "Committed virtual cash movements · zero fee · pool net flow is zero." if view == "Transfers" else
                                       f"Latest {getattr(self.service, 'journal_limit', 500)} records per journal · select a row for reasoning and provenance.") if self.mode.get() == "Paper" else
                                      "Confirmed execution records will appear after an execution adapter is connected.")
        preview = next((r for r in (self.snapshot.forecasts if self.snapshot else [])
                        if r.get("coin") == self._inspector_forecast), None)
        if preview:
            self._render_inspector(preview, "Forecast preview")
        else:
            self._selected()

    def _cell(self, key, row, view):
        value = row.get(key)
        if key == "time":
            return local_time(row.get("timestamp_utc")).replace(" PT", "")
        if key in ("account", "from_account", "to_account"):
            return ACCOUNTS.get(value, (value or "Pool",))[0]
        if key == "market":
            return f"{row.get('coin', '—')} {row.get('kind', '')}".strip()
        if key == "side":
            return "Inherited" if row.get("passive") else ("Buy" if (number(row.get("quantity")) or 0) > 0 else "Sell") if view == "Fills" else str(value or "—").title()
        if key == "quantity":
            return quantity(value)
        if key in ("avg_entry", "mark_price", "notional", "unrealized_pnl", "current_notional", "target_notional", "price", "fee", "amount"):
            return money(value)
        if key == "p_not_down":
            return pct(value)
        if key == "qualification":
            return str(value or "unavailable").capitalize()
        if key == "reason":
            return REASONS.get(value, value or "—")
        if key == "status" and view == "Transfers":
            return "Committed"
        return str(value or "—").capitalize()

    def _render_forecasts(self):
        if not hasattr(self, "forecast_canvas") or self._closed:
            return
        canvas = self.forecast_canvas
        canvas.delete("all")
        width = max(200, canvas.winfo_width())
        paper = self.mode.get() == "Paper"
        self.forecast_caption.configure(text="P(not-down) · 4 × 15m = 1 hour" if paper else "Shared model forecast preview · 1h horizon")
        rows = self.snapshot.forecasts if self.snapshot else []
        args = self._filters()
        args["account"] = "all"
        rows = filter_rows(rows, **args)
        self._forecast_rows = rows
        canvas.configure(height=max(145, 25 + 34*len(rows)))
        if not rows:
            canvas.create_text(10, 48, text="No matching local forecasts", fill=MUTED_TEXT, anchor="w", font=("Segoe UI", 9))
            return
        bar_left, bar_right = width*.37, width*.68
        canvas.create_text(10, 10, text="Asset", fill=MUTED_TEXT, anchor="w", font=("Segoe UI", 8))
        canvas.create_text(width*.19, 10, text="P(not-down)", fill=MUTED_TEXT, anchor="w", font=("Segoe UI", 8))
        canvas.create_text(width-5, 10, text="Model", fill=MUTED_TEXT, anchor="e", font=("Segoe UI", 8))
        for i, row in enumerate(rows):
            y = 37+i*34
            p = number(row.get("p_not_down"))
            canvas.create_line(2, y+17, width-2, y+17, fill=LINE)
            canvas.create_text(10, y, text=row.get("coin", row.get("symbol", "—")), fill=TEXT, anchor="w", font=("Segoe UI", 10, "bold"))
            canvas.create_text(width*.20, y, text=pct(p), fill=TEXT, anchor="w", font=("Segoe UI", 9))
            canvas.create_rectangle(bar_left, y-4, bar_right, y+4, fill="#183346", outline="")
            middle = (bar_left+bar_right)/2
            if p is not None and 0 <= p <= 1:
                end = bar_left+p*(bar_right-bar_left)
                canvas.create_rectangle(min(middle, end), y-4, max(middle, end), y+4, fill=MINT if p >= .5 else "#df777e", outline="")
            canvas.create_line(middle, y-9, middle, y+9, fill=MUTED_TEXT, dash=(2, 2))
            qualification = str(row.get("qualification", "unavailable")).capitalize()
            canvas.create_text(width-5, y, text=qualification, fill=MUTED_TEXT, anchor="e", font=("Segoe UI", 9))

    def _select_forecast(self, event):
        index = int((event.y-20)//34)
        if 0 <= index < len(getattr(self, "_forecast_rows", [])):
            self._inspector_forecast = self._forecast_rows[index].get("coin")
            self._render_inspector(self._forecast_rows[index], "Forecast preview")

    def _selected(self, _event=None):
        self._inspector_forecast = None
        selected = self.tree.selection()
        row = self._rows_by_id.get(selected[0]) if selected else None
        self._render_inspector(row, self.view.get())

    def _render_inspector(self, row, view):
        detail_key = (self.mode.get(), view, str((row or {}).get("decision_id") or (row or {}).get("fill_id") or
                      (row or {}).get("transfer_id") or ((row or {}).get("account"), (row or {}).get("kind"), (row or {}).get("coin"))))
        previous_scroll = self.detail.yview()[0] if detail_key == self._detail_key else 0
        self._detail_key = detail_key
        sections = []
        def add(text, tag="body"):
            sections.append((str(text) + "\n", tag))
        if self.mode.get() == "Powder" and view != "Forecast preview":
            self.detail_heading.configure(text="Execution detail")
            add("Not connected", "title")
            add("Real balances, orders, fills and transfers require an execution and reconciliation adapter.")
            add("Order acknowledgement · requested / filled quantity · actual fees · exchange order ID · reconciliation", "section")
            add("No exchange execution records are available.")
        elif not row:
            self.detail_heading.configure(text="Decision detail")
            add("Select an activity record", "title")
            add("Review the signal, allocation, execution and provenance here. Forecast rows open a shared model preview.")
        else:
            self.detail_heading.configure(text=view if view == "Forecast preview" else "Decision detail")
            data = {**row.get("details", {}), **row}
            # Attach only provenance belonging to this recorded model/forecast.
            # Never substitute a newer signal for a historical decision.
            for forecast in self.snapshot.forecasts if self.snapshot else []:
                if data.get("model_id") and forecast.get("model_id") == data["model_id"]:
                    for field in ("model_published_at_utc", "training_cutoff_utc", "training_label_cutoff_utc", "calibration_cutoff_utc"):
                        data.setdefault(field, forecast.get(field))
                if data.get("forecast_id") and forecast.get("forecast_id") == data["forecast_id"]:
                    for field in ("target_close_utc", "horizon_bars", "horizon_minutes", "per_model"):
                        data.setdefault(field, forecast.get(field))
            policy = data.get("policy", {}) or {}
            account = ACCOUNTS.get(data.get("account"), (data.get("account", "Shared model"),))[0]
            add(f"{account} · {data.get('coin', data.get('symbol', 'USDC'))}", "title")
            reason = data.get("reason")
            add((str(data.get("action", "")).capitalize()+" · " if data.get("action") else "") + REASONS.get(reason, reason or view.rstrip("s")))
            if view == "Transfers":
                add("VIRTUAL TRANSFER", "section")
                add(f"{ACCOUNTS.get(data.get('from_account'), (data.get('from_account'),))[0]} → {ACCOUNTS.get(data.get('to_account'), (data.get('to_account'),))[0]}\n{money(data.get('amount'))} USDC · Committed\nZero fee · no simulated settlement delay")
                for field in ("donor_cash", "donor_reserve", "receiver_requirement"):
                    if field in data:
                        add(field.replace("_", " ").capitalize() + "  " + money(data[field]))
            elif view == "Positions":
                add("POSITION", "section")
                add(f"{data.get('kind', '—').title()} · {data.get('side', '—').title()} · {quantity(data.get('quantity'))}\nEntry {money(data.get('avg_entry'))}  →  Mark {money(data.get('mark_price'))}\nGross {money(data.get('notional'))}\nUnrealized P/L {money(data.get('unrealized_pnl'), True)}")
                if data.get("passive"):
                    add("Inherited · unmanaged", "title")
                add("Unrealized P/L uses the retained entry and may include gains or losses from before paper opening. Account headline P/L uses the paper opening baseline.")
            else:
                add("SIGNAL", "section")
                p = number(data.get("p_not_down"))
                bars = data.get('horizon_bars', 4)
                minutes = data.get('horizon_minutes', number(bars) * 15 if number(bars) is not None else None)
                p_down = data.get("p_down") if "p_down" in data else 1-p if p is not None else None
                horizon = f"{bars} × 15m bars · {quantity(minutes)} minute horizon" if bars is not None and minutes is not None else "Forecast horizon unavailable"
                add(f"P(not-down) {pct(p)}    P(down) {pct(p_down)}\n{horizon}")
                if data.get("source_state") and data["source_state"] != "fresh":
                    add("Forecast source: " + data["source_state"])
                for model, probability in (data.get("per_model") or {}).items():
                    score = probability.get("p_not_down") if isinstance(probability, dict) else probability
                    add(f"{model}  {pct(score)}")
                for field, title in (("forecast_created_at_utc", "Forecast available"), ("created_at_utc", "Available"), ("target_close_utc", "Outcome"), ("outcome_timestamp_utc", "Outcome"), ("outcome_at_utc", "Outcome")):
                    if data.get(field):
                        add(title + "  " + local_time(data[field], date=True))
                if policy or any(field in data for field in ("current_notional", "target_notional")):
                    add("ALLOCATION", "section")
                    add(f"Current {money(data.get('current_notional'))} → Target {money(data.get('target_notional'))}")
                    caps = policy.get("binding_caps", [])
                    if caps:
                        add("Limited by: " + ", ".join(str(c).replace("_", " ") for c in caps))
                    add(f"Confidence {pct(policy.get('confidence'))} · Horizon volatility {pct(policy.get('effective_horizon_sigma'))}")
                    if policy.get("account_cash_constraints_applied"):
                        add("Policy-stage account cash constraint applied")
                execution = {**data, **(data.get("execution") or {})}
                if view == "Fills" or "requested_quantity" in execution:
                    add("SIMULATED EXECUTION", "section")
                    add(f"Requested {quantity(execution.get('requested_quantity'))}\nExecuted {quantity(execution.get('quantity'))} · {str(execution.get('status', 'recorded')).capitalize()}\nUnfilled remainder {quantity(execution.get('unfilled_quantity'))}")
                    add(f"Fill price {money(execution.get('price'))}\nRaw book VWAP {money(execution.get('raw_book_vwap'))}\nAdded slippage {quantity(execution.get('extra_slippage_bps'))} bps · Fee {money(execution.get('fee'))}")
                    if execution.get("book_time_utc"):
                        add("Book " + local_time(execution["book_time_utc"], date=True))
                add("PROVENANCE", "section")
                add(str(data.get("qualification", "unavailable")).capitalize() + " model · evaluation label, not profitability")
                for field, title in (("model_created_at_utc", "Model published"), ("model_published_at_utc", "Model published"), ("training_cutoff_utc", "Training cutoff"), ("training_label_cutoff_utc", "Training label cutoff"), ("calibration_cutoff_utc", "Calibration cutoff"), ("train_end_utc", "Training cutoff"), ("calibration_end_utc", "Calibration cutoff"), ("policy_id", "Policy"), ("forecast_id", "Forecast"), ("model_id", "Model"), ("data_run_id", "Source run")):
                    if data.get(field):
                        add(f"{title}  {data[field]}")
            if data.get("timestamp_utc"):
                add("OBSERVATION", "section")
                add(local_time(data["timestamp_utc"], date=True) + "\n" + data["timestamp_utc"])
            if self.show_record.get():
                add("SAVED RECORD", "section")
                add(json.dumps(data, indent=2, ensure_ascii=False, default=str))
        if sections == self._detail_content:
            return
        self._detail_content = sections
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        for text, tag in sections:
            self.detail.insert("end", text, tag)
        self.detail.configure(state="disabled")
        self.detail.yview_moveto(previous_scroll)

    def _show_operations(self):
        window = tk.Toplevel(self.root)
        window.title("H.Y.P.E.R. · operations and source evidence")
        window.geometry("820x680")
        window.configure(bg=PAGE)
        label(window, "Source ages and measured work", size=16, bold=True).pack(anchor="w", padx=16, pady=(12, 3))
        label(window, "Cadences are schedules. Work, queue waits and publication are measured separately.", size=10, color=MUTED_TEXT).pack(anchor="w", padx=16, pady=(0, 10))
        body = tk.Frame(window, bg=PAGE)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        text = tk.Text(body, bg=INSET, fg=TEXT, wrap="word", relief="flat", font=("Consolas", 10), padx=12, pady=10)
        text.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(body, orient="vertical", command=text.yview)
        bar.pack(side="right", fill="y")
        text.configure(yscrollcommand=bar.set)
        lines = ["Read-only local view; no runtime lifecycle or trading actions.", ""]
        if self.snapshot:
            lines.extend(self.snapshot.warnings)
            for source in self.snapshot.sources.values():
                lines.extend((f"\n{source.name} · {source.state.upper()} · {age_text(source.age_seconds)}",
                              f"Observed UTC: {source.observed_at_utc or 'unavailable'}",
                              f"Cadence: {source.cadence_seconds if source.cadence_seconds is not None else 'unspecified'} seconds", str(source.detail or "")))
            lines.extend(("\nMEASURED DURATIONS", "Missing measurements appear as —; independent stages are not summed.",
                          "Stage / asset         Work       Queue   Poll wait     Publish"))
            def seconds(value):
                value = number(value)
                return f"{value:.3f}s" if value is not None else "—"
            for timing in self.snapshot.timings:
                stage = f"{str(timing.get('kind', '')).capitalize()} / {timing.get('coin', '—')}"
                lines.append(f"{stage:<18}" + "".join(f"{seconds(timing.get(key)):>12}" for key in
                             ("work_seconds", "queue_wait_seconds", "poll_wait_seconds", "publication_seconds")))
                lines.append("  Observed UTC: " + str(timing.get("at_utc") or "unavailable"))
                for key, title in (("fetch_seconds", "Fetch / normalize"), ("feature_seconds", "Feature build"),
                                   ("parquet_write_seconds", "Parquet / catalog"), ("fit_seconds", "Fit"),
                                   ("calibration_seconds", "Calibration"), ("assessment_seconds", "Assessment"),
                                   ("publication_and_overhead_seconds", "Publication + unallocated overhead")):
                    if timing.get(key) is not None:
                        lines.append(f"  {title}: {seconds(timing[key])}")
            lines.extend(("\nRUNTIME EVIDENCE", json.dumps(self.snapshot.runtime, indent=2, ensure_ascii=False, default=str)))
        else:
            lines.append("No snapshot loaded.")
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")
