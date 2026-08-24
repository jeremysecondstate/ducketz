from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from tkinter import messagebox, ttk

from app.models.past_positions import PastPositionsSnapshot
from app.models.portfolio import PortfolioSnapshot
from app.services.schwab import SchwabSession, sync_schwab_portfolio
from app.services.schwab_past_positions import SchwabPastPositionsService
from app.services.schwab_strategy_orders import (
    DAY_ONLY,
    GOOD_UNTIL_CANCELED,
    MARKET_ORDER,
    StrategyOrderDraft,
)
from app.services.strategy_portfolio_impact import (
    StrategyPortfolioImpact,
    calculate_strategy_portfolio_impact,
)
from app.services.strategy_order_review import (
    StrategyEntryOrderReviewDraft,
    StrategyOrderReviewController,
    build_strategy_entry_review_draft,
    refresh_strategy_entry_review_draft,
)
from app.ui.options_strategy_data import (
    HORIZON_LABELS,
    StrategyCandidateView,
    StrategyCandidatesView,
    load_strategy_candidates,
)
from app.ui.background_tasks import run_in_background
from app.ui.options_management import OptionsManagementView
from app.ui.option_templates import OptionsTemplatesView
from app.ui.option_order_review import OptionOrderReviewDialog
from app.ui.past_positions import PastPositionsView
from app.ui.theme import (
    ACCENT,
    BACKGROUND,
    BORDER,
    DANGER,
    MUTED_TEXT,
    SUCCESS,
    SURFACE,
    SURFACE_ALT,
    TEXT,
    WARNING,
)


OPTIONS_COMMAND_TABS = (
    "Discover",
    "Positions",
    "Orders",
    "Templates",
    "Past Positions",
)

_CANDIDATE_COLUMNS = (
    ("rank", "Rank", 48, tk.E),
    ("strategy", "Strategy", 135, tk.W),
    ("exact_legs", "Exact Legs", 195, tk.W),
    ("direction_probability_up", "Direction Up (ML)", 135, tk.E),
    ("calibrated_probability", "ML Profit Probability", 155, tk.E),
    ("scenario_coverage", "Scenario Coverage", 135, tk.E),
    ("expected_return", "Expected Return", 115, tk.E),
    ("portfolio_fit", "Portfolio Fit", 95, tk.W),
    ("score_basis", "Score Basis", 160, tk.W),
    ("pricing_quality", "Pricing / Quality", 280, tk.W),
)
_CANDIDATE_COLUMN_LABELS = {
    name: label for name, label, _width, _anchor in _CANDIDATE_COLUMNS
}
_DESCENDING_FIRST_CANDIDATE_COLUMNS = frozenset(
    {
        "direction_probability_up",
        "calibrated_probability",
        "scenario_coverage",
        "expected_return",
    }
)


@dataclass(frozen=True)
class _DecisionEvidence:
    model: str
    quote_legs_available: int
    quote_legs_total: int
    candidate_snapshot: str
    publication_notices: tuple[str, ...]
    publication_checks: tuple[tuple[str, bool], ...]
    direction_probability_up: float
    predictive_score: float | None
    scenario_coverage: float
    expected_return: float | None
    execution_status: str
    execution_detail: str


