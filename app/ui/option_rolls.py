from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from app.models.option_management import (
    OptionPositionBook,
    OptionPositionLeg,
    RollChainSnapshot,
    RollMetricSnapshot,
    RollOrderDraft,
    RollOrderLeg,
    SavedRollTemplate,
)
from app.services.option_rolls import (
    DEFAULT_MAX_QUOTE_AGE_SECONDS,
    ROLL_EXECUTION_ATOMIC,
    ROLL_EXECUTION_NON_ATOMIC,
    ROLL_PRICE_MANUAL,
    ROLL_PRICE_MID,
    ROLL_PRICE_NATURAL,
    ROLL_SCOPE_ENTIRE,
    ROLL_SCOPE_SELECTED,
    build_roll_order_draft,
    eligible_roll_expirations,
    load_roll_templates,
    parse_roll_chain,
    refresh_roll_order_draft,
    save_roll_template,
    suggest_replacement_contracts,
)
from app.services.option_order_review import OptionOrderReviewController, roll_order_review
from app.services.schwab_option_management import option_position_book
from app.services.schwab_strategy_orders import DAY_ONLY, GOOD_UNTIL_CANCELED
from app.ui.background_tasks import run_in_background
from app.ui.option_order_review import OptionOrderReviewDialog
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


class RollWorkspaceController:
    """Pure presenter state for the roll workspace; it performs no I/O."""

    def __init__(
        self,
        *,
        book: OptionPositionBook,
        position_symbols: tuple[str, ...],
        atomic_order_supported: bool = False,
        fee_per_contract: float | None = None,
        now_provider: Callable[[], datetime] | None = None,
        max_quote_age_seconds: float = DEFAULT_MAX_QUOTE_AGE_SECONDS,
    ) -> None:
        self.book = book
        by_symbol = {leg.symbol: leg for leg in book.legs}
        self.position_symbols = tuple(
            dict.fromkeys(
                str(symbol).strip().upper()
                for symbol in position_symbols
                if str(symbol).strip()
            )
        )
        missing = [symbol for symbol in self.position_symbols if symbol not in by_symbol]
        if not self.position_symbols:
            raise ValueError("Select at least one exact option position to roll.")
        if missing:
            raise ValueError("Selected option position is unavailable: " + ", ".join(missing))
        self._position_by_symbol = by_symbol
        self.scope_mode = ROLL_SCOPE_ENTIRE
        self.leg_enabled = {symbol: True for symbol in self.position_symbols}
        self.keep_strike_widths = True
        self.duration = DAY_ONLY
        self.price_policy = ROLL_PRICE_MID
        self.preferred_days_forward = 30
        self.expiration: str | None = None
        self.chain: RollChainSnapshot | None = None
        self.draft: RollOrderDraft | None = None
        self.error: str | None = "Loading exact option-chain contracts…"
        self.atomic_order_supported = atomic_order_supported
        self.fee_per_contract = fee_per_contract
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.max_quote_age_seconds = max_quote_age_seconds
        self._manual_price: float | None = None

    @property
    def position_legs(self) -> tuple[OptionPositionLeg, ...]:
        return tuple(self._position_by_symbol[symbol] for symbol in self.position_symbols)

    @property
    def active_symbols(self) -> tuple[str, ...]:
        if self.scope_mode == ROLL_SCOPE_ENTIRE:
            return self.position_symbols
        return tuple(symbol for symbol in self.position_symbols if self.leg_enabled[symbol])

    @property
    def active_legs(self) -> tuple[OptionPositionLeg, ...]:
        return tuple(self._position_by_symbol[symbol] for symbol in self.active_symbols)

    @property
    def expirations(self) -> tuple[str, ...]:
        if self.chain is None:
            return ()
        return eligible_roll_expirations(
            self.active_legs,
            self.chain,
            now=self.now_provider(),
        )

    @property
    def can_review(self) -> bool:
        return self.draft is not None and self.draft.review_eligible and self.error is None

    def load_chain(self, payload: object) -> None:
        underlying = self.position_legs[0].underlying_symbol
        self.chain = (
            payload
            if isinstance(payload, RollChainSnapshot)
            else parse_roll_chain(
                payload,
                expected_underlying=underlying,
                observed_at=self.now_provider(),
            )
        )
        self.expiration = self._suggested_expiration(self.preferred_days_forward)
        self._manual_price = None
        self._rebuild()

    def set_scope(self, scope: str) -> None:
        if scope == ROLL_SCOPE_SELECTED and len(self.position_symbols) < 2:
            raise ValueError("Selected-leg scope requires a multi-leg position.")
        if scope not in {ROLL_SCOPE_ENTIRE, ROLL_SCOPE_SELECTED}:
            raise ValueError(f"Unknown roll scope: {scope or 'missing'}")
        self.scope_mode = scope
        if scope == ROLL_SCOPE_ENTIRE:
            for symbol in self.leg_enabled:
                self.leg_enabled[symbol] = True
        self._configuration_changed(reselect_expiration=True)

    def set_leg_enabled(self, symbol: str, enabled: bool) -> None:
        if symbol not in self.leg_enabled:
            raise ValueError(f"Unknown position leg: {symbol}")
        if self.scope_mode != ROLL_SCOPE_SELECTED:
            raise ValueError("Choose Selected legs before changing individual roll legs.")
        self.leg_enabled[symbol] = bool(enabled)
        self._configuration_changed(reselect_expiration=True)

    def set_expiration(self, expiration: str) -> None:
        if expiration not in self.expirations:
            raise ValueError("Choose an available later expiration from the current chain.")
        self.expiration = expiration
        self._configuration_changed(reselect_expiration=False)

    def set_keep_strike_widths(self, enabled: bool) -> None:
        self.keep_strike_widths = bool(enabled)
        self._configuration_changed(reselect_expiration=False)

    def set_duration(self, duration: str) -> None:
        self.duration = duration
        self._rebuild()

    def select_midpoint(self) -> None:
        self._manual_price = None
        self.price_policy = ROLL_PRICE_MID
        self._rebuild()

    def set_manual_price(self, value: object) -> None:
        try:
            price = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("Roll limit price must be a positive number.") from exc
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Roll limit price must be a positive number.")
        self._manual_price = round(price + 1e-12, 2)
        self.price_policy = ROLL_PRICE_MANUAL
        self._rebuild()

    def adjust_price(self, amount: float) -> None:
        if self.draft is None:
            raise ValueError("A current roll market is required before adjusting price.")
        self.set_manual_price(max(0.01, self.draft.limit_price + amount))

    def apply_template(self, template: SavedRollTemplate) -> None:
        self.preferred_days_forward = template.days_forward
        self.keep_strike_widths = template.keep_strike_widths
        self.duration = template.duration
        self.price_policy = template.price_policy
        self._manual_price = None
        self.expiration = self._suggested_expiration(template.days_forward)
        self._rebuild()

    def accept_refreshed_draft(self, draft: RollOrderDraft) -> None:
        self.draft = draft
        self.book = OptionPositionBook(
            account_label=draft.account_label,
            observed_at=draft.reviewed_position_at,
            status=self.book.status,
            legs=self.book.legs,
            summary=self.book.summary,
            unavailable_reasons=self.book.unavailable_reasons,
        )
        self.error = None

    def route_review(self, callback: Callable[[RollOrderDraft], None]) -> None:
        if not self.can_review or self.draft is None:
            raise ValueError(self.error or "Roll draft is not ready for review.")
        callback(self.draft)

    def _configuration_changed(self, *, reselect_expiration: bool) -> None:
        self._manual_price = None
        self.price_policy = ROLL_PRICE_MID
        if reselect_expiration and self.chain is not None:
            available = self.expirations
            if self.expiration not in available:
                self.expiration = self._suggested_expiration(self.preferred_days_forward)
        self._rebuild()

    def _suggested_expiration(self, days_forward: int) -> str | None:
        available = self.expirations
        if not available or not self.active_legs:
            return None
        latest = max(date.fromisoformat(leg.expiration[:10]) for leg in self.active_legs)
        target = latest + timedelta(days=days_forward)
        return min(available, key=lambda value: abs((date.fromisoformat(value) - target).days))

    def _rebuild(self) -> None:
        self.draft = None
        if self.chain is None:
            self.error = "Loading exact option-chain contracts…"
            return
        if not self.active_legs:
            self.error = "Select at least one exact leg to roll."
            return
        if self.expiration is None:
            self.error = "No later exact expiration supports the selected position legs."
            return
        try:
            replacements = suggest_replacement_contracts(
                self.active_legs,
                self.chain,
                expiration=self.expiration,
                keep_strike_widths=self.keep_strike_widths,
            )
            self.draft = build_roll_order_draft(
                self.book,
                self.position_symbols,
                self.active_symbols,
                replacements,
                scope_mode=self.scope_mode,
                keep_strike_widths=self.keep_strike_widths,
                duration=self.duration,
                limit_price=self._manual_price,
                price_policy=self.price_policy,
                atomic_order_supported=self.atomic_order_supported,
                underlying_price=self.chain.underlying_price,
                fee_per_contract=self.fee_per_contract,
                now=self.now_provider(),
                max_quote_age_seconds=self.max_quote_age_seconds,
            )
        except Exception as exc:
            self.error = str(exc) or "Roll draft is unavailable."
            self.draft = None
            return
        self.error = None


