from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from app.models.portfolio import CashBalance, Holding, PortfolioSnapshot
from app.services.aggregate import DucketBucketSnapshot
from app.services.schwab import SchwabSession, sync_schwab_portfolio
from app.services.schwab_order_fields import (
    SCHWAB_EQUITY_ORDER_TYPE_CHOICES,
    SCHWAB_EQUITY_SIDE_CHOICES,
    SCHWAB_EQUITY_TIME_IN_FORCE_CHOICES,
    schwab_equity_tif_from_api,
)
from app.services.schwab_policy_inputs import SCHWAB_TERMINAL_ORDER_STATUSES
from app.services.schwab_stock_orders import (
    SCHWAB_EQUITY_POSITION_EFFECT_CHOICES,
    SCHWAB_EQUITY_SPECIAL_INSTRUCTION_CHOICES,
    build_schwab_stock_order_payload,
    build_schwab_stock_replacement_payload,
    schwab_stock_order_edit,
)
from app.ui.background_tasks import run_in_background
from app.ui.schwab_order_messages import (
    order_confirmation_message,
    order_replacement_confirmation_message,
    order_replaced_message,
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
    MUTED_LABEL_FONT,
    MUTED_TEXT,
    SUCCESS,
    SURFACE,
    SURFACE_ALT,
    TABLE_FIELD,
    TEXT,
    WARNING,
)


EQUITY_BUCKETS = frozenset({"EQUITY", "ETF", "STOCK"})
EQUITY_ORDER_ASSET_TYPES = frozenset(
    {"EQUITY", "ETF", "STOCK", "COLLECTIVE_INVESTMENT"}
)
ALL_ACCOUNTS = "All Accounts"
NO_ACCOUNTS = "No accounts"
ASSET_FILTER_CHOICES = ("All", "Stocks", "ETFs")
SECURITY_MARK_ASSET_DIR = Path(__file__).with_name("assets") / "security_marks"
SECURITY_MARK_FILENAMES = {
    "AAPL": "aapl.png",
    "AMZN": "amzn.png",
    "GOOG": "goog.png",
    "GOOGL": "goog.png",
    "MU": "mu.png",
    "MSFT": "msft.png",
    "NVDA": "nvda.png",
    "SNDK": "sndk.png",
}


@dataclass(frozen=True)
class EquityHoldingView:
    identity: str
    account_label: str
    holding: Holding

    @property
    def symbol(self) -> str:
        return self.holding.symbol.strip().upper()

    @property
    def asset_type(self) -> str:
        normalized = self.holding.bucket.strip().upper()
        return "ETF" if normalized == "ETF" else "Stock"


@dataclass(frozen=True)
class PnlSummary:
    value: float | None
    reported_count: int
    total_count: int

    @property
    def is_partial(self) -> bool:
        return 0 < self.reported_count < self.total_count


@dataclass(frozen=True)
class SchwabEquitySummary:
    net_liquidation: float
    cash_and_sweep: float
    stocks_and_etfs: float
    open_pnl: PnlSummary
    day_pnl: PnlSummary


@dataclass(frozen=True)
class AllocationSegments:
    cash_fraction: float
    equities_fraction: float


@dataclass(frozen=True)
class EquityQuoteView:
    symbol: str
    bid: float | None
    ask: float | None
    mid: float | None
    bid_size: float | None
    ask_size: float | None
    source: str


@dataclass(frozen=True)
class SchwabOrderRow:
    order: Mapping[str, object]
    order_id: str
    status: str
    entered: str
    symbol: str
    side: str
    quantity: str
    order_type: str
    price: str
    time_in_force: str
    position_effect: str
    account: str
    can_modify: bool
    can_cancel: bool

    def values(self) -> tuple[str, ...]:
        return (
            self.order_id,
            self.status,
            self.entered,
            self.symbol,
            self.side,
            self.quantity,
            self.order_type,
            self.price,
            self.time_in_force,
            self.position_effect,
            self.account,
        )


def normalize_snapshot_result(
    value: PortfolioSnapshot | DucketBucketSnapshot | Sequence[PortfolioSnapshot],
) -> tuple[PortfolioSnapshot, ...]:
    if isinstance(value, PortfolioSnapshot):
        return (value,)
    if isinstance(value, DucketBucketSnapshot):
        return tuple(value.snapshots)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        snapshots = tuple(value)
        if all(isinstance(snapshot, PortfolioSnapshot) for snapshot in snapshots):
            return snapshots
    raise TypeError("Schwab snapshot loader returned an unexpected value.")


def equity_holding_views(
    snapshots: Sequence[PortfolioSnapshot],
) -> tuple[EquityHoldingView, ...]:
    rows: list[EquityHoldingView] = []
    for snapshot_index, snapshot in enumerate(snapshots):
        for holding_index, holding in enumerate(snapshot.holdings):
            if holding.bucket.strip().upper() not in EQUITY_BUCKETS:
                continue
            rows.append(
                EquityHoldingView(
                    identity=f"{snapshot_index}:{holding_index}",
                    account_label=snapshot.account_label,
                    holding=holding,
                )
            )
    return tuple(
        sorted(
            rows,
            key=lambda row: (
                row.symbol,
                row.account_label.casefold(),
                row.identity,
            ),
        )
    )


def account_filter_choices(
    snapshots: Sequence[PortfolioSnapshot],
) -> tuple[str, ...]:
    labels = tuple(
        dict.fromkeys(
            snapshot.account_label.strip()
            for snapshot in snapshots
            if snapshot.account_label.strip()
        )
    )
    if not labels:
        return (NO_ACCOUNTS,)
    if len(labels) == 1:
        return labels
    return (ALL_ACCOUNTS, *labels)


def filter_equity_holding_views(
    rows: Sequence[EquityHoldingView],
    *,
    account: str = ALL_ACCOUNTS,
    asset_type: str = "All",
) -> tuple[EquityHoldingView, ...]:
    clean_account = account.strip()
    clean_asset_type = asset_type.strip().casefold()
    filtered: list[EquityHoldingView] = []
    for row in rows:
        if clean_account not in {"", ALL_ACCOUNTS} and row.account_label != clean_account:
            continue
        if clean_asset_type == "stocks" and row.asset_type != "Stock":
            continue
        if clean_asset_type == "etfs" and row.asset_type != "ETF":
            continue
        if clean_asset_type not in {"", "all", "stocks", "etfs"}:
            continue
        filtered.append(row)
    return tuple(filtered)


def schwab_equity_summary(
    snapshots: Sequence[PortfolioSnapshot],
) -> SchwabEquitySummary:
    rows = equity_holding_views(snapshots)
    return SchwabEquitySummary(
        net_liquidation=round(sum(snapshot.total_value for snapshot in snapshots), 2),
        cash_and_sweep=round(sum(snapshot.cash_value for snapshot in snapshots), 2),
        stocks_and_etfs=round(sum(row.holding.value for row in rows), 2),
        open_pnl=_pnl_summary(rows, "unrealized_pnl"),
        day_pnl=_pnl_summary(rows, "day_pnl"),
    )


def safe_allocation_segments(
    cash_and_sweep: float,
    stocks_and_etfs: float,
) -> AllocationSegments | None:
    if not _is_finite_number(cash_and_sweep) or not _is_finite_number(
        stocks_and_etfs
    ):
        return None
    cash = float(cash_and_sweep)
    equities = float(stocks_and_etfs)
    total = cash + equities
    if cash < 0.0 or equities < 0.0 or total <= 0.0:
        return None
    return AllocationSegments(
        cash_fraction=cash / total,
        equities_fraction=equities / total,
    )


def is_equity_order(order: object) -> bool:
    if not isinstance(order, Mapping):
        return False
    legs = order.get("orderLegCollection")
    if not isinstance(legs, list) or not legs:
        return False
    for leg in legs:
        if not isinstance(leg, Mapping):
            return False
        instrument = leg.get("instrument")
        if not isinstance(instrument, Mapping):
            return False
        asset_type = str(instrument.get("assetType") or "").strip().upper()
        if asset_type not in EQUITY_ORDER_ASSET_TYPES:
            return False
    return True


