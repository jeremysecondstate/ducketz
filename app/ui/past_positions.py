from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, ttk

from app.models.past_positions import (
    ClosedPosition,
    PastPositionFilters,
    PastPositionsSnapshot,
    PerformanceSummary,
    PositionOutcome,
)
from app.services.schwab_past_positions import (
    ALL_ACCOUNTS,
    ALL_STRATEGIES,
    DATE_RANGE_CHOICES,
    GROUP_BY_CHOICES,
    filter_closed_positions,
    group_closed_positions,
    performance_summary,
    positions_csv,
)
from app.ui.background_tasks import run_in_background
from app.ui.theme import (
    ACCENT,
    BACKGROUND,
    BORDER,
    DANGER,
    FIELD_BACKGROUND,
    MUTED_TEXT,
    SUCCESS,
    SURFACE,
    SURFACE_ALT,
    TABLE_FIELD,
    TEXT,
    WARNING,
)


@dataclass(frozen=True)
class PastPositionDetailState:
    title: str
    status: str
    related_orders_enabled: bool
    duplicate_template_enabled: bool
    duplicate_template_reason: str


def selected_position_detail_state(
    position: ClosedPosition | None,
) -> PastPositionDetailState:
    if position is None:
        return PastPositionDetailState(
            title="Select a Closed Position",
            status="No selection",
            related_orders_enabled=False,
            duplicate_template_enabled=False,
            duplicate_template_reason=(
                "Select a position. Templates store management policies, not historical OCC packages."
            ),
        )
    outcome = position.outcome.value if position.outcome else "Unavailable"
    return PastPositionDetailState(
        title=f"{position.underlying_symbol} · {position.strategy_label}",
        status=f"Closed — {outcome}",
        related_orders_enabled=bool(position.order_ids),
        duplicate_template_enabled=False,
        duplicate_template_reason=(
            "Unavailable: existing templates store roll/exit policies, not reusable OCC position structures."
        ),
    )


def route_related_orders(
    position: ClosedPosition | None,
    callback: Callable[[tuple[str, ...]], None],
) -> bool:
    if position is None or not position.order_ids:
        return False
    callback(position.order_ids)
    return True


def statement_text(
    filters: PastPositionFilters,
    summary: PerformanceSummary,
    positions: Sequence[ClosedPosition],
) -> str:
    lines = [
        "PAST POSITIONS STATEMENT",
        "Read-only local summary reconstructed from Schwab option executions",
        "",
        (
            f"Filters: Account={filters.account}; Date Range={filters.date_range}; "
            f"Symbol={filters.symbol or 'All Symbols'}; Strategy={filters.strategy}; "
            f"Group By={filters.group_by}"
        ),
        "",
        f"Net Realized P/L: {_money(summary.net_realized_pnl)}",
        f"Win Rate: {_percent(summary.win_rate)} ({summary.win_count} wins / {summary.loss_count} losses)",
        f"Profit Factor: {_number(summary.profit_factor, 2)}",
        f"Avg. Days Held: {_number(summary.average_days_held, 1)}",
        f"Breakeven: {summary.breakeven_count}",
        "",
        "CLOSED POSITIONS",
    ]
    for position in positions:
        lines.append(
            " | ".join(
                (
                    _short_date(position.close_time),
                    position.underlying_symbol,
                    position.strategy_label,
                    _money(position.realized_pnl),
                    _percent(position.return_fraction),
                    (
                        position.outcome.value
                        if position.outcome is not None
                        else "Outcome unavailable"
                    ),
                )
            )
        )
    if not positions:
        lines.append("No eligible closed positions match the active filters.")
    return "\n".join(lines)


class _ScrollableFrame(ttk.Frame):
    def __init__(self, parent: tk.Misc, *, style: str) -> None:
        super().__init__(parent, style=style)
        canvas = tk.Canvas(
            self,
            background=SURFACE,
            borderwidth=0,
            highlightthickness=0,
        )
        scroll = ttk.Scrollbar(self, orient=tk.VERTICAL, command=canvas.yview)
        body = ttk.Frame(canvas, style=style)
        window = canvas.create_window((0, 0), window=body, anchor=tk.NW)
        canvas.configure(yscrollcommand=scroll.set)
        body.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox(tk.ALL)),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window, width=event.width),
        )
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas = canvas
        self.body = body


class _ChartCanvas(tk.Canvas):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(
            parent,
            background=TABLE_FIELD,
            borderwidth=0,
            highlightthickness=0,
        )
        self.bind("<Configure>", lambda _event: self.redraw())

    def redraw(self) -> None:
        raise NotImplementedError


