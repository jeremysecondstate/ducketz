"""Native display of Scout's verified saved Gameplan forecast history."""
from __future__ import annotations

from datetime import datetime
import tkinter as tk
from tkinter import ttk
from zoneinfo import ZoneInfo

from app.ui.gameplan_widgets import label, panel
from app.ui.theme import MUTED_TEXT, SUCCESS, SURFACE, SURFACE_ALT, TEXT, WARNING
from ml.gameplan_probability_target import LEGACY_COST_TARGET, RAW_DIRECTION_TARGET

PACIFIC = ZoneInfo("America/Los_Angeles")
HORIZON_LABELS = {"All horizons": "all", "1 hour": "1h", "4 hours": "4h", "1 day": "1d", "Weekly": "1w"}


def _time(value):
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M:%S %Z")
    except (TypeError, ValueError, OverflowError):
        pass
    return "Not recorded"


def _percent(value):
    return "—" if value is None else f"{value:.1%}"


def _brier(value):
    return "—" if value is None else f"{value:.3f}"


def _target(contract):
    if contract == RAW_DIRECTION_TARGET:
        return "Positive class: raw price return strictly above 0%."
    if contract == LEGACY_COST_TARGET:
        return "Positive class: raw price return strictly above the forecast's saved round-trip cost threshold."
    return "Positive-class definition is not recorded."


def _result(row):
    if row.status == "PENDING_MATURITY":
        return "Pending maturity"
    if row.status == "MATURE_AWAITING_DATA":
        return "Missing outcome"
    return "Evaluated"