class OptionsStrategiesTab:
    def __init__(
        self,
        *,
        root: tk.Tk,
        parent: ttk.Frame,
        candidates_path: Path | None = None,
        snapshot_loader: Callable[[], PortfolioSnapshot] = sync_schwab_portfolio,
        session_factory: Callable[[], SchwabSession] = SchwabSession,
        past_positions_loader: Callable[[], PastPositionsSnapshot] | None = None,
    ) -> None:
        self.root = root
        self.candidates_path = candidates_path
        self.snapshot_loader = snapshot_loader
        self.session_factory = session_factory
        self._past_positions_service = (
            None
            if past_positions_loader is not None
            else SchwabPastPositionsService(session_factory=session_factory)
        )
        self.past_positions_loader = (
            past_positions_loader
            if past_positions_loader is not None
            else self._past_positions_service.load
        )
        self.view: StrategyCandidatesView | None = None
        self.snapshot: PortfolioSnapshot | None = None
        self.management_view: OptionsManagementView | None = None
        self.past_positions_view: PastPositionsView | None = None
        self.visible_candidates: tuple[StrategyCandidateView, ...] = ()
        self.selected_candidate: StrategyCandidateView | None = None
        self.selected_order_index = 0
        self.refresh_button: ttk.Button | None = None
        self.candidate_table: ttk.Treeview | None = None
        self.ticket_legs: ttk.Treeview | None = None
        self.submit_button: ttk.Button | None = None
        self.decision_details_button: ttk.Button | None = None
        self._decision_details_window: tk.Toplevel | None = None
        self._decision_status_label: ttk.Label | None = None
        self._decision_funds_after_value: ttk.Label | None = None
        self._decision_cash_flow_value: ttk.Label | None = None
        self._decision_pricing_text: tk.Text | None = None
        self._decision_assurance_text: tk.Text | None = None
        self._candidate_sort_column: str | None = None
        self._candidate_sort_descending = False

        self.symbol = tk.StringVar()
        self.horizon_label = tk.StringVar()
        self.position_summary = tk.StringVar(value="Syncing Schwab Account")
        self.candidate_summary = tk.StringVar(value="Loading Strategy Candidates")
        self.ticket_strategy = tk.StringVar(value="Select Exact Legs")
        self.ticket_structure = tk.StringVar(value="")
        self.ticket_order_part = tk.StringVar(value="")
        self.ticket_quantity = tk.StringVar(value="1")
        self.ticket_order_method = tk.StringVar()
        self.ticket_limit_price = tk.StringVar()
        self.ticket_duration = tk.StringVar(value=DAY_ONLY)
        self.impact_source = tk.StringVar(value="Select exact legs to calculate impact")
        self.impact_status = tk.StringVar(value="No Strategy Selected")
        self.impact_available_label = tk.StringVar(value="Applicable Funds")
        self.impact_available = tk.StringVar(value="—")
        self.impact_requirement = tk.StringVar(value="—")
        self.impact_funds_after = tk.StringVar(value="—")
        self.impact_cash_balance = tk.StringVar(value="—")
        self.impact_buying_power = tk.StringVar(value="—")
        self.impact_cash_flow = tk.StringVar(value="—")
        self.impact_requirement_basis = tk.StringVar(value="")
        self.impact_position = tk.StringVar(value="")
        self.impact_decision_summary = tk.StringVar(
            value="Select a strategy to inspect its decision evidence"
        )

        self._apply_styles()
        self._build(parent)
        for variable in (
            self.ticket_quantity,
            self.ticket_order_method,
            self.ticket_limit_price,
        ):
            variable.trace_add("write", self._ticket_impact_changed)
        self.root.after_idle(self.refresh)

    def _apply_styles(self) -> None:
        style = ttk.Style(self.root)
        style.configure(
            "StrategyPage.TFrame",
            background=BACKGROUND,
        )
        style.configure(
            "StrategySurface.TFrame",
            background=SURFACE,
            bordercolor=BORDER,
            borderwidth=1,
            relief=tk.SOLID,
        )
        style.configure(
            "StrategyTitle.TLabel",
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 18, "bold"),
        )
        style.configure(
            "StrategySubtitle.TLabel",
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 10),
        )
        style.configure(
            "StrategyHeading.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 12, "bold"),
        )
        style.configure(
            "StrategyBody.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 10),
        )
        style.configure(
            "StrategyMuted.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "StrategyImpactMetric.TFrame",
            background=SURFACE_ALT,
            bordercolor=BORDER,
            borderwidth=1,
            relief=tk.SOLID,
        )
        style.configure(
            "StrategyImpactMetricLabel.TLabel",
            background=SURFACE_ALT,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        )
        style.configure(
            "StrategyImpactMetricValue.TLabel",
            background=SURFACE_ALT,
            foreground=TEXT,
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "StrategyImpactMetricGood.TLabel",
            background=SURFACE_ALT,
            foreground=SUCCESS,
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "StrategyImpactMetricDanger.TLabel",
            background=SURFACE_ALT,
            foreground=DANGER,
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "StrategyImpactGood.TLabel",
            background=SURFACE,
            foreground=SUCCESS,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "StrategyImpactWarning.TLabel",
            background=SURFACE,
            foreground=WARNING,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "StrategyImpactDanger.TLabel",
            background=SURFACE,
            foreground=DANGER,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "StrategySubmit.TButton",
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
            font=("Segoe UI", 10, "bold"),
            padding=(12, 9),
        )
        style.map(
            "StrategySubmit.TButton",
            background=[("active", "#93c5fd"), ("disabled", SURFACE_ALT)],
            foreground=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "StrategyDetails.TButton",
            background=SURFACE_ALT,
            foreground=TEXT,
            bordercolor=BORDER,
            font=("Segoe UI", 9, "bold"),
            padding=(10, 6),
        )
        style.map(
            "StrategyDetails.TButton",
            background=[("active", BORDER), ("disabled", SURFACE)],
            foreground=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "StrategySecondary.TNotebook",
            background=BACKGROUND,
            bordercolor=BORDER,
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "StrategySecondary.TNotebook.Tab",
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 10),
            borderwidth=0,
            padding=(15, 7),
        )
        style.map(
            "StrategySecondary.TNotebook.Tab",
            background=[("selected", BACKGROUND), ("active", SURFACE_ALT)],
            foreground=[("selected", ACCENT), ("active", TEXT)],
        )

    def _build(self, parent: ttk.Frame) -> None:
        outer = ttk.Frame(
            parent,
            padding=(12, 9, 12, 10),
            style="StrategyPage.TFrame",
        )
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer, style="StrategyPage.TFrame")
        header.pack(fill=tk.X, pady=(0, 8))
        heading = ttk.Frame(header, style="StrategyPage.TFrame")
        heading.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(
            heading,
            text="Options Command Center",
            style="StrategyTitle.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            heading,
            text=(
                "Discover, monitor, and safely close exact Schwab option positions."
            ),
            style="StrategySubtitle.TLabel",
        ).pack(anchor=tk.W, pady=(2, 0))
        self.refresh_button = ttk.Button(
            header,
            text="Refresh",
            command=self.refresh,
        )
        self.refresh_button.pack(side=tk.RIGHT, anchor=tk.N)

        notebook = ttk.Notebook(outer, style="StrategySecondary.TNotebook")
        notebook.pack(fill=tk.BOTH, expand=True)
        positions_frame = ttk.Frame(notebook, style="StrategyPage.TFrame")
        discover_frame = ttk.Frame(notebook, style="StrategyPage.TFrame")
        orders_frame = ttk.Frame(notebook, style="StrategyPage.TFrame")
        templates_frame = ttk.Frame(notebook, style="StrategyPage.TFrame")
        past_positions_frame = ttk.Frame(notebook, style="StrategyPage.TFrame")
        notebook.add(discover_frame, text="Discover")
        notebook.add(positions_frame, text="Positions")
        notebook.add(orders_frame, text="Orders")
        notebook.add(templates_frame, text="Templates")
        notebook.add(past_positions_frame, text="Past Positions")
        notebook.select(positions_frame)
        self._secondary_notebook = notebook

        self._build_discover(discover_frame)
        self.management_view = OptionsManagementView(
            root=self.root,
            positions_parent=positions_frame,
            orders_parent=orders_frame,
            snapshot_loader=self.snapshot_loader,
            session_factory=self.session_factory,
            on_refresh=self.refresh,
            on_show_orders=lambda: notebook.select(orders_frame),
        )
        self.templates_view = OptionsTemplatesView(
            root=self.root,
            parent=templates_frame,
        )
        self.past_positions_view = PastPositionsView(
            root=self.root,
            parent=past_positions_frame,
            history_loader=self.past_positions_loader,
            on_related_orders=self.management_view.show_related_orders,
            autoload=False,
        )

    def _build_discover(self, parent: ttk.Frame) -> None:
        outer = ttk.Frame(
            parent,
            padding=(10, 9),
            style="StrategyPage.TFrame",
        )
        outer.pack(fill=tk.BOTH, expand=True)

        controls = ttk.Frame(
            outer,
            padding=(12, 10),
            style="StrategySurface.TFrame",
        )
        controls.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(
            controls,
            text="Symbol",
            style="StrategyBody.TLabel",
        ).pack(side=tk.LEFT)
        symbol_box = ttk.Combobox(
            controls,
            textvariable=self.symbol,
            state="readonly",
            width=10,
        )
        symbol_box.pack(side=tk.LEFT, padx=(7, 16))
        symbol_box.bind("<<ComboboxSelected>>", self._symbol_changed)
        self._symbol_box = symbol_box

        ttk.Label(
            controls,
            text="Horizon",
            style="StrategyBody.TLabel",
        ).pack(side=tk.LEFT)
        horizon_box = ttk.Combobox(
            controls,
            textvariable=self.horizon_label,
            state="readonly",
            width=22,
        )
        horizon_box.pack(side=tk.LEFT, padx=(7, 16))
        horizon_box.bind("<<ComboboxSelected>>", self._filters_changed)
        self._horizon_box = horizon_box
        ttk.Label(
            controls,
            textvariable=self.position_summary,
            style="StrategyBody.TLabel",
        ).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(
            controls,
            textvariable=self.candidate_summary,
            style="StrategyMuted.TLabel",
        ).pack(side=tk.RIGHT)

        panes = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True)
        ranking = ttk.Frame(
            panes,
            padding=(10, 10),
            style="StrategySurface.TFrame",
        )
        ticket = ttk.Frame(
            panes,
            padding=(11, 10),
            style="StrategySurface.TFrame",
        )
        panes.add(ranking, weight=3)
        panes.add(ticket, weight=2)
        self._build_ranking(ranking)
        self._build_ticket(ticket)

    def _build_ranking(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="Ranked Candidates",
            style="StrategyHeading.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            parent,
            text=(
                "Direction Up is Loop B's calibrated price forecast; ML Profit "
                "Probability is the fitted strategy-outcome model; Scenario "
                "Coverage is a non-probabilistic fallback. Click any column "
                "heading to sort; click it again to reverse."
            ),
            style="StrategyMuted.TLabel",
        ).pack(anchor=tk.W, pady=(2, 8))
        table_frame = ttk.Frame(parent, style="StrategySurface.TFrame")
        table_frame.pack(fill=tk.BOTH, expand=True)
        table = ttk.Treeview(
            table_frame,
            columns=(
                "rank",
                "strategy",
                "exact_legs",
                "direction_probability_up",
                "calibrated_probability",
                "scenario_coverage",
                "expected_return",
                "portfolio_fit",
                "score_basis",
                "pricing_quality",
            ),
            show="headings",
            height=18,
        )
        for name, label, width, anchor in _CANDIDATE_COLUMNS:
            table.heading(
                name,
                text=label,
                command=lambda column=name: self._sort_candidates(column),
            )
            table.column(
                name,
                width=width,
                minwidth=50,
                anchor=anchor,
                stretch=name in {"strategy", "exact_legs", "pricing_quality"},
            )
        scroll_y = ttk.Scrollbar(
            table_frame,
            orient=tk.VERTICAL,
            command=table.yview,
        )
        scroll_x = ttk.Scrollbar(
            table_frame,
            orient=tk.HORIZONTAL,
            command=table.xview,
        )
        table.configure(
            yscrollcommand=scroll_y.set,
            xscrollcommand=scroll_x.set,
        )
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        scroll_x.pack(side=tk.BOTTOM, fill=tk.X)
        table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        table.bind("<ButtonRelease-1>", self._candidate_clicked)
        table.bind("<Motion>", self._candidate_motion)
        table.bind("<Return>", self._candidate_entered)
        self.candidate_table = table

    def _build_ticket(self, parent: ttk.Frame) -> None:
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        sections = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        sections.grid(row=0, column=0, sticky=tk.NSEW)
        order_section = ttk.Frame(
            sections,
            padding=(0, 0, 0, 6),
            style="StrategySurface.TFrame",
        )
        impact_section = ttk.Frame(
            sections,
            padding=(0, 7, 0, 0),
            style="StrategySurface.TFrame",
        )
        sections.add(order_section, weight=3)
        sections.add(impact_section, weight=2)
        self._ticket_sections = sections

        ttk.Label(
            order_section,
            text="Order Ticket",
            style="StrategyHeading.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            order_section,
            textvariable=self.ticket_strategy,
            style="StrategyBody.TLabel",
        ).pack(anchor=tk.W, pady=(5, 0))
        ttk.Label(
            order_section,
            textvariable=self.ticket_structure,
            style="StrategyMuted.TLabel",
        ).pack(anchor=tk.W, pady=(1, 6))

        fields = ttk.Frame(order_section, style="StrategySurface.TFrame")
        fields.pack(fill=tk.X)
        order_part_box = ttk.Combobox(
            fields,
            textvariable=self.ticket_order_part,
            state="readonly",
        )
        order_part_box.bind(
            "<<ComboboxSelected>>",
            self._order_part_changed,
        )
        self._ticket_field(
            fields,
            "Schwab Order",
            order_part_box,
            row=0,
            column=0,
        )
        self._order_part_box = order_part_box
        self._ticket_field(
            fields,
            "Quantity",
            ttk.Entry(fields, textvariable=self.ticket_quantity),
            row=0,
            column=1,
        )
        order_box = ttk.Combobox(
            fields,
            textvariable=self.ticket_order_method,
            state="readonly",
        )
        order_box.bind(
            "<<ComboboxSelected>>",
            self._order_method_changed,
        )
        self._ticket_field(
            fields,
            "Order Method",
            order_box,
            row=1,
            column=0,
        )
        self._order_method_box = order_box
        limit_price = ttk.Entry(
            fields,
            textvariable=self.ticket_limit_price,
        )
        self._ticket_field(
            fields,
            "Limit Price",
            limit_price,
            row=1,
            column=1,
        )
        self._limit_price_entry = limit_price
        duration_box = ttk.Combobox(
            fields,
            textvariable=self.ticket_duration,
            values=(DAY_ONLY, GOOD_UNTIL_CANCELED),
            state="readonly",
        )
        self._ticket_field(
            fields,
            "Duration",
            duration_box,
            row=0,
            column=2,
        )

        ttk.Label(
            order_section,
            text="Ticket Legs",
            style="StrategyHeading.TLabel",
        ).pack(anchor=tk.W, pady=(8, 4))
        legs = ttk.Treeview(
            order_section,
            columns=("action", "contract", "quantity", "bid", "ask"),
            show="headings",
            height=3,
        )
        for name, label, width, anchor in (
            ("action", "Action", 105, tk.W),
            ("contract", "Contract", 245, tk.W),
            ("quantity", "Quantity", 65, tk.E),
            ("bid", "Bid", 65, tk.E),
            ("ask", "Ask", 65, tk.E),
        ):
            legs.heading(name, text=label)
            legs.column(name, width=width, anchor=anchor)
        legs.pack(fill=tk.BOTH, expand=True)
        self.ticket_legs = legs

        impact_header = ttk.Frame(
            impact_section,
            style="StrategySurface.TFrame",
        )
        impact_header.pack(fill=tk.X, pady=(0, 3))
        ttk.Label(
            impact_header,
            text="Portfolio Impact",
            style="StrategyHeading.TLabel",
        ).pack(side=tk.LEFT, anchor=tk.W)
        ttk.Label(
            impact_header,
            textvariable=self.impact_source,
            style="StrategyMuted.TLabel",
            justify=tk.RIGHT,
            wraplength=260,
        ).pack(side=tk.RIGHT, anchor=tk.E)

        metrics = ttk.Frame(impact_section, style="StrategySurface.TFrame")
        metrics.pack(fill=tk.X)
        for column in range(3):
            metrics.grid_columnconfigure(column, weight=1, uniform="impact")
        self._impact_metric(
            metrics,
            self.impact_available_label,
            self.impact_available,
            row=0,
            column=0,
            dynamic_label=True,
        )
        self._impact_metric(
            metrics,
            "Est. Requirement",
            self.impact_requirement,
            row=0,
            column=1,
        )
        self._impact_funds_after_value = self._impact_metric(
            metrics,
            "Est. Funds After",
            self.impact_funds_after,
            row=0,
            column=2,
        )
        self._impact_metric(
            metrics,
            "Cash Balance",
            self.impact_cash_balance,
            row=1,
            column=0,
        )
        self._impact_metric(
            metrics,
            "Buying Power",
            self.impact_buying_power,
            row=1,
            column=1,
        )
        self._impact_cash_flow_value = self._impact_metric(
            metrics,
            "Opening Cash Flow",
            self.impact_cash_flow,
            row=1,
            column=2,
        )

        impact_context = ttk.Frame(
            impact_section,
            style="StrategySurface.TFrame",
        )
        impact_context.pack(fill=tk.X, pady=(5, 0))
        self._impact_status_label = ttk.Label(
            impact_context,
            textvariable=self.impact_status,
            style="StrategyImpactWarning.TLabel",
        )
        self._impact_status_label.pack(anchor=tk.W)
        ttk.Label(
            impact_context,
            textvariable=self.impact_position,
            style="StrategyMuted.TLabel",
            wraplength=440,
            justify=tk.LEFT,
        ).pack(fill=tk.X, anchor=tk.W, pady=(1, 0))
        ttk.Label(
            impact_section,
            textvariable=self.impact_requirement_basis,
            style="StrategyMuted.TLabel",
            wraplength=440,
            justify=tk.LEFT,
        ).pack(fill=tk.X, anchor=tk.W, pady=(2, 4))

        decision_bar = ttk.Frame(
            impact_section,
            padding=(8, 7),
            style="StrategyImpactMetric.TFrame",
        )
        decision_bar.pack(fill=tk.X, pady=(4, 0))
        decision_bar.grid_columnconfigure(0, weight=1)
        ttk.Label(
            decision_bar,
            textvariable=self.impact_decision_summary,
            style="StrategyImpactMetricLabel.TLabel",
            justify=tk.LEFT,
            wraplength=300,
        ).grid(row=0, column=0, sticky=tk.EW, padx=(0, 8))
        self.decision_details_button = ttk.Button(
            decision_bar,
            text="Decision Details ↗",
            style="StrategyDetails.TButton",
            command=self._open_decision_details,
            state=tk.DISABLED,
        )
        self.decision_details_button.grid(row=0, column=1, sticky=tk.E)

        self.submit_button = ttk.Button(
            parent,
            text="Review Strategy Order",
            style="StrategySubmit.TButton",
            command=self._submit_order,
            state=tk.DISABLED,
        )
        self.submit_button.grid(
            row=1,
            column=0,
            sticky=tk.EW,
            pady=(8, 0),
        )

    def _ticket_field(
        self,
        parent: ttk.Frame,
        label: str,
        control: ttk.Widget,
        *,
        row: int,
        column: int,
    ) -> None:
        parent.grid_columnconfigure(column, weight=1)
        ttk.Label(
            parent,
            text=label,
            style="StrategyMuted.TLabel",
        ).grid(
            row=row * 2,
            column=column,
            sticky=tk.EW,
            padx=(0 if column == 0 else 5, 5 if column == 0 else 0),
            pady=(0 if row == 0 else 2, 0),
        )
        control.grid(
            row=row * 2 + 1,
            column=column,
            sticky=tk.EW,
            padx=(0 if column == 0 else 5, 5 if column == 0 else 0),
            pady=(3, 7),
        )

    @staticmethod
    def _impact_metric(
        parent: ttk.Frame,
        label: str | tk.StringVar,
        value: tk.StringVar,
        *,
        row: int,
        column: int,
        dynamic_label: bool = False,
    ) -> ttk.Label:
        card = ttk.Frame(
            parent,
            padding=(7, 5),
            style="StrategyImpactMetric.TFrame",
        )
        card.grid(
            row=row,
            column=column,
            sticky=tk.NSEW,
            padx=(0 if column == 0 else 3, 0),
            pady=(0 if row == 0 else 3, 0),
        )
        label_options: dict[str, object] = (
            {"textvariable": label}
            if dynamic_label
            else {"text": str(label)}
        )
        ttk.Label(
            card,
            style="StrategyImpactMetricLabel.TLabel",
            **label_options,
        ).pack(anchor=tk.W)
        value_label = ttk.Label(
            card,
            textvariable=value,
            style="StrategyImpactMetricValue.TLabel",
        )
        value_label.pack(anchor=tk.W, pady=(1, 0))
        return value_label

    def _open_decision_details(self) -> None:
        if self.selected_candidate is None:
            return
        window = self._decision_details_window
        if window is not None:
            try:
                window.deiconify()
                window.lift()
                window.focus_set()
                self._refresh_decision_details()
                return
            except tk.TclError:
                self._decision_details_window = None

        window = tk.Toplevel(self.root)
        self._decision_details_window = window
        window.title("Strategy Decision Details")
        self.root.update_idletasks()
        width = 980
        height = 720
        x = self.root.winfo_rootx() + max(
            16,
            (self.root.winfo_width() - width) // 2,
        )
        y = self.root.winfo_rooty() + max(
            16,
            (self.root.winfo_height() - height) // 2,
        )
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.minsize(740, 520)
        window.configure(background=BACKGROUND)
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", self._close_decision_details)

        outer = ttk.Frame(
            window,
            padding=(16, 14),
            style="StrategyPage.TFrame",
        )
        outer.grid(row=0, column=0, sticky=tk.NSEW)
        window.grid_rowconfigure(0, weight=1)
        window.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(1, weight=1)
        outer.grid_columnconfigure(0, weight=1)

        header = ttk.Frame(outer, style="StrategyPage.TFrame")
        header.grid(row=0, column=0, sticky=tk.EW, pady=(0, 10))
        header.grid_columnconfigure(0, weight=1)
        ttk.Label(
            header,
            text="Strategy Decision Details",
            style="StrategyTitle.TLabel",
        ).grid(row=0, column=0, sticky=tk.W)
        ttk.Label(
            header,
            textvariable=self.ticket_strategy,
            style="StrategySubtitle.TLabel",
        ).grid(row=1, column=0, sticky=tk.W, pady=(2, 0))
        ttk.Label(
            header,
            textvariable=self.ticket_structure,
            style="StrategySubtitle.TLabel",
        ).grid(row=2, column=0, sticky=tk.W)
        ttk.Label(
            header,
            textvariable=self.impact_source,
            style="StrategySubtitle.TLabel",
            justify=tk.RIGHT,
        ).grid(row=0, column=1, rowspan=3, sticky=tk.NE, padx=(18, 0))

        details_panes = tk.PanedWindow(
            outer,
            orient=tk.VERTICAL,
            background=BORDER,
            borderwidth=0,
            sashwidth=6,
            sashrelief=tk.FLAT,
            showhandle=True,
            handlesize=8,
            handlepad=8,
            opaqueresize=True,
        )
        details_panes.grid(row=1, column=0, sticky=tk.NSEW)

        summary = ttk.Frame(
            details_panes,
            padding=(14, 12),
            style="StrategySurface.TFrame",
        )
        evidence_panel = ttk.Frame(
            details_panes,
            padding=(14, 12),
            style="StrategySurface.TFrame",
        )
        details_panes.add(summary, minsize=205, stretch="always")
        details_panes.add(evidence_panel, minsize=245, stretch="always")

        summary_header = ttk.Frame(summary, style="StrategySurface.TFrame")
        summary_header.pack(fill=tk.X, pady=(0, 7))
        ttk.Label(
            summary_header,
            text="Portfolio Overview",
            style="StrategyHeading.TLabel",
        ).pack(side=tk.LEFT)
        self._decision_status_label = ttk.Label(
            summary_header,
            textvariable=self.impact_status,
            style="StrategyImpactWarning.TLabel",
        )
        self._decision_status_label.pack(side=tk.RIGHT)

        metrics = ttk.Frame(summary, style="StrategySurface.TFrame")
        metrics.pack(fill=tk.X)
        for column in range(3):
            metrics.grid_columnconfigure(column, weight=1, uniform="details-impact")
        self._impact_metric(
            metrics,
            self.impact_available_label,
            self.impact_available,
            row=0,
            column=0,
            dynamic_label=True,
        )
        self._impact_metric(
            metrics,
            "Est. Requirement",
            self.impact_requirement,
            row=0,
            column=1,
        )
        self._decision_funds_after_value = self._impact_metric(
            metrics,
            "Est. Funds After",
            self.impact_funds_after,
            row=0,
            column=2,
        )
        self._impact_metric(
            metrics,
            "Cash Balance",
            self.impact_cash_balance,
            row=1,
            column=0,
        )
        self._impact_metric(
            metrics,
            "Buying Power",
            self.impact_buying_power,
            row=1,
            column=1,
        )
        self._decision_cash_flow_value = self._impact_metric(
            metrics,
            "Opening Cash Flow",
            self.impact_cash_flow,
            row=1,
            column=2,
        )
        ttk.Label(
            summary,
            textvariable=self.impact_position,
            style="StrategyBody.TLabel",
            justify=tk.LEFT,
            wraplength=850,
        ).pack(fill=tk.X, anchor=tk.W, pady=(8, 1))
        ttk.Label(
            summary,
            textvariable=self.impact_requirement_basis,
            style="StrategyMuted.TLabel",
            justify=tk.LEFT,
            wraplength=850,
        ).pack(fill=tk.X, anchor=tk.W)

        evidence_header = ttk.Frame(
            evidence_panel,
            style="StrategySurface.TFrame",
        )
        evidence_header.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(
            evidence_header,
            text="Decision Support",
            style="StrategyHeading.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Label(
            evidence_header,
            text="Drag either divider or resize the window to suit the content",
            style="StrategyMuted.TLabel",
        ).pack(side=tk.RIGHT)

        evidence_panes = tk.PanedWindow(
            evidence_panel,
            orient=tk.HORIZONTAL,
            background=BORDER,
            borderwidth=0,
            sashwidth=6,
            sashrelief=tk.FLAT,
            showhandle=True,
            handlesize=8,
            handlepad=8,
            opaqueresize=True,
        )
        evidence_panes.pack(fill=tk.BOTH, expand=True)
        pricing_panel = ttk.Frame(
            evidence_panes,
            padding=(10, 9),
            style="StrategyImpactMetric.TFrame",
        )
        assurance_panel = ttk.Frame(
            evidence_panes,
            padding=(10, 9),
            style="StrategyImpactMetric.TFrame",
        )
        evidence_panes.add(pricing_panel, minsize=300, stretch="always")
        evidence_panes.add(assurance_panel, minsize=300, stretch="always")
        ttk.Label(
            pricing_panel,
            text="Quote Snapshot",
            style="StrategyImpactMetricValue.TLabel",
        ).pack(anchor=tk.W, pady=(0, 6))
        ttk.Label(
            assurance_panel,
            text="Signal & Execution",
            style="StrategyImpactMetricValue.TLabel",
        ).pack(anchor=tk.W, pady=(0, 6))
        self._decision_pricing_text = self._decision_text(pricing_panel)
        self._decision_assurance_text = self._decision_text(assurance_panel)

        footer = ttk.Frame(outer, style="StrategyPage.TFrame")
        footer.grid(row=2, column=0, sticky=tk.EW, pady=(10, 0))
        ttk.Label(
            footer,
            text="Ticket edits update this view automatically.",
            style="StrategySubtitle.TLabel",
        ).pack(side=tk.LEFT)
        ttk.Button(
            footer,
            text="Close",
            style="StrategyDetails.TButton",
            command=self._close_decision_details,
        ).pack(side=tk.RIGHT)
        self._refresh_decision_details()

    def _close_decision_details(self) -> None:
        window = self._decision_details_window
        self._decision_details_window = None
        self._decision_status_label = None
        self._decision_funds_after_value = None
        self._decision_cash_flow_value = None
        self._decision_pricing_text = None
        self._decision_assurance_text = None
        if window is not None:
            try:
                window.destroy()
            except tk.TclError:
                pass

    @staticmethod
    def _decision_text(parent: ttk.Frame) -> tk.Text:
        body = ttk.Frame(parent, style="StrategyImpactMetric.TFrame")
        body.pack(fill=tk.BOTH, expand=True)
        text = tk.Text(
            body,
            background=SURFACE_ALT,
            foreground=TEXT,
            selectbackground=ACCENT,
            selectforeground="#ffffff",
            borderwidth=0,
            highlightthickness=0,
            padx=5,
            pady=3,
            wrap=tk.WORD,
            font=("Segoe UI", 10),
            cursor="arrow",
        )
        scrollbar = ttk.Scrollbar(body, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        text.tag_configure(
            "eyebrow",
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8, "bold"),
            spacing1=5,
            spacing3=3,
        )
        text.tag_configure(
            "heading",
            foreground=TEXT,
            font=("Segoe UI", 12, "bold"),
            spacing3=8,
        )
        text.tag_configure(
            "body",
            foreground=TEXT,
            font=("Segoe UI", 10),
            spacing3=4,
        )
        text.tag_configure(
            "muted",
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
            spacing3=5,
        )
        text.tag_configure(
            "good",
            foreground=SUCCESS,
            font=("Segoe UI", 11, "bold"),
            spacing3=7,
        )
        text.tag_configure(
            "warning",
            foreground=WARNING,
            font=("Segoe UI", 11, "bold"),
            spacing3=7,
        )
        text.tag_configure(
            "danger",
            foreground=DANGER,
            font=("Segoe UI", 11, "bold"),
            spacing3=7,
        )
        text.tag_configure(
            "bullet",
            foreground=TEXT,
            font=("Segoe UI", 9),
            lmargin1=8,
            lmargin2=22,
            spacing3=4,
        )
        text.tag_configure(
            "check_good",
            foreground=SUCCESS,
            font=("Segoe UI", 9, "bold"),
            lmargin1=8,
            lmargin2=22,
            spacing3=3,
        )
        text.tag_configure(
            "check_warning",
            foreground=WARNING,
            font=("Segoe UI", 9, "bold"),
            lmargin1=8,
            lmargin2=22,
            spacing3=3,
        )
        text.configure(state=tk.DISABLED)
        return text

    def _refresh_decision_details(self) -> None:
        window = getattr(self, "_decision_details_window", None)
        pricing_text = getattr(self, "_decision_pricing_text", None)
        assurance_text = getattr(self, "_decision_assurance_text", None)
        candidate = getattr(self, "selected_candidate", None)
        if (
            window is None
            or pricing_text is None
            or assurance_text is None
        ):
            return
        if candidate is None:
            window.title("Strategy Decision Details")
            placeholder = [
                ("NO STRATEGY SELECTED\n", "eyebrow"),
                (
                    "Choose a ranked candidate to populate this view.\n",
                    "muted",
                ),
            ]
            self._replace_decision_text(pricing_text, placeholder)
            self._replace_decision_text(assurance_text, placeholder)
            self._sync_decision_detail_tones()
            return
        evidence = _decision_evidence(
            candidate,
            order_index=self.selected_order_index,
        )
        window.title(f"{candidate.strategy_display_name} — Decision Details")
        quotes_complete = bool(
            evidence.quote_legs_total
            and evidence.quote_legs_available == evidence.quote_legs_total
        )
        quote_tone = "good" if quotes_complete else "warning"
        pricing_chunks: list[tuple[str, str]] = [
            ("SNAPSHOT QUOTES\n", "eyebrow"),
            (
                f"{evidence.quote_legs_available} / "
                f"{evidence.quote_legs_total} ticket legs populated\n",
                quote_tone,
            ),
            ("CANDIDATE SNAPSHOT\n", "eyebrow"),
            (f"{evidence.candidate_snapshot}\n", "heading"),
            ("ORDER INPUT\n", "eyebrow"),
            (
                _ticket_order_input(
                    self.ticket_quantity.get(),
                    self.ticket_order_method.get(),
                    self.ticket_limit_price.get(),
                )
                + "\n",
                "heading",
            ),
            ("LIVE SCHWAB REFRESH\n", "eyebrow"),
            (
                "Review Strategy Order requests current exact-leg quotes and "
                "current account facts before placement.\n",
                "body",
            ),
            ("PUBLICATION NOTES\n", "eyebrow"),
        ]
        if evidence.publication_notices:
            pricing_chunks.extend(
                (f"• {_publication_notice_display(notice)}\n", "bullet")
                for notice in evidence.publication_notices
            )
        else:
            pricing_chunks.append(
                ("No model-overlay exceptions were reported.\n", "muted")
            )
        self._replace_decision_text(pricing_text, pricing_chunks)

        assurance_chunks: list[tuple[str, str]] = [
            ("FORECAST SIGNAL\n", "eyebrow"),
            (f"{evidence.model}\n", "heading"),
            (
                "• Direction Up (ML): "
                f"{_percentage_points(evidence.direction_probability_up)}   "
                "• ML Profit: "
                f"{_percentage_points(evidence.predictive_score)}\n",
                "bullet",
            ),
            (
                "• Scenario Coverage: "
                f"{_percentage_points(evidence.scenario_coverage)}   "
                f"• Expected Return: {_percent(evidence.expected_return)}\n",
                "bullet",
            ),
            ("PUBLICATION CHECKS\n", "eyebrow"),
        ]
        assurance_chunks.extend(
            (
                (
                    f"✓ {label} — Passed\n"
                    if passed
                    else f"• {label} — Not passed in snapshot\n"
                ),
                "check_good" if passed else "check_warning",
            )
            for label, passed in evidence.publication_checks
        )
        assurance_chunks.extend(
            (
                ("EXECUTION PATH\n", "eyebrow"),
                (f"{evidence.execution_status}\n", "good"),
                (f"{evidence.execution_detail}\n", "body"),
            )
        )
        self._replace_decision_text(assurance_text, assurance_chunks)
        self._sync_decision_detail_tones()

    @staticmethod
    def _replace_decision_text(
        widget: tk.Text,
        chunks: Iterable[tuple[str, str]],
    ) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        for value, tag in chunks:
            widget.insert(tk.END, value, tag)
        widget.configure(state=tk.DISABLED)
        widget.yview_moveto(0.0)

    def _sync_decision_detail_tones(self) -> None:
        status_label = getattr(self, "_decision_status_label", None)
        if status_label is not None:
            status_label.configure(
                style=_impact_status_style(self.impact_status.get())
            )
        funds_after = getattr(self, "_decision_funds_after_value", None)
        if funds_after is not None:
            funds_after.configure(
                style=_money_metric_style(
                    self.impact_funds_after.get(),
                    positive_is_good=True,
                )
            )
        cash_flow = getattr(self, "_decision_cash_flow_value", None)
        if cash_flow is not None:
            cash_flow.configure(
                style=_cash_flow_metric_style(self.impact_cash_flow.get())
            )

    def refresh(self) -> None:
        if self.past_positions_view is not None:
            self.past_positions_view.refresh()
        if self.refresh_button is not None:
            self.refresh_button.configure(state=tk.DISABLED)
        self.position_summary.set("Syncing Schwab Account")
        self.candidate_summary.set("Loading Strategy Candidates")
        run_in_background(
            self.root,
            self._load_data,
            self._load_succeeded,
            self._load_failed,
        )

    def _load_data(self) -> tuple[PortfolioSnapshot, StrategyCandidatesView]:
        snapshot = self.snapshot_loader()
        view = load_strategy_candidates(
            self.candidates_path,
            snapshot=snapshot,
        )
        return snapshot, view

    def _load_succeeded(
        self,
        loaded: tuple[PortfolioSnapshot, StrategyCandidatesView],
    ) -> None:
        snapshot, view = loaded
        self.snapshot = snapshot
        self.view = view
        if self.refresh_button is not None:
            self.refresh_button.configure(state=tk.NORMAL)
        if self.management_view is not None:
            self.management_view.show_snapshot(snapshot)
        self._symbol_box.configure(values=view.symbols)
        if not view.symbols:
            self.symbol.set("")
            self.horizon_label.set("")
            self.position_summary.set(
                _empty_candidate_message(view.empty_diagnosis)
            )
            self.candidate_summary.set("0 Candidates")
            self._render_candidates()
            return
        if self.symbol.get() not in view.symbols:
            self.symbol.set(view.symbols[0])
        self._set_horizon_choices()
        self._render_candidates()

    def _load_failed(self, exc: Exception) -> None:
        if self.refresh_button is not None:
            self.refresh_button.configure(state=tk.NORMAL)
        if self.management_view is not None:
            self.management_view.show_refresh_error(exc)
        if self.view is None:
            self.visible_candidates = ()
            self.position_summary.set("Options Strategy Data Could Not Be Loaded")
            self.candidate_summary.set("")
            self._clear_table(self.candidate_table)
            self._clear_ticket()
        else:
            self.position_summary.set("Refresh Failed; Showing Prior Candidates")
        messagebox.showerror(
            "Options Strategy Refresh Failed",
            str(exc) or "Options strategy data could not be loaded.",
        )

    def _symbol_changed(self, _event: object = None) -> None:
        self._set_horizon_choices()
        self._render_candidates()

    def _set_horizon_choices(self) -> None:
        if self.view is None:
            return
        horizons = self.view.horizons_by_symbol.get(self.symbol.get(), ())
        labels = tuple(HORIZON_LABELS[horizon] for horizon in horizons)
        self._horizon_box.configure(values=labels)
        if self.horizon_label.get() not in labels:
            preferred = HORIZON_LABELS["1d"]
            self.horizon_label.set(
                preferred if preferred in labels else (labels[0] if labels else "")
            )

    def _filters_changed(self, _event: object = None) -> None:
        self._render_candidates()

    def _render_candidates(self) -> None:
        self._clear_table(self.candidate_table)
        self._clear_ticket()
        if self.view is None or self.candidate_table is None:
            return
        horizon = next(
            (
                key
                for key, label in HORIZON_LABELS.items()
                if label == self.horizon_label.get()
            ),
            "",
        )
        visible_candidates = tuple(
            item
            for item in self.view.candidates
            if item.symbol == self.symbol.get() and item.horizon == horizon
        )
        sort_column = getattr(self, "_candidate_sort_column", None)
        if sort_column is not None:
            visible_candidates = _sort_candidate_views(
                visible_candidates,
                column=sort_column,
                descending=getattr(
                    self,
                    "_candidate_sort_descending",
                    False,
                ),
            )
        self.visible_candidates = visible_candidates
        for index, candidate in enumerate(self.visible_candidates):
            self.candidate_table.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    candidate.rank,
                    candidate.strategy_display_name,
                    candidate.exact_legs,
                    _percentage_points(candidate.direction_probability_up),
                    _percentage_points(candidate.predictive_score),
                    _percentage_points(candidate.scenario_coverage),
                    _percent(candidate.expected_return),
                    candidate.portfolio_fit.label,
                    candidate.score_basis,
                    _pricing_quality_summary(candidate),
                ),
            )
        validation_required = sum(
            not candidate.manual_order_actionable
            for candidate in self.visible_candidates
        )
        summary = f"{len(self.visible_candidates):,} Candidates"
        if validation_required:
            summary += f" · {validation_required:,} Need Schwab Validation"
        self.candidate_summary.set(summary)
        if self.visible_candidates:
            position = self.visible_candidates[0].position
            self.position_summary.set(_position_summary(position))
        else:
            diagnosis = self.view.route_diagnoses.get(
                (self.symbol.get(), horizon)
            )
            if not self.symbol.get() or not horizon:
                diagnosis = diagnosis or self.view.empty_diagnosis
            self.position_summary.set(
                _empty_candidate_message(diagnosis)
            )

    def _sort_candidates(self, column: str) -> None:
        if column not in _CANDIDATE_COLUMN_LABELS:
            return
        current_column = getattr(self, "_candidate_sort_column", None)
        if current_column == column:
            descending = not getattr(
                self,
                "_candidate_sort_descending",
                False,
            )
        else:
            descending = column in _DESCENDING_FIRST_CANDIDATE_COLUMNS
        self._candidate_sort_column = column
        self._candidate_sort_descending = descending
        self._update_candidate_headings()
        self._render_candidates()

    def _update_candidate_headings(self) -> None:
        if self.candidate_table is None:
            return
        active_column = getattr(self, "_candidate_sort_column", None)
        descending = getattr(self, "_candidate_sort_descending", False)
        for name, label, _width, _anchor in _CANDIDATE_COLUMNS:
            indicator = ""
            if name == active_column:
                indicator = " ↓" if descending else " ↑"
            self.candidate_table.heading(name, text=f"{label}{indicator}")

    def _candidate_clicked(self, event: tk.Event[tk.Misc]) -> None:
        if self.candidate_table is None:
            return
        row_id = self.candidate_table.identify_row(event.y)
        column = self.candidate_table.identify_column(event.x)
        if row_id and column == "#3":
            self.candidate_table.selection_set(row_id)
            self._fill_ticket(int(row_id))

    def _candidate_motion(self, event: tk.Event[tk.Misc]) -> None:
        if self.candidate_table is None:
            return
        column = self.candidate_table.identify_column(event.x)
        row_id = self.candidate_table.identify_row(event.y)
        region = self.candidate_table.identify_region(event.x, event.y)
        self.candidate_table.configure(
            cursor=(
                "hand2"
                if region == "heading" or (column == "#3" and row_id)
                else ""
            )
        )

    def _candidate_entered(self, _event: object) -> None:
        if self.candidate_table is None:
            return
        selected = self.candidate_table.selection()
        if selected:
            self._fill_ticket(int(selected[0]))

    def _fill_ticket(self, index: int) -> None:
        if not 0 <= index < len(self.visible_candidates):
            return
        candidate = self.visible_candidates[index]
        draft = candidate.order_draft
        self.selected_candidate = candidate
        self.selected_order_index = 0
        self.ticket_strategy.set(candidate.strategy_display_name)
        order_word = "Order" if draft.order_count == 1 else "Orders"
        leg_word = "Leg" if len(draft.legs) == 1 else "Legs"
        self.ticket_structure.set(
            f"{draft.order_count} Schwab {order_word} · "
            f"{len(draft.legs)} Strategy {leg_word}"
        )
        self.ticket_quantity.set("1")
        order_names = tuple(order.display_name for order in draft.orders)
        self._order_part_box.configure(values=order_names)
        self.ticket_order_part.set(order_names[0])
        self._render_order_part()
        self._update_portfolio_impact()
        if self.submit_button is not None:
            self.submit_button.configure(
                state=tk.NORMAL,
                text="Review Strategy Order",
            )
        if self.decision_details_button is not None:
            self.decision_details_button.configure(state=tk.NORMAL)

    def _order_part_changed(self, _event: object = None) -> None:
        candidate = self.selected_candidate
        if candidate is None:
            return
        selected = self.ticket_order_part.get()
        self.selected_order_index = next(
            (
                index
                for index, order in enumerate(candidate.order_draft.orders)
                if order.display_name == selected
            ),
            0,
        )
        self._render_order_part()

    def _render_order_part(self) -> None:
        candidate = self.selected_candidate
        if candidate is None:
            return
        order = candidate.order_draft.orders[self.selected_order_index]
        self.ticket_order_method.set(order.suggested_order_method)
        self._order_method_box.configure(values=order.order_method_choices)
        self.ticket_limit_price.set(
            ""
            if order.suggested_limit_price is None
            else f"{order.suggested_limit_price:.2f}"
        )
        self.ticket_duration.set(order.duration)
        self._order_method_changed()
        self._clear_table(self.ticket_legs)
        if self.ticket_legs is not None:
            for leg in order.legs:
                self.ticket_legs.insert(
                    "",
                    tk.END,
                    values=(
                        _human_instruction(leg.instruction),
                        leg.display_name,
                        leg.quantity,
                        _money(leg.bid),
                        _money(leg.ask),
                    ),
                )

    def _order_method_changed(self, _event: object = None) -> None:
        self._limit_price_entry.configure(
            state=(
                tk.DISABLED
                if self.ticket_order_method.get() == MARKET_ORDER
                else tk.NORMAL
            )
        )

    def _ticket_impact_changed(self, *_args: object) -> None:
        self._update_portfolio_impact()

    def _update_portfolio_impact(self) -> None:
        candidate = self.selected_candidate
        snapshot = self.snapshot
        if candidate is None or snapshot is None:
            self._reset_portfolio_impact()
            return
        self._set_decision_evidence(candidate)
        self.impact_source.set(
            _impact_source_text(snapshot.account_label, snapshot.synced_at)
        )
        try:
            impact = calculate_strategy_portfolio_impact(
                candidate.row,
                order_draft=candidate.order_draft,
                position=candidate.position,
                order_index=self.selected_order_index,
                strategy_quantity=self.ticket_quantity.get().strip(),
                order_method=self.ticket_order_method.get(),
                limit_price=(
                    None
                    if self.ticket_order_method.get() == MARKET_ORDER
                    else self.ticket_limit_price.get().strip()
                ),
                account_label=snapshot.account_label,
            )
        except (TypeError, ValueError) as exc:
            self.impact_status.set("Complete Ticket Details")
            self._impact_status_label.configure(
                style="StrategyImpactWarning.TLabel"
            )
            self.impact_available_label.set("Applicable Funds")
            for variable in (
                self.impact_available,
                self.impact_requirement,
                self.impact_funds_after,
                self.impact_cash_balance,
                self.impact_buying_power,
                self.impact_cash_flow,
            ):
                variable.set("—")
            self.impact_requirement_basis.set(str(exc))
            self.impact_position.set(_position_impact_text(candidate.position))
            self._impact_funds_after_value.configure(
                style="StrategyImpactMetricValue.TLabel"
            )
            self._impact_cash_flow_value.configure(
                style="StrategyImpactMetricValue.TLabel"
            )
            self._refresh_decision_details()
            return
        self._render_portfolio_impact(candidate, impact)

    def _render_portfolio_impact(
        self,
        candidate: StrategyCandidateView,
        impact: StrategyPortfolioImpact,
    ) -> None:
        self.impact_available_label.set(impact.applicable_funds_label)
        self.impact_available.set(_money_or_dash(impact.applicable_funds))
        self.impact_requirement.set(
            _money_or_dash(impact.estimated_funds_required)
        )
        self.impact_funds_after.set(
            _money_or_dash(impact.funds_after_estimate)
        )
        self.impact_cash_balance.set(_money_or_dash(impact.cash_balance))
        self.impact_buying_power.set(_money_or_dash(impact.buying_power))
        self.impact_cash_flow.set(
            _cash_flow_text(impact.estimated_opening_cash_flow)
        )

        if impact.has_share_shortfall:
            status = "Share Shortfall"
            status_style = "StrategyImpactDanger.TLabel"
        elif impact.has_funds_shortfall:
            status = "Funds Below Estimate"
            status_style = "StrategyImpactDanger.TLabel"
        elif impact.applicable_funds is None:
            status = "Applicable Balance Unavailable"
            status_style = "StrategyImpactWarning.TLabel"
        elif impact.estimated_funds_required is None:
            status = "Broker Estimate Required"
            status_style = "StrategyImpactWarning.TLabel"
        else:
            status = candidate.portfolio_fit.label
            status_style = "StrategyImpactGood.TLabel"
        self.impact_status.set(status)
        self._impact_status_label.configure(style=status_style)
        self._impact_funds_after_value.configure(
            style=(
                "StrategyImpactMetricDanger.TLabel"
                if impact.has_funds_shortfall
                else "StrategyImpactMetricGood.TLabel"
                if impact.funds_after_estimate is not None
                else "StrategyImpactMetricValue.TLabel"
            )
        )
        self._impact_cash_flow_value.configure(
            style=(
                "StrategyImpactMetricGood.TLabel"
                if impact.estimated_opening_cash_flow is not None
                and impact.estimated_opening_cash_flow > 0.0
                else "StrategyImpactMetricDanger.TLabel"
                if impact.estimated_opening_cash_flow is not None
                and impact.estimated_opening_cash_flow < 0.0
                else "StrategyImpactMetricValue.TLabel"
            )
        )

        coverage = (
            ""
            if impact.coverage_ratio is None
            else f" • {impact.coverage_ratio:.2f}× coverage"
        )
        self.impact_requirement_basis.set(
            "Local estimate: "
            f"{impact.requirement_basis}{coverage} • "
            f"{impact.opening_cash_flow_basis} • Schwab review is authoritative"
        )
        self.impact_position.set(_position_impact_text_from_impact(impact))
        self._refresh_decision_details()

    def _set_decision_evidence(self, candidate: StrategyCandidateView) -> None:
        self.impact_decision_summary.set(
            _inline_decision_summary(
                candidate,
                order_index=self.selected_order_index,
            )
        )
        self._refresh_decision_details()

    def _reset_portfolio_impact(self) -> None:
        self.impact_source.set("Select exact legs to calculate impact")
        self.impact_status.set("No Strategy Selected")
        self.impact_available_label.set("Applicable Funds")
        for variable in (
            self.impact_available,
            self.impact_requirement,
            self.impact_funds_after,
            self.impact_cash_balance,
            self.impact_buying_power,
            self.impact_cash_flow,
        ):
            variable.set("—")
        self.impact_requirement_basis.set("")
        self.impact_position.set("")
        self.impact_decision_summary.set(
            "Select a strategy to inspect its decision evidence"
        )
        self._impact_status_label.configure(
            style="StrategyImpactWarning.TLabel"
        )
        self._impact_funds_after_value.configure(
            style="StrategyImpactMetricValue.TLabel"
        )
        self._impact_cash_flow_value.configure(
            style="StrategyImpactMetricValue.TLabel"
        )
        self._refresh_decision_details()

    def _clear_ticket(self) -> None:
        self.selected_candidate = None
        self.selected_order_index = 0
        self.ticket_strategy.set("Select Exact Legs")
        self.ticket_structure.set("")
        self.ticket_order_part.set("")
        self._order_part_box.configure(values=())
        self.ticket_quantity.set("1")
        self.ticket_order_method.set("")
        self.ticket_limit_price.set("")
        self.ticket_duration.set(DAY_ONLY)
        self._reset_portfolio_impact()
        self._limit_price_entry.configure(state=tk.NORMAL)
        self._clear_table(self.ticket_legs)
        if self.submit_button is not None:
            self.submit_button.configure(
                state=tk.DISABLED,
                text="Review Strategy Order",
            )
        if self.decision_details_button is not None:
            self.decision_details_button.configure(state=tk.DISABLED)

    def _submit_order(self) -> None:
        candidate = self.selected_candidate
        if candidate is None:
            return
        snapshot = self.snapshot
        if snapshot is None:
            messagebox.showerror(
                "Strategy Order Review Unavailable",
                "Refresh the Schwab account before reviewing an entry order.",
            )
            return
        try:
            portfolio_impact = calculate_strategy_portfolio_impact(
                candidate.row,
                order_draft=candidate.order_draft,
                position=candidate.position,
                order_index=self.selected_order_index,
                strategy_quantity=self.ticket_quantity.get().strip(),
                order_method=self.ticket_order_method.get(),
                limit_price=(
                    None
                    if self.ticket_order_method.get() == MARKET_ORDER
                    else self.ticket_limit_price.get().strip()
                ),
                account_label=snapshot.account_label,
            )
            draft = build_strategy_entry_review_draft(
                candidate_row=candidate.row,
                order_draft=candidate.order_draft,
                order_index=self.selected_order_index,
                strategy_quantity=int(self.ticket_quantity.get().strip()),
                order_method=self.ticket_order_method.get(),
                limit_price=(
                    None
                    if self.ticket_order_method.get() == MARKET_ORDER
                    else self.ticket_limit_price.get().strip()
                ),
                duration=self.ticket_duration.get(),
                account_label=snapshot.account_label,
                reviewed_account_at=snapshot.synced_at,
                available_cash=candidate.position.available_cash,
                working_option_orders=candidate.position.working_option_orders,
                research_only=not candidate.manual_order_actionable,
                research_reason=candidate.manual_actionability,
                portfolio_detail=candidate.portfolio_fit.detail,
                model_summary=candidate.model_summary,
                pricing_summary=candidate.pricing_summary,
                quality_warning=candidate.quality_warning,
                portfolio_impact=portfolio_impact,
            )
            controller = StrategyOrderReviewController(
                draft=draft,
                refresher=self._refresh_strategy_entry_review,
                session_factory=self.session_factory,
                on_accepted=lambda accepted: self.root.after(
                    0,
                    lambda: self._strategy_entry_accepted(accepted),
                ),
                on_unknown=lambda: self.root.after(0, self.refresh),
            )
            OptionOrderReviewDialog(root=self.root, controller=controller)
        except Exception as exc:
            messagebox.showerror(
                "Strategy Order Review Unavailable",
                str(exc) or "The strategy order could not be reviewed.",
            )

    def _refresh_strategy_entry_review(
        self,
        draft: StrategyEntryOrderReviewDraft,
    ) -> StrategyEntryOrderReviewDraft:
        return refresh_strategy_entry_review_draft(
            draft,
            snapshot=self.snapshot_loader(),
            session=self.session_factory(),
        )

    def _strategy_entry_accepted(
        self,
        accepted: StrategyEntryOrderReviewDraft,
    ) -> None:
        candidate = self.selected_candidate
        if (
            candidate is not None
            and candidate.candidate_id == accepted.order_draft.candidate_id
            and accepted.order_index + 1 < candidate.order_draft.order_count
        ):
            self.selected_order_index = accepted.order_index + 1
            next_order = candidate.order_draft.orders[self.selected_order_index]
            self.ticket_order_part.set(next_order.display_name)
            self._render_order_part()
            return
        self.refresh()

    @staticmethod
    def _clear_table(table: ttk.Treeview | None) -> None:
        if table is None:
            return
        for item in table.get_children():
            table.delete(item)