class _PnlChart(_ChartCanvas):
    def __init__(self, parent: tk.Misc) -> None:
        self.summary: PerformanceSummary | None = None
        super().__init__(parent)

    def set_summary(self, summary: PerformanceSummary) -> None:
        self.summary = summary
        self.redraw()

    def redraw(self) -> None:
        self.delete(tk.ALL)
        width = max(self.winfo_width(), 160)
        height = max(self.winfo_height(), 110)
        left, top, right, bottom = 54, 12, width - 12, height - 26
        points = self.summary.cumulative_pnl if self.summary else ()
        if not points:
            self.create_text(
                width / 2,
                height / 2,
                text="Realized P/L unavailable for the active filters",
                fill=MUTED_TEXT,
                font=("Segoe UI", 9),
            )
            return
        values = [0.0, *(point.value for point in points)]
        low, high = min(values), max(values)
        if math.isclose(low, high):
            low -= 1
            high += 1
        padding = max((high - low) * 0.12, 1.0)
        low -= padding
        high += padding
        for index in range(5):
            y = top + (bottom - top) * index / 4
            value = high - (high - low) * index / 4
            self.create_line(left, y, right, y, fill=BORDER, width=1)
            self.create_text(
                left - 7,
                y,
                text=_compact_money(value),
                fill=MUTED_TEXT,
                font=("Segoe UI", 8),
                anchor=tk.E,
            )
        x_values = [
            left + (right - left) * index / max(len(points) - 1, 1)
            for index in range(len(points))
        ]
        y_values = [
            bottom - (point.value - low) / (high - low) * (bottom - top)
            for point in points
        ]
        baseline = bottom - (0 - low) / (high - low) * (bottom - top)
        baseline = min(max(baseline, top), bottom)
        polygon = [x_values[0], baseline]
        for x, y in zip(x_values, y_values, strict=True):
            polygon.extend((x, y))
        polygon.extend((x_values[-1], baseline))
        tone = SUCCESS if points[-1].value >= 0 else DANGER
        self.create_polygon(*polygon, fill="#123c32" if tone == SUCCESS else "#3a2028", outline="")
        if len(points) == 1:
            self.create_oval(
                x_values[0] - 2,
                y_values[0] - 2,
                x_values[0] + 2,
                y_values[0] + 2,
                fill=tone,
                outline=tone,
            )
        else:
            coordinates = [value for pair in zip(x_values, y_values, strict=True) for value in pair]
            self.create_line(*coordinates, fill=tone, width=2, smooth=True)
        label_indexes = sorted({0, len(points) // 2, len(points) - 1})
        for index in label_indexes:
            anchor = tk.W if index == 0 else tk.E if index == len(points) - 1 else tk.CENTER
            self.create_text(
                x_values[index],
                height - 10,
                text=points[index].closed_at.strftime("%b '%y"),
                fill=MUTED_TEXT,
                font=("Segoe UI", 8),
                anchor=anchor,
            )


class _OutcomeChart(_ChartCanvas):
    def __init__(self, parent: tk.Misc) -> None:
        self.summary: PerformanceSummary | None = None
        super().__init__(parent)

    def set_summary(self, summary: PerformanceSummary) -> None:
        self.summary = summary
        self.redraw()

    def redraw(self) -> None:
        self.delete(tk.ALL)
        width = max(self.winfo_width(), 220)
        height = max(self.winfo_height(), 110)
        summary = self.summary
        total = (
            summary.win_count + summary.loss_count + summary.breakeven_count
            if summary
            else 0
        )
        if summary is None or not total:
            self.create_text(
                width / 2,
                height / 2,
                text="Wins / losses unavailable",
                fill=MUTED_TEXT,
                font=("Segoe UI", 9),
            )
            return
        size = min(height - 22, width * 0.42)
        x0, y0 = 14, (height - size) / 2
        start = 90.0
        for count, color in (
            (summary.win_count, SUCCESS),
            (summary.loss_count, DANGER),
            (summary.breakeven_count, WARNING),
        ):
            if not count:
                continue
            extent = -360 * count / total
            self.create_arc(
                x0,
                y0,
                x0 + size,
                y0 + size,
                start=start,
                extent=extent,
                fill=color,
                outline=TABLE_FIELD,
                width=1,
            )
            start += extent
        hole = size * 0.54
        inset = (size - hole) / 2
        self.create_oval(
            x0 + inset,
            y0 + inset,
            x0 + inset + hole,
            y0 + inset + hole,
            fill=TABLE_FIELD,
            outline=TABLE_FIELD,
        )
        self.create_text(
            x0 + size / 2,
            y0 + size / 2 - 7,
            text=str(total),
            fill=TEXT,
            font=("Segoe UI", 13, "bold"),
        )
        self.create_text(
            x0 + size / 2,
            y0 + size / 2 + 10,
            text="Closed",
            fill=MUTED_TEXT,
            font=("Segoe UI", 8),
        )
        legend_x = x0 + size + 17
        rows = (
            (SUCCESS, "Wins", summary.win_count, summary.gross_profit),
            (DANGER, "Losses", summary.loss_count, summary.gross_loss),
            (WARNING, "Breakeven", summary.breakeven_count, 0.0),
        )
        visible_rows = [row for row in rows if row[2] or row[1] != "Breakeven"]
        y = max(20, height / 2 - len(visible_rows) * 22)
        for color, label, count, pnl in visible_rows:
            self.create_oval(legend_x, y + 3, legend_x + 8, y + 11, fill=color, outline=color)
            self.create_text(
                legend_x + 14,
                y,
                text=f"{label}  {count} ({count / total:.1%})",
                fill=TEXT,
                font=("Segoe UI", 8),
                anchor=tk.NW,
            )
            self.create_text(
                legend_x + 14,
                y + 15,
                text=_money(pnl),
                fill=color if pnl else MUTED_TEXT,
                font=("Segoe UI", 8),
                anchor=tk.NW,
            )
            y += 43


class _StrategyChart(_ChartCanvas):
    def __init__(self, parent: tk.Misc) -> None:
        self.summary: PerformanceSummary | None = None
        super().__init__(parent)

    def set_summary(self, summary: PerformanceSummary) -> None:
        self.summary = summary
        self.redraw()

    def redraw(self) -> None:
        self.delete(tk.ALL)
        width = max(self.winfo_width(), 230)
        height = max(self.winfo_height(), 110)
        rows = self.summary.strategy_performance[:5] if self.summary else ()
        if not rows:
            self.create_text(
                width / 2,
                height / 2,
                text="Strategy performance unavailable",
                fill=MUTED_TEXT,
                font=("Segoe UI", 9),
            )
            return
        label_width = min(max(width * 0.29, 86), 150)
        value_width = 76
        chart_left = label_width
        chart_right = max(chart_left + 30, width - value_width)
        maximum = max(abs(row.realized_pnl) for row in rows) or 1.0
        row_height = (height - 16) / len(rows)
        for index, row in enumerate(rows):
            y = 8 + index * row_height
            self.create_text(
                8,
                y + row_height / 2,
                text=_ellipsize(row.strategy_label, 18),
                fill=TEXT,
                font=("Segoe UI", 8),
                anchor=tk.W,
            )
            self.create_rectangle(
                chart_left,
                y + row_height * 0.26,
                chart_right,
                y + row_height * 0.74,
                fill=SURFACE_ALT,
                outline="",
            )
            bar_right = chart_left + (chart_right - chart_left) * abs(row.realized_pnl) / maximum
            color = SUCCESS if row.realized_pnl >= 0 else DANGER
            self.create_rectangle(
                chart_left,
                y + row_height * 0.26,
                bar_right,
                y + row_height * 0.74,
                fill=color,
                outline="",
            )
            self.create_text(
                width - 7,
                y + row_height / 2,
                text=_compact_money(row.realized_pnl),
                fill=color,
                font=("Segoe UI", 8),
                anchor=tk.E,
            )


class PastPositionsView:
    def __init__(
        self,
        *,
        root: tk.Misc,
        parent: ttk.Frame,
        history_loader: Callable[[], PastPositionsSnapshot],
        on_related_orders: Callable[[tuple[str, ...]], None] | None = None,
        csv_exporter: Callable[[str], object] | None = None,
        today: Callable[[], date] = date.today,
        autoload: bool = True,
    ) -> None:
        self.root = root
        self.history_loader = history_loader
        self.on_related_orders = on_related_orders or (lambda _ids: None)
        self.csv_exporter = csv_exporter or self._save_csv_dialog
        self.today = today
        self.snapshot: PastPositionsSnapshot | None = None
        self.visible_positions: tuple[ClosedPosition, ...] = ()
        self.selected_position: ClosedPosition | None = None
        self._visible_by_iid: dict[str, ClosedPosition] = {}

        self.account = tk.StringVar(master=root, value=ALL_ACCOUNTS)
        self.date_range = tk.StringVar(master=root, value="YTD")
        self.symbol = tk.StringVar(master=root)
        self.strategy = tk.StringVar(master=root, value=ALL_STRATEGIES)
        self.group_by = tk.StringVar(master=root, value="Month")
        self.status = tk.StringVar(master=root, value="Loading Schwab execution history")
        self.detail_title = tk.StringVar(master=root, value="Select a Closed Position")
        self.detail_status = tk.StringVar(master=root, value="No selection")
        self.detail_template_reason = tk.StringVar(master=root)
        self._kpi_values = {
            key: tk.StringVar(master=root, value="—")
            for key in ("net", "win", "factor", "days")
        }
        self._kpi_details = {
            key: tk.StringVar(master=root)
            for key in ("net", "win", "factor", "days")
        }
        self._apply_styles()
        self._build(parent)
        if autoload:
            self.root.after_idle(self.refresh)

    def _apply_styles(self) -> None:
        style = ttk.Style(self.root)
        style.configure("PastPage.TFrame", background=BACKGROUND)
        style.configure(
            "PastCard.TFrame",
            background=SURFACE,
            bordercolor=BORDER,
            borderwidth=1,
            relief=tk.SOLID,
        )
        style.configure("PastBody.TFrame", background=SURFACE)
        style.configure("PastInset.TFrame", background=TABLE_FIELD)
        style.configure(
            "PastCardTitle.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "PastCardValue.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 16, "bold"),
        )
        style.configure(
            "PastPositive.TLabel",
            background=SURFACE,
            foreground=SUCCESS,
            font=("Segoe UI", 16, "bold"),
        )
        style.configure(
            "PastNegative.TLabel",
            background=SURFACE,
            foreground=DANGER,
            font=("Segoe UI", 16, "bold"),
        )
        style.configure(
            "PastSection.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "PastBody.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "PastMuted.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        )
        style.configure(
            "PastSuccess.TLabel",
            background=SURFACE,
            foreground=SUCCESS,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "PastDanger.TLabel",
            background=SURFACE,
            foreground=DANGER,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "Past.Treeview",
            background=TABLE_FIELD,
            foreground=TEXT,
            fieldbackground=TABLE_FIELD,
            bordercolor=BORDER,
            font=("Segoe UI", 9),
            rowheight=25,
        )
        style.configure(
            "Past.Treeview.Heading",
            background=SURFACE_ALT,
            foreground=TEXT,
            bordercolor=BORDER,
            font=("Segoe UI", 8),
        )
        style.map(
            "Past.Treeview",
            background=[("selected", "#123f68")],
            foreground=[("selected", "#ffffff")],
        )

    def _build(self, parent: ttk.Frame) -> None:
        outer = ttk.Frame(parent, padding=(7, 6, 7, 7), style="PastPage.TFrame")
        outer.pack(fill=tk.BOTH, expand=True)

        analytics = ttk.Frame(outer, style="PastPage.TFrame", height=255)
        analytics.pack(fill=tk.X)
        analytics.pack_propagate(False)
        kpis = ttk.Frame(analytics, style="PastPage.TFrame")
        kpis.pack(fill=tk.X)
        for index, (key, title) in enumerate(
            (
                ("net", "Net Realized P/L"),
                ("win", "Win Rate"),
                ("factor", "Profit Factor"),
                ("days", "Avg. Days Held"),
            )
        ):
            kpis.grid_columnconfigure(index, weight=1, uniform="kpi")
            card = ttk.Frame(kpis, padding=(10, 7), style="PastCard.TFrame")
            card.grid(
                row=0,
                column=index,
                sticky=tk.NSEW,
                padx=(0 if index == 0 else 3, 0 if index == 3 else 3),
            )
            ttk.Label(card, text=title, style="PastCardTitle.TLabel").pack(anchor=tk.W)
            label = ttk.Label(card, textvariable=self._kpi_values[key], style="PastCardValue.TLabel")
            label.pack(anchor=tk.W)
            ttk.Label(card, textvariable=self._kpi_details[key], style="PastMuted.TLabel").pack(anchor=tk.W)
            setattr(self, f"_{key}_kpi_label", label)

        charts = ttk.Frame(analytics, style="PastPage.TFrame")
        charts.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        for index, weight in enumerate((5, 3, 4)):
            charts.grid_columnconfigure(index, weight=weight, uniform="chart")
        charts.grid_rowconfigure(0, weight=1)
        self.pnl_chart = self._chart_panel(charts, 0, "Realized P/L Over Time", _PnlChart)
        self.outcome_chart = self._chart_panel(charts, 1, "Wins vs. Losses", _OutcomeChart)
        self.strategy_chart = self._chart_panel(charts, 2, "Performance by Strategy", _StrategyChart)

        toolbar = ttk.Frame(outer, padding=(8, 3), style="PastCard.TFrame")
        toolbar.pack(fill=tk.X, pady=(6, 6))
        toolbar.grid_columnconfigure(2, weight=1)
        toolbar.grid_columnconfigure(3, weight=1)
        toolbar.grid_columnconfigure(4, weight=1)
        self.account_box = self._toolbar_combo(
            toolbar, "Account", self.account, 0, (ALL_ACCOUNTS,), 16
        )
        self.date_box = self._toolbar_combo(
            toolbar, "Date Range", self.date_range, 1, DATE_RANGE_CHOICES, 15
        )
        symbol_frame = ttk.Frame(toolbar, style="PastBody.TFrame")
        symbol_frame.grid(row=0, column=2, sticky=tk.EW, padx=(7, 0))
        ttk.Label(symbol_frame, text="Symbol", style="PastMuted.TLabel").pack(anchor=tk.W)
        symbol_entry = ttk.Entry(symbol_frame, textvariable=self.symbol, width=13)
        symbol_entry.pack(fill=tk.X)
        symbol_entry.bind("<KeyRelease>", self._filters_changed)
        self.strategy_box = self._toolbar_combo(
            toolbar, "Strategy", self.strategy, 3, (ALL_STRATEGIES,), 18
        )
        self.group_box = self._toolbar_combo(
            toolbar, "Group By", self.group_by, 4, GROUP_BY_CHOICES, 12
        )
        actions = ttk.Frame(toolbar, style="PastBody.TFrame")
        actions.grid(row=0, column=5, sticky=tk.SE, padx=(10, 0))
        self.export_button = ttk.Button(actions, text="Export CSV", command=self._export_csv, state=tk.DISABLED)
        self.export_button.pack(side=tk.LEFT, padx=(0, 5))
        self.statement_button = ttk.Button(
            actions,
            text="View Statement",
            command=self._view_statement,
            state=tk.DISABLED,
        )
        self.statement_button.pack(side=tk.LEFT)

        panes = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True)
        list_surface = ttk.Frame(panes, padding=(7, 6), style="PastCard.TFrame")
        detail_surface = ttk.Frame(panes, padding=(8, 6), style="PastCard.TFrame")
        panes.add(list_surface, weight=1)
        panes.add(detail_surface, weight=1)
        panes.bind("<Configure>", self._balance_lower_panes)
        self._lower_panes = panes
        self._build_list(list_surface)
        self._build_detail(detail_surface)
        self._render_empty_detail()

    def _balance_lower_panes(self, event: tk.Event[ttk.PanedWindow]) -> None:
        if event.width < 200:
            return
        target = event.width // 2
        try:
            current = self._lower_panes.sashpos(0)
            if abs(current - target) > 8:
                self._lower_panes.sashpos(0, target)
        except tk.TclError:
            return

    def _chart_panel(
        self,
        parent: ttk.Frame,
        column: int,
        title: str,
        chart_type: type[_ChartCanvas],
    ) -> _ChartCanvas:
        panel = ttk.Frame(parent, padding=(7, 5), style="PastCard.TFrame")
        panel.grid(
            row=0,
            column=column,
            sticky=tk.NSEW,
            padx=(0 if column == 0 else 3, 0 if column == 2 else 3),
        )
        ttk.Label(panel, text=title, style="PastSection.TLabel").pack(anchor=tk.W, pady=(0, 2))
        chart = chart_type(panel)
        chart.pack(fill=tk.BOTH, expand=True)
        return chart

    def _toolbar_combo(
        self,
        parent: ttk.Frame,
        title: str,
        variable: tk.StringVar,
        column: int,
        values: Sequence[str],
        width: int,
    ) -> ttk.Combobox:
        frame = ttk.Frame(parent, style="PastBody.TFrame")
        frame.grid(row=0, column=column, sticky=tk.EW, padx=(0 if column == 0 else 7, 0))
        ttk.Label(frame, text=title, style="PastMuted.TLabel").pack(anchor=tk.W)
        combo = ttk.Combobox(
            frame,
            textvariable=variable,
            values=tuple(values),
            state="readonly",
            width=width,
        )
        combo.pack(fill=tk.X)
        combo.bind("<<ComboboxSelected>>", self._filters_changed)
        return combo

    def _build_list(self, parent: ttk.Frame) -> None:
        heading = ttk.Frame(parent, style="PastBody.TFrame")
        heading.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(heading, text="Closed Positions", style="PastSection.TLabel").pack(side=tk.LEFT)
        self.list_count = tk.StringVar(master=self.root)
        ttk.Label(heading, textvariable=self.list_count, style="PastMuted.TLabel").pack(side=tk.RIGHT)
        table_frame = ttk.Frame(parent, style="PastBody.TFrame")
        table_frame.pack(fill=tk.BOTH, expand=True)
        columns = ("symbol", "strategy", "pnl", "return", "days", "outcome")
        table = ttk.Treeview(
            table_frame,
            columns=columns,
            show="tree headings",
            selectmode="browse",
            style="Past.Treeview",
            height=10,
        )
        table.heading("#0", text="Close Date")
        table.column("#0", width=112, minwidth=92, anchor=tk.W, stretch=False)
        for name, label, width, anchor, stretch in (
            ("symbol", "Symbol", 64, tk.W, False),
            ("strategy", "Strategy / Expiration", 178, tk.W, True),
            ("pnl", "Realized P/L", 85, tk.E, False),
            ("return", "Return", 70, tk.E, False),
            ("days", "Days", 48, tk.E, False),
            ("outcome", "Outcome", 75, tk.CENTER, False),
        ):
            table.heading(name, text=label)
            table.column(name, width=width, minwidth=42, anchor=anchor, stretch=stretch)
        table.tag_configure("group", background=SURFACE_ALT, foreground=TEXT, font=("Segoe UI", 9, "bold"))
        table.tag_configure("win", foreground=SUCCESS)
        table.tag_configure("loss", foreground=DANGER)
        table.tag_configure("breakeven", foreground=WARNING)
        scroll_y = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=table.yview)
        scroll_x = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=table.xview)
        table.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        table.grid(row=0, column=0, sticky=tk.NSEW)
        scroll_y.grid(row=0, column=1, sticky=tk.NS)
        scroll_x.grid(row=1, column=0, sticky=tk.EW)
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)
        table.bind("<<TreeviewSelect>>", self._selection_changed)
        self.position_table = table
        ttk.Label(
            parent,
            textvariable=self.status,
            style="PastMuted.TLabel",
            wraplength=700,
            justify=tk.LEFT,
        ).pack(fill=tk.X, pady=(4, 0))

    def _build_detail(self, parent: ttk.Frame) -> None:
        header = ttk.Frame(parent, style="PastBody.TFrame")
        header.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(header, textvariable=self.detail_title, style="PastSection.TLabel").pack(side=tk.LEFT)
        self.detail_status_label = ttk.Label(
            header,
            textvariable=self.detail_status,
            style="PastMuted.TLabel",
        )
        self.detail_status_label.pack(side=tk.RIGHT)
        actions = ttk.Frame(parent, style="PastBody.TFrame")
        actions.pack(side=tk.BOTTOM, fill=tk.X, pady=(6, 0))
        self.duplicate_button = ttk.Button(
            actions,
            text="Duplicate as Template · Unavailable",
            state=tk.DISABLED,
        )
        self.duplicate_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))
        self.related_button = ttk.Button(
            actions,
            text="View Related Orders",
            command=self._view_related_orders,
            state=tk.DISABLED,
        )
        self.related_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        scroll = _ScrollableFrame(parent, style="PastBody.TFrame")
        scroll.pack(fill=tk.BOTH, expand=True)
        self.detail_scroll = scroll

    def refresh(self) -> None:
        self.status.set("Loading Schwab order and transaction history…")
        self.export_button.configure(state=tk.DISABLED)
        self.statement_button.configure(state=tk.DISABLED)
        run_in_background(
            self.root,
            self.history_loader,
            self.show_snapshot,
            self.show_refresh_error,
        )

    def show_snapshot(self, snapshot: PastPositionsSnapshot) -> None:
        self.snapshot = snapshot
        accounts = tuple(sorted({position.account_label for position in snapshot.positions}))
        strategies = tuple(sorted({position.strategy_label for position in snapshot.positions}))
        self.account_box.configure(values=(ALL_ACCOUNTS, *accounts))
        self.strategy_box.configure(values=(ALL_STRATEGIES, *strategies))
        if self.account.get() not in (ALL_ACCOUNTS, *accounts):
            self.account.set(ALL_ACCOUNTS)
        if self.strategy.get() not in (ALL_STRATEGIES, *strategies):
            self.strategy.set(ALL_STRATEGIES)
        self._render_filtered()

    def show_refresh_error(self, exc: Exception) -> None:
        if self.snapshot is None:
            self.status.set(f"Past Positions unavailable · {type(exc).__name__}: {exc}")
            self.visible_positions = ()
            self._render_list()
            self._update_analytics(performance_summary(()))
        else:
            self.status.set(
                f"Refresh failed · showing prior data · {type(exc).__name__}: {exc}"
            )
            self.export_button.configure(state=tk.NORMAL if self.visible_positions else tk.DISABLED)
            self.statement_button.configure(state=tk.NORMAL)

    def _filters_changed(self, _event: object = None) -> None:
        self._render_filtered()

    def _filters(self) -> PastPositionFilters:
        return PastPositionFilters(
            account=self.account.get(),
            date_range=self.date_range.get(),
            symbol=self.symbol.get(),
            strategy=self.strategy.get(),
            group_by=self.group_by.get(),
        )

    def _render_filtered(self) -> None:
        if self.snapshot is None:
            return
        self.visible_positions = filter_closed_positions(
            self.snapshot.positions,
            self._filters(),
            today=self.snapshot.range_end,
        )
        summary = performance_summary(self.visible_positions)
        self._update_analytics(summary)
        self._render_list()
        stale = " · STALE" if self.snapshot.stale else ""
        refresh_error = f" · {self.snapshot.refresh_error}" if self.snapshot.refresh_error else ""
        self.status.set(
            f"{self.snapshot.status}{stale} · {self.snapshot.coverage.summary}{refresh_error}"
        )
        self.export_button.configure(state=tk.NORMAL if self.visible_positions else tk.DISABLED)
        self.statement_button.configure(state=tk.NORMAL)

    def _update_analytics(self, summary: PerformanceSummary) -> None:
        self._kpi_values["net"].set(_money(summary.net_realized_pnl))
        self._kpi_details["net"].set(
            f"{summary.included_position_count:,} eligible closed position(s)"
        )
        self._kpi_values["win"].set(_percent(summary.win_rate))
        self._kpi_details["win"].set(
            f"{summary.win_count:,} wins / {summary.loss_count:,} losses"
            + (f" / {summary.breakeven_count:,} breakeven" if summary.breakeven_count else "")
        )
        self._kpi_values["factor"].set(_number(summary.profit_factor, 2))
        self._kpi_details["factor"].set(
            f"Gross profit {_money(summary.gross_profit)} / gross loss {_money(summary.gross_loss)}"
        )
        self._kpi_values["days"].set(_number(summary.average_days_held, 1))
        self._kpi_details["days"].set(
            f"{summary.holding_time_count:,} position(s) with complete timestamps"
        )
        self._net_kpi_label.configure(
            style=(
                "PastPositive.TLabel"
                if summary.net_realized_pnl is not None and summary.net_realized_pnl >= 0
                else "PastNegative.TLabel"
                if summary.net_realized_pnl is not None
                else "PastCardValue.TLabel"
            )
        )
        self._win_kpi_label.configure(style="PastPositive.TLabel" if summary.win_rate is not None else "PastCardValue.TLabel")
        self._factor_kpi_label.configure(style="PastPositive.TLabel" if summary.profit_factor is not None else "PastCardValue.TLabel")
        self.pnl_chart.set_summary(summary)
        self.outcome_chart.set_summary(summary)
        self.strategy_chart.set_summary(summary)

    def _render_list(self) -> None:
        preferred = self.selected_position.position_id if self.selected_position else None
        for item in self.position_table.get_children():
            self.position_table.delete(item)
        self._visible_by_iid = {}
        for group_index, (label, rows) in enumerate(
            group_closed_positions(self.visible_positions, self.group_by.get())
        ):
            group_iid = f"group:{group_index}"
            self.position_table.insert(
                "",
                tk.END,
                iid=group_iid,
                text=f"  {label}",
                values=("", "", "", "", "", ""),
                tags=("group",),
                open=True,
            )
            for position in rows:
                iid = f"position:{position.position_id}"
                self._visible_by_iid[iid] = position
                expiration = position.expiration.strftime("%b %d, %Y") if position.expiration else "Expiration unavailable"
                outcome = position.outcome.value if position.outcome else "Unavailable"
                tag = outcome.casefold() if outcome.casefold() in {"win", "loss", "breakeven"} else ""
                self.position_table.insert(
                    group_iid,
                    tk.END,
                    iid=iid,
                    text=_short_date(position.close_time),
                    values=(
                        position.underlying_symbol,
                        f"{position.strategy_label} · {expiration}",
                        _signed_money(position.realized_pnl),
                        _signed_percent(position.return_fraction),
                        _whole_days(position.holding_days),
                        outcome,
                    ),
                    tags=((tag,) if tag else ()),
                )
        self.list_count.set(f"{len(self.visible_positions):,} position(s)")
        target_iid = next(
            (
                iid
                for iid, position in self._visible_by_iid.items()
                if position.position_id == preferred
            ),
            next(iter(self._visible_by_iid), None),
        )
        if target_iid:
            self.position_table.selection_set(target_iid)
            self.position_table.focus(target_iid)
            self._select_position(self._visible_by_iid[target_iid])
        else:
            self._select_position(None)

    def _selection_changed(self, _event: object = None) -> None:
        selected = self.position_table.selection()
        if not selected:
            return
        position = self._visible_by_iid.get(selected[0])
        if position is not None:
            self._select_position(position)

    def _select_position(self, position: ClosedPosition | None) -> None:
        self.selected_position = position
        if position is None:
            self._render_empty_detail()
        else:
            self._render_position_detail(position)

    def _clear_detail(self) -> None:
        for child in self.detail_scroll.body.winfo_children():
            child.destroy()

    def _render_empty_detail(self) -> None:
        self._clear_detail()
        state = selected_position_detail_state(None)
        self.detail_title.set(state.title)
        self.detail_status.set(state.status)
        self.detail_status_label.configure(style="PastMuted.TLabel")
        self.detail_template_reason.set(state.duplicate_template_reason)
        self.related_button.configure(state=tk.DISABLED)
        ttk.Label(
            self.detail_scroll.body,
            text="Choose a row to inspect matched execution cash flows, exact OCC legs, timeline, and provenance.",
            style="PastMuted.TLabel",
            wraplength=650,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=8, pady=14)

    def _render_position_detail(self, position: ClosedPosition) -> None:
        self._clear_detail()
        body = self.detail_scroll.body
        state = selected_position_detail_state(position)
        self.detail_title.set(state.title)
        self.detail_status.set(state.status)
        self.detail_status_label.configure(
            style=(
                "PastSuccess.TLabel"
                if position.outcome == PositionOutcome.WIN
                else "PastDanger.TLabel"
                if position.outcome == PositionOutcome.LOSS
                else "PastMuted.TLabel"
            )
        )
        self.detail_template_reason.set(state.duplicate_template_reason)
        self.related_button.configure(state=tk.NORMAL if state.related_orders_enabled else tk.DISABLED)

        metrics = ttk.Frame(body, padding=(7, 5), style="PastInset.TFrame")
        metrics.pack(fill=tk.X, pady=(0, 5))
        for index, (label, value, tone) in enumerate(
            (
                ("Realized P/L", _signed_money(position.realized_pnl), _tone(position.realized_pnl)),
                ("Return", _signed_percent(position.return_fraction), _tone(position.return_fraction)),
                ("Days Held", _whole_days(position.holding_days), "neutral"),
            )
        ):
            metrics.grid_columnconfigure(index, weight=1)
            frame = ttk.Frame(metrics, style="PastInset.TFrame")
            frame.grid(row=0, column=index, sticky=tk.EW, padx=(0 if index == 0 else 6, 0))
            tk.Label(
                frame,
                text=label,
                background=TABLE_FIELD,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8),
            ).pack(anchor=tk.W)
            tk.Label(
                frame,
                text=value,
                background=TABLE_FIELD,
                foreground=SUCCESS if tone == "positive" else DANGER if tone == "negative" else TEXT,
                font=("Segoe UI", 11, "bold"),
            ).pack(anchor=tk.W)

        ttk.Label(body, text="Lifecycle Timeline", style="PastSection.TLabel").pack(anchor=tk.W)
        timeline = ttk.Frame(body, padding=(6, 4), style="PastInset.TFrame")
        timeline.pack(fill=tk.X, pady=(2, 5))
        if position.timeline:
            for index, event in enumerate(position.timeline):
                timeline.grid_columnconfigure(index, weight=1)
                event_frame = ttk.Frame(timeline, style="PastInset.TFrame")
                event_frame.grid(row=0, column=index, sticky=tk.NSEW, padx=(0 if index == 0 else 4, 0))
                tk.Label(
                    event_frame,
                    text=event.label,
                    background=TABLE_FIELD,
                    foreground=TEXT,
                    font=("Segoe UI", 8, "bold"),
                ).pack(anchor=tk.W)
                tk.Label(
                    event_frame,
                    text=_detail_timestamp(event.occurred_at),
                    background=TABLE_FIELD,
                    foreground=MUTED_TEXT,
                    font=("Segoe UI", 7),
                    justify=tk.LEFT,
                ).pack(anchor=tk.W)
        else:
            tk.Label(
                timeline,
                text="Lifecycle events unavailable beyond matched executions.",
                background=TABLE_FIELD,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8),
            ).pack(anchor=tk.W)

        ttk.Label(body, text="Exact Option Legs", style="PastSection.TLabel").pack(anchor=tk.W)
        legs = ttk.Treeview(
            body,
            columns=("open", "close", "qty", "contract", "expiry", "strike", "right", "entry", "exit"),
            show="headings",
            height=max(1, min(5, len(position.legs))),
            selectmode="none",
            style="Past.Treeview",
        )
        for name, label, width, anchor, stretch in (
            ("open", "Open", 94, tk.W, False),
            ("close", "Close", 94, tk.W, False),
            ("qty", "Qty", 38, tk.E, False),
            ("contract", "Exact OCC", 184, tk.W, True),
            ("expiry", "Expiration", 82, tk.W, False),
            ("strike", "Strike", 58, tk.E, False),
            ("right", "C/P", 34, tk.CENTER, False),
            ("entry", "Entry", 52, tk.E, False),
            ("exit", "Exit", 52, tk.E, False),
        ):
            legs.heading(name, text=label)
            legs.column(name, width=width, minwidth=30, anchor=anchor, stretch=stretch)
        for leg in position.legs:
            legs.insert(
                "",
                tk.END,
                values=(
                    _instruction(leg.opening_instruction),
                    _instruction(leg.closing_instruction),
                    f"{leg.quantity:g}",
                    leg.contract.occ_symbol,
                    leg.contract.expiration.strftime("%b %d, %Y"),
                    f"{leg.contract.strike:g}",
                    "C" if leg.contract.option_type == "CALL" else "P",
                    _money(leg.entry_price),
                    _money(leg.exit_price),
                ),
            )
        legs.pack(fill=tk.X, pady=(2, 5))

        cash = ttk.Frame(body, style="PastBody.TFrame")
        cash.pack(fill=tk.X)
        cash.grid_columnconfigure(0, weight=1)
        cash.grid_columnconfigure(1, weight=1)
        self._cash_summary(
            cash,
            0,
            "Entry",
            position.opening_cash_flow,
            position.open_time,
        )
        self._cash_summary(
            cash,
            1,
            "Exit",
            position.closing_cash_flow,
            position.close_time,
        )

        facts = ttk.Frame(body, padding=(7, 5), style="PastInset.TFrame")
        facts.pack(fill=tk.X, pady=(5, 0))
        for row, (label, value) in enumerate(
            (
                ("Close Reason", position.close_reason or "Not provided by Schwab"),
                ("Max Profit", _money(position.max_profit) if position.max_profit is not None else "Unavailable for this exact structure"),
                ("Max Loss", _money(position.max_loss) if position.max_loss is not None else "Unavailable or unbounded for this exact structure"),
                (
                    "Fees",
                    (
                        _money(position.fees)
                        if position.fees is not None and position.fees_complete
                        else f"{_money(position.fees)} known; some fees unavailable"
                        if position.fees is not None
                        else "Not reported in the matched Schwab execution evidence"
                    ),
                ),
                ("Notes", position.notes or "Not provided by Schwab"),
                ("Template", state.duplicate_template_reason),
            )
        ):
            tk.Label(
                facts,
                text=label,
                background=TABLE_FIELD,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8),
            ).grid(row=row, column=0, sticky=tk.NW, padx=(0, 12), pady=1)
            tk.Label(
                facts,
                text=value,
                background=TABLE_FIELD,
                foreground=TEXT,
                font=("Segoe UI", 8),
                justify=tk.LEFT,
                anchor=tk.W,
            ).grid(row=row, column=1, sticky=tk.EW, pady=1)
        facts.grid_columnconfigure(1, weight=1)

    def _cash_summary(
        self,
        parent: ttk.Frame,
        column: int,
        title: str,
        cash_flow: float | None,
        occurred_at: datetime | None,
    ) -> None:
        frame = ttk.Frame(parent, padding=(7, 5), style="PastInset.TFrame")
        frame.grid(row=0, column=column, sticky=tk.NSEW, padx=(0 if column == 0 else 3, 3 if column == 0 else 0))
        tk.Label(
            frame,
            text=title,
            background=TABLE_FIELD,
            foreground=TEXT,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor=tk.W)
        if cash_flow is None:
            cash_label = "Cash flow unavailable"
        else:
            cash_label = f"Net {'Credit' if cash_flow >= 0 else 'Debit'}  {_money(abs(cash_flow))}"
        tk.Label(
            frame,
            text=cash_label,
            background=TABLE_FIELD,
            foreground=TEXT,
            font=("Segoe UI", 8),
        ).pack(anchor=tk.W)
        tk.Label(
            frame,
            text=_detail_timestamp(occurred_at),
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 7),
        ).pack(anchor=tk.W)

    def _view_related_orders(self) -> None:
        if route_related_orders(self.selected_position, self.on_related_orders):
            self.status.set("Opened Orders and requested the matched broker order IDs.")

    def _export_csv(self) -> None:
        if not self.visible_positions:
            return
        try:
            result = self.csv_exporter(positions_csv(self.visible_positions))
        except Exception as exc:
            self.status.set(f"CSV export failed · {type(exc).__name__}: {exc}")
            return
        if result is not None:
            self.status.set(f"Exported {len(self.visible_positions):,} filtered position(s) to {result}")

    def _save_csv_dialog(self, content: str) -> Path | None:
        filename = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export Filtered Past Positions",
            defaultextension=".csv",
            filetypes=(("CSV files", "*.csv"),),
            initialfile="past-positions.csv",
        )
        if not filename:
            return None
        path = Path(filename)
        path.write_text(content, encoding="utf-8", newline="")
        return path

    def _view_statement(self) -> None:
        summary = performance_summary(self.visible_positions)
        window = tk.Toplevel(self.root)
        window.title("Past Positions Statement · Read Only")
        window.geometry("860x620")
        window.minsize(640, 420)
        window.configure(background=BACKGROUND)
        text = tk.Text(
            window,
            background=TABLE_FIELD,
            foreground=TEXT,
            insertbackground=TEXT,
            selectbackground=ACCENT,
            font=("Consolas", 10),
            borderwidth=1,
            relief=tk.SOLID,
            padx=14,
            pady=12,
            wrap=tk.NONE,
        )
        scroll_y = ttk.Scrollbar(window, orient=tk.VERTICAL, command=text.yview)
        scroll_x = ttk.Scrollbar(window, orient=tk.HORIZONTAL, command=text.xview)
        text.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        text.grid(row=0, column=0, sticky=tk.NSEW, padx=(10, 0), pady=(10, 0))
        scroll_y.grid(row=0, column=1, sticky=tk.NS, pady=(10, 0))
        scroll_x.grid(row=1, column=0, sticky=tk.EW, padx=(10, 0))
        close = ttk.Button(window, text="Close", command=window.destroy)
        close.grid(row=2, column=0, columnspan=2, sticky=tk.E, padx=10, pady=10)
        window.grid_rowconfigure(0, weight=1)
        window.grid_columnconfigure(0, weight=1)
        text.insert(tk.END, statement_text(self._filters(), summary, self.visible_positions))
        text.configure(state=tk.DISABLED)


