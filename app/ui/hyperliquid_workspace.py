"""Concept B workspace. Presentation and selection state, separate from execution."""
from __future__ import annotations

import json
import os
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont, messagebox, ttk

from app.hyperliquid_accounts import HYPERLIQUID_ACCOUNT_PROFILES
from app.services.hyperliquid_markets import DEFAULT_MARKETS, display_asset
from app.ui.theme import BACKGROUND, BORDER, MUTED_TEXT, SURFACE, TEXT, SUCCESS, DANGER
from app.ui.hyperliquid_controls import INSET, EmptyOrders, ExposureTable, UnitEntry, install_order_styles

MINT = "#78edc1"


def preferences_path() -> Path:
    override = os.getenv("DUCKETZ_HYPERLIQUID_PREFERENCES_PATH")
    return Path(override) if override else Path(os.getenv("LOCALAPPDATA", Path.home())) / "Ducketz" / "hyperliquid-workspace.json"


def load_watchlist() -> list[str]:
    try:
        data = json.loads(preferences_path().read_text(encoding="utf-8"))
        additions = data.get("markets", [])
        if not isinstance(additions, list):
            additions = []
    except (OSError, ValueError, AttributeError):
        additions = []
    return list(dict.fromkeys((*DEFAULT_MARKETS, *(s for s in additions if isinstance(s, str) and 0 < len(s) < 40))))[:24]


