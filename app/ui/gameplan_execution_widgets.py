"""Read-only native presentation of a saved, private Gameplan execution report."""
from __future__ import annotations

from datetime import datetime
import math
import tkinter as tk
from tkinter import ttk
from zoneinfo import ZoneInfo

from app.ui.gameplan_widgets import label, panel, Tooltip
from app.ui.theme import BORDER, DANGER, MUTED_TEXT, SUCCESS, SURFACE, SURFACE_ALT, TEXT, WARNING

PACIFIC = ZoneInfo("America/Los_Angeles")
CYAN = "#32d4ec"
STATUS_LABELS = {
    "fully_filled": "Fully filled", "partially_filled": "Partially filled",
    "working": "Working", "cancelled": "Cancelled", "rejected": "Rejected", "unknown": "Unknown",
}
STATUS_COLORS = {
    "fully_filled": SUCCESS, "partially_filled": CYAN, "working": "#75bfff",
    "cancelled": "#ffda5b", "rejected": DANGER, "unknown": MUTED_TEXT,
}
SCOPE_LABELS = {"Submitted attempts": "submitted", "Carryover orders": "carryover",
                "Not submitted": "not_submitted", "All saved records": "all"}


def recorded_timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return value.astimezone(PACIFIC)


def recorded_time(value, *, short=False):
    """Never substitute the UI's clock for an observation timestamp."""
    stamp = recorded_timestamp(value)
    return stamp.strftime("%H:%M" if short else "%b %d, %H:%M:%S %Z") if stamp is not None else "—" if short else "Not recorded"


def quantity(value):
    if value is None:
        return "—"
    try:
        number = float(value)
        return f"{number:g}" if math.isfinite(number) else "—"
    except (TypeError, ValueError):
        return "—"


def fill_price(value):
    if value is None:
        return "Not recorded"
    try:
        number = float(value)
        return f"${number:,.4f}".rstrip("0").rstrip(".") if math.isfinite(number) else "Not recorded"
    except (TypeError, ValueError):
        return "Not recorded"