def _instruction(value: str) -> str:
    return {
        "BUY_TO_OPEN": "Buy to Open",
        "SELL_TO_OPEN": "Sell to Open",
        "BUY_TO_CLOSE": "Buy to Close",
        "SELL_TO_CLOSE": "Sell to Close",
    }.get(value, value.replace("_", " ").title())


def _tone(value: float | None) -> str:
    return "neutral" if value is None else "positive" if value >= 0 else "negative"


def _money(value: float | None) -> str:
    if value is None:
        return "Unavailable"
    return f"${value:,.2f}"


def _signed_money(value: float | None) -> str:
    if value is None:
        return "Unavailable"
    return f"{'+' if value > 0 else ''}${value:,.2f}" if value >= 0 else f"-${abs(value):,.2f}"


def _compact_money(value: float) -> str:
    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    if magnitude >= 1_000_000:
        return f"{sign}${magnitude / 1_000_000:.1f}M"
    if magnitude >= 1_000:
        return f"{sign}${magnitude / 1_000:.0f}K"
    return f"{sign}${magnitude:.0f}"


def _percent(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value:.1%}"


def _signed_percent(value: float | None) -> str:
    return "Unavailable" if value is None else f"{value:+.2%}"


def _number(value: float | None, digits: int) -> str:
    return "Unavailable" if value is None else f"{value:.{digits}f}"


def _whole_days(value: float | None) -> str:
    return "Unavailable" if value is None else str(int(round(value)))


def _short_date(value: datetime | None) -> str:
    return "Unavailable" if value is None else value.strftime("%b %d, %Y")


def _detail_timestamp(value: datetime | None) -> str:
    return "Unavailable" if value is None else value.astimezone().strftime("%b %d, %Y\n%I:%M %p")


def _ellipsize(value: str, length: int) -> str:
    return value if len(value) <= length else value[: max(1, length - 1)] + "…"


__all__ = [
    "PastPositionDetailState",
    "PastPositionsView",
    "route_related_orders",
    "selected_position_detail_state",
    "statement_text",
]
