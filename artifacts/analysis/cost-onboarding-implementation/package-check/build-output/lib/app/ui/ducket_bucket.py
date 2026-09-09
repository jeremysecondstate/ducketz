from __future__ import annotations

import math
import threading
import tkinter as tk
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from app.models.portfolio import PortfolioSnapshot
from app.services.aggregate import DucketBucketSnapshot
from app.ui.options_strategies import OptionsStrategiesTab
from app.ui.rolling_forecasts import RollingForecastTab
from app.ui.schwab_duckets import SchwabDucketsTab
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

HYPERLIQUID_TIF_CHOICES = ("Gtc", "Ioc", "Alo")
HYPERLIQUID_SPOT_SIZE_UNITS = ("USDC", "BASE")
HYPERLIQUID_ASSET_DIR = Path(__file__).with_name("assets") / "hyperliquid"
HYPERLIQUID_ASSET_FILENAMES = {
    "jeremy": "jeremy.png",
    "alex": "alex.png",
    "hype": "hype.png",
}
HYPERLIQUID_ACCOUNT_CHOICES = ("Jeremy (JE)", "Alex (AL)")
HYPE_ACCENT = "#78edc1"
HYPE_ACCENT_DARK = "#123f3a"
HYPERLIQUID_POSITION_ROW_HEIGHT = 38
HYPERLIQUID_POSITION_ICON_SIZE = 28
HYPERLIQUID_POSITION_ICON_COLUMN_WIDTH = 52


@dataclass(frozen=True)
class HyperliquidAccountSummary:
    equity: float
    spot_equity: float
    perp_equity: float
    available: float | None
    unrealized_pnl: float | None
    margin_used: float | None


@dataclass(frozen=True)
class HyperliquidPositionView:
    identity: str
    account_key: str
    account_label: str
    holding: object
    entry_price: float | None
    liquidation_price: float | None
    margin_mode: str
    leverage: float | None
    signed_size: float | None
    return_on_equity: float | None

    @property
    def market(self) -> str:
        return str(getattr(self.holding, "symbol", "")).strip().upper()

    @property
    def kind(self) -> str:
        bucket = str(getattr(self.holding, "bucket", "")).strip().upper()
        return "Spot" if bucket == "SPOT" else "Perp"

    @property
    def side(self) -> str:
        if self.kind == "Spot":
            return "Long"
        if self.signed_size is not None:
            return "Short" if self.signed_size < 0 else "Long"
        return "Short" if self.market.endswith("-SHORT") else "Long"

    @property
    def pnl_percent(self) -> float | None:
        if self.return_on_equity is not None:
            return self.return_on_equity * 100.0
        pnl = _to_float(getattr(self.holding, "unrealized_pnl", None))
        value = _to_float(getattr(self.holding, "value", None))
        if pnl is None or value is None or value <= 0:
            return None
        return (pnl / value) * 100.0


def hyperliquid_asset_path(
    asset_key: str,
    *,
    asset_root: Path = HYPERLIQUID_ASSET_DIR,
) -> Path | None:
    filename = HYPERLIQUID_ASSET_FILENAMES.get(asset_key.strip().lower())
    if filename is None:
        return None
    candidate = asset_root / filename
    return candidate if candidate.is_file() else None


def hyperliquid_account_key(label: str) -> str:
    normalized = label.strip().casefold()
    if "jeremy" in normalized or normalized.startswith("je"):
        return "jeremy"
    if "alex" in normalized or normalized.startswith("al"):
        return "alex"
    return normalized


def hyperliquid_account_summary(snapshot: PortfolioSnapshot) -> HyperliquidAccountSummary:
    facts = snapshot.account_facts if isinstance(snapshot.account_facts, Mapping) else {}
    calculated_spot = sum(
        balance.value
        for balance in snapshot.cash
        if balance.bucket.strip().upper() == "SPOT"
    ) + sum(
        holding.value
        for holding in snapshot.holdings
        if holding.bucket.strip().upper() == "SPOT"
    )
    spot_equity = _mapping_float(facts, "spot_equity")
    if spot_equity is None:
        spot_equity = calculated_spot
    perp_equity = _mapping_float(facts, "perp_equity")
    if perp_equity is None:
        perp_equity = snapshot.total_value - spot_equity
    return HyperliquidAccountSummary(
        equity=round(spot_equity + perp_equity, 2),
        spot_equity=round(spot_equity, 2),
        perp_equity=round(perp_equity, 2),
        available=_mapping_float(facts, "available"),
        unrealized_pnl=snapshot.unrealized_pnl,
        margin_used=_mapping_float(facts, "margin_used"),
    )


def hyperliquid_position_views(
    snapshots: Sequence[PortfolioSnapshot],
) -> tuple[HyperliquidPositionView, ...]:
    rows: list[HyperliquidPositionView] = []
    for snapshot_index, snapshot in enumerate(snapshots):
        facts = snapshot.account_facts if isinstance(snapshot.account_facts, Mapping) else {}
        positions = facts.get("positions")
        position_facts = positions if isinstance(positions, Mapping) else {}
        account_key = hyperliquid_account_key(snapshot.account_label)
        for holding_index, holding in enumerate(snapshot.holdings):
            market = _hyperliquid_display_symbol(holding.symbol)
            raw = (
                position_facts.get(market)
                if holding.bucket.strip().upper() == "PERPS"
                else None
            )
            details = raw if isinstance(raw, Mapping) else {}
            rows.append(
                HyperliquidPositionView(
                    identity=f"{snapshot_index}:{holding_index}",
                    account_key=account_key,
                    account_label=snapshot.account_label,
                    holding=holding,
                    entry_price=_mapping_float(details, "entry_price"),
                    liquidation_price=_mapping_float(details, "liquidation_price"),
                    margin_mode=str(details.get("margin_mode") or "").strip(),
                    leverage=_mapping_float(details, "leverage"),
                    signed_size=_mapping_float(details, "signed_size"),
                    return_on_equity=_mapping_float(details, "return_on_equity"),
                )
            )
    return tuple(rows)


def _mapping_float(values: Mapping[object, object], key: object) -> float | None:
    return _to_float(values.get(key))


