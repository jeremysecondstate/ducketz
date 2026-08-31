"""Offline visual fixture for the equities-only Schwab Duckets tab.

Usage examples:
    python tests/visual_schwab_duckets_fixture.py --size 1672x941 \
        --capture artifacts/validation/schwab-duckets-equities-wide.png
    python tests/visual_schwab_duckets_fixture.py --size 1180x760 \
        --capture artifacts/validation/schwab-duckets-equities-1180.png
    python tests/visual_schwab_duckets_fixture.py --size 1906x1030 \
        --position-count 17 \
        --capture artifacts/validation/schwab-duckets-equities-1906.png

Every account, position, quote, and order is deterministic fixture data. The
fixture does not read credentials, perform network calls, or permit mutations.
"""

from __future__ import annotations

import argparse
import sys
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from tkinter import ttk


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.models.portfolio import CashBalance, Holding, PortfolioSnapshot
from app.ui.ducket_bucket import DucketBucketApp
from app.ui.schwab_duckets import SchwabDucketsTab
from visual_option_management_fixture import _write_window_png


class FakeSchwabSession:
    def __init__(self, open_orders: list[dict[str, object]], recent_orders: list[dict[str, object]]) -> None:
        self._open_orders = open_orders
        self._recent_orders = recent_orders

    def get_equity_quote(self, symbol: str) -> dict[str, object]:
        quotes = {
            "AAPL": {"bidPrice": 183.48, "askPrice": 183.52, "mark": 183.50, "bidSize": 100, "askSize": 200},
            "MSFT": {"bidPrice": 416.55, "askPrice": 416.61, "mark": 416.58, "bidSize": 60, "askSize": 45},
            "SPY": {"bidPrice": 530.10, "askPrice": 530.14, "mark": 530.12},
            "QQQ": {"bidPrice": 458.18, "askPrice": 458.26, "mark": 458.22},
            "NVDA": {"bidPrice": 945.05, "askPrice": 945.19, "mark": 945.12},
            "IWM": {"bidPrice": 206.75, "askPrice": 206.81, "mark": 206.78},
            "ZZZZ": {"bidPrice": 17.20, "askPrice": 17.30, "mark": 17.25},
        }
        clean_symbol = symbol.strip().upper()
        if clean_symbol not in quotes:
            raise RuntimeError(f"Fixture quote unavailable for {clean_symbol}")
        return quotes[clean_symbol]

    def get_open_orders(self) -> list[dict[str, object]]:
        return self._open_orders

    def get_recent_orders(self) -> list[dict[str, object]]:
        return self._recent_orders

    def submit_order(self, _payload: dict[str, object]) -> None:
        raise AssertionError("The visual fixture must never submit a Schwab order.")

    def replace_order(self, _order_id: str, _payload: dict[str, object]) -> None:
        raise AssertionError("The visual fixture must never replace a Schwab order.")

    def cancel_order(self, _order_id: str) -> None:
        raise AssertionError("The visual fixture must never cancel a Schwab order.")


def _immediate_background(
    _root: tk.Misc,
    work: object,
    on_success: object,
    on_error: object,
) -> None:
    try:
        result = work()
    except Exception as exc:
        on_error(exc)
    else:
        on_success(result)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", default="1672x941")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--scroll-bottom", action="store_true")
    parser.add_argument("--position-count", type=int, choices=(6, 17), default=6)
    parser.add_argument(
        "--state",
        choices=("normal", "empty", "error", "unknown"),
        default="normal",
    )
    args = parser.parse_args()

    root = tk.Tk()
    root.title("Duckets — fake-only Schwab Duckets fixture")
    root.geometry(args.size)
    root.configure(background="#08111f")
    DucketBucketApp._apply_theme(SimpleNamespace(root=root))

    notebook = ttk.Notebook(root)
    notebook.pack(fill=tk.BOTH, expand=True)
    frames: dict[str, ttk.Frame] = {}
    for title in (
        "Rolling Forecasts",
        "Options Strategies",
        "Schwab Duckets",
        "Hyperliquid Duckets",
    ):
        frame = ttk.Frame(notebook)
        frames[title] = frame
        notebook.add(frame, text=title)

    snapshots = _fixture_snapshots(
        unknown=args.state == "unknown",
        position_count=args.position_count,
    )
    open_orders = _fixture_open_orders()
    recent_orders = _fixture_recent_orders()
    session = FakeSchwabSession(open_orders, recent_orders)
    tab = SchwabDucketsTab(
        root,
        frames["Schwab Duckets"],
        snapshot_loader=lambda: snapshots,
        session_factory=lambda: session,
        background_runner=_immediate_background,
    )
    root.fixture_schwab_tab = tab  # type: ignore[attr-defined]
    notebook.select(frames["Schwab Duckets"])

    if args.state == "empty":
        tab._finish_sync(_empty_snapshot())
        tab._show_orders(tab.open_orders_table, [])
        tab._show_orders(tab.recent_orders_table, [])
    elif args.state == "error":
        tab._finish_sync_error(ConnectionError("fixture Schwab connection unavailable"), show_dialog=False)
    else:
        tab._finish_sync(snapshots)
        tab._show_orders(tab.open_orders_table, open_orders)
        tab._show_orders(tab.recent_orders_table, recent_orders)
        root.after(
            80,
            lambda: _select_position(tab, "ZZZZ" if args.state == "unknown" else None),
        )

    if args.capture is not None:
        root.after(
            900,
            lambda: _capture_and_exit(
                root,
                args.capture,
                scroll_bottom=args.scroll_bottom,
            ),
        )
    root.mainloop()


