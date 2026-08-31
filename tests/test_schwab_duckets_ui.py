from __future__ import annotations

import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from tkinter import ttk

import pytest

from app.models.portfolio import CashBalance, Holding, PortfolioSnapshot
from app.ui.schwab_duckets import (
    ALL_ACCOUNTS,
    SchwabDucketsTab,
    account_filter_choices,
    equity_holding_views,
    equity_only_orders,
    filter_equity_holding_views,
    is_equity_order,
    quote_request_is_current,
    quote_view_from_payload,
    safe_allocation_segments,
    schwab_equity_summary,
    schwab_layout,
    schwab_order_row,
    security_mark_path,
    security_monogram,
)


def _holding(
    symbol: str,
    *,
    bucket: str = "Stock",
    value: float = 1_000.0,
    quantity: float = 10.0,
    price: float = 100.0,
    open_pnl: float | None = 20.0,
    day_pnl: float | None = 5.0,
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


def _snapshot(
    account: str,
    *holdings: Holding,
    cash: float = 500.0,
    total: float | None = None,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        source="schwab",
        account_label=account,
        cash=[CashBalance("USD", cash, cash, "schwab", "Cash & sweep")],
        holdings=list(holdings),
        synced_at=datetime(2026, 8, 29, 17, 30, tzinfo=timezone.utc),
        status="fixture current",
        reported_total_value=total,
    )


def _equity_order(
    order_id: int = 1001,
    *,
    symbol: str = "AAPL",
    asset_type: str = "EQUITY",
    status: str = "WORKING",
    editable: bool = True,
) -> dict[str, object]:
    return {
        "orderId": order_id,
        "status": status,
        "editable": editable,
        "filledQuantity": 0,
        "orderType": "LIMIT",
        "price": 183.50,
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "NONE",
        "enteredTime": "2026-08-29T17:20:11Z",
        "accountNumber": "12345678",
        "orderLegCollection": [
            {
                "instruction": "BUY",
                "positionEffect": "AUTOMATIC",
                "quantity": 10,
                "instrument": {"symbol": symbol, "assetType": asset_type},
            }
        ],
    }


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


def test_stock_etf_rows_and_filters_exclude_options_and_preserve_accounts() -> None:
    snapshots = (
        _snapshot(
            "Primary",
            _holding("AAPL"),
            _holding("SPY", bucket="ETF"),
            _holding("AAPL  260918C00200000", bucket="Option"),
        ),
        _snapshot("IRA", _holding("AAPL", value=600.0, quantity=3, price=200.0)),
    )

    rows = equity_holding_views(snapshots)

    assert [row.symbol for row in rows] == ["AAPL", "AAPL", "SPY"]
    assert len({row.identity for row in rows}) == 3
    assert account_filter_choices(snapshots) == (ALL_ACCOUNTS, "Primary", "IRA")
    assert [row.symbol for row in filter_equity_holding_views(rows, account="Primary")] == [
        "AAPL",
        "SPY",
    ]
    assert [row.symbol for row in filter_equity_holding_views(rows, asset_type="ETFs")] == ["SPY"]
    assert [row.account_label for row in filter_equity_holding_views(rows, asset_type="Stocks")] == [
        "IRA",
        "Primary",
    ]


def test_summary_keeps_whole_account_total_and_reports_partial_equity_pnl() -> None:
    snapshot = _snapshot(
        "Primary",
        _holding("AAPL", value=2_000.0, open_pnl=125.0, day_pnl=-10.0),
        _holding("SPY", bucket="ETF", value=3_000.0, open_pnl=None, day_pnl=0.0),
        _holding("AAPL  260918C00200000", bucket="Option", value=900.0, open_pnl=400.0),
        cash=1_000.0,
        total=6_900.0,
    )

    summary = schwab_equity_summary((snapshot,))

    assert summary.net_liquidation == 6_900.0
    assert summary.cash_and_sweep == 1_000.0
    assert summary.stocks_and_etfs == 5_000.0
    assert summary.open_pnl.value == 125.0
    assert (summary.open_pnl.reported_count, summary.open_pnl.total_count) == (1, 2)
    assert summary.day_pnl.value == -10.0
    assert (summary.day_pnl.reported_count, summary.day_pnl.total_count) == (2, 2)


@pytest.mark.parametrize(
    ("cash", "equities", "expected"),
    (
        (20.0, 80.0, (0.2, 0.8)),
        (0.0, 80.0, (0.0, 1.0)),
        (20.0, 0.0, (1.0, 0.0)),
        (0.0, 0.0, None),
        (-1.0, 80.0, None),
        (20.0, -80.0, None),
    ),
)
def test_allocation_math_is_truthful_for_long_cash_and_negative_cases(
    cash: float,
    equities: float,
    expected: tuple[float, float] | None,
) -> None:
    segments = safe_allocation_segments(cash, equities)
    if expected is None:
        assert segments is None
    else:
        assert segments is not None
        assert (segments.cash_fraction, segments.equities_fraction) == pytest.approx(expected)


def test_equity_order_filter_rejects_options_mixed_legs_and_malformed_legs() -> None:
    equity = _equity_order(1)
    option = _equity_order(2, asset_type="OPTION")
    mixed = _equity_order(3)
    mixed["orderLegCollection"].append(
        {
            "instruction": "SELL_TO_OPEN",
            "quantity": 1,
            "instrument": {"symbol": "AAPL  260918C00200000", "assetType": "OPTION"},
        }
    )
    malformed = {"orderId": 4, "orderLegCollection": [{"instruction": "BUY"}]}

    assert is_equity_order(equity) is True
    assert is_equity_order(option) is False
    assert is_equity_order(mixed) is False
    assert is_equity_order(malformed) is False
    assert equity_only_orders([equity, option, mixed, malformed, "bad"]) == (equity,)


def test_order_row_exposes_supported_modify_cancel_fields_and_masks_account() -> None:
    row = schwab_order_row(_equity_order())

    assert row.symbol == "AAPL"
    assert row.order_type == "Limit"
    assert row.price == "183.50"
    assert row.time_in_force == "DAY"
    assert row.position_effect == "Auto"
    assert row.account == "••••5678"
    assert row.can_modify is True
    assert row.can_cancel is True


def test_quote_view_uses_midpoint_then_mark_then_last_and_never_fabricates() -> None:
    midpoint = quote_view_from_payload(
        "aapl",
        {"bidPrice": 183.48, "askPrice": 183.52, "mark": 190.0, "bidSize": 10, "askSize": 20},
    )
    mark = quote_view_from_payload("AAPL", {"bidPrice": 0, "askPrice": 0, "mark": 182.25})
    last = quote_view_from_payload("AAPL", {"lastPrice": 181.75})
    unavailable = quote_view_from_payload("AAPL", {})

    assert midpoint.mid == pytest.approx(183.50)
    assert midpoint.source == "Bid / ask midpoint"
    assert mark.mid == pytest.approx(182.25)
    assert mark.source == "Mark fallback"
    assert last.mid == pytest.approx(181.75)
    assert last.source == "Last-price fallback"
    assert unavailable.mid is None


def test_stale_quote_generation_or_symbol_is_rejected() -> None:
    assert quote_request_is_current(3, "AAPL", current_generation=3, current_symbol="aapl") is True
    assert quote_request_is_current(2, "AAPL", current_generation=3, current_symbol="AAPL") is False
    assert quote_request_is_current(3, "MSFT", current_generation=3, current_symbol="AAPL") is False


def test_security_mark_known_asset_path_and_unknown_monogram_fallback(tmp_path: Path) -> None:
    mark = tmp_path / "aapl.png"
    mark.write_bytes(b"fixture")

    assert security_mark_path("aapl", asset_root=tmp_path) == mark
    assert security_mark_path("UNKNOWN", asset_root=tmp_path) is None
    assert security_monogram(" unknown ") == "UN"
    assert security_monogram("$$") == "--"


@pytest.mark.parametrize(
    ("symbol", "filename"),
    (
        ("AAPL", "aapl.png"),
        ("AMZN", "amzn.png"),
        ("EWY", "ewy.png"),
        ("GOOG", "goog.png"),
        ("GOOGL", "goog.png"),
        ("MRNA", "mrna.png"),
        ("MU", "mu.png"),
        ("NBIS", "nbis.png"),
        ("NVDA", "nvda.png"),
        ("SLS", "sls.png"),
        ("SNDK", "sndk.png"),
        ("TENB", "tenb.png"),
        ("VXUS", "vxus.png"),
        ("ZETA", "zeta.png"),
    ),
)
def test_bundled_security_marks_are_loadable_square_pngs(
    root: tk.Tk,
    symbol: str,
    filename: str,
) -> None:
    path = security_mark_path(symbol)

    assert path is not None
    assert path.name == filename
    root.update_idletasks()
    photo = tk.PhotoImage(master=root, file=str(path))
    assert (photo.width(), photo.height()) == (512, 512)


def test_bundled_security_mark_renders_in_row_and_selected_holding(root: tk.Tk) -> None:
    parent = ttk.Frame(root)
    parent.pack(fill=tk.BOTH, expand=True)
    try:
        tab = SchwabDucketsTab(root, parent, background_runner=_immediate_background)
        for symbol in (
            "AAPL",
            "AMZN",
            "EWY",
            "GOOG",
            "MRNA",
            "MU",
            "NBIS",
            "NVDA",
            "SLS",
            "SNDK",
            "TENB",
            "VXUS",
            "ZETA",
        ):
            row_mark = tab._tree_mark(symbol)
            tab.selected_mark_widget.show(symbol)
            selected_mark = tab.selected_mark_widget._photo

            assert 16 <= max(row_mark.width(), row_mark.height()) <= 18
            assert row_mark.transparency_get(row_mark.width() // 2, 0) is True
            assert selected_mark is not None
            assert 32 <= max(selected_mark.width(), selected_mark.height()) <= 38
            assert selected_mark.transparency_get(selected_mark.width() // 2, 0) is True
            assert len(tab.selected_mark_widget.find_all()) == 1
    finally:
        parent.destroy()


@pytest.mark.parametrize(
    ("width", "expected"),
    ((1672, (5, True)), (1450, (5, True)), (1180, (3, False)), (700, (2, False)), (500, (1, False))),
)
def test_responsive_layout_boundaries(width: int, expected: tuple[int, bool]) -> None:
    assert schwab_layout(width) == expected


def test_portfolio_rows_use_airier_unboxed_style_without_enlarging_orders(root: tk.Tk) -> None:
    parent = ttk.Frame(root)
    parent.pack(fill=tk.BOTH, expand=True)
    try:
        tab = SchwabDucketsTab(root, parent, background_runner=_immediate_background)
        style = ttk.Style(root)

        assert int(style.lookup("SchwabPortfolio.Treeview", "rowheight")) >= 40
        assert "Treeheading.border" not in repr(
            style.layout("SchwabPortfolio.Treeview.Heading")
        )
        assert str(tab.holdings_table.cget("style")) == "SchwabPortfolio.Treeview"
        assert str(tab.cash_table.cget("style")) == "SchwabPortfolio.Treeview"
        assert str(tab.open_orders_table.cget("style")) == "Schwab.Treeview"
    finally:
        parent.destroy()


def test_wide_layout_fills_viewport_and_compact_layout_remains_scrollable(
    root: tk.Tk,
) -> None:
    window = tk.Toplevel(root)
    window.geometry("1906x1030")
    parent = ttk.Frame(window)
    parent.pack(fill=tk.BOTH, expand=True)
    try:
        tab = SchwabDucketsTab(window, parent, background_runner=_immediate_background)
        window.deiconify()
        window.update()

        for _ in range(3):
            tab._canvas_viewport_height = tab.canvas.winfo_height()
            tab._apply_responsive_layout(force=True)
            window.update_idletasks()
            tab._sync_body_window_height()
            window.update()

        assert tab._last_layout == (5, True)
        assert abs(tab.body.winfo_height() - tab.canvas.winfo_height()) <= 1
        assert tab.holdings_table.winfo_height() > tab.holdings_table.winfo_reqheight()
        assert tab.orders_card.winfo_rooty() < tab.selected_card.winfo_rooty()

        window.geometry("1180x760")
        window.update()
        for _ in range(3):
            tab._canvas_viewport_height = tab.canvas.winfo_height()
            tab._apply_responsive_layout(force=True)
            window.update_idletasks()
            tab._sync_body_window_height()
            window.update()

        assert tab._last_layout == (3, False)
        assert int(tab.body.grid_rowconfigure(2)["minsize"]) == 0
        assert tab.body.winfo_height() > tab.canvas.winfo_height()
    finally:
        window.destroy()


def test_real_tab_contains_no_option_ticket_chain_or_options_summary(root: tk.Tk) -> None:
    parent = ttk.Frame(root)
    parent.pack(fill=tk.BOTH, expand=True)
    tab = SchwabDucketsTab(root, parent, background_runner=_immediate_background)
    root.update_idletasks()

    texts: list[str] = []

    def visit(widget: tk.Misc) -> None:
        try:
            text = widget.cget("text")
        except tk.TclError:
            text = ""
        if text:
            texts.append(str(text))
        for child in widget.winfo_children():
            visit(child)

    visit(parent)

    assert not hasattr(tab, "option_symbol")
    assert not hasattr(tab, "option_chain_table")
    assert not hasattr(tab, "options_value")
    assert all("Options Ticket" not in text and "Options Chain" not in text for text in texts)


def test_duplicate_symbol_selection_retains_exact_account_and_populates_ticket(root: tk.Tk) -> None:
    class Session:
        def get_equity_quote(self, _symbol: str) -> dict[str, float]:
            return {"bidPrice": 99.0, "askPrice": 101.0}

    parent = ttk.Frame(root)
    parent.pack(fill=tk.BOTH, expand=True)
    tab = SchwabDucketsTab(
        root,
        parent,
        session_factory=Session,
        background_runner=_immediate_background,
    )
    tab.show_snapshots(
        (
            _snapshot("Primary", _holding("AAPL", quantity=10)),
            _snapshot("IRA", _holding("AAPL", quantity=3)),
        )
    )
    tab.account_filter.set(ALL_ACCOUNTS)
    tab._render_portfolio()
    item_id, row = next(
        (item_id, row)
        for item_id, row in tab._position_by_item_id.items()
        if row.account_label == "IRA"
    )
    tab.holdings_table.selection_set(item_id)
    tab._use_selected_holding(None)

    assert tab.stock_symbol.get() == "AAPL"
    assert tab.selected_holding_title.get() == "AAPL · IRA"
    assert tab.selected_shares.get() == "3"
    assert tab._selected_holding_key == row.identity
    assert tab._ticket_source_key == row.identity


def test_confirmed_cancel_routes_exact_selected_equity_order(root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    selected = _equity_order(771, symbol="MSFT")

    class Session:
        canceled: list[str] = []

        def get_open_orders(self) -> list[dict[str, object]]:
            return []

        def cancel_order(self, order_id: str) -> None:
            self.canceled.append(order_id)

    session = Session()
    parent = ttk.Frame(root)
    parent.pack(fill=tk.BOTH, expand=True)
    tab = SchwabDucketsTab(
        root,
        parent,
        session_factory=lambda: session,
        background_runner=_immediate_background,
    )
    monkeypatch.setattr("app.ui.schwab_duckets.messagebox.askyesno", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.ui.schwab_duckets.messagebox.showinfo", lambda *_args, **_kwargs: None)
    tab._show_orders(tab.open_orders_table, [selected])
    item_id = next(iter(tab.schwab_open_order_by_item_id))
    tab.open_orders_table.selection_set(item_id)
    tab._use_selected_schwab_order(SimpleNamespace(widget=tab.open_orders_table))

    tab._cancel_selected_order()

    assert session.canceled == ["771"]


def test_sync_failure_retains_last_successful_render(root: tk.Tk, monkeypatch: pytest.MonkeyPatch) -> None:
    current = _snapshot("Primary", _holding("AAPL"), total=1_500.0)

    def failing_loader() -> PortfolioSnapshot:
        raise ConnectionError("fixture offline")

    parent = ttk.Frame(root)
    parent.pack(fill=tk.BOTH, expand=True)
    tab = SchwabDucketsTab(
        root,
        parent,
        snapshot_loader=failing_loader,
        background_runner=_immediate_background,
    )
    tab.show_snapshots((current,))
    before = tuple(tab._position_by_item_id.values())
    monkeypatch.setattr("app.ui.schwab_duckets.messagebox.showerror", lambda *_args, **_kwargs: None)

    tab._sync()

    assert tab.sync_status.get() == "Sync failed"
    assert tuple(tab._position_by_item_id.values()) == before
    assert tab._summary_values["net"].get() == "$1,500.00"


def test_sync_loading_and_success_states_are_textual_and_nonblocking(root: tk.Tk) -> None:
    queued: list[tuple[object, object, object]] = []

    def deferred(
        _root: tk.Misc,
        work: object,
        on_success: object,
        on_error: object,
    ) -> None:
        queued.append((work, on_success, on_error))

    snapshot = _snapshot("Primary", _holding("MSFT"), total=1_500.0)
    parent = ttk.Frame(root)
    parent.pack(fill=tk.BOTH, expand=True)
    tab = SchwabDucketsTab(
        root,
        parent,
        snapshot_loader=lambda: snapshot,
        background_runner=deferred,
    )

    tab._sync()

    assert tab.sync_status.get() == "Syncing"
    assert str(tab.sync_button.cget("state")) == tk.DISABLED
    assert tab.position_status.get() == "Loading Schwab positions…"
    _work, on_success, _on_error = queued.pop()
    on_success(snapshot)
    assert tab.sync_status.get() == "Connected"
    assert str(tab.sync_button.cget("state")) == tk.NORMAL
    assert "Aug 29, 2026" in tab.last_sync.get()


def test_empty_open_orders_state_reports_hidden_non_equity_rows(root: tk.Tk) -> None:
    parent = ttk.Frame(root)
    parent.pack(fill=tk.BOTH, expand=True)
    tab = SchwabDucketsTab(root, parent, background_runner=_immediate_background)
    option_order = _equity_order(8, asset_type="OPTION")

    tab._show_orders(tab.open_orders_table, [option_order])

    assert tab.open_orders_table.get_children() == ()
    assert tab.order_status.get() == "No open Stock/ETF orders. 1 non-equity order was hidden."
