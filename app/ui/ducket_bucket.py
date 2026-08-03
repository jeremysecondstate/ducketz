from __future__ import annotations

import math
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk

from app.models.portfolio import PortfolioSnapshot
from app.services.aggregate import DucketBucketSnapshot
from app.ui.options_strategies import OptionsStrategiesTab
from app.ui.rolling_forecasts import RollingForecastTab
from app.ui.schwab_order_messages import (
    order_confirmation_message,
    order_submitted_message,
)
from app.ui.theme import (
    ACCENT,
    BACKGROUND,
    BODY_FONT,
    BORDER,
    DANGER,
    FIELD_BACKGROUND,
    FIELD_TEXT,
    HEADER_HOVER,
    HEADER_HOVER_TEXT,
    MUTED_LABEL_FONT,
    MUTED_TEXT,
    SUCCESS,
    SURFACE,
    SURFACE_ALT,
    TABLE_FIELD,
    TEXT,
)

from app.services.hyperliquid import HyperliquidInfoClient, sync_hyperliquid_portfolios
from app.services.hyperliquid_trading import (
    HyperliquidExecutionAdapter,
    HyperliquidOrderTicket,
    format_hyperliquid_limit_price,
    normalize_hyperliquid_coin,
    normalize_hyperliquid_limit_price,
    normalize_hyperliquid_spot_market,
)

from app.services.schwab import sync_schwab_portfolio, SchwabSession
from app.services.schwab_order_fields import (
    SCHWAB_EQUITY_ORDER_TYPE_CHOICES,
    SCHWAB_EQUITY_SIDE_CHOICES,
    SCHWAB_EQUITY_TIME_IN_FORCE_CHOICES,
    SCHWAB_OPTION_STRATEGY_CHOICES,
    schwab_equity_session_duration,
    schwab_equity_tif_requires_limit_order,
    schwab_option_strategy_is_supported,
)

HYPERLIQUID_SIDE_CHOICES = ("buy", "sell")
HYPERLIQUID_ORDER_TYPE_CHOICES = ("limit", "market", "trigger")
HYPERLIQUID_TIF_CHOICES = ("Gtc", "Ioc", "Alo")
HYPERLIQUID_SPOT_SIZE_UNITS = ("USDC", "BASE")
HYPERLIQUID_MARGIN_MODE_CHOICES = ("Cross", "Isolated")


def run_ducket_bucket_ui() -> None:
    root = tk.Tk()
    DucketBucketApp(root)
    root.mainloop()


class DucketBucketApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Duckets")
        self.root.geometry("1180x760")
        self.root.configure(background=BACKGROUND)
        self._apply_theme()

        self._build_layout()

    def _apply_theme(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")

        style.configure(".", background=BACKGROUND, foreground=TEXT, fieldbackground=TABLE_FIELD)
        style.configure("TFrame", background=BACKGROUND)
        style.configure("TLabel", background=BACKGROUND, foreground=TEXT, font=BODY_FONT)
        style.configure(
            "TLabelframe",
            background=BACKGROUND,
            foreground=TEXT,
            bordercolor=BORDER,
            borderwidth=1,
            relief=tk.FLAT,
        )
        style.configure("TLabelframe.Label", background=BACKGROUND, foreground=TEXT)
        style.configure("TButton", background=SURFACE_ALT, foreground=TEXT, bordercolor=BORDER, focusthickness=1)
        style.map(
            "TButton",
            background=[("active", ACCENT), ("disabled", SURFACE)],
            foreground=[("disabled", MUTED_TEXT)],
        )

        style.configure(
            "Summary.TLabelframe",
            background=SURFACE,
            foreground=TEXT,
            bordercolor=BORDER,
        )
        style.configure(
            "Summary.TLabelframe.Label",
            background=SURFACE,
            foreground=TEXT,
        )
        style.configure(
            "Summary.TLabel",
            background=SURFACE,
            foreground=TEXT,
        )

        style.configure(
            "Treeview",
            background=TABLE_FIELD,
            foreground=TEXT,
            fieldbackground=TABLE_FIELD,
            bordercolor=BORDER,
            font=BODY_FONT,
            rowheight=26,
        )
        style.configure(
            "Treeview.Heading",
            background=SURFACE_ALT,
            foreground=TEXT,
            bordercolor=BORDER,
            font=MUTED_LABEL_FONT,
        )
        style.map(
            "Treeview.Heading",
            background=[
                ("active", HEADER_HOVER),
                ("pressed", HEADER_HOVER),
            ],
            foreground=[
                ("active", HEADER_HOVER_TEXT),
                ("pressed", HEADER_HOVER_TEXT),
            ],
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT)],
            foreground=[("selected", "#020617")],
        )
        style.configure(
            "TNotebook",
            background=BACKGROUND,
            bordercolor=BORDER,
            borderwidth=0,
        )
        style.configure(
            "TNotebook.Tab",
            background=SURFACE,
            foreground=TEXT,
            font=MUTED_LABEL_FONT,
            padding=(12, 7),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", SURFACE_ALT), ("active", HEADER_HOVER)],
            foreground=[("selected", TEXT), ("active", HEADER_HOVER_TEXT)],
        )
        style.configure(
            "TEntry",
            fieldbackground=FIELD_BACKGROUND,
            foreground=FIELD_TEXT,
            insertcolor=FIELD_TEXT,
        )
        style.configure(
            "TCombobox",
            fieldbackground=FIELD_BACKGROUND,
            background=FIELD_BACKGROUND,
            foreground=FIELD_TEXT,
            arrowcolor=FIELD_TEXT,
            selectbackground=FIELD_BACKGROUND,
            selectforeground=FIELD_TEXT,
        )
        style.map(
            "TCombobox",
            fieldbackground=[
                ("readonly", FIELD_BACKGROUND),
                ("active", FIELD_BACKGROUND),
            ],
            foreground=[
                ("readonly", FIELD_TEXT),
                ("active", FIELD_TEXT),
            ],
            selectbackground=[
                ("readonly", FIELD_BACKGROUND),
                ("active", FIELD_BACKGROUND),
            ],
            selectforeground=[
                ("readonly", FIELD_TEXT),
                ("active", FIELD_TEXT),
            ],
        )

    def _build_layout(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True)

        forecasts_frame = ttk.Frame(notebook)
        strategies_frame = ttk.Frame(notebook)
        bucket_frame = ttk.Frame(notebook)
        schwab_frame = ttk.Frame(notebook)
        hyperliquid_frame = ttk.Frame(notebook)

        notebook.add(forecasts_frame, text="Rolling Forecasts")
        notebook.add(strategies_frame, text="Options Strategies")
        notebook.add(bucket_frame, text="Ducket Bucket")
        notebook.add(schwab_frame, text="Schwab Duckets")
        notebook.add(hyperliquid_frame, text="Hyperliquid Duckets")

        RollingForecastTab(
            root=self.root,
            parent=forecasts_frame,
        )

        OptionsStrategiesTab(
            root=self.root,
            parent=strategies_frame,
        )

        DucketsTab(
            root=self.root,
            parent=bucket_frame,
            title="Ducket Bucket",
            sync_button_text="Sync Bucket",
            sync_snapshots=lambda: [sync_schwab_portfolio(), *sync_hyperliquid_portfolios()],
        )

        SchwabDucketsTab(
            root=self.root,
            parent=schwab_frame,
        )

        HyperliquidDucketsTab(
            root=self.root,
            parent=hyperliquid_frame,
        )