def _sort_candidate_views(
    candidates: Iterable[StrategyCandidateView],
    *,
    column: str,
    descending: bool,
) -> tuple[StrategyCandidateView, ...]:
    """Sort candidates by their typed values while keeping blanks last."""
    if column not in _CANDIDATE_COLUMN_LABELS:
        return tuple(candidates)
    populated: list[tuple[float | int | str, StrategyCandidateView]] = []
    missing: list[StrategyCandidateView] = []
    for candidate in candidates:
        value = _candidate_sort_value(candidate, column)
        if value is None or (
            isinstance(value, float) and math.isnan(value)
        ):
            missing.append(candidate)
            continue
        normalized = value.casefold() if isinstance(value, str) else value
        populated.append((normalized, candidate))
    populated.sort(key=lambda item: item[0], reverse=descending)
    return tuple(candidate for _value, candidate in populated) + tuple(missing)


def _candidate_sort_value(
    candidate: StrategyCandidateView,
    column: str,
) -> float | int | str | None:
    if column == "portfolio_fit":
        return candidate.portfolio_fit.label
    if column == "pricing_quality":
        return _pricing_quality_summary(candidate)
    attribute = {
        "rank": "rank",
        "strategy": "strategy_display_name",
        "exact_legs": "exact_legs",
        "direction_probability_up": "direction_probability_up",
        "calibrated_probability": "predictive_score",
        "scenario_coverage": "scenario_coverage",
        "expected_return": "expected_return",
        "score_basis": "score_basis",
    }.get(column)
    if attribute is None:
        return None
    return getattr(candidate, attribute)


