"""Fake-only native visual fixture for Options Command Center > Past Positions.

Usage:
    python tests/visual_past_positions_fixture.py --capture artifacts/past-positions.png

Every account, order, execution, and P/L value is deterministic fixture data. The
fixture never reads credentials and never performs network I/O.
"""

from __future__ import annotations

import argparse
import tkinter as tk
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tkinter import ttk

from app.services.schwab_past_positions import snapshot_from_history
from app.ui.option_templates import OptionsTemplatesView
from app.ui.options_management import OptionsManagementView
from app.ui.past_positions import PastPositionsView
from app.ui.theme import BACKGROUND, MUTED_TEXT, TEXT
from visual_option_management_fixture import (
    FakeSchwabSession,
    _discover_placeholder,
    _fixture_styles,
    _global_header,
    _snapshot,
    _write_window_png,
)


UTC = timezone.utc


class FakeHistorySession(FakeSchwabSession):
    def __init__(self, orders: list[dict[str, object]]) -> None:
        self.orders = orders

    def get_recent_orders(self) -> list[dict[str, object]]:
        return self.orders

    def get_orders(self, **_kwargs: object) -> list[dict[str, object]]:
        return self.orders

    def get_transactions(self, **_kwargs: object) -> list[object]:
        return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", default="1600x900", help="Root size as WIDTHxHEIGHT.")
    parser.add_argument("--capture", type=Path, help="Capture the native root window and exit.")
    args = parser.parse_args()
    try:
        width, height = (int(value) for value in args.size.casefold().split("x", maxsplit=1))
    except (TypeError, ValueError) as exc:
        raise SystemExit("--size must be WIDTHxHEIGHT.") from exc
    if width < 1080 or height < 720:
        raise SystemExit("--size must be at least 1080x720.")

    orders = _fixture_orders()
    history_snapshot = snapshot_from_history(
        orders,
        range_start=date(2025, 1, 1),
        range_end=date(2025, 5, 31),
        observed_at=datetime(2025, 5, 31, 22, 22, tzinfo=UTC),
    )
    root = tk.Tk()
    root.title("Duckets fake Past Positions · no network")
    root.geometry(f"{width}x{height}+0+0")
    root.minsize(1080, 720)
    root.configure(background=BACKGROUND)
    ttk.Style(root).theme_use("clam")
    _fixture_styles(root)
    _global_header(root)

    content = ttk.Frame(root, padding=(12, 7, 12, 8), style="StrategyPage.TFrame")
    content.pack(fill=tk.BOTH, expand=True)
    title = ttk.Frame(content, style="StrategyPage.TFrame")
    title.pack(fill=tk.X, pady=(0, 6))
    heading = ttk.Frame(title, style="StrategyPage.TFrame")
    heading.pack(side=tk.LEFT, fill=tk.X, expand=True)
    tk.Label(
        heading,
        text="Options Command Center",
        background=BACKGROUND,
        foreground=TEXT,
        font=("Segoe UI", 18, "bold"),
    ).pack(anchor=tk.W)
    tk.Label(
        heading,
        text="Discover, monitor, and safely close exact Schwab option positions.",
        background=BACKGROUND,
        foreground=MUTED_TEXT,
        font=("Segoe UI", 9),
    ).pack(anchor=tk.W)
    ttk.Button(title, text="Refresh", state=tk.DISABLED).pack(side=tk.RIGHT, anchor=tk.N)

    notebook = ttk.Notebook(content, style="StrategySecondary.TNotebook")
    notebook.pack(fill=tk.BOTH, expand=True)
    discover = ttk.Frame(notebook, style="StrategyPage.TFrame")
    positions = ttk.Frame(notebook, style="StrategyPage.TFrame")
    orders_tab = ttk.Frame(notebook, style="StrategyPage.TFrame")
    templates = ttk.Frame(notebook, style="StrategyPage.TFrame")
    past = ttk.Frame(notebook, style="StrategyPage.TFrame")
    for frame, label in (
        (discover, "Discover"),
        (positions, "Positions"),
        (orders_tab, "Orders"),
        (templates, "Templates"),
        (past, "Past Positions"),
    ):
        notebook.add(frame, text=label)
    _discover_placeholder(discover)

    portfolio = _snapshot()
    session = FakeHistorySession(orders)
    management = OptionsManagementView(
        root=root,
        positions_parent=positions,
        orders_parent=orders_tab,
        snapshot_loader=lambda: portfolio,
        session_factory=lambda: session,
        on_refresh=lambda: None,
        on_show_orders=lambda: notebook.select(orders_tab),
    )
    management.show_snapshot(portfolio)
    OptionsTemplatesView(
        root=root,
        parent=templates,
        roll_loader=lambda: (),
        exit_loader=lambda: (),
    )
    past_view = PastPositionsView(
        root=root,
        parent=past,
        history_loader=lambda: history_snapshot,
        on_related_orders=management.show_related_orders,
        autoload=False,
        today=lambda: date(2025, 5, 31),
    )
    past_view.show_snapshot(history_snapshot)
    notebook.select(past)

    if args.capture is not None:
        root.after(1400, lambda: _capture(root, args.capture))
    root.mainloop()