def _select_position(tab: SchwabDucketsTab, symbol: str | None) -> None:
    if tab.holdings_table is None:
        return
    children = tab.holdings_table.get_children()
    if not children:
        return
    selected = children[0]
    if symbol is not None:
        selected = next(
            (
                item_id
                for item_id in children
                if str(tab.holdings_table.item(item_id, "values")[0]).strip().upper()
                == symbol.strip().upper()
            ),
            selected,
        )
    tab.holdings_table.selection_set(selected)
    tab.holdings_table.focus(selected)
    tab._use_selected_holding(None)


def _capture_and_exit(root: tk.Tk, path: Path, *, scroll_bottom: bool) -> None:
    try:
        if scroll_bottom:
            tab = getattr(root, "fixture_schwab_tab", None)
            canvas = getattr(tab, "canvas", None)
            if canvas is not None:
                root.update_idletasks()
                canvas.yview_moveto(1.0)
                root.update()
        root.deiconify()
        root.lift()
        root.focus_force()
        root.attributes("-topmost", True)
        root.update_idletasks()
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_window_png(root.winfo_id(), path)
    finally:
        root.destroy()


def _fixture_snapshots(
    *,
    unknown: bool = False,
    position_count: int = 6,
) -> tuple[PortfolioSnapshot, ...]:
    synced_at = datetime(2026, 8, 29, 17, 21, 34, tzinfo=timezone.utc)
    first_symbol = "ZZZZ" if unknown else "AAPL"
    primary_holdings = [
        _holding(first_symbol, 100, 183.74 if not unknown else 17.25, 18_374.0 if not unknown else 1_725.0, 1_274.0, 274.0, "Stock"),
        _holding("MSFT", 50, 416.58, 20_829.0, 829.5, 204.0, "Stock"),
        _holding("SPY", 150, 530.12, 79_518.0, 1_218.0, 415.0, "ETF"),
        _holding("QQQ", 75, 458.22, 34_366.5, -153.0, -67.5, "ETF"),
        _holding("AAPL  260918C00200000", 1, 9.50, 950.0, 130.0, -20.0, "Option"),
    ]
    ira_holdings = [
        _holding("NVDA", 30, 945.12, 28_353.6, 642.0, 198.0, "Stock"),
        _holding("IWM", 200, 206.78, 41_356.0, 64.06, -139.29, "ETF"),
    ]
    extra_holdings = [
        _holding("AMD", 24, 162.40, 3_897.60, 218.40, 42.24, "Stock"),
        _holding("AMZN", 18, 178.20, 3_207.60, 117.00, -21.60, "Stock"),
        _holding("EWY", 22, 74.65, 1_642.30, 82.50, 14.30, "ETF"),
        _holding("GOOGL", 20, 171.30, 3_426.00, 186.00, 24.00, "Stock"),
        _holding("MRNA", 8, 137.99, 1_103.92, -23.55, -7.30, "Stock"),
        _holding("MU", 30, 142.25, 4_267.50, 173.40, 31.20, "Stock"),
        _holding("NBIS", 25, 20.918, 522.95, -73.12, -13.50, "Stock"),
        _holding("SNDK", 20, 148.40, 2_968.00, 184.00, 22.00, "Stock"),
        _holding("TENB", 10, 41.75, 417.50, -18.50, 3.20, "Stock"),
        _holding("VXUS", 30, 87.52, 2_625.60, 112.25, 0.00, "ETF"),
        _holding("ZETA", 10, 30.54, 305.40, 92.90, -1.40, "Stock"),
    ]
    requested_extras = max(position_count - 6, 0)
    selected_extras = extra_holdings[:requested_extras]
    primary_holdings.extend(selected_extras)
    extra_value = sum(holding.value for holding in selected_extras)
    return (
        PortfolioSnapshot(
            source="schwab",
            account_label="Schwab ••••0907",
            cash=[CashBalance("USD", 14_100.0, 14_100.0, "schwab", "Cash & sweep")],
            holdings=primary_holdings,
            synced_at=synced_at,
            status="Fixture current",
            reported_total_value=168_137.5 + extra_value,
        ),
        PortfolioSnapshot(
            source="schwab",
            account_label="Schwab IRA ••••1842",
            cash=[CashBalance("USD", 5_732.41, 5_732.41, "schwab", "Cash & sweep")],
            holdings=ira_holdings,
            synced_at=synced_at,
            status="Fixture current",
            reported_total_value=75_442.01,
        ),
    )