class _VerticalScrolledFrame(tk.Frame):
    def __init__(self, parent: tk.Misc, *, background: str) -> None:
        super().__init__(parent, background=background)
        self.canvas = tk.Canvas(
            self,
            background=background,
            borderwidth=0,
            highlightthickness=0,
        )
        self.scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, background=background)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor=tk.NW)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.inner.bind("<Configure>", self._content_resized)
        self.canvas.bind("<Configure>", self._canvas_resized)
        self.canvas.bind("<Enter>", lambda _event: self.canvas.bind_all("<MouseWheel>", self._wheel))
        self.canvas.bind("<Leave>", lambda _event: self.canvas.unbind_all("<MouseWheel>"))

    def _content_resized(self, _event: object = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._update_scrollbar()

    def _canvas_resized(self, event: object) -> None:
        self.canvas.itemconfigure(self._window, width=max(1, int(getattr(event, "width", 1))))
        self._update_scrollbar()

    def _wheel(self, event: object) -> str:
        delta = int(getattr(event, "delta", 0))
        if delta:
            self.canvas.yview_scroll(-int(delta / 120), "units")
        return "break"

    def _update_scrollbar(self) -> None:
        self.update_idletasks()
        needed = self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1
        if needed:
            self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        else:
            self.scrollbar.pack_forget()


class RollWorkspaceDialog(tk.Toplevel):
    def __init__(
        self,
        *,
        root: tk.Tk,
        book: OptionPositionBook,
        position_symbols: tuple[str, ...],
        snapshot_loader: Callable[[], object],
        chain_loader: Callable[[], object],
        on_review: Callable[[RollOrderDraft], None] | None = None,
        atomic_order_supported: bool = False,
        fee_per_contract: float | None = None,
        now_provider: Callable[[], datetime] | None = None,
        template_path: Path | None = None,
    ) -> None:
        super().__init__(root)
        self.root = root
        self.snapshot_loader = snapshot_loader
        self.chain_loader = chain_loader
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.template_path = template_path
        self.controller = RollWorkspaceController(
            book=book,
            position_symbols=position_symbols,
            atomic_order_supported=atomic_order_supported,
            fee_per_contract=fee_per_contract,
            now_provider=self.now_provider,
        )
        self.on_review = on_review or (lambda draft: _show_roll_review(self, draft, self.now_provider()))
        self._busy = False
        self._syncing = False
        self._step = 1
        self._expiration_by_label: dict[str, str] = {}
        self._template_by_name: dict[str, SavedRollTemplate] = {}
        self._scope_cards: dict[str, tk.Frame] = {}
        self._scope_radios: dict[str, tk.Radiobutton] = {}
        self._leg_vars = {
            symbol: tk.BooleanVar(master=self, value=True)
            for symbol in self.controller.position_symbols
        }

        self.scope_var = tk.StringVar(master=self, value=ROLL_SCOPE_ENTIRE)
        self.expiration_var = tk.StringVar(master=self)
        self.keep_widths_var = tk.BooleanVar(master=self, value=True)
        self.duration_var = tk.StringVar(master=self, value=DAY_ONLY)
        self.order_type_var = tk.StringVar(master=self, value="Unavailable")
        self.limit_price_var = tk.StringVar(master=self)
        self.net_price_var = tk.StringVar(master=self, value="—")
        self.net_kind_var = tk.StringVar(master=self)
        self.bid_var = tk.StringVar(master=self, value="Bid —")
        self.mid_var = tk.StringVar(master=self, value="Mid —")
        self.ask_var = tk.StringVar(master=self, value="Ask —")
        self.expiration_context_var = tk.StringVar(master=self, value="Waiting for chain")
        self.status_var = tk.StringVar(master=self, value="Loading option chain")
        self.warning_var = tk.StringVar(master=self, value="Loading current contracts and quotes…")
        self.net_result_var = tk.StringVar(master=self, value="Net result unavailable")
        self.analysis_mode = tk.StringVar(master=self, value="payoff")

        self.title("Roll option position")
        self.configure(background=BACKGROUND)
        self.minsize(980, 680)
        self.transient(root)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._apply_styles()
        self._build()
        self._fit_to_root()
        self._load_saved_templates()
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Control-Return>", lambda _event: self._review())
        self.after_idle(self._load_chain)
        self.grab_set()
        self.focus_set()

    def _apply_styles(self) -> None:
        style = ttk.Style(self)
        style.configure(
            "Roll.Primary.TButton",
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
            font=("Segoe UI", 10, "bold"),
            padding=(13, 8),
        )
        style.map(
            "Roll.Primary.TButton",
            background=[("active", "#2799f4"), ("disabled", SURFACE_ALT)],
            foreground=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "Roll.Compact.TButton",
            background=TABLE_FIELD,
            foreground=TEXT,
            bordercolor=BORDER,
            font=("Segoe UI", 9),
            padding=(8, 5),
        )
        style.map("Roll.Compact.TButton", background=[("active", SURFACE_ALT)])
        style.configure(
            "Roll.Active.TButton",
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
            font=("Segoe UI", 9, "bold"),
            padding=(10, 5),
        )

    def _fit_to_root(self) -> None:
        self.root.update_idletasks()
        width = max(980, self.root.winfo_width() - 18)
        height = max(680, self.root.winfo_height() - 28)
        x = max(0, self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2)
        y = max(0, self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self) -> None:
        outer = tk.Frame(self, background=BACKGROUND, padx=10, pady=9)
        outer.pack(fill=tk.BOTH, expand=True)
        self._build_header(outer)
        panes = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, pady=(7, 0))
        left_scroll = _VerticalScrolledFrame(panes, background=BACKGROUND)
        right_scroll = _VerticalScrolledFrame(panes, background=BACKGROUND)
        panes.add(left_scroll, weight=51)
        panes.add(right_scroll, weight=49)
        self._build_configuration(left_scroll.inner)
        self._build_analysis(right_scroll.inner)
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
            highlightthickness=0,
            font=("Segoe UI", 10),
            cursor="hand2",
            takefocus=True,
        )
        back.pack(side=tk.LEFT, anchor=tk.N, padx=(0, 14), pady=(3, 0))
        title = tk.Frame(header, background=BACKGROUND)
        title.pack(side=tk.LEFT, anchor=tk.N)
        tk.Label(
            title,
            text=_roll_title(self.controller.position_legs),
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor=tk.W)
        tk.Label(
            title,
            textvariable=self.status_var,
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        ).pack(anchor=tk.W, pady=(1, 0))
        steps = tk.Frame(header, background=BACKGROUND)
        steps.pack(side=tk.LEFT, padx=(42, 0), pady=(5, 0))
        self._step_widgets: list[tuple[tk.Canvas, tk.Label]] = []
        for index, label in enumerate(("Configure", "Analyze", "Review"), start=1):
            if index > 1:
                tk.Frame(steps, width=28, height=1, background=BORDER).pack(side=tk.LEFT, padx=8)
            circle = tk.Canvas(steps, width=24, height=24, background=BACKGROUND, highlightthickness=0)
            circle.pack(side=tk.LEFT)
            text = tk.Label(
                steps,
                text=label,
                background=BACKGROUND,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 9),
            )
            text.pack(side=tk.LEFT, padx=(5, 0))
            self._step_widgets.append((circle, text))
        self._draw_steps()
        templates = tk.Menubutton(
            header,
            text="Templates  ▾",
            background=TABLE_FIELD,
            foreground=TEXT,
            activebackground=SURFACE_ALT,
            activeforeground=TEXT,
            highlightbackground=BORDER,
            highlightthickness=1,
            borderwidth=0,
            font=("Segoe UI", 9),
            padx=10,
            pady=5,
            cursor="hand2",
        )
        templates.pack(side=tk.RIGHT, anchor=tk.N, pady=(3, 0))
        self.template_menu = tk.Menu(templates, tearoff=False, background=SURFACE_ALT, foreground=TEXT)
        templates.configure(menu=self.template_menu)

    def _build_configuration(self, parent: tk.Frame) -> None:
        surface = tk.Frame(
            parent,
            background=SURFACE,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        surface.pack(fill=tk.BOTH, expand=True, padx=(0, 5), pady=(0, 2))
        scope = tk.Frame(surface, background=SURFACE, padx=12, pady=11)
        scope.pack(fill=tk.X)
        for column, (value, title, detail) in enumerate(
            (
                (ROLL_SCOPE_ENTIRE, "Entire strategy" if len(self.controller.position_symbols) > 1 else "Entire position", "Close and replace every confirmed leg"),
                (ROLL_SCOPE_SELECTED, "Selected legs", "Choose exact legs to roll"),
            )
        ):
            scope.grid_columnconfigure(column, weight=1, uniform="roll-scope")
            card = tk.Frame(
                scope,
                background=TABLE_FIELD,
                highlightbackground=BORDER,
                highlightthickness=1,
                padx=8,
                pady=5,
            )
            card.grid(row=0, column=column, sticky=tk.NSEW, padx=(0, 5) if column == 0 else (5, 0))
            radio = tk.Radiobutton(
                card,
                text=title,
                variable=self.scope_var,
                value=value,
                command=self._scope_changed,
                background=TABLE_FIELD,
                foreground=TEXT,
                activebackground=TABLE_FIELD,
                activeforeground=TEXT,
                selectcolor=SURFACE_ALT,
                font=("Segoe UI", 9, "bold"),
                borderwidth=0,
                highlightthickness=0,
                anchor=tk.W,
                takefocus=True,
            )
            radio.pack(fill=tk.X)
            tk.Label(
                card,
                text=detail,
                background=TABLE_FIELD,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8),
                anchor=tk.W,
            ).pack(fill=tk.X, padx=(22, 0), pady=(0, 2))
            self._scope_cards[value] = card
            self._scope_radios[value] = radio
        if len(self.controller.position_symbols) < 2:
            self._scope_radios[ROLL_SCOPE_SELECTED].configure(state=tk.DISABLED)

        self._divider(surface)
        close_section = tk.Frame(surface, background=SURFACE, padx=12, pady=9)
        close_section.pack(fill=tk.X)
        self._section_heading(close_section, "1. Position to close")
        self.close_table = tk.Frame(close_section, background=TABLE_FIELD, highlightbackground=BORDER, highlightthickness=1)
        self.close_table.pack(fill=tk.X, pady=(7, 0))
        self._build_close_rows()
        self.close_symbols_label = tk.Label(
            close_section,
            text="",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Cascadia Mono", 7),
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=720,
        )
        self.close_symbols_label.pack(fill=tk.X, pady=(4, 0))

        connector = tk.Canvas(surface, height=38, background=SURFACE, highlightthickness=0)
        connector.pack(fill=tk.X, padx=12)
        connector.bind("<Configure>", lambda _event: self._draw_roll_connector(connector))

        self._divider(surface)
        replacement = tk.Frame(surface, background=SURFACE, padx=12, pady=9)
        replacement.pack(fill=tk.X)
        top = tk.Frame(replacement, background=SURFACE)
        top.pack(fill=tk.X)
        self._section_heading(top, "2. Replacement position", pack=False).pack(side=tk.LEFT)
        expiration_box = ttk.Combobox(
            top,
            textvariable=self.expiration_var,
            state="readonly",
            width=23,
            takefocus=True,
        )
        expiration_box.pack(side=tk.RIGHT, padx=(7, 0))
        expiration_box.bind("<<ComboboxSelected>>", self._expiration_changed)
        self.expiration_box = expiration_box
        tk.Label(
            top,
            text="Expiration",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(side=tk.RIGHT)
        context = tk.Frame(replacement, background=SURFACE)
        context.pack(fill=tk.X, pady=(6, 6))
        tk.Label(
            context,
            textvariable=self.expiration_context_var,
            background=SURFACE,
            foreground=ACCENT,
            font=("Segoe UI", 8, "bold"),
        ).pack(side=tk.RIGHT)
        keep = tk.Checkbutton(
            context,
            text="Keep strike widths",
            variable=self.keep_widths_var,
            command=self._keep_widths_changed,
            indicatoron=False,
            background=TABLE_FIELD,
            foreground=TEXT,
            activebackground=SURFACE_ALT,
            activeforeground=TEXT,
            selectcolor=ACCENT,
            font=("Segoe UI", 8, "bold"),
            borderwidth=0,
            highlightbackground=BORDER,
            highlightthickness=1,
            padx=9,
            pady=3,
            takefocus=True,
        )
        keep.pack(side=tk.RIGHT, padx=(0, 8))
        self.replacement_table = tk.Frame(replacement, background=TABLE_FIELD, highlightbackground=BORDER, highlightthickness=1)
        self.replacement_table.pack(fill=tk.X)
        self._build_replacement_rows()
        self.replacement_symbols_label = tk.Label(
            replacement,
            text="",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Cascadia Mono", 7),
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=720,
        )
        self.replacement_symbols_label.pack(fill=tk.X, pady=(4, 0))

        self._divider(surface)
        order = tk.Frame(surface, background=SURFACE, padx=12, pady=9)
        order.pack(fill=tk.X, pady=(0, 5))
        self._section_heading(order, "3. Net order")
        terms = tk.Frame(order, background=SURFACE)
        terms.pack(fill=tk.X, pady=(7, 0))
        left = tk.Frame(terms, background=SURFACE)
        left.pack(side=tk.LEFT, anchor=tk.N)
        tk.Label(left, text="Order type", background=SURFACE, foreground=MUTED_TEXT, font=("Segoe UI", 8)).grid(row=0, column=0, sticky=tk.W)
        order_type = ttk.Combobox(left, textvariable=self.order_type_var, state="readonly", width=18, takefocus=True)
        order_type.grid(row=1, column=0, sticky=tk.W, pady=(3, 0))
        self.order_type_box = order_type
        tk.Label(left, text="Time in force", background=SURFACE, foreground=MUTED_TEXT, font=("Segoe UI", 8)).grid(row=0, column=1, sticky=tk.W, padx=(9, 0))
        duration = ttk.Combobox(
            left,
            textvariable=self.duration_var,
            values=(DAY_ONLY, GOOD_UNTIL_CANCELED),
            state="readonly",
            width=18,
            takefocus=True,
        )
        duration.grid(row=1, column=1, sticky=tk.W, padx=(9, 0), pady=(3, 0))
        duration.bind("<<ComboboxSelected>>", self._duration_changed)
        self.duration_box = duration
        price = tk.Frame(terms, background=SURFACE)
        price.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(25, 0))
        tk.Label(price, text="Net price", background=SURFACE, foreground=MUTED_TEXT, font=("Segoe UI", 8)).pack(anchor=tk.W)
        price_line = tk.Frame(price, background=SURFACE)
        price_line.pack(fill=tk.X)
        self.net_price_label = tk.Label(
            price_line,
            textvariable=self.net_price_var,
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 18, "bold"),
        )
        self.net_price_label.pack(side=tk.LEFT)
        self.net_kind_label = tk.Label(
            price_line,
            textvariable=self.net_kind_var,
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 13, "bold"),
        )
        self.net_kind_label.pack(side=tk.LEFT, padx=(7, 0), pady=(4, 0))
        controls = tk.Frame(order, background=SURFACE)
        controls.pack(fill=tk.X, pady=(9, 0))
        ttk.Button(controls, text="−0.01", command=lambda: self._adjust_price(-0.01), style="Roll.Compact.TButton").pack(side=tk.LEFT)
        ttk.Button(controls, text="MID", command=self._select_mid, style="Roll.Compact.TButton").pack(side=tk.LEFT, padx=5)
        ttk.Button(controls, text="+0.01", command=lambda: self._adjust_price(0.01), style="Roll.Compact.TButton").pack(side=tk.LEFT)
        entry = ttk.Entry(controls, textvariable=self.limit_price_var, width=9, justify=tk.RIGHT, takefocus=True)
        entry.pack(side=tk.RIGHT)
        entry.bind("<Return>", self._manual_price_changed)
        entry.bind("<FocusOut>", self._manual_price_changed)
        tk.Label(controls, text="Limit", background=SURFACE, foreground=MUTED_TEXT, font=("Segoe UI", 8)).pack(side=tk.RIGHT, padx=(0, 6))
        self.limit_entry = entry
        rail = tk.Canvas(order, height=76, background=SURFACE, highlightthickness=0)
        rail.pack(fill=tk.X, pady=(3, 0))
        rail.bind("<Configure>", lambda _event: self._draw_price_rail())
        self.price_rail = rail

    def _build_analysis(self, parent: tk.Frame) -> None:
        surface = tk.Frame(
            parent,
            background=SURFACE,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        surface.pack(fill=tk.BOTH, expand=True, padx=(5, 0), pady=(0, 2))
        heading = tk.Frame(surface, background=SURFACE, padx=12, pady=10)
        heading.pack(fill=tk.X)
        tk.Label(
            heading,
            text="Before / after",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 17, "bold"),
        ).pack(anchor=tk.W)
        switch = tk.Frame(heading, background=SURFACE)
        switch.pack(fill=tk.X, pady=(7, 0))
        self.payoff_button = ttk.Button(
            switch,
            text="Payoff",
            command=lambda: self._set_analysis_mode("payoff"),
            style="Roll.Active.TButton",
            takefocus=True,
        )
        self.payoff_button.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.greeks_button = ttk.Button(
            switch,
            text="Greeks",
            command=lambda: self._set_analysis_mode("greeks"),
            style="Roll.Compact.TButton",
            takefocus=True,
        )
        self.greeks_button.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.payoff_button.bind("<Alt-p>", lambda _event: self._set_analysis_mode("payoff"))
        self.greeks_button.bind("<Alt-g>", lambda _event: self._set_analysis_mode("greeks"))

        visual = tk.Frame(surface, background=SURFACE, padx=10)
        visual.pack(fill=tk.X)
        chart = tk.Canvas(visual, height=280, background=SURFACE, highlightthickness=0, takefocus=True)
        chart.pack(fill=tk.X)
        chart.bind("<Configure>", lambda _event: self._draw_payoff_chart())
        self.payoff_canvas = chart
        greeks = tk.Frame(visual, height=280, background=TABLE_FIELD, highlightbackground=BORDER, highlightthickness=1)
        greeks.pack_propagate(False)
        self.greeks_panel = greeks
        self._build_greeks_panel()

        self._divider(surface)
        metrics = tk.Frame(surface, background=SURFACE, padx=10, pady=7)
        metrics.pack(fill=tk.X)
        self.metrics_frame = metrics
        self._build_metrics()

        facts = tk.Frame(surface, background=TABLE_FIELD, highlightbackground=BORDER, highlightthickness=1, padx=9, pady=6)
        facts.pack(fill=tk.X, padx=10, pady=(2, 7))
        self.fact_values: dict[str, tk.Label] = {}
        for column, (key, label) in enumerate(
            (
                ("realized", "Realized P/L est."),
                ("days", "Days extended"),
                ("fees", "Fees est."),
                ("execution", "Execution"),
            )
        ):
            facts.grid_columnconfigure(column, weight=1, uniform="roll-facts")
            cell = tk.Frame(facts, background=TABLE_FIELD)
            cell.grid(row=0, column=column, sticky=tk.EW, padx=(0 if column == 0 else 5, 0))
            tk.Label(cell, text=label, background=TABLE_FIELD, foreground=MUTED_TEXT, font=("Segoe UI", 8)).pack(anchor=tk.W)
            value = tk.Label(cell, text="—", background=TABLE_FIELD, foreground=TEXT, font=("Segoe UI", 9, "bold"), wraplength=180, justify=tk.LEFT)
            value.pack(anchor=tk.W, pady=(2, 0))
            self.fact_values[key] = value

        warning = tk.Frame(surface, background=SURFACE_ALT, highlightbackground=WARNING, highlightthickness=1)
        warning.pack(fill=tk.X, padx=10, pady=(0, 7))
        tk.Label(warning, text="⚠", background=SURFACE_ALT, foreground=WARNING, font=("Segoe UI Symbol", 12, "bold")).pack(side=tk.LEFT, padx=(8, 6), pady=6)
        tk.Label(
            warning,
            textvariable=self.warning_var,
            background=SURFACE_ALT,
            foreground=WARNING,
            font=("Segoe UI", 8, "bold"),
            wraplength=690,
            justify=tk.LEFT,
            anchor=tk.W,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), pady=6)
        result = tk.Frame(surface, background=TABLE_FIELD, highlightbackground=BORDER, highlightthickness=1)
        result.pack(fill=tk.X, padx=10, pady=(0, 10))
        self.net_result_label = tk.Label(
            result,
            textvariable=self.net_result_var,
            background=TABLE_FIELD,
            foreground=TEXT,
            font=("Segoe UI", 9, "bold"),
            pady=8,
            wraplength=710,
        )
        self.net_result_label.pack(fill=tk.X)

    def _build_greeks_panel(self) -> None:
        tk.Label(
            self.greeks_panel,
            text="Aggregate position Greeks",
            background=TABLE_FIELD,
            foreground=TEXT,
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor=tk.W, padx=16, pady=(18, 4))
        tk.Label(
            self.greeks_panel,
            text="Contract Greeks × signed quantity × multiplier. Missing broker values remain unavailable.",
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
            wraplength=650,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, padx=16, pady=(0, 15))
        grid = tk.Frame(self.greeks_panel, background=TABLE_FIELD)
        grid.pack(fill=tk.X, padx=16)
        for column, label in enumerate(("Greek", "Before", "", "After roll")):
            grid.grid_columnconfigure(column, weight=1 if column != 2 else 0)
            tk.Label(grid, text=label, background=SURFACE_ALT, foreground=MUTED_TEXT, font=("Segoe UI", 9, "bold"), padx=8, pady=6).grid(row=0, column=column, sticky=tk.EW)
        self.greeks_values: dict[str, tuple[tk.Label, tk.Label]] = {}
        for row, (key, label) in enumerate((("delta", "Delta"), ("theta", "Theta / day")), start=1):
            bg = TABLE_FIELD if row % 2 else SURFACE
            tk.Label(grid, text=label, background=bg, foreground=TEXT, font=("Segoe UI", 10), padx=8, pady=14).grid(row=row, column=0, sticky=tk.EW)
            before = tk.Label(grid, text="—", background=bg, foreground=TEXT, font=("Segoe UI", 11, "bold"), padx=8)
            before.grid(row=row, column=1, sticky=tk.EW)
            tk.Label(grid, text="→", background=bg, foreground=MUTED_TEXT, font=("Segoe UI", 12), padx=6).grid(row=row, column=2)
            after = tk.Label(grid, text="—", background=bg, foreground=TEXT, font=("Segoe UI", 11, "bold"), padx=8)
            after.grid(row=row, column=3, sticky=tk.EW)
            self.greeks_values[key] = (before, after)

    def _build_metrics(self) -> None:
        for child in self.metrics_frame.winfo_children():
            child.destroy()
        columns = ("Metric", "Before", "", "After roll")
        for column, label in enumerate(columns):
            self.metrics_frame.grid_columnconfigure(column, weight=1 if column in {0, 1, 3} else 0)
            tk.Label(
                self.metrics_frame,
                text=label,
                background=SURFACE_ALT,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8, "bold"),
                padx=8,
                pady=5,
            ).grid(row=0, column=column, sticky=tk.EW)
        self.metric_values: dict[str, tuple[tk.Label, tk.Label]] = {}
        for row, (key, label) in enumerate(
            (
                ("profit", "Max profit"),
                ("loss", "Max loss"),
                ("breakeven", "Breakeven"),
                ("delta", "Delta"),
                ("theta", "Theta / day"),
                ("buying_power", "Buying power"),
            ),
            start=1,
        ):
            bg = TABLE_FIELD if row % 2 else SURFACE
            tk.Label(self.metrics_frame, text=label, background=bg, foreground=TEXT, font=("Segoe UI", 9), padx=8, pady=6, anchor=tk.W).grid(row=row, column=0, sticky=tk.EW)
            before = tk.Label(self.metrics_frame, text="—", background=bg, foreground=TEXT, font=("Segoe UI", 9), padx=8, anchor=tk.E)
            before.grid(row=row, column=1, sticky=tk.EW)
            tk.Label(self.metrics_frame, text="→", background=bg, foreground=MUTED_TEXT, font=("Segoe UI", 10), padx=6).grid(row=row, column=2)
            after = tk.Label(self.metrics_frame, text="—", background=bg, foreground=TEXT, font=("Segoe UI", 9), padx=8, anchor=tk.E)
            after.grid(row=row, column=3, sticky=tk.EW)
            self.metric_values[key] = (before, after)

    def _build_footer(self, parent: tk.Frame) -> None:
        tk.Frame(parent, background=BORDER, height=1).pack(fill=tk.X, pady=(8, 0))
        footer = tk.Frame(parent, background=BACKGROUND)
        footer.pack(fill=tk.X, pady=(10, 1))
        ttk.Button(footer, text="Cancel", command=self.destroy, width=20).pack(side=tk.LEFT)
        ttk.Button(footer, text="Save as template", command=self._save_template, width=24).pack(side=tk.LEFT, padx=(8, 0))
        review = ttk.Button(
            footer,
            text="Review roll order",
            command=self._review,
            style="Roll.Primary.TButton",
            state=tk.DISABLED,
            width=42,
        )
        review.pack(side=tk.RIGHT)
        self.review_button = review
        tk.Label(
            footer,
            text="Review refreshes positions and quotes; this builder never submits.",
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(side=tk.RIGHT, padx=(10, 14))

    @staticmethod
    def _divider(parent: tk.Misc) -> None:
        tk.Frame(parent, background=BORDER, height=1).pack(fill=tk.X)

    @staticmethod
    def _section_heading(parent: tk.Misc, text: str, *, pack: bool = True) -> tk.Label:
        label = tk.Label(parent, text=text, background=SURFACE, foreground=TEXT, font=("Segoe UI", 12, "bold"))
        if pack:
            label.pack(anchor=tk.W)
        return label

    def _build_close_rows(self) -> None:
        for child in self.close_table.winfo_children():
            child.destroy()
        columns = (
            ("Action", 3, tk.W),
            ("Quantity", 1, tk.E),
            ("Expiration / DTE", 2, tk.W),
            ("Strike", 1, tk.E),
            ("Type", 1, tk.W),
            ("Mark", 1, tk.E),
        )
        self._table_header(self.close_table, columns)
        active = set(self.controller.active_symbols)
        for row_index, leg in enumerate(self.controller.position_legs, start=1):
            bg = TABLE_FIELD if row_index % 2 else SURFACE
            for column in range(len(columns)):
                self.close_table.grid_columnconfigure(column, weight=columns[column][1])
            variable = self._leg_vars[leg.symbol]
            variable.set(leg.symbol in active)
            action = tk.Checkbutton(
                self.close_table,
                text=_human_instruction(leg.close_instruction),
                variable=variable,
                command=lambda symbol=leg.symbol: self._leg_toggled(symbol),
                state=tk.NORMAL if self.controller.scope_mode == ROLL_SCOPE_SELECTED else tk.DISABLED,
                background=bg,
                foreground=TEXT,
                disabledforeground=TEXT,
                activebackground=bg,
                activeforeground=TEXT,
                selectcolor=SURFACE_ALT,
                font=("Segoe UI", 9, "bold"),
                borderwidth=0,
                highlightthickness=0,
                anchor=tk.W,
                takefocus=True,
            )
            action.grid(row=row_index, column=0, sticky=tk.EW, padx=7, pady=5)
            values = (
                _number(abs(leg.net_quantity)),
                f"{_short_date(leg.expiration)}  ({_dte(leg.expiration, self.now_provider())} DTE)",
                f"{leg.strike:g}",
                leg.option_type.title(),
                _money(leg.mark),
            )
            anchors = (tk.E, tk.W, tk.E, tk.W, tk.E)
            for offset, (value, anchor) in enumerate(zip(values, anchors, strict=True), start=1):
                tk.Label(self.close_table, text=value, background=bg, foreground=TEXT if leg.symbol in active else MUTED_TEXT, font=("Segoe UI", 9), anchor=anchor, padx=7, pady=5).grid(row=row_index, column=offset, sticky=tk.EW)
        symbols = [leg.symbol for leg in self.controller.position_legs if leg.symbol in active]
        if hasattr(self, "close_symbols_label"):
            self.close_symbols_label.configure(
                text="Exact OCC: " + "  •  ".join(symbols)
                if symbols
                else "No exact legs selected"
            )

    def _build_replacement_rows(self) -> None:
        for child in self.replacement_table.winfo_children():
            child.destroy()
        columns = (
            ("Enabled", 1, tk.W),
            ("Action", 2, tk.W),
            ("Qty", 1, tk.E),
            ("Expiration", 2, tk.W),
            ("Strike", 1, tk.E),
            ("Type", 1, tk.W),
            ("Mark", 1, tk.E),
        )
        self._table_header(self.replacement_table, columns)
        draft = self.controller.draft
        if draft is None:
            tk.Label(
                self.replacement_table,
                text=self.controller.error or "Replacement contracts unavailable",
                background=TABLE_FIELD,
                foreground=WARNING,
                font=("Segoe UI", 9),
                anchor=tk.W,
                padx=9,
                pady=10,
                wraplength=680,
                justify=tk.LEFT,
            ).grid(row=1, column=0, columnspan=len(columns), sticky=tk.EW)
            if hasattr(self, "replacement_symbols_label"):
                self.replacement_symbols_label.configure(text="")
            return
        for row_index, leg in enumerate(draft.replacement_legs, start=1):
            bg = TABLE_FIELD if row_index % 2 else SURFACE
            source_var = self._leg_vars[leg.source_position_symbol]
            check = tk.Checkbutton(
                self.replacement_table,
                variable=source_var,
                command=lambda symbol=leg.source_position_symbol: self._leg_toggled(symbol),
                state=tk.NORMAL if self.controller.scope_mode == ROLL_SCOPE_SELECTED else tk.DISABLED,
                background=bg,
                activebackground=bg,
                selectcolor=SURFACE_ALT,
                borderwidth=0,
                highlightthickness=0,
                takefocus=True,
            )
            check.grid(row=row_index, column=0, sticky=tk.W, padx=8)
            values = (
                _human_instruction(leg.instruction),
                str(leg.quantity),
                f"{_short_date(leg.expiration)}  ({_dte(leg.expiration, self.now_provider())} DTE)",
                f"{leg.strike:g}",
                leg.option_type.title(),
                _money(leg.mark),
            )
            anchors = (tk.W, tk.E, tk.W, tk.E, tk.W, tk.E)
            for offset, (value, anchor) in enumerate(zip(values, anchors, strict=True), start=1):
                tk.Label(self.replacement_table, text=value, background=bg, foreground=TEXT, font=("Segoe UI", 9, "bold" if offset == 1 else "normal"), anchor=anchor, padx=7, pady=5).grid(row=row_index, column=offset, sticky=tk.EW)
        if hasattr(self, "replacement_symbols_label"):
            self.replacement_symbols_label.configure(
                text="Exact OCC: " + "  •  ".join(leg.symbol for leg in draft.replacement_legs)
            )

    @staticmethod
    def _table_header(parent: tk.Frame, columns: tuple[tuple[str, int, str], ...]) -> None:
        for column, (label, weight, anchor) in enumerate(columns):
            parent.grid_columnconfigure(column, weight=weight)
            tk.Label(
                parent,
                text=label,
                background=SURFACE_ALT,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8, "bold"),
                anchor=anchor,
                padx=7,
                pady=5,
            ).grid(row=0, column=column, sticky=tk.EW)

    @staticmethod
    def _draw_roll_connector(canvas: tk.Canvas) -> None:
        canvas.delete("all")
        width = max(canvas.winfo_width(), 240)
        center = width / 2
        y = 19
        canvas.create_line(center - 115, y, center - 34, y, fill=ACCENT)
        canvas.create_line(center + 34, y, center + 115, y, fill=ACCENT)
        canvas.create_oval(center - 16, y - 16, center + 16, y + 16, outline=ACCENT, width=1, fill=SURFACE)
        canvas.create_text(center, y, text="↓", fill=TEXT, font=("Segoe UI Symbol", 13, "bold"))
        canvas.create_text(center + 54, y, text="Roll to", fill=ACCENT, font=("Segoe UI", 9, "bold"), anchor=tk.W)

    def _draw_steps(self) -> None:
        for index, (canvas, label) in enumerate(self._step_widgets, start=1):
            canvas.delete("all")
            active = index == self._step
            fill = ACCENT if active else SURFACE_ALT
            outline = ACCENT if active else BORDER
            canvas.create_oval(2, 2, 22, 22, fill=fill, outline=outline)
            canvas.create_text(12, 12, text=str(index), fill="#ffffff" if active else MUTED_TEXT, font=("Segoe UI", 8, "bold"))
            label.configure(foreground=TEXT if active else MUTED_TEXT, font=("Segoe UI", 9, "bold" if active else "normal"))

    def _set_step(self, step: int) -> None:
        self._step = step
        self._draw_steps()

    def _scope_changed(self) -> None:
        if self._syncing:
            return
        try:
            self.controller.set_scope(self.scope_var.get())
        except Exception as exc:
            self.controller.error = str(exc)
        self._set_step(1)
        self._sync()

    def _leg_toggled(self, symbol: str) -> None:
        if self._syncing:
            return
        try:
            self.controller.set_leg_enabled(symbol, self._leg_vars[symbol].get())
        except Exception as exc:
            self.controller.error = str(exc)
        self._set_step(1)
        self._sync()

    def _expiration_changed(self, _event: object = None) -> None:
        if self._syncing:
            return
        expiration = self._expiration_by_label.get(self.expiration_var.get())
        try:
            self.controller.set_expiration(expiration or "")
        except Exception as exc:
            self.controller.error = str(exc)
        self._set_step(1)
        self._sync()

    def _keep_widths_changed(self) -> None:
        if self._syncing:
            return
        self.controller.set_keep_strike_widths(self.keep_widths_var.get())
        self._set_step(1)
        self._sync()

    def _duration_changed(self, _event: object = None) -> None:
        if self._syncing:
            return
        self.controller.set_duration(self.duration_var.get())
        self._set_step(1)
        self._sync()

    def _manual_price_changed(self, event: object = None) -> str | None:
        if self._syncing or self._busy:
            return None
        try:
            self.controller.set_manual_price(self.limit_price_var.get())
        except Exception as exc:
            self.controller.error = str(exc)
        self._set_step(1)
        self._sync()
        return "break" if getattr(event, "keysym", "") == "Return" else None

    def _adjust_price(self, amount: float) -> None:
        try:
            self.controller.adjust_price(amount)
        except Exception as exc:
            self.controller.error = str(exc)
        self._set_step(1)
        self._sync()

    def _select_mid(self) -> None:
        self.controller.select_midpoint()
        self._set_step(1)
        self._sync()

    def _load_chain(self) -> None:
        self._set_busy(True, "Loading exact option-chain contracts")

        def succeeded(payload: object) -> None:
            try:
                self.controller.load_chain(payload)
            except Exception as exc:
                self.controller.error = str(exc)
                self.controller.draft = None
            self._set_busy(False)
            self._sync()

        def failed(exc: Exception) -> None:
            self.controller.error = f"Option-chain load failed: {type(exc).__name__}: {exc}"
            self.controller.draft = None
            self._set_busy(False)
            self._sync()

        run_in_background(self, self.chain_loader, succeeded, failed)

    def _sync(self) -> None:
        self._syncing = True
        try:
            self.scope_var.set(self.controller.scope_mode)
            self.keep_widths_var.set(self.controller.keep_strike_widths)
            self.duration_var.set(self.controller.duration)
            for symbol, variable in self._leg_vars.items():
                variable.set(self.controller.leg_enabled[symbol])
            for value, card in self._scope_cards.items():
                card.configure(highlightbackground=ACCENT if value == self.controller.scope_mode else BORDER)
            self._expiration_by_label = {
                _expiration_label(expiration, self.now_provider()): expiration
                for expiration in self.controller.expirations
            }
            labels = tuple(self._expiration_by_label)
            self.expiration_box.configure(values=labels, state="readonly" if labels and not self._busy else tk.DISABLED)
            current_label = next(
                (label for label, value in self._expiration_by_label.items() if value == self.controller.expiration),
                "",
            )
            self.expiration_var.set(current_label)
            self._build_close_rows()
            self._build_replacement_rows()
            draft = self.controller.draft
            if draft is None:
                self.status_var.set(self.controller.error or "Roll needs attention")
                self.order_type_var.set("Unavailable")
                self.order_type_box.configure(values=("Unavailable",))
                self.limit_price_var.set("")
                self.net_price_var.set("—")
                self.net_kind_var.set("")
                self.bid_var.set("Bid —")
                self.mid_var.set("Mid —")
                self.ask_var.set("Ask —")
                self.expiration_context_var.set("No valid replacement")
                self.warning_var.set(self.controller.error or "Complete a valid roll configuration.")
                self.net_result_var.set("Net result unavailable")
                self._clear_metrics()
                self._clear_facts()
            else:
                self.status_var.set(
                    f"{draft.scope_label} • {len(draft.close_legs)} close + {len(draft.replacement_legs)} open legs • quotes current"
                )
                order_label = draft.api_order_type.replace("_", " ") + " LIMIT"
                self.order_type_var.set(order_label)
                self.order_type_box.configure(values=(order_label,))
                self.limit_price_var.set(f"{draft.limit_price:.2f}")
                self.net_price_var.set(_money(draft.limit_price))
                self.net_kind_var.set("CREDIT" if draft.is_credit else "DEBIT")
                financial_color = SUCCESS if draft.is_credit else DANGER
                self.net_price_label.configure(foreground=financial_color)
                self.net_kind_label.configure(foreground=financial_color)
                self.bid_var.set(f"Bid {_money(draft.price_rail.bid)}")
                self.mid_var.set(f"Mid {_money(draft.price_rail.midpoint)}")
                self.ask_var.set(f"Ask {_money(draft.price_rail.ask)}")
                self.expiration_context_var.set(
                    f"{_dte(draft.replacement_expiration, self.now_provider())} DTE • +{draft.analysis.days_extended} days"
                )
                material = [warning for warning in draft.warnings if "unavailable" in warning.lower() or "risk" in warning.lower()]
                self.warning_var.set("  •  ".join(material[:2] or draft.warnings[:1]))
                cash_word = "receive" if draft.estimated_cash_effect >= 0 else "pay"
                fees = _money(draft.analysis.estimated_fees) if draft.analysis.estimated_fees is not None else "unavailable"
                self.net_result_var.set(
                    f"Net result: {cash_word} {_money(abs(draft.estimated_cash_effect))} "
                    f"{'credit' if draft.is_credit else 'debit'}  •  Extend {draft.analysis.days_extended} days  •  Fees est. {fees}"
                )
                self.net_result_label.configure(foreground=financial_color)
                self._render_metrics(draft)
                self._render_facts(draft)
            enabled = self.controller.can_review and not self._busy
            self.review_button.configure(state=tk.NORMAL if enabled else tk.DISABLED)
            entry_state = tk.NORMAL if draft is not None and not self._busy else tk.DISABLED
            self.limit_entry.configure(state=entry_state)
            self.duration_box.configure(state="readonly" if draft is not None and not self._busy else tk.DISABLED)
        finally:
            self._syncing = False
        self.after_idle(self._draw_price_rail)
        self.after_idle(self._draw_payoff_chart)
        self._render_greeks()

    def _draw_price_rail(self) -> None:
        canvas = self.price_rail
        canvas.delete("all")
        width = max(canvas.winfo_width(), 280)
        left, right, y = 32, width - 32, 25
        draft = self.controller.draft
        if draft is None:
            canvas.create_line(left, y, right, y, fill=BORDER, width=2)
            canvas.create_text(width / 2, 53, text="Current bid / midpoint / ask unavailable", fill=MUTED_TEXT, font=("Segoe UI", 8))
            return
        rail = draft.price_rail
        span = max(rail.ask - rail.bid, 0.01)
        selected_x = left + (min(max(rail.selected, rail.bid), rail.ask) - rail.bid) / span * (right - left)
        mid_x = left + (rail.midpoint - rail.bid) / span * (right - left)
        color = SUCCESS if draft.is_credit else DANGER
        canvas.create_line(left, y, right, y, fill=BORDER, width=4)
        canvas.create_line(left, y, mid_x, y, fill=color, width=4)
        canvas.create_line(mid_x, y, right, y, fill=ACCENT, width=4)
        canvas.create_line(selected_x, y - 10, selected_x, y + 10, fill=TEXT, width=2)
        canvas.create_oval(selected_x - 4, y - 4, selected_x + 4, y + 4, fill=TEXT, outline=BACKGROUND)
        canvas.create_text(left, 54, text=self.bid_var.get(), fill=MUTED_TEXT, font=("Segoe UI", 8), anchor=tk.W)
        canvas.create_text(width / 2, 54, text=self.mid_var.get(), fill=TEXT, font=("Segoe UI", 8), anchor=tk.CENTER)
        canvas.create_text(right, 54, text=self.ask_var.get(), fill=MUTED_TEXT, font=("Segoe UI", 8), anchor=tk.E)

    def _set_analysis_mode(self, mode: str) -> None:
        self.analysis_mode.set(mode)
        self._set_step(2)
        if mode == "payoff":
            self.greeks_panel.pack_forget()
            self.payoff_canvas.pack(fill=tk.X)
            self.payoff_button.configure(style="Roll.Active.TButton")
            self.greeks_button.configure(style="Roll.Compact.TButton")
            self.after_idle(self._draw_payoff_chart)
        else:
            self.payoff_canvas.pack_forget()
            self.greeks_panel.pack(fill=tk.X)
            self.greeks_button.configure(style="Roll.Active.TButton")
            self.payoff_button.configure(style="Roll.Compact.TButton")
            self._render_greeks()

    def _draw_payoff_chart(self) -> None:
        canvas = self.payoff_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 430)
        height = max(canvas.winfo_height(), 260)
        left, right, top, bottom = 64, width - 24, 32, height - 46
        draft = self.controller.draft
        if draft is None:
            self._chart_unavailable(canvas, width, height, self.controller.error or "Payoff unavailable")
            return
        before = draft.analysis.before_curve
        after = draft.analysis.after_curve
        if not before.available or not after.available:
            reason = before.unavailable_reason or after.unavailable_reason or "Required payoff facts are unavailable."
            self._chart_unavailable(canvas, width, height, reason)
            return
        x_values = after.prices
        y_values = before.profit_loss + after.profit_loss + (0.0,)
        x_min, x_max = min(x_values), max(x_values)
        y_min, y_max = min(y_values), max(y_values)
        y_pad = max((y_max - y_min) * 0.10, 1.0)
        y_min -= y_pad
        y_max += y_pad

        def x(value: float) -> float:
            return left + (value - x_min) / max(x_max - x_min, 1e-9) * (right - left)

        def y(value: float) -> float:
            return bottom - (value - y_min) / max(y_max - y_min, 1e-9) * (bottom - top)

        for index in range(6):
            value = y_min + (y_max - y_min) * index / 5
            py = y(value)
            canvas.create_line(left, py, right, py, fill=BORDER, dash=(2, 3))
            canvas.create_text(left - 8, py, text=_compact_money(value), fill=MUTED_TEXT, font=("Segoe UI", 8), anchor=tk.E)
        for index in range(7):
            value = x_min + (x_max - x_min) * index / 6
            px = x(value)
            canvas.create_line(px, top, px, bottom, fill=BORDER, dash=(2, 3))
            canvas.create_text(px, bottom + 15, text=f"{value:.0f}", fill=MUTED_TEXT, font=("Segoe UI", 8))
        zero_y = y(0.0)
        canvas.create_line(left, zero_y, right, zero_y, fill=TEXT, width=1)
        after_points = [(x(px), y(py)) for px, py in zip(after.prices, after.profit_loss, strict=True)]
        for index in range(len(after_points) - 1):
            (x1, y1), (x2, y2) = after_points[index], after_points[index + 1]
            v1, v2 = after.profit_loss[index], after.profit_loss[index + 1]
            if v1 >= 0 and v2 >= 0:
                canvas.create_polygon(x1, zero_y, x1, y1, x2, y2, x2, zero_y, fill=SUCCESS, outline="", stipple="gray75")
            elif v1 <= 0 and v2 <= 0:
                canvas.create_polygon(x1, zero_y, x1, y1, x2, y2, x2, zero_y, fill=DANGER, outline="", stipple="gray75")
        before_coords = [coordinate for pair in zip((x(value) for value in before.prices), (y(value) for value in before.profit_loss), strict=True) for coordinate in pair]
        after_coords = [coordinate for pair in after_points for coordinate in pair]
        canvas.create_line(*before_coords, fill=MUTED_TEXT, width=2, dash=(6, 4), smooth=True)
        canvas.create_line(*after_coords, fill=ACCENT, width=2, smooth=True)
        underlying = draft.analysis.underlying_price
        if underlying is not None and x_min <= underlying <= x_max:
            marker = x(underlying)
            canvas.create_line(marker, top, marker, bottom, fill=TEXT, dash=(5, 4))
            canvas.create_text(marker, top - 12, text=f"{draft.underlying_symbol} {_money(underlying)}", fill=TEXT, font=("Segoe UI", 8))
        canvas.create_text(left, 14, text="P/L at expiration", fill=MUTED_TEXT, font=("Segoe UI", 8), anchor=tk.W)
        canvas.create_text((left + right) / 2, height - 12, text=f"{draft.underlying_symbol} price at expiration", fill=MUTED_TEXT, font=("Segoe UI", 8))
        legend_x = right - 112
        canvas.create_line(legend_x, 14, legend_x + 28, 14, fill=MUTED_TEXT, width=2, dash=(6, 4))
        canvas.create_text(legend_x + 35, 14, text="Current", fill=TEXT, font=("Segoe UI", 8), anchor=tk.W)
        canvas.create_line(legend_x, 28, legend_x + 28, 28, fill=ACCENT, width=2)
        canvas.create_text(legend_x + 35, 28, text="After roll", fill=TEXT, font=("Segoe UI", 8), anchor=tk.W)

    @staticmethod
    def _chart_unavailable(canvas: tk.Canvas, width: int, height: int, reason: str) -> None:
        canvas.create_rectangle(18, 18, width - 18, height - 18, outline=BORDER, fill=TABLE_FIELD)
        canvas.create_text(width / 2, height / 2 - 10, text="Payoff unavailable", fill=TEXT, font=("Segoe UI", 12, "bold"))
        canvas.create_text(width / 2, height / 2 + 18, text=reason, fill=MUTED_TEXT, font=("Segoe UI", 9), width=max(280, width - 90), justify=tk.CENTER)

    def _render_metrics(self, draft: RollOrderDraft) -> None:
        before = draft.analysis.before_metrics
        after = draft.analysis.after_metrics
        values = {
            "profit": (_metric_bound(before.max_profit, before.max_profit_unbounded, profit=True), _metric_bound(after.max_profit, after.max_profit_unbounded, profit=True)),
            "loss": (_metric_bound(before.max_loss, before.max_loss_unbounded, profit=False), _metric_bound(after.max_loss, after.max_loss_unbounded, profit=False)),
            "breakeven": (_breakevens(before), _breakevens(after)),
            "delta": (_signed_number(before.delta), _signed_number(after.delta)),
            "theta": (_money(before.theta_per_day), _money(after.theta_per_day)),
            "buying_power": (_money(before.buying_power), _money(after.buying_power)),
        }
        for key, pair in values.items():
            labels = self.metric_values[key]
            for label, value in zip(labels, pair, strict=True):
                label.configure(text=value, foreground=_financial_text_color(value) if key in {"profit", "loss", "theta"} else TEXT)

    def _clear_metrics(self) -> None:
        for before, after in self.metric_values.values():
            before.configure(text="—", foreground=TEXT)
            after.configure(text="—", foreground=TEXT)

    def _render_greeks(self) -> None:
        draft = self.controller.draft
        if draft is None:
            for before, after in self.greeks_values.values():
                before.configure(text="—")
                after.configure(text="—")
            return
        pairs = {
            "delta": (_signed_number(draft.analysis.before_metrics.delta), _signed_number(draft.analysis.after_metrics.delta)),
            "theta": (_money(draft.analysis.before_metrics.theta_per_day), _money(draft.analysis.after_metrics.theta_per_day)),
        }
        for key, values in pairs.items():
            for label, value in zip(self.greeks_values[key], values, strict=True):
                label.configure(text=value)

    def _render_facts(self, draft: RollOrderDraft) -> None:
        realized = draft.analysis.estimated_realized_pnl
        self.fact_values["realized"].configure(text=_money(realized), foreground=_value_color(realized))
        self.fact_values["days"].configure(text=f"+{draft.analysis.days_extended} days", foreground=TEXT)
        self.fact_values["fees"].configure(text=_money(draft.analysis.estimated_fees), foreground=TEXT)
        execution_color = SUCCESS if draft.execution_mode == ROLL_EXECUTION_ATOMIC else WARNING
        self.fact_values["execution"].configure(text=draft.execution_detail, foreground=execution_color)

    def _clear_facts(self) -> None:
        for label in self.fact_values.values():
            label.configure(text="—", foreground=TEXT)

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self._busy = busy
        if status:
            self.status_var.set(status)
        if hasattr(self, "review_button"):
            self.review_button.configure(state=tk.DISABLED if busy else (tk.NORMAL if self.controller.can_review else tk.DISABLED))

    def _review(self) -> None:
        draft = self.controller.draft
        if self._busy or draft is None or not self.controller.can_review:
            return
        self._set_step(3)
        self._set_busy(True, "Refreshing positions and quotes before review")

        def work() -> RollOrderDraft:
            latest_snapshot = self.snapshot_loader()
            latest_book = option_position_book(latest_snapshot)  # type: ignore[arg-type]
            chain = parse_roll_chain(
                self.chain_loader(),
                expected_underlying=draft.underlying_symbol,
                observed_at=self.now_provider(),
            )
            return refresh_roll_order_draft(
                draft,
                latest=latest_book,
                chain=chain,
                now=self.now_provider(),
                max_quote_age_seconds=self.controller.max_quote_age_seconds,
            )

        def succeeded(refreshed: RollOrderDraft) -> None:
            self.controller.accept_refreshed_draft(refreshed)
            self._set_busy(False)
            self._sync()
            self.on_review(refreshed)

        def failed(exc: Exception) -> None:
            self._set_step(1)
            self._set_busy(False)
            self.controller.error = f"Review stopped: {str(exc) or type(exc).__name__}"
            self.controller.draft = None
            self._sync()

        run_in_background(self, work, succeeded, failed)

    def _save_template(self) -> None:
        draft = self.controller.draft
        if draft is None:
            messagebox.showerror("Template unavailable", "Complete a valid roll configuration first.", parent=self)
            return
        name = simpledialog.askstring("Save roll template", "Template name:", parent=self)
        if not name:
            return
        template = SavedRollTemplate(
            name=name.strip(),
            days_forward=draft.analysis.days_extended,
            keep_strike_widths=draft.keep_strike_widths,
            duration=draft.duration,
            price_policy=(ROLL_PRICE_NATURAL if draft.price_policy == ROLL_PRICE_NATURAL else ROLL_PRICE_MID),
        )
        try:
            path = save_roll_template(template, self.template_path)
        except Exception as exc:
            messagebox.showerror("Template not saved", str(exc), parent=self)
            return
        self._load_saved_templates()
        messagebox.showinfo(
            "Roll template saved",
            f"Saved configuration defaults to {path}.\n\nNo account, quantity, quote, balance, price, or OCC symbol was stored.",
            parent=self,
        )

    def _load_saved_templates(self) -> None:
        try:
            templates = load_roll_templates(self.template_path)
        except Exception as exc:
            templates = ()
            self.warning_var.set(str(exc))
        self._template_by_name = {template.name: template for template in templates}
        self.template_menu.delete(0, tk.END)
        if not templates:
            self.template_menu.add_command(label="No saved templates", state=tk.DISABLED)
            return
        for template in templates:
            self.template_menu.add_command(
                label=template.name,
                command=lambda selected=template: self._apply_template(selected),
            )

    def _apply_template(self, template: SavedRollTemplate) -> None:
        self.controller.apply_template(template)
        self._set_step(1)
        self._sync()


