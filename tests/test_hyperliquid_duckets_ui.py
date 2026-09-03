from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pytest

from app.models.portfolio import CashBalance, Holding, PortfolioSnapshot
from app.ui.ducket_bucket import (
    HYPERLIQUID_POSITION_ICON_COLUMN_WIDTH,
    HYPERLIQUID_POSITION_ROW_HEIGHT,
    HyperliquidDucketsTab,
    hyperliquid_account_key,
    hyperliquid_account_summary,
    hyperliquid_asset_path,
    hyperliquid_position_views,
)


@pytest.fixture(scope="module")
def root() -> tk.Tk:
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"Tk is unavailable: {exc}")
    window.withdraw()
    yield window
    try:
        window.destroy()
    except tk.TclError:
        pass


def _snapshot() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        source="hyperliquid",
        account_label="Jeremy",
        cash=[CashBalance("USDC", 200.0, 200.0, "hyperliquid", "Spot")],
        holdings=[
            Holding("HYPE-SPOT", 2.0, 80.0, 160.0, "hyperliquid", "Spot"),
            Holding(
                "HYPE-PERP-SHORT",
                3.0,
                82.0,
                246.0,
                "hyperliquid",
                "Perps",
                unrealized_pnl=-12.5,
            ),
        ],
        reported_total_value=1_560.0,
        account_facts={
            "spot_equity": 360.0,
            "perp_equity": 1_200.0,
            "available": 425.0,
            "margin_used": 775.0,
            "positions": {
                "HYPE": {
                    "entry_price": 78.5,
                    "liquidation_price": 106.0,
                    "margin_mode": "cross",
                    "leverage": 5.0,
                    "signed_size": -3.0,
                    "return_on_equity": -0.016,
                }
            },
        },
    )


def test_account_summary_uses_exchange_equity_and_risk_facts() -> None:
    summary = hyperliquid_account_summary(_snapshot())

    assert summary.equity == 1_560.0
    assert summary.spot_equity == 360.0
    assert summary.perp_equity == 1_200.0
    assert summary.available == 425.0
    assert summary.unrealized_pnl == -12.5
    assert summary.margin_used == 775.0


def test_position_views_preserve_account_and_perp_risk_details() -> None:
    rows = hyperliquid_position_views((_snapshot(),))

    assert len(rows) == 2
    assert rows[0].kind == "Spot"
    assert rows[0].side == "Long"
    assert rows[1].account_key == "jeremy"
    assert rows[1].kind == "Perp"
    assert rows[1].side == "Short"
    assert rows[1].entry_price == 78.5
    assert rows[1].liquidation_price == 106.0
    assert rows[1].pnl_percent == pytest.approx(-1.6)


def test_account_asset_lookup_uses_png_when_present_and_falls_back_when_missing(
    tmp_path: Path,
) -> None:
    jeremy = tmp_path / "jeremy.png"
    jeremy.write_bytes(b"not decoded by this pure lookup test")

    assert hyperliquid_asset_path("Jeremy", asset_root=tmp_path) == jeremy
    assert hyperliquid_asset_path("alex", asset_root=tmp_path) is None
    assert hyperliquid_asset_path("unknown", asset_root=tmp_path) is None
    assert hyperliquid_account_key("Jeremy (JE)") == "jeremy"
    assert hyperliquid_account_key("Alex") == "alex"


def test_wide_layout_expands_portfolio_and_tickets_while_compact_layout_scrolls(
    root: tk.Tk,
) -> None:
    window = tk.Toplevel(root)
    window.geometry("1906x1030")
    parent = ttk.Frame(window)
    parent.pack(fill=tk.BOTH, expand=True)
    try:
        tab = HyperliquidDucketsTab(window, parent)
        window.deiconify()
        window.update()

        for _ in range(3):
            tab._canvas_viewport_height = tab.canvas.winfo_height()
            tab._apply_hyperliquid_layout(tab.canvas.winfo_width(), force=True)
            window.update_idletasks()
            tab._sync_hyperliquid_body_height()
            window.update()

        style = ttk.Style(window)
        assert tab._last_hyperliquid_layout == "wide"
        assert abs(tab.body.winfo_height() - tab.canvas.winfo_height()) <= 2
        assert tab.holdings_table.winfo_height() > tab.holdings_table.winfo_reqheight()
        assert tab.trading_frame.winfo_height() > tab.trading_frame.winfo_reqheight()
        assert int(style.lookup("HyperPortfolio.Treeview", "rowheight")) >= (
            HYPERLIQUID_POSITION_ROW_HEIGHT
        )
        assert int(tab.holdings_table.column("#0", "width")) >= (
            HYPERLIQUID_POSITION_ICON_COLUMN_WIDTH
        )

        window.geometry("1180x760")
        window.update()
        for _ in range(3):
            tab._canvas_viewport_height = tab.canvas.winfo_height()
            tab._apply_hyperliquid_layout(tab.canvas.winfo_width(), force=True)
            window.update_idletasks()
            tab._sync_hyperliquid_body_height()
            window.update()

        assert tab._last_hyperliquid_layout == "medium"
        assert int(tab.body.grid_rowconfigure(2)["minsize"]) == 0
        assert int(tab.body.grid_rowconfigure(4)["minsize"]) == 0
        assert tab.body.winfo_height() > tab.canvas.winfo_height()
    finally:
        window.destroy()
