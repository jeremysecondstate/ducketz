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
        self._template_badges: dict[str, tk.Label] = {}
        self._leg_buttons: dict[str, tk.Checkbutton] = {}
        self._stop_widgets: list[tk.Widget] = []
        self._refresh_after: str | None = None

        self.template_id = tk.StringVar(master=self, value=TARGET_STOP)
        self.coverage_mode = tk.StringVar(
            master=self,
            value="selected" if len(selected_symbols) > 1 else "entire",
        )
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
        self.saved_choice = tk.StringVar(master=self, value="Saved templates")
        self.atomic_link = tk.BooleanVar(master=self, value=False)
        self.activate_after_accept = tk.BooleanVar(master=self, value=True)
        self.sync_quantities = tk.BooleanVar(master=self, value=True)
        self.leg_enabled = {
            leg.symbol: tk.BooleanVar(master=self, value=leg.symbol in selected_symbols)
            for leg in book.legs
            if leg.symbol in selected_symbols
        }

        self.title("Build exit plan")
        self.configure(background=BACKGROUND)
        self.minsize(1060, 690)
        self.transient(root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._fit_to_root()
        self._build()
        self._load_saved_choices()
        self._refresh()
        self.grab_set()
        self.focus_set()

    def _fit_to_root(self) -> None:
        self.root.update_idletasks()
        width = max(1060, self.root.winfo_width() - 24)
        height = max(690, self.root.winfo_height() - 34)
        x = max(0, self.root.winfo_rootx() + 12)
        y = max(0, self.root.winfo_rooty() + 17)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self) -> None:
        outer = tk.Frame(self, background=BACKGROUND, padx=12, pady=10)
        outer.pack(fill=tk.BOTH, expand=True)
        self._build_header(outer)

        body = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        left = tk.Frame(body, background=BACKGROUND)
        right = tk.Frame(body, background=BACKGROUND)
        body.add(left, weight=13)
        body.add(right, weight=7)

        self._build_template_section(left)
        self._build_coverage_section(left)
        self._build_linked_exits(left)
        self._build_quick_actions(left)

        self._build_sequence(right)
        self._build_at_glance(right)
        self._build_safeguards(right)
        self._build_footer(outer)

    def _build_header(self, parent: tk.Frame) -> None:
        header = tk.Frame(parent, background=BACKGROUND)
        header.pack(fill=tk.X)
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
        back.pack(side=tk.LEFT, anchor=tk.N, padx=(0, 14), pady=(3, 0))
        heading = tk.Frame(header, background=BACKGROUND)
        heading.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            heading,
            text="Build exit plan",
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 17, "bold"),
        ).pack(anchor=tk.W)
        first = next((leg for leg in self.book.legs if leg.symbol in self.initial_symbols), None)
        subtitle = (
            f"{first.underlying_symbol} · exact OCC position · {len(self.initial_symbols)} "
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
        pill.pack(side=tk.RIGHT, anchor=tk.N, pady=(3, 0))
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
        saved = ttk.Combobox(
            section.header,
            textvariable=self.saved_choice,
            values=("Saved templates",),
            state="readonly",
            width=20,
        )
        saved.pack(side=tk.RIGHT)
        saved.bind("<<ComboboxSelected>>", self._saved_template_selected)
        self.saved_box = saved

        cards = tk.Frame(section.body, background=SURFACE)
        cards.pack(fill=tk.X, padx=8, pady=(5, 8))
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
            pady=6,
            cursor="hand2" if selectable else "arrow",
        )
        card.grid(row=0, column=column, sticky=tk.NSEW, padx=(0 if column == 0 else 4, 0 if column == 3 else 4))
        # Tk canvases default to a surprisingly large requested width.  Four of
        # those requests force the left pane to consume nearly the whole dialog,
        # even though the icons themselves only use about 100 px.  Keep the
        # request compact and let ``fill=X`` stretch the cards when room exists.
        chart = tk.Canvas(
            card,
            width=110,
            height=28,
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
            font=("Segoe UI", 10, "bold"),
        )
        label.pack()
        detail_label = tk.Label(
            card,
            text=detail,
            background=SURFACE_ALT,
            foreground=MUTED_TEXT if selectable else "#536174",
            font=("Segoe UI", 8),
        )
        detail_label.pack(pady=(1, 0))
        badge = tk.Label(
            card,
            text="",
            background=SURFACE_ALT,
            foreground=ACCENT,
            font=("Segoe UI", 8, "bold"),
        )
        badge.pack(pady=(2, 0))
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
        row.pack(fill=tk.X, padx=8, pady=(4, 8))
        choices = tk.Frame(row, background=SURFACE)
        choices.pack(side=tk.LEFT)
        for value, title, detail in (
            ("entire", "Entire position", "Close as one net order"),
            ("selected", "Selected legs", "Choose exact contracts"),
        ):
            card = tk.Frame(choices, background=TABLE_FIELD, highlightbackground=BORDER, highlightthickness=1)
            card.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 6))
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

        chips = tk.Frame(row, background=TABLE_FIELD, highlightbackground=BORDER, highlightthickness=1, padx=6, pady=5)
        chips.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        by_symbol = {leg.symbol: leg for leg in self.book.legs}
        for symbol, variable in self.leg_enabled.items():
            leg = by_symbol[symbol]
            chip = tk.Checkbutton(
                chips,
                text=f"{leg.strike:g} {leg.option_type.title()}",
                variable=variable,
                command=self._schedule_refresh,
                background=TABLE_FIELD,
                foreground=TEXT,
                activebackground=TABLE_FIELD,
                activeforeground=TEXT,
                selectcolor=SURFACE_ALT,
                font=("Segoe UI", 8),
                borderwidth=0,
                highlightthickness=0,
            )
            chip.pack(side=tk.LEFT, padx=3)
            self._leg_buttons[symbol] = chip

    def _build_linked_exits(self, parent: tk.Frame) -> None:
        section = self._section(parent, "3. Linked exits")
        oco = tk.Label(
            section.header,
            text="ONE CANCELS OTHER",
            background=SURFACE,
            foreground=ACCENT,
            font=("Segoe UI", 8, "bold"),
        )
        oco.pack(side=tk.RIGHT, padx=(0, 10))
        self.oco_label = oco

        content = tk.Frame(section.body, background=SURFACE)
        content.pack(fill=tk.X, padx=8, pady=(4, 6))
        self._build_exit_row(content, "target")
        self._build_exit_row(content, "stop")
        relation = tk.Label(
            content,
            text="🔗  When one fills, cancel the other",
            background=SURFACE,
            foreground=ACCENT,
            font=("Segoe UI", 8),
        )
        relation.pack(anchor=tk.W, pady=(1, 4))
        self.link_relation_label = relation
        time_exit = tk.Frame(content, background=TABLE_FIELD, highlightbackground=BORDER, highlightthickness=1)
        time_exit.pack(fill=tk.X)
        tk.Label(
            time_exit,
            text="＋  Add time-based exit",
            background=TABLE_FIELD,
            foreground="#66758a",
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT, padx=9, pady=6)
        tk.Label(
            time_exit,
            text="Not yet supported",
            background=TABLE_FIELD,
            foreground="#536174",
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT, padx=(5, 0))

    def _build_exit_row(self, parent: tk.Frame, kind: str) -> None:
        is_target = kind == "target"
        color = SUCCESS if is_target else DANGER
        frame = tk.Frame(
            parent,
            background=TABLE_FIELD,
            highlightbackground=color,
            highlightthickness=1,
            padx=7,
            pady=4,
        )
        frame.pack(fill=tk.X, pady=(0, 6))
        if not is_target:
            self.stop_frame = frame
        title_row = tk.Frame(frame, background=TABLE_FIELD)
        title_row.pack(fill=tk.X)
        check = tk.Checkbutton(
            title_row,
            variable=tk.BooleanVar(master=self, value=True),
            state=tk.DISABLED,
            background=TABLE_FIELD,
            activebackground=TABLE_FIELD,
            selectcolor=SURFACE_ALT,
            borderwidth=0,
            highlightthickness=0,
        )
        check.pack(side=tk.LEFT)
        tk.Label(
            title_row,
            text="◉  Take profit" if is_target else "◉  Stop loss",
            background=TABLE_FIELD,
            foreground=color,
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT)
        estimate = self.target_estimate if is_target else self.stop_estimate
        tk.Label(
            title_row,
            textvariable=estimate,
            background=TABLE_FIELD,
            foreground=color,
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.RIGHT)

        controls = tk.Frame(frame, background=TABLE_FIELD)
        controls.pack(fill=tk.X, pady=(4, 0))
        basis = ttk.Combobox(controls, values=("Position mark",), state="readonly", width=13)
        basis.set("Position mark")
        basis.pack(side=tk.LEFT, padx=(0, 5))
        tk.Label(
            controls,
            text="+" if is_target else "−",
            background=TABLE_FIELD,
            foreground=TEXT,
            font=("Segoe UI", 10, "bold"),
        ).pack(side=tk.LEFT, padx=3)
        value_var = self.target_percent if is_target else self.stop_percent
        entry = ttk.Entry(controls, textvariable=value_var, width=7)
        entry.pack(side=tk.LEFT, padx=(0, 3))
        entry.bind("<KeyRelease>", self._schedule_refresh)
        tk.Label(controls, text="%", background=TABLE_FIELD, foreground=MUTED_TEXT).pack(side=tk.LEFT, padx=(0, 7))
        order_type = ttk.Combobox(
            controls,
            values=("Limit",) if is_target else ("Stop limit",),
            state="readonly",
            width=10,
        )
        order_type.set("Limit" if is_target else "Stop limit")
        order_type.pack(side=tk.LEFT, padx=(0, 5))
        if not is_target:
            tk.Label(
                controls,
                text="Offset",
                background=TABLE_FIELD,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8),
            ).pack(side=tk.LEFT, padx=(2, 3))
            offset = ttk.Entry(controls, textvariable=self.limit_offset, width=6)
            offset.pack(side=tk.LEFT, padx=(0, 5))
            offset.bind("<KeyRelease>", self._schedule_refresh)
            self._stop_widgets.extend((basis, entry, order_type, offset))
        duration = ttk.Combobox(
            controls,
            textvariable=self.duration,
            values=(GOOD_UNTIL_CANCELED,),
            state="readonly",
            width=8,
        )
        duration.pack(side=tk.RIGHT)
        duration.bind("<<ComboboxSelected>>", self._schedule_refresh)
        if not is_target:
            self._stop_widgets.append(duration)

    def _build_quick_actions(self, parent: tk.Frame) -> None:
        quick = tk.Frame(parent, background=BACKGROUND)
        quick.pack(fill=tk.X, pady=(7, 0))
        tk.Label(
            quick,
            text="Quick actions route through confirmation",
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT)
        cancel = ttk.Button(quick, text="Cancel working orders", command=self._show_orders)
        cancel.pack(side=tk.RIGHT, padx=(7, 0))
        self.cancel_orders_button = cancel
        ttk.Button(quick, text="Close now…", command=self._close_now).pack(side=tk.RIGHT)

    def _build_sequence(self, parent: tk.Frame) -> None:
        section = self._right_section(parent, "Exit sequence", expand=True)
        # Keep the diagram compact enough for the application's supported
        # minimum height.  The section can still grow when the root is taller.
        canvas = tk.Canvas(section, background=SURFACE, highlightthickness=0, height=175)
        canvas.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))
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
        section = self._right_section(parent, "Safeguards")
        checks = tk.Frame(section, background=SURFACE)
        checks.pack(fill=tk.X, padx=8, pady=(0, 4))
        for text, variable in (
            ("Submit both exits as one linked order (unverified)", self.atomic_link),
            ("Activate only after broker accepts order(s)", self.activate_after_accept),
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
            ).pack(anchor=tk.W)
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
            wraplength=360,
            justify=tk.LEFT,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), pady=5)
        rail = tk.Canvas(section, height=62, background=SURFACE, highlightthickness=0)
        rail.pack(fill=tk.X, padx=8, pady=(0, 5))
        rail.bind("<Configure>", lambda _event: self._draw_price_rail())
        self.price_rail = rail

    def _build_footer(self, parent: tk.Frame) -> None:
        footer = tk.Frame(parent, background=BACKGROUND)
        footer.pack(fill=tk.X, pady=(9, 0))
        ttk.Button(footer, text="Save as template", command=self._save_template).pack(side=tk.LEFT)
        tk.Label(
            footer,
            textvariable=self.builder_message,
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
            wraplength=520,
            justify=tk.CENTER,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=12)
        review = ttk.Button(
            footer,
            text="Review exit plan",
            command=self._review,
            style="ManagementPrimary.TButton",
            state=tk.DISABLED,
        )
        review.pack(side=tk.RIGHT)
        self.review_button = review

    @staticmethod
    def _section(parent: tk.Frame, title: str) -> object:
        frame = tk.Frame(parent, background=SURFACE, highlightbackground=BORDER, highlightthickness=1)
        frame.pack(fill=tk.X, pady=(0, 7))
        header = tk.Frame(frame, background=SURFACE)
        header.pack(fill=tk.X, padx=8, pady=(6, 1))
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
            button.configure(state=tk.DISABLED if self.coverage_mode.get() == "entire" else tk.NORMAL)
        entire = self.coverage_mode.get() == "entire"
        self.entire_coverage_card.configure(highlightbackground=ACCENT if entire else BORDER)
        self.selected_coverage_card.configure(highlightbackground=ACCENT if not entire else BORDER)
        stop_enabled = self.template_id.get() == TARGET_STOP
        for widget in self._stop_widgets:
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="readonly" if stop_enabled else tk.DISABLED)
            else:
                widget.configure(state=tk.NORMAL if stop_enabled else tk.DISABLED)
        self.oco_label.configure(
            text="ONE CANCELS OTHER" if stop_enabled else "SINGLE EXIT",
            foreground=ACCENT if stop_enabled else MUTED_TEXT,
        )
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
        if draft.conflicting_order_ids:
            self.status.set(f"{len(draft.conflicting_order_ids)} close order conflict")
            self.builder_message.set(
                "Resolve working close order " + ", ".join(draft.conflicting_order_ids) + " before review."
            )
            self.review_button.configure(state=tk.DISABLED)
        elif draft.capability_reason:
            self.status.set("Draft · broker linkage unverified")
            self.builder_message.set(
                "Stop-limit orders may not fill during fast moves or price gaps. "
                "Linked Schwab OCO placement remains unverified."
                if draft.template_id == TARGET_STOP
                else draft.capability_reason
            )
            self.review_button.configure(state=tk.NORMAL)
        else:
            self.status.set("Verified single-target close")
            self.builder_message.set(
                "This plan becomes one reviewed GTC limit close; no linked broker order is required."
            )
            self.review_button.configure(state=tk.NORMAL)
        self.cancel_orders_button.configure(state=tk.NORMAL if draft.conflicting_order_ids else tk.DISABLED)
        self._draw_sequence()
        self._draw_price_rail()

    def _refresh_template_cards(self) -> None:
        current = self.template_id.get()
        for template_id, card in self._template_cards.items():
            selectable = _TEMPLATE_DETAILS[template_id][2]
            selected = current == template_id
            card.configure(highlightbackground=ACCENT if selected else BORDER)
            badge = self._template_badges[template_id]
            if selected:
                badge.configure(text="✓ Selected")
            elif not selectable:
                badge.configure(text="Not yet supported", foreground="#66758a")
            elif template_id == TARGET_STOP:
                badge.configure(text="Review only", foreground=WARNING)
            else:
                badge.configure(text="Verified close", foreground=SUCCESS)

    def _draw_sequence(self) -> None:
        canvas = getattr(self, "sequence_canvas", None)
        if canvas is None:
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 360)
        center = width / 2
        draft = self.draft
        if draft is None:
            canvas.create_text(center, 90, text="Complete valid trigger values to preview the sequence.", fill=MUTED_TEXT, font=("Segoe UI", 9))
            return
        _canvas_box(canvas, center - 85, 4, center + 85, 33, f"Existing {draft.underlying_symbol} position\n{len(draft.position_symbols)} exact leg{'s' if len(draft.position_symbols) != 1 else ''}", BORDER, TEXT)
        canvas.create_line(center, 33, center, 44, fill=MUTED_TEXT, arrow=tk.LAST)
        _canvas_box(canvas, center - 78, 45, center + 78, 70, "Monitor position mark", BORDER, TEXT)
        target = draft.take_profit
        stop = draft.stop_loss
        if draft.relationship == "OCO" and target and stop:
            canvas.create_text(center, 80, text="OCO", fill=TEXT, font=("Segoe UI", 9, "bold"))
            canvas.create_line(center, 70, center, 85, fill=MUTED_TEXT)
            canvas.create_line(center - 105, 85, center + 105, 85, fill=MUTED_TEXT)
            canvas.create_line(center - 105, 85, center - 105, 95, fill=MUTED_TEXT, arrow=tk.LAST)
            canvas.create_line(center + 105, 85, center + 105, 95, fill=MUTED_TEXT, arrow=tk.LAST)
            _canvas_box(canvas, center - 180, 96, center - 30, 132, f"{target.trigger_operator}{target.trigger_percent:g}% Take profit\n{_money(target.trigger_price)}", SUCCESS, SUCCESS)
            _canvas_box(canvas, center + 30, 96, center + 180, 132, f"{stop.trigger_operator}{stop.trigger_percent:g}% Stop loss\n{_money(stop.trigger_price)}", DANGER, DANGER)
            canvas.create_line(center - 105, 132, center - 105, 143, center, 151, fill=MUTED_TEXT)
            canvas.create_line(center + 105, 132, center + 105, 143, center, 151, fill=MUTED_TEXT)
            _canvas_box(canvas, center - 70, 151, center + 70, 174, "Position closed", BORDER, TEXT)
        elif target:
            canvas.create_line(center, 70, center, 94, fill=MUTED_TEXT, arrow=tk.LAST)
            _canvas_box(canvas, center - 92, 95, center + 92, 132, f"{target.trigger_operator}{target.trigger_percent:g}% Take profit\n{_money(target.trigger_price)}", SUCCESS, SUCCESS)
            canvas.create_line(center, 132, center, 150, fill=MUTED_TEXT, arrow=tk.LAST)
            _canvas_box(canvas, center - 70, 151, center + 70, 174, "Position closed", BORDER, TEXT)

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
        canvas.create_text(current_x, 45, text=f"{_money(draft.position_mark)}\nCurrent", fill=TEXT, font=("Segoe UI", 8), justify=tk.CENTER)
        if stop and stop.trigger_price is not None:
            canvas.create_line(left, y, current_x, y, fill=DANGER, width=3)
            canvas.create_oval(left - 5, y - 5, left + 5, y + 5, fill=DANGER, outline="#ffb0b2")
            canvas.create_text(left, 45, text=f"{_money(stop.trigger_price)}\nStop", fill=DANGER, font=("Segoe UI", 8), justify=tk.CENTER)
        if target and target.trigger_price is not None:
            canvas.create_line(current_x, y, right, y, fill=SUCCESS, width=3)
            canvas.create_oval(right - 5, y - 5, right + 5, y + 5, fill=SUCCESS, outline="#b8ffd5")
            canvas.create_text(right, 45, text=f"{_money(target.trigger_price)}\nTarget", fill=SUCCESS, font=("Segoe UI", 8), justify=tk.CENTER)

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
        self.saved_box.configure(values=("Saved templates", *self.saved_templates))

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


def _canvas_box(
    canvas: tk.Canvas,
    left: float,
    top: float,
    right: float,
    bottom: float,
    text: str,
    outline: str,
    foreground: str,
) -> None:
    canvas.create_rectangle(left, top, right, bottom, outline=outline, width=1, fill=TABLE_FIELD)
    canvas.create_text((left + right) / 2, (top + bottom) / 2, text=text, fill=foreground, font=("Segoe UI", 8), justify=tk.CENTER)


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