def equity_only_orders(orders: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(orders, list):
        return ()
    return tuple(order for order in orders if is_equity_order(order))


def schwab_order_row(order: Mapping[str, object]) -> SchwabOrderRow:
    legs = order.get("orderLegCollection")
    leg_rows = [leg for leg in legs if isinstance(leg, Mapping)] if isinstance(legs, list) else []
    first_leg = leg_rows[0] if leg_rows else {}
    instrument = first_leg.get("instrument")
    first_instrument = instrument if isinstance(instrument, Mapping) else {}
    symbols = tuple(
        dict.fromkeys(
            str(
                (leg.get("instrument") if isinstance(leg.get("instrument"), Mapping) else {}).get(
                    "symbol"
                )
                or ""
            ).strip().upper()
            for leg in leg_rows
            if str(
                (leg.get("instrument") if isinstance(leg.get("instrument"), Mapping) else {}).get(
                    "symbol"
                )
                or ""
            ).strip()
        )
    )
    symbol = symbols[0] if len(symbols) == 1 else " + ".join(symbols)
    instruction = str(first_leg.get("instruction") or "").strip().upper()
    quantity = first_leg.get("quantity")
    order_type = str(order.get("orderType") or "").strip().upper()
    order_id = str(order.get("orderId") or "").strip()
    status_raw = str(order.get("status") or "").strip().upper()
    try:
        time_in_force = schwab_equity_tif_from_api(
            order.get("session"), order.get("duration")
        )
    except ValueError:
        time_in_force = str(order.get("duration") or "--").strip().upper() or "--"
    position_effect = str(first_leg.get("positionEffect") or "AUTO").strip().upper()
    if position_effect in {"", "AUTOMATIC"}:
        position_effect = "AUTO"
    try:
        schwab_stock_order_edit(order)
    except (TypeError, ValueError):
        can_modify = False
    else:
        can_modify = True
    return SchwabOrderRow(
        order=order,
        order_id=order_id or "--",
        status=_humanize(status_raw) if status_raw else "--",
        entered=_format_order_time(order.get("enteredTime")),
        symbol=symbol or str(first_instrument.get("symbol") or "--").strip().upper(),
        side=_humanize(instruction) if instruction else "--",
        quantity=_number_text(quantity),
        order_type=_humanize(order_type) if order_type else "--",
        price=_order_price_text(order, order_type),
        time_in_force=time_in_force,
        position_effect=_humanize(position_effect),
        account=_masked_account(order),
        can_modify=can_modify,
        can_cancel=bool(order_id and status_raw not in SCHWAB_TERMINAL_ORDER_STATUSES),
    )


def quote_view_from_payload(symbol: str, payload: object) -> EquityQuoteView:
    clean_symbol = symbol.strip().upper()
    row = payload if isinstance(payload, Mapping) else {}
    nested = row.get("quote") if isinstance(row, Mapping) else None
    quote = nested if isinstance(nested, Mapping) else row
    bid = _first_number(quote, ("bidPrice", "bid"))
    ask = _first_number(quote, ("askPrice", "ask"))
    mark = _first_number(quote, ("mark", "markPrice"))
    last = _first_number(quote, ("lastPrice", "last"))
    if bid is not None and ask is not None and bid > 0.0 and ask > 0.0:
        mid = round((bid + ask) / 2.0, 8)
        source = "Bid / ask midpoint"
    elif mark is not None and mark > 0.0:
        mid = mark
        source = "Mark fallback"
    elif last is not None and last > 0.0:
        mid = last
        source = "Last-price fallback"
    else:
        mid = None
        source = "No usable midpoint"
    return EquityQuoteView(
        symbol=clean_symbol,
        bid=bid,
        ask=ask,
        mid=mid,
        bid_size=_first_number(quote, ("bidSize", "bidSizeInLong")),
        ask_size=_first_number(quote, ("askSize", "askSizeInLong")),
        source=source,
    )


def quote_request_is_current(
    request_generation: int,
    request_symbol: str,
    *,
    current_generation: int,
    current_symbol: str,
) -> bool:
    return request_generation == current_generation and request_symbol.strip().upper() == current_symbol.strip().upper()


def security_mark_path(
    symbol: str,
    *,
    asset_root: Path = SECURITY_MARK_ASSET_DIR,
) -> Path | None:
    filename = SECURITY_MARK_FILENAMES.get(symbol.strip().upper())
    if not filename:
        return None
    candidate = asset_root / filename
    return candidate if candidate.is_file() else None


def security_monogram(symbol: str) -> str:
    cleaned = "".join(character for character in symbol.strip().upper() if character.isalnum())
    return cleaned[:2] or "--"


def schwab_layout(width: int) -> tuple[int, bool]:
    if width >= 1450:
        return 5, True
    if width >= 900:
        return 3, False
    if width >= 560:
        return 2, False
    return 1, False


class _StatusDot(tk.Canvas):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(
            parent,
            width=12,
            height=12,
            background=BACKGROUND,
            highlightthickness=0,
            bd=0,
        )
        self.set_tone("neutral")

    def set_tone(self, tone: str) -> None:
        self.delete("all")
        self.create_oval(2, 2, 10, 10, fill=_tone_color(tone), outline="")


class _AllocationBar(tk.Canvas):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(
            parent,
            height=12,
            background=SURFACE,
            highlightthickness=0,
            bd=0,
        )
        self._segments: AllocationSegments | None = None
        self.bind("<Configure>", self._redraw)

    def set_values(self, cash: float, equities: float) -> None:
        self._segments = safe_allocation_segments(cash, equities)
        self._redraw()

    def _redraw(self, _event: object | None = None) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 1)
        height = max(self.winfo_height(), 1)
        self.create_rectangle(0, 2, width, height - 2, fill=TABLE_FIELD, outline="")
        if self._segments is None:
            self.create_rectangle(0, 2, width, height - 2, fill=BORDER, outline="")
            return
        cash_end = int(round(width * self._segments.cash_fraction))
        if cash_end > 0:
            self.create_rectangle(0, 2, cash_end, height - 2, fill="#45b85c", outline="")
        if cash_end < width:
            self.create_rectangle(cash_end, 2, width, height - 2, fill=ACCENT, outline="")