class ScoutHorizonStatsPanel:
    """Render one saved peer review with an independent horizon filter."""

    def __init__(self, parent, root):
        self.root = root
        self.frame = panel(parent)
        self.snapshot = None
        self.horizon = tk.StringVar(master=root, value="All horizons")
        self._wrapped = []
        self._forecasts = {}
        self._build()
        self.set_snapshot(None)

    def _text(self, parent, text="", **kwargs):
        widget = label(parent, text, justify="left", wraplength=700, **kwargs)
        self._wrapped.append(widget)
        return widget

    def _build(self):
        self.heading = label(self.frame, "Scout Gameplan forecast Stats", size=19, bold=True)
        self.heading.pack(anchor="w", padx=14, pady=(14, 5))
        self.source_label = self._text(self.frame, size=9, color=MUTED_TEXT)
        self.source_label.pack(fill="x", padx=14, pady=(0, 5))
        self.revision_label = self._text(self.frame, size=9, color=MUTED_TEXT)
        self.revision_label.pack(fill="x", padx=14, pady=(0, 5))
        self.contract_label = self._text(self.frame, size=9, color=MUTED_TEXT)
        self.contract_label.pack(fill="x", padx=14, pady=(0, 8))
        self.state_label = self._text(self.frame, size=10, color=WARNING)
        self.state_label.pack(fill="x", padx=14, pady=(0, 8))

        controls = tk.Frame(self.frame, background=SURFACE)
        controls.pack(fill="x", padx=14, pady=(0, 10))
        label(controls, "Scout forecast horizon", size=10, bold=True).pack(side="left", padx=(0, 10))
        self.horizon_box = ttk.Combobox(controls, state="readonly", values=tuple(HORIZON_LABELS),
            textvariable=self.horizon, width=16)
        self.horizon_box.pack(side="left")
        self.horizon_box.bind("<<ComboboxSelected>>", self.render)

        scores = self.scores = tk.Frame(self.frame, background=SURFACE)
        scores.pack(fill="x", padx=14, pady=(0, 10))
        for column in range(4):
            scores.columnconfigure(column, weight=1)
        self.accuracy_label = label(scores, size=23, bold=True, color=SUCCESS)
        self.brier_label = label(scores, size=23, bold=True)
        self.bullish_label = label(scores, size=19, bold=True)
        self.bearish_label = label(scores, size=19, bold=True)
        for column, widget in enumerate((self.accuracy_label, self.brier_label, self.bullish_label, self.bearish_label)):
            widget.grid(row=0, column=column, sticky="w", padx=(0, 12))
        self.score_titles = []
        for column, title in enumerate(("Direction accuracy", "Brier score", "Bullish accuracy", "Bearish accuracy")):
            widget = label(scores, title, size=9, bold=True)
            widget.grid(row=1, column=column, sticky="w")
            self.score_titles.append(widget)
        self.accuracy_caption = label(scores, size=9, color=MUTED_TEXT)
        self.accuracy_caption.grid(row=2, column=0, sticky="w", pady=(3, 0))
        self.brier_caption = label(scores, "Lower is better", size=9, color=MUTED_TEXT)
        self.brier_caption.grid(row=2, column=1, sticky="w", pady=(3, 0))
        self.coverage_label = self._text(self.frame, size=9, color=MUTED_TEXT)
        self.coverage_label.pack(fill="x", padx=14, pady=(0, 10))
        self.symbol_tree = self._tree((
            ("symbol", "Company", 80), ("total", "Forecasts", 70), ("scored", "Scored", 65),
            ("correct", "Correct", 65), ("accuracy", "Accuracy", 80), ("brier", "Brier", 70),
            ("pending", "Pending", 70), ("missing", "Missing outcome", 120)), height=4)
        self._text(self.frame, "Saved forecasts", size=12, bold=True).pack(fill="x", padx=14, pady=(12, 5))
        self.forecast_tree = self._tree((
            ("symbol", "Company", 80), ("horizon", "Horizon", 65), ("probability", "Saved probability", 120),
            ("direction", "Saved direction", 115), ("start", "Target start · Pacific", 190),
            ("end", "Target end · Pacific", 190), ("outcome", "Outcome", 135), ("score", "Direction score", 110)), height=5)
        self.forecast_tree.bind("<<TreeviewSelect>>", self._select_forecast)
        self.detail_label = self._text(self.frame, size=9, color=MUTED_TEXT)
        self.detail_label.pack(fill="x", padx=14, pady=(8, 12))
        self.profit_label = self._text(self.frame,
            "Forecast scores do not measure trading profit. Realized P&L: Not available.",
            size=9, color=MUTED_TEXT)
        self.profit_label.pack(fill="x", padx=14, pady=(0, 12))
        self.frame.bind("<Configure>", self._resize)

    def _tree(self, columns, *, height):
        outer = tk.Frame(self.frame, background=SURFACE)
        outer.pack(fill="x", padx=12)
        outer.columnconfigure(0, weight=1)
        style = ttk.Style(self.root)
        style.configure("ScoutHorizon.Treeview", background="#0c1929", fieldbackground="#0c1929",
            foreground=TEXT, rowheight=27, font=("Segoe UI", 9), borderwidth=0)
        style.configure("ScoutHorizon.Treeview.Heading", background=SURFACE_ALT, foreground=MUTED_TEXT,
            font=("Segoe UI", 9))
        tree = ttk.Treeview(outer, columns=tuple(item[0] for item in columns), show="headings",
            height=height, selectmode="browse", style="ScoutHorizon.Treeview")
        for name, title, width in columns:
            tree.heading(name, text=title)
            tree.column(name, width=width, minwidth=width, stretch=True, anchor="w")
        tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(outer, orient="vertical", command=tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(outer, orient="horizontal", command=tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        return tree

    @property
    def selected_horizon(self):
        return HORIZON_LABELS.get(self.horizon.get(), "all")

    def set_snapshot(self, snapshot):
        self.snapshot = snapshot
        self.render()

    def render(self, _event=None):
        snapshot = self.snapshot
        review = snapshot.review if snapshot else None
        for tree in (self.symbol_tree, self.forecast_tree):
            tree.delete(*tree.get_children())
        self._forecasts = {}
        self.detail_label.configure(text="Select a saved forecast to inspect its target and observed outcome.")
        for widget in (self.accuracy_label, self.brier_label, self.bullish_label, self.bearish_label):
            widget.configure(text="—")
        self.accuracy_caption.configure(text="No scored forecast population")
        self.coverage_label.configure(text="Saved prediction coverage: unavailable")
        self.source_label.configure(text=(f"Source: Scout saved Gameplan history · Session {snapshot.session}\n"
            f"Exported {_time(snapshot.exported_at)} · Completion {snapshot.completion_id or 'Not recorded'}"
            if snapshot else "Scout saved Gameplan history is unavailable for this session."))
        self.revision_label.configure(text=(f"Saved revisions: {snapshot.revision_count} · Selected package SHA-256: "
            f"{snapshot.package_sha256 or 'Not recorded'}" if snapshot else ""))
        self.contract_label.configure(text="Probability target contract: Not recorded")
        self.state_label.configure(text=(snapshot.message if snapshot else "No saved Scout forecast source loaded."))
        if review is None:
            return
        self.source_label.configure(text=self.source_label.cget("text") +
            f"\nReviewed {_time(review.reviewed_at)} · Outcomes through {_time(review.outcomes_through)}")
        self.contract_label.configure(text=f"Saved probability target: {review.probability_target_contract}\n"
            f"{_target(review.probability_target_contract)} Original probabilities and saved direction labels are preserved.")
        coverage = snapshot.coverage_status or review.coverage_status
        if coverage == "NO_SAVED_INDEPENDENT_GAMEPLAN":
            self.state_label.configure(text="No saved Gameplan forecasts. " + (snapshot.message or ""))
            self.coverage_label.configure(text="No saved forecasts are available to score for this session.")
            return
        if not review.outcomes:
            self.state_label.configure(text="No promoted Gameplan forecasts are available for scoring. " + (snapshot.message or ""))
            self.coverage_label.configure(text=f"Excluded non-promoted research forecasts: {review.excluded_forecasts}. "
                "No performance score is available for this population.")
            return
        self.state_label.configure(text=snapshot.message)
        metrics = review.metrics(self.selected_horizon)
        self.accuracy_label.configure(text=_percent(metrics.accuracy))
        self.brier_label.configure(text=_brier(metrics.brier))
        self.bullish_label.configure(text=_percent(metrics.bullish_accuracy))
        self.bearish_label.configure(text=_percent(metrics.bearish_accuracy))
        self.accuracy_caption.configure(text=f"{metrics.correct} / {metrics.scored} scored")
        self.coverage_label.configure(text=f"{metrics.total} promoted forecasts · {metrics.evaluated} evaluated · "
            f"{metrics.pending} pending maturity · {metrics.awaiting_data} missing outcomes\n"
            f"Excluded non-promoted research forecasts: {review.excluded_forecasts}. "
            f"Direction scores exclude {metrics.neutral} neutral forecasts. Brier uses saved evaluated probability errors. "
            "Pending and missing outcomes do not enter performance.")
        rows = review.rows(self.selected_horizon)
        for index, symbol in enumerate(sorted({row.symbol for row in rows})):
            summary = review.metrics(self.selected_horizon, symbol)
            self.symbol_tree.insert("", "end", iid=f"symbol-{index}", values=(symbol, summary.total,
                summary.scored, summary.correct, _percent(summary.accuracy), _brier(summary.brier),
                summary.pending, summary.awaiting_data))
        for index, row in enumerate(rows):
            identity = f"forecast-{index}"
            self._forecasts[identity] = row
            self.forecast_tree.insert("", "end", iid=identity, values=(row.symbol, row.horizon,
                f"{row.probability:.4%}", row.direction, _time(row.start), _time(row.end), _result(row),
                "Correct" if row.correct is True else "Incorrect" if row.correct is False else "Not scored"))

    def _select_forecast(self, _event=None):
        selection = self.forecast_tree.selection()
        row = self._forecasts.get(selection[0]) if selection else None
        if row is None:
            return
        review = self.snapshot.review
        contract = row.probability_target_contract
        self.detail_label.configure(text="\n".join((
            f"{row.symbol} · {row.horizon} · {row.route} · Saved role {row.role} · {_result(row)}",
            f"Saved probability: {row.probability!r} ({row.probability:.4%}) · Saved direction: {row.direction}",
            f"Original target: {contract}. {_target(contract)}",
            f"Saved cost threshold: {row.target_cost:.4%} · Positive-class return threshold: {row.probability_target_threshold:.4%}",
            f"Target window: {_time(row.start)} → {_time(row.end)}",
            f"Actual start observed: {_time(row.observed_start)} · Actual end observed: {_time(row.observed_end)}",
            f"Saved observed return: {'Not recorded' if row.actual_return is None else format(row.actual_return, '.4%')} · "
            f"Saved Brier error: {'Not recorded' if row.brier is None else repr(row.brier)}",
            f"Outcome cutoff: {_time(review.outcomes_through)} · Reviewed: {_time(review.reviewed_at)}",
            f"Saved direction policy: {row.direction_policy_version or 'Unrecorded'} · Forecast ID: {row.forecast_id}",
        )))

    def _resize(self, event):
        for widget in self._wrapped:
            widget.configure(wraplength=max(240, event.width - 45))
        narrow = event.width < 750
        for index, widget in enumerate((self.accuracy_label, self.brier_label, self.bullish_label, self.bearish_label)):
            row, column = (3 * (index // 2), index % 2) if narrow else (0, index)
            widget.grid_configure(row=row, column=column, pady=(8 if row else 0, 0))
            self.score_titles[index].grid_configure(row=row + 1, column=column)
        for column in range(4):
            self.scores.columnconfigure(column, weight=1 if not narrow or column < 2 else 0)