class DucketsTab:
    def __init__(
        self,
        root: tk.Tk,
        parent: ttk.Frame,
        title: str,
        sync_button_text: str,
        sync_snapshots: Callable[[], list[PortfolioSnapshot]],
    ) -> None:
        self.root = root
        self.sync_snapshots = sync_snapshots

        self.cash_value = tk.StringVar(value="Cash: --")
        self.holdings_value = tk.StringVar(value="Holdings: --")
        self.total_value = tk.StringVar(value="Total: --")
        self.unrealized_pnl = tk.StringVar(value="Unrealized PnL: --")
        self.day_pnl = tk.StringVar(value="Day PnL: --")
        self.status_icon = tk.StringVar(value="❌")

        self.sync_button: ttk.Button | None = None
        self.cash_table: ttk.Treeview | None = None
        self.holdings_table: ttk.Treeview | None = None

        self._build(parent, title, sync_button_text)

    def _build(self, parent: ttk.Frame, title: str, sync_button_text: str) -> None:
        root_frame = ttk.Frame(parent, padding=16)
        root_frame.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(root_frame)
        header.pack(fill=tk.X)

        ttk.Label(header, text=title, font=("Segoe UI", 22, "bold")).pack(side=tk.LEFT)

        self.sync_button = ttk.Button(header, text=sync_button_text, command=self._sync)
        self.sync_button.pack(side=tk.RIGHT)

        ttk.Label(
            header,
            textvariable=self.status_icon,
            font=("Segoe UI", 16, "bold"),
            foreground=DANGER,
        ).pack(side=tk.RIGHT, padx=(0, 10))

        summary = ttk.Frame(root_frame)
        summary.pack(fill=tk.X, pady=(16, 12))

        for label_var in (
            self.cash_value,
            self.holdings_value,
            self.total_value,
            self.unrealized_pnl,
            self.day_pnl,
        ):
            card = ttk.LabelFrame(summary, text="", style="Summary.TLabelframe")
            card.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
            ttk.Label(
                card,
                textvariable=label_var,
                font=("Segoe UI", 11, "bold"),
                style="Summary.TLabel",
            ).pack(anchor=tk.W, padx=10, pady=10)

        content_panes = ttk.PanedWindow(root_frame, orient=tk.VERTICAL)
        content_panes.pack(fill=tk.BOTH, expand=True)

        cash_frame = ttk.LabelFrame(content_panes, text="Cash")
        content_panes.add(cash_frame, weight=1)

        self.cash_table = ttk.Treeview(
            cash_frame,
            columns=("account", "bucket", "symbol", "amount", "value"),
            show="headings",
            height=6,
        )
        self._setup_column(self.cash_table, "account", "Account", 140)
        self._setup_column(self.cash_table, "bucket", "Bucket", 100)
        self._setup_column(self.cash_table, "symbol", "Symbol", 100)
        self._setup_column(self.cash_table, "amount", "Amount", 140, anchor=tk.E)
        self._setup_column(self.cash_table, "value", "Value", 140, anchor=tk.E)
        self.cash_table.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        holdings_frame = ttk.LabelFrame(content_panes, text="Holdings")
        content_panes.add(holdings_frame, weight=4)

        self.holdings_table = ttk.Treeview(
            holdings_frame,
            columns=("account", "bucket", "symbol", "quantity", "price", "value", "unrealized_pnl", "day_pnl"),
            show="headings",
            height=14,
        )
        self._setup_column(self.holdings_table, "account", "Account", 140)
        self._setup_column(self.holdings_table, "bucket", "Bucket", 100)
        self._setup_column(self.holdings_table, "symbol", "Symbol", 120)
        self._setup_column(self.holdings_table, "quantity", "Quantity", 110, anchor=tk.E)
        self._setup_column(self.holdings_table, "price", "Price", 110, anchor=tk.E)
        self._setup_column(self.holdings_table, "value", "Value", 120, anchor=tk.E)
        self._setup_column(self.holdings_table, "unrealized_pnl", "Unrealized PnL", 140, anchor=tk.E)
        self._setup_column(self.holdings_table, "day_pnl", "Day PnL", 120, anchor=tk.E)
        self.holdings_table.tag_configure("pnl_positive", foreground=SUCCESS)
        self.holdings_table.tag_configure("pnl_negative", foreground=DANGER)
        self.holdings_table.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

    def _setup_column(
        self,
        table: ttk.Treeview,
        column: str,
        label: str,
        width: int,
        anchor: str = tk.W,
    ) -> None:
        table.heading(column, text=label)
        table.column(column, width=width, anchor=anchor)

    def _sync(self) -> None:
        if self.sync_button is not None:
            self.sync_button.configure(state=tk.DISABLED)

        self.status_icon.set("…")

        thread = threading.Thread(target=self._sync_background, daemon=True)
        thread.start()

    def _sync_background(self) -> None:
        try:
            snapshots = self.sync_snapshots()
            bucket = DucketBucketSnapshot(snapshots=snapshots)
        except Exception as exc:
            self.root.after(0, lambda caught_exc=exc: self._show_error(caught_exc))
            return

        self.root.after(0, lambda: self._show_bucket(bucket))

    def _show_bucket(self, bucket: DucketBucketSnapshot) -> None:
        self.cash_value.set(f"Cash: {_money(bucket.cash_value)}")
        self.holdings_value.set(f"Holdings: {_money(bucket.holdings_value)}")
        self.total_value.set(f"Total: {_money(bucket.total_value)}")
        self.unrealized_pnl.set(f"Unrealized PnL: {_money_or_dash(bucket.unrealized_pnl)}")
        self.day_pnl.set(
            f"Day PnL: {_money_or_dash(bucket.day_pnl)} ({_coverage_or_dash(bucket.day_pnl_accounts)})"
        )

        self._clear_table(self.cash_table)
        self._clear_table(self.holdings_table)

        for snapshot in bucket.snapshots:
            self._insert_snapshot(snapshot)

        self.status_icon.set("✅")

        if self.sync_button is not None:
            self.sync_button.configure(state=tk.NORMAL)

    def _show_error(self, exc: Exception) -> None:
        self.status_icon.set("❌")

        if self.sync_button is not None:
            self.sync_button.configure(state=tk.NORMAL)

        messagebox.showerror("Sync failed", f"{type(exc).__name__}: {exc}")

    def _insert_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        if self.cash_table is None or self.holdings_table is None:
            return

        for cash in snapshot.cash:
            self.cash_table.insert(
                "",
                tk.END,
                values=(
                    snapshot.account_label,
                    cash.bucket,
                    cash.symbol,
                    _number(cash.amount),
                    _money(cash.value),
                ),
            )

        for holding in snapshot.holdings:
            pnl_tag = _pnl_row_tag(holding.unrealized_pnl, holding.day_pnl)

            self.holdings_table.insert(
                "",
                tk.END,
                values=(
                    snapshot.account_label,
                    holding.bucket,
                    holding.symbol,
                    _number(holding.quantity),
                    _money(holding.price),
                    _money(holding.value),
                    _money_or_dash(holding.unrealized_pnl),
                    _money_or_dash(holding.day_pnl),
                ),
                tags=pnl_tag,
            )

    def _clear_table(self, table: ttk.Treeview | None) -> None:
        if table is None:
            return

        for item_id in table.get_children():
            table.delete(item_id)


