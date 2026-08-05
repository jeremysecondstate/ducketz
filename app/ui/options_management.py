from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from datetime import date, datetime, timezone
from tkinter import messagebox, ttk

from app.models.option_management import (
    ClosingOrderDraft,
    ClosingOrderLeg,
    ManagedOptionOrder,
    OptionPositionBook,
    OptionPositionLeg,
    RollOrderDraft,
)
from app.models.portfolio import PortfolioSnapshot
from app.services.schwab_option_management import (
    build_closing_order_draft,
    filter_option_positions,
    option_orders_from_payload,
    option_orders_from_snapshot,
    option_position_book,
)
from app.services.option_order_review import (
    OptionOrderReviewController,
    closing_order_review,
    closing_price_rail,
    roll_order_review,
)
from app.services.option_rolls import roll_action_disabled_reason
from app.services.schwab_strategy_orders import DAY_ONLY, GOOD_UNTIL_CANCELED
from app.ui.background_tasks import run_in_background
from app.ui.option_exit_plans import ExitPlanBuilderDialog
from app.ui.option_order_review import OptionOrderReviewDialog
from app.ui.option_rolls import RollWorkspaceDialog
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


ALL_SYMBOLS = "All symbols"
ALL_EXPIRATIONS = "All expirations"
SELECTED_ROW = "#174f86"
ALTERNATE_ROW = "#0e1a2b"
CLOSE_SCOPE_ENTIRE = "entire"
CLOSE_SCOPE_SELECTED = "selected"


def _selection_after_click(
    current: tuple[int, ...],
    clicked: int,
    anchor: int | None,
    row_count: int,
    *,
    extend: bool,
    toggle: bool,
) -> tuple[tuple[int, ...], int | None]:
    if clicked < 0 or clicked >= row_count:
        return tuple(sorted(set(current))), anchor
    selected = set(current)
    if extend and anchor is not None:
        interval = set(range(min(anchor, clicked), max(anchor, clicked) + 1))
        selected = selected | interval if toggle else interval
        return tuple(sorted(selected)), anchor
    if toggle:
        if clicked in selected:
            selected.remove(clicked)
        else:
            selected.add(clicked)
    else:
        selected = {clicked}
    return tuple(sorted(selected)), clicked


def _initial_position_selection(
    rows: tuple[OptionPositionLeg, ...],
    preferred_symbols: tuple[str, ...] = (),
) -> tuple[int, ...]:
    preferred = set(preferred_symbols)
    preserved = tuple(index for index, row in enumerate(rows) if row.symbol in preferred)
    if preserved:
        return preserved
    for index, row in enumerate(rows):
        if not row.close_disabled_reason:
            return (index,)
    return (0,) if rows else ()


def _closing_scope_rows(
    selected: tuple[OptionPositionLeg, ...],
    *,
    active_symbol: str | None,
    scope: str,
) -> tuple[OptionPositionLeg, ...]:
    if not selected:
        return ()
    if scope == CLOSE_SCOPE_SELECTED:
        return selected
    active = next((leg for leg in selected if leg.symbol == active_symbol), None)
    return (active or selected[0],)


def _closing_price_rail(draft: ClosingOrderDraft) -> tuple[float, float, float] | None:
    """Compatibility tuple around the shared review-domain price rail."""
    rail = closing_price_rail(draft)
    return None if rail is None else (rail.bid, rail.midpoint, rail.ask)


