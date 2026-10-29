"""Offline Atlas composition: the shared panel remains accessible beside saved Stats."""
from __future__ import annotations

import gc
import tkinter as tk
from tkinter import ttk

import pytest

from app.ui.gameplan_stats import GameplanStatsTab
from test_rolling_price_widgets import fixture_snapshot

ATLAS_SYMBOLS = ("AAPL", "AMZN", "GOOG", "MU", "NVDA", "SNDK", "COST", "CROX", "PATH", "TWST", "IONQ")


@pytest.mark.parametrize("size,narrow", [("1860x1000", False), ("800x900", True)])
def test_full_stats_geometry_preserves_all_eleven_symbols_below_order_details(tmp_path, size, narrow):
    root = tk.Tk()
    root.withdraw()
    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)

    def forbidden_loader(**_kwargs):
        pytest.fail("Geometry review must use mocked evidence only")

    view = GameplanStatsTab(root, frame, datastore_root=tmp_path, auto_load=False, price_loader=forbidden_loader)
    try:
        prices = fixture_snapshot(symbols=ATLAS_SYMBOLS)
        view.rolling_prices.set_snapshot(prices)
        root.geometry(size)
        root.deiconify()
        root.update()
        rolling = view.rolling_prices
        assert rolling.average_title.cget("text") == "11-symbol average"
        assert tuple(rolling.symbol_labels) == ATLAS_SYMBOLS
        assert view.execution.frame.grid_info()["column"] == (0 if narrow else 1)
        assert view.execution.frame.grid_info()["row"] == (1 if narrow else 0)
        details = view.execution.detail_title.master
        assert rolling.frame.winfo_y() >= details.winfo_y() + details.winfo_height()
        assert not view.execution.observation_label.winfo_manager()
        assert not view.execution.coverage_label.winfo_manager()
        assert len(rolling.tiles) == 11
        assert "disabled" not in rolling.refresh_button.state()
        assert rolling.title_label.winfo_x() + rolling.title_label.winfo_width() <= rolling.refresh_button.winfo_x()
        for symbol, tile in zip(ATLAS_SYMBOLS, rolling.tiles):
            assert tile.winfo_manager() == "grid"
            assert tile.winfo_x() >= 0
            assert tile.winfo_x() + tile.winfo_width() <= rolling.grid_frame.winfo_width()
            assert tile.winfo_y() + tile.winfo_height() <= rolling.grid_frame.winfo_height()
            assert rolling.symbol_labels[symbol].winfo_x() + rolling.symbol_labels[symbol].winfo_width() <= rolling.value_labels[symbol].winfo_x()
        # The page's ordinary vertical scroll exposes its full panel at any size.
        bottom = view.execution.frame.winfo_y() + rolling.frame.winfo_y() + rolling.frame.winfo_height()
        assert bottom <= float(view.canvas.cget("scrollregion").split()[3])
        view.canvas.yview_moveto(1)
        root.update()
        assert view.canvas.yview()[1] == 1.0
    finally:
        frame.destroy()
        root.destroy()
        del view, rolling, frame, root
        gc.collect()
