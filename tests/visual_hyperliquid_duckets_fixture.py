"""Offline visual fixture for the Hyperliquid Duckets command center.

The fixture uses deterministic portfolio, market, chain, and order data. It does
not read credentials, perform network calls, or permit trade mutations.
"""

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

import app.ui.ducket_bucket as ducket_bucket_ui
from app.models.portfolio import CashBalance, Holding, PortfolioSnapshot
from app.services.aggregate import DucketBucketSnapshot
from app.ui.ducket_bucket import DucketBucketApp, HyperliquidDucketsTab
from visual_option_management_fixture import _write_window_png


class FakeInfoClient:
    def post_info(self, payload: dict[str, object]) -> list[object]:
        if payload.get("type") != "spotMetaAndAssetCtxs":
            raise AssertionError(f"Unexpected fake info request: {payload}")
        return [
            {
                "tokens": [
                    {"name": "USDC", "index": 0},
                    {"name": "HYPE", "index": 150},
                ],
                "universe": [{"name": "@107", "tokens": [150, 0], "index": 107}],
            },
            [{"coin": "@107", "midPx": "81.87"}],
        ]


class FakeExecutionAdapter:
    def __init__(self, account_key: str) -> None:
        self.account_key = account_key

    def open_orders(self) -> list[dict[str, object]]:
        if self.account_key == "jeremy":
            return [
                {
                    "accountKey": "jeremy",
                    "accountLabel": "Jeremy",
                    "coin": "HYPE",
                    "oid": 41001,
                    "side": "B",
                    "sz": "175",
                    "limitPx": "80.04",
                    "orderType": "Limit",
                    "reduceOnly": False,
                }
            ]
        return [
            {
                "accountKey": "alex",
                "accountLabel": "Alex",
                "coin": "HYPE",
                "oid": 41002,
                "side": "A",
                "sz": "200",
                "limitPx": "81.01",
                "orderType": "Limit",
                "reduceOnly": False,
            }
        ]

    def submit(self, _ticket: object) -> None:
        raise AssertionError("The visual fixture must never submit an order.")

    def cancel(self, _coin: str, _order_id: int) -> None:
        raise AssertionError("The visual fixture must never cancel an order.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", default="1706x923")
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--scroll-bottom", action="store_true")
    args = parser.parse_args()

    root = tk.Tk()
    root.title("Duckets — fake-only Hyperliquid Duckets fixture")
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

    tab = HyperliquidDucketsTab(root, frames["Hyperliquid Duckets"])
    root.fixture_hyperliquid_tab = tab  # type: ignore[attr-defined]
    notebook.select(frames["Hyperliquid Duckets"])
    tab._show_bucket(DucketBucketSnapshot(snapshots=list(_snapshots())))
    ducket_bucket_ui.HyperliquidInfoClient = FakeInfoClient  # type: ignore[assignment]
    ducket_bucket_ui.HyperliquidExecutionAdapter = FakeExecutionAdapter  # type: ignore[assignment]
    tab._load_hyperliquid_open_orders()

    if args.capture is not None:
        root.after(
            800,
            lambda: _capture_and_exit(root, args.capture, scroll_bottom=args.scroll_bottom),
        )
    root.mainloop()


def _snapshots() -> tuple[PortfolioSnapshot, PortfolioSnapshot]:
    synced_at = datetime(2026, 9, 2, 17, 24, 31, tzinfo=timezone.utc)
    closes = [
        78.0 + (index * 0.045) + math.sin(index / 4.2) * 1.35
        for index in range(96)
    ]
    common = {
        "hype_market": {
            "status": "current",
            "coin": "@107",
            "price": 81.87,
            "previous_price": 80.03,
            "change_percent_24h": 2.30,
            "volume_24h": 642_180_000.0,
            "circulating_supply": 333_930_000.0,
            "closes_24h": closes,
            "chart_status": "current",
        },
        "chain_status": {
            "available": True,
            "chain_id": 999,
            "block_number": 107_845_312,
            "gas_price_wei": 100_000_000,
        },
    }
    jeremy = PortfolioSnapshot(
        source="hyperliquid",
        account_label="Jeremy",
        cash=[CashBalance("USDC", -14_007.73, -14_007.73, "hyperliquid", "Perps")],
        holdings=[
            Holding(
                "HYPE-PERP",
                175,
                81.87,
                14_327.25,
                "hyperliquid",
                "Perps",
                unrealized_pnl=319.52,
            )
        ],
        synced_at=synced_at,
        reported_total_value=14_327.25,
        account_facts={
            **common,
            "spot_equity": 0.0,
            "perp_equity": 14_327.25,
            "available": 319.52,
            "margin_used": 14_007.73,
            "positions": {
                "HYPE": {
                    "entry_price": 80.04,
                    "liquidation_price": 54.21,
                    "margin_mode": "cross",
                    "leverage": 5,
                    "signed_size": 175,
                    "return_on_equity": 0.0228,
                }
            },
        },
    )
    alex = PortfolioSnapshot(
        source="hyperliquid",
        account_label="Alex",
        cash=[
            CashBalance("USDC", 12_844.14, 12_844.14, "hyperliquid", "Spot"),
            CashBalance("USDC", -15_221.41, -15_221.41, "hyperliquid", "Perps"),
        ],
        holdings=[
            Holding("HYPE-SPOT", 0.0000752, 81.87, 0.01, "hyperliquid", "Spot"),
            Holding(
                "HYPE-PERP-SHORT",
                200,
                81.87,
                16_374.38,
                "hyperliquid",
                "Perps",
                unrealized_pnl=-173.31,
            ),
        ],
        synced_at=synced_at,
        reported_total_value=13_997.11,
        account_facts={
            **common,
            "spot_equity": 12_844.14,
            "perp_equity": 1_152.97,
            "available": 173.31,
            "margin_used": 1_096.84,
            "positions": {
                "HYPE": {
                    "entry_price": 81.01,
                    "liquidation_price": 106.88,
                    "margin_mode": "cross",
                    "leverage": 5,
                    "signed_size": -200,
                    "return_on_equity": -0.0107,
                }
            },
        },
    )
    return jeremy, alex


def _capture_and_exit(root: tk.Tk, path: Path, *, scroll_bottom: bool) -> None:
    try:
        tab = getattr(root, "fixture_hyperliquid_tab", None)
        if scroll_bottom and tab is not None:
            root.update_idletasks()
            tab.canvas.yview_moveto(1.0)
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


if __name__ == "__main__":
    main()
