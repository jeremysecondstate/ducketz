from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
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
    build_strategy_order_payload,
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
from app.ui.past_positions import PastPositionsView
from app.ui.schwab_order_messages import (
    order_confirmation_message,
    order_submitted_message,
)
from app.ui.theme import (
    ACCENT,
    BACKGROUND,
    BORDER,
    MUTED_TEXT,
    SURFACE,
    SURFACE_ALT,
    TEXT,
)


OPTIONS_COMMAND_TABS = (
    "Discover",
    "Positions",
    "Orders",
    "Templates",
    "Past Positions",
)


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
        self.ticket_portfolio_impact = tk.StringVar(value="")

        self._apply_styles()
        self._build(parent)
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
            text="Select an entry in Exact Legs to fill the order ticket.",
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
        columns = (
            ("rank", "Rank", 48, tk.E),
            ("strategy", "Strategy", 135, tk.W),
            ("exact_legs", "Exact Legs", 195, tk.W),
            ("calibrated_probability", "Calibrated Probability", 135, tk.E),
            ("scenario_coverage", "Scenario Coverage", 120, tk.E),
            ("expected_return", "Expected Return", 100, tk.E),
            ("portfolio_fit", "Portfolio Fit", 95, tk.W),
            ("score_basis", "Score Basis", 160, tk.W),
            ("pricing_quality", "Pricing / Quality", 280, tk.W),
        )
        for name, label, width, anchor in columns:
            table.heading(name, text=label)
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
        ttk.Label(
            parent,
            text="Order Ticket",
            style="StrategyHeading.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            parent,
            textvariable=self.ticket_strategy,
            style="StrategyBody.TLabel",
        ).pack(anchor=tk.W, pady=(5, 0))
        ttk.Label(
            parent,
            textvariable=self.ticket_structure,
            style="StrategyMuted.TLabel",
        ).pack(anchor=tk.W, pady=(1, 6))

        fields = ttk.Frame(parent, style="StrategySurface.TFrame")
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
            parent,
            text="Ticket Legs",
            style="StrategyHeading.TLabel",
        ).pack(anchor=tk.W, pady=(8, 4))
        legs = ttk.Treeview(
            parent,
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
        legs.pack(fill=tk.X)
        self.ticket_legs = legs

        ttk.Label(
            parent,
            text="Portfolio Impact",
            style="StrategyHeading.TLabel",
        ).pack(anchor=tk.W, pady=(8, 2))
        ttk.Label(
            parent,
            textvariable=self.ticket_portfolio_impact,
            style="StrategyBody.TLabel",
            wraplength=430,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, fill=tk.X)

        self.submit_button = ttk.Button(
            parent,
            text="Submit Order",
            style="StrategySubmit.TButton",
            command=self._submit_order,
            state=tk.DISABLED,
        )
        self.submit_button.pack(fill=tk.X, pady=(8, 0))

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
        self.visible_candidates = tuple(
            item
            for item in self.view.candidates
            if item.symbol == self.symbol.get() and item.horizon == horizon
        )
        for index, candidate in enumerate(self.visible_candidates):
            self.candidate_table.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    candidate.rank,
                    candidate.strategy_display_name,
                    candidate.exact_legs,
                    _percentage_points(candidate.predictive_score),
                    _percentage_points(candidate.scenario_coverage),
                    _percent(candidate.expected_return),
                    candidate.portfolio_fit.label,
                    candidate.score_basis,
                    f"{candidate.pricing_summary} · {candidate.quality_warning}",
                ),
            )
        research_only = sum(
            not candidate.manual_order_actionable
            for candidate in self.visible_candidates
        )
        summary = f"{len(self.visible_candidates):,} Candidates"
        if research_only:
            summary += f" · {research_only:,} Research Only"
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
        self.candidate_table.configure(
            cursor="hand2" if column == "#3" and row_id else ""
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
        self.ticket_portfolio_impact.set(
            " ".join(
                (
                    candidate.portfolio_fit.detail,
                    candidate.pricing_summary + ".",
                    candidate.quality_warning + ".",
                    candidate.manual_actionability,
                )
            )
        )
        order_names = tuple(order.display_name for order in draft.orders)
        self._order_part_box.configure(values=order_names)
        self.ticket_order_part.set(order_names[0])
        self._render_order_part()
        if self.submit_button is not None:
            self.submit_button.configure(
                state=(
                    tk.NORMAL
                    if candidate.manual_order_actionable
                    else tk.DISABLED
                ),
                text=(
                    "Submit Order"
                    if candidate.manual_order_actionable
                    else "Research Only — Submission Disabled"
                ),
            )

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
        self.ticket_portfolio_impact.set("")
        self._limit_price_entry.configure(state=tk.NORMAL)
        self._clear_table(self.ticket_legs)
        if self.submit_button is not None:
            self.submit_button.configure(
                state=tk.DISABLED,
                text="Submit Order",
            )

    def _submit_order(self) -> None:
        candidate = self.selected_candidate
        if candidate is None:
            return
        if not candidate.manual_order_actionable:
            messagebox.showwarning(
                "Option Order Not Actionable",
                candidate.manual_actionability,
            )
            return
        try:
            payload = build_strategy_order_payload(
                candidate.order_draft,
                order_index=self.selected_order_index,
                strategy_quantity=int(self.ticket_quantity.get().strip()),
                order_method=self.ticket_order_method.get(),
                limit_price=(
                    None
                    if self.ticket_order_method.get() == MARKET_ORDER
                    else self.ticket_limit_price.get().strip()
                ),
                duration=self.ticket_duration.get(),
            )
            if not messagebox.askyesno(
                "Confirm Option Order",
                order_confirmation_message(payload),
            ):
                return
            submit_button = getattr(self, "submit_button", None)
            if submit_button is not None:
                submit_button.configure(state=tk.DISABLED)

            def succeeded(location: str | None) -> None:
                if submit_button is not None:
                    submit_button.configure(
                        state=(
                            tk.NORMAL
                            if candidate.manual_order_actionable
                            else tk.DISABLED
                        )
                    )
                messagebox.showinfo(
                    "Option Order Submitted",
                    order_submitted_message(payload, location),
                )
                next_index = self.selected_order_index + 1
                if next_index < candidate.order_draft.order_count:
                    self.selected_order_index = next_index
                    next_order = candidate.order_draft.orders[next_index]
                    self.ticket_order_part.set(next_order.display_name)
                    self._render_order_part()

            def failed(exc: Exception) -> None:
                if submit_button is not None:
                    submit_button.configure(
                        state=(
                            tk.NORMAL
                            if candidate.manual_order_actionable
                            else tk.DISABLED
                        )
                    )
                messagebox.showerror(
                    "Option Order Failed",
                    str(exc) or "The option order could not be submitted.",
                )

            run_in_background(
                self.root,
                lambda: self.session_factory().submit_order(payload),
                succeeded,
                failed,
            )
        except Exception as exc:
            messagebox.showerror(
                "Option Order Failed",
                str(exc) or "The option order could not be submitted.",
            )

    @staticmethod
    def _clear_table(table: ttk.Treeview | None) -> None:
        if table is None:
            return
        for item in table.get_children():
            table.delete(item)


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
