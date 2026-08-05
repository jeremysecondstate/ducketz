from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, simpledialog, ttk

from app.models.option_management import (
    ClosingOrderDraft,
    ExitPlanDraft,
    ManagedOptionOrder,
    OptionPositionBook,
    SavedExitPlanTemplate,
)
from app.services.option_exit_plans import (
    SINGLE_TARGET,
    TARGET_STOP,
    TRAILING_STOP,
    TWO_TARGETS,
    build_exit_plan_draft,
    load_exit_plan_templates,
    save_exit_plan_template,
)
from app.services.schwab_strategy_orders import GOOD_UNTIL_CANCELED
from app.ui.theme import (
    ACCENT,
    BACKGROUND,
    BORDER,
    DANGER,
    MUTED_TEXT,
    SUCCESS,
    SURFACE,
    SURFACE_ALT,
    TABLE_FIELD,
    TEXT,
    WARNING,
)


_TEMPLATE_DETAILS = {
    TARGET_STOP: ("Target + stop", "Take profit and stop loss", True),
    SINGLE_TARGET: ("Single target", "Take profit only", True),
    TWO_TARGETS: ("2 targets", "Scale out in two steps", False),
    TRAILING_STOP: ("Trailing stop", "Follow price higher", False),
}


class ExitPlanBuilderDialog(tk.Toplevel):
    def __init__(
        self,
        *,
        root: tk.Tk,
        book: OptionPositionBook,
        selected_symbols: tuple[str, ...],
        working_orders: tuple[ManagedOptionOrder, ...],
        on_review_single_target: Callable[[ClosingOrderDraft], None],
        on_close_now: Callable[[], None],
        on_show_orders: Callable[[], None],
    ) -> None:
        super().__init__(root)
        self.root = root
        self.book = book
        self.initial_symbols = selected_symbols
        self.working_orders = working_orders
        self.on_review_single_target = on_review_single_target
        self.on_close_now = on_close_now
        self.on_show_orders = on_show_orders
        self.draft: ExitPlanDraft | None = None
        self._template_cards: dict[str, tk.Frame] = {}
        self._template_badges: dict[str, tk.Canvas] = {}
        self._leg_buttons: dict[str, tk.Checkbutton] = {}
        self._leg_chip_frames: dict[str, tk.Frame] = {}
        self._row_enabled_vars: dict[str, tk.BooleanVar] = {}
        self._stop_widgets: list[tk.Widget] = []
        self._refresh_after: str | None = None

        self.template_id = tk.StringVar(master=self, value=TARGET_STOP)
        # The supplied symbols are the position/strategy being managed.  Keep
        # the safest whole-package close as the default; selecting individual
        # legs is always an explicit choice.
        self.coverage_mode = tk.StringVar(master=self, value="entire")
        self.target_percent = tk.StringVar(master=self, value="25")
        self.stop_percent = tk.StringVar(master=self, value="12")
        self.limit_offset = tk.StringVar(master=self, value="0.05")
        self.duration = tk.StringVar(master=self, value=GOOD_UNTIL_CANCELED)
        self.status = tk.StringVar(master=self, value="No exit orders active")
        self.builder_message = tk.StringVar(master=self)
        self.current_mark = tk.StringVar(master=self, value="—")
        self.target_price = tk.StringVar(master=self, value="—")
        self.stop_price = tk.StringVar(master=self, value="—")
        self.protected_quantity = tk.StringVar(master=self, value="—")
        self.target_estimate = tk.StringVar(master=self, value="—")
        self.stop_estimate = tk.StringVar(master=self, value="—")
        self.target_scope = tk.StringVar(master=self, value="Close entire position")
        self.stop_scope = tk.StringVar(master=self, value="Close entire position")
        self.saved_choice = tk.StringVar(master=self, value="Saved templates")
        self.atomic_link = tk.BooleanVar(master=self, value=False)
        self.activate_after_accept = tk.BooleanVar(master=self, value=True)
        self.sync_quantities = tk.BooleanVar(master=self, value=True)
        available_symbols = {leg.symbol for leg in book.legs}
        self.leg_enabled = {
            symbol: tk.BooleanVar(master=self, value=True)
            for symbol in selected_symbols
            if symbol in available_symbols
        }

        self.title("Build exit plan")
        self.configure(background=BACKGROUND)
        self.minsize(1080, 760)
        self.transient(root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._build()
        self._fit_to_root()
        self._load_saved_choices()
        self._refresh()
        self.grab_set()
        self.focus_set()

    def _fit_to_root(self) -> None:
        self.root.update_idletasks()
        self.update_idletasks()
        width = max(1080, self.root.winfo_width() - 24)
        available_height = max(760, self.root.winfo_height() - 34)
        height = min(available_height, max(760, self.winfo_reqheight()))
        x = max(0, self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2)
        y = max(0, self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self) -> None:
        outer = tk.Frame(self, background=BACKGROUND, padx=12, pady=10)
        outer.pack(fill=tk.BOTH, expand=True)
        body = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True)
        left = tk.Frame(body, background=BACKGROUND)
        right = tk.Frame(body, background=BACKGROUND)
        # Pane weights distribute only the space beyond each pane's requested
        # width.  The builder requests more width than the preview, so the
        # right pane needs the larger surplus weight to land on the concept's
        # roughly 63/37 split without clipping either pane's children.
        body.add(left, weight=7)
        body.add(right, weight=12)

        self._build_header(left)
        self._build_template_section(left)
        self._build_coverage_section(left)
        self._build_linked_exits(left)

        self._build_sequence(right)
        self._build_at_glance(right)
        self._build_safeguards(right)
        self._build_footer(outer)

    def _build_header(self, parent: tk.Frame) -> None:
        header = tk.Frame(parent, background=BACKGROUND)
        header.pack(fill=tk.X, pady=(0, 9))
        header.grid_columnconfigure(1, minsize=390)
        header.grid_columnconfigure(3, weight=1)
        back = tk.Button(
            header,
            text="‹  Positions",
            command=self.destroy,
            background=BACKGROUND,
            foreground=ACCENT,
            activebackground=BACKGROUND,
            activeforeground="#5db3ff",
            borderwidth=0,
            font=("Segoe UI", 10),
            cursor="hand2",
        )
        back.grid(row=0, column=0, sticky=tk.NW, padx=(0, 14), pady=(3, 0))
        heading = tk.Frame(header, background=BACKGROUND)
        heading.grid(row=0, column=1, sticky=tk.NW)
        tk.Label(
            heading,
            text="Build exit plan",
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 17, "bold"),
        ).pack(anchor=tk.W)
        first = next((leg for leg in self.book.legs if leg.symbol in self.initial_symbols), None)
        position_kind = "custom option strategy" if len(self.initial_symbols) > 1 else "exact OCC position"
        subtitle = (
            f"{first.underlying_symbol} · {position_kind} · {len(self.initial_symbols)} "
            f"leg{'s' if len(self.initial_symbols) != 1 else ''} · Qty {self._package_quantity()}"
            if first is not None
            else "Exact option position"
        )
        tk.Label(
            heading,
            text=subtitle,
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, pady=(1, 0))
        pill = tk.Frame(header, background=SURFACE_ALT, highlightbackground=BORDER, highlightthickness=1)
        pill.grid(row=0, column=2, sticky=tk.N, padx=(18, 0), pady=(3, 0))
        tk.Label(
            pill,
            text="●",
            background=SURFACE_ALT,
            foreground=WARNING,
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=(9, 4), pady=5)
        tk.Label(
            pill,
            textvariable=self.status,
            background=SURFACE_ALT,
            foreground=TEXT,
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=(0, 9), pady=5)

    def _build_template_section(self, parent: tk.Frame) -> None:
        section = self._section(parent, "1. Choose a template")
        saved = tk.Menubutton(
            section.header,
            text="Manage templates",
            background=SURFACE,
            foreground=ACCENT,
            activebackground=SURFACE,
            activeforeground="#5db3ff",
            borderwidth=0,
            highlightthickness=0,
            font=("Segoe UI", 9),
            cursor="hand2",
        )
        saved.pack(side=tk.RIGHT)
        menu = tk.Menu(saved, tearoff=False, background=SURFACE_ALT, foreground=TEXT)
        saved.configure(menu=menu)
        self.saved_menu = menu

        cards = tk.Frame(section.body, background=SURFACE)
        cards.pack(fill=tk.X, padx=8, pady=(7, 14))
        for column, template_id in enumerate((TARGET_STOP, SINGLE_TARGET, TWO_TARGETS, TRAILING_STOP)):
            cards.grid_columnconfigure(column, weight=1, uniform="exit-template")
            self._template_card(cards, template_id, column)

    def _template_card(self, parent: tk.Frame, template_id: str, column: int) -> None:
        title, detail, selectable = _TEMPLATE_DETAILS[template_id]
        card = tk.Frame(
            parent,
            background=SURFACE_ALT,
            highlightbackground=BORDER,
            highlightthickness=1,
            padx=8,
            pady=12,
            height=126,
            cursor="hand2" if selectable else "arrow",
        )
        card.grid(row=0, column=column, sticky=tk.NSEW, padx=(0 if column == 0 else 4, 0 if column == 3 else 4))
        card.pack_propagate(False)
        # Tk canvases default to a surprisingly large requested width.  Four of
        # those requests force the left pane to consume nearly the whole dialog,
        # even though the icons themselves only use about 100 px.  Keep the
        # request compact and let ``fill=X`` stretch the cards when room exists.
        chart = tk.Canvas(
            card,
            width=110,
            height=38,
            background=SURFACE_ALT,
            highlightthickness=0,
        )
        chart.pack(fill=tk.X)
        self._draw_template_icon(chart, template_id, selectable)
        label = tk.Label(
            card,
            text=title,
            background=SURFACE_ALT,
            foreground=TEXT if selectable else "#66758a",
            font=("Segoe UI", 11, "bold"),
        )
        label.pack()
        detail_label = tk.Label(
            card,
            text=detail,
            background=SURFACE_ALT,
            foreground=MUTED_TEXT if selectable else "#536174",
            font=("Segoe UI", 9),
        )
        detail_label.pack(pady=(1, 0))
        badge = tk.Canvas(
            card,
            background=SURFACE_ALT,
            highlightthickness=0,
            width=24,
            height=24,
        )
        badge.create_oval(2, 2, 22, 22, fill=ACCENT, outline="#5db3ff")
        badge.create_text(12, 12, text="✓", fill="#ffffff", font=("Segoe UI", 9, "bold"))
        self._template_cards[template_id] = card
        self._template_badges[template_id] = badge
        for widget in (card, chart, label, detail_label, badge):
            widget.bind("<Button-1>", lambda _event, key=template_id: self._template_clicked(key))

    @staticmethod
    def _draw_template_icon(canvas: tk.Canvas, template_id: str, selectable: bool) -> None:
        color = MUTED_TEXT if selectable else "#536174"
        canvas.create_line(20, 16, 100, 16, fill=color, dash=(4, 3), width=1)
        if template_id in {TARGET_STOP, SINGLE_TARGET, TWO_TARGETS}:
            canvas.create_line(20, 12, 44, 12, 50, 5, 95, 5, fill=SUCCESS if selectable else color, width=2)
        if template_id == TWO_TARGETS:
            canvas.create_line(20, 24, 38, 24, 44, 18, 78, 18, fill=SUCCESS if selectable else color, width=2)
        if template_id in {TARGET_STOP, TRAILING_STOP}:
            canvas.create_line(20, 20, 44, 20, 50, 26, 95, 26, fill=DANGER if selectable else color, width=2)

    def _build_coverage_section(self, parent: tk.Frame) -> None:
        section = self._section(parent, "2. Position coverage")
        row = tk.Frame(section.body, background=SURFACE)
        row.pack(fill=tk.X, padx=8, pady=(6, 14))
        choices = tk.Frame(row, background=SURFACE)
        choices.pack(side=tk.LEFT)
        entire_title = "Entire strategy" if len(self.leg_enabled) > 1 else "Entire position"
        for value, title, detail in (
            ("entire", entire_title, "Close as one net order"),
            ("selected", "Selected legs", "Choose exact contracts"),
        ):
            card = tk.Frame(
                choices,
                background=TABLE_FIELD,
                highlightbackground=BORDER,
                highlightthickness=1,
                width=220,
                height=64,
            )
            card.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6))
            card.pack_propagate(False)
            radio = tk.Radiobutton(
                card,
                text=title,
                variable=self.coverage_mode,
                value=value,
                command=self._coverage_changed,
                background=TABLE_FIELD,
                foreground=TEXT,
                activebackground=TABLE_FIELD,
                activeforeground=TEXT,
                selectcolor=SURFACE_ALT,
                font=("Segoe UI", 9, "bold"),
                borderwidth=0,
                highlightthickness=0,
            )
            radio.pack(anchor=tk.W, padx=7, pady=(4, 0))
            tk.Label(
                card,
                text=detail,
                background=TABLE_FIELD,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8),
            ).pack(anchor=tk.W, padx=29, pady=(0, 5))
            if value == "selected" and len(self.leg_enabled) < 2:
                radio.configure(state=tk.DISABLED)
            if value == "entire":
                self.entire_coverage_card = card
            else:
                self.selected_coverage_card = card

        chips = tk.Frame(
            row,
            background=TABLE_FIELD,
            highlightbackground=BORDER,
            highlightthickness=1,
            padx=6,
            pady=5,
        )
        chips.pack(side=tk.LEFT, anchor=tk.N)
        chip_row = tk.Frame(chips, background=TABLE_FIELD)
        chip_row.pack(anchor=tk.W)
        by_symbol = {leg.symbol: leg for leg in self.book.legs}
        for symbol, variable in self.leg_enabled.items():
            leg = by_symbol[symbol]
            chip_frame = tk.Frame(
                chip_row,
                background=SURFACE_ALT,
                highlightbackground=BORDER,
                highlightthickness=1,
            )
            chip_frame.pack(side=tk.LEFT, padx=3)
            chip = tk.Checkbutton(
                chip_frame,
                text=f"{leg.strike:g} {leg.option_type.title()}",
                variable=variable,
                command=self._schedule_refresh,
                indicatoron=False,
                background=SURFACE_ALT,
                foreground=TEXT,
                activebackground=TABLE_FIELD,
                activeforeground=TEXT,
                selectcolor=TABLE_FIELD,
                disabledforeground=TEXT,
                font=("Segoe UI", 8),
                borderwidth=0,
                highlightthickness=0,
                padx=8,
                pady=3,
            )
            chip.pack()
            self._leg_buttons[symbol] = chip
            self._leg_chip_frames[symbol] = chip_frame
        tk.Label(
            chips,
            text="Close as one net order",
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(anchor=tk.W, padx=3, pady=(3, 0))

    def _build_linked_exits(self, parent: tk.Frame) -> None:
        section = self._section(parent, "3. Linked exits")
        oco = tk.Canvas(
            section.header,
            background=SURFACE,
            highlightthickness=0,
            width=240,
            height=22,
        )
        oco.place(relx=0.48, rely=0.5, anchor=tk.CENTER)
        oco.bind("<Configure>", lambda _event: self._draw_oco_header())
        self.oco_header = oco

        content = tk.Frame(section.body, background=SURFACE)
        content.pack(fill=tk.X, padx=8, pady=(5, 9))

        linked_rows = tk.Frame(content, background=SURFACE)
        linked_rows.pack(fill=tk.X)
        connector = tk.Canvas(
            linked_rows,
            background=SURFACE,
            highlightthickness=0,
            width=52,
            height=136,
        )
        connector.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 3))
        connector.bind("<Configure>", lambda _event: self._draw_oco_connector())
        self.oco_connector = connector

        rows = tk.Frame(linked_rows, background=SURFACE)
        rows.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._build_exit_row(rows, "target")
        self._build_exit_row(rows, "stop")
        relation = tk.Label(
            content,
            text="🔗  When one fills, cancel the other",
            background=SURFACE,
            foreground=ACCENT,
            font=("Segoe UI", 8),
        )
        relation.pack(anchor=tk.W, pady=(4, 10))
        self.link_relation_label = relation

        time_exit = tk.Frame(
            content,
            background=TABLE_FIELD,
            highlightbackground=MUTED_TEXT,
            highlightthickness=1,
        )
        time_exit.pack(fill=tk.X)
        tk.Label(
            time_exit,
            text="＋",
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 13),
        ).pack(side=tk.LEFT, padx=(10, 7), pady=7)
        time_copy = tk.Frame(time_exit, background=TABLE_FIELD)
        time_copy.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=6)
        tk.Label(
            time_copy,
            text="Add time-based exit",
            background=TABLE_FIELD,
            foreground=TEXT,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            time_copy,
            text="Close before expiration or at a specific time · Not yet supported",
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(anchor=tk.W, pady=(1, 0))
        tk.Label(
            time_exit,
            text="⌄",
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 11),
        ).pack(side=tk.RIGHT, padx=10)

        self._build_quick_actions(content)

    def _build_exit_row(self, parent: tk.Frame, kind: str) -> None:
        is_target = kind == "target"
        color = SUCCESS if is_target else DANGER
        frame = tk.Frame(
            parent,
            background=TABLE_FIELD,
            highlightbackground=color,
            highlightthickness=1,
            padx=10,
            pady=9,
        )
        frame.pack(fill=tk.X, pady=(0, 8))
        frame.grid_columnconfigure(0, minsize=205)
        frame.grid_columnconfigure(1, weight=1)
        if not is_target:
            self.stop_frame = frame

        branch = tk.Frame(frame, background=TABLE_FIELD)
        branch.grid(row=0, column=0, sticky="nsw", padx=(0, 12))
        enabled = tk.BooleanVar(master=self, value=True)
        self._row_enabled_vars[kind] = enabled
        check = tk.Checkbutton(
            branch,
            variable=enabled,
            state=tk.DISABLED,
            background=TABLE_FIELD,
            activebackground=TABLE_FIELD,
            selectcolor=SURFACE_ALT,
            borderwidth=0,
            highlightthickness=0,
        )
        check.pack(side=tk.LEFT)
        tk.Label(
            branch,
            text="◎" if is_target else "!",
            background=TABLE_FIELD,
            foreground=color,
            font=("Segoe UI", 12, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 7))
        branch_copy = tk.Frame(branch, background=TABLE_FIELD)
        branch_copy.pack(side=tk.LEFT)
        tk.Label(
            branch_copy,
            text="Take profit" if is_target else "Stop loss",
            background=TABLE_FIELD,
            foreground=color,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor=tk.W)
        scope = self.target_scope if is_target else self.stop_scope
        tk.Label(
            branch_copy,
            textvariable=scope,
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(anchor=tk.W, pady=(1, 0))

        controls = tk.Frame(frame, background=TABLE_FIELD)
        controls.grid(row=0, column=1, sticky=tk.EW)

        def field(title: str, *, right_pad: int = 9) -> tk.Frame:
            group = tk.Frame(controls, background=TABLE_FIELD)
            group.pack(side=tk.LEFT, padx=(0, right_pad))
            tk.Label(
                group,
                text=title,
                background=TABLE_FIELD,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8),
            ).pack(anchor=tk.W, pady=(0, 2))
            return group

        basis_group = field("Trigger basis")
        basis = ttk.Combobox(basis_group, values=("Position mark",), state="readonly", width=12)
        basis.set("Position mark")
        basis.pack()

        operation_group = field("Op")
        tk.Label(
            operation_group,
            text="+" if is_target else "−",
            background=TABLE_FIELD,
            foreground=TEXT,
            highlightbackground=BORDER,
            highlightthickness=1,
            width=3,
            pady=3,
            font=("Segoe UI", 9, "bold"),
        ).pack()

        value_group = field("Value")
        value_row = tk.Frame(value_group, background=TABLE_FIELD)
        value_row.pack()
        value_var = self.target_percent if is_target else self.stop_percent
        entry = ttk.Entry(value_row, textvariable=value_var, width=6)
        entry.pack(side=tk.LEFT)
        entry.bind("<KeyRelease>", self._schedule_refresh)
        tk.Label(
            value_row,
            text="%",
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT, padx=(3, 0))

        order_group = field("Order type")
        order_type = ttk.Combobox(
            order_group,
            values=("LIMIT",) if is_target else ("STOP LIMIT",),
            state="readonly",
            width=10,
        )
        order_type.set("LIMIT" if is_target else "STOP LIMIT")
        order_type.pack()

        offset_group = field("" if is_target else "Limit offset")
        if is_target:
            tk.Frame(offset_group, background=TABLE_FIELD, width=72, height=22).pack()
        else:
            offset_row = tk.Frame(offset_group, background=TABLE_FIELD)
            offset_row.pack()
            tk.Label(
                offset_row,
                text="$",
                background=TABLE_FIELD,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8),
            ).pack(side=tk.LEFT, padx=(0, 2))
            offset = ttk.Entry(offset_row, textvariable=self.limit_offset, width=5)
            offset.pack(side=tk.LEFT)
            offset.bind("<KeyRelease>", self._schedule_refresh)
            self._stop_widgets.extend((basis, entry, order_type, offset))

        duration_group = field("TIF", right_pad=0)
        duration = ttk.Combobox(
            duration_group,
            values=("GTC",),
            state="readonly",
            width=5,
        )
        duration.set("GTC")
        duration.pack()
        if not is_target:
            self._stop_widgets.append(duration)

        estimate = self.target_estimate if is_target else self.stop_estimate
        estimate_group = tk.Frame(frame, background=TABLE_FIELD)
        estimate_group.grid(row=0, column=2, sticky=tk.E, padx=(12, 2))
        tk.Label(
            estimate_group,
            text="Est. net",
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(anchor=tk.E, pady=(0, 2))
        tk.Label(
            estimate_group,
            textvariable=estimate,
            background=TABLE_FIELD,
            foreground=color,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor=tk.E)

    def _build_quick_actions(self, parent: tk.Frame) -> None:
        quick = tk.Frame(parent, background=SURFACE)
        quick.pack(fill=tk.X, pady=(12, 0))
        tk.Frame(quick, background=BORDER, height=1).pack(fill=tk.X, pady=(0, 8))
        tk.Label(
            quick,
            text="Quick actions (requires confirmation)",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(anchor=tk.W)
        actions = tk.Frame(quick, background=SURFACE)
        actions.pack(fill=tk.X, pady=(7, 0))
        close_border = tk.Frame(actions, background=DANGER, padx=1, pady=1)
        close_border.pack(side=tk.LEFT, padx=(0, 5))
        close_now = tk.Button(
            close_border,
            text="⊗  Close now…",
            command=self._close_now,
            background=TABLE_FIELD,
            foreground=DANGER,
            activebackground=SURFACE_ALT,
            activeforeground=DANGER,
            highlightthickness=0,
            borderwidth=0,
            font=("Segoe UI", 9),
            width=24,
            padx=16,
            pady=4,
            cursor="hand2",
        )
        close_now.pack(fill=tk.X)
        cancel_border = tk.Frame(actions, background=BORDER, padx=1, pady=1)
        cancel_border.pack(side=tk.LEFT, padx=(5, 0))
        cancel = tk.Button(
            cancel_border,
            text="⊗  Cancel working orders",
            command=self._show_orders,
            state=tk.NORMAL if self.working_orders else tk.DISABLED,
            background=TABLE_FIELD,
            foreground=TEXT,
            activebackground=SURFACE_ALT,
            activeforeground=TEXT,
            disabledforeground="#536174",
            highlightthickness=0,
            borderwidth=0,
            font=("Segoe UI", 9),
            width=24,
            padx=16,
            pady=4,
            cursor="hand2",
        )
        cancel.pack(fill=tk.X)
        self.cancel_orders_button = cancel
        self.close_now_button = close_now

    def _build_sequence(self, parent: tk.Frame) -> None:
        section = self._right_section(parent, "Exit sequence")
        canvas = tk.Canvas(section, background=SURFACE, highlightthickness=0, height=270)
        canvas.pack(fill=tk.X, padx=6, pady=(0, 6))
        canvas.bind("<Configure>", lambda _event: self._draw_sequence())
        self.sequence_canvas = canvas

    def _build_at_glance(self, parent: tk.Frame) -> None:
        section = self._right_section(parent, "At a glance")
        grid = tk.Frame(section, background=SURFACE)
        grid.pack(fill=tk.X, padx=6, pady=(0, 7))
        entries = (
            ("Current mark", self.current_mark, TEXT),
            ("Profit target", self.target_price, SUCCESS),
            ("Stop trigger", self.stop_price, DANGER),
            ("Protected qty", self.protected_quantity, TEXT),
        )
        for column, (title, variable, color) in enumerate(entries):
            grid.grid_columnconfigure(column, weight=1, uniform="exit-glance")
            cell = tk.Frame(grid, background=SURFACE)
            cell.grid(row=0, column=column, sticky=tk.EW, padx=3)
            tk.Label(cell, text=title, background=SURFACE, foreground=MUTED_TEXT, font=("Segoe UI", 8)).pack()
            tk.Label(cell, textvariable=variable, background=SURFACE, foreground=color, font=("Segoe UI", 11, "bold")).pack(pady=(2, 0))

    def _build_safeguards(self, parent: tk.Frame) -> None:
        section = self._right_section(parent, "Safeguards", expand=True)
        checks = tk.Frame(section, background=SURFACE)
        checks.pack(fill=tk.X, padx=8, pady=(0, 4))
        for text, variable in (
            ("Submit both exits as one linked order", self.atomic_link),
            ("Activate only after broker accepts both", self.activate_after_accept),
            ("Keep quantities synchronized", self.sync_quantities),
        ):
            tk.Checkbutton(
                checks,
                text=text,
                variable=variable,
                state=tk.DISABLED,
                background=SURFACE,
                foreground=MUTED_TEXT,
                disabledforeground=MUTED_TEXT,
                activebackground=SURFACE,
                selectcolor=SURFACE_ALT,
                font=("Segoe UI", 8),
                borderwidth=0,
                highlightthickness=0,
            ).pack(anchor=tk.W, pady=2)
        warning = tk.Frame(section, background="#382a10", highlightbackground=WARNING, highlightthickness=1)
        warning.pack(fill=tk.X, padx=8, pady=(4, 5))
        tk.Label(
            warning,
            text="⚠",
            background="#382a10",
            foreground=WARNING,
            font=("Segoe UI Symbol", 12, "bold"),
        ).pack(side=tk.LEFT, padx=(8, 6), pady=5)
        tk.Label(
            warning,
            textvariable=self.builder_message,
            background="#382a10",
            foreground="#ffd58a",
            font=("Segoe UI", 8, "bold"),
            wraplength=500,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), pady=5)
        rail = tk.Canvas(section, height=76, background=SURFACE, highlightthickness=0)
        rail.pack(side=tk.BOTTOM, fill=tk.X, padx=8, pady=(10, 8))
        rail.bind("<Configure>", lambda _event: self._draw_price_rail())
        self.price_rail = rail

    def _build_footer(self, parent: tk.Frame) -> None:
        tk.Frame(parent, background=BORDER, height=1).pack(fill=tk.X, pady=(5, 0))
        footer = tk.Frame(parent, background=BACKGROUND)
        footer.pack(fill=tk.X, pady=(12, 2))
        ttk.Button(
            footer,
            text="Save as template",
            command=self._save_template,
            width=26,
        ).pack(side=tk.LEFT)
        tk.Label(
            footer,
            text="Estimated fees if closed: shown during review",
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 9),
            justify=tk.CENTER,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=12)
        review = ttk.Button(
            footer,
            text="Review exit plan",
            command=self._review,
            style="ManagementPrimary.TButton",
            state=tk.DISABLED,
            width=38,
        )
        review.pack(side=tk.RIGHT)
        self.review_button = review

    @staticmethod
    def _section(parent: tk.Frame, title: str) -> object:
        frame = tk.Frame(parent, background=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        frame.pack(fill=tk.X, pady=(0, 7))
        header = tk.Frame(frame, background=SURFACE)
        header.pack(fill=tk.X, padx=10, pady=(9, 4))
        tk.Label(header, text=title, background=SURFACE, foreground=TEXT, font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        body = tk.Frame(frame, background=SURFACE)
        body.pack(fill=tk.X)
        return type("Section", (), {"frame": frame, "header": header, "body": body})()

    @staticmethod
    def _right_section(parent: tk.Frame, title: str, *, expand: bool = False) -> tk.Frame:
        frame = tk.Frame(parent, background=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        frame.pack(fill=tk.BOTH if expand else tk.X, expand=expand, padx=(7, 0), pady=(0, 7))
        tk.Label(
            frame,
            text=title,
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor=tk.W, padx=9, pady=(7, 5))
        return frame

    def _template_clicked(self, template_id: str) -> None:
        if not _TEMPLATE_DETAILS[template_id][2]:
            reason = (
                "Two-target scale-out is visible but disabled until partial strategy linkage is verified."
                if template_id == TWO_TARGETS
                else "Trailing stops are visible but disabled until Schwab option support is verified."
            )
            self.builder_message.set(reason)
            return
        self.template_id.set(template_id)
        self._refresh()

    def _coverage_changed(self) -> None:
        if self.coverage_mode.get() == "entire":
            for variable in self.leg_enabled.values():
                variable.set(True)
        self._refresh()

    def _draw_oco_header(self) -> None:
        canvas = getattr(self, "oco_header", None)
        if canvas is None:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 180)
        height = max(canvas.winfo_height(), 20)
        linked = self.template_id.get() == TARGET_STOP
        color = ACCENT if linked else MUTED_TEXT
        caption = "ONE CANCELS OTHER" if linked else "SINGLE EXIT"
        center = width / 2
        half_text = 66 if linked else 40
        y = height / 2
        canvas.create_line(3, height - 3, 3, y, center - half_text - 8, y, fill=color, width=1)
        canvas.create_line(center + half_text + 8, y, width - 3, y, width - 3, height - 3, fill=color, width=1)
        canvas.create_text(
            center,
            y,
            text=caption,
            fill=color,
            font=("Segoe UI", 8, "bold"),
        )

    def _draw_oco_connector(self) -> None:
        canvas = getattr(self, "oco_connector", None)
        if canvas is None:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 48)
        height = max(canvas.winfo_height(), 120)
        top_y = height * 0.25
        bottom_y = height * 0.75
        right = width - 3
        trunk = 15
        if self.template_id.get() != TARGET_STOP:
            canvas.create_line(trunk, top_y, right, top_y, fill=MUTED_TEXT, arrow=tk.LAST)
            return
        center_y = height / 2
        canvas.create_line(
            trunk,
            center_y - 11,
            trunk,
            top_y,
            right,
            top_y,
            fill=ACCENT,
            width=1,
            arrow=tk.LAST,
        )
        canvas.create_line(
            trunk,
            center_y + 11,
            trunk,
            bottom_y,
            right,
            bottom_y,
            fill=ACCENT,
            width=1,
            arrow=tk.LAST,
        )
        canvas.create_text(
            trunk,
            center_y,
            text="OCO",
            fill=MUTED_TEXT,
            font=("Segoe UI", 8, "bold"),
        )

    def _schedule_refresh(self, _event: object = None) -> None:
        if self._refresh_after is not None:
            self.after_cancel(self._refresh_after)
        self._refresh_after = self.after(120, self._refresh)

    def _selected_symbols(self) -> tuple[str, ...]:
        if self.coverage_mode.get() == "entire":
            return self.initial_symbols
        return tuple(symbol for symbol, variable in self.leg_enabled.items() if variable.get())

    def _refresh(self) -> None:
        self._refresh_after = None
        self._refresh_template_cards()
        selected = self._selected_symbols()
        for symbol, button in self._leg_buttons.items():
            selecting_legs = self.coverage_mode.get() == "selected"
            button.configure(state=tk.NORMAL if selecting_legs else tk.DISABLED)
            selected_chip = selecting_legs and self.leg_enabled[symbol].get()
            self._leg_chip_frames[symbol].configure(
                highlightbackground=ACCENT if selected_chip else BORDER
            )
        entire = self.coverage_mode.get() == "entire"
        self.entire_coverage_card.configure(highlightbackground=ACCENT if entire else BORDER)
        self.selected_coverage_card.configure(highlightbackground=ACCENT if not entire else BORDER)
        stop_enabled = self.template_id.get() == TARGET_STOP
        for widget in self._stop_widgets:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="readonly" if stop_enabled else tk.DISABLED)
            else:
                widget.configure(state=tk.NORMAL if stop_enabled else tk.DISABLED)
        self._draw_oco_header()
        self._draw_oco_connector()
        self.link_relation_label.configure(
            text=(
                "🔗  When one fills, cancel the other"
                if stop_enabled
                else "One reviewed GTC limit close"
            ),
            foreground=ACCENT if stop_enabled else MUTED_TEXT,
        )
        self.stop_frame.configure(highlightbackground=DANGER if stop_enabled else BORDER)
        self.atomic_link.set(False)
        try:
            draft = build_exit_plan_draft(
                self.book,
                selected,
                working_orders=self.working_orders,
                coverage_mode=self.coverage_mode.get(),
                template_id=self.template_id.get(),
                target_percent=self.target_percent.get(),
                stop_percent=self.stop_percent.get(),
                limit_offset=self.limit_offset.get(),
                duration=self.duration.get(),
            )
        except Exception as exc:
            self.draft = None
            self.builder_message.set(str(exc))
            self.current_mark.set("—")
            self.target_price.set("—")
            self.stop_price.set("—")
            self.protected_quantity.set("—")
            self.target_estimate.set("Unavailable")
            self.stop_estimate.set("Unavailable")
            self.status.set("Plan needs attention")
            self.review_button.configure(state=tk.DISABLED)
            self.cancel_orders_button.configure(
                state=tk.NORMAL if self.working_orders else tk.DISABLED
            )
            self._draw_sequence()
            self._draw_price_rail()
            return

        self.draft = draft
        target = draft.take_profit
        stop = draft.stop_loss
        self.current_mark.set(_money(draft.position_mark))
        self.target_price.set(_money(target.trigger_price) if target else "—")
        self.stop_price.set(_money(stop.trigger_price) if stop else "—")
        self.protected_quantity.set(f"{draft.protected_quantity} of {draft.protected_quantity}")
        self.target_estimate.set(_branch_estimate(target))
        self.stop_estimate.set(_branch_estimate(stop) if stop else "Disabled")
        if self.coverage_mode.get() == "selected":
            close_scope = f"Close {len(draft.position_symbols)} selected leg{'s' if len(draft.position_symbols) != 1 else ''}"
        else:
            unit = "strategy" if len(draft.position_symbols) > 1 else "contract"
            close_scope = (
                f"Close {draft.protected_quantity} {unit}"
                f"{'s' if draft.protected_quantity != 1 else ''}"
            )
        self.target_scope.set(close_scope)
        self.stop_scope.set(close_scope)
        if draft.conflicting_order_ids:
            self.status.set(f"{len(draft.conflicting_order_ids)} close order conflict")
            self.builder_message.set(
                "Resolve working close order " + ", ".join(draft.conflicting_order_ids) + " before review."
            )
            self.review_button.configure(state=tk.DISABLED)
        elif draft.capability_reason:
            self.status.set("No exit orders active")
            self.builder_message.set(
                "Stop-limit orders may not fill during fast moves or price gaps."
                if draft.template_id == TARGET_STOP
                else draft.capability_reason
            )
            self.review_button.configure(state=tk.NORMAL)
        else:
            self.status.set("No exit orders active")
            self.builder_message.set(
                "This plan becomes one reviewed GTC limit close; no linked broker order is required."
            )
            self.review_button.configure(state=tk.NORMAL)
        self.cancel_orders_button.configure(
            state=tk.NORMAL if self.working_orders else tk.DISABLED
        )
        self._draw_sequence()
        self._draw_price_rail()

    def _refresh_template_cards(self) -> None:
        current = self.template_id.get()
        for template_id, card in self._template_cards.items():
            selected = current == template_id
            card.configure(highlightbackground=ACCENT if selected else BORDER)
            badge = self._template_badges[template_id]
            if selected:
                badge.place(relx=1.0, x=-7, y=7, anchor=tk.NE)
            else:
                badge.place_forget()

    def _draw_sequence(self) -> None:
        canvas = getattr(self, "sequence_canvas", None)
        if canvas is None:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 320)
        height = max(canvas.winfo_height(), 260)
        center = width / 2
        draft = self.draft
        if draft is None:
            canvas.create_text(
                center,
                height / 2,
                text="Complete valid trigger values to preview the sequence.",
                fill=MUTED_TEXT,
                font=("Segoe UI", 10),
            )
            return

        diagram_width = min(width - 28, 620)
        diagram_height = min(max(height - 24, 260), 360)
        top = (height - diagram_height) / 2
        root_top = top
        root_bottom = top + diagram_height * 0.15
        monitor_top = top + diagram_height * 0.22
        monitor_bottom = top + diagram_height * 0.34
        branch_top = top + diagram_height * 0.54
        branch_bottom = top + diagram_height * 0.73
        closed_top = top + diagram_height * 0.86
        closed_bottom = top + diagram_height * 0.99
        root_width = min(230, diagram_width * 0.42)
        monitor_width = min(210, diagram_width * 0.38)
        root_detail = (
            f"{len(draft.position_symbols)} leg{'s' if len(draft.position_symbols) != 1 else ''}"
            f" · Qty {draft.protected_quantity}"
        )
        _canvas_node(
            canvas,
            center - root_width / 2,
            root_top,
            center + root_width / 2,
            root_bottom,
            f"Existing {draft.underlying_symbol} position",
            root_detail,
            BORDER,
            TEXT,
        )
        canvas.create_line(
            center,
            root_bottom,
            center,
            monitor_top,
            fill=MUTED_TEXT,
            arrow=tk.LAST,
        )
        _canvas_node(
            canvas,
            center - monitor_width / 2,
            monitor_top,
            center + monitor_width / 2,
            monitor_bottom,
            "Monitor position mark",
            "Live net mark",
            BORDER,
            TEXT,
        )
        target = draft.take_profit
        stop = draft.stop_loss
        if draft.relationship == "OCO" and target and stop:
            branch_gap = 34
            branch_width = min(220, (diagram_width - branch_gap) / 2)
            branch_offset = (branch_width + branch_gap) / 2
            target_center = center - branch_offset
            stop_center = center + branch_offset
            split_y = top + diagram_height * 0.45
            label_y = top + diagram_height * 0.405
            canvas.create_line(center, monitor_bottom, center, split_y, fill=MUTED_TEXT)
            canvas.create_text(
                center,
                label_y,
                text="OCO",
                fill=TEXT,
                font=("Segoe UI", 9, "bold"),
            )
            canvas.create_line(target_center, split_y, stop_center, split_y, fill=MUTED_TEXT)
            canvas.create_line(
                target_center,
                split_y,
                target_center,
                branch_top,
                fill=MUTED_TEXT,
                arrow=tk.LAST,
            )
            canvas.create_line(
                stop_center,
                split_y,
                stop_center,
                branch_top,
                fill=MUTED_TEXT,
                arrow=tk.LAST,
            )
            _canvas_node(
                canvas,
                target_center - branch_width / 2,
                branch_top,
                target_center + branch_width / 2,
                branch_bottom,
                f"{target.trigger_operator}{target.trigger_percent:g}% Take profit",
                f"{self.target_scope.get()} · {_money(target.trigger_price)}",
                SUCCESS,
                SUCCESS,
            )
            _canvas_node(
                canvas,
                stop_center - branch_width / 2,
                branch_top,
                stop_center + branch_width / 2,
                branch_bottom,
                f"{stop.trigger_operator}{stop.trigger_percent:g}% Stop loss",
                f"{self.stop_scope.get()} · {_money(stop.trigger_price)}",
                DANGER,
                DANGER,
            )
            merge_y = top + diagram_height * 0.80
            canvas.create_line(target_center, branch_bottom, target_center, merge_y, center, merge_y, fill=MUTED_TEXT)
            canvas.create_line(stop_center, branch_bottom, stop_center, merge_y, center, merge_y, fill=MUTED_TEXT)
            canvas.create_line(center, merge_y, center, closed_top, fill=MUTED_TEXT, arrow=tk.LAST)
            _canvas_node(
                canvas,
                center - monitor_width * 0.42,
                closed_top,
                center + monitor_width * 0.42,
                closed_bottom,
                "Position closed",
                "No open quantity",
                BORDER,
                TEXT,
            )
        elif target:
            branch_width = min(240, diagram_width * 0.5)
            canvas.create_line(center, monitor_bottom, center, branch_top, fill=MUTED_TEXT, arrow=tk.LAST)
            _canvas_node(
                canvas,
                center - branch_width / 2,
                branch_top,
                center + branch_width / 2,
                branch_bottom,
                f"{target.trigger_operator}{target.trigger_percent:g}% Take profit",
                f"{self.target_scope.get()} · {_money(target.trigger_price)}",
                SUCCESS,
                SUCCESS,
            )
            canvas.create_line(center, branch_bottom, center, closed_top, fill=MUTED_TEXT, arrow=tk.LAST)
            _canvas_node(
                canvas,
                center - monitor_width * 0.42,
                closed_top,
                center + monitor_width * 0.42,
                closed_bottom,
                "Position closed",
                "No open quantity",
                BORDER,
                TEXT,
            )

    def _draw_price_rail(self) -> None:
        canvas = getattr(self, "price_rail", None)
        if canvas is None:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 320)
        left, right, y = 28, width - 28, 20
        canvas.create_line(left, y, right, y, fill="#d3deeb", width=2)
        draft = self.draft
        if draft is None:
            return
        target = draft.take_profit
        stop = draft.stop_loss
        current_x = (left + right) / 2
        canvas.create_oval(current_x - 5, y - 5, current_x + 5, y + 5, fill="#eaf2fb", outline=MUTED_TEXT)
        canvas.create_text(
            current_x,
            45,
            text=f"{_money(draft.position_mark)}\nCurrent mark",
            fill=TEXT,
            font=("Segoe UI", 8),
            justify=tk.CENTER,
        )
        if stop and stop.trigger_price is not None:
            canvas.create_line(left, y, current_x, y, fill=DANGER, width=3)
            canvas.create_oval(left - 5, y - 5, left + 5, y + 5, fill=DANGER, outline="#ffb0b2")
            canvas.create_text(
                left,
                45,
                text=f"{_money(stop.trigger_price)}\nStop trigger",
                fill=DANGER,
                font=("Segoe UI", 8),
                justify=tk.CENTER,
            )
        if target and target.trigger_price is not None:
            canvas.create_line(current_x, y, right, y, fill=SUCCESS, width=3)
            canvas.create_oval(right - 5, y - 5, right + 5, y + 5, fill=SUCCESS, outline="#b8ffd5")
            canvas.create_text(
                right,
                45,
                text=f"{_money(target.trigger_price)}\nProfit target",
                fill=SUCCESS,
                font=("Segoe UI", 8),
                justify=tk.CENTER,
            )

    def _review(self) -> None:
        draft = self.draft
        if draft is None or draft.conflicting_order_ids:
            return
        if draft.placeable and draft.template_id == SINGLE_TARGET:
            closing_order = draft.take_profit.closing_order if draft.take_profit else None
            if closing_order is None:
                messagebox.showerror("Exit plan unavailable", "The verified target close is missing.", parent=self)
                return
            self.grab_release()
            self.destroy()
            self.on_review_single_target(closing_order)
            return
        ExitPlanReviewDialog(root=self, draft=draft)

    def _close_now(self) -> None:
        self.grab_release()
        self.destroy()
        self.on_close_now()

    def _show_orders(self) -> None:
        self.grab_release()
        self.destroy()
        self.on_show_orders()

    def _save_template(self) -> None:
        if self.draft is None:
            messagebox.showerror("Template unavailable", "Fix the exit-plan values before saving.", parent=self)
            return
        name = simpledialog.askstring("Save exit-plan template", "Template name:", parent=self)
        if not name:
            return
        try:
            template = SavedExitPlanTemplate(
                name=name.strip(),
                base_template_id=self.template_id.get(),
                target_percent=float(self.target_percent.get()),
                stop_percent=float(self.stop_percent.get()),
                limit_offset=float(self.limit_offset.get()),
                duration=self.duration.get(),
            )
            path = save_exit_plan_template(template)
        except Exception as exc:
            messagebox.showerror("Template not saved", str(exc), parent=self)
            return
        self._load_saved_choices()
        self.saved_choice.set(template.name)
        messagebox.showinfo(
            "Exit-plan template saved",
            f"Saved configuration defaults to {path}.\n\nNo account or OCC contract identity was stored.",
            parent=self,
        )

    def _load_saved_choices(self) -> None:
        try:
            templates = load_exit_plan_templates()
        except Exception as exc:
            templates = ()
            self.builder_message.set(str(exc))
        self.saved_templates = {template.name: template for template in templates}
        self.saved_menu.delete(0, tk.END)
        if not self.saved_templates:
            self.saved_menu.add_command(label="No saved templates", state=tk.DISABLED)
            return
        for name in self.saved_templates:
            self.saved_menu.add_command(
                label=name,
                command=lambda choice=name: self._choose_saved_template(choice),
            )

    def _choose_saved_template(self, name: str) -> None:
        self.saved_choice.set(name)
        self._saved_template_selected()

    def _saved_template_selected(self, _event: object = None) -> None:
        template = self.saved_templates.get(self.saved_choice.get())
        if template is None:
            return
        if not _TEMPLATE_DETAILS[template.base_template_id][2]:
            self.builder_message.set(
                f"{template.name} uses a template that is still visible but not supported."
            )
            return
        self.template_id.set(template.base_template_id)
        self.target_percent.set(f"{template.target_percent:g}")
        self.stop_percent.set(f"{template.stop_percent:g}")
        self.limit_offset.set(f"{template.limit_offset:.2f}")
        self.duration.set(template.duration)
        self._refresh()

    def _package_quantity(self) -> int:
        selected = [leg for leg in self.book.legs if leg.symbol in self.initial_symbols]
        quantities = [int(round(abs(leg.net_quantity))) for leg in selected if abs(leg.net_quantity) >= 1]
        return min(quantities, default=0)