class SchwabDucketsTab(DucketsTab):
    def __init__(self, root: tk.Tk, parent: ttk.Frame) -> None:
        self.order_id = tk.StringVar()

        self.chain_symbol = tk.StringVar()
        self.chain_strikes = tk.StringVar(value="10")

        self.stock_symbol = tk.StringVar()
        self.stock_side = tk.StringVar(value="BUY")
        self.stock_order_type = tk.StringVar(value="LIMIT")
        self.stock_tif = tk.StringVar(value="DAY")
        self.stock_position_effect = tk.StringVar(value="AUTO")
        self.stock_quantity = tk.StringVar()
        self.stock_entry_limit = tk.StringVar()
        self.stock_stop_price = tk.StringVar()

        self.option_strategy = tk.StringVar(value="SINGLE")
        self.option_symbol = tk.StringVar()
        self.option_side = tk.StringVar(value="BUY_TO_OPEN")
        self.option_order_type = tk.StringVar(value="LIMIT")
        self.option_tif = tk.StringVar(value="DAY")
        self.option_contracts = tk.StringVar()
        self.option_expiration = tk.StringVar()
        self.option_strike = tk.StringVar()
        self.option_call_put = tk.StringVar()
        self.option_bid = tk.StringVar()
        self.option_ask = tk.StringVar()
        self.option_mark = tk.StringVar()
        self.option_limit_debit = tk.StringVar()
        self.option_short_strike = tk.StringVar()
        self.option_credit = tk.StringVar()
        self.option_target_price = tk.StringVar()

        self.open_orders_table: ttk.Treeview | None = None
        self.recent_orders_table: ttk.Treeview | None = None
        self.option_chain_table: ttk.Treeview | None = None

        super().__init__(
            root=root,
            parent=parent,
            title="Schwab Duckets",
            sync_button_text="Sync Schwab",
            sync_snapshots=lambda: [sync_schwab_portfolio()],
        )

    def _build(self, parent: ttk.Frame, title: str, sync_button_text: str) -> None:
        root_panes = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        root_panes.pack(fill=tk.BOTH, expand=True)

        balances_frame = ttk.Frame(root_panes)
        actions_frame = ttk.LabelFrame(root_panes, text="Schwab Order Actions")

        root_panes.add(balances_frame, weight=3)
        root_panes.add(actions_frame, weight=2)

        super()._build(balances_frame, title, sync_button_text)

        if self.holdings_table is not None:
            self.holdings_table.bind("<<TreeviewSelect>>", self._use_selected_holding)

        actions_panes = ttk.PanedWindow(actions_frame, orient=tk.HORIZONTAL)
        actions_panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        left_frame = ttk.Frame(actions_panes)
        right_frame = ttk.Frame(actions_panes)

        actions_panes.add(left_frame, weight=2)
        actions_panes.add(right_frame, weight=3)

        left_panes = ttk.PanedWindow(left_frame, orient=tk.VERTICAL)
        left_panes.pack(fill=tk.BOTH, expand=True)

        right_panes = ttk.PanedWindow(right_frame, orient=tk.VERTICAL)
        right_panes.pack(fill=tk.BOTH, expand=True)

        self._build_ticket_panel(left_panes)
        self._build_orders_panel(right_panes)
        self._build_option_chain_panel(right_panes)

    def _build_ticket_panel(self, parent: ttk.Frame) -> None:
        ticket_frame = ttk.LabelFrame(parent, text="Schwab Stock / ETF Ticket")
        parent.add(ticket_frame, weight=4)

        ticket_columns = ttk.PanedWindow(ticket_frame, orient=tk.HORIZONTAL)
        ticket_columns.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        stock_frame = ttk.LabelFrame(ticket_columns, text="Stock / ETF Ticket")
        option_frame = ttk.LabelFrame(ticket_columns, text="Options Ticket Fields")

        ticket_columns.add(stock_frame, weight=1)
        ticket_columns.add(option_frame, weight=1)

        self._entry_row(stock_frame, "Symbol", self.stock_symbol, 0, 0)
        self._combo_row(stock_frame, "Side", self.stock_side, SCHWAB_EQUITY_SIDE_CHOICES, 0, 2)

        self._combo_row(
            stock_frame,
            "Order Type",
            self.stock_order_type,
            SCHWAB_EQUITY_ORDER_TYPE_CHOICES,
            1,
            0,
        )
        self._combo_row(stock_frame, "TIF", self.stock_tif, SCHWAB_EQUITY_TIME_IN_FORCE_CHOICES, 1, 2)

        self._combo_row(
            stock_frame,
            "Position Effect",
            self.stock_position_effect,
            ("AUTO", "OPENING", "CLOSING"),
            2,
            0,
        )
        self._entry_row(stock_frame, "Quantity", self.stock_quantity, 2, 2)

        self._entry_row(stock_frame, "Entry / Limit", self.stock_entry_limit, 3, 0)
        self._entry_row(stock_frame, "Stop Price", self.stock_stop_price, 3, 2)

        ttk.Button(stock_frame, text="Use Mid", command=self._use_stock_mid).grid(
            row=4,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=8,
            pady=8,
        )

        ttk.Button(stock_frame, text="Submit Stock / ETF Order", command=self._submit_stock_order).grid(
            row=4,
            column=2,
            columnspan=2,
            sticky="ew",
            padx=8,
            pady=8,
        )

        self._combo_row(option_frame, "Strategy", self.option_strategy, SCHWAB_OPTION_STRATEGY_CHOICES, 0, 0)
        self._entry_row(option_frame, "Contracts", self.option_contracts, 0, 2)

        self._entry_row(option_frame, "Expiration", self.option_expiration, 1, 0)
        self._entry_row(option_frame, "Strike", self.option_strike, 1, 2)

        self._combo_row(option_frame, "Call / Put", self.option_call_put, ("CALL", "PUT"), 2, 0)
        self._entry_row(option_frame, "Bid", self.option_bid, 2, 2)

        self._entry_row(option_frame, "Ask", self.option_ask, 3, 0)
        self._entry_row(option_frame, "Mark", self.option_mark, 3, 2)

        self._entry_row(option_frame, "Limit / Debit", self.option_limit_debit, 4, 0)

        ttk.Button(option_frame, text="Use Mid", command=self._use_option_mid).grid(
            row=4,
            column=2,
            sticky="ew",
            padx=8,
            pady=8,
        )

        self._entry_row(option_frame, "Short Strike", self.option_short_strike, 5, 2)
        self._entry_row(option_frame, "Credit", self.option_credit, 6, 0)
        self._entry_row(option_frame, "Target Price", self.option_target_price, 6, 2)

        ttk.Button(option_frame, text="Submit Option Order", command=self._submit_option_order).grid(
            row=7,
            column=0,
            columnspan=4,
            sticky="ew",
            padx=8,
            pady=8,
        )

        cancel_frame = ttk.LabelFrame(parent, text="Cancel Order")
        parent.add(cancel_frame, weight=1)

        self._entry_row(cancel_frame, "Cancel Order ID", self.order_id, 0, 0)

        ttk.Button(cancel_frame, text="Cancel Order", command=self._cancel_order).grid(
            row=1,
            column=0,
            columnspan=4,
            sticky="ew",
            padx=8,
            pady=8,
        )

    def _build_orders_panel(self, parent: ttk.Frame) -> None:
        orders_frame = ttk.LabelFrame(parent, text="Orders")
        parent.add(orders_frame, weight=2)

        button_row = ttk.Frame(orders_frame)
        button_row.pack(fill=tk.X, padx=8, pady=8)

        ttk.Button(button_row, text="Open Orders", command=self._load_open_orders).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(button_row, text="Recent Orders", command=self._load_recent_orders).pack(side=tk.LEFT)

        tables = ttk.PanedWindow(orders_frame, orient=tk.VERTICAL)
        tables.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        open_frame = ttk.LabelFrame(tables, text="Open Orders")
        recent_frame = ttk.LabelFrame(tables, text="Recent Orders")

        tables.add(open_frame, weight=1)
        tables.add(recent_frame, weight=1)

        self.open_orders_table = self._orders_table(open_frame)
        self.recent_orders_table = self._orders_table(recent_frame)

    def _build_option_chain_panel(self, parent: ttk.Frame) -> None:
        chain_frame = ttk.LabelFrame(parent, text="Options Chain")
        parent.add(chain_frame, weight=2)

        input_row = ttk.Frame(chain_frame)
        input_row.pack(fill=tk.X, padx=8, pady=8)

        ttk.Label(input_row, text="Symbol").pack(side=tk.LEFT)
        ttk.Entry(input_row, textvariable=self.chain_symbol, width=12).pack(side=tk.LEFT, padx=(6, 12))

        ttk.Label(input_row, text="Strikes").pack(side=tk.LEFT)
        ttk.Entry(input_row, textvariable=self.chain_strikes, width=8).pack(side=tk.LEFT, padx=(6, 12))

        ttk.Button(input_row, text="Load Options Chain", command=self._load_option_chain).pack(side=tk.LEFT)

        self.option_chain_table = ttk.Treeview(
            chain_frame,
            columns=("symbol", "expiration", "strike", "side", "bid", "ask", "mark"),
            show="headings",
            height=8,
        )
        self._setup_column(self.option_chain_table, "symbol", "Symbol", 220)
        self._setup_column(self.option_chain_table, "expiration", "Expiration", 100)
        self._setup_column(self.option_chain_table, "strike", "Strike", 90, anchor=tk.E)
        self._setup_column(self.option_chain_table, "side", "Side", 70)
        self._setup_column(self.option_chain_table, "bid", "Bid", 90, anchor=tk.E)
        self._setup_column(self.option_chain_table, "ask", "Ask", 90, anchor=tk.E)
        self._setup_column(self.option_chain_table, "mark", "Mark", 90, anchor=tk.E)
        self.option_chain_table.tag_configure("option_itm", foreground=SUCCESS)
        self.option_chain_table.tag_configure("option_otm", foreground=DANGER)
        self.option_chain_table.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.option_chain_table.bind("<<TreeviewSelect>>", self._use_selected_option)

    def _orders_table(self, parent: ttk.Frame) -> ttk.Treeview:
        table = ttk.Treeview(
            parent,
            columns=("order_id", "status", "entered", "symbol", "side", "quantity", "price"),
            show="headings",
            height=6,
        )
        self._setup_column(table, "order_id", "Order ID", 120)
        self._setup_column(table, "status", "Status", 100)
        self._setup_column(table, "entered", "Entered", 160)
        self._setup_column(table, "symbol", "Symbol", 120)
        self._setup_column(table, "side", "Side", 120)
        self._setup_column(table, "quantity", "Qty", 80, anchor=tk.E)
        self._setup_column(table, "price", "Price", 100, anchor=tk.E)
        table.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        return table

    def _entry_row(
        self,
        parent: ttk.LabelFrame,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=8, pady=6)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=column + 1, sticky="ew", padx=8, pady=6)
        parent.columnconfigure(column + 1, weight=1)

    def _combo_row(
        self,
        parent: ttk.LabelFrame,
        label: str,
        variable: tk.StringVar,
        values: tuple[str, ...],
        row: int,
        column: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=8, pady=6)
        ttk.Combobox(parent, textvariable=variable, values=values, state="readonly").grid(
            row=row,
            column=column + 1,
            sticky="ew",
            padx=8,
            pady=6,
        )
        parent.columnconfigure(column + 1, weight=1)

    def _load_open_orders(self) -> None:
        try:
            orders = SchwabSession().get_open_orders()
            self._show_orders(self.open_orders_table, orders)
        except Exception as exc:
            messagebox.showerror("Open orders failed", f"{type(exc).__name__}: {exc}")

    def _load_recent_orders(self) -> None:
        try:
            orders = SchwabSession().get_recent_orders()
            self._show_orders(self.recent_orders_table, orders)
        except Exception as exc:
            messagebox.showerror("Recent orders failed", f"{type(exc).__name__}: {exc}")

    def _load_option_chain(self) -> None:
        try:
            strikes = int(self.chain_strikes.get().strip())
            chain = SchwabSession().get_option_chain(self.chain_symbol.get(), strikes)
            self._show_option_chain(chain)
        except Exception as exc:
            messagebox.showerror("Option chain failed", f"{type(exc).__name__}: {exc}")

    def _cancel_order(self) -> None:
        try:
            result = SchwabSession().cancel_order(self.order_id.get())
            messagebox.showinfo("Cancel order", f"Cancel response: {result}")
        except Exception as exc:
            messagebox.showerror("Cancel order failed", f"{type(exc).__name__}: {exc}")

    def _submit_stock_order(self) -> None:
        try:
            payload = self._stock_order_payload()

            if not messagebox.askyesno(
                "Confirm Stock / ETF Order",
                order_confirmation_message(payload),
            ):
                return

            location = SchwabSession().submit_order(payload)
            self._load_open_orders()
            messagebox.showinfo(
                "Stock / ETF order submitted",
                order_submitted_message(payload, location),
            )
        except Exception as exc:
            messagebox.showerror("Stock / ETF order failed", f"{type(exc).__name__}: {exc}")

    def _submit_option_order(self) -> None:
        try:
            payload = self._option_order_payload()

            if not messagebox.askyesno(
                "Confirm Option Order",
                order_confirmation_message(payload),
            ):
                return

            location = SchwabSession().submit_order(payload)
            self._load_open_orders()
            messagebox.showinfo(
                "Option order submitted",
                order_submitted_message(payload, location),
            )
        except Exception as exc:
            messagebox.showerror("Option order failed", f"{type(exc).__name__}: {exc}")

    def _use_stock_mid(self) -> None:
        symbol = self.stock_symbol.get().strip().upper()
        if not symbol:
            messagebox.showwarning("Use mid", "Stock / ETF symbol is required.")
            return

        try:
            mid = SchwabSession().get_equity_mid(symbol)
        except Exception as exc:
            messagebox.showerror("Stock quote failed", f"{type(exc).__name__}: {exc}")
            return

        self.stock_symbol.set(symbol)
        self.stock_order_type.set("LIMIT")
        self.stock_entry_limit.set(f"{mid:.2f}")

    def _use_option_mid(self) -> None:
        mark = self.option_mark.get().strip()
        if mark:
            self.option_limit_debit.set(mark)
            return

        bid = _to_float(self.option_bid.get())
        ask = _to_float(self.option_ask.get())

        if bid is None or ask is None:
            return

        self.option_limit_debit.set(f"{((bid + ask) / 2):.2f}")

    def _use_selected_holding(self, _event: object) -> None:
        if self.holdings_table is None:
            return

        selected = self.holdings_table.selection()
        if not selected:
            return

        values = self.holdings_table.item(selected[0], "values")
        if len(values) < 3:
            return

        bucket = str(values[1]).strip().upper()
        symbol = str(values[2]).strip().upper()

        if not symbol:
            return

        if bucket == "EQUITY":
            self.stock_symbol.set(symbol)

    def _stock_order_payload(self) -> dict[str, object]:
        symbol = self.stock_symbol.get().strip().upper()
        quantity = _positive_int(self.stock_quantity.get(), "Quantity")
        order_type = self.stock_order_type.get().strip().upper()
        side = self.stock_side.get().strip().upper()
        session, duration = schwab_equity_session_duration(self.stock_tif.get())

        if not symbol:
            raise ValueError("Stock / ETF symbol is required.")

        if schwab_equity_tif_requires_limit_order(self.stock_tif.get()) and order_type == "MARKET":
            raise ValueError("Extended-hours Schwab equity orders require a limit price.")

        payload: dict[str, object] = {
            "orderType": order_type,
            "session": session,
            "duration": duration,
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": side,
                    "quantity": quantity,
                    "instrument": {
                        "symbol": symbol,
                        "assetType": "EQUITY",
                    },
                }
            ],
        }

        if order_type in {"LIMIT", "STOP_LIMIT"}:
            payload["price"] = _required_positive_price(self.stock_entry_limit.get(), "Entry / Limit")

        if order_type in {"STOP", "STOP_LIMIT"}:
            payload["stopPrice"] = _required_positive_price(self.stock_stop_price.get(), "Stop Price")

        return payload

    def _option_order_payload(self) -> dict[str, object]:
        symbol = self.option_symbol.get().strip().upper()
        quantity = _positive_int(self.option_contracts.get(), "Contracts")
        order_type = self.option_order_type.get().strip().upper()
        side = self.option_side.get().strip().upper()
        session, duration = schwab_equity_session_duration(self.option_tif.get())

        strategy = self.option_strategy.get().strip().upper()
        if not schwab_option_strategy_is_supported(strategy):
            raise ValueError(
                f"{strategy} is available in the ticket, but live submit is not wired yet. "
                "Use SINGLE for now."
            )

        if not symbol:
            raise ValueError("Option symbol is required.")

        payload: dict[str, object] = {
            "orderType": order_type,
            "session": session,
            "duration": duration,
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": side,
                    "quantity": quantity,
                    "instrument": {
                        "symbol": symbol,
                        "assetType": "OPTION",
                    },
                }
            ],
        }

        price = self.option_limit_debit.get().strip() or self.option_credit.get().strip()

        if order_type in {"LIMIT", "STOP_LIMIT"}:
            payload["price"] = _required_positive_price(price, "Limit / Debit or Credit")

        return payload

    def _show_orders(self, table: ttk.Treeview | None, orders: object) -> None:
        if table is None:
            return

        self._clear_table(table)

        if not isinstance(orders, list):
            return

        for order in orders:
            if not isinstance(order, dict):
                continue

            order_id = str(order.get("orderId") or "")
            status = str(order.get("status") or "")
            entered = str(order.get("enteredTime") or "")
            leg = _first_order_leg(order)
            symbol = str(leg.get("symbol") or "")
            side = str(leg.get("instruction") or "")
            quantity = str(leg.get("quantity") or "")
            price = str(order.get("price") or "")

            table.insert(
                "",
                tk.END,
                values=(order_id, status, entered, symbol, side, quantity, price),
            )

    def _show_option_chain(self, chain: object) -> None:
        if self.option_chain_table is None:
            return

        self._clear_table(self.option_chain_table)

        for row in _option_chain_rows(chain):
            self.option_chain_table.insert(
                "",
                tk.END,
                values=(
                    row["symbol"],
                    row["expiration"],
                    row["strike"],
                    row["side"],
                    row["bid"],
                    row["ask"],
                    row["mark"],
                ),
                tags=_option_moneyness_tag(row),
            )

    def _use_selected_option(self, _event: object) -> None:
        if self.option_chain_table is None:
            return

        selected = self.option_chain_table.selection()
        if not selected:
            return

        values = self.option_chain_table.item(selected[0], "values")
        if not values:
            return

        self.option_symbol.set(str(values[0]))
        self.option_expiration.set(str(values[1]))
        self.option_strike.set(str(values[2]))
        self.option_call_put.set(str(values[3]))
        self.option_bid.set(str(values[4]))
        self.option_ask.set(str(values[5]))
        self.option_mark.set(str(values[6]))