class _ExactLegTable(tk.Frame):
    _HEADER_HEIGHT = 32
    _ROW_HEIGHT = 48
    _DETAIL_HEIGHT = 70
    _COLUMNS = (
        ("position", "Position / exact OCC", 225, "w"),
        ("quantity", "Qty", 46, "e"),
        ("dte", "DTE", 44, "e"),
        ("mark", "Mark", 62, "e"),
        ("open_pnl", "Open P/L", 76, "e"),
        ("day_pnl", "Day P/L", 72, "e"),
        ("delta", "Delta", 58, "e"),
        ("action", "Close", 64, "w"),
    )

    def __init__(
        self,
        parent: tk.Misc,
        *,
        on_selection_changed: Callable[[tuple[OptionPositionLeg, ...]], None],
    ) -> None:
        super().__init__(
            parent,
            background=SURFACE,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            highlightthickness=1,
            takefocus=True,
        )
        self._rows: tuple[OptionPositionLeg, ...] = ()
        self._selected: tuple[int, ...] = ()
        self._anchor: int | None = None
        self._expanded_symbols: set[str] = set()
        self._on_selection_changed = on_selection_changed
        self._redraw_pending = False

        self.header = tk.Canvas(
            self,
            height=self._HEADER_HEIGHT,
            background=SURFACE_ALT,
            highlightthickness=0,
            borderwidth=0,
        )
        self.body = tk.Canvas(
            self,
            background=TABLE_FIELD,
            highlightthickness=0,
            borderwidth=0,
            takefocus=True,
        )
        vertical = ttk.Scrollbar(
            self,
            orient=tk.VERTICAL,
            command=self.body.yview,
            style="Management.Vertical.TScrollbar",
        )
        horizontal = ttk.Scrollbar(
            self,
            orient=tk.HORIZONTAL,
            command=self._xview,
            style="Management.Horizontal.TScrollbar",
        )
        self._vertical = vertical
        self._horizontal = horizontal
        self.header.grid(row=0, column=0, sticky=tk.EW)
        self.body.grid(row=1, column=0, sticky=tk.NSEW)
        vertical.grid(row=1, column=1, sticky=tk.NS)
        horizontal.grid(row=2, column=0, sticky=tk.EW)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.body.configure(yscrollcommand=self._body_yscroll, xscrollcommand=self._body_xscroll)
        self.header.bind("<Configure>", self._schedule_redraw)
        self.body.bind("<Configure>", self._schedule_redraw)
        self.body.bind("<Button-1>", self._clicked)
        self.body.bind("<MouseWheel>", self._mouse_wheel)
        self.body.bind("<Control-a>", self._select_all)
        self.body.bind("<Return>", self._toggle_active)
        self.body.bind("<space>", self._toggle_active)

    def set_rows(
        self,
        rows: tuple[OptionPositionLeg, ...],
        *,
        preferred_symbols: tuple[str, ...] = (),
    ) -> tuple[OptionPositionLeg, ...]:
        had_rows = bool(self._rows)
        self._rows = rows
        self._selected = _initial_position_selection(rows, preferred_symbols)
        self._anchor = self._selected[0] if self._selected else None
        visible_symbols = {row.symbol for row in rows}
        self._expanded_symbols.intersection_update(visible_symbols)
        if not had_rows and self._anchor is not None:
            self._expanded_symbols.add(rows[self._anchor].symbol)
        self.body.yview_moveto(0)
        self._redraw()
        return self.selected_rows()

    def selected_rows(self) -> tuple[OptionPositionLeg, ...]:
        return tuple(self._rows[index] for index in self._selected if index < len(self._rows))

    def selected_symbols(self) -> tuple[str, ...]:
        return tuple(row.symbol for row in self.selected_rows())

    def active_row(self) -> OptionPositionLeg | None:
        if self._anchor is None or self._anchor >= len(self._rows):
            return None
        return self._rows[self._anchor]

    def _row_layout(self) -> tuple[tuple[int, int, int, int], ...]:
        layout: list[tuple[int, int, int, int]] = []
        top = 0
        for index, leg in enumerate(self._rows):
            main_bottom = top + self._ROW_HEIGHT
            bottom = main_bottom + (self._DETAIL_HEIGHT if leg.symbol in self._expanded_symbols else 0)
            layout.append((index, top, main_bottom, bottom))
            top = bottom
        return tuple(layout)

    def _schedule_redraw(self, _event: object = None) -> None:
        if self._redraw_pending:
            return
        self._redraw_pending = True
        self.after_idle(self._redraw)

    def _redraw(self) -> None:
        self._redraw_pending = False
        if not self.winfo_exists():
            return
        self.header.delete("all")
        self.body.delete("all")
        viewport = max(self.header.winfo_width(), self.body.winfo_width(), 1)
        base_width = sum(column[2] for column in self._COLUMNS)
        extra = max(0, viewport - base_width)
        bounds: list[tuple[str, int, int, str]] = []
        left = 0
        for name, label, width, anchor in self._COLUMNS:
            actual_width = width + (extra if name == "position" else 0)
            right = left + actual_width
            bounds.append((name, left, right, anchor))
            self.header.create_rectangle(
                left,
                0,
                right,
                self._HEADER_HEIGHT,
                fill=SURFACE_ALT,
                outline=BORDER,
                width=1,
            )
            self.header.create_text(
                self._text_x(left, right, anchor),
                self._HEADER_HEIGHT / 2,
                text=label,
                anchor=self._canvas_anchor(anchor),
                fill=MUTED_TEXT,
                font=("Segoe UI", 9, "bold"),
            )
            left = right
        total_width = max(left, viewport)
        layout = self._row_layout()
        content_height = layout[-1][3] if layout else 0
        total_height = max(content_height, self.body.winfo_height())
        self.header.configure(scrollregion=(0, 0, total_width, self._HEADER_HEIGHT))
        self.body.configure(scrollregion=(0, 0, total_width, total_height))
        if not self._rows:
            self.body.create_text(
                viewport / 2,
                38,
                text="No exact option positions match the current filters.",
                fill=MUTED_TEXT,
                font=("Segoe UI", 10),
            )
            return
        for index, top, main_bottom, bottom in layout:
            leg = self._rows[index]
            selected = index in self._selected
            background = SELECTED_ROW if selected else (SURFACE if index % 2 == 0 else ALTERNATE_ROW)
            self.body.create_rectangle(0, top, total_width, main_bottom, fill=background, outline="")
            self.body.create_line(0, main_bottom, total_width, main_bottom, fill=BORDER)
            for name, cell_left, cell_right, anchor in bounds:
                if name == "position":
                    expanded = leg.symbol in self._expanded_symbols
                    self.body.create_text(
                        cell_left + 14,
                        top + self._ROW_HEIGHT / 2,
                        text="⌄" if expanded else "›",
                        anchor="center",
                        fill="#dce7f5" if selected else MUTED_TEXT,
                        font=("Segoe UI Symbol", 13, "bold"),
                    )
                    descriptor = " · ".join(
                        part
                        for part in (
                            leg.underlying_symbol or "Unknown",
                            f"{leg.strike:g}",
                            leg.option_type.title() if leg.option_type else "Option",
                        )
                        if part
                    )
                    self.body.create_text(
                        cell_left + 30,
                        top + 15,
                        text=descriptor,
                        anchor="w",
                        fill=TEXT,
                        font=("Segoe UI", 10, "bold"),
                    )
                    self.body.create_text(
                        cell_left + 30,
                        top + 34,
                        text=leg.symbol or "Identity unavailable",
                        anchor="w",
                        fill="#b7c4d6" if selected else MUTED_TEXT,
                        font=("Cascadia Mono", 8),
                    )
                    continue
                value, color = self._cell_value(name, leg)
                self.body.create_text(
                    self._text_x(cell_left, cell_right, anchor),
                    top + self._ROW_HEIGHT / 2,
                    text=value,
                    anchor=self._canvas_anchor(anchor),
                    fill=color,
                    font=("Segoe UI", 9, "bold" if name in {"open_pnl", "day_pnl", "action"} else "normal"),
                )
            if bottom > main_bottom:
                self._draw_expanded_leg(
                    leg,
                    bounds,
                    top=main_bottom,
                    bottom=bottom,
                    total_width=total_width,
                    selected=selected,
                )
            self.body.create_line(0, bottom, total_width, bottom, fill=BORDER)

    def _draw_expanded_leg(
        self,
        leg: OptionPositionLeg,
        bounds: list[tuple[str, int, int, str]],
        *,
        top: int,
        bottom: int,
        total_width: int,
        selected: bool,
    ) -> None:
        background = "#0c2035" if selected else TABLE_FIELD
        self.body.create_rectangle(0, top, total_width, bottom, fill=background, outline="")
        self.body.create_text(
            30,
            top + 15,
            text="Contract details",
            anchor="w",
            fill=MUTED_TEXT,
            font=("Segoe UI", 8, "bold"),
        )
        for name, cell_left, cell_right, anchor in bounds:
            if name == "position":
                action = "BUY" if leg.net_quantity > 0 else "SELL"
                action_color = SUCCESS if action == "BUY" else DANGER
                self.body.create_text(
                    cell_left + 30,
                    top + 46,
                    text=action,
                    anchor="w",
                    fill=action_color,
                    font=("Segoe UI", 8, "bold"),
                )
                self.body.create_text(
                    cell_left + 70,
                    top + 46,
                    text=(
                        f"{leg.underlying_symbol}  {_short_expiration(leg.expiration)}  "
                        f"{leg.strike:g}  {leg.option_type.title()}"
                    ),
                    anchor="w",
                    fill=TEXT,
                    font=("Segoe UI", 8),
                )
                continue
            if name == "quantity":
                value, color = _number(leg.absolute_quantity), TEXT
            elif name == "action":
                value, color = "—", MUTED_TEXT
            else:
                value, color = self._cell_value(name, leg)
            self.body.create_text(
                self._text_x(cell_left, cell_right, anchor),
                top + 46,
                text=value,
                anchor=self._canvas_anchor(anchor),
                fill=color,
                font=("Segoe UI", 8, "bold" if name == "action" else "normal"),
            )

    @staticmethod
    def _cell_value(name: str, leg: OptionPositionLeg) -> tuple[str, str]:
        if name == "quantity":
            return _number(leg.net_quantity), TEXT
        if name == "dte":
            return _number(days_to_expiration(leg.expiration, leg.observed_at)), TEXT
        if name == "mark":
            return _money(leg.mark), TEXT
        if name == "open_pnl":
            return _money(leg.unrealized_pnl), _signed_value_color(leg.unrealized_pnl)
        if name == "day_pnl":
            return _money(leg.day_pnl), _signed_value_color(leg.day_pnl)
        if name == "delta":
            return "—" if leg.delta is None else f"{leg.delta:+.2f}", TEXT
        if leg.close_disabled_reason:
            return "Blocked", MUTED_TEXT
        if leg.close_instruction.startswith("BUY"):
            return "Buy", SUCCESS
        return "Sell", DANGER

    @staticmethod
    def _text_x(left: int, right: int, anchor: str) -> float:
        if anchor == "w":
            return left + 9
        if anchor == "e":
            return right - 9
        return (left + right) / 2

    @staticmethod
    def _canvas_anchor(anchor: str) -> str:
        return {"w": "w", "e": "e"}.get(anchor, "center")

    def _clicked(self, event: object) -> str:
        self.body.focus_set()
        y = self.body.canvasy(getattr(event, "y", 0))
        hit = next(
            ((index, top, main_bottom) for index, top, main_bottom, bottom in self._row_layout() if top <= y < bottom),
            None,
        )
        if hit is None:
            return "break"
        clicked, _top, main_bottom = hit
        previous_selection = self._selected
        previous_anchor = self._anchor
        state = int(getattr(event, "state", 0))
        self._selected, self._anchor = _selection_after_click(
            self._selected,
            clicked,
            self._anchor,
            len(self._rows),
            extend=bool(state & 0x0001),
            toggle=bool(state & 0x0004),
        )
        if y < main_bottom and self.body.canvasx(getattr(event, "x", 0)) < 30:
            self._toggle_expanded(clicked)
        self._redraw()
        if self._selected != previous_selection or self._anchor != previous_anchor:
            self._on_selection_changed(self.selected_rows())
        return "break"

    def _toggle_active(self, _event: object = None) -> str:
        if self._anchor is not None:
            self._toggle_expanded(self._anchor)
            self._redraw()
        return "break"

    def _toggle_expanded(self, index: int) -> None:
        symbol = self._rows[index].symbol
        if symbol in self._expanded_symbols:
            self._expanded_symbols.remove(symbol)
        else:
            self._expanded_symbols.add(symbol)

    def _select_all(self, _event: object) -> str:
        self._selected = tuple(range(len(self._rows)))
        self._anchor = 0 if self._rows else None
        self._redraw()
        self._on_selection_changed(self.selected_rows())
        return "break"

    def _mouse_wheel(self, event: object) -> str:
        delta = int(getattr(event, "delta", 0))
        if delta:
            self.body.yview_scroll(-int(delta / 120), "units")
        return "break"

    def _xview(self, *args: object) -> None:
        self.header.xview(*args)
        self.body.xview(*args)

    def _body_xscroll(self, first: str, last: str) -> None:
        self._horizontal.set(first, last)
        self.header.xview_moveto(first)
        self._set_scrollbar_visibility(self._horizontal, first, last)

    def _body_yscroll(self, first: str, last: str) -> None:
        self._vertical.set(first, last)
        self._set_scrollbar_visibility(self._vertical, first, last)

    @staticmethod
    def _set_scrollbar_visibility(scrollbar: ttk.Scrollbar, first: str, last: str) -> None:
        if float(first) <= 0.0 and float(last) >= 0.999999:
            scrollbar.grid_remove()
        else:
            scrollbar.grid()