def _pricing_quality_summary(candidate: StrategyCandidateView) -> str:
    return (
        f"{candidate.model_summary} · {candidate.pricing_summary} "
        f"· {candidate.quality_warning}"
    )


def _position_summary(position: object) -> str:
    shares = float(getattr(position, "shares", 0.0))
    options = float(getattr(position, "option_contracts", 0.0))
    orders = int(getattr(position, "working_option_orders", 0))
    symbol = str(getattr(position, "symbol", ""))
    parts = [f"{symbol} position: {shares:g} shares"]
    if options:
        parts.append(f"{options:g} option contracts")
    if orders:
        parts.append(
            f"{orders} working option order" + ("s" if orders != 1 else "")
        )
    return " · ".join(parts)


def _impact_source_text(
    account_label: str,
    observed_at: object,
) -> str:
    label = str(account_label or "Schwab").strip()
    source = "Schwab Duckets" if label.casefold() == "schwab" else label
    if not hasattr(observed_at, "astimezone"):
        return f"{source} snapshot"
    local = observed_at.astimezone()  # type: ignore[union-attr]
    timestamp = local.strftime("%b %d, %I:%M %p").replace(" 0", " ")
    return f"{source} • Synced {timestamp}"


def _position_impact_text(position: object) -> str:
    symbol = str(getattr(position, "symbol", "")).strip().upper()
    shares = float(getattr(position, "shares", 0.0))
    options = float(getattr(position, "option_contracts", 0.0))
    orders = int(getattr(position, "working_option_orders", 0))
    return (
        f"{symbol}: {shares:g} shares • {options:g} option contracts • "
        f"{orders} working option order{'s' if orders != 1 else ''}"
    )