class _SecurityMark(tk.Canvas):
    def __init__(self, parent: tk.Misc, *, size: int = 40) -> None:
        super().__init__(
            parent,
            width=size,
            height=size,
            background=SURFACE,
            highlightthickness=0,
            bd=0,
        )
        self._size = size
        self._photo: tk.PhotoImage | None = None
        self.show("")

    def show(self, symbol: str) -> None:
        self.delete("all")
        self._photo = None
        path = security_mark_path(symbol)
        if path is not None:
            try:
                photo = tk.PhotoImage(master=self, file=str(path))
            except tk.TclError:
                photo = None
            if photo is not None:
                factor = max(1, math.ceil(max(photo.width(), photo.height()) / self._size))
                self._photo = photo.subsample(factor, factor)
                self.create_image(self._size / 2, self._size / 2, image=self._photo)
                return
        color = _symbol_color(symbol)
        self.create_oval(2, 2, self._size - 2, self._size - 2, fill=color, outline=BORDER)
        self.create_text(
            self._size / 2,
            self._size / 2,
            text=security_monogram(symbol),
            fill=TEXT,
            font=("Segoe UI", max(8, self._size // 4), "bold"),
        )


class SchwabDucketsTab:
    """Equities-only Schwab portfolio and order command center."""

    def __init__(
        self,
        root: tk.Tk,
        parent: ttk.Frame,
        *,
        snapshot_loader: Callable[
            [], PortfolioSnapshot | DucketBucketSnapshot | Sequence[PortfolioSnapshot]
        ] = sync_schwab_portfolio,
        session_factory: Callable[[], SchwabSession] = SchwabSession,
        background_runner: Callable[..., object] = run_in_background,
    ) -> None:
        self.root = root
        self.snapshot_loader = snapshot_loader
        self.session_factory = session_factory
        self._background_runner = background_runner

        self._snapshots: tuple[PortfolioSnapshot, ...] = ()
        self._equity_rows: tuple[EquityHoldingView, ...] = ()
        self._selected_holding_key: str | None = None
        self._ticket_source_key: str | None = None
        self._position_by_item_id: dict[str, EquityHoldingView] = {}
        self._cash_by_item_id: dict[str, tuple[str, CashBalance]] = {}
        self.schwab_open_order_by_item_id: dict[str, dict[str, object]] = {}
        self._recent_order_by_item_id: dict[str, dict[str, object]] = {}
        self._selected_order: Mapping[str, object] | None = None
        self._order_status_by_kind = {
            "open": "Choose Refresh to load open orders.",
            "recent": "Choose Refresh to load recent orders.",
        }
        self._quote: EquityQuoteView | None = None
        self._quote_generation = 0
        self._quote_job: str | None = None
        self._sync_in_progress = False
        self._order_submission_in_progress = False
        self._last_layout: tuple[int, bool] | None = None
        self._canvas_viewport_height = 1
        self._inside_canvas = False
        self._row_icons: dict[str, tk.PhotoImage] = {}
        self._order_xscrolls: list[ttk.Scrollbar] = []

        self.sync_status = tk.StringVar(master=root, value="Not synced")
        self.last_sync = tk.StringVar(master=root, value="Last sync: --")
        self.portfolio_view = tk.StringVar(master=root, value="Positions")
        self.account_filter = tk.StringVar(master=root, value=NO_ACCOUNTS)
        self.asset_filter = tk.StringVar(master=root, value="All")
        self.position_status = tk.StringVar(master=root, value="Sync Schwab to load positions.")
        self.cash_status = tk.StringVar(master=root, value="Sync Schwab to load cash balances.")
        self.portfolio_footer = tk.StringVar(master=root, value="No positions loaded")
        self.allocation_cash = tk.StringVar(master=root, value="Cash & Sweep --")
        self.allocation_equities = tk.StringVar(master=root, value="Stocks / ETFs --")
        self.allocation_status = tk.StringVar(master=root, value="Allocation awaiting data")
        self.order_status = tk.StringVar(master=root, value="Choose Refresh to load orders.")
        self.selected_order_status = tk.StringVar(master=root, value="No order selected")
        self.order_id = tk.StringVar(master=root)

        self.stock_symbol = tk.StringVar(master=root)
        self.stock_side = tk.StringVar(master=root, value="BUY")
        self.stock_quantity = tk.StringVar(master=root)
        self.stock_order_type = tk.StringVar(master=root, value="LIMIT")
        self.stock_position_effect = tk.StringVar(master=root, value="AUTO")
        self.stock_tif = tk.StringVar(master=root, value="DAY")
        self.stock_entry_limit = tk.StringVar(master=root)
        self.stock_stop_price = tk.StringVar(master=root)
        self.quote_bid = tk.StringVar(master=root, value="--")
        self.quote_mid = tk.StringVar(master=root, value="--")
        self.quote_ask = tk.StringVar(master=root, value="--")
        self.quote_bid_size = tk.StringVar(master=root, value="Size --")
        self.quote_ask_size = tk.StringVar(master=root, value="Size --")
        self.quote_status = tk.StringVar(master=root, value="Enter a symbol for a live quote.")

        self.selected_holding_title = tk.StringVar(master=root, value="No holding selected")
        self.selected_holding_detail = tk.StringVar(
            master=root, value="Select a Stock/ETF row to populate the ticket."
        )
        self.selected_shares = tk.StringVar(master=root, value="--")
        self.selected_mark = tk.StringVar(master=root, value="--")
        self.selected_value = tk.StringVar(master=root, value="--")
        self.selected_open_pnl = tk.StringVar(master=root, value="--")
        self.selected_day_pnl = tk.StringVar(master=root, value="--")

        self._summary_values = {
            key: tk.StringVar(master=root, value="--")
            for key in ("net", "cash", "equities", "open", "day")
        }
        self._summary_helpers = {
            key: tk.StringVar(master=root, value=value)
            for key, value in {
                "net": "Whole-account broker total",
                "cash": "Broker cash and sweep",
                "equities": "Visible Stock/ETF holdings",
                "open": "Visible-equity coverage --",
                "day": "Visible-equity coverage --",
            }.items()
        }

        self.sync_button: ttk.Button | None = None
        self.holdings_table: ttk.Treeview | None = None
        self.cash_table: ttk.Treeview | None = None
        self.open_orders_table: ttk.Treeview | None = None
        self.recent_orders_table: ttk.Treeview | None = None
        self.review_button: ttk.Button | None = None
        self.modify_order_button: ttk.Button | None = None
        self.cancel_order_button: ttk.Button | None = None

        self._apply_styles()
        self._build(parent)

    def _apply_styles(self) -> None:
        style = ttk.Style(self.root)
        style.configure("SchwabPage.TFrame", background=BACKGROUND)
        style.configure("SchwabCard.TFrame", background=SURFACE)
        style.configure("SchwabCard.TLabel", background=SURFACE, foreground=TEXT, font=BODY_FONT)
        style.configure(
            "SchwabMuted.TLabel",
            background=SURFACE,
            foreground=MUTED_TEXT,
            font=MUTED_LABEL_FONT,
        )
        style.configure(
            "SchwabCardTitle.TLabel",
            background=SURFACE,
            foreground=TEXT,
            font=("Segoe UI", 12, "bold"),
        )
        style.configure(
            "SchwabPrimary.TButton",
            background=ACCENT,
            foreground=TEXT,
            bordercolor=ACCENT,
            padding=(12, 7),
        )
        style.map(
            "SchwabPrimary.TButton",
            background=[("active", "#247fd1"), ("disabled", SURFACE_ALT)],
            foreground=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "SchwabDanger.TButton",
            background=SURFACE,
            foreground=DANGER,
            bordercolor=DANGER,
            padding=(10, 6),
        )
        style.map(
            "SchwabDanger.TButton",
            background=[("active", "#3a2028"), ("disabled", SURFACE)],
            foreground=[("disabled", MUTED_TEXT)],
        )
        style.configure(
            "Schwab.Treeview",
            background=TABLE_FIELD,
            foreground=TEXT,
            fieldbackground=TABLE_FIELD,
            bordercolor=BORDER,
            rowheight=30,
            font=BODY_FONT,
        )
        style.configure(
            "Schwab.Treeview.Heading",
            background=SURFACE_ALT,
            foreground=TEXT,
            bordercolor=BORDER,
            font=MUTED_LABEL_FONT,
        )
        style.map(
            "Schwab.Treeview",
            background=[("selected", "#164e7a")],
            foreground=[("selected", TEXT)],
        )
        portfolio_font = ("Segoe UI", 11)
        portfolio_rowheight = max(
            40,
            tkfont.Font(root=self.root, font=portfolio_font).metrics("linespace") + 18,
        )
        style.configure(
            "SchwabPortfolio.Treeview",
            background=TABLE_FIELD,
            foreground=TEXT,
            fieldbackground=TABLE_FIELD,
            bordercolor=BORDER,
            lightcolor=TABLE_FIELD,
            darkcolor=TABLE_FIELD,
            borderwidth=1,
            relief=tk.FLAT,
            rowheight=portfolio_rowheight,
            font=portfolio_font,
        )
        style.configure(
            "SchwabPortfolio.Treeview.Heading",
            background=TABLE_FIELD,
            foreground=MUTED_TEXT,
            bordercolor=TABLE_FIELD,
            lightcolor=TABLE_FIELD,
            darkcolor=TABLE_FIELD,
            borderwidth=0,
            relief=tk.FLAT,
            padding=(8, 7),
            font=("Segoe UI", 9, "bold"),
        )
        style.layout(
            "SchwabPortfolio.Treeview.Heading",
            [
                ("Treeheading.cell", {"sticky": "nswe"}),
                (
                    "Treeheading.padding",
                    {
                        "sticky": "nswe",
                        "children": [
                            ("Treeheading.image", {"side": "right", "sticky": ""}),
                            ("Treeheading.text", {"sticky": "we"}),
                        ],
                    },
                ),
            ],
        )
        style.map(
            "SchwabPortfolio.Treeview",
            background=[("selected", "#164e7a")],
            foreground=[("selected", TEXT)],
        )
        style.configure(
            "Schwab.TNotebook",
            background=SURFACE,
            bordercolor=BORDER,
            borderwidth=0,
        )
        style.configure(
            "Schwab.TNotebook.Tab",
            background=SURFACE,
            foreground=MUTED_TEXT,
            padding=(12, 6),
            font=MUTED_LABEL_FONT,
        )
        style.map(
            "Schwab.TNotebook.Tab",
            background=[("selected", SURFACE_ALT), ("active", SURFACE_ALT)],
            foreground=[("selected", TEXT), ("active", TEXT)],
        )
        style.configure(
            "SchwabSide.TRadiobutton",
            background=FIELD_BACKGROUND,
            foreground=TEXT,
            indicatorcolor=FIELD_BACKGROUND,
            bordercolor=BORDER,
            padding=(16, 6),
        )
        style.map(
            "SchwabSide.TRadiobutton",
            background=[("selected", "#285e37"), ("active", SURFACE_ALT)],
            foreground=[("selected", TEXT)],
        )

    def _build(self, parent: ttk.Frame) -> None:
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

        self.body = ttk.Frame(self.canvas, style="SchwabPage.TFrame", padding=(18, 14, 18, 20))
        self._body_window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.columnconfigure(0, weight=1)
        self.body.rowconfigure(2, weight=0)

        self._build_header()
        self._build_summary_strip()
        self._build_content_cards()

        self.body.bind("<Configure>", self._update_scroll_region)
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind("<Enter>", lambda _event: self._set_canvas_hover(True))
        self.canvas.bind("<Leave>", lambda _event: self._set_canvas_hover(False))
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.root.after_idle(lambda: self._apply_responsive_layout(force=True))

    def _build_header(self) -> None:
        self.header = ttk.Frame(self.body, style="SchwabPage.TFrame")
        self.header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.header.columnconfigure(0, weight=1)

        self.header_title = ttk.Frame(self.header, style="SchwabPage.TFrame")
        ttk.Label(
            self.header_title,
            text="Schwab Duckets",
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 20, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            self.header_title,
            text="Equities-only portfolio & trade command center.",
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=BODY_FONT,
        ).pack(anchor=tk.W, pady=(2, 0))

        self.header_actions = ttk.Frame(self.header, style="SchwabPage.TFrame")
        status = ttk.Frame(self.header_actions, style="SchwabPage.TFrame")
        status.pack(side=tk.LEFT, padx=(0, 16))
        self.status_dot = _StatusDot(status)
        self.status_dot.pack(side=tk.LEFT, padx=(0, 7))
        status_copy = ttk.Frame(status, style="SchwabPage.TFrame")
        status_copy.pack(side=tk.LEFT)
        ttk.Label(
            status_copy,
            textvariable=self.sync_status,
            background=BACKGROUND,
            foreground=TEXT,
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            status_copy,
            textvariable=self.last_sync,
            background=BACKGROUND,
            foreground=MUTED_TEXT,
            font=("Segoe UI", 8),
        ).pack(anchor=tk.W)
        self.sync_button = ttk.Button(
            self.header_actions,
            text="↻  Sync Schwab",
            command=self._sync,
        )
        self.sync_button.pack(side=tk.RIGHT)

    def _build_summary_strip(self) -> None:
        self.summary_frame = ttk.Frame(self.body, style="SchwabPage.TFrame")
        self.summary_frame.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        card_specs = (
            ("net", "Net Liquidation"),
            ("cash", "Cash & Sweep"),
            ("equities", "Stocks / ETFs"),
            ("open", "Open P/L"),
            ("day", "Day P/L"),
        )
        self.summary_cards: list[tk.Frame] = []
        self.summary_value_labels: dict[str, tk.Label] = {}
        for key, title in card_specs:
            card = self._card(self.summary_frame, padding=(13, 10))
            self.summary_cards.append(card)
            tk.Label(
                card,
                text=title,
                background=SURFACE,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 9),
            ).pack(anchor=tk.W)
            value_label = tk.Label(
                card,
                textvariable=self._summary_values[key],
                background=SURFACE,
                foreground=TEXT,
                font=("Segoe UI", 15, "bold"),
            )
            value_label.pack(anchor=tk.W, pady=(4, 1))
            self.summary_value_labels[key] = value_label
            tk.Label(
                card,
                textvariable=self._summary_helpers[key],
                background=SURFACE,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 8),
                anchor="w",
            ).pack(fill=tk.X)

    def _build_content_cards(self) -> None:
        self.content = ttk.Frame(self.body, style="SchwabPage.TFrame")
        self.content.grid(row=2, column=0, sticky="nsew")
        self.content.columnconfigure(0, weight=2)
        self.content.columnconfigure(1, weight=1)

        self.left_stack = ttk.Frame(self.content, style="SchwabPage.TFrame")
        self.right_stack = ttk.Frame(self.content, style="SchwabPage.TFrame")
        self.left_stack.columnconfigure(0, weight=1)
        self.right_stack.columnconfigure(0, weight=1)

        self.portfolio_card = self._card(self.left_stack)
        self.ticket_card = self._card(self.right_stack)
        self.orders_card = self._card(self.left_stack)
        self.selected_card = self._card(self.right_stack)
        self._build_portfolio_card()
        self._build_ticket_card()
        self._build_orders_card()
        self._build_selected_holding_card()

    def _build_portfolio_card(self) -> None:
        card = self.portfolio_card
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=0)
        card.rowconfigure(1, weight=1)
        header = ttk.Frame(card, style="SchwabCard.TFrame")
        self.portfolio_header = header
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        ttk.Label(header, text="Portfolio", style="SchwabCardTitle.TLabel").pack(side=tk.LEFT)
        tabs = ttk.Frame(header, style="SchwabCard.TFrame")
        tabs.pack(side=tk.LEFT, padx=(18, 0))
        ttk.Radiobutton(
            tabs,
            text="Positions",
            value="Positions",
            variable=self.portfolio_view,
            command=self._switch_portfolio_view,
            style="SchwabSide.TRadiobutton",
        ).pack(side=tk.LEFT)
        ttk.Radiobutton(
            tabs,
            text="Cash",
            value="Cash",
            variable=self.portfolio_view,
            command=self._switch_portfolio_view,
            style="SchwabSide.TRadiobutton",
        ).pack(side=tk.LEFT, padx=(4, 0))

        filters = ttk.Frame(card, style="SchwabCard.TFrame")
        self.portfolio_filters = filters
        filters.grid(row=0, column=1, sticky="ew", padx=12, pady=(5, 4))
        filters.columnconfigure(0, weight=1)
        filters.columnconfigure(1, weight=1)
        account_box = ttk.Frame(filters, style="SchwabCard.TFrame")
        account_box.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Label(account_box, text="Account", style="SchwabMuted.TLabel").pack(anchor=tk.W)
        self.account_combo = ttk.Combobox(
            account_box,
            textvariable=self.account_filter,
            values=(NO_ACCOUNTS,),
            state="readonly",
            width=19,
        )
        self.account_combo.pack(fill=tk.X, pady=(2, 0))
        self.account_combo.bind("<<ComboboxSelected>>", self._filters_changed)
        asset_box = ttk.Frame(filters, style="SchwabCard.TFrame")
        asset_box.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Label(asset_box, text="Asset Type", style="SchwabMuted.TLabel").pack(anchor=tk.W)
        self.asset_combo = ttk.Combobox(
            asset_box,
            textvariable=self.asset_filter,
            values=ASSET_FILTER_CHOICES,
            state="readonly",
            width=14,
        )
        self.asset_combo.pack(fill=tk.X, pady=(2, 0))
        self.asset_combo.bind("<<ComboboxSelected>>", self._filters_changed)

        self.positions_view = ttk.Frame(card, style="SchwabCard.TFrame")
        self.cash_view = ttk.Frame(card, style="SchwabCard.TFrame")
        self.positions_view.grid(
            row=1, column=0, columnspan=2, sticky="nsew", padx=12, pady=(0, 6)
        )
        self.cash_view.grid(
            row=1, column=0, columnspan=2, sticky="nsew", padx=12, pady=(0, 6)
        )
        self.positions_view.columnconfigure(0, weight=1)
        self.positions_view.rowconfigure(1, weight=1)
        self.cash_view.columnconfigure(0, weight=1)
        self.cash_view.rowconfigure(0, weight=1)

        allocation = ttk.Frame(self.positions_view, style="SchwabCard.TFrame")
        allocation.grid(row=0, column=0, sticky="ew", pady=(2, 6))
        allocation.columnconfigure(0, weight=1)
        allocation.columnconfigure(1, weight=1)
        ttk.Label(
            allocation,
            textvariable=self.allocation_cash,
            style="SchwabCard.TLabel",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            allocation,
            textvariable=self.allocation_equities,
            style="SchwabCard.TLabel",
        ).grid(row=0, column=1, sticky="e")
        self.allocation_bar = _AllocationBar(allocation)
        self.allocation_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(4, 1))
        self.allocation_status_label = ttk.Label(
            allocation,
            textvariable=self.allocation_status,
            style="SchwabMuted.TLabel",
        )
        self.allocation_status_label.grid(row=2, column=0, columnspan=2, sticky="w")

        table_frame = ttk.Frame(self.positions_view, style="SchwabCard.TFrame")
        table_frame.grid(row=1, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        self.holdings_table = ttk.Treeview(
            table_frame,
            columns=("symbol", "type", "qty", "mark", "value", "open", "day"),
            show="tree headings",
            height=6,
            style="SchwabPortfolio.Treeview",
            selectmode="browse",
        )
        self.holdings_table.heading("#0", text="")
        self.holdings_table.column("#0", width=32, minwidth=32, stretch=False)
        self._table_column(self.holdings_table, "symbol", "Symbol", 130)
        self._table_column(self.holdings_table, "type", "Type", 70)
        self._table_column(self.holdings_table, "qty", "Qty", 78, tk.E)
        self._table_column(self.holdings_table, "mark", "Mark", 90, tk.E)
        self._table_column(self.holdings_table, "value", "Market Value", 112, tk.E)
        self._table_column(self.holdings_table, "open", "Open P/L", 100, tk.E)
        self._table_column(self.holdings_table, "day", "Day P/L", 94, tk.E)
        yscroll = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.holdings_table.yview)
        xscroll = ttk.Scrollbar(table_frame, orient=tk.HORIZONTAL, command=self.holdings_table.xview)
        self.holdings_xscroll = xscroll
        self.holdings_table.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.holdings_table.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        self.holdings_table.bind("<<TreeviewSelect>>", self._use_selected_holding)
        footer = ttk.Frame(self.positions_view, style="SchwabCard.TFrame")
        footer.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        footer.columnconfigure(1, weight=1)
        self.position_status_label = ttk.Label(
            footer,
            textvariable=self.position_status,
            style="SchwabMuted.TLabel",
        )
        self.position_status_label.grid(row=0, column=0, sticky="w")
        self.portfolio_footer_label = ttk.Label(
            footer,
            textvariable=self.portfolio_footer,
            style="SchwabCard.TLabel",
        )
        self.portfolio_footer_label.grid(row=0, column=1, sticky="e")

        cash_table_frame = ttk.Frame(self.cash_view, style="SchwabCard.TFrame")
        cash_table_frame.grid(row=0, column=0, sticky="nsew")
        cash_table_frame.columnconfigure(0, weight=1)
        cash_table_frame.rowconfigure(0, weight=1)
        self.cash_table = ttk.Treeview(
            cash_table_frame,
            columns=("account", "bucket", "symbol", "amount", "value"),
            show="headings",
            height=8,
            style="SchwabPortfolio.Treeview",
        )
        self._table_column(self.cash_table, "account", "Account", 150)
        self._table_column(self.cash_table, "bucket", "Bucket", 120)
        self._table_column(self.cash_table, "symbol", "Symbol", 80)
        self._table_column(self.cash_table, "amount", "Amount", 130, tk.E)
        self._table_column(self.cash_table, "value", "Value", 130, tk.E)
        cash_y = ttk.Scrollbar(cash_table_frame, orient=tk.VERTICAL, command=self.cash_table.yview)
        self.cash_table.configure(yscrollcommand=cash_y.set)
        self.cash_table.grid(row=0, column=0, sticky="nsew")
        cash_y.grid(row=0, column=1, sticky="ns")
        ttk.Label(
            self.cash_view,
            textvariable=self.cash_status,
            style="SchwabMuted.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._switch_portfolio_view()

    def _build_ticket_card(self) -> None:
        card = self.ticket_card
        card.columnconfigure(0, weight=1)
        header = ttk.Frame(card, style="SchwabCard.TFrame")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 8))
        ttk.Label(header, text="Stock / ETF Trade Ticket", style="SchwabCardTitle.TLabel").pack(side=tk.LEFT)
        tk.Label(
            header,
            text="DRAFT",
            background=SURFACE,
            foreground=WARNING,
            font=("Segoe UI", 8, "bold"),
            highlightbackground=WARNING,
            highlightthickness=1,
            padx=8,
            pady=2,
        ).pack(side=tk.RIGHT)

        form = ttk.Frame(card, style="SchwabCard.TFrame")
        form.grid(row=1, column=0, sticky="ew", padx=12)
        form.columnconfigure(1, weight=1)
        form.columnconfigure(2, weight=1)
        form.columnconfigure(3, weight=1)
        ttk.Label(form, text="Symbol", style="SchwabCard.TLabel").grid(row=0, column=0, sticky="w", pady=2)
        symbol_entry = ttk.Entry(form, textvariable=self.stock_symbol)
        symbol_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=2)
        symbol_entry.bind("<Return>", lambda _event: self._request_quote_now())
        symbol_entry.bind("<FocusOut>", lambda _event: self._request_quote_now())

        ttk.Label(form, text="Side", style="SchwabCard.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        side_frame = ttk.Frame(form, style="SchwabCard.TFrame")
        side_frame.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=2)
        for column, side in enumerate(SCHWAB_EQUITY_SIDE_CHOICES):
            side_frame.columnconfigure(column, weight=1)
            ttk.Radiobutton(
                side_frame,
                text=side,
                value=side,
                variable=self.stock_side,
                style="SchwabSide.TRadiobutton",
            ).grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 3, 0))

        self._ticket_entry_row(form, "Quantity", self.stock_quantity, 2)
        self._ticket_combo_row(
            form,
            "Order Type",
            self.stock_order_type,
            SCHWAB_EQUITY_ORDER_TYPE_CHOICES,
            3,
        )
        self._ticket_combo_row(
            form,
            "Position Effect",
            self.stock_position_effect,
            SCHWAB_EQUITY_POSITION_EFFECT_CHOICES,
            4,
        )
        self._ticket_combo_row(
            form,
            "Time in Force",
            self.stock_tif,
            SCHWAB_EQUITY_TIME_IN_FORCE_CHOICES,
            5,
        )
        ttk.Label(form, text="Limit Price", style="SchwabCard.TLabel").grid(row=6, column=0, sticky="w", pady=2)
        self.limit_entry = ttk.Entry(form, textvariable=self.stock_entry_limit)
        self.limit_entry.grid(row=6, column=1, sticky="ew", padx=(8, 5), pady=2)
        ttk.Label(form, text="Stop Price", style="SchwabCard.TLabel").grid(row=6, column=2, sticky="e", padx=(4, 5), pady=2)
        self.stop_entry = ttk.Entry(form, textvariable=self.stock_stop_price)
        self.stop_entry.grid(row=6, column=3, sticky="ew", pady=2)

        quote = tk.Frame(
            card,
            background=TABLE_FIELD,
            highlightbackground=BORDER,
            highlightthickness=1,
        )
        quote.grid(row=2, column=0, sticky="ew", padx=12, pady=(9, 4))
        quote.columnconfigure((0, 1, 2), weight=1, uniform="quote")
        self._quote_metric(quote, 0, "Bid", self.quote_bid, self.quote_bid_size, SUCCESS)
        self._quote_metric(quote, 1, "Mid", self.quote_mid, None, TEXT)
        self._quote_metric(quote, 2, "Ask", self.quote_ask, self.quote_ask_size, DANGER)
        ttk.Label(
            card,
            textvariable=self.quote_status,
            style="SchwabMuted.TLabel",
        ).grid(row=3, column=0, sticky="w", padx=12, pady=(1, 5))

        actions = ttk.Frame(card, style="SchwabCard.TFrame")
        actions.grid(row=4, column=0, sticky="ew", padx=12, pady=(3, 11))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=2)
        self.use_mid_button = ttk.Button(actions, text="Use Mid", command=self._use_stock_mid)
        self.use_mid_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.review_button = ttk.Button(
            actions,
            text="Review Stock / ETF Order",
            command=self._submit_stock_order,
            style="SchwabPrimary.TButton",
        )
        self.review_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        self.stock_order_type.trace_add("write", lambda *_args: self._update_price_fields())
        self.stock_symbol.trace_add("write", lambda *_args: self._schedule_quote_request())
        self._update_price_fields()

    def _build_orders_card(self) -> None:
        card = self.orders_card
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)
        header = ttk.Frame(card, style="SchwabCard.TFrame")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))
        header_copy = ttk.Frame(header, style="SchwabCard.TFrame")
        header_copy.pack(side=tk.LEFT)
        ttk.Label(header_copy, text="Order Activity", style="SchwabCardTitle.TLabel").pack(anchor=tk.W)
        ttk.Label(header_copy, textvariable=self.order_status, style="SchwabMuted.TLabel").pack(anchor=tk.W)
        header_actions = ttk.Frame(header, style="SchwabCard.TFrame")
        header_actions.pack(side=tk.RIGHT)
        self.cancel_order_button = ttk.Button(
            header_actions,
            text="Cancel",
            command=self._cancel_selected_order,
            style="SchwabDanger.TButton",
            state=tk.DISABLED,
        )
        self.cancel_order_button.pack(side=tk.RIGHT)
        self.modify_order_button = ttk.Button(
            header_actions,
            text="Modify",
            command=self._edit_selected_schwab_open_order,
            state=tk.DISABLED,
        )
        self.modify_order_button.pack(side=tk.RIGHT, padx=(0, 5))
        ttk.Button(
            header_actions,
            text="↻  Refresh",
            command=self._refresh_selected_orders,
        ).pack(side=tk.RIGHT, padx=(0, 5))

        self.orders_notebook = ttk.Notebook(card, style="Schwab.TNotebook")
        self.orders_notebook.grid(row=1, column=0, sticky="nsew", padx=12)
        open_frame = ttk.Frame(self.orders_notebook, style="SchwabCard.TFrame")
        recent_frame = ttk.Frame(self.orders_notebook, style="SchwabCard.TFrame")
        self.orders_notebook.add(open_frame, text="Open Orders")
        self.orders_notebook.add(recent_frame, text="Recent Orders")
        self.open_orders_table = self._orders_table(open_frame)
        self.recent_orders_table = self._orders_table(recent_frame)
        self.open_orders_table.bind("<<TreeviewSelect>>", self._use_selected_schwab_order)
        self.open_orders_table.bind("<Double-1>", self._edit_selected_schwab_open_order)
        self.recent_orders_table.bind("<<TreeviewSelect>>", self._use_selected_schwab_order)
        self.orders_notebook.bind("<<NotebookTabChanged>>", self._orders_tab_changed)

        ttk.Label(
            card,
            textvariable=self.selected_order_status,
            style="SchwabMuted.TLabel",
        ).grid(row=2, column=0, sticky="w", padx=12, pady=(3, 5))

    def _build_selected_holding_card(self) -> None:
        card = self.selected_card
        card.columnconfigure(1, weight=1)
        ttk.Label(card, text="Selected Holding", style="SchwabCardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(10, 7)
        )
        self.selected_mark_widget = _SecurityMark(card, size=38)
        self.selected_mark_widget.grid(row=1, column=0, sticky="nw", padx=(12, 9))
        title = ttk.Frame(card, style="SchwabCard.TFrame")
        title.grid(row=1, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(
            title,
            textvariable=self.selected_holding_title,
            style="SchwabCard.TLabel",
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor=tk.W)
        ttk.Label(
            title,
            textvariable=self.selected_holding_detail,
            style="SchwabMuted.TLabel",
            wraplength=420,
        ).pack(anchor=tk.W, pady=(2, 0))
        metrics = ttk.Frame(card, style="SchwabCard.TFrame")
        metrics.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(9, 11))
        values = (
            ("Shares", self.selected_shares),
            ("Mark", self.selected_mark),
            ("Market Value", self.selected_value),
            ("Open P/L", self.selected_open_pnl),
            ("Day P/L", self.selected_day_pnl),
        )
        for column, (label, variable) in enumerate(values):
            metrics.columnconfigure(column, weight=1, uniform="holding-metric")
            block = ttk.Frame(metrics, style="SchwabCard.TFrame")
            block.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 0))
            ttk.Label(block, text=label, style="SchwabMuted.TLabel").pack(anchor=tk.CENTER)
            ttk.Label(
                block,
                textvariable=variable,
                style="SchwabCard.TLabel",
                font=("Segoe UI", 9, "bold"),
            ).pack(anchor=tk.CENTER, pady=(2, 0))

    def _card(
        self,
        parent: tk.Misc,
        *,
        padding: tuple[int, int] | tuple[int, int, int, int] = (0, 0),
    ) -> tk.Frame:
        if len(padding) == 2:
            padx, pady = padding
        else:
            padx = int(padding[0])
            pady = int(padding[1])
        return tk.Frame(
            parent,
            background=SURFACE,
            highlightbackground=BORDER,
            highlightthickness=1,
            bd=0,
            padx=padx,
            pady=pady,
        )

    def _table_column(
        self,
        table: ttk.Treeview,
        column: str,
        heading: str,
        width: int,
        anchor: str = tk.W,
    ) -> None:
        table.heading(column, text=heading)
        table.column(column, width=width, minwidth=max(50, width // 2), anchor=anchor)

    def _ticket_entry_row(
        self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int
    ) -> ttk.Entry:
        ttk.Label(parent, text=label, style="SchwabCard.TLabel").grid(
            row=row, column=0, sticky="w", pady=2
        )
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=2)
        return entry

    def _ticket_combo_row(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        values: tuple[str, ...],
        row: int,
    ) -> ttk.Combobox:
        ttk.Label(parent, text=label, style="SchwabCard.TLabel").grid(
            row=row, column=0, sticky="w", pady=2
        )
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly")
        combo.grid(row=row, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=2)
        return combo

    def _quote_metric(
        self,
        parent: tk.Frame,
        column: int,
        heading: str,
        value: tk.StringVar,
        size: tk.StringVar | None,
        color: str,
    ) -> None:
        block = tk.Frame(parent, background=TABLE_FIELD, padx=6, pady=7)
        block.grid(row=0, column=column, sticky="nsew")
        tk.Label(
            block,
            text=heading,
            background=TABLE_FIELD,
            foreground=color,
            font=("Segoe UI", 8),
        ).pack()
        tk.Label(
            block,
            textvariable=value,
            background=TABLE_FIELD,
            foreground=color,
            font=("Segoe UI", 12, "bold"),
        ).pack()
        if size is not None:
            tk.Label(
                block,
                textvariable=size,
                background=TABLE_FIELD,
                foreground=MUTED_TEXT,
                font=("Segoe UI", 7),
            ).pack()

    def _orders_table(self, parent: ttk.Frame) -> ttk.Treeview:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        table = ttk.Treeview(
            parent,
            columns=(
                "order_id",
                "status",
                "entered",
                "symbol",
                "side",
                "quantity",
                "type",
                "price",
                "tif",
                "effect",
                "account",
            ),
            show="headings",
            height=3,
            style="Schwab.Treeview",
            selectmode="browse",
        )
        specs = (
            ("order_id", "Order ID", 105, tk.W),
            ("status", "Status", 82, tk.W),
            ("entered", "Entered", 126, tk.W),
            ("symbol", "Symbol", 92, tk.W),
            ("side", "Side", 94, tk.W),
            ("quantity", "Qty", 55, tk.E),
            ("type", "Type", 74, tk.W),
            ("price", "Price", 88, tk.E),
            ("tif", "TIF", 62, tk.W),
            ("effect", "Position Effect", 102, tk.W),
            ("account", "Account", 90, tk.W),
        )
        for name, heading, width, anchor in specs:
            self._table_column(table, name, heading, width, anchor)
        yscroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=table.yview)
        xscroll = ttk.Scrollbar(parent, orient=tk.HORIZONTAL, command=table.xview)
        self._order_xscrolls.append(xscroll)
        table.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        table.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        return table

    def _sync(self) -> None:
        if self._sync_in_progress:
            return
        self._sync_in_progress = True
        self.sync_status.set("Syncing")
        self.status_dot.set_tone("warning")
        if self.sync_button is not None:
            self.sync_button.configure(state=tk.DISABLED)
        if not self._snapshots:
            self.position_status.set("Loading Schwab positions…")
            self.cash_status.set("Loading Schwab cash balances…")
        self._run_background(
            self.snapshot_loader,
            self._finish_sync,
            self._finish_sync_error,
        )

    def _finish_sync(
        self,
        result: PortfolioSnapshot | DucketBucketSnapshot | Sequence[PortfolioSnapshot],
    ) -> None:
        snapshots = normalize_snapshot_result(result)
        self.show_snapshots(snapshots)
        self._sync_in_progress = False
        self.sync_status.set("Connected")
        self.status_dot.set_tone("success")
        if self.sync_button is not None:
            self.sync_button.configure(state=tk.NORMAL)

    def _finish_sync_error(self, exc: Exception, *, show_dialog: bool = True) -> None:
        self._sync_in_progress = False
        self.sync_status.set("Sync failed")
        self.status_dot.set_tone("danger")
        if self.sync_button is not None:
            self.sync_button.configure(state=tk.NORMAL)
        if not self._snapshots:
            detail = f"Unable to load Schwab data: {type(exc).__name__}: {exc}"
            self.position_status.set(detail)
            self.cash_status.set(detail)
        if show_dialog:
            messagebox.showerror("Sync Failed", f"{type(exc).__name__}: {exc}")

    def show_snapshots(self, snapshots: Sequence[PortfolioSnapshot]) -> None:
        self._snapshots = tuple(snapshots)
        self._equity_rows = equity_holding_views(self._snapshots)
        choices = account_filter_choices(self._snapshots)
        self.account_combo.configure(values=choices)
        current = self.account_filter.get()
        if current not in choices:
            self.account_filter.set(choices[0])
        self._render_summary()
        self._render_portfolio()
        self._render_cash()
        synced = [snapshot.synced_at for snapshot in self._snapshots if snapshot.synced_at is not None]
        self.last_sync.set(
            f"Last sync: {_format_sync_time(max(synced))}" if synced else "Last sync: unavailable"
        )

    def _show_bucket(self, bucket: DucketBucketSnapshot) -> None:
        self.show_snapshots(tuple(bucket.snapshots))
        self.sync_status.set("Connected")
        self.status_dot.set_tone("success")

    def _show_error(self, exc: Exception) -> None:
        self._finish_sync_error(exc)

    def _render_summary(self) -> None:
        summary = schwab_equity_summary(self._snapshots)
        self._summary_values["net"].set(_money(summary.net_liquidation))
        self._summary_values["cash"].set(_money(summary.cash_and_sweep))
        self._summary_values["equities"].set(_money(summary.stocks_and_etfs))
        self._summary_values["open"].set(_signed_money_or_dash(summary.open_pnl.value))
        self._summary_values["day"].set(_signed_money_or_dash(summary.day_pnl.value))
        self._summary_helpers["open"].set(_coverage_text(summary.open_pnl))
        self._summary_helpers["day"].set(_coverage_text(summary.day_pnl))
        self.summary_value_labels["net"].configure(foreground=TEXT)
        self.summary_value_labels["cash"].configure(foreground=_value_color(summary.cash_and_sweep))
        self.summary_value_labels["equities"].configure(foreground=_value_color(summary.stocks_and_etfs))
        self.summary_value_labels["open"].configure(foreground=_value_color(summary.open_pnl.value))
        self.summary_value_labels["day"].configure(foreground=_value_color(summary.day_pnl.value))
        segments = safe_allocation_segments(summary.cash_and_sweep, summary.stocks_and_etfs)
        self.allocation_bar.set_values(summary.cash_and_sweep, summary.stocks_and_etfs)
        if segments is None:
            self.allocation_cash.set(f"Cash & Sweep {_money(summary.cash_and_sweep)}")
            self.allocation_equities.set(f"Stocks / ETFs {_money(summary.stocks_and_etfs)}")
            self.allocation_status.set("Allocation unavailable for negative or nonpositive net values")
            self.allocation_status_label.grid()
        else:
            self.allocation_cash.set(
                f"Cash & Sweep {_money(summary.cash_and_sweep)} ({segments.cash_fraction:.1%})"
            )
            self.allocation_equities.set(
                f"Stocks / ETFs {_money(summary.stocks_and_etfs)} ({segments.equities_fraction:.1%})"
            )
            self.allocation_status.set("")
            self.allocation_status_label.grid_remove()

    def _render_portfolio(self) -> None:
        table = self.holdings_table
        if table is None:
            return
        prior_key = self._selected_holding_key
        self._clear_table(table)
        self._position_by_item_id = {}
        self._row_icons = {}
        filtered = filter_equity_holding_views(
            self._equity_rows,
            account=self.account_filter.get(),
            asset_type=self.asset_filter.get(),
        )
        for row in filtered:
            icon = self._tree_mark(row.symbol)
            item_id = table.insert(
                "",
                tk.END,
                image=icon,
                values=(
                    row.symbol,
                    row.asset_type,
                    _number_text(row.holding.quantity),
                    _money(row.holding.price),
                    _money(row.holding.value),
                    _signed_money_or_dash(row.holding.unrealized_pnl),
                    _signed_money_or_dash(row.holding.day_pnl),
                ),
            )
            self._position_by_item_id[str(item_id)] = row
            self._row_icons[str(item_id)] = icon
            if row.identity == prior_key:
                table.selection_set(item_id)
                table.focus(item_id)
        if not self._equity_rows:
            self.position_status.set("No Stock/ETF positions were reported.")
        elif not filtered:
            self.position_status.set("No positions match the selected account and asset type.")
        else:
            self.position_status.set(
                f"Showing {len(filtered)} of {len(self._equity_rows)} Stock/ETF positions"
            )
        value = round(sum(row.holding.value for row in filtered), 2)
        open_pnl = _pnl_summary(filtered, "unrealized_pnl")
        day_pnl = _pnl_summary(filtered, "day_pnl")
        self.portfolio_footer.set(
            f"{len(filtered)} visible · Market Value {_money(value)} · "
            f"Open P/L {_signed_money_or_dash(open_pnl.value)} · "
            f"Day P/L {_signed_money_or_dash(day_pnl.value)}"
        )
        if prior_key and all(row.identity != prior_key for row in filtered):
            self._clear_selected_holding()

    def _render_cash(self) -> None:
        table = self.cash_table
        if table is None:
            return
        self._clear_table(table)
        self._cash_by_item_id = {}
        account = self.account_filter.get()
        rows: list[tuple[str, CashBalance]] = []
        for snapshot in self._snapshots:
            if account not in {"", ALL_ACCOUNTS, NO_ACCOUNTS} and snapshot.account_label != account:
                continue
            rows.extend((snapshot.account_label, cash) for cash in snapshot.cash)
        for account_label, cash in rows:
            item_id = table.insert(
                "",
                tk.END,
                values=(
                    account_label,
                    cash.bucket,
                    cash.symbol,
                    _number_text(cash.amount),
                    _money(cash.value),
                ),
            )
            self._cash_by_item_id[str(item_id)] = (account_label, cash)
        self.cash_status.set(
            f"{len(rows)} cash balance{'s' if len(rows) != 1 else ''} shown"
            if rows
            else "No cash balances were reported for this account selection."
        )

    def _switch_portfolio_view(self) -> None:
        if self.portfolio_view.get() == "Cash":
            self.positions_view.grid_remove()
            self.cash_view.grid()
            self._render_cash()
        else:
            self.cash_view.grid_remove()
            self.positions_view.grid()

    def _filters_changed(self, _event: object | None = None) -> None:
        self._render_portfolio()
        self._render_cash()

    def _use_selected_holding(self, _event: object | None) -> None:
        table = self.holdings_table
        if table is None:
            return
        selected = table.selection()
        if not selected:
            return
        row = getattr(self, "_position_by_item_id", {}).get(str(selected[0]))
        if row is None:
            values = table.item(selected[0], "values")
            if len(values) < 3:
                return
            bucket = str(values[1]).strip().upper()
            symbol = str(values[2]).strip().upper()
            if bucket in EQUITY_BUCKETS and symbol:
                self.stock_symbol.set(symbol)
            return
        self._selected_holding_key = row.identity
        self._ticket_source_key = row.identity
        self.stock_symbol.set(row.symbol)
        self.selected_mark_widget.show(row.symbol)
        self.selected_holding_title.set(f"{row.symbol} · {row.account_label}")
        self.selected_holding_detail.set("Ticket populated from this exact holding.")
        self.selected_shares.set(_number_text(row.holding.quantity))
        self.selected_mark.set(_money(row.holding.price))
        self.selected_value.set(_money(row.holding.value))
        self.selected_open_pnl.set(_signed_money_or_dash(row.holding.unrealized_pnl))
        self.selected_day_pnl.set(_signed_money_or_dash(row.holding.day_pnl))
        self._request_quote_now()

    def _clear_selected_holding(self) -> None:
        self._selected_holding_key = None
        self.selected_mark_widget.show("")
        self.selected_holding_title.set("No holding selected")
        self.selected_holding_detail.set("The current filter no longer includes the prior selection.")
        for variable in (
            self.selected_shares,
            self.selected_mark,
            self.selected_value,
            self.selected_open_pnl,
            self.selected_day_pnl,
        ):
            variable.set("--")

    def _refresh_selected_orders(self) -> None:
        if self.orders_notebook.index(self.orders_notebook.select()) == 0:
            self._load_open_orders()
        else:
            self._load_recent_orders()

    def _load_open_orders(self) -> None:
        self._set_order_status("open", "Loading open Stock/ETF orders…")
        self._run_background(
            lambda: self.session_factory().get_open_orders(),
            lambda orders: self._show_orders(self.open_orders_table, orders),
            lambda exc: self._orders_failed("Open Orders", exc),
        )

    def _load_recent_orders(self) -> None:
        self._set_order_status("recent", "Loading recent Stock/ETF orders…")
        self._run_background(
            lambda: self.session_factory().get_recent_orders(),
            lambda orders: self._show_orders(self.recent_orders_table, orders),
            lambda exc: self._orders_failed("Recent Orders", exc),
        )

    def _show_orders(self, table: ttk.Treeview | None, orders: object) -> None:
        if table is None:
            return
        self._clear_table(table)
        target = self.schwab_open_order_by_item_id if table is self.open_orders_table else self._recent_order_by_item_id
        target.clear()
        rows = tuple(schwab_order_row(order) for order in equity_only_orders(orders))
        for row in rows:
            item_id = table.insert("", tk.END, values=row.values())
            target[str(item_id)] = dict(row.order)
            # Retain the exact object when it is already a dict; order editing and
            # cancellation must route to the broker row selected by the user.
            if isinstance(row.order, dict):
                target[str(item_id)] = row.order
        kind = "open" if table is self.open_orders_table else "recent"
        hidden_count = len(orders) - len(rows) if isinstance(orders, list) else 0
        status_variable = getattr(self, "order_status", None)
        if rows:
            suffix = f" · {hidden_count} non-equity order{'s' if hidden_count != 1 else ''} hidden" if hidden_count else ""
            status_text = f"{len(rows)} {kind} Stock/ETF order{'s' if len(rows) != 1 else ''}{suffix}"
        else:
            status_text = (
                f"No {kind} Stock/ETF orders."
                + (f" {hidden_count} non-equity order{'s were' if hidden_count != 1 else ' was'} hidden." if hidden_count else "")
            )
        if hasattr(self, "_order_status_by_kind"):
            self._set_order_status(kind, status_text)
        elif status_variable is not None:
            status_variable.set(status_text)
        self._clear_order_selection()

    def _orders_failed(self, title: str, exc: Exception) -> None:
        kind = "open" if title == "Open Orders" else "recent"
        self._set_order_status(kind, f"{title} unavailable: {type(exc).__name__}: {exc}")
        messagebox.showerror(f"{title} Failed", f"{type(exc).__name__}: {exc}")

    def _use_selected_schwab_order(self, event: object) -> None:
        table = getattr(event, "widget", None)
        if table not in (self.open_orders_table, self.recent_orders_table):
            return
        selected = table.selection()
        if not selected:
            self._clear_order_selection()
            return
        values = table.item(selected[0], "values")
        order_id = str(values[0]).strip() if values else ""
        self.order_id.set(order_id)
        source = self.schwab_open_order_by_item_id if table is self.open_orders_table else self._recent_order_by_item_id
        self._selected_order = source.get(str(selected[0]))
        other = self.recent_orders_table if table is self.open_orders_table else self.open_orders_table
        if other is not None:
            other_selected = other.selection()
            if other_selected:
                other.selection_remove(*other_selected)
        row = schwab_order_row(self._selected_order) if self._selected_order is not None else None
        if row is None:
            self._clear_order_selection()
            return
        selected_status = getattr(self, "selected_order_status", None)
        if selected_status is not None:
            selected_status.set(f"Selected {row.order_id} · {row.symbol} · {row.status}")
        modify_button = getattr(self, "modify_order_button", None)
        if modify_button is not None:
            modify_button.configure(
                state=tk.NORMAL if table is self.open_orders_table and row.can_modify else tk.DISABLED
            )
        cancel_button = getattr(self, "cancel_order_button", None)
        if cancel_button is not None:
            cancel_button.configure(
                state=tk.NORMAL if table is self.open_orders_table and row.can_cancel else tk.DISABLED
            )

    def _selected_schwab_open_order(self) -> dict[str, object] | None:
        if self.open_orders_table is None:
            return None
        selected = self.open_orders_table.selection()
        if not selected:
            return None
        order = self.schwab_open_order_by_item_id.get(str(selected[0]))
        return order if isinstance(order, dict) else None

    def _clear_order_selection(self) -> None:
        self._selected_order = None
        order_id = getattr(self, "order_id", None)
        if order_id is not None:
            order_id.set("")
        selected_status = getattr(self, "selected_order_status", None)
        if selected_status is not None:
            selected_status.set("No order selected")
        modify_button = getattr(self, "modify_order_button", None)
        if modify_button is not None:
            modify_button.configure(state=tk.DISABLED)
        cancel_button = getattr(self, "cancel_order_button", None)
        if cancel_button is not None:
            cancel_button.configure(state=tk.DISABLED)

    def _orders_tab_changed(self, _event: object | None = None) -> None:
        self._clear_order_selection()
        kind = self._active_orders_kind()
        self.order_status.set(self._order_status_by_kind[kind])

    def _active_orders_kind(self) -> str:
        try:
            return "open" if self.orders_notebook.index(self.orders_notebook.select()) == 0 else "recent"
        except tk.TclError:
            return "open"

    def _set_order_status(self, kind: str, text: str) -> None:
        self._order_status_by_kind[kind] = text
        if self._active_orders_kind() == kind:
            self.order_status.set(text)

    def _cancel_selected_order(self) -> None:
        order = self._selected_schwab_open_order()
        if order is None or not is_equity_order(order):
            messagebox.showinfo("Cancel Schwab Order", "Select an open Stock/ETF order first.")
            return
        row = schwab_order_row(order)
        if not row.can_cancel or row.order_id == "--":
            messagebox.showerror("Cancel Schwab Order", "The selected order is not cancelable.")
            return
        if not messagebox.askyesno(
            "Confirm Schwab Order Cancellation",
            f"Cancel LIVE Schwab order {row.order_id} for {row.symbol}?\n\n"
            "This sends a cancellation request to Schwab.",
        ):
            return
        if self.cancel_order_button is not None:
            self.cancel_order_button.configure(state=tk.DISABLED)

        def succeeded(result: object) -> None:
            self._load_open_orders()
            messagebox.showinfo(
                "Schwab Order Canceled",
                f"Cancellation requested for order {row.order_id} ({row.symbol}).\n\n"
                f"Response: {result if result not in (None, '') else 'Accepted'}",
            )

        def failed(exc: Exception) -> None:
            if self.cancel_order_button is not None:
                self.cancel_order_button.configure(state=tk.NORMAL)
            messagebox.showerror("Cancel Order Failed", f"{type(exc).__name__}: {exc}")

        self._run_background(
            lambda: self.session_factory().cancel_order(row.order_id),
            succeeded,
            failed,
        )

    def _edit_selected_schwab_open_order(self, event: object | None = None) -> None:
        if event is not None and getattr(event, "widget", None) in (
            self.open_orders_table,
            self.recent_orders_table,
        ):
            self._use_selected_schwab_order(event)
        order = self._selected_schwab_open_order()
        if order is None:
            messagebox.showinfo("Modify Schwab Order", "Select an open Stock/ETF order first.")
            return
        try:
            edit = schwab_stock_order_edit(order)
        except Exception as exc:
            messagebox.showerror("Modify Schwab Order", f"{type(exc).__name__}: {exc}")
            return
        self._open_order_editor(edit)

    def _open_order_editor(self, edit: object) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Modify Schwab Stock / ETF Order {edit.order_id}")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        body.columnconfigure(1, weight=1)
        variables = {
            "order_type": tk.StringVar(master=dialog, value=edit.order_type),
            "tif": tk.StringVar(master=dialog, value=edit.time_in_force),
            "effect": tk.StringVar(master=dialog, value=edit.position_effect),
            "quantity": tk.StringVar(master=dialog, value=str(edit.quantity)),
            "price": tk.StringVar(master=dialog, value=edit.price),
            "stop": tk.StringVar(master=dialog, value=edit.stop_price),
            "special": tk.StringVar(master=dialog, value=edit.special_instruction),
        }
        ttk.Label(body, text="Edit this working order. Schwab creates a replacement Order ID.").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10)
        )
        for row_index, (label, value) in enumerate(
            (("Order ID", edit.order_id), ("Status", _humanize(edit.status)), ("Symbol", edit.symbol), ("Side", _humanize(edit.instruction))),
            start=1,
        ):
            ttk.Label(body, text=label).grid(row=row_index, column=0, sticky="w", padx=(0, 12), pady=3)
            ttk.Label(body, text=value).grid(row=row_index, column=1, sticky="w", pady=3)
        ttk.Separator(body, orient=tk.HORIZONTAL).grid(row=5, column=0, columnspan=2, sticky="ew", pady=8)

        def combo(row: int, label: str, key: str, values: tuple[str, ...]) -> ttk.Combobox:
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=4)
            widget = ttk.Combobox(body, textvariable=variables[key], values=values, state="readonly", width=30)
            widget.grid(row=row, column=1, sticky="ew", pady=4)
            return widget

        order_type_box = combo(6, "Order Type", "order_type", SCHWAB_EQUITY_ORDER_TYPE_CHOICES)
        combo(7, "Time in Force", "tif", SCHWAB_EQUITY_TIME_IN_FORCE_CHOICES)
        combo(8, "Position Effect", "effect", SCHWAB_EQUITY_POSITION_EFFECT_CHOICES)
        ttk.Label(body, text="Quantity").grid(row=9, column=0, sticky="w", padx=(0, 12), pady=4)
        ttk.Entry(body, textvariable=variables["quantity"]).grid(row=9, column=1, sticky="ew", pady=4)
        ttk.Label(body, text="Limit Price").grid(row=10, column=0, sticky="w", padx=(0, 12), pady=4)
        price_entry = ttk.Entry(body, textvariable=variables["price"])
        price_entry.grid(row=10, column=1, sticky="ew", pady=4)
        ttk.Label(body, text="Stop Price").grid(row=11, column=0, sticky="w", padx=(0, 12), pady=4)
        stop_entry = ttk.Entry(body, textvariable=variables["stop"])
        stop_entry.grid(row=11, column=1, sticky="ew", pady=4)
        combo(12, "Special Instruction", "special", SCHWAB_EQUITY_SPECIAL_INSTRUCTION_CHOICES)
        ttk.Label(
            body,
            text="Only unfilled, single-leg orders marked editable by Schwab can be replaced here.",
        ).grid(row=13, column=0, columnspan=2, sticky="w", pady=(8, 0))
        buttons = ttk.Frame(body)
        buttons.grid(row=14, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        buttons.columnconfigure((0, 1), weight=1)
        close_button = ttk.Button(buttons, text="Close", command=dialog.destroy)
        close_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        submit_button = ttk.Button(buttons, text="Replace Order", style="SchwabPrimary.TButton")
        submit_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))

        def update_price_fields(_event: object | None = None) -> None:
            order_type = variables["order_type"].get().strip().upper()
            price_entry.configure(state=tk.NORMAL if order_type in {"LIMIT", "STOP_LIMIT"} else tk.DISABLED)
            stop_entry.configure(state=tk.NORMAL if order_type in {"STOP", "STOP_LIMIT"} else tk.DISABLED)

        def submit_edit() -> None:
            try:
                payload = build_schwab_stock_replacement_payload(
                    edit,
                    order_type=variables["order_type"].get(),
                    time_in_force=variables["tif"].get(),
                    position_effect=variables["effect"].get(),
                    quantity=variables["quantity"].get(),
                    price=variables["price"].get(),
                    stop_price=variables["stop"].get(),
                    special_instruction=variables["special"].get(),
                )
                if not messagebox.askyesno(
                    "Confirm Schwab Order Replacement",
                    order_replacement_confirmation_message(edit.order_id, payload),
                    parent=dialog,
                ):
                    return
            except Exception as exc:
                messagebox.showerror("Schwab Order Replacement Failed", f"{type(exc).__name__}: {exc}", parent=dialog)
                return
            submit_button.configure(state=tk.DISABLED)
            close_button.configure(state=tk.DISABLED)
            dialog.protocol("WM_DELETE_WINDOW", lambda: None)

            def succeeded(location: str | None) -> None:
                dialog.destroy()
                self._load_open_orders()
                messagebox.showinfo("Schwab Order Replaced", order_replaced_message(edit.order_id, payload, location))

            def failed(exc: Exception) -> None:
                submit_button.configure(state=tk.NORMAL)
                close_button.configure(state=tk.NORMAL)
                dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
                messagebox.showerror("Schwab Order Replacement Failed", f"{type(exc).__name__}: {exc}", parent=dialog)

            self._run_background(
                lambda: self.session_factory().replace_order(edit.order_id, payload),
                succeeded,
                failed,
            )

        submit_button.configure(command=submit_edit)
        order_type_box.bind("<<ComboboxSelected>>", update_price_fields)
        update_price_fields()
        order_type_box.focus_set()

    def _stock_order_payload(self) -> dict[str, object]:
        return build_schwab_stock_order_payload(
            symbol=self.stock_symbol.get(),
            instruction=self.stock_side.get(),
            order_type=self.stock_order_type.get(),
            time_in_force=self.stock_tif.get(),
            position_effect=self.stock_position_effect.get(),
            quantity=self.stock_quantity.get(),
            price=self.stock_entry_limit.get(),
            stop_price=self.stock_stop_price.get(),
        )

    def _submit_stock_order(self) -> None:
        if self._order_submission_in_progress:
            return
        try:
            payload = self._stock_order_payload()
            if not messagebox.askyesno("Confirm Stock / ETF Order", order_confirmation_message(payload)):
                return
        except Exception as exc:
            messagebox.showerror("Stock / ETF Order Failed", f"{type(exc).__name__}: {exc}")
            return
        self._order_submission_in_progress = True
        if self.review_button is not None:
            self.review_button.configure(state=tk.DISABLED)

        def finished() -> None:
            self._order_submission_in_progress = False
            if self.review_button is not None:
                self.review_button.configure(state=tk.NORMAL)

        def succeeded(location: str | None) -> None:
            finished()
            self._load_open_orders()
            messagebox.showinfo("Stock / ETF Order Submitted", order_submitted_message(payload, location))

        def failed(exc: Exception) -> None:
            finished()
            messagebox.showerror("Stock / ETF Order Failed", f"{type(exc).__name__}: {exc}")

        self._run_background(lambda: self.session_factory().submit_order(payload), succeeded, failed)

    def _update_price_fields(self) -> None:
        order_type = self.stock_order_type.get().strip().upper()
        self.limit_entry.configure(state=tk.NORMAL if order_type in {"LIMIT", "STOP_LIMIT"} else tk.DISABLED)
        self.stop_entry.configure(state=tk.NORMAL if order_type in {"STOP", "STOP_LIMIT"} else tk.DISABLED)

    def _schedule_quote_request(self) -> None:
        if self._quote_job is not None:
            try:
                self.root.after_cancel(self._quote_job)
            except tk.TclError:
                pass
        self._quote_job = self.root.after(450, self._request_quote_now)

    def _request_quote_now(self) -> None:
        self._quote_job = None
        symbol = self.stock_symbol.get().strip().upper()
        self._quote_generation += 1
        generation = self._quote_generation
        self._quote = None
        self._render_quote(None)
        if not symbol:
            self.quote_status.set("Enter a symbol for a live quote.")
            return
        self.quote_status.set(f"Loading {symbol} quote…")
        self._run_background(
            lambda: self.session_factory().get_equity_quote(symbol),
            lambda payload: self._accept_quote(generation, symbol, payload),
            lambda exc: self._quote_failed(generation, symbol, exc),
        )

    def _accept_quote(self, generation: int, symbol: str, payload: object) -> bool:
        if not quote_request_is_current(
            generation,
            symbol,
            current_generation=self._quote_generation,
            current_symbol=self.stock_symbol.get(),
        ):
            return False
        quote = quote_view_from_payload(symbol, payload)
        self._quote = quote
        self._render_quote(quote)
        self.quote_status.set(
            f"{quote.symbol} · {quote.source}"
            if quote.mid is not None
            else f"{quote.symbol} quote did not include a usable midpoint."
        )
        return True

    def _quote_failed(self, generation: int, symbol: str, exc: Exception) -> bool:
        if not quote_request_is_current(
            generation,
            symbol,
            current_generation=self._quote_generation,
            current_symbol=self.stock_symbol.get(),
        ):
            return False
        self._quote = None
        self._render_quote(None)
        self.quote_status.set(f"{symbol} quote unavailable: {type(exc).__name__}: {exc}")
        return True

    def _render_quote(self, quote: EquityQuoteView | None) -> None:
        self.quote_bid.set(_quote_price(quote.bid) if quote is not None else "--")
        self.quote_mid.set(_quote_price(quote.mid) if quote is not None else "--")
        self.quote_ask.set(_quote_price(quote.ask) if quote is not None else "--")
        self.quote_bid_size.set(f"Size {_number_text(quote.bid_size)}" if quote is not None and quote.bid_size is not None else "Size --")
        self.quote_ask_size.set(f"Size {_number_text(quote.ask_size)}" if quote is not None and quote.ask_size is not None else "Size --")

    def _use_stock_mid(self) -> None:
        symbol = self.stock_symbol.get().strip().upper()
        if not symbol:
            messagebox.showwarning("Use Mid", "Stock / ETF symbol is required.")
            return
        if self._quote is not None and self._quote.symbol == symbol and self._quote.mid is not None:
            self.stock_order_type.set("LIMIT")
            self.stock_entry_limit.set(f"{self._quote.mid:.2f}")
            return

        generation = self._quote_generation + 1
        self._quote_generation = generation
        self.quote_status.set(f"Loading {symbol} quote…")

        def succeeded(payload: object) -> None:
            if self._accept_quote(generation, symbol, payload) and self._quote is not None and self._quote.mid is not None:
                self.stock_order_type.set("LIMIT")
                self.stock_entry_limit.set(f"{self._quote.mid:.2f}")
            elif self._quote is not None:
                messagebox.showerror("Use Mid", f"Quote for {symbol} did not include a usable midpoint, mark, or last price.")

        self._run_background(
            lambda: self.session_factory().get_equity_quote(symbol),
            succeeded,
            lambda exc: self._quote_failed(generation, symbol, exc),
        )

    def _tree_mark(self, symbol: str) -> tk.PhotoImage:
        path = security_mark_path(symbol)
        if path is not None:
            try:
                source = tk.PhotoImage(master=self.root, file=str(path))
            except tk.TclError:
                source = None
            if source is not None:
                factor = max(1, math.ceil(max(source.width(), source.height()) / 18))
                return source.subsample(factor, factor)
        icon = tk.PhotoImage(master=self.root, width=18, height=18)
        color = _symbol_color(symbol)
        for y in range(18):
            inset = 3 if y in {0, 17} else 1 if y in {1, 16} else 0
            if inset < 9:
                icon.put(color, to=(inset, y, 18 - inset, y + 1))
        return icon

    def _run_background(
        self,
        work: Callable[[], object],
        on_success: Callable[[object], None],
        on_error: Callable[[Exception], None],
    ) -> object:
        return self._background_runner(self.root, work, on_success, on_error)

    def _clear_table(self, table: ttk.Treeview | None) -> None:
        if table is None:
            return
        for item_id in table.get_children():
            table.delete(item_id)

    def _set_canvas_hover(self, inside: bool) -> None:
        self._inside_canvas = inside

    def _on_mousewheel(self, event: tk.Event[tk.Misc]) -> str | None:
        if not self._inside_canvas:
            return None
        delta = int(getattr(event, "delta", 0))
        if delta:
            self.canvas.yview_scroll(-1 if delta > 0 else 1, "units")
            return "break"
        return None

    def _update_scroll_region(self, _event: object | None = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_resize(self, event: tk.Event[tk.Canvas]) -> None:
        self._canvas_viewport_height = max(self.canvas.winfo_height(), 1)
        self.canvas.itemconfigure(self._body_window, width=max(event.width, 1))
        self._apply_responsive_layout()
        self.root.after_idle(self._sync_body_window_height)

    def _sync_body_window_height(self) -> None:
        """Give wide content a viewport-sized row without creating a sizing loop."""
        wide = bool(self._last_layout and self._last_layout[1])
        if wide:
            # The canvas window keeps its natural height.  A minimum on the
            # content row makes that natural height fill the visible viewport,
            # while still allowing intrinsically taller content to scroll.
            fixed_height = (
                14
                + 20
                + self.header.winfo_reqheight()
                + 12
                + self.summary_frame.winfo_reqheight()
                + 12
            )
            natural_content_height = max(
                self.portfolio_card.winfo_reqheight()
                + 10
                + self.orders_card.winfo_reqheight(),
                self.ticket_card.winfo_reqheight()
                + 10
                + self.selected_card.winfo_reqheight(),
            )
            content_height = max(
                natural_content_height,
                self._canvas_viewport_height - fixed_height,
            )
        else:
            content_height = 0
        content_height = max(content_height, 0)
        current_height = int(self.body.grid_rowconfigure(2)["minsize"])
        self.body.rowconfigure(2, minsize=content_height, weight=0)
        self.canvas.itemconfigure(self._body_window, height=0)
        if current_height != content_height:
            # A short follow-up observes final notebook/header geometry rather
            # than the provisional sizes from the first Configure event.
            self.root.after(20, self._sync_body_window_height)
        else:
            self.root.after_idle(self._update_scroll_region)

    def _apply_responsive_layout(self, *, force: bool = False) -> None:
        width = max(self.canvas.winfo_width(), self.root.winfo_width(), 1)
        layout = schwab_layout(width)
        if not force and layout == self._last_layout:
            return
        self._last_layout = layout
        summary_columns, wide = layout
        if width >= 850:
            self.holdings_xscroll.grid_remove()
        else:
            self.holdings_xscroll.grid(row=1, column=0, sticky="ew")
        for order_xscroll in self._order_xscrolls:
            if width >= 1100:
                order_xscroll.grid_remove()
            else:
                order_xscroll.grid(row=1, column=0, sticky="ew")
        for column in range(5):
            self.summary_frame.columnconfigure(column, weight=0, uniform="")
        for index, card in enumerate(self.summary_cards):
            row, column = divmod(index, summary_columns)
            card.grid(row=row, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 0), pady=(0 if row == 0 else 5, 0))
            self.summary_frame.columnconfigure(column, weight=1, uniform="summary")

        self.header_title.grid_forget()
        self.header_actions.grid_forget()
        if width >= 720:
            self.header_title.grid(row=0, column=0, sticky="w")
            self.header_actions.grid(row=0, column=1, sticky="e")
        else:
            self.header_title.grid(row=0, column=0, columnspan=2, sticky="w")
            self.header_actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self.portfolio_header.grid_forget()
        self.portfolio_filters.grid_forget()
        for row in range(3):
            self.portfolio_card.rowconfigure(row, weight=0)
        if width >= 900:
            portfolio_view_row = 1
            self.portfolio_header.grid(
                row=0, column=0, sticky="ew", padx=(12, 6), pady=(8, 4)
            )
            self.portfolio_filters.grid(
                row=0, column=1, sticky="e", padx=(6, 12), pady=(5, 4)
            )
        else:
            portfolio_view_row = 2
            self.portfolio_header.grid(
                row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(8, 4)
            )
            self.portfolio_filters.grid(
                row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=(0, 7)
            )
        self.portfolio_card.rowconfigure(portfolio_view_row, weight=1)
        for view in (self.positions_view, self.cash_view):
            view.grid(
                row=portfolio_view_row,
                column=0,
                columnspan=2,
                sticky="nsew",
                padx=12,
                pady=(0, 6),
            )
        if self.portfolio_view.get() == "Cash":
            self.positions_view.grid_remove()
        else:
            self.cash_view.grid_remove()

        self.position_status_label.grid_forget()
        self.portfolio_footer_label.grid_forget()
        if width >= 900:
            self.position_status_label.grid(row=0, column=0, sticky="w")
            self.portfolio_footer_label.grid(row=0, column=1, sticky="e")
        else:
            self.position_status_label.grid(row=0, column=0, columnspan=2, sticky="w")
            self.portfolio_footer_label.grid(
                row=1, column=0, columnspan=2, sticky="w", pady=(3, 0)
            )

        for widget in (self.portfolio_card, self.ticket_card, self.orders_card, self.selected_card):
            widget.grid_forget()
        for stack in (self.left_stack, self.right_stack):
            stack.grid_forget()
            for row in range(2):
                stack.rowconfigure(row, weight=0)
        for row in range(4):
            self.content.rowconfigure(row, weight=0)
        if wide:
            self.content.columnconfigure(0, weight=2, uniform="")
            self.content.columnconfigure(1, weight=1, uniform="")
            self.content.rowconfigure(0, weight=1)
            self.left_stack.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
            self.right_stack.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
            # Independent column stacks preserve the concept's masonry-like
            # alignment: orders follow Portfolio and Selected Holding follows
            # the naturally taller ticket instead of sharing rigid grid rows.
            self.left_stack.rowconfigure(0, weight=3)
            self.left_stack.rowconfigure(1, weight=1)
            self.right_stack.rowconfigure(0, weight=3)
            self.right_stack.rowconfigure(1, weight=1)
        else:
            self.content.columnconfigure(0, weight=1)
            self.content.columnconfigure(1, weight=0)
            self.left_stack.grid(row=0, column=0, sticky="nsew")
            self.right_stack.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self.portfolio_card.grid(row=0, column=0, sticky="nsew", pady=(0, 5))
        self.orders_card.grid(row=1, column=0, sticky="nsew", pady=(5, 0))
        self.ticket_card.grid(row=0, column=0, sticky="nsew", pady=(0, 5))
        self.selected_card.grid(row=1, column=0, sticky="nsew", pady=(5, 0))
        self.root.after_idle(self._sync_body_window_height)