class GameplanExecutionPanel:
    """Filters immutable report dictionaries; never opens a ledger or broker."""

    def __init__(self, parent, root):
        self.frame = panel(parent)
        self.root = root
        self.snapshot = None
        self._orders = ()
        self._known_symbols = ()
        self._visible_orders = {}
        self.selected_order = None
        self.company = tk.StringVar(master=root, value="All companies")
        self.status_filter = tk.StringVar(master=root, value="All statuses")
        self.scope_filter = tk.StringVar(master=root, value="Submitted attempts")
        self._build()
        self.set_snapshot(None)

    def _build(self):
        label(self.frame, "Trade execution", size=19, bold=True).pack(anchor="w", padx=14, pady=(12, 2))
        label(self.frame, "Gameplan trader · Saved broker observations", size=10, color=MUTED_TEXT).pack(anchor="w", padx=14)
        self.state_label = label(self.frame, "", size=9, color=MUTED_TEXT, justify="left", wraplength=480)
        self.state_label.pack(fill="x", padx=14, pady=(7, 8))
        summary = panel(self.frame)
        summary.pack(fill="x", padx=12, pady=(0, 10))
        self.scope_label = label(summary, "Whole session · All companies", size=10, bold=True)
        self.scope_label.pack(anchor="w", padx=10, pady=(9, 4))
        label(summary, "Recorded status distribution", size=9, color=MUTED_TEXT).pack(anchor="w", padx=10)
        self.distribution = tk.Canvas(summary, height=18, background=SURFACE_ALT, highlightthickness=0)
        self.distribution.pack(fill="x", padx=10, pady=(6, 4))
        self.distribution.bind("<Configure>", lambda _event: self._draw_distribution())
        self.total_label = label(summary, "— submitted order attempts", size=12, bold=True)
        self.total_label.pack(anchor="w", padx=10, pady=(2, 6))
        self.count_labels = {}
        legend = tk.Frame(summary, background=SURFACE)
        legend.pack(fill="x", padx=10, pady=(0, 8))
        for index, (status, title) in enumerate(STATUS_LABELS.items()):
            legend.columnconfigure(index % 3, weight=1, uniform="execution_status")
            item = label(legend, f"—  {title}", size=9, color=STATUS_COLORS[status])
            item.grid(row=index // 3, column=index % 3, sticky="w", pady=3)
            self.count_labels[status] = item
        Tooltip(summary, "Each submitted attempt has one lifecycle category. Cancelled orders can retain partial fills. "
                "Retries count as separate attempts. These counts do not measure profitability.")

        filters = tk.Frame(self.frame, background=SURFACE)
        filters.pack(fill="x", padx=12, pady=(0, 6))
        for column in range(3):
            filters.columnconfigure(column, weight=1, uniform="execution_filters")
        for column, (title, variable, values) in enumerate((
                ("Company", self.company, ("All companies",)),
                ("Order status", self.status_filter, ("All statuses", *STATUS_LABELS.values(), "Not submitted")),
                ("Record scope", self.scope_filter, tuple(SCOPE_LABELS)))):
            label(filters, title, size=9, color=MUTED_TEXT).grid(row=0, column=column, sticky="w", padx=(0, 6))
            box = ttk.Combobox(filters, textvariable=variable, state="readonly", values=values, width=12)
            box.grid(row=1, column=column, sticky="ew", padx=(0, 6 if column < 2 else 0), pady=(3, 0))
            box.bind("<<ComboboxSelected>>", lambda _event: self.render())
            if column == 0:
                self.company_box = box
        self.table_caption = label(self.frame, "", size=9, color=MUTED_TEXT, justify="left")
        self.table_caption.pack(anchor="w", padx=14, pady=(2, 5))

        table = tk.Frame(self.frame, background=SURFACE)
        table.pack(fill="x", padx=12)
        table.columnconfigure(0, weight=1)
        style = ttk.Style(self.root)
        style.configure("GameplanExecution.Treeview", background="#0c1929", fieldbackground="#0c1929",
                        foreground=TEXT, font=("Segoe UI", 10), rowheight=31, borderwidth=0)
        style.configure("GameplanExecution.Treeview.Heading", background=SURFACE_ALT, foreground=MUTED_TEXT,
                        font=("Segoe UI", 9), relief="flat")
        style.map("GameplanExecution.Treeview", background=[("selected", "#1c3e57")], foreground=[("selected", TEXT)])
        self.tree = ttk.Treeview(table, columns=("time", "symbol", "side", "fill", "status"), show="headings",
                                 height=6, selectmode="browse", style="GameplanExecution.Treeview")
        for name, title, width in (("time", "Time", 50), ("symbol", "Company", 78), ("side", "Side", 48),
                                   ("fill", "Filled / requested", 116), ("status", "Status", 112)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=width, stretch=True, anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        for status, color in STATUS_COLORS.items():
            self.tree.tag_configure(status, foreground=color)
        self.tree.tag_configure("not_submitted", foreground=MUTED_TEXT)
        self.tree.bind("<<TreeviewSelect>>", self._select_order)

        detail = panel(self.frame)
        detail.pack(fill="x", padx=12, pady=(10, 8))
        self.detail_title = label(detail, "Order details", size=12, bold=True)
        self.detail_title.pack(anchor="w", padx=10, pady=(8, 5))
        self.detail_text = tk.Text(detail, height=8, wrap="word", background=SURFACE, foreground=TEXT,
                                   font=("Segoe UI", 9), relief="flat", padx=10, pady=5,
                                   insertbackground=TEXT, state="disabled", takefocus=True)
        scroll = ttk.Scrollbar(detail, orient="vertical", command=self.detail_text.yview)
        scroll.pack(side="right", fill="y", pady=(0, 6))
        self.detail_text.configure(yscrollcommand=scroll.set)
        self.detail_text.pack(fill="both", expand=True, pady=(0, 6))
        self.observation_label = label(self.frame, "Last broker observation: not recorded", size=9,
                                       color=MUTED_TEXT, justify="left", wraplength=480)
        self.coverage_label = label(self.frame, "", size=9, color=MUTED_TEXT, justify="left", wraplength=480)
        # Keep the saved provenance available without repeating footer prose.
        # The space beneath Order details is used by current market prices.
        self.evidence_tooltip = Tooltip(self.detail_title, lambda:
            self.observation_label.cget("text") + "\n\n" + self.coverage_label.cget("text"))
        Tooltip(self.state_label, lambda:
            self.observation_label.cget("text") + "\n\n" + self.coverage_label.cget("text"))
        self.frame.bind("<Configure>", self._resize)

    def _resize(self, event):
        width = max(250, event.width - 30)
        for widget in (self.state_label, self.observation_label, self.coverage_label, self.table_caption):
            widget.configure(wraplength=width)

    def set_symbols(self, symbols):
        self._known_symbols = tuple(symbols)
        self._update_symbols()

    def _update_symbols(self):
        symbols = sorted(set(self._known_symbols) | {row.get("symbol", "") for row in self._orders} - {""})
        self.company_box.configure(values=("All companies", *symbols))
        if self.company.get() != "All companies" and self.company.get() not in symbols:
            self.company.set("All companies")

    def set_company(self, symbol):
        if symbol:
            self.company.set(symbol)
            self.render()

    def set_snapshot(self, snapshot):
        self.snapshot = snapshot
        self._orders = tuple(snapshot.orders) if snapshot is not None else ()
        self.selected_order = None
        self._update_symbols()
        self.render()

    def _company_orders(self):
        company = self.company.get()
        return tuple(row for row in self._orders if company == "All companies" or row.get("symbol") == company)

    def _counts(self):
        if self.snapshot is None or self.snapshot.counts is None:
            return None
        if self.company.get() == "All companies":
            return self.snapshot.counts
        rows = self._company_orders()
        submitted = [row for row in rows if row.get("session_scope") == "submitted"]
        result = {status: sum(row.get("status") == status for row in submitted) for status in STATUS_LABELS}
        result.update(submitted_attempts=len(submitted),
                      not_submitted=sum(row.get("session_scope") == "not_submitted" for row in rows),
                      carryover_attempts=sum(row.get("session_scope") == "carryover" for row in rows),
                      unknown_submission=sum(row.get("submission_state") in {"attempted_unknown", "unknown"} for row in rows))
        return result

    def render(self):
        snapshot = self.snapshot
        state = snapshot.state if snapshot is not None else "missing"
        self.state_label.configure(text=(snapshot.message if snapshot is not None else
                                   "No saved execution summary loaded. Prediction results remain available."),
                                   foreground=MUTED_TEXT if state == "available" else WARNING)
        company = self.company.get()
        self.scope_label.configure(text="Whole session · All companies" if company == "All companies" else
                                   f"Company scope · {company} · selected session")
        counts = self._counts()
        self.total_label.configure(text=(f"{counts['submitted_attempts']} recorded submitted order attempts"
                                        if counts is not None else "— submitted order attempts · unavailable"))
        for key, widget in self.count_labels.items():
            widget.configure(text=f"{counts.get(key, 0) if counts is not None else '—'}  {STATUS_LABELS[key]}")
        self._draw_distribution()
        rows = self._company_orders()
        scope = SCOPE_LABELS.get(self.scope_filter.get(), "submitted")
        status = next((key for key, value in STATUS_LABELS.items() if value == self.status_filter.get()), None)
        if self.status_filter.get() == "Not submitted":
            status = "not_submitted"
        rows = [row for row in rows if (scope == "all" or row.get("session_scope") == scope)
                and (status is None or row.get("status") == status)]
        self.table_caption.configure(text=f"{len(rows)} saved rows · {self.scope_filter.get()} · {self.status_filter.get()}"
                                     if counts is not None else "Order records unavailable")
        previous = self.selected_order
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self._visible_orders = {}
        for index, row in enumerate(rows):
            iid = str(index)
            self._visible_orders[iid] = row
            row_status = row.get("status", "unknown")
            title = STATUS_LABELS.get(row_status, "Not submitted" if row_status == "not_submitted" else "Unknown")
            self.tree.insert("", "end", iid=iid, values=(recorded_time(row.get("submitted_at"), short=True),
                             row.get("symbol", "—"), str(row.get("side", "—")).title(),
                             f"{quantity(row.get('filled_quantity'))} / {quantity(row.get('requested_quantity'))}", title),
                             tags=(row_status,))
        chosen = next((iid for iid, row in self._visible_orders.items() if row.get("order_ref") == previous), None)
        if chosen is not None:
            self.tree.selection_set(chosen)
            self._select_order()
        else:
            self.selected_order = None
            self.detail_title.configure(text="Order details")
            self._set_detail("Select a saved order to inspect fills, recorded times, retries and reasons." if rows else
                             "No order rows match these filters." if counts is not None else
                             "No verified saved execution records are available for this session. This does not establish that no orders occurred.")
        observed = snapshot.observed_at if snapshot is not None else None
        observation_scope = "Whole session" if company == "All companies" else company
        if company != "All companies":
            observed = max((stamp for row in self._company_orders()
                            if (stamp := recorded_timestamp(row.get("last_observed_at"))) is not None), default=None)
        self.observation_label.configure(text=f"{observation_scope} last recorded broker observation: {recorded_time(observed)}\nSaved evidence only · Prediction/order refresh reads saved records only.")
        if snapshot is None or snapshot.report is None:
            coverage_text = "Coverage unavailable. No execution totals are inferred from missing records."
        else:
            coverage = snapshot.coverage
            reasons = coverage.get("reasons") or []
            coverage_text = f"Coverage: {state}. Pacific submission-date cohort; later fills update those attempts. "
            if counts is not None:
                coverage_text += (f"{counts.get('carryover_attempts', 0)} carryover records and "
                                  f"{counts.get('not_submitted', 0)} not-submitted records shown separately. ")
                if counts.get("unknown_submission"):
                    coverage_text += f"{counts['unknown_submission']} records have uncertain submission evidence. "
            coverage_text += " ".join(str(reason) for reason in reasons)
        self.coverage_label.configure(text=coverage_text)

    def _draw_distribution(self):
        canvas = self.distribution
        canvas.delete("all")
        counts = self._counts()
        if counts is None:
            canvas.create_text(8, 9, text="No saved distribution", fill=MUTED_TEXT, anchor="w", font=("Segoe UI", 8))
            return
        total = counts.get("submitted_attempts", 0)
        if not total:
            canvas.create_text(8, 9, text="No submitted attempts recorded", fill=MUTED_TEXT, anchor="w", font=("Segoe UI", 8))
            return
        left = 0
        for key in STATUS_LABELS:
            right = left + canvas.winfo_width() * counts.get(key, 0) / total
            canvas.create_rectangle(left, 0, right, 18, fill=STATUS_COLORS[key], outline="")
            left = right

    def _set_detail(self, text):
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")
        self.detail_text.yview_moveto(0)

    def _related_order(self, reference):
        related = next((row for row in self._orders if row.get("order_ref") == reference), None)
        short_ref = str(reference)[:10]
        if related is None:
            return short_ref + " · outside saved report"
        status = STATUS_LABELS.get(related.get("status"), "Unknown")
        return (f"{related.get('symbol', '—')} {recorded_time(related.get('submitted_at'), short=True)} · {status} · "
                f"{quantity(related.get('filled_quantity'))} / {quantity(related.get('requested_quantity'))} shares [{short_ref}]")

    def _select_order(self, _event=None):
        selected = self.tree.selection()
        row = self._visible_orders.get(selected[0]) if selected else None
        if row is None:
            return
        self.selected_order = row.get("order_ref")
        title = STATUS_LABELS.get(row.get("status"), "Not submitted" if row.get("status") == "not_submitted" else "Unknown")
        self.detail_title.configure(text=f"{row.get('symbol', '—')} · {str(row.get('side', '—')).title()} · {title}")
        lines = [f"Filled / requested: {quantity(row.get('filled_quantity'))} / {quantity(row.get('requested_quantity'))} shares",
                 f"Actual average fill price: {fill_price(row.get('average_fill_price'))}",
                 f"Submitted: {recorded_time(row.get('submitted_at'))}",
                 f"Last observed: {recorded_time(row.get('last_observed_at'))}",
                 f"Recorded lifecycle: {row.get('lifecycle_status') or 'Unknown'} · {row.get('session_scope', 'unknown')} cohort"]
        for name, key in (("Recorded", "recorded_at"), ("Cancel requested", "cancel_requested_at"),
                          ("Timeout requested", "timeout_requested_at")):
            if row.get(key):
                lines.append(f"{name}: {recorded_time(row[key])}")
        if row.get("filled_quantity_in_session") is not None:
            lines.append(f"Fill quantity dated in selected Pacific session: {quantity(row['filled_quantity_in_session'])}")
        if row.get("is_catchup"):
            lines.append("Catch-up attempt: may combine several forecast intentions.")
        if row.get("fill_details_complete") is False:
            lines.append("Fill-detail coverage is incomplete; recorded quantities are retained.")
        fills = row.get("fills") or []
        if fills:
            lines.append("Saved fills:")
            lines.extend(f"  {quantity(fill.get('quantity'))} shares at {fill_price(fill.get('price'))} · {recorded_time(fill.get('executed_at'))}" for fill in fills)
        else:
            lines.append("Fill-level records: not available")
        reasons = row.get("reasons") or []
        lines.append("Recorded reasons: " + ("; ".join(str(value) for value in reasons) if reasons else "None recorded"))
        for name, key in (("Retry / replacement parent", "retry_parent_ref"), ("Retry / replacement children", "retry_child_refs")):
            value = row.get(key)
            references = value if isinstance(value, list) else [value] if value else []
            lines.append(f"{name}: " + ("; ".join(self._related_order(ref) for ref in references) if references else "None recorded"))
        lines.append(f"Attempt reference: {str(row.get('order_ref'))[:10] if row.get('order_ref') else 'Not recorded'}")
        self._set_detail("\n".join(lines))