class HyperliquidDucketsTab(DucketsTab):
    def __init__(self, root: tk.Tk, parent: ttk.Frame) -> None:
        self.spot_market = tk.StringVar()
        self.spot_side = tk.StringVar(value="buy")
        self.spot_order_type = tk.StringVar(value="limit")
        self.spot_quantity = tk.StringVar()
        self.spot_size_unit = tk.StringVar(value="USDC")
        self.spot_entry_limit = tk.StringVar()
        self.spot_stop_price = tk.StringVar()
        self.spot_tif = tk.StringVar(value="Gtc")
        self.spot_cancel_order_id = tk.StringVar()
        self.spot_size_status = tk.StringVar(value="Sync Hyperliquid, then choose a size %")

        self.perp_coin = tk.StringVar()
        self.perp_direction = tk.StringVar(value="buy")
        self.perp_order_type = tk.StringVar(value="limit")
        self.perp_size = tk.StringVar()
        self.perp_entry_limit = tk.StringVar()
        self.perp_tp_price = tk.StringVar()
        self.perp_sl_price = tk.StringVar()
        self.perp_tif = tk.StringVar(value="Gtc")
        self.perp_reduce_only = tk.BooleanVar(value=False)
        self.perp_leverage = tk.StringVar(value="1")
        self.perp_margin_mode = tk.StringVar(value="Cross")
        self.perp_attach_tpsl = tk.BooleanVar(value=False)
        self.perp_fee_rate = tk.StringVar(value="0.045")
        self.perp_cancel_order_id = tk.StringVar()
        self.hyperliquid_open_order_by_lookup_key: dict[str, dict[str, object]] = {}
        self.selected_hyperliquid_order_key = ""

        self.latest_hyperliquid_bucket: DucketBucketSnapshot | None = None
        self.hyperliquid_open_orders_table: ttk.Treeview | None = None

        super().__init__(
            root=root,
            parent=parent,
            title="Hyperliquid Duckets",
            sync_button_text="Sync Hyperliquid",
            sync_snapshots=sync_hyperliquid_portfolios,
        )

    def _build(self, parent: ttk.Frame, title: str, sync_button_text: str) -> None:
        root_panes = ttk.PanedWindow(parent, orient=tk.VERTICAL)
        root_panes.pack(fill=tk.BOTH, expand=True)

        balances_frame = ttk.Frame(root_panes)
        actions_frame = ttk.LabelFrame(root_panes, text="Hyperliquid Order Actions")

        root_panes.add(balances_frame, weight=3)
        root_panes.add(actions_frame, weight=2)

        super()._build(balances_frame, title, sync_button_text)

        if self.holdings_table is not None:
            self.holdings_table.bind("<<TreeviewSelect>>", self._use_selected_hyperliquid_holding)

        actions_panes = ttk.PanedWindow(actions_frame, orient=tk.HORIZONTAL)
        actions_panes.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        spot_frame = ttk.Frame(actions_panes)
        perp_frame = ttk.Frame(actions_panes)
        orders_frame = ttk.Frame(actions_panes)

        actions_panes.add(spot_frame, weight=2)
        actions_panes.add(perp_frame, weight=2)
        actions_panes.add(orders_frame, weight=3)

        self._build_spot_ticket(spot_frame)
        self._build_perp_ticket(perp_frame)
        self._build_hyperliquid_orders_panel(orders_frame)

    def _show_bucket(self, bucket: DucketBucketSnapshot) -> None:
        self.latest_hyperliquid_bucket = bucket
        super()._show_bucket(bucket)

    def _build_spot_ticket(self, parent: ttk.Frame) -> None:
        ticket = ttk.LabelFrame(parent, text="Hyperliquid Spot Ticket")
        ticket.pack(fill=tk.BOTH, expand=True)

        self._hl_entry_row(ticket, "Market", self.spot_market, 0, 0)
        self._hl_combo_row(ticket, "Side", self.spot_side, HYPERLIQUID_SIDE_CHOICES, 1, 0)
        self._hl_combo_row(ticket, "Order type", self.spot_order_type, HYPERLIQUID_ORDER_TYPE_CHOICES, 1, 2)

        self._hl_entry_row(ticket, "Quantity", self.spot_quantity, 2, 0)
        self._hl_combo_row(ticket, "Unit", self.spot_size_unit, HYPERLIQUID_SPOT_SIZE_UNITS, 2, 2)

        self._hl_entry_row(ticket, "Entry / Limit", self.spot_entry_limit, 3, 0)
        ttk.Button(ticket, text="Use Mid", command=self._use_spot_mid).grid(
            row=3, column=2, columnspan=2, sticky="ew", padx=8, pady=6
        )

        self._hl_size_button_row(ticket, "Alex size %", "alex", 4)
        self._hl_size_button_row(ticket, "Jeremy size %", "jeremy", 5)

        ttk.Label(ticket, textvariable=self.spot_size_status).grid(
            row=6, column=0, columnspan=4, sticky="w", padx=8, pady=(0, 6)
        )

        self._hl_entry_row(ticket, "Stop price", self.spot_stop_price, 7, 0)
        self._hl_combo_row(ticket, "HL TIF", self.spot_tif, HYPERLIQUID_TIF_CHOICES, 8, 0)
        self._hl_entry_row(ticket, "Cancel order ID", self.spot_cancel_order_id, 9, 0)

        actions = ttk.LabelFrame(ticket, text="Spot Actions")
        actions.grid(row=10, column=0, columnspan=4, sticky="ew", padx=8, pady=8)
        actions.columnconfigure((0, 1), weight=1)

        self._hl_button(actions, "Submit Order Alex", lambda: self._submit_spot_order("alex"), 0, 0)
        self._hl_button(actions, "Submit Order Jeremy", lambda: self._submit_spot_order("jeremy"), 0, 1)
        self._hl_button(actions, "Cancel Order Alex", lambda: self._cancel_spot_order("alex"), 1, 0)
        self._hl_button(actions, "Cancel Order Jeremy", lambda: self._cancel_spot_order("jeremy"), 1, 1)
        self._hl_button(actions, "Edit Selected Order", self._edit_selected_hyperliquid_open_order, 2, 0, columnspan=2)
        self._hl_button(actions, "Refresh Balances / Open Orders", self._refresh_hyperliquid, 3, 0, columnspan=2)

    def _build_perp_ticket(self, parent: ttk.Frame) -> None:
        ticket = ttk.LabelFrame(parent, text="Hyperliquid Perp Ticket")
        ticket.pack(fill=tk.BOTH, expand=True)

        self._hl_entry_row(ticket, "Coin", self.perp_coin, 0, 0)
        self._hl_combo_row(ticket, "Direction", self.perp_direction, HYPERLIQUID_SIDE_CHOICES, 1, 0)
        self._hl_combo_row(ticket, "Order type", self.perp_order_type, HYPERLIQUID_ORDER_TYPE_CHOICES, 1, 2)

        self._hl_entry_row(ticket, "Size", self.perp_size, 2, 0)
        self._hl_entry_row(ticket, "Entry / Limit", self.perp_entry_limit, 2, 2)

        self._hl_entry_row(ticket, "TP price", self.perp_tp_price, 3, 0)
        self._hl_entry_row(ticket, "SL price", self.perp_sl_price, 3, 2)

        self._hl_combo_row(ticket, "HL TIF", self.perp_tif, HYPERLIQUID_TIF_CHOICES, 4, 0)
        self._hl_check_row(ticket, "Reduce-only", self.perp_reduce_only, 4, 2)

        ttk.Button(ticket, text="Use Mid", command=self._use_perp_mid).grid(
            row=5, column=2, columnspan=2, sticky="ew", padx=8, pady=6
        )

        self._hl_entry_row(ticket, "Leverage x", self.perp_leverage, 6, 0)
        self._hl_combo_row(ticket, "Margin mode", self.perp_margin_mode, HYPERLIQUID_MARGIN_MODE_CHOICES, 6, 2)

        self._hl_check_row(ticket, "Attach TP/SL", self.perp_attach_tpsl, 7, 0)
        self._hl_entry_row(ticket, "Fee % / side", self.perp_fee_rate, 7, 2)

        self._hl_entry_row(ticket, "Cancel order ID", self.perp_cancel_order_id, 8, 0)

        actions = ttk.LabelFrame(ticket, text="Perp Actions")
        actions.grid(row=9, column=0, columnspan=4, sticky="ew", padx=8, pady=8)
        actions.columnconfigure((0, 1), weight=1)

        self._hl_button(actions, "Submit Order Alex", lambda: self._submit_perp_order("alex"), 0, 0)
        self._hl_button(actions, "Submit Order Jeremy", lambda: self._submit_perp_order("jeremy"), 0, 1)
        self._hl_button(actions, "Cancel Order Alex", lambda: self._cancel_perp_order("alex"), 1, 0)
        self._hl_button(actions, "Cancel Order Jeremy", lambda: self._cancel_perp_order("jeremy"), 1, 1)
        self._hl_button(actions, "Edit Selected Position", self._edit_selected_perp_position, 2, 0)
        self._hl_button(actions, "TP/SL Orders", self._open_tpsl_orders, 2, 1)
        self._hl_button(actions, "Open Orders", self._load_hyperliquid_open_orders, 3, 0)
        self._hl_button(actions, "Refresh", self._refresh_hyperliquid, 3, 1)

    def _build_hyperliquid_orders_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text="Hyperliquid Open Orders")
        panel.pack(fill=tk.BOTH, expand=True)

        self.hyperliquid_open_orders_table = ttk.Treeview(
            panel,
            columns=("account", "kind", "oid", "coin", "side", "size", "price", "type", "reduce_only"),
            show="headings",
            height=10,
        )
        self._setup_column(self.hyperliquid_open_orders_table, "account", "Account", 90)
        self._setup_column(self.hyperliquid_open_orders_table, "kind", "Kind", 70)
        self._setup_column(self.hyperliquid_open_orders_table, "oid", "Order ID", 110)
        self._setup_column(self.hyperliquid_open_orders_table, "coin", "Coin", 90)
        self._setup_column(self.hyperliquid_open_orders_table, "side", "Side", 80)
        self._setup_column(self.hyperliquid_open_orders_table, "size", "Size", 100, anchor=tk.E)
        self._setup_column(self.hyperliquid_open_orders_table, "price", "Price", 100, anchor=tk.E)
        self._setup_column(self.hyperliquid_open_orders_table, "type", "Type", 110)
        self._setup_column(self.hyperliquid_open_orders_table, "reduce_only", "Reduce", 80)
        self.hyperliquid_open_orders_table.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)
        self.hyperliquid_open_orders_table.bind("<<TreeviewSelect>>", self._use_selected_hyperliquid_order)
        self.hyperliquid_open_orders_table.bind("<Double-1>", self._edit_selected_hyperliquid_open_order)

    def _hyperliquid_account_snapshot(self, account_key: str) -> PortfolioSnapshot:
        bucket = self.latest_hyperliquid_bucket

        if bucket is None:
            raise ValueError("Sync Hyperliquid first.")

        normalized_key = account_key.strip().lower()

        for snapshot in bucket.snapshots:
            if snapshot.account_label.strip().lower() == normalized_key:
                return snapshot

        labels = ", ".join(snapshot.account_label for snapshot in bucket.snapshots) or "--"
        raise ValueError(f"No synced Hyperliquid account named {account_key}. Synced accounts: {labels}")

    def _hl_entry_row(
        self,
        parent: ttk.LabelFrame,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=8, pady=6)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=column + 1, sticky="ew", padx=8, pady=6)
        parent.columnconfigure(column + 1, weight=1)

    def _hl_combo_row(
        self,
        parent: ttk.LabelFrame,
        label: str,
        variable: tk.StringVar,
        values: tuple[str, ...],
        row: int,
        column: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, sticky="w", padx=8, pady=6)
        ttk.Combobox(parent, textvariable=variable, values=values, state="readonly").grid(
            row=row,
            column=column + 1,
            sticky="ew",
            padx=8,
            pady=6,
        )
        parent.columnconfigure(column + 1, weight=1)

    def _hl_check_row(
        self,
        parent: ttk.LabelFrame,
        label: str,
        variable: tk.BooleanVar,
        row: int,
        column: int,
    ) -> None:
        ttk.Checkbutton(parent, text=label, variable=variable).grid(
            row=row,
            column=column,
            columnspan=2,
            sticky="w",
            padx=8,
            pady=6,
        )

    def _hl_button(
        self,
        parent: ttk.LabelFrame,
        text: str,
        command: Callable[[], None],
        row: int,
        column: int,
        columnspan: int = 1,
    ) -> None:
        ttk.Button(parent, text=text, command=command).grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky="ew",
            padx=6,
            pady=4,
        )

    def _hl_size_button_row(
        self,
        parent: ttk.LabelFrame,
        label: str,
        account_key: str,
        row: int,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=6)

        button_row = ttk.Frame(parent)
        button_row.grid(row=row, column=1, columnspan=3, sticky="ew", padx=8, pady=6)

        for button_label, percent in (("25%", 25), ("50%", 50), ("75%", 75), ("Max", 100)):
            ttk.Button(
                button_row,
                text=button_label,
                command=lambda pct=percent, acct=account_key: self._apply_spot_size_percent(acct, pct),
            ).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

    def _use_spot_mid(self) -> None:
        self._use_hyperliquid_mid(self.spot_market.get(), self.spot_entry_limit)

    def _use_perp_mid(self) -> None:
        self._use_hyperliquid_mid(self.perp_coin.get(), self.perp_entry_limit)

    def _use_hyperliquid_mid(self, raw_market: str, target_var: tk.StringVar) -> None:
        market = raw_market.strip().upper()
        if not market:
            messagebox.showwarning("Use Mid", "Enter a Hyperliquid market / coin first.")
            return

        try:
            all_mids = HyperliquidInfoClient().post_info({"type": "allMids"})
            if not isinstance(all_mids, dict):
                raise RuntimeError("Hyperliquid allMids returned an unexpected response.")

            candidates = _hyperliquid_mid_candidates(market)
            price = next((_to_float(all_mids.get(candidate)) for candidate in candidates if _to_float(all_mids.get(candidate)) is not None), None)

            if price is None:
                raise RuntimeError(f"No mid found for {market}. Tried: {', '.join(candidates)}")

            target_var.set(_format_hyperliquid_price(price))
        except Exception as exc:
            messagebox.showerror("Hyperliquid mid failed", f"{type(exc).__name__}: {exc}")

    def _apply_spot_size_percent(self, account_key: str, percent: int) -> None:
        try:
            snapshot = self._hyperliquid_account_snapshot(account_key)

            market = self.spot_market.get().strip().upper()
            if not market:
                raise ValueError("Enter a spot market first.")

            side = self.spot_side.get().strip().lower()
            unit = self.spot_size_unit.get().strip().upper()
            base = _hyperliquid_display_symbol(market)
            price = _to_float(self.spot_entry_limit.get())

            max_base_size, basis = _max_spot_base_size(snapshot, base, side, price)
            selected_base_size = max_base_size * (float(percent) / 100.0)

            if selected_base_size <= 0:
                raise ValueError(f"No available {basis} for {snapshot.account_label}.")

            if unit == "USDC":
                if price is None or price <= 0:
                    raise ValueError("Enter a positive Entry / Limit price before sizing in USDC.")

                selected_quote_size = selected_base_size * price
                self.spot_quantity.set(_format_hyperliquid_size(selected_quote_size))
                displayed = f"{_format_hyperliquid_size(selected_quote_size)} USDC"
            else:
                self.spot_quantity.set(_format_hyperliquid_size(selected_base_size))
                displayed = f"{_format_hyperliquid_size(selected_base_size)} {base}"

            self.spot_size_status.set(
                f"{snapshot.account_label} {percent}% of {basis} = {displayed}"
            )
        except Exception as exc:
            self.spot_size_status.set(f"Size helper: {type(exc).__name__}: {exc}")

    def _refresh_hyperliquid(self) -> None:
        self._sync()
        self._load_hyperliquid_open_orders()

    def _load_hyperliquid_open_orders(self) -> None:
        if self.hyperliquid_open_orders_table is None:
            return

        self._clear_table(self.hyperliquid_open_orders_table)
        self.hyperliquid_open_order_by_lookup_key = {}

        errors: list[str] = []

        try:
            spot_meta_and_asset_ctxs = HyperliquidInfoClient().post_info({"type": "spotMetaAndAssetCtxs"})
        except Exception:
            spot_meta_and_asset_ctxs = None

        for account_key in ("alex", "jeremy"):
            try:
                orders = HyperliquidExecutionAdapter(account_key).open_orders()
            except Exception as exc:
                errors.append(f"{account_key}: {type(exc).__name__}: {exc}")
                continue

            for order in orders:
                lookup_key = _hyperliquid_open_order_lookup_key(order)
                self.hyperliquid_open_order_by_lookup_key[lookup_key] = order
                raw_coin = str(order.get("coin") or "")
                display_coin = _hyperliquid_display_open_order_coin(raw_coin, spot_meta_and_asset_ctxs)

                self.hyperliquid_open_orders_table.insert(
                    "",
                    tk.END,
                    iid=lookup_key,
                    values=(
                        order.get("accountLabel") or account_key.title(),
                        _hyperliquid_order_kind(order),
                        order.get("oid") or "",
                        display_coin,
                        _hyperliquid_order_side(order),
                        order.get("sz") or "",
                        order.get("limitPx") or order.get("price") or "",
                        order.get("orderType") or order.get("type") or "Limit",
                        "yes" if _to_bool(order.get("reduceOnly")) else "no",
                    ),
                )

        if errors:
            messagebox.showwarning("Hyperliquid open orders partially loaded", "\n".join(errors))

    def _spot_order_ticket(self) -> HyperliquidOrderTicket:
        order_type = self.spot_order_type.get().strip().lower()
        if order_type != "limit":
            raise ValueError("Live Hyperliquid spot submit is wired for limit orders first.")

        market = normalize_hyperliquid_spot_market(self.spot_market.get())
        is_buy = self.spot_side.get().strip().lower() == "buy"
        limit_price = _required_float(self.spot_entry_limit.get(), "Entry / Limit")
        raw_quantity = _required_float(self.spot_quantity.get(), "Quantity")
        unit = self.spot_size_unit.get().strip().upper()

        size = raw_quantity
        if unit == "USDC":
            size = raw_quantity / limit_price

        return HyperliquidOrderTicket(
            coin=market,
            is_buy=is_buy,
            size=size,
            limit_price=limit_price,
            tif=self.spot_tif.get().strip() or "Gtc",
            reduce_only=False,
        )

    def _perp_order_ticket(self) -> HyperliquidOrderTicket:
        order_type = self.perp_order_type.get().strip().lower()
        if order_type != "limit":
            raise ValueError("Live Hyperliquid perp submit is wired for limit orders first.")

        return HyperliquidOrderTicket(
            coin=normalize_hyperliquid_coin(self.perp_coin.get()),
            is_buy=self.perp_direction.get().strip().lower() == "buy",
            size=_required_float(self.perp_size.get(), "Size"),
            limit_price=_required_float(self.perp_entry_limit.get(), "Entry / Limit"),
            tif=self.perp_tif.get().strip() or "Gtc",
            reduce_only=bool(self.perp_reduce_only.get()),
        )

    def _submit_spot_order(self, account_key: str) -> None:
        try:
            ticket = self._spot_order_ticket()

            if not messagebox.askyesno(
                "Confirm Hyperliquid Spot Order",
                _hyperliquid_order_confirmation_message(account_key, ticket),
            ):
                return

            result = HyperliquidExecutionAdapter(account_key).submit(ticket)
            self._load_hyperliquid_open_orders()
            messagebox.showinfo(
                "Hyperliquid spot order submitted",
                _hyperliquid_order_submitted_message(account_key, ticket, result),
            )
        except Exception as exc:
            messagebox.showerror("Hyperliquid spot order failed", f"{type(exc).__name__}: {exc}")

    def _submit_perp_order(self, account_key: str) -> None:
        try:
            ticket = self._perp_order_ticket()

            if not messagebox.askyesno(
                "Confirm Hyperliquid Perp Order",
                _hyperliquid_order_confirmation_message(account_key, ticket),
            ):
                return

            result = HyperliquidExecutionAdapter(account_key).submit(ticket)
            self._load_hyperliquid_open_orders()
            messagebox.showinfo(
                "Hyperliquid perp order submitted",
                _hyperliquid_order_submitted_message(account_key, ticket, result),
            )
        except Exception as exc:
            messagebox.showerror("Hyperliquid perp order failed", f"{type(exc).__name__}: {exc}")

    def _cancel_spot_order(self, account_key: str) -> None:
        try:
            coin = normalize_hyperliquid_spot_market(self.spot_market.get())
            order_id = _positive_int(self.spot_cancel_order_id.get(), "Cancel order ID")

            if not messagebox.askyesno(
                "Confirm Hyperliquid Spot Cancel",
                f"Cancel {account_key.upper()} spot order?\n\nCoin: {coin}\nOrder ID: {order_id}",
            ):
                return

            result = HyperliquidExecutionAdapter(account_key).cancel(coin, order_id)
            self._load_hyperliquid_open_orders()
            messagebox.showinfo("Hyperliquid spot cancel submitted", f"Response:\n{result}")
        except Exception as exc:
            messagebox.showerror("Hyperliquid spot cancel failed", f"{type(exc).__name__}: {exc}")

    def _cancel_perp_order(self, account_key: str) -> None:
        try:
            coin = normalize_hyperliquid_coin(self.perp_coin.get())
            order_id = _positive_int(self.perp_cancel_order_id.get(), "Cancel order ID")

            if not messagebox.askyesno(
                "Confirm Hyperliquid Perp Cancel",
                f"Cancel {account_key.upper()} perp order?\n\nCoin: {coin}\nOrder ID: {order_id}",
            ):
                return

            result = HyperliquidExecutionAdapter(account_key).cancel(coin, order_id)
            self._load_hyperliquid_open_orders()
            messagebox.showinfo("Hyperliquid perp cancel submitted", f"Response:\n{result}")
        except Exception as exc:
            messagebox.showerror("Hyperliquid perp cancel failed", f"{type(exc).__name__}: {exc}")

    def _edit_selected_perp_position(self) -> None:
        self._edit_selected_hyperliquid_open_order()

    def _open_tpsl_orders(self) -> None:
        self._hyperliquid_action_not_wired("open TP/SL orders", "selected account")

    def _edit_selected_hyperliquid_open_order(self, _event: object | None = None) -> None:
        self._use_selected_hyperliquid_order(_event)

        order = self._selected_hyperliquid_order()
        if order is None:
            messagebox.showinfo("Edit Hyperliquid order", "Select an open order first.")
            return

        account_key = str(order.get("accountKey") or "").strip().lower()
        account_label = str(order.get("accountLabel") or account_key.title())
        raw_coin = str(order.get("coin") or "")
        order_id = _positive_int(order.get("oid"), "Order ID")

        dialog = tk.Toplevel(self.root)
        dialog.title("Edit Hyperliquid Open Order")
        dialog.transient(self.root)
        dialog.resizable(False, False)

        body = ttk.Frame(dialog, padding=14)
        body.pack(fill=tk.BOTH, expand=True)

        size_var = tk.StringVar(value=str(order.get("sz") or ""))
        price_var = tk.StringVar(value=str(order.get("limitPx") or order.get("price") or ""))
        side = _hyperliquid_order_side(order).lower()
        is_buy = side in {"b", "buy"}
        reduce_only = bool(_to_bool(order.get("reduceOnly")))

        ttk.Label(body, text=f"Account: {account_label}").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(body, text=f"Coin: {raw_coin}").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(body, text=f"Order ID: {order_id}").grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 12))

        ttk.Label(body, text="New size").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(body, textvariable=size_var, width=24).grid(row=3, column=1, sticky="ew", pady=6)

        ttk.Label(body, text="New price").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(body, textvariable=price_var, width=24).grid(row=4, column=1, sticky="ew", pady=6)

        def submit_edit() -> None:
            try:
                ticket = HyperliquidOrderTicket(
                    coin=raw_coin,
                    is_buy=is_buy,
                    size=_required_float(size_var.get(), "New size"),
                    limit_price=_required_float(price_var.get(), "New price"),
                    tif=str(order.get("tif") or order.get("timeInForce") or "Gtc"),
                    reduce_only=reduce_only,
                )

                if not messagebox.askyesno(
                    "Confirm Hyperliquid Edit",
                    _hyperliquid_order_confirmation_message(account_key, ticket),
                ):
                    return

                result = HyperliquidExecutionAdapter(account_key).modify_order(order_id, ticket)
                dialog.destroy()
                self._load_hyperliquid_open_orders()
                messagebox.showinfo("Hyperliquid order edited", f"Response:\n{result}")
            except Exception as exc:
                messagebox.showerror("Hyperliquid edit failed", f"{type(exc).__name__}: {exc}")

        ttk.Button(body, text="Submit Edit", command=submit_edit).grid(
            row=5,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(12, 0),
        )

    def _hyperliquid_action_not_wired(self, action: str, account_key: str) -> None:
        messagebox.showinfo(
            "Hyperliquid action not wired yet",
            (
                f"{action} for {account_key} is in the UI now, but live routing is not connected yet.\n\n"
                "Next step: port HyperliquidExecutionAdapter from portfolio-risk-cockpit and wire this button through "
                "a confirmation + live-order gate."
            ),
        )

    def _use_selected_hyperliquid_holding(self, _event: object) -> None:
        if self.holdings_table is None:
            return

        selected = self.holdings_table.selection()
        if not selected:
            return

        values = self.holdings_table.item(selected[0], "values")
        if len(values) < 3:
            return

        bucket = str(values[1]).strip().upper()
        symbol = str(values[2]).strip().upper()

        if not symbol:
            return

        if bucket == "SPOT":
            self.spot_market.set(_hyperliquid_display_symbol(symbol))
        elif bucket == "PERPS":
            self.perp_coin.set(_hyperliquid_display_symbol(symbol))

    def _use_selected_hyperliquid_order(self, _event: object) -> None:
        if self.hyperliquid_open_orders_table is None:
            return

        selected = self.hyperliquid_open_orders_table.selection()
        if not selected:
            return

        lookup_key = str(selected[0])
        values = self.hyperliquid_open_orders_table.item(lookup_key, "values")
        if len(values) < 4:
            return

        self.selected_hyperliquid_order_key = lookup_key

        kind = str(values[1])
        order_id = str(values[2])
        display_coin = str(values[3])

        self.spot_cancel_order_id.set(order_id)
        self.perp_cancel_order_id.set(order_id)

        if kind.upper() == "SPOT":
            self.spot_market.set(display_coin)
        else:
            self.perp_coin.set(display_coin)

    def _selected_hyperliquid_order(self) -> dict[str, object] | None:
        if not self.selected_hyperliquid_order_key:
            return None

        order = self.hyperliquid_open_order_by_lookup_key.get(self.selected_hyperliquid_order_key)
        return order if isinstance(order, dict) else None