def _position_impact_text_from_impact(
    impact: StrategyPortfolioImpact,
) -> str:
    if impact.shares_required > 0.0:
        share_text = (
            f"{impact.shares_held:g} held / {impact.shares_required:g} required"
        )
    else:
        share_text = f"{impact.shares_held:g} shares held"
    return (
        f"{share_text} • {impact.option_contracts:g} option contracts • "
        f"{impact.working_option_orders} working option order"
        f"{'s' if impact.working_option_orders != 1 else ''}"
    )


def _decision_evidence(
    candidate: StrategyCandidateView,
    *,
    order_index: int = 0,
) -> _DecisionEvidence:
    pricing_parts = [
        part.strip()
        for part in str(candidate.pricing_summary).split(" · ")
        if part.strip()
    ]
    publication_notices: list[str] = []
    for part in pricing_parts[2:]:
        for line in part.replace("\n", ";").split(";"):
            notice = line.strip()
            if notice:
                publication_notices.append(notice)

    draft = candidate.order_draft
    try:
        legs = draft.orders[order_index].legs
    except (AttributeError, IndexError, TypeError):
        legs = draft.legs
    quote_legs_total = len(legs)
    quote_legs_available = sum(
        _ticket_quote_populated(leg.bid, leg.ask)
        for leg in legs
    )
    row = candidate.row if isinstance(candidate.row, Mapping) else {}
    return _DecisionEvidence(
        model=str(candidate.model_summary),
        quote_legs_available=quote_legs_available,
        quote_legs_total=quote_legs_total,
        candidate_snapshot=_candidate_snapshot_text(
            row.get("decision_timestamp")
        ),
        publication_notices=tuple(publication_notices),
        publication_checks=(
            (
                "Contract quote fields",
                bool(
                    row.get(
                        "all_option_quotes_valid",
                        quote_legs_available == quote_legs_total,
                    )
                ),
            ),
            (
                "Volatility surface screen",
                bool(row.get("surface_quality_pass", False)),
            ),
            (
                "Liquidity screen",
                bool(row.get("liquidity_policy_pass", False)),
            ),
        ),
        direction_probability_up=float(candidate.direction_probability_up),
        predictive_score=(
            None
            if candidate.predictive_score is None
            else float(candidate.predictive_score)
        ),
        scenario_coverage=float(candidate.scenario_coverage),
        expected_return=(
            None
            if candidate.expected_return is None
            else float(candidate.expected_return)
        ),
        execution_status="Schwab Review & Confirm",
        execution_detail=(
            "Refreshes current quotes, balances, and order facts; you explicitly "
            "confirm placement."
        ),
    )