class HyperliquidWorkspaceMixin:
    @property
    def _u(self):
        # The host owns the existing ticket/confirmation adapter and pure views.
        from app.ui import ducket_bucket
        return ducket_bucket

    def _label(self, parent, text="", *, variable=None, size=10, bold=False, color=TEXT):
        args = {"textvariable": variable} if variable is not None else {"text": text}
        return tk.Label(parent, **args, bg=SURFACE, fg=color, anchor="w", font=("Segoe UI", size, "bold" if bold else "normal"))

    def _heading(self, parent, text):
        label = self._label(parent, text, size=12, bold=True)
        label.pack(anchor="w", pady=(0, 10))
        return label

    def _build(self, parent, title, sync_button_text):
        u = self._u
        self._apply_hyperliquid_styles()
        self._order_scale = 1.0
        self._order_font_sizes = {"body": 11, "small": 10, "heading": 13, "strong": 11}
        self._order_fonts = {name: tkfont.Font(root=self.root, family="Segoe UI", size=size, weight="bold" if name in {"heading", "strong"} else "normal") for name, size in self._order_font_sizes.items()}
        install_order_styles(self.root, self._order_fonts)
        self.watchlist = load_watchlist()
        self._market_facts = {}
        self._catalog = []
        self._spots = {}
        self._orders_by_account = {}
        self._order_errors = {}
        self._orders_loading = False
        self._snapshot_by_account = {}
        self._all_rows = ()
        self._activity = []
        self.account_filter = tk.StringVar(self.root, "All accounts")
        self.market_filter = tk.StringVar(self.root, "All markets")
        self.composer_account = tk.StringVar(self.root, "Clearpond")
        self.composer_product = tk.StringVar(self.root, "Perp")
        self.composer_market = tk.StringVar(self.root, "HYPE")
        self.composer_available = tk.StringVar(self.root, "Sync to load balance")
        self.composer_notional = tk.StringVar(self.root, "—")
        self.account_total = tk.StringVar(self.root, "—")
        self.portfolio_message = tk.StringVar(self.root, "Sync Hyperliquid to load your portfolio.")
        self.detail_title = tk.StringVar(self.root, "Clearpond Cash & Status")
        self.detail_balance = tk.StringVar(self.root, "—")
        self.detail_status = tk.StringVar(self.root, "Awaiting account sync")
        self.risk_details = tk.BooleanVar(self.root, False)
        self._account_notes = {}
        self._market_widgets = {}
        self._context = None

        self.canvas = tk.Canvas(parent, bg=BACKGROUND, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=self.canvas.yview, style="Hyper.Vertical.TScrollbar")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.body = ttk.Frame(self.canvas, style="HyperPage.TFrame", padding=(14, 12, 14, 12))
        self._body_window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(1, weight=1)
        header = self.header_frame = ttk.Frame(self.body, style="HyperPage.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        tk.Label(header, text=title, bg=BACKGROUND, fg=TEXT, font=("Segoe UI", 18, "bold")).pack(side="left")
        self.sync_button = ttk.Button(header, text="↻  " + sync_button_text, command=self._sync)
        self.sync_button.pack(side="right")
        tk.Label(header, textvariable=self.last_sync, bg=BACKGROUND, fg=MUTED_TEXT, font=("Segoe UI", 9)).pack(side="right", padx=16)
        self.workspace = ttk.Frame(self.body, style="HyperPage.TFrame")
        self.workspace.grid(row=1, column=0, sticky="nsew")
        self.workspace.rowconfigure(0, weight=1)
        self.accounts_rail = self._hyper_card(self.workspace, padding=(10, 10))
        self.center = ttk.Frame(self.workspace, style="HyperPage.TFrame")
        self.center.columnconfigure(0, weight=1)
        self.center.rowconfigure(1, weight=1)
        self.composer_card = self.trading_frame = self._hyper_card(self.workspace, padding=(14, 12))
        self._build_accounts()
        self._build_markets()
        self._build_portfolio()
        self._build_details()
        self._build_composer()
        footer = self.chain_card = ttk.Frame(self.body, style="HyperPage.TFrame")
        footer.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        for text, var in (("HyperEVM · Chain ", self.chain_id), ("   Block ", self.chain_block), ("   Base fee ", self.chain_base_fee)):
            tk.Label(footer, text=text, bg=BACKGROUND, fg=MUTED_TEXT, font=("Segoe UI", 8)).pack(side="left")
            tk.Label(footer, textvariable=var, bg=BACKGROUND, fg=TEXT, font=("Segoe UI", 8)).pack(side="left")
        tk.Label(footer, textvariable=self.sync_status, bg=BACKGROUND, fg=MUTED_TEXT, font=("Segoe UI", 8)).pack(side="right")
        self._style_workspace_controls(self.body)
        self._select_context()
        self._apply_order_density(1.0, force=True)
        for var in (self.composer_account, self.composer_product, self.composer_market):
            var.trace_add("write", self._select_context)
        for var in (self.spot_quantity, self.spot_entry_limit, self.spot_size_unit, self.perp_size, self.perp_entry_limit):
            var.trace_add("write", self._update_notional)
        self.spot_side.trace_add("write", lambda *_: self._refresh_composer_status())
        self.body.bind("<Configure>", self._update_hyperliquid_scroll_region)
        for panel in (self.workspace, self.accounts_rail, self.center, self.composer_card):
            panel.bind("<Configure>", lambda e: self.root.after_idle(self._sync_hyperliquid_body_height))
        self.canvas.bind("<Configure>", self._resize_hyperliquid_body)
        self.root.bind("<MouseWheel>", self._scroll_workspace, add="+")
        self._apply_hyperliquid_layout(1700, force=True)

    def _build_accounts(self):
        u = self._u
        self._heading(self.accounts_rail, "Accounts")
        total = self._hyper_card(self.accounts_rail, padding=(10, 9))
        total.pack(fill="x", pady=(0, 10))
        self._label(total, "All accounts · 3 accounts", bold=True).pack(anchor="w")
        self._label(total, variable=self.account_total, size=16, bold=True).pack(anchor="w", pady=(4, 0))
        self._bind_children(total, "<Button-1>", lambda e: self._filter_account("All accounts"))
        self.account_cards = {}
        for key, profile in HYPERLIQUID_ACCOUNT_PROFILES.items():
            card = self._hyper_card(self.accounts_rail, padding=(10, 10))
            card.pack(fill="both", expand=True, pady=(0, 8))
            self.account_cards[key] = card
            top = tk.Frame(card, bg=SURFACE)
            top.pack(fill="x")
            u._HyperliquidAssetMark(top, key, size=62).pack(side="left", padx=(0, 10))
            copy = tk.Frame(top, bg=SURFACE)
            copy.pack(side="left", fill="x", expand=True)
            self._label(copy, profile.label, size=12, bold=True).pack(anchor="w")
            self._label(copy, "Equity", size=8, color=MUTED_TEXT).pack(anchor="w", pady=(3, 0))
            self._label(copy, variable=self.account_summary_values[key]["equity"], size=14, bold=True).pack(anchor="w")
            for metric, name in (("pnl", "Unrealized P/L"), ("available", "Available"), ("margin", "Margin used")):
                row = tk.Frame(card, bg=SURFACE)
                row.pack(fill="x", pady=(6 if metric == "pnl" else 2, 0))
                self._label(row, name, size=9, color=MUTED_TEXT).pack(side="left")
                value = self._label(row, variable=self.account_summary_values[key][metric], bold=metric == "pnl")
                value.pack(side="right")
                if metric == "pnl":
                    self.account_pnl_labels[key] = value
            note = tk.StringVar(self.root, "Awaiting sync")
            self._account_notes[key] = note
            self._label(card, variable=note, size=8, color=MUTED_TEXT).pack(anchor="w", pady=(5, 0))
            self._bind_children(card, "<Button-1>", lambda e, label=profile.label: self._filter_account(label))

    def _bind_children(self, widget, event, command):
        widget.bind(event, command)
        widget.configure(cursor="hand2")
        for child in widget.winfo_children():
            self._bind_children(child, event, command)

    def _filter_account(self, label):
        self.account_filter.set(label)
        if label != "All accounts":
            self.composer_account.set(label)
        self._render_portfolio()

    def _build_markets(self):
        self.markets_card = self._hyper_card(self.center, padding=(10, 10))
        self.markets_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header = tk.Frame(self.markets_card, bg=SURFACE)
        header.pack(fill="x", pady=(0, 8))
        self._label(header, "Markets", size=12, bold=True).pack(side="left")
        self._label(header, "  Perp mark · 24h", size=8, color=MUTED_TEXT).pack(side="left")
        ttk.Button(header, text="+ Add market", command=self._add_market).pack(side="right")
        self.market_grid = tk.Frame(self.markets_card, bg=SURFACE)
        self.market_grid.pack(fill="x")
        self._rebuild_market_tiles()

    def _rebuild_market_tiles(self):
        u = self._u
        for child in self.market_grid.winfo_children():
            child.destroy()
        self._market_widgets = {}
        for column in range(4):
            self.market_grid.columnconfigure(column, weight=1, uniform="market")
        for i, coin in enumerate(self.watchlist):
            tile = self._hyper_card(self.market_grid, padding=(9, 7))
            tile.grid(row=i // 4, column=i % 4, sticky="nsew", padx=(0 if i % 4 == 0 else 4, 0), pady=(0, 3))
            head = tk.Frame(tile, bg=SURFACE)
            head.pack(fill="x")
            u._HyperliquidAssetMark(head, coin.lower(), size=36).pack(side="left", padx=(0, 7))
            self._label(head, coin, bold=True).pack(side="left")
            price, change = tk.StringVar(self.root, "—"), tk.StringVar(self.root, "Awaiting sync")
            self._label(tile, variable=price, size=14, bold=True).pack(anchor="w", pady=(5, 0))
            change_label = self._label(tile, variable=change, color=MUTED_TEXT)
            change_label.pack(anchor="w")
            spark = u._HyperliquidSparkline(tile, height=32)
            spark.configure(width=70)
            spark.pack(fill="x", pady=(4, 0))
            self._market_widgets[coin] = (price, change, change_label, spark)
            self._bind_children(tile, "<Button-1>", lambda e, symbol=coin: self.composer_market.set(symbol))
        if hasattr(self, "market_combo"):
            self._refresh_market_picker()
        self._render_markets()

    def _render_markets(self):
        u = self._u
        for coin, (price, change, label, spark) in self._market_widgets.items():
            facts = self._market_facts.get(coin, {})
            price.set(u._money_or_dash(u._to_float(facts.get("price"))))
            delta = u._to_float(facts.get("change_percent_24h"))
            status = facts.get("status", "unavailable")
            change.set(f"{delta:+.2f}%" if delta is not None and status == "current" else "Stale quote" if status == "stale" else "Unavailable")
            label.configure(fg=u._pnl_color(delta) if status == "current" else MUTED_TEXT)
            spark._line_color = DANGER if delta is not None and delta < 0 else MINT
            spark.set_values(facts.get("closes_24h", ()) if status == "current" else ())

    def _add_market(self):
        choices = [coin for coin in self._catalog if coin not in self.watchlist]
        if not choices:
            messagebox.showinfo("Add market", "Sync Hyperliquid to load available perpetual markets.")
            return
        if len(self.watchlist) >= 24:
            messagebox.showinfo("Add market", "This watchlist supports up to 24 markets.")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Add market")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both")
        ttk.Label(body, text="Watch a Hyperliquid perpetual market").pack(anchor="w")
        selected = tk.StringVar(dialog, choices[0])
        picker = ttk.Combobox(body, textvariable=selected, values=choices, state="readonly", width=30)
        picker.pack(fill="x", pady=10)
        def add():
            coin = selected.get()
            if coin not in choices or coin in self.watchlist:
                return
            new_list = self.watchlist + [coin]
            try:
                path = preferences_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps({"markets": new_list}), encoding="utf-8")
                temp.replace(path)
            except OSError:
                messagebox.showerror("Add market", "Could not save the watchlist.")
                return
            self.watchlist = new_list
            self._rebuild_market_tiles()
            self.market_filter_combo.configure(values=("All markets", *self.watchlist))
            dialog.destroy()
            self._sync()
        ttk.Button(body, text="Add to watchlist", command=add).pack(fill="x")

    def _tree(self, parent, columns, *, height=4, icons=False):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        table = ttk.Treeview(parent, columns=tuple(c[0] for c in columns), show="tree headings" if icons else "headings", height=height, style="HyperPortfolio.Treeview", selectmode="browse")
        table.grid(row=0, column=0, sticky="nsew")
        if icons:
            table.heading("#0", text="")
            table.column("#0", width=42, minwidth=42, stretch=False)
        for key, label, width in columns:
            table.heading(key, text=label)
            table.column(key, width=width, minwidth=width, stretch=True, anchor="e" if key in {"size", "entry", "mark", "price", "value", "pnl", "pnl_pct", "liquidation", "amount", "fee"} else "w")
        y = ttk.Scrollbar(parent, orient="vertical", command=table.yview, style="Hyper.Vertical.TScrollbar")
        y.grid(row=0, column=1, sticky="ns")
        x = ttk.Scrollbar(parent, orient="horizontal", command=table.xview, style="Hyper.Horizontal.TScrollbar")
        x.grid(row=1, column=0, sticky="ew")
        def scroll(bar, first, last):
            bar.set(first, last)
            if float(first) <= 0 and float(last) >= 1:
                bar.grid_remove()
            else:
                bar.grid()
        table.configure(xscrollcommand=lambda a, b: scroll(x, a, b), yscrollcommand=lambda a, b: scroll(y, a, b))
        table.tag_configure("pnl_positive", foreground=SUCCESS)
        table.tag_configure("pnl_negative", foreground=DANGER)
        return table

    def _build_portfolio(self):
        card = self.portfolio_card = self._hyper_card(self.center, padding=(10, 10))
        card.grid(row=1, column=0, sticky="nsew", pady=(0, 10))
        header = tk.Frame(card, bg=SURFACE)
        header.pack(fill="x", pady=(0, 10))
        self._label(header, "Portfolio", size=12, bold=True).pack(side="left")
        self.market_filter_combo = ttk.Combobox(header, textvariable=self.market_filter, values=("All markets", *self.watchlist), state="readonly", width=12)
        self.market_filter_combo.pack(side="right")
        accounts = ttk.Combobox(header, textvariable=self.account_filter, values=("All accounts", *(p.label for p in HYPERLIQUID_ACCOUNT_PROFILES.values())), state="readonly", width=12)
        accounts.pack(side="right", padx=8)
        for combo in (accounts, self.market_filter_combo):
            combo.bind("<<ComboboxSelected>>", lambda e: self._render_portfolio())
        tabs = tk.Frame(card, bg=SURFACE)
        tabs.pack(fill="x", pady=(0, 8))
        self._view_buttons = {}
        for name in ("Positions", "Cash", "Open Orders", "Activity"):
            button = tk.Button(tabs, text=name, command=lambda n=name: self._choose_view(n), bd=0, padx=10, pady=6, bg=SURFACE, fg=TEXT, activebackground="#174b4a", activeforeground=MINT, font=("Segoe UI", 9))
            button.pack(side="left")
            self._view_buttons[name] = button
        ttk.Checkbutton(tabs, text="Risk details", variable=self.risk_details, command=self._set_risk_columns).pack(side="right")
        self.portfolio_stack = tk.Frame(card, bg=SURFACE)
        self.portfolio_stack.pack(fill="both", expand=True)
        self.portfolio_stack.columnconfigure(0, weight=1)
        self.portfolio_stack.rowconfigure(0, weight=1)
        self.positions_view, self.cash_view, self.portfolio_orders_view, self.activity_view = [tk.Frame(self.portfolio_stack, bg=SURFACE) for _ in range(4)]
        for view in (self.positions_view, self.cash_view, self.portfolio_orders_view, self.activity_view):
            view.grid(row=0, column=0, sticky="nsew")
        self.holdings_table = self._tree(self.positions_view, (("account", "Account", 72), ("market", "Market", 110), ("type", "Type", 80), ("side", "Side", 58), ("size", "Size / Qty", 102), ("entry", "Entry price", 87), ("mark", "Mark price", 87), ("value", "Value", 90), ("pnl", "Unrealized P/L", 102), ("pnl_pct", "ROE %", 70), ("liquidation", "Liquidation", 90)), icons=True)
        self.holdings_table.bind("<<TreeviewSelect>>", self._use_selected_hyperliquid_holding)
        self.cash_table = self._tree(self.cash_view, (("account", "Account", 95), ("bucket", "Balance", 90), ("symbol", "Asset", 70), ("amount", "Quantity", 110), ("value", "Value", 110)))
        self.portfolio_orders_table = self._tree(self.portfolio_orders_view, self._order_columns())
        self.portfolio_orders_table.bind("<<TreeviewSelect>>", self._use_selected_hyperliquid_order)
        self.activity_table = self._tree(self.activity_view, (("time", "Time", 110), ("account", "Account", 80), ("market", "Market", 90), ("side", "Fill", 90), ("size", "Size", 90), ("price", "Price", 90), ("pnl", "Closed P/L", 90), ("fee", "Fee", 70)))
        self._label(card, variable=self.portfolio_message, size=8, color=MUTED_TEXT).pack(anchor="w", pady=(5, 0))
        self._set_risk_columns()
        self._choose_view("Positions")

    def _order_columns(self):
        return (("account", "Account", 75), ("market", "Market", 100), ("kind", "Kind", 55), ("side", "Side", 55), ("size", "Size", 80), ("price", "Price", 80), ("type", "Type", 60), ("reduce", "Reduce", 55), ("oid", "Order ID", 90))

    def _set_risk_columns(self):
        columns = ("account", "market", "type", "side", "size", "entry", "mark", "pnl")
        self.holdings_table.configure(displaycolumns="#all" if self.risk_details.get() else columns)

    def _choose_view(self, name):
        self.portfolio_view.set(name)
        self._switch_hyperliquid_portfolio_view()

    def _switch_hyperliquid_portfolio_view(self):
        name = self.portfolio_view.get()
        views = {"Positions": self.positions_view, "Cash": self.cash_view, "Open Orders": self.portfolio_orders_view, "Activity": self.activity_view}
        views[name].tkraise()
        for label, button in self._view_buttons.items():
            button.configure(bg="#174b4a" if label == name else SURFACE, fg=MINT if label == name else TEXT)
        if hasattr(self, "detail_status") and self.latest_hyperliquid_bucket is not None:
            self._portfolio_hint()

    def _build_details(self):
        details = tk.Frame(self.center, bg=BACKGROUND)
        details.grid(row=2, column=0, sticky="ew")
        details.columnconfigure((0, 1), weight=1, uniform="details")
        cash = self._hyper_card(details, padding=(12, 10))
        cash.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        self._label(cash, variable=self.detail_title, size=11, bold=True).pack(anchor="w", pady=(0, 10))
        row = tk.Frame(cash, bg=SURFACE)
        row.pack(fill="x")
        self.detail_avatar = self._u._HyperliquidAssetMark(row, "clearpond", size=60)
        self.detail_avatar.pack(side="left", padx=(0, 12))
        self._label(row, variable=self.detail_balance, size=14, bold=True).pack(side="left")
        self.detail_status_label = self._label(cash, variable=self.detail_status, color=MUTED_TEXT)
        self.detail_status_label.configure(wraplength=300, justify="left")
        self.detail_status_label.pack(anchor="w", pady=(10, 4))
        exposure = self.exposure_card = self._hyper_card(details, padding=(14, 12))
        exposure.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        self._heading(exposure, "Exposure by Market")
        self.exposure_table = ExposureTable(exposure, self._order_fonts, self._u._HyperliquidAssetMark)
        self.exposure_table.pack(fill="x")

    def _build_composer(self):
        card = self.composer_card
        fonts = self._order_fonts
        self.composer_heading = self._label(card, "Trade Composer", bold=True)
        self.composer_heading.configure(font=fonts["heading"])
        self.composer_heading.pack(anchor="w", pady=(0, 14))
        header = self.composer_account_header = tk.Frame(card, bg=SURFACE)
        header.pack(fill="x", pady=(0, 16))
        header.columnconfigure(1, weight=1)
        self.composer_avatar = self._u._HyperliquidAssetMark(header, "clearpond", size=82)
        self.composer_avatar.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        self._order_label(header, "Account", muted=True).grid(row=0, column=1, sticky="sw", pady=(8, 4))
        self.composer_account_combo = ttk.Combobox(header, textvariable=self.composer_account, values=tuple(p.label for p in HYPERLIQUID_ACCOUNT_PROFILES.values()), state="readonly", width=10, font=fonts["body"], style="HyperOrder.TCombobox")
        self.composer_account_combo.grid(row=1, column=1, sticky="new", pady=(0, 8))
        self.balance_caption = tk.StringVar(self.root, "Available · USDC")
        self.balance_amount = tk.StringVar(self.root, "—")
        self._order_label(header, variable=self.balance_caption, muted=True).grid(row=0, column=2, sticky="sw", padx=(14, 0), pady=(8, 4))
        self._order_label(header, variable=self.balance_amount, strong=True).grid(row=1, column=2, sticky="w", padx=(14, 0))
        products = self.product_group = tk.Frame(card, bg=SURFACE)
        products.pack(fill="x", pady=(0, 14))
        products.columnconfigure((0, 1), weight=1, uniform="product")
        self._product_buttons = {}
        for i, name in enumerate(("Spot", "Perp")):
            button = ttk.Radiobutton(products, text=name, variable=self.composer_product, value=name, style="HyperOrderProduct.TRadiobutton", takefocus=True)
            button.grid(row=0, column=i, sticky="ew", padx=(0, 4) if i == 0 else (4, 0))
            self._product_buttons[name] = button
        self.market_heading = tk.Frame(card, bg=SURFACE)
        self.market_heading.pack(fill="x", pady=(0, 6))
        self._order_label(self.market_heading, "Market", muted=True).pack(side="left")
        self.mid_button = ttk.Button(self.market_heading, text="Use mid", command=self._composer_mid, style="HyperOrderLink.TButton")
        self.mid_button.pack(side="right")
        self.market_control = ttk.Frame(card, style="HyperOrderField.TFrame", padding=(8, 2))
        self.market_control.pack(fill="x")
        self.market_control.columnconfigure(1, weight=1)
        self.composer_market_avatar = self._u._HyperliquidAssetMark(self.market_control, "hype", size=32)
        self.composer_market_avatar.configure(background=INSET)
        self.composer_market_avatar.grid(row=0, column=0, padx=(0, 4))
        self.market_choice = tk.StringVar(self.root)
        self.market_combo = ttk.Combobox(self.market_control, textvariable=self.market_choice, values=(), state="readonly", width=1, font=fonts["body"], style="HyperOrderInline.TCombobox")
        self.market_combo.grid(row=0, column=1, sticky="ew")
        self.market_combo.bind("<<ComboboxSelected>>", lambda e: self.composer_market.set(self._market_choice_to_coin[self.market_choice.get()]))
        self.market_route = tk.StringVar(self.root)
        self.route_label = self._order_label(card, variable=self.market_route, muted=True)
        self.route_label.pack(anchor="w", pady=(4, 14))
        self.ticket_fields = tk.Frame(card, bg=SURFACE)
        self.ticket_fields.pack(fill="x")
        self.notional_group = tk.Frame(card, bg=SURFACE)
        self.notional_group.pack(fill="x", pady=(16, 14))
        self._order_label(self.notional_group, "Est. notional (USDC)", muted=True).pack(side="left")
        self._order_label(self.notional_group, variable=self.composer_notional, strong=True).pack(side="right")
        self.review_button = ttk.Button(card, text="Review Order", command=self._review_composer, style="HyperOrderPrimary.TButton")
        self.review_button.pack(fill="x", pady=(0, 16))
        tk.Frame(card, height=1, bg=BORDER).pack(fill="x")
        title = self.orders_heading = tk.Frame(card, bg=SURFACE)
        title.pack(fill="x", pady=(12, 12))
        title.columnconfigure(0, weight=1)
        self._order_label(title, "Open orders", strong=True).grid(row=0, column=0, sticky="w")
        self.orders_refresh_button = ttk.Button(title, text="Refresh", width=7, command=self._load_hyperliquid_open_orders, style="HyperOrderAction.TButton")
        self.orders_refresh_button.grid(row=0, column=1, padx=(8, 8))
        self.orders_account_combo = ttk.Combobox(title, textvariable=self.composer_account, values=tuple(p.label for p in HYPERLIQUID_ACCOUNT_PROFILES.values()), state="readonly", width=10, font=fonts["body"], style="HyperOrder.TCombobox")
        self.orders_account_combo.grid(row=0, column=2)
        self.orders_panel = tk.Frame(card, bg=INSET)
        self.orders_panel.pack(fill="both", expand=True)
        self.orders_panel.columnconfigure(0, weight=1)
        self.orders_panel.rowconfigure(0, weight=1, minsize=160)
        self.orders_table_frame = tk.Frame(self.orders_panel, bg=INSET)
        self.orders_table_frame.grid(row=0, column=0, sticky="nsew")
        self.hyperliquid_open_orders_table = self._tree(self.orders_table_frame, self._order_columns(), height=3)
        self.hyperliquid_open_orders_table.configure(displaycolumns=("market", "side", "size", "price"), style="HyperOrders.Treeview")
        self.hyperliquid_open_orders_table.bind("<<TreeviewSelect>>", self._use_selected_hyperliquid_order)
        self.orders_empty_panel = EmptyOrders(self.orders_panel, fonts)
        self.orders_empty_panel.grid(row=0, column=0, sticky="nsew")
        self.orders_empty = self.orders_empty_panel.title
        self.orders_actions = tk.Frame(card, bg=SURFACE)
        self.orders_actions.pack(fill="x", pady=(8, 0))
        self.edit_order_button = ttk.Button(self.orders_actions, text="Edit selected", command=self._edit_selected_hyperliquid_open_order, style="HyperOrderAction.TButton")
        self.edit_order_button.pack(side="left", fill="x", expand=True)
        self.cancel_order_button = ttk.Button(self.orders_actions, text="Cancel selected", command=self._cancel_selected_hyperliquid_order, style="HyperOrderAction.TButton")
        self.cancel_order_button.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.orders_status_label = self._order_label(card, variable=self.orders_status, muted=True)
        self.orders_status_label.pack(anchor="w", pady=(6, 0))
        self._update_order_actions()

    def _order_label(self, parent, text="", *, variable=None, muted=False, strong=False):
        widget = self._label(parent, text, variable=variable, color=MUTED_TEXT if muted else TEXT)
        widget.configure(font=self._order_fonts["small" if muted else "strong" if strong else "body"], padx=0, pady=0)
        return widget

    def _refresh_market_picker(self):
        suffix = "-PERP" if self.composer_product.get() == "Perp" else "/USDC"
        self._market_choice_to_coin = {coin + suffix: coin for coin in self.watchlist}
        self.market_combo.configure(values=tuple(self._market_choice_to_coin))
        self.market_choice.set(self.composer_market.get() + suffix)
        self.composer_market_avatar._asset_key = self.composer_market.get().lower()
        self.composer_market_avatar._draw()


    def _draw_ticket_fields(self):
        for child in self.ticket_fields.winfo_children():
            child.destroy()
        spot = self.composer_product.get() == "Spot"
        side = self.spot_side if spot else self.perp_direction
        scale = self._order_scale
        self.ticket_fields.columnconfigure(0, minsize=round(84 * scale))
        self.ticket_fields.columnconfigure(1, weight=1)
        self.ticket_inputs = []
        self.ticket_labels = []
        def label(text, row):
            widget = self._order_label(self.ticket_fields, text, muted=True)
            widget.grid(row=row, column=0, sticky="w", padx=(0, 10), pady=(0, round(12 * scale)))
            self.ticket_labels.append(widget)
        label("Side", 0)
        self.side_group = tk.Frame(self.ticket_fields, bg=SURFACE)
        self.side_group.grid(row=0, column=1, sticky="ew", pady=(0, round(12 * scale)))
        self.side_group.columnconfigure((0, 1), weight=1, uniform="side")
        self.side_buttons = {}
        for i, (text, value, style) in enumerate((("Buy" if spot else "Long", "buy", "HyperOrderLong.TRadiobutton"), ("Sell" if spot else "Short", "sell", "HyperOrderShort.TRadiobutton"))):
            button = ttk.Radiobutton(self.side_group, text=text, variable=side, value=value, style=style, takefocus=True)
            button.grid(row=0, column=i, sticky="ew", padx=(0, 5) if i == 0 else (5, 0))
            self.side_buttons[value] = button
        label("Quantity" if spot else "Size", 1)
        quantity = UnitEntry(self.ticket_fields, self.spot_quantity if spot else self.perp_size, font=self._order_fonts["body"], unit=self.composer_market.get(), unit_variable=self.spot_size_unit if spot else None)
        quantity.grid(row=1, column=1, sticky="ew", pady=(0, round(12 * scale)))
        label("Limit price", 2)
        price = UnitEntry(self.ticket_fields, self.spot_entry_limit if spot else self.perp_entry_limit, font=self._order_fonts["body"], unit="USDC")
        price.grid(row=2, column=1, sticky="ew", pady=(0, round(12 * scale)))
        self.ticket_inputs = [quantity, price]
        for field in self.ticket_inputs:
            field.set_scale(scale)
        label("Time in force", 3)
        self.tif_group = tk.Frame(self.ticket_fields, bg=SURFACE)
        self.tif_group.grid(row=3, column=1, sticky="ew", pady=(0, round(12 * scale)))
        self.tif_group.columnconfigure(0, weight=1)
        self.tif_combo = ttk.Combobox(self.tif_group, textvariable=self.spot_tif if spot else self.perp_tif, values=("Gtc", "Ioc", "Alo"), state="readonly", width=6, font=self._order_fonts["body"], style="HyperOrder.TCombobox")
        self.tif_combo.grid(row=0, column=0, sticky="ew")
        if not spot:
            ttk.Checkbutton(self.tif_group, text="Reduce only", variable=self.perp_reduce_only, style="HyperOrder.TCheckbutton").grid(row=0, column=1, padx=(12, 0), sticky="e")

    def _apply_order_density(self, scale, *, force=False):
        if not force and scale == self._order_scale:
            return
        self._order_scale = scale
        px = lambda n: round(n * scale)
        for name, font in self._order_fonts.items():
            font.configure(size=px(self._order_font_sizes[name]))
        install_order_styles(self.root, self._order_fonts, scale)
        self.composer_card.configure(padx=px(16), pady=px(14))
        self.composer_heading.pack_configure(pady=(0, px(10)))
        self.composer_account_header.pack_configure(pady=(0, px(16)))
        self.product_group.pack_configure(pady=(0, px(12)))
        self.route_label.pack_configure(pady=(4, px(10)))
        self.review_button.pack_configure(pady=(0, px(14)))
        self.orders_heading.pack_configure(pady=(px(10), px(10)))
        self.notional_group.pack_configure(pady=(px(8), px(8)))
        self.orders_panel.rowconfigure(0, minsize=px(155))
        self.ticket_fields.columnconfigure(0, minsize=px(84))
        for widget in (*self.ticket_labels, *self.ticket_inputs, self.side_group, self.tif_group):
            widget.grid_configure(pady=(0, px(10)))
        for field in self.ticket_inputs:
            field.set_scale(scale)
        for avatar, size in ((self.composer_avatar, 82), (self.composer_market_avatar, 32)):
            avatar._size = px(size)
            avatar.configure(width=px(size), height=px(size))
            avatar._draw()
        self.orders_empty_panel.set_scale(scale)
        self.exposure_table.set_scale(scale)


    def _select_context(self, *_):
        context = (self.composer_account.get(), self.composer_product.get(), self.composer_market.get())
        if context == self._context:
            return
        self._context = context
        label, product, coin = context
        key = self._u.hyperliquid_account_key(label)
        for variable in (self.spot_quantity, self.spot_entry_limit, self.perp_size, self.perp_entry_limit):
            variable.set("")
        self.perp_reduce_only.set(False)
        self.spot_side.set("buy")
        self.perp_direction.set("buy")
        self.spot_tif.set("Gtc")
        self.perp_tif.set("Gtc")
        self.spot_size_unit.set("USDC")
        self.spot_account.set(self._u._account_choice_for_key(key))
        self.perp_account.set(self._u._account_choice_for_key(key))
        self.spot_market.set(coin)
        self.perp_coin.set(coin)
        self.selected_hyperliquid_order_key = ""
        self._refresh_market_picker()
        for avatar in (self.composer_avatar, self.detail_avatar):
            avatar._asset_key = key
            avatar._draw()
        for account, card in self.account_cards.items():
            card.configure(highlightbackground=MINT if account == key else BORDER)
        self._draw_ticket_fields()
        self._apply_order_density(self._order_scale, force=True)
        self._refresh_composer_status()
        self._render_open_orders()

    def _refresh_composer_status(self):
        u = self._u
        key = u.hyperliquid_account_key(self.composer_account.get())
        snapshot = self._snapshot_by_account.get(key)
        facts = snapshot.account_facts if snapshot else {}
        usable = snapshot is not None and not facts.get("sync_error")
        spot = self.composer_product.get() == "Spot"
        coin = self.composer_market.get()
        route = self._spots.get(coin)
        balance_asset = route["base"] if spot and route and self.spot_side.get() == "sell" else "USDC"
        available = facts.get("spot_available", {}).get(balance_asset, 0.0) if spot else facts.get("available")
        amount = u._money_or_dash(available) if balance_asset == "USDC" else u._number(available or 0)
        self.composer_available.set(f"Available · {amount} {balance_asset}" if usable else "Account data unavailable · Sync to load")
        self.balance_caption.set(f"Available · {balance_asset}" if usable else "Sync needed")
        self.balance_amount.set(amount if usable else "—")
        self.market_route.set(f"Spot · {route['pair']}" if spot and route else "Spot pair unavailable · Sync to verify" if spot else f"{coin}-PERP · USDC")
        market_ready = bool(route) if spot else coin in self._catalog and self._market_facts.get(coin, {}).get("status") == "current"
        self.review_button.configure(state="normal" if usable and market_ready else "disabled")
        self.detail_title.set(self.composer_account.get() + " Cash & Status")
        self.detail_balance.set(f"{u._money_or_dash(facts.get('cash_usdc'))} USDC" if usable else "—")
        count = len(snapshot.holdings) if usable else 0
        mode = "Portfolio margin" if facts.get("account_mode") == "portfolioMargin" else "Unified account" if facts.get("unified") else "Spot & perps account"
        self.detail_status.set(mode + (" · No open positions" if count == 0 else f" · {count} holdings / positions") if usable else "Account unavailable. Sync to load balances.")

    def _update_notional(self, *_):
        if not hasattr(self, "composer_notional"):
            return
        u = self._u
        spot = self.composer_product.get() == "Spot"
        size = u._to_float(self.spot_quantity.get() if spot else self.perp_size.get())
        price = u._to_float(self.spot_entry_limit.get() if spot else self.perp_entry_limit.get())
        value = size if spot and self.spot_size_unit.get() == "USDC" else size * price if size is not None and price is not None else None
        self.composer_notional.set(u._money_or_dash(value))

    def _composer_mid(self):
        # Public lookup only; never substitute a perp quote for a spot pair.
        spot = self.composer_product.get() == "Spot"
        coin = self.composer_market.get()
        if spot:
            route = self._spots.get(coin)
            if not route:
                messagebox.showinfo("Use mid", "Sync to verify the selected spot pair.")
                return
            self._use_hyperliquid_mid(route["coin"], self.spot_entry_limit)
        else:
            self._use_hyperliquid_mid(coin, self.perp_entry_limit)

    def _review_composer(self):
        self._refresh_composer_status()
        if str(self.review_button.cget("state")) == "disabled":
            messagebox.showinfo("Review Order", "Sync the selected account and market before reviewing an order.")
            return
        if self.composer_product.get() == "Spot":
            self.spot_market.set(self._spots[self.composer_market.get()]["pair"])
            self._review_spot_order()
        else:
            self._review_perp_order()

    def _show_bucket(self, bucket):
        u = self._u
        self.latest_hyperliquid_bucket = bucket
        self._snapshot_by_account = {u.hyperliquid_account_key(s.account_label): s for s in bucket.snapshots}
        self._all_rows = u.hyperliquid_position_views([s for s in bucket.snapshots if not s.account_facts.get("sync_error")])
        valid = []
        self._activity = []
        for key in HYPERLIQUID_ACCOUNT_PROFILES:
            snapshot = self._snapshot_by_account.get(key)
            facts = snapshot.account_facts if snapshot else {}
            if snapshot is None or facts.get("sync_error"):
                self._set_account_summary_unavailable(key)
                self._account_notes[key].set("Account unavailable")
                self._orders_by_account[key] = []
                self._order_errors[key] = "Account unavailable"
                continue
            valid.append(snapshot)
            self._set_account_summary(key, u.hyperliquid_account_summary(snapshot))
            mode = "Portfolio margin" if facts.get("account_mode") == "portfolioMargin" else "Unified account"
            self._account_notes[key].set(mode if facts.get("unified") else "Spot " + u._compact_money(facts.get("spot_equity")) + " · Perps " + u._compact_money(facts.get("perp_equity")))
            self._orders_by_account[key] = self._annotate_orders(key, facts.get("open_orders", []))
            self._order_errors[key] = facts.get("open_orders_error", "")
            for row in facts.get("activity", []):
                self._activity.append({**row, "accountKey": key, "accountLabel": snapshot.account_label})
        common = self._common_hyperliquid_facts(bucket.snapshots)
        for coin in self.watchlist:
            fresh = common.get("markets", {}).get(coin, {})
            self._market_facts[coin] = fresh if fresh.get("status") == "current" else {**self._market_facts.get(coin, {}), "status": "stale" if coin in self._market_facts else "unavailable"}
        self._catalog = common.get("market_catalog", [])
        self._spots = common.get("spot_catalog", {})
        self.account_total.set(u._money(sum(s.total_value for s in valid)) + (" · partial" if len(valid) < 3 else "") if valid else "—")
        self._show_chain_status(common.get("chain_status"))
        self._render_markets()
        self._render_portfolio()
        self._refresh_composer_status()
        self._render_open_orders()
        times = [s.synced_at for s in valid if s.synced_at]
        self.last_sync.set("Last synced: " + u._format_local_timestamp(max(times) if times else None))
        self.sync_status.set(f"{len(valid)} / 3 accounts current" + (" · Some data unavailable" if len(valid) < 3 else ""))
        self.status_icon.set("✓" if len(valid) == 3 else "!")
        self.sync_button.configure(state="normal")

    def _matches(self, label, symbol=None):
        return (self.account_filter.get() == "All accounts" or label == self.account_filter.get()) and (symbol is None or self.market_filter.get() == "All markets" or display_asset(symbol) == self.market_filter.get())

    def _render_portfolio(self):
        if not hasattr(self, "exposure_table"):
            return
        u = self._u
        self._clear_table(self.holdings_table)
        self._clear_table(self.cash_table)
        self._clear_table(self.activity_table)
        self._position_by_item_id = {}
        for row in self._all_rows:
            if self._matches(row.account_label, row.market):
                self._insert_hyperliquid_position(row)
        for snapshot in self._snapshot_by_account.values():
            if self._matches(snapshot.account_label) and not snapshot.account_facts.get("sync_error"):
                self._insert_hyperliquid_cash(snapshot)
        for fill in sorted(self._activity, key=lambda r: u._to_float(r.get("time")) or 0, reverse=True)[:150]:
            coin = self._display_coin(str(fill.get("coin", "")))
            if not self._matches(fill["accountLabel"], coin):
                continue
            try:
                stamp = datetime.fromtimestamp(float(fill.get("time", 0)) / 1000).strftime("%m/%d %H:%M")
            except (ValueError, TypeError, OSError, OverflowError):
                stamp = "—"
            fee = u._to_float(fill.get("fee"))
            fee_text = f"{fee:g} {fill.get('feeToken', 'USDC')}" if fee is not None else "—"
            self.activity_table.insert("", "end", values=(stamp, fill["accountLabel"], coin, fill.get("dir", fill.get("side", "")), fill.get("sz", ""), u._money_or_dash(u._to_float(fill.get("px"))), u._money_or_dash(u._to_float(fill.get("closedPnl"))), fee_text))
        exposure_rows = []
        for coin in self.watchlist:
            rows = [r for r in self._all_rows if display_asset(r.market) == coin and self._matches(r.account_label)]
            long = sum(r.holding.quantity for r in rows if r.side == "Long")
            short = sum(r.holding.quantity for r in rows if r.side == "Short")
            exposure_rows.append((coin, long, short, bool(rows)))
        self.exposure_table.set_rows(exposure_rows, self.account_filter.get())
        self._render_open_orders()
        self._portfolio_hint()

    def _portfolio_hint(self):
        view = self.portfolio_view.get()
        table = {"Positions": self.holdings_table, "Cash": self.cash_table, "Open Orders": self.portfolio_orders_table, "Activity": self.activity_table}[view]
        count = len(table.get_children())
        errors = [s for s in self._snapshot_by_account.values() if s.account_facts.get("sync_error") and self._matches(s.account_label)]
        suffix = " · Account data unavailable" if errors else ""
        if view == "Activity":
            activity_error = any(s.account_facts.get("activity_error") for s in self._snapshot_by_account.values() if self._matches(s.account_label))
            suffix += " · Fill history unavailable for some accounts" if activity_error else " · Recent fills"
        if view == "Open Orders" and any(self._order_errors.get(k) for k, p in HYPERLIQUID_ACCOUNT_PROFILES.items() if self._matches(p.label)):
            suffix += " · Order data unavailable for some accounts"
        self.portfolio_message.set(f"{count} {view.lower()}" + suffix if count or suffix else f"No {view.lower()} for this selection.")

    def _display_coin(self, coin):
        return next((symbol + "/USDC" for symbol, route in self._spots.items() if route["coin"] == coin), coin)

    def _annotate_orders(self, key, rows):
        return [{**r, "accountKey": key, "accountLabel": HYPERLIQUID_ACCOUNT_PROFILES[key].label} for r in rows if isinstance(r, dict)]

    def _render_open_orders(self):
        if self.hyperliquid_open_orders_table is None:
            return
        u = self._u
        for table in (self.hyperliquid_open_orders_table, self.portfolio_orders_table):
            self._clear_table(table)
        self.selected_hyperliquid_order_key = ""
        self.hyperliquid_open_order_by_lookup_key = {}
        selected_account = u.hyperliquid_account_key(self.composer_account.get())
        for key, orders in self._orders_by_account.items():
            for order in orders:
                lookup = u._hyperliquid_open_order_lookup_key(order)
                coin = self._display_coin(str(order.get("coin", "")))
                values = (order["accountLabel"], coin, u._hyperliquid_order_kind(order).title(), u._hyperliquid_side_label(order), order.get("sz", ""), order.get("limitPx", ""), u._hyperliquid_order_type_label(order), "Yes" if order.get("reduceOnly") else "No", order.get("oid", ""))
                self.hyperliquid_open_order_by_lookup_key[lookup] = order
                if key == selected_account:
                    self.hyperliquid_open_orders_table.insert("", "end", iid=lookup, values=values)
                if self._matches(order["accountLabel"], coin):
                    self.portfolio_orders_table.insert("", "end", iid=lookup, values=values)
        count = len(self.hyperliquid_open_orders_table.get_children())
        error = self._order_errors.get(selected_account)
        message = "Order data unavailable" if error else "Sync to load open orders" if selected_account not in self._orders_by_account else f"No open orders for {self.composer_account.get()}"
        self.orders_empty_panel.set_message("Loading open orders…" if self._orders_loading else message, "Checking account orders." if self._orders_loading else "Refresh to try again." if error else "Your open orders will appear here.")
        if count:
            self.orders_table_frame.tkraise()
            self.orders_actions.pack(fill="x", pady=(8, 0))
            self.orders_status_label.pack(anchor="w", pady=(6, 0))
        else:
            self.orders_empty_panel.tkraise()
            self.orders_actions.pack_forget()
            self.orders_status_label.pack_forget()
        self.orders_status.set("Loading open orders…" if self._orders_loading else ("Orders unavailable · Refresh to retry" if error else f"{count} open orders · {self.composer_account.get()}"))
        self._update_order_actions()

    def _update_order_actions(self):
        order = self.hyperliquid_open_order_by_lookup_key.get(self.selected_hyperliquid_order_key)
        for name in ("edit_order_button", "cancel_order_button"):
            button = getattr(self, name, None)
            if button:
                editable = order and not order.get("isTrigger") and self._u._hyperliquid_order_type_label(order) == "Limit"
                button.configure(state="normal" if order and (name == "cancel_order_button" or editable) else "disabled")

    def _load_hyperliquid_open_orders(self):
        if self._orders_loading:
            return
        self._orders_loading = True
        self.orders_status.set("Loading open orders…")
        self.orders_refresh_button.configure(state="disabled")
        if not self.hyperliquid_open_orders_table.get_children():
            self.orders_empty_panel.set_message("Loading open orders…", "Checking account orders.")
        def fetch():
            results, errors = {}, {}
            for key in HYPERLIQUID_ACCOUNT_PROFILES:
                try:
                    results[key] = self._annotate_orders(key, self._u.HyperliquidExecutionAdapter(key).open_orders())
                except Exception as exc:
                    errors[key] = type(exc).__name__
            self.root.after(0, lambda: finish(results, errors))
        def finish(results, errors):
            self._orders_loading = False
            self.orders_refresh_button.configure(state="normal")
            self._orders_by_account = results
            self._order_errors = errors
            self._render_open_orders()
            self._portfolio_hint()
        threading.Thread(target=fetch, daemon=True).start()

    def _use_selected_hyperliquid_holding(self, _event=None):
        selected = self.holdings_table.selection()
        row = self._position_by_item_id.get(selected[0]) if selected else None
        if row:
            self.composer_account.set(row.account_label)
            self.composer_product.set(row.kind)
            self.composer_market.set(display_asset(row.market))

    def _use_selected_hyperliquid_order(self, event=None):
        table = getattr(event, "widget", self.hyperliquid_open_orders_table)
        selected = table.selection()
        order = self.hyperliquid_open_order_by_lookup_key.get(selected[0]) if selected else None
        if order:
            lookup = selected[0]
            self.composer_account.set(order["accountLabel"])
            self.composer_product.set(self._u._hyperliquid_order_kind(order).title())
            self.composer_market.set(display_asset(self._display_coin(str(order["coin"]))))
            self.selected_hyperliquid_order_key = lookup
            self._update_order_actions()

    def _show_error(self, exc):
        self.sync_button.configure(state="normal")
        self.sync_status.set("Sync failed · displayed data is stale")
        for facts in self._market_facts.values():
            facts["status"] = "stale"
        for snapshot in self._snapshot_by_account.values():
            snapshot.account_facts["sync_error"] = "Latest sync failed"
        for key in self.account_cards:
            self._account_notes[key].set("Stale · Sync failed")
        self._render_markets()
        self._refresh_composer_status()
        messagebox.showerror("Hyperliquid Sync Failed", f"{type(exc).__name__}: {exc}")

    def _style_workspace_controls(self, parent):
        styles = {"TButton": "Hyper.TButton", "TCombobox": "Hyper.TCombobox", "TEntry": "Hyper.TEntry", "TCheckbutton": "Hyper.TCheckbutton"}
        for child in parent.winfo_children():
            kind = child.winfo_class()
            if kind in styles and not child.cget("style"):
                child.configure(style=styles[kind])
                if kind in {"TCombobox", "TEntry"}:
                    child.configure(font=("Segoe UI", 10))
            self._style_workspace_controls(child)

    def _sync(self):
        self.sync_status.set("Syncing accounts and markets…")
        super()._sync()

    def _resize_hyperliquid_body(self, event):
        self._canvas_viewport_height = max(event.height, 1)
        self.canvas.itemconfigure(self._body_window, width=max(event.width, 1))
        self._apply_hyperliquid_layout(event.width)
        # Windows already applies display scaling; this is a modest adjustment
        # for additional logical space, not another multiplication by DPI.
        scale = max(1.0, min(1.25, round(min(event.width / 1800, event.height / 1030) * 20) / 20))
        self._apply_order_density(scale)
        self.root.after_idle(self._sync_hyperliquid_body_height)

    def _scroll_workspace(self, event):
        widget = event.widget
        ancestor = widget
        while ancestor is not None and ancestor is not self.canvas:
            ancestor = getattr(ancestor, "master", None)
        if ancestor is None:
            return
        if isinstance(widget, ttk.Treeview):
            first, last = widget.yview()
            if last - first < .999:
                return
        if event.delta:
            self.canvas.yview_scroll((-1 if event.delta > 0 else 1) * max(1, int(abs(event.delta) / 120)), "units")
            return "break"

    def _apply_hyperliquid_layout(self, width, *, force=False):
        layout = "wide" if width >= 1450 else "medium" if width >= 900 else "narrow"
        if layout == self._last_hyperliquid_layout and not force:
            return
        self._last_hyperliquid_layout = layout
        for widget in (self.accounts_rail, self.center, self.composer_card):
            widget.grid_forget()
        for column in range(3):
            self.workspace.columnconfigure(column, weight=0, minsize=0)
        if layout == "wide":
            for column, (weight, minimum) in enumerate(((19, 260), (54, 670), (27, 335))):
                self.workspace.columnconfigure(column, weight=weight, minsize=minimum)
            self.accounts_rail.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
            self.center.grid(row=0, column=1, sticky="nsew", padx=(0, 10))
            self.composer_card.grid(row=0, column=2, sticky="nsew")
        elif layout == "medium":
            self.workspace.columnconfigure(0, minsize=260)
            self.workspace.columnconfigure(1, weight=3)
            self.accounts_rail.grid(row=0, column=0, sticky="new", padx=(0, 10))
            self.center.grid(row=0, column=1, sticky="nsew")
            self.composer_card.grid(row=1, column=1, sticky="nsew", pady=(10, 0))
        else:
            self.workspace.columnconfigure(0, weight=1)
            self.accounts_rail.grid(row=0, column=0, sticky="ew")
            self.center.grid(row=1, column=0, sticky="nsew", pady=10)
            self.composer_card.grid(row=2, column=0, sticky="ew")
        self.root.after_idle(self._sync_hyperliquid_body_height)

    def _sync_hyperliquid_body_height(self):
        if not self.body.winfo_exists():
            return
        height = max(self.body.winfo_reqheight(), self._canvas_viewport_height)
        if abs(float(self.canvas.itemcget(self._body_window, "height")) - height) > 1:
            self.canvas.itemconfigure(self._body_window, height=height)
        self._update_hyperliquid_scroll_region()

    def _update_hyperliquid_scroll_region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