def _capture(root: tk.Tk, path: Path) -> None:
    try:
        root.lift()
        root.focus_force()
        root.attributes("-topmost", True)
        root.update_idletasks()
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_window_png(root.winfo_id(), path)
    finally:
        root.destroy()


def _fixture_orders() -> list[dict[str, object]]:
    orders: list[dict[str, object]] = []
    order_id = 5000

    def add(
        symbol: str,
        strategy: str,
        opened: datetime,
        closed: datetime,
        opening_legs: tuple[tuple[str, float, float, str], ...],
        closing_legs: tuple[tuple[str, float, float, str], ...],
    ) -> None:
        nonlocal order_id
        order_id += 1
        orders.append(
            _order(
                str(order_id),
                symbol,
                strategy,
                opened,
                opening_legs,
            )
        )
        order_id += 1
        orders.append(
            _order(
                str(order_id),
                symbol,
                strategy,
                closed,
                closing_legs,
            )
        )

    add(
        "NVDA",
        "VERTICAL",
        datetime(2025, 5, 2, 17, 14, tzinfo=UTC),
        datetime(2025, 5, 12, 18, 2, tzinfo=UTC),
        (
            ("SELL_TO_OPEN", 120, 6.20, "P"),
            ("BUY_TO_OPEN", 115, 1.50, "P"),
        ),
        (
            ("BUY_TO_CLOSE", 120, 2.00, "P"),
            ("SELL_TO_CLOSE", 115, 0.70, "P"),
        ),
    )
    add(
        "SPY",
        "IRON_CONDOR",
        datetime(2025, 5, 2, 16, 5, tzinfo=UTC),
        datetime(2025, 5, 9, 18, 20, tzinfo=UTC),
        _iron_condor_open(3.25),
        _iron_condor_close(0.40),
    )
    add(
        "AAPL",
        "COVERED",
        datetime(2025, 5, 1, 16, 5, tzinfo=UTC),
        datetime(2025, 5, 9, 17, 10, tzinfo=UTC),
        (("SELL_TO_OPEN", 205, 2.00, "C"),),
        (("BUY_TO_CLOSE", 205, 0.50, "C"),),
    )
    add(
        "TSLA",
        "VERTICAL",
        datetime(2025, 4, 30, 17, 5, tzinfo=UTC),
        datetime(2025, 5, 1, 18, 12, tzinfo=UTC),
        (("BUY_TO_OPEN", 280, 3.00, "C"), ("SELL_TO_OPEN", 290, 1.00, "C")),
        (("SELL_TO_CLOSE", 280, 1.20, "C"), ("BUY_TO_CLOSE", 290, 0.40, "C")),
    )
    add(
        "QQQ",
        "IRON_CONDOR",
        datetime(2025, 4, 24, 16, 30, tzinfo=UTC),
        datetime(2025, 4, 28, 18, 15, tzinfo=UTC),
        _iron_condor_open(3.60),
        _iron_condor_close(0.50),
    )
    add(
        "SPY",
        "VERTICAL",
        datetime(2025, 4, 21, 16, 40, tzinfo=UTC),
        datetime(2025, 4, 23, 18, 5, tzinfo=UTC),
        (("SELL_TO_OPEN", 550, 3.75, "P"), ("BUY_TO_OPEN", 540, 0.50, "P")),
        (("BUY_TO_CLOSE", 550, 0.75, "P"), ("SELL_TO_CLOSE", 540, 0.25, "P")),
    )
    add(
        "NVDA",
        "VERTICAL",
        datetime(2025, 4, 16, 16, 5, tzinfo=UTC),
        datetime(2025, 4, 17, 17, 5, tzinfo=UTC),
        (("SELL_TO_OPEN", 130, 1.50, "C"), ("BUY_TO_OPEN", 135, 0.50, "C")),
        (("BUY_TO_CLOSE", 130, 3.20, "C"), ("SELL_TO_CLOSE", 135, 0.40, "C")),
    )
    add(
        "AAPL",
        "IRON_CONDOR",
        datetime(2025, 4, 6, 16, 5, tzinfo=UTC),
        datetime(2025, 4, 11, 17, 45, tzinfo=UTC),
        _iron_condor_open(3.15),
        _iron_condor_close(0.50),
    )
    add(
        "TSLA",
        "VERTICAL",
        datetime(2025, 4, 3, 16, 5, tzinfo=UTC),
        datetime(2025, 4, 4, 17, 30, tzinfo=UTC),
        (("SELL_TO_OPEN", 250, 3.55, "P"), ("BUY_TO_OPEN", 240, 0.50, "P")),
        (("BUY_TO_CLOSE", 250, 0.40, "P"), ("SELL_TO_CLOSE", 240, 0.40, "P")),
    )
    add(
        "AMD",
        "NONE",
        datetime(2025, 3, 10, 16, 5, tzinfo=UTC),
        datetime(2025, 3, 14, 17, 30, tzinfo=UTC),
        (("BUY_TO_OPEN", 145, 1.00, "C"),),
        (("SELL_TO_CLOSE", 145, 1.00, "C"),),
    )
    return orders