def _inline_decision_summary(
    candidate: StrategyCandidateView,
    *,
    order_index: int = 0,
) -> str:
    evidence = _decision_evidence(candidate, order_index=order_index)
    incomplete_checks = sum(
        not passed for _label, passed in evidence.publication_checks
    )
    checks_summary = (
        "Publication checks passed"
        if incomplete_checks == 0
        else (
            f"{incomplete_checks} publication check"
            f"{'s' if incomplete_checks != 1 else ''} incomplete"
        )
    )
    return " • ".join(
        (
            (
                f"{evidence.quote_legs_available}/"
                f"{evidence.quote_legs_total} snapshot quotes populated"
            ),
            checks_summary,
            "Live Schwab validation",
        )
    )


def _publication_notice_display(value: str) -> str:
    subject, separator, detail = str(value).partition(":")
    raw_reason = detail.strip() if separator else subject.strip()
    normalized = " ".join(raw_reason.replace("_", " ").split()).casefold()
    explanation = {
        "target event stale": "Model prediction window expired",
        "future or invalid pricing clock": "Pricing clock is outside the candidate window",
        "prediction missing": "No matching model overlay",
        "target snapshot missing": "Prediction target snapshot is missing",
        "leg quote timestamp missing": "Candidate quote timestamp is missing",
        "semantic contract incomplete": "Contract metadata is incomplete",
        "pricing evidence not attached": "Model overlay is not attached",
    }.get(normalized, raw_reason)
    return (
        f"{subject.strip()} — {explanation}"
        if separator
        else explanation
    )