def _hyperliquid_display_open_order_coin(raw_coin: str, spot_meta_and_asset_ctxs: object) -> str:
    coin = raw_coin.strip().upper()

    if not coin.startswith("@"):
        return coin

    market_index = _int_from_at_market(coin)
    if market_index is None:
        return coin

    market = _spot_market_label_from_meta(market_index, spot_meta_and_asset_ctxs)
    return market or coin


def _hyperliquid_order_kind(order: dict[str, object]) -> str:
    coin = str(order.get("coin") or "").strip()

    if coin.startswith("@") or "/" in coin:
        return "SPOT"

    return "PERP"


def _int_from_at_market(value: str) -> int | None:
    cleaned = value.strip()
    if not cleaned.startswith("@"):
        return None

    try:
        return int(cleaned[1:])
    except ValueError:
        return None


def _spot_market_label_from_meta(market_index: int, spot_meta_and_asset_ctxs: object) -> str:
    if not isinstance(spot_meta_and_asset_ctxs, list) or not spot_meta_and_asset_ctxs:
        return ""

    meta = spot_meta_and_asset_ctxs[0]
    if not isinstance(meta, dict):
        return ""

    universe = meta.get("universe")
    tokens = meta.get("tokens")

    if not isinstance(universe, list):
        return ""

    token_names_by_index = _spot_token_names_by_index(tokens)

    for index, asset in enumerate(universe):
        if not isinstance(asset, dict):
            continue

        asset_index = _to_int_or_none(asset.get("index"))

        if market_index not in {index, 10000 + index, asset_index, None if asset_index is None else 10000 + asset_index}:
            continue

        token_indices = asset.get("tokens")
        if isinstance(token_indices, list) and len(token_indices) >= 2:
            base = token_names_by_index.get(_to_int_or_none(token_indices[0]), "")
            quote = token_names_by_index.get(_to_int_or_none(token_indices[1]), "USDC")

            if base:
                return f"{base}/{quote or 'USDC'}"

        name = str(asset.get("name") or "").strip().upper()
        if name and not name.startswith("@"):
            return name

    return ""