class _ClosingLegPreview(tk.Frame):
    _COLUMNS = (
        ("Action", 6, "w"),
        ("Symbol", 6, "w"),
        ("Expiry", 8, "w"),
        ("Strike", 5, "e"),
        ("Type", 5, "w"),
        ("Qty", 3, "e"),
        ("Bid", 5, "e"),
        ("Ask", 5, "e"),
        ("Mark", 5, "e"),
    )

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(
            parent,
            background=SURFACE,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        self._body = tk.Frame(self, background=SURFACE)
        header = tk.Frame(self, background=SURFACE_ALT)
        header.pack(fill=tk.X)
        self._body.pack(fill=tk.X)
        self._configure_columns(header)
        for column, (text, width, anchor) in enumerate(self._COLUMNS):
            tk.Label(
                header,
                text=text,
                width=width,
                anchor=anchor,
                background=SURFACE_ALT,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8, "bold"),
                padx=2,
                pady=4,
            ).grid(row=0, column=column, sticky=tk.EW)
        self.clear()

    @classmethod
    def _configure_columns(cls, frame: tk.Frame) -> None:
        for column in range(len(cls._COLUMNS)):
            frame.grid_columnconfigure(column, weight=1)

    def clear(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        tk.Label(
            self._body,
            text="Select one or more exact positions to preview the closing legs.",
            anchor="w",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
            padx=8,
            pady=5,
        ).pack(fill=tk.X)

    def set_legs(self, legs: tuple[ClosingOrderLeg, ...]) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        for row_index, leg in enumerate(legs):
            background = SURFACE if row_index % 2 == 0 else ALTERNATE_ROW
            row = tk.Frame(self._body, background=background)
            row.pack(fill=tk.X)
            self._configure_columns(row)
            action = "Buy" if leg.instruction.startswith("BUY") else "Sell"
            values = (
                (action, SUCCESS if action == "Buy" else DANGER),
                (leg.underlying_symbol or "—", TEXT),
                (_short_expiration(leg.expiration), TEXT),
                (_number(leg.strike), TEXT),
                (leg.option_type.title() if leg.option_type else "—", TEXT),
                (_number(leg.quantity), TEXT),
                (_money(leg.bid), MUTED_TEXT),
                (_money(leg.ask), MUTED_TEXT),
                (_money(leg.mark), TEXT),
            )
            for column, ((_, width, anchor), (value, color)) in enumerate(zip(self._COLUMNS, values)):
                tk.Label(
                    row,
                    text=value,
                    width=width,
                    anchor=anchor,
                    background=background,
                    foreground=color,
                    font=("Segoe UI", 8, "bold" if column == 0 else "normal"),
                    padx=2,
                    pady=5,
                ).grid(row=0, column=column, sticky=tk.EW)


class OptionsManagementView:
    def __init__(
        self,
        *,
        root: tk.Tk,
        positions_parent: ttk.Frame,
        orders_parent: ttk.Frame,
        snapshot_loader: Callable[[], PortfolioSnapshot],
        session_factory: Callable[[], object],
        on_refresh: Callable[[], None],
        on_show_orders: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
        self.snapshot_loader = snapshot_loader
        self.session_factory = session_factory
        self.on_refresh = on_refresh
        self.on_show_orders = on_show_orders or (lambda: None)
        self.snapshot: PortfolioSnapshot | None = None
        self.book: OptionPositionBook | None = None
        self._working_orders: tuple[ManagedOptionOrder, ...] = ()
        self._visible_order_by_iid: dict[str, ManagedOptionOrder] = {}

        self.position_account = tk.StringVar(master=root, value="Schwab account")
        self.position_symbol = tk.StringVar(master=root, value=ALL_SYMBOLS)
        self.position_expiration = tk.StringVar(master=root, value=ALL_EXPIRATIONS)
        self.position_status = tk.StringVar(master=root, value="Loading Schwab option positions")
        self.position_updated_status = tk.StringVar(master=root)
        self.management_title = tk.StringVar(master=root, value="Select exact option legs")
        self.management_detail = tk.StringVar(
            master=root,
            value="Use Ctrl or Shift to select more than one ungrouped leg.",
        )
        self.close_scope = tk.StringVar(
            master=root,
            value="Selected exact positions · highlighted rows only",
        )
        self.close_scope_mode = tk.StringVar(master=root, value=CLOSE_SCOPE_ENTIRE)
        self.close_entire_detail = tk.StringVar(master=root, value="Close the active exact position")
        self.close_selected_detail = tk.StringVar(master=root, value="Choose multiple highlighted contracts")
        self.close_order_type = tk.StringVar(master=root, value="—")
        self.close_limit_price = tk.StringVar(master=root)
        self.close_duration = tk.StringVar(master=root, value=DAY_ONLY)
        self.close_bid_label = tk.StringVar(master=root, value="Bid —")
        self.close_mid_label = tk.StringVar(master=root, value="Mid —")
        self.close_ask_label = tk.StringVar(master=root, value="Ask —")
        self.close_open_pnl = tk.StringVar(master=root, value="—")
        self.close_estimate_title = tk.StringVar(master=root, value="Est. proceeds / cost")
        self.close_estimate = tk.StringVar(master=root)
        self.close_fees = tk.StringVar(master=root, value="At broker review")
        self.close_estimate_source = tk.StringVar(master=root)
        self.order_source_status = tk.StringVar(master=root, value="Working orders from the latest account refresh")
        self.order_detail = tk.StringVar(master=root, value="Select an option order to inspect every leg.")

        self._summary_values = {
            "value": tk.StringVar(master=root, value="—"),
            "open": tk.StringVar(master=root, value="—"),
            "day": tk.StringVar(master=root, value="—"),
            "funds": tk.StringVar(master=root, value="—"),
        }
        self._summary_labels: dict[str, ttk.Label] = {}
        self._summary_title_labels: dict[str, ttk.Label] = {}
        self.position_table: _ExactLegTable | None = None
        self.close_preview: _ClosingLegPreview | None = None
        self.close_estimate_label: ttk.Label | None = None
        self.review_close_button: ttk.Button | None = None
        self.close_limit_entry: ttk.Entry | None = None
        self.exit_plan_button: ttk.Button | None = None
        self.roll_button: ttk.Button | None = None
        self.close_duration_box: ttk.Combobox | None = None
        self.close_price_scale: tk.Scale | None = None
        self._scope_controls: dict[str, tuple[tk.Frame, tk.Radiobutton, tk.Label]] = {}
        self._close_draft: ClosingOrderDraft | None = None
        self._ticket_updating = False
        self.order_table: ttk.Treeview | None = None
        self.order_legs_table: ttk.Treeview | None = None
        self.cancel_order_button: ttk.Button | None = None
        self.load_recent_button: ttk.Button | None = None

        self._apply_styles()
        self._build_positions(positions_parent)
        self._build_orders(orders_parent)

    def _apply_styles(self) -> None:
        style = ttk.Style(self.root)
        style.configure("ManagementPage.TFrame", background=BACKGROUND)
        style.configure(
            "ManagementCard.TFrame",
            background=SURFACE,
            bordercolor=BORDER,
            borderwidth=1,
            relief=tk.SOLID,
        )
        style.configure(
            "ManagementInset.TFrame",
            background=TABLE_FIELD,
            bordercolor=BORDER,
            borderwidth=1,
            relief=tk.SOLID,
        )
        style.configure(
            "ManagementCardTitle.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "ManagementCardValue.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 16, "bold"),
        )
        style.configure(
            "ManagementCardPositive.TLabel",
            background=SURFACE,
            foreground=SUCCESS,
            font=("Segoe UI", 16, "bold"),
        )
        style.configure(
            "ManagementCardNegative.TLabel",
            background=SURFACE,
            foreground=DANGER,
            font=("Segoe UI", 16, "bold"),
        )
        style.configure(
            "ManagementSection.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 12, "bold"),
        )
        style.configure(
            "ManagementBody.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 10),
        )
        style.configure(
            "ManagementMuted.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "ManagementWarning.TLabel",
            background=SURFACE,
            foreground=WARNING,
            font=("Segoe UI", 9),
        )
        style.configure(
            "ManagementPrimary.TButton",
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
            font=("Segoe UI", 10, "bold"),
            padding=(12, 8),
        )
        style.map(
            "ManagementPrimary.TButton",
            background=[("active", "#2799f4"), ("disabled", SURFACE_ALT)],
            foreground=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "ManagementActiveSegment.TButton",
            background=ACCENT,
            foreground="#ffffff",
            bordercolor=ACCENT,
            font=("Segoe UI", 9, "bold"),
            padding=(5, 6),
        )
        style.map(
            "ManagementActiveSegment.TButton",
            background=[("active", "#2799f4")],
        )
        style.configure(
            "ManagementSegment.TButton",
            background=SURFACE,
            foreground=TEXT,
            bordercolor=BORDER,
            font=("Segoe UI", 9),
            padding=(5, 6),
        )
        style.map(
            "ManagementSegment.TButton",
            background=[("active", SURFACE_ALT), ("disabled", SURFACE)],
            foreground=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "ManagementSelected.TLabel",
            background=TABLE_FIELD,
            foreground=TEXT,
            font=("Segoe UI", 10, "bold"),
            padding=(9, 7),
        )
        style.configure(
            "ManagementInsetBody.TLabel",
            background=TABLE_FIELD,
            foreground=TEXT,
            font=("Segoe UI", 10),
        )
        style.configure(
            "ManagementInsetMuted.TLabel",
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "ManagementInsetAccent.TLabel",
            background=TABLE_FIELD,
            foreground=ACCENT,
            font=("Segoe UI", 12, "bold"),
        )
        style.configure(
            "ManagementEstimatePositive.TLabel",
            background=TABLE_FIELD,
            foreground=SUCCESS,
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "ManagementEstimateNegative.TLabel",
            background=TABLE_FIELD,
            foreground=DANGER,
            font=("Segoe UI", 10, "bold"),
        )
        style.configure(
            "ManagementPlace.TButton",
            background=SUCCESS,
            foreground="#04130a",
            bordercolor=SUCCESS,
            font=("Segoe UI", 10, "bold"),
            padding=(14, 9),
        )
        style.map(
            "ManagementPlace.TButton",
            background=[("active", "#4ade80"), ("disabled", SURFACE_ALT)],
            foreground=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "ManagementDanger.TButton",
            background=SURFACE_ALT,
            foreground=DANGER,
            bordercolor=DANGER,
            font=("Segoe UI", 10, "bold"),
            padding=(10, 7),
        )
        style.map(
            "ManagementDanger.TButton",
            foreground=[("disabled", MUTED_TEXT)],
            bordercolor=[("disabled", BORDER)],
        )
        style.configure(
            "ManagementDisabled.Treeview",
            background=SURFACE,
            fieldbackground=SURFACE,
            foreground=TEXT,
        )
        for orientation in ("Vertical", "Horizontal"):
            style.configure(
                f"Management.{orientation}.TScrollbar",
                background=SURFACE_ALT,
                troughcolor=TABLE_FIELD,
                bordercolor=TABLE_FIELD,
                darkcolor=SURFACE_ALT,
                lightcolor=SURFACE_ALT,
                arrowcolor=MUTED_TEXT,
            )

    def _build_positions(self, parent: ttk.Frame) -> None:
        outer = ttk.Frame(parent, padding=(2, 8, 2, 2), style="ManagementPage.TFrame")
        outer.pack(fill=tk.BOTH, expand=True)

        workspace = ttk.Frame(outer, style="ManagementPage.TFrame")
        workspace.pack(fill=tk.BOTH, expand=True)
        workspace.grid_columnconfigure(0, weight=13, uniform="management-workspace", minsize=620)
        workspace.grid_columnconfigure(1, weight=7, uniform="management-workspace", minsize=400)
        workspace.grid_rowconfigure(0, weight=1)

        positions_stack = ttk.Frame(workspace, style="ManagementPage.TFrame")
        manage_surface = ttk.Frame(workspace, padding=(11, 9), style="ManagementCard.TFrame")
        positions_stack.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 4))
        manage_surface.grid(row=0, column=1, sticky=tk.NSEW, padx=(4, 0))

        cards = ttk.Frame(positions_stack, style="ManagementPage.TFrame")
        cards.pack(fill=tk.X, pady=(0, 9))
        for column in range(4):
            cards.grid_columnconfigure(column, weight=1, uniform="summary")
        self._summary_card(cards, "Options value", "value", 0)
        self._summary_card(cards, "Open P/L", "open", 1)
        self._summary_card(cards, "Theta / day", "day", 2)
        self._summary_card(cards, "Available funds", "funds", 3)

        filters = ttk.Frame(positions_stack, padding=(10, 7), style="ManagementCard.TFrame")
        filters.pack(fill=tk.X, pady=(0, 8))
        for column in range(3):
            filters.grid_columnconfigure(column, weight=1, uniform="position-filter")
        filters.grid_columnconfigure(3, weight=1)

        ttk.Label(filters, text="Account", style="ManagementMuted.TLabel").grid(
            row=0, column=0, sticky=tk.W, padx=(0, 8)
        )
        ttk.Label(filters, text="Symbol", style="ManagementMuted.TLabel").grid(
            row=0, column=1, sticky=tk.W, padx=8
        )
        ttk.Label(filters, text="Expiration", style="ManagementMuted.TLabel").grid(
            row=0, column=2, sticky=tk.W, padx=8
        )
        account = ttk.Label(
            filters,
            textvariable=self.position_account,
            style="ManagementSelected.TLabel",
        )
        account.grid(row=1, column=0, sticky=tk.EW, padx=(0, 8), pady=(3, 0))
        symbol_box = ttk.Combobox(
            filters,
            textvariable=self.position_symbol,
            values=(ALL_SYMBOLS,),
            state="readonly",
        )
        symbol_box.grid(row=1, column=1, sticky=tk.EW, padx=8, pady=(3, 0))
        symbol_box.bind("<<ComboboxSelected>>", self._position_filters_changed)
        self._position_symbol_box = symbol_box

        expiration_box = ttk.Combobox(
            filters,
            textvariable=self.position_expiration,
            values=(ALL_EXPIRATIONS,),
            state="readonly",
        )
        expiration_box.grid(row=1, column=2, sticky=tk.EW, padx=8, pady=(3, 0))
        expiration_box.bind("<<ComboboxSelected>>", self._position_filters_changed)
        self._position_expiration_box = expiration_box
        ttk.Label(
            filters,
            text="Exact broker positions · ungrouped",
            style="ManagementMuted.TLabel",
            justify=tk.RIGHT,
            wraplength=180,
        ).grid(
            row=0,
            column=3,
            rowspan=2,
            sticky=tk.E,
            padx=(14, 0),
        )

        position_surface = ttk.Frame(positions_stack, padding=(10, 9), style="ManagementCard.TFrame")
        position_surface.pack(fill=tk.BOTH, expand=True)
        self._build_position_table(position_surface)
        self._build_manage_panel(manage_surface)

    def _summary_card(self, parent: ttk.Frame, title: str, key: str, column: int) -> None:
        card = ttk.Frame(parent, padding=(12, 9), style="ManagementCard.TFrame")
        card.grid(row=0, column=column, sticky=tk.NSEW, padx=(0 if column == 0 else 4, 0 if column == 3 else 4))
        title_label = ttk.Label(card, text=title, style="ManagementCardTitle.TLabel")
        title_label.pack(anchor=tk.W)
        value = ttk.Label(card, textvariable=self._summary_values[key], style="ManagementCardValue.TLabel")
        value.pack(anchor=tk.W, pady=(4, 0))
        self._summary_title_labels[key] = title_label
        self._summary_labels[key] = value

    def _build_position_table(self, parent: ttk.Frame) -> None:
        footer = ttk.Frame(parent, style="ManagementCard.TFrame")
        footer.pack(side=tk.BOTTOM, fill=tk.X, pady=(7, 0))
        ttk.Label(footer, textvariable=self.position_status, style="ManagementMuted.TLabel").pack(
            side=tk.LEFT
        )
        ttk.Label(
            footer,
            textvariable=self.position_updated_status,
            style="ManagementMuted.TLabel",
        ).pack(side=tk.RIGHT)
        table = _ExactLegTable(parent, on_selection_changed=self._position_selection_changed)
        table.pack(fill=tk.BOTH, expand=True)
        self.position_table = table

    def _build_manage_panel(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, textvariable=self.management_title, style="ManagementSection.TLabel").pack(anchor=tk.W)
        ttk.Label(
            parent,
            textvariable=self.management_detail,
            style="ManagementMuted.TLabel",
            wraplength=410,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, fill=tk.X, pady=(2, 8))

        actions = ttk.Frame(parent, style="ManagementCard.TFrame")
        actions.pack(fill=tk.X, pady=(0, 7))
        ttk.Button(actions, text="Close", style="ManagementActiveSegment.TButton").pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 3)
        )
        roll = ttk.Button(
            actions,
            text="Roll",
            state=tk.DISABLED,
            style="ManagementSegment.TButton",
            command=self._open_roll,
        )
        roll.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=3)
        self.roll_button = roll
        exit_plan = ttk.Button(
            actions,
            text="Exit Plan",
            state=tk.DISABLED,
            style="ManagementSegment.TButton",
            command=self._open_exit_plan,
        )
        exit_plan.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=3)
        self.exit_plan_button = exit_plan
        ttk.Button(
            actions,
            text="Exercise",
            state=tk.DISABLED,
            style="ManagementSegment.TButton",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(3, 0))

        ttk.Label(parent, text="Close scope", style="ManagementMuted.TLabel").pack(anchor=tk.W)
        scope_choices = ttk.Frame(parent, style="ManagementCard.TFrame")
        scope_choices.pack(fill=tk.X, pady=(4, 7))
        for column in range(2):
            scope_choices.grid_columnconfigure(column, weight=1, uniform="close-scope")
        self._scope_choice(
            scope_choices,
            column=0,
            value=CLOSE_SCOPE_ENTIRE,
            title="Entire position",
            detail=self.close_entire_detail,
        )
        self._scope_choice(
            scope_choices,
            column=1,
            value=CLOSE_SCOPE_SELECTED,
            title="Selected contracts",
            detail=self.close_selected_detail,
        )

        ttk.Label(parent, text="Closing order preview", style="ManagementSection.TLabel").pack(anchor=tk.W)
        preview = _ClosingLegPreview(parent)
        preview.pack(fill=tk.X, pady=(4, 7))
        self.close_preview = preview

        terms = ttk.Frame(parent, style="ManagementCard.TFrame")
        terms.pack(fill=tk.X)
        terms.grid_columnconfigure(0, weight=1)
        terms.grid_columnconfigure(1, weight=1)
        terms.grid_columnconfigure(2, weight=1)
        self._term_label(terms, "Order type", 0, 0)
        ttk.Label(terms, textvariable=self.close_order_type, style="ManagementSelected.TLabel").grid(
            row=1, column=0, sticky=tk.EW, padx=(0, 5), pady=(3, 5)
        )
        self._term_label(terms, "Limit price", 0, 1)
        limit_entry = ttk.Entry(terms, textvariable=self.close_limit_price, state=tk.DISABLED, width=10)
        limit_entry.grid(row=1, column=1, sticky=tk.EW, padx=(5, 0), pady=(3, 5))
        limit_entry.bind("<Return>", self._close_terms_changed)
        limit_entry.bind("<FocusOut>", self._close_terms_changed)
        self.close_limit_entry = limit_entry
        self._term_label(terms, "Time in force", 0, 2)
        duration_box = ttk.Combobox(
            terms,
            textvariable=self.close_duration,
            values=(DAY_ONLY, GOOD_UNTIL_CANCELED),
            state="readonly",
            width=10,
        )
        duration_box.grid(row=1, column=2, sticky=tk.EW, padx=(5, 0), pady=(3, 5))
        duration_box.bind("<<ComboboxSelected>>", self._close_terms_changed)
        self.close_duration_box = duration_box

        rail = ttk.Frame(parent, padding=(8, 5), style="ManagementInset.TFrame")
        rail.pack(fill=tk.X, pady=(4, 0))
        rail.grid_columnconfigure(0, weight=1)
        rail.grid_columnconfigure(1, weight=1)
        ttk.Label(rail, textvariable=self.close_bid_label, style="ManagementInsetMuted.TLabel").grid(
            row=0, column=0, sticky=tk.W
        )
        ttk.Label(rail, textvariable=self.close_ask_label, style="ManagementInsetMuted.TLabel").grid(
            row=0, column=1, sticky=tk.E
        )
        price_scale = tk.Scale(
            rail,
            from_=0.01,
            to=1.0,
            resolution=0.01,
            showvalue=False,
            orient=tk.HORIZONTAL,
            command=self._price_scale_changed,
            background=TABLE_FIELD,
            activebackground="#eaf2fb",
            troughcolor=BORDER,
            highlightthickness=0,
            borderwidth=0,
            sliderlength=14,
            width=7,
            state=tk.DISABLED,
        )
        price_scale.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(2, 0))
        ttk.Label(rail, textvariable=self.close_mid_label, style="ManagementInsetMuted.TLabel").grid(
            row=2, column=0, columnspan=2
        )
        self.close_price_scale = price_scale

        impact = ttk.Frame(parent, padding=(8, 5), style="ManagementInset.TFrame")
        impact.pack(fill=tk.X, pady=(4, 0))
        ttk.Label(impact, text="Estimated impact", style="ManagementInsetMuted.TLabel").grid(
            row=0, column=0, columnspan=3, sticky=tk.W
        )
        for column in range(3):
            impact.grid_columnconfigure(column, weight=1, uniform="close-impact")
        ttk.Label(impact, text="Open P/L (current)", style="ManagementInsetMuted.TLabel").grid(
            row=1, column=0, pady=(6, 1)
        )
        ttk.Label(impact, textvariable=self.close_estimate_title, style="ManagementInsetMuted.TLabel").grid(
            row=1, column=1, pady=(6, 1)
        )
        ttk.Label(impact, text="Fees est.", style="ManagementInsetMuted.TLabel").grid(
            row=1, column=2, pady=(6, 1)
        )
        ttk.Label(impact, textvariable=self.close_open_pnl, style="ManagementInsetBody.TLabel").grid(
            row=2, column=0
        )
        estimate = ttk.Label(
            impact,
            textvariable=self.close_estimate,
            style="ManagementInsetBody.TLabel",
        )
        estimate.grid(row=2, column=1)
        ttk.Label(impact, textvariable=self.close_fees, style="ManagementInsetBody.TLabel").grid(
            row=2, column=2
        )
        ttk.Label(
            impact,
            textvariable=self.close_estimate_source,
            style="ManagementInsetMuted.TLabel",
            wraplength=390,
            justify=tk.LEFT,
        ).grid(row=3, column=0, columnspan=3, sticky=tk.W, pady=(6, 0))
        self.close_estimate_label = estimate

        actions = ttk.Frame(parent, style="ManagementCard.TFrame")
        actions.pack(fill=tk.X, pady=(7, 0))
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_columnconfigure(1, weight=1)
        ttk.Button(
            actions,
            text="Analyze",
            state=tk.DISABLED,
            style="ManagementSegment.TButton",
        ).grid(row=0, column=0, sticky=tk.EW, padx=(0, 4))
        review = ttk.Button(
            actions,
            text="Review closing order",
            style="ManagementPrimary.TButton",
            command=self._review_closing_order,
            state=tk.DISABLED,
        )
        review.grid(row=0, column=1, sticky=tk.EW, padx=(4, 0))
        self.review_close_button = review

    def _scope_choice(
        self,
        parent: ttk.Frame,
        *,
        column: int,
        value: str,
        title: str,
        detail: tk.StringVar,
    ) -> None:
        card = tk.Frame(
            parent,
            background=TABLE_FIELD,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            highlightthickness=1,
            padx=6,
            pady=4,
        )
        card.grid(row=0, column=column, sticky=tk.NSEW, padx=(0, 4) if column == 0 else (4, 0))
        radio = tk.Radiobutton(
            card,
            text=title,
            variable=self.close_scope_mode,
            value=value,
            command=self._close_scope_changed,
            anchor=tk.W,
            background=TABLE_FIELD,
            foreground=TEXT,
            activebackground=TABLE_FIELD,
            activeforeground=TEXT,
            selectcolor=SURFACE_ALT,
            font=("Segoe UI", 9, "bold"),
            borderwidth=0,
            highlightthickness=0,
        )
        radio.pack(fill=tk.X)
        detail_label = tk.Label(
            card,
            textvariable=detail,
            anchor=tk.W,
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        )
        detail_label.pack(fill=tk.X, padx=(22, 0), pady=(0, 2))
        self._scope_controls[value] = (card, radio, detail_label)

    @staticmethod
    def _term_label(parent: ttk.Frame, text: str, row: int, column: int) -> None:
        ttk.Label(parent, text=text, style="ManagementMuted.TLabel").grid(
            row=row,
            column=column,
            sticky=tk.EW,
            padx=(0, 5) if column == 0 else (5, 0),
        )

    def _build_orders(self, parent: ttk.Frame) -> None:
        outer = ttk.Frame(parent, padding=(10, 9), style="ManagementPage.TFrame")
        outer.pack(fill=tk.BOTH, expand=True)
        header = ttk.Frame(outer, padding=(11, 8), style="ManagementCard.TFrame")
        header.pack(fill=tk.X, pady=(0, 9))
        heading = ttk.Frame(header, style="ManagementCard.TFrame")
        heading.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(heading, text="Option orders", style="ManagementSection.TLabel").pack(anchor=tk.W)
        ttk.Label(heading, textvariable=self.order_source_status, style="ManagementMuted.TLabel").pack(anchor=tk.W)
        ttk.Button(header, text="Working orders", command=self._show_working_orders).pack(side=tk.LEFT, padx=(8, 4))
        recent = ttk.Button(header, text="Load recent", command=self._load_recent_orders)
        recent.pack(side=tk.LEFT, padx=(4, 0))
        self.load_recent_button = recent

        panes = ttk.PanedWindow(outer, orient=tk.VERTICAL)
        panes.pack(fill=tk.BOTH, expand=True)
        table_surface = ttk.Frame(panes, padding=(10, 9), style="ManagementCard.TFrame")
        detail_surface = ttk.Frame(panes, padding=(10, 9), style="ManagementCard.TFrame")
        panes.add(table_surface, weight=2)
        panes.add(detail_surface, weight=2)
        self._build_order_table(table_surface)
        self._build_order_detail(detail_surface)

    def _build_order_table(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent, style="ManagementCard.TFrame")
        frame.pack(fill=tk.BOTH, expand=True)
        columns = ("order_id", "status", "entered", "type", "legs", "remaining", "price", "tif")
        table = ttk.Treeview(frame, columns=columns, show="headings", selectmode="browse", height=11)
        for name, label, width, anchor in (
            ("order_id", "Order ID", 105, tk.W),
            ("status", "Status", 105, tk.W),
            ("entered", "Entered", 165, tk.W),
            ("type", "Type", 105, tk.W),
            ("legs", "Exact legs", 395, tk.W),
            ("remaining", "Remaining", 80, tk.E),
            ("price", "Limit / stop", 105, tk.E),
            ("tif", "TIF", 95, tk.W),
        ):
            table.heading(name, text=label)
            table.column(name, width=width, minwidth=55, anchor=anchor, stretch=name == "legs")
        scroll_y = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=table.yview)
        scroll_x = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=table.xview)
        table.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        scroll_y.pack(side=tk.RIGHT, fill=tk.Y)
        scroll_x.pack(side=tk.BOTTOM, fill=tk.X)
        table.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        table.bind("<<TreeviewSelect>>", self._order_selection_changed)
        self.order_table = table

    def _build_order_detail(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text="Selected order", style="ManagementSection.TLabel").pack(anchor=tk.W)
        ttk.Label(
            parent,
            textvariable=self.order_detail,
            style="ManagementMuted.TLabel",
            wraplength=1000,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, fill=tk.X, pady=(2, 7))

        actions = ttk.Frame(parent, style="ManagementCard.TFrame")
        actions.pack(side=tk.BOTTOM, fill=tk.X, pady=(8, 0))
        cancel = ttk.Button(
            actions,
            text="Cancel selected order",
            style="ManagementDanger.TButton",
            command=self._cancel_selected_order,
            state=tk.DISABLED,
        )
        cancel.pack(side=tk.LEFT)
        self.cancel_order_button = cancel
        ttk.Button(actions, text="Replace", state=tk.DISABLED).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(
            actions,
            text="Replace is unavailable because no verified replace service is wired.",
            style="ManagementWarning.TLabel",
        ).pack(side=tk.LEFT, padx=(10, 0))

        legs = ttk.Treeview(
            parent,
            columns=("action", "quantity", "contract"),
            show="headings",
            selectmode="none",
            height=3,
        )
        for name, label, width, anchor in (
            ("action", "Action", 140, tk.W),
            ("quantity", "Remaining qty", 105, tk.E),
            ("contract", "Exact OCC contract", 520, tk.W),
        ):
            legs.heading(name, text=label)
            legs.column(name, width=width, anchor=anchor, stretch=name == "contract")
        legs.pack(fill=tk.BOTH, expand=True)
        self.order_legs_table = legs

    def show_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        self.snapshot = snapshot
        self.book = option_position_book(snapshot)
        self.position_account.set(self.book.account_label or "Schwab account")
        self._working_orders = option_orders_from_snapshot(snapshot)
        self._update_summary()
        symbols = tuple(sorted({leg.underlying_symbol for leg in self.book.legs if leg.underlying_symbol}))
        expirations = tuple(sorted({leg.expiration for leg in self.book.legs if leg.expiration}))
        self._position_symbol_box.configure(values=(ALL_SYMBOLS, *symbols))
        self._position_expiration_box.configure(values=(ALL_EXPIRATIONS, *expirations))
        if self.position_symbol.get() not in (ALL_SYMBOLS, *symbols):
            self.position_symbol.set(ALL_SYMBOLS)
        if self.position_expiration.get() not in (ALL_EXPIRATIONS, *expirations):
            self.position_expiration.set(ALL_EXPIRATIONS)
        self._render_positions()
        self._show_working_orders()

    def show_refresh_error(self, exc: Exception) -> None:
        if self.book is None:
            self.position_status.set("Schwab option positions unavailable")
        else:
            self.position_updated_status.set(
                f"Refresh failed · showing prior data · {type(exc).__name__}"
            )

    def _update_summary(self) -> None:
        if self.book is None:
            return
        summary = self.book.summary
        values = {
            "value": summary.net_market_value,
            "open": summary.unrealized_pnl,
            "day": summary.theta_per_day,
            "funds": summary.buying_power if summary.buying_power is not None else summary.available_funds,
        }
        self._summary_title_labels["funds"].configure(
            text="Buying power" if summary.buying_power is not None else "Available funds"
        )
        for key, value in values.items():
            self._summary_values[key].set(_money(value))
            style = "ManagementCardValue.TLabel"
            if key in {"open", "day"} and value is not None:
                style = "ManagementCardPositive.TLabel" if value >= 0 else "ManagementCardNegative.TLabel"
            self._summary_labels[key].configure(style=style)

    def _position_filters_changed(self, _event: object = None) -> None:
        self._render_positions()

    def _render_positions(self) -> None:
        preferred_symbols = self.position_table.selected_symbols() if self.position_table is not None else ()
        self._clear_manage_panel()
        if self.book is None or self.position_table is None:
            return
        symbol = "" if self.position_symbol.get() == ALL_SYMBOLS else self.position_symbol.get()
        expiration = "" if self.position_expiration.get() == ALL_EXPIRATIONS else self.position_expiration.get()
        visible = filter_option_positions(self.book, symbol=symbol, expiration=expiration)
        selected = self.position_table.set_rows(visible, preferred_symbols=preferred_symbols)
        if visible:
            total = len(self.book.legs)
            count = str(len(visible)) if len(visible) == total else f"{len(visible)} of {total}"
            self.position_status.set(f"{count} position{'s' if len(visible) != 1 else ''}")
        else:
            self.position_status.set("0 positions")
        self.position_updated_status.set(f"Updated {_clock_timestamp(self.book.observed_at)}")
        if selected:
            self.close_scope_mode.set(
                CLOSE_SCOPE_SELECTED if len(selected) > 1 else CLOSE_SCOPE_ENTIRE
            )
            self._render_close_selection(selected)

    def _position_selection_changed(self, selected: tuple[OptionPositionLeg, ...]) -> None:
        if self.book is None:
            return
        self.close_scope_mode.set(
            CLOSE_SCOPE_SELECTED if len(selected) > 1 else CLOSE_SCOPE_ENTIRE
        )
        self._render_close_selection(selected)

    def _render_close_selection(self, selected: tuple[OptionPositionLeg, ...]) -> None:
        if self.close_preview is not None:
            self.close_preview.clear()
        if not selected or self.book is None:
            self._clear_manage_panel()
            return
        self._update_scope_controls(selected)
        scoped = self._scoped_position_rows(selected)
        if len(scoped) == 1:
            leg = scoped[0]
            dte = days_to_expiration(leg.expiration, leg.observed_at)
            self.management_title.set("Manage position")
            self.management_detail.set(
                f"{leg.underlying_symbol} · {leg.strike:g} {leg.option_type.title()} · "
                f"{_number(dte)} DTE · exact OCC"
            )
            self.close_scope.set("Exact position · closes the highlighted contract")
        else:
            self.management_title.set(f"Manage {len(scoped)} exact positions")
            self.management_detail.set(
                f"{scoped[0].underlying_symbol} · custom close · no inferred strategy grouping"
            )
            self.close_scope.set(f"{len(scoped)} exact positions · highlighted rows only")
        try:
            draft = build_closing_order_draft(
                self.book,
                (leg.symbol for leg in scoped),
                duration=self.close_duration.get(),
            )
        except Exception as exc:
            self._render_close_error(exc, scoped)
            return
        self._apply_close_draft(draft, scoped)

    def _apply_close_draft(
        self,
        draft: ClosingOrderDraft,
        scoped: tuple[OptionPositionLeg, ...],
    ) -> None:
        self._close_draft = draft
        self._ticket_updating = True
        self.close_order_type.set(draft.api_order_type.replace("_", " ").title())
        self.close_limit_price.set(f"{draft.limit_price:.2f}")
        self.close_estimate_title.set(
            "Est. proceeds" if draft.estimated_cash_effect >= 0 else "Est. cost"
        )
        self.close_estimate.set(_money(abs(draft.estimated_cash_effect)))
        pnl_values = tuple(leg.unrealized_pnl for leg in scoped)
        open_pnl = None if any(value is None for value in pnl_values) else sum(
            float(value) for value in pnl_values if value is not None
        )
        self.close_open_pnl.set(_money(open_pnl))
        self.close_fees.set("At broker review")
        self.close_estimate_source.set(
            f"{draft.price_source} · fees and broker buying-power impact are not locally estimated."
        )
        if self.close_estimate_label is not None:
            self.close_estimate_label.configure(
                style=(
                    "ManagementEstimatePositive.TLabel"
                    if draft.estimated_cash_effect >= 0
                    else "ManagementEstimateNegative.TLabel"
                )
            )
        if self.close_preview is not None:
            self.close_preview.set_legs(draft.legs)
        self._set_close_controls(True)
        self._update_roll_control(scoped)
        self._configure_price_rail(draft)
        self._ticket_updating = False

    def _render_close_error(
        self,
        exc: Exception,
        scoped: tuple[OptionPositionLeg, ...],
    ) -> None:
        self._close_draft = None
        self.close_order_type.set("Unavailable")
        self.close_limit_price.set("")
        self.close_estimate_title.set("Close unavailable")
        self.close_estimate.set("—")
        pnl_values = tuple(leg.unrealized_pnl for leg in scoped)
        open_pnl = None if any(value is None for value in pnl_values) else sum(
            float(value) for value in pnl_values if value is not None
        )
        self.close_open_pnl.set(_money(open_pnl))
        self.close_fees.set("—")
        self.close_estimate_source.set(str(exc))
        self._clear_price_rail()
        if self.close_estimate_label is not None:
            self.close_estimate_label.configure(style="ManagementInsetBody.TLabel")
        self._set_close_controls(False)
        self._update_roll_control(scoped)

    def _configure_price_rail(self, draft: ClosingOrderDraft) -> None:
        rail = _closing_price_rail(draft)
        if rail is None or self.close_price_scale is None:
            self._clear_price_rail()
            return
        bid, midpoint, ask = rail
        self.close_bid_label.set(f"Bid {_money(bid)}")
        self.close_mid_label.set(f"Mid {_money(midpoint)}")
        self.close_ask_label.set(f"Ask {_money(ask)}")
        scale_from = bid
        scale_to = ask
        if scale_to <= scale_from:
            scale_from = max(0.01, bid - 0.01)
            scale_to = ask + 0.01
        self.close_price_scale.configure(
            from_=scale_from,
            to=scale_to,
            state=tk.NORMAL,
        )
        self.close_price_scale.set(min(max(draft.limit_price, scale_from), scale_to))

    def _clear_price_rail(self) -> None:
        self.close_bid_label.set("Bid —")
        self.close_mid_label.set("Mid —")
        self.close_ask_label.set("Ask —")
        if self.close_price_scale is not None:
            self.close_price_scale.configure(state=tk.DISABLED)

    def _price_scale_changed(self, value: str) -> None:
        if self._ticket_updating or self.book is None:
            return
        self._reprice_close(value)

    def _close_terms_changed(self, _event: object = None) -> str | None:
        if self._ticket_updating or self.book is None:
            return None
        self._reprice_close(self.close_limit_price.get())
        return "break" if getattr(_event, "keysym", "") == "Return" else None

    def _reprice_close(self, limit_price: object) -> None:
        scoped = self._scoped_position_rows()
        if not scoped or self.book is None:
            return
        try:
            draft = build_closing_order_draft(
                self.book,
                (leg.symbol for leg in scoped),
                duration=self.close_duration.get(),
                limit_price=limit_price,
            )
        except Exception as exc:
            self.close_estimate_title.set("Invalid ticket")
            self.close_estimate.set("—")
            self.close_estimate_source.set(str(exc))
            if self.review_close_button is not None:
                self.review_close_button.configure(state=tk.DISABLED)
            return
        self._apply_close_draft(draft, scoped)

    def _close_scope_changed(self) -> None:
        selected = self.position_table.selected_rows() if self.position_table is not None else ()
        if selected:
            self._render_close_selection(selected)

    def _update_scope_controls(self, selected: tuple[OptionPositionLeg, ...]) -> None:
        self.close_entire_detail.set("Close active OCC contract")
        self.close_selected_detail.set(
            f"Close all {len(selected)} highlighted contracts"
            if len(selected) > 1
            else "Highlight 2+ contracts"
        )
        for value, (card, radio, detail) in self._scope_controls.items():
            enabled = bool(selected) and (value != CLOSE_SCOPE_SELECTED or len(selected) > 1)
            radio.configure(state=tk.NORMAL if enabled else tk.DISABLED)
            detail.configure(foreground=MUTED_TEXT if enabled else "#536174")
            chosen = enabled and self.close_scope_mode.get() == value
            card.configure(highlightbackground=ACCENT if chosen else BORDER)

    def _scoped_position_rows(
        self,
        selected: tuple[OptionPositionLeg, ...] | None = None,
    ) -> tuple[OptionPositionLeg, ...]:
        if self.position_table is None:
            return ()
        current = self.position_table.selected_rows() if selected is None else selected
        active = self.position_table.active_row()
        return _closing_scope_rows(
            current,
            active_symbol=active.symbol if active is not None else None,
            scope=self.close_scope_mode.get(),
        )

    def _set_close_controls(self, enabled: bool) -> None:
        state = tk.NORMAL if enabled else tk.DISABLED
        if self.close_limit_entry is not None:
            self.close_limit_entry.configure(state=state)
        if self.close_duration_box is not None:
            self.close_duration_box.configure(state="readonly" if enabled else tk.DISABLED)
        if self.review_close_button is not None:
            self.review_close_button.configure(state=state)
        if self.exit_plan_button is not None:
            self.exit_plan_button.configure(state=state)
        if not enabled and self.roll_button is not None:
            self.roll_button.configure(state=tk.DISABLED)

    def _update_roll_control(self, scoped: tuple[OptionPositionLeg, ...]) -> None:
        if self.roll_button is None or self.book is None or not scoped:
            if self.roll_button is not None:
                self.roll_button.configure(state=tk.DISABLED)
            return
        reason = roll_action_disabled_reason(
            self.book,
            (leg.symbol for leg in scoped),
        )
        self.roll_button.configure(state=tk.DISABLED if reason else tk.NORMAL)

    def _clear_manage_panel(self) -> None:
        self.management_title.set("Select exact option legs")
        self.management_detail.set("Use Ctrl or Shift to select more than one ungrouped leg.")
        self.close_scope.set("Selected exact positions · highlighted rows only")
        self.close_scope_mode.set(CLOSE_SCOPE_ENTIRE)
        self.close_order_type.set("—")
        self.close_limit_price.set("")
        self.close_open_pnl.set("—")
        self.close_estimate_title.set("Est. proceeds / cost")
        self.close_estimate.set("")
        self.close_fees.set("At broker review")
        self.close_estimate_source.set("")
        self._close_draft = None
        self._clear_price_rail()
        self._update_scope_controls(())
        if self.close_estimate_label is not None:
            self.close_estimate_label.configure(style="ManagementInsetBody.TLabel")
        if self.close_preview is not None:
            self.close_preview.clear()
        self._set_close_controls(False)

    def _selected_position_symbols(self) -> tuple[str, ...]:
        return tuple(leg.symbol for leg in self._scoped_position_rows())

    def _review_closing_order(self) -> None:
        if self.book is None:
            return
        try:
            draft = build_closing_order_draft(
                self.book,
                self._selected_position_symbols(),
                duration=self.close_duration.get(),
                limit_price=self.close_limit_price.get(),
            )
        except Exception as exc:
            messagebox.showerror("Closing order unavailable", str(exc))
            return
        self._open_universal_close_review(draft)

    def _open_exit_plan(self) -> None:
        if self.book is None:
            return
        symbols = self._selected_position_symbols()
        if not symbols:
            return
        ExitPlanBuilderDialog(
            root=self.root,
            book=self.book,
            selected_symbols=symbols,
            working_orders=self._working_orders,
            on_close_now=self._review_closing_order,
            on_show_orders=self.on_show_orders,
        )

    def _open_roll(self) -> None:
        if self.book is None:
            return
        scoped = self._scoped_position_rows()
        if not scoped:
            return
        reason = roll_action_disabled_reason(
            self.book,
            (leg.symbol for leg in scoped),
        )
        if reason:
            messagebox.showerror("Roll unavailable", reason)
            return
        underlying = scoped[0].underlying_symbol

        def load_chain() -> object:
            session = self.session_factory()
            getter = getattr(session, "get_option_chain", None)
            if not callable(getter):
                raise TypeError("Schwab session does not provide get_option_chain.")
            return getter(underlying, 100)

        RollWorkspaceDialog(
            root=self.root,
            book=self.book,
            position_symbols=tuple(leg.symbol for leg in scoped),
            snapshot_loader=self.snapshot_loader,
            chain_loader=load_chain,
            on_review=self._review_roll,
        )

    def _review_roll(self, draft: RollOrderDraft) -> None:
        controller = OptionOrderReviewController(
            review=roll_order_review(draft, now=datetime.now(timezone.utc)),
            draft=draft,
        )
        origin = self.root.grab_current() or self.root
        OptionOrderReviewDialog(root=origin, controller=controller)

    def _open_universal_close_review(self, draft: ClosingOrderDraft) -> None:
        controller = OptionOrderReviewController(
            review=closing_order_review(draft, now=datetime.now(timezone.utc)),
            draft=draft,
            snapshot_loader=self.snapshot_loader,
            session_factory=self.session_factory,
            on_accepted=lambda: self.root.after(0, self.on_refresh),
            on_unknown=lambda: self.root.after(0, self._load_recent_orders),
        )
        OptionOrderReviewDialog(root=self.root, controller=controller)

    def _show_working_orders(self) -> None:
        self.order_source_status.set("Working orders from the latest account refresh")
        self._render_orders(self._working_orders)

    def _load_recent_orders(self) -> None:
        if self.load_recent_button is not None:
            self.load_recent_button.configure(state=tk.DISABLED)
        self.order_source_status.set("Loading recent Schwab option orders")

        def work() -> tuple[ManagedOptionOrder, ...]:
            session = self.session_factory()
            getter = getattr(session, "get_recent_orders", None)
            if not callable(getter):
                raise TypeError("Schwab session does not provide get_recent_orders.")
            return option_orders_from_payload(getter())

        def succeeded(orders: tuple[ManagedOptionOrder, ...]) -> None:
            if self.load_recent_button is not None:
                self.load_recent_button.configure(state=tk.NORMAL)
            self.order_source_status.set(
                f"Recent option orders · {len(orders)} result{'s' if len(orders) != 1 else ''}"
            )
            self._render_orders(orders)

        def failed(exc: Exception) -> None:
            if self.load_recent_button is not None:
                self.load_recent_button.configure(state=tk.NORMAL)
            self.order_source_status.set("Recent order load failed; prior rows were preserved")
            messagebox.showerror("Recent orders failed", f"{type(exc).__name__}: {exc}")

        run_in_background(self.root, work, succeeded, failed)

    def _render_orders(self, orders: tuple[ManagedOptionOrder, ...]) -> None:
        _clear_tree(self.order_table)
        _clear_tree(self.order_legs_table)
        self._visible_order_by_iid = {}
        self.order_detail.set("Select an option order to inspect every leg.")
        if self.cancel_order_button is not None:
            self.cancel_order_button.configure(state=tk.DISABLED)
        if self.order_table is None:
            return
        for index, order in enumerate(orders):
            iid = f"order-{index}"
            self._visible_order_by_iid[iid] = order
            price_parts = []
            if order.limit_price is not None:
                price_parts.append(_money(order.limit_price))
            if order.stop_price is not None:
                price_parts.append(f"stop {_money(order.stop_price)}")
            leg_summary = " · ".join(
                f"{_human_instruction(leg.instruction)} {_number(leg.quantity)} {leg.symbol}"
                for leg in order.legs
            )
            self.order_table.insert(
                "",
                tk.END,
                iid=iid,
                values=(
                    order.order_id or "—",
                    order.status.replace("_", " ").title(),
                    order.entered_time or "—",
                    (order.complex_order_strategy_type or order.order_type or "—").replace("_", " ").title(),
                    leg_summary,
                    _number(order.remaining_quantity),
                    " / ".join(price_parts) or "—",
                    order.duration.replace("_", " ").title() if order.duration else "—",
                ),
            )

    def _order_selection_changed(self, _event: object = None) -> None:
        _clear_tree(self.order_legs_table)
        if self.order_table is None:
            return
        selected = self.order_table.selection()
        order = self._visible_order_by_iid.get(selected[0]) if selected else None
        if order is None:
            return
        self.order_detail.set(
            f"Order {order.order_id or 'without an ID'} · {order.status.replace('_', ' ').title()} · "
            f"{len(order.legs)} exact option leg{'s' if len(order.legs) != 1 else ''}"
        )
        if self.order_legs_table is not None:
            for leg in order.legs:
                self.order_legs_table.insert(
                    "",
                    tk.END,
                    values=(
                        _human_instruction(leg.instruction),
                        _number(leg.quantity),
                        leg.symbol,
                    ),
                )
        if self.cancel_order_button is not None:
            self.cancel_order_button.configure(state=tk.NORMAL if order.can_cancel else tk.DISABLED)
        if order.cancel_disabled_reason:
            self.order_detail.set(f"{self.order_detail.get()} · {order.cancel_disabled_reason}")

    def _selected_order(self) -> ManagedOptionOrder | None:
        if self.order_table is None:
            return None
        selected = self.order_table.selection()
        return self._visible_order_by_iid.get(selected[0]) if selected else None

    def _cancel_selected_order(self) -> None:
        order = self._selected_order()
        if order is None or not order.can_cancel:
            return
        legs = "\n".join(
            f"- {_human_instruction(leg.instruction)} {_number(leg.quantity)} {leg.symbol}"
            for leg in order.legs
        )
        if not messagebox.askyesno(
            "Confirm option order cancellation",
            f"Cancel Schwab option order {order.order_id}?\n\n{legs}\n\nThis does not close a held position.",
        ):
            return
        if self.cancel_order_button is not None:
            self.cancel_order_button.configure(state=tk.DISABLED)

        def work() -> object:
            session = self.session_factory()
            cancel = getattr(session, "cancel_order", None)
            if not callable(cancel):
                raise TypeError("Schwab session does not provide cancel_order.")
            return cancel(order.order_id)

        def succeeded(_result: object) -> None:
            messagebox.showinfo("Option order cancellation submitted", f"Order {order.order_id} cancellation was accepted.")
            self.on_refresh()

        def failed(exc: Exception) -> None:
            if self.cancel_order_button is not None:
                self.cancel_order_button.configure(state=tk.NORMAL)
            messagebox.showerror("Cancel order failed", f"{type(exc).__name__}: {exc}")

        run_in_background(self.root, work, succeeded, failed)