def _candidate_snapshot_text(value: object) -> str:
    observed = value
    converter = getattr(observed, "to_pydatetime", None)
    if callable(converter):
        observed = converter()
    if not hasattr(observed, "astimezone"):
        return "Timestamp unavailable"
    local = observed.astimezone()  # type: ignore[union-attr]
    return local.strftime("%b %d, %I:%M %p").replace(" 0", " ")


def _ticket_order_input(
    quantity: str,
    order_method: str,
    limit_price: str,
) -> str:
    clean_quantity = str(quantity).strip() or "—"
    clean_method = str(order_method).strip() or "Order method unavailable"
    parts = [f"Qty {clean_quantity}", clean_method]
    if clean_method != MARKET_ORDER and str(limit_price).strip():
        try:
            parts.append(f"${float(limit_price):,.2f}")
        except (TypeError, ValueError):
            parts.append(str(limit_price).strip())
    return " • ".join(parts)


def _ticket_quote_populated(bid: object, ask: object) -> bool:
    try:
        return bool(
            math.isfinite(float(bid))
            and math.isfinite(float(ask))
            and float(bid) >= 0.0
            and float(ask) >= 0.0
        )
    except (TypeError, ValueError):
        return False


def _impact_status_style(status: str) -> str:
    if status in {"Share Shortfall", "Funds Below Estimate"}:
        return "StrategyImpactDanger.TLabel"
    if status in {
        "Applicable Balance Unavailable",
        "Broker Estimate Required",
        "Complete Ticket Details",
        "No Strategy Selected",
    }:
        return "StrategyImpactWarning.TLabel"
    return "StrategyImpactGood.TLabel"