def _spot_token_names_by_index(tokens: object) -> dict[int | None, str]:
    result: dict[int | None, str] = {}

    if not isinstance(tokens, list):
        return result

    for index, token in enumerate(tokens):
        if not isinstance(token, dict):
            continue

        token_index = _to_int_or_none(token.get("index"))
        name = str(token.get("name") or token.get("token") or token.get("coin") or "").strip().upper()

        if name:
            result[index] = name
            result[token_index] = name

    return result


def _to_int_or_none(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _hyperliquid_mid_candidates(market: str) -> tuple[str, ...]:
    clean = _hyperliquid_display_symbol(market)
    candidates = [
        market.strip().upper(),
        clean,
        f"{clean}/USDC",
        f"U{clean}/USDC",
        f"{clean}-PERP",
    ]

    if clean.startswith("U") and len(clean) > 1:
        candidates.append(f"{clean[1:]}/USDC")

    return tuple(_dedupe_strings([candidate for candidate in candidates if candidate]))


def _hyperliquid_display_symbol(symbol: str) -> str:
    clean = symbol.strip().upper()

    for suffix in ("-PERP-SHORT", "-PERP", "-SPOT"):
        if clean.endswith(suffix):
            clean = clean[: -len(suffix)]

    if "/" in clean:
        clean = clean.split("/", 1)[0]

    return clean


def _format_hyperliquid_price(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []

    for value in values:
        if value not in result:
            result.append(value)

    return result


def _max_spot_base_size(
    snapshot: PortfolioSnapshot,
    base: str,
    side: str,
    price: float | None,
) -> tuple[float, str]:
    normalized_base = _hyperliquid_display_symbol(base)

    if side == "sell":
        base_balance = _spot_base_balance(snapshot, normalized_base)
        return base_balance, f"{normalized_base} spot balance"

    if side == "buy":
        if price is None or price <= 0:
            raise ValueError("Enter a positive Entry / Limit price before sizing a buy.")

        quote_balance = _spot_quote_balance(snapshot, "USDC")
        return quote_balance / price, f"USDC spot cash at {price:g}"

    raise ValueError("Side must be buy or sell.")


def _spot_quote_balance(snapshot: PortfolioSnapshot, quote: str) -> float:
    normalized_quote = quote.strip().upper()

    for cash in snapshot.cash:
        if cash.bucket.strip().upper() == "SPOT" and cash.symbol.strip().upper() == normalized_quote:
            return max(float(cash.amount), 0.0)

    return 0.0


def _spot_base_balance(snapshot: PortfolioSnapshot, base: str) -> float:
    normalized_base = _hyperliquid_display_symbol(base)

    for holding in snapshot.holdings:
        if holding.bucket.strip().upper() != "SPOT":
            continue

        holding_base = _hyperliquid_display_symbol(holding.symbol)
        if holding_base == normalized_base:
            return max(float(holding.quantity), 0.0)

    return 0.0


def _format_hyperliquid_size(value: float) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text or "0"


def _required_float(value: object, label: str) -> float:
    number = _to_float(value)

    if number is None or number <= 0:
        raise ValueError(f"{label} must be a positive number.")

    return number


def _hyperliquid_order_confirmation_message(
    account_key: str,
    ticket: HyperliquidOrderTicket,
) -> str:
    normalized_price = normalize_hyperliquid_limit_price(ticket.limit_price, is_buy=ticket.is_buy)

    return "\n".join(
        [
            "Review this LIVE Hyperliquid order before submitting:",
            "",
            f"Account: {account_key.upper()}",
            f"Coin: {ticket.coin}",
            f"Side: {ticket.side_label}",
            f"Size: {ticket.size:g}",
            f"Limit price: {format_hyperliquid_limit_price(normalized_price)}",
            f"Estimated notional: ${ticket.notional:,.2f}",
            f"TIF: {ticket.tif}",
            f"Reduce only: {'yes' if ticket.reduce_only else 'no'}",
            "",
            "Submit this order?",
        ]
    )


def _hyperliquid_order_submitted_message(
    account_key: str,
    ticket: HyperliquidOrderTicket,
    result: object,
) -> str:
    return "\n".join(
        [
            "Hyperliquid accepted the submit request.",
            "",
            f"Account: {account_key.upper()}",
            f"Coin: {ticket.coin}",
            f"Side: {ticket.side_label}",
            f"Size: {ticket.size:g}",
            f"Limit price: {format_hyperliquid_limit_price(ticket.limit_price)}",
            f"Estimated notional: ${ticket.notional:,.2f}",
            "",
            "",
        ]
    )


def _hyperliquid_open_order_lookup_key(order: dict[str, object]) -> str:
    account_key = str(order.get("accountKey") or "").strip().lower()
    account_address = str(order.get("accountAddress") or "").strip().lower()
    order_id = str(order.get("oid") or "").strip()

    if account_key:
        return f"{account_key}:{order_id}"

    if account_address:
        return f"{account_address}:{order_id}"

    return order_id


def _hyperliquid_order_side(order: dict[str, object]) -> str:
    side = str(order.get("side") or order.get("dir") or "").strip()
    if side:
        return side

    is_buy = order.get("isBuy")
    if _to_bool(is_buy):
        return "buy"

    if is_buy is not None:
        return "sell"

    return ""


def _pnl_row_tag(*values: float | None) -> tuple[str, ...]:
    has_negative = any(value is not None and value < 0 for value in values)
    has_positive = any(value is not None and value > 0 for value in values)

    if has_negative and not has_positive:
        return ("pnl_negative",)

    if has_positive and not has_negative:
        return ("pnl_positive",)

    return ()


def _first_order_leg(order: dict[str, object]) -> dict[str, object]:
    legs = order.get("orderLegCollection")
    if not isinstance(legs, list) or not legs:
        return {}

    first_leg = legs[0]
    if not isinstance(first_leg, dict):
        return {}

    instrument = first_leg.get("instrument")
    symbol = ""
    if isinstance(instrument, dict):
        symbol = str(instrument.get("symbol") or "")

    return {
        "symbol": symbol,
        "instruction": first_leg.get("instruction"),
        "quantity": first_leg.get("quantity"),
    }


def _option_chain_rows(chain: object) -> list[dict[str, object]]:
    if not isinstance(chain, dict):
        return []

    def display_price(value: float | None) -> str:
        return "--" if value is None else f"{value:,.2f}"

    rows: list[dict[str, object]] = []
    for side, map_name in (("CALL", "callExpDateMap"), ("PUT", "putExpDateMap")):
        expiration_map = chain.get(map_name)
        if not isinstance(expiration_map, dict):
            continue

        for expiration_key, strikes in expiration_map.items():
            if not isinstance(strikes, dict):
                continue

            expiration = str(expiration_key).split(":", 1)[0]
            for strike, contracts in strikes.items():
                if not isinstance(contracts, list):
                    continue

                for contract in contracts:
                    if not isinstance(contract, dict):
                        continue

                    strike_value = _to_float(contract.get("strikePrice"))
                    if strike_value is None:
                        strike_value = _to_float(strike)

                    bid_value = _to_float(contract.get("bid"))
                    ask_value = _to_float(contract.get("ask"))
                    mark_value = _to_float(contract.get("mark"))
                    if mark_value is None and bid_value is not None and ask_value is not None:
                        mark_value = (bid_value + ask_value) / 2

                    in_the_money = _to_bool(contract.get("inTheMoney"))
                    if in_the_money is None:
                        intrinsic_value = _to_float(contract.get("intrinsicValue"))
                        if intrinsic_value is not None:
                            in_the_money = intrinsic_value > 0

                    rows.append(
                        {
                            "symbol": contract.get("symbol") or "",
                            "expiration": expiration,
                            "strike": display_price(strike_value) if strike_value is not None else strike,
                            "side": side,
                            "bid": display_price(bid_value),
                            "ask": display_price(ask_value),
                            "mark": display_price(mark_value),
                            "in_the_money": in_the_money,
                        }
                    )

    return rows


def _option_moneyness_tag(row: dict[str, object]) -> tuple[str, ...]:
    in_the_money = row.get("in_the_money")

    if in_the_money is True:
        return ("option_itm",)

    if in_the_money is False:
        return ("option_otm",)

    return ()


def _to_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value

    if isinstance(value, str):
        cleaned = value.strip().lower()

        if cleaned in {"true", "yes", "1"}:
            return True

        if cleaned in {"false", "no", "0"}:
            return False

    return None


def _positive_int(value: object, label: str) -> int:
    try:
        number = int(str(value).strip())
    except ValueError:
        raise ValueError(f"{label} must be a whole number.") from None

    if number <= 0:
        raise ValueError(f"{label} must be greater than zero.")

    return number


def _required_positive_price(value: object, label: str) -> str:
    number = _to_float(value)

    if number is None or number <= 0:
        raise ValueError(f"{label} must be a positive number.")

    return f"{number:.2f}"


def _to_float(value: object) -> float | None:
    if value is None or value == "":
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _money_or_dash(value: float | None) -> str:
    return "--" if value is None else _money(value)


def _number(value: float) -> str:
    return f"{value:g}"


def _coverage_or_dash(labels: list[str]) -> str:
    return " + ".join(labels) if labels else "no account day PnL available"
