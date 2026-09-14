"""Offline Concept B fixture: deterministic data, with network and trading blocked."""
from __future__ import annotations

import argparse
import math
import sys
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from tkinter import ttk

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import app.ui.ducket_bucket as ui
import app.ui.hyperliquid_workspace as workspace
from app.models.portfolio import CashBalance, Holding, PortfolioSnapshot
from app.services.aggregate import DucketBucketSnapshot
from app.services.hyperliquid_markets import DEFAULT_MARKETS
from visual_option_management_fixture import _write_window_png


def _blocked(*args, **kwargs):
    raise AssertionError("The visual fixture cannot use network or trading services.")


def _snapshots() -> tuple[PortfolioSnapshot, ...]:
    """Deliberately illustrative positions; no private/live account data."""
    stamp = datetime(2026, 9, 13, 6, 47, tzinfo=timezone.utc)
    prices = {"HYPE": 79.00, "BTC": 112000.00, "ETH": 4200.00, "ZEC": 1200.00}
    changes = {"HYPE": 0.08, "BTC": 1.24, "ETH": -0.62, "ZEC": 2.1}
    common = {
        "markets": {coin: {
            "coin": coin, "status": "current", "price": price,
            "change_percent_24h": changes[coin], "chart_status": "current",
            "closes_24h": [price * (1 + (i / 95 - .5) * changes[coin] / 100 + math.sin(i / 4.2) * .002) for i in range(96)],
        } for coin, price in prices.items()},
        "market_catalog": [*DEFAULT_MARKETS, "SOL"],
        "spot_catalog": {coin: {"coin": f"@{index}", "pair": f"{base}/USDC", "base": base, "mid": prices[coin]} for coin, base, index in (("HYPE", "HYPE", 107), ("BTC", "UBTC", 142), ("ETH", "UETH", 151), ("ZEC", "UZEC", 272))},
        "chain_status": {"available": True, "chain_id": 999, "block_number": 45_776_152, "gas_price_wei": 100_000_000},
        "sync_error": "",
    }
    snapshots = []
    for label, equity, available, sign in (("Jeremy", 14400, 100, 1), ("Alex", 14600, 120, -1)):
        holdings, positions = [], {}
        for coin, size, entry, pnl, liquidation in (("HYPE", 185, 83, -740, 17.25 if sign == 1 else 134.57), ("ZEC", 8, 1257.5, -460, 460 if sign == 1 else 2354.06)):
            holdings.append(Holding(f"{coin}-PERP" + ("-SHORT" if sign < 0 else ""), size, prices[coin], size * prices[coin], "hyperliquid", "Perps", unrealized_pnl=pnl * sign))
            positions[coin] = {"entry_price": entry, "signed_size": size * sign, "leverage": 10, "margin_mode": "cross", "liquidation_price": liquidation, "return_on_equity": pnl * sign / (size * prices[coin] / 10)}
        snapshots.append(PortfolioSnapshot(
            source="hyperliquid", account_label=label,
            cash=[CashBalance("USDC", 12000, 12000, "hyperliquid", "Spot"), CashBalance("USDC", equity - 12000 + sign * 1200, equity - 12000 + sign * 1200, "hyperliquid", "Perps")],
            holdings=holdings, synced_at=stamp, reported_total_value=equity,
            account_facts={**common, "spot_equity": 12000, "perp_equity": equity - 12000, "cash_usdc": 12000, "spot_available": {"USDC": 12000}, "available": available, "margin_used": equity - 12000 - available, "unrealized_pnl": -1200 * sign, "positions": positions,
                "open_orders": [{"coin": "HYPE", "oid": 41001 if sign == 1 else 41002, "side": "B" if sign == 1 else "A", "sz": "5", "limitPx": "75" if sign == 1 else "85", "orderType": "Limit", "reduceOnly": False}],
                "activity": [{"time": int(stamp.timestamp() * 1000), "coin": "ZEC", "dir": "Open Long" if sign == 1 else "Open Short", "sz": "8", "px": "1257.5", "closedPnl": "0", "fee": "4.52", "feeToken": "USDC"}],
            },
        ))
    snapshots.append(PortfolioSnapshot(
        source="hyperliquid", account_label="Clearpond", cash=[CashBalance("USDC", 410.80, 410.80, "hyperliquid", "Spot")],
        synced_at=stamp, reported_total_value=410.80,
        account_facts={**common, "unified": True, "account_mode": "unifiedAccount", "spot_equity": 410.80, "perp_equity": 0, "cash_usdc": 410.80, "spot_available": {"USDC": 410.80}, "available": 410.80, "margin_used": 0, "unrealized_pnl": 0, "positions": {}, "open_orders": [], "activity": []},
    ))
    return tuple(snapshots)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", default="1706x1000")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--scroll-bottom", action="store_true")
    parser.add_argument("--view", default="Positions", choices=("Positions", "Cash", "Open Orders", "Activity"))
    parser.add_argument("--account", default="Clearpond", choices=("Clearpond", "Jeremy", "Alex"))
    parser.add_argument("--product", default="Perp", choices=("Spot", "Perp"))
    args = parser.parse_args()
    ui.HyperliquidInfoClient = _blocked
    ui.HyperliquidExecutionAdapter = _blocked
    ui.sync_hyperliquid_portfolios = _blocked
    workspace.load_watchlist = lambda: list(DEFAULT_MARKETS)
    root = tk.Tk()
    root.title("Hyperliquid Duckets — offline preview · sample data")
    root.geometry(args.size)
    root.configure(background="#08111f")
    ui.DucketBucketApp._apply_theme(SimpleNamespace(root=root))
    notebook = ttk.Notebook(root)
    notebook.pack(fill=tk.BOTH, expand=True)
    frames = {}
    for title in ("Rolling Forecasts", "Options Strategies", "Schwab Duckets", "Hyperliquid Duckets", "Gameplan Stats", "Gameplan"):
        frame = ttk.Frame(notebook)
        frames[title] = frame
        notebook.add(frame, text=title)
    tab = ui.HyperliquidDucketsTab(root, frames["Hyperliquid Duckets"])
    root.fixture_hyperliquid_tab = tab
    notebook.select(frames["Hyperliquid Duckets"])
    tab._show_bucket(DucketBucketSnapshot(list(_snapshots())))
    tab.composer_account.set(args.account)
    tab.composer_market.set("BTC")
    tab.composer_product.set(args.product)
    tab._choose_view(args.view)
    if args.capture:
        root.after(800, lambda: _capture_and_exit(root, args.capture, scroll_bottom=args.scroll_bottom))
    root.mainloop()


def _capture_and_exit(root, path, *, scroll_bottom=False):
    try:
        root.update_idletasks()
        if scroll_bottom:
            root.fixture_hyperliquid_tab.canvas.yview_moveto(1)
        root.deiconify()
        root.update()
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_window_png(root.winfo_id(), path)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
