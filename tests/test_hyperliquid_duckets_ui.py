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
    assert hyperliquid_account_key("Clearpond (CP)") == "clearpond"


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

        assert tab._last_hyperliquid_layout == "medium"
        assert int(tab.body.grid_rowconfigure(2)["minsize"]) == 0
        assert int(tab.body.grid_rowconfigure(4)["minsize"]) == 0
        assert tab.body.winfo_height() > tab.canvas.winfo_height()
        assert tab.accounts_rail.winfo_height() >= tab.accounts_rail.winfo_reqheight()
        assert tab.center.winfo_height() >= tab.center.winfo_reqheight()
        assert tab.composer_card.winfo_y() >= tab.center.winfo_y() + tab.center.winfo_height()
    finally:
        window.destroy()


@pytest.fixture
def workspace(root, monkeypatch, tmp_path):
    import app.ui.ducket_bucket as ui
    from app.services.aggregate import DucketBucketSnapshot
    from visual_hyperliquid_duckets_fixture import _snapshots
    monkeypatch.setenv("DUCKETZ_HYPERLIQUID_PREFERENCES_PATH", str(tmp_path / "watchlist.json"))
    def forbidden(*args, **kwargs):
        pytest.fail("UI test attempted real network/trading access")
    monkeypatch.setattr(ui, "HyperliquidExecutionAdapter", forbidden)
    monkeypatch.setattr(ui, "HyperliquidInfoClient", forbidden)
    monkeypatch.setattr(ui, "sync_hyperliquid_portfolios", forbidden)
    window = tk.Toplevel(root)
    parent = ttk.Frame(window)
    parent.pack(fill="both", expand=True)
    tab = HyperliquidDucketsTab(window, parent)
    tab._show_bucket(DucketBucketSnapshot(list(_snapshots())))
    yield tab
    window.destroy()


def test_switching_account_product_and_market_discards_prior_draft(workspace):
    tab = workspace
    for variable, value in ((tab.composer_account, "Alex"), (tab.composer_product, "Spot"), (tab.composer_market, "BTC")):
        tab.perp_size.set("3")
        tab.perp_entry_limit.set("79")
        tab.spot_quantity.set("100")
        tab.spot_entry_limit.set("79")
        tab.perp_reduce_only.set(True)
        tab.selected_hyperliquid_order_key = "old-order"
        variable.set(value)
        assert not any(v.get() for v in (tab.perp_size, tab.perp_entry_limit, tab.spot_quantity, tab.spot_entry_limit))
        assert not tab.perp_reduce_only.get()
        assert not tab.selected_hyperliquid_order_key
    assert tab.spot_account.get() == "Alex (AL)"
    assert tab.perp_account.get() == "Alex (AL)"
    assert tab.perp_coin.get() == "BTC"
    assert tab.market_route.get() == "Spot · UBTC/USDC"


@pytest.mark.parametrize("product, coin, size, price, expected_coin, expected_size", [("Perp", "BTC", "0.002", "110000", "BTC", .002), ("Spot", "BTC", "220", "110000", "UBTC/USDC", .002), ("Perp", "ETH", "0.1", "4000", "ETH", .1), ("Spot", "ZEC", "120", "1200", "UZEC/USDC", .1)])
def test_review_routes_clearpond_to_correct_market_and_size_only_after_confirmation(workspace, monkeypatch, product, coin, size, price, expected_coin, expected_size):
    import app.ui.ducket_bucket as ui
    submitted, confirmations = [], []
    class Adapter:
        def __init__(self, key):
            self.key = key
        def submit(self, ticket):
            submitted.append((self.key, ticket))
            return {"status": "ok"}
    tab = workspace
    monkeypatch.setattr(ui, "HyperliquidExecutionAdapter", Adapter)
    monkeypatch.setattr(tab, "_load_hyperliquid_open_orders", lambda: None)
    monkeypatch.setattr(ui.messagebox, "showinfo", lambda *args: None)
    monkeypatch.setattr(ui.messagebox, "showerror", lambda *args: pytest.fail(str(args)))
    tab.composer_product.set(product)
    tab.composer_market.set(coin)
    (tab.spot_quantity if product == "Spot" else tab.perp_size).set(size)
    (tab.spot_entry_limit if product == "Spot" else tab.perp_entry_limit).set(price)
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda title, body: confirmations.append(body) or False)
    tab._review_composer()
    assert submitted == []
    assert "clearpond" in confirmations[0].lower()
    assert expected_coin in confirmations[0]
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *args: True)
    tab._review_composer()
    assert submitted[0][0] == "clearpond"
    assert submitted[0][1].coin == expected_coin
    assert submitted[0][1].size == pytest.approx(expected_size)