def _iron_condor_open(net_credit: float) -> tuple[tuple[str, float, float, str], ...]:
    sold_total = net_credit + 1.00
    return (
        ("BUY_TO_OPEN", 485, 0.50, "P"),
        ("SELL_TO_OPEN", 490, sold_total / 2, "P"),
        ("SELL_TO_OPEN", 510, sold_total / 2, "C"),
        ("BUY_TO_OPEN", 515, 0.50, "C"),
    )


def _iron_condor_close(net_debit: float) -> tuple[tuple[str, float, float, str], ...]:
    bought_total = net_debit + 0.20
    return (
        ("SELL_TO_CLOSE", 485, 0.10, "P"),
        ("BUY_TO_CLOSE", 490, bought_total / 2, "P"),
        ("BUY_TO_CLOSE", 510, bought_total / 2, "C"),
        ("SELL_TO_CLOSE", 515, 0.10, "C"),
    )


def _order(
    order_id: str,
    symbol: str,
    strategy: str,
    executed_at: datetime,
    legs: tuple[tuple[str, float, float, str], ...],
) -> dict[str, object]:
    expiration = date(2025, 5, 16)
    order_legs = []
    executions = []
    for index, (instruction, strike, price, right) in enumerate(legs, 1):
        occ = _occ(symbol, expiration, right, strike)
        order_legs.append(
            {
                "legId": index,
                "instruction": instruction,
                "quantity": 1,
                "instrument": {
                    "assetType": "OPTION",
                    "symbol": occ,
                    "underlyingSymbol": symbol,
                    "multiplier": 100,
                },
            }
        )
        executions.append(
            {
                "legId": index,
                "quantity": 1,
                "price": price,
                "time": executed_at.isoformat(),
                "executionId": f"fixture-{order_id}-{index}",
                "fees": 0.0,
            }
        )
    return {
        "orderId": order_id,
        "accountNumber": "0907",
        "status": "FILLED",
        "complexOrderStrategyType": strategy,
        "enteredTime": (executed_at - timedelta(minutes=4)).isoformat(),
        "closeTime": executed_at.isoformat(),
        "orderLegCollection": order_legs,
        "orderActivityCollection": [
            {
                "activityType": "EXECUTION",
                "activityId": f"fixture-activity-{order_id}",
                "executionLegs": executions,
            }
        ],
    }


def _occ(symbol: str, expiration: date, right: str, strike: float) -> str:
    return f"{symbol:<6}{expiration:%y%m%d}{right}{int(round(strike * 1000)):08d}"


if __name__ == "__main__":
    main()