def _empty_snapshot() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        source="schwab",
        account_label="Schwab ••••0907",
        cash=[],
        holdings=[],
        synced_at=datetime(2026, 8, 29, 17, 21, 34, tzinfo=timezone.utc),
        status="Fixture empty",
        reported_total_value=0.0,
    )


def _holding(
    symbol: str,
    quantity: float,
    price: float,
    value: float,
    open_pnl: float | None,
    day_pnl: float | None,
    bucket: str,
) -> Holding:
    return Holding(
        symbol=symbol,
        quantity=quantity,
        price=price,
        value=value,
        source="schwab",
        bucket=bucket,
        unrealized_pnl=open_pnl,
        day_pnl=day_pnl,
    )


def _fixture_open_orders() -> list[dict[str, object]]:
    return [
        _order(1001, "WORKING", "2026-08-29T17:20:11Z", "AAPL", "BUY", 10, "LIMIT", 183.50, "DAY", True),
        _order(1002, "WORKING", "2026-08-29T17:18:45Z", "MSFT", "BUY", 5, "LIMIT", 416.00, "DAY", True),
        _order(1003, "PENDING_ACTIVATION", "2026-08-29T17:16:02Z", "SPY", "SELL", 20, "STOP_LIMIT", 528.80, "GOOD_TILL_CANCEL", True, stop_price=529.00),
        _order(1004, "WORKING", "2026-08-29T17:15:00Z", "AAPL  260918C00200000", "SELL_TO_CLOSE", 1, "LIMIT", 10.25, "DAY", True, asset_type="OPTION"),
    ]


def _fixture_recent_orders() -> list[dict[str, object]]:
    return [
        _order(900, "FILLED", "2026-08-28T19:31:00Z", "NVDA", "BUY", 10, "LIMIT", 930.00, "DAY", False),
        _order(899, "CANCELED", "2026-08-28T18:15:00Z", "QQQ", "SELL", 5, "LIMIT", 459.25, "DAY", False),
    ]


def _order(
    order_id: int,
    status: str,
    entered: str,
    symbol: str,
    side: str,
    quantity: int,
    order_type: str,
    price: float,
    duration: str,
    editable: bool,
    *,
    stop_price: float | None = None,
    asset_type: str = "EQUITY",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "orderId": order_id,
        "status": status,
        "enteredTime": entered,
        "editable": editable,
        "filledQuantity": 0,
        "orderType": order_type,
        "price": price,
        "session": "NORMAL",
        "duration": duration,
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "NONE",
        "accountNumber": "12345678",
        "orderLegCollection": [
            {
                "instruction": side,
                "positionEffect": "AUTOMATIC",
                "quantity": quantity,
                "instrument": {"symbol": symbol, "assetType": asset_type},
            }
        ],
    }
    if stop_price is not None:
        payload["stopPrice"] = stop_price
    return payload


if __name__ == "__main__":
    main()