def test_position_order_filters_and_selected_order_keep_account_identity(workspace):
    from types import SimpleNamespace
    tab = workspace
    assert len(tab.holdings_table.get_children()) == 4
    tab._filter_account("Alex")
    tab.market_filter.set("ZEC")
    tab._render_portfolio()
    assert len(tab.holdings_table.get_children()) == 1
    row = tab.holdings_table.get_children()[0]
    tab.holdings_table.selection_set(row)
    tab._use_selected_hyperliquid_holding()
    assert tab.composer_account.get() == "Alex"
    assert tab.composer_market.get() == "ZEC"
    tab.market_filter.set("All markets")
    tab.account_filter.set("All accounts")
    tab._render_portfolio()
    order_id = next(i for i in tab.portfolio_orders_table.get_children() if tab.hyperliquid_open_order_by_lookup_key[i]["accountKey"] == "jeremy")
    tab.portfolio_orders_table.selection_set(order_id)
    tab._use_selected_hyperliquid_order(SimpleNamespace(widget=tab.portfolio_orders_table))
    assert tab.composer_account.get() == "Jeremy"
    assert tab._selected_hyperliquid_order()["accountKey"] == "jeremy"
    assert str(tab.edit_order_button.cget("state")) == "normal"
    tab.composer_account.set("Clearpond")
    assert tab._selected_hyperliquid_order() is None
    assert str(tab.cancel_order_button.cget("state")) == "disabled"


def test_missing_account_and_unavailable_market_cannot_review(workspace, monkeypatch):
    import app.ui.ducket_bucket as ui
    tab = workspace
    monkeypatch.setattr(ui.messagebox, "showinfo", lambda *args: None)
    monkeypatch.setattr(ui.messagebox, "showerror", lambda *args: None)
    tab.composer_market.set("UNLISTED")
    tab._review_composer()
    assert str(tab.review_button.cget("state")) == "disabled"
    tab.composer_market.set("HYPE")
    tab._show_error(RuntimeError("Offline"))
    tab._review_composer()
    assert str(tab.review_button.cget("state")) == "disabled"
    assert all(f[1].get() == "Stale quote" for f in tab._market_widgets.values())


def test_cash_activity_empty_states_and_spot_sell_available(workspace):
    tab = workspace
    tab._filter_account("Clearpond")
    tab._choose_view("Cash")
    assert len(tab.cash_table.get_children()) == 1
    tab._choose_view("Activity")
    assert "0 activity" in tab.portfolio_message.get()
    tab._filter_account("Alex")
    assert len(tab.activity_table.get_children()) == 1
    tab.composer_product.set("Spot")
    tab.composer_market.set("BTC")
    tab.spot_side.set("sell")
    assert tab.composer_available.get() == "Available · 0 UBTC"


def test_add_market_uses_exchange_catalog_and_persists_watchlist(workspace, monkeypatch):
    from app.ui.hyperliquid_workspace import load_watchlist
    tab = workspace
    synced = []
    monkeypatch.setattr(tab, "_sync", lambda: synced.append(True))
    tab._add_market()
    dialog = next(w for w in tab.root.winfo_children() if isinstance(w, tk.Toplevel))
    body = dialog.winfo_children()[0]
    picker = next(w for w in body.winfo_children() if isinstance(w, ttk.Combobox))
    assert picker["values"] == ("SOL",)
    button = next(w for w in body.winfo_children() if isinstance(w, ttk.Button))
    button.invoke()
    assert load_watchlist() == ["HYPE", "BTC", "ETH", "ZEC", "SOL"]
    assert "SOL" in tab._market_widgets
    assert synced == [True]


