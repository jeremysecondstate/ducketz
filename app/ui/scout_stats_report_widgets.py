"""Display verified Scout report aggregates without inventing source rows."""
from __future__ import annotations

import tkinter as tk

from app.ui.gameplan_widgets import label, panel
from app.ui.scout_stats_widgets import ScoutStatsPanel, _percent, _quantity, _time
from app.ui.theme import MUTED_TEXT, SUCCESS, SURFACE, WARNING


class ScoutStatsReportPanel(ScoutStatsPanel):
    """Use Scout's original reported totals and keep individual records absent."""

    def _build(self):
        label(self.frame, "Scout 15-Minute Trader", size=19, bold=True).pack(anchor="w", padx=14, pady=(14, 3))
        self._text(self.frame, "Saved Scout report · Independent forecasts and execution", color=MUTED_TEXT).pack(fill="x", padx=14)
        self.source_label = self._text(self.frame, "", size=9, color=MUTED_TEXT)
        self.source_label.pack(fill="x", padx=14, pady=(5, 3))
        self.state_label = self._text(self.frame, "", size=9, color=MUTED_TEXT)
        self.state_label.pack(fill="x", padx=14, pady=(0, 9))
        self.columns = tk.Frame(self.frame, background=SURFACE)
        self.columns.pack(fill="x", padx=10, pady=(0, 10))
        for column in (0, 1):
            self.columns.columnconfigure(column, weight=1, uniform="scout_stats")
        self.prediction_panel, self.order_panel = panel(self.columns), panel(self.columns)
        self.prediction_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        self.order_panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        label(self.prediction_panel, "Prediction accuracy", size=15, bold=True).pack(anchor="w", padx=12, pady=(10, 4))
        values = tk.Frame(self.prediction_panel, background=SURFACE)
        values.pack(fill="x", padx=12)
        values.columnconfigure(0, weight=1)
        values.columnconfigure(1, weight=1)
        self.accuracy_label = label(values, "—", size=23, bold=True, color=SUCCESS)
        self.accuracy_label.grid(row=0, column=0, sticky="w")
        self.brier_label = label(values, "—", size=23, bold=True)
        self.brier_label.grid(row=0, column=1, sticky="w")
        label(values, "Reported accuracy", size=9, bold=True).grid(row=1, column=0, sticky="w")
        label(values, "Brier score", size=9, bold=True).grid(row=1, column=1, sticky="w")
        self.accuracy_caption = label(values, "", size=9, color=MUTED_TEXT)
        self.accuracy_caption.grid(row=2, column=0, sticky="w", pady=(3, 0))
        label(values, "Lower is better", size=9, color=MUTED_TEXT).grid(row=2, column=1, sticky="w")
        self.coverage_label = self._text(self.prediction_panel, "", size=10)
        self.coverage_label.pack(fill="x", padx=12, pady=(9, 4))
        self.exclusions_label = self._text(self.prediction_panel, "", size=9, color=MUTED_TEXT)
        self.exclusions_label.pack(fill="x", padx=12, pady=(0, 8))
        self.company_tree = self._tree(self.prediction_panel, (
            ("symbol", "Company", 70), ("forecasts", "Forecasts", 70), ("scored", "Scored", 60),
            ("correct", "Correct", 60), ("accuracy", "Accuracy", 75), ("brier", "Brier", 65),
            ("missing", "Missing", 65), ("synthetic", "Synthetic", 70)), height=4)
        self._text(self.prediction_panel,
            "Original Scout scoring · Missing and synthetic outcomes remain excluded. Forecast accuracy does not measure trading profit.",
            size=9, color=MUTED_TEXT).pack(fill="x", padx=12, pady=(7, 10))
        label(self.order_panel, "Orders and fills", size=15, bold=True).pack(anchor="w", padx=12, pady=(10, 4))
        self.order_total = self._text(self.order_panel, "", size=12, bold=True)
        self.order_total.pack(fill="x", padx=12)
        self.order_counts = self._text(self.order_panel, "", size=10)
        self.order_counts.pack(fill="x", padx=12, pady=(5, 4))
        self.fill_coverage = self._text(self.order_panel, "", size=9, color=MUTED_TEXT)
        self.fill_coverage.pack(fill="x", padx=12, pady=(0, 8))
        self.order_tree = self._tree(self.order_panel, (
            ("symbol", "Company", 70), ("filled", "Filled orders", 95),
            ("submitted", "Known submitted qty", 140), ("quantity", "Known filled qty", 120),
            ("unknown", "Unknown submissions", 145)), height=4)
        self.detail_label = self._text(self.order_panel,
            "Company totals from Scout's saved report. Individual order and fill records are not included.",
            size=9, color=MUTED_TEXT)
        self.detail_label.pack(fill="x", padx=12, pady=(7, 4))
        self.profit_label = self._text(self.order_panel,
            "Realized P&L: Unknown · Gross buy/sell amounts and share quantities are activity, not profit.",
            size=9, color=MUTED_TEXT)
        self.profit_label.pack(fill="x", padx=12, pady=(0, 10))
        self.warning_label = self._text(self.frame, "", size=9, color=WARNING)
        self.warning_label.pack(fill="x", padx=14, pady=(0, 10))
        self.frame.bind("<Configure>", self._resize)

    def render(self):
        snapshot = self.snapshot
        for tree in (self.company_tree, self.order_tree):
            tree.delete(*tree.get_children())
        available = snapshot is not None and snapshot.status == "available"
        self.state_label.configure(text=snapshot.message if snapshot else "Scout saved report has not been loaded.")
        self.source_label.configure(text=(
            f"Session {snapshot.session} · Report as of {_time(snapshot.generated_at)}\n"
            f"Source: Scout saved report · {snapshot.completion_id}" if snapshot and snapshot.completion_id else ""))
        self.warning_label.configure(text=("\n".join(snapshot.limitations) if available else
            "The report is unavailable; missing evidence is not zero performance." if snapshot else ""))
        if not available:
            self.accuracy_label.configure(text="—")
            self.brier_label.configure(text="—")
            self.accuracy_caption.configure(text="No verified score population")
            self.coverage_label.configure(text="Outcome coverage: unavailable")
            self.exclusions_label.configure(text="")
            self.order_total.configure(text="— saved requests")
            self.order_counts.configure(text="Order statuses: unavailable")
            self.fill_coverage.configure(text="Fill quantities: unavailable")
            return
        totals = snapshot.aggregate
        self.accuracy_label.configure(text=_percent(totals["accuracy"]))
        self.brier_label.configure(text=f"{totals['brier']:.3f}" if totals["brier"] is not None else "—")
        self.accuracy_caption.configure(text=f"{totals['correct']} / {totals['scored']} scored")
        self.coverage_label.configure(text=(
            f"{totals['forecasts']} live forecasts · {_percent(totals['scored_coverage'])} scored coverage\n"
            f"{totals['scored']} observed scored · {totals['missing_outcomes']} missing · {totals['synthetic_outcomes']} synthetic"))
        self.exclusions_label.configure(text=(
            f"Other unscored: {totals['other_unscored_outcomes']} · Late forecasts: {totals['late_forecasts']}\n"
            "Missing and synthetic outcomes do not enter the score."))
        statuses = totals["execution_status_counts"]
        requests = totals["journal_request_states"]
        self.order_total.configure(text=f"{sum(requests.values())} saved order requests · {requests.get('ACCEPTED', 0)} accepted")
        no_send = statuses.get("not_submitted", 0) + statuses.get("not_submitted_shadow", 0)
        self.order_counts.configure(text=(
            f"{statuses.get('FILLED', 0)} filled · {statuses.get('PARTIAL', 0)} partial · {statuses.get('WORKING', 0)} working\n"
            f"{statuses.get('CANCELLED', 0)} cancelled · {statuses.get('REJECTED', 0)} rejected\n"
            f"{statuses.get('UNKNOWN', 0)} unknown · {no_send} not submitted"))
        self.fill_coverage.configure(text=(
            f"Known submitted quantity: {_quantity(totals['submitted_quantity'])} · Known filled quantity: {_quantity(totals['filled_quantity'])}\n"
            f"{totals['unique_fill_events']} identified fill events · {totals['submission_unknown']} submissions retain unknown quantities"))
        for index, row in enumerate(snapshot.symbols):
            self.company_tree.insert("", "end", iid=f"report-company-{index}", values=(row["symbol"], row["forecasts"],
                row["scored"], row["correct"], _percent(row["accuracy"]),
                f"{row['brier']:.3f}" if row["brier"] is not None else "—", row["missing_outcomes"], row["synthetic_outcomes"]))
            self.order_tree.insert("", "end", iid=f"report-summary-{index}", values=(row["symbol"],
                row["execution_status_counts"].get("FILLED", 0), _quantity(row["submitted_quantity"]),
                _quantity(row["filled_quantity"]), row["submission_unknown"]))
