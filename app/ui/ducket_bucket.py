from __future__ import annotations

import math
import threading
import tkinter as tk
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from PIL import Image, ImageTk

from app.models.portfolio import PortfolioSnapshot
from app.hyperliquid_accounts import HYPERLIQUID_ACCOUNT_PROFILES
from app.ui.hyperliquid_workspace import HyperliquidWorkspaceMixin
from app.services.hyperliquid_markets import display_asset
from app.services.aggregate import DucketBucketSnapshot
from app.ui.options_strategies import OptionsStrategiesTab
from app.ui.rolling_forecasts import RollingForecastTab
from app.ui.gameplan_stats import GameplanStatsTab
from app.ui.gameplan import GameplanTab
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
    "clearpond": "clearpond-avatar.png",
    "btc": "btc.png",
    "eth": "eth.png",
    "zec": "zec.png",
}
HYPERLIQUID_ACCOUNT_CHOICES = ("Jeremy (JE)", "Alex (AL)", "Clearpond (CP)")
HYPE_ACCENT = "#78edc1"
HYPE_ACCENT_DARK = "#123f3a"
HYPERLIQUID_POSITION_ROW_HEIGHT = 44
HYPERLIQUID_POSITION_ICON_SIZE = 28
HYPERLIQUID_POSITION_ICON_COLUMN_WIDTH = 42


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
        return None


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


def _hyperliquid_photo(path: Path, master: tk.Misc, size: int) -> ImageTk.PhotoImage | None:
    """Antialias the supplied artwork at display size; keep source files intact."""
    try:
        with Image.open(path) as source:
            artwork = source.convert("RGBA")
            artwork.thumbnail((size, size), Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(artwork, master=master)
    except (OSError, ValueError, tk.TclError):
        return None


def hyperliquid_account_key(label: str) -> str:
    normalized = label.strip().casefold()
    for key in HYPERLIQUID_ACCOUNT_PROFILES:
        if normalized == key or normalized.startswith(key + " ("):
            return key
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
        equity=snapshot.total_value,
        spot_equity=round(spot_equity, 2),
        perp_equity=round(perp_equity, 2),
        available=_mapping_float(facts, "available"),
        unrealized_pnl=_mapping_float(facts, "unrealized_pnl") if "unrealized_pnl" in facts else snapshot.unrealized_pnl,
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
        self._photo: ImageTk.PhotoImage | None = None
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        path = hyperliquid_asset_path(self._asset_key)
        if path is not None:
            self._photo = _hyperliquid_photo(path, self, self._size - 4)
            if self._photo is not None:
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
        initials = {"jeremy": "JE", "alex": "AL", "clearpond": "CP", "btc": "B", "eth": "E", "zec": "Z"}.get(self._asset_key, self._asset_key[:2].upper())
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
        self._line_color = HYPE_ACCENT
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
        self.create_line(*points, fill=self._line_color, width=2, smooth=True)
        self.create_oval(
            points[-2] - 2,
            points[-1] - 2,
            points[-2] + 2,
            points[-1] + 2,
            fill=self._line_color,
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
        stats_frame = ttk.Frame(notebook)
        gameplan_frame = ttk.Frame(notebook)

        notebook.add(forecasts_frame, text="Rolling Forecasts")
        notebook.add(strategies_frame, text="Options Strategies")
        notebook.add(schwab_frame, text="Schwab Duckets")
        notebook.add(hyperliquid_frame, text="Hyperliquid Duckets")
        notebook.add(stats_frame, text="Gameplan Stats")
        notebook.add(gameplan_frame, text="Gameplan")

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

        GameplanStatsTab(root=self.root, parent=stats_frame)
        GameplanTab(root=self.root, parent=gameplan_frame)


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


class HyperliquidDucketsTab(HyperliquidWorkspaceMixin, DucketsTab):
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
            for account_key in HYPERLIQUID_ACCOUNT_PROFILES
        }
        self.account_pnl_labels: dict[str, tk.Label] = {}
        self.hyperliquid_open_order_by_lookup_key: dict[str, dict[str, object]] = {}
        self.selected_hyperliquid_order_key = ""
        self._position_by_item_id: dict[str, HyperliquidPositionView] = {}
        self._row_icons: dict[str, tk.PhotoImage | ImageTk.PhotoImage] = {}
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
            sync_snapshots=lambda: sync_hyperliquid_portfolios(tuple(self.watchlist)),
        )
        self.status_icon.set("○")


    def _apply_hyperliquid_styles(self) -> None:
        style = ttk.Style(self.root)
        for orientation in ("Horizontal", "Vertical"):
            style.configure(f"Hyper.{orientation}.TScrollbar", background="#253b4d", troughcolor=TABLE_FIELD, bordercolor=TABLE_FIELD, lightcolor=TABLE_FIELD, darkcolor=TABLE_FIELD, arrowcolor=MUTED_TEXT, arrowsize=11)
        for kind in ("TButton", "TCombobox", "TEntry", "TCheckbutton"):
            style.configure(f"Hyper.{kind}", font=("Segoe UI", 10), background=SURFACE, foreground=TEXT, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=5)
        style.map("Hyper.TButton", foreground=[("disabled", MUTED_TEXT)], background=[("active", "#174b4a")])
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
            font=("Segoe UI", 10),
        )
        style.configure(
            "HyperPortfolio.Treeview.Heading",
            background=SURFACE_ALT,
            foreground=MUTED_TEXT,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            relief="flat",
            padding=(5, 6),
            font=("Segoe UI", 9),
        )
        style.map(
            "HyperPortfolio.Treeview",
            background=[("selected", "#174b4a")],
            foreground=[("selected", TEXT)],
        )


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
        entry = row.entry_price
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
                f"{display_asset(row.market)}-{'SPOT' if row.kind == 'Spot' else 'PERP'}",
                position_type,
                row.side,
                f"{_number(quantity)} {display_asset(row.market)}",
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
    ) -> tk.PhotoImage | ImageTk.PhotoImage:
        cached = self._row_icons.get(account_key)
        if cached is not None:
            return cached
        path = hyperliquid_asset_path(account_key)
        photo = _hyperliquid_photo(path, self.holdings_table, size) if path is not None else None
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
        if order.get("isTrigger") or _hyperliquid_order_type_label(order) != "Limit":
            messagebox.showinfo("Edit Hyperliquid Order", "Editing is supported for limit orders only.")
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
    for choice in HYPERLIQUID_ACCOUNT_CHOICES:
        if hyperliquid_account_key(choice) == account_key:
            return choice
    raise ValueError(f"Unknown Hyperliquid account: {account_key}")


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
