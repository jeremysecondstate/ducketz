from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from tkinter import messagebox, ttk

from app.models.option_management import (
    OptionOrderReview,
    OptionOrderReviewNotice,
    OrderReviewCashDirection,
    OrderReviewNoticeSeverity,
    OrderReviewOperation,
    OrderReviewOutcomeStatus,
    OrderReviewPlacementCapability,
    OrderReviewPlacementOutcome,
    OrderReviewQuoteState,
)
from app.services.option_order_review import (
    OptionOrderReviewController,
    quote_age_seconds,
)
from app.ui.background_tasks import run_in_background
from app.ui.schwab_order_messages import order_submitted_message
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


_NOTICE_COLORS = {
    OrderReviewNoticeSeverity.INFORMATION: ("#13243a", ACCENT, "i"),
    OrderReviewNoticeSeverity.WARNING: ("#382a10", WARNING, "!"),
    OrderReviewNoticeSeverity.BLOCKING: ("#35171d", DANGER, "X"),
}


class _ScrollableReviewBody(tk.Frame):
    def __init__(self, parent: tk.Misc, *, minimum_content_width: int = 980) -> None:
        super().__init__(parent, background=BACKGROUND)
        self.minimum_content_width = minimum_content_width
        self.canvas = tk.Canvas(
            self,
            background=BACKGROUND,
            highlightthickness=0,
            borderwidth=0,
        )
        self.vertical = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.horizontal = ttk.Scrollbar(self, orient=tk.HORIZONTAL, command=self.canvas.xview)
        self.inner = tk.Frame(self.canvas, background=BACKGROUND)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor=tk.NW)
        self.canvas.configure(
            yscrollcommand=self._vertical_changed,
            xscrollcommand=self._horizontal_changed,
        )
        self.canvas.grid(row=0, column=0, sticky=tk.NSEW)
        self.vertical.grid(row=0, column=1, sticky=tk.NS)
        self.horizontal.grid(row=1, column=0, sticky=tk.EW)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.inner.bind("<Configure>", self._content_resized)
        self.canvas.bind("<Configure>", self._canvas_resized)
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)

    def _content_resized(self, _event: object = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _canvas_resized(self, event: object) -> None:
        width = max(self.minimum_content_width, int(getattr(event, "width", 1)))
        self.canvas.itemconfigure(self._window, width=width)
        self._content_resized()

    def _vertical_changed(self, first: str, last: str) -> None:
        self.vertical.set(first, last)
        if float(first) <= 0.0 and float(last) >= 0.999999:
            self.vertical.grid_remove()
        else:
            self.vertical.grid()

    def _horizontal_changed(self, first: str, last: str) -> None:
        self.horizontal.set(first, last)
        if float(first) <= 0.0 and float(last) >= 0.999999:
            self.horizontal.grid_remove()
        else:
            self.horizontal.grid()

    def _bind_wheel(self, _event: object = None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _unbind_wheel(self, _event: object = None) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _wheel(self, event: object) -> str:
        delta = int(getattr(event, "delta", 0))
        if delta:
            self.canvas.yview_scroll(-int(delta / 120), "units")
        return "break"


class OptionOrderReviewDialog(tk.Toplevel):
    """One native review surface for Close, Roll, and Exit Plan drafts."""

    def __init__(
        self,
        *,
        root: tk.Misc,
        controller: OptionOrderReviewController,
        on_back: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(root)
        self.root = root
        self.controller = controller
        self.on_back = on_back
        self._prior_listener = controller.state_listener
        self.controller.state_listener = self._controller_changed
        self._rendered_review: OptionOrderReview | None = None
        self._refresh_dispatched = False
        self._placement_dispatched = False
        self._clock_after: str | None = None
        self._prior_grab = self.grab_current()
        self._inline_status = ""
        self._cost_value_labels: dict[str, tk.Label] = {}
        self.price_var = tk.StringVar(master=self)
        self.acknowledged_var = tk.BooleanVar(master=self, value=False)
        self.quote_label_var = tk.StringVar(master=self)
        self.quote_age_var = tk.StringVar(master=self)
        self.state_var = tk.StringVar(master=self)

        self.title(controller.review.title)
        self.configure(background=BACKGROUND)
        self.minsize(820, 600)
        self.transient(root)
        self.protocol("WM_DELETE_WINDOW", self._leave)
        self._apply_styles()
        self._fit_to_root()
        self._build()
        self.bind("<Escape>", lambda _event: self._leave())
        self.bind("<Control-Return>", lambda _event: self._primary_action())
        self.grab_set()
        self.after_idle(self._establish_focus)
        self.after_idle(self._start_initial_refresh)
        self._tick_quote_clock()

    def _apply_styles(self) -> None:
        style = ttk.Style(self)
        style.configure(
            "OrderReview.Treeview",
            background=TABLE_FIELD,
            fieldbackground=TABLE_FIELD,
            foreground=TEXT,
            bordercolor=BORDER,
            rowheight=30,
            font=("Segoe UI", 8),
        )
        style.configure(
            "OrderReview.Treeview.Heading",
            background=SURFACE_ALT,
            foreground=MUTED_TEXT,
            bordercolor=BORDER,
            relief=tk.FLAT,
            font=("Segoe UI", 8, "bold"),
        )
        style.map(
            "OrderReview.Treeview.Heading",
            background=[("active", "#203a5a")],
            foreground=[("active", TEXT)],
        )
        style.configure(
            "OrderReview.Secondary.TButton",
            background=TABLE_FIELD,
            foreground=TEXT,
            bordercolor=BORDER,
            font=("Segoe UI", 10),
            padding=(14, 8),
        )
        style.map(
            "OrderReview.Secondary.TButton",
            background=[("active", SURFACE_ALT), ("disabled", BACKGROUND)],
            foreground=[("disabled", "#536174")],
            bordercolor=[("focus", ACCENT), ("disabled", BORDER)],
        )
        style.configure(
            "OrderReview.Place.TButton",
            background=SUCCESS,
            foreground="#04130a",
            bordercolor=SUCCESS,
            font=("Segoe UI", 10, "bold"),
            padding=(18, 9),
        )
        style.map(
            "OrderReview.Place.TButton",
            background=[("active", "#4ade80"), ("disabled", SURFACE_ALT)],
            foreground=[("disabled", MUTED_TEXT)],
            bordercolor=[("focus", "#b8ffd5"), ("disabled", BORDER)],
        )
        style.configure(
            "OrderReview.Finish.TButton",
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
            font=("Segoe UI", 10, "bold"),
            padding=(18, 9),
        )
        style.map(
            "OrderReview.Finish.TButton",
            background=[("active", "#2799f4"), ("disabled", SURFACE_ALT)],
            foreground=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "OrderReview.Price.TSpinbox",
            fieldbackground=TABLE_FIELD,
            foreground=TEXT,
            bordercolor=BORDER,
            arrowcolor=TEXT,
            padding=(6, 6),
        )

    def _fit_to_root(self) -> None:
        self.root.update_idletasks()
        root_width = max(1, self.root.winfo_width())
        root_height = max(1, self.root.winfo_height())
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        if root_width <= 10:
            root_width = screen_width
        if root_height <= 10:
            root_height = screen_height
        width = min(1140, max(820, root_width - 32), screen_width - 24)
        height = min(790, max(600, root_height - 48), screen_height - 48)
        root_x = self.root.winfo_rootx()
        root_y = self.root.winfo_rooty()
        x = root_x + (root_width - width) // 2
        y = root_y + (root_height - height) // 2
        x = min(max(0, x), max(0, screen_width - width))
        y = min(max(0, y), max(0, screen_height - height))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self) -> None:
        outer = tk.Frame(self, background=BACKGROUND)
        outer.pack(fill=tk.BOTH, expand=True)
        self._build_header(outer)
        tk.Frame(outer, height=1, background=BORDER).pack(fill=tk.X)
        body = _ScrollableReviewBody(outer, minimum_content_width=980)
        body.pack(fill=tk.BOTH, expand=True, padx=13, pady=10)
        self.body = body
        self._build_footer(outer)
        self._render_body()
        self._sync_controls()

    def _build_header(self, parent: tk.Frame) -> None:
        header = tk.Frame(parent, background=BACKGROUND, padx=18, pady=13)
        header.pack(fill=tk.X)
        heading = tk.Frame(header, background=BACKGROUND)
        heading.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.title_label = tk.Label(
            heading,
            text=self.controller.review.title,
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 19, "bold"),
            anchor=tk.W,
        )
        self.title_label.pack(anchor=tk.W)
        self.subtitle_label = tk.Label(
            heading,
            text=self.controller.review.subtitle,
            background=BACKGROUND,
            foreground="#c4cfdd",
            font=("Segoe UI", 10),
            anchor=tk.W,
        )
        self.subtitle_label.pack(anchor=tk.W, pady=(2, 0))

        close = tk.Button(
            header,
            text="Close  ×",
            command=self._leave,
            background=BACKGROUND,
            foreground=TEXT,
            activebackground=SURFACE_ALT,
            activeforeground=TEXT,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            highlightthickness=1,
            borderwidth=0,
            font=("Segoe UI", 9),
            padx=8,
            pady=4,
            takefocus=True,
        )
        close.pack(side=tk.RIGHT, anchor=tk.N, padx=(16, 0))
        self.close_button = close
        quote = tk.Frame(header, background=BACKGROUND)
        quote.pack(side=tk.RIGHT, anchor=tk.N, pady=(1, 0))
        line = tk.Frame(quote, background=BACKGROUND)
        line.pack(anchor=tk.E)
        self.quote_dot = tk.Label(
            line,
            text="●",
            background=BACKGROUND,
            foreground=SUCCESS,
            font=("Segoe UI", 9, "bold"),
        )
        self.quote_dot.pack(side=tk.LEFT, padx=(0, 5))
        self.quote_label = tk.Label(
            line,
            textvariable=self.quote_label_var,
            background=BACKGROUND,
            foreground=SUCCESS,
            font=("Segoe UI", 9, "bold"),
        )
        self.quote_label.pack(side=tk.LEFT)
        tk.Label(
            quote,
            textvariable=self.quote_age_var,
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(anchor=tk.E, pady=(2, 0))

    def _build_footer(self, parent: tk.Frame) -> None:
        tk.Frame(parent, height=1, background=BORDER).pack(fill=tk.X)
        footer = tk.Frame(parent, background="#091524", padx=16, pady=10)
        footer.pack(fill=tk.X)
        safety = tk.Frame(footer, background="#091524")
        safety.pack(side=tk.LEFT, fill=tk.X, expand=True, anchor=tk.CENTER)
        tk.Label(
            safety,
            text="CLOSE ONLY",
            background="#091524",
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 8))
        self.safety_label = tk.Label(
            safety,
            text=self.controller.review.safety_copy,
            background="#091524",
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
            wraplength=430,
            justify=tk.LEFT,
            anchor=tk.W,
        )
        self.safety_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

        actions = tk.Frame(footer, background="#091524")
        actions.pack(side=tk.RIGHT)
        back = ttk.Button(
            actions,
            text="Back to edit",
            command=self._back,
            style="OrderReview.Secondary.TButton",
            takefocus=True,
        )
        back.grid(row=0, column=0, sticky=tk.EW, padx=(0, 7))
        save = ttk.Button(
            actions,
            text="Save order",
            command=self._save,
            style="OrderReview.Secondary.TButton",
            state=tk.DISABLED,
            takefocus=False,
        )
        save.grid(row=0, column=1, sticky=tk.EW, padx=(0, 7))
        capability = self.controller.review.placement_capability
        style = (
            "OrderReview.Place.TButton"
            if capability == OrderReviewPlacementCapability.SUPPORTED
            else "OrderReview.Finish.TButton"
        )
        self.primary_button = ttk.Button(
            actions,
            text=self._primary_button_text(),
            command=self._primary_action,
            style=style,
            state=tk.DISABLED,
            takefocus=True,
        )
        self.primary_button.grid(row=0, column=2, sticky=tk.EW)
        tk.Label(
            actions,
            text="Saved orders are unavailable in this application.",
            background="#091524",
            foreground="#66758a",
            font=("Segoe UI", 7),
        ).grid(row=1, column=1, sticky=tk.E, padx=(0, 8), pady=(3, 0))
        self.state_label = tk.Label(
            actions,
            textvariable=self.state_var,
            background="#091524",
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        )
        self.state_label.grid(row=1, column=2, sticky=tk.E, pady=(3, 0))
        self.back_button = back
        self.save_button = save

    def _render_body(self) -> None:
        for child in self.body.inner.winfo_children():
            child.destroy()
        review = self.controller.review
        self._rendered_review = review
        self._cost_value_labels = {}
        self.body.inner.grid_columnconfigure(0, weight=3, uniform="review-columns", minsize=570)
        self.body.inner.grid_columnconfigure(1, weight=2, uniform="review-columns", minsize=380)
        left = tk.Frame(self.body.inner, background=BACKGROUND)
        right = tk.Frame(self.body.inner, background=BACKGROUND)
        left.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 5))
        right.grid(row=0, column=1, sticky=tk.NSEW, padx=(5, 0))
        self._build_order_summary(left, review)
        self._build_price_panel(left, review)
        self._build_effects(right, review)
        self._build_costs(right, review)
        self._build_notices(right, review)
        self._build_acknowledgment(right, review)
        self.body.inner.after_idle(self.body._content_resized)

    def _card(self, parent: tk.Misc, *, padding: tuple[int, int] = (11, 9)) -> tk.Frame:
        card = tk.Frame(
            parent,
            background=SURFACE,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            highlightthickness=1,
            padx=padding[0],
            pady=padding[1],
        )
        card.pack(fill=tk.X, pady=(0, 8))
        return card

    @staticmethod
    def _section_title(parent: tk.Misc, text: str) -> tk.Label:
        label = tk.Label(
            parent,
            text=text,
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 12, "bold"),
            anchor=tk.W,
        )
        label.pack(anchor=tk.W, fill=tk.X)
        return label

    def _build_order_summary(self, parent: tk.Frame, review: OptionOrderReview) -> None:
        card = self._card(parent)
        self._section_title(card, "Order summary")
        facts = tk.Frame(card, background=SURFACE)
        facts.pack(fill=tk.X, pady=(7, 8))
        facts.grid_columnconfigure(1, weight=1)
        for row, (label, value) in enumerate(
            (
                ("Account", review.account_display_label),
                ("Strategy / purpose", review.strategy_label),
                ("Instruction", review.instruction),
                ("Order type", review.order_type),
                ("Time in force", review.duration),
                ("Execution", review.execution_mode),
            )
        ):
            background = SURFACE if row % 2 == 0 else "#0e1a2b"
            tk.Label(
                facts,
                text=label,
                background=background,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 9),
                anchor=tk.W,
                padx=7,
                pady=5,
            ).grid(row=row, column=0, sticky=tk.NSEW)
            tk.Label(
                facts,
                text=value or "Unavailable",
                background=background,
                foreground=TEXT,
                font=("Segoe UI", 9, "bold" if label in {"Instruction", "Execution"} else "normal"),
                anchor=tk.W,
                justify=tk.LEFT,
                wraplength=450,
                padx=7,
                pady=5,
            ).grid(row=row, column=1, sticky=tk.NSEW)

        tk.Label(
            card,
            text=f"Exact legs ({len(review.legs)})",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 10, "bold"),
            anchor=tk.W,
        ).pack(anchor=tk.W, pady=(1, 4))
        table_frame = tk.Frame(card, background=TABLE_FIELD, highlightbackground=BORDER, highlightthickness=1)
        table_frame.pack(fill=tk.BOTH, expand=True)
        columns = (
            ("role", "Role", 100, tk.W),
            ("action", "Action", 88, tk.W),
            ("quantity", "Qty", 30, tk.E),
            ("contract", "Contract", 132, tk.W),
            ("symbol", "Exact OCC symbol", 135, tk.W),
            ("bid", "Bid", 40, tk.E),
            ("ask", "Ask", 40, tk.E),
            ("mark", "Mark", 40, tk.E),
        )
        table = ttk.Treeview(
            table_frame,
            columns=tuple(column[0] for column in columns),
            show="headings",
            selectmode="none",
            height=min(6, max(2, len(review.legs))),
            style="OrderReview.Treeview",
            takefocus=True,
        )
        for name, label, width, anchor in columns:
            table.heading(name, text=label)
            table.column(name, width=width, minwidth=30, anchor=anchor, stretch=False)
        vertical = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=table.yview)
        horizontal = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=table.xview)
        table.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        table_frame.grid_rowconfigure(0, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)
        table.grid(row=0, column=0, sticky=tk.NSEW)
        vertical.grid(row=0, column=1, sticky=tk.NS)
        horizontal.grid(row=1, column=0, sticky=tk.EW)
        table.tag_configure("buy", foreground=SUCCESS)
        table.tag_configure("sell", foreground=DANGER)
        table.tag_configure("unavailable", foreground=WARNING)
        for leg in review.legs:
            action = leg.action.lower()
            tag = "buy" if action.startswith("buy") else "sell" if action.startswith("sell") else "unavailable"
            table.insert(
                "",
                tk.END,
                values=(
                    leg.role,
                    leg.action,
                    _quantity(leg.quantity),
                    leg.contract_label,
                    leg.symbol,
                    _money(leg.bid),
                    _money(leg.ask),
                    _money(leg.mark),
                ),
                tags=(tag,),
            )

    def _build_price_panel(self, parent: tk.Frame, review: OptionOrderReview) -> None:
        card = self._card(parent, padding=(13, 11))
        top = tk.Frame(card, background=SURFACE)
        top.pack(fill=tk.X)
        value = tk.Frame(top, background=SURFACE)
        value.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            value,
            text=review.price_title,
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W)
        direction = review.cash_direction.value
        color = _direction_color(review.cash_direction)
        tk.Label(
            value,
            text=f"{_money(review.net_price)}  {direction}",
            background=SURFACE,
            foreground=color,
            font=("Segoe UI", 19, "bold"),
        ).pack(anchor=tk.W, pady=(3, 0))
        editor = tk.Frame(top, background=SURFACE, padx=8)
        editor.pack(side=tk.RIGHT, anchor=tk.N)
        tk.Label(
            editor,
            text="Limit price" if review.price_editable else "Price editing",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(anchor=tk.W)
        self.price_var.set("" if review.net_price is None else f"{review.net_price:.2f}")
        if review.price_editable:
            spin = ttk.Spinbox(
                editor,
                textvariable=self.price_var,
                from_=0.01,
                to=99_999.99,
                increment=0.01,
                width=11,
                justify=tk.RIGHT,
                style="OrderReview.Price.TSpinbox",
                command=self._price_changed,
                takefocus=True,
            )
            spin.pack(anchor=tk.W, pady=(3, 0))
            spin.bind("<Return>", self._price_changed)
            spin.bind("<FocusOut>", self._price_changed)
            self.price_spin = spin
        else:
            tk.Label(
                editor,
                text="Read only",
                background=TABLE_FIELD,
                foreground=MUTED_TEXT,
                highlightbackground=BORDER,
                highlightthickness=1,
                font=("Segoe UI", 9, "bold"),
                padx=12,
                pady=6,
            ).pack(anchor=tk.W, pady=(3, 0))
            self.price_spin = None
        tk.Label(
            card,
            text=review.price_editor_explanation,
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
            wraplength=580,
            justify=tk.LEFT,
            anchor=tk.W,
        ).pack(fill=tk.X, pady=(7, 1))
        rail = tk.Canvas(card, height=74, background=SURFACE, highlightthickness=0)
        rail.pack(fill=tk.X, pady=(0, 1))
        rail.bind("<Configure>", lambda _event: self._draw_price_rail())
        self.price_rail_canvas = rail
        estimate = tk.Frame(card, background=TABLE_FIELD, highlightbackground=BORDER, highlightthickness=1, padx=9, pady=7)
        estimate.pack(fill=tk.X, pady=(3, 0))
        tk.Label(
            estimate,
            text=review.estimated_cash_label,
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT)
        amount = "Unavailable" if review.estimated_cash_effect is None else _money(abs(review.estimated_cash_effect))
        tk.Label(
            estimate,
            text=amount,
            background=TABLE_FIELD,
            foreground=color if review.estimated_cash_effect is not None else MUTED_TEXT,
            font=("Segoe UI", 10, "bold"),
        ).pack(side=tk.RIGHT)
        tk.Label(
            card,
            text=f"Source: {review.price_provenance}",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
            anchor=tk.W,
        ).pack(fill=tk.X, pady=(5, 0))
        self.after_idle(self._draw_price_rail)

    def _draw_price_rail(self) -> None:
        canvas = getattr(self, "price_rail_canvas", None)
        if canvas is None or not canvas.winfo_exists():
            return
        canvas.delete("all")
        width = max(canvas.winfo_width(), 320)
        left, right, y = 22, width - 22, 29
        rail = self.controller.review.price_rail
        if rail is None:
            canvas.create_line(left, y, right, y, fill=BORDER, width=3)
            canvas.create_text(width / 2, y + 21, text="Bid / midpoint / ask rail unavailable for this review", fill=MUTED_TEXT, font=("Segoe UI", 8))
            return
        span = max(rail.ask - rail.bid, 0.01)
        selected_x = left + (min(max(rail.selected, rail.bid), rail.ask) - rail.bid) / span * (right - left)
        midpoint_x = left + (rail.midpoint - rail.bid) / span * (right - left)
        color = _direction_color(self.controller.review.cash_direction)
        canvas.create_line(left, y, right, y, fill="#d1d9e4", width=2)
        canvas.create_line(left, y, midpoint_x, y, fill=color, width=3)
        canvas.create_oval(selected_x - 5, y - 5, selected_x + 5, y + 5, fill=TEXT, outline=BACKGROUND)
        canvas.create_line(selected_x, y - 12, selected_x, y + 12, fill=TEXT, width=1)
        labels = ((left, "Bid", rail.bid, tk.W), (midpoint_x, "Mid", rail.midpoint, tk.CENTER), (right, "Ask", rail.ask, tk.E))
        for x, label, value, anchor in labels:
            canvas.create_text(x, y + 23, text=f"{label}  {_money(value)}", fill=MUTED_TEXT if label != "Mid" else TEXT, font=("Segoe UI", 8, "bold" if label == "Mid" else "normal"), anchor=anchor)

    def _build_effects(self, parent: tk.Frame, review: OptionOrderReview) -> None:
        card = self._card(parent)
        self._section_title(card, "What changes")
        grid = tk.Frame(card, background=SURFACE)
        grid.pack(fill=tk.X, pady=(7, 0))
        grid.grid_columnconfigure(0, weight=3)
        grid.grid_columnconfigure(1, weight=2)
        grid.grid_columnconfigure(2, minsize=25)
        grid.grid_columnconfigure(3, weight=2)
        for column, label in enumerate(("", "Before", "", "After")):
            tk.Label(
                grid,
                text=label,
                background=SURFACE_ALT,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8, "bold"),
                padx=6,
                pady=5,
            ).grid(row=0, column=column, sticky=tk.EW)
        for index, metric in enumerate(review.metrics, start=1):
            background = TABLE_FIELD if index % 2 else SURFACE
            value_row = index * 2 - 1
            source_row = index * 2
            tk.Label(grid, text=metric.label, background=background, foreground=TEXT, font=("Segoe UI", 9), anchor=tk.W, padx=7, pady=6).grid(row=value_row, column=0, sticky=tk.NSEW)
            tk.Label(grid, text=metric.before, background=background, foreground=_tone_color(metric.before_tone), font=("Segoe UI", 9), anchor=tk.E, padx=7, pady=6).grid(row=value_row, column=1, sticky=tk.NSEW)
            tk.Label(grid, text="→", background=background, foreground=MUTED_TEXT, font=("Segoe UI", 10, "bold"), padx=3).grid(row=value_row, column=2, sticky=tk.NSEW)
            tk.Label(grid, text=metric.after, background=background, foreground=_tone_color(metric.after_tone), font=("Segoe UI", 9, "bold"), anchor=tk.E, justify=tk.RIGHT, wraplength=145, padx=7, pady=6).grid(row=value_row, column=3, sticky=tk.NSEW)
            tk.Label(grid, text=f"Source: {metric.provenance}", background=background, foreground=MUTED_TEXT, font=("Segoe UI", 7), anchor=tk.W, justify=tk.LEFT, wraplength=330, padx=7, pady=1).grid(row=source_row, column=0, columnspan=4, sticky=tk.EW, pady=(0, 4))

    def _build_costs(self, parent: tk.Frame, review: OptionOrderReview) -> None:
        card = self._card(parent)
        self._section_title(card, "Costs & timing")
        rows = tk.Frame(card, background=SURFACE)
        rows.pack(fill=tk.X, pady=(6, 0))
        rows.grid_columnconfigure(0, weight=1)
        for row, cost in enumerate(review.costs):
            background = TABLE_FIELD if row % 2 == 0 else SURFACE
            label_copy = f"{cost.label}{' (estimate)' if cost.estimated else ''}"
            tk.Label(rows, text=label_copy, background=background, foreground=MUTED_TEXT, font=("Segoe UI", 9), anchor=tk.W, padx=7, pady=5).grid(row=row * 2, column=0, sticky=tk.NSEW)
            value = tk.Label(rows, text=cost.value, background=background, foreground=_tone_color(cost.tone), font=("Segoe UI", 9, "bold"), anchor=tk.E, padx=7, pady=5)
            value.grid(row=row * 2, column=1, sticky=tk.NSEW)
            tk.Label(rows, text=f"Source: {cost.provenance}", background=background, foreground=MUTED_TEXT, font=("Segoe UI", 7), anchor=tk.W, padx=7, pady=1).grid(row=row * 2 + 1, column=0, columnspan=2, sticky=tk.EW, pady=(0, 4))
            self._cost_value_labels[cost.label] = value

    def _build_notices(self, parent: tk.Frame, review: OptionOrderReview) -> None:
        notices = self._effective_notices(review)
        if not notices:
            return
        card = self._card(parent, padding=(8, 8))
        heading = "Action required" if any(notice.blocking for notice in notices) else "Execution notes"
        self._section_title(card, heading)
        for notice in notices:
            background, color, icon = _NOTICE_COLORS[notice.severity]
            rail = tk.Frame(card, background=background, highlightbackground=color, highlightthickness=1)
            rail.pack(fill=tk.X, pady=(6, 0))
            tk.Label(rail, text=icon, background=background, foreground=color, font=("Segoe UI", 10, "bold"), width=3).pack(side=tk.LEFT, padx=(5, 1), pady=7)
            copy = tk.Frame(rail, background=background)
            copy.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(1, 7), pady=6)
            tk.Label(copy, text=notice.title, background=background, foreground=color, font=("Segoe UI", 8, "bold"), anchor=tk.W).pack(fill=tk.X)
            tk.Label(copy, text=notice.detail, background=background, foreground=TEXT, font=("Segoe UI", 8), wraplength=350, justify=tk.LEFT, anchor=tk.W).pack(fill=tk.X, pady=(2, 0))

    def _effective_notices(self, review: OptionOrderReview) -> tuple[OptionOrderReviewNotice, ...]:
        notices = [
            notice
            for notice in review.notices
            if notice.title != "Data provenance and revalidation"
            and not notice.title.startswith("Limit price is ")
        ]
        titles = {notice.title.casefold() for notice in notices}
        if review.quote_state == OrderReviewQuoteState.UPDATING and "refreshing positions and quotes" not in titles:
            notices.insert(0, OptionOrderReviewNotice(OrderReviewNoticeSeverity.INFORMATION, "Refreshing positions and quotes", "Current account positions and exact-leg quotes are being revalidated."))
        elif review.quote_state == OrderReviewQuoteState.AGING and "quote is aging" not in titles:
            notices.insert(0, OptionOrderReviewNotice(OrderReviewNoticeSeverity.WARNING, "Quote is aging", "The reviewed quote is still within the placement limit but is approaching its refresh threshold."))
        elif review.quote_state == OrderReviewQuoteState.STALE and "stale quote" not in titles:
            notices.insert(0, OptionOrderReviewNotice(OrderReviewNoticeSeverity.BLOCKING, "Stale quote", "The reviewed quote aged past the placement limit. Refresh and acknowledge again.", blocking=True))
        elif review.quote_state == OrderReviewQuoteState.UNAVAILABLE and "quote unavailable" not in titles and "refresh failed" not in titles:
            notices.insert(0, OptionOrderReviewNotice(OrderReviewNoticeSeverity.BLOCKING, "Quote unavailable", "A required quote timestamp is unavailable. Placement is blocked.", blocking=True))
        return tuple(notices)

    def _build_acknowledgment(self, parent: tk.Frame, review: OptionOrderReview) -> None:
        card = self._card(parent, padding=(10, 8))
        check = tk.Checkbutton(
            card,
            text=review.acknowledgment_copy,
            variable=self.acknowledged_var,
            command=self._acknowledgment_changed,
            background=SURFACE,
            foreground=TEXT,
            activebackground=SURFACE,
            activeforeground=TEXT,
            selectcolor=TABLE_FIELD,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            highlightthickness=1,
            borderwidth=0,
            font=("Segoe UI", 9),
            wraplength=350,
            justify=tk.LEFT,
            anchor=tk.W,
            takefocus=True,
            padx=5,
            pady=6,
        )
        check.pack(fill=tk.X)
        if review.placement_disabled_reason:
            tk.Label(
                card,
                text=f"Reason: {review.placement_disabled_reason}",
                background=SURFACE,
                foreground=WARNING if review.placement_capability == OrderReviewPlacementCapability.UNAVAILABLE else MUTED_TEXT,
                font=("Segoe UI", 8),
                wraplength=350,
                justify=tk.LEFT,
                anchor=tk.W,
            ).pack(fill=tk.X, padx=5, pady=(3, 1))
        self.acknowledgment_check = check

    def _price_changed(self, event: object = None) -> str | None:
        review = self.controller.review
        if not review.price_editable:
            return None
        try:
            current = float(self.price_var.get())
        except ValueError:
            current = float("nan")
        if review.net_price is not None and current == review.net_price:
            return "break" if getattr(event, "keysym", "") == "Return" else None
        try:
            self.controller.set_limit_price(self.price_var.get())
        except Exception as exc:
            self._inline_status = str(exc) or "The reviewed price is invalid."
            self.price_var.set("" if review.net_price is None else f"{review.net_price:.2f}")
        else:
            self._inline_status = "Price changed — confirmation and preview state were reset."
        self._sync_from_controller()
        return "break" if getattr(event, "keysym", "") == "Return" else None

    def _acknowledgment_changed(self) -> None:
        self._inline_status = ""
        self.controller.acknowledge(self.acknowledged_var.get())
        self._sync_controls()

    def _primary_action(self) -> None:
        review = self.controller.review
        if review.placement_capability == OrderReviewPlacementCapability.REVIEW_ONLY:
            if self.controller.finish_review():
                self._leave()
            return
        if review.placement_capability != OrderReviewPlacementCapability.SUPPORTED:
            return
        if self._placement_dispatched or not self.controller.can_place:
            return
        self._placement_dispatched = True
        self._inline_status = ""
        self._sync_controls()
        run_in_background(self, self.controller.place, self._placement_finished, self._placement_crashed)

    def _placement_finished(self, outcome: OrderReviewPlacementOutcome) -> None:
        if outcome.status == OrderReviewOutcomeStatus.ACCEPTED and outcome.submission is not None:
            payload = outcome.submission.payload
            location = outcome.submission.location
            operation = self.controller.review.operation
            self._destroy_modal()
            messagebox.showinfo(
                "Exit order accepted" if operation == OrderReviewOperation.EXIT_PLAN else "Closing order accepted",
                order_submitted_message(payload, location),
                parent=self.root,
            )
            return
        if outcome.retryable:
            self._placement_dispatched = False
        self._inline_status = outcome.message
        self._sync_from_controller()
        if outcome.status == OrderReviewOutcomeStatus.UNKNOWN:
            messagebox.showwarning("Submission result unknown", outcome.message, parent=self)
        elif outcome.status not in {OrderReviewOutcomeStatus.BLOCKED, OrderReviewOutcomeStatus.INVALIDATED}:
            messagebox.showerror("Order not submitted", outcome.message, parent=self)

    def _placement_crashed(self, exc: Exception) -> None:
        self._inline_status = f"Unexpected review error: {type(exc).__name__}: {exc}"
        self._placement_dispatched = self.controller.state.value == "UNKNOWN"
        self._sync_from_controller()
        messagebox.showerror("Order not submitted", self._inline_status, parent=self)

    def _start_initial_refresh(self) -> None:
        if not self.winfo_exists() or self._refresh_dispatched or not self.controller.supports_background_refresh:
            return
        self._refresh_dispatched = True
        self._sync_controls()

        def succeeded(_review: OptionOrderReview) -> None:
            self._refresh_dispatched = False
            self._inline_status = "Positions and quotes refreshed — confirmation required."
            self._sync_from_controller()

        def failed(_exc: Exception) -> None:
            self._refresh_dispatched = False
            self._inline_status = "Refresh unavailable; reviewed data retained — Place will revalidate."
            self._sync_from_controller()

        run_in_background(self.root, self.controller.refresh_review, succeeded, failed)

    def _controller_changed(self, controller: OptionOrderReviewController) -> None:
        prior = self._prior_listener
        if prior is not None:
            prior(controller)
        try:
            self.after(0, self._sync_from_controller)
        except tk.TclError:
            return

    def _sync_from_controller(self) -> None:
        if not self.winfo_exists():
            return
        if self._rendered_review != self.controller.review:
            self._render_body()
        self._sync_controls()

    def _sync_controls(self) -> None:
        if not self.winfo_exists():
            return
        review = self.controller.review
        self.title(review.title)
        self.title_label.configure(text=review.title)
        self.subtitle_label.configure(text=review.subtitle)
        self.safety_label.configure(text=review.safety_copy)
        self.acknowledged_var.set(self.controller.acknowledged)
        enabled = self.controller.primary_action_enabled and not self._placement_dispatched and not self._refresh_dispatched
        self.primary_button.configure(
            text=self._primary_button_text(),
            state=tk.NORMAL if enabled else tk.DISABLED,
        )
        busy = self.controller.state.value in {"REVALIDATING", "PREVIEWING", "FALLBACK", "SUBMITTING"}
        self.back_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.close_button.configure(state=tk.DISABLED if busy else tk.NORMAL)
        self.acknowledgment_check.configure(state=tk.DISABLED if busy else tk.NORMAL)
        if getattr(self, "price_spin", None) is not None:
            self.price_spin.configure(state=tk.DISABLED if busy or self._refresh_dispatched else tk.NORMAL)
        if review.quote_state in {OrderReviewQuoteState.STALE, OrderReviewQuoteState.UNAVAILABLE}:
            visible_state = self.controller.state_text
        elif busy:
            visible_state = self.controller.state_text
        else:
            visible_state = self._inline_status or self.controller.state_text
        self.state_var.set(visible_state)
        if (
            self.controller.state.value in {"REJECTED", "UNKNOWN"}
            or review.quote_state in {OrderReviewQuoteState.STALE, OrderReviewQuoteState.UNAVAILABLE}
            or review.has_blocking_notice
        ):
            state_color = DANGER
        elif review.quote_state == OrderReviewQuoteState.AGING:
            state_color = WARNING
        elif self.controller.primary_action_enabled:
            state_color = SUCCESS
        else:
            state_color = MUTED_TEXT
        self.state_label.configure(foreground=state_color)
        self._update_quote_header()

    def _primary_button_text(self) -> str:
        review = self.controller.review
        return review.primary_action_label

    def _update_quote_header(self) -> None:
        review = self.controller.review
        state = review.quote_state
        labels = {
            OrderReviewQuoteState.LIVE: ("LIVE QUOTE", SUCCESS),
            OrderReviewQuoteState.AGING: ("AGING QUOTE", WARNING),
            OrderReviewQuoteState.STALE: ("STALE QUOTE", DANGER),
            OrderReviewQuoteState.UPDATING: ("UPDATING", ACCENT),
            OrderReviewQuoteState.UNAVAILABLE: ("QUOTE UNAVAILABLE", DANGER),
        }
        label, color = labels[state]
        self.quote_label_var.set(label)
        self.quote_dot.configure(foreground=color)
        self.quote_label.configure(foreground=color)
        age = quote_age_seconds(review.display_quote_at, now=datetime.now().astimezone())
        if state == OrderReviewQuoteState.UPDATING:
            age_text = "Refreshing positions and exact-leg quotes…"
        elif age is None:
            age_text = "Timestamp unavailable"
        else:
            age_text = f"Updated {_human_age(age)} ago"
        self.quote_age_var.set(age_text)
        quote_cost = self._cost_value_labels.get("Quote age")
        if quote_cost is not None:
            safety_age = quote_age_seconds(review.validation_quote_at, now=datetime.now().astimezone())
            quote_cost.configure(text="Unavailable" if safety_age is None else _human_age(safety_age))

    def _tick_quote_clock(self) -> None:
        if not self.winfo_exists():
            return
        self.controller.age_quotes(now=datetime.now().astimezone())
        self._update_quote_header()
        self._sync_controls()
        self._clock_after = self.after(1000, self._tick_quote_clock)

    def _establish_focus(self) -> None:
        if self.winfo_exists():
            self.acknowledgment_check.focus_set()

    def _save(self) -> None:
        # Deliberately non-submitting. There is no executable saved-draft store.
        self.controller.save_order()
        self._inline_status = "Save order is unavailable; no saved-order persistence exists."
        self._sync_controls()

    def _back(self) -> None:
        self.controller.abandon_review()
        callback = self.on_back
        self._destroy_modal()
        if callback is not None:
            callback()

    def _leave(self) -> None:
        if self.controller.state.value in {"REVALIDATING", "PREVIEWING", "FALLBACK", "SUBMITTING"}:
            return
        self.controller.abandon_review()
        self._destroy_modal()

    def _destroy_modal(self) -> None:
        prior_grab = self._prior_grab
        if self._clock_after is not None:
            try:
                self.after_cancel(self._clock_after)
            except tk.TclError:
                pass
            self._clock_after = None
        self.controller.state_listener = self._prior_listener
        try:
            self.grab_release()
        except tk.TclError:
            pass
        if self.winfo_exists():
            self.destroy()
        if prior_grab is not None:
            try:
                if prior_grab.winfo_exists():
                    prior_grab.grab_set()
                    prior_grab.focus_set()
            except tk.TclError:
                pass


def _direction_color(direction: OrderReviewCashDirection) -> str:
    if direction == OrderReviewCashDirection.CREDIT:
        return SUCCESS
    if direction == OrderReviewCashDirection.DEBIT:
        return DANGER
    return TEXT


def _tone_color(tone: str) -> str:
    if tone == "positive":
        return SUCCESS
    if tone == "negative":
        return DANGER
    if tone == "warning":
        return WARNING
    if tone == "unavailable":
        return MUTED_TEXT
    return TEXT


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _quantity(value: int | None) -> str:
    return "—" if value is None else str(value)


def _human_age(seconds: float) -> str:
    total = max(0, int(seconds))
    if total < 60:
        return f"{total} sec"
    minutes, remainder = divmod(total, 60)
    return f"{minutes}m {remainder:02d}s"


__all__ = ["OptionOrderReviewDialog"]
