"""Render only the Gameplan tab from local artifacts, without other app services."""
from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.ducket_bucket import DucketBucketApp
from app.ui.gameplan import GameplanTab
from app.ui.gameplan_data import load_gameplan, plan_sessions
from visual_option_management_fixture import _write_window_png


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore", type=Path)
    parser.add_argument("--session")
    parser.add_argument("--horizon", default="All horizons")
    parser.add_argument("--company", default="All companies")
    views = parser.add_mutually_exclusive_group()
    views.add_argument("--forecasts", action="store_true")
    views.add_argument("--trades", action="store_true")
    parser.add_argument("--select")
    parser.add_argument("--size", default="1708x960")
    parser.add_argument("--capture", type=Path)
    args = parser.parse_args()
    root = tk.Tk()
    root.title("Duckets · Gameplan preview")
    root.geometry(args.size + "+0+0")
    DucketBucketApp._apply_theme(SimpleNamespace(root=root))
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    for title in ("Rolling Forecasts", "Options Strategies", "Schwab Duckets", "Hyperliquid Duckets", "Gameplan Stats", "Gameplan"):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=title)
    notebook.select(frame)
    tab = GameplanTab(root, frame, datastore_root=args.datastore, auto_load=False)
    tab.horizon.set(args.horizon)
    tab.company.set(args.company)
    tab.view.set("trades" if args.trades else "forecasts")
    tab.date_box.configure(values=plan_sessions(args.datastore))
    tab.set_plan(load_gameplan(args.datastore, args.session))
    if args.select:
        tab.selected_key = next(tab._key(row) for row in tab.visible_rows if row.symbol == args.select)
        tab.render()
    if args.capture:
        def capture():
            try:
                args.capture.parent.mkdir(parents=True, exist_ok=True)
                _write_window_png(root.winfo_id(), args.capture)
                print(f"Captured {args.capture}")
            finally:
                root.destroy()
        root.after(1200, capture)
    root.mainloop()


if __name__ == "__main__":
    main()