def _show_roll_review(root: tk.Misc, draft: RollOrderDraft, now: datetime) -> None:
    controller = OptionOrderReviewController(
        review=roll_order_review(draft, now=now),
        draft=draft,
    )
    OptionOrderReviewDialog(root=root, controller=controller)


def _roll_title(legs: tuple[OptionPositionLeg, ...]) -> str:
    first = legs[0]
    if len(legs) == 1:
        return f"Roll {first.underlying_symbol} {first.strike:g} {first.option_type.title()}"
    option_types = {leg.option_type.upper() for leg in legs}
    expirations = {leg.expiration for leg in legs}
    if len(legs) == 2 and len(option_types) == 1 and len(expirations) == 1:
        return f"Roll {first.underlying_symbol} {first.option_type.title()} Spread"
    return f"Roll {first.underlying_symbol} Custom Option Strategy"


def _human_instruction(value: str) -> str:
    return {
        "BUY_TO_CLOSE": "Buy to close",
        "SELL_TO_CLOSE": "Sell to close",
        "BUY_TO_OPEN": "Buy to open",
        "SELL_TO_OPEN": "Sell to open",
    }.get(value, value.replace("_", " ").title())


def _short_date(value: str) -> str:
    try:
        return date.fromisoformat(value[:10]).strftime("%d %b %y").lstrip("0")
    except ValueError:
        return "—"


