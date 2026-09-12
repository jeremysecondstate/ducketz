"""Render the actual Stats tab using saved local reviews, without other app services.

Example: python tests/visual_gameplan_stats_fixture.py --session 2026-09-11 --capture stats.png
"""
from __future__ import annotations

import argparse
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.ducket_bucket import DucketBucketApp
from app.ui.gameplan_stats import GameplanStatsTab
from app.ui.gameplan_stats_data import load_gameplan_stats, review_sessions
from visual_option_management_fixture import _write_window_png


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datastore", type=Path)
    parser.add_argument("--session")
    parser.add_argument("--horizon", default="All horizons")
    parser.add_argument("--symbol")
    parser.add_argument("--size", default="1708x960")
    parser.add_argument("--capture", type=Path)
    args = parser.parse_args()
    root = tk.Tk()
    root.title("Duckets · Gameplan Stats preview")
    # Tk canvases need an onscreen paint before Windows can capture the window.
    # This short-lived fixture never replaces or operates the running app.
    root.geometry(args.size + "+0+0")
    DucketBucketApp._apply_theme(SimpleNamespace(root=root))
    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    for label in ("Rolling Forecasts", "Options Strategies", "Schwab Duckets", "Hyperliquid Duckets", "Gameplan Stats"):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=label)
    notebook.select(frame)
    tab = GameplanStatsTab(root, frame, datastore_root=args.datastore, auto_load=False)
    tab.horizon.set(args.horizon)
    tab.selected_symbol = args.symbol
    try:
        tab.date_box.configure(values=review_sessions(args.datastore))
        tab.set_review(load_gameplan_stats(args.datastore, args.session))
    except Exception as exc:
        tab.status.set(str(exc))
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