def _money_metric_style(
    value: str,
    *,
    positive_is_good: bool,
) -> str:
    cleaned = str(value).strip()
    if not cleaned or cleaned == "—":
        return "StrategyImpactMetricValue.TLabel"
    is_negative = cleaned.startswith("-")
    is_good = not is_negative if positive_is_good else is_negative
    return (
        "StrategyImpactMetricGood.TLabel"
        if is_good
        else "StrategyImpactMetricDanger.TLabel"
    )


def _cash_flow_metric_style(value: str) -> str:
    cleaned = str(value).strip()
    if cleaned.startswith("+"):
        return "StrategyImpactMetricGood.TLabel"
    if cleaned.startswith("-"):
        return "StrategyImpactMetricDanger.TLabel"
    return "StrategyImpactMetricValue.TLabel"


def _money_or_dash(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 0.0:
        return f"-${abs(value):,.2f}"
    return f"${value:,.2f}"


def _cash_flow_text(value: float | None) -> str:
    if value is None:
        return "—"
    if value > 0.0:
        return f"+${value:,.2f} credit"
    if value < 0.0:
        return f"-${abs(value):,.2f} debit"
    return "$0.00"


def _empty_candidate_message(reason: str | None) -> str:
    return f"No candidates: {reason}" if reason else "No Candidates for This Route"


def _human_instruction(value: str) -> str:
    labels = {
        "BUY": "Buy",
        "SELL": "Sell",
        "BUY_TO_OPEN": "Buy to Open",
        "SELL_TO_OPEN": "Sell to Open",
        "BUY_TO_CLOSE": "Buy to Close",
        "SELL_TO_CLOSE": "Sell to Close",
    }
    return labels.get(value, value.replace("_", " ").title())


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def _number(value: float | None, digits: int) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _percentage_points(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}%"


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


__all__ = ["OPTIONS_COMMAND_TABS", "OptionsStrategiesTab"]