def _dte(expiration: str, now: datetime) -> int:
    try:
        target = date.fromisoformat(expiration[:10])
    except ValueError:
        return 0
    current = now.astimezone().date() if now.tzinfo else now.date()
    return (target - current).days


def _expiration_label(expiration: str, now: datetime) -> str:
    return f"{_short_date(expiration)}  ({_dte(expiration, now)} DTE)"


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 0:
        return f"-${abs(value):,.2f}"
    return f"${value:,.2f}"


def _number(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def _signed_number(value: float | None) -> str:
    return "—" if value is None else f"{value:+,.2f}"


def _compact_money(value: float) -> str:
    if abs(value) >= 1_000:
        prefix = "-$" if value < 0 else "$"
        return f"{prefix}{abs(value) / 1_000:,.1f}k"
    prefix = "-$" if value < 0 else "$"
    return f"{prefix}{abs(value):,.0f}"


def _breakevens(metrics: RollMetricSnapshot) -> str:
    if metrics.breakevens is None:
        return "—"
    if not metrics.breakevens:
        return "None"
    return ", ".join(f"${value:,.2f}" for value in metrics.breakevens)


def _metric_bound(value: float | None, unbounded: bool, *, profit: bool) -> str:
    if unbounded:
        return "Unlimited profit" if profit else "Unlimited loss"
    return _money(value)


def _value_color(value: float | None) -> str:
    if value is None or math.isclose(value, 0.0, abs_tol=1e-9):
        return TEXT
    return SUCCESS if value > 0 else DANGER


def _financial_text_color(text: str) -> str:
    if text.startswith("-$") or "loss" in text.lower():
        return DANGER
    if text.startswith("$") and text != "$0.00" or "profit" in text.lower():
        return SUCCESS
    return TEXT


def _timestamp(value: datetime) -> str:
    local = value.astimezone() if value.tzinfo else value
    return local.strftime("%b %d, %Y %I:%M:%S %p")


__all__ = [
    "RollWorkspaceController",
    "RollWorkspaceDialog",
]