def _clear_tree(table: ttk.Treeview | None) -> None:
    if table is None:
        return
    for item in table.get_children():
        table.delete(item)


def _human_instruction(value: str) -> str:
    return {
        "BUY_TO_OPEN": "Buy to open",
        "SELL_TO_OPEN": "Sell to open",
        "BUY_TO_CLOSE": "Buy to close",
        "SELL_TO_CLOSE": "Sell to close",
        "BUY": "Buy",
        "SELL": "Sell",
    }.get(value, value.replace("_", " ").title())


def _signed_value_color(value: float | None) -> str:
    if value is None or value == 0:
        return TEXT
    return SUCCESS if value > 0 else DANGER


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def _number(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:g}"


def _timestamp(value: datetime | None) -> str:
    if value is None:
        return "time unavailable"
    local = value.astimezone() if value.tzinfo is not None else value
    return local.strftime("%b %d, %Y %I:%M:%S %p")


def _clock_timestamp(value: datetime | None) -> str:
    if value is None:
        return "time unavailable"
    local = value.astimezone() if value.tzinfo is not None else value
    return local.strftime("%I:%M:%S %p").lstrip("0")


def _short_expiration(value: str) -> str:
    try:
        expiration = date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return "—"
    return expiration.strftime("%m/%d/%y")


def days_to_expiration(expiration: str, observed_at: datetime | None) -> int | None:
    try:
        expiration_date = date.fromisoformat(expiration[:10])
    except (TypeError, ValueError):
        return None
    observed_date = (observed_at.astimezone().date() if observed_at and observed_at.tzinfo else observed_at.date()) if observed_at else date.today()
    return (expiration_date - observed_date).days


__all__ = ["OptionsManagementView", "days_to_expiration"]