def _pnl_summary(
    rows: Sequence[EquityHoldingView],
    field: str,
) -> PnlSummary:
    values = [getattr(row.holding, field) for row in rows]
    reported = [float(value) for value in values if value is not None and _is_finite_number(value)]
    return PnlSummary(
        value=round(sum(reported), 2) if reported else None,
        reported_count=len(reported),
        total_count=len(rows),
    )


def _is_finite_number(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _money(value: float) -> str:
    number = float(value)
    sign = "-" if number < 0 else ""
    return f"{sign}${abs(number):,.2f}"


def _signed_money_or_dash(value: float | None) -> str:
    if value is None or not _is_finite_number(value):
        return "--"
    number = float(value)
    if number > 0.0:
        return f"+${number:,.2f}"
    if number < 0.0:
        return f"-${abs(number):,.2f}"
    return "$0.00"


def _number_text(value: object) -> str:
    if value is None or value == "":
        return "--"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "--"
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.8f}".rstrip("0").rstrip(".")


def _quote_price(value: float | None) -> str:
    return f"{value:,.2f}" if value is not None and _is_finite_number(value) else "--"


def _value_color(value: float | None) -> str:
    if value is None or not _is_finite_number(value) or float(value) == 0.0:
        return TEXT
    return SUCCESS if float(value) > 0.0 else DANGER


