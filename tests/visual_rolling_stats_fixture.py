"""Capture the actual Gameplan Stats layout with entirely offline fixture data.

No refresh is invoked; the market loader fails if called. Neither brokerage
services nor unrelated application tabs are constructed. The body scrolls just
enough to show the complete rolling panel beneath the existing Order details.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from tkinter import ttk

from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.ui.gameplan_stats import GameplanStatsTab
from app.ui.gameplan_stats_data import load_gameplan_stats
from app.ui.theme import ACCENT, BACKGROUND, BORDER, FIELD_BACKGROUND, MUTED_TEXT, SURFACE, SURFACE_ALT, TABLE_FIELD, TEXT
from gameplan_stats_fixture import forecast, write_review
from test_gameplan_stats_ui import execution_snapshot
from test_rolling_price_widgets import fixture_snapshot


def configure_style(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    root.option_add("*Font", "{Segoe UI} 10")
    root.option_add("*TCombobox*Listbox.background", FIELD_BACKGROUND)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    style.configure(".", background=BACKGROUND, foreground=TEXT, fieldbackground=TABLE_FIELD, font=("Segoe UI", 10))
    style.configure("TFrame", background=BACKGROUND)
    style.configure("TButton", background=SURFACE_ALT, foreground=TEXT, bordercolor=BORDER, padding=(10, 6))
    style.map("TButton", background=[("active", ACCENT), ("disabled", SURFACE)], foreground=[("disabled", MUTED_TEXT)])
    style.configure("TNotebook", background=BACKGROUND, borderwidth=0)
    style.configure("TNotebook.Tab", background=SURFACE, foreground=TEXT, font=("Segoe UI", 9), padding=(12, 7))
    style.map("TNotebook.Tab", background=[("selected", SURFACE_ALT)])
    style.configure("TCombobox", background=FIELD_BACKGROUND, fieldbackground=FIELD_BACKGROUND, foreground=TEXT,
                    arrowcolor=TEXT, padding=5)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD_BACKGROUND)], foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", FIELD_BACKGROUND)], selectforeground=[("readonly", TEXT)])
    style.configure("TScrollbar", background=SURFACE_ALT, troughcolor=TABLE_FIELD, bordercolor=BACKGROUND, arrowcolor=MUTED_TEXT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", default="1860x1100")
    parser.add_argument("--missing", action="store_true")
    parser.add_argument("--top", action="store_true", help="Keep the initial viewport instead of scrolling to show the panel.")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = tk.Tk()
    root.withdraw()
    root.title("Ducketz Stats · Offline rolling-price fixture")
    width, height = (int(value) for value in args.size.split("x"))
    # ImageGrab must remain inside the actual display, including window chrome.
    root.geometry(f"{min(width, root.winfo_screenwidth() - 40)}x"
                  f"{min(height, root.winfo_screenheight() - 70)}+10+10")
    root.configure(background=BACKGROUND)
    configure_style(root)

    def forbidden_loader(**_kwargs):
        raise AssertionError("Offline visual fixture must not request market data")

    with tempfile.TemporaryDirectory(prefix="ducketz-rolling-stats-visual-") as temporary:
        store = Path(temporary)
        prices = fixture_snapshot(missing=args.missing)
        rows = [forecast(symbol=row.symbol, hour=hour, change=-.01 if (index + hour) % 3 else .02)
                for index, row in enumerate(prices.rows) for hour in range(4, 10)]
        write_review(store, rows)
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)
        stats_parent = ttk.Frame(notebook)
        notebook.add(stats_parent, text="Gameplan Stats")
        tab = GameplanStatsTab(root, stats_parent, datastore_root=store, auto_load=False, price_loader=forbidden_loader)
        tab.set_review(load_gameplan_stats(store))
        execution = execution_snapshot()
        for order in execution.report["orders"]:
            order["symbol"] = "DOCU" if order["symbol"] == "AAPL" else "DBX"
        tab.set_execution(execution)
        tab.rolling_prices.set_snapshot(prices)
        root.deiconify()
        root.update()
        if not args.top:
            bottom = tab.execution.frame.winfo_y() + tab.rolling_prices.frame.winfo_y() + tab.rolling_prices.frame.winfo_height()
            scroll_top = max(0, bottom - tab.canvas.winfo_height() + 12)
            bounds = tab.canvas.bbox("all")
            tab.canvas.yview_moveto(scroll_top / max(1, bounds[3] - bounds[1]))
            root.update()
        root.attributes("-topmost", True)
        root.update()

        def capture():
            args.output.parent.mkdir(parents=True, exist_ok=True)
            x, y = root.winfo_rootx(), root.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(args.output)
            print(f"Captured {root.winfo_width()}x{root.winfo_height()}, rolling panel "
                  f"{tab.rolling_prices.frame.winfo_width()}x{tab.rolling_prices.frame.winfo_height()}, "
                  f"{tab.rolling_prices._columns} columns, canvas view {tab.canvas.yview()}")
            root.destroy()

        root.after(350, capture)
        root.mainloop()


if __name__ == "__main__":
    main()