def test_cancel_uses_the_selected_accounts_order_and_decline_does_nothing(workspace, monkeypatch):
    from types import SimpleNamespace
    import app.ui.ducket_bucket as ui
    tab = workspace
    calls = []
    class Adapter:
        def __init__(self, key):
            self.key = key
        def cancel(self, coin, oid):
            calls.append((self.key, coin, oid))
            return {"status": "ok"}
    monkeypatch.setattr(ui, "HyperliquidExecutionAdapter", Adapter)
    monkeypatch.setattr(tab, "_load_hyperliquid_open_orders", lambda: None)
    monkeypatch.setattr(ui.messagebox, "showinfo", lambda *args: None)
    monkeypatch.setattr(ui.messagebox, "showerror", lambda *args: pytest.fail(str(args)))
    key = "alex:41002"
    tab.portfolio_orders_table.selection_set(key)
    tab._use_selected_hyperliquid_order(SimpleNamespace(widget=tab.portfolio_orders_table))
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *args: False)
    tab._cancel_selected_hyperliquid_order()
    assert calls == []
    monkeypatch.setattr(ui.messagebox, "askyesno", lambda *args: True)
    tab._cancel_selected_hyperliquid_order()
    assert calls == [("alex", "HYPE", 41002)]


def test_trigger_orders_can_be_selected_for_cancel_but_not_converted_to_limits(workspace, monkeypatch):
    from types import SimpleNamespace
    import app.ui.ducket_bucket as ui
    tab = workspace
    monkeypatch.setattr(ui.messagebox, "showinfo", lambda *args: None)
    tab._orders_by_account["clearpond"] = [{"accountKey": "clearpond", "accountLabel": "Clearpond", "coin": "BTC", "oid": 77, "side": "B", "sz": ".001", "limitPx": "110000", "isTrigger": True, "orderType": "Stop Market"}]
    tab._render_open_orders()
    tab.hyperliquid_open_orders_table.selection_set("clearpond:77")
    tab._use_selected_hyperliquid_order(SimpleNamespace(widget=tab.hyperliquid_open_orders_table))
    assert str(tab.edit_order_button.cget("state")) == "disabled"
    assert str(tab.cancel_order_button.cget("state")) == "normal"
    tab._edit_selected_hyperliquid_open_order()
    assert not any(isinstance(w, tk.Toplevel) for w in tab.root.winfo_children())


def test_mouse_wheel_over_account_cards_scrolls_the_workspace(workspace):
    from types import SimpleNamespace
    tab = workspace
    tab.root.geometry("1180x760")
    tab.root.update()
    tab.canvas.yview_moveto(0)
    tab._scroll_workspace(SimpleNamespace(widget=tab.accounts_rail, delta=-120))
    assert tab.canvas.yview()[0] > 0


def test_styled_side_buttons_preserve_draft_when_window_resizes(workspace):
    tab = workspace
    tab.composer_market.set("BTC")
    tab.perp_size.set("0.002")
    tab.perp_entry_limit.set("110000")
    tab.side_buttons["sell"].invoke()
    assert tab.perp_direction.get() == "sell"
    tab.root.geometry("2182x1183")
    tab.root.update()
    assert tab._order_scale > 1
    assert tab.perp_direction.get() == "sell"
    assert tab.perp_size.get() == "0.002"
    assert tab.perp_entry_limit.get() == "110000"
    fields = (tab.side_group, *tab.ticket_inputs, tab.tif_group)
    assert len({field.winfo_rootx() for field in fields}) == 1
    assert len({field.winfo_width() for field in fields}) == 1
    tab._product_buttons["Spot"].invoke()
    assert tab.composer_product.get() == "Spot"
    assert not tab.perp_size.get()
    tab.side_buttons["sell"].invoke()
    assert tab.spot_side.get() == "sell"


def test_orders_account_picker_routes_to_the_named_account(workspace):
    tab = workspace
    assert not tab.hyperliquid_open_orders_table.get_children()
    tab.perp_size.set("1")
    tab.orders_account_combo.set("Alex")
    assert tab.composer_account.get() == "Alex"
    assert tab.perp_account.get() == "Alex (AL)"
    assert tab.perp_size.get() == ""
    assert tab.hyperliquid_open_orders_table.get_children() == ("alex:41002",)


def test_market_picker_preserves_underlying_spot_and_perp_routes(workspace):
    tab = workspace
    tab.root.update()
    tab.market_combo.set("BTC-PERP")
    tab.market_combo.event_generate("<<ComboboxSelected>>")
    assert tab.composer_market.get() == "BTC"
    assert tab.perp_coin.get() == "BTC"
    tab.perp_size.set("0.002")
    tab._product_buttons["Spot"].invoke()
    assert not tab.perp_size.get()
    assert tab.market_choice.get() == "BTC/USDC"
    tab.market_combo.set("ETH/USDC")
    tab.market_combo.event_generate("<<ComboboxSelected>>")
    assert tab.composer_market.get() == "ETH"
    assert tab.spot_market.get() == "ETH"
    assert tab.market_route.get() == "Spot · UETH/USDC"