class _HyperliquidAssetMark(tk.Canvas):
    def __init__(self, parent: tk.Misc, asset_key: str, *, size: int = 38) -> None:
        super().__init__(
            parent,
            width=size,
            height=size,
            background=SURFACE,
            highlightthickness=0,
            bd=0,
        )
        self._asset_key = asset_key.strip().lower()
        self._size = size
        self._photo: tk.PhotoImage | None = None
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        path = hyperliquid_asset_path(self._asset_key)
        if path is not None:
            try:
                source = tk.PhotoImage(master=self, file=str(path))
            except tk.TclError:
                source = None
            if source is not None:
                factor = max(
                    1,
                    math.ceil(max(source.width(), source.height()) / max(self._size - 4, 1)),
                )
                self._photo = source.subsample(factor, factor)
                self.create_image(self._size / 2, self._size / 2, image=self._photo)
                return

        self.create_oval(
            2,
            2,
            self._size - 2,
            self._size - 2,
            fill=HYPE_ACCENT_DARK,
            outline="#2b7664",
        )
        if self._asset_key == "hype":
            mid = self._size / 2
            self.create_line(
                self._size * 0.23,
                mid,
                self._size * 0.36,
                self._size * 0.39,
                self._size * 0.49,
                self._size * 0.61,
                self._size * 0.63,
                self._size * 0.39,
                self._size * 0.77,
                mid,
                fill=HYPE_ACCENT,
                width=max(3, self._size // 10),
                smooth=True,
                capstyle=tk.ROUND,
                joinstyle=tk.ROUND,
            )
            return
        initials = "JE" if self._asset_key == "jeremy" else "AL" if self._asset_key == "alex" else "--"
        self.create_text(
            self._size / 2,
            self._size / 2,
            text=initials,
            fill=HYPE_ACCENT,
            font=("Segoe UI", max(8, self._size // 4), "bold"),
        )


class _HyperliquidSparkline(tk.Canvas):
    def __init__(self, parent: tk.Misc, *, height: int = 74) -> None:
        super().__init__(
            parent,
            height=height,
            background=SURFACE,
            highlightthickness=0,
            bd=0,
        )
        self._values: tuple[float, ...] = ()
        self.bind("<Configure>", self._redraw)

    def set_values(self, values: Sequence[object]) -> None:
        self._values = tuple(
            number
            for value in values
            if (number := _to_float(value)) is not None and math.isfinite(number)
        )
        self._redraw()

    def _redraw(self, _event: object | None = None) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 2)
        height = max(self.winfo_height(), 2)
        if len(self._values) < 2:
            self.create_text(
                width / 2,
                height / 2,
                text="24H chart unavailable",
                fill=MUTED_TEXT,
                font=("Segoe UI", 8),
            )
            return
        low = min(self._values)
        high = max(self._values)
        spread = high - low
        if spread <= 0:
            spread = max(abs(high) * 0.01, 1.0)
            low -= spread / 2
        pad_x = 3
        pad_y = 7
        points: list[float] = []
        for index, value in enumerate(self._values):
            x = pad_x + ((width - (pad_x * 2)) * index / (len(self._values) - 1))
            y = height - pad_y - ((height - (pad_y * 2)) * (value - low) / spread)
            points.extend((x, y))
        self.create_line(*points, fill=HYPE_ACCENT, width=2, smooth=True)
        self.create_oval(
            points[-2] - 2,
            points[-1] - 2,
            points[-2] + 2,
            points[-1] + 2,
            fill=HYPE_ACCENT,
            outline="",
        )


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

        self.root.option_add("*TCombobox*Listbox.background", FIELD_BACKGROUND)
        self.root.option_add("*TCombobox*Listbox.foreground", FIELD_TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", TEXT)

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
        style.configure(
            "TButton",
            background=SURFACE_ALT,
            foreground=TEXT,
            bordercolor=BORDER,
            darkcolor=BORDER,
            lightcolor=BORDER,
            focusthickness=1,
            padding=(10, 6),
        )
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
            bordercolor=BORDER,
            darkcolor=BORDER,
            lightcolor=BORDER,
            padding=5,
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", SURFACE_ALT), ("readonly", FIELD_BACKGROUND)],
            foreground=[("disabled", MUTED_TEXT), ("readonly", FIELD_TEXT)],
            bordercolor=[("focus", ACCENT)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=FIELD_BACKGROUND,
            background=FIELD_BACKGROUND,
            foreground=FIELD_TEXT,
            arrowcolor=FIELD_TEXT,
            selectbackground=FIELD_BACKGROUND,
            selectforeground=FIELD_TEXT,
            bordercolor=BORDER,
            darkcolor=BORDER,
            lightcolor=BORDER,
            padding=5,
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
            bordercolor=[("focus", ACCENT), ("active", ACCENT)],
            arrowcolor=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "TScrollbar",
            background=SURFACE_ALT,
            troughcolor=TABLE_FIELD,
            bordercolor=BACKGROUND,
            darkcolor=SURFACE_ALT,
            lightcolor=SURFACE_ALT,
            arrowcolor=MUTED_TEXT,
        )

    def _build_layout(self) -> None:
        notebook = ttk.Notebook(self.root)
        notebook.pack(fill=tk.BOTH, expand=True)

        forecasts_frame = ttk.Frame(notebook)
        strategies_frame = ttk.Frame(notebook)
        schwab_frame = ttk.Frame(notebook)
        hyperliquid_frame = ttk.Frame(notebook)

        notebook.add(forecasts_frame, text="Rolling Forecasts")
        notebook.add(strategies_frame, text="Options Strategies")
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

        summary_rows = self._summary_rows()
        for row_index, label_vars in enumerate(summary_rows):
            summary_row = ttk.Frame(summary)
            summary_row.pack(
                fill=tk.X,
                pady=(0, 8 if row_index < len(summary_rows) - 1 else 0),
            )
            for label_var in label_vars:
                card = ttk.LabelFrame(summary_row, text="", style="Summary.TLabelframe")
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

    def _summary_rows(self) -> tuple[tuple[tk.StringVar, ...], ...]:
        return ((
            self.cash_value,
            self.holdings_value,
            self.total_value,
            self.unrealized_pnl,
            self.day_pnl,
        ),)

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

        messagebox.showerror("Sync Failed", f"{type(exc).__name__}: {exc}")

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


class HyperliquidDucketsTab(DucketsTab):
    def __init__(self, root: tk.Tk, parent: ttk.Frame) -> None:
        self.spot_account = tk.StringVar(master=root, value=HYPERLIQUID_ACCOUNT_CHOICES[0])
        self.spot_market = tk.StringVar(master=root, value="HYPE")
        self.spot_side = tk.StringVar(master=root, value="buy")
        self.spot_order_type = tk.StringVar(master=root, value="limit")
        self.spot_quantity = tk.StringVar(master=root)
        self.spot_size_unit = tk.StringVar(master=root, value="USDC")
        self.spot_entry_limit = tk.StringVar(master=root)
        self.spot_tif = tk.StringVar(master=root, value="Gtc")
        self.spot_size_status = tk.StringVar(
            master=root,
            value="Sync Hyperliquid, then choose a size percentage.",
        )

        self.perp_account = tk.StringVar(master=root, value=HYPERLIQUID_ACCOUNT_CHOICES[0])
        self.perp_coin = tk.StringVar(master=root, value="HYPE")
        self.perp_direction = tk.StringVar(master=root, value="buy")
        self.perp_order_type = tk.StringVar(master=root, value="limit")
        self.perp_size = tk.StringVar(master=root)
        self.perp_entry_limit = tk.StringVar(master=root)
        self.perp_tif = tk.StringVar(master=root, value="Gtc")
        self.perp_reduce_only = tk.BooleanVar(master=root, value=False)

        self.sync_status = tk.StringVar(master=root, value="Ready for explicit sync")
        self.last_sync = tk.StringVar(master=root, value="Last synced: --")
        self.portfolio_view = tk.StringVar(master=root, value="Positions")
        self.orders_status = tk.StringVar(master=root, value="Choose Refresh to load open orders.")
        self.hype_price = tk.StringVar(master=root, value="--")
        self.hype_change = tk.StringVar(master=root, value="24H --")
        self.hype_volume = tk.StringVar(master=root, value="--")
        self.hype_supply = tk.StringVar(master=root, value="--")
        self.hype_status = tk.StringVar(master=root, value="Market data unavailable")
        self.chain_id = tk.StringVar(master=root, value="--")
        self.chain_block = tk.StringVar(master=root, value="--")
        self.chain_base_fee = tk.StringVar(master=root, value="--")
        self.chain_health = tk.StringVar(master=root, value="Unavailable")

        self.account_summary_values = {
            account_key: {
                metric: tk.StringVar(master=root, value="--")
                for metric in ("equity", "spot", "perps", "available", "pnl", "margin")
            }
            for account_key in ("jeremy", "alex")
        }
        self.account_pnl_labels: dict[str, tk.Label] = {}
        self.hyperliquid_open_order_by_lookup_key: dict[str, dict[str, object]] = {}
        self.selected_hyperliquid_order_key = ""
        self._position_by_item_id: dict[str, HyperliquidPositionView] = {}
        self._row_icons: dict[str, tk.PhotoImage] = {}
        self._canvas_hovered = False
        self._canvas_viewport_height = 1
        self._last_hyperliquid_layout: str | None = None
        self.latest_hyperliquid_bucket: DucketBucketSnapshot | None = None
        self.hyperliquid_open_orders_table: ttk.Treeview | None = None
        self.portfolio_orders_table: ttk.Treeview | None = None
        self.hype_sparkline: _HyperliquidSparkline | None = None
        self.hype_change_label: tk.Label | None = None
        self.chain_health_label: tk.Label | None = None

        super().__init__(
            root=root,
            parent=parent,
            title="Hyperliquid Duckets",
            sync_button_text="Sync Hyperliquid",
            sync_snapshots=sync_hyperliquid_portfolios,
        )
        self.status_icon.set("○")

    def _build(self, parent: ttk.Frame, title: str, sync_button_text: str) -> None:
        del title, sync_button_text
        self._apply_hyperliquid_styles()
        self.canvas = tk.Canvas(
            parent,
            background=BACKGROUND,
            highlightthickness=0,
            bd=0,
        )
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.body = ttk.Frame(
            self.canvas,
            style="HyperPage.TFrame",
            padding=(14, 12, 14, 16),
        )
        self._body_window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.columnconfigure(0, weight=1)

        self._build_hyperliquid_header()
        self._build_overview_cards()
        self._build_portfolio_workspace()
        self._build_chain_strip()
        self._build_trading_workspace()

        self.body.bind("<Configure>", self._update_hyperliquid_scroll_region)
        self.canvas.bind("<Configure>", self._resize_hyperliquid_body)
        self.canvas.bind("<Enter>", lambda _event: self._set_hyperliquid_canvas_hover(True))
        self.canvas.bind("<Leave>", lambda _event: self._set_hyperliquid_canvas_hover(False))
        self.canvas.bind("<MouseWheel>", self._on_hyperliquid_mousewheel)

    def _apply_hyperliquid_styles(self) -> None:
        style = ttk.Style(self.root)
        style.configure("HyperPage.TFrame", background=BACKGROUND)
        style.configure("HyperCard.TFrame", background=SURFACE)
        style.configure("HyperCard.TLabel", background=SURFACE, foreground=TEXT, font=BODY_FONT)
        style.configure(
            "HyperMuted.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=MUTED_LABEL_FONT,
        )
        style.configure(
            "HyperPrimary.TButton",
            background=HYPE_ACCENT,
            foreground="#04231d",
            bordercolor=HYPE_ACCENT,
            padding=(10, 6),
        )
        style.map(
            "HyperPrimary.TButton",
            background=[("active", "#9af6d3"), ("disabled", SURFACE_ALT)],
            foreground=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "HyperDanger.TButton",
            background=SURFACE,
            foreground=DANGER,
            bordercolor=DANGER,
            padding=(9, 5),
        )
        style.map(
            "HyperDanger.TButton",
            background=[("active", "#3a2028"), ("disabled", SURFACE)],
            foreground=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "HyperSide.TRadiobutton",
            background=FIELD_BACKGROUND,
            foreground=TEXT,
            indicatorcolor=FIELD_BACKGROUND,
            bordercolor=BORDER,
            padding=(12, 5),
        )
        style.map(
            "HyperSide.TRadiobutton",
            background=[("selected", HYPE_ACCENT_DARK), ("active", SURFACE_ALT)],
            foreground=[("selected", HYPE_ACCENT), ("active", TEXT)],
        )
        style.configure(
            "HyperTab.TRadiobutton",
            background=SURFACE,
            foreground=MUTED_TEXT,
            indicatorcolor=SURFACE,
            bordercolor=SURFACE,
            padding=(12, 6),
        )
        style.map(
            "HyperTab.TRadiobutton",
            background=[("selected", SURFACE_ALT), ("active", SURFACE_ALT)],
            foreground=[("selected", HYPE_ACCENT), ("active", TEXT)],
        )
        style.configure(
            "HyperPortfolio.Treeview",
            background=TABLE_FIELD,
            foreground=TEXT,
            fieldbackground=TABLE_FIELD,
            bordercolor=BORDER,
            lightcolor=TABLE_FIELD,
            darkcolor=TABLE_FIELD,
            rowheight=HYPERLIQUID_POSITION_ROW_HEIGHT,
            font=("Segoe UI", 9),
        )
        style.configure(
            "HyperPortfolio.Treeview.Heading",
            background=SURFACE_ALT,
            foreground=MUTED_TEXT,
            bordercolor=BORDER,
            padding=(5, 6),
            font=("Segoe UI", 8, "bold"),
        )
        style.map(
            "HyperPortfolio.Treeview",
            background=[("selected", "#174b4a")],
            foreground=[("selected", TEXT)],
        )

    def _build_hyperliquid_header(self) -> None:
        header = ttk.Frame(self.body, style="HyperPage.TFrame")
        self.header_frame = header
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)

        title_group = ttk.Frame(header, style="HyperPage.TFrame")
        title_group.grid(row=0, column=0, sticky="w")
        title_row = ttk.Frame(title_group, style="HyperPage.TFrame")
        title_row.pack(anchor=tk.W)
        ttk.Label(
            title_row,
            text="Hyperliquid Duckets",
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 18, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            title_row,
            text="●  READ-ONLY CHAIN DATA",
            background="#0d2e2c",
            foreground=HYPE_ACCENT,
            font=("Segoe UI", 8, "bold"),
            padx=9,
            pady=4,
        ).pack(side=tk.LEFT, padx=(12, 0))
        status_row = ttk.Frame(title_group, style="HyperPage.TFrame")
        status_row.pack(anchor=tk.W, pady=(3, 0))
        ttk.Label(
            status_row,
            textvariable=self.status_icon,
            background=BACKGROUND,
            foreground=HYPE_ACCENT,
            font=("Segoe UI", 8, "bold"),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Label(
            status_row,
            textvariable=self.sync_status,
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT)
        ttk.Label(
            status_row,
            text="  •  ",
            background=BACKGROUND,
            foreground=BORDER,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT)
        ttk.Label(
            status_row,
            textvariable=self.last_sync,
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT)

        self.sync_button = ttk.Button(
            header,
            text="↻  Sync Hyperliquid",
            command=self._sync,
        )
        self.sync_button.grid(row=0, column=1, sticky="e")

    def _build_overview_cards(self) -> None:
        overview = ttk.Frame(self.body, style="HyperPage.TFrame")
        self.overview_frame = overview
        overview.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for column, weight in enumerate((1, 1, 1)):
            overview.columnconfigure(column, weight=weight, uniform="hyper-overview")
        self.account_cards = {
            "jeremy": self._build_account_card(overview, "jeremy", "Jeremy", 0),
            "alex": self._build_account_card(overview, "alex", "Alex", 1),
        }
        self.hype_pulse_card = self._build_hype_pulse_card(overview, 2)

    def _build_account_card(
        self,
        parent: ttk.Frame,
        account_key: str,
        account_label: str,
        column: int,
    ) -> tk.Frame:
        card = self._hyper_card(parent, padding=(12, 5))
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 5 if column < 2 else 0))
        header = ttk.Frame(card, style="HyperCard.TFrame")
        header.pack(fill=tk.X, pady=(0, 8))
        _HyperliquidAssetMark(header, account_key, size=40).pack(side=tk.LEFT, padx=(0, 12))
        copy = ttk.Frame(header, style="HyperCard.TFrame")
        copy.pack(side=tk.LEFT)
        ttk.Label(
            copy,
            text=account_label,
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            copy,
            text="Portfolio account",
            style="HyperMuted.TLabel",
        ).pack(anchor=tk.W)

        metrics = ttk.Frame(card, style="HyperCard.TFrame")
        metrics.pack(fill=tk.X)
        specs = (
            ("equity", "Equity"),
            ("spot", "Spot"),
            ("perps", "Perps"),
            ("available", "Available"),
            ("pnl", "Unrealized P/L"),
            ("margin", "Margin Used"),
        )
        for index, (key, label) in enumerate(specs):
            row, metric_column = divmod(index, 3)
            metrics.columnconfigure(metric_column, weight=1, uniform=f"{account_key}-metrics")
            cell = ttk.Frame(metrics, style="HyperCard.TFrame")
            cell.grid(
                row=row,
                column=metric_column,
                sticky="ew",
                padx=(0 if metric_column == 0 else 8, 0),
                pady=(0 if row == 0 else 5, 1),
            )
            ttk.Label(cell, text=label, style="HyperMuted.TLabel").pack(anchor=tk.W)
            value = tk.Label(
                cell,
                textvariable=self.account_summary_values[account_key][key],
                background=SURFACE,
                foreground=TEXT,
                font=("Segoe UI", 9, "bold"),
            )
            value.pack(anchor=tk.W, pady=(2, 0))
            if key == "pnl":
                self.account_pnl_labels[account_key] = value
        return card

    def _build_hype_pulse_card(self, parent: ttk.Frame, column: int) -> tk.Frame:
        card = self._hyper_card(parent, padding=(12, 7))
        card.grid(row=0, column=column, sticky="nsew", padx=(5, 0))
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=2)

        left = ttk.Frame(card, style="HyperCard.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        header = ttk.Frame(left, style="HyperCard.TFrame")
        header.pack(fill=tk.X, pady=(0, 4))
        _HyperliquidAssetMark(header, "hype", size=32).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(
            header,
            text="HYPE Pulse",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 10, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            left,
            textvariable=self.hype_price,
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor=tk.W, pady=(2, 0))
        self.hype_change_label = tk.Label(
            left,
            textvariable=self.hype_change,
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 9, "bold"),
        )
        self.hype_change_label.pack(anchor=tk.W)
        ttk.Label(left, text="24H volume", style="HyperMuted.TLabel").pack(anchor=tk.W, pady=(3, 0))
        ttk.Label(left, textvariable=self.hype_volume, style="HyperCard.TLabel").pack(anchor=tk.W)
        ttk.Label(left, text="Circulating supply", style="HyperMuted.TLabel").pack(anchor=tk.W, pady=(2, 0))
        ttk.Label(left, textvariable=self.hype_supply, style="HyperCard.TLabel").pack(anchor=tk.W)

        chart = ttk.Frame(card, style="HyperCard.TFrame")
        chart.grid(row=0, column=1, sticky="nsew")
        chart.columnconfigure(0, weight=1)
        chart.rowconfigure(1, weight=1)
        chart_header = ttk.Frame(chart, style="HyperCard.TFrame")
        chart_header.grid(row=0, column=0, sticky="ew")
        ttk.Label(chart_header, text="24H", style="HyperMuted.TLabel").pack(side=tk.LEFT)
        ttk.Label(chart_header, textvariable=self.hype_status, style="HyperMuted.TLabel").pack(side=tk.RIGHT)
        self.hype_sparkline = _HyperliquidSparkline(chart, height=52)
        self.hype_sparkline.grid(row=1, column=0, sticky="nsew", pady=(3, 0))
        return card

    def _build_portfolio_workspace(self) -> None:
        card = self._hyper_card(self.body, padding=(0, 0))
        self.portfolio_card = card
        card.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)
        tabs = ttk.Frame(card, style="HyperCard.TFrame")
        tabs.grid(row=0, column=0, sticky="ew", padx=8, pady=(5, 2))
        for label in ("Positions", "Cash", "Open Orders"):
            ttk.Radiobutton(
                tabs,
                text=label,
                value=label,
                variable=self.portfolio_view,
                command=self._switch_hyperliquid_portfolio_view,
                style="HyperTab.TRadiobutton",
            ).pack(side=tk.LEFT, padx=(0, 3))

        self.portfolio_stack = ttk.Frame(card, style="HyperCard.TFrame")
        self.portfolio_stack.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 7))
        self.portfolio_stack.columnconfigure(0, weight=1)
        self.portfolio_stack.rowconfigure(0, weight=1)
        self.positions_view = ttk.Frame(self.portfolio_stack, style="HyperCard.TFrame")
        self.cash_view = ttk.Frame(self.portfolio_stack, style="HyperCard.TFrame")
        self.portfolio_orders_view = ttk.Frame(self.portfolio_stack, style="HyperCard.TFrame")
        for view in (self.positions_view, self.cash_view, self.portfolio_orders_view):
            view.grid(row=0, column=0, sticky="nsew")
            view.columnconfigure(0, weight=1)
            view.rowconfigure(0, weight=1)

        position_columns = (
            "account",
            "market",
            "type",
            "side",
            "size",
            "entry",
            "mark",
            "value",
            "pnl",
            "pnl_pct",
            "liquidation",
        )
        self.holdings_table = ttk.Treeview(
            self.positions_view,
            columns=position_columns,
            show="tree headings",
            height=5,
            style="HyperPortfolio.Treeview",
            selectmode="browse",
        )
        self.holdings_table.heading("#0", text="")
        self.holdings_table.column(
            "#0",
            width=HYPERLIQUID_POSITION_ICON_COLUMN_WIDTH,
            minwidth=HYPERLIQUID_POSITION_ICON_COLUMN_WIDTH,
            stretch=False,
        )
        for column_name, label, width, anchor in (
            ("account", "Account", 90, tk.W),
            ("market", "Market", 130, tk.W),
            ("type", "Type", 75, tk.W),
            ("side", "Side", 65, tk.W),
            ("size", "Size / Qty", 95, tk.E),
            ("entry", "Entry Price", 90, tk.E),
            ("mark", "Mark Price", 90, tk.E),
            ("value", "Value", 100, tk.E),
            ("pnl", "Unrealized P/L", 110, tk.E),
            ("pnl_pct", "P/L %", 70, tk.E),
            ("liquidation", "Liquidation", 95, tk.E),
        ):
            self._setup_column(self.holdings_table, column_name, label, width, anchor)
        self.holdings_table.tag_configure("pnl_positive", foreground=SUCCESS)
        self.holdings_table.tag_configure("pnl_negative", foreground=DANGER)
        position_y = ttk.Scrollbar(self.positions_view, orient=tk.VERTICAL, command=self.holdings_table.yview)
        self.holdings_table.configure(yscrollcommand=position_y.set)
        self.holdings_table.grid(row=0, column=0, sticky="nsew")
        position_y.grid(row=0, column=1, sticky="ns")
        self.holdings_table.bind("<<TreeviewSelect>>", self._use_selected_hyperliquid_holding)

        self.cash_table = ttk.Treeview(
            self.cash_view,
            columns=("account", "bucket", "symbol", "amount", "value"),
            show="headings",
            height=5,
            style="HyperPortfolio.Treeview",
        )
        for column_name, label, width, anchor in (
            ("account", "Account", 150, tk.W),
            ("bucket", "Bucket", 110, tk.W),
            ("symbol", "Asset", 90, tk.W),
            ("amount", "Amount", 130, tk.E),
            ("value", "Value", 130, tk.E),
        ):
            self._setup_column(self.cash_table, column_name, label, width, anchor)
        cash_y = ttk.Scrollbar(self.cash_view, orient=tk.VERTICAL, command=self.cash_table.yview)
        self.cash_table.configure(yscrollcommand=cash_y.set)
        self.cash_table.grid(row=0, column=0, sticky="nsew")
        cash_y.grid(row=0, column=1, sticky="ns")
        self.portfolio_orders_table = ttk.Treeview(
            self.portfolio_orders_view,
            columns=("account", "market", "kind", "side", "size", "price", "type", "reduce", "oid"),
            show="headings",
            height=5,
            style="HyperPortfolio.Treeview",
            selectmode="browse",
        )
        self._configure_hyperliquid_order_columns(self.portfolio_orders_table)
        portfolio_order_y = ttk.Scrollbar(
            self.portfolio_orders_view,
            orient=tk.VERTICAL,
            command=self.portfolio_orders_table.yview,
        )
        self.portfolio_orders_table.configure(yscrollcommand=portfolio_order_y.set)
        self.portfolio_orders_table.grid(row=0, column=0, sticky="nsew")
        portfolio_order_y.grid(row=0, column=1, sticky="ns")
        self.portfolio_orders_table.bind("<<TreeviewSelect>>", self._use_selected_hyperliquid_order)
        self.portfolio_orders_table.bind("<Double-1>", self._edit_selected_hyperliquid_open_order)
        self._switch_hyperliquid_portfolio_view()

    def _build_chain_strip(self) -> None:
        card = self._hyper_card(self.body, padding=(11, 5))
        self.chain_card = card
        card.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        for column in range(4):
            card.columnconfigure(column, weight=1, uniform="chain")
        self._chain_metric(card, "▥  Chain Status", "HyperEVM", 0)
        self._chain_metric(card, "Chain ID", self.chain_id, 1)
        self._chain_metric(card, "Block", self.chain_block, 2)
        fee_group = ttk.Frame(card, style="HyperCard.TFrame")
        fee_group.grid(row=0, column=3, sticky="ew")
        fee_row = ttk.Frame(fee_group, style="HyperCard.TFrame")
        fee_row.pack(anchor=tk.E)
        ttk.Label(fee_row, text="Base fee  ", style="HyperMuted.TLabel").pack(side=tk.LEFT)
        ttk.Label(fee_row, textvariable=self.chain_base_fee, style="HyperCard.TLabel").pack(side=tk.LEFT)
        self.chain_health_label = tk.Label(
            fee_group,
            textvariable=self.chain_health,
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8, "bold"),
        )
        self.chain_health_label.pack(anchor=tk.E)

    def _chain_metric(
        self,
        parent: tk.Frame,
        label: str,
        value: str | tk.StringVar,
        column: int,
    ) -> None:
        group = ttk.Frame(parent, style="HyperCard.TFrame")
        group.grid(row=0, column=column, sticky="ew")
        ttk.Label(group, text=f"{label}  ", style="HyperMuted.TLabel").pack(side=tk.LEFT)
        options = {"textvariable": value} if isinstance(value, tk.StringVar) else {"text": value}
        ttk.Label(group, style="HyperCard.TLabel", **options).pack(side=tk.LEFT)

    def _build_trading_workspace(self) -> None:
        trading = ttk.Frame(self.body, style="HyperPage.TFrame")
        self.trading_frame = trading
        trading.grid(row=4, column=0, sticky="nsew")
        trading.rowconfigure(0, weight=1)
        for column, weight in enumerate((1, 1, 1)):
            trading.columnconfigure(column, weight=weight, uniform="hyper-trading")
        spot_card = self._hyper_card(trading, padding=(10, 9))
        perp_card = self._hyper_card(trading, padding=(10, 9))
        orders_card = self._hyper_card(trading, padding=(10, 9))
        self.spot_ticket_card = spot_card
        self.perp_ticket_card = perp_card
        self.order_review_card = orders_card
        spot_card.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        perp_card.grid(row=0, column=1, sticky="nsew", padx=5)
        orders_card.grid(row=0, column=2, sticky="nsew", padx=(5, 0))
        self._build_spot_ticket(spot_card)
        self._build_perp_ticket(perp_card)
        self._build_hyperliquid_orders_panel(orders_card)

    def _show_bucket(self, bucket: DucketBucketSnapshot) -> None:
        self.latest_hyperliquid_bucket = bucket
        self._clear_table(self.cash_table)
        self._clear_table(self.holdings_table)
        self._position_by_item_id = {}
        self._row_icons = {}

        snapshots_by_key = {
            hyperliquid_account_key(snapshot.account_label): snapshot
            for snapshot in bucket.snapshots
        }
        for account_key in ("jeremy", "alex"):
            snapshot = snapshots_by_key.get(account_key)
            if snapshot is None:
                self._set_account_summary_unavailable(account_key)
                continue
            self._set_account_summary(account_key, hyperliquid_account_summary(snapshot))
            self._insert_hyperliquid_cash(snapshot)

        rows = hyperliquid_position_views(bucket.snapshots)
        for row in rows:
            self._insert_hyperliquid_position(row)

        common_facts = self._common_hyperliquid_facts(bucket.snapshots)
        self._show_hype_market(common_facts.get("hype_market"))
        self._show_chain_status(common_facts.get("chain_status"))
        synced_times = [snapshot.synced_at for snapshot in bucket.snapshots if snapshot.synced_at is not None]
        latest = max(synced_times) if synced_times else None
        self.last_sync.set(f"Last synced: {_format_local_timestamp(latest)}")
        self.sync_status.set("Current portfolio snapshot")
        self.status_icon.set("✓")
        if self.sync_button is not None:
            self.sync_button.configure(state=tk.NORMAL)

    def _show_error(self, exc: Exception) -> None:
        self.status_icon.set("!")
        self.sync_status.set(f"Sync failed: {type(exc).__name__}")
        if self.sync_button is not None:
            self.sync_button.configure(state=tk.NORMAL)
        messagebox.showerror("Hyperliquid Sync Failed", f"{type(exc).__name__}: {exc}")

    def _sync(self) -> None:
        self.sync_status.set("Syncing portfolio and public chain data…")
        super()._sync()

    def _build_spot_ticket(self, parent: tk.Frame) -> None:
        self._ticket_heading(parent, "Spot Ticket", "LIMIT")
        form = ttk.Frame(parent, style="HyperCard.TFrame")
        form.pack(fill=tk.BOTH, expand=True)
        form.columnconfigure((0, 1), weight=1, uniform="spot-fields")
        self._compact_combo(form, "Account", self.spot_account, HYPERLIQUID_ACCOUNT_CHOICES, 0, 0)
        self._compact_entry(form, "Market", self.spot_market, 0, 1)
        self._compact_side_row(form, "Side", self.spot_side, (("Buy", "buy"), ("Sell", "sell")), 1)
        self._compact_entry(form, "Quantity", self.spot_quantity, 2, 0)
        self._compact_combo(form, "Unit", self.spot_size_unit, HYPERLIQUID_SPOT_SIZE_UNITS, 2, 1)
        self._compact_entry(form, "Limit Price", self.spot_entry_limit, 3, 0)
        self._compact_combo(form, "Time in force", self.spot_tif, HYPERLIQUID_TIF_CHOICES, 3, 1)

        size_row = ttk.Frame(form, style="HyperCard.TFrame")
        size_row.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(7, 3))
        for index, (label, percent) in enumerate((("25%", 25), ("50%", 50), ("75%", 75), ("Max", 100))):
            size_row.columnconfigure(index, weight=1)
            ttk.Button(
                size_row,
                text=label,
                command=lambda pct=percent: self._apply_spot_size_percent(
                    self._account_key_from_choice(self.spot_account.get()), pct
                ),
            ).grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 3, 0))
        size_row.columnconfigure(4, weight=1)
        ttk.Button(size_row, text="Use Mid", command=self._use_spot_mid).grid(
            row=0, column=4, sticky="ew", padx=(3, 0)
        )
        ttk.Label(
            form,
            textvariable=self.spot_size_status,
            style="HyperMuted.TLabel",
            wraplength=320,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(2, 4))
        form.rowconfigure(6, weight=1, minsize=8)
        ttk.Button(
            form,
            text="Review Spot Order",
            command=self._review_spot_order,
            style="HyperPrimary.TButton",
        ).grid(row=7, column=0, columnspan=2, sticky="ew", pady=(2, 0))

    def _build_perp_ticket(self, parent: tk.Frame) -> None:
        self._ticket_heading(parent, "Perp Ticket", "LIMIT")
        form = ttk.Frame(parent, style="HyperCard.TFrame")
        form.pack(fill=tk.BOTH, expand=True)
        form.columnconfigure((0, 1), weight=1, uniform="perp-fields")
        self._compact_combo(form, "Account", self.perp_account, HYPERLIQUID_ACCOUNT_CHOICES, 0, 0)
        self._compact_entry(form, "Market", self.perp_coin, 0, 1)
        self._compact_side_row(form, "Direction", self.perp_direction, (("Long", "buy"), ("Short", "sell")), 1)
        self._compact_entry(form, "Size", self.perp_size, 2, 0)
        self._compact_entry(form, "Limit Price", self.perp_entry_limit, 2, 1)
        self._compact_combo(form, "Time in force", self.perp_tif, HYPERLIQUID_TIF_CHOICES, 3, 0)
        reduce_box = ttk.Frame(form, style="HyperCard.TFrame")
        reduce_box.grid(row=3, column=1, sticky="nsew", padx=(4, 0), pady=(4, 0))
        ttk.Label(reduce_box, text="Position intent", style="HyperMuted.TLabel").pack(anchor=tk.W)
        ttk.Checkbutton(
            reduce_box,
            text="Reduce only",
            variable=self.perp_reduce_only,
        ).pack(anchor=tk.W, pady=(3, 0))
        ttk.Button(form, text="Use Mid", command=self._use_perp_mid).grid(
            row=4, column=0, columnspan=2, sticky="ew", pady=(6, 4)
        )
        form.rowconfigure(5, weight=1, minsize=8)
        ttk.Button(
            form,
            text="Review Perp Order",
            command=self._review_perp_order,
            style="HyperPrimary.TButton",
        ).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(2, 0))

    def _build_hyperliquid_orders_panel(self, parent: tk.Frame) -> None:
        header = ttk.Frame(parent, style="HyperCard.TFrame")
        header.pack(fill=tk.X, pady=(0, 7))
        ttk.Label(
            header,
            text="Order Review",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 11, "bold"),
        ).pack(side=tk.LEFT)
        ttk.Label(header, text="SELECTED-ITEM ACTIONS", style="HyperMuted.TLabel").pack(side=tk.RIGHT)

        table_frame = ttk.Frame(parent, style="HyperCard.TFrame")
        table_frame.pack(fill=tk.BOTH, expand=True)
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.hyperliquid_open_orders_table = ttk.Treeview(
            table_frame,
            columns=("account", "market", "kind", "side", "size", "price", "type", "reduce", "oid"),
            show="headings",
            height=6,
            style="HyperPortfolio.Treeview",
            selectmode="browse",
        )
        self.hyperliquid_open_orders_table.configure(
            displaycolumns=("account", "market", "side", "size", "price")
        )
        self._configure_hyperliquid_order_columns(self.hyperliquid_open_orders_table, compact=True)
        order_y = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.hyperliquid_open_orders_table.yview)
        self.hyperliquid_open_orders_table.configure(yscrollcommand=order_y.set)
        self.hyperliquid_open_orders_table.grid(row=0, column=0, sticky="nsew")
        order_y.grid(row=0, column=1, sticky="ns")
        self.hyperliquid_open_orders_table.bind("<<TreeviewSelect>>", self._use_selected_hyperliquid_order)
        self.hyperliquid_open_orders_table.bind("<Double-1>", self._edit_selected_hyperliquid_open_order)
        ttk.Label(parent, textvariable=self.orders_status, style="HyperMuted.TLabel").pack(
            anchor=tk.W, pady=(5, 5)
        )
        actions = ttk.Frame(parent, style="HyperCard.TFrame")
        actions.pack(fill=tk.X)
        for column in range(3):
            actions.columnconfigure(column, weight=1)
        ttk.Button(
            actions,
            text="Edit Selected",
            command=self._edit_selected_hyperliquid_open_order,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(
            actions,
            text="Cancel Selected",
            command=self._cancel_selected_hyperliquid_order,
            style="HyperDanger.TButton",
        ).grid(row=0, column=1, sticky="ew", padx=3)
        ttk.Button(
            actions,
            text="Refresh",
            command=self._refresh_hyperliquid,
        ).grid(row=0, column=2, sticky="ew", padx=(3, 0))

    def _hyper_card(
        self,
        parent: tk.Misc,
        *,
        padding: tuple[int, int],
    ) -> tk.Frame:
        horizontal, vertical = padding
        return tk.Frame(
            parent,
            background=SURFACE,
            highlightbackground=BORDER,
            highlightcolor=BORDER,
            highlightthickness=1,
            bd=0,
            padx=horizontal,
            pady=vertical,
        )

    def _ticket_heading(self, parent: tk.Frame, title: str, badge: str) -> None:
        header = ttk.Frame(parent, style="HyperCard.TFrame")
        header.pack(fill=tk.X, pady=(0, 7))
        ttk.Label(
            header,
            text=title,
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 11, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            header,
            text=badge,
            background=HYPE_ACCENT_DARK,
            foreground=HYPE_ACCENT,
            font=("Segoe UI", 7, "bold"),
            padx=7,
            pady=2,
        ).pack(side=tk.RIGHT)

    def _compact_entry(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
    ) -> None:
        field = ttk.Frame(parent, style="HyperCard.TFrame")
        field.grid(
            row=row,
            column=column,
            sticky="ew",
            padx=(0 if column == 0 else 4, 4 if column == 0 else 0),
            pady=(2, 0),
        )
        ttk.Label(field, text=label, style="HyperMuted.TLabel").pack(anchor=tk.W)
        ttk.Entry(field, textvariable=variable).pack(fill=tk.X, pady=(1, 0))

    def _compact_combo(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        values: Sequence[str],
        row: int,
        column: int,
    ) -> None:
        field = ttk.Frame(parent, style="HyperCard.TFrame")
        field.grid(
            row=row,
            column=column,
            sticky="ew",
            padx=(0 if column == 0 else 4, 4 if column == 0 else 0),
            pady=(2, 0),
        )
        ttk.Label(field, text=label, style="HyperMuted.TLabel").pack(anchor=tk.W)
        ttk.Combobox(
            field,
            textvariable=variable,
            values=tuple(values),
            state="readonly",
        ).pack(fill=tk.X, pady=(1, 0))

    def _compact_side_row(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        choices: Sequence[tuple[str, str]],
        row: int,
    ) -> None:
        field = ttk.Frame(parent, style="HyperCard.TFrame")
        field.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(field, text=label, style="HyperMuted.TLabel").pack(anchor=tk.W)
        buttons = ttk.Frame(field, style="HyperCard.TFrame")
        buttons.pack(fill=tk.X, pady=(1, 0))
        for index, (text, value) in enumerate(choices):
            buttons.columnconfigure(index, weight=1)
            ttk.Radiobutton(
                buttons,
                text=text,
                value=value,
                variable=variable,
                style="HyperSide.TRadiobutton",
            ).grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 3, 0))

    def _configure_hyperliquid_order_columns(
        self,
        table: ttk.Treeview,
        *,
        compact: bool = False,
    ) -> None:
        widths = {
            "account": 76 if compact else 100,
            "market": 86 if compact else 120,
            "kind": 58 if compact else 70,
            "side": 58 if compact else 70,
            "size": 78 if compact else 90,
            "price": 82 if compact else 100,
            "type": 76 if compact else 100,
            "reduce": 62 if compact else 75,
            "oid": 84 if compact else 110,
        }
        labels = {
            "account": "Account",
            "market": "Market",
            "kind": "Kind",
            "side": "Side",
            "size": "Size",
            "price": "Price",
            "type": "Type",
            "reduce": "Reduce",
            "oid": "Order ID",
        }
        for column in table.cget("columns"):
            anchor = tk.E if column in {"size", "price", "oid"} else tk.W
            self._setup_column(table, str(column), labels[str(column)], widths[str(column)], anchor)

    def _switch_hyperliquid_portfolio_view(self) -> None:
        selected = self.portfolio_view.get()
        if selected == "Cash":
            self.cash_view.tkraise()
        elif selected == "Open Orders":
            self.portfolio_orders_view.tkraise()
        else:
            self.positions_view.tkraise()

    def _update_hyperliquid_scroll_region(self, _event: object | None = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_hyperliquid_body(self, event: tk.Event[tk.Canvas]) -> None:
        self._canvas_viewport_height = max(event.height, 1)
        self.canvas.itemconfigure(self._body_window, width=max(event.width, 1))
        self._apply_hyperliquid_layout(event.width)
        self.root.after_idle(self._sync_hyperliquid_body_height)

    def _sync_hyperliquid_body_height(self) -> None:
        """Share spare wide-screen height between portfolio and trading workspaces."""
        if self._last_hyperliquid_layout == "wide":
            fixed_height = (
                12
                + 16
                + self.header_frame.winfo_reqheight()
                + 10
                + self.overview_frame.winfo_reqheight()
                + 10
                + self.chain_card.winfo_reqheight()
                + 10
            )
            portfolio_natural = self.portfolio_card.winfo_reqheight() + 10
            trading_natural = self.trading_frame.winfo_reqheight()
            spare_height = max(
                self._canvas_viewport_height
                - fixed_height
                - portfolio_natural
                - trading_natural,
                0,
            )
            portfolio_height = portfolio_natural + round(spare_height * 0.45)
            trading_height = trading_natural + spare_height - round(spare_height * 0.45)
        else:
            portfolio_height = 0
            trading_height = 0

        current_portfolio_height = int(self.body.grid_rowconfigure(2)["minsize"])
        current_trading_height = int(self.body.grid_rowconfigure(4)["minsize"])
        self.body.rowconfigure(2, minsize=portfolio_height, weight=0)
        self.body.rowconfigure(4, minsize=trading_height, weight=0)
        self.canvas.itemconfigure(self._body_window, height=0)
        if (
            current_portfolio_height != portfolio_height
            or current_trading_height != trading_height
        ):
            self.root.after(20, self._sync_hyperliquid_body_height)
        else:
            self.root.after_idle(self._update_hyperliquid_scroll_region)

    def _apply_hyperliquid_layout(self, width: int, *, force: bool = False) -> None:
        layout = "wide" if width >= 1450 else "medium" if width >= 760 else "narrow"
        if not force and layout == self._last_hyperliquid_layout:
            return
        self._last_hyperliquid_layout = layout
        for widget in (*self.account_cards.values(), self.hype_pulse_card):
            widget.grid_forget()
        for widget in (self.spot_ticket_card, self.perp_ticket_card, self.order_review_card):
            widget.grid_forget()
        for row in range(3):
            self.overview_frame.rowconfigure(row, weight=0)
            self.trading_frame.rowconfigure(row, weight=0)

        if layout == "wide":
            self.overview_frame.rowconfigure(0, weight=1)
            self.trading_frame.rowconfigure(0, weight=1)
            for column in range(3):
                self.overview_frame.columnconfigure(column, weight=1, uniform="hyper-overview")
                self.trading_frame.columnconfigure(column, weight=1, uniform="hyper-trading")
            self.account_cards["jeremy"].grid(row=0, column=0, sticky="nsew", padx=(0, 5))
            self.account_cards["alex"].grid(row=0, column=1, sticky="nsew", padx=5)
            self.hype_pulse_card.grid(row=0, column=2, sticky="nsew", padx=(5, 0))
            self.spot_ticket_card.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
            self.perp_ticket_card.grid(row=0, column=1, sticky="nsew", padx=5)
            self.order_review_card.grid(row=0, column=2, sticky="nsew", padx=(5, 0))
            return

        self.overview_frame.columnconfigure(0, weight=1, uniform="hyper-overview")
        self.overview_frame.columnconfigure(1, weight=1, uniform="hyper-overview")
        self.overview_frame.columnconfigure(2, weight=0, uniform="")
        self.trading_frame.columnconfigure(0, weight=1, uniform="hyper-trading")
        self.trading_frame.columnconfigure(1, weight=1, uniform="hyper-trading")
        self.trading_frame.columnconfigure(2, weight=0, uniform="")
        if layout == "medium":
            self.overview_frame.rowconfigure((0, 1), weight=1)
            self.trading_frame.rowconfigure((0, 1), weight=1)
            self.account_cards["jeremy"].grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=(0, 5))
            self.account_cards["alex"].grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=(0, 5))
            self.hype_pulse_card.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(5, 0))
            self.spot_ticket_card.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=(0, 5))
            self.perp_ticket_card.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=(0, 5))
            self.order_review_card.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(5, 0))
            return

        self.overview_frame.columnconfigure(1, weight=0, uniform="")
        self.trading_frame.columnconfigure(1, weight=0, uniform="")
        self.overview_frame.rowconfigure((0, 1, 2), weight=1)
        self.trading_frame.rowconfigure((0, 1, 2), weight=1)
        self.account_cards["jeremy"].grid(row=0, column=0, sticky="nsew", pady=(0, 5))
        self.account_cards["alex"].grid(row=1, column=0, sticky="nsew", pady=5)
        self.hype_pulse_card.grid(row=2, column=0, sticky="nsew", pady=(5, 0))
        self.spot_ticket_card.grid(row=0, column=0, sticky="nsew", pady=(0, 5))
        self.perp_ticket_card.grid(row=1, column=0, sticky="nsew", pady=5)
        self.order_review_card.grid(row=2, column=0, sticky="nsew", pady=(5, 0))

    def _set_hyperliquid_canvas_hover(self, hovered: bool) -> None:
        self._canvas_hovered = hovered

    def _on_hyperliquid_mousewheel(self, event: tk.Event[tk.Canvas]) -> str | None:
        if not self._canvas_hovered:
            return None
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"

    def _set_account_summary(
        self,
        account_key: str,
        summary: HyperliquidAccountSummary,
    ) -> None:
        values = self.account_summary_values[account_key]
        values["equity"].set(_money(summary.equity))
        values["spot"].set(_money(summary.spot_equity))
        values["perps"].set(_money(summary.perp_equity))
        values["available"].set(_money_or_dash(summary.available))
        values["pnl"].set(_money_or_dash(summary.unrealized_pnl))
        values["margin"].set(_money_or_dash(summary.margin_used))
        pnl_label = self.account_pnl_labels.get(account_key)
        if pnl_label is not None:
            pnl_label.configure(foreground=_pnl_color(summary.unrealized_pnl))

    def _set_account_summary_unavailable(self, account_key: str) -> None:
        for variable in self.account_summary_values[account_key].values():
            variable.set("--")
        pnl_label = self.account_pnl_labels.get(account_key)
        if pnl_label is not None:
            pnl_label.configure(foreground=MUTED_TEXT)

    def _insert_hyperliquid_cash(self, snapshot: PortfolioSnapshot) -> None:
        if self.cash_table is None:
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

    def _insert_hyperliquid_position(self, row: HyperliquidPositionView) -> None:
        if self.holdings_table is None:
            return
        holding = row.holding
        item_id = f"position:{row.identity}"
        self._position_by_item_id[item_id] = row
        image = self._account_row_image(row.account_key)
        quantity = _to_float(getattr(holding, "quantity", None)) or 0.0
        mark = _to_float(getattr(holding, "price", None))
        value = _to_float(getattr(holding, "value", None))
        pnl = _to_float(getattr(holding, "unrealized_pnl", None))
        entry = row.entry_price if row.entry_price is not None else (mark if row.kind == "Spot" else None)
        position_type = row.kind
        if row.kind == "Perp" and row.margin_mode:
            position_type = row.margin_mode.title()
            if row.leverage is not None:
                position_type = f"{position_type} {row.leverage:g}x"
        self.holdings_table.insert(
            "",
            tk.END,
            iid=item_id,
            image=image,
            values=(
                row.account_label,
                row.market,
                position_type,
                row.side,
                f"{_number(quantity)} HYPE" if _hyperliquid_display_symbol(row.market) == "HYPE" else _number(quantity),
                _money_or_dash(entry),
                _money_or_dash(mark),
                _money_or_dash(value),
                _money_or_dash(pnl),
                _percent_or_dash(row.pnl_percent),
                _money_or_dash(row.liquidation_price),
            ),
            tags=_pnl_row_tag(pnl),
        )

    def _account_row_image(
        self,
        account_key: str,
        size: int = HYPERLIQUID_POSITION_ICON_SIZE,
    ) -> tk.PhotoImage:
        cached = self._row_icons.get(account_key)
        if cached is not None:
            return cached
        path = hyperliquid_asset_path(account_key)
        photo: tk.PhotoImage | None = None
        if path is not None:
            try:
                source = tk.PhotoImage(master=self.holdings_table, file=str(path))
            except tk.TclError:
                source = None
            if source is not None:
                factor = max(1, math.ceil(max(source.width(), source.height()) / size))
                photo = source.subsample(factor, factor)
        if photo is None:
            photo = tk.PhotoImage(master=self.holdings_table, width=size, height=size)
            radius = (size - 2) / 2
            center = (size - 1) / 2
            for y in range(size):
                half_width = math.sqrt(max(radius * radius - (y - center) ** 2, 0.0))
                left = max(0, int(center - half_width))
                right = min(size, int(center + half_width) + 1)
                if right > left:
                    photo.put(HYPE_ACCENT_DARK, to=(left, y, right, y + 1))
        self._row_icons[account_key] = photo
        return photo

    def _common_hyperliquid_facts(
        self,
        snapshots: Sequence[PortfolioSnapshot],
    ) -> Mapping[object, object]:
        for snapshot in snapshots:
            if isinstance(snapshot.account_facts, Mapping):
                return snapshot.account_facts
        return {}

    def _show_hype_market(self, value: object) -> None:
        market = value if isinstance(value, Mapping) else {}
        price = _mapping_float(market, "price")
        change = _mapping_float(market, "change_percent_24h")
        volume = _mapping_float(market, "volume_24h")
        supply = _mapping_float(market, "circulating_supply")
        self.hype_price.set(_money_or_dash(price))
        self.hype_change.set(f"{change:+.2f}% 24H" if change is not None else "24H --")
        self.hype_volume.set(_compact_money(volume))
        self.hype_supply.set(
            f"{_compact_number(supply)} HYPE" if supply is not None else "--"
        )
        closes = market.get("closes_24h")
        chart_values = closes if isinstance(closes, Sequence) and not isinstance(closes, (str, bytes)) else ()
        if self.hype_sparkline is not None:
            self.hype_sparkline.set_values(chart_values)
        chart_status = str(market.get("chart_status") or "").strip().casefold()
        self.hype_status.set("Public market data" if price is not None else "Market data unavailable")
        if price is not None and chart_status != "current":
            self.hype_status.set("Quote current • chart unavailable")
        if self.hype_change_label is not None:
            self.hype_change_label.configure(foreground=_pnl_color(change))

    def _show_chain_status(self, value: object) -> None:
        chain = value if isinstance(value, Mapping) else {}
        available = bool(chain.get("available"))
        chain_id = _mapping_int(chain, "chain_id")
        block = _mapping_int(chain, "block_number")
        gas_price_wei = _mapping_int(chain, "gas_price_wei")
        self.chain_id.set(str(chain_id) if chain_id is not None else "--")
        self.chain_block.set(f"{block:,}" if block is not None else "--")
        self.chain_base_fee.set(
            f"{gas_price_wei / 1_000_000_000:.3f} Gwei"
            if gas_price_wei is not None
            else "--"
        )
        self.chain_health.set("● Current" if available else "● Unavailable")
        if self.chain_health_label is not None:
            self.chain_health_label.configure(foreground=SUCCESS if available else MUTED_TEXT)

    def _review_spot_order(self) -> None:
        self._submit_spot_order(self._account_key_from_choice(self.spot_account.get()))

    def _review_perp_order(self) -> None:
        self._submit_perp_order(self._account_key_from_choice(self.perp_account.get()))

    def _account_key_from_choice(self, value: str) -> str:
        return hyperliquid_account_key(value)

    def _cancel_selected_hyperliquid_order(self) -> None:
        order = self._selected_hyperliquid_order()
        if order is None:
            messagebox.showinfo("Cancel Hyperliquid Order", "Select an open order first.")
            return
        account_key = str(order.get("accountKey") or "").strip().lower()
        account_label = str(order.get("accountLabel") or account_key.title())
        coin = str(order.get("coin") or "").strip()
        try:
            order_id = _positive_int(order.get("oid"), "Order ID")
            if not account_key or not coin:
                raise ValueError("The selected order is missing account or market metadata.")
            if not messagebox.askyesno(
                "Confirm Hyperliquid Cancel",
                f"Cancel the selected order?\n\nAccount: {account_label}\nMarket: {coin}\nOrder ID: {order_id}",
            ):
                return
            result = HyperliquidExecutionAdapter(account_key).cancel(coin, order_id)
            self._load_hyperliquid_open_orders()
            messagebox.showinfo("Hyperliquid Cancel Submitted", f"Response:\n{result}")
        except Exception as exc:
            messagebox.showerror("Hyperliquid Cancel Failed", f"{type(exc).__name__}: {exc}")

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

    def _use_spot_mid(self) -> None:
        if _hyperliquid_display_symbol(self.spot_market.get()) == "HYPE":
            bucket = self.latest_hyperliquid_bucket
            snapshots = bucket.snapshots if bucket is not None else ()
            facts = self._common_hyperliquid_facts(snapshots)
            market = facts.get("hype_market")
            market_facts = market if isinstance(market, Mapping) else {}
            price = _mapping_float(market_facts, "price")
            if price is not None and price > 0:
                self.spot_entry_limit.set(_format_hyperliquid_price(price))
                return
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
            messagebox.showerror("Hyperliquid Mid Failed", f"{type(exc).__name__}: {exc}")

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

        tables = tuple(
            table
            for table in (self.hyperliquid_open_orders_table, self.portfolio_orders_table)
            if table is not None
        )
        for table in tables:
            self._clear_table(table)
        self.hyperliquid_open_order_by_lookup_key = {}
        self.selected_hyperliquid_order_key = ""
        self.orders_status.set("Loading open orders…")

        errors: list[str] = []
        order_count = 0

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
                values = (
                    order.get("accountLabel") or account_key.title(),
                    display_coin,
                    _hyperliquid_order_kind(order).title(),
                    _hyperliquid_side_label(order),
                    order.get("sz") or "",
                    order.get("limitPx") or order.get("price") or "",
                    _hyperliquid_order_type_label(order),
                    "Yes" if _to_bool(order.get("reduceOnly")) else "No",
                    order.get("oid") or "",
                )
                for table in tables:
                    table.insert("", tk.END, iid=lookup_key, values=values)
                order_count += 1

        if errors:
            self.orders_status.set(
                f"{order_count} open order{'s' if order_count != 1 else ''}; {len(errors)} account error{'s' if len(errors) != 1 else ''}."
            )
            messagebox.showwarning("Hyperliquid Open Orders Partially Loaded", "\n".join(errors))
        else:
            self.orders_status.set(
                f"{order_count} open order{'s' if order_count != 1 else ''}."
                if order_count
                else "No open orders reported."
            )

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
                "Hyperliquid Spot Order Submitted",
                _hyperliquid_order_submitted_message(account_key, ticket, result),
            )
        except Exception as exc:
            messagebox.showerror("Hyperliquid Spot Order Failed", f"{type(exc).__name__}: {exc}")

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
                "Hyperliquid Perp Order Submitted",
                _hyperliquid_order_submitted_message(account_key, ticket, result),
            )
        except Exception as exc:
            messagebox.showerror("Hyperliquid Perp Order Failed", f"{type(exc).__name__}: {exc}")

    def _edit_selected_hyperliquid_open_order(self, _event: object | None = None) -> None:
        self._use_selected_hyperliquid_order(_event)

        order = self._selected_hyperliquid_order()
        if order is None:
            messagebox.showinfo("Edit Hyperliquid Order", "Select an open order first.")
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

        ttk.Label(body, text="New Size").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(body, textvariable=size_var, width=24).grid(row=3, column=1, sticky="ew", pady=6)

        ttk.Label(body, text="New Price").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=6)
        ttk.Entry(body, textvariable=price_var, width=24).grid(row=4, column=1, sticky="ew", pady=6)

        def submit_edit() -> None:
            try:
                ticket = HyperliquidOrderTicket(
                    coin=raw_coin,
                    is_buy=is_buy,
                    size=_required_float(size_var.get(), "New Size"),
                    limit_price=_required_float(price_var.get(), "New Price"),
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
                messagebox.showinfo("Hyperliquid Order Edited", f"Response:\n{result}")
            except Exception as exc:
                messagebox.showerror("Hyperliquid Edit Failed", f"{type(exc).__name__}: {exc}")

        ttk.Button(body, text="Submit Edit", command=submit_edit).grid(
            row=5,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(12, 0),
        )

    def _use_selected_hyperliquid_holding(self, _event: object) -> None:
        if self.holdings_table is None:
            return

        selected = self.holdings_table.selection()
        if not selected:
            return
        row = self._position_by_item_id.get(str(selected[0]))
        if row is None:
            return
        bucket = str(getattr(row.holding, "bucket", "")).strip().upper()
        symbol = str(getattr(row.holding, "symbol", "")).strip().upper()
        if not symbol:
            return

        if bucket == "SPOT":
            self.spot_market.set(_hyperliquid_display_symbol(symbol))
            self.spot_account.set(_account_choice_for_key(row.account_key))
        elif bucket == "PERPS":
            self.perp_coin.set(_hyperliquid_display_symbol(symbol))
            self.perp_account.set(_account_choice_for_key(row.account_key))

    def _use_selected_hyperliquid_order(self, event: object | None) -> None:
        default_table = self.hyperliquid_open_orders_table
        event_widget = getattr(event, "widget", None)
        table = event_widget if isinstance(event_widget, ttk.Treeview) else default_table
        if table is None:
            return

        selected = table.selection()
        if not selected:
            return

        lookup_key = str(selected[0])
        values = table.item(lookup_key, "values")
        if len(values) < 9:
            return

        self.selected_hyperliquid_order_key = lookup_key

        display_coin = str(values[1])
        kind = str(values[2])

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


def _hyperliquid_side_label(order: dict[str, object]) -> str:
    side = _hyperliquid_order_side(order).strip().casefold()
    if side in {"b", "buy", "bid"}:
        return "Buy"
    if side in {"a", "s", "sell", "ask"}:
        return "Sell"
    return side.title() or "--"


def _hyperliquid_order_type_label(order: dict[str, object]) -> str:
    raw = order.get("orderType") or order.get("type")
    if isinstance(raw, Mapping):
        if "limit" in raw:
            limit = raw.get("limit")
            if isinstance(limit, Mapping):
                tif = str(limit.get("tif") or "").strip()
                return f"Limit {tif}".strip()
            return "Limit"
        if "trigger" in raw:
            return "Trigger"
    text = str(raw or "Limit").strip()
    return text.title() if text else "Limit"


def _account_choice_for_key(account_key: str) -> str:
    return HYPERLIQUID_ACCOUNT_CHOICES[1] if account_key == "alex" else HYPERLIQUID_ACCOUNT_CHOICES[0]


def _pnl_row_tag(*values: float | None) -> tuple[str, ...]:
    has_negative = any(value is not None and value < 0 for value in values)
    has_positive = any(value is not None and value > 0 for value in values)

    if has_negative and not has_positive:
        return ("pnl_negative",)

    if has_positive and not has_negative:
        return ("pnl_positive",)

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
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _money_or_dash(value: float | None) -> str:
    return "--" if value is None else _money(value)


def _compact_money(value: float | None) -> str:
    if value is None:
        return "--"
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"${value / 1_000_000_000:,.2f}B"
    if absolute >= 1_000_000:
        return f"${value / 1_000_000:,.2f}M"
    if absolute >= 1_000:
        return f"${value / 1_000:,.2f}K"
    return _money(value)


def _compact_number(value: float | None) -> str:
    if value is None:
        return "--"
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        return f"{value / 1_000_000_000:,.2f}B"
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:,.2f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:,.2f}K"
    return _number(value)


def _percent_or_dash(value: float | None) -> str:
    return "--" if value is None else f"{value:+.2f}%"


def _mapping_int(values: Mapping[object, object], key: object) -> int | None:
    value = values.get(key)
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pnl_color(value: float | None) -> str:
    if value is None or value == 0:
        return MUTED_TEXT
    return SUCCESS if value > 0 else DANGER


def _format_local_timestamp(value: datetime | None) -> str:
    if value is None:
        return "--"
    local = value.astimezone() if value.tzinfo is not None else value
    return local.strftime("%I:%M:%S %p").lstrip("0")


def _number(value: float) -> str:
    return f"{value:,.8f}".rstrip("0").rstrip(".")


def _coverage_or_dash(labels: list[str]) -> str:
    return " + ".join(labels) if labels else "no account day PnL available"