def _coverage_text(summary: PnlSummary) -> str:
    if summary.total_count == 0:
        return "No visible Stock/ETF positions"
    if summary.reported_count == 0:
        return f"Unavailable for {summary.total_count} visible position{'s' if summary.total_count != 1 else ''}"
    qualifier = "Partial" if summary.is_partial else "Complete"
    return f"{qualifier} coverage · {summary.reported_count}/{summary.total_count} positions"


def _humanize(value: object) -> str:
    text = str(value or "").strip()
    return text.replace("_", " ").title() if text else "--"


def _format_sync_time(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime("%b %d, %Y %I:%M:%S %p %Z")


def _format_order_time(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "--"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%b %d %I:%M %p")


def _first_number(row: Mapping[str, object], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        try:
            value = float(row.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            return value
    return None


def _order_price_text(order: Mapping[str, object], order_type: str) -> str:
    price = _first_number(order, ("price",))
    stop = _first_number(order, ("stopPrice",))
    if order_type == "MARKET":
        return "Market"
    if order_type == "STOP_LIMIT":
        return f"{_quote_price(stop)} / {_quote_price(price)}"
    if order_type == "STOP":
        return _quote_price(stop)
    return _quote_price(price)


def _masked_account(order: Mapping[str, object]) -> str:
    account = str(
        order.get("accountNumber")
        or order.get("accountId")
        or order.get("account")
        or ""
    ).strip()
    if not account:
        return "--"
    return f"••••{account[-4:]}" if len(account) > 4 else account


def _tone_color(tone: str) -> str:
    return {
        "success": SUCCESS,
        "warning": WARNING,
        "danger": DANGER,
        "neutral": MUTED_TEXT,
    }.get(tone, MUTED_TEXT)


def _symbol_color(symbol: str) -> str:
    palette = ("#2563eb", "#0f766e", "#7c3aed", "#b45309", "#be123c", "#0369a1")
    cleaned = symbol.strip().upper()
    return palette[sum(ord(character) for character in cleaned) % len(palette)] if cleaned else BORDER


__all__ = [
    "ALL_ACCOUNTS",
    "ASSET_FILTER_CHOICES",
    "AllocationSegments",
    "EquityHoldingView",
    "EquityQuoteView",
    "PnlSummary",
    "SchwabDucketsTab",
    "SchwabEquitySummary",
    "SchwabOrderRow",
    "account_filter_choices",
    "equity_holding_views",
    "equity_only_orders",
    "filter_equity_holding_views",
    "is_equity_order",
    "normalize_snapshot_result",
    "quote_request_is_current",
    "quote_view_from_payload",
    "safe_allocation_segments",
    "schwab_equity_summary",
    "schwab_layout",
    "schwab_order_row",
    "security_mark_path",
    "security_monogram",
]