class ExitPlanReviewDialog(tk.Toplevel):
    def __init__(self, *, root: tk.Misc, draft: ExitPlanDraft) -> None:
        super().__init__(root)
        self.draft = draft
        self.title("Review exit plan")
        self.geometry("780x560")
        self.minsize(720, 520)
        self.configure(background=BACKGROUND)
        self.transient(root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._build()
        self.grab_set()
        self.focus_set()

    def _build(self) -> None:
        outer = tk.Frame(self, background=BACKGROUND, padx=14, pady=12)
        outer.pack(fill=tk.BOTH, expand=True)
        tk.Label(outer, text="Review exit plan", background=BACKGROUND, foreground=TEXT, font=("Segoe UI", 17, "bold")).pack(anchor=tk.W)
        tk.Label(
            outer,
            text=f"{self.draft.template_name} · {self.draft.coverage_label} · {self.draft.relationship}",
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, pady=(2, 8))
        body = tk.Frame(outer, background=SURFACE, highlightbackground=BORDER, highlightthickness=1, padx=10, pady=8)
        body.pack(fill=tk.BOTH, expand=True)
        facts = (
            ("Account", self.draft.account_label),
            ("Underlying", self.draft.underlying_symbol),
            ("Current net mark", _money(self.draft.position_mark)),
            ("Protected quantity", str(self.draft.protected_quantity)),
            ("Broker placement", "Available" if self.draft.placeable else "Unavailable"),
        )
        for row, (label, value) in enumerate(facts):
            tk.Label(body, text=label, background=SURFACE, foreground=MUTED_TEXT, font=("Segoe UI", 8)).grid(row=row, column=0, sticky=tk.W, padx=(0, 18), pady=2)
            tk.Label(body, text=value, background=SURFACE, foreground=TEXT, font=("Segoe UI", 9, "bold")).grid(row=row, column=1, sticky=tk.W, pady=2)
        tk.Label(body, text="Exit branches", background=SURFACE, foreground=TEXT, font=("Segoe UI", 10, "bold")).grid(row=6, column=0, columnspan=2, sticky=tk.W, pady=(10, 4))
        branches = ttk.Treeview(body, columns=("branch", "trigger", "order", "limit", "tif"), show="headings", height=4, selectmode="none")
        for name, label, width in (
            ("branch", "Branch", 120),
            ("trigger", "Resolved trigger", 130),
            ("order", "Order type", 105),
            ("limit", "Limit", 90),
            ("tif", "TIF", 110),
        ):
            branches.heading(name, text=label)
            branches.column(name, width=width, stretch=name == "branch")
        branches.grid(row=7, column=0, columnspan=2, sticky=tk.NSEW)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(7, weight=1)
        for branch in self.draft.branches:
            branches.insert("", tk.END, values=(branch.label, _money(branch.trigger_price), branch.order_type.replace("_", " ").title(), _money(branch.limit_price), branch.duration))
        warnings = tk.Frame(body, background="#382a10", highlightbackground=WARNING, highlightthickness=1)
        warnings.grid(row=8, column=0, columnspan=2, sticky=tk.EW, pady=(9, 0))
        tk.Label(
            warnings,
            text="\n".join(f"• {warning}" for warning in self.draft.warnings),
            background="#382a10",
            foreground="#ffd58a",
            font=("Segoe UI", 8),
            wraplength=700,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=9, pady=7)
        footer = tk.Frame(outer, background=BACKGROUND)
        footer.pack(fill=tk.X, pady=(9, 0))
        tk.Label(
            footer,
            text="No broker order will be sent from this review.",
            background=BACKGROUND,
            foreground=WARNING,
            font=("Segoe UI", 8, "bold"),
        ).pack(side=tk.LEFT)
        ttk.Button(footer, text="Back to edit", command=self.destroy).pack(side=tk.RIGHT)
        ttk.Button(footer, text="Placement unavailable", state=tk.DISABLED).pack(side=tk.RIGHT, padx=(0, 7))


def _canvas_node(
    canvas: tk.Canvas,
    left: float,
    top: float,
    right: float,
    bottom: float,
    title: str,
    detail: str,
    outline: str,
    foreground: str,
) -> None:
    canvas.create_rectangle(left, top, right, bottom, outline=outline, width=1, fill=TABLE_FIELD)
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    spacing = min(9.0, max(6.0, (bottom - top) * 0.18))
    canvas.create_text(
        center_x,
        center_y - spacing,
        text=title,
        fill=foreground,
        font=("Segoe UI", 9, "bold"),
        justify=tk.CENTER,
    )
    canvas.create_text(
        center_x,
        center_y + spacing,
        text=detail,
        fill=MUTED_TEXT,
        font=("Segoe UI", 8),
        justify=tk.CENTER,
    )


def _branch_estimate(branch: object) -> str:
    if branch is None:
        return "—"
    price = getattr(branch, "limit_price", None)
    closing_order = getattr(branch, "closing_order", None)
    if price is None or closing_order is None:
        return "Unavailable"
    direction = "CREDIT" if closing_order.estimated_cash_effect >= 0 else "DEBIT"
    return f"{_money(price)} {direction}"


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


__all__ = ["ExitPlanBuilderDialog", "ExitPlanReviewDialog"]
