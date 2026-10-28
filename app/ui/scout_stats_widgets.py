"""Native read-only display of independently saved 15-minute results."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
import tkinter as tk
from tkinter import ttk
from zoneinfo import ZoneInfo

from app.ui.gameplan_widgets import label, panel, Tooltip
from app.ui.scout_stats_data import execution_summary, prediction_summary
from app.ui.theme import MUTED_TEXT, SUCCESS, SURFACE, SURFACE_ALT, TEXT, WARNING

PACIFIC = ZoneInfo("America/Los_Angeles")


def _quantity(value):
    try:
        number = float(value)
        return f"{number:g}" if math.isfinite(number) else "—"
    except (TypeError, ValueError):
        return "—"


def _time(value, *, short=False):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.astimezone(PACIFIC).strftime("%H:%M" if short else "%b %d, %H:%M:%S %Z")
    except (TypeError, ValueError, OverflowError):
        pass
    return "—" if short else "Not recorded"


def _percent(value):
    return "—" if value is None else f"{value:.1%}"


def _price(value):
    try:
        number = float(value)
        return f"${number:,.4f}" if math.isfinite(number) else "Not recorded"
    except (TypeError, ValueError):
        return "Not recorded"


class ScoutStatsPanel:
    """Show independent prediction and order populations for one saved session."""

    def __init__(self, parent, root, *, actor="Scout"):
        self.actor = actor
        self.root = root
        self.frame = panel(parent)
        self.snapshot = None
        self._orders = {}
        self._wrapped = []
        self._build()
        self.set_snapshot(None)

    def _text(self, parent, text="", **kwargs):
        widget = label(parent, text, justify="left", wraplength=450, **kwargs)
        self._wrapped.append(widget)
        return widget

    def _build(self):
        label(self.frame, f"{self.actor} 15-Minute Trader", size=19, bold=True).pack(anchor="w", padx=14, pady=(14, 3))
        self._text(self.frame, "Live saved evidence · Separate from Gameplan horizons", color=MUTED_TEXT).pack(fill="x", padx=14)
        self.session_label = self._text(self.frame, "", size=10, bold=True)
        self.session_label.pack(fill="x", padx=14, pady=(6, 3))
        self.source_label = self._text(self.frame, "", size=9, color=MUTED_TEXT)
        self.source_label.pack(fill="x", padx=14, pady=(0, 3))
        self.state_label = self._text(self.frame, "", size=9, color=MUTED_TEXT)
        self.state_label.pack(fill="x", padx=14, pady=(0, 10))
        self.columns = tk.Frame(self.frame, background=SURFACE)
        self.columns.pack(fill="x", padx=12, pady=(0, 10))
        self.prediction_panel = panel(self.columns)
        self.order_panel = panel(self.columns)
        self.prediction_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.order_panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        self.columns.columnconfigure(0, weight=1, uniform="scout_stats")
        self.columns.columnconfigure(1, weight=1, uniform="scout_stats")

        label(self.prediction_panel, "Prediction accuracy", size=15, bold=True).pack(anchor="w", padx=12, pady=(10, 4))
        self.prediction_state = self._text(self.prediction_panel, "", size=9, color=MUTED_TEXT)
        self.prediction_state.pack(fill="x", padx=12, pady=(0, 8))
        values = tk.Frame(self.prediction_panel, background=SURFACE)
        values.pack(fill="x", padx=12)
        values.columnconfigure(0, weight=1)
        values.columnconfigure(1, weight=1)
        self.accuracy_label = label(values, "—", size=23, bold=True, color=SUCCESS)
        self.accuracy_label.grid(row=0, column=0, sticky="w")
        self.brier_label = label(values, "—", size=23, bold=True)
        self.brier_label.grid(row=0, column=1, sticky="w")
        label(values, "Direction accuracy", size=9, bold=True).grid(row=1, column=0, sticky="w")
        label(values, "Brier score", size=9, bold=True).grid(row=1, column=1, sticky="w")
        self.accuracy_caption = label(values, "", size=9, color=MUTED_TEXT)
        self.accuracy_caption.grid(row=2, column=0, sticky="w", pady=(3, 0))
        label(values, "Lower is better", size=9, color=MUTED_TEXT).grid(row=2, column=1, sticky="w", pady=(3, 0))
        Tooltip(values, "Accuracy compares P(not down) at 50% with the saved target direction. "
                "Both scores use forecasts issued before target open with observed, scorable outcomes. "
                "The third target candle's close is compared with the reference candle's open; equality counts as not down.")
        self.coverage_label = self._text(self.prediction_panel, "", size=10)
        self.coverage_label.pack(fill="x", padx=12, pady=(10, 4))
        self.exclusions_label = self._text(self.prediction_panel, "", size=9, color=MUTED_TEXT)
        self.exclusions_label.pack(fill="x", padx=12, pady=(0, 8))
        self.company_tree = self._tree(self.prediction_panel, (
            ("symbol", "Company", 68), ("eligible", "Before open", 82), ("scored", "Scored", 60),
            ("correct", "Correct", 60), ("accuracy", "Accuracy", 75),
            ("brier", "Brier", 65), ("missing", "Missing / unscorable", 140)), height=4)
        self._text(self.prediction_panel, "Binary accuracy uses P(not down) >= 50%; equality of the observed close and reference open counts as not down. "
                   "Brier is mean squared probability error (lower is better). BUY/SELL/HOLD execution decisions are separate. "
                   "Missing, unscorable, synthetic and pending outcomes do not enter either score.",
                   size=9, color=MUTED_TEXT).pack(fill="x", padx=12, pady=(7, 10))

        label(self.order_panel, "Orders and fills", size=15, bold=True).pack(anchor="w", padx=12, pady=(10, 4))
        self.execution_state = self._text(self.order_panel, "", size=9, color=MUTED_TEXT)
        self.execution_state.pack(fill="x", padx=12, pady=(0, 8))
        self.order_total = self._text(self.order_panel, "", size=12, bold=True)
        self.order_total.pack(fill="x", padx=12)
        self.order_counts = self._text(self.order_panel, "", size=10)
        self.order_counts.pack(fill="x", padx=12, pady=(5, 4))
        self.fill_coverage = self._text(self.order_panel, "", size=9, color=MUTED_TEXT)
        self.fill_coverage.pack(fill="x", padx=12, pady=(0, 8))
        self.order_tree = self._tree(self.order_panel, (
            ("time", "Saved time", 70), ("symbol", "Company", 68), ("side", "Side", 45),
            ("fill", "Filled / requested", 115), ("status", "Status", 95)), height=4)
        self.order_tree.bind("<<TreeviewSelect>>", self._select_order)
        self.detail_label = self._text(self.order_panel, "Select an order to inspect saved fill evidence.", size=9, color=MUTED_TEXT)
        self.detail_label.pack(fill="x", padx=12, pady=(7, 4))
        self.profit_label = self._text(self.order_panel, "Realized P&L: Not available · Purchase-cost matching and fees are not recorded in this summary.",
                                       size=9, color=MUTED_TEXT)
        self.profit_label.pack(fill="x", padx=12, pady=(0, 10))
        Tooltip(self.order_panel, "Each saved request appears once. Filled quantity is cumulative; repeated status observations are not added. "
                "An unknown submission or missing fill record stays unknown. Order and fill counts do not measure profit.")
        self.warning_label = self._text(self.frame, "", size=9, color=WARNING)
        self.warning_label.pack(fill="x", padx=14, pady=(0, 10))
        self.frame.bind("<Configure>", self._resize)

    def _tree(self, parent, columns, *, height):
        table = tk.Frame(parent, background=SURFACE)
        table.pack(fill="x", padx=10)
        table.columnconfigure(0, weight=1)
        style = ttk.Style(self.root)
        style.configure("ScoutStats.Treeview", background="#0c1929", fieldbackground="#0c1929",
                        foreground=TEXT, font=("Segoe UI", 9), rowheight=27, borderwidth=0)
        style.configure("ScoutStats.Treeview.Heading", background=SURFACE_ALT, foreground=MUTED_TEXT,
                        font=("Segoe UI", 9), relief="flat")
        tree = ttk.Treeview(table, columns=tuple(item[0] for item in columns), show="headings", height=height,
                            selectmode="browse", style="ScoutStats.Treeview")
        for name, title, width in columns:
            tree.heading(name, text=title)
            tree.column(name, width=width, minwidth=width, anchor="w", stretch=True)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        return tree

    def _resize(self, event):
        narrow = event.width < 900
        self.columns.columnconfigure(1, weight=0 if narrow else 1, uniform="" if narrow else "scout_stats")
        self.columns.columnconfigure(0, weight=1, uniform="" if narrow else "scout_stats")
        self.prediction_panel.grid_configure(padx=(0, 0 if narrow else 6))
        self.order_panel.grid(row=1 if narrow else 0, column=0 if narrow else 1,
                              sticky="nsew", padx=(0 if narrow else 6, 0), pady=(10 if narrow else 0, 0))
        for widget in self._wrapped:
            outer = widget.master is self.frame
            widget.configure(wraplength=max(240, event.width - 45 if outer or narrow else event.width // 2 - 55))

    def set_snapshot(self, snapshot):
        self.snapshot = snapshot
        self.render()

    @staticmethod
    def _available(status):
        return status in {"read", "empty", "available", "partial"}

    def render(self):
        snapshot = self.snapshot
        for tree in (self.company_tree, self.order_tree):
            tree.delete(*tree.get_children())
        self._orders = {}
        self.detail_label.configure(text="Select an order to inspect saved fill evidence.")
        self.session_label.configure(text=f"Session {snapshot.session} · All times Pacific" if snapshot and snapshot.session else "No saved session selected")
        exported_at = getattr(snapshot, "exported_at", None)
        if exported_at:
            self.source_label.configure(text=f"Source: Scout saved export · Exported {_time(exported_at)}\n"
                f"Export: {getattr(snapshot, 'completion_id', '')}")
        else:
            self.source_label.configure(text=f"Source: Local {self.actor} saved evidence" if snapshot else "")
        self.state_label.configure(text=snapshot.message if snapshot else f"Saved {self.actor} 15-Minute evidence has not been loaded.")
        self.warning_label.configure(text="\n".join(snapshot.warnings) if snapshot else "")
        prediction_available = snapshot is not None and self._available(snapshot.prediction_status)
        execution_available = snapshot is not None and self._available(snapshot.execution_status)
        forecasts = snapshot.forecasts if snapshot else ()
        orders = snapshot.orders if snapshot else ()
        metrics = prediction_summary(forecasts)
        self.prediction_state.configure(text=(("Issued before target open · Observed outcomes only" if forecasts
            else f"No live {self.actor} forecasts saved for this session.") if prediction_available
            else "Prediction data missing or unreadable. Accuracy is unavailable."))
        if snapshot and snapshot.prediction_status == "partial":
            self.prediction_state.configure(text="Incomplete prediction evidence · Counts cover readable records only.")
        self.accuracy_label.configure(text=_percent(metrics["accuracy"]) if prediction_available else "—")
        self.brier_label.configure(text=(f"{metrics['brier']:.3f}" if metrics["brier"] is not None else "—") if prediction_available else "—")
        self.accuracy_caption.configure(text=f"{metrics['correct']} / {metrics['scored']} scored" if prediction_available else "No verified score population")
        self.coverage_label.configure(text=(
            f"{metrics['issued']} live forecasts · {metrics['eligible']} before target open\n"
            f"{metrics['scored']} scored · {metrics['pending']} pending · {metrics['missing']} missing\n"
            f"{metrics['unscorable']} unscorable · {metrics['synthetic']} synthetic"
            if prediction_available else "Outcome coverage: unavailable"))
        self.exclusions_label.configure(text=(
            f"Excluded timing: {metrics['during_target']} during target · {metrics['after_target']} after close · {metrics['timing_unknown']} unknown\n"
            f"Invalid probability or outcome: {metrics['invalid_probability']} · Unsupported target: {metrics.get('unsupported_target', 0)}"
            if prediction_available else "Missing data is not counted as zero accuracy."))
        grouped = defaultdict(list)
        for row in forecasts:
            grouped[row.get("symbol") or "Unknown"].append(row)
        for index, (symbol, rows) in enumerate(sorted(grouped.items())):
            summary = prediction_summary(rows)
            self.company_tree.insert("", "end", iid=f"company-{index}", values=(symbol, summary["eligible"], summary["scored"],
                                    summary["correct"], _percent(summary["accuracy"]),
                                    f"{summary['brier']:.3f}" if summary["brier"] is not None else "—",
                                    f"{summary['missing']} / {summary['unscorable']}"))
        execution = execution_summary(orders)
        self.execution_state.configure(text=(("Latest saved order evidence · All session requests" if orders
            else f"No saved {self.actor} order requests for this session.") if execution_available
            else "Order/fill data missing or unreadable. Counts are unavailable."))
        if snapshot and snapshot.execution_status == "partial":
            self.execution_state.configure(text="Incomplete order evidence · Counts cover readable records only.")
        self.order_total.configure(text=f"{execution['attempts']} saved requests · {execution['submitted']} recorded submitted" if execution_available else "— saved requests")
        self.order_counts.configure(text=(
            f"{execution['filled']} filled · {execution['partial']} partial · {execution['working']} working\n"
            f"{execution['cancelled']} cancelled · {execution['rejected']} rejected\n"
            f"{execution['unknown']} unknown · {execution['not_sent']} not sent"
            if execution_available else "Filled / partial / working / cancelled / rejected / unknown: —"))
        self.fill_coverage.configure(text=(
            f"{execution['fill_records']} identified fills · Known filled quantity: {_quantity(execution['filled_quantity'])}\n"
            f"Missing fill evidence: {execution['missing_fill_evidence']} requests"
            if execution_available else "Fill coverage: unavailable"))
        for index, order in enumerate(orders):
            item = f"order-{index}"
            self._orders[item] = order
            self.order_tree.insert("", "end", iid=item, values=(_time(order.get("submitted_at") or order.get("issued_at"), short=True), order.get("symbol", "—"),
                order.get("side", "—"), f"{_quantity(order.get('filled_quantity'))} / {_quantity(order.get('quantity'))}", order.get("status", "UNKNOWN")))

    def _select_order(self, _event=None):
        selection = self.order_tree.selection()
        order = self._orders.get(selection[0]) if selection else None
        if order is None:
            return
        lines = [f"{order.get('symbol', 'Unknown')} {order.get('side', '—')} · {order.get('status', 'UNKNOWN')}",
                 f"Reference time: {_time(order.get('reference_at'))}",
                 f"Submitted time: {_time(order.get('submitted_at'))}",
                 f"Last saved observation: {_time(order.get('observed_at'))}"]
        if order.get("note"):
            lines.append(str(order["note"]))
        fills = order.get("fills") or ()
        if fills:
            lines.append("Saved fills:")
            for fill in fills:
                lines.append(f"{_quantity(fill.get('quantity'))} @ {_price(fill.get('price'))} · {_time(fill.get('executed_at'))}")
        else:
            lines.append("No identified fill records saved; unknown filled quantity remains —.")
        self.detail_label.configure(text="\n".join(lines))


class AtlasStatsPanel(ScoutStatsPanel):
    """The same saved-evidence presentation, labelled for Atlas local history."""

    def __init__(self, parent, root):
        super().__init__(parent, root, actor="Atlas")
